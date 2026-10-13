# iOS (Swift, URLSessionWebSocketTask)

A complete path on the native `URLSessionWebSocketTask`: connecting, `Hello`/`Ready`, sending
text and photos (base64), handling prompt cards via `UIAlertController`, saving
incoming media to `Documents`, streaming edits. No third-party dependencies.

## 1. Protocol client

Keep the token only in the Keychain, not in the sources. Keep `device.id` constant (for example in
`UserDefaults`, generated once).

```swift
import Foundation

enum ConnectionState: String { case idle, connecting, connected, reconnecting, failed }

struct HermesDevice { let id: String; let name: String; let platform: String; let appVersion: String }

protocol HermesClientDelegate: AnyObject {
    func hermes(_ c: HermesClient, didChange state: ConnectionState)
    func hermes(_ c: HermesClient, didReady ready: [String: Any])
    func hermes(_ c: HermesClient, didReceiveMessage id: String, text: String, final: Bool)
    func hermes(_ c: HermesClient, didEditMessage id: String, text: String)   // streaming
    func hermes(_ c: HermesClient, didChangeAvatar state: String)
    func hermes(_ c: HermesClient, didChangeTyping active: Bool)
    func hermes(_ c: HermesClient, didReceivePrompt prompt: [String: Any])
    func hermes(_ c: HermesClient, didFinishPrompt id: String, optionId: String?, expired: Bool)
    func hermes(_ c: HermesClient, didReceiveMedia media: [String: Any])       // whole file
    func hermes(_ c: HermesClient, didFail code: String, message: String, fatal: Bool)
}

final class HermesClient: NSObject {
    private let url: URL
    private let token: String
    private let device: HermesDevice
    weak var delegate: HermesClientDelegate?

    private var task: URLSessionWebSocketTask?
    private var session: URLSession!
    private var attempt = 0
    private var closedByUser = false
    private var reconnectWork: DispatchWorkItem?

    // Assembling multi-part media: id → parts.
    private var mediaParts: [String: [Int: Data]] = [:]
    private var mediaMeta: [String: [String: Any]] = [:]
    private let mediaChunk = 3 * 1024 * 1024

    init(url: URL, token: String, device: HermesDevice) {
        self.url = url; self.token = token; self.device = device
        super.init()
        self.session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    }

    func connect() { closedByUser = false; open() }

    func disconnect() {
        closedByUser = true
        reconnectWork?.cancel()
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        delegate?.hermes(self, didChange: .idle)
    }

    private func open() {
        delegate?.hermes(self, didChange: attempt == 0 ? .connecting : .reconnecting)
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        var items = components.queryItems ?? []
        items.append(URLQueryItem(name: "token", value: token))
        components.queryItems = items
        let socket = session.webSocketTask(with: components.url!)
        task = socket
        socket.resume()
        send(["t": "hello", "v": 1, "token": token,
              "device": ["id": device.id, "name": device.name, "platform": device.platform, "app_version": device.appVersion],
              "user": [:]])
        receiveLoop()
    }

    private func scheduleReconnect() {
        attempt += 1
        delegate?.hermes(self, didChange: .reconnecting)
        let delay = min(500.0 * pow(2.0, Double(min(attempt, 5))), 15000.0) / 1000.0
        reconnectWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.open() }
        reconnectWork = work
        DispatchQueue.global().asyncAfter(deadline: .now() + delay, execute: work)
    }

    func send(_ frame: [String: Any]) {
        guard let task, let data = try? JSONSerialization.data(withJSONObject: frame),
              let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { [weak self] error in
            if let error, let self { self.delegate?.hermes(self, didFail: "offline", message: "\(error)", fatal: false) }
        }
    }

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure:
                self.handleClosed()
            case .success(let message):
                switch message {
                case .string(let text): self.handle(raw: text)
                case .data(let data): if let text = String(data: data, encoding: .utf8) { self.handle(raw: text) }
                @unknown default: break
                }
                self.receiveLoop() // keep the read loop going
            }
        }
    }

    private func handleClosed() {
        task = nil
        guard !closedByUser else { return }
        if task?.closeCode == .policyViolation {   // 1008
            delegate?.hermes(self, didChange: .failed)
            delegate?.hermes(self, didFail: "auth_failed", message: "Нет доступа: проверьте токен", fatal: true)
            return
        }
        scheduleReconnect()
    }

    private func handle(raw: String) {
        guard let f = try? JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any],
              let t = f["t"] as? String else { return }
        switch t {
        case "ready":
            attempt = 0
            delegate?.hermes(self, didChange: .connected)
            delegate?.hermes(self, didReady: f)
        case "typing":
            delegate?.hermes(self, didChangeTyping: !((f["stop"] as? Bool) ?? false))
        case "message":
            let id = f["id"] as? String ?? ""
            let text = f["text"] as? String ?? ""
            let isFinal = f["final"] as? Bool ?? false
            if f["edit"] as? Bool == true { delegate?.hermes(self, didEditMessage: id, text: text) }
            else { delegate?.hermes(self, didReceiveMessage: id, text: text, final: isFinal) }
        case "prompt":
            delegate?.hermes(self, didReceivePrompt: f)
        case "prompt_done":
            delegate?.hermes(self, didFinishPrompt: f["id"] as? String ?? "",
                             optionId: f["option_id"] as? String, expired: f["expired"] as? Bool ?? false)
        case "media":
            if let complete = accumulate(f) { delegate?.hermes(self, didReceiveMedia: complete) }
        case "avatar":
            delegate?.hermes(self, didChangeAvatar: f["state"] as? String ?? "idle")
        case "error":
            delegate?.hermes(self, didFail: f["code"] as? String ?? "error",
                             message: f["message"] as? String ?? "", fatal: f["fatal"] as? Bool ?? false)
        default:
            break // ignore an unknown frame
        }
    }
}
```

## 2. Media: sending a photo (base64) and receiving into Documents

```swift
extension HermesClient {
    /// Sending a photo: UIImage → JPEG → base64. Large photos are cut into chunks.
    func sendPhoto(_ image: UIImage, filename: String = "photo.jpg", caption: String? = nil) {
        guard let data = image.jpegData(compressionQuality: 0.85) else { return }
        sendMedia(kind: "image", bytes: data, mime: "image/jpeg", filename: filename, caption: caption)
    }

    func sendText(_ text: String, id: String = UUID().uuidString) {
        send(["t": "text", "id": id, "text": text])
    }

    func sendAudio(_ wav: Data, transcript: String? = nil) {
        var frame: [String: Any] = ["t": "audio", "id": UUID().uuidString, "format": "wav",
                                    "data": wav.base64EncodedString()]
        if let transcript { frame["text"] = transcript }
        send(frame)
    }

    func answerPrompt(_ promptId: String, optionId: String?, text: String? = nil) {
        var frame: [String: Any] = ["t": "prompt", "prompt_id": promptId]
        if let optionId { frame["option_id"] = optionId.lowercased() }
        if let text { frame["text"] = text }
        send(frame)
    }

    private func sendMedia(kind: String, bytes: Data, mime: String, filename: String?, caption: String?) {
        let id = UUID().uuidString
        let parts = max(1, Int(ceil(Double(bytes.count) / Double(mediaChunk))))
        for index in 0..<parts {
            let start = index * mediaChunk
            let end = min(start + mediaChunk, bytes.count)
            var frame: [String: Any] = ["t": "media", "id": id, "kind": kind, "mime": mime,
                                        "data": bytes.subdata(in: start..<end).base64EncodedString(),
                                        "part": index, "parts": parts]
            if let filename { frame["filename"] = filename }
            if let caption { frame["caption"] = caption }
            send(frame)
        }
    }

    /// Assembling multi-part media from the agent. Returns a whole file once the last chunk has arrived.
    private func accumulate(_ f: [String: Any]) -> [String: Any]? {
        let id = f["id"] as? String ?? ""
        let parts = (f["parts"] as? Int) ?? 1
        let part = (f["part"] as? Int) ?? 0
        mediaMeta[id] = f
        if parts <= 1 { return f }
        var bucket = mediaParts[id] ?? [:]
        if let dataString = f["data"] as? String, let chunk = Data(base64Encoded: dataString) { bucket[part] = chunk }
        if bucket.count < parts { mediaParts[id] = bucket; return nil }
        mediaParts[id] = nil
        var merged = mediaMeta[id] ?? f
        merged["data"] = (bucket.keys.sorted().compactMap { bucket[$0] }).reduce(Data(), +).base64EncodedString()
        return merged
    }

    /// Saves incoming media to Documents and returns the path.
    @discardableResult
    func saveMediaToDocuments(_ media: [String: Any]) -> URL? {
        let fm = FileManager.default
        let dir = fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("hermes-media", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        let name = (media["filename"] as? String) ?? "\((media["id"] as? String) ?? UUID().uuidString).bin"
        let target = dir.appendingPathComponent(name)
        if let dataString = media["data"] as? String, let data = Data(base64Encoded: dataString) {
            try? data.write(to: target)
            return target
        }
        // no data — the file is served via a url link: download it yourself (URLSession.shared.downloadTask).
        return nil
    }
}

extension HermesClient: URLSessionWebSocketDelegate {
    func urlSession(_ s: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        if closeCode == .policyViolation {   // 1008 — access denied
            delegate?.hermes(self, didChange: .failed)
            delegate?.hermes(self, didFail: "auth_failed", message: "Нет доступа: проверьте токен", fatal: true)
        }
    }
}
```

## 3. Prompt cards via UIAlertController

`exec_approval` and `slash_confirm` are a warning with options; `clarify` may carry
text input (`other`). The answer is a single `prompt` frame.

```swift
extension UIViewController {
    func presentPrompt(_ prompt: [String: Any], client: HermesClient) {
        let id = prompt["id"] as? String ?? ""
        let kind = prompt["kind"] as? String ?? ""
        let text = prompt["text"] as? String ?? ""
        let options = prompt["options"] as? [[String: Any]] ?? []

        let alert = UIAlertController(title: kind, message: text, preferredStyle: .alert)
        for option in options {
            let optionId = option["id"] as? String ?? ""
            let isDanger = (option["style"] as? String) == "danger"
            let title = option["label"] as? String ?? optionId
            let action = UIAlertAction(title: title, style: isDanger ? .destructive : .default) { [weak self] _ in
                if optionId == "other" {
                    // clarify "custom answer": option_id=other MUST carry text
                    let input = UIAlertController(title: "Свой ответ", message: nil, preferredStyle: .alert)
                    input.addTextField { $0.placeholder = "Введите ответ" }
                    input.addAction(UIAlertAction(title: "Отмена", style: .cancel))
                    input.addAction(UIAlertAction(title: "Отправить", style: .default) { _ in
                        let answer = input.textFields?.first?.text ?? ""
                        guard !answer.trimmingCharacters(in: .whitespaces).isEmpty else { return }
                        client.answerPrompt(id, optionId: "other", text: answer)
                    })
                    self?.present(input, animated: true)
                } else {
                    client.answerPrompt(id, optionId: optionId)
                }
            }
            alert.addAction(action)
        }
        present(alert, animated: true)
    }

    /// prompt_done{expired:true} arrived — the card expired; just close it.
    func closeExpiredPrompt() { presentedViewController?.dismiss(animated: true) }
}
```

Pitfalls:

* **`prompt_done` arrives both on expiry (300 s) and on your answer.** Do not consider the card
  answered until `prompt_done` arrives; the `option_id` in it is the variant actually applied
  (the server may have replaced an invalid value with `deny`/`cancel`).
* **`UIAlertController` is not suitable for `clarify` with text and options at the same time** on
  older iOS — for `other` show a second alert (as above) or a custom view.

## 4. Streaming edits and lifecycle

```swift
final class ChatViewController: UIViewController, HermesClientDelegate {
    var messages: [(id: String, text: String)] = []

    func hermes(_ c: HermesClient, didReceiveMessage id: String, text: String, final: Bool) {
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].text += text }
        else { messages.append((id, text)) }
        tableView.reloadData()
    }

    func hermes(_ c: HermesClient, didEditMessage id: String, text: String) {
        // Streaming: replace the text of the existing bubble instead of adding a new one.
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].text = text }
        else { messages.append((id, text)) }
        tableView.reloadData()
    }

    func hermes(_ c: HermesClient, didChangeTyping active: Bool) { /* "typing" indicator */ }
    func hermes(_ c: HermesClient, didChangeAvatar state: String) { /* avatar: idle|thinking|speaking */ }
    func hermes(_ c: HermesClient, didChange state: ConnectionState) { /* header/connection indicator */ }
    func hermes(_ c: HermesClient, didReady ready: [String: Any]) {
        // avatar_url from ready → load it into a WKWebView and call window.hermesAvatar.setState(...)
    }
    func hermes(_ c: HermesClient, didReceiveMedia media: [String: Any]) {
        _ = c.saveMediaToDocuments(media)  // save to Documents
        // media["kind"]: image | voice | video | document | animation
    }
    func hermes(_ c: HermesClient, didFail code: String, message: String, fatal: Bool) { /* banner */ }
    func hermes(_ c: HermesClient, didReceivePrompt prompt: [String: Any]) { presentPrompt(prompt, client: client) }
    func hermes(_ c: HermesClient, didFinishPrompt id: String, optionId: String?, expired: Bool) { /* close the card */ }
}
```

Lifecycle:

```swift
// AppDelegate / SceneDelegate
func applicationDidEnterBackground(_ app: UIApplication) {
    // The connection lives while the app is in the background (URLSession may put it to sleep).
}
func applicationWillEnterForeground(_ app: UIApplication) {
    client.connect() // check the state; if .failed — do not reconnect (1008)
}
```

Pitfalls:

* **`URLSessionWebSocketTask` requires a continuous `receive`.** If you stop calling
  `receive()` after each frame, the connection will "freeze". The loop in `receiveLoop()` is mandatory.
* **Background mode puts the socket to sleep** — on return to the foreground check the state and call
  `connect()` only if it is not `.failed`.
* **`sessions didCloseWith` does not provide the closeCode in a `receive` failure** — code 1008 arrives in
  the `didCloseWith` delegate; parse it from there.
* **Send photos as JPEG**, not PNG without need: the HEIC that the camera produces is not always
  recognized by the agent — convert to JPEG (`jpegData`).
