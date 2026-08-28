#!/usr/bin/env python3
"""Freeze search finalists and prepare their pinned vEcoli lineage workflow."""

from __future__ import annotations

import argparse
from pathlib import Path

from yggdrisil_ecoli.vecoli import prepare_finalist_workflow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--vecoli-checkout", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--deletion-band", type=float, default=0.9)
    parser.add_argument("--lineage-seed", type=int, default=101)
    parser.add_argument("--generations", type=int, default=20)
    parser.add_argument("--sim-data-path", type=Path)
    args = parser.parse_args()
    manifest = prepare_finalist_workflow(
        graph_path=args.graph,
        registry_path=args.registry,
        vecoli_checkout=args.vecoli_checkout,
        output_root=args.output_root,
        manifest_path=args.manifest,
        config_path=args.config,
        count=args.count,
        deletion_band=args.deletion_band,
        lineage_seed=args.lineage_seed,
        generations=args.generations,
        sim_data_path=args.sim_data_path,
    )
    finalists = manifest["finalists"]
    if not isinstance(finalists, list):
        raise RuntimeError("generated manifest lacks finalists")
    workflow = manifest["workflow"]
    if not isinstance(workflow, dict):
        raise RuntimeError("generated manifest lacks workflow")
    print(f"Prepared {len(finalists)} frozen finalists")
    print(f"Experiment: {workflow['experiment_id']}")
    print(f"Manifest: {args.manifest.resolve()}")
    print(f"Config: {args.config.resolve()}")


if __name__ == "__main__":
    main()
