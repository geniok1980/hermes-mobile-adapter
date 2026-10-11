# TypeScript SDK

Клиент протокола Hermes Mobile Adapter. Без зависимостей: используется стандартный
`WebSocket`, поэтому работает в React Native/Expo, в браузере, в Electron и в Node 22+.

```ts
import { HermesClient } from './hermesClient';

const client = new HermesClient({
  url: 'wss://agent.example.com/ws',
  token: process.env.MOBILE_TOKEN!,
  device: { id: 'app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' },
  handlers: {
    onReady: (ready) => console.log('сессия', ready.session),
    onMessage: (m) => append(m.text, m.final),
    onAvatar: (state) => avatar.setState(state),   // idle | thinking | speaking
    onTyping: () => showTyping(),
    onError: (e) => console.warn(e.code, e.message),
    onState: (state) => console.log('связь:', state),
  },
  maxBackoffMs: 15000,
});

client.connect();
client.sendText('привет');
// client.sendAudio(base64wav, 'wav', 'распознанный текст');
```

Что уже внутри: авторизация токеном, переподключение с экспоненциальной паузой, отдельная
обработка отказа в доступе (код 1008 — повторять бессмысленно), сборка длинных ответов из
кадров `message` (каждый кадр — часть, `chunk.index/total` и `final`), идемпотентные `id`
для повторной отправки после обрыва, игнорирование неизвестных кадров (требование
протокола).

Типы: `ReadyPayload`, `AgentMessage`, `ProtocolError`, `AvatarState`, `ConnectionState`,
`HermesClientHandlers`.

Работа с историей и UI — на стороне приложения: SDK не хранит состояние, только транспорт.
Пример со всем этим сразу — `examples/react-native/`.
