"""Summarise an eval run: accuracy by call site, by sub-group, and failures.

    python analyze_results.py results/results.jsonl
    python analyze_results.py results/results.jsonl --failures      # show what broke
    python analyze_results.py results/results.jsonl --csv table.csv # for the paper

`summary.csv` from the harness only covers the last invocation; this reads the
accumulated `results.jsonl`, so it works across several runs into one folder.
Sub-groups come from the case id (actuator-refusal-029 -> "refusal"), which is
where the interesting structure lives: accuracy by *how the device was named*,
or by *whether an existing agent should have been reused*.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def group_of(case_id: str) -> str:
    parts = case_id.split("-")
    # actuator-refusal-029 -> refusal ; intent-003 -> (none)
    return parts[1] if len(parts) >= 3 and not parts[1].isdigit() else "-"


def table(rows, title, keys=("model", "category")):
    print(f"\n{title}")
    width = max((len(" / ".join(str(r[k]) for k in keys)) for r in rows), default=10)
    print(f"{'key':<{width}} {'n':>4} {'err':>4} {'acc':>7} {'lat(s)':>8} {'cost($)':>9}")
    print("-" * (width + 36))
    for r in rows:
        key = " / ".join(str(r[k]) for k in keys)
        lat = f"{r['lat']:.2f}" if r["lat"] is not None else "-"
        print(
            f"{key:<{width}} {r['n']:>4} {r['err']:>4} {r['acc']:>6.1%} {lat:>8} {r['cost']:>9.4f}"
        )


def summarise(records, keyfn):
    buckets = collections.defaultdict(list)
    for rec in records:
        buckets[keyfn(rec)].append(rec)
    rows = []
    for key, rs in sorted(buckets.items()):
        oks = [r for r in rs if r.get("ok")]
        lats = [r["latency_s"] for r in oks if r.get("latency_s") is not None]
        rows.append(
            {
                "key": key,
                "n": len(rs),
                "err": len(rs) - len(oks),
                "acc": sum(bool(r.get("passed")) for r in rs) / len(rs),
                "lat": sum(lats) / len(lats) if lats else None,
                "cost": sum(r.get("cost_usd", 0.0) for r in rs),
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("results", help="path to results.jsonl")
    ap.add_argument("--failures", action="store_true", help="print failing cases")
    ap.add_argument("--csv", help="write the per-category table to CSV")
    args = ap.parse_args()

    records = [
        json.loads(line)
        for line in Path(args.results).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    print(f"{len(records)} records from {args.results}")

    by_cat = summarise(records, lambda r: (r["model"], r["category"]))
    for row in by_cat:
        row["model"], row["category"] = row["key"]
    table(by_cat, "ACCURACY BY CALL SITE")

    by_group = [
        r
        for r in summarise(records, lambda r: (r["model"], r["category"], group_of(r["case_id"])))
        if r["key"][2] != "-"
    ]
    for row in by_group:
        row["model"], row["category"], row["group"] = row["key"]
    if by_group:
        table(by_group, "ACCURACY BY SUB-GROUP", keys=("model", "category", "group"))

    # Errors deserve their own line: with ~28K-token actuator prompts a small
    # model may simply exceed its context window, which is not the same as
    # answering wrongly.
    errs = [r for r in records if not r.get("ok")]
    if errs:
        print(f"\n{len(errs)} ERRORED CALLS (not scored as wrong answers):")
        for msg, count in collections.Counter(
            (r.get("error") or "")[:110] for r in errs
        ).most_common(8):
            print(f"  {count:>3}x {msg}")

    if args.failures:
        print("\nFAILURES (passed=false, no error):")
        for r in records:
            if not r.get("passed") and r.get("ok"):
                print(f"\n--- {r['model']} {r['case_id']}")
                print(f"    output: {(r.get('output') or '')[:300]}")

    if args.csv:
        import csv as _csv

        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=["model", "category", "n", "err", "acc", "lat", "cost"])
            w.writeheader()
            for row in by_cat:
                w.writerow({k: row[k] for k in w.fieldnames})
        print(f"\nwrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
