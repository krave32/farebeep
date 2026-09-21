"""Render web/og.png (1200x630) without native cairo.

Windows has no libcairo, so cairosvg cannot run here. The card is simple
geometry + text, so Pillow reproduces it directly from the same palette
and layout constants as web/og.svg. Run whenever web/og.svg changes:

    venv/Scripts/python.exe FareBeep/make_og_png.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WEB = Path(__file__).parent / "web"
W, H = 1200, 630

# Kivi palette (same tokens as styles.css)
PINE = (14, 59, 44)
PINE_DEEP = (10, 45, 33)
INK = (238, 243, 226)
MUTED = (157, 184, 164)
LIME = (216, 233, 91)
LIME_SOFT = (207, 224, 92)
GRID = (29, 80, 64)
WHITE = (234, 244, 224)


def _font(size, bold=False, mono=False):
    """Pick an installed Windows font; fall back to Pillow's default."""
    candidates = []
    if mono:
        candidates = [r"C:\Windows\Fonts\consolab.ttf" if bold
                      else r"C:\Windows\Fonts\consola.ttf"]
    else:
        candidates = ([r"C:\Windows\Fonts\PlusJakartaSans-ExtraBold.ttf",
                       r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf"]
                      if bold else
                      [r"C:\Windows\Fonts\PlusJakartaSans-Regular.ttf",
                       r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"])
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main():
    img = Image.new("RGB", (W, H), PINE)

    # diagonal pine -> deep-pine wash (the og.svg gradient, banded)
    px = img.load()
    for x in range(W):
        for y in range(H):
            t = (x / W + y / H) / 2
            if t > 0.55:
                f = min(1.0, (t - 0.55) / 0.45)
                px[x, y] = tuple(round(a + (b - a) * f)
                                 for a, b in zip(PINE, PINE_DEEP))

    d = ImageDraw.Draw(img)

    # faint hero grid
    for gx in range(100, W, 150):
        d.line([(gx, 0), (gx, H)], fill=GRID, width=1)
    for gy in range(90, H, 120):
        d.line([(0, gy), (W, gy)], fill=GRID, width=1)

    # mark (restored original): two radar arcs + beam + plane + ping dot, at 2.3x
    ox, oy = 90, 205
    S = 2.3
    # Both arcs share center (40,52) and open to 3 o'clock (gap = 315..45 deg:
    # from (61.2,30.8) A30 30 0 1 0 (61.2,73.2), the long way around the left).
    # Pillow angles: 0=3 o'clock, increasing clockwise; arc draws start->end.
    d.arc([ox + (40 - 30) * S, oy + (52 - 30) * S,
           ox + (40 + 30) * S, oy + (52 + 30) * S], start=45, end=315,
          fill=LIME, width=18)
    d.arc([ox + (40 - 18) * S, oy + (52 - 18) * S,
           ox + (40 + 18) * S, oy + (52 + 18) * S], start=45, end=315,
          fill=LIME, width=18)
    # beam: (30,76)->(74,30)
    d.line([(ox + 30 * S, oy + 76 * S), (ox + 74 * S, oy + 30 * S)],
           fill=LIME, width=14)
    # plane with pine outline so it stays crisp over the beam
    pts = [(ox + 34 * S, oy + 51 * S), (ox + 60 * S, oy + 42 * S),
           (ox + 46 * S, oy + 58 * S), (ox + 44 * S, oy + 52 * S)]
    d.line(pts + [pts[0]], fill=PINE, width=7, joint="curve")
    d.polygon(pts, fill=WHITE)
    # ping dot at (80,24) r=9
    d.ellipse([ox + (80 - 9) * S, oy + (24 - 9) * S,
               ox + (80 + 9) * S, oy + (24 + 9) * S], fill=LIME)

    # kicker
    d.text((480, 205), "NIGERIA DOMESTIC FLIGHTS · IN CHAT",
           font=_font(24, mono=True), fill=MUTED)

    # headline
    d.text((480, 262), "Say the route.",
           font=_font(76, bold=True), fill=INK)

    # highlight box + second line
    d.rectangle([476, 344, 476 + 560, 440], fill=LIME)
    d.text((502, 358), "Lock the fare.",
           font=_font(76, bold=True), fill=PINE)

    # bot handle
    d.text((480, 495), "@FareBeep_bot",
           font=_font(28, bold=True, mono=True), fill=LIME_SOFT)

    img.save(WEB / "og.png", "PNG", optimize=True)
    print(f"wrote {WEB / 'og.png'} ({(WEB / 'og.png').stat().st_size} bytes)")


if __name__ == "__main__":
    main()
