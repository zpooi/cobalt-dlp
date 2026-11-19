#!/usr/bin/env bash
# 本地/服务器直接运行(不用 Docker)的启动脚本
# 需要先装好 python3 与 ffmpeg
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[*] 创建虚拟环境..."
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

export PORT="${PORT:-13000}"
export HOST="${HOST:-0.0.0.0}"
export DOWNLOAD_DIR="${DOWNLOAD_DIR:-$PWD/downloads}"
export COOKIES_FILE="${COOKIES_FILE:-$PWD/cookies.txt}"
export RETENTION_HOURS="${RETENTION_HOURS:-24}"
export CLEANUP_INTERVAL="${CLEANUP_INTERVAL:-600}"
mkdir -p "$DOWNLOAD_DIR"

echo "[*] 启动 http://$HOST:$PORT  下载目录: $DOWNLOAD_DIR"
python app.py
