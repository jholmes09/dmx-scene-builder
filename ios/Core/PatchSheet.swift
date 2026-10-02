import Foundation

/// Printable per-float patch sheet (light theme for paper). Port of scenebuilder/patchsheet.py.
enum PatchSheet {
    static func e(_ x: String) -> String {
        x.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
            .replacingOccurrences(of: "'", with: "&#x27;")
    }
    static func e(_ j: JSON) -> String { j.isNull ? "" : e(j.pyStr) }

    static func describeState(_ fx: JSON, _ st: JSON, _ cal: JSON) -> String {
        let v = Store.effectiveVariant(fx)
        return (try? Fixtures.describeMix(v, fx["mode"].int ?? 0, st.or([:]), cal: v == "RGBW" ? cal : .null)) ?? ""
    }

    static func render(project: String, fl: JSON, cal: JSON) -> String {
        let boxes = Dictionary(fl["boxes"].arrayValue.map { ($0["id"].pyStr, $0) }, uniquingKeysWith: { a, _ in a })
        let sorted = fl["fixtures"].arrayValue.sorted { a, b in
            let ka = (a["box_id"].string ?? "", a["address"].int ?? 9999, a["label"].pyStr)
            let kb = (b["box_id"].string ?? "", b["address"].int ?? 9999, b["label"].pyStr)
            return ka < kb
        }
        var rows = ""
        for fx in sorted {
            let b = boxes[fx["box_id"].pyStr] ?? [:]
            let fp = (try? Store.fixtureFootprint(fx)) ?? 0
            let a = fx["address"].int
            let chans = (a != nil && a != 0) ? "\(a!)-\(a! + fp - 1)" : "not set"
            let variant = fx["variant"].pyStr
            rows += "<tr><td>\(e(fx["label"]))</td><td>\(e(Fixtures.variantLabels[variant] ?? variant))</td><td>\(e(fx["mode"]))</td>"
            rows += "<td>\(e(chans))</td><td>\(e(b["name"].or("")))</td><td>\(e(fx["uid"].or("")))</td>"
            rows += "<td>\(e(describeState(fx, fl["live"][fx["id"].pyStr], cal)))</td><td>\(e(fx["notes"].or("")))</td></tr>"
        }
        let boxRows = fl["boxes"].arrayValue.map { b in
            "<tr><td>\(e(b["name"]))</td><td>\(e(b["ip"].or("not set")))</td><td>\(b["net"].int ?? 0) : \(b["subnet"].int ?? 0) : \(b["universe"].int ?? 0)</td><td>\(e(b["notes"].or("")))</td></tr>"
        }.joined()
        var probs = Store.patchProblems(fl).arrayValue.map { "<li>\(e($0["text"]))</li>" }.joined()
        if probs.isEmpty { probs = "<li>None</li>" }
        var looks = fl["looks"].arrayValue.map { "<li>\(e($0["name"]))</li>" }.joined()
        if looks.isEmpty { looks = "<li>None saved</li>" }
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd HH:mm"
        let code = e(fl["code"]), name = e(fl["name"])
        return """
<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>\(code) \(name) patch</title>
<style>
@font-face{font-family:Inter;src:url(/fonts/inter-latin-400-normal.woff2) format('woff2')}
@font-face{font-family:Playfair;font-weight:900;src:url(/fonts/playfair-display-latin-900-normal.woff2) format('woff2')}
@font-face{font-family:Cinzel;font-weight:700;src:url(/fonts/cinzel-latin-700-normal.woff2) format('woff2')}
body{font:12px Inter,system-ui,sans-serif;color:#070605;background:#F6F0E1;margin:24px}
h1{font:900 28px Playfair,serif;margin:0}
.k{font:700 10px Cinzel,serif;letter-spacing:.2em;color:#9A7430;text-transform:uppercase}
table{border-collapse:collapse;width:100%;margin:10px 0 18px}
th,td{border-bottom:1px solid #9A743055;padding:5px 6px;text-align:left;vertical-align:top}
th{font:700 9px Cinzel,serif;letter-spacing:.14em;color:#9A7430;text-transform:uppercase;border-bottom:1.5px solid #9A7430}
@media print{body{margin:10mm}}
</style></head><body>
<div class="k">\(e(project)) &middot; DMX Scene Builder patch sheet</div>
<h1>\(code) \(name)</h1>
<div class="k" style="margin:6px 0 14px">Printed \(fmt.string(from: Date()))</div>
<div class="k">Boxes (Art-Net Net : Sub-Net : Universe)</div>
<table><tr><th>Box</th><th>IP</th><th>Art-Net</th><th>Notes</th></tr>\(boxRows)</table>
<div class="k">Fixtures</div>
<table><tr><th>Fixture</th><th>Type</th><th>Mode</th><th>Channels</th><th>Box</th><th>RDM UID</th><th>Live look</th><th>Notes</th></tr>\(rows)</table>
<div class="k">Patch problems</div><ul>\(probs)</ul>
<div class="k">Saved looks</div><ul>\(looks)</ul>
</body></html>
"""
    }
}
