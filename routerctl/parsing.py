"""Helpers that read the router's web pages.

D-Link DSL routers built on the Broadcom platform render most data either as
plain HTML tables or as JavaScript string variables such as
``var qosgs = "name,1,LAN,...|";``. Forms are not real HTML forms: a JavaScript
function builds a URL like ``qoscls.cmd?action=add&clsName=...`` and loads it.
The helpers here read all three shapes without needing a browser.
"""

import html
import re
from html.parser import HTMLParser

MAC_RE = re.compile(r"\b([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})\b")
IP_RE = re.compile(r"\b((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})\b")
IFACE_RE = re.compile(r"^(eth\d+(\.\d+)?|wl\d+(\.\d+)?|ppp\d+(\.\d+)?|ptm\d+(\.\d+)?|atm\d+(\.\d+)?|"
                      r"nas_?\S*|br\d+|veip\d+(\.\d+)?|epon\S*|usb\d+|bcmsw\S*)$", re.I)


def norm_mac(mac):
    """'AA-BB-CC-DD-EE-FF' -> 'aa:bb:cc:dd:ee:ff'."""
    return mac.strip().replace("-", ":").lower()


def is_mac(text):
    return bool(MAC_RE.fullmatch(text.strip()))


def is_ip(text):
    return bool(IP_RE.fullmatch(text.strip()))


def clean(text):
    """Collapse whitespace and decode entities."""
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def strip_tags(text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text or "")
    return clean(re.sub(r"(?s)<[^>]+>", " ", text))


class _Tables(HTMLParser):
    """Collect every table row as {'cells': [...], 'inputs': [(name, value)]}."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._stack = []
        self._in_cell = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "tr":
            self._stack.append({"cells": [], "inputs": []})
        elif tag in ("td", "th") and self._stack:
            self._stack[-1]["cells"].append("")
            self._in_cell = True
        elif tag == "input" and self._stack:
            a = dict(attrs)
            self._stack[-1]["inputs"].append((a.get("name") or "", a.get("value") or ""))
        elif tag == "br" and self._stack and self._in_cell and self._stack[-1]["cells"]:
            self._stack[-1]["cells"][-1] += " "

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag == "tr" and self._stack:
            row = self._stack.pop()
            row["cells"] = [clean(c) for c in row["cells"]]
            if row["cells"] or row["inputs"]:
                self.rows.append(row)
        elif tag in ("td", "th"):
            self._in_cell = False

    def handle_data(self, data):
        if self._skip or not self._stack:
            return
        row = self._stack[-1]
        if row["cells"]:
            row["cells"][-1] += data


def table_rows(text):
    p = _Tables()
    try:
        p.feed(text or "")
        p.close()
    except Exception:  # badly broken HTML: keep what was parsed
        pass
    return p.rows


def js_vars(text):
    """All `var name = '...'` / `name = "..."` string assignments (first one wins)."""
    out = {}
    for m in re.finditer(r"""(?:\bvar\s+)?\b([A-Za-z_]\w*)\s*=\s*(['"])(.*?)\2\s*[;\n]""", text or ""):
        out.setdefault(m.group(1), m.group(3))
    return out


def session_key(text):
    """The one-time key Broadcom pages embed for the next change request."""
    for pat in (r"sessionKey\s*=\s*['\"]?(\d+)", r"sessionKey=(\d+)",
                r"name=['\"]?sessionKey['\"]?[^>]*value=['\"]?(\d+)"):
        m = re.search(pat, text or "")
        if m:
            return m.group(1)
    return None


class FormSpec:
    """What a page's JavaScript sends for one action, e.g. qoscls.cmd?action=add."""

    def __init__(self, base, params, source):
        self.base = base          # e.g. 'qoscls.cmd?action=add&isWired=1'
        self.params = params      # ordered parameter names, e.g. ['clsName', 'rulsOrder', ...]
        self.source = source      # the JavaScript that builds the URL (for the report)

    def pick(self, *candidates):
        """First candidate name the page actually uses (case-insensitive)."""
        low = {p.lower(): p for p in self.params}
        for c in candidates:
            if c.lower() in low:
                return low[c.lower()]
        return None

    def __repr__(self):
        return "FormSpec(%r, %r)" % (self.base, self.params)


def form_spec(text, marker):
    """Find the JavaScript that builds `marker...` (e.g. 'qoscls.cmd?action=add') and list its parameters."""
    text = text or ""
    idx = text.find(marker)
    if idx < 0:
        return None
    start = text.rfind("function", 0, idx)
    start = 0 if start < 0 else start
    nxt = re.search(r"\n\s*function\s", text[idx:])
    end = idx + nxt.start() if nxt else len(text)
    body = text[start:end]
    m = re.match(r"""[^'"\s]*""", text[idx:])
    base = m.group(0).rstrip("&") if m else marker
    params = []
    for p in re.findall(r"""['"]\s*[&?]?(\w+)=[^'"&\s]*['"]""", body):
        if p not in params and p not in ("action", "location", "src", "href", "code", "value", "name", "type", "id"):
            params.append(p)
    # parameters already inside the base string (e.g. isWired=1) are not repeated
    for p in re.findall(r"[?&](\w+)=", base):
        if p in params:
            params.remove(p)
    return FormSpec(base, params, body)


def select_options(text, name):
    """[(value, label)] of <select name=...> when the options are plain HTML."""
    m = re.search(r"""(?is)<select[^>]*\bname\s*=\s*['"]?%s['"]?[^>]*>(.*?)</select>""" % re.escape(name), text or "")
    if not m:
        return []
    out = []
    for om in re.finditer(r"""(?is)<option[^>]*?value\s*=\s*['"]?([^'" >]*)['"]?[^>]*>([^<]*)""", m.group(1)):
        out.append((om.group(1), clean(om.group(2))))
    return out


def split_records(value, sep="|", field=","):
    """'a,b,c|d,e,f|' -> [['a','b','c'], ['d','e','f']]"""
    return [r.split(field) for r in (value or "").split(sep) if r.strip()]


def label_values(text):
    """Two-column rows ('Model Name' | 'DSL-G256DG') as a dict, for info pages."""
    out = {}
    for row in table_rows(text):
        cells = [c for c in row["cells"] if c]
        if len(cells) == 2 and len(cells[0]) < 40:
            out.setdefault(cells[0].rstrip(":").strip(), cells[1])
    return out


def looks_like_login(text):
    t = (text or "").lower()
    if "login.cgi" in t and ("password" in t):
        return True
    if "frmlogin" in t:
        return True
    if len(t) < 2000 and "login" in t and ("location" in t or "href" in t) and "<table" not in t:
        return True
    return False


def error_text(text):
    """A short error message if the router answered with an error page, else ''."""
    plain = strip_tags(text)
    for pat in (r"(?i)(error[^.]{0,160}\.)", r"(?i)([^.]{0,80}failed[^.]{0,120}\.)",
                r"(?i)(invalid[^.]{0,160}\.)"):
        e = re.search(pat, plain)
        if e:
            return clean(e.group(1))[:200]
    return ""
