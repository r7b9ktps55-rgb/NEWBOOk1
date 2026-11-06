FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git djvulibre-bin \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/app/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bookhub ./bookhub
RUN useradd --uid 1000 --create-home app && mkdir -p /app/data && chown app:app /app/data
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["uvicorn", "bookhub.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
