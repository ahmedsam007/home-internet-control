"""Ping and traffic-speed bookkeeping."""

import platform
import random
import re
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

SYSTEM = platform.system()


def _ping_cmd(ip, timeout_ms):
    if SYSTEM == "Windows":
        return ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    if SYSTEM == "Darwin":
        return ["ping", "-c", "1", "-W", str(timeout_ms), ip]
    return ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]


def ping_once(ip, timeout_ms=1000):
    """Round-trip time in ms, or None when there is no reply."""
    kw = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "timeout": timeout_ms / 1000 + 2}
    if SYSTEM == "Windows":
        kw["creationflags"] = 0x08000000  # no console window
    t0 = time.perf_counter()
    try:
        p = subprocess.run(_ping_cmd(ip, timeout_ms), **kw)
    except (OSError, subprocess.TimeoutExpired):
        return None
    elapsed = (time.perf_counter() - t0) * 1000
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0 or "ttl=" not in out.lower():
        return None   # Windows returns 0 for "Destination host unreachable"; that has no TTL
    m = re.search(r"[=<]\s*([\d.]+)\s*ms", out)
    if m:
        return max(0.5, float(m.group(1)))
    return round(elapsed, 1)  # non-English ping output: use the measured time


class Pinger:
    def __init__(self, fake=False, window=30):
        self.fake = fake
        self.window = window
        self.results = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=16)

    def _one(self, ip):
        if self.fake:
            time.sleep(0.05)
            if random.random() < 0.04:
                return None
            return round(random.uniform(1, 9) if ip.startswith("192.168.") else random.uniform(25, 60), 1)
        return ping_once(ip)

    def run(self, ips):
        ips = sorted({ip for ip in ips if ip})
        now = time.time()
        for ip, ms in zip(ips, self.pool.map(self._one, ips)):
            with self.lock:
                self.results.setdefault(ip, deque(maxlen=self.window)).append((now, ms))

    def summary(self, ip):
        with self.lock:
            r = list(self.results.get(ip, ()))
        if not r:
            return {"ms": None, "avg": None, "loss": None, "last_ok": None, "samples": 0}
        ok = [ms for _, ms in r if ms is not None]
        last_ok = max((t for t, ms in r if ms is not None), default=None)
        return {"ms": r[-1][1], "avg": round(sum(ok) / len(ok), 1) if ok else None,
                "loss": round(100 * (len(r) - len(ok)) / len(r)), "last_ok": last_ok, "samples": len(r),
                "recent": [ms for _, ms in r[-20:]]}


def counter_delta(cur, prev):
    d = cur - prev
    if d < 0 and prev < 2 ** 32:
        d += 2 ** 32          # 32-bit counter wrapped
    if d < 0 or d > 2 ** 40:
        d = 0                 # counters were reset (router restart)
    return d


class Traffic:
    """Speeds from the router's byte counters."""

    def __init__(self, keep=720):
        self.prev = None
        self.prev_t = None
        self.history = deque(maxlen=keep)   # (time, down kbit/s, up kbit/s)
        self.ifaces = {}                     # name -> {'down': kbps to devices, 'up': kbps from devices}
        self.wan = None
        self.today = {"date": "", "down": 0, "up": 0}
        self.lock = threading.Lock()

    def update(self, counters, wan_name):
        now = time.time()
        with self.lock:
            self.wan = wan_name
            if self.prev is not None and now > self.prev_t:
                dt = now - self.prev_t
                ifaces = {}
                for name, (rx, tx) in counters.items():
                    if name not in self.prev:
                        continue
                    drx = counter_delta(rx, self.prev[name][0])
                    dtx = counter_delta(tx, self.prev[name][1])
                    if name == wan_name:
                        down, up = drx * 8 / dt / 1000, dtx * 8 / dt / 1000
                        self.history.append((now, round(down, 1), round(up, 1)))
                        day = time.strftime("%Y-%m-%d")
                        if self.today["date"] != day:
                            self.today = {"date": day, "down": 0, "up": 0}
                        self.today["down"] += drx
                        self.today["up"] += dtx
                    else:
                        # the router RECEIVES what a home device uploads
                        ifaces[name] = {"down": round(dtx * 8 / dt / 1000, 1), "up": round(drx * 8 / dt / 1000, 1)}
                self.ifaces = ifaces
            self.prev, self.prev_t = dict(counters), now

    def snapshot(self, seconds=600):
        with self.lock:
            cut = time.time() - seconds
            hist = [h for h in self.history if h[0] >= cut]
            now = hist[-1] if hist else None
            return {"wan": self.wan, "down": now[1] if now else None, "up": now[2] if now else None,
                    "history": hist, "ifaces": dict(self.ifaces), "today": dict(self.today),
                    "peak_down": max((h[1] for h in hist), default=None), "peak_up": max((h[2] for h in hist), default=None)}
