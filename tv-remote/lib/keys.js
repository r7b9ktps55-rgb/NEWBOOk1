'use strict';

// Белый список клавиш: короткое имя -> KEYCODE_* Android.
const KEYS = {
  power: 'KEYCODE_POWER',
  wakeup: 'KEYCODE_WAKEUP',
  sleep: 'KEYCODE_SLEEP',
  input: 'KEYCODE_TV_INPUT',
  home: 'KEYCODE_HOME',
  back: 'KEYCODE_BACK',
  menu: 'KEYCODE_MENU',
  recents: 'KEYCODE_APP_SWITCH',
  settings: 'KEYCODE_SETTINGS',
  search: 'KEYCODE_SEARCH',
  assist: 'KEYCODE_ASSIST',
  info: 'KEYCODE_INFO',
  guide: 'KEYCODE_GUIDE',
  captions: 'KEYCODE_CAPTIONS',

  up: 'KEYCODE_DPAD_UP',
  down: 'KEYCODE_DPAD_DOWN',
  left: 'KEYCODE_DPAD_LEFT',
  right: 'KEYCODE_DPAD_RIGHT',
  ok: 'KEYCODE_DPAD_CENTER',
  enter: 'KEYCODE_ENTER',
  del: 'KEYCODE_DEL',

  vol_up: 'KEYCODE_VOLUME_UP',
  vol_down: 'KEYCODE_VOLUME_DOWN',
  mute: 'KEYCODE_VOLUME_MUTE',
  ch_up: 'KEYCODE_CHANNEL_UP',
  ch_down: 'KEYCODE_CHANNEL_DOWN',

  play_pause: 'KEYCODE_MEDIA_PLAY_PAUSE',
  play: 'KEYCODE_MEDIA_PLAY',
  pause: 'KEYCODE_MEDIA_PAUSE',
  stop: 'KEYCODE_MEDIA_STOP',
  next: 'KEYCODE_MEDIA_NEXT',
  prev: 'KEYCODE_MEDIA_PREVIOUS',
  rewind: 'KEYCODE_MEDIA_REWIND',
  ff: 'KEYCODE_MEDIA_FAST_FORWARD',

  red: 'KEYCODE_PROG_RED',
  green: 'KEYCODE_PROG_GREEN',
  yellow: 'KEYCODE_PROG_YELLOW',
  blue: 'KEYCODE_PROG_BLUE',
};
for (let i = 0; i <= 9; i++) KEYS[String(i)] = `KEYCODE_${i}`;

function resolveKey(name) {
  return Object.prototype.hasOwnProperty.call(KEYS, name) ? KEYS[name] : null;
}

module.exports = { KEYS, resolveKey };
