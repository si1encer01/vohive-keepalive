import http.server
import json
import os
import tempfile
import threading
import unittest
import urllib.parse
import subprocess
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
        self.assertFalse(cfg["profile_management_enabled"])

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

    def test_profile_discovery_creates_independent_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            profiles = [
                {
                    "iccid": "1111111111111111111",
                    "profileState": "enabled",
                    "serviceProviderName": "Carrier A",
                    "profileName": "Line A",
                },
                {
                    "iccid": "2222222222222222222",
                    "profileState": "disabled",
                    "serviceProviderName": "Carrier B",
                    "profileName": "Line B",
                },
            ]
            db.sync_profiles(profiles, 120, "2026-11-17T00:00:00+00:00")
            items = db.profiles()
            self.assertEqual(len(items), 2)
            self.assertEqual({item["next_run_at"] for item in items}, {"2026-11-17T00:00:00+00:00"})
            db.schedule_profile(
                profiles[1]["iccid"],
                "2026-12-01T00:00:00+00:00",
                success_at="2026-08-01T00:00:00+00:00",
                success_bytes=321,
            )
            updated = db.profile(profiles[1]["iccid"])
            self.assertEqual(updated["last_success_bytes"], 321)
            self.assertEqual(updated["next_run_at"], "2026-12-01T00:00:00+00:00")

            edited = db.update_profile_policy(
                profiles[0]["iccid"],
                label="Main number",
                keepalive_enabled=False,
                interval_days=90,
            )
            self.assertEqual(edited["label"], "Main number")
            self.assertEqual(edited["keepalive_enabled"], 0)
            self.assertEqual(edited["interval_days"], 90)

            db.sync_profiles([profiles[0]], 120, "2027-01-01T00:00:00+00:00")
            self.assertEqual(db.profile(profiles[1]["iccid"])["profile_state"], "missing")
            self.assertIsNone(db.next_due_profile())

    def test_lpac_profile_list_uses_safe_argument_vector(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "lpac"
            binary.write_text("test")
            binary.chmod(0o700)
            cfg = vk.validate_config({"lpac_path": str(binary), "lpac_at_device": "/dev/ttyUSB9"})
            output = {
                "type": "lpa",
                "payload": {
                    "code": 0,
                    "message": "success",
                    "data": [{"iccid": "1111111111111111111", "profileState": "enabled"}],
                },
            }
            completed = subprocess.CompletedProcess([], 0, json.dumps(output), "")
            with mock.patch.object(vk.subprocess, "run", return_value=completed) as run:
                items = vk.ProfileManager().list_profiles(cfg)
            self.assertEqual(items[0]["iccid"], "1111111111111111111")
            args = run.call_args.args[0]
            self.assertEqual(args, [str(binary), "profile", "list"])
            self.assertEqual(run.call_args.kwargs["env"]["LPAC_APDU"], "at")
            self.assertEqual(run.call_args.kwargs["env"]["LPAC_APDU_AT_DEVICE"], "/dev/ttyUSB9")

    def test_single_profile_backfills_legacy_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            cfg = vk.validate_config({})
            run_id = db.start_run("scheduled", cfg)
            finished = "2026-07-16T09:56:35+00:00"
            db.finish_run(
                run_id,
                {"finished_at": finished, "status": "success", "session_total_bytes": 36116},
            )
            iccid = "1111111111111111111"
            db.sync_profiles(
                [{"iccid": iccid, "profileState": "enabled", "profileName": "Primary"}],
                120,
                "2026-11-13T09:56:35+00:00",
            )
            db.backfill_single_profile_legacy_success(iccid)
            item = db.profile(iccid)
            self.assertEqual(item["last_success_at"], finished)
            self.assertEqual(item["last_success_bytes"], 36116)

    def test_new_profile_gets_full_interval_instead_of_stale_legacy_date(self):
        first = "1111111111111111111"
        second = "2222222222222222222"
        with tempfile.TemporaryDirectory() as tmp:
            config = vk.ConfigStore(str(Path(tmp) / "config.json"))
            cfg = config.load()
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            db.set_meta("next_run_at", "2026-01-01T00:00:00+00:00")
            manager = vk.KeepAliveManager(config, db)
            manager._sync_discovered_profiles(
                cfg, [{"iccid": first, "profileState": "enabled", "profileName": "Primary"}]
            )
            self.assertEqual(db.profile(first)["next_run_at"], "2026-01-01T00:00:00+00:00")

            manager._sync_discovered_profiles(
                cfg,
                [
                    {"iccid": first, "profileState": "disabled", "profileName": "Primary"},
                    {"iccid": second, "profileState": "enabled", "profileName": "New line"},
                ],
            )
            remaining_days = (
                vk.parse_iso(db.profile(second)["next_run_at"]) - vk.now_utc()
            ).total_seconds() / 86400
            self.assertAlmostEqual(remaining_days, 120, delta=0.01)
            self.assertEqual(db.profile(first)["next_run_at"], "2026-01-01T00:00:00+00:00")

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

    def test_multi_profile_run_switches_target_and_restores_primary(self):
        first = "1111111111111111111"
        second = "2222222222222222222"

        class FakeProfiles:
            def __init__(self):
                self.current = first
                self.enabled = []

            def list_profiles(self, _cfg):
                return [
                    {"iccid": first, "profileState": "enabled" if self.current == first else "disabled", "profileName": "Primary"},
                    {"iccid": second, "profileState": "enabled" if self.current == second else "disabled", "profileName": "Secondary"},
                ]

            def enable_profile(self, _cfg, iccid):
                self.current = iccid
                self.enabled.append(iccid)

        profile_manager = FakeProfiles()

        class FakeClient:
            def overview(self, _device_id):
                return {"network_connected": True, "modem": {"iccid": profile_manager.current}}

            def set_network(self, *args, **kwargs): return {}
            def set_vowifi(self, *args, **kwargs): return {}
            def set_flight(self, *args, **kwargs): return {}
            def put_policy(self, *args, **kwargs): return {}

        samples = iter([(1000, 2000), (1010, 2005), (1110, 2055), (1120, 2060), (1120, 2060)])

        with tempfile.TemporaryDirectory() as tmp:
            config = vk.ConfigStore(str(Path(tmp) / "config.json"))
            cfg = config.load()
            cfg["profile_management_enabled"] = True
            config.save(cfg)
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            db.sync_profiles(profile_manager.list_profiles(cfg), 120, "2026-11-17T00:00:00+00:00")
            db.update_profile_policy(
                second, label="Secondary", keepalive_enabled=True, interval_days=37
            )
            manager = vk.KeepAliveManager(config, db)
            manager.profile_manager = profile_manager
            manager._notify = lambda *args, **kwargs: None
            with mock.patch.object(vk, "VoHiveClient", FakeClient), mock.patch.object(
                vk, "interface_counters", side_effect=lambda _interface: next(samples, (1120, 2060))
            ), mock.patch.object(
                vk, "bound_http_get", return_value=vk.FetchResult(200, 100, "10.0.0.2", "https://example.com/")
            ):
                manager._run("manual", second)
            self.assertEqual(profile_manager.enabled, [second, first])
            self.assertEqual(profile_manager.current, first)
            row = db.history(1)[0]
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["target_iccid"], second)
            updated = db.profile(second)
            self.assertIsNotNone(updated["last_success_at"])
            scheduled_days = (vk.parse_iso(updated["next_run_at"]) - vk.now_utc()).total_seconds() / 86400
            self.assertAlmostEqual(scheduled_days, 37, delta=0.01)

    def test_profile_policy_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = vk.ConfigStore(str(Path(tmp) / "config.json"))
            db = vk.Database(str(Path(tmp) / "db.sqlite"))
            iccid = "1111111111111111111"
            db.sync_profiles(
                [{"iccid": iccid, "profileState": "enabled", "profileName": "Line"}],
                120,
                "2026-11-17T00:00:00+00:00",
            )
            manager = vk.KeepAliveManager(config, db)
            with self.assertRaises(ValueError):
                manager.update_profile_policy(iccid, {"label": "", "interval_days": 120})
            with self.assertRaises(ValueError):
                manager.update_profile_policy(
                    iccid, {"label": "Line", "keepalive_enabled": "yes", "interval_days": 120}
                )

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
