import Foundation

/// Art-Net 4 packet encode/decode (port of scenebuilder/artnet.py, the subset the app needs).
/// Port-Address = Net (7 bits) << 8 | Sub-Net (4 bits) << 4 | Universe (4 bits).
enum ArtNet {
    static let port = 6454
    static let header: [UInt8] = Array("Art-Net".utf8) + [0]
    static let protVer = 14
    static let opPoll = 0x2000
    static let opPollReply = 0x2100
    static let opDmx = 0x5000

    static func portAddress(_ net: Int, _ subnet: Int, _ universe: Int) throws -> Int {
        guard (0...127).contains(net), (0...15).contains(subnet), (0...15).contains(universe) else {
            throw AppError.value("net 0-127, subnet 0-15, universe 0-15")
        }
        return (net << 8) | (subnet << 4) | universe
    }

    static func opcode(_ d: [UInt8]) -> Int? {
        guard d.count >= 10, Array(d[0..<8]) == header else { return nil }
        return Int(d[8]) | Int(d[9]) << 8
    }

    private static func head(_ op: Int) -> [UInt8] {
        header + [UInt8(op & 0xFF), UInt8(op >> 8), UInt8(protVer >> 8), UInt8(protVer & 0xFF)]
    }

    // ------------------------------------------------------------ ArtPoll
    static func buildPoll(flags: UInt8 = 0) -> [UInt8] {
        head(opPoll) + [flags, 0x10] + [UInt8](repeating: 0, count: 8)
    }

    // ------------------------------------------------------------ ArtPollReply
    struct PollReply {
        var ip: String, port: Int, firmware: Int, net: Int, subnet: Int, oem: Int, esta: Int
        var shortName: String, longName: String, nodeReport: String
        var numPorts: Int, portTypes: [Int], swIn: [Int], swOut: [Int], goodOutput: [Int]
        var mac: String, bindIp: String, bindIndex: Int, status2: Int, style: Int, status1: Int

        var outputPortAddresses: [Int] {
            var out: [Int] = []
            for i in 0..<min(numPorts, 4) where portTypes[i] & 0x80 != 0 {
                out.append((net << 8) | (subnet << 4) | (swOut[i] & 0x0F))
            }
            return out
        }
        var rdmCapable: Bool { status1 & 0x02 != 0 }

        func toJSON() -> JSON {
            let ints: ([Int]) -> JSON = { .array($0.map { .int($0) }) }
            return ["ip": .string(ip), "port": .int(port), "firmware": .int(firmware), "net": .int(net),
                    "subnet": .int(subnet), "oem": .int(oem), "esta": .int(esta), "short_name": .string(shortName),
                    "long_name": .string(longName), "node_report": .string(nodeReport), "num_ports": .int(numPorts),
                    "port_types": ints(portTypes), "sw_in": ints(swIn), "sw_out": ints(swOut),
                    "good_output": ints(goodOutput), "mac": .string(mac), "bind_ip": .string(bindIp),
                    "bind_index": .int(bindIndex), "status2": .int(status2), "style": .int(style),
                    "status1": .int(status1), "output_port_addresses": ints(outputPortAddresses),
                    "rdm_capable": .bool(rdmCapable)]
        }
    }

    private static func cstr(_ b: ArraySlice<UInt8>) -> String {
        let bytes = b.prefix { $0 != 0 }
        // latin-1: every byte is one character
        let s = String(bytes.map { Character(Unicode.Scalar($0)) })
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func ip(_ d: [UInt8], _ at: Int) -> String {
        "\(d[at]).\(d[at + 1]).\(d[at + 2]).\(d[at + 3])"
    }

    static func parsePollReply(_ d: [UInt8]) -> PollReply? {
        guard opcode(d) == opPollReply, d.count >= 207 else { return nil }
        let addr = ip(d, 10)
        let mac = d[201..<207].map { String(format: "%02x", $0) }.joined(separator: ":")
        return PollReply(
            ip: addr, port: Int(d[14]) | Int(d[15]) << 8, firmware: Int(d[16]) << 8 | Int(d[17]),
            net: Int(d[18] & 0x7F), subnet: Int(d[19] & 0x0F), oem: Int(d[20]) << 8 | Int(d[21]),
            esta: Int(d[24]) | Int(d[25]) << 8,
            shortName: cstr(d[26..<44]), longName: cstr(d[44..<108]), nodeReport: cstr(d[108..<172]),
            numPorts: Int(d[172]) << 8 | Int(d[173]), portTypes: d[174..<178].map { Int($0) },
            swIn: d[186..<190].map { Int($0) }, swOut: d[190..<194].map { Int($0) },
            goodOutput: d[182..<186].map { Int($0) }, mac: mac,
            bindIp: d.count >= 211 ? ip(d, 207) : addr, bindIndex: d.count > 211 ? Int(d[211]) : 0,
            status2: d.count > 212 ? Int(d[212]) : 0, style: Int(d[200]), status1: Int(d[23]))
    }

    /// Used by tests (the Python simulator builds these). universes = 4-bit universe numbers (max 4).
    static func buildPollReply(ip: String, net: Int, subnet: Int, universes: [Int], shortName: String,
                               longName: String, mac: [UInt8] = [0, 0, 0, 0, 0, 0], esta: Int = 0x5253,
                               oem: Int = 0, bindIndex: Int = 1, nodeReport: String = "#0001 [0000] OK") -> [UInt8] {
        var b = [UInt8](repeating: 0, count: 239)
        b.replaceSubrange(0..<8, with: header)
        b[8] = UInt8(opPollReply & 0xFF); b[9] = UInt8(opPollReply >> 8)
        let parts = ip.split(separator: ".").map { UInt8($0) ?? 0 }
        for i in 0..<4 { b[10 + i] = parts[i] }
        b[14] = UInt8(port & 0xFF); b[15] = UInt8(port >> 8)
        b[16] = 0x01; b[17] = 0x00
        b[18] = UInt8(net & 0x7F); b[19] = UInt8(subnet & 0x0F)
        b[20] = UInt8((oem >> 8) & 0xFF); b[21] = UInt8(oem & 0xFF)
        b[23] = 0xE0 | 0x02
        b[24] = UInt8(esta & 0xFF); b[25] = UInt8((esta >> 8) & 0xFF)
        let sn = Array(shortName.utf8.prefix(17)), ln = Array(longName.utf8.prefix(63))
        b.replaceSubrange(26..<(26 + sn.count), with: sn)
        b.replaceSubrange(44..<(44 + ln.count), with: ln)
        let nr = Array(nodeReport.utf8.prefix(63))
        b.replaceSubrange(108..<(108 + nr.count), with: nr)
        let n = min(universes.count, 4)
        b[172] = UInt8(n >> 8); b[173] = UInt8(n & 0xFF)
        for i in 0..<n {
            b[174 + i] = 0x80
            b[182 + i] = 0x80
            b[190 + i] = UInt8(universes[i] & 0x0F)
        }
        b[200] = 0
        for i in 0..<min(6, mac.count) { b[201 + i] = mac[i] }
        for i in 0..<4 { b[207 + i] = b[10 + i] }
        b[211] = UInt8(bindIndex & 0xFF)
        b[212] = 0x08
        return b
    }

    // ------------------------------------------------------------ ArtDmx
    static func buildDmx(pa: Int, data: [UInt8], sequence: Int = 0, physical: Int = 0) -> [UInt8] {
        var d = data
        if d.count < 2 { d += [UInt8](repeating: 0, count: 2 - d.count) }
        else if d.count % 2 == 1 { d.append(0) }
        if d.count > 512 { d = Array(d[0..<512]) }
        return head(opDmx) + [UInt8(sequence & 0xFF), UInt8(physical & 0xFF), UInt8(pa & 0xFF), UInt8((pa >> 8) & 0x7F),
                              UInt8(d.count >> 8), UInt8(d.count & 0xFF)] + d
    }

    struct Dmx { var sequence: Int, physical: Int, portAddress: Int, data: [UInt8] }

    static func parseDmx(_ d: [UInt8]) -> Dmx? {
        guard opcode(d) == opDmx, d.count >= 18 else { return nil }
        let length = Int(d[16]) << 8 | Int(d[17])
        let end = min(d.count, 18 + length)
        return Dmx(sequence: Int(d[12]), physical: Int(d[13]), portAddress: Int(d[15]) << 8 | Int(d[14]),
                   data: Array(d[18..<end]))
    }
}
