'use strict';
// Интеграционный тест облачного режима: сервер + домашний агент + имитация adb.
const test = require('node:test');
const assert = require('node:assert');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const WebSocket = require('ws');

const ROOT = path.join(__dirname, '..');
const FAKE = path.join(ROOT, 'tools/fake-adb.js');
const PORT = 18000 + Math.floor(Math.random() * 1000);
const BASE = `http://127.0.0.1:${PORT}`;
const TOKEN = 'test-agent-token-1234567890';
const PIN = 'secret-pass';
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tvr-cloud-'));
const LOG = path.join(tmp, 'adb.log');

const procs = [];
function start(script, env) {
  const p = spawn(process.execPath, [path.join(ROOT, script)], {
    env: { ...process.env, ADB_PATH: FAKE, FAKE_ADB_LOG: LOG, ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  p.out = '';
  p.stdout.on('data', (d) => (p.out += d));
  p.stderr.on('data', (d) => (p.out += d));
  procs.push(p);
  return p;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(fn, ms = 8000) {
  const t0 = Date.now();
  for (;;) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (_) {
      /* retry */
    }
    if (Date.now() - t0 > ms) throw new Error('timeout');
    await sleep(150);
  }
}

let cookie = '';
async function api(p, opts = {}) {
  const res = await fetch(BASE + p, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json', Cookie: cookie, ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const ct = res.headers.get('content-type') || '';
  return { status: res.status, headers: res.headers, data: ct.includes('json') ? await res.json() : Buffer.from(await res.arrayBuffer()) };
}

const cloudEnv = {
  PORT: String(PORT), BIND: '127.0.0.1', HTTPS: '0', PUBLIC_URL: 'https://tv.example.test',
  ACCESS_PIN: PIN, AGENT_TOKEN: TOKEN, DATA_DIR: path.join(tmp, 'data'), POLL_MS: '1000',
};

test.after(() => {
  for (const p of procs) p.kill();
});

test('облако не стартует без пароля', async () => {
  const p = start('server.js', { ...cloudEnv, ACCESS_PIN: '', PORT: String(PORT + 1) });
  const code = await new Promise((r) => p.on('exit', r));
  assert.strictEqual(code, 1);
  assert.match(p.out, /ACCESS_PIN/);
});

test('облако + агент: полный сценарий', async () => {
  start('server.js', cloudEnv);
  await waitFor(async () => (await api('/healthz')).status === 200);

  // без пароля API закрыто
  assert.strictEqual((await api('/api/tvs')).status, 401);
  assert.strictEqual((await api('/api/login', { method: 'POST', body: { pin: 'wrong' } })).status, 401);
  const login = await api('/api/login', { method: 'POST', body: { pin: PIN }, headers: { 'X-Forwarded-Proto': 'https' } });
  assert.strictEqual(login.status, 200);
  cookie = login.headers.get('set-cookie').split(';')[0];
  assert.match(login.headers.get('set-cookie'), /HttpOnly; SameSite=Strict/);

  // агент с неверным токеном отклоняется
  const bad = start('agent.js', { SERVER_URL: BASE, AGENT_TOKEN: 'wrong-token-wrong-token', AGENT_NAME: 'Чужой' });
  await waitFor(() => /отклонил токен/.test(bad.out));
  bad.kill();

  // правильный агент подключается
  const agent = start('agent.js', { SERVER_URL: BASE, AGENT_TOKEN: TOKEN, AGENT_NAME: 'Дом' });
  await waitFor(async () => (await api('/api/tvs')).data.agents.some((a) => a.name === 'Дом' && a.online));

  // добавляем ТВ через агента
  const add = await api('/api/tvs', { method: 'POST', body: { name: 'Зал', host: '192.168.1.77', agent: 'Дом' } });
  assert.strictEqual(add.status, 201);
  const id = add.data.tv.id;
  await waitFor(async () => (await api('/api/tvs')).data.tvs[0].state === 'device');

  // команды через WebSocket
  const ws = new WebSocket(`ws://127.0.0.1:${PORT}/ws`, { headers: { Cookie: cookie } });
  await new Promise((r, j) => { ws.once('open', r); ws.once('error', j); });
  const replies = new Map();
  ws.on('message', (m) => { const x = JSON.parse(m); if (x.id) replies.set(x.id, x); });
  const send = async (id2, payload) => { ws.send(JSON.stringify({ id: id2, ...payload })); return waitFor(() => replies.get(id2)); };
  assert.ok((await send(1, { t: 'action', tv: id, action: { type: 'key', key: 'home' } })).ok);
  assert.ok((await send(2, { t: 'action', tv: id, action: { type: 'key', key: 'vol_up', repeat: 3 } })).ok);
  const v = await send(3, { t: 'voice', tv: id, text: 'открой ютуб' });
  assert.ok(v.ok, JSON.stringify(v));
  ws.close();

  // скриншот и приложения через агента
  const shot = await api(`/api/tvs/${id}/screenshot`);
  assert.strictEqual(shot.status, 200);
  assert.strictEqual(shot.data.slice(1, 4).toString(), 'PNG');
  const apps = await api(`/api/tvs/${id}/apps`);
  assert.ok(apps.data.apps.length > 3);

  // WS с чужого сайта отклоняется
  const evil = new WebSocket(`ws://127.0.0.1:${PORT}/ws`, { headers: { Cookie: cookie, Origin: 'https://evil.example' } });
  const evilRes = await new Promise((r) => { evil.once('open', () => r('open')); evil.once('error', () => r('rejected')); });
  assert.strictEqual(evilRes, 'rejected');

  // архив агента раздаётся
  const tgz = await api('/agent.tgz');
  assert.strictEqual(tgz.status, 200);
  assert.ok(tgz.data.length > 1000);

  // агент отключился -> статус agent_offline
  agent.kill();
  await waitFor(async () => (await api('/api/tvs')).data.tvs[0].state === 'agent_offline');
  const fail = await api(`/api/tvs/${id}/action`, { method: 'POST', body: { type: 'key', key: 'home' } });
  assert.strictEqual(fail.status, 503);

  const log = fs.readFileSync(LOG, 'utf8');
  assert.match(log, /\[192\.168\.1\.77:5555\] input keyevent KEYCODE_HOME/);
  assert.match(log, /KEYCODE_VOLUME_UP KEYCODE_VOLUME_UP KEYCODE_VOLUME_UP/);
  assert.match(log, /am start -n 'com\.google\.android\.youtube\.tv\//);
});
