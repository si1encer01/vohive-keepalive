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
import subprocess
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
    "profile_management_enabled": False,
    "lpac_path": "/usr/local/bin/lpac-at",
    "lpac_at_device": "/dev/ttyUSB2",
    "profile_switch_timeout_seconds": 120,
    "profile_discovery_interval_seconds": 300,
    "restore_profile_iccid": "",
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
    for key in (
        "notify_on_success", "notify_on_failure", "cleanup_on_start", "profile_management_enabled"
    ):
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
    merged["profile_switch_timeout_seconds"] = clamp_int(
        merged["profile_switch_timeout_seconds"], 30, 300, "profile_switch_timeout_seconds"
    )
    merged["profile_discovery_interval_seconds"] = clamp_int(
        merged["profile_discovery_interval_seconds"], 60, 86400, "profile_discovery_interval_seconds"
    )
    if merged.get("ip_version") not in ("v4", "v6", "v4v6"):
        raise ValueError("ip_version 仅支持 v4、v6 或 v4v6")
    merged["apn"] = str(merged.get("apn", "")).strip()[:128]
    if merged.get("idle_mode") not in ("cellular_sms", "vowifi", "airplane"):
        raise ValueError("idle_mode 无效")
    lpac_path = str(merged.get("lpac_path", "")).strip()
    if not lpac_path.startswith("/") or not re.fullmatch(r"[A-Za-z0-9_./-]{2,240}", lpac_path):
        raise ValueError("lpac_path 必须是安全的绝对路径")
    at_device = str(merged.get("lpac_at_device", "")).strip()
    if not re.fullmatch(r"/dev/[A-Za-z0-9_.-]{1,80}", at_device):
        raise ValueError("lpac_at_device 格式无效")
    restore_iccid = str(merged.get("restore_profile_iccid", "")).strip()
    if restore_iccid and not re.fullmatch(r"[0-9]{10,24}", restore_iccid):
        raise ValueError("restore_profile_iccid 格式无效")
    merged["lpac_path"] = lpac_path
    merged["lpac_at_device"] = at_device
    merged["restore_profile_iccid"] = restore_iccid
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

    @contextlib.contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

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
                CREATE TABLE IF NOT EXISTS managed_profiles (
                    iccid TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    provider TEXT,
                    profile_name TEXT,
                    profile_state TEXT NOT NULL DEFAULT 'unknown',
                    keepalive_enabled INTEGER NOT NULL DEFAULT 1,
                    interval_days INTEGER NOT NULL DEFAULT 120,
                    next_run_at TEXT,
                    last_success_at TEXT,
                    last_success_bytes INTEGER,
                    last_error TEXT,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_managed_profiles_due
                    ON managed_profiles(keepalive_enabled, next_run_at);
                """
            )
            columns = {str(row[1]) for row in con.execute("PRAGMA table_info(runs)")}
            if "target_iccid" not in columns:
                con.execute("ALTER TABLE runs ADD COLUMN target_iccid TEXT")
            if "profile_label" not in columns:
                con.execute("ALTER TABLE runs ADD COLUMN profile_label TEXT")
            con.execute(
                "UPDATE runs SET status='failed', finished_at=?, error=COALESCE(error, '服务重启导致执行中断') "
                "WHERE status='running'",
                (iso(),),
            )

    def start_run(
        self, trigger: str, cfg: dict[str, Any], target_iccid: str = "", profile_label: str = ""
    ) -> int:
        with self.lock, self.connect() as con:
            cur = con.execute(
                "INSERT INTO runs(started_at, trigger, status, device_id, interface, target_url,"
                "target_iccid,profile_label) VALUES(?,?,?,?,?,?,?,?)",
                (
                    iso(), trigger, "running", cfg["device_id"], cfg["interface"], cfg["target_url"],
                    target_iccid or None, profile_label or None,
                ),
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
                "SELECT id,started_at,finished_at,trigger,status,device_id,interface,target_url,"
                "target_iccid,profile_label,http_status,"
                "network_connected_at,session_rx_bytes,session_tx_bytes,session_total_bytes,"
                "request_rx_bytes,request_tx_bytes,request_total_bytes,duration_seconds,error,restore_status "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_success(self, target_iccid: str = "") -> dict[str, Any] | None:
        with self.lock, self.connect() as con:
            if target_iccid:
                row = con.execute(
                    "SELECT * FROM runs WHERE status='success' AND target_iccid=? ORDER BY id DESC LIMIT 1",
                    (target_iccid,),
                ).fetchone()
            else:
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

    def sync_profiles(
        self,
        profiles: list[dict[str, Any]],
        interval_days: int,
        initial_next_run_at: str,
        active_initial_next_run_at: str | None = None,
    ) -> None:
        seen_at = iso()
        seen_iccids: set[str] = set()
        with self.lock, self.connect() as con:
            for profile in profiles:
                iccid = str(profile["iccid"])
                seen_iccids.add(iccid)
                label = str(
                    profile.get("profileNickname")
                    or profile.get("profileName")
                    or profile.get("serviceProviderName")
                    or "eSIM"
                )[:120]
                con.execute(
                    """
                    INSERT INTO managed_profiles(
                        iccid,label,provider,profile_name,profile_state,interval_days,next_run_at,
                        last_seen_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(iccid) DO UPDATE SET
                        label=CASE WHEN managed_profiles.label='' THEN excluded.label ELSE managed_profiles.label END,
                        provider=excluded.provider,
                        profile_name=excluded.profile_name,
                        profile_state=excluded.profile_state,
                        last_seen_at=excluded.last_seen_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        iccid,
                        label,
                        str(profile.get("serviceProviderName") or "")[:120],
                        str(profile.get("profileName") or "")[:120],
                        str(profile.get("profileState") or "unknown")[:32],
                        interval_days,
                        (
                            active_initial_next_run_at
                            if active_initial_next_run_at
                            and str(profile.get("profileState") or "").lower() == "enabled"
                            else initial_next_run_at
                        ),
                        seen_at,
                        seen_at,
                    ),
                )
            rows = con.execute("SELECT iccid FROM managed_profiles").fetchall()
            for row in rows:
                if str(row["iccid"]) not in seen_iccids:
                    con.execute(
                        "UPDATE managed_profiles SET profile_state='missing',updated_at=? WHERE iccid=?",
                        (seen_at, str(row["iccid"])),
                    )

    def profiles(self) -> list[dict[str, Any]]:
        with self.lock, self.connect() as con:
            rows = con.execute(
                "SELECT * FROM managed_profiles ORDER BY CASE WHEN profile_state='enabled' THEN 0 ELSE 1 END, label, iccid"
            ).fetchall()
        return [dict(row) for row in rows]

    def profile(self, iccid: str) -> dict[str, Any] | None:
        with self.lock, self.connect() as con:
            row = con.execute("SELECT * FROM managed_profiles WHERE iccid=?", (iccid,)).fetchone()
        return dict(row) if row else None

    def backfill_single_profile_legacy_success(self, iccid: str) -> None:
        """Attribute a pre-multi-profile success when only one profile exists."""
        with self.lock, self.connect() as con:
            current = con.execute(
                "SELECT last_success_at FROM managed_profiles WHERE iccid=?", (iccid,)
            ).fetchone()
            if current is None or current["last_success_at"]:
                return
            legacy = con.execute(
                "SELECT finished_at,session_total_bytes FROM runs "
                "WHERE status='success' AND target_iccid IS NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if legacy:
                con.execute(
                    "UPDATE managed_profiles SET last_success_at=?,last_success_bytes=?,updated_at=? "
                    "WHERE iccid=?",
                    (legacy["finished_at"], legacy["session_total_bytes"], iso(), iccid),
                )

    def next_due_profile(self) -> dict[str, Any] | None:
        with self.lock, self.connect() as con:
            row = con.execute(
                "SELECT * FROM managed_profiles WHERE keepalive_enabled=1 "
                "AND profile_state!='missing' AND next_run_at IS NOT NULL "
                "ORDER BY next_run_at ASC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def update_profile_policy(
        self,
        iccid: str,
        *,
        label: str,
        keepalive_enabled: bool,
        interval_days: int,
    ) -> dict[str, Any]:
        """Update user-owned per-profile policy without touching the eUICC itself."""
        next_value = iso(now_utc() + dt.timedelta(days=interval_days))
        with self.lock, self.connect() as con:
            row = con.execute(
                "SELECT keepalive_enabled,interval_days,next_run_at FROM managed_profiles WHERE iccid=?",
                (iccid,),
            ).fetchone()
            if row is None:
                raise ValueError("eSIM 配置文件不存在")
            should_reschedule = (
                int(row["interval_days"]) != interval_days
                or (not bool(row["keepalive_enabled"]) and keepalive_enabled)
            )
            next_run_at = next_value if should_reschedule else row["next_run_at"]
            con.execute(
                "UPDATE managed_profiles SET label=?,keepalive_enabled=?,interval_days=?,"
                "next_run_at=?,updated_at=? WHERE iccid=?",
                (label, 1 if keepalive_enabled else 0, interval_days, next_run_at, iso(), iccid),
            )
        updated = self.profile(iccid)
        if updated is None:  # pragma: no cover - guarded by the same database transaction
            raise RuntimeError("无法读取更新后的 eSIM 配置")
        return updated

    def schedule_profile(
        self,
        iccid: str,
        next_run_at: str,
        *,
        success_at: str | None = None,
        success_bytes: int | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock, self.connect() as con:
            if success_at is not None:
                con.execute(
                    "UPDATE managed_profiles SET next_run_at=?,last_success_at=?,last_success_bytes=?,"
                    "last_error=NULL,updated_at=? WHERE iccid=?",
                    (next_run_at, success_at, success_bytes, iso(), iccid),
                )
            else:
                con.execute(
                    "UPDATE managed_profiles SET next_run_at=?,last_error=?,updated_at=? WHERE iccid=?",
                    (next_run_at, (error or "")[:1000] or None, iso(), iccid),
                )

    def reschedule_profiles(self, interval_days: int) -> None:
        next_value = iso(now_utc() + dt.timedelta(days=interval_days))
        with self.lock, self.connect() as con:
            con.execute(
                "UPDATE managed_profiles SET interval_days=?,next_run_at=?,updated_at=?",
                (interval_days, next_value, iso()),
            )

    def resume_profiles(self) -> None:
        with self.lock, self.connect() as con:
            rows = con.execute(
                "SELECT iccid,interval_days FROM managed_profiles WHERE keepalive_enabled=1"
            ).fetchall()
            for row in rows:
                con.execute(
                    "UPDATE managed_profiles SET next_run_at=?,updated_at=? WHERE iccid=?",
                    (
                        iso(now_utc() + dt.timedelta(days=int(row["interval_days"]))),
                        iso(),
                        str(row["iccid"]),
                    ),
                )


class ProfileManager:
    """Read and enable eUICC profiles through lpac's standards-based AT backend.

    This class intentionally exposes no disable, delete, reset, or download operation.
    The keepalive service may only list existing profiles and enable a known ICCID.
    """

    @staticmethod
    def _environment(cfg: dict[str, Any]) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "LPAC_APDU": "at",
            "LPAC_APDU_AT_DEVICE": cfg["lpac_at_device"],
        })
        return env

    def _run(self, cfg: dict[str, Any], args: list[str], timeout: int) -> dict[str, Any]:
        path = Path(cfg["lpac_path"])
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"lpac 不可执行: {path}")
        try:
            completed = subprocess.run(
                [str(path), *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
                env=self._environment(cfg),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("lpac 操作超时") from exc
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "lpac 执行失败").strip()
            raise RuntimeError(message[-500:])
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("lpac 返回了无效 JSON") from exc
        payload = result.get("payload") if isinstance(result, dict) else None
        if not isinstance(payload, dict) or payload.get("code") not in (0, "0"):
            message = str(payload.get("message") if isinstance(payload, dict) else "lpac 返回错误")
            raise RuntimeError(message[:500])
        return payload

    def list_profiles(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        payload = self._run(cfg, ["profile", "list"], timeout=30)
        raw_items = payload.get("data") or []
        if not isinstance(raw_items, list):
            raise RuntimeError("lpac 配置文件列表格式无效")
        profiles: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            iccid = str(raw.get("iccid") or "").strip()
            if not re.fullmatch(r"[0-9]{10,24}", iccid):
                continue
            item = dict(raw)
            item["iccid"] = iccid
            item["profileState"] = str(item.get("profileState") or "unknown").lower()
            profiles.append(item)
        if not profiles:
            raise RuntimeError("eUICC 中未发现可管理的配置文件")
        return profiles

    def enable_profile(self, cfg: dict[str, Any], iccid: str) -> None:
        if not re.fullmatch(r"[0-9]{10,24}", iccid):
            raise ValueError("目标 ICCID 格式无效")
        profiles = self.list_profiles(cfg)
        target = next((item for item in profiles if item["iccid"] == iccid), None)
        if target is None:
            raise RuntimeError("目标配置文件不存在于当前 eUICC")
        if target.get("profileState") == "enabled":
            return
        self._run(
            cfg,
            ["profile", "enable", iccid, "1"],
            timeout=cfg["profile_switch_timeout_seconds"],
        )
        deadline = time.monotonic() + cfg["profile_switch_timeout_seconds"]
        last_error = ""
        while time.monotonic() < deadline:
            try:
                current = self.list_profiles(cfg)
                if any(
                    item["iccid"] == iccid and item.get("profileState") == "enabled"
                    for item in current
                ):
                    return
            except Exception as exc:
                last_error = type(exc).__name__
            time.sleep(2)
        raise RuntimeError("等待 eUICC 配置切换超时" + (("：" + last_error) if last_error else ""))


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
        self.profile_manager = ProfileManager()
        self.state_lock = threading.RLock()
        self.profile_refresh_lock = threading.RLock()
        self.running = False
        self.current_run_id: int | None = None
        self.current_started_at: str | None = None
        self.current_target_iccid: str | None = None
        self.current_profile_label: str | None = None
        self.last_profile_refresh_monotonic = 0.0
        self.stop_event = threading.Event()
        self.scheduler_thread = threading.Thread(target=self._scheduler, name="keepalive-scheduler", daemon=True)

    @staticmethod
    def mask_iccid(iccid: str) -> str:
        return iccid[:6] + "…" + iccid[-4:] if len(iccid) > 10 else "••••"

    def start(self) -> None:
        cfg = self.config_store.load()
        if cfg["enabled"] and not self.db.get_meta("next_run_at"):
            self._schedule_after(days=cfg["interval_days"])
        if cfg.get("profile_management_enabled"):
            threading.Thread(
                target=self._startup_profile_discovery, name="profile-discovery", daemon=True
            ).start()
        if cfg.get("cleanup_on_start"):
            threading.Thread(target=self._startup_cleanup, name="keepalive-cleanup", daemon=True).start()
        self.scheduler_thread.start()

    def _startup_profile_discovery(self) -> None:
        time.sleep(1)
        try:
            profiles = self.refresh_profiles(force=True)
            print(f"PROFILES_DISCOVERED count={len(profiles)}", flush=True)
        except Exception as exc:
            print("PROFILE_DISCOVERY_ERROR " + type(exc).__name__, file=sys.stderr, flush=True)

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

    def refresh_profiles(
        self, cfg: dict[str, Any] | None = None, *, force: bool = False
    ) -> list[dict[str, Any]]:
        cfg = cfg or self.config_store.load()
        if not cfg.get("profile_management_enabled"):
            return self.db.profiles()
        with self.profile_refresh_lock:
            age = time.monotonic() - self.last_profile_refresh_monotonic
            if not force and age < cfg["profile_discovery_interval_seconds"]:
                return self.db.profiles()
            discovered = self.profile_manager.list_profiles(cfg)
            self._sync_discovered_profiles(cfg, discovered)
            self.last_profile_refresh_monotonic = time.monotonic()
            return self.db.profiles()

    def _sync_discovered_profiles(
        self, cfg: dict[str, Any], discovered: list[dict[str, Any]]
    ) -> None:
        had_managed_profiles = bool(self.db.profiles())
        new_profile_next = iso(now_utc() + dt.timedelta(days=cfg["interval_days"]))
        active_initial_next = (
            new_profile_next
            if had_managed_profiles
            else (self.db.get_meta("next_run_at") or new_profile_next)
        )
        self.db.sync_profiles(
            discovered,
            cfg["interval_days"],
            new_profile_next,
            active_initial_next_run_at=active_initial_next,
        )
        if len(discovered) == 1:
            self.db.backfill_single_profile_legacy_success(str(discovered[0]["iccid"]))

    def profiles(self, force: bool = False) -> list[dict[str, Any]]:
        cfg = self.config_store.load()
        items = self.refresh_profiles(cfg, force=force) if cfg.get("profile_management_enabled") else self.db.profiles()
        result = []
        for item in items:
            copy = dict(item)
            copy["masked_iccid"] = self.mask_iccid(str(copy.get("iccid") or ""))
            copy["active"] = copy.get("profile_state") == "enabled"
            result.append(copy)
        return result

    def update_profile_policy(self, iccid: str, incoming: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]{10,24}", iccid):
            raise ValueError("ICCID 格式无效")
        current = self.db.profile(iccid)
        if current is None:
            raise ValueError("eSIM 配置文件不存在")
        label = str(incoming.get("label", current.get("label") or "eSIM")).strip()
        if not label or len(label) > 120 or any(ord(char) < 32 for char in label):
            raise ValueError("号码备注必须为 1 到 120 个可见字符")
        enabled_value = incoming.get("keepalive_enabled", bool(current.get("keepalive_enabled")))
        if not isinstance(enabled_value, bool):
            raise ValueError("keepalive_enabled 必须是布尔值")
        interval_days = clamp_int(
            incoming.get("interval_days", current.get("interval_days", 120)),
            1,
            179,
            "interval_days",
        )
        self.db.update_profile_policy(
            iccid,
            label=label,
            keepalive_enabled=enabled_value,
            interval_days=interval_days,
        )
        return next(item for item in self.profiles(force=False) if item["iccid"] == iccid)

    def _scheduler(self) -> None:
        while not self.stop_event.wait(15):
            try:
                cfg = self.config_store.load()
                if not cfg["enabled"]:
                    continue
                if cfg.get("profile_management_enabled"):
                    self.refresh_profiles(cfg)
                    profile = self.db.next_due_profile()
                    due = parse_iso(str(profile.get("next_run_at") or "")) if profile else None
                    if profile and due is not None and due <= now_utc():
                        self.trigger("scheduled", str(profile["iccid"]))
                else:
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
            return
        if not old.get("enabled") and new["enabled"]:
            self.db.resume_profiles()
        if old.get("interval_days") != new.get("interval_days"):
            self._schedule_after(days=new["interval_days"])
            self.db.reschedule_profiles(new["interval_days"])
        elif not self.db.get_meta("next_run_at"):
            self._schedule_after(days=new["interval_days"])
        if new.get("profile_management_enabled"):
            self.last_profile_refresh_monotonic = 0.0
            threading.Thread(
                target=self._startup_profile_discovery, name="profile-discovery-update", daemon=True
            ).start()

    def trigger(self, trigger: str = "manual", target_iccid: str = "") -> bool:
        cfg = self.config_store.load()
        if cfg.get("profile_management_enabled"):
            self.refresh_profiles(cfg, force=bool(target_iccid))
            if not target_iccid:
                active = next(
                    (item for item in self.db.profiles() if item.get("profile_state") == "enabled"), None
                )
                if active:
                    target_iccid = str(active["iccid"])
            selected = self.db.profile(target_iccid) if target_iccid else None
            if not target_iccid or selected is None or selected.get("profile_state") == "missing":
                raise ValueError("请选择有效的 eSIM 配置文件")
        profile = self.db.profile(target_iccid) if target_iccid else None
        with self.state_lock:
            if self.running:
                return False
            self.running = True
            self.current_started_at = iso()
            self.current_target_iccid = target_iccid or None
            self.current_profile_label = str(profile.get("label") or "eSIM") if profile else None
            thread = threading.Thread(
                target=self._run,
                args=(trigger, target_iccid),
                name="keepalive-run",
                daemon=True,
            )
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

    def _wait_profile(self, client: VoHiveClient, cfg: dict[str, Any], iccid: str) -> str:
        deadline = time.monotonic() + cfg["profile_switch_timeout_seconds"]
        while time.monotonic() < deadline:
            try:
                overview = client.overview(cfg["device_id"])
                if self._iccid_from_overview(overview) == iccid:
                    return iso()
            except Exception:
                pass
            time.sleep(2)
        raise RuntimeError("VoHive 未在超时时间内识别切换后的 eSIM 配置")

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

    def _run(self, trigger: str, target_iccid: str = "") -> None:
        cfg = self.config_store.load()
        profile = self.db.profile(target_iccid) if target_iccid else None
        profile_label = str(profile.get("label") or "eSIM") if profile else ""
        run_id = self.db.start_run(trigger, cfg, target_iccid, profile_label)
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
        counters_started = False
        active_before = ""
        restore_target = ""
        next_run_at = ""
        session_started_monotonic = time.monotonic()

        def watchdog() -> None:
            while not watchdog_stop.wait(1):
                try:
                    current = interface_counters(cfg["interface"])
                    _, _, total = counter_delta(session_before, current)
                    if total > cfg["max_session_bytes"] or time.monotonic() - session_started_monotonic > cfg["max_session_seconds"]:
                        cap_exceeded.set()
                        with contextlib.suppress(Exception):
                            client.set_network(cfg["device_id"], False, cfg)
                        return
                except Exception:
                    return

        try:
            if cfg.get("profile_management_enabled"):
                discovered = self.profile_manager.list_profiles(cfg)
                self._sync_discovered_profiles(cfg, discovered)
                active = next((item for item in discovered if item.get("profileState") == "enabled"), None)
                active_before = str(active.get("iccid") or "") if active else ""
                restore_target = cfg.get("restore_profile_iccid") or active_before
                if not target_iccid:
                    raise RuntimeError("多配置文件模式缺少目标 ICCID")
                client.set_network(cfg["device_id"], False, cfg)
                if target_iccid != active_before:
                    self.profile_manager.enable_profile(cfg, target_iccid)
                    self._wait_profile(client, cfg, target_iccid)

            overview = client.overview(cfg["device_id"])
            actual_iccid = self._iccid_from_overview(overview)
            if target_iccid and actual_iccid != target_iccid:
                raise RuntimeError("当前启用的 eSIM 配置与保号目标不一致")

            session_before = interface_counters(cfg["interface"])
            counters_started = True
            session_started_monotonic = time.monotonic()

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
            if counters_started:
                with contextlib.suppress(Exception):
                    session_after = interface_counters(cfg["interface"])
            restore_parts: list[str] = []
            try:
                restore_parts.append(self.restore_idle(cfg, client))
            except Exception as exc:
                restore_parts.append("idle-failed:" + type(exc).__name__)
            if (
                cfg.get("profile_management_enabled")
                and restore_target
                and restore_target != target_iccid
            ):
                try:
                    self.profile_manager.enable_profile(cfg, restore_target)
                    self._wait_profile(client, cfg, restore_target)
                    restore_parts.append("profile-restored")
                    restore_parts.append(self.restore_idle(cfg, client))
                except Exception as exc:
                    restore_parts.append("profile-restore-failed:" + type(exc).__name__)
            restore_status = ",".join(restore_parts) or "not_run"
            if counters_started:
                with contextlib.suppress(Exception):
                    time.sleep(1)
                    session_after = interface_counters(cfg["interface"])

            session_rx, session_tx, session_total = (
                counter_delta(session_before, session_after) if counters_started else (0, 0, 0)
            )
            request_rx, request_tx, request_total = (
                counter_delta(request_before, request_after) if request_before != (0, 0) else (0, 0, 0)
            )
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
                profile_interval = cfg["interval_days"]
                if target_iccid and cfg.get("profile_management_enabled"):
                    current_profile = self.db.profile(target_iccid)
                    if current_profile:
                        profile_interval = int(current_profile.get("interval_days") or profile_interval)
                next_run_at = iso(now_utc() + dt.timedelta(days=profile_interval))
                if target_iccid and cfg.get("profile_management_enabled"):
                    self.db.schedule_profile(
                        target_iccid,
                        next_run_at,
                        success_at=iso(),
                        success_bytes=session_total,
                    )
                else:
                    self.db.set_meta("next_run_at", next_run_at)
                print(f"KEEPALIVE_SUCCESS run_id={run_id} bytes={session_total}", flush=True)
            else:
                next_run_at = iso(now_utc() + dt.timedelta(hours=cfg["failure_retry_hours"]))
                if target_iccid and cfg.get("profile_management_enabled"):
                    self.db.schedule_profile(target_iccid, next_run_at, error=error)
                else:
                    self.db.set_meta("next_run_at", next_run_at)
            if cfg.get("profile_management_enabled"):
                with contextlib.suppress(Exception):
                    self.refresh_profiles(cfg, force=True)
            self._notify(
                cfg, status, session_total, error, profile_label, target_iccid, next_run_at, restore_status
            )
            with self.state_lock:
                self.running = False
                self.current_run_id = None
                self.current_started_at = None
                self.current_target_iccid = None
                self.current_profile_label = None

    def _notify(
        self,
        cfg: dict[str, Any],
        status: str,
        total_bytes: int,
        error: str,
        profile_label: str = "",
        target_iccid: str = "",
        next_run_at: str = "",
        restore_status: str = "",
    ) -> None:
        if status == "success" and not cfg.get("notify_on_success"):
            return
        if status != "success" and not cfg.get("notify_on_failure"):
            return
        key = os.environ.get("PUSHDEER_KEY", "")
        endpoint = os.environ.get("PUSHDEER_ENDPOINT", "https://api2.pushdeer.com/message/push")
        if not key:
            return
        suffix = (" · " + profile_label) if profile_label else ""
        title = ("VoHive 保号成功" if status == "success" else "VoHive 保号失败") + suffix
        profile_line = (
            f"配置：{profile_label or 'eSIM'}（{self.mask_iccid(target_iccid)}）\n"
            if target_iccid else ""
        )
        description = (
            f"{profile_line}本次蜂窝流量：{total_bytes} bytes\n下次执行：{next_run_at or self.db.get_meta('next_run_at') or '-'}"
            if status == "success"
            else f"{profile_line}错误：{error[:300]}\n下次重试：{next_run_at or self.db.get_meta('next_run_at') or '-'}"
        )
        if restore_status and "failed" in restore_status:
            description += "\n恢复警告：" + restore_status[:200]
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
            current_target_iccid = self.current_target_iccid
            current_profile_label = self.current_profile_label
        history = self.db.history(1)
        last_success = self.db.last_success()
        managed_profiles = self.profiles(force=False) if cfg.get("profile_management_enabled") else []
        due_profile = self.db.next_due_profile() if cfg.get("profile_management_enabled") else None
        return {
            "service": "保号",
            "enabled": cfg["enabled"],
            "running": running,
            "current_run_id": current_run_id,
            "current_started_at": started,
            "next_run_at": (
                due_profile.get("next_run_at")
                if cfg.get("profile_management_enabled") and due_profile
                else (None if cfg.get("profile_management_enabled") else self.db.get_meta("next_run_at"))
            ),
            "last_success_at": last_success.get("finished_at") if last_success else None,
            "last_success_bytes": last_success.get("session_total_bytes") if last_success else None,
            "last_run": history[0] if history else None,
            "profile_management_enabled": cfg.get("profile_management_enabled", False),
            "profile_count": len(managed_profiles),
            "profiles": managed_profiles,
            "current_target_iccid": current_target_iccid,
            "current_profile_label": current_profile_label,
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
.inline{width:120px;padding:7px 8px;border:1px solid var(--line);border-radius:7px;background:#0e1529;color:var(--text)}.narrow{width:72px}.row-actions{display:flex;gap:7px;min-width:190px}.row-actions button{padding:7px 10px}
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
 <div class="field"><label>默认间隔（天，修改应用到全部号码）</label><input id="interval_days" type="number" min="1" max="179"></div>
 <div class="field"><label>验证网址</label><input id="target_url"></div>
 <div class="field"><label>连接超时（秒）</label><input id="network_connect_timeout_seconds" type="number"></div>
 <div class="field"><label>请求超时（秒）</label><input id="request_timeout_seconds" type="number"></div>
 <div class="field"><label>单次最长时间（秒）</label><input id="max_session_seconds" type="number"></div>
 <div class="field"><label>单次流量上限（KiB）</label><input id="max_session_kib" type="number"></div>
 <div class="field"><label>失败后重试（小时）</label><input id="failure_retry_hours" type="number"></div>
 <div class="field"><label>执行后空闲模式</label><select id="idle_mode"><option value="cellular_sms">蜂窝驻网接短信（推荐）</option><option value="vowifi">VoWiFi</option><option value="airplane">飞行模式</option></select></div>
 <div class="field"><label>lpac 路径</label><input id="lpac_path"></div>
 <div class="field"><label>eUICC AT 端口</label><input id="lpac_at_device"></div>
 <div class="field"><label>配置切换超时（秒）</label><input id="profile_switch_timeout_seconds" type="number" min="30" max="300"></div>
 <div class="field"><label>任务结束恢复号码</label><select id="restore_profile_iccid"><option value="">恢复执行前号码</option></select></div>
 <div class="check"><input id="enabled" type="checkbox"><label for="enabled">启用定时保号</label></div>
 <div class="check"><input id="profile_management_enabled" type="checkbox"><label for="profile_management_enabled">自动管理多个 eSIM 号码</label></div>
 <div class="check"><input id="notify_on_success" type="checkbox"><label for="notify_on_success">成功时 PushDeer</label></div>
 <div class="check"><input id="notify_on_failure" type="checkbox"><label for="notify_on_failure">失败时 PushDeer</label></div>
</div><div class="actions"><button onclick="saveConfig()">保存配置</button><button id="run" class="danger" onclick="runNow('','')">保号当前号码</button></div>
<div class="notice">“立即保号”会真实打开蜂窝数据并产生少量资费；验证请求强制绑定配置的蜂窝网卡，不会被服务器宽带出口替代。</div></div>
<div class="card section"><div class="head"><h2>eSIM 号码配置</h2><button class="secondary" onclick="refreshProfiles()">重新检测</button></div><div style="overflow:auto"><table><thead><tr><th>号码备注</th><th>ICCID</th><th>卡内状态</th><th>自动保号</th><th>间隔/天</th><th>下次保号</th><th>上次成功</th><th>上次流量</th><th>操作</th></tr></thead><tbody id="profiles"></tbody></table></div><div class="notice">每个号码都有独立的开关、周期和记录；新配置不会立即使用流量，任务结束后会恢复指定的常用号码。</div></div>
<div class="card section"><h2>执行历史</h2><div style="overflow:auto"><table><thead><tr><th>开始时间</th><th>号码</th><th>结果</th><th>HTTP</th><th>接收</th><th>发送</th><th>总流量</th><th>耗时</th><th>说明</th></tr></thead><tbody id="history"></tbody></table></div></div>
</div><script>
const ids=['device_id','interface','interval_days','target_url','network_connect_timeout_seconds','request_timeout_seconds','max_session_seconds','failure_retry_hours','idle_mode','lpac_path','lpac_at_device','profile_switch_timeout_seconds','restore_profile_iccid'];
const checks=['enabled','profile_management_enabled','notify_on_success','notify_on_failure'];
const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#039;');
const fmtTime=s=>s?new Date(s).toLocaleString():'-';const fmtBytes=n=>n==null?'-':n<1024?n+' B':n<1048576?(n/1024).toFixed(2)+' KiB':(n/1048576).toFixed(2)+' MiB';
async function api(path,opt={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},cache:'no-store',...opt});let j={};try{j=await r.json()}catch(_){}if(!r.ok)throw new Error(j.error||'请求失败');return j}
function renderProfiles(items,c){const restore=document.getElementById('restore_profile_iccid');const selected=c.restore_profile_iccid||'';restore.innerHTML='<option value="">恢复执行前号码</option>'+items.filter(x=>x.profile_state!=='missing').map(x=>`<option value="${esc(x.iccid)}">${esc(x.label||'eSIM')} · ${esc(x.masked_iccid)}</option>`).join('');restore.value=selected;document.getElementById('profiles').innerHTML=items.map(x=>`<tr><td><input class="inline" maxlength="120" id="label-${esc(x.iccid)}" value="${esc(x.label||'eSIM')}"></td><td>${esc(x.masked_iccid)}</td><td>${x.profile_state==='missing'?'卡内未找到':(x.active?'当前启用':'已保存')}</td><td><input id="enabled-${esc(x.iccid)}" type="checkbox" ${x.keepalive_enabled?'checked':''}></td><td><input class="inline narrow" id="interval-${esc(x.iccid)}" type="number" min="1" max="179" value="${esc(x.interval_days)}"></td><td>${esc(fmtTime(x.next_run_at))}</td><td>${esc(fmtTime(x.last_success_at))}</td><td>${esc(fmtBytes(x.last_success_bytes))}</td><td><div class="row-actions"><button class="secondary" data-iccid="${esc(x.iccid)}" onclick="saveProfile(this.dataset.iccid)">保存策略</button><button class="danger" data-iccid="${esc(x.iccid)}" data-label="${esc(x.label||'eSIM')}" onclick="runNow(this.dataset.iccid,this.dataset.label)" ${x.profile_state==='missing'?'disabled':''}>保号此号码</button></div></td></tr>`).join('')||`<tr><td colspan="9" class="muted">${c.profile_management_enabled?'未检测到 eSIM 配置':'尚未启用多号码管理'}</td></tr>`}
async function loadAll(){try{const [c,s,h,p]=await Promise.all([api('/api/config'),api('/api/status'),api('/api/history?limit=50'),api('/api/profiles')]);ids.filter(k=>k!=='restore_profile_iccid').forEach(k=>document.getElementById(k).value=c[k]??'');document.getElementById('max_session_kib').value=Math.round(c.max_session_bytes/1024);checks.forEach(k=>document.getElementById(k).checked=!!c[k]);document.getElementById('state').textContent=s.running?('执行中'+(s.current_profile_label?' · '+s.current_profile_label:'')):((s.enabled?'已启用':'已停用')+(s.profile_management_enabled?' · '+(s.profile_count||0)+'个号码':''));document.getElementById('state').className=s.enabled?'ok':'';document.getElementById('next').textContent=fmtTime(s.next_run_at);document.getElementById('last').textContent=fmtTime(s.last_success_at);document.getElementById('bytes').textContent=fmtBytes(s.last_success_bytes);document.getElementById('run').disabled=s.running;renderProfiles(p.items||[],c);document.getElementById('history').innerHTML=h.items.map(x=>`<tr><td>${esc(fmtTime(x.started_at))}</td><td>${esc(x.profile_label||(x.target_iccid?'eSIM':'当前号码'))}</td><td class="${x.status==='success'?'ok':'fail'}">${esc(x.status)}</td><td>${esc(x.http_status??'-')}</td><td>${esc(fmtBytes(x.session_rx_bytes))}</td><td>${esc(fmtBytes(x.session_tx_bytes))}</td><td>${esc(fmtBytes(x.session_total_bytes))}</td><td>${esc(x.duration_seconds??'-')}s</td><td>${esc(x.error||x.restore_status||'')}</td></tr>`).join('')||'<tr><td colspan="9" class="muted">暂无执行记录</td></tr>'}catch(e){alert(e.message)}}
async function saveConfig(){try{const c={};ids.forEach(k=>c[k]=document.getElementById(k).value);['interval_days','network_connect_timeout_seconds','request_timeout_seconds','max_session_seconds','failure_retry_hours','profile_switch_timeout_seconds'].forEach(k=>c[k]=Number(c[k]));c.max_session_bytes=Number(document.getElementById('max_session_kib').value)*1024;checks.forEach(k=>c[k]=document.getElementById(k).checked);await api('/api/config',{method:'PUT',body:JSON.stringify(c)});alert('配置已保存');loadAll()}catch(e){alert(e.message)}}
async function saveProfile(iccid){try{await api('/api/profiles/'+encodeURIComponent(iccid),{method:'PUT',body:JSON.stringify({label:document.getElementById('label-'+iccid).value,keepalive_enabled:document.getElementById('enabled-'+iccid).checked,interval_days:Number(document.getElementById('interval-'+iccid).value)})});alert('此号码的策略已保存');loadAll()}catch(e){alert(e.message)}}
async function refreshProfiles(){try{await api('/api/profiles/refresh',{method:'POST',body:'{}'});loadAll()}catch(e){alert(e.message)}}
async function runNow(iccid,label){const target=label?'“'+label+'”':'当前号码';if(!confirm('这会真实使用少量蜂窝流量，确定为'+target+'执行保号？'))return;try{await api('/api/run',{method:'POST',body:JSON.stringify({confirm:true,iccid:iccid||''})});alert('已开始执行');loadAll()}catch(e){alert(e.message)}}
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
            elif path.path == "/api/profiles":
                query = urllib.parse.parse_qs(path.query)
                force = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                self._send(200, {"items": self.manager.profiles(force=force)})
            else:
                self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(400, {"error": str(exc)})

    def do_PUT(self) -> None:
        if not self._require_auth():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/profiles/"):
            try:
                iccid = urllib.parse.unquote(path.removeprefix("/api/profiles/"))
                if "/" in iccid:
                    raise ValueError("ICCID 格式无效")
                updated = self.manager.update_profile_policy(iccid, self._body())
                self._send(200, updated)
            except Exception as exc:
                self._send(400, {"error": str(exc)})
            return
        if path != "/api/config":
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
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/profiles/refresh":
            try:
                self._send(200, {"items": self.manager.profiles(force=True)})
            except Exception as exc:
                self._send(400, {"error": str(exc)})
            return
        if path != "/api/run":
            self._send(404, {"error": "not found"})
            return
        try:
            body = self._body()
            if body.get("confirm") is not True:
                raise ValueError("必须明确确认会使用蜂窝流量")
            target_iccid = str(body.get("iccid") or "").strip()
            if not self.manager.trigger("manual", target_iccid):
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
