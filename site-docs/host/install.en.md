# Installing the adapter on the agent's machine

The adapter is a Hermes Agent plugin. It is installed where `hermes gateway` runs.

## 1. Plugin

```bash
cp -r adapter/hermes_mobile ~/.hermes/plugins/hermes-mobile        # default profile
hermes plugins list | grep hermes-mobile                          # the plugin is visible (source: user)
```

Enable the plugin and the platform in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-mobile
gateway:
  platforms:
    mobile:
      enabled: true
```

The plugin requires Hermes **`>=0.21.6`** (`requires_hermes` in `plugin.yaml`) and **does not install
dependencies itself**: the `websockets` package is part of the Hermes environment (if it is missing — update Hermes).

## 2. Token and parameters

```bash
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
printf 'MOBILE_HOST=127.0.0.1\nMOBILE_PORT=8787\nMOBILE_PATH=/ws\n' >> ~/.hermes/.env
```

| Variable | Purpose |
|---|---|
| `MOBILE_TOKEN` | required shared app secret (WS authorization) |
| `MOBILE_HOST` / `MOBILE_PORT` / `MOBILE_PATH` | where the adapter listens (**default `127.0.0.1:8787/ws`** — loopback) |
| `MOBILE_AVATAR_URL` | the avatar HTML page that the app opens (delivered in the `ready` frame) |
| `MOBILE_ALLOWED_USERS` | comma-separated list of users — checked by Hermes itself (authorization, pairing) |
| `MOBILE_ALLOWED_DEVICES` | comma-separated list of `device.id` — an additional adapter filter (empty — any device with a valid token) |
| `MOBILE_ALLOW_ALL_USERS` | `1`/`true` — accept any device with a valid token without checking against the allowlist (handy for a quick check and development; in production prefer `MOBILE_ALLOWED_USERS`) |
| `MOBILE_EXTRA_TOKENS` | additional tokens — if there are several apps/developers. An item of the form `device_id=token` **binds a token to a device**: it cannot take a foreign `device.id` |
| `MOBILE_DELIVER_TOKENS` | list of tokens allowed the service `deliver` frame (from cron) outside loopback |
| `MOBILE_HOME_DEVICE` | default device for cron delivery (`deliver=mobile`) |

**Loopback by default.** The adapter listens on `127.0.0.1:8787/ws` and is accessible only from the agent's
machine itself. For remote access **deliberately** set `MOBILE_HOST=0.0.0.0` and **necessarily**
close it with a wss proxy (TLS) and firewall — do not expose the port outward "as is".

**Service delivery (`deliver`).** The `deliver` frame (cron → device) is accepted only from a
loopback address or from a connection with a token from `MOBILE_DELIVER_TOKENS`; for others — `forbidden`.
This variable is needed when the cron process connects to the gateway not over loopback.

The same parameters can be set without `.env` — in `~/.hermes/config.yaml` in the block
`gateway.platforms.mobile.extra` (keys without the `MOBILE_` prefix: `host`, `port`, `path`,
`avatar_url`, `allowed_users`, `extra_tokens`, `deliver_tokens`, `home_device`). Environment variables
take priority: if a value is set in `.env`, it overrides `config.yaml`.

```yaml
gateway:
  platforms:
    mobile:
      enabled: true
      extra:
        host: 127.0.0.1
        port: 8787
        avatar_url: https://agent.example.com/avatar/
        allowed_users: [ "tel-1", "tablet-2" ]
```

Interactively: `hermes gateway setup` → **Hermes Go Mobile** (the wizard will generate a token).

## 3. Start and verify

```bash
hermes gateway restart
ss -ltn | grep 8787
```

Verification from the terminal:

```bash
python - <<'PY'
import asyncio, json, websockets
async def main():
    token = "<MOBILE_TOKEN>"
    async with websockets.connect("ws://127.0.0.1:8787/ws") as ws:
        await ws.send(json.dumps({"t": "hello", "token": token, "device": {"id": "cli-1", "name": "Check"}}))
        print("ready:", await ws.recv())
        await ws.send(json.dumps({"t": "text", "id": "t1", "text": "hi"}))
        for _ in range(10):
            print("frame:", await ws.recv())
asyncio.run(main())
PY
```

An unknown device's first contact is met by Hermes with a pairing code — this is normal:

```bash
hermes pairing list
hermes pairing approve mobile <request-id>     # or <the code from the message>
```

Or in advance: `MOBILE_ALLOWED_DEVICES=<device.id>` (device filter) or
`MOBILE_ALLOWED_USERS=<user id>` (Hermes' built-in authorization, without pairing).

## 4. Outward — HTTPS/WSS

The app connects over `wss://`, so we publish the WebSocket through a reverse proxy. Example for Caddy:

```
agent.example.com {
    handle /ws* {
        reverse_proxy 127.0.0.1:8787
    }
}
```

Verification: `wscat -c "wss://agent.example.com/ws"` and send
`{"t":"hello","token":"<token>"}` as the first frame — a token in the URL (`?token=`) is not accepted.

If the gateway is in a container — publish the port and do not forget about the firewall (usually we expose only
80/443 outward and proxy the adapter). In our stand this is the Caddy `rkdash-caddy` on the network
`hermes-saas_default`.

## 5. Several apps

* Different teams/apps — different tokens: `MOBILE_TOKEN` (the first) and
  `MOBILE_EXTRA_TOKENS=tok2,tok3` (the rest). Each gets its own `device.id`; you can bind them right away:
  `MOBILE_EXTRA_TOKENS=tel-1=tok2,tablet-2=tok3`.
* Limit which devices connect at all: `MOBILE_ALLOWED_DEVICES=app-a,app-b`
  (which users are admitted — decided by Hermes' built-in authorization: `MOBILE_ALLOWED_USERS`, pairing).
* Sessions in Hermes are separated by `device.id`, so conversations do not mix.

## 6. Delivery from cron

```bash
hermes cron add "reminder" --schedule "0 9 * * *" --deliver mobile:app-a
```

Works via `standalone_sender_fn`: the process connects to the running gateway as
a service client and asks it to deliver a message to the device. If the gateway is not running or the device
is offline — a delivery error is returned (mobile delivery requires a live connection).

## 7. Update

```bash
rsync -a --delete adapter/hermes_mobile/ ~/.hermes/plugins/hermes-mobile/
hermes gateway restart
```

## 8. Common problems

| Symptom | Fix |
|---|---|
| `bind_failed: failed to bind 127.0.0.1:8787` | the port is busy: `ss -ltnp \| grep 8787`, change `MOBILE_PORT` |
| `config_missing: MOBILE_TOKEN is not set` | there is no token in the profile's `.env` — the platform does not come up without a token (this is a safeguard: the adapter is never "open") |
| the app receives `auth_failed` | the token in the app ≠ `MOBILE_TOKEN`, or the device is outside `MOBILE_ALLOWED_DEVICES`, or the user is not approved by Hermes (`MOBILE_ALLOWED_USERS` / pairing) |
| `ready` arrives, no replies | the device is not approved (`hermes pairing approve mobile …`) |
| the plugin is not visible in `hermes plugins list` | check the path `~/.hermes/plugins/<name>/plugin.yaml` and `plugins.enabled` in `config.yaml` |
| `websockets` is not installed | the adapter **does not install** dependencies: `websockets` is part of Hermes — update Hermes if the package is missing |

## 9. Verifying cards and media by hand

Connect with `wscat` (or the python script from §3) and go through the scenarios below. Substitute your
own token; it is not shown in the frame examples.

**1) Make sure the capabilities are declared.** In response to `hello` the server sends `ready`; in
`capabilities` there should be the new fields:

```json
{"t":"ready","v":1,"session":"mobile:cli-1","capabilities":{"text":true,"voice":true,"typing":true,"avatar_states":["idle","thinking","speaking"],"media_in":["image","video","document","audio"],"media_out":["image","voice","video","document","animation"],"prompts":["exec_approval","slash_confirm","clarify"],"edit":true,"max_media_bytes":8388608,"max_text_chars":12000}}
```

**2) Send media to the agent.** A small file — as one frame (`part`/`parts` can be
omitted, then it is `parts:1`):

```json
{"t":"media","id":"m1","kind":"image","mime":"image/png","filename":"test.png","caption":"check","data":"iVBORw0KGgoAAAANSUhEUg…"}
```

The agent will receive the photo with the caption. A file larger than 8 MB — send in chunks: `{"...","part":0,"parts":2,...}`
and `{"...","part":1,"parts":2,...}` with the same `id`.

**3) Wait for a card.** Ask the agent to do something dangerous (or to clarify the request) —
a `prompt` frame will arrive:

```json
{"t":"prompt","id":"5f6a…","kind":"exec_approval","text":"Dangerous command rm -rf …","command":"rm -rf /tmp/build","description":"Deletes the temporary build directory","options":[{"id":"once","label":"✅ Approve once","style":"primary"},{"id":"deny","label":"❌ Decline","style":"danger"}],"session":"mobile:cli-1","timeout_seconds":300}
```

Show `command` next to the buttons — the person must see exactly what they are approving. Only the device it is
shown to can close the card: an answer from another connection gets `forbidden`.

**4) Answer with one frame.** `option_id` — from `options`. The answer does not go to the chat but resolves
the request; the server confirms with a `prompt_done` frame:

```
→ {"t":"prompt","prompt_id":"5f6a…","option_id":"deny"}
← {"t":"prompt_done","id":"5f6a…","option_id":"deny"}
```

An invalid `option_id` (for example `"whatever"`) will be replaced by the server with a safe one (`deny` for
approval, `cancel` for confirmation) — this is visible from `option_id` in `prompt_done`. If you
answer nothing, after `timeout_seconds` `{"t":"prompt_done","id":"5f6a…","expired":true}` will arrive.

**5) Receive media from the agent.** If the agent sends an image/file, a `media` frame will arrive with
chunks and `final`:

```json
{"t":"media","id":"a1","kind":"image","mime":"image/png","filename":"chart.png","caption":"Chart","part":0,"parts":1,"final":true,"size":20480,"data":"iVBORw0KGgo…"}
```

Assemble `data` by `id` and save the file when `final:true` arrives.
