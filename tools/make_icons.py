"""Make every app icon from the real gold logo: the iPad icon (full-bleed 1024, iPadOS rounds it)
and the desktop icons (packaging/icon.png, .ico, .icns as a rounded tile).
Brand: gold logo on near-black, showbill double rule, "DMX" in Cinzel.
Needs Pillow; the generated files are committed so CI doesn't. Run: python3 tools/make_icons.py"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "tools/brand"
BG, GOLD = (7, 6, 5), (212, 168, 74)
S = 1024
R = 0.2237 * S  # iPadOS/macOS icon corner radius


def art() -> Image.Image:
    im = Image.new("RGB", (S, S), BG)
    d = ImageDraw.Draw(im)
    for inset, w in ((40, 6), (60, 2)):  # double rule, rounded to sit inside the system's corner mask
        d.rounded_rectangle([inset, inset, S - 1 - inset, S - 1 - inset], radius=int(R - inset * 0.75), outline=GOLD, width=w)
    logo = Image.open(BRAND / "logo_gold.png").convert("RGBA")
    logo = logo.crop(logo.getbbox())
    tw = 740
    logo = logo.resize((tw, int(logo.height * tw / logo.width)), Image.LANCZOS)
    top = 160
    im.paste(logo, ((S - tw) // 2, top), logo)
    f = ImageFont.truetype(str(BRAND / "cinzel-latin-700-normal.woff"), 140)
    w = d.textlength("DMX", font=f)
    d.text(((S - w) / 2, top + logo.height + 38), "DMX", font=f, fill=GOLD)
    return im


a = art()
a.save(ROOT / "ios/App/Assets.xcassets/AppIcon.appiconset/icon-1024.png")  # no alpha: App Store rule

# Desktop: the same art as a rounded tile with the usual 10% margin.
m = 100
inner = S - 2 * m
small = a.resize((inner, inner), Image.LANCZOS)
mask = Image.new("L", (inner, inner), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, inner - 1, inner - 1], radius=int(inner * 0.2237), fill=255)
tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
tile.paste(small, (m, m), mask)
out = ROOT / "packaging"
out.mkdir(exist_ok=True)
tile.save(out / "icon.png")
tile.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
tile.save(out / "icon.icns")
print("wrote iPad icon and", [p.name for p in sorted(out.glob("icon.*"))])
