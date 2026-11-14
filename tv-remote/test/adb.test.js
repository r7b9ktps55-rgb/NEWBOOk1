'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { Adb, TvSession, sq } = require('../lib/adb');

const LOG = path.join(os.tmpdir(), `fake-adb-test-${process.pid}.log`);
process.env.FAKE_ADB_LOG = LOG;
const adb = new Adb(path.join(__dirname, '../tools/fake-adb.js'));

test('sq экранирует кавычки', () => {
  assert.strictEqual(sq("it's"), `'it'\\''s'`);
  assert.strictEqual(sq('a; rm -rf /'), `'a; rm -rf /'`);
});

test('connect + сессия + склейка нажатий', async () => {
  const c = await adb.connect('10.0.0.9:5555');
  assert.strictEqual(c.state, 'device');
  const s = new TvSession(adb, '10.0.0.9:5555');
  await Promise.all([s.key('KEYCODE_HOME'), s.key('KEYCODE_DPAD_UP'), s.key('KEYCODE_DPAD_UP'), s.key('KEYCODE_DPAD_DOWN')]);
  await s.text('hello world');
  await s.text('Привет');
  const apps = await s.listApps();
  assert.ok(apps.find((a) => a.pkg === 'com.google.android.youtube.tv'));
  assert.strictEqual(apps.find((a) => a.pkg === 'ru.kinopoisk.tv').component, 'ru.kinopoisk.tv/ru.kinopoisk.tv.MainActivity');
  await s.launch({ component: 'ru.kinopoisk.tv/ru.kinopoisk.tv.MainActivity' });
  await assert.rejects(() => s.launch({ component: 'x; reboot' }));
  const png = await s.screenshot();
  assert.strictEqual(png.slice(1, 4).toString(), 'PNG');
  s.dispose();
  const log = fs.readFileSync(LOG, 'utf8');
  assert.match(log, /input keyevent KEYCODE_HOME\n/);
  assert.match(log, /input keyevent KEYCODE_DPAD_UP KEYCODE_DPAD_UP KEYCODE_DPAD_DOWN/);
  assert.match(log, /input text 'hello%sworld'/);
  assert.ok(log.includes("ADB_INPUT_B64 --es msg '0J/RgNC40LLQtdGC'"));
  fs.unlinkSync(LOG);
});
