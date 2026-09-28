import AppKit
import Foundation

private let helpText = """
FamilyBridge – giver Family Dashboard adgang til den ægte lokale Påmindelser-database
(EventKit) og eventuelt iCloud Notes på denne Mac.

Brug:
  family-bridge            Starter appen (menubarikon + lokal HTTP-tjeneste)
  family-bridge --token    Udskriver API-tokenet (kopier det til dashboardet)
  family-bridge --status   Tjekker tjenesten: curl -s -H "X-Bridge-Token: <token>" \\
                              http://127.0.0.1:8787/health
  family-bridge --help     Viser denne hjælp

Vigtigt: Mac'en skal være logget ind, og FamilyBridge skal have adgang under
Systemindstillinger > Anonymitet og sikkerhed > Påmindelser.
"""

private func printHelp() {
    print(helpText)
}

let arguments = Array(CommandLine.arguments.dropFirst())

if arguments.contains("--help") || arguments.contains("-h") {
    printHelp()
    exit(0)
}

if arguments.contains("--version") {
    let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
    print(version)
    exit(0)
}

let config: BridgeConfig
do {
    config = try BridgeConfig.load()
} catch {
    FileHandle.standardError.write(Data("Kunne ikke læse config: \(error.localizedDescription)\n".utf8))
    exit(1)
}

if arguments.contains("--token") {
    print(config.token)
    exit(0)
}

if arguments.contains("--status") {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/curl")
    process.arguments = [
        "-s", "-H", "X-Bridge-Token: \(config.token)",
        "http://\(config.host):\(config.port)/health",
    ]
    let pipe = Pipe()
    process.standardOutput = pipe
    try? process.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    print(String(data: data, encoding: .utf8) ?? "ingen svar")
    exit(process.terminationStatus == 0 ? 0 : 1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

let store = ReminderStore()
let server = HTTPServer(config: config, store: store)
Log.info("FamilyBridge starter, token gemt i \(BridgeConfig.fileURL.path)")

var statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
let statusButton = statusItem.button!
statusButton.title = "◐"
statusButton.toolTip = "FamilyBridge"

private func refreshUI() {
    if store.hasAccess {
        statusButton.title = "✓"
        statusButton.toolTip = "FamilyBridge – Påmindelser: adgang OK"
    } else {
        statusButton.title = "!"
        statusButton.toolTip = "FamilyBridge – mangler adgang til Påmindelser"
    }
}

private func buildMenu() -> NSMenu {
    let menu = NSMenu()
    let status = NSMenuItem(title: "Status: \(store.hasAccess ? "adgang OK" : "mangler adgang")", action: nil, keyEquivalent: "")
    status.isEnabled = false
    menu.addItem(status)
    let target = NSMenuItem(title: "tjekker …", action: nil, keyEquivalent: "")
    target.isEnabled = false
    menu.addItem(target)
    menu.addItem(.separator())

    let copy = NSMenuItem(title: "Vis / kopiér API-token", action: #selector(Coordinator.showToken), keyEquivalent: "")
    copy.target = Coordinator.shared
    menu.addItem(copy)

    let reopen = NSMenuItem(title: "Bed om adgang igen", action: #selector(Coordinator.requestAccess), keyEquivalent: "")
    reopen.target = Coordinator.shared
    menu.addItem(reopen)

    let logItem = NSMenuItem(title: "Åbn logfil", action: #selector(Coordinator.openLog), keyEquivalent: "")
    logItem.target = Coordinator.shared
    menu.addItem(logItem)

    let help = NSMenuItem(title: "Hjælp", action: #selector(Coordinator.showHelp), keyEquivalent: "")
    help.target = Coordinator.shared
    menu.addItem(help)

    menu.addItem(.separator())
    let quit = NSMenuItem(title: "Afslut FamilyBridge", action: #selector(Coordinator.quit), keyEquivalent: "q")
    quit.target = Coordinator.shared
    menu.addItem(quit)
    return menu
}

final class Coordinator: NSObject {
    static let shared = Coordinator()
    var menu: NSMenu?
}

let coordinator = Coordinator.shared

statusButton.target = coordinator
statusButton.action = #selector(Coordinator.showMenu)
statusButton.sendAction(on: [NSEvent.EventTypeMask.leftMouseUp])

func showHelpDialog() {
    let alert = NSAlert()
    alert.messageText = "FamilyBridge"
    alert.informativeText = helpText
    alert.alertStyle = .informational
    alert.addButton(withTitle: "OK")
    alert.runModal()
}

func requestPermission() {
    Task { @MainActor in
        _ = try? await store.requestAccess()
        refreshUI()
        coordinator.menu = buildMenu()
    }
}

extension Coordinator {
    @objc func showMenu() {
        refreshUI()
        let menu = buildMenu()
        statusItem.menu = menu
    }

    @objc func showToken() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(config.token, forType: .string)
        let alert = NSAlert()
        alert.messageText = "API-token"
        alert.informativeText = "Tokenet er kopieret til udklipsholderen. Indsæt det i Family Dashboard under Indstillinger > Påmindelser > Mac mini.\n\n\(config.token)"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    @objc func requestAccess() {
        requestPermission()
    }

    @objc func openLog() {
        let url = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/FamilyBridge.log")
        if !FileManager.default.fileExists(atPath: url.path) {
            showHelpDialog()
            return
        }
        NSWorkspace.shared.open(url)
    }

    @objc func showHelp() {
        showHelpDialog()
    }

    @objc func quit() {
        NSApp.terminate(nil)
    }
}

do {
    try server.start()
} catch {
    Log.error("Serveren kunne ikke starte: \(error)")
    let alert = NSAlert()
    alert.messageText = "FamilyBridge kunne ikke starte"
    alert.informativeText = String(describing: error)
    alert.alertStyle = .critical
    alert.addButton(withTitle: "OK")
    alert.runModal()
    exit(1)
}

refreshUI()
requestPermission()
app.run()
