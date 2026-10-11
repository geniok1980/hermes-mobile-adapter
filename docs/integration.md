# Интеграция в своё приложение

Три шага: получить адрес и токен от владельца агента → подключиться по WebSocket →
отправлять и принимать кадры. Никакого SDK-обязательства нет: протокол — это JSON-кадры,
поэтому подойдёт любая платформа с WebSocket.

## 1. Что нужно от владельца агента

| Что | Откуда | Пример |
|---|---|---|
| Адрес адаптера | `MOBILE_HOST`/`MOBILE_PORT`/`MOBILE_PATH` на машине агента, либо домен через reverse-proxy | `wss://agent.example.com/ws` |
| Токен приложения | `MOBILE_TOKEN` в `~/.hermes/.env` | 64 hex-символа |
| Разрешение устройству | `hermes pairing approve mobile <id>` после первого контакта либо заранее `MOBILE_ALLOWED_USERS=<id пользователя>`; отдельный фильтр устройств — `MOBILE_ALLOWED_DEVICES` | `hermes pairing approve mobile 84b9042da9357a03` |

Идентификатор устройства (`device.id`) выбирает приложение и держит его постоянным: от него
зависит идентификатор сессии в Hermes (`mobile:<device.id>`), адрес для cron-доставки и
записи в allowlist. Один пользователь может иметь несколько устройств — это разные сессии.

## 2. Подключение и жизненный цикл

```
connect() → отправляем hello{token, device} → ждём ready{session, avatar_url, capabilities}
    ↓
текст/голос/медиа вверх (text/audio/media) ⟷ ответы вниз (message, prompt, media, typing, avatar, error)
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
анимация аватара, «агент печатает», отключение кнопки отправки. Кадр `typing` с `stop: true`
означает «выключить индикатор немедленно» (агент закончил) — обработайте его так же, как
финал ответа.

## 4. Кнопки вместо текста (карточки-вопросы)

Иногда агент не отвечает текстом, а просит выбор: одобрить опасную команду, подтвердить
слэш-команду или уточнить запрос. Приходит кадр `prompt` с готовыми кнопками:

```json
{"t":"prompt","id":"5f6a…","kind":"clarify","text":"Какой формат нужен?","options":[{"id":"c0","label":"CSV","style":""},{"id":"c1","label":"JSON","style":""},{"id":"other","label":"✏️ Свой ответ","style":""}],"session":"mobile:tel-1","timeout_seconds":300}
```

Что делает приложение:

1. Показывает `text` и кнопки из `options` (`label` — подпись, `style` — подсказка
   оформления: `primary`/`danger`/пусто).
2. На нажатие отправляет **один кадр** `prompt` с `id` карточки и `option_id`:

```json
{"t":"prompt","prompt_id":"5f6a…","option_id":"c1"}
```

3. Ждёт `prompt_done`. Если он пришёл с `expired: true` — карточка истекла (по умолчанию
   через 300 с), просто закройте её.

Особенности:

* Ответ на карточку **не становится сообщением в чате** — адаптер резолвит его штатными
  механизмами Hermes (одобрение / подтверждение / уточнение). Поэтому не дублируйте выбор
  отдельным `text`.
* Для `clarify` вариант `other` означает «пользователь введёт свой ответ»: отправьте кадр
  с `option_id: "other"` и полем `text`, либо сразу `{"t":"prompt","prompt_id":"…","text":"…"}`
  без `option_id`.
* Не подставляйте свой `option_id` наугад: сервер сам заменит недопустимое значение на
  безопасное (`deny` для одобрения, `cancel` для подтверждения) и вернёт фактический выбор
  в `prompt_done.option_id`.
* Если карточка уже истекла/неизвестна, ответ не сломает соединение: придёт
  `prompt_done{expired:true}`.

## 5. Медиа

### Отправка медиа агенту

Один файл — один или несколько кадров `media`. Для файла ≤ 8 МБ хватит одного кадра
(`parts: 1`); крупнее — шлите кусками по ~3 МБ (`part: 0,1,2…`), адаптер соберёт их и
отдаст агенту целый файл.

```json
{"t":"media","id":"m1","kind":"image","mime":"image/jpeg","filename":"photo.jpg","caption":"Смотри","data":"<base64>","part":0,"parts":1}
```

```kotlin
// Kotlin: отправка фото одним кадром
val bytes = File(path).readBytes()
ws.send(JSONObject(mapOf(
  "t" to "media", "id" to mid, "kind" to "image",
  "mime" to "image/jpeg", "filename" to "photo.jpg",
  "caption" to "Смотри", "data" to Base64.encodeToString(bytes, Base64.NO_WRAP),
  "part" to 0, "parts" to 1
)).toString())
```

```dart
// Dart: отправка кусками
final bytes = await File(path).readAsBytes();
const chunk = 3 * 1024 * 1024;
final parts = (bytes.length / chunk).ceil();
for (var i = 0; i < parts; i++) {
  final slice = bytes.sublist(i * chunk, min((i + 1) * chunk, bytes.length));
  channel.sink.add(jsonEncode({'t': 'media', 'id': mid, 'kind': 'document',
    'mime': 'application/pdf', 'filename': 'report.pdf',
    'data': base64Encode(slice), 'part': i, 'parts': parts}));
}
```

Правила: `kind` — `image` / `video` / `document` / `audio`; досылайте **все** `part` до
`parts-1` (серия без последнего куска не собирается и агенту не уходит); `id` держите
постоянным для всех кусков одного файла. Если в приведённом выше примере `parts` для
документа больше 1, финальный кусок имеет `part == parts - 1`.

### Приём медиа от агента

Агент может прислать картинку, голос, видео, файл или анимацию — всё это кадры `media`:

```json
{"t":"media","id":"a1","kind":"image","mime":"image/png","filename":"chart.png","caption":"График","part":0,"parts":1,"final":true,"size":20480,"data":"<base64>"}
```

```swift
// Swift: приём и сохранение файла на диск
func onMedia(_ f: [String: Any]) {
  guard let id = f["id"] as? String, let data = f["data"] as? String,
        let chunk = Data(base64Encoded: data) else { return }
  var buffer = self.mediaBuffers[id] ?? Data()
  buffer.append(chunk); self.mediaBuffers[id] = buffer
  if f["final"] as? Bool == true {
    let name = (f["filename"] as? String) ?? "file"
    try? buffer.write(to: docDir.appendingPathComponent(name))
    self.mediaBuffers[id] = nil; self.showMedia(name, f["caption"] as? String)
  }
}
```

Правила: собирайте куски по `id`; файл готов, когда пришёл кадр с `final: true`; если у кадра
есть `url`, файла в `data` может не быть — скачайте по ссылке; ориентируйтесь на `mime` и
`filename` при сохранении. Лимит одного файла — 8 МБ (`max_media_bytes` из `capabilities`).

## 6. Потоковая выдача ответа

Если на стороне агента включён стриминг, ответ сначала приходит целиком, а потом **правится
по мере генерации**. Признак правки — поле `edit: true` в кадре `message` с тем же `id`:

```json
{"t":"message","id":"r1","text":"Считаю","edit":true,"chunk":{"index":0,"total":1},"final":false}
{"t":"message","id":"r1","text":"Считаю ответ…","edit":true,"chunk":{"index":0,"total":1},"final":true}
```

Приложение хранит сообщения по `id`: при `edit: true` оно **перерисовывает** существующее
сообщение, а не добавляет новое. `final: true` в правке — конец. Обрабатывать `edit`
необязательно: если его игнорировать, кадры покажутся как новые сообщения (будут дубли) —
поэтому для аккуратного вида лучше перерисовывать по `id`.

Стриминг и индикатор «печатает» независимы: пока генерация идёт, аватар в `thinking`;
после последней правки приходит `typing{stop:true}` и через паузу `avatar:idle`.

## 7. Как не сломаться на неизвестных кадрах

Протокол расширяется только за счёт **необязательных** полей и новых типов кадров, поэтому:

* Разбирайте кадр по полю `t`; неизвестный `t` — молча игнорируйте (не рвите соединение).
* Не считайте поля обязательными, если они помечены как необязательные (`edit`, `stop`,
  `url`, `reply_to`, `caption` и т.п.) — проверяйте их наличие.
* Не падайте на незнакомом `kind` карточки или медиа: покажите общий вид и ждите
  `prompt_done`/следующий кадр.
* `capabilities` в `ready` — источник истины: если поля нет или оно `false`, соответствующую
  возможность просто не используйте.
* Ошибку `unsupported_frame` показывает сервер, когда он не понял **ваш** кадр; для своих
  новых кадров это сигнал обновиться.

## 8. Голос

1. Запишите WAV: 16 кГц, моно, PCM 16 бит (так делает `react-native-live-audio-stream`;
   на iOS/Android то же самое даёт `AVAudioRecorder`/`MediaRecorder`).
2. Отправьте `audio` с `format: "wav"` и `data: base64`.
3. Если у вас есть собственное распознавание — положите текст в `text`: агент получит и
   текст, и аудиофайл. Если текста нет, Hermes распознает речь сам (при настроенном STT на
   машине агента).

Ограничения: аудио ≤ 8 МБ (примерно несколько минут речи), кадр целиком ≤ 16 МБ. Для длинных
записей отправляйте несколько сообщений. Голосовое от агента приходит кадром `media` с
`kind: "voice"` — сохраните и проиграйте его.

Озвучка ответа — на стороне приложения (TTS устройства) или аватара: SDK отдаёт текст,
дальше решаете вы.

## 9. Аватар

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

## 10. Скетчи для разных платформ

Общая часть везде одна: WebSocket + JSON. Ниже — минимальный «скелет» подключения, а затем
обработка новых кадров (`prompt`, `media`, `edit`, `typing.stop`).

**Kotlin (Android, OkHttp)**

```kotlin
val ws = OkHttpClient().newWebSocket(
  Request.Builder().url("$wsUrl?token=$token").build(),
  object : WebSocketListener() {
    override fun onOpen(webSocket: WebSocket?, response: Response?) {
      webSocket?.send("""{"t":"hello","token":"$token","device":{"id":"android-1","name":"Телефон","platform":"android"}}""")
    }
    override fun onMessage(webSocket: WebSocket?, text: String) {
      val f = JSONObject(text)
      when (f.getString("t")) {
        "ready" -> session = f.getString("session")
        "message" -> if (f.optBoolean("edit")) editMessage(f.getString("id"), f.getString("text"))
                     else append(f.getString("id"), f.getString("text"), f.getBoolean("final"))
        "avatar" -> avatarState = f.getString("state")
      }
    }
    override fun onClosing(code: Int, reason: String?) { if (code == 1008) showNoAccess() }
  }
)
```

Новые кадры в том же `onMessage` (Kotlin):

```kotlin
"prompt" -> showButtons(f.getString("id"), f.getJSONArray("options"))
"media"  -> saveMedia(f.getString("id"), f.getString("kind"), f.optString("filename"),
                      f.optString("data"), f.getBoolean("final"))
"typing" -> if (f.optBoolean("stop")) hideTyping() else showTyping()
// ответ на карточку: ws.send("""{"t":"prompt","prompt_id":"$pid","option_id":"$opt"}""")
```

**Swift (iOS, URLSessionWebSocketTask)**

```swift
let task = URLSession.shared.webSocketTask(with: URL(string: "\(wsUrl)?token=\(token)")!)
task.resume()
task.send(.string(#"{"t":"hello","token":"\#(token)","device":{"id":"ios-1","name":"iPhone","platform":"ios"}}"#))
// далее task.receive { ... } в цикле, разбор JSON по полю "t"
```

Обработка новых кадров (Swift):

```swift
func onFrame(_ raw: String) {
  guard let f = try? JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any],
        let t = f["t"] as? String else { return }
  switch t {
  case "message": (f["edit"] as? Bool) == true
      ? editMessage(f["id"] as? String, text: f["text"] as? String)
      : append(f["id"] as? String, text: f["text"] as? String, final: f["final"] as? Bool ?? false)
  case "prompt": showButtons(id: f["id"] as? String, options: f["options"] as? [[String: Any]] ?? [])
  case "media":  onMedia(f)   // собрать по id, сохранить на final (см. раздел «Медиа»)
  case "typing": (f["stop"] as? Bool) == true ? hideTyping() : showTyping()
  default: break
  }
}
// ответ на карточку:
// task.send(.string(#"{"t":"prompt","prompt_id":"\#(pid)","option_id":"\#(opt)"}"#))
```

**Dart / Flutter (web_socket_channel)**

```dart
final channel = WebSocketChannel.connect(Uri.parse('$wsUrl?token=$token'));
channel.sink.add(jsonEncode({'t': 'hello', 'token': token, 'device': {'id': 'flutter-1', 'name': 'Телефон'}}));
channel.stream.listen((raw) {
  final f = jsonDecode(raw as String) as Map<String, dynamic>;
  switch (f['t']) {
    case 'message':
      f['edit'] == true ? editMessage(f['id'], f['text']) : addMessage(f['id'], f['text'], f['final']);
    case 'prompt':
      showButtons(f['id'], (f['options'] as List).cast<Map>());
    case 'typing':
      f['stop'] == true ? hideTyping() : showTyping();
  }
});
// ответ на карточку: channel.sink.add(jsonEncode({'t': 'prompt', 'prompt_id': pid, 'option_id': opt}));
// медиа от агента: собрать data по id, сохранить при final == true (см. раздел «Медиа»)
```

## 11. Диагностика

| Симптом | Причина |
|---|---|
| `auth_failed` / закрытие 1008 | токен не совпадает с `MOBILE_TOKEN`, либо `device.id` отсутствует в `MOBILE_ALLOWED_DEVICES`, либо пользователь ещё не одобрен Hermes (`MOBILE_ALLOWED_USERS` / pairing / `MOBILE_ALLOW_ALL_USERS=1`) |
| `ready` приходит, ответов нет | устройство ещё не одобрено: `hermes pairing list` → `approve`; на первом контакте агент отвечает pairing-кодом |
| Ответ «No home channel is set» | штатное напоминание Hermes: отправьте `/sethome`, чтобы этот чат стал домашним (туда уходят cron-сообщения) |
| `device_offline` в cron-доставке | приложение не подключено: мобильная доставка требует живого соединения |
| `too_big` при медиа | файл больше 8 МБ или кадр больше 16 МБ — шлите кусками (`part`/`parts`) или сожмите |
| ответ на карточку «проглочен» | карточка истекла (`prompt_done{expired:true}`) или `prompt_id` не тот |
| «кадр не пришёл» при подключении извне | порт адаптера не открыт наружу или не проксируется через Caddy — см. `docs/adapter-install.md` |

Проверить руками: `wscat -c "ws://<хост>:8787/ws?token=<токен>"`, затем отправить
`{"t":"hello","device":{"id":"cli"}}`.

## 12. Безопасность

* Токен — единственный секрет приложения: храните его в Keychain/Keystore, не в открытом
  коде и не в логи.
* Наружу — только `wss://` (TLS на reverse-proxy). `ws://` допустим в локальной сети или
  через `adb reverse tcp:8787 tcp:8787` при разработке.
* Одно приложение — один токен (`MOBILE_EXTRA_TOKENS` позволяет их различить). Отзыв токена
  = убрать его из `.env` и перезапустить шлюз.
* Ответ на карточку-вопрос валидируется на сервере: недопустимый `option_id` превращается в
  безопасное значение (`deny`/`cancel`), «подделать» одобрение командой клиента нельзя.
* Полезная нагрузка (текст, медиа) — часть диалога с агентом: не отправляйте через протокол
  данные, которые агент не должен видеть.
