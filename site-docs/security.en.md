# Security

## The token is the app's only secret

Everything that protects access to the agent is the token. There are no passwords, accounts or signatures in
the protocol: an invalid token → an `error` frame (`auth_failed`) and closing the connection with code **1008**.

* Store the token in the **Keychain (iOS) / Keystore (Android)**, not in the clear, not in the sources and
  not in the logs.
* The server never writes the token to the logs — it is masked (`protocol.redact_token`). Do the same.
* The platform **does not start at all without `MOBILE_TOKEN`** (`config_missing`): the adapter is never
  "open". An empty list of allowed tokens is a refusal.

## Why you must not ship a shared token in the build

For a public app (an APK/IPA distributed to other people) a single embedded token means: **any
owner of the build gets access to the agent**. It is enough to unpack the app and pull out the string.

A public build must be a client without secrets: the user enters **their own** address and **their own**
token. The agent owner issues tokens per device.

* **Your own agent** (you own both the agent and the app) — you can embed a token if the build does not
  leave your control.
* **An app for other people's agents** — you must not embed it in any form.

## A separate token per device

Issue a separate token for each device/developer:

* `MOBILE_TOKEN` — the first token; `MOBILE_EXTRA_TOKENS=tok2,tok3` — the rest (comma- or
  newline-separated). An entry of the form **`device_id=token`** binds the token to a device:
  `MOBILE_EXTRA_TOKENS=tel-1=tok2,tablet-2=tok3`.
* Or Hermes's standard `pairing`: `hermes pairing list` → `hermes pairing approve mobile <id>`.

Then revoking one token does not affect the others, and the logs show exactly who connected (by
`device.id`). Sessions are separated by `device.id` — dialogues of different devices are not mixed.

### Binding a token to a device

A shared token by itself does not stop a client from claiming **someone else's** `device.id` and hijacking another
session, responses and approval cards. That is why `MOBILE_EXTRA_TOKENS` accepts `device_id=token` entries:

* a taken `device.id` cannot be hijacked — a connection with the same id and a **different** token receives
  `error{code:"forbidden"}` and close **1008**;
* the same device with the **same** token is a **reconnection**: the old connection is closed,
  the new one takes the session (standard behaviour when reinstalling/changing networks).

## `wss` only

Externally — only **`wss://`** (TLS on the reverse proxy). `ws://` without encryption transmits text, media
and voice in the clear.

* `ws://` is acceptable only on a local network or during development (`adb reverse tcp:8787 tcp:8787`).
* The token is passed **only via the `token` field in the `hello` frame** — acceptance via `?token=` in the URL has been removed:
  the query string ends up in reverse-proxy logs, while the frame body is protected by TLS.
* By default the adapter listens on **loopback `127.0.0.1:8787/ws`** and is not reachable from outside at all.
  Remote access is a deliberate step: `MOBILE_HOST=0.0.0.0`, a closed `wss` proxy (TLS) and a firewall;
  do not expose port 8787 outward "as is".

## Revoking access

Revoking a token = removing it from `.env` (`MOBILE_TOKEN` or `MOBILE_EXTRA_TOKENS`) and **restarting
the gateway** (`hermes gateway restart`). An active connection does not need to be closed manually: without a valid
token, a reconnect will receive `auth_failed` and code **1008**.

If you need to restrict exactly which devices can connect, use `MOBILE_ALLOWED_DEVICES`
(a list of `device.id`). Which users are allowed is decided by Hermes's standard authorization
(`MOBILE_ALLOWED_USERS` / pairing). More on the difference between the variables — in the section
[How it works](concepts.md).

## Approving dangerous commands

The response to a question card **is validated on the adapter side**: an invalid `option_id`
is replaced with a safe value (`deny` for command approval, `cancel` for a slash command).
An approval cannot be "forged" by the client — the server will not execute anything extra from a
fabricated `option_id`.

A card (`exec_approval`) can be closed by **only the device it is shown to**: a response from
another connection (the same shared token but a different `device.id`) receives `error{code:"forbidden"}`.
For `exec_approval` the server sends separate **`command` and `description`** fields — the app must
show the exact command next to the buttons so the person sees exactly what they are approving.

## Service delivery from cron

The `deliver` frame (cron/`hermes send` → device) is a service frame: it is accepted only by a connection from a
**loopback address** (`127.0.0.0/8`, `::1`) or a connection with a token from **`MOBILE_DELIVER_TOKENS`**.
To everyone else the server replies `error{code:"forbidden"}` — an ordinary client cannot broadcast a
message on behalf of cron. Set `MOBILE_DELIVER_TOKENS` only for the tokens that are actually
used by service delivery outside loopback; in the typical setup (cron on the same machine) the variable
is not needed.

## What the agent sees

Everything sent through the protocol (text, voice, media, captions) is **part of the dialogue with the agent**,
on a par with messages from Telegram/CLI. This data ends up in the agent's history and memory and may be
sent to the model.

* Do not send through the protocol anything the agent should not see.
* Media is saved to the profile directory on the agent machine (`~/.hermes/mobile-media`).
* The plugin is marked `pii_safe=True`, but this does not replace common sense: message content is
  regular agent input.
