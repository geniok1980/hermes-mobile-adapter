# Установка адаптера на машину агента

Адаптер — плагин Hermes Agent. Ставится туда, где работает `hermes gateway`.

## 1. Плагин

```bash
cp -r adapter/hermes_mobile ~/.hermes/plugins/hermes-mobile        # профиль по умолчанию
hermes plugins list | grep hermes-mobile                          # плагин виден (source: user)
```

Включите плагин и платформу в `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-mobile
gateway:
  platforms:
    mobile:
      enabled: true
```

## 2. Токен и параметры

```bash
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
printf 'MOBILE_HOST=0.0.0.0\nMOBILE_PORT=8787\nMOBILE_PATH=/ws\n' >> ~/.hermes/.env
```

| Переменная | Назначение |
|---|---|
| `MOBILE_TOKEN` | обязательный общий секрет приложения (авторизация WS) |
| `MOBILE_HOST` / `MOBILE_PORT` / `MOBILE_PATH` | где слушает адаптер (по умолчанию `0.0.0.0:8787/ws`) |
| `MOBILE_AVATAR_URL` | HTML-страница аватара, которую открывает приложение (приходит в кадре `ready`) |
| `MOBILE_ALLOWED_USERS` | список `device.id` через запятую (пусто — любое устройство с валидным токеном) |
| `MOBILE_EXTRA_TOKENS` | дополнительные токены — если приложений/разработчиков несколько |
| `MOBILE_HOME_DEVICE` | устройство по умолчанию для доставки cron (`deliver=mobile`) |

Интерактивно: `hermes gateway setup` → **Hermes Go Mobile** (мастер сгенерирует токен).

## 3. Запуск и проверка

```bash
hermes gateway restart
ss -ltn | grep 8787
```

Проверка из терминала:

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

Первый контакт незнакомого устройства Hermes встречает pairing-кодом — это штатно:

```bash
hermes pairing list
hermes pairing approve mobile <request-id>     # или <код из сообщения>
```

Либо заранее: `MOBILE_ALLOWED_USERS=<device.id>` + перезапуск шлюза.

## 4. Наружу — HTTPS/WSS

Приложение ходит по `wss://`, поэтому публикуем WebSocket через reverse-proxy. Пример для Caddy:

```
agent.example.com {
    handle /ws* {
        reverse_proxy 127.0.0.1:8787
    }
}
```

Проверка: `wscat -c "wss://agent.example.com/ws?token=<токен>"`.

Если шлюз в контейнере — опубликуйте порт и не забудьте про firewall (обычно открываем
наружу только 80/443, а адаптер проксируем). В нашем стенде это Caddy `rkdash-caddy` в сети
`hermes-saas_default`.

## 5. Несколько приложений

* Разные команды/приложения — разные токены: `MOBILE_TOKEN` (первое) и
  `MOBILE_EXTRA_TOKENS=tok2,tok3` (остальные). Каждому — свой `device.id`.
* Ограничить, кто вообще может подключиться: `MOBILE_ALLOWED_USERS=app-a,app-b`.
* Сессии в Hermes разделяются по `device.id`, поэтому диалоги не смешиваются.

## 6. Доставка из cron

```bash
hermes cron add "напоминание" --schedule "0 9 * * *" --deliver mobile:app-a
```

Работает через `standalone_sender_fn`: процесс подключается к работающему шлюзу как
служебный клиент и просит отдать сообщение устройству. Если шлюз не запущен или устройство
не в сети — вернётся ошибка доставки (мобильная доставка требует живого соединения).

## 7. Обновление

```bash
rsync -a --delete adapter/hermes_mobile/ ~/.hermes/plugins/hermes-mobile/
hermes gateway restart
```

## 8. Частые проблемы

| Симптом | Решение |
|---|---|
| `bind_failed: не удалось занять 0.0.0.0:8787` | порт занят: `ss -ltnp \| grep 8787`, смените `MOBILE_PORT` |
| `config_missing: MOBILE_TOKEN не задан` | нет токена в `.env` профиля — платформа без токена не поднимается (это защита: адаптер не бывает «открытым») |
| приложение получает `auth_failed` | токен в приложении ≠ `MOBILE_TOKEN`, либо устройство вне `MOBILE_ALLOWED_USERS` |
| `ready` есть, ответов нет | устройство не одобрено (`hermes pairing approve mobile …`) |
| плагин не виден в `hermes plugins list` | проверьте путь `~/.hermes/plugins/<name>/plugin.yaml` и `plugins.enabled` в `config.yaml` |
| `websockets` не установлен | адаптер поставит его сам при подключении (`ensure_deps_fn`); вручную: `pip install websockets>=12` |
