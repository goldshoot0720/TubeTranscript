#!/bin/bash
# TubeTranscript 啟動腳本
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
PY="${PYTHON:-}"

# 1) 找一個裝好依賴的 Python
if [ -z "$PY" ]; then
  if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
  elif command -v python3 >/dev/null 2>&1 && python3 -c "import fastapi, uvicorn, requests" >/dev/null 2>&1; then
    PY="python3"
  elif [ -x "$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python" ]; then
    PY="$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python"
  else
    echo "→ 建立虛擬環境 .venv …"
    python3 -m venv .venv
    ./.venv/bin/pip install -q --upgrade pip
    ./.venv/bin/pip install -q -r requirements.txt
    PY=".venv/bin/python"
  fi
fi

# 2) 檢查必要工具
command -v yt-dlp >/dev/null 2>&1 || echo "⚠️  找不到 yt-dlp，請先執行：brew install yt-dlp"
command -v ffmpeg >/dev/null 2>&1 || echo "⚠️  找不到 ffmpeg，請先執行：brew install ffmpeg"

# 3) 開瀏覽器並啟動
( sleep 1.2; open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true ) &
echo ""
echo "  TubeTranscript  →  http://127.0.0.1:${PORT}"
echo "  按 Ctrl+C 停止"
echo ""
exec "$PY" app.py
