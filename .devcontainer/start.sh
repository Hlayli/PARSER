#!/usr/bin/env bash
# Запуск PARSER в GitHub Codespaces: сервер слушает 0.0.0.0, чтобы порт
# 8777 пробрасывался на публичный адрес вида https://<codespace>-8777.app.github.dev
set -euo pipefail

cd "$(dirname "$0")/.."

if curl -fsS --max-time 2 http://127.0.0.1:8777/api/health >/dev/null 2>&1; then
  echo "PARSER уже запущен: откройте вкладку PORTS -> 8777"
  exit 0
fi

mkdir -p data
nohup python -m uvicorn backend.app:app --host 0.0.0.0 --port 8777 \
  > /tmp/parser.log 2>&1 &

echo "PARSER запускается. Откройте вкладку PORTS -> 8777 (Open in Browser)."
echo "Лог: /tmp/parser.log"
