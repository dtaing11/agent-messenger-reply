"""Command line for the eval set. See evals/README.md."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from evals.dataset_check import check_dataset
from evals.runner import ROOT, load_cases, run_case


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score Sprinkle on the eval conversations.")
    ap.add_argument("--category", action="append", help="only these categories (repeatable)")
    ap.add_argument("--case", action="append", help="only these case ids (repeatable)")
    ap.add_argument("--model", default=os.environ.get("AI_MODEL", "deepseek/deepseek-v4.1-flash"))
    ap.add_argument("--fallback-model", default=os.environ.get("AI_FALLBACK_MODEL", "google/gemini-2.5-flash-lite"))
    ap.add_argument("--repeat", type=int, default=1, help="run each case N times (models vary run to run)")
    ap.add_argument("--semantic", action="store_true", help="turn on meaning-based search (downloads ~70 MB once)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", type=Path, help="results file (default evals/results/<time>.json)")
    ap.add_argument("--check", action="store_true", help="only validate the dataset against the menu; no model calls")
    args = ap.parse_args(argv)

    cases = load_cases()
    problems = check_dataset(cases)
    if problems:
        print("Dataset problems:\n  " + "\n  ".join(problems))
        return 2
    if args.check:
        print(f"Dataset OK: {len(cases)} cases in {len({c['category'] for c in cases})} categories.")
        return 0
    if args.category:
        cases = [c for c in cases if c["category"] in args.category]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        print("Set OPENROUTER_API_KEY (use a separate key from the live bot's if you can).")
        return 2

    results = asyncio.run(_run_all(cases, args, key))
    out = args.out or ROOT / "evals" / "results" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"model": args.model, "fallback": args.fallback_model, "repeat": args.repeat,
                               "results": results}, indent=1, ensure_ascii=False))
    print_report(results)
    print(f"\nFull transcripts: {out}")
    return 0


async def _run_all(cases: list[dict], args, key: str) -> list[dict]:
    sem = asyncio.Semaphore(args.concurrency)

    async def one(c):
        async with sem:
            r = await run_case(c, key, args.model, args.fallback_model, args.semantic)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"{mark}  {c['category']:<14} {c['id']}", flush=True)
            return r

    return await asyncio.gather(*(one(c) for c in cases for _ in range(args.repeat)))


def print_report(results: list[dict]) -> None:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    print("\nCategory          Pass rate")
    total = sum(r["passed"] for r in results)
    for cat in sorted(by_cat):
        rs = by_cat[cat]
        n = sum(r["passed"] for r in rs)
        print(f"  {cat:<15} {n:>3}/{len(rs):<3} {100 * n / len(rs):5.0f}%")
    print(f"  {'ALL':<15} {total:>3}/{len(results):<3} {100 * total / max(len(results), 1):5.0f}%")
    fails = [r for r in results if not r["passed"]]
    if fails:
        print("\nFailed checks:")
        for r in fails:
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"  {r['id']}: {c['check']}" + (f"  ({c['detail']})" if c["detail"] else ""))


if __name__ == "__main__":
    sys.exit(main())
