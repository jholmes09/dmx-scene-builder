import Foundation

/// The JSON API the web UI (web/app.js) calls, answered inside the app instead of by the
/// Python server on a Mac. Port of scenebuilder/server.py: same endpoints, response shapes,
/// refusal rules and error texts. Requests arrive through the WKURLSchemeHandler (app://local/...).
final class API {
    static let version = "1.0.0"

    struct Response {
        var status: Int
        var contentType: String
        var body: Data
        static func json(_ j: JSON, _ status: Int = 200) -> Response {
            Response(status: status, contentType: "application/json", body: j.data())
        }
        static func error(_ msg: String, _ status: Int = 400) -> Response { .json(["error": .string(msg)], status) }
    }

    let store: Store
    let ctl: ArtNetController
    let engine: Engine
    let webRoot: URL?
    private let scanLock = NSLock()
    var pauseDuringRdm = true
    private var identifyTimers: [String: DispatchWorkItem] = [:]
    private let timersLock = NSLock()
    private var reapDevices: [String: [String: JSON]] = [:]   // box ip -> {uid_str: device dict from the box's page}
    private let devLock = NSRecursiveLock()

    init(store: Store, ctl: ArtNetController, webRoot: URL?) {
        self.store = store
        self.ctl = ctl
        self.engine = Engine(store: store, ctl: ctl)
        self.webRoot = webRoot
    }

    func shutdown() {
        timersLock.lock(); identifyTimers.values.forEach { $0.cancel() }; identifyTimers.removeAll(); timersLock.unlock()
        engine.stop()
    }

    // ------------------------------------------------------------ device cache
    private func cache(_ ip: String) -> [String: JSON] { devLock.lock(); defer { devLock.unlock() }; return reapDevices[ip] ?? [:] }
    private func cacheUpdate(_ ip: String, _ devs: [JSON]) {
        devLock.lock(); defer { devLock.unlock() }
        var c = reapDevices[ip] ?? [:]
        for d in devs { c[Reap.uidToStr(d["d_uid"].pyStr)] = d }
        reapDevices[ip] = c
    }
    private func cacheSet(_ ip: String, _ uid: String, _ d: JSON) {
        devLock.lock(); reapDevices[ip, default: [:]][uid] = d; devLock.unlock()
    }

    // ------------------------------------------------------------ routing
    func handle(method: String, path: String, query: [String: String] = [:], body: Data?) -> Response {
        let parts = path.split(separator: "/").map(String.init)
        do {
            if parts.first != "api" {
                if method == "GET" && parts.count == 2 && parts[0] == "patch" { return patchSheet(parts[1]) }
                if method == "GET" { return staticFile(path) }
                return .error("not found", 404)
            }
            return try api(method, Array(parts.dropFirst()), query, body ?? Data())
        } catch let e as AppError {
            return .error(e.description, e.status)
        } catch {
            return .error("Internal error: \(error)", 500)
        }
    }

    private func staticFile(_ path: String) -> Response {
        guard let root = webRoot else { return .error("not found", 404) }
        var p = path
        if p.isEmpty || p == "/" { p = "/index.html" }
        let rel = p.split(separator: "/").map(String.init).filter { $0 != ".." && $0 != "." && !$0.isEmpty }
        let url = rel.reduce(root) { $0.appendingPathComponent($1) }
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), !isDir.boolValue,
              let data = try? Data(contentsOf: url) else { return .error("not found", 404) }
        return Response(status: 200, contentType: API.mimeType(url.pathExtension), body: data)
    }

    static func mimeType(_ ext: String) -> String {
        switch ext.lowercased() {
        case "html", "htm": return "text/html; charset=utf-8"
        case "js": return "text/javascript; charset=utf-8"
        case "css": return "text/css; charset=utf-8"
        case "json": return "application/json"
        case "webmanifest": return "application/manifest+json"
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "svg": return "image/svg+xml"
        case "woff2": return "font/woff2"
        case "woff": return "font/woff"
        case "txt": return "text/plain; charset=utf-8"
        default: return "application/octet-stream"
        }
    }

    private func parseBody(_ body: Data) throws -> JSON {
        if body.isEmpty { return [:] }
        let j = try JSON.parse(body)
        guard j.object != nil else { throw AppError.value("The request body must be a JSON object.") }
        return j
    }

    /// Python's b["key"]: a missing key is a KeyError (404 with the key in quotes).
    private func req(_ b: JSON, _ key: String) throws -> JSON {
        guard b.has(key) else { throw AppError.key(key) }
        return b[key]
    }

    private func api(_ method: String, _ p: [String], _ q: [String: String], _ rawBody: Data) throws -> Response {
        let eng = engine
        let body = { try self.parseBody(rawBody) }
        switch (method, p) {
        case ("GET", ["state"]):
            var snap = store.snapshot()
            snap["floats"] = .array(snap["floats"].arrayValue.map { fl in var f = fl; f["problems"] = Store.patchProblems(fl); return f })
            return .json(["version": .string(API.version), "project": snap, "engine": eng.status(),
                          "settings": ["pause_during_rdm": .bool(pauseDuringRdm)], "modes": Fixtures.modeCatalog(),
                          "sim": .null, "native": true])
        case ("GET", ["debug"]):
            return .json(["rdm_log": [], "raw_rdm": [], "packets_sent": .int(ctl.packetsSent), "packets_received": .int(ctl.packetsReceived)])
        case ("GET", ["status"]):
            return .json(["engine": eng.status(), "sim": .null, "rev": .int(store.rev)])
        case ("GET", ["network"]):
            let wifi = ArtNetController.sweepInterfaces().first
            return .json(["interfaces": .array(ArtNetController.localInterfaces().map { $0.toJSON() }), "http_port": .null,
                          "artnet_port": .int(ctl.port), "bind_error": JSON(ctl.bindError), "pinned_ip": .null,
                          "wifi": wifi.map { ["name": .string($0.name), "ip": .string($0.ip), "netmask": .string($0.netmask)] } ?? .null])
        case ("POST", ["network", "interface"]):
            throw AppError.value("The iPad uses its Wi-Fi for the boxes; there is no adapter to pick.")
        case ("POST", ["discover"]):
            let b = try body()
            var extra: [(String, Int)] = []
            for t in b["targets"].arrayValue where t["ip"].truthy {
                extra.append((t["ip"].pyStr, try t["udp_port"].or(6454).toInt()))
            }
            let wait = try b.has("wait") ? b["wait"].toDouble("wait") : 2.0
            return .json(["nodes": .array(ctl.poll(extraTargets: extra, wait: wait))])
        case ("POST", ["sim"]):
            if try body()["on"].truthy { throw AppError.value("The simulator isn't part of the iPad app.") }
            return .json(["sim": .null])
        case ("GET", ["folders"]):
            throw AppError.value("Folder browsing isn't available on the iPad. Use Export to keep a copy.")
        case ("GET", ["settings"]):
            return .json(settingsView())
        case ("POST", ["settings"]):
            let b = try body()
            if b.has("pause_during_rdm") { pauseDuringRdm = b["pause_during_rdm"].truthy }
            if b.has("mirror_dir"), !(b["mirror_dir"].string ?? "").trimmingCharacters(in: .whitespaces).isEmpty {
                throw AppError.value("Backup folders aren't used on the iPad. Use Export to keep a copy.")
            }
            return .json(settingsView())
        case ("POST", ["output"]):
            return try output(try body())
        case ("POST", ["white_cal"]):
            return try whiteCal(try body())
        case (_, _) where p.first == "palette":
            return try palette(method, Array(p.dropFirst()), rawBody)
        case ("GET", ["project"]):
            return .json(store.snapshot())
        case ("PUT", ["project"]):
            if eng.withLock({ eng.applying != nil }) { throw AppError.runtime("Lights are being changed right now. Try again in a moment.") }
            let b = try body()
            // Check the file before letting go of the lights: a bad file changes nothing.
            var probe = b
            guard probe["floats"].array != nil else { throw AppError.value("Not a DMX Scene Builder project file") }
            var floats = probe["floats"].arrayValue
            for i in floats.indices {
                for key in ["boxes", "fixtures", "looks"] { floats[i].setDefault(key, []) }
                try Store.validateFloat(&floats[i])
            }
            probe["floats"] = .array(floats)
            eng.setActive(nil)
            try store.replaceProject(b)
            eng.refresh()
            return .json(["ok": true])
        case ("POST", ["project", "merge_plan"]):
            return .json(try store.planMerge(try body()))
        case ("POST", ["project", "merge_apply"]):
            let b = try body()
            let res = b["resolutions"].or([:])
            if let a = eng.withLock({ eng.applying }), res[a].string == "theirs" {
                throw AppError.runtime("Lights on that float are being changed right now. Try again in a moment.")
            }
            if let act = eng.withLock({ eng.activeFloat }), res[act].string == "theirs" {
                eng.release()  // the other device's patch may not match these lights
            }
            let out = try store.applyMerge(b["incoming"].or([:]), res)
            eng.refresh()
            return .json(out)
        case ("POST", ["project", "name"]):
            let b = try body()
            store.withLock {
                store.data["project"] = .string(String(b["name"].or("Untitled").pyStr.prefix(80)))
                store.markDirty()
            }
            return .json(["ok": true])
        case ("POST", ["floats"]):
            let b = try body()
            let fl = Store.newFloat(code: b["code"].string ?? "", name: b["name"].truthy ? b["name"].pyStr : "New float")
            return .json(try store.putFloat(fl))
        default:
            break
        }
        if p.count >= 2 && p[0] == "floats" {
            return try floatEndpoint(method, p[1], Array(p.dropFirst(2)), rawBody)
        }
        throw AppError.key("no such endpoint")
    }

    private func settingsView() -> JSON {
        ["pause_during_rdm": .bool(pauseDuringRdm), "data_file": .string(store.path.path),
         "backups_dir": .string(store.backupsDir.path), "last_saved": .double(store.lastSaved),
         "mirror_dir": .null, "mirror_file": .null, "last_mirrored": 0, "mirror_error": .null,
         "save_error": JSON(store.saveError), "native": true]
    }

    // ------------------------------------------------------------ output
    private func output(_ b: JSON) throws -> Response {
        let eng = engine
        let action = b["action"].string
        if eng.withLock({ eng.applying != nil }), ["activate", "resume", "release", "white_test", "hold"].contains(action ?? "") {
            throw AppError.runtime("Lights are being changed right now. Wait for that to finish.")
        }
        switch action {
        case "activate":
            let fid = b["float_id"].string
            let (running, jobFloat, active) = eng.withLock { (eng.job["state"].string == "running", eng.job["float"].string, eng.activeFloat) }
            if running && jobFloat != fid { throw AppError.runtime("A save is running on another float. Wait for it to finish.") }
            if !(running && active == fid) {
                if let fid = fid, store.getFloat(fid) == nil { throw AppError.key("float not found") }
                eng.setActive(fid, output: true)
            }
        case "release": eng.release()
        case "resume": eng.resume()
        case "blackout": try eng.setBlackout(b.has("on") ? b["on"].truthy : true)
        case "white_test":
            let m = b["method"]
            try eng.setWhiteTest(m.truthy ? try m.toInt("method") : nil, k: try b["k"].or(6500).toInt("k"), mix: b["mix"])
        case "hold": eng.setHold(b.has("on") ? b["on"].truthy : true)
        default: throw AppError.value("unknown action")
        }
        return .json(["engine": eng.status()])
    }

    private func whiteCal(_ b: JSON) throws -> Response {
        let k = try b["k"].or(6500).toInt("k")
        guard (1800...10000).contains(k) else { throw AppError.value("Color temperature must be 1800-10000K.") }
        let mix: [Double]? = try {
            if b["reset_all"].truthy || b["delete"].truthy { return nil }
            let m = try req(b, "mix").arrayValue.map { max(0.0, min(1.0, try $0.toDouble("mix"))) }
            guard m.count == 4 else { throw AppError.value("mix must be [r, g, b, w]") }
            return m
        }()
        let cal: JSON = store.withLock {
            var wc = store.data["white_cal"].object ?? [:]
            var rgbw = wc["RGBW"]?.object ?? [:]
            if b["reset_all"].truthy { rgbw = [:] }
            else if b["delete"].truthy { rgbw.removeValue(forKey: String(k)) }
            else { rgbw[String(k)] = .array(mix!.map { .double($0) }) }
            wc["RGBW"] = .object(rgbw)
            store.data["white_cal"] = .object(wc)
            store.bump()
            return store.data["white_cal"]
        }
        engine.refresh()
        return .json(["white_cal": cal])
    }

    // ------------------------------------------------------------ palette (saved colours, shared by every float)
    private func palette(_ method: String, _ rest: [String], _ raw: Data) throws -> Response {
        if rest.isEmpty && method == "GET" { return .json(store.withLock { store.data["palette"].or([]) }) }
        if rest.isEmpty && method == "POST" {
            let b = try parseBody(raw)
            let st = b["state"].or([:])
            var keep: [String: JSON] = [:]
            for k in ["kind", "cct", "hue", "sat", "white", "dim"] where st.has(k) { keep[k] = st[k] }
            if b["include_dim"] == .bool(false) { keep.removeValue(forKey: "dim") }
            let variant = keep["kind"]?.string == "color" ? "RGBW" : "TW"
            var state = try Fixtures.normalizeState(.object(keep), variant).toJSON()
            let includeDim = keep["dim"] != nil
            if !includeDim { state.remove("dim") }
            let name = b["name"].truthy ? b["name"].pyStr.trimmingCharacters(in: .whitespacesAndNewlines) : "Color"
            let entry: JSON = ["id": .string(Store.newId("col")), "name": .string(String(name.prefix(40))), "state": state,
                               "include_dim": .bool(includeDim), "created": .double(Date().timeIntervalSince1970)]
            store.withLock {
                store.data["palette"] = .array(store.data["palette"].arrayValue + [entry])
                store.bump()
            }
            return .json(entry)
        }
        if rest.count == 1 {
            return try store.withLock {
                var pal = store.data["palette"].arrayValue
                guard let i = pal.firstIndex(where: { $0["id"].string == rest[0] }) else { throw AppError.key("color not found") }
                if method == "DELETE" {
                    pal.remove(at: i)
                    store.data["palette"] = .array(pal)
                    store.bump()
                    return .json(["ok": true])
                }
                if method == "PUT" {
                    let b = try parseBody(raw)
                    if b["name"].truthy {
                        pal[i]["name"] = .string(String(b["name"].pyStr.trimmingCharacters(in: .whitespacesAndNewlines).prefix(40)))
                    }
                    store.data["palette"] = .array(pal)
                    store.markDirty()
                    return .json(pal[i])
                }
                throw AppError.key("no such endpoint")
            }
        }
        throw AppError.key("no such endpoint")
    }

    // ------------------------------------------------------------ floats
    private func floatEndpoint(_ method: String, _ fid: String, _ rest: [String], _ raw: Data) throws -> Response {
        let eng = engine
        guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
        let body = { try self.parseBody(raw) }
        switch (method, rest) {
        case ("GET", []):
            return .json(fl)
        case ("PUT", []):
            if eng.withLock({ eng.applying }) == fid {
                return .error("Lights on this float are being changed right now. Try again in a moment.", 409)
            }
            var b = try body()
            let saved: JSON = try store.withLock {
                let cur = store.getFloat(fid) ?? fl
                if b.has("rev") && b["rev"] != (cur.has("rev") ? cur["rev"] : .int(0)) {
                    throw AppError.conflict("This float was changed on another device. Reloading it now.")
                }
                b["id"] = .string(fid)
                let ids = Set(b["fixtures"].arrayValue.map { $0["id"].pyStr })
                b["live"] = .object(cur["live"].objectValue.filter { ids.contains($0.key) })
                b["looks"] = cur["looks"].or([])
                b["rev"] = .int((cur["rev"].int ?? 0) + 1)
                return try store.putFloat(b)
            }
            eng.refresh()
            return .json(saved)
        case ("DELETE", []):
            if eng.withLock({ eng.activeFloat }) == fid { eng.setActive(nil) }
            store.deleteFloat(fid)
            return .json(["ok": true])
        case ("POST", ["live"]):
            try eng.updateLive(fid, try body()["changes"].or([:]))
            return .json(["ok": true])
        case ("POST", ["looks"]):
            let b = try body()
            let name = b["name"].truthy ? b["name"].pyStr.trimmingCharacters(in: .whitespacesAndNewlines) : "Look"
            var look: JSON = [:]
            store.withLock {
                store.updateFloat(fid) { f in
                    look = ["id": .string(Store.newId("lk")), "name": .string(String(name.prefix(60))),
                            "created": .double(Date().timeIntervalSince1970), "states": f["live"].or([:])]
                    f["looks"] = .array(f["looks"].arrayValue + [look])
                }
                store.bump(fid)
            }
            return .json(look)
        case ("POST", let r) where r.count == 3 && r[0] == "looks" && r[2] == "recall":
            try store.withLock {
                try store.updateFloat(fid) { f in
                    guard let look = f["looks"].arrayValue.first(where: { $0["id"].string == r[1] }) else { throw AppError.key("look not found") }
                    f["live"] = look["states"]
                }
                store.markDirty(fid)
            }
            eng.refresh()
            return .json(["ok": true])
        case ("DELETE", let r) where r.count == 2 && r[0] == "looks":
            store.withLock {
                store.updateFloat(fid) { f in f["looks"] = .array(f["looks"].arrayValue.filter { $0["id"].string != r[1] }) }
                store.bump(fid)
            }
            return .json(["ok": true])
        case ("PUT", let r) where r.count == 2 && r[0] == "looks":
            let b = try body()
            var out: JSON = .null
            try store.withLock {
                try store.updateFloat(fid) { f in
                    var looks = f["looks"].arrayValue
                    guard let i = looks.firstIndex(where: { $0["id"].string == r[1] }) else { throw AppError.key("look not found") }
                    if b.has("name") {
                        let n = b["name"].pyStr.trimmingCharacters(in: .whitespacesAndNewlines)
                        if !n.isEmpty { looks[i]["name"] = .string(String(n.prefix(60))) }
                    }
                    if b["overwrite"].truthy { looks[i]["states"] = f["live"].or([:]) }
                    f["looks"] = .array(looks)
                    out = looks[i]
                }
                store.markDirty(fid)
            }
            return .json(out)
        case ("POST", ["flash"]):
            let b = try body()
            eng.flashFixture(try req(b, "fixture_id").pyStr, seconds: try b["seconds"].or(6).toDouble("seconds"))
            return .json(["ok": true])
        case ("POST", ["scan"]):
            let b = try body()
            return .json(try scan(fid, try req(b, "box_id").pyStr))
        case ("POST", ["box"]):
            return try boxAction(fl, try body())
        case ("POST", ["rdm"]):
            return try rdmAction(fid, try body())
        case ("POST", ["sweep"]):
            let b = try body()
            if b["action"].isNull || b["action"].string == "start" {
                return .json(try eng.startSweep(fid, boxId: try req(b, "box_id").pyStr, variant: b["variant"].string ?? "TW",
                                                mode: try b["mode"].or(11).toInt("mode"), start: try b["start"].or(1).toInt("start"),
                                                count: try b["count"].or(24).toInt("count"), seconds: try b["seconds"].or(2.5).toDouble("seconds")))
            }
            eng.sweepControl(try req(b, "action").pyStr)
            return .json(["engine": eng.status()])
        case ("POST", ["autopatch"]):
            let b = try body()
            let res = try autopatch(fid, boxId: b["box_id"].string, start: try b["start"].or(1).toInt("start"),
                                    fixtureIds: b["fixture_ids"].array?.map { $0.pyStr }, gap: try b["gap"].or(0).toInt("gap"))
            eng.refresh()
            return .json(res)
        case ("POST", ["save_rdm"]):
            throw AppError.runtime("Saving over RDM isn't part of the iPad app. Use Save look (Mode 7).")
        case ("POST", ["apply"]):
            return .json(try applyToLights(fid, try body()))
        case ("POST", ["save"]):
            let b = try body()
            let verify = b["verify"].truthy
            for box in fl["boxes"].arrayValue where box["ip"].truthy && !verify {
                // A box on "Play saved look" ignores the app: saving would do nothing.
                let reap = Reap(box["ip"].pyStr)
                if !reap.available() {
                    if !ctl.alive(box["ip"].pyStr, box["udp_port"].or(6454).int ?? 6454) {
                        throw AppError.runtime("\(box["name"].pyStr) isn't answering. Check its power and that the iPad is on the same network, then try again.")
                    }
                    continue  // answers Art-Net but has no web page (other gear)
                }
                if try reap.otherSettings()["ic_od"].string == "disabled" {
                    throw AppError.runtime("\(box["name"].pyStr) is on 'Play saved look'. Switch it back to app control first.")
                }
            }
            return .json(try eng.startSave(fid, b["fixture_ids"].arrayValue.map { $0.pyStr }, verifyOnly: verify))
        default:
            throw AppError.key("no such endpoint")
        }
    }

    // ------------------------------------------------------------ helpers
    func boxTarget(_ fl: JSON, _ boxId: String) throws -> Target {
        for b in fl["boxes"].arrayValue where b["id"].pyStr == boxId {
            if !b["ip"].truthy { throw AppError.value("Set the box's IP address first.") }
            let pa = try ArtNet.portAddress(b["net"].int ?? 0, b["subnet"].int ?? 0, b["universe"].int ?? 0)
            return Target(ip: b["ip"].pyStr, port: b["udp_port"].or(6454).int ?? 6454, pa: pa)
        }
        throw AppError.key("box not found")
    }

    /// Talk to fixtures one conversation at a time, with output paused (Python App.rdm_session).
    func rdmSession<T>(_ body: () throws -> T) throws -> T {
        guard scanLock.lock(before: Date().addingTimeInterval(3)) else {
            throw AppError.runtime("Busy talking to the box. Try again in a moment.")
        }
        do {
            if pauseDuringRdm { try engine.pauseForRdm() }
            else if engine.jobRunning { throw AppError.runtime("A save is running. Wait for it to finish.") }
        } catch {
            scanLock.unlock()
            throw error
        }
        defer {
            if pauseDuringRdm { engine.resumeAfterRdm() }
            scanLock.unlock()
        }
        return try body()
    }

    private func reachError(_ ip: String) -> AppError {
        .runtime("Can't reach the box's web page at \(ip). Check the box is powered and the iPad is on the venue Wi-Fi, then try again.")
    }

    static func reapVariant(_ mode: Int) -> String? {
        if mode == 11 || mode == 12 { return "TW" }
        if mode == 13 { return "PW" }
        if mode == Fixtures.saveMode { return nil }  // Mode 7 exists on RGBW and TW: keep the patch's type
        return "RGBW"
    }

    static func reapScanEntry(_ d: JSON) -> JSON {
        let mode = (try? d["dmx_p"].or(1).toInt()) ?? 1
        let variant = reapVariant(mode)
        var avail = Set(Fixtures.modesOf(variant ?? "RGBW"))
        if variant == "PW" { avail.insert(Fixtures.saveMode) }
        let fpVariant = (variant != nil && Fixtures.hasMode(variant!, mode)) ? variant! : "RGBW"
        let fp = try? Fixtures.footprint(fpVariant, mode)
        return ["uid": .string(Reap.uidToStr(d["d_uid"].pyStr)), "ok": true,
                "address": .int(((try? d["dmx_a"].or(0).toInt()) ?? 0) + 1), "mode": .int(mode),
                "personality": .int(mode), "personality_count": d["dmx_p_c"], "footprint": JSON(fp),
                "label": .string((d["u_l"].string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)),
                "model": d["d_l"].or(""), "variant_guess": JSON(variant),
                "modes_available": .array(avail.sorted().map { .int($0) }), "kind": "fixture", "via": "reap",
                "terminator": d["dmx_t"]]
    }

    // ------------------------------------------------------------ scan
    func scan(_ fid: String, _ boxId: String) throws -> JSON {
        guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
        let t = try boxTarget(fl, boxId)
        let reap = Reap(t.ip)
        guard reap.available() else { throw reachError(t.ip) }
        let expected = fl["fixtures"].arrayValue.filter { $0["box_id"].pyStr == boxId }.count
        var found: [String: JSON] = [:]
        var order: [String] = []
        try rdmSession {
            for _ in 0..<2 {  // the box's search sometimes misses a light; one retry fills the gaps
                for d in try reap.discover() {
                    let k = d["d_uid"].pyStr
                    if found[k] == nil { order.append(k) }
                    found[k] = d
                }
                if found.count >= expected { break }
            }
        }
        let devs = order.map { found[$0]! }
        devLock.lock()
        reapDevices[t.ip] = Dictionary(devs.map { (Reap.uidToStr($0["d_uid"].pyStr), $0) }, uniquingKeysWith: { _, b in b })
        let known = reapDevices[t.ip]!
        devLock.unlock()
        // A light left "unknown" (address cleared) by a failed change gets its real values back.
        store.withLock {
            var changed = 0
            store.updateFloat(fid) { f in
                var fxs = f["fixtures"].arrayValue
                for i in fxs.indices {
                    guard let d = known[fxs[i]["uid"].string ?? ""], fxs[i]["box_id"].pyStr == boxId, fxs[i]["address"].isNull else { continue }
                    fxs[i]["address"] = .int(((try? d["dmx_a"].or(0).toInt()) ?? 0) + 1)
                    let m = (try? d["dmx_p"].or(1).toInt()) ?? 1
                    if Fixtures.hasMode(fxs[i]["variant"].string ?? "", m) || m == Fixtures.saveMode { fxs[i]["mode"] = .int(m) }
                    changed += 1
                }
                f["fixtures"] = .array(fxs)
                if changed > 0 { f["rev"] = .int((f["rev"].int ?? 0) + changed) }
            }
            if changed > 0 { store.bump(fid) }
        }
        let sorted = devs.enumerated().sorted {
            (($0.element["dmx_a"].int ?? 0), $0.offset) < (($1.element["dmx_a"].int ?? 0), $1.offset)
        }.map { API.reapScanEntry($0.element) }
        return ["box_id": .string(boxId), "ip": .string(t.ip), "port_address": .int(t.pa), "devices": .array(sorted),
                "via": "box web page", "time": .double(Date().timeIntervalSince1970)]
    }

    // ------------------------------------------------------------ one light through the box's page
    private func rdmAction(_ fid: String, _ b: JSON) throws -> Response {
        guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
        let t = try boxTarget(fl, try req(b, "box_id").pyStr)
        let uid = try req(b, "uid").pyStr
        var dev = cache(t.ip)[uid]
        if dev == nil {
            let reap = Reap(t.ip)
            guard reap.available() else { throw reachError(t.ip) }
            // Not known yet (app restarted, or the box's search missed it last time): search again.
            let devs = try rdmSession { try reap.discover() }
            cacheUpdate(t.ip, devs)
            dev = cache(t.ip)[uid]
            if dev == nil { throw AppError.value("That light didn't answer the box's search. Try again in a moment.") }
        }
        return try reapAction(fid, b, t.ip, dev!)
    }

    private func reapAction(_ fid: String, _ b: JSON, _ ip: String, _ devIn: JSON) throws -> Response {
        let reap = Reap(ip)
        let act = try req(b, "action").pyStr
        let uid = try req(b, "uid").pyStr
        let dUid = Reap.uidFromStr(uid)
        var dev = devIn
        if ["address", "mode", "label", "set_param"].contains(act) {
            // Every box write resends address, mode and label together: use the light's current
            // values, not what we saw at the last Scan (someone may have changed it since).
            let fresh = try reap.discover(timeout: 20)
            cacheUpdate(ip, fresh)
            guard let d = fresh.first(where: { Reap.uidToStr($0["d_uid"].pyStr) == uid }) else {
                throw AppError.value("That light didn't answer the box's search. Try again.")
            }
            dev = d
        }
        var label = (dev["u_l"].string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        var addr = ((try? dev["dmx_a"].or(0).toInt()) ?? 0) + 1
        var mode = (try? dev["dmx_p"].or(1).toInt()) ?? 1
        let early: Response? = try rdmSession {
            switch act {
            case "identify":
                let on = b.has("on") ? b["on"].truthy : true
                try reap.identify(dUid, on: on)
                let key = "\(ip)|\(dUid)"
                timersLock.lock()
                identifyTimers.removeValue(forKey: key)?.cancel()
                if on {
                    let item = DispatchWorkItem { [weak self] in
                        guard let self = self else { return }
                        self.scanLock.lock()
                        _ = try? reap.identify(dUid, on: false)
                        self.scanLock.unlock()
                    }
                    identifyTimers[key] = item
                    DispatchQueue.global().asyncAfter(deadline: .now() + 15.0, execute: item)
                }
                timersLock.unlock()
            case "address":
                addr = try req(b, "address").toInt("address")
                try reap.setup(dUid, label: label, address: addr, mode: mode, device: dev)
                dev["dmx_a"] = .int(addr - 1)
                writeBack(fid, uid, address: .int(addr))
            case "mode":
                mode = try req(b, "mode").toInt("mode")
                try reap.setup(dUid, label: label, address: addr, mode: mode, device: dev)
                dev["dmx_p"] = .int(mode)
                writeBack(fid, uid, mode: mode)
            case "label":
                label = String(try req(b, "label").pyStr.prefix(32))
                try reap.setup(dUid, label: label, address: addr, mode: mode, device: dev)
                dev["u_l"] = .string(label)
            case "params":
                var params: [JSON] = []
                if ((try? dev["sp"].or(0).toInt()) ?? 0) & 1 != 0 {
                    params.append(["pid": 1, "pid_hex": "box", "description": "DMX terminator (last light on a cable)",
                                   "size": 1, "data_type": "uint8", "command_class": "GET_SET", "can_get": true,
                                   "can_set": true, "min": 0, "max": 1, "default": 0,
                                   "value": .int(dev["dmx_t"].string == "on" ? 1 : 0)])
                }
                return .json(["ok": true, "uid": .string(uid), "params": .array(params), "save_pid": .null])
            case "set_param":
                if try req(b, "pid").toInt("pid") != 1 { throw AppError.value("That setting isn't available through this box.") }
                let on = try req(b, "value").toInt("value") != 0
                try reap.setup(dUid, label: label, address: addr, mode: mode, device: dev, terminator: on)
                dev["dmx_t"] = .string(on ? "on" : "off")
            case "info":
                break
            default:
                throw AppError.value("unknown RDM action")
            }
            return nil
        }
        if let r = early { return r }
        cacheSet(ip, uid, dev)
        engine.refresh()
        try? store.flush()
        let info: JSON = ["address": .int(((try? dev["dmx_a"].or(0).toInt()) ?? 0) + 1),
                          "mode": .int((try? dev["dmx_p"].or(1).toInt()) ?? 1),
                          "personality": .int((try? dev["dmx_p"].or(1).toInt()) ?? 1)]
        return .json(["ok": true, "uid": .string(uid), "info": info, "rev": .int(store.rev)])
    }

    /// Keep the patch in step with the hardware the moment a change succeeds.
    /// address: .null = unknown (the light isn't driven until it is scanned again).
    func writeBack(_ fid: String, _ uid: String, address: JSON? = nil, mode: Int? = nil) {
        store.withLock {
            store.updateFloat(fid) { f in
                var fxs = f["fixtures"].arrayValue
                for i in fxs.indices where fxs[i]["uid"].string == uid {
                    if let m = mode, Fixtures.hasMode(fxs[i]["variant"].string ?? "", m) || m == Fixtures.saveMode {
                        fxs[i]["mode"] = .int(m)
                    }
                    if let a = address { fxs[i]["address"] = a }
                }
                f["fixtures"] = .array(fxs)
                f["rev"] = .int((f["rev"].int ?? 0) + 1)
            }
            store.bump(fid)
        }
    }

    // ------------------------------------------------------------ box-wide settings
    private func boxAction(_ fl: JSON, _ b: JSON) throws -> Response {
        let t = try boxTarget(fl, try req(b, "box_id").pyStr)
        let reap = Reap(t.ip)
        if b["action"].string == "output_data" {
            engine.release()
            try reap.setOther(outputData: try req(b, "enabled").truthy)
            reap.restart()
            return .json(["ok": true, "restarting": true])
        }
        let s = try reap.otherSettings()
        return .json(["ok": true, "output_data": s["ic_od"], "mode": s["ebm"], "dmx_hold": s["dmxh"]])
    }

    // ------------------------------------------------------------ autopatch
    /// Give the chosen fixtures consecutive blocks by footprint, in list order, skipping over
    /// channels used by fixtures on the same box that are not being re-addressed. Pure.
    static func autopatchCore(_ fixtures: [JSON], boxId: String?, start: Int, fixtureIds: [String]?, gap: Int) throws -> [(String, Int)] {
        let bid = (boxId?.isEmpty ?? true) ? nil : boxId
        let ids = (fixtureIds?.isEmpty ?? true) ? nil : Set(fixtureIds!)
        let chosen = fixtures.filter { (bid == nil || $0["box_id"].string == bid) && (ids == nil || ids!.contains($0["id"].pyStr)) }
        let chosenIds = Set(chosen.map { $0["id"].pyStr })
        var taken: [(Int, Int)] = []
        for fx in fixtures {
            if chosenIds.contains(fx["id"].pyStr) || !fx["address"].truthy { continue }
            if !chosen.isEmpty && fx["box_id"] != chosen[0]["box_id"] && bid != nil { continue }
            if bid != nil && fx["box_id"].string != bid { continue }
            let a = fx["address"].int ?? 0
            taken.append((a, a + (try Store.fixtureFootprint(fx)) - 1))
        }
        var addr = max(1, start)
        var changed: [(String, Int)] = []
        for fx in chosen {
            let fp = try Store.fixtureFootprint(fx)
            var moved = true
            while moved {
                moved = false
                for (a0, a1) in taken where addr <= a1 && addr + fp - 1 >= a0 {
                    addr = a1 + 1
                    moved = true
                }
            }
            if addr + fp - 1 > 512 { throw AppError.value("Ran out of channels at \(fx["label"].pyStr)") }
            changed.append((fx["id"].pyStr, addr))
            addr += fp + max(0, gap)
        }
        return changed
    }

    /// {fixture id: address} for the chosen fixtures; `modes` overrides modes for the plan only.
    static func planAddresses(_ fl: JSON, boxId: String?, start: Int = 1, fixtureIds: [String]? = nil, gap: Int = 0,
                              modes: [String: Int]? = nil) throws -> [(String, Int)] {
        let fake = fl["fixtures"].arrayValue.map { fx -> JSON in
            var f = fx
            if let m = modes?[fx["id"].pyStr] { f["mode"] = .int(m) }
            return f
        }
        return try autopatchCore(fake, boxId: boxId, start: start, fixtureIds: fixtureIds, gap: gap)
    }

    func autopatch(_ fid: String, boxId: String?, start: Int, fixtureIds: [String]?, gap: Int) throws -> JSON {
        try store.withLock {
            guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
            let changed = try API.autopatchCore(fl["fixtures"].arrayValue, boxId: boxId, start: start, fixtureIds: fixtureIds, gap: gap)
            let byId = Dictionary(changed, uniquingKeysWith: { _, b in b })
            store.updateFloat(fid) { f in
                var fxs = f["fixtures"].arrayValue
                for i in fxs.indices { if let a = byId[fxs[i]["id"].pyStr] { fxs[i]["address"] = .int(a) } }
                f["fixtures"] = .array(fxs)
                f["rev"] = .int((f["rev"].int ?? 0) + 1)
            }
            store.bump(fid)
            return ["changed": .array(changed.map { ["id": .string($0.0), "address": .int($0.1)] })]
        }
    }

    // ------------------------------------------------------------ write modes/addresses to the lights
    /// body: {"to_mode7": [fixture ids]} or {"push": true} (write the patch as it stands).
    /// Output goes live on this float and is held at zero first, so no light ever reads a stale
    /// byte on its save channel while layouts change. The store records a light's new
    /// mode/address only after that light confirms it. On any failure the hold stays on.
    func applyToLights(_ fid: String, _ b: JSON) throws -> JSON {
        let eng = engine
        guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
        let fxs = fl["fixtures"].arrayValue.filter { $0["uid"].truthy && $0["address"].truthy }
        var targets: [(String, Int, Int)] = []  // fixture id, address, mode (in order)
        if b["to_mode7"].truthy {
            let ids = Set(b["to_mode7"].arrayValue.map { $0.pyStr })
            let chosen = fxs.filter { ids.contains($0["id"].pyStr) }
            var boxOrder: [String] = []
            for fx in chosen where !boxOrder.contains(fx["box_id"].pyStr) { boxOrder.append(fx["box_id"].pyStr) }
            for boxId in boxOrder {
                let plan = try API.planAddresses(fl, boxId: boxId, start: 1,
                                                 fixtureIds: chosen.filter { $0["box_id"].pyStr == boxId }.map { $0["id"].pyStr },
                                                 modes: Dictionary(chosen.map { ($0["id"].pyStr, Fixtures.saveMode) }, uniquingKeysWith: { a, _ in a }))
                targets += plan.map { ($0.0, $0.1, Fixtures.saveMode) }
            }
        } else {
            targets = fxs.map { ($0["id"].pyStr, $0["address"].int ?? 0, $0["mode"].int ?? 0) }
        }
        let byId = Dictionary(fl["fixtures"].arrayValue.map { ($0["id"].pyStr, $0) }, uniquingKeysWith: { a, _ in a })
        if targets.isEmpty { throw AppError.value("No lights to change. Scan the box first so the app knows which light is which.") }
        guard scanLock.lock(before: Date().addingTimeInterval(3)) else {
            throw AppError.runtime("Busy talking to the box. Try again in a moment.")
        }
        let busy = eng.withLock { eng.applying != nil || eng.job["state"].string == "running" }
        if busy {
            scanLock.unlock()
            throw AppError.runtime("Busy. Try again in a moment.")
        }
        var done: [JSON] = [], errors: [JSON] = []
        defer {
            eng.withLock { eng.applying = nil }
            scanLock.unlock()
            try? store.flush()  // the lights already changed: get it on disk now
        }
        eng.withLock {
            eng.applying = fid
            eng.hold = true                                   // zeros from the very first frame...
        }
        eng.setActive(fid, output: true, keepHold: true)      // ...then go live (still held)
        Thread.sleep(forTimeInterval: 0.4)                    // let a few all-zero frames reach the box
        for box in fl["boxes"].arrayValue {
            let mine = targets.filter { byId[$0.0]?["box_id"] == box["id"] }
            if mine.isEmpty || !box["ip"].truthy { continue }
            let name = box["name"].pyStr
            let ip = box["ip"].pyStr
            let reap = Reap(ip)
            if !reap.available() {
                errors.append(.string("\(name): the box's web page didn't answer."))
                continue
            }
            let want = Set(mine.map { byId[$0.0]!["uid"].pyStr })
            do {
                for _ in 0..<2 {  // the box's search sometimes misses a light
                    cacheUpdate(ip, try reap.discover(timeout: 20))
                    if want.isSubset(of: Set(cache(ip).keys)) { break }
                }
            } catch {
                errors.append(.string("\(name): \(error)"))
                continue
            }
            var failed: [JSON] = []
            var boxGone = false
            for (n, (tid, addr, mode)) in mine.enumerated() {
                let fx = byId[tid]!
                let uid = fx["uid"].pyStr
                guard var dev = cache(ip)[uid] else {
                    errors.append(.string("\(fx["label"].pyStr): didn't answer the box's search."))
                    continue
                }
                if ((try? dev["dmx_a"].or(-1).toInt()) ?? -1) + 1 == addr && ((try? dev["dmx_p"].or(0).toInt()) ?? 0) == mode {
                    writeBack(fid, uid, address: .int(addr), mode: mode)
                    done.append(fx["label"])
                    continue
                }
                do {
                    try reap.setup(Reap.uidFromStr(uid), label: (dev["u_l"].string ?? "").trimmingCharacters(in: .whitespacesAndNewlines),
                                   address: addr, mode: mode, device: dev)
                } catch {
                    errors.append(.string("\(fx["label"].pyStr): \(error)"))
                    failed.append(fx)
                    if "\(error)".contains("Can't reach") {  // the box is gone: don't wait on every remaining light
                        boxGone = true
                        for rest in mine[(n + 1)...] {
                            errors.append(.string("\(byId[rest.0]!["label"].pyStr): skipped, the box stopped answering."))
                            failed.append(byId[rest.0]!)
                        }
                        break
                    }
                    continue
                }
                dev["dmx_a"] = .int(addr - 1)
                dev["dmx_p"] = .int(mode)
                cacheSet(ip, uid, dev)
                writeBack(fid, uid, address: .int(addr), mode: mode)
                done.append(fx["label"])
            }
            if !failed.isEmpty {
                // A light whose change wasn't confirmed may or may not have switched. Ask the box
                // what it really is now; if it can't say, forget that light's address so the app
                // never drives it with a guessed layout (it shows as unaddressed until re-scanned).
                var actual: [String: JSON] = [:]
                if !boxGone && reap.available(), let devs = try? reap.discover(timeout: 15) {
                    cacheUpdate(ip, devs)
                    for d in devs { actual[Reap.uidToStr(d["d_uid"].pyStr)] = d }
                }
                for fx in failed {
                    let uid = fx["uid"].pyStr
                    if let d = actual[uid] {
                        writeBack(fid, uid, address: .int(((try? d["dmx_a"].or(0).toInt()) ?? 0) + 1),
                                  mode: (try? d["dmx_p"].or(1).toInt()) ?? 1)
                    } else {
                        writeBack(fid, uid, address: .null)
                    }
                }
            }
        }
        if errors.isEmpty {
            eng.withLock { eng.applying = nil }
            eng.setHold(false)
        }
        return ["ok": .bool(errors.isEmpty), "done": .array(done), "errors": .array(errors), "held": .bool(!errors.isEmpty)]
    }

    // ------------------------------------------------------------ patch sheet
    private func patchSheet(_ fid: String) -> Response {
        let (project, fl, cal) = store.withLock { (store.data["project"].or("").pyStr, store.getFloat(fid), store.data["white_cal"]["RGBW"]) }
        guard let f = fl else { return Response(status: 404, contentType: "text/html; charset=utf-8", body: Data("<h1>Not found</h1>".utf8)) }
        return Response(status: 200, contentType: "text/html; charset=utf-8", body: Data(PatchSheet.render(project: project, fl: f, cal: cal).utf8))
    }
}
