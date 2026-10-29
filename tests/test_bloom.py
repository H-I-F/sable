"""Bloom filter invariants.

These tests pin down the contract the read path depends on: **never** a false
negative, a bounded false positive rate, and parameters that scale sanely.
"""

from __future__ import annotations

import pytest

from sable.bloom import (
    BLOOM_MAGIC,
    BloomFilter,
    optimal_bit_width,
    optimal_probe_count,
)


def build(entries: int, rate: float, count: int) -> BloomFilter:
    filter = BloomFilter.with_capacity(entries, rate)
    for index in range(count):
        filter.add(f"member-{index:05d}".encode())
    return filter


def test_never_reports_false_negatives() -> None:
    members = 400
    filter = build(members, 0.01, members)

    for index in range(members):
        assert filter.may_contain(f"member-{index:05d}".encode())


def test_false_positive_rate_stays_near_target() -> None:
    members = 300
    filter = build(members, 0.05, members)
    strangers = [f"stranger-{index:05d}".encode() for index in range(4000)]

    observed = sum(filter.may_contain(key) for key in strangers) / len(strangers)

    # Double hashing costs a little independence, so allow headroom above the
    # 5% target without accepting a wildly mis-sized filter.
    assert observed < 0.12, f"false positive rate drifted to {observed:.1%}"


def test_serialization_roundtrip_preserves_answers() -> None:
    members = 150
    filter = build(members, 0.01, members)

    restored = BloomFilter.from_bytes(filter.to_bytes())

    assert restored.bit_width == filter.bit_width
    assert restored.probe_count == filter.probe_count
    assert restored.byte_width == filter.byte_width
    for index in range(members):
        assert restored.may_contain(f"member-{index:05d}".encode())
    # Bit-for-bit equality of the payload is what makes approaches comparable
    # across processes rather than merely across objects.
    assert restored.to_bytes() == filter.to_bytes()


def test_from_bytes_rejects_foreign_payload() -> None:
    blob = bytearray(build(10, 0.01, 10).to_bytes())
    blob[0:4] = b"XXXX"

    with pytest.raises(ValueError, match="not a sable bloom filter"):
        BloomFilter.from_bytes(bytes(blob))


def test_from_bytes_rejects_future_version() -> None:
    blob = bytearray(build(10, 0.01, 10).to_bytes())
    blob[4:6] = (99).to_bytes(2, "little")

    with pytest.raises(ValueError, match="unsupported bloom version"):
        BloomFilter.from_bytes(bytes(blob))


def test_from_bytes_rejects_short_blob() -> None:
    with pytest.raises(ValueError, match="shorter than its header"):
        BloomFilter.from_bytes(b"abc")


def test_from_bytes_rejects_declared_width_beyond_payload() -> None:
    blob = bytearray(build(10, 0.01, 10).to_bytes())
    blob[8:16] = (1 << 20).to_bytes(8, "little")

    with pytest.raises(ValueError, match="too small for its declared"):
        BloomFilter.from_bytes(bytes(blob))


def test_optimal_sizing_scales_with_entry_count() -> None:
    narrow = optimal_bit_width(100, 0.01)
    wide = optimal_bit_width(10_000, 0.01)

    assert wide > narrow
    # Roughly 9.6 bits per key at a 1% target, the classic result.
    assert 8 < wide / 10_000 < 12
    assert optimal_probe_count(wide, 10_000) == 7


def test_probe_count_is_clamped_for_extreme_geometry() -> None:
    assert optimal_probe_count(64, 0) == 1
    assert optimal_probe_count(1 << 20, 1) <= 32


def test_fill_ratio_rises_toward_saturation() -> None:
    filter = BloomFilter.with_capacity(64, 0.01)
    assert filter.fill_ratio() == 0.0

    for index in range(64):
        filter.add(f"k{index}".encode())

    assert filter.fill_ratio() > 0.0
    assert filter.fill_ratio() <= 1.0


def test_bit_width_floor_is_enforced() -> None:
    assert optimal_bit_width(0, 0.01) == 8
    with pytest.raises(ValueError, match="at least 8"):
        BloomFilter(4, 3)


def test_header_layout_is_stable() -> None:
    filter = build(50, 0.01, 50)
    blob = filter.to_bytes()

    assert blob[:4] == BLOOM_MAGIC
    assert len(blob) == 16 + filter.byte_width


def test_adding_duplicate_keys_is_idempotent() -> None:
    filter = BloomFilter.with_capacity(20, 0.01)
    key = b"repeated"

    for _ in range(10):
        filter.add(key)

    before = filter.to_bytes()
    filter.add(key)
    assert filter.to_bytes() == before
    assert filter.may_contain(key)
