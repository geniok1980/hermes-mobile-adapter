# Владельцу агента

Короткая страница: как поднять платформу `mobile` на машине агента и выдать доступ
разработчику. Полная версия с проверками руками — `../../host/install.md` в репозитории.

## 1. Установите плагин

```bash
cp -r adapter/hermes_mobile ~/.hermes/plugins/hermes-mobile      # профиль по умолчанию
hermes plugins list | grep hermes-mobile                         # плагин виден (source: user)
```

## 2. Включите платформу в `config.yaml`

```yaml
plugins:
  enabled:
    - hermes-mobile
gateway:
  platforms:
    mobile:
      enabled: true
```

## 3. Токен и параметры

```bash
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
printf 'MOBILE_HOST=0.0.0.0\nMOBILE_PORT=8787\nMOBILE_PATH=/ws\n' >> ~/.hermes/.env
```

| Переменная | Назначение |
|---|---|
| `MOBILE_TOKEN` | **обязательный** общий секрет приложения (авторизация WS) |
| `MOBILE_HOST` / `MOBILE_PORT` / `MOBILE_PATH` | где слушает адаптер (по умолчанию `0.0.0.0:8787/ws`) |
| `MOBILE_AVATAR_URL` | HTML-страница аватара, приходит приложению в кадре `ready` |
| `MOBILE_ALLOWED_USERS` | список пользователей — проверяет сам Hermes (авторизация, pairing) |
| `MOBILE_ALLOWED_DEVICES` | список `device.id` — отдельный фильтр адаптера (пусто = любое устройство с валидным токеном) |
| `MOBILE_ALLOW_ALL_USERS` | `1`/`true` — принимать любое устройство с валидным токеном (для отладки) |
| `MOBILE_EXTRA_TOKENS` | дополнительные токены — если приложений/разработчиков несколько |
| `MOBILE_HOME_DEVICE` | устройство по умолчанию для доставки cron |

Те же параметры можно задать в `config.yaml` → `gateway.platforms.mobile.extra` (ключи без
префикса `MOBILE_`). Переменные окружения имеют приоритет. Интерактивно — `hermes gateway setup`.

## 4. Запустите и проверьте

```bash
hermes gateway restart
ss -ltn | grep 8787          # адаптер слушает
```

Проверка из терминала (подставьте свой токен):

```bash
python - <<'PY'
import asyncio, json, websockets
async def main():
    token = "<MOBILE_TOKEN>"
    async with websockets.connect(f"ws://127.0.0.1:8787/ws?token={token}") as ws:
        await ws.send(json.dumps({"t": "hello", "device": {"id": "cli-1", "name": "Проверка"}}))
        print("ready:", await ws.recv())
        await ws.send(json.dumps({"t": "text", "id": "t1", "text": "привет"}))
        for _ in range(10):
            print("кадр:", await ws.recv())
asyncio.run(main())
PY
```

## 5. Проксируйте `wss` наружу

Приложение ходит по `wss://`, поэтому WebSocket публикуется через reverse-proxy. Пример для Caddy:

```
agent.example.com {
    handle /ws* {
        reverse_proxy 127.0.0.1:8787
    }
}
```

Проверка: `wscat -c "wss://agent.example.com/ws?token=<токен>"`. Наружу обычно открываем только
80/443, а адаптер проксируем. `ws://` без TLS допустим лишь в локальной сети или для отладки.

## 6. Выдайте доступ разработчику

Передайте разработчику **адрес** (`wss://agent.example.com/ws`), **токен** и, если нужно,
**id пользователя** и **id устройства**. Варианты разделения доступа:

* Разным командам/приложениям — разные токены: `MOBILE_TOKEN` (первый) и
  `MOBILE_EXTRA_TOKENS=tok2,tok3`. Каждому — свой `device.id`.
* Ограничить, какие устройства вообще подключаются: `MOBILE_ALLOWED_DEVICES=app-a,app-b`.
* Кто из пользователей допущен — решает штатная авторизация Hermes: `MOBILE_ALLOWED_USERS`
  либо pairing (`hermes pairing approve mobile <id>` после первого контакта).

!!! warning "Один общий токен в публичной сборке"
    Если ваше приложение скачивают другие люди, не вшивайте в него один общий токен: любой
    владелец сборки получит доступ к вашему агенту. Каждому устройству — свой токен
    (`MOBILE_EXTRA_TOKENS` или pairing). См. [Безопасность](../security.md).

## 7. Доставка из cron

```bash
hermes cron add "напоминание" --schedule "0 9 * * *" --deliver mobile:app-a
```

Работает через `standalone_sender_fn`: отдельный процесс подключается к работающему шлюзу как
служебный клиент. Если шлюз не запущен или устройство не в сети — вернётся ошибка доставки.

## 8. Обновление

```bash
rsync -a --delete adapter/hermes_mobile/ ~/.hermes/plugins/hermes-mobile/
hermes gateway restart
```

Диагностика частых проблем — [Диагностика и FAQ](../troubleshooting.md).
