#!/usr/bin/env python3
"""
VNC Turbo — finds what's making VNC slow on a Raspberry Pi 5 and lets you
switch it off with one click. Every change can be undone.

Run:  python3 vnc_turbo.py          (desktop app)
      python3 vnc_turbo.py --scan   (text report, handy over SSH)
"""
import os
import re
import sys
import json
import glob
import pwd
import time
import queue
import shutil
import signal
import threading
import subprocess

APP = "VNC Turbo"
VERSION = "1.0"
CONF_DIR = os.path.expanduser("~/.config/vnc-turbo")
UNDO_FILE = os.path.join(CONF_DIR, "undo.json")
BACKUP_DIR = os.path.join(CONF_DIR, "backup")
MARK = "# added-by-vnc-turbo"

# Things that must never be ended — the desktop, VNC itself, networking, the case fan app...
PROTECTED = {
    "systemd", "init", "kthreadd", "labwc", "wayfire", "Xorg", "Xwayland", "lxsession",
    "wayvnc", "vncserver-x11-core", "vncserver-x11", "vncserver-x11-serviced", "Xvnc",
    "x11vnc", "Xtigervnc", "sshd", "NetworkManager", "wpa_supplicant", "dbus-daemon",
    "pipewire", "wireplumber", "pulseaudio", "lxpanel", "wf-panel-pi", "pcmanfm",
    "lightdm", "polkitd", "lxpolkit", "udisksd", "systemd-journal", "systemd-logind",
    "systemd-udevd", "rpi-connectd", "kanshi", "squeekboard", "pironman5", "python3",
}

VNC_NAMES = ["wayvnc", "vncserver-x11-core", "vncserver-x11", "Xvnc", "x11vnc", "Xtigervnc"]

# (service, what it does, safe-to-turn-off?, units to stop)
SERVICES = [
    ("cups", "Printing. Off is fine unless you print from the Pi.", True,
     ["cups.service", "cups.socket", "cups.path"]),
    ("cups-browsed", "Hunts the network for printers.", True, ["cups-browsed.service"]),
    ("ModemManager", "Cellular / USB modems. Almost nobody needs it.", True, ["ModemManager.service"]),
    ("packagekit", "Checks for updates in the background.", True, ["packagekit.service"]),
    ("colord", "Colour profiles for printers and scanners.", True, ["colord.service"]),
    ("triggerhappy", "Hotkey helper for media keys.", True,
     ["triggerhappy.service", "triggerhappy.socket"]),
    ("bluetooth", "Bluetooth. KEEP ON if you use a Bluetooth keyboard, mouse or speaker.", False,
     ["bluetooth.service"]),
    ("avahi-daemon", "Lets you reach the Pi by name (raspberrypi.local). KEEP ON if you connect by name.",
     False, ["avahi-daemon.service", "avahi-daemon.socket"]),
]


# ───────────────────────── command helpers ─────────────────────────

def _exec(cmd, timeout, inp):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=inp)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as e:  # noqa
        return 1, str(e)


def run(cmd, root=False, timeout=15, inp=None):
    """Run a command. root=True uses sudo (no prompt) and falls back to a pkexec password box."""
    if root and os.geteuid() != 0:
        rc, out = _exec(["sudo", "-n"] + cmd, timeout, inp)
        if rc == 0 or not any(k in out for k in ("password", "terminal is required", "sudo:")):
            return rc, out
        if shutil.which("pkexec"):
            return _exec(["pkexec"] + cmd, 180, inp)
        return rc, out
    return _exec(cmd, timeout, inp)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except Exception:
        return default


def have(prog):
    return shutil.which(prog) is not None


# ───────────────────────── live sampler ─────────────────────────

class Sampler:
    """Reads /proc directly — cheap enough to run every couple of seconds."""

    def __init__(self):
        self.prev_cpu = None
        self.prev_procs = {}
        self.prev_net = None
        self.prev_t = None
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.users = {}
        self.last = None

    def _cpu(self):
        v = list(map(int, read("/proc/stat").splitlines()[0].split()[1:]))
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v), idle

    def _net(self):
        up = down = 0
        for line in read("/proc/net/dev").splitlines()[2:]:
            name, data = line.split(":", 1)
            if name.strip() == "lo":
                continue
            f = data.split()
            down += int(f[0])
            up += int(f[8])
        return up, down

    def _user(self, uid):
        if uid not in self.users:
            try:
                self.users[uid] = pwd.getpwuid(uid).pw_name
            except KeyError:
                self.users[uid] = str(uid)
        return self.users[uid]

    def _procs(self):
        out = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            raw = read(f"/proc/{pid}/stat")
            if not raw or ")" not in raw:
                continue
            try:
                name = raw[raw.index("(") + 1: raw.rindex(")")]
                f = raw[raw.rindex(")") + 2:].split()
                ticks = int(f[11]) + int(f[12])
                rss = int(f[21]) * self.page
                if rss == 0:  # kernel thread
                    continue
                uid = os.stat(f"/proc/{pid}").st_uid
                out[int(pid)] = (name, ticks, rss, uid)
            except Exception:
                continue
        return out

    def sample(self):
        now = time.time()
        total, idle = self._cpu()
        procs = self._procs()
        net = self._net()
        res = {"cpu": 0.0, "procs": [], "up": 0.0, "down": 0.0}
        if self.prev_cpu:
            dt = max(total - self.prev_cpu[0], 1)
            res["cpu"] = 100.0 * (1 - (idle - self.prev_cpu[1]) / dt)
            for pid, (name, ticks, rss, uid) in procs.items():
                prev = self.prev_procs.get(pid)
                cpu = 100.0 * (ticks - prev[1]) / dt if prev and prev[0] == name else 0.0
                res["procs"].append({"pid": pid, "name": name, "cpu": max(cpu, 0.0),
                                     "mem": rss / 1048576, "user": self._user(uid)})
            el = max(now - self.prev_t, 0.1)
            res["up"] = (net[0] - self.prev_net[0]) / el / 1024
            res["down"] = (net[1] - self.prev_net[1]) / el / 1024
        self.prev_cpu, self.prev_procs, self.prev_net, self.prev_t = (total, idle), procs, net, now
        res["procs"].sort(key=lambda p: p["cpu"], reverse=True)
        res.update(mem_info())
        res["temp"] = temp_c()
        self.last = res
        return res


def mem_info():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) / 1024
    return {"mem_total": m.get("MemTotal", 0), "mem_avail": m.get("MemAvailable", 0),
            "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}


def temp_c():
    t = read("/sys/class/thermal/thermal_zone0/temp").strip()
    return int(t) / 1000 if t.isdigit() else None


def running(name):
    return run(["pgrep", "-x", name])[0] == 0


# ───────────────────────── undo journal ─────────────────────────

class Undo:
    def __init__(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        try:
            self.items = json.loads(read(UNDO_FILE, "[]"))
        except Exception:
            self.items = []

    def add(self, desc, kind, **data):
        self.items.append({"desc": desc, "kind": kind, **data})
        self.save()

    def save(self):
        with open(UNDO_FILE, "w") as f:
            json.dump(self.items, f, indent=1)

    def undo_all(self, log):
        for it in reversed(self.items):
            k = it["kind"]
            if k == "cmd":
                rc, out = run(it["cmd"], root=it.get("root", False), inp=it.get("inp"))
                log(f"Undo: {it['desc']} → {'ok' if rc == 0 else out}")
            elif k == "restore":
                if it.get("backup") and os.path.exists(it["backup"]):
                    shutil.copy2(it["backup"], it["path"])
                elif os.path.exists(it["path"]):
                    os.remove(it["path"])
                log(f"Undo: restored {it['path']}")
            elif k == "strip":
                strip_marked(it["path"])
                log(f"Undo: cleaned {it['path']}")
        self.items = []
        self.save()
        if have("pcmanfm"):
            run(["pcmanfm", "--reconfigure"])


def backup(undo, path, desc):
    dst = None
    if os.path.exists(path):
        dst = os.path.join(BACKUP_DIR, path.strip("/").replace("/", "__") + f".{int(time.time())}")
        shutil.copy2(path, dst)
    undo.add(desc, "restore", path=path, backup=dst)


def strip_marked(path):
    if not os.path.exists(path):
        return
    lines = read(path).splitlines(keepends=True)
    keep, skip = [], False
    for ln in lines:
        if ln.strip() == MARK:
            skip = True
            continue
        if skip:
            skip = False
            continue
        keep.append(ln)
    with open(path, "w") as f:
        f.writelines(keep)


# ───────────────────────── system checks ─────────────────────────

def vnc_server():
    for n in VNC_NAMES:
        if running(n):
            return n
    return None


def session_type():
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return os.environ.get("XDG_SESSION_TYPE") or "none"


def displays():
    """Returns (kind, [ {name, current, modes} ])."""
    outs = []
    if session_type() == "wayland" and have("wlr-randr"):
        rc, out = run(["wlr-randr"])
        cur = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                cur = {"name": line.split()[0], "current": None, "modes": [], "enabled": True}
                outs.append(cur)
            elif cur:
                if "Enabled: no" in line:
                    cur["enabled"] = False
                m = re.search(r"(\d+)x(\d+) px", line)
                if m:
                    mode = f"{m.group(1)}x{m.group(2)}"
                    if mode not in cur["modes"]:
                        cur["modes"].append(mode)
                    if "current" in line:
                        cur["current"] = mode
        return "wayland", [o for o in outs if o["enabled"] and o["current"]]
    if session_type() == "x11" and have("xrandr"):
        rc, out = run(["xrandr", "--query"])
        cur = None
        for line in out.splitlines():
            if " connected" in line:
                cur = {"name": line.split()[0], "current": None, "modes": []}
                outs.append(cur)
            elif " disconnected" in line:
                cur = None
            elif cur and line.startswith("   "):
                mode = line.split()[0]
                cur["modes"].append(mode)
                if "*" in line:
                    cur["current"] = mode
        return "x11", [o for o in outs if o["current"]]
    return None, []


def default_iface():
    m = re.search(r"dev (\S+)", run(["ip", "route", "show", "default"])[1])
    return m.group(1) if m else None


def wifi_state(iface):
    link = run(["iw", "dev", iface, "link"])[1]
    sig = re.search(r"signal:\s*(-?\d+)", link)
    ps = run(["iw", "dev", iface, "get", "power_save"])[1]
    return (int(sig.group(1)) if sig else None), ("on" in ps.lower())


def throttled():
    if not have("vcgencmd"):
        return None
    m = re.search(r"0x([0-9a-fA-F]+)", run(["vcgencmd", "get_throttled"])[1])
    return int(m.group(1), 16) if m else None


def governors():
    return sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"))


def wallpaper_files():
    """User desktop configs (create from system defaults if missing)."""
    users = glob.glob(os.path.expanduser("~/.config/pcmanfm/*/desktop-items-*.conf"))
    if users:
        return users, []
    systems = glob.glob("/etc/xdg/pcmanfm/*/desktop-items-*.conf")
    return [], systems


def wallpaper_is_picture():
    users, systems = wallpaper_files()
    for f in users or systems:
        m = re.search(r"^wallpaper_mode=(\S+)", read(f), re.M)
        if m and m.group(1) != "color":
            return True
    return False


def service_active(units):
    return any(run(["systemctl", "is-active", u])[1].strip() == "active" for u in units)


# ───────────────────────── fixes ─────────────────────────

def fix_wallpaper(undo, log):
    users, systems = wallpaper_files()
    targets = list(users)
    for s in systems:
        parts = s.split("/")
        dst = os.path.expanduser(f"~/.config/pcmanfm/{parts[-2]}/{parts[-1]}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        undo.add("remove created desktop config", "restore", path=dst, backup=None)
        shutil.copy2(s, dst)
        targets.append(dst)
    for f in targets:
        if f in users:
            backup(undo, f, "desktop wallpaper")
        txt = read(f)
        txt = re.sub(r"^wallpaper_mode=.*$", "wallpaper_mode=color", txt, flags=re.M)
        if re.search(r"^desktop_bg=", txt, re.M):
            txt = re.sub(r"^desktop_bg=.*$", "desktop_bg=#1e2a38", txt, flags=re.M)
        else:
            txt = txt.replace("wallpaper_mode=color", "wallpaper_mode=color\ndesktop_bg=#1e2a38")
        with open(f, "w") as fh:
            fh.write(txt)
    run(["pcmanfm", "--reconfigure"])
    log("Wallpaper switched to a plain colour (pictures are slow to send over VNC).")
    return True


def fix_wifi_powersave(iface, undo, log):
    rc, out = run(["iw", "dev", iface, "set", "power_save", "off"], root=True)
    if rc != 0:
        log(f"Couldn't turn off Wi-Fi power saving: {out}")
        return False
    undo.add("Wi-Fi power saving back on", "cmd", cmd=["iw", "dev", iface, "set", "power_save", "on"], root=True)
    for line in run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])[1].splitlines():
        name, _, dev = line.rpartition(":")
        if dev == iface and name:
            if run(["nmcli", "connection", "modify", name, "802-11-wireless.powersave", "2"], root=True)[0] == 0:
                undo.add("Wi-Fi power saving default", "cmd",
                         cmd=["nmcli", "connection", "modify", name, "802-11-wireless.powersave", "0"], root=True)
                log(f"Wi-Fi power saving off for '{name}' (stays off after reboot).")
    log("Wi-Fi power saving is off — no more naps between VNC frames.")
    return True


def fix_governor(undo, log):
    paths = governors()
    if not paths:
        return False
    prev = read(paths[0]).strip() or "ondemand"
    rc, out = run(["tee"] + paths, root=True, inp="performance\n")
    if rc != 0:
        log(f"Couldn't set CPU to performance: {out}")
        return False
    undo.add(f"CPU governor back to {prev}", "cmd", cmd=["tee"] + paths, root=True, inp=prev + "\n")
    log("CPU set to full speed (performance). Resets to normal on reboot.")
    return True


def fix_services(names, undo, log, turn_on=False):
    ok = True
    for name, _, _, units in SERVICES:
        if name not in names:
            continue
        verb = ["enable", "--now"] if turn_on else ["disable", "--now"]
        rc, out = run(["systemctl"] + verb + units, root=True, timeout=40)
        if rc == 0:
            if not turn_on:
                undo.add(f"turn {name} back on", "cmd", cmd=["systemctl", "enable", "--now"] + units, root=True)
            log(f"{'Turned on' if turn_on else 'Turned off'} {name}.")
        else:
            ok = False
            log(f"{name}: {out}")
    return ok


def fix_resolution(kind, out, mode, persist, undo, log):
    w, h = mode.split("x")
    prev = out["current"]

    def cmd_for(m):
        if kind == "wayland":
            flag = "--mode" if m in out["modes"] else "--custom-mode"
            return ["wlr-randr", "--output", out["name"], flag, m]
        return ["xrandr", "--output", out["name"], "--mode", m]

    rc, msg = run(cmd_for(mode))
    if rc != 0:
        log(f"Couldn't change resolution: {msg}")
        return False
    undo.add(f"screen back to {prev}", "cmd", cmd=cmd_for(prev))
    log(f"Screen {out['name']} is now {w}×{h} (was {prev}).")
    if persist:
        if kind == "wayland":
            path = os.path.expanduser("~/.config/labwc/autostart")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path) and os.path.exists("/etc/xdg/labwc/autostart"):
                shutil.copy2("/etc/xdg/labwc/autostart", path)
                undo.add("remove autostart copy", "restore", path=path, backup=None)
            strip_marked(path)
            with open(path, "a") as f:
                f.write(f"\n{MARK}\n" + " ".join(cmd_for(mode)) + " &\n")
            undo.add("remove resolution from autostart", "strip", path=path)
        else:
            path = os.path.expanduser("~/.config/autostart/vnc-turbo-resolution.desktop")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=VNC Turbo resolution\nExec="
                        + " ".join(cmd_for(mode)) + "\n")
            undo.add("remove resolution autostart", "restore", path=path, backup=None)
        log("Resolution will be kept after reboot.")
    return True


def end_process(pid, name, log):
    try:
        os.kill(pid, signal.SIGTERM)
        log(f"Ended {name} (PID {pid}).")
        return True
    except PermissionError:
        rc, out = run(["kill", str(pid)], root=True)
        log(f"Ended {name} (PID {pid})." if rc == 0 else f"Couldn't end {name}: {out}")
        return rc == 0
    except ProcessLookupError:
        log(f"{name} already closed.")
        return True


def enable_vnc(undo, log):
    if not have("raspi-config"):
        log("raspi-config not found — turn VNC on in Raspberry Pi Configuration → Interfaces.")
        return False
    rc, out = run(["raspi-config", "nonint", "do_vnc", "0"], root=True, timeout=60)
    log("VNC server turned on." if rc == 0 else f"Couldn't turn on VNC: {out}")
    return rc == 0


# ───────────────────────── the scan ─────────────────────────

class Finding:
    def __init__(self, sev, title, detail, fix=None, fix_label="Fix it", auto=True, key=None):
        self.sev, self.title, self.detail = sev, title, detail
        self.fix, self.fix_label, self.auto, self.key = fix, fix_label, auto, key or title


def scan(sampler, undo, log):
    """Returns (score, findings, summary). fix callables take no args and return bool."""
    sampler.sample()
    time.sleep(1.2)
    s = sampler.sample()
    F = []
    vnc = vnc_server()
    sess = session_type()
    summary = {"vnc": vnc or "none found", "session": sess}

    # VNC server
    if not vnc:
        F.append(Finding("med", "No VNC server is running",
                         "If you're connecting through Raspberry Pi Connect that's fine. "
                         "Otherwise turn on the built-in VNC server.",
                         fix=lambda: enable_vnc(undo, log), fix_label="Turn VNC on", auto=False))
    else:
        vp = next((p for p in s["procs"] if p["name"] == vnc), None)
        if vp and vp["cpu"] > 20:
            F.append(Finding("med", f"{vnc} itself is working hard ({vp['cpu']:.0f}% CPU)",
                             "The VNC server is squeezing a lot of pixels. Lower the screen "
                             "resolution below and set your viewer's Picture Quality to Low or Medium."))

    # Resolution
    kind, outs = displays()
    for o in outs:
        w, h = map(int, o["current"].split("x"))
        px = w * h
        summary["resolution"] = o["current"]
        if px > 1920 * 1080:
            sev = "high"
        elif px > 1600 * 900:
            sev = "med"
        else:
            continue
        F.append(Finding(sev, f"Screen {o['name']} is big: {o['current']}",
                         f"VNC has to send every pixel. {o['current']} is {px / 1e6:.1f} million "
                         "pixels per frame. 1280×720 is about 3× less work than 1920×1080 and "
                         "feels much snappier.",
                         fix=("resolution", kind, o), fix_label="Pick a size…", auto=False))
    if not outs and sess == "none":
        F.append(Finding("low", "Can't see the desktop from here",
                         "Run VNC Turbo from inside the desktop (in your VNC window) so it can "
                         "check the screen size and wallpaper."))

    # Wallpaper
    if sess != "none" and wallpaper_is_picture():
        F.append(Finding("med", "Desktop wallpaper is a photo",
                         "Photos don't compress well, so every time a window moves VNC resends "
                         "lots of detail. A plain colour is much faster.",
                         fix=lambda: fix_wallpaper(undo, log), fix_label="Use plain colour"))

    # CPU
    cpu = s["cpu"]
    summary["cpu"] = f"{cpu:.0f}%"
    if cpu > 75:
        F.append(Finding("high", f"CPU is busy: {cpu:.0f}%",
                         "VNC needs spare CPU to encode the screen. See the hogs below or the Processes tab."))
    elif cpu > 50:
        F.append(Finding("med", f"CPU is fairly busy: {cpu:.0f}%",
                         "Closing the apps at the top of the Processes tab will free it up."))
    for p in s["procs"][:6]:
        if p["cpu"] >= 15 and p["name"] not in PROTECTED and p["pid"] != os.getpid():
            F.append(Finding("high" if p["cpu"] > 40 else "med",
                             f"{p['name']} is using {p['cpu']:.0f}% CPU",
                             f"PID {p['pid']}, {p['mem']:.0f} MB RAM, owner {p['user']}. "
                             "Ending it frees that CPU for VNC (unsaved work in it will be lost).",
                             fix=("kill", p["pid"], p["name"]), fix_label="End it", auto=False,
                             key=f"kill{p['pid']}"))

    # Memory
    avail, total, swap = s["mem_avail"], s["mem_total"], s["swap_used"]
    summary["ram"] = f"{avail:.0f} MB free of {total:.0f} MB"
    if avail < 350:
        F.append(Finding("high", f"Almost out of RAM ({avail:.0f} MB free)",
                         "When RAM runs out the Pi starts swapping and everything stutters. "
                         "Close big apps (browsers are the usual culprit) — see the Processes tab.",
                         fix=("tab", "procs_mem"), fix_label="Show RAM hogs", auto=False))
    elif avail < 700:
        F.append(Finding("med", f"RAM is getting low ({avail:.0f} MB free)",
                         "Closing a browser tab or two will help.",
                         fix=("tab", "procs_mem"), fix_label="Show RAM hogs", auto=False))
    if swap > 250:
        F.append(Finding("med", f"{swap:.0f} MB pushed into swap",
                         "Some apps got moved out of RAM. Switching back to them will be slow "
                         "until they're reloaded. Closing unused apps helps."))
    for p in sorted(s["procs"], key=lambda p: p["mem"], reverse=True)[:3]:
        if p["mem"] > 500 and p["name"] not in PROTECTED:
            F.append(Finding("med", f"{p['name']} is using {p['mem']:.0f} MB of RAM",
                             "That's a big slice of a 4 GB Pi.",
                             fix=("kill", p["pid"], p["name"]), fix_label="End it", auto=False,
                             key=f"kill{p['pid']}"))

    # Heat and power
    t = s["temp"]
    if t is not None:
        summary["temp"] = f"{t:.0f}°C"
        if t >= 80:
            F.append(Finding("high", f"Pi is hot: {t:.0f}°C", "At 80°C+ it slows itself down. "
                             "Check the fans are on in Pironman Pilot or your case software."))
        elif t >= 70:
            F.append(Finding("low", f"Pi is warm: {t:.0f}°C", "Not throttling yet, but close."))
    th = throttled()
    if th:
        bits = []
        if th & 0x1:
            bits.append("under-voltage RIGHT NOW (weak power supply or cable)")
        elif th & 0x10000:
            bits.append("under-voltage happened since boot")
        if th & 0x4:
            bits.append("CPU is throttled right now")
        elif th & 0x40000:
            bits.append("CPU got throttled since boot")
        if th & 0x8:
            bits.append("hit the temperature limit")
        F.append(Finding("high" if th & 0x5 else "med", "Pi has been slowing itself down",
                         "; ".join(bits).capitalize() + ". Pi 5 wants the official 27 W (5 V / 5 A) "
                         "power supply — weak supplies are a top cause of laggy VNC."))

    # Network
    iface = default_iface()
    summary["network"] = iface or "not connected"
    if iface and iface.startswith("wl"):
        sig, ps = wifi_state(iface)
        if sig is not None:
            summary["network"] = f"Wi-Fi {sig} dBm"
            if sig <= -72:
                F.append(Finding("high", f"Weak Wi-Fi signal ({sig} dBm)",
                                 "Below about -70 dBm, VNC gets choppy. Move the Pi closer to the "
                                 "router or plug in an Ethernet cable — the single biggest VNC speed-up."))
            elif sig <= -62:
                F.append(Finding("low", f"Wi-Fi signal is OK but not great ({sig} dBm)",
                                 "Ethernet would make VNC noticeably smoother."))
        if ps:
            F.append(Finding("med", "Wi-Fi power saving is on",
                             "The Wi-Fi chip naps between packets, which adds lag to every click "
                             "and mouse move over VNC.",
                             fix=lambda: fix_wifi_powersave(iface, undo, log), fix_label="Turn it off"))

    # CPU governor
    g = governors()
    if g:
        gov = read(g[0]).strip()
        summary["governor"] = gov
        if gov not in ("performance",):
            F.append(Finding("low", f"CPU speed mode is '{gov}'",
                             "It ramps up only after load arrives, so VNC feels a beat behind. "
                             "'performance' keeps it at full speed (a little warmer; resets on reboot).",
                             fix=lambda: fix_governor(undo, log), fix_label="Full speed"))

    # Services
    active_safe = [n for n, _, safe, u in SERVICES if safe and service_active(u)]
    if active_safe:
        F.append(Finding("low", f"{len(active_safe)} background services you probably don't need",
                         "Running: " + ", ".join(active_safe) + ". They nibble at RAM and CPU.",
                         fix=lambda: fix_services(active_safe, undo, log), fix_label="Turn them off"))

    pts = {"high": 18, "med": 9, "low": 3}
    score = max(5, 100 - sum(pts.get(f.sev, 0) for f in F))
    order = {"high": 0, "med": 1, "low": 2}
    F.sort(key=lambda f: order.get(f.sev, 3))
    return score, F, summary


TIPS = """THE BIG WINS (in order)

1.  Plug in Ethernet.  Wi-Fi is the #1 cause of laggy VNC. A cable to the router fixes most of it.

2.  Lower the screen size.  Use "Pick a size…" on the scan. 1280×720 or 1600×900 is much faster than 1920×1080.
    Headless (no monitor)? You can also set it in:  sudo raspi-config → Display Options → VNC Resolution.

3.  Turn down quality in your VNC viewer.
    • RealVNC Viewer: right-click the connection → Properties → Options → Picture quality → Low or Medium.
    • TigerVNC / other viewers: pick "Tight" or "ZRLE" encoding and lower the colour depth.
    • Turn off "Scale" in the viewer if you can — scaling adds work on your PC.

4.  Plain-colour wallpaper and close the browser when you're not using it.

5.  Power supply.  Pi 5 wants the official 27 W supply. A weak one makes the Pi throttle itself.


GOOD TO KNOW

•  On a Pi 5 with Raspberry Pi OS Bookworm/Trixie, the VNC server is "wayvnc" (Wayland). It's lighter than the old RealVNC server.
•  Viewing from outside your house? Raspberry Pi Connect (connect.raspberrypi.com) can be smoother than port-forwarded VNC.
•  Everything VNC Turbo changes is recorded. "Undo my changes" puts it all back.
•  Text report over SSH:   vnc-turbo --scan
"""


# ───────────────────────── GUI ─────────────────────────

def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    BG, PANEL, CARD, FG, DIM = "#0f1720", "#16212d", "#1c2a39", "#e6edf3", "#8b9bb0"
    ACC, GOOD, WARN, BAD, INFO = "#38bdf8", "#22c55e", "#f59e0b", "#ef4444", "#64748b"
    SEV = {"high": BAD, "med": WARN, "low": INFO}
    SEVTXT = {"high": "SLOW", "med": "HELPS", "low": "TWEAK"}

    sampler = Sampler()
    undo = Undo()
    q = queue.Queue()

    root = tk.Tk()
    root.title(f"{APP}")
    root.geometry("980x700")
    root.minsize(760, 520)
    root.configure(bg=BG)
    try:
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vnc-turbo.png")
        if os.path.exists(icon):
            root.iconphoto(True, tk.PhotoImage(file=icon))
    except Exception:
        pass

    F_T = ("DejaVu Sans", 18, "bold")
    F_H = ("DejaVu Sans", 11, "bold")
    F_B = ("DejaVu Sans", 10)
    F_S = ("DejaVu Sans", 9)

    st = ttk.Style()
    st.theme_use("clam")
    st.configure("TNotebook", background=BG, borderwidth=0, bordercolor=BG, lightcolor=BG, darkcolor=BG)
    st.configure("TNotebook.Tab", background=PANEL, foreground=DIM, padding=(16, 7), font=F_H, borderwidth=0,
                 bordercolor=BG, lightcolor=PANEL, darkcolor=PANEL)
    st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", FG)],
           lightcolor=[("selected", CARD)])
    st.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=26,
                 font=F_B, borderwidth=0)
    st.configure("Treeview.Heading", background=PANEL, foreground=DIM, font=F_H, relief="flat")
    st.map("Treeview", background=[("selected", "#24507a")])
    st.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, borderwidth=0, arrowcolor=DIM,
                 bordercolor=BG, lightcolor=PANEL, darkcolor=PANEL, gripcount=0)

    def btn(parent, text, cmd, color=ACC, fg="#04121d", small=False):
        b = tk.Button(parent, text=text, command=cmd, bg=color, fg=fg, activebackground=FG,
                      activeforeground="#04121d", relief="flat", bd=0, cursor="hand2",
                      font=F_S if small else F_H, padx=10 if small else 14, pady=3 if small else 6,
                      highlightthickness=0)
        return b

    # ── header
    hdr = tk.Frame(root, bg=BG)
    hdr.pack(fill="x", padx=18, pady=(14, 6))
    tk.Label(hdr, text="⚡ VNC Turbo", font=F_T, bg=BG, fg=FG).pack(side="left")
    tk.Label(hdr, text="  make remote desktop snappy again", font=F_B, bg=BG, fg=DIM).pack(side="left", pady=(6, 0))
    score_lbl = tk.Label(hdr, text="—", font=("DejaVu Sans", 20, "bold"), bg=PANEL, fg=DIM, width=4, pady=2)
    score_lbl.pack(side="right")
    tk.Label(hdr, text="Speed score ", font=F_S, bg=BG, fg=DIM).pack(side="right")

    stats = tk.Frame(root, bg=BG)
    stats.pack(fill="x", padx=18, pady=(0, 8))
    stat_lbls = {}
    for key, name in [("cpu", "CPU"), ("ram", "RAM free"), ("temp", "Temp"), ("up", "Sending"), ("down", "Receiving"), ("vnc", "VNC server")]:
        box = tk.Frame(stats, bg=PANEL, padx=12, pady=6)
        box.pack(side="left", padx=(0, 8), fill="x", expand=True)
        tk.Label(box, text=name, font=F_S, bg=PANEL, fg=DIM, anchor="w").pack(fill="x")
        lb = tk.Label(box, text="…", font=F_H, bg=PANEL, fg=FG, anchor="w")
        lb.pack(fill="x")
        stat_lbls[key] = lb

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=18, pady=(0, 6))

    status = tk.Label(root, text="Ready.", font=F_S, bg=BG, fg=DIM, anchor="w")
    status.pack(fill="x", padx=18, pady=(0, 8))

    # ── log tab (built first so log() works)
    tab_log = tk.Frame(nb, bg=CARD)
    logbox = tk.Text(tab_log, bg=CARD, fg=FG, font=("DejaVu Sans Mono", 9), relief="flat",
                     wrap="word", padx=12, pady=10, insertbackground=FG)
    logbox.pack(fill="both", expand=True)

    def log(msg):
        q.put(("log", msg))

    def _log_ui(msg):
        logbox.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        logbox.see("end")
        status.config(text=msg)

    def bg(work, done=None):
        def w():
            try:
                r = work()
            except Exception as e:  # noqa
                r = e
                q.put(("log", f"Error: {e}"))
            if done:
                q.put(("call", lambda: done(r)))
        threading.Thread(target=w, daemon=True).start()

    # ── scan tab
    tab_scan = tk.Frame(nb, bg=BG)
    bar = tk.Frame(tab_scan, bg=BG)
    bar.pack(fill="x", pady=(10, 8))
    scan_btn = btn(bar, "🔍  Scan now", lambda: do_scan())
    scan_btn.pack(side="left")
    fixall_btn = btn(bar, "⚡  Fix all recommended", lambda: fix_all(), color=GOOD)
    fixall_btn.pack(side="left", padx=8)
    btn(bar, "↶  Undo my changes", lambda: do_undo(), color=PANEL, fg=FG).pack(side="left")
    summary_lbl = tk.Label(tab_scan, text="", font=F_S, bg=BG, fg=DIM, anchor="w", justify="left")
    summary_lbl.pack(fill="x", pady=(0, 6))

    wrap = tk.Frame(tab_scan, bg=BG)
    wrap.pack(fill="both", expand=True)
    canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
    cards = tk.Frame(canvas, bg=BG)
    cards.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    cwin = canvas.create_window((0, 0), window=cards, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfig(cwin, width=e.width))
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    def _wheel(e):
        canvas.yview_scroll(-1 if (e.num == 4 or getattr(e, "delta", 0) > 0) else 1, "units")
    for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
        canvas.bind_all(ev, lambda e: _wheel(e) if nb.index("current") == 0 else None)

    state = {"findings": [], "busy": False}

    def show_intro():
        for w in cards.winfo_children():
            w.destroy()
        tk.Label(cards, text="Press  Scan now  to check what's slowing your VNC down.\n\n"
                 "VNC Turbo looks at screen size, wallpaper, Wi-Fi, CPU and RAM hogs, heat, power\n"
                 "and background services — then gives you one-click fixes you can undo.",
                 font=F_B, bg=BG, fg=DIM, justify="left").pack(anchor="w", pady=30, padx=6)

    def render(score, findings, summary):
        state["findings"] = findings
        col = GOOD if score >= 80 else WARN if score >= 55 else BAD
        score_lbl.config(text=str(score), fg=col)
        parts = [f"VNC: {summary.get('vnc')}", f"desktop: {summary.get('session')}",
                 f"screen: {summary.get('resolution', '?')}", f"network: {summary.get('network')}",
                 f"CPU mode: {summary.get('governor', '?')}"]
        summary_lbl.config(text="   •   ".join(parts))
        for w in cards.winfo_children():
            w.destroy()
        if not findings:
            tk.Label(cards, text="✅  Nothing slowing VNC down on the Pi side.\n\nIf it still feels laggy, "
                     "check the Tips tab — Ethernet and viewer picture quality are the next big wins.",
                     font=F_B, bg=BG, fg=GOOD, justify="left").pack(anchor="w", pady=24, padx=6)
            return
        for f in findings:
            c = tk.Frame(cards, bg=CARD)
            c.pack(fill="x", pady=4, padx=(0, 6))
            tk.Frame(c, bg=SEV.get(f.sev, INFO), width=5).pack(side="left", fill="y")
            body = tk.Frame(c, bg=CARD, padx=12, pady=9)
            body.pack(side="left", fill="both", expand=True)
            top = tk.Frame(body, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text=SEVTXT.get(f.sev, ""), font=("DejaVu Sans", 8, "bold"), bg=SEV.get(f.sev, INFO),
                     fg="#ffffff", padx=6).pack(side="left")
            tk.Label(top, text="  " + f.title, font=F_H, bg=CARD, fg=FG, anchor="w").pack(side="left")
            d = tk.Label(body, text=f.detail, font=F_S, bg=CARD, fg=DIM, anchor="w", justify="left", wraplength=640)
            d.pack(fill="x", pady=(4, 0))
            c.bind("<Configure>", lambda e, d=d: d.config(wraplength=max(300, e.width - 210)))
            if f.fix:
                b = btn(c, f.fix_label, None, color=ACC if f.auto else PANEL, fg="#04121d" if f.auto else FG, small=True)
                b.config(command=lambda f=f, b=b: apply_fix(f, b))
                b.pack(side="right", padx=12)

    def do_scan():
        if state["busy"]:
            return
        state["busy"] = True
        scan_btn.config(text="Scanning…")
        log("Scanning…")

        def done(r):
            state["busy"] = False
            scan_btn.config(text="🔍  Scan now")
            if isinstance(r, Exception):
                return
            render(*r)
            n = len(r[1])
            log(f"Scan done — score {r[0]}, {n} thing{'' if n == 1 else 's'} to look at.")
        bg(lambda: scan(sampler, undo, log), done)

    def mark_done(b, ok):
        if ok:
            b.config(text="✓ Done", bg=GOOD, fg="#04121d", state="disabled")
        else:
            b.config(text="Didn't work — see Log", bg=BAD, fg="#ffffff")

    def apply_fix(f, b):
        fx = f.fix
        if isinstance(fx, tuple):
            if fx[0] == "resolution":
                return resolution_dialog(fx[1], fx[2], b)
            if fx[0] == "kill":
                if messagebox.askyesno(APP, f"End {fx[2]} (PID {fx[1]})?\n\nAny unsaved work in it will be lost."):
                    bg(lambda: end_process(fx[1], fx[2], log), lambda ok: mark_done(b, ok))
                return
            if fx[0] == "tab":
                proc_sort["key"] = "mem"
                nb.select(tab_proc)
                return
        b.config(text="Working…", state="disabled")
        bg(fx, lambda ok: mark_done(b, ok is True))

    def fix_all():
        todo = [f for f in state["findings"] if f.fix and f.auto and callable(f.fix)]
        if not todo:
            messagebox.showinfo(APP, "Nothing to auto-fix. Run a scan first, or use the buttons on each item "
                                "(screen size and ending apps need your OK).")
            return
        names = "\n".join("•  " + f.title for f in todo)
        if not messagebox.askyesno(APP, f"Apply these fixes?\n\n{names}\n\nYou can undo them all later."):
            return

        def work():
            for f in todo:
                f.fix()
            return True
        bg(work, lambda r: (log("All recommended fixes applied. Rescanning…"), do_scan()))

    def do_undo():
        if not undo.items:
            messagebox.showinfo(APP, "No changes to undo.")
            return
        names = "\n".join("•  " + i["desc"] for i in undo.items[-12:])
        if messagebox.askyesno(APP, f"Undo {len(undo.items)} change(s)?\n\n{names}"):
            bg(lambda: undo.undo_all(log), lambda r: (log("Everything put back."), do_scan()))

    def resolution_dialog(kind, out, b):
        d = tk.Toplevel(root, bg=PANEL, padx=20, pady=16)
        d.title("Screen size")
        d.transient(root)
        tk.Label(d, text=f"Screen {out['name']} — now {out['current']}", font=F_H, bg=PANEL, fg=FG).pack(anchor="w")
        tk.Label(d, text="Smaller = faster VNC", font=F_S, bg=PANEL, fg=DIM).pack(anchor="w", pady=(0, 10))
        choice = tk.StringVar(value="1280x720")
        opts = [("1280x720", "fastest — great for most things"), ("1600x900", "balanced"),
                ("1920x1080", "full HD — sharp but heavier")]
        for m, note in opts:
            if m == out["current"]:
                continue
            tk.Radiobutton(d, text=f"{m.replace('x', ' × ')}   ({note})", variable=choice, value=m, font=F_B,
                           bg=PANEL, fg=FG, selectcolor=CARD, activebackground=PANEL, activeforeground=FG,
                           anchor="w").pack(fill="x")
        keep = tk.BooleanVar(value=True)
        tk.Checkbutton(d, text="Keep it after reboot", variable=keep, font=F_B, bg=PANEL, fg=FG,
                       selectcolor=CARD, activebackground=PANEL, activeforeground=FG).pack(anchor="w", pady=(10, 12))

        def go():
            m, p = choice.get(), keep.get()
            d.destroy()
            b.config(text="Working…", state="disabled")
            bg(lambda: fix_resolution(kind, out, m, p, undo, log), lambda ok: mark_done(b, ok is True))
        row = tk.Frame(d, bg=PANEL)
        row.pack(fill="x")
        btn(row, "Apply", go).pack(side="left")
        btn(row, "Cancel", d.destroy, color=CARD, fg=FG).pack(side="left", padx=8)

    # ── processes tab
    tab_proc = tk.Frame(nb, bg=BG)
    pbar = tk.Frame(tab_proc, bg=BG)
    pbar.pack(fill="x", pady=(10, 8))
    proc_sort = {"key": "cpu"}
    btn(pbar, "Sort by CPU", lambda: proc_sort.update(key="cpu") or refresh_procs(), color=PANEL, fg=FG).pack(side="left")
    btn(pbar, "Sort by RAM", lambda: proc_sort.update(key="mem") or refresh_procs(), color=PANEL, fg=FG).pack(side="left", padx=8)
    btn(pbar, "End selected", lambda: end_selected(), color=BAD, fg="#ffffff").pack(side="right")
    tk.Label(pbar, text="Live — updates every 2 seconds. CPU % is of the whole Pi (all 4 cores).",
             font=F_S, bg=BG, fg=DIM).pack(side="left", padx=12)
    cols = ("name", "cpu", "mem", "user", "pid", "note")
    tree = ttk.Treeview(tab_proc, columns=cols, show="headings", selectmode="browse")
    for c, t, w, a in [("name", "App / process", 230, "w"), ("cpu", "CPU %", 80, "e"), ("mem", "RAM MB", 90, "e"),
                       ("user", "Owner", 100, "w"), ("pid", "PID", 70, "e"), ("note", "", 200, "w")]:
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor=a)
    tree.tag_configure("hot", foreground=WARN)
    tree.tag_configure("prot", foreground=DIM)
    tree.pack(fill="both", expand=True)

    def refresh_procs():
        s = sampler.last
        if not s:
            return
        sel = tree.selection()
        sel_pid = tree.set(sel[0], "pid") if sel else None
        tree.delete(*tree.get_children())
        procs = sorted(s["procs"], key=lambda p: p[proc_sort["key"]], reverse=True)[:60]
        for p in procs:
            prot = p["name"] in PROTECTED or p["pid"] == os.getpid()
            tag = "prot" if prot else ("hot" if p["cpu"] > 15 or p["mem"] > 500 else "")
            iid = tree.insert("", "end", values=(p["name"], f"{p['cpu']:.1f}", f"{p['mem']:.0f}", p["user"], p["pid"],
                                                 "needed — leave running" if prot else ""), tags=(tag,))
            if str(p["pid"]) == sel_pid:
                tree.selection_set(iid)

    def end_selected():
        sel = tree.selection()
        if not sel:
            return
        name, pid, note = tree.set(sel[0], "name"), int(tree.set(sel[0], "pid")), tree.set(sel[0], "note")
        if note:
            messagebox.showwarning(APP, f"{name} is part of the desktop, VNC or networking. Ending it would "
                                   "drop your connection, so VNC Turbo won't do that.")
            return
        if messagebox.askyesno(APP, f"End {name} (PID {pid})?\n\nAny unsaved work in it will be lost."):
            bg(lambda: end_process(pid, name, log))

    # ── services tab
    tab_svc = tk.Frame(nb, bg=BG)
    sbar = tk.Frame(tab_svc, bg=BG)
    sbar.pack(fill="x", pady=(10, 8))
    btn(sbar, "Turn off", lambda: svc_action(False), color=WARN).pack(side="left")
    btn(sbar, "Turn on", lambda: svc_action(True), color=PANEL, fg=FG).pack(side="left", padx=8)
    btn(sbar, "Refresh", lambda: refresh_svcs(), color=PANEL, fg=FG).pack(side="left")
    tk.Label(sbar, text="Background helpers that start with the Pi. Turning one off is fully reversible.",
             font=F_S, bg=BG, fg=DIM).pack(side="left", padx=12)
    stree = ttk.Treeview(tab_svc, columns=("svc", "status", "advice", "what"), show="headings", selectmode="extended")
    for c, t, w in [("svc", "Service", 140), ("status", "Status", 90), ("advice", "Advice", 140), ("what", "What it does", 520)]:
        stree.heading(c, text=t)
        stree.column(c, width=w, anchor="w")
    stree.tag_configure("on_safe", foreground=WARN)
    stree.tag_configure("off", foreground=DIM)
    stree.pack(fill="both", expand=True)

    def refresh_svcs():
        def work():
            return [(n, w, safe, service_active(u), run(["systemctl", "cat", u[0]])[0] == 0) for n, w, safe, u in SERVICES]

        def done(rows):
            if isinstance(rows, Exception):
                return
            stree.delete(*stree.get_children())
            for n, w, safe, act, exists in rows:
                if not exists:
                    continue
                stree.insert("", "end", iid=n, values=(n, "running" if act else "off",
                                                       "safe to turn off" if safe else "your call", w),
                             tags=("on_safe" if act and safe else "" if act else "off",))
        bg(work, done)

    def svc_action(on):
        names = list(stree.selection())
        if not names:
            messagebox.showinfo(APP, "Pick one or more services first.")
            return
        bg(lambda: fix_services(names, undo, log, turn_on=on), lambda r: refresh_svcs())

    # ── tips tab
    tab_tips = tk.Frame(nb, bg=CARD)
    tips = tk.Text(tab_tips, bg=CARD, fg=FG, font=F_B, relief="flat", wrap="word", padx=18, pady=14)
    tips.insert("1.0", TIPS)
    tips.config(state="disabled")
    tips.pack(fill="both", expand=True)

    nb.add(tab_scan, text="Scan & Fix")
    nb.add(tab_proc, text="Processes")
    nb.add(tab_svc, text="Services")
    nb.add(tab_tips, text="Tips")
    nb.add(tab_log, text="Log")
    nb.bind("<<NotebookTabChanged>>", lambda e: (refresh_svcs() if nb.index("current") == 2 else None,
                                                  refresh_procs() if nb.index("current") == 1 else None))

    # ── live loop
    def tick():
        def work():
            s = sampler.sample()
            s["vnc"] = vnc_server()
            return s

        def done(s):
            if isinstance(s, Exception):
                return
            c = s["cpu"]
            stat_lbls["cpu"].config(text=f"{c:.0f}%", fg=BAD if c > 75 else WARN if c > 50 else FG)
            a = s["mem_avail"]
            stat_lbls["ram"].config(text=f"{a:.0f} MB", fg=BAD if a < 350 else WARN if a < 700 else FG)
            t = s["temp"]
            stat_lbls["temp"].config(text=f"{t:.0f}°C" if t else "?", fg=BAD if t and t >= 80 else FG)
            stat_lbls["up"].config(text=f"{s['up']:.0f} KB/s")
            stat_lbls["down"].config(text=f"{s['down']:.0f} KB/s")
            stat_lbls["vnc"].config(text=s["vnc"] or "none", fg=FG if s["vnc"] else WARN)
            if nb.index("current") == 1:
                refresh_procs()
        bg(work, done)
        root.after(2000, tick)

    def pump():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    _log_ui(val)
                else:
                    val()
        except queue.Empty:
            pass
        root.after(100, pump)

    show_intro()
    pump()
    tick()
    root.after(600, do_scan)
    root.mainloop()


def cli():
    s = Sampler()
    u = Undo()
    score, findings, summary = scan(s, u, lambda m: None)
    print(f"\n{APP} {VERSION} — speed score {score}/100")
    print("  " + "  |  ".join(f"{k}: {v}" for k, v in summary.items()))
    print()
    if not findings:
        print("  Nothing slowing VNC down on the Pi side. See tips in the app (Ethernet, viewer quality).")
    for f in findings:
        print(f"  [{f.sev.upper():4}] {f.title}")
        print(f"         {f.detail}\n")


if __name__ == "__main__":
    if "--scan" in sys.argv:
        cli()
    elif "--version" in sys.argv:
        print(f"{APP} {VERSION}")
    else:
        try:
            gui()
        except Exception as e:  # e.g. no display
            if "display" in str(e).lower():
                print("No desktop found — showing a text scan instead.\n")
                cli()
            else:
                raise
