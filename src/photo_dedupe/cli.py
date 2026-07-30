"""CLI for scanning, reporting, and cleaning duplicate photos."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from photo_dedupe import __version__
from photo_dedupe.db import Database
from photo_dedupe.dedupe import (
    KeeperPolicy,
    filter_groups_by_roots,
    filter_groups_involving_roots,
    find_hash_duplicates,
    path_is_under_roots,
)
from photo_dedupe.hashing import HASH_ALGO
from photo_dedupe.organize import (
    apply_organize_plan,
    build_organize_plan,
    find_emptied_directories,
    remove_empty_directories,
)
from photo_dedupe.plan import (
    filter_plan_by_keeper_under,
    load_clean_plan,
)
from photo_dedupe.report import export_reports, format_bytes
from photo_dedupe.scanner import DEFAULT_EXTENSIONS, scan_roots

app = typer.Typer(
    name="photo-dedupe",
    help=(
        "Find and remove exact duplicate photos using a local SQLite index. "
        "Works on local folders and cloud sync paths (Google Drive / OneDrive)."
    ),
    epilog=(
        "Typical flow: scan → status → clean → organize → purge-missing. "
        "Destructive commands are dry-run unless you pass --apply."
    ),
    add_completion=False,
    no_args_is_help=True,
)


def _default_db_path() -> Path:
    return Path.cwd() / "photo-dedupe.sqlite"


def _open_db(db_path: Path) -> Database:
    return Database(db_path)


def _keeper_policy(
    keep: str,
    prefer_root: list[Path] | None,
    avoid_root: list[Path] | None = None,
) -> KeeperPolicy:
    keep_norm = keep.lower().strip()
    if keep_norm not in ("oldest", "newest"):
        raise typer.BadParameter("keep must be 'oldest' or 'newest'")
    return KeeperPolicy(
        keep=keep_norm,  # type: ignore[arg-type]
        prefer_roots=tuple(prefer_root or ()),
        avoid_roots=tuple(avoid_root or ()),
    )


@app.callback()
def main(
    ctx: typer.Context,
    db: Path = typer.Option(
        None,
        "--db",
        help="SQLite index path (default: ./photo-dedupe.sqlite in the current directory)",
        show_default=False,
    ),
) -> None:
    """Photo de-duplication toolkit."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db or _default_db_path()


@app.command()
def version() -> None:
    """Show package version and content-hash algorithm."""
    typer.echo(f"photo-dedupe {__version__} (hash={HASH_ALGO})")


@app.command()
def status(
    ctx: typer.Context,
    check_disk: bool = typer.Option(
        True,
        "--check-disk/--no-check-disk",
        help="Count present index rows whose files are missing on disk (slower on large indexes)",
    ),
) -> None:
    """Show index health: sources, file counts, and duplicate summary."""
    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        stats = database.count_stats()
        sources = database.list_sources()
        groups = find_hash_duplicates(database)
        ghost_count = (
            database.count_present_missing_on_disk() if check_disk else None
        )

    extra = sum(len(g.delete_candidates) for g in groups)
    reclaimable = sum(sum(f.size for f in g.delete_candidates) for g in groups)

    typer.echo(f"Database: {db_path.resolve()}")
    typer.echo(f"Hash algorithm: {HASH_ALGO}")
    typer.echo("")
    typer.echo(f"Sources ({len(sources)}):")
    if not sources:
        typer.echo("  (none)")
    else:
        for _sid, root, added in sources:
            typer.echo(f"  - {root}")
            typer.echo(f"      added: {added}")
    typer.echo("")
    typer.echo("Files")
    typer.echo(f"  Present:  {stats['present']}")
    typer.echo(f"  Hashed:   {stats['hashed']}")
    typer.echo(f"  Missing:  {stats['missing']}")
    typer.echo(f"  Size:     {format_bytes(stats['present_bytes'])}")
    if ghost_count is not None:
        typer.echo(f"  Ghosts:   {ghost_count}  (present in index, missing on disk)")
    typer.echo("")
    typer.echo("Duplicates")
    typer.echo(f"  Exact groups: {len(groups)}")
    typer.echo(f"  Extra files:  {extra}")
    typer.echo(f"  Reclaimable:  {format_bytes(reclaimable)}")
    if stats["missing"] > 0:
        typer.echo("")
        typer.echo("Tip: run `photo-dedupe purge-missing` to drop stale missing rows.")
    if ghost_count:
        typer.echo(
            "Tip: run `photo-dedupe scan <roots>` then "
            "`photo-dedupe purge-missing --apply` to clear ghosts."
        )


@app.command()
def scan(
    ctx: typer.Context,
    paths: list[Path] = typer.Argument(
        ...,
        help="Directories or files to index (repeatable)",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Follow symlinks while walking directories (off by default)",
    ),
) -> None:
    """Scan folders and update the SQLite index.

    Records name/size/mtime, reuses hashes when unchanged, and hashes only
    files that share a size with another present file.
    """
    db_path: Path = ctx.obj["db_path"]
    for root in paths:
        if not root.exists():
            typer.echo(
                f"Root not found (skipped): {root}\n"
                f"  Tip: quote paths with spaces, e.g. "
                f'"G:\\My Drive\\Photography\\_unsorted"',
                err=True,
            )
    with _open_db(db_path) as database:
        result = scan_roots(
            database,
            paths,
            extensions=DEFAULT_EXTENSIONS,
            follow_symlinks=follow_symlinks,
        )
        stats = result.stats
        db_stats = database.count_stats()

    if stats.roots == 0:
        typer.echo("No valid roots scanned.", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Database: {db_path}")
    typer.echo(f"Roots scanned: {stats.roots}")
    typer.echo(f"Images seen: {stats.seen}")
    typer.echo(f"Index upserts: {stats.upserted}")
    typer.echo(f"Hashes reused: {stats.reused_hash}")
    typer.echo(f"Newly hashed: {stats.hashed}")
    typer.echo(f"Unreadable: {stats.unreadable}")
    typer.echo(f"Hash errors: {stats.hash_errors}")
    typer.echo(
        f"Index totals — present={db_stats['present']} "
        f"hashed={db_stats['hashed']} missing={db_stats['missing']}"
    )


def _sort_by(value: str) -> str:
    norm = value.lower().strip()
    if norm not in ("path", "hash"):
        raise typer.BadParameter("sort must be 'path' or 'hash'")
    return norm


@app.command()
def report(
    ctx: typer.Context,
    format: str = typer.Option(
        "both",
        "--format",
        help="Output format: md, json, or both",
    ),
    output_dir: Path = typer.Option(
        Path("output"),
        "--output-dir",
        "-o",
        help="Directory for report.md / duplicates.json (default: ./output)",
    ),
    under: Optional[list[Path]] = typer.Option(
        None,
        "--under",
        help="Only report groups that involve this path; delete candidates listed are limited to it (repeatable)",
    ),
    keep: str = typer.Option(
        "oldest",
        "--keep",
        help="Which copy to keep: oldest or newest (by mtime)",
    ),
    prefer_root: Optional[list[Path]] = typer.Option(
        None,
        "--prefer-root",
        help="Prefer keeping files under this path when choosing the keeper (repeatable)",
    ),
    avoid_root: Optional[list[Path]] = typer.Option(
        None,
        "--avoid-root",
        help="Prefer any copy outside this path when another exists (repeatable); e.g. phone unsorted",
    ),
    sort: str = typer.Option(
        "path",
        "--sort",
        help="Order groups by keeper path (default) or hash",
    ),
) -> None:
    """Write Markdown and/or JSON duplicate reports from the index."""
    fmt = format.lower().strip()
    if fmt not in {"md", "json", "both"}:
        raise typer.BadParameter("format must be md, json, or both")
    sort_by = _sort_by(sort)

    policy = _keeper_policy(keep, prefer_root, avoid_root)
    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        written = export_reports(
            database,
            output_dir,
            fmt=fmt,
            policy=policy,
            under=list(under or []),
            sort_by=sort_by,  # type: ignore[arg-type]
        )

    for path in written:
        typer.echo(f"Wrote {path}")


@app.command()
def duplicates(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Print JSON instead of a text listing",
    ),
    under: Optional[list[Path]] = typer.Option(
        None,
        "--under",
        help="Only list groups that involve this path; delete candidates shown are limited to it (repeatable)",
    ),
    keep: str = typer.Option(
        "oldest",
        "--keep",
        help="Which copy to keep: oldest or newest (by mtime)",
    ),
    prefer_root: Optional[list[Path]] = typer.Option(
        None,
        "--prefer-root",
        help="Prefer keeping files under this path when choosing the keeper (repeatable)",
    ),
    avoid_root: Optional[list[Path]] = typer.Option(
        None,
        "--avoid-root",
        help="Prefer any copy outside this path when another exists (repeatable); e.g. phone unsorted",
    ),
    sort: str = typer.Option(
        "path",
        "--sort",
        help="Order groups by keeper path (default) or hash",
    ),
    show_hash: bool = typer.Option(
        False,
        "--show-hash",
        help="Include content hash in the text listing",
    ),
) -> None:
    """List exact duplicate groups and the chosen keeper for each."""
    policy = _keeper_policy(keep, prefer_root, avoid_root)
    sort_by = _sort_by(sort)
    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        groups = filter_groups_involving_roots(
            find_hash_duplicates(database, policy, sort_by=sort_by),  # type: ignore[arg-type]
            list(under or []),
        )

    if json_out:
        payload = [
            {
                "hash": g.key,
                "keeper": g.keeper.path,
                "delete_candidates": [f.path for f in g.delete_candidates],
            }
            for g in groups
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    if not groups:
        typer.echo("No exact duplicates found.")
        return

    typer.echo(f"Found {len(groups)} duplicate group(s):\n")
    for i, group in enumerate(groups, start=1):
        typer.echo(f"[{i}] {group.keeper.name}")
        if show_hash:
            typer.echo(f"  hash:   {group.key}")
        typer.echo(f"  keep:   {group.keeper.path}")
        for f in group.delete_candidates:
            typer.echo(f"  delete: {f.path}")
        typer.echo("")


@app.command(
    epilog=(
        "Examples:\n"
        "  photo-dedupe clean\n"
        "  photo-dedupe clean --detailed\n"
        "  photo-dedupe clean --prefer-root ~/Pictures --delete-under ~/Downloads -d\n"
        "  photo-dedupe duplicates --json > output/plan.json\n"
        "  photo-dedupe clean --plan output/plan.json "
        '--keeper-under "G:/My Drive/Photography/Tequila Hike" -d\n'
        "  photo-dedupe clean --plan output/plan.json --apply\n"
    ),
)
def clean(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Delete files on disk (default is dry-run preview only)",
    ),
    detailed: bool = typer.Option(
        False,
        "--detailed",
        "-d",
        help="Show per-file keep/delete listing (default output is summary only)",
    ),
    plan: Optional[Path] = typer.Option(
        None,
        "--plan",
        help="JSON plan from `duplicates --json` (deletes exactly those candidates)",
    ),
    delete_under: Optional[list[Path]] = typer.Option(
        None,
        "--delete-under",
        help="Only allow deletes under this directory; other paths are left alone (repeatable)",
    ),
    keeper_under: Optional[list[Path]] = typer.Option(
        None,
        "--keeper-under",
        help="Only include groups whose keeper is under this directory (repeatable)",
    ),
    keep: str = typer.Option(
        "oldest",
        "--keep",
        help="Which copy to keep: oldest or newest (by mtime); ignored with --plan",
    ),
    prefer_root: Optional[list[Path]] = typer.Option(
        None,
        "--prefer-root",
        help="Prefer keeping files under this path when choosing the keeper (repeatable); ignored with --plan",
    ),
    avoid_root: Optional[list[Path]] = typer.Option(
        None,
        "--avoid-root",
        help="Prefer any copy outside this path when another exists (repeatable); ignored with --plan",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Path for the delete log when using --apply (default: ./logs/photo-dedupe-delete.log)",
    ),
) -> None:
    """Preview or delete exact duplicate files.

    Default is a dry-run summary (counts and sizes). Use --detailed for paths.
    Pass --plan to apply a curated JSON list from `duplicates --json`.
    Nothing is deleted unless you pass --apply.
    """
    do_delete = apply
    delete_roots = list(delete_under or [])
    keeper_roots = list(keeper_under or [])
    policy = _keeper_policy(keep, prefer_root, avoid_root)

    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    # Unified work items: (keeper_path, delete_path, size_bytes)
    work: list[tuple[str, str, int]] = []
    group_count = 0
    present_count = 0
    size_before = 0
    all_groups_count = 0

    if plan is not None:
        if not plan.exists():
            typer.echo(f"Plan not found: {plan}", err=True)
            raise typer.Exit(code=1)
        try:
            entries = load_clean_plan(plan)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            typer.echo(f"Failed to load plan: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if keeper_roots:
            entries = filter_plan_by_keeper_under(entries, keeper_roots)
        group_count = len(entries)
        all_groups_count = group_count
        for entry in entries:
            for delete_path in entry.delete_candidates:
                if delete_roots and not path_is_under_roots(
                    delete_path, delete_roots
                ):
                    continue
                size = 0
                p = Path(delete_path)
                try:
                    if p.is_file():
                        size = p.stat().st_size
                except OSError:
                    size = 0
                work.append((entry.keeper, delete_path, size))
        with _open_db(db_path) as database:
            stats = database.count_stats()
            present_count = stats["present"]
            size_before = stats["present_bytes"]
    else:
        with _open_db(db_path) as database:
            stats = database.count_stats()
            present_count = stats["present"]
            size_before = stats["present_bytes"]
            all_groups = find_hash_duplicates(database, policy)
            all_groups_count = len(all_groups)
            if keeper_roots:
                all_groups = [
                    g
                    for g in all_groups
                    if path_is_under_roots(g.keeper.path, keeper_roots)
                ]
            groups = filter_groups_by_roots(all_groups, delete_roots)
            group_count = len(groups)
            for g in groups:
                for f in g.delete_candidates:
                    work.append((g.keeper.path, f.path, f.size))

    candidates = work
    size_to_delete = sum(size for _k, _p, size in candidates)
    size_after = size_before - size_to_delete

    mode = "APPLY" if do_delete else "DRY-RUN"
    typer.echo(f"{mode} summary")
    if plan is not None:
        typer.echo(f"  Plan:                   {plan}")
    else:
        typer.echo(f"  Keep policy:            {policy.keep}")
        if policy.prefer_roots:
            typer.echo("  Prefer roots:")
            for r in policy.prefer_roots:
                typer.echo(f"    - {r.resolve()}")
        if policy.avoid_roots:
            typer.echo("  Avoid roots:")
            for r in policy.avoid_roots:
                typer.echo(f"    - {r.resolve()}")
    if keeper_roots:
        typer.echo("  Keeper under:")
        for r in keeper_roots:
            typer.echo(f"    - {r.resolve()}")
    if delete_roots:
        typer.echo("  Delete under:")
        for r in delete_roots:
            typer.echo(f"    - {r.resolve()}")
    if delete_roots or keeper_roots or plan is not None:
        typer.echo(f"  Index files:            {present_count}")
        if plan is None:
            typer.echo(f"  Index duplicate groups: {all_groups_count}")
        typer.echo(f"  Index size:             {format_bytes(size_before)}")
        typer.echo(f"  Groups in scope:        {group_count}")
        typer.echo(f"  Files to delete:        {len(candidates)}")
        typer.echo(f"  Size to delete:         {format_bytes(size_to_delete)}")
        typer.echo(f"  Files after clean:      {present_count - len(candidates)}")
        typer.echo(f"  Size after clean:       {format_bytes(size_after)}")
    else:
        typer.echo(f"  Total files found:      {present_count}")
        typer.echo(f"  Duplicate groups found: {all_groups_count}")
        typer.echo(f"  Files to delete:        {len(candidates)}")
        typer.echo(f"  Files after clean:      {present_count - len(candidates)}")
        typer.echo(f"  Total size (before):    {format_bytes(size_before)}")
        typer.echo(f"  Size to delete:         {format_bytes(size_to_delete)}")
        typer.echo(f"  Total size (after):     {format_bytes(size_after)}")

    if not candidates:
        typer.echo("\nNo duplicate files to remove.")
        return

    deleted: list[str] = []
    errors: list[str] = []

    # Group by keeper for detailed display
    by_keeper: dict[str, list[tuple[str, int]]] = {}
    for keeper, delete_path, size in candidates:
        by_keeper.setdefault(keeper, []).append((delete_path, size))

    if detailed and not do_delete:
        typer.echo("")
        for keeper, items in by_keeper.items():
            typer.echo(f"keep: {keeper}")
            for delete_path, size in items:
                typer.echo(
                    f"  would delete: {delete_path} ({format_bytes(size)})"
                )

    if do_delete:
        if detailed:
            typer.echo("")
        for keeper, items in by_keeper.items():
            if detailed:
                typer.echo(f"keep: {keeper}")
            for delete_path, _size in items:
                path = Path(delete_path)
                try:
                    path.unlink(missing_ok=True)
                    deleted.append(delete_path)
                    if detailed:
                        typer.echo(f"  deleted: {delete_path}")
                except OSError as exc:
                    errors.append(f"{delete_path}: {exc}")
                    typer.echo(f"  error: {delete_path} ({exc})", err=True)

        if deleted:
            with _open_db(db_path) as database:
                database.remove_files_by_paths(deleted)

        log_path = log_file or (Path.cwd() / "logs" / "photo-dedupe-delete.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        lines = [f"# photo-dedupe delete log {stamp}", ""]
        if plan is not None:
            lines.append(f"# plan: {plan}")
            lines.append("")
        lines.extend(deleted)
        if errors:
            lines.append("")
            lines.append("# errors")
            lines.extend(errors)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        deleted_set = set(deleted)
        deleted_bytes = sum(
            size for _k, p, size in candidates if p in deleted_set
        )
        typer.echo(
            f"\nDeleted {len(deleted)} file(s) "
            f"({format_bytes(deleted_bytes)}). Log: {log_path}"
        )
        if errors:
            typer.echo(f"{len(errors)} error(s).", err=True)
            raise typer.Exit(code=1)
    else:
        typer.echo("\nDry-run only. Re-run with --apply to delete.")
        if not detailed:
            typer.echo("Use --detailed for a per-file preview.")


@app.command(
    epilog=(
        "Examples:\n"
        "  photo-dedupe organize --dest ~/Pictures/Library --under ~/Downloads\n"
        "  photo-dedupe organize --dest ~/Pictures/Library --under ~/Downloads -d\n"
        "  photo-dedupe organize --dest ~/Pictures/Library --under ~/Downloads --apply\n"
        "  photo-dedupe organize --dest ~/Pictures/Library --under ~/Downloads "
        "--apply --remove-empty-dirs\n"
    ),
)
def organize(
    ctx: typer.Context,
    dest: Path = typer.Option(
        ...,
        "--dest",
        help="Destination library root (files move into YYYY/YYYY-MM/ under this)",
    ),
    under: Optional[list[Path]] = typer.Option(
        None,
        "--under",
        help="Only organize files under this path (repeatable)",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Move files on disk (default is dry-run preview only)",
    ),
    remove_empty_dirs: bool = typer.Option(
        False,
        "--remove-empty-dirs",
        help="After --apply, delete directories emptied by this organize run (opt-in)",
    ),
    detailed: bool = typer.Option(
        False,
        "--detailed",
        "-d",
        help="Show per-file source → destination mapping",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Path for the move log when using --apply (default: ./logs/photo-dedupe-organize.log)",
    ),
) -> None:
    """Move indexed photos into dest/YYYY/YYYY-MM/ (dry-run by default).

    Date comes from EXIF DateTimeOriginal when available, otherwise file mtime.
    Files already under --dest are skipped. Index paths are updated on --apply.
    Empty source directories are reported; delete them only with --remove-empty-dirs.
    """
    if remove_empty_dirs and not apply:
        typer.echo(
            "--remove-empty-dirs requires --apply (dry-run only reports would-empty dirs).",
            err=True,
        )
        raise typer.Exit(code=1)

    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    under_list = list(under or [])
    with _open_db(db_path) as database:
        plan = build_organize_plan(database, dest, under=under_list)
        to_move = plan.to_move
        skipped = plan.skipped
        move_bytes = sum(i.record.size for i in to_move)
        exif_count = sum(1 for i in to_move if i.date_source == "exif")
        mtime_count = sum(1 for i in to_move if i.date_source == "mtime")

        mode = "APPLY" if apply else "DRY-RUN"
        typer.echo(f"{mode} organize summary")
        typer.echo(f"  Destination:     {plan.dest_root}")
        typer.echo(f"  Layout:          YYYY/YYYY-MM")
        if under_list:
            typer.echo("  Under:")
            for path in under_list:
                typer.echo(f"    - {path.resolve()}")
        typer.echo(f"  Files to move:   {len(to_move)}")
        typer.echo(f"  Size to move:    {format_bytes(move_bytes)}")
        typer.echo(f"  Date from EXIF:  {exif_count}")
        typer.echo(f"  Date from mtime: {mtime_count}")
        typer.echo(f"  Skipped:         {len(skipped)}")
        typer.echo(
            f"  Remove empty dirs: {'yes' if remove_empty_dirs else 'no'}"
        )

        source_paths = [item.source for item in to_move]
        emptied: list[Path] = []
        if not apply and to_move:
            emptied = find_emptied_directories(
                source_paths,
                under=under_list or None,
                ignore_files_still_present=True,
            )
            typer.echo(f"  Would empty dirs: {len(emptied)}")

        if detailed:
            if to_move:
                typer.echo("")
                typer.echo("Moves:")
                for item in to_move:
                    typer.echo(
                        f"  {item.source} -> {item.dest} "
                        f"({item.date_source} {item.taken_at.date()})"
                    )
            if skipped:
                typer.echo("")
                typer.echo("Skipped:")
                for item in skipped:
                    typer.echo(f"  {item.source} ({item.skip_reason})")
            if not apply and emptied:
                typer.echo("")
                typer.echo("Would empty directories:")
                for directory in emptied:
                    typer.echo(f"  {directory}")

        if not to_move:
            typer.echo("\nNothing to organize.")
            return

        if apply:
            moved, errors = apply_organize_plan(database, plan)
            emptied_after = find_emptied_directories(
                [src for src, _dst in moved],
                under=under_list or None,
                ignore_files_still_present=False,
            )
            removed_dirs: list[Path] = []
            prune_errors: list[str] = []
            if remove_empty_dirs and emptied_after:
                removed_dirs, prune_errors = remove_empty_directories(
                    emptied_after,
                    protect=[plan.dest_root],
                )
                errors.extend(prune_errors)

            log_path = log_file or (
                Path.cwd() / "logs" / "photo-dedupe-organize.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat()
            lines = [f"# photo-dedupe organize log {stamp}", ""]
            lines.extend(f"{src} -> {dst}" for src, dst in moved)
            if emptied_after:
                lines.append("")
                if remove_empty_dirs:
                    lines.append("# emptied directories")
                else:
                    lines.append("# emptied directories (not deleted)")
                lines.extend(str(p) for p in emptied_after)
            if removed_dirs:
                lines.append("")
                lines.append("# removed empty directories")
                lines.extend(str(p) for p in removed_dirs)
            if errors:
                lines.append("")
                lines.append("# errors")
                lines.extend(errors)
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            typer.echo(f"\nMoved {len(moved)} file(s). Log: {log_path}")
            if remove_empty_dirs:
                typer.echo(f"Removed empty directories: {len(removed_dirs)}")
                for directory in removed_dirs:
                    typer.echo(f"  {directory}")
                leftover = [
                    d
                    for d in emptied_after
                    if d.resolve() not in {r.resolve() for r in removed_dirs}
                ]
                if leftover:
                    typer.echo(
                        f"Still present (not empty or protected): {len(leftover)}"
                    )
                    for directory in leftover:
                        typer.echo(f"  {directory}")
            else:
                typer.echo(
                    f"Emptied directories: {len(emptied_after)} (not deleted; "
                    "pass --remove-empty-dirs to prune)"
                )
                for directory in emptied_after:
                    typer.echo(f"  {directory}")
            if errors:
                typer.echo(f"{len(errors)} error(s).", err=True)
                raise typer.Exit(code=1)
        else:
            if emptied and not detailed:
                typer.echo("")
                typer.echo("Would empty directories:")
                for directory in emptied:
                    typer.echo(f"  {directory}")
            typer.echo("\nDry-run only. Re-run with --apply to move files.")
            if not detailed:
                typer.echo("Use --detailed for a per-file preview.")


@app.command(
    "purge-missing",
    epilog=(
        "Examples:\n"
        "  photo-dedupe purge-missing\n"
        "  photo-dedupe purge-missing --detailed\n"
        "  photo-dedupe purge-missing --apply\n"
    ),
)
def purge_missing(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Remove missing rows from the SQLite index (default is dry-run)",
    ),
    detailed: bool = typer.Option(
        False,
        "--detailed",
        "-d",
        help="List each missing path that would be purged",
    ),
) -> None:
    """Remove stale index rows for files no longer on disk.

    Run scan first so missing status is up to date. This deletes database
    rows only — not files on disk.
    """
    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        stats = database.count_stats()
        missing = database.missing_files()
        missing_bytes = sum(f.size for f in missing)

        mode = "APPLY" if apply else "DRY-RUN"
        typer.echo(f"{mode} purge-missing summary")
        typer.echo(f"  Present files:   {stats['present']}")
        typer.echo(f"  Missing rows:    {len(missing)}")
        typer.echo(f"  Missing size:    {format_bytes(missing_bytes)}")

        if not missing:
            typer.echo("\nNo missing rows to purge.")
            return

        if detailed:
            typer.echo("")
            for record in missing:
                typer.echo(f"  {record.path} ({format_bytes(record.size)})")

        if apply:
            removed = database.purge_missing()
            typer.echo(f"\nPurged {removed} missing row(s) from the index.")
        else:
            typer.echo("\nDry-run only. Re-run with --apply to purge.")
            if not detailed:
                typer.echo("Use --detailed to list missing paths.")


if __name__ == "__main__":
    app()
