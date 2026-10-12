"use strict";
/**
 * Клиент протокола Hermes Mobile Adapter (версия 1).
 *
 * Отвечает за: подключение к адаптеру Hermes, авторизацию, переподключение с backoff,
 * отправку текста, голоса и медиа (фото/файлы/голос), приём ответов агента, индикатора
 * «печатает» (в том числе остановку — `typing.stop`), состояний аватара, карточек-вопросов
 * (`prompt`/`prompt_done`) и медиафайлов от агента (`media`), включая правку уже
 * показанного сообщения (`message.edit` — потоковая выдача). Ничего не знает про UI —
 * только колбэки. Никаких зависимостей: стандартный WebSocket.
 *
 * Спецификация: site-docs/protocol/index.md
 */
Object.defineProperty(exports, "__esModule", { value: true });
exports.HermesClient = exports.MEDIA_CHUNK_BYTES = exports.PROTOCOL_VERSION = void 0;
exports.makeId = makeId;
exports.bytesToBase64 = bytesToBase64;
exports.base64ToBytes = base64ToBytes;
exports.PROTOCOL_VERSION = 1;
/** Размер куска при отправке больших файлов (protocol.MEDIA_CHUNK_BYTES). */
exports.MEDIA_CHUNK_BYTES = 3 * 1024 * 1024;
const CLOSE_POLICY_VIOLATION = 1008;
class HermesClient {
    constructor(options) {
        this.socket = null;
        this.attempt = 0;
        this.closedByUser = false;
        this.reconnectTimer = null;
        this.state = 'idle';
        /** Сборка многочастных медиа от агента: id → части. */
        this.mediaBuffers = new Map();
        this.options = options;
        this.handlers = options.handlers ?? {};
    }
    setHandlers(handlers) {
        this.handlers = { ...this.handlers, ...handlers };
    }
    get ready() {
        return this.state === 'connected';
    }
    connect() {
        this.closedByUser = false;
        this.open();
    }
    disconnect() {
        this.closedByUser = true;
        this.clearReconnect();
        this.socket?.close();
        this.socket = null;
        this.setState('idle');
    }
    /** Текстовое сообщение агенту. `id` обеспечивает идемпотентность на стороне Hermes. */
    sendText(text, messageId) {
        if (!String(text ?? '').trim()) {
            throw new Error('sendText: пустое сообщение — адаптер ответит ошибкой bad_frame');
        }
        const id = messageId ?? makeId();
        this.send({ t: 'text', id, text });
        return id;
    }
    /** Голосовое сообщение: WAV/OGG в base64 (+ необязательный распознанный текст). */
    sendAudio(base64, format, transcript, messageId) {
        if (format !== 'wav' && format !== 'ogg') {
            throw new Error(`sendAudio: поддерживаются только wav и ogg, получено «${String(format)}»`);
        }
        if (!base64) {
            throw new Error('sendAudio: пустая запись');
        }
        const id = messageId ?? makeId();
        this.send({ t: 'audio', id, format, data: base64, text: transcript });
        return id;
    }
    sendTyping() {
        this.send({ t: 'typing' });
    }
    acknowledge(messageId) {
        this.send({ t: 'ack', id: messageId });
    }
    ping() {
        this.send({ t: 'ping', ts: Date.now() });
    }
    // ── карточки-вопросы ──────────────────────────────────────────────────────
    /**
     * Ответ на карточку-вопрос.
     *
     * exec_approval — `once|session|always|deny`; slash_confirm — `once|always|cancel`;
     * clarify — `c0..cN` либо `other` вместе с текстом. Незнакомое значение адаптер
     * заменит на безопасное (`deny`/`cancel`).
     */
    answerPrompt(promptId, optionId, text) {
        if (!promptId)
            throw new Error('answerPrompt: нужен promptId');
        if (!optionId && !text)
            throw new Error('answerPrompt: укажите optionId или text');
        const frame = { t: 'prompt', prompt_id: promptId };
        if (optionId)
            frame.option_id = optionId.toLowerCase();
        if (text)
            frame.text = text;
        this.send(frame);
    }
    // ── отправка медиа ────────────────────────────────────────────────────────
    /**
     * Отправляет медиа агенту, автоматически нарезая файл по `chunkBytes` (3 МБ).
     *
     * `source` — base64, байты или `{ path }` (в Node читается с диска). Куски идут
     * кадрами `media` с общим `id` и полями `part`/`parts`; адаптер собирает их в файл,
     * поэтому большие файлы не упираются в лимит одного кадра.
     */
    sendMedia(kind, source, options = {}) {
        const normalized = normalizeKind(kind);
        const bytes = sourceToBytes(source);
        if (bytes.length === 0)
            throw new Error('sendMedia: пустой файл');
        const chunkBytes = options.chunkBytes && options.chunkBytes > 0 ? options.chunkBytes : exports.MEDIA_CHUNK_BYTES;
        const id = options.messageId ?? makeId();
        const mime = options.mime ?? guessMime(options.filename ?? pathName(source));
        const parts = Math.max(1, Math.ceil(bytes.length / chunkBytes));
        for (let index = 0; index < parts; index += 1) {
            const chunk = bytes.subarray(index * chunkBytes, (index + 1) * chunkBytes);
            const frame = {
                t: 'media',
                id,
                kind: normalized,
                mime,
                data: bytesToBase64(chunk),
                part: index,
                parts,
            };
            if (options.filename)
                frame.filename = options.filename;
            if (options.caption)
                frame.caption = options.caption;
            this.send(frame);
        }
        return id;
    }
    /** Фото: base64, байты или путь к файлу. */
    sendImage(source, options = {}) {
        return this.sendMedia('image', source, options);
    }
    /** Файл (PDF, zip…): base64, байты или путь к файлу. */
    sendDocument(source, options = {}) {
        return this.sendMedia('document', source, options);
    }
    /** Голосовое: base64, байты или путь к файлу (kind=audio). */
    sendVoice(source, options = {}) {
        return this.sendMedia('audio', source, options);
    }
    // ── внутреннее ────────────────────────────────────────────────────────────
    open() {
        this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');
        // Токен не уходит в URL: query-строки оседают в логах прокси и в истории браузера.
        // Аутентификация — кадром hello (см. ниже).
        const socket = new WebSocket(this.options.url);
        this.socket = socket;
        socket.onopen = () => {
            socket.send(JSON.stringify({
                t: 'hello',
                v: exports.PROTOCOL_VERSION,
                token: this.options.token,
                device: this.options.device,
                user: this.options.user ?? {},
            }));
        };
        socket.onmessage = (event) => this.handleFrame(String(event.data));
        socket.onerror = () => {
            this.handlers.onError?.({ code: 'transport', message: 'Соединение с шлюзом прервано', fatal: false });
        };
        socket.onclose = (event) => {
            this.socket = null;
            if (this.closedByUser) {
                return;
            }
            if (event.code === CLOSE_POLICY_VIOLATION) {
                // Отказ в доступе: повторять бессмысленно — приложение показывает «нет доступа».
                this.setState('failed', `Код ${event.code}`);
                this.handlers.onError?.({
                    code: 'auth_failed',
                    message: 'Шлюз отказал в доступе: проверьте токен приложения',
                    fatal: true,
                });
                return;
            }
            this.scheduleReconnect();
        };
    }
    handleFrame(raw) {
        let frame;
        try {
            frame = JSON.parse(raw);
        }
        catch {
            return; // мусор в канале игнорируем
        }
        const kind = String(frame.t ?? '');
        switch (kind) {
            case 'ready':
                this.attempt = 0;
                this.setState('connected');
                this.handlers.onReady?.(frame);
                return;
            case 'typing':
                if (frame.stop) {
                    this.handlers.onTypingStop?.();
                }
                else {
                    this.handlers.onTyping?.();
                }
                return;
            case 'message': {
                const chunk = (frame.chunk ?? {});
                const message = {
                    id: String(frame.id ?? ''),
                    text: String(frame.text ?? ''),
                    index: Number(chunk.index ?? 0),
                    total: Number(chunk.total ?? 1),
                    final: Boolean(frame.final),
                    edit: Boolean(frame.edit),
                };
                if (message.edit) {
                    this.handlers.onMessageEdit?.(message);
                }
                else {
                    this.handlers.onMessage?.(message);
                }
                return;
            }
            case 'prompt':
                this.handlers.onPrompt?.({
                    id: String(frame.id ?? ''),
                    kind: String(frame.kind ?? ''),
                    text: String(frame.text ?? ''),
                    options: parseOptions(frame.options),
                    session: String(frame.session ?? ''),
                    timeout_seconds: Number(frame.timeout_seconds ?? 0),
                });
                return;
            case 'prompt_done':
                this.handlers.onPromptDone?.({
                    id: String(frame.id ?? ''),
                    option_id: frame.option_id === undefined ? undefined : String(frame.option_id),
                    expired: frame.expired === undefined ? undefined : Boolean(frame.expired),
                });
                return;
            case 'media': {
                const complete = this.accumulateMedia(frame);
                if (complete)
                    this.handlers.onMedia?.(complete);
                return;
            }
            case 'avatar':
                this.handlers.onAvatar?.(frame.state ?? 'idle');
                return;
            case 'error':
                this.handlers.onError?.({
                    code: String(frame.code ?? 'error'),
                    message: String(frame.message ?? ''),
                    fatal: Boolean(frame.fatal),
                });
                return;
            case 'pong':
            default:
                return; // неизвестные кадры игнорируем — так требует протокол
        }
    }
    /**
     * Собирает многочастные медиа в целый файл. Наверх отдаём только готовый файл —
     * иначе потребитель получил бы обрывок картинки.
     */
    accumulateMedia(frame) {
        const id = String(frame.id ?? '');
        const parts = Number(frame.parts ?? 1) || 1;
        const part = Number(frame.part ?? 0) || 0;
        const chunk = frame.data ? base64ToBytes(String(frame.data)) : new Uint8Array(0);
        const meta = {
            id,
            kind: String(frame.kind ?? ''),
            mime: String(frame.mime ?? ''),
            filename: String(frame.filename ?? ''),
            caption: String(frame.caption ?? ''),
            part,
            parts,
            final: Boolean(frame.final),
            size: Number(frame.size ?? 0),
            url: frame.url === undefined ? undefined : String(frame.url),
            reply_to: frame.reply_to === undefined ? undefined : String(frame.reply_to),
        };
        if (parts <= 1) {
            return { ...meta, data: bytesToBase64(chunk) };
        }
        const buffer = this.mediaBuffers.get(id) ?? { parts, chunks: new Array(parts), meta };
        buffer.parts = parts;
        buffer.chunks[part] = chunk;
        buffer.meta = meta; // последний кадр несёт url/reply_to/caption
        let received = 0;
        for (let i = 0; i < parts; i += 1)
            if (buffer.chunks[i])
                received += 1;
        if (received < parts) {
            this.mediaBuffers.set(id, buffer);
            return null;
        }
        this.mediaBuffers.delete(id);
        const total = concatBytes(buffer.chunks, parts);
        return { ...buffer.meta, data: bytesToBase64(total), size: buffer.meta.size || total.length };
    }
    send(frame) {
        if (!this.socket || this.socket.readyState !== 1) {
            this.handlers.onError?.({ code: 'offline', message: 'Нет связи со шлюзом', fatal: false });
            return;
        }
        this.socket.send(JSON.stringify(frame));
    }
    scheduleReconnect() {
        this.attempt += 1;
        const maxBackoff = this.options.maxBackoffMs ?? 15000;
        const delay = Math.min(maxBackoff, 500 * 2 ** Math.min(this.attempt, 5));
        this.setState('reconnecting', `попытка ${this.attempt}`);
        this.clearReconnect();
        this.reconnectTimer = setTimeout(() => this.open(), delay);
    }
    clearReconnect() {
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
    }
    setState(state, detail) {
        this.state = state;
        this.handlers.onState?.(state, detail);
    }
}
exports.HermesClient = HermesClient;
/** Простой идентификатор сообщения (достаточно для идемпотентности в пределах сессии). */
function makeId() {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
// ── помощники медиа (без зависимостей, работают и в браузере, и в Node) ────────
function parseOptions(raw) {
    if (!Array.isArray(raw))
        return [];
    return raw
        .filter((item) => typeof item === 'object' && item !== null && Boolean(item.id))
        .map((item) => ({ id: String(item.id), label: String(item.label ?? ''), style: item.style ? String(item.style) : undefined }));
}
const INBOUND_MEDIA_KINDS = ['image', 'video', 'document', 'audio'];
function normalizeKind(kind) {
    const value = String(kind ?? '').toLowerCase();
    if (!INBOUND_MEDIA_KINDS.includes(value)) {
        throw new Error(`sendMedia: недопустимый тип ${kind} (можно: ${INBOUND_MEDIA_KINDS.join(', ')})`);
    }
    return value;
}
function pathName(source) {
    if (typeof source === 'string')
        return looksLikePath(source) ? basename(source) : undefined;
    return typeof source === 'object' && source !== null && 'path' in source ? source.path : undefined;
}
/** Похоже ли значение на путь к файлу, а не на base64 или data-url. */
function looksLikePath(value) {
    if (!value || value.length > 4096)
        return false;
    if (value.startsWith('data:'))
        return false; // data-url — это уже содержимое
    if (/^[A-Za-z]:[\\/]/.test(value))
        return true; // Windows: C:\… или C:/…
    if (value.startsWith('file://'))
        return true;
    if (value.startsWith('/') || value.startsWith('./') || value.startsWith('../') || value.startsWith('~')) {
        return true;
    }
    // Относительный путь: есть разделитель и расширение в конце — типичный файл, а не base64.
    return /[\\/]/.test(value) && /\.[A-Za-z0-9]{1,6}$/.test(value);
}
function basename(value) {
    const clean = value.startsWith('file://') ? value.slice(7) : value;
    return clean.split(/[\\/]/).pop() || clean;
}
/** Приводит источник к байтам: base64/data-url → декодируем, путь → читаем с диска (Node). */
function sourceToBytes(source) {
    if (typeof source === 'string') {
        // Строка может быть как base64, так и путём к файлу — путь распознаём по виду
        // (в Python-SDK так же), иначе разработчик получает «мусор» из декодированного пути.
        return looksLikePath(source) ? readLocalFile(source) : base64ToBytes(source);
    }
    if (source instanceof Uint8Array)
        return source;
    if (source && typeof source === 'object' && 'path' in source) {
        return readLocalFile(source.path);
    }
    throw new Error('sendMedia: неподдерживаемый источник');
}
function readLocalFile(path) {
    const readFile = nodeRequire('fs')?.readFileSync;
    if (typeof readFile !== 'function') {
        throw new Error(`sendMedia: файл «${path}» можно прочитать только в Node (fs); в приложении передайте Uint8Array или base64`);
    }
    const clean = path.startsWith('file://') ? path.slice(7) : path;
    const raw = readFile(clean);
    return raw instanceof Uint8Array ? raw : new Uint8Array(raw);
}
/** `require` из Node без статических типов — чтобы файл собирался и в браузере. */
function nodeRequire(module) {
    const global = globalThis;
    if (typeof global.require !== 'function')
        return null;
    try {
        return global.require(module);
    }
    catch {
        return null;
    }
}
function guessMime(filename) {
    const table = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
        '.gif': 'image/gif', '.heic': 'image/heic', '.mp4': 'video/mp4', '.mov': 'video/quicktime',
        '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg',
        '.wav': 'audio/wav', '.opus': 'audio/opus', '.pdf': 'application/pdf', '.txt': 'text/plain',
        '.zip': 'application/zip',
    };
    const low = String(filename ?? '').toLowerCase();
    for (const ext of Object.keys(table))
        if (low.endsWith(ext))
            return table[ext];
    return 'application/octet-stream';
}
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
/** Кодирование байтов в base64 без зависимостей (portable между браузером и Node). */
function bytesToBase64(bytes) {
    let out = '';
    for (let i = 0; i < bytes.length; i += 3) {
        const b0 = bytes[i];
        const b1 = i + 1 < bytes.length ? bytes[i + 1] : 0;
        const b2 = i + 2 < bytes.length ? bytes[i + 2] : 0;
        out += B64[b0 >> 2];
        out += B64[((b0 & 3) << 4) | (b1 >> 4)];
        out += i + 1 < bytes.length ? B64[((b1 & 15) << 2) | (b2 >> 6)] : '=';
        out += i + 2 < bytes.length ? B64[b2 & 63] : '=';
    }
    return out;
}
/** Декодирование base64 в байты — своя реализация, чтобы не зависеть от atob/Buffer. */
function base64ToBytes(input) {
    const clean = String(input ?? '').replace(/[^A-Za-z0-9+/=]/g, '');
    const length = clean.length;
    const out = new Uint8Array(((length * 3) >> 2) - (clean.endsWith('==') ? 2 : clean.endsWith('=') ? 1 : 0));
    let outIndex = 0;
    for (let i = 0; i < length; i += 4) {
        const c0 = B64.indexOf(clean[i]);
        const c1 = B64.indexOf(clean[i + 1]);
        const c2 = clean[i + 2] === '=' || clean[i + 2] === undefined ? -1 : B64.indexOf(clean[i + 2]);
        const c3 = clean[i + 3] === '=' || clean[i + 3] === undefined ? -1 : B64.indexOf(clean[i + 3]);
        if (outIndex < out.length)
            out[outIndex++] = (c0 << 2) | (c1 >> 4);
        if (c2 >= 0 && outIndex < out.length)
            out[outIndex++] = ((c1 & 15) << 4) | (c2 >> 2);
        if (c3 >= 0 && outIndex < out.length)
            out[outIndex++] = ((c2 & 3) << 6) | c3;
    }
    return out;
}
function concatBytes(chunks, limit) {
    let size = 0;
    for (let i = 0; i < limit; i += 1)
        size += chunks[i] ? chunks[i].length : 0;
    const out = new Uint8Array(size);
    let offset = 0;
    for (let i = 0; i < limit; i += 1) {
        const chunk = chunks[i];
        if (chunk) {
            out.set(chunk, offset);
            offset += chunk.length;
        }
    }
    return out;
}
