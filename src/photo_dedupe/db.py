"""SQLite persistence for scanned photo metadata."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    hash TEXT,
    hashed_at TEXT,
    status TEXT NOT NULL DEFAULT 'present',
    CHECK (status IN ('present', 'missing'))
);

CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
"""


@dataclass(frozen=True)
class FileRecord:
    id: int
    source_id: int
    path: str
    name: str
    size: int
    mtime: float
    hash: str | None
    hashed_at: str | None
    status: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def upsert_source(self, root_path: Path) -> int:
        resolved = str(Path(root_path).resolve())
        row = self._conn.execute(
            "SELECT id FROM sources WHERE root_path = ?", (resolved,)
        ).fetchone()
        if row:
            return int(row["id"])
        cur = self._conn.execute(
            "INSERT INTO sources (root_path, added_at) VALUES (?, ?)",
            (resolved, _utc_now()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def mark_source_files_missing(self, source_id: int) -> None:
        self._conn.execute(
            "UPDATE files SET status = 'missing' WHERE source_id = ?",
            (source_id,),
        )
        self._conn.commit()

    def get_file_by_path(self, path: str) -> FileRecord | None:
        row = self._conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()
        return _row_to_file(row) if row else None

    def upsert_file(
        self,
        *,
        source_id: int,
        path: str,
        name: str,
        size: int,
        mtime: float,
        keep_hash: bool,
    ) -> FileRecord:
        existing = self.get_file_by_path(path)
        if existing and keep_hash:
            self._conn.execute(
                """
                UPDATE files
                SET source_id = ?, name = ?, size = ?, mtime = ?, status = 'present'
                WHERE path = ?
                """,
                (source_id, name, size, mtime, path),
            )
        elif existing:
            self._conn.execute(
                """
                UPDATE files
                SET source_id = ?, name = ?, size = ?, mtime = ?,
                    hash = NULL, hashed_at = NULL, status = 'present'
                WHERE path = ?
                """,
                (source_id, name, size, mtime, path),
            )
        else:
            self._conn.execute(
                """
                INSERT INTO files (source_id, path, name, size, mtime, status)
                VALUES (?, ?, ?, ?, ?, 'present')
                """,
                (source_id, path, name, size, mtime),
            )
        self._conn.commit()
        record = self.get_file_by_path(path)
        assert record is not None
        return record

    def set_hash(self, file_id: int, digest: str) -> None:
        self._conn.execute(
            "UPDATE files SET hash = ?, hashed_at = ? WHERE id = ?",
            (digest, _utc_now(), file_id),
        )
        self._conn.commit()

    def sizes_with_multiple_present(self) -> list[int]:
        rows = self._conn.execute(
            """
            SELECT size FROM files
            WHERE status = 'present'
            GROUP BY size
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        return [int(r["size"]) for r in rows]

    def present_files_without_hash_for_sizes(
        self, sizes: Sequence[int]
    ) -> list[FileRecord]:
        if not sizes:
            return []
        placeholders = ",".join("?" * len(sizes))
        rows = self._conn.execute(
            f"""
            SELECT * FROM files
            WHERE status = 'present'
              AND hash IS NULL
              AND size IN ({placeholders})
            """,
            tuple(sizes),
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    def present_files(self) -> list[FileRecord]:
        rows = self._conn.execute(
            "SELECT * FROM files WHERE status = 'present' ORDER BY path"
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    def present_files_with_hash(self) -> list[FileRecord]:
        rows = self._conn.execute(
            """
            SELECT * FROM files
            WHERE status = 'present' AND hash IS NOT NULL
            ORDER BY hash, path
            """
        ).fetchall()
        return [_row_to_file(r) for r in rows]

    def count_stats(self) -> dict[str, int]:
        present = self._conn.execute(
            "SELECT COUNT(*) AS c FROM files WHERE status = 'present'"
        ).fetchone()["c"]
        missing = self._conn.execute(
            "SELECT COUNT(*) AS c FROM files WHERE status = 'missing'"
        ).fetchone()["c"]
        hashed = self._conn.execute(
            "SELECT COUNT(*) AS c FROM files WHERE status = 'present' AND hash IS NOT NULL"
        ).fetchone()["c"]
        sources = self._conn.execute("SELECT COUNT(*) AS c FROM sources").fetchone()[
            "c"
        ]
        return {
            "sources": int(sources),
            "present": int(present),
            "missing": int(missing),
            "hashed": int(hashed),
        }

    def commit(self) -> None:
        self._conn.commit()


def _row_to_file(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=int(row["id"]),
        source_id=int(row["source_id"]),
        path=str(row["path"]),
        name=str(row["name"]),
        size=int(row["size"]),
        mtime=float(row["mtime"]),
        hash=row["hash"],
        hashed_at=row["hashed_at"],
        status=str(row["status"]),
    )
