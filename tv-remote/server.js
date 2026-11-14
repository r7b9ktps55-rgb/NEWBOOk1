'use strict';

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { WebSocketServer } = require('ws');
const QRCode = require('qrcode');

const { Adb, TvSession } = require('./lib/adb');
const { resolveKey } = require('./lib/keys');
const { describeApp } = require('./lib/apps');
const { parseCommand } = require('./lib/voice');
const { localIPv4, discover, wakeOnLan } = require('./lib/net');

// ---------- Конфигурация ----------
const PORT = parseInt(process.env.PORT || '8080', 10);
const HTTPS_PORT = parseInt(process.env.HTTPS_PORT || '8443', 10);
const DATA_DIR = path.resolve(process.env.DATA_DIR || path.join(__dirname, 'data'));
const PUBLIC_DIR = path.join(__dirname, 'public');
const ADB_PATH = process.env.ADB_PATH || 'adb';
const ACCESS_PIN = (process.env.ACCESS_PIN || '').trim();
const POLL_MS = parseInt(process.env.POLL_MS || '10000', 10);

fs.mkdirSync(DATA_DIR, { recursive: true });
const TVS_FILE = path.join(DATA_DIR, 'tvs.json');

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// ---------- Хранилище телевизоров ----------
function loadTvs() {
  try {
    return JSON.parse(fs.readFileSync(TVS_FILE, 'utf8'));
  } catch (_) {
    // Первичная настройка из переменной TV_HOSTS="Зал=192.168.1.50,Кухня=192.168.1.51"
    const seed = (process.env.TV_HOSTS || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s, i) => {
        const [name, host] = s.includes('=') ? s.split('=') : [`Телевизор ${i + 1}`, s];
        return makeTv({ name, host });
      });
    return seed;
  }
}

function saveTvs() {
  const data = tvList.map(({ id, name, host, port, mac }) => ({ id, name, host, port, mac }));
  fs.writeFileSync(TVS_FILE, JSON.stringify(data, null, 2));
}

const HOST_RE = /^[A-Za-z0-9.-]{1,253}$/;

function makeTv({ name, host, port, mac }) {
  host = String(host || '').trim();
  let p = parseInt(port || '5555', 10);
  const m = host.match(/^(.+):(\d+)$/);
  if (m) {
    host = m[1];
    p = parseInt(m[2], 10);
  }
  if (!HOST_RE.test(host)) throw new Error('Неверный IP-адрес');
  if (!(p > 0 && p < 65536)) throw new Error('Неверный порт');
  return {
    id: crypto.randomBytes(4).toString('hex'),
    name: String(name || 'Телевизор').trim().slice(0, 40) || 'Телевизор',
    host,
    port: p,
    mac: String(mac || '').trim().slice(0, 17),
  };
}

const adb = new Adb(ADB_PATH);
let tvList = loadTvs();
const runtime = new Map(); // id -> { session, state, message, apps, appsAt }

function rt(tv) {
  let r = runtime.get(tv.id);
  const serial = `${tv.host}:${tv.port}`;
  if (!r || r.serial !== serial) {
    if (r) r.session.dispose();
    r = { serial, session: new TvSession(adb, serial), state: 'unknown', message: '', apps: null, appsAt: 0 };
    runtime.set(tv.id, r);
  }
  return r;
}

function publicTv(tv) {
  const r = rt(tv);
  return { id: tv.id, name: tv.name, host: tv.host, port: tv.port, mac: tv.mac, state: r.state, message: r.message };
}

function getTv(id) {
  const tv = tvList.find((t) => t.id === id) || (id ? null : tvList[0]);
  if (!tv) throw httpError(404, 'Телевизор не найден');
  return tv;
}

// ---------- Подключение и статус ----------
async function connectTv(tv) {
  const r = rt(tv);
  const res = await adb.connect(r.serial);
  setState(tv, res.state, res.message);
  return res;
}

function setState(tv, state, message = '') {
  const r = rt(tv);
  if (r.state !== state) {
    log(`[${tv.name}] ${r.state} -> ${state}`);
    r.state = state;
    r.message = message;
    if (state !== 'device') r.session.kill();
    broadcastStatus();
  }
}

let polling = false;
async function pollAll() {
  if (polling) return;
  polling = true;
  try {
    const devs = await adb.devices();
    for (const tv of tvList) {
      const r = rt(tv);
      const st = devs.get(r.serial);
      if (st === 'device') setState(tv, 'device');
      else await connectTv(tv).catch(() => setState(tv, 'offline'));
    }
  } catch (e) {
    log('poll error', e.message);
  } finally {
    polling = false;
  }
}

async function ensureSession(tv) {
  const r = rt(tv);
  if (r.state !== 'device') await connectTv(tv);
  if (r.state === 'unauthorized') {
    throw httpError(409, 'Подтвердите отладку на экране телевизора («Разрешить» + «Всегда разрешать»)');
  }
  if (r.state !== 'device') throw httpError(503, 'Телевизор недоступен. Он включён и в той же сети?');
  return r.session;
}

async function getApps(tv, force = false) {
  const r = rt(tv);
  if (!force && r.apps && Date.now() - r.appsAt < 5 * 60 * 1000) return r.apps;
  const session = await ensureSession(tv);
  const raw = await session.listApps();
  const hidden = /(tvlauncher|launcherx|\.setupwraith|\.tvrecommendations|com\.android\.tv\.settings$)/;
  const apps = raw
    .filter((a) => !hidden.test(a.pkg))
    .map((a) => ({ ...a, ...describeApp(a.pkg) }))
    .sort((a, b) => a.name.localeCompare(b.name, 'ru'));
  r.apps = apps;
  r.appsAt = Date.now();
  return apps;
}

// ---------- Действия ----------
async function wakeTv(tv) {
  if (tv.mac) await wakeOnLan(tv.mac).catch(() => {});
  try {
    const s = await ensureSession(tv);
    await s.key('KEYCODE_WAKEUP');
  } catch (e) {
    if (!tv.mac) throw e;
    // ТВ просыпается по WoL — ADB поднимется через несколько секунд
    setTimeout(() => connectTv(tv).catch(() => {}), 6000);
  }
}

async function runAction(tv, action) {
  switch (action.type) {
    case 'key': {
      const code = resolveKey(action.key);
      if (!code) throw httpError(400, `Неизвестная кнопка: ${action.key}`);
      if (action.key === 'wakeup') return wakeTv(tv);
      const s = await ensureSession(tv);
      const n = Math.min(30, Math.max(1, parseInt(action.repeat || 1, 10)));
      if (action.long) return s.key(code, { long: true });
      return s.keys(Array(n).fill(code));
    }
    case 'text': {
      const s = await ensureSession(tv);
      const res = await s.text(String(action.text || '').slice(0, 500));
      if (!res.ok) throw httpError(422, res.error);
      return;
    }
    case 'launch': {
      const s = await ensureSession(tv);
      return s.launch({ pkg: action.pkg, component: action.component });
    }
    case 'url': {
      const url = String(action.url || '');
      if (!/^https?:\/\//.test(url)) throw httpError(400, 'Неверная ссылка');
      const s = await ensureSession(tv);
      return s.openUrl(url);
    }
    case 'search': {
      const s = await ensureSession(tv);
      return s.globalSearch(String(action.query || '').slice(0, 200));
    }
    case 'wake':
      return wakeTv(tv);
    default:
      throw httpError(400, 'Неизвестное действие');
  }
}

async function runVoice(tv, text) {
  let apps = [];
  try {
    apps = await getApps(tv);
  } catch (_) {
    /* без списка приложений тоже работаем */
  }
  const parsed = parseCommand(text, apps);
  for (const a of parsed.actions) await runAction(tv, a);
  log(`[${tv.name}] voice: "${text}" -> ${parsed.say}`);
  return parsed;
}

// ---------- Авторизация (опциональный PIN) ----------
const SECRET_FILE = path.join(DATA_DIR, 'secret');
let SECRET;
try {
  SECRET = fs.readFileSync(SECRET_FILE, 'utf8').trim();
} catch (_) {
  SECRET = crypto.randomBytes(32).toString('hex');
  fs.writeFileSync(SECRET_FILE, SECRET, { mode: 0o600 });
}
const AUTH_TOKEN = crypto.createHmac('sha256', SECRET).update(`pin:${ACCESS_PIN}`).digest('hex');

function parseCookies(req) {
  const out = {};
  for (const part of String(req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function isAuthed(req) {
  if (!ACCESS_PIN) return true;
  const c = parseCookies(req).tvr_auth || '';
  return c.length === AUTH_TOKEN.length && crypto.timingSafeEqual(Buffer.from(c), Buffer.from(AUTH_TOKEN));
}

// ---------- HTTP ----------
function httpError(status, message) {
  const e = new Error(message);
  e.status = status;
  return e;
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > 64 * 1024) {
        reject(httpError(413, 'Слишком большой запрос'));
        req.destroy();
      } else chunks.push(c);
    });
    req.on('end', () => {
      if (!chunks.length) return resolve({});
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      } catch (_) {
        reject(httpError(400, 'Неверный JSON'));
      }
    });
    req.on('error', reject);
  });
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
};

function serveStatic(req, res, pathname) {
  if (pathname === '/') pathname = '/index.html';
  const file = path.normalize(path.join(PUBLIC_DIR, decodeURIComponent(pathname)));
  if (!file.startsWith(PUBLIC_DIR + path.sep)) return sendJson(res, 403, { error: 'forbidden' });
  fs.readFile(file, (err, data) => {
    if (err) return sendJson(res, 404, { error: 'not found' });
    const ext = path.extname(file);
    const headers = { 'Content-Type': MIME[ext] || 'application/octet-stream' };
    headers['Cache-Control'] = ['.html', '.js', '.css', '.webmanifest'].includes(ext) || file.endsWith('sw.js')
      ? 'no-cache'
      : 'public, max-age=86400';
    res.writeHead(200, headers);
    res.end(data);
  });
}

function serverUrls() {
  const ips = localIPv4().map((i) => i.address);
  return {
    http: ips.map((ip) => `http://${ip}:${PORT}`),
    https: httpsServer ? ips.map((ip) => `https://${ip}:${HTTPS_PORT}`) : [],
  };
}

async function handleApi(req, res, url) {
  const p = url.pathname;
  const method = req.method;
  let m;

  if (p === '/api/login' && method === 'POST') {
    const body = await readBody(req);
    if (!ACCESS_PIN || String(body.pin || '') === ACCESS_PIN) {
      res.setHeader(
        'Set-Cookie',
        `tvr_auth=${AUTH_TOKEN}; Path=/; Max-Age=${60 * 60 * 24 * 365 * 5}; HttpOnly; SameSite=Lax`
      );
      return sendJson(res, 200, { ok: true });
    }
    await new Promise((r) => setTimeout(r, 800));
    return sendJson(res, 401, { error: 'Неверный PIN' });
  }

  if (p === '/api/info' && method === 'GET') {
    return sendJson(res, 200, { needPin: !!ACCESS_PIN, authed: isAuthed(req), urls: serverUrls() });
  }

  if (!isAuthed(req)) return sendJson(res, 401, { error: 'Нужен PIN', needPin: true });

  if (p === '/api/qr' && method === 'GET') {
    const text = String(url.searchParams.get('text') || '').slice(0, 300);
    const svg = await QRCode.toString(text || 'empty', { type: 'svg', margin: 1, errorCorrectionLevel: 'M' });
    res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-store' });
    return res.end(svg);
  }

  if (p === '/api/tvs' && method === 'GET') return sendJson(res, 200, { tvs: tvList.map(publicTv) });

  if (p === '/api/tvs' && method === 'POST') {
    const body = await readBody(req);
    let tv;
    try {
      tv = makeTv(body);
    } catch (e) {
      throw httpError(400, e.message);
    }
    if (tvList.some((t) => t.host === tv.host && t.port === tv.port)) throw httpError(409, 'Этот ТВ уже добавлен');
    tvList.push(tv);
    saveTvs();
    connectTv(tv).catch(() => {});
    broadcastStatus();
    return sendJson(res, 201, { tv: publicTv(tv) });
  }

  if (p === '/api/discover' && method === 'GET') {
    const known = new Set(tvList.map((t) => t.host));
    const hosts = await discover(5555);
    return sendJson(res, 200, { hosts: hosts.map((h) => ({ host: h, known: known.has(h) })) });
  }

  if ((m = p.match(/^\/api\/tvs\/([a-f0-9]+)(\/[a-z]+)?$/))) {
    const tv = getTv(m[1]);
    const sub = m[2] || '';

    if (sub === '' && method === 'PUT') {
      const body = await readBody(req);
      let upd;
      try {
        upd = makeTv({ name: body.name ?? tv.name, host: body.host ?? tv.host, port: body.port ?? tv.port, mac: body.mac ?? tv.mac });
      } catch (e) {
        throw httpError(400, e.message);
      }
      Object.assign(tv, { name: upd.name, host: upd.host, port: upd.port, mac: upd.mac });
      saveTvs();
      connectTv(tv).catch(() => {});
      broadcastStatus();
      return sendJson(res, 200, { tv: publicTv(tv) });
    }
    if (sub === '' && method === 'DELETE') {
      tvList = tvList.filter((t) => t.id !== tv.id);
      const r = runtime.get(tv.id);
      if (r) {
        r.session.dispose();
        adb.disconnect(r.serial).catch(() => {});
        runtime.delete(tv.id);
      }
      saveTvs();
      broadcastStatus();
      return sendJson(res, 200, { ok: true });
    }
    if (sub === '/connect' && method === 'POST') {
      const r = rt(tv);
      await adb.disconnect(r.serial);
      const result = await connectTv(tv);
      return sendJson(res, 200, { tv: publicTv(tv), message: result.message });
    }
    if (sub === '/pair' && method === 'POST') {
      // Android 11+: Настройки → Для разработчиков → Отладка по Wi-Fi → Подключить по коду
      const body = await readBody(req);
      const pport = parseInt(body.port, 10);
      const code = String(body.code || '').replace(/\D/g, '');
      if (!(pport > 0 && pport < 65536) || code.length !== 6) throw httpError(400, 'Нужны порт и 6-значный код');
      const result = await adb.pair(`${tv.host}:${pport}`, code);
      if (body.connectPort) {
        const cport = parseInt(body.connectPort, 10);
        if (cport > 0 && cport < 65536) {
          tv.port = cport;
          saveTvs();
        }
      }
      if (result.ok) await connectTv(tv).catch(() => {});
      return sendJson(res, result.ok ? 200 : 400, { ok: result.ok, message: result.message, tv: publicTv(tv) });
    }
    if (sub === '/apps' && method === 'GET') {
      const apps = await getApps(tv, url.searchParams.has('refresh'));
      return sendJson(res, 200, { apps: apps.map(({ pkg, component, name, color }) => ({ pkg, component, name, color })) });
    }
    if (sub === '/screenshot' && method === 'GET') {
      const s = await ensureSession(tv);
      const png = await s.screenshot();
      res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' });
      return res.end(png);
    }
    if (sub === '/action' && method === 'POST') {
      const body = await readBody(req);
      await runAction(tv, body);
      return sendJson(res, 200, { ok: true });
    }
    if (sub === '/voice' && method === 'POST') {
      const body = await readBody(req);
      const result = await runVoice(tv, String(body.text || '').slice(0, 300));
      return sendJson(res, 200, { ok: !result.unknown, say: result.say });
    }
  }

  throw httpError(404, 'Не найдено');
}

async function onRequest(req, res) {
  const url = new URL(req.url, 'http://local');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  try {
    if (url.pathname.startsWith('/api/')) return await handleApi(req, res, url);
    if (req.method !== 'GET' && req.method !== 'HEAD') return sendJson(res, 405, { error: 'method' });
    return serveStatic(req, res, url.pathname);
  } catch (e) {
    if (!e.status) log('error', e.stack || e.message);
    if (!res.headersSent) sendJson(res, e.status || 500, { error: e.message || 'Ошибка' });
  }
}

// ---------- WebSocket ----------
const wss = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 });

function broadcastStatus() {
  const msg = JSON.stringify({ t: 'status', tvs: tvList.map(publicTv) });
  for (const c of wss.clients) if (c.readyState === 1) c.send(msg);
}

wss.on('connection', (ws) => {
  ws.isAlive = true;
  ws.on('pong', () => (ws.isAlive = true));
  ws.send(JSON.stringify({ t: 'status', tvs: tvList.map(publicTv) }));
  ws.on('message', async (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch (_) {
      return;
    }
    const reply = (obj) => ws.readyState === 1 && ws.send(JSON.stringify({ id: msg.id, ...obj }));
    try {
      if (msg.t === 'ping') return reply({ t: 'pong' });
      const tv = getTv(msg.tv);
      if (msg.t === 'voice') {
        const r = await runVoice(tv, String(msg.text || '').slice(0, 300));
        return reply({ ok: !r.unknown, say: r.say });
      }
      if (msg.t === 'action') {
        await runAction(tv, msg.action || {});
        return reply({ ok: true });
      }
      reply({ ok: false, error: 'Неизвестное сообщение' });
    } catch (e) {
      reply({ ok: false, error: e.message });
    }
  });
});

setInterval(() => {
  for (const ws of wss.clients) {
    if (!ws.isAlive) {
      ws.terminate();
      continue;
    }
    ws.isAlive = false;
    ws.ping();
  }
}, 30000).unref();

function onUpgrade(req, socket, head) {
  const url = new URL(req.url, 'http://local');
  if (url.pathname !== '/ws' || !isAuthed(req)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n');
    return socket.destroy();
  }
  wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
}

// ---------- HTTPS-сертификат (самоподписанный) ----------
function loadOrCreateCert() {
  const certFile = path.join(DATA_DIR, 'cert.pem');
  const keyFile = path.join(DATA_DIR, 'key.pem');
  const metaFile = path.join(DATA_DIR, 'cert.json');
  const ips = localIPv4().map((i) => i.address);
  try {
    const meta = JSON.parse(fs.readFileSync(metaFile, 'utf8'));
    if (ips.every((ip) => meta.ips.includes(ip))) {
      return { cert: fs.readFileSync(certFile), key: fs.readFileSync(keyFile) };
    }
  } catch (_) {
    /* создадим новый */
  }
  const selfsigned = require('selfsigned');
  const altNames = [
    { type: 2, value: 'localhost' },
    { type: 7, ip: '127.0.0.1' },
    ...ips.map((ip) => ({ type: 7, ip })),
  ];
  const pems = selfsigned.generate([{ name: 'commonName', value: 'TV Remote' }], {
    days: 3650,
    keySize: 2048,
    algorithm: 'sha256',
    extensions: [
      { name: 'basicConstraints', cA: false },
      { name: 'keyUsage', digitalSignature: true, keyEncipherment: true },
      { name: 'extKeyUsage', serverAuth: true },
      { name: 'subjectAltName', altNames },
    ],
  });
  fs.writeFileSync(certFile, pems.cert);
  fs.writeFileSync(keyFile, pems.private, { mode: 0o600 });
  fs.writeFileSync(metaFile, JSON.stringify({ ips, created: new Date().toISOString() }));
  log('Создан самоподписанный HTTPS-сертификат для', ips.join(', ') || 'localhost');
  return { cert: pems.cert, key: pems.private };
}

// ---------- Запуск ----------
const httpServer = http.createServer(onRequest);
httpServer.on('upgrade', onUpgrade);
httpServer.listen(PORT, '0.0.0.0');

let httpsServer = null;
if (process.env.HTTPS !== '0') {
  try {
    httpsServer = https.createServer(loadOrCreateCert(), onRequest);
    httpsServer.on('upgrade', onUpgrade);
    httpsServer.listen(HTTPS_PORT, '0.0.0.0');
  } catch (e) {
    log('HTTPS отключён:', e.message);
    httpsServer = null;
  }
}

(async () => {
  const r = await adb.startServer();
  if (r.code !== 0) log(`ВНИМАНИЕ: adb не запускается (${ADB_PATH}). Установите android-tools / platform-tools.`, r.stderr);
  await pollAll();
  setInterval(pollAll, POLL_MS).unref();
})();

setTimeout(() => {
  const urls = serverUrls();
  log('Пульт запущен:');
  for (const u of [...urls.http, ...urls.https]) log('  ', u);
  if (ACCESS_PIN) log('   Доступ по PIN включён');
}, 300);

function shutdown() {
  for (const r of runtime.values()) r.session.dispose();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
