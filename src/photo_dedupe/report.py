"""Markdown and JSON report exporters."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from photo_dedupe.db import Database, FileRecord
from photo_dedupe.dedupe import (
    DuplicateGroup,
    find_hash_duplicates,
    find_name_size_mismatches,
)
from photo_dedupe.hashing import HASH_ALGO


def _file_dict(record: FileRecord) -> dict:
    return {
        "id": record.id,
        "path": record.path,
        "name": record.name,
        "size": record.size,
        "mtime": record.mtime,
        "hash": record.hash,
        "status": record.status,
    }


def _group_dict(group: DuplicateGroup) -> dict:
    return {
        "key": group.key,
        "kind": group.kind,
        "keeper": _file_dict(group.keeper),
        "delete_candidates": [_file_dict(f) for f in group.delete_candidates],
        "files": [_file_dict(f) for f in group.files],
    }


def build_report_payload(db: Database) -> dict:
    hash_groups = find_hash_duplicates(db)
    name_groups = find_name_size_mismatches(db)
    stats = db.count_stats()
    reclaimable = sum(
        sum(f.size for f in g.delete_candidates) for g in hash_groups
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hash_algorithm": HASH_ALGO,
        "stats": stats,
        "duplicate_groups": len(hash_groups),
        "duplicate_files_extra": sum(len(g.delete_candidates) for g in hash_groups),
        "reclaimable_bytes": reclaimable,
        "hash_duplicates": [_group_dict(g) for g in hash_groups],
        "name_size_mismatches": [_group_dict(g) for g in name_groups],
    }


def write_json_report(db: Database, path: Path) -> Path:
    path = Path(path)
    payload = build_report_payload(db)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{n} B"


def write_markdown_report(db: Database, path: Path) -> Path:
    path = Path(path)
    payload = build_report_payload(db)
    lines: list[str] = [
        "# Photo Deduper Report",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Hash algorithm: `{payload['hash_algorithm']}`",
        "",
        "## Summary",
        "",
        f"- Sources: {payload['stats']['sources']}",
        f"- Present files: {payload['stats']['present']}",
        f"- Hashed files: {payload['stats']['hashed']}",
        f"- Missing (from prior scans): {payload['stats']['missing']}",
        f"- Exact duplicate groups: {payload['duplicate_groups']}",
        f"- Extra duplicate files: {payload['duplicate_files_extra']}",
        f"- Reclaimable: {_format_bytes(payload['reclaimable_bytes'])}",
        "",
        "## Exact duplicates (same content hash)",
        "",
    ]

    hash_dups = payload["hash_duplicates"]
    if not hash_dups:
        lines.append("_No exact duplicates found._")
        lines.append("")
    else:
        for i, group in enumerate(hash_dups, start=1):
            lines.append(f"### Group {i}")
            lines.append("")
            lines.append(f"- Hash: `{group['key']}`")
            lines.append(f"- Keep: `{group['keeper']['path']}`")
            lines.append("- Delete candidates:")
            for f in group["delete_candidates"]:
                lines.append(
                    f"  - `{f['path']}` ({_format_bytes(f['size'])})"
                )
            lines.append("")

    lines.extend(
        [
            "## Same name + size, different hash",
            "",
        ]
    )
    mismatches = payload["name_size_mismatches"]
    if not mismatches:
        lines.append("_No name/size mismatches with differing hashes._")
        lines.append("")
    else:
        for i, group in enumerate(mismatches, start=1):
            lines.append(f"### Collision {i}")
            lines.append("")
            lines.append(f"- Key: `{group['key']}`")
            for f in group["files"]:
                lines.append(f"  - `{f['path']}` hash=`{f['hash']}`")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def export_reports(
    db: Database,
    output_dir: Path,
    *,
    fmt: str = "both",
) -> list[Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if fmt in ("md", "both"):
        written.append(write_markdown_report(db, output_dir / "report.md"))
    if fmt in ("json", "both"):
        written.append(write_json_report(db, output_dir / "duplicates.json"))
    return written
