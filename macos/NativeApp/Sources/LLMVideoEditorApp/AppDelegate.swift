import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler, WKNavigationDelegate, CompositionEngineDelegate {
    private let root: URL
    private let baseURL = URL(string: "http://127.0.0.1:8765")!
    private let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: 1600, height: 1000),
        styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
        backing: .buffered,
        defer: false
    )
    private let webView: WKWebView
    private let playerView = NativePlayerView(frame: .zero)
    private let engine: CompositionEngine
    private let server: ServerController
    private var webReady = false
    private let smokeMode = CommandLine.arguments.contains("--smoke-play")
    private let smokeStart = AppDelegate.argumentDouble("--smoke-start", default: 6)
    private let smokeDuration = AppDelegate.argumentDouble("--smoke-duration", default: 9)
    private var smokeStarted = false
    private var smokeStartWorkItem: DispatchWorkItem?
    private var lastTimelineSignature: Data?
    private var loggedMonitorFrame = false

    override init() {
        root = AppDelegate.resolveRoot()
        let userContent = WKUserContentController()
        let configuration = WKWebViewConfiguration()
        configuration.userContentController = userContent
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        engine = CompositionEngine(root: root)
        server = ServerController(root: root, baseURL: baseURL)
        super.init()
        userContent.add(self, name: "nativeEditor")
        webView.navigationDelegate = self
        engine.delegate = self
        playerView.player = engine.player
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        writeLog("Native app launched; root=\(root.path)")
        NSApp.setActivationPolicy(.regular)
        configureMenus()
        configureWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        server.ensureRunning { [weak self] result in
            switch result {
            case .success:
                self?.loadEditor()
            case .failure(let error):
                self?.showFatalError(error.localizedDescription)
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    private func configureWindow() {
        window.title = "LLM Video Editor"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.minSize = NSSize(width: 1100, height: 720)
        window.center()
        window.isReleasedWhenClosed = false
        guard let content = window.contentView else { return }
        webView.frame = content.bounds
        webView.autoresizingMask = [.width, .height]
        webView.allowsMagnification = false
        webView.allowsBackForwardNavigationGestures = false
        content.addSubview(webView)

        playerView.isHidden = true
        content.addSubview(playerView, positioned: .above, relativeTo: webView)
    }

    private func configureMenus() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About LLM Video Editor", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit LLM Video Editor", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        menu.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        menu.addItem(editItem)
        NSApp.mainMenu = menu
    }

    private func loadEditor() {
        var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "native", value: "1"),
            URLQueryItem(name: "build", value: "native-monitor-v15"),
        ]
        webView.load(URLRequest(url: components.url!))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webReady = true
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "nativeEditor", let body = message.body as? [String: Any] else { return }
        switch body["type"] as? String {
        case "monitor_rect":
            positionMonitor(body)
        case "timeline":
            receiveTimeline(body)
        case "transport":
            receiveTransport(body)
        default:
            break
        }
    }

    private func positionMonitor(_ body: [String: Any]) {
        guard let x = (body["x"] as? NSNumber)?.doubleValue,
              let y = (body["y"] as? NSNumber)?.doubleValue,
              let width = (body["width"] as? NSNumber)?.doubleValue,
              let height = (body["height"] as? NSNumber)?.doubleValue,
              width > 1, height > 1 else { return }
        let frame = webView.frame
        playerView.frame = NSRect(
            x: frame.minX + x,
            y: frame.minY + frame.height - y - height,
            width: width,
            height: height
        ).integral
        playerView.isHidden = false
        if !loggedMonitorFrame {
            loggedMonitorFrame = true
            writeLog("Native monitor frame=\(playerView.frame.debugDescription)")
        }
    }

    private func receiveTimeline(_ body: [String: Any]) {
        guard let projectID = body["project_id"] as? String,
              let state = body["state"] as? [String: Any],
              let snapshot = TimelineSnapshot(projectID: projectID, dictionary: state) else { return }
        writeLog("Timeline received project=\(projectID) segments=\(snapshot.segments.count) duration=\(snapshot.duration)")
        playerView.configure(snapshot)
        let time = (body["time"] as? NSNumber)?.doubleValue ?? engine.player.currentTime().seconds
        let signatureObject: [String: Any] = [
            "project_id": projectID,
            "resolution": state["resolution"] ?? [],
            "fps": state["fps"] ?? 30,
            "tracks": state["tracks"] ?? [],
            "segments": state["segments"] ?? [],
        ]
        let signature = try? JSONSerialization.data(withJSONObject: signatureObject, options: [.sortedKeys])
        if signature == lastTimelineSignature {
            writeLog("Timeline unchanged; native rebuild skipped")
            return
        }
        lastTimelineSignature = signature
        engine.update(snapshot, preferredTime: time)
    }

    private func receiveTransport(_ body: [String: Any]) {
        let command = body["command"] as? String
        let time = (body["time"] as? NSNumber)?.doubleValue ?? engine.player.currentTime().seconds
        switch command {
        case "toggle":
            engine.toggle(at: time)
        case "seek":
            engine.seek(to: time, play: (body["play"] as? NSNumber)?.boolValue ?? false)
        default:
            break
        }
    }

    func compositionEngine(_ engine: CompositionEngine, didUpdateTime time: Double, playing: Bool) {
        playerView.updateOverlays(at: time)
        guard webReady else { return }
        webView.evaluateJavaScript("window.llmNativePlaybackUpdate?.(\(time), \(playing ? "true" : "false"));")
    }

    func compositionEngine(_ engine: CompositionEngine, didChangeStatus message: String, isError: Bool) {
        writeLog("\(isError ? "ERROR" : "STATUS") \(message)")
        guard webReady,
              let data = try? JSONSerialization.data(withJSONObject: message, options: [.fragmentsAllowed]),
              let encoded = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.llmNativeStatusUpdate?.(\(encoded), \(isError ? "true" : "false"));")
        if smokeMode, !smokeStarted, !isError, message == "Native preview ready" {
            smokeStartWorkItem?.cancel()
            let work = DispatchWorkItem { [weak self, weak engine] in
                guard let self, let engine, !self.smokeStarted else { return }
                self.smokeStarted = true
                engine.player.isMuted = true
                let frameURL = URL(fileURLWithPath: "/tmp/llm-video-editor-native-frame.png")
                do {
                    try engine.captureDiagnosticFrame(at: 1, to: frameURL)
                    self.writeLog("SMOKE frame=\(frameURL.path)")
                } catch {
                    self.writeLog("SMOKE frame capture failed: \(error.localizedDescription)")
                }
                engine.seek(to: self.smokeStart, play: true)
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                    guard let self else { return }
                    self.writeLog("SMOKE layerReady=\(self.playerView.playerLayer.isReadyForDisplay) hidden=\(self.playerView.isHidden) frame=\(self.playerView.frame.debugDescription)")
                }
                DispatchQueue.main.asyncAfter(deadline: .now() + self.smokeDuration) { [weak self, weak engine] in
                    guard let self, let engine else { return }
                    let reached = engine.player.currentTime().seconds
                    let expected = min(max(0, engine.duration - 0.1), self.smokeStart + self.smokeDuration - 0.5)
                    let passed = reached >= expected
                    let itemStatus = engine.player.currentItem?.status.rawValue ?? -1
                    let error = engine.player.currentItem?.error?.localizedDescription ?? "none"
                    self.writeLog("SMOKE \(passed ? "PASS" : "FAIL") reached=\(reached) expected>=\(expected) itemStatus=\(itemStatus) error=\(error)")
                    NSApp.terminate(nil)
                }
            }
            smokeStartWorkItem = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.75, execute: work)
        }
    }

    private func showFatalError(_ message: String) {
        writeLog("FATAL \(message)")
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "LLM Video Editor could not start"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    private func writeLog(_ message: String) {
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/LLM Video Editor", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent("native.log")
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let data = Data("[\(timestamp)] \(message)\n".utf8)
        if !FileManager.default.fileExists(atPath: url.path) {
            try? data.write(to: url)
            return
        }
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        try? handle.seekToEnd()
        try? handle.write(contentsOf: data)
        try? handle.close()
    }

    private static func resolveRoot() -> URL {
        let arguments = CommandLine.arguments
        if let index = arguments.firstIndex(of: "--root"), arguments.indices.contains(index + 1) {
            return URL(fileURLWithPath: arguments[index + 1], isDirectory: true).standardizedFileURL
        }
        if let value = ProcessInfo.processInfo.environment["LLM_VIDEO_EDITOR_ROOT"], !value.isEmpty {
            return URL(fileURLWithPath: value, isDirectory: true).standardizedFileURL
        }
        if let value = Bundle.main.object(forInfoDictionaryKey: "LLMEditorRoot") as? String,
           !value.isEmpty, value != "__EDITOR_ROOT__" {
            return URL(fileURLWithPath: value, isDirectory: true).standardizedFileURL
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath, isDirectory: true).standardizedFileURL
    }

    private static func argumentDouble(_ name: String, default fallback: Double) -> Double {
        let arguments = CommandLine.arguments
        guard let index = arguments.firstIndex(of: name), arguments.indices.contains(index + 1) else { return fallback }
        return Double(arguments[index + 1]) ?? fallback
    }
}
