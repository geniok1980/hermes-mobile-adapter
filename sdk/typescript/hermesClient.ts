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

export const PROTOCOL_VERSION = 1;

/** Размер куска при отправке больших файлов (protocol.MEDIA_CHUNK_BYTES). */
export const MEDIA_CHUNK_BYTES = 3 * 1024 * 1024;

export type AvatarState = 'idle' | 'thinking' | 'speaking';

export type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'failed';

export interface ReadyPayload {
  v: number;
  session: string;
  device: { id: string; name: string };
  avatar_url: string;
  /** Новые возможности необязательны — старый шлюз их не шлёт. */
  capabilities: {
    text: boolean;
    voice: boolean;
    typing: boolean;
    avatar_states?: AvatarState[];
    media_in?: string[];
    media_out?: string[];
    prompts?: string[];
    /** Умеет отдавать каталог команд агента (кадр `commands`). */
    commands?: boolean;
    /** Умеет отдавать список моделей для выбора (кадр `models`). */
    models?: boolean;
    edit?: boolean;
    max_media_bytes?: number;
    max_text_chars?: number;
  };
}

export interface AgentMessage {
  id: string;
  text: string;
  index: number;
  total: number;
  final: boolean;
  /** true — это правка уже показанного сообщения с тем же `id` (стриминг). */
  edit?: boolean;
}

/** Вариант ответа на карточку (кнопка). */
export interface PromptOption {
  id: string;
  label: string;
  style?: string;
}

/** Карточка-вопрос от агента: exec_approval | slash_confirm | clarify. */
export interface PromptPayload {
  id: string;
  kind: string;
  text: string;
  options: PromptOption[];
  session: string;
  timeout_seconds: number;
}

/** Карточка закрыта: применённый вариант либо признак истечения. */
export interface PromptDonePayload {
  id: string;
  option_id?: string;
  expired?: boolean;
}

/** Медиа от агента. `data` — base64 целого файла (все части собраны), либо только `url`. */
export interface MediaPayload {
  id: string;
  kind: string;
  mime: string;
  filename: string;
  caption: string;
  part: number;
  parts: number;
  final: boolean;
  size: number;
  data?: string;
  url?: string;
  reply_to?: string;
}

/** Команда агента для списка команд в приложении (кадр `commands`). */
export interface CommandEntry {
  name: string;
  args: string;
  description: string;
  category: string;
  aliases: string[];
}

/** Каталог команд агента: `{t:"commands", v, commands:[…]}`. */
export interface CommandsPayload {
  version: number;
  commands: CommandEntry[];
}

/** Список моделей для выбора: `{t:"models", v, provider, current, models:[…]}`. */
export interface ModelsPayload {
  version: number;
  provider: string;
  current: string;
  models: string[];
}

export interface ProtocolError {
  code: string;
  message: string;
  fatal: boolean;
}

export interface HermesClientHandlers {
  onState?: (state: ConnectionState, detail?: string) => void;
  onReady?: (ready: ReadyPayload) => void;
  onMessage?: (message: AgentMessage) => void;
  /** Пришла правка сообщения (`message.edit`) — перерисовать пузырь с этим `id`. */
  onMessageEdit?: (message: AgentMessage) => void;
  onTyping?: () => void;
  /** Пришёл `typing.stop` — погасить индикатор «печатает». */
  onTypingStop?: () => void;
  onAvatar?: (state: AvatarState) => void;
  onPrompt?: (prompt: PromptPayload) => void;
  onPromptDone?: (done: PromptDonePayload) => void;
  onMedia?: (media: MediaPayload) => void;
  /** Пришёл каталог команд агента (ответ на `requestCommands`). */
  onCommands?: (payload: CommandsPayload) => void;
  /** Пришёл список моделей для выбора (ответ на `requestModels`). */
  onModels?: (payload: ModelsPayload) => void;
  onError?: (error: ProtocolError) => void;
}

export interface DeviceInfo {
  id: string;
  name: string;
  platform: string;
  appVersion: string;
}

export interface ClientOptions {
  url: string;
  token: string;
  device: DeviceInfo;
  user?: { id?: string; name?: string };
  handlers?: HermesClientHandlers;
  /** Максимальная пауза между попытками переподключения, мс. */
  maxBackoffMs?: number;
}

/** Источник медиа: base64-строка, готовые байты или путь к файлу (в Node читается с диска). */
export type MediaSource = string | Uint8Array | { path: string };

export interface MediaSendOptions {
  mime?: string;
  filename?: string;
  caption?: string;
  messageId?: string;
  /** Размер куска в байтах (по умолчанию 3 МБ). */
  chunkBytes?: number;
}

const CLOSE_POLICY_VIOLATION = 1008;

/** События WebSocket, которые реально нужны приложению (не тянем типы из окружения). */
interface SocketMessageEvent {
  data: unknown;
}

interface SocketCloseEvent {
  code: number;
  reason: string;
}

export class HermesClient {
  private socket: WebSocket | null = null;
  private readonly options: ClientOptions;
  private handlers: HermesClientHandlers;
  private attempt = 0;
  private closedByUser = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private state: ConnectionState = 'idle';
  /** Сборка многочастных медиа от агента: id → части. */
  private mediaBuffers = new Map<string, { parts: number; chunks: Uint8Array[]; meta: MediaPayload }>();

  constructor(options: ClientOptions) {
    this.options = options;
    this.handlers = options.handlers ?? {};
  }

  setHandlers(handlers: HermesClientHandlers): void {
    this.handlers = { ...this.handlers, ...handlers };
  }

  get ready(): boolean {
    return this.state === 'connected';
  }

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  disconnect(): void {
    this.closedByUser = true;
    this.clearReconnect();
    this.socket?.close();
    this.socket = null;
    this.setState('idle');
  }

  /** Текстовое сообщение агенту. `id` обеспечивает идемпотентность на стороне Hermes. */
  sendText(text: string, messageId?: string): string {
    if (!String(text ?? '').trim()) {
      throw new Error('sendText: пустое сообщение — адаптер ответит ошибкой bad_frame');
    }
    const id = messageId ?? makeId();
    this.send({ t: 'text', id, text });
    return id;
  }

  /** Голосовое сообщение: WAV/OGG в base64 (+ необязательный распознанный текст). */
  sendAudio(base64: string, format: 'wav' | 'ogg', transcript?: string, messageId?: string): string {
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

  sendTyping(): void {
    this.send({ t: 'typing' });
  }

  acknowledge(messageId: string): void {
    this.send({ t: 'ack', id: messageId });
  }

  ping(): void {
    this.send({ t: 'ping', ts: Date.now() });
  }

  /** Просит у агента каталог команд — ответ придёт в `onCommands`. */
  requestCommands(): void {
    this.send({ t: 'commands' });
  }

  /** Просит у агента список моделей — ответ придёт в `onModels`. */
  requestModels(): void {
    this.send({ t: 'models' });
  }

  // ── карточки-вопросы ──────────────────────────────────────────────────────

  /**
   * Ответ на карточку-вопрос.
   *
   * exec_approval — `once|session|always|deny`; slash_confirm — `once|always|cancel`;
   * clarify — `c0..cN` либо `other` вместе с текстом. Незнакомое значение адаптер
   * заменит на безопасное (`deny`/`cancel`).
   */
  answerPrompt(promptId: string, optionId?: string, text?: string): void {
    if (!promptId) throw new Error('answerPrompt: нужен promptId');
    if (!optionId && !text) throw new Error('answerPrompt: укажите optionId или text');
    const frame: Record<string, unknown> = { t: 'prompt', prompt_id: promptId };
    if (optionId) frame.option_id = optionId.toLowerCase();
    if (text) frame.text = text;
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
  sendMedia(kind: string, source: MediaSource, options: MediaSendOptions = {}): string {
    const normalized = normalizeKind(kind);
    const bytes = sourceToBytes(source);
    if (bytes.length === 0) throw new Error('sendMedia: пустой файл');
    const chunkBytes = options.chunkBytes && options.chunkBytes > 0 ? options.chunkBytes : MEDIA_CHUNK_BYTES;
    const id = options.messageId ?? makeId();
    const mime = options.mime ?? guessMime(options.filename ?? pathName(source));
    const parts = Math.max(1, Math.ceil(bytes.length / chunkBytes));
    for (let index = 0; index < parts; index += 1) {
      const chunk = bytes.subarray(index * chunkBytes, (index + 1) * chunkBytes);
      const frame: Record<string, unknown> = {
        t: 'media',
        id,
        kind: normalized,
        mime,
        data: bytesToBase64(chunk),
        part: index,
        parts,
      };
      if (options.filename) frame.filename = options.filename;
      if (options.caption) frame.caption = options.caption;
      this.send(frame);
    }
    return id;
  }

  /** Фото: base64, байты или путь к файлу. */
  sendImage(source: MediaSource, options: MediaSendOptions = {}): string {
    return this.sendMedia('image', source, options);
  }

  /** Файл (PDF, zip…): base64, байты или путь к файлу. */
  sendDocument(source: MediaSource, options: MediaSendOptions = {}): string {
    return this.sendMedia('document', source, options);
  }

  /** Голосовое: base64, байты или путь к файлу (kind=audio). */
  sendVoice(source: MediaSource, options: MediaSendOptions = {}): string {
    return this.sendMedia('audio', source, options);
  }

  // ── внутреннее ────────────────────────────────────────────────────────────

  private open(): void {
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');
    // Токен не уходит в URL: query-строки оседают в логах прокси и в истории браузера.
    // Аутентификация — кадром hello (см. ниже).
    const socket = new WebSocket(this.options.url);
    this.socket = socket;

    socket.onopen = () => {
      socket.send(
        JSON.stringify({
          t: 'hello',
          v: PROTOCOL_VERSION,
          token: this.options.token,
          device: this.options.device,
          user: this.options.user ?? {},
        }),
      );
    };

    socket.onmessage = (event: SocketMessageEvent) => this.handleFrame(String(event.data));

    socket.onerror = () => {
      this.handlers.onError?.({ code: 'transport', message: 'Соединение с шлюзом прервано', fatal: false });
    };

    socket.onclose = (event: SocketCloseEvent) => {
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

  private handleFrame(raw: string): void {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return; // мусор в канале игнорируем
    }
    const kind = String(frame.t ?? '');
    switch (kind) {
      case 'ready':
        this.attempt = 0;
        this.setState('connected');
        this.handlers.onReady?.(frame as unknown as ReadyPayload);
        return;
      case 'typing':
        if (frame.stop) {
          this.handlers.onTypingStop?.();
        } else {
          this.handlers.onTyping?.();
        }
        return;
      case 'message': {
        const chunk = (frame.chunk ?? {}) as { index?: number; total?: number };
        const message: AgentMessage = {
          id: String(frame.id ?? ''),
          text: String(frame.text ?? ''),
          index: Number(chunk.index ?? 0),
          total: Number(chunk.total ?? 1),
          final: Boolean(frame.final),
          edit: Boolean(frame.edit),
        };
        if (message.edit) {
          this.handlers.onMessageEdit?.(message);
        } else {
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
        if (complete) this.handlers.onMedia?.(complete);
        return;
      }
      case 'commands':
        this.handlers.onCommands?.(parseCommands(frame));
        return;
      case 'models':
        this.handlers.onModels?.(parseModels(frame));
        return;
      case 'avatar':
        this.handlers.onAvatar?.((frame.state as AvatarState) ?? 'idle');
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
  private accumulateMedia(frame: Record<string, unknown>): MediaPayload | null {
    const id = String(frame.id ?? '');
    const parts = Number(frame.parts ?? 1) || 1;
    const part = Number(frame.part ?? 0) || 0;
    const chunk = frame.data ? base64ToBytes(String(frame.data)) : new Uint8Array(0);
    const meta: MediaPayload = {
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
    for (let i = 0; i < parts; i += 1) if (buffer.chunks[i]) received += 1;
    if (received < parts) {
      this.mediaBuffers.set(id, buffer);
      return null;
    }
    this.mediaBuffers.delete(id);
    const total = concatBytes(buffer.chunks, parts);
    return { ...buffer.meta, data: bytesToBase64(total), size: buffer.meta.size || total.length };
  }

  private send(frame: Record<string, unknown>): void {
    if (!this.socket || this.socket.readyState !== 1) {
      this.handlers.onError?.({ code: 'offline', message: 'Нет связи со шлюзом', fatal: false });
      return;
    }
    this.socket.send(JSON.stringify(frame));
  }

  private scheduleReconnect(): void {
    this.attempt += 1;
    const maxBackoff = this.options.maxBackoffMs ?? 15000;
    const delay = Math.min(maxBackoff, 500 * 2 ** Math.min(this.attempt, 5));
    this.setState('reconnecting', `попытка ${this.attempt}`);
    this.clearReconnect();
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private setState(state: ConnectionState, detail?: string): void {
    this.state = state;
    this.handlers.onState?.(state, detail);
  }
}

/** Простой идентификатор сообщения (достаточно для идемпотентности в пределах сессии). */
export function makeId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

// ── помощники медиа (без зависимостей, работают и в браузере, и в Node) ────────

/** Разбирает кадр `commands`: незнакомые поля не роняют клиента. */
function parseCommands(frame: Record<string, unknown>): CommandsPayload {
  const list = Array.isArray(frame.commands) ? frame.commands : [];
  return {
    version: Number(frame.v ?? 1),
    commands: list.map(raw => {
      const item = (raw ?? {}) as Record<string, unknown>;
      const aliases = Array.isArray(item.aliases) ? item.aliases : [];
      return {
        name: String(item.name ?? ''),
        args: String(item.args ?? ''),
        description: String(item.description ?? ''),
        category: String(item.category ?? ''),
        aliases: aliases.map(alias => String(alias)),
      };
    }),
  };
}

/** Разбирает кадр `models`: список моделей и текущая. */
function parseModels(frame: Record<string, unknown>): ModelsPayload {
  const list = Array.isArray(frame.models) ? frame.models : [];
  return {
    version: Number(frame.v ?? 1),
    provider: String(frame.provider ?? ''),
    current: String(frame.current ?? ''),
    models: list.map(model => String(model)),
  };
}

function parseOptions(raw: unknown): PromptOption[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null && Boolean((item as { id?: unknown }).id))
    .map((item) => ({ id: String(item.id), label: String(item.label ?? ''), style: item.style ? String(item.style) : undefined }));
}

const INBOUND_MEDIA_KINDS = ['image', 'video', 'document', 'audio'];

function normalizeKind(kind: string): string {
  const value = String(kind ?? '').toLowerCase();
  if (!INBOUND_MEDIA_KINDS.includes(value)) {
    throw new Error(`sendMedia: недопустимый тип ${kind} (можно: ${INBOUND_MEDIA_KINDS.join(', ')})`);
  }
  return value;
}

function pathName(source: MediaSource): string | undefined {
  if (typeof source === 'string') return looksLikePath(source) ? basename(source) : undefined;
  return typeof source === 'object' && source !== null && 'path' in source ? source.path : undefined;
}

/** Похоже ли значение на путь к файлу, а не на base64 или data-url. */
function looksLikePath(value: string): boolean {
  if (!value || value.length > 4096) return false;
  if (value.startsWith('data:')) return false; // data-url — это уже содержимое
  if (/^[A-Za-z]:[\\/]/.test(value)) return true; // Windows: C:\… или C:/…
  if (value.startsWith('file://')) return true;
  if (value.startsWith('/') || value.startsWith('./') || value.startsWith('../') || value.startsWith('~')) {
    return true;
  }
  // Относительный путь: есть разделитель и расширение в конце — типичный файл, а не base64.
  return /[\\/]/.test(value) && /\.[A-Za-z0-9]{1,6}$/.test(value);
}

function basename(value: string): string {
  const clean = value.startsWith('file://') ? value.slice(7) : value;
  return clean.split(/[\\/]/).pop() || clean;
}

/** Приводит источник к байтам: base64/data-url → декодируем, путь → читаем с диска (Node). */
function sourceToBytes(source: MediaSource): Uint8Array {
  if (typeof source === 'string') {
    // Строка может быть как base64, так и путём к файлу — путь распознаём по виду
    // (в Python-SDK так же), иначе разработчик получает «мусор» из декодированного пути.
    return looksLikePath(source) ? readLocalFile(source) : base64ToBytes(source);
  }
  if (source instanceof Uint8Array) return source;
  if (source && typeof source === 'object' && 'path' in source) {
    return readLocalFile(source.path);
  }
  throw new Error('sendMedia: неподдерживаемый источник');
}

function readLocalFile(path: string): Uint8Array {
  const readFile = nodeRequire('fs')?.readFileSync;
  if (typeof readFile !== 'function') {
    throw new Error(
      `sendMedia: файл «${path}» можно прочитать только в Node (fs); в приложении передайте Uint8Array или base64`,
    );
  }
  const clean = path.startsWith('file://') ? path.slice(7) : path;
  const raw = readFile(clean);
  return raw instanceof Uint8Array ? raw : new Uint8Array(raw);
}

/** `require` из Node без статических типов — чтобы файл собирался и в браузере. */
function nodeRequire(module: string): { readFileSync?: (path: string) => Uint8Array } | null {
  const global = globalThis as unknown as { require?: (id: string) => unknown };
  if (typeof global.require !== 'function') return null;
  try {
    return global.require(module) as { readFileSync?: (path: string) => Uint8Array };
  } catch {
    return null;
  }
}

function guessMime(filename: string | undefined): string {
  const table: Record<string, string> = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp',
    '.gif': 'image/gif', '.heic': 'image/heic', '.mp4': 'video/mp4', '.mov': 'video/quicktime',
    '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.ogg': 'audio/ogg',
    '.wav': 'audio/wav', '.opus': 'audio/opus', '.pdf': 'application/pdf', '.txt': 'text/plain',
    '.zip': 'application/zip',
  };
  const low = String(filename ?? '').toLowerCase();
  for (const ext of Object.keys(table)) if (low.endsWith(ext)) return table[ext];
  return 'application/octet-stream';
}

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

/** Кодирование байтов в base64 без зависимостей (portable между браузером и Node). */
export function bytesToBase64(bytes: Uint8Array): string {
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
export function base64ToBytes(input: string): Uint8Array {
  const clean = String(input ?? '').replace(/[^A-Za-z0-9+/=]/g, '');
  const length = clean.length;
  const out = new Uint8Array(((length * 3) >> 2) - (clean.endsWith('==') ? 2 : clean.endsWith('=') ? 1 : 0));
  let outIndex = 0;
  for (let i = 0; i < length; i += 4) {
    const c0 = B64.indexOf(clean[i]);
    const c1 = B64.indexOf(clean[i + 1]);
    const c2 = clean[i + 2] === '=' || clean[i + 2] === undefined ? -1 : B64.indexOf(clean[i + 2]);
    const c3 = clean[i + 3] === '=' || clean[i + 3] === undefined ? -1 : B64.indexOf(clean[i + 3]);
    if (outIndex < out.length) out[outIndex++] = (c0 << 2) | (c1 >> 4);
    if (c2 >= 0 && outIndex < out.length) out[outIndex++] = ((c1 & 15) << 4) | (c2 >> 2);
    if (c3 >= 0 && outIndex < out.length) out[outIndex++] = ((c2 & 3) << 6) | c3;
  }
  return out;
}

function concatBytes(chunks: Uint8Array[], limit: number): Uint8Array {
  let size = 0;
  for (let i = 0; i < limit; i += 1) size += chunks[i] ? chunks[i].length : 0;
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
