#!/usr/bin/env python3
"""Generate the flat PNG icons the Office manifest requires (no dependencies)."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "addin" / "assets"
SIZES = (16, 32, 64, 80, 128)
BG = (107, 68, 35)  # terracotta-brown, matches the task pane accent
FG = (245, 243, 240)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def make_png(size: int) -> bytes:
    """Rounded-ish square with a white check mark."""
    pad = max(1, size // 8)
    bar = max(1, size // 8)

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type 0
        for x in range(size):
            r, g, b = BG
            # Check mark: short down-right stroke, then long up-right stroke.
            dx1 = x - (pad + size * 0.10)
            dy1 = y - (size * 0.55)
            on_short = abs(dy1 - dx1) < bar and pad <= x <= size * 0.45
            dx2 = x - (size * 0.45)
            dy2 = y - (size * 0.70)
            on_long = abs(dy2 + dx2) < bar and size * 0.40 <= x <= size - pad
            if on_short or on_long:
                r, g, b = FG
            rows.extend((r, g, b))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        path = OUT / f"icon-{size}.png"
        path.write_bytes(make_png(size))
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
