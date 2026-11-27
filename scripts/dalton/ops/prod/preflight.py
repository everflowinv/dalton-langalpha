#!/usr/bin/env python3
"""Everflow Research Host cutover preflight & shared ops helper CLI (§6.4b).

Strictly stdlib-only at module top level so this file can be copied into
`<repo>/var/dalton/cutover-snapshots/recovery-bin/` and executed even after
`git reset --keep "$PRE_FF"` reverts the worktree to an older commit.

Preflight modes (`--stage-a` and `--switch`) are 100% read-only:
- No file writes, no DB connections, no mutating `launchctl` subcommands
  (only `list`, `print`, `getenv`), HTTP GET plus read-only admission probe.
- All external binaries and endpoints are injectable for offline unit tests.

Also exports `validate_timing_budgets_env(path_or_mapping) -> list[str]` and
provides shared CLI subcommands (`journal-set`, `journal-read`, `manifest-write`,
`manifest-verify`, `approve-budgets`, `guard-check`, `status-summary`) so shell
scripts share one single Python implementation.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HANDOFF_DEFAULT_LIMIT_S: int = 90
STAGE_B_DEFAULT_LIMIT_S: int = 3600

POSITIVE_INT_BUDGET_KEYS: tuple[str, ...] = (
    "PROBE_TIMEOUT_S",
    "DRAIN_WAIT_S",
    "SERVICE_RELOAD_S",
    "FORWARD_HANDOFF_BUDGET_S",
    "RECOVERY_BUDGET_S",
    "FULL_RECOVERY_DEADLINE_S",
    "REQUIRED_HANDOFF_WINDOW_S",
    "REQUIRED_STAGE_B_WINDOW_S",
    "HANDOFF_DEADLINE_S",
    "STAGE_B_WINDOW_DEADLINE_S",
)

TIMING_BUDGET_KEYS: tuple[str, ...] = (
    *POSITIVE_INT_BUDGET_KEYS,
    "BUDGET_APPROVAL_REQUIRED",
    "BUDGET_APPROVED_AT",
    "MEASURED_AT",
    "MEASURED_SOURCE_COMMIT",
)
TIMING_BUDGET_KEY_SET: frozenset[str] = frozenset(TIMING_BUDGET_KEYS)
_POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")
_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")

DEFAULT_OWNERSHIP_REL_PATH = Path(
    "var/dalton/cutover-snapshots/research-host/handoff-journal.json"
)
DEFAULT_BUDGETS_REL_PATH = Path(
    "var/dalton/cutover-snapshots/research-host/timing-budgets.env"
)
DEFAULT_ADMISSION_REL_PATH = Path("var/dalton/maintenance-admission.json")

EXTRA_PLUGIN_NAMES: tuple[str, ...] = (
    "dalton-funda-ai",
    "dalton-guidepoint",
    "dalton-sales-notes",
    "dalton-tmtb",
)

RESEARCH_HOST_REQUIRED_CREDENTIAL_KEYS: tuple[str, ...] = (
    "DALTON_RESEARCH_HOST_TOKEN",
    "DALTON_RESEARCH_ADMIN_TOKEN",
    "DALTON_BRIDGE_TOKEN",
    "GUIDEPOINT_CLIENT_ID",
    "GUIDEPOINT_CLIENT_SECRET",
)

_PLIST_PATH_ENV_KEYS: frozenset[str] = frozenset(
    {
        "HOME",
        "DALTON_REPO_ENV_FILE",
        "DALTON_BRIDGE_ENV_FILE",
        "DALTON_RESEARCH_HOST_ENV_FILE",
        "DALTON_RESEARCH_CODE",
        "DALTON_RESEARCH_HOME",
        "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH",
        "DALTON_GUIDEPOINT_OWNERSHIP_FILE",
        "DALTON_HOST_OWNERSHIP_FILE",
        "GUIDEPOINT_TOKENS_FILE",
        "DALTON_GUIDEPOINT_TOKENS_FILE",
        "GUIDEPOINT_ENV_FILE",
        "GUIDEPOINT_PYTHON",
        "DALTON_ALPHAENGINE_USAGE_FILE",
        "DALTON_WIKI_INGEST_AUDIT_FILE",
        "DALTON_WIKI_DATA_DIR",
    }
)

_CONTROL_SCAN_BASE_KEYS: tuple[str, ...] = (
    "APP_ENV",
    "DALTON_PROFILE",
    "DALTON_HOST_PLATFORM",
    "DALTON_ENABLED",
    "DALTON_RESEARCH_HOST_URL",
    "DALTON_RESEARCH_HOST_BASE_URL",
    "DALTON_BRIDGE_ENV_FILE",
    "DALTON_GUIDEPOINT_MCP_URL",
)

_GUARDED_ACTIONS: frozenset[str] = frozenset(
    {
        "set_target_owner",
        "stop_sandboxes",
        "drop_guidepoint_owner",
        "rewrite_bridge_url",
    }
)
_EXEMPT_ACTIONS: frozenset[str] = frozenset(
    {
        "kickstart_after_drain",
        "force_recreate_backend_edge",
    }
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _default_repo_root() -> Path:
    here = Path(__file__).resolve()
    # Normal worktree: <repo>/scripts/dalton/ops/prod/preflight.py
    if here.parent.name == "prod" and len(here.parents) >= 5:
        return here.parents[4]
    # Recovery bin: <repo>/var/dalton/cutover-snapshots/recovery-bin/preflight.py
    if here.parent.name == "recovery-bin" and len(here.parents) >= 5:
        return here.parents[4]
    return Path.cwd().resolve()


def _abs_no_deref(path: Path | str) -> Path:
    return Path(os.path.abspath(str(Path(path).expanduser())))


def _has_unexpanded_path_syntax(value: str) -> str | None:
    if _PLACEHOLDER_RE.search(value):
        return f"unreplaced placeholder in {value!r}"
    if "$HOME" in value or "${" in value:
        return f"unexpanded shell variable in {value!r}"
    # Check tilde at start or after ':' (e.g. PYTHONPATH)
    for part in value.split(":"):
        if part == "~" or part.startswith("~/"):
            return f"unexpanded tilde path in {value!r}"
    return None


def _is_literal_abs_path(value: str) -> bool:
    if not value or not value.startswith("/"):
        return False
    return _has_unexpanded_path_syntax(value) is None


def _parse_simple_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE pairs from a dotenv file without printing or executing it."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            result[key] = val
    return result


def _atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 5.0,
) -> tuple[int | None, Any, str | None]:
    req_headers = dict(headers or {})
    if body is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"

    curl_bin = str(os.environ.get("CURL_BIN") or "").strip()
    if curl_bin:
        cmd: list[str] = [
            curl_bin,
            "-sS",
            "--max-time",
            str(max(1, int(timeout_s))),
            "-X",
            method,
        ]
        for k, v in req_headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        if body is not None:
            cmd.extend(["--data-binary", body.decode("utf-8", errors="replace")])
        cmd.extend(["-w", "\n%{http_code}", url])
        rc, out, err = _run_cmd(cmd, timeout_s=max(5.0, timeout_s + 2.0))
        if rc != 0:
            return None, None, f"curl failed (rc={rc}): {err.strip()}"
        if "\n" in out:
            body_text, code_str = out.rsplit("\n", 1)
        else:
            body_text, code_str = "", out.strip()
        code_str = code_str.strip()
        if not code_str.isdigit():
            return None, None, f"curl returned non-numeric status: {code_str!r}"
        code = int(code_str)
        if code == 0:
            return None, None, "curl connection failed (status 000)"
        try:
            parsed = json.loads(body_text) if body_text.strip() else {}
        except ValueError:
            parsed = body_text
        return code, parsed, None

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = raw
            return int(resp.status), parsed, None
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = raw
        return int(exc.code), parsed, None
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _run_cmd(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 15.0,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(argv),
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return int(proc.returncode), proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"command timed out after {timeout_s}s: {argv[0]}"
    except OSError as exc:
        return 126, "", f"os error executing {argv[0]}: {exc}"


def _import_guidepoint_lock(repo_root: Path | None = None) -> Any:
    search_dirs: list[Path] = []
    if repo_root is not None:
        search_dirs.append(repo_root / "scripts" / "dalton" / "research_runtime")
    search_dirs.append(Path(__file__).resolve().parent)
    search_dirs.append(_default_repo_root() / "scripts" / "dalton" / "research_runtime")
    for d in search_dirs:
        s = str(d)
        if d.is_dir() and s not in sys.path:
            sys.path.insert(0, s)
    return importlib.import_module("guidepoint_lock")


def _import_capabilities(repo_root: Path) -> tuple[Any, Any]:
    for d in (repo_root / "src", repo_root):
        s = str(d)
        if d.is_dir() and s not in sys.path:
            sys.path.insert(0, s)
    caps_mod = importlib.import_module("src.dalton.capabilities")
    prof_mod = importlib.import_module("src.dalton.profile")
    return caps_mod, prof_mod


# ---------------------------------------------------------------------------
# Strict timing-budgets.env parsing and validation (shared by --switch & --approve-budgets)
# ---------------------------------------------------------------------------


def parse_timing_budgets_env(path: Path | str) -> tuple[dict[str, str], list[str]]:
    """Strictly parse `timing-budgets.env` (KEY=VALUE, no quotes, no expansion, no duplicates)."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {}, [f"timing-budgets.env missing: {p}"]
    try:
        raw_text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f"failed to read timing-budgets.env ({p}): {exc}"]

    data: dict[str, str] = {}
    errors: list[str] = []
    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            errors.append(f"line {lineno}: 'export' prefix is forbidden in timing-budgets.env")
            continue
        if "=" not in raw_line:
            errors.append(f"line {lineno}: expected KEY=VALUE")
            continue
        key, val = raw_line.split("=", 1)
        if key != key.strip() or not key:
            errors.append(f"line {lineno}: invalid whitespace around key {key!r}")
            continue
        if key not in TIMING_BUDGET_KEY_SET:
            errors.append(f"line {lineno}: unknown key {key!r}")
            continue
        if key in data:
            errors.append(f"line {lineno}: duplicate key {key!r}")
            continue
        if val != val.strip():
            errors.append(f"line {lineno}: leading/trailing whitespace in value for {key}")
            continue
        if any(ch in val for ch in ('"', "'", "$", "`")):
            errors.append(f"line {lineno}: quotes or shell expansion forbidden in value for {key}")
            continue
        data[key] = val
    return data, errors


def _validate_timing_budgets_mapping(mapping: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []

    for key in mapping:
        if str(key) not in TIMING_BUDGET_KEY_SET:
            errors.append(f"unknown key {key!r}")

    for key in TIMING_BUDGET_KEYS:
        if key not in mapping:
            errors.append(f"missing required key {key}")

    ints: dict[str, int] = {}
    for key in POSITIVE_INT_BUDGET_KEYS:
        if key not in mapping:
            continue
        raw_val = mapping[key]
        if isinstance(raw_val, bool):
            errors.append(f"{key} must be a positive integer, got bool")
            continue
        if isinstance(raw_val, int):
            if raw_val <= 0:
                errors.append(f"{key} must be a positive integer > 0, got {raw_val}")
            else:
                ints[key] = raw_val
            continue
        sval = str(raw_val)
        if not _POSITIVE_INT_RE.match(sval):
            errors.append(f"{key} must be a positive integer, got {raw_val!r}")
        else:
            ints[key] = int(sval)

    raw_approval_req = mapping.get("BUDGET_APPROVAL_REQUIRED")
    if isinstance(raw_approval_req, bool):
        approval_req_str = "true" if raw_approval_req else "false"
    else:
        approval_req_str = str(raw_approval_req) if raw_approval_req is not None else ""
    if "BUDGET_APPROVAL_REQUIRED" in mapping:
        if approval_req_str not in ("true", "false"):
            errors.append(
                f"BUDGET_APPROVAL_REQUIRED must be 'true' or 'false', got {raw_approval_req!r}"
            )
        elif approval_req_str != "false":
            errors.append("BUDGET_APPROVAL_REQUIRED must be false before cutover")

    for meta_key in ("MEASURED_AT", "MEASURED_SOURCE_COMMIT"):
        if meta_key in mapping and not str(mapping.get(meta_key) or "").strip():
            errors.append(f"{meta_key} must be non-empty")

    approved_at = str(mapping.get("BUDGET_APPROVED_AT") or "").strip()

    if len(ints) < len(POSITIVE_INT_BUDGET_KEYS):
        return errors

    probe_timeout_s = ints["PROBE_TIMEOUT_S"]
    drain_wait_s = ints["DRAIN_WAIT_S"]
    service_reload_s = ints["SERVICE_RELOAD_S"]
    forward_budget_s = ints["FORWARD_HANDOFF_BUDGET_S"]
    recovery_budget_s = ints["RECOVERY_BUDGET_S"]
    full_recovery_s = ints["FULL_RECOVERY_DEADLINE_S"]
    req_handoff_s = ints["REQUIRED_HANDOFF_WINDOW_S"]
    req_stage_b_s = ints["REQUIRED_STAGE_B_WINDOW_S"]
    handoff_deadline_s = ints["HANDOFF_DEADLINE_S"]
    stage_b_deadline_s = ints["STAGE_B_WINDOW_DEADLINE_S"]

    min_forward_s = drain_wait_s + service_reload_s + 2 * probe_timeout_s
    if forward_budget_s < min_forward_s:
        errors.append(
            f"FORWARD_HANDOFF_BUDGET_S ({forward_budget_s}) < "
            f"DRAIN_WAIT_S + SERVICE_RELOAD_S + 2 * PROBE_TIMEOUT_S ({min_forward_s})"
        )

    min_recovery_s = service_reload_s + 2 * probe_timeout_s
    if recovery_budget_s < min_recovery_s:
        errors.append(
            f"RECOVERY_BUDGET_S ({recovery_budget_s}) < "
            f"SERVICE_RELOAD_S + 2 * PROBE_TIMEOUT_S ({min_recovery_s})"
        )

    expected_req_handoff_s = forward_budget_s + recovery_budget_s
    if req_handoff_s != expected_req_handoff_s:
        errors.append(
            f"REQUIRED_HANDOFF_WINDOW_S ({req_handoff_s}) != "
            f"FORWARD_HANDOFF_BUDGET_S + RECOVERY_BUDGET_S ({expected_req_handoff_s})"
        )

    if handoff_deadline_s < expected_req_handoff_s:
        errors.append(
            f"HANDOFF_DEADLINE_S ({handoff_deadline_s}) < "
            f"FORWARD_HANDOFF_BUDGET_S + RECOVERY_BUDGET_S ({expected_req_handoff_s})"
        )

    if full_recovery_s < recovery_budget_s:
        errors.append(
            f"FULL_RECOVERY_DEADLINE_S ({full_recovery_s}) < RECOVERY_BUDGET_S ({recovery_budget_s})"
        )

    # Handoff window approval contract
    if req_handoff_s <= HANDOFF_DEFAULT_LIMIT_S:
        if handoff_deadline_s != HANDOFF_DEFAULT_LIMIT_S:
            errors.append(
                f"REQUIRED_HANDOFF_WINDOW_S ({req_handoff_s}) <= {HANDOFF_DEFAULT_LIMIT_S} "
                f"requires HANDOFF_DEADLINE_S == {HANDOFF_DEFAULT_LIMIT_S} (got {handoff_deadline_s})"
            )
    else:
        if not approved_at:
            errors.append(
                f"REQUIRED_HANDOFF_WINDOW_S ({req_handoff_s}) > {HANDOFF_DEFAULT_LIMIT_S} "
                "requires non-empty BUDGET_APPROVED_AT"
            )
        if handoff_deadline_s < req_handoff_s:
            errors.append(
                f"HANDOFF_DEADLINE_S ({handoff_deadline_s}) < REQUIRED_HANDOFF_WINDOW_S ({req_handoff_s})"
            )

    # Stage B window approval contract
    if req_stage_b_s <= STAGE_B_DEFAULT_LIMIT_S:
        if stage_b_deadline_s != STAGE_B_DEFAULT_LIMIT_S:
            errors.append(
                f"REQUIRED_STAGE_B_WINDOW_S ({req_stage_b_s}) <= {STAGE_B_DEFAULT_LIMIT_S} "
                f"requires STAGE_B_WINDOW_DEADLINE_S == {STAGE_B_DEFAULT_LIMIT_S} (got {stage_b_deadline_s})"
            )
    else:
        if not approved_at:
            errors.append(
                f"REQUIRED_STAGE_B_WINDOW_S ({req_stage_b_s}) > {STAGE_B_DEFAULT_LIMIT_S} "
                "requires non-empty BUDGET_APPROVED_AT"
            )
        if stage_b_deadline_s < req_stage_b_s:
            errors.append(
                f"STAGE_B_WINDOW_DEADLINE_S ({stage_b_deadline_s}) < "
                f"REQUIRED_STAGE_B_WINDOW_S ({req_stage_b_s})"
            )

    return errors


def validate_timing_budgets_env(path_or_mapping: Path | str | Mapping[str, Any]) -> list[str]:
    """Validate `timing-budgets.env` file or parsed mapping. Returns list of violations (empty = OK)."""
    if isinstance(path_or_mapping, Mapping):
        return _validate_timing_budgets_mapping(path_or_mapping)
    parsed, parse_errors = parse_timing_budgets_env(path_or_mapping)
    if parse_errors:
        return parse_errors
    return _validate_timing_budgets_mapping(parsed)


def format_timing_budgets_env(values: Mapping[str, Any]) -> str:
    """Serialize `timing-budgets.env` in canonical key order (KEY=VALUE, no quotes)."""
    lines: list[str] = []
    for key in TIMING_BUDGET_KEYS:
        val = values.get(key, "")
        if isinstance(val, bool):
            sval = "true" if val else "false"
        else:
            sval = str(val)
        lines.append(f"{key}={sval}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Preflight Context & Check Implementations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightConfig:
    repo_root: Path
    home_dir: Path
    plist_dir: Path
    backend_url: str
    proxy_url: str
    bridge_url: str
    research_host_url: str
    budgets_file: Path
    expect_admission: str = "any"
    proxy_listen_port: int = 8943
    launchctl_bin: str = "launchctl"
    lsof_bin: str = "lsof"
    ps_bin: str = "ps"
    docker_bin: str = "docker"
    uid: str = ""

    @property
    def repo_env_file(self) -> Path:
        return self.repo_root / ".env"

    @property
    def bridge_env_file(self) -> Path:
        return self.home_dir / ".config" / "dalton" / "researchdata-bridge.env"

    @property
    def research_host_env_file(self) -> Path:
        return self.home_dir / ".config" / "dalton" / "research-host.env"

    @property
    def ownership_file(self) -> Path:
        return (self.repo_root / DEFAULT_OWNERSHIP_REL_PATH).resolve()

    @property
    def venv_python(self) -> Path:
        return self.repo_root / ".venv" / "bin" / "python"

    @property
    def effective_uid(self) -> str:
        return self.uid or str(os.getuid())


def _result(check: str, status: str, detail: str) -> dict[str, str]:
    return {"check": check, "status": status, "detail": detail}


def _inspect_plist_file(
    plist_path: Path,
    *,
    expected_label: str,
    cfg: PreflightConfig,
    require_venv_python: bool = False,
    require_bridge_env_match: bool = False,
    require_ownership_match: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not plist_path.is_file():
        return None, [f"missing plist: {plist_path}"]
    try:
        raw_bytes = plist_path.read_bytes()
        data = plistlib.loads(raw_bytes)
    except Exception as exc:
        return None, [f"invalid plist {plist_path}: {exc}"]
    if not isinstance(data, dict):
        return None, [f"plist root is not dict: {plist_path}"]

    if str(data.get("Label") or "") != expected_label:
        errors.append(
            f"{plist_path.name}: Label={data.get('Label')!r}, expected {expected_label!r}"
        )

    # Scan all string fields for unexpanded syntax ($HOME, ~, ${, __X__)
    def _walk_strings(obj: Any, loc: str) -> None:
        if isinstance(obj, str):
            bad = _has_unexpanded_path_syntax(obj)
            if bad:
                errors.append(f"{plist_path.name} {loc}: {bad}")
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                _walk_strings(item, f"{loc}[{idx}]")
        elif isinstance(obj, dict):
            for k, v in obj.items():
                _walk_strings(v, f"{loc}.{k}")

    _walk_strings(data, "root")

    # Validate literal absolute paths for standard plist path keys
    for pkey in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        val = data.get(pkey)
        if val is not None and not _is_literal_abs_path(str(val)):
            errors.append(f"{plist_path.name}: {pkey} must be a literal absolute path (got {val!r})")

    prog_args = data.get("ProgramArguments")
    if not isinstance(prog_args, list) or not prog_args:
        errors.append(f"{plist_path.name}: missing ProgramArguments")
        prog_args = []
    else:
        if not _is_literal_abs_path(str(prog_args[0])):
            errors.append(
                f"{plist_path.name}: ProgramArguments[0] must be a literal absolute path (got {prog_args[0]!r})"
            )
        # Validate path-valued CLI flags like --env-file / --repo-env-file
        for idx, arg in enumerate(prog_args):
            if str(arg) in ("--env-file", "--repo-env-file") and idx + 1 < len(prog_args):
                nxt = str(prog_args[idx + 1])
                if not _is_literal_abs_path(nxt):
                    errors.append(
                        f"{plist_path.name}: {arg} value must be a literal absolute path (got {nxt!r})"
                    )

    env_vars = data.get("EnvironmentVariables")
    if not isinstance(env_vars, dict):
        errors.append(f"{plist_path.name}: missing EnvironmentVariables dict")
        env_vars = {}

    for k, v in env_vars.items():
        sv = str(v)
        if k in _PLIST_PATH_ENV_KEYS and not _is_literal_abs_path(sv):
            errors.append(
                f"{plist_path.name}: EnvironmentVariables.{k} must be a literal absolute path (got {sv!r})"
            )
        elif k == "PYTHONPATH":
            parts = [part for part in sv.split(":") if part]
            if not parts or any(not _is_literal_abs_path(part) for part in parts):
                errors.append(
                    f"{plist_path.name}: PYTHONPATH entries must be literal absolute paths (got {sv!r})"
                )

    repo_abs = cfg.repo_root.resolve()
    expected_py = str(repo_abs / ".venv" / "bin" / "python")
    expected_pythonpath = f"{repo_abs}/src:{repo_abs}"
    expected_repo_env = str(repo_abs / ".env")

    if require_venv_python:
        actual_py = str(prog_args[0]) if prog_args else ""
        if actual_py != expected_py:
            errors.append(
                f"{plist_path.name}: ProgramArguments[0]={actual_py!r}, expected {expected_py!r}"
            )
        actual_pp = str(env_vars.get("PYTHONPATH") or "")
        if actual_pp != expected_pythonpath:
            errors.append(
                f"{plist_path.name}: PYTHONPATH={actual_pp!r}, expected {expected_pythonpath!r}"
            )
        actual_repo_env = str(env_vars.get("DALTON_REPO_ENV_FILE") or "")
        if actual_repo_env != expected_repo_env:
            errors.append(
                f"{plist_path.name}: DALTON_REPO_ENV_FILE={actual_repo_env!r}, "
                f"expected {expected_repo_env!r}"
            )

    if require_bridge_env_match:
        raw_bridge_env = str(env_vars.get("DALTON_BRIDGE_ENV_FILE") or "").strip()
        expected_bridge_env = cfg.bridge_env_file.expanduser().resolve()
        if not raw_bridge_env:
            errors.append(f"{plist_path.name}: missing DALTON_BRIDGE_ENV_FILE")
        else:
            resolved_bridge_env = Path(raw_bridge_env).expanduser().resolve()
            if resolved_bridge_env != expected_bridge_env:
                errors.append(
                    f"{plist_path.name}: DALTON_BRIDGE_ENV_FILE resolves to {resolved_bridge_env}, "
                    f"expected {expected_bridge_env}"
                )

    if require_ownership_match:
        raw_own = str(env_vars.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip()
        expected_own = cfg.ownership_file
        if not raw_own:
            errors.append(f"{plist_path.name}: missing DALTON_GUIDEPOINT_OWNERSHIP_FILE")
        else:
            resolved_own = Path(raw_own).expanduser().resolve()
            if resolved_own != expected_own:
                errors.append(
                    f"{plist_path.name}: DALTON_GUIDEPOINT_OWNERSHIP_FILE resolves to {resolved_own}, "
                    f"expected {expected_own}"
                )

    return data, errors


def _check_backend_admission_gate(cfg: PreflightConfig) -> dict[str, str]:
    repo_vals = _parse_simple_env_file(cfg.repo_env_file)
    svc_token = (
        repo_vals.get("INTERNAL_SERVICE_TOKEN")
        or os.environ.get("INTERNAL_SERVICE_TOKEN")
        or ""
    ).strip()
    headers = {"X-Service-Token": svc_token} if svc_token else {}
    base = cfg.backend_url.rstrip("/")

    status_code, status_body, err = _http_request(
        "GET",
        f"{base}/api/v1/dalton/maintenance/admission",
        headers=headers,
    )
    if err is not None:
        return _result("backend_admission_gate", "fail", f"现网后端缺暂停能力或不可达 ({err})")
    if status_code == 404:
        return _result(
            "backend_admission_gate",
            "fail",
            "现网后端缺暂停能力 (/api/v1/dalton/maintenance/admission 返回 404)",
        )
    if status_code != 200 or not isinstance(status_body, dict):
        return _result(
            "backend_admission_gate",
            "fail",
            f"现网后端缺暂停能力或鉴权异常 (GET status={status_code})",
        )
    if not isinstance(status_body.get("paused"), bool) or not isinstance(
        status_body.get("revision"), int
    ):
        return _result(
            "backend_admission_gate",
            "fail",
            "现网后端缺暂停能力 (GET 返回缺少 paused/revision 结构)",
        )

    # POST /maintenance/admission/probe is verified read-only / side-effect-free
    probe_code, probe_body, probe_err = _http_request(
        "POST",
        f"{base}/api/v1/dalton/maintenance/admission/probe",
        headers=headers,
        body=b"{}",
    )
    if probe_err is not None:
        return _result("backend_admission_gate", "fail", f"准入探针请求失败 ({probe_err})")
    if probe_code == 404:
        return _result(
            "backend_admission_gate",
            "fail",
            "现网后端缺暂停能力 (/api/v1/dalton/maintenance/admission/probe 返回 404)",
        )

    def _is_maint_503(code: int | None, body: Any) -> bool:
        if code != 503 or not isinstance(body, dict):
            return False
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        return any(
            str(detail.get(k) or "") == "maintenance_in_progress"
            for k in ("error_code", "error", "status")
        )

    is_paused = bool(status_body["paused"])
    rev = int(status_body["revision"])
    expect = cfg.expect_admission

    if expect == "paused":
        if not is_paused or not _is_maint_503(probe_code, probe_body):
            return _result(
                "backend_admission_gate",
                "fail",
                f"expected paused (503 maintenance_in_progress), got paused={is_paused} probe_http={probe_code}",
            )
    elif expect == "open":
        if is_paused or probe_code != 200 or not (
            isinstance(probe_body, dict) and probe_body.get("admitted") is True
        ):
            return _result(
                "backend_admission_gate",
                "fail",
                f"expected open (200 admitted=true), got paused={is_paused} probe_http={probe_code}",
            )
    else:
        if is_paused:
            if not _is_maint_503(probe_code, probe_body):
                return _result(
                    "backend_admission_gate",
                    "fail",
                    f"gate reports paused=true but probe returned HTTP {probe_code}",
                )
        else:
            if probe_code != 200 or not (
                isinstance(probe_body, dict) and probe_body.get("admitted") is True
            ):
                return _result(
                    "backend_admission_gate",
                    "fail",
                    f"gate reports paused=false but probe returned HTTP {probe_code}",
                )

    return _result(
        "backend_admission_gate",
        "ok",
        f"paused={is_paused} revision={rev} probe_http={probe_code}",
    )


def _parse_launchctl_list_pids(stdout: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid_raw, _status, label = parts[0], parts[1], parts[2]
        if label == "Label" and pid_raw == "PID":
            continue
        if pid_raw.isdigit():
            result[label] = int(pid_raw)
        else:
            result[label] = None
    return result


def _parse_lsof_listen_pids(stdout: str) -> set[int]:
    pids: set[int] = set()
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("COMMAND"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
        elif len(parts) == 1 and parts[0].isdigit():
            pids.add(int(parts[0]))
    return pids


def _check_guidepoint_proxy_pid_match(cfg: PreflightConfig) -> dict[str, str]:
    label = "com.dalton.guidepoint-mcp-proxy"
    rc_lc, out_lc, err_lc = _run_cmd([cfg.launchctl_bin, "list"])
    if rc_lc != 0:
        return _result("guidepoint_proxy_pid_match", "fail", f"launchctl list failed: {err_lc.strip()}")
    label_pids = _parse_launchctl_list_pids(out_lc)
    if label not in label_pids:
        return _result("guidepoint_proxy_pid_match", "fail", f"launchctl list missing label {label}")
    label_pid = label_pids[label]
    if label_pid is None or label_pid <= 0:
        return _result(
            "guidepoint_proxy_pid_match",
            "fail",
            f"launchctl label {label} has no running PID",
        )

    rc_lsof, out_lsof, err_lsof = _run_cmd(
        [cfg.lsof_bin, "-nP", f"-iTCP:{cfg.proxy_listen_port}", "-sTCP:LISTEN"]
    )
    if rc_lsof != 0 and not out_lsof.strip():
        return _result(
            "guidepoint_proxy_pid_match",
            "fail",
            f"no LISTEN socket found on :{cfg.proxy_listen_port} ({err_lsof.strip() or 'empty'})",
        )
    listen_pids = _parse_lsof_listen_pids(out_lsof)
    if not listen_pids:
        return _result(
            "guidepoint_proxy_pid_match",
            "fail",
            f"no LISTEN PID parsed on :{cfg.proxy_listen_port}",
        )
    if listen_pids != {label_pid}:
        return _result(
            "guidepoint_proxy_pid_match",
            "fail",
            f"PID mismatch: launchctl {label}={label_pid}, lsof :{cfg.proxy_listen_port}={sorted(listen_pids)}",
        )
    return _result(
        "guidepoint_proxy_pid_match",
        "ok",
        f"{label} PID={label_pid} matches :{cfg.proxy_listen_port} LISTEN PID",
    )


def _check_stage_a_plists(cfg: PreflightConfig) -> dict[str, str]:
    all_errors: list[str] = []
    proxy_data, e1 = _inspect_plist_file(
        cfg.plist_dir / "com.dalton.guidepoint-mcp-proxy.plist",
        expected_label="com.dalton.guidepoint-mcp-proxy",
        cfg=cfg,
        require_venv_python=False,
        require_bridge_env_match=False,
        require_ownership_match=True,
    )
    all_errors.extend(e1)

    _, e2 = _inspect_plist_file(
        cfg.plist_dir / "com.dalton.researchdata-bridge.plist",
        expected_label="com.dalton.researchdata-bridge",
        cfg=cfg,
        require_venv_python=True,
        require_bridge_env_match=True,
        require_ownership_match=False,
    )
    all_errors.extend(e2)

    host_data, e3 = _inspect_plist_file(
        cfg.plist_dir / "com.dalton.research-host.plist",
        expected_label="com.dalton.research-host",
        cfg=cfg,
        require_venv_python=True,
        require_bridge_env_match=False,
        require_ownership_match=True,
    )
    all_errors.extend(e3)

    if proxy_data and host_data:
        p_own = str((proxy_data.get("EnvironmentVariables") or {}).get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "")
        h_own = str((host_data.get("EnvironmentVariables") or {}).get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "")
        if p_own and h_own and Path(p_own).expanduser().resolve() != Path(h_own).expanduser().resolve():
            all_errors.append(f"ownership file mismatch between proxy ({p_own}) and research-host ({h_own})")

    if all_errors:
        return _result("stage_a_plists", "fail", "; ".join(all_errors))
    return _result("stage_a_plists", "ok", "all three LaunchAgent plists valid")


def _check_research_host_env(
    cfg: PreflightConfig, *, require_distinct_tokens: bool = False
) -> dict[str, str]:
    p = cfg.research_host_env_file
    if not p.is_file():
        return _result("research_host_env", "fail", f"missing {p}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode != 0o600:
        return _result("research_host_env", "fail", f"{p} mode is {oct(mode)}, expected 0o600")
    vals = _parse_simple_env_file(p)
    missing = [k for k in RESEARCH_HOST_REQUIRED_CREDENTIAL_KEYS if not str(vals.get(k) or "").strip()]
    if missing:
        return _result(
            "research_host_env",
            "fail",
            f"{p} missing required credential key(s): {', '.join(missing)}",
        )

    errors: list[str] = []
    if require_distinct_tokens:
        t_host = vals["DALTON_RESEARCH_HOST_TOKEN"].strip()
        t_admin = vals["DALTON_RESEARCH_ADMIN_TOKEN"].strip()
        t_bridge = vals["DALTON_BRIDGE_TOKEN"].strip()
        if len({t_host, t_admin, t_bridge}) != 3:
            errors.append(
                "DALTON_RESEARCH_HOST_TOKEN, DALTON_RESEARCH_ADMIN_TOKEN, and DALTON_BRIDGE_TOKEN must be pairwise distinct"
            )

    for k, v in vals.items():
        if k in _PLIST_PATH_ENV_KEYS or k.endswith(("_FILE", "_DIR", "_HOME", "_CODE", "_ROOT", "_PATH")):
            if v and not _is_literal_abs_path(v):
                errors.append(f"{k} must be a literal absolute path without shell/tilde expansion")

    if errors:
        return _result("research_host_env", "fail", "; ".join(errors))
    return _result("research_host_env", "ok", f"{p} mode=0600 keys present and valid")


def _resolve_tokens_path_from_config(cfg: PreflightConfig) -> tuple[Path | None, str | None]:
    try:
        gl = _import_guidepoint_lock(cfg.repo_root)
    except Exception as exc:
        return None, f"cannot import guidepoint_lock: {exc}"

    merged_env: dict[str, str] = {"HOME": str(cfg.home_dir)}
    for plist_name in ("com.dalton.guidepoint-mcp-proxy.plist", "com.dalton.research-host.plist"):
        pp = cfg.plist_dir / plist_name
        if pp.is_file():
            try:
                pdata = plistlib.loads(pp.read_bytes())
                ev = pdata.get("EnvironmentVariables") if isinstance(pdata, dict) else None
                if isinstance(ev, dict):
                    for k in ("GUIDEPOINT_TOKENS_FILE", "DALTON_GUIDEPOINT_TOKENS_FILE", "DALTON_RESEARCH_HOME"):
                        if ev.get(k) and k not in merged_env:
                            merged_env[k] = str(ev[k])
            except Exception:
                pass
    for env_path in (cfg.research_host_env_file, cfg.bridge_env_file):
        ev = _parse_simple_env_file(env_path)
        for k in ("GUIDEPOINT_TOKENS_FILE", "DALTON_GUIDEPOINT_TOKENS_FILE", "DALTON_RESEARCH_HOME"):
            if ev.get(k) and k not in merged_env:
                merged_env[k] = str(ev[k])

    if not merged_env.get("DALTON_RESEARCH_HOME"):
        merged_env["DALTON_RESEARCH_HOME"] = str(
            cfg.home_dir / "Library" / "Application Support" / "Everflow" / "research"
        )
    return gl.resolve_guidepoint_tokens_file(environ=merged_env), None


def _check_guidepoint_tokens_json(cfg: PreflightConfig) -> dict[str, str]:
    tokens_path, err = _resolve_tokens_path_from_config(cfg)
    if err or tokens_path is None:
        return _result("guidepoint_tokens_json", "fail", err or "could not resolve tokens path")
    if not tokens_path.is_file():
        return _result("guidepoint_tokens_json", "fail", f"missing tokens file: {tokens_path}")
    try:
        raw = json.loads(tokens_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _result("guidepoint_tokens_json", "fail", f"invalid JSON in {tokens_path}: {exc}")
    if not isinstance(raw, dict):
        return _result("guidepoint_tokens_json", "fail", f"{tokens_path} root is not a JSON object")

    missing = []
    for k in ("access_token", "refresh_token"):
        if not str(raw.get(k) or "").strip():
            missing.append(k)
    for k in ("obtained_at", "expires_in"):
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            missing.append(k)
        elif k == "expires_in" and v <= 0:
            missing.append(f"{k}(<=0)")
    if missing:
        return _result(
            "guidepoint_tokens_json",
            "fail",
            f"{tokens_path} missing/invalid field(s): {', '.join(missing)}",
        )
    return _result("guidepoint_tokens_json", "ok", f"{tokens_path} valid JSON with required fields")


def _check_venv_python_imports(cfg: PreflightConfig) -> dict[str, str]:
    venv_py = cfg.venv_python
    if not (venv_py.is_file() and os.access(venv_py, os.X_OK)):
        return _result("venv_python_imports", "fail", f"missing executable venv python: {venv_py}")

    # Build PYTHONPATH from plist config (falling back to canonical paths)
    repo_abs = cfg.repo_root.resolve()
    pp_parts: list[str] = []
    for plist_name in ("com.dalton.research-host.plist", "com.dalton.researchdata-bridge.plist"):
        pp = cfg.plist_dir / plist_name
        if pp.is_file():
            try:
                pdata = plistlib.loads(pp.read_bytes())
                ev = pdata.get("EnvironmentVariables") if isinstance(pdata, dict) else {}
                if isinstance(ev, dict):
                    for key in ("PYTHONPATH", "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH"):
                        raw = str(ev.get(key) or "").strip()
                        for entry in re.split(r"[:,]", raw):
                            e = entry.strip()
                            if e and e not in pp_parts:
                                pp_parts.append(e)
            except Exception:
                pass
    for fallback in (
        str(repo_abs / "src"),
        str(repo_abs),
        str(repo_abs / "scripts" / "dalton" / "research_runtime"),
    ):
        if fallback not in pp_parts:
            pp_parts.append(fallback)

    sub_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cfg.home_dir),
        "PYTHONPATH": ":".join(pp_parts),
    }
    rc, _out, err = _run_cmd(
        [
            str(venv_py),
            "-c",
            "import guidepoint_lock, fastapi, uvicorn, httpx, src.dalton.capabilities",
        ],
        env=sub_env,
        timeout_s=20.0,
    )
    if rc != 0:
        return _result(
            "venv_python_imports",
            "fail",
            f"import probe failed (rc={rc}): {err.strip().splitlines()[-1] if err.strip() else 'unknown'}",
        )
    return _result(
        "venv_python_imports",
        "ok",
        "guidepoint_lock, fastapi, uvicorn, httpx, src.dalton.capabilities importable",
    )


def _check_web_releases_current(cfg: PreflightConfig) -> dict[str, str]:
    releases_root = (cfg.repo_root / "web" / "releases").resolve()
    current_link = cfg.repo_root / "web" / "releases" / "current"
    if not current_link.is_symlink():
        return _result(
            "web_releases_current",
            "fail",
            f"{current_link} is not a symlink",
        )
    if not current_link.exists():
        return _result(
            "web_releases_current",
            "fail",
            f"{current_link} is a dangling symlink",
        )
    resolved = current_link.resolve()
    if not resolved.is_dir():
        return _result(
            "web_releases_current",
            "fail",
            f"{current_link} target is not a directory: {resolved}",
        )
    try:
        rel = resolved.relative_to(releases_root)
    except ValueError:
        return _result(
            "web_releases_current",
            "fail",
            f"{current_link} escapes web/releases/ ({resolved})",
        )
    if str(rel) in ("", "."):
        return _result(
            "web_releases_current",
            "fail",
            f"{current_link} points to web/releases/ itself instead of a build directory",
        )

    missing: list[str] = []
    if not (resolved / "index.html").is_file():
        missing.append("index.html")
    if not (resolved / "version.json").is_file():
        missing.append("version.json")
    else:
        try:
            vj = json.loads((resolved / "version.json").read_text(encoding="utf-8"))
            if not isinstance(vj, dict):
                missing.append("version.json(not object)")
        except Exception:
            missing.append("version.json(invalid JSON)")
    if not (resolved / "assets").is_dir():
        missing.append("assets/")
    if not (resolved / "help" / "index.html").is_file():
        missing.append("help/index.html")

    if missing:
        return _result(
            "web_releases_current",
            "fail",
            f"{resolved} missing/invalid build artifact(s): {', '.join(missing)}",
        )
    return _result("web_releases_current", "ok", f"current -> {rel} (all 4 artifacts present)")


# ---------------------------------------------------------------------------
# --switch mode checks (D1.4)
# ---------------------------------------------------------------------------


def _resolve_guidepoint_env_file(cfg: PreflightConfig) -> Path:
    proxy_plist = cfg.plist_dir / "com.dalton.guidepoint-mcp-proxy.plist"
    if proxy_plist.is_file():
        try:
            pdata = plistlib.loads(proxy_plist.read_bytes())
            ev = pdata.get("EnvironmentVariables") if isinstance(pdata, dict) else {}
            if isinstance(ev, dict) and str(ev.get("GUIDEPOINT_ENV_FILE") or "").strip():
                return Path(str(ev["GUIDEPOINT_ENV_FILE"])).expanduser().resolve()
        except Exception:
            pass
    bridge_vals = _parse_simple_env_file(cfg.bridge_env_file)
    raw_home = str(bridge_vals.get("DALTON_RESEARCH_HOME") or "").strip()
    if raw_home:
        return Path(raw_home).expanduser().resolve() / "config" / "guidepoint_mcp.env"
    return (
        cfg.home_dir
        / "Library"
        / "Application Support"
        / "Everflow"
        / "research"
        / "config"
        / "guidepoint_mcp.env"
    )


def _check_live_services_health_and_drain(cfg: PreflightConfig) -> dict[str, str]:
    errors: list[str] = []
    bridge_vals = _parse_simple_env_file(cfg.bridge_env_file)
    bridge_admin_tok = str(bridge_vals.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()

    gp_env_file = _resolve_guidepoint_env_file(cfg)
    gp_vals = _parse_simple_env_file(gp_env_file)
    gp_admin_tok = str(gp_vals.get("GUIDEPOINT_PROXY_ADMIN_TOKEN") or "").strip()

    bridge_base = cfg.bridge_url.rstrip("/")
    proxy_base = cfg.proxy_url.rstrip("/")

    # 1. :8960/healthz
    c, _, e = _http_request("GET", f"{bridge_base}/healthz")
    if e or c != 200:
        errors.append(f"bridge /healthz expected 200, got {c} ({e or 'bad status'})")

    # 2. :8960/admin/drain
    b_hdr = {"Authorization": f"Bearer {bridge_admin_tok}"} if bridge_admin_tok else {}
    c, _, e = _http_request("GET", f"{bridge_base}/admin/drain", headers=b_hdr)
    if e or c != 200:
        errors.append(f"bridge /admin/drain expected 200, got {c} ({e or 'bad status'})")

    # 3. :8943/healthz (owner_lock_held == true and draining == false)
    c, body, e = _http_request("GET", f"{proxy_base}/healthz")
    if e or c != 200 or not isinstance(body, dict):
        errors.append(f"proxy /healthz expected 200 JSON, got {c} ({e or 'bad response'})")
    else:
        if body.get("owner_lock_held") is not True:
            errors.append(f"proxy /healthz owner_lock_held={body.get('owner_lock_held')!r} (expected True)")
        if body.get("draining") is not False:
            errors.append(f"proxy /healthz draining={body.get('draining')!r} (expected False)")

    # 4. :8943/admin/drain
    p_hdr = {"Authorization": f"Bearer {gp_admin_tok}"} if gp_admin_tok else {}
    c, _, e = _http_request("GET", f"{proxy_base}/admin/drain", headers=p_hdr)
    if e or c != 200:
        errors.append(f"proxy /admin/drain expected 200, got {c} ({e or 'bad status'})")

    if errors:
        return _result("live_services_health_and_drain", "fail", "; ".join(errors))
    return _result(
        "live_services_health_and_drain",
        "ok",
        "bridge & proxy /healthz and /admin/drain returned 200 (proxy holds lock, not draining)",
    )


def _check_bridge_plist_and_budgets(cfg: PreflightConfig) -> dict[str, str]:
    errors: list[str] = []
    _, plist_errors = _inspect_plist_file(
        cfg.plist_dir / "com.dalton.researchdata-bridge.plist",
        expected_label="com.dalton.researchdata-bridge",
        cfg=cfg,
        require_venv_python=True,
        require_bridge_env_match=True,
        require_ownership_match=False,
    )
    errors.extend(plist_errors)

    budget_errors = validate_timing_budgets_env(cfg.budgets_file)
    errors.extend(budget_errors)

    if errors:
        return _result("bridge_plist_and_budgets", "fail", "; ".join(errors))
    return _result("bridge_plist_and_budgets", "ok", "bridge plist and timing-budgets.env valid")


def _check_extra_plugins(cfg: PreflightConfig) -> dict[str, str]:
    plugins_dir = cfg.repo_root / "scripts" / "dalton" / "research_runtime" / "extra_plugins"
    missing: list[str] = []
    for name in EXTRA_PLUGIN_NAMES:
        pdir = plugins_dir / name
        if not pdir.is_dir():
            missing.append(name)
            continue
        for req in ("plugin.json", "mcp.json"):
            if not (pdir / req).is_file():
                missing.append(f"{name}/{req}")
    if missing:
        return _result("extra_plugins", "fail", f"missing extra plugin(s): {', '.join(missing)}")
    return _result("extra_plugins", "ok", f"all {len(EXTRA_PLUGIN_NAMES)} extra plugins present")


def _check_two_sided_guidepoint_paths(cfg: PreflightConfig) -> dict[str, str]:
    try:
        gl = _import_guidepoint_lock(cfg.repo_root)
    except Exception as exc:
        return _result("two_sided_guidepoint_paths", "fail", f"cannot import guidepoint_lock: {exc}")

    def _side_env(plist_name: str, env_file: Path | None) -> dict[str, str]:
        env_map: dict[str, str] = {"HOME": str(cfg.home_dir)}
        if env_file is not None and env_file.is_file():
            env_map.update(_parse_simple_env_file(env_file))
        pp = cfg.plist_dir / plist_name
        if pp.is_file():
            try:
                pdata = plistlib.loads(pp.read_bytes())
                ev = pdata.get("EnvironmentVariables") if isinstance(pdata, dict) else {}
                if isinstance(ev, dict):
                    for k, v in ev.items():
                        env_map[str(k)] = str(v)
            except Exception:
                pass
        if not env_map.get("DALTON_RESEARCH_HOME"):
            env_map["DALTON_RESEARCH_HOME"] = str(
                cfg.home_dir / "Library" / "Application Support" / "Everflow" / "research"
            )
        return env_map

    gp_env_file = _resolve_guidepoint_env_file(cfg)
    proxy_env = _side_env("com.dalton.guidepoint-mcp-proxy.plist", gp_env_file)
    host_env = _side_env("com.dalton.research-host.plist", cfg.research_host_env_file)

    proxy_tokens = gl.resolve_guidepoint_tokens_file(environ=proxy_env)
    host_tokens = gl.resolve_guidepoint_tokens_file(environ=host_env)
    proxy_lock = gl.resolve_guidepoint_lock_file(proxy_tokens)
    host_lock = gl.resolve_guidepoint_lock_file(host_tokens)
    proxy_own = gl.resolve_ownership_file(repo_root=cfg.repo_root, environ=proxy_env)
    host_own = gl.resolve_ownership_file(repo_root=cfg.repo_root, environ=host_env)

    errors: list[str] = []
    if proxy_tokens != host_tokens:
        errors.append(f"tokens path mismatch: proxy={proxy_tokens} vs host={host_tokens}")
    if proxy_lock != host_lock:
        errors.append(f"owner lock path mismatch: proxy={proxy_lock} vs host={host_lock}")
    if proxy_own != host_own:
        errors.append(f"handoff-journal path mismatch: proxy={proxy_own} vs host={host_own}")
    if proxy_own != cfg.ownership_file or host_own != cfg.ownership_file:
        errors.append(
            f"handoff-journal path does not match canonical {cfg.ownership_file}: proxy={proxy_own}, host={host_own}"
        )

    if errors:
        return _result("two_sided_guidepoint_paths", "fail", "; ".join(errors))
    return _result(
        "two_sided_guidepoint_paths",
        "ok",
        f"tokens={host_tokens}, lock={host_lock}, journal={host_own}",
    )


def _read_compose_backend_env(cfg: PreflightConfig) -> tuple[dict[str, str] | None, str]:
    """Read-only `docker compose config --format json` for Everflow backend service."""
    compose_base = cfg.repo_root / "docker-compose.yml"
    compose_dalton = cfg.repo_root / "docker-compose.dalton.yml"
    if not (compose_base.is_file() and compose_dalton.is_file()):
        return None, "compose_files_absent"

    cmd = [
        cfg.docker_bin,
        "compose",
        "-f",
        str(compose_base),
        "-f",
        str(compose_dalton),
        "config",
        "--format",
        "json",
    ]
    sub_env = dict(os.environ)
    sub_env["HOME"] = str(cfg.home_dir)
    rc, out, err = _run_cmd(cmd, env=sub_env, timeout_s=15.0)
    if rc != 0:
        return None, f"compose_config_failed(rc={rc}: {err.strip()[:120]})"
    try:
        parsed = json.loads(out)
        svc = (parsed.get("services") or {}).get("backend") or {}
        raw_env = svc.get("environment") or {}
        env_dict: dict[str, str] = {}
        if isinstance(raw_env, dict):
            for k, v in raw_env.items():
                if v is not None:
                    env_dict[str(k)] = str(v)
        elif isinstance(raw_env, list):
            for item in raw_env:
                if isinstance(item, str) and "=" in item:
                    k, v = item.split("=", 1)
                    env_dict[k] = v
        return env_dict, "compose_config"
    except Exception as exc:
        return None, f"compose_json_error({exc})"


def _load_plist_env_vars(plist_path: Path) -> dict[str, str]:
    if not plist_path.is_file():
        return {}
    try:
        data = plistlib.loads(plist_path.read_bytes())
        ev = data.get("EnvironmentVariables") if isinstance(data, dict) else {}
        if isinstance(ev, dict):
            return {str(k): str(v) for k, v in ev.items()}
    except Exception:
        pass
    return {}


def _check_three_way_capability_parity(cfg: PreflightConfig) -> dict[str, str]:
    try:
        caps_mod, prof_mod = _import_capabilities(cfg.repo_root)
    except Exception as exc:
        return _result("three_way_capability_parity", "fail", f"cannot import src.dalton.capabilities: {exc}")

    repo_env = cfg.repo_env_file
    compose_env, compose_src = _read_compose_backend_env(cfg)
    backend_merged = caps_mod.load_deployment_capability_env(
        repo_env_file=repo_env,
        environ=compose_env if compose_env is not None else {},
    )

    host_plist_env = _load_plist_env_vars(cfg.plist_dir / "com.dalton.research-host.plist")
    bridge_plist_env = _load_plist_env_vars(cfg.plist_dir / "com.dalton.researchdata-bridge.plist")

    host_merged = caps_mod.load_deployment_capability_env(
        repo_env_file=repo_env,
        service_env_file=cfg.research_host_env_file,
        environ=host_plist_env,
    )
    bridge_merged = caps_mod.load_deployment_capability_env(
        repo_env_file=repo_env,
        service_env_file=cfg.bridge_env_file,
        environ=bridge_plist_env,
    )

    def _snapshot(env_map: Mapping[str, str]) -> dict[str, Any]:
        prof = prof_mod.resolve_profile(
            app_env=env_map.get("APP_ENV"),
            dalton_profile=env_map.get("DALTON_PROFILE"),
        )
        plat = caps_mod.resolve_host_platform(profile=prof, environ=env_map)
        caps = sorted(c.value for c in caps_mod.effective_capabilities(profile=prof, environ=env_map))
        b3 = bool(caps_mod.is_batch3_connectors_enabled(profile=prof, environ=env_map))
        return {
            "profile": prof.value,
            "host_platform": plat,
            "capabilities": caps,
            "batch3_enabled": b3,
        }

    s_backend = _snapshot(backend_merged)
    s_host = _snapshot(host_merged)
    s_bridge = _snapshot(bridge_merged)

    errors: list[str] = []
    if s_backend != s_host:
        errors.append(f"backend vs research-host mismatch: {s_backend} != {s_host}")
    if s_backend != s_bridge:
        errors.append(f"backend vs bridge mismatch: {s_backend} != {s_bridge}")

    expected_bridge_env = cfg.bridge_env_file.expanduser().resolve()
    for svc_label, svc_file in (
        ("research-host.env", cfg.research_host_env_file),
        ("researchdata-bridge.env", cfg.bridge_env_file),
    ):
        conflicts = caps_mod.find_capability_env_conflicts(
            repo_env_file=repo_env,
            service_env_file=svc_file,
            environ={},
            expected_bridge_env_file=expected_bridge_env,
        )
        for c in conflicts:
            errors.append(f"{svc_label} conflict on {c.get('key')}: {c.get('reason')}")

    if errors:
        return _result("three_way_capability_parity", "fail", "; ".join(errors))
    return _result(
        "three_way_capability_parity",
        "ok",
        f"profile={s_backend['profile']} platform={s_backend['host_platform']} "
        f"batch3={s_backend['batch3_enabled']} ({compose_src})",
    )


def _extract_proc_env_vars(ps_output: str, target_keys: set[str]) -> dict[str, str]:
    """Extract target KEY=VALUE assignments from `ps -wwwE -p <pid>` output."""
    found: dict[str, str] = {}
    for token in ps_output.split():
        if "=" not in token:
            continue
        k, v = token.split("=", 1)
        if k in target_keys:
            found[k] = v
    return found


def _get_service_pid_readonly(cfg: PreflightConfig, label: str) -> int | None:
    rc, out, _ = _run_cmd([cfg.launchctl_bin, "print", f"gui/{cfg.effective_uid}/{label}"])
    if rc == 0 and out:
        m = re.search(r"^\s*pid\s*=\s*([0-9]+)\b", out, flags=re.MULTILINE)
        if m:
            return int(m.group(1))
    rc2, out2, _ = _run_cmd([cfg.launchctl_bin, "list"])
    if rc2 == 0 and out2:
        pids = _parse_launchctl_list_pids(out2)
        return pids.get(label)
    return None


def _check_env_override_scan(cfg: PreflightConfig) -> dict[str, str]:
    try:
        caps_mod, _ = _import_capabilities(cfg.repo_root)
    except Exception as exc:
        return _result("env_override_scan", "fail", f"cannot import src.dalton.capabilities: {exc}")

    repo_vals = _parse_simple_env_file(cfg.repo_env_file)
    scan_keys: set[str] = set(_CONTROL_SCAN_BASE_KEYS)
    for k in repo_vals:
        if caps_mod.is_capability_control_env_key(k):
            scan_keys.add(k)
    for cap_id in caps_mod.CapabilityId:
        scan_keys.add(f"DALTON_CAP_{cap_id.name}")

    extra_sources: dict[str, dict[str, str]] = {}
    warnings: list[str] = []

    # 1. launchctl getenv for each target key
    lc_env: dict[str, str] = {}
    for key in sorted(scan_keys):
        rc, out, _ = _run_cmd([cfg.launchctl_bin, "getenv", key])
        val = out.strip()
        if rc == 0 and val:
            lc_env[key] = val
    if lc_env:
        extra_sources["launchctl_getenv"] = lc_env

    # 2. All com.dalton.*.plist in --plist-dir
    if cfg.plist_dir.is_dir():
        for pp in sorted(cfg.plist_dir.glob("com.dalton.*.plist")):
            ev = _load_plist_env_vars(pp)
            if ev:
                extra_sources[f"plist:{pp.name}"] = ev

    # 3. Running process environments for bridge and research-host
    for label in ("com.dalton.researchdata-bridge", "com.dalton.research-host"):
        pid = _get_service_pid_readonly(cfg, label)
        if pid is None or pid <= 0:
            continue
        rc, out, err = _run_cmd([cfg.ps_bin, "-wwwE", "-p", str(pid)])
        if rc != 0 or not out.strip():
            warnings.append(f"could not read process env for {label} (pid={pid}): {err.strip() or f'rc={rc}'}")
            continue
        proc_vars = _extract_proc_env_vars(out, scan_keys)
        if proc_vars:
            extra_sources[f"proc:{label}(pid={pid})"] = proc_vars

    expected_bridge_env = cfg.bridge_env_file.expanduser().resolve()
    conflicts = caps_mod.find_capability_env_conflicts(
        repo_env_file=cfg.repo_env_file,
        service_env_file=cfg.bridge_env_file,
        environ={},
        extra_sources=extra_sources,
        expected_bridge_env_file=expected_bridge_env,
    )
    if conflicts:
        details = [
            f"{c.get('source')} {c.get('key')}={c.get('value')!r} ({c.get('reason')})"
            for c in conflicts
        ]
        return _result("env_override_scan", "fail", "; ".join(details))

    if warnings:
        return _result("env_override_scan", "warn", "; ".join(warnings))
    return _result("env_override_scan", "ok", "no conflicting env overrides found")


def run_preflight(mode: str, cfg: PreflightConfig, *, report_json: bool = False) -> tuple[int, list[dict[str, str]]]:
    """Execute preflight checks for `stage-a` or `switch` mode."""
    if mode == "stage-a":
        checks: list[Callable[[PreflightConfig], dict[str, str]]] = [
            _check_backend_admission_gate,
            _check_guidepoint_proxy_pid_match,
            _check_stage_a_plists,
            lambda c: _check_research_host_env(c, require_distinct_tokens=False),
            _check_guidepoint_tokens_json,
            _check_venv_python_imports,
            _check_web_releases_current,
        ]
    elif mode == "switch":
        checks = [
            _check_live_services_health_and_drain,
            _check_bridge_plist_and_budgets,
            lambda c: _check_research_host_env(c, require_distinct_tokens=True),
            _check_extra_plugins,
            _check_venv_python_imports,
            _check_web_releases_current,
            _check_two_sided_guidepoint_paths,
            _check_three_way_capability_parity,
            _check_env_override_scan,
        ]
    else:
        raise ValueError(f"Unsupported preflight mode: {mode!r}")

    results: list[dict[str, str]] = []
    failed = False
    for fn in checks:
        res = fn(cfg)
        results.append(res)
        if res["status"] == "fail":
            failed = True
            if not report_json:
                return 1, results
    return (1 if failed else 0), results


# ---------------------------------------------------------------------------
# Shared helper subcommands (journal, recovery-manifest, approve-budgets, guard)
# ---------------------------------------------------------------------------


def _coerce_journal_value(raw: str) -> Any:
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw == "null":
        return None
    return raw


def cmd_journal_set(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    gl = _import_guidepoint_lock(repo_root)
    ownership_path = Path(args.ownership_file).expanduser().resolve()
    current = gl.read_ownership_journal(ownership_file=ownership_path)
    for item in args.pairs:
        if "=" not in item:
            print(f"ERROR: expected key=value pair, got {item!r}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            print(f"ERROR: empty key in {item!r}", file=sys.stderr)
            return 2
        current[k] = _coerce_journal_value(v)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    gl.write_ownership_journal_atomic(current, ownership_file=ownership_path)
    if args.print_json:
        print(json.dumps(current, ensure_ascii=False))
    return 0


def cmd_journal_read(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    gl = _import_guidepoint_lock(repo_root)
    ownership_path = Path(args.ownership_file).expanduser().resolve()
    data = gl.read_ownership_journal(ownership_file=ownership_path)
    if args.key:
        val = data.get(args.key)
        if isinstance(val, bool):
            print("true" if val else "false")
        elif val is None:
            print("")
        else:
            print(str(val))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_manifest_write(args: argparse.Namespace) -> int:
    rec_dir = Path(args.recovery_dir).expanduser().resolve()
    if not rec_dir.is_dir():
        print(f"ERROR: recovery dir not found: {rec_dir}", file=sys.stderr)
        return 1
    files_meta: dict[str, dict[str, Any]] = {}
    for name in args.files:
        fp = rec_dir / name
        if not fp.is_file():
            print(f"ERROR: missing file for manifest: {fp}", file=sys.stderr)
            return 1
        files_meta[name] = {
            "sha256": _sha256_file(fp),
            "size": fp.stat().st_size,
        }
    manifest = {
        "version": 1,
        "source_commit": str(args.source_commit or "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files_meta,
    }
    manifest_path = rec_dir / "recovery-manifest.json"
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    return 0


def cmd_manifest_verify(args: argparse.Namespace) -> int:
    rec_dir = Path(args.recovery_dir).expanduser().resolve()
    manifest_path = rec_dir / "recovery-manifest.json"
    if not manifest_path.is_file():
        print(f"ERROR: missing recovery-manifest.json in {rec_dir}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: invalid recovery-manifest.json: {exc}", file=sys.stderr)
        return 1
    files_meta = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files_meta, dict) or not files_meta:
        print("ERROR: recovery-manifest.json has no files entries", file=sys.stderr)
        return 1
    errors: list[str] = []
    for name, info in sorted(files_meta.items()):
        fp = rec_dir / name
        if not fp.is_file():
            errors.append(f"missing file: {name}")
            continue
        expected = str((info or {}).get("sha256") or "")
        actual = _sha256_file(fp)
        if not expected or actual != expected:
            errors.append(f"sha256 mismatch for {name}: expected={expected[:12]} actual={actual[:12]}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_approve_budgets(args: argparse.Namespace) -> int:
    budgets_path = Path(args.budgets_file).expanduser().resolve()
    parsed, parse_errors = parse_timing_budgets_env(budgets_path)
    if parse_errors:
        print("ERROR: timing-budgets.env 校验未通过：", file=sys.stderr)
        for err in parse_errors:
            print(f"  - {err}", file=sys.stderr)
        print("请核对参数或运行 --measure-budgets 重新测量。", file=sys.stderr)
        return 1

    violations: list[str] = []

    def _parse_pos_int(key: str, raw: str | None) -> int | None:
        if raw is None or not _POSITIVE_INT_RE.match(str(raw)):
            violations.append(f"{key} must be a positive integer (got {raw!r})")
            return None
        return int(str(raw))

    req_handoff_s = _parse_pos_int("REQUIRED_HANDOFF_WINDOW_S", parsed.get("REQUIRED_HANDOFF_WINDOW_S"))
    req_stage_b_s = _parse_pos_int("REQUIRED_STAGE_B_WINDOW_S", parsed.get("REQUIRED_STAGE_B_WINDOW_S"))

    handoff_arg = args.handoff_deadline_s
    stage_b_m_arg = args.stage_b_window_m

    if handoff_arg is not None and not _POSITIVE_INT_RE.match(str(handoff_arg)):
        violations.append(f"--handoff-deadline-s must be a positive integer (got {handoff_arg!r})")
    if stage_b_m_arg is not None and not _POSITIVE_INT_RE.match(str(stage_b_m_arg)):
        violations.append(f"--stage-b-window-m must be a positive integer (got {stage_b_m_arg!r})")

    candidate: dict[str, str] = dict(parsed)

    if req_handoff_s is not None:
        if req_handoff_s > HANDOFF_DEFAULT_LIMIT_S:
            if handoff_arg is None:
                violations.append(
                    f"REQUIRED_HANDOFF_WINDOW_S ({req_handoff_s}s) > {HANDOFF_DEFAULT_LIMIT_S}s: "
                    "must supply --handoff-deadline-s"
                )
            elif _POSITIVE_INT_RE.match(str(handoff_arg)):
                candidate["HANDOFF_DEADLINE_S"] = str(int(handoff_arg))
        else:
            if handoff_arg is not None:
                violations.append(
                    f"REQUIRED_HANDOFF_WINDOW_S ({req_handoff_s}s) <= {HANDOFF_DEFAULT_LIMIT_S}s: "
                    "--handoff-deadline-s is not allowed when handoff window did not exceed 90s"
                )

    if req_stage_b_s is not None:
        if req_stage_b_s > STAGE_B_DEFAULT_LIMIT_S:
            if stage_b_m_arg is None:
                violations.append(
                    f"REQUIRED_STAGE_B_WINDOW_S ({req_stage_b_s}s) > {STAGE_B_DEFAULT_LIMIT_S}s: "
                    "must supply --stage-b-window-m"
                )
            elif _POSITIVE_INT_RE.match(str(stage_b_m_arg)):
                candidate["STAGE_B_WINDOW_DEADLINE_S"] = str(int(stage_b_m_arg) * 60)
        else:
            if stage_b_m_arg is not None:
                violations.append(
                    f"REQUIRED_STAGE_B_WINDOW_S ({req_stage_b_s}s) <= {STAGE_B_DEFAULT_LIMIT_S}s: "
                    "--stage-b-window-m is not allowed when Stage B window did not exceed 60m"
                )

    candidate["BUDGET_APPROVED_AT"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    candidate["BUDGET_APPROVAL_REQUIRED"] = "false"

    # Re-validate the full candidate via the exact same validate_timing_budgets_env() function
    violations.extend(validate_timing_budgets_env(candidate))
    if violations:
        print("ERROR: 预算审批复核未通过，原 timing-budgets.env 保持不变：", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print("请核对参数或运行 --measure-budgets 重新测量。", file=sys.stderr)
        return 1

    _atomic_write_text(budgets_path, format_timing_budgets_env(candidate), mode=0o600)
    print(
        f"BUDGET_APPROVAL_OK: HANDOFF_DEADLINE_S={candidate['HANDOFF_DEADLINE_S']} "
        f"STAGE_B_WINDOW_DEADLINE_S={candidate['STAGE_B_WINDOW_DEADLINE_S']} "
        f"BUDGET_APPROVED_AT={candidate['BUDGET_APPROVED_AT']}"
    )
    return 0


def cmd_guard_check(args: argparse.Namespace) -> int:
    action = str(args.action or "").strip()
    if action in _EXEMPT_ACTIONS:
        return 0
    if action not in _GUARDED_ACTIONS:
        print(f"ERROR: unknown change guard action: {action!r}", file=sys.stderr)
        return 2

    missing_reasons: list[str] = []

    # Condition 1: current process holds .ops.lock
    if str(args.lock_held) != "1":
        missing_reasons.append("current process does not hold .ops.lock")

    # Condition 2: maintenance-admission.json fully paused (paused == true and allow_workspace_ids == [])
    adm_path = Path(args.admission_file).expanduser().resolve()
    if not adm_path.is_file():
        missing_reasons.append(f"maintenance-admission.json missing ({adm_path})")
    else:
        try:
            adm = json.loads(adm_path.read_text(encoding="utf-8"))
            if not isinstance(adm, dict):
                raise ValueError("not a JSON object")
            paused = adm.get("paused") is True
            allow_ids = adm.get("allow_workspace_ids")
            empty_allow = isinstance(allow_ids, list) and len(allow_ids) == 0
            if not paused:
                missing_reasons.append("maintenance-admission.json paused != true")
            if not empty_allow:
                missing_reasons.append(
                    f"maintenance-admission.json allow_workspace_ids != [] (got {allow_ids!r})"
                )
        except Exception as exc:
            missing_reasons.append(f"maintenance-admission.json unreadable ({exc})")

    # Condition 3: handoff-journal.json completed == false
    j_path = Path(args.journal_file).expanduser().resolve()
    if not j_path.is_file():
        missing_reasons.append(f"handoff-journal.json missing ({j_path})")
    else:
        try:
            j = json.loads(j_path.read_text(encoding="utf-8"))
            if not isinstance(j, dict):
                raise ValueError("not a JSON object")
            if j.get("completed") is not False:
                missing_reasons.append(
                    f"handoff-journal.json completed != false (got {j.get('completed')!r})"
                )
        except Exception as exc:
            missing_reasons.append(f"handoff-journal.json unreadable ({exc})")

    if missing_reasons:
        print(
            f"ERROR: change guard denied action {action!r}: " + "; ".join(missing_reasons),
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_status_summary(args: argparse.Namespace) -> int:
    j_path = Path(args.journal_file).expanduser().resolve()
    a_path = Path(args.admission_file).expanduser().resolve()
    b_path = Path(args.budgets_file).expanduser().resolve() if args.budgets_file else None

    print("=== Current Cutover State Summary (read-only) ===")
    if j_path.is_file():
        try:
            j = json.loads(j_path.read_text(encoding="utf-8"))
            print(
                f"handoff-journal.json: exists=true operation={j.get('operation', '')} "
                f"phase={j.get('phase', '')} target_owner={j.get('target_owner', '')} "
                f"completed={j.get('completed', '')} updated_at={j.get('updated_at', '')}"
            )
        except Exception as exc:
            print(f"handoff-journal.json: exists=true unreadable ({exc})")
    else:
        print(f"handoff-journal.json: missing ({j_path})")

    if a_path.is_file():
        try:
            a = json.loads(a_path.read_text(encoding="utf-8"))
            print(
                f"maintenance-admission.json: exists=true paused={a.get('paused', '')} "
                f"pause_host_direct={a.get('pause_host_direct', '')} "
                f"allow_workspace_ids={a.get('allow_workspace_ids', [])} "
                f"revision={a.get('revision', '')}"
            )
        except Exception as exc:
            print(f"maintenance-admission.json: exists=true unreadable ({exc})")
    else:
        print(f"maintenance-admission.json: missing ({a_path})")

    if args.include_dry_run_plan:
        print("")
        print("=== Planned 7-Step Stage B Cutover Sequence (dry-run) ===")
        steps = (
            "1. Record switch_pausing_admission intent, lock maintenance admission (allow_workspace_ids=[]), verify idle & preflight, snapshot + baseline-snapshot.json",
            "2. Bootstrap com.dalton.research-host (:8951) in standby and run read-only probes",
            "3. Start handoff clock (T_trigger), drain :8943, archive plist & bootout to release .guidepoint.owner.lock",
            "4. Set target_owner=research_host_8951, call :8951/admin/guidepoint/acquire-lock & run bounded search probe",
            "5. Drain & kickstart :8960 with :8951 Guidepoint URL and run parallel 3-way real probes",
            "6. Build frontend (--out-dir), close migrated :8960 routes first, force-recreate backend+edge & stop old sandboxes",
            "7. Verify optional --test-workspace-id (new OK + legacy route_migrated), resume admission, mark completed=true",
        )
        for line in steps:
            print(f"  {line}")
        print("")
        print("=== Timing Budgets ===")
        if b_path is None or not b_path.is_file():
            print(f"timing-budgets.env: missing ({b_path})")
        else:
            parsed, errs = parse_timing_budgets_env(b_path)
            if errs:
                print(f"timing-budgets.env: parse error ({'; '.join(errs)})")
            else:
                print(f"  T_trigger (FORWARD_HANDOFF_BUDGET_S): {parsed.get('FORWARD_HANDOFF_BUDGET_S', '<missing>')}s")
                print(f"  RECOVERY_BUDGET_S:                    {parsed.get('RECOVERY_BUDGET_S', '<missing>')}s")
                print(f"  HANDOFF_DEADLINE_S:                   {parsed.get('HANDOFF_DEADLINE_S', '<missing>')}s")
                print(f"  STAGE_B_WINDOW_DEADLINE_S:            {parsed.get('STAGE_B_WINDOW_DEADLINE_S', '<missing>')}s")
                print(f"  BUDGET_APPROVAL_REQUIRED:             {parsed.get('BUDGET_APPROVAL_REQUIRED', '<missing>')}")
    return 0


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def _build_preflight_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Everflow Research Host cutover preflight (read-only) & shared helpers"
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stage-a", action="store_true", help="Run Stage A preflight checks")
    mode.add_argument("--switch", action="store_true", help="Run Stage B switch preflight checks")
    p.add_argument("--report-json", action="store_true", help="Run all checks and emit JSON report")
    p.add_argument(
        "--expect-admission",
        choices=("any", "paused", "open"),
        default="any",
        help="Expected backend admission gate state for --stage-a (default: any)",
    )
    p.add_argument("--repo", default=None, help="Repository root path")
    p.add_argument("--home", default=None, help="Home directory path")
    p.add_argument("--plist-dir", default=None, help="LaunchAgents plist directory")
    p.add_argument("--backend-url", default="http://127.0.0.1:8000")
    p.add_argument("--proxy-url", default="http://127.0.0.1:8943")
    p.add_argument("--bridge-url", default="http://127.0.0.1:8960")
    p.add_argument("--research-host-url", default="http://127.0.0.1:8951")
    p.add_argument("--proxy-listen-port", type=int, default=8943)
    p.add_argument("--budgets-file", default=None)
    return p


def _build_subcommand_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Shared cutover ops helpers")
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_js = sub.add_parser("journal-set")
    p_js.add_argument("--ownership-file", required=True)
    p_js.add_argument("--repo", default=None)
    p_js.add_argument("--print-json", action="store_true")
    p_js.add_argument("pairs", nargs="+")

    p_jr = sub.add_parser("journal-read")
    p_jr.add_argument("--ownership-file", required=True)
    p_jr.add_argument("--repo", default=None)
    p_jr.add_argument("--key", default=None)

    p_mw = sub.add_parser("manifest-write")
    p_mw.add_argument("--recovery-dir", required=True)
    p_mw.add_argument("--source-commit", required=True)
    p_mw.add_argument("--file", dest="files", action="append", required=True)

    p_mv = sub.add_parser("manifest-verify")
    p_mv.add_argument("--recovery-dir", required=True)

    p_ab = sub.add_parser("approve-budgets")
    p_ab.add_argument("--budgets-file", required=True)
    p_ab.add_argument("--handoff-deadline-s", default=None)
    p_ab.add_argument("--stage-b-window-m", default=None)

    p_gc = sub.add_parser("guard-check")
    p_gc.add_argument("--action", required=True)
    p_gc.add_argument("--admission-file", required=True)
    p_gc.add_argument("--journal-file", required=True)
    p_gc.add_argument("--lock-held", required=True)

    p_ss = sub.add_parser("status-summary")
    p_ss.add_argument("--journal-file", required=True)
    p_ss.add_argument("--admission-file", required=True)
    p_ss.add_argument("--budgets-file", default=None)
    p_ss.add_argument("--include-dry-run-plan", action="store_true")

    return p


_SUBCOMMANDS = frozenset(
    {
        "journal-set",
        "journal-read",
        "manifest-write",
        "manifest-verify",
        "approve-budgets",
        "guard-check",
        "status-summary",
    }
)


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in _SUBCOMMANDS:
        ns = _build_subcommand_parser().parse_args(args_list)
        if ns.subcommand == "journal-set":
            return cmd_journal_set(ns)
        if ns.subcommand == "journal-read":
            return cmd_journal_read(ns)
        if ns.subcommand == "manifest-write":
            return cmd_manifest_write(ns)
        if ns.subcommand == "manifest-verify":
            return cmd_manifest_verify(ns)
        if ns.subcommand == "approve-budgets":
            return cmd_approve_budgets(ns)
        if ns.subcommand == "guard-check":
            return cmd_guard_check(ns)
        if ns.subcommand == "status-summary":
            return cmd_status_summary(ns)
        return 2

    ns = _build_preflight_parser().parse_args(args_list)
    repo_root = Path(ns.repo).expanduser().resolve() if ns.repo else _default_repo_root()
    home_dir = Path(ns.home).expanduser().resolve() if ns.home else Path.home().resolve()
    plist_dir = (
        Path(ns.plist_dir).expanduser().resolve()
        if ns.plist_dir
        else (home_dir / "Library" / "LaunchAgents")
    )
    budgets_file = (
        Path(ns.budgets_file).expanduser().resolve()
        if ns.budgets_file
        else (repo_root / DEFAULT_BUDGETS_REL_PATH)
    )

    cfg = PreflightConfig(
        repo_root=repo_root,
        home_dir=home_dir,
        plist_dir=plist_dir,
        backend_url=ns.backend_url,
        proxy_url=ns.proxy_url,
        bridge_url=ns.bridge_url,
        research_host_url=ns.research_host_url,
        budgets_file=budgets_file,
        expect_admission=ns.expect_admission,
        proxy_listen_port=int(ns.proxy_listen_port),
        launchctl_bin=os.environ.get("LAUNCHCTL_BIN", "launchctl"),
        lsof_bin=os.environ.get("LSOF_BIN", "lsof"),
        ps_bin=os.environ.get("PS_BIN", "ps"),
        docker_bin=os.environ.get("DOCKER_BIN", "docker"),
        uid=os.environ.get("DALTON_LAUNCHD_UID", ""),
    )

    mode = "stage-a" if ns.stage_a else "switch"
    rc, results = run_preflight(mode, cfg, report_json=ns.report_json)
    if ns.report_json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "ok": rc == 0,
                    "checks": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for r in results:
            tag = r["status"].upper()
            stream = sys.stderr if r["status"] == "fail" else sys.stdout
            print(f"[{tag}] {r['check']}: {r['detail']}", file=stream)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
