"""The local web page and its small JSON API.

Phone access: when it is switched on, the page also listens on the home
network. The computer itself never needs a PIN; phones enter the PIN once (or
scan the QR code, which carries it) and then stay signed in.
"""

import hmac
import http.server
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse

from . import drivers, report
from .control import Store
from .drivers import RouterError
from .qr import QR
from .selftest import SelfTest

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"manifest.webmanifest": "application/manifest+json", "icon-180.png": "image/png",
          "icon-192.png": "image/png", "icon-512.png": "image/png", "icon-maskable.png": "image/png"}
SESSION_DAYS = 180
COOKIE = "dslc_session"

DEFAULT_CONFIG = {
    "router": {"host": "192.168.1.1", "username": "admin", "password": ""},
    "model": "auto",            # "auto" or a driver id from routerctl/drivers (e.g. "dlink-broadcom")
    "detected_model": "",       # what auto-detect found last time
    "port": 8770,
    "phone_access": False,
    "phone_pin": "",
    "download_method": "auto",
    "internet_ping": "1.1.1.1",
    "poll_seconds": 5,
    "devices_seconds": 20,
    "ping_seconds": 10,
    "wifi_bands": {"wl0": "2.4 GHz", "wl1": "5 GHz"},
    "wan_interface": "",
    "pages": {},
    "params": {},
}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg


def save_config(path, cfg):
    if path is None:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)


def lan_ip(towards="192.168.1.1"):
    """This computer's address on the home network (no packet is sent)."""
    for target in (towards, "192.168.1.1", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 80))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return "127.0.0.1"


class App:
    def __init__(self, ctl, config_path, lan=False):
        self.ctl = ctl
        self.config_path = config_path
        self.lan_forced = lan            # started with --lan / start-with-phone
        self.selftest = SelfTest(ctl)
        self.lock = threading.Lock()
        self.sessions = Store(os.path.join(ctl.data_dir, "phone_sessions.json"), {"tokens": {}})
        self.fails = {}                  # ip -> [wrong tries, locked until]
        self.httpd = None
        self.port = int(ctl.cfg.get("port", 8770))
        self.restart = False
        self.trust_localhost = True      # tests switch this off to act like a phone
        ctl.save_config = self._save
        self.ensure_pin()

    # --- phone access

    @property
    def phone_on(self):
        return self.lan_forced or bool(self.ctl.cfg.get("phone_access"))

    def _save(self):
        with self.lock:
            save_config(self.config_path, self.ctl.cfg)

    def ensure_pin(self):
        if self.phone_on and not re.fullmatch(r"\d{4,8}", str(self.ctl.cfg.get("phone_pin", ""))):
            self.ctl.cfg["phone_pin"] = "%06d" % secrets.randbelow(10 ** 6)
            self._save()

    def phone_url(self, with_pin=False):
        host = (self.ctl.cfg.get("router", {}).get("host") or "192.168.1.1")
        host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
        url = "http://%s:%d/" % (lan_ip(host), self.port)
        return url + ("#pin=" + self.ctl.cfg["phone_pin"] if with_pin else "")

    def phone_info(self):
        return {"on": self.phone_on, "forced": self.lan_forced, "url": self.phone_url() if self.phone_on else "",
                "pin": self.ctl.cfg.get("phone_pin", "") if self.phone_on else "",
                "phones": len(self.sessions.data["tokens"])}

    def set_phone(self, body):
        cfg = self.ctl.cfg
        restart = False
        if "on" in body and not self.lan_forced:
            on = bool(body["on"])
            if on != bool(cfg.get("phone_access")):
                cfg["phone_access"] = on
                restart = True
        if body.get("new_pin"):
            cfg["phone_pin"] = ""
            self.sessions.data["tokens"] = {}      # every phone must sign in again
            self.sessions.save()
        if body.get("sign_out_all"):
            self.sessions.data["tokens"] = {}
            self.sessions.save()
        self.ensure_pin()
        self._save()
        self.ctl.log("phone access %s%s" % ("on" if self.phone_on else "off", ", new PIN" if body.get("new_pin") else ""))
        if restart:
            self.request_restart()
        return dict(self.phone_info(), restart=restart)

    def request_restart(self):
        """Re-open the listener (only on this computer, or on the whole home network)."""
        self.restart = True
        if self.httpd is not None:
            threading.Timer(0.5, self.httpd.shutdown).start()

    def check_pin(self, ip, pin):
        now = time.time()
        tries, until = self.fails.get(ip, [0, 0])
        if until > now:
            return None, "Too many wrong PINs. Try again in %d minutes." % max(1, round((until - now) / 60))
        good = str(self.ctl.cfg.get("phone_pin", ""))
        if good and hmac.compare_digest(str(pin or "").strip(), good):
            self.fails.pop(ip, None)
            token = secrets.token_urlsafe(24)
            tokens = self.sessions.data["tokens"]
            tokens[token] = {"since": now, "ip": ip}
            for old in sorted(tokens, key=lambda t: tokens[t]["since"])[:-20]:
                del tokens[old]
            self.sessions.save()
            self.ctl.log("phone signed in from %s" % ip)
            return token, ""
        tries += 1
        self.fails[ip] = [0, now + 300] if tries >= 5 else [tries, 0]
        return None, "Wrong PIN." if tries < 5 else "Too many wrong PINs. Try again in 5 minutes."

    def token_ok(self, token):
        t = self.sessions.data["tokens"].get(token or "")
        return bool(t) and time.time() - t["since"] < SESSION_DAYS * 86400

    # --- API handlers

    def settings(self):
        c = self.ctl.cfg
        r = c.get("router", {})
        detected = c.get("detected_model", "")
        return {"host": r.get("host", ""), "username": r.get("username", ""), "has_password": bool(r.get("password")),
                "model": c.get("model", "auto"), "models": drivers.model_list(),
                "detected": detected, "detected_name": drivers.model_name(detected),
                "download_method": c.get("download_method", "auto"), "internet_ping": c.get("internet_ping", "1.1.1.1"),
                "wifi_bands": c.get("wifi_bands", {})}

    def save_settings(self, body):
        cfg = json.loads(json.dumps(self.ctl.cfg))
        r = cfg.setdefault("router", {})
        host = (body.get("host") or r.get("host") or "192.168.1.1").strip()
        if not re.fullmatch(r"(https?://)?[A-Za-z0-9.\-]+(:\d+)?/?", host):
            return 400, {"error": "The router address looks wrong (example: 192.168.1.1)"}
        r["host"] = host
        r["username"] = (body.get("username") or r.get("username") or "admin").strip()
        if body.get("password"):
            r["password"] = body["password"]
        model = body.get("model") or cfg.get("model") or "auto"
        if model != "auto" and model not in drivers.MODELS:
            return 400, {"error": "Unknown router model"}
        cfg["model"] = model
        if body.get("download_method") in ("auto", "router", "upload_trick"):
            cfg["download_method"] = body["download_method"]
        if body.get("internet_ping"):
            cfg["internet_ping"] = body["internet_ping"].strip()
        if not r.get("password"):
            return 400, {"error": "Enter the router password"}
        kw = {"pages": cfg.get("pages"), "params": cfg.get("params"), "log": self.ctl.log}
        try:
            if model == "auto":
                drv = drivers.detect(r["host"], r["username"], r["password"], timeout=8, **kw)
                cfg["detected_model"] = drv.MODEL
            else:
                drv = drivers.make(model, r["host"], r["username"], r["password"], timeout=8, **kw)
                drv.login()
            drv.timeout = 10
        except RouterError as e:
            return 400, {"error": str(e)}
        with self.lock:
            save_config(self.config_path, cfg)
            self.ctl.reconfigure(cfg, router=drv)
        self.ctl.log("settings saved (router %s, user %s, model %s)" % (r["host"], r["username"], drv.NAME))
        return 200, {"ok": True, "model": drv.MODEL, "model_name": drv.NAME, "settings": self.settings()}

    def device_action(self, body):
        mac, action = body.get("mac", ""), body.get("action")
        c = self.ctl
        if action == "name":
            c.set_name(mac, body.get("name", ""))
            return {"ok": True}
        if action == "limit":
            return {"ok": True, "device": c.set_limit(mac, _kbps(body.get("down")), _kbps(body.get("up")))}
        if action == "unlimit":
            return {"ok": True, "device": c.remove_limit(mac)}
        if action == "block":
            return {"ok": True, "device": c.block(mac, body.get("mode", "pause"), body.get("start"), body.get("end"),
                                                  int(body.get("days") or 127))}
        if action == "unblock":
            return {"ok": True, "device": c.unblock(mac)}
        if action == "forget":
            c.forget(mac)
            return {"ok": True}
        raise RouterError("Unknown action")


def _kbps(v):
    try:
        return max(0, int(float(v or 0)))
    except (TypeError, ValueError):
        raise RouterError("Speed must be a number")


OFF_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>Phone access is off</title>
<body style="font:16px system-ui;padding:24px;max-width:420px;margin:auto"><h2>Phone access is off</h2>
<p>On the computer, open Home Internet Control → Settings → <b>Use from your phone</b> and switch it on.</p></body>"""


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = "home-internet-control"

    def log_message(self, *a):
        pass

    # --- who is asking

    def _host(self):
        return (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()

    def _local(self):
        """The computer the program runs on (no PIN needed)."""
        return (self.app.trust_localhost and self.client_address[0] in ("127.0.0.1", "::1")
                and self._host() in ("127.0.0.1", "localhost", "::1"))

    def _host_ok(self):
        if self._host() in ("127.0.0.1", "localhost", "::1"):
            return True
        # phones use the computer's address; any other name could be a DNS-rebinding trick
        return self.app.phone_on and bool(re.fullmatch(r"[\d.]+|[a-z0-9-]+(\.local|\.lan|\.home)?", self._host()))

    def _token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def _authed(self):
        return self._local() or (self.app.phone_on and self.app.token_ok(self._token()))

    # --- replies

    def _send(self, code, data, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json", extra)

    def _text(self, code, text, ctype="text/plain; charset=utf-8", extra=None):
        self._send(code, text.encode(), ctype, extra)

    # --- GET

    def do_GET(self):
        if not self._host_ok():
            return self._text(403, "forbidden")
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        app, ctl = self.app, self.app.ctl
        if not self._local() and not app.phone_on:
            return self._text(403, OFF_PAGE, "text/html; charset=utf-8")
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
                return self._text(200, f.read(), "text/html; charset=utf-8")
        if path.lstrip("/") in STATIC:
            name = path.lstrip("/")
            with open(os.path.join(WEB, name), "rb") as f:
                return self._send(200, f.read(), STATIC[name], {"Cache-Control": "max-age=86400"})
        if path == "/api/auth":
            return self._json(200, {"local": self._local(), "authed": self._authed(), "phone": app.phone_on})
        if not self._authed():
            return self._json(401, {"error": "Enter the PIN", "need_pin": True})
        try:
            if path == "/api/overview":
                qs = urllib.parse.parse_qs(u.query)
                try:
                    secs = int(qs.get("s", ["600"])[0])
                except ValueError:
                    secs = 600
                o = ctl.overview(secs)
                o["selftest_run"] = app.selftest.state
                o["local"] = self._local()
                return self._json(200, o)
            if path == "/api/devices":
                return self._json(200, {"devices": ctl.device_list()})
            if path == "/api/settings":
                return self._json(200, app.settings())
            if path == "/api/selftest":
                return self._json(200, app.selftest.state)
            if path == "/api/report":
                return self._text(200, report.build(ctl), extra={
                    "Content-Disposition": "inline; filename=router-report.txt"})
            if path == "/api/log":
                return self._json(200, {"lines": [l.rstrip("\n") for l in ctl.recent_log(100)]})
            if path == "/api/phone":
                if not self._local():
                    return self._json(200, {"on": True, "remote": True})
                return self._json(200, app.phone_info())
            if path == "/api/qr.svg":
                if not self._local() or not app.phone_on:
                    return self._text(403, "forbidden")
                return self._text(200, QR(app.phone_url(with_pin=True)).svg(), "image/svg+xml")
        except RouterError as e:
            return self._json(502, {"error": str(e)})
        self._text(404, "not found")

    # --- POST

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Requested-With") != "home-internet-control":
            return self._text(403, "forbidden")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100000:
            return self._json(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
        except ValueError:
            return self._json(400, {"error": "bad request"})
        path = urllib.parse.urlsplit(self.path).path
        app, ctl = self.app, self.app.ctl
        if not self._local() and not app.phone_on:
            return self._json(403, {"error": "Phone access is off"})
        if path == "/api/login":
            token, err = app.check_pin(self.client_address[0], body.get("pin"))
            if not token:
                return self._json(401, {"error": err})
            return self._json(200, {"ok": True}, {"Set-Cookie": "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                                                  % (COOKIE, token, SESSION_DAYS * 86400)})
        if path == "/api/logout":
            app.sessions.data["tokens"].pop(self._token(), None)
            app.sessions.save()
            return self._json(200, {"ok": True}, {"Set-Cookie": "%s=; Path=/; Max-Age=0" % COOKIE})
        if not self._authed():
            return self._json(401, {"error": "Enter the PIN", "need_pin": True})
        try:
            if path == "/api/phone":
                if not self._local():
                    return self._json(403, {"error": "Change phone access on the computer itself."})
                return self._json(200, app.set_phone(body))
            if path == "/api/settings":
                code, out = app.save_settings(body)
                return self._json(code, out)
            if path == "/api/device":
                return self._json(200, app.device_action(body))
            if path == "/api/site":
                if body.get("action") == "remove":
                    ctl.remove_site(body.get("url", ""))
                else:
                    ctl.add_site(body.get("url", ""))
                return self._json(200, {"ok": True, "sites": ctl.sites})
            if path == "/api/selftest":
                started = app.selftest.start()
                return self._json(200 if started else 409, app.selftest.state)
            if path == "/api/refresh":
                ctl._wake.set()
                return self._json(200, {"ok": True})
        except RouterError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:  # never leave the page hanging
            ctl.log("error in %s: %r" % (path, e))
            return self._json(500, {"error": "Unexpected error: %s" % e})
        self._text(404, "not found")


def make_server(app, port):
    """Listen only on this computer, or on the whole home network when phone access is on."""
    handler = type("H", (Handler,), {"app": app})
    app.port = port
    host = "0.0.0.0" if app.phone_on else "127.0.0.1"
    srv = http.server.ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    app.httpd = srv
    return srv
