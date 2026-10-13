# Protocol v1 — reference

Transport is WebSocket, each message is **one JSON frame in UTF-8**. Default entry point:
`ws://<host>:8787/ws`; externally — `wss://<domain>/ws` through a reverse proxy (see [For the agent owner](../host/index.md)).

The source of truth is `adapter/hermes_mobile/protocol.py` (constants and validation) and
`adapter/hermes_mobile/adapter.py` (behaviour). Client implementations:
`sdk/typescript/`, `sdk/python/hermes_client.py`.

## General frame rules

* `t` — frame type, **required**, case-insensitive (lowercased).
* `v` — protocol version; the server declares it in the `ready` frame. Current — `1`.
* `id` — identifier set by the client; needed for **idempotency** and replies
  (truncated to 128 characters).
* An unknown frame type — the client **must ignore** it, not drop the connection. The server behaves
  the same way: an unknown frame from the client is ignored (a debug log is written).
* A frame that is not an object, not JSON or not UTF-8 → error `bad_frame`. Above the size limit — `too_big`.

## Constraints and limits

| Constant | Value | What it limits |
|---|---|---|
| `PROTOCOL_VERSION` | `1` | protocol version (in `ready.v`) |
| `MAX_FRAME_BYTES` | 16 MiB (`16777216`) | one WebSocket frame (voice in base64) |
| `MAX_TEXT_CHARS` | `12000` | message text (`text`, `caption`) |
| `MAX_AUDIO_BYTES` | 8 MiB (`8388608`) | `audio` frame (voice recording) |
| `MAX_MEDIA_BYTES` | 8 MiB (`8388608`) | one media file in either direction |
| `MEDIA_CHUNK_BYTES` | 3 MiB (`3145728`) | chunk size when transferring large files |
| `MIN_MEDIA_CHUNK_BYTES` | 4 KiB (`4096`) | minimum chunk when assembling media — the `parts` ceiling is derived from it |
| `MAX_MEDIA_PARTS` | `2048` | ceiling on `parts` for one file (`ceil(8 MiB / 4 KiB)`) |
| `DEFAULT_CHUNK_LIMIT` | `3500` | how many characters in one `message` frame (response slicing) |
| `DEFAULT_PROMPT_TTL` | `300` s | card lifetime if the owner has not overridden it |
| unfinished uploads | 8 per device | how many multi-part `media` uploads can be kept open at once |
| total device buffer | 8 MiB (`8388608`) | the sum of all open multi-part uploads of one device |
| hello timeout | 15 s | how long the server waits for the first `hello` frame |
| typing idle | 3 s | idle without messages → the avatar returns to `idle` |
| close code | `1008` | access denied (WebSocket policy violation) |

## Handshake

1. The client connects to `ws(s)://…/ws`. **The token is passed only via the `token` field in the `hello` frame** —
   acceptance via `?token=` in the URL has been removed: the query string ends up in reverse-proxy logs and leaks.
2. **The first frame the client must send is `hello`** — otherwise the server sends `error` and closes
   the connection.
3. The server replies with `ready` containing `session`, `device`, `avatar_url` and `capabilities`.

```json
{"t":"hello","token":"<token>","device":{"id":"tel-1","name":"Phone","platform":"android","app_version":"1.0.0"},"user":{"id":"u-1","name":"Eugene"}}
```

The token can be **bound to a device**: a `MOBILE_EXTRA_TOKENS` entry of the form `device_id=token`
allows this token to connect only under the specified `device.id`. An attempt to take someone else's id
with a different token is rejected with code `forbidden` and close **1008**; the same device with the same
token is a reconnection (the old connection is closed). More — in
[Security](../security.md).

```json
{"t":"ready","v":1,"session":"mobile:tel-1","device":{"id":"tel-1","name":"Phone"},"avatar_url":"https://agent.example.com/avatar/","capabilities":{"text":true,"voice":true,"typing":true,"avatar_states":["idle","thinking","speaking"],"media_in":["image","video","document","audio"],"media_out":["image","voice","video","document","animation"],"prompts":["exec_approval","slash_confirm","clarify"],"edit":true,"max_media_bytes":8388608,"max_text_chars":12000}}
```

`capabilities` fields:

| Field | Type | Meaning |
|---|---|---|
| `text` / `voice` / `typing` | bool | basic capabilities (always `true`) |
| `avatar_states` | list | avatar states: `idle`, `thinking`, `speaking` |
| `media_in` | list | what the server accepts from the app: `image`, `video`, `document`, `audio` |
| `media_out` | list | what the server can send: `image`, `voice`, `video`, `document`, `animation` |
| `prompts` | list | card types: `exec_approval`, `slash_confirm`, `clarify` |
| `edit` | bool | the server can edit a `message` by `id` (streaming) |
| `max_media_bytes` | number | ceiling for one media file (`8388608`) |
| `max_text_chars` | number | text ceiling (`12000`) |

`session` (`mobile:<device.id>`) — the session identifier in Hermes and the delivery address for
`hermes send --to mobile:<device.id>`.

## Client → server: all frames

### `hello`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"hello"` |
| `token` | string | yes | app token — **only in the `hello` frame**; the `?token=` query parameter is not supported |
| `device` | object | no | `{id,name,platform,app_version}` — all strings, truncated to 120 characters |
| `device.id` | string | no* | permanent device identifier → `session`; if absent — `dev-<hex>` is generated |
| `user` | object | no | `{id,name}` — which user Hermes sees you as |

\* `device.id` is not formally required, but **without it the session address changes** and access becomes harder.
Always set it. A repeated `hello` on the same connection — error `bad_frame`.

### `text`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"text"` |
| `id` | string | yes | message identifier (idempotency) |
| `text` | string | yes | message text, ≤ 12,000 characters, not empty |

### `audio`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"audio"` |
| `id` | string | yes | message identifier |
| `format` | string | yes | `"wav"` or `"ogg"` (others → `unsupported_frame`) |
| `data` | string | yes | recording in base64, decoded ≤ 8 MiB |
| `text` | string | no | recognized text (if you have your own STT) — then the agent gets both the text and the file |

### `media` (app → agent)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"media"` |
| `id` | string | yes | file identifier; **the same** for all chunks |
| `kind` | string | yes | `image` / `video` / `document` / `audio` (otherwise `unsupported_frame`) |
| `mime` | string | no | MIME; if empty — derived from `filename` |
| `filename` | string | no | file name (truncated to 200 characters), important for documents |
| `caption` | string | no | caption (≤ 12,000); for `audio` it works as recognized text |
| `data` | string | yes | the **chunk** content in base64 |
| `part` | integer | no | chunk number, starting at `0` (default `0`) |
| `parts` | integer | no | total chunks, ≥ 1 and ≤ `2048` (default `1`) |

A file ≤ 8 MiB — in one frame (`parts: 1`); larger — in chunks of ~3 MiB (`part: 0,1,2…`).
The adapter **assembles all chunks** and only then hands the file to the agent: a series cannot be interrupted.

### Memory constraints on file uploads

The app cannot eat the gateway's memory with unfinished uploads:

* no more than **8 unfinished multi-part uploads** per device at a time;
* the total size of all open buffers of one device — no more than **8 MiB**;
* `parts` of one file — no more than **2048** (`ceil(8 MiB / 4 KiB)`); `parts` is computed from
  the minimum chunk of 4 KiB, not from 3 MiB.

When the number of uploads, the total volume (> 8 MiB) or `parts` (> 2048) is exceeded, the server replies
`too_big` / `bad_frame` and does not keep the buffer. A device's unfinished buffers are freed when it
disconnects.

### `prompt` (card response)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"prompt"` |
| `prompt_id` | string | yes | `id` of the card the server sent |
| `option_id` | string | conditional | selected option; if absent — a non-empty `text` is required |
| `text` | string | conditional | "custom answer" (for `clarify`) or instead of `option_id` |

At least one of `option_id` / `text` must be non-empty, otherwise `bad_frame`.

### Other client frames

| Frame | Fields | Meaning |
|---|---|---|
| `typing` | `t` | "user is typing"; the server takes it into account for the avatar |
| `ping` | `t`, `ts`? | connection check; the server replies with `pong` carrying the same `ts` |
| `ack` | `t`, `id` | the app displayed/played the message; the server **ignores** it |
| `deliver` | `t`, `id`, `target`?, `text`?, `media`? | service delivery (cron/`hermes send`); at least `text` or `media` |

`deliver.media` — attachment object: `{kind, mime?, filename?, data(base64)}`. `target` — the device
address; if empty — `MOBILE_HOME_DEVICE` is used.

The `deliver` frame is a **service** frame: it is accepted only by a connection from a loopback address (`127.0.0.0/8`,
`::1`) or a connection with a token from `MOBILE_DELIVER_TOKENS`. To everyone else the device replies
`error{code:"forbidden"}` — so an ordinary client cannot broadcast a message on behalf of cron.

## Server → client: all frames

### `ready`

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"ready"` |
| `v` | integer | yes | protocol version (`1`) |
| `session` | string | yes | `mobile:<device.id>` |
| `device` | object | yes | `{id,name}` — the device as the server sees it |
| `avatar_url` | string | yes | URL of the avatar HTML page (may be empty) |
| `capabilities` | object | yes | declared capabilities (table above) |

### `message` (agent response)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"message"` |
| `id` | string | yes | agent message identifier — **the same** for all parts and edits |
| `text` | string | yes | next piece of text (≤ 3,500 characters per frame) |
| `chunk` | object | yes | `{index, total}` — part number and count |
| `final` | bool | yes | `true` — the last part/edit |
| `edit` | bool | no | `true` — an **edit** of a previously sent message with the same `id` (streaming) |

A long response is cut into several `message` frames with one `id`; the last has `final: true`.
Concatenate by `id` — then the response looks like a single message.

### `media` (agent → app)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"media"` |
| `id` | string | yes | file identifier (collect chunks by it) |
| `kind` | string | yes | `image` / `voice` / `video` / `document` / `animation` |
| `mime` | string | yes | MIME type |
| `filename` | string | yes | file name |
| `caption` | string | no | caption |
| `part` | integer | yes | chunk number, starting at `0` |
| `parts` | integer | yes | total chunks |
| `final` | bool | yes | `true` — last chunk of the file |
| `size` | integer | yes | full file size in bytes (not of the chunk) |
| `data` | string | no | chunk in base64 (may be absent if the file is delivered by link) |
| `url` | string | no | link to the file (the app downloads it itself) |
| `reply_to` | string | no | `id` of the message the media replies to |

### `prompt` (card with buttons)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `t` | string | yes | `"prompt"` |
| `id` | string | yes | `prompt_id` — return it in the response |
| `kind` | string | yes | `exec_approval` / `slash_confirm` / `clarify` |
| `text` | string | yes | title/question |
| `options` | array | yes | buttons: `[{id, label, style}]` (`style` — `primary`/`danger`/empty) |
| `command` | string | no | for `exec_approval`: the **exact command** being approved (show it next to the buttons) |
| `description` | string | no | for `exec_approval`: an explanation of what the command does |
| `session` | string | yes | the Hermes session the response is addressed to |
| `timeout_seconds` | integer | yes | card lifetime (default `300`) |

For an `exec_approval` card the server sends **separate `command` and `description` fields**: the app
shows the exact command next to the buttons so the person sees exactly what they are approving. The card
can be closed by **only the device it is shown to** — a response from another connection
receives `error{code:"forbidden"}`.

### `prompt_done` (card closed)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | card `id` |
| `option_id` | string | no | the option actually applied (may differ from the one sent) |
| `expired` | bool | no | `true` — the card was closed by timeout or was unknown |

### `avatar`, `typing`, `pong`, `error`

| Frame | Fields | Meaning |
|---|---|---|
| `avatar` | `state` (`idle`/`thinking`/`speaking`) | avatar state |
| `typing` | `stop`? | the agent is working; `stop: true` — clear the indicator immediately |
| `pong` | `ts` | reply to `ping` (echoes `ts`) |
| `error` | `code`, `message`, `fatal` | error; `fatal: true` — the connection is then closed |

!!! note "Declared but not sent by the adapter"
    The `delivered` frame (`{t,id,device}`) is declared in `protocol.py`, but the current adapter does not send it:
    service delivery is confirmed only at the connection level itself. Rely on the absence of
    `error`/`device_offline`. The `avatar.mood` field is also declared but not sent —
    read only `state`. The error code `auth_required` is in the specification, but on the first frame
    other than `hello` the adapter sends `bad_frame` (see [Troubleshooting](../troubleshooting.md)).

## Question cards

A response to a card does **not** become a chat message: the adapter resolves it with Hermes's standard
mechanisms (command approval, slash-command confirmation, clarification).

| `kind` | Allowed `option_id` | Unknown value → |
|---|---|---|
| `exec_approval` | `once`, `session`, `always`, `deny` | `deny` |
| `slash_confirm` | `once`, `always`, `cancel` | `cancel` |
| `clarify` | `c0`, `c1`, … (in option order), `other` | text request (`other`) |

For `clarify` the option `cN` means "the N-th proposed option was selected" (zero-based numbering), `other` —
"the user will enter a custom answer": the app **must** pass `text`.

**The response is validated on the server.** The client cannot "forge" an option: any value outside
the table is replaced with a safe one, and the actual choice is returned in `prompt_done.option_id`.
The client must not be trusted.

A card lives for `timeout_seconds`; once it elapses, `prompt_done` arrives with `expired: true`. A response to
an unknown/expired `prompt_id` does not drop the connection — `prompt_done{expired:true}` arrives.

## Media in both directions

One `media` frame is one chunk of one file. Large files arrive as several frames and are assembled
on the receiving side.

* **up:** `kind` ∈ `image`/`video`/`document`/`audio`; ≤ 8 MiB — in one frame, larger — in chunks
  of ~3 MiB with a constant `id`; a series cannot be interrupted.
* **down:** `kind` ∈ `image`/`voice`/`video`/`document`/`animation`; collect `data` by `id` and
  save on `final: true`; use `mime`/`filename`; if there is a `url` — download by link.

If an incoming file is larger than 8 MiB or the frame size is exceeded, the server replies with `error` code
`too_big`; split into chunks (`part`/`parts`) or compress. Keep the memory limits in mind: ≤ 8
unfinished uploads per device, total buffer ≤ 8 MiB, `parts` ≤ 2048 (see above).

## Connection lifecycle and avatar states

```
connect → hello → ready ──► exchange: text/audio/media/prompt ⇄ message/media/prompt/avatar/typing
   ▲                                                          │
   │                                                          ▼
 disconnect/sleep ── reconnect with a pause ─┘       typing{stop:true} → avatar:idle
```

The frame order in a typical exchange: `avatar:thinking` (and `typing`) → `message` (1..N parts) →
`avatar:speaking` → after a pause (`typing idle`, ~3 s) `avatar:idle`. If `typing{stop:true}` arrives at
the end — the indicator is cleared immediately, without waiting for the timeout.

Avatar states: `thinking` (the agent is processing), `speaking` (the agent is producing a response/media),
`idle` (at rest). Any unknown state is replaced by the server with `idle`.

## Idempotency by `id`

* The `id` in `text` / `audio` / `media` frames is the deduplication key. A repeat with the same `id` (for example,
  after a network drop) is **ignored**: the agent will not receive the message twice.
* The deduplication key is the pair `<chat_id>:<id>`; the retention window is about 600 seconds (a buffer of ~4000 entries).
* A duplicate `text`/`audio` is discarded silently (without `error`); media is checked for a duplicate **by
  the whole file**, not by chunk.
* Keep `id` constant for each message and repeat it on resend.

## Error codes

| Code | When it arrives | What the app should do |
|---|---|---|
| `bad_frame` | not JSON/not UTF-8/not an object; missing a required field (`text`, `data`, `prompt_id`); empty message; repeated `hello` | fix the frame; on fatal ones — reconnect |
| `auth_failed` | invalid token (or a device outside `MOBILE_ALLOWED_DEVICES`); followed by close `1008` | show "no access", **do not retry** |
| `forbidden` | action forbidden for this connection: someone else's `device.id` is taken (the token is bound to another device), a response to a **foreign** device's card, or a `deliver` not from cron (not loopback and no token from `MOBILE_DELIVER_TOKENS`) | do not retry as is: check the token binding/your own device; `deliver` is sent only by cron |
| `unsupported_frame` | unknown frame type; unsupported audio `format` or media `kind` | update the app |
| `too_big` | `MAX_FRAME_BYTES` / `MAX_TEXT_CHARS` / `MAX_AUDIO_BYTES` / `MAX_MEDIA_BYTES` exceeded | split the message, send the file in chunks or compress |
| `device_offline` | delivery (`deliver`) to a device that is not online | retry later |
| `auth_required` | declared in the specification; the current adapter sends `bad_frame` instead on the first frame other than `hello` | send `hello` first |
| `internal_error` | internal adapter error (declared; used rarely) | show a generic text, reconnect |

Fatal errors (invalid token, someone else's `device.id` taken, frame limit exceeded, hello timeout)
are accompanied by closing the connection with code **1008** (in `reason` — the error code).

## Versioning and backward compatibility

* New **optional** fields can be added at any time — old clients ignore them.
* New **frame types** are added the same way: the client must ignore an unknown `t`.
* Incompatible changes — only by changing `v` in `ready` and in the documentation.
* All the capabilities described here (question cards, media, `message` edits, `typing.stop`) are
  backward-compatible additions, so the version stays **`1`**.
