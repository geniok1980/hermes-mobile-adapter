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
 * Спецификация: docs/protocol.md
 */
export declare const PROTOCOL_VERSION = 1;
/** Размер куска при отправке больших файлов (protocol.MEDIA_CHUNK_BYTES). */
export declare const MEDIA_CHUNK_BYTES: number;
export type AvatarState = 'idle' | 'thinking' | 'speaking';
export type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'failed';
export interface ReadyPayload {
    v: number;
    session: string;
    device: {
        id: string;
        name: string;
    };
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
    user?: {
        id?: string;
        name?: string;
    };
    handlers?: HermesClientHandlers;
    /** Максимальная пауза между попытками переподключения, мс. */
    maxBackoffMs?: number;
}
/** Источник медиа: base64-строка, готовые байты или путь к файлу (в Node читается с диска). */
export type MediaSource = string | Uint8Array | {
    path: string;
};
export interface MediaSendOptions {
    mime?: string;
    filename?: string;
    caption?: string;
    messageId?: string;
    /** Размер куска в байтах (по умолчанию 3 МБ). */
    chunkBytes?: number;
}
export declare class HermesClient {
    private socket;
    private readonly options;
    private handlers;
    private attempt;
    private closedByUser;
    private reconnectTimer;
    private state;
    /** Сборка многочастных медиа от агента: id → части. */
    private mediaBuffers;
    constructor(options: ClientOptions);
    setHandlers(handlers: HermesClientHandlers): void;
    get ready(): boolean;
    connect(): void;
    disconnect(): void;
    /** Текстовое сообщение агенту. `id` обеспечивает идемпотентность на стороне Hermes. */
    sendText(text: string, messageId?: string): string;
    /** Голосовое сообщение: WAV/OGG в base64 (+ необязательный распознанный текст). */
    sendAudio(base64: string, format: 'wav' | 'ogg', transcript?: string, messageId?: string): string;
    sendTyping(): void;
    acknowledge(messageId: string): void;
    ping(): void;
    /**
     * Ответ на карточку-вопрос.
     *
     * exec_approval — `once|session|always|deny`; slash_confirm — `once|always|cancel`;
     * clarify — `c0..cN` либо `other` вместе с текстом. Незнакомое значение адаптер
     * заменит на безопасное (`deny`/`cancel`).
     */
    answerPrompt(promptId: string, optionId?: string, text?: string): void;
    /**
     * Отправляет медиа агенту, автоматически нарезая файл по `chunkBytes` (3 МБ).
     *
     * `source` — base64, байты или `{ path }` (в Node читается с диска). Куски идут
     * кадрами `media` с общим `id` и полями `part`/`parts`; адаптер собирает их в файл,
     * поэтому большие файлы не упираются в лимит одного кадра.
     */
    sendMedia(kind: string, source: MediaSource, options?: MediaSendOptions): string;
    /** Фото: base64, байты или путь к файлу. */
    sendImage(source: MediaSource, options?: MediaSendOptions): string;
    /** Файл (PDF, zip…): base64, байты или путь к файлу. */
    sendDocument(source: MediaSource, options?: MediaSendOptions): string;
    /** Голосовое: base64, байты или путь к файлу (kind=audio). */
    sendVoice(source: MediaSource, options?: MediaSendOptions): string;
    private open;
    private handleFrame;
    /**
     * Собирает многочастные медиа в целый файл. Наверх отдаём только готовый файл —
     * иначе потребитель получил бы обрывок картинки.
     */
    private accumulateMedia;
    private send;
    private scheduleReconnect;
    private clearReconnect;
    private setState;
}
/** Простой идентификатор сообщения (достаточно для идемпотентности в пределах сессии). */
export declare function makeId(): string;
/** Кодирование байтов в base64 без зависимостей (portable между браузером и Node). */
export declare function bytesToBase64(bytes: Uint8Array): string;
/** Декодирование base64 в байты — своя реализация, чтобы не зависеть от atob/Buffer. */
export declare function base64ToBytes(input: string): Uint8Array;
