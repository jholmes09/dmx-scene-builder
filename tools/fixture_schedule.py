"""Calumma fixture schedule, by float, from a DMX Scene Builder show file. Prints Markdown.
    python3 tools/fixture_schedule.py <show.json> "###" > schedule.md"""
import json, sys, urllib.parse
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), ".."))
from scenebuilder import fixtures

src, level = sys.argv[1], sys.argv[2]          # level: "##" or "###" for each float's heading
d = json.load(open(src))
cal = (d.get("white_cal") or {}).get("RGBW")
TYPE = {"TW": "Calumma TW", "RGBW": "Calumma RGBW", "PW": "Calumma white"}

def clean(label):
    s = label or ""
    for _ in range(8):
        n = urllib.parse.unquote(s)
        if n == s: break
        s = n
    return s.replace("’", "'").strip() or "Calumma"

def value(fx, st):
    v = fx["variant"]; mode = fx["mode"]
    m = fixtures.mix_values(v, mode, fixtures.normalize_state(st, v), cal if v == "RGBW" else None)
    if v == "RGBW":
        t = "%d%%, R%d G%d B%d W%d" % (m["dim"], m["r"], m["g"], m["b"], m["w"])
        return t + (", fixture white %dK" % m["ctc_k"] if "ctc_k" in m else "")
    if v == "TW":
        return "%d%%, %dK" % (m["dim"], m["k"]) + (", max output" if m.get("boost") else "")
    return "%d%%" % m["dim"]

out = []
for fl in d["floats"]:
    name = fl["name"]; code = (fl.get("code") or "").strip()
    out.append(f"{level} {code + ' ' if code else ''}{name}\n")
    boxes = fl["boxes"]
    # looks: drop exact duplicates, number repeated names
    looks, seen, names = [], set(), {}
    for l in fl.get("looks", []):
        key = (l["name"], json.dumps(l.get("states"), sort_keys=True))
        if key in seen: continue
        seen.add(key)
        names[l["name"]] = names.get(l["name"], 0) + 1
        looks.append((clean(l["name"]) + ("" if names[l["name"]] == 1 else " (%d)" % names[l["name"]]), l.get("states") or {}))
    if not fl["fixtures"]:
        out.append("No Calumma lights in the app for this float.\n"); continue
    ids = {x["id"] for x in fl["fixtures"]}
    modes = sorted({x["mode"] for x in fl["fixtures"]})
    if modes != [7]:
        out.append("These lights are in Mode %s, not Mode 7, so they can't store a look yet.\n" % ", ".join(map(str, modes)))
    if not looks:
        out.append("No saved look in the app yet.\n")
    elif not any(i in st for _, st in looks for i in ids):
        out.append("This float's saved looks were made before its lights were re-patched, so none of them match the current lights. Set the look and save it again.\n")
        looks = []
    for b in boxes:
        fxs = [x for x in fl["fixtures"] if x.get("box_id") == b["id"]]
        if not fxs: continue
        ip = b.get("ip") or "not set"
        out.append(f"**{b.get('name') or 'E-Box'}:** IP {ip}, Art-Net {b.get('net',0)}:{b.get('subnet',0)}:{b.get('universe',0)}\n")
        out.append("| Fixture | Fixture type | Address | Color presets |\n|---|---|---|---|")
        for x in sorted(fxs, key=lambda x: (x.get("address") is None, x.get("address") or 0)):
            tid = x.get("type_id") or ""
            t = TYPE.get(x["variant"], x["variant"]) + (f" ({tid})" if tid and not clean(x["label"]).startswith(tid) else "")
            addr = str(x["address"]) if x.get("address") else "Not set"
            pres = [f"{n}: {value(x, st[x['id']])}" for n, st in looks if x["id"] in st]
            out.append(f"| {clean(x['label'])} | {t} | {addr} | {'<br>'.join(pres) or 'None'} |")
        out.append("")
print("\n".join(out))
