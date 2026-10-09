#!/usr/bin/env python3
"""Benchmark contextual correction against a goldset.

Usage:
    scripts/bench_correction.py <goldset.jsonl> [--provider jev|semif] [--timeout 2.0]

Goldset format (one JSON object per line):
    {"text": "打开jeff工具", "expected": "打开jev工具", "aliases": [{"heard":"jeff","word":"jev","meaning":"软件工具"}]}

Outputs precision, recall, F1, and lists false positives/negatives.
"""

import argparse
import json
import sys
from pathlib import Path


def load_goldset(path: Path) -> list[dict]:
    cases = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Invalid JSON at line {line_no}: {e}", file=sys.stderr)
    return cases


def run_correction(text: str, aliases: list, provider: str, timeout_s: float) -> str:
    from recordian.streaming_correction import StreamingHotwordCorrector

    corrector = StreamingHotwordCorrector(
        [],
        endpoint="http://192.168.5.111:42171",
        timeout_s=timeout_s,
        enabled=True,
        provider=provider,
        contextual_aliases=aliases,
    )
    try:
        return corrector.finish(text)
    finally:
        corrector.close()


def evaluate(goldset: list[dict], provider: str, timeout_s: float) -> dict:
    tp = 0  # true positive: should change & did change correctly
    fp = 0  # false positive: should keep & changed
    fn = 0  # false negative: should change & didn't
    tn = 0  # true negative: should keep & kept

    fp_cases = []
    fn_cases = []

    for case in goldset:
        text = case["text"]
        expected = case["expected"]
        aliases = case.get("aliases", [])
        should_change = text != expected

        actual = run_correction(text, aliases, provider, timeout_s)

        if should_change:
            if actual == expected:
                tp += 1
            else:
                fn += 1
                fn_cases.append({"text": text, "expected": expected, "actual": actual})
        else:
            if actual == text:
                tn += 1
            else:
                fp += 1
                fp_cases.append({"text": text, "expected": expected, "actual": actual})

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "fp_cases": fp_cases,
        "fn_cases": fn_cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goldset", type=Path, help="Path to goldset JSONL file")
    parser.add_argument("--provider", choices=["jev", "semif"], default="jev", help="Correction provider (default: jev)")
    parser.add_argument("--timeout", type=float, default=2.0, help="Timeout per utterance in seconds (default: 2.0)")
    args = parser.parse_args()

    if not args.goldset.exists():
        print(f"Goldset not found: {args.goldset}", file=sys.stderr)
        sys.exit(1)

    goldset = load_goldset(args.goldset)
    if not goldset:
        print("Goldset is empty", file=sys.stderr)
        sys.exit(1)

    print(f"Running benchmark: {len(goldset)} cases, provider={args.provider}, timeout={args.timeout}s")
    results = evaluate(goldset, args.provider, args.timeout)

    print(f"\n{'='*60}")
    print(f"Precision: {results['precision']:.2%} ({results['tp']}/{results['tp'] + results['fp']})")
    print(f"Recall:    {results['recall']:.2%} ({results['tp']}/{results['tp'] + results['fn']})")
    print(f"F1 Score:  {results['f1']:.2%}")
    print(f"{'='*60}")
    print(f"TP: {results['tp']}  FP: {results['fp']}  FN: {results['fn']}  TN: {results['tn']}")

    if results["fp_cases"]:
        print(f"\nFalse Positives ({len(results['fp_cases'])}):")
        for case in results["fp_cases"]:
            print(f"  '{case['text']}' → '{case['actual']}' (expected: '{case['expected']}')")

    if results["fn_cases"]:
        print(f"\nFalse Negatives ({len(results['fn_cases'])}):")
        for case in results["fn_cases"]:
            print(f"  '{case['text']}' → '{case['actual']}' (expected: '{case['expected']}')")


if __name__ == "__main__":
    main()
