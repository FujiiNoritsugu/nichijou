' nichijou autostart trigger (runs at Windows login)
' Boot the default WSL distro silently; systemd starts nichijou.service inside WSL.
' The service runs as a systemd *system* service, so the boot trigger does not
' need to specify a user. If you use a non-default distro, add: -d <DistroName>.
' Window hidden (0), do not wait (False).
Set sh = CreateObject("WScript.Shell")
sh.Run "wsl.exe -e true", 0, False
