import Foundation

/// Client for the Robe/Anolis E-Box built-in web page (REAP). Port of scenebuilder/reap.py.
///
/// The real E-Box Remote (software 6.6, Pass-Thr) doesn't bridge RDM over Art-Net, but its web
/// page runs RDM on its own outputs: find the Calumma, set address / mode / label, identify.
/// All POST, form-encoded, HTTP basic auth (robe / 2479). Fields are those the page's script uses:
///   rdm_test, ds_get_device (poll; disc_done:1 at end), ds_setup_device (0-based dmx_a, dmx_p),
///   rdm_identify, ds_get_setup (poll for confirmation), oth_s (dmxh/ebm/ic_od), sys_res (restart).
final class Reap {
    /// The box's web port. Tests point this at a fake box (like reap.WEB_PORT in Python).
    static var webPort = 80
    static let defaultUser = "robe", defaultPassword = "2479"
    static let estaRobe = "5253"

    /// REAP's 8-hex device id -> the app's UID format '5253:012E2A57'.
    static func uidToStr(_ dUid: String) -> String { "\(estaRobe):\(dUid.uppercased())" }
    /// '5253:012E2A57' -> '012e2a57'.
    static func uidFromStr(_ uid: String) -> String { (uid.split(separator: ":").last.map(String.init) ?? uid).lowercased() }

    let ip: String
    let timeout: TimeInterval
    private let auth: String

    init(_ ip: String, user: String = defaultUser, password: String = defaultPassword, timeout: TimeInterval = 5.0) {
        self.ip = ip
        self.timeout = timeout
        auth = "Basic " + Data("\(user):\(password)".utf8).base64EncodedString()
    }

    // ------------------------------------------------------------ transport
    static func formEncode(_ data: [(String, String)]) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        let enc = { (s: String) in (s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s).replacingOccurrences(of: "%20", with: "+") }
        return data.map { "\(enc($0.0))=\(enc($0.1))" }.joined(separator: "&")
    }

    @discardableResult
    func post(_ path: String, _ data: [(String, String)] = []) throws -> JSON {
        let body = Array(Reap.formEncode(data).utf8)
        let resp: HTTPClient.Response
        do {
            resp = try HTTPClient.post(host: ip, port: Reap.webPort, path: "/" + path, body: body,
                                       headers: ["Authorization": auth, "Accept-Encoding": "gzip",
                                                 "Content-Type": "application/x-www-form-urlencoded"],
                                       timeout: timeout)
        } catch HTTPClient.Failure.timeout {
            throw AppError.runtime("Can't reach the box's web page at \(ip) (timed out).")
        } catch HTTPClient.Failure.unreachable(let why) {
            throw AppError.runtime("Can't reach the box's web page at \(ip) (\(why)).")
        } catch {
            throw AppError.runtime("Can't reach the box's web page at \(ip) (\(error)).")
        }
        if resp.status == 401 { throw AppError.runtime("The box's web page rejected the login (robe / 2479).") }
        if resp.status >= 400 { throw AppError.runtime("The box's web page answered \(resp.status) for \(path).") }
        var raw = resp.body
        if Gzip.isGzip(raw) {
            do { raw = try Gzip.decompress(raw) } catch {
                throw AppError.runtime("Unexpected answer from the box's web page for \(path).")
            }
        }
        let text = String(decoding: raw, as: UTF8.self)
        if text.isEmpty { return [:] }
        guard let j = try? JSON.parse(Data(text.utf8)) else {
            throw AppError.runtime("Unexpected answer from the box's web page for \(path).")
        }
        return j
    }

    // ------------------------------------------------------------ info
    func status() throws -> JSON { try post("status_i") }

    func available() -> Bool {
        guard let s = try? status() else { return false }
        return s.has("rdmu")
    }

    // ------------------------------------------------------------ RDM through the box
    /// Same light reported twice in the last search: a sign of a noisy cable run.
    private(set) var lastRepeats = 0

    func discover(timeout: TimeInterval = 25.0) throws -> [JSON] {
        try post("rdm_test")
        var found: [String: JSON] = [:]
        lastRepeats = 0
        var order: [String] = []
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let r = try post("ds_get_device")
            if r["d_uid"].truthy {
                let k = r["d_uid"].pyStr
                if found[k] == nil { order.append(k) } else { lastRepeats += 1 }
                found[k] = r
            }
            if r["disc_done"].number == 1 { return order.map { found[$0]! } }
            Thread.sleep(forTimeInterval: 0.2)
        }
        throw AppError.runtime("The box's device search didn't finish within \(Int(timeout)) s.")
    }

    /// Wait for the box to confirm a change to *this* light (ignore leftovers for others).
    func waitSetup(_ dUid: String, timeout: TimeInterval = 8.0) throws -> JSON {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            Thread.sleep(forTimeInterval: 0.25)
            let r = try post("ds_get_setup")
            if r["d_uid"].truthy && r["d_uid"].pyStr.lowercased() == dUid.lowercased() { return r }
        }
        throw AppError.runtime("The fixture didn't confirm the change within \(Int(timeout)) s.")
    }

    /// Write label/address/mode (and optionally the line terminator) to one fixture.
    func setup(_ dUid: String, label: String, address: Int, mode: Int, device: JSON = .null, terminator: Bool? = nil) throws {
        guard (1...512).contains(address) else { throw AppError.runtime("Address must be 1-512.") }
        let count = (try? device["dmx_p_c"].or(16).toInt()) ?? 16
        guard 1 <= mode && mode <= count else { throw AppError.runtime("Mode must be 1-\(count) on this fixture.") }
        var data: [(String, String)] = [("uid", dUid), ("u_l", String(label.prefix(32))),
                                        ("dmx_a", String(address - 1)), ("dmx_p", String(mode - 1))]
        let sp = (try? device["sp"].or(0).toInt()) ?? 0
        if sp & 1 != 0 {
            let cur = device["dmx_t"].string == "on" ? 1 : 0
            data.append(("dmx_t", String(terminator == nil ? cur : (terminator! ? 1 : 0))))
        }
        if sp & 2 != 0 { data.append(("b_f", device["b_f"].string == "on" ? "1" : "0")) }
        if sp & 4 != 0 {
            let p = ["low": 0, "middle": 1, "high": 2][device["pwr"].string ?? ""] ?? 0
            data.append(("pwr", String(p)))
        }
        let r = try post("ds_setup_device", data)
        let st = r.has("status") ? r["status"] : .int(0)
        if st != .int(0) { throw AppError.runtime("The box refused the setting (status \(st.pyStr)).") }
        let done = try waitSetup(dUid)
        let err = done.has("err") ? done["err"] : .int(0)
        if err != .int(0) { throw AppError.runtime("The fixture rejected the setting (error \(err.pyStr)).") }
    }

    func identify(_ dUid: String, on: Bool) throws {
        try post("rdm_identify", [("uid", dUid), ("i_v", on ? "1" : "0")])
        _ = try waitSetup(dUid)
    }

    // ------------------------------------------------------------ box settings
    /// {'dmxh': 'on'|'off', 'ebm': 'standard'|'pass-through', 'ic_od': 'enabled'|'disabled'}
    func otherSettings() throws -> JSON { try post("oth_s") }

    func setOther(dmxHold: Bool? = nil, passThrough: Bool? = nil, outputData: Bool? = nil) throws {
        let cur = try otherSettings()
        let hold = dmxHold ?? (cur["dmxh"].string == "on")
        let pt = passThrough ?? (cur["ebm"].string == "pass-through")
        let od = outputData ?? (cur["ic_od"].string == "enabled")
        let r = try post("oth_s", [("dmxh", hold ? "0" : "1"), ("ebm", pt ? "1" : "0"), ("ic_od", od ? "1" : "0")])
        if r["status"] != .int(0) { throw AppError.runtime("The box didn't confirm the setting (status \(r["status"].pyStr)).") }
    }

    /// Option lists exactly as the box's own settings page offers them (index = value written).
    static let dmxInputs = ["wired DMX", "wireless DMX", "wireless out DMX", "ethernet"]
    static let ethModes = ["Art-NET", "sACN", "gMA I", "gMA II"]

    /// The box may report a setting as its index or its label; return the index, or nil.
    static func optionIndex(_ value: JSON, _ options: [String]) -> Int? {
        let asInt: Int?
        switch value {
        case .null: return nil
        case .string(let s):
            if s.isEmpty { return nil }
            asInt = Int(s.trimmingCharacters(in: .whitespacesAndNewlines))
        case .int, .double, .bool: asInt = value.int
        default: asInt = nil
        }
        if let i = asInt { return (0..<options.count).contains(i) ? i : nil }
        let low = value.pyStr.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return options.firstIndex { $0.lowercased() == low }
    }

    /// {'dmxa': box DMX address, 'dmxi': DMX input, 'rdmn': box label}
    func dmxSettings() throws -> JSON { try post("set_dmx") }

    /// {'ip', 'netm', 'ethm': Ethernet mode, 'igmpt'}
    func ethSettings() throws -> JSON { try post("eth_s") }

    func setDmxInput(_ index: Int) throws {
        let cur = try dmxSettings()
        guard let dmxa = try? cur["dmxa"].toInt() else {
            throw AppError.runtime("Couldn't read the box's DMX settings, so nothing was changed.")
        }
        let rdmn = String((cur["rdmn"].truthy ? cur["rdmn"].pyStr : "").prefix(32))
        let r = try post("set_dmx", [("dmxa", String(dmxa)), ("dmxi", String(index)), ("rdmn", rdmn)])
        if r["status"] != .int(0) {
            throw AppError.runtime("The box didn't confirm the DMX input change (status \(r["status"].pyStr)).")
        }
    }

    static func isIPv4(_ s: String) -> Bool {
        let parts = s.split(separator: ".", omittingEmptySubsequences: false)
        return parts.count == 4 && parts.allSatisfy { p in
            !p.isEmpty && p.count <= 3 && p.allSatisfy { $0.isASCII && $0.isNumber } && (Int(p) ?? 256) <= 255
                && !(p.count > 1 && p.first == "0")
        }
    }

    /// Change only the Ethernet mode. The box's IP and mask are sent back exactly as read, and
    /// nothing is written unless the box reports the IP we are talking to.
    func setEthMode(_ index: Int) throws {
        let cur = try ethSettings()
        let ip = cur["ip"].truthy ? cur["ip"].pyStr : "", netm = cur["netm"].truthy ? cur["netm"].pyStr : ""
        guard Reap.isIPv4(ip), Reap.isIPv4(netm) else {
            throw AppError.runtime("Couldn't read the box's network settings, so nothing was changed.")
        }
        if ip != self.ip { throw AppError.runtime("The box reports IP \(ip), not \(self.ip), so nothing was changed.") }
        let igmpt = Reap.optionIndex(cur["igmpt"], ["off"] + (1...10).map { "\($0) s" }) ?? 0
        let r = try post("eth_s", [("ip", ip), ("netm", netm), ("ethm", String(index)), ("igmpt", String(igmpt))])
        if r["status"] != .int(0) {
            throw AppError.runtime("The box didn't confirm the Ethernet mode change (status \(r["status"].pyStr)).")
        }
    }

    func restart() {
        _ = try? post("sys_res")  // the box drops the connection as it restarts
    }
}
