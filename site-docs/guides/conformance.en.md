# Acceptance checklist

Step-by-step verification of the integration. Each item: how to reproduce (`wscat` or code), what should
be in the response, what a typical mistake looks like. Substitute your own token and `device.id`; the
examples contain no real tokens.

Tools: `npm i -g wscat`, the pilot address is `wss://hermes-go.com/mobile-ws`.
For local development — `ws://127.0.0.1:8787/ws`. It is more convenient to pass the token in the `hello`
frame itself (not in the URL) so that it does not linger in proxy logs.

---

## 1. Connection and `ready` with `capabilities`

**How to get**

```bash
wscat -c "wss://hermes-go.com/mobile-ws"
# then send:
{"t":"hello","token":"<token>","device":{"id":"qa-1","name":"QA","platform":"cli","app_version":"0.0.1"},"user":{"id":"u-qa","name":"QA"}}
```

**Expected result**

```json
{"t":"ready","v":1,"session":"mobile:qa-1","device":{"id":"qa-1","name":"QA"},
 "avatar_url":"…","capabilities":{"text":true,"voice":true,"typing":true,
 "avatar_states":["idle","thinking","speaking"],"media_in":["image","video","document","audio"],
 "media_out":["image","voice","video","document","animation"],
 "prompts":["exec_approval","slash_confirm","clarify"],"edit":true,
 "max_media_bytes":8388608,"max_text_chars":12000}}
```

Check: `v == 1`, `session == "mobile:<your device.id>"`, `capabilities.edit == true`.

**Typical mistake:** `{"t":"error","code":"auth_required",…}` — the `hello` frame is not the first one (or
not sent). Only `hello` is allowed as the first frame.

---

## 2. Sending text and receiving a reply

**How to get**

```json
{"t":"text","id":"q-1","text":"Hello, answer in one word"}
```

**Expected result:** a series of `message` frames with one `id` and a growing `chunk.index`, the last —
`final:true`; before that an `avatar`/`typing` may arrive. `event.message_id == "q-1"` goes to the agent.

**Typical mistake:** the reply text does not arrive at all — the device has not yet been approved by the
agent owner. On the agent's machine: `hermes pairing list` → `approve mobile qa-1`. Or empty
text was sent — then `{"t":"error","code":"bad_frame","message":"Пустое сообщение"}` arrives.

---

## 3. A repeat with the same `id` → no duplicate

**How to get**

```json
{"t":"text","id":"dup-1","text":"remember the number 42"}
{"t":"text","id":"dup-1","text":"remember the number 42"}
{"t":"text","id":"dup-1","text":"remember the number 42"}
```

**Expected result:** **one** message reaches the agent (the adapter ignores repeats with the same
`id`). Ask the agent "how many times did I ask you to remember 42?" — the answer is "once".

**Typical mistake:** you use a new `id` on each resend — the agent sees three messages.
`id` is required for idempotency and must repeat on a retry after a drop.

---

## 4. An approval card and the answer to it

**How to get:** you need an agent that requests approval of a dangerous command (in Hermes that is
`exec_approval`). Ask it to run a command that requires confirmation — a frame will arrive:

```json
{"t":"prompt","id":"<prompt_id>","kind":"exec_approval","text":"…rm -rf…",
 "command":"rm -rf /tmp/build","description":"Deletes the temporary build directory",
 "options":[{"id":"once","label":"✅ Approve once","style":"primary"},
            {"id":"deny","label":"❌ Decline","style":"danger"}],
 "session":"mobile:qa-1","timeout_seconds":300}
```

Check the `command`/`description` fields: the app must show the exact command next to the buttons.

Answer with a button:

```json
{"t":"prompt","prompt_id":"<prompt_id>","option_id":"once"}
```

**Expected result:** `{"t":"prompt_done","id":"<prompt_id>","option_id":"once"}` and execution of the
command. The answer to the card **does not** go to the chat as an ordinary message.

**Typical mistake:** you send `{"t":"text","text":"once"}` instead of the `prompt` frame — the choice is
not applied; or a wrong `prompt_id` — you will get `prompt_done{expired:true}`. Safety
check: send `option_id:"allow_all"` — the server will replace it with `deny` and return it in
`prompt_done`.

---

## 5. Media both ways

**Up (client → agent).** First prepare the base64 of a small image and send it:

```json
{"t":"media","id":"mq-1","kind":"image","mime":"image/png","filename":"dot.png","caption":"dot","data":"<base64>","part":0,"parts":1}
```

**Expected result:** the agent receives the image as an attachment (in the log — `MessageType.PHOTO`).
Ask "what is in the picture?" — the agent will describe it.

**Down (agent → client).** Ask the agent to send an image/file. A `media` frame will arrive:

```json
{"t":"media","id":"a1","kind":"image","mime":"image/png","filename":"chart.png","caption":"Chart",
 "part":0,"parts":1,"final":true,"size":20480,"data":"<base64>"}
```

If the file is large — several frames with `part 0..parts-1`, `final:true` on the last one.

**Typical mistake:** `{"t":"error","code":"too_big",…}` — a file > 8 MB sent in one frame.
Split into ~3 MB chunks (`part`/`parts`), sending **all** the chunks. Or `unsupported_frame` — `kind`
outside `image|video|document|audio`.

---

## 6. Streaming: edits arrive with `edit=true`

**How to get:** enable streaming on the agent and ask for a long reply. Observe the frames:

```json
{"t":"message","id":"r1","text":"In progress","edit":true,"chunk":{"index":0,"total":1},"final":false}
{"t":"message","id":"r1","text":"Reply in progress…","edit":true,"chunk":{"index":0,"total":1},"final":true}
```

**Expected result:** `message` frames arrive with the **same** `id` and `edit:true`; your client
redraws one bubble instead of adding new ones.

**Typical mistake:** the client shows each `edit` frame as a new message — duplicates of the
growing text are visible. Redraw by `id`. Remember: if an edit fails, the gateway will send the tail as an
ordinary `message` (without `edit`) — do not treat that as an error.

---

## 7. Avatar states: thinking / speaking / idle

**How to get:** send `{"t":"text","id":"a2","text":"say hi"}` and observe `avatar`.

**Expected result (typical order):**

```json
{"t":"avatar","state":"thinking"}   // + possibly {"t":"typing"}
…message…
{"t":"avatar","state":"speaking"}
{"t":"avatar","state":"idle"}       // after a pause following the reply
```

**Typical mistake:** you rigidly expect exactly three frames — `speaking` may be absent (the avatar is
voiced by the client), and `thinking`/`typing` arrive in different orders. Handle each
state independently, values strictly `idle|thinking|speaking`.

---

## 8. `typing.stop` clears the indicator

**How to get:** during the agent's reply, wait for the frame `{"t":"typing","stop":true}`.

**Expected result:** the "agent is typing" indicator is cleared **immediately**, without waiting for the
avatar timeout; then `{"t":"avatar","state":"idle"}` arrives.

**Typical mistake:** the client clears the indicator only on timeout and ignores `stop:true` —
the indicator "hangs" a couple of seconds longer than needed. `stop` is an explicit signal; handle it
as final.

---

## 9. Rejection with code 1008 on a bad token

**How to get**

```bash
wscat -c "wss://hermes-go.com/mobile-ws"
{"t":"hello","token":"bad-token","device":{"id":"qa-bad"}}
```

**Expected result:** an `error` frame, then the connection closes with code **1008**:

```json
{"t":"error","code":"auth_failed","message":"…","fatal":true}
```

`wscat` will print `Disconnected (code: 1008)`.

**Typical mistake:** the app starts a reconnect loop on 1008 — the server is hammered
for nothing. **Retrying is pointless:** show "no access" and do not reconnect. The same
applies to a `device.id` missing from `MOBILE_ALLOWED_DEVICES`.

---

## 10. An unknown frame does not break the connection

**How to get:** send the server a frame with an unknown type, then normal text:

```json
{"t":"some-new-frame","field":1}
{"t":"text","id":"q-9","text":"does it work?"}
```

**Expected result:** the server ignores the unknown frame and keeps working — you get a
reply to the text. The connection is not broken.

**Typical mistake:** the client itself tears down the connection or crashes with an exception on an **unknown
frame from the server**. Protocol rule: unknown types and optional fields are ignored.
Handle new frames that an updated gateway sends the same way.

---

## Additional

| Check | How | Expectation |
|---|---|---|
| `ping`/`pong` | `{"t":"ping","ts":42}` | `{"t":"pong","ts":42}` |
| empty text | `{"t":"text","id":"z","text":"   "}` | `error{code:"bad_frame"}` |
| text > 12,000 characters | one `text` with a long string | `error{code:"too_big"}` |
| audio not wav/ogg | `{"t":"audio","format":"mp3",…}` | `error{code:"unsupported_frame"}` |
| invalid media `kind` | `{"t":"media","kind":"sticker",…}` | `error{code:"unsupported_frame"}` |
| answer to a foreign `prompt_id` | `{"t":"prompt","prompt_id":"no-such","option_id":"once"}` | `prompt_done{expired:true}`, no error |
| answer to a card of a foreign device | connect under your own `device.id` and answer a card shown to another device | `error{code:"forbidden"}` |
| `device.id` taken by another token | `MOBILE_EXTRA_TOKENS=qa-1=<token>`; connect with another token under `id:"qa-1"` | `error{code:"forbidden"}` + close **1008** |
| `deliver` not from cron | an ordinary (non-loopback) connection sends `{"t":"deliver","id":"x","target":"qa-1","text":"hi"}` | `error{code:"forbidden"}` |
| media upload beyond the memory limit | declare `parts` > 2048 or keep > 8 series / > 8 MiB open in total | `error{code:"bad_frame"}` (`parts`) / `too_big` (volume) |
| cron delivery offline | `hermes send --to <device.id> "text"` with the app closed | result `device_offline` on the agent's machine |

**How to read rejections:** `fatal:true` in an `error` frame — the connection will be closed, no need to retry
(`auth_failed`). `fatal:false` — you can continue (for example, `bad_frame` on a specific
frame). Close code **1008** — always access denied.
