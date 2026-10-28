"""OpenClaw model catalog sync, reference protection, status, and health service (P0.7 §3)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from src.config.env import HOST_IP, _IN_DOCKER
from src.dalton.database.openclaw_sync import (
    create_sync_run,
    fetch_recent_sync_runs,
    find_user_referenced_models,
    finish_sync_run,
    release_sync_lock,
    try_acquire_sync_lock,
)
from src.dalton.llm.openclaw_catalog import (
    OVERLAY_PATH,
    REPO_ROOT,
    build_overlay,
    diff_overlay,
    load_overrides,
    public_extract_sha256,
    write_overlay_atomic,
)
from src.dalton.llm.overlay import current_overlay_stamp, load_overlay_data
from src.dalton.llm.role_policy import check_roles
from src.dalton.settings import (
    get_dalton_broker_token,
    get_dalton_broker_url,
    get_dalton_settings,
    get_pm_user_ids_source,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

BUILD_GIT_SHA_FILE = Path("/app/.build_git_sha")
BUILD_GIT_DIRTY_FILE = Path("/app/.build_git_dirty")
BUILD_FILES_SHA256_FILE = Path("/app/.build_files.sha256")
DOCKER_MANIFEST_WATCHED_PATHS: tuple[str, ...] = (
    "server.py",
    "config.yaml",
    "agent_config.yaml",
    "agent_config.dalton.yaml",
    "alembic.ini",
    "config/dalton",
    "libs/ptc-cli",
    "libs/market-protocol",
    "src",
    "scripts",
    "migrations",
    "mcp_servers",
    "plugins",
    "workflows",
)
_IGNORED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
_IGNORED_FILE_NAMES = frozenset({".DS_Store"})
_IGNORED_FILE_SUFFIXES = (".pyc", ".pyo")


def _read_image_build_git_sha(build_sha_file: Path = BUILD_GIT_SHA_FILE) -> str:
    if build_sha_file.is_file():
        try:
            return build_sha_file.read_text(encoding="utf-8").strip()[:8]
        except Exception:
            return ""
    return ""


def _read_image_build_git_dirty(build_dirty_file: Path = BUILD_GIT_DIRTY_FILE) -> bool:
    if build_dirty_file.is_file():
        try:
            raw = build_dirty_file.read_text(encoding="utf-8").strip().lower()
            if raw == "false":
                return False
            if raw == "true":
                return True
        except Exception:
            return True
    return True


def _is_ignored_runtime_file(rel_posix: str) -> bool:
    parts = rel_posix.split("/")
    if any(p in _IGNORED_DIR_NAMES for p in parts[:-1]):
        return True
    fname = parts[-1]
    if fname in _IGNORED_FILE_NAMES or fname.endswith(_IGNORED_FILE_SUFFIXES):
        return True
    return False


def _verify_docker_build_manifest(
    app_root: Path,
    build_files_sha256_file: Path = BUILD_FILES_SHA256_FILE,
) -> bool:
    """Return True iff `build_files_sha256_file` exists, all listed files match SHA-256, and no new files exist."""
    if not build_files_sha256_file.is_file():
        return False
    try:
        raw_lines = build_files_sha256_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False

    manifest: dict[str, str] = {}
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            return False
        digest, rel_raw = parts[0].strip().lower(), parts[1].strip()
        if rel_raw.startswith("*"):
            rel_raw = rel_raw[1:]
        while rel_raw.startswith("./"):
            rel_raw = rel_raw[2:]
        if len(digest) != 64 or not rel_raw:
            return False
        if _is_ignored_runtime_file(rel_raw):
            continue
        manifest[rel_raw] = digest

    if not manifest:
        return False

    # 1. Verify every file in manifest still exists and matches its build-time SHA-256
    for rel_path, expected_sha in manifest.items():
        target = app_root / rel_path
        if not target.is_file():
            return False
        try:
            h = hashlib.sha256()
            with target.open("rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            if h.hexdigest().lower() != expected_sha:
                return False
        except Exception:
            return False

    # 2. Verify no new untracked/post-build files exist under watched paths or manifest parent dirs
    watched_roots: set[str] = set(DOCKER_MANIFEST_WATCHED_PATHS)
    for rel_path in manifest:
        if "/" in rel_path:
            watched_roots.add(rel_path.rsplit("/", 1)[0])
        else:
            watched_roots.add(rel_path)

    for root_rel in watched_roots:
        root_path = app_root / root_rel
        if not root_path.exists():
            continue
        if root_path.is_file():
            rel_posix = root_path.relative_to(app_root).as_posix()
            if not _is_ignored_runtime_file(rel_posix) and rel_posix not in manifest:
                return False
        elif root_path.is_dir():
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
                for fname in filenames:
                    if fname in _IGNORED_FILE_NAMES or fname.endswith(_IGNORED_FILE_SUFFIXES):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        rel_posix = fpath.relative_to(app_root).as_posix()
                    except ValueError:
                        return False
                    if not _is_ignored_runtime_file(rel_posix) and rel_posix not in manifest:
                        return False

    return True


def _compute_startup_git_info(
    *,
    in_docker: bool = _IN_DOCKER,
    app_root: Path = REPO_ROOT,
    build_sha_file: Path = BUILD_GIT_SHA_FILE,
    build_dirty_file: Path = BUILD_GIT_DIRTY_FILE,
    build_files_sha256_file: Path = BUILD_FILES_SHA256_FILE,
) -> tuple[str, bool]:
    """Compute (git_sha_8char, git_dirty) at backend startup (P0J §1.3, L41).

    In Docker (`in_docker=True`), reads the build-time `/app/.build_git_sha` and
    `/app/.build_git_dirty` baked into the image by `scripts/dalton/Dockerfile.dalton`,
    and verifies the baked SHA-256 manifest (`/app/.build_files.sha256`) against the
    bind-mounted code/config under `/app` so missing manifests, modified files, or newly
    added files all mark `git_dirty=True`.
    """
    if in_docker:
        image_build_sha = _read_image_build_git_sha(build_sha_file)
        if not image_build_sha or image_build_sha == "unknown":
            return "unknown", True
        git_dirty = _read_image_build_git_dirty(build_dirty_file)
        if not git_dirty:
            if not _verify_docker_build_manifest(app_root, build_files_sha256_file):
                git_dirty = True
        return image_build_sha, git_dirty

    git_sha = "unknown"
    git_dirty = True
    try:
        git_sha = subprocess.check_output(
            [
                "git",
                "-c",
                "safe.directory=*",
                "--no-optional-locks",
                "-C",
                str(app_root),
                "rev-parse",
                "--short=8",
                "HEAD",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        ).strip()
        raw_status = subprocess.check_output(
            [
                "git",
                "-c",
                "safe.directory=*",
                "--no-optional-locks",
                "-C",
                str(app_root),
                "status",
                "--porcelain",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        git_dirty = bool(raw_status.strip())
    except Exception as exc:
        logger.debug("Failed to compute startup git info: %s", exc)
    return git_sha, git_dirty


_STARTUP_GIT_SHA: str | None = None
_STARTUP_GIT_DIRTY: bool | None = None
_DEPLOYMENT_MODE = "docker" if _IN_DOCKER else "host"


def init_startup_git_info() -> tuple[str, bool]:
    """Initialize and cache (git_sha, git_dirty) during application startup lifespan."""
    global _STARTUP_GIT_SHA, _STARTUP_GIT_DIRTY
    _STARTUP_GIT_SHA, _STARTUP_GIT_DIRTY = _compute_startup_git_info()
    return _STARTUP_GIT_SHA, _STARTUP_GIT_DIRTY


def get_startup_git_info() -> tuple[str, bool]:
    """Return cached (git_sha, git_dirty) recorded when the backend process started."""
    if _STARTUP_GIT_SHA is None or _STARTUP_GIT_DIRTY is None:
        return init_startup_git_info()
    return _STARTUP_GIT_SHA, _STARTUP_GIT_DIRTY


def _sanitize_error_text(exc: Exception) -> str:
    """Strip any token or raw file payload from error messages before returning/storing."""
    token = get_dalton_broker_token()
    msg = f"{exc.__class__.__name__}: {exc}"
    if token and token in msg:
        msg = msg.replace(token, "***")
    return msg[:300]


def _host_port_only(url: str) -> str:
    try:
        parsed = urlparse(url)
        return parsed.netloc or url
    except Exception:
        return "localhost:18789"


def _read_latest_smoke_summary() -> dict[str, Any] | None:
    smoke_path = REPO_ROOT / "var" / "dalton" / "smoke" / "latest_broker.json"
    if not smoke_path.exists():
        return None
    try:
        data = json.loads(smoke_path.read_text(encoding="utf-8"))
        return {
            "generated_at": data.get("generated_at"),
            "blocking_failures": data.get("blocking_failures") or [],
            "summary": data.get("summary") or {},
        }
    except Exception:
        return None


async def probe_health(user_id: str | None = None) -> dict[str, Any]:
    """Real-time probe of OpenClaw Broker v2 `/health` (P0.7 §3.3, P0J §1.3). Never caches in-process."""
    base_url = get_dalton_broker_url(HOST_IP)
    token = get_dalton_broker_token()
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    smoke = _read_latest_smoke_summary()
    host_port = _host_port_only(base_url)
    git_sha, git_dirty = get_startup_git_info()
    pm_ids = get_dalton_settings().pm_user_ids
    meta = {
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "user_id": user_id,
        "is_pm": bool(user_id and user_id in pm_ids),
        "deployment": _DEPLOYMENT_MODE,
        "pm_user_ids_source": get_pm_user_ids_source(),
    }

    if not token:
        return {
            "status": "token_missing",
            "http_status": None,
            "latency_ms": 0,
            "checked_at": checked_at,
            "base_url": host_port,
            "broker": None,
            "smoke": smoke,
            **meta,
        }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/health",
                headers={"Authorization": f"Bearer {token}"},
            )
        latency_ms = int((time.monotonic() - t0) * 1000)
        code = resp.status_code

        if code == 200:
            body = resp.json() if resp.content else {}
            journal = body.get("journal") or {}
            pending = int(journal.get("pending") or 0)
            inflight = int(body.get("inflight") or 0)
            queued = int(body.get("queued") or 0)
            max_conc = int((body.get("limits") or {}).get("maxConcurrent") or 8)
            low_capacity = max_conc > 0 and ((max_conc - inflight) / max_conc) < 0.10

            status = "degraded" if (pending > 0 or low_capacity) else (
                "ok" if body.get("status") == "ok" else "error"
            )
            return {
                "status": status,
                "http_status": code,
                "latency_ms": latency_ms,
                "checked_at": checked_at,
                "base_url": host_port,
                "broker": {
                    "brokerVersion": body.get("brokerVersion"),
                    "openclawVersion": body.get("openclawVersion"),
                    "journal": journal,
                    "inflight": inflight,
                    "queued": queued,
                },
                "smoke": smoke,
                **meta,
            }

        status_map = {
            401: "auth_error",
            403: "auth_error",
            404: "not_installed",
            503: "gateway_restarting",
        }
        return {
            "status": status_map.get(code, "error"),
            "http_status": code,
            "latency_ms": latency_ms,
            "checked_at": checked_at,
            "base_url": host_port,
            "broker": None,
            "smoke": smoke,
            **meta,
        }
    except Exception:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "status": "unreachable",
            "http_status": None,
            "latency_ms": latency_ms,
            "checked_at": checked_at,
            "base_url": host_port,
            "broker": None,
            "smoke": smoke,
            **meta,
        }


async def _collect_model_references() -> dict[str, list[str]]:
    """Collect active references to model keys from `setup.agent_config` and user preferences."""
    refs: dict[str, list[str]] = {}

    def _add(model_key: Any, source: str) -> None:
        if isinstance(model_key, str) and model_key.strip():
            refs.setdefault(model_key.strip(), []).append(source)

    try:
        from src.server.app import setup

        ac = getattr(setup, "agent_config", None)
        if ac is not None:
            llm_cfg = getattr(ac, "llm", None)
            if llm_cfg is not None:
                for attr in ("name", "background", "compaction", "fetch"):
                    _add(getattr(llm_cfg, attr, None), f"agent_config.llm.{attr}")
                for fb in getattr(llm_cfg, "fallback", None) or []:
                    _add(fb, "agent_config.llm.fallback")
            sub_cfg = getattr(ac, "subagents", None)
            defs = getattr(sub_cfg, "definitions", None) if sub_cfg else None
            if isinstance(defs, dict):
                for sname, sdef in defs.items():
                    m = (
                        sdef.get("model")
                        if isinstance(sdef, dict)
                        else getattr(sdef, "model", None)
                    )
                    _add(m, f"agent_config.subagents.{sname}")
    except Exception as exc:
        logger.debug("Could not inspect setup.agent_config references: %s", exc)

    try:
        user_refs = await find_user_referenced_models()
        for k, src_list in user_refs.items():
            refs.setdefault(k, []).extend(src_list)
    except Exception as exc:
        logger.debug("Could not query user_preferences references: %s", exc)

    return refs


def _invalidate_local_worker_model_caches() -> None:
    """Clear `LLM._model_config` and the provider-catalog caches on the current worker."""
    try:
        from src.llms.llm import LLM

        LLM._model_config = None
    except Exception:
        pass

    # DALTON-HOOK(catalog-cache): the published catalog, its key-candidate map
    # and the per-user available-models cache all derive from the overlay.
    try:
        from src.server.services.llm.provider_catalog import key_candidates, provider_catalog

        provider_catalog.cache_clear()
        key_candidates.cache_clear()
    except Exception:
        pass


async def run_sync(
    *,
    user_id: str,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Execute OpenClaw model catalog sync with advisory lock and reference protection (P0.7 §3.1)."""
    async with get_db_connection() as conn:
        locked = await try_acquire_sync_lock(conn)
        if not locked:
            raise HTTPException(
                status_code=409,
                detail={"code": "sync_in_progress", "message": "另一个模型同步正在进行中，请稍候。"},
            )

        run_id = await create_sync_run(
            started_by=user_id,
            dry_run=dry_run,
            forced=force,
            conn=conn,
        )
        try:
            base_url = get_dalton_broker_url(HOST_IP)
            token = get_dalton_broker_token()
            if not token:
                health = await probe_health()
                await finish_sync_run(
                    run_id=run_id,
                    status="failed",
                    error="broker_unreachable: DALTON_BROKER_TOKEN missing",
                    conn=conn,
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "broker_unreachable",
                        "message": "未配置 DALTON_BROKER_TOKEN，无法连接 OpenClaw broker。",
                        "health": health,
                    },
                )

            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        f"{base_url.rstrip('/')}/models?detail=1",
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except Exception as exc:
                err_msg = _sanitize_error_text(exc)
                health = await probe_health()
                await finish_sync_run(
                    run_id=run_id,
                    status="failed",
                    error=f"broker_unreachable: {err_msg}",
                    conn=conn,
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "broker_unreachable",
                        "message": "无法连接到 OpenClaw broker，请检查网关状态。",
                        "health": health,
                    },
                ) from exc

            if resp.status_code != 200:
                health = await probe_health()
                await finish_sync_run(
                    run_id=run_id,
                    status="failed",
                    error=f"broker_unreachable: HTTP {resp.status_code}",
                    conn=conn,
                )
                raise HTTPException(
                    status_code=502,
                    detail={
                        "code": "broker_unreachable",
                        "message": f"OpenClaw broker 返回 HTTP {resp.status_code}。",
                        "health": health,
                    },
                )

            try:
                catalog = resp.json()
                current = load_overlay_data(OVERLAY_PATH)
                new_overlay = build_overlay(
                    catalog=catalog,
                    overrides=load_overrides(),
                    current_overlay=current,
                )
            except ValueError as exc:
                err_msg = _sanitize_error_text(exc)
                await finish_sync_run(
                    run_id=run_id,
                    status="failed",
                    error=f"catalog_invalid: {err_msg}",
                    conn=conn,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "catalog_invalid",
                        "message": f"OpenClaw 模型目录无效或为空：{err_msg}",
                    },
                ) from exc

            diff = diff_overlay(current, new_overlay)
            meta = new_overlay.get("meta") or {}
            raw_counts = meta.get("counts") or {}
            counts = {
                "allowlist": int(raw_counts.get("catalog") or 0),
                "visible": int(raw_counts.get("visible") or 0),
                "hidden": int(raw_counts.get("hidden") or 0),
                "text_only": int(raw_counts.get("text_only") or 0),
                "added": len(diff["added"]),
                "removed": len(diff["removed"]),
                "changed": len(diff["changed"]),
            }

            # Reference protection (P0.7 §3.1 step 4)
            refs_map = await _collect_model_references()
            blocked: list[dict[str, Any]] = []
            for rem in diff["removed"]:
                rkey = rem.get("key")
                if rkey and rkey in refs_map:
                    blocked.append({"key": rkey, "referenced_by": refs_map[rkey]})

            sha256 = public_extract_sha256(new_overlay)
            warnings = list(meta.get("warnings") or [])

            if blocked and not force:
                await finish_sync_run(
                    run_id=run_id,
                    status="blocked",
                    counts=counts,
                    diff={**diff, "blocked": blocked},
                    warnings=warnings,
                    error="in_use",
                    overlay_stamp=current_overlay_stamp(OVERLAY_PATH),
                    public_extract_sha256=sha256,
                    conn=conn,
                )
                if not dry_run:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "in_use",
                            "message": "待移除或隐藏的模型正被系统配置或用户偏好引用。",
                            "blocked": blocked,
                            "diff": diff,
                        },
                    )

            stamp = current_overlay_stamp(OVERLAY_PATH)
            applied = False
            if not dry_run:
                stamp = write_overlay_atomic(new_overlay, OVERLAY_PATH)
                _invalidate_local_worker_model_caches()
                applied = True

            await finish_sync_run(
                run_id=run_id,
                status="succeeded",
                counts=counts,
                diff=diff,
                warnings=warnings,
                error=None,
                overlay_stamp=stamp,
                public_extract_sha256=sha256,
                conn=conn,
            )

            return {
                "run_id": run_id,
                "dry_run": dry_run,
                "applied": applied,
                "added": diff["added"],
                "removed": diff["removed"],
                "changed": diff["changed"],
                "unchanged": diff["unchanged"],
                "warnings": warnings,
                "price_discrepancies": meta.get("price_discrepancies") or [],
                "blocked": blocked,
                "synced_at": meta.get("generated_at"),
                "counts": counts,
            }
        except HTTPException:
            raise
        except Exception as exc:
            err_msg = _sanitize_error_text(exc)
            await finish_sync_run(
                run_id=run_id,
                status="failed",
                error=err_msg,
                conn=conn,
            )
            raise HTTPException(
                status_code=500,
                detail={"code": "sync_failed", "message": err_msg},
            ) from exc
        finally:
            await release_sync_lock(conn)


def _summarize_pricing(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(card, dict):
        return None
    if card.get("pricing_type") == "subscription":
        return None
    if "input" in card or "output" in card:
        return {
            "input": card.get("input"),
            "output": card.get("output"),
            "cached_input": card.get("cached_input"),
        }
    in_tiers = card.get("input_tiers")
    out_tiers = card.get("output_tiers")
    if isinstance(in_tiers, list) and in_tiers and isinstance(out_tiers, list) and out_tiers:
        return {
            "input": in_tiers[0].get("rate"),
            "output": out_tiers[0].get("rate"),
            "cached_input": in_tiers[0].get("cached_input"),
            "tiered": True,
        }
    return None


async def get_openclaw_status() -> dict[str, Any]:
    """Return current overlay metadata, recent sync runs, model table, and role warnings (P0.7 §3.2)."""
    overlay = load_overlay_data(OVERLAY_PATH) or {}
    meta = overlay.get("meta") or {}
    models_map = overlay.get("models") or {}
    prov_models = ((overlay.get("providers") or {}).get("models") or {}).get(
        "openclaw-broker", []
    )
    by_ref = {
        str(pm.get("id")): pm
        for pm in prov_models
        if isinstance(pm, dict) and pm.get("id")
    }
    hidden_reasons = {
        str(h.get("key")): str(h.get("reason") or "hidden")
        for h in (meta.get("hidden") or [])
        if isinstance(h, dict)
    }

    models_list: list[dict[str, Any]] = []
    for key, mcfg in sorted(models_map.items()):
        if not isinstance(mcfg, dict):
            continue
        ref = str(mcfg.get("model_id") or "")
        pm = by_ref.get(ref) or {}
        dalton_meta = pm.get("dalton") or {}
        billing = str(dalton_meta.get("billing") or "cash")
        is_sub = billing == "subscription" or (pm.get("pricing") or {}).get("pricing_type") == "subscription"
        tool_calling = bool(dalton_meta.get("tool_calling", True))
        m_warnings: list[str] = []
        if dalton_meta.get("family") == "unknown":
            m_warnings.append("unknown_family")
        if not tool_calling:
            m_warnings.append("text_only")

        models_list.append(
            {
                "key": key,
                "openclaw_ref": ref,
                "display_name": mcfg.get("display_name") or ref,
                "family": dalton_meta.get("family") or "unknown",
                "subscription": is_sub,
                "visible": bool(mcfg.get("visible", True)),
                "hide_reason": hidden_reasons.get(key),
                "tool_calling": tool_calling,
                "context": mcfg.get("context") or pm.get("context_window"),
                "max_output": (mcfg.get("parameters") or {}).get("max_tokens") or pm.get("max_output"),
                "pricing_summary": None if is_sub else _summarize_pricing(pm.get("pricing")),
                "notional_summary": _summarize_pricing(pm.get("notional_pricing")) if is_sub else None,
                "warnings": m_warnings,
            }
        )

    last_run, last_succeeded = None, None
    try:
        last_run, last_succeeded = await fetch_recent_sync_runs()
    except Exception as exc:
        logger.debug("Could not read dalton_openclaw_sync_runs: %s", exc)

    # Role policy checks against active configuration
    role_warnings = check_roles()

    return {
        "meta": {
            "generated_at": meta.get("generated_at"),
            "source": meta.get("source"),
            "broker_version": meta.get("broker_version"),
            "openclaw_version": meta.get("openclaw_version"),
            "counts": meta.get("counts") or {"catalog": 0, "visible": 0, "hidden": 0, "text_only": 0},
            "warnings": meta.get("warnings") or [],
            "price_discrepancies": meta.get("price_discrepancies") or [],
        },
        "last_run": last_run,
        "last_succeeded_run": last_succeeded,
        "role_warnings": role_warnings,
        "models": models_list,
    }
