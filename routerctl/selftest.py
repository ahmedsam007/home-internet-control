"""Checks, on the real router, whether its speed limits actually work.

It limits THIS computer for about a minute, measures the speed with and
without the limit, then removes the limit again. The download side is the
uncertain one on these routers, so if the router ignores a download limit,
the program switches to the "upload trick" (a low upload cap also holds
download down) and tests that too.
"""

import http.client
import socket
import threading
import time
import urllib.request

DOWN_URL = "https://speed.cloudflare.com/__down?bytes=200000000"
UP_HOST, UP_PATH = "speed.cloudflare.com", "/__up"


def measure_down(seconds=8, url=DOWN_URL, warmup=1.5):
    """Average download speed in kbit/s over `seconds` (the first `warmup` seconds are skipped)."""
    req = urllib.request.Request(url, headers={"User-Agent": "home-internet-control"})
    t0 = time.time()
    got, t_start = 0, None
    with urllib.request.urlopen(req, timeout=15) as r:
        while time.time() - t0 < seconds:
            chunk = r.read(32768)
            if not chunk:
                break
            if t_start is None and time.time() - t0 >= warmup:
                t_start = time.time()
            elif t_start is not None:
                got += len(chunk)
    if t_start is None or time.time() - t_start < 1:
        return None
    return round(got * 8 / (time.time() - t_start) / 1000)


def measure_up(seconds=8, warmup=1.5):
    """Average upload speed in kbit/s. A small send buffer keeps the number honest at low speeds."""
    total = 200 * 1000 * 1000
    conn = http.client.HTTPSConnection(UP_HOST, timeout=15)
    conn.connect()
    try:
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
    except OSError:
        pass
    conn.putrequest("POST", UP_PATH)
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(total))
    conn.endheaders()
    block = b"\0" * 16384
    t0 = time.time()
    sent, t_start = 0, None
    try:
        while time.time() - t0 < seconds:
            conn.sock.sendall(block)
            if t_start is None and time.time() - t0 >= warmup:
                t_start = time.time()
            elif t_start is not None:
                sent += len(block)
    finally:
        conn.close()
    if t_start is None or time.time() - t_start < 1:
        return None
    return round(sent * 8 / (time.time() - t_start) / 1000)


def local_ip_towards(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    finally:
        s.close()


class SelfTest:
    def __init__(self, ctl):
        self.ctl = ctl
        self.state = {"running": False, "steps": [], "result": None, "error": ""}
        self.lock = threading.Lock()
        # the demo replaces these
        self.measure_down = measure_down
        self.measure_up = measure_up
        self.find_me = self._find_me

    def _step(self, text):
        self.state["steps"].append({"t": time.time(), "text": text})

    def _find_me(self):
        host = (self.ctl.cfg.get("router", {}).get("host") or "192.168.1.1").replace("http://", "").split("/")[0].split(":")[0]
        ip = local_ip_towards(host)
        for d in self.ctl.devices.values():
            if d.get("ip") == ip:
                return d
        return {"ip": ip}

    def start(self):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "steps": [], "result": None, "error": "", "started": time.time()}
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _limited(self, mac, down, up, method, what):
        self.ctl.set_limit(mac, down, up, method=method)
        self._step("Limit set; waiting for it to take effect…")
        time.sleep(4)
        self._step("Measuring %s with the limit…" % what)
        return self.measure_down() if what == "download" else self.measure_up()

    def _run(self):
        ctl = self.ctl
        me = None
        try:
            ctl._need_router("speed_limit")
            me = self.find_me()
            if not me.get("mac"):
                raise RuntimeError("This computer (%s) is not in the router's device list yet. Wait a minute and "
                                   "try again." % me.get("ip"))
            if me["mac"] in ctl.store.data["limits"]:
                raise RuntimeError("This computer already has a limit. Remove it first.")
            kind = "wifi" if me.get("link") == "wifi" else "wired"
            self._step("This computer: %s, %s (%s)" % (me.get("name") or me.get("hostname") or "this computer",
                                                       me["ip"], "Wi-Fi " + me.get("band", "") if kind == "wifi" else "cable"))
            self._step("Measuring normal download speed…")
            d0 = self.measure_down()
            self._step("Measuring normal upload speed…")
            u0 = self.measure_up()
            if not d0 or not u0:
                raise RuntimeError("Could not measure the internet speed. Is the internet working?")
            self._step("Normal speed: %.1f Mbit/s down, %.1f Mbit/s up" % (d0 / 1000, u0 / 1000))
            lim_d = int(min(3000, max(300, d0 / 4)))
            lim_u = int(min(1000, max(128, u0 / 4)))
            if d0 < lim_d * 2.5 or u0 < lim_u * 2.5:
                self._step("Note: the line is slow, so the result is less certain.")
            self._step("Test 1: limit this computer to %d kbit/s down and %d kbit/s up (router method)…" % (lim_d, lim_u))
            d1 = self._limited(me["mac"], lim_d, lim_u, "router", "download")
            self._step("Measuring upload with the limit…")
            u1 = self.measure_up()
            ctl.remove_limit(me["mac"])
            down_ok = bool(d1) and d1 <= lim_d * 1.6
            up_ok = bool(u1) and u1 <= lim_u * 1.6
            self._step("With limit: %.1f Mbit/s down (%s), %.1f Mbit/s up (%s)" % (
                (d1 or 0) / 1000, "limited ✓" if down_ok else "NOT limited ✗",
                (u1 or 0) / 1000, "limited ✓" if up_ok else "NOT limited ✗"))
            result = {"time": time.time(), "kind": kind, "base_down": d0, "base_up": u0, "limit_down": lim_d,
                      "limit_up": lim_u, "down": d1, "up": u1, "download_ok": down_ok, "upload_ok": up_ok}
            if not down_ok and up_ok:
                self._step("Test 2: the router ignores download limits; trying the upload trick…")
                d2 = self._limited(me["mac"], lim_d, 0, "upload_trick", "download")
                ctl.remove_limit(me["mac"])
                trick_ok = bool(d2) and d2 <= lim_d * 2.5
                result.update(trick_down=d2, trick_ok=trick_ok)
                self._step("Upload trick: %.1f Mbit/s down (%s)" % ((d2 or 0) / 1000, "works ✓" if trick_ok else "does not work ✗"))
            ctl.store.data["selftest"][kind] = result
            ctl.store.save()
            ctl.log("self-test (%s): %s" % (kind, result))
            if down_ok:
                verdict = "Speed limits work in both directions on %s devices." % ("Wi-Fi" if kind == "wifi" else "cable")
            elif up_ok and result.get("trick_ok"):
                verdict = ("Upload limits work. The router ignores download limits, so download is now held down "
                           "with a low upload cap (this also lowers upload).")
            elif up_ok:
                verdict = "Upload limits work; download limits do not. Please send the report."
            else:
                verdict = "The router did not limit this computer. Please send the report from this page."
            self._step(verdict)
            self.state["result"] = dict(result, verdict=verdict)
        except Exception as e:  # report anything to the page
            self.state["error"] = str(e)
            self._step("Stopped: %s" % e)
            if me and me.get("mac") and me["mac"] in ctl.store.data["limits"]:
                try:
                    ctl.remove_limit(me["mac"])
                except Exception:
                    pass
        finally:
            self.state["running"] = False
