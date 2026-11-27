"""Image processing."""

import io

from PIL import Image

from custom_components.room_companion import imaging


def _png(size=(200, 100), color=(255, 0, 0, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def test_cover_fills_box() -> None:
    out = imaging.cover(_png(), 96, 96, "jpeg")
    assert out.content_type == "image/jpeg"
    assert Image.open(io.BytesIO(out.data)).size == (96, 96)


def test_rgb565_order() -> None:
    red = Image.new("RGB", (1, 1), (255, 0, 0))
    assert imaging.rgb565(red) == bytes([0x00, 0xF8])
    assert imaging.rgb565(red, swap=True) == bytes([0xF8, 0x00])
    out = imaging.cover(_png(), 20, 10, "rgb565")
    assert len(out.data) == 20 * 10 * 2


def test_backdrop_is_dark_and_sized() -> None:
    out = imaging.backdrop(_png(color=(200, 200, 200, 255)), 320, 480, dim=0.5, fmt="png")
    image = Image.open(io.BytesIO(out.data)).convert("RGB")
    assert image.size == (320, 480)
    assert max(image.getpixel((160, 240))) <= 110


def test_square_bgra() -> None:
    out = imaging.square(_png((128, 128), (0, 0, 255, 255)), 48, "bgra")
    assert len(out.data) == 48 * 48 * 4
    assert out.data[:4] == bytes([255, 0, 0, 255])  # Blue, green, red, alpha.


def test_contain_never_enlarges() -> None:
    out = imaging.contain(_png((50, 40)), 240, 240)
    assert (out.width, out.height) == (50, 40)
