1|1|# Hermes Mobile Adapter
2|2|
3|3|A mobile app talks to **Hermes Agent** directly over WebSocket — **with no backend of its
4|4|own**. Messages from the phone land in the same sessions, history, tools and memory of the
5|5|agent as Telegram/CLI, and replies come back with a "typing" indicator, avatar states
6|6|(`thinking` → `speaking` → `idle`), button cards and media in both directions.
7|7|
8|8|```
┌───────────────────────┐   WebSocket: text, voice, media, cards   ┌────────────────────────────┐
│  your app             │  ⇄  ws(s)://<agent-host>:8787/ws         │  Hermes Agent (gateway)    │
│  Swift / Kotlin / RN  │                                          │  hermes-mobile plugin      │
│  / Flutter / Web      │                                          │  → agent, sessions, memory │
└───────────────────────┘                                          └────────────────────────────┘
```
15|15|
16|16|## Who this is for
17|17|
18|18|* **App developers** — you are building a mobile client (native, RN, Flutter, web) and
19|19|  want your agent running inside it, but you do not want to build and maintain the server
20|20|  side: LLM routing, history, tools, memory, cron. Hermes already has all of it — the app
21|21|  just talks to it over WebSocket.
22|22|* **Agent owners** — you run `hermes gateway`; you install the adapter plugin, enable the
23|23|  `mobile` platform in `config.yaml`, and hand the developer an address and a token.
24|24|
25|25|## What it gives you
26|26|
27|27|* **No second backend.** The adapter is an ordinary Hermes platform: it raises a WebSocket
28|28|  server inside the gateway and turns protocol frames into `MessageEvent`. There is no
29|29|  separate API layer, second history, second billing or second set of tools.
30|30|* **The same agent as in the other channels.** Sessions and memory live in Hermes, not in
31|31|  the app; user decisions (approving dangerous commands, clarifications) are applied by
32|32|  Hermes' native mechanisms.
33|33|* **The protocol is JSON frames.** No SDK lock-in: any runtime with WebSocket will do.
34|34|  SDKs (TypeScript/Python) and examples are included, but not required.
35|35|* **Cron can deliver to the device** (`hermes send --to mobile:<device.id>`): reminders and
36|36|  reports arrive in the app while it is connected.
37|37|
38|38|## Two usage scenarios
39|39|
40|40|**1. Your own agent (one owner, one to a few devices of your own).**
41|41|You own both the agent and the app. You install the adapter on the agent machine, generate
42|42|`MOBILE_TOKEN`, embed it in the app (or hand it to the device), and connect. You can restrict
43|43|access with a device allowlist. See [For the agent owner](host/index.md) and
44|44|[Getting started](quickstart/index.md).
45|45|
46|46|**2. An app for other people's agents (a public product).**
47|47|You publish an app, and the agents are owned by different people. Then **a shared token must
48|48|not be embedded in the build**: every device needs its own token (`MOBILE_EXTRA_TOKENS` on the
49|49|owner's side, or Hermes' built-in pairing), and the user enters their own agent's address and
50|50|token in the app. For details — [Security](security.md).
51|51|
52|52|## Documentation map
53|53|
54|54|| Section | What it covers |
55|55||---|---|
56|56|| [Getting started](quickstart/index.md) | from zero to the agent's first reply: install, token, example, checking without code |
57|57|| [For the agent owner](host/index.md) | installing the plugin, enabling the platform, token, `wss` through a proxy, granting access |
58|58|| [How it works](concepts.md) | the adapter as a Hermes platform, sessions and `device.id`, tokens and allowlists, memory/tools/cron |
59|59|| [Protocol v1](protocol/index.md) | full reference: all frames, fields, limits, states, error codes, versioning |
60|60|| [App interface](ui/index.md) | what must be implemented in the UI and what happens if it is not |
61|61|| [Security](security.md) | the token as the only secret, per-device tokens, `wss` only, revoking access |
62|62|| [Troubleshooting and FAQ](troubleshooting.md) | a "symptom → cause → fix" matrix and common questions |
63|63|| [SDK reference](reference/index.md) | TypeScript and Python protocol clients |
64|64|| [Platform guides](guides/react-native.md) | React Native, Flutter, iOS, Android, web, recipes, conformance |
65|65|
66|66|The specification-level protocol also lives in the repository: `../protocol/index.md`;
67|67|a practical embedding guide — `../guides/integration.md`.