"""Export package icons from the unchanged official CloudSaver logo.

Normal exports use the committed, transparent raster of the official SVG and
need only Pillow. To reproduce that raster with the documented browser:

    python tools/make_icon.py --render-source --browser-path PATH_TO_MSEDGE

The optional rasterization step also needs Playwright. It never downloads
assets or changes the official artwork. See assets/official-logo-source.json
and THIRD_PARTY_NOTICES.md for provenance and the upstream license.
"""

import argparse
import hashlib
import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SVG_SHA256 = "532ff4db0324e75112098c1259dee2ed5ed641d6c7cfde9791a60d1dc6b7baa4"
PNG_SHA256 = "9ba94ee049a0e9d07ee2480aca30c83b28b72888c0cdbae74d15ad3c1e40c045"
RASTER_SIZE = 2048
ICON_SIZES = (64, 256, 512)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def checked_bytes(path: Path, expected: str) -> bytes:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Official logo checksum mismatch: {path.name}")
    return data


def render_source(browser_path: str | None) -> None:
    """Optionally reproduce the checked-in raster; refuse differing output."""
    from playwright.sync_api import sync_playwright

    svg = checked_bytes(ASSETS / "official-logo.svg", SVG_SHA256)
    with sync_playwright() as playwright:
        options = {"headless": True, "args": ["--force-color-profile=srgb"]}
        if browser_path:
            options["executable_path"] = browser_path
        else:
            options["channel"] = "msedge"
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page(
                viewport={"width": RASTER_SIZE, "height": RASTER_SIZE},
                device_scale_factor=1,
            )
            page.set_content(
                '<html><body style="margin:0;background:transparent">'
                + svg.decode("utf-8")
                + "</body></html>"
            )
            page.locator("svg").evaluate(
                "(e) => {e.setAttribute('width', '2048');"
                "e.setAttribute('height', '2048')}"
            )
            screenshot = page.locator("svg").screenshot(omit_background=True)
            renderer = browser.version
        finally:
            browser.close()
    buffer = io.BytesIO()
    with Image.open(io.BytesIO(screenshot)) as image:
        image.convert("RGBA").save(buffer, format="PNG", optimize=True)
    raster = buffer.getvalue()
    actual = hashlib.sha256(raster).hexdigest()
    if actual != PNG_SHA256:
        raise ValueError(
            f"Raster differs with browser {renderer}: {actual}. "
            "Use the committed raster or the renderer/Pillow versions recorded "
            "in assets/official-logo-source.json."
        )
    (ASSETS / "official-logo.png").write_bytes(raster)


def load_official_logo() -> Image.Image:
    checked_bytes(ASSETS / "official-logo.svg", SVG_SHA256)
    data = checked_bytes(ASSETS / "official-logo.png", PNG_SHA256)
    with Image.open(io.BytesIO(data)) as source:
        if source.size != (RASTER_SIZE, RASTER_SIZE) or source.mode != "RGBA":
            raise ValueError("Expected the transparent 2048px official raster")
        return source.copy()


def create_icon(size: int) -> Image.Image:
    """Preserve the official square canvas, transparency, shape and colors."""
    if size <= 0:
        raise ValueError("Icon size must be positive")
    return load_official_logo().resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-source", action="store_true")
    parser.add_argument("--browser-path")
    args = parser.parse_args()
    if args.browser_path and not args.render_source:
        parser.error("--browser-path requires --render-source")
    if args.render_source:
        render_source(args.browser_path)
    source = load_official_logo()
    for size in ICON_SIZES:
        source.resize((size, size), Image.Resampling.LANCZOS).save(
            ASSETS / f"icon_{size}.png", optimize=True
        )
    source.save(
        ASSETS / "CloudSaver-For-FNOS.ico",
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
    )
    print("Official CloudSaver logo exported: 64/256/512px PNG and Windows ICO")


if __name__ == "__main__":
    main()
