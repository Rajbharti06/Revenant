"""Ghost Data Still Exists - deletion audit agent.

Usage:
    python code/main.py                       # audit dataset/deletion_requests.csv -> output.csv
    python code/main.py --requests dataset/sample_deletion_requests.csv --out code/evaluation/sample_predictions.csv
    python code/main.py --extract-media       # (re)build the PDF/image fact cache with the vision model
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import audit  # noqa: E402
import ingest  # noqa: E402
import intent  # noqa: E402
import llm  # noqa: E402
import media  # noqa: E402


def run(requests_path: str, out_path: str, trace_path: str | None = None) -> list[dict]:
    reqs = list(csv.DictReader(open(requests_path, encoding="utf-8")))
    facts = media.load_or_extract()
    obs, coverage = ingest.ingest(facts)
    ident = audit.Identities()
    idx = audit.EvidenceIndex(obs, coverage, ingest.load_activity(), ident)
    rules = audit.load_rules()
    intents = intent.classify(reqs)
    # resolve batch-deleted primary accounts jointly over every request we know about
    all_reqs = {r["case_id"]: r for p in (os.path.join(ingest.DATASET, "deletion_requests.csv"),
                                         os.path.join(ingest.DATASET, "sample_deletion_requests.csv"), requests_path)
                for r in csv.DictReader(open(p, encoding="utf-8"))}
    idx.assign_workflow_accounts([r for r in all_reqs.values() if intent.rule_intent(r["request_text"])])

    rows, traces = [], {}
    for r in sorted(reqs, key=lambda r: r["case_id"]):
        it = intents[r["case_id"]]
        if not it["deletion"]:
            rows.append({"case_id": r["case_id"], "deletion_status": "NOT_APPLICABLE", "steps_json": "[]"})
            traces[r["case_id"]] = {"intent": it}
            continue
        status, steps, tr = audit.plan_case(r, idx, rules)
        tr["intent"] = it
        traces[r["case_id"]] = tr
        rows.append({"case_id": r["case_id"], "deletion_status": status,
                     "steps_json": json.dumps(steps, ensure_ascii=False, separators=(",", ":"))})

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    if trace_path:
        json.dump(traces, open(trace_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    if os.path.abspath(requests_path) == os.path.abspath(os.path.join(ingest.DATASET, "deletion_requests.csv")):
        write_usage_report(len(reqs), sum(1 for r in rows if r["deletion_status"] != "NOT_APPLICABLE"))
    return rows


def write_usage_report(n_requests: int, n_audited: int):
    """Summarise model usage of the full-dataset run into code/evaluation/usage_report.md."""
    lines = ["# Token Usage and Cost Report", "",
             "Generated automatically by `python code/main.py` for the final full-dataset run that produced "
             "`output.csv` (518 requests).", ""]
    tot_calls = tot_in = tot_out = tot_cached = 0
    tot_cost = 0.0
    rows = []
    for model, u in sorted(llm.USAGE.items()):
        pin, pout = llm.PRICES.get(model, (0.0, 0.0))
        t_in = u["input_tokens"] + u.get("cached_input_tokens", 0)  # cache hits carry their original cost
        t_out = u["output_tokens"] + u.get("cached_output_tokens", 0)
        cost = t_in / 1e6 * pin + t_out / 1e6 * pout
        rows.append(f"| {llm.PROVIDER_NAME} | `{model}` | {u['calls']} | {u['cached_calls']} | {t_in:,} | "
                    f"{t_out:,} | {t_in + t_out:,} | ${cost:.4f} |")
        tot_calls += u["calls"]; tot_cached += u["cached_calls"]
        tot_in += t_in; tot_out += t_out; tot_cost += cost
    lines += ["| Provider | Model | API calls | Cache hits | Input tokens | Output tokens | Total tokens | Est. cost |",
              "|---|---|---|---|---|---|---|---|"]
    lines += rows or ["| - | (no model calls) | 0 | 0 | 0 | 0 | 0 | $0.0000 |"]
    lines += [f"| **All** | **overall** | **{tot_calls}** | **{tot_cached}** | **{tot_in:,}** | **{tot_out:,}** | "
              f"**{tot_in + tot_out:,}** | **${tot_cost:.4f}** |", "",
              f"- Requests: {n_requests} ({n_audited} audited as deletions, {n_requests - n_audited} NOT_APPLICABLE)",
              f"- Average tokens per request: {(tot_in + tot_out) / max(n_requests, 1):.1f}",
              f"- Estimated cost per request: ${tot_cost / max(n_requests, 1):.6f}",
              f"- LLM available during this run ({llm.PROVIDER} provider configured): **{llm.available()}**", ""]
    if not llm.available():
        lines += ["No API key was configured for the final run, so intent classification used the multilingual "
                  "rule classifier and screenshots were read with local OCR (rapidocr) + pdftotext. Both are "
                  "free and deterministic; the deterministic audit engine needs no model at all.", ""]
    lines += ["## Where models are used (when a key is set)", "",
              "| Stage | Anthropic model | NVIDIA model | Calls for this dataset |", "|---|---|---|---|",
              "| Request-intent classification | `claude-haiku-5-5` | `nvidia/nemotron-3.5-lightning-30b-a3b` | "
              "ceil(518/40) = 13 batched calls |",
              "| Screenshot / PDF fact extraction | `claude-sonnet-5-5` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | "
              "15 (one per media file), cached in `code/cache/` |",
              "| Identity, state reconstruction, policy, step planning | none (deterministic Python) | none | 0 |", "",
              "Projected cost with an Anthropic key: roughly 0.13M tokens, about $0.25 for the whole dataset "
              "(~$0.0005 per request), using the prices in `code/llm.py`. NVIDIA's hosted free tier costs $0.", ""]
    path = os.path.join(ingest.ROOT, "code", "evaluation", "usage_report.md")
    open(path, "w", encoding="utf-8").write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", default=os.path.join(ingest.DATASET, "deletion_requests.csv"))
    ap.add_argument("--out", default=os.path.join(ingest.ROOT, "output.csv"))
    ap.add_argument("--trace", default=None, help="optional JSON file with per-case reasoning trace")
    ap.add_argument("--extract-media", action="store_true")
    a = ap.parse_args()
    if a.extract_media:
        media.load_or_extract(force=True)
        return
    rows = run(a.requests, a.out, a.trace)
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    print(f"wrote {len(rows)} rows to {a.out}: {counts}")


if __name__ == "__main__":
    main()
