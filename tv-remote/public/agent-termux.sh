#!/data/data/com.termux/files/usr/bin/bash
# Установка домашнего агента пульта в Termux (Android-телефон/планшет/приставка).
#   curl -fsSL https://ВАШ-СЕРВЕР/agent-termux.sh | SERVER_URL=https://ВАШ-СЕРВЕР AGENT_TOKEN=xxx AGENT_NAME=Дом bash
set -e
: "${SERVER_URL:?Задайте SERVER_URL}"
: "${AGENT_TOKEN:?Задайте AGENT_TOKEN}"
AGENT_NAME="${AGENT_NAME:-Дом}"
DIR="$HOME/tv-agent"

echo "==> Устанавливаю Node.js и adb…"
yes | pkg update >/dev/null 2>&1 || true
pkg install -y nodejs-lts android-tools curl tar >/dev/null

echo "==> Скачиваю агента с $SERVER_URL…"
mkdir -p "$DIR"
curl -fsSL "$SERVER_URL/agent.tgz" | tar xz -C "$DIR"
cd "$DIR" && npm ci --omit=dev --no-audit --no-fund >/dev/null

cat > "$DIR/agent.env" <<ENV
export SERVER_URL='$SERVER_URL'
export AGENT_TOKEN='$AGENT_TOKEN'
export AGENT_NAME='$AGENT_NAME'
ENV
chmod 600 "$DIR/agent.env"

cat > "$DIR/run.sh" <<'RUN'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
cd "$HOME/tv-agent" && . ./agent.env
while true; do node agent.js; sleep 5; done
RUN
chmod +x "$DIR/run.sh"

# Автозапуск после перезагрузки телефона (нужно приложение Termux:Boot)
mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/tv-agent" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
nohup "$HOME/tv-agent/run.sh" > "$HOME/tv-agent/agent.log" 2>&1 &
BOOT
chmod +x "$HOME/.termux/boot/tv-agent"

pkill -f "tv-agent/run.sh" 2>/dev/null || true
pkill -f "node agent.js" 2>/dev/null || true
nohup "$DIR/run.sh" > "$DIR/agent.log" 2>&1 &
sleep 3
echo
tail -n 5 "$DIR/agent.log" || true
echo
echo "✅ Агент «$AGENT_NAME» запущен. Лог: tail -f ~/tv-agent/agent.log"
echo "   Для автозапуска установите Termux:Boot (F-Droid) и один раз откройте его."
echo "   В настройках Android отключите оптимизацию батареи для Termux."
