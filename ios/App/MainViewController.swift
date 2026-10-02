import UIKit
import WebKit
import SafariServices

/// Hosts the existing web UI (bundled copy of web/) in a WKWebView. Every /api/... request the
/// page makes is answered in-process by the API (see SchemeHandler), so nothing else is needed:
/// no Mac, no server, no browser.
final class MainViewController: UIViewController, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    let core: AppCore
    private var webView: WKWebView!
    private let scheme: SchemeHandler
    private var statusTimer: Timer?
    private var bgTask: UIBackgroundTaskIdentifier = .invalid

    static let startURL = URL(string: "app://local/index.html")!

    init(core: AppCore) {
        self.core = core
        scheme = SchemeHandler(api: core.api)
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError() }

    override var prefersStatusBarHidden: Bool { false }
    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    static func makeConfiguration(scheme: SchemeHandler, handler: WKScriptMessageHandler?) -> WKWebViewConfiguration {
        let config = WKWebViewConfiguration()
        config.setURLSchemeHandler(scheme, forURLScheme: "app")
        let ucc = WKUserContentController()
        ucc.addUserScript(WKUserScript(source: "window.NATIVE_APP = true;", injectionTime: .atDocumentStart, forMainFrameOnly: false))
        if let h = handler { ucc.add(h, name: "native") }
        config.userContentController = ucc
        config.allowsInlineMediaPlayback = true
        return config
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 7 / 255, green: 6 / 255, blue: 5 / 255, alpha: 1)
        webView = WKWebView(frame: view.bounds, configuration: MainViewController.makeConfiguration(scheme: scheme, handler: self))
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        webView.isOpaque = false
        webView.backgroundColor = view.backgroundColor
        webView.scrollView.backgroundColor = view.backgroundColor
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.uiDelegate = self
        webView.navigationDelegate = self
        if #available(iOS 16.4, *) { webView.isInspectable = true }
        view.addSubview(webView)
        webView.load(URLRequest(url: MainViewController.startURL))

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(didEnterBackground), name: UIApplication.didEnterBackgroundNotification, object: nil)
        nc.addObserver(self, selector: #selector(willEnterForeground), name: UIApplication.willEnterForegroundNotification, object: nil)
        nc.addObserver(self, selector: #selector(willTerminate), name: UIApplication.willTerminateNotification, object: nil)
        // Keep the screen awake while a float is live (a dark screen mid-show is a scare, and
        // iOS stops our output once the app is suspended).
        statusTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            let live = self.core.ctl.outputEnabled
            if UIApplication.shared.isIdleTimerDisabled != live { UIApplication.shared.isIdleTimerDisabled = live }
        }
    }

    // ------------------------------------------------------------ lifecycle
    @objc private func didEnterBackground() {
        bgTask = UIApplication.shared.beginBackgroundTask(withName: "save") { [weak self] in
            guard let self = self else { return }
            UIApplication.shared.endBackgroundTask(self.bgTask)
            self.bgTask = .invalid
        }
        let engine = core.api.engine
        let store = core.store
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            engine.enterBackground()
            store.markDirty()
            try? store.flush()
            DispatchQueue.main.async {
                guard let self = self, self.bgTask != .invalid else { return }
                UIApplication.shared.endBackgroundTask(self.bgTask)
                self.bgTask = .invalid
            }
        }
    }

    @objc private func willEnterForeground() {
        core.api.engine.enterForeground()
        webView.evaluateJavaScript("typeof pollStatus === 'function' && pollStatus()", completionHandler: nil)
    }

    @objc private func willTerminate() {
        core.api.engine.release(toBlack: false)
        core.store.close()
    }

    // ------------------------------------------------------------ links that open something else
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { return decisionHandler(.cancel) }
        if url.scheme == "app" {
            // The app page itself stays put; patch sheet / guide links open in a sheet.
            if navigationAction.navigationType == .linkActivated && url.path != "/" && url.path != "/index.html" {
                open(url)
                return decisionHandler(.cancel)
            }
            return decisionHandler(.allow)
        }
        if url.scheme == "about" { return decisionHandler(.allow) }
        open(url)
        decisionHandler(.cancel)
    }

    private func open(_ url: URL) {
        if url.scheme == "app" {
            let vc = DocumentViewController(url: url, scheme: scheme)
            let nav = UINavigationController(rootViewController: vc)
            nav.modalPresentationStyle = .pageSheet
            present(nav, animated: true)
        } else if url.scheme == "http" || url.scheme == "https" {
            // The box's own web page (REAP, login robe / 2479).
            present(SFSafariViewController(url: url), animated: true)
        }
    }

    // ------------------------------------------------------------ messages from the page
    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        if cmd == "export" { exportProject() }
    }

    private func exportProject() {
        let snap = core.store.snapshot()
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd HHmm"
        let name = "DMX Scene Builder \(fmt.string(from: Date())).json"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        do {
            try snap.data(indent: 1).write(to: url, options: .atomic)
        } catch {
            let a = UIAlertController(title: "Export failed", message: "\(error)", preferredStyle: .alert)
            a.addAction(UIAlertAction(title: "OK", style: .default))
            present(a, animated: true)
            return
        }
        let share = UIActivityViewController(activityItems: [url], applicationActivities: nil)
        if let pop = share.popoverPresentationController {
            pop.sourceView = view
            pop.sourceRect = CGRect(x: view.bounds.midX, y: 80, width: 1, height: 1)
            pop.permittedArrowDirections = [.up]
        }
        present(share, animated: true)
    }

    // ------------------------------------------------------------ JS dialogs (the page uses its own modals, but be safe)
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(a, animated: true)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        a.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        a.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(a, animated: true)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()  // the page is only the remote control; the show state lives in the app
    }
}

/// A sheet showing an app page (patch sheet, field guide) with Print and Share.
final class DocumentViewController: UIViewController {
    let url: URL
    let scheme: SchemeHandler
    private var webView: WKWebView!

    init(url: URL, scheme: SchemeHandler) {
        self.url = url
        self.scheme = scheme
        super.init(nibName: nil, bundle: nil)
    }
    required init?(coder: NSCoder) { fatalError() }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        webView = WKWebView(frame: view.bounds, configuration: MainViewController.makeConfiguration(scheme: scheme, handler: nil))
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(webView)
        webView.load(URLRequest(url: url))
        navigationItem.leftBarButtonItem = UIBarButtonItem(barButtonSystemItem: .done, target: self, action: #selector(done))
        navigationItem.rightBarButtonItem = UIBarButtonItem(image: UIImage(systemName: "printer"), style: .plain, target: self, action: #selector(printPage))
        title = url.path.hasPrefix("/patch/") ? "Patch sheet" : "Field guide"
    }

    @objc private func done() { dismiss(animated: true) }

    @objc private func printPage() {
        let pc = UIPrintInteractionController.shared
        let info = UIPrintInfo(dictionary: nil)
        info.outputType = .general
        info.jobName = title ?? "DMX Scene Builder"
        pc.printInfo = info
        pc.printFormatter = webView.viewPrintFormatter()
        pc.present(from: navigationItem.rightBarButtonItem!, animated: true)
    }
}

/// Answers app://local/... : static files from the bundled web UI, /api/... from the API.
final class SchemeHandler: NSObject, WKURLSchemeHandler {
    let api: API
    private var live = Set<ObjectIdentifier>()  // main thread only

    init(api: API) { self.api = api }

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        let id = ObjectIdentifier(task)
        live.insert(id)
        let req = task.request
        guard let url = req.url else { return }
        let method = req.httpMethod ?? "GET"
        var body = req.httpBody
        if body == nil, let stream = req.httpBodyStream {
            var d = Data()
            stream.open()
            var buf = [UInt8](repeating: 0, count: 65536)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                d.append(buf, count: n)
            }
            stream.close()
            body = d
        }
        var query: [String: String] = [:]
        for item in URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? [] { query[item.name] = item.value ?? "" }
        let api = self.api
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let r = api.handle(method: method, path: url.path, query: query, body: body)
            DispatchQueue.main.async {
                guard let self = self, self.live.remove(id) != nil else { return }  // the page gave up on it
                let headers = ["Content-Type": r.contentType, "Cache-Control": "no-store", "Content-Length": String(r.body.count)]
                let resp = HTTPURLResponse(url: url, statusCode: r.status, httpVersion: "HTTP/1.1", headerFields: headers)!
                task.didReceive(resp)
                task.didReceive(r.body)
                task.didFinish()
            }
        }
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        live.remove(ObjectIdentifier(task))
    }
}
