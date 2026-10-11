# iOS (Swift, URLSessionWebSocketTask)

Полный путь на нативном `URLSessionWebSocketTask`: подключение, `Hello`/`Ready`, отправка
текста и фото (base64), обработка prompt-карточек через `UIAlertController`, сохранение
входящих медиа в `Documents`, стриминг-правки. Без сторонних зависимостей.

## 1. Клиент протокола

Токен — только в Keychain, не в исходниках. `device.id` держите постоянным (например, в
`UserDefaults`, сгенерённый один раз).

```swift
import Foundation

enum ConnectionState: String { case idle, connecting, connected, reconnecting, failed }

struct HermesDevice { let id: String; let name: String; let platform: String; let appVersion: String }

protocol HermesClientDelegate: AnyObject {
    func hermes(_ c: HermesClient, didChange state: ConnectionState)
    func hermes(_ c: HermesClient, didReady ready: [String: Any])
    func hermes(_ c: HermesClient, didReceiveMessage id: String, text: String, final: Bool)
    func hermes(_ c: HermesClient, didEditMessage id: String, text: String)   // стриминг
    func hermes(_ c: HermesClient, didChangeAvatar state: String)
    func hermes(_ c: HermesClient, didChangeTyping active: Bool)
    func hermes(_ c: HermesClient, didReceivePrompt prompt: [String: Any])
    func hermes(_ c: HermesClient, didFinishPrompt id: String, optionId: String?, expired: Bool)
    func hermes(_ c: HermesClient, didReceiveMedia media: [String: Any])       // целый файл
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

    // Сборка многочастных медиа: id → части.
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
                self.receiveLoop() // продолжаем цикл чтения
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
            break // неизвестный кадр игнорируем
        }
    }
}
```

## 2. Медиа: отправка фото (base64) и приём в Documents

```swift
extension HermesClient {
    /// Отправка фото: UIImage → JPEG → base64. Крупное фото режем на куски.
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

    /// Сборка многочастных медиа от агента. Возвращает целый файл, когда пришёл последний кусок.
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

    /// Сохраняет входящее медиа в Documents и возвращает путь.
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
        // data нет — файл отдан ссылкой url: скачайте сами (URLSession.shared.downloadTask).
        return nil
    }
}

extension HermesClient: URLSessionWebSocketDelegate {
    func urlSession(_ s: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        if closeCode == .policyViolation {   // 1008 — отказ в доступе
            delegate?.hermes(self, didChange: .failed)
            delegate?.hermes(self, didFail: "auth_failed", message: "Нет доступа: проверьте токен", fatal: true)
        }
    }
}
```

## 3. Карточки prompt через UIAlertController

`exec_approval` и `slash_confirm` — это предупреждение с вариантами; `clarify` может нести
текстовый ввод (`other`). Ответ — один кадр `prompt`.

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
                    // clarify «свой ответ»: option_id=other ОБЯЗАН нести text
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

    /// Пришёл prompt_done{expired:true} — карточка истекла; просто закройте её.
    func closeExpiredPrompt() { presentedViewController?.dismiss(animated: true) }
}
```

Подводные камни:

* **`prompt_done` приходит и на истечение (300 с), и на ваш ответ.** Не считайте карточку
  отвеченной, пока не пришёл `prompt_done`; `option_id` в нём — фактически применённый вариант
  (сервер мог заменить недопустимое значение на `deny`/`cancel`).
* **`UIAlertController` не подходит для `clarify` с текстом и вариантами одновременно** в
  старых iOS — для `other` показывайте второй алерт (как выше) или кастомную вью.

## 4. Стриминг-правки и жизненный цикл

```swift
final class ChatViewController: UIViewController, HermesClientDelegate {
    var messages: [(id: String, text: String)] = []

    func hermes(_ c: HermesClient, didReceiveMessage id: String, text: String, final: Bool) {
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].text += text }
        else { messages.append((id, text)) }
        tableView.reloadData()
    }

    func hermes(_ c: HermesClient, didEditMessage id: String, text: String) {
        // Стриминг: заменяем текст существующего пузыря, а не добавляем новый.
        if let i = messages.firstIndex(where: { $0.id == id }) { messages[i].text = text }
        else { messages.append((id, text)) }
        tableView.reloadData()
    }

    func hermes(_ c: HermesClient, didChangeTyping active: Bool) { /* индикатор «печатает» */ }
    func hermes(_ c: HermesClient, didChangeAvatar state: String) { /* avatar: idle|thinking|speaking */ }
    func hermes(_ c: HermesClient, didChange state: ConnectionState) { /* заголовок/индикатор связи */ }
    func hermes(_ c: HermesClient, didReady ready: [String: Any]) {
        // avatar_url из ready → загрузите в WKWebView и вызывайте window.hermesAvatar.setState(...)
    }
    func hermes(_ c: HermesClient, didReceiveMedia media: [String: Any]) {
        _ = c.saveMediaToDocuments(media)  // сохранить в Documents
        // media["kind"]: image | voice | video | document | animation
    }
    func hermes(_ c: HermesClient, didFail code: String, message: String, fatal: Bool) { /* баннер */ }
    func hermes(_ c: HermesClient, didReceivePrompt prompt: [String: Any]) { presentPrompt(prompt, client: client) }
    func hermes(_ c: HermesClient, didFinishPrompt id: String, optionId: String?, expired: Bool) { /* закрыть карточку */ }
}
```

Жизненный цикл:

```swift
// AppDelegate / SceneDelegate
func applicationDidEnterBackground(_ app: UIApplication) {
    // Соединение живёт, пока приложение в фоне (URLSession может его усыпить).
}
func applicationWillEnterForeground(_ app: UIApplication) {
    client.connect() // проверьте состояние; если .failed — не переподключайтесь (1008)
}
```

Подводные камни:

* **`URLSessionWebSocketTask` требует непрерывного `receive`.** Если перестанете вызывать
  `receive()` после каждого кадра, соединение «застынет». Цикл в `receiveLoop()` обязателен.
* **Фоновый режим усыпляет сокет** — на возврате в foreground проверяйте состояние и зовите
  `connect()` только если не `.failed`.
* **`sessions didCloseWith` не даёт closeCode в `receive` failure** — код 1008 приходит в
  делегате `didCloseWith`; разбирайте его оттуда.
* **Фото шлите JPEG**, а не PNG без нужды: HEIC, который отдаёт камера, агент не всегда
  распознаёт — конвертируйте в JPEG (`jpegData`).
