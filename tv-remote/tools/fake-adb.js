#!/usr/bin/env node
'use strict';
// Имитация adb для разработки без телевизора: `npm run dev`.
// Все команды, «отправленные на ТВ», печатаются в stderr сервера и в FAKE_ADB_LOG.
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const readline = require('readline');

const STATE = path.join(os.tmpdir(), 'fake-adb-state.json');
const LOG = process.env.FAKE_ADB_LOG || path.join(os.tmpdir(), 'fake-adb.log');
const load = () => { try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch (_) { return {}; } };
const save = (s) => fs.writeFileSync(STATE, JSON.stringify(s));
const logCmd = (serial, cmd) => fs.appendFileSync(LOG, `${new Date().toISOString()} [${serial}] ${cmd}\n`);

let args = process.argv.slice(2);
let serial = null;
if (args[0] === '-s') { serial = args[1]; args = args.slice(2); }

const APPS = [
  'com.google.android.youtube.tv/com.google.android.apps.youtube.tv.activity.ShellActivity',
  'ru.kinopoisk.tv/.MainActivity',
  'com.netflix.ninja/.MainActivity',
  'ru.ivi.client/.TvActivity',
  'org.videolan.vlc/.StartActivity',
  'com.spotify.tv.android/.SpotifyTVActivity',
  'com.android.tv.settings/.MainSettings',
  'com.mitv.tvhome.atv/.HomeActivity',
];

function fakePng() {
  const w = 480, h = 270;
  const raw = Buffer.alloc((w * 3 + 1) * h);
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0;
    for (let x = 0; x < w; x++) {
      const o = y * (w * 3 + 1) + 1 + x * 3;
      raw[o] = 30 + (x * 120) / w; raw[o + 1] = 40 + (y * 100) / h; raw[o + 2] = 120;
    }
  }
  const crcTable = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
  const crc = (b) => { let c = 0xffffffff; for (const x of b) c = crcTable[(c ^ x) & 255] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0; };
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const td = Buffer.concat([Buffer.from(type), data]);
    const c = Buffer.alloc(4); c.writeUInt32BE(crc(td));
    return Buffer.concat([len, td, c]);
  };
  const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 2;
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw)), chunk('IEND', Buffer.alloc(0))]);
}

function respond(cmd) {
  if (/query-activities/.test(cmd) && /LEANBACK/.test(cmd)) return APPS.join('\n') + '\n';
  if (/pm list packages com\.android\.adbkeyboard/.test(cmd)) return 'package:com.android.adbkeyboard\n';
  return '';
}

const [cmd, ...rest] = args;
switch (cmd) {
  case 'start-server': process.exit(0);
  case 'devices': {
    const s = load();
    console.log('List of devices attached');
    for (const k of Object.keys(s)) console.log(`${k}\tdevice`);
    process.exit(0);
  }
  case 'connect': {
    const s = load(); s[rest[0]] = 'device'; save(s);
    console.log(`connected to ${rest[0]}`); process.exit(0);
  }
  case 'disconnect': {
    const s = load(); delete s[rest[0]]; save(s);
    console.log(`disconnected ${rest[0]}`); process.exit(0);
  }
  case 'pair': console.log(`Successfully paired to ${rest[0]}`); process.exit(0);
  case 'exec-out': logCmd(serial, rest.join(' ')); process.stdout.write(fakePng()); break;
  case 'shell': {
    if (rest.length) { logCmd(serial, rest.join(' ')); process.stdout.write(respond(rest.join(' '))); break; }
    const rl = readline.createInterface({ input: process.stdin });
    rl.on('line', (line) => {
      const m = line.match(/^(.*) <\/dev\/null 2>&1; echo (__TVR_\d+__)$/);
      if (!m) return;
      logCmd(serial, m[1]);
      process.stderr.write(`[fake-adb ${serial}] ${m[1]}\n`);
      setTimeout(() => process.stdout.write(respond(m[1]) + m[2] + '\n'), 40);
    });
    break;
  }
  default: console.error('fake-adb: unsupported', args.join(' ')); process.exit(1);
}
