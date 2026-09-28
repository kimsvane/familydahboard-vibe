import Foundation

enum BridgeError: Error {
    case config(String)
    case http(String)
    case eventKit(String)
    case reminders(String)

    var message: String {
        switch self {
        case .config(let text): return "config: \(text)"
        case .http(let text): return "http: \(text)"
        case .eventKit(let text): return "eventkit: \(text)"
        case .reminders(let text): return "reminders: \(text)"
        }
    }
}

enum Log {
    private static let queue = DispatchQueue(label: "bridge.log")
    private static let url: URL = {
        let base = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        return base.appendingPathComponent("FamilyBridge.log")
    }()

    private static func write(_ line: String) {
        let stamp = ISO8601DateFormatter().string(from: Date())
        let text = "\(stamp) \(line)\n"
        FileHandle.standardError.write(Data(text.utf8))
        queue.sync {
            guard let data = text.data(using: .utf8) else { return }
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(data)
                try? handle.close()
            } else {
                try? data.write(to: url)
            }
        }
    }

    static func info(_ message: String) { write("INFO  \(message)") }
    static func error(_ message: String) { write("ERROR \(message)") }
}

struct BridgeConfig {
    var token: String
    var host: String
    var port: UInt16
    var notesTitle: String

    static var directory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/FamilyBridge", isDirectory: true)
    }

    static var fileURL: URL { directory.appendingPathComponent("config.json") }

    /// Listens on the LAN so the dashboard on the NAS can reach it.
    /// Override with FAMILYBRIDGE_HOST when the Mac should stay local-only.
    static var defaultHost: String {
        let override = ProcessInfo.processInfo.environment["FAMILYBRIDGE_HOST"] ?? ""
        return override.isEmpty ? "0.0.0.0" : override
    }

    static func load() throws -> BridgeConfig {
        let file = fileURL
        guard FileManager.default.fileExists(atPath: file.path) else {
            let fresh = BridgeConfig(
                token: Token.random(),
                host: defaultHost,
                port: 8787,
                notesTitle: ""
            )
            try fresh.save()
            Log.info("Nyt config oprettet i \(file.path)")
            return fresh
        }
        let data = try Data(contentsOf: file)
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        let token = (json["token"] as? String) ?? ""
        let storedHost = (json["host"] as? String) ?? ""
        // 127.0.0.1 can only ever be reached from the Mac itself, so migrate it.
        let host = storedHost.isEmpty || storedHost == "127.0.0.1" || storedHost == "localhost"
            ? defaultHost
            : storedHost
        let config = BridgeConfig(
            token: token.isEmpty ? Token.random() : token,
            host: host,
            port: UInt16((json["port"] as? Int) ?? 8787),
            notesTitle: (json["notes_title"] as? String) ?? ""
        )
        if config.token != token || host != storedHost { try config.save() }
        return config
    }

    func save() throws {
        try FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        let payload: [String: Any] = [
            "token": token,
            "host": host,
            "port": Int(port),
            "notes_title": notesTitle,
        ]
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted])
        try data.write(to: Self.fileURL, options: .atomic)
    }
}

enum Token {
    private static let alphabet = Array("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789")

    static func random(length: Int = 40) -> String {
        var value = ""
        var buffer = [UInt8](repeating: 0, count: length)
        for index in buffer.indices {
            buffer[index] = UInt8.random(in: UInt8.min...UInt8.max)
        }
        for byte in buffer { value.append(alphabet[Int(byte) % alphabet.count]) }
        return value
    }

    static func matches(_ lhs: String, _ rhs: String) -> Bool {
        let left = Array(lhs.utf8)
        let right = Array(rhs.utf8)
        guard left.count == right.count, !left.isEmpty else { return false }
        var diff: UInt8 = 0
        for index in left.indices { diff |= left[index] ^ right[index] }
        return diff == 0
    }
}

enum HTML {
    private static let entities: [String: String] = [
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": "\"",
        "&#39;": "'", "&apos;": "'", "&hellip;": "…", "&mdash;": "–", "&ndash;": "-",
    ]

    static func plainText(from html: String) -> String {
        var text = html
        text = text.replacingOccurrences(
            of: "<br\\s*/?>|</p>|</div>|</li>",
            with: "\n",
            options: [.regularExpression, .caseInsensitive]
        )
        text = text.replacingOccurrences(of: "<[^>]+>", with: "", options: .regularExpression)
        for (entity, replacement) in entities {
            text = text.replacingOccurrences(of: entity, with: replacement, options: .caseInsensitive)
        }
        text = text.replacingOccurrences(of: "\\s+\n", with: "\n", options: .regularExpression)
        return Trim.linesAndSpaces(text)
    }
}

/// Ren-Swift-trim. Bevidst undviger CharacterSet.whitespaces /
/// .whitespacesAndNewlines: de er Swift-statiske egenskaber i Foundation-
/// overlayet, som ikke findes i alle macOS-versioners runtime (f.eks. Monterey),
/// og giver en dyld-fejl ved start.
enum Trim {
    private static func isSpace(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value {
        case 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0:
            return true
        default:
            return false
        }
    }

    ///Fjerner for- og bagvedlige mellemrum.
    static func spaces(_ value: String) -> String {
        var scalars = Array(value.unicodeScalars)
        var start = scalars.startIndex
        var end = scalars.endIndex
        while start < end, isSpace(scalars[start]) { start += 1 }
        while end > start, isSpace(scalars[end - 1]) { end -= 1 }
        return String(String.UnicodeScalarView(scalars[start..<end]))
    }

    /// Fjerner for- og bagvedlige mellemrum, inkl. linjeskift.
    static func linesAndSpaces(_ value: String) -> String {
        spaces(value)
    }
}

enum Dates {
    static let isoDay: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }()

    static let iso: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        return formatter
    }()

    static func day(_ date: Date?) -> String? {
        guard let date else { return nil }
        return isoDay.string(from: date)
    }

    static func stamp(_ date: Date?) -> String? {
        guard let date else { return nil }
        return iso.string(from: date)
    }

    static func parseDay(_ value: String) -> Date? {
        let trimmed = Trim.spaces(value)
        guard !trimmed.isEmpty else { return nil }
        if let date = isoDay.date(from: trimmed) { return date }
        let withTime = DateFormatter()
        withTime.locale = Locale(identifier: "en_US_POSIX")
        withTime.timeZone = TimeZone(secondsFromGMT: 0)
        withTime.dateFormat = "yyyy-MM-dd'T'HH:mm:ssZ"
        return withTime.date(from: trimmed) ?? withTime.date(from: trimmed + "Z")
    }
}
