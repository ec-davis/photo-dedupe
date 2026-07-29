"""Duplicate grouping and keeper selection."""

from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from photo_dedupe.db import Database, FileRecord


@dataclass(frozen=True)
class DuplicateGroup:
    key: str
    kind: str  # "hash" or "name_size"
    files: tuple[FileRecord, ...]
    keeper: FileRecord
    delete_candidates: tuple[FileRecord, ...]


def choose_keeper(files: list[FileRecord]) -> FileRecord:
    """Keep oldest mtime; tie-break on shortest path, then path string."""
    return min(files, key=lambda f: (f.mtime, len(f.path), f.path))


def find_hash_duplicates(db: Database) -> list[DuplicateGroup]:
    by_hash: dict[str, list[FileRecord]] = defaultdict(list)
    for record in db.present_files_with_hash():
        assert record.hash is not None
        by_hash[record.hash].append(record)

    groups: list[DuplicateGroup] = []
    for digest, files in sorted(by_hash.items(), key=lambda item: item[0]):
        if len(files) < 2:
            continue
        keeper = choose_keeper(files)
        deletes = tuple(f for f in files if f.id != keeper.id)
        groups.append(
            DuplicateGroup(
                key=digest,
                kind="hash",
                files=tuple(sorted(files, key=lambda f: f.path)),
                keeper=keeper,
                delete_candidates=deletes,
            )
        )
    return groups


def find_name_size_mismatches(db: Database) -> list[DuplicateGroup]:
    """Same name + size but different hashes (suspicious naming collisions)."""
    by_key: dict[tuple[str, int], list[FileRecord]] = defaultdict(list)
    for record in db.present_files():
        by_key[(record.name.lower(), record.size)].append(record)

    groups: list[DuplicateGroup] = []
    for (name, size), files in sorted(by_key.items(), key=lambda item: item[0]):
        if len(files) < 2:
            continue
        hashes = {f.hash for f in files if f.hash}
        # Only report when we have differing content hashes
        if len(hashes) < 2:
            continue
        keeper = choose_keeper(files)
        deletes = tuple(f for f in files if f.id != keeper.id)
        groups.append(
            DuplicateGroup(
                key=f"{name}|{size}",
                kind="name_size",
                files=tuple(sorted(files, key=lambda f: f.path)),
                keeper=keeper,
                delete_candidates=deletes,
            )
        )
    return groups
