"""Minimal PNG helpers for tests."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def make_png(rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    r, g, b = rgb
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = bytes([0, r, g, b])
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


PNG_RED = make_png((255, 0, 0))
PNG_BLUE = make_png((0, 0, 255))


def write_png(path: Path, rgb: tuple[int, int, int] = (255, 0, 0)) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_png(rgb))
    return path
