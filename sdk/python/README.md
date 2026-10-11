# Python SDK

Клиент протокола Hermes Mobile Adapter для Python (`asyncio` + `websockets`).
Для ботов, скриптов, серверных интеграций и автотестов.

```bash
pip install websockets>=12
```

## Диалог

```python
import asyncio
from sdk.python.hermes_client import Device, HermesMobileClient

async def main():
    async with HermesMobileClient(
        "ws://127.0.0.1:8787/ws", token="…", device=Device(id="bot-1", name="Бот")
    ) as client:
        await client.send_text("привет, Hermes")
        answer = await client.wait_message(timeout=120)   # один полный ответ агента
        print("".join(answer))

asyncio.run(main())
```

## Карточки-вопросы

Агент присылает карточку с вариантами (`exec_approval`, `slash_confirm`, `clarify`).
Колбэк `on_prompt` получает `Prompt` (`.id`, `.kind`, `.text`, `.options`), ответ —
методом `answer_prompt`:

```python
def on_prompt(prompt):
    print(prompt.kind, prompt.text, [o.id for o in prompt.options])   # o.id, o.label, o.style
    asyncio.create_task(client.answer_prompt(prompt.id, option_id="once"))

def on_prompt_done(done):                 # карточка закрыта
    print("итог:", done.option_id, "истекла" if done.expired else "")

client = HermesMobileClient(url, token, on_prompt=on_prompt, on_prompt_done=on_prompt_done)
# exec_approval — once|session|always|deny; slash_confirm — once|always|cancel;
# clarify — c0..cN либо other вместе с текстом:
#   await client.answer_prompt(prompt.id, option_id="other", text="мой вариант")
```

## Медиа в обе стороны

Отправка — байты или путь к файлу; большие файлы автоматически режутся по 3 МБ
на кадры `part`/`parts`:

```python
await client.send_image("~/скрин.png", caption="вот ошибка")     # путь → base64
await client.send_document(b"%PDF-1.4 …", filename="отчёт.pdf")  # байты
await client.send_voice("~/answer.ogg")
await client.send_media("video", "/tmp/clip.mp4")                # любой из image/video/document/audio
```

Приём — колбэк `on_media` вызывается один раз на **целый** файл (куски собираются внутри),
плюс удобное сохранение на диск:

```python
def on_media(message):
    path = client.save_media(message, "~/загрузки")   # -> Path
    print("сохранён:", path, message.kind, message.mime)
    # message.data — байты; message.data_url() — data-URL для показа картинки

client = HermesMobileClient(url, token, on_media=on_media)
media = await client.wait_media(timeout=30)   # или ждать явно
await client.send_image("~/скрин.png")        # и отправить своё
```

## Стриминг и индикатор «печатает»

- `on_message(message)` — обычный ответ; `message.edit == True` означает правку уже
  показанного сообщения с тем же `id` (потоковая выдача). Для правок есть отдельный
  колбэк `on_message_edit`.
- `on_typing()` — агент печатает; `on_typing_stop()` — пришёл `typing.stop`, погасите
  индикатор. Оба совместимы со старым кодом: `on_typing` вызывается без аргументов.

## Прочее

- `send_audio(...)`, `send_text(...)`, `send_typing()`, `ping()`, `acknowledge()` — как раньше.
- `ready.capabilities` содержит `media_in`, `media_out`, `prompts`, `edit`, `max_media_bytes`.
- Ошибки протокола — исключение/колбэк `HermesProtocolError` (`.code`, `.message`, `.fatal`).
