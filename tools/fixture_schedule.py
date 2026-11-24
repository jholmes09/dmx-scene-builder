"""Calumma fixture schedule, by float, from a DMX Scene Builder show file. Prints Markdown.

    python3 tools/fixture_schedule.py <show.json> [--rows N] [--first N] > schedule.md

One row per light per saved look (preset), with separate Dimmer and Color columns. With --rows,
page breaks are inserted so a light's rows never split, a float starts on a new page when it
won't fit, and a float that runs over a page continues under a "continued" heading.
--rows is how many table rows fit on a page; --first is how many fit on the first page.
"""
import argparse, json, os, sys, urllib.parse
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from scenebuilder import fixtures

ap = argparse.ArgumentParser()
ap.add_argument("show"); ap.add_argument("--rows", type=int, default=0); ap.add_argument("--first", type=int, default=0)
ap.add_argument("--all-looks", action="store_true", help="list every saved look instead of only what the app last sent")
ap.add_argument("--note", action="append", default=[], help='"CODE=text": a note printed under that float\'s heading')
args = ap.parse_args()
d = json.load(open(args.show))
cal = (d.get("white_cal") or {}).get("RGBW")
TYPE = {"TW": "TW", "RGBW": "RGBW", "PW": "White"}
HEAD_ROWS, NOTE_ROWS, BOX_ROWS = 2, 2, 3     # page space a float heading, a note, a box line + table header take

def clean(label):
    s = label or ""
    for _ in range(8):
        n = urllib.parse.unquote(s)
        if n == s: break
        s = n
    return s.replace("’", "'").strip() or "Calumma"

def value(fx, st):
    v = fx["variant"]
    m = fixtures.mix_values(v, fx["mode"], fixtures.normalize_state(st, v), cal if v == "RGBW" else None)
    if v == "RGBW":
        return "%d%%" % m["dim"], "R%d G%d B%d W%d" % (m["r"], m["g"], m["b"], m["w"]) + (", %dK" % m["ctc_k"] if "ctc_k" in m else "")
    if v == "TW":
        return "%d%%" % m["dim"], "%dK" % m["k"] + (", max output" if m.get("boost") else "")
    return "%d%%" % m["dim"], ""

# ---- build: floats -> (title, notes, [(box line, [fixture row groups])])
floats = []
for fl in d["floats"]:
    code = (fl.get("code") or "").strip()
    title = (code + " " if code else "") + fl["name"]
    looks, seen, names = [], set(), {}
    for l in fl.get("looks", []):
        key = (l["name"], json.dumps(l.get("states"), sort_keys=True))
        if key in seen: continue
        seen.add(key); n = clean(l["name"]); names[n] = names.get(n, 0) + 1
        looks.append((n + ("" if names[n] == 1 else " (%d)" % names[n]), l.get("states") or {}))
    ids = {x["id"] for x in fl["fixtures"]}
    notes = [n.split("=", 1)[1] for n in args.note if n.split("=", 1)[0] == code]
    modes = sorted({x["mode"] for x in fl["fixtures"]})
    if fl["fixtures"] and modes != [7]:
        notes.append("These lights are in Mode %s, not Mode 7, so they can't store a look yet." % ", ".join(map(str, modes)))
    live = fl.get("live") or {}
    if not args.all_looks:
        # What the app last sent each light: after Save look, that is what is stored in the lights,
        # unless someone changed it afterwards. Named after the saved look it matches, if one does.
        def same(a, b):
            return a.get("kind") == b.get("kind") and all(abs(float(a.get(k, 0)) - float(b.get(k, 0))) < 0.011 for k in ("dim", "cct", "hue", "sat", "white"))
        lit = [i for i in ids if i in live]
        name = next((n for n, st in looks if lit and all(i in st and same(st[i], live[i]) for i in lit)), "Current setting")
        looks = [(name, live)] if lit else []
        if not lit:
            notes.append("No setting in the app yet.")
    elif looks and not any(i in st for _, st in looks for i in ids):
        notes.append("This float's saved looks were made before its lights were re-added, so they no longer apply. Shown here is the app's current setting. Save it as a look again.")
        looks = []
    if args.all_looks and not looks and any(i in live for i in ids):
        looks = [("Current setting", live)]
        if not fl.get("looks"):
            notes.append("No saved look in the app yet. Shown here is the app's current setting.")
    elif args.all_looks and not looks:
        notes.append("No saved look in the app yet.")
    boxes = []
    for b in fl["boxes"]:
        fxs = [x for x in fl["fixtures"] if x.get("box_id") == b["id"]]
        if not fxs: continue
        line = "**%s:** IP %s, Art-Net %d:%d:%d" % (b.get("name") or "E-Box", b.get("ip") or "not set", b.get("net", 0), b.get("subnet", 0), b.get("universe", 0))
        groups = []
        for x in sorted(fxs, key=lambda x: (x.get("address") is None, x.get("address") or 0)):
            addr = str(x["address"]) if x.get("address") else "Not set"
            pres = [(n,) + value(x, st[x["id"]]) for n, st in looks if x["id"] in st] or [("None", "", "")]
            groups.append([("| %s | %s | %s | %s | %s | %s |" % (((clean(x["label"]), TYPE.get(x["variant"], x["variant"]), addr) if i == 0 else ("", "", "")) + p),
                            1 + 0.6 * (len(p[0]) > 13) + 0.6 * (i == 0 and len(clean(x["label"])) > 22) + 0.6 * (len(p[2]) > 22))
                           for i, p in enumerate(pres)])
        boxes.append((line, groups))
    if not fl["fixtures"]:
        notes.append("No Calumma lights in the app for this float.")
    floats.append((title, notes, boxes))

# ---- lay out
TH = "| Fixture | Type | Address | Preset | Dimmer | Color |\n|---|---|---|---|---|---|"
out, used = [], 0
cap = args.first or args.rows

def new_page():
    global used, cap
    out.append("---\n---\n")
    used, cap = 0, args.rows

for title, notes, boxes in floats:
    need = HEAD_ROWS + NOTE_ROWS * len(notes) + sum(BOX_ROWS + sum(w for g in gs for _, w in g) for _, gs in boxes)
    if args.rows and used and used + need > cap:
        new_page()
    out.append("### %s\n" % title); used += HEAD_ROWS
    for n in notes:
        out.append(n + "\n"); used += NOTE_ROWS
    for line, groups in boxes:
        if args.rows and used + BOX_ROWS + sum(w for _, w in groups[0]) > cap:
            new_page(); out.append("### %s, continued\n" % title); used += HEAD_ROWS
        out.append(line + "\n"); out.append(TH); used += BOX_ROWS
        for g in groups:
            if args.rows and used + sum(w for _, w in g) > cap:
                out.append("")
                new_page(); out.append("### %s, continued\n" % title); used += HEAD_ROWS
                out.append(line + "\n"); out.append(TH); used += BOX_ROWS
            out.extend(r for r, _ in g); used += sum(w for _, w in g)
        out.append("")
print("\n".join(out))
