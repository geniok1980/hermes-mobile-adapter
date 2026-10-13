# Python SDK — API reference

A client of the Hermes Mobile Adapter v1 protocol for Python (`asyncio` + `websockets`). For bots,
scripts, server integrations and automated tests. The protocol is the same as in the TypeScript SDK.

Source: `sdk/python/hermes_client.py`. Frame specification: `../../protocol/index.md`.

## Installation

```bash
pip install websockets>=12
```

| Requirement | Value |
|---|---|
| Python | 3.10+ (`from __future__ import annotations`, `X \| Y` in annotations are used) |
| Package | `websockets >= 12` (the asyncio implementation). Already present in the Hermes environment |
| Import | `from sdk.python.hermes_client import HermesMobileClient, Device` |

!!! warning "The token is a secret"
    `token` is the `MOBILE_TOKEN` on the agent owner's side. Do not log it and do not
    commit it: the token grants access to the agent's sessions, tools and memory.

!!! warning "Who configures access"
    The device `allowlist` and pairing are configured by the agent owner (`hermes pairing`).
    The SDK does not set them: an unapproved device gets a close with code `1008`, and the client —
    `HermesProtocolError('auth_failed', …, fatal=True)` in `on_error`.

---

## Class `HermesMobileClient`

### Constructor

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

`url`, `token` and `device` are positional; the rest are keyword-only.

| Parameter | Req. | Default | Meaning |
|---|---|---|---|
| `url` | yes | — | `ws://host:8787/ws` or `wss://host/ws`. The token is not passed in the URL. |
| `token` | yes | — | Application token. **Required**: empty → `ValueError`. Sent only in the `hello` frame. |
| `device` | no | `Device(id="py-<8 hex>", name="Python-клиент")` | Device (pairing, allowlist, cron). |
| `user` | no | `{}` | User — carried in `hello`. |
| `on_message` | no | `None` | Agent reply (a chunk without `edit`). |
| `on_message_edit` | no | `None` | Message edit (`edit`). |
| `on_avatar` | no | `None` | Avatar state — a string. |
| `on_typing` | no | `None` | The agent is typing. |
| `on_typing_stop` | no | `None` | `typing.stop` arrived. |
| `on_prompt` | no | `None` | Prompt card (`Prompt`). |
| `on_prompt_done` | no | `None` | Card closed (`PromptDone`). |
| `on_media` | no | `None` | Assembled media file (`MediaMessage`). |
| `on_error` | no | `None` | Protocol error (`HermesProtocolError`). Without it, errors are written to the log. |
| `on_ready` | no | `None` | Called after `connect()` with the `ready` frame. |
| `open_timeout` | no | `15.0` | Timeout for opening the WS connection, sec. |
| `request_timeout` | no | `15.0` | Timeout for waiting for the first frame (`ready`) in `connect()`, sec. |

Attributes after connecting:

| Attribute | Type | Meaning |
|---|---|---|
| `ready` | `Optional[Ready]` | `None` before `connect()`, then the `ready` frame. |
| `url`, `token`, `device`, `user` | — | Values from the constructor. |

All callbacks can be either plain functions or `async def` — the SDK `await`s them itself
when needed. An exception inside a callback propagates into the read loop.

!!! warning "There is no auto-reconnect"
    Unlike the TypeScript SDK, the Python client does **not** reconnect on its own. On a disconnect
    the read loop ends; on a close with code `1008`,
    `on_error(HermesProtocolError('auth_failed', …))` is called, other disconnects — silently (except for the log).
    Reconnection is up to the caller.

### Lifecycle

| Method | Signature | Returns | Description |
|---|---|---|---|
| `connect` | `async connect() -> Ready` | `Ready` | Opens the socket, sends `hello`, waits for `ready`, starts the read loop. Throws `HermesProtocolError` if `error` arrives or it is not `ready`. |
| `aclose` | `async aclose() -> None` | — | Cancels the read loop and closes the socket. Idempotent. |
| `__aenter__` | `async __aenter__() -> HermesMobileClient` | self | `async with` — calls `connect()` itself. |
| `__aexit__` | `async __aexit__(*exc_info) -> None` | — | Calls `aclose()` itself. |

Context manager:

```python
async with HermesMobileClient(url, token) as client:
    await client.send_text("привет")
    ...
# соединение закрыто автоматически
```

### Sending text and voice

| Method | Signature | Returns | Frame |
|---|---|---|---|
| `send_text` | `async send_text(text, message_id=None) -> str` | `message_id` | `text` |
| `send_audio` | `async send_audio(audio, fmt="wav", transcript=None, message_id=None) -> str` | `message_id` | `audio` |
| `send_typing` | `async send_typing() -> None` | — | `typing` |
| `ping` | `async ping(ts=None) -> None` | — | `ping` (`ts` defaults to 0) |
| `acknowledge` | `async acknowledge(message_id) -> None` | — | `ack` |

- `send_text`: empty text (or whitespace) → `ValueError`. Otherwise `message_id` — `uuid4().hex`;
  a repeat with the same `message_id` is treated by the adapter as a duplicate.
- `send_audio`: `audio` — `bytes` / `bytearray` / a path (`str` / `Path`), read from disk.
  `fmt` — only `"wav"` or `"ogg"`, otherwise `ValueError`. `transcript` is optional.
- Any method with a closed socket (`connect()` not called) → `RuntimeError`
  `"Соединение не открыто: вызовите connect()"`.

### Prompt cards

```python
async answer_prompt(prompt_id, option_id=None, text=None) -> None
```

Sends the `prompt` frame. `option_id` is lowercased and stripped.

| Parameter | Req. | Meaning |
|---|---|---|
| `prompt_id` | yes | `id` from `Prompt`. Empty → `ValueError`. |
| `option_id` | no¹ | `exec_approval`: `once\|session\|always\|deny`; `slash_confirm`: `once\|always\|cancel`; `clarify`: `c0..cN` or `other`. |
| `text` | no¹ | Free-form answer (for `clarify` with `other`). |

¹ At least one is required; otherwise `ValueError`.

!!! warning "An unknown option is replaced with a safe one"
    The adapter itself replaces an unrecognized value with `deny`/`cancel`. You cannot rely on the client
    — send a correct value.

### Media (sending)

| Method | Signature | `kind` |
|---|---|---|
| `send_media` | `async send_media(kind, payload=None, *, mime=None, filename=None, caption=None, message_id=None, chunk_bytes=MEDIA_CHUNK_BYTES) -> str` | set manually |
| `send_image` | `async send_image(payload, caption=None, **kwargs) -> str` | `image` |
| `send_document` | `async send_document(payload, caption=None, **kwargs) -> str` | `document` |
| `send_voice` | `async send_voice(payload, caption=None, **kwargs) -> str` | `audio` |

`payload` — `bytes` / `bytearray` / a path (`str` / `Path`); a `str` that is not a path
is treated as **base64**. `None` — no data.

- `kind` for `send_media` is lowercased and must be one of
  `image | video | document | audio` — otherwise `ValueError`.
- Empty data (`b""`) → `ValueError`.
- MIME: explicit `mime`, otherwise by extension, otherwise `application/octet-stream`.
- Large files are split by `chunk_bytes` (3 MB by default) into `media` frames with a common `id`
  and `part`/`parts` fields; the adapter reassembles them into one file.
- They return `message_id` (the passed one or a fresh `uuid4().hex`).

### Media (receiving)

| Method | Signature | Returns |
|---|---|---|
| `wait_media` | `async wait_media(timeout=30.0) -> MediaMessage` | A fully assembled file |
| `save_media` | `save_media(message, directory) -> Path` | Path of the saved file |

- `wait_media` — waits for a whole file from the queue; on timeout — `asyncio.TimeoutError`.
  Usually the `on_media` callback is enough.
- `save_media` — synchronous: creates the directory (expanding `~`), writes `message.data`, takes the name
  from `filename` or `<id><mime-suffix>`. The name is sanitized against `../`.

### Receiving replies

| Method | Signature | Returns |
|---|---|---|
| `wait_message` | `async wait_message(timeout=120.0) -> List[str]` | Parts of one full agent reply |
| `wait_prompt` | `async wait_prompt(timeout=30.0) -> Prompt` | The next prompt card |
| `wait_avatar` | `async wait_avatar(timeout=30.0) -> str` | The next avatar state |

- `wait_message` collects chunks until `final=True` arrives; on timeout —
  `asyncio.TimeoutError("Ответ агента не пришёл")`. Convenient for bots and tests.
- `wait_prompt` / `wait_media` / `wait_avatar` read from queues and throw
  `asyncio.TimeoutError` on timeout. `wait_avatar` skips non-`avatar` frames.

---

## Data (dataclasses)

### `Device`

| Field | Type | Default |
|---|---|---|
| `id` | `str` | — (required) |
| `name` | `str` | `""` |
| `platform` | `str` | `"python"` |
| `app_version` | `str` | `"1.0.0"` |

### `Ready` (the `ready` frame)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `v` | `int` | — | Protocol version. |
| `session` | `str` | — | Session identifier. |
| `device` | `Dict[str, Any]` | `{}` | The device as seen by the gateway. |
| `avatar_url` | `str` | `""` | Avatar URL for the WebView. |
| `capabilities` | `Dict[str, Any]` | `{}` | `text`, `voice`, `typing`, `avatar_states`, `media_in`, `media_out`, `prompts`, `edit`, `max_media_bytes`, `max_text_chars` — optional keys. |

### `AgentMessage` (the `message` frame)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | — | Agent message identifier. |
| `text` | `str` | — | Text of the chunk. |
| `index` | `int` | `0` | Chunk number. |
| `total` | `int` | `1` | Total chunks. |
| `final` | `bool` | `True` | Last chunk / end of the reply. |
| `edit` | `bool` | `False` | `True` — an edit of an already displayed message with the same `id`. |

### `PromptOption`, `Prompt`, `PromptDone`

`PromptOption`:

| Field | Type | Default |
|---|---|---|
| `id` | `str` | — |
| `label` | `str` | `""` |
| `style` | `str` | `""` |

`Prompt` (the `prompt` frame):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | — | Card identifier. |
| `kind` | `str` | — | `exec_approval` \| `slash_confirm` \| `clarify`. |
| `text` | `str` | `""` | Question text. |
| `options` | `List[PromptOption]` | `[]` | Answer options. |
| `session` | `str` | `""` | The card's session. |
| `timeout_seconds` | `int` | `0` | How long to wait for an answer. |

Method `Prompt.option_ids() -> List[str]` — the list of option `id`s.

`PromptDone` (the `prompt_done` frame):

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | — | Card identifier. |
| `option_id` | `Optional[str]` | `None` | The option that was applied. |
| `expired` | `bool` | `False` | `True` — expired. |

### `MediaMessage` (the `media` frame)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | — | Message identifier. |
| `kind` | `str` | — | `image` \| `video` \| `document` \| `audio` \| `voice` \| `animation`. |
| `data` | `bytes` | `b""` | **Assembled** bytes of the whole file. |
| `mime` | `str` | `""` | MIME type. |
| `filename` | `str` | `""` | File name. |
| `caption` | `str` | `""` | Caption. |
| `part` | `int` | `0` | Part number (0-based). |
| `parts` | `int` | `1` | Total parts. |
| `final` | `bool` | `True` | Last part. |
| `size` | `int` | `0` | Size, bytes. |
| `url` | `str` | `""` | A link, if the file is served by link. |
| `reply_to` | `str` | `""` | `id` of the message the media replies to. |

Method `MediaMessage.data_url() -> str` — a data URL for display in the WebView without saving to disk.

### Exception `HermesProtocolError`

A subclass of `RuntimeError`. Thrown from `connect()` and delivered to `on_error`.

| Attribute | Type | Meaning |
|---|---|---|
| `code` | `str` | Stable code (`auth_failed`, `bad_frame`, `unsupported_frame`, `too_big`, …). |
| `message` | `str` | Text for the user. |
| `fatal` | `bool` | `True` — retrying is pointless. |

`str(error)` → `"<code>: <message>"`.

## Constants

| Name | Value | Description |
|---|---|---|
| `PROTOCOL_VERSION` | `1` | Protocol version. |
| `MAX_FRAME_BYTES` | `16 * 1024 * 1024` | Ceiling of a single WS frame. |
| `MEDIA_CHUNK_BYTES` | `3 * 1024 * 1024` | Media chunk size. |
| `CLOSE_POLICY_VIOLATION` | `1008` | "Access denied" close code. |
| `INBOUND_MEDIA_KINDS` | `("image", "video", "document", "audio")` | What can be sent to the agent. |
| `CLARIFY_OTHER` | `"other"` | Free-form answer to `clarify`. |
| `C_*` / `S_*` | strings | Frame type names (`C_TEXT`, `S_MESSAGE`, …). |

---

## Which frames arrive in which callback

| Server frame | Callback / queue |
|---|---|
| `ready` | `on_ready` (and `self.ready`) |
| `message` (`edit` not set/false) | `on_message` **and** the `wait_message` queue |
| `message` (`edit: true`) | `on_message_edit` (does not go into the queue) |
| `typing` (without `stop`) | `on_typing` (and a utility queue) |
| `typing` (`stop: true`) | `on_typing_stop` |
| `avatar` | `on_avatar` (and the `wait_avatar` queue) |
| `prompt` | `on_prompt` (and the `wait_prompt` queue) |
| `prompt_done` | `on_prompt_done` |
| `media` (after all parts are assembled) | `on_media` (and the `wait_media` queue) |
| `error` | `on_error` (without it — `logger.warning`) |
| `pong`, unknown frames | ignored |
| close with code `1008` | `on_error` (`auth_failed`, `fatal=True`) |

## Which methods send which frames

| Method | Frame(s) client → server |
|---|---|
| `connect` | WS open + `hello { t, v, token, device, user }` |
| `aclose` | WS close |
| `send_text` | `text { t, id, text }` |
| `send_audio` | `audio { t, id, format, data, text? }` |
| `send_typing` | `typing { t }` |
| `ping` | `ping { t, ts }` |
| `acknowledge` | `ack { t, id }` |
| `answer_prompt` | `prompt { t, prompt_id, option_id?, text? }` |
| `send_media` / `send_image` / `send_document` / `send_voice` | `media { t, id, kind, mime, data, part, parts, filename?, caption? }` (1..N frames) |

---

## Examples

### Bot dialogue

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

### Receiving media and saving it to a file

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

### Answering a card

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
