"""Filesystem scanning into the SQLite index."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from photo_dedupe.db import Database
from photo_dedupe.hashing import hash_file

DEFAULT_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".heif",
        ".webp",
        ".gif",
        ".tif",
        ".tiff",
        ".bmp",
        ".raw",
        ".cr2",
        ".nef",
        ".arw",
        ".dng",
    }
)


@dataclass
class ScanStats:
    roots: int = 0
    seen: int = 0
    upserted: int = 0
    reused_hash: int = 0
    hashed: int = 0
    unreadable: int = 0
    hash_errors: int = 0
    skipped_non_image: int = 0


@dataclass
class ScanResult:
    stats: ScanStats = field(default_factory=ScanStats)


def iter_image_files(
    root: Path,
    *,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    follow_symlinks: bool = False,
) -> list[Path]:
    root = Path(root)
    found: list[Path] = []
    if not root.exists():
        return found

    def _walk(directory: Path) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink() and not follow_symlinks:
                    continue
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    _walk(entry)
                elif entry.is_file(follow_symlinks=follow_symlinks):
                    if entry.suffix.lower() in extensions:
                        found.append(entry)
            except OSError:
                continue

    if root.is_file():
        if root.suffix.lower() in extensions:
            found.append(root)
        return found

    _walk(root)
    return found


def scan_roots(
    db: Database,
    roots: list[Path],
    *,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    follow_symlinks: bool = False,
) -> ScanResult:
    result = ScanResult()
    stats = result.stats

    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        stats.roots += 1
        source_id = db.upsert_source(root)
        db.mark_source_files_missing(source_id)

        for path in iter_image_files(
            root, extensions=extensions, follow_symlinks=follow_symlinks
        ):
            stats.seen += 1
            try:
                st = path.stat()
            except OSError:
                stats.unreadable += 1
                continue

            resolved = str(path.resolve())
            existing = db.get_file_by_path(resolved)
            keep_hash = bool(
                existing
                and existing.hash
                and existing.size == st.st_size
                and existing.mtime == st.st_mtime
            )
            if keep_hash:
                stats.reused_hash += 1

            db.upsert_file(
                source_id=source_id,
                path=resolved,
                name=path.name,
                size=st.st_size,
                mtime=st.st_mtime,
                keep_hash=keep_hash,
            )
            stats.upserted += 1

    # Size-gated hashing: only hash files whose size is shared by another present file
    sizes = db.sizes_with_multiple_present()
    to_hash = db.present_files_without_hash_for_sizes(sizes)
    for record in to_hash:
        try:
            digest = hash_file(Path(record.path))
            db.set_hash(record.id, digest)
            stats.hashed += 1
        except OSError:
            stats.hash_errors += 1

    return result
