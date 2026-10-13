# Integrating into your app

Three steps: get the address and token from the agent owner → connect over WebSocket →
send and receive frames. There is no SDK obligation: the protocol is JSON frames,
so any platform with WebSocket will work.

## 1. What you need from the agent owner

| What | Where | Example |
|---|---|---|
| Adapter address | `MOBILE_HOST`/`MOBILE_PORT`/`MOBILE_PATH` on the agent machine, or a domain via reverse-proxy | `wss://agent.example.com/ws` |
| App token | `MOBILE_TOKEN` in `~/.hermes/.env` | 64 hex characters |
| Device approval | `hermes pairing approve mobile <id>` after first contact, or `MOBILE_ALLOWED_USERS=<user id>` in advance; a separate device filter is `MOBILE_ALLOWED_DEVICES` | `hermes pairing approve mobile 84b9042da9357a03` |

The device identifier (`device.id`) is chosen by the app and must stay constant: the session
identifier in Hermes (`mobile:<device.id>`), the cron delivery address, and the allowlist
entry all depend on it. One user may have several devices — these are separate sessions.

## 2. Connection and lifecycle

```
connect() → send hello{token, device} → wait for ready{session, avatar_url, capabilities}
    ↓
text/voice/media up (text/audio/media) ⟷ replies down (message, prompt, media, typing, avatar, error)
    ↓
network drop → reconnect after a delay → hello again (history and session are preserved on the Hermes side)
```

Rules to follow:

* **The first frame must be `hello` only.** Otherwise the adapter sends `error` and closes the connection.
* **Close code 1008** — access denied (wrong token or device not in the list).
  Retrying is pointless: show the user "no access" and don't hammer the server.
* **Set a stable message `id`** for each message and reuse it when resending after a
  drop: the adapter ignores duplicates (protection against double messages).
* **Ignore unknown frames** — that is how the protocol is extended without breaking.
* **Drops are normal** (mobile network, device sleep). Reconnect with
  exponential backoff (built into the TS SDK).

## 3. Agent replies and "typing"

A long reply arrives as several `message` frames with the same `id` and a
`chunk: {index, total}` field; the last frame has `final: true`. Concatenate by `id` — that way the
reply looks like a single message. Until `final` arrives, show an indicator.

`avatar` frames (`thinking` / `speaking` / `idle`) and `typing` are UI signals:
avatar animation, "agent is typing", disabling the send button. A `typing` frame with `stop: true`
means "turn off the indicator immediately" (the agent is done) — handle it the same way as the
final reply.

## 4. Buttons instead of text (question cards)

Sometimes the agent doesn't reply with text but asks for a choice: approve a dangerous command, confirm
a slash command, or clarify a request. A `prompt` frame arrives with ready-made buttons:

```json
{"t":"prompt","id":"5f6a…","kind":"clarify","text":"Какой формат нужен?","options":[{"id":"c0","label":"CSV","style":""},{"id":"c1","label":"JSON","style":""},{"id":"other","label":"✏️ Свой ответ","style":""}],"session":"mobile:tel-1","timeout_seconds":300}
```

What the app does:

1. Shows `text` and the buttons from `options` (`label` is the caption, `style` is a
   presentation hint: `primary`/`danger`/empty). For `exec_approval` the server also sends the fields **`command`**
   (the exact command) and **`description`** — show the command next to the buttons.
2. On a tap it sends **a single frame** `prompt` with the card `id` and `option_id`:

```json
{"t":"prompt","prompt_id":"5f6a…","option_id":"c1"}
```

3. Waits for `prompt_done`. If it arrives with `expired: true` — the card expired (by default
   after 300 s), just close it.

Specifics:

* Answering a card **does not become a chat message** — the adapter resolves it through Hermes's standard
  mechanisms (approval / confirmation / clarification). So don't duplicate the choice with a separate `text`.
* For `clarify`, the `other` option means "the user will enter their own answer": send a frame
  with `option_id: "other"` and a `text` field, or straight away `{"t":"prompt","prompt_id":"…","text":"…"}`
  without `option_id`.
* Don't invent your own `option_id`: the server replaces an invalid value with
  a safe one (`deny` for approval, `cancel` for confirmation) and returns the actual choice
  in `prompt_done.option_id`.
* If the card has already expired or is unknown, the answer won't break the connection:
  you'll get `prompt_done{expired:true}`.
* A card can be closed **only by the device it was shown to**: an answer from another
  connection gets `error{code:"forbidden"}`.

## 5. Media

### Sending media to the agent

One file — one or more `media` frames. For a file ≤ 8 MB a single frame is enough
(`parts: 1`); for larger ones send chunks of ~3 MB (`part: 0,1,2…`), the adapter assembles them and
hands the agent a complete file.

```json
{"t":"media","id":"m1","kind":"image","mime":"image/jpeg","filename":"photo.jpg","caption":"Смотри","data":"<base64>","part":0,"parts":1}
```

```kotlin
// Kotlin: sending a photo in a single frame
val bytes = File(path).readBytes()
ws.send(JSONObject(mapOf(
  "t" to "media", "id" to mid, "kind" to "image",
  "mime" to "image/jpeg", "filename" to "photo.jpg",
  "caption" to "Смотри", "data" to Base64.encodeToString(bytes, Base64.NO_WRAP),
  "part" to 0, "parts" to 1
)).toString())
```

```dart
// Dart: sending in chunks
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

Rules: `kind` is `image` / `video` / `document` / `audio`; send **all** `part`s up to
`parts-1` (a series without the last chunk isn't assembled and isn't delivered to the agent); keep
`id` constant across all chunks of one file. If in the example above `parts` for the
document is greater than 1, the final chunk has `part == parts - 1`.

### Receiving media from the agent

The agent can send a picture, voice, video, file or animation — all of these are `media` frames:

```json
{"t":"media","id":"a1","kind":"image","mime":"image/png","filename":"chart.png","caption":"График","part":0,"parts":1,"final":true,"size":20480,"data":"<base64>"}
```

```swift
// Swift: receiving a file and saving it to disk
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

Rules: assemble chunks by `id`; the file is ready when a frame with `final: true` arrives; if a frame
has a `url`, there may be no file in `data` — download it via the link; use `mime` and
`filename` when saving. The single-file limit is 8 MB (`max_media_bytes` from `capabilities`).

## 6. Streaming the reply

If streaming is enabled on the agent side, the reply first arrives in full and is then **edited
as it is generated**. The edit marker is the `edit: true` field in a `message` frame with the same `id`:

```json
{"t":"message","id":"r1","text":"Считаю","edit":true,"chunk":{"index":0,"total":1},"final":false}
{"t":"message","id":"r1","text":"Считаю ответ…","edit":true,"chunk":{"index":0,"total":1},"final":true}
```

The app stores messages by `id`: on `edit: true` it **redraws** the existing
message instead of adding a new one. `final: true` in an edit means the end. Handling `edit`
is optional: if you ignore it, the frames appear as new messages (you get duplicates) —
so for a clean look it's better to redraw by `id`.

Streaming and the "typing" indicator are independent: while generation is running, the avatar is in `thinking`;
after the last edit a `typing{stop:true}` arrives and, after a pause, `avatar:idle`.

## 7. How not to break on unknown frames

The protocol is extended only via **optional** fields and new frame types, so:

* Parse a frame by its `t` field; an unknown `t` — silently ignore it (don't drop the connection).
* Don't treat fields as required if they're marked optional (`edit`, `stop`,
  `url`, `reply_to`, `caption`, etc.) — check whether they're present.
* Don't crash on an unfamiliar `kind` of a card or media: show a generic view and wait for
  `prompt_done`/the next frame.
* `capabilities` in `ready` is the source of truth: if a field is absent or `false`, just don't use
  the corresponding capability.
* The `unsupported_frame` error is shown by the server when it didn't understand **your** frame; for your
  own new frames it's a signal to upgrade.

## 8. Voice

1. Record a WAV: 16 kHz, mono, PCM 16 bit (that's what `react-native-live-audio-stream` does;
   on iOS/Android `AVAudioRecorder`/`MediaRecorder` does the same).
2. Send `audio` with `format: "wav"` and `data: base64`.
3. If you have your own recognition — put the text in `text`: the agent will get both
   the text and the audio file. If there's no text, Hermes recognizes the speech itself (given STT is
   configured on the agent machine).

Limits: audio ≤ 8 MB (roughly a few minutes of speech), a whole frame ≤ 16 MB. For long
recordings send several messages. Voice from the agent arrives as a `media` frame with
`kind: "voice"` — save it and play it.

Playing back the reply is on the app side (the device's TTS) or the avatar: the SDK returns the text,
the rest is up to you.

## 9. Avatar

The avatar is a regular HTML page in a WebView that implements the contract:

```js
window.hermesAvatar = {
  setState: (state) => {},   // 'idle' | 'thinking' | 'speaking'
  speak:    (text) => {},    // ask to voice/show the agent's line
  setVolume:(level) => {},   // microphone level while recording, 0..1
};
```

The page URL arrives in the `ready` frame (`avatar_url`) — set by the
`MOBILE_AVATAR_URL` variable on the agent machine. If the URL isn't set, use your own page
(in `examples/react-native/avatarHtml.ts` there's a minimal one with the same contract).

Calls from the app:

```ts
webView.injectJavaScript(`window.hermesAvatar.setState('speaking'); true;`);
```

## 10. Sketches for different platforms

The common part is the same everywhere: WebSocket + JSON. Below is a minimal connection
"skeleton", then handling of new frames (`prompt`, `media`, `edit`, `typing.stop`).

**Kotlin (Android, OkHttp)**

```kotlin
val ws = OkHttpClient().newWebSocket(
  Request.Builder().url(wsUrl).build(),
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

New frames in the same `onMessage` (Kotlin):

```kotlin
"prompt" -> showButtons(f.getString("id"), f.getJSONArray("options"))
"media"  -> saveMedia(f.getString("id"), f.getString("kind"), f.optString("filename"),
                      f.optString("data"), f.getBoolean("final"))
"typing" -> if (f.optBoolean("stop")) hideTyping() else showTyping()
// answering a card: ws.send("""{"t":"prompt","prompt_id":"$pid","option_id":"$opt"}""")
```

**Swift (iOS, URLSessionWebSocketTask)**

```swift
let task = URLSession.shared.webSocketTask(with: URL(string: wsUrl)!)
task.resume()
task.send(.string(#"{"t":"hello","token":"\#(token)","device":{"id":"ios-1","name":"iPhone","platform":"ios"}}"#))
// then task.receive { ... } in a loop, parsing JSON by the "t" field
```

Handling new frames (Swift):

```swift
func onFrame(_ raw: String) {
  guard let f = try? JSONSerialization.jsonObject(with: Data(raw.utf8)) as? [String: Any],
        let t = f["t"] as? String else { return }
  switch t {
  case "message": (f["edit"] as? Bool) == true
      ? editMessage(f["id"] as? String, text: f["text"] as? String)
      : append(f["id"] as? String, text: f["text"] as? String, final: f["final"] as? Bool ?? false)
  case "prompt": showButtons(id: f["id"] as? String, options: f["options"] as? [[String: Any]] ?? [])
  case "media":  onMedia(f)   // assemble by id, save on final (see the "Media" section)
  case "typing": (f["stop"] as? Bool) == true ? hideTyping() : showTyping()
  default: break
  }
}
// answering a card:
// task.send(.string(#"{"t":"prompt","prompt_id":"\#(pid)","option_id":"\#(opt)"}"#))
```

**Dart / Flutter (web_socket_channel)**

```dart
final channel = WebSocketChannel.connect(Uri.parse(wsUrl));
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
// answering a card: channel.sink.add(jsonEncode({'t': 'prompt', 'prompt_id': pid, 'option_id': opt}));
// media from the agent: assemble data by id, save when final == true (see the "Media" section)
```

## 11. Troubleshooting

| Symptom | Cause |
|---|---|
| `auth_failed` / close 1008 | the token doesn't match `MOBILE_TOKEN`, or `device.id` is missing from `MOBILE_ALLOWED_DEVICES`, or the user hasn't been approved by Hermes yet (`MOBILE_ALLOWED_USERS` / pairing / `MOBILE_ALLOW_ALL_USERS=1`) |
| `ready` arrives, but there are no replies | the device isn't approved yet: `hermes pairing list` → `approve`; on first contact the agent replies with a pairing code |
| Reply "No home channel is set" | a standard Hermes reminder: send `/sethome` so this chat becomes the home channel (that's where cron messages go) |
| `device_offline` in cron delivery | the app isn't connected: mobile delivery requires a live connection |
| `too_big` on media | the file is larger than 8 MB or the frame is larger than 16 MB — send it in chunks (`part`/`parts`) or compress it |
| `too_big` / `bad_frame` on a chunk series | more than 8 unfinished uploads per device, the device's total buffer > 8 MB, or `parts` > 2048: wait for the uploads to finish, don't cut smaller than 4 KiB per chunk (buffers are freed on disconnect) |
| `forbidden` | someone else's `device.id` (the token is bound to another device), an answer to a card of another device, or `deliver` that isn't from cron: connect under your own `device.id`; `deliver` is sent by cron (loopback) or a token from `MOBILE_DELIVER_TOKENS` |
| the answer to a card is "swallowed" | the card expired (`prompt_done{expired:true}`) or the `prompt_id` is wrong |
| "frame didn't arrive" when connecting from outside | the adapter port isn't exposed or isn't proxied through Caddy — see `../../host/install.md` |

Manual check: `wscat -c "ws://127.0.0.1:8787/ws"`, then send as the first frame
`{"t":"hello","token":"<token>","device":{"id":"cli"}}` (a token in the URL `?token=` is not accepted).

## 12. Security

* The token is the app's only secret: store it in Keychain/Keystore, not in open
  source and not in logs.
* Externally — only `wss://` (TLS at the reverse-proxy). `ws://` is acceptable on a local network or
  via `adb reverse tcp:8787 tcp:8787` during development.
* One app — one token (`MOBILE_EXTRA_TOKENS` lets you tell them apart). Revoking a token
  = remove it from `.env` and restart the gateway.
* The answer to a question card is validated on the server: an invalid `option_id` is turned into a
  safe value (`deny`/`cancel`), so an approval can't be "forged" by the client's choice.
* The payload (text, media) is part of the dialogue with the agent: don't send data through the protocol
  that the agent shouldn't see.
