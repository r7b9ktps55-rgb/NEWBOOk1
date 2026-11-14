'use strict';

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { WebSocketServer } = require('ws');
const QRCode = require('qrcode');

const { LocalGateway, AgentGateway } = require('./lib/gateway');
const { resolveKey } = require('./lib/keys');
const { describeApp } = require('./lib/apps');
const { parseCommand } = require('./lib/voice');
const { localIPv4 } = require('./lib/net');

// ---------- Конфигурация ----------
const env = process.env;
const PORT = parseInt(env.PORT || '8080', 10);
const HTTPS_PORT = parseInt(env.HTTPS_PORT || '8443', 10);
const BIND = env.BIND || '0.0.0.0';
const DATA_DIR = path.resolve(env.DATA_DIR || path.join(__dirname, 'data'));
const PUBLIC_DIR = path.join(__dirname, 'public');
const ADB_PATH = env.ADB_PATH || 'adb';
const ACCESS_PIN = (env.ACCESS_PIN || '').trim();
const AGENT_TOKEN = (env.AGENT_TOKEN || '').trim();
const PUBLIC_URL = (env.PUBLIC_URL || '').trim().replace(/\/+$/, '');
const TRUST_PROXY = env.TRUST_PROXY === '1';
const POLL_MS = parseInt(env.POLL_MS || '10000', 10);

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

// В облаке пульт смотрит в интернет — без пароля не запускаемся.
if (PUBLIC_URL && !ACCESS_PIN && env.ALLOW_NO_PIN !== '1') {
  console.error('ОШИБКА: задан PUBLIC_URL (облачный режим), но не задан ACCESS_PIN.');
  console.error('Пульт будет доступен из интернета — задайте пароль в ACCESS_PIN (или ALLOW_NO_PIN=1, если понимаете риск).');
  process.exit(1);
}
if (ACCESS_PIN && PUBLIC_URL && ACCESS_PIN.length < 6) {
  log('ВНИМАНИЕ: пароль короче 6 символов — для облака лучше длиннее.');
}
if (AGENT_TOKEN && AGENT_TOKEN.length < 16) {
  console.error('ОШИБКА: AGENT_TOKEN должен быть не короче 16 символов.');
  process.exit(1);
}

fs.mkdirSync(DATA_DIR, { recursive: true });
const TVS_FILE = path.join(DATA_DIR, 'tvs.json');

// ---------- Шлюзы ----------
const local = new LocalGateway(ADB_PATH);
const agents = new Map(); // имя -> AgentGateway

function gatewayFor(tv) {
  if (!tv.agent) return local;
  const a = agents.get(tv.agent);
  if (!a || !a.online) throw httpError(503, `Домашний агент «${tv.agent}» не в сети. Проверьте устройство дома.`);
  return a;
}

function agentList() {
  const names = new Set([...agents.keys(), ...tvList.filter((t) => t.agent).map((t) => t.agent)]);
  return [...names].map((name) => {
    const a = agents.get(name);
    return { name, online: !!(a && a.online), version: a?.meta.version || '', platform: a?.meta.platform || '', ip: a?.meta.ip || '' };
  });
}

// ---------- Хранилище телевизоров ----------
const HOST_RE = /^[A-Za-z0-9.-]{1,253}$/;
const AGENT_NAME_RE = /^[\p{L}\p{N} _.-]{1,40}$/u;

function makeTv({ name, host, port, mac, agent }) {
  host = String(host || '').trim();
  let p = parseInt(port || '5555', 10);
  const m = host.match(/^(.+):(\d+)$/);
  if (m) {
    host = m[1];
    p = parseInt(m[2], 10);
  }
  if (!HOST_RE.test(host)) throw new Error('Неверный IP-адрес');
  if (!(p > 0 && p < 65536)) throw new Error('Неверный порт');
  agent = String(agent || '').trim();
  if (agent && !AGENT_NAME_RE.test(agent)) throw new Error('Неверное имя агента');
  return {
    id: crypto.randomBytes(4).toString('hex'),
    name: String(name || 'Телевизор').trim().slice(0, 40) || 'Телевизор',
    host,
    port: p,
    mac: String(mac || '').trim().slice(0, 17),
    agent,
  };
}

function loadTvs() {
  try {
    return JSON.parse(fs.readFileSync(TVS_FILE, 'utf8')).map((t) => ({ agent: '', mac: '', ...t }));
  } catch (_) {
    // Первичная настройка: TV_HOSTS="Зал=192.168.1.50,Кухня=192.168.1.51"
    return (env.TV_HOSTS || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s, i) => {
        const [name, host] = s.includes('=') ? s.split('=') : [`Телевизор ${i + 1}`, s];
        return makeTv({ name, host });
      });
  }
}

function saveTvs() {
  const data = tvList.map(({ id, name, host, port, mac, agent }) => ({ id, name, host, port, mac, agent }));
  fs.writeFileSync(TVS_FILE, JSON.stringify(data, null, 2));
}

let tvList = loadTvs();
const runtime = new Map(); // id -> { key, serial, agent, state, message, apps, appsAt }

function rt(tv) {
  const serial = `${tv.host}:${tv.port}`;
  const key = `${tv.agent}|${serial}`;
  let r = runtime.get(tv.id);
  if (!r || r.key !== key) {
    if (r) disposeRuntime(r);
    r = { key, serial, agent: tv.agent, state: 'unknown', message: '', apps: null, appsAt: 0 };
    runtime.set(tv.id, r);
  }
  return r;
}

function disposeRuntime(r) {
  const gw = r.agent ? agents.get(r.agent) : local;
  if (gw && gw.online) gw.dispose(r.serial);
}

function publicTv(tv) {
  const r = rt(tv);
  return { id: tv.id, name: tv.name, host: tv.host, port: tv.port, mac: tv.mac, agent: tv.agent, state: r.state, message: r.message };
}

function getTv(id) {
  const tv = tvList.find((t) => t.id === id) || (id ? null : tvList[0]);
  if (!tv) throw httpError(404, 'Телевизор не найден');
  return tv;
}

// ---------- Подключение и статус ----------
function setState(tv, state, message = '') {
  const r = rt(tv);
  if (r.state !== state) {
    log(`[${tv.name}] ${r.state} -> ${state}`);
    r.state = state;
    r.message = message;
    if (state !== 'device') {
      const gw = tv.agent ? agents.get(tv.agent) : local;
      if (gw && gw.online) gw.kill(r.serial);
    }
    broadcastStatus();
  }
}

async function connectTv(tv) {
  let gw;
  try {
    gw = gatewayFor(tv);
  } catch (e) {
    setState(tv, 'agent_offline', e.message);
    throw e;
  }
  const res = await gw.connect(rt(tv).serial);
  setState(tv, res.state, res.message);
  return res;
}

let polling = false;
async function pollAll() {
  if (polling) return;
  polling = true;
  try {
    const devCache = new Map();
    for (const tv of tvList) {
      let gw;
      try {
        gw = gatewayFor(tv);
      } catch (e) {
        setState(tv, 'agent_offline', e.message);
        continue;
      }
      try {
        if (!devCache.has(gw)) devCache.set(gw, await gw.devices());
        const st = devCache.get(gw)[rt(tv).serial];
        if (st === 'device') setState(tv, 'device');
        else await connectTv(tv);
      } catch (_) {
        setState(tv, 'offline');
      }
    }
  } finally {
    polling = false;
  }
}

async function ensureReady(tv) {
  const r = rt(tv);
  if (r.state !== 'device') await connectTv(tv);
  if (r.state === 'unauthorized') {
    throw httpError(409, 'Подтвердите отладку на экране телевизора («Разрешить» + «Всегда разрешать»)');
  }
  if (r.state !== 'device') throw httpError(503, 'Телевизор недоступен. Он включён и подключён к сети?');
  return { gw: gatewayFor(tv), serial: r.serial };
}

async function tvCall(tv, method, ...args) {
  const { gw, serial } = await ensureReady(tv);
  return gw.tv(serial, method, ...args);
}

async function getApps(tv, force = false) {
  const r = rt(tv);
  if (!force && r.apps && Date.now() - r.appsAt < 5 * 60 * 1000) return r.apps;
  const raw = await tvCall(tv, 'listApps');
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
  if (tv.mac) {
    try {
      await gatewayFor(tv).wake(tv.mac);
    } catch (_) {
      /* WoL из облака без агента невозможен — пробуем ADB */
    }
  }
  try {
    await tvCall(tv, 'key', 'KEYCODE_WAKEUP');
  } catch (e) {
    if (!tv.mac) throw e;
    setTimeout(() => connectTv(tv).catch(() => {}), 6000);
  }
}

async function runAction(tv, action) {
  switch (action.type) {
    case 'key': {
      if (action.key === 'wakeup') return wakeTv(tv);
      const code = resolveKey(action.key);
      if (!code) throw httpError(400, `Неизвестная кнопка: ${action.key}`);
      if (action.long) return tvCall(tv, 'key', code, { long: true });
      const n = Math.min(30, Math.max(1, parseInt(action.repeat || 1, 10)));
      return tvCall(tv, 'keys', Array(n).fill(code));
    }
    case 'text': {
      const res = await tvCall(tv, 'text', String(action.text || '').slice(0, 500));
      if (res && res.ok === false) throw httpError(422, res.error);
      return;
    }
    case 'launch':
      return tvCall(tv, 'launch', { pkg: action.pkg, component: action.component });
    case 'url': {
      const url = String(action.url || '');
      if (!/^https?:\/\//.test(url)) throw httpError(400, 'Неверная ссылка');
      return tvCall(tv, 'openUrl', url);
    }
    case 'search':
      return tvCall(tv, 'globalSearch', String(action.query || '').slice(0, 200));
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

// ---------- Авторизация ----------
const SECRET_FILE = path.join(DATA_DIR, 'secret');
let SECRET;
try {
  SECRET = fs.readFileSync(SECRET_FILE, 'utf8').trim();
} catch (_) {
  SECRET = crypto.randomBytes(32).toString('hex');
  fs.writeFileSync(SECRET_FILE, SECRET, { mode: 0o600 });
}
const AUTH_TOKEN = crypto.createHmac('sha256', SECRET).update(`pin:${ACCESS_PIN}`).digest('hex');

function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  return ba.length === bb.length && crypto.timingSafeEqual(ba, bb);
}

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
  return safeEqual(parseCookies(req).tvr_auth || '', AUTH_TOKEN);
}

function clientIp(req) {
  if (TRUST_PROXY && req.headers['x-forwarded-for']) return String(req.headers['x-forwarded-for']).split(',')[0].trim();
  return req.socket.remoteAddress || '?';
}

function isHttps(req) {
  return req.socket.encrypted || (TRUST_PROXY && req.headers['x-forwarded-proto'] === 'https');
}

// Защита от подбора пароля: 8 ошибок — блокировка IP на 15 минут
const loginFails = new Map();
function loginBlocked(ip) {
  const f = loginFails.get(ip);
  return f && f.until > Date.now();
}
function loginFailed(ip) {
  const f = loginFails.get(ip) || { count: 0, until: 0 };
  f.count += 1;
  if (f.count >= 8) {
    f.until = Date.now() + 15 * 60 * 1000;
    f.count = 0;
    log(`Блокировка входа для ${ip} на 15 минут`);
  }
  loginFails.set(ip, f);
}
setInterval(() => {
  const now = Date.now();
  for (const [ip, f] of loginFails) if (f.until < now && f.count === 0) loginFails.delete(ip);
}, 60 * 60 * 1000).unref();

// ---------- HTTP ----------
function httpError(status, message) {
  const e = new Error(message);
  e.status = status;
  return e;
}

function sendJson(res, status, obj) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(obj));
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
  '.sh': 'text/plain; charset=utf-8',
};

// Архив с кодом агента: домашние устройства скачивают его прямо с этого сервера
const AGENT_TGZ = path.join(DATA_DIR, 'agent.tgz');
function buildAgentBundle() {
  require('child_process').execFile(
    'tar',
    ['czf', AGENT_TGZ, '-C', __dirname, 'agent.js', 'lib', 'package.json', 'package-lock.json'],
    (err) => err && log('Не удалось собрать agent.tgz:', err.message)
  );
}

function serveStatic(req, res, pathname) {
  if (pathname === '/') pathname = '/index.html';
  let file;
  try {
    file = path.normalize(path.join(PUBLIC_DIR, decodeURIComponent(pathname)));
  } catch (_) {
    return sendJson(res, 400, { error: 'bad path' });
  }
  if (!file.startsWith(PUBLIC_DIR + path.sep)) return sendJson(res, 403, { error: 'forbidden' });
  fs.readFile(file, (err, data) => {
    if (err) return sendJson(res, 404, { error: 'not found' });
    const ext = path.extname(file);
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Cache-Control': ['.html', '.js', '.css', '.webmanifest'].includes(ext) ? 'no-cache' : 'public, max-age=86400',
    });
    res.end(data);
  });
}

function serverUrls() {
  if (PUBLIC_URL) return { http: [], https: [PUBLIC_URL] };
  const ips = localIPv4().map((i) => i.address);
  return {
    http: ips.map((ip) => `http://${ip}:${PORT}`),
    https: httpsServer ? ips.map((ip) => `https://${ip}:${HTTPS_PORT}`) : [],
  };
}

function requestOrigin(req) {
  if (PUBLIC_URL) return PUBLIC_URL;
  return `${isHttps(req) ? 'https' : 'http'}://${req.headers.host}`;
}

async function handleApi(req, res, url) {
  const p = url.pathname;
  const method = req.method;
  let m;

  if (p === '/api/login' && method === 'POST') {
    const ip = clientIp(req);
    if (loginBlocked(ip)) return sendJson(res, 429, { error: 'Слишком много попыток. Подождите 15 минут.' });
    const body = await readBody(req);
    if (!ACCESS_PIN || safeEqual(String(body.pin || ''), ACCESS_PIN)) {
      loginFails.delete(ip);
      const secure = isHttps(req) ? '; Secure' : '';
      res.setHeader('Set-Cookie', `tvr_auth=${AUTH_TOKEN}; Path=/; Max-Age=${60 * 60 * 24 * 365 * 2}; HttpOnly; SameSite=Strict${secure}`);
      return sendJson(res, 200, { ok: true });
    }
    loginFailed(ip);
    await new Promise((r) => setTimeout(r, 800));
    return sendJson(res, 401, { error: 'Неверный пароль' });
  }

  if (p === '/api/info' && method === 'GET') {
    return sendJson(res, 200, {
      needPin: !!ACCESS_PIN,
      authed: isAuthed(req),
      cloud: !!PUBLIC_URL,
      agents: !!AGENT_TOKEN,
      urls: isAuthed(req) ? serverUrls() : { http: [], https: [] },
    });
  }

  if (!isAuthed(req)) return sendJson(res, 401, { error: 'Нужен пароль', needPin: true });

  if (p === '/api/logout' && method === 'POST') {
    res.setHeader('Set-Cookie', 'tvr_auth=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict');
    return sendJson(res, 200, { ok: true });
  }

  if (p === '/api/qr' && method === 'GET') {
    const text = String(url.searchParams.get('text') || '').slice(0, 300);
    const svg = await QRCode.toString(text || 'empty', { type: 'svg', margin: 1, errorCorrectionLevel: 'M' });
    res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-store' });
    return res.end(svg);
  }

  if (p === '/api/tvs' && method === 'GET') return sendJson(res, 200, { tvs: tvList.map(publicTv), agents: agentList() });

  if (p === '/api/agent-setup' && method === 'GET') {
    return sendJson(res, 200, { enabled: !!AGENT_TOKEN, url: requestOrigin(req), token: AGENT_TOKEN });
  }

  if (p === '/api/tvs' && method === 'POST') {
    const body = await readBody(req);
    let tv;
    try {
      tv = makeTv(body);
    } catch (e) {
      throw httpError(400, e.message);
    }
    if (tvList.some((t) => t.host === tv.host && t.port === tv.port && t.agent === tv.agent)) throw httpError(409, 'Этот ТВ уже добавлен');
    tvList.push(tv);
    saveTvs();
    connectTv(tv).catch(() => {});
    broadcastStatus();
    return sendJson(res, 201, { tv: publicTv(tv) });
  }

  if (p === '/api/discover' && method === 'GET') {
    const via = String(url.searchParams.get('via') || '');
    const gw = via ? agents.get(via) : local;
    if (!gw || !gw.online) throw httpError(503, `Агент «${via}» не в сети`);
    const known = new Set(tvList.filter((t) => t.agent === via).map((t) => t.host));
    const hosts = await gw.discover();
    return sendJson(res, 200, { hosts: hosts.map((h) => ({ host: h, known: known.has(h) })) });
  }

  if ((m = p.match(/^\/api\/tvs\/([a-f0-9]+)(\/[a-z]+)?$/))) {
    const tv = getTv(m[1]);
    const sub = m[2] || '';

    if (sub === '' && method === 'PUT') {
      const body = await readBody(req);
      let upd;
      try {
        upd = makeTv({
          name: body.name ?? tv.name,
          host: body.host ?? tv.host,
          port: body.port ?? tv.port,
          mac: body.mac ?? tv.mac,
          agent: body.agent ?? tv.agent,
        });
      } catch (e) {
        throw httpError(400, e.message);
      }
      Object.assign(tv, { name: upd.name, host: upd.host, port: upd.port, mac: upd.mac, agent: upd.agent });
      saveTvs();
      connectTv(tv).catch(() => {});
      broadcastStatus();
      return sendJson(res, 200, { tv: publicTv(tv) });
    }
    if (sub === '' && method === 'DELETE') {
      tvList = tvList.filter((t) => t.id !== tv.id);
      const r = runtime.get(tv.id);
      if (r) {
        const gw = r.agent ? agents.get(r.agent) : local;
        if (gw && gw.online) gw.disconnect(r.serial).catch(() => {});
        runtime.delete(tv.id);
      }
      saveTvs();
      broadcastStatus();
      return sendJson(res, 200, { ok: true });
    }
    if (sub === '/connect' && method === 'POST') {
      const gw = gatewayFor(tv);
      await gw.disconnect(rt(tv).serial).catch(() => {});
      rt(tv).state = 'unknown';
      const result = await connectTv(tv);
      return sendJson(res, 200, { tv: publicTv(tv), message: result.message });
    }
    if (sub === '/pair' && method === 'POST') {
      // Android 11+: Настройки → Для разработчиков → Отладка по Wi-Fi → Подключить по коду
      const body = await readBody(req);
      const pport = parseInt(body.port, 10);
      const code = String(body.code || '').replace(/\D/g, '');
      if (!(pport > 0 && pport < 65536) || code.length !== 6) throw httpError(400, 'Нужны порт и 6-значный код');
      const result = await gatewayFor(tv).pair(`${tv.host}:${pport}`, code);
      const cport = parseInt(body.connectPort, 10);
      if (cport > 0 && cport < 65536 && cport !== tv.port) {
        tv.port = cport;
        saveTvs();
      }
      if (result.ok) await connectTv(tv).catch(() => {});
      return sendJson(res, result.ok ? 200 : 400, { ok: result.ok, message: result.message, tv: publicTv(tv) });
    }
    if (sub === '/apps' && method === 'GET') {
      const apps = await getApps(tv, url.searchParams.has('refresh'));
      return sendJson(res, 200, { apps: apps.map(({ pkg, component, name, color }) => ({ pkg, component, name, color })) });
    }
    if (sub === '/screenshot' && method === 'GET') {
      const png = await tvCall(tv, 'screenshot');
      res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' });
      return res.end(png);
    }
    if (sub === '/action' && method === 'POST') {
      await runAction(tv, await readBody(req));
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
  let url;
  try {
    url = new URL(req.url, 'http://local');
  } catch (_) {
    return sendJson(res, 400, { error: 'bad url' });
  }
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('X-Frame-Options', 'DENY');
  if (isHttps(req) && PUBLIC_URL) res.setHeader('Strict-Transport-Security', 'max-age=15552000');
  try {
    if (url.pathname === '/healthz') return sendJson(res, 200, { ok: true });
    if (url.pathname === '/agent.tgz') {
      return fs.readFile(AGENT_TGZ, (err, data) => {
        if (err) return sendJson(res, 404, { error: 'agent bundle not ready' });
        res.writeHead(200, { 'Content-Type': 'application/gzip', 'Cache-Control': 'no-cache' });
        res.end(data);
      });
    }
    if (url.pathname.startsWith('/api/')) return await handleApi(req, res, url);
    if (req.method !== 'GET' && req.method !== 'HEAD') return sendJson(res, 405, { error: 'method' });
    return serveStatic(req, res, url.pathname);
  } catch (e) {
    if (!e.status) log('error', e.stack || e.message);
    if (!res.headersSent) sendJson(res, e.status || 500, { error: e.message || 'Ошибка' });
  }
}

// ---------- WebSocket: телефоны ----------
const wss = new WebSocketServer({ noServer: true, maxPayload: 16 * 1024 });

function broadcastStatus() {
  const msg = JSON.stringify({ t: 'status', tvs: tvList.map(publicTv), agents: agentList() });
  for (const c of wss.clients) if (c.readyState === 1) c.send(msg);
}

wss.on('connection', (ws) => {
  ws.isAlive = true;
  ws.on('pong', () => (ws.isAlive = true));
  ws.send(JSON.stringify({ t: 'status', tvs: tvList.map(publicTv), agents: agentList() }));
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

// ---------- WebSocket: домашние агенты ----------
const agentWss = new WebSocketServer({ noServer: true, maxPayload: 32 * 1024 * 1024 });

agentWss.on('connection', (ws, req, name) => {
  ws.isAlive = true;
  ws.on('pong', () => (ws.isAlive = true));
  const old = agents.get(name);
  if (old) old.close();
  const gw = new AgentGateway(name, ws, { ip: clientIp(req) });
  agents.set(name, gw);
  log(`Агент «${name}» подключился (${clientIp(req)})`);
  gw.on('close', () => {
    if (agents.get(name) === gw) {
      log(`Агент «${name}» отключился`);
      for (const tv of tvList) if (tv.agent === name) setState(tv, 'agent_offline', 'Домашний агент не в сети');
      broadcastStatus();
    }
  });
  gw.on('hello', broadcastStatus);
  broadcastStatus();
  setTimeout(pollAll, 300);
});

setInterval(() => {
  for (const set of [wss.clients, agentWss.clients]) {
    for (const ws of set) {
      if (!ws.isAlive) {
        ws.terminate();
        continue;
      }
      ws.isAlive = false;
      ws.ping();
    }
  }
}, 30000).unref();

function rejectUpgrade(socket, code, text) {
  socket.write(`HTTP/1.1 ${code} ${text}\r\nConnection: close\r\n\r\n`);
  socket.destroy();
}

function onUpgrade(req, socket, head) {
  let url;
  try {
    url = new URL(req.url, 'http://local');
  } catch (_) {
    return rejectUpgrade(socket, 400, 'Bad Request');
  }

  if (url.pathname === '/agent') {
    if (!AGENT_TOKEN) return rejectUpgrade(socket, 404, 'Not Found');
    const auth = String(req.headers.authorization || '');
    const token = auth.startsWith('Bearer ') ? auth.slice(7).trim() : '';
    if (!safeEqual(token, AGENT_TOKEN)) {
      log(`Отклонён агент с неверным токеном (${clientIp(req)})`);
      return rejectUpgrade(socket, 401, 'Unauthorized');
    }
    const name = String(url.searchParams.get('name') || 'Дом').trim().slice(0, 40);
    if (!AGENT_NAME_RE.test(name)) return rejectUpgrade(socket, 400, 'Bad Request');
    return agentWss.handleUpgrade(req, socket, head, (ws) => agentWss.emit('connection', ws, req, name));
  }

  if (url.pathname === '/ws') {
    if (!isAuthed(req)) return rejectUpgrade(socket, 401, 'Unauthorized');
    // Защита от подключения с чужих сайтов
    const origin = req.headers.origin;
    if (origin) {
      let ok = false;
      try {
        ok = new URL(origin).host === req.headers.host;
      } catch (_) {
        /* ok = false */
      }
      if (!ok) return rejectUpgrade(socket, 403, 'Forbidden');
    }
    return wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req));
  }

  rejectUpgrade(socket, 404, 'Not Found');
}

// ---------- HTTPS-сертификат (самоподписанный, для домашнего режима) ----------
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
  const altNames = [{ type: 2, value: 'localhost' }, { type: 7, ip: '127.0.0.1' }, ...ips.map((ip) => ({ type: 7, ip }))];
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
httpServer.listen(PORT, BIND);

let httpsServer = null;
if (env.HTTPS !== '0' && !PUBLIC_URL) {
  try {
    httpsServer = https.createServer(loadOrCreateCert(), onRequest);
    httpsServer.on('upgrade', onUpgrade);
    httpsServer.listen(HTTPS_PORT, BIND);
  } catch (e) {
    log('HTTPS отключён:', e.message);
    httpsServer = null;
  }
}

buildAgentBundle();

(async () => {
  const r = await local.start();
  if (r.code !== 0) log(`Локальный adb недоступен (${ADB_PATH}) — работают только домашние агенты.`);
  await pollAll();
  setInterval(pollAll, POLL_MS).unref();
})();

setTimeout(() => {
  const urls = serverUrls();
  log(PUBLIC_URL ? 'Пульт запущен в облачном режиме:' : 'Пульт запущен:');
  for (const u of [...urls.http, ...urls.https]) log('  ', u);
  if (PUBLIC_URL) log(`   (сервер слушает ${BIND}:${PORT}, снаружи — через HTTPS-прокси)`);
  if (ACCESS_PIN) log('   Вход по паролю включён');
  log(AGENT_TOKEN ? '   Домашние агенты: включены' : '   Домашние агенты: выключены (задайте AGENT_TOKEN)');
}, 300);

function shutdown() {
  local.disposeAll();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
