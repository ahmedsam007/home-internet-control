"""The local web page and its small JSON API."""

import json
import os
import re
import threading
import http.server
import urllib.parse

from . import report
from .router import LoginError, Router, RouterError
from .selftest import SelfTest

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

DEFAULT_CONFIG = {
    "router": {"host": "192.168.1.1", "username": "admin", "password": ""},
    "port": 8770,
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


class App:
    def __init__(self, ctl, config_path, lan=False):
        self.ctl = ctl
        self.config_path = config_path
        self.lan = lan
        self.selftest = SelfTest(ctl)
        self.lock = threading.Lock()

    # --- API handlers return (status, payload)

    def settings(self):
        c = self.ctl.cfg
        r = c.get("router", {})
        return {"host": r.get("host", ""), "username": r.get("username", ""), "has_password": bool(r.get("password")),
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
        if body.get("download_method") in ("auto", "router", "upload_trick"):
            cfg["download_method"] = body["download_method"]
        if body.get("internet_ping"):
            cfg["internet_ping"] = body["internet_ping"].strip()
        if not r.get("password"):
            return 400, {"error": "Enter the router password"}
        try:
            test = Router(r["host"], r["username"], r["password"], timeout=8)
            test.login()
        except LoginError as e:
            return 400, {"error": str(e)}
        except RouterError as e:
            return 400, {"error": str(e)}
        with self.lock:
            save_config(self.config_path, cfg)
            self.ctl.reconfigure(cfg)
        self.ctl.log("settings saved (router %s, user %s)" % (r["host"], r["username"]))
        return 200, {"ok": True, "settings": self.settings()}

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


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = "dlink-dsl-control"

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            return True
        return self.app.lan and bool(re.fullmatch(r"[\d.]+|[a-z0-9-]+(\.local)?", host))

    def _json(self, code, obj):
        data = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, code, text, ctype="text/plain; charset=utf-8", extra=None):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._host_ok():
            return self._text(403, "forbidden")
        path = urllib.parse.urlsplit(self.path).path
        app, ctl = self.app, self.app.ctl
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(WEB, "index.html"), encoding="utf-8") as f:
                    return self._text(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/overview":
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try:
                    secs = int(qs.get("s", ["600"])[0])
                except ValueError:
                    secs = 600
                o = ctl.overview(secs)
                o["selftest_run"] = app.selftest.state
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
        except RouterError as e:
            return self._json(502, {"error": str(e)})
        self._text(404, "not found")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Requested-With") != "dlink-dsl-control":
            return self._text(403, "forbidden")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode() or "{}") if n else {}
        except ValueError:
            return self._json(400, {"error": "bad request"})
        path = urllib.parse.urlsplit(self.path).path
        app, ctl = self.app, self.app.ctl
        try:
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
    handler = type("H", (Handler,), {"app": app})
    host = "0.0.0.0" if app.lan else "127.0.0.1"
    srv = http.server.ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv
