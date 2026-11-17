#!/bin/bash
# Пересобирает bookhub/static/vendor/motion-<версия>.min.js — облегчённую сборку Motion (motion.dev)
# только с функциями animate, inView и stagger. Нужен Node.js.
set -euo pipefail
MOTION_VERSION=14.6.0
ESBUILD_VERSION=0.28.2
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
npm init -y >/dev/null
npm install --no-audit --no-fund "motion@$MOTION_VERSION" "esbuild@$ESBUILD_VERSION" >/dev/null
echo 'export { animate, inView, stagger } from "motion";' > entry.js
npx esbuild entry.js --bundle --format=esm --minify --target=es2020 --legal-comments=none \
  --outfile="$ROOT/bookhub/static/vendor/motion-$MOTION_VERSION.min.js"
cp node_modules/motion/LICENSE.md "$ROOT/bookhub/static/vendor/LICENSE-motion.md"
echo "Готово: motion@$MOTION_VERSION"
