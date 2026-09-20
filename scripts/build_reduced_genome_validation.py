#!/usr/bin/env python3
"""Prepare held-out labels for the reduced-genome experiment."""

import argparse
from pathlib import Path

from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.data.reduced_genomes import build_validation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    validation = args.data_dir / "validation"
    payload = build_validation(
        registry_path=args.data_dir / "processed" / "gene_registry.parquet",
        reference_path=validation / "NC_000913.3.ncbi.json",
        mds42_path=validation / "AP012306.ncbi.json",
        ms56_pdf_path=validation / "MS56_Park_2014_supplement.pdf",
    )
    atomic_json(validation / "reduced_genomes.json", payload)


if __name__ == "__main__":
    main()
