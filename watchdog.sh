#!/usr/bin/env bash
# Farm console watchdog - keeps the target app alive.
# Paths and package are overridable with environment variables, see README.

PKG="${FARM_PKG:-com.roblox.client}"
WATCH_LOG="${FARM_WATCH_LOG:-$(dirname "$0")/watchdog.log}"
ADB="${FARM_ADB:-/usr/bin/adb}"

log() { echo "[$(date '+%F %T')] $*" >> "$WATCH_LOG"; }

log "=== watchdog started ==="

while true; do
    # wait for the device to come back after USB hiccups or reboots
    timeout 20 "$ADB" wait-for-device >/dev/null 2>&1

    pid=$(timeout 15 "$ADB" shell pidof "$PKG" 2>/dev/null | tr -d '\r')
    if [ -z "$pid" ]; then
        timeout 15 "$ADB" shell input keyevent KEYCODE_WAKEUP 2>/dev/null
        timeout 15 "$ADB" shell wm dismiss-keyguard 2>/dev/null
        timeout 15 "$ADB" shell monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1
        sleep 5
        pid2=$(timeout 15 "$ADB" shell pidof "$PKG" 2>/dev/null | tr -d '\r')
        log "${PKG} was dead -> relaunch ${pid2:+=pid $pid2 (ok)}"
    fi

    sleep "${FARM_WATCH_INTERVAL:-60}"
done
