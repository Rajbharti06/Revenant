"""Turn PDFs and screenshots into structured facts the audit engine can use.

Each fact: {resource_id, system?, category?, present?, pii?, active?, identifiers[]}.

Paths, best first:
  1. Vision LLM (ANTHROPIC_API_KEY set): screenshot/PDF text -> JSON facts via prompts/media_extraction.md
  2. Local OCR (rapidocr) for screenshots + pdftotext for PDFs, then row-wise heuristics
Results are cached in code/cache/media_facts.json so the full run is reproducible and cheap.
"""
from __future__ import annotations

import json
import os
import re

import ingest
import llm
from ingest import EMAIL_RE, HEX64_RE, RID_RE, USER_RE

PROMPT_PATH = os.path.join(ingest.ROOT, "code", "prompts", "media_extraction.md")
CACHE_PATH = os.path.join(ingest.CACHE_DIR, "media_facts.json")


def _prompt() -> str:
    return open(PROMPT_PATH, encoding="utf-8").read()


# ---------------------------------------------------------------- heuristics shared by OCR/PDF paths
STATE_WORDS = [
    (r"restore (is )?(allowed|enabled|permitted)|restorable|eligible", dict(active=True)),
    (r"restore (is )?(blocked|denied|disabled)|deletion block|blocked", dict(active=False)),
    (r"\b(disabled|paused|stopped|inactive)\b", dict(active=False)),
    (r"\b(enabled|running|delivering|delivered|active|syncing|continuous)\b", dict(active=True)),
    (r"not found|\b404\b|no results|0 results|deleted", dict(present=False)),
    (r"anonymi[sz]ed|no direct identifier|redacted", dict(pii=False)),
]


def row_facts(lines: list[str], context: str) -> list[dict]:
    """lines: OCR'd rows (left->right text). A resource id is associated with the identifiers and
    state words on its own row; document-level identifiers are attached when a row has none."""
    doc_ids = set(e.lower() for e in EMAIL_RE.findall(context)) | set(USER_RE.findall(context))
    facts: dict[str, dict] = {}
    for ln in lines:
        rids = [m.group(0) for m in RID_RE.finditer(ln)]
        if not rids:
            continue
        ids = set(e.lower() for e in EMAIL_RE.findall(ln)) | set(USER_RE.findall(ln)) | set(HEX64_RE.findall(ln))
        low = ln.lower()
        for rid in rids:
            if rid.startswith("user_"):
                continue
            f = facts.setdefault(rid, {"resource_id": rid, "present": True, "identifiers": []})
            f["identifiers"] = sorted(set(f["identifiers"]) | ids)
            for pat, st in STATE_WORDS:
                if re.search(pat, low):
                    for k, v in st.items():
                        f.setdefault(k, v)
            if ids and any("@" in i for i in ids) and "pii" not in f:
                f["pii"] = True
    for f in facts.values():
        if not f["identifiers"] and len(facts) == 1:
            f["identifiers"] = sorted(doc_ids)
    return list(facts.values())


def ocr_lines(path: str) -> list[str]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return []
    eng = ocr_lines.engine = getattr(ocr_lines, "engine", None) or RapidOCR()
    res, _ = eng(path)
    if not res:
        return []
    items = []
    for box, text, _conf in res:
        ys = [p[1] for p in box]; xs = [p[0] for p in box]
        items.append(((min(ys) + max(ys)) / 2, min(xs), text))
    items.sort()
    rows, cur, cy = [], [], None
    for y, x, t in items:
        if cy is None or abs(y - cy) <= 12:
            cur.append((x, t)); cy = y if cy is None else (cy + y) / 2
        else:
            rows.append(" ".join(t for _, t in sorted(cur))); cur = [(x, t)]; cy = y
    if cur:
        rows.append(" ".join(t for _, t in sorted(cur)))
    return rows


# ---------------------------------------------------------------- extraction
def alias_facts(text: str, aliases: list[tuple[str, str, str]], origin: str = "") -> list[dict]:
    """Business artifacts often show a display label (file name, statement number, customer key)
    instead of the resource id. The data catalog (LINK_RESOURCE_IDENTITY activity) maps those
    labels to resource ids; any label visible in the artifact yields a fact for that resource.
    A bare customer key (user_NN) is too generic to identify a record on its own, so it only counts in
    an artifact produced by the record's own system (e.g. the identity service's reference page)."""
    out = []
    has_direct_id = bool(EMAIL_RE.search(text))
    for rid, alias, system in aliases:
        if alias == rid or not re.search(r"(?<![\w.-])" + re.escape(alias) + r"(?![\w-])", text, re.I):
            continue
        if USER_RE.fullmatch(alias) and system != origin:
            continue
        f = {"resource_id": rid, "system": system, "present": True, "identifiers": [], "via_alias": alias}
        if system == "identity_service":
            f["category"] = "ACCOUNT_TOMBSTONE"; f["pii"] = has_direct_id
        elif system == "object_storage":
            f["category"] = "SUPPORT_ATTACHMENT"; f["pii"] = True
        else:
            f["pii"] = True
        out.append(f)
    return out


def media_aliases() -> list[tuple[str, str, str]]:
    out = []
    for a in ingest.load_activity():
        if a["event_type"] != "LINK_RESOURCE_IDENTITY":
            continue
        m = ingest.LINK_ACT_RE.search(a["details"])
        if m:
            g = [x for x in m.groups() if x is not None]
            out.append((g[0], g[1], ingest.system_for(g[0], a["target_system"])))
    return out


def extract_one(fid: str, path: str, origin: str) -> list[dict]:
    ext = path.rsplit(".", 1)[-1].lower()
    text = ingest.pdf_text(path) if ext == "pdf" else ""
    facts = _extract_facts(fid, path, origin, ext, text)
    if ext != "pdf":
        text = "\n".join(getattr(ocr_lines, "last", []))
    seen = {f["resource_id"] for f in facts}
    facts += [f for f in alias_facts(text, media_aliases(), origin) if f["resource_id"] not in seen]
    return facts


def _extract_facts(fid: str, path: str, origin: str, ext: str, text: str) -> list[dict]:
    ocr_lines.last = []
    if llm.available():
        content = [{"type": "text", "text": f"file_id={fid} origin_system={origin}"}]
        if ext == "pdf":
            content.append({"type": "text", "text": "Document text (pdftotext -layout):\n" + text[:20000]})
        else:
            content.append(llm.image_block(path))
        try:
            facts = llm.extract_json(llm.call(llm.VISION_MODEL, _prompt(), content, 6000))
            if isinstance(facts, dict):
                facts = facts.get("facts", [])
            model_facts = [f for f in facts if isinstance(f, dict) and RID_RE.fullmatch(str(f.get("resource_id", "")))]
            return merge_facts(model_facts, _heuristic_facts(path, ext, text))
        except Exception as e:
            print(f"[media] LLM extraction failed for {fid}: {e}; falling back to heuristics")
    return _heuristic_facts(path, ext, text)


def _heuristic_facts(path: str, ext: str, text: str) -> list[dict]:
    if ext == "pdf":
        return ingest.heuristic_doc_facts(text)
    lines = ocr_lines(path)
    ocr_lines.last = lines
    return row_facts(lines, "\n".join(lines))


def merge_facts(model: list[dict], heur: list[dict]) -> list[dict]:
    """Ensemble: union of resources seen by the vision model and by OCR/text heuristics. Identifiers are
    unioned; state fields come from the model when it states them, else from the heuristics."""
    out = {f["resource_id"]: dict(f) for f in heur}
    for f in model:
        g = out.setdefault(f["resource_id"], {"resource_id": f["resource_id"], "identifiers": []})
        for k in ("system", "category", "present", "pii", "active", "observed_at"):
            if f.get(k) is not None:
                g[k] = f[k]
        g["identifiers"] = sorted(set(g.get("identifiers", [])) | {str(i) for i in f.get("identifiers", []) if i})
    return list(out.values())


def load_or_extract(force: bool = False) -> dict:
    cache = {}
    if os.path.exists(CACHE_PATH) and not force:
        cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    changed = False
    for m in ingest.load_manifest():
        ext = m["file_path"].rsplit(".", 1)[-1].lower()
        if ext not in ("pdf", "png", "jpg", "jpeg") or (m["file_id"] in cache and not force):
            continue
        path = os.path.join(ingest.DATASET, m["file_path"])
        cache[m["file_id"]] = extract_one(m["file_id"], path, m["origin"])
        changed = True
    if changed:
        os.makedirs(ingest.CACHE_DIR, exist_ok=True)
        json.dump(cache, open(CACHE_PATH, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    return cache
