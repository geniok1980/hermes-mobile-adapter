# Рецепты

Двенадцать сценариев «как сделать X». Код короткий и рабочий; платформо-независимые кадры —
это один и тот же JSON в любом рантайме. Для каждого — подводный камень.

## 1. Отправить фото с подписью

```js
// Клиент любой: собираем кадр media одним куском (файл ≤ 8 МБ → parts: 1).
send({
  t: 'media', id: 'm-photo-' + Date.now(), kind: 'image',
  mime: 'image/jpeg', filename: 'photo.jpg', caption: 'Смотри на график',
  data: base64Bytes, part: 0, parts: 1,
});
```

**Подводный камень:** `caption` — это подпись, а не отдельное сообщение. Не отправляйте рядом
`text` с тем же смыслом — агент увидит и подпись, и текст как разные сообщения. Для `kind: "audio"`
`caption` работает как распознанный текст.

## 2. Отправить документ кусками

```js
// Файл > 8 МБ не влезает в один кадр: режем по ~3 МБ, общий id, досылаем ВСЕ куски.
const id = 'm-doc-' + Date.now();
const CHUNK = 3 * 1024 * 1024;
const parts = Math.max(1, Math.ceil(bytes.length / CHUNK));
for (let i = 0; i < parts; i++) {
  const slice = bytes.slice(i * CHUNK, (i + 1) * CHUNK);
  send({ t: 'media', id, kind: 'document', mime: 'application/pdf', filename: 'отчёт.pdf',
         data: toBase64(slice), part: i, parts });
}
```

**Подводный камень:** адаптер собирает файл только когда пришли **все** куски до `parts-1`.
Оборвёте серию — файл агенту не уйдёт вовсе (`too_big`/тишина). `id` у всех кусков — один и тот же.

## 3. Принять файл от агента и сохранить

```ts
// React Native / Expo
onMedia: async (m) => {
  const name = m.filename || `${m.id}.bin`;
  const path = `${FileSystem.documentDirectory}hermes-media/${name}`;
  await FileSystem.makeDirectoryAsync(`${FileSystem.documentDirectory}hermes-media/`, { intermediates: true });
  if (m.data) await FileSystem.writeAsStringAsync(path, m.data, { encoding: FileSystem.EncodingType.Base64 });
  else await FileSystem.downloadAsync(m.url!, path);   // data нет — качаем по ссылке
}
```

**Подводный камень:** SDK уже собрал куски (`part`/`parts`) — колбэк вызывается **один раз на
целый файл**, вручную склеивать не нужно. Но если `data` пуст, у кадра есть только `url`
(файл на диске недоступен) — скачивайте сами.

## 4. Показать карточку одобрения и ответить

```js
case 'prompt': {
  // options: [{id:'once',label:'Одобрить один раз',style:'primary'}, {id:'deny',label:'Отклонить',style:'danger'}, ...]
  // для exec_approval: покажите data.command рядом с кнопками (data.description — пояснение)
  showCard(data.id, data.text, data.command, data.description, data.options);
  break;
}
// при нажатии кнопки:
send({ t: 'prompt', prompt_id: promptId, option_id: 'once' });
```

**Подводный камень:** ответ на карточку **не становится сообщением в чате** — не дублируйте
выбор отдельным `text`. Значения строго по типу: `exec_approval` → `once|session|always|deny`;
`slash_confirm` → `once|always|cancel`. Незнакомое сервер заменит на безопасное (`deny`/`cancel`),
но и полагаться на это не стоит — шлите корректное. Карточку закрывает **только то устройство,
которому она показана** (ответ с другого соединения → `forbidden`).

## 5. Обработать ответ на clarify текстом (`option id: other`)

```js
// kind: 'clarify', options заканчиваются вариантом {id:'other', label:'✏️ Свой ответ'}
if (option.id === 'other') {
  const text = await promptUserForText();      // ваш ввод
  if (!text.trim()) return;
  send({ t: 'prompt', prompt_id: data.id, option_id: 'other', text });   // text ОБЯЗАТЕЛЕН
} else {
  send({ t: 'prompt', prompt_id: data.id, option_id: option.id });       // c0, c1, …
}
```

**Подводный камень:** `option_id: "other"` **без** непустого `text` переводит вопрос в режим
ожидания текста — карточка «зависнет». Альтернатива: `{t:'prompt', prompt_id, text}` без
`option_id` вообще.

## 6. Перерисовать сообщение при стриминге

```js
case 'message': {
  const node = document.querySelector(`[data-msg="${CSS.escape(data.id)}"]`);
  if (data.edit) {                       // правка того же сообщения
    if (node) node.textContent = data.text;
    else { const n = log(data.text); n.dataset.msg = data.id; }
  } else if (node) {                      // длинный ответ — куски одного id
    node.textContent += data.text;
  } else {
    const n = log(data.text); n.dataset.msg = data.id;
  }
  break;
}
```

**Подводный камень:** если игнорировать `edit`, каждый стриминг-кадр покажется как **новое**
сообщение — будут дубли растущего текста. Перерисовывайте пузырь по `id`. `final: true` в
последней правке — конец.

## 7. Записать и отправить голосовое

```ts
// WAV 16 кГц, моно, PCM 16 бит → base64 в кадре audio.
client.sendAudio(wavBase64, 'wav');                 // format только 'wav' или 'ogg'
client.sendAudio(wavBase64, 'wav', 'своё распознавание');  // + необязательный текст
```

**Подводный камень:** формат строго `wav`/`ogg` — `mp3`/`aac`/`m4a` сервер отвергнет с
`unsupported_frame`. Аудио ≤ 8 МБ (несколько минут); длинную запись бейте на несколько `audio`-кадров.
`media` с `kind:"audio"` — это тоже допустимо вверх, но для «голосового» канонический кадр — `audio`.

## 8. Восстановить связь и не продублировать сообщение

```ts
// id сообщения генерируйте один раз и ПОВТОРЯЙТЕ его при переотправке после обрыва.
const id = makeId();            // сохраните рядом с черновиком/очередью
client.sendText(text, id);
// после reconnect — тот же вызов с тем же id:
client.sendText(text, id);      // адаптер игнорирует повтор с тем же id
```

**Подводный камень:** идемпотентность держится на `id`. Сгенерируете новый `id` при повторной
отправке — агент получит **два** одинаковых сообщения. `HermesClient` генерирует `id` сам, но при
ручной переотправке передавайте прежний (`sendText(text, messageId)`).

## 9. Принять сообщение от cron (`deliver`)

```js
// Кадр сервер→клиент приходит как обычное message, но может не иметь предшествующего text.
case 'message':
  // Различать источник не нужно: показывайте как ответ агента.
  // id у cron-сообщений свой; edit для них не применяется.
  upsert(data.id, { role: 'agent', text: data.text });
  break;
```

**Подводный камень:** cron-доставка требует **живого** соединения. Устройство офлайн — на
машине агента вернётся `device_offline`. На клиенте ничего особенного не делайте: то же
`message`. Кадр `delivered` (сервер→клиент `{t,id,device}`) — служебное подтверждение
standalone-доставки, показывать его пользователю не нужно. Кадр `deliver` — служебный и
принимается только с loopback-адреса (cron на той же машине) или с токеном из
`MOBILE_DELIVER_TOKENS`; обычное приложение его слать не должно — иначе получит `forbidden`.

## 10. Не сломаться на неизвестном кадре

```js
switch (data.t) {
  case 'ready': /* … */ break;
  // … известные типы …
  default:
    // Новая версия протокола прислала незнакомый кадр — молча игнорируем.
    console.debug('[hermes] неизвестный кадр', data.t);
    break;
}
```

**Подводный камень:** нельзя прерывать соединение или кидать исключение на неизвестный `t`.
Протокол расширяется за счёт новых **необязательных** полей и новых типов кадров — клиент,
который падает на незнакомом кадре, ломается при первом же обновлении шлюза. То же и с полями:
не считайте `edit`, `stop`, `caption`, `url`, `reply_to` обязательными.

## 11. Подтвердить показ сообщения (`ack`)

```js
// Показали/проиграли сообщение — можно подтвердить:
send({ t: 'ack', id: data.id });
```

**Подводный камень:** `ack` не заменяет `message.final` — это разные вещи. `final` — про
склейку длинного ответа; `ack` — про то, что приложение показало/проиграло. Отправляйте `ack`
только если реально показываете (для аналитики/доставки), не превращайте в обязательный шаг.

## 12. Пинг для проверки связи

```js
send({ t: 'ping', ts: Date.now() });   // сервер ответит {t:'pong', ts}
```

**Подводный камень:** `ts` сервер возвращает как есть — используйте его для измерения RTT.
Не спамьте `ping`: на мобильных сетях держите интервал ≥ 30 с; при невозможности получить
`pong` за таймаут считайте соединение мёртвым и переподключайтесь.
