import AppKit
import Foundation
import WebKit

private enum SeldLaunchError: LocalizedError {
    case invalidConfiguration(String)
    case invalidReceipt
    case serviceFailed
    case serviceTimedOut
    case unavailable

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration(let field):
            return "This Seld app is missing \(field). Reinstall it; your local record is unchanged."
        case .invalidReceipt:
            return "Seld could not verify this local session. Reopen the app or reinstall it."
        case .serviceFailed:
            return "Seld could not start its local Bridge. Your local record is unchanged."
        case .serviceTimedOut:
            return "The local Bridge is taking longer than expected. Try again in a moment."
        case .unavailable:
            return "Seld could not reach its verified local Bridge. Your record is unchanged."
        }
    }
}

private struct SeldConfiguration {
    let bridgeStatePath: String
    let serviceExecutable: URL
    let vaultID: String
    let vaultRoot: String
    let vaultRootDevice: UInt64
    let vaultRootInode: UInt64

    init(bundle: Bundle = .main) throws {
        bridgeStatePath = try Self.requiredString("SeldBridgeStatePath", bundle: bundle)
        vaultID = try Self.requiredString("SeldVaultID", bundle: bundle)
        vaultRoot = try Self.requiredString("SeldVaultRoot", bundle: bundle)
        vaultRootDevice = try Self.requiredUInt64("SeldVaultRootDevice", bundle: bundle)
        vaultRootInode = try Self.requiredUInt64("SeldVaultRootInode", bundle: bundle)
        let serviceName = try Self.requiredString("SeldServiceExecutable", bundle: bundle)
        guard let executableDirectory = bundle.executableURL?.deletingLastPathComponent() else {
            throw SeldLaunchError.invalidConfiguration("its executable location")
        }
        serviceExecutable = executableDirectory.appendingPathComponent(serviceName)
    }

    private static func requiredString(_ key: String, bundle: Bundle) throws -> String {
        guard let value = bundle.object(forInfoDictionaryKey: key) as? String,
              !value.isEmpty else {
            throw SeldLaunchError.invalidConfiguration(key)
        }
        return value
    }

    private static func requiredUInt64(_ key: String, bundle: Bundle) throws -> UInt64 {
        guard let number = bundle.object(forInfoDictionaryKey: key) as? NSNumber else {
            throw SeldLaunchError.invalidConfiguration(key)
        }
        let value = number.uint64Value
        guard value > 0 else {
            throw SeldLaunchError.invalidConfiguration(key)
        }
        return value
    }
}

private struct BridgeState: Decodable {
    let formatVersion: Int
    let instanceID: String
    let pid: Int
    let port: Int
    let token: String
    let url: String
    let vault: String
    let vaultID: String

    enum CodingKeys: String, CodingKey {
        case formatVersion = "format_version"
        case instanceID = "instance_id"
        case pid
        case port
        case token
        case url
        case vault
        case vaultID = "vault_id"
    }
}

private struct BridgeHealth: Decodable {
    let instanceID: String
    let pid: Int
    let port: Int
    let service: String
    let vaultID: String
    let vaultRootDevice: UInt64
    let vaultRootInode: UInt64

    enum CodingKeys: String, CodingKey {
        case instanceID = "instance_id"
        case pid
        case port
        case service
        case vaultID = "vault_id"
        case vaultRootDevice = "vault_root_device"
        case vaultRootInode = "vault_root_inode"
    }
}

private final class SeldViewController: NSViewController, WKNavigationDelegate, WKUIDelegate {
    private static let healthTimeout: TimeInterval = 0.8
    private static let serviceTimeout: DispatchTimeInterval = .seconds(30)
    private static let stateLimit = 64 * 1024

    private let configuration: SeldConfiguration
    private var allowedOrigin: (host: String, port: Int)?
    private var startInProgress = false
    private var webView: WKWebView?

    init(configuration: SeldConfiguration) {
        self.configuration = configuration
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func loadView() {
        let canvas = NSView()
        canvas.wantsLayer = true
        canvas.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        view = canvas
        showLoading()
        startBridge()
    }

    func recoverIfNeeded() {
        guard !startInProgress else { return }
        let configuration = self.configuration
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard Self.healthyBridgeURL(configuration) == nil else {
                return
            }
            DispatchQueue.main.async {
                self?.showLoading()
                self?.startBridge()
            }
        }
    }

    private func startBridge() {
        guard !startInProgress else { return }
        startInProgress = true
        let configuration = self.configuration
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let url = try Self.bridgeURL(configuration)
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.startInProgress = false
                    self.showBridge(url)
                }
            } catch {
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.startInProgress = false
                    self.showFailure(error)
                }
            }
        }
    }

    private static func bridgeURL(_ configuration: SeldConfiguration) throws -> URL {
        if let current = healthyBridgeURL(configuration) {
            return current
        }
        try runService(configuration)
        let deadline = Date().addingTimeInterval(4)
        while Date() < deadline {
            if let current = healthyBridgeURL(configuration) {
                return current
            }
            Thread.sleep(forTimeInterval: 0.1)
        }
        throw SeldLaunchError.unavailable
    }

    private static func runService(_ configuration: SeldConfiguration) throws {
        let values = try configuration.serviceExecutable.resourceValues(
            forKeys: [.isRegularFileKey, .isSymbolicLinkKey]
        )
        guard values.isRegularFile == true,
              values.isSymbolicLink != true,
              FileManager.default.isExecutableFile(atPath: configuration.serviceExecutable.path)
        else {
            throw SeldLaunchError.invalidConfiguration("its verified service")
        }

        let process = Process()
        process.executableURL = configuration.serviceExecutable
        process.arguments = [
            "--json",
            "--vault",
            configuration.vaultRoot,
            "bridge",
            "open",
            "--no-browser",
        ]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let completed = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in completed.signal() }
        do {
            try process.run()
        } catch {
            throw SeldLaunchError.serviceFailed
        }
        guard completed.wait(timeout: .now() + serviceTimeout) == .success else {
            process.terminate()
            throw SeldLaunchError.serviceTimedOut
        }
        guard process.terminationStatus == 0 else {
            throw SeldLaunchError.serviceFailed
        }
    }

    private static func healthyBridgeURL(_ configuration: SeldConfiguration) -> URL? {
        guard let state = readState(configuration),
              let healthURL = URL(string: state.url + "api/v1/health")
        else { return nil }

        var request = URLRequest(url: healthURL)
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(state.token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = healthTimeout

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.connectionProxyDictionary = [:]
        let session = URLSession(configuration: sessionConfiguration)
        let completed = DispatchSemaphore(value: 0)
        var health: BridgeHealth?
        var statusCode = 0
        let task = session.dataTask(with: request) { data, response, _ in
            statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
            if let data, data.count <= stateLimit {
                health = try? JSONDecoder().decode(BridgeHealth.self, from: data)
            }
            completed.signal()
        }
        task.resume()
        guard completed.wait(timeout: .now() + healthTimeout + 0.2) == .success else {
            task.cancel()
            session.invalidateAndCancel()
            return nil
        }
        session.finishTasksAndInvalidate()
        guard statusCode == 200,
              let health,
              health.service == "gsv-bridge",
              health.instanceID == state.instanceID,
              health.pid == state.pid,
              health.port == state.port,
              health.vaultID == configuration.vaultID,
              health.vaultRootDevice == configuration.vaultRootDevice,
              health.vaultRootInode == configuration.vaultRootInode,
              var components = URLComponents(string: state.url)
        else { return nil }
        components.fragment = "token=\(state.token)"
        return components.url
    }

    private static func readState(_ configuration: SeldConfiguration) -> BridgeState? {
        let stateURL = URL(fileURLWithPath: configuration.bridgeStatePath)
        guard let values = try? stateURL.resourceValues(
            forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey]
        ),
        values.isRegularFile == true,
        values.isSymbolicLink != true,
        let size = values.fileSize,
        size > 0,
        size <= stateLimit,
        let data = try? Data(contentsOf: stateURL, options: [.mappedIfSafe]),
        data.count == size,
        let state = try? JSONDecoder().decode(BridgeState.self, from: data),
        validState(state, configuration: configuration)
        else { return nil }
        return state
    }

    private static func validState(
        _ state: BridgeState,
        configuration: SeldConfiguration
    ) -> Bool {
        guard state.formatVersion == 1,
              state.pid > 0,
              (1...65_535).contains(state.port),
              state.instanceID.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil,
              state.token.range(of: "^[0-9a-f]{48}$", options: .regularExpression) != nil,
              state.vaultID == configuration.vaultID,
              canonicalPath(state.vault) == canonicalPath(configuration.vaultRoot),
              let components = URLComponents(string: state.url)
        else { return false }
        return components.scheme == "http"
            && components.host == "127.0.0.1"
            && components.port == state.port
            && (components.path.isEmpty || components.path == "/")
            && components.user == nil
            && components.password == nil
            && components.query == nil
            && components.fragment == nil
    }

    private static func canonicalPath(_ value: String) -> String {
        URL(fileURLWithPath: value)
            .standardizedFileURL
            .resolvingSymlinksInPath()
            .path
    }

    private func showLoading() {
        clearView()
        let title = NSTextField(labelWithString: "Seld is getting your briefing ready…")
        title.font = .systemFont(ofSize: 23, weight: .semibold)
        title.alignment = .center

        let detail = NSTextField(
            labelWithString: "It is opening the verified local record on this Mac."
        )
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        detail.alignment = .center

        let progress = NSProgressIndicator()
        progress.style = .spinning
        progress.controlSize = .small
        progress.startAnimation(nil)

        let stack = NSStackView(views: [title, detail, progress])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 32),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -32),
        ])
    }

    private func showFailure(_ error: Error) {
        clearView()
        let title = NSTextField(labelWithString: "Seld couldn’t open your briefing")
        title.font = .systemFont(ofSize: 23, weight: .semibold)
        title.alignment = .center

        let detail = NSTextField(wrappingLabelWithString: error.localizedDescription)
        detail.font = .systemFont(ofSize: 14)
        detail.textColor = .secondaryLabelColor
        detail.alignment = .center
        detail.maximumNumberOfLines = 4

        let retry = NSButton(title: "Try again", target: self, action: #selector(retryBridge))
        retry.bezelStyle = .rounded
        retry.keyEquivalent = "\r"

        let stack = NSStackView(views: [title, detail, retry])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            detail.widthAnchor.constraint(lessThanOrEqualToConstant: 520),
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 40),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -40),
        ])
    }

    @objc private func retryBridge() {
        showLoading()
        startBridge()
    }

    private func clearView() {
        webView = nil
        view.subviews.forEach { $0.removeFromSuperview() }
    }

    private func showBridge(_ url: URL) {
        clearView()
        allowedOrigin = (url.host ?? "", url.port ?? 0)
        let webConfiguration = WKWebViewConfiguration()
        webConfiguration.websiteDataStore = .default()
        webConfiguration.applicationNameForUserAgent = "Seld"
        let webView = WKWebView(frame: .zero, configuration: webConfiguration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsMagnification = true
        webView.translatesAutoresizingMaskIntoConstraints = false
        self.webView = webView
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData))
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if sameBridge(url) {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "codex" {
            NSWorkspace.shared.open(url)
        } else if ["https", "http"].contains(url.scheme ?? ""),
                  !["127.0.0.1", "localhost", "::1"].contains(url.host ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let url = navigationAction.request.url else { return nil }
        if sameBridge(url) {
            webView.load(navigationAction.request)
        } else if url.scheme == "codex"
                    || (["https", "http"].contains(url.scheme ?? "")
                        && !["127.0.0.1", "localhost", "::1"].contains(url.host ?? "")) {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    private func sameBridge(_ url: URL) -> Bool {
        guard url.scheme == "http", let allowedOrigin else { return false }
        return url.host == allowedOrigin.host && url.port == allowedOrigin.port
    }
}

private final class SeldAppDelegate: NSObject, NSApplicationDelegate {
    private var controller: SeldViewController?
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMainMenu()
        do {
            let controller = SeldViewController(configuration: try SeldConfiguration())
            let window = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 1_240, height: 820),
                styleMask: [.titled, .closable, .miniaturizable, .resizable],
                backing: .buffered,
                defer: false
            )
            window.title = "Seld"
            window.titleVisibility = .hidden
            window.isReleasedWhenClosed = false
            window.minSize = NSSize(width: 820, height: 620)
            window.contentViewController = controller
            if !window.setFrameUsingName("Seld Main Window") {
                window.center()
            }
            window.setFrameAutosaveName("Seld Main Window")
            window.makeKeyAndOrderFront(nil)
            self.controller = controller
            self.window = window
            NSApp.activate(ignoringOtherApps: true)
        } catch {
            let alert = NSAlert(error: error)
            alert.messageText = "Seld could not open"
            alert.runModal()
            NSApp.terminate(nil)
        }
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        if let window {
            if window.isMiniaturized {
                window.deminiaturize(nil)
            }
            window.makeKeyAndOrderFront(nil)
        }
        controller?.recoverIfNeeded()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func installMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "About Seld",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Seld", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Seld", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        mainMenu.addItem(editItem)
        NSApp.mainMenu = mainMenu
    }
}

private let app = NSApplication.shared
private let delegate = SeldAppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
