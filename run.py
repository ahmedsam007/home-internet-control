"""Start D-Link DSL Control:  python run.py   (or double-click start-windows.bat / start-mac.command)

Options:
  --demo      try it with a fake router (nothing on your network is touched)
  --lan       let phones and other computers in the house open the page too
  --port N    use another port for the page (default 8770)
  --no-open   do not open the browser
"""

import argparse
import os
import sys
import tempfile
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

if sys.version_info < (3, 8):
    sys.exit("Python 3.8 or newer is needed. Get it from https://www.python.org/downloads/")

from routerctl.control import Controller  # noqa: E402
from routerctl.server import App, load_config, make_server  # noqa: E402


def demo_setup(app, state):
    """Make the self-test work against the fake router."""
    from tools.mock_router import DEVICES
    laptop = next(d for d in DEVICES if d["host"] == "Laptop")

    def rule_rate(field, value):
        for r in state.rules:
            if r[-1] == "1" and r[field].split("/")[0] == value and r[-2]:
                return int(r[-2])
        return None

    def down():
        time.sleep(1.5)
        r = rule_rate(7, laptop["ip"])
        if r:
            return int(r * 0.93)
        up = rule_rate(4, laptop["mac"])
        return int(min(24000, up * 13)) if up else 24000

    def up():
        time.sleep(1.5)
        r = rule_rate(4, laptop["mac"])
        return int(r * 0.95) if r else 4200

    app.selftest.measure_down = down
    app.selftest.measure_up = up
    app.selftest.find_me = lambda: app.ctl.devices.get(laptop["mac"], {"ip": laptop["ip"]})


def main():
    ap = argparse.ArgumentParser(description="D-Link DSL Control")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--lan", action="store_true")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.demo:
        from tools.mock_router import serve
        srv, state = serve(0)
        mock_port = srv.server_address[1]
        cfg = load_config(os.path.join(ROOT, "no-such-config.json"))
        cfg["router"] = {"host": "127.0.0.1:%d" % mock_port, "username": "admin", "password": "admin"}
        cfg["internet_ping"] = "1.1.1.1"
        data_dir = os.path.join(tempfile.gettempdir(), "dlink-dsl-control-demo-%d" % os.getpid())
        config_path = None
        ctl = Controller(cfg, data_dir, fake_ping=True)
    else:
        config_path = os.path.join(ROOT, "config.json")
        cfg = load_config(config_path)
        ctl = Controller(cfg, os.path.join(ROOT, "data"))

    app = App(ctl, config_path, lan=a.lan)
    if a.demo:
        demo_setup(app, state)
    port = a.port or int(cfg.get("port", 8770))
    try:
        httpd = make_server(app, port)
    except OSError:
        sys.exit("Port %d is busy — is the program already running? Close the other window, or use --port 8771." % port)
    ctl.start()
    url = "http://127.0.0.1:%d/" % port
    print("D-Link DSL Control%s is running." % (" (DEMO, fake router)" if a.demo else ""))
    print("Open %s in your browser. Close this window to stop it." % url)
    if a.lan:
        print("Other devices in the house can open http://<this computer's address>:%d/" % port)
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctl.stop()


if __name__ == "__main__":
    main()
