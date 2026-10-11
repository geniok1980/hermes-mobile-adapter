# Android (Kotlin, OkHttp)

Полный путь на `OkHttp WebSocket`: подключение, отправка текста и медиа, карточки через
`AlertDialog`, приём медиа, потоковые правки и обвязка жизненного цикла (переподключение при
возврате из фона). Зависимости: `com.squareup.okhttp3:okhttp`.

```kotlin
// build.gradle.kts
dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
```

## 1. Клиент протокола

Токен храните в `EncryptedSharedPreferences`/Keystore, не в исходниках. `device.id` держите
постоянным.

```kotlin
// HermesClient.kt
import android.util.Base64
import okhttp3.*
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

sealed class ConnectionState {
    object Idle : ConnectionState()
    object Connecting : ConnectionState()
    object Connected : ConnectionState()
    object Reconnecting : ConnectionState()
    object Failed : ConnectionState()
}

interface HermesListener {
    fun onState(state: ConnectionState) {}
    fun onReady(ready: JSONObject) {}
    fun onMessage(id: String, text: String, isFinal: Boolean) {}
    fun onMessageEdit(id: String, text: String) {}        // стриминг
    fun onTyping(active: Boolean) {}
    fun onAvatar(state: String) {}
    fun onPrompt(prompt: JSONObject) {}
    fun onPromptDone(id: String, optionId: String?, expired: Boolean) {}
    fun onMediaComplete(media: JSONObject) {}             // целый файл (куски собраны)
    fun onError(code: String, message: String, fatal: Boolean) {}
}

class HermesClient(
    private val url: String,
    private val token: String,
    private val device: JSONObject,
    private var listener: HermesListener? = null,
) {
    private val http = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)   // keepalive: OkHttp шлёт WS-ping сам
        .build()

    private var ws: WebSocket? = null
    private var attempt = 0
    private var closedByUser = false
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())

    private val mediaParts = HashMap<String, HashMap<Int, ByteArray>>()
    private val mediaMeta = HashMap<String, JSONObject>()

    companion object {
        const val MEDIA_CHUNK_BYTES = 3 * 1024 * 1024
        const val CLOSE_UNAUTHORIZED = 1008
    }

    fun setListener(l: HermesListener) { listener = l }

    fun connect() {
        closedByUser = false
        open()
    }

    fun disconnect() {
        closedByUser = true
        handler.removeCallbacksAndMessages(null)
        ws?.close(1000, null)
        ws = null
        listener?.onState(ConnectionState.Idle)
    }

    /** Вызывайте из onResume: если сокет отвалился в фоне — поднять снова. */
    fun ensureConnected() {
        if (ws == null && !closedByUser) open()
    }

    private fun open() {
        listener?.onState(if (attempt == 0) ConnectionState.Connecting else ConnectionState.Reconnecting)
        val sep = if (url.contains("?")) "&" else "?"
        val request = Request.Builder().url("$url$sep$tokenKey=$token").build()
        ws = http.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send(JSONObject(mapOf(
                    "t" to "hello", "v" to 1, "token" to token,
                    "device" to device, "user" to JSONObject()
                )).toString())
            }
            override fun onMessage(webSocket: WebSocket, text: String) = handleFrame(text)
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                ws = null
                if (closedByUser) return
                listener?.onError("transport", t.message ?: "Соединение прервано", false)
                scheduleReconnect()
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                ws = null
                if (closedByUser) return
                if (code == CLOSE_UNAUTHORIZED) {
                    // Отказ в доступе: повторять бессмысленно.
                    listener?.onState(ConnectionState.Failed)
                    listener?.onError("auth_failed", "Нет доступа: проверьте токен", true)
                    return
                }
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        attempt += 1
        listener?.onState(ConnectionState.Reconnecting)
        val delay = minOf(500L * (1L shl minOf(attempt, 5)), 15000L)
        handler.postDelayed({ if (!closedByUser) open() }, delay)
    }

    private val tokenKey get() = "token"

    private fun send(frame: JSONObject) {
        val socket = ws
        if (socket == null) { listener?.onError("offline", "Нет связи со шлюзом", false); return }
        socket.send(frame.toString())
    }

    // ── клиент → сервер ────────────────────────────────────────────────────────

    fun sendText(text: String, id: String = makeId()): String {
        send(JSONObject(mapOf("t" to "text", "id" to id, "text" to text)))
        return id
    }

    fun sendAudio(wav: ByteArray, transcript: String? = null): String {
        val id = makeId()
        send(JSONObject(mapOf(
            "t" to "audio", "id" to id, "format" to "wav",
            "data" to Base64.encodeToString(wav, Base64.NO_WRAP)
        )).apply { if (transcript != null) put("text", transcript) })
        return id
    }

    /** kind: image | video | document | audio. Большие файлы режутся на куски по 3 МБ. */
    fun sendMedia(kind: String, bytes: ByteArray, mime: String, filename: String? = null, caption: String? = null): String {
        require(kind in setOf("image", "video", "document", "audio")) { "kind: image|video|document|audio" }
        require(bytes.isNotEmpty()) { "sendMedia: пустой файл" }
        val id = makeId()
        val parts = maxOf(1, (bytes.size + MEDIA_CHUNK_BYTES - 1) / MEDIA_CHUNK_BYTES)
        for (i in 0 until parts) {
            val from = i * MEDIA_CHUNK_BYTES
            val to = minOf(from + MEDIA_CHUNK_BYTES, bytes.size)
            val frame = JSONObject(mapOf(
                "t" to "media", "id" to id, "kind" to kind, "mime" to mime,
                "data" to Base64.encodeToString(bytes.copyOfRange(from, to), Base64.NO_WRAP),
                "part" to i, "parts" to parts
            ))
            filename?.let { frame.put("filename", it) }
            caption?.let { frame.put("caption", it) }
            send(frame)
        }
        return id
    }

    /** Ответ на карточку. exec_approval once|session|always|deny; slash_confirm once|always|cancel; clarify c0..cN|other. */
    fun answerPrompt(promptId: String, optionId: String? = null, text: String? = null) {
        require(!(optionId == null && text.isNullOrBlank())) { "нужен optionId или text" }
        val frame = JSONObject(mapOf("t" to "prompt", "prompt_id" to promptId))
        optionId?.let { frame.put("option_id", it.lowercase()) }
        text?.let { frame.put("text", it) }
        send(frame)
    }

    fun acknowledge(messageId: String) = send(JSONObject(mapOf("t" to "ack", "id" to messageId)))

    // ── сервер → клиент ────────────────────────────────────────────────────────

    private fun handleFrame(raw: String) {
        val f = try { JSONObject(raw) } catch (_: Exception) { return } // мусор игнорируем
        when (f.optString("t")) {
            "ready" -> { attempt = 0; listener?.onState(ConnectionState.Connected); listener?.onReady(f) }
            "typing" -> listener?.onTyping(!f.optBoolean("stop"))
            "message" -> {
                val id = f.optString("id"); val text = f.optString("text")
                if (f.optBoolean("edit")) listener?.onMessageEdit(id, text)
                else listener?.onMessage(id, text, f.optBoolean("final"))
            }
            "prompt" -> listener?.onPrompt(f)
            "prompt_done" -> listener?.onPromptDone(f.optString("id"),
                if (f.has("option_id")) f.optString("option_id") else null, f.optBoolean("expired"))
            "media" -> accumulate(f)?.let { listener?.onMediaComplete(it) }
            "avatar" -> listener?.onAvatar(f.optString("state", "idle"))
            "error" -> listener?.onError(f.optString("code", "error"), f.optString("message"), f.optBoolean("fatal"))
            else -> { /* неизвестный кадр игнорируем */ }
        }
    }

    private fun accumulate(f: JSONObject): JSONObject? {
        val id = f.optString("id")
        val parts = f.optInt("parts", 1)
        val part = f.optInt("part", 0)
        mediaMeta[id] = f
        if (parts <= 1) return f
        val bucket = mediaParts.getOrPut(id) { HashMap() }
        f.optString("data").takeIf { it.isNotEmpty() }?.let {
            bucket[part] = Base64.decode(it, Base64.NO_WRAP)
        }
        if (bucket.size < parts) return null
        mediaParts.remove(id)
        val merged = java.io.ByteArrayOutputStream()
        for (i in 0 until parts) bucket[i]?.let { merged.write(it) }
        return JSONObject(mediaMeta[id].toString()).apply {
            put("data", Base64.encodeToString(merged.toByteArray(), Base64.NO_WRAP))
        }
    }

    private fun makeId(): String = "${System.currentTimeMillis().toString(36)}-${(0..0xFFFFFF).random().toString(36)}"
}
```

## 2. Activity: лента, карточки (AlertDialog), медиа

```kotlin
// ChatActivity.kt
import android.app.AlertDialog
import android.os.Bundle
import android.os.Environment
import android.util.Base64
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

data class Item(val id: String, val role: String, val text: String, val card: JSONObject? = null, val resolved: String? = null)

class ChatActivity : AppCompatActivity(), HermesListener {
    private lateinit var client: HermesClient
    private val items = mutableListOf<Item>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        client = HermesClient(
            url = "wss://hermes-go.com/mobile-ws",
            token = getSharedPreferences("hermes", MODE_PRIVATE).getString("token", "")!!,
            device = JSONObject(mapOf("id" to "android-1", "name" to "Телефон", "platform" to "android", "app_version" to "1.0.0")),
        ).apply { setListener(this@ChatActivity); connect() }
    }

    override fun onResume() { super.onResume(); client.ensureConnected() } // вернулись из фона → поднять сокет
    override fun onDestroy() { super.onDestroy(); client.disconnect() }

    override fun onState(state: ConnectionState) { /* индикатор в шапке */ }

    override fun onMessage(id: String, text: String, isFinal: Boolean) {
        runOnUiThread {
            val i = items.indexOfFirst { it.id == id }
            if (i == -1) items.add(Item(id, "agent", text)) else items[i] = items[i].copy(text = items[i].text + text)
            render()
        }
    }

    override fun onMessageEdit(id: String, text: String) {
        runOnUiThread {   // стриминг: заменяем текст пузыря с тем же id
            val i = items.indexOfFirst { it.id == id }
            if (i == -1) items.add(Item(id, "agent", text)) else items[i] = items[i].copy(text = text)
            render()
        }
    }

    override fun onPrompt(prompt: JSONObject) {
        runOnUiThread { items.add(Item("card-${prompt.optString("id")}", "card", prompt.optString("text"), card = prompt)); render() }
    }

    override fun onPromptDone(id: String, optionId: String?, expired: Boolean) {
        runOnUiThread {
            val i = items.indexOfFirst { it.id == "card-$id" }
            if (i != -1) items[i] = items[i].copy(resolved = if (expired) "expired" else (optionId ?: "закрыта"))
            render()
        }
    }

    override fun onMediaComplete(media: JSONObject) {
        val id = media.optString("id")
        val name = media.optString("filename", "$id.bin")
        val dir = File(getExternalFilesDir(Environment.DIRECTORY_DOCUMENTS), "hermes-media").apply { mkdirs() }
        val file = File(dir, name)
        val data = media.optString("data")
        if (data.isNotEmpty()) {
            lifecycleScope.launch(Dispatchers.IO) { file.writeBytes(Base64.decode(data, Base64.NO_WRAP)) }
        }
        // data нет — у медиа есть только "url": скачайте сами (OkHttp).
        runOnUiThread { items.add(Item("media-$id-${System.currentTimeMillis()}", "media", media.optString("caption", name))); render() }
    }

    override fun onError(code: String, message: String, fatal: Boolean) { /* баннер; при fatal не reconnect'им */ }

    override fun onAvatar(state: String) { /* анимация аватара: idle|thinking|speaking */ }
    override fun onTyping(active: Boolean) { /* «печатает» */ }
    override fun onReady(ready: JSONObject) { /* ready["avatar_url"], ready["capabilities"] */ }

    private fun render() { /* обновить RecyclerView */ }

    // ── карточка с кнопками ────────────────────────────────────────────────────
    private fun showCard(item: Item) {
        val prompt = item.card ?: return
        val options = prompt.optJSONArray("options") ?: return
        val labels = Array(options.length()) { options.getJSONObject(it).optString("label", options.getJSONObject(it).optString("id")) }
        val ids = Array(options.length()) { options.getJSONObject(it).optString("id") }
        AlertDialog.Builder(this)
            .setTitle(prompt.optString("kind"))
            .setMessage(prompt.optString("text"))
            .setItems(labels) { _, which -> answer(item, ids[which]) }
            .setNegativeButton("Отмена", null)
            .show()
    }

    private fun answer(item: Item, optionId: String) {
        val promptId = item.id.removePrefix("card-")
        if (optionId == "other") {
            val input = android.widget.EditText(this)
            AlertDialog.Builder(this).setTitle("Свой ответ").setView(input)
                .setPositiveButton("Отправить") { _, _ ->
                    val text = input.text.toString().trim()
                    if (text.isNotEmpty()) client.answerPrompt(promptId, optionId = "other", text = text) // other обязан нести text
                }
                .setNegativeButton("Отмена", null).show()
        } else {
            client.answerPrompt(promptId, optionId = optionId)
        }
        val i = items.indexOfFirst { it.id == item.id }
        if (i != -1) { items[i] = items[i].copy(resolved = optionId); render() }
    }

    private fun sendTextToAgent(text: String) {
        if (text.isBlank()) return
        client.sendText(text)
        items.add(Item("u-${System.currentTimeMillis()}", "user", text)); render()
    }
}
```

## 3. Отправка фото и голоса

```kotlin
// фото из галереи: читаем байты, жмём и отправляем
val bytes = contentResolver.openInputStream(uri)!!.use { it.readBytes() }
client.sendMedia("image", bytes, mime = "image/jpeg", filename = "photo.jpg", caption = "Смотри")

// голос: MediaRecorder с WAV-совместимыми настройками, затем sendAudio
// MediaRecorder не пишет WAV напрямую — либо пишите PCM и оборачивайте в WAV-заголовок,
// либо используйте библиотеку (напр. omrecorder). Ниже — отправка готового WAV-файла:
val wav = File(cacheDir, "voice.wav").readBytes()
client.sendAudio(wav, transcript = null)
```

## 4. Подводные камни

| Симптом | Причина | Решение |
|---|---|---|
| Дубли при стриминге | `edit` показан как новое | перерисовка по `id` в `onMessageEdit` |
| Сокет отвалился после сворачивания | система убила соединение | `ensureConnected()` в `onResume`, `pingInterval` в OkHttp |
| Reconnect по кругу | не разобран `onClosed(code=1008)` | при 1008 не переподключаться |
| ANR / крэш | тяжёлая работа со стримом в UI-потоке | запись файлов в `Dispatchers.IO`, UI только `runOnUiThread` |
| Карточка «зависла» | `other` без `text` | текст в том же кадре `prompt` |
| Медиа не пришло | `data` пуст, есть `url` | скачать по `url` через OkHttp |
| `NetworkOnMainThreadException` | отправка в главном потоке | OkHttp `send` сам неблокирующий (ws.send — ок), декодирование base64 — в IO |
