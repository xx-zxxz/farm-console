# Farm console

A small self-hosted web panel for keeping an Android device and one target app
running continuously over adb: live status, restarts, watchdog, battery and
thermal visibility, and remote device controls. Python standard library only,
no dependencies.

## Layout

- `dashboard.py` - the web server and single-page UI (serves on `127.0.0.1:8088`)
- `watchdog.sh`  - babysits the app: relaunches it if it dies, waits out USB hiccups

## Requirements

- Linux (or anything running Python 3 and adb)
- `adb` from android-tools / android-platform-tools
- one device connected and authorized for USB debugging

## Setup

1. Enable USB debugging on the device, plug it in, accept the authorization dialog.
2. Point the env vars below at your paths and packages.
3. Start the watchdog and the panel:

```sh
setsid nohup bash watchdog.sh >/dev/null 2>&1 &
setsid nohup python3 dashboard.py >/dev/null 2>&1 &
```

Open http://localhost:8088

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `FARM_ADB` | `/usr/bin/adb` | adb binary |
| `FARM_PKG` | `com.roblox.client` | app the watchdog relaunches |
| `FARM_WATCH_SCRIPT` | `watchdog.sh` | script the panel (re)starts |
| `FARM_WATCH_PID` | `watchdog.pid` | watchdog pid file |
| `FARM_WATCH_LOG` | `watchdog.log` | watchdog log file |
| `FARM_CFG` | `farm-config.json` | panel settings (thermal guard) |
| `FARM_SHOTS` | `shots` | screenshot output directory |
| `FARM_HOST` | `127.0.0.1` | bind address |
| `FARM_PORT` | `8088` | bind port |
| `FARM_WATCH_INTERVAL` | `60` | watchdog loop seconds |

## What the panel does

- status: app state, pid, uptime, cpu, memory, threads, foreground activity
- checks: six automated preconditions (app running, screen timeout disabled,
  battery saver off, doze whitelist entry, watchdog alive, power source)
- actions: relaunch, focus, stop, clear data, screen timeout disable/enable,
  brightness, battery saver, do-not-disturb, remote key events, screenshots,
  doze whitelist management, background policy (appops), reboots
- watchdog management with relaunch counters and live log
- 2h ring-buffer trend charts (battery, app cpu, temperature, memory)
- thermal guard: server-side job that dims the screen above a configured
  temperature and logs the event

## HTTP API

```
GET  /stats          full status JSON
GET  /history        trend buffer JSON
GET  /log            watchdog log tail      (also /logdl for full download)
GET  /farmlog        filtered device logcat
GET  /shot/latest.png    device screenshot
GET  /api-docs           list of action names
POST /action/<name>      see ACTIONS in dashboard.py
```

## Systemd example

```ini
# farm.service
[Unit]
Description=farm console panel
After=network.target

[Service]
WorkingDirectory=/opt/farm-console
ExecStart=/usr/bin/python3 /opt/farm-console/dashboard.py
Restart=always
User=cz

[Install]
WantedBy=multi-user.target
```

## Disclaimer

This project is not affiliated with Roblox or Xiaomi. Automating games can
violate their terms of service; use it on devices and accounts you own and
understand the risk. No code here touches game internals - it only manages the
device (power, fans of dying apps, screenshots, inputs).

## License

MIT
