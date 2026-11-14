#!/usr/bin/env node
'use strict';

// Домашний агент: запускается дома (старый телефон в Termux, Raspberry Pi, ПК),
// сам подключается к облачному серверу пульта и выполняет команды на ТВ через adb.
// Проброс портов на роутере НЕ нужен — соединение исходящее.
//
//   SERVER_URL=https://tv.example.com AGENT_TOKEN=xxxx AGENT_NAME=Дом node agent.js

const os = require('os');
const WebSocket = require('ws');
const { LocalGateway, handleRpc } = require('./lib/gateway');

const SERVER_URL = (process.env.SERVER_URL || '').trim().replace(/\/+$/, '');
const AGENT_TOKEN = (process.env.AGENT_TOKEN || '').trim();
const AGENT_NAME = (process.env.AGENT_NAME || os.hostname() || 'Дом').trim().slice(0, 40);
const ADB_PATH = process.env.ADB_PATH || 'adb';
const VERSION = require('./package.json').version;

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

if (!SERVER_URL || !AGENT_TOKEN) {
  console.error('Нужно задать SERVER_URL и AGENT_TOKEN. Пример:');
  console.error('  SERVER_URL=https://tv.example.com AGENT_TOKEN=xxxx AGENT_NAME=Дом node agent.js');
  process.exit(1);
}

const wsUrl = `${SERVER_URL.replace(/^http/, 'ws')}/agent?name=${encodeURIComponent(AGENT_NAME)}`;
const gw = new LocalGateway(ADB_PATH);
let backoff = 1000;
let ws = null;

function connect() {
  ws = new WebSocket(wsUrl, {
    headers: { Authorization: `Bearer ${AGENT_TOKEN}` },
    maxPayload: 32 * 1024 * 1024,
    handshakeTimeout: 15000,
  });
  let alive = true;
  let pingTimer = null;

  ws.on('open', () => {
    backoff = 1000;
    log(`Подключён к ${SERVER_URL} как «${AGENT_NAME}»`);
    ws.send(JSON.stringify({ t: 'hello', version: VERSION, platform: `${os.platform()}/${os.arch()}` }));
    pingTimer = setInterval(() => {
      if (!alive) return ws.terminate();
      alive = false;
      ws.ping();
    }, 25000);
  });
  ws.on('pong', () => (alive = true));
  ws.on('ping', () => (alive = true));

  ws.on('message', async (raw) => {
    alive = true;
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch (_) {
      return;
    }
    if (!msg || typeof msg.id !== 'number') return;
    try {
      const result = await handleRpc(gw, msg.op, msg.args);
      if (msg.op === 'tv' && msg.args) log(`→ ${msg.args.serial} ${msg.args.method}`);
      ws.readyState === 1 && ws.send(JSON.stringify({ id: msg.id, ok: true, result: result === undefined ? null : result }));
    } catch (e) {
      ws.readyState === 1 && ws.send(JSON.stringify({ id: msg.id, ok: false, error: e.message }));
    }
  });

  ws.on('unexpected-response', (_req, res) => {
    if (res.statusCode === 401 || res.statusCode === 403) log('Сервер отклонил токен агента (AGENT_TOKEN). Проверьте его.');
    else log(`Сервер ответил ${res.statusCode}`);
  });
  ws.on('error', (e) => log('Ошибка соединения:', e.message));
  ws.on('close', () => {
    clearInterval(pingTimer);
    log(`Отключён. Переподключение через ${Math.round(backoff / 1000)} с…`);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 30000);
  });
}

(async () => {
  const r = await gw.start();
  if (r.code !== 0) log(`ВНИМАНИЕ: adb не запускается (${ADB_PATH}). Установите adb / android-tools.`);
  connect();
})();

function shutdown() {
  gw.disposeAll();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
