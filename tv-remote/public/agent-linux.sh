#!/usr/bin/env bash
# Установка домашнего агента пульта на Linux (Raspberry Pi OS, Debian, Ubuntu) как systemd-сервиса.
#   curl -fsSL https://ВАШ-СЕРВЕР/agent-linux.sh | sudo SERVER_URL=https://ВАШ-СЕРВЕР AGENT_TOKEN=xxx AGENT_NAME=Дом bash
set -e
: "${SERVER_URL:?Задайте SERVER_URL}"
: "${AGENT_TOKEN:?Задайте AGENT_TOKEN}"
AGENT_NAME="${AGENT_NAME:-Дом}"
DIR=/opt/tv-agent
[ "$(id -u)" = 0 ] || { echo "Запустите через sudo"; exit 1; }

echo "==> Устанавливаю зависимости…"
if command -v apt-get >/dev/null; then
  apt-get update -qq
  command -v adb >/dev/null || apt-get install -y -qq adb
  if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 18 ]; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
    apt-get install -y -qq nodejs
  fi
else
  command -v node >/dev/null && command -v adb >/dev/null || { echo "Установите Node.js 18+ и adb вручную"; exit 1; }
fi

echo "==> Скачиваю агента с $SERVER_URL…"
id tvagent >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/tvagent --shell /usr/sbin/nologin tvagent
mkdir -p "$DIR"
curl -fsSL "$SERVER_URL/agent.tgz" | tar xz -C "$DIR"
cd "$DIR" && npm ci --omit=dev --no-audit --no-fund >/dev/null
chown -R tvagent:tvagent "$DIR"

umask 077
cat > /etc/tv-agent.env <<ENV
SERVER_URL=$SERVER_URL
AGENT_TOKEN=$AGENT_TOKEN
AGENT_NAME=$AGENT_NAME
ENV

cat > /etc/systemd/system/tv-agent.service <<UNIT
[Unit]
Description=TV Remote home agent
After=network-online.target
Wants=network-online.target

[Service]
User=tvagent
EnvironmentFile=/etc/tv-agent.env
WorkingDirectory=$DIR
ExecStart=$(command -v node) agent.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now tv-agent
systemctl restart tv-agent
sleep 3
journalctl -u tv-agent -n 5 --no-pager || true
echo
echo "✅ Агент «$AGENT_NAME» установлен. Логи: journalctl -u tv-agent -f"
