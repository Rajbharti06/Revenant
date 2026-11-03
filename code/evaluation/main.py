"""Evaluation harness: scores predictions against expected outputs.

    python code/evaluation/main.py                     # run agent on samples + custom cases, score both
    python code/evaluation/main.py --pred P --gold G   # score any prediction file

Metrics
- status accuracy: deletion_status exact match
- step precision / recall / F1: steps matched as (phase, system_id, resource_id, operation, state, evidence_id)
- field accuracy: per field, over steps aligned by (system_id, resource_id, operation)
- sequence accuracy: share of cases whose whole steps_json array equals the gold array
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path):
    return {r["case_id"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}


def step_key(s, with_evidence=True):
    k = (s["phase"], s["system_id"], s["resource_id"], s["operation"], s["state"])
    return k + (s.get("evidence_id", ""),) if with_evidence else k


def score(pred: dict, gold: dict) -> dict:
    n = len(gold)
    status_ok = 0
    tp = fp = fn = 0
    tp_ne = 0
    field_hits, field_tot = Counter(), Counter()
    seq_ok = 0
    failures = []
    for cid, g in gold.items():
        p = pred.get(cid, {"deletion_status": "", "steps_json": "[]"})
        gs, ps = json.loads(g["steps_json"]), json.loads(p["steps_json"])
        if p["deletion_status"] == g["deletion_status"]:
            status_ok += 1
        gc, pc = Counter(map(step_key, gs)), Counter(map(step_key, ps))
        inter = sum((gc & pc).values())
        tp += inter; fp += sum(pc.values()) - inter; fn += sum(gc.values()) - inter
        gce, pce = Counter(step_key(s, False) for s in gs), Counter(step_key(s, False) for s in ps)
        tp_ne += sum((gce & pce).values())
        palign = {(s["system_id"], s["resource_id"], s["operation"]): s for s in ps}
        for s in gs:
            m = palign.get((s["system_id"], s["resource_id"], s["operation"]))
            for f in FIELDS:
                field_tot[f] += 1
                if m is not None and m.get(f) == s.get(f):
                    field_hits[f] += 1
        same = gs == ps
        seq_ok += same
        if not same or p["deletion_status"] != g["deletion_status"]:
            failures.append({"case_id": cid, "gold_status": g["deletion_status"], "pred_status": p["deletion_status"],
                             "missing": sorted(set(map(step_key, gs)) - set(map(step_key, ps))),
                             "extra": sorted(set(map(step_key, ps)) - set(map(step_key, gs)))})
    gold_steps = tp + fn
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / gold_steps if gold_steps else 0.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n if n else 0,
        "step_precision": prec, "step_recall": rec,
        "step_f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
        "step_recall_ignoring_evidence": tp_ne / gold_steps if gold_steps else 0.0,
        "field_accuracy": {f: field_hits[f] / field_tot[f] for f in FIELDS if field_tot[f]},
        "sequence_accuracy": seq_ok / n if n else 0,
        "failures": failures,
    }


def print_report(name, s, show=10):
    print(f"\n== {name}: {s['cases']} cases")
    print(f"status accuracy   {s['status_accuracy']:.3f}")
    print(f"step P/R/F1       {s['step_precision']:.3f} / {s['step_recall']:.3f} / {s['step_f1']:.3f}"
          f"   (recall w/o evidence {s['step_recall_ignoring_evidence']:.3f})")
    print("field accuracy    " + ", ".join(f"{k}={v:.3f}" for k, v in s["field_accuracy"].items()))
    print(f"sequence accuracy {s['sequence_accuracy']:.3f}")
    for f in s["failures"][:show]:
        print(f"  x {f['case_id']} gold={f['gold_status']} pred={f['pred_status']}")
        for k in f["missing"]:
            print("      missing", k)
        for k in f["extra"]:
            print("      extra  ", k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred")
    ap.add_argument("--gold")
    ap.add_argument("--show", type=int, default=40)
    a = ap.parse_args()
    if a.pred and a.gold:
        print_report(os.path.basename(a.pred), score(load(a.pred), load(a.gold)), a.show)
        return
    import main as agent
    root = os.path.dirname(os.path.dirname(HERE))
    ds = os.path.join(root, "dataset")
    sample_pred = os.path.join(HERE, "sample_predictions.csv")
    agent.run(os.path.join(ds, "sample_deletion_requests.csv"), sample_pred, os.path.join(HERE, "sample_trace.json"))
    s = score(load(sample_pred), load(os.path.join(ds, "sample_output.csv")))
    print_report("public samples", s, a.show)
    json.dump({k: v for k, v in s.items() if k != "failures"}, open(os.path.join(HERE, "sample_scores.json"), "w"), indent=1)
    custom_req = os.path.join(HERE, "custom_cases", "custom_requests.csv")
    if os.path.exists(custom_req):
        custom_pred = os.path.join(HERE, "custom_predictions.csv")
        agent.run(custom_req, custom_pred)
        c = score(load(custom_pred), load(os.path.join(HERE, "custom_cases", "custom_expected.csv")))
        print_report("custom cases", c, a.show)
        json.dump({k: v for k, v in c.items() if k != "failures"},
                  open(os.path.join(HERE, "custom_scores.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
