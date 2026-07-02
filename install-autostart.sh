#!/usr/bin/env bash
# 日乗 自動起動セットアップ（systemd システムサービス）。
# 使い方:  cd <このリポジトリ> && sudo ./install-autostart.sh
# 冪等: 何度実行しても同じ状態になる。
# 実行ユーザ・設置パスは環境から自動導出する（特定のユーザ名やパスを埋め込まない）。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "sudo で実行してください:  sudo ./install-autostart.sh" >&2
  exit 1
fi

# sudo を起動した実ユーザとこのスクリプトの置き場所から設定を導出する。
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"

UNIT=/etc/systemd/system/nichijou.service

# 変数展開させるため非クォートのヒアドキュメントを使う。
cat > "$UNIT" <<EOF
[Unit]
Description=nichijou (日乗) - personal journal app
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
Environment=HOME=${RUN_HOME}
# 観察機能の AI 判定に使う Anthropic API キー（任意）。
# ファイルが無くても起動できるよう先頭に "-" を付ける。実キーはこのユニットに書かない。
EnvironmentFile=-${PROJECT_DIR}/secrets/anthropic.env
# 常用起動: --reload は付けない。venv の uvicorn を直接起動。
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 異常終了時のみ自動再起動
Restart=on-failure
RestartSec=2
# ログをファイルに追記（journalctl -u nichijou でも閲覧可）
StandardOutput=append:${PROJECT_DIR}/logs/nichijou.log
StandardError=append:${PROJECT_DIR}/logs/nichijou.log

[Install]
WantedBy=multi-user.target
EOF

# ログ出力先を用意（無いと append: で起動失敗するため）
install -d -o "$RUN_USER" -g "$RUN_GROUP" "$PROJECT_DIR/logs"

systemctl daemon-reload
systemctl enable nichijou        # WSL起動時に自動起動
systemctl restart nichijou       # 今すぐ起動（既存があれば張り替え）

echo
echo "=== 状態 ==="
systemctl --no-pager --full status nichijou | head -n 12
echo
echo "セットアップ完了。→ http://localhost:8000"
