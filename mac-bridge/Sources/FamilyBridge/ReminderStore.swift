import AppKit
import EventKit
import Foundation

/// Serial wrapper around EventKit so every access happens on one private queue.
final class ReminderStore {
    private let store = EKEventStore()
    private let queue = DispatchQueue(label: "bridge.eventkit")
    private(set) var accessGranted = false
    private(set) var accessChecked = false

    var hasAccess: Bool { accessChecked && accessGranted }

    /// Must run on the main thread: shows the system permission dialog.
    @MainActor
    func requestAccess() async throws -> Bool {
        if #available(macOS 14.0, *) {
            let granted = try await store.requestFullAccessToReminders()
            accessGranted = granted
            accessChecked = true
            if !granted {
                Log.error("Adgang til Påmindelser blev afvist – tilføj FamilyBridge i Systemindstillinger > Anonymitet og sikkerhed > Påmindelser")
            }
            return granted
        } else {
            let granted = try await store.requestAccess(to: .reminder)
            accessGranted = granted
            accessChecked = true
            return granted
        }
    }

    func authorizationDescription() -> String {
        switch EKEventStore.authorizationStatus(for: .reminder) {
        case .notDetermined: return "notDetermined"
        case .restricted: return "restricted"
        case .denied: return "denied"
        case .authorized: return "authorized"
        case .fullAccess: return "fullAccess"
        case .writeOnly: return "writeOnly"
        @unknown default: return "unknown"
        }
    }

    private func requireAccess() throws {
        guard hasAccess else {
            throw BridgeError.reminders(
                "Ingen adgang til den lokale Påmindelser-database (\(authorizationDescription()))"
            )
        }
    }

    private func run<T>(_ work: @escaping (EKEventStore) throws -> T) throws -> T {
        try queue.sync { try work(store) }
    }

    func lists() throws -> [[String: Any]] {
        try requireAccess()
        return try run { store in
            var result: [[String: Any]] = []
            for calendar in store.calendars(for: .reminder) {
                result.append([
                    "id": calendar.calendarIdentifier,
                    "name": calendar.title,
                    "color": calendar.cgColor?.toHex() ?? "",
                    "allows_modification": calendar.allowsContentModifications,
                ])
            }
            return result
        }
    }

    func todos(listId: String?, includeDone: Bool) throws -> [[String: Any]] {
        try requireAccess()
        return try run { store in
            let predicate: NSPredicate
            if let listId, !listId.isEmpty, let calendar = store.calendar(withIdentifier: listId) {
                predicate = store.predicateForReminders(in: [calendar])
            } else {
                predicate = store.predicateForReminders(in: nil)
            }
            var fetched: [EKReminder] = []
            let done = DispatchSemaphore(value: 0)
            store.fetchReminders(matching: predicate) { reminders in
                fetched = reminders ?? []
                done.signal()
            }
            done.wait()
            var result: [[String: Any]] = []
            for reminder in fetched {
                let isDone = reminder.isCompleted
                if isDone && !includeDone { continue }
                result.append(Self.payload(reminder))
            }
            result.sort { left, right in
                let leftDue = (left["due"] as? String) ?? ""
                let rightDue = (right["due"] as? String) ?? ""
                if leftDue == rightDue { return leftDue < rightDue }
                if leftDue.isEmpty { return false }
                if rightDue.isEmpty { return true }
                return leftDue < rightDue
            }
            return result
        }
    }

    func create(listId: String?, title: String, due: String?, notes: String?) throws -> [String: Any] {
        try requireAccess()
        return try run { store in
            guard let calendar = Self.target(store: store, listId: listId) else {
                throw BridgeError.reminders("Fant ikke påmindelsesliste '\(listId ?? "")'")
            }
            let reminder = EKReminder(eventStore: store)
            reminder.calendar = calendar
            reminder.title = title
            if let notes, !notes.isEmpty { reminder.notes = notes }
            if let due, let date = Dates.parseDay(due) {
                reminder.dueDateComponents = Self.dayComponents(date)
            }
            try store.save(reminder, commit: true)
            return Self.payload(reminder)
        }
    }

    func update(id: String, title: String?, done: Bool?, due: String?) throws -> [String: Any] {
        try requireAccess()
        return try run { store in
            guard let reminder = store.calendarItem(withIdentifier: id) as? EKReminder else {
                throw BridgeError.reminders("Fant ikke påmindelsen '\(id)'")
            }
            if let title { reminder.title = title }
            if let done {
                reminder.isCompleted = done
                reminder.completionDate = done ? Date() : nil
            }
            if let due {
                reminder.dueDateComponents = Dates.parseDay(due).map(Self.dayComponents)
            }
            try store.save(reminder, commit: true)
            return Self.payload(reminder)
        }
    }

    func delete(id: String) throws {
        try requireAccess()
        try run { store in
            guard let reminder = store.calendarItem(withIdentifier: id) as? EKReminder else {
                throw BridgeError.reminders("Fant ikke påmindelsen '\(id)'")
            }
            try store.remove(reminder, commit: true)
        }
    }

    private static func target(store: EKEventStore, listId: String?) -> EKCalendar? {
        if let listId, !listId.isEmpty, let calendar = store.calendar(withIdentifier: listId) {
            return calendar
        }
        return store.calendars(for: .reminder).first { $0.allowsContentModifications }
    }

    private static func dayComponents(_ date: Date) -> DateComponents {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar.dateComponents([.year, .month, .day], from: date)
    }

    static func payload(_ reminder: EKReminder) -> [String: Any] {
        let due = reminder.dueDateComponents.flatMap { DateComponents.calendar.date(from: $0) }
        var item: [String: Any] = [
            "id": reminder.calendarItemIdentifier,
            "title": reminder.title ?? "",
            "done": reminder.isCompleted,
            "due": Dates.day(due) ?? NSNull(),
            "list_id": reminder.calendar?.calendarIdentifier ?? "",
            "list_name": reminder.calendar?.title ?? "",
            "notes": reminder.notes ?? "",
            "url": reminder.url?.absoluteString ?? "",
            "created": Dates.stamp(reminder.creationDate) ?? NSNull(),
            "modified": Dates.stamp(reminder.lastModifiedDate) ?? NSNull(),
        ]
        item["alarms"] = (reminder.alarms?.isEmpty == false)
        return item
    }
}

private extension DateComponents {
    static var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        return calendar
    }
}

extension CGColor {
    func toHex() -> String {
        guard let color = NSColor(cgColor: self) else { return "" }
        let red = Int((color.redComponent * 255).rounded())
        let green = Int((color.greenComponent * 255).rounded())
        let blue = Int((color.blueComponent * 255).rounded())
        return String(format: "#%02x%02x%02x", red, green, blue)
    }
}
