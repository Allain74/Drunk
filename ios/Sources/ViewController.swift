import UIKit
import WebKit

class ViewController: UIViewController, WKNavigationDelegate {

    // ── Config ──────────────────────────────────────────────────────────────
    private let siteURL = URL(string: "https://drunk-weld.vercel.app")!

    // ── UI ───────────────────────────────────────────────────────────────────
    private var webView: WKWebView!
    private var progressBar: UIProgressView!
    private var progressObserver: NSKeyValueObservation?

    // ── Lifecycle ────────────────────────────────────────────────────────────
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        setupWebView()
        setupProgressBar()
        loadSite()
    }

    // ── Setup ────────────────────────────────────────────────────────────────
    private func setupWebView() {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        // Autoriser le stockage localStorage/cookies (pour le login)
        config.websiteDataStore = .default()

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.scrollView.bounces = false
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
    }

    private func setupProgressBar() {
        progressBar = UIProgressView(progressViewStyle: .bar)
        progressBar.tintColor = .systemGreen
        progressBar.trackTintColor = .clear
        progressBar.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(progressBar)

        NSLayoutConstraint.activate([
            progressBar.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            progressBar.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            progressBar.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            progressBar.heightAnchor.constraint(equalToConstant: 2),
        ])

        progressObserver = webView.observe(\.estimatedProgress, options: .new) { [weak self] webView, _ in
            let p = Float(webView.estimatedProgress)
            self?.progressBar.setProgress(p, animated: true)
            if p >= 1.0 {
                UIView.animate(withDuration: 0.3, delay: 0.3) {
                    self?.progressBar.alpha = 0
                } completion: { _ in
                    self?.progressBar.setProgress(0, animated: false)
                    self?.progressBar.alpha = 1
                }
            }
        }
    }

    private func loadSite() {
        webView.load(URLRequest(url: siteURL, cachePolicy: .returnCacheDataElseLoad))
    }

    // ── WKNavigationDelegate ─────────────────────────────────────────────────
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showOfflinePage()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showOfflinePage()
    }

    private func showOfflinePage() {
        let html = """
        <html><body style="font-family:sans-serif;text-align:center;padding:60px;background:#111;color:#eee">
          <div style="font-size:60px">🍺</div>
          <h2>Pas de connexion</h2>
          <p>Vérifie ta connexion et réessaie.</p>
          <button onclick="location.reload()"
            style="background:#22c55e;color:#fff;border:none;padding:12px 24px;border-radius:12px;font-size:16px">
            Réessayer
          </button>
        </body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // ── Pull to refresh ──────────────────────────────────────────────────────
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        let refresh = UIRefreshControl()
        refresh.addTarget(self, action: #selector(pullRefresh), for: .valueChanged)
        webView.scrollView.refreshControl = refresh
    }

    @objc private func pullRefresh(_ sender: UIRefreshControl) {
        webView.reload()
        sender.endRefreshing()
    }

    // ── Cleanup ──────────────────────────────────────────────────────────────
    deinit {
        progressObserver?.invalidate()
    }
}
