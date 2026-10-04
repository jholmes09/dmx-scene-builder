"""Make the desktop-app icons (packaging/icon.png, icon.ico, icon.icns) from web/img/logo.png.
Needs Pillow; the generated files are committed so CI doesn't. Run: python3 tools/make_icons.py"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
logo = Image.open(ROOT / "web/img/logo.png").convert("RGBA")
N = 1024
# macOS-style rounded square (about 22% corner radius, 10% margin) in the brand near-black.
m = 100
tile = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(tile).rounded_rectangle((m, m, N - m, N - m), radius=int((N - 2 * m) * 0.225), fill=(7, 6, 5, 255))
w = int((N - 2 * m) * 0.80)
lg = logo.resize((w, int(w * logo.height / logo.width)), Image.LANCZOS)
tile.alpha_composite(lg, ((N - lg.width) // 2, (N - lg.height) // 2))
out = ROOT / "packaging"
out.mkdir(exist_ok=True)
tile.save(out / "icon.png")
tile.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
tile.save(out / "icon.icns")
print("wrote", [p.name for p in sorted(out.glob("icon.*"))])
