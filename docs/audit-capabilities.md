# Аудит возможностей платформенного адаптера Hermes Agent vs. hermes-mobile

> **Статус: все найденные пробелы закрыты** (адаптер — коммит `47098dc`, SDK — коммит вместе с
> документацией; 97 тестов зелёные). Файл оставлен как есть: он фиксирует контракт Hermes
> и то, что именно было пробелом у платформы `mobile`.

Источник фактов — **только** установленный код `/usr/local/lib/hermes-agent`
(venv: `/usr/local/lib/hermes-agent/venv/bin/python`). Наш адаптер:
`/root/hermes-mobile-adapter/adapter/hermes_mobile/adapter.py` (+ `protocol.py`).

Ключевые файлы, из которых всё выведено:

| Файл | Что там |
|---|---|
| `gateway/platform_registry.py` | `PlatformEntry` (стр. 41–101), `PlatformRegistry.create_adapter` |
| `gateway/platforms/base.py` | `BasePlatformAdapter` (стр. 1812+, 4455 строк) |
| `gateway/platforms/event.py` | `MessageEvent` (стр. 35–109), `MessageType` |
| `hermes_cli/plugins.py` | `PluginContext.register_platform` (стр. 793–822) |
| `gateway/run_turn_runner.py` | отправка approval/clarify, текстовые фоллбэки |
| `gateway/run_inbound.py` | перехват текстового ответа на clarify/approval/slash-confirm |
| `gateway/relay/adapter.py` | эталонная реализация `prompt_response` для WS-транспорта |
| `plugins/platforms/*/adapter.py` | эталонные реализации (telegram, discord, slack, matrix) |

---

## 1. Поля `PlatformEntry` и что мы НЕ передаём

`PlatformEntry` — датакласс (`gateway/platform_registry.py:41`). Плагин регистрирует
адаптер через `ctx.register_platform(...)` (`hermes_cli/plugins.py:793`), который
принимает обязательные `name, label, adapter_factory, check_fn` и пробрасывает
остальные именованные аргументы (`**entry_kwargs`) прямо в `PlatformEntry`; неизвестный
ключ → `TypeError`. `plugin_name` подставляется автоматически.

| # | Поле | Назначение | По умолчанию | Передаём? |
|---|---|---|---|---|
| 1 | `name` | id платформы в config.yaml | — (обяз.) | ✅ `"mobile"` |
| 2 | `label` | human-readable | — (обяз.) | ✅ «Mobile App (WebSocket)» |
| 3 | `adapter_factory` | `PlatformConfig -> adapter` | — (обяз.) | ✅ лямбда |
| 4 | `check_fn` | ПАССИВНАЯ проверка зависимостей (никогда не ставит) | — (обяз.) | ✅ `check_requirements` |
| 5 | `validate_config` | хватает ли настроек | `None` («пусть `connect()` упадёт понятно») | ✅ `validate_config` |
| 6 | `ensure_deps_fn` | АКТИВНАЯ установка зависимостей | `None` = нет авто-установки, `check_fn=False` — жёсткий блок | ✅ `ensure_deps` |
| 7 | `is_connected` | «подключено ли» для `gateway status` / setup-UI | `None` → фоллбэк на `validate_config`/`check_fn` | ❌ (фоллбэк ок) |
| 8 | `required_env` | показ в `hermes setup` | `[]` | ✅ `["MOBILE_TOKEN"]` |
| 9 | `install_hint` | текст, когда `check_fn` ложно | `""` | ✅ |
| 10 | `setup_fn` | интерактивная настройка | `None` = стандартный/none | ✅ `interactive_setup` |
| 11 | `source` | `"builtin"`/`"plugin"` | `"plugin"` | авто |
| 12 | `plugin_name` | владелец (манифест) | `""` | авто (`ctx`) |
| 13 | `allowed_users_env` | env со списком разрешённых user id | `""` | ✅ `MOBILE_ALLOWED_USERS` |
| 14 | `allow_all_env` | env-«разрешить всех» | `""` | ❌ |
| 15 | `max_message_length` | лимит нарезки (0 = без лимита) | `0` | ✅ `DEFAULT_CHUNK_LIMIT` |
| 16 | `pii_safe` | редактировать PII в описаниях сессий | `False` | ✅ `True` |
| 17 | `emoji` | иконка в CLI/статусе | `"🔌"` | ✅ `"📱"` |
| 18 | `allow_update_command` | `/update` разрешён с платформы | `True` | ❌ (дефолт ок) |
| 19 | `platform_hint` | вставка в системный промпт | `""` | ✅ (наш русский hint) |
| 20 | `env_enablement_fn` | заполнить `extra` из env до создания адаптера | `None` | ✅ `_env_enablement` |
| 21 | `apply_yaml_config_fn` | мост YAML→env (`apply_yaml_bridge`) | `None` | ❌ |
| 22 | `cron_deliver_env_var` | env «домашнего» канала для `deliver=<name>` | `""` | ✅ `MOBILE_HOME_DEVICE` |
| 23 | `parse_target_ref_fn` | разбор нативного синтаксиса цели | `None` | ❌ |
| 24 | `validate_target_ref_fn` | валидация цели после разбора | `None` | ❌ |
| 25 | `send_message_handler` | цельная доставка (`hermes send`) | `None` | ❌ |
| 26 | `standalone_sender_fn` | доставка вне процесса шлюза (cron) | `None` | ✅ `standalone_send` |

**Не передаём (5 полей):** `is_connected`, `allow_all_env`, `apply_yaml_config_fn`,
`parse_target_ref_fn`, `validate_target_ref_fn`, `send_message_handler`.
Все они **опциональны** — отсутствие даёт безопасный фоллбэк (см. значения по
умолчанию). Критичных пробелов здесь нет; см. раздел 6.

---

## 2. Методы `BasePlatformAdapter`: обязательные и опциональные

### 2.1 `@abstractmethod` — **обязательно** переопределить (иначе класс не инстанцируется)

Найдено ровно 4:

```python
# gateway/platforms/base.py:2500-2513, 4379-4382
@abstractmethod
async def connect(self, *, is_reconnect: bool = False) -> bool: ...
@abstractmethod
async def disconnect(self) -> None: ...
@abstractmethod
async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult: ...
@abstractmethod
async def get_chat_info(self, chat_id) -> Dict[str, Any]: ...
```

Мы реализуем все четыре ✅.

### 2.2 Опциональные методы: сигнатура, поведение по умолчанию, последствие не-переопределения

Все методы ниже в базовом классе **работают** (не `raise`), кроме явно указанного
`send_draft`. Последствие «не переопределять» — это либо тихий пропуск, либо
откат на текст.

| Метод | Сигнатура (сокр.) | Что делает базовый код | Если НЕ переопределить |
|---|---|---|---|
| `send_typing` | `async (chat_id, metadata=None)` | пустое тело — **no-op** (`base.py:2717`) | индикатор «печатает» не показывается (тихий пропуск) |
| `stop_typing` | `async (chat_id)` | пустое тело — **no-op** (`base.py:2720`) | ничего не гаснет (для нас ок — индикатор на аватаре) |
| `edit_message` | `async (chat_id, message_id, content, *, finalize=False) -> SendResult` | `return SendResult(success=False, error="Not supported")` (`base.py:2524`) | callers шлют сообщение заново; стриминг/правки недоступны |
| `delete_message` | `async (chat_id, message_id) -> bool` | `return False` (`base.py:2532`) | «свежий финал» не удаляет превью; best-effort, не ошибка |
| `send_draft` | `async (chat_id, draft_id, content, metadata=None) -> SendResult` | **`raise NotImplementedError`** (`base.py:1977`), но вызывается только при `supports_draft_streaming()==True` | т.к. `supports_draft_streaming` тоже дефолт `False`, метод **никогда не зовётся** — безопасно |
| `send_image` | `async (chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult` | **откат на текст**: `send(caption + "\n" + url)` (`base.py:2780`) | URL картинки уходит текстом |
| `send_animation` | `async (chat_id, animation_url, ...)` | **откат** на `send_image` (`base.py:2787`) | то же (→ текст URL) |
| `send_multiple_images` | `async (chat_id, images: List[(url,alt)], metadata=None, human_delay=0.0) -> SendResult` | базовый цикл по `send_image_file`/`send_animation`/`send_image` (`base.py:2744`) | при отсутствии и их — все картинки уходят текстом/заглушкой |
| `send_image_file` | `async (chat_id, image_path, caption=None, reply_to=None, metadata=None, **kw) -> SendResult` | `_send_media_fallback_notice(...)` → текст `"⚠️ Couldn't deliver the image attachment."` (`base.py:2943`, `base.py:177`) | изображение **не** доставляется, юзер видит заглушку |
| `send_voice` | `async (chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kw) -> SendResult` | `_send_media_fallback_notice("send_voice","audio",...)` (`base.py:2827`) | голос (в т.ч. авто-TTS) не играет, юзер видит заглушку |
| `send_video` | `async (chat_id, video_path, ...)` | заглушка `"⚠️ Couldn't deliver the video attachment."` (`base.py:2904`) | видео не доставляется |
| `send_document` | `async (chat_id, file_path, caption=None, file_name=None, ...)` | заглушка `"⚠️ Couldn't deliver the file attachment (name)."` (`base.py:2911`) | документы/файлы не доставляются |
| `send_private_notice` | `async (chat_id, user_id, content, reply_to=None, metadata=None) -> SendResult` | **откат** на обычный `send` (`base.py:2711`) | для DM неотличимо от обычной отправки — ок |
| `play_tts` | `async (chat_id, audio_path, **kw) -> SendResult` | **откат** на `send_voice` (`base.py:2859`) | наследует судьбу `send_voice` |
| `prepare_tts_text` | `(text) -> str` | нормализация Markdown→речь (`base.py:2847`) | работает как есть |
| `supports_draft_streaming` | `(chat_type=None, metadata=None, chat_id=None) -> bool` | `return False` (`base.py:1958`) | стриминга нет; consumer шлёт финал целиком |
| `prefers_fresh_final_streaming` | `(content, metadata=None) -> bool` | `return False` (`base.py:1966`) | финал редактирует превью, а не шлёт новое |
| `streaming_overflow_limit` | `() -> Optional[int]` | `None` → лимит `MAX_MESSAGE_LENGTH` (`base.py:1971`) | ок |
| `supports_streaming_tts` | `(chat_id, audio_format) -> bool` | `return False` (`base.py:2871`) | нет потокового TTS (целый файл) |
| `begin/write/finish/abort_streaming_tts` | см. base 2875–2890 | заглушки (`None`/no-op) | ок, вызываются только при `supports_streaming_tts=True` |
| `create_handoff_thread` | `async (parent_chat_id, name) -> Optional[str]` | `return None` (`base.py:2518`) | handoff использует `parent_chat_id` напрямую — ок |
| `toolsets_for_source` | `(source) -> Optional[List[str]]` | `return None` (`base.py:4384`) | берётся `platform_toolsets.<platform>` из конфига |
| `format_message` | `(content) -> str` | `return content` как есть (`base.py:4389`) | Markdown не трансформируется (клиент должен уметь) |
| `send_final_ledgered` | `async (event, session_key, text_content, metadata, *, reply_to, is_ephemeral_response=False)` | **bracket** «запись обязательства → `_send_with_retry` → фиксация» (`base.py:3996`); использует наш `send` | работает автоматически, пока есть `send` |
| `_send_exec_approval_prompt` | `async (prompt: ExecApprovalPrompt) -> SendResult` | `return SendResult(success=False, error="Not supported")` (`base.py:2651`) | см. §4 — авто-откат на текст |
| `send_exec_approval` | `async (chat_id, command, session_key, description, metadata, allow_permanent, allow_session, smart_denied) -> SendResult` | шаблонный метод: собирает `ExecApprovalPrompt` и зовёт `_send_exec_approval_prompt` (`base.py:2635`) | **не переопределять самому** — переопределяют только `_send_exec_approval_prompt` |
| `send_slash_confirm` | `async (chat_id, title, message, session_key, confirm_id, metadata=None) -> SendResult` | `SendResult(success=False, error="Not supported")` (`base.py:2669`) | **откат на текст** `/approve`/`/always`/`/cancel` (см. §4) |
| `send_clarify` | `async (chat_id, question, choices, clarify_id, session_key, metadata=None) -> SendResult` | нумерованный список + `mark_awaiting_text` + `send` (`base.py:2679`) | работает: текст + перехват ответа (см. §4) |
| `render_message_event` / `format_tool_event` | см. base 1988/2000 | презентационные хуки | ок |

**Дополнительно — управление «кнопочностью»:**

```python
# base.py:2629
@classmethod
def supports_exec_approval_buttons(cls) -> bool:
    return cls._send_exec_approval_prompt is not BasePlatformAdapter._send_exec_approval_prompt
```

То есть: переопределив `_send_exec_approval_prompt`, адаптер автоматически
«объявляет» себя кнопочным, и раннер (см. §4) перестаёт слать текстовый фоллбэк.

**Классовые атрибуты, которые можно переопределять:** `REQUIRES_EDIT_FINALIZE`
(по умолчанию `False`), `typed_command_prefix` (по умолчанию `/`), `_EA_*`
(шаблоны разметки approval-промпта), `_EA_ACTION_LABELS`, `_EA_ACTION_STYLES`.

---

## 3. Эталонные реализации опциональных возможностей (штатные адаптеры)

### `send_document` / `send_video` / `send_voice` / `send_image` — Telegram
`plugins/platforms/telegram/adapter.py:5007` (и каждый сохраняет родительский
фоллбэк при ошибке):

```python
async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                        reply_to=None, metadata=None, **kwargs) -> SendResult:
    return await self._send_local_file(
        "File", file_path, chat_id, reply_to, metadata, "document",
        lambda f: {"document": f, "filename": file_name or os.path.basename(file_path),
                   "caption": self._caption_1024(caption)},
        lambda e: self._warn_then("document", e, super().send_document(...)))  # фоллбэк на базу
```

Реализуют `send_image*`: `plugins/platforms/{discord/adapter_media.py,
mattermost/adapter.py, simplex/adapter.py, matrix/adapter.py, email/adapter.py,
dingtalk/adapter.py}`. `send_animation`: telegram, discord, feishu.
`send_multiple_images`: telegram, slack, discord, matrix, email,
`gateway/platforms/signal.py`.

### Кнопочные approval — Telegram / Discord
`plugins/platforms/telegram/adapter.py:3905`:

```python
async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
    def build():
        approval_id = next(self._approval_counter)
        buttons = [InlineKeyboardButton(label, callback_data=f"ea:{choice}:{approval_id}")
                   for label, choice, _ in prompt.actions]
        return prompt.text, InlineKeyboardMarkup(self._rows_of_two(buttons)), (
            lambda msg: self._approval_state.__setitem__(approval_id, prompt.session_key))
    return await self._send_prompt("send_exec_approval", prompt.chat_id, prompt.metadata, build, ...)
```

`plugins/platforms/discord/adapter.py:5327` (кнопки-`View`), `matrix/adapter.py:1653`,
`slack/adapter.py:4763`, `feishu`, `teams`, `gateway/platforms/qqbot/adapter.py`.
Кнопка резолвится через `tools.approval.resolve_gateway_approval(session_key, choice)`.

### `send_clarify` — Telegram / Slack / Discord
`plugins/platforms/telegram/adapter.py:3938`:

```python
async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
    def build():
        text, keyboard = f"❓ {question}", None
        if choices:
            rows = [[InlineKeyboardButton(str(i+1), callback_data=f"cl:{clarify_id}:{i}")]
                    for i in range(len(choices))]
            rows.append([InlineKeyboardButton("✏️ Other (type answer)",
                                              callback_data=f"cl:{clarify_id}:other")])
            keyboard = InlineKeyboardMarkup(rows)
        return text, keyboard, lambda msg: self._clarify_state.__setitem__(clarify_id, session_key)
    ...
```

`plugins/platforms/slack/adapter.py:5199` (Block Kit; без choices → `super().send_clarify`),
`discord/adapter.py:5375`, `google_chat`, `whatsapp`, `photon`.

### `send_slash_confirm` — Telegram / Slack / Discord
`plugins/platforms/telegram/adapter.py:3922`: три кнопки
`sc:once:<id>` / `sc:always:<id>` / `sc:cancel:<id>`, резолв через
`GatewayRunner._resolve_slash_confirm(confirm_id, "once"|"always"|"cancel")`.

### `edit_message` — Matrix / Telegram / Discord
`plugins/platforms/matrix/adapter.py:1474`:

```python
async def edit_message(self, chat_id, message_id, content, *, finalize=False) -> SendResult:
    formatted = self.format_message(content)
    msg_content = {"msgtype": "m.text", "body": f"* {formatted}",
                   "m.new_content": self._build_text_message_content(formatted)}
    msg_content["m.relates_to"] = {"rel_type": "m.replace", "event_id": message_id}
    return await self._send_content_event(chat_id, msg_content)
```

`delete_message`: telegram, slack, buzz, google_chat. `send_private_notice`:
только `plugins/platforms/slack/adapter.py:2263`. `stop_typing`: discord, matrix,
slack, google_chat, photon. `create_handoff_thread`: telegram, discord, slack, matrix.
`send_draft` (стриминг): telegram, slack. `play_tts`: discord.

Рекомендуемый минимальный паттерн для кастомного (WS) адаптера — переопределять
`_send_exec_approval_prompt`, `send_clarify`, `send_slash_confirm`, `edit_message`,
`send_*` при сохранении `super()`-фоллбэка на ошибке.

---

## 4. Медиа и интерактивные запросы без явной реализации

### 4.1 Медиа наружу (агент хочет картинку/голос/документ)

База `_process_message_background` → `_extract_response_content`/`_deliver_attachments`
разбирает `MEDIA:`-теги и локальные файлы и вызывает
`self._deliver_media_attachments` (`base.py:3928`), которая маршрутизирует:

- изображения → `send_multiple_images` (→ `send_image_file`/`send_image`/`send_animation`);
- `is_voice`/аудио → `send_voice`; видео → `send_video`; остальное → `send_document`.

Наш адаптер ничего из этого не переопределяет, значит срабатывают **заглушки**:

```python
# base.py:2835  _send_media_fallback_notice
logger.warning("[%s] %s fallback: native %s send unavailable for %s", ...)
text = _media_failure_text(kind, file_name)   # "⚠️ Couldn't deliver the <kind> attachment (name)."
return await self.send(chat_id=chat_id, content=text, ...)
```

Итог: картинка/голос/документ/видео **не доставляются**; пользователь получает
текстовую заглушку. Авто-TTS идёт через `play_tts → send_voice` → та же заглушка.
`send_image` (URL) не падает, а откатывается на текст `caption + "\n" + url`.

### 4.2 Интерактивные запросы: авто-откат на текст — есть, и он общий

**Одобрение опасной команды.** `gateway/run_turn_runner.py:1404`
`_approval_notify_sync`:

```python
if _renders_exec_approval_buttons(type(adapter)):     # True, если переопределён _send_exec_approval_prompt
    ... fut = self._schedule(adapter.send_exec_approval(...)); outcome = _approval_send_outcome(fut, 15)
    if outcome == "sent": ...; return
    logger.warning("Button-based approval failed ... falling back to text")
# ФОЛЛБЭК (наш путь):
msg = _format_exec_approval_fallback(cmd, desc, getattr(adapter, "typed_command_prefix", "/"), **flags)
await adapter.send(ctx._status_chat_id, msg, metadata={... "is_approval_prompt": True})
```

Текст фоллбэка (`gateway/run.py:559`) содержит инструкции
`/approve` · `/approve session` · `/approve always` · `/deny`. Ответ пользователя
перехватывает шлюз (`gateway/run_inbound.py`) и резолвит через
`tools.approval.resolve_gateway_approval(session_key, choice)`
(`gateway/slash_commands.py:1141`; `/approve|yes|ok|confirm` → `once`,
`/deny|no` → `deny`). **Нам ничего не нужно** — достаточно `send`, который у нас есть.

**Slash-подтверждение** (`/reload-mcp` и т.п.). `gateway/run_busy.py:1213` зовёт
`adapter.send_slash_confirm(...)`; база возвращает `success=False` → шлюз падает на
текстовый фоллбэк с `/approve`/`/always`/`/cancel`, которые резолвит
`_hm_slash_confirm_reply` (`run_inbound.py:441`).

**Clarify-вопрос.** `gateway/run_turn_runner.py:1367` зовёт `adapter.send_clarify(...)`.
Базовый `send_clarify` (`base.py:2679`) сам рендерит нумерованный список,
вызывает `mark_awaiting_text(clarify_id)` и `self.send(...)`; ответ пользователя
перехватывает `_hm_clarify_reply` (`run_inbound.py:351`) и резолвит через
`clarify_gateway.attempt_text_response_for_session` / `resolve_gateway_clarify`.

**Вывод:** без `send_exec_approval`/`send_slash_confirm`/`send_clarify` всё
**функционально работает через текст** — теряются только кнопки/UX.

### 4.3 `prompt_response` — нужен ТОЛЬКО для нативных кнопок

`MessageEvent.prompt_response: Optional[Dict]` (`event.py:70`):
`{prompt_id, option_id, label?, prompt_message_id?}`. В установленном коде его
читает **только** relay-транспорт (`gateway/relay/ws_transport.py:229`,
`gateway/relay/adapter.py:2070 _consume_prompt_response`); общий раннер
`prompt_response` не разбирает. Паттерн для WS-адаптера (эталон relay):

```python
# relay/adapter.py:2070 (сокр.)
pr = event.prompt_response
prompt_id, option_id = str(pr["prompt_id"]), str(pr["option_id"])
state = self._pop_prompt(prompt_id)                     # наш map prompt_id -> {kind, session_key,...}
handler = _PROMPT_RESOLVERS[state["kind"]]              # exec_approval | slash_confirm | clarify
await handler(self, state, option_id, chat_id, ack_meta)
# имена резолверов:
#  exec_approval -> tools.approval.resolve_gateway_approval(session_key, choice)
#  slash_confirm -> tools.slash_confirm.resolve(session_key, confirm_id, choice)
#  clarify       -> tools.clarify_gateway.{mark_awaiting_text|resolve_gateway_clarify}
```

Если кнопок нет — `prompt_response` не нужен вовсе. Если добавим кнопки в протокол,
придётся либо: (а) заполнять `event.prompt_response` в `_build_event` и разрешать
вручную (как relay), либо (б) просто принимать текстовый ответ — уже работает.

---

## 5. Наши текущие возможности (hermes-mobile)

`adapter.py` переопределяет только: `connect`, `disconnect`, `send`, `send_typing`,
`get_chat_info` (+ `name`, вспомогательные). Складываем по пунктам:

| Возможность | У нас | Комментарий |
|---|---|---|
| `connect`/`disconnect`/`send`/`get_chat_info` (abstract) | ✅ | `adapter.py:167,203,405,444` |
| `send_typing` | ✅ | `adapter.py:436` (через `avatar:thinking` + `S_TYPING`) |
| `stop_typing` | ⚪ нет (no-op базы) | не нужно — «печать» на аватаре гаснет по таймеру |
| Медиа: `send_image`/`_image_file`/`_multiple_images`/`send_voice`/`send_video`/`send_document`/`send_animation` | ❌ **нет** | всё уходит текстовой заглушкой. **Главный пробел** |
| `edit_message` | ❌ нет | правок/стриминга нет; финал шлётся целиком |
| `delete_message` | ❌ нет | не критично |
| `send_draft` + `supports_draft_streaming` | ❌ нет | токен-стриминга в приложении нет |
| `send_private_notice` | ⚪ нет (фоллбэк на `send`) | платформа = только DM, неотличимо |
| Кнопочное approval (`_send_exec_approval_prompt`) | ❌ нет | **работает текст.**`/approve`/`/deny` |
| `send_slash_confirm` | ❌ нет | работает текстовый фоллбэк |
| `send_clarify` | ❌ нет | работает нумерованный текст + перехват ответа |
| `prompt_response` в `MessageEvent` | ❌ не заполняем | нужно только для кнопок |
| `create_handoff_thread` | ⚪ нет (фоллбэк `None`) | тредов в мобилке нет |
| `toolsets_for_source` | ⚪ нет (фоллбэк `None`) | работает через `platform_toolsets` |
| `format_message` | ⚪ нет (как есть) | клиент должен рендерить Markdown сам |
| `send_final_ledgered` | ✅ наследуется | работает поверх нашего `send` |
| `play_tts`/`prepare_tts_text` | ⚪ наследуются | упираются в отсутствующий `send_voice` |
| Поля `PlatformEntry` (5 недостающих) | ⚪ | безопасные дефолты (см. §1) |

Регистрация (`adapter.py:699`) передаёт 17 из 26 полей; остальное — дефолты.

Дополнительно: голосовой **ввод** у нас реализован (кадр `audio`, `MessageType.VOICE`,
`media_urls`) — это входящее медиа, оно работает.

---

## 6. Приоритизированный список пробелов и минимальная реализация

### P0 — критично для мобильного пользователя

1. **Исходящее медиа (`send_image_file`, `send_document`, `send_voice`, `send_video`,
   `send_image`, `send_animation`, `send_multiple_images`).**
   Сейчас любая картинка/файл/голос от агента становятся текстом-заглушкой —
   для «аватара» и генерации изображений это блокер.
   *Минимальная реализация:* новые кадры протокола `S_MEDIA`
   `{t, id, kind:image|video|audio|file, name?, mime?, caption?, data(base64)}`
   (или `url` для `send_image`), плюс серверные методы:

   ```python
   async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                           reply_to=None, metadata=None, **kw) -> SendResult:
       session = await self._session_for(chat_id)
       if session is None: return SendResult(success=False, error=proto.E_DEVICE_OFFLINE)
       path = Path(file_path)
       if not path.exists():
           return await super().send_document(chat_id, file_path, caption, file_name, reply_to, metadata, **kw)
       data = base64.b64encode(path.read_bytes()).decode()
       mid = uuid.uuid4().hex
       ok = await self._push(session, {"t": proto.S_MEDIA, "id": mid, "kind": "file",
                                       "name": file_name or path.name, "caption": caption, "data": data})
       return SendResult(success=ok, message_id=mid,
                         error=None if ok else proto.E_DEVICE_OFFLINE)
   ```

   `send_image_file`/`send_voice`/`send_video` — те же кадры с другим `kind`;
   `send_image` (URL) — `{kind:"image", url:image_url}`. Обязательно
   **сохранять `super()`-фоллбэк** при ошибке, чтобы не сломать текстовый путь.
   Прототип: `protocol.py` уже умеет base64-обмен (`MAX_FRAME_BYTES=16 МБ`).

### P1 — важно для UX (но без потери функций)

2. **Кнопочное одобрение опасных команд (`_send_exec_approval_prompt`).**
   Сейчас работает текстом (`/approve`), но на телефоне кнопки лучше.
   *Минимальная реализация:* кадр `S_PROMPT {t, prompt_id, kind:"exec_approval",
   text, actions:[{label,choice,style}]}`; хранить `prompt_id -> {session_key}`
   в адаптере; на входящий кадр `C_PROMPT_RESULT {prompt_id, choice}` вызывать
   `tools.approval.resolve_gateway_approval(session_key, choice)`; вернуть кадр-ack.
   Отправить клиенту и проставить `metadata` (как telegram, `adapter.py:3905`).

3. **`send_clarify` кнопками.** Аналогично: кадр `S_PROMPT kind:"clarify"` c
   `choices`; на выбор — `clarify_gateway.resolve_gateway_clarify(clarify_id, text)`,
   на «Other» — `mark_awaiting_text(clarify_id)`. Учтите: без choices база уже
   делает правильно — оставьте `super()`.

4. **Стриминг ответа (`edit_message` + `supports_draft_streaming` + `send_draft`).**
   Для мобильного текста «печатается по мере генерации» — заметный плюс.
   *Минимальная реализация:* отдавать клиенту `message_id`, принимать `C_ACK`;
   `supports_draft_streaming` → `True` только для DM; `send_draft` шлёт кадр
   `S_DRAFT {draft_id, text}`; `edit_message` — кадр `S_EDIT {message_id, text}`.
   Если не хочется стриминга — оставить дефолты (функционально безопасно).

5. **`send_slash_confirm` кнопками.** Как P0-#2, kind `slash_confirm`, резолв через
   `tools.slash_confirm.resolve(session_key, confirm_id, choice)`.

### P2 — не критично

6. **`prompt_response`**: заполнять `MessageEvent.prompt_response` вместо
   собственного кадра-ответа — если хотите переиспользовать relay-инфраструктуру
   (сейчас не требуется: кнопок нет).
7. **Поля `PlatformEntry`**: `is_connected` (точнее статус в setup-UI),
   `apply_yaml_config_fn` (YAML→env мост), `parse_target_ref_fn`/
   `validate_target_ref_fn` (нативный синтаксис `--to mobile:<device>`),
   `send_message_handler`, `allow_all_env`. Все — необязательные улучшения;
   корректные дефолты уже работают.
8. **`send_private_notice` / `delete_message` / `play_tts` / `format_message` /
   `toolsets_for_source` / `create_handoff_thread`** — дефолты достаточны; трогать
   только при появлении конкретной потребности (напр. рендер Markdown на клиенте
   или скрытая озвучка аватаром через `play_tts`).

### Итог по важности (топ-5)

1. Исходящее медиа: `send_image_file` + `send_document` + `send_voice` (+ video/image/animation).
2. Кнопочное одобрение опасных команд (`_send_exec_approval_prompt`).
3. Кнопочный `send_clarify`.
4. Стриминг ответа (`edit_message` + `send_draft`).
5. Кнопочный `send_slash_confirm`.

Всё перечисленное в P0/P1 сейчас **не ломает** шлюз: базовые реализации
аккуратно откатываются на текст, поэтому мобильное приложение работоспособно и
без них — но UX и медиа-возможности неполны.
