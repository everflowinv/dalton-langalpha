"""Deployment profile resolution (``everflow`` vs ``core``).

Single source of truth shared by:
- ``src/ptc_agent/config/file_utils.py`` (generic YAML overlay loading)
- ``src/dalton/settings.py`` (``dalton.yaml`` + ``dalton.core.yaml`` loading)
- ``src/dalton/llm/role_policy.py`` (effective agent role configuration)

Keep this module stdlib-only so lower-level config loaders can import it
without circular dependencies.
"""

from __future__ import annotations

import os
from enum import StrEnum


class DeploymentProfile(StrEnum):
    """Supported Dalton deployment profiles."""

    EVERFLOW = "everflow"
    CORE = "core"


class DaltonStartupConfigError(RuntimeError):
    """Fatal Dalton startup configuration or schema migration error (P0J.J3)."""


class ProfileConfigError(DaltonStartupConfigError, ValueError):
    """Raised when ``APP_ENV`` and ``DALTON_PROFILE`` conflict or are invalid."""


_VALID_DALTON_PROFILES = frozenset({"", DeploymentProfile.EVERFLOW.value, DeploymentProfile.CORE.value})


def resolve_profile(
    *,
    app_env: str | None = None,
    dalton_profile: str | None = None,
) -> DeploymentProfile:
    """Resolve the active deployment profile from ``APP_ENV`` and ``DALTON_PROFILE``.

    Rules:
    - ``APP_ENV=core`` or ``DALTON_PROFILE=core`` -> ``DeploymentProfile.CORE``.
    - ``APP_ENV`` unset/empty or ``APP_ENV=dalton`` (or other non-core env when
      ``DALTON_PROFILE`` is not ``core``) -> ``DeploymentProfile.EVERFLOW``.
    - Conflicting combinations (e.g. ``APP_ENV=dalton`` + ``DALTON_PROFILE=core``
      or ``APP_ENV=core`` + ``DALTON_PROFILE=everflow``) or unknown
      ``DALTON_PROFILE`` values raise :class:`ProfileConfigError` with
      actionable remediation guidance.
    """
    raw_app_env = (os.getenv("APP_ENV", "") if app_env is None else app_env).strip()
    raw_profile = (
        os.getenv("DALTON_PROFILE", "") if dalton_profile is None else dalton_profile
    ).strip()

    app_env_norm = raw_app_env.lower()
    profile_norm = raw_profile.lower()

    if profile_norm not in _VALID_DALTON_PROFILES:
        raise ProfileConfigError(
            f"Invalid DALTON_PROFILE={raw_profile!r}. "
            "Set DALTON_PROFILE=core for the standalone core profile, "
            "DALTON_PROFILE=everflow for Everflow, or unset DALTON_PROFILE."
        )

    if profile_norm == DeploymentProfile.CORE.value and app_env_norm not in (
        "",
        DeploymentProfile.CORE.value,
    ):
        raise ProfileConfigError(
            f"Conflicting profile environment variables: APP_ENV={raw_app_env!r} "
            f"cannot be combined with DALTON_PROFILE={raw_profile!r}. "
            "For standalone core mode, set APP_ENV=core (or unset APP_ENV) with "
            "DALTON_PROFILE=core; for Everflow mode, unset DALTON_PROFILE or set "
            "DALTON_PROFILE=everflow."
        )

    if profile_norm == DeploymentProfile.EVERFLOW.value and (
        app_env_norm == DeploymentProfile.CORE.value
    ):
        raise ProfileConfigError(
            f"Conflicting profile environment variables: APP_ENV={raw_app_env!r} "
            f"cannot be combined with DALTON_PROFILE={raw_profile!r}. "
            "For Everflow mode, set APP_ENV=dalton (or unset APP_ENV); "
            "for standalone core mode, set DALTON_PROFILE=core or unset DALTON_PROFILE."
        )

    if (
        app_env_norm == DeploymentProfile.CORE.value
        or profile_norm == DeploymentProfile.CORE.value
    ):
        return DeploymentProfile.CORE

    return DeploymentProfile.EVERFLOW


def resolve_yaml_overlay_env(
    *,
    app_env: str | None = None,
    dalton_profile: str | None = None,
) -> str:
    """Return the sibling YAML overlay suffix (``*.<suffix>.yaml``) for generic config loading.

    - In ``core`` profile (whether entered via ``APP_ENV=core`` or
      ``DALTON_PROFILE=core`` with empty ``APP_ENV``), always returns ``"core"``.
    - In ``everflow`` profile, returns the trimmed ``APP_ENV`` verbatim
      (``"dalton"`` -> ``*.dalton.yaml``, ``""`` -> no overlay, preserving
      existing behavior and custom test envs such as ``production``).
    """
    profile = resolve_profile(app_env=app_env, dalton_profile=dalton_profile)
    if profile is DeploymentProfile.CORE:
        return DeploymentProfile.CORE.value
    return (os.getenv("APP_ENV", "") if app_env is None else app_env).strip()


def is_core_profile() -> bool:
    """Return ``True`` when the active deployment profile is ``core``."""
    return resolve_profile() is DeploymentProfile.CORE
