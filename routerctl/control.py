"""The program's brain: devices, limits, blocks, background loops."""

import json
import os
import re
import threading
import time

from . import drivers
from . import parsing as P
from .drivers import ActionError, PageMissing, RouterError
from .drivers.base import DOWN_PREFIX, UP_PREFIX, hexmac
from .monitor import Pinger, Traffic


def hhmm_to_min(s):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", s or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ActionError("Time must look like 22:30")
    return int(m.group(1)) * 60 + int(m.group(2))


def next_day(days):
    """Shift a Mon=1..Sun=64 day mask by one day (for the part of a block after midnight)."""
    return ((days << 1) & 127) | (1 if days & 64 else 0)


def windows(start, end, days):
    """Split a block that crosses midnight into two same-day windows."""
    if start == end:
        raise ActionError("Start and end time are the same")
    if start < end:
        return [(start, end - 1 if end < 1440 else 1439, days)]
    out = [(start, 1439, days)]
    if end > 0:
        out.append((0, end - 1, next_day(days)))
    return out


class Store:
    """A small JSON file written atomically."""

    def __init__(self, path, defaults):
        self.path = path
        self.lock = threading.RLock()
        self.data = json.loads(json.dumps(defaults))
        try:
            with open(path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)


class Controller:
    def __init__(self, config, data_dir, fake_ping=False):
        self.cfg = config
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.store = Store(os.path.join(data_dir, "state.json"), {
            "names": {}, "devices": {}, "limits": {}, "blocks": {}, "selftest": {}, "usage": {}})
        self.log_path = os.path.join(data_dir, "actions.log")
        self.pinger = Pinger(fake=fake_ping)
        self.traffic = Traffic()
        self.status = {"configured": False, "connected": False, "error": "", "info": {}, "warnings": [],
                       "last_ok": None, "clock_ok": None, "model": "", "model_name": ""}
        self.router = None
        self.devices = {}
        self.limit_state = {}       # mac -> {'up': bool, 'down': bool} for our limits on the router
        self.other_rules = []       # limit rules made by hand in the router
        self.block_entries = []
        self.sites = []
        self.caps = {}
        self.checks = []
        self.save_config = lambda: None   # the App replaces this to write config.json
        self.connect_lock = threading.Lock()
        self.action_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads = []
        self.reconfigure(config)

    # ------------------------------------------------------------------ setup

    def _driver_args(self):
        rc = self.cfg.get("router", {})
        return (rc.get("host", "192.168.1.1"), rc.get("username", "admin"), rc.get("password", "")), \
            {"pages": self.cfg.get("pages"), "params": self.cfg.get("params"), "log": self.log}

    def model(self):
        """The model in use: the one chosen in Settings, or the one auto-detect found."""
        m = self.cfg.get("model") or "auto"
        if m == "auto":
            m = self.cfg.get("detected_model") or ""
        return m if m in drivers.MODELS else ""

    def reconfigure(self, config, router=None):
        """`router` = a driver that is already logged in (from Settings), to skip a second detection."""
        self.cfg = config
        rc = config.get("router", {})
        self.status["configured"] = bool(rc.get("password"))
        self.router = None
        self.caps, self.checks = {}, []
        if self.status["configured"]:
            if router is not None:
                self.router = router
            elif self.model():
                args, kw = self._driver_args()
                self.router = drivers.make(self.model(), *args, **kw)
        self._set_model()
        self.traffic = Traffic()
        self._wake.set()

    def _set_model(self):
        m = self.router.MODEL if self.router else self.model()
        self.status["model"] = m
        self.status["model_name"] = drivers.model_name(m)

    def _connect(self):
        """The driver to use, auto-detecting the model the first time. None when not set up."""
        r = self.router
        if r is not None or not self.status["configured"]:
            return r
        with self.connect_lock:
            if self.router is None:
                args, kw = self._driver_args()
                r = drivers.detect(*args, timeout=10, **kw)
                self.cfg["detected_model"] = r.MODEL
                self.router = r
                self._set_model()
                self.log("router recognised as %s" % r.NAME)
                self.save_config()
        return self.router

    def log(self, msg):
        line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def recent_log(self, n=40):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                return f.readlines()[-n:]
        except OSError:
            return []

    def start(self):
        for fn in (self._traffic_loop, self._devices_loop, self._ping_loop):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def _fail(self, e):
        self.status["connected"] = False
        self.status["error"] = str(e)

    # ------------------------------------------------------------------ loops

    def _traffic_loop(self):
        while not self._stop.is_set():
            r = self.router
            if r is None or not self.caps.get("traffic"):
                self._sleep(5)
                continue
            try:
                counters = r.counters()
                wan = r.pick_wan(list(counters), self.cfg.get("wan_interface"))
                self.traffic.update(counters, wan)
                self.status.update(connected=True, error="", last_ok=time.time())
                u = self.store.data["usage"]
                snap = self.traffic.today
                if snap["date"]:
                    u[snap["date"]] = {"down": snap["down"], "up": snap["up"]}
                    for old in sorted(u)[:-60]:
                        del u[old]
            except RouterError as e:
                self._fail(e)
                self._sleep(15)
                continue
            self._stop.wait(max(2, float(self.cfg.get("poll_seconds", 5))))

    def _devices_loop(self):
        n = 0
        while not self._stop.is_set():
            try:
                if self._connect() is None:
                    self._sleep(5)
                    continue
                self.refresh(slow=(n % 6 == 0))
                self.reconcile()
                n += 1
            except RouterError as e:
                self._fail(e)
            self._sleep(float(self.cfg.get("devices_seconds", 20)))

    def _ping_loop(self):
        while not self._stop.is_set():
            ips = [d["ip"] for d in self.devices.values() if d.get("ip")]
            host = (self.cfg.get("router", {}).get("host") or "").replace("http://", "").split("/")[0].split(":")[0]
            internet = self.cfg.get("internet_ping", "1.1.1.1")
            try:
                self.pinger.run(ips + [host, internet])
            except Exception as e:  # ping must never kill the loop
                self.log("ping failed: %s" % e)
            self._stop.wait(float(self.cfg.get("ping_seconds", 10)))

    # ------------------------------------------------------------------ reading

    def refresh(self, slow=True):
        r = self._connect()
        if slow or not self.caps:
            self.caps = r.capabilities()
        try:
            leases = r.leases()
        except PageMissing:
            leases = []
        stations = r.stations()
        arp = r.arp()
        self.limit_state, self.other_rules = r.limit_overview()
        self.block_entries = r.block_names()
        if slow:
            self.status["info"] = r.info()
            r.qos_enabled()
            self.status["clock_ok"] = r.clock_set()
            try:
                self.sites = r.sites()
            except PageMissing:
                self.sites = None
            self.checks = r.checks()
        self._merge(leases, stations, arp)
        self.status.update(connected=True, error="", last_ok=time.time())
        self._warnings()
        self.store.save()

    def _merge(self, leases, stations, arp):
        now = time.time()
        known = self.store.data["devices"]
        st = {s["mac"]: s for s in stations}
        for l in leases:
            k = known.setdefault(l["mac"], {"first_seen": now})
            if l["ip"]:
                k["ip"] = l["ip"]
            if l["hostname"] and l["hostname"] not in ("*", "Unknown", "unknown"):
                k["hostname"] = l["hostname"]
        for a in arp:
            k = known.setdefault(a["mac"], {"first_seen": now})
            k.setdefault("ip", a["ip"])
        band = self.cfg.get("wifi_bands", {"wl0": "2.4 GHz", "wl1": "5 GHz"})
        devices = {}
        for mac, k in known.items():
            s = st.get(mac)
            ping = self.pinger.summary(k.get("ip")) if k.get("ip") else {}
            pinged = bool(ping.get("last_ok") and now - ping["last_ok"] < 60)
            online = bool(s) or pinged
            if s:
                ifc = s["ifc"].split(".")[0] if s["ifc"] else "wl0"
                k["link"] = "wifi"
                k["ifc"] = ifc
                k["band"] = band.get(ifc, ifc)
            elif pinged and self.caps.get("wifi", True):
                k["link"] = "wired"
                k["ifc"] = ""
            if online:
                k["last_seen"] = now
            devices[mac] = {
                "mac": mac, "ip": k.get("ip", ""), "hostname": k.get("hostname", ""),
                "name": self.store.data["names"].get(mac) or k.get("hostname") or "",
                "link": k.get("link", ""), "ifc": k.get("ifc", ""), "band": k.get("band", ""),
                "ssid": s["ssid"] if s else "", "online": online, "last_seen": k.get("last_seen"),
                "first_seen": k.get("first_seen"), "ping": ping,
            }
        self.devices = devices

    def _warnings(self):
        w = list(self.router.warnings(bool(self.store.data["limits"]))) if self.router else []
        if self.status["clock_ok"] is False:
            w.append("The router's clock is not set. Bedtime blocks only work when it is (router: Setup → Time and Date).")
        if self.caps and not self.caps.get("devices"):
            w.append("This program cannot read this router's device list yet. Please send the report from the Check page.")
        self.status["warnings"] = w

    def device_view(self, mac):
        d = dict(self.devices.get(mac) or {"mac": mac, "name": self.store.data["names"].get(mac, "")})
        h = hexmac(mac)
        lim = self.store.data["limits"].get(mac)
        if lim:
            have = self.limit_state.get(mac, {})
            missing = []
            if lim.get("up_applied") and not have.get("up"):
                missing.append(UP_PREFIX + h)
            if lim.get("method") == "router" and lim.get("down") and not have.get("down"):
                missing.append(DOWN_PREFIX + h)
            d["limit"] = dict(lim, on_router=not missing, missing=missing)
        blk = self.store.data["blocks"].get(mac)
        names = set(self.block_entries)
        if blk:
            d["block"] = dict(blk, on_router=all(n in names for n in blk.get("entries", [])))
        other = [r for r in self.other_rules if r.get("src_mac") == mac or (d.get("ip") and r.get("dst_ip") == d.get("ip"))]
        if other:
            d["other_rules"] = [r["name"] for r in other]
        return d

    def device_list(self):
        macs = set(self.devices) | set(self.store.data["limits"]) | set(self.store.data["blocks"])
        out = [self.device_view(m) for m in macs]
        out.sort(key=lambda d: (not d.get("online"), (d.get("name") or d.get("hostname") or "~").lower(), d["mac"]))
        return out

    # ------------------------------------------------------------------ helpers

    def _need_router(self, cap=None):
        if not self.status["configured"]:
            raise ActionError("Set the router password first (Settings).")
        r = self._connect()
        if cap and self.caps and not self.caps.get(cap):
            raise drivers.NotSupported("This router (%s) does not support that yet." % r.NAME)
        return r

    def _device(self, mac):
        mac = P.norm_mac(mac)
        if not P.is_mac(mac):
            raise ActionError("Not a device address: %s" % mac)
        return mac, self.devices.get(mac) or {}

    def download_method(self, dev):
        m = self.cfg.get("download_method", "auto")
        if m in ("router", "upload_trick"):
            return m
        kind = "wifi" if dev.get("link") == "wifi" else "wired"
        t = self.store.data["selftest"].get(kind)
        if t and t.get("download_ok") is False:
            return "upload_trick"
        return "router"

    # ------------------------------------------------------------------ actions

    def set_name(self, mac, name):
        mac, _ = self._device(mac)
        name = (name or "").strip()[:40]
        if name:
            self.store.data["names"][mac] = name
        else:
            self.store.data["names"].pop(mac, None)
        if mac in self.devices:
            self.devices[mac]["name"] = name or self.devices[mac].get("hostname", "")
        self.store.save()

    def forget(self, mac):
        mac, _ = self._device(mac)
        if mac in self.store.data["limits"] or mac in self.store.data["blocks"]:
            raise ActionError("Remove the limit and block first.")
        self.store.data["devices"].pop(mac, None)
        self.store.data["names"].pop(mac, None)
        self.devices.pop(mac, None)
        self.store.save()

    def set_limit(self, mac, down, up, method=None):
        """down/up in kbit/s; 0 or None = no limit in that direction."""
        mac, dev = self._device(mac)
        down, up = int(down or 0), int(up or 0)
        if down and down < 64 or up and up < 32:
            raise ActionError("That limit is too low; use at least 64 kbit/s down and 32 kbit/s up.")
        if not down and not up:
            return self.remove_limit(mac)
        r = self._need_router("speed_limit")
        with self.action_lock:
            method = method or self.download_method(dev)
            up_eff = up
            if down and method == "upload_trick":
                up_eff = min(up or 10 ** 9, max(50, down // 12))
            errors = r.apply_limit(mac, dev.get("ip", ""), dev.get("ifc", ""), up_eff,
                                   down if method == "router" else 0)
            self.store.data["limits"][mac] = {"down": down, "up": up, "method": method, "up_applied": up_eff,
                                              "ip": dev.get("ip", ""), "set_at": time.time()}
            self.store.save()
            self.log("limit %s (%s): down %s up %s via %s%s" % (dev.get("name") or mac, mac, down, up_eff, method,
                                                                 (" — " + "; ".join(errors)) if errors else ""))
            self.limit_state, self.other_rules = r.limit_overview()
            if errors:
                raise ActionError("; ".join(errors))
            return self.device_view(mac)

    def remove_limit(self, mac):
        mac, dev = self._device(mac)
        r = self._need_router()
        with self.action_lock:
            r.clear_limit(mac)
            self.store.data["limits"].pop(mac, None)
            self.store.save()
            self.limit_state, self.other_rules = r.limit_overview()
            self.log("limit removed for %s (%s)" % (dev.get("name") or mac, mac))
            return self.device_view(mac)

    def block(self, mac, mode="pause", start=None, end=None, days=127):
        """mode 'pause' = no internet until unblocked; 'bedtime' = no internet between start and end."""
        mac, dev = self._device(mac)
        if mode == "pause":
            r = self._need_router("pause")
            wins, rec = None, {"mode": "pause"}
        elif mode == "bedtime":
            r = self._need_router("bedtime")
            days = int(days) & 127 or 127
            wins = windows(hhmm_to_min(start), hhmm_to_min(end), days)
            rec = {"mode": "bedtime", "start": start, "end": end, "days": days}
        else:
            raise ActionError("Unknown block type")
        with self.action_lock:
            rec.update(entries=r.apply_block(mac, wins), set_at=time.time())
            self.store.data["blocks"][mac] = rec
            self.store.save()
            self.block_entries = r.block_names()
            self.log("block %s (%s): %s" % (dev.get("name") or mac, mac, rec))
            return self.device_view(mac)

    def unblock(self, mac):
        mac, dev = self._device(mac)
        r = self._need_router()
        with self.action_lock:
            r.clear_block(mac)
            self.store.data["blocks"].pop(mac, None)
            self.store.save()
            self.block_entries = r.block_names()
            self.log("unblocked %s (%s)" % (dev.get("name") or mac, mac))
            return self.device_view(mac)

    def add_site(self, url):
        url = re.sub(r"^https?://", "", (url or "").strip()).split("/")[0].lower()
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", url):
            raise ActionError("Write a website like example.com")
        r = self._need_router("sites")
        with self.action_lock:
            r.add_site(url)
            self.sites = r.sites()
            self.log("website blocked: %s" % url)

    def remove_site(self, url):
        r = self._need_router()
        with self.action_lock:
            r.remove_sites([url])
            self.sites = r.sites()
            self.log("website unblocked: %s" % url)

    def reconcile(self):
        """Move a download limit to a device's new address when its address changed."""
        for mac, lim in list(self.store.data["limits"].items()):
            dev = self.devices.get(mac)
            if not dev or not dev.get("online") or not dev.get("ip"):
                continue
            if lim.get("method") == "router" and lim.get("down") and dev["ip"] != lim.get("ip"):
                try:
                    self.set_limit(mac, lim["down"], lim["up"], method="router")
                    self.log("address of %s changed to %s; limit moved" % (mac, dev["ip"]))
                except RouterError as e:
                    self.log("could not move limit of %s: %s" % (mac, e))

    # ------------------------------------------------------------------ status for the page

    def overview(self, seconds=600):
        snap = self.traffic.snapshot(max(60, min(3600, int(seconds))))
        host = (self.cfg.get("router", {}).get("host") or "").replace("http://", "").split("/")[0].split(":")[0]
        devs = list(self.devices.values())
        return {
            "status": self.status, "traffic": snap,
            "ping": {"router": self.pinger.summary(host),
                     "internet": self.pinger.summary(self.cfg.get("internet_ping", "1.1.1.1")),
                     "internet_target": self.cfg.get("internet_ping", "1.1.1.1")},
            "counts": {"online": sum(1 for d in devs if d["online"]), "known": len(devs),
                       "wifi": sum(1 for d in devs if d["online"] and d["link"] == "wifi"),
                       "wired": sum(1 for d in devs if d["online"] and d["link"] == "wired"),
                       "limited": len(self.store.data["limits"]), "blocked": len(self.store.data["blocks"])},
            "usage": self.store.data["usage"], "caps": self.caps, "checks": self.checks, "sites": self.sites,
            "selftest": self.store.data["selftest"], "other_rules": self.other_rules,
        }
