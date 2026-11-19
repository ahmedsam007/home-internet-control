"""The routers this program can talk to, and how it tells them apart.

To add a router: write a driver (see base.py), then list it in DRIVERS.
"""

import re

from .base import CAPABILITIES, ActionError, Driver, LoginError, NotSupported, PageMissing, RouterError
from .broadcom import DLinkBroadcom
from .vtech import VTechNB403

DRIVERS = [DLinkBroadcom, VTechNB403]
MODELS = {d.MODEL: d for d in DRIVERS}

__all__ = ["CAPABILITIES", "DRIVERS", "MODELS", "ActionError", "Driver", "LoginError", "NotSupported",
           "PageMissing", "RouterError", "detect", "make", "model_list", "model_name"]


def model_list():
    return [{"id": d.MODEL, "name": d.NAME, "examples": d.EXAMPLES} for d in DRIVERS]


def model_name(model):
    d = MODELS.get(model)
    return d.NAME if d else ""


def make(model, host, username, password, **kw):
    if model not in MODELS:
        raise RouterError("Unknown router model: %s" % model)
    return MODELS[model](host, username, password, **kw)


def front_page(host, timeout=8):
    """(status, text, headers) of the router's first page, read without logging in."""
    d = Driver(host, "", "", timeout=timeout)
    status, text = d._fetch("/")
    if status == 200 and len(text) < 600:
        # a tiny page that only forwards to the real one (meta refresh or script)
        m = re.search(r"""(?i)(?:url=|location(?:\.href)?\s*=\s*)['"]?([\w./-]+\.\w+)""", text)
        if m:
            s2, t2 = d._fetch(m.group(1))
            if s2 == 200:
                text = text + "\n" + t2
    return status, text, d.last_headers


def rank(status, text, headers):
    """Drivers, most likely first, with their scores."""
    scored = [(d.identify(status, text, headers), i, d) for i, d in enumerate(DRIVERS)]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(d, s) for s, _, d in scored]


def detect(host, username, password, timeout=8, log=None, **kw):
    """Find which driver fits the router and log in with it. Returns the logged-in driver.

    The router's front page usually says what it is. When it does, only that
    driver is tried, so a wrong password gives a clear message. When it does
    not, each driver is tried in turn and the first that logs in wins.
    """
    status, text, headers = front_page(host, timeout)
    ranked = rank(status, text, headers)
    recognised = [d for d, s in ranked if s >= 3]
    candidates = recognised[:1] or [d for d, _ in ranked]
    last = None
    for cls in candidates:
        drv = cls(host, username, password, timeout=timeout, log=log, **kw)
        try:
            drv.login()
            return drv
        except LoginError as e:
            last = e
            if recognised:
                raise
    raise LoginError("Could not log in. Check the password. If it is right, this router model may not be "
                     "supported yet: choose the model under Settings, or send the report from the Check page."
                     if last else "No router driver could log in.")
