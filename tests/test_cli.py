"""CLI behaviour: exit codes, encoding handling and stats reporting.

Each case invokes the same ``main`` function the console script calls, so these
tests exercise argument plumbing rather than re-testing engine behaviour that
already has dedicated coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sable import Engine, EngineOptions
from sable.cli import EXIT_MISSING_KEY, EXIT_OK, main


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "cli-data"


def run(*argv: str, capsysbinary: pytest.CaptureFixture[bytes]) -> tuple[int, bytes, bytes]:
    """Invoke the CLI and return ``(exit_code, stdout_bytes, stderr_text)``."""
    code = main(list(argv))
    captured = capsysbinary.readouterr()
    return code, captured.out, captured.err


def test_put_then_get_roundtrip(home: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    code, _, _ = run("--home", str(home), "put", "greeting", "hello", capsysbinary=capsysbinary)
    assert code == EXIT_OK

    code, out, _ = run("--home", str(home), "get", "greeting", capsysbinary=capsysbinary)
    assert code == EXIT_OK
    assert out == b"hello\n"


def test_missing_key_returns_dedicated_exit_code(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    run("--home", str(home), "put", "known", "v", capsysbinary=capsysbinary)

    code, out, err = run("--home", str(home), "get", "absent", capsysbinary=capsysbinary)

    assert code == EXIT_MISSING_KEY
    assert out == b""
    assert "key not found" in err.decode()


def test_delete_masks_flushed_value(home: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    run("--home", str(home), "put", "ephemeral", "value", capsysbinary=capsysbinary)
    engine = Engine.open(home, EngineOptions())
    engine.flush()
    engine.close()
    assert Engine.open(home, EngineOptions()).get(b"ephemeral") == b"value"

    code, _, _ = run("--home", str(home), "delete", "ephemeral", capsysbinary=capsysbinary)
    assert code == EXIT_OK

    code, _, _ = run("--home", str(home), "get", "ephemeral", capsysbinary=capsysbinary)
    assert code == EXIT_MISSING_KEY


def test_binary_payloads_via_hex(home: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    run("--home", str(home), "put", "--hex", "00ff", "cafebabe", capsysbinary=capsysbinary)

    code, out, _ = run("--home", str(home), "get", "--hex", "00ff", capsysbinary=capsysbinary)

    assert code == EXIT_OK
    assert out == bytes.fromhex("cafebabe") + b"\n"


def test_invalid_hex_is_reported_as_usage(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    code, _, err = run("--home", str(home), "put", "--hex", "zz", "v", capsysbinary=capsysbinary)

    assert code == 2
    assert "invalid argument" in err.decode()


def test_stats_reports_levels_and_amplification(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    engine = Engine.open(home, EngineOptions(block_size_bytes=64))
    for index in range(40):
        engine.put(f"k-{index:03d}".encode(), b"payload")
    engine.flush()
    engine.close()

    code, out, _ = run("--home", str(home), "stats", capsysbinary=capsysbinary)
    text = out.decode()

    assert code == EXIT_OK
    assert "L0" in text
    assert "read amplification (worst case): 1" in text


def test_stats_json_is_machine_readable(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    run("--home", str(home), "put", "k", "v", capsysbinary=capsysbinary)

    code, out, _ = run("--home", str(home), "stats", "--json", capsysbinary=capsysbinary)

    assert code == EXIT_OK
    payload = json.loads(out.decode())
    assert payload["sequence"] >= 0
    assert "counters" in payload
    assert payload["read_amplification"] == 0


def test_subcommand_is_required(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    with pytest.raises(SystemExit) as raised:
        main([])

    assert raised.value.code == 2


def test_home_defaults_to_environment_variable(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "env-home"
    monkeypatch.setenv("SABLE_HOME", str(target))

    run("put", "env-key", "env-value", capsysbinary=capsysbinary)
    code, out, _ = run("get", "env-key", capsysbinary=capsysbinary)

    assert code == EXIT_OK
    assert out == b"env-value\n"
    assert target.exists()


def test_scan_emits_ordered_key_value_pairs(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    code, _, _ = run("--home", str(home), "put", "a", "1", capsysbinary=capsysbinary)
    assert code == EXIT_OK
    run("--home", str(home), "put", "b", "2", capsysbinary=capsysbinary)
    run("--home", str(home), "put", "c", "3", capsysbinary=capsysbinary)
    run("--home", str(home), "delete", "b", capsysbinary=capsysbinary)

    code, out, _ = run("--home", str(home), "scan", capsysbinary=capsysbinary)
    assert code == EXIT_OK
    assert out == b"a\t1\nc\t3\n", "a deleted key must be absent, not emitted with a sentinel"


def test_scan_honours_window_limit_and_bound_mode(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    for index in range(5):
        run("--home", str(home), "put", f"k{index}", str(index), capsysbinary=capsysbinary)

    code, out, _ = run("--home", str(home), "scan", "k1", "k3", capsysbinary=capsysbinary)
    assert code == EXIT_OK
    assert out == b"k1\t1\nk2\t2\n"

    code, out, _ = run(
        "--home", str(home), "scan", "k1", "k3", "--end-inclusive", capsysbinary=capsysbinary
    )
    assert code == EXIT_OK
    assert out == b"k1\t1\nk2\t2\nk3\t3\n"

    code, out, _ = run("--home", str(home), "scan", "--limit", "2", capsysbinary=capsysbinary)
    assert code == EXIT_OK
    assert out == b"k0\t0\nk1\t1\n"


def test_scan_hex_mode_roundtrips_binary_payloads(
    home: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    key = bytes([0x00, 0xFF, 0x10])
    value = bytes([0xDE, 0xAD])
    run("--home", str(home), "put", "--hex", key.hex(), value.hex(), capsysbinary=capsysbinary)

    code, out, _ = run("--home", str(home), "scan", "--hex", capsysbinary=capsysbinary)
    assert code == EXIT_OK
    assert out == key.hex().encode() + b"\t" + value.hex().encode() + b"\n"
