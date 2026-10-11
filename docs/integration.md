# Интеграция в своё приложение

Три шага: получить адрес и токен от владельца агента → подключиться по WebSocket →
отправлять и принимать кадры. Никакого SDK-обязательства нет: протокол — это JSON-кадры,
поэтому подойдёт любая платформа с WebSocket.

## 1. Что нужно от владельца агента

| Что | Откуда | Пример |
|---|---|---|
| Адрес адаптера | `MOBILE_HOST`/`MOBILE_PORT`/`MOBILE_PATH` на машине агента, либо домен через reverse-proxy | `wss://agent.example.com/ws` |
| Токен приложения | `MOBILE_TOKEN` в `~/.hermes/.env` | 64 hex-символа |
| Разрешение устройству | `hermes pairing approve mobile <id>` после первого контакта, либо заранее `MOBILE_ALLOWED_USERS` | `hermes pairing approve mobile 84b9042da9357a03` |

Идентификатор устройства (`device.id`) выбирает приложение и держит его постоянным: от него
зависит идентификатор сессии в Hermes (`mobile:<device.id>`), адрес для cron-доставки и
записи в allowlist. Один пользователь может иметь несколько устройств — это разные сессии.

## 2. Подключение и жизненный цикл

```
connect() → отправляем hello{token, device} → ждём ready{session, avatar_url, capabilities}
    ↓
текст/голос вверх (text/audio) ⟷ ответы вниз (message, typing, avatar, error)
    ↓
обрыв сети → переподключение с паузой → снова hello (история и сессия сохраняются на стороне Hermes)
```

Правила, которые важно соблюсти:

* **Первый кадр — только `hello`.** Иначе адаптер пришлёт `error` и закроет соединение.
* **Код закрытия 1008** — отказ в доступе (неверный токен или устройство вне списка).
  Повторять бессмысленно: покажите пользователю «нет доступа» и не долбите сервер.
* **`id` сообщения** ставьте постоянный для каждого сообщения и повторяйте его при
  переотправке после обрыва: адаптер игнорирует повторы (защита от двойных сообщений).
* **Неизвестные кадры игнорируйте** — так протокол расширяется без поломок.
* **Разрывы нормальны** (мобильная сеть, сон устройства). Переподключайтесь с
  экспоненциальной паузой (в TS-SDK это встроено).

## 3. Ответы агента и «печатает»

Длинный ответ приходит несколькими кадрами `message` с одним и тем же `id` и полем
`chunk: {index, total}`; последний кадр имеет `final: true`. Склеивайте по `id` — так
ответ выглядит как одно сообщение. Пока `final` не пришёл, показывайте индикатор.

Кадры `avatar` (`thinking` / `speaking` / `idle`) и `typing` — сигналы для интерфейса:
анимация аватара, «агент печатает», отключение кнопки отправки.

## 4. Голос

1. Запишите WAV: 16 кГц, моно, PCM 16 бит (так делает `react-native-live-audio-stream`;
   на iOS/Android то же самое даёт `AVAudioRecorder`/`MediaRecorder`).
2. Отправьте `audio` с `format: "wav"` и `data: base64`.
3. Если у вас есть собственное распознавание — положите текст в `text`: агент получит и
   текст, и аудиофайл. Если текста нет, Hermes распознает речь сам (при настроенном STT на
   машине агента).

Ограничения: аудио ≤ 8 МБ (примерно несколько минут речи), кадр целиком ≤ 16 МБ. Для длинных
записей отправляйте несколько сообщений.

Озвучка ответа — на стороне приложения (TTS устройства) или аватара: SDK отдаёт текст,
дальше решаете вы.

## 5. Аватар

Аватар — обычная HTML-страница в WebView, которая реализует контракт:

```js
window.hermesAvatar = {
  setState: (state) => {},   // 'idle' | 'thinking' | 'speaking'
  speak:    (text) => {},    // просьба озвучить/показать реплику агента
  setVolume:(level) => {},   // уровень микрофона при записи, 0..1
};
```

URL страницы приходит в кадре `ready` (`avatar_url`) — задаётся переменной
`MOBILE_AVATAR_URL` на машине агента. Если URL не задан, используйте свою страницу
(в `examples/react-native/avatarHtml.ts` есть минимальная с тем же контрактом).

Вызовы из приложения:

```ts
webView.injectJavaScript(`window.hermesAvatar.setState('speaking'); true;`);
```

## 6. Скетчи для разных платформ

Общая часть везде одна: WebSocket + JSON. Ниже — минимальный «скелет» подключения.

**Kotlin (Android, OkHttp)**

```kotlin
val ws = OkHttpClient().newWebSocket(
  Request.Builder().url("$wsUrl?token=$token").build(),
  object : WebSocketListener() {
    override fun onOpen(webSocket: WebSocket?, response: Response?) {
      webSocket?.send("""{"t":"hello","token":"$token","device":{"id":"android-1","name":"Телефон","platform":"android"}}""")
    }
    override fun onMessage(webSocket: WebSocket?, text: String) {
      val frame = JSONObject(text)
      when (frame.getString("t")) {
        "ready" -> session = frame.getString("session")
        "message" -> append(frame.getString("text"), frame.getBoolean("final"))
        "avatar" -> avatarState = frame.getString("state")
      }
    }
    override fun onClosing(code: Int, reason: String?) { if (code == 1008) showNoAccess() }
  }
)
```

**Swift (iOS, URLSessionWebSocketTask)**

```swift
let task = URLSession.shared.webSocketTask(with: URL(string: "\(wsUrl)?token=\(token)")!)
task.resume()
task.send(.string(#"{"t":"hello","token":"\#(token)","device":{"id":"ios-1","name":"iPhone","platform":"ios"}}"#))
// далее task.receive { ... } в цикле, разбор JSON по полю "t"
```

**Dart / Flutter (web_socket_channel)**

```dart
final channel = WebSocketChannel.connect(Uri.parse('$wsUrl?token=$token'));
channel.sink.add(jsonEncode({'t': 'hello', 'token': token, 'device': {'id': 'flutter-1', 'name': 'Телефон'}}));
channel.stream.listen((raw) {
  final frame = jsonDecode(raw as String) as Map<String, dynamic>;
  if (frame['t'] == 'message') addMessage(frame['text'] as String, frame['final'] as bool);
});
```

## 7. Диагностика

| Симптом | Причина |
|---|---|
| `auth_failed` / закрытие 1008 | токен не совпадает с `MOBILE_TOKEN`, либо `device.id` отсутствует в `MOBILE_ALLOWED_USERS` |
| `ready` приходит, ответов нет | устройство ещё не одобрено: `hermes pairing list` → `approve`; на первом контакте агент отвечает pairing-кодом |
| Ответ «No home channel is set» | штатное напоминание Hermes: отправьте `/sethome`, чтобы этот чат стал домашним (туда уходят cron-сообщения) |
| `device_offline` в cron-доставке | приложение не подключено: мобильная доставка требует живого соединения |
| «кадр не пришёл» при подключении извне | порт адаптера не открыт наружу или не проксируется через Caddy — см. `docs/adapter-install.md` |

Проверить руками: `wscat -c "ws://<хост>:8787/ws?token=<токен>"`, затем отправить
`{"t":"hello","device":{"id":"cli"}}`.

## 8. Безопасность

* Токен — единственный секрет приложения: храните его в Keychain/Keystore, не в открытом
  коде и не в логи.
* Наружу — только `wss://` (TLS на reverse-proxy). `ws://` допустим в локальной сети или
  через `adb reverse tcp:8787 tcp:8787` при разработке.
* Одно приложение — один токен (`MOBILE_EXTRA_TOKENS` позволяет их различить). Отзыв токена
  = убрать его из `.env` и перезапустить шлюз.
* Полезная нагрузка — часть диалога с агентом: не отправляйте через протокол данные, которые
  агент не должен видеть.
