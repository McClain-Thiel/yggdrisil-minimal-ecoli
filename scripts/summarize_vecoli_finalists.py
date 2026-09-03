#!/usr/bin/env python3
"""Summarize completed vEcoli divisions for every frozen finalist."""

from __future__ import annotations

import argparse
from pathlib import Path

from yggdrisil_ecoli.vecoli_results import summarize_vecoli_lineages


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = summarize_vecoli_lineages(args.manifest, args.output)
    finalists = result["finalists"]
    if not isinstance(finalists, list):
        raise RuntimeError("result lacks finalists")
    for finalist in finalists:
        if not isinstance(finalist, dict):
            raise RuntimeError("malformed finalist result")
        print(
            finalist["state_id"],
            f"{finalist['generations_completed']}/{finalist['maximum_generations']}",
            finalist["terminal_reason"],
        )
    print(f"Result: {args.output.resolve()}")


if __name__ == "__main__":
    main()
