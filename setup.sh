#!/usr/bin/env bash
# 倉管系統 — Linux / macOS server 安裝 + 啟動腳本
# 用法:
#   chmod +x setup.sh
#   ./setup.sh              # 安裝並啟動 (預設綁 0.0.0.0:8000)
#   ./setup.sh install      # 只安裝, 不啟動
#   ./setup.sh run          # 只啟動 (假設已安裝)
#   HOST=0.0.0.0 PORT=8000 ./setup.sh

set -e
cd "$(dirname "$0")"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
PY="${PYTHON:-python3}"

cmd="${1:-all}"

install_deps() {
  if ! command -v "$PY" >/dev/null 2>&1; then
    echo "[ERROR] $PY not found. Please install Python 3.10+ first."
    echo "  Ubuntu/Debian: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    echo "  RHEL/CentOS:   sudo dnf install -y python3 python3-pip"
    exit 1
  fi

  if [ ! -d ".venv" ]; then
    echo "[setup] Creating virtualenv (.venv)..."
    "$PY" -m venv .venv
  fi

  echo "[setup] Installing requirements..."
  ./.venv/bin/pip install --upgrade pip -q
  ./.venv/bin/pip install -q -r requirements.txt
  echo "[setup] Done."
}

run_app() {
  echo "============================================"
  echo " Warehouse Manager"
  echo " Listening on http://$HOST:$PORT"
  echo " Press Ctrl+C to stop"
  echo "============================================"
  exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
}

case "$cmd" in
  install) install_deps ;;
  run)     run_app ;;
  all)     install_deps; run_app ;;
  *)       echo "Usage: $0 [install|run|all]"; exit 1 ;;
esac
