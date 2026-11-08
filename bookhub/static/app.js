// Book Hub: анимации (Motion), живая очередь, загрузка перетаскиванием, ролик «Как это работает».
// Без JavaScript сайт полностью работает; этот файл только улучшает его.
import { animate, inView, stagger } from "./vendor/motion.min.js";

const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
const EASE = [0.22, 1, 0.36, 1];
const SPRING = [0.34, 1.56, 0.64, 1];

const ICONS = {
  ok: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8 5.2-5.8"/></svg>',
  warn: '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.5h.01"/></svg>',
  check: '<svg class="ico" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
};

// ---------- Появление блоков ----------
function showNow(el) {
  el.style.animation = "none";
  el.style.opacity = "1";
}

function revealList(list) {
  const kids = [...list.children];
  if (!kids.length) return;
  kids.forEach((k) => (k.style.animation = "none"));
  animate(kids, { opacity: [0, 1], y: [10, 0] }, { delay: stagger(0.045), duration: 0.4, ease: EASE })
    .then(() => kids.forEach((k) => (k.style.transform = "")));
}

function setupReveal(root = document) {
  const blocks = root.querySelectorAll("[data-reveal]");
  const lists = root.querySelectorAll("[data-stagger]");
  if (reduce) {
    blocks.forEach(showNow);
    lists.forEach((l) => [...l.children].forEach(showNow));
    return;
  }
  inView(blocks, (el) => {
    el.style.animation = "none";
    animate(el, { opacity: [0, 1], y: [14, 0] }, { duration: 0.5, ease: EASE }).then(() => (el.style.transform = ""));
  }, { amount: 0.1 });
  inView(lists, (list) => revealList(list), { amount: 0.05 });
  inView(root.querySelectorAll("[data-draw]"), (el) => el.classList.add("is-drawn"), { amount: 0.6 });
}

// ---------- Уведомления ----------
const toastBox = document.querySelector("[data-toasts]");

function toast(text, kind = "ok") {
  if (!toastBox) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = ICONS[kind] || ICONS.ok;
  const span = document.createElement("span");
  span.textContent = text;
  el.append(span);
  toastBox.append(el);
  if (!reduce) animate(el, { opacity: [0, 1], y: [16, 0], scale: [0.98, 1] }, { duration: 0.35, ease: EASE });
  setTimeout(async () => {
    if (!reduce) await animate(el, { opacity: 0, y: 8 }, { duration: 0.25, ease: "easeIn" });
    el.remove();
  }, 4500);
}

function flashToToast() {
  const flash = document.querySelector("[data-flash]");
  if (!flash) return;
  toast(flash.textContent.trim(), flash.dataset.flash === "warn" ? "warn" : "ok");
  flash.remove();
  const url = new URL(location.href);
  url.searchParams.delete("m");
  history.replaceState(null, "", url);
}

// ---------- Формы: подтверждение и защита от двойного нажатия ----------
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (form.dataset.confirm && !confirm(form.dataset.confirm)) {
    e.preventDefault();
    return;
  }
  if (!form.matches("[data-busy-form]")) return;
  const btn = e.submitter;
  if (btn && !btn.querySelector(".spinner")) btn.insertAdjacentHTML("afterbegin", '<span class="spinner" aria-hidden="true"></span>');
  // Отключаем кнопки после того, как браузер собрал данные формы (иначе потеряется name/value нажатой кнопки).
  setTimeout(() => form.querySelectorAll("button").forEach((b) => {
    if (!b.disabled) { b.disabled = true; b.dataset.autoDisabled = "1"; }
  }), 0);
});

// Возврат кнопкой «Назад»: страница может прийти из кэша с отключёнными кнопками.
addEventListener("pageshow", (e) => {
  if (!e.persisted) return;
  document.querySelectorAll("[data-auto-disabled]").forEach((b) => {
    b.disabled = false;
    delete b.dataset.autoDisabled;
    b.querySelector(".spinner")?.remove();
  });
});

// ---------- Копирование ----------
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const label = btn.querySelector("span") || btn;
  const before = label.textContent;
  try {
    await navigator.clipboard.writeText(document.getElementById(btn.dataset.copy).value);
    label.textContent = "Скопировано";
    if (!reduce) animate(btn, { scale: [0.96, 1] }, { duration: 0.35, ease: SPRING });
  } catch {
    label.textContent = "Не удалось скопировать";
  }
  setTimeout(() => (label.textContent = before), 2000);
});

// ---------- Метки форматов ----------
document.addEventListener("change", (e) => {
  const input = e.target;
  if (!reduce && input.matches(".chip input")) {
    animate(input.nextElementSibling, { scale: [0.94, 1] }, { duration: 0.35, ease: SPRING });
  }
});

// ---------- Загрузка файлов перетаскиванием ----------
function humanSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1).replace(".", ",")} МБ`;
}

function setupDrop(drop) {
  const input = drop.querySelector("input[type=file]");
  const list = document.querySelector("[data-drop-list]");
  const types = (drop.dataset.types || "").split(",").filter(Boolean);
  const supported = (name) => types.some((t) => name.toLowerCase().endsWith(t) && name.length > t.length);
  const render = () => {
    list.replaceChildren();
    for (const f of input.files) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = f.name;
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = supported(f.name) ? humanSize(f.size) : "формат не поддерживается";
      if (!supported(f.name)) li.className = "bad";
      li.append(name, size);
      list.append(li);
    }
    list.hidden = !input.files.length;
    if (!reduce && input.files.length) {
      animate(list.children, { opacity: [0, 1], y: [6, 0] }, { delay: stagger(0.04), duration: 0.3, ease: EASE });
    }
  };
  input.addEventListener("change", render);
  const on = (e) => { e.preventDefault(); drop.classList.add("is-over"); };
  const off = (e) => { e.preventDefault(); drop.classList.remove("is-over"); };
  drop.addEventListener("dragenter", on);
  drop.addEventListener("dragover", on);
  drop.addEventListener("dragleave", off);
  drop.addEventListener("drop", (e) => {
    off(e);
    if (e.dataTransfer?.files?.length) {
      input.files = e.dataTransfer.files;
      render();
    }
  });
}

// ---------- Поиск по библиотеке ----------
function applyFilter() {
  const input = document.querySelector("[data-filter]");
  const q = (input?.value || "").trim().toLowerCase();
  let shown = 0;
  document.querySelectorAll(".book-card").forEach((card) => {
    const hit = !q || card.dataset.title.includes(q);
    card.hidden = !hit;
    shown += hit;
  });
  const empty = document.querySelector("[data-filter-empty]");
  if (empty) empty.hidden = shown > 0;
}
document.addEventListener("input", (e) => {
  if (e.target.matches("[data-filter]")) applyFilter();
});

// ---------- Живая очередь: обновление без перезагрузки ----------
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

function setupLive(root) {
  const queueBox = root.querySelector("[data-live-queue]");
  const libBox = root.querySelector("[data-live-library]");
  const navCount = document.querySelector("[data-live-count]");
  let version = root.dataset.version;
  let libVersion = root.dataset.libVersion;
  let notes = Number(root.dataset.notes || 0);
  let active = Number(root.dataset.active || 0);
  let busy = root.dataset.busy === "1";
  let timer = 0;

  const schedule = (ms) => {
    clearTimeout(timer);
    timer = setTimeout(tick, ms ?? (active || busy ? 3000 : 20000));
  };

  async function tick() {
    if (document.hidden) return; // продолжим, когда вкладку откроют
    try {
      const r = await fetch(`/live?lib=${encodeURIComponent(libVersion)}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (r.ok) {
        const d = await r.json();
        if (d.version !== version) {
          const before = new Set([...queueBox.querySelectorAll("[data-job]")].map((el) => el.dataset.job));
          queueBox.innerHTML = d.queue;
          queueBox.querySelectorAll("[data-stagger]").forEach((l) => l.removeAttribute("data-stagger"));
          const fresh = [...queueBox.querySelectorAll("[data-job]")].filter((el) => !before.has(el.dataset.job));
          if (fresh.length && !reduce) animate(fresh, { opacity: [0, 1], y: [8, 0] }, { delay: stagger(0.05), duration: 0.35, ease: EASE });
          version = d.version;
        }
        if (d.library != null) {
          const before = new Set([...libBox.querySelectorAll(".book-card")].map((el) => el.dataset.title));
          libBox.innerHTML = d.library;
          libBox.querySelectorAll("[data-stagger]").forEach((l) => l.removeAttribute("data-stagger"));
          const fresh = [...libBox.querySelectorAll(".book-card")].filter((el) => !before.has(el.dataset.title));
          if (fresh.length && !reduce) {
            animate(fresh, { opacity: [0, 1], y: [10, 0], scale: [0.98, 1] }, { delay: stagger(0.06), duration: 0.45, ease: EASE })
              .then(() => fresh.forEach((el) => (el.style.transform = "")));
          }
          libVersion = d.lib_version;
          applyFilter();
        }
        if (d.notes > notes) {
          const n = d.notes - notes;
          toast(`Готово: ${n} ${plural(n, "новая заметка", "новые заметки", "новых заметок")}`);
        }
        notes = d.notes;
        active = d.active;
        busy = d.busy;
        if (navCount) navCount.textContent = String(active);
      }
    } catch {
      // нет связи — попробуем в следующий раз
    }
    schedule();
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedule(300);
  });
  schedule();
}

// ---------- Ролик «Как это работает» ----------
const TYPES = ["PDF", "EPUB", "FB2", "DjVu", "DOCX"];
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));

function setupScene(scene) {
  const q = (name) => scene.querySelector(`[data-sc="${name}"]`);
  const how = scene.closest("[data-how]");
  const steps = how ? [...how.querySelectorAll("[data-step]")] : [];
  const playBtn = q("play");
  const type = q("type");
  const coreLabel = q("core-label");
  const count = q("count");
  const fileText = q("file-text");
  const FILE = fileText.textContent;
  if (reduce) return; // показываем финальный кадр без движения

  playBtn.hidden = false;
  const seq = [
    [q("book"), { opacity: [0, 1], y: ["-40%", "0%"], rotate: [-12, -4] }, { duration: 0.7, ease: EASE, at: 0.1 }],
    [q("book"), { scale: [1, 0.94] }, { duration: 0.4, ease: "easeInOut", at: 1.1 }],
    [q("lines").children, { opacity: [0, 1, 1, 0], x: ["0%", "80%", "260%", "330%"] }, { duration: 1.0, delay: stagger(0.1), ease: "easeInOut", at: 1.15 }],
    [q("core"), { scale: [1, 1.08, 1] }, { duration: 0.8, ease: "easeInOut", at: 1.5 }],
    [q("ring"), { strokeDashoffset: [1, 0] }, { duration: 1.6, ease: "linear", at: 1.5 }],
    [scene.querySelectorAll('[data-sc="note"]'), { opacity: [0, 1], x: ["-14%", "0%"], scale: [0.94, 1] }, { duration: 0.55, delay: stagger(0.18), ease: EASE, at: 3.2 }],
    [q("mark"), { scaleX: [0, 1] }, { duration: 0.6, ease: EASE, at: 3.7 }],
    [q("squeeze"), { scaleX: [1, 0.34] }, { duration: 1.0, ease: EASE, at: 4.2 }],
    [q("file"), { opacity: [0, 1], y: ["60%", "0%"] }, { duration: 0.45, ease: EASE, at: 5.0 }],
    [q("stage"), { opacity: [1, 0] }, { duration: 0.5, ease: "easeIn", at: 8.3 }],
  ];
  const controls = animate(seq, { repeat: Infinity });
  controls.pause();
  const total = controls.duration || 8.8;

  let raf = 0;
  let userPaused = false;
  let visible = false;

  const frame = () => {
    const t = ((controls.time % total) + total) % total;
    type.textContent = t < 1.0 ? TYPES[Math.floor(t / 0.2) % TYPES.length] : "EPUB";
    coreLabel.textContent = t >= 1.3 && t < 3.2 ? "Gemini читает…" : "Gemini";
    const k = clamp((t - 4.2) / 1.0, 0, 1);
    count.textContent = `${Math.round(300 - 200 * (1 - (1 - k) ** 3))} стр.`;
    fileText.textContent = FILE.slice(0, Math.round(clamp((t - 5.1) / 1.4, 0, 1) * FILE.length));
    const current = t < 1.2 ? 0 : t < 3.2 ? 1 : 2;
    steps.forEach((s, i) => s.classList.toggle("is-active", i === current));
    raf = requestAnimationFrame(frame);
  };

  const play = () => {
    if (userPaused || !visible || document.hidden) return;
    controls.play();
    scene.classList.add("is-playing");
    scene.classList.remove("is-paused");
    how?.classList.add("is-playing");
    if (!raf) raf = requestAnimationFrame(frame);
  };
  const stop = () => {
    controls.pause();
    cancelAnimationFrame(raf);
    raf = 0;
    scene.classList.remove("is-playing");
  };

  playBtn.addEventListener("click", () => {
    userPaused = !userPaused;
    if (userPaused) {
      stop();
      scene.classList.add("is-paused");
      playBtn.setAttribute("aria-label", "Смотреть");
    } else {
      playBtn.setAttribute("aria-label", "Пауза");
      play();
    }
  });
  inView(scene, () => {
    visible = true;
    play();
    return () => {
      visible = false;
      stop();
    };
  }, { amount: 0.35 });
  document.addEventListener("visibilitychange", () => (document.hidden ? stop() : play()));
}

// ---------- Запуск ----------
setupReveal();
flashToToast();
document.querySelectorAll("[data-drop]").forEach(setupDrop);
document.querySelectorAll("[data-scene]").forEach(setupScene);
const live = document.querySelector("[data-live]");
if (live) setupLive(live);
