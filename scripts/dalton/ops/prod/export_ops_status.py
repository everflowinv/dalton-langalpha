#!/usr/bin/env python3
"""Everflow macOS host operational jobs status exporter (`var/dalton/ops_jobs_status.json`).

Pure Python 3 standard library only so macOS ``launchd`` (``com.dalton.ops-status-exporter``)
can run this script every 60 seconds without third-party dependencies.

Decouples ``src/dalton/autonomy/system_jobs.py`` from the host ``researchdata_bridge``
``GET /jobs/ops`` endpoint by atomically exporting operational job health to
``var/dalton/ops_jobs_status.json`` (bind-mounted into the backend container at
``/app/var/dalton/ops_jobs_status.json``).

Output JSON schema (``schema_version = 1``)::

    {
      "schema_version": 1,
      "generated_at": "2026-11-25T11:00:00+00:00",
      "host_platform": "macos",
      "exporter": "com.dalton.ops-status-exporter",
      "ttl_seconds": 300,
      "jobs": [
        {
          "key": "ops-db-backup",
          "kind": "ops_db_backup",
          "scope": "ops",
          "title": "数据库备份",
          "description": "宿主机 launchd 定时备份 PostgreSQL 数据库",
          "status": "on" | "off" | "partial" | "unknown",
          "last_run_at": "<ISO-8601 timestamp or null>",
          "last_result": "<sanitized status text or null>",
          "next_run_at": "<ISO-8601 timestamp or null>",
          "can_toggle": false,
          "can_run_now": false
        },
        ...
      ]
    }

Staleness contract:
- Default export cadence: every 60 seconds (``StartInterval = 60``).
- Default staleness threshold: ``300`` seconds (5 minutes; overridable via
  ``--ttl-seconds`` or ``DALTON_OPS_STATUS_MAX_AGE_SECONDS``).
- Backend readers MUST reject files older than the staleness threshold (or more
  than 60 seconds in the future) and report ``status = "unknown"``.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta, timezone
import fcntl
import http.client
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

OPS_STATUS_SCHEMA_VERSION = 1
DEFAULT_OPS_STATUS_TTL_SECONDS = 300
EXPORTER_LABEL = "com.dalton.ops-status-exporter"
DEFAULT_REDIS_DIAG_STALE_SECONDS = 180
DEFAULT_REDIS_SERVER_SAMPLE_INTERVAL_SECONDS = 30
DEFAULT_REDIS_SERVER_STALE_SECONDS = DEFAULT_REDIS_SERVER_SAMPLE_INTERVAL_SECONDS * 3
DEFAULT_REDIS_ALERT_COOLDOWN_SECONDS = 1800
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8962"
DEFAULT_GATEWAY_ALERT_TIMEOUT_SECONDS = 30.0  # Matches Feishu DEFAULT_FEISHU_HTTP_TIMEOUT_MS (30s)
REDIS_DIAG_LARGE_PAYLOAD_BYTES = 512 * 1024
REDIS_DIAG_P2_RECOVERED_THRESHOLD = 5
REDIS_DIAG_P2_POOL_UTIL_THRESHOLD = 0.80
REDIS_DIAG_P2_LOOP_LAG_MS_THRESHOLD = 1500.0
REDIS_DIAG_P2_FORK_USEC_THRESHOLD = 200_000
REDIS_DIAG_P2_SLOWLOG_US_THRESHOLD = 100_000

_LOCAL_PATH_RE = re.compile(r"/(?:Users|private|opt|tmp|var)/[^\s,;:\"')\]>]+")


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parents[4]


def _scrub_local_paths(text: Any) -> str:
    """Replace host absolute paths starting with `/Users/`, `/private/`, `/opt/`, `/tmp/`, or `/var/` with `<local>`."""
    if text is None:
        return ""
    return _LOCAL_PATH_RE.sub("<local>", str(text)).strip()


def read_complete_tail_lines(
    path: Path,
    *,
    max_lines: int = 25,
    max_scan_bytes: int = 4 * 1024 * 1024,
) -> list[str]:
    """Read the last ``max_lines`` complete lines from ``path`` without truncating a line's leading prefix."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        file_size = fh.tell()
        if file_size <= 0:
            return []
        block_size = 65536
        pos = file_size
        blocks: list[bytes] = []
        newline_count = 0
        scanned = 0
        while pos > 0 and newline_count <= max_lines and scanned < max_scan_bytes:
            read_len = min(block_size, pos, max_scan_bytes - scanned)
            pos -= read_len
            fh.seek(pos, os.SEEK_SET)
            chunk = fh.read(read_len)
            blocks.append(chunk)
            newline_count += chunk.count(b"\n")
            scanned += read_len
        raw = b"".join(reversed(blocks))
        if pos > 0:
            first_nl = raw.find(b"\n")
            if first_nl == -1:
                return []
            raw = raw[first_nl + 1 :]
        decoded = raw.decode("utf-8", errors="replace").strip()
        if not decoded:
            return []
        return decoded.splitlines()[-max_lines:]


def probe_launchd_label_status(
    label: str,
    *,
    uid: int | None = None,
    launchctl_bin: str | None = None,
) -> str:
    """Return ``'on'`` if ``launchctl print gui/<uid>/<label>`` succeeds, ``'off'`` on non-zero exit, else ``'unknown'``."""
    try:
        uid_val = os.getuid() if uid is None else uid
        bin_path = launchctl_bin or os.environ.get("LAUNCHCTL_BIN", "").strip() or "/bin/launchctl"
        if not os.path.isabs(bin_path) and bin_path != "launchctl":
            bin_path = "launchctl"
        elif os.path.isabs(bin_path) and not os.path.exists(bin_path):
            bin_path = "launchctl"
        minimal_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": os.environ.get("HOME", os.path.expanduser("~")),
        }
        res = subprocess.run(
            [bin_path, "print", f"gui/{uid_val}/{label}"],
            env=minimal_env,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        return "on" if res.returncode == 0 else "off"
    except Exception:
        return "unknown"


def load_plist_dict(path: Path) -> dict[str, Any] | None:
    """Read a launchd plist file via ``plistlib`` and return its root dict, or ``None`` on error."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as fh:
            data = plistlib.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def resolve_host_tzinfo(
    localtime_path: Path = Path("/etc/localtime"),
    *,
    fallback_now: datetime | None = None,
) -> Any:
    """Resolve host IANA timezone from ``/etc/localtime`` symlink via ``ZoneInfo``."""
    for target_fn in (
        lambda: os.readlink(localtime_path),
        lambda: str(localtime_path.resolve()),
    ):
        try:
            raw_target = target_fn()
            marker = "zoneinfo/"
            idx = raw_target.find(marker)
            if idx != -1:
                iana_name = raw_target[idx + len(marker) :].strip("/")
                if iana_name:
                    return ZoneInfo(iana_name)
        except Exception:
            continue
    if fallback_now is not None and isinstance(fallback_now.tzinfo, ZoneInfo):
        return fallback_now.tzinfo
    return datetime.now().astimezone().tzinfo or timezone.utc


def compute_next_calendar_interval_iso(
    plist_data: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    localtime_path: Path = Path("/etc/localtime"),
) -> str | None:
    """Compute next run ISO 8601 timestamp in host local timezone from ``StartCalendarInterval``."""
    if not isinstance(plist_data, dict):
        return None
    raw_interval = plist_data.get("StartCalendarInterval")
    intervals: list[dict[str, Any]] = []
    if isinstance(raw_interval, dict):
        intervals.append(raw_interval)
    elif isinstance(raw_interval, list):
        intervals.extend(x for x in raw_interval if isinstance(x, dict))
    if not intervals:
        return None

    for item in intervals:
        if any(k in item for k in ("Weekday", "Day", "Month")):
            return None

    tz = resolve_host_tzinfo(
        localtime_path=localtime_path,
        fallback_now=now,
    )
    now_local = (now or datetime.now(tz)).astimezone(tz)
    candidates: list[datetime] = []
    for item in intervals:
        raw_hour = item.get("Hour")
        raw_min = item.get("Minute", 0)
        if not isinstance(raw_hour, int) or isinstance(raw_hour, bool):
            continue
        if not isinstance(raw_min, int) or isinstance(raw_min, bool):
            continue
        if not (0 <= raw_hour <= 23 and 0 <= raw_min <= 59):
            continue
        cand = datetime(
            now_local.year,
            now_local.month,
            now_local.day,
            raw_hour,
            raw_min,
            0,
            tzinfo=tz,
        )
        if cand <= now_local:
            next_day = now_local.date() + timedelta(days=1)
            cand = datetime(
                next_day.year,
                next_day.month,
                next_day.day,
                raw_hour,
                raw_min,
                0,
                tzinfo=tz,
            )
        candidates.append(cand)
    if not candidates:
        return None
    return min(candidates).isoformat()


def inspect_pg_backup_job(
    *,
    pg_backup_log_file: Path,
    pg_backup_plist_file: Path,
    now: datetime | None = None,
    launchctl_bin: str | None = None,
    uid: int | None = None,
    localtime_path: Path = Path("/etc/localtime"),
) -> dict[str, Any]:
    """Inspect ``com.dalton.pg-backup`` launchd status, log file, and schedule."""
    job: dict[str, Any] = {
        "key": "ops-db-backup",
        "kind": "ops_db_backup",
        "scope": "ops",
        "title": "数据库备份",
        "description": "宿主机 launchd 定时备份 PostgreSQL 数据库",
        "status": "unknown",
        "last_run_at": None,
        "last_result": None,
        "next_run_at": None,
        "can_toggle": False,
        "can_run_now": False,
    }
    try:
        job["status"] = probe_launchd_label_status(
            "com.dalton.pg-backup",
            uid=uid,
            launchctl_bin=launchctl_bin,
        )
    except Exception:
        job["status"] = "unknown"

    try:
        if pg_backup_log_file.is_file():
            lines = read_complete_tail_lines(pg_backup_log_file, max_lines=25)
            non_empty = [ln.strip() for ln in lines if ln.strip()]
            if non_empty:
                last_line = non_empty[-1]
                parts = last_line.split()
                if len(parts) >= 3:
                    naive_dt = datetime.strptime(
                        f"{parts[0]} {parts[1]}",
                        "%Y-%m-%d %H:%M:%S",
                    )
                    job["last_run_at"] = naive_dt.astimezone().isoformat()
                    status_tok = parts[2].lower()
                    if status_tok == "ok":
                        size_tok = (
                            parts[3]
                            if len(parts) >= 4
                            and not parts[3].startswith(("/", "~"))
                            else ""
                        )
                        res_text = f"成功（{size_tok}）" if size_tok else "成功"
                    else:
                        res_text = "失败"
                    job["last_result"] = _scrub_local_paths(res_text)
    except Exception:
        pass

    try:
        plist_data = load_plist_dict(pg_backup_plist_file)
        job["next_run_at"] = compute_next_calendar_interval_iso(
            plist_data,
            now=now,
            localtime_path=localtime_path,
        )
    except Exception:
        pass

    return job


def inspect_backend_watchdog_job(
    *,
    backend_watchdog_plist_file: Path,
    watchdog_failures_file: Path,
    launchctl_bin: str | None = None,
    uid: int | None = None,
) -> dict[str, Any]:
    """Inspect ``com.dalton.backend-watchdog`` launchd status, failures file, and next interval."""
    job: dict[str, Any] = {
        "key": "ops-backend-watchdog",
        "kind": "ops_backend_watchdog",
        "scope": "ops",
        "title": "后端看门狗",
        "description": "宿主机 launchd 监控后端与桥接服务健康状态",
        "status": "unknown",
        "last_run_at": None,
        "last_result": None,
        "next_run_at": None,
        "can_toggle": False,
        "can_run_now": False,
    }
    try:
        job["status"] = probe_launchd_label_status(
            "com.dalton.backend-watchdog",
            uid=uid,
            launchctl_bin=launchctl_bin,
        )
    except Exception:
        job["status"] = "unknown"

    start_interval = 60
    try:
        plist_data = load_plist_dict(backend_watchdog_plist_file)
        if isinstance(plist_data, dict):
            raw_iv = plist_data.get("StartInterval")
            if isinstance(raw_iv, int) and not isinstance(raw_iv, bool) and raw_iv > 0:
                start_interval = raw_iv
    except Exception:
        pass

    try:
        if watchdog_failures_file.is_file():
            mtime = watchdog_failures_file.stat().st_mtime
            last_run_dt = datetime.fromtimestamp(mtime).astimezone()
            job["last_run_at"] = last_run_dt.isoformat()
            raw_text = watchdog_failures_file.read_text(encoding="utf-8", errors="replace").strip()
            if raw_text.isascii() and raw_text.isdigit():
                count = int(raw_text)
                job["last_result"] = "健康" if count == 0 else f"连续失败 {count} 次"
            else:
                job["last_result"] = None
            job["next_run_at"] = (
                last_run_dt + timedelta(seconds=start_interval)
            ).isoformat()
    except Exception:
        pass

    return job


def collect_ops_jobs(
    *,
    pg_backup_log_file: Path,
    pg_backup_plist_file: Path,
    backend_watchdog_plist_file: Path,
    watchdog_failures_file: Path,
    now: datetime | None = None,
    launchctl_bin: str | None = None,
    uid: int | None = None,
    localtime_path: Path = Path("/etc/localtime"),
) -> list[dict[str, Any]]:
    """Collect read-only operational job snapshots."""
    return [
        inspect_pg_backup_job(
            pg_backup_log_file=pg_backup_log_file,
            pg_backup_plist_file=pg_backup_plist_file,
            now=now,
            launchctl_bin=launchctl_bin,
            uid=uid,
            localtime_path=localtime_path,
        ),
        inspect_backend_watchdog_job(
            backend_watchdog_plist_file=backend_watchdog_plist_file,
            watchdog_failures_file=watchdog_failures_file,
            launchctl_bin=launchctl_bin,
            uid=uid,
        ),
    ]


def build_ops_status_payload(
    jobs: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
    ttl_seconds: int = DEFAULT_OPS_STATUS_TTL_SECONDS,
    host_platform: str | None = None,
    redis_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap collected ops jobs into the versioned ``ops_jobs_status.json`` schema."""
    ts = (generated_at or datetime.now(UTC)).astimezone(UTC)
    plat = (
        str(host_platform).strip().lower()
        if host_platform
        else (
            str(os.environ.get("DALTON_HOST_PLATFORM") or "").strip().lower()
            or ("macos" if sys.platform == "darwin" else "linux")
        )
    )
    eff_ttl = max(1, int(ttl_seconds))
    payload: dict[str, Any] = {
        "schema_version": OPS_STATUS_SCHEMA_VERSION,
        "generated_at": ts.isoformat(),
        "host_platform": plat,
        "exporter": EXPORTER_LABEL,
        "ttl_seconds": eff_ttl,
        "jobs": jobs,
    }
    if redis_diagnostics is not None:
        payload["redis_diagnostics"] = redis_diagnostics
    return payload


def _parse_iso_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def aggregate_redis_diagnostics(
    diag_dir: Path,
    *,
    now: datetime | None = None,
    stale_worker_seconds: int = DEFAULT_REDIS_DIAG_STALE_SECONDS,
    stale_server_seconds: int = DEFAULT_REDIS_SERVER_STALE_SECONDS,
    prune_stale: bool = True,
) -> dict[str, Any]:
    """Merge ``redis_diag_server.json`` and active ``redis_diag_worker_*.json`` (stdlib-only)."""
    now_dt = (now or datetime.now(UTC)).astimezone(UTC)
    server_data: dict[str, Any] | None = None
    server_path = diag_dir / "redis_diag_server.json"
    if server_path.is_file():
        try:
            loaded = json.loads(server_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                written_dt = _parse_iso_utc(loaded.get("written_at") or loaded.get("updated_at"))
                if written_dt is not None:
                    server_age_s = (now_dt - written_dt).total_seconds()
                    if server_age_s <= stale_server_seconds:
                        server_data = loaded
        except Exception:
            server_data = None

    workers: list[dict[str, Any]] = []
    pruned_files: list[str] = []
    if diag_dir.is_dir():
        for wf in sorted(diag_dir.glob("redis_diag_worker_*.json")):
            try:
                loaded = json.loads(wf.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("worker file is not an object")
                updated_dt = _parse_iso_utc(loaded.get("updated_at"))
                if updated_dt is None:
                    raise ValueError("missing updated_at")
                age_s = (now_dt - updated_dt).total_seconds()
                if age_s > stale_worker_seconds:
                    if prune_stale:
                        try:
                            wf.unlink()
                            pruned_files.append(wf.name)
                        except OSError:
                            pass
                    continue
                workers.append(loaded)
            except Exception:
                if prune_stale:
                    try:
                        if (now_dt.timestamp() - wf.stat().st_mtime) > stale_worker_seconds:
                            wf.unlink()
                            pruned_files.append(wf.name)
                    except OSError:
                        pass

    total_slow = 0
    total_timeout = 0
    total_recovered = 0
    total_transport_lost_5m = 0
    total_transport_lost_ever = 0
    total_large_payloads = 0
    total_active_subagent_streams = 0
    max_total_ms = 0.0
    max_pool_queue_ms = 0.0
    max_health_check_ms = 0.0
    max_acquire_ms = 0.0
    max_wire_ms = 0.0
    max_payload_bytes = 0
    max_pool_util = 0.0
    max_pool_util_target: str | None = None
    max_lag_5s = 0.0
    max_lag_60s = 0.0
    max_hb_age = 0.0
    latest_transport_lost_at = ""

    for w in workers:
        counts = w.get("counts") or {}
        lat = w.get("latency_5m") or {}
        lag = w.get("loop_lag") or {}
        pools = w.get("pools") or {}

        total_slow += int(counts.get("slow_writes_5m", 0) or 0)
        total_timeout += int(counts.get("timeout_errors_5m", 0) or 0)
        total_recovered += int(counts.get("recovered_writes_5m", 0) or 0)
        total_transport_lost_5m += int(counts.get("transport_lost_5m", 0) or 0)
        total_transport_lost_ever += int(counts.get("transport_lost_total", 0) or 0)
        total_large_payloads += int(counts.get("large_payloads_5m", 0) or 0)
        total_active_subagent_streams += int(counts.get("active_subagent_streams", 0) or 0)

        tl_at = str(w.get("last_transport_lost_at") or "")
        if tl_at and tl_at > latest_transport_lost_at:
            latest_transport_lost_at = tl_at

        max_total_ms = max(max_total_ms, float(lat.get("max_total_ms", 0.0) or 0.0))
        max_pool_queue_ms = max(max_pool_queue_ms, float(lat.get("max_pool_queue_ms", 0.0) or 0.0))
        max_health_check_ms = max(max_health_check_ms, float(lat.get("max_health_check_ms", 0.0) or 0.0))
        max_acquire_ms = max(max_acquire_ms, float(lat.get("max_acquire_ms", 0.0) or 0.0))
        max_wire_ms = max(max_wire_ms, float(lat.get("max_wire_ms", 0.0) or 0.0))
        max_payload_bytes = max(max_payload_bytes, int(lat.get("max_payload_bytes", 0) or 0))

        max_lag_5s = max(max_lag_5s, float(lag.get("max_5s_ms", 0.0) or 0.0))
        max_lag_60s = max(max_lag_60s, float(lag.get("max_60s_ms", 0.0) or 0.0))
        max_hb_age = max(max_hb_age, float(lag.get("last_heartbeat_age_ms", 0.0) or 0.0))

        if isinstance(pools, dict):
            for pool_name, pool_stats in pools.items():
                if not isinstance(pool_stats, dict):
                    continue
                util = float(pool_stats.get("utilization", 0.0) or 0.0)
                if util > max_pool_util:
                    max_pool_util = util
                    max_pool_util_target = f"{w.get('worker_id')}:{pool_name}"

    return {
        "schema_version": 1,
        "generated_at": now_dt.isoformat(),
        "stale_worker_seconds": int(stale_worker_seconds),
        "stale_server_seconds": int(stale_server_seconds),
        "pruned_worker_files": pruned_files,
        "cluster": {
            "active_worker_count": len(workers),
            "total_slow_writes_5m": total_slow,
            "total_timeout_errors_5m": total_timeout,
            "total_recovered_writes_5m": total_recovered,
            "total_transport_lost_5m": total_transport_lost_5m,
            "total_transport_lost_ever": total_transport_lost_ever,
            "last_transport_lost_at": latest_transport_lost_at or None,
            "total_large_payloads_5m": total_large_payloads,
            "total_active_subagent_streams": total_active_subagent_streams,
            "max_total_ms": round(max_total_ms, 2),
            "max_pool_queue_ms": round(max_pool_queue_ms, 2),
            "max_health_check_ms": round(max_health_check_ms, 2),
            "max_acquire_ms": round(max_acquire_ms, 2),
            "max_wire_ms": round(max_wire_ms, 2),
            "max_payload_bytes_5m": max_payload_bytes,
            "max_pool_utilization": round(max_pool_util, 4),
            "max_pool_utilization_target": max_pool_util_target,
            "max_loop_lag_5s_ms": round(max_lag_5s, 2),
            "max_loop_lag_60s_ms": round(max_lag_60s, 2),
            "max_heartbeat_age_ms": round(max_hb_age, 2),
        },
        "server": server_data,
        "workers": workers,
    }


def _read_env_file_var(env_file: Path, key: str) -> str:
    """Read one KEY=VALUE entry from an owner-only env file (refuses symlinks)."""
    if not env_file.is_file() or env_file.is_symlink():
        return ""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        k, v = line.split("=", 1)
        if k.strip() != key:
            continue
        val = v.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        return val.strip()
    return ""


def _normalize_gateway_base_url(raw: str) -> str:
    cleaned = raw.strip().rstrip("/")
    suffix = "/internal/ops-alerts"
    if cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)].rstrip("/")
    return cleaned


def resolve_ops_alert_transport(
    *,
    gateway_url: str = "",
    service_token: str = "",
    gateway_env_file: Path | None = None,
) -> tuple[str, str]:
    """Resolve Everflow loopback ops-alert gateway URL and service token.

    Matches ``watchdog.sh`` / ``channel-gateway.plist``:
    - Gateway URL: explicit arg -> ``DALTON_CHANNEL_GATEWAY_URL`` -> ``DALTON_OPS_ALERTS_URL``
      -> ``channel-gateway.env`` -> ``http://127.0.0.1:8962``.
    - Service token: explicit arg -> ``INTERNAL_SERVICE_TOKEN`` env ->
      ``INTERNAL_SERVICE_TOKEN`` from ``DALTON_CHANNEL_GATEWAY_ENV_FILE``
      (default ``~/.config/dalton/channel-gateway.env``).
    """
    env_file = gateway_env_file
    if env_file is None:
        raw_env_file = os.environ.get("DALTON_CHANNEL_GATEWAY_ENV_FILE", "").strip()
        env_file = (
            Path(raw_env_file).expanduser()
            if raw_env_file
            else Path.home() / ".config" / "dalton" / "channel-gateway.env"
        )

    eff_gw = (
        _normalize_gateway_base_url(gateway_url)
        or _normalize_gateway_base_url(os.environ.get("DALTON_CHANNEL_GATEWAY_URL", ""))
        or _normalize_gateway_base_url(os.environ.get("DALTON_OPS_ALERTS_URL", ""))
        or _normalize_gateway_base_url(_read_env_file_var(env_file, "DALTON_CHANNEL_GATEWAY_URL"))
        or _normalize_gateway_base_url(_read_env_file_var(env_file, "DALTON_OPS_ALERTS_URL"))
        or DEFAULT_GATEWAY_URL
    )
    eff_tok = (
        service_token.strip()
        or os.environ.get("INTERNAL_SERVICE_TOKEN", "").strip()
        or _read_env_file_var(env_file, "INTERNAL_SERVICE_TOKEN")
    )
    return eff_gw, eff_tok


def _evaluate_redis_diag_alerts_with_patches(
    summary: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
    p1_window_seconds: int = 300,
    p2_cooldown_seconds: int = DEFAULT_REDIS_ALERT_COOLDOWN_SECONDS,
    p3_cooldown_seconds: int = DEFAULT_REDIS_ALERT_COOLDOWN_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate cluster-level P1/P2/P3 alerts, attaching per-alert ``state_update`` patches."""
    now_dt = (now or datetime.now(UTC)).astimezone(UTC)
    now_ts = now_dt.timestamp()
    cur_state = dict(state or {})
    next_state = dict(cur_state)
    alerts: list[dict[str, Any]] = []

    cluster = summary.get("cluster") or {}
    server = summary.get("server") or {}
    workers = summary.get("workers") or []

    active_worker_totals: dict[str, int] = {}
    for w in workers:
        if not isinstance(w, dict):
            continue
        wid = str(w.get("worker_id") or "").strip()
        if not wid:
            continue
        w_counts = w.get("counts") or {}
        active_worker_totals[wid] = int(w_counts.get("transport_lost_total", 0) or 0)

    tl_5m = int(cluster.get("total_transport_lost_5m", 0) or 0)
    tl_ever = int(cluster.get("total_transport_lost_ever", 0) or 0)
    tl_at = str(cluster.get("last_transport_lost_at") or "")
    if tl_5m >= 1:
        event_key = f"{tl_ever}:{tl_at}" if (tl_ever or tl_at) else f"5m:{tl_5m}"
        prev_key = str(cur_state.get("p1_last_event_key") or "")
        prev_ts = float(cur_state.get("p1_last_sent_ts", 0.0) or 0.0)
        prev_ever = int(cur_state.get("p1_last_ever", 0) or 0)
        prev_tl_at = str(cur_state.get("p1_last_transport_lost_at") or "")
        prev_worker_totals = cur_state.get("p1_worker_totals")
        if not isinstance(prev_worker_totals, dict):
            prev_worker_totals = {}
        same_window = (now_ts - prev_ts) < p1_window_seconds
        worker_new_incident = any(
            cnt > int(prev_worker_totals.get(wid, 0) or 0)
            for wid, cnt in active_worker_totals.items()
            if cnt > 0
        )
        ts_new_incident = bool(tl_at and prev_tl_at and tl_at > prev_tl_at)
        cluster_new_incident = (not active_worker_totals) and (tl_ever > prev_ever)
        has_new_incident = (
            worker_new_incident
            or ts_new_incident
            or cluster_new_incident
            or (event_key != prev_key and not same_window)
        )
        if not prev_ts or has_new_incident:
            p1_patch: dict[str, Any] = {
                "p1_last_event_key": event_key,
                "p1_last_ever": tl_ever,
                "p1_last_transport_lost_at": tl_at,
                "p1_last_sent_ts": now_ts,
                "p1_worker_totals": active_worker_totals,
            }
            alerts.append(
                {
                    "severity": "P1",
                    "code": "redis_diag_p1",
                    "detail": (
                        f"集群 5 分钟内发生 transport_lost={tl_5m} 次"
                        f"（active_workers={cluster.get('active_worker_count', 0)}, "
                        f"max_wire_ms={cluster.get('max_wire_ms', 0)}, "
                        f"max_acquire_ms={cluster.get('max_acquire_ms', 0)}, "
                        f"max_loop_lag_5s_ms={cluster.get('max_loop_lag_5s_ms', 0)}）"
                    ),
                    "_state_update": p1_patch,
                }
            )
            next_state.update(p1_patch)

    p2_reasons: list[str] = []
    recovered_5m = int(cluster.get("total_recovered_writes_5m", 0) or 0)
    if recovered_5m >= REDIS_DIAG_P2_RECOVERED_THRESHOLD:
        p2_reasons.append(f"recovered_5m={recovered_5m}")
    max_pool_util = float(cluster.get("max_pool_utilization", 0.0) or 0.0)
    if max_pool_util >= REDIS_DIAG_P2_POOL_UTIL_THRESHOLD:
        p2_reasons.append(f"pool_util={int(round(max_pool_util * 100))}%")
    max_lag_ms = max(
        float(cluster.get("max_loop_lag_5s_ms", 0.0) or 0.0),
        float(cluster.get("max_loop_lag_60s_ms", 0.0) or 0.0),
        float(cluster.get("max_heartbeat_age_ms", 0.0) or 0.0),
    )
    if max_lag_ms >= REDIS_DIAG_P2_LOOP_LAG_MS_THRESHOLD:
        p2_reasons.append(f"loop_lag_ms={round(max_lag_ms, 1)}")
    latest_fork_usec = int(((server.get("stats") or {}).get("latest_fork_usec", 0)) or 0)
    if latest_fork_usec >= REDIS_DIAG_P2_FORK_USEC_THRESHOLD:
        p2_reasons.append(f"latest_fork_usec={latest_fork_usec}")

    # Only count NEW >=100ms slowlog entries so lingering IDs in Redis SLOWLOG do not re-alert every 30m.
    prev_slow_id = int(cur_state.get("p2_last_slowlog_id", -1))
    prev_slow_ts = int(cur_state.get("p2_last_slowlog_start_time", 0) or 0)
    slowlog = server.get("slowlog") or []
    new_slow_over: list[dict[str, Any]] = []
    for e in slowlog:
        if not isinstance(e, dict):
            continue
        if int(e.get("duration_us", 0) or 0) < REDIS_DIAG_P2_SLOWLOG_US_THRESHOLD:
            continue
        eid_raw = e.get("id")
        ets = int(e.get("start_time", 0) or 0)
        if eid_raw is not None:
            eid = int(eid_raw)
            if prev_slow_id < 0 or eid > prev_slow_id or (ets > 0 and prev_slow_ts > 0 and ets > prev_slow_ts):
                new_slow_over.append(e)
        else:
            if (prev_slow_id < 0 and prev_slow_ts == 0) or (ets > prev_slow_ts):
                new_slow_over.append(e)

    p2_slowlog_patch: dict[str, Any] = {}
    if new_slow_over:
        max_slow_ms = max(round(int(e.get("duration_us", 0) or 0) / 1000.0, 1) for e in new_slow_over)
        p2_reasons.append(f"slowlog_ge_100ms={len(new_slow_over)}(max={max_slow_ms}ms)")
        latest_entry = max(
            new_slow_over,
            key=lambda e: (
                int(e.get("start_time", 0) or 0),
                int(e.get("id", -1) if e.get("id") is not None else -1),
            ),
        )
        if latest_entry.get("id") is not None:
            p2_slowlog_patch["p2_last_slowlog_id"] = int(latest_entry["id"])
        latest_ets = int(latest_entry.get("start_time", 0) or 0)
        if latest_ets > prev_slow_ts:
            p2_slowlog_patch["p2_last_slowlog_start_time"] = latest_ets

    if p2_reasons:
        prev_p2_ts = float(cur_state.get("p2_last_sent_ts", 0.0) or 0.0)
        if not prev_p2_ts or (now_ts - prev_p2_ts) >= p2_cooldown_seconds:
            p2_patch = {"p2_last_sent_ts": now_ts, **p2_slowlog_patch}
            alerts.append(
                {
                    "severity": "P2",
                    "code": "redis_diag_p2",
                    "detail": "Redis 写入链路退化：" + "，".join(p2_reasons),
                    "_state_update": p2_patch,
                }
            )
            next_state.update(p2_patch)

    max_payload = int(cluster.get("max_payload_bytes_5m", 0) or 0)
    if max_payload >= REDIS_DIAG_LARGE_PAYLOAD_BYTES:
        prev_p3_ts = float(cur_state.get("p3_last_sent_ts", 0.0) or 0.0)
        if not prev_p3_ts or (now_ts - prev_p3_ts) >= p3_cooldown_seconds:
            p3_patch = {"p3_last_sent_ts": now_ts}
            alerts.append(
                {
                    "severity": "P3",
                    "code": "redis_diag_p3",
                    "detail": f"检测到超大单帧载荷 payload_bytes={max_payload}（阈值 {REDIS_DIAG_LARGE_PAYLOAD_BYTES}）",
                    "_state_update": p3_patch,
                }
            )
            next_state.update(p3_patch)

    return alerts, next_state


def evaluate_redis_diag_alerts(
    summary: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    now: datetime | None = None,
    p1_window_seconds: int = 300,
    p2_cooldown_seconds: int = DEFAULT_REDIS_ALERT_COOLDOWN_SECONDS,
    p3_cooldown_seconds: int = DEFAULT_REDIS_ALERT_COOLDOWN_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate cluster-level P1/P2/P3 alerts with event-window / 30-minute cooldown dedup."""
    raw_alerts, next_state = _evaluate_redis_diag_alerts_with_patches(
        summary,
        state=state,
        now=now,
        p1_window_seconds=p1_window_seconds,
        p2_cooldown_seconds=p2_cooldown_seconds,
        p3_cooldown_seconds=p3_cooldown_seconds,
    )
    clean_alerts = [{k: v for k, v in a.items() if k != "_state_update"} for a in raw_alerts]
    return clean_alerts, next_state


def post_gateway_ops_alert(
    code: str,
    detail: str,
    *,
    gateway_url: str,
    service_token: str,
    timeout_s: float = DEFAULT_GATEWAY_ALERT_TIMEOUT_SECONDS,
) -> tuple[bool, int | None, str | None]:
    """Post one alert to loopback ``POST {gateway_url}/internal/ops-alerts``."""
    if not gateway_url or not service_token:
        return False, None, "gateway_url or service_token not configured"
    url = f"{gateway_url.rstrip('/')}/internal/ops-alerts"
    body = json.dumps({"code": code, "detail": detail}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Service-Token": service_token,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300, int(resp.status), None
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:200]
        return False, int(exc.code), f"HTTP {exc.code}: {err_body}"
    except urllib.error.URLError as exc:
        return False, None, f"URLError: {exc.reason}"
    except (TimeoutError, http.client.HTTPException, OSError) as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def dispatch_redis_diag_alerts(
    summary: dict[str, Any],
    *,
    state_file: Path | None = None,
    gateway_url: str = "",
    service_token: str = "",
    gateway_env_file: Path | None = None,
    now: datetime | None = None,
    sender: Callable[[str, str], tuple[bool, int | None, str | None]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate and dispatch deduplicated cluster-level Redis diagnostic alerts.

    Cooldown / dedup state for each alert (P1/P2/P3) is committed to disk
    IMMEDIATELY after that individual alert succeeds (``ok is True``), so a
    subsequent alert timeout or crash never loses already-delivered cooldowns.
    """
    state: dict[str, Any] = {}
    if state_file is not None and state_file.is_file():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}

    alerts, _ = _evaluate_redis_diag_alerts_with_patches(summary, state=state, now=now)
    dispatched: list[dict[str, Any]] = []
    if not alerts:
        return dispatched

    eff_gw, eff_tok = gateway_url, service_token
    if sender is None:
        eff_gw, eff_tok = resolve_ops_alert_transport(
            gateway_url=gateway_url,
            service_token=service_token,
            gateway_env_file=gateway_env_file,
        )
        if not eff_gw or not eff_tok:
            return dispatched

    committed_state = dict(state)
    for raw_alert in alerts:
        state_patch = raw_alert.get("_state_update") or {}
        alert = {k: v for k, v in raw_alert.items() if k != "_state_update"}
        code = str(alert["code"])
        detail = str(alert["detail"])
        try:
            if sender is not None:
                ok, status, err = sender(code, detail)
            else:
                ok, status, err = post_gateway_ops_alert(
                    code,
                    detail,
                    gateway_url=eff_gw,
                    service_token=eff_tok,
                )
        except Exception as exc:
            ok, status, err = False, None, f"{type(exc).__name__}: {exc}"
        if ok:
            committed_state.update(state_patch)
            if state_file is not None:
                try:
                    write_ops_status_atomic(state_file, committed_state)
                except Exception:
                    pass
        dispatched.append({**alert, "ok": ok, "status": status, "error": err})

    return dispatched


def write_ops_status_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write ``ops_jobs_status.json`` (mode 0644) under an exclusive file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            tmp_path.write_text(rendered, encoding="utf-8")
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def resolve_default_paths() -> dict[str, Path]:
    """Resolve default Everflow macOS host paths for ops status export."""
    repo_root = _repo_root_default()
    launch_agents = Path(
        os.environ.get("DALTON_LAUNCH_AGENTS_DIR", os.path.expanduser("~/Library/LaunchAgents"))
    ).expanduser()
    output_default = os.environ.get("DALTON_OPS_STATUS_FILE", "").strip()
    output_path = (
        Path(output_default).expanduser()
        if output_default
        else (repo_root / "var" / "dalton" / "ops_jobs_status.json")
    )
    return {
        "output": output_path,
        "pg_backup_plist": Path(
            os.environ.get(
                "DALTON_PG_BACKUP_PLIST_FILE",
                str(launch_agents / "com.dalton.pg-backup.plist"),
            )
        ).expanduser(),
        "pg_backup_log": Path(
            os.environ.get(
                "DALTON_PG_BACKUP_LOG_FILE",
                os.path.expanduser("~/Library/Logs/dalton-pg-backup.log"),
            )
        ).expanduser(),
        "watchdog_plist": Path(
            os.environ.get(
                "DALTON_BACKEND_WATCHDOG_PLIST_FILE",
                str(launch_agents / "com.dalton.backend-watchdog.plist"),
            )
        ).expanduser(),
        "watchdog_failures": Path(
            os.environ.get(
                "DALTON_WATCHDOG_FAILURES_FILE",
                os.path.expanduser("~/Backups/dalton/.watchdog_failures"),
            )
        ).expanduser(),
        "redis_diag_dir": Path(
            os.environ.get(
                "DALTON_REDIS_DIAG_DIR",
                str(repo_root / "var" / "dalton" / "ops"),
            )
        ).expanduser(),
        "redis_alert_state": Path(
            os.environ.get(
                "DALTON_REDIS_DIAG_ALERT_STATE_FILE",
                str(repo_root / "var" / "dalton" / "ops" / "redis_diag_alert_state.json"),
            )
        ).expanduser(),
        "gateway_env_file": Path(
            os.environ.get(
                "DALTON_CHANNEL_GATEWAY_ENV_FILE",
                os.path.expanduser("~/.config/dalton/channel-gateway.env"),
            )
        ).expanduser(),
    }


def export_once(
    *,
    output_path: Path,
    pg_backup_log_file: Path,
    pg_backup_plist_file: Path,
    backend_watchdog_plist_file: Path,
    watchdog_failures_file: Path,
    ttl_seconds: int = DEFAULT_OPS_STATUS_TTL_SECONDS,
    now: datetime | None = None,
    launchctl_bin: str | None = None,
    uid: int | None = None,
    localtime_path: Path = Path("/etc/localtime"),
    redis_diag_dir: Path | None = None,
    redis_alert_state_file: Path | None = None,
    gateway_url: str = "",
    service_token: str = "",
    gateway_env_file: Path | None = None,
    alert_sender: Callable[[str, str], tuple[bool, int | None, str | None]] | None = None,
) -> dict[str, Any]:
    """Collect host ops job states and write ``ops_jobs_status.json`` atomically."""
    jobs = collect_ops_jobs(
        pg_backup_log_file=pg_backup_log_file,
        pg_backup_plist_file=pg_backup_plist_file,
        backend_watchdog_plist_file=backend_watchdog_plist_file,
        watchdog_failures_file=watchdog_failures_file,
        now=now,
        launchctl_bin=launchctl_bin,
        uid=uid,
        localtime_path=localtime_path,
    )
    redis_diag_summary: dict[str, Any] | None = None
    if redis_diag_dir is not None and redis_diag_dir.is_dir():
        has_diag_files = (redis_diag_dir / "redis_diag_server.json").is_file() or any(
            redis_diag_dir.glob("redis_diag_worker_*.json")
        )
        if has_diag_files:
            redis_diag_summary = aggregate_redis_diagnostics(
                redis_diag_dir,
                now=now,
                prune_stale=True,
            )
            eff_gw, eff_tok = resolve_ops_alert_transport(
                gateway_url=gateway_url,
                service_token=service_token,
                gateway_env_file=gateway_env_file,
            )
            eff_state = redis_alert_state_file or (redis_diag_dir / "redis_diag_alert_state.json")
            dispatch_redis_diag_alerts(
                redis_diag_summary,
                state_file=eff_state,
                gateway_url=eff_gw,
                service_token=eff_tok,
                gateway_env_file=gateway_env_file,
                now=now,
                sender=alert_sender,
            )
    payload = build_ops_status_payload(
        jobs,
        generated_at=now,
        ttl_seconds=ttl_seconds,
        redis_diagnostics=redis_diag_summary,
    )
    write_ops_status_atomic(output_path, payload)
    return payload


def build_cli_parser() -> argparse.ArgumentParser:
    defaults = resolve_default_paths()
    parser = argparse.ArgumentParser(
        description="Export macOS host launchd operational jobs status to var/dalton/ops_jobs_status.json"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=defaults["output"],
        help="Destination path for ops_jobs_status.json",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=int(os.environ.get("DALTON_OPS_STATUS_MAX_AGE_SECONDS", str(DEFAULT_OPS_STATUS_TTL_SECONDS))),
        help="Freshness TTL in seconds embedded in the exported status file (default: 300)",
    )
    parser.add_argument("--pg-backup-log", type=Path, default=defaults["pg_backup_log"])
    parser.add_argument("--pg-backup-plist", type=Path, default=defaults["pg_backup_plist"])
    parser.add_argument("--watchdog-plist", type=Path, default=defaults["watchdog_plist"])
    parser.add_argument("--watchdog-failures", type=Path, default=defaults["watchdog_failures"])
    parser.add_argument("--redis-diag-dir", type=Path, default=defaults["redis_diag_dir"])
    parser.add_argument("--redis-alert-state", type=Path, default=defaults["redis_alert_state"])
    parser.add_argument("--gateway-url", type=str, default=os.environ.get("DALTON_CHANNEL_GATEWAY_URL", ""))
    parser.add_argument("--gateway-env-file", type=Path, default=defaults["gateway_env_file"])
    parser.add_argument("--print-json", action="store_true", help="Also print the exported JSON to stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    payload = export_once(
        output_path=args.output.expanduser(),
        pg_backup_log_file=args.pg_backup_log.expanduser(),
        pg_backup_plist_file=args.pg_backup_plist.expanduser(),
        backend_watchdog_plist_file=args.watchdog_plist.expanduser(),
        watchdog_failures_file=args.watchdog_failures.expanduser(),
        ttl_seconds=max(1, int(args.ttl_seconds)),
        redis_diag_dir=args.redis_diag_dir.expanduser(),
        redis_alert_state_file=args.redis_alert_state.expanduser(),
        gateway_url=str(args.gateway_url or "").strip(),
        gateway_env_file=args.gateway_env_file.expanduser(),
    )
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
