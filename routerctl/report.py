"""A plain-text report to send to whoever helps you, with private details hidden.

It lists which router pages were found, which fields their forms send, the
queues and interfaces, and the recent actions. Device addresses are shortened
(only the last two parts stay), public IP addresses are hidden, and the
password is never included.
"""

import ipaddress
import json
import platform
import re
import sys
import time

from . import parsing as P
from . import drivers
from .drivers import RouterError

VERSION = "2.0.0"


def _mask_mac(m):
    p = m.group(0).replace("-", ":").split(":")
    return "xx:xx:xx:xx:%s:%s" % (p[4], p[5])


def _mask_ip(m):
    try:
        ip = ipaddress.ip_address(m.group(0))
    except ValueError:
        return m.group(0)
    return m.group(0) if (ip.is_private or ip.is_loopback or ip.is_unspecified) else "x.x.x.x"


def redact(text, password=""):
    text = P.MAC_RE.sub(_mask_mac, text)
    text = P.IP_RE.sub(_mask_ip, text)
    text = re.sub(r"(sessionKey\D{0,4})\d+", r"\g<1>…", text)
    text = re.sub(r"\b(r[ud]|bk)[0-9a-f]{8}([0-9a-f]{4}b?)\b", r"\1xxxxxxxx\2", text)
    if password and len(password) >= 4:
        text = text.replace(password, "********")
    return text


def build(ctl):
    lines = []
    add = lines.append
    add("Home Internet Control report — %s" % time.strftime("%Y-%m-%d %H:%M"))
    add("App %s, Python %s, %s %s" % (VERSION, sys.version.split()[0], platform.system(), platform.release()))
    st = ctl.status
    add("Connected: %s   Error: %s" % (st.get("connected"), st.get("error") or "-"))
    add("Router info: %s" % json.dumps(st.get("info", {})))
    add("Model: %s (setting: %s)" % (st.get("model_name") or "-", ctl.cfg.get("model", "auto")))
    add("Can do: %s" % (", ".join(k for k, v in ctl.caps.items() if v) or "-"))
    add("Clock set: %s" % st.get("clock_ok"))
    add("Warnings: %s" % ("; ".join(st.get("warnings", [])) or "-"))
    r = ctl.router
    if r is not None:
        add("")
        add("== Router pages ==")
        try:
            pr = r.probe()
            add("Address %s, login %s" % (pr["base"], pr["auth"]))
            for feature, e in pr["features"].items():
                add("- %-9s %s" % (feature, e.get("page") or ("ERROR " + e["error"] if e.get("error") else "not found")))
                if e.get("js_vars"):
                    add("    vars: %s" % ", ".join(e["js_vars"]))
                for marker, f in (e.get("forms") or {}).items():
                    add("    form %s -> %s" % (f["base"], ", ".join(f["params"])))
        except RouterError as e:
            add("probe failed: %s" % e)
        try:
            lines.extend(r.report_lines())
        except RouterError as e:
            add("driver report failed: %s" % e)
    elif st.get("configured"):
        add("")
        add("== Router front page (not logged in) ==")
        try:
            args, kw = ctl._driver_args()
            status, text, headers = drivers.front_page(args[0])
            ranked = drivers.rank(status, text, headers)
            add("HTTP %s, %d bytes, server: %s" % (status, len(text), headers.get("server", "-")))
            add("Model scores: %s" % ", ".join("%s=%d" % (d.MODEL, s) for d, s in ranked))
            title = re.search(r"(?is)<title>(.*?)</title>", text)
            add("Title: %s" % (P.clean(P.strip_tags(title.group(1))) if title else "-"))
            drv = drivers.make(ctl.model() or ranked[0][0].MODEL, *args, **kw)
            drv.front = (status, text)
            lines.extend(drv.report_lines())
        except RouterError as e:
            add("could not read it: %s" % e)
    add("")
    add("== Devices ==")
    devs = ctl.device_list()
    add("%d known, %d online (%d Wi-Fi, %d cable)" % (len(devs), sum(1 for d in devs if d.get("online")),
                                                     sum(1 for d in devs if d.get("online") and d.get("link") == "wifi"),
                                                     sum(1 for d in devs if d.get("online") and d.get("link") == "wired")))
    for d in devs:
        add("  %s %-15s %-6s %-7s online=%s limit=%s block=%s" % (
            d["mac"], d.get("ip", ""), d.get("link", ""), d.get("ifc", ""), d.get("online"),
            json.dumps(d.get("limit")) if d.get("limit") else "-", json.dumps(d.get("block")) if d.get("block") else "-"))
    add("")
    add("== Traffic ==")
    snap = ctl.traffic.snapshot(60)
    add("WAN interface: %s  now down %s / up %s kbit/s" % (snap["wan"], snap["down"], snap["up"]))
    add("Interfaces: %s" % json.dumps(snap["ifaces"]))
    add("")
    add("== Self-test ==")
    add(json.dumps(ctl.store.data.get("selftest", {}), indent=1))
    add("")
    add("== Recent actions ==")
    lines.extend(l.rstrip("\n") for l in ctl.recent_log(40))
    pw = (ctl.cfg.get("router") or {}).get("password", "")
    return redact("\n".join(lines), pw)
