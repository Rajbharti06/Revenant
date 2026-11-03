# Evaluation Report: Ghost Data Still Exists deletion-audit agent

Reproduce everything below with:

```bash
python code/evaluation/main.py      # runs the agent on the 32 public samples + 5 custom cases and scores them
```

The harness (`code/evaluation/main.py`) writes `sample_predictions.csv`, `sample_trace.json` (per-case
reasoning: resolved users, every linked resource, its category, treatment, state and cited evidence),
`sample_scores.json` and `custom_scores.json`.

## Metric definitions

| Metric | Definition |
|---|---|
| Case-level status accuracy | `deletion_status` exact match |
| Step precision / recall / F1 | a step matches when the whole tuple (phase, system_id, resource_id, operation, state, evidence_id) matches; multiset comparison per case |
| Recall ignoring evidence | same tuple without `evidence_id`, which isolates citation errors from discovery errors |
| Field-level accuracy | gold steps aligned to predicted steps by (system_id, resource_id, operation); share of gold steps whose field is identical (`step_no` also checks the position) |
| Sequence accuracy | share of cases whose entire `steps_json` array equals the gold array, including order |

## Results (final version)

### Public samples (`dataset/sample_deletion_requests.csv`, 32 cases)

| Metric | Value |
|---|---|
| Case-level status accuracy | **1.000** (32/32) |
| Step precision | **1.000** (no step we emit is wrong) |
| Step recall | **0.907** |
| Step F1 | **0.951** |
| Step recall ignoring evidence | 0.907 (every step we find also cites the right evidence) |
| Field accuracy: phase / system_id / resource_id / operation / state / evidence_id | 0.907 each |
| Field accuracy: step_no (position) | 0.751 |
| Sequence accuracy | **0.562** (18/32 exact arrays) |

### Custom evaluation cases (created by us, `code/evaluation/custom_cases/`)

| Case | What it tests | Expected | Result |
|---|---|---|---|
| custom_01 | English **access request that explicitly says "do not delete"**, for a user whose data really is left over (sample case_05 identity and timing) | `NOT_APPLICABLE`, `[]` | pass |
| custom_02 | **German** deletion request, a language absent from the dataset, for sample case_06's subject | same steps as case_06 (search index leftover) | pass |
| custom_03 | Request arrives under the user's **current** email (`nadia.petrescu@...`), while the leftover support record still carries the **historical** address from `user_audit_log.csv` | same steps as case_10 | pass |
| custom_04 | **Spanish** data-portability request ("copia de mis datos ... no quiero cerrar la cuenta") | `NOT_APPLICABLE`, `[]` | pass |
| custom_05 | Email typed in **mixed case** (`Keiko.Nakamura@Gmail.com`); evidence stores it in lower case | same steps as case_24 | pass |

Custom-case scores: status 1.000, step P/R/F1 1.000/1.000/1.000, sequence 1.000.

### Full dataset (518 requests, no labels)

The output passes every contract check (`python code/main.py` followed by the validation in this
report's development log): 518 rows sorted by `case_id`, exact header, consecutive `step_no`, COMPLETE rows
contain only cited VERIFICATION steps, INCOMPLETE rows cite evidence only on FINDING steps, NOT_APPLICABLE
rows are `[]`, every operation/state pair is allowed, every cited id exists, and **no citation was captured
after the case's `audit_at`**. Distribution: 348 INCOMPLETE, 160 COMPLETE, 10 NOT_APPLICABLE, 5.5 steps per
case on average.

## Failures found and improvements made

Each row is one iteration of the loop "run the harness, read the failing cases in `sample_trace.json`,
fix the general cause (never the specific case), re-run".

| # | Change | Status acc. | Step P / R / F1 | Seq. acc. |
|---|---|---|---|---|
| v0 | Baseline: parsers for 6 log dialects, 5 JSON shapes, SQLite snapshots, TXT handover; exact identifier linking; latest-state-wins | 0.844 | 0.907 / 0.756 / 0.825 | 0.125 |
| v1 | (a) Intent rules: "copy of my profile" was misread as an access request, and "remove that search result" / "is gone" were not seen as deletion. (b) Identity links became time-agnostic (who owns a record never changes; only its *state* is time-bound). (c) Workflow fallback limited to the primary account, using a window around `reported_at`. (d) Legacy contact-index rows are secondary identifiers, so DELETE not REDACT | 1.000 | 0.994 / 0.870 / 0.928 | 0.438 |
| v2 | Bug: "absent by omission" from an unrelated SQL snapshot was overriding *retained* security records. Omission now only proves absence for records whose policy is DELETE; retained records with no in-window evidence still get their required end-state check | 1.000 | 0.994 / 0.896 / 0.943 | 0.531 |
| v3 | Experiment: extend the workflow fallback to analytics and security records. It linked anonymized analytics rows to the wrong person in the same batch, so it was **reverted** | 1.000 | 0.967 / 0.907 / 0.936 | 0.438 |
| v4 | Media: local OCR of the 11 screenshots, pdftotext for the 4 PDFs, and **catalog alias resolution** (`LINK_RESOURCE_IDENTITY` maps labels such as `delivery_confirmation_19.pdf` or `NST-2026-0718-1842` to resource ids). This surfaced a February attachment inside an August case | 1.000 | 0.972 / 0.896 / 0.933 | 0.500 |
| v5 | **Investigation window**: a resource's state is taken only from evidence captured in `[requested_at, audit_at]`. Every gold citation in the samples lies in that window, and older artifacts belong to earlier requests by the same person (the test set re-uses emails with different dates) | 1.000 | 0.994 / 0.896 / 0.943 | 0.531 |
| v6 | Full-dataset trace review: `legal_service` holds were falling back to DELETE and are now LEGAL_HOLD_RECORD → RETAIN. Connector rows marked "Continuous" are active; an archive shown with no restore block is a restore risk. Two COMPLETE-with-no-steps cases became evidenced INCOMPLETE cases | 1.000 | 0.994 / 0.896 / 0.943 | 0.531 |
| v7 | **Recreation paths**: activity like "Active connector repopulated downstream resource search_2016" proves the connector (or backup) is active and carries the downstream owner's data. The flow is linked to that person and cited by `activity_id` when no file evidence is newer (31 sync + 1 restore events) | 1.000 | 0.994 / 0.896 / 0.943 | 0.531 |
| v8 | **Joint workflow resolution**: batch-deleted accounts and batch-anonymized analytics rows are resolved across *all* requests at once (a candidate claimed by one case is eliminated for the others), preferring candidates that evidence inside the case's window actually looked up. Missing accounts on the 518-case set fell from 255 to 242 | 1.000 | 0.994 / 0.907 / 0.949 | 0.531 |
| v9 | An unevidenced *second* account (seen only in older captures) is no longer verified when the case already has an in-window account | 1.000 | **1.000 / 0.907 / 0.951** | **0.562** |

### Remaining failure analysis (14 samples not exact, all missing steps; none has a wrong step)

1. **Primary account not linkable (case_04, 18, 20, 21, 22, 23, 25, 29, 31, 32).** The only connection
   between the account id and the person is a `deletion_worker` action in a batch where 3–5 accounts were
   deleted within minutes of each other, and every candidate is looked up by in-window evidence. No evidence
   file names the owner, so the agent refuses to guess; it links only when elimination leaves one candidate.
2. **Anonymized analytics rows (case_03, 04, 21, 25, 26, 27, 28).** These records carry no identifier in any
   capture, so the same batch ambiguity applies.
3. **Security records that appear in no evidence file at all** (`audit_record_6645`, `audit_record_5569`). These
   cannot be derived from participant data.

Because the unresolvable resources are all VERIFICATION steps, status accuracy stays at 100% and step
precision at 1.000. We prefer a missing check to a check on someone else's record.

## Design choices validated by the evaluation

- **Claims are not evidence.** `reported_status` and activity `reported_result` are never used as state;
  activity is used only to *link* identities (catalog links, workflow actions).
- **Replica drift.** Rows from every table of a SQL snapshot are merged per resource, so a record deleted in
  the primary but alive in a failover or regional replica is reported as remaining (sample case_05).
- **Decoys.** Exact (case-insensitive) identifier matching keeps look-alikes such as `g.silva76.jr@gmail.com`
  apart from `g.silva76@gmail.com` (custom_05 checks the case-insensitive half).
- **Deterministic ordering.** Phases, then the operation order from the problem statement, then `system_id`,
  then `resource_id`. Every one of the 17 exact-match samples confirms the ordering.
