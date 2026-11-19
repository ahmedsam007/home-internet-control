"""D-Link DSL routers with the Broadcom web interface (orange-and-black SETUP /
ADVANCED / MAINTENANCE / STATUS pages, e.g. DSL-G256DG, DSL-2740B, DSL-2750U).

Every change works the way the router's own pages do it: load the page (which
carries a one-time ``sessionKey``), then load the URL its JavaScript would
build, then read the list again to confirm the router saved it.

The parameter names are learned from each page's own JavaScript, so small
differences between firmware versions are handled. Anything that still does
not fit can be overridden in config.json ("pages" and "params").
"""

import base64
import re
import urllib.parse

from .. import parsing as P
from .base import (BLOCK_PREFIX, DOWN_PREFIX, OURS_RE, SCHED_PREFIX, UP_PREFIX, WAN_NAME_RE, ActionError, Driver,
                   LoginError, PageMissing, RouterError, hexmac)


# feature -> candidate pages, each with a text that must appear in it (None = any page)
PAGES = {
    "info": [("info.html", None), ("status.html", None)],
    "dhcp": [("dhcpinfo.html", None), ("dhcpinfo.cmd", None)],
    "stations": [("wlstationlist.cmd", None), ("wlstationlist.html", None)],
    "arp": [("arpview.cmd", None), ("arpinfo.html", None)],
    "lanstats": [("statsifc.html", None)],
    "wanstats": [("statsifcwan.html", None), ("statswan.cmd", None)],
    "qos": [("qosclscfg.html", "qoscls.cmd"), ("qoscls.cmd?action=view", "qoscls.cmd"),
            ("qoscls.html", "qoscls.cmd")],
    "qosform": [("qosclscfg.html", "qoscls.cmd?action=add"), ("qosclsadd.html", "qoscls.cmd?action=add"),
                ("qoscls.html", "qoscls.cmd?action=add"), ("qoscls.cmd?action=view", "qoscls.cmd?action=add")],
    "qosmgmt": [("qosqmgmt.html", "qosmgmt.cmd"), ("qosmgmt.html", "qosmgmt.cmd")],
    "blockmac": [("todadd.html", ".tod?action="), ("todmngr.tod?action=view", ".tod?action=")],
    "sched": [("iptschedadd.html", "sciptsched"), ("sciptsched.cmd", "sciptsched")],
    "urlflt": [("urlfltadd.html", "urlfilter.cmd"), ("url_add.html", "urlfilter"),
               ("urlfilter.cmd?action=view", "urlfilter")],
}

def _q(value):
    """Encode a value the way the router's own JavaScript sends it (':' and '.' stay as they are)."""
    return urllib.parse.quote(str(value), safe=":./-_")


class DLinkBroadcom(Driver):
    MODEL = "dlink-broadcom"
    NAME = "D-Link DSL (Broadcom)"
    EXAMPLES = "DSL-G256DG, DSL-2740B, DSL-2750U and other D-Link DSL routers with the orange-and-black page"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._login_variant = None
        self.found = {}
        self._qos_on = None
        self._block_kind = None

    @classmethod
    def identify(cls, status, text, headers):
        t = (text or "").lower()
        score = 0
        if "login.cgi" in t:
            score += 2
        if "frmlogin" in t:
            score += 1
        if "captchaenbl" in t:
            score += 2
        if "sessionkey" in t:
            score += 1
        if "d-link" in t or re.search(r"\bdsl-\w+", t):
            score += 2
        if "micro_httpd" in headers.get("server", "").lower():
            score += 2
        if status == 401 and re.search(r"d-?link|dsl", headers.get("www-authenticate", ""), re.I):
            score += 3
        return score

    # ------------------------------------------------------------------ HTTP

    def _logged_in(self):
        for page in ("info.html", "dhcpinfo.html", "statsifc.html"):
            s, t = self._fetch(page)
            if s == 200 and not P.looks_like_login(t):
                return True
            if s in (401, 403) or (s == 200 and P.looks_like_login(t)):
                return False
        return False

    def login(self):
        with self.lock:
            self.auth_mode = None
            s, t = self._fetch("/")
            if s == 401:
                self._set_basic()
                s, t = self._fetch("/")
                if s == 401:
                    self._basic = None
                    raise LoginError("The router did not accept the user name or password.")
                self.auth_mode = "basic"
                return
            if not P.looks_like_login(t):
                if self._logged_in():
                    self.auth_mode = "none"
                    return
                s2, t2 = self._fetch("login.html")
                if s2 == 200 and P.looks_like_login(t2):
                    t = t2
            if re.search(r"captchaEnbl\s*=\s*['\"]?1\b", t):
                raise LoginError("The router asks for a picture code (captcha) at login, "
                                 "which this program cannot type. Turn the captcha off in the router.")
            m = re.search(r"""(?i)<form[^>]*action\s*=\s*['"]?([^'" >]+\.cgi)""", t)
            action = m.group(1) if m else "login.cgi"
            variants = ["plain", "base64"]
            if self._login_variant in variants:
                variants.remove(self._login_variant)
                variants.insert(0, self._login_variant)
            for variant in variants:
                pw = self.password if variant == "plain" else base64.b64encode(self.password.encode()).decode()
                self._fetch(action, {"username": self.username, "password": pw})
                if self._logged_in():
                    self._login_variant = variant
                    self.auth_mode = "form-" + variant
                    self.log("logged in to the router (%s)" % self.auth_mode)
                    return
            raise LoginError("The router did not accept the user name or password.")

    def get(self, path):
        with self.lock:
            if self.auth_mode is None:
                self.login()
            s, t = self._fetch(path)
            if s in (401, 403) or (s == 200 and "login" not in path and P.looks_like_login(t)):
                self.login()
                s, t = self._fetch(path)
            if s == 404:
                raise PageMissing(path)
            if s >= 400:
                raise RouterError("The router answered HTTP %s for %s" % (s, path.split("?")[0]))
            return t

    # ------------------------------------------------------------------ pages

    def page(self, feature):
        """Path of the page that serves `feature`, or None if this router has none."""
        if feature in self.page_overrides:
            return self.page_overrides[feature]
        if feature in self.found:
            return self.found[feature]
        with self.lock:
            for path, marker in PAGES.get(feature, []):
                try:
                    t = self.get(path)
                except PageMissing:
                    continue
                if marker is None or marker in t:
                    self.found[feature] = path
                    return path
            self.found[feature] = None
            return None

    def fetch(self, feature):
        path = self.page(feature)
        if not path:
            raise PageMissing("no page found for %s" % feature)
        return self.get(path)

    def has(self, feature):
        try:
            return self.page(feature) is not None
        except RouterError:
            return False

    def _param(self, spec, key, *candidates):
        if key in self.param_overrides:
            return self.param_overrides[key]
        found = spec.pick(*candidates) if spec is not None else None
        return found or candidates[0]

    def _send(self, base, pairs, key):
        query = "&".join("%s=%s" % (k, _q(v)) for k, v in pairs if k)
        url = base + ("&" if "?" in base else "?") + query
        if key:
            url += "&sessionKey=" + key
        return self.get(url)

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _js_records_with_mac(text):
        out = []
        for value in P.js_vars(text).values():
            if not P.MAC_RE.search(value):
                continue
            for rec in value.split("|"):
                fields = [f.strip() for f in re.split(r"[,;/]", rec)]
                if any(P.is_mac(f) for f in fields):
                    out.append(fields)
        return out

    def info(self):
        try:
            t = self.fetch("info")
        except PageMissing:
            return {}
        lv = P.label_values(t)
        v = P.js_vars(t)
        out = {}
        wanted = [("model", r"model|product"), ("firmware", r"firmware|software version"),
                  ("hardware", r"hardware"), ("uptime", r"up ?time"), ("wan_ip", r"(wan )?ip address|^ip$"),
                  ("line", r"connection status|line rate|link status")]
        for key, pat in wanted:
            for label, value in lv.items():
                if re.search(pat, label, re.I) and value:
                    out.setdefault(key, value)
        for key, name in (("model", "modelName"), ("firmware", "swVersion"), ("uptime", "sysUpTime")):
            if key not in out and v.get(name):
                out[key] = v[name]
        return out

    def leases(self):
        t = self.fetch("dhcp")
        out = []
        for row in P.table_rows(t):
            cells = row["cells"]
            macs = [c for c in cells if P.is_mac(c)]
            if not macs:
                continue
            ips = [c for c in cells if P.is_ip(c)]
            host = cells[0] if cells and not P.is_mac(cells[0]) and not P.is_ip(cells[0]) else ""
            out.append({"mac": P.norm_mac(macs[0]), "ip": ips[0] if ips else "", "hostname": host,
                        "expires": cells[-1] if len(cells) > 3 else ""})
        if not out:
            for f in self._js_records_with_mac(t):
                mac = next(x for x in f if P.is_mac(x))
                ip = next((x for x in f if P.is_ip(x)), "")
                host = next((x for x in f if x and not P.is_mac(x) and not P.is_ip(x)), "")
                out.append({"mac": P.norm_mac(mac), "ip": ip, "hostname": host, "expires": ""})
        return out

    def stations(self):
        try:
            t = self.fetch("stations")
        except PageMissing:
            return []
        out = []
        for row in P.table_rows(t):
            cells = row["cells"]
            macs = [c for c in cells if P.is_mac(c)]
            if not macs:
                continue
            ifc = next((c for c in cells if re.match(r"^wl\d+(\.\d+)?$", c)), "")
            others = [c for c in cells if c and not P.is_mac(c) and c != ifc and c.lower() not in ("yes", "no")]
            out.append({"mac": P.norm_mac(macs[0]), "ifc": ifc, "ssid": others[0] if others else ""})
        if not out:
            for f in self._js_records_with_mac(t):
                mac = next(x for x in f if P.is_mac(x))
                ifc = next((x for x in f if re.match(r"^wl\d+(\.\d+)?$", x)), "")
                out.append({"mac": P.norm_mac(mac), "ifc": ifc, "ssid": ""})
        return out

    def arp(self):
        try:
            t = self.fetch("arp")
        except PageMissing:
            return []
        out = []
        for row in P.table_rows(t):
            cells = row["cells"]
            macs = [c for c in cells if P.is_mac(c)]
            ips = [c for c in cells if P.is_ip(c)]
            if macs and ips and P.norm_mac(macs[0]) != "00:00:00:00:00:00":
                ifc = next((c for c in cells if P.IFACE_RE.match(c)), "")
                out.append({"mac": P.norm_mac(macs[0]), "ip": ips[0], "ifc": ifc})
        return out

    def counters(self):
        """{interface name: (received bytes, sent bytes)} as the router counts them."""
        out = {}
        for feature in ("wanstats", "lanstats"):
            try:
                t = self.fetch(feature)
            except PageMissing:
                continue
            for row in P.table_rows(t):
                cells = row["cells"]
                if len(cells) < 9 or not cells[0]:
                    continue
                ints = [int(c) for c in cells[1:] if re.fullmatch(r"\d+", c)]
                if len(ints) >= 8:
                    out.setdefault(cells[0], (ints[-8], ints[-4]))
        return out

    # ------------------------------------------------------------------ what this router offers

    def capabilities(self):
        block = self.has("blockmac")
        kind = self._block_kind
        if block and kind is None:
            try:
                kind = self.blocks()[0]
            except RouterError:
                pass
        return {"devices": self.has("dhcp") or self.has("arp"),
                "wifi": self.has("stations"),
                "traffic": self.has("wanstats") or self.has("lanstats"),
                "speed_limit": self.has("qosform"),
                "pause": block,
                "bedtime": block and (kind == "todmngr" or self.has("sched")),
                "sites": self.has("urlflt")}

    def checks(self):
        labels = [("qos", "Speed rules list"), ("qosform", "Speed limit form"), ("blockmac", "Block / pause device"),
                  ("sched", "Schedules (bedtime)"), ("urlflt", "Website blocking"), ("stations", "Wi-Fi device list"),
                  ("arp", "Address table")]
        return [(label, self.has(f)) for f, label in labels]

    def warnings(self, has_limits):
        w = []
        if self._qos_on is False and has_limits:
            w.append("QoS is switched off on the router, so speed limits do nothing. Open a device and press Save "
                     "to switch it on.")
        if "qosform" not in self.page_overrides and "qosform" in self.found and not self.found["qosform"]:
            w.append("This router's speed-limit page was not found. Please send the report from the Check page.")
        return w

    # ------------------------------------------------------------------ speed limits (program level)

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

    def apply_limit(self, mac, ip, ifc, up_kbps, down_kbps):
        """Upload = rule on LAN ingress by source MAC into the internet queue. Download = rule on
        WAN ingress by destination IP into the device's Wi-Fi queue (or the internet queue)."""
        h = hexmac(mac)
        up_name, dn_name = UP_PREFIX + h, DOWN_PREFIX + h
        with self.lock:
            q = self.qos()
            ours = [x["name"] for x in q["rules"] if x["name"] in (up_name, dn_name)]
            if ours:
                self.remove_qos_rules(ours)
            if self.qos_enabled() is False:
                self.enable_qos()
            wan_q = self._wan_queue(q["queues"])
            if not wan_q:
                raise ActionError("The router has no internet (WAN) queue. Create one under Network Tools → QoS Queue.")
            errors = []
            if up_kbps:
                try:
                    self.add_qos_rule(up_name, self._iface(q["ifaces"], "LAN"), wan_q, up_kbps, src_mac=mac)
                except RouterError as e:
                    errors.append("upload: %s" % e)
            if down_kbps:
                if not ip:
                    errors.append("download: the device has no address right now; it is set when it comes online")
                else:
                    dq = self._down_queue(q["queues"], ifc) or wan_q
                    try:
                        self.add_qos_rule(dn_name, self._iface(q["ifaces"], "WAN"), dq, down_kbps, dst_ip=ip,
                                          ethertype="800")
                    except RouterError as e:
                        errors.append("download: %s" % e)
            return errors

    def clear_limit(self, mac):
        h = hexmac(mac)
        with self.lock:
            names = [x["name"] for x in self.qos()["rules"] if x["name"] in (UP_PREFIX + h, DOWN_PREFIX + h)]
            self.remove_qos_rules(names)

    def limit_overview(self):
        try:
            rules = self.qos()["rules"]
        except PageMissing:
            return {}, []
        state, other = {}, []
        for r in rules:
            n = r["name"]
            if re.fullmatch(r"r[ud][0-9a-f]{12}", n):
                mac = ":".join(n[i:i + 2] for i in range(2, 14, 2))
                state.setdefault(mac, {"up": False, "down": False})["up" if n[1] == "u" else "down"] = True
            elif not OURS_RE.match(n):
                other.append({"name": n, "src_mac": r.get("src_mac", ""), "dst_ip": r.get("dst_ip", ""),
                              "rate": r.get("rate", ""), "enabled": r.get("enabled")})
        return state, other

    # ------------------------------------------------------------------ blocking (program level)

    def apply_block(self, mac, windows=None):
        h = hexmac(mac)
        with self.lock:
            self.clear_block(mac)
            kind, _ = self.blocks()
            entries = []
            if windows is None:
                name = BLOCK_PREFIX + h
                if kind == "blockmac":
                    self.add_block(name, mac, "Always")
                else:
                    self.add_block(name, mac, days=127, start=0, end=1439)
                entries.append(name)
            else:
                for i, (ws, we, wd) in enumerate(windows):
                    name = BLOCK_PREFIX + h + ("" if i == 0 else "b")
                    if kind == "blockmac":
                        sched = "%s%02d%02d%02d%02dd%d" % (SCHED_PREFIX, ws // 60, ws % 60, we // 60, we % 60, wd)
                        if sched not in {x["name"] for x in self.schedules()}:
                            self.add_schedule(sched, wd, ws, we)
                        self.add_block(name, mac, sched)
                    else:
                        self.add_block(name, mac, days=wd, start=ws, end=we)
                    entries.append(name)
            return entries

    def clear_block(self, mac):
        h = hexmac(mac)
        with self.lock:
            _, entries = self.blocks()
            mine = [e["name"] for e in entries if e["name"] in (BLOCK_PREFIX + h, BLOCK_PREFIX + h + "b")]
            self.remove_blocks(mine)
            if self.has("sched"):
                _, left = self.blocks()
                used = {e.get("schedule") for e in left}
                unused = [s["name"] for s in self.schedules() if s["name"].startswith(SCHED_PREFIX)
                          and s["name"] not in used and re.fullmatch(r"rc\d{8}d\d+", s["name"])]
                if unused:
                    self.remove_schedules(unused)

    def block_names(self):
        try:
            return [e["name"] for e in self.blocks()[1]]
        except PageMissing:
            return []

    # ------------------------------------------------------------------ QoS

    @staticmethod
    def _rule(f):
        g = lambda i: f[i].strip() if len(f) > i else ""
        return {"name": g(0), "order": g(1), "intf": g(2), "ethertype": g(3), "src_mac": P.norm_mac(g(4).split("/")[0]) if P.is_mac(g(4).split("/")[0]) else g(4),
                "dst_ip": g(7).split("/")[0], "queue_key": g(13), "rate": f[-2].strip() if len(f) >= 3 else "",
                "enabled": f[-1].strip() == "1" if f else False}

    def qos(self, text=None):
        t = text if text is not None else self.fetch("qos")
        v = P.js_vars(t)
        rules = []
        if "qosgs" in v:
            rules = [self._rule(f) for f in P.split_records(v["qosgs"])]
        else:
            for row in P.table_rows(t):
                names = [val for (n, val) in row["inputs"] if n in ("rml", "rmLst") and val]
                if names:
                    cells = row["cells"]
                    nums = [c for c in cells if re.fullmatch(r"\d+", c)]
                    macs = [P.norm_mac(c.split("/")[0]) for c in cells if P.is_mac(c.split("/")[0])]
                    ips = [c.split("/")[0] for c in cells if P.is_ip(c.split("/")[0])]
                    rules.append({"name": names[0], "intf": "", "src_mac": macs[0] if macs else "",
                                  "dst_ip": ips[-1] if ips else "", "queue_key": "",
                                  "rate": nums[-1] if nums else "", "enabled": None})
        queues = []
        if v.get("queueInfo"):
            for rec in v["queueInfo"].split("|"):
                parts = rec.split(":")
                if len(parts) >= 2 and parts[0].strip():
                    queues.append({"key": parts[0].strip(), "text": parts[1].strip()})
        if not queues:
            queues = [{"key": val, "text": lab} for val, lab in P.select_options(t, "queuekey") if val not in ("", "-1")]
        for q in queues:
            q["ifc"] = q["text"].split("&")[0]
            m = re.search(r"Pre(\d+)", q["text"])
            q["prec"] = int(m.group(1)) if m else 0
        ifaces = [x for x in v.get("qosIntf", "").split("|") if x]
        if not ifaces:
            ifaces = [lab or val for val, lab in P.select_options(t, "classIfc") if val]
        order_last = None
        opts = P.select_options(t, "rulsorder")
        for val, lab in opts:
            if "last" in lab.lower():
                order_last = val
        if order_last is None and "qosclsRulsOrder" in v:
            lst = sorted(v["qosclsRulsOrder"].split("|"))
            if "Last" in lst:
                order_last = str(lst.index("Last") + 1)
        if order_last is None:
            order_last = str(len(rules) + 1)
        return {"rules": rules, "queues": queues, "ifaces": ifaces, "order_last": order_last,
                "tc_enabled": v.get("enableTc", "1") != "0"}

    def qos_form(self):
        """(page text, FormSpec or None, sessionKey) for adding a QoS rule."""
        t = self.fetch("qosform")
        return t, P.form_spec(t, "qoscls.cmd?action=add"), P.session_key(t)

    def add_qos_rule(self, name, class_if, queue, rate_kbps, src_mac=None, dst_ip=None, ethertype=None):
        """queue = {'key':..., 'ifc':...} from qos()['queues']."""
        with self.lock:
            t, spec, key = self.qos_form()
            info = self.qos(t) if ("qosgs" in t or "queueInfo" in t) else self.qos()
            base = spec.base if spec else "qoscls.cmd?action=add"
            rate_name = self._param(spec, "qos.rate", "rate", "rateLimit", "ratelimit", "clsRate", "rateCtrl", "rateControl")
            pairs = [
                (self._param(spec, "qos.name", "clsName", "className"), name),
                (self._param(spec, "qos.order", "rulsOrder", "ruleOrder"), info["order_last"]),
                (self._param(spec, "qos.enable", "rulsEnbl", "clsEnable", "rulsEnable"), 1),
                (self._param(spec, "qos.classif", "classIf", "lanIfcName", "classIfc"), class_if),
            ]
            if ethertype:
                pairs.append((self._param(spec, "qos.ethertype", "ethertype", "etherType"), ethertype))
            if src_mac:
                pairs.append((self._param(spec, "qos.srcmac", "srcMacAddr", "srcMac"), src_mac))
            if dst_ip:
                pairs.append((self._param(spec, "qos.dstip", "dstAddr", "dstIp", "dstIP"), dst_ip))
            pairs += [
                (self._param(spec, "qos.vlan8021p", "vlan8021p"), -1),
                (self._param(spec, "qos.queue", "queuekey", "queueKey"), queue["key"]),
                (self._param(spec, "qos.egress", "egress"), queue.get("ifc", "")),
                (self._param(spec, "qos.dscpmark", "dscpMark"), -1),
                (self._param(spec, "qos.wanvlan8021p", "wanVlan8021p"), -1),
                (rate_name, int(rate_kbps)),
            ]
            resp = self._send(base, pairs, key)
            rules = {r["name"]: r for r in self.qos()["rules"]}
            if name not in rules:
                raise ActionError(P.error_text(resp) or "The router did not save the speed rule %s." % name)
            saved = re.sub(r"\D", "", rules[name].get("rate") or "")
            if saved and saved != str(int(rate_kbps)) or (not saved and "qosgs" in t):
                self.remove_qos_rules([name])
                raise ActionError("The router saved the rule without the speed limit (field '%s' not accepted). "
                                  "Please send the report from the Check page." % rate_name)
            self.log("QoS rule %s added (%s kbit/s, queue %s)" % (name, rate_kbps, queue.get("text", queue["key"])))
            return rules[name]

    def remove_qos_rules(self, names):
        names = [n for n in names if n]
        if not names:
            return
        with self.lock:
            t = self.fetch("qos")
            key = P.session_key(t)
            spec = P.form_spec(t, "qoscls.cmd?action=remove")
            lst = self._param(spec, "qos.rmlist", "rmLst") or "rmLst"
            self._send("qoscls.cmd?action=remove", [(lst, ",".join(names) + ",")], key)
            left = {r["name"] for r in self.qos()["rules"]} & set(names)
            if left:
                raise ActionError("The router kept the rule(s): %s" % ", ".join(sorted(left)))
            self.log("QoS rule(s) removed: %s" % ", ".join(names))

    def qos_enabled(self):
        try:
            t = self.fetch("qosmgmt")
        except PageMissing:
            return None
        v = P.js_vars(t)
        if "enblQos" in v:
            self._qos_on = v["enblQos"] == "1"
            return self._qos_on
        m = re.search(r"""(?i)<input[^>]*name=['"]?enableQos['"]?[^>]*>""", t)
        self._qos_on = bool(m and "checked" in m.group(0).lower()) if m else None
        return self._qos_on

    def enable_qos(self):
        with self.lock:
            t = self.fetch("qosmgmt")
            v = P.js_vars(t)
            spec = P.form_spec(t, "qosmgmt.cmd?action=savapply")
            mark = v.get("defMark") or "-1"
            self._send("qosmgmt.cmd?action=savapply", [
                (self._param(spec, "qosmgmt.enable", "enblQos"), 1),
                (self._param(spec, "qosmgmt.queue", "defaultQueue"), -1),
                (self._param(spec, "qosmgmt.mark", "defaultDscpMark"), mark),
            ], P.session_key(t))
            if self.qos_enabled() is False:
                raise ActionError("The router did not turn QoS on.")
            self.log("QoS switched on")

    # ------------------------------------------------------------------ blocking

    def blocks(self):
        """(kind, [{'name','mac','schedule'|days/start/end}]); kind is 'blockmac' or 'todmngr'."""
        t = self.fetch("blockmac")
        kind = "blockmac" if "blockmac.tod" in t else "todmngr"
        self._block_kind = kind
        v = P.js_vars(t)
        entries = []
        if "macs" in v:
            for f in P.split_records(v.get("macs", "")):
                if len(f) >= 2 and P.is_mac(f[1]):
                    entries.append({"name": f[0], "mac": P.norm_mac(f[1]), "schedule": f[2] if len(f) > 2 else ""})
        else:
            for row in P.table_rows(t):
                macs = [c for c in row["cells"] if P.is_mac(c)]
                if not macs:
                    continue
                names = [val for (n, val) in row["inputs"] if n in ("rml", "rmLst") and val]
                entries.append({"name": names[0] if names else row["cells"][0], "mac": P.norm_mac(macs[0]),
                                "schedule": ""})
        return kind, entries

    def add_block(self, name, mac, schedule="Always", days=127, start=0, end=1439):
        with self.lock:
            t = self.fetch("blockmac")
            key = P.session_key(t)
            if "blockmac.tod" in t:
                spec = P.form_spec(t, "blockmac.tod?action=add")
                pairs = [(self._param(spec, "block.name", "username"), name),
                         (self._param(spec, "block.mac", "mac"), mac),
                         (self._param(spec, "block.schedule", "schedule"), schedule)]
                resp = self._send("blockmac.tod?action=add", pairs, key)
            else:
                spec = P.form_spec(t, "todmngr.tod")
                pairs = [(self._param(spec, "block.name", "username"), name),
                         (self._param(spec, "block.mac", "mac"), mac),
                         (self._param(spec, "block.days", "days"), days),
                         (self._param(spec, "block.start", "start_time"), start),
                         (self._param(spec, "block.end", "end_time"), end)]
                resp = self._send("todmngr.tod?action=add", pairs, key)
            if name not in {e["name"] for e in self.blocks()[1]}:
                raise ActionError(P.error_text(resp) or "The router did not save the block %s." % name)
            self.log("block %s added for %s (%s)" % (name, mac, schedule if "blockmac.tod" in t else "%s-%s" % (start, end)))

    def remove_blocks(self, names):
        names = [n for n in names if n]
        if not names:
            return
        with self.lock:
            t = self.fetch("blockmac")
            kind = "blockmac" if "blockmac.tod" in t else "todmngr"
            self._send("%s.tod?action=remove" % kind, [("rmLst", ",".join(names) + ",")], P.session_key(t))
            left = {e["name"] for e in self.blocks()[1]} & set(names)
            if left:
                raise ActionError("The router kept the block(s): %s" % ", ".join(sorted(left)))
            self.log("block(s) removed: %s" % ", ".join(names))

    def schedules(self):
        t = self.fetch("sched")
        v = P.js_vars(t)
        out = []
        for f in P.split_records(v.get("tods", "")):
            try:
                out.append({"name": f[0], "days": int(f[2]), "start": int(f[3]), "end": int(f[4])})
            except (IndexError, ValueError):
                out.append({"name": f[0]})
        return out

    def add_schedule(self, name, days, start, end):
        with self.lock:
            t = self.fetch("sched")
            spec = P.form_spec(t, "sciptsched.sched?action=add")
            pairs = [(self._param(spec, "sched.name", "schedule"), name),
                     (self._param(spec, "sched.days", "days"), days),
                     (self._param(spec, "sched.start", "start_time"), start),
                     (self._param(spec, "sched.end", "end_time"), end)]
            resp = self._send("sciptsched.sched?action=add", pairs, P.session_key(t))
            if name not in {s["name"] for s in self.schedules()}:
                raise ActionError(P.error_text(resp) or "The router did not save the schedule %s." % name)
            self.log("schedule %s added" % name)

    def remove_schedules(self, names):
        names = [n for n in names if n]
        if not names:
            return
        with self.lock:
            t = self.fetch("sched")
            self._send("sciptsched.sched?action=remove", [("rmLst", ",".join(names) + ",")], P.session_key(t))
            self.log("schedule(s) removed: %s" % ", ".join(names))

    def clock_set(self):
        """False when the router says its clock is not set (schedules then do nothing)."""
        for feature in ("blockmac", "sched"):
            try:
                t = self.fetch(feature)
            except RouterError:
                continue
            m = re.search(r"""if\s*\(\s*['"](\d?)['"]\s*!=\s*['"]1['"]\s*\)[\s\S]{0,300}?time is not set""", t, re.I)
            if m:
                return m.group(1) == "1"
        return None

    # ------------------------------------------------------------------ website blocking

    def sites(self):
        t = self.fetch("urlflt")
        v = P.js_vars(t)
        return [{"url": f[0], "schedule": f[1] if len(f) > 1 else ""} for f in P.split_records(v.get("urls", ""))]

    def add_site(self, url, schedule="Always"):
        with self.lock:
            t = self.fetch("urlflt")
            spec = P.form_spec(t, "urlfilter.cmd?action=add")
            pairs = [(self._param(spec, "url.url", "TodUrl", "TodUrlAdd", "url"), url)]
            sch = self._param(spec, "url.schedule", "schedule")
            if sch and (spec is None or sch in spec.params):
                pairs.append((sch, schedule))
            resp = self._send("urlfilter.cmd?action=add", pairs, P.session_key(t))
            if url not in {s["url"] for s in self.sites()}:
                raise ActionError(P.error_text(resp) or "The router did not save the website %s." % url)
            self.log("website blocked: %s" % url)

    def remove_sites(self, urls):
        with self.lock:
            t = self.fetch("urlflt")
            self._send("urlfilter.cmd?action=remove", [("rmLst", ",".join(urls) + ",")], P.session_key(t))
            self.log("website(s) unblocked: %s" % ", ".join(urls))

    # ------------------------------------------------------------------ diagnostics

    def probe(self):
        """What this router offers, for the Check page and the report. Reads only."""
        out = {"base": self.base, "auth": self.auth_mode, "features": {}}
        for feature in PAGES:
            entry = {}
            try:
                path = self.page(feature)
                entry["page"] = path
                if path:
                    t = self.get(path)
                    entry["size"] = len(t)
                    entry["js_vars"] = sorted(P.js_vars(t).keys())[:60]
                    entry["session_key"] = bool(P.session_key(t))
                    for marker in ("qoscls.cmd?action=add", "qoscls.cmd?action=remove", "qosmgmt.cmd?action=savapply",
                                   "blockmac.tod?action=add", "todmngr.tod", "sciptsched.sched?action=add",
                                   "urlfilter.cmd?action=add"):
                        spec = P.form_spec(t, marker)
                        if spec:
                            entry.setdefault("forms", {})[marker] = {"base": spec.base, "params": spec.params}
            except RouterError as e:
                entry["error"] = str(e)
            out["features"][feature] = entry
        return out

    def report_lines(self):
        out = []
        add = out.append
        add("QoS on: %s   Block type: %s" % (self._qos_on, self._block_kind))
        add("")
        add("== QoS ==")
        try:
            q = self.qos()
            add("Interfaces: %s" % " | ".join(q["ifaces"]))
            add("Queues: %s" % " | ".join("%s=%s" % (x["key"], x["text"]) for x in q["queues"]))
            add("Rule order 'Last' value: %s   rate field shown: %s" % (q["order_last"], q["tc_enabled"]))
            for x in q["rules"]:
                add("  rule %s intf=%s src=%s dst=%s queue=%s rate=%s on=%s" % (
                    x["name"], x.get("intf"), x.get("src_mac"), x.get("dst_ip"), x.get("queue_key"), x.get("rate"),
                    x.get("enabled")))
            t, spec, key = self.qos_form()
            if spec:
                add("Add-rule script (from the router page):")
                add(spec.source[:3000])
        except RouterError as e:
            add("QoS read failed: %s" % e)
        for feature, marker in (("blockmac", ".tod?action=add"), ("sched", "sciptsched.sched?action=add"),
                                ("urlflt", "urlfilter.cmd?action=add")):
            try:
                t = self.fetch(feature)
                spec = P.form_spec(t, "blockmac.tod?action=add" if feature == "blockmac" and "blockmac.tod" in t else
                                   ("todmngr.tod" if feature == "blockmac" else marker))
                if spec:
                    add("")
                    add("%s script:" % feature)
                    add(spec.source[:1500])
            except RouterError:
                pass
        return out
