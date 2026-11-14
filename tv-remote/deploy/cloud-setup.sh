#!/usr/bin/env bash
# Установка пульта на облачный сервер (Ubuntu / Debian) одной командой:
#   sudo bash deploy/cloud-setup.sh
# Можно без вопросов: sudo DOMAIN=tv.example.com ACCESS_PIN=пароль INSTALL_TAILSCALE=yes bash deploy/cloud-setup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo: sudo bash deploy/cloud-setup.sh"; exit 1; }

rnd() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }  # rnd N -> 2N hex-символов

ask() { # ask "Вопрос" "по умолчанию" -> ответ
  local q="$1" def="${2:-}" ans=""
  if [ -r /dev/tty ]; then read -r -p "$q${def:+ [$def]}: " ans </dev/tty || true; fi
  echo "${ans:-$def}"
}

echo "==> 1/5 Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || { echo "Нужен docker compose v2"; exit 1; }

echo "==> 2/5 Настройки"
if [ -f .env ]; then
  echo "Найден существующий .env — использую его (удалите файл, чтобы настроить заново)."
  set -a; . ./.env; set +a
else
  PUBLIC_IP="$(curl -fsS4 --max-time 5 https://api.ipify.org || curl -fsS4 --max-time 5 https://ifconfig.me || true)"
  DEF_DOMAIN=""
  [ -n "$PUBLIC_IP" ] && DEF_DOMAIN="$(echo "$PUBLIC_IP" | tr . -).sslip.io"
  DOMAIN="${DOMAIN:-$(ask "Домен пульта (свой домен с A-записью на этот сервер, или Enter для бесплатного sslip.io)" "$DEF_DOMAIN")}"
  [ -n "$DOMAIN" ] || { echo "Нужен домен"; exit 1; }
  GEN_PIN="$(rnd 5)"
  ACCESS_PIN="${ACCESS_PIN:-$(ask "Пароль для входа в пульт" "$GEN_PIN")}"
  [ "${#ACCESS_PIN}" -ge 6 ] || { echo "Пароль должен быть не короче 6 символов"; exit 1; }
  AGENT_TOKEN="${AGENT_TOKEN:-$(rnd 24)}"
  umask 077
  cat > .env <<ENV
DOMAIN=$DOMAIN
ACCESS_PIN=$ACCESS_PIN
AGENT_TOKEN=$AGENT_TOKEN
TZ=${TZ:-Europe/Moscow}
ENV
  umask 022
fi

echo "==> 3/5 Tailscale (связь с ТВ без домашнего агента)"
TS="${INSTALL_TAILSCALE:-}"
if [ -z "$TS" ] && ! command -v tailscale >/dev/null; then
  TS="$(ask "Установить Tailscale на сервер? (нужно, если на ТВ будет стоять приложение Tailscale) y/N" "n")"
fi
if [[ "$TS" =~ ^(y|yes|д|да)$ ]] && ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
  echo "Сейчас появится ссылка — откройте её и войдите в тот же аккаунт Tailscale, что и на телевизоре."
  tailscale up --hostname=tv-remote-cloud
fi

echo "==> 4/5 Файрвол"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  echo "Открыты порты 80 и 443 в ufw"
fi
echo "(Если у облачного провайдера есть свой файрвол / Security Group — откройте там TCP 80 и 443.)"

echo "==> 5/5 Запуск"
docker compose -f docker-compose.cloud.yml up -d --build

echo "Жду, пока Caddy получит HTTPS-сертификат…"
for i in $(seq 1 30); do
  if curl -fsS --max-time 5 "https://$DOMAIN/healthz" >/dev/null 2>&1; then OK=1; break; fi
  sleep 3
done

cat <<INFO

============================================================
 ✅ Пульт развёрнут

 Адрес:   https://$DOMAIN
 Пароль:  $ACCESS_PIN

 Откройте адрес на телефоне → введите пароль → «Добавить на главный экран».

 Как подключить телевизор (выберите один способ):
  A) Tailscale на телевизоре: поставьте приложение Tailscale на ТВ,
     войдите в тот же аккаунт и добавьте ТВ в пульте по его адресу 100.x.x.x.
  B) Домашний агент: в пульте → Настройки → «Домашний агент» →
     «Показать команды установки» — и запустите команду дома.

 Логи:        docker compose -f docker-compose.cloud.yml logs -f
 Обновление:  git pull && docker compose -f docker-compose.cloud.yml up -d --build
============================================================
INFO
[ "${OK:-0}" = 1 ] || echo "⚠️  HTTPS пока не отвечает. Проверьте, что домен указывает на этот сервер и порты 80/443 открыты: docker compose -f docker-compose.cloud.yml logs caddy"
