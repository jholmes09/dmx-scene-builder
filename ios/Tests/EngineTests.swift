import XCTest
#if canImport(Core)
@testable import Core
#endif

/// Frame building without a network: the Special-functions guards.
final class EngineTests: XCTestCase {
    var dir: URL!
    var store: Store!
    var ctl: ArtNetController!
    var engine: Engine!
    let t = Target(ip: "127.0.0.1", port: 9, pa: 0)

    override func setUpWithError() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("eng-\(UUID().uuidString)")
        store = try Store(directory: dir, autosave: false)
        ctl = ArtNetController(port: 0)  // never started: nothing is sent
        engine = Engine(store: store, ctl: ctl)
        var fl = Store.newFloat(code: "E", name: "E")
        fl["id"] = "fl"
        fl["boxes"][0]["ip"] = "127.0.0.1"
        fl["boxes"][0]["udp_port"] = 9
        let bid = fl["boxes"][0]["id"].string!
        // Mode 1 neighbour overlapping the Mode 7 light's Special channel (address 10).
        var a = Store.newFixture(label: "M1", variant: "RGBW", mode: 1, boxId: bid, address: 8)
        a["id"] = "m1"
        var b = Store.newFixture(label: "M7", variant: "RGBW", mode: 7, boxId: bid, address: 10)
        b["id"] = "m7"
        fl["fixtures"] = [a, b]
        fl["live"] = ["m1": ["dim": 1.0, "kind": "color", "hue": 0, "sat": 0, "white": 1.0]]
        _ = try store.putFloat(fl)
        engine.activeFloat = "fl"
    }

    override func tearDown() { engine.stop(); try? FileManager.default.removeItem(at: dir) }

    func testNeighbourNeverWritesSpecialChannel() {
        let f = engine.frames()[t]!
        XCTAssertEqual(f[7], 255)  // the neighbour's red
        XCTAssertEqual(f[9], 0, "Special channel forced to 0 although the neighbour's byte is 255")
    }

    func testSpecialOnlyWhileASaveRuns() {
        engine.special["m7"] = 1
        XCTAssertEqual(engine.frames()[t]![9], 0, "no running save: Special stays 0")
        engine.job = ["state": "running"]
        XCTAssertEqual(engine.frames()[t]![9], 1)
        engine.blackout = true
        XCTAssertEqual(engine.frames()[t]![9], 0, "blackout forces 0")
        engine.blackout = false
        engine.job["abort"] = "x"
        XCTAssertEqual(engine.frames()[t]![9], 0, "aborting save forces 0")
    }

    func testHoldSendsZeros() {
        engine.hold = true
        XCTAssertEqual(engine.frames()[t]!, [UInt8](repeating: 0, count: 512))
    }

    func testFlashAndSweepOverlay() throws {
        engine.flashFixture("m1", seconds: 5)
        XCTAssertNotNil(engine.frames()[t])
        engine.flash.removeAll()
        engine.sweep = Engine.Sweep(target: t, variant: "TW", mode: 11, addresses: [100], current: 100, index: 0,
                                    seconds: 1, footprint: 3, paused: true, token: 1)
        let f = engine.frames()[t]!
        XCTAssertEqual(Array(f[99..<102]), try Fixtures.render("TW", 11, ["dim": 1.0, "kind": "white", "cct": 4000]))
        XCTAssertEqual(f[7], 0, "everything else dark while the address finder runs")
    }
}
