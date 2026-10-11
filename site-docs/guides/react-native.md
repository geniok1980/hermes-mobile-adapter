# React Native / Expo

Полный путь: установка пакета, клиент с переподключением, экран чата с лентами, карточки с
кнопками, приём и показ медиа, правка пузыря при стриминге, аватар в WebView и запись голоса.
Код — прямое продолжение примера `examples/react-native/HermesChat.tsx`: там всё то же, но
здесь собрано в готовые шаги с разбором подводных камней. Псевдокода нет — каждый фрагмент
компилируется при подключённых зависимостях.

## 0. Что нужно от владельца агента

| Что | Пример |
|---|---|
| Адрес | `wss://hermes-go.com/mobile-ws` (пилот) или ваш домен из `MOBILE_HOST`/`MOBILE_PORT`/`MOBILE_PATH` |
| Токен | 64 hex-символа из `MOBILE_TOKEN` в `~/.hermes/.env` |
| `device.id` | постоянный идентификатор, который выбираете вы, напр. `my-app-1` |

Токен в документации не приводится — его выдаёт владелец агента отдельно. `device.id` держите
постоянным: от него зависит сессия Hermes (`mobile:<device.id>`) и адрес cron-доставки.

## 1. Установка

```bash
npm i hermes-mobile-client          # клиент протокола (без рантайм-зависимостей)
npx expo install expo-file-system react-native-webview
npm i react-native-live-audio-stream # запись голоса (WAV 16 кГц)
```

`hermes-mobile-client` использует стандартный `WebSocket` и работает в Expo из коробки.
Для сохранения медиа нужен `expo-file-system`; для аватара — `react-native-webview`; для
записи голоса — `react-native-live-audio-stream` (нативный модуль, требует dev-build, не Expo Go).

## 2. Клиент с переподключением

Переподключение с экспоненциальной паузой уже встроено в `HermesClient` — не пишите своё.
Важно только правильно отреагировать на отказ в доступе (код 1008) и не долбить сервер.

```ts
// src/hermes/client.ts
import { HermesClient, type ConnectionState } from 'hermes-mobile-client';

const WS_URL = 'wss://hermes-go.com/mobile-ws';
const TOKEN = process.env.EXPO_PUBLIC_MOBILE_TOKEN ?? ''; // не хардкодьте токен в репозиторий
const DEVICE = { id: 'my-app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' };

export function createClient(handlers: ConstructorParameters<typeof HermesClient>[0]['handlers']) {
  const client = new HermesClient({
    url: WS_URL,
    token: TOKEN,
    device: DEVICE,
    maxBackoffMs: 15000,          // потолок паузы между попытками
    handlers: {
      ...handlers,
      onState: (state: ConnectionState, detail?: string) => handlers?.onState?.(state, detail),
      onError: (error) => {
        // auth_failed приходит с fatal:true — сервер закрыл соединение кодом 1008.
        // Повторять бессмысленно: показываем пользователю «нет доступа» и не reconnect'им.
        if (error.code === 'auth_failed' || error.fatal) {
          handlers?.onError?.({ ...error, message: 'Нет доступа: проверьте токен приложения' });
          return;
        }
        handlers?.onError?.(error); // transport/offline — не фатально, переподключение уже идёт
      },
    },
  });
  client.connect();
  return client;
}
```

Подводные камни:

* **`auth_failed` наступает после закрытия 1008.** SDK сам выставляет состояние `failed` и
  не переподключается — ваша задача показать текст, а не запускать цикл попыток заново.
* **Токен — секрет.** В Expo читайте его из `EXPO_PUBLIC_*`/EAS Secrets или `expo-secure-store`,
  не коммитьте в `app.json`.
* **`device.id` уникален на устройство.** Два приложения с одним `id` — это одна сессия Hermes
  и «прыгающие» ответы.

## 3. Экран чата с лентами

Модель — один список `Item`: пользовательские и агентские пузыри, карточки и медиа.
Ключевой момент — `upsert` по `id`, чтобы и стриминг, и повторная доставка правили один пузырь.

```tsx
// src/screens/ChatScreen.tsx
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FlatList, Image, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { WebView } from 'react-native-webview';
import * as FileSystem from 'expo-file-system';
import type { AgentMessage, AvatarState, MediaPayload, PromptDonePayload, PromptPayload } from 'hermes-mobile-client';
import { HermesClient } from 'hermes-mobile-client';
import { createClient } from '../hermes/client';
import { AVATAR_FALLBACK_HTML } from '../hermes/avatarHtml'; // см. examples/react-native/avatarHtml.ts

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
      // Новое сообщение: дописываем в пузырь с тем же id (длинный ответ режется на куски).
      onMessage: (m: AgentMessage) =>
        setItems((prev) => {
          const i = prev.findIndex((it) => it.id === m.id);
          if (i !== -1) { const next = [...prev]; next[i] = { ...next[i], text: next[i].text + m.text }; return next; }
          return [...prev, { id: m.id, role: 'agent', text: m.text }];
        }),
      // Стриминг-правка: заменяем текст пузыря, а не добавляем новый.
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

  // ... saveMedia, answer, send — см. ниже
}
```

## 4. Карточки с кнопками

Карточка — это кадр `prompt` с готовыми `options`. Ответ — **один** кадр `prompt` с `prompt_id`
и `option_id`; отдельным `text` выбор дублировать нельзя.

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

// ответ:
const answer = useCallback((item: Item, optionId: string) => {
  const promptId = item.id.replace(/^card-/, '');
  const client = clientRef.current;
  if (!client) return;
  if (optionId === 'other') {
    // clarify: вариант «свой ответ» требует текст — спросите его у пользователя.
    // Здесь для краткости: text приходит из вашего диалога ввода.
    const text = '';               // подставьте ввод из UI
    if (!text.trim()) return;
    client.answerPrompt(promptId, 'other', text);
  } else {
    client.answerPrompt(promptId, optionId);
  }
  upsert(item.id, { role: 'card', text: item.text, resolved: optionId });
}, [upsert]);
```

Подводный камень: вариант `other` **обязан** нести `text`. Отправите `option_id: "other"` без
текста — сервер переведёт карточку в режим ожидания текста, и она «зависнет».

## 5. Приём и показ медиа (expo-file-system)

Кадры `media` от агента приходят с `part`/`parts`; SDK собирает куски и вызывает `onMedia`
один раз на **целый** файл. Для картинок показываем `data:`-URL, всё остальное — сохраняем и
отдаём путь.

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

Подводные камни:

* **`data` может отсутствовать** — тогда файл отдаётся ссылкой `url`, скачивайте сами.
* **Не сохраняйте base64 в AsyncStorage** — это раздувает хранилище; пишите файл через
  `expo-file-system`, а в стейте держите путь.
* Ориентируйтесь на `mime`/`filename` при выборе расширения.

## 6. Отправка текста и медиа

```tsx
const send = useCallback(() => {
  const text = draft.trim();
  if (!text || !clientRef.current) return;
  clientRef.current.sendText(text);              // id генерится внутри и повторяется при retry
  setItems((prev) => [...prev, { id: `u-${Date.now()}`, role: 'user', text }]);
  setDraft('');
}, [draft]);

// Фото/файл: base64 или путь. SDK сам нарежет на куски по 3 МБ.
async function sendPicked(uri: string, filename: string, mime: string, caption?: string) {
  const base64 = await FileSystem.readAsStringAsync(uri, { encoding: FileSystem.EncodingType.Base64 });
  const kind = mime.startsWith('image/') ? 'image' : mime.startsWith('video/') ? 'video' : 'document';
  clientRef.current?.sendMedia(kind, base64, { mime, filename, caption });
}
```

Подводный камень: `kind` при отправке — только `image` / `video` / `document` / `audio`
(`INBOUND_MEDIA_KINDS`). `voice` относится к исходящим кадрам **от** агента — отправить его
вверх нельзя.

## 7. Аватар в WebView

Аватар — HTML-страница, реализующая контракт `window.hermesAvatar`:

```js
window.hermesAvatar = {
  setState: (state) => {},   // 'idle' | 'thinking' | 'speaking'
  speak:    (text) => {},    // попросить озвучить реплику
  setVolume:(level) => {},   // уровень микрофона при записи, 0..1
};
```

URL страницы приходит в кадре `ready` (`avatar_url`). Если URL нет — используйте свою страницу
(её минимальная реализация — `examples/react-native/avatarHtml.ts`).

Главный подводный камень: **страницу нужно инициализировать после `ready`.** До этого
`window.hermesAvatar` в WebView не существует, и `injectJavaScript` молча ничего не сделает.

```tsx
const [avatarHtml, setAvatarHtml] = useState<string | null>(null);
const [avatarUrl, setAvatarUrl] = useState<string | null>(null);

// в handlers.onReady:
onReady: (ready) => {
  setAvatarUrl(ready.avatar_url || null);
  // Если URL нет или он недоступен — падаем на встроенную страницу:
  if (!ready.avatar_url) setAvatarHtml(AVATAR_FALLBACK_HTML);
},

useEffect(() => {
  if (!avatarHtml && !avatarUrl) return;         // ready ещё не пришёл — WebView не создан
  webViewRef.current?.injectJavaScript(
    `window.hermesAvatar && window.hermesAvatar.setState('${avatarState}'); true;`,
  );
}, [avatarState, avatarHtml, avatarUrl]);

// В разметке:
{(avatarHtml || avatarUrl) ? (
  <WebView
    ref={webViewRef}
    source={avatarHtml ? { html: avatarHtml } : { uri: avatarUrl! }}
    style={styles.avatar} scrollEnabled={false} javaScriptEnabled
    // можно слушать реплики страницы:
    onMessage={(e) => { try { const m = JSON.parse(e.nativeEvent.data); if (m.type === 'avatar_ready') {/* контракт готов */} } catch {} }}
  />
) : null}
```

Подводные камни:

* **WebView создаётся только после `ready`** — иначе инжект состояния уходит в никуда.
  Дополнительно можно дождаться сообщения `avatar_ready` от страницы (`onMessage`).
* Вызовы `injectJavaScript` возвращайте с `true;` в конце — иначе iOS может считать промис
  неразрешённым.
* Если аватарная страница внешняя (`avatar_url`), она должна быть доступна по HTTPS — WebView
  не загрузит `http://` из production-сборки без `App Transport Security`-исключений.

## 8. Запись голоса (WAV 16 кГц)

Протокол принимает `audio` только в `wav` или `ogg`, WAV — 16 кГц, моно, PCM 16 бит.
`react-native-live-audio-stream` отдаёт сырой PCM (base64) — его надо завернуть в WAV-заголовок.

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
  voiceClient.sendAudio(bytesToBase64(out), 'wav');   // bytesToBase64 из hermes-mobile-client
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

При записи отдавайте уровень микрофона аватару:
`webViewRef.current?.injectJavaScript(\`window.hermesAvatar.setVolume(${level}); true;\`)`.

Подводные камни:

* **Формат строго `wav`/`ogg`** — `mp3`/`aac` сервер отвергнет с `unsupported_frame`.
* **Аудио ≤ 8 МБ** (примерно несколько минут). Длинную запись бейте на несколько `sendAudio`
  с новым сообщением.
* Если есть своё распознавание — передайте `text` третьим аргументом: агент получит и текст,
  и файл. Кладите `text` в `sendAudio`, а не в отдельный `text`-кадр.

## 9. Сводка подводных камней

| Симптом | Причина | Решение |
|---|---|---|
| WebView не реагирует на состояние | инжект до `ready` | создавать WebView только после `ready` |
| Дубли в лентах при стриминге | `edit`-кадры показаны как новые | перерисовывать пузырь по `id` (`onMessageEdit`) |
| «Нет доступа», reconnect по кругу | не обработан `auth_failed`/1008 | показать текст, не переподключаться |
| Карточка «зависла» | `other` без `text` | дослать текст в том же кадре `prompt` |
| Медиа не сохранилось | `data` пуст, есть только `url` | скачать по `url` |
| Аудио отклонено | не wav/ogg или > 8 МБ | WAV 16 кГц моно, разбить запись |
