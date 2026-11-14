'use strict';

// Известные приложения: красивое имя, цвет плитки и голосовые синонимы.
const KNOWN_APPS = {
  'com.google.android.youtube.tv': { name: 'YouTube', color: '#ff0033', aliases: ['ютуб', 'ютюб', 'youtube', 'ютубчик'] },
  'com.teamsmart.videomanager.tv': { name: 'SmartTube', color: '#e53935', aliases: ['смарттюб', 'смарт тюб', 'smarttube', 'смарттуб'] },
  'com.liskovsoft.smarttubetv.beta': { name: 'SmartTube', color: '#e53935', aliases: ['смарттюб', 'смарт тюб', 'smarttube'] },
  'com.liskovsoft.smarttubetv': { name: 'SmartTube', color: '#e53935', aliases: ['смарттюб', 'smarttube'] },
  'com.google.android.youtube.tvkids': { name: 'YouTube Kids', color: '#ff4e45', aliases: ['ютуб детский', 'ютуб кидс', 'youtube kids'] },
  'com.google.android.youtube.tvmusic': { name: 'YouTube Music', color: '#ff0000', aliases: ['ютуб музыка', 'youtube music'] },
  'com.netflix.ninja': { name: 'Netflix', color: '#e50914', aliases: ['нетфликс', 'netflix'] },
  'ru.kinopoisk.tv': { name: 'Кинопоиск', color: '#ff6600', aliases: ['кинопоиск', 'kinopoisk'] },
  'com.yandex.tv.kinopoisk': { name: 'Кинопоиск', color: '#ff6600', aliases: ['кинопоиск'] },
  'ru.ivi.client': { name: 'Иви', color: '#ea003d', aliases: ['иви', 'ivi'] },
  'ru.okko.tv': { name: 'Okko', color: '#5a2fd8', aliases: ['окко', 'okko', 'око'] },
  'ru.more.play': { name: 'START', color: '#111111', aliases: ['старт', 'start'] },
  'ru.start.androidmobile': { name: 'START', color: '#111111', aliases: ['старт'] },
  'ru.mts.mtstv': { name: 'KION', color: '#ff0032', aliases: ['кион', 'kion', 'мтс тв'] },
  'ru.rt.video.app.tv': { name: 'Wink', color: '#ff4f12', aliases: ['винк', 'wink'] },
  'ru.more.tv': { name: 'more.tv', color: '#20153c', aliases: ['мор тв', 'море тв', 'more tv'] },
  'ru.premier.tv': { name: 'PREMIER', color: '#000000', aliases: ['премьер', 'premier'] },
  'ru.rutube.app.tv': { name: 'RUTUBE', color: '#100943', aliases: ['рутуб', 'рутюб', 'rutube'] },
  'ru.rutube.app': { name: 'RUTUBE', color: '#100943', aliases: ['рутуб', 'rutube'] },
  'com.vk.tv': { name: 'VK Видео', color: '#0077ff', aliases: ['вк видео', 'вконтакте', 'vk', 'вк'] },
  'com.amediateka.tv': { name: 'Амедиатека', color: '#000000', aliases: ['амедиатека'] },
  'com.spotify.tv.android': { name: 'Spotify', color: '#1db954', aliases: ['спотифай', 'spotify'] },
  'ru.yandex.music': { name: 'Яндекс Музыка', color: '#ffcc00', aliases: ['яндекс музыка', 'музыка'] },
  'com.yandex.tv.music': { name: 'Яндекс Музыка', color: '#ffcc00', aliases: ['яндекс музыка', 'музыка'] },
  'tv.twitch.android.app': { name: 'Twitch', color: '#9146ff', aliases: ['твич', 'twitch'] },
  'tv.twitch.android.viewer': { name: 'Twitch', color: '#9146ff', aliases: ['твич', 'twitch'] },
  'com.plexapp.android': { name: 'Plex', color: '#e5a00d', aliases: ['плекс', 'plex'] },
  'org.xbmc.kodi': { name: 'Kodi', color: '#17b2e7', aliases: ['коди', 'kodi'] },
  'org.videolan.vlc': { name: 'VLC', color: '#ff8800', aliases: ['влс', 'vlc', 'плеер'] },
  'com.amazon.amazonvideo.livingroom': { name: 'Prime Video', color: '#00a8e1', aliases: ['прайм', 'prime video', 'амазон'] },
  'com.disney.disneyplus': { name: 'Disney+', color: '#113ccf', aliases: ['дисней', 'disney'] },
  'com.mitv.tvhome.atv': { name: 'PatchWall', color: '#ff6900', aliases: ['патчвол', 'patchwall'] },
  'com.mitv.tvhome': { name: 'PatchWall', color: '#ff6900', aliases: ['патчвол'] },
  'com.android.tv.settings': { name: 'Настройки', color: '#546e7a', aliases: ['настройки'] },
  'com.android.vending': { name: 'Play Маркет', color: '#01875f', aliases: ['плей маркет', 'маркет', 'гугл плей', 'play market'] },
  'com.google.android.tvlauncher': { name: 'Главный экран', color: '#4285f4', aliases: [] },
  'com.google.android.apps.tv.launcherx': { name: 'Google TV', color: '#4285f4', aliases: [] },
  'com.google.android.videos': { name: 'Google TV', color: '#4285f4', aliases: [] },
  'com.android.chrome': { name: 'Chrome', color: '#4285f4', aliases: ['хром', 'браузер', 'chrome'] },
  'com.seraphimstudio.tvbro': { name: 'TV Bro', color: '#3949ab', aliases: ['браузер', 'тв бро'] },
  'com.lazycatsoftware.lazymedia': { name: 'LazyMedia', color: '#7cb342', aliases: ['лэйзи', 'лейзи медиа', 'lazy media'] },
  'com.lampa.tv': { name: 'Lampa', color: '#37474f', aliases: ['лампа', 'lampa'] },
  'top.rootu.lampa': { name: 'Lampa', color: '#37474f', aliases: ['лампа', 'lampa'] },
  'com.ottplay.ottplay': { name: 'OTT Play', color: '#1565c0', aliases: ['отт плей', 'ott play'] },
  'ru.iptvremote.android.iptv': { name: 'IPTV', color: '#00897b', aliases: ['айпитиви', 'iptv'] },
  'com.perm.kate': { name: 'Kate', color: '#2e7d32', aliases: [] },
};

function prettify(pkg) {
  const parts = pkg.split('.').filter((p) => !['com', 'ru', 'org', 'tv', 'android', 'app', 'apps', 'atv', 'leanback'].includes(p));
  const word = parts[parts.length - 1] || pkg;
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function describeApp(pkg) {
  const known = KNOWN_APPS[pkg];
  if (known) return { name: known.name, color: known.color, aliases: known.aliases };
  // Детерминированный цвет из имени пакета
  let h = 0;
  for (const ch of pkg) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return { name: prettify(pkg), color: `hsl(${h} 45% 38%)`, aliases: [] };
}

module.exports = { KNOWN_APPS, describeApp };
