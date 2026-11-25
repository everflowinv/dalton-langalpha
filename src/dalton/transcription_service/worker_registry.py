"""Worker registry and private Seed-ASR 2.0 extension point for ``dalton-transcription``.

Open-source Standalone Core ships with:
- ``whisper``: :class:`OpenAICompatibleWhisperWorker`
- ``fake``: :class:`FakeAsrWorker` (deterministic test/dry-run worker)

Private / commercial workers (such as Volcengine Seed-ASR 2.0) plug in via:
1. Programmatic registration: :func:`register_worker_factory`
2. Dynamic factory spec: ``DALTON_ASR_WORKER_FACTORY="module.path:factory_fn"``
   where ``factory_fn(jobs_dir=..., atomic_write_fn=..., environ=...)`` returns
   an :class:`AsrWorkerProtocol` instance.
3. Built-in lazy host adapter (``seed_asr``): loads
   ``scripts/dalton/asr_jobs.RealAsrWorker`` on demand ONLY when the script
   exists on disk (absent in Standalone Core release packages) and
   ``SEED_ASR_*`` / ``VOLC_TOS_*`` credentials are configured.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .jobs import FakeAsrWorker, FakeAsrWorkerConfig, atomic_write_json_locked
from .whisper_worker import OpenAICompatibleWhisperWorker, WhisperWorkerConfig
from .worker_base import AsrWorkerProtocol, is_binary_available

logger = logging.getLogger("dalton-transcription")

WorkerFactory = Callable[..., AsrWorkerProtocol]

_WORKER_FACTORIES: dict[str, WorkerFactory] = {}


class UnconfiguredAsrWorker:
    """Fail-closed placeholder worker when no real ASR provider is configured in ``auto`` mode."""

    worker_name = "unconfigured"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason or (
            "No transcription worker is configured. Set DALTON_WHISPER_API_KEY / OPENAI_API_KEY "
            "(for Whisper), register a private Seed-ASR worker via DALTON_ASR_WORKER_FACTORY, "
            "or set DALTON_ASR_WORKER_MODE=fake for testing."
        )

    async def cleanup_job_resources(
        self,
        job_id: str,
        segments: list[dict[str, Any]] | None = None,
    ) -> None:
        return None

    async def run_job(
        self,
        job_state: dict[str, Any],
        *,
        on_progress: Callable[[dict[str, Any]], Awaitable[bool | None]],
    ) -> dict[str, Any]:
        caller = dict(job_state.get("caller") or {})
        attempt = int(job_state.get("attempt") or 1)
        return {
            "status": "failed",
            "stage": "failed",
            "segment_count": 1,
            "completed_segments": 0,
            "current_segment": None,
            "provider_request_ids": [],
            "segments": [],
            "usage": {
                "user_id": caller.get("user_id", ""),
                "workspace_id": caller.get("workspace_id", ""),
                "thread_id": caller.get("thread_id", ""),
                "duration_seconds": 0.0,
                "submitted_seconds": 0.0,
                "completed_seconds": 0.0,
                "billable_seconds": None,
                "attempt": attempt,
            },
            "error_category": "asr_worker_not_configured",
            "error_message": self.reason,
        }


def register_worker_factory(name: str, factory: WorkerFactory) -> None:
    """Register a named worker extension factory (e.g. ``seed_asr``)."""
    key = str(name or "").strip().lower()
    if not key:
        raise ValueError("Worker factory name must be non-empty")
    _WORKER_FACTORIES[key] = factory


def unregister_worker_factory(name: str) -> None:
    """Remove a previously registered worker factory (primarily for tests)."""
    _WORKER_FACTORIES.pop(str(name or "").strip().lower(), None)


def list_registered_worker_factories() -> tuple[str, ...]:
    """Return sorted names of explicitly registered custom worker factories."""
    return tuple(sorted(_WORKER_FACTORIES.keys()))


def load_worker_factory_from_spec(spec: str) -> WorkerFactory:
    """Import a callable worker factory from ``'package.module:callable_name'``."""
    cleaned = str(spec or "").strip()
    if ":" not in cleaned:
        raise ValueError(
            f"Invalid DALTON_ASR_WORKER_FACTORY={spec!r}; expected 'module.path:callable'"
        )
    mod_name, attr_name = cleaned.split(":", 1)
    mod_name = mod_name.strip()
    attr_name = attr_name.strip()
    if not mod_name or not attr_name:
        raise ValueError(
            f"Invalid DALTON_ASR_WORKER_FACTORY={spec!r}; expected 'module.path:callable'"
        )
    module = importlib.import_module(mod_name)
    factory = getattr(module, attr_name, None)
    if not callable(factory):
        raise TypeError(
            f"Worker factory {cleaned!r} resolved to non-callable {type(factory).__name__}"
        )
    return factory


SEED_ASR_REQUIRED_ENV_KEYS: tuple[str, ...] = (
    "SEED_ASR_APP_ID",
    "SEED_ASR_ACCESS_KEY",
    "VOLC_TOS_AK",
    "VOLC_TOS_SK",
    "VOLC_TOS_BUCKET",
)

_SEED_ASR_HINT_KEYS: tuple[str, ...] = (
    *SEED_ASR_REQUIRED_ENV_KEYS,
    "VOLC_TOS_ENDPOINT",
    "VOLC_TOS_REGION",
)

_WHISPER_HINT_KEYS: tuple[str, ...] = (
    "DALTON_WHISPER_API_BASE_URL",
    "DALTON_WHISPER_API_KEY",
    "DALTON_WHISPER_ALLOW_NO_AUTH",
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class WorkerStartupValidation:
    """Result of pre-startup validation for the active transcription worker."""

    ok: bool
    worker_mode: str
    adapter: str
    missing_config: tuple[str, ...] = ()


def _is_valid_http_url(raw: str) -> bool:
    cleaned = str(raw or "").strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return False
    parsed = urlparse(cleaned)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _is_valid_tos_endpoint(raw: str) -> bool:
    cleaned = str(raw or "").strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return False
    candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
    parsed = urlparse(candidate)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _has_builtin_seed_asr_script() -> bool:
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / "scripts" / "dalton" / "asr_jobs.py").is_file()


def _check_binary_available(
    name: str,
    env: Mapping[str, str],
    *,
    use_env_path: bool,
) -> bool:
    if use_env_path and "PATH" in env:
        return shutil.which(name, path=str(env.get("PATH") or "")) is not None
    return is_binary_available(name)


def _missing_source_storage_keys(env: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    endpoint = str(
        env.get("DALTON_ASR_STORAGE_ENDPOINT_URL")
        or env.get("STORAGE_ENDPOINT_URL")
        or env.get("S3_ENDPOINT_URL")
        or ""
    ).strip()
    if not _is_valid_http_url(endpoint):
        missing.append("DALTON_ASR_STORAGE_ENDPOINT_URL")
    bucket = str(
        env.get("DALTON_ASR_STORAGE_BUCKET_NAME")
        or env.get("STORAGE_BUCKET_NAME")
        or env.get("S3_BUCKET_NAME")
        or ""
    ).strip()
    if not bucket:
        missing.append("DALTON_ASR_STORAGE_BUCKET_NAME")
    ak = str(
        env.get("DALTON_ASR_STORAGE_ACCESS_KEY_ID")
        or env.get("STORAGE_ACCESS_KEY_ID")
        or env.get("MINIO_ROOT_USER")
        or env.get("AWS_ACCESS_KEY_ID")
        or ""
    ).strip()
    if not ak:
        missing.append("DALTON_ASR_STORAGE_ACCESS_KEY_ID")
    sk = str(
        env.get("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY")
        or env.get("STORAGE_SECRET_ACCESS_KEY")
        or env.get("MINIO_ROOT_PASSWORD")
        or env.get("AWS_SECRET_ACCESS_KEY")
        or ""
    ).strip()
    if not sk:
        missing.append("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY")
    return missing


def _missing_seed_asr_keys(env: Mapping[str, str], *, factory_spec: str) -> list[str]:
    missing: list[str] = [
        k for k in SEED_ASR_REQUIRED_ENV_KEYS if not str(env.get(k) or "").strip()
    ]
    raw_tos_ep = str(env.get("VOLC_TOS_ENDPOINT") or "").strip()
    if raw_tos_ep and not _is_valid_tos_endpoint(raw_tos_ep):
        missing.append("VOLC_TOS_ENDPOINT")
    if (
        "seed_asr" not in _WORKER_FACTORIES
        and not factory_spec
        and not _has_builtin_seed_asr_script()
    ):
        missing.append("seed_asr_adapter")
    missing.extend(_missing_source_storage_keys(env))
    return missing


def _missing_whisper_keys(env: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    allow_no_auth = str(env.get("DALTON_WHISPER_ALLOW_NO_AUTH") or "").strip().lower() in _TRUTHY
    raw_base = str(env.get("DALTON_WHISPER_API_BASE_URL") or "").strip()
    effective_base = raw_base or "https://api.openai.com/v1"
    if allow_no_auth and not raw_base:
        missing.append("DALTON_WHISPER_API_BASE_URL")
    elif not _is_valid_http_url(effective_base):
        missing.append("DALTON_WHISPER_API_BASE_URL")

    api_key = str(
        env.get("DALTON_WHISPER_API_KEY") or env.get("OPENAI_API_KEY") or ""
    ).strip()
    if not allow_no_auth and not api_key:
        missing.append("DALTON_WHISPER_API_KEY")
    missing.extend(_missing_source_storage_keys(env))
    return missing


def _missing_binaries(env: Mapping[str, str], *, use_env_path: bool) -> list[str]:
    missing: list[str] = []
    for binary in ("ffmpeg", "ffprobe"):
        if not _check_binary_available(binary, env, use_env_path=use_env_path):
            missing.append(binary)
    return missing


def _dedupe_names(names: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return tuple(out)


def validate_worker_startup_config(
    *,
    mode: str | None = None,
    custom_worker: AsrWorkerProtocol | None = None,
    environ: Mapping[str, str] | None = None,
) -> WorkerStartupValidation:
    """Validate required adapter config and ``ffmpeg``/``ffprobe`` before lock/recovery.

    Returns only missing configuration key names (never secret values).
    """
    env = os.environ if environ is None else environ
    use_env_path = environ is not None
    eff_mode = (
        str(mode if mode is not None else env.get("DALTON_ASR_WORKER_MODE", "auto"))
        .strip()
        .lower()
        or "auto"
    )

    if custom_worker is not None:
        return WorkerStartupValidation(
            ok=True,
            worker_mode=eff_mode,
            adapter=str(getattr(custom_worker, "worker_name", type(custom_worker).__name__)),
        )

    if eff_mode == "fake":
        return WorkerStartupValidation(ok=True, worker_mode="fake", adapter="fake")

    factory_spec = str(env.get("DALTON_ASR_WORKER_FACTORY") or "").strip()

    if eff_mode == "whisper":
        missing = _missing_whisper_keys(env) + _missing_binaries(env, use_env_path=use_env_path)
        deduped = _dedupe_names(missing)
        return WorkerStartupValidation(
            ok=not deduped,
            worker_mode=eff_mode,
            adapter="whisper",
            missing_config=deduped,
        )

    if eff_mode in ("seed_asr", "real"):
        missing = _missing_seed_asr_keys(env, factory_spec=factory_spec) + _missing_binaries(
            env, use_env_path=use_env_path
        )
        deduped = _dedupe_names(missing)
        return WorkerStartupValidation(
            ok=not deduped,
            worker_mode=eff_mode,
            adapter="seed_asr",
            missing_config=deduped,
        )

    if eff_mode == "custom":
        missing: list[str] = []
        if not factory_spec:
            missing.append("DALTON_ASR_WORKER_FACTORY")
        else:
            try:
                load_worker_factory_from_spec(factory_spec)
            except Exception:
                missing.append("DALTON_ASR_WORKER_FACTORY")
        missing.extend(_missing_binaries(env, use_env_path=use_env_path))
        deduped = _dedupe_names(missing)
        return WorkerStartupValidation(
            ok=not deduped,
            worker_mode=eff_mode,
            adapter="custom",
            missing_config=deduped,
        )

    if eff_mode in _WORKER_FACTORIES:
        missing = _missing_binaries(env, use_env_path=use_env_path)
        deduped = _dedupe_names(missing)
        return WorkerStartupValidation(
            ok=not deduped,
            worker_mode=eff_mode,
            adapter=eff_mode,
            missing_config=deduped,
        )

    if eff_mode != "auto":
        return WorkerStartupValidation(
            ok=False,
            worker_mode=eff_mode,
            adapter="unknown",
            missing_config=("DALTON_ASR_WORKER_MODE",),
        )

    # Auto mode: determine the intended adapter before taking `.owner.lock` or recovering jobs.
    if factory_spec:
        missing = []
        try:
            load_worker_factory_from_spec(factory_spec)
        except Exception:
            missing.append("DALTON_ASR_WORKER_FACTORY")
        missing.extend(_missing_binaries(env, use_env_path=use_env_path))
        deduped = _dedupe_names(missing)
        return WorkerStartupValidation(
            ok=not deduped,
            worker_mode=eff_mode,
            adapter="custom",
            missing_config=deduped,
        )

    if env.get("DALTON_ASR_FAKE_TEXT") or str(env.get("DALTON_ASR_FAKE_FAIL_CATEGORY") or "").strip():
        return WorkerStartupValidation(ok=True, worker_mode=eff_mode, adapter="fake")

    has_seed_hint = any(str(env.get(k) or "").strip() for k in _SEED_ASR_HINT_KEYS)
    has_whisper_hint = any(str(env.get(k) or "").strip() for k in _WHISPER_HINT_KEYS)
    prefer_seed_asr = (
        "seed_asr" in _WORKER_FACTORIES
        or has_seed_hint
        or (
            _has_builtin_seed_asr_script()
            and not has_whisper_hint
            and not WhisperWorkerConfig.is_configured(env)
        )
    )
    if prefer_seed_asr:
        missing = _missing_seed_asr_keys(env, factory_spec="") + _missing_binaries(
            env, use_env_path=use_env_path
        )
        deduped = _dedupe_names(missing)
        return WorkerStartupValidation(
            ok=not deduped,
            worker_mode=eff_mode,
            adapter="seed_asr",
            missing_config=deduped,
        )

    missing = _missing_whisper_keys(env) + _missing_binaries(env, use_env_path=use_env_path)
    deduped = _dedupe_names(missing)
    return WorkerStartupValidation(
        ok=not deduped,
        worker_mode=eff_mode,
        adapter="whisper",
        missing_config=deduped,
    )


def _seed_asr_env_configured(environ: Mapping[str, str]) -> bool:
    return all(bool(str(environ.get(k) or "").strip()) for k in SEED_ASR_REQUIRED_ENV_KEYS)


def _try_build_builtin_seed_asr_worker(
    jobs_dir: Path,
    *,
    atomic_write_fn: Callable[..., bool] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AsrWorkerProtocol | None:
    """Lazy host adapter loading ``scripts/dalton/asr_jobs.RealAsrWorker`` if present.

    Returns ``None`` in Standalone Core installations where ``scripts/dalton/asr_jobs.py``
    is excluded from the release package.
    """
    env = os.environ if environ is None else environ
    if not _seed_asr_env_configured(env):
        return None
    repo_root = Path(__file__).resolve().parents[3]
    bridge_dir = repo_root / "scripts" / "dalton"
    asr_jobs_file = bridge_dir / "asr_jobs.py"
    if not asr_jobs_file.is_file():
        return None
    if str(bridge_dir) not in sys.path:
        sys.path.insert(0, str(bridge_dir))
    asr_jobs_mod = importlib.import_module("asr_jobs")
    real_worker_cls = getattr(asr_jobs_mod, "RealAsrWorker")
    worker = real_worker_cls(
        jobs_dir,
        atomic_write_fn=atomic_write_fn or atomic_write_json_locked,
    )
    setattr(worker, "worker_name", "seed_asr")
    return worker


def _build_fake_worker_from_env(environ: Mapping[str, str]) -> FakeAsrWorker:
    try:
        delay_s = max(0.0, float(environ.get("DALTON_ASR_FAKE_DELAY_S", "0.02") or 0.02))
    except ValueError:
        delay_s = 0.02
    fixed_text = environ.get("DALTON_ASR_FAKE_TEXT") or None
    fail_cat = str(environ.get("DALTON_ASR_FAKE_FAIL_CATEGORY") or "").strip() or None
    return FakeAsrWorker(
        FakeAsrWorkerConfig(
            delay_s=delay_s,
            fixed_text=fixed_text,
            fail_category=fail_cat,
        )
    )


def resolve_worker(
    jobs_dir: Path,
    *,
    mode: str | None = None,
    custom_worker: AsrWorkerProtocol | None = None,
    atomic_write_fn: Callable[..., bool] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AsrWorkerProtocol:
    """Resolve the active transcription worker according to mode, extensions, and env."""
    if custom_worker is not None:
        return custom_worker

    env = os.environ if environ is None else environ
    eff_mode = (
        str(mode if mode is not None else env.get("DALTON_ASR_WORKER_MODE", "auto"))
        .strip()
        .lower()
        or "auto"
    )
    write_fn = atomic_write_fn or atomic_write_json_locked

    if eff_mode == "fake":
        return _build_fake_worker_from_env(env)

    factory_spec = str(env.get("DALTON_ASR_WORKER_FACTORY") or "").strip()

    if eff_mode == "whisper":
        return OpenAICompatibleWhisperWorker(
            jobs_dir,
            config=WhisperWorkerConfig.from_env(env),
            atomic_write_fn=write_fn,
        )

    if eff_mode in ("seed_asr", "real"):
        if "seed_asr" in _WORKER_FACTORIES:
            return _WORKER_FACTORIES["seed_asr"](
                jobs_dir=jobs_dir, atomic_write_fn=write_fn, environ=env
            )
        if factory_spec:
            factory = load_worker_factory_from_spec(factory_spec)
            return factory(jobs_dir=jobs_dir, atomic_write_fn=write_fn, environ=env)
        builtin_seed = _try_build_builtin_seed_asr_worker(
            jobs_dir, atomic_write_fn=write_fn, environ=env
        )
        if builtin_seed is not None:
            return builtin_seed
        return UnconfiguredAsrWorker(
            "Seed-ASR worker requested but private Seed-ASR implementation or credentials are unavailable."
        )

    if eff_mode in _WORKER_FACTORIES:
        return _WORKER_FACTORIES[eff_mode](
            jobs_dir=jobs_dir, atomic_write_fn=write_fn, environ=env
        )

    if eff_mode == "custom":
        if not factory_spec:
            raise ValueError(
                "DALTON_ASR_WORKER_MODE=custom requires DALTON_ASR_WORKER_FACTORY='module:callable'"
            )
        factory = load_worker_factory_from_spec(factory_spec)
        return factory(jobs_dir=jobs_dir, atomic_write_fn=write_fn, environ=env)

    # Auto mode: explicit factory -> fake env overrides -> Seed-ASR (if registered or available+configured) -> Whisper -> unconfigured
    if factory_spec:
        factory = load_worker_factory_from_spec(factory_spec)
        return factory(jobs_dir=jobs_dir, atomic_write_fn=write_fn, environ=env)

    if env.get("DALTON_ASR_FAKE_TEXT") or str(env.get("DALTON_ASR_FAKE_FAIL_CATEGORY") or "").strip():
        return _build_fake_worker_from_env(env)

    if "seed_asr" in _WORKER_FACTORIES and _seed_asr_env_configured(env):
        return _WORKER_FACTORIES["seed_asr"](
            jobs_dir=jobs_dir, atomic_write_fn=write_fn, environ=env
        )

    builtin_seed = _try_build_builtin_seed_asr_worker(
        jobs_dir, atomic_write_fn=write_fn, environ=env
    )
    if builtin_seed is not None:
        return builtin_seed

    if WhisperWorkerConfig.is_configured(env):
        return OpenAICompatibleWhisperWorker(
            jobs_dir,
            config=WhisperWorkerConfig.from_env(env),
            atomic_write_fn=write_fn,
        )

    return UnconfiguredAsrWorker()


__all__ = [
    "SEED_ASR_REQUIRED_ENV_KEYS",
    "UnconfiguredAsrWorker",
    "WorkerFactory",
    "WorkerStartupValidation",
    "list_registered_worker_factories",
    "load_worker_factory_from_spec",
    "register_worker_factory",
    "resolve_worker",
    "unregister_worker_factory",
    "validate_worker_startup_config",
]
