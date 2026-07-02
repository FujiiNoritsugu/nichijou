#!/usr/bin/env bash
# 日乗 手動起動フォールバック。
# 自動起動（systemd の nichijou.service）が不調なときの保険として使う。
# 二重起動防止: ポート 8000 が使用中なら起動しない。
set -euo pipefail
cd "$(dirname "$0")"
PORT=8000
mkdir -p logs

if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "日乗は既に起動しています（ポート ${PORT} 使用中）。→ http://localhost:${PORT}"
  exit 0
fi

echo "日乗を起動します… ログ: $(pwd)/logs/nichijou.log"
nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" >> logs/nichijou.log 2>&1 &

# 起動確認（最大3秒待つ）
for _ in 1 2 3 4 5 6; do
  sleep 0.5
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    echo "起動しました → http://localhost:${PORT}"
    exit 0
  fi
done
echo "起動に失敗した可能性があります。ログを確認してください: logs/nichijou.log" >&2
exit 1
