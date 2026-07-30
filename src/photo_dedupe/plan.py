"""Load and filter clean plans produced by ``duplicates --json``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from photo_dedupe.dedupe import filename_contains_any, path_is_under_roots


@dataclass(frozen=True)
class PlanEntry:
    keeper: str
    delete_candidates: tuple[str, ...]
    hash: str | None = None


def _read_text_auto(path: Path) -> str:
    """Read text trying encodings PowerShell redirects commonly use."""
    data = Path(path).read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    # UTF-16 LE without BOM sometimes appears; null bytes in ASCII JSON are a hint
    if b"\x00" in data[:64]:
        return data.decode("utf-16")
    return data.decode("utf-8")


def load_clean_plan(path: Path) -> list[PlanEntry]:
    """Load a clean plan from duplicates --json or report duplicates.json."""
    raw = json.loads(_read_text_auto(path))
    if isinstance(raw, dict) and "hash_duplicates" in raw:
        items = raw["hash_duplicates"]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(
            "Plan must be a JSON list from `duplicates --json`, "
            "or a report object with hash_duplicates"
        )

    entries: list[PlanEntry] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each plan entry must be a JSON object")
        keeper = item.get("keeper")
        if isinstance(keeper, dict):
            keeper = keeper.get("path")
        deletes = item.get("delete_candidates") or item.get("delete") or []
        normalized_deletes: list[str] = []
        for d in deletes:
            if isinstance(d, dict):
                p = d.get("path")
                if p:
                    normalized_deletes.append(str(p))
            else:
                normalized_deletes.append(str(d))
        if not keeper or not normalized_deletes:
            continue
        entries.append(
            PlanEntry(
                keeper=str(keeper),
                delete_candidates=tuple(normalized_deletes),
                hash=item.get("hash") or item.get("key"),
            )
        )
    return entries


def filter_plan_by_keeper_under(
    entries: list[PlanEntry],
    roots: list[Path],
) -> list[PlanEntry]:
    """Keep only entries whose keeper path is under one of the roots."""
    if not roots:
        return entries
    return [
        e for e in entries if path_is_under_roots(e.keeper, roots)
    ]


def filter_plan_by_delete_names(
    entries: list[PlanEntry],
    needles: list[str] | tuple[str, ...] | None,
) -> list[PlanEntry]:
    """Keep only delete candidates whose filenames match needles; drop empties."""
    names = tuple(n for n in (needles or ()) if n and str(n).strip())
    if not names:
        return entries
    filtered: list[PlanEntry] = []
    for entry in entries:
        deletes = tuple(
            p for p in entry.delete_candidates if filename_contains_any(p, names)
        )
        if not deletes:
            continue
        filtered.append(
            PlanEntry(keeper=entry.keeper, delete_candidates=deletes, hash=entry.hash)
        )
    return filtered


def write_clean_plan(path: Path, entries: list[PlanEntry]) -> Path:
    """Write plan entries in duplicates --json shape."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "hash": e.hash,
            "keeper": e.keeper,
            "delete_candidates": list(e.delete_candidates),
        }
        for e in entries
    ]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
