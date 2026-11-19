"""The program's brain: devices, limits, blocks, background loops."""

import json
import os
import re
import threading
import time

from . import parsing as P
from .monitor import Pinger, Traffic
from .router import ActionError, PageMissing, Router, RouterError, WAN_NAME_RE

# Names this program gives the router entries it creates. Anything else on the
# router (rules someone made by hand) is shown but never changed.
UP_PREFIX, DOWN_PREFIX, BLOCK_PREFIX, SCHED_PREFIX = "ru", "rd", "bk", "rc"
OURS_RE = re.compile(r"^r[ud][0-9a-f]{12}$")


def hexmac(mac):
    return mac.replace(":", "").lower()


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
                       "last_ok": None, "qos_on": None, "clock_ok": None, "block_kind": None}
        self.router = None
        self.devices = {}
        self.rules = []
        self.block_entries = []
        self.sites = []
        self.features = {}
        self.action_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads = []
        self.reconfigure(config)

    # ------------------------------------------------------------------ setup

    def reconfigure(self, config):
        self.cfg = config
        rc = config.get("router", {})
        self.status["configured"] = bool(rc.get("password"))
        self.router = Router(rc.get("host", "192.168.1.1"), rc.get("username", "admin"), rc.get("password", ""),
                             pages=config.get("pages"), params=config.get("params"), log=self.log) \
            if self.status["configured"] else None
        self.traffic = Traffic()
        self._wake.set()

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
            if r is None:
                self._sleep(5)
                continue
            try:
                counters = r.counters()
                wan = Router.pick_wan(list(counters), self.cfg.get("wan_interface"))
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
            if self.router is None:
                self._sleep(5)
                continue
            try:
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
        r = self.router
        leases = r.leases()
        stations = r.stations()
        arp = r.arp() if r.has("arp") else []
        try:
            self.rules = r.qos()["rules"]
        except PageMissing:
            self.rules = []
        try:
            self.status["block_kind"], self.block_entries = r.blocks()
        except PageMissing:
            self.block_entries = []
        if slow:
            self.status["info"] = r.info()
            self.status["qos_on"] = r.qos_enabled()
            self.status["clock_ok"] = r.clock_set()
            try:
                self.sites = r.sites()
            except PageMissing:
                self.sites = None
            self.features = {f: r.has(f) for f in ("qos", "qosform", "blockmac", "sched", "urlflt", "stations", "arp")}
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
            elif pinged and self.features.get("stations", True):
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
        w = []
        if self.status["qos_on"] is False and self.store.data["limits"]:
            w.append("QoS is switched off on the router, so speed limits do nothing. Open a device and press Save to switch it on.")
        if self.status["clock_ok"] is False:
            w.append("The router's clock is not set. Bedtime blocks only work when it is (router: Setup → Time and Date).")
        if self.features and not self.features.get("qosform"):
            w.append("This router's speed-limit page was not found. Please send the report from the Check page.")
        self.status["warnings"] = w

    def device_view(self, mac):
        d = dict(self.devices.get(mac) or {"mac": mac, "name": self.store.data["names"].get(mac, "")})
        h = hexmac(mac)
        rules = {r["name"]: r for r in self.rules}
        lim = self.store.data["limits"].get(mac)
        if lim:
            want = []
            if lim.get("up_applied"):
                want.append(UP_PREFIX + h)
            if lim.get("method") == "router" and lim.get("down"):
                want.append(DOWN_PREFIX + h)
            missing = [n for n in want if n not in rules]
            d["limit"] = dict(lim, on_router=not missing, missing=missing)
        blk = self.store.data["blocks"].get(mac)
        names = {e["name"] for e in self.block_entries}
        if blk:
            d["block"] = dict(blk, on_router=all(n in names for n in blk.get("entries", [])))
        other = [r for r in self.rules if (r.get("src_mac") == mac or (d.get("ip") and r.get("dst_ip") == d.get("ip")))
                 and not r["name"].startswith((UP_PREFIX + h, DOWN_PREFIX + h))]
        if other:
            d["other_rules"] = [r["name"] for r in other]
        return d

    def device_list(self):
        macs = set(self.devices) | set(self.store.data["limits"]) | set(self.store.data["blocks"])
        out = [self.device_view(m) for m in macs]
        out.sort(key=lambda d: (not d.get("online"), (d.get("name") or d.get("hostname") or "~").lower(), d["mac"]))
        return out

    # ------------------------------------------------------------------ helpers

    def _need_router(self):
        if self.router is None:
            raise ActionError("Set the router password first (Settings).")
        return self.router

    def _device(self, mac):
        mac = P.norm_mac(mac)
        if not P.is_mac(mac):
            raise ActionError("Not a device address: %s" % mac)
        return mac, self.devices.get(mac) or {}

    @staticmethod
    def _wan_queue(queues):
        wan = [q for q in queues if "(wan)" in q["text"].lower() or WAN_NAME_RE.match(q["ifc"])]
        if not wan:
            wan = [q for q in queues if "wan" in q["text"].lower()]
        return max(wan, key=lambda q: q["prec"]) if wan else None

    @staticmethod
    def _down_queue(queues, ifc):
        if not ifc:
            return None
        mine = [q for q in queues if q["ifc"].split(".")[0] == ifc]
        return max(mine, key=lambda q: q["prec"]) if mine else None

    @staticmethod
    def _iface(ifaces, want):
        for i in ifaces:
            if i.lower() == want.lower():
                return i
        if want == "WAN":
            for i in ifaces:
                if "routed" in i.lower() or WAN_NAME_RE.match(i):
                    return i
        return want

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
        r = self._need_router()
        with self.action_lock:
            h = hexmac(mac)
            up_name, dn_name = UP_PREFIX + h, DOWN_PREFIX + h
            q = r.qos()
            ours = [x["name"] for x in q["rules"] if x["name"] in (up_name, dn_name)]
            if ours:
                r.remove_qos_rules(ours)
            if r.qos_enabled() is False:
                r.enable_qos()
                self.status["qos_on"] = True
            wan_q = self._wan_queue(q["queues"])
            if not wan_q:
                raise ActionError("The router has no internet (WAN) queue. Create one under Network Tools → QoS Queue.")
            method = method or self.download_method(dev)
            up_eff = up
            if down and method == "upload_trick":
                up_eff = min(up or 10 ** 9, max(50, down // 12))
            rec = {"down": down, "up": up, "method": method, "up_applied": up_eff, "ip": dev.get("ip", ""),
                   "set_at": time.time()}
            self.store.data["limits"][mac] = rec
            self.store.save()
            errors = []
            if up_eff:
                try:
                    r.add_qos_rule(up_name, self._iface(q["ifaces"], "LAN"), wan_q, up_eff, src_mac=mac)
                except RouterError as e:
                    errors.append("upload: %s" % e)
            if down and method == "router":
                if not dev.get("ip"):
                    errors.append("download: the device has no address right now; it is set when it comes online")
                else:
                    dq = self._down_queue(q["queues"], dev.get("ifc")) or wan_q
                    try:
                        r.add_qos_rule(dn_name, self._iface(q["ifaces"], "WAN"), dq, down, dst_ip=dev["ip"], ethertype="800")
                    except RouterError as e:
                        errors.append("download: %s" % e)
            self.log("limit %s (%s): down %s up %s via %s%s" % (dev.get("name") or mac, mac, down, up_eff, method,
                                                                 (" — " + "; ".join(errors)) if errors else ""))
            self.rules = r.qos()["rules"]
            if errors:
                raise ActionError("; ".join(errors))
            return self.device_view(mac)

    def remove_limit(self, mac):
        mac, dev = self._device(mac)
        r = self._need_router()
        with self.action_lock:
            h = hexmac(mac)
            names = [x["name"] for x in r.qos()["rules"] if x["name"] in (UP_PREFIX + h, DOWN_PREFIX + h)]
            r.remove_qos_rules(names)
            self.store.data["limits"].pop(mac, None)
            self.store.save()
            self.rules = r.qos()["rules"]
            self.log("limit removed for %s (%s)" % (dev.get("name") or mac, mac))
            return self.device_view(mac)

    def block(self, mac, mode="pause", start=None, end=None, days=127):
        """mode 'pause' = no internet until unblocked; 'bedtime' = no internet between start and end."""
        mac, dev = self._device(mac)
        r = self._need_router()
        with self.action_lock:
            self._remove_block_entries(mac)
            kind, _ = r.blocks()
            h = hexmac(mac)
            entries = []
            if mode == "pause":
                name = BLOCK_PREFIX + h
                if kind == "blockmac":
                    r.add_block(name, mac, "Always")
                else:
                    r.add_block(name, mac, days=127, start=0, end=1439)
                entries.append(name)
                rec = {"mode": "pause"}
            elif mode == "bedtime":
                days = int(days) & 127 or 127
                s, e = hhmm_to_min(start), hhmm_to_min(end)
                for i, (ws, we, wd) in enumerate(windows(s, e, days)):
                    name = BLOCK_PREFIX + h + ("" if i == 0 else "b")
                    if kind == "blockmac":
                        sched = "%s%02d%02d%02d%02dd%d" % (SCHED_PREFIX, ws // 60, ws % 60, we // 60, we % 60, wd)
                        if sched not in {x["name"] for x in r.schedules()}:
                            r.add_schedule(sched, wd, ws, we)
                        r.add_block(name, mac, sched)
                    else:
                        r.add_block(name, mac, days=wd, start=ws, end=we)
                    entries.append(name)
                rec = {"mode": "bedtime", "start": start, "end": end, "days": days}
            else:
                raise ActionError("Unknown block type")
            rec.update(entries=entries, set_at=time.time())
            self.store.data["blocks"][mac] = rec
            self.store.save()
            self.status["block_kind"], self.block_entries = r.blocks()
            self.log("block %s (%s): %s" % (dev.get("name") or mac, mac, rec))
            return self.device_view(mac)

    def _remove_block_entries(self, mac):
        r = self.router
        h = hexmac(mac)
        _, entries = r.blocks()
        mine = [e["name"] for e in entries if e["name"] in (BLOCK_PREFIX + h, BLOCK_PREFIX + h + "b")]
        r.remove_blocks(mine)
        if r.has("sched"):
            _, left = r.blocks()
            used = {e.get("schedule") for e in left}
            unused = [s["name"] for s in r.schedules() if s["name"].startswith(SCHED_PREFIX) and s["name"] not in used
                      and re.fullmatch(r"rc\d{8}d\d+", s["name"])]
            if unused:
                r.remove_schedules(unused)

    def unblock(self, mac):
        mac, dev = self._device(mac)
        self._need_router()
        with self.action_lock:
            self._remove_block_entries(mac)
            self.store.data["blocks"].pop(mac, None)
            self.store.save()
            self.status["block_kind"], self.block_entries = self.router.blocks()
            self.log("unblocked %s (%s)" % (dev.get("name") or mac, mac))
            return self.device_view(mac)

    def add_site(self, url):
        url = re.sub(r"^https?://", "", (url or "").strip()).split("/")[0].lower()
        if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", url):
            raise ActionError("Write a website like example.com")
        r = self._need_router()
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
            "usage": self.store.data["usage"], "features": self.features, "sites": self.sites,
            "selftest": self.store.data["selftest"],
            "other_rules": [r for r in self.rules if not OURS_RE.match(r["name"])],
        }
