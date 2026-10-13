# Browser (raw WebSocket)

A minimal client without libraries: `WebSocket` + JSON. A reference working page with all
frames is `examples/web/index.html`; here we cover the key points and the limitations of the browser
approach.

## 1. Connection and handshake

The token is passed **only in the `hello` frame** — acceptance via `?token=` in the URL has been removed (the query string
ends up in the reverse-proxy logs). The open example below does exactly that.

```html
<script>
(() => {
  const PROTOCOL_VERSION = 1;
  const MEDIA_CHUNK_BYTES = 3 * 1024 * 1024;
  let socket = null;

  function connect(url, token, deviceId) {
    socket = new WebSocket(url);                // we do NOT pass the token in the URL

    socket.onopen = () => socket.send(JSON.stringify({
      t: 'hello', v: PROTOCOL_VERSION, token,    // token only here
      device: { id: deviceId, name: 'Браузер', platform: 'web', app_version: '1.0.0' },
    }));
    socket.onmessage = (event) => handle(String(event.data));
    socket.onerror = () => setStatus('Ошибка соединения: проверьте адрес и доступность порта');
    socket.onclose = (event) => {
      // 1008 — access denied; retrying is pointless.
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

## 2. Receiving frames

Parsing strictly by `t`; an unknown frame — `default` and nothing. We read optional fields
defensively (`data.edit`, `data.stop`).

```js
function handle(raw) {
  let data;
  try { data = JSON.parse(raw); } catch { return; }   // ignore garbage
  switch (data.t) {
    case 'ready':
      setStatus(`Подключено. Сессия: ${data.session}` + (data.v !== 1 ? ` (шлюз v${data.v})` : ''));
      // data.capabilities — source of truth: edit, media_in/out, prompts, max_media_bytes
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
      if (data.edit) {                                  // streaming: replace the text
        if (existing) existing.textContent = data.text;
        else { const n = log(data.text); n.dataset.msg = key; }
      } else if (existing) existing.textContent += data.text;   // long reply — in chunks
      else { const n = log(data.text); n.dataset.msg = key; }
      break;
    }
    case 'prompt':       showPrompt(data); break;
    case 'prompt_done':  finishPrompt(data.id, data.expired ? null : data.option_id); break;
    case 'media':
      if (data.parts > 1 && !data.final) break;         // wait for the last chunk
      showMedia(data);
      break;
    case 'error':
      setStatus(`Ошибка ${data.code}: ${data.message}`);
      break;
    default: /* ignore an unknown frame */ break;
  }
}
```

## 3. Cards in HTML

Buttons are built from `options`; `style: "danger"` — red, `primary` — accent. The answer is
a single `prompt` frame. For `clarify`, the `other` option asks for text.

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
        if (!answer.trim()) return;                     // other must carry a non-empty text
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

## 4. Media: receiving into a blob

A file from the agent arrives as base64 (or as a `url` link). For large files it's better to assemble a `Blob`
from bytes rather than a `data:` URL string — that way we don't keep huge strings in memory twice.

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
    src = URL.createObjectURL(blob);          // don't forget URL.revokeObjectURL(src) when removing
  } else {
    src = m.url;                              // the file is given as a link — download it with the browser
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

## 5. Sending a file in chunks

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
             data: btoa(binary), part: index, parts });   // send ALL chunks up to parts-1
    }
  };
  reader.readAsDataURL(file);
}
```

## 6. Limitations of the browser variant

* **The token is read from JS.** Anyone with access to the tab/DevTools (`localStorage`,
  `window`) can obtain it and then connect to the gateway under your name. That's why a raw
  browser client is only suitable for **your own** use (a closed network, trusted operators) or for
  **tests and a pilot** — not for a public web app. For production, issue a short-lived token from
  the backend and proxy the WebSocket through your own server, which alone holds
  `MOBILE_TOKEN`.
* **`ws://` on an `https://` page is blocked by the browser** (mixed content). You need `wss://`.
* **No reconnection out of the box** — on a drop, `WebSocket` doesn't come back up on its own; implement
  backoff manually (as in `HermesClient`) or use `hermes-mobile-client`, which does it
  and also works in the browser.
* **CORS doesn't block WebSocket**, but the reverse-proxy must correctly proxy the Upgrade headers.
* **Frame limit** — a single WS frame ≤ 16 MB, so in the browser media is also cut into 3 MB pieces.
