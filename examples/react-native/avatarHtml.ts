/**
 * Встроенная страница аватара (запасной вариант).
 *
 * В продакшене аватар берётся по URL из настроек (`APP_AVATAR_URL`, тот же аватарный
 * слой, что и в кабинете). Эта страница нужна, чтобы приложение работало «из коробки»
 * и чтобы приёмка не зависела от доступности внешнего URL: она реализует тот же
 * контракт — `window.hermesAvatar.setState/speak/setVolume`.
 */

export const AVATAR_HTML = `<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; height: 100%; background: transparent; overflow: hidden; }
  body { display: flex; align-items: center; justify-content: center; }
  .stage { position: relative; width: 190px; height: 190px; }
  .halo {
    position: absolute; inset: 0; border-radius: 50%;
    background: radial-gradient(circle at 50% 45%, rgba(120,180,255,.55), rgba(120,180,255,0) 62%);
    transition: opacity .4s ease, transform .4s ease; opacity: .45; transform: scale(1);
  }
  .head {
    position: absolute; left: 50%; top: 50%; width: 108px; height: 108px; margin: -54px 0 0 -54px;
    border-radius: 50%; background: linear-gradient(160deg, #dfe9ff 0%, #9fb8e6 55%, #6f8bc7 100%);
    box-shadow: inset 0 -10px 18px rgba(20,30,60,.35), 0 8px 30px rgba(0,0,0,.45);
    transition: transform .35s ease;
  }
  .eye { position: absolute; top: 44%; width: 12px; height: 12px; border-radius: 50%; background: #14203a; }
  .eye.left { left: 22%; } .eye.right { right: 22%; }
  .mouth {
    position: absolute; left: 50%; bottom: 26%; width: 30px; height: 6px; margin-left: -15px;
    border-radius: 6px; background: #223055; transition: height .12s linear, width .12s linear;
  }
  .label {
    position: absolute; bottom: -30px; left: 0; right: 0; text-align: center;
    font: 500 12px/1.2 -apple-system, system-ui, sans-serif; color: rgba(230,240,255,.75); letter-spacing: .04em;
  }
  body[data-state="thinking"] .head { animation: nod 1.6s ease-in-out infinite; }
  body[data-state="thinking"] .halo { opacity: .8; transform: scale(1.08); }
  body[data-state="speaking"] .halo { opacity: 1; animation: pulse .9s ease-in-out infinite; }
  body[data-state="speaking"] .mouth { animation: talk .18s steps(2, end) infinite; }
  @keyframes nod { 0%,100% { transform: translateY(0) } 50% { transform: translateY(-4px) } }
  @keyframes pulse { 0%,100% { transform: scale(1.04) } 50% { transform: scale(1.14) } }
  @keyframes talk { 0% { height: 5px } 100% { height: 15px } }
</style>
</head>
<body data-state="idle">
  <div class="stage">
    <div class="halo"></div>
    <div class="head"><div class="eye left"></div><div class="eye right"></div><div class="mouth"></div></div>
    <div class="label" id="label">Hermes</div>
  </div>
<script>
  var label = document.getElementById('label');
  var speaking = null;

  window.hermesAvatar = {
    setState: function (state) {
      document.body.dataset.state = state || 'idle';
      label.textContent = state === 'thinking' ? 'думаю…' : state === 'speaking' ? 'говорю' : 'Hermes';
    },
    speak: function (text) {
      if (typeof text === 'string' && text.length) { label.textContent = text.slice(0, 60); }
      window.hermesAvatar.setState('speaking');
      if (speaking) { clearTimeout(speaking); }
      speaking = setTimeout(function () { window.hermesAvatar.setState('idle'); }, 2500);
    },
    setVolume: function (level) {
      var scale = 1 + Math.max(0, Math.min(1, level)) * 0.12;
      document.querySelector('.halo').style.transform = 'scale(' + scale + ')';
    }
  };

  function notify(type, payload) {
    if (window.ReactNativeWebView) { window.ReactNativeWebView.postMessage(JSON.stringify({ type: type, payload: payload })); }
  }
  notify('avatar_ready', { contract: 'hermesAvatar v1' });
</script>
</body>
</html>`;
