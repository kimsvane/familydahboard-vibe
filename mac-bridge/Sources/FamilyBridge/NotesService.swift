import Foundation

/// Reads local iCloud Notes through the Notes app scripting dictionary.
/// iCloud Notes are NOT exposed over IMAP, so this is the only supported path.
enum NotesService {
    private static let fieldSeparator = "\u{1F}"
    private static let recordSeparator = "\u{1E}"

    static var available: Bool { FileManager.default.fileExists(atPath: "/System/Applications/Notes.app") }

    static func notes(titleMatch: String?, limit: Int = 200) throws -> [[String: Any]] {
        guard available else { throw BridgeError.config("Notes.app blev ikke fundet på denne Mac") }
        // `body of n` er en Apple Event pr. note. En konto med 142 noter kan
        // derfor ikke hente alle kroppe inden for timeout'en, så uden en titel
        // returnerer vi kun id, titel og dato.
        let wantsBody = !(titleMatch ?? "").isEmpty
        let script = """
        set fieldSep to (ASCII character 31)
        set recordSep to (ASCII character 30)
        set targetName to "\(escape(titleMatch ?? ""))"
        set out to ""
        tell application "Notes"
            repeat with n in notes
                set nName to (name of n) as string
                if targetName is "" then
                    set out to out & (id of n as string) & fieldSep & nName & fieldSep & ((modification date of n) as string) & recordSep
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
            guard fields.count >= 2 else { continue }
            // AppleScripts `is` er case-insensitiv, så "fmd" ville ramme "FMD".
            // Den eksakte kontrol sker derfor her i Swift.
            if let wanted = titleMatch, !wanted.isEmpty, fields[1] != wanted { continue }
            let html = wantsBody && fields.count > 2 ? fields[2] : ""
            let body = HTML.plainText(from: html)
            let modified = Dates.parseDay(fields.last ?? "") ?? Date()
            results.append([
                "id": fields[0],
                "title": fields[1],
                "text": body,
                "html": html,
                "body_loaded": wantsBody,
                "modified": Dates.iso.string(from: modified),
            ])
            if results.count >= limit { break }
        }
        return results
    }

    static func save(title: String, content: String) throws -> [String: Any] {
        guard available else { throw BridgeError.config("Notes.app blev ikke fundet på denne Mac") }

        // AppleScripts `is` er case-insensitiv, så vi må ikke skrive direkte på
        // et match. Først finder vi den eksakte id, derefter skriver vi til den.
        let listing = try runScript("""
        set fieldSep to (ASCII character 31)
        set recordSep to (ASCII character 30)
        set out to ""
        tell application "Notes"
            repeat with n in notes
                set out to out & (id of n as string) & fieldSep & ((name of n) as string) & recordSep
            end repeat
        end tell
        return out
        """)
        var matches: [(id: String, name: String)] = []
        for record in listing.components(separatedBy: recordSeparator) where !record.isEmpty {
            let fields = record.components(separatedBy: fieldSeparator)
            guard fields.count >= 2, fields[1] == title else { continue }
            matches.append((fields[0], fields[1]))
        }
        if matches.isEmpty {
            throw BridgeError.config("Ingen note med titlen \"\(title)\" blev fundet.")
        }
        if matches.count > 1 {
            throw BridgeError.config(
                "Der er \(matches.count) noter med titlen \"\(title)\". Giv dem unikke titler, ellers ved vi ikke hvilken der skal skrives."
            )
        }

        let html = htmlBody(content)
        let output = try runScript("""
        set fieldSep to (ASCII character 31)
        set newBody to "\(escape(html))"
        tell application "Notes"
            set n to note id "\(escape(matches[0].id))"
            set body of n to newBody
            return ((id of n as string) & fieldSep & (name of n as string) & fieldSep & (body of n as string) & fieldSep & ((modification date of n) as string))
        end tell
        """)
        let fields = output.components(separatedBy: fieldSeparator)
        guard fields.count >= 3 else { throw BridgeError.config("Kunne ikke gemme noten") }
        return [
            "id": fields[0],
            "title": fields[1],
            "text": HTML.plainText(from: fields[2]),
            "html": fields[2],
            "body_loaded": true,
            "modified": Dates.iso.string(from: Dates.parseDay(fields.count > 3 ? fields[3] : "") ?? Date()),
        ]
    }

    /// Turns plain text (possibly markdown-ish) into the small HTML subset Notes accepts.
    private static func htmlBody(_ content: String) -> String {
        let paragraphs = content
            .components(separatedBy: "\n")
            .map { Trim.spaces($0) }
            .filter { !$0.isEmpty }
        if paragraphs.isEmpty { return "" }
        return paragraphs.map { "<p>\(escape($0))</p>" }.joined()
    }

    private static func escape(_ value: String) -> String {
        value
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    /// Timeout for et Notes-script. Uden den hænger en anmodning, der venter
    /// på Notes.app eller på en TCC-dialog, der aldrig kan vises headless.
    private static let scriptTimeout: TimeInterval = 20

    /// Samler output fra læsetråden, så den kan afleveres når tidsfristen er ude.
    private final class ThreadReader {
        let lock: NSLock
        var data = Data()
        init(lock: NSLock) { self.lock = lock }
    }

    private static func runScript(_ script: String) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]

        // Ét rør for begge strømme. Med to separate rør døde kaldet fast, fordi
        // vi læste stdout til EOF mens barnet blokerede på en fyldt stderr-pipe.
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
        } catch {
            throw BridgeError.config("Kunne ikke starte osascript: \(error.localizedDescription)")
        }

        // availableData blokerer, indtil der kommer data eller EOF, så det kan
        // ikke bruges til en timeout. Derfor læses på en egen tråd, mens
        // hovedtråden venter på en semafor med tidsgrænse.
        let lock = NSLock()
        let reader = ThreadReader(lock: lock)
        let finished = DispatchSemaphore(value: 0)
        let worker = Thread {
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            lock.lock()
            reader.data = data
            lock.unlock()
            finished.signal()
        }
        worker.stackSize = 1 << 19
        worker.start()

        guard finished.wait(timeout: .now() + scriptTimeout) == .success else {
            process.terminate()
            throw BridgeError.config(
                "Notes.app svarede ikke inden \(Int(scriptTimeout)) sekunder. "
                    + "Er Notes.app åben, og står der en dialog i vente?"
            )
        }
        lock.lock()
        let collected = reader.data
        lock.unlock()
        process.waitUntilExit()

        let text = String(data: collected, encoding: .utf8) ?? ""
        if process.terminationStatus != 0 {
            let trimmed = Trim.linesAndSpaces(text)
            if trimmed.contains("-1743") || trimmed.contains("not allowed") || trimmed.contains("Not authorized") {
                throw BridgeError.config(
                    "Notes.app nægtede adgang. Giv FamilyBridge adgang under Systemindstillinger > Anonymitet og sikkerhed > Automatisering."
                )
            }
            throw BridgeError.config("Notes-script fejlede: \(trimmed)")
        }
        return Trim.linesAndSpaces(text)
    }
}
