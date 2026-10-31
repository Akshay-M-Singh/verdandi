"""Derive the desktop branding assets from the canonical logo source.

`verdandi-logo.png` is the owner's final lockup (1254x1254, near-black tile):
the gold mark with the VERDANDI wordmark beneath it. This script regenerates
every derived file from that source, so swapping the artwork is one command:

    .venv/bin/python desktop/branding/make_assets.py

Outputs (paths relative to the repo root):
  desktop/app-icon.png                mark on the near-black tile, 1024x1024
  desktop/public/favicon.png          the same tile, 128x128
  desktop/src/assets/logo-lockup.png  mark + wordmark, transparent, trimmed
  desktop/src-tauri/icons/*           regenerate separately: npx tauri icon app-icon.png

The tile background is removed with a soft alpha ramp (near-black becomes
transparent) so the mark sits cleanly on the app's dark surfaces without a
visible tile edge; the RGB of the gold is untouched.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SRC = HERE / "verdandi-logo.png"

TILE = (4, 4, 4)        # the source's near-black tile colour
ALPHA_FLOOR = 16        # max(rgb) <= floor -> fully transparent
ALPHA_CEIL = 64         # max(rgb) >= ceil -> fully opaque
MARK_FRACTION = 0.72    # mark size relative to the icon tile


def _maxc(p: tuple) -> int:
    return max(p[0], p[1], p[2])


def _bands(im: Image.Image, min_height: int = 4) -> list[tuple[int, int]]:
    """(start, end) y-ranges of non-background content, top to bottom.

    Single-pixel bands are compression noise around the artwork — dropped."""
    px = im.load()
    w, h = im.size
    rows = [any(_maxc(px[x, y]) > ALPHA_FLOOR for x in range(0, w, 3))
            for y in range(h)]
    bands: list[tuple[int, int]] = []
    start = None
    for y, found in enumerate(rows):
        if found and start is None:
            start = y
        elif not found and start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, h - 1))
    return [b for b in bands if b[1] - b[0] + 1 >= min_height]


def _bbox(im: Image.Image, y0: int, y1: int) -> tuple[int, int, int, int]:
    px = im.load()
    w = im.size[0]
    xs: list[int] = []
    ys: list[int] = []
    for y in range(y0, y1 + 1):
        for x in range(w):
            if _maxc(px[x, y]) > ALPHA_FLOOR:
                xs.append(x)
                ys.append(y)
    if not xs:
        raise SystemExit(f"no content in rows {y0}-{y1}")
    return min(xs), min(ys), max(xs) + 1, max(ys) + 1


def _transparent(crop: Image.Image) -> Image.Image:
    """Near-black -> alpha 0 (soft ramp); keeps the gold RGB untouched."""
    out = crop.convert("RGBA")
    px = out.load()
    for y in range(out.size[1]):
        for x in range(out.size[0]):
            r, g, b, a = px[x, y]
            m = max(r, g, b)
            if m <= ALPHA_FLOOR:
                px[x, y] = (r, g, b, 0)
            elif m < ALPHA_CEIL:
                ramp = (m - ALPHA_FLOOR) / (ALPHA_CEIL - ALPHA_FLOOR)
                px[x, y] = (r, g, b, int(a * ramp))
    return out


def _square(im: Image.Image, pad: float = 0.06) -> Image.Image:
    """Pad into a transparent square canvas (uniform margin)."""
    side = int(max(im.size) * (1 + 2 * pad))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.size[0]) // 2, (side - im.size[1]) // 2), im)
    return canvas


def _tile(mark: Image.Image, size: int) -> Image.Image:
    """The mark centered on the source's near-black tile."""
    tile = Image.new("RGBA", (size, size), (*TILE, 255))
    target = int(size * MARK_FRACTION)
    scale = target / max(mark.size)
    scaled = mark.resize((max(1, round(mark.size[0] * scale)),
                          max(1, round(mark.size[1] * scale))), Image.LANCZOS)
    tile.paste(scaled, ((size - scaled.size[0]) // 2,
                        (size - scaled.size[1]) // 2), scaled)
    return tile


def main() -> None:
    src = Image.open(SRC).convert("RGBA")
    bands = _bands(src)
    if len(bands) < 2:
        raise SystemExit(f"expected a mark + wordmark in {SRC}, found bands {bands}")
    mark = _transparent(src.crop(_bbox(src, *bands[0])))
    lockup = _transparent(src.crop(_bbox(src, bands[0][0], bands[-1][1])))
    mark_sq = _square(mark)
    lockup_sq = _square(lockup, pad=0.03)

    outputs = {
        ROOT / "desktop" / "app-icon.png": _tile(mark_sq, 1024),
        ROOT / "desktop" / "public" / "favicon.png": _tile(mark_sq, 128),
        ROOT / "desktop" / "src" / "assets" / "logo-lockup.png": lockup_sq,
    }
    for path, im in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        im.save(path)
        print(f"{path.relative_to(ROOT)}  {im.size[0]}x{im.size[1]}")


if __name__ == "__main__":
    main()
