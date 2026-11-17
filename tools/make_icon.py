"""Render the wrapper's original cloud icon; requires Pillow at build time only."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def create_icon(size: int) -> Image.Image:
    scale = 4
    image = Image.new("RGBA", (256 * scale, 256 * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    def box(values):
        return tuple(int(value * scale) for value in values)
    draw.rounded_rectangle(box((8, 8, 248, 248)), radius=54 * scale, fill="#edf6ff")
    blue = "#1597ed"
    draw.ellipse(box((42, 95, 121, 174)), fill=blue)
    draw.ellipse(box((76, 57, 176, 157)), fill=blue)
    draw.ellipse(box((139, 91, 216, 168)), fill=blue)
    draw.rounded_rectangle(box((53, 116, 207, 181)), radius=30 * scale, fill=blue)
    draw.line([(128 * scale, 104 * scale), (128 * scale, 159 * scale)], fill="white", width=15 * scale)
    draw.line([(103 * scale, 139 * scale), (128 * scale, 164 * scale), (153 * scale, 139 * scale)],
              fill="white", width=15 * scale, joint="curve")
    return image.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    target = ROOT / "assets"
    target.mkdir(exist_ok=True)
    for size in (64, 256):
        create_icon(size).save(target / f"icon_{size}.png", optimize=True)
    print("CloudSaver icon assets rendered")
