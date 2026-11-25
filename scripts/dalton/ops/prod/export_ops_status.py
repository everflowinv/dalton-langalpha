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
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import uuid
from typing import Any
from zoneinfo import ZoneInfo

OPS_STATUS_SCHEMA_VERSION = 1
DEFAULT_OPS_STATUS_TTL_SECONDS = 300
EXPORTER_LABEL = "com.dalton.ops-status-exporter"

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
    return {
        "schema_version": OPS_STATUS_SCHEMA_VERSION,
        "generated_at": ts.isoformat(),
        "host_platform": plat,
        "exporter": EXPORTER_LABEL,
        "ttl_seconds": eff_ttl,
        "jobs": jobs,
    }


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
    payload = build_ops_status_payload(
        jobs,
        generated_at=now,
        ttl_seconds=ttl_seconds,
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
    )
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
