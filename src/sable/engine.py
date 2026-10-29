"""The engine: version bookkeeping and the read path.

Reads descend freshest-first and stop at the first version found, regardless of
whether that version is live or a tombstone:

    active memtable -> immutable memtables -> L0 files -> L1 .. Ln files

Stopping *at* a tombstone rather than skipping past it is the whole point: the
tombstone is what masks elder versions sitting in lower levels, so returning it
to the caller is not an error condition, it is the hiding mechanism.

Version state
-------------
Every mutation to the file set is published through ``MANIFEST-*`` files whose
path is recorded in ``CURRENT``. Rather than appending deltas forever, each
version change rewrites the manifest as a complete image and swaps it in
atomically via :func:`sable.fsutil.atomic_write_bytes`. The cost is O(files) per
change, dwarfed by the compaction that triggered it, and the benefit is that a
recovery needs no replay semantics: the last visible manifest is the truth, not
a log whose meaning depends on every frame preceding it.
"""

from __future__ import annotations

import json
import struct
import threading
import zlib
from pathlib import Path

from sable.errors import (
    ClosedEngineError,
    CorruptManifestError,
    InvalidArgumentError,
    SableError,
)
from sable.fsutil import atomic_write_bytes, fsync_directory
from sable.memtable import MemTable
from sable.sstable import SSTableReader, SSTableWriter, looks_like_temporary
from sable.types import (
    MAX_KEY_BYTES,
    EngineOptions,
    EngineStats,
    InternalEntry,
    ReadOptions,
    SSTableMeta,
)

__all__ = ["Engine", "HOME_FILE_LAYOUT"]

MANIFEST_MAGIC = b"SMAN"
MANIFEST_VERSION = 1
_MANIFEST_HEADER = struct.Struct("<4sI")

CURRENT_FILE = "CURRENT"
MANIFEST_PREFIX = "MANIFEST-"
_EMPTY_SEQUENCE = -1

HOME_FILE_LAYOUT = (CURRENT_FILE, MANIFEST_PREFIX)


class Engine:
    """A single-node LSM-tree key-value store rooted at one directory."""

    def __init__(self, home: Path, options: EngineOptions) -> None:
        self._home = Path(home)
        self._options = options
        self._lock = threading.RLock()
        self._readers: dict[int, SSTableReader] = {}
        self._active = MemTable()
        self._immutable: list[MemTable] = []
        self._levels: list[list[SSTableMeta]] = [[] for _ in range(options.max_levels)]
        self._next_file_id = 1
        self._last_sequence = _EMPTY_SEQUENCE
        self._stats = EngineStats()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, home: Path, options: EngineOptions | None = None) -> Engine:
        """Create or recover a database directory.

        Args:
            home: Directory holding ``CURRENT``, the manifest, WAL segments and
                SSTable files.
            options: Engine configuration; defaults are used when omitted.

        Returns:
            An open engine that the caller must later :meth:`close`.

        Raises:
            InvalidArgumentError: The directory is missing when creation was not
                requested, or already holds a database when that was forbidden.
            CorruptManifestError: The visible manifest is unreadable or names
                files that no longer exist.
        """
        home = Path(home)
        engine = cls(home, options or EngineOptions())
        engine._bootstrap()
        return engine

    def close(self) -> None:
        """Release every descriptor. Idempotent, and therefore safe on exit."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for reader in self._readers.values():
                reader.close()
            self._readers.clear()

    @property
    def home(self) -> Path:
        return self._home

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def options(self) -> EngineOptions:
        return self._options

    @property
    def stats(self) -> EngineStats:
        return self._stats.copy()

    @property
    def level_files(self) -> list[list[SSTableMeta]]:
        """Per-level table metadata, newest first inside each level."""
        return [list(level) for level in self._levels]

    # -- reads -------------------------------------------------------------

    def get(self, user_key: bytes, read_options: ReadOptions | None = None) -> bytes | None:
        """Resolve the visible value of ``user_key``.

        Args:
            user_key: Exact key to resolve.
            read_options: Verification strictness; ignored apart from being
                forwarded to SSTable readers.

        Returns:
            The live value, or ``None`` if the key is absent or deleted.
        """
        self._require_live(user_key)
        del read_options

        entry = self._lookup_entry(user_key)
        with self._lock:
            self._stats.gets += 1
        if entry is None or entry.is_tombstone:
            return None
        with self._lock:
            self._stats.get_hits += 1
        return entry.value

    def _lookup_entry(self, user_key: bytes) -> InternalEntry | None:
        """Return the first version encountered, tombstones included."""
        with self._lock:
            found = self._active.get(user_key)
            if found is not None:
                return found

            for table in self._immutable:
                found = table.get(user_key)
                if found is not None:
                    return found

            for meta in self._levels[0]:
                if not meta.covers(user_key):
                    continue
                hit = self._reader_for(meta).get_entry(user_key)
                if hit is not None:
                    return hit

            for level_index in range(1, len(self._levels)):
                for meta in self._levels[level_index]:
                    if not meta.covers(user_key):
                        continue
                    hit = self._reader_for(meta).get_entry(user_key)
                    if hit is not None:
                        return hit
        return None

    # -- writes (memtable-resident until PR 06 adds the WAL) ---------------

    def put(self, user_key: bytes, value: bytes) -> None:
        """Stage a live value in the active memtable.

        Args:
            user_key: Key to write.
            value: Payload; may be empty, which is distinct from absence.
        """
        self._require_live(user_key)
        if not isinstance(value, (bytes, bytearray)):
            raise InvalidArgumentError("value must be bytes")
        with self._lock:
            self._active.put(user_key, self._allocate_sequence(), bytes(value))
            self._stats.puts += 1

    def delete(self, user_key: bytes) -> None:
        """Stage a tombstone in the active memtable."""
        self._require_live(user_key)
        with self._lock:
            self._active.delete(user_key, self._allocate_sequence())
            self._stats.deletes += 1

    def flush(self) -> None:
        """Freeze the active memtable and publish it as a new L0 table.

        Raises:
            SableError: Nothing has been written since the last flush.
        """
        with self._lock:
            self._require_open()
            frozen = self._active
            if frozen.is_empty:
                raise SableError("nothing to flush")

            frozen.freeze()
            self._immutable.insert(0, frozen)
            self._active = MemTable()
            self._seal_one_immutable()

    # -- internals ---------------------------------------------------------

    def _allocate_sequence(self) -> int:
        self._last_sequence += 1
        return self._last_sequence

    def _require_live(self, user_key: bytes) -> None:
        self._require_open()
        if not isinstance(user_key, (bytes, bytearray)):
            raise InvalidArgumentError("key must be bytes")
        if len(user_key) > MAX_KEY_BYTES:
            raise InvalidArgumentError("key exceeds the supported maximum")

    def _require_open(self) -> None:
        if self._closed:
            raise ClosedEngineError("engine is closed")

    def _seal_one_immutable(self) -> None:
        """Publish the newest immutable table as one L0 file."""
        table = self._immutable.pop(0)
        meta = self._write_table(table, level=0)
        self._levels[0].insert(0, meta)
        self._publish_manifest()

    def _write_table(self, table: MemTable, *, level: int) -> SSTableMeta:
        file_id = self._next_file_id
        self._next_file_id += 1
        target = self._home / f"{file_id:06d}.sst"
        writer = SSTableWriter(target, self._options, file_id=file_id, level=level)
        for entry in table.iter_latest():
            writer.add_entry(entry)
        return writer.finish()

    def _reader_for(self, meta: SSTableMeta) -> SSTableReader:
        """Return the cached reader for ``meta``, opening it on first use."""
        reader = self._readers.get(meta.file_id)
        if reader is None:
            path = self._home / meta.file_name
            if not path.exists():
                raise CorruptManifestError(
                    f"manifest references {meta.file_name} which is missing from disk"
                )
            reader = SSTableReader.open(path)
            self._readers[meta.file_id] = reader
        return reader

    # -- version bookkeeping ----------------------------------------------

    def _bootstrap(self) -> None:
        exists = self._home.exists() and any(self._home.iterdir()) if self._home.exists() else False
        if not self._home.exists():
            if not self._options.create_if_missing:
                raise InvalidArgumentError(
                    f"{self._home} does not exist and creation was not requested"
                )
            self._home.mkdir(parents=True, exist_ok=True)
        elif exists and self._options.error_if_exists:
            raise InvalidArgumentError(f"{self._home} already holds a database")

        manifest = self._read_current()
        if manifest is not None and manifest.exists():
            self._load_manifest(manifest)

    def _manifest_path(self) -> Path:
        manifest = self._read_current()
        if manifest is None:
            manifest = self._home / f"{MANIFEST_PREFIX}000001"
        return manifest

    def _read_current(self) -> Path | None:
        current = self._home / CURRENT_FILE
        if not current.exists():
            return None
        name = current.read_text(encoding="utf-8").strip()
        return self._home / name if name else None

    def _publish_manifest(self) -> None:
        """Write a complete version image and point CURRENT at it atomically."""
        files = [
            {
                "id": meta.file_id,
                "level": meta.level,
                "size": meta.file_size,
                "entries": meta.num_entries,
                "blocks": meta.num_blocks,
                "min_key": meta.smallest_key.hex(),
                "max_key": meta.largest_key.hex(),
                "min_seq": meta.min_sequence,
                "max_seq": meta.max_sequence,
            }
            for level in self._levels
            for meta in level
        ]
        document = {
            "version": MANIFEST_VERSION,
            "next_file_id": self._next_file_id,
            "last_sequence": self._last_sequence,
            "files": files,
        }
        blob = json.dumps(document, separators=(",", ":")).encode()
        payload = _MANIFEST_HEADER.pack(MANIFEST_MAGIC, _crc32(blob)) + blob

        generation = self._home / f"{MANIFEST_PREFIX}{(self._next_file_id):06d}"
        atomic_write_bytes(generation, payload)
        atomic_write_bytes(self._home / CURRENT_FILE, generation.name.encode())
        self._expire_old_manifests(keep=generation)

    def _expire_old_manifests(self, keep: Path) -> None:
        """Delete superseded manifests now that ``keep`` is safely linked.

        Args:
            keep: The generation CURRENT points at; never removed.
        """
        for candidate in sorted(self._home.glob(f"{MANIFEST_PREFIX}*")):
            if candidate.name == keep.name or looks_like_temporary(candidate):
                continue
            candidate.unlink()
        self._seal_home_directory()

    def _load_manifest(self, manifest: Path) -> None:
        raw = manifest.read_bytes()
        if len(raw) < _MANIFEST_HEADER.size:
            raise CorruptManifestError(f"{manifest.name} is too short to hold a header")
        magic, stored_crc = _MANIFEST_HEADER.unpack_from(raw, 0)
        blob = raw[_MANIFEST_HEADER.size :]
        if magic != MANIFEST_MAGIC or stored_crc != _crc32(blob):
            raise CorruptManifestError(f"{manifest.name} failed CRC32 verification")

        try:
            document = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise CorruptManifestError(f"{manifest.name} is not valid JSON: {exc}") from exc
        if document.get("version") != MANIFEST_VERSION:
            raise CorruptManifestError(f"{manifest.name} uses an unsupported version")

        for record in document["files"]:
            meta = SSTableMeta(
                file_id=record["id"],
                level=record["level"],
                file_size=record["size"],
                num_entries=record["entries"],
                num_blocks=record["blocks"],
                smallest_key=bytes.fromhex(record["min_key"]),
                largest_key=bytes.fromhex(record["max_key"]),
                min_sequence=record["min_seq"],
                max_sequence=record["max_seq"],
            )
            self._reader_for(meta)
            self._levels[record["level"]].append(meta)

        for level in self._levels:
            level.sort(key=lambda meta: meta.file_id, reverse=True)

        known_ids = [meta.file_id for level in self._levels for meta in level]
        known_sequences = [meta.max_sequence for level in self._levels for meta in level]
        self._next_file_id = max(document["next_file_id"], max(known_ids, default=0) + 1)
        self._last_sequence = max(
            document["last_sequence"], max(known_sequences, default=_EMPTY_SEQUENCE)
        )

    def _seal_home_directory(self) -> None:
        fsync_directory(self._home)


def _crc32(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF
