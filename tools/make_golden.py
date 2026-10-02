#!/usr/bin/env python3
"""Dump golden vectors from the Python core into ios/Tests/golden.json.

The iPad app's Swift port must reproduce every byte and string here exactly:
fixtures.render / mix_values / describe_mix / normalize_state over a wide grid of
variants, modes, states, Special bytes and eye-tuned white calibrations, the colour
temperature helpers, the white-test frames, patch problems / auto-addressing, and
Art-Net packet encode/decode. Run it again whenever the Python core changes:

    python3 tools/make_golden.py
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scenebuilder import artnet, fixtures  # noqa: E402
from scenebuilder.engine import white_test_bytes  # noqa: E402
from scenebuilder.server import plan_addresses  # noqa: E402
from scenebuilder.store import patch_problems, validate_float  # noqa: E402

OUT = ROOT / "ios" / "Tests" / "golden.json"

CALS = [
    None,
    {},
    {"6500": [0.75, 1, 0.9, 1]},
    {"4200": [1, 0.9, 0.55, 1], "5600": [0.8, 0.95, 0.75, 1], "6500": [0.75, 1, 0.9, 1]},
    {"5000": [1, 1, 1, 1], "3000": [0.2, 0.4, 0.0, 1.2]},
]
SPECIALS = [0, 1, 2, 3, 255, 256]

DIMS = [0, 0.0, 0.005, 0.02, 0.1234, 1 / 3.0, 0.5, 0.999, 1, 1.0, 1.5, -0.1]
KINDS = ["white", "color", "bogus"]
CCTS = [1000, 1800, 2000, 2700, 2999.9, 3000, 3100, 3200, 4000, 4200, 4321.5, 5600, 6000, 6500, 7000, 9999]
HUES = [0, 0.0, 15, 30, 59.9, 60, 120, 180, 235, 300, 359.99, 360, -30, -1e-20, 400, 720.5]
SATS = [0, 0.0, 0.25, 0.5, 1, 1.0, 2, -1]
WHITES = [0, 0.3, 0.5, 1, 1.0, 1.7]
BOOSTS = [False, True, 0, 1]


def rand_state(rng):
    full = {"dim": rng.choice(DIMS), "kind": rng.choice(KINDS), "cct": rng.choice(CCTS),
            "hue": rng.choice(HUES), "sat": rng.choice(SATS), "white": rng.choice(WHITES),
            "boost": rng.choice(BOOSTS)}
    if rng.random() < 0.25:  # partial states: defaults fill the rest
        keys = rng.sample(sorted(full), rng.randint(0, len(full)))
        full = {k: full[k] for k in keys}
    if rng.random() < 0.1:
        full["junk"] = 5  # ignored
    return full


def render_cases():
    rng = random.Random(20261002)
    combos = [(v, m) for v, modes in fixtures.MODES.items() for m in modes]
    cases = []
    # Structured edges: every combo x every kind x a few temperatures, default specials/cal
    for v, m in combos:
        for kind in KINDS:
            for cct in (1800, 2700, 3000, 4000, 6500):
                for dim in (0, 0.5, 1):
                    cases.append((v, m, {"dim": dim, "kind": kind, "cct": cct, "hue": 235, "sat": 1, "white": 0.3}, 0, 0))
        for boost in (False, True):
            for cct in (3000, 3500, 4000, 4500, 5000, 6500):
                cases.append((v, m, {"dim": 1, "cct": cct, "boost": boost}, 0, 0))
        for sp in SPECIALS:
            cases.append((v, m, {}, sp, 0))
        for ci in range(len(CALS)):
            for cct in (2700, 4199, 4200, 4900, 5600, 6000, 6500):
                cases.append((v, m, {"kind": "white", "cct": cct, "dim": 0.8}, 0, ci))
    for _ in range(4000):
        v, m = rng.choice(combos)
        cases.append((v, m, rand_state(rng), rng.choice(SPECIALS), rng.randrange(len(CALS))))
    out = []
    for v, m, st, sp, ci in cases:
        cal = CALS[ci]
        out.append({"v": v, "m": m, "s": st, "sp": sp, "cal": ci,
                    "render": list(fixtures.render(v, m, st, sp, cal)),
                    "mix": fixtures.mix_values(v, m, st, cal),
                    "desc": fixtures.describe_mix(v, m, st, cal),
                    "norm": fixtures.normalize_state(st, v)})
    return out


def float_fixture(fid, variant, mode, box, address, label=None):
    return {"id": fid, "label": label or fid, "variant": variant, "mode": mode, "box_id": box,
            "address": address, "uid": None, "notes": ""}


def store_cases():
    boxes = [{"id": "bA", "name": "A", "ip": "", "net": 0, "subnet": 0, "universe": 0},
             {"id": "bB", "name": "B", "ip": "", "net": 0, "subnet": 0, "universe": 1}]
    floats = [
        {"id": "f1", "boxes": boxes, "looks": [], "fixtures": [
            float_fixture("x1", "TW", 11, "bA", 1), float_fixture("x2", "TW", 12, "bA", 3),
            float_fixture("x3", "RGBW", 7, "bA", 10), float_fixture("x4", "RGBW", 3, "bA", None),
            float_fixture("x5", "PW", 7, "bB", 500), float_fixture("x6", "TW", 7, "bB", 1),
            float_fixture("x7", "RGBW", 1, "bB", 10), float_fixture("x8", "PW", 13, "bA", 511)]},
        {"id": "f2", "boxes": boxes[:1], "looks": [], "fixtures": [
            float_fixture("y%d" % i, ["TW", "RGBW", "PW"][i % 3], [11, 7, 13][i % 3], "bA", (i * 5) % 40 + 1)
            for i in range(12)]},
    ]
    out = []
    for fl in floats:
        fl = json.loads(json.dumps(fl))
        validate_float(fl)
        plans = []
        for box_id in ("bA", "bB", None):
            for start in (1, 7, 480):
                for ids in (None, [fx["id"] for fx in fl["fixtures"][::2]]):
                    for gap in (0, 2):
                        for modes in (None, {fx["id"]: 7 for fx in fl["fixtures"]}):
                            try:
                                res = plan_addresses(fl, box_id, start, ids, gap, modes)
                                plans.append({"box_id": box_id, "start": start, "ids": ids, "gap": gap,
                                              "modes": modes, "result": res})
                            except ValueError as e:
                                plans.append({"box_id": box_id, "start": start, "ids": ids, "gap": gap,
                                              "modes": modes, "error": str(e)})
        out.append({"float": fl, "problems": patch_problems(fl), "plans": plans})
    return out


def packet_cases():
    out = {"poll": [list(artnet.build_poll()), list(artnet.build_poll(0x02))], "dmx": [], "poll_reply": [],
           "port_address": []}
    for net, sub, uni in ((0, 0, 0), (0, 0, 1), (1, 2, 3), (127, 15, 15), (5, 0, 9)):
        out["port_address"].append([net, sub, uni, artnet.port_address(net, sub, uni)])
    for pa, n, seq in ((0, 512, 1), (1, 0, 0), (0x7FFF, 1, 255), (0x123, 3, 256), (17, 600, 77), (0, 24, 9)):
        data = bytes((i * 7 + n) & 0xFF for i in range(n))
        pkt = artnet.build_dmx(pa, data, seq)
        parsed = artnet.parse_dmx(pkt)
        out["dmx"].append({"pa": pa, "data": list(data), "seq": seq, "packet": list(pkt),
                           "parsed": dict(parsed, data=list(parsed["data"]))})
    for args in (("10.248.31.11", 0, 0, [0], "E-box Remote", "E-box Remote (SIM)", b"\x00\x50\xc2\x12\x34\x56"),
                 ("2.0.0.1", 3, 4, [5, 6], "Box", "A much longer name " * 4, b"\x01\x02\x03\x04\x05\x06")):
        pkt = artnet.build_poll_reply(*args)
        out["poll_reply"].append({"args": [args[0], args[1], args[2], args[3], args[4], args[5], list(args[6])],
                                  "packet": list(pkt), "parsed": artnet.parse_poll_reply(pkt).to_dict()})
    return out


def main():
    golden = {
        "render": render_cases(),
        "cals": CALS,
        "kelvin_to_ctc": [[k, fixtures.kelvin_to_ctc(k)] for k in [x / 2.0 for x in range(3000, 14200, 7)]],
        "ctc_to_kelvin": [[v, fixtures.ctc_to_kelvin(v)] for v in range(256)],
        "kelvin_to_wsel": [[k, fixtures.kelvin_to_wsel(k)] for k in range(2000, 7200, 13)],
        "footprints": [[v, m, fixtures.footprint(v, m)] for v, ms in fixtures.MODES.items() for m in ms],
        "white_test": [{"method": meth, "k": k, "mix": mix, "bytes": list(white_test_bytes(meth, k, mix))}
                       for meth in range(0, 14) for k in (1800, 2700, 3000, 4200, 5000, 6500)
                       for mix in (None, [0.75, 1, 0.9, 1])],
        "store": store_cases(),
        "packets": packet_cases(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(golden, separators=(",", ":")))
    print("wrote %s (%d render cases)" % (OUT, len(golden["render"])))


if __name__ == "__main__":
    main()
