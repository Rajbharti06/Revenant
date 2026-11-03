"""Deterministic deletion audit: identity resolution -> resource linking -> state at audit_at
-> policy -> ordered steps."""
from __future__ import annotations

import csv
import hashlib
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta

from ingest import DATASET, LINK_ACT_RE, SYSTEM_CATEGORY, Obs, norm_id, norm_phone, system_for

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OP_ORDER = {op: i for i, op in enumerate([
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
])}
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
PHASE_OF = {op: ("FINDING" if op.startswith("REPORT") else "VERIFICATION" if op.startswith("VERIFY")
                 else "REMEDIATION") for op in OP_ORDER}

# (finding, remediation, verification) per required treatment
TREATMENT_OPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
}


def load_rules() -> dict[str, str]:
    rules = {r["data_category"]: r["required_action"]
             for r in csv.DictReader(open(os.path.join(DATASET, "deletion_rules.csv"), encoding="utf-8"))}
    rules.setdefault("DATA_FLOW", "DISABLE")  # active flows must be stopped (problem statement)
    return rules


def treatment(category: str, rules: dict) -> str:
    if category == "DATA_FLOW":
        return "DISABLE"
    if category == "BACKUP_ARCHIVE":
        return "BLOCK"  # retain the archive, block restore of the subject's data
    return rules.get(category, "DELETE")


# ---------------------------------------------------------------- identity
def sha(e: str) -> str:
    return hashlib.sha256(e.strip().lower().encode()).hexdigest()


class Identities:
    def __init__(self):
        self.users: dict[str, dict] = {}
        for r in csv.DictReader(open(os.path.join(DATASET, "user_identities.csv"), encoding="utf-8")):
            self.users[r["user_id"]] = {"current": r, "history": []}
        for r in csv.DictReader(open(os.path.join(DATASET, "user_audit_log.csv"), encoding="utf-8")):
            u = self.users.setdefault(r["user_id"], {"current": {"user_id": r["user_id"]}, "history": []})
            u["history"].append(r)
        self.index: dict[str, set] = defaultdict(set)  # identifier -> user_ids
        for uid in self.users:
            for i in self.identifiers(uid):
                self.index[i].add(uid)

    def identifiers(self, uid: str) -> set:
        u = self.users.get(uid)
        if not u:
            return {uid}
        cur = u["current"]
        vals = {"user_id": [uid], "email": [cur.get("email")], "phone": [cur.get("phone")],
                "name": [cur.get("name")], "email_hash": [cur.get("email_hash")]}
        for h in u["history"]:
            vals.setdefault(h["field_name"], []).extend([h["old_value"], h["new_value"]])
        out = set()
        for f, vs in vals.items():
            for v in vs:
                n = norm_id(v)
                if not n:
                    continue
                out.add(n)
                if f == "email":
                    out.add(sha(n))
                if f == "phone":
                    out.add("tel:" + norm_phone(n))
        return out

    def resolve(self, email: str, at: str) -> list[str]:
        """Users that own `email` now or historically; current owners first."""
        e = email.strip().lower()
        cur = [u for u, d in self.users.items() if (d["current"].get("email") or "").lower() == e]
        hist = [u for u in self.index.get(e, set()) if u not in cur]
        return cur + sorted(hist)


# ---------------------------------------------------------------- index of observations
class EvidenceIndex:
    def __init__(self, obs: list[Obs], coverage: dict, activity: list[dict], ident: Identities):
        self.obs = obs
        self.by_key: dict[tuple, list[Obs]] = defaultdict(list)
        for o in obs:
            self.by_key[o.key()].append(o)
        self.coverage = coverage
        self.activity = activity
        self.ident = ident
        # resource -> [(time, user_ids, source_id)] links from observations and catalog activity
        self.links: dict[tuple, list] = defaultdict(list)
        for o in obs:
            users = set()
            for i in o.ids:
                users |= ident.index.get(i, set())
                if re.fullmatch(r"user_\d+", i):
                    users.add(i)
            if users:
                self.links[o.key()].append((o.t, users, o.fid))
        self.link_acts = []
        for a in activity:
            if a["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = LINK_ACT_RE.search(a["details"])
            if not m:
                continue
            g = [x for x in m.groups() if x is not None]
            rid, alias, email, uid = g
            key = (system_for(rid, a["target_system"]), rid)
            users = {uid} | ident.index.get(email.lower(), set())
            self.links[key].append((a["event_time"], users, a["activity_id"]))
            self.link_acts.append((key, a, alias))
        # resources ever mentioned in activity (for workflow-window linking)
        self.acts_by_time = sorted(activity, key=lambda a: a["event_time"])
        self._add_recreation_paths()

    REPOP_RE = re.compile(r"repopulated downstream resource (\w+)")

    def _add_recreation_paths(self):
        """A sync/restore that re-created a downstream record proves the flow (or backup) was
        active at that moment and that it carries the downstream record owner's data."""
        for a in self.activity:
            m = self.REPOP_RE.search(a["details"])
            if not m or a["event_type"] not in ("SYNC_RECORD", "RESTORE_BACKUP"):
                continue
            down = (system_for(m.group(1), a["target_system"]), m.group(1))
            src = (system_for(a["resource_id"], a["source_system"]), a["resource_id"])
            users = self.owners(down)
            if users:
                self.links[src].append((a["event_time"], users, a["activity_id"]))
            o = Obs(a["activity_id"], a["event_time"], a["event_time"], src[0], src[1],
                    category="DATA_FLOW" if src[0] == "integration_service" else "BACKUP_ARCHIVE",
                    present=True, active=True)
            self.by_key[src].append(o)

    def owners(self, key) -> set:
        s = set()
        for _, users, _ in self.links.get(key, []):
            s |= users
        return s

    def linked_resources(self, uids: set) -> dict[tuple, str]:
        """resource key -> evidence id that links it to the subject. Ownership is static, so links
        may come from any capture; the *state* used for the decision is restricted to audit_at."""
        out = {}
        for key, ls in self.links.items():
            for t, users, src in sorted(ls):
                if users & uids:
                    out.setdefault(key, src)
        return out

    WORKFLOW_SYSTEMS = ("account_service",)
    JOINT_SYSTEMS = ("account_service", "analytics_platform")
    workflow_assign: dict = {}

    def assign_workflow_accounts(self, reqs: list[dict]):
        """Joint resolution of workflow-touched records across all requests. Each workflow deletes one
        primary profile (and anonymizes one analytics profile) near its reported_at; batches put several
        actions minutes apart. A candidate claimed by one case (because it is that case's only option)
        is removed from every other case's window, repeated until nothing changes. Request inputs only."""
        assigned: dict = {}
        users_of = {r["case_id"]: set(self.ident.resolve(r["email"], r["requested_at"])) for r in reqs}
        for system in self.JOINT_SYSTEMS:
            cands = {}
            for r in reqs:
                uids = users_of[r["case_id"]]
                if any(k[0] == system for k in self.linked_resources(uids)):
                    continue
                cands[r["case_id"]] = self._window_candidates(r, uids, system)
            done: dict = {}
            changed = True
            while changed:
                changed = False
                for cid, cs in cands.items():
                    if cid in done:
                        continue
                    taken = {k: c for c, (k, _) in done.items()}
                    free = {k: s for k, s in cs.items() if k not in taken or users_of[taken[k]] & users_of[cid]}
                    if len(free) == 1:
                        done[cid] = next(iter(free.items()))
                        changed = True
            for cid, v in done.items():
                assigned.setdefault(cid, []).append(v)
        self.workflow_assign = assigned

    def _window_candidates(self, req: dict, uids: set, system: str = "account_service") -> dict:
        rep = datetime.fromisoformat(req["reported_at"].replace("Z", "+00:00"))
        lo = (rep - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (rep + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = {}
        for a in self.acts_by_time:
            if a["event_time"] < lo:
                continue
            if a["event_time"] > hi:
                break
            if a["source_system"] != "deletion_worker":
                continue
            key = (system_for(a["resource_id"], a["target_system"]), a["resource_id"])
            if key[0] != system or (self.owners(key) - uids):
                continue
            out.setdefault(key, a["activity_id"])
        # the audit of this request must be able to observe the account: prefer candidates that some
        # evidence captured inside [requested_at, audit_at] actually looked up
        seen = {k: v for k, v in out.items()
                if any(req["requested_at"] <= o.cap <= req["audit_at"] for o in self.by_key.get(k, []))}
        return seen or out

    def workflow_resources(self, req: dict, uids: set, known: dict) -> dict[tuple, str]:
        out = {k: src for k, src in self.workflow_assign.get(req["case_id"], [])}
        if not any(k[0] == "account_service" for k in list(known) + list(out)):
            out.update(self._workflow_resources_single(req, uids, known))
        return out

    def _workflow_resources_single(self, req: dict, uids: set, known: dict) -> dict[tuple, str]:
        """Fallback for the primary account: the privacy workflow deletes the subject's primary
        profile shortly before it reports a status. Take deletion-worker actions on account
        records in a window around reported_at whose owner is not some other known person; link
        only when exactly one candidate remains."""
        rep = datetime.fromisoformat(req["reported_at"].replace("Z", "+00:00"))
        lo = (rep - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (rep + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cand = defaultdict(set)
        known_systems = {k[0] for k in known}
        src = {}
        for a in self.acts_by_time:
            if a["event_time"] < lo:
                continue
            if a["event_time"] > hi:
                break
            if a["source_system"] != "deletion_worker":
                continue
            key = (system_for(a["resource_id"], a["target_system"]), a["resource_id"])
            if key[0] not in self.WORKFLOW_SYSTEMS or key[0] in known_systems:
                continue
            if self.owners(key) - uids:
                continue
            cand[key[0]].add(key)
            src.setdefault(key, a["activity_id"])
        return {next(iter(ks)): src[next(iter(ks))] for ks in cand.values() if len(ks) == 1}

    def state_at(self, key, at: str, since: str = ""):
        """Latest evidence about a resource inside the investigation window [since, at]: captured
        after the request was filed and no later than audit_at. Observations from the same file at
        the same instant are merged (a record present in any replica table is present)."""
        cands = [o for o in self.by_key.get(key, []) if since <= o.cap <= at and o.t <= at]
        if not cands:
            return self.omission_state(key, at, since)
        latest_t = max(o.t for o in cands)
        top = [o for o in cands if o.t == latest_t]
        top.sort(key=lambda o: (o.cap, o.fid))
        fid = top[-1].fid
        same = [o for o in top if o.fid == fid]
        merged = Obs(fid, same[0].cap, latest_t, key[0], key[1])
        for o in same:
            if o.present is not None:
                merged.present = bool(merged.present) or o.present
            if o.pii is not None:
                merged.pii = bool(merged.pii) or o.pii
            if o.active is not None:
                merged.active = bool(merged.active) or o.active
        cats = [o.category for o in cands if o.category]
        merged.category = cats[-1] if cats else None
        if any(o.category == "SUPPORT_ATTACHMENT" for o in cands):
            merged.category = "SUPPORT_ATTACHMENT"
        if any(o.category == "SECONDARY_IDENTIFIER" for o in cands):
            merged.category = "SECONDARY_IDENTIFIER"
        return merged

    def omission_state(self, key, at, since=""):
        """A full snapshot that covers the system but lacks the record shows it is absent. Only used for
        records never observed explicitly; a record seen elsewhere needs its own in-window evidence."""
        if self.by_key.get(key):
            return None
        best = None
        for fid, (cap, systems) in self.coverage.items():
            if since <= cap <= at and key[0] in systems and (best is None or cap > best[1]):
                best = (fid, cap)
        if not best:
            return None
        return Obs(best[0], best[1], best[1], key[0], key[1], present=False, weak=True)


# ---------------------------------------------------------------- planner
def plan_case(req: dict, idx: EvidenceIndex, rules: dict) -> tuple[str, list[dict], dict]:
    at = req["audit_at"]
    uids = set(idx.ident.resolve(req["email"], req["requested_at"]))
    subject_ids = set()
    for u in uids:
        subject_ids |= idx.ident.identifiers(u)
    linked = idx.linked_resources(uids)
    linked.update(idx.workflow_resources(req, uids, linked))

    findings, remediations, verifications = [], [], []
    pending = []  # required end states we know about but cannot evidence at audit_at
    trace = {"users": sorted(uids), "resources": {}}
    for key in sorted(linked):
        st = idx.state_at(key, at, req["requested_at"])
        if st is not None and st.weak and treatment(SYSTEM_CATEGORY.get(key[0]), rules) != "DELETE":
            st = None  # omission only proves absence for records that must be deleted
        if st is None:
            cat = SYSTEM_CATEGORY.get(key[0])
            if cat == "USER_PROFILE" or treatment(cat, rules) == "RETAIN":
                pending.append((TREATMENT_OPS[treatment(cat, rules)][2], key, ""))
            continue
        cat = st.category or SYSTEM_CATEGORY.get(key[0], "USER_PROFILE")
        treat = treatment(cat, rules)
        f_op, r_op, v_op = TREATMENT_OPS[treat]
        violation = False
        if treat == "DELETE":
            if st.present is None:
                continue
            violation = st.present
        elif treat in ("REDACT", "ANONYMIZE"):
            if not st.present:
                continue
            violation = bool(st.pii)
        elif treat in ("DISABLE", "BLOCK"):
            if st.active is None and treat == "BLOCK" and st.present:
                st.active = True  # an archive shown without a restore block can still be restored
            if st.active is None:
                continue
            violation = st.active
        elif treat == "RETAIN":
            if not st.present:
                continue
        trace["resources"]["%s/%s" % key] = {"category": cat, "treatment": treat, "violation": violation,
                                             "evidence": st.fid, "present": st.present, "pii": st.pii,
                                             "active": st.active}
        if violation:
            findings.append((f_op, key, st.fid))
            remediations.append((r_op, key, ""))
            verifications.append((v_op, key, ""))
        else:
            verifications.append((v_op, key, st.fid))

    status = "INCOMPLETE" if findings else "COMPLETE"
    # an unevidenced extra account is only checked when no account was evidenced in the window
    if any(k[0] == "account_service" for _, k, _ in findings + verifications):
        pending = [p for p in pending if p[1][0] != "account_service"]
    if status == "INCOMPLETE":
        verifications = [(op, key, "") for op, key, _ in verifications] + pending
    rows = findings + remediations + verifications
    rows.sort(key=lambda r: (PHASES.index(PHASE_OF[r[0]]), OP_ORDER[r[0]], r[1][0], r[1][1]))
    steps = [{"step_no": i + 1, "phase": PHASE_OF[op], "system_id": key[0], "resource_id": key[1],
              "operation": op, "state": OP_STATE[op], "evidence_id": ev}
             for i, (op, key, ev) in enumerate(rows)]
    return status, steps, trace
