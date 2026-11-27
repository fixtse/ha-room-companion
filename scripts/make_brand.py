"""Draw the brand icons from the panel favicon (branding_assets.h PANEL_FAVICON).

The favicon is a 64x64 SVG of rounded rectangles; this redraws it with Pillow
at 4x supersampling. Run: python scripts/make_brand.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

# (x, y, w, h, rx, fill, stroke, stroke width) in the favicon's 64-unit viewBox.
RECTS = [
    (4, 10, 56, 44, 8, "#0f172a", "#64748b", 2),
    (10, 16, 44, 32, 3, "#1e293b", None, 0),
    (14, 20, 16, 11, 2, "#f97316", None, 0),
    (34, 20, 16, 11, 2, "#f8fafc", None, 0),
    (14, 34, 16, 10, 2, "#f8fafc", None, 0),
    (34, 34, 16, 10, 2, "#f8fafc", None, 0),
]


def draw(size: int) -> Image.Image:
    scale = size * 4 / 64
    image = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    for x, y, w, h, rx, fill, stroke, width in RECTS:
        half = width / 2
        box = [(x - half) * scale, (y - half) * scale, (x + w + half) * scale, (y + h + half) * scale]
        if stroke:
            pen.rounded_rectangle(box, radius=(rx + half) * scale, fill=stroke)
            box = [(x + half) * scale, (y + half) * scale, (x + w - half) * scale, (y + h - half) * scale]
            pen.rounded_rectangle(box, radius=(rx - half) * scale, fill=fill)
        else:
            pen.rounded_rectangle(box, radius=rx * scale, fill=fill)
    return image.resize((size, size), Image.Resampling.LANCZOS)


root = Path(__file__).resolve().parent.parent
brand = root / "custom_components" / "room_companion" / "brand"
brand.mkdir(parents=True, exist_ok=True)
draw(256).save(brand / "icon.png", optimize=True)
draw(512).save(brand / "icon@2x.png", optimize=True)
draw(256).save(root / "icon.png", optimize=True)
