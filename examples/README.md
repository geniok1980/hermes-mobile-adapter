# Примеры

| Пример | Для чего |
|---|---|
| [`web/index.html`](web/index.html) | одна HTML-страница: чат с агентом и состояния аватара прямо в браузере. Эталонная реализация протокола на голом JS — открывается двойным кликом (или через `python -m http.server`) |
| [`react-native/HermesChat.tsx`](react-native/HermesChat.tsx) | экран диалога для RN/Expo: клиент протокола из `sdk/typescript`, аватар в WebView, состояния `thinking`/`speaking` |
| [`react-native/avatarHtml.ts`](react-native/avatarHtml.ts) | страница аватара-заглушки с контрактом `window.hermesAvatar.setState/speak/setVolume` — замените на свою (или на URL своего аватара) |

## Как проверить за минуту

1. Владелец агента ставит адаптер (`site-docs/host/install.md`) и берёт `MOBILE_TOKEN`.
2. Открываете `web/index.html`, вводите `ws://<хост>:8787/ws` и токен → «Подключиться».
3. Пишете сообщение — агент отвечает; точка рядом с «аватар» меняет цвет на `thinking`/`speaking`.

Если агент отвечает «I don't recognize you yet… pairing code» — это штатный pairing Hermes:
`hermes pairing approve mobile <request-id>` на машине агента (или заранее внести устройство в
`MOBILE_ALLOWED_USERS`).
