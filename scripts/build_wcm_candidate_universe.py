#!/usr/bin/env python3
"""Build the pinned Bristol WCM-1219 candidate universe."""

from __future__ import annotations

import argparse
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.wcm_universe_build import build_wcm_candidate_universe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("data/processed/gene_registry.parquet"),
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/processed/wcm_1219_candidate_universe.json"),
    )
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        output = build_wcm_candidate_universe(
            registry_path=args.registry,
            raw_dir=args.raw_dir,
            output_path=args.output,
            refresh=args.refresh,
        )
    except (DataValidationError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(output)


if __name__ == "__main__":
    main()
