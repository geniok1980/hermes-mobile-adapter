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

Плагин требует Hermes **`>=0.21.6`** (`requires_hermes` в `plugin.yaml`) и **не устанавливает
зависимости сам**: пакет `websockets` входит в окружение Hermes (если его нет — обновите Hermes).

## 2. Токен и параметры

```bash
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
printf 'MOBILE_HOST=127.0.0.1\nMOBILE_PORT=8787\nMOBILE_PATH=/ws\n' >> ~/.hermes/.env
```

| Переменная | Назначение |
|---|---|
| `MOBILE_TOKEN` | обязательный общий секрет приложения (авторизация WS) |
| `MOBILE_HOST` / `MOBILE_PORT` / `MOBILE_PATH` | где слушает адаптер (**по умолчанию `127.0.0.1:8787/ws`** — loopback) |
| `MOBILE_AVATAR_URL` | HTML-страница аватара, которую открывает приложение (приходит в кадре `ready`) |
| `MOBILE_ALLOWED_USERS` | список пользователей через запятую — проверяет сам Hermes (авторизация, pairing) |
| `MOBILE_ALLOWED_DEVICES` | список `device.id` через запятую — дополнительный фильтр адаптера (пусто — любое устройство с валидным токеном) |
| `MOBILE_ALLOW_ALL_USERS` | `1`/`true` — принимать любое устройство с валидным токеном, не сверяя с белым списком (удобно для быстрой проверки и разработки; в проде лучше `MOBILE_ALLOWED_USERS`) |
| `MOBILE_EXTRA_TOKENS` | дополнительные токены — если приложений/разработчиков несколько. Элемент вида `device_id=token` **привязывает токен к устройству**: им нельзя занять чужой `device.id` |
| `MOBILE_DELIVER_TOKENS` | список токенов, которым разрешён служебный кадр `deliver` (от cron) вне loopback |
| `MOBILE_HOME_DEVICE` | устройство по умолчанию для доставки cron (`deliver=mobile`) |

**Loopback по умолчанию.** Адаптер слушает `127.0.0.1:8787/ws` и доступен только с самой машины
агента. Для удалённого доступа **осознанно** поставьте `MOBILE_HOST=0.0.0.0` и **обязательно**
закройте его wss-прокси (TLS) и firewall — «как есть» наружу порт не выставляйте.

**Служебная доставка (`deliver`).** Кадр `deliver` (cron → устройство) принимается только с
loopback-адреса или от соединения с токеном из `MOBILE_DELIVER_TOKENS`; остальным — `forbidden`.
Эта переменная нужна, когда cron-процесс подключается к шлюзу не по loopback.

Те же параметры можно задать без `.env` — в `~/.hermes/config.yaml` в блоке
`gateway.platforms.mobile.extra` (ключи без префикса `MOBILE_`: `host`, `port`, `path`,
`avatar_url`, `allowed_users`, `extra_tokens`, `deliver_tokens`, `home_device`). Приоритет у переменных
окружения: если в `.env` значение задано, оно перекрывает `config.yaml`.

```yaml
gateway:
  platforms:
    mobile:
      enabled: true
      extra:
        host: 127.0.0.1
        port: 8787
        avatar_url: https://agent.example.com/avatar/
        allowed_users: [ "tel-1", "tablet-2" ]
```

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
    async with websockets.connect("ws://127.0.0.1:8787/ws") as ws:
        await ws.send(json.dumps({"t": "hello", "token": token, "device": {"id": "cli-1", "name": "Проверка"}}))
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

Либо заранее: `MOBILE_ALLOWED_DEVICES=<device.id>` (фильтр устройств) или
`MOBILE_ALLOWED_USERS=<id пользователя>` (штатная авторизация Hermes, без pairing).

## 4. Наружу — HTTPS/WSS

Приложение ходит по `wss://`, поэтому публикуем WebSocket через reverse-proxy. Пример для Caddy:

```
agent.example.com {
    handle /ws* {
        reverse_proxy 127.0.0.1:8787
    }
}
```

Проверка: `wscat -c "wss://agent.example.com/ws"` и первым кадром отправить
`{"t":"hello","token":"<токен>"}` — токен в URL (`?token=`) не принимается.

Если шлюз в контейнере — опубликуйте порт и не забудьте про firewall (обычно открываем
наружу только 80/443, а адаптер проксируем). В нашем стенде это Caddy `rkdash-caddy` в сети
`hermes-saas_default`.

## 5. Несколько приложений

* Разные команды/приложения — разные токены: `MOBILE_TOKEN` (первое) и
  `MOBILE_EXTRA_TOKENS=tok2,tok3` (остальные). Каждому — свой `device.id`; можно привязать сразу:
  `MOBILE_EXTRA_TOKENS=tel-1=tok2,tablet-2=tok3`.
* Ограничить, какие устройства вообще подключаются: `MOBILE_ALLOWED_DEVICES=app-a,app-b`
  (кто из пользователей допущен — решает штатная авторизация Hermes: `MOBILE_ALLOWED_USERS`, pairing).
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
| `bind_failed: не удалось занять 127.0.0.1:8787` | порт занят: `ss -ltnp \| grep 8787`, смените `MOBILE_PORT` |
| `config_missing: MOBILE_TOKEN не задан` | нет токена в `.env` профиля — платформа без токена не поднимается (это защита: адаптер не бывает «открытым») |
| приложение получает `auth_failed` | токен в приложении ≠ `MOBILE_TOKEN`, либо устройство вне `MOBILE_ALLOWED_DEVICES`, либо пользователь не одобрен Hermes (`MOBILE_ALLOWED_USERS` / pairing) |
| `ready` есть, ответов нет | устройство не одобрено (`hermes pairing approve mobile …`) |
| плагин не виден в `hermes plugins list` | проверьте путь `~/.hermes/plugins/<name>/plugin.yaml` и `plugins.enabled` в `config.yaml` |
| `websockets` не установлен | адаптер зависимостей **не ставит**: `websockets` входит в Hermes — обновите Hermes, если пакета нет |

## 9. Проверка карточек и медиа руками

Подключитесь `wscat`-ом (или python-скриптом из §3) и пройдите сценарии ниже. Токен
подставляйте свой, в примерах кадров он не показан.

**1) Убедиться, что возможности объявлены.** В ответ на `hello` сервер шлёт `ready`; в
`capabilities` должны быть новые поля:

```json
{"t":"ready","v":1,"session":"mobile:cli-1","capabilities":{"text":true,"voice":true,"typing":true,"avatar_states":["idle","thinking","speaking"],"media_in":["image","video","document","audio"],"media_out":["image","voice","video","document","animation"],"prompts":["exec_approval","slash_confirm","clarify"],"edit":true,"max_media_bytes":8388608,"max_text_chars":12000}}
```

**2) Отправить медиа агенту.** Небольшой файл — одним кадром (`part`/`parts` можно не
указывать, тогда это `parts:1`):

```json
{"t":"media","id":"m1","kind":"image","mime":"image/png","filename":"test.png","caption":"проверка","data":"iVBORw0KGgoAAAANSUhEUg…"}
```

Агент получит фото с подписью. Файл больше 8 МБ шлите кусками: `{"...","part":0,"parts":2,...}`
и `{"...","part":1,"parts":2,...}` с одним и тем же `id`.

**3) Дождаться карточки.** Попросите агента выполнить что-то опасное (или уточнить запрос) —
придёт кадр `prompt`:

```json
{"t":"prompt","id":"5f6a…","kind":"exec_approval","text":"Опасная команда rm -rf …","command":"rm -rf /tmp/build","description":"Удаляет временный каталог сборки","options":[{"id":"once","label":"✅ Одобрить один раз","style":"primary"},{"id":"deny","label":"❌ Отклонить","style":"danger"}],"session":"mobile:cli-1","timeout_seconds":300}
```

Покажите `command` рядом с кнопками — человек должен видеть, что именно одобряет. Карточку может
закрыть только устройство, которому она показана: ответ с другого соединения получает `forbidden`.

**4) Ответить одним кадром.** `option_id` — из `options`. Ответ не уходит в чат, а резолвит
запрос; сервер подтверждает кадром `prompt_done`:

```
→ {"t":"prompt","prompt_id":"5f6a…","option_id":"deny"}
← {"t":"prompt_done","id":"5f6a…","option_id":"deny"}
```

Недопустимый `option_id` (например `"whatever"`) сервер заменит на безопасный (`deny` для
одобрения, `cancel` для подтверждения) — это видно по `option_id` в `prompt_done`. Если
ничего не отвечать, через `timeout_seconds` придёт `{"t":"prompt_done","id":"5f6a…","expired":true}`.

**5) Принять медиа от агента.** Если агент пришлёт картинку/файл, придёт кадр `media` с
кусками и `final`:

```json
{"t":"media","id":"a1","kind":"image","mime":"image/png","filename":"chart.png","caption":"График","part":0,"parts":1,"final":true,"size":20480,"data":"iVBORw0KGgo…"}
```

Соберите `data` по `id` и сохраните файл, когда пришёл `final:true`.
