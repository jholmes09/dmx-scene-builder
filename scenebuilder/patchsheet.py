"""Printable per-float patch sheet (light theme for paper)."""
from __future__ import annotations

import html
import time

from . import fixtures
from .store import effective_variant, fixture_footprint, patch_problems


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def describe_state(fx: dict, st: dict, cal=None) -> str:
    v = effective_variant(fx)
    return fixtures.describe_mix(v, fx["mode"], st or {}, cal if v == "RGBW" else None)


def render_patch_sheet(project: str, fl: dict, cal=None) -> str:
    boxes = {b["id"]: b for b in fl["boxes"]}
    rows = []
    for fx in sorted(fl["fixtures"], key=lambda f: (f.get("box_id") or "", f.get("address") or 9999, f["label"])):
        b = boxes.get(fx.get("box_id"), {})
        fp = fixture_footprint(fx)
        a = fx.get("address")
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            _e(fx["label"]), _e(fixtures.VARIANT_LABELS.get(fx["variant"], fx["variant"])), _e(fx["mode"]),
            _e("%d-%d" % (a, a + fp - 1) if a else "not set"), _e(b.get("name", "")), _e(fx.get("uid") or ""),
            _e(describe_state(fx, fl["live"].get(fx["id"]), cal)), _e(fx.get("notes", ""))))
    box_rows = "".join("<tr><td>%s</td><td>%s</td><td>%d : %d : %d</td><td>%s</td></tr>" % (
        _e(b["name"]), _e(b.get("ip") or "not set"), b["net"], b["subnet"], b["universe"], _e(b.get("notes", "")))
        for b in fl["boxes"])
    probs = "".join("<li>%s</li>" % _e(p["text"]) for p in patch_problems(fl)) or "<li>None</li>"
    looks = "".join("<li>%s</li>" % _e(l["name"]) for l in fl["looks"]) or "<li>None saved</li>"
    return """<!doctype html><html><head><meta charset="utf-8"><title>%(code)s %(name)s patch</title>
<style>
@font-face{font-family:Inter;src:url(/fonts/inter-latin-400-normal.woff2) format('woff2')}
@font-face{font-family:Playfair;font-weight:900;src:url(/fonts/playfair-display-latin-900-normal.woff2) format('woff2')}
@font-face{font-family:Cinzel;font-weight:700;src:url(/fonts/cinzel-latin-700-normal.woff2) format('woff2')}
body{font:12px Inter,system-ui,sans-serif;color:#070605;background:#F6F0E1;margin:24px}
h1{font:900 28px Playfair,serif;margin:0}
.k{font:700 10px Cinzel,serif;letter-spacing:.2em;color:#9A7430;text-transform:uppercase}
table{border-collapse:collapse;width:100%%;margin:10px 0 18px}
th,td{border-bottom:1px solid #9A743055;padding:5px 6px;text-align:left;vertical-align:top}
th{font:700 9px Cinzel,serif;letter-spacing:.14em;color:#9A7430;text-transform:uppercase;border-bottom:1.5px solid #9A7430}
@media print{body{margin:10mm}}
</style></head><body>
<div class="k">%(project)s &middot; DMX Scene Builder patch sheet</div>
<h1>%(code)s %(name)s</h1>
<div class="k" style="margin:6px 0 14px">Printed %(date)s</div>
<div class="k">Boxes (Art-Net Net : Sub-Net : Universe)</div>
<table><tr><th>Box</th><th>IP</th><th>Art-Net</th><th>Notes</th></tr>%(boxes)s</table>
<div class="k">Fixtures</div>
<table><tr><th>Fixture</th><th>Type</th><th>Mode</th><th>Channels</th><th>Box</th><th>RDM UID</th><th>Live look</th><th>Notes</th></tr>%(rows)s</table>
<div class="k">Patch problems</div><ul>%(probs)s</ul>
<div class="k">Saved looks</div><ul>%(looks)s</ul>
</body></html>""" % {"project": _e(project), "code": _e(fl.get("code")), "name": _e(fl.get("name")),
                     "date": time.strftime("%Y-%m-%d %H:%M"), "boxes": box_rows, "rows": "".join(rows),
                     "probs": probs, "looks": looks}
