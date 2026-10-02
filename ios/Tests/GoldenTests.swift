import XCTest
#if canImport(Core)
@testable import Core
#endif

/// The Swift core must reproduce the Python core exactly: every DMX byte, every mix summary,
/// every packet. Vectors come from tools/make_golden.py (run it after changing the Python core).
final class GoldenTests: XCTestCase {
    static var golden: JSON = {
        let url = Bundle(for: GoldenTests.self).url(forResource: "golden", withExtension: "json")
            ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("golden.json")
        return try! JSON.parse(try! Data(contentsOf: url))
    }()
    var g: JSON { GoldenTests.golden }

    func bytes(_ j: JSON) -> [UInt8] { j.arrayValue.map { UInt8($0.int!) } }

    func testRenderMixDescribeNormalize() throws {
        let cals = g["cals"].arrayValue
        var bad: [String] = []
        let cases = g["render"].arrayValue
        XCTAssertGreaterThan(cases.count, 4000)
        for c in cases {
            let v = c["v"].string!, m = c["m"].int!, s = c["s"], sp = c["sp"].int!
            let cal = cals[c["cal"].int!]
            let label = "\(v) M\(m) sp=\(sp) cal=\(c["cal"].int!) \(s.serialize())"
            let r = try Fixtures.render(v, m, s, special: sp, cal: cal)
            if r != bytes(c["render"]) { bad.append("render \(label): swift \(r) python \(bytes(c["render"]))") }
            let mix = try Fixtures.mixValues(v, m, s, cal: cal)
            if mix != c["mix"] { bad.append("mix \(label): swift \(mix.serialize()) python \(c["mix"].serialize())") }
            let d = try Fixtures.describeMix(v, m, s, cal: cal)
            if d != c["desc"].string! { bad.append("describe \(label): swift '\(d)' python '\(c["desc"].string!)'") }
            let n = try Fixtures.normalizeState(s, v).toJSON()
            if n != c["norm"] { bad.append("normalize \(label): swift \(n.serialize()) python \(c["norm"].serialize())") }
        }
        XCTAssertEqual(bad.count, 0, "\(bad.count) mismatches, first: \(bad.prefix(8).joined(separator: "\n"))")
    }

    func testTemperatureHelpers() {
        for p in g["kelvin_to_ctc"].arrayValue {
            XCTAssertEqual(Fixtures.kelvinToCtc(p[0].number!), p[1].int!, "kelvin_to_ctc(\(p[0].number!))")
        }
        for p in g["ctc_to_kelvin"].arrayValue {
            XCTAssertEqual(Fixtures.ctcToKelvin(p[0].int!), p[1].number!, "ctc_to_kelvin(\(p[0].int!))")
        }
        for p in g["kelvin_to_wsel"].arrayValue {
            XCTAssertEqual(Fixtures.kelvinToWsel(p[0].number!), p[1].int!, "kelvin_to_wsel(\(p[0].number!))")
        }
        for p in g["footprints"].arrayValue {
            XCTAssertEqual(try Fixtures.footprint(p[0].string!, p[1].int!), p[2].int!)
        }
    }

    func testWhiteTestFrames() {
        for c in g["white_test"].arrayValue {
            let mix = c["mix"].array?.map { $0.number! }
            XCTAssertEqual(Engine.whiteTestBytes(c["method"].int!, c["k"].int!, mix), bytes(c["bytes"]),
                           "method \(c["method"].int!) k \(c["k"].int!)")
        }
    }

    func testPatchProblemsAndAutoAddressing() throws {
        for c in g["store"].arrayValue {
            var fl = c["float"]
            try Store.validateFloat(&fl)
            XCTAssertEqual(fl, c["float"], "validate_float is idempotent on a Python-validated float")
            XCTAssertEqual(Store.patchProblems(fl), c["problems"])
            for p in c["plans"].arrayValue {
                let ids = p["ids"].array?.map { $0.string! }
                let modes = p["modes"].object.map { $0.mapValues { $0.int! } }
                let what = "box \(p["box_id"].pyStr) start \(p["start"].int!) ids \(p["ids"].serialize()) gap \(p["gap"].int!) modes \(p["modes"].isNull ? "-" : "7")"
                do {
                    let res = try API.planAddresses(fl, boxId: p["box_id"].string, start: p["start"].int!, fixtureIds: ids,
                                                    gap: p["gap"].int!, modes: modes)
                    XCTAssertFalse(p.has("error"), "expected error for \(what)")
                    var obj: [String: JSON] = [:]
                    for (k, a) in res { obj[k] = .int(a) }
                    XCTAssertEqual(JSON.object(obj), p["result"], what)
                } catch let e as AppError {
                    XCTAssertEqual(e.description, p["error"].string, what)
                }
            }
        }
    }

    func testArtNetPackets() throws {
        let pk = g["packets"]
        XCTAssertEqual(ArtNet.buildPoll(), bytes(pk["poll"][0]))
        XCTAssertEqual(ArtNet.buildPoll(flags: 0x02), bytes(pk["poll"][1]))
        for p in pk["port_address"].arrayValue {
            XCTAssertEqual(try ArtNet.portAddress(p[0].int!, p[1].int!, p[2].int!), p[3].int!)
        }
        XCTAssertThrowsError(try ArtNet.portAddress(128, 0, 0))
        for c in pk["dmx"].arrayValue {
            let pkt = ArtNet.buildDmx(pa: c["pa"].int!, data: bytes(c["data"]), sequence: c["seq"].int!)
            XCTAssertEqual(pkt, bytes(c["packet"]))
            let d = ArtNet.parseDmx(pkt)!
            XCTAssertEqual(d.sequence, c["parsed"]["sequence"].int!)
            XCTAssertEqual(d.physical, c["parsed"]["physical"].int!)
            XCTAssertEqual(d.portAddress, c["parsed"]["port_address"].int!)
            XCTAssertEqual(d.data, bytes(c["parsed"]["data"]))
        }
        for c in pk["poll_reply"].arrayValue {
            let a = c["args"]
            let pkt = ArtNet.buildPollReply(ip: a[0].string!, net: a[1].int!, subnet: a[2].int!, universes: a[3].arrayValue.map { $0.int! },
                                            shortName: a[4].string!, longName: a[5].string!, mac: bytes(a[6]))
            XCTAssertEqual(pkt, bytes(c["packet"]))
            let parsed = ArtNet.parsePollReply(bytes(c["packet"]))!.toJSON()
            XCTAssertEqual(parsed, c["parsed"])
        }
        XCTAssertNil(ArtNet.parsePollReply(ArtNet.buildPoll()))
        XCTAssertNil(ArtNet.parseDmx([1, 2, 3]))
    }
}
