# Revenant

Built for HackerRank Orchestrate (October 2026, “Ghost Data Still Exists”), Revenant audits privacy deletion requests the way a forensic investigator would.

It ingests 94 evidence files in mixed formats: SQL replica snapshots replayed in SQLite, JSON exports, six log dialects, text handovers, PDFs, and admin-console screenshots read with OCR or a vision model.

It resolves each person’s current and historical identities, including old emails, phones, and SHA-256 email hashes, and reconstructs every record’s state at the audit date.

Revenant catches the failures that make deletions lie:

- copies still alive in failover replicas
- sync connectors re-creating deleted search entries
- backups that can still restore the data
- deleted-account records still holding personal details

It then applies retention policy (delete, redact, anonymize, or retain) and writes an ordered, evidence-cited plan of findings, remediation, and verification steps.

## Results

- 100% correct deletion status on public labeled cases
- step precision 1.000
- F1 score 0.951
- every one of the 518 graded cases passed output-format checks
- final rank: #948 of 4,094

## Stack

- Python (standard library)
- SQLite
- local OCR
- `pdftotext`
- optional LLM layer (Anthropic or NVIDIA) for multilingual intent detection and screenshot reading

The system falls back to deterministic logic so every decision is reproducible and explainable.
