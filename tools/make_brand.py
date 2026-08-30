"""Generate the in-repo brand images.

Since Home Assistant 2026.3 a custom integration carries its own brand images
and no pull request against `home-assistant/brands` is needed. HACS reads
`custom_components/<domain>/brand/` first and falls back to the brands
repository only when the in-repo icon is missing.

Sizes are exact requirements, not suggestions:

    icon.png       256x256 exactly
    icon@2x.png    512x512 exactly
    logo.png       shortest side 128-256
    logo@2x.png    shortest side 256-512

The mark is a load trace: a flat baseline, a step up to a plateau, a spike, and
a step back down - the shape this integration exists to recognise. The two
dotted rules mark the floor and the peak, which are the features that actually
identify an appliance.
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

BG = (14, 22, 33, 255)
TRACE = (94, 200, 245, 255)
FLOOR = (245, 176, 66, 255)
PEAK = (120, 130, 150, 255)


def _trace_points(w: int, h: int) -> list[tuple[float, float]]:
    """A stylised appliance run, in fractions of the canvas."""
    pts = [
        (0.00, 0.80),
        (0.14, 0.80),  # idle
        (0.14, 0.52),
        (0.34, 0.52),  # step up to the plateau
        (0.34, 0.20),
        (0.44, 0.20),  # spike
        (0.44, 0.52),
        (0.72, 0.52),  # back to the plateau
        (0.72, 0.80),
        (1.00, 0.80),  # switch off
    ]
    return [(x * w, y * h) for x, y in pts]


def draw(size: tuple[int, int], pad_frac: float = 0.14) -> Image.Image:
    w, h = size
    img = Image.new("RGBA", size, BG)
    d = ImageDraw.Draw(img)

    pad_x, pad_y = w * pad_frac, h * pad_frac
    iw, ih = w - 2 * pad_x, h - 2 * pad_y
    pts = [(pad_x + x, pad_y + y) for x, y in _trace_points(iw, ih)]

    line = max(2, int(min(w, h) * 0.055))
    dash = max(1, line // 2)

    # floor and peak rules - the two features that identify a machine
    for frac, colour in ((0.52, FLOOR), (0.20, PEAK)):
        y = pad_y + ih * frac
        x = pad_x
        while x < pad_x + iw:
            x2 = min(x + dash * 3, pad_x + iw)
            d.line([(x, y), (x2, y)], fill=colour, width=dash)
            x += dash * 5

    d.line(pts, fill=TRACE, width=line, joint="curve")
    return img


def main() -> None:
    out = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "custom_components",
        "power_fingerprint",
        "brand",
    )
    os.makedirs(out, exist_ok=True)
    specs = {
        "icon.png": (256, 256),
        "icon@2x.png": (512, 512),
        "logo.png": (512, 256),
        "logo@2x.png": (1024, 512),
    }
    for name, size in specs.items():
        path = os.path.join(out, name)
        draw(size, pad_frac=0.14 if "icon" in name else 0.10).save(path, "PNG")
        print(f"  {name:14s} {size[0]}x{size[1]}")

    # Verify against the published rules rather than trusting the call above.
    ok = True
    for name, (want_w, want_h) in specs.items():
        with Image.open(os.path.join(out, name)) as im:
            w, h = im.size
        if name.startswith("icon"):
            good = (w, h) == (want_w, want_h) and w == h
        else:
            short = min(w, h)
            good = (256 <= short <= 512) if "@2x" in name else (128 <= short <= 256)
        print(f"  check {name:14s} {w}x{h} {'OK' if good else 'FAILS THE RULE'}")
        ok &= good
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
