# TypeScript SDK — справочник API

Пакет `hermes-mobile-client` — клиент протокола Hermes Mobile Adapter v1 для приложений и
Node-скриптов. Ничего не знает про UI: только транспорт и колбэки. Рантайм-зависимостей
нет — используется стандартный `WebSocket`.

Исходник: `sdk/typescript/hermesClient.ts`. Спецификация кадров: `../../protocol/index.md`.

## Установка

```bash
npm i hermes-mobile-client
```

Пакет собран в двух форматах (поле `exports`):

```js
// ESM — React Native, Expo, web, сборщики, современный Node
import { HermesClient } from 'hermes-mobile-client';

// CommonJS — обычные Node-скрипты
const { HermesClient } = require('hermes-mobile-client');
```

Требования и совместимость:

| Что | Значение |
|---|---|
| Node | `>= 22` (поле `engines`) |
| Платформы | React Native / Expo, браузер (web), Electron, Node |
| Зависимости | нет (используется глобальный `WebSocket`, в Node 22 он встроен) |
| Типы | `.d.ts` прилагаются (`dist/esm/hermesClient.d.ts`) |
| Версия пакета | `1.0.1` |

!!! warning "Токен — секрет"
    `token` — это `MOBILE_TOKEN` на стороне владельца агента. Не вшивайте его в
    публичные бандлы (web/Electron) и не коммитьте: по токену открыт доступ к сессиям,
    инструментам и памяти агента. В web отдавайте его со своего бэкенда.

!!! warning "Кто настраивает доступ"
    Список устройств (`allowlist`) и pairing настраивает владелец агента (`hermes pairing`).
    SDK их не задаёт: если устройство не одобрено, шлюз закроет соединение кодом `1008`,
    и клиент перейдёт в состояние `failed` с ошибкой `auth_failed` (повторять бессмысленно).

---

## Класс `HermesClient`

### Конструктор

```ts
new HermesClient(options: ClientOptions)
```

`ClientOptions`:

| Поле | Тип | Обяз. | По умолчанию | Значение |
|---|---|---|---|---|
| `url` | `string` | да | — | Адрес шлюза, `ws://host:8787/ws` или `wss://host/ws`. Токен дописывается к URL как query-параметр. |
| `token` | `string` | да | — | Токен приложения (`MOBILE_TOKEN`). Уходит и в query, и в кадре `hello`. |
| `device` | `DeviceInfo` | да | — | Устройство, как оно видно в Hermes (pairing, allowlist, cron). |
| `user` | `{ id?: string; name?: string }` | нет | `{}` | Пользователь — попадает в кадр `hello`. |
| `handlers` | `HermesClientHandlers` | нет | `{}` | Колбэки событий (см. ниже). |
| `maxBackoffMs` | `number` | нет | `15000` | Потолок паузы между попытками переподключения, мс. |

!!! note "`openTimeout` в TS-SDK нет"
    В отличие от Python-SDK (там есть `open_timeout`), конструктор TypeScript **не
    принимает** `openTimeout`: таймаут открытия сокета отдан глобальному `WebSocket`
    рантайма. Поле `ClientOptions` с таким именем отсутствует.

`DeviceInfo`:

| Поле | Тип | Значение |
|---|---|---|
| `id` | `string` | Идентификатор устройства (используется в `mobile:<id>`). |
| `name` | `string` | Отображаемое имя. |
| `platform` | `string` | `ios` / `android` / `web` / иная метка. |
| `appVersion` | `string` | Версия приложения. |

### Свойства и служебные методы

| Член | Сигнатура | Что делает |
|---|---|---|
| `ready` | `get ready(): boolean` | `true`, если состояние `connected`. |
| `setHandlers` | `setHandlers(handlers: HermesClientHandlers): void` | Дополняет текущие колбэки (слияние поверх уже установленных). Позволяет задать обработчики после конструктора. |
| `connect` | `connect(): void` | Сбрасывает флаг ручного закрытия и открывает соединение. После `open` шлёт `hello`. Возвращает `void` — готовность приходит в `onReady`/`onState`. |
| `disconnect` | `disconnect(): void` | Помечает закрытие как ручное, отменяет переподключение, закрывает сокет, переводит состояние в `idle`. |

### Отправка текста, голоса и служебных кадров

| Метод | Сигнатура | Возвращает | Кадр | Ошибки |
|---|---|---|---|---|
| `sendText` | `sendText(text: string, messageId?: string): string` | `id` отправленного сообщения | `text` | Не бросает. Пустой текст **не** проверяется — сервер ответит ошибкой `bad_frame` в `onError`. |
| `sendAudio` | `sendAudio(base64: string, format: 'wav' \| 'ogg', transcript?: string, messageId?: string): string` | `id` | `audio` | Тип ограничивает только компилятор; рантайм-проверки формата нет. `transcript` необязателен. |
| `sendTyping` | `sendTyping(): void` | — | `typing` | Не бросает. |
| `acknowledge` | `acknowledge(messageId: string): void` | — | `ack` | Подтверждение показа/проигрывания сообщения с данным `id`. Не бросает. |
| `ping` | `ping(): void` | — | `ping` | Шлёт `ts = Date.now()`. Ответ `pong` игнорируется. Не бросает. |

`id`, если не передан, генерируется `makeId()`. Повтор с тем же `id` адаптер считает
дубликатом и игнорирует — это идемпотентность повторной доставки.

!!! warning "Нет связи — не исключение, а `onError`"
    Все `send*` методы при закрытом сокете **не бросают**, а вызывают
    `onError({ code: 'offline', message: 'Нет связи со шлюзом', fatal: false })`.
    Проверяйте состояние через `client.ready` перед отправкой.

### Карточки-вопросы

```ts
answerPrompt(promptId: string, optionId?: string, text?: string): void
```

Отправляет кадр `prompt`. `optionId` приводится к нижнему регистру.

| Параметр | Обяз. | Значение |
|---|---|---|
| `promptId` | да | `id` из `onPrompt`. |
| `optionId` | нет¹ | Для `exec_approval` — `once\|session\|always\|deny`; для `slash_confirm` — `once\|always\|cancel`; для `clarify` — `c0..cN` или `other`. |
| `text` | нет¹ | Свободный ответ (для `clarify` с `other`). |

¹ Нужен хотя бы один из `optionId` / `text`.

Бросает `Error`, если `promptId` пуст, либо если не задан ни `optionId`, ни `text`.

!!! warning "Незнакомый вариант заменяется на безопасный"
    Адаптер сам заменяет нераспознанное значение на `deny` (для одобрения команды) или
    `cancel` (для слэш-команды). Полагаться на клиента нельзя — шлите корректное значение.

### Медиа

| Метод | Сигнатура | Возвращает | Кадры |
|---|---|---|---|
| `sendMedia` | `sendMedia(kind: string, source: MediaSource, options?: MediaSendOptions): string` | `id` | 1..N кадров `media` (`part`/`parts`) |
| `sendImage` | `sendImage(source: MediaSource, options?: MediaSendOptions): string` | `id` | `media` с `kind=image` |
| `sendDocument` | `sendDocument(source: MediaSource, options?: MediaSendOptions): string` | `id` | `media` с `kind=document` |
| `sendVoice` | `sendVoice(source: MediaSource, options?: MediaSendOptions): string` | `id` | `media` с `kind=audio` |

`MediaSource = string | Uint8Array | { path: string }`:

- `string` — **только base64** (декодируется);
- `Uint8Array` — готовые байты;
- `{ path: string }` — путь к файлу, читается с диска (**только в Node**; в браузере/RN
  вызовет `Error`).

`MediaSendOptions`:

| Поле | Тип | По умолчанию | Значение |
|---|---|---|---|
| `mime` | `string` | по расширению | MIME-тип кадра. |
| `filename` | `string` | — | Имя файла. |
| `caption` | `string` | — | Подпись. |
| `messageId` | `string` | `makeId()` | Идемпотентность. |
| `chunkBytes` | `number` | `3 МБ` (`MEDIA_CHUNK_BYTES`) | Размер куска: файл режется на кадры `part`/`parts`. |

Особенности:

- `kind` для `sendMedia` приводится к нижнему регистру и должен быть одним из
  `image | video | document | audio` — иначе бросает `Error`. «Голос» шлётся как `audio`
  (обёртка `sendVoice`).
- Пустой файл (0 байт) → бросает `Error`.
- Большие файлы автоматически режутся по `chunkBytes` (по умолчанию 3 МБ): адаптер собирает
  куски в один файл, поэтому размер не упирается в лимит одного WS-кадра.
- MIME при отсутствии `mime`/`filename` угадывается по расширению (`guessMime`), иначе
  `application/octet-stream`.

!!! danger "Строка в TS — это base64, а не путь"
    В TypeScript-SDK `string` в `source` **всегда** трактуется как base64. Пример вида
    `client.sendVoice('/tmp/answer.ogg')` со строкой-путём (он встречался в README) не
    прочитает файл, а декодирует путь как base64. Для файла используйте
    `client.sendVoice({ path: '/tmp/answer.ogg' })`.

---

## Колбэки `HermesClientHandlers`

Все колбэки необязательны; исключение внутри колбэка не перехватывается SDK.

| Колбэк | Сигнатура | Когда вызывается | Аргумент |
|---|---|---|---|
| `onState` | `(state: ConnectionState, detail?: string) => void` | При каждой смене состояния | Текущее состояние; `detail` — текст (напр. `попытка 2`) |
| `onReady` | `(ready: ReadyPayload) => void` | Пришёл кадр `ready` (после `hello`) | Данные сессии |
| `onMessage` | `(message: AgentMessage) => void` | Кадр `message` без `edit` | Кусок ответа агента |
| `onMessageEdit` | `(message: AgentMessage) => void` | Кадр `message` с `edit: true` | Правка ранее показанного сообщения с тем же `id` (стриминг) |
| `onTyping` | `() => void` | Кадр `typing` без `stop` | — (агент печатает) |
| `onTypingStop` | `() => void` | Кадр `typing` с `stop: true` | — (погасить индикатор) |
| `onAvatar` | `(state: AvatarState) => void` | Кадр `avatar` | `idle \| thinking \| speaking` (по умолчанию `idle`) |
| `onPrompt` | `(prompt: PromptPayload) => void` | Кадр `prompt` | Карточка-вопрос с вариантами |
| `onPromptDone` | `(done: PromptDonePayload) => void` | Кадр `prompt_done` | Карточка закрыта (выбран вариант или истекла) |
| `onMedia` | `(media: MediaPayload) => void` | Кадр `media`, **после** сборки всех частей | Один готовый файл |
| `onError` | `(error: ProtocolError) => void` | Кадр `error`, отказ `auth_failed`, обрыв (`transport`) или офлайн-отправка | Код и сообщение |

Порядок вызовов:

- `onState` вызывается до «содержательных» колбэков на смену состояния: `connecting` →
  (`connected` и `onReady`) при первой сессии; при обрыве `reconnecting` с `detail`,
  при отказе — `failed` и следом `onError(auth_failed)`.
- `onMessageEdit` — отдельная ветка: кадр `edit` попадает в `onMessageEdit`, а не в
  `onMessage`.
- `onMedia` вызывается один раз на целый файл: промежуточные части собираются внутри и
  наружу не отдаются.

---

## Типы и константы

### Состояния и версия

| Имя | Тип | Значение |
|---|---|---|
| `PROTOCOL_VERSION` | `const number` | `1` |
| `AvatarState` | `'idle' \| 'thinking' \| 'speaking'` | Состояние аватара |
| `ConnectionState` | `'idle' \| 'connecting' \| 'connected' \| 'reconnecting' \| 'failed'` | Состояние связи |

### `ReadyPayload` (кадр `ready`)

| Поле | Тип | Значение |
|---|---|---|
| `v` | `number` | Версия протокола. |
| `session` | `string` | Идентификатор сессии. |
| `device` | `{ id: string; name: string }` | Устройство по мнению шлюза. |
| `avatar_url` | `string` | URL аватара для WebView. |
| `capabilities` | объект (см. ниже) | Возможности шлюза. |

`capabilities`:

| Поле | Тип | Значение |
|---|---|---|
| `text` | `boolean` | Поддержка текста. |
| `voice` | `boolean` | Поддержка голоса. |
| `typing` | `boolean` | Поддержка индикатора «печатает». |
| `avatar_states?` | `AvatarState[]` | Доступные состояния аватара. |
| `media_in?` | `string[]` | Типы медиа, принимаемые агентом. |
| `media_out?` | `string[]` | Типы медиа от агента. |
| `prompts?` | `string[]` | Типы карточек. |
| `edit?` | `boolean` | Поддержка правок сообщений. |
| `max_media_bytes?` | `number` | Лимит файла. |
| `max_text_chars?` | `number` | Лимит текста. |

Новые поля необязательны — старый шлюз их не шлёт, проверяйте `?.` .

### `AgentMessage` (кадр `message`)

| Поле | Тип | Значение |
|---|---|---|
| `id` | `string` | Идентификатор сообщения агента. |
| `text` | `string` | Текст куска. |
| `index` | `number` | Номер куска (из `chunk.index`, по умолчанию 0). |
| `total` | `number` | Всего кусков (из `chunk.total`, по умолчанию 1). |
| `final` | `boolean` | Последний кусок / конец ответа. |
| `edit?` | `boolean` | `true` — правка уже показанного сообщения с тем же `id`. |

### `PromptOption`, `PromptPayload`, `PromptDonePayload`

`PromptOption`:

| Поле | Тип | Значение |
|---|---|---|
| `id` | `string` | Идентификатор варианта. |
| `label` | `string` | Подпись кнопки. |
| `style?` | `string` | Подсказка стиля. |

`PromptPayload` (кадр `prompt`):

| Поле | Тип | Значение |
|---|---|---|
| `id` | `string` | Идентификатор карточки. |
| `kind` | `string` | `exec_approval` \| `slash_confirm` \| `clarify`. |
| `text` | `string` | Текст вопроса. |
| `options` | `PromptOption[]` | Варианты ответа. |
| `session` | `string` | Сессия, к которой относится карточка. |
| `timeout_seconds` | `number` | Сколько ждать ответ (секунд). |

`PromptDonePayload` (кадр `prompt_done`):

| Поле | Тип | Значение |
|---|---|---|
| `id` | `string` | Идентификатор карточки. |
| `option_id?` | `string` | Применённый вариант. |
| `expired?` | `boolean` | `true` — карточка истекла. |

### `MediaPayload` (кадр `media`)

| Поле | Тип | Значение |
|---|---|---|
| `id` | `string` | Идентификатор сообщения. |
| `kind` | `string` | `image` \| `video` \| `document` \| `audio` \| `voice` \| `animation`. |
| `mime` | `string` | MIME-тип. |
| `filename` | `string` | Имя файла. |
| `caption` | `string` | Подпись. |
| `part` | `number` | Номер части (0-based). |
| `parts` | `number` | Всего частей. |
| `final` | `boolean` | Последняя часть. |
| `size` | `number` | Размер файла, байт. |
| `data?` | `string` | base64 **целого** файла (все части собраны), либо отсутствует. |
| `url?` | `string` | Ссылка, если файл отдан ссылкой. |
| `reply_to?` | `string` | `id` сообщения, на которое отвечает медиа. |

Для отображения: `media.data ? \`data:${media.mime};base64,${media.data}\` : media.url`.

### Прочее

`ProtocolError`:

| Поле | Тип | Значение |
|---|---|---|
| `code` | `string` | Стабильный код (`auth_failed`, `transport`, `offline`, `bad_frame`, …). |
| `message` | `string` | Текст для пользователя. |
| `fatal` | `boolean` | `true` — повторять бессмысленно. |

`MEDIA_CHUNK_BYTES` — `const number`, `3 * 1024 * 1024` (размер куска медиа).

Функции:

| Функция | Сигнатура | Значение |
|---|---|---|
| `makeId` | `(): string` | Идентификатор сообщения (время в base36 + случайный суффикс). |
| `bytesToBase64` | `(bytes: Uint8Array): string` | Кодирование байтов в base64 без зависимостей. |
| `base64ToBytes` | `(input: string): Uint8Array` | Декодирование base64 (не-base64 символы отбрасываются, свой декодер — не нужен `atob`/`Buffer`). |

---

## Какие кадры в какой колбэк приходят

| Кадр сервера | Колбэк |
|---|---|
| `ready` | `onReady` (и `onState('connected')`) |
| `message` (`edit` не задан/false) | `onMessage` |
| `message` (`edit: true`) | `onMessageEdit` |
| `typing` (без `stop`) | `onTyping` |
| `typing` (`stop: true`) | `onTypingStop` |
| `avatar` | `onAvatar` |
| `prompt` | `onPrompt` |
| `prompt_done` | `onPromptDone` |
| `media` (после сборки всех частей) | `onMedia` |
| `error` | `onError` |
| `pong` | игнорируется |
| неизвестный кадр | игнорируется (требование протокола) |
| событие сокета `onerror` | `onError` (`code: 'transport'`, `fatal: false`) |
| закрытие кодом `1008` | `onState('failed')` + `onError` (`code: 'auth_failed'`, `fatal: true`) |
| отправка при закрытом сокете | `onError` (`code: 'offline'`, `fatal: false`) |

## Какие методы шлют какие кадры

| Метод | Кадр(ы) клиент → сервер |
|---|---|
| `connect` | открытие WS + `hello { t, v, token, device, user }` |
| `disconnect` | закрытие WS |
| `sendText` | `text { t, id, text }` |
| `sendAudio` | `audio { t, id, format, data, text? }` |
| `sendTyping` | `typing { t }` |
| `acknowledge` | `ack { t, id }` |
| `ping` | `ping { t, ts }` |
| `answerPrompt` | `prompt { t, prompt_id, option_id?, text? }` |
| `sendMedia` / `sendImage` / `sendDocument` / `sendVoice` | `media { t, id, kind, mime, data, part, parts, filename?, caption? }` (1..N кадров) |

---

## Примеры

### Подключение и чат

```ts
import { HermesClient } from 'hermes-mobile-client';

const client = new HermesClient({
  url: 'wss://agent.example.com/ws',
  token: process.env.MOBILE_TOKEN!,
  device: { id: 'app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' },
  handlers: {
    onReady: (ready) => console.log('сессия', ready.session),
    onState: (state, detail) => console.log('связь:', state, detail ?? ''),
    onMessage: (m) => append(m.text, m.final),          // кусок ответа
    onMessageEdit: (m) => replace(m.id, m.text),         // стриминг: перерисовать пузырь
    onAvatar: (state) => avatar.setState(state),         // idle | thinking | speaking
    onTyping: () => showTyping(),
    onTypingStop: () => hideTyping(),
    onError: (e) => console.warn(e.code, e.message, e.fatal),
  },
  maxBackoffMs: 15000,
});

client.connect();
client.sendText('привет, Hermes');
```

### Карточка одобрения команды

```ts
const client = new HermesClient({
  /* … */
  handlers: {
    onPrompt: (prompt) => {
      // prompt.options: [{ id, label, style }]
      renderCard(prompt.text, prompt.options, (optionId) => {
        client.answerPrompt(prompt.id, optionId);   // once | session | always | deny
      });
    },
    onPromptDone: (done) => {
      closeCard(done.id, done.expired ? 'истекла' : done.option_id ?? '');
    },
  },
});

client.connect();

// Уточняющий вопрос: свободный ответ вместе с 'other'
// client.answerPrompt(prompt.id, 'other', 'мой вариант');
```

### Отправка фото и приём медиа

```ts
const client = new HermesClient({
  /* … */
  handlers: {
    onMedia: (media) => {
      // media.data — base64 целого файла; media.url — если файл отдан ссылкой
      const src = media.data
        ? `data:${media.mime};base64,${media.data}`
        : media.url;
      show(media.kind, src, media.filename, media.caption);
    },
  },
});

client.connect();

client.sendImage({ path: '/tmp/chart.png' }, { caption: 'график' });      // файл (Node)
client.sendDocument(new Uint8Array(pdfBytes), { filename: 'отчёт.pdf' }); // байты
client.sendVoice({ path: '/tmp/answer.ogg' });                // файл → { path }
client.sendMedia('video', { path: '/tmp/clip.mp4' });
```
