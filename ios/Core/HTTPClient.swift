import Foundation
import Compression

/// A tiny blocking HTTP/1.1 client over a plain TCP socket, for the E-Box's built-in web page.
/// Plain sockets (not URLSession) because the box is a bare IP on the venue network, with no TLS,
/// and we want exact control of timeouts. One request per connection ("Connection: close").
enum HTTPClient {
    struct Response { let status: Int; let headers: [String: String]; let body: [UInt8] }

    enum Failure: Error { case unreachable(String), timeout, badResponse(String) }

    static func post(host: String, port: Int, path: String, body: [UInt8], headers: [String: String],
                     timeout: TimeInterval) throws -> Response {
        let deadline = Date().addingTimeInterval(timeout)
        let fd = try connect(host: host, port: port, deadline: deadline)
        defer { close(fd) }
        var req = "POST \(path) HTTP/1.1\r\nHost: \(port == 80 ? host : "\(host):\(port)")\r\n"
        for (k, v) in headers { req += "\(k): \(v)\r\n" }
        req += "Content-Length: \(body.count)\r\nConnection: close\r\n\r\n"
        try sendAll(fd, Array(req.utf8) + body, deadline: deadline)
        var raw: [UInt8] = []
        var buf = [UInt8](repeating: 0, count: 16384)
        while true {
            try waitReadable(fd, deadline: deadline)
            let n = buf.withUnsafeMutableBytes { recv(fd, $0.baseAddress, $0.count, 0) }
            if n == 0 { break }
            if n < 0 {
                if errno == EAGAIN || errno == EINTR { continue }
                if raw.isEmpty { throw Failure.unreachable(String(cString: strerror(errno))) }
                break
            }
            raw += buf[0..<n]
            if let r = try? complete(raw) { return r }
        }
        return try parse(raw)
    }

    // ------------------------------------------------------------ socket plumbing
    private static func connect(host: String, port: Int, deadline: Date) throws -> Int32 {
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        guard inet_pton(AF_INET, host, &addr.sin_addr) == 1 else {
            throw Failure.unreachable("not an IP address: \(host)")
        }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw Failure.unreachable(String(cString: strerror(errno))) }
        var one: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        let flags = fcntl(fd, F_GETFL, 0)
        _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)
        let rc = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if rc != 0 && errno != EINPROGRESS {
            let e = String(cString: strerror(errno)); close(fd)
            throw Failure.unreachable(e)
        }
        if rc != 0 {
            do { try waitWritable(fd, deadline: deadline) } catch { close(fd); throw error }
            var err: Int32 = 0
            var len = socklen_t(MemoryLayout<Int32>.size)
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len)
            if err != 0 { close(fd); throw Failure.unreachable(String(cString: strerror(err))) }
        }
        return fd
    }

    private static func poll1(_ fd: Int32, _ events: Int16, deadline: Date) throws {
        while true {
            let ms = Int32(max(0, deadline.timeIntervalSinceNow * 1000))
            if ms <= 0 { throw Failure.timeout }
            var p = pollfd(fd: fd, events: events, revents: 0)
            let r = Darwin.poll(&p, 1, ms)
            if r > 0 { return }
            if r == 0 { throw Failure.timeout }
            if errno != EINTR { throw Failure.unreachable(String(cString: strerror(errno))) }
        }
    }

    private static func waitWritable(_ fd: Int32, deadline: Date) throws { try poll1(fd, Int16(POLLOUT), deadline: deadline) }
    private static func waitReadable(_ fd: Int32, deadline: Date) throws { try poll1(fd, Int16(POLLIN), deadline: deadline) }

    private static func sendAll(_ fd: Int32, _ bytes: [UInt8], deadline: Date) throws {
        var off = 0
        while off < bytes.count {
            try waitWritable(fd, deadline: deadline)
            let n = bytes[off...].withUnsafeBytes { send(fd, $0.baseAddress, $0.count, 0) }
            if n < 0 {
                if errno == EAGAIN || errno == EINTR { continue }
                throw Failure.unreachable(String(cString: strerror(errno)))
            }
            off += n
        }
    }

    // ------------------------------------------------------------ response parsing
    private static func headerEnd(_ raw: [UInt8]) -> Int? {
        guard raw.count >= 4 else { return nil }
        for i in 0...(raw.count - 4) where raw[i] == 13 && raw[i + 1] == 10 && raw[i + 2] == 13 && raw[i + 3] == 10 {
            return i
        }
        return nil
    }

    /// The response, once all of it has arrived (by Content-Length or a final chunk), else throws.
    private static func complete(_ raw: [UInt8]) throws -> Response {
        guard let he = headerEnd(raw) else { throw Failure.badResponse("incomplete") }
        let (status, headers) = try head(Array(raw[0..<he]))
        let bodyRaw = Array(raw[(he + 4)...])
        if let te = headers["transfer-encoding"], te.lowercased().contains("chunked") {
            guard let body = dechunk(bodyRaw, requireEnd: true) else { throw Failure.badResponse("incomplete") }
            return Response(status: status, headers: headers, body: body)
        }
        if let cl = headers["content-length"].flatMap({ Int($0.trimmingCharacters(in: .whitespaces)) }) {
            guard bodyRaw.count >= cl else { throw Failure.badResponse("incomplete") }
            return Response(status: status, headers: headers, body: Array(bodyRaw[0..<cl]))
        }
        throw Failure.badResponse("incomplete")  // read to EOF
    }

    private static func parse(_ raw: [UInt8]) throws -> Response {
        if let r = try? complete(raw) { return r }
        guard let he = headerEnd(raw) else {
            if raw.isEmpty { throw Failure.unreachable("connection closed") }
            throw Failure.badResponse("no HTTP header")
        }
        let (status, headers) = try head(Array(raw[0..<he]))
        var body = Array(raw[(he + 4)...])
        if let te = headers["transfer-encoding"], te.lowercased().contains("chunked") {
            body = dechunk(body, requireEnd: false) ?? body
        }
        return Response(status: status, headers: headers, body: body)
    }

    private static func head(_ bytes: [UInt8]) throws -> (Int, [String: String]) {
        let text = String(decoding: bytes, as: UTF8.self)
        var lines = text.components(separatedBy: "\r\n")
        let statusLine = lines.removeFirst().split(separator: " ")
        guard statusLine.count >= 2, statusLine[0].hasPrefix("HTTP/"), let code = Int(statusLine[1]) else {
            throw Failure.badResponse("bad status line")
        }
        var headers: [String: String] = [:]
        for l in lines {
            guard let c = l.firstIndex(of: ":") else { continue }
            headers[l[..<c].lowercased()] = String(l[l.index(after: c)...]).trimmingCharacters(in: .whitespaces)
        }
        return (code, headers)
    }

    private static func dechunk(_ b: [UInt8], requireEnd: Bool) -> [UInt8]? {
        var out: [UInt8] = [], i = 0
        while true {
            guard let le = indexOfCRLF(b, from: i) else { return requireEnd ? nil : out }
            let sizeText = String(decoding: b[i..<le], as: UTF8.self).split(separator: ";").first ?? ""
            guard let size = Int(sizeText.trimmingCharacters(in: .whitespaces), radix: 16) else { return requireEnd ? nil : out }
            if size == 0 { return out }
            let start = le + 2
            guard start + size <= b.count else { return requireEnd ? nil : out + b[start...] }
            out += b[start..<(start + size)]
            i = start + size + 2
            if i > b.count { return requireEnd ? nil : out }
        }
    }

    private static func indexOfCRLF(_ b: [UInt8], from: Int) -> Int? {
        var i = from
        while i + 1 < b.count { if b[i] == 13 && b[i + 1] == 10 { return i }; i += 1 }
        return nil
    }
}

/// gzip decoding (the box's web page may send gzip-compressed JSON).
enum Gzip {
    static func isGzip(_ b: [UInt8]) -> Bool { b.count >= 2 && b[0] == 0x1f && b[1] == 0x8b }

    static func decompress(_ b: [UInt8]) throws -> [UInt8] {
        guard isGzip(b), b.count >= 18, b[2] == 8 else { throw AppError.runtime("bad gzip data") }
        let flags = b[3]
        var i = 10
        if flags & 0x04 != 0 { guard i + 2 <= b.count else { throw AppError.runtime("bad gzip data") }
            i += 2 + (Int(b[i]) | Int(b[i + 1]) << 8) }
        if flags & 0x08 != 0 { while i < b.count && b[i] != 0 { i += 1 }; i += 1 }
        if flags & 0x10 != 0 { while i < b.count && b[i] != 0 { i += 1 }; i += 1 }
        if flags & 0x02 != 0 { i += 2 }
        guard i < b.count - 8 else { throw AppError.runtime("bad gzip data") }
        let deflated = Array(b[i..<(b.count - 8)])
        let n = b.count
        let isize = Int(b[n - 4]) | Int(b[n - 3]) << 8 | Int(b[n - 2]) << 16 | Int(b[n - 1]) << 24
        var cap = max(isize, 64) + 64
        while cap < 64 << 20 {
            var out = [UInt8](repeating: 0, count: cap)
            let got = deflated.withUnsafeBufferPointer { src in
                out.withUnsafeMutableBufferPointer { dst in
                    compression_decode_buffer(dst.baseAddress!, cap, src.baseAddress!, src.count, nil, COMPRESSION_ZLIB)
                }
            }
            if got > 0 && got < cap { return Array(out[0..<got]) }
            if got == 0 && isize == 0 { return [] }
            cap *= 4
        }
        throw AppError.runtime("bad gzip data")
    }
}
