import XCTest
#if canImport(Core)
@testable import Core
#endif

/// The Swift engine driven through its API (the way the web UI does) against the Python fake
/// E-Box (tools/ios_fakebox.py): Art-Net simulator + fake REAP web page on 127.0.0.1.
/// Run with tools/ios_test.sh, which starts the fake box and passes its control port in
/// FAKEBOX_CONTROL (TEST_RUNNER_FAKEBOX_CONTROL for xcodebuild).
final class EndToEndTests: XCTestCase {
    var control = 0
    var artnetPort = 0
    var dir: URL!
    var store: Store!
    var ctl: ArtNetController!
    var api: API!
    var fid = ""
    var bid = ""
    let ids = ["fx1", "fx2", "fx3", "fx4"]
    let uids = ["5253:012E2A01", "5253:012E2A02", "5253:012E2A03", "5253:012E2A04"]

    // ------------------------------------------------------------ harness
    func http(_ method: String, _ path: String, _ body: JSON? = nil) -> JSON {
        var req = URLRequest(url: URL(string: "http://127.0.0.1:\(control)\(path)")!)
        req.httpMethod = method
        req.timeoutInterval = 10
        if let b = body { req.httpBody = b.data() }
        let sem = DispatchSemaphore(value: 0)
        var out: JSON = .null
        URLSession.shared.dataTask(with: req) { d, _, _ in
            if let d = d { out = (try? JSON.parse(d)) ?? .null }
            sem.signal()
        }.resume()
        sem.wait()
        return out
    }

    func box() -> JSON { http("GET", "/state") }
    func module(_ d: String) -> JSON { box()["modules"].arrayValue.first { $0["d_uid"].string == d }! }

    @discardableResult
    func call(_ method: String, _ path: String, _ body: JSON? = nil) -> (Int, JSON) {
        let r = api.handle(method: method, path: path, body: body?.data())
        return (r.status, (try? JSON.parse(r.body)) ?? .null)
    }

    func ok(_ method: String, _ path: String, _ body: JSON? = nil, file: StaticString = #filePath, line: UInt = #line) -> JSON {
        let (s, j) = call(method, path, body)
        XCTAssertEqual(s, 200, "\(method) \(path): \(j.serialize())", file: file, line: line)
        return j
    }

    func refused(_ method: String, _ path: String, _ body: JSON?, _ contains: String, file: StaticString = #filePath, line: UInt = #line) {
        let (s, j) = call(method, path, body)
        XCTAssertNotEqual(s, 200, "\(method) \(path) should be refused", file: file, line: line)
        XCTAssertTrue(contains.isEmpty || j["error"].pyStr.contains(contains), "error was: \(j["error"].pyStr)", file: file, line: line)
    }

    func waitFor(_ timeout: Double = 5, _ cond: () -> Bool) -> Bool {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end { if cond() { return true }; Thread.sleep(forTimeInterval: 0.05) }
        return cond()
    }

    func status() -> JSON { ok("GET", "/api/status")["engine"] }

    func waitJob(_ timeout: Double = 20) -> JSON {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            let j = status()["job"]
            if j["state"].string != "running" { return j }
            Thread.sleep(forTimeInterval: 0.1)
        }
        XCTFail("save job stuck in running")
        return .null
    }

    override func setUpWithError() throws {
        guard let c = ProcessInfo.processInfo.environment["FAKEBOX_CONTROL"].flatMap(Int.init) else {
            if ProcessInfo.processInfo.environment["FAKEBOX_REQUIRED"] != nil { XCTFail("FAKEBOX_CONTROL not set") }
            throw XCTSkip("Fake E-Box not running: use tools/ios_test.sh")
        }
        control = c
        _ = http("POST", "/reset", [:])
        let info = http("GET", "/info")
        artnetPort = info["artnet_port"].int!
        Reap.webPort = info["web_port"].int!
        Engine.settleS = 0.5  // shorter save job; the 4.5 s save hold stays real
        ArtNetController.sweepSubnets = false
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("e2e-\(UUID().uuidString)")
        store = try Store(directory: dir, autosave: false)
        ctl = ArtNetController(port: 0).start()
        api = API(store: store, ctl: ctl, webRoot: nil)
        var fl = ok("POST", "/api/floats", ["code": "S1", "name": "Test float"])
        fid = fl["id"].string!
        bid = fl["boxes"][0]["id"].string!
        fl["boxes"][0]["ip"] = "127.0.0.1"
        fl["boxes"][0]["udp_port"] = .int(artnetPort)
        let spec: [(String, Int, Int)] = [("RGBW", 1, 1), ("RGBW", 1, 16), ("TW", 11, 31), ("TW", 11, 50)]
        fl["fixtures"] = .array((0..<4).map { i in
            ["id": .string(ids[i]), "label": .string("L\(i + 1)"), "variant": .string(spec[i].0), "mode": .int(spec[i].1),
             "box_id": .string(bid), "address": .int(spec[i].2), "uid": .string(uids[i])]
        })
        _ = ok("PUT", "/api/floats/\(fid)", fl)
    }

    override func tearDown() {
        if api != nil {
            _ = waitFor(20) { self.status()["job"]["state"].string != "running" && !self.status()["applying"].truthy }
            api.engine.release(toBlack: false)
            api.shutdown()
            ctl.stop()
            store.close()
            try? FileManager.default.removeItem(at: dir)
            // Mode 7 lights must never see 5-6 on their Special channel, and never a stray 1-2.
            for e in box()["special_log"].arrayValue {
                XCTAssertFalse([5, 6].contains(e[3].int!), "Special byte hit \(e[3].int!) on \(e[1].pyStr)")
            }
        }
        Reap.webPort = 80
    }

    func render(_ v: String, _ m: Int, _ s: JSON) -> [Int] { try! Fixtures.render(v, m, s).map { Int($0) } }
    func dmx(_ d: String) -> [Int] { module(d)["dmx"].arrayValue.map { $0.int! } }

    func toMode7() -> JSON {
        let r = ok("POST", "/api/floats/\(fid)/apply", ["to_mode7": .array(ids.map { .string($0) })])
        XCTAssertEqual(r["ok"], true, r.serialize())
        return r
    }

    // ------------------------------------------------------------ discovery and scan
    func testFindBoxByUnicastPoll() {
        let r = ok("POST", "/api/discover", ["targets": [["ip": "127.0.0.1", "udp_port": .int(artnetPort)]], "wait": 0.5])
        XCTAssertTrue(r["nodes"].arrayValue.contains { $0["long_name"].string == "E-box Remote (SIM)" }, r.serialize())
        XCTAssertEqual(r["nodes"].arrayValue.first?["output_port_addresses"], [0])
    }

    func testScanThroughBoxWebPage() {
        let r = ok("POST", "/api/floats/\(fid)/scan", ["box_id": .string(bid)])
        let devs = r["devices"].arrayValue
        XCTAssertEqual(devs.map { $0["uid"].string! }, uids)
        XCTAssertEqual(devs.map { $0["address"].int! }, [1, 16, 31, 50])
        XCTAssertEqual(devs.map { $0["variant_guess"].string! }, ["RGBW", "RGBW", "TW", "TW"])
        XCTAssertEqual(devs[2]["modes_available"], [7, 11, 12, 13])
        XCTAssertEqual(r["via"], "box web page")
    }

    func testScanRefusedWhenBoxUnreachable() {
        _ = http("POST", "/set", ["offline": "refuse"])
        refused("POST", "/api/floats/\(fid)/scan", ["box_id": .string(bid)], "Can't reach the box's web page")
        _ = http("POST", "/set", ["offline": .null])
    }

    func testAddressModeLabelIdentifyThroughBox() {
        _ = ok("POST", "/api/floats/\(fid)/scan", ["box_id": .string(bid)])
        var r = ok("POST", "/api/floats/\(fid)/rdm", ["box_id": .string(bid), "uid": .string(uids[0]), "action": "address", "address": 100])
        XCTAssertEqual(r["info"]["address"], 100)
        XCTAssertEqual(module("012e2a01")["address"], 100)
        XCTAssertEqual(store.getFloat(fid)!["fixtures"][0]["address"], 100, "patch follows the light")
        r = ok("POST", "/api/floats/\(fid)/rdm", ["box_id": .string(bid), "uid": .string(uids[2]), "action": "mode", "mode": 7])
        XCTAssertEqual(r["info"]["mode"], 7)
        XCTAssertEqual(module("012e2a03")["mode"], 7)
        XCTAssertEqual(store.getFloat(fid)!["fixtures"][2]["mode"], 7, "TW accepts Mode 7")
        _ = ok("POST", "/api/floats/\(fid)/rdm", ["box_id": .string(bid), "uid": .string(uids[1]), "action": "label", "label": "Front left"])
        XCTAssertEqual(module("012e2a02")["label"], "Front left")
        _ = ok("POST", "/api/floats/\(fid)/rdm", ["box_id": .string(bid), "uid": .string(uids[1]), "action": "identify", "on": true])
        XCTAssertTrue(box()["reap_log"].arrayValue.contains { $0[1].string == "rdm_identify" })
        let p = ok("POST", "/api/floats/\(fid)/rdm", ["box_id": .string(bid), "uid": .string(uids[1]), "action": "params"])
        XCTAssertEqual(p["params"][0]["description"], "DMX terminator (last light on a cable)")
    }

    // ------------------------------------------------------------ live output
    func testLiveOutputMatchesPythonRender() {
        _ = ok("POST", "/api/output", ["action": "activate", "float_id": .string(fid)])
        let look: JSON = ["fx1": ["dim": 0.5, "kind": "color", "hue": 240, "sat": 1],
                          "fx3": ["dim": 1.0, "cct": 2700], "fx4": ["dim": 0.25, "cct": 6500]]
        _ = ok("POST", "/api/floats/\(fid)/live", ["changes": look])
        XCTAssertTrue(waitFor { self.dmx("012e2a01") == self.render("RGBW", 1, look["fx1"]) }, "\(dmx("012e2a01"))")
        XCTAssertTrue(waitFor { self.dmx("012e2a03") == self.render("TW", 11, look["fx3"]) }, "\(dmx("012e2a03"))")
        XCTAssertTrue(waitFor { self.dmx("012e2a04") == self.render("TW", 11, look["fx4"]) })
        XCTAssertEqual(status()["output"], true)
        // Looks: save, change, recall.
        let lk = ok("POST", "/api/floats/\(fid)/looks", ["name": "Parade"])
        _ = ok("POST", "/api/floats/\(fid)/live", ["changes": ["fx3": ["dim": 0]]])
        XCTAssertTrue(waitFor { self.dmx("012e2a03")[1] == 0 })
        _ = ok("POST", "/api/floats/\(fid)/looks/\(lk["id"].string!)/recall")
        XCTAssertTrue(waitFor { self.dmx("012e2a03") == self.render("TW", 11, look["fx3"]) })
        // Blackout and back.
        _ = ok("POST", "/api/output", ["action": "blackout", "on": true])
        XCTAssertTrue(waitFor { self.dmx("012e2a03")[1] == 0 && self.dmx("012e2a03")[2] == 0 })
        _ = ok("POST", "/api/output", ["action": "blackout", "on": false])
        XCTAssertTrue(waitFor { self.dmx("012e2a03") == self.render("TW", 11, look["fx3"]) })
    }

    func testLiveOnlyWhenTheBoxAnswers() {
        api.engine.boxCheckS = 0.3
        api.engine.boxLostS = 1.5
        _ = ok("POST", "/api/output", ["action": "activate", "float_id": .string(fid)])
        XCTAssertTrue(waitFor { self.status()["boxes"][0]["state"].string == "ok" }, status()["boxes"].serialize())
        // Point the float at a box that isn't there: still sending, but never claims the box is there.
        var fl = ok("GET", "/api/floats/\(fid)")
        fl["boxes"][0]["udp_port"] = 9
        _ = ok("PUT", "/api/floats/\(fid)", fl)
        XCTAssertTrue(waitFor { self.status()["boxes"][0]["state"].string == "down" }, status()["boxes"].serialize())
        XCTAssertEqual(status()["output"], true)
    }

    func testReleaseSendsBlackThenStops() {
        _ = ok("POST", "/api/output", ["action": "activate", "float_id": .string(fid)])
        _ = ok("POST", "/api/floats/\(fid)/live", ["changes": ["fx3": ["dim": 1.0, "cct": 4000]]])
        XCTAssertTrue(waitFor { self.dmx("012e2a03")[1] == 255 })
        let r = ok("POST", "/api/output", ["action": "release"])
        XCTAssertEqual(r["engine"]["output"], false)
        XCTAssertTrue(waitFor(1) { self.box()["last_dmx"].arrayValue.allSatisfy { $0 == 0 } }, "black went out")
        Thread.sleep(forTimeInterval: 1.6)
        let n = box()["frames"].int!
        Thread.sleep(forTimeInterval: 0.6)
        XCTAssertEqual(box()["frames"].int!, n, "output stopped after the black")
        // Touching a control takes control again (the UI calls activate first).
        _ = ok("POST", "/api/output", ["action": "activate", "float_id": .string(fid)])
        XCTAssertTrue(waitFor { self.dmx("012e2a03")[1] == 255 })
    }

    // ------------------------------------------------------------ Mode 7 switching and saving
    func testSwitchToMode7ThenTunableWhiteMode7Output() {
        _ = toMode7()
        let fxs = store.getFloat(fid)!["fixtures"].arrayValue
        XCTAssertEqual(fxs.map { $0["mode"].int! }, [7, 7, 7, 7])
        XCTAssertEqual(fxs.map { $0["address"].int! }, [1, 16, 31, 46])
        XCTAssertEqual(box()["modules"].arrayValue.map { $0["address"].int! }, [1, 16, 31, 46])
        XCTAssertEqual(box()["modules"].arrayValue.map { $0["mode"].int! }, [7, 7, 7, 7])
        XCTAssertEqual(status()["hold"], false, "hold lifted once every light confirmed")
        XCTAssertEqual(status()["output"], true)
        for (i, k) in [(2, 3000), (3, 4000)] {
            _ = ok("POST", "/api/floats/\(fid)/live", ["changes": [ids[i]: ["dim": 1.0, "cct": .int(k), "boost": true]]])
        }
        XCTAssertTrue(waitFor { self.dmx("012e2a03") == self.render("TW", 7, ["dim": 1.0, "cct": 3000, "boost": true]) }, "\(dmx("012e2a03"))")
        XCTAssertTrue(waitFor { self.dmx("012e2a04") == self.render("TW", 7, ["dim": 1.0, "cct": 4000, "boost": true]) })
        // Special byte 0 on every Mode 7 light while just live.
        XCTAssertEqual(dmx("012e2a03")[0], 0)
        XCTAssertTrue(box()["special_log"].arrayValue.allSatisfy { $0[3].int! == 0 })
    }

    func testFailedChangeKeepsHoldAndPatch() {
        _ = http("POST", "/set", ["fail_uid": "012e2a02"])
        let r = ok("POST", "/api/floats/\(fid)/apply", ["to_mode7": .array(ids.map { .string($0) })])
        XCTAssertEqual(r["ok"], false)
        XCTAssertEqual(r["held"], true)
        XCTAssertEqual(status()["hold"], true, "nothing stray reaches the lights")
        XCTAssertTrue(waitFor { self.box()["last_dmx"].arrayValue.allSatisfy { $0 == 0 } })
        let fx2 = store.getFloat(fid)!["fixtures"][1]
        XCTAssertEqual(fx2["mode"], 1)
        XCTAssertEqual(fx2["address"], 16, "the box says it's still Mode 1 at 16")
        refused("POST", "/api/floats/\(fid)/save", ["fixture_ids": .array(ids.map { .string($0) })], "held dark")
        _ = ok("POST", "/api/output", ["action": "release"])
        XCTAssertEqual(status()["hold"], false, "Release clears it")
    }

    func testSaveHoldsSpecialForTheSaveTimeOnly() {
        _ = toMode7()
        _ = ok("POST", "/api/floats/\(fid)/live", ["changes": ["fx1": ["dim": 0.8, "kind": "color", "hue": 30, "sat": 1],
                                                               "fx3": ["dim": 1.0, "cct": 3500]]])
        let t0 = Date().timeIntervalSince1970
        let job = ok("POST", "/api/floats/\(fid)/save", ["fixture_ids": .array(ids.map { .string($0) })])
        XCTAssertEqual(job["state"], "running")
        // A second save, another float going live, white test, blackout: all refused meanwhile.
        refused("POST", "/api/floats/\(fid)/save", ["fixture_ids": ["fx1"]], "already running")
        let other = ok("POST", "/api/floats", ["name": "Other"])
        refused("POST", "/api/output", ["action": "activate", "float_id": other["id"]], "A save is running on another float")
        refused("POST", "/api/output", ["action": "white_test", "method": 1, "k": 6500], "A save is running")
        refused("POST", "/api/output", ["action": "blackout", "on": true], "A save is running")
        let done = waitJob()
        let t1 = Date().timeIntervalSince1970
        XCTAssertEqual(done["state"], "done", done.serialize())
        Thread.sleep(forTimeInterval: 0.3)
        let log = box()["special_log"].arrayValue
        for d in ["012e2a01", "012e2a02", "012e2a03", "012e2a04"] {
            let mine = log.filter { $0[1].string == d }
            guard let on = mine.first(where: { $0[3].int == 1 }), let off = mine.last(where: { $0[3].int == 0 }) else {
                return XCTFail("no save command seen on \(d): \(mine.map { $0.serialize() })")
            }
            let held = off[0].number! - on[0].number!
            XCTAssertGreaterThanOrEqual(held, 4.3, "\(d) held for \(held) s")
            XCTAssertLessThanOrEqual(held, 5.5, "\(d) held for \(held) s")
            XCTAssertGreaterThanOrEqual(on[0].number!, t0)
            XCTAssertLessThanOrEqual(off[0].number!, t1 + 0.3)
            XCTAssertEqual(mine.filter { $0[3].int! != 0 }.map { $0[3].int! }, [1], "only the save command, once")
            XCTAssertNotNil(module(d)["saved_initial"].array, "\(d) saved its look")
        }
        // Saved look recorded the live look (Special 0 + the colour)
        XCTAssertEqual(module("012e2a03")["saved_initial"].arrayValue.map { $0.int! },
                       render("TW", 7, ["dim": 1.0, "cct": 3500]).enumerated().map { $0.offset == 0 ? 1 : $0.element })
    }

    func testSaveRefusedWhenBoxPlaysSavedLook() {
        _ = toMode7()
        _ = http("POST", "/set", ["ic_od": "disabled"])
        refused("POST", "/api/floats/\(fid)/save", ["fixture_ids": .array(ids.map { .string($0) })], "Play saved look")
    }

    func testSaveRefusedWhenNotMode7() {
        refused("POST", "/api/floats/\(fid)/save", ["fixture_ids": .array(ids.map { .string($0) })], "Mode 7")
    }

    func testRefusalsWhileLightsAreBeingChanged() {
        _ = http("POST", "/set", ["confirm_delay": 0.6])
        var res: JSON = .null
        let done = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            res = self.ok("POST", "/api/floats/\(self.fid)/apply", ["to_mode7": .array(self.ids.map { .string($0) })])
            done.signal()
        }
        XCTAssertTrue(waitFor { self.status()["applying"].truthy })
        refused("POST", "/api/output", ["action": "activate", "float_id": .string(fid)], "Lights are being changed")
        refused("POST", "/api/output", ["action": "release"], "Lights are being changed")
        refused("POST", "/api/output", ["action": "hold", "on": false], "Lights are being changed")
        refused("POST", "/api/output", ["action": "white_test", "method": 1], "Lights are being changed")
        refused("POST", "/api/floats/\(fid)/save", ["fixture_ids": .array(ids.map { .string($0) })], "")  // (not Mode 7 yet, or busy)
        refused("POST", "/api/floats/\(fid)/apply", ["push": true], "Busy")
        let fl = store.getFloat(fid)!
        let (s, j) = call("PUT", "/api/floats/\(fid)", fl)
        XCTAssertEqual(s, 409, j.serialize())
        refused("PUT", "/api/project", ["floats": []], "Lights are being changed")
        // While held, every Mode 7 Special byte stays 0.
        XCTAssertTrue(box()["last_dmx"].arrayValue.allSatisfy { $0 == 0 })
        done.wait()
        XCTAssertEqual(res["ok"], true, res.serialize())
    }

    func testStaleEditFromAnotherDeviceRefused() {
        var fl = store.getFloat(fid)!
        fl["rev"] = .int((fl["rev"].int ?? 0) - 1)
        let (s, j) = call("PUT", "/api/floats/\(fid)", fl)
        XCTAssertEqual(s, 409)
        XCTAssertEqual(j["error"], "This float was changed on another device. Reloading it now.")
    }

    // ------------------------------------------------------------ box settings
    func testPlaySavedLookSwitchRestartsBox() {
        var r = ok("POST", "/api/floats/\(fid)/box", ["box_id": .string(bid)])
        XCTAssertEqual(r["output_data"], "enabled")
        _ = http("POST", "/set", ["restart_s": 1.0])
        r = ok("POST", "/api/floats/\(fid)/box", ["box_id": .string(bid), "action": "output_data", "enabled": false])
        XCTAssertEqual(r["restarting"], true)
        XCTAssertEqual(box()["ic_od"], "disabled")
        XCTAssertTrue(waitFor { !self.box()["offline"].truthy }, "box back after restart")
        r = ok("POST", "/api/floats/\(fid)/box", ["box_id": .string(bid)])
        XCTAssertEqual(r["output_data"], "disabled")
        _ = ok("POST", "/api/floats/\(fid)/box", ["box_id": .string(bid), "action": "output_data", "enabled": true])
        XCTAssertEqual(box()["ic_od"], "enabled")
    }

    // ------------------------------------------------------------ iOS lifecycle
    func testBackgroundStopsSaveWithSpecialZeroAndResumes() {
        _ = toMode7()
        _ = ok("POST", "/api/floats/\(fid)/live", ["changes": ["fx3": ["dim": 1.0, "cct": 5000]]])
        _ = ok("POST", "/api/floats/\(fid)/save", ["fixture_ids": .array(ids.map { .string($0) })])
        XCTAssertTrue(waitFor { self.dmx("012e2a03").first == 1 }, "save command on")
        api.engine.enterBackground()
        XCTAssertTrue(waitFor(1) { self.dmx("012e2a03").first == 0 }, "last frame before suspending has Special 0")
        let n = box()["frames"].int!
        Thread.sleep(forTimeInterval: 0.5)
        XCTAssertLessThanOrEqual(box()["frames"].int! - n, 1, "nothing sent while in the background")
        let job = waitJob()
        XCTAssertEqual(job["state"], "error")
        XCTAssertTrue(job["message"].pyStr.contains("background"), job["message"].pyStr)
        api.engine.enterForeground()
        XCTAssertTrue(waitFor { self.box()["frames"].int! > n + 5 }, "output resumed")
        XCTAssertEqual(dmx("012e2a03"), render("TW", 7, ["dim": 1.0, "cct": 5000]))
    }

    // ------------------------------------------------------------ import from the Mac
    func testImportMacExportThroughAPI() {
        var mac = Store.newFloat(code: "M1", name: "From the Mac")
        mac["fixtures"] = [Store.newFixture(label: "A", variant: "RGBW", mode: 7, boxId: mac["boxes"][0]["id"].string, address: 1)]
        let export: JSON = ["schema": 1, "project": "Show", "floats": [mac, store.getFloat(fid)!], "palette": []]
        let plan = ok("POST", "/api/project/merge_plan", export)
        XCTAssertEqual(plan["new"].arrayValue.count, 1)
        XCTAssertEqual(plan["identical"].arrayValue.count, 1)
        let r = ok("POST", "/api/project/merge_apply", ["incoming": export, "resolutions": [:]])
        XCTAssertEqual(r["added"], 1)
        XCTAssertNotNil(store.getFloat(mac["id"].string!))
        XCTAssertNotNil(store.getFloat(fid), "nothing deleted")
        let st = ok("GET", "/api/state")
        XCTAssertEqual(st["project"]["floats"].arrayValue.count, 2)
        XCTAssertNotNil(st["project"]["floats"][0]["problems"].array)
    }
}
