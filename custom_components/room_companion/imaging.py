"""Image processing for the panels, free of Home Assistant imports.

Everything here is CPU work: callers run it in the executor. Outputs are sized
for the panel so it only decodes (JPEG/PNG) or copies (raw formats) them.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from functools import lru_cache
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

MAX_SIDE = 960

# Bundled fallbacks for characters the panel fonts lack (SIL OFL 1.1, see fonts/):
# Latin, Greek and Cyrillic; Chinese and Japanese kana; Korean.
FONT_DIR = Path(__file__).parent / "fonts"
BUNDLED_FONTS = [
    str(FONT_DIR / name)
    for name in ("NotoSans-Regular.ttf", "NotoSansSC-Regular.otf", "NotoSansKR-Regular.otf")
]
FORMATS = ("jpeg", "png", "rgb565", "bgra")


@dataclass(frozen=True)
class Encoded:
    """An encoded image and its content type."""

    data: bytes
    content_type: str
    width: int
    height: int


def clamp(value: int, low: int, high: int) -> int:
    """Clamp an integer."""
    return max(low, min(high, value))


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    # The first frame of animated images.
    image.seek(0)
    return image.convert("RGBA")


def rgb565(image: Image.Image, swap: bool = False) -> bytes:
    """Pack an image as RGB565, little endian (LVGL's native order) or swapped."""
    rgb = image.convert("RGB").tobytes()
    out = bytearray(len(rgb) // 3 * 2)
    j = 0
    for i in range(0, len(rgb), 3):
        value = ((rgb[i] & 0xF8) << 8) | ((rgb[i + 1] & 0xFC) << 3) | (rgb[i + 2] >> 3)
        if swap:
            out[j] = value >> 8
            out[j + 1] = value & 0xFF
        else:
            out[j] = value & 0xFF
            out[j + 1] = value >> 8
        j += 2
    return bytes(out)


def bgra(image: Image.Image) -> bytes:
    """Pack an image as BGRA (LVGL ARGB8888), straight alpha."""
    return image.convert("RGBA").tobytes("raw", "BGRA")


def encode(image: Image.Image, fmt: str, quality: int = 85, swap: bool = False) -> Encoded:
    """Encode an image in one of FORMATS."""
    width, height = image.size
    if fmt == "rgb565":
        return Encoded(rgb565(image, swap), "application/octet-stream", width, height)
    if fmt == "bgra":
        return Encoded(bgra(image), "application/octet-stream", width, height)
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, "PNG", optimize=True)
        return Encoded(buffer.getvalue(), "image/png", width, height)
    # Baseline JPEG: progressive JPEG is slow or unsupported on the panel decoder.
    flat = Image.new("RGB", image.size, (0, 0, 0))
    flat.paste(image, mask=image.getchannel("A") if image.mode == "RGBA" else None)
    flat.save(buffer, "JPEG", quality=clamp(quality, 30, 95), progressive=False, optimize=True)
    return Encoded(buffer.getvalue(), "image/jpeg", width, height)


def cover(data: bytes, width: int, height: int, fmt: str = "jpeg", quality: int = 85,
          swap: bool = False) -> Encoded:
    """Album art cropped to fill width x height."""
    image = _open(data)
    image = ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
    return encode(image, fmt, quality, swap)


def backdrop(data: bytes, width: int, height: int, dim: float = 0.62, fmt: str = "jpeg",
             quality: int = 80, swap: bool = False) -> Encoded:
    """The lyrics backdrop: the art filling the screen, heavily blurred and darkened.

    Matches the firmware's own backdrop (reduce to 32 px, blur, enlarge, darken),
    with a smoother Gaussian blur at full resolution.
    """
    image = _open(data).convert("RGB")
    small = ImageOps.fit(image, (32, max(1, round(32 * height / width))), Image.Resampling.BOX)
    small = small.filter(ImageFilter.GaussianBlur(3))
    large = small.resize((width, height), Image.Resampling.BICUBIC)
    large = large.filter(ImageFilter.GaussianBlur(max(width, height) / 60))
    large = ImageEnhance.Brightness(large).enhance(max(0.1, min(1.0, dim)))
    return encode(large.convert("RGBA"), fmt, quality, swap)


def contain(data: bytes, max_width: int, max_height: int, fmt: str = "png",
            quality: int = 85, swap: bool = False) -> Encoded:
    """Shrink (never enlarge) to fit inside max_width x max_height."""
    image = _open(data)
    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return encode(image, fmt, quality, swap)


def square(data: bytes, size: int, fmt: str = "png") -> Encoded:
    """Fit an icon or emoji inside a transparent size x size square."""
    image = _open(data)
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2), image)
    return encode(canvas, fmt)


@dataclass(frozen=True)
class Glyph:
    """A rendered glyph: an 8-bit alpha bitmap plus its metrics."""

    data: bytes
    content_type: str
    width: int
    height: int
    advance: int
    ascent: int


@lru_cache(maxsize=32)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _draw(font: ImageFont.FreeTypeFont, text: str) -> Image.Image:
    left, top, right, bottom = font.getbbox(text)
    image = Image.new("L", (max(1, right - left), max(1, bottom - top)), 0)
    ImageDraw.Draw(image).text((-left, -top), text, fill=255, font=font)
    return image


@lru_cache(maxsize=32)
def _notdef(path: str, size: int) -> tuple[tuple[int, int], bytes]:
    """What the font draws for a character it lacks (its .notdef box)."""
    image = _draw(_font(path, size), "\U000f0000")
    return image.size, image.tobytes()


def has_glyph(path: str, text: str, size: int) -> bool:
    """Whether the font has its own glyph for text (not the .notdef box)."""
    font = _font(path, size)
    if text.isspace():
        return font.getlength(text) > 0
    image = _draw(font, text)
    return image.getbbox() is not None and (image.size, image.tobytes()) != _notdef(path, size)


def glyph(fonts: list[str], text: str, size: int, fmt: str = "a8") -> Glyph | None:
    """Render text (normally one character) in white, for characters the panel fonts lack.

    Uses the first font in fonts that has the character. The bitmap is the
    font's full line height so glyphs share a baseline (ascent pixels from the
    top). Returns None when no font has it.
    """
    for path in fonts:
        if has_glyph(path, text, size):
            break
    else:
        return None
    font = _font(path, size)
    ascent, descent = font.getmetrics()
    advance = round(font.getlength(text))
    if advance <= 0:
        return None
    height = ascent + descent
    mask = Image.new("L", (advance, height), 0)
    ImageDraw.Draw(mask).text((0, 0), text, fill=255, font=font)
    if fmt == "png":
        image = Image.new("RGBA", mask.size, (255, 255, 255, 0))
        image.putalpha(mask)
        buffer = io.BytesIO()
        image.save(buffer, "PNG", optimize=True)
        return Glyph(buffer.getvalue(), "image/png", advance, height, advance, ascent)
    data = mask.tobytes()
    if fmt == "a8h":
        # For the firmware: width, height, advance and ascent (uint16 little
        # endian) before the 8-bit alpha rows, so no response headers are needed.
        data = struct.pack("<HHHH", advance, height, advance, ascent) + data
    return Glyph(data, "application/octet-stream", advance, height, advance, ascent)
