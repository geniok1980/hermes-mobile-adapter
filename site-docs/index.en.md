# Hermes Mobile Adapter

A mobile app talks to **Hermes Agent** directly over WebSocket — **with no backend of its
own**. Messages from the phone land in the same sessions, history, tools and memory of the
agent as Telegram/CLI, and replies come back with a "typing" indicator, avatar states
(`thinking` → `speaking` → `idle`), button cards and media in both directions.

```
┌───────────────────────┐   WebSocket: text, voice, media, cards   ┌────────────────────────────┐
│  your app             │  ⇄  ws(s)://<agent-host>:8787/ws         │  Hermes Agent (gateway)    │
│  Swift / Kotlin / RN  │                                          │  hermes-mobile plugin      │
│  / Flutter / Web      │                                          │  → agent, sessions, memory │
└───────────────────────┘                                          └────────────────────────────┘
```

## Who this is for

* **App developers** — you are building a mobile client (native, RN, Flutter, web) and
  want your agent running inside it, but you do not want to build and maintain the server
  side: LLM routing, history, tools, memory, cron. Hermes already has all of it — the app
  just talks to it over WebSocket.
* **Agent owners** — you run `hermes gateway`; you install the adapter plugin, enable the
  `mobile` platform in `config.yaml`, and hand the developer an address and a token.

## What it gives you

* **No second backend.** The adapter is an ordinary Hermes platform: it raises a WebSocket
  server inside the gateway and turns protocol frames into `MessageEvent`. There is no
  separate API layer, second history, second billing or second set of tools.
* **The same agent as in the other channels.** Sessions and memory live in Hermes, not in
  the app; user decisions (approving dangerous commands, clarifications) are applied by
  Hermes' native mechanisms.
* **The protocol is JSON frames.** No SDK lock-in: any runtime with WebSocket will do.
  SDKs (TypeScript/Python) and examples are included, but not required.
* **Cron can deliver to the device** (`hermes send --to mobile:<device.id>`): reminders and
  reports arrive in the app while it is connected.

## Two usage scenarios

**1. Your own agent (one owner, one to a few devices of your own).**
You own both the agent and the app. You install the adapter on the agent machine, generate
`MOBILE_TOKEN`, embed it in the app (or hand it to the device), and connect. You can restrict
access with a device allowlist. See [For the agent owner](host/index.md) and
[Getting started](quickstart/index.md).

**2. An app for other people's agents (a public product).**
You publish an app, and the agents are owned by different people. Then **a shared token must
not be embedded in the build**: every device needs its own token (`MOBILE_EXTRA_TOKENS` on the
owner's side, or Hermes' built-in pairing), and the user enters their own agent's address and
token in the app. For details — [Security](security.md).

## Documentation map

| Section | What it covers |
|---|---|
| [Getting started](quickstart/index.md) | from zero to the agent's first reply: install, token, example, checking without code |
| [For the agent owner](host/index.md) | installing the plugin, enabling the platform, token, `wss` through a proxy, granting access |
| [How it works](concepts.md) | the adapter as a Hermes platform, sessions and `device.id`, tokens and allowlists, memory/tools/cron |
| [Protocol v1](protocol/index.md) | full reference: all frames, fields, limits, states, error codes, versioning |
| [App interface](ui/index.md) | what must be implemented in the UI and what happens if it is not |
| [Security](security.md) | the token as the only secret, per-device tokens, `wss` only, revoking access |
| [Troubleshooting and FAQ](troubleshooting.md) | a "symptom → cause → fix" matrix and common questions |
| [SDK reference](reference/index.md) | TypeScript and Python protocol clients |
| [Platform guides](guides/react-native.md) | React Native, Flutter, iOS, Android, web, recipes, conformance |

The specification-level protocol also lives in the repository: `../protocol/index.md`;
a practical embedding guide — `../guides/integration.md`.
