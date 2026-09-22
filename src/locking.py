"""One mailbot run at a time, per install.

The scheduled jobs overlap by design. Drafting fires every two hours, sending
every fifteen minutes, and both carry a resume-from-sleep trigger, so on every
wake the two start within the same second. Both then run `mirror`.

Two concurrent mirrors are the one case that can break the promise the whole
project rests on, that nobody is mailed twice:

  both read drafts_to_mirror and see the same row, which still has no copy
  both APPEND the draft to Gmail, so the folder now holds two
  the second set_draft_mirror overwrites the first Message-ID
  the send drops the copy it knows about, and the other stays in Drafts,
  addressed and ready, for a message that has already gone out

So runs are serialised. A job that cannot get the lock does not queue up behind
the holder and does not fail; it says so and exits cleanly, because every one
of these jobs runs again soon and a skipped tick costs nothing.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if sys.platform == "win32":  # pragma: no cover - platform split
    import msvcrt

    def _grab(handle) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _release(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:  # pragma: no cover - platform split
    import fcntl

    def _grab(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Busy(RuntimeError):
    """Another run holds the lock. Not an error; the caller should stand down."""


@contextmanager
def single_run(path: str | os.PathLike[str], wait_seconds: float = 20.0,
               poll: float = 0.5) -> Iterator[Path]:
    """Hold the install-wide lock for the duration of the block.

    An advisory file lock rather than a PID file, because the operating system
    drops it when the process dies. A PID file outlives a crash, and a stale
    one would silently stop every later run, which is a worse failure than the
    one being prevented.

    Waits briefly before giving up: the collisions worth surviving are two jobs
    starting in the same second, and those clear as soon as the short one is
    done. A long wait would only queue a fifteen-minute tick behind a drafting
    run that can take minutes, so give up and let the next tick have it.
    """
    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock, "a+b")
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                handle.seek(0)
                _grab(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise Busy(f"another mailbot run holds {lock}") from None
                time.sleep(poll)
        try:
            yield lock
        finally:
            try:
                _release(handle)
            except OSError:
                pass
    finally:
        handle.close()


def default_lock_path(db_path: str | os.PathLike[str]) -> Path:
    """Beside the database, so one lock covers one install's data."""
    return Path(db_path).with_suffix(".lock")
