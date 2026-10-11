# Hermes Mobile Adapter (plugin: `hermes-mobile`)

Adds a **WebSocket platform** to the Hermes gateway so *your own* mobile / desktop / web
application can talk to a Hermes Agent directly — no second backend, no duplicated history.

Messages from the app land in the same sessions, tools and memory as Telegram, Discord or the
CLI; agent replies come back with typing indicators, streaming edits and avatar state events
(`thinking` → `speaking` → `idle`).

```
your app  ⇄  ws(s)://<agent-host>:8787/ws  ⇄  Hermes gateway  →  agent, sessions, memory
```

## What it provides

| Area | Details |
|---|---|
| Inbound | text, voice (WAV/OGG, 16 kHz), images, video, documents, arbitrary files |
| Outbound | text (chunked for small screens), typing indicator, streaming edits, images/voice/video/documents |
| Interactive cards | command-approval prompts, `clarify` questions, slash-command confirmations — answered from the app |
| Media | files up to 8 MB per frame, larger payloads split into `part`/`parts` frames and reassembled; inbound files saved under `$HERMES_HOME/mobile-media` |
| Delivery | cron/`hermes send` delivery to a chosen device, attachments included |
| Protocol | versioned JSON frames (`protocol v1`), idempotency by message `id` |
| SDKs | TypeScript (`hermes-mobile-client` on npm) and Python, plus platform examples |

Only public plugin surfaces are used: `ctx.register_platform()` and the adapter ABC. No Hermes
core files are patched, nothing is monkey-patched at runtime, and the plugin never updates
itself.

## Install

```bash
# once listed in the catalog:
hermes plugins install hermes-mobile
# or straight from this repository (the plugin lives in a subdirectory):
hermes plugins install geniok1980/hermes-mobile-adapter/adapter/hermes_mobile
```

Manual install: copy `adapter/hermes_mobile/` to `~/.hermes/plugins/hermes-mobile/`, then enable
it and the platform:

```yaml
# ~/.hermes/config.yaml
plugins:
  enabled: [hermes-mobile]
gateway:
  platforms:
    mobile:
      enabled: true
```

```bash
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
hermes gateway restart
hermes status                      # mobile shows as (plugin)
hermes pairing approve mobile <id> # first contact from a new user needs approval
```

Full walkthrough (HTTPS/WSS in front, firewall, verification):
<https://geniok1980.github.io/hermes-mobile-adapter/host/install/>

## Configuration

Required:

| Variable | Meaning |
|---|---|
| `MOBILE_TOKEN` | shared secret the app sends in the `hello` frame |

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `MOBILE_HOST` | `0.0.0.0` | listen address |
| `MOBILE_PORT` | `8787` | listen port |
| `MOBILE_PATH` | `/ws` | WebSocket path |
| `MOBILE_ALLOWED_USERS` | — | comma-separated user ids allowed to talk to the agent (Hermes authorization) |
| `MOBILE_ALLOW_ALL_USERS` | — | `1` accepts any user with a valid token (development convenience) |
| `MOBILE_ALLOWED_DEVICES` | — | comma-separated device ids — an extra adapter-level filter, separate from users |
| `MOBILE_EXTRA_TOKENS` | — | additional tokens, for one token per device |
| `MOBILE_HOME_DEVICE` | — | device id used for unattended (cron) delivery |
| `MOBILE_AVATAR_URL` | — | avatar page URL handed to the app in `ready` |

`hermes config` renders all of these (`hermes gateway setup` too).

## Disclosure (what to know before installing)

* **Network.** The plugin opens a listening WebSocket server on `MOBILE_HOST` / `MOBILE_PORT`
  — the default host is the wildcard address (all interfaces) and the default port is 8787. Set
  `MOBILE_HOST=127.0.0.1` unless remote devices must reach it, and put `wss://` (TLS) plus a
  firewall in front when you expose it. Anyone who obtains the token can talk to the agent; use
  `MOBILE_EXTRA_TOKENS` for a token per device and revoke by removing it.
* **Files on disk.** Inbound attachments (images, video, documents, voice) are written to
  `$HERMES_HOME/mobile-media`. Nothing is uploaded anywhere by the plugin.
* **Credentials.** Secrets are read through Hermes's profile-scoped secret route
  (`get_scoped_secret`); the plugin does not parse `.env` files, does not read `auth.json`,
  keychains or any other credential store, and never refreshes or writes tokens.
* **Approvals.** Command approvals keep going through Hermes: the adapter renders the approval
  card in the app and resolves it through Hermes's own resolver. Nothing is auto-approved, and
  unattended (cron) runs fail cleanly instead of hanging on a prompt.
* **No telemetry**, no third-party network calls, no shell commands started by the plugin
  itself, no self-updating code.

## Compatibility

* `requires_hermes: ">=0.21.3"`.
* Dependency: `websockets` (already present in the Hermes environment). No extra pip installs.
* Protocol v1 is stable; unknown frames are ignored by clients so new fields can be added
  without breaking older apps.

## Links

* Documentation: <https://geniok1980.github.io/hermes-mobile-adapter/>
* Protocol reference: <https://geniok1980.github.io/hermes-mobile-adapter/protocol/>
* TypeScript / Python SDK reference: <https://geniok1980.github.io/hermes-mobile-adapter/reference/typescript/>
* Integration guides (React Native, Flutter, Swift, Kotlin, web) and acceptance checklist:
  <https://geniok1980.github.io/hermes-mobile-adapter/guides/react-native/>
* License: MIT.
