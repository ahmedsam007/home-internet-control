"""A fake Bezeq VTech NB403-IL for the tests and `python run.py --demo vtech`.

The real router's pages have not been seen yet, so these pages are INVENTED:
a login form, a menu, a status page with a DHCP client table, and links that
must never be followed (reboot, logout, a save form). They only check that the
early-support driver logs in, finds the device table, and stays read-only.

    python tools/mock_vtech.py --port 8772      (user admin, password admin)
"""

import argparse
import http.server
import random
import threading
import urllib.parse

DEVICES = [
    {"mac": "3c:22:fb:30:40:01", "ip": "10.0.0.2", "host": "Living-room-TV"},
    {"mac": "a4:83:e7:30:40:02", "ip": "10.0.0.3", "host": "Phone-B"},
    {"mac": "f0:18:98:30:40:03", "ip": "10.0.0.4", "host": "Laptop-2"},
]

LOGIN = """<html><head><title>VTech NB403-IL</title></head><body>
<h1>Bezeq NB403-IL</h1>
<form name="login" method="post" action="/goform/login">
<input type="hidden" name="lang" value="he">
<input type="text" name="loginUser"><input type="password" name="loginPass">
<input type="submit" value="Login"></form></body></html>"""


class State:
    def __init__(self, user="admin", password="admin"):
        self.user, self.password = user, password
        self.sessions = set()
        self.requested = []
        self.lock = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    state = None
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, text, headers=None):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Server", "httpd")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v in self.state.sessions:
                return True
        return False

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        self.state.requested.append("POST " + self.path)
        if self.path == "/goform/login":
            if form.get("loginUser", [""])[0] == self.state.user and form.get("loginPass", [""])[0] == self.state.password:
                sid = str(random.randint(10 ** 9, 10 ** 10))
                self.state.sessions.add(sid)
                return self._send(302, "", {"Location": "/", "Set-Cookie": "sid=%s; path=/" % sid})
            return self._send(200, LOGIN)
        self._send(404, "not found")

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        self.state.requested.append(path)
        if not self._authed():
            return self._send(200, LOGIN)
        if path in ("/", "/index.html"):
            return self._send(200, """<html><head><title>NB403-IL</title><script src="/menu.js"></script></head><body>
<a href="/status.html">Status</a> <a href="/lan.html">LAN</a> <a href="/reboot.cgi">Reboot</a>
<a href="/logout.html">Log out</a></body></html>""")
        if path == "/menu.js":
            return self._send(200, "var pages = ['wifi.html', 'reset.html'];")
        if path == "/status.html":
            return self._send(200, "<html><head><title>Status</title></head><body><table>"
                                   "<tr><td>Firmware Version</td><td>BZ_2.02.07.09.13.01</td></tr></table></body></html>")
        if path == "/lan.html":
            rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (d["host"], d["mac"].upper(), d["ip"])
                           for d in DEVICES)
            return self._send(200, """<html><head><title>LAN</title></head><body><h2>DHCP Clients</h2>
<table><tr><th>Host Name</th><th>MAC Address</th><th>IP Address</th></tr>%s</table>
<form method="post" action="/goform/save_lan"><input name="lanIp" value="10.0.0.138"></form></body></html>""" % rows)
        if path == "/wifi.html":
            return self._send(200, "<html><head><title>Wireless</title></head><body><form action='/goform/wifi'>"
                                   "<input name='ssid'><select name='channel'></select></form></body></html>")
        self._send(404, "not found")


def serve(port=8772, host="127.0.0.1", user="admin", password="admin"):
    state = State(user, password)
    handler = type("H", (Handler,), {"state": state})
    srv = http.server.ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8772)
    a = ap.parse_args()
    srv, _ = serve(a.port)
    print("Fake VTech NB403-IL on http://127.0.0.1:%d (admin / admin). Ctrl+C to stop." % srv.server_address[1])
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
