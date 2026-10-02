"""Fixture library and look -> DMX rendering.

Channel layouts transcribed from "DMX protocol for Calumma - All sizes - MC and SC, Version 1.3"
(appendix of the Anolis E-Box Remote user manual v2.5).

A fixture's *state* (what the UI edits) is a dict:
    {
      "dim":   0.0-1.0            master intensity
      "kind":  "white" | "color"  white = tunable/calibrated white, color = RGB(+W) mix
      "cct":   1800-6500          colour temperature in K (white kind)
      "hue":   0-360, "sat": 0-1  colour (color kind)
      "white": 0.0-1.0            extra white LED level mixed into a colour (RGBW only)
    }
"""
from __future__ import annotations

import colorsys
from typing import Dict, List, Optional

# ---------------------------------------------------------------- channel roles
# special  Special functions (save initial values etc.), 0 = no function
# r g b w  8-bit colour (coarse); *_f = fine byte of a 16-bit pair
# gc       green correction (128 = uncorrected, default)
# ctc      colour temperature correction (0 = off; 21..255 = 1800..6500 K)
# vcw      virtual colour wheel (0 = off)
# shutter  shutter/strobe (255 = open)
# dim dim_f  master dimmer 16-bit
# wsel     TW white selection (0..255 = 2700..6500 K)
# ww cw    TW warm / cool white LEDs

MODES: Dict[str, Dict[int, dict]] = {
    "RGBW": {
        1: {"name": "Mode 1 - RGBW 8-bit", "roles": ["r", "g", "b", "w"]},
        2: {"name": "Mode 2 - RGB 8-bit", "roles": ["r", "g", "b"]},
        3: {"name": "Mode 3 - Full RGBW 16-bit", "roles": ["r", "r_f", "g", "g_f", "b", "b_f", "w", "w_f", "gc", "ctc", "dim", "dim_f"]},
        4: {"name": "Mode 4 - White (CTC) only", "roles": ["gc", "ctc", "dim"]},
        5: {"name": "Mode 5 - Reduced RGBW + dimmer", "roles": ["r", "g", "b", "w", "dim", "dim_f"]},
        6: {"name": "Mode 6 - Reduced RGBW + white control", "roles": ["r", "g", "b", "w", "gc", "ctc", "dim", "dim_f"]},
        7: {"name": "Mode 7 - Full control (can save stand-alone look)", "roles": ["special", "r", "r_f", "g", "g_f", "b", "b_f", "w", "w_f", "gc", "ctc", "vcw", "shutter", "dim", "dim_f"]},
    },
    "TW": {
        11: {"name": "Mode 11 - White selection + dimmer", "roles": ["wsel", "dim", "dim_f"]},
        12: {"name": "Mode 12 - Warm + cool + dimmer", "roles": ["ww", "cw", "dim", "dim_f"]},
        13: {"name": "Mode 13 - Dimmer only", "roles": ["dim", "dim_f"]},
        # Not in Robe's chart, but TW Calumma accept it (field test 2026-10-01): CTC is ignored,
        # red + blue drive the cool LEDs, green + white the warm ones. The only mode that can save a look.
        7: {"name": "Mode 7 - Full control (can save stand-alone look)", "roles": ["special", "r", "r_f", "g", "g_f", "b", "b_f", "w", "w_f", "gc", "ctc", "vcw", "shutter", "dim", "dim_f"]},
    },
    "PW": {
        13: {"name": "Mode 13 - Dimmer only", "roles": ["dim", "dim_f"]},
    },
}

DEFAULT_MODE = {"RGBW": 1, "TW": 11, "PW": 13}  # factory defaults per the manual
SAVE_MODE = 7  # the only documented mode with the "save as initial values" channel

VARIANT_LABELS = {
    "RGBW": "RGB + White",
    "TW": "Tunable white",
    "PW": "Single white",
}

# Calibrated CTC anchors: (DMX value, Kelvin) from the chart: 21-1800, 66-2700, 91-3200, 141-4200, 211-5600, 255-6500
CTC_ANCHORS = [(21, 1800), (66, 2700), (91, 3200), (141, 4200), (211, 5600), (255, 6500)]
TW_MIN_K, TW_MAX_K = 2700, 6500
TW7_MIN_K = 3000  # the SDC tunable whites' warm LED (Mode 7 mixes the two LEDs directly)


def mode_info(variant: str, mode: int) -> dict:
    try:
        return MODES[variant][int(mode)]
    except KeyError:
        raise ValueError("Unknown mode %s for %s" % (mode, variant))


def footprint(variant: str, mode: int) -> int:
    return len(mode_info(variant, mode)["roles"])


def variant_for_mode(mode: int) -> str:
    for v, modes in MODES.items():
        if int(mode) in modes:
            return v
    raise ValueError("Unknown Calumma mode %s" % mode)


def mode_catalog() -> List[dict]:
    out = []
    for v, modes in MODES.items():
        for m, info in modes.items():
            out.append({"variant": v, "mode": m, "name": info["name"], "footprint": len(info["roles"]),
                        "roles": info["roles"]})
    return out


def kelvin_to_ctc(k: float) -> int:
    k = max(CTC_ANCHORS[0][1], min(CTC_ANCHORS[-1][1], float(k)))
    for (v0, k0), (v1, k1) in zip(CTC_ANCHORS, CTC_ANCHORS[1:]):
        if k0 <= k <= k1:
            return int(round(v0 + (v1 - v0) * (k - k0) / (k1 - k0)))
    return CTC_ANCHORS[-1][0]


def ctc_to_kelvin(v: int) -> float:
    if v < CTC_ANCHORS[0][0]:
        return 0.0
    for (v0, k0), (v1, k1) in zip(CTC_ANCHORS, CTC_ANCHORS[1:]):
        if v0 <= v <= v1:
            return k0 + (k1 - k0) * (v - v0) / (v1 - v0)
    return float(CTC_ANCHORS[-1][1])


def kelvin_to_wsel(k: float) -> int:
    k = max(TW_MIN_K, min(TW_MAX_K, float(k)))
    return int(round(255 * (k - TW_MIN_K) / (TW_MAX_K - TW_MIN_K)))


def _u16(x: float):
    v = int(round(max(0.0, min(1.0, x)) * 65535))
    return v >> 8, v & 0xFF


def _u8(x: float) -> int:
    return int(round(max(0.0, min(1.0, x)) * 255))


DEFAULT_STATE = {"dim": 1.0, "kind": "white", "cct": 3000, "hue": 30.0, "sat": 1.0, "white": 0.0}


def normalize_state(state: dict, variant: str) -> dict:
    s = dict(DEFAULT_STATE)
    s.update({k: v for k, v in (state or {}).items() if k in DEFAULT_STATE})
    s["dim"] = max(0.0, min(1.0, float(s["dim"])))
    s["cct"] = max(1800, min(6500, float(s["cct"])))
    s["hue"] = float(s["hue"]) % 360.0
    s["sat"] = max(0.0, min(1.0, float(s["sat"])))
    s["white"] = max(0.0, min(1.0, float(s["white"])))
    if variant != "RGBW" or s["kind"] not in ("white", "color"):
        s["kind"] = "white"
    return s


def tuned_mix(cal: dict, k: float):
    """Eye-tuned RGBW white: cal = {"6500": [r, g, b, w], ...} (0..1). Returns (r, g, b, w) for k,
    blending between tuned temperatures, or None when k is warmer than every tuned one."""
    pts = sorted((int(kk), v) for kk, v in (cal or {}).items() if isinstance(v, (list, tuple)) and len(v) == 4)
    if not pts or k < pts[0][0]:
        return None
    if k >= pts[-1][0]:
        return tuple(pts[-1][1])
    for (k0, a), (k1, b) in zip(pts, pts[1:]):
        if k0 <= k <= k1:
            t = (k - k0) / float(k1 - k0)
            return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))
    return None


def role_values(variant: str, mode: int, state: dict, special: int = 0, cal: Optional[dict] = None) -> Dict[str, float]:
    """Compute a 0..1 (or raw byte for special/gc/ctc/vcw/shutter/wsel) value for every role."""
    s = normalize_state(state, variant)
    roles = mode_info(variant, mode)["roles"]
    has_dimmer = "dim" in roles
    has_ctc = "ctc" in roles
    dim = s["dim"]
    vals: Dict[str, float] = {}

    if variant == "RGBW":
        if s["kind"] == "color":
            r, g, b = colorsys.hsv_to_rgb(s["hue"] / 360.0, s["sat"], 1.0)
            w = s["white"]
            ctc_byte = 0
        else:  # white
            mix = tuned_mix(cal, s["cct"]) if ("w" in roles) else None
            if mix:
                # Cool whites tuned by eye (the fixture's own calibration is pink at the cool end).
                r, g, b, w = (max(0.0, min(1.0, float(x))) for x in mix)
                ctc_byte = 0
            elif has_ctc:
                # Calibrated white: all emitters full, CTC picks the calibrated colour temperature.
                r = g = b = w = 1.0
                ctc_byte = kelvin_to_ctc(s["cct"])
            elif "w" in roles:
                # No CTC channel in this mode: use the native white LED only.
                r = g = b = 0.0
                w = 1.0
                ctc_byte = 0
            else:
                # Mode 2 (RGB only): make white from R+G+B.
                r = g = b = 1.0
                w = 0.0
                ctc_byte = 0
        scale = 1.0 if has_dimmer else dim
        vals.update({"r": r * scale, "g": g * scale, "b": b * scale, "w": w * scale})
        vals["ctc_byte"] = ctc_byte
    elif variant == "TW":
        k = max(TW_MIN_K, min(TW_MAX_K, s["cct"]))
        vals["wsel_byte"] = kelvin_to_wsel(k)
        cool = (k - TW_MIN_K) / (TW_MAX_K - TW_MIN_K)
        warm = 1.0 - cool
        peak = max(warm, cool) or 1.0
        vals["ww"], vals["cw"] = warm / peak, cool / peak
        if "r" in roles:  # Mode 7: blend the two LEDs in mireds (equal parts measured ~4000K).
            # Shares add up to one LED's worth, so brightness stays even across the range
            # (full-on both LEDs at 4000K looked about twice as bright as 3000K/6500K).
            k7 = max(TW7_MIN_K, min(TW_MAX_K, s["cct"]))
            cool = (1e6 / TW7_MIN_K - 1e6 / k7) / (1e6 / TW7_MIN_K - 1e6 / TW_MAX_K)
            vals["r"] = vals["b"] = cool
            vals["g"] = vals["w"] = 1.0 - cool
    vals["dim"] = dim
    vals["special_byte"] = special
    return vals


def mix_values(variant: str, mode: int, state: dict, cal: Optional[dict] = None) -> dict:
    """What the light is asked to make, for people: R/G/B/W 0-255 before the dimmer, brightness %,
    and the fixture's own colour-temperature setting when that's how the white is made."""
    s = normalize_state(state, variant)
    out = {"dim": round(s["dim"] * 100)}
    if variant == "RGBW":
        v = role_values("RGBW", mode, dict(s, dim=1.0), 0, cal)
        out.update({c: int(round(v.get(c, 0.0) * 255)) for c in "rgbw"})
        if s["kind"] == "white" and v.get("ctc_byte"):
            out["ctc_k"] = int(round(ctc_to_kelvin(v["ctc_byte"]) / 10.0) * 10)
    elif variant == "TW":
        lo = TW7_MIN_K if "r" in mode_info(variant, mode)["roles"] else TW_MIN_K
        out["k"] = int(max(lo, min(TW_MAX_K, s["cct"])))
    return out


def describe_mix(variant: str, mode: int, state: dict, cal: Optional[dict] = None) -> str:
    m = mix_values(variant, mode, state, cal)
    if variant == "RGBW":
        txt = "%d%%  R %d  G %d  B %d  W %d" % (m["dim"], m["r"], m["g"], m["b"], m["w"])
        return txt + ("  (fixture white %dK)" % m["ctc_k"] if "ctc_k" in m else "")
    if variant == "TW":
        return "%d%%  %dK" % (m["dim"], m["k"])
    return "%d%%" % m["dim"]


def render(variant: str, mode: int, state: dict, special: int = 0, cal: Optional[dict] = None) -> bytes:
    """Return the fixture's DMX footprint bytes for a state. `cal` = eye-tuned RGBW whites."""
    roles = mode_info(variant, mode)["roles"]
    v = role_values(variant, mode, state, special, cal)
    out = []
    i = 0
    while i < len(roles):
        role = roles[i]
        nxt = roles[i + 1] if i + 1 < len(roles) else None
        if nxt == role + "_f":
            hi, lo = _u16(v.get(role, 0.0))
            out += [hi, lo]
            i += 2
            continue
        if role == "special":
            out.append(int(v["special_byte"]) & 0xFF)
        elif role == "gc":
            out.append(128)
        elif role == "ctc":
            out.append(int(v.get("ctc_byte", 0)))
        elif role == "vcw":
            out.append(0)
        elif role == "shutter":
            out.append(255)
        elif role == "wsel":
            out.append(int(v.get("wsel_byte", 0)))
        else:
            out.append(_u8(v.get(role, 0.0)))
        i += 1
    return bytes(out)


def preview_rgb(variant: str, state: dict, white_k: int = 3000) -> List[int]:
    """Approximate on-screen colour for the UI/simulator (sRGB 0-255)."""
    s = normalize_state(state, variant)
    if variant == "RGBW" and s["kind"] == "color":
        r, g, b = colorsys.hsv_to_rgb(s["hue"] / 360.0, s["sat"], 1.0)
        wr, wg, wb = kelvin_to_rgb(white_k)
        w = s["white"]
        r, g, b = min(1, r + w * wr), min(1, g + w * wg), min(1, b + w * wb)
    else:
        r, g, b = kelvin_to_rgb(s["cct"] if variant != "PW" else white_k)
    d = s["dim"]
    return [int(round(r * d * 255)), int(round(g * d * 255)), int(round(b * d * 255))]


def kelvin_to_rgb(k: float):
    """Tanner Helland approximation, normalised to 0..1."""
    import math
    t = max(1000.0, min(40000.0, float(k))) / 100.0
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    c = lambda x: max(0.0, min(255.0, x)) / 255.0
    return c(r), c(g), c(b)
