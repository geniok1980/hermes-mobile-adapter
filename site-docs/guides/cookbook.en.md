# Cookbook

Twelve "how to do X" scenarios. The code is short and working; platform-independent frames are
the same JSON in any runtime. Each one comes with a pitfall.

## 1. Send a photo with a caption

```js
// Any client: build the media frame as a single chunk (file ≤ 8 MB → parts: 1).
send({
  t: 'media', id: 'm-photo-' + Date.now(), kind: 'image',
  mime: 'image/jpeg', filename: 'photo.jpg', caption: 'Смотри на график',
  data: base64Bytes, part: 0, parts: 1,
});
```

**Pitfall:** `caption` is a caption, not a separate message. Do not send a nearby
`text` with the same meaning — the agent will see both the caption and the text as different messages. For `kind: "audio"`
`caption` works as the recognized text.

## 2. Send a document in chunks

```js
// A file > 8 MB does not fit in one frame: split into ~3 MB chunks, a shared id, send ALL chunks.
const id = 'm-doc-' + Date.now();
const CHUNK = 3 * 1024 * 1024;
const parts = Math.max(1, Math.ceil(bytes.length / CHUNK));
for (let i = 0; i < parts; i++) {
  const slice = bytes.slice(i * CHUNK, (i + 1) * CHUNK);
  send({ t: 'media', id, kind: 'document', mime: 'application/pdf', filename: 'отчёт.pdf',
         data: toBase64(slice), part: i, parts });
}
```

**Pitfall:** the adapter assembles the file only when **all** chunks up to `parts-1` have arrived.
Break the series and the file will not reach the agent at all (`too_big`/silence). The `id` of all chunks is the same.

## 3. Receive a file from the agent and save it

```ts
// React Native / Expo
onMedia: async (m) => {
  const name = m.filename || `${m.id}.bin`;
  const path = `${FileSystem.documentDirectory}hermes-media/${name}`;
  await FileSystem.makeDirectoryAsync(`${FileSystem.documentDirectory}hermes-media/`, { intermediates: true });
  if (m.data) await FileSystem.writeAsStringAsync(path, m.data, { encoding: FileSystem.EncodingType.Base64 });
  else await FileSystem.downloadAsync(m.url!, path);   // no data — download by link
}
```

**Pitfall:** the SDK has already assembled the chunks (`part`/`parts`) — the callback is called **once per
whole file**, no need to glue them manually. But if `data` is empty, the frame has only `url`
(the file is not available on disk) — download it yourself.

## 4. Show an approval card and answer it

```js
case 'prompt': {
  // options: [{id:'once',label:'Approve once',style:'primary'}, {id:'deny',label:'Decline',style:'danger'}, ...]
  // for exec_approval: show data.command next to the buttons (data.description is the explanation)
  showCard(data.id, data.text, data.command, data.description, data.options);
  break;
}
// on button press:
send({ t: 'prompt', prompt_id: promptId, option_id: 'once' });
```

**Pitfall:** the answer to a card **does not become a message in the chat** — do not duplicate
the choice with a separate `text`. Values strictly by type: `exec_approval` → `once|session|always|deny`;
`slash_confirm` → `once|always|cancel`. The server will replace an unrecognized one with a safe value
(`deny`/`cancel`), but you should not rely on that — send a correct one. **Only the device it is shown to**
can close the card (an answer from another connection → `forbidden`).

## 5. Handle a clarify answer with text (`option id: other`)

```js
// kind: 'clarify', options end with {id:'other', label:'✏️ Your own answer'}
if (option.id === 'other') {
  const text = await promptUserForText();      // your input
  if (!text.trim()) return;
  send({ t: 'prompt', prompt_id: data.id, option_id: 'other', text });   // text is REQUIRED
} else {
  send({ t: 'prompt', prompt_id: data.id, option_id: option.id });       // c0, c1, …
}
```

**Pitfall:** `option_id: "other"` **without** a non-empty `text` puts the question into
text-waiting mode — the card will "hang". Alternative: `{t:'prompt', prompt_id, text}` without
`option_id` at all.

## 6. Redraw a message while streaming

```js
case 'message': {
  const node = document.querySelector(`[data-msg="${CSS.escape(data.id)}"]`);
  if (data.edit) {                       // editing the same message
    if (node) node.textContent = data.text;
    else { const n = log(data.text); n.dataset.msg = data.id; }
  } else if (node) {                      // long reply — chunks of one id
    node.textContent += data.text;
  } else {
    const n = log(data.text); n.dataset.msg = data.id;
  }
  break;
}
```

**Pitfall:** if you ignore `edit`, each streaming frame will appear as a **new**
message — you will get duplicates of the growing text. Redraw the bubble by `id`. `final: true` in
the last edit is the end.

## 7. Record and send a voice message

```ts
// WAV 16 kHz, mono, PCM 16 bit → base64 in the audio frame.
client.sendAudio(wavBase64, 'wav');                 // format can only be 'wav' or 'ogg'
client.sendAudio(wavBase64, 'wav', 'своё распознавание');  // + optional text
```

**Pitfall:** the format is strictly `wav`/`ogg` — the server will reject `mp3`/`aac`/`m4a` with
`unsupported_frame`. Audio ≤ 8 MB (a few minutes); split a long recording into several `audio` frames.
`media` with `kind:"audio"` is also allowed upward, but for a "voice" the canonical frame is `audio`.

## 8. Restore the connection without duplicating a message

```ts
// Generate the message id once and REPEAT it when resending after a drop.
const id = makeId();            // keep it next to the draft/queue
client.sendText(text, id);
// after reconnect — the same call with the same id:
client.sendText(text, id);      // the adapter ignores a repeat with the same id
```

**Pitfall:** idempotency rests on `id`. If you generate a new `id` when resending,
the agent will receive **two** identical messages. `HermesClient` generates the `id` itself, but on a
manual resend pass the previous one (`sendText(text, messageId)`).

## 9. Receive a message from cron (`deliver`)

```js
// The server→client frame arrives as an ordinary message, but may have no preceding text.
case 'message':
  // No need to distinguish the source: show it as an agent reply.
  // cron messages have their own id; edit is not applied to them.
  upsert(data.id, { role: 'agent', text: data.text });
  break;
```

**Pitfall:** cron delivery requires a **live** connection. If the device is offline, on the
agent's machine `device_offline` will be returned. On the client do nothing special: the same
`message`. The `delivered` frame (server→client `{t,id,device}`) is a service acknowledgement of
standalone delivery, no need to show it to the user. The `deliver` frame is a service frame and
is accepted only from a loopback address (cron on the same machine) or with a token from
`MOBILE_DELIVER_TOKENS`; an ordinary app must not send it — otherwise it will get `forbidden`.

## 10. Do not break on an unknown frame

```js
switch (data.t) {
  case 'ready': /* … */ break;
  // … known types …
  default:
    // A new protocol version sent an unknown frame — silently ignore it.
    console.debug('[hermes] неизвестный кадр', data.t);
    break;
}
```

**Pitfall:** you must not break the connection or throw an exception on an unknown `t`.
The protocol is extended through new **optional** fields and new frame types — a client
that crashes on an unfamiliar frame breaks on the very first gateway update. Same with fields:
do not consider `edit`, `stop`, `caption`, `url`, `reply_to` mandatory.

## 11. Acknowledge displaying a message (`ack`)

```js
// Displayed/played the message — you can acknowledge:
send({ t: 'ack', id: data.id });
```

**Pitfall:** `ack` does not replace `message.final` — these are different things. `final` is about
gluing a long reply; `ack` is about the app having displayed/played it. Send `ack`
only if you actually display it (for analytics/delivery), do not turn it into a mandatory step.

## 12. A ping to check the connection

```js
send({ t: 'ping', ts: Date.now() });   // the server will reply {t:'pong', ts}
```

**Pitfall:** the server returns `ts` as is — use it to measure RTT.
Do not spam `ping`: on mobile networks keep the interval ≥ 30 s; if you cannot get a
`pong` within the timeout, consider the connection dead and reconnect.
