# How it works

## The adapter is a Hermes platform, not a separate server

`hermes-mobile` is a plugin adapter, an ordinary Hermes platform (the same as Telegram). It
**raises a WebSocket server inside the gateway process** (`hermes gateway`) and turns
protocol frames into `MessageEvent`s that go to the agent the same way as the other channels.

```
        your app                      agent machine (hermes gateway)
   ┌────────────────────┐        ┌────────────────────────────────────────────┐
   │ HermesClient / SDK │        │  hermes-mobile plugin  (WS server :8787/ws) │
   │  text/audio/media  │ ─────► │        │                                   │
   │  prompt (buttons)  │        │        ▼  MessageEvent                     │
   └────────────────────┘        │   ┌─────────────────────────────┐          │
            ▲                    │   │  Hermes Agent               │          │
            │  message/media     │   │  sessions · memory · tools  │          │
            │  avatar/typing     │   │  cron · billing · history   │          │
            └────────────────────│   └─────────────────────────────┘          │
                                 └────────────────────────────────────────────┘
```

The key takeaway: **the app has no backend of its own**. There is no separate API layer, second
history, second set of tools and second billing. Whatever the agent can do in Telegram/CLI, it
can do here too — because it is the same agent.

## Sessions and the device identifier

The session identifier in Hermes is `mobile:<device.id>`. The `device.id` is chosen by the
**app** and kept constant (for example `pilot-phone`). The following depend on it:

* the session identifier and the entire conversation history (history lives on the Hermes side, not in the app);
* the delivery address for cron and `hermes send` (`mobile:<device.id>`);
* allowlist entries.

One user can have several devices — **they are different sessions**. A device that reconnects
returns to its own session: after a network drop it is enough to send `hello` again, and the
conversation continues.

If `device.id` is not set, the adapter generates one itself (`dev-<hex>`), but then the delivery
address changes from connection to connection — always set `device.id`.

## Tokens and allowlists are different things

It is easy to get confused here: **owners/users** and **devices** are different entities and
different variables.

| Variable | What it lists | Who checks it | What happens on a mismatch |
|---|---|---|---|
| `MOBILE_TOKEN` | the app's shared token (secret) | the adapter, at handshake | the connection closes with code **1008**, frame `error` → `auth_failed` |
| `MOBILE_EXTRA_TOKENS` | additional tokens (for different apps/devices); an item `device_id=token` binds a token to a device | the adapter, at handshake | on a taken foreign `device.id` → frame `error` → `forbidden` + **1008** |
| `MOBILE_DELIVER_TOKENS` | tokens allowed to send the service frame `deliver` outside loopback | the adapter, on the `deliver` frame | `error` → `forbidden` |
| `MOBILE_ALLOWED_DEVICES` | a list of `device.id` — **devices** | the adapter (an additional adapter filter) | refusal `auth_failed` + **1008** |
| `MOBILE_ALLOWED_USERS` | a list of **users** — Hermes' built-in authorization and pairing | Hermes itself | the connection is accepted (`ready`), but the agent does not reply — pairing/approval is needed |
| `MOBILE_ALLOW_ALL_USERS` | `1`/`true` — accept any device with a valid token | Hermes itself | — (removes the user check) |
| `MOBILE_HOME_DEVICE` | the default device for cron | the adapter | `device_offline` if it is not online |

Rules:

* An empty `MOBILE_ALLOWED_DEVICES` → **any** device with a valid token is allowed.
* An empty `MOBILE_TOKEN` → the platform **does not start at all** (`config_missing`): the adapter
  is never "open".
* `MOBILE_ALLOWED_USERS` is about *users* (who is admitted to the agent), while the *device* filter
  is `MOBILE_ALLOWED_DEVICES`. Do not confuse them: one name must not mean two different things.

## What happens with memory, tools and cron

* **Memory and history** — on the Hermes side, in the agent profile. Reinstalling the app does not
  lose them; several devices see their sessions independently.
* **Tools** — the same as in other channels. Dangerous commands go through the built-in
  approval: the adapter sends the app a `prompt` card with buttons, and the answer is resolved
  by Hermes' native mechanism (not as a chat message).
* **Cron and `hermes send`** — can deliver messages and files to the device. When the gateway
  is running, a separate process (`standalone_sender_fn`) connects to it as a service client
  and asks it to hand the frame to the device. Mobile delivery requires a **live** app
  connection: if the device is not online — `device_offline` is returned, not a "silent" success.
* **Streaming** — if enabled on the agent side, the reply first arrives in full and is then
  edited by `id` (`message` frames with `edit: true`).

## Directions of data flow

* **up (app → agent):** `hello`, `text`, `audio`, `media`, `prompt`, `typing`, `ping`,
  `ack`, `deliver`.
* **down (agent → app):** `ready`, `message`, `media`, `prompt`, `prompt_done`, `avatar`,
  `typing`, `pong`, `error`.

A full description of each frame — [Protocol v1](protocol/index.md). What to implement in the
interface — [App interface](ui/index.md).
