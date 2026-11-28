"""HTTP service and lifecycle runtime for ``dalton-transcription`` (default ``:8965``).

Lifecycle & ownership contract (§6.3):
1. When ``disabled=True`` (``DALTON_TRANSCRIPTION_DISABLED=true``):
   - Skips acquiring ``<DALTON_ASR_JOBS_DIR>/.owner.lock``.
   - Skips constructing ``AsrJobManager`` and calling ``recover_incomplete_jobs()``.
   - Every ``/transcriptions*`` route returns ``503 Service Unavailable``.
2. When enabled:
   - Acquires ``<DALTON_ASR_JOBS_DIR>/.owner.lock`` FIRST (fail-fast with PID
     diagnostics on timeout).
   - ONLY after holding ``.owner.lock`` constructs ``AsrJobManager``, runs
     ``recover_incomplete_jobs()``, and accepts requests.
3. Drain control:
   - ``POST /transcriptions/drain`` (authenticated) or POSIX ``SIGUSR1`` puts the
     service into draining mode.
   - While draining, new submissions (``POST /transcriptions``) and retries
     (``POST /transcriptions/{id}/retry``) return ``503`` with
     ``error_code="service_draining"``; status, result, raw download, and cancel
     remain available for in-flight jobs.
   - ``GET /healthz`` and ``GET /transcriptions/drain`` report ``draining``,
     ``active_jobs``, ``active_job_ids``, and ``ready_for_handoff``.
4. Authentication:
   - Strictly validates ``DALTON_TRANSCRIPTION_TOKEN`` (via
     ``Authorization: Bearer <token>`` or ``X-Dalton-Transcription-Token``).
   - Fail-closed: empty/unset token rejects all ``/transcriptions*`` requests
     with ``401 Unauthorized``. Never accepts bridge tokens.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .jobs import AsrJobManager
from .owner_lock import (
    DEFAULT_OWNER_LOCK_TIMEOUT_S,
    AsrOwnerLock,
    OwnerLockBusyError,
    resolve_owner_lock_timeout_s,
)
from .worker_base import AsrWorkerProtocol, is_binary_available
from .worker_registry import resolve_worker, validate_worker_startup_config

logger = logging.getLogger("dalton-transcription")

DEFAULT_TRANSCRIPTION_HOST: str = "127.0.0.1"
DEFAULT_TRANSCRIPTION_PORT: int = 8965
DEFAULT_SHUTDOWN_GRACE_S: float = 10.0
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def default_asr_jobs_dir() -> Path:
    """Default ASR jobs directory matching legacy ``researchdata_bridge.py`` (``<repo>/var/dalton/asr_jobs``)."""
    return Path(__file__).resolve().parents[3] / "var" / "dalton" / "asr_jobs"


def _parse_bool(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY


@dataclass
class TranscriptionServiceConfig:
    """Runtime configuration for ``dalton-transcription``."""

    jobs_dir: Path
    usage_ledger_path: Path | None = None
    token: str = ""
    host: str = DEFAULT_TRANSCRIPTION_HOST
    port: int = DEFAULT_TRANSCRIPTION_PORT
    worker_mode: str = "auto"
    disabled: bool = False
    lock_timeout_s: float = DEFAULT_OWNER_LOCK_TIMEOUT_S
    shutdown_grace_s: float = DEFAULT_SHUTDOWN_GRACE_S

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> TranscriptionServiceConfig:
        env = os.environ if environ is None else environ
        raw_jobs_dir = str(env.get("DALTON_ASR_JOBS_DIR") or "").strip()
        jobs_dir = (
            Path(raw_jobs_dir).expanduser() if raw_jobs_dir else default_asr_jobs_dir()
        )
        raw_usage = str(env.get("DALTON_ASR_USAGE_FILE") or "").strip()
        usage_path = Path(raw_usage).expanduser() if raw_usage else None
        token = str(env.get("DALTON_TRANSCRIPTION_TOKEN") or "").strip()
        host = (
            str(env.get("DALTON_TRANSCRIPTION_HOST") or DEFAULT_TRANSCRIPTION_HOST).strip()
            or DEFAULT_TRANSCRIPTION_HOST
        )
        try:
            port = int(env.get("DALTON_TRANSCRIPTION_PORT") or DEFAULT_TRANSCRIPTION_PORT)
        except ValueError:
            port = DEFAULT_TRANSCRIPTION_PORT
        worker_mode = (
            str(env.get("DALTON_ASR_WORKER_MODE") or "auto").strip().lower() or "auto"
        )
        disabled = _parse_bool(env.get("DALTON_TRANSCRIPTION_DISABLED"))

        raw_lock_to = str(env.get("DALTON_ASR_LOCK_TIMEOUT_S") or "").strip()
        if raw_lock_to:
            try:
                lock_timeout_s = max(0.0, float(raw_lock_to))
            except ValueError:
                lock_timeout_s = DEFAULT_OWNER_LOCK_TIMEOUT_S
        else:
            lock_timeout_s = resolve_owner_lock_timeout_s()

        raw_grace = str(env.get("DALTON_TRANSCRIPTION_SHUTDOWN_GRACE_S") or "").strip()
        try:
            shutdown_grace_s = max(0.0, float(raw_grace)) if raw_grace else DEFAULT_SHUTDOWN_GRACE_S
        except ValueError:
            shutdown_grace_s = DEFAULT_SHUTDOWN_GRACE_S

        return cls(
            jobs_dir=jobs_dir,
            usage_ledger_path=usage_path,
            token=token,
            host=host,
            port=port,
            worker_mode=worker_mode,
            disabled=disabled,
            lock_timeout_s=lock_timeout_s,
            shutdown_grace_s=shutdown_grace_s,
        )


def _extract_transcription_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    explicit = request.headers.get("x-dalton-transcription-token", "").strip()
    if explicit:
        return explicit
    return ""


class TranscriptionRuntime:
    """Owns the exclusive ``.owner.lock``, ``AsrJobManager``, and drain state."""

    def __init__(
        self,
        config: TranscriptionServiceConfig,
        *,
        worker: AsrWorkerProtocol | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self._custom_worker = worker
        self._environ = dict(environ) if environ is not None else None
        self._owner_lock: AsrOwnerLock | None = None
        self._manager: AsrJobManager | None = None
        self._draining: bool = False
        self._misconfigured: bool = False
        self._missing_config: list[str] = []
        self._expected_adapter: str = ""
        self._started: bool = False
        self._installed_usr1: bool = False

    @property
    def disabled(self) -> bool:
        return bool(self.config.disabled)

    @property
    def misconfigured(self) -> bool:
        return bool(self._misconfigured)

    @property
    def missing_config(self) -> tuple[str, ...]:
        return tuple(self._missing_config)

    @property
    def draining(self) -> bool:
        return bool(self._draining)

    @property
    def owner_lock(self) -> AsrOwnerLock | None:
        return self._owner_lock

    @property
    def manager(self) -> AsrJobManager | None:
        return self._manager

    def begin_drain(self) -> dict[str, Any]:
        """Stop accepting new submissions/retries while letting in-flight jobs finish."""
        if not self._draining:
            logger.info(
                "dalton-transcription entering drain mode (active_jobs=%d)",
                len(self.active_job_ids()),
            )
        self._draining = True
        return self.drain_status()

    def resume_intake(self) -> dict[str, Any]:
        """Leave drain mode and resume accepting new transcription jobs."""
        self._draining = False
        return self.drain_status()

    def active_job_ids(self) -> list[str]:
        if self._manager is None:
            return []
        return self._manager.active_job_ids()

    def drain_status(self) -> dict[str, Any]:
        active = self.active_job_ids()
        lock_held = bool(self._owner_lock and self._owner_lock.is_held)
        lock_pid = (
            self._owner_lock.metadata.pid
            if self._owner_lock and self._owner_lock.metadata
            else None
        )
        return {
            "ok": True,
            "disabled": self.disabled,
            "draining": self._draining,
            "owner_lock_held": lock_held,
            "owner_lock_pid": lock_pid,
            "active_jobs": len(active),
            "active_job_ids": active,
            "ready_for_handoff": bool((self._draining or self.disabled) and len(active) == 0),
        }

    def health_payload(self) -> dict[str, Any]:
        active = self.active_job_ids()
        lock_held = bool(self._owner_lock and self._owner_lock.is_held)
        lock_pid = (
            self._owner_lock.metadata.pid
            if self._owner_lock and self._owner_lock.metadata
            else None
        )
        if self.disabled:
            status = "disabled"
        elif self._misconfigured:
            status = "misconfigured"
        elif self._draining:
            status = "draining"
        else:
            status = "ok"
        worker_name = (
            getattr(self._manager.worker, "worker_name", type(self._manager.worker).__name__)
            if self._manager is not None
            else ("unconfigured" if self._misconfigured else "none")
        )
        return {
            "ok": not self._misconfigured,
            "service": "dalton-transcription",
            "status": status,
            "disabled": self.disabled,
            "misconfigured": self._misconfigured,
            "missing_config": list(self._missing_config),
            "draining": self._draining,
            "owner_lock_held": lock_held,
            "owner_lock_pid": lock_pid,
            "active_jobs": len(active),
            "ready_for_handoff": bool((self._draining or self.disabled) and len(active) == 0),
            "worker_mode": self.config.worker_mode,
            "worker_name": worker_name,
            "ffmpeg_available": is_binary_available("ffmpeg"),
            "ffprobe_available": is_binary_available("ffprobe"),
        }

    def startup(self) -> list[str]:
        """Validate worker config, acquire ``.owner.lock``, then construct ``AsrJobManager`` and recover jobs.

        When ``config.disabled`` is True or required adapter configuration / binaries are
        missing, skips lock acquisition, manager construction, and job recovery.
        Raises :class:`OwnerLockBusyError` before constructing ``AsrJobManager`` if the
        lock is held by another owner.
        """
        if self._started:
            return []
        if self.config.disabled:
            logger.info(
                "dalton-transcription started in disabled mode (DALTON_TRANSCRIPTION_DISABLED=true); "
                "skipping .owner.lock and AsrJobManager startup recovery"
            )
            self._started = True
            return []

        validation = validate_worker_startup_config(
            mode=self.config.worker_mode,
            custom_worker=self._custom_worker,
            environ=self._environ,
        )
        if not validation.ok:
            self._misconfigured = True
            self._missing_config = list(validation.missing_config)
            self._expected_adapter = validation.adapter
            self._started = True
            logger.error(
                "dalton-transcription startup blocked (status=misconfigured, adapter=%s): "
                "missing required configuration: %s; skipping .owner.lock and job recovery",
                validation.adapter,
                ", ".join(self._missing_config),
            )
            return []

        self._misconfigured = False
        self._missing_config = []
        self._expected_adapter = validation.adapter

        lock = AsrOwnerLock(
            self.config.jobs_dir,
            service_name="dalton-transcription",
        )
        lock.acquire(timeout_s=self.config.lock_timeout_s)
        try:
            worker = resolve_worker(
                self.config.jobs_dir,
                mode=self.config.worker_mode,
                custom_worker=self._custom_worker,
                environ=self._environ,
            )
            mgr = AsrJobManager(
                self.config.jobs_dir,
                worker=worker,
                usage_ledger_path=self.config.usage_ledger_path,
                owner_lock=lock,
                auto_recover=False,
            )
            resumed = mgr.recover_incomplete_jobs()
        except BaseException:
            lock.release()
            raise

        self._owner_lock = lock
        self._manager = mgr
        self._started = True
        self._install_drain_signal_handler()
        logger.info(
            "dalton-transcription acquired %s (pid=%s, recovered=%d, worker=%s)",
            lock.lock_path,
            lock.metadata.pid if lock.metadata else os.getpid(),
            len(resumed),
            getattr(worker, "worker_name", type(worker).__name__),
        )
        return resumed

    def _install_drain_signal_handler(self) -> None:
        if not hasattr(signal, "SIGUSR1"):
            return
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGUSR1, self.begin_drain)
            self._installed_usr1 = True
        except (NotImplementedError, RuntimeError, ValueError):
            self._installed_usr1 = False

    async def shutdown(self) -> None:
        """Enter drain mode, wait/cancel active tasks cleanly, and release ``.owner.lock``."""
        self._draining = True
        if self._installed_usr1 and hasattr(signal, "SIGUSR1"):
            try:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGUSR1)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            self._installed_usr1 = False
        if self._manager is not None:
            try:
                await self._manager.shutdown(grace_s=self.config.shutdown_grace_s)
            except Exception as exc:
                logger.warning("dalton-transcription shutdown warning: %s", type(exc).__name__)
        if self._owner_lock is not None:
            self._owner_lock.release()
            self._owner_lock = None
        self._manager = None
        self._misconfigured = False
        self._missing_config = []
        self._expected_adapter = ""
        self._started = False


def create_app(
    *,
    config: TranscriptionServiceConfig | None = None,
    worker: AsrWorkerProtocol | None = None,
    environ: Mapping[str, str] | None = None,
    runtime: TranscriptionRuntime | None = None,
) -> FastAPI:
    """Create the FastAPI app for ``dalton-transcription``."""
    cfg = config or TranscriptionServiceConfig.from_env(environ)
    rt = runtime or TranscriptionRuntime(cfg, worker=worker, environ=environ)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # uvloop spawns ffprobe/ffmpeg without close_fds; mark listening sockets
        # non-inheritable before startup can resume transcription jobs.
        from src.server.utils.child_fds import keep_descriptors_from_children

        keep_descriptors_from_children()
        rt.startup()
        try:
            yield
        finally:
            await rt.shutdown()

    app = FastAPI(
        title="dalton-transcription",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.runtime = rt

    def _expected_token() -> str:
        # Honor config token if explicitly supplied; otherwise read current env dynamically
        # so runtime token injection in tests works consistently.
        if cfg.token:
            return cfg.token
        return os.environ.get("DALTON_TRANSCRIPTION_TOKEN", "").strip()

    def _require_transcription_token(request: Request) -> None:
        expected = _expected_token()
        supplied = _extract_transcription_token(request)
        if not expected or not supplied or not hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
            )

    def _require_manager(*, intake: bool = False) -> AsrJobManager:
        if rt.disabled:
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "error_code": "service_disabled",
                    "error": "Transcription service is disabled (DALTON_TRANSCRIPTION_DISABLED=true)",
                },
            )
        if rt.misconfigured:
            missing = list(rt.missing_config)
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "error_code": "service_misconfigured",
                    "missing_config": missing,
                    "error": (
                        "Transcription service is misconfigured; missing required configuration: "
                        + ", ".join(missing)
                    ),
                },
            )
        if intake and rt.draining:
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "error_code": "service_draining",
                    "draining": True,
                    "error": "Transcription service is draining and not accepting new jobs",
                },
            )
        if rt.manager is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "ok": False,
                    "error_code": "service_not_ready",
                    "error": "Transcription service is not initialized",
                },
            )
        return rt.manager

    @app.exception_handler(HTTPException)
    async def _http_exc_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "error": str(exc.detail)},
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return rt.health_payload()

    @app.get("/transcriptions/drain")
    async def get_drain_status(
        _: None = Depends(_require_transcription_token),
    ) -> dict[str, Any]:
        return rt.drain_status()

    @app.post("/transcriptions/drain")
    async def set_drain_status(
        request: Request,
        _: None = Depends(_require_transcription_token),
    ) -> dict[str, Any]:
        drain_flag = True
        raw_body = await request.body()
        if raw_body.strip():
            try:
                parsed = await request.json()
            except Exception as exc:
                raise HTTPException(status_code=400, detail="Parse error") from exc
            if not isinstance(parsed, dict):
                raise HTTPException(status_code=400, detail="Expected JSON object")
            if "drain" in parsed:
                drain_flag = bool(parsed["drain"])
        return rt.begin_drain() if drain_flag else rt.resume_intake()

    @app.post("/transcriptions")
    async def submit_transcription(
        request: Request,
        _: None = Depends(_require_transcription_token),
    ) -> JSONResponse:
        _require_manager(intake=True)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"ok": False, "error": "Parse error"},
            )
        mgr = _require_manager(intake=True)
        code, payload = await mgr.submit_job(body)
        return JSONResponse(status_code=code, content=payload)

    @app.get("/transcriptions/{job_id}")
    async def get_transcription_status(
        job_id: str,
        _: None = Depends(_require_transcription_token),
    ) -> JSONResponse:
        mgr = _require_manager(intake=False)
        code, payload = await mgr.get_job(job_id)
        return JSONResponse(status_code=code, content=payload)

    @app.get("/transcriptions/{job_id}/result")
    async def get_transcription_result(
        job_id: str,
        _: None = Depends(_require_transcription_token),
    ) -> JSONResponse:
        mgr = _require_manager(intake=False)
        code, payload = await mgr.get_job_result(job_id)
        return JSONResponse(status_code=code, content=payload)

    @app.get("/transcriptions/{job_id}/result/raw")
    async def get_transcription_raw_result(
        job_id: str,
        _: None = Depends(_require_transcription_token),
    ) -> Any:
        mgr = _require_manager(intake=False)
        code, payload_or_path = await mgr.get_job_raw_result(job_id)
        if code == 200 and isinstance(payload_or_path, Path):
            return FileResponse(
                path=payload_or_path,
                media_type="application/json; charset=utf-8",
            )
        err_obj = (
            payload_or_path
            if isinstance(payload_or_path, dict)
            else {"ok": False, "error": "Raw response unavailable"}
        )
        return JSONResponse(status_code=code, content=err_obj)

    @app.post("/transcriptions/{job_id}/retry")
    async def retry_transcription(
        job_id: str,
        request: Request,
        _: None = Depends(_require_transcription_token),
    ) -> JSONResponse:
        _require_manager(intake=True)
        action_body: dict[str, Any] = {}
        raw_body = await request.body()
        if raw_body.strip():
            try:
                parsed = await request.json()
                if not isinstance(parsed, dict):
                    raise ValueError("Expected JSON object")
                action_body = parsed
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "Parse error"},
                )
        mgr = _require_manager(intake=True)
        code, payload = await mgr.retry_job(job_id, action_body)
        return JSONResponse(status_code=code, content=payload)

    @app.post("/transcriptions/{job_id}/cancel")
    async def cancel_transcription(
        job_id: str,
        request: Request,
        _: None = Depends(_require_transcription_token),
    ) -> JSONResponse:
        _require_manager(intake=False)
        action_body: dict[str, Any] = {}
        raw_body = await request.body()
        if raw_body.strip():
            try:
                parsed = await request.json()
                if not isinstance(parsed, dict):
                    raise ValueError("Expected JSON object")
                action_body = parsed
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "error": "Parse error"},
                )
        mgr = _require_manager(intake=False)
        code, payload = await mgr.cancel_job(job_id, action_body)
        return JSONResponse(status_code=code, content=payload)

    return app


__all__ = [
    "DEFAULT_SHUTDOWN_GRACE_S",
    "DEFAULT_TRANSCRIPTION_HOST",
    "DEFAULT_TRANSCRIPTION_PORT",
    "OwnerLockBusyError",
    "TranscriptionRuntime",
    "TranscriptionServiceConfig",
    "create_app",
    "default_asr_jobs_dir",
]
