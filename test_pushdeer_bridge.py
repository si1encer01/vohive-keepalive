import importlib.util
import os
import urllib.parse
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("PUSHDEER_KEY", "test-key")
os.environ.setdefault("PUSHDEER_ENDPOINT", "https://push.example.test/message")
SPEC = importlib.util.spec_from_file_location(
    "vohive_pushdeer_bridge", Path(__file__).with_name("vohive_pushdeer_bridge.py")
)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b'{"code":0}'


class PushDeerBridgeTests(unittest.TestCase):
    def send(self, payload):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["fields"] = urllib.parse.parse_qs(request.data.decode())
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.object(bridge.urllib.request, "urlopen", fake_urlopen):
            bridge.send_push(payload)
        return captured

    def test_sms_body_is_notification_title(self):
        result = self.send(
            {
                "event": "sms.received",
                "sender": "+440000000000",
                "device_id": "example-device",
                "content": "Your verification code is 654321",
            }
        )
        fields = result["fields"]
        self.assertEqual(fields["text"], ["Your verification code is 654321"])
        self.assertNotIn("VoHive 短信通知", fields["text"][0])
        self.assertNotIn("Your verification code is 654321", fields["desp"][0])
        self.assertIn("发件人：+440000000000", fields["desp"][0])
        self.assertIn("设备：example-device", fields["desp"][0])

    def test_missing_body_uses_sender_fallback(self):
        result = self.send({"sender": "+440000000000"})
        self.assertEqual(result["fields"]["text"], ["来自 +440000000000"])


if __name__ == "__main__":
    unittest.main()
