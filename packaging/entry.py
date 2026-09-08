"""Executable entry point.

Bare double-click opens the console, because that is what someone who
downloaded an executable expects. Every CLI subcommand still works when
arguments are passed, so the packaged build and a source checkout behave
identically.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()  # Windows: stop child processes re-running main
    if len(sys.argv) == 1:
        sys.argv.append("dashboard")
    from src.main import main as cli
    return cli()


if __name__ == "__main__":
    raise SystemExit(main())
