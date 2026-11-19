"""Bezeq VTech NB403-IL (VDSL, Lantiq platform). EARLY SUPPORT.

Nobody working on this program has seen this router's pages yet, so this
driver only does safe, read-only things:

* recognises the router from its front page;
* logs in with the usual methods (HTTP Basic, or the page's own login form);
* looks through the router's menu pages, reading only (it follows plain links,
  never form targets, skips anything that sounds like save / apply / reboot /
  reset / logout, and never sends a form);
* shows the devices it finds in any table that lists hardware (MAC) and IP
  addresses.

The report on the Check page lists every page it found with its forms and
fields. That is what the full driver (speed limits, blocking) will be built from.
"""

import re
import urllib.parse

from .. import parsing as P
from .base import Driver, LoginError, PageMissing, RouterError

# never GET anything whose address sounds like it changes something
UNSAFE_RE = re.compile(r"(?i)(log_?out|log_?off|sign_?out|exit|reboot|restart|reset|restore|default|factory|upgrade|"
                       r"firmware|flash|backup|save|apply|commit|submit|delete|remove|\bdel\b|wps|diag|ping|trace|"
                       r"action=|cmd=|set_|\bset\b|write|clear|goform|formset|\.cmd\b)")
STATIC_RE = re.compile(r"(?i)\.(css|png|gif|jpe?g|ico|svg|woff2?|ttf|eot|bmp|mp4)(\?|$)")
LINK_RE = re.compile(r"""(?i)(?:href|src)\s*=\s*['"]([^'"#<>\s]+)['"]"""
                     r"""|['"]([\w./-]+\.(?:html?|asp|cgi|lp|lua|php|stm|cmd|js|xml|json))(?:\?[^'"]*)?['"]""")
PASSWORD_INPUT_RE = re.compile(r"""(?i)<input[^>]*type\s*=\s*['"]?password""")
MAX_PAGES = 60


def _attr(tag, name):
    m = re.search(r"""(?i)\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % name, tag)
    return next((g for g in m.groups() if g is not None), "") if m else ""


def login_form(text):
    """{'action', 'method', 'inputs': [(name, type, value)]} of the first form with a password box."""
    forms = list(re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", text or ""))
    for m in forms:
        if PASSWORD_INPUT_RE.search(m.group(2)):
            return {"action": _attr(m.group(1), "action"), "method": (_attr(m.group(1), "method") or "post").lower(),
                    "inputs": [(_attr(i, "name"), (_attr(i, "type") or "text").lower(), _attr(i, "value"))
                               for i in re.findall(r"(?is)<input\b[^>]*>", m.group(2))]}
    if PASSWORD_INPUT_RE.search(text or ""):          # password box outside any form (sent by script)
        return {"action": "", "method": "post", "script_only": True,
                "inputs": [(_attr(i, "name") or _attr(i, "id"), (_attr(i, "type") or "text").lower(), _attr(i, "value"))
                           for i in re.findall(r"(?is)<input\b[^>]*>", text)]}
    return None


def is_login_page(text):
    return bool(PASSWORD_INPUT_RE.search(text or ""))


def mac_rows(text):
    """[{'mac', 'ip', 'name'}] from tables and script variables that list hardware addresses."""
    out = []
    for row in P.table_rows(text):
        cells = row["cells"]
        macs = [c for c in cells if P.is_mac(c)]
        if not macs:
            continue
        ips = [c for c in cells if P.is_ip(c)]
        name = next((c for c in cells if c and not P.is_mac(c) and not P.is_ip(c)
                     and not re.fullmatch(r"[\d\s:.,/-]+|yes|no|static|dynamic|dhcp|\*", c, re.I)), "")
        out.append({"mac": P.norm_mac(macs[0]), "ip": ips[0] if ips else "", "name": name})
    if not out:
        for value in P.js_vars(text).values():
            for rec in value.split("|"):
                fields = [f.strip() for f in re.split(r"[,;/]", rec)]
                mac = next((f for f in fields if P.is_mac(f)), None)
                if mac:
                    ip = next((f for f in fields if P.is_ip(f)), "")
                    name = next((f for f in fields if f and not P.is_mac(f) and not P.is_ip(f)), "")
                    out.append({"mac": P.norm_mac(mac), "ip": ip, "name": name})
    return out


class VTechNB403(Driver):
    MODEL = "vtech-nb403"
    NAME = "Bezeq VTech NB403-IL (early support)"
    EXAMPLES = "Bezeq VTech NB403-IL VDSL router"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pages = None           # path -> summary, filled by discover()
        self.device_pages = []
        self.front = None           # (status, text) of the front page before logging in

    @classmethod
    def identify(cls, status, text, headers):
        t = (text or "").lower() + " " + headers.get("server", "").lower() + " " + headers.get("www-authenticate", "").lower()
        score = 0
        if "nb403" in t:
            score += 5
        if "vtech" in t:
            score += 3
        if "bezeq" in t or "בזק" in t:
            score += 2
        if "lantiq" in t:
            score += 1
        return score

    # ------------------------------------------------------------------ login

    def _url_path(self, link, current="/"):
        u = urllib.parse.urlsplit(urllib.parse.urljoin(self.base + "/" + current.lstrip("/"), link))
        if u.scheme not in ("http", "https") or u.netloc != urllib.parse.urlsplit(self.base).netloc:
            return None
        return (u.path or "/") + ("?" + u.query if u.query else "")

    def login(self):
        with self.lock:
            self.auth_mode = None
            s, t = self._fetch("/")
            self.front = (s, t)
            if s == 401:
                self._set_basic()
                s, t = self._fetch("/")
                if s == 401:
                    self._basic = None
                    raise LoginError("The router did not accept the user name or password.")
                self.auth_mode = "basic"
                return
            form = login_form(t)
            if form is None:
                self.auth_mode = "none"
                return
            if form.get("script_only"):
                raise LoginError("This router logs in with a script the program does not know yet. "
                                 "Please send the report from the Check page.")
            fields = [(n, ty, v) for n, ty, v in form["inputs"] if n]
            pw = next((n for n, ty, _ in fields if ty == "password"), None)
            user = next((n for n, ty, _ in fields if ty in ("text", "email", "")
                         and re.search(r"user|login|name|account", n, re.I)), None) \
                or next((n for n, ty, _ in fields if ty in ("text", "email")), None)
            data = {n: v for n, ty, v in fields if ty == "hidden"}
            if user:
                data[user] = self.username
            data[pw] = self.password
            action = self._url_path(form["action"] or "/") or "/"
            if form["method"] == "get":
                self._fetch(action + ("&" if "?" in action else "?") + urllib.parse.urlencode(data))
            else:
                self._fetch(action, data)
            s2, t2 = self._fetch("/")
            if s2 == 200 and not is_login_page(t2):
                self.auth_mode = "form"
                self.log("logged in to the router (form)")
                return
            raise LoginError("The router did not accept the password, or it logs in in a way this program does not "
                             "know yet. Check the password; if it is right, send the report from the Check page.")

    def get(self, path):
        with self.lock:
            if self.auth_mode is None:
                self.login()
            s, t = self._fetch(path)
            if s in (401, 403) or (s == 200 and is_login_page(t) and path not in ("/", "")):
                self.login()
                s, t = self._fetch(path)
            if s == 404:
                raise PageMissing(path)
            if s >= 400:
                raise RouterError("The router answered HTTP %s for %s" % (s, path.split("?")[0]))
            return t

    # ------------------------------------------------------------------ looking around (read only)

    def discover(self, force=False):
        if self.pages is not None and not force:
            return self.pages
        with self.lock:
            pages, queue = {}, ["/"]
            while queue and len(pages) < MAX_PAGES:
                path = queue.pop(0)
                if path in pages:
                    continue
                try:
                    t = self.get(path)
                except RouterError as e:
                    pages[path] = {"error": str(e)}
                    continue
                pages[path] = self._summary(path, t)
                for m in LINK_RE.finditer(t):
                    link = m.group(1) or m.group(2)
                    p = self._url_path(link, path)
                    if (p and p not in pages and p not in queue and not STATIC_RE.search(p)
                            and not UNSAFE_RE.search(p) and not p.lower().startswith("/javascript:")):
                        queue.append(p)
            self.pages = pages
            self.device_pages = [p for p, s in pages.items() if s.get("mac_rows", 0) and s.get("with_ip", 0)
                                 and not p.lower().endswith(".js")]
            self.log("looked through %d router pages; device lists on: %s" % (len(pages), ", ".join(self.device_pages) or "none"))
            return pages

    @staticmethod
    def _summary(path, t):
        title = re.search(r"(?is)<title>(.*?)</title>", t)
        rows = mac_rows(t)
        forms = []
        for m in re.finditer(r"(?is)<form\b([^>]*)>(.*?)</form>", t):
            names = [_attr(i, "name") for i in re.findall(r"(?is)<(?:input|select|textarea)\b[^>]*>", m.group(2))]
            forms.append({"action": _attr(m.group(1), "action"), "method": _attr(m.group(1), "method"),
                          "fields": [n for n in names if n][:40]})
        heads = [P.clean(P.strip_tags(h)) for h in re.findall(r"(?is)<(?:h\d|th)\b[^>]*>(.*?)</(?:h\d|th)>", t)][:12]
        return {"title": P.clean(P.strip_tags(title.group(1))) if title else "", "size": len(t),
                "forms": forms, "js_vars": sorted(P.js_vars(t))[:40], "headings": [h for h in heads if h],
                "mac_rows": len(rows), "with_ip": sum(1 for r in rows if r["ip"])}

    # ------------------------------------------------------------------ what it offers

    def capabilities(self):
        caps = super().capabilities()
        try:
            self.discover()
            caps["devices"] = bool(self.device_pages)
        except RouterError:
            pass
        return caps

    def checks(self):
        found = bool(self.device_pages)
        return [("Router recognised and logged in", self.auth_mode is not None),
                ("Device list found", found),
                ("Speed limits, pause, bedtime (waiting for the report)", False)]

    def warnings(self, has_limits):
        return ["This router has early support: the program shows devices and ping, but cannot limit or block yet. "
                "Please send the report from the Check page so full support can be added."]

    def info(self):
        out = {"model": "VTech NB403-IL"}
        try:
            pages = self.discover()
        except RouterError:
            return out
        wanted = ["/"] + [p for p, s in pages.items() if p != "/" and not s.get("error")
                          and re.search(r"status|info|device|system|about|overview", p + " " + s.get("title", ""), re.I)]
        for path in wanted[:4]:
            try:
                t = self.get(path)
            except RouterError:
                continue
            for label, value in P.label_values(t).items():
                if re.search(r"firmware|software version", label, re.I) and value:
                    out.setdefault("firmware", value)
                if re.search(r"up ?time", label, re.I) and value:
                    out.setdefault("uptime", value)
            if "firmware" in out:
                break
        return out

    def leases(self):
        self.discover()
        if not self.device_pages:
            raise PageMissing("device list")
        seen, out = set(), []
        for path in self.device_pages:
            try:
                rows = mac_rows(self.get(path))
            except RouterError:
                continue
            for r in rows:
                if r["mac"] not in seen and r["mac"] != "00:00:00:00:00:00":
                    seen.add(r["mac"])
                    out.append({"mac": r["mac"], "ip": r["ip"], "hostname": r["name"], "expires": ""})
        return out

    # ------------------------------------------------------------------ report

    def probe(self):
        out = {"base": self.base, "auth": self.auth_mode, "features": {}}
        if self.front is None:
            try:
                self.front = self._fetch("/")
            except RouterError as e:
                out["front_error"] = str(e)
        if self.front:
            s, t = self.front
            out["front"] = {"status": s, "size": len(t), "login_form": login_form(t),
                            "scripts": re.findall(r"""(?i)<script[^>]*src\s*=\s*['"]([^'"]+)""", t)[:20],
                            "inline_login_script": "\n".join(x for x in re.findall(r"(?is)<script\b[^>]*>(.*?)</script>", t)
                                                             if re.search(r"pass|login|md5|sha|encrypt|base64", x, re.I))[:3000]}
        return out

    def report_lines(self):
        out = ["", "== VTech NB403-IL (early support) =="]
        pr = self.probe()
        f = pr.get("front") or {}
        out.append("Front page: HTTP %s, %s bytes, login %s" % (f.get("status"), f.get("size"), self.auth_mode))
        if f.get("login_form"):
            lf = f["login_form"]
            out.append("Login form: action=%s method=%s fields=%s" % (
                lf.get("action"), lf.get("method"), ", ".join("%s(%s)" % (n, t) for n, t, _ in lf["inputs"])))
        if f.get("scripts"):
            out.append("Scripts: %s" % ", ".join(f["scripts"]))
        if f.get("inline_login_script"):
            out.append("Login script:")
            out.append(f["inline_login_script"])
        if self.auth_mode is None:
            out.append("Not logged in, so no other pages were read.")
            return out
        try:
            pages = self.discover(force=True)
        except RouterError as e:
            out.append("Looking through pages failed: %s" % e)
            return out
        out.append("")
        out.append("== Router pages (%d, read only) ==" % len(pages))
        for path, s in pages.items():
            if s.get("error"):
                out.append("- %s  ERROR %s" % (path, s["error"]))
                continue
            out.append("- %s  \"%s\" %d bytes%s" % (path, s["title"], s["size"],
                                                     "  devices:%d" % s["mac_rows"] if s["mac_rows"] else ""))
            if s["headings"]:
                out.append("    headings: %s" % " | ".join(s["headings"]))
            if s["js_vars"]:
                out.append("    vars: %s" % ", ".join(s["js_vars"]))
            for fm in s["forms"]:
                out.append("    form %s %s -> %s" % (fm["method"] or "get", fm["action"], ", ".join(fm["fields"])))
        return out
