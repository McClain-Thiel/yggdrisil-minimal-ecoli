from __future__ import annotations

import argparse
from collections.abc import Sequence

from yggdrisil.viewer import serve


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="yggdrisil")
    commands = parser.add_subparsers(required=True)

    inspect = commands.add_parser("inspect", help="inspect a search DAG")
    inspect.add_argument("graph", help="path to a Yggdrisil SQLite graph")
    inspect.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind host (default: 127.0.0.1; the inspector has no authentication)",
    )
    inspect.add_argument("--port", type=int, default=8765)
    inspect.add_argument("--no-open", action="store_true")

    args = parser.parse_args(argv)
    serve(
        args.graph,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
