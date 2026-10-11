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

## 📖 Документация

**Вся документация — на сайте (MkDocs Material): <https://geniok1980.github.io/hermes-mobile-adapter/>**

| Раздел сайта | Для кого |
|---|---|
| [Быстрый старт](https://geniok1980.github.io/hermes-mobile-adapter/quickstart/) | разработчик приложения: адрес, токен, первый ответ агента |
| [Передача разработчику](https://geniok1980.github.io/hermes-mobile-adapter/quickstart/handover/) | владелец агента: что выдать и какие правила сообщить |
| [Протокол v1](https://geniok1980.github.io/hermes-mobile-adapter/protocol/) | все: кадры, поля, лимиты, коды ошибок, версионирование |
| [Справочник SDK](https://geniok1980.github.io/hermes-mobile-adapter/reference/typescript/) | все методы и колбэки TypeScript и Python |
| [Руководства](https://geniok1980.github.io/hermes-mobile-adapter/guides/react-native/) | готовый код: RN/Expo, Flutter, Swift, Kotlin, веб, рецепты, чек-лист приёмки |
| [Интерфейс приложения](https://geniok1980.github.io/hermes-mobile-adapter/ui/) | что обязательно реализовать и что будет, если не делать |
| [Установка адаптера](https://geniok1980.github.io/hermes-mobile-adapter/host/) | владелец агента: плагин, wss, доступы |

Страницы сайта лежат в `site-docs/` — исходники и сайт всегда совпадают:
`pip install -r docs-requirements.txt && ./scripts/build-docs.sh serve`.

## Что в репозитории

| Каталог | Для кого | Что это |
|---|---|---|
| `adapter/hermes_mobile/` | владелец агента | плагин-адаптер Hermes (Python), ставится в `~/.hermes/plugins/` |
| `sdk/typescript/` | разработчики приложений | клиент протокола на TypeScript (RN, Expo, web, Electron) |
| `sdk/python/` | боты, тесты, скрипты | asyncio-клиент протокола |
| `examples/web/` | быстрый старт | одна HTML-страница: чат с агентом прямо в браузере |
| `examples/react-native/` | мобильные | экран диалога + аватар в WebView |
| `site-docs/` | все | **вся документация** (MkDocs Material) — публикуется на GitHub Pages |
| `tests/` | контрибьюторы | тесты протокола и адаптера (в т.ч. `tests/test_capabilities.py` — карточки, медиа, правки) |

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

Подробно: [`site-docs/host/install.md`](site-docs/host/install.md).

### 2. Разработчик приложения: подключается

Минимальный путь — TypeScript SDK. Ставится из npm:

```bash
npm i hermes-mobile-client
```

```ts
import { HermesClient } from 'hermes-mobile-client';

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

## Возможности

* **Текст** — обычные сообщения и нарезка длинных ответов агента на части (`message` +
  `chunk`);
* **Голос** — запись WAV/OGG вверх и голосовое от агента вниз;
* **Медиа в обе стороны** — фото, файлы, видео, аудио: приложение шлёт агенту (`media`
  вверх, крупные файлы кусками `part`/`parts`), агент шлёт приложению (картинка, голос,
  видео, документ, анимация; ссылкой или файлом). Лимит — 8 МБ на файл;
* **Карточки-вопросы** — кнопочное одобрение опасных команд, подтверждение слэш-команд и
  уточняющие вопросы (`prompt` с вариантами; ответ резолвится штатными механизмами Hermes,
  а не как сообщение в чате);
* **Потоковая выдача ответа** — правка уже отправленного сообщения по `id` (кадр `message`
  с `edit: true`), чтобы ответ «дорисовывался» по мере генерации;
* **Индикатор «печатает» и состояния аватара** (`thinking` → `speaking` → `idle`, остановка
  кадром `typing{stop:true}`);
* **Адресация устройств** — `mobile:<device.id>` и краткая форма `<device.id>` в
  `hermes send`/cron.

## Протокол

Коротко: приложение подключается, отправляет `hello` с токеном и получает `ready` с
`capabilities`; далее кадры `text`/`audio`/`media`/`prompt` вверх и
`message`/`prompt`/`media`/`typing`/`avatar`/`error` вниз. Полное описание с примерами и
кодами ошибок — [`site-docs/protocol/index.md`](site-docs/protocol/index.md), практика — в
[`site-docs/guides/integration.md`](site-docs/guides/integration.md). Адаптер и SDK содержат одну и ту же версию
констант (`PROTOCOL_VERSION = 1`); все перечисленные возможности — обратно совместимые
дополнения, версия протокола не менялась.

## Тесты

```bash
python -m unittest discover -s tests -v
```

**97 тестов** поднимают **настоящий** WebSocket-сервер адаптера и подключаются к нему
настоящими клиентами (включая Python-SDK): авторизация и отказ по токену с кодом 1008,
дедупликация, нарезка длинного ответа, голос с записью файла, доставка `deliver`,
`standalone_sender_fn`, регистрация платформы в реальном реестре Hermes, а также новые
возможности — карточки-вопросы (`tests/test_capabilities.py`), медиа в обе стороны
(включая куски `part`/`parts`) и правки сообщения (`edit`). CI:
[`.github/workflows/tests.yml`](.github/workflows/tests.yml).

Адаптер проверен живьём на настоящем Hermes: подключение реальной сессии, ответ агента и
прохождение pairing (одобрение устройства).

## Требования

* Hermes Agent (адаптер использует его платформенный API);
* Python 3.10+ и пакет `websockets` (в окружении Hermes он уже есть; адаптер доустановит сам);
* для клиента — любой рантайм с WebSocket: React Native/Expo, Flutter, Swift, Kotlin, web.

## Лицензия

MIT — см. [`LICENSE`](LICENSE).
