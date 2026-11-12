"""Deployment identity and worker model-cache invalidation.

Git SHA / dirty detection (P0J) stays here because startup and
``GET /openclaw/health`` still report the running build. OpenClaw broker
model sync was removed with the ``ocb-*`` catalog (OC-5). The health route
keeps its path so real-run preflight can read deployment identity; it no
longer calls the broker.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from src.config.env import _IN_DOCKER
from src.dalton.settings import get_dalton_settings, get_pm_user_ids_source

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
BUILD_GIT_SHA_FILE = Path("/app/.build_git_sha")
BUILD_GIT_DIRTY_FILE = Path("/app/.build_git_dirty")
BUILD_FILES_SHA256_FILE = Path("/app/.build_files.sha256")
DOCKER_MANIFEST_WATCHED_PATHS: tuple[str, ...] = (
    "server.py",
    "config.yaml",
    "config.dalton.yaml",
    "agent_config.yaml",
    "agent_config.dalton.yaml",
    "alembic.ini",
    "config/dalton",
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


async def probe_health(user_id: str | None = None) -> dict[str, Any]:
    """Deployment identity for ``GET /openclaw/health``.

    Real-run preflight and the G5 evidence script read ``git_sha``,
    ``git_dirty``, ``user_id``, ``is_pm``, ``deployment``, and
    ``pm_user_ids_source`` from this payload. OpenClaw broker model probing
    was removed with the ``ocb-*`` catalog (OC-5); this no longer calls the
    broker.
    """
    git_sha, git_dirty = get_startup_git_info()
    pm_ids = get_dalton_settings().pm_user_ids
    return {
        "status": "ok",
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "user_id": user_id,
        "is_pm": bool(user_id and user_id in pm_ids),
        "deployment": _DEPLOYMENT_MODE,
        "pm_user_ids_source": get_pm_user_ids_source(),
    }


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
