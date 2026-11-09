"""Ephemeral Docker container executor and path mapping for OC-1 data tasks (§2.1–§2.3, §3.2–§3.4, §4.1, §4.4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import UUID

from src.dalton.data_jobs.manifest import CONTAINER_PYTHON_BIN, DEFAULT_TIMEOUT_SECONDS
from src.dalton.data_jobs.runner_wrapper import (
    DEFAULT_SECRETS_DIR,
    DEFAULT_TMPFS_OUTBOX_DIR,
    DEFAULT_WORKSPACE_DIR,
)
from src.dalton.settings import get_dalton_backend_var_dir, get_dalton_host_var_dir

logger = logging.getLogger(__name__)

DEFAULT_TASK_CONTAINER_IMAGE = "dalton-langalpha-backend"
CONTAINER_ENTRYPOINT = "/usr/bin/tini"
WRAPPER_MODULE = "src.dalton.data_jobs.runner_wrapper"

CONTAINER_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GiB (1024m)
CONTAINER_MEMORY_SWAP_BYTES = 1024 * 1024 * 1024  # 1 GiB (no swap beyond 1024m)
CONTAINER_NANO_CPUS = int(2.0 * 1_000_000_000)  # 2.0 CPUs
CONTAINER_PIDS_LIMIT = 256
LOG_TAIL_MAX_BYTES = 16 * 1024  # 16 KiB (§3.1, §3.4.1)
CONTAINER_OUTBOX_TMPFS_OPTS = "rw,noexec,nosuid,size=16m"


@dataclass(frozen=True)
class DataTaskPathMap:
    """Path correspondence across host, backend container, and ephemeral task container (§2.1.3, §4.1.1)."""

    host_var_dir: Path
    backend_var_dir: Path
    container_workspace_dir: Path = Path(DEFAULT_WORKSPACE_DIR)
    container_secrets_dir: Path = Path(DEFAULT_SECRETS_DIR)
    container_outbox_tmp_dir: Path = Path(DEFAULT_TMPFS_OUTBOX_DIR)

    @property
    def host_data_root(self) -> Path:
        return self.host_var_dir / "openclaw-data"

    @property
    def backend_data_root(self) -> Path:
        return self.backend_var_dir / "openclaw-data"

    @property
    def host_src_dir(self) -> Path:
        """Host ``src/`` mounted read-only at ``/app/src`` in task containers.

        The backend runs the bind-mounted checkout, while the image's own
        ``/app/src`` is whatever was current when the image was built; the
        wrapper must run the same code as the backend that started it, or a
        wrapper change (outbox redaction, cache prewarm) silently waits for
        the next image rebuild. ``DALTON_HOST_SRC_DIR`` overrides the default
        ``<repo>/src`` next to ``var/dalton``.
        """
        override = os.getenv("DALTON_HOST_SRC_DIR", "").strip()
        if override:
            return Path(override)
        return self.host_var_dir.parent.parent / "src"

    def host_run_secrets_dir(self, run_id: str | UUID) -> Path:
        return self.host_var_dir / "run-secrets" / str(run_id)

    def backend_run_secrets_dir(self, run_id: str | UUID) -> Path:
        return self.backend_var_dir / "run-secrets" / str(run_id)

    def backend_run_artifacts_dir(self, run_id: str | UUID) -> Path:
        return self.backend_data_root / ".dalton-runs" / str(run_id)

    def container_outbox_file(self, run_id: str | UUID) -> str:
        """Persistent redacted outbox location on the data volume."""
        return str(self.container_workspace_dir / ".dalton-runs" / str(run_id) / "outbox.json")

    def container_staging_outbox_file(self) -> str:
        """Unredacted child staging outbox path inside container-internal tmpfs."""
        return str(self.container_outbox_tmp_dir / "outbox.json")

    def docker_binds(self, run_id: str | UUID) -> list[str]:
        return [
            f"{self.host_data_root}:{self.container_workspace_dir}:rw",
            f"{self.host_run_secrets_dir(run_id)}:{self.container_secrets_dir}:ro",
            f"{self.host_src_dir}:/app/src:ro",
        ]

    def docker_tmpfs(self) -> dict[str, str]:
        return {
            str(self.container_outbox_tmp_dir): CONTAINER_OUTBOX_TMPFS_OPTS,
        }


def build_path_map(
    *,
    host_var_dir: str | Path | None = None,
    backend_var_dir: str | Path | None = None,
) -> DataTaskPathMap:
    """Resolve the host/backend/task-container path mapping (§2.1.3, §4.1.1)."""
    resolved_host = Path(host_var_dir) if host_var_dir is not None else Path(get_dalton_host_var_dir())
    resolved_backend = (
        Path(backend_var_dir) if backend_var_dir is not None else Path(get_dalton_backend_var_dir())
    )
    return DataTaskPathMap(host_var_dir=resolved_host, backend_var_dir=resolved_backend)


def format_container_name(job_key: str, run_id: str | UUID) -> str:
    """Return ``dalton-dt-<job_key>-<run_id8>`` (§3.2.4)."""
    run_id8 = str(run_id).replace("-", "")[:8]
    return f"dalton-dt-{job_key}-{run_id8}"


def build_container_create_config(
    *,
    job_key: str,
    run_id: str | UUID,
    generation: int,
    command_argv: Sequence[str],
    workdir: str = DEFAULT_WORKSPACE_DIR,
    timeout_seconds: int | None = None,
    no_output_timeout_seconds: int | None = None,
    notify_enabled: bool = False,
    cos_mode: str = "skip",
    path_map: DataTaskPathMap | None = None,
    image: str = DEFAULT_TASK_CONTAINER_IMAGE,
) -> dict[str, Any]:
    """Build the Docker Engine ``containers.create`` payload per §2.1–§2.3."""
    paths = path_map or build_path_map()
    run_id_str = str(run_id)
    notify_dry_run = "0" if notify_enabled else "1"
    effective_timeout = (
        int(timeout_seconds) if timeout_seconds is not None else DEFAULT_TIMEOUT_SECONDS
    )

    cmd: list[str] = [
        "--",
        CONTAINER_PYTHON_BIN,
        "-m",
        WRAPPER_MODULE,
        "--run-id",
        run_id_str,
        "--job-key",
        str(job_key),
        "--workdir",
        str(workdir),
        "--workspace-dir",
        str(paths.container_workspace_dir),
        "--secrets-dir",
        str(paths.container_secrets_dir),
        "--outbox-tmp-dir",
        str(paths.container_outbox_tmp_dir),
        "--timeout-seconds",
        str(effective_timeout),
        "--notify-dry-run",
        notify_dry_run,
        "--cos-mode",
        str(cos_mode or "skip"),
    ]
    if no_output_timeout_seconds is not None:
        cmd.extend(["--no-output-timeout-seconds", str(int(no_output_timeout_seconds))])
    cmd.append("--")
    cmd.extend(str(a) for a in command_argv)

    env_map = {
        "HOME": "/Users/everflow",
        "OPENCLAW_WORKSPACE": str(paths.container_workspace_dir),
        "WORKDIR": str(paths.container_workspace_dir),
        "TZ": "America/New_York",
        "PATH": "/app/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        # The image installs the core package editable only when the backend's
        # entrypoint runs; task containers start under tini, so /app/src (where
        # ptc_agent and friends live) has to be on the path explicitly.
        "PYTHONPATH": "/app:/app/src",
        "DALTON_PYTHON_BIN": CONTAINER_PYTHON_BIN,
        "DALTON_NO_DIRECT_SEND": "1",
        "DALTON_NOTIFY_DRY_RUN": notify_dry_run,
        "DALTON_COS_UPLOAD_MODE": str(cos_mode or "skip"),
        "DALTON_OUTBOX_FILE": paths.container_staging_outbox_file(),
        "DALTON_RUN_ID": run_id_str,
        "DALTON_JOB_KEY": str(job_key),
    }

    labels = {
        "dalton.data_task": "1",
        "dalton.job_key": str(job_key),
        "dalton.run_id": run_id_str,
        "dalton.generation": str(int(generation)),
    }

    host_config: dict[str, Any] = {
        "AutoRemove": False,
        "Memory": CONTAINER_MEMORY_BYTES,
        "MemorySwap": CONTAINER_MEMORY_SWAP_BYTES,
        "NanoCpus": CONTAINER_NANO_CPUS,
        "PidsLimit": CONTAINER_PIDS_LIMIT,
        "Binds": paths.docker_binds(run_id_str),
        "Tmpfs": paths.docker_tmpfs(),
    }

    return {
        "Image": image,
        "Entrypoint": [CONTAINER_ENTRYPOINT],
        "Cmd": cmd,
        "WorkingDir": str(workdir),
        "Env": [f"{k}={v}" for k, v in env_map.items()],
        "Labels": labels,
        "HostConfig": host_config,
    }


def write_run_secrets_dir(
    run_id: str | UUID,
    secrets: Mapping[str, str] | None = None,
    *,
    path_map: DataTaskPathMap | None = None,
) -> Path:
    """Create ``<backend_var_dir>/run-secrets/<run_id>`` (`0700`) and write secret files (`0600`) (§4.4.2)."""
    paths = path_map or build_path_map()
    sec_dir = paths.backend_run_secrets_dir(run_id)
    sec_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(sec_dir, 0o700)
    if secrets:
        for name, value in secrets.items():
            if not name or "/" in name or name.startswith("."):
                continue
            file_path = sec_dir / name
            file_path.write_text(str(value), encoding="utf-8")
            os.chmod(file_path, 0o600)
    return sec_dir


def cleanup_run_secrets_dir(
    run_id: str | UUID,
    *,
    path_map: DataTaskPathMap | None = None,
) -> None:
    """Delete ``<backend_var_dir>/run-secrets/<run_id>`` after ledger commit or abort (§3.4.1, §4.4.4)."""
    paths = path_map or build_path_map()
    sec_dir = paths.backend_run_secrets_dir(run_id)
    if sec_dir.exists():
        shutil.rmtree(sec_dir, ignore_errors=True)


def truncate_log_tail(log_text: str, max_bytes: int = LOG_TAIL_MAX_BYTES) -> str:
    """Return at most the last ``max_bytes`` (16 KiB) of ``log_text`` on a valid UTF-8 boundary."""
    raw = log_text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return log_text
    sliced = raw[-max_bytes:]
    return sliced.decode("utf-8", errors="ignore")


def read_run_artifacts(
    run_id: str | UUID,
    *,
    path_map: DataTaskPathMap | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read ``outbox.json`` and ``frozen_manifest.json`` from ``<data_root>/.dalton-runs/<run_id>/``."""
    paths = path_map or build_path_map()
    art_dir = paths.backend_run_artifacts_dir(run_id)
    outbox_payload: dict[str, Any] | None = None
    frozen_manifest: dict[str, Any] | None = None

    outbox_file = art_dir / "outbox.json"
    if outbox_file.is_file():
        try:
            parsed = json.loads(outbox_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                outbox_payload = parsed
        except Exception:
            outbox_payload = None

    manifest_file = art_dir / "frozen_manifest.json"
    if manifest_file.is_file():
        try:
            parsed_manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            if isinstance(parsed_manifest, dict):
                frozen_manifest = parsed_manifest
        except Exception:
            frozen_manifest = None

    return outbox_payload, frozen_manifest


def is_container_not_found_error(exc: BaseException) -> bool:
    """Only the daemon's own 404 proves the container is gone.

    A socket or transport error must not count, or a slot could be released
    while its container still runs.
    """
    from aiodocker.exceptions import DockerError

    return isinstance(exc, DockerError) and exc.status == 404


@dataclass
class ContainerInspectionOutcome:
    """Exit metadata and logs collected from a finished task container before deletion (§3.4.1)."""

    container_id: str
    exit_code: int | None
    oom_killed: bool
    running: bool
    log_tail: str
    timeout_kind: str | None = None


class DataTaskDockerClient:
    """Typed async Docker client wrapper over ``aiodocker.Docker`` (`docker.sock`) (§2.1, §3.2–§3.4)."""

    def __init__(self, docker: Any | None = None) -> None:
        self._docker = docker

    async def _get_docker(self) -> Any:
        if self._docker is None:
            import aiodocker

            self._docker = aiodocker.Docker()
        return self._docker

    async def create_container(self, *, config: dict[str, Any], name: str) -> str:
        docker = await self._get_docker()
        container = await docker.containers.create(config=config, name=name)
        cid = getattr(container, "id", None)
        if not cid and hasattr(container, "_id"):
            cid = container._id
        return str(cid)

    async def start_container(self, container_id: str) -> None:
        docker = await self._get_docker()
        container = await docker.containers.get(container_id)
        await container.start()

    async def stop_container(self, container_id: str, *, timeout: int = 10) -> None:
        docker = await self._get_docker()
        container = await docker.containers.get(container_id)
        await container.stop(t=timeout)

    async def kill_container(self, container_id: str) -> None:
        docker = await self._get_docker()
        container = await docker.containers.get(container_id)
        await container.kill()

    async def remove_container(self, container_id: str, *, force: bool = True) -> None:
        docker = await self._get_docker()
        try:
            container = await docker.containers.get(container_id)
            await container.delete(force=force)
        except Exception as exc:
            if is_container_not_found_error(exc):
                return
            raise

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        docker = await self._get_docker()
        container = await docker.containers.get(container_id)
        info = await container.show()
        return dict(info) if isinstance(info, Mapping) else {}

    async def get_container_logs(
        self,
        container_id: str,
        *,
        since: int | None = None,
        timestamps: bool = False,
    ) -> str:
        docker = await self._get_docker()
        container = await docker.containers.get(container_id)
        log_kwargs: dict[str, Any] = {"stdout": True, "stderr": True}
        if since is not None:
            log_kwargs["since"] = int(since)
        if timestamps:
            log_kwargs["timestamps"] = True
        logs = await container.log(**log_kwargs)
        if isinstance(logs, list):
            return "".join(str(item) for item in logs)
        if isinstance(logs, bytes):
            return logs.decode("utf-8", errors="replace")
        return str(logs or "")

    async def collect_container_outcome(self, container_id: str) -> ContainerInspectionOutcome:
        info = await self.inspect_container(container_id)
        state = info.get("State") if isinstance(info.get("State"), Mapping) else {}
        running = bool(state.get("Running", False))
        raw_exit = state.get("ExitCode")
        exit_code = int(raw_exit) if raw_exit is not None else None
        oom_killed = bool(state.get("OOMKilled", False))
        raw_logs = await self.get_container_logs(container_id)
        return ContainerInspectionOutcome(
            container_id=container_id,
            exit_code=exit_code,
            oom_killed=oom_killed,
            running=running,
            log_tail=truncate_log_tail(raw_logs),
        )

    async def list_data_task_containers(self) -> list[dict[str, Any]]:
        docker = await self._get_docker()
        # aiodocker.utils.httpize JSON-encodes non-string query params (aiodocker/utils.py:158-159)
        filters: dict[str, list[str]] = {"label": ["dalton.data_task=1"]}
        containers = await docker.containers.list(all=True, filters=filters)
        results: list[dict[str, Any]] = []
        for c in containers or ():
            cid = getattr(c, "id", None) or getattr(c, "_id", "")
            meta = getattr(c, "_container", None)
            if not isinstance(meta, Mapping):
                try:
                    meta = await c.show()
                except Exception:
                    meta = {}
            state = meta.get("State") if isinstance(meta.get("State"), Mapping) else {}
            config = meta.get("Config") if isinstance(meta.get("Config"), Mapping) else {}
            labels = (
                meta.get("Labels")
                if isinstance(meta.get("Labels"), Mapping)
                else config.get("Labels")
                if isinstance(config.get("Labels"), Mapping)
                else {}
            )
            running = (
                bool(state.get("Running"))
                if state
                else str(meta.get("State", "")).lower() == "running"
            )
            results.append(
                {
                    "container_id": str(cid),
                    "running": running,
                    "labels": dict(labels),
                }
            )
        return results

    async def close(self) -> None:
        if self._docker is not None and hasattr(self._docker, "close"):
            await self._docker.close()
