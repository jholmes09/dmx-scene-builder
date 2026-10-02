import Foundation

/// Persistent project data: floats, boxes, fixtures, looks. JSON on disk with rolling backups.
/// Port of scenebuilder/store.py. On the iPad the folder is the app's Documents folder, so the
/// project file and its backups are reachable in the Files app.
final class Store {
    static let schema = 1
    /// Stamped on every edited float ("edited_on"), so Import can say which copy is whose.
    static var hostname: String = {
        let h = ProcessInfo.processInfo.hostName.split(separator: ".").first.map(String.init) ?? "iPad"
        return h.isEmpty || h == "localhost" ? "iPad" : h
    }()

    let dir: URL
    let path: URL
    let backupsDir: URL
    let lock = NSRecursiveLock()
    var data: JSON
    private var dirty = false
    private var lastBackup = 0.0
    private(set) var lastSaved = 0.0
    private var timer: DispatchSourceTimer?
    /// The last time the background saver failed, and why (shown in Setup).
    private(set) var saveError: String?

    init(directory: URL, autosave: Bool = true) throws {
        dir = directory
        path = dir.appendingPathComponent("project.json")
        backupsDir = dir.appendingPathComponent("backups")
        try FileManager.default.createDirectory(at: backupsDir, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: path.path) {
            let raw = try Data(contentsOf: path)
            data = try JSON.parse(raw)
            if data.object == nil { throw AppError.value("The saved project file is damaged: \(path.path)") }
        } else {
            data = Store.emptyProject()
        }
        migrate()
        if autosave {
            let t = DispatchSource.makeTimerSource(queue: DispatchQueue.global(qos: .utility))
            t.schedule(deadline: .now() + 1, repeating: 1.0)
            t.setEventHandler { [weak self] in
                do { try self?.flush() } catch { self?.saveError = "\(error)" }
            }
            t.resume()
            timer = t
        }
    }

    deinit { timer?.cancel() }

    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock(); defer { lock.unlock() }
        return try body()
    }

    // ------------------------------------------------------------ factories
    static func newId(_ prefix: String) -> String {
        let hex = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        return "\(prefix)_\(hex.prefix(8))"
    }

    static func emptyProject() -> JSON {
        ["schema": .int(schema), "project": "Untitled", "floats": [], "updated": .double(Date().timeIntervalSince1970)]
    }

    static func newBox(name: String = "E-Box 1", ip: String = "", udpPort: Int = 6454, net: Int = 0, subnet: Int = 0, universe: Int = 0) -> JSON {
        ["id": .string(newId("box")), "name": .string(name), "ip": .string(ip), "udp_port": .int(udpPort),
         "net": .int(net), "subnet": .int(subnet), "universe": .int(universe), "notes": ""]
    }

    static func newFixture(label: String = "", variant: String = "TW", mode: Int? = nil, boxId: String? = nil,
                           address: Int? = nil, typeId: String = "", notes: String = "") -> JSON {
        ["id": .string(newId("fx")), "label": .string(label), "type_id": .string(typeId), "variant": .string(variant),
         "mode": .int(mode ?? Fixtures.defaultMode[variant] ?? 1), "box_id": JSON(boxId), "address": JSON(address),
         "uid": .null, "notes": .string(notes)]
    }

    static func newFloat(code: String = "", name: String = "New float") -> JSON {
        ["id": .string(newId("fl")), "code": .string(code), "name": .string(name), "notes": "",
         "boxes": [newBox()], "fixtures": [], "looks": [], "live": [:], "run_mode": "standalone"]
    }

    private func migrate() {
        data.setDefault("schema", .int(Store.schema))
        data.setDefault("floats", [])
        data.remove("network_interface")  // a per-computer setting on the Mac; meaningless here
        data.setDefault("palette", [])
        var floats = data["floats"].arrayValue
        for i in floats.indices {
            for key in ["looks", "boxes", "fixtures"] { floats[i].setDefault(key, []) }
            floats[i].setDefault("live", [:])
            floats[i].setDefault("run_mode", "standalone")
            var boxes = floats[i]["boxes"].arrayValue
            for j in boxes.indices { boxes[j].setDefault("udp_port", 6454) }
            floats[i]["boxes"] = .array(boxes)
        }
        data["floats"] = .array(floats)
    }

    // ------------------------------------------------------------ persistence
    /// Stamp a float as edited now, on this device. Import uses it to tell which copy is newer.
    func touch(_ fid: String?) {
        guard let fid = fid else { return }
        _ = withLock {
            updateFloat(fid) { fl in
                fl["updated"] = .double(Date().timeIntervalSince1970)
                fl["edited_on"] = .string(Store.hostname)
            }
        }
    }

    /// Structural change (patch, looks, palette): other devices reload when this changes.
    func bump(_ fid: String? = nil) {
        withLock {
            data["rev"] = .int((data["rev"].int ?? 0) + 1)
            markDirty(fid)
        }
    }

    func markDirty(_ fid: String? = nil) {
        withLock {
            touch(fid)
            data["updated"] = .double(Date().timeIntervalSince1970)
            dirty = true
        }
    }

    var rev: Int { withLock { data["rev"].int ?? 0 } }

    func flush() throws {
        let snapshot: String
        lock.lock()
        if !dirty { lock.unlock(); return }
        snapshot = data.serialize(indent: 1)
        dirty = false
        lock.unlock()
        do {
            try Store.atomicWrite(path, snapshot)  // temp file + fsync + rename: a crash can't leave half a file
        } catch {
            withLock { dirty = true }  // try again on the next tick
            throw error
        }
        lastSaved = Date().timeIntervalSince1970
        saveError = nil
        let now = Date().timeIntervalSince1970
        if now - lastBackup > 60 {  // at most one backup a minute, keep 100
            lastBackup = now
            let fmt = DateFormatter()
            fmt.locale = Locale(identifier: "en_US_POSIX")
            fmt.dateFormat = "yyyyMMdd-HHmmss"
            let stamp = fmt.string(from: Date())
            try? Store.atomicWrite(backupsDir.appendingPathComponent("project-\(stamp).json"), snapshot)
            prune(backupsDir, keep: 100)
            let hourly = backupsDir.appendingPathComponent("hourly")
            try? FileManager.default.createDirectory(at: hourly, withIntermediateDirectories: true)
            fmt.dateFormat = "yyyyMMdd-HH"
            let hourFile = hourly.appendingPathComponent("project-\(fmt.string(from: Date())).json")
            if !FileManager.default.fileExists(atPath: hourFile.path) {  // one per hour, kept 7 days
                try? Store.atomicWrite(hourFile, snapshot)
                prune(hourly, keep: 168)
            }
        }
    }

    private func prune(_ folder: URL, keep: Int) {
        let names = ((try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? [])
            .filter { $0.hasPrefix("project-") && $0.hasSuffix(".json") }.sorted()
        for old in names.dropLast(keep) {
            try? FileManager.default.removeItem(at: folder.appendingPathComponent(old))
        }
    }

    func close() {
        timer?.cancel()
        timer = nil
        markDirty()
        try? flush()
    }

    private static let writeLock = NSLock()

    /// Temp file + fsync + rename, one writer at a time; a unique temp name so two saves can't collide.
    static func atomicWrite(_ url: URL, _ text: String) throws {
        writeLock.lock(); defer { writeLock.unlock() }
        let tmp = url.deletingLastPathComponent().appendingPathComponent(
            "\(url.lastPathComponent).\(UUID().uuidString.prefix(8)).tmp")
        defer { try? FileManager.default.removeItem(at: tmp) }
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_TRUNC, 0o644)
        guard fd >= 0 else { throw AppError.runtime("Can't save \(url.lastPathComponent): \(String(cString: strerror(errno)))") }
        let bytes = Array(text.utf8)
        var off = 0
        while off < bytes.count {
            let n = bytes[off...].withUnsafeBytes { Darwin.write(fd, $0.baseAddress, $0.count) }
            if n <= 0 {
                let err = String(cString: strerror(errno))
                Darwin.close(fd)
                throw AppError.runtime("Can't save \(url.lastPathComponent): \(err)")
            }
            off += n
        }
        fsync(fd)
        Darwin.close(fd)
        if rename(tmp.path, url.path) != 0 {
            throw AppError.runtime("Can't save \(url.lastPathComponent): \(String(cString: strerror(errno)))")
        }
    }

    // ------------------------------------------------------------ access
    func snapshot() -> JSON { withLock { data } }

    func getFloat(_ fid: String) -> JSON? {
        withLock { data["floats"].arrayValue.first { $0["id"].string == fid } }
    }

    /// Mutate one float in place. Returns false if there is no such float.
    @discardableResult
    func updateFloat(_ fid: String, _ body: (inout JSON) throws -> Void) rethrows -> Bool {
        lock.lock(); defer { lock.unlock() }
        var floats = data["floats"].arrayValue
        guard let i = floats.firstIndex(where: { $0["id"].string == fid }) else { return false }
        try body(&floats[i])
        data["floats"] = .array(floats)
        return true
    }

    func replaceProject(_ incoming: JSON) throws {
        guard incoming.object != nil, incoming["floats"].array != nil else {
            throw AppError.value("Not a DMX Scene Builder project file")
        }
        var d = incoming
        d.setDefault("palette", [])
        var floats = d["floats"].arrayValue
        for i in floats.indices {
            for key in ["boxes", "fixtures", "looks"] { floats[i].setDefault(key, []) }
            try Store.validateFloat(&floats[i])  // raises with a clear message before anything is replaced
        }
        d["floats"] = .array(floats)
        withLock {
            let rev = data["rev"].int ?? 0
            data = d
            migrate()
            data["rev"] = .int(rev + 1)
            markDirty()
        }
    }

    func putFloat(_ flIn: JSON) throws -> JSON {
        var fl = flIn
        try Store.validateFloat(&fl)
        fl["updated"] = .double(Date().timeIntervalSince1970)
        fl["edited_on"] = .string(Store.hostname)
        return withLock {
            var floats = data["floats"].arrayValue
            if let i = floats.firstIndex(where: { $0["id"] == fl["id"] }) { floats[i] = fl } else { floats.append(fl) }
            data["floats"] = .array(floats)
            bump()
            return fl
        }
    }

    func deleteFloat(_ fid: String) {
        withLock {
            data["floats"] = .array(data["floats"].arrayValue.filter { $0["id"].string != fid })
            bump()
        }
    }

    // ------------------------------------------------------------ merge import
    /// Compare another device's project file against this one, float by float. Never mutates anything.
    func planMerge(_ incoming: JSON) throws -> JSON {
        guard incoming.object != nil, incoming["floats"].array != nil else {
            throw AppError.value("Not a DMX Scene Builder project file")
        }
        let mine = withLock { Dictionary(data["floats"].arrayValue.compactMap { f in f["id"].string.map { ($0, f) } },
                                         uniquingKeysWith: { a, _ in a }) }
        var new: [JSON] = [], identical: [JSON] = [], conflicts: [JSON] = []
        for fl in incoming["floats"].arrayValue {
            guard fl["id"].truthy, let fid = fl["id"].string else { continue }
            let summary = Store.floatSummary(fl)
            if let m = mine[fid] {
                if Store.floatsEqual(m, fl) { identical.append(summary) }
                else {
                    conflicts.append(["id": .string(fid), "code": fl.has("code") ? fl["code"] : "",
                                      "name": fl.has("name") ? fl["name"] : "",
                                      "mine": Store.floatSummary(m), "theirs": summary])
                }
            } else { new.append(summary) }
        }
        return ["new": .array(new), "identical": .array(identical), "conflicts": .array(conflicts)]
    }

    /// Add floats this device doesn't have, replace only floats explicitly resolved "theirs".
    /// Everything else, including every float not mentioned in `incoming`, is left exactly as it is.
    func applyMerge(_ incoming: JSON, _ resolutions: JSON) throws -> JSON {
        guard incoming.object != nil, incoming["floats"].array != nil else {
            throw AppError.value("Not a DMX Scene Builder project file")
        }
        lock.lock(); defer { lock.unlock() }
        // Validate everything first so a bad float can't leave a half-applied import.
        var staged = data
        var added = 0, replaced = 0
        var floats = staged["floats"].arrayValue
        let mine = Dictionary(floats.compactMap { f in f["id"].string.map { ($0, f) } }, uniquingKeysWith: { a, _ in a })
        for flIn in incoming["floats"].arrayValue {
            guard flIn["id"].truthy, let fid = flIn["id"].string else { continue }
            var fl = flIn
            for key in ["boxes", "fixtures", "looks"] { fl.setDefault(key, []) }
            if mine[fid] == nil {
                try Store.validateFloat(&fl)
                floats.append(fl)
                added += 1
            } else if resolutions[fid].string == "theirs", !Store.floatsEqual(mine[fid]!, fl) {
                try Store.validateFloat(&fl)
                fl["rev"] = .int((mine[fid]!["rev"].int ?? 0) + 1)  // stale edits from elsewhere can't overwrite it
                if let i = floats.firstIndex(where: { $0["id"].string == fid }) { floats[i] = fl }
                replaced += 1
            }
        }
        staged["floats"] = .array(floats)
        var cal = staged["white_cal"].object ?? [:]
        var rgbw = cal["RGBW"]?.object ?? [:]
        for (kk, mix) in incoming["white_cal"]["RGBW"].objectValue where rgbw[kk] == nil { rgbw[kk] = mix }
        cal["RGBW"] = .object(rgbw)
        staged["white_cal"] = .object(cal)
        var pal = staged["palette"].arrayValue
        let existing = Set(pal.map { $0["id"].serialize() })
        for c in incoming["palette"].arrayValue where !existing.contains(c["id"].serialize()) {
            pal.append(c)  // by id: the other device's own "Warm" is kept too
        }
        staged["palette"] = .array(pal)
        data = staged
        if added > 0 || replaced > 0 { bump() } else { markDirty() }
        return ["added": .int(added), "replaced": .int(replaced)]
    }

    static func floatSummary(_ fl: JSON) -> JSON {
        ["id": fl["id"], "code": fl.has("code") ? fl["code"] : "", "name": fl.has("name") ? fl["name"] : "",
         "fixtures": .int(fl["fixtures"].arrayValue.count), "boxes": .int(fl["boxes"].arrayValue.count),
         "looks": .int(fl["looks"].arrayValue.count), "updated": fl["updated"].or(.int(0)),
         "edited_on": fl["edited_on"].or("")]
    }

    static func floatsEqual(_ a: JSON, _ b: JSON) -> Bool {
        func strip(_ f: JSON) -> JSON {
            var o = f.objectValue
            for k in ["rev", "updated", "edited_on"] { o.removeValue(forKey: k) }
            return .object(o)
        }
        return strip(a) == strip(b)
    }

    // ------------------------------------------------------------ validation (store.validate_float)
    static func validateFloat(_ fl: inout JSON) throws {
        guard fl.object != nil, fl["id"].truthy else { throw AppError.value("float needs an id") }
        for key in ["boxes", "fixtures", "looks"] where fl[key].array == nil {
            throw AppError.value("float.\(key) must be a list")
        }
        fl.setDefault("live", [:])
        if fl["run_mode"].string != "standalone" && fl["run_mode"].string != "live_dmx" { fl["run_mode"] = "standalone" }
        var boxes = fl["boxes"].arrayValue
        for i in boxes.indices {
            let name = boxes[i]["name"].pyStr
            for (k, lo, hi) in [("net", 0, 127), ("subnet", 0, 15), ("universe", 0, 15), ("udp_port", 1, 65535)] {
                let v = try boxes[i][k].or(.int(k == "udp_port" ? 6454 : 0)).toInt(k)
                boxes[i][k] = .int(v)
                if !(lo...hi).contains(v) { throw AppError.value("\(name): \(k) must be \(lo)-\(hi)") }
            }
            let ip = (boxes[i]["ip"].or("").string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            boxes[i]["ip"] = .string(ip)
            if !ip.isEmpty {
                let parts = ip.split(separator: ".", omittingEmptySubsequences: false)
                let ok = parts.count == 4 && parts.allSatisfy { p in
                    !p.isEmpty && p.allSatisfy { $0.isASCII && $0.isNumber } && (Int(p) ?? 999) <= 255
                }
                if !ok { throw AppError.value("\(name): '\(ip)' isn't an IP address (four numbers, like 10.248.31.11).") }
            }
        }
        fl["boxes"] = .array(boxes)
        let boxIds = boxes.map { $0["id"] }
        var fxs = fl["fixtures"].arrayValue
        for i in fxs.indices {
            let label = fxs[i]["label"].pyStr
            guard let variant = fxs[i]["variant"].string, Fixtures.variants.contains(variant) else {
                let v = fxs[i]["variant"]
                throw AppError.value("\(label): unknown fixture type \(v.string.map { "'\($0)'" } ?? v.pyStr)")
            }
            let mode = try fxs[i]["mode"].or(.int(Fixtures.defaultMode[variant]!)).toInt("mode")
            fxs[i]["mode"] = .int(mode)
            if !Fixtures.hasMode(variant, mode) && mode != Fixtures.saveMode {
                throw AppError.value("\(label): mode \(mode) isn't valid for \(variant)")
            }
            let a = fxs[i]["address"]
            if a.isNull || a.string == "" {
                fxs[i]["address"] = .null
            } else {
                let addr = try a.toInt("address")
                fxs[i]["address"] = .int(addr)
                if !(1...512).contains(addr) { throw AppError.value("\(label): address must be 1-512") }
            }
            if !boxIds.contains(fxs[i]["box_id"]) || (fxs[i]["box_id"].isNull && !boxIds.contains(.null)) {
                fxs[i]["box_id"] = boxes.first?["id"] ?? .null
            }
        }
        fl["fixtures"] = .array(fxs)
    }

    // ------------------------------------------------------------ patch helpers
    static func effectiveVariant(_ fx: JSON) -> String {
        let v = fx["variant"].string ?? "RGBW"
        return Fixtures.hasMode(v, fx["mode"].int ?? 0) ? v : "RGBW"
    }

    static func fixtureFootprint(_ fx: JSON) throws -> Int {
        try Fixtures.footprint(effectiveVariant(fx), fx["mode"].int ?? 0)
    }

    /// Overlaps, out-of-range footprints, missing addresses.
    static func patchProblems(_ fl: JSON) -> JSON {
        var problems: [JSON] = []
        var byBox: [String: [(Int, Int, JSON)]] = [:]
        var boxOrder: [String] = []
        for fx in fl["fixtures"].arrayValue {
            guard let addr = fx["address"].int else {
                problems.append(["fixture": fx["id"], "level": "warn",
                                 "text": .string("\(fx["label"].or("Fixture").pyStr) has no DMX address")])
                continue
            }
            let end = addr + ((try? fixtureFootprint(fx)) ?? 0) - 1
            if end > 512 {
                problems.append(["fixture": fx["id"], "level": "error", "text": .string("\(fx["label"].pyStr) runs past channel 512")])
            }
            let key = fx["box_id"].serialize()
            if byBox[key] == nil { boxOrder.append(key) }
            byBox[key, default: []].append((addr, end, fx))
        }
        for key in boxOrder {
            let items = byBox[key]!.enumerated().sorted { ($0.element.0, $0.offset) < ($1.element.0, $1.offset) }.map { $0.element }
            for i in 0..<max(0, items.count - 1) {
                let (a0, e0, f0) = items[i], (a1, e1, f1) = items[i + 1]
                if a1 <= e0 {
                    let text = "\(f0["label"].or("?").pyStr) and \(f1["label"].or("?").pyStr) overlap (ch \(a0)-\(e0) vs \(a1)-\(e1))"
                    for f in [f0, f1] {
                        problems.append(["fixture": f["id"], "level": "error", "text": .string(text)])
                    }
                }
            }
        }
        return .array(problems)
    }
}
