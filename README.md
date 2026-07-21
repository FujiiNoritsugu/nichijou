# 日乗（nichijou）

飾らない日々の記録のための、個人用の小さな Web アプリ。二つのページからなる。

- **日乗** … 一行の短文をその場で書き留める記録。
- **観察** … 撮った写真を取り込むと、AI（Claude の vision）が種類を判定してラベルを付け、日付とともに蓄積する自然観察の記録。1 つの観察に複数の写真（同じ対象を別角度・別解像度で撮ったもの）をまとめられる。

## 設計の要点（技術的事実として）

- 一度保存した記録は**編集できない**（編集 UI を持たない）。**削除はできる**。
- 記録は生のまま時系列で並べる。日乗は本文をプレーンテキストで保存する。
- 観察の判定結果は手で直さない。ただし AI の判定が誤ったときのために、**再判定（AI に問い直す）**だけはできる。
- 日乗と観察は同一アプリ内の別ページとして分離している。
- **1 回の投函＝1 つの観察**。複数枚を投函すると「同じ対象の複数アングル」とみなし、まとめて 1 回の判定に渡す（1 観察 = 1 判定）。別の対象は分けて投函する。
- 投函後に写真を追加・削除したり、別々の観察をあとから束ねたりはできない（やり直したいときは投函し直す）。1 観察あたり **8 枚**まで。

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
- 観察の写真は `photos/` に保存し、DB にはファイル名・判定結果・撮影/登録日時などのメタデータのみを持つ。写真は `observation_photos` テーブルが観察と 1 対多で持ち（撮影日時は写真ごと）、判定結果は観察側に 1 つだけ持つ。
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

## スマホから写真を投函する（投函口）

散歩で撮った写真を、帰宅後に家の Wi-Fi から**スマホのブラウザで直接投函**するための専用ルート。取り込み処理（保存→EXIF→AI判定→DB）は観察と同一。

**守っている線（設計の核）**

境界は「**どのポートを LAN に転送するか**」で引く。**送信元 IP では判定しない**（後述の理由により WSL2 では PC からの接続と LAN からの接続を送信元 IP で区別できないため）。日記と投函口を**別々の systemd サービス／別ポート**として動かす。

| | バインド | LAN 公開 | 中身 |
|---|---|---|---|
| **日記** `nichijou.service` | `127.0.0.1:8000` | しない | 全機能（日乗・観察・写真閲覧） |
| **投函口** `nichijou-dropbox.service` | `0.0.0.0:8001` | portproxy で 8001 だけ転送 | `/u/<token>` のみ。他は 404 |

- **日記は `127.0.0.1` バインド**なので LAN からは到達不能（WSL2 は NAT 配下で、転送していないポートは LAN に見えない）。Windows のブラウザからは従来どおり `localhost:8000` で開ける（WSL の localhost 転送）。
- **投函口インスタンスは `NICHIJOU_LAN_ONLY=1`** で起動し、`/u/` 以外の全ルートを **404** にする（母屋はこのインスタンスに存在しないものとして扱い、日記の存在自体を晒さない）。LAN に転送するのはこの 8001 だけ。
- トークンは URL に含まれるが、アクセスログには `/u/<redacted>` と伏字化される（生の値はログに残らない）。

> **なぜ送信元 IP 判定にしないのか**: WSL2 は NAT 配下のため、Windows ホスト経由の接続（`localhost` 転送）も LAN のスマホ（portproxy 経由）も、WSL アプリから見ると送信元が**同じゲートウェイ IP**（例 `172.x.0.1`）になる。両者を送信元で区別できないので、「日記は PC だけ・投函口だけ LAN」をポート／バインドの分離で担保する。この方式は `.wslconfig` のネットワークモードにも依存しない。

### 1. 投函トークンを作る

`secrets/`（gitignore・700）に 64 文字の hex を置く。これが URL の `<token>` になる。

```bash
python -c "import secrets; print(secrets.token_hex(32))" > secrets/upload.token
chmod 600 secrets/upload.token
```

トークンが無い／空のときは投函口は**全て 404**（＝トークンを置かない限り投函口は存在しない）。値を変えたいときはこのファイルを書き換えて `sudo systemctl restart nichijou-dropbox` する。

### 2. 二つのサービスを立てる

`install-autostart.sh` が日記（127.0.0.1:8000）と投函口（0.0.0.0:8001）の二つの systemd サービスを生成・起動する。再実行で反映:

```bash
sudo ./install-autostart.sh
systemctl status nichijou nichijou-dropbox   # 両方 active か確認
```

> `start.sh`（手起動フォールバック）は日記のみ・`127.0.0.1` のまま。投函口は systemd 経路（`nichijou-dropbox.service`）でのみ有効。

### 3. Windows 側の転送設定（WSL2 は NAT 配下のため必要）

`windows/nichijou-portproxy.ps1` が、現在の WSL の IP へ **`:8001`（投函口）だけ**を転送し直し、ファイアウォール（**信頼ネットワーク＝ Domain + Private のみ**許可。Public は不許可）を冪等に確保する。**WSL の IP は再起動ごとに変わる**ため、これを**ログオンのたびに昇格実行**する。

> **8000 は転送しないこと。** 日記の 8000 を portproxy すると Windows 自身の `localhost:8000` まで奪ってしまい、日記が開けなくなる（スクリプトは古い 8000 転送があれば自動削除する）。

> ネットワーク分類について: 家の Wi-Fi が Windows 上で「ドメイン認証済み（DomainAuthenticated）」と判定される環境では、分類を Private に手動変更できない（グループポリシーでロックされる）。この場合はドメイン プロファイルのファイアウォール規則が使われるため、本スクリプトは **Domain と Private の両方**を許可する（喫茶店等の Public は許可しない＝露出しない）。

**(a) スクリプトを Windows 側へコピー**（`\\wsl$` 直参照はログオン初期に不安定なため、ローカルに置く）:

```powershell
# 通常の PowerShell（<Distro> は `wsl -l -q` で確認）
Copy-Item "\\wsl$\<Distro>\home\fujii\nichijou\windows\nichijou-portproxy.ps1" `
          "$env:USERPROFILE\nichijou-portproxy.ps1"
```

**(b) タスクスケジューラに「ログオン時・最上位の特権」で登録**（管理者 PowerShell、一度だけ。portproxy/FW は管理者権限が必須）:

```powershell
$ps1 = "$env:USERPROFILE\nichijou-portproxy.ps1"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName "nichijou portproxy" -Action $action `
  -Trigger $trigger -Principal $principal -Force
```

これで Windows ログオンのたびに WSL 起動＋portproxy 張り直し＋FW 確保が昇格実行される。**旧 `windows/nichijou-autostart.vbs`（WSL を起こすだけ）の役割は本スクリプトが内包する**ため、スタートアップに置いていた場合は外してよい。

**(c) 動作確認（初回は手動で一度実行）**:

```powershell
# 管理者 PowerShell
# 直接 & で呼ぶとマシンの実行ポリシー（Restricted 等）で弾かれるため、
# タスク登録時と同じく -ExecutionPolicy Bypass 付きで呼ぶ（ポリシー自体は変えない）。
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\nichijou-portproxy.ps1"
netsh interface portproxy show v4tov4   # 0.0.0.0:8001 -> <WSLのIP>:8001 を確認
```

> 実行ポリシーの意味: 既定の `Restricted` は `.ps1` ファイルの実行を禁じる。上のように
> `-ExecutionPolicy Bypass` を**そのプロセスにだけ**渡せば、システム設定を変えずに実行できる
> （タスクスケジューラの登録コマンドも同じ方式なので、ログオン時の自動実行はこのままで動く）。
> 恒久的にポリシーを緩めたい場合のみ `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を検討。

### 4. スマホから開く

**PC の LAN 内 IP を確認**（Wi-Fi アダプタの `192.168.x.x` 等）:

```powershell
Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object {$_.PrefixOrigin -eq "Dhcp"} |
  Select-Object IPAddress, InterfaceAlias
```

スマホ（PC と同じ Wi-Fi）のブラウザで次を開く（**ポートは 8001**）:

```
http://<PCのLAN内IP>:8001/u/<token>
```

写真を選んで「投函する」を押すと、観察へ取り込まれ「n 枚を 1 つの観察として受け付けました」が表示される。カメラロールから複数選択可（`accept="image/*"`、受理は jpg/png のみ）。**複数枚は同じ対象の写真としてまとめて判定される**ので、別の対象は分けて投函する。1 回の上限は 8 枚・合計 64 MiB。

**ホーム画面に追加**（Android Chrome）: 上記 URL を開く → ⋮ メニュー →「ホーム画面に追加」。タイトルは「写真投函」。以後アイコンから一発で開ける。

## 構成

```
nichijou/
├── pyproject.toml
├── uv.lock
├── .gitignore                  # *.db / photos/ / secrets/ / logs/ を除外
├── README.md
├── LICENSE
├── install-autostart.sh        # 自動起動セットアップ（日記=127.0.0.1:8000 と投函口=0.0.0.0:8001 の2サービス）
├── start.sh / stop.sh          # 手動起動フォールバック（日記のみ・127.0.0.1）
├── windows/
│   ├── nichijou-autostart.vbs   # （旧）Windowsログイン時にWSLを起こすトリガ
│   └── nichijou-portproxy.ps1   # ログオン時に 8001 を現WSL-IPへ転送（要・昇格。8000は転送しない）
├── secrets/                    # APIキー・投函トークン（gitignore・非公開）
│   ├── anthropic.env            #   ANTHROPIC_API_KEY
│   └── upload.token             #   投函口トークン（64 hex）
├── photos/                     # 観察写真の実体（gitignore・非公開）
├── nichijou.db                 # 日乗＋観察のメタデータ（gitignore・非公開）
└── app/
    ├── main.py                 # ルーティング（日乗 / 観察 / 投函口）＋ 投函口専用モード（NICHIJOU_LAN_ONLY）
    ├── db.py                   # 保存層（ここだけが SQLite を知る）
    ├── vision.py               # 写真の種類判定（同じ対象の複数枚を 1 コールで渡す）
    └── templates/
        ├── index.html          # 日乗（入力＋一覧）
        ├── observations.html   # 観察（写真アップロード＋判定一覧）
        └── upload.html         # 投函口（スマホ向け・投函専用。一覧なし）
```

## ライセンス

MIT License. [LICENSE](LICENSE) を参照。
