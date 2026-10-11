/**
 * Пример для React Native / Expo: диалог с Hermes Agent + аватар в WebView.
 *
 * Показывает весь путь: клиент протокола → состояние → UI. Добавлены возможности
 * адаптера: карточки-вопросы (одобрение команды / clarify) с кнопками, приём медиа
 * от агента (сохранение на устройство + показ) и правка уже показанного сообщения
 * (`edit` — потоковая выдача). Скопируйте в свой проект и подставьте свои адрес/токен
 * (в примере — из констант, в продакшене — из настроек/секретов).
 *
 * Дополнительная зависимость для сохранения медиа: `expo-file-system`.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FlatList, Image, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { WebView } from 'react-native-webview';
import * as FileSystem from 'expo-file-system';

import {
  HermesClient,
  type AgentMessage,
  type AvatarState,
  type MediaPayload,
  type PromptDonePayload,
  type PromptPayload,
} from '../../sdk/typescript/hermesClient';
import { AVATAR_HTML } from './avatarHtml';

const WS_URL = 'wss://agent.example.com/ws';   // адрес адаптера Hermes
const TOKEN = '<MOBILE_TOKEN>';                // токен от владельца агента
const DEVICE = { id: 'my-app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' };
const MEDIA_DIR = `${FileSystem.documentDirectory}hermes-media/`;

type ItemRole = 'user' | 'agent' | 'card' | 'media';

interface Item {
  id: string;
  role: ItemRole;
  text: string;
  /** карточка */
  kind?: string;
  options?: PromptPayload['options'];
  resolved?: 'expired' | string;
  /** медиа */
  mediaKind?: string;
  uri?: string;      // data-URL (для показа) или путь на устройстве
  localPath?: string;
  caption?: string;
}

/** Показ медиа: картинка — инлайном, остальное — по ссылке (открывается системно). */
function MediaBubble({ item }: { item: Item }): React.JSX.Element {
  const isImage = item.mediaKind === 'image' || item.mediaKind === 'animation';
  return (
    <View style={[styles.bubble, styles.agent, styles.media]}>
      {isImage && item.uri ? (
        <Image source={{ uri: item.uri }} style={styles.image} resizeMode="contain" />
      ) : (
        <Text style={styles.link}>📄 {item.caption || item.localPath || 'файл'}</Text>
      )}
      {item.localPath ? <Text style={styles.meta}>сохранено: {item.localPath}</Text> : null}
      {item.caption && isImage ? <Text style={styles.meta}>{item.caption}</Text> : null}
    </View>
  );
}

/** Карточка-вопрос с кнопками вариантов. */
function PromptCard({ item, onAnswer }: { item: Item; onAnswer: (item: Item, optionId: string) => void }): React.JSX.Element {
  return (
    <View style={[styles.card, item.resolved ? styles.cardResolved : null]}>
      <Text style={styles.cardKind}>{item.kind}</Text>
      <Text style={styles.cardText}>{item.text}</Text>
      <View style={styles.options}>
        {(item.options ?? []).map((option) => (
          <Pressable
            key={option.id}
            disabled={Boolean(item.resolved)}
            style={[styles.option, option.style === 'danger' ? styles.optionDanger : null]}
            onPress={() => onAnswer(item, option.id)}
          >
            <Text style={styles.optionLabel}>{option.label || option.id}</Text>
          </Pressable>
        ))}
      </View>
      {item.resolved ? <Text style={styles.meta}>{item.resolved === 'expired' ? 'истекла' : `выбрано: ${item.resolved}`}</Text> : null}
    </View>
  );
}

export function HermesChat(): React.JSX.Element {
  const webViewRef = useRef<WebView | null>(null);
  const clientRef = useRef<HermesClient | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [avatarState, setAvatarState] = useState<AvatarState>('idle');
  const [draft, setDraft] = useState('');
  const [connection, setConnection] = useState('idle');

  const upsert = useCallback((id: string, patch: Partial<Item> & { role: ItemRole }) => {
    setItems((prev) => {
      const index = prev.findIndex((item) => item.id === id);
      if (index === -1) return [...prev, { id, text: '', ...patch }];
      const next = [...prev];
      next[index] = { ...next[index], ...patch };
      return next;
    });
  }, []);

  // Медиа от агента: сохраняем на устройство и показываем data-URL для картинок.
  const saveMedia = useCallback(async (media: MediaPayload) => {
    const name = media.filename || `${media.id}.bin`;
    const dataUrl = media.data ? `data:${media.mime || 'application/octet-stream'};base64,${media.data}` : media.url;
    let localPath: string | undefined;
    try {
      await FileSystem.makeDirectoryAsync(MEDIA_DIR, { intermediates: true });
      localPath = `${MEDIA_DIR}${name}`;
      if (media.data) {
        await FileSystem.writeAsStringAsync(localPath, media.data, { encoding: FileSystem.EncodingType.Base64 });
      }
    } catch (error) {
      console.warn('[hermes] не удалось сохранить медиа', error);
    }
    setItems((prev) => [
      ...prev,
      { id: `media-${media.id}-${Date.now()}`, role: 'media', text: '', mediaKind: media.kind, uri: dataUrl, localPath, caption: media.caption || name },
    ]);
  }, []);

  // Аватар принимает состояние через тот же контракт, что и в приложении HermesAI:
  // window.hermesAvatar.setState('idle'|'thinking'|'speaking').
  useEffect(() => {
    webViewRef.current?.injectJavaScript(
      `window.hermesAvatar && window.hermesAvatar.setState('${avatarState}'); true;`,
    );
  }, [avatarState]);

  useEffect(() => {
    const client = new HermesClient({
      url: WS_URL,
      token: TOKEN,
      device: DEVICE,
      handlers: {
        onState: (state) => setConnection(state),
        onAvatar: (state) => setAvatarState(state),
        onTyping: () => setAvatarState('thinking'),
        onTypingStop: () => setAvatarState('idle'),
        onMessage: (message: AgentMessage) =>
          setItems((prev) => {
            const index = prev.findIndex((item) => item.id === message.id);
            if (index !== -1) {
              const next = [...prev];
              next[index] = { ...next[index], text: next[index].text + message.text };
              return next;
            }
            return [...prev, { id: message.id, role: 'agent', text: message.text }];
          }),
        // Правка сообщения (стриминг): заменяем текст пузыря с тем же id.
        onMessageEdit: (message: AgentMessage) =>
          upsert(message.id, { role: 'agent', text: message.text }),
        onPrompt: (prompt: PromptPayload) =>
          setItems((prev) => [
            ...prev,
            { id: `card-${prompt.id}`, role: 'card', text: prompt.text, kind: prompt.kind, options: prompt.options },
          ]),
        onPromptDone: (done: PromptDonePayload) =>
          upsert(`card-${done.id}`, { role: 'card', resolved: done.expired ? 'expired' : done.option_id ?? 'закрыта' }),
        onMedia: (media: MediaPayload) => {
          void saveMedia(media);
        },
        onError: (error) => console.warn(`[hermes] ${error.code}: ${error.message}`),
      },
    });
    clientRef.current = client;
    client.connect();
    return () => client.disconnect();
  }, [saveMedia, upsert]);

  const answer = useCallback((item: Item, optionId: string) => {
    // promptId живёт в id карточки: card-<promptId>.
    const promptId = item.id.replace(/^card-/, '');
    clientRef.current?.answerPrompt(promptId, optionId);
    upsert(item.id, { role: 'card', text: item.text, resolved: optionId });
  }, [upsert]);

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text || !clientRef.current) return;
    clientRef.current.sendText(text);
    setItems((prev) => [...prev, { id: `u-${Date.now()}`, role: 'user', text }]);
    setDraft('');
  }, [draft]);

  const avatarSource = useMemo(() => ({ html: AVATAR_HTML }), []);

  return (
    <View style={styles.root}>
      <WebView ref={webViewRef} source={avatarSource} style={styles.avatar} scrollEnabled={false} javaScriptEnabled />
      <Text style={styles.status}>{connection === 'connected' ? 'агент на связи' : `связь: ${connection}`}</Text>

      <FlatList
        style={styles.list}
        data={items}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => {
          if (item.role === 'card') return <PromptCard item={item} onAnswer={answer} />;
          if (item.role === 'media') return <MediaBubble item={item} />;
          return (
            <View style={[styles.bubble, item.role === 'user' ? styles.me : styles.agent]}>
              <Text style={styles.bubbleText}>{item.text}</Text>
            </View>
          );
        }}
      />

      <View style={styles.row}>
        <TextInput style={styles.input} value={draft} onChangeText={setDraft} placeholder="Сообщение…" onSubmitEditing={send} />
        <Pressable style={styles.button} onPress={send}>
          <Text style={styles.buttonLabel}>→</Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#0b0b0f', padding: 12 },
  avatar: { height: 200, backgroundColor: 'transparent' },
  status: { color: '#9ba3b4', fontSize: 12, textAlign: 'center', marginBottom: 8 },
  list: { flex: 1 },
  bubble: { padding: 10, borderRadius: 12, marginBottom: 8, maxWidth: '90%' },
  me: { alignSelf: 'flex-end', backgroundColor: 'rgba(91,141,239,.16)' },
  agent: { alignSelf: 'flex-start', backgroundColor: '#1c1c25' },
  bubbleText: { color: '#f2f4f8', fontSize: 16 },
  media: { paddingVertical: 8 },
  image: { width: 240, height: 180, borderRadius: 10 },
  link: { color: '#5b8def', fontSize: 15 },
  meta: { color: '#9ba3b4', fontSize: 11, marginTop: 4 },
  card: { alignSelf: 'flex-start', backgroundColor: '#161620', borderColor: '#5b8def', borderWidth: 1, borderRadius: 12, padding: 12, marginBottom: 8, maxWidth: '92%' },
  cardResolved: { opacity: 0.55, borderColor: '#262633' },
  cardKind: { color: '#9ba3b4', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.6, marginBottom: 4 },
  cardText: { color: '#f2f4f8', fontSize: 15, marginBottom: 10 },
  options: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  option: { backgroundColor: '#5b8def', borderRadius: 10, paddingHorizontal: 14, paddingVertical: 8 },
  optionDanger: { backgroundColor: '#e5484d' },
  optionLabel: { color: '#fff', fontSize: 14, fontWeight: '600' },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  input: { flex: 1, color: '#f2f4f8', backgroundColor: '#14141b', borderRadius: 10, paddingHorizontal: 12, paddingVertical: 10 },
  button: { width: 44, height: 44, borderRadius: 22, backgroundColor: '#5b8def', alignItems: 'center', justifyContent: 'center' },
  buttonLabel: { color: '#fff', fontSize: 22 },
});
