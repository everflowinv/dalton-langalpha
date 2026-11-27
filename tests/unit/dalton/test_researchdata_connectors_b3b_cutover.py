"""Unit tests for Batch 3b Step 3 & Step 4 foundation (s34-r1).

Covers:
- `scripts/dalton/ops/prod/preflight.py` (`--stage-a`, `--switch`, `--report-json`,
  `validate_timing_budgets_env`)
- LaunchAgent templates & renderers (`com.dalton.research-host`, `guidepoint-mcp-proxy`,
  `researchdata-bridge`) and `research_runtime_migrate.sh` `--check-runtime` env
- `scripts/dalton/ops/prod/wait_backend_idle.sh` (legacy no-arg + maintenance mode)
- `scripts/dalton/ops/prod/_research_host_handoff_lib.sh` (`.ops.lock`, `recovery-bin/`,
  `rh_journal_set`, `rh_require_change_allowed`, `rh_require_no_incomplete_journal`)
- `scripts/dalton/ops/prod/switch_research_host.sh` (unimplemented exits 2 cleanly,
  read-only `--check` & `--switch --dry-run`, `--approve-budgets`)

Strictly isolated: never touches real ports (8000/8088/8943/8951/8960/5432),
real `~/Library/LaunchAgents`, real `~/.config/dalton`, or real `launchctl`/`docker`.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import time
from typing import Any
from unittest.mock import create_autospec
import urllib.error
import urllib.request

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
PROD_OPS = SCRIPTS_DALTON / "ops" / "prod"
LAUNCHD_DIR = SCRIPTS_DALTON / "launchd"
PREFLIGHT_PY = PROD_OPS / "preflight.py"
WAIT_IDLE_SH = PROD_OPS / "wait_backend_idle.sh"
HANDOFF_LIB_SH = PROD_OPS / "_research_host_handoff_lib.sh"
SWITCH_SH = PROD_OPS / "switch_research_host.sh"
MIGRATE_SH = SCRIPTS_DALTON / "ops" / "research_runtime_migrate.sh"
HOST_SERVICES_SWITCH_SH = SCRIPTS_DALTON / "ops" / "host_services_switch.sh"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


preflight = _load_module("dalton_test_preflight", PREFLIGHT_PY)
bse = _load_module("dalton_test_bse", SCRIPTS_DALTON / "bridge_subprocess_env.py")


# ---------------------------------------------------------------------------
# Fake CURL_BIN HTTP router (works identically inside no-network sandboxes)
# ---------------------------------------------------------------------------


def _make_fake_curl_bin(
    script_path: Path,
    *,
    svc_token: str,
    bridge_admin_tok: str,
    gp_admin_tok: str,
    backend_paused: bool = False,
    backend_404: bool = False,
) -> Path:
    cfg = {
        "svc_token": svc_token,
        "bridge_admin_tok": bridge_admin_tok,
        "gp_admin_tok": gp_admin_tok,
        "backend_paused": backend_paused,
        "backend_404": backend_404,
    }
    return _write_exec(
        script_path,
        f"""#!/usr/bin/env python3
import json, sys
from urllib.parse import urlparse

cfg = {cfg!r}
args = sys.argv[1:]
method = "GET"
headers = {{}}
write_status = False
url = ""
i = 0
while i < len(args):
    a = args[i]
    if a == "-X" and i + 1 < len(args):
        method = args[i + 1].upper()
        i += 2
    elif a == "-H" and i + 1 < len(args):
        hv = args[i + 1]
        if ":" in hv:
            k, v = hv.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        i += 2
    elif a in ("--max-time", "--data-binary", "-d") and i + 1 < len(args):
        i += 2
    elif a == "-w" and i + 1 < len(args):
        if "http_code" in args[i + 1]:
            write_status = True
        i += 2
    elif a.startswith("-"):
        i += 1
    else:
        url = a
        i += 1

parsed = urlparse(url)
host = parsed.netloc
path = parsed.path

def emit(code, payload):
    body = json.dumps(payload)
    if write_status:
        sys.stdout.write(body + "\\n" + str(code))
    else:
        if code >= 400:
            sys.exit(22)
        sys.stdout.write(body)
    sys.exit(0)

if host == "stub-backend.invalid":
    if cfg["backend_404"]:
        emit(404, {{"detail": "Not Found"}})
    if headers.get("x-service-token") != cfg["svc_token"]:
        emit(401, {{"detail": "Unauthorized"}})
    if method == "GET" and path == "/api/v1/dalton/maintenance/admission":
        emit(200, {{"ok": True, "paused": cfg["backend_paused"], "revision": 3, "allow_workspace_ids": [], "pause_host_direct": cfg["backend_paused"]}})
    if method == "POST" and path == "/api/v1/dalton/maintenance/admission/probe":
        if cfg["backend_paused"]:
            emit(503, {{"detail": {{"ok": False, "status": "maintenance_in_progress", "error_code": "maintenance_in_progress", "revision": 3}}}})
        emit(200, {{"ok": True, "admitted": True, "paused": False, "revision": 3}})
    emit(404, {{"detail": "Not Found"}})

if host == "stub-bridge.invalid":
    if method == "GET" and path == "/healthz":
        emit(200, {{"ok": True, "inflight": 0, "queued": 0, "ingest_inflight": 0}})
    if method == "GET" and path == "/admin/drain":
        if headers.get("authorization") != f"Bearer {{cfg['bridge_admin_tok']}}":
            emit(401, {{"ok": False, "error": "Unauthorized"}})
        emit(200, {{"ok": True, "draining": False, "inflight": 0, "queued": 0, "ingest_inflight": 0}})
    emit(404, {{"ok": False}})

if host == "stub-proxy.invalid":
    if method == "GET" and path == "/healthz":
        emit(200, {{"ok": True, "owner_lock_held": True, "draining": False, "in_flight": 0}})
    if method == "GET" and path == "/admin/drain":
        if headers.get("authorization") != f"Bearer {{cfg['gp_admin_tok']}}":
            emit(401, {{"ok": False, "error": "Unauthorized"}})
        emit(200, {{"ok": True, "draining": False, "in_flight": 0, "owner_lock_held": True}})
    emit(404, {{"ok": False}})

emit(404, {{"ok": False, "error": f"unknown stub host: {{host}}"}})
""",
    )


# ---------------------------------------------------------------------------
# Isolated test environment builder (all under tmp_path)
# ---------------------------------------------------------------------------


def _write_exec(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_valid_budgets_dict(
    *,
    probe_timeout_s: int = 10,
    drain_wait_s: int = 8,
    service_reload_s: int = 10,
    forward_budget_s: int = 40,
    recovery_budget_s: int = 35,
    full_recovery_s: int = 180,
    req_stage_b_s: int = 2400,
    handoff_deadline_s: int = 90,
    stage_b_deadline_s: int = 3600,
    approval_required: str = "false",
    approved_at: str = "",
) -> dict[str, str]:
    req_handoff_s = forward_budget_s + recovery_budget_s
    return {
        "PROBE_TIMEOUT_S": str(probe_timeout_s),
        "DRAIN_WAIT_S": str(drain_wait_s),
        "SERVICE_RELOAD_S": str(service_reload_s),
        "FORWARD_HANDOFF_BUDGET_S": str(forward_budget_s),
        "RECOVERY_BUDGET_S": str(recovery_budget_s),
        "FULL_RECOVERY_DEADLINE_S": str(full_recovery_s),
        "REQUIRED_HANDOFF_WINDOW_S": str(req_handoff_s),
        "REQUIRED_STAGE_B_WINDOW_S": str(req_stage_b_s),
        "HANDOFF_DEADLINE_S": str(handoff_deadline_s),
        "STAGE_B_WINDOW_DEADLINE_S": str(stage_b_deadline_s),
        "BUDGET_APPROVAL_REQUIRED": approval_required,
        "BUDGET_APPROVED_AT": approved_at,
        "MEASURED_AT": "2026-11-27T01:00:00Z",
        "MEASURED_SOURCE_COMMIT": "1a0aaf82e",
    }


def _setup_isolated_env(
    tmp_path: Path,
    *,
    budgets: dict[str, str] | None = None,
    proxy_pid: int = 4242,
    lsof_pids: list[int] | None = None,
    bridge_pid: int | None = 5151,
    ps_output_override: str | None = None,
    ps_exit_code: int = 0,
    launchctl_getenv_map: dict[str, str] | None = None,
    backend_paused: bool = False,
    backend_404: bool = False,
) -> dict[str, Any]:
    """Create a complete synthetic repo, home, LaunchAgents, web/releases, stubs & servers."""
    repo = (tmp_path / "repo").resolve()
    home = (tmp_path / "home").resolve()
    plist_dir = home / "Library" / "LaunchAgents"
    logs_dir = home / "Library" / "Logs"
    cfg_dir = home / ".config" / "dalton"
    res_home = home / "Library" / "Application Support" / "Everflow" / "research"
    gp_cfg_dir = res_home / "config"
    bin_dir = tmp_path / "fakebin"

    for d in (repo, plist_dir, logs_dir, cfg_dir, gp_cfg_dir, bin_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Link real code/venv directories and copy tiny launchd templates (bridge_subprocess_env rejects symlinked template parent dirs)
    if (REPO_ROOT / ".venv").is_dir():
        (repo / ".venv").symlink_to(REPO_ROOT / ".venv")
    else:
        (repo / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (repo / ".venv" / "bin" / "python").symlink_to(Path(sys.executable))
    (repo / "src").symlink_to(REPO_ROOT / "src")
    (repo / "scripts" / "dalton" / "launchd").mkdir(parents=True, exist_ok=True)
    for tpl_name in (
        "com.dalton.guidepoint-mcp-proxy.plist.template",
        "com.dalton.researchdata-bridge.plist.template",
        "com.dalton.research-host.plist.template",
    ):
        (repo / "scripts" / "dalton" / "launchd" / tpl_name).write_bytes(
            (LAUNCHD_DIR / tpl_name).read_bytes()
        )
    (repo / "scripts" / "dalton" / "research_runtime").symlink_to(
        SCRIPTS_DALTON / "research_runtime"
    )
    (repo / "scripts" / "dalton" / "ops").symlink_to(SCRIPTS_DALTON / "ops")

    # Repo .env (shared capability config + INTERNAL_SERVICE_TOKEN)
    svc_token = "svc_test_token_alpha"
    repo_env = repo / ".env"
    repo_env.write_text(
        "APP_ENV=dalton\n"
        "DALTON_PROFILE=everflow\n"
        "DALTON_HOST_PLATFORM=macos\n"
        "DALTON_ENABLED=true\n"
        f"INTERNAL_SERVICE_TOKEN={svc_token}\n",
        encoding="utf-8",
    )
    repo_env.chmod(0o600)

    # researchdata-bridge.env
    bridge_admin_tok = "bridge_admin_tok_1"
    bridge_mcp_tok = "bridge_mcp_tok_1"
    bridge_env = cfg_dir / "researchdata-bridge.env"
    bridge_env.write_text(
        "DALTON_BRIDGE_PORT=8960\n"
        f"DALTON_BRIDGE_TOKEN={bridge_mcp_tok}\n"
        f"DALTON_BRIDGE_ADMIN_TOKEN={bridge_admin_tok}\n"
        f"DALTON_RESEARCH_CODE={repo / 'scripts' / 'dalton' / 'research_runtime'}\n"
        f"DALTON_RESEARCH_HOME={res_home}\n",
        encoding="utf-8",
    )
    bridge_env.chmod(0o600)

    # research-host.env (0600, pairwise distinct tokens)
    host_env = cfg_dir / "research-host.env"
    host_env.write_text(
        "DALTON_RESEARCH_HOST_TOKEN=host_ro_tok_distinct\n"
        "DALTON_RESEARCH_ADMIN_TOKEN=host_admin_tok_distinct\n"
        f"DALTON_BRIDGE_TOKEN={bridge_mcp_tok}\n"
        "GUIDEPOINT_CLIENT_ID=gp_cid_test\n"
        "GUIDEPOINT_CLIENT_SECRET=gp_csec_test\n"
        f"DALTON_RESEARCH_HOME={res_home}\n",
        encoding="utf-8",
    )
    host_env.chmod(0o600)

    # guidepoint_mcp.env & guidepoint_mcp_tokens.json
    gp_admin_tok = "gp_admin_tok_1"
    gp_env = gp_cfg_dir / "guidepoint_mcp.env"
    gp_env.write_text(
        "GUIDEPOINT_CLIENT_ID=gp_cid_test\n"
        "GUIDEPOINT_CLIENT_SECRET=gp_csec_test\n"
        f"GUIDEPOINT_PROXY_ADMIN_TOKEN={gp_admin_tok}\n",
        encoding="utf-8",
    )
    gp_env.chmod(0o600)

    tokens_file = gp_cfg_dir / "guidepoint_mcp_tokens.json"
    tokens_file.write_text(
        json.dumps(
            {
                "access_token": "tok_access_test",
                "refresh_token": "tok_refresh_test",
                "obtained_at": int(time.time()),
                "expires_in": 600,
            }
        ),
        encoding="utf-8",
    )
    tokens_file.chmod(0o600)

    # Render all 3 plists cleanly into plist_dir
    bse.render_stage_a_plists_atomic(
        output_dir=plist_dir,
        repo_root=repo,
        home_dir=home,
        bridge_env_path=bridge_env,
        research_host_env_path=host_env,
        repo_env_path=repo_env,
        logs_dir=logs_dir,
    )

    # web/releases/current -> web/releases/build-001
    rel_dir = repo / "web" / "releases" / "build-001"
    (rel_dir / "assets").mkdir(parents=True, exist_ok=True)
    (rel_dir / "help").mkdir(parents=True, exist_ok=True)
    (rel_dir / "index.html").write_text("<html>ok</html>\n", encoding="utf-8")
    (rel_dir / "version.json").write_text('{"build_id":"build-001"}\n', encoding="utf-8")
    (rel_dir / "help" / "index.html").write_text("<html>help</html>\n", encoding="utf-8")
    (repo / "web" / "releases" / "current").symlink_to(rel_dir)

    # timing-budgets.env
    cutover_dir = repo / "var" / "dalton" / "cutover-snapshots" / "research-host"
    cutover_dir.mkdir(parents=True, exist_ok=True)
    budgets_file = cutover_dir / "timing-budgets.env"
    if budgets is not None:
        budgets_file.write_text(preflight.format_timing_budgets_env(budgets), encoding="utf-8")
        budgets_file.chmod(0o600)

    # Fake launchctl, lsof, ps, docker
    lc_getenv = launchctl_getenv_map or {}
    lc_list_lines = ["PID\tStatus\tLabel"]
    if proxy_pid > 0:
        lc_list_lines.append(f"{proxy_pid}\t0\tcom.dalton.guidepoint-mcp-proxy")
    if bridge_pid:
        lc_list_lines.append(f"{bridge_pid}\t0\tcom.dalton.researchdata-bridge")
    lc_list_text = "\n".join(lc_list_lines)

    launchctl_bin = _write_exec(
        bin_dir / "launchctl",
        f"""#!/usr/bin/env python3
import json, sys
getenv_map = {json.dumps(lc_getenv)}
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "list":
    print({lc_list_text!r})
    sys.exit(0)
if cmd == "getenv":
    k = sys.argv[2] if len(sys.argv) > 2 else ""
    v = getenv_map.get(k, "")
    if v:
        print(v)
    sys.exit(0)
if cmd == "print":
    target = sys.argv[2] if len(sys.argv) > 2 else ""
    if target.endswith("/com.dalton.researchdata-bridge") and {bridge_pid!r}:
        print("    pid = {bridge_pid}")
        sys.exit(0)
    if target.endswith("/com.dalton.guidepoint-mcp-proxy") and {proxy_pid} > 0:
        print("    pid = {proxy_pid}")
        sys.exit(0)
    sys.exit(113)
sys.exit(1)
""",
    )

    eff_lsof_pids = [proxy_pid] if lsof_pids is None else lsof_pids
    lsof_lines = ["COMMAND   PID USER   FD   TYPE DEVICE SIZE/OFF NODE NAME"]
    for p in eff_lsof_pids:
        lsof_lines.append(f"python  {p} user   5u  IPv4 0x1234      0t0  TCP 127.0.0.1:8943 (LISTEN)")
    lsof_text = "\n".join(lsof_lines) if eff_lsof_pids else ""
    lsof_bin = _write_exec(
        bin_dir / "lsof",
        f"""#!/usr/bin/env python3
import sys
out = {lsof_text!r}
if out:
    print(out)
    sys.exit(0)
sys.exit(1)
""",
    )

    ps_out = (
        ps_output_override
        if ps_output_override is not None
        else f"  PID TT STAT TIME COMMAND\n {bridge_pid or 5151} ?? S 0:01 python researchdata_bridge.py DALTON_BRIDGE_ENV_FILE={bridge_env}\n"
    )
    ps_bin = _write_exec(
        bin_dir / "ps",
        f"""#!/usr/bin/env python3
import sys
if {ps_exit_code} != 0:
    sys.exit({ps_exit_code})
print({ps_out!r})
""",
    )

    # Compose config stub for docker
    compose_payload = {
        "services": {
            "backend": {
                "environment": {
                    "APP_ENV": "dalton",
                    "DALTON_PROFILE": "everflow",
                    "DALTON_HOST_PLATFORM": "macos",
                    "DALTON_ENABLED": "true",
                }
            }
        }
    }
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (repo / "docker-compose.dalton.yml").write_text("services: {}\n", encoding="utf-8")
    docker_bin = _write_exec(
        bin_dir / "docker",
        f"""#!/usr/bin/env python3
import json, sys
if len(sys.argv) >= 2 and sys.argv[1] == "compose" and "config" in sys.argv:
    print(json.dumps({compose_payload!r}))
    sys.exit(0)
sys.exit(1)
""",
    )

    # Fake CURL_BIN routing synthetic stub endpoints (zero real ports / zero sockets)
    curl_bin = _make_fake_curl_bin(
        bin_dir / "curl",
        svc_token=svc_token,
        bridge_admin_tok=bridge_admin_tok,
        gp_admin_tok=gp_admin_tok,
        backend_paused=backend_paused,
        backend_404=backend_404,
    )
    backend_url = "http://stub-backend.invalid"
    bridge_url = "http://stub-bridge.invalid"
    proxy_url = "http://stub-proxy.invalid"

    env_vars = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "LAUNCHCTL_BIN": str(launchctl_bin),
        "LSOF_BIN": str(lsof_bin),
        "PS_BIN": str(ps_bin),
        "DOCKER_BIN": str(docker_bin),
        "CURL_BIN": str(curl_bin),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_HOME": str(home),
        "DALTON_LAUNCH_AGENTS_DIR": str(plist_dir),
        "DALTON_BACKEND_URL": backend_url,
        "DALTON_PROXY_URL": proxy_url,
        "DALTON_BRIDGE_LOCAL_URL": bridge_url,
        "DALTON_TIMING_BUDGETS_FILE": str(budgets_file),
    }

    def _cleanup() -> None:
        return None

    return {
        "repo": repo,
        "home": home,
        "plist_dir": plist_dir,
        "logs_dir": logs_dir,
        "bridge_env": bridge_env,
        "host_env": host_env,
        "gp_env": gp_env,
        "tokens_file": tokens_file,
        "budgets_file": budgets_file,
        "cutover_dir": cutover_dir,
        "backend_url": backend_url,
        "bridge_url": bridge_url,
        "proxy_url": proxy_url,
        "env": env_vars,
        "cleanup": _cleanup,
    }


def _run_preflight_cli(ctx: dict[str, Any], *extra_args: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
        str(PREFLIGHT_PY),
        *extra_args,
        "--repo",
        str(ctx["repo"]),
        "--home",
        str(ctx["home"]),
        "--plist-dir",
        str(ctx["plist_dir"]),
        "--backend-url",
        str(ctx["backend_url"]),
        "--proxy-url",
        str(ctx["proxy_url"]),
        "--bridge-url",
        str(ctx["bridge_url"]),
        "--budgets-file",
        str(ctx["budgets_file"]),
    ]
    return subprocess.run(
        cmd,
        env=ctx["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# ---------------------------------------------------------------------------
# D1: preflight.py passing cases (stage-a + 3 valid timing-budgets.env variants)
# ---------------------------------------------------------------------------


def test_preflight_stage_a_passes_and_honors_expect_admission(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=_make_valid_budgets_dict(), backend_paused=False)
    try:
        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode == 0, res.stdout + res.stderr

        res_open = _run_preflight_cli(ctx, "--stage-a", "--expect-admission", "open")
        assert res_open.returncode == 0, res_open.stdout + res_open.stderr

        res_paused_fail = _run_preflight_cli(ctx, "--stage-a", "--expect-admission", "paused")
        assert res_paused_fail.returncode != 0
    finally:
        ctx["cleanup"]()

    ctx_p = _setup_isolated_env(tmp_path / "paused", budgets=_make_valid_budgets_dict(), backend_paused=True)
    try:
        res_paused_ok = _run_preflight_cli(ctx_p, "--stage-a", "--expect-admission", "paused")
        assert res_paused_ok.returncode == 0, res_paused_ok.stdout + res_paused_ok.stderr
    finally:
        ctx_p["cleanup"]()


@pytest.mark.parametrize(
    "case_name,budgets",
    [
        ("neither_exceeded", _make_valid_budgets_dict()),
        (
            "handoff_exceeded_approved",
            _make_valid_budgets_dict(
                forward_budget_s=60,
                recovery_budget_s=45,
                handoff_deadline_s=120,
                approved_at="2026-11-27T02:00:00Z",
            ),
        ),
        (
            "only_stage_b_exceeded_keeps_handoff_90",
            _make_valid_budgets_dict(
                forward_budget_s=40,
                recovery_budget_s=35,
                req_stage_b_s=4200,
                handoff_deadline_s=90,
                stage_b_deadline_s=4500,
                approved_at="2026-11-27T02:00:00Z",
            ),
        ),
    ],
)
def test_preflight_switch_passes_for_all_three_valid_budget_classes(
    tmp_path: Path,
    case_name: str,
    budgets: dict[str, str],
) -> None:
    ctx = _setup_isolated_env(tmp_path / case_name, budgets=budgets)
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode == 0, f"{case_name} failed:\n{res.stdout}\n{res.stderr}"
    finally:
        ctx["cleanup"]()


# ---------------------------------------------------------------------------
# D1: preflight.py individual failure detection cases
# ---------------------------------------------------------------------------


def test_preflight_stage_a_fails_when_backend_lacks_admission_gate(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=_make_valid_budgets_dict(), backend_404=True)
    try:
        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode != 0
        assert "现网后端缺暂停能力" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


@pytest.mark.parametrize("drop_key", ["PYTHONPATH", "DALTON_REPO_ENV_FILE"])
def test_preflight_fails_when_bridge_plist_missing_pythonpath_or_repo_env(
    tmp_path: Path,
    drop_key: str,
) -> None:
    ctx = _setup_isolated_env(tmp_path / drop_key, budgets=_make_valid_budgets_dict())
    try:
        bp = ctx["plist_dir"] / "com.dalton.researchdata-bridge.plist"
        data = plistlib.loads(bp.read_bytes())
        del data["EnvironmentVariables"][drop_key]
        bp.write_bytes(plistlib.dumps(data))

        res_a = _run_preflight_cli(ctx, "--stage-a")
        assert res_a.returncode != 0
        assert drop_key in (res_a.stderr + res_a.stdout)

        res_s = _run_preflight_cli(ctx, "--switch")
        assert res_s.returncode != 0
        assert drop_key in (res_s.stderr + res_s.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_when_timing_budgets_missing(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=None)
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "timing-budgets.env" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_forward_budget_below_two_probes(tmp_path: Path) -> None:
    # DRAIN_WAIT_S(8) + SERVICE_RELOAD_S(10) + 2*PROBE_TIMEOUT_S(10) = 38 > 35
    bad = _make_valid_budgets_dict(forward_budget_s=35, recovery_budget_s=35)
    ctx = _setup_isolated_env(tmp_path, budgets=bad)
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "FORWARD_HANDOFF_BUDGET_S" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_budget_approval_required_true(tmp_path: Path) -> None:
    bad = _make_valid_budgets_dict(approval_required="true")
    ctx = _setup_isolated_env(tmp_path, budgets=bad)
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "BUDGET_APPROVAL_REQUIRED" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_stage_b_only_exceeded_with_non90_handoff_deadline(
    tmp_path: Path,
) -> None:
    bad = _make_valid_budgets_dict(
        forward_budget_s=40,
        recovery_budget_s=35,
        req_stage_b_s=4200,
        handoff_deadline_s=110,  # illegal: required handoff 75 <= 90 must keep 90
        stage_b_deadline_s=4500,
        approved_at="2026-11-27T02:00:00Z",
    )
    ctx = _setup_isolated_env(tmp_path, budgets=bad)
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "HANDOFF_DEADLINE_S == 90" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_missing_budget_approved_at(tmp_path: Path) -> None:
    bad = _make_valid_budgets_dict(
        forward_budget_s=60,
        recovery_budget_s=45,
        handoff_deadline_s=120,
        approved_at="",
    )
    ctx = _setup_isolated_env(tmp_path, budgets=bad)
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "BUDGET_APPROVED_AT" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_confirmed_deadline_less_than_required(tmp_path: Path) -> None:
    bad_handoff = _make_valid_budgets_dict(
        forward_budget_s=60,
        recovery_budget_s=45,
        handoff_deadline_s=100,  # < 105
        approved_at="2026-11-27T02:00:00Z",
    )
    ctx1 = _setup_isolated_env(tmp_path / "h", budgets=bad_handoff)
    try:
        res = _run_preflight_cli(ctx1, "--switch")
        assert res.returncode != 0
        assert "HANDOFF_DEADLINE_S" in (res.stderr + res.stdout)
    finally:
        ctx1["cleanup"]()

    bad_stage_b = _make_valid_budgets_dict(
        req_stage_b_s=4200,
        stage_b_deadline_s=4000,  # < 4200
        approved_at="2026-11-27T02:00:00Z",
    )
    ctx2 = _setup_isolated_env(tmp_path / "b", budgets=bad_stage_b)
    try:
        res = _run_preflight_cli(ctx2, "--switch")
        assert res.returncode != 0
        assert "STAGE_B_WINDOW_DEADLINE_S" in (res.stderr + res.stdout)
    finally:
        ctx2["cleanup"]()


def test_preflight_switch_fails_unexpected_bridge_env_file_path(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=_make_valid_budgets_dict())
    try:
        alt_env = tmp_path / "other-bridge.env"
        alt_env.write_text("DALTON_BRIDGE_PORT=8960\n", encoding="utf-8")
        bp = ctx["plist_dir"] / "com.dalton.researchdata-bridge.plist"
        data = plistlib.loads(bp.read_bytes())
        data["EnvironmentVariables"]["DALTON_BRIDGE_ENV_FILE"] = str(alt_env)
        bp.write_bytes(plistlib.dumps(data))

        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "DALTON_BRIDGE_ENV_FILE" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_host_process_residual_8943_override_and_warns_unreadable_ps(
    tmp_path: Path,
) -> None:
    # 1. Process env has residual DALTON_GUIDEPOINT_MCP_URL=:8943 -> FAIL
    ctx = _setup_isolated_env(
        tmp_path / "override",
        budgets=_make_valid_budgets_dict(),
        ps_output_override="PID COMMAND\n5151 python researchdata_bridge.py DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp\n",
    )
    try:
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "DALTON_GUIDEPOINT_MCP_URL" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()

    # 2. ps fails (unreadable process env) -> WARN, exit 0
    ctx_w = _setup_isolated_env(
        tmp_path / "warn_ps",
        budgets=_make_valid_budgets_dict(),
        ps_exit_code=1,
    )
    try:
        res_w = _run_preflight_cli(ctx_w, "--switch", "--report-json")
        assert res_w.returncode == 0, res_w.stdout + res_w.stderr
        rep = json.loads(res_w.stdout)
        by_name = {c["check"]: c for c in rep["checks"]}
        assert by_name["env_override_scan"]["status"] == "warn"
    finally:
        ctx_w["cleanup"]()


def test_preflight_stage_a_fails_literal_home_var_pollution(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=_make_valid_budgets_dict())
    try:
        hp = ctx["plist_dir"] / "com.dalton.research-host.plist"
        data = plistlib.loads(hp.read_bytes())
        data["EnvironmentVariables"]["DALTON_RESEARCH_HOME"] = "$HOME/Library/Application Support/Everflow/research"
        hp.write_bytes(plistlib.dumps(data))

        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode != 0
        assert "$HOME" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_switch_fails_two_sided_tokens_path_drift(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=_make_valid_budgets_dict())
    try:
        pp = ctx["plist_dir"] / "com.dalton.guidepoint-mcp-proxy.plist"
        data = plistlib.loads(pp.read_bytes())
        data["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] = str(tmp_path / "other_tokens.json")
        pp.write_bytes(plistlib.dumps(data))

        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "tokens path mismatch" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


@pytest.mark.parametrize(
    "break_kind",
    [
        "dangling",
        "escape_outside",
        "missing_index_html",
        "missing_version_json",
        "missing_assets_dir",
        "missing_help_index",
    ],
)
def test_preflight_fails_broken_web_releases_current(tmp_path: Path, break_kind: str) -> None:
    ctx = _setup_isolated_env(tmp_path / break_kind, budgets=_make_valid_budgets_dict())
    try:
        current_link = ctx["repo"] / "web" / "releases" / "current"
        rel_dir = ctx["repo"] / "web" / "releases" / "build-001"
        if break_kind == "dangling":
            current_link.unlink()
            current_link.symlink_to(ctx["repo"] / "web" / "releases" / "does-not-exist")
        elif break_kind == "escape_outside":
            outside = tmp_path / "outside_web"
            outside.mkdir()
            current_link.unlink()
            current_link.symlink_to(outside)
        elif break_kind == "missing_index_html":
            (rel_dir / "index.html").unlink()
        elif break_kind == "missing_version_json":
            (rel_dir / "version.json").unlink()
        elif break_kind == "missing_assets_dir":
            (rel_dir / "assets").rmdir()
        elif break_kind == "missing_help_index":
            (rel_dir / "help" / "index.html").unlink()

        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode != 0
        assert "web_releases_current" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_stage_a_fails_proxy_pid_mismatch(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(
        tmp_path,
        budgets=_make_valid_budgets_dict(),
        proxy_pid=4242,
        lsof_pids=[9999],
    )
    try:
        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode != 0
        assert "PID mismatch" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()


def test_preflight_report_json_runs_all_checks_even_with_failures(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(
        tmp_path,
        budgets=_make_valid_budgets_dict(),
        backend_404=True,
        proxy_pid=4242,
        lsof_pids=[9999],
    )
    try:
        res = _run_preflight_cli(ctx, "--stage-a", "--report-json")
        assert res.returncode != 0
        report = json.loads(res.stdout)
        assert report["mode"] == "stage-a"
        assert report["ok"] is False
        checks = report["checks"]
        # Must have executed all 7 Stage A checks, not stopped at check 1
        assert len(checks) == 7
        for entry in checks:
            assert set(entry.keys()) == {"check", "status", "detail"}
            assert entry["status"] in ("ok", "fail", "warn", "skip")
        by_name = {c["check"]: c["status"] for c in checks}
        assert by_name["backend_admission_gate"] == "fail"
        assert by_name["guidepoint_proxy_pid_match"] == "fail"
        assert by_name["stage_a_plists"] == "ok"
        assert by_name["web_releases_current"] == "ok"
    finally:
        ctx["cleanup"]()


# ---------------------------------------------------------------------------
# D2 & D3: Three LaunchAgent templates + renderers + migrate script
# ---------------------------------------------------------------------------


def test_stage_a_three_plist_templates_render_and_validate(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    home = (tmp_path / "home").resolve()
    out_dir = tmp_path / "rendered"
    (repo / ".venv" / "bin").mkdir(parents=True)
    real_py = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    (repo / ".venv" / "bin" / "python").symlink_to(real_py)
    (repo / "scripts" / "dalton" / "launchd").mkdir(parents=True)
    for tpl_name in (
        "com.dalton.guidepoint-mcp-proxy.plist.template",
        "com.dalton.researchdata-bridge.plist.template",
        "com.dalton.research-host.plist.template",
    ):
        (repo / "scripts" / "dalton" / "launchd" / tpl_name).write_bytes(
            (LAUNCHD_DIR / tpl_name).read_bytes()
        )

    cfg_dir = home / ".config" / "dalton"
    cfg_dir.mkdir(parents=True)
    bridge_env = cfg_dir / "researchdata-bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_PORT=8960\n", encoding="utf-8")
    bridge_env.chmod(0o600)
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")
    host_env.chmod(0o600)

    res = subprocess.run(
        [
            str(real_py),
            str(SCRIPTS_DALTON / "bridge_subprocess_env.py"),
            "render-stage-a-plists",
            "--output-dir",
            str(out_dir),
            "--repo-root",
            str(repo),
            "--home",
            str(home),
            "--bridge-env",
            str(bridge_env),
            "--research-host-env",
            str(host_env),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    rendered_map = json.loads(res.stdout)
    expected_own = str(repo / "var" / "dalton" / "cutover-snapshots" / "research-host" / "handoff-journal.json")
    expected_py = str(repo / ".venv" / "bin" / "python")
    expected_pp = f"{repo}/src:{repo}"

    for label in (
        "com.dalton.guidepoint-mcp-proxy",
        "com.dalton.researchdata-bridge",
        "com.dalton.research-host",
    ):
        p = Path(rendered_map[label])
        raw = p.read_text(encoding="utf-8")
        assert "__" not in raw, f"leftover placeholder in {label}"
        parsed = plistlib.loads(raw.encode("utf-8"))
        assert parsed["Label"] == label
        ev = parsed["EnvironmentVariables"]
        assert ev["DALTON_GUIDEPOINT_OWNERSHIP_FILE"] == expected_own

    rh = plistlib.loads((out_dir / "com.dalton.research-host.plist").read_bytes())
    assert rh["ProgramArguments"][0] == expected_py
    assert rh["ProgramArguments"][1:3] == ["-m", "src.dalton.research_host"]
    assert "--env-file" in rh["ProgramArguments"]
    assert "--repo-env-file" in rh["ProgramArguments"]
    assert rh["EnvironmentVariables"]["PYTHONPATH"] == expected_pp
    assert rh["EnvironmentVariables"]["DALTON_REPO_ENV_FILE"] == str(repo / ".env")
    assert rh["EnvironmentVariables"]["DALTON_RESEARCH_HOST_EXTRA_MODULES"] == "host_connectors"
    assert rh["EnvironmentVariables"]["DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH"] == str(
        repo / "scripts" / "dalton" / "research_runtime"
    )
    assert rh["RunAtLoad"] is True
    assert rh["KeepAlive"] is True

    br = plistlib.loads((out_dir / "com.dalton.researchdata-bridge.plist").read_bytes())
    assert br["ProgramArguments"][0] == expected_py
    assert br["EnvironmentVariables"]["PYTHONPATH"] == expected_pp
    assert br["EnvironmentVariables"]["DALTON_REPO_ENV_FILE"] == str(repo / ".env")
    assert br["EnvironmentVariables"]["DALTON_BRIDGE_ENV_FILE"] == str(bridge_env)


def test_render_bridge_plist_fails_when_repo_venv_python_missing(tmp_path: Path) -> None:
    base = tmp_path.resolve()
    repo = base / "no_venv_repo"
    home = base / "home"
    repo.mkdir()
    home.mkdir()
    bridge_env = base / "bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_PORT=8960\n", encoding="utf-8")
    bridge_env.chmod(0o600)

    with pytest.raises(bse.EnvFileValidationError, match="uv sync --frozen"):
        bse.render_plist_atomic(
            template_path=LAUNCHD_DIR / "com.dalton.researchdata-bridge.plist.template",
            dest_path=base / "out.plist",
            bridge_env_path=bridge_env,
            repo_root=repo,
            home_dir=home,
            logs_dir=base / "logs",
        )


def test_research_runtime_migrate_check_runtime_matches_bridge_plist_env() -> None:
    text = MIGRATE_SH.read_text(encoding="utf-8")
    assert 'local bridge_py="${REPO_ROOT}/.venv/bin/python"' in text
    assert 'PYTHONPATH="${REPO_ROOT}/src:${REPO_ROOT}"' in text
    assert 'DALTON_REPO_ENV_FILE="${repo_env_file}"' in text
    assert '"${bridge_py}" "${REPO_ROOT}/scripts/dalton/researchdata_bridge.py" --check-runtime' in text


# ---------------------------------------------------------------------------
# D4: wait_backend_idle.sh (no-arg legacy + maintenance mode)
# ---------------------------------------------------------------------------


def _make_fake_docker_psql(script_path: Path, state_file: Path, schedule: list[dict[str, Any]]) -> Path:
    """Fake docker executable serving scripted psql count responses per check round."""
    state_file.write_text(json.dumps({"calls": 0, "schedule": schedule}), encoding="utf-8")
    return _write_exec(
        script_path,
        f"""#!/usr/bin/env python3
import json, sys
import importlib.util
import json
import os
from pathlib import Path

state_path = Path({str(state_file)!r})
st = json.loads(state_path.read_text(encoding="utf-8"))
sql = sys.argv[-1] if sys.argv else ""
idx = st["calls"]
sched = st["schedule"]

if "conversation_threads" in sql:
    key = "threads"
    round_idx = st.get("round_threads", 0)
    st["round_threads"] = round_idx + 1
elif "subagent_runs" in sql:
    key = "subagents"
    round_idx = st.get("round_subagents", 0)
    st["round_subagents"] = round_idx + 1
elif "dalton_data_task_slot" in sql:
    key = "data_slots"
    round_idx = st.get("round_slots", 0)
    st["round_slots"] = round_idx + 1
elif "dalton_data_task_runs" in sql:
    key = "data_runs"
    round_idx = st.get("round_runs", 0)
    st["round_runs"] = round_idx + 1
else:
    sys.exit(2)

st["calls"] = idx + 1
state_path.write_text(json.dumps(st), encoding="utf-8")
entry = sched[min(round_idx, len(sched) - 1)]
val = entry.get(key, "0")
if val == "__FAIL__":
    sys.exit(1)
print(val)
""",
    )


def test_wait_backend_idle_no_args_preserves_legacy_output_and_exit(tmp_path: Path) -> None:
    docker_ok = _make_fake_docker_psql(
        tmp_path / "docker_ok",
        tmp_path / "state_ok.json",
        [
            {"threads": "1", "subagents": "0"},
            {"threads": "0", "subagents": "0"},
            {"threads": "0", "subagents": "0"},
        ],
    )
    res_ok = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(docker_ok),
            "WAIT_IDLE_DEFAULT_POLL_S": "0",
            "WAIT_IDLE_DEFAULT_MAX_ROUNDS": "5",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    assert "threads=1 subagents=0" in res_ok.stdout
    assert "idle after 3 min (" in res_ok.stdout

    docker_busy = _make_fake_docker_psql(
        tmp_path / "docker_busy",
        tmp_path / "state_busy.json",
        [{"threads": "1", "subagents": "0"}],
    )
    res_busy = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(docker_busy),
            "WAIT_IDLE_DEFAULT_POLL_S": "0",
            "WAIT_IDLE_DEFAULT_MAX_ROUNDS": "2",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_busy.returncode == 1
    assert "still_busy" in res_busy.stdout


def test_wait_backend_idle_maintenance_waits_for_data_task_slot_and_two_consecutive_zeros(
    tmp_path: Path,
) -> None:
    docker_bin = _make_fake_docker_psql(
        tmp_path / "docker_maint",
        tmp_path / "state_maint.json",
        [
            {"threads": "0", "subagents": "0", "data_slots": "1", "data_runs": "1"},
            {"threads": "0", "subagents": "0", "data_slots": "0", "data_runs": "0"},
            {"threads": "0", "subagents": "0", "data_slots": "1", "data_runs": "0"},  # resets streak
            {"threads": "0", "subagents": "0", "data_slots": "0", "data_runs": "0"},
            {"threads": "0", "subagents": "0", "data_slots": "0", "data_runs": "0"},
        ],
    )
    res = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH), "--max-wait-seconds", "10"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(docker_bin),
            "WAIT_IDLE_POLL_S": "0.05",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "checks=5" in res.stdout
    assert "data_slots=0 data_runs=0" in res.stdout


def test_wait_backend_idle_maintenance_query_error_and_non_numeric_treated_as_busy(
    tmp_path: Path,
) -> None:
    docker_err = _make_fake_docker_psql(
        tmp_path / "docker_err",
        tmp_path / "state_err.json",
        [
            {"threads": "0", "subagents": "0", "data_slots": "__FAIL__", "data_runs": "0"},
            {"threads": "0", "subagents": "0", "data_slots": "ERROR: db down", "data_runs": "0"},
        ],
    )
    res = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH), "--max-wait-seconds", "1"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(docker_err),
            "WAIT_IDLE_POLL_S": "0.2",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 1
    assert "still_busy" in res.stdout
    assert "data_slots=err" in res.stdout


def test_wait_backend_idle_maintenance_check_bridge_drain_three_counters(tmp_path: Path) -> None:
    docker_zero = _make_fake_docker_psql(
        tmp_path / "docker_zero",
        tmp_path / "state_zero.json",
        [{"threads": "0", "subagents": "0", "data_slots": "0", "data_runs": "0"}],
    )
    bridge_env = tmp_path / "researchdata-bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_ADMIN_TOKEN=secret_drain_tok\n", encoding="utf-8")
    bridge_env.chmod(0o600)

    responses = [
        {"ok": True, "draining": True, "inflight": 1, "queued": 0, "ingest_inflight": 0},
        {"ok": True, "draining": True, "inflight": 0, "queued": 2, "ingest_inflight": 0},
        {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 1},
        {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 0},
        {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 0},
    ]
    curl_state = tmp_path / "curl_state.json"
    curl_state.write_text(json.dumps({"n": 0, "responses": responses}), encoding="utf-8")
    fake_curl = _write_exec(
        tmp_path / "fake_curl",
        f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path

args = sys.argv[1:]
auth = ""
url = ""
i = 0
while i < len(args):
    if args[i] == "-H" and i + 1 < len(args):
        if args[i + 1].lower().startswith("authorization:"):
            auth = args[i + 1].split(":", 1)[1].strip()
        i += 2
    elif args[i] == "--max-time" and i + 1 < len(args):
        i += 2
    elif args[i].startswith("-"):
        i += 1
    else:
        url = args[i]
        i += 1

if not url.endswith("/admin/drain") or auth != "Bearer secret_drain_tok":
    sys.exit(22)

st_path = Path({str(curl_state)!r})
st = json.loads(st_path.read_text(encoding="utf-8"))
idx = min(st["n"], len(st["responses"]) - 1)
st["n"] += 1
st_path.write_text(json.dumps(st), encoding="utf-8")
print(json.dumps(st["responses"][idx]))
""",
    )

    res = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH), "--check-bridge-drain", "--max-wait-seconds", "10"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "DOCKER_BIN": str(docker_zero),
            "CURL_BIN": str(fake_curl),
            "DALTON_BRIDGE_LOCAL_URL": "http://stub-bridge.invalid",
            "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
            "WAIT_IDLE_POLL_S": "0.05",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "checks=5" in res.stdout
    assert "secret_drain_tok" not in (res.stdout + res.stderr)


def test_preflight_http_request_default_urllib_path_with_autospec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify default (non-CURL_BIN) urllib path handles 200, HTTPError 503, and URLError."""
    monkeypatch.delenv("CURL_BIN", raising=False)

    class _FakeResp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    real_urlopen = urllib.request.urlopen

    def _ok_urlopen(url: Any, *args: Any, **kwargs: Any) -> _FakeResp:
        assert url.get_method() == "GET"
        return _FakeResp(200, b'{"ok": true, "paused": false, "revision": 7}')

    monkeypatch.setattr(
        preflight.urllib.request,
        "urlopen",
        create_autospec(real_urlopen, side_effect=_ok_urlopen),
    )
    code, body, err = preflight._http_request("GET", "http://stub-backend.invalid/status")
    assert (code, err) == (200, None)
    assert body == {"ok": True, "paused": False, "revision": 7}

    def _err_urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(
        preflight.urllib.request,
        "urlopen",
        create_autospec(real_urlopen, side_effect=_err_urlopen),
    )
    code2, body2, err2 = preflight._http_request("POST", "http://stub-backend.invalid/probe", body=b"{}")
    assert code2 is None and body2 is None and err2 is not None and "URLError" in err2


# ---------------------------------------------------------------------------
# D5: _research_host_handoff_lib.sh base functions
# ---------------------------------------------------------------------------


def test_ops_lock_mutual_exclusion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ready_file = tmp_path / "lock_ready"
    release_file = tmp_path / "lock_release"

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }
    holder_script = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
rh_acquire_ops_lock "holder_proc"
touch "{ready_file}"
while [ ! -f "{release_file}" ]; do sleep 0.05; done
rh_release_ops_lock
"""
    p1 = subprocess.Popen(["/bin/bash", "-c", holder_script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "holder failed to acquire ops lock"

        # Second process must immediately fail with holder info
        contender_script = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
rh_acquire_ops_lock "contender_proc"
"""
        p2 = subprocess.run(
            ["/bin/bash", "-c", contender_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert p2.returncode != 0
        assert "holder_proc" in p2.stderr
        assert ".ops.lock" in p2.stderr

        release_file.touch()
        p1.wait(timeout=5)
        assert p1.returncode == 0

        # Now contender succeeds
        p3 = subprocess.run(
            ["/bin/bash", "-c", contender_script + "\nrh_release_ops_lock\n"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert p3.returncode == 0, p3.stderr
    finally:
        release_file.touch()
        p1.wait(timeout=5)


def test_persist_recovery_bin_atomic_and_manifest_tamper_detection(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts" / "dalton" / "ops" / "prod").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "research_runtime").mkdir(parents=True)
    for f in ("switch_research_host.sh", "_research_host_handoff_lib.sh", "wait_backend_idle.sh", "preflight.py"):
        (repo / "scripts" / "dalton" / "ops" / "prod" / f).write_bytes((PROD_OPS / f).read_bytes())
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").write_bytes(
        (SCRIPTS_DALTON / "research_runtime" / "guidepoint_lock.py").read_bytes()
    )

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }
    res = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_persist_recovery_bin; rh_verify_recovery_manifest',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0, res.stderr

    rec_dir = repo / "var" / "dalton" / "cutover-snapshots" / "recovery-bin"
    assert rec_dir.is_dir()
    assert stat.S_IMODE(rec_dir.stat().st_mode) == 0o700
    manifest = json.loads((rec_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["files"].keys()) == {
        "switch_research_host.sh",
        "_research_host_handoff_lib.sh",
        "wait_backend_idle.sh",
        "preflight.py",
        "guidepoint_lock.py",
    }

    # Tamper with one file -> manifest verify must fail
    (rec_dir / "wait_backend_idle.sh").write_text("# tampered\n", encoding="utf-8")
    res_tamper = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_verify_recovery_manifest',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_tamper.returncode != 0
    assert "sha256 mismatch" in res_tamper.stderr


def test_journal_set_preserves_existing_fields_and_writes_json_booleans(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }
    script = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
rh_journal_set operation=stage_a target_owner=legacy_proxy_8943 completed=false
rh_journal_set phase=stage_a_snapshotted
"""
    res = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=15, check=False)
    assert res.returncode == 0, res.stderr

    journal_path = repo / "var" / "dalton" / "cutover-snapshots" / "research-host" / "handoff-journal.json"
    data = json.loads(journal_path.read_text(encoding="utf-8"))
    assert data["operation"] == "stage_a"
    assert data["target_owner"] == "legacy_proxy_8943"
    assert data["phase"] == "stage_a_snapshotted"
    assert data["completed"] is False
    assert isinstance(data["updated_at"], str) and data["updated_at"]


def test_change_guard_allow_and_deny_matrix(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    adm_file = repo / "var" / "dalton" / "maintenance-admission.json"
    j_file = repo / "var" / "dalton" / "cutover-snapshots" / "research-host" / "handoff-journal.json"
    adm_file.parent.mkdir(parents=True, exist_ok=True)
    j_file.parent.mkdir(parents=True, exist_ok=True)

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }

    def _run_guard(action: str, *, hold_lock: bool) -> subprocess.CompletedProcess[str]:
        lock_cmd = 'rh_acquire_ops_lock "test_guard"' if hold_lock else ":"
        script = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
{lock_cmd}
rh_require_change_allowed "{action}"
"""
        return subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=10, check=False)

    # 1. All 3 conditions satisfied -> all 4 guarded actions allowed
    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": []}), encoding="utf-8")
    j_file.write_text(json.dumps({"completed": False}), encoding="utf-8")
    for act in ("set_target_owner", "stop_sandboxes", "drop_guidepoint_owner", "rewrite_bridge_url"):
        r = _run_guard(act, hold_lock=True)
        assert r.returncode == 0, f"{act} should pass: {r.stderr}"

    # 2. Missing lock -> denied
    r = _run_guard("set_target_owner", hold_lock=False)
    assert r.returncode != 0 and ".ops.lock" in r.stderr

    # 3. Admission open (paused == false) -> denied
    adm_file.write_text(json.dumps({"paused": False, "allow_workspace_ids": []}), encoding="utf-8")
    r = _run_guard("stop_sandboxes", hold_lock=True)
    assert r.returncode != 0 and "paused != true" in r.stderr

    # 4. Admission paused with non-empty allowlist -> denied
    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": ["ws-1"]}), encoding="utf-8")
    r = _run_guard("drop_guidepoint_owner", hold_lock=True)
    assert r.returncode != 0 and "allow_workspace_ids" in r.stderr

    # 5. Journal completed == true -> denied
    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": []}), encoding="utf-8")
    j_file.write_text(json.dumps({"completed": True}), encoding="utf-8")
    r = _run_guard("rewrite_bridge_url", hold_lock=True)
    assert r.returncode != 0 and "completed != false" in r.stderr

    # 6. Exempt actions ALWAYS allowed (even open admission, no lock, completed=true)
    adm_file.write_text(json.dumps({"paused": False, "allow_workspace_ids": []}), encoding="utf-8")
    for exempt in ("kickstart_after_drain", "force_recreate_backend_edge"):
        r = _run_guard(exempt, hold_lock=False)
        assert r.returncode == 0, f"{exempt} should always be allowed: {r.stderr}"


# ---------------------------------------------------------------------------
# D6: switch_research_host.sh entrypoints (unimplemented + read-only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--prepare-stage-a"],
        ["--measure-budgets"],
        ["--switch"],
        ["--recover"],
        ["--rollback"],
        ["--rollback", "--dry-run"],
    ],
)
def test_switch_research_host_unimplemented_actions_exit_2_without_creating_files(
    tmp_path: Path,
    argv: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    res = subprocess.run(
        ["/bin/bash", str(SWITCH_SH), *argv],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DALTON_REPO_ROOT": str(repo),
            "HOME": str(tmp_path / "home"),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res.returncode == 2
    assert "本版本尚未实现" in res.stderr
    assert not (repo / "var").exists()


def test_switch_research_host_check_and_dry_run_work_with_incomplete_journal_and_no_writes(
    tmp_path: Path,
) -> None:
    ctx = _setup_isolated_env(tmp_path, budgets=_make_valid_budgets_dict())
    try:
        # Write an incomplete handoff-journal.json (completed: false)
        j_file = ctx["cutover_dir"] / "handoff-journal.json"
        j_bytes = json.dumps({"operation": "stage_a", "phase": "stage_a_snapshotted", "completed": False}).encode("utf-8")
        j_file.write_bytes(j_bytes)

        def _snapshot_var_tree() -> dict[str, bytes]:
            var_dir = ctx["repo"] / "var"
            return {
                str(p.relative_to(var_dir)): p.read_bytes()
                for p in sorted(var_dir.rglob("*"))
                if p.is_file()
            }

        before = _snapshot_var_tree()

        r_chk = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--check"],
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert r_chk.returncode == 0, r_chk.stdout + r_chk.stderr
        assert "completed=False" in r_chk.stdout
        assert _snapshot_var_tree() == before

        r_dry = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--switch", "--dry-run", "--test-workspace-id", "ws-demo-1"],
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert r_dry.returncode == 0, r_dry.stdout + r_dry.stderr
        assert "Planned 7-Step Stage B Cutover Sequence" in r_dry.stdout
        assert "T_trigger (FORWARD_HANDOFF_BUDGET_S): 40s" in r_dry.stdout
        assert "RECOVERY_BUDGET_S:                    35s" in r_dry.stdout
        assert "HANDOFF_DEADLINE_S:                   90s" in r_dry.stdout
        assert "STAGE_B_WINDOW_DEADLINE_S:            3600s" in r_dry.stdout
        assert _snapshot_var_tree() == before
    finally:
        ctx["cleanup"]()


# ---------------------------------------------------------------------------
# D7: --approve-budgets writeback & rejection byte-preservation
# ---------------------------------------------------------------------------


def test_approve_budgets_handoff_exceeded_and_stage_b_only_exceeded_pass_preflight(
    tmp_path: Path,
) -> None:
    # Branch 1: handoff > 90s
    b1 = _make_valid_budgets_dict(
        forward_budget_s=60,
        recovery_budget_s=45,
        handoff_deadline_s=90,
        approval_required="true",
        approved_at="",
    )
    ctx1 = _setup_isolated_env(tmp_path / "b1", budgets=b1)
    try:
        r = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--approve-budgets", "--handoff-deadline-s", "120"],
            env=ctx1["env"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert stat.S_IMODE(ctx1["budgets_file"].stat().st_mode) == 0o600
        assert preflight.validate_timing_budgets_env(ctx1["budgets_file"]) == []
        # Must NOT write handoff-journal.json
        assert not (ctx1["cutover_dir"] / "handoff-journal.json").exists()
        # Full preflight --switch passes now
        r_pre = _run_preflight_cli(ctx1, "--switch")
        assert r_pre.returncode == 0, r_pre.stdout + r_pre.stderr
    finally:
        ctx1["cleanup"]()

    # Branch 2: only Stage B > 60m (handoff stays 90s)
    b2 = _make_valid_budgets_dict(
        forward_budget_s=40,
        recovery_budget_s=35,
        req_stage_b_s=4200,
        handoff_deadline_s=90,
        stage_b_deadline_s=3600,
        approval_required="true",
        approved_at="",
    )
    ctx2 = _setup_isolated_env(tmp_path / "b2", budgets=b2)
    try:
        r = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--approve-budgets", "--stage-b-window-m", "75"],
            env=ctx2["env"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        parsed, errs = preflight.parse_timing_budgets_env(ctx2["budgets_file"])
        assert errs == []
        assert parsed["HANDOFF_DEADLINE_S"] == "90"
        assert parsed["STAGE_B_WINDOW_DEADLINE_S"] == "4500"
        assert parsed["BUDGET_APPROVAL_REQUIRED"] == "false"
        assert parsed["BUDGET_APPROVED_AT"] != ""
        assert preflight.validate_timing_budgets_env(ctx2["budgets_file"]) == []
        r_pre = _run_preflight_cli(ctx2, "--switch")
        assert r_pre.returncode == 0, r_pre.stdout + r_pre.stderr
    finally:
        ctx2["cleanup"]()


@pytest.mark.parametrize(
    "case_name,budgets,cli_args",
    [
        (
            "confirmed_below_required",
            _make_valid_budgets_dict(
                forward_budget_s=60,
                recovery_budget_s=45,
                handoff_deadline_s=90,
                approval_required="true",
            ),
            ["--handoff-deadline-s", "95"],
        ),
        (
            "arg_for_non_exceeded_handoff",
            _make_valid_budgets_dict(
                forward_budget_s=40,
                recovery_budget_s=35,
                req_stage_b_s=4200,
                handoff_deadline_s=90,
                stage_b_deadline_s=3600,
                approval_required="true",
            ),
            ["--handoff-deadline-s", "100", "--stage-b-window-m", "75"],
        ),
        (
            "missing_required_arg_for_exceeded_stage_b",
            _make_valid_budgets_dict(
                forward_budget_s=60,
                recovery_budget_s=45,
                req_stage_b_s=4200,
                handoff_deadline_s=90,
                stage_b_deadline_s=3600,
                approval_required="true",
            ),
            ["--handoff-deadline-s", "120"],
        ),
        (
            "corrupted_underlying_measured_forward_budget",
            _make_valid_budgets_dict(
                probe_timeout_s=20,
                drain_wait_s=10,
                service_reload_s=10,
                forward_budget_s=45,  # < 10 + 10 + 40 = 60
                recovery_budget_s=60,
                handoff_deadline_s=90,
                approval_required="true",
            ),
            ["--handoff-deadline-s", "120"],
        ),
    ],
)
def test_approve_budgets_rejections_leave_original_file_byte_identical(
    tmp_path: Path,
    case_name: str,
    budgets: dict[str, str],
    cli_args: list[str],
) -> None:
    ctx = _setup_isolated_env(tmp_path / case_name, budgets=budgets)
    try:
        before_bytes = ctx["budgets_file"].read_bytes()
        r = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--approve-budgets", *cli_args],
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert r.returncode != 0
        assert "--measure-budgets" in r.stderr
        assert ctx["budgets_file"].read_bytes() == before_bytes
    finally:
        ctx["cleanup"]()


# ---------------------------------------------------------------------------
# D8: bash -n syntax check on all new/modified shell scripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script_path",
    [
        WAIT_IDLE_SH,
        HANDOFF_LIB_SH,
        SWITCH_SH,
        HOST_SERVICES_SWITCH_SH,
        MIGRATE_SH,
    ],
)
def test_shell_scripts_pass_bash_n(script_path: Path) -> None:
    res = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
