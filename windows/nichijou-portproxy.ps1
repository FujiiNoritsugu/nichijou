# nichijou drop-box - refresh Windows->WSL2 port forwarding (run at Windows logon)
#
# Forwards the LAN's inbound :8001 (drop-box) to the current WSL IP. WSL2 is behind
# NAT and its IP changes on every restart, so this must run at each logon.
#
# IMPORTANT: only the drop-box port (8001) is forwarded to the LAN. The journal runs
# on 127.0.0.1:8000 inside WSL and is NOT forwarded, so it stays PC-only (NAT isolates
# it). Do NOT add a portproxy for 8000: doing so would also shadow the Windows host's
# own localhost:8000 and break journal access.
#
# Also:
#   - calling `wsl hostname -I` boots WSL itself (replaces the old .vbs boot trigger)
#   - ensures a firewall rule allowing inbound TCP 8001 from trusted profiles
#
# NOTE (ASCII only on purpose): Windows PowerShell 5.1 reads a BOM-less .ps1 as the
#   system ANSI code page (e.g. CP932), which corrupts non-ASCII bytes and breaks
#   parsing. Keep this file pure ASCII. Japanese docs live in README.md instead.
#
# netsh portproxy and New-NetFirewallRule require administrator rights. Register this
# in Task Scheduler as "Run with highest privileges" + "At log on" (see README).
# For a non-default distro, add `-d <DistroName>` to the `wsl` call below.

$ErrorActionPreference = 'Stop'
$Port = 8001

# --- get the current WSL IP (this call also boots WSL) ---
$wslIp = (wsl hostname -I).Trim().Split(' ')[0]
if (-not $wslIp) {
    Write-Error "Could not get the WSL IP. Make sure WSL is running."
    exit 1
}

# --- re-point portproxy to the current IP (delete then add every time) ---
netsh interface portproxy delete v4tov4 listenport=$Port listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add    v4tov4 listenport=$Port listenaddress=0.0.0.0 connectport=$Port connectaddress=$wslIp | Out-Null

# --- firewall rule (trusted profiles only: Domain + Private; NOT Public) ---
# Public (untrusted networks like cafe Wi-Fi) stays blocked. A home network that
# is domain-authenticated uses the Domain profile, so include it too.
# Idempotent: create if missing, otherwise re-assert the profile on every run.
$rule = Get-NetFirewallRule -DisplayName "nichijou WSL $Port" -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -DisplayName "nichijou WSL $Port" -Direction Inbound `
        -Action Allow -Protocol TCP -LocalPort $Port -Profile Domain,Private | Out-Null
} else {
    Set-NetFirewallRule -DisplayName "nichijou WSL $Port" -Profile Domain,Private | Out-Null
}

# --- clean up any leftover 8000 forwarding from earlier setups (would break journal) ---
netsh interface portproxy delete v4tov4 listenport=8000 listenaddress=0.0.0.0 2>$null | Out-Null

Write-Host "nichijou: portproxy 0.0.0.0:$Port -> ${wslIp}:$Port is set."
