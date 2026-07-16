import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import vohive_keepalive as vk


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


class KeepaliveTests(unittest.TestCase):
    def test_validate_defaults(self):
        cfg = vk.validate_config({})
        self.assertEqual(cfg["interval_days"], 120)
        self.assertEqual(cfg["idle_mode"], "cellular_sms")

    def test_rejects_non_https_config(self):
        with self.assertRaises(ValueError):
            vk.validate_config({"target_url": "http://example.com/"})

    def test_database_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            cfg = vk.validate_config({})
            run_id = db.start_run("manual", cfg)
            db.finish_run(run_id, {"finished_at": vk.iso(), "status": "success", "session_total_bytes": 12})
            self.assertEqual(db.history(1)[0]["session_total_bytes"], 12)
            self.assertIsNotNone(db.last_success())

    def test_full_run_records_success_without_real_network(self):
        class FakeClient:
            def overview(self, device_id):
                return {"network_connected": True, "modem": {"iccid": "test-card"}}

            def set_network(self, *args, **kwargs):
                return {}

            def set_vowifi(self, *args, **kwargs):
                return {}

            def set_flight(self, *args, **kwargs):
                return {}

            def put_policy(self, *args, **kwargs):
                return {}

        samples = iter([(1000, 2000), (1010, 2005), (1110, 2055), (1120, 2060), (1120, 2060)])

        def counters(_interface):
            try:
                return next(samples)
            except StopIteration:
                return (1120, 2060)

        with tempfile.TemporaryDirectory() as tmp:
            config = vk.ConfigStore(str(Path(tmp) / "config.json"))
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            manager = vk.KeepAliveManager(config, db)
            manager._notify = lambda *args, **kwargs: None
            with mock.patch.object(vk, "VoHiveClient", FakeClient), mock.patch.object(
                vk, "interface_counters", counters
            ), mock.patch.object(
                vk,
                "bound_http_get",
                return_value=vk.FetchResult(200, 100, "10.0.0.2", "https://example.com/"),
            ):
                manager._run("manual")
            row = db.history(1)[0]
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["http_status"], 200)
            self.assertGreater(row["request_total_bytes"], 0)

    def test_success_notification_contains_usage_and_next_run(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"code":0}'

        with tempfile.TemporaryDirectory() as tmp:
            config = vk.ConfigStore(str(Path(tmp) / "config.json"))
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            db.set_meta("next_run_at", "2026-11-13T08:35:38+00:00")
            manager = vk.KeepAliveManager(config, db)
            captured = {}

            def fake_urlopen(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse()

            with mock.patch.dict(
                os.environ,
                {"PUSHDEER_KEY": "test-key", "PUSHDEER_ENDPOINT": "https://push.example.test/message"},
                clear=False,
            ), mock.patch.object(vk.urllib.request, "urlopen", side_effect=fake_urlopen):
                manager._notify(config.load(), "success", 321, "")
            fields = urllib.parse.parse_qs(captured["request"].data.decode())
            self.assertEqual(fields["text"], ["VoHive 保号成功"])
            self.assertIn("321 bytes", fields["desp"][0])
            self.assertIn("2026-11-13", fields["desp"][0])

    @unittest.skipUnless(os.geteuid() == 0 and Path("/sys/class/net/lo").exists(), "requires root and loopback")
    def test_bound_http_loopback(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = vk.bound_http_get(
                f"http://127.0.0.1:{server.server_port}/", "lo", 5, 4096
            )
            self.assertEqual(result.status, 200)
            self.assertEqual(result.source_address, "127.0.0.1")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
