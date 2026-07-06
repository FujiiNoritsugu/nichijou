# nichijou 投函口 — Windows → WSL2 ポート転送の張り直し（Windows ログオン時に実行）
#
# WSL2 は NAT 配下で IP が再起動ごとに変わるため、LAN からの :8000 着信を
# 現在の WSL の IP へ転送し直す。あわせて:
#   - `wsl hostname -I` の呼び出しが WSL 自体を起こす（旧 nichijou-autostart.vbs の役割を内包）
#   - プライベートネットワークからの TCP 8000 を許可するファイアウォール規則を冪等に確保
#
# ※ netsh portproxy と New-NetFirewallRule は管理者権限が必須。
#   タスクスケジューラに「最上位の特権で実行」「ログオン時」で登録すること（README 参照）。
#   非既定ディストロを使う場合は下の `wsl` 呼び出しに `-d <DistroName>` を足す。

$ErrorActionPreference = 'Stop'
$Port = 8000

# --- 現在の WSL の IP を取得（この呼び出しで WSL も起動する） ---
$wslIp = (wsl hostname -I).Trim().Split(' ')[0]
if (-not $wslIp) {
    Write-Error "WSL の IP を取得できませんでした。WSL が起動しているか確認してください。"
    exit 1
}

# --- portproxy を現在の IP へ張り直す（毎回 delete → add で確実に最新化） ---
netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add    v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=$Port connectaddress=$wslIp | Out-Null

# --- ファイアウォール規則（プライベートのみ・冪等） ---
if (-not (Get-NetFirewallRule -DisplayName "nichijou WSL $Port" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName "nichijou WSL $Port" -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
}

Write-Host "nichijou: portproxy 0.0.0.0:$Port -> ${wslIp}:$Port を設定しました。"
