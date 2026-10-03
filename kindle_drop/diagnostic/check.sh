#!/bin/sh
# Read the system; write only a report in USB storage. No network or OTA changes.
ROOT=/mnt/us
HERE="$ROOT/extensions/kindle-drop-check"
REPORT="$ROOT/kindle-drop-report.txt"
sha() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }
field() { printf '%s=%s\n' "$1" "$2"; }
{
    field schema 1
    field challenge "$(cat "$HERE/challenge.txt" 2>/dev/null)"
    field uid "$(id -u)"
    field checker_sha "$(sha "$HERE/check.sh")"
    field version_sha "$(sha "$ROOT/system/version.txt")"
    field plugin_sha "$(sha "$ROOT/koreader/plugins/SSH.koplugin/main.lua")"
    field dropbear_sha "$(sha "$ROOT/koreader/dropbear")"
    field sftp_sha "$(sha "$ROOT/koreader/sftp-server")"
    if [ ! -e /usr/bin/otaupd ] && [ ! -e /usr/bin/otav3 ] &&
       [ -f /usr/bin/otaupd.bck ] && [ -f /usr/bin/otav3.bck ]; then
        field ota renamed
    else field ota unknown; fi
    if pidof otaupd otav3 >/dev/null 2>&1; then field ota_processes running;
    elif command -v pidof >/dev/null 2>&1; then field ota_processes none;
    else field ota_processes unknown; fi
    pending=none
    for path in "$ROOT"/update*.bin "$ROOT"/update.bin.tmp.partial; do
        if [ -e "$path" ]; then pending=present; fi
    done
    field pending_updates "$pending"
    if [ -x "$ROOT/koreader/dropbear" ]; then field dropbear_executable yes;
    else field dropbear_executable no; fi
    if [ -x "$ROOT/koreader/sftp-server" ]; then field sftp_executable yes;
    else field sftp_executable no; fi
    if [ -w "$ROOT" ]; then field storage_writable yes; else field storage_writable no; fi
    field free_kb "$(df -Pk "$ROOT" | awk 'END {print $4}')"
    settings="$ROOT/koreader/settings.reader.lua"
    if grep -q 'SSH_allow_no_password' "$settings" 2>/dev/null; then
        if grep -Eq '\["SSH_allow_no_password"\][[:space:]]*=[[:space:]]*false' "$settings"; then
            field ssh_bypass off
        else field ssh_bypass unknown; fi
    else field ssh_bypass off; fi
    if grep -q 'SSH_autostart' "$settings" 2>/dev/null; then
        if grep -Eq '\["SSH_autostart"\][[:space:]]*=[[:space:]]*false' "$settings"; then
            field ssh_autostart off
        else field ssh_autostart unknown; fi
    else field ssh_autostart off; fi
    runtime=off
    if [ -f /tmp/dropbear_koreader.pid ]; then
        pid="$(cat /tmp/dropbear_koreader.pid)"
        case "$pid" in ''|*[!0-9]*) runtime=unknown;;
        *)
            if [ -r "/proc/$pid/cmdline" ]; then
                if tr '\000' '\n' < "/proc/$pid/cmdline" | grep -Eq '^-[^-]*n'; then runtime=on; fi
            fi;;
        esac
    fi
    field ssh_runtime_bypass "$runtime"
} > "$REPORT.tmp"
mv "$REPORT.tmp" "$REPORT"
sync
eips 0 2 'Kindle Drop: report saved. Reconnect USB.' 2>/dev/null || true
