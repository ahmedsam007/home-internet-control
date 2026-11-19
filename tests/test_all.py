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
from routerctl.router import LoginError, Router  # noqa: E402
from routerctl.server import load_config  # noqa: E402
from tools.mock_router import serve  # noqa: E402


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
            Router(self.host, "admin", "nope").login()

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


if __name__ == "__main__":
    unittest.main()
