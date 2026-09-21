#!/usr/bin/env python3
"""Freeze search finalists and prepare their pinned vEcoli lineage workflow."""

from __future__ import annotations

import argparse
from pathlib import Path

from yggdrisil_ecoli.vecoli import prepare_finalist_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", dest="graph_path", type=Path, required=True)
    parser.add_argument(
        "--genes",
        dest="genes_path",
        type=Path,
        default=Path("data/processed/genes.parquet"),
    )
    parser.add_argument("--vecoli-checkout", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifest", dest="manifest_path", type=Path, required=True)
    parser.add_argument("--config", dest="config_path", type=Path, required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--deletion-band", type=float, default=0.9)
    parser.add_argument("--lineage-seed", type=int, default=101)
    parser.add_argument("--generations", type=int, default=20)
    parser.add_argument("--sim-data-path", type=Path)
    manifest = prepare_finalist_workflow(**vars(parser.parse_args()))
    print(f"Prepared {len(manifest['finalists'])} frozen finalists")
    print(f"Experiment: {manifest['workflow']['experiment_id']}")
    print(f"Config: {manifest['workflow']['config_path']}")


if __name__ == "__main__":
    main()
