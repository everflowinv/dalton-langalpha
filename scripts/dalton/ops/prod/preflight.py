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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

# Strictly prevent read-only preflight and helpers from writing __pycache__ into production trees.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

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

RECOVERY_REQUIRED_FILES: tuple[str, ...] = (
    "rollback_research_host.sh",
    "snapshot_research_host.sh",
    "_research_host_handoff_lib.sh",
    "wait_backend_idle.sh",
    "preflight.py",
    "guidepoint_lock.py",
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_HINT_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|AUTH)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _default_repo_root() -> Path:
    here = Path(__file__).resolve()
    # Normal worktree: <repo>/scripts/dalton/ops/prod/preflight.py
    if here.parent.name == "prod" and len(here.parents) >= 5:
        return here.parents[4]
    # Recovery bin (canonical or versioned sibling release under cutover-snapshots):
    # <repo>/var/dalton/cutover-snapshots/<recovery-bin|.recovery-bin.rel.XXXXXX>/preflight.py
    if (
        here.parent.name == "recovery-bin" or here.parent.name.startswith(".recovery-bin.rel.")
    ) and len(here.parents) >= 5 and here.parents[1].name == "cutover-snapshots":
        return here.parents[4]
    return Path.cwd().resolve()


def _abs_no_deref(path: Path | str) -> Path:
    return Path(os.path.abspath(str(Path(path).expanduser())))


def _has_unexpanded_path_syntax(value: str) -> str | None:
    """Return ONLY the error category (never raw value) if unexpanded path/template syntax is present."""
    if _PLACEHOLDER_RE.search(value):
        return "unreplaced_placeholder"
    if "$HOME" in value or "${" in value:
        return "unexpanded_shell_variable"
    # Check tilde at start or after ':' (e.g. PYTHONPATH)
    for part in value.split(":"):
        if part == "~" or part.startswith("~/"):
            return "unexpanded_tilde_path"
    return None


def _is_literal_abs_path(value: str) -> bool:
    if not value or not value.startswith("/"):
        return False
    return _has_unexpanded_path_syntax(value) is None


def _strip_dotenv_value(raw_val: str, *, strict_hash_comment: bool = False) -> str:
    """Strip quotes or trailing `#` comments matching `deploy_main.sh` `.env` parsing."""
    val = raw_val.strip()
    if not val:
        return ""
    if val[0] == '"':
        end = val.find('"', 1)
        return val[1:end] if end > 0 else val[1:]
    if val[0] == "'":
        end = val.find("'", 1)
        return val[1:end] if end > 0 else val[1:]
    if strict_hash_comment:
        return re.sub(r"[ \t]*#.*$", "", val).strip()
    return re.sub(r"[ \t]+#.*$", "", val).strip()


def _parse_simple_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE pairs from a dotenv file without printing or executing it."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        if key:
            strict_hash = key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PATH_SEPARATOR")
            result[key] = _strip_dotenv_value(val, strict_hash_comment=strict_hash)
    return result


def _sanitize_url_for_diag(raw_url: str) -> str:
    """Strip credentials (userinfo), query strings, and fragments from a URL before logging."""
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError:
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc:
        return "<redacted>"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = "***@" if (parsed.username is not None or parsed.password is not None) else ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{userinfo}{host}{port}{path}"


def _sanitize_diag_value(key: str, value: Any) -> str:
    sval = str(value or "").strip()
    if not sval:
        return "<empty>"
    if _SECRET_KEY_HINT_RE.search(key):
        return "<redacted>"
    if "://" in sval:
        return _sanitize_url_for_diag(sval)
    if key.endswith("_URL"):
        return "<redacted-url>"
    return sval


_GENERIC_URI_RE = re.compile(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s"'<>)\],;]+""")


def _sanitize_error_text(text: str) -> str:
    """Redact credentials, URL userinfo, URL queries, URL fragments, and bearer/header tokens."""
    cleaned = _GENERIC_URI_RE.sub(lambda m: _sanitize_url_for_diag(m.group(0)), text)
    cleaned = re.sub(r"(?i)\b((?:Bearer|Basic|Token)\s+)[^\s;\"']+", r"\1<redacted>", cleaned)
    cleaned = re.sub(
        r"(?i)\b(Authorization|X-Service-Token)\s*:\s*[^\r\n;,]+",
        r"\1: <redacted>",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)\b(password|passwd|secret|token|api_key|client_secret)\s*([:=])\s*[^\s,;\"']+",
        r"\1\2<redacted>",
        cleaned,
    )
    return cleaned


def _fsync_dir(path: Path, *, strict: bool = True) -> None:
    try:
        dfd = os.open(str(path), os.O_RDONLY)
    except OSError:
        if strict:
            raise
        return
    try:
        os.fsync(dfd)
    except OSError:
        if strict:
            raise
    finally:
        os.close(dfd)


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
        _fsync_dir(path.parent, strict=True)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _curl_escape_cfg_val(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _extract_response_headers(raw_hdrs: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if raw_hdrs is None:
        return out
    items_fn = getattr(raw_hdrs, "items", None)
    if callable(items_fn):
        try:
            for k, v in items_fn():
                if isinstance(k, str) and isinstance(v, str):
                    out[k.lower()] = v.strip()
        except Exception:
            pass
    return out


def _parse_curl_header_dump(raw_text: str) -> dict[str, str]:
    """Parse the final HTTP response header block from `curl -D` output."""
    out: dict[str, str] = {}
    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip("\r")
        if not line:
            continue
        if line.upper().startswith("HTTP/"):
            out = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _http_request_with_headers(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 5.0,
    capture_headers: bool = True,
) -> tuple[int | None, Any, dict[str, str], str | None]:
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
        cfg_path: str | None = None
        hdr_path: str | None = None
        try:
            if req_headers:
                fd, cfg_path = tempfile.mkstemp(prefix=".dalton-curl-", suffix=".cfg")
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        for k, v in req_headers.items():
                            esc = _curl_escape_cfg_val(f"{k}: {v}")
                            fh.write(f'header = "{esc}"\n')
                        fh.flush()
                except Exception:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    raise
                cmd.extend(["-K", cfg_path])
            if capture_headers:
                h_fd, hdr_path = tempfile.mkstemp(prefix=".dalton-curl-", suffix=".hdr")
                try:
                    os.fchmod(h_fd, 0o600)
                finally:
                    try:
                        os.close(h_fd)
                    except OSError:
                        pass
                cmd.extend(["-D", hdr_path])
            if body is not None:
                cmd.extend(["--data-binary", body.decode("utf-8", errors="replace")])
            cmd.extend(["-w", "\n%{http_code}", url])
            rc, out, err = _run_cmd(cmd, timeout_s=max(5.0, timeout_s + 2.0))
            resp_headers: dict[str, str] = {}
            if hdr_path and os.path.isfile(hdr_path):
                try:
                    resp_headers = _parse_curl_header_dump(Path(hdr_path).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    resp_headers = {}
        finally:
            for tmp_p in (cfg_path, hdr_path):
                if tmp_p:
                    try:
                        os.unlink(tmp_p)
                    except OSError:
                        pass
        if rc != 0:
            return None, None, {}, f"curl failed (rc={rc}): {_sanitize_error_text(err.strip())}"
        if "\n" in out:
            body_text, code_str = out.rsplit("\n", 1)
        else:
            body_text, code_str = "", out.strip()
        code_str = code_str.strip()
        if not code_str.isdigit():
            return None, None, {}, f"curl returned non-numeric status: {code_str!r}"
        code = int(code_str)
        if code == 0:
            return None, None, {}, "curl connection failed (status 000)"
        try:
            parsed = json.loads(body_text) if body_text.strip() else {}
        except ValueError:
            parsed = body_text
        return code, parsed, resp_headers, None

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_headers = _extract_response_headers(getattr(resp, "headers", None))
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                parsed = raw
            return int(resp.status), parsed, resp_headers, None
    except urllib.error.HTTPError as exc:
        resp_headers = _extract_response_headers(getattr(exc, "headers", None))
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = raw
        return int(exc.code), parsed, resp_headers, None
    except Exception as exc:
        return None, None, {}, f"{type(exc).__name__}: {_sanitize_error_text(str(exc))}"


def _http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: float = 5.0,
) -> tuple[int | None, Any, str | None]:
    code, parsed, _resp_headers, err = _http_request_with_headers(
        method,
        url,
        headers=headers,
        body=body,
        timeout_s=timeout_s,
        capture_headers=False,
    )
    return code, parsed, err


def _run_cmd(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout_s: float = 15.0,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            list(argv),
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
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


def _load_module_from_path(module_name: str, file_path: Path) -> Any:
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module spec from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_running_from_recovery_bin() -> bool:
    here = Path(__file__).resolve()
    parent_name = here.parent.name
    grandparent_name = here.parents[1].name if len(here.parents) >= 2 else ""
    return (
        parent_name == "recovery-bin"
        or parent_name.startswith(".recovery-bin.rel.")
        or grandparent_name == "cutover-snapshots"
    )


def _import_guidepoint_lock(repo_root: Path | None = None) -> Any:
    """Load `guidepoint_lock.py`.

    When executing from `recovery-bin/` (or a staged sibling release), fail closed if the
    same-batch sibling `guidepoint_lock.py` is missing — NEVER fall back to the worktree copy.
    """
    sys.dont_write_bytecode = True
    sibling = Path(__file__).resolve().parent / "guidepoint_lock.py"
    if _is_running_from_recovery_bin():
        if not sibling.is_file() or sibling.is_symlink():
            raise ImportError(
                f"same-batch recovery-bin guidepoint_lock.py missing: {sibling} (refusing worktree fallback)"
            )
        return _load_module_from_path("_dalton_recovery_guidepoint_lock", sibling)

    if sibling.is_file():
        return _load_module_from_path("_dalton_recovery_guidepoint_lock", sibling)

    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(repo_root / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py")
    candidates.append(_default_repo_root() / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py")
    for cand in candidates:
        if cand.is_file():
            return _load_module_from_path("_dalton_worktree_guidepoint_lock", cand)
    raise ImportError("guidepoint_lock.py not found in recovery-bin or repo research_runtime")


def _import_bridge_subprocess_env(repo_root: Path | None = None) -> Any:
    """Load shared `bridge_subprocess_env.py` so preflight & renderer use one parser/resolver."""
    sys.dont_write_bytecode = True
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(repo_root / "scripts" / "dalton" / "bridge_subprocess_env.py")
    candidates.append(_default_repo_root() / "scripts" / "dalton" / "bridge_subprocess_env.py")
    for cand in candidates:
        if cand.is_file():
            return _load_module_from_path("_dalton_shared_bridge_subprocess_env", cand)
    raise ImportError("bridge_subprocess_env.py not found")


def _import_capabilities(repo_root: Path) -> tuple[Any, Any]:
    sys.dont_write_bytecode = True
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
    skip_budgets: bool = False
    proxy_listen_port: int = 8943
    research_host_plist: Path | None = None
    archived_plist_dir: Path | None = None
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
    def snapshots_root(self) -> Path:
        return (self.repo_root / "var" / "dalton" / "cutover-snapshots").resolve()

    @property
    def venv_python(self) -> Path:
        return self.repo_root / ".venv" / "bin" / "python"

    @property
    def effective_uid(self) -> str:
        return self.uid or str(os.getuid())

    def resolve_research_host_plist(self, mode: str) -> tuple[Path | None, str | None]:
        """Resolve `com.dalton.research-host.plist` for the given stage layout (§6.4b).

        - `stage-a`: reads from `--research-host-plist` or `--plist-dir`.
        - `switch` (Stage B): Stage A moves `com.dalton.research-host.plist` OUT of
          `~/Library/LaunchAgents/` into `archived-launchagents/`; default lookup reads the
          archived plist (`--research-host-plist` > `--archived-plist-dir` >
          `<journal.snapshot_dir>/archived-launchagents/` >
          `cutover-snapshots/research-host/archived-launchagents/` >
          latest `cutover-snapshots/stage-a/*/archived-launchagents/`). When post-cutover
          (`target_owner == "research_host_8951"`), the active LaunchAgents copy is also checked.
        """
        fname = "com.dalton.research-host.plist"
        if self.research_host_plist is not None:
            p = self.research_host_plist.expanduser().resolve()
            if not p.is_file():
                return None, f"missing research-host plist: {p}"
            return p, None

        if mode == "stage-a":
            p = self.plist_dir / fname
            if not p.is_file():
                return None, f"missing plist: {p}"
            return p, None

        if self.archived_plist_dir is not None:
            p = self.archived_plist_dir.expanduser().resolve() / fname
            if not p.is_file():
                return None, f"missing archived research-host plist: {p}"
            return p, None

        checked: list[str] = []
        journal, j_err = _read_journal_strict(self.ownership_file)
        if j_err is not None:
            return None, j_err

        target_owner = str((journal or {}).get("target_owner") or "").strip()
        snap_raw = str((journal or {}).get("snapshot_dir") or "").strip()
        if snap_raw:
            snap_dir = Path(snap_raw).expanduser().resolve()
            try:
                snap_dir.relative_to(self.snapshots_root)
            except ValueError:
                return None, f"untrusted snapshot_dir outside cutover-snapshots: {snap_dir}"
            cand = snap_dir / "archived-launchagents" / fname
            checked.append(str(cand))
            if cand.is_file():
                return cand, None

        canonical_archived = self.snapshots_root / "research-host" / "archived-launchagents" / fname
        checked.append(str(canonical_archived))
        if canonical_archived.is_file():
            return canonical_archived, None

        stage_a_root = self.snapshots_root / "stage-a"
        if stage_a_root.is_dir():
            for snap in sorted(stage_a_root.iterdir(), key=lambda p: p.name, reverse=True):
                if not snap.is_dir():
                    continue
                cand = snap / "archived-launchagents" / fname
                checked.append(str(cand))
                if cand.is_file():
                    return cand, None

        if target_owner == "research_host_8951":
            active_p = self.plist_dir / fname
            checked.append(str(active_p))
            if active_p.is_file():
                return active_p, None

        return None, (
            f"missing archived {fname} (Stage A must archive it out of LaunchAgents; "
            f"checked: {', '.join(checked) if checked else str(canonical_archived)})"
        )


def _result(check: str, status: str, detail: str) -> dict[str, str]:
    return {"check": check, "status": status, "detail": detail}


def _extract_flag_values(prog_args: Sequence[Any], flag: str) -> tuple[list[str], list[str]]:
    """Extract all occurrences of `flag` from `ProgramArguments`, failing on duplicates/missing values."""
    values: list[str] = []
    errors: list[str] = []
    idx = 0
    n = len(prog_args)
    while idx < n:
        s = str(prog_args[idx])
        if s == "--":
            break
        if s == flag:
            if idx + 1 >= n:
                errors.append(f"{flag}: missing_value")
                idx += 1
            else:
                values.append(str(prog_args[idx + 1]))
                idx += 2
            continue
        if s.startswith(flag + "="):
            values.append(s[len(flag) + 1 :])
            idx += 1
            continue
        idx += 1
    if len(values) > 1:
        errors.append(f"{flag}: duplicate_argument ({len(values)} occurrences)")
    return values, errors


def _inspect_plist_file(
    plist_path: Path,
    *,
    expected_label: str,
    cfg: PreflightConfig,
    require_venv_python: bool = False,
    require_bridge_env_match: bool = False,
    require_ownership_match: bool = False,
    require_research_host_startup: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not plist_path.is_file():
        return None, [f"missing plist: {plist_path}"]
    try:
        raw_bytes = plist_path.read_bytes()
        data = plistlib.loads(raw_bytes)
    except Exception as exc:
        return None, [f"invalid plist {plist_path.name}: {type(exc).__name__}"]
    if not isinstance(data, dict):
        return None, [f"plist root is not dict: {plist_path.name}"]

    if str(data.get("Label") or "") != expected_label:
        errors.append(f"{plist_path.name}: Label: mismatch (expected {expected_label})")

    # Scan all string fields for unexpanded syntax ($HOME, ~, ${, __X__) — emit ONLY field location + error class
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

    # Validate literal absolute paths for standard plist path keys (never echo raw values)
    for pkey in ("WorkingDirectory", "StandardOutPath", "StandardErrorPath"):
        val = data.get(pkey)
        if val is not None and not _is_literal_abs_path(str(val)):
            errors.append(f"{plist_path.name}: {pkey}: not_literal_abs_path")

    prog_args = data.get("ProgramArguments")
    if not isinstance(prog_args, list) or not prog_args:
        errors.append(f"{plist_path.name}: ProgramArguments: missing")
        prog_args = []
    else:
        if not _is_literal_abs_path(str(prog_args[0])):
            errors.append(f"{plist_path.name}: ProgramArguments[0]: not_literal_abs_path")
        # Validate path-valued & critical CLI flags; reject duplicates explicitly
        for flag in ("--env-file", "--repo-env-file"):
            vals, ferrs = _extract_flag_values(prog_args, flag)
            for fe in ferrs:
                errors.append(f"{plist_path.name}: {fe}")
            for v in vals:
                if not _is_literal_abs_path(v):
                    errors.append(f"{plist_path.name}: {flag}: not_literal_abs_path")
        for flag in ("--host", "--port"):
            _, ferrs = _extract_flag_values(prog_args, flag)
            for fe in ferrs:
                errors.append(f"{plist_path.name}: {fe}")

    env_vars = data.get("EnvironmentVariables")
    if not isinstance(env_vars, dict):
        errors.append(f"{plist_path.name}: EnvironmentVariables: missing_or_not_dict")
        env_vars = {}

    for k, v in env_vars.items():
        sv = str(v)
        if k == "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH":
            parts = [part.strip() for part in re.split(r"[:,]", sv) if part.strip()]
            if not parts or any(not _is_literal_abs_path(part) for part in parts):
                errors.append(f"{plist_path.name}: EnvironmentVariables.{k}: not_literal_abs_path")
        elif k in _PLIST_PATH_ENV_KEYS and not _is_literal_abs_path(sv):
            errors.append(f"{plist_path.name}: EnvironmentVariables.{k}: not_literal_abs_path")
        elif k == "PYTHONPATH":
            parts = [part for part in sv.split(":") if part]
            if not parts or any(not _is_literal_abs_path(part) for part in parts):
                errors.append(f"{plist_path.name}: EnvironmentVariables.PYTHONPATH: not_literal_abs_path")

    repo_abs = cfg.repo_root.resolve()
    expected_py = str(repo_abs / ".venv" / "bin" / "python")
    expected_pythonpath = f"{repo_abs}/src:{repo_abs}"
    expected_repo_env = str(repo_abs / ".env")

    if require_venv_python:
        actual_py = str(prog_args[0]) if prog_args else ""
        if actual_py != expected_py:
            errors.append(f"{plist_path.name}: ProgramArguments[0]: expected {expected_py}")
        actual_pp = str(env_vars.get("PYTHONPATH") or "")
        if actual_pp != expected_pythonpath:
            errors.append(f"{plist_path.name}: EnvironmentVariables.PYTHONPATH: expected {expected_pythonpath}")
        actual_repo_env = str(env_vars.get("DALTON_REPO_ENV_FILE") or "")
        if actual_repo_env != expected_repo_env:
            errors.append(f"{plist_path.name}: EnvironmentVariables.DALTON_REPO_ENV_FILE: expected {expected_repo_env}")

    if require_bridge_env_match:
        raw_bridge_env = str(env_vars.get("DALTON_BRIDGE_ENV_FILE") or "").strip()
        expected_bridge_env = cfg.bridge_env_file.expanduser().resolve()
        if not raw_bridge_env:
            errors.append(f"{plist_path.name}: EnvironmentVariables.DALTON_BRIDGE_ENV_FILE: missing")
        elif _is_literal_abs_path(raw_bridge_env):
            resolved_bridge_env = Path(raw_bridge_env).expanduser().resolve()
            if resolved_bridge_env != expected_bridge_env:
                errors.append(
                    f"{plist_path.name}: EnvironmentVariables.DALTON_BRIDGE_ENV_FILE: expected {expected_bridge_env}"
                )

    if require_ownership_match:
        raw_own = str(env_vars.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip()
        expected_own = cfg.ownership_file
        if not raw_own:
            errors.append(f"{plist_path.name}: EnvironmentVariables.DALTON_GUIDEPOINT_OWNERSHIP_FILE: missing")
        elif _is_literal_abs_path(raw_own):
            resolved_own = Path(raw_own).expanduser().resolve()
            if resolved_own != expected_own:
                errors.append(
                    f"{plist_path.name}: EnvironmentVariables.DALTON_GUIDEPOINT_OWNERSHIP_FILE: expected {expected_own}"
                )

    if require_research_host_startup:
        expected_host_env = cfg.research_host_env_file.expanduser().resolve()
        expected_repo_env_resolved = cfg.repo_env_file.expanduser().resolve()
        env_vals, _ = _extract_flag_values(prog_args, "--env-file")
        repo_vals, _ = _extract_flag_values(prog_args, "--repo-env-file")
        arg_env_file = env_vals[-1] if env_vals else None
        arg_repo_env = repo_vals[-1] if repo_vals else None
        ev_host_env = str(env_vars.get("DALTON_RESEARCH_HOST_ENV_FILE") or "").strip()
        ev_repo_env = str(env_vars.get("DALTON_REPO_ENV_FILE") or "").strip()

        if not arg_env_file:
            errors.append(f"{plist_path.name}: ProgramArguments.--env-file: missing")
        elif _is_literal_abs_path(arg_env_file) and Path(arg_env_file).expanduser().resolve() != expected_host_env:
            errors.append(f"{plist_path.name}: ProgramArguments.--env-file: expected {expected_host_env}")
        if not ev_host_env:
            errors.append(f"{plist_path.name}: EnvironmentVariables.DALTON_RESEARCH_HOST_ENV_FILE: missing")
        elif _is_literal_abs_path(ev_host_env) and Path(ev_host_env).expanduser().resolve() != expected_host_env:
            errors.append(
                f"{plist_path.name}: EnvironmentVariables.DALTON_RESEARCH_HOST_ENV_FILE: expected {expected_host_env}"
            )
        if not arg_repo_env:
            errors.append(f"{plist_path.name}: ProgramArguments.--repo-env-file: missing")
        elif _is_literal_abs_path(arg_repo_env) and Path(arg_repo_env).expanduser().resolve() != expected_repo_env_resolved:
            errors.append(
                f"{plist_path.name}: ProgramArguments.--repo-env-file: expected {expected_repo_env_resolved}"
            )
        if ev_repo_env and _is_literal_abs_path(ev_repo_env) and Path(ev_repo_env).expanduser().resolve() != expected_repo_env_resolved:
            errors.append(
                f"{plist_path.name}: EnvironmentVariables.DALTON_REPO_ENV_FILE: expected {expected_repo_env_resolved}"
            )

        extra_mods = [
            m.strip()
            for m in str(env_vars.get("DALTON_RESEARCH_HOST_EXTRA_MODULES") or "").split(",")
            if m.strip()
        ]
        if not extra_mods or "host_connectors" not in extra_mods:
            errors.append(
                f"{plist_path.name}: EnvironmentVariables.DALTON_RESEARCH_HOST_EXTRA_MODULES: missing 'host_connectors'"
            )
        extra_pp = str(env_vars.get("DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH") or "").strip()
        if not extra_pp:
            errors.append(f"{plist_path.name}: EnvironmentVariables.DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH: missing")

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

    host_plist_path, hp_err = cfg.resolve_research_host_plist("stage-a")
    host_data: dict[str, Any] | None = None
    if hp_err or host_plist_path is None:
        all_errors.append(hp_err or f"missing plist: {cfg.plist_dir / 'com.dalton.research-host.plist'}")
    else:
        host_data, e3 = _inspect_plist_file(
            host_plist_path,
            expected_label="com.dalton.research-host",
            cfg=cfg,
            require_venv_python=True,
            require_bridge_env_match=False,
            require_ownership_match=True,
            require_research_host_startup=True,
        )
        all_errors.extend(e3)

    if proxy_data and host_data:
        p_ev = proxy_data.get("EnvironmentVariables")
        h_ev = host_data.get("EnvironmentVariables")
        if isinstance(p_ev, dict) and isinstance(h_ev, dict):
            p_own = str(p_ev.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "")
            h_own = str(h_ev.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "")
            if p_own and h_own and Path(p_own).expanduser().resolve() != Path(h_own).expanduser().resolve():
                all_errors.append(
                    f"ownership file mismatch between proxy ({p_own}) and research-host ({h_own})"
                )

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


def _load_plist_full_strict(plist_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not plist_path.is_file():
        return None, f"missing plist: {plist_path}"
    try:
        data = plistlib.loads(plist_path.read_bytes())
    except Exception as exc:
        return None, f"invalid plist {plist_path.name}: {exc}"
    if not isinstance(data, dict):
        return None, f"plist root is not dict: {plist_path.name}"
    return data, None


def _load_plist_env_vars_strict(plist_path: Path) -> tuple[dict[str, str] | None, str | None]:
    """Return `(env_dict, None)` on success or `(None, reason)` if plist is unreadable/corrupt."""
    data, err = _load_plist_full_strict(plist_path)
    if err is not None or data is None:
        return None, err
    if "EnvironmentVariables" not in data or data["EnvironmentVariables"] is None:
        return {}, None
    ev = data["EnvironmentVariables"]
    if not isinstance(ev, dict):
        return None, f"{plist_path.name}: EnvironmentVariables is not a dict"
    return {str(k): str(v) for k, v in ev.items()}, None


def _resolve_legacy_proxy_effective_env(cfg: PreflightConfig) -> tuple[dict[str, str], Path, list[str]]:
    """Resolve `com.dalton.guidepoint-mcp-proxy` env via shared `bridge_subprocess_env` parser/resolver."""
    default_gp_env = (
        cfg.home_dir
        / "Library"
        / "Application Support"
        / "Everflow"
        / "research"
        / "config"
        / "guidepoint_mcp.env"
    ).resolve()
    try:
        bse_mod = _import_bridge_subprocess_env(cfg.repo_root)
    except Exception as exc:
        return {"HOME": str(cfg.home_dir)}, default_gp_env, [f"cannot import bridge_subprocess_env: {exc}"]

    proxy_plist = cfg.plist_dir / "com.dalton.guidepoint-mcp-proxy.plist"
    bridge_vals = _parse_simple_env_file(cfg.bridge_env_file)
    try:
        resolved = bse_mod.resolve_legacy_proxy_effective_paths(
            repo_root=cfg.repo_root,
            home_dir=cfg.home_dir,
            existing_proxy_plist_path=proxy_plist,
            bridge_env_vals=bridge_vals,
        )
    except Exception as exc:
        return (
            {"HOME": str(cfg.home_dir)},
            default_gp_env,
            [f"legacy proxy env resolution failed ({type(exc).__name__}: {_sanitize_error_text(str(exc))})"],
        )

    eff_env = dict(resolved.get("effective_env") or {"HOME": str(cfg.home_dir)})
    gp_env_file = Path(str(resolved.get("guidepoint_env_file") or default_gp_env)).expanduser().resolve()
    return eff_env, gp_env_file, []


def _resolve_research_host_effective_env(
    cfg: PreflightConfig,
    *,
    mode: str = "stage-a",
) -> tuple[dict[str, str], Path, Path, list[str]]:
    """Resolve `com.dalton.research-host` env in real startup order (host.env < repo.env < plist)."""
    errors: list[str] = []
    host_plist, hp_err = cfg.resolve_research_host_plist(mode)
    plist_ev: dict[str, str] = {}
    host_env_path = cfg.research_host_env_file
    repo_env_path = cfg.repo_env_file

    if hp_err is not None or host_plist is None:
        errors.append(hp_err or "missing com.dalton.research-host.plist")
    else:
        pdata, perr = _load_plist_full_strict(host_plist)
        if perr or pdata is None:
            errors.append(perr or f"unreadable {host_plist.name}")
        else:
            ev = pdata.get("EnvironmentVariables")
            if isinstance(ev, dict):
                plist_ev = {str(k): str(v) for k, v in ev.items()}
            elif ev is not None:
                errors.append(f"{host_plist.name}: EnvironmentVariables is not a dict")
            prog_args = pdata.get("ProgramArguments")
            if isinstance(prog_args, list):
                env_vals, env_errs = _extract_flag_values(prog_args, "--env-file")
                errors.extend(f"{host_plist.name}: {e}" for e in env_errs)
                arg_env = env_vals[-1] if env_vals else None
                if arg_env and _is_literal_abs_path(arg_env):
                    host_env_path = Path(arg_env).expanduser().resolve()
                elif plist_ev.get("DALTON_RESEARCH_HOST_ENV_FILE") and _is_literal_abs_path(
                    plist_ev["DALTON_RESEARCH_HOST_ENV_FILE"]
                ):
                    host_env_path = Path(plist_ev["DALTON_RESEARCH_HOST_ENV_FILE"]).expanduser().resolve()

                repo_vals, repo_errs = _extract_flag_values(prog_args, "--repo-env-file")
                errors.extend(f"{host_plist.name}: {e}" for e in repo_errs)
                arg_repo = repo_vals[-1] if repo_vals else None
                if arg_repo and _is_literal_abs_path(arg_repo):
                    repo_env_path = Path(arg_repo).expanduser().resolve()
                elif plist_ev.get("DALTON_REPO_ENV_FILE") and _is_literal_abs_path(
                    plist_ev["DALTON_REPO_ENV_FILE"]
                ):
                    repo_env_path = Path(plist_ev["DALTON_REPO_ENV_FILE"]).expanduser().resolve()

    env_map: dict[str, str] = {"HOME": str(cfg.home_dir)}
    if host_env_path.is_file():
        env_map.update(_parse_simple_env_file(host_env_path))
    if repo_env_path.is_file():
        env_map.update(_parse_simple_env_file(repo_env_path))
    # launchd plist EnvironmentVariables populate os.environ and take precedence over .env/host.env
    env_map.update(plist_ev)
    if not env_map.get("DALTON_RESEARCH_HOME"):
        env_map["DALTON_RESEARCH_HOME"] = str(
            cfg.home_dir / "Library" / "Application Support" / "Everflow" / "research"
        )
    return env_map, host_env_path, repo_env_path, errors


def _check_guidepoint_tokens_json(cfg: PreflightConfig, *, mode: str = "stage-a") -> dict[str, str]:
    try:
        gl = _import_guidepoint_lock(cfg.repo_root)
    except Exception as exc:
        return _result("guidepoint_tokens_json", "fail", f"cannot import guidepoint_lock: {exc}")

    proxy_env, _gp_env, proxy_errs = _resolve_legacy_proxy_effective_env(cfg)
    host_env, _host_env_p, _repo_env_p, host_errs = _resolve_research_host_effective_env(cfg, mode=mode)
    if proxy_errs or host_errs:
        return _result("guidepoint_tokens_json", "fail", "; ".join(proxy_errs + host_errs))

    proxy_tokens = gl.resolve_guidepoint_tokens_file(environ=proxy_env)
    host_tokens = gl.resolve_guidepoint_tokens_file(environ=host_env)
    if proxy_tokens != host_tokens:
        return _result(
            "guidepoint_tokens_json",
            "fail",
            f"tokens path mismatch between legacy proxy ({proxy_tokens}) and research-host ({host_tokens})",
        )

    tokens_path = proxy_tokens
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


def _check_venv_python_imports(cfg: PreflightConfig, *, mode: str = "stage-a") -> dict[str, str]:
    venv_py = cfg.venv_python
    if not (venv_py.is_file() and os.access(venv_py, os.X_OK)):
        return _result("venv_python_imports", "fail", f"missing executable venv python: {venv_py}")

    # Strictly use ONLY paths declared in the plists (no synthetic repo fallbacks and isolated cwd)
    host_plist_path, hp_err = cfg.resolve_research_host_plist(mode)
    if hp_err or host_plist_path is None:
        return _result("venv_python_imports", "fail", hp_err or "missing com.dalton.research-host.plist")
    host_ev, host_err = _load_plist_env_vars_strict(host_plist_path)
    if host_err or host_ev is None:
        return _result("venv_python_imports", "fail", host_err or f"unreadable {host_plist_path}")

    rh_pp_raw = str(host_ev.get("PYTHONPATH") or "").strip()
    rh_extra_pp_raw = str(host_ev.get("DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH") or "").strip()
    rh_extra_mods_raw = str(host_ev.get("DALTON_RESEARCH_HOST_EXTRA_MODULES") or "").strip()
    missing_keys: list[str] = []
    if not rh_pp_raw:
        missing_keys.append("PYTHONPATH")
    if not rh_extra_pp_raw:
        missing_keys.append("DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH")
    if not rh_extra_mods_raw:
        missing_keys.append("DALTON_RESEARCH_HOST_EXTRA_MODULES")
    if missing_keys:
        return _result(
            "venv_python_imports",
            "fail",
            f"{host_plist_path.name} missing required import env key(s): {', '.join(missing_keys)}",
        )

    extra_mods = [m.strip() for m in rh_extra_mods_raw.split(",") if m.strip()]
    for mod in extra_mods:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", mod):
            return _result(
                "venv_python_imports",
                "fail",
                f"invalid module name in DALTON_RESEARCH_HOST_EXTRA_MODULES: {mod!r}",
            )

    extra_pp_entries = [e.strip() for e in re.split(r"[:,]", rh_extra_pp_raw) if e.strip()]
    host_modules = list(
        dict.fromkeys(
            [
                "guidepoint_lock",
                "fastapi",
                "uvicorn",
                "httpx",
                "src.dalton.capabilities",
                *extra_mods,
            ]
        )
    )
    # Reproduce real startup sys.path order:
    # 1) PYTHONPATH from plist (with cwd stripped via PYTHONSAFEPATH=1 + explicit sys.path filter)
    # 2) `src.dalton.research_host.__main__` inserts DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH entries
    #    at front of sys.path before importing extra modules.
    host_probe_code = (
        "import importlib, os, sys\n"
        "cwd = os.path.realpath(os.getcwd())\n"
        "sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != cwd]\n"
        f"extra_paths = {extra_pp_entries!r}\n"
        "importlib.import_module('src.dalton.capabilities')\n"
        "for _p in extra_paths:\n"
        "    if _p and _p not in sys.path:\n"
        "        sys.path.insert(0, _p)\n"
        f"for _m in {host_modules!r}:\n"
        "    importlib.import_module(_m)\n"
    )
    sub_env_host = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cfg.home_dir),
        "PYTHONPATH": rh_pp_raw,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONSAFEPATH": "1",
    }

    bridge_plist_path = cfg.plist_dir / "com.dalton.researchdata-bridge.plist"
    bridge_ev, bridge_err = _load_plist_env_vars_strict(bridge_plist_path)
    if bridge_err or bridge_ev is None:
        return _result("venv_python_imports", "fail", bridge_err or f"unreadable {bridge_plist_path}")
    bridge_pp_raw = str(bridge_ev.get("PYTHONPATH") or "").strip()
    if not bridge_pp_raw:
        return _result(
            "venv_python_imports",
            "fail",
            f"{bridge_plist_path.name} missing required PYTHONPATH",
        )
    bridge_probe_code = (
        "import importlib, os, sys; "
        "cwd = os.path.realpath(os.getcwd()); "
        "sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != cwd]; "
        "importlib.import_module('src.dalton.capabilities')"
    )
    sub_env_bridge = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cfg.home_dir),
        "PYTHONPATH": bridge_pp_raw,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONSAFEPATH": "1",
    }

    with tempfile.TemporaryDirectory(prefix="dalton-import-probe-") as neutral_cwd:
        rc, _out, err = _run_cmd(
            [str(venv_py), "-B", "-c", host_probe_code],
            env=sub_env_host,
            cwd=neutral_cwd,
            timeout_s=20.0,
        )
        if rc != 0:
            last_line = err.strip().splitlines()[-1] if err.strip() else "unknown"
            return _result(
                "venv_python_imports",
                "fail",
                f"research-host import probe failed (rc={rc}): {_sanitize_error_text(last_line)}",
            )

        rc_b, _out_b, err_b = _run_cmd(
            [str(venv_py), "-B", "-c", bridge_probe_code],
            env=sub_env_bridge,
            cwd=neutral_cwd,
            timeout_s=20.0,
        )
        if rc_b != 0:
            last_line = err_b.strip().splitlines()[-1] if err_b.strip() else "unknown"
            return _result(
                "venv_python_imports",
                "fail",
                f"bridge import probe failed (rc={rc_b}): {_sanitize_error_text(last_line)}",
            )

    return _result(
        "venv_python_imports",
        "ok",
        f"plist-scoped imports ok ({', '.join(host_modules)})",
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
    _, gp_env_file, _ = _resolve_legacy_proxy_effective_env(cfg)
    return gp_env_file


def _check_live_services_health_and_drain(cfg: PreflightConfig) -> dict[str, str]:
    errors: list[str] = []
    bridge_vals = _parse_simple_env_file(cfg.bridge_env_file)
    bridge_admin_tok = str(bridge_vals.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()

    gp_eff_env, _gp_env_file, gp_env_errs = _resolve_legacy_proxy_effective_env(cfg)
    if gp_env_errs:
        errors.extend(gp_env_errs)
    gp_admin_tok = str(gp_eff_env.get("GUIDEPOINT_PROXY_ADMIN_TOKEN") or "").strip()

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
    _, bridge_errors = _inspect_plist_file(
        cfg.plist_dir / "com.dalton.researchdata-bridge.plist",
        expected_label="com.dalton.researchdata-bridge",
        cfg=cfg,
        require_venv_python=True,
        require_bridge_env_match=True,
        require_ownership_match=False,
    )
    errors.extend(bridge_errors)

    _, proxy_errors = _inspect_plist_file(
        cfg.plist_dir / "com.dalton.guidepoint-mcp-proxy.plist",
        expected_label="com.dalton.guidepoint-mcp-proxy",
        cfg=cfg,
        require_venv_python=False,
        require_bridge_env_match=False,
        require_ownership_match=True,
    )
    errors.extend(proxy_errors)

    journal, j_err = _read_journal_strict(cfg.ownership_file)
    if j_err is not None:
        errors.append(j_err)
    target_owner = str((journal or {}).get("target_owner") or "legacy_proxy_8943").strip()

    # Stage A terminal invariant: com.dalton.research-host.plist must be archived out of LaunchAgents
    if (
        target_owner != "research_host_8951"
        and cfg.research_host_plist is None
        and (cfg.plist_dir / "com.dalton.research-host.plist").exists()
    ):
        errors.append(
            "com.dalton.research-host.plist is still present in LaunchAgents; "
            "Stage A must archive it out of the auto-load directory"
        )

    host_plist_path, hp_err = cfg.resolve_research_host_plist("switch")
    if hp_err or host_plist_path is None:
        errors.append(hp_err or "missing archived com.dalton.research-host.plist")
    else:
        _, host_errors = _inspect_plist_file(
            host_plist_path,
            expected_label="com.dalton.research-host",
            cfg=cfg,
            require_venv_python=True,
            require_bridge_env_match=False,
            require_ownership_match=True,
            require_research_host_startup=True,
        )
        errors.extend(host_errors)

    if not cfg.skip_budgets:
        budget_errors = validate_timing_budgets_env(cfg.budgets_file)
        errors.extend(budget_errors)

    if errors:
        return _result("bridge_plist_and_budgets", "fail", "; ".join(errors))
    budget_note = (
        "budgets check skipped for manual downtime cutover"
        if cfg.skip_budgets
        else "and timing-budgets.env valid"
    )
    return _result(
        "bridge_plist_and_budgets",
        "ok",
        f"installed bridge/proxy plists, archived research-host plist ({host_plist_path}), {budget_note}",
    )


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


def _check_two_sided_guidepoint_paths(cfg: PreflightConfig, *, mode: str = "switch") -> dict[str, str]:
    try:
        gl = _import_guidepoint_lock(cfg.repo_root)
    except Exception as exc:
        return _result("two_sided_guidepoint_paths", "fail", f"cannot import guidepoint_lock: {exc}")

    proxy_env, _gp_env, proxy_errs = _resolve_legacy_proxy_effective_env(cfg)
    host_env, _host_env_p, _repo_env_p, host_errs = _resolve_research_host_effective_env(cfg, mode=mode)
    if proxy_errs or host_errs:
        return _result("two_sided_guidepoint_paths", "fail", "; ".join(proxy_errs + host_errs))

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


_COMPOSE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def resolve_compose_project_name(
    repo_root: Path,
    *,
    repo_env_file: Path | None = None,
    docker_bin: str = "docker",
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve Docker Compose project name matching `deploy_main.sh`'s `_edge_compose_project_name`."""
    env = os.environ if environ is None else environ
    env_file = repo_env_file if repo_env_file is not None else (repo_root / ".env")

    rc, out, _ = _run_cmd(
        [
            docker_bin,
            "inspect",
            "-f",
            '{{index .Config.Labels "com.docker.compose.project"}}',
            "dalton-langalpha-edge-1",
        ],
        timeout_s=5.0,
    )
    if rc == 0:
        proj = out.strip().rstrip("\r")
        if proj and proj != "<no value>" and _COMPOSE_PROJECT_RE.fullmatch(proj):
            return proj

    raw_env_proj = str(env.get("COMPOSE_PROJECT_NAME") or "").strip()
    if raw_env_proj and _COMPOSE_PROJECT_RE.fullmatch(raw_env_proj):
        return raw_env_proj

    if env_file.is_file():
        repo_vals = _parse_simple_env_file(env_file)
        file_proj = str(repo_vals.get("COMPOSE_PROJECT_NAME") or "").strip()
        if file_proj and _COMPOSE_PROJECT_RE.fullmatch(file_proj):
            return file_proj

    return ""


def resolve_compose_file_chain(
    repo_root: Path,
    *,
    repo_env_file: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[list[Path], str | None]:
    """Shared Compose file-chain resolver matching `deploy_main.sh`'s `compose_files_list` / `_edge_compose_files_list`."""
    env = os.environ if environ is None else environ
    env_file = repo_env_file if repo_env_file is not None else (repo_root / ".env")
    repo_vals = _parse_simple_env_file(env_file)

    sep = (
        str(env.get("COMPOSE_PATH_SEPARATOR") or "").strip()
        or str(repo_vals.get("COMPOSE_PATH_SEPARATOR") or "").strip()
        or ":"
    )
    cf = (
        str(env.get("COMPOSE_FILE") or "").strip()
        or str(repo_vals.get("COMPOSE_FILE") or "").strip()
    )

    def _resolve_one(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else (repo_root / p)

    files: list[Path] = []
    if cf:
        for entry in cf.split(sep):
            item = entry.strip()
            if not item:
                continue
            resolved = _resolve_one(item)
            if not resolved.is_file():
                return [], f"explicit COMPOSE_FILE entry not found: {resolved}"
            files.append(resolved)
    else:
        for base_name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
            base_path = repo_root / base_name
            if base_path.is_file():
                files.append(base_path)
                for ovr_name in (
                    "compose.override.yaml",
                    "compose.override.yml",
                    "docker-compose.override.yaml",
                    "docker-compose.override.yml",
                ):
                    ovr_path = repo_root / ovr_name
                    if ovr_path.is_file():
                        files.append(ovr_path)
                        break
                break

    has_dalton = any(p.name == "docker-compose.dalton.yml" for p in files)
    dalton_compose = repo_root / "docker-compose.dalton.yml"
    if not has_dalton and dalton_compose.is_file():
        files.append(dalton_compose)

    return files, None


def resolve_compose_args(
    repo_root: Path,
    *,
    repo_env_file: Path | None = None,
    docker_bin: str = "docker",
    environ: Mapping[str, str] | None = None,
) -> tuple[list[str], list[Path], str | None]:
    """Build shared `docker compose` CLI args (`-p`, `--project-directory`, `--env-file`, `-f ...`)."""
    env_file = repo_env_file if repo_env_file is not None else (repo_root / ".env")
    compose_files, chain_err = resolve_compose_file_chain(
        repo_root,
        repo_env_file=env_file,
        environ=environ,
    )
    if chain_err is not None:
        return [], [], chain_err
    if not compose_files:
        return [], [], None

    args: list[str] = []
    proj = resolve_compose_project_name(
        repo_root,
        repo_env_file=env_file,
        docker_bin=docker_bin,
        environ=environ,
    )
    if proj:
        args.extend(["-p", proj])
    args.extend(["--project-directory", str(repo_root)])
    if env_file.is_file():
        args.extend(["--env-file", str(env_file)])
    for cf in compose_files:
        args.extend(["-f", str(cf)])
    return args, compose_files, None


def _resolve_compose_file_chain(cfg: PreflightConfig) -> tuple[list[Path], str | None]:
    return resolve_compose_file_chain(cfg.repo_root, repo_env_file=cfg.repo_env_file)


def _read_compose_backend_env(cfg: PreflightConfig) -> tuple[dict[str, str] | None, str, str | None]:
    """Read-only `docker compose config --format json` for Everflow backend service.

    Returns `(env_dict, source_tag, error_or_none)`:
    - Only when NO compose files exist at all (and COMPOSE_FILE is unset) is `(None, "compose_files_absent", None)` returned.
    - When compose files exist (or COMPOSE_FILE is set), any command failure, invalid JSON,
      or missing `services.backend.environment` returns an error string (fail closed).
    """
    compose_args, compose_files, chain_err = resolve_compose_args(
        cfg.repo_root,
        repo_env_file=cfg.repo_env_file,
        docker_bin=cfg.docker_bin,
    )
    if chain_err is not None:
        return None, "compose_file_missing", chain_err
    if not compose_files:
        return None, "compose_files_absent", None

    cmd: list[str] = [cfg.docker_bin, "compose", *compose_args, "config", "--format", "json"]

    sub_env = dict(os.environ)
    sub_env["HOME"] = str(cfg.home_dir)
    rc, out, err = _run_cmd(cmd, env=sub_env, timeout_s=15.0)
    if rc != 0:
        return (
            None,
            "compose_config_failed",
            f"docker compose config failed (rc={rc}: {_sanitize_error_text(err.strip()[:240])})",
        )
    try:
        parsed = json.loads(out)
    except Exception as exc:
        return None, "compose_json_error", f"docker compose config emitted invalid JSON: {type(exc).__name__}"
    if not isinstance(parsed, dict):
        return None, "compose_json_error", "docker compose config JSON root is not an object"
    services = parsed.get("services")
    if not isinstance(services, dict):
        return None, "compose_missing_services", "docker compose config missing services object"
    svc = services.get("backend")
    if not isinstance(svc, dict):
        return None, "compose_missing_backend", "docker compose config missing services.backend"
    if "environment" not in svc or svc["environment"] is None:
        return (
            None,
            "compose_missing_backend_env",
            "docker compose config missing services.backend.environment",
        )
    raw_env = svc["environment"]
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
    else:
        return (
            None,
            "compose_invalid_backend_env",
            f"services.backend.environment has invalid type: {type(raw_env).__name__}",
        )
    return env_dict, "compose_config", None


def _check_three_way_capability_parity(cfg: PreflightConfig) -> dict[str, str]:
    try:
        caps_mod, prof_mod = _import_capabilities(cfg.repo_root)
    except Exception as exc:
        return _result("three_way_capability_parity", "fail", f"cannot import src.dalton.capabilities: {exc}")

    repo_env = cfg.repo_env_file
    compose_env, compose_src, compose_err = _read_compose_backend_env(cfg)
    if compose_err is not None:
        return _result("three_way_capability_parity", "fail", compose_err)

    backend_merged = caps_mod.load_deployment_capability_env(
        repo_env_file=repo_env,
        environ=compose_env if compose_env is not None else {},
    )

    host_plist_path, hp_err = cfg.resolve_research_host_plist("switch")
    bridge_plist_path = cfg.plist_dir / "com.dalton.researchdata-bridge.plist"
    if hp_err or host_plist_path is None:
        return _result("three_way_capability_parity", "fail", hp_err or "missing archived com.dalton.research-host.plist")
    host_plist_ev, host_plist_err = _load_plist_env_vars_strict(host_plist_path)
    bridge_plist_ev, bridge_plist_err = _load_plist_env_vars_strict(bridge_plist_path)
    if host_plist_err or bridge_plist_err:
        errs = [e for e in (host_plist_err, bridge_plist_err) if e]
        return _result("three_way_capability_parity", "fail", "; ".join(errs))

    # Honor actual research-host startup --env-file / --repo-env-file if declared in archived plist
    _, eff_host_env_path, eff_host_repo_env_path, rh_errs = _resolve_research_host_effective_env(
        cfg, mode="switch"
    )
    if rh_errs:
        return _result("three_way_capability_parity", "fail", "; ".join(rh_errs))

    eff_bridge_repo_env_path = (
        Path(bridge_plist_ev["DALTON_REPO_ENV_FILE"]).expanduser().resolve()
        if bridge_plist_ev and _is_literal_abs_path(str(bridge_plist_ev.get("DALTON_REPO_ENV_FILE") or ""))
        else repo_env
    )
    eff_bridge_env_path = (
        Path(bridge_plist_ev["DALTON_BRIDGE_ENV_FILE"]).expanduser().resolve()
        if bridge_plist_ev and _is_literal_abs_path(str(bridge_plist_ev.get("DALTON_BRIDGE_ENV_FILE") or ""))
        else cfg.bridge_env_file
    )

    host_merged = caps_mod.load_deployment_capability_env(
        repo_env_file=eff_host_repo_env_path,
        service_env_file=eff_host_env_path,
        environ=host_plist_ev or {},
    )
    bridge_merged = caps_mod.load_deployment_capability_env(
        repo_env_file=eff_bridge_repo_env_path,
        service_env_file=eff_bridge_env_path,
        environ=bridge_plist_ev or {},
    )

    def _snapshot(label: str, env_map: Mapping[str, str]) -> tuple[dict[str, Any] | None, str | None]:
        try:
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
            }, None
        except Exception as exc:
            return None, f"{label} capability/profile resolution error ({type(exc).__name__}: {_sanitize_error_text(str(exc))})"

    s_backend, err_b = _snapshot("backend", backend_merged)
    s_host, err_h = _snapshot("research-host", host_merged)
    s_bridge, err_br = _snapshot("bridge", bridge_merged)

    errors: list[str] = [e for e in (err_b, err_h, err_br) if e]
    if s_backend is not None and s_host is not None and s_backend != s_host:
        errors.append(f"backend vs research-host mismatch: {s_backend} != {s_host}")
    if s_backend is not None and s_bridge is not None and s_backend != s_bridge:
        errors.append(f"backend vs bridge mismatch: {s_backend} != {s_bridge}")

    expected_bridge_env = cfg.bridge_env_file.expanduser().resolve()
    for svc_label, svc_file in (
        ("research-host.env", eff_host_env_path),
        ("researchdata-bridge.env", eff_bridge_env_path),
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
    assert s_backend is not None
    return _result(
        "three_way_capability_parity",
        "ok",
        f"profile={s_backend['profile']} platform={s_backend['host_platform']} "
        f"batch3={s_backend['batch3_enabled']} ({compose_src})",
    )


_PS_ENV_ASSIGN_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=")


def _extract_proc_env_vars(ps_output: str, target_keys: set[str]) -> dict[str, str]:
    """Extract target KEY=VALUE assignments from `ps -wwwE -p <pid>` preserving spaces in values."""
    found: dict[str, str] = {}
    for raw_line in ps_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("PID "):
            continue
        matches = list(_PS_ENV_ASSIGN_RE.finditer(line))
        for idx, m in enumerate(matches):
            key = m.group(1)
            if key not in target_keys:
                continue
            val_start = m.end()
            val_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            found[key] = line[val_start:val_end].rstrip()
    return found


def _get_service_pid_readonly(cfg: PreflightConfig, label: str) -> tuple[int | None, str, str | None]:
    """Return `(pid, state, detail)` where state is 'running'|'not_loaded'|'no_pid'|'error'."""
    rc, out, err = _run_cmd([cfg.launchctl_bin, "print", f"gui/{cfg.effective_uid}/{label}"])
    if rc == 0 and out:
        m = re.search(r"^\s*pid\s*=\s*([0-9]+)\b", out, flags=re.MULTILINE)
        if m:
            pid_val = int(m.group(1))
            if pid_val > 0:
                return pid_val, "running", None
    rc2, out2, err2 = _run_cmd([cfg.launchctl_bin, "list"])
    if rc2 != 0:
        detail = err2.strip() or err.strip() or f"rc={rc2}"
        return None, "error", f"launchctl failed for {label} ({detail})"
    pids = _parse_launchctl_list_pids(out2)
    if label not in pids:
        return None, "not_loaded", f"{label} is not loaded in launchctl"
    pid2 = pids[label]
    if pid2 is None or pid2 <= 0:
        return None, "no_pid", f"{label} loaded without a running PID"
    return pid2, "running", None


def _expected_stage_guidepoint_urls(
    cfg: PreflightConfig,
    *,
    mode: str = "switch",
) -> tuple[set[str], str, str | None]:
    """Determine expected `DALTON_GUIDEPOINT_MCP_URL` values SOLELY from explicit stage + persisted ownership."""
    journal, j_err = _read_journal_strict(cfg.ownership_file)
    if j_err is not None:
        return set(), "", j_err
    target_owner = str((journal or {}).get("target_owner") or "legacy_proxy_8943").strip()
    if target_owner not in ("legacy_proxy_8943", "research_host_8951"):
        return set(), target_owner, f"invalid target_owner in handoff-journal.json: {target_owner!r}"
    if mode == "stage-a":
        if target_owner != "legacy_proxy_8943":
            return set(), target_owner, f"stage-a expects target_owner='legacy_proxy_8943', got {target_owner!r}"
        return {
            f"{cfg.proxy_url.rstrip('/')}/mcp",
            "http://127.0.0.1:8943/mcp",
        }, target_owner, None
    if target_owner == "research_host_8951":
        return {
            f"{cfg.research_host_url.rstrip('/')}/mcp/guidepoint",
            "http://127.0.0.1:8951/mcp/guidepoint",
        }, target_owner, None
    return {
        f"{cfg.proxy_url.rstrip('/')}/mcp",
        "http://127.0.0.1:8943/mcp",
    }, target_owner, None


def _check_env_override_scan(cfg: PreflightConfig, *, mode: str = "switch") -> dict[str, str]:
    try:
        caps_mod, _ = _import_capabilities(cfg.repo_root)
    except Exception as exc:
        return _result("env_override_scan", "fail", f"cannot import src.dalton.capabilities: {exc}")

    repo_vals = _parse_simple_env_file(cfg.repo_env_file)
    bridge_vals = _parse_simple_env_file(cfg.bridge_env_file)
    bridge_env_gp_url = str(bridge_vals.get("DALTON_GUIDEPOINT_MCP_URL") or "").strip()
    expected_gp_urls, target_owner, stage_err = _expected_stage_guidepoint_urls(cfg, mode=mode)
    if stage_err is not None:
        return _result("env_override_scan", "fail", stage_err)

    normalized_expected_urls = {u.rstrip("/") for u in expected_gp_urls}
    direct_errors: list[str] = []

    # Independently validate researchdata-bridge.env effective DALTON_GUIDEPOINT_MCP_URL against stage + owner
    if bridge_env_gp_url:
        if bridge_env_gp_url.rstrip("/") not in normalized_expected_urls:
            direct_errors.append(
                f"researchdata-bridge.env DALTON_GUIDEPOINT_MCP_URL={_sanitize_diag_value('DALTON_GUIDEPOINT_MCP_URL', bridge_env_gp_url)} "
                f"(does not match expected target for stage={mode} owner={target_owner})"
            )
    elif target_owner == "research_host_8951":
        direct_errors.append(
            f"researchdata-bridge.env missing DALTON_GUIDEPOINT_MCP_URL for owner={target_owner} "
            "(default falls back to legacy :8943)"
        )

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
    lc_getenv_failed_keys: list[str] = []
    for key in sorted(scan_keys):
        rc, out, _ = _run_cmd([cfg.launchctl_bin, "getenv", key])
        if rc != 0:
            lc_getenv_failed_keys.append(key)
            continue
        val = out.strip()
        if val:
            lc_env[key] = val
    if lc_getenv_failed_keys:
        warnings.append(
            f"launchctl getenv failed for {len(lc_getenv_failed_keys)} key(s): "
            f"{', '.join(lc_getenv_failed_keys[:3])}"
        )
    if lc_env:
        extra_sources["launchctl_getenv"] = lc_env

    # 2. All com.dalton.*.plist in --plist-dir + archived com.dalton.research-host.plist
    seen_plists: set[Path] = set()
    if cfg.plist_dir.is_dir():
        for pp in sorted(cfg.plist_dir.glob("com.dalton.*.plist")):
            seen_plists.add(pp.resolve())
            ev, perr = _load_plist_env_vars_strict(pp)
            if perr is not None:
                warnings.append(f"could not read plist {pp.name}: {perr}")
                continue
            if ev:
                extra_sources[f"plist:{pp.name}"] = ev

    archived_host_plist, _ = cfg.resolve_research_host_plist(mode)
    if archived_host_plist is not None and archived_host_plist.resolve() not in seen_plists:
        ev, perr = _load_plist_env_vars_strict(archived_host_plist)
        if perr is not None:
            warnings.append(f"could not read archived plist {archived_host_plist.name}: {perr}")
        elif ev:
            extra_sources[f"archived_plist:{archived_host_plist.name}"] = ev

    # 3. Running process environments for bridge and research-host
    for label in ("com.dalton.researchdata-bridge", "com.dalton.research-host"):
        pid, state, detail = _get_service_pid_readonly(cfg, label)
        if state in ("error", "no_pid"):
            warnings.append(f"could not read process env for {label}: {detail}")
            continue
        if state == "not_loaded":
            # Bridge is expected running during --switch preflight; research-host may be standby/not loaded yet.
            if label == "com.dalton.researchdata-bridge":
                warnings.append(f"could not read process env for {label}: {detail}")
            continue
        assert pid is not None
        rc, out, err = _run_cmd([cfg.ps_bin, "-wwwE", "-p", str(pid)])
        if rc != 0 or not out.strip():
            warnings.append(f"could not read process env for {label} (pid={pid}): {err.strip() or f'rc={rc}'}")
            continue
        proc_vars = _extract_proc_env_vars(out, scan_keys)
        if label == "com.dalton.researchdata-bridge" and "DALTON_GUIDEPOINT_MCP_URL" in proc_vars:
            proc_gp_url = proc_vars["DALTON_GUIDEPOINT_MCP_URL"].strip()
            # Allow bridge process DALTON_GUIDEPOINT_MCP_URL ONLY when it matches the expected target
            # for the explicit stage/owner AND (bridge.env omits it or matches it).
            if proc_gp_url.rstrip("/") in normalized_expected_urls and (
                not bridge_env_gp_url or proc_gp_url.rstrip("/") == bridge_env_gp_url.rstrip("/")
            ):
                proc_vars = {k: v for k, v in proc_vars.items() if k != "DALTON_GUIDEPOINT_MCP_URL"}
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
    conflict_details = [
        f"{c.get('source')} {c.get('key')}={_sanitize_diag_value(str(c.get('key') or ''), c.get('value'))} ({c.get('reason')})"
        for c in conflicts
    ]
    all_fail_details = direct_errors + conflict_details
    if all_fail_details:
        return _result("env_override_scan", "fail", "; ".join(all_fail_details))

    if warnings:
        return _result("env_override_scan", "warn", "; ".join(warnings))
    return _result("env_override_scan", "ok", "no conflicting env overrides found")


def run_preflight(mode: str, cfg: PreflightConfig, *, report_json: bool = False) -> tuple[int, list[dict[str, str]]]:
    """Execute preflight checks for `stage-a` or `switch` mode."""
    if mode == "stage-a":
        checks: list[tuple[str, Callable[[PreflightConfig], dict[str, str]]]] = [
            ("backend_admission_gate", _check_backend_admission_gate),
            ("guidepoint_proxy_pid_match", _check_guidepoint_proxy_pid_match),
            ("stage_a_plists", _check_stage_a_plists),
            ("research_host_env", lambda c: _check_research_host_env(c, require_distinct_tokens=False)),
            ("guidepoint_tokens_json", lambda c: _check_guidepoint_tokens_json(c, mode="stage-a")),
            ("venv_python_imports", lambda c: _check_venv_python_imports(c, mode="stage-a")),
            ("web_releases_current", _check_web_releases_current),
        ]
    elif mode == "switch":
        checks = [
            ("live_services_health_and_drain", _check_live_services_health_and_drain),
            ("bridge_plist_and_budgets", _check_bridge_plist_and_budgets),
            ("research_host_env", lambda c: _check_research_host_env(c, require_distinct_tokens=True)),
            ("extra_plugins", _check_extra_plugins),
            ("venv_python_imports", lambda c: _check_venv_python_imports(c, mode="switch")),
            ("web_releases_current", _check_web_releases_current),
            ("two_sided_guidepoint_paths", lambda c: _check_two_sided_guidepoint_paths(c, mode="switch")),
            ("three_way_capability_parity", _check_three_way_capability_parity),
            ("env_override_scan", lambda c: _check_env_override_scan(c, mode="switch")),
        ]
    else:
        raise ValueError(f"Unsupported preflight mode: {mode!r}")

    results: list[dict[str, str]] = []
    failed = False
    for check_name, fn in checks:
        try:
            res = fn(cfg)
        except Exception as exc:
            res = _result(
                check_name,
                "fail",
                f"check failed ({type(exc).__name__}: {_sanitize_error_text(str(exc))})",
            )
        results.append(res)
        if res["status"] == "fail":
            failed = True
            if not report_json:
                return 1, results
    return (1 if failed else 0), results


# ---------------------------------------------------------------------------
# Shared helper subcommands (lock, journal, recovery-manifest, approve-budgets, guard)
# ---------------------------------------------------------------------------


def _verify_inherited_ops_lock(lock_file: Path, lock_fd: int = 8) -> tuple[bool, str]:
    """Verify via kernel `flock` that `lock_fd` itself holds an exclusive lock on `lock_file`.

    Three-step unforgeable check:
      1. `fstat(lock_fd)` must match `stat(lock_file)` (`st_dev`, `st_ino`).
      2. A fresh independent open on `lock_file` must get `BlockingIOError` on `LOCK_EX|LOCK_NB`
         (proving an exclusive flock is actually held on that inode).
      3. `flock(lock_fd, LOCK_EX|LOCK_NB)` must succeed (idempotent on the same open file
         description that holds the lock, fails with `BlockingIOError` if another process holds it).
    """
    try:
        path_st = os.stat(str(lock_file))
    except OSError as exc:
        return False, f"ops lock file missing or inaccessible ({lock_file}: {exc})"

    try:
        fd_st = os.fstat(int(lock_fd))
    except OSError:
        return False, f"ops lock FD {lock_fd} is not open"

    if (fd_st.st_dev, fd_st.st_ino) != (path_st.st_dev, path_st.st_ino):
        return False, f"ops lock FD {lock_fd} does not refer to {lock_file}"

    probe_fd: int | None = None
    try:
        probe_fd = os.open(str(lock_file), os.O_RDONLY)
        try:
            fcntl.flock(probe_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            # Nobody held the exclusive flock — immediately unlock and reject!
            try:
                fcntl.flock(probe_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            return False, f"no exclusive flock is held on {lock_file}"
    except OSError as exc:
        return False, f"failed probing {lock_file}: {exc}"
    finally:
        if probe_fd is not None:
            try:
                os.close(probe_fd)
            except OSError:
                pass

    try:
        fcntl.flock(int(lock_fd), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False, f"{lock_file} is held by another process, not FD {lock_fd}"
    except OSError as exc:
        return False, f"flock verification on FD {lock_fd} failed: {exc}"

    return True, "ok"


@contextmanager
def _hold_or_verify_ops_lock(
    lock_file: Path,
    *,
    lock_fd: int | None = None,
    caller_tag: str = "preflight",
) -> Iterator[None]:
    """Reuse a verified inherited FD lock or acquire a non-blocking exclusive flock on `lock_file`."""
    if lock_fd is not None:
        ok, _ = _verify_inherited_ops_lock(lock_file, lock_fd=lock_fd)
        if ok:
            yield
            return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock_file.parent, 0o700)
    except OSError:
        pass

    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            holder = ""
            try:
                holder = lock_file.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            detail = f" ({holder})" if holder else ""
            raise RuntimeError(
                f"another research-host ops/deploy process holds {lock_file}{detail}"
            ) from None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        os.write(fd, f"pid={os.getpid()} started={ts} caller={caller_tag}\n".encode("utf-8"))
        os.fsync(fd)
        yield
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def _read_journal_strict(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Strictly read `handoff-journal.json`.

    Returns:
      - `(None, None)` when the file does not exist.
      - `(dict, None)` when the file exists and parses as a JSON object.
      - `(None, error)` when the file exists but is unreadable, not a regular file,
        invalid JSON, or not a JSON object (fail closed).
    """
    if not path.exists() and not path.is_symlink():
        return None, None
    if not path.is_file():
        return None, f"handoff-journal.json is not a regular file: {path}"
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"failed reading handoff-journal.json ({path}): {exc}"
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        return None, f"corrupt JSON in handoff-journal.json ({path}): {exc}"
    if not isinstance(parsed, dict):
        return None, f"handoff-journal.json root is not a JSON object ({path})"
    return parsed, None


def _canonical_ops_paths(repo_root: Path) -> tuple[Path, Path, Path, Path]:
    """Derive canonical production lock, journal, admission, and budgets paths strictly from `repo_root`."""
    root = repo_root.expanduser().resolve()
    cutover_dir = root / "var" / "dalton" / "cutover-snapshots" / "research-host"
    return (
        cutover_dir / ".ops.lock",
        cutover_dir / "handoff-journal.json",
        root / "var" / "dalton" / "maintenance-admission.json",
        cutover_dir / "timing-budgets.env",
    )


def _check_admission_and_incomplete_journal(
    adm_path: Path,
    j_path: Path,
    *,
    existing_journal: dict[str, Any] | None = None,
    journal_already_read: bool = False,
) -> list[str]:
    """Return missing guard conditions for full admission pause + incomplete handoff-journal."""
    missing_reasons: list[str] = []

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

    if journal_already_read:
        j_data, j_err = existing_journal, None
    else:
        j_data, j_err = _read_journal_strict(j_path)
    if j_err is not None:
        missing_reasons.append(j_err)
    elif j_data is None:
        missing_reasons.append(f"handoff-journal.json missing ({j_path})")
    elif j_data.get("completed") is not False:
        missing_reasons.append(
            f"handoff-journal.json completed != false (got {j_data.get('completed')!r})"
        )

    return missing_reasons


def cmd_lock_verify(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    lock_file, _, _, _ = _canonical_ops_paths(repo_root)
    ok, reason = _verify_inherited_ops_lock(lock_file, lock_fd=int(args.lock_fd))
    if not ok:
        print(f"ERROR: {reason}", file=sys.stderr)
        return 1
    return 0


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
    lock_file, ownership_path, adm_path, _ = _canonical_ops_paths(repo_root)
    lock_fd = int(args.lock_fd) if getattr(args, "lock_fd", None) is not None else None

    updates: list[tuple[str, Any]] = []
    updates_map: dict[str, Any] = {}
    for item in args.pairs:
        if "=" not in item:
            print(f"ERROR: expected key=value pair, got {item!r}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        k = k.strip()
        if not k:
            print(f"ERROR: empty key in {item!r}", file=sys.stderr)
            return 2
        coerced = _coerce_journal_value(v)
        updates.append((k, coerced))
        updates_map[k] = coerced

    writes_owner = "target_owner" in updates_map
    if writes_owner:
        owner_val = str(updates_map["target_owner"] or "").strip()
        if owner_val not in gl.VALID_TARGET_OWNERS:
            print(
                f"ERROR: invalid target_owner {updates_map['target_owner']!r}; "
                f"expected one of {sorted(gl.VALID_TARGET_OWNERS)}",
                file=sys.stderr,
            )
            return 2
        if updates_map.get("completed") is True:
            print(
                "ERROR: refusing to write target_owner and completed=true in the same operation",
                file=sys.stderr,
            )
            return 1

    try:
        with _hold_or_verify_ops_lock(lock_file, lock_fd=lock_fd, caller_tag="preflight:journal-set"):
            existing, j_err = _read_journal_strict(ownership_path)
            if j_err is not None:
                print(
                    f"ERROR: refusing to overwrite corrupt/unreadable journal: {j_err}",
                    file=sys.stderr,
                )
                return 1
            if writes_owner:
                guard_reasons = _check_admission_and_incomplete_journal(
                    adm_path,
                    ownership_path,
                    existing_journal=existing,
                    journal_already_read=True,
                )
                if guard_reasons:
                    print(
                        "ERROR: change guard denied target_owner write: " + "; ".join(guard_reasons),
                        file=sys.stderr,
                    )
                    return 1
            current: dict[str, Any] = dict(existing) if existing is not None else {}
            for k, v in updates:
                current[k] = v
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            gl.write_ownership_journal_atomic(current, ownership_file=ownership_path)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.print_json:
        print(json.dumps(current, ensure_ascii=False))
    return 0


def cmd_journal_read(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    _, ownership_path, _, _ = _canonical_ops_paths(repo_root)
    data, j_err = _read_journal_strict(ownership_path)
    if j_err is not None:
        print(f"ERROR: {j_err}", file=sys.stderr)
        return 1
    if data is None:
        data = {}
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


def _fsync_file(path: Path) -> None:
    """Strictly fsync a regular file; any OSError aborts immediately."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_swap_paths(src: Path, dst: Path) -> None:
    """Atomically exchange two filesystem entries (e.g. symlink and real directory) in one syscall.

    Uses `renamex_np(..., RENAME_SWAP)` on macOS and `renameat2(..., RENAME_EXCHANGE)` on Linux.
    If the atomic swap fails or is unsupported, neither entry is modified and `OSError` is raised.
    """
    src_b = os.fsencode(str(src))
    dst_b = os.fsencode(str(dst))
    if sys.platform == "darwin":
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        libc.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        libc.renamex_np.restype = ctypes.c_int
        RENAME_SWAP = 0x00000002
        rc = libc.renamex_np(src_b, dst_b, RENAME_SWAP)
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(src), None, str(dst))
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        if hasattr(libc, "renameat2"):
            libc.renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            libc.renameat2.restype = ctypes.c_int
            AT_FDCWD = -100
            RENAME_EXCHANGE = 2
            rc = libc.renameat2(AT_FDCWD, src_b, AT_FDCWD, dst_b, RENAME_EXCHANGE)
            if rc != 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), str(src), None, str(dst))
            return
    raise OSError(
        errno.ENOTSUP,
        f"atomic directory-to-symlink swap unsupported on platform {sys.platform}",
        str(src),
        None,
        str(dst),
    )


def _write_recovery_manifest_dir(rec_dir: Path, source_commit: str) -> None:
    files_meta: dict[str, dict[str, Any]] = {}
    for name in RECOVERY_REQUIRED_FILES:
        fp = rec_dir / name
        if fp.is_symlink() or not fp.is_file():
            raise FileNotFoundError(f"missing or non-regular file for manifest: {fp}")
        _fsync_file(fp)
        files_meta[name] = {
            "sha256": _sha256_file(fp),
            "size": fp.stat().st_size,
        }
    manifest = {
        "version": 1,
        "source_commit": str(source_commit or "unknown"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files_meta,
    }
    manifest_path = rec_dir / "recovery-manifest.json"
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    _fsync_dir(rec_dir, strict=True)


def _verify_recovery_manifest_dir(rec_dir: Path) -> list[str]:
    manifest_path = rec_dir / "recovery-manifest.json"
    if not manifest_path.is_file():
        return [f"missing recovery-manifest.json in {rec_dir}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"invalid recovery-manifest.json: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return ["recovery-manifest.json must be a version 1 JSON object"]
    files_meta = manifest.get("files")
    if not isinstance(files_meta, dict) or not files_meta:
        return ["recovery-manifest.json has no files entries"]

    errors: list[str] = []
    required_set = set(RECOVERY_REQUIRED_FILES)
    manifest_keys = set(files_meta.keys())
    missing_entries = sorted(required_set - manifest_keys)
    extra_entries = sorted(manifest_keys - required_set)
    if missing_entries:
        errors.append(f"manifest missing required file entry(ies): {', '.join(missing_entries)}")
    if extra_entries:
        errors.append(f"manifest contains unexpected file entry(ies): {', '.join(extra_entries)}")

    for name in RECOVERY_REQUIRED_FILES:
        info = files_meta.get(name)
        if not isinstance(info, dict):
            if name not in missing_entries:
                errors.append(f"invalid metadata entry for {name}")
            continue
        fp = rec_dir / name
        if fp.is_symlink() or not fp.is_file():
            errors.append(f"missing or non-regular file: {name}")
            continue
        expected = str(info.get("sha256") or "")
        if not _SHA256_HEX_RE.fullmatch(expected):
            errors.append(f"invalid sha256 digest in manifest for {name}")
            continue
        expected_size = info.get("size")
        actual_size = fp.stat().st_size
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or actual_size != expected_size:
            errors.append(f"size mismatch for {name}: expected={expected_size!r} actual={actual_size}")
        actual = _sha256_file(fp)
        if actual != expected:
            errors.append(f"sha256 mismatch for {name}: expected={expected[:12]} actual={actual[:12]}")
    return errors


def publish_recovery_bin_atomic(
    *,
    repo_root: Path,
    src_dir: Path,
    gp_lock_src: Path,
    source_commit: str,
) -> Path:
    """Atomically stage, fsync, verify, and publish `<repo>/var/dalton/cutover-snapshots/recovery-bin`.

    - Any copy, fsync, verification, or swap failure aborts immediately and leaves any
      existing `recovery-bin` entry (symlink or legacy real directory) completely intact.
    - When `recovery-bin` is absent or already a symlink: single `os.replace` swap.
    - When `recovery-bin` is an existing real directory: single atomic `_atomic_swap_paths`
      (`renamex_np(..., RENAME_SWAP)` / `renameat2(..., RENAME_EXCHANGE)`) so the fixed
      entrypoint is NEVER missing for even an instant.
    """
    repo_abs = repo_root.expanduser().resolve()
    snapshots_root = repo_abs / "var" / "dalton" / "cutover-snapshots"
    snapshots_root.mkdir(parents=True, exist_ok=True)
    os.chmod(snapshots_root, 0o700)

    src_map: dict[str, tuple[Path, int]] = {
        "rollback_research_host.sh": (src_dir / "rollback_research_host.sh", 0o700),
        "snapshot_research_host.sh": (src_dir / "snapshot_research_host.sh", 0o700),
        "_research_host_handoff_lib.sh": (src_dir / "_research_host_handoff_lib.sh", 0o600),
        "wait_backend_idle.sh": (src_dir / "wait_backend_idle.sh", 0o700),
        "preflight.py": (src_dir / "preflight.py", 0o700),
        "guidepoint_lock.py": (gp_lock_src, 0o600),
    }
    for name, (sp, _mode) in src_map.items():
        if not sp.is_file():
            raise FileNotFoundError(f"missing required recovery file ({name}): {sp}")

    new_rel_dir = Path(tempfile.mkdtemp(prefix=".recovery-bin.rel.", dir=str(snapshots_root)))
    tmp_link = snapshots_root / f".recovery-bin.link.{os.getpid()}.{secrets.token_hex(4)}"
    rollback_link: Path | None = None
    published = False
    preserve_staged = False
    try:
        os.chmod(new_rel_dir, 0o700)
        for name, (sp, mode) in src_map.items():
            dst = new_rel_dir / name
            raw_bytes = sp.read_bytes()
            fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            closed = False
            try:
                os.fchmod(fd, mode)
                view = memoryview(raw_bytes)
                written = 0
                while written < len(view):
                    written += os.write(fd, view[written:])
                os.fsync(fd)
                os.close(fd)
                closed = True
            except BaseException:
                if not closed:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                raise
            os.chmod(dst, mode)

        _write_recovery_manifest_dir(new_rel_dir, source_commit)
        verify_errors = _verify_recovery_manifest_dir(new_rel_dir)
        if verify_errors:
            raise RuntimeError("staged recovery-bin failed manifest verification: " + "; ".join(verify_errors))

        os.symlink(new_rel_dir.name, tmp_link)
        rec_entry = snapshots_root / "recovery-bin"

        entry_is_symlink = rec_entry.is_symlink()
        entry_exists = rec_entry.exists()
        prev_target = os.readlink(rec_entry) if entry_is_symlink else ""
        if entry_is_symlink:
            rollback_link = snapshots_root / f".recovery-bin.rollback.{os.getpid()}.{secrets.token_hex(4)}"
            os.symlink(prev_target, rollback_link)

        # Durably flush the staged release tree and parent directory entries BEFORE swapping `recovery-bin`.
        _fsync_dir(snapshots_root, strict=True)

        if entry_is_symlink:
            assert rollback_link is not None
            os.replace(tmp_link, rec_entry)
            try:
                _fsync_dir(snapshots_root, strict=True)
            except BaseException as post_exc:
                try:
                    os.replace(rollback_link, rec_entry)
                except OSError as rb_exc:
                    preserve_staged = True
                    raise OSError(
                        rb_exc.errno,
                        (
                            f"post-swap fsync failed ({post_exc}) and rollback of {rec_entry} "
                            f"to {prev_target!r} failed ({rb_exc}); rollback symlink kept at {rollback_link}"
                        ),
                        str(rec_entry),
                    ) from post_exc
                _fsync_dir(snapshots_root, strict=False)
                raise
            published = True
            try:
                rollback_link.unlink(missing_ok=True)
            except OSError:
                pass
            if prev_target and prev_target != new_rel_dir.name and prev_target.startswith(".recovery-bin.rel."):
                shutil.rmtree(snapshots_root / prev_target, ignore_errors=True)
        elif not entry_exists:
            os.replace(tmp_link, rec_entry)
            try:
                _fsync_dir(snapshots_root, strict=True)
            except BaseException as post_exc:
                try:
                    rec_entry.unlink()
                except OSError as rb_exc:
                    preserve_staged = True
                    raise OSError(
                        rb_exc.errno,
                        f"post-swap fsync failed ({post_exc}) and rollback unlink of {rec_entry} failed ({rb_exc})",
                        str(rec_entry),
                    ) from post_exc
                _fsync_dir(snapshots_root, strict=False)
                raise
            published = True
        else:
            # Atomic single-syscall migration from existing real directory to symlink.
            _atomic_swap_paths(tmp_link, rec_entry)
            try:
                _fsync_dir(snapshots_root, strict=True)
            except BaseException as post_exc:
                try:
                    _atomic_swap_paths(tmp_link, rec_entry)
                except OSError as rb_exc:
                    preserve_staged = True
                    raise OSError(
                        rb_exc.errno,
                        (
                            f"post-swap fsync failed ({post_exc}) and rollback swap of {rec_entry} "
                            f"failed ({rb_exc}); displaced previous entry kept at {tmp_link}"
                        ),
                        str(rec_entry),
                    ) from post_exc
                _fsync_dir(snapshots_root, strict=False)
                raise
            published = True
            # tmp_link now points to the displaced old real entry; remove it cleanly.
            if tmp_link.is_dir() and not tmp_link.is_symlink():
                shutil.rmtree(tmp_link, ignore_errors=True)
            else:
                try:
                    tmp_link.unlink(missing_ok=True)
                except OSError:
                    pass
        return rec_entry
    except BaseException:
        if not published and not preserve_staged:
            for link_path in (tmp_link, rollback_link):
                if link_path is None:
                    continue
                try:
                    if link_path.is_symlink() or link_path.is_file():
                        link_path.unlink()
                except OSError:
                    pass
            shutil.rmtree(new_rel_dir, ignore_errors=True)
        raise


def cmd_recovery_publish(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    src_dir = Path(args.src_dir).expanduser().resolve()
    gp_lock_src = Path(args.gp_lock_src).expanduser().resolve()
    try:
        publish_recovery_bin_atomic(
            repo_root=repo_root,
            src_dir=src_dir,
            gp_lock_src=gp_lock_src,
            source_commit=str(args.source_commit or "unknown"),
        )
    except Exception as exc:
        print(f"ERROR: atomic recovery-bin publish failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_manifest_write(args: argparse.Namespace) -> int:
    rec_dir = Path(args.recovery_dir).expanduser().resolve()
    if not rec_dir.is_dir():
        print(f"ERROR: recovery dir not found: {rec_dir}", file=sys.stderr)
        return 1
    requested = list(args.files)
    if set(requested) != set(RECOVERY_REQUIRED_FILES) or len(requested) != len(RECOVERY_REQUIRED_FILES):
        print(
            f"ERROR: recovery manifest must cover the exact required {len(RECOVERY_REQUIRED_FILES)}-file set "
            f"{list(RECOVERY_REQUIRED_FILES)}, got {requested}",
            file=sys.stderr,
        )
        return 1
    try:
        _write_recovery_manifest_dir(rec_dir, str(args.source_commit or "unknown"))
    except Exception as exc:
        print(f"ERROR: failed writing recovery manifest: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_manifest_verify(args: argparse.Namespace) -> int:
    rec_dir = Path(args.recovery_dir).expanduser().resolve()
    errors = _verify_recovery_manifest_dir(rec_dir)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_approve_budgets(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    lock_file, journal_path, _, canonical_budgets_path = _canonical_ops_paths(repo_root)
    budgets_path = (
        Path(args.budgets_file).expanduser().resolve()
        if getattr(args, "budgets_file", None)
        else canonical_budgets_path
    )
    lock_fd = int(args.lock_fd) if getattr(args, "lock_fd", None) is not None else None

    try:
        with _hold_or_verify_ops_lock(
            lock_file, lock_fd=lock_fd, caller_tag="preflight:approve-budgets"
        ):
            # Strict journal precondition: must exist, be valid JSON object, and completed == true
            journal, j_err = _read_journal_strict(journal_path)
            if j_err is not None:
                print(
                    f"ERROR: --approve-budgets 拒绝执行，交接日志不可用：{j_err}",
                    file=sys.stderr,
                )
                return 1
            if journal is None:
                print(
                    f"ERROR: --approve-budgets 要求交接日志存在且 completed == true（缺失 {journal_path}）",
                    file=sys.stderr,
                )
                return 1
            if journal.get("completed") is not True:
                print(
                    f"ERROR: --approve-budgets 要求 handoff-journal.json 的 completed == true "
                    f"（当前为 {journal.get('completed')!r}）",
                    file=sys.stderr,
                )
                return 1

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
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_guard_check(args: argparse.Namespace) -> int:
    action = str(args.action or "").strip()
    if action in _EXEMPT_ACTIONS:
        return 0
    if action not in _GUARDED_ACTIONS:
        print(f"ERROR: unknown change guard action: {action!r}", file=sys.stderr)
        return 2

    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    lock_file, j_path, adm_path, _ = _canonical_ops_paths(repo_root)
    missing_reasons: list[str] = []

    # Condition 1: current process ACTUALLY holds kernel flock on canonical .ops.lock
    lock_fd = int(args.lock_fd) if getattr(args, "lock_fd", None) is not None else 8
    if str(args.lock_held) != "1":
        missing_reasons.append("current process does not hold .ops.lock")
    else:
        lock_ok, lock_reason = _verify_inherited_ops_lock(lock_file, lock_fd=lock_fd)
        if not lock_ok:
            missing_reasons.append(f"current process does not hold .ops.lock ({lock_reason})")

    # Conditions 2 & 3: full admission pause + incomplete handoff-journal.json
    missing_reasons.extend(_check_admission_and_incomplete_journal(adm_path, j_path))

    if missing_reasons:
        print(
            f"ERROR: change guard denied action {action!r}: " + "; ".join(missing_reasons),
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_compose_args(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    repo_env_file = Path(args.repo_env).expanduser().resolve() if getattr(args, "repo_env", None) else None
    docker_bin = os.environ.get("DOCKER_BIN", "docker")
    c_args, _, err = resolve_compose_args(
        repo_root,
        repo_env_file=repo_env_file,
        docker_bin=docker_bin,
    )
    if err is not None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(c_args, ensure_ascii=False))
    else:
        for item in c_args:
            print(item)
    return 0


def _collect_status_summary_dict(
    j_path: Path,
    a_path: Path,
    b_path: Path | None,
    *,
    include_dry_run_plan: bool = False,
    test_workspace_id: str = "",
    keep_admission_paused: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if j_path.is_file():
        try:
            j = json.loads(j_path.read_text(encoding="utf-8"))
            summary["handoff_journal"] = {"exists": True, "data": j}
        except Exception as exc:
            summary["handoff_journal"] = {"exists": True, "error": type(exc).__name__}
    else:
        summary["handoff_journal"] = {"exists": False, "path": str(j_path)}

    if a_path.is_file():
        try:
            a = json.loads(a_path.read_text(encoding="utf-8"))
            summary["maintenance_admission"] = {"exists": True, "data": a}
        except Exception as exc:
            summary["maintenance_admission"] = {"exists": True, "error": type(exc).__name__}
    else:
        summary["maintenance_admission"] = {"exists": False, "path": str(a_path)}

    if include_dry_run_plan:
        summary["dry_run_plan"] = [
            "1. Record switch_pausing_admission intent, lock maintenance admission (allow_workspace_ids=[]), verify idle & preflight, snapshot + baseline-snapshot.json",
            "2. Bootstrap com.dalton.research-host (:8951) in standby and run read-only probes",
            "3. Start handoff clock (T_trigger), drain :8943, archive plist & bootout to release .guidepoint.owner.lock",
            "4. Set target_owner=research_host_8951, call :8951/admin/guidepoint/acquire-lock & run bounded search probe",
            "5. Drain & kickstart :8960 with :8951 Guidepoint URL and run parallel 3-way real probes",
            "6. Build frontend (--out-dir), close migrated :8960 routes first, force-recreate backend+edge & stop old sandboxes",
            "7. Verify optional --test-workspace-id (new OK + legacy route_migrated), resume admission, mark completed=true",
        ]
        if test_workspace_id:
            summary["test_workspace_id"] = test_workspace_id
        if keep_admission_paused:
            summary["keep_admission_paused"] = True
        if b_path is None or not b_path.is_file():
            summary["timing_budgets"] = {"exists": False, "path": str(b_path) if b_path else ""}
        else:
            parsed, errs = parse_timing_budgets_env(b_path)
            if errs:
                summary["timing_budgets"] = {"exists": True, "errors": errs}
            else:
                summary["timing_budgets"] = {"exists": True, "values": parsed}
    return summary


def cmd_status_summary(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if getattr(args, "repo", None) else _default_repo_root()
    _, j_path, a_path, default_b_path = _canonical_ops_paths(repo_root)
    b_path = Path(args.budgets_file).expanduser().resolve() if args.budgets_file else default_b_path

    if getattr(args, "json", False):
        payload = _collect_status_summary_dict(
            j_path,
            a_path,
            b_path,
            include_dry_run_plan=bool(args.include_dry_run_plan),
            test_workspace_id=str(getattr(args, "test_workspace_id", "") or ""),
            keep_admission_paused=bool(getattr(args, "keep_admission_paused", False)),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

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
    p.add_argument(
        "--skip-budgets",
        action="store_true",
        help="Skip timing-budgets.env validation (for manual downtime cutover precheck)",
    )
    p.add_argument("--repo", default=None, help="Repository root path")
    p.add_argument("--home", default=None, help="Home directory path")
    p.add_argument("--plist-dir", default=None, help="Installed LaunchAgents plist directory")
    p.add_argument(
        "--research-host-plist",
        default=None,
        help="Explicit path to archived/staged com.dalton.research-host.plist",
    )
    p.add_argument(
        "--archived-plist-dir",
        default=None,
        help="Directory holding archived com.dalton.research-host.plist for Stage B",
    )
    p.add_argument("--backend-url", default="http://127.0.0.1:8000")
    p.add_argument("--proxy-url", default="http://127.0.0.1:8943")
    p.add_argument("--bridge-url", default="http://127.0.0.1:8960")
    p.add_argument("--research-host-url", default="http://127.0.0.1:8951")
    p.add_argument("--proxy-listen-port", type=int, default=8943)
    p.add_argument("--budgets-file", default=None)
    p.add_argument(
        "--include-summary",
        action="store_true",
        help="Include read-only cutover state summary in JSON output",
    )
    p.add_argument(
        "--include-dry-run-plan",
        action="store_true",
        help="Include Stage B 7-step dry-run plan in JSON summary output",
    )
    p.add_argument("--test-workspace-id", default="")
    p.add_argument("--keep-admission-paused", action="store_true")
    return p


# ---------------------------------------------------------------------------
# Manual downtime cutover helpers: atomic snapshots, gate control, live probes
# ---------------------------------------------------------------------------

SNAPSHOT_MANIFEST_NAME = "snapshot-manifest.json"
SNAPSHOT_MANDATORY_EXISTING_KEYS: tuple[str, ...] = (
    "repo_env",
    "plist_proxy",
    "plist_bridge",
    "guidepoint_tokens",
)
SNAPSHOT_RESERVED_DIRS: frozenset[str] = frozenset(
    {
        "recovery-bin",
        "research-host",
        "stage-a",
        "measure-budgets",
        "deploy-research-host",
    }
)
_SNAPSHOT_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")


def _import_maintenance_gate(repo_root: Path) -> Any:
    repo_str = str(repo_root.expanduser().resolve())
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    return importlib.import_module("src.dalton.maintenance_gate")


def _write_bytes_atomic(dest: Path, data: bytes, *, mode: int = 0o600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent),
        prefix=f".{dest.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    closed = False
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
        _fsync_dir(dest.parent, strict=True)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _resolve_snapshot_sources(
    *,
    repo_root: Path,
    home_dir: Path,
    plist_dir: Path | None = None,
    repo_env: Path | None = None,
    bridge_env: Path | None = None,
    research_host_env: Path | None = None,
    guidepoint_env: Path | None = None,
    tokens_file: Path | None = None,
    journal_file: Path | None = None,
    web_releases_current: Path | None = None,
) -> dict[str, Path]:
    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    eff_plist_dir = (
        plist_dir.expanduser().resolve()
        if plist_dir is not None
        else (
            Path(os.environ["DALTON_LAUNCH_AGENTS_DIR"]).expanduser().resolve()
            if str(os.environ.get("DALTON_LAUNCH_AGENTS_DIR") or "").strip()
            else (home_abs / "Library" / "LaunchAgents")
        )
    )
    eff_repo_env = (
        repo_env.expanduser().resolve()
        if repo_env is not None
        else (
            Path(os.environ["DALTON_REPO_ENV_FILE"]).expanduser().resolve()
            if str(os.environ.get("DALTON_REPO_ENV_FILE") or "").strip()
            else (repo_abs / ".env")
        )
    )
    eff_bridge_env = (
        bridge_env.expanduser().resolve()
        if bridge_env is not None
        else (
            Path(os.environ["DALTON_BRIDGE_ENV_FILE"]).expanduser().resolve()
            if str(os.environ.get("DALTON_BRIDGE_ENV_FILE") or "").strip()
            else (home_abs / ".config" / "dalton" / "researchdata-bridge.env")
        )
    )
    eff_rh_env = (
        research_host_env.expanduser().resolve()
        if research_host_env is not None
        else (
            Path(os.environ["DALTON_RESEARCH_HOST_ENV_FILE"]).expanduser().resolve()
            if str(os.environ.get("DALTON_RESEARCH_HOST_ENV_FILE") or "").strip()
            else (home_abs / ".config" / "dalton" / "research-host.env")
        )
    )
    eff_journal = (
        journal_file.expanduser().resolve()
        if journal_file is not None
        else (
            Path(os.environ["DALTON_GUIDEPOINT_OWNERSHIP_FILE"]).expanduser().resolve()
            if str(os.environ.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip()
            else (repo_abs / DEFAULT_OWNERSHIP_REL_PATH)
        )
    )

    proxy_plist = eff_plist_dir / "com.dalton.guidepoint-mcp-proxy.plist"
    bridge_plist = eff_plist_dir / "com.dalton.researchdata-bridge.plist"
    rh_plist = eff_plist_dir / "com.dalton.research-host.plist"

    # Reuse preflight's legacy proxy path resolver (including guidepoint_mcp.env overrides)
    # whenever tokens_file or guidepoint_env is not explicitly overridden.
    eff_gp_env: Path
    eff_tokens: Path
    if tokens_file is not None and guidepoint_env is not None:
        eff_tokens = tokens_file.expanduser().resolve()
        eff_gp_env = guidepoint_env.expanduser().resolve()
    else:
        bse_mod = _import_bridge_subprocess_env(repo_abs)
        gl_mod = _import_guidepoint_lock(repo_abs)
        bridge_vals = _parse_simple_env_file(eff_bridge_env)
        resolved_proxy = bse_mod.resolve_legacy_proxy_effective_paths(
            repo_root=repo_abs,
            home_dir=home_abs,
            existing_proxy_plist_path=proxy_plist,
            bridge_env_vals=bridge_vals,
            guidepoint_env_path=guidepoint_env,
        )
        eff_proxy_env = dict(resolved_proxy.get("effective_env") or {"HOME": str(home_abs)})
        eff_gp_env = (
            guidepoint_env.expanduser().resolve()
            if guidepoint_env is not None
            else Path(str(resolved_proxy["guidepoint_env_file"])).expanduser().resolve()
        )
        eff_tokens = (
            tokens_file.expanduser().resolve()
            if tokens_file is not None
            else gl_mod.resolve_guidepoint_tokens_file(environ=eff_proxy_env)
        )

    eff_web_cur = (
        _abs_no_deref(web_releases_current)
        if web_releases_current is not None
        else (repo_abs / "web" / "releases" / "current")
    )

    return {
        "repo_env": eff_repo_env,
        "bridge_env": eff_bridge_env,
        "research_host_env": eff_rh_env,
        "guidepoint_env": eff_gp_env,
        "plist_proxy": proxy_plist,
        "plist_bridge": bridge_plist,
        "plist_research_host": rh_plist,
        "guidepoint_tokens": eff_tokens,
        "handoff_journal": eff_journal,
        "web_releases_current": eff_web_cur,
    }


def _snapshot_entry_rel_path(key: str) -> str:
    mapping = {
        "repo_env": "files/repo.env",
        "bridge_env": "files/researchdata-bridge.env",
        "research_host_env": "files/research-host.env",
        "guidepoint_env": "files/guidepoint_mcp.env",
        "plist_proxy": "files/plists/com.dalton.guidepoint-mcp-proxy.plist",
        "plist_bridge": "files/plists/com.dalton.researchdata-bridge.plist",
        "plist_research_host": "files/plists/com.dalton.research-host.plist",
        "guidepoint_tokens": "files/guidepoint_mcp_tokens.json",
        "handoff_journal": "files/handoff-journal.json",
    }
    return mapping[key]


def _parse_frontend_version_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a frontend `version.json` file and require a JSON object with a non-empty build ID."""
    if path.is_symlink() or not path.is_file():
        return None, f"missing or non-regular version.json ({path})"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"invalid JSON in {path}: {exc}"
    if not isinstance(raw, dict):
        return None, f"version.json root must be a JSON object ({path})"
    build_val = str(raw.get("build") or raw.get("build_id") or "").strip()
    if not build_val:
        return None, f"version.json missing non-empty build identifier ({path})"
    return raw, None


def _diff_frontend_version_json(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    actual_label: str = "actual",
    expected_label: str = "expected",
) -> list[str]:
    """Compare full `version.json` JSON objects (including `entry`) and return field mismatch descriptions."""
    exp_dict = dict(expected)
    act_dict = dict(actual)
    if json.dumps(act_dict, sort_keys=True) == json.dumps(exp_dict, sort_keys=True):
        return []
    mismatches: list[str] = []
    for key in sorted(set(exp_dict.keys()) | set(act_dict.keys())):
        if key not in act_dict:
            mismatches.append(f"{key}: missing in {actual_label} ({expected_label}={exp_dict[key]!r})")
        elif key not in exp_dict:
            mismatches.append(f"{key}: unexpected in {actual_label} ({actual_label}={act_dict[key]!r})")
        elif json.dumps(act_dict[key], sort_keys=True) != json.dumps(exp_dict[key], sort_keys=True):
            mismatches.append(
                f"{key}: {actual_label}={act_dict[key]!r} {expected_label}={exp_dict[key]!r}"
            )
    return mismatches


def _validate_web_releases_current_for_snapshot(
    web_cur: Path,
) -> tuple[Path, str, str, dict[str, Any]]:
    """Validate `web/releases/current` is a valid symlink inside `web/releases/` with valid `version.json`."""
    if not web_cur.is_symlink():
        raise ValueError(
            f"refusing snapshot/verification: {web_cur} must be a symlink to a frontend release directory"
        )
    if not web_cur.exists():
        raise FileNotFoundError(
            f"refusing snapshot/verification: {web_cur} is a dangling symlink"
        )
    resolved = web_cur.resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"refusing snapshot/verification: {web_cur} target is not a directory ({resolved})"
        )
    releases_root = web_cur.parent.resolve()
    try:
        rel_sub = resolved.relative_to(releases_root)
    except ValueError as exc:
        raise ValueError(
            f"refusing snapshot/verification: {web_cur} escapes {releases_root} ({resolved})"
        ) from exc
    if str(rel_sub) in ("", "."):
        raise ValueError(
            f"refusing snapshot/verification: {web_cur} points to {releases_root} itself instead of a release directory"
        )
    link_target = os.readlink(web_cur)
    if not link_target or link_target in (".", ".."):
        raise ValueError(
            f"refusing snapshot/verification: {web_cur} has invalid symlink target {link_target!r}"
        )
    ver_payload, ver_err = _parse_frontend_version_json(resolved / "version.json")
    if ver_err or ver_payload is None:
        raise ValueError(f"refusing snapshot/verification: {ver_err}")
    rel_id = resolved.name if resolved.parent == releases_root else str(rel_sub)
    return resolved, link_target, rel_id, ver_payload


def _verify_snapshot_frontend_target(
    snap_path: Path,
    web_info: Mapping[str, Any],
    *,
    snap_ver_obj: Mapping[str, Any] | None = None,
) -> list[str]:
    """Verify the snapshot's frontend release target directory exists and its `version.json` matches."""
    errors: list[str] = []
    w_path = str(web_info.get("path") or "").strip()
    w_target = str(web_info.get("link_target") or "").strip()
    if not w_path or not w_target:
        return ["web_releases_current missing path or link_target"]
    cur_path = _abs_no_deref(w_path)
    resolved_check = (
        Path(w_target)
        if os.path.isabs(w_target)
        else (cur_path.parent / w_target)
    )
    if not resolved_check.is_dir():
        return [f"web_releases_current target release directory missing: {resolved_check}"]
    releases_root = cur_path.parent.resolve()
    resolved_abs = resolved_check.resolve()
    try:
        rel_sub = resolved_abs.relative_to(releases_root)
    except ValueError:
        return [
            f"web_releases_current target release directory escapes {releases_root}: {resolved_abs}"
        ]
    if str(rel_sub) in ("", "."):
        return [
            f"web_releases_current target points to {releases_root} itself instead of a release directory"
        ]
    if cur_path.exists() and not cur_path.is_symlink():
        errors.append(
            f"web_releases_current path {cur_path} exists as a non-symlink; inspect or move it manually before rollback"
        )
    target_ver_obj, target_ver_err = _parse_frontend_version_json(resolved_abs / "version.json")
    if target_ver_err or target_ver_obj is None:
        errors.append(f"web_releases_current target release version.json invalid ({target_ver_err})")
        return errors
    if snap_ver_obj is None:
        v_rel = str(web_info.get("version_json_rel_path") or "").strip()
        snap_ver_obj, snap_ver_err = _parse_frontend_version_json(snap_path / v_rel)
        if snap_ver_err or snap_ver_obj is None:
            errors.append(f"web_releases_current snapshot version.json invalid ({snap_ver_err})")
            return errors
    mismatches = _diff_frontend_version_json(
        snap_ver_obj,
        target_ver_obj,
        actual_label="target",
        expected_label="snapshot",
    )
    for msg in mismatches:
        errors.append(
            f"web_releases_current target {resolved_abs}/version.json mismatch ({msg})"
        )
    return errors


def verify_cutover_snapshot(snapshot_dir: Path | str) -> list[str]:
    """Verify a cutover snapshot directory against its `snapshot-manifest.json` SHA-256 list."""
    snap_path = Path(snapshot_dir).expanduser().resolve()
    if not snap_path.is_dir():
        return [f"snapshot directory not found: {snap_path}"]
    manifest_path = snap_path / SNAPSHOT_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return [f"missing or non-regular {SNAPSHOT_MANIFEST_NAME} in {snap_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"invalid {SNAPSHOT_MANIFEST_NAME}: {exc}"]
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        return [f"{SNAPSHOT_MANIFEST_NAME} must be a version 1 JSON object"]
    entries = manifest.get("files")
    if not isinstance(entries, dict) or not entries:
        return [f"{SNAPSHOT_MANIFEST_NAME} has no file entries"]

    required_keys = {
        "repo_env",
        "plist_proxy",
        "plist_bridge",
        "plist_research_host",
        "guidepoint_tokens",
        "handoff_journal",
    }
    missing_keys = sorted(required_keys - set(entries.keys()))
    errors: list[str] = []
    if missing_keys:
        errors.append(f"manifest missing required key(s): {', '.join(missing_keys)}")

    for key, info in entries.items():
        if not isinstance(info, dict):
            errors.append(f"invalid entry metadata for {key}")
            continue
        rel = str(info.get("rel_path") or "")
        existed = info.get("existed")
        if not isinstance(existed, bool):
            errors.append(f"entry {key} missing boolean existed flag")
            continue
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            errors.append(f"entry {key} has unsafe rel_path: {rel!r}")
            continue
        fp = snap_path / rel
        if not existed:
            if key in SNAPSHOT_MANDATORY_EXISTING_KEYS:
                errors.append(f"mandatory rollback file {key} marked existed=false in manifest")
            if fp.exists() or fp.is_symlink():
                errors.append(f"entry {key} marked existed=false but file present: {rel}")
            continue
        if fp.is_symlink() or not fp.is_file():
            errors.append(f"missing or non-regular snapshot file for {key}: {rel}")
            continue
        expected_sha = str(info.get("sha256") or "")
        if not _SHA256_HEX_RE.fullmatch(expected_sha):
            errors.append(f"invalid sha256 in manifest for {key}")
            continue
        expected_size = info.get("size")
        actual_size = fp.stat().st_size
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or actual_size != expected_size:
            errors.append(f"size mismatch for {key}: expected={expected_size!r} actual={actual_size}")
        actual_sha = _sha256_file(fp)
        if actual_sha != expected_sha:
            errors.append(f"sha256 mismatch for {key}: expected={expected_sha[:12]} actual={actual_sha[:12]}")

    web_info = manifest.get("web_releases_current")
    if not isinstance(web_info, dict):
        errors.append("manifest missing required web_releases_current metadata")
    else:
        w_path = str(web_info.get("path") or "").strip()
        w_exists = web_info.get("exists")
        w_is_symlink = web_info.get("is_symlink")
        w_target = str(web_info.get("link_target") or "").strip()
        web_meta_ok = True
        if not w_path:
            errors.append("web_releases_current missing path")
            web_meta_ok = False
        if w_exists is not True:
            errors.append("web_releases_current must record exists=true")
            web_meta_ok = False
        if w_is_symlink is not True:
            errors.append("web_releases_current must record is_symlink=true")
            web_meta_ok = False
        if not w_target or w_target in (".", "..") or ".." in Path(w_target).parts:
            errors.append(f"web_releases_current has missing or unsafe link_target: {w_target!r}")
            web_meta_ok = False
        v_rel = str(web_info.get("version_json_rel_path") or "").strip()
        v_sha = str(web_info.get("version_json_sha256") or "").strip()
        snap_ver_obj: dict[str, Any] | None = None
        if not v_rel or v_rel.startswith("/") or ".." in Path(v_rel).parts:
            errors.append(f"web_releases_current missing or unsafe version_json_rel_path: {v_rel!r}")
        else:
            v_fp = snap_path / v_rel
            if v_fp.is_symlink() or not v_fp.is_file():
                errors.append(f"missing web release version snapshot: {v_rel}")
            elif not _SHA256_HEX_RE.fullmatch(v_sha) or _sha256_file(v_fp) != v_sha:
                errors.append(f"sha256 mismatch for {v_rel}")
            else:
                v_obj, v_err = _parse_frontend_version_json(v_fp)
                if v_err or v_obj is None:
                    errors.append(f"invalid web release version snapshot ({v_rel}): {v_err}")
                else:
                    snap_ver_obj = v_obj
        if web_meta_ok and snap_ver_obj is not None:
            errors.extend(
                _verify_snapshot_frontend_target(
                    snap_path,
                    web_info,
                    snap_ver_obj=snap_ver_obj,
                )
            )

    return errors


def create_cutover_snapshot(
    *,
    repo_root: Path,
    home_dir: Path,
    out_dir: Path | None = None,
    label: str = "",
    plist_dir: Path | None = None,
    repo_env: Path | None = None,
    bridge_env: Path | None = None,
    research_host_env: Path | None = None,
    guidepoint_env: Path | None = None,
    tokens_file: Path | None = None,
    journal_file: Path | None = None,
    web_releases_current: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Atomically capture all rollback-required files and verify SHA-256 manifest."""
    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    clean_label = str(label or "").strip()
    if clean_label and not _SNAPSHOT_LABEL_RE.fullmatch(clean_label):
        raise ValueError(f"invalid snapshot label: {clean_label!r}")

    snapshots_root = repo_abs / "var" / "dalton" / "cutover-snapshots"
    now_utc = datetime.now(timezone.utc)
    stamp = now_utc.strftime("%Y%m%d-%H%M%SZ")
    snap_id = f"{stamp}-{clean_label}" if clean_label else stamp

    if out_dir is not None:
        final_dir = _abs_no_deref(out_dir)
        parent_dir = final_dir.parent
    else:
        parent_dir = snapshots_root
        final_dir = parent_dir / snap_id
        # Avoid same-second name collisions when invoked repeatedly
        if final_dir.exists():
            snap_id = f"{snap_id}-{secrets.token_hex(3)}"
            final_dir = parent_dir / snap_id

    if final_dir.exists() or final_dir.is_symlink():
        raise FileExistsError(f"target snapshot directory already exists: {final_dir}")

    parent_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(parent_dir, 0o700)

    sources = _resolve_snapshot_sources(
        repo_root=repo_abs,
        home_dir=home_abs,
        plist_dir=plist_dir,
        repo_env=repo_env,
        bridge_env=bridge_env,
        research_host_env=research_host_env,
        guidepoint_env=guidepoint_env,
        tokens_file=tokens_file,
        journal_file=journal_file,
        web_releases_current=web_releases_current,
    )

    missing_mandatory: list[str] = []
    for req_key in SNAPSHOT_MANDATORY_EXISTING_KEYS:
        req_path = sources[req_key]
        if not req_path.is_file():
            missing_mandatory.append(f"{req_key} ({req_path})")
    if missing_mandatory:
        raise FileNotFoundError(
            "refusing to publish incomplete snapshot; missing mandatory file(s): "
            + ", ".join(missing_mandatory)
        )
    tok_ok, _tok_ts, tok_reason = _inspect_guidepoint_tokens_file(sources["guidepoint_tokens"])
    if not tok_ok:
        raise ValueError(
            f"refusing to publish snapshot with invalid Guidepoint tokens file ({sources['guidepoint_tokens']}): {tok_reason}"
        )
    web_resolved_path, web_link_target, web_rel_id, _web_ver_obj = _validate_web_releases_current_for_snapshot(
        sources["web_releases_current"]
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix=".snapshot.tmp.", dir=str(parent_dir)))
    renamed = False
    try:
        os.chmod(tmp_dir, 0o700)
        files_dir = tmp_dir / "files"
        plists_dir = files_dir / "plists"
        plists_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(files_dir, 0o700)
        os.chmod(plists_dir, 0o700)

        file_keys = (
            "repo_env",
            "bridge_env",
            "research_host_env",
            "guidepoint_env",
            "plist_proxy",
            "plist_bridge",
            "plist_research_host",
            "guidepoint_tokens",
            "handoff_journal",
        )
        files_meta: dict[str, dict[str, Any]] = {}
        for key in file_keys:
            src = sources[key]
            rel = _snapshot_entry_rel_path(key)
            if src.is_file():
                raw = src.read_bytes()
                st_src = src.stat()
                src_mode = stat.S_IMODE(st_src.st_mode)
                dst = tmp_dir / rel
                fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                closed = False
                try:
                    os.fchmod(fd, 0o600)
                    view = memoryview(raw)
                    written = 0
                    while written < len(view):
                        written += os.write(fd, view[written:])
                    os.fsync(fd)
                    os.close(fd)
                    closed = True
                except BaseException:
                    if not closed:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    raise
                os.chmod(dst, 0o600)
                files_meta[key] = {
                    "rel_path": rel,
                    "source_path": str(src),
                    "existed": True,
                    "mode": f"{src_mode:04o}",
                    "mtime": float(st_src.st_mtime),
                    "size": len(raw),
                    "sha256": _sha256_file(dst),
                }
            else:
                files_meta[key] = {
                    "rel_path": rel,
                    "source_path": str(src),
                    "existed": False,
                    "mode": None,
                    "mtime": None,
                    "size": 0,
                    "sha256": None,
                }

        web_cur = sources["web_releases_current"]
        web_meta: dict[str, Any] = {
            "path": str(web_cur),
            "exists": True,
            "is_symlink": True,
            "link_target": web_link_target,
            "resolved_target": str(web_resolved_path),
            "release_id": web_rel_id,
        }
        pin_dir = web_cur.parent / ".pinned-releases"
        pin_dir.mkdir(parents=True, exist_ok=True)
        pin_file = pin_dir / web_rel_id
        pin_payload = {
            "release_id": web_rel_id,
            "pinned_by": "cutover_snapshot",
            "snapshot_id": final_dir.name,
            "created_at": now_utc.isoformat(),
        }
        _atomic_write_text(
            pin_file,
            json.dumps(pin_payload, ensure_ascii=False, indent=2) + "\n",
            mode=0o644,
        )
        web_meta["pin_file"] = str(pin_file)

        ver_src = web_resolved_path / "version.json"
        ver_rel = "files/web_release_version.json"
        ver_dst = tmp_dir / ver_rel
        ver_raw = ver_src.read_bytes()
        fd = os.open(str(ver_dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        closed = False
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(ver_raw)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
            os.fsync(fd)
            os.close(fd)
            closed = True
        except BaseException:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        os.chmod(ver_dst, 0o600)
        web_meta["version_json_rel_path"] = ver_rel
        web_meta["version_json_sha256"] = _sha256_file(ver_dst)

        git_rc, git_out, _ = _run_cmd(["git", "-C", str(repo_abs), "rev-parse", "HEAD"], timeout_s=5.0)
        git_head = git_out.strip() if git_rc == 0 else ""

        manifest: dict[str, Any] = {
            "version": 1,
            "snapshot_id": final_dir.name,
            "label": clean_label,
            "created_at": now_utc.isoformat(),
            "repo_root": str(repo_abs),
            "home_dir": str(home_abs),
            "plist_dir": str(sources["plist_proxy"].parent),
            "git_head": git_head,
            "files": files_meta,
            "web_releases_current": web_meta,
        }
        manifest_path = tmp_dir / SNAPSHOT_MANIFEST_NAME
        _atomic_write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
        )

        # Fsync every directory level before atomic rename
        _fsync_dir(plists_dir, strict=True)
        _fsync_dir(files_dir, strict=True)
        _fsync_dir(tmp_dir, strict=True)

        # Verify hash manifest BEFORE publishing
        pre_errs = verify_cutover_snapshot(tmp_dir)
        if pre_errs:
            raise RuntimeError("snapshot verification failed before rename: " + "; ".join(pre_errs))

        os.rename(tmp_dir, final_dir)
        renamed = True
        _fsync_dir(parent_dir, strict=True)

        # Post-rename verification on final path
        post_errs = verify_cutover_snapshot(final_dir)
        if post_errs:
            raise RuntimeError("snapshot verification failed after rename: " + "; ".join(post_errs))
        return final_dir, manifest
    except BaseException:
        if not renamed:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def _inspect_guidepoint_tokens_file(
    path: Path,
    *,
    fallback_mtime: float | None = None,
) -> tuple[bool, float | None, str]:
    """Validate Guidepoint token JSON and return `(ok, freshness_ts, reason)`.

    Freshness uses numeric `obtained_at` when present and positive, falling back to
    `fallback_mtime` (snapshot manifest source mtime) or filesystem mtime.
    """
    if not path.is_file():
        return False, None, "file missing"
    try:
        raw_obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, None, f"unreadable or invalid JSON ({type(exc).__name__})"
    if not isinstance(raw_obj, dict):
        return False, None, "JSON root is not an object"
    if not str(raw_obj.get("access_token") or "").strip():
        return False, None, "missing access_token"
    if not str(raw_obj.get("refresh_token") or "").strip():
        return False, None, "missing refresh_token"
    obtained = raw_obj.get("obtained_at")
    if isinstance(obtained, (int, float)) and not isinstance(obtained, bool) and float(obtained) > 0:
        return True, float(obtained), "obtained_at"
    if isinstance(fallback_mtime, (int, float)) and not isinstance(fallback_mtime, bool):
        return True, float(fallback_mtime), "manifest_mtime"
    try:
        return True, float(path.stat().st_mtime), "mtime"
    except OSError:
        return True, None, "no_timestamp"


def find_latest_cutover_snapshot(repo_root: Path) -> tuple[Path | None, str | None]:
    """Locate the newest valid cutover snapshot under `<repo>/var/dalton/cutover-snapshots/`."""
    snapshots_root = repo_root.expanduser().resolve() / "var" / "dalton" / "cutover-snapshots"
    if not snapshots_root.is_dir():
        return None, f"snapshots directory does not exist: {snapshots_root}"

    candidates: list[tuple[str, str, Path]] = []
    for entry in snapshots_root.iterdir():
        if entry.name.startswith(".") or entry.name in SNAPSHOT_RESERVED_DIRS:
            continue
        if entry.is_symlink() or not entry.is_dir():
            continue
        m_path = entry / SNAPSHOT_MANIFEST_NAME
        if not m_path.is_file():
            continue
        created_at = ""
        try:
            data = json.loads(m_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                created_at = str(data.get("created_at") or "")
        except Exception:
            created_at = ""
        candidates.append((created_at, entry.name, entry))

    if not candidates:
        return None, f"no valid cutover snapshots found under {snapshots_root}"

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    failures: list[str] = []
    for _created_at, _name, cand in candidates:
        errs = verify_cutover_snapshot(cand)
        if not errs:
            return cand, None
        failures.append(f"{cand.name}: {'; '.join(errs)}")
    return None, (
        f"no valid cutover snapshots found under {snapshots_root} "
        f"(skipped {len(failures)} invalid snapshot(s): {' | '.join(failures[:3])})"
    )


def restore_cutover_snapshot(
    snapshot_dir: Path | str,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Atomically restore config/plists/journal/web release from a verified snapshot.

    Preserves newer valid `guidepoint_mcp_tokens.json` on disk (comparing `obtained_at`,
    falling back to mtime) so rotated refresh tokens are never overwritten; only restores
    the snapshot token file when the live file is missing, corrupted, or strictly older.
    Fails closed if the snapshot's `web/releases/current` target release directory is missing.
    """
    snap_path = Path(snapshot_dir).expanduser().resolve()
    errs = verify_cutover_snapshot(snap_path)
    if errs:
        raise RuntimeError(f"refusing to restore invalid snapshot {snap_path}: " + "; ".join(errs))

    manifest = json.loads((snap_path / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    files_meta: dict[str, dict[str, Any]] = manifest["files"]
    actions: list[str] = []

    restore_order = (
        "repo_env",
        "bridge_env",
        "research_host_env",
        "guidepoint_env",
        "plist_proxy",
        "plist_bridge",
        "plist_research_host",
        "guidepoint_tokens",
        "handoff_journal",
    )
    ordered_keys = [k for k in restore_order if k in files_meta] + [
        k for k in sorted(files_meta.keys()) if k not in restore_order
    ]

    for key in ordered_keys:
        info = files_meta[key]
        target = Path(str(info["source_path"])).expanduser()
        existed = bool(info["existed"])
        if key == "guidepoint_tokens":
            live_ok, live_ts, live_src = _inspect_guidepoint_tokens_file(target)
            snap_fp = snap_path / str(info["rel_path"])
            snap_mtime_meta = info.get("mtime")
            snap_ok, snap_ts, snap_src = (
                _inspect_guidepoint_tokens_file(
                    snap_fp,
                    fallback_mtime=float(snap_mtime_meta)
                    if isinstance(snap_mtime_meta, (int, float)) and not isinstance(snap_mtime_meta, bool)
                    else None,
                )
                if existed
                else (False, None, "absent")
            )
            if live_ok and (not snap_ok or snap_ts is None or live_ts is None or live_ts >= snap_ts):
                actions.append(
                    f"keep-live guidepoint_tokens: {target} "
                    f"(live {live_src}={live_ts} >= snapshot {snap_src}={snap_ts})"
                )
                continue
            if not existed:
                raise RuntimeError(
                    f"live guidepoint_tokens invalid ({live_src}) and snapshot has no fallback copy"
                )
            mode_str = str(info.get("mode") or "0600")
            try:
                mode_val = int(mode_str, 8)
            except ValueError:
                mode_val = 0o600
            reason = (
                f"fallback: live invalid/missing ({live_src})"
                if not live_ok
                else f"snapshot newer ({snap_src}={snap_ts} > live {live_src}={live_ts})"
            )
            actions.append(f"restore guidepoint_tokens: {snap_fp.name} -> {target} (mode {mode_val:04o}; {reason})")
            if not dry_run:
                _write_bytes_atomic(target, snap_fp.read_bytes(), mode=mode_val)
            continue

        if existed:
            src_fp = snap_path / str(info["rel_path"])
            mode_str = str(info.get("mode") or "0600")
            try:
                mode_val = int(mode_str, 8)
            except ValueError:
                mode_val = 0o600
            actions.append(f"restore {key}: {src_fp.name} -> {target} (mode {mode_val:04o})")
            if not dry_run:
                _write_bytes_atomic(target, src_fp.read_bytes(), mode=mode_val)
        else:
            if target.exists() or target.is_symlink():
                actions.append(f"remove {key}: {target} (absent in snapshot)")
                if not dry_run:
                    target.unlink()
                    _fsync_dir(target.parent, strict=True)
            else:
                actions.append(f"keep-absent {key}: {target}")

    web_info = manifest.get("web_releases_current")
    if (
        not isinstance(web_info, dict)
        or not web_info.get("path")
        or web_info.get("is_symlink") is not True
        or not str(web_info.get("link_target") or "").strip()
    ):
        raise RuntimeError(
            "cannot restore web_releases_current: snapshot manifest lacks valid symlink metadata"
        )
    target_errs = _verify_snapshot_frontend_target(snap_path, web_info)
    if target_errs:
        raise RuntimeError("cannot restore web_releases_current: " + "; ".join(target_errs))
    cur_path = _abs_no_deref(str(web_info["path"]))
    link_target = str(web_info["link_target"]).strip()
    actions.append(f"restore web_releases_current: {cur_path} -> {link_target}")
    if not dry_run:
        cur_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_link = cur_path.parent / f".current.restore.{os.getpid()}.{secrets.token_hex(4)}"
        try:
            os.symlink(link_target, tmp_link)
            os.replace(tmp_link, cur_path)
            _fsync_dir(cur_path.parent, strict=True)
        except BaseException:
            try:
                if tmp_link.is_symlink():
                    tmp_link.unlink()
            except OSError:
                pass
            raise
    return actions


def cmd_snapshot_create(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    home_dir = Path(args.home).expanduser().resolve() if args.home else Path.home().resolve()
    try:
        final_dir, manifest = create_cutover_snapshot(
            repo_root=repo_root,
            home_dir=home_dir,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            label=str(args.label or ""),
            plist_dir=Path(args.plist_dir) if args.plist_dir else None,
            repo_env=Path(args.repo_env) if args.repo_env else None,
            bridge_env=Path(args.bridge_env) if args.bridge_env else None,
            research_host_env=Path(args.research_host_env) if args.research_host_env else None,
            guidepoint_env=Path(args.guidepoint_env) if args.guidepoint_env else None,
            tokens_file=Path(args.tokens_file) if args.tokens_file else None,
            journal_file=Path(args.journal_file) if args.journal_file else None,
            web_releases_current=Path(args.web_releases_current) if args.web_releases_current else None,
        )
    except Exception as exc:
        print(f"ERROR: snapshot creation failed: {exc}", file=sys.stderr)
        return 1
    if args.print_path:
        print(str(final_dir))
        return 0
    if args.json:
        print(json.dumps({"snapshot_dir": str(final_dir), "manifest": manifest}, ensure_ascii=False, indent=2))
        return 0
    saved = [k for k, v in manifest["files"].items() if v.get("existed")]
    absent = [k for k, v in manifest["files"].items() if not v.get("existed")]
    print(f"SNAPSHOT_OK: {final_dir}")
    print(f"  saved_files ({len(saved)}): {', '.join(saved) if saved else 'none'}")
    print(f"  absent_files ({len(absent)}): {', '.join(absent) if absent else 'none'}")
    web_info = manifest.get("web_releases_current") or {}
    if web_info.get("is_symlink"):
        print(f"  web_releases_current: {web_info.get('link_target')}")
    else:
        print(f"  web_releases_current: exists={web_info.get('exists')}")
    print(f"  manifest: {final_dir / SNAPSHOT_MANIFEST_NAME} (SHA-256 verified)")
    return 0


def cmd_snapshot_verify(args: argparse.Namespace) -> int:
    errs = verify_cutover_snapshot(Path(args.snapshot_dir))
    if errs:
        for e in errs:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if args.json:
        snap_path = Path(args.snapshot_dir).expanduser().resolve()
        manifest = json.loads((snap_path / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        print(json.dumps({"ok": True, "snapshot_dir": str(snap_path), "manifest": manifest}, ensure_ascii=False, indent=2))
    else:
        print(f"SNAPSHOT_VERIFY_OK: {Path(args.snapshot_dir).expanduser().resolve()}")
    return 0


def cmd_snapshot_latest(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    latest, err = find_latest_cutover_snapshot(repo_root)
    if err or latest is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    print(str(latest))
    return 0


def cmd_snapshot_restore(args: argparse.Namespace) -> int:
    try:
        actions = restore_cutover_snapshot(
            Path(args.snapshot_dir),
            dry_run=bool(args.dry_run),
        )
    except Exception as exc:
        print(f"ERROR: snapshot restore failed: {exc}", file=sys.stderr)
        return 1
    prefix = "[DRY-RUN] " if args.dry_run else ""
    for line in actions:
        print(f"{prefix}{line}")
    return 0


def cmd_gate_status(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    state_file = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else (repo_root / DEFAULT_ADMISSION_REL_PATH)
    )
    mg = _import_maintenance_gate(repo_root)
    st = mg.read_maintenance_state(state_file)
    print(json.dumps({**st.to_dict(), "exists": st.exists, "state_file": str(state_file)}, ensure_ascii=False))
    return 0


def cmd_gate_set(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    state_file = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else (repo_root / DEFAULT_ADMISSION_REL_PATH)
    )
    mg = _import_maintenance_gate(repo_root)
    paused_bool = args.paused == "true"
    host_direct_bool = (
        None if args.pause_host_direct is None else (args.pause_host_direct == "true")
    )
    no_db_lock = bool(args.no_db_lock) or str(os.environ.get("DALTON_MAINTENANCE_NO_DB_LOCK") or "") == "1"
    try:
        if no_db_lock:
            current = mg.read_maintenance_state(state_file)
            nxt = mg._build_next_state(
                current,
                paused=paused_bool,
                allow_workspace_ids=args.allow_workspace_id,
                pause_host_direct=host_direct_bool,
                reason=args.reason,
                operation=args.operation,
            )
            verified = mg.write_maintenance_state_atomic(nxt, state_file=state_file)
        else:
            verified = mg.update_maintenance_state_under_db_lock(
                paused=paused_bool,
                allow_workspace_ids=args.allow_workspace_id,
                pause_host_direct=host_direct_bool,
                reason=args.reason,
                operation=args.operation,
                state_file=state_file,
                dsn=args.dsn,
                lock_timeout_s=getattr(args, "lock_timeout_s", None),
            )
    except Exception as exc:
        print(f"ERROR: failed to update maintenance admission gate: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(verified.to_dict(), ensure_ascii=False))
    return 0


def cmd_gate_verify(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    state_file = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else (repo_root / DEFAULT_ADMISSION_REL_PATH)
    )
    mg = _import_maintenance_gate(repo_root)
    st = mg.read_maintenance_state(state_file)
    expected_rev = int(args.expected_revision) if args.expected_revision is not None else int(st.revision)
    expected_paused = args.expected_paused == "true"

    repo_env_path = (
        Path(args.repo_env).expanduser().resolve()
        if args.repo_env
        else (repo_root / ".env")
    )
    repo_vals = _parse_simple_env_file(repo_env_path)
    if args.repo_env:
        svc_tok = (
            str(repo_vals.get("INTERNAL_SERVICE_TOKEN") or "").strip()
            or str(os.environ.get("INTERNAL_SERVICE_TOKEN") or "").strip()
        )
    else:
        svc_tok = (
            str(os.environ.get("INTERNAL_SERVICE_TOKEN") or "").strip()
            or str(repo_vals.get("INTERNAL_SERVICE_TOKEN") or "").strip()
        )
    raw_fb = getattr(args, "fallback_repo_env", None)
    fb_paths: list[str] = [raw_fb] if isinstance(raw_fb, str) else list(raw_fb or [])
    fallback_svc_toks: list[str] = []
    for fb_item in fb_paths:
        if not fb_item:
            continue
        fb_env_path = Path(fb_item).expanduser().resolve()
        fb_vals = _parse_simple_env_file(fb_env_path)
        fb_tok = str(fb_vals.get("INTERNAL_SERVICE_TOKEN") or "").strip()
        if fb_tok and fb_tok != svc_tok and fb_tok not in fallback_svc_toks:
            fallback_svc_toks.append(fb_tok)
    docker_bin = os.environ.get("DOCKER_BIN", "docker")

    def _docker_runner(d_args: list[str], timeout_s: float) -> tuple[int, str, str]:
        return _run_cmd([docker_bin, *d_args], timeout_s=timeout_s)

    saw_unauthorized = False

    def _http_requester(
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> tuple[int | None, dict[str, Any] | None, str]:
        nonlocal saw_unauthorized
        code, parsed, err = _http_request(
            method,
            url,
            headers=headers,
            body=body,
            timeout_s=timeout_s,
        )
        attempt_saw_401 = False
        if code == 401:
            attempt_saw_401 = True
            primary_tok = str(headers.get("X-Service-Token") or "").strip()
            for fb_tok in fallback_svc_toks:
                if fb_tok == primary_tok:
                    continue
                fb_headers = dict(headers)
                fb_headers["X-Service-Token"] = fb_tok
                code, parsed, err = _http_request(
                    method,
                    url,
                    headers=fb_headers,
                    body=body,
                    timeout_s=timeout_s,
                )
                if code != 401:
                    if code is not None:
                        attempt_saw_401 = False
                    break
        if attempt_saw_401:
            saw_unauthorized = True
        return code, (parsed if isinstance(parsed, dict) else None), (err or "")

    res = mg.verify_container_maintenance_gate(
        expected_revision=expected_rev,
        expected_paused=expected_paused,
        backend_url=args.backend_url,
        service_token=svc_tok,
        container_name=args.container_name,
        deadline_s=float(args.deadline_s),
        poll_interval_s=float(args.poll_interval_s),
        docker_runner=_docker_runner,
        http_requester=_http_requester,
    )
    out_dict = res.to_dict()
    if res.status == "verified":
        print(json.dumps(out_dict, ensure_ascii=False))
        return 0
    if saw_unauthorized or res.probe_status_code == 401:
        if out_dict.get("status") == "unavailable":
            out_dict["status"] = "mismatch"
            out_dict["container_available"] = True
            orig_reason = str(out_dict.get("reason") or "")
            out_dict["reason"] = f"probe_unauthorized_401:{orig_reason}" if orig_reason else "probe_unauthorized_401"
        print(json.dumps(out_dict, ensure_ascii=False))
        return 1
    print(json.dumps(out_dict, ensure_ascii=False))
    if res.status == "unavailable" and args.allow_unavailable:
        return 0
    return 1


def build_legacy_bridge_probe_rpc(
    route: str,
    operation: str,
    query: str,
    *,
    req_id: int = 1,
    limit: int = 1,
) -> dict[str, Any]:
    """Construct a JSON-RPC `tools/call` payload matching the legacy bridge route contract."""
    return {
        "jsonrpc": "2.0",
        "id": int(req_id),
        "method": "tools/call",
        "params": {
            "name": "research",
            "arguments": {
                "route": route,
                "operation": operation,
                "query": query,
                "limit": int(limit),
            },
        },
    }


def _decode_mcp_json_or_sse(raw_body: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Decode MCP JSON-RPC payload from either direct JSON or `text/event-stream` SSE.

    Matches Guidepoint client JSON/SSE decoding (`host_connectors/guidepoint.py` / `gp_search.py`),
    supporting direct JSON objects as well as single- or multi-event SSE `data:` streams.
    """
    if isinstance(raw_body, dict):
        return raw_body, None
    if not isinstance(raw_body, str):
        return None, f"expected JSON object or SSE string response, got {type(raw_body).__name__}"
    stripped = raw_body.lstrip()
    if not stripped:
        return None, "empty response body"
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError as exc:
            return None, f"invalid JSON response: {exc}"
        if isinstance(obj, dict):
            return obj, None
        return None, f"expected JSON object response, got {type(obj).__name__}"

    # SSE decoding: collect event-grouped multi-line `data:` blocks and individual `data:` lines.
    event_blocks: list[str] = []
    cur_data: list[str] = []
    single_data_lines: list[str] = []
    for raw_line in raw_body.splitlines():
        line = raw_line.rstrip("\r")
        if line == "":
            if cur_data:
                event_blocks.append("\n".join(cur_data))
                cur_data = []
            continue
        if line.startswith("data:"):
            payload_part = line[5:].lstrip(" ") if len(line) > 5 and line[5] == " " else line[5:].strip()
            cur_data.append(payload_part)
            single_data_lines.append(payload_part.strip())
    if cur_data:
        event_blocks.append("\n".join(cur_data))

    for candidate in reversed(event_blocks):
        text = candidate.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj, None
        except ValueError:
            continue

    for candidate in reversed(single_data_lines):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj, None
        except ValueError:
            continue

    return None, "SSE response contained no valid JSON-RPC object"


def _extract_mcp_payload(parsed: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Decode JSON-RPC MCP tools/call response payload (JSON or SSE) and report protocol/tool errors."""
    msg, dec_err = _decode_mcp_json_or_sse(parsed)
    if dec_err or msg is None:
        return None, dec_err or "invalid MCP response"
    if "error" in msg and msg["error"]:
        return None, f"jsonrpc error: {msg['error']}"
    result = msg.get("result")
    if not isinstance(result, dict):
        return None, "missing result object in JSON-RPC response"
    if result.get("isError") is True:
        content = result.get("content")
        snippet = ""
        if isinstance(content, list) and content and isinstance(content[0], dict):
            snippet = str(content[0].get("text") or "")[:200]
        return None, f"tool returned isError=true ({snippet})"
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    inner = json.loads(text)
                    if isinstance(inner, dict):
                        return inner, None
                    if isinstance(inner, list):
                        return {"data": inner}, None
                except ValueError:
                    continue
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured, None
    return result, None


def _call_legacy_proxy_guidepoint_mcp(
    base_url: str,
    *,
    query: str = "cloud",
    size: int = 1,
    timeout_s: float = 15.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """Call `:8943/mcp` `search_library` with the same MCP handshake & JSON/SSE decoding as `gp_search.py`.

    1. `initialize` (`protocolVersion=2025-03-26`) -> capture `Mcp-Session-Id` header and verify JSON/SSE `result`.
    2. Best-effort `notifications/initialized` with `Mcp-Session-Id`.
    3. `tools/call` `search_library` with `Mcp-Session-Id`, `Accept: application/json, text/event-stream`,
       and `X-Dalton-Request-Timeout-Ms`.
    """
    mcp_url = f"{base_url.rstrip('/')}/mcp"
    deadline_mono = time.monotonic() + max(0.5, float(timeout_s))

    def _rpc(payload: dict[str, Any], session_id: str | None = None) -> tuple[int | None, Any, str | None, str | None]:
        rem_s = max(0.1, deadline_mono - time.monotonic())
        hdrs: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Dalton-Request-Timeout-Ms": str(max(100, int(rem_s * 1000))),
        }
        if session_id:
            hdrs["Mcp-Session-Id"] = session_id
        code, body, resp_hdrs, err = _http_request_with_headers(
            "POST",
            mcp_url,
            headers=hdrs,
            body=json.dumps(payload).encode("utf-8"),
            timeout_s=rem_s,
            capture_headers=True,
        )
        sid = resp_hdrs.get("mcp-session-id") or session_id
        return code, body, sid, err

    init_code, init_body, sid, init_err = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "dalton-ops-probe", "version": "1.0"},
            },
        }
    )
    if init_err or init_code != 200:
        return None, f"proxy :8943/mcp initialize -> HTTP {init_code} ({init_err or 'error'})"
    init_msg, init_dec_err = _decode_mcp_json_or_sse(init_body)
    if init_dec_err or init_msg is None:
        return None, f"proxy :8943/mcp initialize decode failed: {init_dec_err}"
    if init_msg.get("error"):
        return None, f"proxy :8943/mcp initialize error: {init_msg['error']}"
    if not isinstance(init_msg.get("result"), dict):
        return None, "proxy :8943/mcp initialize missing result object"

    # Best-effort initialized notification (matches gp_search.py:174-177)
    try:
        _rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    except Exception:
        pass

    rem_call = max(0.1, deadline_mono - time.monotonic())
    call_timeout = max(1, int(rem_call)) if rem_call >= 1.0 else rem_call
    call_code, call_body, _sid2, call_err = _rpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_library",
                "arguments": {"query": query, "size": int(size), "timeout": call_timeout},
            },
        },
        sid,
    )
    if call_err or call_code != 200:
        return None, f"proxy :8943/mcp search_library -> HTTP {call_code} ({call_err or 'error'})"
    inner, m_err = _extract_mcp_payload(call_body)
    if m_err or inner is None:
        return None, f"proxy :8943/mcp search_library failed: {m_err}"
    if inner.get("ok") is False:
        return None, f"proxy :8943/mcp search_library error: {inner.get('error')}"
    return inner, None


def _run_probe_with_retries(
    name: str,
    fn: Callable[[], tuple[bool, str]],
    *,
    retries: int,
    retry_delay: float,
) -> dict[str, str]:
    attempts = max(1, int(retries))
    last_detail = ""
    for attempt in range(1, attempts + 1):
        ok, detail = fn()
        if ok:
            return {"probe": name, "status": "ok", "detail": detail}
        last_detail = detail
        if attempt < attempts and retry_delay > 0:
            time.sleep(retry_delay)
    return {"probe": name, "status": "fail", "detail": last_detail}


def cmd_probe_services(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    home_dir = Path(args.home).expanduser().resolve() if args.home else Path.home().resolve()
    bridge_env_path = (
        Path(args.bridge_env).expanduser().resolve()
        if args.bridge_env
        else (
            (home_dir / ".config" / "dalton" / "researchdata-bridge.env")
            if args.home
            else (
                Path(os.environ["DALTON_BRIDGE_ENV_FILE"]).expanduser().resolve()
                if str(os.environ.get("DALTON_BRIDGE_ENV_FILE") or "").strip()
                else (home_dir / ".config" / "dalton" / "researchdata-bridge.env")
            )
        )
    )
    rh_env_path = (
        Path(args.research_host_env).expanduser().resolve()
        if args.research_host_env
        else (
            (home_dir / ".config" / "dalton" / "research-host.env")
            if args.home
            else (
                Path(os.environ["DALTON_RESEARCH_HOST_ENV_FILE"]).expanduser().resolve()
                if str(os.environ.get("DALTON_RESEARCH_HOST_ENV_FILE") or "").strip()
                else (home_dir / ".config" / "dalton" / "research-host.env")
            )
        )
    )
    repo_env_path = (
        Path(args.repo_env).expanduser().resolve()
        if args.repo_env
        else (
            (repo_root / ".env")
            if args.repo
            else (
                Path(os.environ["DALTON_REPO_ENV_FILE"]).expanduser().resolve()
                if str(os.environ.get("DALTON_REPO_ENV_FILE") or "").strip()
                else (repo_root / ".env")
            )
        )
    )

    bridge_vals = _parse_simple_env_file(bridge_env_path)
    rh_vals = _parse_simple_env_file(rh_env_path)
    repo_vals = _parse_simple_env_file(repo_env_path)

    bridge_tok = (
        str(bridge_vals.get("DALTON_BRIDGE_TOKEN") or "").strip()
        or str(repo_vals.get("DALTON_BRIDGE_TOKEN") or "").strip()
    )
    rh_tok = (
        str(rh_vals.get("DALTON_RESEARCH_HOST_TOKEN") or "").strip()
        or str(repo_vals.get("DALTON_RESEARCH_HOST_TOKEN") or "").strip()
    )

    timeout_s = float(args.timeout)
    retries = int(args.retries)
    retry_delay = float(args.retry_delay)
    query = str(args.query or "cloud")
    proxy_base = args.proxy_url.rstrip("/")
    bridge_base = args.bridge_url.rstrip("/")
    rh_base = args.research_host_url.rstrip("/")
    backend_base = args.backend_url.rstrip("/")

    results: list[dict[str, str]] = []

    def _probe_backend_health() -> tuple[bool, str]:
        code, _body, err = _http_request("GET", f"{backend_base}/health", timeout_s=timeout_s)
        if err or code != 200:
            return False, f"GET {backend_base}/health -> {code} ({err or 'unexpected status'})"
        return True, f"backend /health returned 200"

    if args.target == "legacy":
        def _probe_proxy_healthz() -> tuple[bool, str]:
            code, body, err = _http_request("GET", f"{proxy_base}/healthz", timeout_s=timeout_s)
            if err or code != 200 or not isinstance(body, dict):
                return False, f"GET {proxy_base}/healthz -> {code} ({err or 'bad response'})"
            if body.get("owner_lock_held") is not True:
                return False, f"proxy owner_lock_held={body.get('owner_lock_held')!r} (expected True)"
            if body.get("draining") is True:
                return False, "proxy draining=True (expected False)"
            return True, "proxy :8943 healthy and holds .guidepoint.owner.lock"

        def _probe_bridge_healthz() -> tuple[bool, str]:
            code, body, err = _http_request("GET", f"{bridge_base}/healthz", timeout_s=timeout_s)
            if err or code != 200 or not isinstance(body, dict):
                return False, f"GET {bridge_base}/healthz -> {code} ({err or 'bad response'})"
            if body.get("ok") is False:
                return False, f"bridge /healthz ok=False"
            return True, "legacy bridge :8960 /healthz returned 200"

        def _probe_legacy_wiki_search() -> tuple[bool, str]:
            if not bridge_tok:
                return False, f"missing DALTON_BRIDGE_TOKEN in {bridge_env_path}"
            rpc = json.dumps(
                build_legacy_bridge_probe_rpc("company-wiki", "search", query, req_id=1, limit=1)
            ).encode("utf-8")
            code, body, err = _http_request(
                "POST",
                f"{bridge_base}/mcp",
                headers={"Authorization": f"Bearer {bridge_tok}", "Content-Type": "application/json"},
                body=rpc,
                timeout_s=timeout_s,
            )
            if err or code != 200:
                return False, f"bridge company-wiki search -> HTTP {code} ({err or 'error'})"
            inner, m_err = _extract_mcp_payload(body)
            if m_err or inner is None:
                return False, f"bridge company-wiki search failed: {m_err}"
            if inner.get("ok") is False or inner.get("error_code") == "route_migrated":
                return False, f"bridge company-wiki search returned error: {inner.get('error_code') or inner.get('error')}"
            return True, "legacy bridge company-wiki search succeeded"

        def _probe_legacy_guidepoint() -> tuple[bool, str]:
            if not bridge_tok:
                return False, f"missing DALTON_BRIDGE_TOKEN in {bridge_env_path}"
            rpc = json.dumps(
                build_legacy_bridge_probe_rpc("guidepoint", "search_library", query, req_id=2, limit=1)
            ).encode("utf-8")
            code, body, err = _http_request(
                "POST",
                f"{bridge_base}/mcp",
                headers={"Authorization": f"Bearer {bridge_tok}", "Content-Type": "application/json"},
                body=rpc,
                timeout_s=timeout_s,
            )
            if err or code != 200:
                return False, f"bridge guidepoint search_library -> HTTP {code} ({err or 'error'})"
            inner, m_err = _extract_mcp_payload(body)
            if m_err or inner is None:
                return False, f"bridge guidepoint search_library failed: {m_err}"
            if inner.get("ok") is False or inner.get("error_code") == "route_migrated":
                return False, f"bridge guidepoint search_library returned error: {inner.get('error_code') or inner.get('error')}"
            return True, "legacy bridge guidepoint search_library succeeded"

        def _probe_proxy_direct_guidepoint() -> tuple[bool, str]:
            _inner, p_err = _call_legacy_proxy_guidepoint_mcp(
                proxy_base,
                query=query,
                size=1,
                timeout_s=timeout_s,
            )
            if p_err:
                return False, p_err
            return True, "proxy :8943 direct guidepoint search_library succeeded"

        probes: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
            ("proxy_healthz", _probe_proxy_healthz),
            ("bridge_healthz", _probe_bridge_healthz),
            ("wiki_search", _probe_legacy_wiki_search),
            ("guidepoint_search_library", _probe_legacy_guidepoint),
            ("proxy_guidepoint_search_library", _probe_proxy_direct_guidepoint),
        ]
        if not args.skip_backend:
            probes.append(("backend_health", _probe_backend_health))
    else:
        def _probe_rh_healthz() -> tuple[bool, str]:
            code, body, err = _http_request("GET", f"{rh_base}/healthz", timeout_s=timeout_s)
            if err or code != 200 or not isinstance(body, dict):
                return False, f"GET {rh_base}/healthz -> {code} ({err or 'bad response'})"
            if body.get("ok") is not True:
                return False, f"research-host /healthz ok={body.get('ok')!r} (expected True)"
            probes_block = body.get("probes")
            if not isinstance(probes_block, dict):
                return False, "research-host /healthz missing probes object"
            gp_block = probes_block.get("guidepoint")
            if not isinstance(gp_block, dict):
                return False, "research-host /healthz missing probes.guidepoint object"
            if gp_block.get("owner_lock_held") is not True:
                return (
                    False,
                    f"research-host probes.guidepoint.owner_lock_held={gp_block.get('owner_lock_held')!r} (expected True)",
                )
            if gp_block.get("standby") is True:
                return False, "research-host probes.guidepoint.standby=True (expected False)"
            return True, "research-host :8951 healthy and probes.guidepoint.owner_lock_held=True"

        def _probe_rh_wiki_search() -> tuple[bool, str]:
            if not rh_tok:
                return False, f"missing DALTON_RESEARCH_HOST_TOKEN in {rh_env_path}"
            rpc = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search",
                        "arguments": {"query": query, "limit": 1},
                    },
                }
            ).encode("utf-8")
            code, body, err = _http_request(
                "POST",
                f"{rh_base}/mcp/company-wiki",
                headers={"Authorization": f"Bearer {rh_tok}", "Content-Type": "application/json"},
                body=rpc,
                timeout_s=timeout_s,
            )
            if err or code != 200:
                return False, f"research-host /mcp/company-wiki search -> HTTP {code} ({err or 'error'})"
            inner, m_err = _extract_mcp_payload(body)
            if m_err or inner is None:
                return False, f"research-host company-wiki search failed: {m_err}"
            if inner.get("ok") is False:
                return False, f"research-host company-wiki search error: {inner.get('error')}"
            return True, "research-host /mcp/company-wiki search succeeded"

        def _probe_rh_guidepoint() -> tuple[bool, str]:
            if not rh_tok:
                return False, f"missing DALTON_RESEARCH_HOST_TOKEN in {rh_env_path}"
            rpc = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "search_library",
                        "arguments": {"query": query, "limit": 1},
                    },
                }
            ).encode("utf-8")
            code, body, err = _http_request(
                "POST",
                f"{rh_base}/mcp/guidepoint",
                headers={"Authorization": f"Bearer {rh_tok}", "Content-Type": "application/json"},
                body=rpc,
                timeout_s=timeout_s,
            )
            if err or code != 200:
                return False, f"research-host /mcp/guidepoint search_library -> HTTP {code} ({err or 'error'})"
            inner, m_err = _extract_mcp_payload(body)
            if m_err or inner is None:
                return False, f"research-host guidepoint search_library failed: {m_err}"
            if inner.get("ok") is False:
                return False, f"research-host guidepoint search_library error: {inner.get('error')}"
            return True, "research-host /mcp/guidepoint search_library succeeded"

        def _probe_legacy_route_closed() -> tuple[bool, str]:
            if not bridge_tok:
                return False, f"missing DALTON_BRIDGE_TOKEN in {bridge_env_path}"
            rpc = json.dumps(
                build_legacy_bridge_probe_rpc("company-wiki", "search", query, req_id=3, limit=1)
            ).encode("utf-8")
            code, body, err = _http_request(
                "POST",
                f"{bridge_base}/mcp",
                headers={"Authorization": f"Bearer {bridge_tok}", "Content-Type": "application/json"},
                body=rpc,
                timeout_s=timeout_s,
            )
            if err or code is None:
                return False, f"bridge migrated-route check failed: {err}"
            raw_text = json.dumps(body, ensure_ascii=False) if isinstance(body, (dict, list)) else str(body)
            if "route_migrated" in raw_text:
                return True, "legacy bridge :8960 rejected migrated route with route_migrated"
            return False, f"legacy bridge :8960 did not return route_migrated (HTTP {code})"

        probes = [
            ("research_host_healthz", _probe_rh_healthz),
            ("company_wiki_search", _probe_rh_wiki_search),
            ("guidepoint_search_library", _probe_rh_guidepoint),
            ("legacy_route_migrated", _probe_legacy_route_closed),
        ]
        if not args.skip_backend:
            probes.append(("backend_health", _probe_backend_health))

    failed = False
    for name, fn in probes:
        res = _run_probe_with_retries(name, fn, retries=retries, retry_delay=retry_delay)
        results.append(res)
        if res["status"] != "ok":
            failed = True

    if args.json:
        print(json.dumps({"ok": not failed, "target": args.target, "probes": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            tag = r["status"].upper()
            stream = sys.stderr if r["status"] != "ok" else sys.stdout
            print(f"[{tag}] {r['probe']}: {r['detail']}", file=stream)
    return 1 if failed else 0


def _check_healthz_owner_lock(target: str, body: Any) -> tuple[bool, str]:
    """Validate Guidepoint lock ownership from a `/healthz` JSON payload."""
    if not isinstance(body, dict):
        return False, "healthz body is not a JSON object"
    if body.get("ok") is not True:
        return False, f"healthz ok={body.get('ok')!r} (expected True)"
    if target == "research-host":
        probes_block = body.get("probes")
        if not isinstance(probes_block, dict):
            return False, "healthz missing probes object"
        gp_block = probes_block.get("guidepoint")
        if not isinstance(gp_block, dict):
            return False, "healthz missing probes.guidepoint object"
        if gp_block.get("owner_lock_held") is not True:
            return (
                False,
                f"probes.guidepoint.owner_lock_held={gp_block.get('owner_lock_held')!r} (expected True)",
            )
        if gp_block.get("standby") is True:
            return False, "probes.guidepoint.standby=True (expected False)"
        return True, "probes.guidepoint.owner_lock_held=True"
    if body.get("owner_lock_held") is not True:
        return False, f"owner_lock_held={body.get('owner_lock_held')!r} (expected True)"
    if body.get("draining") is True:
        return False, "draining=True (expected False)"
    if body.get("standby") is True:
        return False, "standby=True (expected False)"
    return True, "owner_lock_held=True"


def cmd_wait_owner(args: argparse.Namespace) -> int:
    """Bounded poll on `/healthz` until the target service is healthy and holds `.guidepoint.owner.lock`."""
    target = str(args.target)
    default_url = "http://127.0.0.1:8951" if target == "research-host" else "http://127.0.0.1:8943"
    base_url = str(args.url or default_url).rstrip("/")
    timeout_s = max(0.5, float(args.timeout))
    poll_s = max(0.05, float(args.poll_interval))
    req_timeout = max(1.0, min(5.0, timeout_s))
    deadline = time.monotonic() + timeout_s
    last_reason = "no response received"

    while True:
        code, body, err = _http_request("GET", f"{base_url}/healthz", timeout_s=req_timeout)
        if err or code != 200:
            last_reason = f"GET {base_url}/healthz -> {code} ({err or 'unexpected status'})"
        else:
            ok, reason = _check_healthz_owner_lock(target, body)
            if ok:
                print(json.dumps(body, ensure_ascii=False, indent=2))
                return 0
            last_reason = reason
        if time.monotonic() >= deadline:
            print(
                f"ERROR: wait-owner ({target}) timed out after {timeout_s:.1f}s: {last_reason}",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))


def cmd_wait_health(args: argparse.Namespace) -> int:
    """Bounded poll on a health endpoint until HTTP 200 (and not explicitly `ok: false`)."""
    url = str(args.url).strip()
    timeout_s = max(0.5, float(args.timeout))
    poll_s = max(0.05, float(args.poll_interval))
    req_timeout = max(1.0, min(5.0, timeout_s))
    deadline = time.monotonic() + timeout_s
    last_reason = "no response received"

    while True:
        rem = max(0.2, deadline - time.monotonic())
        code, body, err = _http_request("GET", url, timeout_s=min(req_timeout, rem))
        if err or code != 200:
            last_reason = f"GET {url} -> {code} ({err or 'unexpected status'})"
        elif isinstance(body, dict) and body.get("ok") is False:
            last_reason = f"GET {url} -> HTTP 200 with ok=False"
        else:
            if isinstance(body, (dict, list)):
                print(json.dumps(body, ensure_ascii=False, indent=2))
            else:
                print(str(body))
            return 0
        if time.monotonic() >= deadline:
            print(
                f"ERROR: wait-health ({url}) timed out after {timeout_s:.1f}s: {last_reason}",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))


def _resolve_tokens_path_for_ops(args: argparse.Namespace, repo_root: Path, home_dir: Path) -> Path:
    if getattr(args, "tokens_file", None):
        return Path(args.tokens_file).expanduser().resolve()
    sources = _resolve_snapshot_sources(
        repo_root=repo_root,
        home_dir=home_dir,
        plist_dir=Path(args.plist_dir) if getattr(args, "plist_dir", None) else None,
        repo_env=Path(args.repo_env) if getattr(args, "repo_env", None) else None,
        bridge_env=Path(args.bridge_env) if getattr(args, "bridge_env", None) else None,
        research_host_env=Path(args.research_host_env) if getattr(args, "research_host_env", None) else None,
        guidepoint_env=Path(args.guidepoint_env) if getattr(args, "guidepoint_env", None) else None,
    )
    return sources["guidepoint_tokens"]


def _resolve_legacy_proxy_ops_context(
    args: argparse.Namespace,
    repo_root: Path,
    home_dir: Path,
) -> tuple[Path, Path, Path, str]:
    """Resolve `(tokens_path, gp_env_path, proxy_plist, admin_token)` using `resolve_legacy_proxy_effective_paths`."""
    eff_plist_dir = (
        Path(args.plist_dir).expanduser().resolve()
        if getattr(args, "plist_dir", None)
        else (
            Path(os.environ["DALTON_LAUNCH_AGENTS_DIR"]).expanduser().resolve()
            if str(os.environ.get("DALTON_LAUNCH_AGENTS_DIR") or "").strip()
            else (home_dir / "Library" / "LaunchAgents")
        )
    )
    eff_bridge_env = (
        Path(args.bridge_env).expanduser().resolve()
        if getattr(args, "bridge_env", None)
        else (
            Path(os.environ["DALTON_BRIDGE_ENV_FILE"]).expanduser().resolve()
            if str(os.environ.get("DALTON_BRIDGE_ENV_FILE") or "").strip()
            else (home_dir / ".config" / "dalton" / "researchdata-bridge.env")
        )
    )
    proxy_plist = eff_plist_dir / "com.dalton.guidepoint-mcp-proxy.plist"
    explicit_gp_env = (
        Path(args.guidepoint_env).expanduser().resolve()
        if getattr(args, "guidepoint_env", None)
        else None
    )
    explicit_tokens = (
        Path(args.tokens_file).expanduser().resolve()
        if getattr(args, "tokens_file", None)
        else None
    )

    try:
        bse_mod = _import_bridge_subprocess_env(repo_root)
        gl_mod = _import_guidepoint_lock(repo_root)
        bridge_vals = _parse_simple_env_file(eff_bridge_env)
        resolved_proxy = bse_mod.resolve_legacy_proxy_effective_paths(
            repo_root=repo_root,
            home_dir=home_dir,
            existing_proxy_plist_path=proxy_plist,
            bridge_env_vals=bridge_vals,
            guidepoint_env_path=explicit_gp_env,
        )
        eff_proxy_env = dict(resolved_proxy.get("effective_env") or {"HOME": str(home_dir)})
        gp_env_path = (
            explicit_gp_env
            if explicit_gp_env is not None
            else Path(str(resolved_proxy["guidepoint_env_file"])).expanduser().resolve()
        )
        tokens_path = (
            explicit_tokens
            if explicit_tokens is not None
            else gl_mod.resolve_guidepoint_tokens_file(environ=eff_proxy_env)
        )
        admin_tok = str(eff_proxy_env.get("GUIDEPOINT_PROXY_ADMIN_TOKEN") or "").strip()
        return tokens_path, gp_env_path, proxy_plist, admin_tok
    except ImportError:
        if explicit_gp_env is None or explicit_tokens is None:
            raise
        gp_vals = _parse_simple_env_file(explicit_gp_env)
        admin_tok = str(gp_vals.get("GUIDEPOINT_PROXY_ADMIN_TOKEN") or "").strip()
        if not admin_tok and proxy_plist.is_file():
            p_ev, _p_err = _load_plist_env_vars_strict(proxy_plist)
            if p_ev:
                admin_tok = str(p_ev.get("GUIDEPOINT_PROXY_ADMIN_TOKEN") or "").strip()
        return explicit_tokens, explicit_gp_env, proxy_plist, admin_tok


def _matches_refresher_command(target: str, cmdline: str) -> bool:
    """Return True when a `ps` command line corresponds to the live refresher process."""
    text = cmdline.strip()
    if not text:
        return False
    # Exclude test runners and ops helper invocations referencing script names in arguments.
    if "pytest" in text or "preflight.py" in text or "rollback_research_host.sh" in text:
        return False
    if target == "legacy":
        return bool(
            re.search(r"(?:^|[\s/])guidepoint_mcp_proxy(?:_run\.sh|\.py)(?:\s|$)", text)
        )
    return bool(
        re.search(
            r"(?:^|\s)(?:-m\s+src\.dalton\.research_host|[^\s]*src/dalton/research_host(?:/__main__\.py)?)(?:\s|$)",
            text,
        )
    )


def _matches_bridge_command(cmdline: str) -> bool:
    """Return True when a `ps` command line corresponds to the live legacy bridge process."""
    text = cmdline.strip()
    if not text:
        return False
    if "pytest" in text or "preflight.py" in text or "rollback_research_host.sh" in text:
        return False
    return bool(re.search(r"(?:^|[\s/])researchdata_bridge\.py(?:\s|$)", text))


def _is_launchd_service_stopped(
    label: str,
    cmd_matcher: Callable[[str], bool],
    target_desc: str,
) -> tuple[bool, str]:
    """Confirm via `launchctl print`, `launchctl list`, AND `ps` that service is unloaded & exited.

    Fail-closed: returns `(False, detail)` if the service is still registered in launchd
    (including `KeepAlive` services waiting to restart with no PID yet), if a matching
    process exists in `ps`, or if any verification command fails unexpectedly.
    """
    domain = str(os.environ.get("DALTON_LAUNCHD_DOMAIN") or "").strip()
    if not domain:
        uid_val = str(os.environ.get("DALTON_LAUNCHD_UID") or "").strip() or str(os.getuid())
        domain = f"gui/{uid_val}"
    lc_bin = str(os.environ.get("LAUNCHCTL_BIN") or "launchctl").strip() or "launchctl"
    ps_bin = str(os.environ.get("PS_BIN") or "ps").strip() or "ps"

    rc_p, out_p, err_p = _run_cmd([lc_bin, "print", f"{domain}/{label}"], timeout_s=5.0)
    if rc_p == 0:
        m_pid = re.search(r"^\s*pid\s*=\s*([0-9]+)\b", out_p, flags=re.MULTILINE)
        if m_pid and int(m_pid.group(1)) > 0:
            return False, f"launchctl print reports {label} still registered in launchd (pid={m_pid.group(1)})"
        m_state = re.search(r"^\s*state\s*=\s*(\S+)", out_p, flags=re.MULTILINE)
        state_desc = f"state={m_state.group(1)}" if m_state else "registered/pending restart"
        return False, f"launchctl print reports {label} still registered in launchd ({state_desc})"
    combined_p = f"{out_p}\n{err_p}".lower()
    if rc_p not in (3, 113) and "could not find" not in combined_p and "not found" not in combined_p:
        return (
            False,
            f"launchctl print failed unexpectedly for {label} (rc={rc_p}: {err_p.strip() or out_p.strip() or 'error'})",
        )

    rc_l, out_l, err_l = _run_cmd([lc_bin, "list"], timeout_s=5.0)
    if rc_l != 0:
        return False, f"launchctl list failed (rc={rc_l}: {err_l.strip() or 'error'})"
    pids = _parse_launchctl_list_pids(out_l)
    if label in pids:
        pid_desc = str(pids[label]) if pids[label] is not None else "none/waiting-restart"
        return False, f"launchctl list reports {label} still registered in launchd (pid={pid_desc})"

    rc_ps, out_ps, err_ps = _run_cmd([ps_bin, "-axo", "pid=,command="], timeout_s=5.0)
    if rc_ps != 0:
        return False, f"ps process check failed (rc={rc_ps}: {err_ps.strip() or 'error'})"
    self_pids = {os.getpid(), os.getppid()}
    for raw_line in out_ps.splitlines():
        m = re.match(r"^\s*([0-9]+)\s+(.*)$", raw_line)
        if not m:
            continue
        pid_val = int(m.group(1))
        if pid_val in self_pids:
            continue
        if cmd_matcher(m.group(2)):
            return False, f"process table reports {target_desc} alive (pid={pid_val})"

    return True, f"{label} confirmed unloaded from launchd and absent from process table"


def _is_refresher_process_stopped(target: str) -> tuple[bool, str]:
    """Confirm via launchd registration (`launchctl print`/`list`) AND `ps` that refresher is unloaded & exited."""
    label = "com.dalton.guidepoint-mcp-proxy" if target == "legacy" else "com.dalton.research-host"
    return _is_launchd_service_stopped(
        label,
        lambda cmd: _matches_refresher_command(target, cmd),
        target,
    )


def _is_bridge_process_stopped() -> tuple[bool, str]:
    """Confirm via launchd registration (`launchctl print`/`list`) AND `ps` that `:8960` bridge is unloaded & exited."""
    return _is_launchd_service_stopped(
        "com.dalton.researchdata-bridge",
        _matches_bridge_command,
        "bridge",
    )


def _list_token_tmp_files(tokens_path: Path) -> list[Path]:
    if not tokens_path.parent.is_dir():
        return []
    return sorted(tokens_path.parent.glob(".guidepoint_mcp_tokens.*.tmp"))


def cmd_drain_refresher(args: argparse.Namespace) -> int:
    """Drain the active Guidepoint token refresher cleanly before `launchctl bootout`.

    1. Polls `/healthz` up to `--timeout`; when unreachable with `--allow-down`, skips drain ONLY
       after confirming via `launchctl print` / `launchctl list` AND `ps` that the refresher process
       has actually exited (and zero `.guidepoint_mcp_tokens.*.tmp` files remain). If the process
       is still alive while `/healthz` times out, continues polling until `--timeout` and exits 1.
    2. If the refresher holds the owner lock and the access token has `<= refresh_skew_s` (60s)
       remaining, performs a controlled refresh while it is still the sole lock holder (using the
       full MCP session handshake for `:8943`) and verifies the updated token landed on disk with
       no leftover `.tmp` files.
    3. Sends `POST /admin/drain` (`{"drain": true}`) using the resolved admin token (legacy proxy
       reuses `resolve_legacy_proxy_effective_paths`, including `GUIDEPOINT_ENV_FILE`); fails if
       not already draining and the admin token is missing.
    4. Polls `/healthz` until `draining=true` AND `in_flight == 0` AND zero `.tmp` files exist.
    """
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    home_dir = Path(args.home).expanduser().resolve() if args.home else Path.home().resolve()
    target = str(args.target)
    default_url = "http://127.0.0.1:8943" if target == "legacy" else "http://127.0.0.1:8951"
    base_url = str(args.url or default_url).rstrip("/")
    timeout_s = max(0.5, float(args.timeout))
    poll_s = max(0.05, float(args.poll_interval))
    skew_s = max(0.0, float(args.refresh_skew_s))
    allow_down = bool(args.allow_down)

    repo_env_path = Path(args.repo_env).expanduser().resolve() if args.repo_env else (repo_root / ".env")
    rh_env_path = (
        Path(args.research_host_env).expanduser().resolve()
        if args.research_host_env
        else (home_dir / ".config" / "dalton" / "research-host.env")
    )
    repo_vals = _parse_simple_env_file(repo_env_path)
    rh_vals = _parse_simple_env_file(rh_env_path)

    gp_env_path: Path | None = None
    proxy_plist: Path | None = None
    admin_tok = ""
    try:
        if target == "legacy":
            tokens_path, gp_env_path, proxy_plist, admin_tok = _resolve_legacy_proxy_ops_context(
                args, repo_root, home_dir
            )
        else:
            tokens_path = _resolve_tokens_path_for_ops(args, repo_root, home_dir)
            admin_tok = (
                str(rh_vals.get("DALTON_RESEARCH_ADMIN_TOKEN") or "").strip()
                or str(repo_vals.get("DALTON_RESEARCH_ADMIN_TOKEN") or "").strip()
            )
    except Exception as exc:
        print(f"ERROR: failed to resolve Guidepoint refresher context: {exc}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + timeout_s

    def _wait_no_tmp() -> tuple[bool, list[Path]]:
        while True:
            tmps = _list_token_tmp_files(tokens_path)
            if not tmps:
                return True, []
            if time.monotonic() >= deadline:
                return False, tmps
            time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))

    # Phase 1: Reach /healthz (bounded poll); --allow-down skips drain ONLY when process exit is verified.
    body: dict[str, Any] | None = None
    init_last_err = "no response received"
    while True:
        req_t = min(5.0, max(0.2, deadline - time.monotonic()))
        code, raw_body, err = _http_request("GET", f"{base_url}/healthz", timeout_s=req_t)
        if not err and code == 200 and isinstance(raw_body, dict):
            body = raw_body
            break
        if err or code is None:
            if allow_down:
                stopped, proc_detail = _is_refresher_process_stopped(target)
                if stopped:
                    clean, tmps = _wait_no_tmp()
                    if not clean:
                        print(
                            f"ERROR: refresher ({target}) exited but temp token file(s) remain: {[p.name for p in tmps]}",
                            file=sys.stderr,
                        )
                        return 1
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "target": target,
                                "status": "already_down",
                                "in_flight": 0,
                                "tmp_files": 0,
                                "controlled_refresh": False,
                                "process_check": proc_detail,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0
                init_last_err = (
                    f"GET {base_url}/healthz failed ({err or 'unreachable'}) while process still alive ({proc_detail})"
                )
            else:
                init_last_err = f"cannot reach {base_url}/healthz ({err or 'unreachable'})"
        else:
            init_last_err = f"unexpected {base_url}/healthz response (HTTP {code})"

        if time.monotonic() >= deadline:
            print(
                f"ERROR: drain-refresher ({target}) timed out after {timeout_s:.1f}s ({init_last_err})",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))

    if target == "research-host":
        probes_b = body.get("probes")
        gp_b = probes_b.get("guidepoint") if isinstance(probes_b, dict) else None
        holds_lock = isinstance(gp_b, dict) and gp_b.get("owner_lock_held") is True
    else:
        holds_lock = body.get("owner_lock_held") is True

    controlled_refresh = False
    if holds_lock and body.get("draining") is not True and tokens_path.is_file():
        tok_data: Any = None
        try:
            tok_data = json.loads(tokens_path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt/unreadable live token file: skip controlled refresh so rollback can
            # still drain, stop the refresher, and fall back to the snapshot token copy.
            tok_data = None
        if isinstance(tok_data, dict):
            raw_obt = tok_data.get("obtained_at")
            raw_exp = tok_data.get("expires_in", 600)
            if (
                isinstance(raw_obt, (int, float))
                and not isinstance(raw_obt, bool)
                and isinstance(raw_exp, (int, float))
                and not isinstance(raw_exp, bool)
            ):
                prev_obt = float(raw_obt)
                remaining_s = (prev_obt + float(raw_exp)) - time.time()
                if remaining_s <= skew_s:
                    rem_budget = max(2.0, deadline - time.monotonic())
                    if target == "legacy":
                        _inner, r_mcp_err = _call_legacy_proxy_guidepoint_mcp(
                            base_url,
                            query="cloud",
                            size=1,
                            timeout_s=rem_budget,
                        )
                        if r_mcp_err:
                            print(
                                f"ERROR: controlled token refresh via {target} failed: {r_mcp_err}",
                                file=sys.stderr,
                            )
                            return 1
                    else:
                        rh_tok = (
                            str(rh_vals.get("DALTON_RESEARCH_HOST_TOKEN") or "").strip()
                            or str(repo_vals.get("DALTON_RESEARCH_HOST_TOKEN") or "").strip()
                        )
                        if not rh_tok:
                            print(
                                f"ERROR: access token TTL={remaining_s:.1f}s <= {skew_s:.0f}s "
                                f"but missing DALTON_RESEARCH_HOST_TOKEN in {rh_env_path} for controlled refresh",
                                file=sys.stderr,
                            )
                            return 1
                        rpc_body = json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 102,
                                "method": "tools/call",
                                "params": {
                                    "name": "search_library",
                                    "arguments": {"query": "cloud", "limit": 1},
                                },
                            }
                        ).encode("utf-8")
                        r_code, r_body, r_err = _http_request(
                            "POST",
                            f"{base_url}/mcp/guidepoint",
                            headers={
                                "Authorization": f"Bearer {rh_tok}",
                                "Content-Type": "application/json",
                            },
                            body=rpc_body,
                            timeout_s=rem_budget,
                        )
                        if r_err or r_code != 200:
                            print(
                                f"ERROR: controlled token refresh via {target} failed: HTTP {r_code} ({r_err or 'error'})",
                                file=sys.stderr,
                            )
                            return 1
                        inner, m_err = _extract_mcp_payload(r_body)
                        if m_err or inner is None or inner.get("ok") is False:
                            print(
                                f"ERROR: controlled token refresh via {target} returned error: {m_err or (inner or {}).get('error')}",
                                file=sys.stderr,
                            )
                            return 1
                    clean_after, tmps_after = _wait_no_tmp()
                    if not clean_after:
                        print(
                            f"ERROR: temp token file(s) still present after controlled refresh: {[p.name for p in tmps_after]}",
                            file=sys.stderr,
                        )
                        return 1
                    tok_ok, _new_ts, tok_reason = _inspect_guidepoint_tokens_file(tokens_path)
                    if not tok_ok:
                        print(
                            f"ERROR: tokens file invalid after controlled refresh ({tok_reason})",
                            file=sys.stderr,
                        )
                        return 1
                    new_data = json.loads(tokens_path.read_text(encoding="utf-8"))
                    new_obt = float(new_data.get("obtained_at", 0) or 0)
                    new_exp = float(new_data.get("expires_in", 600) or 600)
                    new_rem = (new_obt + new_exp) - time.time()
                    if new_obt <= prev_obt and new_rem <= skew_s:
                        print(
                            f"ERROR: controlled refresh did not persist a fresh token on disk "
                            f"(obtained_at={new_obt}, prev={prev_obt}, remaining={new_rem:.1f}s)",
                            file=sys.stderr,
                        )
                        return 1
                    controlled_refresh = True

    # Phase 3: Enter drain mode; require admin token when refresher is not already draining.
    if body.get("draining") is not True:
        if not admin_tok:
            if target == "legacy":
                print(
                    f"ERROR: cannot drain {base_url}: missing GUIDEPOINT_PROXY_ADMIN_TOKEN "
                    f"(checked {gp_env_path} and {proxy_plist})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"ERROR: cannot drain {base_url}: missing DALTON_RESEARCH_ADMIN_TOKEN "
                    f"(checked {rh_env_path} and {repo_env_path})",
                    file=sys.stderr,
                )
            return 1
        d_code, d_body, d_err = _http_request(
            "POST",
            f"{base_url}/admin/drain",
            headers={
                "Authorization": f"Bearer {admin_tok}",
                "Content-Type": "application/json",
            },
            body=b'{"drain": true}',
            timeout_s=min(5.0, max(1.0, deadline - time.monotonic())),
        )
        if d_err or d_code != 200:
            print(
                f"ERROR: POST {base_url}/admin/drain failed: HTTP {d_code} ({d_err or d_body})",
                file=sys.stderr,
            )
            return 1

    # Phase 4: Confirm draining=true AND in_flight==0 AND zero .tmp files on disk.
    last_detail = ""
    while True:
        h_code, h_body, h_err = _http_request(
            "GET",
            f"{base_url}/healthz",
            timeout_s=min(5.0, max(0.2, deadline - time.monotonic())),
        )
        if h_err or h_code is None:
            if allow_down:
                stopped, proc_detail = _is_refresher_process_stopped(target)
                if stopped:
                    clean, tmps = _wait_no_tmp()
                    if not clean:
                        print(
                            f"ERROR: refresher ({target}) stopped while temp token file(s) remain: {[p.name for p in tmps]}",
                            file=sys.stderr,
                        )
                        return 1
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "target": target,
                                "status": "stopped_clean",
                                "in_flight": 0,
                                "tmp_files": 0,
                                "controlled_refresh": controlled_refresh,
                                "process_check": proc_detail,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0
                last_detail = (
                    f"GET {base_url}/healthz -> {h_code} ({h_err}); process still alive ({proc_detail})"
                )
            else:
                last_detail = f"GET {base_url}/healthz -> {h_code} ({h_err})"
        elif h_code != 200 or not isinstance(h_body, dict):
            last_detail = f"GET {base_url}/healthz -> HTTP {h_code} (invalid body)"
        else:
            is_draining = h_body.get("draining") is True
            in_flight = h_body.get("in_flight")
            tmps = _list_token_tmp_files(tokens_path)
            if (
                is_draining
                and isinstance(in_flight, int)
                and not isinstance(in_flight, bool)
                and in_flight == 0
                and not tmps
            ):
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "target": target,
                            "status": "drained",
                            "draining": True,
                            "in_flight": 0,
                            "tmp_files": 0,
                            "controlled_refresh": controlled_refresh,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            last_detail = (
                f"draining={h_body.get('draining')!r}, in_flight={in_flight!r}, "
                f"tmp_files={[p.name for p in tmps]}"
            )

        if time.monotonic() >= deadline:
            print(
                f"ERROR: drain-refresher ({target}) timed out after {timeout_s:.1f}s ({last_detail})",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))


def _resolve_bridge_admin_token_from_verified_snapshot(
    snapshot_dir: Path | str,
) -> tuple[str, str | None]:
    """Verify a cutover snapshot and extract its saved `DALTON_BRIDGE_ADMIN_TOKEN`."""
    snap_path = Path(snapshot_dir).expanduser().resolve()
    snap_errs = verify_cutover_snapshot(snap_path)
    if snap_errs:
        return "", f"snapshot verification failed ({'; '.join(snap_errs)})"
    try:
        manifest = json.loads((snap_path / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception as exc:
        return "", f"unreadable snapshot manifest ({exc})"
    entries = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(entries, dict):
        return "", "snapshot manifest missing files map"

    def _entry_path(key: str) -> Path | None:
        info = entries.get(key)
        if not isinstance(info, dict) or info.get("existed") is not True:
            return None
        rel = str(info.get("rel_path") or "").strip()
        if not rel:
            return None
        fp = snap_path / rel
        return fp if fp.is_file() else None

    snap_bridge_env = _entry_path("bridge_env")
    if snap_bridge_env is not None:
        tok = str(_parse_simple_env_file(snap_bridge_env).get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
        if tok:
            return tok, None

    snap_bridge_plist = _entry_path("plist_bridge")
    if snap_bridge_plist is not None:
        p_ev, _p_err = _load_plist_env_vars_strict(snap_bridge_plist)
        if p_ev:
            tok = str(p_ev.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
            if tok:
                return tok, None

    snap_repo_env = _entry_path("repo_env")
    if snap_repo_env is not None:
        tok = str(_parse_simple_env_file(snap_repo_env).get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
        if tok:
            return tok, None

    return "", None


def cmd_drain_bridge(args: argparse.Namespace) -> int:
    """Drain legacy researchdata bridge (`:8960`) via `/admin/drain` before `launchctl bootout`.

    1. Sends `POST /admin/drain` (`{"drain": true}`) using `DALTON_BRIDGE_ADMIN_TOKEN` from
       current config; on HTTP `401` or `403` (or missing current token) when `--snapshot-dir` is given,
       verifies the snapshot and retries with the snapshot's saved `DALTON_BRIDGE_ADMIN_TOKEN`.
    2. Polls `GET /admin/drain` until `draining=true`, `inflight == 0`, `queued == 0`, and
       `ingest_inflight == 0` (default timeout 240s to cover the 180s ingest limit).
    3. With `--allow-down`, skips drain ONLY when `com.dalton.researchdata-bridge` is confirmed
       unloaded from launchd AND absent from the process table (`ps`).
    """
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    home_dir = Path(args.home).expanduser().resolve() if args.home else Path.home().resolve()
    base_url = str(args.url or "http://127.0.0.1:8960").rstrip("/")
    timeout_s = max(0.5, float(args.timeout))
    poll_s = max(0.05, float(args.poll_interval))
    allow_down = bool(args.allow_down)

    eff_plist_dir = (
        Path(args.plist_dir).expanduser().resolve()
        if getattr(args, "plist_dir", None)
        else (
            Path(os.environ["DALTON_LAUNCH_AGENTS_DIR"]).expanduser().resolve()
            if str(os.environ.get("DALTON_LAUNCH_AGENTS_DIR") or "").strip()
            else (home_dir / "Library" / "LaunchAgents")
        )
    )
    bridge_plist = eff_plist_dir / "com.dalton.researchdata-bridge.plist"
    bridge_env_path = (
        Path(args.bridge_env).expanduser().resolve()
        if getattr(args, "bridge_env", None)
        else (
            Path(os.environ["DALTON_BRIDGE_ENV_FILE"]).expanduser().resolve()
            if str(os.environ.get("DALTON_BRIDGE_ENV_FILE") or "").strip()
            else (home_dir / ".config" / "dalton" / "researchdata-bridge.env")
        )
    )
    repo_env_path = (
        Path(args.repo_env).expanduser().resolve()
        if getattr(args, "repo_env", None)
        else (repo_root / ".env")
    )

    bridge_vals = _parse_simple_env_file(bridge_env_path)
    admin_tok = str(bridge_vals.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
    if not admin_tok and bridge_plist.is_file():
        p_ev, _p_err = _load_plist_env_vars_strict(bridge_plist)
        if p_ev:
            admin_tok = str(p_ev.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
            if not admin_tok and p_ev.get("DALTON_BRIDGE_ENV_FILE"):
                plist_bridge_env = Path(str(p_ev["DALTON_BRIDGE_ENV_FILE"])).expanduser().resolve()
                admin_tok = str(_parse_simple_env_file(plist_bridge_env).get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
    if not admin_tok:
        repo_vals = _parse_simple_env_file(repo_env_path)
        admin_tok = str(repo_vals.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()

    snapshot_admin_tok = ""
    snapshot_tok_err: str | None = None
    snapshot_dir_arg = getattr(args, "snapshot_dir", None)
    if snapshot_dir_arg:
        snapshot_admin_tok, snapshot_tok_err = _resolve_bridge_admin_token_from_verified_snapshot(
            snapshot_dir_arg
        )

    candidate_tokens: list[tuple[str, str]] = []
    if admin_tok:
        candidate_tokens.append(("current", admin_tok))
    if snapshot_admin_tok and snapshot_admin_tok != admin_tok:
        candidate_tokens.append(("snapshot", snapshot_admin_tok))

    if not candidate_tokens:
        if allow_down:
            stopped, proc_detail = _is_bridge_process_stopped()
            if stopped:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "target": "bridge",
                            "status": "already_down",
                            "draining": True,
                            "inflight": 0,
                            "queued": 0,
                            "ingest_inflight": 0,
                            "process_check": proc_detail,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
        extra_snap = f"; snapshot fallback error: {snapshot_tok_err}" if snapshot_tok_err else ""
        print(
            f"ERROR: cannot drain {base_url}: missing DALTON_BRIDGE_ADMIN_TOKEN "
            f"(checked {bridge_env_path}, {bridge_plist}, and {repo_env_path}{extra_snap})",
            file=sys.stderr,
        )
        return 1

    active_idx = 0
    deadline = time.monotonic() + timeout_s

    def _parse_bridge_counters(payload: Any) -> tuple[bool, dict[str, int] | None, str]:
        if not isinstance(payload, dict):
            return False, None, "response is not a JSON object"
        is_draining = payload.get("draining") is True
        vals: dict[str, int] = {}
        for field in ("inflight", "queued", "ingest_inflight"):
            v = payload.get(field)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return False, None, f"invalid {field}={v!r}"
            vals[field] = v
        if not is_draining:
            return False, vals, f"draining={payload.get('draining')!r}, {vals}"
        return True, vals, f"draining=True, {vals}"

    # Phase 1: Enter drain mode via POST /admin/drain (bounded retry on transient connectivity;
    # fallback to verified snapshot admin token on HTTP 401/403)
    last_post_err = "no response received"
    while True:
        tok_source, active_tok = candidate_tokens[active_idx]
        auth_headers = {
            "Authorization": f"Bearer {active_tok}",
            "Content-Type": "application/json",
        }
        req_t = min(5.0, max(0.2, deadline - time.monotonic()))
        d_code, d_body, d_err = _http_request(
            "POST",
            f"{base_url}/admin/drain",
            headers=auth_headers,
            body=b'{"drain": true}',
            timeout_s=req_t,
        )
        if not d_err and d_code == 200 and isinstance(d_body, dict):
            break
        if d_code in (401, 403) and active_idx + 1 < len(candidate_tokens):
            active_idx += 1
            continue
        if d_code in (401, 403):
            tried = ",".join(src for src, _ in candidate_tokens[: active_idx + 1])
            snap_note = f"; snapshot fallback unavailable: {snapshot_tok_err}" if snapshot_tok_err else ""
            print(
                f"ERROR: POST {base_url}/admin/drain rejected credentials (HTTP {d_code}, tried={tried}{snap_note})",
                file=sys.stderr,
            )
            return 1
        if d_err or d_code is None:
            if allow_down:
                stopped, proc_detail = _is_bridge_process_stopped()
                if stopped:
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "target": "bridge",
                                "status": "already_down",
                                "draining": True,
                                "inflight": 0,
                                "queued": 0,
                                "ingest_inflight": 0,
                                "process_check": proc_detail,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0
                last_post_err = (
                    f"POST {base_url}/admin/drain failed ({d_err or 'unreachable'}) "
                    f"while service still registered/alive ({proc_detail})"
                )
            else:
                last_post_err = f"cannot reach {base_url}/admin/drain ({d_err or 'unreachable'})"
        else:
            last_post_err = f"POST {base_url}/admin/drain returned HTTP {d_code} ({d_body})"

        if time.monotonic() >= deadline:
            print(
                f"ERROR: drain-bridge timed out after {timeout_s:.1f}s ({last_post_err}); "
                "if ingest is still running, wait for ingest to finish before re-running (入库未结束时先等待再重跑)",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))

    # Phase 2: Poll GET /admin/drain until draining=true AND inflight==0 AND queued==0 AND ingest_inflight==0
    last_detail = ""
    while True:
        tok_source, active_tok = candidate_tokens[active_idx]
        req_t = min(5.0, max(0.2, deadline - time.monotonic()))
        g_code, g_body, g_err = _http_request(
            "GET",
            f"{base_url}/admin/drain",
            headers={"Authorization": f"Bearer {active_tok}"},
            timeout_s=req_t,
        )
        if g_code in (401, 403) and active_idx + 1 < len(candidate_tokens):
            active_idx += 1
            continue
        if g_code in (401, 403):
            tried = ",".join(src for src, _ in candidate_tokens[: active_idx + 1])
            snap_note = f"; snapshot fallback unavailable: {snapshot_tok_err}" if snapshot_tok_err else ""
            print(
                f"ERROR: GET {base_url}/admin/drain rejected credentials (HTTP {g_code}, tried={tried}{snap_note})",
                file=sys.stderr,
            )
            return 1
        if g_err or g_code is None:
            if allow_down:
                stopped, proc_detail = _is_bridge_process_stopped()
                if stopped:
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "target": "bridge",
                                "status": "stopped_clean",
                                "draining": True,
                                "inflight": 0,
                                "queued": 0,
                                "ingest_inflight": 0,
                                "token_source": tok_source,
                                "process_check": proc_detail,
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 0
                last_detail = (
                    f"GET {base_url}/admin/drain -> {g_code} ({g_err}); "
                    f"service still registered/alive ({proc_detail})"
                )
            else:
                last_detail = f"GET {base_url}/admin/drain -> {g_code} ({g_err})"
        elif g_code != 200:
            last_detail = f"GET {base_url}/admin/drain -> HTTP {g_code}"
        else:
            ok_draining, vals, desc = _parse_bridge_counters(g_body)
            if (
                ok_draining
                and vals is not None
                and vals["inflight"] == 0
                and vals["queued"] == 0
                and vals["ingest_inflight"] == 0
            ):
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "target": "bridge",
                            "status": "drained",
                            "draining": True,
                            "inflight": 0,
                            "queued": 0,
                            "ingest_inflight": 0,
                            "token_source": tok_source,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            last_detail = desc

        if time.monotonic() >= deadline:
            print(
                f"ERROR: drain-bridge timed out after {timeout_s:.1f}s ({last_detail}); "
                "if ingest_inflight > 0, wait for ingest to finish before re-running (入库未结束时先等待再重跑)",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))


def cmd_verify_frontend_version(args: argparse.Namespace) -> int:
    """Verify `web/releases/current` and actually served `<edge_url>/version.json` match expected version."""
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    repo_env_path = (
        Path(args.repo_env).expanduser().resolve()
        if getattr(args, "repo_env", None)
        else (repo_root / ".env")
    )
    repo_vals = _parse_simple_env_file(repo_env_path)

    web_cur = (
        _abs_no_deref(args.web_releases_current)
        if getattr(args, "web_releases_current", None)
        else (repo_root / "web" / "releases" / "current")
    )
    try:
        resolved_dir, link_target, rel_id, disk_ver = _validate_web_releases_current_for_snapshot(web_cur)
    except Exception as exc:
        print(f"ERROR: verify-frontend-version disk check failed: {exc}", file=sys.stderr)
        return 1

    expected_ver = dict(disk_ver)

    if getattr(args, "snapshot_dir", None):
        snap_path = Path(args.snapshot_dir).expanduser().resolve()
        snap_errs = verify_cutover_snapshot(snap_path)
        if snap_errs:
            print(
                "ERROR: verify-frontend-version snapshot verification failed: " + "; ".join(snap_errs),
                file=sys.stderr,
            )
            return 1
        manifest = json.loads((snap_path / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        web_info = manifest["web_releases_current"]
        snap_ver_fp = snap_path / str(web_info["version_json_rel_path"])
        snap_ver, snap_ver_err = _parse_frontend_version_json(snap_ver_fp)
        if snap_ver_err or snap_ver is None:
            print(f"ERROR: verify-frontend-version invalid snapshot version.json: {snap_ver_err}", file=sys.stderr)
            return 1
        disk_mismatches = _diff_frontend_version_json(
            snap_ver,
            disk_ver,
            actual_label="disk",
            expected_label="snapshot",
        )
        if disk_mismatches:
            print(
                "ERROR: verify-frontend-version disk web/releases/current/version.json mismatch: "
                + "; ".join(disk_mismatches),
                file=sys.stderr,
            )
            return 1
        expected_ver = dict(snap_ver)

    edge_url_raw = str(getattr(args, "edge_url", None) or "").strip()
    if not edge_url_raw:
        edge_port = (
            str(os.environ.get("DALTON_EDGE_PORT") or "").strip()
            or str(repo_vals.get("DALTON_EDGE_PORT") or "").strip()
            or "8088"
        )
        edge_url_raw = f"http://127.0.0.1:{edge_port}"
    edge_base = edge_url_raw.rstrip("/")
    version_url = f"{edge_base}/version.json"

    timeout_s = max(0.5, float(args.timeout))
    poll_s = max(0.05, float(args.poll_interval))
    deadline = time.monotonic() + timeout_s
    last_reason = "no response received"

    while True:
        req_t = min(5.0, max(0.2, deadline - time.monotonic()))
        code, body, err = _http_request("GET", version_url, timeout_s=req_t)
        if err or code != 200 or not isinstance(body, dict):
            last_reason = f"GET {version_url} -> {code} ({err or 'unexpected non-JSON/status response'})"
        else:
            mismatches = _diff_frontend_version_json(
                expected_ver,
                body,
                actual_label="served",
                expected_label="expected",
            )
            if not mismatches:
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "url": version_url,
                            "release_id": rel_id,
                            "link_target": link_target,
                            "resolved_dir": str(resolved_dir),
                            "expected": expected_ver,
                            "served": body,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            last_reason = f"GET {version_url} mismatch: " + "; ".join(mismatches)

        if time.monotonic() >= deadline:
            print(
                f"ERROR: verify-frontend-version timed out after {timeout_s:.1f}s ({last_reason})",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))


def cmd_wait_lock_free(args: argparse.Namespace) -> int:
    """Poll until `.guidepoint.owner.lock` is not held by any process (non-blocking flock probe)."""
    repo_root = Path(args.repo).expanduser().resolve() if args.repo else _default_repo_root()
    home_dir = Path(args.home).expanduser().resolve() if args.home else Path.home().resolve()
    timeout_s = max(0.2, float(args.timeout))
    poll_s = max(0.05, float(args.poll_interval))

    try:
        tokens_path = _resolve_tokens_path_for_ops(args, repo_root, home_dir)
        gl = _import_guidepoint_lock(repo_root)
        lock_file = gl.resolve_guidepoint_lock_file(tokens_path)
    except Exception as exc:
        print(f"ERROR: failed to resolve Guidepoint owner lock path: {exc}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + timeout_s
    while True:
        if not lock_file.exists():
            print(json.dumps({"ok": True, "lock_file": str(lock_file), "free": True}, ensure_ascii=False))
            return 0
        fd: int | None = None
        try:
            fd = os.open(str(lock_file), os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            print(json.dumps({"ok": True, "lock_file": str(lock_file), "free": True}, ensure_ascii=False))
            return 0
        except FileNotFoundError:
            print(json.dumps({"ok": True, "lock_file": str(lock_file), "free": True}, ensure_ascii=False))
            return 0
        except BlockingIOError:
            pass
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        if time.monotonic() >= deadline:
            holder = ""
            try:
                holder = lock_file.read_text(encoding="utf-8").strip()
            except OSError:
                holder = ""
            detail = f" ({holder})" if holder else ""
            print(
                f"ERROR: Guidepoint owner lock {lock_file} is still held after {timeout_s:.1f}s{detail}",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))


def _build_subcommand_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Shared cutover ops helpers")
    sub = p.add_subparsers(dest="subcommand", required=True)

    p_lv = sub.add_parser("lock-verify")
    p_lv.add_argument("--repo", required=True)
    p_lv.add_argument("--lock-fd", type=int, default=8)

    p_js = sub.add_parser("journal-set")
    p_js.add_argument("--repo", required=True)
    p_js.add_argument("--lock-fd", type=int, default=None)
    p_js.add_argument("--print-json", action="store_true")
    p_js.add_argument("pairs", nargs="+")

    p_jr = sub.add_parser("journal-read")
    p_jr.add_argument("--repo", required=True)
    p_jr.add_argument("--key", default=None)

    p_rp = sub.add_parser("recovery-publish")
    p_rp.add_argument("--repo", required=True)
    p_rp.add_argument("--src-dir", required=True)
    p_rp.add_argument("--gp-lock-src", required=True)
    p_rp.add_argument("--source-commit", required=True)

    p_mw = sub.add_parser("manifest-write")
    p_mw.add_argument("--recovery-dir", required=True)
    p_mw.add_argument("--source-commit", required=True)
    p_mw.add_argument("--file", dest="files", action="append", required=True)

    p_mv = sub.add_parser("manifest-verify")
    p_mv.add_argument("--recovery-dir", required=True)

    p_ab = sub.add_parser("approve-budgets")
    p_ab.add_argument("--repo", required=True)
    p_ab.add_argument("--budgets-file", default=None)
    p_ab.add_argument("--lock-fd", type=int, default=None)
    p_ab.add_argument("--handoff-deadline-s", default=None)
    p_ab.add_argument("--stage-b-window-m", default=None)

    p_gc = sub.add_parser("guard-check")
    p_gc.add_argument("--repo", required=True)
    p_gc.add_argument("--action", required=True)
    p_gc.add_argument("--lock-fd", type=int, default=8)
    p_gc.add_argument("--lock-held", required=True)

    p_ca = sub.add_parser("compose-args")
    p_ca.add_argument("--repo", required=True)
    p_ca.add_argument("--repo-env", default=None)
    p_ca.add_argument("--json", action="store_true")

    p_ss = sub.add_parser("status-summary")
    p_ss.add_argument("--repo", required=True)
    p_ss.add_argument("--budgets-file", default=None)
    p_ss.add_argument("--include-dry-run-plan", action="store_true")
    p_ss.add_argument("--test-workspace-id", default="")
    p_ss.add_argument("--keep-admission-paused", action="store_true")
    p_ss.add_argument("--json", action="store_true")

    p_sc = sub.add_parser("snapshot-create")
    p_sc.add_argument("--repo", required=True)
    p_sc.add_argument("--home", default=None)
    p_sc.add_argument("--out-dir", default=None)
    p_sc.add_argument("--label", default="")
    p_sc.add_argument("--plist-dir", default=None)
    p_sc.add_argument("--repo-env", default=None)
    p_sc.add_argument("--bridge-env", default=None)
    p_sc.add_argument("--research-host-env", default=None)
    p_sc.add_argument("--guidepoint-env", default=None)
    p_sc.add_argument("--tokens-file", default=None)
    p_sc.add_argument("--journal-file", default=None)
    p_sc.add_argument("--web-releases-current", default=None)
    p_sc.add_argument("--print-path", action="store_true")
    p_sc.add_argument("--json", action="store_true")

    p_sv = sub.add_parser("snapshot-verify")
    p_sv.add_argument("--snapshot-dir", required=True)
    p_sv.add_argument("--json", action="store_true")

    p_sl = sub.add_parser("snapshot-latest")
    p_sl.add_argument("--repo", required=True)

    p_sr = sub.add_parser("snapshot-restore")
    p_sr.add_argument("--snapshot-dir", required=True)
    p_sr.add_argument("--dry-run", action="store_true")

    p_gst = sub.add_parser("gate-status")
    p_gst.add_argument("--repo", required=True)
    p_gst.add_argument("--state-file", default=None)

    p_gset = sub.add_parser("gate-set")
    p_gset.add_argument("--repo", required=True)
    p_gset.add_argument("--state-file", default=None)
    p_gset.add_argument("--paused", required=True, choices=("true", "false"))
    p_gset.add_argument("--pause-host-direct", default=None, choices=("true", "false"))
    p_gset.add_argument("--allow-workspace-id", action="append", default=[])
    p_gset.add_argument("--reason", default="")
    p_gset.add_argument("--operation", default="")
    p_gset.add_argument("--dsn", default=None)
    p_gset.add_argument("--lock-timeout-s", type=float, default=None)
    p_gset.add_argument("--no-db-lock", action="store_true")

    p_gver = sub.add_parser("gate-verify")
    p_gver.add_argument("--repo", required=True)
    p_gver.add_argument("--state-file", default=None)
    p_gver.add_argument("--repo-env", default=None)
    p_gver.add_argument("--fallback-repo-env", action="append", default=[])
    p_gver.add_argument("--expected-revision", type=int, default=None)
    p_gver.add_argument("--expected-paused", default="true", choices=("true", "false"))
    p_gver.add_argument("--backend-url", default="http://127.0.0.1:8000")
    p_gver.add_argument("--container-name", default="dalton-langalpha-backend-1")
    p_gver.add_argument("--deadline-s", type=float, default=30.0)
    p_gver.add_argument("--poll-interval-s", type=float, default=1.0)
    p_gver.add_argument("--allow-unavailable", action="store_true")

    p_ps = sub.add_parser("probe-services")
    p_ps.add_argument("--repo", required=True)
    p_ps.add_argument("--home", default=None)
    p_ps.add_argument("--target", required=True, choices=("legacy", "research-host"))
    p_ps.add_argument("--backend-url", default="http://127.0.0.1:8000")
    p_ps.add_argument("--proxy-url", default="http://127.0.0.1:8943")
    p_ps.add_argument("--bridge-url", default="http://127.0.0.1:8960")
    p_ps.add_argument("--research-host-url", default="http://127.0.0.1:8951")
    p_ps.add_argument("--repo-env", default=None)
    p_ps.add_argument("--bridge-env", default=None)
    p_ps.add_argument("--research-host-env", default=None)
    p_ps.add_argument("--query", default="cloud")
    p_ps.add_argument("--timeout", type=float, default=15.0)
    p_ps.add_argument("--retries", type=int, default=3)
    p_ps.add_argument("--retry-delay", type=float, default=1.0)
    p_ps.add_argument("--skip-backend", action="store_true")
    p_ps.add_argument("--json", action="store_true")

    p_wo = sub.add_parser("wait-owner")
    p_wo.add_argument("--target", required=True, choices=("legacy", "research-host"))
    p_wo.add_argument("--url", default=None)
    p_wo.add_argument("--timeout", type=float, default=30.0)
    p_wo.add_argument("--poll-interval", type=float, default=0.5)

    p_wh = sub.add_parser("wait-health")
    p_wh.add_argument("--url", required=True)
    p_wh.add_argument("--timeout", type=float, default=30.0)
    p_wh.add_argument("--poll-interval", type=float, default=0.5)

    p_dr = sub.add_parser("drain-refresher")
    p_dr.add_argument("--repo", required=True)
    p_dr.add_argument("--home", default=None)
    p_dr.add_argument("--target", required=True, choices=("legacy", "research-host"))
    p_dr.add_argument("--url", default=None)
    p_dr.add_argument("--plist-dir", default=None)
    p_dr.add_argument("--repo-env", default=None)
    p_dr.add_argument("--bridge-env", default=None)
    p_dr.add_argument("--research-host-env", default=None)
    p_dr.add_argument("--guidepoint-env", default=None)
    p_dr.add_argument("--tokens-file", default=None)
    p_dr.add_argument("--timeout", type=float, default=30.0)
    p_dr.add_argument("--poll-interval", type=float, default=0.5)
    p_dr.add_argument("--refresh-skew-s", type=float, default=60.0)
    p_dr.add_argument("--allow-down", action="store_true")

    p_db = sub.add_parser("drain-bridge")
    p_db.add_argument("--repo", required=True)
    p_db.add_argument("--home", default=None)
    p_db.add_argument("--url", default="http://127.0.0.1:8960")
    p_db.add_argument("--plist-dir", default=None)
    p_db.add_argument("--repo-env", default=None)
    p_db.add_argument("--bridge-env", default=None)
    p_db.add_argument("--snapshot-dir", default=None)
    p_db.add_argument("--timeout", type=float, default=240.0)
    p_db.add_argument("--poll-interval", type=float, default=0.5)
    p_db.add_argument("--allow-down", action="store_true")

    p_vfv = sub.add_parser("verify-frontend-version")
    p_vfv.add_argument("--repo", required=True)
    p_vfv.add_argument("--edge-url", default=None)
    p_vfv.add_argument("--repo-env", default=None)
    p_vfv.add_argument("--snapshot-dir", default=None)
    p_vfv.add_argument("--web-releases-current", default=None)
    p_vfv.add_argument("--timeout", type=float, default=15.0)
    p_vfv.add_argument("--poll-interval", type=float, default=0.5)

    p_wl = sub.add_parser("wait-lock-free")
    p_wl.add_argument("--repo", required=True)
    p_wl.add_argument("--home", default=None)
    p_wl.add_argument("--plist-dir", default=None)
    p_wl.add_argument("--repo-env", default=None)
    p_wl.add_argument("--bridge-env", default=None)
    p_wl.add_argument("--research-host-env", default=None)
    p_wl.add_argument("--guidepoint-env", default=None)
    p_wl.add_argument("--tokens-file", default=None)
    p_wl.add_argument("--timeout", type=float, default=15.0)
    p_wl.add_argument("--poll-interval", type=float, default=0.2)

    return p


_SUBCOMMANDS = frozenset(
    {
        "lock-verify",
        "journal-set",
        "journal-read",
        "recovery-publish",
        "manifest-write",
        "manifest-verify",
        "approve-budgets",
        "guard-check",
        "compose-args",
        "status-summary",
        "snapshot-create",
        "snapshot-verify",
        "snapshot-latest",
        "snapshot-restore",
        "gate-status",
        "gate-set",
        "gate-verify",
        "probe-services",
        "wait-owner",
        "wait-health",
        "drain-refresher",
        "drain-bridge",
        "verify-frontend-version",
        "wait-lock-free",
    }
)


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in _SUBCOMMANDS:
        ns = _build_subcommand_parser().parse_args(args_list)
        if ns.subcommand == "lock-verify":
            return cmd_lock_verify(ns)
        if ns.subcommand == "journal-set":
            return cmd_journal_set(ns)
        if ns.subcommand == "journal-read":
            return cmd_journal_read(ns)
        if ns.subcommand == "recovery-publish":
            return cmd_recovery_publish(ns)
        if ns.subcommand == "manifest-write":
            return cmd_manifest_write(ns)
        if ns.subcommand == "manifest-verify":
            return cmd_manifest_verify(ns)
        if ns.subcommand == "approve-budgets":
            return cmd_approve_budgets(ns)
        if ns.subcommand == "guard-check":
            return cmd_guard_check(ns)
        if ns.subcommand == "compose-args":
            return cmd_compose_args(ns)
        if ns.subcommand == "status-summary":
            return cmd_status_summary(ns)
        if ns.subcommand == "snapshot-create":
            return cmd_snapshot_create(ns)
        if ns.subcommand == "snapshot-verify":
            return cmd_snapshot_verify(ns)
        if ns.subcommand == "snapshot-latest":
            return cmd_snapshot_latest(ns)
        if ns.subcommand == "snapshot-restore":
            return cmd_snapshot_restore(ns)
        if ns.subcommand == "gate-status":
            return cmd_gate_status(ns)
        if ns.subcommand == "gate-set":
            return cmd_gate_set(ns)
        if ns.subcommand == "gate-verify":
            return cmd_gate_verify(ns)
        if ns.subcommand == "probe-services":
            return cmd_probe_services(ns)
        if ns.subcommand == "wait-owner":
            return cmd_wait_owner(ns)
        if ns.subcommand == "wait-health":
            return cmd_wait_health(ns)
        if ns.subcommand == "drain-refresher":
            return cmd_drain_refresher(ns)
        if ns.subcommand == "drain-bridge":
            return cmd_drain_bridge(ns)
        if ns.subcommand == "verify-frontend-version":
            return cmd_verify_frontend_version(ns)
        if ns.subcommand == "wait-lock-free":
            return cmd_wait_lock_free(ns)
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
    rh_plist = (
        Path(ns.research_host_plist).expanduser().resolve()
        if ns.research_host_plist
        else None
    )
    archived_dir = (
        Path(ns.archived_plist_dir).expanduser().resolve()
        if ns.archived_plist_dir
        else None
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
        skip_budgets=bool(ns.skip_budgets),
        proxy_listen_port=int(ns.proxy_listen_port),
        research_host_plist=rh_plist,
        archived_plist_dir=archived_dir,
        launchctl_bin=os.environ.get("LAUNCHCTL_BIN", "launchctl"),
        lsof_bin=os.environ.get("LSOF_BIN", "lsof"),
        ps_bin=os.environ.get("PS_BIN", "ps"),
        docker_bin=os.environ.get("DOCKER_BIN", "docker"),
        uid=os.environ.get("DALTON_LAUNCHD_UID", ""),
    )

    mode = "stage-a" if ns.stage_a else "switch"
    rc, results = run_preflight(mode, cfg, report_json=ns.report_json)
    if ns.report_json:
        out_obj: dict[str, Any] = {
            "mode": mode,
            "ok": rc == 0,
            "checks": results,
        }
        if ns.include_summary:
            _, j_path, a_path, _ = _canonical_ops_paths(repo_root)
            out_obj["summary"] = _collect_status_summary_dict(
                j_path,
                a_path,
                budgets_file,
                include_dry_run_plan=bool(ns.include_dry_run_plan),
                test_workspace_id=str(ns.test_workspace_id or ""),
                keep_admission_paused=bool(ns.keep_admission_paused),
            )
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))
    else:
        for r in results:
            tag = r["status"].upper()
            stream = sys.stderr if r["status"] == "fail" else sys.stdout
            print(f"[{tag}] {r['check']}: {r['detail']}", file=stream)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
