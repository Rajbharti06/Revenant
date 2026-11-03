# Revenant

Revenant is an AI-assisted audit agent that proves whether a user’s “deleted” personal data is truly gone across databases, replicas, logs, backups and sync pipelines.

Built for **HackerRank Orchestrate (October 2026)**, challenge *Ghost Data Still Exists*. Final rank: **#948 of 4,094**.

A deletion workflow can report "Complete" while personal data survives in replicas, logs, backups and search
indexes, or gets silently re-created by a sync job. Revenant audits each deletion request and proves whether
the data is truly gone.

## What it does

- **Detects intent:** decides whether a request, in any language, actually asks for deletion. Access, export
  and marketing opt-out requests are left untouched.
- **Resolves identity:** links current and historical emails, phones, names and SHA-256 email hashes to one
  person, while ignoring look-alike decoy accounts.
- **Reads evidence:** 94 files across SQL snapshots replayed in SQLite, six log dialects, JSON exports, text
  handovers, PDFs and admin-console screenshots read with OCR or a vision model.
- **Reconstructs state:** determines what each system held at audit time using only evidence from the
  investigation window. Workflow status reports are treated as claims, never as proof.
- **Catches ghosts:** copies alive in failover replicas, sync connectors re-creating deleted records, backups
  that can still restore data, and deleted-account records still holding personal details.
- **Plans remediation:** applies the deletion policy (delete, redact, anonymize or retain) and outputs
  ordered findings, remediation and verification steps, with every finding citing the evidence that proves it.

## Design principle

Ownership is timeless, state is not: links between people and records may come from any capture, but every
decision uses only evidence captured after the request and before the audit date.

## Results

On the public labeled cases: **100% deletion-status accuracy, step precision 1.000 and F1 0.951**. Five
custom evaluation cases, covering German and Spanish requests, historical emails, mixed-case input and a
"do not delete" access request, also pass. All 518 graded cases pass output validation. When ownership is
ambiguous, Revenant refuses to guess rather than act on someone else's data.

Full metrics, the iteration history and a failure analysis are in
[`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).

## Stack

Python (standard library), SQLite, rapidocr and pdftotext. An optional LLM layer (Anthropic or NVIDIA)
handles multilingual intent detection and screenshot extraction, with response caching, token accounting
and a deterministic fallback, so every decision stays reproducible and explainable.

## Run it

The challenge dataset is not included in this repo. Copy it from the
[official challenge repository](https://github.com/interviewstreet/hackerrank-orchestrate-october26):

```bash
git clone --depth 1 https://github.com/interviewstreet/hackerrank-orchestrate-october26 challenge
cp -r challenge/dataset ./dataset
```

Then:

```bash
pip install -r requirements.txt   # optional: local OCR; screenshot facts are already cached
python code/main.py               # writes output.csv
python code/evaluation/main.py    # scores the public samples and the custom cases
```

Optional model-backed mode: put `NVIDIA_API_KEY` or `ANTHROPIC_API_KEY` in your environment or in a `.env`
file at the repo root (git-ignored). Without a key, Revenant falls back to rules and OCR automatically.

## Layout

| Path | Purpose |
|---|---|
| `code/ingest.py` | evidence parsers → uniform observations |
| `code/media.py`, `code/prompts/` | screenshot/PDF extraction (vision model or OCR) |
| `code/intent.py` | deletion-intent classifier |
| `code/audit.py` | identity resolution, linking, state reconstruction, policy, step planner |
| `code/llm.py` | stdlib LLM client (Anthropic / NVIDIA) with caching and token accounting |
| `code/evaluation/` | evaluation harness, reports, custom cases |
| `output.csv` | predictions submitted for the 518 graded requests |
