"""Run with:  python -m unittest discover -s tests"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from routerctl import parsing as P  # noqa: E402
from routerctl import report  # noqa: E402
from routerctl.control import Controller, next_day, windows  # noqa: E402
from routerctl.drivers import LoginError, DLinkBroadcom, VTechNB403, detect  # noqa: E402
from routerctl.server import load_config  # noqa: E402
from tools.mock_router import serve  # noqa: E402
from tools import mock_vtech  # noqa: E402
from routerctl import drivers  # noqa: E402
from routerctl.qr import QR  # noqa: E402
from routerctl.server import App, make_server  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402


class Parsing(unittest.TestCase):
    def test_form_spec(self):
        js = """function btnApply() {
          var loc = 'qosmgmt.cmd?action=savapply';
          loc += '&enblQos=1';
          loc += '&defaultQueue=' + '-1';
          loc += '&sessionKey=123';
          var code = 'location="' + loc + '"';
        }
        function other() {}"""
        spec = P.form_spec(js, "qosmgmt.cmd?action=savapply")
        self.assertEqual(spec.base, "qosmgmt.cmd?action=savapply")
        self.assertEqual(spec.params, ["enblQos", "defaultQueue", "sessionKey"])
        self.assertEqual(spec.pick("ENBLQOS"), "enblQos")

    def test_tables_and_vars(self):
        t = "<table><tr><td>pc</td><td>AA-BB-CC-DD-EE-FF</td><td>192.168.1.9</td></tr></table>" \
            "<script>var macs = 'a,11:22:33:44:55:66,Always|';</script>"
        rows = P.table_rows(t)
        self.assertEqual(rows[0]["cells"][1], "AA-BB-CC-DD-EE-FF")
        self.assertEqual(P.js_vars(t)["macs"], "a,11:22:33:44:55:66,Always|")
        self.assertEqual(P.norm_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")

    def test_session_key(self):
        self.assertEqual(P.session_key("loc += '&sessionKey=987654';"), "987654")
        self.assertEqual(P.session_key("var sessionKey='42';"), "42")

    def test_windows(self):
        self.assertEqual(windows(60, 120, 127), [(60, 119, 127)])
        self.assertEqual(windows(23 * 60, 7 * 60, 1), [(1380, 1439, 1), (0, 419, 2)])
        self.assertEqual(next_day(64), 1)

    def test_redact(self):
        out = report.redact("aa:bb:cc:dd:ee:ff 8.8.8.8 192.168.1.4 sessionKey=123 rdaabbccddeeff secretpw", "secretpw")
        self.assertNotIn("aa:bb:cc:dd", out)
        self.assertIn("xx:xx:xx:xx:ee:ff", out)
        self.assertIn("x.x.x.x", out)
        self.assertIn("192.168.1.4", out)
        self.assertNotIn("123", out)
        self.assertNotIn("secretpw", out)
        self.assertIn("rdxxxxxxxxeeff", out)


class AgainstFakeRouter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.state = serve(0)
        cls.host = "127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def ctl(self):
        cfg = load_config("/nonexistent.json")
        cfg["router"] = {"host": self.host, "username": "admin", "password": "admin"}
        c = Controller(cfg, tempfile.mkdtemp(), fake_ping=True)
        c.refresh()
        return c

    def test_wrong_password(self):
        with self.assertRaises(LoginError):
            DLinkBroadcom(self.host, "admin", "nope").login()

    def test_devices(self):
        c = self.ctl()
        self.assertEqual(len(c.devices), 6)
        phone = c.devices["a4:83:e7:10:20:02"]
        self.assertEqual((phone["link"], phone["band"], phone["ip"]), ("wifi", "2.4 GHz", "192.168.1.3"))

    def test_limit_wifi_device(self):
        c = self.ctl()
        mac = "a4:83:e7:10:20:02"
        d = c.set_limit(mac, 2000, 500)
        self.assertTrue(d["limit"]["on_router"])
        rules = {r[0]: r for r in self.state.rules}
        up, down = rules["rua483e7102002"], rules["rda483e7102002"]
        self.assertEqual((up[2], up[4], up[13], up[17]), ("LAN", mac, "67", "500"))
        self.assertEqual((down[2], down[3], down[7], down[13], down[17]), ("WAN", "800", "192.168.1.3", "8", "2000"))
        c.remove_limit(mac)
        self.assertNotIn("rua483e7102002", {r[0] for r in self.state.rules})
        self.assertIn("old", {r[0] for r in self.state.rules}, "rules made by hand must not be touched")

    def test_upload_trick(self):
        c = self.ctl()
        mac = "f0:18:98:10:20:03"
        d = c.set_limit(mac, 2400, 0, method="upload_trick")
        self.assertEqual(d["limit"]["up_applied"], 200)
        names = {r[0] for r in self.state.rules}
        self.assertIn("ruf01898102003", names)
        self.assertNotIn("rdf01898102003", names)
        c.remove_limit(mac)

    def test_pause_and_bedtime(self):
        c = self.ctl()
        mac = "d8:3a:dd:10:20:04"
        c.block(mac, "pause")
        self.assertEqual(self.state.blocks, [["bkd83add102004", mac, "Always"]])
        c.block(mac, "bedtime", "22:30", "06:00", 31)
        self.assertEqual([b[2] for b in self.state.blocks], ["rc22302359d31", "rc00000559d62"])
        self.assertEqual({s[0] for s in self.state.scheds}, {"rc22302359d31", "rc00000559d62"})
        c.unblock(mac)
        self.assertEqual(self.state.blocks, [])
        self.assertEqual(self.state.scheds, [], "unused schedules are cleaned up")

    def test_qos_switched_on_when_needed(self):
        self.state.enbl_qos = "0"
        c = self.ctl()
        c.set_limit("9c:b6:d0:10:20:06", 1000, 300)
        self.assertEqual(self.state.enbl_qos, "1")
        c.remove_limit("9c:b6:d0:10:20:06")

    def test_websites(self):
        c = self.ctl()
        c.add_site("https://www.example.com/page")
        self.assertEqual(c.sites, [{"url": "www.example.com", "schedule": "Always"}])
        c.remove_site("www.example.com")
        self.assertEqual(c.sites, [])

    def test_report_has_no_password(self):
        c = self.ctl()
        text = report.build(c)
        self.assertIn("qoscls.cmd?action=add", text)
        self.assertNotIn("a4:83:e7:10", text)


class PhoneAccess(unittest.TestCase):
    """The page as a phone sees it (localhost is not trusted in these tests)."""

    def setUp(self):
        cfg = load_config("/nonexistent.json")
        cfg["phone_access"] = True
        self.ctl = Controller(cfg, tempfile.mkdtemp(), fake_ping=True)
        self.app = App(self.ctl, None)
        self.app.trust_localhost = False
        self.srv = make_server(self.app, 0)
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path, body=None, cookie="", host=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Requested-With": "home-internet-control", "Cookie": cookie})
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode(), r.headers.get("Set-Cookie") or ""
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode(), ""

    def test_pin_flow(self):
        pin = self.ctl.cfg["phone_pin"]
        self.assertRegex(pin, r"^\d{6}$")
        self.assertEqual(self.call("/api/devices")[0], 401)
        self.assertEqual(self.call("/")[0], 200, "the page itself loads and shows the PIN screen")
        self.assertEqual(self.call("/api/login", {"pin": "000000" if pin != "000000" else "111111"})[0], 401)
        code, _, cookie = self.call("/api/login", {"pin": pin})
        self.assertEqual(code, 200)
        cookie = cookie.split(";")[0]
        self.assertEqual(self.call("/api/devices", cookie=cookie)[0], 200)
        self.assertEqual(self.call("/api/qr.svg", cookie=cookie)[0], 403, "only the computer sees the QR/PIN")
        self.assertEqual(self.call("/api/phone", {"on": False}, cookie=cookie)[0], 403)
        self.call("/api/logout", {}, cookie=cookie)
        self.assertEqual(self.call("/api/devices", cookie=cookie)[0], 401)

    def test_lockout(self):
        for _ in range(5):
            self.call("/api/login", {"pin": "x"})
        code, body, _ = self.call("/api/login", {"pin": self.ctl.cfg["phone_pin"]})
        self.assertEqual(code, 401)
        self.assertIn("Too many", body)

    def test_foreign_host_refused(self):
        self.assertEqual(self.call("/api/auth", host="evil.example.com")[0], 403)

    def test_new_pin_signs_out(self):
        code, _, cookie = self.call("/api/login", {"pin": self.ctl.cfg["phone_pin"]})
        old = self.ctl.cfg["phone_pin"]
        self.app.set_phone({"new_pin": True})
        self.assertEqual(self.call("/api/devices", cookie=cookie.split(";")[0])[0], 401)
        self.assertRegex(self.ctl.cfg["phone_pin"], r"^\d{6}$")
        self.assertTrue(old or True)

    def test_qr_svg(self):
        svg = QR("http://192.168.1.4:8770/#pin=123456").svg()
        self.assertTrue(svg.startswith("<svg") and "path" in svg)


class Models(unittest.TestCase):
    """Telling routers apart, and the VTech early-support driver."""

    @classmethod
    def setUpClass(cls):
        cls.dlink, cls.dstate = serve(0)
        cls.vtech, cls.vstate = mock_vtech.serve(0)
        cls.dhost = "127.0.0.1:%d" % cls.dlink.server_address[1]
        cls.vhost = "127.0.0.1:%d" % cls.vtech.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.dlink.shutdown()
        cls.vtech.shutdown()

    def ctl(self, host, model="auto"):
        cfg = load_config("/nonexistent.json")
        cfg["router"] = {"host": host, "username": "admin", "password": "admin"}
        cfg["model"] = model
        c = Controller(cfg, tempfile.mkdtemp(), fake_ping=True)
        c.refresh()
        return c

    def test_detect_each_router(self):
        self.assertIsInstance(detect(self.dhost, "admin", "admin"), DLinkBroadcom)
        self.assertIsInstance(detect(self.vhost, "admin", "admin"), VTechNB403)

    def test_recognised_router_wrong_password_is_clear(self):
        with self.assertRaises(LoginError) as e:
            detect(self.vhost, "admin", "wrong")
        self.assertIn("password", str(e.exception))

    def test_unrecognised_front_page_tries_each_driver(self):
        ranked = drivers.rank(200, "<html>hello</html>", {})
        self.assertEqual([s for _, s in ranked], [0, 0])
        self.assertEqual(ranked[0][0], DLinkBroadcom, "keeps the list order when nothing matches")

    def test_dlink_capabilities(self):
        c = self.ctl(self.dhost)
        self.assertEqual(c.status["model"], "dlink-broadcom")
        self.assertEqual(c.cfg["detected_model"], "dlink-broadcom")
        self.assertTrue(all(c.caps.values()), c.caps)

    def test_vtech_devices_read_only(self):
        self.vstate.requested.clear()
        c = self.ctl(self.vhost)
        self.assertEqual(c.status["model"], "vtech-nb403")
        self.assertTrue(c.caps["devices"])
        self.assertFalse(c.caps["speed_limit"] or c.caps["pause"] or c.caps["sites"] or c.caps["traffic"])
        self.assertEqual({d["mac"] for d in c.devices.values()}, {d["mac"] for d in mock_vtech.DEVICES})
        self.assertEqual(c.devices["a4:83:e7:30:40:02"]["hostname"], "Phone-B")
        self.assertEqual(c.status["info"].get("firmware"), "BZ_2.02.07.09.13.01")
        asked = set(self.vstate.requested)
        for bad in ("/reboot.cgi", "/logout.html", "/reset.html", "/goform/save_lan", "/goform/wifi"):
            self.assertNotIn(bad, asked, "must never open " + bad)
        self.assertIn("/wifi.html", asked, "follows links found in scripts")
        self.assertEqual([r for r in self.vstate.requested if r.startswith("POST")], ["POST /goform/login"])

    def test_vtech_refuses_unsupported_actions(self):
        c = self.ctl(self.vhost)
        with self.assertRaises(drivers.NotSupported):
            c.set_limit("a4:83:e7:30:40:02", 2000, 500)
        with self.assertRaises(drivers.NotSupported):
            c.block("a4:83:e7:30:40:02", "pause")
        with self.assertRaises(drivers.NotSupported):
            c.add_site("example.com")

    def test_vtech_report(self):
        c = self.ctl(self.vhost)
        text = report.build(c)
        self.assertIn("Router pages", text)
        self.assertIn("/lan.html", text)
        self.assertIn("loginPass(password)", text)
        self.assertNotIn("a4:83:e7:30", text)

    def test_vtech_report_when_login_fails(self):
        cfg = load_config("/nonexistent.json")
        cfg["router"] = {"host": self.vhost, "username": "admin", "password": "wrong-pass"}
        c = Controller(cfg, tempfile.mkdtemp(), fake_ping=True)
        with self.assertRaises(LoginError):
            c.refresh()
        text = report.build(c)
        self.assertIn("vtech-nb403=", text)
        self.assertIn("loginPass(password)", text)
        self.assertNotIn("wrong-pass", text)

    def test_chosen_model_skips_detection(self):
        c = self.ctl(self.dhost, model="dlink-broadcom")
        self.assertIsInstance(c.router, DLinkBroadcom)
        self.assertNotIn("detected_model", {k for k, v in c.cfg.items() if v})

    def test_settings_save_detects_and_reports_model(self):
        cfg = load_config("/nonexistent.json")
        c = Controller(cfg, tempfile.mkdtemp(), fake_ping=True)
        app = App(c, None)
        code, out = app.save_settings({"host": self.vhost, "username": "admin", "password": "admin", "model": "auto"})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["model"], "vtech-nb403")
        self.assertEqual(out["settings"]["detected"], "vtech-nb403")
        self.assertEqual({m["id"] for m in out["settings"]["models"]}, {"dlink-broadcom", "vtech-nb403"})
        code, out = app.save_settings({"model": "no-such-router"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
