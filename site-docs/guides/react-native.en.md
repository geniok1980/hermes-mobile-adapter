# React Native / Expo

The full path: installing the package, a client with reconnection, a chat screen with feeds,
cards with buttons, receiving and displaying media, editing a bubble during streaming, an avatar in a
WebView, and voice recording. The code is a direct continuation of the
`examples/react-native/HermesChat.tsx` example: everything there is the same, but
here it's assembled into ready-made steps with a discussion of pitfalls. There's no pseudocode — every
fragment compiles given the dependencies are connected.

## 0. What you need from the agent owner

| What | Example |
|---|---|
| Address | `wss://hermes-go.com/mobile-ws` (pilot) or your domain from `MOBILE_HOST`/`MOBILE_PORT`/`MOBILE_PATH` |
| Token | 64 hex characters from `MOBILE_TOKEN` in `~/.hermes/.env` |
| `device.id` | a stable identifier that you choose, e.g. `my-app-1` |

The token is not given in the docs — the agent owner issues it separately. Keep `device.id`
constant: the Hermes session (`mobile:<device.id>`) and the cron delivery address depend on it.

## 1. Installation

```bash
npm i hermes-mobile-client          # protocol client (no runtime dependencies)
npx expo install expo-file-system react-native-webview
npm i react-native-live-audio-stream # voice recording (WAV 16 kHz)
```

`hermes-mobile-client` uses the standard `WebSocket` and works in Expo out of the box.
To save media you need `expo-file-system`; for the avatar — `react-native-webview`; for
voice recording — `react-native-live-audio-stream` (a native module, requires a dev build, not Expo Go).

## 2. Client with reconnection

Reconnection with exponential backoff is already built into `HermesClient` — don't write your own.
The only important thing is to react correctly to access denial (code 1008) and not hammer the server.

```ts
// src/hermes/client.ts
import { HermesClient, type ConnectionState } from 'hermes-mobile-client';

const WS_URL = 'wss://hermes-go.com/mobile-ws';
const TOKEN = process.env.EXPO_PUBLIC_MOBILE_TOKEN ?? ''; // don't hardcode the token into the repository
const DEVICE = { id: 'my-app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' };

export function createClient(handlers: ConstructorParameters<typeof HermesClient>[0]['handlers']) {
  const client = new HermesClient({
    url: WS_URL,
    token: TOKEN,
    device: DEVICE,
    maxBackoffMs: 15000,          // backoff ceiling between attempts
    handlers: {
      ...handlers,
      onState: (state: ConnectionState, detail?: string) => handlers?.onState?.(state, detail),
      onError: (error) => {
        // auth_failed comes with fatal:true — the server closed the connection with code 1008.
        // Retrying is pointless: show the user "no access" and don't reconnect.
        if (error.code === 'auth_failed' || error.fatal) {
          handlers?.onError?.({ ...error, message: 'Нет доступа: проверьте токен приложения' });
          return;
        }
        handlers?.onError?.(error); // transport/offline — not fatal, reconnection is already in progress
      },
    },
  });
  client.connect();
  return client;
}
```

Pitfalls:

* **`auth_failed` occurs after close 1008.** The SDK itself sets the `failed` state and
  doesn't reconnect — your job is to show the text, not to start the retry loop again.
* **The token is a secret.** In Expo read it from `EXPO_PUBLIC_*`/EAS Secrets or `expo-secure-store`;
  don't commit it to `app.json`.
* **`device.id` is unique per device.** Two apps with the same `id` are one Hermes session
  and "jumping" replies.

## 3. Chat screen with feeds

The model is a single `Item` list: user and agent bubbles, cards and media.
The key point is `upsert` by `id`, so that both streaming and redelivery edit the same bubble.

```tsx
// src/screens/ChatScreen.tsx
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FlatList, Image, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { WebView } from 'react-native-webview';
import * as FileSystem from 'expo-file-system';
import type { AgentMessage, AvatarState, MediaPayload, PromptDonePayload, PromptPayload } from 'hermes-mobile-client';
import { HermesClient } from 'hermes-mobile-client';
import { createClient } from '../hermes/client';
import { AVATAR_FALLBACK_HTML } from '../hermes/avatarHtml'; // see examples/react-native/avatarHtml.ts

const MEDIA_DIR = `${FileSystem.documentDirectory}hermes-media/`;

type Role = 'user' | 'agent' | 'card' | 'media';
interface Item {
  id: string; role: Role; text: string;
  kind?: string; options?: PromptPayload['options']; resolved?: string;
  mediaKind?: string; uri?: string; localPath?: string; caption?: string;
}

export function ChatScreen() {
  const webViewRef = useRef<WebView | null>(null);
  const clientRef = useRef<HermesClient | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [avatarState, setAvatarState] = useState<AvatarState>('idle');
  const [connection, setConnection] = useState('idle');
  const [draft, setDraft] = useState('');

  const upsert = useCallback((id: string, patch: Partial<Item> & { role: Role }) => {
    setItems((prev) => {
      const i = prev.findIndex((it) => it.id === id);
      if (i === -1) return [...prev, { id, text: '', ...patch }];
      const next = [...prev];
      next[i] = { ...next[i], ...patch };
      return next;
    });
  }, []);

  useEffect(() => {
    const client = createClient({
      onState: setConnection,
      onAvatar: setAvatarState,
      onTyping: () => setAvatarState('thinking'),
      onTypingStop: () => setAvatarState('idle'),
      // New message: append to the bubble with the same id (a long reply is cut into chunks).
      onMessage: (m: AgentMessage) =>
        setItems((prev) => {
          const i = prev.findIndex((it) => it.id === m.id);
          if (i !== -1) { const next = [...prev]; next[i] = { ...next[i], text: next[i].text + m.text }; return next; }
          return [...prev, { id: m.id, role: 'agent', text: m.text }];
        }),
      // Streaming edit: replace the bubble's text instead of adding a new one.
      onMessageEdit: (m: AgentMessage) => upsert(m.id, { role: 'agent', text: m.text }),
      onPrompt: (p: PromptPayload) =>
        setItems((prev) => [...prev, { id: `card-${p.id}`, role: 'card', text: p.text, kind: p.kind, options: p.options }]),
      onPromptDone: (d: PromptDonePayload) =>
        upsert(`card-${d.id}`, { role: 'card', resolved: d.expired ? 'expired' : d.option_id ?? 'закрыта' }),
      onMedia: (m: MediaPayload) => { void saveMedia(m); },
    });
    clientRef.current = client;
    return () => client.disconnect();
  }, [upsert]);

  // ... saveMedia, answer, send — see below
}
```

## 4. Cards with buttons

A card is a `prompt` frame with ready-made `options`. The answer is **a single** `prompt` frame with `prompt_id`
and `option_id`; you must not duplicate the choice with a separate `text`.

```tsx
function PromptCard({ item, onAnswer }: { item: Item; onAnswer: (it: Item, optionId: string) => void }) {
  return (
    <View style={[styles.card, item.resolved ? styles.cardResolved : null]}>
      <Text style={styles.cardKind}>{item.kind}</Text>
      <Text style={styles.cardText}>{item.text}</Text>
      <View style={styles.options}>
        {(item.options ?? []).map((o) => (
          <Pressable key={o.id} disabled={Boolean(item.resolved)}
            style={[styles.option, o.style === 'danger' ? styles.optionDanger : null]}
            onPress={() => onAnswer(item, o.id)}>
            <Text style={styles.optionLabel}>{o.label || o.id}</Text>
          </Pressable>
        ))}
      </View>
      {item.resolved ? <Text style={styles.meta}>{item.resolved === 'expired' ? 'истекла' : `выбрано: ${item.resolved}`}</Text> : null}
    </View>
  );
}

// answer:
const answer = useCallback((item: Item, optionId: string) => {
  const promptId = item.id.replace(/^card-/, '');
  const client = clientRef.current;
  if (!client) return;
  if (optionId === 'other') {
    // clarify: the "own answer" option requires text — ask the user for it.
    // Here, for brevity: text comes from your input dialog.
    const text = '';               // insert input from the UI
    if (!text.trim()) return;
    client.answerPrompt(promptId, 'other', text);
  } else {
    client.answerPrompt(promptId, optionId);
  }
  upsert(item.id, { role: 'card', text: item.text, resolved: optionId });
}, [upsert]);
```

Pitfall: the `other` option is **required** to carry `text`. If you send `option_id: "other"` without
text — the server puts the card into text-wait mode and it "hangs".

## 5. Receiving and displaying media (expo-file-system)

`media` frames from the agent arrive with `part`/`parts`; the SDK assembles the chunks and calls `onMedia`
once per **whole** file. For images we show a `data:` URL, everything else we save and
hand back a path.

```tsx
const saveMedia = useCallback(async (m: MediaPayload) => {
  const name = m.filename || `${m.id}.bin`;
  const dataUrl = m.data ? `data:${m.mime || 'application/octet-stream'};base64,${m.data}` : m.url;
  let localPath: string | undefined;
  try {
    await FileSystem.makeDirectoryAsync(MEDIA_DIR, { intermediates: true });
    localPath = `${MEDIA_DIR}${name}`;
    if (m.data) {
      await FileSystem.writeAsStringAsync(localPath, m.data, { encoding: FileSystem.EncodingType.Base64 });
    }
  } catch (error) {
    console.warn('[hermes] не удалось сохранить медиа', error);
  }
  setItems((prev) => [...prev, {
    id: `media-${m.id}-${Date.now()}`, role: 'media', text: '',
    mediaKind: m.kind, uri: dataUrl, localPath, caption: m.caption || name,
  }]);
}, []);
```

Pitfalls:

* **`data` may be absent** — then the file is given as a `url` link, download it yourself.
* **Don't store base64 in AsyncStorage** — it bloats storage; write the file via
  `expo-file-system` and keep the path in state.
* Use `mime`/`filename` when choosing the extension.

## 6. Sending text and media

```tsx
const send = useCallback(() => {
  const text = draft.trim();
  if (!text || !clientRef.current) return;
  clientRef.current.sendText(text);              // the id is generated internally and reused on retry
  setItems((prev) => [...prev, { id: `u-${Date.now()}`, role: 'user', text }]);
  setDraft('');
}, [draft]);

// Photo/file: base64 or a path. The SDK itself chunks it into 3 MB pieces.
async function sendPicked(uri: string, filename: string, mime: string, caption?: string) {
  const base64 = await FileSystem.readAsStringAsync(uri, { encoding: FileSystem.EncodingType.Base64 });
  const kind = mime.startsWith('image/') ? 'image' : mime.startsWith('video/') ? 'video' : 'document';
  clientRef.current?.sendMedia(kind, base64, { mime, filename, caption });
}
```

Pitfall: `kind` when sending — only `image` / `video` / `document` / `audio`
(`INBOUND_MEDIA_KINDS`). `voice` belongs to outbound frames **from** the agent — you can't send it
up.

## 7. Avatar in the WebView

The avatar is an HTML page implementing the `window.hermesAvatar` contract:

```js
window.hermesAvatar = {
  setState: (state) => {},   // 'idle' | 'thinking' | 'speaking'
  speak:    (text) => {},    // ask to voice a line
  setVolume:(level) => {},   // microphone level while recording, 0..1
};
```

The page URL arrives in the `ready` frame (`avatar_url`). If there's no URL — use your own page
(its minimal implementation is `examples/react-native/avatarHtml.ts`).

The main pitfall: **the page must be initialized after `ready`.** Before that
`window.hermesAvatar` doesn't exist in the WebView, and `injectJavaScript` silently does nothing.

```tsx
const [avatarHtml, setAvatarHtml] = useState<string | null>(null);
const [avatarUrl, setAvatarUrl] = useState<string | null>(null);

// in handlers.onReady:
onReady: (ready) => {
  setAvatarUrl(ready.avatar_url || null);
  // If there's no URL or it's unreachable — fall back to the built-in page:
  if (!ready.avatar_url) setAvatarHtml(AVATAR_FALLBACK_HTML);
},

useEffect(() => {
  if (!avatarHtml && !avatarUrl) return;         // ready hasn't arrived yet — the WebView isn't created
  webViewRef.current?.injectJavaScript(
    `window.hermesAvatar && window.hermesAvatar.setState('${avatarState}'); true;`,
  );
}, [avatarState, avatarHtml, avatarUrl]);

// In the markup:
{(avatarHtml || avatarUrl) ? (
  <WebView
    ref={webViewRef}
    source={avatarHtml ? { html: avatarHtml } : { uri: avatarUrl! }}
    style={styles.avatar} scrollEnabled={false} javaScriptEnabled
    // you can listen to the page's lines:
    onMessage={(e) => { try { const m = JSON.parse(e.nativeEvent.data); if (m.type === 'avatar_ready') {/* contract ready */} } catch {} }}
  />
) : null}
```

Pitfalls:

* **The WebView is created only after `ready`** — otherwise the state inject goes nowhere.
  Additionally you can wait for the `avatar_ready` message from the page (`onMessage`).
* End `injectJavaScript` calls with `true;` — otherwise iOS may consider the promise
  unresolved.
* If the avatar page is external (`avatar_url`), it must be reachable over HTTPS — the WebView
  won't load `http://` from a production build without `App Transport Security` exceptions.

## 8. Voice recording (WAV 16 kHz)

The protocol accepts `audio` only as `wav` or `ogg`, WAV — 16 kHz, mono, PCM 16 bit.
`react-native-live-audio-stream` returns raw PCM (base64) — it has to be wrapped in a WAV header.

```ts
// src/hermes/recorder.ts
import LiveAudioStream from 'react-native-live-audio-stream';

export function makeWavHeader(dataBytes: number, sampleRate = 16000, channels = 1, bits = 16): Uint8Array {
  const byteRate = (sampleRate * channels * bits) / 8;
  const blockAlign = (channels * bits) / 8;
  const buffer = new ArrayBuffer(44);
  const view = new DataView(buffer);
  const ascii = (o: number, s: string) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
  ascii(0, 'RIFF'); view.setUint32(4, 36 + dataBytes, true); ascii(8, 'WAVE');
  ascii(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
  view.setUint16(22, channels, true); view.setUint32(24, sampleRate, true);
  view.setUint32(28, byteRate, true); view.setUint16(32, blockAlign, true); view.setUint16(34, bits, true);
  ascii(36, 'data'); view.setUint32(40, dataBytes, true);
  return new Uint8Array(buffer);
}

let pcm: Uint8Array[] = [];
let voiceClient: { sendAudio: (b64: string, fmt: 'wav' | 'ogg', text?: string) => string } | null = null;

export function startRecording(client: { sendAudio: (b64: string, fmt: 'wav' | 'ogg', text?: string) => string }) {
  voiceClient = client; pcm = [];
  LiveAudioStream.init({ sampleRate: 16000, channels: 1, bitsPerSample: 16, audioSource: 6, bufferSize: 4096 });
  LiveAudioStream.on('data', (chunk: string) => pcm.push(base64ToBytes(chunk)));
  LiveAudioStream.start();
}

export function stopAndSend() {
  LiveAudioStream.stop();
  const size = pcm.reduce((n, p) => n + p.length, 0);
  if (size === 0 || !voiceClient) return;
  const out = new Uint8Array(44 + size);
  out.set(makeWavHeader(size), 0);
  let offset = 44;
  for (const part of pcm) { out.set(part, offset); offset += part.length; }
  voiceClient.sendAudio(bytesToBase64(out), 'wav');   // bytesToBase64 from hermes-mobile-client
}

function base64ToBytes(input: string): Uint8Array {
  const clean = input.replace(/[^A-Za-z0-9+/=]/g, '');
  const raw = typeof atob === 'function' ? atob(clean) : Buffer.from(clean, 'base64').toString('binary');
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return typeof btoa === 'function' ? btoa(binary) : Buffer.from(bytes).toString('base64');
}
```

While recording, feed the microphone level to the avatar:
`webViewRef.current?.injectJavaScript(\`window.hermesAvatar.setVolume(${level}); true;\`)`.

Pitfalls:

* **Format strictly `wav`/`ogg`** — the server rejects `mp3`/`aac` with `unsupported_frame`.
* **Audio ≤ 8 MB** (about a few minutes). Break a long recording into several `sendAudio`
  calls with a new message.
* If you have your own recognition — pass `text` as the third argument: the agent gets both the text
  and the file. Put `text` into `sendAudio`, not into a separate `text` frame.

## 9. Pitfalls summary

| Symptom | Cause | Fix |
|---|---|---|
| The WebView doesn't react to state | inject before `ready` | create the WebView only after `ready` |
| Duplicates in the feeds during streaming | `edit` frames shown as new | redraw the bubble by `id` (`onMessageEdit`) |
| "No access", reconnect in a loop | `auth_failed`/1008 not handled | show the text, don't reconnect |
| The card "hangs" | `other` without `text` | send the text in the same `prompt` frame |
| Media wasn't saved | `data` is empty, only `url` is present | download via `url` |
| Audio rejected | not wav/ogg or > 8 MB | WAV 16 kHz mono, split the recording |
