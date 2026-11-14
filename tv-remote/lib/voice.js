'use strict';

// Разбор голосовых/текстовых команд на русском языке в действия для ТВ.
// Возвращает { actions: [...], say: 'что сделали' } или { actions: [], say, unknown: true }.

const NUM_WORDS = {
  один: 1, одну: 1, раз: 1, два: 2, две: 2, три: 3, четыре: 4, пять: 5, шесть: 6,
  семь: 7, восемь: 8, девять: 9, десять: 10, пятнадцать: 15, двадцать: 20,
};

function normalize(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/ё/g, 'е')
    .replace(/[.,!?;:«»"()]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function extractCount(t, def) {
  const m = t.match(/\b(\d{1,2})\b/);
  if (m) return Math.min(30, Math.max(1, parseInt(m[1], 10)));
  for (const w of t.split(' ')) {
    if (NUM_WORDS[w] && w !== 'раз') return NUM_WORDS[w];
  }
  return def;
}

const key = (k, repeat = 1) => ({ type: 'key', key: k, repeat });

function matchApp(query, apps) {
  const q = normalize(query)
    .replace(/^(приложение|программу|программа|сервис)\s+/, '')
    .replace(/\s+(на телевизоре|на тв|пожалуйста)$/, '')
    .trim();
  if (!q) return null;
  let best = null;
  let bestScore = 0;
  for (const app of apps) {
    const names = [app.name, ...(app.aliases || [])].map(normalize).filter(Boolean);
    for (const n of names) {
      let score = 0;
      if (n === q) score = 100;
      else if (q.startsWith(n + ' ') || q === n) score = 80;
      else if (n.startsWith(q) && q.length >= 3) score = 60;
      else if (q.includes(n) && n.length >= 3) score = 50;
      else if (n.includes(q) && q.length >= 4) score = 40;
      if (score > bestScore) {
        bestScore = score;
        best = app;
      }
    }
  }
  return best;
}

function parseCommand(rawText, apps = []) {
  const t = normalize(rawText);
  if (!t) return { actions: [], say: 'Пустая команда', unknown: true };
  let m;

  // Ввод текста: "напиши привет", "введи пароль 1234"
  if ((m = String(rawText).trim().match(/^(напиши|введи|набери|напечатай)\s+(.+)$/i))) {
    return { actions: [{ type: 'text', text: m[2] }], say: `Ввожу: «${m[2]}»` };
  }

  // Звук
  if (/(без звука|выключи звук|отключи звук|убери звук|заглуши|беззвучн|\bmute\b|включи звук|верни звук)/.test(t)) {
    return { actions: [key('mute')], say: 'Звук вкл/выкл' };
  }

  // Питание
  if (/(выключи|выключить|отключи|погаси)\s*(телевизор|тв|телек|экран)?$/.test(t) || /^(спать|выключение)$/.test(t)) {
    return { actions: [key('sleep')], say: 'Выключаю телевизор' };
  }
  if (/(включи|включить|разбуди)\s+(телевизор|тв|телек|экран)$/.test(t)) {
    return { actions: [{ type: 'wake' }], say: 'Включаю телевизор' };
  }

  // Поиск на YouTube / RUTUBE
  if (
    (m = t.match(/^(?:найди|поищи|ищи|покажи|включи|поставь|запусти|открой)\s+(.+?)\s+(?:на|в|во)\s+(ютуб[а-яa-z]*|ютюб[а-яa-z]*|youtube|рутуб[а-яa-z]*|rutube)$/)) ||
    (m = t.match(/^(?:найди|поищи|ищи|покажи|включи|поставь)\s+(?:на|в|во)\s+(ютуб[а-яa-z]*|ютюб[а-яa-z]*|youtube|рутуб[а-яa-z]*|rutube)\s+(.+)$/))
  ) {
    let query = m[1];
    let site = m[2];
    if (/ютуб|ютюб|youtube|рутуб|rutube/.test(query) && !/ютуб|ютюб|youtube|рутуб|rutube/.test(site)) {
      [query, site] = [site, query];
    }
    if (/рутуб|rutube/.test(site)) {
      return {
        actions: [{ type: 'url', url: `https://rutube.ru/search/?query=${encodeURIComponent(query)}` }],
        say: `Ищу на RUTUBE: «${query}»`,
      };
    }
    return {
      actions: [{ type: 'url', url: `https://www.youtube.com/results?search_query=${encodeURIComponent(query)}` }],
      say: `Ищу на YouTube: «${query}»`,
    };
  }

  // Общий поиск
  if ((m = t.match(/^(?:найди|поищи|ищи|поиск|search)\s+(.+)$/))) {
    return { actions: [{ type: 'search', query: m[1] }], say: `Ищу: «${m[1]}»` };
  }

  // Громкость
  if (/(громче|погромче|прибавь|увеличь громкость|громкость (вверх|выше|больше|плюс)|louder|volume up)/.test(t)) {
    const n = extractCount(t, 3);
    return { actions: [key('vol_up', n)], say: `Громче на ${n}` };
  }
  if (/(тише|потише|убавь|уменьши громкость|громкость (вниз|ниже|меньше|минус)|quieter|volume down)/.test(t)) {
    const n = extractCount(t, 3);
    return { actions: [key('vol_down', n)], say: `Тише на ${n}` };
  }

  // Каналы
  if ((m = t.match(/канал[а-яa-z]*\s+(?:номер\s+)?(\d{1,4})/))) {
    const digits = m[1].split('');
    return { actions: digits.map((d) => key(d)), say: `Канал ${m[1]}` };
  }
  if (/(следующий канал|канал (вперед|выше|плюс))/.test(t)) return { actions: [key('ch_up')], say: 'Следующий канал' };
  if (/(предыдущий канал|канал (назад|ниже|минус))/.test(t)) return { actions: [key('ch_down')], say: 'Предыдущий канал' };

  // Медиа
  if (/^(пауза|поставь на паузу|останови|стоп|stop|pause|замри)/.test(t)) return { actions: [key('pause')], say: 'Пауза' };
  if (/^(продолжи|играй|воспроизведи|плей|play|сними с паузы|продолжить)/.test(t)) return { actions: [key('play')], say: 'Воспроизведение' };
  if (/(перемотай|мотай|промотай)\s*(вперед)?$/.test(t) || /перемотай вперед|мотай вперед/.test(t)) {
    const n = extractCount(t, 1);
    return { actions: [key('ff', n)], say: 'Перемотка вперёд' };
  }
  if (/(перемотай|мотай|промотай) назад/.test(t)) {
    const n = extractCount(t, 1);
    return { actions: [key('rewind', n)], say: 'Перемотка назад' };
  }
  if (/^(следующ|дальше|next)/.test(t)) return { actions: [key('next')], say: 'Следующее' };
  if (/^(предыдущ|previous)/.test(t)) return { actions: [key('prev')], say: 'Предыдущее' };

  // Системные экраны
  if (/^(домой|на главную|главный экран|главная|home)/.test(t)) return { actions: [key('home')], say: 'Домой' };
  if (/^(назад|вернись|back)$/.test(t)) return { actions: [key('back')], say: 'Назад' };
  if (/(источник|вход|hdmi|input)/.test(t)) return { actions: [key('input')], say: 'Выбор источника' };
  if (/(недавние|последние приложения|переключи приложение)/.test(t)) return { actions: [key('recents')], say: 'Недавние приложения' };

  // Запуск приложений
  if ((m = t.match(/^(?:открой|запусти|включи|покажи|зайди в|перейди в|давай)\s+(.+)$/))) {
    const app = matchApp(m[1], apps);
    if (app) return { actions: [{ type: 'launch', pkg: app.pkg, component: app.component }], say: `Открываю ${app.name}` };
    if (/^настройки/.test(m[1])) return { actions: [key('settings')], say: 'Настройки' };
    return { actions: [], say: `Не нашёл приложение «${m[1]}» на телевизоре`, unknown: true };
  }
  if (/^(настройки|settings)$/.test(t)) return { actions: [key('settings')], say: 'Настройки' };
  if (/^меню$/.test(t)) return { actions: [key('menu')], say: 'Меню' };

  // Навигация
  const nav = [
    [/^(вверх|наверх|выше|up)(?=\s|$)/, 'up', 'Вверх'],
    [/^(вниз|ниже|down)(?=\s|$)/, 'down', 'Вниз'],
    [/^(влево|налево|левее|left)(?=\s|$)/, 'left', 'Влево'],
    [/^(вправо|направо|правее|right)(?=\s|$)/, 'right', 'Вправо'],
  ];
  for (const [re, k, say] of nav) {
    if (re.test(t)) {
      const n = extractCount(t, 1);
      return { actions: [key(k, n)], say: n > 1 ? `${say} ×${n}` : say };
    }
  }
  if (/^(ок|окей|ok|okay|выбери|выбрать|нажми|ввод|enter|да)$/.test(t)) return { actions: [key('ok')], say: 'ОК' };

  // Просто название приложения: "ютуб"
  const app = matchApp(t, apps);
  if (app) return { actions: [{ type: 'launch', pkg: app.pkg, component: app.component }], say: `Открываю ${app.name}` };

  return { actions: [], say: `Не понял команду «${rawText}»`, unknown: true };
}

module.exports = { parseCommand, normalize, matchApp };
