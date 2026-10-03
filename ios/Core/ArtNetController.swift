import Foundation

/// One Art-Net output: (box ip, UDP port, Port-Address).
struct Target: Hashable {
    let ip: String
    let port: Int
    let pa: Int
}

/// A network adapter's IPv4 address (getifaddrs).
struct Interface {
    let name: String, ip: String, netmask: String
    func toJSON() -> JSON { ["name": .string(name), "ip": .string(ip), "netmask": .string(netmask), "broadcast": .null] }
}

/// Art-Net controller: discovery and continuous DMX output. Port of the parts of
/// scenebuilder/node.py the iPad needs (RDM over Art-Net is not used: the real box doesn't
/// bridge it; the box's web page does that job).
///
/// iOS can't send broadcasts without Apple's multicast entitlement, so discovery asks every
/// host on the Wi-Fi's subnet directly (a unicast ArtPoll each), plus the boxes' saved IPs.
final class ArtNetController {
    private(set) var port: Int
    let fps: Double
    private var fd: Int32 = -1
    private(set) var bindError: String?
    private let lock = NSRecursiveLock()
    private var buffers: [Target: [UInt8]] = [:]
    private var seq: [Target: Int] = [:]
    private var _outputEnabled = false
    var nodes: [String: JSON] = [:]
    private var stopped = false
    private(set) var packetsSent = 0
    private(set) var packetsReceived = 0
    private(set) var lastSendError: String?
    /// While false the transmit loop sends nothing (app in the background).
    var suspended = false
    /// Every DMX packet goes through here first; tests use it to watch output.
    var onSend: ((Target, [UInt8]) -> Void)?
    /// Discovery also polls every host on the Wi-Fi subnet. Tests turn this off (they only use 127.0.0.1).
    static var sweepSubnets = true

    init(port: Int = ArtNet.port, fps: Double = 30.0) {
        self.port = port
        self.fps = fps
    }

    var outputEnabled: Bool {
        get { lock.lock(); defer { lock.unlock() }; return _outputEnabled }
        set { lock.lock(); _outputEnabled = newValue; lock.unlock() }
    }

    // ------------------------------------------------------------ lifecycle
    @discardableResult
    func start() -> ArtNetController {
        fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, socklen_t(MemoryLayout<Int32>.size))
        var tv = timeval(tv_sec: 0, tv_usec: 200_000)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        if !bind(port) {
            // No SO_REUSEPORT: if another Art-Net app holds the port we want to know, not lose replies.
            bindError = "Could not open Art-Net port \(port) (\(String(cString: strerror(errno)))). Is another lighting app open?"
            _ = bind(0)
        }
        var sa = sockaddr_in()
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &sa) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        port = Int(UInt16(bigEndian: sa.sin_port))
        let rx = Thread { [weak self] in self?.rxLoop() }
        rx.name = "artnet-rx"; rx.qualityOfService = .userInitiated; rx.start()
        let tx = Thread { [weak self] in self?.txLoop() }
        tx.name = "artnet-tx"; tx.qualityOfService = .userInteractive; tx.start()
        return self
    }

    private func bind(_ p: Int) -> Bool {
        var sa = sockaddr_in()
        sa.sin_family = sa_family_t(AF_INET)
        sa.sin_port = in_port_t(UInt16(p).bigEndian)
        sa.sin_addr = in_addr(s_addr: INADDR_ANY)
        return withUnsafePointer(to: &sa) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) == 0
            }
        }
    }

    func stop() {
        lock.lock(); stopped = true; lock.unlock()
        Thread.sleep(forTimeInterval: 0.25)
        if fd >= 0 { close(fd); fd = -1 }
    }

    // ------------------------------------------------------------ sending
    private func sendTo(_ data: [UInt8], _ ip: String, _ port: Int) -> Int32 {
        var sa = sockaddr_in()
        sa.sin_family = sa_family_t(AF_INET)
        sa.sin_port = in_port_t(UInt16(truncatingIfNeeded: port).bigEndian)
        guard inet_pton(AF_INET, ip, &sa.sin_addr) == 1 else { errno = EINVAL; return -1 }
        let n = data.withUnsafeBytes { buf in
            withUnsafePointer(to: &sa) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    sendto(fd, buf.baseAddress, buf.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        return n < 0 ? -1 : 0
    }

    @discardableResult
    func send(_ data: [UInt8], _ ip: String, _ port: Int) -> Bool {
        if sendTo(data, ip, port) == 0 {
            lock.lock(); packetsSent += 1; lastSendError = nil; lock.unlock()
            return true
        }
        let e = "\(ip):\(port) - \(String(cString: strerror(errno)))"
        lock.lock(); lastSendError = e; lock.unlock()
        return false
    }

    /// For discovery: most swept addresses are empty, which isn't an error worth showing.
    @discardableResult
    func sendQuiet(_ data: [UInt8], _ ip: String, _ port: Int) -> Bool {
        if sendTo(data, ip, port) == 0 {
            lock.lock(); packetsSent += 1; lock.unlock()
            return true
        }
        return false
    }

    func clearTargets() {
        lock.lock(); buffers.removeAll(); lock.unlock()
    }

    /// Replace the whole output set atomically: a frame is always built complete before it can be sent.
    func setTargets(_ frames: [Target: [UInt8]]) {
        var b: [Target: [UInt8]] = [:]
        for (t, d) in frames {
            var x = Array(d.prefix(512))
            if x.count < 512 { x += [UInt8](repeating: 0, count: 512 - x.count) }
            b[t] = x
        }
        lock.lock(); buffers = b; lock.unlock()
    }

    func universe(_ t: Target) -> [UInt8] {
        lock.lock(); defer { lock.unlock() }
        return buffers[t] ?? [UInt8](repeating: 0, count: 512)
    }

    var targets: [Target] { lock.lock(); defer { lock.unlock() }; return Array(buffers.keys) }

    func sendNow() {
        lock.lock()
        let items = buffers
        lock.unlock()
        for (t, data) in items {
            lock.lock()
            let s = (seq[t] ?? 0) % 255 + 1
            seq[t] = s
            lock.unlock()
            onSend?(t, data)
            send(ArtNet.buildDmx(pa: t.pa, data: data, sequence: s), t.ip, t.port)
        }
    }

    private func txLoop() {
        let period = 1.0 / fps
        var nxt = Date().timeIntervalSinceReferenceDate
        while true {
            lock.lock(); let stop = stopped; let on = _outputEnabled && !suspended; lock.unlock()
            if stop { return }
            if on { sendNow() }
            nxt += period
            let delay = nxt - Date().timeIntervalSinceReferenceDate
            if delay < -1 { nxt = Date().timeIntervalSinceReferenceDate }
            else if delay > 0 { Thread.sleep(forTimeInterval: delay) }
        }
    }

    // ------------------------------------------------------------ receiving
    private func rxLoop() {
        var buf = [UInt8](repeating: 0, count: 2048)
        while true {
            lock.lock(); let stop = stopped; let sock = fd; lock.unlock()
            if stop || sock < 0 { return }
            var from = sockaddr_in()
            var len = socklen_t(MemoryLayout<sockaddr_in>.size)
            let n = buf.withUnsafeMutableBytes { b in
                withUnsafeMutablePointer(to: &from) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { recvfrom(sock, b.baseAddress, b.count, 0, $0, &len) }
                }
            }
            if n <= 0 {
                if errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR { Thread.sleep(forTimeInterval: 0.1) }
                continue
            }
            lock.lock(); packetsReceived += 1; lock.unlock()
            var ipBuf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
            inet_ntop(AF_INET, &from.sin_addr, &ipBuf, socklen_t(INET_ADDRSTRLEN))
            handle(Array(buf[0..<n]), String(cString: ipBuf), Int(UInt16(bigEndian: from.sin_port)))
        }
    }

    private func handle(_ data: [UInt8], _ ip: String, _ port: Int) {
        guard ArtNet.opcode(data) == ArtNet.opPollReply, let pr = ArtNet.parsePollReply(data) else { return }
        var d = pr.toJSON()
        d["from"] = .string("\(ip):\(port)")
        d["udp_port"] = .int(port)
        d["seen"] = .double(Date().timeIntervalSince1970)
        lock.lock(); nodes["\(ip):\(port)/\(pr.bindIndex)"] = d; lock.unlock()
    }

    // ------------------------------------------------------------ discovery
    /// This device's IPv4 adapters (Wi-Fi is en0; a USB-C Ethernet adapter shows as en1+).
    static func localInterfaces() -> [Interface] {
        var out: [Interface] = []
        var ifap: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifap) == 0, let first = ifap else { return out }
        defer { freeifaddrs(ifap) }
        var p: UnsafeMutablePointer<ifaddrs>? = first
        while let cur = p {
            let i = cur.pointee
            p = i.ifa_next
            guard let sa = i.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET), let nm = i.ifa_netmask,
                  (i.ifa_flags & UInt32(IFF_UP)) != 0 else { continue }
            let name = String(cString: i.ifa_name)
            func str(_ s: UnsafeMutablePointer<sockaddr>) -> String {
                var buf = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                s.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { sin in
                    var a = sin.pointee.sin_addr
                    _ = inet_ntop(AF_INET, &a, &buf, socklen_t(INET_ADDRSTRLEN))
                }
                return String(cString: buf)
            }
            out.append(Interface(name: name, ip: str(sa), netmask: str(nm)))
        }
        return out
    }

    /// The interfaces discovery sweeps: real adapters, not loopback or link-local, Wi-Fi first.
    static func sweepInterfaces() -> [Interface] {
        localInterfaces().filter { !$0.ip.hasPrefix("127.") && !$0.ip.hasPrefix("169.254.") && $0.name.hasPrefix("en") }
            .sorted { ($0.name == "en0" ? 0 : 1, $0.name) < ($1.name == "en0" ? 0 : 1, $1.name) }
    }

    /// Every host address on ip/mask, if the network is small enough to poll one by one.
    static func hosts(_ ip: String, _ mask: String, limit: Int = 1022) -> [String] {
        var a = in_addr(), m = in_addr()
        guard inet_pton(AF_INET, ip, &a) == 1, inet_pton(AF_INET, mask, &m) == 1 else { return [] }
        let ipn = UInt32(bigEndian: a.s_addr), mn = UInt32(bigEndian: m.s_addr)
        let size = Int(~mn) + 1
        if size - 2 > limit || size < 4 { return [] }
        let net = ipn & mn
        var out: [String] = []
        for i in 1..<(size - 1) {
            let h = net + UInt32(i)
            if h == ipn { continue }
            out.append("\(h >> 24).\((h >> 16) & 0xFF).\((h >> 8) & 0xFF).\(h & 0xFF)")
        }
        return out
    }

    /// Poll every host on the Wi-Fi's subnet (and the extra targets) and collect who answers.
    func poll(extraTargets: [(String, Int)] = [], wait: TimeInterval = 2.0, sweep: Bool = ArtNetController.sweepSubnets) -> [JSON] {
        let pkt = ArtNet.buildPoll()
        var dests: [String] = []
        var seen = Set<String>()
        func add(_ ip: String, _ port: Int) { let k = "\(ip):\(port)"; if seen.insert(k).inserted { dests.append(k) } }
        for t in extraTargets { add(t.0, t.1) }  // the boxes we already know first
        if sweep {
            for itf in ArtNetController.sweepInterfaces() {
                for h in ArtNetController.hosts(itf.ip, itf.netmask) { add(h, ArtNet.port) }
            }
        }
        let started = Date().timeIntervalSince1970
        for (n, d) in dests.enumerated() {
            let parts = d.split(separator: ":")
            let ip = String(parts[0]), port = Int(parts[1]) ?? ArtNet.port
            // A full send buffer (hundreds of polls at once on Wi-Fi) is not a missing box: retry briefly.
            var tries = 0
            while !sendQuiet(pkt, ip, port) && errno == ENOBUFS && tries < 20 {
                tries += 1
                Thread.sleep(forTimeInterval: 0.005)
            }
            if n % 64 == 63 { Thread.sleep(forTimeInterval: 0.01) }
        }
        Thread.sleep(forTimeInterval: wait)
        lock.lock(); defer { lock.unlock() }
        return nodes.values.filter { ($0["seen"].number ?? 0) >= started }
            .sorted { ($0["ip"].string ?? "") < ($1["ip"].string ?? "") }
    }

    /// When a box at this IP last answered a poll (0 = never).
    func lastSeen(_ ip: String) -> Double {
        lock.lock(); defer { lock.unlock() }
        var t = 0.0
        for n in nodes.values where n["ip"].string == ip || (n["from"].string ?? "").split(separator: ":").first.map(String.init) == ip {
            t = max(t, n["seen"].number ?? 0)
        }
        return t
    }

    /// Does a box answer an Art-Net poll sent straight to it?
    func alive(_ ip: String, _ port: Int, wait: TimeInterval = 0.8) -> Bool {
        let t0 = Date().timeIntervalSince1970
        sendQuiet(ArtNet.buildPoll(), ip, port)
        while Date().timeIntervalSince1970 - t0 < wait {
            Thread.sleep(forTimeInterval: 0.05)
            lock.lock()
            let hit = nodes.values.contains { $0["ip"].string == ip && ($0["seen"].number ?? 0) >= t0 - 0.01 }
            lock.unlock()
            if hit { return true }
        }
        return false
    }
}
