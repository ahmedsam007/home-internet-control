"""A fake D-Link DSL router for trying the program and for the tests.

It copies the shapes of the real pages (JavaScript variables such as
``qosgs`` and ``queueInfo``, URL-building ``btnApply`` functions, one-time
``sessionKey`` values, a cookie login) closely enough to exercise every code
path. It is NOT the real router: a firmware can still differ, which is what the
Check page and its report are for.

    python tools/mock_router.py --port 8771      (user admin, password admin)
"""

import argparse
import http.server
import random
import threading
import time
import urllib.parse

DEVICES = [
    {"mac": "3c:22:fb:10:20:01", "ip": "192.168.1.2", "host": "Living-room-TV", "ifc": "eth1"},
    {"mac": "a4:83:e7:10:20:02", "ip": "192.168.1.3", "host": "Phone-A", "ifc": "wl0"},
    {"mac": "f0:18:98:10:20:03", "ip": "192.168.1.4", "host": "Laptop", "ifc": "wl1"},
    {"mac": "d8:3a:dd:10:20:04", "ip": "192.168.1.5", "host": "Tablet", "ifc": "wl0"},
    {"mac": "60:45:cb:10:20:05", "ip": "192.168.1.6", "host": "PlayStation", "ifc": "eth2"},
    {"mac": "9c:b6:d0:10:20:06", "ip": "192.168.1.7", "host": "", "ifc": "wl1"},
]

QUEUES = "".join("%d:wl0&Key%d&Pre%d|" % (k, k, k) for k in range(1, 9)) + \
         "".join("%d:wl1&Key%d&Pre%d|" % (32 + k, 32 + k, k) for k in range(1, 9)) + "67:ppp0.1(wan)&Key67&Pre1|"
IFACES = "LAN|WAN|Local|eth0|eth1|eth2|eth3|ppp0.1(routed)|wl0|wl1|"


class State:
    def __init__(self, user="admin", password="admin"):
        self.user, self.password = user, password
        self.sessions = set()
        self.key = None
        self.lock = threading.Lock()
        self.enbl_qos = "1"
        # one leftover rule like the real router had: wl0 queue key 1, 1024 kbit/s, disabled
        self.rules = [["old", "1", "LAN", "", "", "", "", "", "", "", "", "", "", "1", "", "", "", "1024", "0"]]
        self.blocks = []        # [name, mac, schedule]
        self.scheds = []        # [name, '', days, start, end]
        self.urls = []          # [url, schedule]
        self.counters = {}      # ifc -> [rx, tx]
        self.t0 = time.time()
        self.last = time.time()
        self.actions = []

    def new_key(self):
        self.key = str(random.randint(100000000, 2147483647))
        return self.key

    def tick(self):
        """Advance fake traffic counters."""
        now = time.time()
        dt, self.last = now - self.last, now
        wan = self.counters.setdefault("ppp0.1", [10 ** 9, 2 * 10 ** 8])
        for d in DEVICES:
            down = random.uniform(0, 600_000) * (3 if d["ifc"] == "eth1" else 1)   # bytes/s to the device
            up = random.uniform(0, 80_000)
            for r in self.rules:
                if r[-1] == "1" and r[4].split("/")[0] == d["mac"] and r[-2]:
                    up = min(up, int(r[-2]) * 125)
                if r[-1] == "1" and r[7].split("/")[0] == d["ip"] and r[-2]:
                    down = min(down, int(r[-2]) * 125)
            if any(b[1] == d["mac"] and b[2] == "Always" for b in self.blocks):
                down = up = 0
            c = self.counters.setdefault(d["ifc"], [0, 0])
            c[0] += int(up * dt)        # router receives from the device = upload
            c[1] += int(down * dt)
            wan[0] += int(down * dt)    # WAN receives = download
            wan[1] += int(up * dt)
        for ifc in ("eth0", "eth3"):
            self.counters.setdefault(ifc, [0, 0])


LOGIN = """<html><head><title>Login</title></head><body>
<form focus="password" name="frmLogin" method="POST" action="login.cgi">
<input type="text" name="username"><input type="password" name="password">
<input type="button" value="Log In" onclick="checkForm()"></form>
<script>var captchaEnbl = '0';</script></body></html>"""


def page(title, body, script=""):
    return ("<html><head><title>%s</title><script language='javascript'>\n%s\n</script>"
            "<script language='JavaScript' src='menu.js'></script></head><body><blockquote>%s</blockquote></body></html>"
            % (title, script, body))


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
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        c = self.headers.get("Cookie", "")
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "SESSIONID" and v in self.state.sessions:
                return True
        return False

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if self.path.startswith("/login.cgi"):
            if form.get("username", [""])[0] == self.state.user and form.get("password", [""])[0] == self.state.password:
                sid = str(random.randint(10 ** 9, 10 ** 10))
                self.state.sessions.add(sid)
                return self._send(200, page("ok", "<script>top.location='index.html'</script>Welcome"),
                                  {"Set-Cookie": "SESSIONID=%s; path=/" % sid})
            return self._send(200, LOGIN)
        self._send(404, "not found")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path.lstrip("/") or "index.html"
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query, keep_blank_values=True).items()}
        if path in ("login.html",):
            return self._send(200, LOGIN)
        if not self._authed():
            return self._send(200, LOGIN)
        st = self.state
        with st.lock:
            st.tick()
            if path.endswith((".cmd", ".tod", ".sched")) and q.get("action", "view") not in ("view",):
                if q.get("sessionKey") != st.key:
                    return self._send(200, page("err", "<b>Error: invalid session key.</b>"))
                st.new_key()
                st.actions.append((path, q))
                return self._send(200, self.action(path, q))
            fn = ROUTES.get(path)
            if not fn:
                return self._send(404, "<html><body>404 File not found</body></html>")
            return self._send(200, fn(st))

    def action(self, path, q):
        st = self.state
        a = q.get("action")
        if path == "qoscls.cmd":
            if a == "add":
                if len(st.rules) >= 16 or not q.get("clsName") or q.get("queuekey") in (None, "-1"):
                    return page("err", "<b>Add QoS classification failed. Error 9002.</b>")
                if any(r[0] == q["clsName"] for r in st.rules):
                    return page("err", "<b>Error: the class name already exists.</b>")
                st.rules.append([q.get("clsName"), q.get("rulsOrder", ""), q.get("classIf", ""), q.get("ethertype", ""),
                                 q.get("srcMacAddr", ""), q.get("dstMacAddr", ""), q.get("srcAddr", ""), q.get("dstAddr", ""),
                                 q.get("protocol", ""), "", "", "", "", q.get("queuekey", ""), "", "", "",
                                 q.get("rate", ""), q.get("rulsEnbl", "0")])
            elif a == "remove":
                names = [n for n in q.get("rmLst", "").split(",") if n]
                st.rules = [r for r in st.rules if r[0] not in names]
            return qosclscfg(st)
        if path == "qosmgmt.cmd":
            st.enbl_qos = q.get("enblQos", st.enbl_qos)
            return qosqmgmt(st)
        if path == "blockmac.tod":
            if a == "add":
                st.blocks.append([q.get("username"), q.get("mac", "").lower(), q.get("schedule", "Always")])
            elif a == "remove":
                names = [n for n in q.get("rmLst", "").split(",") if n]
                st.blocks = [b for b in st.blocks if b[0] not in names]
            return todadd(st)
        if path == "sciptsched.sched":
            if a == "add":
                st.scheds.append([q.get("schedule"), "", q.get("days"), q.get("start_time"), q.get("end_time")])
            elif a == "remove":
                names = [n for n in q.get("rmLst", "").split(",") if n]
                st.scheds = [s for s in st.scheds if s[0] not in names]
            return iptschedadd(st)
        if path == "urlfilter.cmd":
            if a == "add":
                st.urls.append([q.get("TodUrl"), q.get("schedule", "Always")])
            elif a == "remove":
                names = [n for n in q.get("rmLst", "").split(",") if n]
                st.urls = [x for x in st.urls if x[0] not in names]
            return urlfltadd(st)
        return page("err", "Error: unknown action")


def index(st):
    return page("D-Link", "<frameset><frame src='info.html'></frameset>")


def info(st):
    up = int(time.time() - st.t0)
    rows = [("Model Name", "DSL-G256DG (demo)"), ("Firmware Version", "EG_1.00 (demo)"),
            ("System Up Time", "%dD %dH %dM %dS" % (up // 86400, up // 3600 % 24, up // 60 % 60, up % 60)),
            ("Connection Status", "Connected"), ("IP Address", "100.64.12.34")]
    return page("Info", "<table>" + "".join("<tr><td class=form_label>%s:</td><td>%s</td></tr>" % r for r in rows) + "</table>")


def dhcpinfo(st):
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>23 hours, 10 minutes</td></tr>"
                   % (d["host"], d["mac"].upper(), d["ip"]) for d in DEVICES)
    return page("DHCP", "<table class=formlisting><tr><td class='form_label_col'>Hostname</td><td>MAC Address</td>"
                        "<td>IP Address</td><td>Expires In</td></tr>%s</table>" % rows)


def wlstationlist(st):
    rows = "".join("<tr><td>%s</td><td>Yes</td><td>Yes</td><td>Home-%s</td><td>%s</td></tr>"
                   % (d["mac"].upper(), "5G" if d["ifc"] == "wl1" else "2G", d["ifc"])
                   for d in DEVICES if d["ifc"].startswith("wl"))
    return page("Stations", "<table><tr><td>MAC</td><td>Associated</td><td>Authorized</td><td>SSID</td>"
                            "<td>Interface</td></tr>%s</table>" % rows)


def _stat_rows(st, names, extra=0):
    out = ""
    for n in names:
        rx, tx = st.counters.get(n, [0, 0])
        pre = "<td>%s</td>" % n + "<td>-</td>" * extra
        out += "<tr>%s<td>%d</td><td>%d</td><td>0</td><td>0</td><td>%d</td><td>%d</td><td>0</td><td>0</td></tr>" % (
            pre, rx, rx // 900, tx, tx // 1200)
    return out


def statsifc(st):
    return page("Stats", "<table><tr><td>Interface</td><td colspan=4>Received</td><td colspan=4>Transmitted</td></tr>"
                         + _stat_rows(st, ["eth0", "eth1", "eth2", "eth3", "wl0", "wl1"]) + "</table>",
                "var sessionKey='%s';" % st.new_key())


def statsifcwan(st):
    return page("Stats", "<table>" + _stat_rows(st, ["eth0", "eth1", "eth2", "eth3", "wl0", "wl1"]) + "</table>"
                + "<table><tr><td>Interface</td><td>PVC</td><td>Protocol</td><td>Service</td></tr>"
                + _stat_rows(st, ["ppp0.1"], extra=3) + "</table>", "var sessionKey='%s';" % st.new_key())


QOS_JS = r"""
var enableTc = '1';
var queueInfo = '%(queues)s';
var queues  = queueInfo.split('|');
var rateLimit = '1';
var qosgs = "%(rules)s";
var qosclsRulsOrder = '%(order)s';
var qosIntf = '%(ifaces)s';
function btnApply() {
   var loc = 'qoscls.cmd?action=add&isWired=1';
   with ( document.forms[0] ) {
      loc += '&clsName=' + encodeUrl(txtclsname.value);
      loc += '&rulsOrder=' + rulsorder.options[rulsorder.selectedIndex].value
      loc += '&rulsEnbl=' + rulsenbl.options[rulsenbl.selectedIndex].value;
      val = classIfc.options[classIfc.selectedIndex].value.split('/');
      loc += '&classIf=' + val[0];
      if ( ethtype.selectedIndex > 0 )
         loc += '&ethertype=' + ethtype.options[ethtype.selectedIndex].value;
      if ( srcmacaddr.value != "" )
         loc += '&srcMacAddr=' + srcmacaddr.value;
      if ( dstmacaddr.value != "" )
         loc += '&dstMacAddr=' + dstmacaddr.value;
      if ( srcaddr.value != "" ) {
         loc += '&ipoptionlist=' + ipoptionlist.selectedIndex;
         loc += '&srcAddr=' + srcaddr.value;
      }
      if ( dstaddr.value != "" )
         loc += '&dstAddr=' + dstaddr.value;
      loc += '&vlan8021p=' + vlan8021p.options[vlan8021p.selectedIndex].value;
      loc += '&queuekey=' + queuekey.options[queuekey.selectedIndex].value;
      val = queuekey.options[queuekey.selectedIndex].text.split('&');
      loc += '&egress=' + val[0];
      loc += '&dscpMark=' + dscp.options[dscp.selectedIndex].value;
      loc += '&wanVlan8021p=' + wanVlan8021p.options[wanVlan8021p.selectedIndex].value;
      if ( enableTc == '1' ) {
         if ( txtrate.value != "" ) {
            loc += '&rate=' + txtrate.value;
         }
      }
   }
   loc += '&sessionKey=%(key)s';
   var code = 'location="' + loc + '"';
   eval(code);
}
function removeClick(rml) {
   var lst = '';
   var loc = 'qoscls.cmd?action=remove&rmLst=' + lst;
   var code = 'location="' + loc + '"';
   eval(code);
}
"""


def qosclscfg(st):
    n = len(st.rules)
    order = "|".join([str(i) for i in range(1, n + 1)] + ["Last"]) + "|"
    js = QOS_JS % {"queues": QUEUES, "rules": "".join(",".join(r) + "|" for r in st.rules),
                   "order": order, "ifaces": IFACES, "key": st.new_key()}
    body = ("<form><table class=formlisting><script>for (i = 0; i < gNum; i++) document.writeln('<tr>');</script></table>"
            "<td>Set Rate Limit:</td><td><input type=text name=txtrate> [Kbits/s]</td></form>")
    return page("QoS", body, js)


def qosqmgmt(st):
    return page("QoS", "<form><input type=checkbox name=enableQos></form>",
                "var enblQos = '%s';\nvar defMark = '-1';\nfunction btnApply() {\n var loc = 'qosmgmt.cmd?action=savapply';\n"
                " loc += '&enblQos=1';\n loc += '&defaultQueue=' + '-1';\n loc += '&defaultDscpMark=' + dscpMark.value;\n"
                " loc += '&sessionKey=%s';\n}\n" % (st.enbl_qos, st.new_key()))


def todadd(st):
    macs = "".join("%s,%s,%s|" % tuple(b) for b in st.blocks)
    scheds = "".join(s[0] + "|" for s in st.scheds)
    js = ("var macs = '%s';\nvar schedule = '%s';\nfunction btnApply() {\n  var loc = 'blockmac.tod?action=add&';\n"
          "        loc += \"username=\"+username.value+\"&mac=\"+macUsed;\n"
          "\t\tloc += '&schedule=' + iptval;\n        if('1' != '1')\n"
          "          alert(\"WARNING: Modem time is not set and Parental Control will not work correctly without it!\");\n"
          "  loc += '&sessionKey=%s';\n}\nfunction removeClick(rml) {\n var loc = 'blockmac.tod?action=remove&rmLst=' + lst;\n}\n"
          % (macs, scheds, st.new_key()))
    return page("Parental", "<form>Block MAC Address</form>", js)


def iptschedadd(st):
    tods = "".join(",".join(s) + "|" for s in st.scheds)
    js = ("var tods = '%s';\nfunction btnApply() {\n  var loc = 'sciptsched.sched?action=add&';\n"
          "        loc += \"schedule=\"+rulename.value;\n        loc += \"&days=\" + day;\n"
          "        loc += '&start_time=';\n        loc += st_time;\n        loc += '&end_time=';\n        loc += en_time;\n"
          "  loc += '&sessionKey=%s';\n}\n" % (tods, st.new_key()))
    return page("Schedules", "<form>Schedules</form>", js)


def urlfltadd(st):
    urls = "".join("%s,%s|" % tuple(u) for u in st.urls)
    js = ("var urls = '%s';\nfunction btnApply() {\n   var loc = 'urlfilter.cmd?action=add';\n"
          "\t      loc += '&TodUrl=' + encodeUrl(ret);\n\t\tloc += '&schedule=' + iptval;\n"
          "   loc += '&sessionKey=%s';\n}\n" % (urls, st.new_key()))
    return page("URL", "<form>Block Website</form>", js)


ROUTES = {
    "index.html": index, "info.html": info, "dhcpinfo.html": dhcpinfo, "wlstationlist.cmd": wlstationlist,
    "statsifc.html": statsifc, "statsifcwan.html": statsifcwan, "qosclscfg.html": qosclscfg,
    "qosqmgmt.html": qosqmgmt, "todadd.html": todadd, "iptschedadd.html": iptschedadd, "urlfltadd.html": urlfltadd,
}


def serve(port=8771, host="127.0.0.1", user="admin", password="admin"):
    """Start the fake router in a background thread; returns (server, state)."""
    state = State(user, password)
    handler = type("H", (Handler,), {"state": state})
    srv = http.server.ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args()
    serve(a.port)
    print("Fake router on http://127.0.0.1:%d (admin / admin). Ctrl+C to stop." % a.port)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
