import Foundation

/// Fixture library and look -> DMX rendering. A line-by-line port of scenebuilder/fixtures.py;
/// tests compare it byte for byte with the Python output (ios/Tests/golden.json).
///
/// Channel layouts from "DMX protocol for Calumma - All sizes - MC and SC, Version 1.3".
enum Fixtures {
    struct ModeInfo { let name: String; let roles: [String] }

    static let mode7Roles = ["special", "r", "r_f", "g", "g_f", "b", "b_f", "w", "w_f", "gc", "ctc", "vcw", "shutter", "dim", "dim_f"]
    static let mode7Name = "Mode 7 - Full control (can save stand-alone look)"

    /// Ordered like the Python dicts (mode_catalog order matters to the UI's lists).
    static let modes: [(variant: String, modes: [(Int, ModeInfo)])] = [
        ("RGBW", [
            (1, ModeInfo(name: "Mode 1 - RGBW 8-bit", roles: ["r", "g", "b", "w"])),
            (2, ModeInfo(name: "Mode 2 - RGB 8-bit", roles: ["r", "g", "b"])),
            (3, ModeInfo(name: "Mode 3 - Full RGBW 16-bit", roles: ["r", "r_f", "g", "g_f", "b", "b_f", "w", "w_f", "gc", "ctc", "dim", "dim_f"])),
            (4, ModeInfo(name: "Mode 4 - White (CTC) only", roles: ["gc", "ctc", "dim"])),
            (5, ModeInfo(name: "Mode 5 - Reduced RGBW + dimmer", roles: ["r", "g", "b", "w", "dim", "dim_f"])),
            (6, ModeInfo(name: "Mode 6 - Reduced RGBW + white control", roles: ["r", "g", "b", "w", "gc", "ctc", "dim", "dim_f"])),
            (7, ModeInfo(name: mode7Name, roles: mode7Roles)),
        ]),
        ("TW", [
            (11, ModeInfo(name: "Mode 11 - White selection + dimmer", roles: ["wsel", "dim", "dim_f"])),
            (12, ModeInfo(name: "Mode 12 - Warm + cool + dimmer", roles: ["ww", "cw", "dim", "dim_f"])),
            (13, ModeInfo(name: "Mode 13 - Dimmer only", roles: ["dim", "dim_f"])),
            // Not in Robe's chart, but TW Calumma accept it (field test 2026-10-01): CTC is ignored,
            // red + blue drive the cool LEDs, green + white the warm ones. The only mode that can save a look.
            (7, ModeInfo(name: mode7Name, roles: mode7Roles)),
        ]),
        ("PW", [
            (13, ModeInfo(name: "Mode 13 - Dimmer only", roles: ["dim", "dim_f"])),
        ]),
    ]

    static let variants = modes.map { $0.variant }
    static let defaultMode = ["RGBW": 1, "TW": 11, "PW": 13]
    static let saveMode = 7
    static let variantLabels = ["RGBW": "RGB + White", "TW": "Tunable white", "PW": "Single white"]

    static let ctcAnchors: [(Int, Double)] = [(21, 1800), (66, 2700), (91, 3200), (141, 4200), (211, 5600), (255, 6500)]
    static let twMinK = 2700.0, twMaxK = 6500.0
    static let tw7MinK = 3000.0

    static func modesOf(_ variant: String) -> [Int] {
        modes.first { $0.variant == variant }?.modes.map { $0.0 } ?? []
    }

    static func hasMode(_ variant: String, _ mode: Int) -> Bool { modesOf(variant).contains(mode) }

    static func modeInfo(_ variant: String, _ mode: Int) throws -> ModeInfo {
        if let m = modes.first(where: { $0.variant == variant })?.modes.first(where: { $0.0 == mode }) { return m.1 }
        throw AppError.value("Unknown mode \(mode) for \(variant)")
    }

    static func footprint(_ variant: String, _ mode: Int) throws -> Int { try modeInfo(variant, mode).roles.count }

    static func modeCatalog() -> JSON {
        var out: [JSON] = []
        for (v, ms) in modes {
            for (m, info) in ms {
                out.append(["variant": .string(v), "mode": .int(m), "name": .string(info.name),
                            "footprint": .int(info.roles.count), "roles": .array(info.roles.map { .string($0) })])
            }
        }
        return .array(out)
    }

    // ------------------------------------------------------------ temperature helpers
    static func kelvinToCtc(_ kIn: Double) -> Int {
        let k = max(ctcAnchors[0].1, min(ctcAnchors[ctcAnchors.count - 1].1, kIn))
        for i in 0..<(ctcAnchors.count - 1) {
            let (v0, k0) = ctcAnchors[i], (v1, k1) = ctcAnchors[i + 1]
            if k0 <= k && k <= k1 {
                return pyRound(Double(v0) + Double(v1 - v0) * (k - k0) / (k1 - k0))
            }
        }
        return ctcAnchors[ctcAnchors.count - 1].0
    }

    static func ctcToKelvin(_ v: Int) -> Double {
        if v < ctcAnchors[0].0 { return 0.0 }
        for i in 0..<(ctcAnchors.count - 1) {
            let (v0, k0) = ctcAnchors[i], (v1, k1) = ctcAnchors[i + 1]
            if v0 <= v && v <= v1 {
                return k0 + (k1 - k0) * Double(v - v0) / Double(v1 - v0)
            }
        }
        return ctcAnchors[ctcAnchors.count - 1].1
    }

    static func kelvinToWsel(_ kIn: Double) -> Int {
        let k = max(twMinK, min(twMaxK, kIn))
        return pyRound(255 * (k - twMinK) / (twMaxK - twMinK))
    }

    static func u16(_ x: Double) -> (UInt8, UInt8) {
        let v = pyRound(max(0.0, min(1.0, x)) * 65535)
        return (UInt8(v >> 8), UInt8(v & 0xFF))
    }

    static func u8(_ x: Double) -> UInt8 { UInt8(pyRound(max(0.0, min(1.0, x)) * 255)) }

    // ------------------------------------------------------------ state
    struct State: Equatable {
        var dim = 1.0
        var kind = "white"
        var cct = 3000.0
        var hue = 30.0
        var sat = 1.0
        var white = 0.0
        var boost = false

        func toJSON() -> JSON {
            ["dim": .double(dim), "kind": .string(kind), "cct": .double(cct), "hue": .double(hue),
             "sat": .double(sat), "white": .double(white), "boost": .bool(boost)]
        }
    }

    static let defaultStateJSON: JSON = ["dim": 1.0, "kind": "white", "cct": 3000, "hue": 30.0, "sat": 1.0, "white": 0.0, "boost": false]
    static let stateKeys = ["dim", "kind", "cct", "hue", "sat", "white", "boost"]

    static func normalizeState(_ state: JSON, _ variant: String) throws -> State {
        var s = State()
        let o = state.objectValue
        if let v = o["dim"] { s.dim = try v.toDouble("dim") }
        if let v = o["cct"] { s.cct = try v.toDouble("cct") }
        if let v = o["hue"] { s.hue = try v.toDouble("hue") }
        if let v = o["sat"] { s.sat = try v.toDouble("sat") }
        if let v = o["white"] { s.white = try v.toDouble("white") }
        if let v = o["boost"] { s.boost = v.truthy }
        var kindIsValid = true
        if let v = o["kind"] {
            if let k = v.string { s.kind = k } else { kindIsValid = false }
        }
        s.dim = max(0.0, min(1.0, s.dim))
        s.cct = max(1800, min(6500, s.cct))
        s.hue = pyMod(s.hue, 360.0)
        s.sat = max(0.0, min(1.0, s.sat))
        s.white = max(0.0, min(1.0, s.white))
        if variant != "RGBW" || !kindIsValid || (s.kind != "white" && s.kind != "color") {
            s.kind = "white"
        }
        return s
    }

    /// Eye-tuned RGBW white: cal = {"6500": [r, g, b, w], ...}. nil when k is warmer than every tuned point.
    static func tunedMix(_ cal: JSON, _ k: Double) -> [Double]? {
        var pts: [(Int, [Double])] = []
        for (kk, v) in cal.objectValue {
            guard let a = v.array, a.count == 4, let ki = Int(kk.trimmingCharacters(in: .whitespaces)) else { continue }
            pts.append((ki, a.map { $0.number ?? 0 }))
        }
        pts.sort { $0.0 < $1.0 }
        guard let first = pts.first, k >= Double(first.0) else { return nil }
        if k >= Double(pts[pts.count - 1].0) { return pts[pts.count - 1].1 }
        for i in 0..<(pts.count - 1) {
            let (k0, a) = pts[i], (k1, b) = pts[i + 1]
            if Double(k0) <= k && k <= Double(k1) {
                let t = (k - Double(k0)) / Double(k1 - k0)
                return (0..<4).map { a[$0] + (b[$0] - a[$0]) * t }
            }
        }
        return nil
    }

    static func hsvToRgb(_ h: Double, _ s: Double, _ v: Double) -> (Double, Double, Double) {
        if s == 0.0 { return (v, v, v) }
        var i = Int(h * 6.0)
        let f = (h * 6.0) - Double(i)
        let p = v * (1.0 - s), q = v * (1.0 - s * f), t = v * (1.0 - s * (1.0 - f))
        i = ((i % 6) + 6) % 6
        switch i {
        case 0: return (v, t, p)
        case 1: return (q, v, p)
        case 2: return (p, v, t)
        case 3: return (p, q, v)
        case 4: return (t, p, v)
        default: return (v, p, q)
        }
    }

    struct Values {
        var vals: [String: Double] = [:]
        var ctcByte = 0
        var wselByte = 0
        var special = 0
        func get(_ role: String) -> Double { vals[role] ?? 0.0 }
    }

    static func roleValues(_ variant: String, _ mode: Int, _ s: State, special: Int = 0, cal: JSON = .null) throws -> Values {
        let roles = try modeInfo(variant, mode).roles
        let hasDimmer = roles.contains("dim"), hasCtc = roles.contains("ctc")
        let dim = s.dim
        var out = Values()
        if variant == "RGBW" {
            var r = 0.0, g = 0.0, b = 0.0, w = 0.0, ctc = 0
            if s.kind == "color" {
                (r, g, b) = hsvToRgb(s.hue / 360.0, s.sat, 1.0)
                w = s.white
            } else {
                let mix = roles.contains("w") ? tunedMix(cal, s.cct) : nil
                if let mix = mix {
                    let c = mix.map { max(0.0, min(1.0, $0)) }
                    (r, g, b, w) = (c[0], c[1], c[2], c[3])
                } else if hasCtc {
                    r = 1; g = 1; b = 1; w = 1
                    ctc = kelvinToCtc(s.cct)
                } else if roles.contains("w") {
                    w = 1.0
                } else {
                    r = 1; g = 1; b = 1
                }
            }
            let scale = hasDimmer ? 1.0 : dim
            out.vals["r"] = r * scale; out.vals["g"] = g * scale; out.vals["b"] = b * scale; out.vals["w"] = w * scale
            out.ctcByte = ctc
        } else if variant == "TW" {
            let k = max(twMinK, min(twMaxK, s.cct))
            out.wselByte = kelvinToWsel(k)
            let cool = (k - twMinK) / (twMaxK - twMinK)
            let warm = 1.0 - cool
            var peak = max(warm, cool)
            if peak == 0 { peak = 1.0 }
            out.vals["ww"] = warm / peak; out.vals["cw"] = cool / peak
            if roles.contains("r") {
                let k7 = max(tw7MinK, min(twMaxK, s.cct))
                let c7 = (1e6 / tw7MinK - 1e6 / k7) / (1e6 / tw7MinK - 1e6 / twMaxK)
                let pk = s.boost ? max(c7, 1.0 - c7) : 1.0
                out.vals["r"] = c7 / pk; out.vals["b"] = c7 / pk
                out.vals["g"] = (1.0 - c7) / pk; out.vals["w"] = (1.0 - c7) / pk
            }
        }
        out.vals["dim"] = dim
        out.special = special
        return out
    }

    static func render(_ variant: String, _ mode: Int, _ state: JSON, special: Int = 0, cal: JSON = .null) throws -> [UInt8] {
        try render(variant, mode, normalizeState(state, variant), special: special, cal: cal)
    }

    static func render(_ variant: String, _ mode: Int, _ s: State, special: Int = 0, cal: JSON = .null) throws -> [UInt8] {
        let roles = try modeInfo(variant, mode).roles
        let v = try roleValues(variant, mode, s, special: special, cal: cal)
        var out: [UInt8] = []
        var i = 0
        while i < roles.count {
            let role = roles[i]
            let nxt = i + 1 < roles.count ? roles[i + 1] : nil
            if nxt == role + "_f" {
                let (hi, lo) = u16(v.get(role))
                out += [hi, lo]
                i += 2
                continue
            }
            switch role {
            case "special": out.append(UInt8(v.special & 0xFF))
            case "gc": out.append(128)
            case "ctc": out.append(UInt8(v.ctcByte & 0xFF))
            case "vcw": out.append(0)
            case "shutter": out.append(255)
            case "wsel": out.append(UInt8(v.wselByte & 0xFF))
            default: out.append(u8(v.get(role)))
            }
            i += 1
        }
        return out
    }

    /// What the light is asked to make, for people (fixtures.mix_values).
    static func mixValues(_ variant: String, _ mode: Int, _ state: JSON, cal: JSON = .null) throws -> JSON {
        var s = try normalizeState(state, variant)
        var out: [String: JSON] = ["dim": .int(pyRound(s.dim * 100))]
        if variant == "RGBW" {
            s.dim = 1.0
            let v = try roleValues("RGBW", mode, s, special: 0, cal: cal)
            for c in ["r", "g", "b", "w"] { out[c] = .int(pyRound(v.get(c) * 255)) }
            if s.kind == "white" && v.ctcByte != 0 {
                out["ctc_k"] = .int(pyRound(ctcToKelvin(v.ctcByte) / 10.0) * 10)
            }
        } else if variant == "TW" {
            let lo = try modeInfo(variant, mode).roles.contains("r") ? tw7MinK : twMinK
            out["k"] = .int(Int(max(lo, min(twMaxK, s.cct))))
            if s.boost && lo == tw7MinK { out["boost"] = .bool(true) }
        }
        return .object(out)
    }

    static func describeMix(_ variant: String, _ mode: Int, _ state: JSON, cal: JSON = .null) throws -> String {
        let m = try mixValues(variant, mode, state, cal: cal)
        let n = { (k: String) in m[k].int ?? 0 }
        if variant == "RGBW" {
            let txt = "\(n("dim"))%  R \(n("r"))  G \(n("g"))  B \(n("b"))  W \(n("w"))"
            return txt + (m.has("ctc_k") ? "  (fixture white \(n("ctc_k"))K)" : "")
        }
        if variant == "TW" {
            return "\(n("dim"))%  \(n("k"))K" + (m["boost"].truthy ? "  max output" : "")
        }
        return "\(n("dim"))%"
    }
}
