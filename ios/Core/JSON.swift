import Foundation

/// A loosely typed JSON value. The project file and the web UI's API are plain JSON with
/// open-ended keys, exactly like the Python app's dicts, so the port works on JSON values too:
/// unknown keys in an imported project survive a round trip untouched.
enum JSON: Equatable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSON])
    case object([String: JSON])

    // ------------------------------------------------------------ equality (numbers compare by value)
    static func == (a: JSON, b: JSON) -> Bool {
        switch (a, b) {
        case (.null, .null): return true
        case let (.bool(x), .bool(y)): return x == y
        case let (.string(x), .string(y)): return x == y
        case let (.array(x), .array(y)): return x == y
        case let (.object(x), .object(y)): return x == y
        case let (.int(x), .int(y)): return x == y
        case let (.int(x), .double(y)), let (.double(y), .int(x)): return Double(x) == y
        case let (.double(x), .double(y)): return x == y
        default: return false
        }
    }

    // ------------------------------------------------------------ access
    subscript(key: String) -> JSON {
        get { if case .object(let o) = self { return o[key] ?? .null }; return .null }
        set {
            if case .object(var o) = self { o[key] = newValue; self = .object(o) }
            else if case .null = self { self = .object([key: newValue]) }
        }
    }

    subscript(index: Int) -> JSON {
        get { if case .array(let a) = self, index >= 0, index < a.count { return a[index] }; return .null }
        set { if case .array(var a) = self, index >= 0, index < a.count { a[index] = newValue; self = .array(a) } }
    }

    func has(_ key: String) -> Bool {
        if case .object(let o) = self { return o[key] != nil }
        return false
    }

    mutating func remove(_ key: String) {
        if case .object(var o) = self { o.removeValue(forKey: key); self = .object(o) }
    }

    /// Python's dict.setdefault: set `key` to `value` only when it is missing.
    mutating func setDefault(_ key: String, _ value: JSON) {
        if !has(key) { self[key] = value }
    }

    var isNull: Bool { if case .null = self { return true }; return false }
    var string: String? { if case .string(let s) = self { return s }; return nil }
    var array: [JSON]? { if case .array(let a) = self { return a }; return nil }
    var object: [String: JSON]? { if case .object(let o) = self { return o }; return nil }
    var arrayValue: [JSON] { array ?? [] }
    var objectValue: [String: JSON] { object ?? [:] }
    var isNumber: Bool { switch self { case .int, .double: return true; default: return false } }

    /// Numeric value, if this is a number (bools count, like Python).
    var number: Double? {
        switch self {
        case .int(let i): return Double(i)
        case .double(let d): return d
        case .bool(let b): return b ? 1 : 0
        default: return nil
        }
    }

    var int: Int? {
        switch self {
        case .int(let i): return i
        case .double(let d) where d.isFinite: return Int(d)
        case .bool(let b): return b ? 1 : 0
        default: return nil
        }
    }

    /// Python truthiness.
    var truthy: Bool {
        switch self {
        case .null: return false
        case .bool(let b): return b
        case .int(let i): return i != 0
        case .double(let d): return d != 0
        case .string(let s): return !s.isEmpty
        case .array(let a): return !a.isEmpty
        case .object(let o): return !o.isEmpty
        }
    }

    /// Python `x or default`.
    func or(_ other: JSON) -> JSON { truthy ? self : other }

    /// Python int(x): numbers truncate, numeric strings parse, anything else is an error.
    func toInt(_ what: String = "value") throws -> Int {
        switch self {
        case .int(let i): return i
        case .double(let d):
            guard d.isFinite else { throw AppError.value("cannot convert float \(d) to integer") }
            return Int(d)
        case .bool(let b): return b ? 1 : 0
        case .string(let s):
            if let i = Int(s.trimmingCharacters(in: .whitespaces)) { return i }
            throw AppError.value("invalid literal for int() with base 10: '\(s)'")
        default: throw AppError.value("\(what) must be a number")
        }
    }

    /// Python float(x).
    func toDouble(_ what: String = "value") throws -> Double {
        switch self {
        case .int(let i): return Double(i)
        case .double(let d): return d
        case .bool(let b): return b ? 1 : 0
        case .string(let s):
            if let d = Double(s.trimmingCharacters(in: .whitespaces)) { return d }
            throw AppError.value("could not convert string to float: '\(s)'")
        default: throw AppError.value("\(what) must be a number")
        }
    }

    /// Python str(x) for display.
    var pyStr: String {
        switch self {
        case .null: return "None"
        case .bool(let b): return b ? "True" : "False"
        case .int(let i): return String(i)
        case .double(let d): return JSON.formatDouble(d)
        case .string(let s): return s
        default: return serialize()
        }
    }

    // ------------------------------------------------------------ parse
    static func parse(_ data: Data) throws -> JSON {
        if data.isEmpty { return .object([:]) }
        let obj: Any
        do {
            obj = try JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])
        } catch {
            throw AppError.value("That isn't valid JSON.")
        }
        return from(any: obj)
    }

    static func parse(_ text: String) throws -> JSON { try parse(Data(text.utf8)) }

    static func from(any: Any) -> JSON {
        switch any {
        case is NSNull: return .null
        case let s as String: return .string(s)
        case let n as NSNumber:
            if CFGetTypeID(n) == CFBooleanGetTypeID() { return .bool(n.boolValue) }
            if CFNumberIsFloatType(n) {
                let d = n.doubleValue
                return .double(d)
            }
            return .int(n.intValue)
        case let a as [Any]: return .array(a.map { from(any: $0) })
        case let o as [String: Any]:
            var out: [String: JSON] = [:]
            for (k, v) in o { out[k] = from(any: v) }
            return .object(out)
        default: return .null
        }
    }

    // ------------------------------------------------------------ serialize
    /// Compact JSON, keys sorted (stable output). `indent` > 0 pretty-prints like Python's json.dumps(indent=n).
    func serialize(indent: Int = 0) -> String {
        var out = ""
        write(&out, indent: indent, level: 0)
        return out
    }

    func data(indent: Int = 0) -> Data { Data(serialize(indent: indent).utf8) }

    private func write(_ out: inout String, indent: Int, level: Int) {
        switch self {
        case .null: out += "null"
        case .bool(let b): out += b ? "true" : "false"
        case .int(let i): out += String(i)
        case .double(let d): out += JSON.formatDouble(d)
        case .string(let s): JSON.writeString(s, &out)
        case .array(let a):
            if a.isEmpty { out += "[]"; return }
            out += "["
            for (i, v) in a.enumerated() {
                if i > 0 { out += indent > 0 ? "," : ", " }
                if indent > 0 { out += "\n" + String(repeating: " ", count: indent * (level + 1)) }
                v.write(&out, indent: indent, level: level + 1)
            }
            if indent > 0 { out += "\n" + String(repeating: " ", count: indent * level) }
            out += "]"
        case .object(let o):
            if o.isEmpty { out += "{}"; return }
            out += "{"
            for (i, k) in o.keys.sorted().enumerated() {
                if i > 0 { out += indent > 0 ? "," : ", " }
                if indent > 0 { out += "\n" + String(repeating: " ", count: indent * (level + 1)) }
                JSON.writeString(k, &out)
                out += ": "
                o[k]!.write(&out, indent: indent, level: level + 1)
            }
            if indent > 0 { out += "\n" + String(repeating: " ", count: indent * level) }
            out += "}"
        }
    }

    static func formatDouble(_ d: Double) -> String {
        if d.isNaN { return "NaN" }
        if d.isInfinite { return d > 0 ? "Infinity" : "-Infinity" }
        if d == d.rounded(), abs(d) < 1e16 { return String(format: "%.1f", d) }
        return "\(d)"  // shortest round-trip form, like Python's repr
    }

    private static func writeString(_ s: String, _ out: inout String) {
        out += "\""
        for u in s.unicodeScalars {
            switch u {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if u.value < 0x20 || u.value == 0x2028 || u.value == 0x2029 {
                    out += String(format: "\\u%04x", u.value)
                } else {
                    out.unicodeScalars.append(u)
                }
            }
        }
        out += "\""
    }
}

extension JSON: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral, ExpressibleByFloatLiteral,
                ExpressibleByBooleanLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral, ExpressibleByNilLiteral {
    init(stringLiteral v: String) { self = .string(v) }
    init(integerLiteral v: Int) { self = .int(v) }
    init(floatLiteral v: Double) { self = .double(v) }
    init(booleanLiteral v: Bool) { self = .bool(v) }
    init(arrayLiteral elements: JSON...) { self = .array(elements) }
    init(dictionaryLiteral elements: (String, JSON)...) {
        var o: [String: JSON] = [:]
        for (k, v) in elements { o[k] = v }
        self = .object(o)
    }
    init(nilLiteral: ()) { self = .null }
}

extension JSON {
    init(_ s: String?) { self = s.map { .string($0) } ?? .null }
    init(_ i: Int?) { self = i.map { .int($0) } ?? .null }
    init(_ d: Double) { self = .double(d) }
    init(_ b: Bool) { self = .bool(b) }
    init(_ a: [JSON]) { self = .array(a) }
    init(_ o: [String: JSON]) { self = .object(o) }
}

/// The Python app's error classes, mapped to HTTP codes the same way (server._route).
enum AppError: Error, CustomStringConvertible {
    case value(String)    // ValueError   -> 400
    case runtime(String)  // RuntimeError -> 400 (ReapError is one too)
    case key(String)      // KeyError     -> 404, shown with quotes like Python's str(KeyError)
    case conflict(String) // explicit 409

    var description: String {
        switch self {
        case .value(let m), .runtime(let m), .conflict(let m): return m
        case .key(let m): return "'\(m)'"
        }
    }

    var status: Int {
        switch self {
        case .key: return 404
        case .conflict: return 409
        default: return 400
        }
    }
}

// ------------------------------------------------------------ Python numeric helpers

/// Python's round(x) for floats: round half to even.
@inline(__always) func pyRound(_ x: Double) -> Int { Int(x.rounded(.toNearestOrEven)) }

/// Python's float % (result takes the sign of the divisor).
@inline(__always) func pyMod(_ a: Double, _ b: Double) -> Double {
    var r = fmod(a, b)
    if r != 0 && ((r < 0) != (b < 0)) { r += b }
    return r
}
