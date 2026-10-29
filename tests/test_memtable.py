"""MemTable ordering, sequence monotonicity and tombstone shadowing.

Every claim here is about iteration order rather than storage details: because
entries sort by ``(user_key, -sequence)``, "the newest version wins" is a
consequence of walking the list, not a lookup special case.
"""

from __future__ import annotations

import pytest

from helpers import key_for, value_for
from sable.errors import InvalidArgumentError
from sable.memtable import MemTable
from sable.types import InternalEntry, KeyRange, OpType


def fill_table(count: int, *, start_sequence: int = 0) -> MemTable:
    table = MemTable(first_sequence=start_sequence)
    for index in range(count):
        table.put(key_for(index), start_sequence + index, value_for(index))
    return table


def test_sequence_numbers_must_strictly_increase() -> None:
    table = MemTable()
    table.put(b"alpha", 5, b"five")

    with pytest.raises(InvalidArgumentError, match="strictly increase"):
        table.put(b"beta", 5, b"five-again")
    with pytest.raises(InvalidArgumentError, match="strictly increase"):
        table.put(b"beta", 4, b"four")

    # Equal-and-below is rejected; strictly greater is accepted.
    table.put(b"beta", 6, b"six")
    assert table.last_sequence == 6
    assert len(table) == 2


def test_recovered_start_sequence_is_respected() -> None:
    table = MemTable(first_sequence=41)
    assert table.first_sequence == 41

    with pytest.raises(InvalidArgumentError):
        table.put(b"k", 40, b"v")


def test_iteration_is_sorted_by_user_key() -> None:
    table = MemTable()
    # Sequences must climb, so they are allocated independently of key order.
    for sequence, index in enumerate([7, 3, 9, 1, 4]):
        table.put(key_for(index), sequence, value_for(index))

    keys = [entry.user_key for entry in table.iter_range()]

    assert keys == sorted(keys)
    assert keys == [key_for(i) for i in (1, 3, 4, 7, 9)]


def test_versions_of_one_key_collapse_newest_first() -> None:
    table = MemTable()
    table.put(b"hot", 1, b"v1")
    table.put(b"hot", 3, b"v2")
    table.put(b"hot", 5, b"v3")

    versions = [entry.sequence for entry in table.iter_range()]

    # Negated sequences mean insertion order is already "newest first", which is
    # what makes `get` a single descent rather than a scan-and-compare.
    assert versions == [5, 3, 1]
    assert table.get(b"hot") is not None
    assert table.get(b"hot").value == b"v3"


def test_tombstone_shadows_live_value_without_removing_it() -> None:
    table = MemTable()
    table.put(b"gone", 1, b"value")
    table.delete(b"gone", 2)

    shadowed = table.get(b"gone")

    assert shadowed is not None, "readers need the tombstone to mask lower levels"
    assert shadowed.is_tombstone is True
    assert shadowed.op is OpType.DELETE
    assert shadowed.value == b""
    assert len(table) == 2, "tombstones are entries, not erasures"

    # A later write resurrects the key for readers above this table.
    table.put(b"gone", 3, b"reborn")
    assert table.get(b"gone").value == b"reborn"


def test_get_returns_none_for_unknown_key() -> None:
    table = fill_table(3)

    assert table.get(b"absent") is None
    assert MemTable().get(b"anything") is None


def test_iter_latest_keeps_only_the_newest_version_per_key() -> None:
    table = MemTable()
    table.put(b"a", 1, b"a1")
    table.put(b"b", 2, b"b1")
    table.put(b"a", 3, b"a2")
    table.delete(b"b", 4)

    latest = list(table.iter_latest())

    assert [entry.user_key for entry in latest] == [b"a", b"b"]
    assert latest[0].sequence == 3
    assert latest[1].sequence == 4 and latest[1].is_tombstone
    assert len(list(table.iter_range())) == 4, "all versions survive in storage"


def test_range_iteration_honours_window_semantics() -> None:
    table = fill_table(6)

    window = KeyRange(start=key_for(2), end=key_for(4))
    assert [entry.user_key for entry in table.iter_range(window)] == [key_for(2), key_for(3)]

    exclusive_start = KeyRange(start=key_for(2), start_inclusive=False)
    assert [entry.user_key for entry in table.iter_range(exclusive_start)] == [
        key_for(3),
        key_for(4),
        key_for(5),
    ]

    inclusive_end = KeyRange(end=key_for(1), end_exclusive=False)
    assert [entry.user_key for entry in table.iter_range(inclusive_end)] == [
        key_for(0),
        key_for(1),
    ]


def test_key_bounds_track_insertion_order() -> None:
    table = MemTable()
    table.put(b"mango", 0, b"1")
    assert table.smallest_key == table.largest_key == b"mango"

    table.put(b"apple", 1, b"2")
    table.put(b"zebra", 2, b"3")

    assert (table.smallest_key, table.largest_key) == (b"apple", b"zebra")
    assert table.key_edge_slice() == (b"apple", b"zebra")


def test_freezing_blocks_mutation_but_not_reads() -> None:
    table = fill_table(3)
    table.freeze()

    assert table.frozen is True
    with pytest.raises(InvalidArgumentError, match="frozen"):
        table.put(b"denied", 99, b"v")

    assert table.get(key_for(1)) is not None
    assert [entry.user_key for entry in table.iter_range()] == [key_for(i) for i in range(3)]


def test_accounting_reports_versions_and_payload() -> None:
    table = MemTable()
    assert table.is_empty
    assert table.approximate_bytes == 0

    with_16 = InternalEntry.put(b"k" * 8, 0, b"v" * 8)
    table.add(with_16)

    assert table.is_empty is False
    assert table.version_count == 1
    assert table.approximate_bytes >= 16


def test_single_version_table_has_equal_edges() -> None:
    table = MemTable()
    table.put(b"only", 0, b"v")

    assert table.smallest_key == table.largest_key == b"only"


def test_insertion_and_lookup_scale_past_random_level_height() -> None:
    count = 700
    table = fill_table(count)

    assert len(table) == count
    for index in range(count):
        found = table.get(key_for(index))
        assert found is not None
        assert found.value == value_for(index)
    assert [entry.user_key for entry in table.iter_latest()] == [
        key_for(i) for i in range(count)
    ]
