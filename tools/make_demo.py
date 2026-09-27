#!/usr/bin/env python3
"""Build data/demo_project.json: made-up floats for trying DMX Scene Builder. Real show data stays local."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scenebuilder.store import new_box, new_fixture, new_float  # noqa: E402

DEMO = [
    # code, name, run_mode, [(type id, variant, count, purpose)]
    ("D1", "Demo Float: Snow Globe", "standalone", [("TW1", "TW", 4, "Front light, performers"), ("RGB1", "RGBW", 2, "Uplight, globe")]),
    ("D2", "Demo Float: Gingerbread House", "standalone", [("TW2", "TW", 6, "Wash, house front")]),
    ("D3", "Demo Float: Lantern Cart", "standalone", [("RGB2", "RGBW", 5, "Underside glow")]),
    ("D4", "Demo Float: Music Box (live DMX)", "live_dmx", [("TW3", "TW", 3, "Key light, dancer")]),
]


def main():
    floats = []
    for code, name, run_mode, groups in DEMO:
        fl = new_float(code, name)
        fl["run_mode"] = run_mode
        fl["boxes"] = [new_box("E-Box A")]
        for type_id, variant, count, purpose in groups:
            for i in range(count):
                fl["fixtures"].append(new_fixture(label="%s-%d" % (type_id, i + 1), variant=variant,
                                                  box_id=fl["boxes"][0]["id"], type_id=type_id, notes=purpose))
        floats.append(fl)
    out = ROOT / "data" / "demo_project.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"schema": 1, "project": "Demo Parade", "floats": floats}, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    main()
