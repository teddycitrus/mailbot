"""`python -m dash` opens the local setup and progress console.

A shortcut, not a second implementation: it forwards to the `dashboard`
subcommand in src/main.py, so it takes the same flags. It has to sit in the
root because that is the only directory `python -m` searches by default.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.main import main  # noqa: E402  (needs the path above)

if __name__ == "__main__":
    argv = sys.argv[1:]
    # --env belongs to the top level parser, so it has to precede the subcommand.
    env = argv[:2] if argv[:1] == ["--env"] else []
    raise SystemExit(main([*env, "dashboard", *argv[len(env):]]))
