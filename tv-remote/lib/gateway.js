'use strict';

// «Шлюз» — то, через что сервер достаёт до телевизоров.
//  - LocalGateway: adb запущен прямо здесь (сервер дома или облако + Tailscale).
//  - AgentGateway: команды уходят по WebSocket домашнему агенту, а он выполняет их у себя.
// У обоих одинаковый асинхронный интерфейс.

const { EventEmitter } = require('events');
const { Adb, TvSession } = require('./adb');
const { discover, wakeOnLan } = require('./net');

// Методы сессии ТВ, которые можно вызывать удалённо (никакого произвольного shell!)
const TV_METHODS = new Set(['key', 'keys', 'text', 'listApps', 'launch', 'openUrl', 'globalSearch', 'screenshot']);

class LocalGateway {
  constructor(adbPath) {
    this.adb = new Adb(adbPath);
    this.sessions = new Map();
    this.online = true;
  }

  start() {
    return this.adb.startServer();
  }

  async devices() {
    return Object.fromEntries(await this.adb.devices());
  }

  connect(serial) {
    return this.adb.connect(serial);
  }

  async disconnect(serial) {
    this.dispose(serial);
    await this.adb.disconnect(serial);
  }

  pair(hostPort, code) {
    return this.adb.pair(hostPort, code);
  }

  _session(serial) {
    let s = this.sessions.get(serial);
    if (!s) {
      s = new TvSession(this.adb, serial);
      this.sessions.set(serial, s);
    }
    return s;
  }

  async tv(serial, method, ...args) {
    if (!TV_METHODS.has(method)) throw new Error(`Метод запрещён: ${method}`);
    return this._session(serial)[method](...args);
  }

  discover() {
    return discover(5555);
  }

  wake(mac) {
    return wakeOnLan(mac);
  }

  kill(serial) {
    const s = this.sessions.get(serial);
    if (s) s.kill();
  }

  dispose(serial) {
    const s = this.sessions.get(serial);
    if (s) {
      s.dispose();
      this.sessions.delete(serial);
    }
  }

  disposeAll() {
    for (const serial of [...this.sessions.keys()]) this.dispose(serial);
  }
}

/** Исполняет RPC-запрос агента на локальном шлюзе (используется в agent.js). */
async function handleRpc(gw, op, a = {}) {
  switch (op) {
    case 'ping':
      return 'pong';
    case 'devices':
      return gw.devices();
    case 'connect':
      return gw.connect(String(a.serial));
    case 'disconnect':
      return gw.disconnect(String(a.serial));
    case 'pair':
      return gw.pair(String(a.hostPort), String(a.code));
    case 'discover':
      return gw.discover();
    case 'wake':
      return gw.wake(String(a.mac || ''));
    case 'kill':
      return gw.kill(String(a.serial));
    case 'dispose':
      return gw.dispose(String(a.serial));
    case 'tv': {
      const r = await gw.tv(String(a.serial), String(a.method), ...(Array.isArray(a.args) ? a.args : []));
      return Buffer.isBuffer(r) ? { $b64: r.toString('base64') } : r;
    }
    default:
      throw new Error(`Неизвестная операция: ${op}`);
  }
}

class AgentGateway extends EventEmitter {
  constructor(name, ws, meta = {}) {
    super();
    this.name = name;
    this.ws = ws;
    this.meta = meta;
    this.online = true;
    this.seq = 0;
    this.pending = new Map();
    ws.on('message', (raw) => this._onMessage(raw));
    ws.on('close', () => this._closed());
    ws.on('error', () => {});
  }

  _onMessage(raw) {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch (_) {
      return;
    }
    if (msg.t === 'hello') {
      this.meta = { ...this.meta, version: msg.version, platform: msg.platform };
      this.emit('hello');
      return;
    }
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    clearTimeout(p.timer);
    if (msg.ok) {
      const r = msg.result;
      p.resolve(r && typeof r === 'object' && r.$b64 ? Buffer.from(r.$b64, 'base64') : r);
    } else p.reject(new Error(msg.error || 'Ошибка агента'));
  }

  _closed() {
    if (!this.online) return;
    this.online = false;
    for (const p of this.pending.values()) {
      clearTimeout(p.timer);
      p.reject(new Error(`Домашний агент «${this.name}» отключился`));
    }
    this.pending.clear();
    this.emit('close');
  }

  _call(op, args = {}, timeout = 20000) {
    if (!this.online || this.ws.readyState !== 1) {
      return Promise.reject(new Error(`Домашний агент «${this.name}» не в сети`));
    }
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Домашний агент «${this.name}» не ответил`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, op, args }));
    });
  }

  devices() {
    return this._call('devices');
  }
  connect(serial) {
    return this._call('connect', { serial }, 15000);
  }
  disconnect(serial) {
    return this._call('disconnect', { serial });
  }
  pair(hostPort, code) {
    return this._call('pair', { hostPort, code }, 30000);
  }
  tv(serial, method, ...args) {
    return this._call('tv', { serial, method, args }, method === 'screenshot' ? 30000 : 20000);
  }
  discover() {
    return this._call('discover', {}, 60000);
  }
  wake(mac) {
    return this._call('wake', { mac });
  }
  kill(serial) {
    this._call('kill', { serial }).catch(() => {});
  }
  dispose(serial) {
    this._call('dispose', { serial }).catch(() => {});
  }
  close() {
    try {
      this.ws.close();
    } catch (_) {
      /* ignore */
    }
  }
}

module.exports = { LocalGateway, AgentGateway, handleRpc, TV_METHODS };
