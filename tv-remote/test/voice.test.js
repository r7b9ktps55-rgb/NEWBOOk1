'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { parseCommand } = require('../lib/voice');
const { describeApp } = require('../lib/apps');

const apps = ['com.google.android.youtube.tv', 'ru.kinopoisk.tv', 'com.netflix.ninja', 'org.videolan.vlc'].map((pkg) => ({
  pkg, component: `${pkg}/.Main`, ...describeApp(pkg),
}));
const p = (t) => parseCommand(t, apps);

test('громкость', () => {
  assert.deepStrictEqual(p('Громче').actions, [{ type: 'key', key: 'vol_up', repeat: 3 }]);
  assert.deepStrictEqual(p('сделай тише на 5').actions, [{ type: 'key', key: 'vol_down', repeat: 5 }]);
  assert.deepStrictEqual(p('погромче на два').actions, [{ type: 'key', key: 'vol_up', repeat: 2 }]);
  assert.strictEqual(p('выключи звук').actions[0].key, 'mute');
});

test('питание', () => {
  assert.strictEqual(p('выключи телевизор').actions[0].key, 'sleep');
  assert.strictEqual(p('Выключи').actions[0].key, 'sleep');
  assert.strictEqual(p('включи телевизор').actions[0].type, 'wake');
});

test('приложения', () => {
  assert.strictEqual(p('открой ютуб').actions[0].pkg, 'com.google.android.youtube.tv');
  assert.strictEqual(p('Запусти Кинопоиск').actions[0].pkg, 'ru.kinopoisk.tv');
  assert.strictEqual(p('включи нетфликс').actions[0].pkg, 'com.netflix.ninja');
  assert.strictEqual(p('ютуб').actions[0].pkg, 'com.google.android.youtube.tv');
  assert.ok(p('открой телеграм').unknown);
});

test('поиск', () => {
  const a = p('найди котиков на ютубе').actions[0];
  assert.strictEqual(a.type, 'url');
  assert.match(a.url, /youtube\.com\/results\?search_query=%D0%BA%D0%BE%D1%82%D0%B8%D0%BA%D0%BE%D0%B2/);
  assert.strictEqual(p('включи на ютубе лофи').actions[0].type, 'url');
  assert.deepStrictEqual(p('найди фильм интерстеллар').actions[0], { type: 'search', query: 'фильм интерстеллар' });
});

test('каналы, медиа, навигация', () => {
  assert.deepStrictEqual(p('канал 12').actions.map((a) => a.key), ['1', '2']);
  assert.strictEqual(p('пауза').actions[0].key, 'pause');
  assert.strictEqual(p('продолжи').actions[0].key, 'play');
  assert.strictEqual(p('перемотай назад').actions[0].key, 'rewind');
  assert.strictEqual(p('домой').actions[0].key, 'home');
  assert.strictEqual(p('назад').actions[0].key, 'back');
  assert.deepStrictEqual(p('вниз 3 раза').actions[0], { type: 'key', key: 'down', repeat: 3 });
  assert.strictEqual(p('вправо').actions[0].key, 'right');
  assert.strictEqual(p('ок').actions[0].key, 'ok');
});

test('ввод текста сохраняет регистр', () => {
  assert.deepStrictEqual(p('напиши Привет Мир').actions[0], { type: 'text', text: 'Привет Мир' });
});

test('непонятное', () => {
  assert.ok(p('абракадабра').unknown);
});
