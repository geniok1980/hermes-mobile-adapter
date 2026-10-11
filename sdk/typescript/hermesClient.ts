/**
 * Клиент протокола Hermes Go Mobile (версия 1).
 *
 * Отвечает за: подключение к адаптеру Hermes, авторизацию, переподключение с backoff,
 * отправку текста и голоса, приём ответов агента, индикатора «печатает» и состояний
 * аватара. Ничего не знает про UI — только колбэки.
 *
 * Спецификация: docs/protocol.md
 */

export const PROTOCOL_VERSION = 1;

export type AvatarState = 'idle' | 'thinking' | 'speaking';

export type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'failed';

export interface ReadyPayload {
  v: number;
  session: string;
  device: { id: string; name: string };
  avatar_url: string;
  capabilities: { text: boolean; voice: boolean; typing: boolean; avatar_states?: AvatarState[] };
}

export interface AgentMessage {
  id: string;
  text: string;
  index: number;
  total: number;
  final: boolean;
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
  onTyping?: () => void;
  onAvatar?: (state: AvatarState) => void;
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
    const id = messageId ?? makeId();
    this.send({ t: 'text', id, text });
    return id;
  }

  /** Голосовое сообщение: WAV в base64 (+ необязательный распознанный текст). */
  sendAudio(base64: string, format: 'wav' | 'ogg', transcript?: string, messageId?: string): string {
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

  // ── внутреннее ────────────────────────────────────────────────────────────

  private open(): void {
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');
    const separator = this.options.url.includes('?') ? '&' : '?';
    const url = `${this.options.url}${separator}token=${encodeURIComponent(this.options.token)}`;
    const socket = new WebSocket(url);
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
        this.handlers.onTyping?.();
        return;
      case 'message': {
        const chunk = (frame.chunk ?? {}) as { index?: number; total?: number };
        this.handlers.onMessage?.({
          id: String(frame.id ?? ''),
          text: String(frame.text ?? ''),
          index: Number(chunk.index ?? 0),
          total: Number(chunk.total ?? 1),
          final: Boolean(frame.final),
        });
        return;
      }
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
