# Troubleshooting and FAQ

A "symptom → cause → fix" matrix based on real cases, then frequently asked questions.

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| The connection closes with code **1008**, `error{code:"auth_failed"}` arrives | the token in the app ≠ any of `MOBILE_TOKEN` / `MOBILE_EXTRA_TOKENS`, **or** `device.id` is absent from `MOBILE_ALLOWED_DEVICES` | verify the token with the agent owner; if devices are restricted — add the `device.id` or remove the filter; retrying is pointless |
| `error{code:"forbidden"}` arrives (+ 1008 on connect) | the token is bound to a different `device.id` (`device_id=token`), someone else's id is taken by another token, or a response to a foreign device's card | connect under your own `device.id` (the one bound to the token); a card is closed only by the device it is shown to |
| `error{code:"forbidden"}` on a `deliver` frame | `deliver` is not sent by cron: the connection is not from loopback and the token is not in `MOBILE_DELIVER_TOKENS` | service delivery is performed by cron/`hermes send` from the same machine (loopback); for delivery from outside — add the token to `MOBILE_DELIVER_TOKENS` |
| `error{code:"too_big"}` / `bad_frame` on a media upload | the number of unfinished uploads (8 per device), the device's total buffer (> 8 MiB) or `parts` > 2048 is exceeded | wait for the current uploads to finish; do not keep many series open; split a file larger than 4 KiB into chunks; buffers are freed on disconnect |
| `ready` arrives, but there are no agent responses | the device has not yet been approved by Hermes (pairing) | on the agent machine: `hermes pairing list` → `hermes pairing approve mobile <request-id>`; or, in advance, `MOBILE_ALLOWED_USERS=<user id>` |
| The agent responds: "No home channel is set" / about a pairing code | standard Hermes behaviour on first contact with an unfamiliar user | approve the pairing; or send `/sethome` so that this chat becomes the home channel (for cron delivery) |
| `error{code:"device_offline"}` on cron delivery | the app is not connected at the moment of delivery | mobile delivery requires a **live** connection: keep the app connected at the right moment or deliver to a device that is usually online (`MOBILE_HOME_DEVICE`) |
| Media does not arrive | a chunk series was interrupted (`part`/`parts`), or the 8 MiB limit / 16 MiB frame is exceeded | send **all** `part`s up to `parts-1` with a single `id`; send a large file in chunks of ~3 MiB or compress it; on `too_big` — the same |
| Duplicate messages on resend | a constant `id` is not set, or a new `id` is used on resend | keep `id` constant for the message and repeat it on resend — the server ignores the repeat |
| The connection does not establish: **404** / **426** on the public address | the reverse proxy does not proxy WebSocket (no `Upgrade`), or the path is wrong | proxy the route (`handle /ws*` in Caddy) with an upgrade to WS; verify the path against `MOBILE_PATH` (default `/ws`) |
| The adapter does not start: `bind_failed: не удалось занять 127.0.0.1:8787` | port 8787 is taken by another process | `ss -ltnp \| grep 8787`; free the port or change `MOBILE_PORT` and update the proxy |
| `config_missing: MOBILE_TOKEN не задан` | no token in the profile's `.env` | the platform does not start without a token (protection): add `MOBILE_TOKEN` to `~/.hermes/.env` and restart the gateway |
| `deps_missing` / "no websockets" | the `websockets` package is unavailable in the environment | the adapter does **not** install dependencies: the package is part of Hermes — update Hermes |
| The plugin is not visible in `hermes plugins list` | wrong path or not enabled in the config | check `~/.hermes/plugins/<name>/plugin.yaml` and `plugins.enabled` in `config.yaml` |
| First frame is not `hello` → `error{code:"bad_frame"}` + 1008 | the client sent `text`/`media` before the handshake | always send `hello` as the first frame |
| The card does not close / the response is "swallowed" | the card expired (`prompt_done{expired:true}`) or the `prompt_id` is wrong | respond with a value from `options` immediately; once expired, the card simply closes |
| Audio format not accepted: `unsupported_frame` | `format` is not `wav`/`ogg` | send WAV 16 kHz mono or OGG |
| Unrecognized media type: `unsupported_frame` | `kind` is outside `image`/`video`/`document`/`audio` | use the allowed values |

!!! tip "Manual check"
    `wscat -c "ws://127.0.0.1:8787/ws"`, then send
    `{"t":"hello","token":"<token>","device":{"id":"cli"}}` as the first frame — `ready` should arrive (a token in the URL
    `?token=` is not accepted). Then `{"t":"text","id":"t1","text":"hi"}`.

## FAQ

**Is the SDK required?**
No. The protocol is JSON frames over WebSocket; any runtime will do. The SDK (TypeScript/Python)
takes care of reconnection, slicing/gluing and frame parsing.

**What is `session` in the `ready` frame?**
The session identifier in Hermes: `mobile:<device.id>`. It is also the delivery address for
`hermes send --to mobile:<device.id>` and cron.

**Where does the history go if the app is reinstalled?**
Nowhere: the history and memory live on the Hermes side in the agent profile. The app is only a client.

**Can I use one `device.id` on several phones?**
Technically yes, but it will be **one session** for all of them. For separate dialogues, give each
device its own `device.id`. With a device-bound token (`MOBILE_EXTRA_TOKENS=device_id=token`),
someone else's id cannot be taken — `forbidden` will arrive.

**What is `MOBILE_DELIVER_TOKENS`?**
The `deliver` frame (service delivery from cron) is accepted only from a loopback address or from a connection
with a token from this list; to everyone else — `forbidden`. In the typical setup (cron on the same machine)
there is no need to set the variable — a loopback connection is allowed anyway.

**How does `MOBILE_ALLOWED_USERS` differ from `MOBILE_ALLOWED_DEVICES`?**
The former concerns **users** (Hermes's standard authorization and pairing), the latter concerns **devices**
(an additional adapter filter). These are different variables and different entities — see
[How it works](concepts.md).

**Why does the response not arrive at once, but gets "drawn in"?**
If streaming is enabled on the agent side: first the message arrives in full, then edits as
`message` frames with `edit: true` and the same `id`. Redraw by `id`, otherwise you will see duplicates.

**Does delivery work when the app is closed?**
No. Mobile delivery (cron, `hermes send`) requires a live connection: otherwise
`device_offline` is returned. The history is not lost, though — the agent serves the session even without a mobile client.

**How do I revoke access for one device?**
Remove its token from `MOBILE_TOKEN`/`MOBILE_EXTRA_TOKENS` (or delete the `device.id` from
`MOBILE_ALLOWED_DEVICES`) and restart the gateway: `hermes gateway restart`. More — in
[Security](security.md).
