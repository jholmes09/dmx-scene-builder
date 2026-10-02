import SwiftUI
import UIKit

@main
struct DMXSceneBuilderApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
                .ignoresSafeArea()
                .preferredColorScheme(.dark)
        }
    }
}

struct RootView: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> UIViewController {
        switch AppCore.shared {
        case .success(let core): return MainViewController(core: core)
        case .failure(let message): return StartupErrorViewController(message: message)
        }
    }
    func updateUIViewController(_ vc: UIViewController, context: Context) {}
}

/// The whole show-control core, running inside the app: project store, Art-Net output, the API.
final class AppCore {
    enum Result { case success(AppCore), failure(String) }

    let store: Store
    let ctl: ArtNetController
    let api: API

    static let shared: Result = {
        Store.hostname = UIDevice.current.name
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        do {
            let store = try Store(directory: docs)
            let ctl = ArtNetController(port: ArtNet.port).start()
            let web = Bundle.main.url(forResource: "web", withExtension: nil)
            return .success(AppCore(store: store, ctl: ctl, api: API(store: store, ctl: ctl, webRoot: web)))
        } catch {
            // Fail loud: never start on an empty project over a file we couldn't read.
            return .failure("The project file couldn't be opened, so the app stopped rather than start empty.\n\n\(error)\n\nIn the Files app, open On My iPad > DMX Scene Builder. Move project.json somewhere safe (or replace it with one from the backups folder), then open the app again.")
        }
    }()

    init(store: Store, ctl: ArtNetController, api: API) {
        self.store = store
        self.ctl = ctl
        self.api = api
    }
}

final class StartupErrorViewController: UIViewController {
    let message: String
    init(message: String) { self.message = message; super.init(nibName: nil, bundle: nil) }
    required init?(coder: NSCoder) { fatalError() }
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(red: 0.03, green: 0.02, blue: 0.02, alpha: 1)
        let label = UILabel()
        label.text = message
        label.numberOfLines = 0
        label.textColor = .white
        label.font = .systemFont(ofSize: 18)
        label.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            label.widthAnchor.constraint(lessThanOrEqualToConstant: 640),
            label.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: 32),
        ])
    }
}
