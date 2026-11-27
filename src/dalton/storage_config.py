"""Single source of truth for main object-storage configuration and workspace-file rules.

Shared by:
- ``src/utils/storage/__init__.py`` and ``src/utils/storage/s3_client.py`` (via ``# DALTON-HOOK(standalone-core)``)
- ``src/dalton/auth/bootstrap.py`` (startup provider check for Standalone Core)
- ``src/dalton/wsfiles_write_mode.py`` and ``src/server/services/persistence/transfer.py``
- ``python -m src.dalton.storage_config show`` operator probe (and Batch 3 standalone scripts)

Keep this module free of ``src.utils.storage``, ``boto3``, database, and FastAPI imports so
scripts and CLI probes can run it directly with stdlib + PyYAML.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from src.dalton.profile import (
    DaltonStartupConfigError,
    DeploymentProfile,
    resolve_profile,
    resolve_yaml_overlay_env,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DALTON_YAML_PATH = Path(__file__).resolve().parent / "dalton.yaml"
DALTON_CORE_YAML_PATH = Path(__file__).resolve().parent / "dalton.core.yaml"

CORE_DEFAULT_BUCKET = "dalton-storage"
CORE_DEFAULT_ENDPOINT_URL = "http://minio:9000"
CORE_ALLOWED_STORAGE_PROVIDERS = frozenset({"s3", "none"})
CORE_STORAGE_PROVIDER_ERROR = (
    "核心安装的主存储请用 S3 兼容服务（内置 MinIO 或外部 S3）"
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class ResolvedStorageConfig:
    """Effective main-storage configuration resolved from YAML + environment."""

    provider: str
    raw_provider: str
    bucket_name: str
    endpoint_url: str
    is_storage_enabled: bool
    write_inline_only: bool
    exclude_dir_names: tuple[str, ...]
    exclude_rel_dirs: tuple[str, ...]
    exclude_rel_globs: tuple[str, ...]

    def to_probe_dict(self) -> dict[str, Any]:
        """Safe JSON-serializable summary for ``show`` (contains no credentials)."""
        return {
            "is_storage_enabled": self.is_storage_enabled,
            "provider": self.provider,
            "bucket_name": self.bucket_name,
            "endpoint_url": self.endpoint_url,
            "write_inline_only": self.write_inline_only,
            "exclude_rel_globs": list(self.exclude_rel_globs),
        }


def _env_map(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _deep_merge_dicts(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _find_agent_config_path() -> Path | None:
    cwd_path = Path.cwd() / "agent_config.yaml"
    if cwd_path.is_file():
        return cwd_path
    repo_path = REPO_ROOT / "agent_config.yaml"
    if repo_path.is_file():
        return repo_path
    return None


def _load_agent_storage_section(
    *,
    env: Mapping[str, str] | None = None,
    agent_config: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Read the merged ``storage`` mapping from ``agent_config.yaml`` (+ profile overlay)."""
    if agent_config is not None:
        section = agent_config.get("storage")
        return section if isinstance(section, Mapping) else {}

    base_path = _find_agent_config_path()
    if base_path is None:
        return {}

    em = _env_map(env)
    overlay_env = resolve_yaml_overlay_env(
        app_env=em.get("APP_ENV", ""),
        dalton_profile=em.get("DALTON_PROFILE", ""),
    )
    try:
        raw_base = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw_base, dict):
            raw_base = {}
        merged: dict[str, Any] = raw_base
        if overlay_env:
            overlay_path = base_path.with_name(
                f"{base_path.stem}.{overlay_env}{base_path.suffix}"
            )
            if overlay_path.is_file():
                raw_overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
                if isinstance(raw_overlay, dict):
                    merged = _deep_merge_dicts(merged, raw_overlay)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Failed to load %s: %s", base_path, exc)
        return {}

    section = merged.get("storage")
    return section if isinstance(section, Mapping) else {}


def _load_dalton_workspace_files_raw(profile: DeploymentProfile) -> Mapping[str, Any]:
    """Read ``workspace_files`` from merged Dalton YAML via ``_load_profile_dalton_raw``."""
    try:
        from src.dalton.settings import _load_profile_dalton_raw

        raw = _load_profile_dalton_raw(profile)
    except Exception:
        raw = {}
        if DALTON_YAML_PATH.is_file():
            loaded = yaml.safe_load(DALTON_YAML_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                raw = loaded
        if profile is DeploymentProfile.CORE and DALTON_CORE_YAML_PATH.is_file():
            core_loaded = (
                yaml.safe_load(DALTON_CORE_YAML_PATH.read_text(encoding="utf-8")) or {}
            )
            if isinstance(core_loaded, dict):
                raw = _deep_merge_dicts(raw, core_loaded)

    section = raw.get("workspace_files") if isinstance(raw, Mapping) else None
    return section if isinstance(section, Mapping) else {}


def _active_profile(env: Mapping[str, str] | None = None) -> DeploymentProfile:
    em = _env_map(env)
    return resolve_profile(
        app_env=em.get("APP_ENV", ""),
        dalton_profile=em.get("DALTON_PROFILE", ""),
    )


def resolve_storage_provider(
    *,
    env: Mapping[str, str] | None = None,
    agent_config: Mapping[str, Any] | None = None,
) -> str:
    """Resolve the raw main-storage provider (before bucket-presence disablement).

    Priority:
    1. Explicit environment ``STORAGE_PROVIDER=none`` always disables main storage,
       even when ``agent_config.yaml`` sets ``storage.provider: "s3"``.
    2. Non-empty ``storage.provider`` in merged ``agent_config.yaml`` (an empty string
       counts as unset so an overlay such as ``agent_config.core.yaml`` can delegate
       selection to the environment).
    3. Environment ``STORAGE_PROVIDER`` (default ``"none"``).
    """
    em = _env_map(env)
    env_provider = (em.get("STORAGE_PROVIDER") or "").strip().lower()
    if env_provider == "none":
        return "none"

    storage_section = _load_agent_storage_section(env=em, agent_config=agent_config)
    yaml_value = storage_section.get("provider")
    if yaml_value is not None:
        yaml_provider = str(yaml_value).strip().lower()
        if yaml_provider:
            return yaml_provider

    return env_provider or "none"


def validate_core_storage_provider(
    *,
    env: Mapping[str, str] | None = None,
    agent_config: Mapping[str, Any] | None = None,
) -> None:
    """Reject non-S3 storage providers in Standalone Core profile.

    Core profile only accepts ``"s3"`` and ``"none"``; Everflow profile is unrestricted.
    """
    em = _env_map(env)
    if _active_profile(em) is not DeploymentProfile.CORE:
        return

    env_provider = (em.get("STORAGE_PROVIDER") or "").strip().lower()
    if env_provider and env_provider not in CORE_ALLOWED_STORAGE_PROVIDERS:
        raise DaltonStartupConfigError(CORE_STORAGE_PROVIDER_ERROR)

    raw_provider = resolve_storage_provider(env=em, agent_config=agent_config)
    if raw_provider not in CORE_ALLOWED_STORAGE_PROVIDERS:
        raise DaltonStartupConfigError(CORE_STORAGE_PROVIDER_ERROR)


def resolve_storage_bucket(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
    apply_core_defaults: bool = False,
) -> str:
    """Resolve main-storage bucket name for ``provider``.

    - ``"none"`` -> ``""``.
    - ``"oss"`` -> ``OSS_BUCKET_NAME`` (trimmed).
    - S3-compatible:
      - If ``STORAGE_BUCKET_NAME`` is defined in ``env``, return its trimmed value
        directly (an explicit empty string disables main storage without falling back
        to ``S3_BUCKET_NAME`` or core defaults).
      - Otherwise fall back to ``S3_BUCKET_NAME``.
      - When still empty and ``apply_core_defaults`` is True in Core profile,
        default to ``"dalton-storage"``.
    """
    em = _env_map(env)
    norm_provider = (provider or "").strip().lower()
    if not norm_provider or norm_provider == "none":
        return ""
    if norm_provider == "oss":
        return (em.get("OSS_BUCKET_NAME") or "").strip()

    if "STORAGE_BUCKET_NAME" in em:
        return (em.get("STORAGE_BUCKET_NAME") or "").strip()

    legacy = (em.get("S3_BUCKET_NAME") or "").strip()
    if legacy:
        return legacy

    if apply_core_defaults and _active_profile(em) is DeploymentProfile.CORE:
        return CORE_DEFAULT_BUCKET
    return ""


def resolve_storage_endpoint_url(
    *,
    env: Mapping[str, str] | None = None,
    apply_core_defaults: bool = False,
) -> str:
    """Resolve S3-compatible endpoint URL (``STORAGE_ENDPOINT_URL`` -> ``S3_ENDPOINT_URL``)."""
    em = _env_map(env)
    primary = (em.get("STORAGE_ENDPOINT_URL") or "").strip()
    if primary:
        return primary
    legacy = (em.get("S3_ENDPOINT_URL") or "").strip()
    if legacy:
        return legacy
    if apply_core_defaults and _active_profile(em) is DeploymentProfile.CORE:
        return CORE_DEFAULT_ENDPOINT_URL
    return ""


def is_wsfiles_write_inline_only(
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether Level-1 write-inline-only fallback is active.

    Read live on every call (never cached at import time):
    1. ``DALTON_WSFILES_WRITE_INLINE_ONLY`` env var (``1/true/yes/on`` -> True,
       ``0/false/no/off`` -> False; unrecognized non-empty values log a WARNING
       and evaluate as False).
    2. ``workspace_files.write_inline_only`` in merged Dalton YAML (default False).

    Changing ``.env`` requires recreating the backend container (重建后端容器) for
    Docker Compose to reload ``env_file``.
    """
    em = _env_map(env)
    raw_env = em.get("DALTON_WSFILES_WRITE_INLINE_ONLY")
    if raw_env is not None:
        cleaned = raw_env.strip().lower()
        if cleaned:
            if cleaned in _TRUTHY:
                return True
            if cleaned in _FALSY:
                return False
            logger.warning(
                "Invalid DALTON_WSFILES_WRITE_INLINE_ONLY=%r; treating as False "
                "(expected 1/true/yes/on or 0/false/no/off)",
                raw_env,
            )
            return False

    section = _load_dalton_workspace_files_raw(_active_profile(em))
    yaml_val = section.get("write_inline_only", False)
    if isinstance(yaml_val, bool):
        return yaml_val
    if isinstance(yaml_val, str):
        norm = yaml_val.strip().lower()
        if norm in _TRUTHY:
            return True
        if norm in _FALSY:
            return False
    return bool(yaml_val)


def _clean_string_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _parse_csv_env(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        cleaned = part.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def get_wsfiles_exclusions(
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Read optional Dalton workspace-file sync exclusion lists.

    WARNING (§4): Non-empty exclusion patterns cause already-mirrored matching rows
    in ``workspace_files`` to be pruned on the next workspace sync. Keep all lists
    empty for the initial rollout and evaluate carefully before adding patterns.
    """
    em = _env_map(env)
    section = _load_dalton_workspace_files_raw(_active_profile(em))

    dir_names = _clean_string_list(section.get("exclude_dir_names"))

    if "DALTON_WSFILES_EXCLUDE_REL_DIRS" in em:
        rel_dirs = _parse_csv_env(em.get("DALTON_WSFILES_EXCLUDE_REL_DIRS") or "")
    else:
        rel_dirs = _clean_string_list(section.get("exclude_rel_dirs"))

    if "DALTON_WSFILES_EXCLUDE_REL_GLOBS" in em:
        rel_globs = _parse_csv_env(em.get("DALTON_WSFILES_EXCLUDE_REL_GLOBS") or "")
    else:
        rel_globs = _clean_string_list(section.get("exclude_rel_globs"))

    return {
        "exclude_dir_names": dir_names,
        "exclude_rel_dirs": rel_dirs,
        "exclude_rel_globs": rel_globs,
    }


def apply_wsfiles_exclusions(
    spec: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Append configured Dalton exclusions onto upstream ``exclusion_spec()``.

    When all three Dalton lists are empty, returns ``spec`` with the exact same keys
    and values (without inserting an ``exclude_rel_globs`` key) so ``ScanRules``
    fingerprints remain unchanged.
    """
    extra = get_wsfiles_exclusions(env=env)
    dir_names = extra["exclude_dir_names"]
    rel_dirs = extra["exclude_rel_dirs"]
    rel_globs = extra["exclude_rel_globs"]
    if not dir_names and not rel_dirs and not rel_globs:
        return spec

    updated = dict(spec)
    if dir_names:
        updated["exclude_dir_names"] = sorted(
            set(updated.get("exclude_dir_names") or ()) | set(dir_names)
        )
    if rel_dirs:
        existing_rel_dirs = list(updated.get("exclude_rel_dirs") or ())
        seen = set(existing_rel_dirs)
        for item in rel_dirs:
            if item not in seen:
                seen.add(item)
                existing_rel_dirs.append(item)
        updated["exclude_rel_dirs"] = existing_rel_dirs
    if rel_globs:
        existing_globs = list(updated.get("exclude_rel_globs") or ())
        seen_globs = set(existing_globs)
        for item in rel_globs:
            if item not in seen_globs:
                seen_globs.add(item)
                existing_globs.append(item)
        updated["exclude_rel_globs"] = existing_globs
    return updated


def resolve(
    *,
    env: Mapping[str, str] | None = None,
    agent_config: Mapping[str, Any] | None = None,
    apply_core_defaults: bool = False,
    validate_core: bool = False,
) -> ResolvedStorageConfig:
    """Resolve effective main-storage settings and workspace-file rules."""
    em = _env_map(env)
    if validate_core:
        validate_core_storage_provider(env=em, agent_config=agent_config)

    raw_provider = resolve_storage_provider(env=em, agent_config=agent_config)
    bucket_name = resolve_storage_bucket(
        raw_provider, env=em, apply_core_defaults=apply_core_defaults
    )
    endpoint_url = resolve_storage_endpoint_url(
        env=em, apply_core_defaults=apply_core_defaults
    )
    effective_provider = (
        "none" if raw_provider == "none" or not bucket_name else raw_provider
    )
    exclusions = get_wsfiles_exclusions(env=em)
    return ResolvedStorageConfig(
        provider=effective_provider,
        raw_provider=raw_provider,
        bucket_name=bucket_name,
        endpoint_url=endpoint_url,
        is_storage_enabled=effective_provider != "none",
        write_inline_only=is_wsfiles_write_inline_only(env=em),
        exclude_dir_names=tuple(exclusions["exclude_dir_names"]),
        exclude_rel_dirs=tuple(exclusions["exclude_rel_dirs"]),
        exclude_rel_globs=tuple(exclusions["exclude_rel_globs"]),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.storage_config",
        description=(
            "Inspect resolved Dalton main object-storage configuration without "
            "printing any credentials. Note: after editing .env, recreate the "
            "backend container (重建后端容器) so Docker Compose reloads env_file."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    show_parser = sub.add_parser(
        "show",
        help="Print one-line JSON summary of effective main-storage configuration.",
    )
    show_parser.add_argument(
        "--apply-core-defaults",
        action="store_true",
        help="Apply Standalone Core bucket/endpoint defaults when unset.",
    )

    args = parser.parse_args(argv)
    if args.command == "show":
        cfg = resolve(apply_core_defaults=bool(args.apply_core_defaults))
        print(json.dumps(cfg.to_probe_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
