# TypeScript SDK

Клиент протокола Hermes Mobile Adapter. Без зависимостей: используется стандартный
`WebSocket`, поэтому работает в React Native/Expo, в браузере, в Electron и в Node 22+.

## Установка

```bash
npm i hermes-mobile-client
```

Пакет собран в двух форматах: `import` (ESM — RN, Expo, web, сборщики) и `require`
(CommonJS — обычные Node-скрипты), типы прилагаются. Никаких рантайм-зависимостей.

```js
// CommonJS
const { HermesClient } = require('hermes-mobile-client');

// ESM
import { HermesClient } from 'hermes-mobile-client';
```

Опубликованная версия: https://www.npmjs.com/package/hermes-mobile-client

```ts
import { HermesClient } from 'hermes-mobile-client';

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

Источник можно передать тремя способами: **путь к файлу** (строка или `{ path }` — читается
с диска в Node), **base64** (строка, не похожая на путь) или **`Uint8Array`** (байты из памяти —
единственный способ в браузере и React Native, где нет доступа к файловой системе).
Большие файлы автоматически режутся по 3 МБ на кадры `part`/`parts`:

```ts
client.sendImage('/tmp/chart.png', { caption: 'график' });   // путь — читается с диска (Node)
client.sendImage({ path: 'chart.png' }, { caption: 'график' }); // то же самое, явной формой
client.sendDocument(new Uint8Array(pdfBytes), { filename: 'отчёт.pdf' });
client.sendVoice('/tmp/answer.ogg');
client.sendImage(base64FromCamera);                            // готовый base64 из приложения
client.sendMedia('video', '/tmp/clip.mp4');                    // произвольный тип
```

!!! warning "Путь или base64?"
    Строка, похожая на путь (`/tmp/x.png`, `./файл.jpg`, `file://…`, `C:\\…`), читается с диска;
    всё остальное считается base64. Если путь и base64 различить нельзя — передайте форму
    `{ path: '…' }`. В браузере и React Native чтения с диска нет: используйте `Uint8Array`
    (например из `fetch(...).arrayBuffer()`).

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

## Каталог команд и список моделей

Агент отдаёт их отдельными кадрами, а не текстом ответа — так список не попадает в переписку.
Признаки есть в `ready` (`capabilities.commands` / `capabilities.models`): если агент их не
объявляет (старый шлюз), запрашивайте текстом, как раньше.

```ts
const client = new HermesClient({
  url: 'wss://hermes-go.com/chef-nova-ws',
  token,
  device: { id: 'phone-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' },
  handlers: {
    onReady: ready => {
      if (ready.capabilities.commands) client.requestCommands();
      if (ready.capabilities.models) client.requestModels();
    },
    onCommands: payload => {
      // payload: { version, commands: [{ name, args, description, category, aliases }] }
      console.log(payload.commands.length);
    },
    onModels: payload => {
      // payload: { version, provider, current, models: [...] }
      console.log(payload.provider, payload.current, payload.models.length);
    },
  },
});

client.connect();
```

## Что внутри

Авторизация токеном, переподключение с экспоненциальной паузой, обработка отказа в
доступе (код 1008 — повторять бессмысленно), сборка длинных ответов из кадров `message`
(`chunk.index/total`, `final`), идемпотентные `id`, игнорирование неизвестных кадров.

Типы: `ReadyPayload`, `AgentMessage`, `PromptPayload`, `PromptOption`, `PromptDonePayload`,
`MediaPayload`, `MediaSource`, `CommandsPayload`, `CommandEntry`, `ModelsPayload`,
`ProtocolError`, `AvatarState`, `ConnectionState`, `HermesClientHandlers`.

Работа с историей и UI — на стороне приложения: SDK не хранит состояние, только транспорт.
Примеры: `examples/react-native/` и `examples/web/`.

## Проверка типов

```bash
npx tsc --noEmit --strict --target ES2020 --module ESNext --moduleResolution bundler hermesClient.ts
```
