from __future__ import annotations

import os
from pathlib import Path

from photo_dedupe.db import Database, FileRecord
from photo_dedupe.dedupe import (
    KeeperPolicy,
    choose_keeper,
    filter_groups_by_names,
    filter_groups_by_roots,
    filter_groups_involving_roots,
    find_hash_duplicates,
)
from photo_dedupe.hashing import hash_file
from photo_dedupe.organize import (
    allocate_dest,
    apply_organize_plan,
    build_organize_plan,
    dated_subdir,
    find_emptied_directories,
    remove_empty_directories,
)
from photo_dedupe.plan import (
    PlanEntry,
    filter_plan_by_delete_names,
    filter_plan_by_keeper_under,
    load_clean_plan,
    write_clean_plan,
)
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


def test_choose_keeper_oldest_and_newest(tmp_path: Path) -> None:
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
    assert choose_keeper([newer, older], KeeperPolicy(keep="newest")) == newer


def test_choose_keeper_prefer_root(tmp_path: Path) -> None:
    library = tmp_path / "library"
    downloads = tmp_path / "downloads"
    library.mkdir()
    downloads.mkdir()
    (library / "shot.png").write_bytes(b"x")
    (downloads / "shot.png").write_bytes(b"x")

    lib_file = FileRecord(
        id=1,
        source_id=1,
        path=str((library / "shot.png").resolve()),
        name="shot.png",
        size=10,
        mtime=200.0,
        hash="abc",
        hashed_at=None,
        status="present",
    )
    dl_file = FileRecord(
        id=2,
        source_id=1,
        path=str((downloads / "shot.png").resolve()),
        name="shot.png",
        size=10,
        mtime=100.0,
        hash="abc",
        hashed_at=None,
        status="present",
    )

    policy = KeeperPolicy(keep="oldest", prefer_roots=(library,))
    assert choose_keeper([dl_file, lib_file], policy) == lib_file


def test_choose_keeper_avoid_root(tmp_path: Path) -> None:
    album = tmp_path / "various unidentified"
    phone = tmp_path / "phone unsorted"
    album.mkdir()
    phone.mkdir()
    (album / "shot.png").write_bytes(b"x")
    (phone / "shot.png").write_bytes(b"x")

    album_file = FileRecord(
        id=1,
        source_id=1,
        path=str((album / "shot.png").resolve()),
        name="shot.png",
        size=10,
        mtime=200.0,  # newer
        hash="abc",
        hashed_at=None,
        status="present",
    )
    phone_file = FileRecord(
        id=2,
        source_id=1,
        path=str((phone / "shot.png").resolve()),
        name="shot.png",
        size=10,
        mtime=100.0,  # older — would win without avoid
        hash="abc",
        hashed_at=None,
        status="present",
    )

    policy = KeeperPolicy(keep="oldest", avoid_roots=(phone,))
    assert choose_keeper([phone_file, album_file], policy) == album_file


def test_choose_keeper_avoid_name(tmp_path: Path) -> None:
    folder = tmp_path / "pics"
    folder.mkdir()
    original = FileRecord(
        id=1,
        source_id=1,
        path=str((folder / "vacation.jpg").resolve()),
        name="vacation.jpg",
        size=10,
        mtime=200.0,  # newer
        hash="abc",
        hashed_at=None,
        status="present",
    )
    copy_of = FileRecord(
        id=2,
        source_id=1,
        path=str((folder / "Copy of vacation.jpg").resolve()),
        name="Copy of vacation.jpg",
        size=10,
        mtime=100.0,  # older — would win without avoid-name
        hash="abc",
        hashed_at=None,
        status="present",
    )

    policy = KeeperPolicy(keep="oldest", avoid_names=("copy of",))
    assert choose_keeper([copy_of, original], policy) == original


def test_choose_keeper_avoid_name_matches_folder(tmp_path: Path) -> None:
    album = tmp_path / "album"
    copy_dir = tmp_path / "birth - Copy"
    album.mkdir()
    copy_dir.mkdir()
    good = FileRecord(
        id=1,
        source_id=1,
        path=str((album / "shot.jpg").resolve()),
        name="shot.jpg",
        size=10,
        mtime=200.0,
        hash="abc",
        hashed_at=None,
        status="present",
    )
    bad = FileRecord(
        id=2,
        source_id=1,
        path=str((copy_dir / "shot.jpg").resolve()),
        name="shot.jpg",
        size=10,
        mtime=100.0,
        hash="abc",
        hashed_at=None,
        status="present",
    )
    policy = KeeperPolicy(keep="oldest", avoid_names=("copy",))
    assert choose_keeper([bad, good], policy) == good


def test_filter_groups_by_names(tmp_path: Path) -> None:
    pics = tmp_path / "pics"
    write_png(pics / "vacation.png", (255, 0, 0))
    write_png(pics / "Copy of vacation.png", (255, 0, 0))
    write_png(pics / "vacation_backup.png", (255, 0, 0))
    os.utime(pics / "vacation.png", (2000, 2000))
    os.utime(pics / "Copy of vacation.png", (1000, 1000))
    os.utime(pics / "vacation_backup.png", (1500, 1500))

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        scan_roots(db, [tmp_path])
        groups = find_hash_duplicates(
            db, KeeperPolicy(keep="oldest", avoid_names=("copy of",))
        )

    assert len(groups) == 1
    limited = filter_groups_by_names(groups, ["copy of"])
    assert len(limited) == 1
    assert len(limited[0].delete_candidates) == 1
    assert "copy of" in limited[0].delete_candidates[0].path.casefold()


def test_filter_plan_by_delete_names() -> None:
    entries = [
        PlanEntry(
            keeper=r"C:\pics\vacation.jpg",
            delete_candidates=(
                r"C:\pics\Copy of vacation.jpg",
                r"C:\pics\vacation_backup.jpg",
            ),
            hash="abc",
        ),
        PlanEntry(
            keeper=r"C:\pics\other.jpg",
            delete_candidates=(r"C:\pics\other_backup.jpg",),
            hash="def",
        ),
    ]
    filtered = filter_plan_by_delete_names(entries, ["copy of"])
    assert len(filtered) == 1
    assert filtered[0].delete_candidates == (r"C:\pics\Copy of vacation.jpg",)


def test_filter_groups_by_roots(tmp_path: Path) -> None:
    keep_dir = tmp_path / "library"
    junk_dir = tmp_path / "downloads"
    write_png(keep_dir / "original.png", (255, 0, 0))
    write_png(junk_dir / "copy.png", (255, 0, 0))
    original = keep_dir / "original.png"
    copy = junk_dir / "copy.png"
    os.utime(original, (1000, 1000))
    os.utime(copy, (2000, 2000))

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        scan_roots(db, [tmp_path])
        groups = find_hash_duplicates(db)

    assert len(groups) == 1
    limited = filter_groups_by_roots(groups, [junk_dir])
    assert len(limited) == 1
    assert len(limited[0].delete_candidates) == 1
    assert limited[0].delete_candidates[0].path == str(copy.resolve())
    assert limited[0].keeper.path == str(original.resolve())

    none = filter_groups_by_roots(groups, [tmp_path / "other"])
    assert none == []

    involving = filter_groups_involving_roots(groups, [junk_dir])
    assert len(involving) == 1
    assert len(involving[0].files) == 2
    assert len(involving[0].delete_candidates) == 1
    assert involving[0].delete_candidates[0].path == str(copy.resolve())

    empty = filter_groups_involving_roots(groups, [tmp_path / "other"])
    assert empty == []


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


def test_purge_missing(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    keep = write_png(photos / "keep.png", (255, 0, 0))
    gone = write_png(photos / "gone.png", (0, 0, 255))

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        scan_roots(db, [photos])
        assert db.count_stats()["present"] == 2

        gone.unlink()
        scan_roots(db, [photos])
        stats = db.count_stats()
        assert stats["present"] == 1
        assert stats["missing"] == 1
        assert len(db.missing_files()) == 1

        removed = db.purge_missing()
        assert removed == 1
        stats = db.count_stats()
        assert stats["missing"] == 0
        assert stats["present"] == 1
        assert db.get_file_by_path(str(keep.resolve())) is not None


def test_list_sources_and_ghost_count(tmp_path: Path) -> None:
    photos = tmp_path / "photos"
    write_png(photos / "a.png", (255, 0, 0))
    gone = write_png(photos / "b.png", (0, 0, 255))

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        scan_roots(db, [photos])
        sources = db.list_sources()
        assert len(sources) == 1
        assert sources[0][1] == str(photos.resolve())

        gone.unlink()
        # Stale present row (no rescan yet)
        assert db.count_present_missing_on_disk() == 1


def test_dated_subdir_and_allocate_dest(tmp_path: Path) -> None:
    from datetime import datetime

    assert dated_subdir(datetime(2023, 7, 4, 12, 0, 0)) == Path("2023") / "2023-07"
    claimed: set[Path] = set()
    first = allocate_dest(tmp_path / "a.png", claimed)
    second = allocate_dest(tmp_path / "a.png", claimed)
    assert first == tmp_path / "a.png"
    assert second == tmp_path / "a_1.png"


def test_organize_moves_and_updates_index(tmp_path: Path) -> None:
    src = tmp_path / "inbox"
    dest = tmp_path / "library"
    photo = write_png(src / "shot.png", (255, 0, 0))
    os.utime(photo, (1_688_428_800, 1_688_428_800))  # ~2023-07-04 UTC-ish

    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        scan_roots(db, [src])
        plan = build_organize_plan(db, dest, under=[src])
        assert len(plan.to_move) == 1
        item = plan.to_move[0]
        assert item.dest.parent.name == "2023-07"
        assert item.dest.parent.parent.name == "2023"

        emptied = find_emptied_directories(
            [item.source], under=[src], ignore_files_still_present=True
        )
        assert src.resolve() in {p.resolve() for p in emptied}

        moved, errors = apply_organize_plan(db, plan)
        assert errors == []
        assert len(moved) == 1
        assert not photo.exists()
        assert moved[0][1].is_file()
        assert db.get_file_by_path(str(photo.resolve())) is None
        assert db.get_file_by_path(str(moved[0][1].resolve())) is not None

        emptied_after = find_emptied_directories(
            [moved[0][0]], under=[src], ignore_files_still_present=False
        )
        assert src.resolve() in {p.resolve() for p in emptied_after}

        plan2 = build_organize_plan(db, dest)
        assert plan2.to_move == []
        assert any(i.skip_reason == "already under destination" for i in plan2.skipped)


def test_emptied_dirs_ignores_dirs_with_remaining_files(tmp_path: Path) -> None:
    folder = tmp_path / "inbox"
    moving = write_png(folder / "move_me.png", (255, 0, 0))
    write_png(folder / "stay.png", (0, 0, 255))
    emptied = find_emptied_directories(
        [moving], under=[folder], ignore_files_still_present=True
    )
    assert emptied == []


def test_remove_empty_directories(tmp_path: Path) -> None:
    nested = tmp_path / "inbox" / "2022" / "dups"
    nested.mkdir(parents=True)
    protect = tmp_path / "orgdest"
    protect.mkdir()
    (protect / "keep").mkdir()

    removed, errors = remove_empty_directories(
        [nested, nested.parent, nested.parent.parent],
        protect=[protect],
    )
    assert errors == []
    assert nested.resolve() in {p.resolve() for p in removed}
    assert not nested.exists()
    assert protect.exists()


def test_load_and_filter_clean_plan(tmp_path: Path) -> None:
    from photo_dedupe.plan import PlanEntry

    tequila = tmp_path / "Tequila Hike"
    phone = tmp_path / "phone unsorted"
    other = tmp_path / "other album"
    tequila.mkdir()
    phone.mkdir()
    other.mkdir()
    plan_path = tmp_path / "plan.json"
    write_clean_plan(
        plan_path,
        [
            PlanEntry(
                keeper=str((tequila / "a.jpg").resolve()),
                delete_candidates=(str((phone / "a.jpg").resolve()),),
                hash="aaa",
            ),
            PlanEntry(
                keeper=str((other / "b.jpg").resolve()),
                delete_candidates=(str((phone / "b.jpg").resolve()),),
                hash="bbb",
            ),
        ],
    )
    entries = load_clean_plan(plan_path)
    assert len(entries) == 2
    filtered = filter_plan_by_keeper_under(entries, [tequila])
    assert len(filtered) == 1
    assert "Tequila Hike" in filtered[0].keeper
