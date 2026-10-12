# Браузер (голый WebSocket)

Минимальный клиент без библиотек: `WebSocket` + JSON. Эталонная рабочая страница со всеми
кадрами — `examples/web/index.html`; здесь разобраны ключевые места и ограничения браузерного
подхода.

## 1. Подключение и рукопожатие

Токен передаётся **только в кадре `hello`** — приём через `?token=` в URL удалён (query-строка
попадает в логи reverse-proxy). Open-пример ниже так и делает.

```html
<script>
(() => {
  const PROTOCOL_VERSION = 1;
  const MEDIA_CHUNK_BYTES = 3 * 1024 * 1024;
  let socket = null;

  function connect(url, token, deviceId) {
    socket = new WebSocket(url);                // токен в URL НЕ передаём

    socket.onopen = () => socket.send(JSON.stringify({
      t: 'hello', v: PROTOCOL_VERSION, token,    // токен только здесь
      device: { id: deviceId, name: 'Браузер', platform: 'web', app_version: '1.0.0' },
    }));
    socket.onmessage = (event) => handle(String(event.data));
    socket.onerror = () => setStatus('Ошибка соединения: проверьте адрес и доступность порта');
    socket.onclose = (event) => {
      // 1008 — отказ в доступе; повторять бессмысленно.
      setStatus(event.code === 1008 ? 'Доступ запрещён: неверный токен (код 1008)' : `Соединение закрыто (${event.code})`);
    };
  }

  const send = (frame) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) { setStatus('Сначала подключитесь'); return false; }
    socket.send(JSON.stringify(frame));
    return true;
  };
})();
</script>
```

## 2. Приём кадров

Разбор строго по `t`; неизвестный кадр — `default` и ничего. Поля необязательные читаем
защищённо (`data.edit`, `data.stop`).

```js
function handle(raw) {
  let data;
  try { data = JSON.parse(raw); } catch { return; }   // мусор игнорируем
  switch (data.t) {
    case 'ready':
      setStatus(`Подключено. Сессия: ${data.session}` + (data.v !== 1 ? ` (шлюз v${data.v})` : ''));
      // data.capabilities — источник истины: edit, media_in/out, prompts, max_media_bytes
      break;
    case 'typing':
      setAvatar(data.stop ? 'idle' : 'thinking');
      break;
    case 'avatar':
      setAvatar(data.state);   // idle | thinking | speaking
      break;
    case 'message': {
      const key = data.id;
      const existing = document.querySelector(`[data-msg="${CSS.escape(key)}"]`);
      if (data.edit) {                                  // стриминг: заменяем текст
        if (existing) existing.textContent = data.text;
        else { const n = log(data.text); n.dataset.msg = key; }
      } else if (existing) existing.textContent += data.text;   // длинный ответ — кусками
      else { const n = log(data.text); n.dataset.msg = key; }
      break;
    }
    case 'prompt':       showPrompt(data); break;
    case 'prompt_done':  finishPrompt(data.id, data.expired ? null : data.option_id); break;
    case 'media':
      if (data.parts > 1 && !data.final) break;         // ждём последний кусок
      showMedia(data);
      break;
    case 'error':
      setStatus(`Ошибка ${data.code}: ${data.message}`);
      break;
    default: /* неизвестный кадр игнорируем */ break;
  }
}
```

## 3. Карточки в HTML

Кнопки строятся из `options`; `style: "danger"` — красная, `primary` — акцентная. Ответ —
один кадр `prompt`. Для `clarify` вариант `other` спрашивает текст.

```js
function showPrompt(data) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.prompt = CSS.escape(data.id);
  card.append(textEl('div', 'kind', `${data.kind} · ответьте кнопкой${data.timeout_seconds ? ` (до ${data.timeout_seconds} c)` : ''}`));
  card.append(textEl('div', '', data.text));

  const opts = document.createElement('div');
  opts.className = 'opts';
  (data.options || []).forEach((option) => {
    const button = document.createElement('button');
    button.textContent = option.label || option.id;
    if (option.style === 'danger') button.className = 'danger';
    if (option.style === 'primary') button.className = 'primary';
    button.addEventListener('click', () => {
      if (option.id === 'other') {
        const answer = window.prompt('Свой ответ:') || '';
        if (!answer.trim()) return;                     // other обязан нести непустой text
        send({ t: 'prompt', prompt_id: data.id, option_id: 'other', text: answer });
      } else {
        send({ t: 'prompt', prompt_id: data.id, option_id: option.id });
      }
      finishPrompt(data.id, option.id);
    });
    opts.append(button);
  });
  card.append(opts);
  log.appendChild(card);
}
```

## 4. Медиа: приём в blob

Файл от агента приходит base64 (или ссылкой `url`). Для больших файлов лучше собирать `Blob`
из байтов, а не `data:`-URL строкой — так не держим огромные строки в памяти дважды.

```js
function base64ToBytes(base64) {
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

function showMedia(m) {
  const box = document.createElement('div');
  box.className = 'msg agent media';
  let src;
  if (m.data) {
    const blob = new Blob([base64ToBytes(m.data)], { type: m.mime || 'application/octet-stream' });
    src = URL.createObjectURL(blob);          // не забудьте URL.revokeObjectURL(src) при удалении
  } else {
    src = m.url;                              // файл отдан ссылкой — качаем браузером
  }
  if (m.kind === 'image' || (m.mime || '').startsWith('image/')) {
    const img = document.createElement('img'); img.src = src; img.alt = m.filename || 'изображение'; box.append(img);
  } else if (m.kind === 'video' || (m.mime || '').startsWith('video/')) {
    const v = document.createElement('video'); v.src = src; v.controls = true; box.append(v);
  } else if (m.kind === 'voice' || (m.mime || '').startsWith('audio/')) {
    const a = document.createElement('audio'); a.src = src; a.controls = true; box.append(a);
  } else {
    const link = document.createElement('a'); link.href = src; link.download = m.filename || 'файл';
    link.textContent = `📄 ${m.filename || 'файл'}`; box.append(link);
  }
  log.appendChild(box);
}
```

## 5. Отправка файла кусками

```js
function sendMediaFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    const base64 = String(reader.result).split(',')[1] || '';
    const bytes = base64ToBytes(base64);
    const kind = file.type.startsWith('image/') ? 'image'
               : file.type.startsWith('video/') ? 'video'
               : file.type.startsWith('audio/') ? 'audio' : 'document';
    const id = `m-${Date.now().toString(36)}`;
    const parts = Math.max(1, Math.ceil(bytes.length / MEDIA_CHUNK_BYTES));
    for (let index = 0; index < parts; index++) {
      const chunk = bytes.slice(index * MEDIA_CHUNK_BYTES, (index + 1) * MEDIA_CHUNK_BYTES);
      let binary = '';
      for (let i = 0; i < chunk.length; i++) binary += String.fromCharCode(chunk[i]);
      send({ t: 'media', id, kind, mime: file.type || 'application/octet-stream', filename: file.name,
             data: btoa(binary), part: index, parts });   // досылать ВСЕ куски до parts-1
    }
  };
  reader.readAsDataURL(file);
}
```

## 6. Ограничения браузерного варианта

* **Токен читается из JS.** Кто угодно с доступом к вкладке/DevTools (`localStorage`,
  `window`) может его достать, а затем подключиться к шлюзу от вашего имени. Поэтому голый
  браузерный клиент годится только для **своих** (закрытая сеть, доверенные операторы) или для
  **тестов и пилота** — не для публичного веб-приложения. Для продакшена выдавайте токен из
  бэкенда на короткое время и проксируйте WebSocket через свой сервер, который один держит
  `MOBILE_TOKEN`.
* **`ws://` на `https://`-странице браузер заблокирует** (mixed content). Нужен `wss://`.
* **Нет переподключения из коробки** — при обрыве `WebSocket` не поднимается сам; реализуйте
  backoff вручную (как в `HermesClient`) или используйте `hermes-mobile-client`, который это
  умеет и в браузере.
* **CORS не мешает WebSocket**, но reverse-proxy должен корректно проксировать Upgrade-заголовки.
* **Ограничение кадра** — один WS-кадр ≤ 16 МБ, поэтому и в браузере медиа режется по 3 МБ.
