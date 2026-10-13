# Getting started: from zero to the agent's first reply

This path assumes that the **agent owner** has already raised the adapter and handed you an
address and a token. If you are the owner yourself — start with [For the agent owner](../host/index.md).

## 1. Get from the agent owner

| What | Example | Why |
|---|---|---|
| Adapter address | `wss://agent.example.com/ws` | the connection point (WebSocket) |
| App token | 64 hex characters | connection authorization |
| User identifier | `dev-pilot` | the user Hermes sees you as |
| (optional) device id | `pilot-phone` | your own session and cron delivery address |

The address and token are the only thing you need from the owner. Keep the token in the
Keychain/Keystore, not in code. For a public app **every device needs its own token** — see
[Security](../security.md). The agent owner can bind a token to a device
(`MOBILE_EXTRA_TOKENS=device_id=token`): then your `device.id` cannot be taken with a foreign
token, and service cron delivery is allowed only by loopback or `MOBILE_DELIVER_TOKENS`.

## 2. Install the client

=== "TypeScript / JS"

    ```bash
    npm i hermes-mobile-client
    ```

=== "Python"

    ```bash
    pip install websockets>=12
    # the client lives in the repository: sdk/python/hermes_client.py
    ```

The protocol is just JSON frames, so the SDK is not required: any runtime with WebSocket will
do. The SDK is convenient because it handles reconnection, chunking/reassembly and frame parsing
for you.

## 3. Minimal example (20 lines)

=== "TypeScript"

    ```ts
    import { HermesClient } from 'hermes-mobile-client';          // 1

    const client = new HermesClient({                              // 2
      url: 'wss://agent.example.com/ws',                           // 3
      token: '<your token>',                                       // 4
      device: { id: 'pilot-phone', name: 'Phone', platform: 'android', appVersion: '1.0.0' },
      user: { id: 'dev-pilot', name: 'Developer' },                // 6
      handlers: {                                                  // 7
        onReady: (r) => console.log('session', r.session),         // 8
        onMessage: (m) => console.log('agent:', m.text),           // 9
        onMessageEdit: (m) => replaceById(m.id, m.text),           // 10 streaming output
        onAvatar: (s) => avatar.setState(s),                       // 11
        onTyping: () => showTyping(),                              // 12
        onError: (e) => console.warn(e.code, e.message),           // 13
      },
    });                                                            // 15
    client.connect();                                              // 16
    client.sendText('hello, Hermes');                              // 17
    ```

=== "Python"

    ```python
    import asyncio
    from sdk.python.hermes_client import Device, HermesMobileClient

    async def main():
        async with HermesMobileClient(
            "wss://agent.example.com/ws", token="<your token>",
            device=Device(id="pilot-phone", name="Phone"),
        ) as client:
            await client.send_text("hello, Hermes")
            answer = await client.wait_message(timeout=120)   # one full agent reply
            print("".join(answer))

    asyncio.run(main())
    ```

If the agent's text came back — the link works. Next — [App interface](../ui/index.md).

## 4. Checking without code (the web example)

Open the file `examples/web/index.html` from the repository in a browser, enter the address and
token, click "Connect", send a message. This is the reference implementation of the protocol in
plain JS: if the agent replied here — any problem, if there is one, is definitely in your app and
not in the link.

It is best to open this web example through a local server:

```bash
cd examples/web && python3 -m http.server 8000   # then http://localhost:8000
```

## 5. If pairing/allowlist rejected you

The adapter replies `ready` — the connection is accepted — but there are **no agent replies**.
Two common causes:

* **Pairing.** On first contact with an unfamiliar device Hermes replies with a pairing request
  as usual. The agent owner must approve the device on their machine:

    ```bash
    hermes pairing list
    hermes pairing approve mobile <request-id>
    ```

* **Allowlists.** The owner may have restricted access. Check together with them:
    * `MOBILE_ALLOWED_DEVICES` — a list of `device.id` (the adapter filter);
    * `MOBILE_ALLOWED_USERS` — a list of users (Hermes' built-in authorization);
    * `MOBILE_ALLOW_ALL_USERS=1` — accept any device with a valid token (for debugging).

If, however, the connection closes immediately with code **1008** — that is an access denial at
the handshake stage (a wrong token or a device/user outside the list). Retrying is pointless:
you need the right token or access. A breakdown of all cases — [Troubleshooting](../troubleshooting.md).
