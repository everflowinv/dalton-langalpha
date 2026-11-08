"""Manifest loader and DB seed synchronization for OC-1 deterministic data tasks (§1.1, §2.3.1, §3.1)."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
import yaml

MANIFEST_PATH = Path(__file__).resolve().parent / "manifest.yaml"
DEFAULT_TIMEOUT_SECONDS = 1800
CONTAINER_PYTHON_BIN = "/app/.venv/bin/python"


class DataTaskManifestEntry(BaseModel):
    """Single deterministic data task definition loaded from ``manifest.yaml``."""

    task_code: str
    job_key: str
    title: str
    batch_no: int = Field(ge=1, le=4)
    runner: Literal["container", "host"] = "container"
    host_mode: Literal["mirror", "run"] | None = None
    cron: str
    timezone: str
    cutover_cron: str | None = None
    cutover_timezone: str | None = None
    timeout_seconds: int | None = None
    no_output_timeout_seconds: int | None = None
    output_max_bytes: int | None = None
    cwd: str = "/Users/everflow/.openclaw/workspace"
    command: str
    command_argv: list[str]
    secret_names: list[str] = Field(default_factory=list)
    cos_mode: Literal["skip", "prod"] = "skip"
    notify_enabled: bool = False
    enabled: bool = False

    @model_validator(mode="before")
    @classmethod
    def _normalize_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "cron" not in out and "cron_expr" in out:
            out["cron"] = out["cron_expr"]
        if "cwd" not in out and "workdir" in out:
            out["cwd"] = out["workdir"]
        if "job_key" not in out and "name" in out:
            out["job_key"] = out["name"]
        if "secret_names" not in out and "required_secrets" in out:
            out["secret_names"] = out["required_secrets"]
        return out

    @property
    def required_secrets(self) -> list[str]:
        return list(self.secret_names)

    @property
    def cron_expr(self) -> str:
        return self.cron

    @property
    def workdir(self) -> str:
        return self.cwd

    @property
    def name(self) -> str:
        return self.job_key

    @property
    def effective_timeout_seconds(self) -> int:
        return self.timeout_seconds if self.timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS

    def resolved_command_argv(self) -> list[str]:
        """Return ``command_argv`` with any ``[REDACTED]`` placeholder restored to ``/app/.venv/bin/python`` (§2.3.1)."""
        return [arg.replace("[REDACTED]", CONTAINER_PYTHON_BIN) for arg in self.command_argv]


class DataTaskManifest(BaseModel):
    """Collection of the 19 OC-1 deterministic data tasks."""

    tasks: list[DataTaskManifestEntry]

    def by_job_key(self) -> dict[str, DataTaskManifestEntry]:
        return {t.job_key: t for t in self.tasks}

    def by_task_code(self) -> dict[str, DataTaskManifestEntry]:
        return {t.task_code: t for t in self.tasks}


@lru_cache(maxsize=4)
def load_data_task_manifest(path: Path | str | None = None) -> DataTaskManifest:
    """Load and validate ``src/dalton/data_jobs/manifest.yaml``."""
    target = Path(path) if path is not None else MANIFEST_PATH
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return DataTaskManifest.model_validate(raw)


def load_manifest_entries(path: Path | str | None = None) -> list[DataTaskManifestEntry]:
    """Return the ordered list of manifest entries."""
    return list(load_data_task_manifest(path).tasks)


async def sync_manifest_to_db(conn: Any, manifest: DataTaskManifest | None = None) -> int:
    """Idempotently synchronize the 19 task definitions into ``dalton_data_task_defs`` (§3.1, §7.1.5).

    Synchronizes the active ``cron_expr`` and ``timezone`` from ``manifest.yaml`` (excluding
    ``cutover_cron`` / ``cutover_timezone``). Existing rows keep their runtime state
    (``enabled``, ``notify_enabled``, ``cos_mode``, ``paused_by_user``, ``consecutive_failures``,
    ``config_version``, ``active_version``, ``updated_by``, ``audit_history``).
    Newly inserted rows initialize ``audit_history`` with the ``version = 1`` baseline snapshot.
    """
    loaded = manifest or load_data_task_manifest()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    count = 0
    async with conn.cursor() as cur:
        for entry in loaded.tasks:
            initial_audit = [
                {
                    "version": 1,
                    "prev_version": None,
                    "actor_user_id": "system",
                    "changed_at": now_iso,
                    "action": "seed",
                    "changes": {},
                    "snapshot": {
                        "enabled": bool(entry.enabled),
                        "notify_routes": [],
                    },
                }
            ]
            await cur.execute(
                """
                INSERT INTO dalton_data_task_defs (
                    job_key,
                    task_code,
                    title,
                    batch_no,
                    runner,
                    cron_expr,
                    timezone,
                    timeout_seconds,
                    no_output_timeout_seconds,
                    command_argv,
                    workdir,
                    required_secrets,
                    cos_mode,
                    notify_enabled,
                    enabled,
                    paused_by_user,
                    consecutive_failures,
                    config_version,
                    active_version,
                    updated_by,
                    audit_history,
                    updated_at
                ) VALUES (
                    %(job_key)s,
                    %(task_code)s,
                    %(title)s,
                    %(batch_no)s,
                    %(runner)s,
                    %(cron_expr)s,
                    %(timezone)s,
                    %(timeout_seconds)s,
                    %(no_output_timeout_seconds)s,
                    %(command_argv)s::jsonb,
                    %(workdir)s,
                    %(required_secrets)s::jsonb,
                    %(cos_mode)s,
                    %(notify_enabled)s,
                    %(enabled)s,
                    FALSE,
                    0,
                    1,
                    1,
                    NULL,
                    %(audit_history)s::jsonb,
                    NOW()
                )
                ON CONFLICT (job_key) DO UPDATE SET
                    task_code                 = EXCLUDED.task_code,
                    title                     = EXCLUDED.title,
                    batch_no                  = EXCLUDED.batch_no,
                    runner                    = EXCLUDED.runner,
                    cron_expr                 = EXCLUDED.cron_expr,
                    timezone                  = EXCLUDED.timezone,
                    timeout_seconds           = EXCLUDED.timeout_seconds,
                    no_output_timeout_seconds = EXCLUDED.no_output_timeout_seconds,
                    command_argv              = EXCLUDED.command_argv,
                    workdir                   = EXCLUDED.workdir,
                    required_secrets          = EXCLUDED.required_secrets,
                    updated_at                = NOW()
                """,
                {
                    "job_key": entry.job_key,
                    "task_code": entry.task_code,
                    "title": entry.title,
                    "batch_no": entry.batch_no,
                    "runner": entry.runner,
                    "cron_expr": entry.cron_expr,
                    "timezone": entry.timezone,
                    "timeout_seconds": entry.timeout_seconds,
                    "no_output_timeout_seconds": entry.no_output_timeout_seconds,
                    "command_argv": json.dumps(entry.resolved_command_argv(), ensure_ascii=False),
                    "workdir": entry.workdir,
                    "required_secrets": json.dumps(entry.secret_names, ensure_ascii=False),
                    "cos_mode": entry.cos_mode,
                    "notify_enabled": entry.notify_enabled,
                    "enabled": entry.enabled,
                    "audit_history": json.dumps(initial_audit, ensure_ascii=False),
                },
            )
            count += 1
    return count
