#!/usr/bin/env python3
"""Farm console - android device control panel over adb.

Run:  python3 dashboard.py
URL:  http://localhost:8088  (binds 127.0.0.1; change HOST below)

Optional environment overrides:
  FARM_ADB, FARM_PKG, FARM_WATCH_SCRIPT, FARM_WATCH_PID, FARM_WATCH_LOG,
  FARM_CFG, FARM_SHOTS, FARM_PORT, FARM_HOST
"""
import json, os, re, signal, subprocess, threading, time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADB = os.environ.get("FARM_ADB", "/usr/bin/adb")
PKG = os.environ.get("FARM_PKG", "com.roblox.client")
MOD_PKGS = ["com.retrivedmods.wclient", "app.revenge"]
WATCH_SCRIPT = os.environ.get("FARM_WATCH_SCRIPT", "/home/cz/roblox-watch.sh")
WATCH_PID = os.environ.get("FARM_WATCH_PID", "/home/cz/roblox-watch.pid")
WATCH_LOG = os.environ.get("FARM_WATCH_LOG", "/home/cz/roblox-watch.log")
CFG_FILE = os.environ.get("FARM_CFG", "/home/cz/roblox-dash-config.json")
SHOT_DIR = os.environ.get("FARM_SHOTS", "/home/cz/roblox-shots")
HOST = os.environ.get("FARM_HOST", "127.0.0.1")
PORT = int(os.environ.get("FARM_PORT", "8088"))
START_TS = time.time()

_adb_lock = threading.Lock()
_cpu_prev = {"ts": 0.0, "ticks": None}
_cache = {}
HIST = {k: deque(maxlen=720) for k in ("t", "level", "temp", "cpu", "mem")}
HIST_LOCK = threading.Lock()
CFG = {"temp_guard": True, "temp_limit": 40.0, "dim_to": 60}

def load_cfg():
    global CFG
    try:
        CFG.update(json.load(open(CFG_FILE)))
    except Exception:
        pass
    save_cfg()

def save_cfg():
    json.dump(CFG, open(CFG_FILE, "w"), indent=1)

def adb(*args, timeout=10):
    with _adb_lock:
        try:
            p = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
            return (p.stdout or "") + (p.stderr or "")
        except Exception as e:
            return "__ERR__" + str(e)

def adb_bytes(*args, timeout=25):
    with _adb_lock:
        try:
            p = subprocess.run([ADB, *args], capture_output=True, timeout=timeout)
            return p.stdout or b""
        except Exception:
            return b""

def _int(key, text):
    m = re.search(key + r":\s*(-?\d+)", text)
    return int(m.group(1)) if m else None

def cached(key, ttl, fn):
    now = time.time()
    with _adb_lock:
        hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    v = fn()
    with _adb_lock:
        _cache[key] = (now + ttl, v)
    return v

def get_static():
    def f():
        kv = {}
        kv["model"] = adb("shell", "getprop", "ro.product.model").strip() or "?"
        kv["android"] = adb("shell", "getprop", "ro.build.version.release").strip()
        kv["patch"] = adb("shell", "getprop", "ro.build.version.security_patch").strip()
        kv["hyperos"] = (adb("shell", "getprop", "ro.mi.os.version.name").strip()
                         or adb("shell", "getprop", "ro.miui.ui.version.name").strip())
        kv["kernel"] = adb("shell", "uname", "-r").strip()
        return {k: (v if v and not v.startswith("__ERR__") else "") for k, v in kv.items()}
    return cached("static", 3600, f)

def get_versions():
    def f():
        out = adb("shell", "dumpsys", "package", PKG)
        ver = re.search(r"versionName=([\w.\-]+)", out)
        inst = re.search(r"firstInstallTime=([\d\-T:.]+)", out)
        upd = re.search(r"lastUpdateTime=([\d\-T:.]+)", out)
        return {"version": ver.group(1) if ver else None,
                "installed": inst.group(1) if inst else None,
                "updated": upd.group(1) if upd else None}
    return cached("versions", 1800, f)

def get_battery():
    out = adb("shell", "dumpsys", "battery")
    temp, status = _int("temperature", out), _int(r"\bstatus\b", out)
    maxcur = _int(r"Max charging current", out)
    volt = re.search(r"(?m)^[ \t]*voltage: *(-?[\d.,]+)", out)
    return {"level": _int(r"\blevel\b", out), "temp_c": round(temp / 10, 1) if temp is not None else None,
            "charging": status == 2, "full": status == 5, "status": status,
            "voltage_mv": int(volt.group(1).replace(",", "")) if volt else None,
            "max_charge_ma": round(maxcur / 1000) if maxcur else None}

def get_screen():
    m = re.search(r"mWakefulness=(\w+)", adb("shell", "dumpsys", "power"))
    to = adb("shell", "settings", "get", "system", "screen_off_timeout").strip()
    br = adb("shell", "settings", "get", "system", "screen_brightness").strip()
    return {"awake": m.group(1) == "Awake" if m else None, "keep_awake": to == "2147483647",
            "timeout_ms": int(to) if to.isdigit() else None,
            "brightness": int(br) if br.isdigit() else None}

def get_focus():
    out = adb("shell", "dumpsys", "window")
    fg = re.search(r"mCurrentFocus=Window\{[^}]*\s([\w.]+)/([\w.$]+)", out)
    return {"package": fg.group(1) if fg else None, "activity": fg.group(2) if fg else None}

def get_app():
    pid = adb("shell", "pidof", PKG).strip()
    if not pid:
        return {"running": False}
    pid = pid.split()[0]
    q = "awk '{print $14, $15, $20, $22}' /proc/%s/stat; echo __U__; awk '{print $1}' /proc/uptime" % pid
    parts = adb("shell", q).split("__U__")
    age = thr = cpu = None
    try:
        ut, stime, nthreads, start = (int(x) for x in parts[0].split())
        uptime = float(parts[1].split()[0])
        age = max(0, int(uptime - start / 100))
        thr = nthreads
        tot = ut + stime
        global _cpu_prev
        if _cpu_prev["ticks"] is not None:
            dt = max(time.time() - _cpu_prev["ts"], 0.0001)
            dtk = tot - _cpu_prev["ticks"]
            if 0 < dtk < uptime * 100:
                cpu = round(dtk / 100 / dt * 100, 1)
        _cpu_prev = {"ts": time.time(), "ticks": tot}
    except Exception:
        pass
    mem = _int(r"TOTAL PSS", adb("shell", "dumpsys", "meminfo", PKG))
    return {"running": True, "pid": int(pid), "session_s": age, "threads": thr,
            "cpu_pct": cpu, "mem_mb": round(mem / 1024, 1) if mem else None}

def get_ram():
    def f():
        out = adb("shell", "cat", "/proc/meminfo")
        tot = _int("MemTotal", out) or 0
        avail = _int("MemAvailable", out) or 0
        return {"total_gb": round(tot / 1048576, 2), "avail_gb": round(avail / 1048576, 2),
                "used_pct": int(round(100 * (1 - avail / tot))) if tot else None}
    return cached("meminfo", 30, f)

def get_storage():
    def f():
        out = adb("shell", "df", "/data")
        row = next((l for l in out.splitlines() if "/data" in l and not l.startswith("Filesystem")), "")
        m = re.search(r"(\d+)%", row)
        return {"used_pct": int(m.group(1)) if m else None}
    return cached("storage", 300, f)

def get_top():
    out = adb("shell", "top", "-b", "-n", "1", "-m", "8", timeout=15)
    rows = []
    for line in out.splitlines():
        f_ = line.split()
        if len(f_) < 9 or not f_[0].isdigit():
            continue
        cpu = None
        if len(f_) >= 9 and len(f_[7]) == 1 and f_[7].isalpha():
            cpu = f_[8]
        elif len(f_) > 7:
            cpu = f_[7]
        try:
            cpu = float(cpu) if cpu else 0.0
        except ValueError:
            cpu = 0.0
        rows.append({"pid": int(f_[0]), "name": f_[-1], "cpu": cpu})
    rows.sort(key=lambda r: -r["cpu"])
    return rows[:6]

def get_doze():
    return [l for l in adb("shell", "dumpsys", "deviceidle", "whitelist").strip().splitlines() if l.strip()]

def get_saver():
    return adb("shell", "settings", "get", "global", "low_power").strip() == "1"

def get_dnd():
    v = adb("shell", "settings", "get", "global", "zen_mode").strip()
    return {"on": v not in ("0", "null", ""), "mode": v}

def get_adb_state():
    try:
        with _adb_lock:
            p = subprocess.run([ADB, "get-state"], capture_output=True, text=True, timeout=8)
        return p.stdout.strip() or p.stderr.strip()
    except Exception:
        return "error"

def watchdog_alive():
    try:
        pid = int(open(WATCH_PID).read().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None

def relaunch_stats():
    today, n_today, n_all, last = time.strftime("%Y-%m-%d"), 0, 0, None
    try:
        for line in open(WATCH_LOG, errors="replace"):
            if " was dead" in line:
                n_all += 1
                if line.startswith("[" + today):
                    n_today += 1
                last = line.strip()
    except FileNotFoundError:
        pass
    return {"today": n_today, "total": n_all, "last": last}

def get_crashes():
    def f():
        out = adb("shell", "logcat", "-b", "crash", "-d", "-t", "2000", timeout=25)
        return sum(1 for l in out.splitlines()
                   if any(("Process: " + p) in l for p in [PKG] + MOD_PKGS))
    return cached("crashes", 60, f)

def get_farmlog():
    out = adb("shell", "logcat", "-d", "-t", "400", timeout=18)
    hits = [l for l in out.splitlines() if re.search(r"roblox|revenge|wclient", l, re.I)]
    return "\n".join(hits[-45:])

def get_uptime():
    try:
        return int(float(adb("shell", "cat", "/proc/uptime").split()[0]))
    except Exception:
        return None

def poll_history():
    try:
        b = get_battery()
        a = get_app()
        s = {"t": time.time(), "level": b.get("level"), "temp": b.get("temp_c"),
             "cpu": a.get("cpu_pct"), "mem": a.get("mem_mb")}
        if s["level"] is None and s["cpu"] is None and s["mem"] is None:
            return
        with HIST_LOCK:
            for k in ("t", "level", "temp", "cpu", "mem"):
                HIST[k].append(s[k])
        if (CFG.get("temp_guard") and s["temp"] and s["temp"] >= CFG.get("temp_limit", 40)
                and get_screen().get("brightness", 999) > CFG.get("dim_to", 60)):
            adb("shell", "settings", "put", "system", "screen_brightness", str(CFG.get("dim_to", 60)))
            with open(WATCH_LOG, "a") as f:
                f.write("[%s] AUTO-DIM (temp guard %.1fC -> brightness %s)\n"
                        % (time.strftime("%F %T"), s["temp"], CFG.get("dim_to", 60)))
    except Exception:
        pass

def history_out():
    with HIST_LOCK:
        return {k: list(v) for k, v in HIST.items()}

def checklist():
    b, sc, app_ = get_battery(), get_screen(), get_app()
    wd = watchdog_alive()
    return [
        {"name": "App running", "ok": app_.get("running", False),
         "detail": "pid %s" % app_.get("pid") if app_.get("running") else "terminated"},
        {"name": "Screen timeout disabled", "ok": sc.get("keep_awake", False),
         "detail": "timeout %s ms" % sc.get("timeout_ms")},
        {"name": "Battery saver off", "ok": not get_saver(), "detail": "low_power=0"},
        {"name": "App in doze whitelist", "ok": any(PKG in p for p in get_doze()), "detail": "deviceidle"},
        {"name": "Watchdog alive", "ok": wd is not None, "detail": "pid %s" % (wd or "-")},
        {"name": "Power source", "ok": bool(b.get("charging") or b.get("full")),
         "detail": "%s%% at %.1fC" % (b.get("level"), b.get("temp_c") or 0)},
    ]

def collect():
    return {"static": get_static(), "versions": get_versions(), "battery": get_battery(),
            "screen": get_screen(), "focus": get_focus(), "app": get_app(), "ram": get_ram(),
            "storage": get_storage(), "top": get_top(), "doze": get_doze(), "saver": get_saver(),
            "dnd": get_dnd(), "adb_state": get_adb_state(), "watchdog_pid": watchdog_alive(),
            "watchdog_running": watchdog_alive() is not None, "relaunches": relaunch_stats(),
            "crashes": get_crashes(), "checklist": checklist(), "cfg": CFG,
            "uptime_s": get_uptime(), "dash_uptime_s": int(time.time() - START_TS),
            "time": time.strftime("%H:%M:%S")}

# ---------------- actions ----------------
def _launch():
    adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")

def act_restart(_p):
    adb("shell", "am", "force-stop", PKG)
    time.sleep(1.5)
    _launch()
    return {"ok": True, "msg": "app restarted"}

def act_focus(_p):
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    _launch()
    return {"ok": True, "msg": "app brought to front"}

def act_kill(_p):
    adb("shell", "am", "force-stop", PKG)
    return {"ok": True, "msg": "app stopped (watchdog relaunches within 60s unless stopped)"}

def act_cleardata(_p):
    adb("shell", "pm", "clear", PKG)
    return {"ok": True, "msg": "app data wiped; watchdog relaunches fresh on next pass"}

def _set(wpid, _p):
    adb("shell", "settings", "put", "system", "screen_off_timeout", str(wpid))
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    return {"ok": True, "msg": "screen_off_timeout=%s" % wpid}

def act_keep_awake_on(_p):
    return _set(2147483647, _p)

def act_keep_awake_off(_p):
    return _set(600000, _p)

def act_brightness(p):
    v = max(1, min(255, int(p.get("value", 120))))
    adb("shell", "settings", "put", "system", "screen_brightness", str(v))
    return {"ok": True, "msg": "brightness=%d" % v}

def act_saver(p):
    on = bool(p.get("on"))
    adb("shell", "settings", "put", "global", "low_power", "1" if on else "0")
    adb("shell", "am", "broadcast", "-a", "android.intent.action.POWER_SAVE_MODE_CHANGED",
        "--ez", "on", "true" if on else "false")
    return {"ok": True, "msg": "battery saver %s" % ("on" if on else "off")}

def act_dnd(p):
    mode = "none" if p.get("mode", "priority") == "off" else p.get("mode", "priority")
    out = adb("shell", "cmd", "notification", "set_dnd", mode)
    return {"ok": "__ERR__" not in out, "msg": ("dnd -> " + mode + " " + out.strip())[:70]}

def act_key(p):
    keys = {"back": "KEYCODE_BACK", "home": "KEYCODE_HOME", "recents": "KEYCODE_APP_SWITCH",
            "wake": "KEYCODE_WAKEUP", "power": "KEYCODE_POWER", "vol_up": "KEYCODE_VOLUME_UP",
            "vol_down": "KEYCODE_VOLUME_DOWN", "mute": "KEYCODE_VOLUME_MUTE"}
    k = p.get("key", "")
    if k not in keys:
        return {"ok": False, "msg": "unknown key"}
    adb("shell", "input", "keyevent", keys[k])
    return {"ok": True, "msg": "sent " + k}

def act_doze(p):
    pkg = p.get("pkg", PKG)
    out = adb("shell", "dumpsys", "deviceidle", "whitelist", ("+" if p.get("add", True) else "-") + pkg)
    return {"ok": "__ERR__" not in out, "msg": out.strip()[:80]}

def act_appops(p):
    pkg = p.get("pkg", PKG)
    out = adb("shell", "appops", "set", pkg, "RUN_ANY_IN_BACKGROUND",
              "allow" if p.get("allow", True) else "ignore")
    return {"ok": True, "msg": (out.strip() or ("appops set " + pkg))[:70]}

def act_watchdog_stop(_p):
    pid = watchdog_alive()
    if pid:
        os.kill(pid, signal.SIGTERM)
        return {"ok": True, "msg": "watchdog stopped"}
    return {"ok": False, "msg": "watchdog not running"}

def act_watchdog_start(_p):
    if watchdog_alive():
        return {"ok": False, "msg": "already running"}
    p = subprocess.Popen(["setsid", "bash", WATCH_SCRIPT], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    open(WATCH_PID, "w").write(str(p.pid))
    return {"ok": True, "msg": "watchdog started pid %d" % p.pid}

def act_reboot(p):
    args = {"normal": ["reboot"], "recovery": ["reboot", "recovery"],
            "bootloader": ["reboot", "bootloader"]}[p.get("mode", "normal")]
    out = adb(*args)
    return {"ok": "__ERR__" not in out, "msg": "rebooting (%s)" % p.get("mode", "normal")}

def act_setup(_p):
    r = [act_keep_awake_on({}), act_saver({"on": False}), act_doze({"pkg": PKG, "add": True}),
         act_appops({"pkg": PKG, "allow": True}), act_watchdog_start({})]
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    _launch()
    ok = all(x.get("ok") for x in r)
    return {"ok": ok, "msg": "setup applied: %s" % "; ".join(x.get("msg", "")[:36] for x in r)}

def act_clear_log(_p):
    open(WATCH_LOG, "w").close()
    return {"ok": True, "msg": "watchdog log truncated"}

def act_cfg(p):
    for k in ("temp_guard", "temp_limit", "dim_to"):
        if k in p:
            CFG[k] = p[k]
    save_cfg()
    return {"ok": True, "msg": "saved %s" % json.dumps(CFG)}

ACTIONS = {"restart": act_restart, "focus": act_focus, "kill": act_kill, "cleardata": act_cleardata,
           "keep_awake_on": act_keep_awake_on, "keep_awake_off": act_keep_awake_off,
           "brightness": act_brightness, "saver": act_saver, "dnd": act_dnd, "key": act_key,
           "doze": act_doze, "appops": act_appops, "watchdog_start": act_watchdog_start,
           "watchdog_stop": act_watchdog_stop, "reboot": act_reboot, "setup": act_setup,
           "clear_log": act_clear_log, "cfg": act_cfg}

PNG_HDR = b"\x89PNG\r\n\x1a\n"

def take_screenshot():
    os.makedirs(SHOT_DIR, exist_ok=True)
    data = adb_bytes("exec-out", "screencap", "-p")
    if data[:8] != PNG_HDR:
        return False
    open(os.path.join(SHOT_DIR, "latest.png"), "wb").write(data)
    return True

# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _r(self, body, code=200, ctype="application/json", hdrs=None):
        b = body if isinstance(body, bytes) else str(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        if hdrs:
            self.send_header(hdrs[0], hdrs[1])
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            p = self.path.split("?")[0]
            if p == "/":
                self._r(HTML.encode(), ctype="text/html; charset=utf-8")
            elif p == "/stats":
                self._r(json.dumps(collect()))
            elif p == "/history":
                self._r(json.dumps(history_out()))
            elif p == "/log":
                try:
                    lines = open(WATCH_LOG, errors="replace").readlines()
                except Exception:
                    lines = []
                self._r("".join(lines[-60:]) or "(log empty)", ctype="text/plain; charset=utf-8")
            elif p == "/logdl":
                try:
                    full = open(WATCH_LOG, errors="replace").read()
                except Exception:
                    full = "(log missing)"
                self._r(full, ctype="text/plain; charset=utf-8",
                        hdrs=("Content-Disposition", "attachment; filename=watchdog.log"))
            elif p == "/farmlog":
                self._r(get_farmlog(), ctype="text/plain; charset=utf-8")
            elif p == "/shot/latest.png":
                if not take_screenshot():
                    self._r("screencap failed", 502, "text/plain")
                else:
                    with open(os.path.join(SHOT_DIR, "latest.png"), "rb") as f:
                        self._r(f.read(), ctype="image/png")
            elif p == "/api-docs":
                self._r(json.dumps(sorted(ACTIONS.keys())))
            else:
                self._r(json.dumps({"ok": False, "msg": "not found"}), 404)
        except Exception as e:
            self._r(json.dumps({"ok": False, "msg": str(e)}), 500)

    def do_POST(self):
        try:
            if not self.path.startswith("/action/"):
                return self._r(json.dumps({"ok": False, "msg": "bad path"}), 404)
            name = self.path.split("/action/")[1]
            ln = int(self.headers.get("Content-Length") or 0)
            params = json.loads(self.rfile.read(ln) or "{}") if ln else {}
            fn = ACTIONS.get(name)
            if not fn:
                return self._r(json.dumps({"ok": False, "msg": "unknown action"}), 400)
            self._r(json.dumps(fn(params)))
        except Exception as e:
            self._r(json.dumps({"ok": False, "msg": str(e)}), 500)

# ---------------- UI ----------------
HTML = """<!doctype html>
<html data-theme="dark"><head><meta charset="utf-8"><title>Farm console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1216;--bg2:#12161c;--card:#161b21;--line:#262d37;--txt:#d8dee7;--dim:#8a94a3;
--ok:#3f9e63;--bad:#d4544f;--acc:#4f7fd9;--warn:#bd8a3a;--chip:#1c222a}
[data-theme=light]{--bg:#f2f4f7;--bg2:#fafbfc;--card:#ffffff;--line:#d9dee5;--txt:#1d242e;--dim:#646f7d;
--ok:#217a4c;--bad:#bb3b37;--acc:#2f61b8;--warn:#95692b;--chip:#e9edf2}
*{box-sizing:border-box}body{margin:0;color:var(--txt);font:13.5px/1.5 system-ui,'Segoe UI',sans-serif;background:var(--bg2)}
#app{display:block}
.side{position:fixed;top:0;right:0;bottom:0;width:216px;background:var(--bg);border-left:1px solid var(--line);
display:flex;flex-direction:column;z-index:50}
.side .brand{display:flex;gap:9px;align-items:center;padding:16px 16px 12px;border-bottom:1px solid var(--line)}
.side .brand svg{flex:none;color:var(--acc)}
.side .brand b{font-size:13.5px;letter-spacing:-.1px}
.side .brand span{display:block;font-size:10.5px;color:var(--dim)}
nav{flex:1;overflow-y:auto;padding:8px}
nav button{display:flex;align-items:center;gap:9px;width:100%;background:none;border:none;color:var(--dim);
font-size:12.5px;font-weight:500;padding:9px 11px;cursor:pointer;border-radius:8px;text-align:left;margin:0}
nav button svg{flex:none;opacity:.9}
nav button:hover{color:var(--txt);background:var(--chip)}
nav button.act{color:var(--txt);background:var(--chip)}
nav button.act svg{color:var(--acc)}
.side .foot{border-top:1px solid var(--line);padding:10px}
.side .foot .row{gap:6px}
.wrap{margin-right:216px;min-height:100vh;display:flex;flex-direction:column}
.topbar{display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 18px;
border-bottom:1px solid var(--line);background:var(--bg2);position:sticky;top:0;z-index:40;flex-wrap:wrap}
.pill{font-size:11px;border:1px solid var(--line);border-radius:6px;padding:2px 9px;color:var(--dim);white-space:nowrap}
.pill svg{vertical-align:-2px;margin-right:4px}
main{padding:16px 18px 40px;max-width:1120px;width:100%;margin:0 auto;flex:1}
.tab{display:none}.tab.act{display:block}
.grid{display:grid;gap:12px}.c2{grid-template-columns:repeat(auto-fit,minmax(290px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:13px 15px}
.card h2{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin:0 0 10px;
display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.big{font-size:26px;font-weight:640;letter-spacing:-.3px;font-variant-numeric:tabular-nums}
.big small{font-size:12px;color:var(--dim);font-weight:400}
.val{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px solid var(--line);
font-size:12.5px}.val:last-child{border:none}
.val span{color:var(--dim)}
.val b{font-weight:550;text-align:right;word-break:break-word;font-variant-numeric:tabular-nums}
.bar{height:5px;background:var(--line);border-radius:3px;overflow:hidden;margin:6px 0}
.bar i{display:block;height:100%;background:var(--acc);transition:width .35s}
button{display:inline-flex;align-items:center;gap:6px;background:var(--chip);color:var(--txt);
border:1px solid var(--line);border-radius:7px;padding:6px 11px;cursor:pointer;font-size:12.5px;font-weight:500;
margin:2px 4px 2px 0;transition:border-color .15s}
button svg{opacity:.85}
button:hover{border-color:var(--acc);color:var(--acc)}
button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
button.pri:hover{color:#fff;filter:brightness(1.08)}
button.danger{border-color:var(--bad);color:var(--bad)}
button.danger:hover{background:var(--bad);color:#fff}
button.sm{padding:4px 8px;font-size:12px;margin:2px 3px}
input[type=range]{width:100%;accent-color:var(--acc)}
input[type=checkbox]{accent-color:var(--acc)}
input[type=text],input[type=number]{background:var(--bg);border:1px solid var(--line);color:var(--txt);
border-radius:7px;padding:6px 9px;font-size:12.5px}
select{background:var(--bg);border:1px solid var(--line);color:var(--txt);border-radius:7px;padding:5px;font-size:12.5px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em}
canvas{width:100%;height:64px;display:block}
.shot{width:100%;border-radius:8px;border:1px solid var(--line);cursor:zoom-in;background:#0b0d10;display:block}
.ck{display:flex;justify-content:space-between;gap:8px;padding:5px 0;font-size:12.5px;border-bottom:1px solid var(--line)}
.ck:last-child{border:none}
.ck .s{display:flex;gap:7px;align-items:center}
.small{font-size:12px}.dim{color:var(--dim)}
.tlow{font-size:11px}
#toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);z-index:99;display:flex;flex-direction:column;gap:8px;align-items:center}
#toast div{background:var(--card);color:var(--txt);border:1px solid var(--line);padding:8px 14px;border-radius:8px;
font-size:12.5px;max-width:80vw}
.hero{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.hero .st .h{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.09em}
.hero .st b{font-size:22px;font-variant-numeric:tabular-nums}
.stt{display:inline-flex;align-items:center;gap:6px}
.stt svg{flex:none}
.g-ok{color:var(--ok)}.g-bad{color:var(--bad)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;font:11.5px ui-monospace,monospace;
color:var(--dim);overflow:auto;white-space:pre-wrap;max-height:300px}
.logov{color:var(--dim)}
dialog{background:var(--card);border:none;border-radius:10px}
dialog::backdrop{background:rgba(0,0,0,.55)}
#dozep{flex:1}
@media (max-width:900px){
 .side{top:auto;bottom:0;left:0;right:0;width:auto;height:auto;flex-direction:row;align-items:center;
 border-left:none;border-top:1px solid var(--line)}
 .side .brand{display:none}
 nav{display:flex;flex:1;overflow-x:auto;padding:6px}
 nav button{width:auto;flex:none;font-size:12px;padding:8px 10px}
 .side .foot{border-top:none;padding:6px;display:flex}
 .wrap{margin-right:0;margin-bottom:56px}
 .topbar{padding:9px 12px}
}
</style></head>
<body><div id="app">
<aside class="side">
 <div class="brand"><span id="ic-logo"></span><div><b>Farm console</b><span>android adb panel</span></div></div>
 <nav id="tabs">
  <button data-t="home" class="act"><span data-icon="layout"></span>Status</button>
  <button data-t="session"><span data-icon="session"></span>Session</button>
  <button data-t="device"><span data-icon="device"></span>Device</button>
  <button data-t="svcs"><span data-icon="shield"></span>Services</button>
  <button data-t="logs"><span data-icon="term"></span>Logs</button>
  <button data-t="cfg"><span data-icon="sliders"></span>Config</button>
 </nav>
 <div class="foot">
  <div class="row">
   <select id="itv" aria-label="Poll interval"><option value="2000">2s</option>
   <option value="5000" selected>5s</option><option value="10000">10s</option></select>
   <button class="sm" id="pause" onclick="togPause()" aria-label="Pause polling"><span data-icon="pause"></span>pause</button>
   <button class="sm" onclick="theme()" aria-label="Toggle theme"><span data-icon="moon"></span>theme</button>
  </div>
  <div class="small dim logov" style="margin-top:8px" id="verline">v1.2</div>
 </div>
</aside>
<div class="wrap">
<div class="topbar">
 <div class="row">
  <span class="pill" id="dev"></span><span class="pill" id="usb"></span><span class="pill" id="clock"></span>
 </div>
 <div class="row">
  <span class="pill stt" id="appchip"></span>
  <span class="pill stt" id="wdchip"></span>
 </div>
</div>
<main>
<section class="tab act" id="t-home"><div class="grid c2">
<div class="card"><h2>App session</h2>
<div class="hero">
 <div class="st"><div class="h">state</div><b class="stt" id="ast"></b></div>
 <div class="st"><div class="h">uptime</div><b id="aup"></b></div>
 <div class="st"><div class="h">cpu / mem</div><b id="ares"></b></div>
 <div class="st"><div class="h">threads</div><b id="athr"></b></div>
</div>
<div class="row" style="margin-top:10px">
 <button class="pri" onclick="act('setup')"><span data-icon="play"></span>Apply farm setup</button>
 <button onclick="act('restart')"><span data-icon="refresh"></span>Relaunch</button>
 <button onclick="act('focus')"><span data-icon="cross"></span>Focus</button>
</div>
<div class="small dim" style="margin-top:8px">foreground: <span id="rfg">-</span></div></div>
<div class="card"><h2>Automated checks <span class="pill" id="cksum"></span></h2><div id="checks"></div></div>
<div class="card" style="grid-column:1/-1"><h2>Trends - 2h ring buffer, sampled every 10s</h2>
<div class="grid c3">
 <div><span class="pill">battery</span><canvas id="cvl" height="128"></canvas></div>
 <div><span class="pill">app cpu</span><canvas id="cvc" height="128"></canvas></div>
 <div><span class="pill">temp / mem</span><canvas id="cvd" height="128"></canvas></div>
</div></div>
<div class="card"><h2>Top processes</h2><table id="top"></table></div>
<div class="card"><h2>Device</h2>
<div class="val"><span>os</span><b id="os"></b></div>
<div class="val"><span>kernel</span><b id="kern"></b></div>
<div class="val"><span>uptime device / panel</span><b id="ups"></b></div>
<div class="val"><span>ram used</span><b id="ram"></b></div><div class="bar"><i id="rbar"></i></div>
<div class="val"><span>storage /data used</span><b id="sto"></b></div><div class="bar"><i id="sbar"></i></div>
<div class="val"><span>security patch</span><b id="pat"></b></div>
<div class="val"><span>relaunches today / total</span><b id="relc"></b></div></div>
</div></section>
<section class="tab" id="t-session"><div class="grid c2">
<div class="card"><h2>App</h2>
<div class="val"><span>package</span><b id="rpkg"></b></div>
<div class="val"><span>version</span><b id="rver"></b></div>
<div class="val"><span>installed / updated</span><b id="rins"></b></div>
<div class="row" style="margin-top:10px">
 <button class="pri" onclick="act('restart')"><span data-icon="refresh"></span>Restart</button>
 <button onclick="act('focus')"><span data-icon="cross"></span>Focus</button>
 <button class="danger" onclick="if(confirm('Stop the app?'))act('kill')"><span data-icon="stop"></span>Stop</button>
 <button class="danger" onclick="if(confirm('Wipe app data? Session and login will be lost.')&&confirm('Confirm clear data.'))act('cleardata')"><span data-icon="trash"></span>Clear data</button>
</div></div>
<div class="card"><h2>Crashes <span class="pill">since boot</span></h2>
<div class="big" id="crc"></div>
<div class="small dim">logcat crash buffer, app + companion packages</div></div>
<div class="card" style="grid-column:1/-1"><h2>Logcat monitor <span class="pill" id="flstamp"></span>
<button class="sm" onclick="farmlog()" aria-label="Refresh"><span data-icon="refresh"></span>refresh</button></h2>
<pre id="farmlog">(empty)</pre></div>
</div></section>
<section class="tab" id="t-device"><div class="grid c2">
<div class="card"><h2>Battery <span class="pill" id="bp"></span></h2>
<div class="big"><span id="bl"></span></div><div class="bar"><i id="bbr"></i></div>
<div class="val"><span>charging</span><b id="chg"></b></div>
<div class="val"><span>temperature</span><b id="bt"></b></div>
<div class="val"><span>voltage</span><b id="bv"></b></div>
<div class="val"><span>charger headroom</span><b id="bcu"></b></div></div>
<div class="card"><h2>Screen</h2>
<div class="val"><span>state</span><b id="scr"></b></div>
<div class="val"><span>auto sleep</span><b id="ka"></b></div>
<div class="row"><button class="pri" onclick="act('keep_awake_on')"><span data-icon="moon"></span>disable timeout</button>
<button onclick="act('keep_awake_off')"><span data-icon="timer"></span>10 min</button></div>
<input type="range" min="1" max="255" id="brs" oninput="$('brv').textContent=this.value"
onchange="act('brightness',{value:+this.value})">
<div class="small dim">brightness <b id="brv"></b></div></div>
<div class="card"><h2>Input & sound</h2>
<div class="row">
<button class="sm" onclick="key('back')">Back</button>
<button class="sm" onclick="key('home')">Home</button>
<button class="sm" onclick="key('recents')">Recents</button>
<button class="sm" onclick="key('wake')">Wake</button>
<button class="sm danger" onclick="key('power')">Power</button>
<button class="sm" onclick="key('vol_up')">Vol +</button>
<button class="sm" onclick="key('vol_down')">Vol -</button>
<button class="sm" onclick="key('mute')">Mute</button>
</div>
<div class="val" style="margin-top:8px"><span>do not disturb</span><b id="dnd"></b></div>
<div class="row">
<button class="sm" onclick="act('dnd',{mode:'priority'})">dnd on</button>
<button class="sm" onclick="act('dnd',{mode:'off'})">dnd off</button>
<button class="sm" onclick="act('saver',{on:true})">saver on</button>
<button class="sm" onclick="act('saver',{on:false})">saver off</button>
</div></div>
<div class="card"><h2>Screenshot <button class="sm" onclick="shot()" aria-label="Capture"><span data-icon="camera"></span>capture</button></h2>
<img class="shot" id="shotimg" alt="device screenshot"><div class="small dim" id="shott">click to open full size</div></div>
</div></section>
<section class="tab" id="t-svcs"><div class="grid c2">
<div class="card"><h2>Watchdog <span class="pill" id="wpid"></span></h2>
<div class="big stt" id="wst"></div>
<div class="row"><button class="pri" onclick="act('watchdog_start')"><span data-icon="play"></span>start</button>
<button onclick="act('watchdog_stop')"><span data-icon="stop"></span>stop</button></div>
<div class="val" style="margin-top:8px"><span>last relaunch</span><b class="small" id="lastrel"></b></div></div>
<div class="card"><h2>Doze whitelist</h2><div id="doze"></div>
<div class="row" style="margin-top:8px"><input type="text" id="dozep" placeholder="com.example.app">
<button class="sm pri" onclick="dozeAdd()"><span data-icon="plus"></span>add</button></div></div>
<div class="card"><h2>Background policy</h2>
<div class="small dim">RUN_ANY_IN_BACKGROUND appops per package</div>
<div class="row" style="margin-top:6px">
<button class="sm" onclick="ops('com.roblox.client',true)">roblox: allow</button>
<button class="sm" onclick="ops('com.roblox.client',false)">roblox: ignore</button>
<button class="sm" onclick="ops('app.revenge',true)">revenge: allow</button>
<button class="sm" onclick="ops('com.retrivedmods.wclient',true)">wclient: allow</button>
</div></div>
<div class="card"><h2>Reboot</h2>
<div class="row">
<button class="danger" onclick="rb('normal')"><span data-icon="power"></span>normal</button>
<button class="danger" onclick="rb('recovery')"><span data-icon="refresh"></span>recovery</button>
<button class="danger" onclick="rb('bootloader')"><span data-icon="refresh"></span>bootloader</button>
</div>
<div class="small dim">adb reconnects and the watchdog relaunches the app after recovery.</div></div>
</div></section>
<section class="tab" id="t-logs"><div class="grid">
<div class="card"><h2>Watchdog log
<button class="sm" onclick="logs()" aria-label="Refresh"><span data-icon="refresh"></span>refresh</button>
<button class="sm" onclick="act('clear_log')"><span data-icon="x"></span>truncate</button>
<button class="sm" onclick="window.open('/logdl')"><span data-icon="dl"></span>download</button></h2>
<input type="text" id="logf" placeholder="filter">
<pre id="log" style="max-height:420px"></pre></div>
<div class="card"><h2>Interface</h2>
<div class="row"><button class="pri" onclick="expJson()"><span data-icon="dl"></span>export snapshot</button></div>
<div class="small dim" id="eps2"></div></div></div></section>
<section class="tab" id="t-cfg"><div class="grid c2">
<div class="card"><h2>Thermal guard</h2>
<label class="row"><input type="checkbox" id="tgck"> auto-dim when hot</label>
<div class="row" style="margin-top:10px">
 at <input type="number" id="tglimit" min="30" max="55" step="0.5" value="40"> C
 brightness <input type="number" id="tgdim" min="5" max="255" value="60">
</div>
<button class="pri" style="margin-top:10px" onclick="cfgSave()"><span data-icon="save"></span>Save</button>
<div class="small dim">evaluated server side every 10s; writes an AUTO-DIM entry into the watchdog log.</div></div>
<div class="card"><h2>Appearance</h2>
<div class="val"><span>theme</span><span><button class="sm" onclick="theme()" aria-label="Theme"><span data-icon="moon"></span>toggle</button></span></div>
<div class="val"><span>beep on relaunch</span><input type="checkbox" id="beep"></div>
<div class="small dim">theme and beep are stored in the browser. server config lives in roblox-dash-config.json</div></div>
<div class="card"><h2>Endpoints</h2><pre id="eps"></pre></div>
</div></section>
</main></div></div>
<div id="toast"></div><dialog id="dlg"><img id="dlgimg" style="max-width:92vw;max-height:90vh"></dialog>
<script>
const $=id=>document.getElementById(id);
const I={layout:'<rect width="7" height="9" x="3" y="3" rx="1"/><rect width="7" height="5" x="14" y="3" rx="1"/><rect width="7" height="9" x="14" y="12" rx="1"/><rect width="7" height="5" x="3" y="16" rx="1"/>',
session:'<rect width="18" height="14" x="2" y="3" rx="2"/><line x1="8" x2="16" y1="21" y2="21"/><line x1="12" x2="12" y1="17" y2="21"/><polygon points="10 8 15 11 10 14"/>',
device:'<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
shield:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/>',
term:'<path d="m4 17 6-6-6-6"/><path d="M12 19h8"/>',
sliders:'<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>',
pause:'<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
moon:'<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79"/>',
logo:'<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
play:'<polygon points="6 3 20 12 6 21 6 3"/>',
refresh:'<path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 16h5v5"/>',
cross:'<circle cx="12" cy="12" r="10"/><line x1="22" y1="12" x2="18" y2="12"/><line x1="6" y1="12" x2="2" y2="12"/><line x1="12" y1="6" x2="12" y2="2"/><line x1="12" y1="22" x2="12" y2="18"/>',
stop:'<rect width="12" height="12" x="6" y="6" rx="1"/>',
trash:'<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
timer:'<line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/>',
camera:'<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
power:'<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
plus:'<path d="M5 12h14"/><path d="M12 5v14"/>',
x:'<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
check:'<path d="M20 6 9 17l-5-5"/>',
alert:'<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
dl:'<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
save:'<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
zap:'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
battery:'<rect width="16" height="10" x="2" y="7" rx="2"/><line x1="22" y1="10" x2="22" y2="14"/>',
therm:'<path d="M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z"/>',
shieldok:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/>'};
function ic(n,s){s=s||14;return '<svg xmlns="http://www.w3.org/2000/svg" width="'+s+'" height="'+s+'"' +
 ' viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
 ' stroke-linejoin="round">'+(I[n]||'')+'</svg>';}
(function(){document.querySelectorAll('[data-icon]').forEach(function(e){
 e.innerHTML=ic(e.dataset.icon);});
 $('ic-logo').innerHTML=ic('logo',22);
})();
let S=null,paused=false,relaSeen=null;
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('#tabs button').forEach(x=>x.classList.remove('act'));
 document.querySelectorAll('.tab').forEach(x=>x.classList.remove('act'));
 b.classList.add('act');$('t-'+b.dataset.t).classList.add('act');});
function toast(m){const d=document.createElement('div');d.textContent=m;$('toast').append(d);
 setTimeout(()=>d.remove(),4000);}
async function act(n,p){try{const j=await(await fetch('/action/'+n,{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(p||{})})).json();
 toast((j.ok?'ok: ':'failed: ')+(j.msg||n));}catch(e){toast('server error: '+e)}stats();logs();}
function key(k){act('key',{key:k})}
function ops(p,ok){act('appops',{pkg:p,allow:ok})}
function dozeAdd(){const p=$('dozep').value.trim();if(!p)return;act('doze',{pkg:p,add:true});$('dozep').value='';}
function dozeDel(p){act('doze',{pkg:p,add:false})}
function rb(m){if(confirm('Reboot phone ('+m+')?')&&confirm('Proceed with reboot?'))act('reboot',{mode:m})}
function togPause(){paused=!paused;$('pause').innerHTML=paused?ic('play',12)+'resume':ic('pause',12)+'pause';}
function theme(){const h=document.documentElement;h.dataset.theme=h.dataset.theme=='dark'?'light':'dark';
 localStorage.farm=h.dataset.theme;}
if(localStorage.farm)document.documentElement.dataset.theme=localStorage.farm;
const fmt=s=>{if(s==null)return'—';const d=s/86400|0,h=(s%86400)/3600|0,m=(s%3600)/60|0;
 return d?d+'d '+h+'h':h?h+'h '+m+'m':m+'m '+(s%60)+'s';};
async function stats(){if(paused)return;try{S=await(await fetch('/stats')).json();render(S);}catch(e){}}
async function history(){if(paused)return;try{const h=await(await fetch('/history')).json();
 draw('cvl',h.level,0,100);draw('cvc',h.cpu,0,100);draw('cvd',h.temp,25,55,h.mem);}catch(e){}}
function draw(id,ys,lo,hi,second){const c=$(id);if(!c)return;const q=c.getContext('2d');
 const w=c.width=c.clientWidth*2||600,h=c.height=128;q.clearRect(0,0,w,h);
 const cs=getComputedStyle(document.documentElement);
 ys=ys||[];const n=ys.length;if(n<2)return;
 q.lineWidth=3;q.strokeStyle=(cs.getPropertyValue('--acc')||'#4f7fd9').trim();
 line(q,ys,lo,hi,w-24,10,h-18);
 if(second){q.lineWidth=2;q.strokeStyle=(cs.getPropertyValue('--ok')||'#3f9e63').trim();
  const mx=Math.max(...second.filter(v=>v!=null).map(Math.abs))||1;
  q.beginPath();second.forEach((v,i)=>{if(v==null)return;const x=12+i/(second.length-1)*(w-24),
  y=h-10-Math.abs(v)/mx*(h-26);i?q.lineTo(x,y):q.moveTo(x,y)});q.stroke();}}
function line(q,ys,lo,hi,w,pad,bottom){q.beginPath();let started=false;
 ys.forEach((v,i)=>{if(v==null)return;const x=pad+i/(ys.length-1)*w,
 y=bottom-Math.min(hi,Math.max(lo,v)-lo)/(hi-lo)*(bottom-14);
 started?q.lineTo(x,y):q.moveTo(x,y);started=true});q.stroke();}
async function logs(){try{let t=await(await fetch('/log')).text();
 const f=($('logf')&&$('logf').value.toLowerCase())||'';
 if(f)t=t.split('\\n').filter(l=>l.toLowerCase().includes(f)).join('\\n');
 $('log').textContent=t;$('log').scrollTop=1e9;}catch(e){}}
async function farmlog(){try{const t=await(await fetch('/farmlog')).text();
 $('farmlog').textContent=t||'(empty)';$('flstamp').textContent=new Date().toLocaleTimeString();}catch(e){}}
async function shot(){try{const r=await fetch('/shot/latest.png?t='+Date.now());if(!r.ok)throw 0;
 $('shotimg').src=r.url;$('shott').textContent='captured '+new Date().toLocaleTimeString();}catch(e){}}
function expJson(){if(!S)return;const b=new Blob([JSON.stringify(S,null,1)],{type:'application/json'});
 const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='farm-snapshot.json';a.click();}
function cfgSave(){act('cfg',{temp_guard:$('tgck').checked,temp_limit:+$('tglimit').value,dim_to:+$('tgdim').value});}
function beep(){try{const a=new AudioContext(),o=a.createOscillator(),g=a.createGain();
 o.connect(g);g.connect(a.destination);o.frequency.value=880;g.gain.value=.06;o.start();
 o.stop(a.currentTime+.16);}catch(e){}}
function render(s){const st=s.static,a=s.app,b=s.battery,sc=s.screen;
 $('clock').textContent=s.time;$('dev').textContent=st.model+' · android '+st.android+
 (st.hyperos?' · '+st.hyperos:'');
 const us=s.adb_state;$('usb').textContent='adb '+us;
 $('usb').style.borderColor=us==='device'?'var(--ok)':'var(--bad)';
 $('ast').innerHTML=a.running?ic('play',15)+' <span class="g-ok">running</span>':
  ic('stop',15)+' <span class="g-bad">dead</span>';
 $('aup').textContent=a.running?fmt(a.session_s):'—';
 $('ares').textContent=(a.cpu_pct??'—')+'% / '+(a.mem_mb??'—')+'MB';
 $('athr').textContent=a.running?(a.threads??'—'):'—';
 $('rfg').textContent=s.focus.package?(s.focus.package+' / '+s.focus.activity):'none';
 const ccn=(c)=>c?(' <span class="g-ok">'+ic('check',12)+'</span>'):(' <span class="g-bad">'+ic('x',12)+'</span>');
 $('checks').innerHTML=(s.checklist||[]).map(c=>'<div class="ck"><span class="s">'+
  (c.ok?'<span class="g-ok">'+ic('check',13)+'</span>':'<span class="g-bad">'+ic('x',13)+'</span>')+c.name+
  '</span><span class="pill">'+c.detail+'</span></div>').join('');
 const okn=(s.checklist||[]).filter(c=>c.ok).length;
 $('cksum').textContent=okn+' / '+s.checklist.length;
 $('appchip').innerHTML=ic(a.running?'play':'stop',12)+(a.running?' app up':' app down');
 $('appchip').style.borderColor=a.running?'var(--ok)':'var(--bad)';
 $('wdchip').innerHTML=ic('shield',12)+(s.watchdog_running?' watchdog':' watchdog off');
 $('wdchip').style.borderColor=s.watchdog_running?'var(--ok)':'var(--bad)';
 $('relc').textContent=s.relaunches.today+' / '+s.relaunches.total;
 if(relaSeen!=null&&relaSeen!==s.relaunches.today&&$('beep')&&$('beep').checked)beep();
 relaSeen=s.relaunches.today;
 $('top').innerHTML='<tr><th>pid</th><th>process</th><th>cpu</th></tr>'+
  s.top.map(t=>'<tr><td>'+t.pid+'</td><td>'+t.name+'</td><td>'+t.cpu+'%</td></tr>').join('');
 $('os').textContent=(st.android||'?')+(st.hyperos?' · '+st.hyperos:'');
 $('kern').textContent=st.kernel;$('pat').textContent=st.patch;
 $('ups').textContent=fmt(s.uptime_s)+' / '+fmt(s.dash_uptime_s);
 $('ram').textContent=(s.ram.used_pct??'—')+'% ('+s.ram.avail_gb+' of '+s.ram.total_gb+' GB free)';
 $('rbar').style.width=(s.ram.used_pct||0)+'%';
 $('sto').textContent=(s.storage.used_pct!=null?s.storage.used_pct+'%':'no data');
 $('sbar').style.width=(s.storage.used_pct||0)+'%';
 $('bp').textContent=b.charging?'in / charging':b.full?'full':'unplugged';
 $('bl').textContent=(b.level??'—')+'%';$('bbr').style.width=(b.level||0)+'%';
 $('bbr').style.background=(b.level||0)<15?'var(--bad)':'var(--acc)';
 $('chg').innerHTML=(b.charging||b.full)?'<span class="g-ok">'+ic('zap',13)+'</span> power':'<span class="g-bad">'+ic('alert',13)+'</span> none';
 $('bt').textContent=b.temp_c!=null?b.temp_c+' °C'+(b.temp_c>38?' !':''):'—';
 $('bv').textContent=b.voltage_mv?b.voltage_mv+' mV':'—';
 $('bcu').textContent=b.max_charge_ma?b.max_charge_ma+' mA':'—';
 $('scr').textContent=sc.awake?'awake':'asleep / dozing';
 $('ka').innerHTML=sc.keep_awake?'<span class="g-ok">disabled (continuous)</span>':'10 min';
 $('brs').value=sc.brightness??120;$('brv').textContent=$('brs').value;
 $('wst').innerHTML=s.watchdog_running?'<span class="g-ok">'+ic('shieldok',20)+'</span> running':
  '<span class="g-bad">'+ic('alert',20)+'</span> stopped';
 $('wpid').textContent=s.watchdog_running?'pid '+s.watchdog_pid:'not running';
 $('lastrel').textContent=s.relaunches.last||'no relaunch yet';
 const items=[(s.cfg&&s.cfg.temp_guard?'temp guard on':'temp guard off')];
 $('tgck').checked=!!s.cfg.temp_guard;$('tglimit').value=s.cfg.temp_limit;$('tgdim').value=s.cfg.dim_to;
 var dz=s.doze.map(p=>'<div class="ck"><span class="small dim">'+p+
  '</span><button class="sm" onclick=\\'dozeDel("'+p+'")\\' style="font-size:11px;padding:2px 7px;margin:0" aria-label="remove">'+ic('x',12)+'</button></div>').join('');
 $('doze').innerHTML=dz||'<span class="dim small">whitelist empty</span>';
 $('dnd').innerHTML=s.dnd.on?'on ('+s.dnd.mode+')':'off';
 $('rpkg').textContent='com.roblox.client';
 $('rver').textContent=s.versions.version;
 $('rins').textContent=s.versions.installed+' / '+s.versions.updated;
 $('crc').textContent=s.crashes;
}
let t1,t2;function start(){t1=setInterval(stats,itvSel());
 t2=setInterval(()=>{logs();
 if($('t-session')&&$('t-session').classList.contains('act'))farmlog();
 if($('t-device')&&$('t-device').classList.contains('act'))shot();history();},Math.max(itvSel(),8000));}
function itvSel(){return +$('itv').value}
$('itv').onchange=()=>{clearInterval(t1);clearInterval(t2);start();};
$('shotimg').onclick=()=>{$('dlgimg').src=$('shotimg').src;$('dlg').showModal();};
$('eps').textContent=['GET /stats','GET /history','GET /log','GET /logdl','GET /farmlog',
 'GET /shot/latest.png','GET /api-docs'].join('\\n');
$('eps2').textContent='POST /action/<name> - one of: restart focus kill cleardata keep_awake_on keep_awake_off brightness saver dnd key doze appops watchdog_start watchdog_stop reboot setup clear_log cfg';
stats();logs();farmlog();shot();start();history();
</script></body></html>"""

if __name__ == "__main__":
    load_cfg()
    def sampler():
        while True:
            poll_history()
            time.sleep(10)
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print("farm console on http://%s:%d" % (HOST, PORT), flush=True)
    srv.serve_forever()
