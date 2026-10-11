# TypeScript SDK

Клиент протокола Hermes Mobile Adapter. Без зависимостей: используется стандартный
`WebSocket`, поэтому работает в React Native/Expo, в браузере, в Electron и в Node 22+.

```ts
import { HermesClient } from './hermesClient';

const client = new HermesClient({
  url: 'wss://agent.example.com/ws',
  token: process.env.MOBILE_TOKEN!,
  device: { id: 'app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' },
  handlers: {
    onReady: (ready) => console.log('сессия', ready.session),
    onMessage: (m) => append(m.text, m.final),
    onMessageEdit: (m) => replace(m.id, m.text),   // стриминг: перерисовать пузырь
    onAvatar: (state) => avatar.setState(state),   // idle | thinking | speaking
    onTyping: () => showTyping(),
    onTypingStop: () => hideTyping(),              // typing.stop
    onError: (e) => console.warn(e.code, e.message),
    onState: (state) => console.log('связь:', state),
  },
  maxBackoffMs: 15000,
});

client.connect();
client.sendText('привет');
```

## Карточки-вопросы

Агент может прислать карточку с кнопками (`exec_approval` — одобрение команды,
`slash_confirm` — подтверждение слэш-команды, `clarify` — уточняющий вопрос):

```ts
const client = new HermesClient({
  /* … */
  handlers: {
    onPrompt: (prompt) => renderCard(prompt),       // prompt.options: [{ id, label, style }]
    onPromptDone: (done) => closeCard(done.id, done.option_id ?? (done.expired ? 'истекла' : '')),
  },
});

// Ответ: exec_approval — once|session|always|deny, slash_confirm — once|always|cancel,
// clarify — c0..cN либо 'other' вместе с текстом. Незнакомое значение адаптер заменит
// на безопасное (deny/cancel), но лучше слать корректное.
client.answerPrompt(prompt.id, 'once');
client.answerPrompt(prompt.id, 'other', 'мой вариант');   // clarify «свой ответ»
```

## Медиа в обе стороны

Отправка — base64, `Uint8Array` или `{ path }` (в Node файл читается с диска).
Большие файлы автоматически режутся по 3 МБ на кадры `part`/`parts`:

```ts
client.sendImage({ path: '/tmp/chart.png' }, { caption: 'график' });
client.sendDocument(new Uint8Array(pdfBytes), { filename: 'отчёт.pdf' });
client.sendVoice('/tmp/answer.ogg');
// произвольный тип (image | video | document | audio):
client.sendMedia('video', { path: '/tmp/clip.mp4' });
```

Приём — колбэк `onMedia` вызывается один раз на **целый** файл (куски собираются внутри):

```ts
handlers: {
  onMedia: (media) => {
    // media.data — base64 целого файла, media.url — если файл не влезает и шлётся ссылкой
    const src = media.data ? `data:${media.mime};base64,${media.data}` : media.url;
    show(media.kind, src, media.filename, media.caption);
  },
}
```

Хелперы `bytesToBase64` / `base64ToBytes` и константа `MEDIA_CHUNK_BYTES` экспортируются.

## Что внутри

Авторизация токеном, переподключение с экспоненциальной паузой, обработка отказа в
доступе (код 1008 — повторять бессмысленно), сборка длинных ответов из кадров `message`
(`chunk.index/total`, `final`), идемпотентные `id`, игнорирование неизвестных кадров.

Типы: `ReadyPayload`, `AgentMessage`, `PromptPayload`, `PromptOption`, `PromptDonePayload`,
`MediaPayload`, `MediaSource`, `ProtocolError`, `AvatarState`, `ConnectionState`,
`HermesClientHandlers`.

Работа с историей и UI — на стороне приложения: SDK не хранит состояние, только транспорт.
Примеры: `examples/react-native/` и `examples/web/`.

## Проверка типов

```bash
npx tsc --noEmit --strict --target ES2020 --module ESNext --moduleResolution bundler hermesClient.ts
```
