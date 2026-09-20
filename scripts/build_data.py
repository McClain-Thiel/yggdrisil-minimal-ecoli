#!/usr/bin/env python3
"""Optional source rebuild. Experiments use a prepared dataset instead."""

import argparse
from pathlib import Path

from yggdrisil_ecoli.data_build import build_data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--accept-kegg-terms", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    print(
        build_data(
            args.data_dir,
            accept_kegg_terms=args.accept_kegg_terms,
            refresh=args.refresh,
        )
    )


if __name__ == "__main__":
    main()
