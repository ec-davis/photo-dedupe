"""CLI for scanning, reporting, and cleaning duplicate photos."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from photo_dedupe import __version__
from photo_dedupe.db import Database
from photo_dedupe.dedupe import find_hash_duplicates
from photo_dedupe.hashing import HASH_ALGO
from photo_dedupe.report import export_reports
from photo_dedupe.scanner import DEFAULT_EXTENSIONS, scan_roots

app = typer.Typer(
    name="photo-dedupe",
    help="Organize and de-duplicate photos using a local SQLite index.",
    add_completion=False,
    no_args_is_help=True,
)


def _default_db_path() -> Path:
    return Path.cwd() / "photo-dedupe.sqlite"


def _open_db(db_path: Path) -> Database:
    return Database(db_path)


@app.callback()
def main(
    ctx: typer.Context,
    db: Path = typer.Option(
        None,
        "--db",
        help="Path to SQLite index (default: ./photo-dedupe.sqlite)",
        show_default=False,
    ),
) -> None:
    """Photo de-duplication toolkit."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db or _default_db_path()


@app.command()
def version() -> None:
    """Show version and hash algorithm."""
    typer.echo(f"photo-dedupe {__version__} (hash={HASH_ALGO})")


@app.command()
def scan(
    ctx: typer.Context,
    paths: list[Path] = typer.Argument(
        ...,
        help="One or more directories (or files) to scan",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Follow symlinks while walking directories",
    ),
) -> None:
    """Scan folders and update the SQLite index (size-gated hashing)."""
    db_path: Path = ctx.obj["db_path"]
    with _open_db(db_path) as database:
        result = scan_roots(
            database,
            paths,
            extensions=DEFAULT_EXTENSIONS,
            follow_symlinks=follow_symlinks,
        )
        stats = result.stats
        db_stats = database.count_stats()

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


@app.command()
def report(
    ctx: typer.Context,
    format: str = typer.Option(
        "both",
        "--format",
        help="Report format: md, json, or both",
    ),
    output_dir: Path = typer.Option(
        Path("."),
        "--output-dir",
        "-o",
        help="Directory for report files",
    ),
) -> None:
    """Write Markdown and/or JSON reports from the current index."""
    fmt = format.lower().strip()
    if fmt not in {"md", "json", "both"}:
        raise typer.BadParameter("format must be md, json, or both")

    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        written = export_reports(database, output_dir, fmt=fmt)

    for path in written:
        typer.echo(f"Wrote {path}")


@app.command()
def duplicates(
    ctx: typer.Context,
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON instead of a text summary",
    ),
) -> None:
    """Print exact duplicate groups from the index."""
    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        groups = find_hash_duplicates(database)

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
        typer.echo(f"[{i}] hash={group.key[:12]}…")
        typer.echo(f"  keep:   {group.keeper.path}")
        for f in group.delete_candidates:
            typer.echo(f"  delete: {f.path}")
        typer.echo("")


@app.command()
def clean(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually delete duplicate files (without this flag, dry-run only)",
    ),
    log_file: Optional[Path] = typer.Option(
        None,
        "--log-file",
        help="Write delete log path (default: photo-dedupe-delete.log)",
    ),
) -> None:
    """Remove exact duplicate files using the keeper policy (dry-run by default)."""
    do_delete = apply

    db_path: Path = ctx.obj["db_path"]
    if not db_path.exists():
        typer.echo(f"Database not found: {db_path}", err=True)
        raise typer.Exit(code=1)

    with _open_db(db_path) as database:
        groups = find_hash_duplicates(database)

    candidates = [f for g in groups for f in g.delete_candidates]
    if not candidates:
        typer.echo("No duplicate files to remove.")
        return

    mode = "APPLY" if do_delete else "DRY-RUN"
    typer.echo(f"{mode}: {len(candidates)} file(s) in {len(groups)} group(s)\n")

    deleted: list[str] = []
    errors: list[str] = []

    for group in groups:
        typer.echo(f"keep: {group.keeper.path}")
        for f in group.delete_candidates:
            if do_delete:
                try:
                    Path(f.path).unlink()
                    deleted.append(f.path)
                    typer.echo(f"  deleted: {f.path}")
                except OSError as exc:
                    errors.append(f"{f.path}: {exc}")
                    typer.echo(f"  error: {f.path} ({exc})", err=True)
            else:
                typer.echo(f"  would delete: {f.path}")

    if do_delete:
        log_path = log_file or (Path.cwd() / "photo-dedupe-delete.log")
        stamp = datetime.now(timezone.utc).isoformat()
        lines = [f"# photo-dedupe delete log {stamp}", ""]
        lines.extend(deleted)
        if errors:
            lines.append("")
            lines.append("# errors")
            lines.extend(errors)
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        typer.echo(f"\nDeleted {len(deleted)} file(s). Log: {log_path}")
        if errors:
            typer.echo(f"{len(errors)} error(s).", err=True)
            raise typer.Exit(code=1)
    else:
        typer.echo("\nDry-run only. Re-run with --apply to delete.")


if __name__ == "__main__":
    app()
