"""Evidence ingestion: turn every evidence format into uniform observations.

An observation says "at time t, file F saw resource R in system S with this state,
carrying these identifiers". Everything downstream (identity linking, state
reconstruction, policy) works on observations only, so adding a new evidence
format means adding one parser here.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(ROOT, "dataset")
CACHE_DIR = os.path.join(ROOT, "code", "cache")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
RID_RE = re.compile(
    r"\b(account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"marketing|cache|object|telemetry|email_log|legal_hold|secondary)_\d+\b"
)
HEX64_RE = re.compile(r"\b[0-9a-f]{64}\b")

# resource-id prefix -> owning system (used when a source does not name the system)
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "marketing": "marketing_platform",
    "cache": "cache_service",
    "object": "object_storage",
    "legal_hold": "legal_service",
}

# default data category per system (deletion_rules.csv vocabulary)
SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "marketing_platform": "MARKETING_PROFILE",
    "cache_service": "CACHE_ENTRY",
    "object_storage": "SUPPORT_ATTACHMENT",
    "telemetry_service": "PRODUCT_TELEMETRY",
    "notification_service": "EMAIL_DELIVERY_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
}

SERVICE_ALIASES = {"support_replica": "support_platform", "attachment_preview": "support_platform"}

# fields that are direct identifiers (their presence = exposed PII)
PII_FIELDS = {
    "email", "phone", "email_hash", "full_name", "name", "display_name", "requester_name",
    "requester_email", "requester_phone", "customer_email", "owner_email", "owner_phone",
    "matched_value",
}
# fields that may carry an identifier used for linking (PII or stable references)
LINK_FIELDS = PII_FIELDS | {
    "customer_reference", "subject_reference", "user_id", "subject_ref", "lookup_key", "match_key",
    "scan_key", "subject_key", "owner_key", "correlation_key", "query",
}
NULLS = {None, "", "null", "none", "NULL", "None"}


@dataclass
class Obs:
    fid: str                 # evidence file_id (or activity_id)
    cap: str                 # when the evidence was captured (ISO)
    t: str                   # when this specific observation was made (ISO)
    system: str
    rid: str
    category: Optional[str] = None
    present: Optional[bool] = None   # record exists
    pii: Optional[bool] = None       # direct identifiers still exposed
    active: Optional[bool] = None    # data flow enabled / restore allowed
    ids: set = field(default_factory=set)
    weak: bool = False               # inferred from omission in a snapshot

    def key(self):
        return (self.system, self.rid)


def norm_id(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().strip('"').strip()
    if s in NULLS:
        return None
    return s.lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def collect_ids(d: dict) -> set:
    out = set()
    for k, v in d.items():
        if isinstance(v, dict):
            out |= collect_ids(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, dict):
                    out |= collect_ids(x)
        elif k in LINK_FIELDS:
            n = norm_id(v)
            if n:
                out.add(n)
                if k in ("phone", "owner_phone", "requester_phone"):
                    out.add("tel:" + norm_phone(n))
    return out


def has_pii(d: dict) -> bool:
    for k, v in d.items():
        if isinstance(v, dict) and has_pii(v):
            return True
        if k in PII_FIELDS and norm_id(v) and norm_id(v) != "[redacted]":
            return True
    return False


def to_bool(v) -> Optional[bool]:
    if v is None:
        return None
    s = str(v).strip().strip('"').lower()
    if s in ("1", "true", "yes", "allow", "allowed", "enabled", "active"):
        return True
    if s in ("0", "false", "no", "deny", "denied", "disabled", "blocked", "stopped"):
        return False
    return None


def system_for(rid: str, hint: Optional[str] = None) -> str:
    if hint:
        hint = SERVICE_ALIASES.get(hint, hint)
        if hint in SYSTEM_CATEGORY:
            return hint
    m = RID_RE.search(rid or "")
    if m:
        return PREFIX_SYSTEM.get(m.group(1), hint or "unknown")
    return hint or "unknown"


# ---------------------------------------------------------------- generic record -> obs
LOOKUP_RESULT = {
    "FOUND": dict(present=True),
    "NOT_FOUND": dict(present=False),
    "IDENTIFIABLE": dict(present=True, pii=True),
    "ANONYMIZED": dict(present=True, pii=False),
    "REDACTED": dict(present=True, pii=False),
    "ACTIVE": dict(present=True, active=True),
    "DISABLED": dict(present=True, active=False),
    "BLOCKED": dict(present=True, active=False),
}


def lookup_obs(fid, cap, t, system, rid, record_type, result, subject) -> Obs:
    o = Obs(fid, cap, t or cap, system_for(rid, system), rid, category=record_type or None)
    for k, v in LOOKUP_RESULT.get((result or "").upper(), {}).items():
        setattr(o, k, v)
    n = norm_id(subject)
    if n and n != "no direct identifier":
        o.ids.add(n)
        if o.present and o.pii is None and (EMAIL_RE.search(n) or HEX64_RE.search(n)):
            o.pii = True
    return o


def record_obs(fid, cap, t, system, rec: dict, collection: str = "") -> Optional[Obs]:
    rid = rec.get("resource_id")
    if not rid:
        return None
    system = system_for(rid, system)
    o = Obs(fid, cap, rec.get("observed_at") or t or cap, system, rid)
    o.ids = collect_ids(rec)
    status = rec.get("status_code")
    if status is not None:
        o.present = int(status) == 200
    elif "hits" in rec:
        o.present = (rec["hits"] or {}).get("total", 0) > 0
    elif "profile" in rec and rec["profile"] is None:
        o.present = False
    elif str(rec.get("account_status", "")).upper() in ("DELETED_PURGED",):
        o.present = False
    else:
        o.present = True
    if "download_available" in rec:
        o.present = bool(rec["download_available"])
    o.pii = has_pii(rec) if o.present else None
    for k in ("enabled", "restore_allowed"):
        if k in rec:
            o.active = to_bool(rec[k])
    col = (collection or "").lower()
    if "tombstone" in col or "deleted_customer" in col:
        o.category = "ACCOUNT_TOMBSTONE"
    elif "attachment" in col:
        o.category = "SUPPORT_ATTACHMENT"
    elif "marketing" in col:
        o.category = "MARKETING_PROFILE"
    elif "legal" in col:
        o.category = "LEGAL_HOLD_RECORD"
    return o


# ---------------------------------------------------------------- JSON
def parse_json(fid, cap, path) -> list[Obs]:
    d = json.load(open(path, encoding="utf-8"))
    out: list[Obs] = []
    if "service_exports" in d:
        t0 = d.get("exported_at", cap)
        for system, cols in d["service_exports"].items():
            for col, recs in cols.items():
                for r in recs:
                    o = record_obs(fid, cap, t0, system, r, col)
                    if o:
                        if "hits" in r:
                            for doc in r["hits"].get("documents", []):
                                o.ids |= collect_ids(doc)
                                o.pii = o.pii or has_pii(doc)
                        out.append(o)
    elif "snapshots" in d:
        for s in d["snapshots"]:
            for r in s.get("records", []):
                o = record_obs(fid, cap, s.get("captured_at", cap), s.get("data_store"), r, s.get("collection", ""))
                if o:
                    o.t = s.get("captured_at", cap)
                    out.append(o)
    elif d.get("records") and isinstance(d["records"], list) and "record_type" in d["records"][0]:
        for r in d["records"]:
            out.append(lookup_obs(fid, cap, r.get("recorded_at"), r.get("system"), r["resource_id"],
                                  r.get("record_type"), r.get("result"), r.get("subject_reference")))
    elif "records" in d:
        col = d.get("collection", "")
        sysname = d.get("source", "")
        for r in d["records"]:
            o = record_obs(fid, cap, d.get("captured_at", cap), None, r, col)
            if o:
                out.append(o)
    return out


# ---------------------------------------------------------------- SQL
TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_search_documents": "search_service",
    "search_documents": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def parse_sql(fid, cap, path) -> tuple[list[Obs], set]:
    """Returns observations plus the set of systems the snapshot fully covers."""
    con = sqlite3.connect(":memory:")
    con.executescript(open(path, encoding="utf-8").read())
    con.row_factory = sqlite3.Row
    out, covered = [], set()
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = {r[0]: r[1] for r in con.execute("select origin_system, captured_at from snapshot_scope")}
        covered |= set(scope)
    for tb in tables:
        if tb == "snapshot_scope":
            continue
        rows = [dict(r) for r in con.execute(f'select * from "{tb}"')]
        cols = [c[1] for c in con.execute(f'pragma table_info("{tb}")')]
        if "record_json" in cols:  # generic replica tables: one json document per row
            for r in rows:
                rec = json.loads(r["record_json"])
                o = record_obs(fid, cap, r.get("updated_at") or cap, r.get("origin_system"), rec,
                               r.get("collection_name", ""))
                if o:
                    o.t = cap
                    out.append(o)
                    covered.add(o.system)
            continue
        base = next((v for k, v in TABLE_SYSTEM.items() if tb.startswith(k)), None)
        if base is None and tb.startswith("customer_profiles"):
            base = "account_service"
        for r in rows:
            r2 = {k: v for k, v in r.items() if k not in ("source_resource_id",)}
            o = record_obs(fid, cap, cap, base, r2, tb)
            if o:
                o.t = scope.get(o.system, cap)
                if "account_status" in r and str(r["account_status"]).upper() in ("DELETED",):
                    o.category = "ACCOUNT_TOMBSTONE"
                out.append(o)
        if base:
            covered.add(base)
    return out, covered


# ---------------------------------------------------------------- logs
KV_RE = re.compile(r'(\w+)=("((?:[^"\\]|\\.)*)"|[^\s]+)')
BRACKET_RE = re.compile(r"^\[(\w+)\] \[([\w-]+)\] \[([\w-]+)\] (\w+): ?(.*)$")
PIPE_RE = re.compile(r"^\| (\w+) \| ([\w-]+) \| ([\w-]+) \| (\w+) \| ?(.*)$")
SYSLOG_RE = re.compile(r"^<\d+>\d+ \S+ ([\w-]+) ([\w-]+) - - ?(.*)$")


def kv(s: str) -> dict:
    d = {}
    for m in KV_RE.finditer(s):
        v = m.group(3) if m.group(3) is not None else m.group(2)
        d[m.group(1)] = v.replace('\\"', '"')
    return d


def parse_log_line(line: str) -> Optional[dict]:
    line = line.rstrip("\n")
    if len(line) < 21 or line[4] != "-":
        return None
    ts, rest = line[:20], line[21:].strip()
    d: dict = {}
    if rest.startswith("{"):
        try:
            d = json.loads(rest)
        except json.JSONDecodeError:
            return None
    elif (m := BRACKET_RE.match(rest)):
        d = kv(m.group(5)); d.update(level=m.group(1), service=m.group(2), trace_id=m.group(3), event=m.group(4))
    elif (m := PIPE_RE.match(rest)):
        d = kv(m.group(5)); d.update(level=m.group(1), service=m.group(2), trace_id=m.group(3), event=m.group(4))
    elif (m := SYSLOG_RE.match(rest)):
        d = kv(m.group(3)); d.update(service=m.group(1), trace_id=m.group(2))
    else:
        d = kv(rest)
        if "message" in d:
            inner = kv(d.pop("message"))
            d.update(inner)
        if "function" in d and "service" not in d:
            d["service"] = d["function"]
        if "requestId" in d and "trace_id" not in d:
            d["trace_id"] = d["requestId"]
    d["_ts"] = ts
    return d


def state_from_line(d: dict, o: Obs):
    ev = d.get("event", "")
    if "rows" in d:
        try:
            o.present = int(d["rows"]) > 0
        except ValueError:
            pass
    if "http_status" in d and ev.startswith("profile_lookup"):
        o.present = d["http_status"] == "200"
    if d.get("result") in ("hit", "miss") and "index" in ev:
        o.present = d["result"] == "hit"
    if "total_hits" in d:
        o.present = d["total_hits"] not in ("0",)
    for k in ("available", "archive_present"):
        if k in d and to_bool(d[k]) is not None and k != "archive_present":
            o.present = to_bool(d[k])
    if "marker_found" in d:
        o.present = to_bool(d["marker_found"]) is False or o.present
    if "enabled" in d:
        o.active = to_bool(d["enabled"]); o.present = True
    if "restore_allowed" in d:
        o.active = to_bool(d["restore_allowed"]); o.present = True
    if "decision" in d and "restore" in ev:
        o.active = to_bool(d["decision"]); o.present = True
    if "eligible" in d and "restore" in ev:
        blocked = to_bool(d.get("deletion_block"))
        o.active = bool(to_bool(d["eligible"])) and not blocked; o.present = True
    if "direct_identifiers" in d:
        o.pii = d["direct_identifiers"] not in ("0",); o.present = True
    if ev.endswith("_pii_scan") or ev == "analytics_identity_scan":
        o.present = True; o.pii = True
    if "attachment" in ev:
        o.category = "SUPPORT_ATTACHMENT"
    if ev in ("contact_lookup_completed", "contact_query_completed") or "email_opt_in" in d:
        o.category = "SECONDARY_IDENTIFIER"
    if ev in ("message_read",) or "message_text" in d:
        o.present = True
        o.pii = norm_id(d.get("message_text")) not in (None, "[redacted]")
    pii_vals = [d.get(k) for k in PII_FIELDS if k in d and k != "matched_value"]
    if pii_vals and o.pii is None:
        o.pii = any(norm_id(v) for v in pii_vals)
        if o.present is None:
            o.present = True


def parse_log(fid, cap, path) -> list[Obs]:
    lines = [p for p in (parse_log_line(l) for l in open(path, encoding="utf-8")) if p]
    out: list[Obs] = []
    traces: dict[str, list[dict]] = {}
    for d in lines:
        if d.get("event") == "record_lookup" and "system" in d:
            out.append(lookup_obs(fid, cap, d["_ts"], d.get("system"), d["resource_id"], d.get("record_type"),
                                  d.get("result"), d.get("subject_reference")))
            continue
        tr = d.get("trace_id") or f"_line{id(d)}"
        traces.setdefault(tr, []).append(d)
    for tr, ds in traces.items():
        ids = set()
        for d in ds:
            ids |= collect_ids(d)
        by_rid: dict[str, Obs] = {}
        for d in ds:
            rid = d.get("resource_id")
            if not rid:
                continue
            svc = d.get("service") or d.get("logger")
            o = by_rid.get(rid)
            if o is None:
                o = by_rid[rid] = Obs(fid, cap, d["_ts"], system_for(rid, svc), rid)
            o.t = d["_ts"]
            state_from_line(d, o)
        for o in by_rid.values():
            o.ids = set(ids)
            if o.present is None and o.active is None and o.pii is None:
                continue
            out.append(o)
    return out


# ---------------------------------------------------------------- text handover
HANDOVER_RE = re.compile(r"^- (\S+) \| (\S+) \| (\S+) \| (\S+) \| (\S+) \| (.+)$")


def parse_txt(fid, cap, path) -> list[Obs]:
    out = []
    for line in open(path, encoding="utf-8"):
        m = HANDOVER_RE.match(line.strip())
        if m:
            t, system, rid, cat, result, subj = m.groups()
            out.append(lookup_obs(fid, cap, t, system, rid, cat, result, subj))
    return out


# ---------------------------------------------------------------- PDF / image facts
def pdf_text(path) -> str:
    try:
        result = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True,
                              encoding="utf-8", timeout=60).stdout
        if result.strip():
            return result
    except (OSError, subprocess.SubprocessError):
        pass
    # Fallback: PyMuPDF (cross-platform, no external binary needed)
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # older PyMuPDF versions use 'fitz'
        doc = pymupdf.open(path)
        return "\n".join(page.get_text() for page in doc)
    except Exception:
        return ""


def facts_obs(fid, cap, facts: list[dict]) -> list[Obs]:
    """Facts are {system, resource_id, category, present, pii, active, identifiers[]} dicts produced
    by the document/image extractor (LLM/VLM or heuristic)."""
    out = []
    for f in facts:
        rid = f.get("resource_id")
        if not rid:
            continue
        o = Obs(fid, cap, f.get("observed_at") or cap, system_for(rid, f.get("system")), rid,
                category=f.get("category"), present=f.get("present"), pii=f.get("pii"), active=f.get("active"))
        for i in f.get("identifiers", []):
            n = norm_id(i)
            if n:
                o.ids.add(n)
        out.append(o)
    return out


def heuristic_doc_facts(text: str) -> list[dict]:
    """Fallback extractor for PDF text: each referenced resource id is linked to every
    identifier in the document (documents are single-subject business artifacts)."""
    rids = sorted(set(m.group(0) for m in RID_RE.finditer(text)))
    idents = sorted(set(e.lower() for e in EMAIL_RE.findall(text)) | set(USER_RE.findall(text)))
    low = text.lower()
    facts = []
    for rid in rids:
        f = {"resource_id": rid, "present": True, "identifiers": idents}
        if rid.startswith("support_") and ("attachment" in low or "upload" in low):
            f["category"] = "SUPPORT_ATTACHMENT"; f["pii"] = True
        if rid.startswith("audit_record_"):
            f["category"] = "SECURITY_AUDIT_LOG"
        facts.append(f)
    return facts


def load_media_facts() -> dict:
    p = os.path.join(CACHE_DIR, "media_facts.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    return {}


# ---------------------------------------------------------------- activity
LINK_ACT_RE = re.compile(r"linked (\S+) \((.*?)\) to (\S+?) and (user_\d+)|confirmed (\S+) \((.*?)\) belongs only to (\S+?) and (user_\d+)")


def load_activity() -> list[dict]:
    return list(csv.DictReader(open(os.path.join(DATASET, "system_activity.csv"), encoding="utf-8")))


def load_manifest() -> list[dict]:
    return list(csv.DictReader(open(os.path.join(DATASET, "evidence_files.csv"), encoding="utf-8")))


def ingest(media_facts: Optional[dict] = None) -> tuple[list[Obs], dict]:
    """Parse every evidence file. Returns (observations, coverage) where coverage maps
    file_id -> (captured_at, set of systems that snapshot fully covers)."""
    media_facts = media_facts if media_facts is not None else load_media_facts()
    obs: list[Obs] = []
    coverage: dict = {}
    for m in load_manifest():
        fid, cap = m["file_id"], m["captured_at"]
        path = os.path.join(DATASET, m["file_path"])
        ext = path.rsplit(".", 1)[-1].lower()
        try:
            if ext == "json":
                obs += parse_json(fid, cap, path)
            elif ext == "sql":
                o, cov = parse_sql(fid, cap, path)
                obs += o
                coverage[fid] = (cap, cov)
            elif ext == "log":
                obs += parse_log(fid, cap, path)
            elif ext == "txt":
                obs += parse_txt(fid, cap, path)
            elif ext == "pdf":
                facts = media_facts.get(fid) or heuristic_doc_facts(pdf_text(path))
                obs += facts_obs(fid, cap, facts)
            elif ext in ("png", "jpg", "jpeg"):
                obs += facts_obs(fid, cap, media_facts.get(fid, []))
        except Exception as e:  # keep the pipeline alive; report the broken file
            print(f"[ingest] failed on {fid} {path}: {e}")
    return obs, coverage
