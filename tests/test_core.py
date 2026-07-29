from __future__ import annotations

from pathlib import Path

from photo_dedupe.db import Database, FileRecord
from photo_dedupe.dedupe import choose_keeper, find_hash_duplicates
from photo_dedupe.hashing import hash_file
from photo_dedupe.report import build_report_payload, export_reports
from photo_dedupe.scanner import scan_roots

from image_util import PNG_BLUE, write_png


def test_hash_file_stable(tmp_path: Path) -> None:
    a = write_png(tmp_path / "a.png", (255, 0, 0))
    b = write_png(tmp_path / "b.png", (255, 0, 0))
    c = write_png(tmp_path / "c.png", (0, 0, 255))
    assert hash_file(a) == hash_file(b)
    assert hash_file(a) != hash_file(c)


def test_scan_finds_duplicates_and_reuses_hash(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    write_png(photos / "keep" / "vacation.png", (255, 0, 0))
    write_png(photos / "copy" / "vacation_copy.png", (255, 0, 0))
    # Force a distinct size so this file is excluded from size-gated hashing.
    # (Tiny PNGs with different colors often compress to the same byte length.)
    unique = photos / "unique.png"
    unique.parent.mkdir(parents=True, exist_ok=True)
    unique.write_bytes(PNG_BLUE + b"\x00" * 32)

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        first = scan_roots(db, [photos])
        assert first.stats.seen == 3
        assert first.stats.hashed == 2
        groups = find_hash_duplicates(db)
        assert len(groups) == 1
        assert len(groups[0].files) == 2

        second = scan_roots(db, [photos])
        assert second.stats.reused_hash == 2
        assert second.stats.hashed == 0


def test_choose_keeper_oldest_mtime(tmp_path: Path) -> None:
    older = FileRecord(
        id=1,
        source_id=1,
        path=str(tmp_path / "a" / "long" / "path" / "x.png"),
        name="x.png",
        size=10,
        mtime=100.0,
        hash="abc",
        hashed_at=None,
        status="present",
    )
    newer = FileRecord(
        id=2,
        source_id=1,
        path=str(tmp_path / "b.png"),
        name="b.png",
        size=10,
        mtime=200.0,
        hash="abc",
        hashed_at=None,
        status="present",
    )
    assert choose_keeper([newer, older]) == older


def test_report_export(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    write_png(photos / "one.png", (255, 0, 0))
    write_png(photos / "two.png", (255, 0, 0))

    db_path = tmp_path / "index.sqlite"
    out = tmp_path / "out"
    with Database(db_path) as db:
        scan_roots(db, [photos])
        written = export_reports(db, out, fmt="both")
        payload = build_report_payload(db)

    assert len(written) == 2
    assert (out / "report.md").exists()
    assert (out / "duplicates.json").exists()
    assert payload["duplicate_groups"] == 1
    assert "Exact duplicates" in (out / "report.md").read_text(encoding="utf-8")


def test_unique_sizes_not_hashed(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    write_png(photos / "red.png", (255, 0, 0))
    big = photos / "big.png"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(PNG_BLUE + b"\x00" * 64)

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        result = scan_roots(db, [photos])
        assert result.stats.hashed == 0
        assert find_hash_duplicates(db) == []
