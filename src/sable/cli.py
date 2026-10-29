"""Command-line front end for the ``sable`` engine.

The CLI is deliberately a thin adapter: every subcommand opens the store, does
one thing, and leaves. There is no resident daemon and no cached state, so a
crash between invocations is indistinguishable from a clean exit — which is
exactly the property the durability work upstream is trying to establish.

Keys and values are UTF-8 text by default. Binary payloads are handled through
``--hex`` on any argument that would otherwise be ambiguous, and output is
written to the stdout buffer as raw bytes so nothing is corrupted by a text-mode
translation layer.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from sable import Engine, EngineOptions
from sable.errors import SableError
from sable.types import EngineStats

__all__ = ["build_parser", "main"]

HOME_ENV_VAR = "SABLE_HOME"
DEFAULT_HOME_DIR = "sable-data"

EXIT_OK = 0
EXIT_MISSING_KEY = 1
EXIT_USAGE = 2
EXIT_ENGINE_ERROR = 3


def default_home() -> Path:
    """Resolve the data directory from the environment, then the cwd."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.cwd() / DEFAULT_HOME_DIR


def key_bytes(raw: str, *, hex_mode: bool) -> bytes:
    """Decode a command-line key.

    Args:
        raw: Argument as typed.
        hex_mode: Interpret as hexadecimal rather than UTF-8.

    Returns:
        The decoded key.

    Raises:
        ValueError: ``hex_mode`` was requested but the argument is not hex.
    """
    if hex_mode:
        return bytes.fromhex(raw)
    return raw.encode("utf-8")


def value_bytes(raw: str, *, hex_mode: bool) -> bytes:
    """Decode a command-line value using the same rules as :func:`key_bytes`."""
    return key_bytes(raw, hex_mode=hex_mode)


def open_engine(home: Path) -> Engine:
    """Open (creating if needed) the store at ``home``."""
    home.parent.mkdir(parents=True, exist_ok=True)
    return Engine.open(home, EngineOptions(create_if_missing=True))


def command_put(arguments: argparse.Namespace) -> int:
    engine = open_engine(arguments.home)
    try:
        engine.put(
            key_bytes(arguments.key, hex_mode=arguments.hex),
            value_bytes(arguments.value, hex_mode=arguments.hex),
        )
    finally:
        engine.close()
    _emit(b"ok\n")
    return EXIT_OK


def command_get(arguments: argparse.Namespace) -> int:
    engine = open_engine(arguments.home)
    try:
        value = engine.get(key_bytes(arguments.key, hex_mode=arguments.hex))
    finally:
        engine.close()
    if value is None:
        print(f"sable: key not found: {arguments.key}", file=sys.stderr)
        return EXIT_MISSING_KEY
    _emit(value + b"\n")
    return EXIT_OK


def command_delete(arguments: argparse.Namespace) -> int:
    engine = open_engine(arguments.home)
    try:
        engine.delete(key_bytes(arguments.key, hex_mode=arguments.hex))
    finally:
        engine.close()
    _emit(b"ok\n")
    return EXIT_OK


def command_stats(arguments: argparse.Namespace) -> int:
    """Report the shape of the tree, which is how compaction pressure is read."""
    engine = open_engine(arguments.home)
    try:
        levels = engine.level_files
        stats = engine.stats
        sequence = engine.last_sequence
    finally:
        engine.close()

    if arguments.json:
        _emit(_stats_json(levels, stats, sequence, arguments.home))
        return EXIT_OK

    total_bytes = 0
    lines = [f"home: {arguments.home}", f"sequence: {sequence}", ""]
    lines.append(f"{'level':<8}{'files':>8}{'entries':>10}{'bytes':>12}")
    for level_index, files in enumerate(levels):
        if not files:
            continue
        entries = sum(meta.num_entries for meta in files)
        level_bytes = sum(meta.file_size for meta in files)
        total_bytes += level_bytes
        lines.append(f"L{level_index:<7}{len(files):>8}{entries:>10}{level_bytes:>12}")
    lines.append("")
    lines.append(f"sstable bytes: {total_bytes}")
    # Worst-case files touched by one point lookup: every overlapping L0 file,
    # plus at most one file from each deeper level.
    lines.append(f"read amplification (worst case): {_read_amplification(levels)}")
    lines.append(
        f"counters: puts={stats.puts} deletes={stats.deletes} "
        f"gets={stats.gets} hits={stats.get_hits}"
    )
    _emit(("\n".join(lines) + "\n").encode("utf-8"))
    return EXIT_OK


def command_scan(arguments: argparse.Namespace) -> int:
    """Walk ``[start, end)`` in order and emit one ``key<TAB>value`` line each.

    Deleted keys are *absent* rather than emitted with a sentinel: the merge
    consumed their tombstone, so there is nothing left to report.
    """
    engine = open_engine(arguments.home)
    try:
        start = _optional_bytes(arguments.start, arguments.hex)
        end = _optional_bytes(arguments.end, arguments.hex)
        pairs = list(engine.scan(start, end, end_exclusive=not arguments.end_inclusive,
                                 limit=arguments.limit))
    finally:
        engine.close()
    for key, value in pairs:
        _emit(_render(key, arguments.hex) + b"\t" + _render(value, arguments.hex) + b"\n")
    return EXIT_OK


def _optional_bytes(raw: str | None, hex_mode: bool) -> bytes | None:
    """Decode an optional CLI bound, preserving "unbounded" as ``None``."""
    if raw is None:
        return None
    return key_bytes(raw, hex_mode=hex_mode)


def _render(payload: bytes, hex_mode: bool) -> bytes:
    """Render one side of a scan line hexadecimally only when it came in that way."""
    return payload.hex().encode("ascii") if hex_mode else payload


def _read_amplification(levels: Sequence[Sequence[object]]) -> int:
    if not levels:
        return 0
    return len(levels[0]) + sum(1 for level in levels[1:] if level)


def _stats_json(
    levels: Sequence[Sequence[object]], stats: EngineStats, sequence: int, home: Path
) -> bytes:
    payload = {
        "home": str(home),
        "sequence": sequence,
        "levels": [
            {"level": index, "files": len(files)} for index, files in enumerate(levels) if files
        ],
        "read_amplification": _read_amplification(levels),
        "counters": stats.as_dict(),
    }
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _emit(payload: bytes) -> None:
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument tree."""
    parser = argparse.ArgumentParser(
        prog="sable",
        description="A crash-safe pure-Python LSM-tree key-value store.",
    )
    parser.add_argument(
        "--home", type=Path, default=default_home(), help="data directory (env: SABLE_HOME)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # `--home` lives only on the root parser. Repeating it on subparsers would
    # make their defaults silently override a value the caller supplied.
    key_help = argparse.ArgumentParser(add_help=False)
    key_help.add_argument("--hex", action="store_true", help="treat KEY/VALUE as hexadecimal")

    put = subparsers.add_parser("put", parents=[key_help], help="write a key")
    put.add_argument("key")
    put.add_argument("value")
    put.set_defaults(handler=command_put)

    get = subparsers.add_parser("get", parents=[key_help], help="read a key")
    get.add_argument("key")
    get.set_defaults(handler=command_get)

    delete = subparsers.add_parser("delete", parents=[key_help], help="tombstone a key")
    delete.add_argument("key")
    delete.set_defaults(handler=command_delete)

    scan = subparsers.add_parser("scan", parents=[key_help], help="walk a key range in order")
    scan.add_argument("start", nargs="?", help="lower bound; unbounded when omitted")
    scan.add_argument("end", nargs="?", help="upper bound, excluded by default")
    scan.add_argument("--limit", type=int, default=None, help="stop after this many pairs")
    scan.add_argument(
        "--end-inclusive", action="store_true", help="include END rather than stopping before it"
    )
    scan.set_defaults(handler=command_scan)

    stats = subparsers.add_parser("stats", help="report tree shape and counters")
    stats.add_argument("--json", action="store_true", help="emit machine-readable output")
    stats.set_defaults(handler=command_stats)

    return parser


def _version() -> str:
    from sable import __version__

    return __version__


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Arguments to parse; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code. Missing keys are a distinct code from unexpected
        failures so scripts can tell "absent" apart from "broken".
    """
    parser = build_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    arguments.home = Path(arguments.home)

    handler: Callable[[argparse.Namespace], int] | None = getattr(
        arguments, "handler", None
    )
    if handler is None:  # pragma: no cover - argparse enforces a subcommand
        parser.print_help()
        return EXIT_USAGE

    try:
        exit_code: int = handler(arguments)
        return exit_code
    except ValueError as exc:
        print(f"sable: invalid argument: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SableError as exc:
        print(f"sable: {exc}", file=sys.stderr)
        return EXIT_ENGINE_ERROR


if __name__ == "__main__":
    sys.exit(main())
