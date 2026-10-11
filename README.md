# Hermes Mobile Adapter

Подключите **Hermes Agent** к своему мобильному приложению: приложение общается с агентом
напрямую, без своего бэкенда. Сообщения из телефона попадают в те же сессии, инструменты и
память, что Telegram/CLI, а ответы агента приходят с индикатором «печатает» и состояниями
аватара (`thinking` → `speaking` → `idle`).

```
┌───────────────────────┐   WebSocket (текст, голос, события аватара)   ┌──────────────────────────┐
│  ваше приложение      │  ⇄  ws(s)://<хост-агента>:8787/ws            │  Hermes Agent (шлюз)     │
│  Swift / Kotlin / RN  │                                               │  плагин hermes-mobile    │
│  / Flutter / Web      │                                               │  → агент, сессии, память │
└───────────────────────┘                                               └──────────────────────────┘
```

Адаптер — обычная платформа Hermes: он поднимает WebSocket-сервер внутри шлюза и
превращает кадры протокола в `MessageEvent`. Никакой второй истории, второго биллинга и
отдельного API-слоя.

## Что в репозитории

| Каталог | Для кого | Что это |
|---|---|---|
| `adapter/hermes_mobile/` | владелец агента | плагин-адаптер Hermes (Python), ставится в `~/.hermes/plugins/` |
| `sdk/typescript/` | разработчики приложений | клиент протокола на TypeScript (RN, Expo, web, Electron) |
| `sdk/python/` | боты, тесты, скрипты | asyncio-клиент протокола |
| `examples/web/` | быстрый старт | одна HTML-страница: чат с агентом прямо в браузере |
| `examples/react-native/` | мобильные | экран диалога + аватар в WebView |
| `docs/protocol.md` | все | спецификация протокола v1 (кадры, коды ошибок, пределы) |
| `docs/integration.md` | все | как встроить в своё приложение: жизненный цикл, токены, аватар, голос |
| `docs/adapter-install.md` | владелец агента | установка плагина на сервер агента, HTTPS/WSS, проверка |

## Быстрый старт

### 1. Владелец агента: ставит адаптер

```bash
cp -r adapter/hermes_mobile ~/.hermes/plugins/hermes-mobile
printf 'MOBILE_TOKEN=%s\n' "$(openssl rand -hex 32)" >> ~/.hermes/.env
ssh-copy ... # не нужно: дальше включаем платформу
python - <<'PY'
import pathlib, yaml
p = pathlib.Path.home() / '.hermes' / 'config.yaml'
cfg = yaml.safe_load(p.read_text()) or {}
cfg.setdefault('plugins', {}).setdefault('enabled', []).append('hermes-mobile')
cfg.setdefault('gateway', {}).setdefault('platforms', {}).setdefault('mobile', {})['enabled'] = True
p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
PY
hermes gateway restart
ss -ltn | grep 8787          # адаптер слушает
hermes pairing list          # при первом контакте — pairing-код, odobрить: hermes pairing approve mobile <id>
```

Подробно: [`docs/adapter-install.md`](docs/adapter-install.md).

### 2. Разработчик приложения: подключается

Минимальный путь — TypeScript SDK (30 строк):

```ts
import { HermesClient } from './sdk/typescript/hermesClient';

const client = new HermesClient({
  url: 'wss://<хост-агента>/ws',         // или ws://<хост>:8787/ws в локальной сети
  token: '<MOBILE_TOKEN>',                // тот же токен, что у владельца агента
  device: { id: 'my-app-1', name: 'iPhone', platform: 'ios', appVersion: '1.0.0' },
  handlers: {
    onMessage: ({ text, final }) => console.log('агент:', text, final ? '(конец)' : ''),
    onAvatar: (state) => console.log('аватар:', state),
    onTyping: () => console.log('агент печатает…'),
  },
});

client.connect();
client.sendText('привет, Hermes');
```

Хотите без кода — откройте `examples/web/index.html`, впишите адрес и токен: тот же
протокол, чат в браузере.

## Протокол

Коротко: приложение подключается, отправляет `hello` с токеном и получает `ready`; далее
кадры `text`/`audio` вверх и `message`/`typing`/`avatar`/`error` вниз. Полное описание с
примерами и кодами ошибок — [`docs/protocol.md`](docs/protocol.md). Адаптер и SDK содержат
одну и ту же версию констант (`PROTOCOL_VERSION = 1`).

## Тесты

```bash
python -m unittest discover -s tests -v
```

Тесты поднимают **настоящий** WebSocket-сервер адаптера и подключаются к нему настоящими
клиентами (включая Python-SDK): авторизация и отказ по токену с кодом 1008, дедупликация,
нарезка длинного ответа, голос с записью файла, доставка `deliver`, `standalone_sender_fn`,
регистрация платформы в реальном реестре Hermes. CI: [`.github/workflows/tests.yml`](.github/workflows/tests.yml).

## Требования

* Hermes Agent (адаптер использует его платформенный API);
* Python 3.10+ и пакет `websockets` (в окружении Hermes он уже есть; адаптер доустановит сам);
* для клиента — любой рантайм с WebSocket: React Native/Expo, Flutter, Swift, Kotlin, web.

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
