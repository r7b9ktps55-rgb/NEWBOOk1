'use strict';

(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const store = {
    get(k, d) { try { const v = localStorage.getItem('tvr.' + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem('tvr.' + k, JSON.stringify(v)); } catch (_) { /* приватный режим */ } },
  };

  const state = {
    tvs: [],
    tvId: store.get('tvId', null),
    haptics: store.get('haptics', true),
    fastSwipe: store.get('fastSwipe', false),
    mode: store.get('mode', 'pad'),
    apps: {},
    info: null,
  };

  renderIcons();

  // ---------- Утилиты ----------
  let toastTimer;
  function toast(msg, isErr = false) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.toggle('err', isErr);
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), isErr ? 3600 : 2000);
  }
  let lastErr = '', lastErrAt = 0;
  function errToast(msg) {
    const now = Date.now();
    if (msg === lastErr && now - lastErrAt < 4000) return;
    lastErr = msg; lastErrAt = now;
    toast(msg, true);
  }
  const buzz = (ms = 10) => { if (state.haptics && navigator.vibrate) try { navigator.vibrate(ms); } catch (_) { /* */ } };
  const currentTv = () => state.tvs.find((t) => t.id === state.tvId) || state.tvs[0] || null;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: 'same-origin',
    });
    const ct = res.headers.get('content-type') || '';
    const data = ct.includes('json') ? await res.json() : await res.blob();
    if (res.status === 401 && data && data.needPin) showLogin();
    if (!res.ok) throw new Error((data && data.error) || `Ошибка ${res.status}`);
    return data;
  }

  // ---------- WebSocket ----------
  let ws = null, wsReady = false, wsBackoff = 500, msgId = 0;
  const pending = new Map();

  function connectWs() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { wsReady = true; wsBackoff = 500; };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.t === 'status') { state.tvs = msg.tvs; renderStatus(); return; }
      const p = pending.get(msg.id);
      if (p) { pending.delete(msg.id); clearTimeout(p.timer); p.resolve(msg); }
    };
    ws.onclose = () => {
      wsReady = false;
      for (const p of pending.values()) p.resolve({ ok: false, error: 'Нет связи с сервером пульта' });
      pending.clear();
      setTimeout(connectWs, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 8000);
      if (wsBackoff >= 4000) setBanner('Нет связи с сервером пульта. Переподключаюсь…', true);
    };
  }

  function wsSend(payload) {
    return new Promise((resolve) => {
      if (!wsReady) return resolve(null);
      const id = ++msgId;
      const timer = setTimeout(() => { pending.delete(id); resolve({ ok: false, error: 'ТВ не отвечает' }); }, 20000);
      pending.set(id, { resolve, timer });
      ws.send(JSON.stringify({ id, ...payload }));
    });
  }

  async function action(act) {
    const tv = currentTv();
    if (!tv) { openSheet(); toast('Сначала добавьте телевизор'); return { ok: false }; }
    let res = await wsSend({ t: 'action', tv: tv.id, action: act });
    if (!res) {
      try { await api(`/api/tvs/${tv.id}/action`, { method: 'POST', body: act }); res = { ok: true }; }
      catch (e) { res = { ok: false, error: e.message }; }
    }
    if (!res.ok && res.error) errToast(res.error);
    return res;
  }

  const press = (key, opts = {}) => action({ type: 'key', key, ...opts });

  // ---------- Кнопки с data-key ----------
  // data-repeat — авто-повтор при удержании; data-longpress — долгое нажатие при удержании
  function bindKeyButtons(root = document) {
    $$('[data-key]', root).forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      const key = btn.dataset.key;
      const repeat = btn.hasAttribute('data-repeat');
      const longp = btn.hasAttribute('data-longpress');
      let t1 = null, t2 = null, longFired = false, down = false;

      const clear = () => { clearTimeout(t1); clearInterval(t2); t1 = t2 = null; btn.classList.remove('pressed'); };
      btn.addEventListener('pointerdown', (e) => {
        if (e.button !== undefined && e.button !== 0) return;
        down = true; longFired = false;
        btn.classList.add('pressed');
        try { btn.setPointerCapture(e.pointerId); } catch (_) { /* */ }
        if (repeat) {
          buzz(); press(key);
          t1 = setTimeout(() => { t2 = setInterval(() => { buzz(5); press(key); }, 110); }, 420);
        } else if (longp) {
          t1 = setTimeout(() => { longFired = true; buzz(30); press(key, { long: true }); }, 550);
        }
      });
      const up = (e) => {
        if (!down) return;
        down = false;
        const cancelled = e.type === 'pointercancel';
        clear();
        if (cancelled || repeat) return;
        if (!longFired) { buzz(); press(key); }
      };
      btn.addEventListener('pointerup', up);
      btn.addEventListener('pointercancel', up);
      btn.addEventListener('contextmenu', (e) => e.preventDefault());
    });
  }
  bindKeyButtons();

  // ---------- Сенсорная панель ----------
  const tp = $('#touchpad');
  const tpDot = $('#tpDot');
  const tpArrow = $('#tpArrow');
  const DIRS = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
  const ARROW_ICON = { up: 'chevron-up', down: 'chevron-down', left: 'chevron-left', right: 'chevron-right' };

  function flashArrow(dir) {
    tpArrow.innerHTML = `<i data-icon="${ARROW_ICON[dir]}" style="width:64px;height:64px"></i>`;
    renderIcons(tpArrow);
    const [x, y] = DIRS[dir];
    tpArrow.style.setProperty('--ax', `${x * 20}px`);
    tpArrow.style.setProperty('--ay', `${y * 20}px`);
    tpArrow.classList.remove('show');
    void tpArrow.offsetWidth;
    tpArrow.classList.add('show');
  }
  function ripple(x, y) {
    const r = document.createElement('div');
    r.className = 'tp-ripple';
    r.style.left = x + 'px'; r.style.top = y + 'px';
    tp.appendChild(r);
    setTimeout(() => r.remove(), 450);
  }

  let g = null;
  tp.addEventListener('contextmenu', (e) => e.preventDefault());
  tp.addEventListener('pointerdown', (e) => {
    if (g) return;
    try { tp.setPointerCapture(e.pointerId); } catch (_) { /* */ }
    const rect = tp.getBoundingClientRect();
    g = {
      id: e.pointerId, rect, sx: e.clientX, sy: e.clientY, ax: e.clientX, ay: e.clientY,
      t0: performance.now(), moved: 0, lastDir: null, hx: 0, hy: 0, holdTimer: null, repeatTimer: null, longTimer: null, longFired: false, sent: 0,
    };
    tp.classList.add('active');
    tpDot.style.left = (e.clientX - rect.left) + 'px';
    tpDot.style.top = (e.clientY - rect.top) + 'px';
    g.longTimer = setTimeout(() => {
      if (g && g.moved < 12) { g.longFired = true; buzz(35); press('ok', { long: true }); toast('Долгое OK'); }
    }, 600);
  });

  function stopRepeat() {
    if (!g) return;
    clearTimeout(g.holdTimer); clearInterval(g.repeatTimer);
    g.holdTimer = g.repeatTimer = null;
  }

  tp.addEventListener('pointermove', (e) => {
    if (!g || e.pointerId !== g.id) return;
    const step = state.fastSwipe ? 28 : 44;
    tpDot.style.left = (e.clientX - g.rect.left) + 'px';
    tpDot.style.top = (e.clientY - g.rect.top) + 'px';
    const dx = e.clientX - g.ax, dy = e.clientY - g.ay;
    g.moved = Math.max(g.moved, Math.hypot(e.clientX - g.sx, e.clientY - g.sy));
    if (g.moved > 12) clearTimeout(g.longTimer);
    // Палец сдвинулся после остановки — прекращаем авто-прокрутку
    if ((g.holdTimer || g.repeatTimer) && Math.hypot(e.clientX - g.hx, e.clientY - g.hy) > 18) stopRepeat();
    const adx = Math.abs(dx), ady = Math.abs(dy);
    if (Math.max(adx, ady) < step) return;
    const dir = adx > ady ? (dx > 0 ? 'right' : 'left') : (dy > 0 ? 'down' : 'up');
    // Сколько шагов прошли (быстрый длинный свайп = несколько нажатий)
    const n = Math.min(4, Math.floor(Math.max(adx, ady) / step));
    for (let i = 0; i < n; i++) press(dir);
    g.sent += n;
    buzz(6);
    flashArrow(dir);
    if (adx > ady) { g.ax += Math.sign(dx) * n * step; g.ay = e.clientY; } else { g.ay += Math.sign(dy) * n * step; g.ax = e.clientX; }
    g.lastDir = dir;
    g.hx = e.clientX; g.hy = e.clientY;
    stopRepeat();
    // Удержание пальца после свайпа -> авто-прокрутка
    const d = dir;
    g.holdTimer = setTimeout(() => {
      if (!g) return;
      g.repeatTimer = setInterval(() => { press(d); buzz(4); flashArrow(d); }, 140);
    }, 450);
  });

  function endGesture(e) {
    if (!g || (e && e.pointerId !== g.id)) return;
    const dur = performance.now() - g.t0;
    clearTimeout(g.longTimer);
    stopRepeat();
    if (e && e.type === 'pointerup' && !g.longFired && g.moved < 12 && g.sent === 0 && dur < 500) {
      buzz(14);
      press('ok');
      ripple(e.clientX - g.rect.left, e.clientY - g.rect.top);
    }
    tp.classList.remove('active');
    g = null;
  }
  tp.addEventListener('pointerup', endGesture);
  tp.addEventListener('pointercancel', endGesture);

  // Режим: сенсор / кнопки
  function setMode(mode) {
    state.mode = mode;
    store.set('mode', mode);
    $$('.seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.mode === mode));
    $('#touchpad').hidden = mode !== 'pad';
    $('#dpad').hidden = mode !== 'dpad';
  }
  $$('.seg-btn').forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));
  setMode(state.mode);

  // ---------- Вкладки ----------
  function setTab(name) {
    $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
    $$('.view').forEach((v) => v.classList.toggle('active', v.dataset.view === name));
    if (name === 'apps') loadApps();
  }
  $$('.tab').forEach((t) => t.addEventListener('click', () => setTab(t.dataset.tab)));

  // ---------- Статус ----------
  function setBanner(html, isErr = false) {
    const b = $('#banner');
    if (!html) { b.hidden = true; return; }
    b.innerHTML = html;
    b.classList.toggle('err', isErr);
    b.hidden = false;
  }

  function renderStatus() {
    const tv = currentTv();
    if (tv && tv.id !== state.tvId) { state.tvId = tv.id; store.set('tvId', tv.id); }
    $('#tvName').textContent = tv ? tv.name : 'Добавить ТВ';
    $('#tvDot').className = 'dot ' + (tv ? tv.state : '');
    if (!tv) setBanner('Телевизор ещё не добавлен. <button id="bnAdd">Добавить</button>');
    else if (tv.state === 'unauthorized') setBanner('На экране телевизора появился запрос «Разрешить отладку по USB?» — отметьте «Всегда разрешать» и нажмите «OK».');
    else if (tv.state !== 'device') setBanner(`«${esc(tv.name)}» не в сети (${esc(tv.host)}). Включите ТВ и проверьте, что отладка включена. <button id="bnRetry">Повторить</button>`, true);
    else setBanner('');
    const add = $('#bnAdd'); if (add) add.onclick = () => openSheet(true);
    const retry = $('#bnRetry'); if (retry) retry.onclick = () => reconnect(tv);
    if (!$('#sheet').hidden) renderTvList();
  }

  async function reconnect(tv) {
    toast('Подключаюсь…');
    try {
      const r = await api(`/api/tvs/${tv.id}/connect`, { method: 'POST' });
      toast(r.tv.state === 'device' ? 'Подключено' : (r.message || 'Не удалось подключиться'), r.tv.state !== 'device');
    } catch (e) { errToast(e.message); }
  }

  // Быстрое переключение ТВ по нажатию на название
  $('#tvPill').addEventListener('click', () => {
    if (state.tvs.length > 1) {
      const i = state.tvs.findIndex((t) => t.id === state.tvId);
      const next = state.tvs[(i + 1) % state.tvs.length];
      state.tvId = next.id; store.set('tvId', next.id);
      renderStatus();
      toast(`Управляю: ${next.name}`);
      buzz(15);
    } else openSheet(!state.tvs.length);
  });

  // ---------- Приложения ----------
  async function loadApps(force = false) {
    const tv = currentTv();
    const grid = $('#appsGrid');
    const empty = $('#appsEmpty');
    if (!tv) { grid.innerHTML = ''; empty.hidden = false; empty.textContent = 'Сначала добавьте телевизор.'; return; }
    if (!force && state.apps[tv.id]) return renderApps(state.apps[tv.id]);
    empty.hidden = true;
    grid.innerHTML = Array.from({ length: 8 }, () => '<div class="app-tile"><div class="app-icon skeleton"></div><div class="app-name">&nbsp;</div></div>').join('');
    try {
      const { apps } = await api(`/api/tvs/${tv.id}/apps${force ? '?refresh=1' : ''}`);
      state.apps[tv.id] = apps;
      renderApps(apps);
    } catch (e) {
      grid.innerHTML = '';
      empty.hidden = false;
      empty.textContent = e.message;
    }
  }
  function initials(name) {
    const w = name.replace(/[^\p{L}\p{N} +]/gu, ' ').trim().split(/\s+/);
    return (w.length > 1 ? w[0][0] + w[1][0] : name.slice(0, 2)).toUpperCase();
  }
  function renderApps(apps) {
    const grid = $('#appsGrid');
    $('#appsEmpty').hidden = apps.length > 0;
    $('#appsEmpty').textContent = 'Приложения не найдены.';
    grid.innerHTML = apps.map((a, i) => `
      <button class="app-tile" data-i="${i}">
        <div class="app-icon" style="background:${esc(a.color)}">${esc(initials(a.name))}</div>
        <div class="app-name">${esc(a.name)}</div>
      </button>`).join('');
    $$('.app-tile', grid).forEach((el) => el.addEventListener('click', async () => {
      const a = apps[+el.dataset.i];
      buzz(15);
      const r = await action({ type: 'launch', pkg: a.pkg, component: a.component });
      if (r.ok) { toast(`Открываю ${a.name}`); setTab('remote'); }
    }));
  }
  $('#appsRefresh').addEventListener('click', () => loadApps(true));

  // ---------- Клавиатура ----------
  const kbText = $('#kbText');
  const kbLive = $('#kbLive');
  let liveSent = '';
  $('#kbSend').addEventListener('click', async () => {
    const text = kbText.value;
    if (!text.trim()) return;
    const r = await action({ type: 'text', text });
    if (r.ok) { toast('Отправлено'); kbText.value = ''; liveSent = ''; }
  });
  $('#kbClear').addEventListener('click', () => { buzz(); press('del', { repeat: 30 }); });
  $('#kbSearch').addEventListener('click', async () => {
    const q = kbText.value.trim();
    if (!q) { press('search'); return; }
    const r = await action({ type: 'search', query: q });
    if (r.ok) toast(`Ищу: ${q}`);
  });
  kbLive.addEventListener('change', () => { liveSent = kbText.value; });
  kbText.addEventListener('input', () => {
    if (!kbLive.checked) return;
    const now = kbText.value;
    if (now.startsWith(liveSent)) {
      const add = now.slice(liveSent.length);
      if (add) action({ type: 'text', text: add });
    } else if (liveSent.startsWith(now)) {
      const n = liveSent.length - now.length;
      if (n) press('del', { repeat: n });
    } else {
      press('del', { repeat: Math.min(30, liveSent.length) });
      if (now) action({ type: 'text', text: now });
    }
    liveSent = now;
  });

  // ---------- Скриншот ----------
  let shotUrl = null;
  async function screenshot() {
    const tv = currentTv();
    if (!tv) return;
    setTab('more');
    const box = $('#shotBox');
    box.innerHTML = '<p class="muted">Делаю снимок экрана…</p>';
    try {
      const blob = await api(`/api/tvs/${tv.id}/screenshot`);
      if (shotUrl) URL.revokeObjectURL(shotUrl);
      shotUrl = URL.createObjectURL(blob);
      box.innerHTML = `<img src="${shotUrl}" alt="Экран телевизора">`;
      box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) {
      box.innerHTML = `<p class="muted center">${esc(e.message)}</p>`;
    }
  }
  $('#shotBtn').addEventListener('click', screenshot);
  $('#shotBtn2').addEventListener('click', screenshot);

  // ---------- Голос ----------
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const voiceEl = $('#voice');
  const vStatus = $('#voiceStatus');
  const vText = $('#voiceText');
  const vInput = $('#voiceInput');
  const vOrb = $('#voiceOrb');
  let rec = null;
  const EXAMPLES = ['громче на 5', 'пауза', 'открой ютуб', 'найди котиков на ютубе', 'канал 3', 'домой', 'без звука', 'выключи телевизор'];
  $('#voiceChips').innerHTML = EXAMPLES.map((x) => `<button class="chip" type="button">${esc(x)}</button>`).join('');
  $$('#voiceChips .chip').forEach((c) => c.addEventListener('click', () => runVoice(c.textContent)));

  async function runVoice(text) {
    text = String(text || '').trim();
    if (!text) return;
    const tv = currentTv();
    if (!tv) { closeVoice(); openSheet(true); return; }
    vText.textContent = `«${text}»`;
    vStatus.textContent = 'Выполняю…';
    let res = await wsSend({ t: 'voice', tv: tv.id, text });
    if (!res) {
      try { res = await api(`/api/tvs/${tv.id}/voice`, { method: 'POST', body: { text } }); }
      catch (e) { res = { ok: false, error: e.message }; }
    }
    if (res.ok) {
      buzz(20);
      toast(res.say || 'Готово');
      closeVoice();
    } else {
      vStatus.textContent = res.error || res.say || 'Не получилось';
      vOrb.classList.remove('listening');
    }
  }

  function openVoice() {
    voiceEl.hidden = false;
    vText.textContent = '';
    vInput.value = '';
    const canSpeech = SR && window.isSecureContext;
    if (canSpeech) {
      $('#voiceHint').textContent = 'Говорите после сигнала. Можно также напечатать команду.';
      startListening();
    } else {
      vStatus.textContent = 'Скажите или напишите команду';
      vOrb.classList.remove('listening');
      const https = state.info && state.info.urls && state.info.urls.https[0];
      $('#voiceHint').innerHTML = SR && !window.isSecureContext && https
        ? `Распознавание речи браузера работает только по HTTPS. Нажмите 🎤 на клавиатуре телефона или откройте <a href="${esc(https)}" style="color:var(--accent)">защищённую версию</a>.`
        : 'Нажмите 🎤 на клавиатуре телефона и продиктуйте команду.';
      setTimeout(() => vInput.focus(), 50);
    }
  }
  function startListening() {
    try {
      rec = new SR();
      rec.lang = 'ru-RU';
      rec.interimResults = true;
      rec.maxAlternatives = 1;
      rec.onstart = () => { vStatus.textContent = 'Слушаю…'; vOrb.classList.add('listening'); buzz(15); };
      rec.onresult = (ev) => {
        let interim = '', final = '';
        for (const r of ev.results) (r.isFinal ? (final += r[0].transcript) : (interim += r[0].transcript));
        vText.textContent = final || interim;
        if (final) { rec.onend = null; try { rec.stop(); } catch (_) { /* */ } runVoice(final); }
      };
      rec.onerror = (ev) => {
        vOrb.classList.remove('listening');
        vStatus.textContent = ev.error === 'not-allowed' ? 'Нет доступа к микрофону' : ev.error === 'no-speech' ? 'Ничего не услышал' : 'Ошибка распознавания';
        $('#voiceHint').textContent = 'Нажмите на кружок, чтобы попробовать снова, или напишите команду.';
      };
      rec.onend = () => { vOrb.classList.remove('listening'); if (vStatus.textContent === 'Слушаю…') vStatus.textContent = 'Нажмите на кружок и говорите'; };
      rec.start();
    } catch (_) {
      vStatus.textContent = 'Распознавание недоступно — напишите команду';
    }
  }
  function closeVoice() {
    voiceEl.hidden = true;
    if (rec) { rec.onend = null; rec.onresult = null; try { rec.abort(); } catch (_) { /* */ } rec = null; }
    vOrb.classList.remove('listening');
  }
  $('#micBtn').addEventListener('click', () => { buzz(15); openVoice(); });
  $('#voiceClose').addEventListener('click', closeVoice);
  vOrb.addEventListener('click', () => { if (SR && window.isSecureContext) startListening(); else vInput.focus(); });
  voiceEl.addEventListener('click', (e) => { if (e.target === voiceEl) closeVoice(); });
  $('#voiceForm').addEventListener('submit', (e) => { e.preventDefault(); runVoice(vInput.value); });

  // ---------- Настройки / телевизоры ----------
  const sheet = $('#sheet');
  function openSheet(openAdd = false) {
    sheet.hidden = false;
    renderTvList();
    renderShare();
    if (openAdd) $('#addCard').open = true;
  }
  function closeSheet() { sheet.hidden = true; }
  $('#settingsBtn').addEventListener('click', () => openSheet());
  $('#sheetClose').addEventListener('click', closeSheet);
  sheet.addEventListener('click', (e) => { if (e.target === sheet) closeSheet(); });

  const STATE_TEXT = { device: 'подключён', unauthorized: 'ждёт подтверждения на ТВ', offline: 'не в сети', unknown: 'проверяю…' };
  function renderTvList() {
    const list = $('#tvList');
    if (!state.tvs.length) {
      list.innerHTML = '<p class="muted small">Пока нет телевизоров. Добавьте по IP-адресу или найдите в сети ниже.</p>';
    } else {
      list.innerHTML = state.tvs.map((t) => `
        <div class="tv-item ${t.id === state.tvId ? 'current' : ''}" data-id="${esc(t.id)}">
          <span class="dot ${esc(t.state)}"></span>
          <div class="meta" data-act="select"><b>${esc(t.name)}</b><small>${esc(t.host)}:${esc(t.port)} · ${esc(STATE_TEXT[t.state] || t.state)}</small></div>
          <button class="icon-btn" data-act="connect" aria-label="Переподключить"><i data-icon="refresh"></i></button>
          <button class="icon-btn" data-act="edit" aria-label="Изменить"><i data-icon="edit"></i></button>
          <button class="icon-btn" data-act="delete" aria-label="Удалить"><i data-icon="trash"></i></button>
        </div>`).join('');
      renderIcons(list);
      $$('.tv-item', list).forEach((row) => {
        const tv = state.tvs.find((t) => t.id === row.dataset.id);
        row.querySelector('[data-act=select]').onclick = () => { state.tvId = tv.id; store.set('tvId', tv.id); delete state.apps[tv.id]; renderStatus(); toast(`Управляю: ${tv.name}`); };
        row.querySelector('[data-act=connect]').onclick = () => reconnect(tv);
        row.querySelector('[data-act=edit]').onclick = async () => {
          const name = prompt('Название телевизора', tv.name);
          if (name === null) return;
          const mac = prompt('MAC-адрес для включения (можно пусто)', tv.mac || '');
          try { await api(`/api/tvs/${tv.id}`, { method: 'PUT', body: { name, mac: mac ?? tv.mac } }); toast('Сохранено'); } catch (e) { errToast(e.message); }
        };
        row.querySelector('[data-act=delete]').onclick = async () => {
          if (!confirm(`Удалить «${tv.name}» из пульта?`)) return;
          try { await api(`/api/tvs/${tv.id}`, { method: 'DELETE' }); toast('Удалено'); } catch (e) { errToast(e.message); }
        };
      });
    }
    $('#pairTv').innerHTML = state.tvs.map((t) => `<option value="${esc(t.id)}">${esc(t.name)} (${esc(t.host)})</option>`).join('');
  }

  $('#addForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { name: f.name.value.trim() || 'Телевизор', host: f.host.value.trim(), mac: f.mac.value.trim() };
    try {
      const { tv } = await api('/api/tvs', { method: 'POST', body });
      state.tvId = tv.id; store.set('tvId', tv.id);
      f.reset();
      toast('Добавлено. Подтвердите запрос на экране ТВ');
    } catch (err) { errToast(err.message); }
  });

  $('#discoverBtn').addEventListener('click', async () => {
    const box = $('#discoverList');
    box.innerHTML = '<p class="muted small">Ищу устройства с открытым ADB (порт 5555)… до 15 секунд</p>';
    try {
      const { hosts } = await api('/api/discover');
      if (!hosts.length) { box.innerHTML = '<p class="muted small">Ничего не найдено. Убедитесь, что на ТВ включена отладка по сети, или добавьте IP вручную.</p>'; return; }
      box.innerHTML = hosts.map((h) => `<button class="btn" data-host="${esc(h.host)}" ${h.known ? 'disabled' : ''}><span>${esc(h.host)}</span><span class="muted">${h.known ? 'уже добавлен' : 'добавить'}</span></button>`).join('');
      $$('button[data-host]', box).forEach((b) => b.addEventListener('click', () => {
        $('#addForm').host.value = b.dataset.host;
        $('#addForm').name.focus();
      }));
    } catch (err) { box.innerHTML = `<p class="muted small">${esc(err.message)}</p>`; }
  });

  $('#pairForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const f = e.target;
    if (!f.tv.value) { errToast('Сначала добавьте телевизор по IP'); return; }
    toast('Сопрягаю…');
    try {
      const r = await api(`/api/tvs/${f.tv.value}/pair`, { method: 'POST', body: { port: f.port.value, code: f.code.value, connectPort: f.connectPort.value } });
      toast(r.ok ? 'Сопряжено!' : r.message, !r.ok);
      if (r.ok) f.reset();
    } catch (err) { errToast(err.message); }
  });

  function renderShare() {
    const list = $('#qrList');
    const urls = new Set([location.origin]);
    if (state.info) [...state.info.urls.https, ...state.info.urls.http].forEach((u) => urls.add(u));
    const arr = [...urls].filter((u) => !/localhost|127\.0\.0\.1/.test(u) || urls.size === 1).slice(0, 4);
    list.innerHTML = arr.map((u) => `
      <div class="qr-item">
        <img src="/api/qr?text=${encodeURIComponent(u)}" alt="QR-код">
        <div><code>${esc(u)}</code><p>${u.startsWith('https') ? 'С голосом. При первом входе: «Дополнительно» → «Перейти на сайт».' : 'Без предупреждений, голос — через клавиатуру.'}</p></div>
      </div>`).join('');
  }

  const optH = $('#optHaptics');
  optH.checked = state.haptics;
  optH.addEventListener('change', () => { state.haptics = optH.checked; store.set('haptics', optH.checked); });
  const optF = $('#optFastSwipe');
  optF.checked = state.fastSwipe;
  optF.addEventListener('change', () => { state.fastSwipe = optF.checked; store.set('fastSwipe', optF.checked); });

  // ---------- PIN ----------
  function showLogin() {
    $('#login').hidden = false;
    setTimeout(() => $('#pinInput').focus(), 50);
  }
  $('#loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/login', { method: 'POST', body: { pin: $('#pinInput').value } });
      location.reload();
    } catch (err) { $('#loginErr').textContent = err.message; }
  });

  // ---------- Старт ----------
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && (!ws || ws.readyState > 1)) { wsBackoff = 500; connectWs(); }
  });

  (async () => {
    try {
      state.info = await api('/api/info');
      if (state.info.needPin && !state.info.authed) { showLogin(); return; }
      const { tvs } = await api('/api/tvs');
      state.tvs = tvs;
      renderStatus();
      if (!tvs.length) openSheet(true);
    } catch (e) {
      setBanner(esc(e.message), true);
    }
    connectWs();
  })();

  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('sw.js').catch(() => {});
  }
})();
