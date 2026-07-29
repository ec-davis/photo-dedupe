"""Move indexed photos into a dated folder layout."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from photo_dedupe.db import Database, FileRecord
from photo_dedupe.dedupe import path_is_under_roots
from photo_dedupe.scanner import path_is_ignored

# EXIF DateTimeOriginal
_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME = 306
_EXIF_DATE_RE = re.compile(
    r"^(?P<year>\d{4}):(?P<month>\d{2}):(?P<day>\d{2}) "
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})$"
)


@dataclass(frozen=True)
class OrganizePlanItem:
    record: FileRecord
    source: Path
    dest: Path
    taken_at: datetime
    date_source: str  # "exif" | "mtime"
    skipped: bool = False
    skip_reason: str | None = None


@dataclass
class OrganizePlan:
    items: list[OrganizePlanItem]
    dest_root: Path

    @property
    def to_move(self) -> list[OrganizePlanItem]:
        return [i for i in self.items if not i.skipped]

    @property
    def skipped(self) -> list[OrganizePlanItem]:
        return [i for i in self.items if i.skipped]


def exif_datetime(path: Path) -> datetime | None:
    """Best-effort DateTimeOriginal / DateTime from EXIF via Pillow."""
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            raw = exif.get(_EXIF_DATETIME_ORIGINAL) or exif.get(_EXIF_DATETIME)
            if not raw:
                # Some files store DateTimeOriginal in an IFD
                try:
                    from PIL.ExifTags import IFD

                    ifd = exif.get_ifd(IFD.Exif)
                    raw = ifd.get(_EXIF_DATETIME_ORIGINAL) or ifd.get(_EXIF_DATETIME)
                except Exception:
                    raw = None
            if not isinstance(raw, str):
                return None
            match = _EXIF_DATE_RE.match(raw.strip())
            if not match:
                return None
            return datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
                int(match.group("second")),
            )
    except Exception:
        return None


def resolve_taken_at(record: FileRecord, path: Path) -> tuple[datetime, str]:
    exif_dt = exif_datetime(path)
    if exif_dt is not None:
        return exif_dt, "exif"
    return datetime.fromtimestamp(record.mtime), "mtime"


def dated_subdir(taken_at: datetime) -> Path:
    """Return relative YYYY/YYYY-MM path."""
    return Path(f"{taken_at.year:04d}") / f"{taken_at.year:04d}-{taken_at.month:02d}"


def unique_dest(dest: Path) -> Path:
    """If dest exists, append _1, _2, ... before the suffix."""
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def allocate_dest(dest: Path, claimed: set[Path]) -> Path:
    """Pick a non-colliding destination path, considering disk and this plan."""
    candidate = dest
    stem = dest.stem
    suffix = dest.suffix
    parent = dest.parent
    n = 1
    while candidate.exists() or candidate in claimed:
        candidate = parent / f"{stem}_{n}{suffix}"
        n += 1
    claimed.add(candidate)
    return candidate


def build_organize_plan(
    db: Database,
    dest_root: Path,
    *,
    under: list[Path] | None = None,
) -> OrganizePlan:
    dest_root = dest_root.resolve()
    under = list(under or [])
    items: list[OrganizePlanItem] = []
    claimed: set[Path] = set()

    for record in db.present_files():
        source = Path(record.path)
        if path_is_ignored(source):
            continue
        if under and not path_is_under_roots(record.path, under):
            continue
        if not source.is_file():
            items.append(
                OrganizePlanItem(
                    record=record,
                    source=source,
                    dest=source,
                    taken_at=datetime.fromtimestamp(record.mtime),
                    date_source="mtime",
                    skipped=True,
                    skip_reason="missing on disk",
                )
            )
            continue

        try:
            resolved_source = source.resolve()
            if resolved_source == dest_root or dest_root in resolved_source.parents:
                items.append(
                    OrganizePlanItem(
                        record=record,
                        source=source,
                        dest=source,
                        taken_at=datetime.fromtimestamp(record.mtime),
                        date_source="mtime",
                        skipped=True,
                        skip_reason="already under destination",
                    )
                )
                continue
        except OSError:
            pass

        taken_at, date_source = resolve_taken_at(record, source)
        natural = dest_root / dated_subdir(taken_at) / source.name
        dest = allocate_dest(natural, claimed)
        items.append(
            OrganizePlanItem(
                record=record,
                source=source,
                dest=dest,
                taken_at=taken_at,
                date_source=date_source,
            )
        )

    return OrganizePlan(items=items, dest_root=dest_root)


def apply_organize_plan(
    db: Database,
    plan: OrganizePlan,
) -> tuple[list[tuple[Path, Path]], list[str]]:
    """Move files and update index paths. Returns (moved pairs, errors)."""
    moved: list[tuple[Path, Path]] = []
    errors: list[str] = []

    for item in plan.to_move:
        source = item.source
        try:
            # Re-check collision at apply time in case disk changed since plan.
            dest = unique_dest(item.dest) if item.dest.exists() else item.dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            new_path = str(dest.resolve())
            db.update_file_path(item.record.path, new_path, dest.name)
            moved.append((source, dest))
        except OSError as exc:
            errors.append(f"{source}: {exc}")

    return moved, errors


def _ancestor_dirs(path: Path, *, stop_at: list[Path] | None = None) -> list[Path]:
    """Parents of path up to (and including) a stop root, excluding filesystem root.

    When stop_at is set, only ancestors at or under those roots are returned.
    """
    stops: list[Path] = []
    for root in stop_at or []:
        try:
            stops.append(root.resolve())
        except OSError:
            stops.append(root)

    try:
        current = path.resolve().parent
    except OSError:
        current = path.parent

    ancestors: list[Path] = []
    while current.parent != current:
        if stops:
            under_stop = False
            for stop in stops:
                if current == stop:
                    ancestors.append(current)
                    return ancestors
                try:
                    current.relative_to(stop)
                    under_stop = True
                    break
                except ValueError:
                    continue
            if not under_stop:
                return ancestors
        ancestors.append(current)
        current = current.parent
    return ancestors


def _dir_has_no_remaining_files(
    directory: Path,
    *,
    ignoring_files: set[Path] | None = None,
) -> bool:
    """True if directory exists and has no files except those in ignoring_files."""
    ignoring = ignoring_files or set()
    if not directory.is_dir():
        return False
    try:
        for path in directory.rglob("*"):
            try:
                if not path.is_file():
                    continue
                if path.resolve() in ignoring:
                    continue
                return False
            except OSError:
                continue
        return True
    except OSError:
        return False


def find_emptied_directories(
    source_paths: list[Path],
    *,
    under: list[Path] | None = None,
    ignore_files_still_present: bool = True,
) -> list[Path]:
    """Directories that are / would be empty of files after removing source_paths.

    When ignore_files_still_present is True (dry-run), files in source_paths are
    treated as already removed. When False (after apply), checks on-disk emptiness.

    Only reports directories under ``under`` when that list is provided.
    """
    if not source_paths:
        return []

    ignoring: set[Path] = set()
    if ignore_files_still_present:
        for src in source_paths:
            try:
                ignoring.add(src.resolve())
            except OSError:
                ignoring.add(src)

    candidates: set[Path] = set()
    for src in source_paths:
        for ancestor in _ancestor_dirs(src, stop_at=under):
            candidates.add(ancestor)

    emptied = [
        directory
        for directory in candidates
        if _dir_has_no_remaining_files(
            directory,
            ignoring_files=ignoring if ignore_files_still_present else set(),
        )
    ]
    return sorted(emptied, key=lambda p: (len(p.parts), str(p).lower()))
