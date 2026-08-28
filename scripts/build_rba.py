#!/usr/bin/env python3
"""Build the pinned E. coli K-12 WT resource-balance artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.rba_build import build_rba_artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/external/rba_ecoli_k12_wt"),
    )
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        manifest_path = build_rba_artifact(args.output_dir, refresh=args.refresh)
    except (DataValidationError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    print(manifest_path)


if __name__ == "__main__":
    main()
