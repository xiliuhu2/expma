// EXPMA工作台 — 原生壳应用：WKWebView 全屏加载本地策略工作台
// 启动时确保 server.py(8765) + alertd.py(到价通知) 常驻，然后加载界面
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    let home = FileManager.default.homeDirectoryForCurrentUser.path
    var url: URL { URL(string: "http://127.0.0.1:8765/#sh600115/p1")! }

    func applicationDidFinishLaunching(_ note: Notification) {
        ensureServices()

        webView = WKWebView(frame: .zero)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "EXPMA 策略工作台"
        window.minSize = NSSize(width: 1100, height: 700)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("EXPMAWorkbenchMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        load(retries: 10)
    }

    // 服务就绪探测失败时重试加载
    func load(retries: Int) {
        if retries <= 0 { return }
        webView.load(URLRequest(url: url))
        _ = DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self, (self.webView.title ?? "").isEmpty, retries > 1 else { return }
            self.load(retries: retries - 1)
        }
    }

    // 拉起 server + alertd（幂等；与 supervisor.sh 同口径）
    func ensureServices() {
        let script = """
        W="$HOME/WorkBuddy/Claw/webapp"
        PYTHON="$(command -v python3)"
        pgrep -f "Claw/webapp/server\\.py" >/dev/null 2>&1 || nohup "$PYTHON" "$W/server.py" 8765 >>"$W/launchd.log" 2>&1 &
        pgrep -f "Claw/webapp/alertd"    >/dev/null 2>&1 || nohup "$PYTHON" "$W/alertd.py" >>"$W/launchd.log" 2>&1 &
        for i in 1 2 3 4 5 6 7 8; do
          curl -s -o /dev/null --max-time 1 http://127.0.0.1:8765/ && break
          sleep 1
        done
        """
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/bash")
        task.arguments = ["-c", script]
        try? task.run()
        task.waitUntilExit()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    // ---- 外链处理：非本机(127.0.0.1)一律交给系统默认浏览器 ----

    // target="_blank" 新窗口请求（页面里雪球等外链都是这种）
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = navigationAction.targetFrame?.request.url ?? navigationAction.request.url,
           !isLocal(u) { NSWorkspace.shared.open(u) }
        return nil
    }

    // 同窗口导航到外部站点的兜底
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.navigationType == .linkActivated, let u = navigationAction.request.url, !isLocal(u) {
            NSWorkspace.shared.open(u)
            decisionHandler(.cancel)
        } else {
            decisionHandler(.allow)
        }
    }

    private func isLocal(_ u: URL) -> Bool {
        u.host == "127.0.0.1" || u.host == "localhost"
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
