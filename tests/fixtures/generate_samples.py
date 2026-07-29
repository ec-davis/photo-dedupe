"""Generate tiny sample images for manual smoke tests."""

from pathlib import Path

# Re-use the same PNG builder as unit tests when run as a script.
import struct
import zlib


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def make_png(rgb: tuple[int, int, int]) -> bytes:
    r, g, b = rgb
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = bytes([0, r, g, b])
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


def main() -> None:
    root = Path(__file__).resolve().parent
    (root / "dup_a").mkdir(parents=True, exist_ok=True)
    (root / "dup_b").mkdir(parents=True, exist_ok=True)
    red = make_png((255, 0, 0))
    blue = make_png((0, 0, 255))
    (root / "dup_a" / "sunset.png").write_bytes(red)
    (root / "dup_b" / "sunset_copy.png").write_bytes(red)
    (root / "unique.png").write_bytes(blue)
    print(f"Wrote fixtures under {root}")


if __name__ == "__main__":
    main()
