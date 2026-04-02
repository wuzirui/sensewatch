"""Menu bar icon generation and loading.

Generates a stylized eye-with-GPU-chip icon as a macOS template image.
Template images must be black on transparent — macOS auto-adapts to dark/light.
"""

from __future__ import annotations

from pathlib import Path

RESOURCES_DIR = Path(__file__).parent / "resources"
ICON_PATH = RESOURCES_DIR / "icon_template.png"


def get_icon_path() -> str:
    """Return path to the menu bar icon, generating it if needed."""
    if not ICON_PATH.exists():
        _generate_icon()
    return str(ICON_PATH)


def _generate_icon() -> None:
    """Generate the eye-chip icon programmatically using Pillow.

    Design: a stylized eye (almond shape) with a small GPU chip as the pupil.
    32x32, black on transparent, suitable as a macOS template image.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        # If Pillow not available, create a minimal fallback icon
        _generate_fallback_icon()
        return

    size = 32
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Colors
    black = (0, 0, 0, 220)

    # ── Eye outline (almond shape) ──
    # Upper arc
    draw.arc([3, 6, 29, 26], start=200, end=340, fill=black, width=2)
    # Lower arc
    draw.arc([3, 6, 29, 26], start=20, end=160, fill=black, width=2)

    # ── GPU chip (pupil) — small rectangle with pins ──
    # Main chip body
    chip_x, chip_y = 12, 12
    chip_w, chip_h = 8, 8
    draw.rectangle(
        [chip_x, chip_y, chip_x + chip_w, chip_y + chip_h],
        fill=black,
    )

    # Pins — 2 on each side
    pin_len = 3
    pin_w = 1
    # Top pins
    for px in [chip_x + 2, chip_x + chip_w - 3]:
        draw.line([(px, chip_y - pin_len), (px, chip_y)], fill=black, width=pin_w)
    # Bottom pins
    for px in [chip_x + 2, chip_x + chip_w - 3]:
        draw.line([(px, chip_y + chip_h), (px, chip_y + chip_h + pin_len)], fill=black, width=pin_w)
    # Left pins
    for py in [chip_y + 2, chip_y + chip_h - 3]:
        draw.line([(chip_x - pin_len, py), (chip_x, py)], fill=black, width=pin_w)
    # Right pins
    for py in [chip_y + 2, chip_y + chip_h - 3]:
        draw.line([(chip_x + chip_w, py), (chip_x + chip_w + pin_len, py)], fill=black, width=pin_w)

    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    img.save(str(ICON_PATH), "PNG")


def _generate_fallback_icon() -> None:
    """Create a minimal 32x32 PNG without Pillow (single black pixel pattern)."""
    import struct
    import zlib

    size = 32
    # Build a minimal RGBA PNG with a simple eye shape
    # For the fallback, just create a solid small circle
    rows = []
    for y in range(size):
        row = b""
        for x in range(size):
            # Simple circle at center
            dx = x - size // 2
            dy = y - size // 2
            dist = (dx * dx + dy * dy) ** 0.5
            if 8 <= dist <= 12:
                row += b"\x00\x00\x00\xd0"  # black, mostly opaque
            elif dist < 5:
                row += b"\x00\x00\x00\xd0"  # center dot
            else:
                row += b"\x00\x00\x00\x00"  # transparent
        rows.append(b"\x00" + row)  # filter byte + row data

    raw = b"".join(rows)

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + c + crc

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", zlib.compress(raw))
    png += _chunk(b"IEND", b"")

    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    ICON_PATH.write_bytes(png)
