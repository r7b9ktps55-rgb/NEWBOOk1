document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  const text = document.getElementById(btn.dataset.copy).value;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = "Скопировано ✓";
  } catch {
    btn.textContent = "Не удалось скопировать";
  }
});
