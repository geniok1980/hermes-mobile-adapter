# Flutter (Dart)

Полный путь для Dart/Flutter через `web_socket_channel`: подключение, разбор кадров по полю
`t`, отправка текста и медиа кусками, карточки с кнопками, сохранение файлов через
`path_provider`, перерисовка при `edit`. Код готов к копированию в проект.

## 0. Зависимости

```yaml
# pubspec.yaml
dependencies:
  web_socket_channel: ^3.0.0
  path_provider: ^2.1.4
  file_picker: ^8.1.2          # отправка файлов
  record: ^5.1.2               # запись голоса (WAV)
  permission_handler: ^11.3.1  # микрофон/файлы
  http: ^1.2.2                 # скачивание медиа по ссылке url
```

```bash
flutter pub get
```

## 1. Модель кадра и клиент

Клиент дёргает колбэки, состояние держит приложение. Разбор — строго по полю `t`;
неизвестный кадр игнорируем, соединение не рвём. Поля читаем через `?`-safe доступ, потому что
часть из них необязательна (`edit`, `stop`, `caption`, `url`, `reply_to`).

```dart
// lib/hermes/hermes_client.dart
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as ws_status;

typedef Json = Map<String, dynamic>;

class HermesHandlers {
  final void Function(String state)? onState;
  final void Function(Json ready)? onReady;
  final void Function(String id, String text, bool finalChunk)? onMessage;
  final void Function(String id, String text)? onMessageEdit; // стриминг
  final void Function()? onTyping;
  final void Function()? onTypingStop;
  final void Function(String state)? onAvatar;
  final void Function(Json prompt)? onPrompt;
  final void Function(String id, String? optionId, bool expired)? onPromptDone;
  final void Function(Json media)? onMedia;      // целый файл (куски собраны)
  final void Function(String code, String message, bool fatal)? onError;

  const HermesHandlers({
    this.onState, this.onReady, this.onMessage, this.onMessageEdit, this.onTyping,
    this.onTypingStop, this.onAvatar, this.onPrompt, this.onPromptDone, this.onMedia, this.onError,
  });
}

const kMediaChunkBytes = 3 * 1024 * 1024;

class HermesClient {
  final String url;
  final String token;
  final Json device;
  final HermesHandlers handlers;

  WebSocketChannel? _channel;
  Timer? _reconnect;
  int _attempt = 0;
  bool _closedByUser = false;

  // Сборка многочастных медиа: id → части.
  final Map<String, List<Uint8List?>> _buffers = {};
  final Map<String, Json> _meta = {};

  HermesClient({required this.url, required this.token, required this.device, required this.handlers});

  void connect() {
    _closedByUser = false;
    _open();
  }

  void disconnect() {
    _closedByUser = true;
    _reconnect?.cancel();
    _channel?.sink.close(ws_status.normalClosure);
    _channel = null;
    handlers.onState?.call('idle');
  }

  void _open() {
    handlers.onState?.call(_attempt == 0 ? 'connecting' : 'reconnecting');
    final uri = Uri.parse(url).replace(queryParameters: {...Uri.parse(url).queryParameters, 'token': token});
    final channel = WebSocketChannel.connect(uri);
    _channel = channel;

    channel.ready.then((_) {
      channel.sink.add(jsonEncode({'t': 'hello', 'v': 1, 'token': token, 'device': device, 'user': {}}));
    }).catchError((Object e) {
      handlers.onError?.call('transport', 'Не удалось подключиться: $e', false);
    });

    channel.stream.listen(_handle, onError: (Object e) {
      handlers.onError?.call('transport', 'Соединение прервано', false);
    }, onDone: () {
      _channel = null;
      if (_closedByUser) return;
      if (channel.closeCode == 1008) {
        // Отказ в доступе: повторять бессмысленно.
        handlers.onState?.call('failed');
        handlers.onError?.call('auth_failed', 'Шлюз отказал в доступе: проверьте токен', true);
        return;
      }
      _scheduleReconnect();
    }, cancelOnError: true);
  }

  void _scheduleReconnect() {
    _attempt += 1;
    final delayMs = (500 * (1 << (_attempt > 5 ? 5 : _attempt))).clamp(500, 15000);
    handlers.onState?.call('reconnecting');
    _reconnect?.cancel();
    _reconnect = Timer(Duration(milliseconds: delayMs), _open);
  }

  void _send(Json frame) {
    final channel = _channel;
    if (channel == null) { handlers.onError?.call('offline', 'Нет связи со шлюзом', false); return; }
    channel.sink.add(jsonEncode(frame));
  }

  // ── клиент → сервер ────────────────────────────────────────────────────────

  String sendText(String text, {String? messageId}) {
    final id = messageId ?? _makeId();
    _send({'t': 'text', 'id': id, 'text': text});
    return id;
  }

  String sendAudio(Uint8List wav, {String format = 'wav', String? transcript, String? messageId}) {
    final id = messageId ?? _makeId();
    _send({'t': 'audio', 'id': id, 'format': format, 'data': base64Encode(wav), if (transcript != null) 'text': transcript});
    return id;
  }

  /// Ответ на карточку. option_id: exec_approval once|session|always|deny,
  /// slash_confirm once|always|cancel, clarify c0..cN или other (+text).
  void answerPrompt(String promptId, {String? optionId, String? text}) {
    if (optionId == null && (text == null || text.trim().isEmpty)) {
      throw ArgumentError('answerPrompt: нужен optionId или text');
    }
    _send({'t': 'prompt', 'prompt_id': promptId,
      if (optionId != null) 'option_id': optionId.toLowerCase(),
      if (text != null) 'text': text});
  }

  void acknowledge(String messageId) => _send({'t': 'ack', 'id': messageId});
  void ping() => _send({'t': 'ping', 'ts': DateTime.now().millisecondsSinceEpoch});

  /// Отправка медиа кусками. kind: image | video | document | audio.
  String sendMedia(String kind, Uint8List bytes, {String? mime, String? filename, String? caption, String? messageId}) {
    const allowed = {'image', 'video', 'document', 'audio'};
    if (!allowed.contains(kind)) throw ArgumentError('kind: только $allowed');
    if (bytes.isEmpty) throw ArgumentError('sendMedia: пустой файл');
    final id = messageId ?? _makeId();
    final parts = (bytes.length / kMediaChunkBytes).ceil().clamp(1, 1 << 30);
    for (var i = 0; i < parts; i++) {
      final end = ((i + 1) * kMediaChunkBytes) < bytes.length ? (i + 1) * kMediaChunkBytes : bytes.length;
      final slice = bytes.sublist(i * kMediaChunkBytes, end);
      _send({'t': 'media', 'id': id, 'kind': kind, if (mime != null) 'mime': mime,
        if (filename != null) 'filename': filename, if (caption != null) 'caption': caption,
        'data': base64Encode(slice), 'part': i, 'parts': parts});
    }
    return id;
  }

  // ── сервер → клиент ────────────────────────────────────────────────────────

  void _handle(dynamic raw) {
    Json f;
    try { f = jsonDecode(raw as String) as Json; } catch (_) { return; } // мусор игнорируем
    switch (f['t']) {
      case 'ready':
        _attempt = 0;
        handlers.onState?.call('connected');
        handlers.onReady?.call(f);
        break;
      case 'typing':
        (f['stop'] == true) ? handlers.onTypingStop?.call() : handlers.onTyping?.call();
        break;
      case 'message':
        final chunk = (f['chunk'] as Json?) ?? const {};
        final id = '${f['id'] ?? ''}';
        final text = '${f['text'] ?? ''}';
        final isFinal = f['final'] == true;
        (f['edit'] == true) ? handlers.onMessageEdit?.call(id, text)
                            : handlers.onMessage?.call(id, text, isFinal);
        break;
      case 'prompt':
        handlers.onPrompt?.call(f);
        break;
      case 'prompt_done':
        handlers.onPromptDone?.call('${f['id'] ?? ''}',
          f['option_id'] == null ? null : '${f['option_id']}',
          f['expired'] == true);
        break;
      case 'media':
        final complete = _accumulate(f);
        if (complete != null) handlers.onMedia?.call(complete);
        break;
      case 'avatar':
        handlers.onAvatar?.call('${f['state'] ?? 'idle'}');
        break;
      case 'error':
        handlers.onError?.call('${f['code'] ?? 'error'}', '${f['message'] ?? ''}', f['fatal'] == true);
        break;
      default:
        break; // неизвестный кадр игнорируем — так требует протокол
    }
  }

  Json? _accumulate(Json f) {
    final id = '${f['id'] ?? ''}';
    final parts = (f['parts'] as num?)?.toInt() ?? 1;
    final part = (f['part'] as num?)?.toInt() ?? 0;
    final chunk = f['data'] == null ? Uint8List(0) : base64Decode('${f['data']}');
    _meta[id] = f; // последний кадр несёт url/caption/reply_to
    if (parts <= 1) return {...f, 'data': base64Encode(chunk)};
    final list = _buffers.putIfAbsent(id, () => List<Uint8List?>.filled(parts, null));
    list[part] = chunk;
    if (list.any((e) => e == null)) return null;
    _buffers.remove(id);
    final builder = BytesBuilder();
    for (final e in list) { builder.add(e!); }
    return {..._meta[id]!, 'data': base64Encode(builder.takeBytes())};
  }

  String _makeId() => '${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}-${_rand()}';
  static String _rand() => DateTime.now().microsecond.toRadixString(36);
}
```

## 2. Экран чата

```dart
// lib/screens/chat_screen.dart
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';
import 'package:http/http.dart' as http;
import '../hermes/hermes_client.dart';

class ChatItem {
  final String id; final String role; String text;
  final String? kind; final List<dynamic>? options; String? resolved;
  final String? mediaKind; final String? filePath; final String? caption;
  ChatItem(this.id, this.role, this.text, {this.kind, this.options, this.resolved,
    this.mediaKind, this.filePath, this.caption});
}

class ChatScreen extends StatefulWidget { const ChatScreen({super.key}); @override State<ChatScreen> createState() => _ChatScreenState(); }

class _ChatScreenState extends State<ChatScreen> {
  final _items = <ChatItem>[];
  final _draft = TextEditingController();
  late final HermesClient _client;
  String _connection = 'idle';

  @override
  void initState() {
    super.initState();
    _client = HermesClient(
      url: 'wss://hermes-go.com/mobile-ws',
      token: const String.fromEnvironment('MOBILE_TOKEN'), // -d MOBILE_TOKEN=... или из secure storage
      device: const {'id': 'flutter-1', 'name': 'Телефон', 'platform': 'android', 'app_version': '1.0.0'},
      handlers: HermesHandlers(
        onState: (s) => setState(() => _connection = s),
        onTyping: () => setState(() {}),
        onTypingStop: () => setState(() {}),
        onMessage: (id, text, _) => setState(() {
          final i = _items.indexWhere((it) => it.id == id);
          if (i == -1) { _items.add(ChatItem(id, 'agent', text)); } else { _items[i].text += text; }
        }),
        onMessageEdit: (id, text) => setState(() {
          final i = _items.indexWhere((it) => it.id == id);
          if (i == -1) { _items.add(ChatItem(id, 'agent', text)); } else { _items[i].text = text; }
        }),
        onPrompt: (p) => setState(() => _items.add(ChatItem('card-${p['id']}', 'card', '${p['text']}',
          kind: '${p['kind']}', options: p['options'] as List?))),
        onPromptDone: (id, optionId, expired) => setState(() {
          final i = _items.indexWhere((it) => it.id == 'card-$id');
          if (i != -1) _items[i].resolved = expired ? 'expired' : (optionId ?? 'закрыта');
        }),
        onMedia: (m) => _saveMedia(m),
        onError: (code, message, _) => debugPrint('[hermes] $code: $message'),
      ),
    )..connect();
  }

  @override
  void dispose() { _client.disconnect(); _draft.dispose(); super.dispose(); }

  Future<void> _saveMedia(Map<String, dynamic> m) async {
    final dir = await getApplicationDocumentsDirectory();
    final name = (m['filename'] as String?) ?? '${m['id']}.bin';
    final file = File('${dir.path}/hermes-media/$name');
    await file.parent.create(recursive: true);
    Uint8List? bytes;
    if (m['data'] != null) {
      bytes = base64Decode(m['data'] as String);
    } else if (m['url'] != null) {
      bytes = (await http.get(Uri.parse(m['url'] as String))).bodyBytes; // ссылка вместо файла
    }
    if (bytes != null) await file.writeAsBytes(bytes);
    if (!mounted) return;
    setState(() => _items.add(ChatItem('media-${m['id']}-${DateTime.now().millisecondsSinceEpoch}',
      'media', '', mediaKind: '${m['kind']}', filePath: file.path, caption: (m['caption'] as String?) ?? name)));
  }

  void _send() {
    final text = _draft.text.trim();
    if (text.isEmpty) return;
    _client.sendText(text);
    setState(() => _items.add(ChatItem('u-${DateTime.now().microsecondsSinceEpoch}', 'user', text)));
    _draft.clear();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: Text('Hermes — $_connection')),
    body: Column(children: [
      Expanded(child: ListView.builder(
        itemCount: _items.length,
        itemBuilder: (_, i) {
          final it = _items[i];
          if (it.role == 'card') return _card(it);
          if (it.role == 'media') return ListTile(
            leading: Icon(it.mediaKind == 'image' ? Icons.image : Icons.insert_drive_file),
            title: Text(it.caption ?? 'файл'), subtitle: Text(it.filePath ?? ''));
          return Align(
            alignment: it.role == 'user' ? Alignment.centerRight : Alignment.centerLeft,
            child: Container(margin: const EdgeInsets.all(6), padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(color: it.role == 'user' ? Colors.blue.shade100 : Colors.black12,
                borderRadius: BorderRadius.circular(12)),
              child: Text(it.text)),
          );
        })),
      Padding(padding: const EdgeInsets.all(8), child: Row(children: [
        Expanded(child: TextField(controller: _draft, onSubmitted: (_) => _send())),
        IconButton(icon: const Icon(Icons.send), onPressed: _send),
      ])),
    ]));

  Widget _card(ChatItem it) => Card(child: Padding(padding: const EdgeInsets.all(12), child: Column(
    crossAxisAlignment: CrossAxisAlignment.start, children: [
      Text('${it.kind}', style: const TextStyle(fontSize: 11, color: Colors.grey)),
      const SizedBox(height: 4), Text(it.text), const SizedBox(height: 8),
      Wrap(spacing: 8, children: [
        for (final o in (it.options ?? const [])) ElevatedButton(
          style: (o['style'] == 'danger') ? ElevatedButton.styleFrom(backgroundColor: Colors.red) : null,
          onPressed: it.resolved != null ? null : () => _answer(it, '${o['id']}'),
          child: Text('${o['label'] ?? o['id']}')),
      ]),
      if (it.resolved != null) Padding(padding: const EdgeInsets.only(top: 6),
        child: Text(it.resolved == 'expired' ? 'истекла' : 'выбрано: ${it.resolved}',
          style: const TextStyle(fontSize: 11, color: Colors.grey))),
    ])));

  Future<void> _answer(ChatItem it, String optionId) async {
    final promptId = it.id.replaceFirst('card-', '');
    if (optionId == 'other') {
      final text = await _askText(); // «свой ответ» обязан нести текст
      if (text == null || text.trim().isEmpty) return;
      _client.answerPrompt(promptId, optionId: 'other', text: text);
    } else {
      _client.answerPrompt(promptId, optionId: optionId);
    }
    setState(() => it.resolved = optionId);
  }

  Future<String?> _askText() {
    final c = TextEditingController();
    return showDialog<String>(context: context, builder: (ctx) => AlertDialog(
      title: const Text('Свой ответ'), content: TextField(controller: c, autofocus: true),
      actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Отмена')),
        TextButton(onPressed: () => Navigator.pop(ctx, c.text), child: const Text('Отправить'))]));
  }
}
```

## 3. Отправка файла и голоса

```dart
// файл: file_picker
final result = await FilePicker.platform.pickFiles(withData: true);
final picked = result?.files.single;
if (picked != null && picked.bytes != null) {
  final mime = _mime(picked.extension);
  final kind = mime.startsWith('image/') ? 'image' : 'document';
  _client.sendMedia(kind, picked.bytes!, mime: mime, filename: picked.name);
}

// голос: record → WAV 16 кГц моно, затем sendAudio
import 'package:record/record.dart';
final rec = AudioRecorder();
await rec.start(const RecordConfig(encoder: AudioEncoder.wav, sampleRate: 16000, numChannels: 1),
  path: '${(await getTemporaryDirectory()).path}/voice.wav');
// ... на стоп:
final path = await rec.stop();
if (path != null) _client.sendAudio(await File(path).readAsBytes(), format: 'wav');
```

## 4. Подводные камни

| Симптом | Причина | Решение |
|---|---|---|
| Дубли при стриминге | `edit`-кадры добавлены как новые | перерисовывать по `id` (`onMessageEdit`) |
| Карточка «зависла» | `other` без `text` | дослать текст в кадре `prompt` |
| Файл не собрался | отправлены не все `part` | досылать все куски до `parts-1`, общий `id` |
| `Unhandled exception` на кадре | обязательное поле прочитано жёстко | читать необязательные поля через проверку |
| Медиа пустое | `data` нет, есть `url` | скачать по ссылке (`http.get`) |
| Reconnect по кругу | не проверен closeCode 1008 | при 1008 не переподключаться |
