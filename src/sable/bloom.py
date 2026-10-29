"""Bloom filters: answering "definitely absent" without touching a data block.

A read for a missing key is the worst case in an LSM tree: every level of every
file has to be asked before the answer can be ``None``. Bloom filters bound that
cost. They answer one question only:

* **No** — the key is certainly not here. Provable, and lets us skip the read.
* **Yes** — the key *might* be here. Still requires a read.

The asymmetry is the whole value. There are never false negatives, so pruning is
always safe.

Hashing strategy
----------------
Two independent FNV-1a 64-bit digests seed ``k`` probes by double hashing
(``h1 + i * h2 mod m``), per Kirsch and Mitzenmacher. This yields ``k`` probes at
the cost of two digests, and preserves the false-positive-rate analysis that
assumes independent probes.

Nothing here uses a cryptographic digest: filter bits reveal membership
information anyway, and paying for collision resistance would be pure waste in
the hottest loop of the read path.
"""

from __future__ import annotations

import math
import struct

__all__ = ["BLOOM_MAGIC", "BloomFilter", "optimal_bit_width", "optimal_probe_count"]

BLOOM_MAGIC = b"SBLM"
BLOOM_VERSION = 1

# magic | version | probe_count | pad | bit_count
_BLOB_HEADER = struct.Struct("<4sHHQ")
_BLOB_HEADER_SIZE = _BLOB_HEADER.size

_FNV_OFFSET_A = 0xCBF29CE484222325
_FNV_OFFSET_B = 0x9E3779B97F4A7C15
_FNV_PRIME = 0x100000001B3
_MASK64 = (1 << 64) - 1


def optimal_bit_width(expected_entries: int, false_positive_rate: float) -> int:
    """Bits needed to hold ``expected_entries`` at the target rate.

    Args:
        expected_entries: How many distinct keys will be inserted.
        false_positive_rate: Desired probability of a false positive.

    Returns:
        Bit count, floored at 8 so tiny filters stay structurally sane.
    """
    if expected_entries <= 0:
        return 8
    numerator = -expected_entries * math.log(false_positive_rate)
    bits = int(math.ceil(numerator / (math.log(2) ** 2)))
    return max(bits, 8)


def optimal_probe_count(bit_width: int, expected_entries: int) -> int:
    """Number of probes minimising the false-positive rate for this geometry.

    Args:
        bit_width: Total bits in the filter.
        expected_entries: Distinct keys to be inserted.

    Returns:
        Probe count, clamped to ``[1, 32]`` so pathological inputs cannot turn
        one lookup into thousands of probes.
    """
    if expected_entries <= 0 or bit_width <= 0:
        return 1
    probes = int(round((bit_width / expected_entries) * math.log(2)))
    return max(1, min(probes, 32))


def _fnv1a(data: bytes, offset_basis: int) -> int:
    digest = offset_basis
    for byte in data:
        digest ^= byte
        digest = (digest * _FNV_PRIME) & _MASK64
    return digest


class BloomFilter:
    """A fixed-size bit array with no false negatives."""

    def __init__(self, bit_width: int, probe_count: int) -> None:
        if bit_width < 8:
            raise ValueError("bit_width must be at least 8")
        self._bit_width = bit_width
        self._probe_count = optimal_probe_count(bit_width, max(bit_width // 8, 1))
        if probe_count > 0:
            self._probe_count = min(max(probe_count, 1), 32)
        self._bytes = bytearray((bit_width + 7) // 8)

    @classmethod
    def with_capacity(cls, expected_entries: int, false_positive_rate: float = 0.01) -> BloomFilter:
        """Size a filter from expectations rather than from internal geometry.

        Args:
            expected_entries: Distinct keys to be inserted.
            false_positive_rate: Target probability of a false positive.
        """
        bit_width = optimal_bit_width(expected_entries, false_positive_rate)
        return cls(bit_width, optimal_probe_count(bit_width, expected_entries))

    @property
    def bit_width(self) -> int:
        return self._bit_width

    @property
    def probe_count(self) -> int:
        return self._probe_count

    @property
    def byte_width(self) -> int:
        return len(self._bytes)

    def add(self, key: bytes) -> None:
        """Insert one key."""
        for index in self._probe_indices(key):
            self._bytes[index >> 3] |= 1 << (index & 7)

    def may_contain(self, key: bytes) -> bool:
        """True when the key *might* have been added. Never a false negative."""
        bits = self._bytes
        return all(
            bits[index >> 3] & (1 << (index & 7)) for index in self._probe_indices(key)
        )

    def fill_ratio(self) -> float:
        """Fraction of bits set; a crude but useful saturation signal."""
        set_bits = sum(bin(byte).count("1") for byte in self._bytes)
        return set_bits / self._bit_width

    def to_bytes(self) -> bytes:
        """Serialise header plus bit array. Round-trips via :meth:`from_bytes`."""
        header = _BLOB_HEADER.pack(
            BLOOM_MAGIC, BLOOM_VERSION, self._probe_count, self._bit_width
        )
        return header + bytes(self._bytes)

    @classmethod
    def from_bytes(cls, blob: bytes) -> BloomFilter:
        """Rebuild a filter, validating magic, version and geometry.

        Raises:
            ValueError: The blob is too short, has a foreign magic, or declares
                a bit width its payload cannot carry.
        """
        if len(blob) < _BLOB_HEADER_SIZE:
            raise ValueError("bloom blob is shorter than its header")
        magic, version, probe_count, bit_width = _BLOB_HEADER.unpack_from(blob, 0)
        if magic != BLOOM_MAGIC:
            raise ValueError("not a sable bloom filter blob")
        if version != BLOOM_VERSION:
            raise ValueError(f"unsupported bloom version {version}")

        payload = blob[_BLOB_HEADER_SIZE:]
        if len(payload) * 8 < bit_width:
            raise ValueError("bloom payload is too small for its declared bit width")

        instance = cls(bit_width, probe_count)
        instance._bytes = bytearray(payload)
        return instance

    def _probe_indices(self, key: bytes) -> list[int]:
        digest_a = _fnv1a(key, _FNV_OFFSET_A)
        digest_b = _fnv1a(key, _FNV_OFFSET_B) | 1  # odd, so it stays coprime with m
        width = self._bit_width
        probes = self._probe_count
        return [(digest_a + step * digest_b) % width for step in range(probes)]
