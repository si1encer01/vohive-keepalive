#!/usr/bin/env python3
"""VoHive companion service for periodic SIM keepalive traffic.

The service deliberately keeps the cellular data session closed outside a
short verification window.  A verification request is bound to the configured
cellular interface with SO_BINDTODEVICE so the host's normal Ethernet default
route cannot accidentally satisfy the check.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc
DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "device_id": "modem-1",
    "interface": "wwan0",
    "interval_days": 120,
    "target_url": "https://example.com/",
    "ip_version": "v4",
    "apn": "",
    "network_connect_timeout_seconds": 90,
    "request_timeout_seconds": 20,
    "max_session_seconds": 180,
    "max_session_bytes": 524288,
    "max_response_bytes": 65536,
    "failure_retry_hours": 24,
    "idle_mode": "cellular_sms",
    "notify_on_success": True,
    "notify_on_failure": True,
    "cleanup_on_start": True,
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime | None = None) -> str:
    return (value or now_utc()).astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def clamp_int(value: Any, minimum: int, maximum: int, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if result < minimum or result > maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return result


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(raw)
    merged["enabled"] = bool(merged["enabled"])
    for key in ("notify_on_success", "notify_on_failure", "cleanup_on_start"):
        merged[key] = bool(merged[key])

    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(merged["device_id"])):
        raise ValueError("device_id 格式无效")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", str(merged["interface"])):
        raise ValueError("interface 格式无效")
    merged["device_id"] = str(merged["device_id"])
    merged["interface"] = str(merged["interface"])

    target = urllib.parse.urlsplit(str(merged["target_url"]))
    if target.scheme != "https" or not target.hostname:
        raise ValueError("target_url 必须是有效的 HTTPS 地址")
    if target.username or target.password:
        raise ValueError("target_url 不允许包含账号密码")
    merged["target_url"] = urllib.parse.urlunsplit(target)

    merged["interval_days"] = clamp_int(merged["interval_days"], 1, 179, "interval_days")
    merged["network_connect_timeout_seconds"] = clamp_int(
        merged["network_connect_timeout_seconds"], 10, 300, "network_connect_timeout_seconds"
    )
    merged["request_timeout_seconds"] = clamp_int(
        merged["request_timeout_seconds"], 5, 120, "request_timeout_seconds"
    )
    merged["max_session_seconds"] = clamp_int(
        merged["max_session_seconds"], 30, 600, "max_session_seconds"
    )
    merged["max_session_bytes"] = clamp_int(
        merged["max_session_bytes"], 16384, 10485760, "max_session_bytes"
    )
    merged["max_response_bytes"] = clamp_int(
        merged["max_response_bytes"], 1024, 1048576, "max_response_bytes"
    )
    merged["failure_retry_hours"] = clamp_int(
        merged["failure_retry_hours"], 1, 168, "failure_retry_hours"
    )
    if merged.get("ip_version") not in ("v4", "v6", "v4v6"):
        raise ValueError("ip_version 仅支持 v4、v6 或 v4v6")
    merged["apn"] = str(merged.get("apn", "")).strip()[:128]
    if merged.get("idle_mode") not in ("cellular_sms", "vowifi", "airplane"):
        raise ValueError("idle_mode 无效")
    return merged


class ConfigStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_CONFIG)

    def load(self) -> dict[str, Any]:
        with self.lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return validate_config(raw)

    def save(self, value: dict[str, Any]) -> dict[str, Any]:
        clean = validate_config(value)
        with self.lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        return clean


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._init()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _init(self) -> None:
        with self.lock, self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    trigger TEXT NOT NULL,
                    status TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    http_status INTEGER,
                    network_connected_at TEXT,
                    session_rx_bytes INTEGER NOT NULL DEFAULT 0,
                    session_tx_bytes INTEGER NOT NULL DEFAULT 0,
                    session_total_bytes INTEGER NOT NULL DEFAULT 0,
                    request_rx_bytes INTEGER NOT NULL DEFAULT 0,
                    request_tx_bytes INTEGER NOT NULL DEFAULT 0,
                    request_total_bytes INTEGER NOT NULL DEFAULT 0,
                    duration_seconds REAL,
                    error TEXT,
                    restore_status TEXT,
                    detail_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, started_at DESC);
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            con.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=COALESCE(error, '服务重启导致执行中断') "
                "WHERE status='running'",
                (iso(),),
            )

    def start_run(self, trigger: str, cfg: dict[str, Any]) -> int:
        with self.lock, self.connect() as con:
            cur = con.execute(
                "INSERT INTO runs(started_at, trigger, status, device_id, interface, target_url) "
                "VALUES(?,?,?,?,?,?)",
                (iso(), trigger, "running", cfg["device_id"], cfg["interface"], cfg["target_url"]),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, values: dict[str, Any]) -> None:
        allowed = {
            "finished_at", "status", "http_status", "network_connected_at",
            "session_rx_bytes", "session_tx_bytes", "session_total_bytes",
            "request_rx_bytes", "request_tx_bytes", "request_total_bytes",
            "duration_seconds", "error", "restore_status", "detail_json",
        }
        filtered = {k: v for k, v in values.items() if k in allowed}
        if not filtered:
            return
        columns = ",".join(f"{key}=?" for key in filtered)
        with self.lock, self.connect() as con:
            con.execute(f"UPDATE runs SET {columns} WHERE id=?", (*filtered.values(), run_id))

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self.lock, self.connect() as con:
            rows = con.execute(
                "SELECT id,started_at,finished_at,trigger,status,device_id,interface,target_url,http_status,"
                "network_connected_at,session_rx_bytes,session_tx_bytes,session_total_bytes,"
                "request_rx_bytes,request_tx_bytes,request_total_bytes,duration_seconds,error,restore_status "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_success(self) -> dict[str, Any] | None:
        with self.lock, self.connect() as con:
            row = con.execute(
                "SELECT * FROM runs WHERE status='success' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self.lock, self.connect() as con:
            if value is None:
                con.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                con.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def get_meta(self, key: str) -> str | None:
        with self.lock, self.connect() as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None


class VoHiveClient:
    def __init__(self):
        self.base = os.environ.get("VOHIVE_BASE_URL", "http://127.0.0.1:7575/api").rstrip("/")
        self.username = os.environ.get("VOHIVE_USER", "admin")
        self.password = os.environ.get("VOHIVE_PASSWORD", "")
        self.token = ""
        self.lock = threading.RLock()

    def _request(self, method: str, path: str, body: Any = None, auth: bool = True) -> Any:
        data = None if body is None else json_bytes(body)
        headers = {"Content-Type": "application/json", "User-Agent": "vohive-keepalive/1.0"}
        if auth:
            if not self.token:
                self.login()
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(1024 * 1024)
                return json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except urllib.error.HTTPError as exc:
            if auth and exc.code == 401:
                self.token = ""
            message = exc.read(4096).decode("utf-8", "replace")
            raise RuntimeError(f"VoHive {method} {path} HTTP {exc.code}: {message}") from exc

    def login(self) -> None:
        with self.lock:
            result = self._request(
                "POST", "/auth/login", {"username": self.username, "password": self.password}, auth=False
            )
            token = str(result.get("token") or "")
            if not token:
                raise RuntimeError("VoHive 登录失败")
            self.token = token

    def overview(self, device_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/devices/{urllib.parse.quote(device_id, safe='')}/overview")
        devices = result.get("devices") if isinstance(result, dict) else None
        if not isinstance(devices, list) or not devices:
            raise RuntimeError("VoHive 未返回设备概览")
        return dict(devices[0])

    def set_network(self, device_id: str, enabled: bool, cfg: dict[str, Any]) -> Any:
        body: dict[str, Any] = {"enabled": bool(enabled)}
        if enabled:
            body["ip_version"] = cfg.get("ip_version", "v4")
            body["apn"] = cfg.get("apn", "")
        return self._request(
            "PATCH", f"/devices/{urllib.parse.quote(device_id, safe='')}/network", body
        )

    def set_vowifi(self, device_id: str, enabled: bool) -> Any:
        return self._request(
            "PATCH", f"/devices/{urllib.parse.quote(device_id, safe='')}/vowifi", {"enabled": bool(enabled)}
        )

    def set_flight(self, device_id: str, enabled: bool) -> Any:
        return self._request(
            "PATCH", f"/devices/{urllib.parse.quote(device_id, safe='')}/flight-mode", {"enabled": bool(enabled)}
        )

    def put_policy(self, iccid: str, policy: dict[str, bool]) -> Any:
        return self._request(
            "PUT", f"/cards/{urllib.parse.quote(iccid, safe='')}/policy", policy
        )


def interface_counters(interface: str) -> tuple[int, int]:
    root = Path("/sys/class/net") / interface / "statistics"
    try:
        rx = int((root / "rx_bytes").read_text().strip())
        tx = int((root / "tx_bytes").read_text().strip())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取网卡 {interface} 流量计数") from exc
    return rx, tx


def counter_delta(before: tuple[int, int], after: tuple[int, int]) -> tuple[int, int, int]:
    rx = max(0, after[0] - before[0])
    tx = max(0, after[1] - before[1])
    return rx, tx, rx + tx


@dataclasses.dataclass
class FetchResult:
    status: int
    response_bytes: int
    source_address: str
    final_url: str


def _connect_bound(host: str, port: int, interface: str, timeout: int) -> socket.socket:
    errors: list[str] = []
    addresses = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    addresses.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
    for family, socktype, proto, _, address in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            if not hasattr(socket, "SO_BINDTODEVICE"):
                raise RuntimeError("系统不支持 SO_BINDTODEVICE")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
            sock.connect(address)
            return sock
        except Exception as exc:
            errors.append(type(exc).__name__)
            sock.close()
    raise RuntimeError("绑定蜂窝网卡连接失败: " + ",".join(errors[-3:]))


def bound_http_get(
    url: str,
    interface: str,
    timeout: int,
    max_response_bytes: int,
    max_redirects: int = 3,
) -> FetchResult:
    current = url
    context = ssl.create_default_context()
    for _ in range(max_redirects + 1):
        parsed = urllib.parse.urlsplit(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise RuntimeError("目标地址格式无效")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        raw_sock = _connect_bound(parsed.hostname, port, interface, timeout)
        source_address = str(raw_sock.getsockname()[0])
        sock: socket.socket
        try:
            sock = context.wrap_socket(raw_sock, server_hostname=parsed.hostname) if parsed.scheme == "https" else raw_sock
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            host_header = parsed.hostname
            if parsed.port and parsed.port not in (80, 443):
                host_header += f":{parsed.port}"
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "User-Agent: VoHive-Keepalive/1.0\r\n"
                "Accept: */*\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            received = bytearray()
            header_end = -1
            while len(received) < max_response_bytes:
                chunk = sock.recv(min(8192, max_response_bytes - len(received)))
                if not chunk:
                    break
                received.extend(chunk)
                if header_end < 0:
                    header_end = received.find(b"\r\n\r\n")
            if len(received) >= max_response_bytes:
                raise RuntimeError("响应超过配置上限")
        finally:
            with contextlib.suppress(Exception):
                sock.close() if "sock" in locals() else raw_sock.close()
        if header_end < 0:
            raise RuntimeError("目标未返回完整 HTTP 响应头")
        header = bytes(received[:header_end]).decode("iso-8859-1", "replace")
        lines = header.split("\r\n")
        parts = lines[0].split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise RuntimeError("目标返回无效 HTTP 状态行")
        status = int(parts[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            current = urllib.parse.urljoin(current, headers["location"])
            continue
        return FetchResult(status, len(received), source_address, current)
    raise RuntimeError("目标重定向次数过多")


class KeepAliveManager:
    def __init__(self, config: ConfigStore, db: Database):
        self.config_store = config
        self.db = db
        self.state_lock = threading.RLock()
        self.running = False
        self.current_run_id: int | None = None
        self.current_started_at: str | None = None
        self.stop_event = threading.Event()
        self.scheduler_thread = threading.Thread(target=self._scheduler, name="keepalive-scheduler", daemon=True)

    def start(self) -> None:
        cfg = self.config_store.load()
        if cfg["enabled"] and not self.db.get_meta("next_run_at"):
            self._schedule_after(days=cfg["interval_days"])
        if cfg.get("cleanup_on_start"):
            threading.Thread(target=self._startup_cleanup, name="keepalive-cleanup", daemon=True).start()
        self.scheduler_thread.start()

    def _startup_cleanup(self) -> None:
        time.sleep(2)
        try:
            self.restore_idle(self.config_store.load(), VoHiveClient())
            print("IDLE_POLICY_APPLIED", flush=True)
        except Exception as exc:
            print("IDLE_POLICY_ERROR " + type(exc).__name__, file=sys.stderr, flush=True)

    def _schedule_after(self, days: int | None = None, hours: int | None = None) -> str:
        delta = dt.timedelta(days=days or 0, hours=hours or 0)
        value = iso(now_utc() + delta)
        self.db.set_meta("next_run_at", value)
        return value

    def _scheduler(self) -> None:
        while not self.stop_event.wait(15):
            try:
                cfg = self.config_store.load()
                if not cfg["enabled"]:
                    continue
                due = parse_iso(self.db.get_meta("next_run_at"))
                if due is None:
                    self._schedule_after(days=cfg["interval_days"])
                elif due <= now_utc():
                    self.trigger("scheduled")
            except Exception as exc:
                print("SCHEDULER_ERROR " + type(exc).__name__, file=sys.stderr, flush=True)

    def on_config_updated(self, old: dict[str, Any], new: dict[str, Any]) -> None:
        if not new["enabled"]:
            self.db.set_meta("next_run_at", None)
        elif not old.get("enabled") or old.get("interval_days") != new.get("interval_days"):
            self._schedule_after(days=new["interval_days"])
        elif not self.db.get_meta("next_run_at"):
            self._schedule_after(days=new["interval_days"])

    def trigger(self, trigger: str = "manual") -> bool:
        with self.state_lock:
            if self.running:
                return False
            self.running = True
            self.current_started_at = iso()
            thread = threading.Thread(target=self._run, args=(trigger,), name="keepalive-run", daemon=True)
            thread.start()
            return True

    @staticmethod
    def _iccid_from_overview(overview: dict[str, Any]) -> str:
        modem = overview.get("modem") if isinstance(overview.get("modem"), dict) else {}
        value = modem.get("iccid") or overview.get("iccid")
        if not value:
            raise RuntimeError("当前设备未检测到 ICCID")
        return str(value)

    @staticmethod
    def _network_connected(overview: dict[str, Any]) -> bool:
        return bool(overview.get("network_connected"))

    def _wait_network(self, client: VoHiveClient, cfg: dict[str, Any]) -> str:
        deadline = time.monotonic() + cfg["network_connect_timeout_seconds"]
        while time.monotonic() < deadline:
            overview = client.overview(cfg["device_id"])
            if self._network_connected(overview):
                return iso()
            time.sleep(2)
        raise RuntimeError("等待蜂窝数据连接超时")

    def restore_idle(self, cfg: dict[str, Any], client: VoHiveClient) -> str:
        device = cfg["device_id"]
        errors: list[str] = []
        overview: dict[str, Any] = {}
        try:
            overview = client.overview(device)
        except Exception as exc:
            errors.append("overview:" + type(exc).__name__)
        iccid = ""
        with contextlib.suppress(Exception):
            iccid = self._iccid_from_overview(overview)

        def attempt(label: str, fn: Any) -> None:
            try:
                fn()
            except Exception as exc:
                errors.append(label + ":" + type(exc).__name__)

        attempt("network", lambda: client.set_network(device, False, cfg))
        mode = cfg.get("idle_mode", "cellular_sms")
        if mode == "cellular_sms":
            attempt("vowifi", lambda: client.set_vowifi(device, False))
            attempt("flight", lambda: client.set_flight(device, False))
            policy = {"network_enabled": False, "vowifi_enabled": False, "airplane_enabled": False}
        elif mode == "vowifi":
            attempt("flight", lambda: client.set_flight(device, True))
            attempt("vowifi", lambda: client.set_vowifi(device, True))
            policy = {"network_enabled": False, "vowifi_enabled": True, "airplane_enabled": True}
        else:
            attempt("vowifi", lambda: client.set_vowifi(device, False))
            attempt("flight", lambda: client.set_flight(device, True))
            policy = {"network_enabled": False, "vowifi_enabled": False, "airplane_enabled": True}
        if iccid:
            attempt("policy", lambda: client.put_policy(iccid, policy))
        return "ok" if not errors else "partial:" + ",".join(errors)

    def _run(self, trigger: str) -> None:
        cfg = self.config_store.load()
        run_id = self.db.start_run(trigger, cfg)
        with self.state_lock:
            self.current_run_id = run_id
        started_monotonic = time.monotonic()
        client = VoHiveClient()
        status = "failed"
        error = ""
        restore_status = "not_run"
        http_status: int | None = None
        connected_at: str | None = None
        session_before = (0, 0)
        session_after = (0, 0)
        request_before = (0, 0)
        request_after = (0, 0)
        detail: dict[str, Any] = {}
        watchdog_stop = threading.Event()
        cap_exceeded = threading.Event()

        def watchdog() -> None:
            while not watchdog_stop.wait(1):
                try:
                    current = interface_counters(cfg["interface"])
                    _, _, total = counter_delta(session_before, current)
                    if total > cfg["max_session_bytes"] or time.monotonic() - started_monotonic > cfg["max_session_seconds"]:
                        cap_exceeded.set()
                        with contextlib.suppress(Exception):
                            client.set_network(cfg["device_id"], False, cfg)
                        return
                except Exception:
                    return

        try:
            session_before = interface_counters(cfg["interface"])
            overview = client.overview(cfg["device_id"])
            self._iccid_from_overview(overview)

            client.set_network(cfg["device_id"], False, cfg)
            with contextlib.suppress(Exception):
                client.set_vowifi(cfg["device_id"], False)
            client.set_flight(cfg["device_id"], False)
            client.set_network(cfg["device_id"], True, cfg)

            threading.Thread(target=watchdog, name="keepalive-watchdog", daemon=True).start()
            connected_at = self._wait_network(client, cfg)
            if cap_exceeded.is_set():
                raise RuntimeError("连接阶段超过流量或时间上限")

            request_before = interface_counters(cfg["interface"])
            fetched = bound_http_get(
                cfg["target_url"], cfg["interface"], cfg["request_timeout_seconds"], cfg["max_response_bytes"]
            )
            request_after = interface_counters(cfg["interface"])
            http_status = fetched.status
            req_rx, req_tx, req_total = counter_delta(request_before, request_after)
            if not 200 <= fetched.status < 400:
                raise RuntimeError(f"目标返回 HTTP {fetched.status}")
            if req_total <= 0:
                raise RuntimeError("未检测到蜂窝网卡流量增长")
            if cap_exceeded.is_set():
                raise RuntimeError("执行超过流量或时间上限")
            detail = {
                "source_address": fetched.source_address,
                "final_url": fetched.final_url,
                "response_bytes": fetched.response_bytes,
                "bound_interface": cfg["interface"],
            }
            status = "success"
        except Exception as exc:
            error = str(exc)[:1000]
            detail["exception_type"] = type(exc).__name__
            print("KEEPALIVE_FAILED " + type(exc).__name__, file=sys.stderr, flush=True)
        finally:
            watchdog_stop.set()
            with contextlib.suppress(Exception):
                session_after = interface_counters(cfg["interface"])
            try:
                restore_status = self.restore_idle(cfg, client)
            except Exception as exc:
                restore_status = "failed:" + type(exc).__name__
            with contextlib.suppress(Exception):
                time.sleep(1)
                session_after = interface_counters(cfg["interface"])

            session_rx, session_tx, session_total = counter_delta(session_before, session_after)
            request_rx, request_tx, request_total = counter_delta(request_before, request_after)
            duration = round(time.monotonic() - started_monotonic, 3)
            self.db.finish_run(
                run_id,
                {
                    "finished_at": iso(),
                    "status": status,
                    "http_status": http_status,
                    "network_connected_at": connected_at,
                    "session_rx_bytes": session_rx,
                    "session_tx_bytes": session_tx,
                    "session_total_bytes": session_total,
                    "request_rx_bytes": request_rx,
                    "request_tx_bytes": request_tx,
                    "request_total_bytes": request_total,
                    "duration_seconds": duration,
                    "error": error or None,
                    "restore_status": restore_status,
                    "detail_json": json.dumps(detail, ensure_ascii=False),
                },
            )
            if status == "success":
                self._schedule_after(days=cfg["interval_days"])
                print(f"KEEPALIVE_SUCCESS run_id={run_id} bytes={session_total}", flush=True)
            else:
                self._schedule_after(hours=cfg["failure_retry_hours"])
            self._notify(cfg, status, session_total, error)
            with self.state_lock:
                self.running = False
                self.current_run_id = None
                self.current_started_at = None

    def _notify(self, cfg: dict[str, Any], status: str, total_bytes: int, error: str) -> None:
        if status == "success" and not cfg.get("notify_on_success"):
            return
        if status != "success" and not cfg.get("notify_on_failure"):
            return
        key = os.environ.get("PUSHDEER_KEY", "")
        endpoint = os.environ.get("PUSHDEER_ENDPOINT", "https://api2.pushdeer.com/message/push")
        if not key:
            return
        title = "VoHive 保号成功" if status == "success" else "VoHive 保号失败"
        description = (
            f"本次蜂窝流量：{total_bytes} bytes\n下次执行：{self.db.get_meta('next_run_at') or '-'}"
            if status == "success"
            else f"错误：{error[:300]}\n下次重试：{self.db.get_meta('next_run_at') or '-'}"
        )
        body = urllib.parse.urlencode(
            {"pushkey": key, "text": title, "desp": description, "type": "markdown"}
        ).encode()
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "vohive-keepalive/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read(65536)
        except Exception as exc:
            print("NOTIFY_ERROR " + type(exc).__name__, file=sys.stderr, flush=True)

    def status(self) -> dict[str, Any]:
        cfg = self.config_store.load()
        with self.state_lock:
            running = self.running
            current_run_id = self.current_run_id
            started = self.current_started_at
        history = self.db.history(1)
        last_success = self.db.last_success()
        return {
            "service": "保号",
            "enabled": cfg["enabled"],
            "running": running,
            "current_run_id": current_run_id,
            "current_started_at": started,
            "next_run_at": self.db.get_meta("next_run_at"),
            "last_success_at": last_success.get("finished_at") if last_success else None,
            "last_success_bytes": last_success.get("session_total_bytes") if last_success else None,
            "last_run": history[0] if history else None,
        }


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoHive · 保号</title>
<style>
:root{color-scheme:light dark;--bg:#0b1020;--card:#151c31;--line:#2a3555;--text:#e9eefc;--muted:#9aa8ca;--accent:#6f8cff;--good:#36d399;--bad:#fb7185}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#0b1020,#111a33);color:var(--text);font:14px/1.5 Inter,system-ui,sans-serif}
.wrap{max-width:1180px;margin:auto;padding:28px}.head{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.head h1{margin:0;font-size:28px}.head small{color:var(--muted)}.head-actions{display:flex;gap:10px}.linkbtn{border-radius:9px;padding:10px 16px;background:#293555;color:white;font-weight:700;text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:rgba(21,28,49,.94);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 16px 50px #0003}.metric b{display:block;font-size:18px;margin-top:6px}.muted{color:var(--muted)}
.section{margin-top:16px}.form{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.field label{display:block;color:var(--muted);margin-bottom:6px}.field input,.field select{width:100%;padding:10px 11px;border:1px solid var(--line);border-radius:9px;background:#0e1529;color:var(--text)}
.check{display:flex;gap:8px;align-items:center;padding-top:27px}.check input{width:auto}.actions{display:flex;gap:10px;margin-top:16px}button{border:0;border-radius:9px;padding:10px 16px;background:var(--accent);color:white;font-weight:700;cursor:pointer}button.secondary{background:#293555}button.danger{background:#d84a62}button:disabled{opacity:.5;cursor:not-allowed}
table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid var(--line);padding:10px 8px}th{color:var(--muted)}.ok{color:var(--good)}.fail{color:var(--bad)}.notice{margin-top:12px;color:var(--muted)}
@media(max-width:900px){.grid,.form{grid-template-columns:1fr 1fr}}@media(max-width:560px){.grid,.form{grid-template-columns:1fr}.wrap{padding:16px}}
</style></head><body><div class="wrap">
<div class="head"><div><h1>保号</h1><small>VoHive 蜂窝数据定时保活模块</small></div><div class="head-actions"><a id="back" class="linkbtn" href="#">返回 VoHive</a><button class="secondary" onclick="loadAll()">刷新</button></div></div>
<div class="grid">
 <div class="card metric"><span class="muted">服务状态</span><b id="state">-</b></div>
 <div class="card metric"><span class="muted">下次执行</span><b id="next">-</b></div>
 <div class="card metric"><span class="muted">上次成功</span><b id="last">-</b></div>
 <div class="card metric"><span class="muted">上次流量</span><b id="bytes">-</b></div>
</div>
<div class="card section"><h2>策略配置</h2><div class="form">
 <div class="field"><label>设备 ID</label><input id="device_id"></div>
 <div class="field"><label>蜂窝网卡</label><input id="interface"></div>
 <div class="field"><label>执行间隔（天，最大179）</label><input id="interval_days" type="number" min="1" max="179"></div>
 <div class="field"><label>验证网址</label><input id="target_url"></div>
 <div class="field"><label>连接超时（秒）</label><input id="network_connect_timeout_seconds" type="number"></div>
 <div class="field"><label>请求超时（秒）</label><input id="request_timeout_seconds" type="number"></div>
 <div class="field"><label>单次最长时间（秒）</label><input id="max_session_seconds" type="number"></div>
 <div class="field"><label>单次流量上限（KiB）</label><input id="max_session_kib" type="number"></div>
 <div class="field"><label>失败后重试（小时）</label><input id="failure_retry_hours" type="number"></div>
 <div class="field"><label>执行后空闲模式</label><select id="idle_mode"><option value="cellular_sms">蜂窝驻网接短信（推荐）</option><option value="vowifi">VoWiFi</option><option value="airplane">飞行模式</option></select></div>
 <div class="check"><input id="enabled" type="checkbox"><label for="enabled">启用定时保号</label></div>
 <div class="check"><input id="notify_on_success" type="checkbox"><label for="notify_on_success">成功时 PushDeer</label></div>
 <div class="check"><input id="notify_on_failure" type="checkbox"><label for="notify_on_failure">失败时 PushDeer</label></div>
</div><div class="actions"><button onclick="saveConfig()">保存配置</button><button id="run" class="danger" onclick="runNow()">立即保号</button></div>
<div class="notice">“立即保号”会真实打开蜂窝数据并产生少量资费；验证请求强制绑定配置的蜂窝网卡，不会被服务器宽带出口替代。</div></div>
<div class="card section"><h2>执行历史</h2><div style="overflow:auto"><table><thead><tr><th>开始时间</th><th>结果</th><th>HTTP</th><th>接收</th><th>发送</th><th>总流量</th><th>耗时</th><th>说明</th></tr></thead><tbody id="history"></tbody></table></div></div>
</div><script>
const ids=['device_id','interface','interval_days','target_url','network_connect_timeout_seconds','request_timeout_seconds','max_session_seconds','failure_retry_hours','idle_mode'];
const fmtTime=s=>s?new Date(s).toLocaleString():'-';const fmtBytes=n=>n==null?'-':n<1024?n+' B':n<1048576?(n/1024).toFixed(2)+' KiB':(n/1048576).toFixed(2)+' MiB';
async function api(path,opt={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});const j=await r.json();if(!r.ok)throw new Error(j.error||'请求失败');return j}
async function loadAll(){try{const [c,s,h]=await Promise.all([api('/api/config'),api('/api/status'),api('/api/history?limit=50')]);ids.forEach(k=>document.getElementById(k).value=c[k]);document.getElementById('max_session_kib').value=Math.round(c.max_session_bytes/1024);['enabled','notify_on_success','notify_on_failure'].forEach(k=>document.getElementById(k).checked=!!c[k]);document.getElementById('state').textContent=s.running?'执行中':(s.enabled?'已启用':'已停用');document.getElementById('state').className=s.running?'':'ok';document.getElementById('next').textContent=fmtTime(s.next_run_at);document.getElementById('last').textContent=fmtTime(s.last_success_at);document.getElementById('bytes').textContent=fmtBytes(s.last_success_bytes);document.getElementById('run').disabled=s.running;document.getElementById('history').innerHTML=h.items.map(x=>`<tr><td>${fmtTime(x.started_at)}</td><td class="${x.status==='success'?'ok':'fail'}">${x.status}</td><td>${x.http_status??'-'}</td><td>${fmtBytes(x.session_rx_bytes)}</td><td>${fmtBytes(x.session_tx_bytes)}</td><td>${fmtBytes(x.session_total_bytes)}</td><td>${x.duration_seconds??'-'}s</td><td>${x.error||x.restore_status||''}</td></tr>`).join('')||'<tr><td colspan="8" class="muted">暂无执行记录</td></tr>'}catch(e){alert(e.message)}}
async function saveConfig(){try{const c={};ids.forEach(k=>c[k]=document.getElementById(k).value);['interval_days','network_connect_timeout_seconds','request_timeout_seconds','max_session_seconds','failure_retry_hours'].forEach(k=>c[k]=Number(c[k]));c.max_session_bytes=Number(document.getElementById('max_session_kib').value)*1024;['enabled','notify_on_success','notify_on_failure'].forEach(k=>c[k]=document.getElementById(k).checked);await api('/api/config',{method:'PUT',body:JSON.stringify(c)});alert('配置已保存');loadAll()}catch(e){alert(e.message)}}
async function runNow(){if(!confirm('这会真实使用少量蜂窝流量，确定立即执行？'))return;try{await api('/api/run',{method:'POST',body:JSON.stringify({confirm:true})});alert('已开始执行');loadAll()}catch(e){alert(e.message)}}
document.getElementById('back').href=location.protocol+'//'+location.hostname+':7575/';loadAll();setInterval(()=>api('/api/status').then(s=>{if(s.running)loadAll()}),5000);
</script></body></html>'''


class ApiHandler(BaseHTTPRequestHandler):
    manager: KeepAliveManager
    config_store: ConfigStore
    db: Database
    basic_user: str
    basic_password: str

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        if self.path == "/health":
            return True
        expected = base64.b64encode(f"{self.basic_user}:{self.basic_password}".encode()).decode()
        return self.headers.get("Authorization", "") == "Basic " + expected

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="VoHive Keepalive"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, status: int, value: Any, content_type: str = "application/json; charset=utf-8") -> None:
        data = value.encode("utf-8") if isinstance(value, str) else json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 1024 * 1024:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("请求体必须是对象")
        return value

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        path = urllib.parse.urlsplit(self.path)
        try:
            if path.path == "/":
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif path.path == "/health":
                self._send(200, {"ok": True, "service": "保号"})
            elif path.path == "/api/config":
                self._send(200, self.config_store.load())
            elif path.path == "/api/status":
                self._send(200, self.manager.status())
            elif path.path == "/api/history":
                query = urllib.parse.parse_qs(path.query)
                limit = clamp_int((query.get("limit") or ["50"])[0], 1, 500, "limit")
                self._send(200, {"items": self.db.history(limit)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(400, {"error": str(exc)})

    def do_PUT(self) -> None:
        if not self._require_auth():
            return
        if urllib.parse.urlsplit(self.path).path != "/api/config":
            self._send(404, {"error": "not found"})
            return
        try:
            old = self.config_store.load()
            incoming = self._body()
            merged = dict(old)
            merged.update(incoming)
            saved = self.config_store.save(merged)
            self.manager.on_config_updated(old, saved)
            self._send(200, saved)
        except Exception as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        if urllib.parse.urlsplit(self.path).path != "/api/run":
            self._send(404, {"error": "not found"})
            return
        try:
            body = self._body()
            if body.get("confirm") is not True:
                raise ValueError("必须明确确认会使用蜂窝流量")
            if not self.manager.trigger("manual"):
                self._send(409, {"error": "已有保号任务正在执行"})
                return
            self._send(202, {"ok": True, "message": "保号任务已开始"})
        except Exception as exc:
            self._send(400, {"error": str(exc)})


def main() -> None:
    config_path = os.environ.get("CONFIG_PATH", "/etc/vohive-keepalive/config.json")
    db_path = os.environ.get("DATABASE_PATH", "/var/lib/vohive-keepalive/keepalive.db")
    config = ConfigStore(config_path)
    db = Database(db_path)
    manager = KeepAliveManager(config, db)
    ApiHandler.manager = manager
    ApiHandler.config_store = config
    ApiHandler.db = db
    ApiHandler.basic_user = os.environ.get("BASIC_USER", "admin")
    ApiHandler.basic_password = os.environ.get("BASIC_PASSWORD", "")
    if not ApiHandler.basic_password:
        raise RuntimeError("必须通过 BASIC_PASSWORD 设置保号管理 API 密码")
    host = os.environ.get("LISTEN_HOST", "127.0.0.1")
    port = int(os.environ.get("LISTEN_PORT", "7582"))
    manager.start()
    print(f"KEEPALIVE_READY {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), ApiHandler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        raise
