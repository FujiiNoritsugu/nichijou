# 日乗（nichijou）

飾らない日々の記録のための、個人用の小さな Web アプリ。二つのページからなる。

- **日乗** … 一行の短文をその場で書き留める記録。
- **観察** … 撮った写真を取り込むと、AI（Claude の vision）が種類を判定してラベルを付け、日付とともに蓄積する自然観察の記録。

## 設計の要点（技術的事実として）

- 一度保存した記録は**編集できない**（編集 UI を持たない）。**削除はできる**。
- 記録は生のまま時系列で並べる。日乗は本文をプレーンテキストで保存する。
- 観察の判定結果は手で直さない。ただし AI の判定が誤ったときのために、**再判定（AI に問い直す）**だけはできる。
- 日乗と観察は同一アプリ内の別ページとして分離している。

## 技術構成

- **FastAPI**（ルーティング）+ **Uvicorn**
- **SQLite**（メタデータの保存。`app/db.py` だけが DB を知る）
- **Jinja2**（テンプレート、1 ページ 1 テンプレート）
- **Anthropic API / Claude vision**（`claude-sonnet-4-6`）で写真の種類判定
- **Pillow**（EXIF の撮影日時読み取り。**GPS 位置情報は読まない・保存しない**）
- **systemd**（WSL 上で常駐）
- 依存管理は [uv](https://docs.astral.sh/uv/)

## データの扱い（すべてローカル）

- 日乗の本文は `nichijou.db`（SQLite, プロジェクト直下）に保存。
- 観察の写真は `photos/` に保存し、DB にはファイル名・判定結果・撮影/登録日時などのメタデータのみを持つ。
- `nichijou.db` と `photos/` は **`.gitignore` で除外**しており、リポジトリには含まれない（写真は位置情報を含みうるため）。
- API キーは環境変数 `ANTHROPIC_API_KEY` から読む。コードにもリポジトリにも鍵は置かない。

## セットアップ

### 1. 依存の解決

```bash
uv sync
```

### 2. API キーを設定（観察機能に必要）

鍵はリポジトリ外の扱いにする。ここでは gitignore 済みの `secrets/` に置き、systemd の `EnvironmentFile` で注入する方式を採る。

```bash
mkdir -p secrets && chmod 700 secrets
printf 'ANTHROPIC_API_KEY=%s\n' 'sk-ant-...' > secrets/anthropic.env
chmod 600 secrets/anthropic.env
```

（開発中に手起動するだけなら、`export ANTHROPIC_API_KEY=sk-ant-...` でもよい。）

### 3. 起動

```bash
uv run uvicorn app.main:app --reload --port 8000
```

ブラウザで http://localhost:8000 を開く。日乗トップと `/observations`（観察）はページ上部のリンクで行き来できる。

## 自動起動（WSL / systemd で常駐）

WSL の systemd システムサービスとして常駐させる。

```bash
sudo ./install-autostart.sh
```

このスクリプトは、実行ユーザと設置パスを環境から自動導出してユニットを生成する（特定のユーザ名・パスを埋め込まない）。生成されるユニットは:

- WSL 起動時に自動起動（`--reload` なし）
- 異常終了時のみ再起動（`Restart=on-failure`）
- `secrets/anthropic.env` があれば `EnvironmentFile` として読み込む（無くても起動する）
- ログを `logs/nichijou.log` に追記（`journalctl -u nichijou` でも閲覧可）

Windows ログイン時に WSL を起こしたい場合は、`windows/nichijou-autostart.vbs` をスタートアップに置く（既定ディストロを無音で起動する。非既定なら `-d <DistroName>` を足す）。

### 運用コマンド

| 操作 | コマンド |
|---|---|
| 状態確認 | `systemctl status nichijou` |
| 起動/停止/再起動 | `sudo systemctl start/stop/restart nichijou` |
| ログ確認 | `journalctl -u nichijou -f` |
| 自動起動を無効化 | `sudo systemctl disable --now nichijou` |

自動起動が不調なときの手起動フォールバック:

```bash
./start.sh   # 二重起動チェック付きで起動
./stop.sh    # start.sh で起動した分を停止
```

## 構成

```
nichijou/
├── pyproject.toml
├── uv.lock
├── .gitignore                  # *.db / photos/ / secrets/ / logs/ を除外
├── README.md
├── LICENSE
├── install-autostart.sh        # 自動起動セットアップ（sudo で1回実行）
├── start.sh / stop.sh          # 手動起動フォールバック
├── windows/
│   └── nichijou-autostart.vbs  # Windowsログイン時にWSLを起こすトリガ
├── secrets/                    # APIキー等（gitignore・非公開）
├── photos/                     # 観察写真の実体（gitignore・非公開）
├── nichijou.db                 # 日乗＋観察のメタデータ（gitignore・非公開）
└── app/
    ├── main.py                 # ルーティング（日乗 / 観察）
    ├── db.py                   # 保存層（ここだけが SQLite を知る）
    ├── vision.py               # 写真の種類判定（Anthropic vision）
    └── templates/
        ├── index.html          # 日乗（入力＋一覧）
        └── observations.html   # 観察（写真アップロード＋判定一覧）
```

## ライセンス

MIT License. [LICENSE](LICENSE) を参照。
