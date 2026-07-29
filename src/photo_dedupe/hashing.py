"""Content hashing for photo files."""

from __future__ import annotations

from pathlib import Path

_CHUNK = 1024 * 1024

try:
    import blake3

    def hash_file(path: Path) -> str:
        hasher = blake3.blake3()
        with Path(path).open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    HASH_ALGO = "blake3"
except ImportError:
    import hashlib

    def hash_file(path: Path) -> str:
        hasher = hashlib.sha256()
        with Path(path).open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    HASH_ALGO = "sha256"
