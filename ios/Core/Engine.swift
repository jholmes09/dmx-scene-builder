import Foundation

/// Turns the active float's live look into Art-Net frames, and runs timed jobs (flash-to-find,
/// save-to-fixtures, address finder). Port of scenebuilder/engine.py.
final class Engine {
    /// Manual: hold the save command >= 3 s.
    static var saveHoldS = 4.5
    static var settleS = 1.5

    static let vcwWhites: [Int: Int] = [1800: 1, 2700: 3, 3200: 5, 4200: 7, 5600: 9, 6500: 11]
    /// method: (r, g, b, w, use CTC). Tunable-white lights in Mode 7: find which channel does what.
    static let twProbes: [Int: (UInt8, UInt8, UInt8, UInt8, Bool)] = [
        7: (255, 0, 0, 0, false), 8: (0, 255, 0, 0, false), 9: (0, 0, 255, 0, false),
        10: (0, 0, 0, 255, false), 11: (255, 255, 255, 255, true), 12: (0, 0, 0, 255, true)]

    /// Mode 7 frame for comparing white methods (dimmer ~60%).
    static func whiteTestBytes(_ method: Int, _ k: Int, _ mix: [Double]?) -> [UInt8] {
        var ctc = UInt8(Fixtures.kelvinToCtc(Double(k)))
        var r: UInt8 = 0, g: UInt8 = 0, b: UInt8 = 0, w: UInt8 = 0, vcw: UInt8 = 0
        switch method {
        case 1: r = 255; g = 255; b = 255; w = 255
        case 2:
            let nearest = vcwWhites.keys.sorted().min { abs($0 - k) < abs($1 - k) }!
            vcw = UInt8(vcwWhites[nearest]!); ctc = 0
        case 3: w = 255; ctc = 0
        case 4: r = 255; g = 255; b = 255; w = 255; ctc = 0
        case 5: w = 255
        case 6 where (mix?.count ?? 0) >= 4:
            let c = mix!.map { UInt8(pyRound(max(0.0, min(1.0, $0)) * 255)) }
            r = c[0]; g = c[1]; b = c[2]; w = c[3]; ctc = 0
        default:
            if let p = twProbes[method] {
                (r, g, b, w) = (p.0, p.1, p.2, p.3)
                if !p.4 { ctc = 0 }
            }
        }
        // special r rf g gf b bf w wf gc ctc vcw shutter dim dimf
        return [0, r, 0, g, 0, b, 0, w, 0, 128, ctc, vcw, 255, 153, 0]
    }

    struct Sweep {
        var target: Target, variant: String, mode: Int, addresses: [Int], current: Int, index: Int
        var seconds: Double, footprint: Int, paused: Bool, token: Double
        func toJSON() -> JSON {
            ["variant": .string(variant), "mode": .int(mode), "addresses": .array(addresses.map { .int($0) }),
             "current": .int(current), "index": .int(index), "seconds": .double(seconds), "footprint": .int(footprint),
             "paused": .bool(paused), "token": .double(token)]
        }
    }

    let store: Store
    let ctl: ArtNetController
    let lock = NSRecursiveLock()
    var activeFloat: String?
    var flash: [String: Double] = [:]      // fixture id -> until (monotonic)
    var special: [String: Int] = [:]       // fixture id -> Special functions byte override
    var dark = Set<String>()               // fixture ids forced dark (check step)
    var sweep: Sweep?
    var whiteTest: JSON = .null            // {"method", "k", "mix"}
    private var pausedForRdm = 0
    private var resumeAfterRdmFlag = false
    var hold = false                       // all-zero universes (fixtures being re-moded/re-addressed)
    var applying: String?                  // float id while modes/addresses are being written to lights
    var blackout = false
    var job: JSON = .null
    private var releaseToken = 0
    private(set) var releasing = false
    private(set) var inBackground = false
    private var stopped = false

    static func now() -> Double { ProcessInfo.processInfo.systemUptime }

    init(store: Store, ctl: ArtNetController) {
        self.store = store
        self.ctl = ctl
        let t = Thread { [weak self] in self?.loop() }
        t.name = "engine"
        t.start()
    }

    func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock(); defer { lock.unlock() }
        return try body()
    }

    var jobRunning: Bool { withLock { job["state"].string == "running" } }

    // ------------------------------------------------------------ output control
    func setActive(_ fid: String?, output: Bool = true, keepHold: Bool = false) {
        withLock {
            releaseToken += 1  // going live cancels a Release that's still sending black
            releasing = false
            activeFloat = fid
            flash.removeAll(); special.removeAll(); dark.removeAll()
            sweep = nil
            if !keepHold { hold = false }
            whiteTest = .null
            let want = fid != nil && output
            if pausedForRdm > 0 { resumeAfterRdmFlag = want } else { ctl.outputEnabled = want }
            if fid == nil { ctl.clearTargets() }
        }
        refresh()
    }

    /// Release like a console: send black for ~1 s, then stop sending. The boxes' DMX Hold then
    /// keeps the lights dark (rather than frozen on the last look).
    func release(toBlack: Bool = true) {
        let token: Int = withLock {
            resumeAfterRdmFlag = false
            hold = false
            whiteTest = .null
            if job["state"].string == "running" { job["abort"] = "Release was pressed" }
            let wasOn = ctl.outputEnabled
            if !(toBlack && wasOn) {
                ctl.outputEnabled = false
                ctl.clearTargets()
                return -1
            }
            var zeros: [Target: [UInt8]] = [:]
            for t in framesTargets() { zeros[t] = [UInt8](repeating: 0, count: 512) }
            ctl.setTargets(zeros)
            releaseToken += 1
            releasing = true
            return releaseToken
        }
        if token < 0 { return }
        DispatchQueue.global().asyncAfter(deadline: .now() + 1.0) { [weak self] in  // ~30 black frames
            guard let self = self else { return }
            self.withLock {
                if self.releaseToken == token {  // nobody went live again meanwhile
                    self.ctl.outputEnabled = false
                    self.ctl.clearTargets()
                    self.releasing = false
                }
            }
        }
    }

    static func target(_ b: JSON) -> Target {
        let net = b["net"].int ?? 0, sub = b["subnet"].int ?? 0, uni = b["universe"].int ?? 0
        return Target(ip: b["ip"].string ?? "", port: b["udp_port"].or(6454).int ?? 6454, pa: (net << 8) | (sub << 4) | uni)
    }

    /// The (ip, port, port-address) of every box on the active float.
    func framesTargets() -> [Target] {
        guard let fid = withLock({ activeFloat }), let fl = store.getFloat(fid) else { return [] }
        return fl["boxes"].arrayValue.filter { $0["ip"].truthy }.map { Engine.target($0) }
    }

    func resume() {
        withLock {
            releaseToken += 1
            releasing = false
            if activeFloat != nil {
                if pausedForRdm > 0 { resumeAfterRdmFlag = true } else { ctl.outputEnabled = true }
            }
        }
        refresh()
    }

    /// All-zero output while fixtures change mode/address, so no stale byte lands on a Special channel.
    func setHold(_ on: Bool) {
        withLock { hold = on }
        refresh()
    }

    func status() -> JSON {
        withLock {
            var j = job
            if j.isNull == false, j.object == nil { j = .null }
            return ["active_float": JSON(activeFloat), "output": .bool(ctl.outputEnabled && !releasing),
                    "blackout": .bool(blackout), "flashing": .array(flash.keys.sorted().map { .string($0) }),
                    "job": j, "sweep": sweep?.toJSON() ?? .null, "white_test": whiteTest,
                    "paused_for_rdm": .bool(pausedForRdm > 0), "hold": .bool(hold), "applying": .bool(applying != nil),
                    "packets_sent": .int(ctl.packetsSent), "packets_received": .int(ctl.packetsReceived),
                    "bind_error": JSON(ctl.bindError), "send_error": JSON(ctl.lastSendError),
                    "artnet_port": .int(ctl.port), "background": .bool(inBackground)]
        }
    }

    // ------------------------------------------------------------ rendering
    func frames() -> [Target: [UInt8]] {
        lock.lock(); defer { lock.unlock() }
        guard let fid = activeFloat, let fl = store.getFloat(fid) else { return [:] }
        var boxes: [String: JSON] = [:]
        for b in fl["boxes"].arrayValue where b["ip"].truthy { boxes[b["id"].pyStr] = b }
        var out: [Target: [UInt8]] = [:]
        for b in boxes.values { out[Engine.target(b)] = [UInt8](repeating: 0, count: 512) }
        if hold { return out }
        let now = Engine.now()
        let cal = store.withLock { store.data["white_cal"]["RGBW"] }
        var specials: [(Target, Int, Int)] = []  // Mode 7 Special-functions bytes, written last
        let live = fl["live"]
        for fx in fl["fixtures"].arrayValue {
            guard let b = boxes[fx["box_id"].pyStr], fx["address"].truthy, let address = fx["address"].int,
                  let fxId = fx["id"].string, let mode = fx["mode"].int else { continue }
            let key = Engine.target(b)
            var state = live[fxId].object != nil ? live[fxId] : Fixtures.defaultStateJSON
            if blackout || dark.contains(fxId) || sweep != nil { state["dim"] = 0.0 }
            if let until = flash[fxId] {
                if now > until { flash.removeValue(forKey: fxId) }
                else { state = ["dim": .double(Int(now * 3) % 2 == 0 ? 1.0 : 0.0), "kind": "white", "cct": 4000] }
            }
            let ev = Store.effectiveVariant(fx)
            guard let roles = try? Fixtures.modeInfo(ev, mode).roles,
                  var data = try? Fixtures.render(ev, mode, state, special: special[fxId] ?? 0,
                                                  cal: ev == "RGBW" ? cal : .null) else { continue }
            if dark.contains(fxId) && !blackout && sweep == nil {
                // Check step: colours off but dimmer FULL and shutter open.
                data = roles.map { r in (r == "dim" || r == "dim_f" || r == "shutter") ? 255 : (r == "gc" ? 128 : 0) }
            }
            if let wm = whiteTest["method"].int, mode == Fixtures.saveMode,
               (Engine.twProbes[wm] != nil) == (fx["variant"].string == "TW") {
                data = Engine.whiteTestBytes(wm, whiteTest["k"].int ?? 6500, whiteTest["mix"].array?.map { $0.number ?? 0 })
            }
            let a = address - 1
            guard a >= 0, a < 512 else { continue }
            let n = min(data.count, 512 - a)
            out[key]!.replaceSubrange(a..<(a + n), with: data[0..<n])
            if let si = roles.firstIndex(of: "special"), a + si < 512 {
                specials.append((key, a + si, special[fxId] ?? 0))
            }
        }
        if let sw = sweep, out[sw.target] != nil {
            let a = sw.current - 1
            if a >= 0 && a < 512, let data = try? Fixtures.render(sw.variant, sw.mode, ["dim": 1.0, "kind": "white", "cct": 4000]) {
                let n = min(data.count, 512 - a)
                out[sw.target]!.replaceSubrange(a..<(a + n), with: data[0..<n])
            }
        }
        // A neighbour's overlapping bytes must never land on a Mode 7 Special channel
        // (1-2 = save, 5-6 = factory demo at power-on). Only a save job that is running cleanly
        // may put a non-zero value there.
        let saving = job["state"].string == "running" && !job["abort"].truthy
        let darkAll = blackout || sweep != nil
        for (t, i, v) in specials { out[t]![i] = (darkAll || !saving) ? 0 : UInt8(v & 0xFF) }
        return out
    }

    func refresh() {
        if withLock({ releasing }) { return }  // sending black for Release; don't put the look back
        ctl.setTargets(frames())
        if ctl.outputEnabled && !ctl.suspended { ctl.sendNow() }
    }

    private func loop() {
        while true {
            Thread.sleep(forTimeInterval: 0.1)
            let (stop, need) = withLock { (stopped, !flash.isEmpty || !special.isEmpty || sweep != nil) }
            if stop { return }
            if need && ctl.outputEnabled && !withLock({ releasing }) { ctl.setTargets(frames()) }
        }
    }

    // ------------------------------------------------------------ live edits
    func updateLive(_ fid: String, _ changes: JSON) throws {
        var found = false
        try store.withLock {
            found = try store.updateFloat(fid) { fl in
                let ids = Dictionary(fl["fixtures"].arrayValue.compactMap { f in f["id"].string.map { ($0, f) } },
                                     uniquingKeysWith: { a, _ in a })
                var live = fl["live"].object ?? [:]
                for (fxId, st) in changes.objectValue {
                    guard let fx = ids[fxId] else { continue }
                    var cur = live[fxId]?.object != nil ? live[fxId]! : Fixtures.defaultStateJSON
                    for (k, v) in st.objectValue { cur[k] = v }
                    live[fxId] = try Fixtures.normalizeState(cur, Store.effectiveVariant(fx)).toJSON()
                }
                fl["live"] = .object(live)
            }
            if found { store.markDirty(fid) }
        }
        if !found { throw AppError.key("float not found") }
        if fid == withLock({ activeFloat }) { refresh() }
    }

    func flashFixture(_ fxId: String, seconds: Double = 6.0) {
        withLock { flash[fxId] = Engine.now() + seconds }
        refresh()
    }

    func setWhiteTest(_ method: Int?, k: Int = 6500, mix: JSON = .null) throws {
        try withLock {
            if let m = method, m != 0, job["state"].string == "running" {
                throw AppError.runtime("A save is running. Wait for it to finish.")
            }
            if let m = method, m != 0 { whiteTest = ["method": .int(m), "k": .int(k), "mix": mix] } else { whiteTest = .null }
        }
        refresh()
    }

    func setBlackout(_ on: Bool) throws {
        try withLock {
            if on && job["state"].string == "running" { throw AppError.runtime("A save is running. Wait for it to finish.") }
            blackout = on
        }
        refresh()
    }

    func startSave(_ fid: String, _ fxIds: [String], verifyOnly: Bool = false) throws -> JSON {
        let ids: [String] = try withLock {
            if job["state"].string == "running" { throw AppError.runtime("Another save is already running") }
            guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
            let want = Set(fxIds)
            let chosen = fl["fixtures"].arrayValue.filter { want.contains($0["id"].pyStr) }
            let ok = chosen.filter { $0["mode"].int == Fixtures.saveMode && $0["address"].truthy }
            let okIds = Set(ok.map { $0["id"].pyStr })
            let skipped: [JSON] = chosen.filter { !okIds.contains($0["id"].pyStr) }.map {
                ["id": $0["id"], "label": $0["label"],
                 "why": .string($0["mode"].int != Fixtures.saveMode ? "not in Mode 7" : "no address")]
            }
            if ok.isEmpty {
                throw AppError.runtime("None of the selected fixtures are in Mode 7 with an address. Switch them to Mode 7 first (Addressing tab, or REAP).")
            }
            if applying != nil { throw AppError.runtime("Lights are being changed right now. Wait for that to finish.") }
            if hold { throw AppError.runtime("Output is held dark because the last address or mode change didn't finish. Press Clear hold (top right), then try again.") }
            if pausedForRdm > 0 { throw AppError.runtime("Busy talking to fixtures. Try again in a moment.") }
            if inBackground { throw AppError.runtime("The app is in the background. Bring it back to the front and try again.") }
            // Never save a dark look: clear Blackout, the address finder and flashes first.
            blackout = false
            sweep = nil
            flash.removeAll()
            if fid != activeFloat || !ctl.outputEnabled {
                activeFloat = fid
                releaseToken += 1
                releasing = false
                ctl.outputEnabled = true
            }
            job = ["state": "running", "kind": .string(verifyOnly ? "verify" : "save"), "float": .string(fid),
                   "fixtures": .array(ok.map { $0["id"] }), "skipped": .array(skipped), "step": "starting",
                   "progress": 0.0, "started": .double(Date().timeIntervalSince1970), "message": ""]
            return ok.map { $0["id"].pyStr }
        }
        let snapshot = withLock { job }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in self?.runSave(ids, verifyOnly) }
        return snapshot
    }

    private func runSave(_ ids: [String], _ verifyOnly: Bool) {
        // Special functions: 1-2 = save current values as initial values, 3-4 = show saved values.
        // Never park on 5-6 (factory demo at power-on).
        let steps: [(String, Double, Int, Bool)] = verifyOnly
            ? [("Blacking out the live look", Engine.settleS, 0, true),
               ("Showing what is saved in the fixtures", 6.0, 3, true),
               ("Back to the live look", Engine.settleS, 0, false)]
            : [("Sending the look", Engine.settleS, 0, false),
               ("Saving into the fixtures (holding the save command)", Engine.saveHoldS, 1, false),
               ("Releasing", Engine.settleS, 0, false)]
        let total = steps.reduce(0) { $0 + $1.1 }
        var done = 0.0
        let fid = withLock { job["float"].string }
        do {
            for (name, dur, val, isDark) in steps {
                withLock {
                    for i in ids {
                        special[i] = val
                        if isDark { dark.insert(i) } else { dark.remove(i) }
                    }
                    job["step"] = .string(name)
                }
                ctl.setTargets(frames())
                let t0 = Engine.now()
                while Engine.now() - t0 < dur {
                    Thread.sleep(forTimeInterval: 0.1)
                    let why: String? = withLock {
                        job["progress"] = .double(min(1.0, (done + Engine.now() - t0) / total))
                        if let a = job["abort"].string { return a }
                        if activeFloat != fid { return "Another float went live" }
                        if !ctl.outputEnabled || hold || blackout || sweep != nil || inBackground { return "Output was stopped or changed" }
                        return nil
                    }
                    if let why = why {
                        throw AppError.runtime(why + " before the save finished. Nothing is confirmed; run it again.")
                    }
                }
                done += dur
            }
            withLock {
                job["state"] = "done"; job["step"] = "Done"; job["progress"] = 1.0
                job["message"] = .string(verifyOnly ? "Showed the saved look for 6 s." : String(format: "Save command held for %.1f s.", Engine.saveHoldS))
            }
        } catch {
            withLock { job["state"] = "error"; job["step"] = "Stopped"; job["message"] = .string("\(error)") }
        }
        withLock {
            for i in ids { special.removeValue(forKey: i); dark.remove(i) }
        }
        refresh()
    }

    // ------------------------------------------------------------ address finder
    func startSweep(_ fid: String, boxId: String, variant: String, mode: Int, start: Int = 1, count: Int = 20, seconds: Double = 2.5) throws -> JSON {
        if jobRunning { throw AppError.runtime("A save is running. Wait for it to finish.") }
        guard let fl = store.getFloat(fid) else { throw AppError.key("float not found") }
        guard let box = fl["boxes"].arrayValue.first(where: { $0["id"].string == boxId }), box["ip"].truthy else {
            throw AppError.value("Set the box IP first.")
        }
        let target = Engine.target(box)
        let fp = try Fixtures.footprint(variant, mode)
        let addrs = Array(stride(from: start, through: 512, by: fp).prefix(max(0, count)))
        if addrs.isEmpty { throw AppError.value("Nothing to sweep") }
        let token: Double = withLock {
            activeFloat = fid
            releaseToken += 1
            releasing = false
            ctl.outputEnabled = true
            let t = Engine.now()
            sweep = Sweep(target: target, variant: variant, mode: mode, addresses: addrs, current: addrs[0], index: 0,
                          seconds: seconds, footprint: fp, paused: false, token: t)
            return t
        }
        Thread.detachNewThread { [weak self] in self?.runSweep(token) }
        return status()["sweep"]
    }

    func sweepControl(_ action: String) {
        withLock {
            guard var sw = sweep else { return }
            switch action {
            case "stop": sweep = nil; return
            case "pause": sw.paused = true
            case "resume": sw.paused = false
            case "next", "prev":
                sw.paused = true
                sw.index = max(0, min(sw.addresses.count - 1, sw.index + (action == "next" ? 1 : -1)))
                sw.current = sw.addresses[sw.index]
            default: break
            }
            sweep = sw
        }
        refresh()
    }

    private func runSweep(_ token: Double) {
        while true {
            guard let secs = withLock({ sweep?.token == token ? sweep!.seconds : nil }) else { break }
            Thread.sleep(forTimeInterval: max(0.05, secs))
            let alive: Bool = withLock {
                guard var sw = sweep, sw.token == token else { return false }
                if !sw.paused {
                    sw.index = (sw.index + 1) % sw.addresses.count
                    sw.current = sw.addresses[sw.index]
                    sweep = sw
                }
                return true
            }
            if !alive { break }
            ctl.setTargets(frames())
        }
        refresh()
    }

    // ------------------------------------------------------------ talking to fixtures
    func pauseForRdm() throws {
        try withLock {
            if job["state"].string == "running" { throw AppError.runtime("A save is running. Wait for it to finish.") }
            pausedForRdm += 1
            if pausedForRdm == 1 {
                resumeAfterRdmFlag = ctl.outputEnabled
                ctl.outputEnabled = false
            }
        }
    }

    func resumeAfterRdm() {
        withLock {
            pausedForRdm = max(0, pausedForRdm - 1)
            if pausedForRdm == 0 && resumeAfterRdmFlag { ctl.outputEnabled = true }
        }
    }

    // ------------------------------------------------------------ iOS lifecycle
    /// The app is leaving the screen. A save can't be finished reliably once iOS suspends us, so
    /// it is stopped; one last complete frame (Special bytes all 0) goes out, then sending pauses.
    /// The boxes' DMX Hold keeps the lights on that frame.
    func enterBackground() {
        withLock {
            inBackground = true
            if job["state"].string == "running" { job["abort"] = "The app went to the background" }
            special.removeAll()
            dark.removeAll()
        }
        if !withLock({ releasing }) { ctl.setTargets(frames()) }
        if ctl.outputEnabled { ctl.sendNow() }
        ctl.suspended = true
    }

    /// Back on screen: rebuild the frame from the current look first, then resume sending.
    func enterForeground() {
        withLock { inBackground = false }
        if !withLock({ releasing }) { ctl.setTargets(frames()) }
        ctl.suspended = false
    }

    func stop() { withLock { stopped = true } }
}
