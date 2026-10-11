/**
 * Пример для React Native / Expo: диалог с Hermes Agent + аватар в WebView.
 *
 * Показывает весь путь: клиент протокола → состояние → UI, и как связать состояние аватара
 * (`idle`/`thinking`/`speaking`) со страницей аватара. Скопируйте в свой проект и подставьте
 * свои адрес/токен (в примере — из констант, в продакшене — из настроек/секретов).
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FlatList, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { WebView } from 'react-native-webview';

import { HermesClient, type AgentMessage, type AvatarState } from '../../sdk/typescript/hermesClient';
import { AVATAR_HTML } from './avatarHtml';

const WS_URL = 'wss://agent.example.com/ws';   // адрес адаптера Hermes
const TOKEN = '<MOBILE_TOKEN>';                // токен от владельца агента
const DEVICE = { id: 'my-app-1', name: 'Телефон', platform: 'android', appVersion: '1.0.0' };

interface Item { id: string; role: 'user' | 'agent'; text: string }

export function HermesChat(): React.JSX.Element {
  const webViewRef = useRef<WebView | null>(null);
  const clientRef = useRef<HermesClient | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [avatarState, setAvatarState] = useState<AvatarState>('idle');
  const [draft, setDraft] = useState('');
  const [connection, setConnection] = useState('idle');

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
        onMessage: (message: AgentMessage) =>
          setItems((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.id === message.id) {
              return [...prev.slice(0, -1), { ...last, text: last.text + message.text }];
            }
            return [...prev, { id: message.id, role: 'agent', text: message.text }];
          }),
        onError: (error) => console.warn(`[hermes] ${error.code}: ${error.message}`),
      },
    });
    clientRef.current = client;
    client.connect();
    return () => client.disconnect();
  }, []);

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
        renderItem={({ item }) => (
          <View style={[styles.bubble, item.role === 'user' ? styles.me : styles.agent]}>
            <Text style={styles.bubbleText}>{item.text}</Text>
          </View>
        )}
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
  row: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  input: { flex: 1, color: '#f2f4f8', backgroundColor: '#14141b', borderRadius: 10, paddingHorizontal: 12, paddingVertical: 10 },
  button: { width: 44, height: 44, borderRadius: 22, backgroundColor: '#5b8def', alignItems: 'center', justifyContent: 'center' },
  buttonLabel: { color: '#fff', fontSize: 22 },
});
