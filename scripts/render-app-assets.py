"""Render WinDictate app icons (neutral, no brand mark).

One simple microphone glyph, two colour variants for the dark and light themes, a
transparent tray icon and a multi-size Windows .ico (mic on a deep ink rounded square).
Rasterised with the installed Edge/Chrome in headless mode, composed with Pillow.

Run: python scripts/render-app-assets.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

INK_950 = (20, 18, 15, 255)  # #14120F — тёмная полоса канона СТОРИАРТ

SVG = """<svg width="512" height="512" viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
  <rect x="18" y="5" width="12" height="22" rx="6" fill="{accent}"/>
  <path d="M12 21v2c0 6.627 5.373 12 12 12s12-5.373 12-12v-2" stroke="{accent}" stroke-width="3" stroke-linecap="round"/>
  <path d="M24 35v7" stroke="{accent}" stroke-width="3" stroke-linecap="round"/>
  <path d="M17 42h14" stroke="{accent}" stroke-width="3" stroke-linecap="round"/>
</svg>
"""


def find_browser() -> str:
    for path in BROWSERS:
        if Path(path).is_file():
            return path
    raise SystemExit("Edge/Chrome not found; cannot rasterise the icon")


def render(browser: str, svg_text: str, out: Path, size: int = 512) -> Image.Image:
    svg_path = out.with_suffix(".svg")
    svg_path.write_text(svg_text, encoding="utf-8")
    if out.exists():
        out.unlink()
    subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={size},{size}",
            f"--screenshot={out}",
            svg_path.as_uri(),
        ],
        capture_output=True,
        timeout=120,
    )
    if not out.is_file() or out.stat().st_size == 0:
        raise SystemExit(f"render failed: {out}")
    image = Image.open(out).convert("RGBA")
    if image.size != (size, size):
        image = image.resize((size, size), Image.LANCZOS)
    return image


def compose_icon(mic: Image.Image, out_ico: Path, out_png: Path) -> None:
    canvas_size = 256
    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, canvas_size - 1, canvas_size - 1), radius=0, fill=INK_950
    )
    inner = int(canvas_size * 0.66)
    glyph = mic.resize((inner, inner), Image.LANCZOS)
    offset = (canvas_size - inner) // 2
    canvas.alpha_composite(glyph, (offset, offset))
    canvas.save(out_png)
    canvas.save(out_ico, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


def main() -> int:
    browser = find_browser()
    ICONS.mkdir(parents=True, exist_ok=True)
    scene = render(browser, SVG.format(accent="#C0603C"), ICONS / "mic-scene.png")
    render(browser, SVG.format(accent="#A45536"), ICONS / "mic-paper.png")
    compose_icon(scene, ICONS / "windictate.ico", ICONS / "windictate-tray-64.png")
    print("rendered mic-scene.png, mic-paper.png, windictate.ico, windictate-tray-64.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
