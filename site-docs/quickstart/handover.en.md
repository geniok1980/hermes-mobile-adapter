# Connecting an app to Hermes (for the developer)

A short summary: what you need from the agent owner, how to connect, and what to implement.
The full protocol description — [`Protocol v1`](../protocol/index.md), a practical guide —
[`How to embed it`](../guides/integration.md).

## 1. What you receive from the agent owner

| What | Example | Why |
|---|---|---|
| Adapter address | `wss://agent.example.com/mobile-ws` | the connection point (WebSocket) |
| App token | 64 hex characters | connection authorization |
| User identifier | `dev-pilot` | the user you are seen as in Hermes |
| (optional) device id | `pilot-laptop` | your own session and cron delivery address |

The token is the only secret. Keep it in the Keychain/Keystore, not in code and not in logs;
for a public app every device needs its **own** token (the agent owner adds it to
`MOBILE_EXTRA_TOKENS` — including as `device_id=token`, binding the token to a device — or
issues it via pairing) — one shared token in the app build means that anyone who gets the APK
will gain access to the agent.

## 2. A one-minute check (without code)

Open `examples/web/index.html`, enter the address and token, click "Connect" and send a
message. If the agent replied — the link works, and from here it is up to the app.

## 3. Connecting in the app

```bash
npm i hermes-mobile-client
```

```ts
import { HermesClient } from 'hermes-mobile-client';

const client = new HermesClient({
  url: 'wss://agent.example.com/mobile-ws',
  token: '<token>',
  device: { id: 'my-app-1', name: 'Phone', platform: 'android', appVersion: '1.0.0' },
  user: { id: 'dev-pilot', name: 'Developer' },   // the id agreed with the agent owner
  handlers: {
    onReady: (ready) => console.log('session', ready.session, ready.capabilities),
    onMessage: (m) => addToChat(m.text, m.final),
    onMessageEdit: (m) => replaceById(m.id, m.text),   // streaming output
    onPrompt: (p) => showButtons(p.text, p.options),   // command approval / clarify
    onMedia: (media) => saveAndShow(media),
    onAvatar: (state) => avatar.setState(state),
    onTyping: () => showTyping(),
    onError: (e) => show(e.message),
  },
});
client.connect();
client.sendText('hi');
```

Other runtimes (Flutter/Dart, Swift, Kotlin) — follow the sketches in [`How to embed it`](../guides/integration.md)
and the ready-made guides in the site's "Guides" section;
bots and server-side integrations — the Python client `sdk/python/hermes_client.py`.

## 4. What to implement in the interface (by priority)

1. **Feed and input** — the required minimum.
2. **Card buttons** (`prompt` → `answerPrompt(id, optionId)`): approving dangerous commands,
   clarifying questions, confirming slash commands. Without this the agent will ask in text and
   the user will have to type `/approve`.
3. **Media in both directions**: `sendImage/sendDocument/sendVoice` (the client itself splits large files
   into chunks) and saving incoming `media` frames.
4. **Streaming**: when `message.edit = true`, redraw the message with the same `id`.
5. **Avatar** (optional): a WebView with a page implementing
   `window.hermesAvatar.setState/speak/setVolume`; the URL arrives in the `ready` frame (`avatar_url`).
6. **Voice**: record WAV 16 kHz mono → `sendVoice(base64, 'wav')`.

## 5. Rules that must not be broken

* The first frame must be `hello` only; otherwise the connection closes.
* Close code **1008** — access denied (token or device/user not allowed).
  Retrying is pointless.
* A message `id` is constant for each message: on a retry after a network drop the adapter
  will ignore it (duplicate protection).
* Ignore unfamiliar frames and fields — the protocol only ever extends by additions.
* Limits: message ≤ 12,000 characters, file ≤ 8 MB, frame ≤ 16 MB.

## 6. What happens on the Hermes side

Your app lands in the same sessions, memory and tools of the agent as the other channels
(Telegram, CLI): history is stored in Hermes, not in the app, cron can deliver
messages and files to the device (`deliver=mobile:<device>`), and user decisions
(approving commands, answering questions) are applied by Hermes' native mechanisms.
