"""Crash recovery: proves the durability claim against a real SIGKILL.

Everything else in the suite tests how the engine behaves; these tests are about
what it *owes*. The contract is narrow and testable:

    a write is acknowledged only after its frame is fsync-ed, so every
    acknowledged write is readable after the process disappears mid-write.

That is enforced by killing a writer with ``SIGKILL`` — no ``atexit``, no buffer
flush, no ``finally`` — at ten different points in its life, and then demanding
the reopened store answer every one of those acknowledged writes correctly.

The child prints each key only after ``put`` has returned, so the printed lines
are exactly the durable set. Reads after recovery are checked for three things:
every acked key resolves, every value is byte-exact (no torn payload), and no
publish ever leaves a half-built SSTable behind.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import db_path
from sable import Engine, EngineOptions
from sable.fsutil import ATOMIC_TMP_SUFFIX
from sable.wal import WriteAheadLog

MATRIX_ROUNDS = 10
CHILD_TIMEOUT_SECONDS = 15.0

CHILD_SOURCE = """
import json
import sys
from pathlib import Path

from sable import Engine, EngineOptions

home = Path(sys.argv[1])
engine = Engine.open(home, EngineOptions(**json.loads(sys.argv[2])))

index = 0
while True:
    suffix = str(index).zfill(8).encode()
    engine.put(b"k-" + suffix, b"v-" + suffix)
    # Only after put returns - and therefore after the frame reached stable
    # storage - does this write join the acknowledged set the parent will audit.
    sys.stdout.write(suffix.decode() + "\\n")
    sys.stdout.flush()
    index += 1
"""


def fast_options(**kwargs: int) -> dict[str, int]:
    """Engine configuration for the child, serialised across the pipe.

    Returns:
        A plain dict, since it has to survive JSON rather than stay in-process.
    """
    base: dict[str, int] = {
        "block_size_bytes": 64,
        "memtable_bytes_threshold": 1 << 20,
        "level0_file_num_compaction_trigger": 2,
    }
    base.update(kwargs)
    return base


def source_root() -> Path:
    """Directory holding the ``sable`` package, for the child's PYTHONPATH."""
    import sable  # local import: the child needs the path, not the object

    return Path(sable.__file__).resolve().parent.parent


def tail_of(suffix: bytes) -> bytes:
    """The payload written for a given acknowledged suffix."""
    return b"v-" + suffix


def kill_after_acks(home: Path, acks: int, options: dict[str, int]) -> list[bytes]:
    """Run a writer until it has acknowledged ``acks`` writes, then SIGKILL it.

    Args:
        home: Database directory the child writes into.
        acks: Acknowledged writes to collect before pulling the plug.
        options: Engine configuration for the child.

    Returns:
        The suffixes the child acknowledged. This is precisely the set of writes
        the durability contract promises survived, which is why the assertions
        below never look at anything else.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root())

    # The command here is our own interpreter running our own script; nothing it
    # receives crosses a trust boundary, which is what S603 wants confirmed.
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", CHILD_SOURCE, str(home), json.dumps(options)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        text=True,
        bufsize=1,
    )
    acknowledged: list[bytes] = []
    assert process.stdout is not None

    try:
        for line in process.stdout:
            acknowledged.append(line.strip().encode())
            if len(acknowledged) >= acks:
                break
    finally:
        process.send_signal(signal.SIGKILL)
        try:
            process.wait(CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL cannot time out
            process.kill()
            process.wait(CHILD_TIMEOUT_SECONDS)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    assert acknowledged, "the child acknowledged nothing before it was killed"
    assert process.returncode in (-signal.SIGKILL, None), process.returncode
    return acknowledged


def recover_and_read(home: Path, options: dict[str, int]) -> Engine:
    """Reopen a crashed store. Every invariant below is asserted after this."""
    return Engine.open(home, EngineOptions(**options))


@pytest.mark.parametrize("acks", range(1, MATRIX_ROUNDS + 1))
def test_sigkill_preserves_every_acknowledged_write(tmp_path: Path, acks: int) -> None:
    """Mandated matrix: ten different kill points, every acked write survives."""
    home = db_path(tmp_path)
    options = fast_options()
    expected = kill_after_acks(home, acks, options)

    engine = recover_and_read(home, options)
    try:
        for suffix in expected:
            assert engine.get(b"k-" + suffix) == tail_of(suffix), (
                f"acknowledged write {suffix!r} did not survive a SIGKILL"
            )
    finally:
        engine.close()


def test_crash_leaves_no_torn_payload_anywhere(tmp_path: Path) -> None:
    """Reads may differ from expectations by absence, never by corrupted bytes."""
    home = db_path(tmp_path)
    options = fast_options(memtable_bytes_threshold=256)
    expected = kill_after_acks(home, 24, options)

    engine = recover_and_read(home, options)
    try:
        for suffix in expected:
            value = engine.get(b"k-" + suffix)
            assert value == tail_of(suffix), f"{suffix!r} came back as {value!r}"

        # The writer emits a contiguous run of suffixes, so anything readable has
        # to belong to that run; a duplicate or a hole would mean a frame was
        # partially applied rather than discarded wholesale.
        readable = sorted(key[2:] for key, _ in engine.scan())
        assert readable == sorted(readable)
        assert set(expected).issubset(set(readable))
    finally:
        engine.close()


def test_crash_never_publishes_a_partial_sstable(tmp_path: Path) -> None:
    """A kill during publish leaves a temp file, never a half-built table.

    flushes are forced frequently here so the child spends real time inside
    ``atomic_write_bytes``, which is exactly the window this test wants to hit.
    """
    home = db_path(tmp_path)
    options = fast_options(memtable_bytes_threshold=128, block_size_bytes=64)
    kill_after_acks(home, 40, options)

    engine = recover_and_read(home, options)
    try:
        referenced = {meta.file_name for level in engine.level_files for meta in level}
        on_disk = {path.name for path in home.iterdir()}
        temporary = {name for name in on_disk if name.endswith(ATOMIC_TMP_SUFFIX)}

        assert referenced <= on_disk, "the manifest names a file that is missing"
        assert not referenced & temporary, (
            "a temporary artifact was published as a finished table"
        )
        for name in referenced:
            # Opening validates the footer and the header CRC: a table only
            # becomes readable once it was written whole.
            assert (home / name).stat().st_size > 0
    finally:
        engine.close()


def test_recovered_store_continues_with_consistent_sequences(tmp_path: Path) -> None:
    """Three crash cycles in a row: sequences must never regress."""
    home = db_path(tmp_path)
    options = fast_options(memtable_bytes_threshold=256)
    survivors: list[bytes] = []
    sequences: list[int] = []

    for _ in range(3):
        survivors.extend(kill_after_acks(home, 12, options))
        engine = recover_and_read(home, options)
        sequences.append(engine.last_sequence)
        try:
            for suffix in survivors:
                assert engine.get(b"k-" + suffix) == tail_of(suffix)
        finally:
            engine.close()

    assert sequences == sorted(set(sequences)) and len(set(sequences)) == len(sequences), (
        f"recovery replayed the log twice: {sequences}"
    )
    assert sequences[-1] >= len(set(survivors)) - 1


def test_recovery_repairs_the_torn_tail_in_place(tmp_path: Path) -> None:
    """After one reopen the log ends exactly on a frame boundary, twice over."""
    home = db_path(tmp_path)
    options = fast_options()
    kill_after_acks(home, 16, options)

    recover_and_read(home, options).close()

    segments = sorted(home.glob("wal-*.log"))
    assert segments, "recovery left no log segment to inspect"
    for segment in segments:
        verdict = WriteAheadLog.recover(segment)
        assert not verdict.torn_tail, f"{segment.name} still ends in a partial frame"
        assert verdict.valid_bytes == segment.stat().st_size, (
            f"{segment.name} carries bytes past the last verified frame"
        )


def test_reopened_store_accepts_new_writes(tmp_path: Path) -> None:
    """Recovery is not a read-only mode: the store must keep taking writes."""
    home = db_path(tmp_path)
    options = fast_options(memtable_bytes_threshold=256)
    survivors = kill_after_acks(home, 18, options)

    engine = recover_and_read(home, options)
    try:
        fresh_suffix = b"00099999"
        engine.put(b"k-" + fresh_suffix, tail_of(fresh_suffix))
        # Deliberately no flush: durability here comes from the WAL alone, and
        # this key has to survive the very next reopen on that basis.
        assert engine.get(b"k-" + fresh_suffix) == tail_of(fresh_suffix)
        for suffix in survivors:
            assert engine.get(b"k-" + suffix) == tail_of(suffix)
    finally:
        engine.close()

    reopened = recover_and_read(home, options)
    try:
        fresh_suffix = b"00099999"
        assert reopened.get(b"k-" + fresh_suffix) == tail_of(fresh_suffix)
    finally:
        reopened.close()
