// StreamGrab macOS shell: starts the bundled Python server on a free local
// port and shows its UI in a native WebKit window.
import AppKit
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var baseURL: URL?
    var logHandle: FileHandle?
    var outputBuffer = ""
    var quitting = false

    let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/StreamGrab.log")

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        startServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let active = activeJobCount()
        if active > 0 && !quitting {
            let alert = NSAlert()
            alert.messageText = "\(active) download\(active == 1 ? " is" : "s are") still running"
            alert.informativeText = "Downloads will be cancelled. Live recordings are stopped and saved."
            alert.addButton(withTitle: "Keep Downloading")
            alert.addButton(withTitle: "Quit Anyway")
            if alert.runModal() == .alertFirstButtonReturn { return .terminateCancel }
        }
        return .terminateNow
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        if let s = server, s.isRunning {
            s.terminate()  // SIGTERM: server saves live recordings, cancels other jobs, then exits
            let deadline = Date().addingTimeInterval(15)
            while s.isRunning && Date() < deadline { usleep(50_000) }
            if s.isRunning { kill(s.processIdentifier, SIGKILL) }
        }
    }

    // MARK: window

    func buildWindow() {
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "native")
        // Tell the page it is running inside the app (enables native folder picker).
        config.userContentController.addUserScript(WKUserScript(
            source: "window.STREAMGRAB_NATIVE = true;",
            injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground")  // no white flash before load

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "StreamGrab"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0x0e/255, green: 0x0e/255, blue: 0x10/255, alpha: 1)
        window.appearance = NSAppearance(named: .darkAqua)
        window.minSize = NSSize(width: 760, height: 520)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("StreamGrabMain")
        window.makeKeyAndOrderFront(nil)
        showStatus("Starting…")
    }

    func showStatus(_ text: String, isError: Bool = false) {
        let esc = text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("""
        <html><body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
        background:#0e0e10;color:\(isError ? "#ff8080" : "#9a9aa6");font:14px -apple-system;text-align:center">
        <div style="max-width:620px;white-space:pre-wrap">\(esc)</div></body></html>
        """, baseURL: nil)
    }

    // MARK: server

    func findPython() -> String? {
        // Prefer the Python bundled inside the app (shareable build), then Homebrew / system.
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("python/bin/python3").path
        let candidates = [bundled, "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].compactMap { $0 }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    func startServer() {
        guard let res = Bundle.main.resourceURL?.appendingPathComponent("app"),
              FileManager.default.fileExists(atPath: res.appendingPathComponent("server.py").path) else {
            return fail("App resources are missing. Rebuild the app with macos/build.sh.")
        }
        guard let python = findPython() else {
            return fail("Python 3 was not found.\n\nInstall it with:  brew install python (or use the StreamGrab.dmg build, which includes it)")
        }
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        logHandle = try? FileHandle(forWritingTo: logURL)

        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-u", res.appendingPathComponent("server.py").path, "--port", "0", "--no-browser"]
        p.currentDirectoryURL = res
        // Apps launched from Finder get a minimal PATH. Bundled yt-dlp / ffmpeg come first,
        // then Homebrew (for builds made without the bundled tools).
        var env = ProcessInfo.processInfo.environment
        let bundledBin = Bundle.main.resourceURL?.appendingPathComponent("bin").path ?? ""
        env["PATH"] = bundledBin + ":/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p.environment = env

        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let data = h.availableData
            guard !data.isEmpty, let self else { return }
            self.logHandle?.write(data)
            let text = String(decoding: data, as: UTF8.self)
            DispatchQueue.main.async { self.handleServerOutput(text) }
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                let tail = self.outputBuffer.split(separator: "\n").suffix(8).joined(separator: "\n")
                self.fail("The StreamGrab engine stopped (exit \(proc.terminationStatus)).\n\n\(tail)\n\nLog: \(self.logURL.path)")
            }
        }
        do {
            try p.run()
            server = p
        } catch {
            fail("Could not start Python: \(error.localizedDescription)")
        }
    }

    func handleServerOutput(_ text: String) {
        outputBuffer += text
        if outputBuffer.count > 20_000 { outputBuffer = String(outputBuffer.suffix(10_000)) }
        guard baseURL == nil,
              let range = outputBuffer.range(of: #"http://127\.0\.0\.1:\d+/"#, options: .regularExpression),
              let url = URL(string: String(outputBuffer[range])) else { return }
        baseURL = url
        webView.load(URLRequest(url: url))
    }

    func fail(_ message: String) {
        showStatus(message, isError: true)
    }

    /// Synchronously asks the server how many jobs are queued/running (used on quit).
    func activeJobCount() -> Int {
        guard let base = baseURL, server?.isRunning == true else { return 0 }
        var count = 0
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: base.appendingPathComponent("api/jobs"))
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data, let jobs = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] {
                count = jobs.filter { ["queued", "running"].contains($0["status"] as? String ?? "") }.count
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2.5)
        return count
    }

    // MARK: web view

    // Keep the app on the local UI; open anything else (e.g. links) in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.allow) }
        let local = url.host == "127.0.0.1" || url.scheme == "about" || url.scheme == "data"
        if local || action.targetFrame?.isMainFrame == false && url.host == nil {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if let base = baseURL { webView.load(URLRequest(url: base)) }
    }

    // JS -> native bridge: window.webkit.messageHandlers.native.postMessage({type: "pickFolder", current})
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        if type == "pickFolder" {
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.prompt = "Use Folder"
            panel.message = "Choose where StreamGrab saves downloads"
            if let current = body["current"] as? String, !current.isEmpty {
                panel.directoryURL = URL(fileURLWithPath: (current as NSString).expandingTildeInPath)
            }
            panel.beginSheetModal(for: window) { [weak self] resp in
                guard resp == .OK else {  // tell the page the dialog closed, so its button re-enables
                    self?.webView.evaluateJavaScript("window.nativeFolderPicked && window.nativeFolderPicked('')")
                    return
                }
                guard let path = panel.url?.path,
                      let json = try? JSONSerialization.data(withJSONObject: [path]),
                      let arg = String(data: json, encoding: .utf8) else { return }
                self?.webView.evaluateJavaScript("window.nativeFolderPicked && window.nativeFolderPicked(\(arg)[0])")
            }
        }
        if type == "pickFiles" {
            let panel = NSOpenPanel()
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.allowsMultipleSelection = true
            panel.prompt = "Add"
            panel.message = "Choose audio files to convert"
            panel.beginSheetModal(for: window) { [weak self] resp in
                guard resp == .OK,
                      let json = try? JSONSerialization.data(withJSONObject: panel.urls.map(\.path)),
                      let arg = String(data: json, encoding: .utf8) else { return }
                self?.webView.evaluateJavaScript("window.nativeFilesPicked && window.nativeFilesPicked(\(arg))")
            }
        }
    }

    // MARK: menu

    @objc func reload() {
        if let base = baseURL { webView.load(URLRequest(url: base)) }
    }

    @objc func openDownloads() {
        webView.evaluateJavaScript("fetch('/api/reveal',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})")
    }

    @objc func openLog() { NSWorkspace.shared.open(logURL) }

    func buildMenu() {
        let main = NSMenu()
        func add(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach(m.addItem); item.submenu = m; main.addItem(item)
        }
        func mi(_ t: String, _ a: Selector?, _ k: String = "", _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = mods; return i
        }
        add("StreamGrab", [
            mi("About StreamGrab", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            mi("Hide StreamGrab", #selector(NSApplication.hide(_:)), "h"),
            mi("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            mi("Show All", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            mi("Quit StreamGrab", #selector(NSApplication.terminate(_:)), "q"),
        ])
        add("File", [
            mi("Open Downloads Folder", #selector(openDownloads), "o", [.command, .shift]),
            mi("Show Log", #selector(openLog)),
            .separator(),
            mi("Close Window", #selector(NSWindow.performClose(_:)), "w"),
        ])
        // Needed so ⌘C / ⌘V / ⌘A work inside the web view.
        add("Edit", [
            mi("Undo", Selector(("undo:")), "z"),
            mi("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            mi("Cut", #selector(NSText.cut(_:)), "x"),
            mi("Copy", #selector(NSText.copy(_:)), "c"),
            mi("Paste", #selector(NSText.paste(_:)), "v"),
            mi("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        add("View", [
            mi("Reload", #selector(reload), "r"),
            mi("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
        ])
        add("Window", [
            mi("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"),
            mi("Zoom", #selector(NSWindow.performZoom(_:))),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
