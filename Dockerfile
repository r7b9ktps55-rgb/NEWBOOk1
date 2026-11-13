# Debian закреплён (trixie): обновления безопасности приходят при пересборке, но без смены дистрибутива.
FROM python:3.12-slim-trixie

RUN apt-get update \
 && apt-get install -y --no-install-recommends git djvulibre-bin \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app

# Точные версии всех зависимостей с хешами (requirements.lock собирается из requirements.txt).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY bookhub ./bookhub
RUN useradd --uid 1000 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"
# Один процесс: очередь и расписание рассчитаны на одного исполнителя.
CMD ["uvicorn", "bookhub.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--workers", "1"]
