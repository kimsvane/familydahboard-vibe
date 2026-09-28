import Darwin
import Foundation

struct HTTPRequest {
    var method: String
    var path: String
    var query: [String: String]
    var headers: [String: String]
    var body: Data

    func header(_ name: String) -> String? {
        headers[name.lowercased()]
    }
}

struct HTTPResponse {
    var status: Int
    var json: Any?

    var data: Data {
        guard let json, JSONSerialization.isValidJSONObject(json),
              let encoded = try? JSONSerialization.data(withJSONObject: json)
        else { return Data("{}".utf8) }
        return encoded
    }

    static func ok(_ json: Any) -> HTTPResponse { HTTPResponse(status: 200, json: json) }
    static func error(_ status: Int, _ message: String) -> HTTPResponse {
        HTTPResponse(status: status, json: ["ok": false, "error": message])
    }

    var reason: String {
        switch status {
        case 200: return "OK"
        case 400: return "Bad Request"
        case 401: return "Unauthorized"
        case 404: return "Not Found"
        case 405: return "Method Not Allowed"
        case 500: return "Internal Server Error"
        default: return "Error"
        }
    }
}

/// Small blocking-free HTTP/1.1 server: one socket per request, `Connection: close`.
final class HTTPServer {
    /// Genindlæses fra disk ved hvert request, så et token, der skrives af
    /// enable-user.sh eller postinstall, tager virkning uden genstart.
    private var config: BridgeConfig
    private let configLock = NSLock()
    private let store: ReminderStore
    private var listenFD: Int32 = -1
    private let acceptQueue = DispatchQueue(label: "bridge.http.accept")
    private let workQueue = DispatchQueue(label: "bridge.http.work", attributes: .concurrent)

    init(config: BridgeConfig, store: ReminderStore) {
        self.config = config
        self.store = store
    }

    func start() throws {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else { throw BridgeError.http("socket() mislykkedes") }
        var reuse: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = config.port.bigEndian
        address.sin_addr.s_addr = inet_addr(config.host)
        let bound = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(sock, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else {
            close(sock)
            throw BridgeError.http(
                "kunne ikke binde til \(config.host):\(config.port) (fejl \(errno))"
            )
        }
        guard listen(sock, 32) == 0 else {
            close(sock)
            throw BridgeError.http("lytning på \(config.host):\(config.port) mislykkedes")
        }
        listenFD = sock
        acceptQueue.async { [weak self] in self?.acceptLoop() }
        Log.info("Lytter på http://\(config.host):\(config.port)")
    }

    private func acceptLoop() {
        while listenFD >= 0 {
            var client = sockaddr_in()
            var length = socklen_t(MemoryLayout<sockaddr_in>.size)
            let fd = withUnsafeMutablePointer(to: &client) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    accept(listenFD, $0, &length)
                }
            }
            guard fd >= 0 else {
                if errno == EINTR { continue }
                break
            }
            workQueue.async { [weak self] in self?.serve(fd) }
        }
    }

    private func serve(_ fd: Int32) {
        defer { close(fd) }
        var buffer = Data()
        let headerEnd = Data("\r\n\r\n".utf8)
        let limit = 64 * 1024
        while buffer.range(of: headerEnd) == nil, buffer.count < limit {
            guard let chunk = readChunk(fd, 8192) else { return }
            if chunk.isEmpty { return }
            buffer.append(chunk)
        }
        guard let separator = buffer.range(of: headerEnd) else { return }
        let headerData = buffer[..<separator.lowerBound]
        guard var request = parseHead(headerData) else {
            write(fd, HTTPResponse.error(400, "Ugyldig forespørgsel"))
            return
        }
        var bodyData = Data(buffer[separator.upperBound...])
        let expected = Int(request.header("content-length") ?? "0") ?? 0
        while bodyData.count < expected {
            guard let chunk = readChunk(fd, 8192), !chunk.isEmpty else { break }
            bodyData.append(chunk)
        }
        request.body = bodyData
        let response = handle(request)
        write(fd, response)
    }

    private func readChunk(_ fd: Int32, _ size: Int) -> Data? {
        var buffer = [UInt8](repeating: 0, count: size)
        let count = read(fd, &buffer, size)
        guard count > 0 else { return count == 0 ? Data() : nil }
        return Data(buffer[0..<count])
    }

    private func parseHead(_ data: Data) -> HTTPRequest? {
        guard let text = String(data: data, encoding: .utf8) else { return nil }
        var lines = text.components(separatedBy: "\r\n")
        guard !lines.isEmpty else { return nil }
        let start = lines.removeFirst().components(separatedBy: " ")
        guard start.count >= 2 else { return nil }
        let target = start[1]
        let parts = target.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        let path = String(parts[0])
        var query: [String: String] = [:]
        if parts.count > 1 {
            for pair in parts[1].components(separatedBy: "&") {
                let kv = pair.components(separatedBy: "=")
                guard let key = kv.first, !key.isEmpty else { continue }
                let value = kv.count > 1 ? kv.dropFirst().joined(separator: "=") : ""
                query[key.removingPercentEncoding ?? key] = value.removingPercentEncoding ?? value
            }
        }
        var headers: [String: String] = [:]
        for line in lines where !line.isEmpty {
            let kv = line.components(separatedBy: ":")
            guard kv.count >= 2 else { continue }
            headers[kv[0].lowercased()] = Trim.spaces(kv.dropFirst().joined(separator: ":"))
        }
        return HTTPRequest(
            method: start[0].uppercased(),
            path: path,
            query: query,
            headers: headers,
            body: Data()
        )
    }

    private func jsonBody(_ request: HTTPRequest) -> [String: Any] {
        guard !request.body.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: request.body) as? [String: Any]
        else { return [:] }
        return object
    }

    private func currentConfig() -> BridgeConfig {
        configLock.lock()
        defer { configLock.unlock() }
        config = BridgeConfig.reloadIfChanged(config)
        return config
    }

    private func authorized(_ request: HTTPRequest) -> Bool {
        let config = currentConfig()
        if let header = request.header("authorization"),
           header.lowercased().hasPrefix("bearer "),
           Token.matches(Trim.spaces(String(header.dropFirst(7))), config.token)
        { return true }
        if let header = request.header("x-bridge-token"),
           Token.matches(header, config.token)
        { return true }
        return false
    }

    private func handle(_ request: HTTPRequest) -> HTTPResponse {
        let path = request.path.hasSuffix("/") && request.path != "/" ? String(request.path.dropLast()) : request.path
        if path == "/ping" {
            return .ok(["ok": true])
        }
        guard authorized(request) else { return .error(401, "Ugyldigt eller manglende token") }
        if path == "/health" {
            return .ok([
                "ok": true,
                "app": "family-bridge",
                "version": Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0",
                "build": Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0",
                "reminders_access": store.hasAccess,
                "authorization": store.authorizationDescription(),
                "notes_app": NotesService.available,
            ])
        }
        do {
            switch (request.method, path) {
            case ("GET", "/lists"):
                return .ok(["lists": try store.lists()])
            case ("GET", "/todos"):
                let listId = request.query["list"]
                let includeDone = request.query["done"] == "1" || request.query["done"] == "true"
                return .ok(["todos": try store.todos(listId: listId, includeDone: includeDone)])
            case ("POST", "/todos"):
                let body = jsonBody(request)
                let title = Trim.spaces(body["title"] as? String ?? "")
                guard !title.isEmpty else { return .error(400, "Titel er påkrævet") }
                return .ok([
                    "todo": try store.create(
                        listId: body["list_id"] as? String,
                        title: title,
                        due: body["due"] as? String,
                        notes: body["notes"] as? String
                    )
                ])
            case ("PATCH", "/todos"):
                let body = jsonBody(request)
                guard let id = (body["id"] as? String), !id.isEmpty else { return .error(400, "id er påkrævet") }
                var done: Bool?
                if let raw = body["done"] {
                    done = (raw as? Bool) ?? (raw as? Int).map { $0 != 0 } ?? (raw as? String == "true")
                }
                return .ok([
                    "todo": try store.update(
                        id: id,
                        title: body["title"] as? String,
                        done: done,
                        due: body["due"] as? String
                    )
                ])
            case ("DELETE", "/todos"):
                let id = jsonBody(request)["id"] as? String ?? ""
                guard !id.isEmpty else { return .error(400, "id er påkrævet") }
                try store.delete(id: id)
                return .ok(["deleted": true])
            case ("GET", "/notes"):
                let match = request.query["title"] ?? currentConfig().notesTitle
                let limit = Int(request.query["limit"] ?? "200") ?? 200
                return .ok(["notes": try NotesService.notes(titleMatch: match.isEmpty ? nil : match, limit: limit)])
            case ("POST", "/notes"):
                let body = jsonBody(request)
                let title = Trim.spaces(body["title"] as? String ?? "")
                guard !title.isEmpty else { return .error(400, "Titel er påkrævet") }
                return .ok(["note": try NotesService.save(title: title, content: body["content"] as? String ?? "")])
            default:
                return .error(404, "Ukendt rute \(request.method) \(request.path)")
            }
        } catch let error as BridgeError {
            let status = 500
            Log.error("\(request.method) \(request.path) -> \(error.message)")
            return .error(status, error.message)
        } catch {
            Log.error("\(request.method) \(request.path) -> \(error.localizedDescription)")
            return .error(500, error.localizedDescription)
        }
    }

    private func write(_ fd: Int32, _ response: HTTPResponse) {
        let body = response.data
        let header = "HTTP/1.1 \(response.status) \(response.reason)\r\n"
            + "Content-Type: application/json; charset=utf-8\r\n"
            + "Content-Length: \(body.count)\r\n"
            + "Cache-Control: no-store\r\n"
            + "Connection: close\r\n\r\n"
        var payload = Data(header.utf8)
        payload.append(body)
        payload.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count {
                let written = Darwin.write(fd, base.advanced(by: offset), raw.count - offset)
                if written <= 0 { break }
                offset += written
            }
        }
    }
}
