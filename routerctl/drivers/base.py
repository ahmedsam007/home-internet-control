"""What every router driver offers the rest of the program.

A driver talks to one family of routers. The rest of the program (devices,
the page, the self-test, the report) only uses the methods below, so adding a
router means adding one driver file and listing it in ``drivers/__init__.py``.

Each driver says what it can do with ``capabilities()``. The page shows only
the buttons and cards the connected router supports.
"""

import base64
import http.cookiejar
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request


class RouterError(Exception):
    pass


class LoginError(RouterError):
    pass


class PageMissing(RouterError):
    pass


class ActionError(RouterError):
    pass


class NotSupported(ActionError):
    pass


# What a router can do. The page hides whatever the connected router lacks.
CAPABILITIES = {
    "devices": "Device list",
    "wifi": "Wi-Fi or cable shown per device",
    "traffic": "Internet speed graph",
    "speed_limit": "Speed limit per device",
    "pause": "Pause internet per device",
    "bedtime": "Bedtime per device",
    "sites": "Website blocking",
}

# Names this program gives the router entries it creates. Anything else on the
# router (rules someone made by hand) is shown but never changed.
UP_PREFIX, DOWN_PREFIX, BLOCK_PREFIX, SCHED_PREFIX = "ru", "rd", "bk", "rc"
OURS_RE = re.compile(r"^(r[ud][0-9a-f]{12}|bk[0-9a-f]{12}b?|rc\d{8}d\d+)$")

WAN_NAME_RE = re.compile(r"^(ppp|ptm|atm|nas|veip|wan|epon|gpon|ewan)", re.I)


def hexmac(mac):
    return mac.replace(":", "").lower()


def normalize_host(host):
    host = (host or "192.168.1.1").strip().rstrip("/")
    if not re.match(r"https?://", host):
        host = "http://" + host
    return host


class Driver:
    MODEL = ""            # id saved in config.json ("model")
    NAME = ""             # shown to people
    EXAMPLES = ""         # models this driver is meant for, shown in Settings

    def __init__(self, host, username, password, timeout=10, pages=None, params=None, log=None):
        self.base = normalize_host(host)
        self.username = username or "admin"
        self.password = password or ""
        self.timeout = timeout
        self.page_overrides = dict(pages or {})
        self.param_overrides = dict(params or {})
        self.log = log or (lambda msg: None)
        self.lock = threading.RLock()
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.auth_mode = None
        self._basic = None
        self.requests = 0
        self.last_headers = {}

    # ------------------------------------------------------------------ recognising the router

    @classmethod
    def identify(cls, status, text, headers):
        """How sure we are (0 = not at all) that the router's front page, read before
        logging in, belongs to this driver. `headers` is a dict with lower-case keys."""
        return 0

    # ------------------------------------------------------------------ HTTP

    def _fetch(self, path, form=None, extra_headers=None):
        url = path if path.startswith("http") else self.base + "/" + path.lstrip("/")
        headers = {"User-Agent": "Mozilla/5.0 (home-internet-control)", "Referer": self.base + "/",
                   "Accept": "text/html,*/*"}
        if self._basic:
            headers["Authorization"] = "Basic " + self._basic
        headers.update(extra_headers or {})
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                status, body = r.status, r.read()
                self.last_headers = {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            status, body = e.code, (e.read() or b"")
            self.last_headers = {k.lower(): v for k, v in (e.headers or {}).items()}
            e.close()
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            raise RouterError("Cannot reach the router at %s (%s)" % (self.base, getattr(e, "reason", e)))
        self.requests += 1
        return status, body.decode("utf-8", "replace")

    def _set_basic(self):
        self._basic = base64.b64encode(("%s:%s" % (self.username, self.password)).encode()).decode()

    # ------------------------------------------------------------------ the driver interface

    def login(self):
        raise NotImplementedError

    def capabilities(self):
        """{capability: True/False} for the names in CAPABILITIES."""
        return {k: False for k in CAPABILITIES}

    def checks(self):
        """[(label, ok)] shown under "What this router offers" on the Check page."""
        caps = self.capabilities()
        return [(label, bool(caps.get(k))) for k, label in CAPABILITIES.items()]

    def warnings(self, has_limits):
        """Router-specific warnings for the top of the page."""
        return []

    def info(self):
        return {}

    # devices -----------------------------------------------------------

    def leases(self):
        """[{'mac', 'ip', 'hostname', 'expires'}]; raises PageMissing when the router has no such list."""
        raise PageMissing("device list")

    def stations(self):
        """[{'mac', 'ifc', 'ssid'}] for devices on Wi-Fi; [] when unknown."""
        return []

    def arp(self):
        return []

    def counters(self):
        """{interface name: (received bytes, sent bytes)} as the router counts them."""
        return {}

    @staticmethod
    def pick_wan(names, preferred=None):
        if preferred and preferred in names:
            return preferred
        wan = [n for n in names if WAN_NAME_RE.match(n)]
        wan.sort(key=lambda n: (not n.lower().startswith("ppp"), n))
        return wan[0] if wan else None

    # speed limits ------------------------------------------------------

    def qos_enabled(self):
        return None

    def apply_limit(self, mac, ip, ifc, up_kbps, down_kbps):
        """Put a limit on the router, replacing this program's earlier one for `mac`.
        up_kbps / down_kbps of 0 mean no limit in that direction. Returns a list of
        problems (empty when everything was saved); raises when nothing could be done."""
        raise NotSupported("This router's speed limits are not supported yet.")

    def clear_limit(self, mac):
        raise NotSupported("This router's speed limits are not supported yet.")

    def limit_overview(self):
        """({mac: {'up': bool, 'down': bool}} for the limits this program has on the router,
        [{'name', 'src_mac', 'dst_ip'}] for limit rules made by hand in the router)."""
        return {}, []

    # blocking ------------------------------------------------------------

    def apply_block(self, mac, windows=None):
        """windows None = blocked all the time; otherwise [(start min, end min, day mask)] with
        start < end and days Mon=1 … Sun=64. Replaces this program's earlier block for `mac`.
        Returns the names of the router entries it made."""
        raise NotSupported("Blocking devices is not supported on this router yet.")

    def clear_block(self, mac):
        raise NotSupported("Blocking devices is not supported on this router yet.")

    def block_names(self):
        """Names of every block entry on the router (to see if ours are still there)."""
        return []

    def clock_set(self):
        """False when the router says its clock is not set (schedules then do nothing)."""
        return None

    # websites ------------------------------------------------------------

    def sites(self):
        raise PageMissing("website blocking")

    def add_site(self, url):
        raise NotSupported("Website blocking is not supported on this router.")

    def remove_sites(self, urls):
        raise NotSupported("Website blocking is not supported on this router.")

    # report --------------------------------------------------------------

    def probe(self):
        """What this router offers, for the report. Reads only."""
        return {"base": self.base, "auth": self.auth_mode, "features": {}}

    def report_lines(self):
        """Extra driver-specific lines for the report."""
        return []
