"""Single-source shared `.env` file selection and variable interpolation for Dalton.

Used identically by both the backend (`src/config/__init__.py`) and `dalton-research-host`
(`src/dalton/research_host/__main__.py`) so profile and `DALTON_CAP_*` capability resolution
can never diverge across processes, explicit env files, or `${VAR}` interpolations.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from dotenv import find_dotenv
from dotenv.main import DotEnv
from dotenv.variables import parse_variables


def parse_and_resolve_dotenv(
    dotenv_path: Path | str,
    *,
    base_env: Mapping[str, str],
    override: bool = False,
) -> dict[str, str]:
    """Parse ``dotenv_path`` and resolve ``${VAR}`` references against ``base_env``.

    Matches ``python-dotenv``'s ``resolve_variables`` precedence identically whether
    ``base_env`` is ``os.environ`` or an isolated dict:
    - when ``override=False`` (default): existing keys in ``base_env`` take precedence
      over earlier entries in the file during interpolation;
    - when ``override=True``: earlier entries in the file take precedence over ``base_env``.
    """
    raw_items = DotEnv(
        dotenv_path=Path(dotenv_path),
        verbose=False,
        encoding="utf-8",
        interpolate=False,
        override=override,
    ).parse()

    resolved: dict[str, str] = {}
    for name, raw_value in raw_items:
        if not name or raw_value is None:
            continue
        atoms = parse_variables(raw_value)
        lookup: dict[str, str | None] = {}
        if override:
            lookup.update(base_env)
            lookup.update(resolved)
        else:
            lookup.update(resolved)
            lookup.update(base_env)
        resolved[name] = "".join(atom.resolve(lookup) for atom in atoms)
    return resolved


def resolve_shared_dotenv_path(
    dotenv_path: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Select the shared deployment `.env` path using one consistent rule across services."""
    env = os.environ if environ is None else environ
    explicit = (
        str(dotenv_path).strip()
        if dotenv_path is not None and str(dotenv_path).strip()
        else (
            str(env.get("DALTON_DOTENV_PATH") or "").strip()
            or str(env.get("DALTON_REPO_ENV_FILE") or "").strip()
        )
    )
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate.resolve() if candidate.is_file() else None

    repo_default = Path(__file__).resolve().parents[2] / ".env"
    if repo_default.is_file():
        return repo_default.resolve()

    if environ is None:
        found = find_dotenv(usecwd=True)
        if found:
            found_path = Path(found)
            if found_path.is_file():
                return found_path.resolve()
    return None


_DOTENV_DISABLED_VALUES: frozenset[str] = frozenset({"1", "true", "t", "yes", "y"})


def is_dotenv_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when ``PYTHON_DOTENV_DISABLED`` is set to a truthy value.

    Mirrors ``python-dotenv``'s ``_load_dotenv_disabled()`` semantics across both
    ``os.environ`` and an explicit ``environ`` mapping (an explicit key in ``environ``
    overrides ``os.environ``; when absent in ``environ``, ``os.environ`` is consulted).
    """
    if environ is not None and "PYTHON_DOTENV_DISABLED" in environ:
        raw = environ.get("PYTHON_DOTENV_DISABLED")
    else:
        raw = os.environ.get("PYTHON_DOTENV_DISABLED")
    if raw is None:
        return False
    return str(raw).strip().casefold() in _DOTENV_DISABLED_VALUES


def load_shared_dotenv(
    dotenv_path: Path | str | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> Path | None:
    """Load the shared deployment `.env` into ``environ`` (default: ``os.environ``) without override."""
    target_env: MutableMapping[str, str] = os.environ if environ is None else environ
    if is_dotenv_disabled(environ):
        return None
    resolved_path = resolve_shared_dotenv_path(dotenv_path, environ=target_env)
    if resolved_path is None:
        return None

    parsed = parse_and_resolve_dotenv(resolved_path, base_env=target_env, override=False)
    for key, val in parsed.items():
        if key not in target_env:
            target_env[key] = val
    return resolved_path
