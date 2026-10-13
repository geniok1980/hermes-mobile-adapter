# For the agent owner

A short page: how to raise the `mobile` platform on the agent's machine and grant access to a
developer. The full version with manual checks — `../../host/install.md` in the repository.

## 1. Install the plugin

```bash
cp -r adapter/hermes_mobile ~/.hermes/plugins/hermes-mobile      # default profile
hermes plugins list | grep hermes-mobile                         # the plugin is visible (source: user)
```

## 2. Enable the platform in `config.yaml`

```yaml
plugins:
  enabled:
    - hermes-mobile
gateway:
  platforms:
    mobile:
      enabled: true
```

## 3. Token and parameters

```bash
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
printf 'MOBILE_HOST=127.0.0.1\nMOBILE_PORT=8787\nMOBILE_PATH=/ws\n' >> ~/.hermes/.env
```

| Variable | Purpose |
|---|---|
| `MOBILE_TOKEN` | **required** shared app secret (WS authorization) |
| `MOBILE_HOST` / `MOBILE_PORT` / `MOBILE_PATH` | where the adapter listens (**default `127.0.0.1:8787/ws`** — loopback) |
| `MOBILE_AVATAR_URL` | the avatar HTML page, delivered to the app in the `ready` frame |
| `MOBILE_ALLOWED_USERS` | list of users — checked by Hermes itself (authorization, pairing) |
| `MOBILE_ALLOWED_DEVICES` | list of `device.id` — a separate adapter filter (empty = any device with a valid token) |
| `MOBILE_ALLOW_ALL_USERS` | `1`/`true` — accept any device with a valid token (for debugging) |
| `MOBILE_EXTRA_TOKENS` | additional tokens — if there are several apps/developers. An item `device_id=token` binds a token to a device (a foreign `device.id` cannot be taken with it) |
| `MOBILE_DELIVER_TOKENS` | tokens allowed the service `deliver` frame (cron) outside loopback |
| `MOBILE_HOME_DEVICE` | default device for cron delivery |

The same parameters can be set in `config.yaml` → `gateway.platforms.mobile.extra` (keys without the
`MOBILE_` prefix). Environment variables take priority. Interactively — `hermes gateway setup`.

**Loopback by default.** The adapter listens on `127.0.0.1:8787/ws` — accessible only from the agent's machine.
For remote access **deliberately** set `MOBILE_HOST=0.0.0.0` and close it with a `wss` proxy
(TLS) and firewall. The plugin requires Hermes **`>=0.21.6`** and installs no dependencies (`websockets` is part
of Hermes).

## 4. Start and verify

```bash
hermes gateway restart
ss -ltn | grep 8787          # the adapter is listening
```

Verification from the terminal (substitute your own token):

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

## 5. Proxy `wss` outward

The app connects over `wss://`, so the WebSocket is published through a reverse proxy. Example for Caddy:

```
agent.example.com {
    handle /ws* {
        reverse_proxy 127.0.0.1:8787
    }
}
```

Verification: `wscat -c "wss://agent.example.com/ws"` and send
`{"t":"hello","token":"<token>"}` as the first frame — a token in the URL (`?token=`) is not accepted. Outward we usually expose only
80/443 and proxy the adapter. `ws://` without TLS is acceptable only on a local network or for debugging.

## 6. Grant access to a developer

Give the developer the **address** (`wss://agent.example.com/ws`), the **token** and, if needed,
the **user id** and the **device id**. Options for separating access:

* Different teams/apps — different tokens: `MOBILE_TOKEN` (the first) and
  `MOBILE_EXTRA_TOKENS=tok2,tok3`. Each gets its own `device.id`; you can bind them right away:
  `MOBILE_EXTRA_TOKENS=tel-1=tok2,tablet-2=tok3` — then a token cannot take a foreign `device.id`.
* Limit which devices connect at all: `MOBILE_ALLOWED_DEVICES=app-a,app-b`.
* Which users are admitted — decided by Hermes' built-in authorization: `MOBILE_ALLOWED_USERS`
  or pairing (`hermes pairing approve mobile <id>` after the first contact).

!!! warning "A single shared token in a public build"
    If other people download your app, do not embed a single shared token in it: any
    owner of the build will get access to your agent. Every device needs its own token
    (`MOBILE_EXTRA_TOKENS` or pairing). See [Security](../security.md).

## 7. Delivery from cron

```bash
hermes cron add "reminder" --schedule "0 9 * * *" --deliver mobile:app-a
```

Works via `standalone_sender_fn`: a separate process connects to the running gateway as a
service client. If the gateway is not running or the device is offline — a delivery error is returned.

## 8. Update

```bash
rsync -a --delete adapter/hermes_mobile/ ~/.hermes/plugins/hermes-mobile/
hermes gateway restart
```

Diagnostics of common problems — [Troubleshooting and FAQ](../troubleshooting.md).
