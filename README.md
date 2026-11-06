# Book Hub

Личный сайт, который превращает PDF-книги и статьи по ссылке в подробные конспекты для Obsidian с помощью Gemini.

- Загружаете PDF или ссылку → задача попадает в очередь.
- Каждый день в заданное время (или по кнопке) Gemini делает конспект: главная идея, ключевые концепции, разбор по главам, цитаты, план действий, связанные темы `[[...]]`.
- Готовая заметка открывается на сайте, скачивается как `.md`, открывается кнопкой «Открыть в Obsidian» и при желании сама уходит в Git-репозиторий вашего хранилища.

## Быстрый старт (Docker)

```bash
git clone https://github.com/r7b9ktps55-rgb/NEWBOOk1.git bookhub
cd bookhub
cp .env.example .env
nano .env            # GEMINI_API_KEY и WEB_PASSWORD — обязательно
mkdir -p data
docker compose up -d --build
```

Откройте http://localhost:8080, логин `me` (или `WEB_USER`), пароль — `WEB_PASSWORD`.

Чтобы открыть сайт с телефона в той же Wi-Fi-сети, поставьте `BIND=0.0.0.0` в `.env` и перезапустите: `docker compose up -d`.
Наружу в интернет лучше выставлять только через HTTPS (например, Caddy или Cloudflare Tunnel) — пароль передаётся при каждом запросе.

## Без Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
.venv/bin/uvicorn bookhub.main:create_app --factory --port 8080
```

## Настройки (`.env`)

| Переменная | Что делает |
|---|---|
| `GEMINI_API_KEY` | Ключ Gemini, https://aistudio.google.com/apikey |
| `WEB_USER`, `WEB_PASSWORD` | Вход на сайт. Без пароля сайт не запустится |
| `GEMINI_MODEL` | Модель, по умолчанию `gemini-2.5-flash` |
| `DAILY_RUN_TIME` | Время ежедневного разбора `ЧЧ:ММ`; `off` — выключить |
| `DAILY_BATCH_SIZE` | Сколько книг разбирать за раз |
| `TIMEZONE` | Часовой пояс, например `Europe/Saratov` |
| `OBSIDIAN_VAULT_NAME` | Имя хранилища для кнопки «Открыть в Obsidian» |
| `OBSIDIAN_VAULT_SUBFOLDER` | Папка для конспектов внутри хранилища (`Books`) |
| `GIT_REPO_URL`, `GIT_TOKEN`, `GIT_BRANCH` | Автоматический push заметок в репозиторий хранилища |
| `MAX_UPLOAD_MB` | Лимит размера PDF |
| `BIND`, `PORT` | Где слушает сайт (только Docker) |

### Git-синхронизация с Obsidian

1. Создайте приватный репозиторий для хранилища (или используйте существующий — старые заметки сохранятся).
2. Создайте fine-grained токен GitHub с доступом **Contents: Read and write** только к этому репозиторию.
3. Пропишите `GIT_REPO_URL` и `GIT_TOKEN`. Токен не сохраняется в `.git/config`.
4. На компьютере/телефоне подтягивайте изменения плагином Obsidian Git.

### Свой промпт

Положите текст в `data/prompt.md` — он заменит встроенный. Обязательно оставьте требование «первая строка: `FILENAME: Название - Автор`». `{date}` заменяется на сегодняшнюю дату.

## Где лежат данные

```
data/
  inbox/            PDF в очереди (можно класть файлы сюда напрямую)
  archive/          обработанные PDF
  obsidian_vault/   хранилище; конспекты в Books/
  bookhub.db        очередь и история
```

## Разработка

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Тесты не ходят в Gemini — модель подменяется заглушкой.
