#!/usr/bin/env bash
# start.sh で手動起動した日乗を止める。
# 注意: systemd サービスで動いている場合は、こちらではなく
#       `sudo systemctl stop nichijou` を使うこと。
set -euo pipefail
PORT=8000
pids=$(ss -ltnp 2>/dev/null | grep ":${PORT} " | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)

if [ -z "${pids}" ]; then
  echo "ポート ${PORT} で動いている日乗は見つかりませんでした。"
  exit 0
fi
echo "停止します（PID: ${pids}）"
# shellcheck disable=SC2086
kill ${pids}
echo "停止しました。"
