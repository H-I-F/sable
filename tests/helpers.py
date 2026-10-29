"""Shared construction utilities for the ``sable`` test-suite.

Nothing here performs assertions; helpers only shape fixtures so individual
tests stay legible and so every test constructs data through the same
constructor paths callers use.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sable.types import InternalEntry

DEFAULT_VALUE_SIZE = 24


def key_for(index: int, *, width: int = 8) -> bytes:
    """Deterministic, lexicographically ordered key for ``index``.

    Zero-padding matters: ``b"k-10"`` sorts before ``b"k-9"`` lexicographically,
    so unpadded counters silently scramble range-scan expectations.
    """
    return b"k-" + str(index).zfill(width).encode()


def value_for(index: int, *, size: int = DEFAULT_VALUE_SIZE) -> bytes:
    """Repeating payload of a fixed length, so amortized costs stay constant."""
    token = str(index).encode()
    if size <= len(token):
        return token[:size]
    return (token * (size // len(token) + 1))[:size]


def entry(user_key: bytes, sequence: int, value: bytes | None = None) -> InternalEntry:
    """Build either a PUT or a DELETE depending on whether ``value`` is given."""
    if value is None:
        return InternalEntry.delete(user_key, sequence)
    return InternalEntry.put(user_key, sequence, value)


def sequential_puts(count: int, *, start_sequence: int = 0) -> list[InternalEntry]:
    """``count`` PUT frames with contiguous sequence numbers."""
    return [
        InternalEntry.put(key_for(i), start_sequence + i, value_for(i)) for i in range(count)
    ]


def db_path(tmp_path: Path, name: str = "data") -> Path:
    """Resolve-and-create a database directory inside the pytest scratch area."""
    path = tmp_path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def wal_path(tmp_path: Path, name: str = "000001.log") -> Path:
    """Path for a standalone WAL segment under the scratch area."""
    return tmp_path / name


def raw_image_of(frames: bytes, *, truncate_to: int | None = None) -> bytes:
    """Return ``frames`` optionally cut to ``truncate_to`` bytes.

    Args:
        frames: Concatenated encoded frames.
        truncate_to: When given, emulate a partial write that only managed this
            many bytes onto stable storage.

    Returns:
        The possibly-truncated image.
    """
    if truncate_to is None:
        return frames
    return frames[:truncate_to]


def flip_bit(data: bytes, offset: int) -> bytes:
    """Deterministically corrupt one bit, simulating a bad sector or torn page."""
    mutable = bytearray(data)
    mutable[offset] ^= 0x01
    return bytes(mutable)


def count_fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install a spy over ``os.fsync`` and return the list it appends to.

    Args:
        monkeypatch: pytest fixture used to swap the descriptor-level sync call.

    Returns:
        A live list; every fsync pushes a marker string. Tests assert on its
        length so a durability regression (dropped fsync) fails loudly instead
        of silently degrading to "works most of the time".
    """
    calls: list[str] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        try:
            name = os.fdopen(fd, "rb", closefd=False).name
        except OSError:  # pragma: no cover - platform dependent
            name = f"fd:{fd}"
        calls.append(str(name))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return calls


def platform_supports_directory_fsync() -> bool:
    """True when the OS permits opening a directory just to fsync it.

    Windows does not, which is why :func:`sable.fsutil.fsync_directory` swallows
    that failure instead of aborting the write path.
    """
    return os.name != "nt"
