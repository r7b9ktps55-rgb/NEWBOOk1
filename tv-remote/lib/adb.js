'use strict';

const { execFile, spawn } = require('child_process');
const { EventEmitter } = require('events');

/** Безопасное экранирование строки для sh на телевизоре. */
function sq(str) {
  return `'${String(str).replace(/'/g, `'\\''`)}'`;
}

const COMPONENT_RE = /^[A-Za-z0-9_.]+\/[A-Za-z0-9_.$]+$/;
const PACKAGE_RE = /^[A-Za-z0-9_.]+$/;

class Adb {
  constructor(adbPath = 'adb') {
    this.adbPath = adbPath;
  }

  run(args, { timeout = 10000, encoding = 'utf8' } = {}) {
    return new Promise((resolve) => {
      execFile(
        this.adbPath,
        args,
        { timeout, encoding, maxBuffer: 32 * 1024 * 1024 },
        (err, stdout, stderr) => {
          resolve({
            code: err ? (typeof err.code === 'number' ? err.code : 1) : 0,
            stdout,
            stderr: stderr ? stderr.toString() : '',
            error: err,
          });
        }
      );
    });
  }

  async startServer() {
    return this.run(['start-server'], { timeout: 15000 });
  }

  /** Возвращает Map serial -> state (device | unauthorized | offline ...). */
  async devices() {
    const res = await this.run(['devices']);
    const map = new Map();
    for (const line of String(res.stdout).split('\n').slice(1)) {
      const m = line.trim().match(/^(\S+)\s+(\S+)/);
      if (m) map.set(m[1], m[2]);
    }
    return map;
  }

  async connect(serial) {
    const res = await this.run(['connect', serial], { timeout: 8000 });
    const out = `${res.stdout}${res.stderr}`.trim();
    let state = 'offline';
    if (/connected to/i.test(out) && !/failed|cannot|unable/i.test(out)) state = 'device';
    if (/authenticate|unauthorized/i.test(out)) state = 'unauthorized';
    if (state !== 'offline') {
      const devs = await this.devices();
      state = devs.get(serial) || state;
    }
    return { state, message: out };
  }

  async disconnect(serial) {
    return this.run(['disconnect', serial]);
  }

  async pair(hostPort, code) {
    const res = await this.run(['pair', hostPort, String(code)], { timeout: 20000 });
    const out = `${res.stdout}${res.stderr}`.trim();
    return { ok: /successfully paired/i.test(out), message: out };
  }
}

/**
 * Сессия с одним телевизором: одна постоянная `adb shell`, очередь команд,
 * склейка нажатий клавиш в один вызов `input keyevent`.
 */
class TvSession extends EventEmitter {
  constructor(adb, serial) {
    super();
    this.adb = adb;
    this.serial = serial;
    this.proc = null;
    this.buf = '';
    this.queue = [];
    this.current = null;
    this.seq = 0;
    this.caps = {};
  }

  _spawn() {
    if (this.proc) return;
    const proc = spawn(this.adb.adbPath, ['-s', this.serial, 'shell'], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.proc = proc;
    this.buf = '';
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', (d) => this._onData(d));
    proc.stderr.on('data', () => {});
    proc.stdin.on('error', () => {});
    const onExit = () => {
      if (this.proc !== proc) return;
      this.proc = null;
      if (this.current) {
        clearTimeout(this.current.timer);
        this.current.reject(new Error('Соединение с ТВ потеряно'));
        this.current = null;
      }
      this.emit('closed');
      setImmediate(() => this._pump());
    };
    proc.on('exit', onExit);
    proc.on('error', onExit);
  }

  _onData(chunk) {
    this.buf += chunk;
    if (!this.current) return;
    const marker = `__TVR_${this.current.id}__`;
    const idx = this.buf.indexOf(marker);
    if (idx === -1) return;
    const out = this.buf.slice(0, idx);
    const rest = this.buf.slice(idx + marker.length);
    this.buf = rest.replace(/^[^\n]*\n?/, '');
    const job = this.current;
    this.current = null;
    clearTimeout(job.timer);
    job.resolve(out.replace(/\r/g, ''));
    this._pump();
  }

  _pump() {
    if (this.current || this.queue.length === 0) return;
    this._spawn();
    const job = this.queue.shift();
    if (job.kind === 'keys') {
      const longFlag = job.long ? '--longpress ' : '';
      job.cmd = `input keyevent ${longFlag}${job.codes.join(' ')}`;
    }
    job.id = ++this.seq;
    this.current = job;
    job.timer = setTimeout(() => {
      if (this.current !== job) return;
      this.current = null;
      job.reject(new Error('ТВ не ответил вовремя'));
      this.kill();
      this._pump();
    }, job.timeout || 15000);
    this.proc.stdin.write(`${job.cmd} </dev/null 2>&1; echo __TVR_${job.id}__\n`);
  }

  _enqueue(job) {
    return new Promise((resolve, reject) => {
      job.resolve = resolve;
      job.reject = reject;
      this.queue.push(job);
      this._pump();
    });
  }

  /** Произвольная shell-команда на ТВ. Возвращает stdout. */
  shell(cmd, timeout) {
    return this._enqueue({ kind: 'cmd', cmd, timeout });
  }

  /** Нажатие клавиши. Подряд идущие короткие нажатия склеиваются. */
  key(code, { long = false } = {}) {
    if (!long) {
      const last = this.queue[this.queue.length - 1];
      if (last && last.kind === 'keys' && !last.long && last.codes.length < 25) {
        last.codes.push(code);
        return last.promise;
      }
    }
    const job = { kind: 'keys', codes: [code], long };
    job.promise = this._enqueue(job);
    return job.promise;
  }

  async keys(codes) {
    let p;
    for (const c of codes) p = this.key(c);
    return p;
  }

  async hasAdbKeyboard() {
    if (this.caps.adbKeyboard === undefined) {
      const out = await this.shell('pm list packages com.android.adbkeyboard');
      this.caps.adbKeyboard = /com\.android\.adbkeyboard/.test(out);
    }
    return this.caps.adbKeyboard;
  }

  /** Ввод текста в активное поле на ТВ. */
  async text(str) {
    str = String(str || '');
    if (!str) return { ok: true };
    const ascii = /^[\x20-\x7e]*$/.test(str);
    if (ascii) {
      // input text: пробел нужно передавать как %s
      await this.shell(`input text ${sq(str.split(' ').join('%s'))}`);
      return { ok: true };
    }
    if (await this.hasAdbKeyboard()) {
      const b64 = Buffer.from(str, 'utf8').toString('base64');
      const ime = 'com.android.adbkeyboard/.AdbIME';
      await this.shell(
        `prev=$(settings get secure default_input_method); ime enable ${ime} >/dev/null; ime set ${ime} >/dev/null; ` +
          `sleep 0.3; am broadcast -a ADB_INPUT_B64 --es msg ${sq(b64)} >/dev/null; sleep 0.2; ` +
          `[ -n "$prev" ] && [ "$prev" != "null" ] && ime set "$prev" >/dev/null`
      );
      return { ok: true };
    }
    // Без ADBKeyboard — отправим только латиницу/цифры, остальное потеряется
    const fallback = str.replace(/[^\x20-\x7e]/g, '');
    if (fallback.trim()) await this.text(fallback);
    return {
      ok: false,
      error:
        'Кириллица требует приложения ADBKeyboard на ТВ (см. инструкцию). Отправлены только латинские символы.',
    };
  }

  async listApps() {
    const parse = (out) => {
      const comps = new Map();
      for (const line of out.split('\n')) {
        const m = line.trim().match(/^([A-Za-z0-9_.]+)\/([A-Za-z0-9_.$]+)$/);
        if (!m) continue;
        const pkg = m[1];
        const cls = m[2].startsWith('.') ? pkg + m[2] : m[2];
        if (!comps.has(pkg)) comps.set(pkg, `${pkg}/${cls}`);
      }
      return comps;
    };
    let comps = parse(
      await this.shell(
        'cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LEANBACK_LAUNCHER'
      )
    );
    if (comps.size === 0) {
      comps = parse(
        await this.shell(
          'cmd package query-activities --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER'
        )
      );
    }
    if (comps.size === 0) {
      const out = await this.shell('pm list packages -3');
      for (const line of out.split('\n')) {
        const m = line.trim().match(/^package:([A-Za-z0-9_.]+)$/);
        if (m) comps.set(m[1], null);
      }
    }
    return [...comps.entries()].map(([pkg, component]) => ({ pkg, component }));
  }

  async launch({ pkg, component }) {
    if (component && COMPONENT_RE.test(component)) {
      return this.shell(`am start -n ${sq(component)}`);
    }
    if (pkg && PACKAGE_RE.test(pkg)) {
      return this.shell(
        `monkey -p ${sq(pkg)} -c android.intent.category.LEANBACK_LAUNCHER 1 || monkey -p ${sq(pkg)} -c android.intent.category.LAUNCHER 1`
      );
    }
    throw new Error('Некорректное имя приложения');
  }

  openUrl(url) {
    return this.shell(`am start -a android.intent.action.VIEW -d ${sq(url)}`);
  }

  globalSearch(query) {
    return this.shell(
      `am start -a android.search.action.GLOBAL_SEARCH --es query ${sq(query)} || am start -a android.intent.action.WEB_SEARCH --es query ${sq(query)}`
    );
  }

  async screenshot() {
    const res = await this.adb.run(['-s', this.serial, 'exec-out', 'screencap', '-p'], {
      timeout: 15000,
      encoding: 'buffer',
    });
    if (res.code !== 0 || !res.stdout || res.stdout.length < 100) {
      throw new Error('Не удалось сделать скриншот');
    }
    return res.stdout;
  }

  kill() {
    if (this.proc) {
      const p = this.proc;
      this.proc = null;
      try {
        p.kill();
      } catch (_) {
        /* ignore */
      }
    }
  }

  dispose() {
    this.kill();
    for (const job of this.queue) job.reject(new Error('Сессия закрыта'));
    this.queue = [];
  }
}

module.exports = { Adb, TvSession, sq };
