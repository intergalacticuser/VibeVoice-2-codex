import AppKit
import Foundation

struct BridgeQueueItem: Decodable {
    let channel: String
    let preview: String
}

struct BridgeSettings: Decodable {
    let backend: String
    let voice: String
    let speed: Double
    let interrupt_policy: String
    let desktop_speech_mode: String
    let speak_assistant_deltas: Bool
    let speak_assistant_completed: Bool
    let speak_reasoning_summary: Bool
    let speak_status_announcements: Bool
    let muted: Bool
    let read_through: Bool
}

struct BridgeSnapshot: Decodable {
    let settings: BridgeSettings
    let current: BridgeQueueItem?
    let queue: [BridgeQueueItem]
    let backend_ready: Bool
    let backend_error: String?
    let codex_running: Bool
}

final class BridgeAPI {
    let controlURL: URL

    init(controlURL: URL) {
        self.controlURL = controlURL
    }

    func fetchState(completion: @escaping (BridgeSnapshot?) -> Void) {
        let url = controlURL.appendingPathComponent("state")
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data else {
                completion(nil)
                return
            }
            let decoder = JSONDecoder()
            completion(try? decoder.decode(BridgeSnapshot.self, from: data))
        }.resume()
    }

    func post(path: String, payload: [String: Any], completion: (() -> Void)? = nil) {
        let url = controlURL.appendingPathComponent(path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload, options: [])
        URLSession.shared.dataTask(with: request) { _, _, _ in
            completion?()
        }.resume()
    }
}

final class CodexVoiceMenuBarApp: NSObject, NSApplicationDelegate {
    private let interruptPolicies = ["finish_current", "interrupt_latest", "manual"]
    private let desktopModes = ["live_fast", "english_full", "status_only"]
    private let speedOptions: [Double] = [0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5]

    private var api: BridgeAPI!
    private var statusItem: NSStatusItem!
    private var pollTimer: Timer?
    private var snapshot: BridgeSnapshot?
    private var isClosingBridge = false
    private var offlinePolls = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let controlURL = Self.parseControlURL() else {
            NSApp.terminate(nil)
            return
        }

        api = BridgeAPI(controlURL: controlURL)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "VV"
        statusItem.button?.toolTip = "Codex Voice Bridge"

        refreshState()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in
            self?.refreshState()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
    }

    private static func parseControlURL() -> URL? {
        let args = CommandLine.arguments
        for (index, arg) in args.enumerated() {
            if arg == "--control-url", index + 1 < args.count {
                return URL(string: args[index + 1])
            }
            if arg.hasPrefix("--control-url=") {
                return URL(string: String(arg.dropFirst("--control-url=".count)))
            }
        }
        return nil
    }

    private func refreshState() {
        api.fetchState { [weak self] snapshot in
            guard let self else { return }
            DispatchQueue.main.async {
                self.snapshot = snapshot
                if snapshot == nil {
                    self.offlinePolls += 1
                    if self.offlinePolls >= 5 {
                        NSApp.terminate(nil)
                        return
                    }
                } else {
                    self.offlinePolls = 0
                }
                self.rebuildMenu()
            }
        }
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        let snapshot = snapshot
        let settings = snapshot?.settings

        updateStatusButton(snapshot: snapshot)

        if let settings {
            menu.addItem(disabledItem("Backend: \(settings.backend)"))
            menu.addItem(disabledItem(String(format: "Speed: %.1fx", settings.speed)))
            let voiceText = settings.voice.isEmpty ? "default" : settings.voice
            menu.addItem(disabledItem("Voice: \(voiceText)"))
            menu.addItem(disabledItem("Desktop: \(settings.desktop_speech_mode)"))
            menu.addItem(disabledItem("Interrupt: \(settings.interrupt_policy)"))
        } else {
            menu.addItem(disabledItem("Bridge offline"))
        }

        if let current = snapshot?.current {
            menu.addItem(disabledItem("Current: [\(current.channel)] \(current.preview)"))
        } else {
            menu.addItem(disabledItem("Current: idle"))
        }

        if let queue = snapshot?.queue, !queue.isEmpty {
            menu.addItem(disabledItem("Queue: \(queue.count)"))
            for item in queue.prefix(5) {
                menu.addItem(disabledItem("[\(item.channel)] \(item.preview)"))
            }
        } else {
            menu.addItem(disabledItem("Queue: 0"))
        }

        if let backendError = snapshot?.backend_error, !backendError.isEmpty {
            menu.addItem(disabledItem("Error: \(backendError)"))
        }

        menu.addItem(.separator())
        menu.addItem(submenuItem(title: "Channels", menu: buildChannelsMenu(settings: settings)))
        menu.addItem(submenuItem(title: "Desktop Mode", menu: buildDesktopModeMenu(settings: settings)))
        menu.addItem(submenuItem(title: "Interrupt", menu: buildInterruptMenu(settings: settings)))
        menu.addItem(submenuItem(title: "Speed", menu: buildSpeedMenu(settings: settings)))

        let setVoiceItem = NSMenuItem(title: "Set Voice…", action: #selector(promptForVoice(_:)), keyEquivalent: "")
        setVoiceItem.target = self
        menu.addItem(setVoiceItem)

        if let settings {
            menu.addItem(toggleItem(title: "Mute", enabled: settings.muted, payload: ["muted": !settings.muted]))
            menu.addItem(toggleItem(title: "Read Through", enabled: settings.read_through, payload: ["read_through": !settings.read_through]))
        }

        menu.addItem(.separator())
        menu.addItem(actionItem(title: "Stop Current", action: #selector(postBridgeAction(_:)), represented: "actions/stop"))
        menu.addItem(actionItem(title: "Stop All Speech", action: #selector(postBridgeAction(_:)), represented: "actions/stop_all"))
        menu.addItem(actionItem(title: "Next Item", action: #selector(postBridgeAction(_:)), represented: "actions/next"))
        menu.addItem(actionItem(title: "Clear Queue", action: #selector(postBridgeAction(_:)), represented: "actions/clear"))
        menu.addItem(.separator())
        menu.addItem(actionItem(title: "Quit Watcher and Stop All Processes", action: #selector(closeBridge(_:)), represented: nil))
        menu.addItem(actionItem(title: "Quit Menu Only", action: #selector(quitMenuOnly(_:)), represented: nil))

        statusItem.menu = menu
    }

    private func updateStatusButton(snapshot: BridgeSnapshot?) {
        let active = (snapshot?.current != nil) || !(snapshot?.queue.isEmpty ?? true)
        statusItem.button?.title = active ? "VV●" : "VV"
        statusItem.button?.toolTip = active ? "Codex Voice Bridge active" : "Codex Voice Bridge idle"
    }

    private func buildChannelsMenu(settings: BridgeSettings?) -> NSMenu {
        let menu = NSMenu()
        guard let settings else { return menu }
        menu.addItem(toggleItem(title: "Assistant Deltas", enabled: settings.speak_assistant_deltas, payload: ["speak_assistant_deltas": !settings.speak_assistant_deltas]))
        menu.addItem(toggleItem(title: "Completed / Final", enabled: settings.speak_assistant_completed, payload: ["speak_assistant_completed": !settings.speak_assistant_completed]))
        menu.addItem(toggleItem(title: "Reasoning Summary", enabled: settings.speak_reasoning_summary, payload: ["speak_reasoning_summary": !settings.speak_reasoning_summary]))
        menu.addItem(toggleItem(title: "Status", enabled: settings.speak_status_announcements, payload: ["speak_status_announcements": !settings.speak_status_announcements]))
        return menu
    }

    private func buildDesktopModeMenu(settings: BridgeSettings?) -> NSMenu {
        let menu = NSMenu()
        let selected = settings?.desktop_speech_mode
        for mode in desktopModes {
            let item = actionItem(title: mode, action: #selector(postSettings(_:)), represented: ["desktop_speech_mode": mode])
            item.state = selected == mode ? .on : .off
            menu.addItem(item)
        }
        return menu
    }

    private func buildInterruptMenu(settings: BridgeSettings?) -> NSMenu {
        let menu = NSMenu()
        let selected = settings?.interrupt_policy
        for policy in interruptPolicies {
            let item = actionItem(title: policy, action: #selector(postSettings(_:)), represented: ["interrupt_policy": policy])
            item.state = selected == policy ? .on : .off
            menu.addItem(item)
        }
        return menu
    }

    private func buildSpeedMenu(settings: BridgeSettings?) -> NSMenu {
        let menu = NSMenu()
        let selected = settings?.speed ?? 1.0
        for speed in speedOptions {
            let title = String(format: "%.1fx", speed)
            let item = actionItem(title: title, action: #selector(postSettings(_:)), represented: ["speed": speed])
            item.state = abs(selected - speed) < 0.01 ? .on : .off
            menu.addItem(item)
        }
        return menu
    }

    private func disabledItem(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }

    private func submenuItem(title: String, menu: NSMenu) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.submenu = menu
        return item
    }

    private func actionItem(title: String, action: Selector, represented: Any?) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.representedObject = represented
        return item
    }

    private func toggleItem(title: String, enabled: Bool, payload: [String: Any]) -> NSMenuItem {
        let item = actionItem(title: title, action: #selector(postSettings(_:)), represented: payload)
        item.state = enabled ? .on : .off
        return item
    }

    @objc private func postSettings(_ sender: NSMenuItem) {
        guard let payload = sender.representedObject as? [String: Any] else { return }
        api.post(path: "settings", payload: payload) { [weak self] in
            self?.refreshState()
        }
    }

    @objc private func postBridgeAction(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        api.post(path: path, payload: [:]) { [weak self] in
            self?.refreshState()
        }
    }

    @objc private func promptForVoice(_ sender: NSMenuItem) {
        let alert = NSAlert()
        alert.messageText = "Set Voice"
        alert.informativeText = "Enter the voice preset to use for new speech items."
        alert.addButton(withTitle: "Apply")
        alert.addButton(withTitle: "Cancel")

        let textField = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        textField.stringValue = snapshot?.settings.voice ?? ""
        alert.accessoryView = textField

        let response = alert.runModal()
        guard response == .alertFirstButtonReturn else { return }
        api.post(path: "settings", payload: ["voice": textField.stringValue]) { [weak self] in
            self?.refreshState()
        }
    }

    @objc private func closeBridge(_ sender: NSMenuItem) {
        guard !isClosingBridge else { return }
        isClosingBridge = true
        api.post(path: "actions/shutdown", payload: [:]) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                NSApp.terminate(nil)
            }
        }
    }

    @objc private func quitMenuOnly(_ sender: NSMenuItem) {
        NSApp.terminate(nil)
    }
}

@main
struct CodexVoiceMenuBarMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = CodexVoiceMenuBarApp()
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        withExtendedLifetime(delegate) {
            app.run()
        }
    }
}
