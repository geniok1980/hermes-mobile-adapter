# Начало: от нуля до первого ответа агента

Этот путь предполагает, что **владелец агента** уже поднял адаптер и выдал вам адрес и токен.
Если вы сами владелец — сначала [Владельцу агента](../host/index.md).

## 1. Получите от владельца агента

| Что | Пример | Зачем |
|---|---|---|
| Адрес адаптера | `wss://agent.example.com/ws` | точка подключения (WebSocket) |
| Токен приложения | 64 hex-символа | авторизация подключения |
| Идентификатор пользователя | `dev-pilot` | под каким пользователем вас видит Hermes |
| (по желанию) id устройства | `pilot-phone` | своя сессия и адрес для доставки cron |

Адрес и токен — единственное, что нужно от владельца. Токен храните в Keychain/Keystore, не в
коде. Для публичного приложения **каждому устройству нужен свой токен** — см.
[Безопасность](../security.md).

## 2. Установите клиент

=== "TypeScript / JS"

    ```bash
    npm i hermes-mobile-client
    ```

=== "Python"

    ```bash
    pip install websockets>=12
    # клиент лежит в репозитории: sdk/python/hermes_client.py
    ```

Протокол — это просто JSON-кадры, поэтому SDK не обязателен: подойдёт любой рантайм с
WebSocket. SDK удобен тем, что берёт на себя переподключение, нарезку/склейку и разбор кадров.

## 3. Минимальный пример (20 строк)

=== "TypeScript"

    ```ts
    import { HermesClient } from 'hermes-mobile-client';          // 1

    const client = new HermesClient({                              // 2
      url: 'wss://agent.example.com/ws',                           // 3
      token: '<ваш токен>',                                        // 4
      device: { id: 'pilot-phone', name: 'Телефон', platform: 'android', appVersion: '1.0.0' },
      user: { id: 'dev-pilot', name: 'Разработчик' },              // 6
      handlers: {                                                  // 7
        onReady: (r) => console.log('сессия', r.session),          // 8
        onMessage: (m) => console.log('агент:', m.text),           // 9
        onMessageEdit: (m) => replaceById(m.id, m.text),           // 10 потоковая выдача
        onAvatar: (s) => avatar.setState(s),                       // 11
        onTyping: () => showTyping(),                              // 12
        onError: (e) => console.warn(e.code, e.message),           // 13
      },
    });                                                            // 15
    client.connect();                                              // 16
    client.sendText('привет, Hermes');                             // 17
    ```

=== "Python"

    ```python
    import asyncio
    from sdk.python.hermes_client import Device, HermesMobileClient

    async def main():
        async with HermesMobileClient(
            "wss://agent.example.com/ws", token="<ваш токен>",
            device=Device(id="pilot-phone", name="Телефон"),
        ) as client:
            await client.send_text("привет, Hermes")
            answer = await client.wait_message(timeout=120)   # один полный ответ агента
            print("".join(answer))

    asyncio.run(main())
    ```

Если в ответ пришёл текст агента — связка работает. Дальше — [Интерфейс приложения](../ui/index.md).

## 4. Проверка без кода (веб-пример)

Откройте в браузере файл `examples/web/index.html` из репозитория, впишите адрес и токен,
нажмите «Подключиться», отправьте сообщение. Это эталонная реализация протокола на голом JS:
если агент ответил здесь — проблема, если она будет, точно в вашем приложении, а не в связке.

Этот веб-пример хорошо открывать через локальный сервер:

```bash
cd examples/web && python3 -m http.server 8000   # затем http://localhost:8000
```

## 5. Если pairing/allowlist отклонил

Адаптер отвечает `ready` — соединение принято — но **ответов агента нет**. Две частые причины:

* **Pairing.** При первом контакте незнакомого устройства Hermes штатно отвечает
  pairing-запросом. Владелец агента должен одобрить устройство на своей машине:

    ```bash
    hermes pairing list
    hermes pairing approve mobile <request-id>
    ```

* **Белые списки.** Владелец мог ограничить доступ. Проверьте вместе с ним:
    * `MOBILE_ALLOWED_DEVICES` — список `device.id` (фильтр адаптера);
    * `MOBILE_ALLOWED_USERS` — список пользователей (штатная авторизация Hermes);
    * `MOBILE_ALLOW_ALL_USERS=1` — принимать любое устройство с валидным токеном (для отладки).

Если же соединение закрывается сразу с кодом **1008** — это отказ в доступе на этапе
рукопожатия (неверный токен или устройство/пользователь вне списка). Повторять бессмысленно:
нужен правильный токен или доступ. Разбор всех случаев — [Диагностика](../troubleshooting.md).
