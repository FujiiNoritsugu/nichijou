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

# 二つのサービスを立てる。境界は「どのポートを LAN に転送するか」で引く（WSL2 は
# NAT 配下なので、転送しないポートは LAN から到達できない＝送信元 IP 判定は不要）。
#   - nichijou.service          : 日記。127.0.0.1:8000。LAN へは転送しない → PC 専用。
#                                 Windows からは localhost 転送で従来どおり開ける。
#   - nichijou-dropbox.service  : 投函口。0.0.0.0:8001。portproxy でこのポートだけ
#                                 LAN に転送する。NICHIJOU_LAN_ONLY=1 で /u/ 以外は 404。
JOURNAL_UNIT=/etc/systemd/system/nichijou.service
DROPBOX_UNIT=/etc/systemd/system/nichijou-dropbox.service

# 変数展開させるため非クォートのヒアドキュメントを使う。
cat > "$JOURNAL_UNIT" <<EOF
[Unit]
Description=nichijou (日乗) - personal journal app (PC only, 127.0.0.1)
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
# 日記本体。127.0.0.1 バインド＝LAN 非公開。Windows の localhost 転送で PC から開ける。
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=2
StandardOutput=append:${PROJECT_DIR}/logs/nichijou.log
StandardError=append:${PROJECT_DIR}/logs/nichijou.log

[Install]
WantedBy=multi-user.target
EOF

cat > "$DROPBOX_UNIT" <<EOF
[Unit]
Description=nichijou (日乗) - photo drop-box (LAN, /u/ only, 0.0.0.0:8001)
After=network.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
Environment=HOME=${RUN_HOME}
# 投函口専用モード。/u/ 以外の全ルートを 404 にする（母屋を出さない）。
Environment=NICHIJOU_LAN_ONLY=1
EnvironmentFile=-${PROJECT_DIR}/secrets/anthropic.env
# 投函口。0.0.0.0:8001 で待ち受け、portproxy でこのポートだけ LAN に転送する。
ExecStart=${PROJECT_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001
Restart=on-failure
RestartSec=2
StandardOutput=append:${PROJECT_DIR}/logs/nichijou.log
StandardError=append:${PROJECT_DIR}/logs/nichijou.log

[Install]
WantedBy=multi-user.target
EOF

# ログ出力先を用意（無いと append: で起動失敗するため）
install -d -o "$RUN_USER" -g "$RUN_GROUP" "$PROJECT_DIR/logs"

systemctl daemon-reload
systemctl enable nichijou nichijou-dropbox        # WSL起動時に自動起動
systemctl restart nichijou nichijou-dropbox       # 今すぐ起動（既存があれば張り替え）

echo
echo "=== 状態 ==="
systemctl --no-pager --full status nichijou nichijou-dropbox | head -n 24
echo
echo "セットアップ完了。"
echo "  日記（PC専用）      → http://localhost:8000"
echo "  投函口（LAN公開）   → http://<PCのLAN内IP>:8001/u/<token>  （portproxy 設定後）"
