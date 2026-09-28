import Foundation

/// Reads local iCloud Notes through the Notes app scripting dictionary.
/// iCloud Notes are NOT exposed over IMAP, so this is the only supported path.
enum NotesService {
    private static let fieldSeparator = "\u{1F}"
    private static let recordSeparator = "\u{1E}"

    static var available: Bool { FileManager.default.fileExists(atPath: "/System/Applications/Notes.app") }

    static func notes(titleMatch: String?, limit: Int = 200) throws -> [[String: Any]] {
        guard available else { throw BridgeError.config("Notes.app blev ikke fundet på denne Mac") }
        let script = """
        set fieldSep to (ASCII character 31)
        set recordSep to (ASCII character 30)
        set targetName to "\(escape(titleMatch ?? ""))"
        set out to ""
        tell application "Notes"
            repeat with n in notes
                set nName to (name of n) as string
                if targetName is "" then
                    set out to out & (id of n as string) & fieldSep & nName & fieldSep & (body of n as string) & fieldSep & ((modification date of n) as string) & recordSep
                else if nName is targetName then
                    set out to out & (id of n as string) & fieldSep & nName & fieldSep & (body of n as string) & fieldSep & ((modification date of n) as string) & recordSep
                end if
            end repeat
        end tell
        return out
        """
        let output = try runScript(script)
        var results: [[String: Any]] = []
        for record in output.components(separatedBy: recordSeparator) where !record.isEmpty {
            let fields = record.components(separatedBy: fieldSeparator)
            guard fields.count >= 3 else { continue }
            let html = fields[2]
            let body = HTML.plainText(from: html)
            let modified = Dates.parseDay(fields.count > 3 ? fields[3] : "") ?? Date()
            results.append([
                "id": fields[0],
                "title": fields[1],
                "text": body,
                "html": html,
                "modified": Dates.iso.string(from: modified),
            ])
            if results.count >= limit { break }
        }
        return results
    }

    static func save(title: String, content: String) throws -> [String: Any] {
        guard available else { throw BridgeError.config("Notes.app blev ikke fundet på denne Mac") }
        let html = htmlBody(content)
        let script = """
        set targetName to "\(escape(title))"
        set newBody to "\(escape(html))"
        tell application "Notes"
            repeat with n in notes
                if (name of n as string) is targetName then
                    set body of n to newBody
                    return ((id of n as string) & fieldSep & (name of n as string) & fieldSep & (body of n as string) & fieldSep & ((modification date of n) as string))
                end if
            end repeat
        end tell
        error "note-not-found"
        """
        let prelude = "set fieldSep to (ASCII character 31)\n"
        let output = try runScript(prelude + script)
        let fields = output.components(separatedBy: fieldSeparator)
        guard fields.count >= 3 else { throw BridgeError.config("Kunne ikke gemme noten") }
        return [
            "id": fields[0],
            "title": fields[1],
            "text": HTML.plainText(from: fields[2]),
            "html": fields[2],
            "modified": Dates.iso.string(from: Dates.parseDay(fields.count > 3 ? fields[3] : "") ?? Date()),
        ]
    }

    /// Turns plain text (possibly markdown-ish) into the small HTML subset Notes accepts.
    private static func htmlBody(_ content: String) -> String {
        let paragraphs = content
            .components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        if paragraphs.isEmpty { return "" }
        return paragraphs.map { "<p>\(escape($0))</p>" }.joined()
    }

    private static func escape(_ value: String) -> String {
        value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    private static func runScript(_ script: String) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        let pipe = Pipe()
        process.standardOutput = pipe
        let errorPipe = Pipe()
        process.standardError = errorPipe
        do {
            try process.run()
        } catch {
            throw BridgeError.config("Kunne ikke starte osascript: \(error.localizedDescription)")
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let errorData = errorPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(data: data, encoding: .utf8) ?? ""
        if process.terminationStatus != 0 {
            let message = String(data: errorData, encoding: .utf8) ?? "ukendt fejl"
            let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.contains("-1743") || trimmed.contains("not allowed") || trimmed.contains("Not authorized") {
                throw BridgeError.config(
                    "Notes.app nægtede adgang. Giv FamilyBridge adgang under Systemindstillinger > Anonymitet og sikkerhed > Automatisering."
                )
            }
            throw BridgeError.config("Notes-script fejlede: \(trimmed)")
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
