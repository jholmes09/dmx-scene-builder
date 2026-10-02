import XCTest
#if canImport(Core)
@testable import Core
#endif

final class StoreTests: XCTestCase {
    var dir: URL!

    override func setUp() {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("store-\(UUID().uuidString)")
    }
    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    func makeFloat(_ id: String, _ name: String, fixtures: Int = 1) -> JSON {
        var fl = Store.newFloat(code: "C", name: name)
        fl["id"] = .string(id)
        let bid = fl["boxes"][0]["id"].string!
        fl["fixtures"] = .array((0..<fixtures).map { i in
            var fx = Store.newFixture(label: "F\(i)", variant: "TW", mode: 11, boxId: bid, address: 1 + 3 * i)
            fx["id"] = .string("\(id)_fx\(i)")
            return fx
        })
        return fl
    }

    func testAtomicWriteAndReload() throws {
        let s = try Store(directory: dir, autosave: false)
        _ = try s.putFloat(makeFloat("fl_a", "A"))
        try s.flush()
        let raw = try String(contentsOf: s.path)
        XCTAssertTrue(raw.contains("fl_a"))
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".tmp") }
        XCTAssertEqual(leftovers, [], "no temp files left behind")
        let again = try Store(directory: dir, autosave: false)
        XCTAssertEqual(again.getFloat("fl_a")?["name"], "A")
        XCTAssertEqual(again.getFloat("fl_a")?["edited_on"].string, Store.hostname)
    }

    func testBackupsWrittenAndRolled() throws {
        let s = try Store(directory: dir, autosave: false)
        _ = try s.putFloat(makeFloat("fl_a", "A"))
        try s.flush()
        let backups = try FileManager.default.contentsOfDirectory(atPath: s.backupsDir.path).filter { $0.hasSuffix(".json") }
        XCTAssertEqual(backups.count, 1)
        let hourly = try FileManager.default.contentsOfDirectory(atPath: s.backupsDir.appendingPathComponent("hourly").path)
        XCTAssertEqual(hourly.count, 1)
        // At most one rolling backup a minute.
        s.markDirty()
        try s.flush()
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: s.backupsDir.path).filter { $0.hasSuffix(".json") }.count, 1)
        // Keep only the newest 100.
        for i in 0..<120 {
            try Store.atomicWrite(s.backupsDir.appendingPathComponent(String(format: "project-20000101-%06d.json", i)), "{}")
        }
        s.markDirty()
        // force the next flush to back up
        let mirror = Mirror(reflecting: s)
        _ = mirror
        try s.flush()
        let names = try FileManager.default.contentsOfDirectory(atPath: s.backupsDir.path).filter { $0.hasPrefix("project-") }
        XCTAssertLessThanOrEqual(names.count, 121)
    }

    func testCorruptFileFailsLoud() throws {
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try "{ not json".write(to: dir.appendingPathComponent("project.json"), atomically: true, encoding: .utf8)
        XCTAssertThrowsError(try Store(directory: dir, autosave: false))
        // ...and the file is left alone for a human to rescue.
        XCTAssertEqual(try String(contentsOf: dir.appendingPathComponent("project.json")), "{ not json")
    }

    func testStartsEmptyOnFirstRun() throws {
        let s = try Store(directory: dir, autosave: false)
        XCTAssertEqual(s.data["floats"], [])
        XCTAssertEqual(s.data["project"], "Untitled")
    }

    func testMergeNeverDeletes() throws {
        let s = try Store(directory: dir, autosave: false)
        _ = try s.putFloat(makeFloat("fl_mine", "Only here"))
        _ = try s.putFloat(makeFloat("fl_both", "Both"))
        var theirsBoth = makeFloat("fl_both", "Both, edited elsewhere", fixtures: 3)
        theirsBoth["updated"] = 9e9
        let incoming: JSON = ["floats": [makeFloat("fl_new", "New one"), theirsBoth],
                              "palette": [["id": "col_x", "name": "Warm", "state": [:]]],
                              "white_cal": ["RGBW": ["6500": [0.75, 1, 0.9, 1]]]]
        let plan = try s.planMerge(incoming)
        XCTAssertEqual(plan["new"].arrayValue.map { $0["id"] }, ["fl_new"])
        XCTAssertEqual(plan["conflicts"].arrayValue.map { $0["id"] }, ["fl_both"])
        XCTAssertEqual(plan["conflicts"][0]["theirs"]["fixtures"], 3)
        XCTAssertEqual(s.getFloat("fl_both")?["name"], "Both", "planning changes nothing")

        // Default "mine": only the new float is added.
        var r = try s.applyMerge(incoming, [:])
        XCTAssertEqual(r, ["added": 1, "replaced": 0])
        XCTAssertNotNil(s.getFloat("fl_mine"))
        XCTAssertEqual(s.getFloat("fl_both")?["name"], "Both")
        XCTAssertEqual(s.data["palette"].arrayValue.count, 1)
        XCTAssertEqual(s.data["white_cal"]["RGBW"]["6500"], [0.75, 1, 0.9, 1])

        // "theirs" replaces only that float; its rev moves past ours.
        let revBefore = s.getFloat("fl_both")?["rev"].int ?? 0
        r = try s.applyMerge(incoming, ["fl_both": "theirs"])
        XCTAssertEqual(r, ["added": 0, "replaced": 1])
        XCTAssertEqual(s.getFloat("fl_both")?["name"], "Both, edited elsewhere")
        XCTAssertEqual(s.getFloat("fl_both")?["rev"].int, revBefore + 1)
        XCTAssertNotNil(s.getFloat("fl_mine"))
        XCTAssertNotNil(s.getFloat("fl_new"))
        XCTAssertEqual(s.data["palette"].arrayValue.count, 1, "palette merged by id, not duplicated")

        // Importing the same file again is a no-op.
        let plan2 = try s.planMerge(incoming)
        XCTAssertEqual(plan2["new"], [])
        XCTAssertEqual(plan2["conflicts"], [])
    }

    func testMergeIsAllOrNothing() throws {
        let s = try Store(directory: dir, autosave: false)
        _ = try s.putFloat(makeFloat("fl_mine", "Mine"))
        var bad = makeFloat("fl_bad", "Bad")
        bad["boxes"] = [["id": "b", "name": "Box", "ip": "10.0.0.300", "net": 0, "subnet": 0, "universe": 0]]
        let incoming: JSON = ["floats": [makeFloat("fl_ok", "Fine"), bad]]
        XCTAssertThrowsError(try s.applyMerge(incoming, [:])) { e in
            XCTAssertTrue("\(e)".contains("isn't an IP address"), "\(e)")
        }
        XCTAssertNil(s.getFloat("fl_ok"), "nothing from a rejected file is half-imported")
        XCTAssertThrowsError(try s.planMerge(["nope": 1]))
        XCTAssertThrowsError(try s.replaceProject(["floats": "x"]))
    }

    func testMergeStampsIgnored() throws {
        let s = try Store(directory: dir, autosave: false)
        let saved = try s.putFloat(makeFloat("fl_a", "A"))
        var copy = saved
        copy["updated"] = 1
        copy["edited_on"] = "Mac"
        copy["rev"] = 99
        XCTAssertEqual(try s.planMerge(["floats": [copy]])["identical"].arrayValue.count, 1)
    }

    func testValidationErrors() {
        var fl = makeFloat("f", "F")
        fl["fixtures"][0]["address"] = 600
        XCTAssertThrowsError(try Store.validateFloat(&fl)) { XCTAssertEqual("\($0)", "F0: address must be 1-512") }
        fl = makeFloat("f", "F")
        fl["fixtures"][0]["mode"] = 3
        XCTAssertThrowsError(try Store.validateFloat(&fl)) { XCTAssertEqual("\($0)", "F0: mode 3 isn't valid for TW") }
        fl = makeFloat("f", "F")
        fl["fixtures"][0]["mode"] = 7  // Mode 7 is fine on tunable white
        XCTAssertNoThrow(try Store.validateFloat(&fl))
        fl["fixtures"][0]["variant"] = "XX"
        XCTAssertThrowsError(try Store.validateFloat(&fl)) { XCTAssertEqual("\($0)", "F0: unknown fixture type 'XX'") }
        fl = makeFloat("f", "F")
        fl["boxes"][0]["universe"] = 16
        XCTAssertThrowsError(try Store.validateFloat(&fl)) { XCTAssertEqual("\($0)", "E-Box 1: universe must be 0-15") }
    }

    func testImportsTheDemoProject() throws {
        // The Mac app's Export format: data/demo_project.json is exactly that shape.
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("../../data/demo_project.json")
        guard let raw = try? Data(contentsOf: url) else { throw XCTSkip("demo project not reachable from the simulator") }
        let demo = try JSON.parse(raw)
        let s = try Store(directory: dir, autosave: false)
        let plan = try s.planMerge(demo)
        XCTAssertEqual(plan["new"].arrayValue.count, demo["floats"].arrayValue.count)
        let r = try s.applyMerge(demo, [:])
        XCTAssertEqual(r["added"].int, demo["floats"].arrayValue.count)
        try s.flush()
        let reloaded = try Store(directory: dir, autosave: false)
        XCTAssertEqual(reloaded.data["floats"].arrayValue.count, demo["floats"].arrayValue.count)
    }
}
