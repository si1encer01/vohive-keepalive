#!/usr/bin/env python3
import json
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUSHKEY = os.environ["PUSHDEER_KEY"]
ENDPOINT = os.environ.get("PUSHDEER_ENDPOINT", "https://api2.pushdeer.com/message/push")
PORT = int(os.environ.get("LISTEN_PORT", "7581"))

def find_value(obj, keys):
    wanted = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in wanted and v not in (None, "", [], {}):
                if isinstance(v, (dict, list)):
                    return json.dumps(v, ensure_ascii=False)
                return str(v)
        for v in obj.values():
            got = find_value(v, keys)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = find_value(v, keys)
            if got:
                return got
    return ""

def send_push(payload):
    sender = find_value(payload, ["sender", "from", "from_number", "phone", "source"])
    device = find_value(payload, ["device_label", "device_name", "device_id", "sms_device"])
    message = find_value(payload, ["text", "content", "message", "body", "description", "desp"])
    event = find_value(payload, ["event", "event_type", "type"])
    # Use the SMS body itself as the PushDeer notification title.  Keep only
    # useful routing metadata in the description so the message is not shown twice.
    title = message.strip() if message else ("来自 " + sender if sender else "收到短信")
    lines = []
    if sender:
        lines.append("发件人：" + sender)
    if device:
        lines.append("设备：" + device)
    if event:
        lines.append("事件：" + event)
    body = "\n".join(lines)
    data = urllib.parse.urlencode({
        "pushkey": PUSHKEY,
        "text": title,
        "desp": body,
        "type": "markdown",
    }).encode()
    req = urllib.request.Request(
        ENDPOINT,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "vohive-pushdeer-bridge/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read(65536)
        if not (200 <= resp.status < 300):
            raise RuntimeError("pushdeer http status")
        try:
            result = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(result, dict) and result.get("code") not in (None, 0, "0"):
                raise RuntimeError("pushdeer api error")
        except json.JSONDecodeError:
            pass

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return
    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self.reply(200, {"ok": True}) if self.path == "/health" else self.reply(404, {"ok": False})
    def do_POST(self):
        if self.path != "/vohive":
            self.reply(404, {"ok": False})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1048576:
                raise ValueError("invalid body length")
            raw = self.rfile.read(length)
            ctype = self.headers.get("Content-Type", "").lower()
            if "json" in ctype:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
            else:
                parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                payload = {k: v[-1] if isinstance(v, list) and v else v for k, v in parsed.items()}
            send_push(payload)
            print("PUSHDEER_SENT", flush=True)
            self.reply(200, {"ok": True})
        except Exception as exc:
            print("PUSHDEER_ERROR " + type(exc).__name__, file=sys.stderr, flush=True)
            self.reply(502, {"ok": False})

if __name__ == "__main__":
    print("BRIDGE_READY", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
