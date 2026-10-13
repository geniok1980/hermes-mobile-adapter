1|1|# How it works
2|2|
3|3|## The adapter is a Hermes platform, not a separate server
4|4|
5|5|`hermes-mobile` is a plugin adapter, an ordinary Hermes platform (the same as Telegram). It
6|6|**raises a WebSocket server inside the gateway process** (`hermes gateway`) and turns
7|7|protocol frames into `MessageEvent`s that go to the agent the same way as the other channels.
8|8|
9|9|```
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
22|22|
23|23|The key takeaway: **the app has no backend of its own**. There is no separate API layer, second
24|24|history, second set of tools and second billing. Whatever the agent can do in Telegram/CLI, it
25|25|can do here too — because it is the same agent.
26|26|
27|27|## Sessions and the device identifier
28|28|
29|29|The session identifier in Hermes is `mobile:<device.id>`. The `device.id` is chosen by the
30|30|**app** and kept constant (for example `pilot-phone`). The following depend on it:
31|31|
32|32|* the session identifier and the entire conversation history (history lives on the Hermes side, not in the app);
33|33|* the delivery address for cron and `hermes send` (`mobile:<device.id>`);
34|34|* allowlist entries.
35|35|
36|36|One user can have several devices — **they are different sessions**. A device that reconnects
37|37|returns to its own session: after a network drop it is enough to send `hello` again, and the
38|38|conversation continues.
39|39|
40|40|If `device.id` is not set, the adapter generates one itself (`dev-<hex>`), but then the delivery
41|41|address changes from connection to connection — always set `device.id`.
42|42|
43|43|## Tokens and allowlists are different things
44|44|
45|45|It is easy to get confused here: **owners/users** and **devices** are different entities and
46|46|different variables.
47|47|
48|48|| Variable | What it lists | Who checks it | What happens on a mismatch |
49|49||---|---|---|---|
50|50|| `MOBILE_TOKEN` | the app's shared token (secret) | the adapter, at handshake | the connection closes with code **1008**, frame `error` → `auth_failed` |
51|51|| `MOBILE_EXTRA_TOKENS` | additional tokens (for different apps/devices); an item `device_id=token` binds a token to a device | the adapter, at handshake | on a taken foreign `device.id` → frame `error` → `forbidden` + **1008** |
52|52|| `MOBILE_DELIVER_TOKENS` | tokens allowed to send the service frame `deliver` outside loopback | the adapter, on the `deliver` frame | `error` → `forbidden` |
53|53|| `MOBILE_ALLOWED_DEVICES` | a list of `device.id` — **devices** | the adapter (an additional adapter filter) | refusal `auth_failed` + **1008** |
54|54|| `MOBILE_ALLOWED_USERS` | a list of **users** — Hermes' built-in authorization and pairing | Hermes itself | the connection is accepted (`ready`), but the agent does not reply — pairing/approval is needed |
55|55|| `MOBILE_ALLOW_ALL_USERS` | `1`/`true` — accept any device with a valid token | Hermes itself | — (removes the user check) |
56|56|| `MOBILE_HOME_DEVICE` | the default device for cron | the adapter | `device_offline` if it is not online |
57|57|
58|58|Rules:
59|59|
60|60|* An empty `MOBILE_ALLOWED_DEVICES` → **any** device with a valid token is allowed.
61|61|* An empty `MOBILE_TOKEN` → the platform **does not start at all** (`config_missing`): the adapter
62|62|  is never "open".
63|63|* `MOBILE_ALLOWED_USERS` is about *users* (who is admitted to the agent), while the *device* filter
64|64|  is `MOBILE_ALLOWED_DEVICES`. Do not confuse them: one name must not mean two different things.
65|65|
66|66|## What happens with memory, tools and cron
67|67|
68|68|* **Memory and history** — on the Hermes side, in the agent profile. Reinstalling the app does not
69|69|  lose them; several devices see their sessions independently.
70|70|* **Tools** — the same as in other channels. Dangerous commands go through the built-in
71|71|  approval: the adapter sends the app a `prompt` card with buttons, and the answer is resolved
72|72|  by Hermes' native mechanism (not as a chat message).
73|73|* **Cron and `hermes send`** — can deliver messages and files to the device. When the gateway
74|74|  is running, a separate process (`standalone_sender_fn`) connects to it as a service client
75|75|  and asks it to hand the frame to the device. Mobile delivery requires a **live** app
76|76|  connection: if the device is not online — `device_offline` is returned, not a "silent" success.
77|77|* **Streaming** — if enabled on the agent side, the reply first arrives in full and is then
78|78|  edited by `id` (`message` frames with `edit: true`).
79|79|
80|80|## Directions of data flow
81|81|
82|82|* **up (app → agent):** `hello`, `text`, `audio`, `media`, `prompt`, `typing`, `ping`,
83|83|  `ack`, `deliver`.
84|84|* **down (agent → app):** `ready`, `message`, `media`, `prompt`, `prompt_done`, `avatar`,
85|85|  `typing`, `pong`, `error`.
86|86|
87|87|A full description of each frame — [Protocol v1](protocol/index.md). What to implement in the
88|88|interface — [App interface](ui/index.md).