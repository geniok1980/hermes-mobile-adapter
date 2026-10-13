# PR body — for `NousResearch/hermes-agent` → `plugin-catalog/hermes-mobile.yaml`

**Статус: MERGED** — влит мейнтейнером teknium1 2026-10-13 (merge-коммит `8172ebab6b4f`), финальный ревью — APPROVED на пине `7fdaf232`. Запись живёт в апстриме: `plugin-catalog/hermes-mobile.yaml` в main.

**Adds one catalog entry: `plugin-catalog/hermes-mobile.yaml`** (no other file is touched).

## What the plugin does

`hermes-mobile` adds a **WebSocket platform** to the gateway so a developer's *own* app — iOS,
Android, React Native, Flutter, desktop or web — talks to a Hermes Agent directly, with no
second backend and no duplicated history. Messages land in the same sessions, tools and memory
as Telegram/CLI; replies come back with typing indicators, streaming edits and avatar state
events (`thinking` → `speaking` → `idle`).

Feature surface: text, voice (WAV/OGG), images, video and documents both ways; command-approval,
`clarify` and slash-confirm cards answered from the app; media up to 8 MB per frame with
`part`/`parts` reassembly; cron/`hermes send` delivery to a chosen device with attachments;
a versioned JSON protocol; TypeScript (`hermes-mobile-client` on npm) and Python SDKs.

Upstream issue for context: #136584 (this PR is the catalog path, not the in-tree path).

## Hermes surfaces used

* `ctx.register_platform(...)` and `BasePlatformAdapter` from `gateway/platforms/base.py` —
  `connect` / `disconnect` / `send` / `send_typing` / `get_chat_info`, `handle_message`,
  `MessageEvent`, `SendResult`.
* Public helpers only: `gateway.platforms._shared` (`get_scoped_secret`, `coerce_port`),
  `gateway.platforms.helpers` (`cancel_task`), `standalone_sender_fn` for cron delivery.
* Nothing else: no core monkey-patching, no rebinding of core functions or module attributes,
  no `sys.modules` writes, no desktop surface (`desktop/plugin.js` is absent).
* Registers no tools, hooks or middleware — hence the empty `provides_*` blocks.

## Validation

Run against a checkout at the pinned commit:

```
hermes plugins validate /path/to/hermes-mobile-adapter/adapter/hermes_mobile
```

Result: **13/13 checks pass, 0 warnings, `security scan: safe`** (manifest, `requires_hermes`,
loadability, capability probe, declared tools/hooks/middleware match, no built-in collisions).
Test suite in the repository: 97 unit tests over protocol, adapter and capabilities.

## Disclosure (rule 13)

* **Network listener.** The adapter opens a listening WebSocket server (`MOBILE_HOST` /
  `MOBILE_PORT`, default port 8787, default host is the wildcard address). It authenticates with
  a shared token (`MOBILE_TOKEN`; per-device tokens via `MOBILE_EXTRA_TOKENS`), optionally
  narrowed by `MOBILE_ALLOWED_DEVICES`, and refuses unauthorized connections with error code
  1008. Operators are told to bind to loopback or put TLS + a firewall in front; the plugin's
  README states this.
* **Files on disk.** Inbound attachments are written under `$HERMES_HOME/mobile-media`. Nothing
  is uploaded anywhere by the plugin.
* **Credentials.** Secrets are read through Hermes's profile-scoped route (`get_scoped_secret`).
  The plugin never parses `.env`, `auth.json`, keychains or another tool's credential store, and
  never refreshes or writes tokens. `requires_env` is declared, and `config_schema` is not used.
* **Approvals.** The adapter renders Hermes's own approval / clarify / slash-confirm cards in the
  app and resolves them through Hermes's resolvers (`resolve_gateway_approval`, `clarify_gateway`,
  `slash_confirm`). Nothing is auto-approved, and unattended (cron) runs fail cleanly instead of
  hanging.
* **No telemetry**, no third-party network calls, no shell commands started by the plugin itself,
  no self-updating code (the pinned SHA is the only update path), no bundled binaries — pure
  Python source plus Markdown.

## Notes for the reviewer

* The repository is a monorepo: **`subdir: adapter/hermes_mobile`** points at the plugin
  (`plugin.yaml`, `__init__.py`, `adapter.py`, `protocol.py`, `README.md`). `sdk/`, `examples/`
  and `site-docs/` are the client-side material (TypeScript/Python SDKs, platform guides,
  protocol reference) and are not part of the plugin tree.
* `requires_hermes: ">=0.21.6"` — the version the adapter was built and tested against.
* Repository README and docs are in Russian; the plugin README rendered on the catalog page
  (`adapter/hermes_mobile/README.md`) is in English.
* Dependency: `websockets` only, already present in the Hermes environment — no extra pip
  install at any point.
* License: MIT.
