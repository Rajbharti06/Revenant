"""Decide whether a request_text actually authorizes deletion.

Primary path: an LLM classifies requests in batches (any language). Fallback / cross-check:
a multilingual rule set. Non-deletion cues (access, correction, export, marketing opt-out,
explicit "do not delete") override deletion keywords, because a request that merely
mentions deletion must not trigger destructive work.
"""
from __future__ import annotations

import json
import re

import llm

DELETE_CUES = [
    r"\bdelet", r"\beras", r"\bremove (my|the|this) (account|profile)", r"\bclose (my|this|the) account",
    r"\baccount[- ]deletion", r"\bpermanent", r"\bremove records", r"\bremove any remaining",
    r"\bclosed my account", r"\bfinish the deletion", r"\bpersonal[- ]data deletion", r"\bremov",
    r"\bis gone\b", r"cannot identify me", r"\bclosed\b", r"would like it closed",
    # es / pt / fr / de / it
    r"eliminaci[oó]n", r"eliminar", r"exclus[aã]o", r"excluir", r"suppression", r"supprimer",
    r"l[oö]schen", r"l[oö]schung", r"cancellazione", r"cancellare",
    # ko / ja / zh
    "삭제", "削除", "删除", "退会",
]
NON_DELETE_CUES = [
    r"not a request to (erase|delete)", r"(don't|do not|dont) delete", r"marketing lists? only",
    r"stop the promotional", r"\bunsubscribe\b", r"download my (account )?(information|data)",
    r"portable format", r"send me a copy", r"copy of (the )?information", r"copy of last",
    r"what personal information you hold",
    r"misspelled", r"out of date", r"change the email", r"resetting the login", r"reset(ting)? (my )?password",
    r"\binvoices?\b", r"update my (phone|address|name)", r"correct(ion)? (of )?my",
]

SYSTEM = (
    "You audit privacy requests for an e-commerce platform. For each request decide whether the "
    "person is asking to DELETE/ERASE/CLOSE their account or personal data (in any language). "
    "Access, copy/export/portability, correction, email change, login help, invoice copies and "
    "marketing opt-outs are NOT deletion, even if they mention deletion. Reply with only a JSON object "
    'mapping each id to "DELETE" or "NON_DELETE".'
)


def rule_intent(text: str) -> bool:
    t = text.lower()
    if any(re.search(p, t) for p in NON_DELETE_CUES):
        return False
    return any(re.search(p, t) for p in DELETE_CUES)


def classify(requests: list[dict], batch_size: int = 40) -> dict[str, dict]:
    """Returns case_id -> {"deletion": bool, "source": "llm"|"rules", "agree": bool}."""
    out = {r["case_id"]: {"deletion": rule_intent(r["request_text"]), "source": "rules", "agree": True}
           for r in requests}
    if not llm.available():
        return out
    for i in range(0, len(requests), batch_size):
        batch = requests[i:i + batch_size]
        payload = "\n".join(json.dumps({"id": r["case_id"], "text": r["request_text"]}, ensure_ascii=False)
                            for r in batch)
        try:
            ans = llm.extract_json(llm.call(llm.TEXT_MODEL, SYSTEM, [{"type": "text", "text": payload}], 1500))
        except Exception as e:  # network / parse problems -> keep rule answers
            print(f"[intent] LLM batch failed, using rules: {e}")
            continue
        for r in batch:
            v = str(ans.get(r["case_id"], "")).upper()
            if v in ("DELETE", "NON_DELETE"):
                rule = out[r["case_id"]]["deletion"]
                out[r["case_id"]] = {"deletion": v == "DELETE", "source": "llm", "agree": (v == "DELETE") == rule}
    return out
