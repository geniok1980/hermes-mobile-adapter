# Python SDK — справочник API

Клиент протокола Hermes Mobile Adapter v1 для Python (`asyncio` + `websockets`). Для ботов,
скриптов, серверных интеграций и автотестов. Протокол тот же, что у TypeScript-SDK.

Исходник: `sdk/python/hermes_client.py`. Спецификация кадров: `../../protocol/index.md`.

## Установка

```bash
pip install websockets>=12
```

| Требование | Значение |
|---|---|
| Python | 3.10+ (используется `from __future__ import annotations`, `X \| Y` в аннотациях) |
| Пакет | `websockets >= 12` (asyncio-реализация). В окружении Hermes уже есть |
| Импорт | `from sdk.python.hermes_client import HermesMobileClient, Device` |

!!! warning "Токен — секрет"
    `token` — это `MOBILE_TOKEN` на стороне владельца агента. Не логируйте его и не
    коммитьте: по токену открыт доступ к сессиям, инструментам и памяти агента.

!!! warning "Кто настраивает доступ"
    `allowlist` устройств и pairing настраивает владелец агента (`hermes pairing`).
    SDK их не задаёт: неодобренное устройство получает закрытие кодом `1008`, а клиент —
    `HermesProtocolError('auth_failed', …, fatal=True)` в `on_error`.

---

## Класс `HermesMobileClient`

### Конструктор

```python
HermesMobileClient(
    url: str,
    token: str,
    device: Optional[Device] = None,
    *,
    user: Optional[Dict[str, str]] = None,
    on_message: Optional[Callable[[AgentMessage], Union[None, Awaitable[None]]]] = None,
    on_message_edit: Optional[Callable[[AgentMessage], Union[None, Awaitable[None]]]] = None,
    on_avatar: Optional[Callable[[str], Union[None, Awaitable[None]]]] = None,
    on_typing: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    on_typing_stop: Optional[Callable[[], Union[None, Awaitable[None]]]] = None,
    on_prompt: Optional[Callable[[Prompt], Union[None, Awaitable[None]]]] = None,
    on_prompt_done: Optional[Callable[[PromptDone], Union[None, Awaitable[None]]]] = None,
    on_media: Optional[Callable[[MediaMessage], Union[None, Awaitable[None]]]] = None,
    on_error: Optional[Callable[[HermesProtocolError], Union[None, Awaitable[None]]]] = None,
    on_ready: Optional[Callable[[Ready], Union[None, Awaitable[None]]]] = None,
    open_timeout: float = 15.0,
    request_timeout: float = 15.0,
)
```

`url`, `token` и `device` — позиционные; остальное — только по имени.

| Параметр | Обяз. | По умолчанию | Значение |
|---|---|---|---|
| `url` | да | — | `ws://host:8787/ws` или `wss://host/ws`. Токен дописывается query-параметром. |
| `token` | да | — | Токен приложения. **Обязателен**: пустой → `ValueError`. |
| `device` | нет | `Device(id="py-<8 hex>", name="Python-клиент")` | Устройство (pairing, allowlist, cron). |
| `user` | нет | `{}` | Пользователь — уходит в `hello`. |
| `on_message` | нет | `None` | Ответ агента (кусок без `edit`). |
| `on_message_edit` | нет | `None` | Правка сообщения (`edit`). |
| `on_avatar` | нет | `None` | Состояние аватара — строка. |
| `on_typing` | нет | `None` | Агент печатает. |
| `on_typing_stop` | нет | `None` | Пришёл `typing.stop`. |
| `on_prompt` | нет | `None` | Карточка-вопрос (`Prompt`). |
| `on_prompt_done` | нет | `None` | Карточка закрыта (`PromptDone`). |
| `on_media` | нет | `None` | Собранный медиафайл (`MediaMessage`). |
| `on_error` | нет | `None` | Ошибка протокола (`HermesProtocolError`). Без него ошибки пишутся в лог. |
| `on_ready` | нет | `None` | Вызывается после `connect()` с кадром `ready`. |
| `open_timeout` | нет | `15.0` | Таймаут открытия WS-соединения, сек. |
| `request_timeout` | нет | `15.0` | Таймаут ожидания первого кадра (`ready`) в `connect()`, сек. |

Атрибуты после подключения:

| Атрибут | Тип | Значение |
|---|---|---|
| `ready` | `Optional[Ready]` | `None` до `connect()`, далее кадр `ready`. |
| `url`, `token`, `device`, `user` | — | Значения из конструктора. |

Все колбэки могут быть как обычными функциями, так и `async def` — SDK сам их
`await`-ит при необходимости. Исключение внутри колбэка всплывает в цикл чтения.

!!! warning "Автопереподключения нет"
    В отличие от TypeScript-SDK, Python-клиент **не** переподключается сам. При обрыве
    цикл чтения завершается; при закрытии кодом `1008` зовётся
    `on_error(HermesProtocolError('auth_failed', …))`, прочие обрывы — молча (кроме лога).
    Переподключение организуйте вызывающей стороной.

### Жизненный цикл

| Метод | Сигнатура | Возвращает | Описание |
|---|---|---|---|
| `connect` | `async connect() -> Ready` | `Ready` | Открывает сокет, шлёт `hello`, ждёт `ready`, запускает цикл чтения. Бросает `HermesProtocolError`, если пришёл `error` или не `ready`. |
| `aclose` | `async aclose() -> None` | — | Отменяет цикл чтения и закрывает сокет. Идемпотентен. |
| `__aenter__` | `async __aenter__() -> HermesMobileClient` | self | `async with` — сам вызывает `connect()`. |
| `__aexit__` | `async __aexit__(*exc_info) -> None` | — | Сам вызывает `aclose()`. |

Контекстный менеджер:

```python
async with HermesMobileClient(url, token) as client:
    await client.send_text("привет")
    ...
# соединение закрыто автоматически
```

### Отправка текста и голоса

| Метод | Сигнатура | Возвращает | Кадр |
|---|---|---|---|
| `send_text` | `async send_text(text, message_id=None) -> str` | `message_id` | `text` |
| `send_audio` | `async send_audio(audio, fmt="wav", transcript=None, message_id=None) -> str` | `message_id` | `audio` |
| `send_typing` | `async send_typing() -> None` | — | `typing` |
| `ping` | `async ping(ts=None) -> None` | — | `ping` (`ts` по умолчанию 0) |
| `acknowledge` | `async acknowledge(message_id) -> None` | — | `ack` |

- `send_text`: пустой текст (или пробелы) → `ValueError`. `message_id` иначе — `uuid4().hex`;
  повтор с тем же `message_id` адаптер считает дубликатом.
- `send_audio`: `audio` — `bytes` / `bytearray` / путь (`str` / `Path`), читается с диска.
  `fmt` — только `"wav"` или `"ogg"`, иначе `ValueError`. `transcript` необязателен.
- Любой метод при закрытом сокете (`connect()` не вызван) → `RuntimeError`
  `"Соединение не открыто: вызовите connect()"`.

### Карточки-вопросы

```python
async answer_prompt(prompt_id, option_id=None, text=None) -> None
```

Отправляет кадр `prompt`. `option_id` приводится к нижнему регистру и обрезается.

| Параметр | Обяз. | Значение |
|---|---|---|
| `prompt_id` | да | `id` из `Prompt`. Пустой → `ValueError`. |
| `option_id` | нет¹ | `exec_approval`: `once\|session\|always\|deny`; `slash_confirm`: `once\|always\|cancel`; `clarify`: `c0..cN` или `other`. |
| `text` | нет¹ | Свободный ответ (для `clarify` с `other`). |

¹ Нужен хотя бы один; иначе `ValueError`.

!!! warning "Незнакомый вариант заменяется на безопасный"
    Адаптер сам заменяет нераспознанное значение на `deny`/`cancel`. Полагаться на клиента
    нельзя — шлите корректное значение.

### Медиа (отправка)

| Метод | Сигнатура | `kind` |
|---|---|---|
| `send_media` | `async send_media(kind, payload=None, *, mime=None, filename=None, caption=None, message_id=None, chunk_bytes=MEDIA_CHUNK_BYTES) -> str` | задаётся вручную |
| `send_image` | `async send_image(payload, caption=None, **kwargs) -> str` | `image` |
| `send_document` | `async send_document(payload, caption=None, **kwargs) -> str` | `document` |
| `send_voice` | `async send_voice(payload, caption=None, **kwargs) -> str` | `audio` |

`payload` — `bytes` / `bytearray` / путь (`str` / `Path`); `str`, не являющийся путём,
трактуется как **base64**. `None` — без данных.

- `kind` для `send_media` приводится к нижнему регистру и должен быть одним из
  `image | video | document | audio` — иначе `ValueError`.
- Пустые данные (`b""`) → `ValueError`.
- MIME: явный `mime`, иначе по расширению, иначе `application/octet-stream`.
- Крупные файлы режутся по `chunk_bytes` (по умолчанию 3 МБ) на кадры `media` с общим `id`
  и полями `part`/`parts`; адаптер собирает их в один файл.
- Возвращают `message_id` (переданный или свежий `uuid4().hex`).

### Медиа (приём)

| Метод | Сигнатура | Возвращает |
|---|---|---|
| `wait_media` | `async wait_media(timeout=30.0) -> MediaMessage` | Полностью собранный файл |
| `save_media` | `save_media(message, directory) -> Path` | Путь сохранённого файла |

- `wait_media` — ждёт из очереди целый файл; по истечении — `asyncio.TimeoutError`.
  Обычно достаточно колбэка `on_media`.
- `save_media` — синхронный: создаёт каталог (с раскрытием `~`), пишет `message.data`, имя
  берёт из `filename` либо `<id><суффикс-по-mime>`. Имя санитизируется от `../`.

### Приём ответов

| Метод | Сигнатура | Возвращает |
|---|---|---|
| `wait_message` | `async wait_message(timeout=120.0) -> List[str]` | Части одного полного ответа агента |
| `wait_prompt` | `async wait_prompt(timeout=30.0) -> Prompt` | Следующая карточка-вопрос |
| `wait_avatar` | `async wait_avatar(timeout=30.0) -> str` | Следующее состояние аватара |

- `wait_message` собирает куски, пока не придёт `final=True`; по таймауту —
  `asyncio.TimeoutError("Ответ агента не пришёл")`. Удобно для ботов и тестов.
- `wait_prompt` / `wait_media` / `wait_avatar` читают из очередей и бросают
  `asyncio.TimeoutError` по истечении. `wait_avatar` пропускает не-`avatar` кадры.

---

## Данные (dataclasses)

### `Device`

| Поле | Тип | По умолчанию |
|---|---|---|
| `id` | `str` | — (обязательное) |
| `name` | `str` | `""` |
| `platform` | `str` | `"python"` |
| `app_version` | `str` | `"1.0.0"` |

### `Ready` (кадр `ready`)

| Поле | Тип | По умолчанию | Значение |
|---|---|---|---|
| `v` | `int` | — | Версия протокола. |
| `session` | `str` | — | Идентификатор сессии. |
| `device` | `Dict[str, Any]` | `{}` | Устройство по мнению шлюза. |
| `avatar_url` | `str` | `""` | URL аватара для WebView. |
| `capabilities` | `Dict[str, Any]` | `{}` | `text`, `voice`, `typing`, `avatar_states`, `media_in`, `media_out`, `prompts`, `edit`, `max_media_bytes`, `max_text_chars` — необязательные ключи. |

### `AgentMessage` (кадр `message`)

| Поле | Тип | По умолчанию | Значение |
|---|---|---|---|
| `id` | `str` | — | Идентификатор сообщения агента. |
| `text` | `str` | — | Текст куска. |
| `index` | `int` | `0` | Номер куска. |
| `total` | `int` | `1` | Всего кусков. |
| `final` | `bool` | `True` | Последний кусок / конец ответа. |
| `edit` | `bool` | `False` | `True` — правка уже показанного сообщения с тем же `id`. |

### `PromptOption`, `Prompt`, `PromptDone`

`PromptOption`:

| Поле | Тип | По умолчанию |
|---|---|---|
| `id` | `str` | — |
| `label` | `str` | `""` |
| `style` | `str` | `""` |

`Prompt` (кадр `prompt`):

| Поле | Тип | По умолчанию | Значение |
|---|---|---|---|
| `id` | `str` | — | Идентификатор карточки. |
| `kind` | `str` | — | `exec_approval` \| `slash_confirm` \| `clarify`. |
| `text` | `str` | `""` | Текст вопроса. |
| `options` | `List[PromptOption]` | `[]` | Варианты ответа. |
| `session` | `str` | `""` | Сессия карточки. |
| `timeout_seconds` | `int` | `0` | Сколько ждать ответ. |

Метод `Prompt.option_ids() -> List[str]` — список `id` вариантов.

`PromptDone` (кадр `prompt_done`):

| Поле | Тип | По умолчанию | Значение |
|---|---|---|---|
| `id` | `str` | — | Идентификатор карточки. |
| `option_id` | `Optional[str]` | `None` | Применённый вариант. |
| `expired` | `bool` | `False` | `True` — истекла. |

### `MediaMessage` (кадр `media`)

| Поле | Тип | По умолчанию | Значение |
|---|---|---|---|
| `id` | `str` | — | Идентификатор сообщения. |
| `kind` | `str` | — | `image` \| `video` \| `document` \| `audio` \| `voice` \| `animation`. |
| `data` | `bytes` | `b""` | **Собранные** байты целого файла. |
| `mime` | `str` | `""` | MIME-тип. |
| `filename` | `str` | `""` | Имя файла. |
| `caption` | `str` | `""` | Подпись. |
| `part` | `int` | `0` | Номер части (0-based). |
| `parts` | `int` | `1` | Всего частей. |
| `final` | `bool` | `True` | Последняя часть. |
| `size` | `int` | `0` | Размер, байт. |
| `url` | `str` | `""` | Ссылка, если файл отдан ссылкой. |
| `reply_to` | `str` | `""` | `id` сообщения, на которое отвечает медиа. |

Метод `MediaMessage.data_url() -> str` — data-URL для показа в WebView без сохранения на диск.

### Исключение `HermesProtocolError`

Наследник `RuntimeError`. Бросается из `connect()` и попадает в `on_error`.

| Атрибут | Тип | Значение |
|---|---|---|
| `code` | `str` | Стабильный код (`auth_failed`, `bad_frame`, `unsupported_frame`, `too_big`, …). |
| `message` | `str` | Текст для пользователя. |
| `fatal` | `bool` | `True` — повторять бессмысленно. |

`str(error)` → `"<code>: <message>"`.

## Константы

| Имя | Значение | Описание |
|---|---|---|
| `PROTOCOL_VERSION` | `1` | Версия протокола. |
| `MAX_FRAME_BYTES` | `16 * 1024 * 1024` | Потолок одного WS-кадра. |
| `MEDIA_CHUNK_BYTES` | `3 * 1024 * 1024` | Размер куска медиа. |
| `CLOSE_POLICY_VIOLATION` | `1008` | Код закрытия «отказ в доступе». |
| `INBOUND_MEDIA_KINDS` | `("image", "video", "document", "audio")` | Что можно слать агенту. |
| `CLARIFY_OTHER` | `"other"` | Свободный ответ на `clarify`. |
| `C_*` / `S_*` | строки | Имена типов кадров (`C_TEXT`, `S_MESSAGE`, …). |

---

## Какие кадры в какой колбэк приходят

| Кадр сервера | Колбэк / очередь |
|---|---|
| `ready` | `on_ready` (и `self.ready`) |
| `message` (`edit` не задан/false) | `on_message` **и** очередь `wait_message` |
| `message` (`edit: true`) | `on_message_edit` (в очередь не попадает) |
| `typing` (без `stop`) | `on_typing` (и служебная очередь) |
| `typing` (`stop: true`) | `on_typing_stop` |
| `avatar` | `on_avatar` (и очередь `wait_avatar`) |
| `prompt` | `on_prompt` (и очередь `wait_prompt`) |
| `prompt_done` | `on_prompt_done` |
| `media` (после сборки всех частей) | `on_media` (и очередь `wait_media`) |
| `error` | `on_error` (без него — `logger.warning`) |
| `pong`, неизвестные кадры | игнорируются |
| закрытие кодом `1008` | `on_error` (`auth_failed`, `fatal=True`) |

## Какие методы шлют какие кадры

| Метод | Кадр(ы) клиент → сервер |
|---|---|
| `connect` | открытие WS + `hello { t, v, token, device, user }` |
| `aclose` | закрытие WS |
| `send_text` | `text { t, id, text }` |
| `send_audio` | `audio { t, id, format, data, text? }` |
| `send_typing` | `typing { t }` |
| `ping` | `ping { t, ts }` |
| `acknowledge` | `ack { t, id }` |
| `answer_prompt` | `prompt { t, prompt_id, option_id?, text? }` |
| `send_media` / `send_image` / `send_document` / `send_voice` | `media { t, id, kind, mime, data, part, parts, filename?, caption? }` (1..N кадров) |

---

## Примеры

### Бот-диалог

```python
import asyncio
from sdk.python.hermes_client import Device, HermesMobileClient


async def main():
    async with HermesMobileClient(
        "ws://127.0.0.1:8787/ws",
        token="…",
        device=Device(id="bot-1", name="Бот"),
    ) as client:
        await client.send_text("привет, Hermes")
        answer = await client.wait_message(timeout=120)   # части одного полного ответа
        print("".join(answer))


asyncio.run(main())
```

### Приём медиа с сохранением в файл

```python
def on_media(message):
    path = client.save_media(message, "~/загрузки")   # -> Path
    print("сохранён:", path, message.kind, message.mime)
    # message.data — байты; message.data_url() — data-URL для показа картинки

def on_typing():
    print("агент печатает…")

def on_typing_stop():
    print("…перестал")

client = HermesMobileClient(url, token, on_media=on_media,
                            on_typing=on_typing, on_typing_stop=on_typing_stop)
await client.connect()

media = await client.wait_media(timeout=30)           # или ждать явно
await client.send_image("~/скрин.png", caption="вот ошибка")   # путь → base64
await client.send_document(b"%PDF-1.4 …", filename="отчёт.pdf")
await client.send_voice("~/answer.ogg")
await client.send_media("video", "/tmp/clip.mp4")
```

### Ответ на карточку

```python
def on_prompt(prompt):                       # карточка с вариантами
    print(prompt.kind, prompt.text, prompt.option_ids())
    # отвечаем из того же процесса:
    asyncio.create_task(client.answer_prompt(prompt.id, option_id="once"))

def on_prompt_done(done):                    # карточка закрыта
    print("итог:", done.option_id, "истекла" if done.expired else "")

client = HermesMobileClient(url, token, on_prompt=on_prompt, on_prompt_done=on_prompt_done)
await client.connect()

# exec_approval — once|session|always|deny; slash_confirm — once|always|cancel;
# clarify — c0..cN либо other вместе с текстом:
#   await client.answer_prompt(prompt.id, option_id="other", text="мой вариант")
```
