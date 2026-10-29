"""Crash-recovery demonstration driven by a real SIGKILL.

    python examples/crash_demo.py [acknowledged-writes]

A child process writes keys and prints each one only after the engine has made it
durable. This parent pulls the plug with ``SIGKILL`` — no cleanup handlers, no
buffer flush — then reopens the very same directory and reports what came back.

The advertised line is

    recovered: <n>

where ``n`` counts how many acknowledged writes survived. It has to equal the
number collected before the kill; anything less means the acknowledgements lied.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import dataclasses  # noqa: E402 - imported after the sys.path bootstrap below

from sable import Engine, EngineOptions  # noqa: E402

DEFAULT_ACKS = 32
KILL_TIMEOUT_SECONDS = 15.0

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
    # Printed only after put returned, which means the frame was fsync-ed: this
    # line is the durability contract being made, not a guess about it.
    sys.stdout.write(suffix.decode() + "\\n")
    sys.stdout.flush()
    index += 1
"""

OPTIONS = EngineOptions(
    block_size_bytes=64,
    memtable_bytes_threshold=256,
    level0_file_num_compaction_trigger=2,
    l1_bytes_trigger=64,
    max_levels=4,
)


def kill_writer(home: Path, acks: int) -> list[bytes]:
    """Run a durable writer, then kill it after ``acks`` acknowledgements.

    Args:
        home: Directory the child writes into.
        acks: Number of acknowledged writes to collect first.

    Returns:
        The acknowledged suffixes, i.e. the exact set the store owes us.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC)

    process = subprocess.Popen(  # noqa: S603 - our interpreter, our script
        [sys.executable, "-c", CHILD_SOURCE, str(home), json.dumps(dataclasses.asdict(OPTIONS))],
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
        process.wait(KILL_TIMEOUT_SECONDS)
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    return acknowledged


def main(argv: list[str]) -> int:
    """Run the demonstration and return the process exit code.

    Args:
        argv: Process arguments; ``argv[1]`` optionally overrides how many
            acknowledged writes are collected before the kill.

    Returns:
        Zero when every acknowledged write survived, one otherwise.
    """
    acks = int(argv[1]) if len(argv) > 1 else DEFAULT_ACKS
    home = Path(tempfile.mkdtemp(prefix="sable-crash-demo-")) / "data"

    print(f"home: {home}")
    print(f"killing the writer after {acks} acknowledged writes")

    expected = kill_writer(home, acks)
    print(f"acknowledged: {len(expected)}")

    engine = Engine.open(home, OPTIONS)
    try:
        recovered = [suffix for suffix in expected if engine.get(b"k-" + suffix) is not None]
        corrupted = [
            suffix
            for suffix in recovered
            if engine.get(b"k-" + suffix) != b"v-" + suffix
        ]
        print(f"recovered: {len(recovered)}")
        print(f"last sequence after recovery: {engine.last_sequence}")
        print(f"corrupted payloads: {len(corrupted)}")
        print(f"files per level: {[len(level) for level in engine.level_files]}")

        verdict = len(recovered) == len(expected) and not corrupted
        print("verdict: PASS - every acknowledged write survived" if verdict else "verdict: FAIL")
    finally:
        engine.close()
        shutil.rmtree(home.parent, ignore_errors=True)

    return 0 if len(recovered) == len(expected) and not corrupted else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
