"""Filesystem primitives that pin down `sable`'s durability envelope.

The engine never relies on incidental OS behaviour. Every mutation that must
survive a power loss funnels through this module, which encodes the invariants
explicitly:

1. Content is written to a **sibling temporary file**, never in place.
2. The temporary file is ``fsync``-ed so its blocks reach stable storage.
3. ``rename`` swaps it into place; POSIX guarantees this is atomic, so a crash
   observes either the old file or the new one, never a blend.
4. The **containing directory** is ``fsync``-ed so the directory entry itself
   survives. Skipping step 4 is the classic way to "lose" a file whose data was
   perfectly durable.

Refusing to hand-wave these steps is the reason the recovery tests can make
hard claims instead of statistical ones.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

__all__ = ["ATOMIC_TMP_SUFFIX", "atomic_write_bytes", "fsync_directory"]

ATOMIC_TMP_SUFFIX: Final = ".tmp-sable"


def fsync_directory(path: Path) -> None:
    """Persist directory-entry mutations (creations, renames, unlinks).

    Args:
        path: Directory whose entries were mutated.

    Returns:
        None. The call is best-effort on platforms that cannot open a directory
        for reading (notably Windows), where directory durability degrades to
        whatever the platform guarantees. Rather than silently pretending the
        guarantee holds everywhere, this is documented explicitly in the
        README's durability section.
    """
    flags = getattr(os, "O_DIRECTORY", os.O_RDONLY)
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            return
    finally:
        os.close(fd)


def atomic_write_bytes(target: Path, payload: bytes) -> int:
    """Replace ``target`` with ``payload`` atomically and return its byte length.

    Args:
        target: Destination file; created or replaced as a whole.
        payload: Complete serialized content.

    Returns:
        Number of bytes committed to ``target``.

    Raises:
        OSError: Propagated from the underlying filesystem after the temporary
            sibling has been removed, so a failed publish cannot leave a
            half-written artifact behind.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(target.name + ATOMIC_TMP_SUFFIX)
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    fsync_directory(target.parent)
    return len(payload)
