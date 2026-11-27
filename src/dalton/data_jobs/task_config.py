"""Single source of truth for Dalton data-task configuration keys and aliases.

Shared by:
- ``src/dalton/data_jobs/container_runner.py`` (forwarding keys into ephemeral task containers)
- ``scripts/dalton/seed_openclaw_data.sh`` (Everflow preflight against container-readable config)
- ``scripts/dalton/ops/prod/README.md`` (production configuration documentation)

Keep this module stdlib-only so both backend code and standalone helpers can import it.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Mapping

GENERIC_CONTAINER_HOME = "/home/dalton"
GENERIC_WORKSPACE_DIR = "/workspace"
WORKSPACE_PLACEHOLDER = "${DALTON_DATA_TASK_WORKSPACE}"
_EVERFLOW_WORKSPACE_OVERLAY = Path(__file__).resolve().parent / "workspace.dalton.yaml"
_EVERFLOW_DEFAULTS_FILE = Path(__file__).resolve().parent / "everflow_defaults.yaml"


def _is_everflow_profile() -> bool:
    try:
        from src.dalton.profile import DeploymentProfile, resolve_profile

        return resolve_profile() is DeploymentProfile.EVERFLOW
    except ImportError:
        profile_path = Path(__file__).resolve().parents[1] / "profile.py"
        if not profile_path.is_file():
            return False
        spec = importlib.util.spec_from_file_location("_dalton_profile_standalone", profile_path)
        if spec is None or spec.loader is None:
            return False
        profile_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(profile_mod)
        return profile_mod.resolve_profile() is profile_mod.DeploymentProfile.EVERFLOW
    except Exception:
        return False


def _read_simple_yaml_mapping(yaml_path: Path) -> dict[str, str]:
    """Read a flat ``KEY: value`` YAML file using stdlib only (last entry wins)."""
    if not yaml_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in yaml_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and val:
            values[key] = val
    return values


def _read_everflow_workspace_overlay() -> dict[str, str]:
    """Read optional Everflow-only ``workspace.dalton.yaml`` when profile is Everflow."""
    if not _is_everflow_profile():
        return {}
    return _read_simple_yaml_mapping(_EVERFLOW_WORKSPACE_OVERLAY)


def load_everflow_data_task_defaults() -> dict[str, str]:
    """Load Everflow-only ``everflow_defaults.yaml`` when ``src.dalton.profile`` is Everflow.

    Under ``core`` profile this always returns an empty dict and never reads the file.
    """
    if not _is_everflow_profile():
        return {}
    return _read_simple_yaml_mapping(_EVERFLOW_DEFAULTS_FILE)


def get_everflow_data_task_default(key: str) -> str:
    """Return the Everflow-only default for ``key``, or ``""`` under Core / when unset."""
    return load_everflow_data_task_defaults().get(key, "").strip()


def resolve_default_container_home() -> str:
    """Resolve the default container ``HOME`` (env -> Everflow overlay -> neutral core default)."""
    env_val = (os.getenv("DALTON_DATA_TASK_CONTAINER_HOME") or "").strip()
    if env_val:
        return env_val
    return _read_everflow_workspace_overlay().get("container_home") or GENERIC_CONTAINER_HOME


def resolve_default_workspace_dir() -> str:
    """Resolve the default container workspace mount path (env -> Everflow overlay -> neutral core default)."""
    env_val = (os.getenv("DALTON_DATA_TASK_CONTAINER_WORKSPACE") or "").strip()
    if env_val:
        return env_val
    return _read_everflow_workspace_overlay().get("container_workspace_dir") or GENERIC_WORKSPACE_DIR


DEFAULT_CONTAINER_HOME = resolve_default_container_home()
DEFAULT_WORKSPACE_DIR = resolve_default_workspace_dir()

# Required Everflow data-task keys. Each tuple is `(canonical_key, alias1, alias2, ...)`.
REQUIRED_EVERFLOW_KEY_SPECS: tuple[tuple[str, ...], ...] = (
    ("DALTON_DATA_TASK_BBG_HOST", "DALTON_BBG_HOST"),
    ("DALTON_DATA_TASK_BBG_CSV_REMOTE", "DALTON_BBG_CSV_REMOTE"),
    ("DALTON_DATA_TASK_BBG_XLSX_REMOTE", "DALTON_BBG_XLSX_REMOTE"),
    ("DALTON_DATA_TASK_BBG_REFRESH_CMD", "DALTON_BBG_REFRESH_CMD"),
    ("DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID", "DALTON_FEISHU_TRACKING_CHAT_ID"),
    ("DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID", "DALTON_FEISHU_SEMI_CHAT_ID"),
    ("DALTON_DATA_TASK_FEISHU_ASIA_TARGET", "DALTON_FEISHU_ASIA_TARGET"),
    ("DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET", "DALTON_MEGACAP_FEISHU_TARGET"),
    (
        "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID",
        "DALTON_SENTIMENT_DIGEST_CHAT_ID",
        "SENTIMENT_DIGEST_CHAT_ID",
    ),
    ("DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET",),
    ("DALTON_DATA_TASK_DISCORD_DM_TARGET",),
    ("DALTON_DATA_TASK_DISCORD_DEV_CHANNEL",),
    ("DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL",),
    ("DALTON_SEC_USER_AGENT", "DALTON_DATA_TASK_SEC_USER_AGENT", "SEC_USER_AGENT"),
    ("DALTON_DATA_TASK_GS_PB_PYTHON",),
)

# Optional data-task keys (including custom config file path) forwarded into task containers.
OPTIONAL_DATA_TASK_KEY_SPECS: tuple[tuple[str, ...], ...] = (
    ("DALTON_DATA_TASKS_CONFIG",),
    ("DALTON_DATA_TASK_LEGACY_WORKSPACE_PREFIX",),
    ("DALTON_DATA_TASK_OPENCLAW_BACKUP_DIR", "DALTON_OPENCLAW_BACKUP_DIR"),
    ("DALTON_DATA_TASK_OPENCLAW_CONFIG", "DALTON_OPENCLAW_CONFIG"),
    ("DALTON_DATA_TASK_CHINA_2C_UNIVERSE_DIR",),
)

# Flat ordered tuple of every key (canonical + alias) forwarded into task containers.
CONTAINER_FORWARDED_DATA_TASK_ENV_KEYS: tuple[str, ...] = tuple(
    dict.fromkeys(
        key
        for group in (*REQUIRED_EVERFLOW_KEY_SPECS, *OPTIONAL_DATA_TASK_KEY_SPECS)
        for key in group
    )
)


def parse_data_tasks_env_file(config_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE entries from a ``data_tasks.env`` file without touching ``os.environ``."""
    if not config_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def resolve_data_tasks_config_relative_path(
    raw_path: str,
    *,
    host_data_root: Path | None = None,
    backend_data_root: Path | None = None,
    container_workspace_dir: Path | None = None,
) -> Path | None:
    """Resolve ``DALTON_DATA_TASKS_CONFIG`` to a relative path inside the data root, or ``None``.

    Single source of truth shared by ``map_config_path_for_container`` (container runner),
    ``scripts/dalton/seed_openclaw_data.sh`` (preflight), and ``researchdata_bridge.py``.

    Recognizes:
    - Relative paths inside the data root (rejects ``..`` escapes and ``.``).
    - Absolute paths under ``host_data_root`` or ``backend_data_root``.
    - Absolute paths under the active ``container_workspace_dir``.
    - Absolute paths under ``/workspace`` (``GENERIC_WORKSPACE_DIR``), translating them to
      the active container workspace or host data root consistently across Everflow and Core.
    """
    text = str(raw_path or "").strip()
    if not text or "\x00" in text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        norm = Path(os.path.normpath(text))
        if norm == Path(".") or norm.is_absolute() or ".." in norm.parts:
            return None
        return norm

    norm_candidate = Path(os.path.normpath(text))
    active_ws = (
        container_workspace_dir
        if container_workspace_dir is not None
        else Path(resolve_default_workspace_dir())
    )
    bases: list[Path] = []
    for base in (host_data_root, backend_data_root, active_ws, Path(GENERIC_WORKSPACE_DIR)):
        if base is not None:
            bases.append(base)
    for base in bases:
        norm_base = Path(os.path.normpath(str(base)))
        try:
            rel = norm_candidate.relative_to(norm_base)
        except ValueError:
            try:
                rel = candidate.resolve(strict=False).relative_to(base.resolve(strict=False))
            except ValueError:
                continue
        if rel == Path(".") or ".." in rel.parts:
            continue
        return rel
    return None


def resolve_data_tasks_config_host_path(
    raw_path: str,
    *,
    host_data_root: Path,
    backend_data_root: Path | None = None,
    container_workspace_dir: Path | None = None,
) -> Path | None:
    """Resolve ``DALTON_DATA_TASKS_CONFIG`` to an existing file strictly inside ``host_data_root``."""
    rel = resolve_data_tasks_config_relative_path(
        raw_path,
        host_data_root=host_data_root,
        backend_data_root=backend_data_root,
        container_workspace_dir=container_workspace_dir,
    )
    if rel is None:
        return None
    root = host_data_root.resolve(strict=False)
    resolved = (root / rel).resolve(strict=False)
    if not resolved.is_file():
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return resolved


def map_config_path_for_container(
    raw_path: str,
    *,
    host_data_root: Path,
    backend_data_root: Path,
    container_workspace_dir: Path,
) -> str:
    """Translate ``DALTON_DATA_TASKS_CONFIG`` (host, backend, or ``/workspace/``) into container path.

    Shares ``resolve_data_tasks_config_relative_path`` with ``seed_openclaw_data.sh`` so
    preflight and ephemeral task containers always map ``/workspace/...`` identically.
    """
    text = raw_path.strip()
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        return text
    rel = resolve_data_tasks_config_relative_path(
        text,
        host_data_root=host_data_root,
        backend_data_root=backend_data_root,
        container_workspace_dir=container_workspace_dir,
    )
    if rel is not None:
        return (container_workspace_dir / rel).as_posix()
    return text


def collect_container_data_task_env(
    env_source: Mapping[str, str] | None = None,
    *,
    host_data_root: Path | None = None,
    backend_data_root: Path | None = None,
    container_workspace_dir: Path | None = None,
) -> dict[str, str]:
    """Extract all non-empty data-task configuration keys to forward into a task container."""
    src = os.environ if env_source is None else env_source
    forwarded: dict[str, str] = {}
    for profile_key in ("APP_ENV", "DALTON_PROFILE"):
        pval = str(src.get(profile_key) or "").strip()
        if pval:
            forwarded[profile_key] = pval
    for key in CONTAINER_FORWARDED_DATA_TASK_ENV_KEYS:
        val = str(src.get(key) or "").strip()
        if not val:
            continue
        if (
            key == "DALTON_DATA_TASKS_CONFIG"
            and host_data_root is not None
            and backend_data_root is not None
            and container_workspace_dir is not None
        ):
            val = map_config_path_for_container(
                val,
                host_data_root=host_data_root,
                backend_data_root=backend_data_root,
                container_workspace_dir=container_workspace_dir,
            )
        forwarded[key] = val
    return forwarded
