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

import http.client
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
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
INSTALL_BRIDGE_SH = SCRIPTS_DALTON / "install_bridge.sh"
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
import json, os, re, stat, sys
from urllib.parse import urlparse

cfg = {cfg!r}
args = sys.argv[1:]
# Credentials must NEVER be passed via process argv (-H)
for a in args:
    low = a.lower()
    if "authorization:" in low or "x-service-token:" in low:
        sys.stderr.write("SECURITY VIOLATION: credential header passed on argv\\n")
        sys.exit(99)
    for secret in (cfg["svc_token"], cfg["bridge_admin_tok"], cfg["gp_admin_tok"]):
        if secret and secret in a:
            sys.stderr.write("SECURITY VIOLATION: secret token passed on argv\\n")
            sys.exit(99)

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
    elif a in ("-K", "--config") and i + 1 < len(args):
        cfg_file = args[i + 1]
        st = os.stat(cfg_file)
        if stat.S_IMODE(st.st_mode) != 0o600:
            sys.stderr.write(f"SECURITY VIOLATION: curl config mode {{oct(stat.S_IMODE(st.st_mode))}}\\n")
            sys.exit(98)
        with open(cfg_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'^\\s*header\\s*=\\s*"(.*)"\\s*$', line)
                if m:
                    hv = m.group(1).replace('\\\\"', '"').replace('\\\\\\\\', '\\\\')
                    if ":" in hv:
                        k, v = hv.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
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
    stage_a_completed_layout: bool = True,
    write_stage_a_journal: bool = True,
) -> dict[str, Any]:
    """Create a complete synthetic repo, home, LaunchAgents, web/releases, stubs & servers.

    When `stage_a_completed_layout=True` (default, matching real post-Stage-A production layout),
    `com.dalton.research-host.plist` is archived out of `LaunchAgents/` into
    `var/dalton/cutover-snapshots/stage-a/<snap>/archived-launchagents/` and recorded in
    `handoff-journal.json`. Pass `stage_a_completed_layout=False` for `--stage-a` staging tests.
    """
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
    (repo / "scripts" / "dalton" / "bridge_subprocess_env.py").symlink_to(
        SCRIPTS_DALTON / "bridge_subprocess_env.py"
    )
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

    # timing-budgets.env & Stage A archived research-host plist layout
    cutover_dir = repo / "var" / "dalton" / "cutover-snapshots" / "research-host"
    cutover_dir.mkdir(parents=True, exist_ok=True)
    stage_a_snap = repo / "var" / "dalton" / "cutover-snapshots" / "stage-a" / "20261127-1a0aaf82e"
    archived_dir = stage_a_snap / "archived-launchagents"
    archived_host_plist = archived_dir / "com.dalton.research-host.plist"
    if stage_a_completed_layout:
        archived_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plist_dir / "com.dalton.research-host.plist"), str(archived_host_plist))
        if write_stage_a_journal:
            (cutover_dir / "handoff-journal.json").write_text(
                json.dumps(
                    {
                        "operation": "stage_a",
                        "phase": "stage_a_completed",
                        "target_owner": "legacy_proxy_8943",
                        "snapshot_dir": str(stage_a_snap),
                        "completed": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
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
        "archived_host_plist": archived_host_plist,
        "stage_a_snap": stage_a_snap,
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
    ctx = _setup_isolated_env(
        tmp_path,
        budgets=_make_valid_budgets_dict(),
        backend_paused=False,
        stage_a_completed_layout=False,
    )
    try:
        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode == 0, res.stdout + res.stderr

        res_open = _run_preflight_cli(ctx, "--stage-a", "--expect-admission", "open")
        assert res_open.returncode == 0, res_open.stdout + res_open.stderr

        res_paused_fail = _run_preflight_cli(ctx, "--stage-a", "--expect-admission", "paused")
        assert res_paused_fail.returncode != 0
    finally:
        ctx["cleanup"]()

    ctx_p = _setup_isolated_env(
        tmp_path / "paused",
        budgets=_make_valid_budgets_dict(),
        backend_paused=True,
        stage_a_completed_layout=False,
    )
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
    # 1. Healthy pre-switch bridge process carrying DALTON_GUIDEPOINT_MCP_URL matching bridge.env -> PASS
    ctx_ok = _setup_isolated_env(
        tmp_path / "healthy_matching",
        budgets=_make_valid_budgets_dict(),
        ps_output_override="PID COMMAND\n5151 python researchdata_bridge.py DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp\n",
    )
    try:
        with ctx_ok["bridge_env"].open("a", encoding="utf-8") as fh:
            fh.write("DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp\n")
        res_ok = _run_preflight_cli(ctx_ok, "--switch")
        assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    finally:
        ctx_ok["cleanup"]()

    # 2. Residual :8943 in bridge proc while bridge.env / stage already points to :8951 -> FAIL
    ctx = _setup_isolated_env(
        tmp_path / "override",
        budgets=_make_valid_budgets_dict(),
        ps_output_override="PID COMMAND\n5151 python researchdata_bridge.py DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp\n",
    )
    try:
        with ctx["bridge_env"].open("a", encoding="utf-8") as fh:
            fh.write("DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n")
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode != 0
        assert "DALTON_GUIDEPOINT_MCP_URL" in (res.stderr + res.stdout)
    finally:
        ctx["cleanup"]()

    # 3. Rogue URL with embedded credentials in bridge proc -> FAIL and secret redacted from output
    ctx_red = _setup_isolated_env(
        tmp_path / "redact",
        budgets=_make_valid_budgets_dict(),
        ps_output_override="PID COMMAND\n5151 python researchdata_bridge.py DALTON_GUIDEPOINT_MCP_URL=http://admin:topsecret_pw@127.0.0.1:9999/mcp?tok=abc\n",
    )
    try:
        res_red = _run_preflight_cli(ctx_red, "--switch")
        assert res_red.returncode != 0
        combined = res_red.stderr + res_red.stdout
        assert "DALTON_GUIDEPOINT_MCP_URL" in combined
        assert "topsecret_pw" not in combined
        assert "tok=abc" not in combined
    finally:
        ctx_red["cleanup"]()

    # 4. launchctl getenv external DALTON_GUIDEPOINT_MCP_URL override -> FAIL even on :8943
    ctx_lc = _setup_isolated_env(
        tmp_path / "launchctl_override",
        budgets=_make_valid_budgets_dict(),
        launchctl_getenv_map={"DALTON_GUIDEPOINT_MCP_URL": "http://127.0.0.1:8943/mcp"},
    )
    try:
        res_lc = _run_preflight_cli(ctx_lc, "--switch")
        assert res_lc.returncode != 0
        assert "DALTON_GUIDEPOINT_MCP_URL" in (res_lc.stderr + res_lc.stdout)
    finally:
        ctx_lc["cleanup"]()

    # 5. ps fails (unreadable process env) -> WARN, exit 0
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
    ctx = _setup_isolated_env(
        tmp_path,
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    try:
        hp = ctx["plist_dir"] / "com.dalton.research-host.plist"
        data = plistlib.loads(hp.read_bytes())
        data["EnvironmentVariables"]["DALTON_RESEARCH_HOME"] = "$HOME/Library/Application Support/Everflow/research"
        hp.write_bytes(plistlib.dumps(data))

        res = _run_preflight_cli(ctx, "--stage-a")
        assert res.returncode != 0
        combined = res.stderr + res.stdout
        assert "DALTON_RESEARCH_HOME" in combined
        assert "unexpanded_shell_variable" in combined
        # Raw field value must NOT leak into diagnostics
        assert "$HOME/Library/Application Support/Everflow/research" not in combined
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
    ctx = _setup_isolated_env(
        tmp_path / break_kind,
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
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
        stage_a_completed_layout=False,
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
        stage_a_completed_layout=False,
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
    curl_state.write_text(json.dumps({"n": 0, "responses": responses, "cfg_paths": []}), encoding="utf-8")
    fake_curl = _write_exec(
        tmp_path / "fake_curl",
        f"""#!/usr/bin/env python3
import json, os, re, stat, sys
from pathlib import Path

args = sys.argv[1:]
for a in args:
    if "authorization" in a.lower() or "secret_drain_tok" in a:
        sys.stderr.write("SECURITY VIOLATION: auth token on curl argv\\n")
        sys.exit(99)

auth = ""
url = ""
cfg_paths = []
i = 0
while i < len(args):
    if args[i] in ("-K", "--config") and i + 1 < len(args):
        cfg_file = args[i + 1]
        cfg_paths.append(cfg_file)
        st = os.stat(cfg_file)
        if stat.S_IMODE(st.st_mode) != 0o600:
            sys.stderr.write("SECURITY VIOLATION: curl config not 0600\\n")
            sys.exit(98)
        with open(cfg_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r'^\\s*header\\s*=\\s*"(.*)"\\s*$', line)
                if m and m.group(1).lower().startswith("authorization:"):
                    auth = m.group(1).split(":", 1)[1].strip()
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
st.setdefault("cfg_paths", []).extend(cfg_paths)
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
    # Every temporary 0600 curl config file must have been deleted after use
    recorded = json.loads(curl_state.read_text(encoding="utf-8"))["cfg_paths"]
    assert len(recorded) == 5
    for p in recorded:
        assert not Path(p).exists(), f"temp curl config leaked: {p}"


def test_preflight_http_request_default_urllib_path_with_autospec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify default (non-CURL_BIN) urllib path handles 200, HTTPError 503, and URLError using create_autospec."""
    monkeypatch.delenv("CURL_BIN", raising=False)

    real_urlopen = urllib.request.urlopen

    def _ok_urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        assert url.get_method() == "GET"
        resp = create_autospec(http.client.HTTPResponse, instance=True)
        resp.status = 200
        resp.read.return_value = b'{"ok": true, "paused": false, "revision": 7}'
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        return resp

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
    for f in (
        "rollback_research_host.sh",
        "snapshot_research_host.sh",
        "_research_host_handoff_lib.sh",
        "wait_backend_idle.sh",
        "preflight.py",
    ):
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
        "rollback_research_host.sh",
        "snapshot_research_host.sh",
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
    adm_file = repo / "var" / "dalton" / "maintenance-admission.json"
    adm_file.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }
    # Step 1: initial intent write (without target_owner) succeeds before admission pause
    script = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
rh_acquire_ops_lock "test_jset"
rh_journal_set operation=stage_a phase=stage_a_pausing_admission completed=false
rh_journal_set phase=stage_a_snapshotted
"""
    res = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=15, check=False)
    assert res.returncode == 0, res.stderr

    # Step 2: writing target_owner while admission is fully paused + completed=false succeeds and preserves fields
    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": []}), encoding="utf-8")
    script_owner = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
rh_acquire_ops_lock "test_jset_owner"
rh_journal_set target_owner=legacy_proxy_8943
"""
    res2 = subprocess.run(
        ["/bin/bash", "-c", script_owner], env=env, capture_output=True, text=True, timeout=15, check=False
    )
    assert res2.returncode == 0, res2.stderr

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
# D6: switch_research_host.sh entrypoints (retired automation + read-only --check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--prepare-stage-a"],
        ["--measure-budgets"],
        ["--approve-budgets"],
        ["--switch"],
        ["--switch", "--dry-run"],
        ["--recover"],
        ["--rollback"],
        ["--rollback", "--dry-run"],
    ],
)
def test_switch_research_host_retired_automation_actions_exit_2_without_creating_files(
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
    assert "retired" in res.stderr
    assert "rollback_research_host.sh" in res.stderr
    assert not (repo / "var").exists()


def test_switch_research_host_check_works_with_incomplete_journal_and_no_writes(
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

        r_skip = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--check", "--skip-budgets", "--report-json"],
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert r_skip.returncode == 0, r_skip.stdout + r_skip.stderr
        parsed = json.loads(r_skip.stdout)
        assert parsed["ok"] is True
        assert _snapshot_var_tree() == before
    finally:
        ctx["cleanup"]()


# ---------------------------------------------------------------------------
# D7: preflight.py approve-budgets writeback & rejection byte-preservation
# ---------------------------------------------------------------------------


def test_approve_budgets_handoff_exceeded_and_stage_b_only_exceeded_pass_preflight(
    tmp_path: Path,
) -> None:
    # Branch 1: handoff > 90s (requires existing handoff-journal.json with completed == true)
    b1 = _make_valid_budgets_dict(
        forward_budget_s=60,
        recovery_budget_s=45,
        handoff_deadline_s=90,
        approval_required="true",
        approved_at="",
    )
    ctx1 = _setup_isolated_env(tmp_path / "b1", budgets=b1)
    try:
        j1 = ctx1["cutover_dir"] / "handoff-journal.json"
        j1_bytes = json.dumps(
            {"operation": "stage_a", "target_owner": "legacy_proxy_8943", "completed": True}
        ).encode("utf-8")
        j1.write_bytes(j1_bytes)
        r = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PREFLIGHT_PY),
                "approve-budgets",
                "--repo",
                str(ctx1["repo"]),
                "--budgets-file",
                str(ctx1["budgets_file"]),
                "--handoff-deadline-s",
                "120",
            ],
            env=ctx1["env"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert stat.S_IMODE(ctx1["budgets_file"].stat().st_mode) == 0o600
        assert preflight.validate_timing_budgets_env(ctx1["budgets_file"]) == []
        # Must NOT modify handoff-journal.json
        assert j1.read_bytes() == j1_bytes
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
        j2 = ctx2["cutover_dir"] / "handoff-journal.json"
        j2_bytes = json.dumps(
            {"operation": "stage_a", "target_owner": "legacy_proxy_8943", "completed": True}
        ).encode("utf-8")
        j2.write_bytes(j2_bytes)
        r = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PREFLIGHT_PY),
                "approve-budgets",
                "--repo",
                str(ctx2["repo"]),
                "--budgets-file",
                str(ctx2["budgets_file"]),
                "--stage-b-window-m",
                "75",
            ],
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
        assert j2.read_bytes() == j2_bytes
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
        (ctx["cutover_dir"] / "handoff-journal.json").write_text(
            json.dumps({"operation": "stage_a", "target_owner": "legacy_proxy_8943", "completed": True}),
            encoding="utf-8",
        )
        before_bytes = ctx["budgets_file"].read_bytes()
        r = subprocess.run(
            [
                sys.executable,
                "-B",
                str(PREFLIGHT_PY),
                "approve-budgets",
                "--repo",
                str(ctx["repo"]),
                "--budgets-file",
                str(ctx["budgets_file"]),
                *cli_args,
            ],
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
# s34-r2 hardening regression tests (Codex + Grok review items 1-12)
# ---------------------------------------------------------------------------


def test_wait_backend_idle_maintenance_enforces_monotonic_deadline_on_hanging_psql_and_http(
    tmp_path: Path,
) -> None:
    # 1. Hanging docker exec psql must be cut off by --max-wait-seconds
    hanging_docker = _write_exec(
        tmp_path / "docker_hang",
        "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\nprint('0')\n",
    )
    t0 = time.monotonic()
    res_hang = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH), "--max-wait-seconds", "1"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(hanging_docker),
            "WAIT_IDLE_POLL_S": "0.1",
        },
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    elapsed = time.monotonic() - t0
    assert res_hang.returncode == 1
    assert "still_busy" in res_hang.stdout
    assert elapsed < 4.0, f"hanging psql was not bounded by deadline: {elapsed:.2f}s"

    # 2. Slow queries returning 0 twice AFTER deadline must STILL fail closed (not return 0)
    slow_zero_docker = _write_exec(
        tmp_path / "docker_slow_zero",
        "#!/usr/bin/env python3\nimport time\ntime.sleep(0.2)\nprint('0')\n",
    )
    res_overrun = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH), "--max-wait-seconds", "1"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(slow_zero_docker),
            "WAIT_IDLE_POLL_S": "0.1",
        },
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert res_overrun.returncode == 1
    assert "still_busy" in res_overrun.stdout

    # 3. Hanging HTTP healthz must be cut off by the same monotonic deadline
    fast_docker = _write_exec(
        tmp_path / "docker_fast",
        "#!/usr/bin/env python3\nprint('0')\n",
    )
    hanging_curl = _write_exec(
        tmp_path / "curl_hang",
        "#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n",
    )
    t1 = time.monotonic()
    res_http_hang = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH), "--check-bridge-healthz", "--max-wait-seconds", "1"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(fast_docker),
            "CURL_BIN": str(hanging_curl),
            "DALTON_BRIDGE_LOCAL_URL": "http://stub-bridge.invalid",
            "WAIT_IDLE_POLL_S": "0.1",
        },
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert res_http_hang.returncode == 1
    assert "still_busy" in res_http_hang.stdout
    assert (time.monotonic() - t1) < 4.0

    # 4. No-arg mode ignores POSTGRES_CONTAINER (uses dalton-langalpha-postgres-1)
    container_log = tmp_path / "pg_container.log"
    inspect_docker = _write_exec(
        tmp_path / "docker_inspect",
        f"""#!/usr/bin/env python3
import sys
from pathlib import Path
Path({str(container_log)!r}).write_text(sys.argv[2], encoding="utf-8")
print("0")
""",
    )
    res_noarg = subprocess.run(
        ["/bin/bash", str(WAIT_IDLE_SH)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DOCKER_BIN": str(inspect_docker),
            "POSTGRES_CONTAINER": "langalpha-postgresql",
            "WAIT_IDLE_DEFAULT_POLL_S": "0",
            "WAIT_IDLE_DEFAULT_MAX_ROUNDS": "3",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res_noarg.returncode == 0
    assert container_log.read_text(encoding="utf-8") == "dalton-langalpha-postgres-1"


@pytest.mark.parametrize("failure_mode", ["nonzero_rc", "corrupt_json", "missing_backend_env"])
def test_preflight_switch_fails_closed_on_compose_errors_and_honors_compose_file_chain(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    ctx = _setup_isolated_env(tmp_path / failure_mode, budgets=_make_valid_budgets_dict())
    try:
        docker_bin = Path(ctx["env"]["DOCKER_BIN"])
        if failure_mode == "nonzero_rc":
            _write_exec(docker_bin, "#!/usr/bin/env python3\nimport sys\nsys.stderr.write('compose boom\\n')\nsys.exit(1)\n")
        elif failure_mode == "corrupt_json":
            _write_exec(docker_bin, "#!/usr/bin/env python3\nprint('{not valid json')\n")
        else:
            _write_exec(
                docker_bin,
                "#!/usr/bin/env python3\nimport json\nprint(json.dumps({'services': {'backend': {'image': 'x'}}}))\n",
            )
        res = _run_preflight_cli(ctx, "--switch", "--report-json")
        assert res.returncode != 0
        rep = json.loads(res.stdout)
        by_name = {c["check"]: c for c in rep["checks"]}
        assert by_name["three_way_capability_parity"]["status"] == "fail"
    finally:
        ctx["cleanup"]()

    # Verify COMPOSE_FILE chain + --project-directory propagation when valid and fail-closed when missing
    ctx_cf = _setup_isolated_env(tmp_path / "compose_chain", budgets=_make_valid_budgets_dict())
    try:
        custom_a = ctx_cf["repo"] / "compose.custom.yml"
        custom_a.write_text("services: {}\n", encoding="utf-8")
        with (ctx_cf["repo"] / ".env").open("a", encoding="utf-8") as fh:
            fh.write("COMPOSE_FILE=compose.custom.yml:docker-compose.dalton.yml\n")

        args_log = tmp_path / "compose_args.json"
        docker_bin = Path(ctx_cf["env"]["DOCKER_BIN"])
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
        _write_exec(
            docker_bin,
            f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
print(json.dumps({compose_payload!r}))
""",
        )
        res_ok = _run_preflight_cli(ctx_cf, "--switch")
        assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
        argv = json.loads(args_log.read_text(encoding="utf-8"))
        assert "--project-directory" in argv
        assert str(ctx_cf["repo"]) in argv
        assert str(custom_a) in argv

        # Missing explicit COMPOSE_FILE entry -> fail closed
        custom_a.unlink()
        res_miss = _run_preflight_cli(ctx_cf, "--switch", "--report-json")
        assert res_miss.returncode != 0
        rep_m = json.loads(res_miss.stdout)
        by_name_m = {c["check"]: c for c in rep_m["checks"]}
        assert by_name_m["three_way_capability_parity"]["status"] == "fail"
        assert "COMPOSE_FILE" in by_name_m["three_way_capability_parity"]["detail"]
    finally:
        ctx_cf["cleanup"]()


def test_preflight_switch_warns_on_unreadable_plist_and_missing_pid_and_handles_spaces_in_ps(
    tmp_path: Path,
) -> None:
    # 1. Corrupt extra plist -> env_override_scan reports warn (not silent ok)
    ctx_p = _setup_isolated_env(tmp_path / "corrupt_plist", budgets=_make_valid_budgets_dict())
    try:
        (ctx_p["plist_dir"] / "com.dalton.broken-extra.plist").write_text("<not valid plist", encoding="utf-8")
        res_p = _run_preflight_cli(ctx_p, "--switch", "--report-json")
        assert res_p.returncode == 0, res_p.stdout + res_p.stderr
        by_name = {c["check"]: c for c in json.loads(res_p.stdout)["checks"]}
        assert by_name["env_override_scan"]["status"] == "warn"
        assert "com.dalton.broken-extra.plist" in by_name["env_override_scan"]["detail"]
    finally:
        ctx_p["cleanup"]()

    # 2. Missing bridge PID in launchctl -> env_override_scan reports warn
    ctx_pid = _setup_isolated_env(
        tmp_path / "missing_bridge_pid",
        budgets=_make_valid_budgets_dict(),
        bridge_pid=None,
    )
    try:
        res_pid = _run_preflight_cli(ctx_pid, "--switch", "--report-json")
        assert res_pid.returncode == 0, res_pid.stdout + res_pid.stderr
        by_name = {c["check"]: c for c in json.loads(res_pid.stdout)["checks"]}
        assert by_name["env_override_scan"]["status"] == "warn"
        assert "com.dalton.researchdata-bridge" in by_name["env_override_scan"]["detail"]
    finally:
        ctx_pid["cleanup"]()

    # 3. _extract_proc_env_vars preserves paths with spaces
    spaced_path = "/Users/example/Library/Application Support/Everflow/researchdata-bridge.env"
    ps_line = (
        "  PID TT STAT TIME COMMAND\n"
        f" 5151 ?? S 0:01 /opt/py researchdata_bridge.py HOME=/Users/example "
        f"DALTON_BRIDGE_ENV_FILE={spaced_path} PATH=/usr/bin:/bin\n"
    )
    extracted = preflight._extract_proc_env_vars(ps_line, {"DALTON_BRIDGE_ENV_FILE"})
    assert extracted["DALTON_BRIDGE_ENV_FILE"] == spaced_path


def test_preflight_uses_real_proxy_loading_order_source_overrides_plist(tmp_path: Path) -> None:
    # Stage A layout
    ctx_a = _setup_isolated_env(
        tmp_path / "stage_a",
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    try:
        other_tokens = tmp_path / "sourced_other_tokens_a.json"
        other_tokens.write_text(ctx_a["tokens_file"].read_text(encoding="utf-8"), encoding="utf-8")
        with ctx_a["gp_env"].open("a", encoding="utf-8") as fh:
            fh.write(f"GUIDEPOINT_TOKENS_FILE={other_tokens}\n")

        res_a = _run_preflight_cli(ctx_a, "--stage-a", "--report-json")
        assert res_a.returncode != 0
        by_a = {c["check"]: c for c in json.loads(res_a.stdout)["checks"]}
        assert by_a["guidepoint_tokens_json"]["status"] == "fail"
        assert "mismatch" in by_a["guidepoint_tokens_json"]["detail"]
    finally:
        ctx_a["cleanup"]()

    # Stage B layout (archived research-host plist)
    ctx_s = _setup_isolated_env(tmp_path / "stage_b", budgets=_make_valid_budgets_dict())
    try:
        other_tokens_s = tmp_path / "sourced_other_tokens_s.json"
        other_tokens_s.write_text(ctx_s["tokens_file"].read_text(encoding="utf-8"), encoding="utf-8")
        with ctx_s["gp_env"].open("a", encoding="utf-8") as fh:
            fh.write(f"GUIDEPOINT_TOKENS_FILE={other_tokens_s}\n")

        res_s = _run_preflight_cli(ctx_s, "--switch", "--report-json")
        assert res_s.returncode != 0
        by_s = {c["check"]: c for c in json.loads(res_s.stdout)["checks"]}
        assert by_s["two_sided_guidepoint_paths"]["status"] == "fail"
        assert "tokens path mismatch" in by_s["two_sided_guidepoint_paths"]["detail"]
    finally:
        ctx_s["cleanup"]()

    # Also check ownership override inside sourced guidepoint_mcp.env
    ctx2 = _setup_isolated_env(tmp_path / "own_override", budgets=_make_valid_budgets_dict())
    try:
        other_own = tmp_path / "other-journal.json"
        with ctx2["gp_env"].open("a", encoding="utf-8") as fh:
            fh.write(f"DALTON_GUIDEPOINT_OWNERSHIP_FILE={other_own}\n")
        res_s2 = _run_preflight_cli(ctx2, "--switch", "--report-json")
        assert res_s2.returncode != 0
        by_s2 = {c["check"]: c for c in json.loads(res_s2.stdout)["checks"]}
        assert by_s2["two_sided_guidepoint_paths"]["status"] == "fail"
        assert "handoff-journal path mismatch" in by_s2["two_sided_guidepoint_paths"]["detail"]
    finally:
        ctx2["cleanup"]()


def test_preflight_rejects_research_host_startup_args_drift(tmp_path: Path) -> None:
    ctx = _setup_isolated_env(
        tmp_path,
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    try:
        hp = ctx["plist_dir"] / "com.dalton.research-host.plist"
        data = plistlib.loads(hp.read_bytes())
        alt_env = tmp_path / "alt-research-host.env"
        alt_env.write_text("DALTON_RESEARCH_HOST_TOKEN=x\n", encoding="utf-8")
        idx = data["ProgramArguments"].index("--env-file")
        data["ProgramArguments"][idx + 1] = str(alt_env)
        hp.write_bytes(plistlib.dumps(data))

        res = _run_preflight_cli(ctx, "--stage-a", "--report-json")
        assert res.returncode != 0
        by_name = {c["check"]: c for c in json.loads(res.stdout)["checks"]}
        assert by_name["stage_a_plists"]["status"] == "fail"
        assert "--env-file" in by_name["stage_a_plists"]["detail"]
    finally:
        ctx["cleanup"]()


def test_render_stage_a_plists_reconciles_live_legacy_proxy_paths_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
    out_dir = base / "out"
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
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")

    # Existing live legacy proxy points to a custom guidepoint_mcp.env that overrides GUIDEPOINT_TOKENS_FILE
    live_gp_dir = base / "live_gp"
    live_gp_dir.mkdir()
    live_tokens = live_gp_dir / "actual_live_tokens.json"
    live_gp_env = live_gp_dir / "guidepoint_mcp.env"
    live_gp_env.write_text(f"GUIDEPOINT_TOKENS_FILE={live_tokens}\n", encoding="utf-8")

    live_proxy_plist = home / "Library" / "LaunchAgents" / "com.dalton.guidepoint-mcp-proxy.plist"
    live_proxy_plist.parent.mkdir(parents=True)
    live_proxy_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.guidepoint-mcp-proxy",
                "EnvironmentVariables": {
                    "GUIDEPOINT_ENV_FILE": str(live_gp_env),
                    "GUIDEPOINT_TOKENS_FILE": str(base / "stale_plist_tokens.json"),
                },
            }
        )
    )

    # Stage A render reconciles BOTH plists to live_tokens (from sourced guidepoint_mcp.env)
    bse.render_stage_a_plists_atomic(
        output_dir=out_dir,
        repo_root=repo,
        home_dir=home,
        bridge_env_path=bridge_env,
        research_host_env_path=host_env,
        existing_proxy_plist_path=live_proxy_plist,
    )
    rh = plistlib.loads((out_dir / "com.dalton.research-host.plist").read_bytes())
    gp = plistlib.loads((out_dir / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes())
    assert rh["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == str(live_tokens)
    assert gp["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == str(live_tokens)

    # Conflict 1: research-host.env explicitly sets a DIFFERENT GUIDEPOINT_TOKENS_FILE -> rejected
    host_env.write_text(
        f"DALTON_RESEARCH_HOST_TOKEN=t1\nGUIDEPOINT_TOKENS_FILE={base / 'conflicting_host_tokens.json'}\n",
        encoding="utf-8",
    )
    with pytest.raises(bse.EnvFileValidationError, match="conflicts with live legacy proxy"):
        bse.render_stage_a_plists_atomic(
            output_dir=base / "out_conflict1",
            repo_root=repo,
            home_dir=home,
            bridge_env_path=bridge_env,
            research_host_env_path=host_env,
            existing_proxy_plist_path=live_proxy_plist,
        )

    # Conflict 2: sourced guidepoint_mcp.env overrides DALTON_GUIDEPOINT_OWNERSHIP_FILE -> rejected
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")
    live_gp_env.write_text(
        f"GUIDEPOINT_TOKENS_FILE={live_tokens}\nDALTON_GUIDEPOINT_OWNERSHIP_FILE={base / 'wrong_own.json'}\n",
        encoding="utf-8",
    )
    with pytest.raises(bse.EnvFileValidationError, match="overrides ownership file"):
        bse.render_stage_a_plists_atomic(
            output_dir=base / "out_conflict2",
            repo_root=repo,
            home_dir=home,
            bridge_env_path=bridge_env,
            research_host_env_path=host_env,
            existing_proxy_plist_path=live_proxy_plist,
        )


def test_render_stage_a_plists_writes_only_inside_output_dir_and_is_all_or_nothing(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
    out_dir = base / "out"
    nonexistent_logs_dir = base / "outside_logs_must_not_be_created"
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
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")

    # 1. Valid render does NOT create logs_dir outside output_dir
    bse.render_stage_a_plists_atomic(
        output_dir=out_dir,
        repo_root=repo,
        home_dir=home,
        bridge_env_path=bridge_env,
        research_host_env_path=host_env,
        logs_dir=nonexistent_logs_dir,
    )
    assert not nonexistent_logs_dir.exists()
    assert len(list(out_dir.iterdir())) == 3

    # 2. Failure on 3rd template leaves a fresh output_dir with ZERO published plists
    out_fail = base / "out_fail"
    (repo / "scripts" / "dalton" / "launchd" / "com.dalton.guidepoint-mcp-proxy.plist.template").write_text(
        "<plist><broken>__UNKNOWN_PLACEHOLDER__</plist>", encoding="utf-8"
    )
    with pytest.raises(bse.EnvFileValidationError):
        bse.render_stage_a_plists_atomic(
            output_dir=out_fail,
            repo_root=repo,
            home_dir=home,
            bridge_env_path=bridge_env,
            research_host_env_path=host_env,
            logs_dir=nonexistent_logs_dir,
        )
    assert list(out_fail.iterdir()) == []
    assert not nonexistent_logs_dir.exists()

    # 3. host_services_switch.sh delegates rendering to unified bridge_subprocess_env.py
    hss_text = HOST_SERVICES_SWITCH_SH.read_text(encoding="utf-8")
    assert "render-host-service-plist" in hss_text
    assert 'with open(out_path, "w"' not in hss_text


def test_persist_recovery_bin_atomic_symlink_swap_and_failure_preserves_previous(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts" / "dalton" / "ops" / "prod").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "research_runtime").mkdir(parents=True)
    for f in (
        "rollback_research_host.sh",
        "snapshot_research_host.sh",
        "_research_host_handoff_lib.sh",
        "wait_backend_idle.sh",
        "preflight.py",
    ):
        (repo / "scripts" / "dalton" / "ops" / "prod" / f).write_bytes((PROD_OPS / f).read_bytes())
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").write_bytes(
        (SCRIPTS_DALTON / "research_runtime" / "guidepoint_lock.py").read_bytes()
    )

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }
    rec_link = repo / "var" / "dalton" / "cutover-snapshots" / "recovery-bin"

    # First publish -> symlink to .recovery-bin.rel.*
    r1 = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_persist_recovery_bin commit_v1'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r1.returncode == 0, r1.stderr
    assert rec_link.is_symlink()
    target_v1 = os.readlink(rec_link)
    assert target_v1.startswith(".recovery-bin.rel.")
    assert json.loads((rec_link / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v1"

    # Second publish -> atomic symlink swap to new sibling release, old release cleaned up
    r2 = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_persist_recovery_bin commit_v2; rh_verify_recovery_manifest'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r2.returncode == 0, r2.stderr
    assert rec_link.is_symlink()
    target_v2 = os.readlink(rec_link)
    assert target_v2 != target_v1
    assert not (rec_link.parent / target_v1).exists()
    assert json.loads((rec_link / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v2"

    # Publish failure when existing symlink is present (missing source file) -> previous v2 preserved intact
    saved_gp = (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").read_bytes()
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").unlink()
    r_fail = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_persist_recovery_bin commit_v3'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_fail.returncode != 0
    assert rec_link.is_symlink() and os.readlink(rec_link) == target_v2
    r_ver = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_verify_recovery_manifest'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_ver.returncode == 0, r_ver.stderr
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").write_bytes(saved_gp)

    # Legacy real-directory migration: atomic single-syscall swap (`RENAME_SWAP` / `RENAME_EXCHANGE`)
    old_target_dir = rec_link.parent / target_v2
    rec_link.unlink()
    shutil.move(str(old_target_dir), str(rec_link))
    assert rec_link.is_dir() and not rec_link.is_symlink()

    # 1) Atomic swap failure (injected _atomic_swap_paths OSError) -> aborts & preserves real dir untouched!
    src_dir = repo / "scripts" / "dalton" / "ops" / "prod"
    gp_lock_src = repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py"
    orig_swap = preflight._atomic_swap_paths
    try:
        def _fail_swap(s: Path, d: Path) -> None:
            raise OSError("simulated atomic swap failure")

        preflight._atomic_swap_paths = create_autospec(orig_swap, side_effect=_fail_swap)
        with pytest.raises(OSError, match="simulated atomic swap failure"):
            preflight.publish_recovery_bin_atomic(
                repo_root=repo,
                src_dir=src_dir,
                gp_lock_src=gp_lock_src,
                source_commit="commit_v4_fail",
            )
    finally:
        preflight._atomic_swap_paths = orig_swap
    assert rec_link.is_dir() and not rec_link.is_symlink()
    assert json.loads((rec_link / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v2"
    # Staging release dir & temp symlink cleaned up
    leftover_tmp = [p.name for p in rec_link.parent.iterdir() if p.name.startswith(".recovery-bin.")]
    assert leftover_tmp == []

    # 2) Injected fsync failure during publish -> aborts immediately & preserves real dir untouched!
    orig_fsync = os.fsync
    try:
        def _fail_fsync(fd: int) -> None:
            raise OSError("simulated fsync EIO")

        os.fsync = create_autospec(orig_fsync, side_effect=_fail_fsync)  # type: ignore[assignment]
        with pytest.raises(OSError, match="simulated fsync EIO"):
            preflight.publish_recovery_bin_atomic(
                repo_root=repo,
                src_dir=src_dir,
                gp_lock_src=gp_lock_src,
                source_commit="commit_v4_fsync_fail",
            )
    finally:
        os.fsync = orig_fsync  # type: ignore[assignment]
    assert rec_link.is_dir() and not rec_link.is_symlink()
    assert json.loads((rec_link / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v2"

    # 3) Healthy atomic migration from legacy real directory -> symlink in one swap!
    r_mig = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_persist_recovery_bin commit_v5_migrated; rh_verify_recovery_manifest'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_mig.returncode == 0, r_mig.stderr
    assert rec_link.is_symlink()
    assert json.loads((rec_link / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v5_migrated"


def test_recovery_manifest_requires_exact_five_files_and_trusted_entry_and_same_batch_import(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts" / "dalton" / "ops" / "prod").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "research_runtime").mkdir(parents=True)
    for f in (
        "rollback_research_host.sh",
        "snapshot_research_host.sh",
        "_research_host_handoff_lib.sh",
        "wait_backend_idle.sh",
        "preflight.py",
    ):
        (repo / "scripts" / "dalton" / "ops" / "prod" / f).write_bytes((PROD_OPS / f).read_bytes())
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").write_bytes(
        (SCRIPTS_DALTON / "research_runtime" / "guidepoint_lock.py").read_bytes()
    )

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }
    r_pub = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_persist_recovery_bin'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_pub.returncode == 0, r_pub.stderr
    rec_dir = repo / "var" / "dalton" / "cutover-snapshots" / "recovery-bin"

    # 1. Same-batch guidepoint_lock import: break worktree's guidepoint_lock.py; recovery-bin/preflight.py must import its sibling copy!
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").write_text(
        "raise RuntimeError('OLD WORKTREE COPY MUST NOT BE IMPORTED')\n", encoding="utf-8"
    )
    j_path = repo / "var" / "dalton" / "cutover-snapshots" / "research-host" / "handoff-journal.json"
    r_same_batch = subprocess.run(
        [
            env["DALTON_PYTHON_BIN"],
            "-B",
            str(rec_dir / "preflight.py"),
            "journal-set",
            "--repo",
            str(repo),
            "operation=recover",
            "completed=false",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_same_batch.returncode == 0, r_same_batch.stderr
    assert json.loads(j_path.read_text(encoding="utf-8"))["operation"] == "recover"

    # 1b. Fail-closed: if sibling guidepoint_lock.py in recovery-bin is missing, NEVER fall back to worktree!
    (repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py").write_bytes(
        (SCRIPTS_DALTON / "research_runtime" / "guidepoint_lock.py").read_bytes()
    )
    saved_sib_gp = (rec_dir / "guidepoint_lock.py").read_bytes()
    (rec_dir / "guidepoint_lock.py").unlink()
    r_missing_sib = subprocess.run(
        [
            env["DALTON_PYTHON_BIN"],
            "-B",
            str(rec_dir / "preflight.py"),
            "journal-set",
            "--repo",
            str(repo),
            "phase=should_fail_closed",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_missing_sib.returncode != 0
    assert "refusing worktree fallback" in r_missing_sib.stderr
    (rec_dir / "guidepoint_lock.py").write_bytes(saved_sib_gp)

    # 2. Tamper recovery-bin/preflight.py to always exit 0 -> trusted rh_verify_recovery_manifest MUST STILL FAIL!
    saved_pf = (rec_dir / "preflight.py").read_bytes()
    (rec_dir / "preflight.py").write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    r_rogue = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_verify_recovery_manifest'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_rogue.returncode != 0
    assert "preflight.py" in r_rogue.stderr
    (rec_dir / "preflight.py").write_bytes(saved_pf)

    # 3. Delete one required file AND remove its entry from recovery-manifest.json -> MUST FAIL!
    (rec_dir / "wait_backend_idle.sh").unlink()
    m_path = rec_dir / "recovery-manifest.json"
    manifest = json.loads(m_path.read_text(encoding="utf-8"))
    del manifest["files"]["wait_backend_idle.sh"]
    m_path.write_text(json.dumps(manifest), encoding="utf-8")
    r_drop = subprocess.run(
        ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_verify_recovery_manifest'],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_drop.returncode != 0
    assert "missing required file entry" in r_drop.stderr


def test_ops_lock_cannot_be_spoofed_by_flags_and_direct_approve_budgets_acquires_lock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    adm_file = repo / "var" / "dalton" / "maintenance-admission.json"
    cutover_dir = repo / "var" / "dalton" / "cutover-snapshots" / "research-host"
    j_file = cutover_dir / "handoff-journal.json"
    b_file = cutover_dir / "timing-budgets.env"
    adm_file.parent.mkdir(parents=True, exist_ok=True)
    cutover_dir.mkdir(parents=True, exist_ok=True)

    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": []}), encoding="utf-8")
    j_file.write_text(json.dumps({"completed": False}), encoding="utf-8")
    py_bin = str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))

    # 1. Direct preflight.py guard-check --lock-held 1 WITHOUT holding flock -> DENIED!
    r_spoof = subprocess.run(
        [
            py_bin,
            "-B",
            str(PREFLIGHT_PY),
            "guard-check",
            "--repo",
            str(repo),
            "--action",
            "set_target_owner",
            "--lock-held",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r_spoof.returncode != 0
    assert ".ops.lock" in r_spoof.stderr

    # 2. Direct preflight.py approve-budgets while another process holds .ops.lock -> DENIED & file untouched!
    j_file.write_text(json.dumps({"completed": True}), encoding="utf-8")
    b_dict = _make_valid_budgets_dict(
        forward_budget_s=60,
        recovery_budget_s=45,
        handoff_deadline_s=90,
        approval_required="true",
    )
    b_file.write_text(preflight.format_timing_budgets_env(b_dict), encoding="utf-8")
    before_bytes = b_file.read_bytes()

    ready_file = tmp_path / "lock_ready"
    release_file = tmp_path / "lock_release"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": py_bin,
    }
    holder_script = f"""
set -euo pipefail
source "{HANDOFF_LIB_SH}"
rh_acquire_ops_lock "contender_holder"
touch "{ready_file}"
while [ ! -f "{release_file}" ]; do sleep 0.05; done
rh_release_ops_lock
echo "stderr_after_release_ok" >&2
"""
    p_hold = subprocess.Popen(
        ["/bin/bash", "-c", holder_script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists()

        # Direct approve-budgets must fail on busy lock (canonical lock derived from --repo)
        r_busy = subprocess.run(
            [
                py_bin,
                "-B",
                str(PREFLIGHT_PY),
                "approve-budgets",
                "--repo",
                str(repo),
                "--budgets-file",
                str(b_file),
                "--handoff-deadline-s",
                "120",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_busy.returncode != 0
        assert "contender_holder" in r_busy.stderr
        assert b_file.read_bytes() == before_bytes

        # Spoofed shell flags RH_OPS_LOCK_HELD=1 + PID=$$ must NOT fool rh_acquire_ops_lock
        r_flag_spoof = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; RH_OPS_LOCK_HELD=1; DALTON_OPS_LOCK_OWNER_PID="$$"; rh_acquire_ops_lock "spoof"',
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_flag_spoof.returncode != 0

        release_file.touch()
        _, hold_err = p_hold.communicate(timeout=5)
        assert p_hold.returncode == 0
        # Verify rh_release_ops_lock did NOT break caller stderr
        assert "stderr_after_release_ok" in hold_err
    finally:
        release_file.touch()
        p_hold.wait(timeout=5)


def test_corrupt_journal_fails_closed_and_never_overwritten(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    cutover_dir = repo / "var" / "dalton" / "cutover-snapshots" / "research-host"
    cutover_dir.mkdir(parents=True)
    j_file = cutover_dir / "handoff-journal.json"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
    }

    for bad_content in (b"{broken json", b'["not", "an", "object"]\n'):
        j_file.write_bytes(bad_content)
        r_chk = subprocess.run(
            ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_require_no_incomplete_journal'],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_chk.returncode != 0
        assert j_file.read_bytes() == bad_content

        r_set = subprocess.run(
            ["/bin/bash", "-c", f'set -euo pipefail; source "{HANDOFF_LIB_SH}"; rh_journal_set phase=new_phase'],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_set.returncode != 0
        assert j_file.read_bytes() == bad_content


def test_approve_budgets_requires_completed_journal(tmp_path: Path) -> None:
    b_dict = _make_valid_budgets_dict(
        forward_budget_s=60,
        recovery_budget_s=45,
        handoff_deadline_s=90,
        approval_required="true",
    )
    ctx = _setup_isolated_env(tmp_path, budgets=b_dict, write_stage_a_journal=False)
    try:
        before_bytes = ctx["budgets_file"].read_bytes()
        j_file = ctx["cutover_dir"] / "handoff-journal.json"
        base_cmd = [
            sys.executable,
            "-B",
            str(PREFLIGHT_PY),
            "approve-budgets",
            "--repo",
            str(ctx["repo"]),
            "--budgets-file",
            str(ctx["budgets_file"]),
            "--handoff-deadline-s",
            "120",
        ]

        # 1. Missing handoff-journal.json -> refused
        assert not j_file.exists()
        r_missing = subprocess.run(
            base_cmd,
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_missing.returncode != 0
        assert ctx["budgets_file"].read_bytes() == before_bytes

        # 2. completed == false -> refused
        j_file.write_text(json.dumps({"operation": "stage_a", "completed": False}), encoding="utf-8")
        r_inc = subprocess.run(
            base_cmd,
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_inc.returncode != 0
        assert ctx["budgets_file"].read_bytes() == before_bytes

        # 3. Corrupt journal -> refused
        j_file.write_text("{corrupt", encoding="utf-8")
        r_corrupt = subprocess.run(
            base_cmd,
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_corrupt.returncode != 0
        assert ctx["budgets_file"].read_bytes() == before_bytes
    finally:
        ctx["cleanup"]()


def test_preflight_readonly_writes_no_pycache_uses_only_plist_paths_and_survives_malformed_inputs(
    tmp_path: Path,
) -> None:
    ctx = _setup_isolated_env(
        tmp_path / "base",
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    try:
        # 1. Replace symlinked research_runtime with a fresh writable copy and verify ZERO __pycache__ created
        rr_link = ctx["repo"] / "scripts" / "dalton" / "research_runtime"
        rr_link.unlink()
        rr_copy = ctx["repo"] / "scripts" / "dalton" / "research_runtime"
        rr_copy.mkdir(parents=True)
        (rr_copy / "guidepoint_lock.py").write_bytes(
            (SCRIPTS_DALTON / "research_runtime" / "guidepoint_lock.py").read_bytes()
        )
        (rr_copy / "host_connectors").mkdir()
        (rr_copy / "host_connectors" / "__init__.py").write_text("EXTRA_HOST_ENV_KEYS = frozenset()\n", encoding="utf-8")
        res_ro = _run_preflight_cli(ctx, "--stage-a")
        assert res_ro.returncode == 0, res_ro.stdout + res_ro.stderr
        assert list(rr_copy.rglob("__pycache__")) == []
        assert list(rr_copy.rglob("*.pyc")) == []

        # 2. Removing DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH from plist MUST fail venv_python_imports
        hp = ctx["plist_dir"] / "com.dalton.research-host.plist"
        hdata = plistlib.loads(hp.read_bytes())
        del hdata["EnvironmentVariables"]["DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH"]
        hp.write_bytes(plistlib.dumps(hdata))
        res_no_extra = _run_preflight_cli(ctx, "--stage-a", "--report-json")
        assert res_no_extra.returncode != 0
        by_name = {c["check"]: c for c in json.loads(res_no_extra.stdout)["checks"]}
        assert by_name["venv_python_imports"]["status"] == "fail"
        assert "DALTON_RESEARCH_HOST_EXTRA_PYTHONPATH" in by_name["venv_python_imports"]["detail"]

        # 3. Malformed string EnvironmentVariables + invalid DALTON_PROFILE must NOT crash --report-json
        hdata["EnvironmentVariables"] = "corrupt_string_env"
        hp.write_bytes(plistlib.dumps(hdata))
        (ctx["repo"] / ".env").write_text(
            "APP_ENV=dalton\nDALTON_PROFILE=invalid_profile_xyz\nINTERNAL_SERVICE_TOKEN=svc_test_token_alpha\n",
            encoding="utf-8",
        )
        for mode_flag, expected_count in (("--stage-a", 7), ("--switch", 9)):
            res_rep = _run_preflight_cli(ctx, mode_flag, "--report-json")
            assert res_rep.returncode != 0
            parsed = json.loads(res_rep.stdout)
            assert parsed["ok"] is False
            assert len(parsed["checks"]) == expected_count
    finally:
        ctx["cleanup"]()


def test_install_bridge_propagates_repo_env_and_ownership_and_fails_before_writes_when_venv_missing(
    tmp_path: Path,
) -> None:
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
    la_dir = home / "Library" / "LaunchAgents"
    logs_dir = base / "logs"
    fake_bin = base / "fakebin"
    for d in (repo / "scripts" / "dalton" / "launchd", home / ".config" / "dalton", la_dir, fake_bin):
        d.mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "dalton" / "launchd" / "com.dalton.researchdata-bridge.plist.template").write_bytes(
        (LAUNCHD_DIR / "com.dalton.researchdata-bridge.plist.template").read_bytes()
    )

    custom_repo_env = base / "custom-repo.env"
    custom_bridge_env = base / "custom-bridge.env"
    custom_ownership = base / "custom-handoff-journal.json"
    custom_repo_env.write_text("DALTON_PROFILE=everflow\n", encoding="utf-8")
    custom_bridge_env.write_text("DALTON_BRIDGE_PORT=8960\n", encoding="utf-8")
    before_repo = custom_repo_env.read_bytes()
    before_bridge = custom_bridge_env.read_bytes()

    fake_launchctl = _write_exec(fake_bin / "launchctl", "#!/bin/bash\nexit 0\n")
    fake_curl = _write_exec(fake_bin / "curl", '#!/bin/bash\necho \'{"ok":true}\'\nexit 0\n')
    real_py = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)

    base_env = {
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(home),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_REPO_ENV_FILE": str(custom_repo_env),
        "DALTON_BRIDGE_ENV_FILE": str(custom_bridge_env),
        "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(custom_ownership),
        "DALTON_LAUNCH_AGENTS_DIR": str(la_dir),
        "DALTON_BRIDGE_LOGS_DIR": str(logs_dir),
        "DALTON_OCW_PYTHON": str(real_py),
        "LAUNCHCTL_BIN": str(fake_launchctl),
        "CURL_BIN": str(fake_curl),
        "DALTON_BRIDGE_HEALTH_RETRIES": "1",
        "DALTON_BRIDGE_HEALTH_SLEEP_S": "0",
    }

    # 1. Missing .venv/bin/python -> fails BEFORE writing bridge.env or repo.env!
    r_novenv = subprocess.run(
        ["/bin/bash", str(INSTALL_BRIDGE_SH), "--research-runtime=new"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_novenv.returncode != 0
    assert "uv sync --frozen" in r_novenv.stderr
    assert custom_repo_env.read_bytes() == before_repo
    assert custom_bridge_env.read_bytes() == before_bridge
    assert not (la_dir / "com.dalton.researchdata-bridge.plist").exists()

    # 2. With .venv/bin/python present -> merges into custom_repo_env AND renders custom repo_env + ownership into plist
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(real_py)
    r_ok = subprocess.run(
        ["/bin/bash", str(INSTALL_BRIDGE_SH), "--research-runtime=new"],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_ok.returncode == 0, r_ok.stdout + r_ok.stderr
    plist_data = plistlib.loads((la_dir / "com.dalton.researchdata-bridge.plist").read_bytes())
    ev = plist_data["EnvironmentVariables"]
    assert ev["DALTON_REPO_ENV_FILE"] == str(custom_repo_env)
    assert ev["DALTON_GUIDEPOINT_OWNERSHIP_FILE"] == str(custom_ownership)
    assert ev["DALTON_BRIDGE_ENV_FILE"] == str(custom_bridge_env)
    assert "DALTON_BRIDGE_TOKEN=" in custom_repo_env.read_text(encoding="utf-8")

    # 3. Verify research_runtime_migrate.sh forwards the same overrides to install_bridge.sh
    mig_text = MIGRATE_SH.read_text(encoding="utf-8")
    assert 'DALTON_REPO_ENV_FILE="${repo_env_file}"' in mig_text
    assert 'DALTON_GUIDEPOINT_OWNERSHIP_FILE="${ownership_file}"' in mig_text


# ---------------------------------------------------------------------------
# r3: Codex High (1-5), Medium (1-4), and B-class confirmed regression tests
# ---------------------------------------------------------------------------


def test_stage_a_terminal_layout_default_switch_check_and_dry_run_pass_and_unarchived_host_plist_fails(
    tmp_path: Path,
) -> None:
    """Codex High 1 + B-class --report-json single-JSON contract."""
    ctx = _setup_isolated_env(tmp_path / "healthy", budgets=_make_valid_budgets_dict())
    try:
        # Verify real Stage A terminal layout: LaunchAgents has ONLY legacy proxy + bridge;
        # com.dalton.research-host.plist is archived out of LaunchAgents.
        assert (ctx["plist_dir"] / "com.dalton.guidepoint-mcp-proxy.plist").is_file()
        assert (ctx["plist_dir"] / "com.dalton.researchdata-bridge.plist").is_file()
        assert not (ctx["plist_dir"] / "com.dalton.research-host.plist").exists()
        assert ctx["archived_host_plist"].is_file()

        # Default switch_research_host.sh --check (no --research-host-plist / --archived-plist-dir)
        r_chk = subprocess.run(
            ["/bin/bash", str(SWITCH_SH), "--check"],
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert r_chk.returncode == 0, r_chk.stdout + r_chk.stderr

        # --report-json on --check emits ONE single valid JSON object
        for argv in (["--check", "--report-json"], ["--check", "--skip-budgets", "--report-json"]):
            r_json = subprocess.run(
                ["/bin/bash", str(SWITCH_SH), *argv],
                env=ctx["env"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert r_json.returncode == 0, r_json.stdout + r_json.stderr
            parsed = json.loads(r_json.stdout)
            assert parsed["ok"] is True
            assert parsed["mode"] == "switch"
            assert "summary" in parsed
            assert len(parsed["checks"]) == 9

        # Leaving com.dalton.research-host.plist inside LaunchAgents violates Stage A terminal state -> FAIL
        shutil.copy2(ctx["archived_host_plist"], ctx["plist_dir"] / "com.dalton.research-host.plist")
        r_unarchived = _run_preflight_cli(ctx, "--switch", "--report-json")
        assert r_unarchived.returncode != 0
        by_name = {c["check"]: c for c in json.loads(r_unarchived.stdout)["checks"]}
        assert by_name["bridge_plist_and_budgets"]["status"] == "fail"
        assert "still present in LaunchAgents" in by_name["bridge_plist_and_budgets"]["detail"]
    finally:
        ctx["cleanup"]()


def test_journal_set_enforces_change_guard_on_target_owner_and_cli_rejects_external_lock_or_journal_paths(
    tmp_path: Path,
) -> None:
    """Codex High 3: repo-derived lock/journal paths + journal-set ownership guard."""
    repo = tmp_path / "repo"
    adm_file = repo / "var" / "dalton" / "maintenance-admission.json"
    cutover_dir = repo / "var" / "dalton" / "cutover-snapshots" / "research-host"
    j_file = cutover_dir / "handoff-journal.json"
    adm_file.parent.mkdir(parents=True, exist_ok=True)
    cutover_dir.mkdir(parents=True, exist_ok=True)
    py_bin = str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))

    # 1. Writing target_owner when admission is missing -> DENIED
    j_file.write_text(
        json.dumps({"operation": "switch", "target_owner": "legacy_proxy_8943", "completed": False}),
        encoding="utf-8",
    )
    before = j_file.read_bytes()
    r1 = subprocess.run(
        [py_bin, "-B", str(PREFLIGHT_PY), "journal-set", "--repo", str(repo), "target_owner=research_host_8951"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r1.returncode != 0
    assert "maintenance-admission.json missing" in r1.stderr
    assert j_file.read_bytes() == before

    # 2. Admission open (paused=false) -> DENIED
    adm_file.write_text(json.dumps({"paused": False, "allow_workspace_ids": []}), encoding="utf-8")
    r2 = subprocess.run(
        [py_bin, "-B", str(PREFLIGHT_PY), "journal-set", "--repo", str(repo), "target_owner=research_host_8951"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r2.returncode != 0
    assert "paused != true" in r2.stderr
    assert j_file.read_bytes() == before

    # 3. Admission paused with non-empty allowlist -> DENIED
    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": ["ws-1"]}), encoding="utf-8")
    r3 = subprocess.run(
        [py_bin, "-B", str(PREFLIGHT_PY), "journal-set", "--repo", str(repo), "target_owner=research_host_8951"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r3.returncode != 0
    assert "allow_workspace_ids" in r3.stderr
    assert j_file.read_bytes() == before

    # 4. Full pause but journal completed == true -> DENIED
    adm_file.write_text(json.dumps({"paused": True, "allow_workspace_ids": []}), encoding="utf-8")
    j_file.write_text(
        json.dumps({"operation": "stage_a", "target_owner": "legacy_proxy_8943", "completed": True}),
        encoding="utf-8",
    )
    before_done = j_file.read_bytes()
    r4 = subprocess.run(
        [py_bin, "-B", str(PREFLIGHT_PY), "journal-set", "--repo", str(repo), "target_owner=research_host_8951"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r4.returncode != 0
    assert "completed != false" in r4.stderr
    assert j_file.read_bytes() == before_done

    # 5. Full pause + completed == false -> ALLOWED (unless combined with completed=true)
    j_file.write_text(
        json.dumps({"operation": "switch", "target_owner": "legacy_proxy_8943", "completed": False}),
        encoding="utf-8",
    )
    r5_bad = subprocess.run(
        [
            py_bin,
            "-B",
            str(PREFLIGHT_PY),
            "journal-set",
            "--repo",
            str(repo),
            "target_owner=research_host_8951",
            "completed=true",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r5_bad.returncode != 0

    r5_ok = subprocess.run(
        [py_bin, "-B", str(PREFLIGHT_PY), "journal-set", "--repo", str(repo), "target_owner=research_host_8951"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert r5_ok.returncode == 0, r5_ok.stderr
    assert json.loads(j_file.read_text(encoding="utf-8"))["target_owner"] == "research_host_8951"

    # 6. External --lock-file / --ownership-file / --journal-file / --admission-file flags rejected by CLI
    bad_cli_cases = [
        ["journal-set", "--repo", str(repo), "--lock-file", str(tmp_path / "other.lock"), "phase=x"],
        ["journal-set", "--repo", str(repo), "--ownership-file", str(tmp_path / "other.json"), "phase=x"],
        ["approve-budgets", "--repo", str(repo), "--lock-file", str(tmp_path / "other.lock")],
        ["approve-budgets", "--repo", str(repo), "--journal-file", str(tmp_path / "other.json")],
        ["guard-check", "--repo", str(repo), "--action", "set_target_owner", "--lock-held", "1", "--lock-file", str(tmp_path / "other.lock")],
        ["guard-check", "--repo", str(repo), "--action", "set_target_owner", "--lock-held", "1", "--admission-file", str(tmp_path / "other.json")],
        ["lock-verify", "--repo", str(repo), "--lock-file", str(tmp_path / "other.lock")],
    ]
    for sub_argv in bad_cli_cases:
        r_rej = subprocess.run(
            [py_bin, "-B", str(PREFLIGHT_PY), *sub_argv],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_rej.returncode == 2, f"expected exit 2 for {sub_argv}, got {r_rej.returncode}: {r_rej.stderr}"


def test_token_path_real_startup_priority_primary_over_alias_var_expansion_and_reject_unparseable(
    tmp_path: Path,
) -> None:
    """Codex High 4: shared real startup priority (plist primary > sourced alias), shell source var expansion & inline comments, fail closed on unparseable."""
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
    out_dir = base / "rendered"
    (repo / ".venv" / "bin").mkdir(parents=True)
    real_py = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    (repo / ".venv" / "bin" / "python").symlink_to(real_py)
    (repo / "scripts" / "dalton" / "launchd").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "bridge_subprocess_env.py").symlink_to(
        SCRIPTS_DALTON / "bridge_subprocess_env.py"
    )
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
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")

    live_res_home = base / "live_research"
    (live_res_home / "config").mkdir(parents=True)
    primary_tokens = live_res_home / "config" / "primary_live_tokens.json"
    alias_tokens = live_res_home / "config" / "alias_ignored_tokens.json"
    primary_tokens.write_text('{"access_token":"a","refresh_token":"r","obtained_at":1,"expires_in":600}', encoding="utf-8")
    alias_tokens.write_text('{"access_token":"a","refresh_token":"r","obtained_at":1,"expires_in":600}', encoding="utf-8")

    # 1. Existing plist sets PRIMARY `GUIDEPOINT_TOKENS_FILE`, while `guidepoint_mcp.env` only sets
    #    SECONDARY ALIAS `DALTON_GUIDEPOINT_TOKENS_FILE` (with $RESEARCH_HOME + inline comment).
    #    At real runtime, primary wins over alias -> renderer MUST keep primary_tokens, NOT switch to alias!
    live_gp_env = live_res_home / "config" / "guidepoint_mcp.env"
    live_gp_env.write_text(
        'DALTON_GUIDEPOINT_TOKENS_FILE="$RESEARCH_HOME/config/alias_ignored_tokens.json"  # legacy alias comment\n'
        "GUIDEPOINT_PROXY_ADMIN_TOKEN=admin_tok # inline comment\n",
        encoding="utf-8",
    )
    live_proxy_plist = home / "Library" / "LaunchAgents" / "com.dalton.guidepoint-mcp-proxy.plist"
    live_proxy_plist.parent.mkdir(parents=True, exist_ok=True)
    live_proxy_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.guidepoint-mcp-proxy",
                "ProgramArguments": ["/bin/bash", "/tmp/run.sh"],
                "EnvironmentVariables": {
                    "DALTON_RESEARCH_HOME": str(live_res_home),
                    "GUIDEPOINT_ENV_FILE": str(live_gp_env),
                    "GUIDEPOINT_TOKENS_FILE": str(primary_tokens),
                },
            }
        )
    )

    bse.render_stage_a_plists_atomic(
        output_dir=out_dir,
        repo_root=repo,
        home_dir=home,
        bridge_env_path=bridge_env,
        research_host_env_path=host_env,
        existing_proxy_plist_path=live_proxy_plist,
    )
    p_data = plistlib.loads((out_dir / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes())
    h_data = plistlib.loads((out_dir / "com.dalton.research-host.plist").read_bytes())
    assert p_data["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == str(primary_tokens)
    assert h_data["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == str(primary_tokens)

    # 2. Variable expansion ($RESEARCH_HOME, ${SUBDIR:-config}, previously assigned var) + inline `#` comment
    expanded_tokens = live_res_home / "custom_dir" / "expanded_tokens.json"
    expanded_tokens.parent.mkdir(parents=True, exist_ok=True)
    expanded_tokens.write_text('{"access_token":"a","refresh_token":"r","obtained_at":1,"expires_in":600}', encoding="utf-8")
    live_gp_env.write_text(
        'SUBDIR="custom_dir" # comment\n'
        'export GUIDEPOINT_TOKENS_FILE="${RESEARCH_HOME}/${SUBDIR:-config}/expanded_tokens.json" # trailing comment\n',
        encoding="utf-8",
    )
    out_exp = base / "rendered_exp"
    bse.render_stage_a_plists_atomic(
        output_dir=out_exp,
        repo_root=repo,
        home_dir=home,
        bridge_env_path=bridge_env,
        research_host_env_path=host_env,
        existing_proxy_plist_path=live_proxy_plist,
    )
    p_exp = plistlib.loads((out_exp / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes())
    h_exp = plistlib.loads((out_exp / "com.dalton.research-host.plist").read_bytes())
    assert p_exp["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == str(expanded_tokens)
    assert h_exp["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == str(expanded_tokens)

    # Verify preflight shares the exact same resolution on the rendered plists
    ctx_cfg = preflight.PreflightConfig(
        repo_root=repo,
        home_dir=home,
        plist_dir=out_exp,
        backend_url="http://127.0.0.1:8000",
        proxy_url="http://127.0.0.1:8943",
        bridge_url="http://127.0.0.1:8960",
        research_host_url="http://127.0.0.1:8951",
        budgets_file=repo / "var/dalton/cutover-snapshots/research-host/timing-budgets.env",
    )
    eff_env, _, errs = preflight._resolve_legacy_proxy_effective_env(ctx_cfg)
    assert errs == []
    assert eff_env["GUIDEPOINT_TOKENS_FILE"] == str(expanded_tokens)

    # 3. Unparseable shell constructs in guidepoint_mcp.env -> refused BEFORE rendering & failed by preflight!
    unparseable_cases = [
        'GUIDEPOINT_TOKENS_FILE="$(echo /tmp/x.json)"\n',
        "GUIDEPOINT_TOKENS_FILE=`echo /tmp/x.json`\n",
        "if [ -f /tmp/a ]; then GUIDEPOINT_TOKENS_FILE=/tmp/a; fi\n",
        'GUIDEPOINT_TOKENS_FILE="${RESEARCH_HOME##*/}/tokens.json"\n',
        'GUIDEPOINT_TOKENS_FILE="/unclosed/quote\n',
        "FOO=bar rm -rf /tmp/x\n",
    ]
    for idx, bad_env_text in enumerate(unparseable_cases):
        live_gp_env.write_text(bad_env_text, encoding="utf-8")
        out_bad = base / f"rendered_bad_{idx}"
        with pytest.raises(bse.EnvFileValidationError):
            bse.render_stage_a_plists_atomic(
                output_dir=out_bad,
                repo_root=repo,
                home_dir=home,
                bridge_env_path=bridge_env,
                research_host_env_path=host_env,
                existing_proxy_plist_path=live_proxy_plist,
            )
        assert not out_bad.exists() or list(out_bad.iterdir()) == []
        _, _, pre_errs = preflight._resolve_legacy_proxy_effective_env(ctx_cfg)
        assert pre_errs, f"preflight should reject unparseable env: {bad_env_text!r}"


def test_preflight_switch_rejects_residual_8951_in_bridge_env_and_process_when_owner_is_8943(
    tmp_path: Path,
) -> None:
    """Codex High 5: residual :8951 in BOTH bridge.env AND bridge process fails when owner is legacy_proxy_8943."""
    # 1. Owner is legacy_proxy_8943, BOTH bridge.env AND running bridge process have residual :8951 -> FAIL!
    ctx_res = _setup_isolated_env(
        tmp_path / "residual_8951",
        budgets=_make_valid_budgets_dict(),
        ps_output_override="PID COMMAND\n5151 python researchdata_bridge.py DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n",
    )
    try:
        with ctx_res["bridge_env"].open("a", encoding="utf-8") as fh:
            fh.write("DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n")
        res = _run_preflight_cli(ctx_res, "--switch", "--report-json")
        assert res.returncode != 0
        by_name = {c["check"]: c for c in json.loads(res.stdout)["checks"]}
        assert by_name["env_override_scan"]["status"] == "fail"
        assert "researchdata-bridge.env" in by_name["env_override_scan"]["detail"]
        assert "legacy_proxy_8943" in by_name["env_override_scan"]["detail"]
    finally:
        ctx_res["cleanup"]()

    # 2. Owner is research_host_8951, bridge.env points to :8951, bridge proc still has residual :8943 -> FAIL!
    ctx_8951 = _setup_isolated_env(
        tmp_path / "owner_8951_proc_8943",
        budgets=_make_valid_budgets_dict(),
        ps_output_override="PID COMMAND\n5151 python researchdata_bridge.py DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp\n",
    )
    try:
        j_file = ctx_8951["cutover_dir"] / "handoff-journal.json"
        j_data = json.loads(j_file.read_text(encoding="utf-8"))
        j_data["target_owner"] = "research_host_8951"
        j_file.write_text(json.dumps(j_data), encoding="utf-8")
        with ctx_8951["bridge_env"].open("a", encoding="utf-8") as fh:
            fh.write("DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n")
        res2 = _run_preflight_cli(ctx_8951, "--switch", "--report-json")
        assert res2.returncode != 0
        by_name2 = {c["check"]: c for c in json.loads(res2.stdout)["checks"]}
        assert by_name2["env_override_scan"]["status"] == "fail"
        assert "DALTON_GUIDEPOINT_MCP_URL" in by_name2["env_override_scan"]["detail"]
    finally:
        ctx_8951["cleanup"]()


def test_compose_file_chain_strips_trailing_comments_and_matches_deploy_main(tmp_path: Path) -> None:
    """Codex Medium 1: COMPOSE_FILE & COMPOSE_PROJECT_NAME with inline `#` comments match deploy_main.sh."""
    ctx = _setup_isolated_env(tmp_path / "compose_comments", budgets=_make_valid_budgets_dict())
    try:
        custom_a = ctx["repo"] / "compose.custom.yml"
        custom_a.write_text("services: {}\n", encoding="utf-8")
        with (ctx["repo"] / ".env").open("a", encoding="utf-8") as fh:
            fh.write("COMPOSE_FILE=compose.custom.yml:docker-compose.dalton.yml   # 让 make up 自动叠加 Dalton 配置\n")
            fh.write("COMPOSE_PROJECT_NAME=dalton_prod_test   # project name comment\n")

        args_log = tmp_path / "compose_args.json"
        docker_bin = Path(ctx["env"]["DOCKER_BIN"])
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
        _write_exec(
            docker_bin,
            f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
if len(sys.argv) >= 2 and sys.argv[1] == "compose":
    Path({str(args_log)!r}).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
    print(json.dumps({compose_payload!r}))
    sys.exit(0)
sys.exit(1)
""",
        )
        res = _run_preflight_cli(ctx, "--switch")
        assert res.returncode == 0, res.stdout + res.stderr
        argv = json.loads(args_log.read_text(encoding="utf-8"))
        assert "-p" in argv
        assert argv[argv.index("-p") + 1] == "dalton_prod_test"
        assert str(custom_a) in argv
        assert str(ctx["repo"] / "docker-compose.dalton.yml") in argv

        # Verify CLI compose-args helper matches deploy_main.sh's awk comment stripping
        py_bin = str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable))
        r_ca = subprocess.run(
            [py_bin, "-B", str(PREFLIGHT_PY), "compose-args", "--repo", str(ctx["repo"]), "--json"],
            env=ctx["env"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert r_ca.returncode == 0, r_ca.stderr
        ca = json.loads(r_ca.stdout)
        assert ca[:2] == ["-p", "dalton_prod_test"]
        assert str(custom_a) in ca
        assert str(ctx["repo"] / "docker-compose.dalton.yml") in ca
    finally:
        ctx["cleanup"]()


def test_preflight_rejects_duplicate_startup_args_checks_stage_b_host_args_and_isolates_import_cwd(
    tmp_path: Path,
) -> None:
    """Codex Medium 2: reject duplicate CLI flags, check archived research-host args in Stage B, isolate import cwd."""
    # 1. Duplicate --env-file (1st valid, 2nd rogue) rejected in BOTH Stage A AND Stage B!
    ctx_a = _setup_isolated_env(
        tmp_path / "dup_a",
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    try:
        hp = ctx_a["plist_dir"] / "com.dalton.research-host.plist"
        data = plistlib.loads(hp.read_bytes())
        rogue_env = tmp_path / "rogue-host.env"
        rogue_env.write_text("DALTON_RESEARCH_HOST_TOKEN=rogue\n", encoding="utf-8")
        data["ProgramArguments"].extend(["--env-file", str(rogue_env)])
        hp.write_bytes(plistlib.dumps(data))

        res_a = _run_preflight_cli(ctx_a, "--stage-a", "--report-json")
        assert res_a.returncode != 0
        by_a = {c["check"]: c for c in json.loads(res_a.stdout)["checks"]}
        assert by_a["stage_a_plists"]["status"] == "fail"
        assert "duplicate_argument" in by_a["stage_a_plists"]["detail"]
    finally:
        ctx_a["cleanup"]()

    ctx_b = _setup_isolated_env(tmp_path / "dup_b", budgets=_make_valid_budgets_dict())
    try:
        ahp = ctx_b["archived_host_plist"]
        data_b = plistlib.loads(ahp.read_bytes())
        rogue_repo_env = tmp_path / "rogue-repo.env"
        rogue_repo_env.write_text("APP_ENV=dalton\n", encoding="utf-8")
        data_b["ProgramArguments"].extend(["--repo-env-file", str(rogue_repo_env)])
        ahp.write_bytes(plistlib.dumps(data_b))

        res_b = _run_preflight_cli(ctx_b, "--switch", "--report-json")
        assert res_b.returncode != 0
        by_b = {c["check"]: c for c in json.loads(res_b.stdout)["checks"]}
        assert by_b["bridge_plist_and_budgets"]["status"] == "fail"
        assert "duplicate_argument" in by_b["bridge_plist_and_budgets"]["detail"]
    finally:
        ctx_b["cleanup"]()

    # 2. Import probe cwd isolation: point plist PYTHONPATH to empty dir and run preflight FROM REPO_ROOT!
    ctx_cwd = _setup_isolated_env(tmp_path / "cwd_iso", budgets=_make_valid_budgets_dict())
    try:
        empty_dir = tmp_path / "empty_pythonpath"
        empty_dir.mkdir()
        ahp = ctx_cwd["archived_host_plist"]
        data_c = plistlib.loads(ahp.read_bytes())
        data_c["EnvironmentVariables"]["PYTHONPATH"] = str(empty_dir)
        ahp.write_bytes(plistlib.dumps(data_c))

        cmd = [
            str(VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)),
            str(PREFLIGHT_PY),
            "--switch",
            "--report-json",
            "--repo",
            str(ctx_cwd["repo"]),
            "--home",
            str(ctx_cwd["home"]),
            "--plist-dir",
            str(ctx_cwd["plist_dir"]),
            "--backend-url",
            str(ctx_cwd["backend_url"]),
            "--proxy-url",
            str(ctx_cwd["proxy_url"]),
            "--bridge-url",
            str(ctx_cwd["bridge_url"]),
            "--budgets-file",
            str(ctx_cwd["budgets_file"]),
        ]
        # Invoke with cwd=REPO_ROOT (the real repo checkout that contains src/dalton/capabilities.py!)
        res_cwd = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=ctx_cwd["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert res_cwd.returncode != 0
        by_c = {c["check"]: c for c in json.loads(res_cwd.stdout)["checks"]}
        assert by_c["venv_python_imports"]["status"] == "fail"
    finally:
        ctx_cwd["cleanup"]()


def test_render_stage_a_plists_rolls_back_on_mid_commit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex Medium 3: transactional rollback if 2nd or 3rd os.replace fails during Stage A plist commit."""
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
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
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")

    # 1. Fresh directory: fail on 2nd os.replace -> 1st committed file is rolled back (zero files left)
    out_fresh = base / "out_fresh"
    real_replace = os.replace
    calls = {"n": 0}

    def _fail_on_second(src: Any, dst: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated mid-commit replace failure #2")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", create_autospec(real_replace, side_effect=_fail_on_second))
    with pytest.raises(OSError, match="simulated mid-commit replace failure #2"):
        bse.render_stage_a_plists_atomic(
            output_dir=out_fresh,
            repo_root=repo,
            home_dir=home,
            bridge_env_path=bridge_env,
            research_host_env_path=host_env,
        )
    assert list(out_fresh.iterdir()) == []

    # 2. Pre-existing v1 plists: fail on 3rd os.replace -> ALL 3 files restored to exact v1 bytes!
    monkeypatch.setattr(os, "replace", real_replace)
    out_existing = base / "out_existing"
    out_existing.mkdir()
    v1_contents = {
        "com.dalton.researchdata-bridge.plist": b"<plist>v1-bridge</plist>\n",
        "com.dalton.research-host.plist": b"<plist>v1-host</plist>\n",
        "com.dalton.guidepoint-mcp-proxy.plist": b"<plist>v1-proxy</plist>\n",
    }
    for fname, raw in v1_contents.items():
        (out_existing / fname).write_bytes(raw)

    calls["n"] = 0

    def _fail_on_third(src: Any, dst: Any) -> None:
        # Only fail the 3rd forward commit replace (allow rollback replaces afterward!)
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("simulated mid-commit replace failure #3")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", create_autospec(real_replace, side_effect=_fail_on_third))
    with pytest.raises(OSError, match="simulated mid-commit replace failure #3"):
        bse.render_stage_a_plists_atomic(
            output_dir=out_existing,
            repo_root=repo,
            home_dir=home,
            bridge_env_path=bridge_env,
            research_host_env_path=host_env,
        )
    for fname, expected_raw in v1_contents.items():
        assert (out_existing / fname).read_bytes() == expected_raw


def test_preflight_diagnostics_redact_plist_values_and_compose_url_queries_and_credentials(
    tmp_path: Path,
) -> None:
    """Codex Medium 4: plist errors emit field name + error class only; compose errors strip URL queries, fragments & creds."""
    # 1. Plist field containing secret + ${VAR} + query + fragment -> raw value NEVER echoed
    ctx_a = _setup_isolated_env(
        tmp_path / "plist_diag",
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    try:
        hp = ctx_a["plist_dir"] / "com.dalton.research-host.plist"
        hdata = plistlib.loads(hp.read_bytes())
        hdata["EnvironmentVariables"]["CUSTOM_SECRET_FIELD"] = (
            "https://alice:super_secret_pw@host.invalid/api/${LEAKED_SECRET_VAR}?api_key=leak_q99#leak_frag"
        )
        hp.write_bytes(plistlib.dumps(hdata))

        res_a = _run_preflight_cli(ctx_a, "--stage-a", "--report-json")
        assert res_a.returncode != 0
        combined = res_a.stdout + res_a.stderr
        assert "CUSTOM_SECRET_FIELD" in combined
        assert "unexpanded_shell_variable" in combined
        for secret_tok in ("super_secret_pw", "LEAKED_SECRET_VAR", "leak_q99", "leak_frag"):
            assert secret_tok not in combined
    finally:
        ctx_a["cleanup"]()

    # 2. docker compose config error with URL userinfo, query string, fragment, and secret tokens -> stripped!
    ctx_b = _setup_isolated_env(tmp_path / "compose_diag", budgets=_make_valid_budgets_dict())
    try:
        docker_bin = Path(ctx_b["env"]["DOCKER_BIN"])
        _write_exec(
            docker_bin,
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if len(sys.argv) >= 2 and sys.argv[1] == 'compose':\n"
            "    sys.stderr.write('invalid url postgresql://dbuser:dbpass_secret@db.internal:5432/app?ssltoken=q_secret_1#frag_secret_2 and https://u2:pw2_secret@api.test/v1?key=k_secret_3#f3\\n')\n"
            "    sys.exit(1)\n"
            "sys.exit(1)\n",
        )
        res_b = _run_preflight_cli(ctx_b, "--switch", "--report-json")
        assert res_b.returncode != 0
        combined_b = res_b.stdout + res_b.stderr
        assert "postgresql://***@db.internal:5432/app" in combined_b
        assert "https://***@api.test/v1" in combined_b
        for leaked in ("dbpass_secret", "q_secret_1", "frag_secret_2", "pw2_secret", "k_secret_3", "#f3"):
            assert leaked not in combined_b
    finally:
        ctx_b["cleanup"]()


@pytest.mark.enable_socket
def test_preflight_real_ephemeral_http_stub_end_to_end(tmp_path: Path) -> None:
    """Codex B-class D8: real ephemeral-port ThreadingHTTPServer HTTP stub (skips cleanly if sandbox blocks loopback TCP connect)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import socket
    import threading

    ctx_a = _setup_isolated_env(
        tmp_path / "ephemeral_http_a",
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=False,
    )
    ctx_b = _setup_isolated_env(
        tmp_path / "ephemeral_http_b",
        budgets=_make_valid_budgets_dict(),
        stage_a_completed_layout=True,
    )
    try:
        svc_token = "svc_test_token_alpha"
        bridge_admin_tok = "bridge_admin_tok_1"
        gp_admin_tok = "gp_admin_tok_1"

        class _StubHandler(BaseHTTPRequestHandler):
            def _send_json(self, code: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/v1/dalton/maintenance/admission":
                    if self.headers.get("X-Service-Token") != svc_token:
                        self._send_json(401, {"detail": "Unauthorized"})
                        return
                    self._send_json(200, {"ok": True, "paused": False, "revision": 7, "allow_workspace_ids": []})
                    return
                if self.path == "/bridge/healthz":
                    self._send_json(200, {"ok": True, "inflight": 0, "queued": 0, "ingest_inflight": 0})
                    return
                if self.path == "/bridge/admin/drain":
                    if self.headers.get("Authorization") != f"Bearer {bridge_admin_tok}":
                        self._send_json(401, {"ok": False})
                        return
                    self._send_json(200, {"ok": True, "draining": False, "inflight": 0, "queued": 0, "ingest_inflight": 0})
                    return
                if self.path == "/proxy/healthz":
                    self._send_json(200, {"ok": True, "owner_lock_held": True, "draining": False, "in_flight": 0})
                    return
                if self.path == "/proxy/admin/drain":
                    if self.headers.get("Authorization") != f"Bearer {gp_admin_tok}":
                        self._send_json(401, {"ok": False})
                        return
                    self._send_json(200, {"ok": True, "draining": False, "in_flight": 0, "owner_lock_held": True})
                    return
                self._send_json(404, {"detail": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                if length > 0:
                    self.rfile.read(length)
                if self.path == "/api/v1/dalton/maintenance/admission/probe":
                    if self.headers.get("X-Service-Token") != svc_token:
                        self._send_json(401, {"detail": "Unauthorized"})
                        return
                    self._send_json(200, {"ok": True, "admitted": True, "paused": False, "revision": 7})
                    return
                self._send_json(404, {"detail": "Not Found"})

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            # Verify whether the current execution environment permits loopback TCP connect
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    pass
            except PermissionError:
                pytest.skip("sandbox blocks loopback TCP connect (EPERM); verified on host/CI")

            # Use real urllib HTTP path (remove fake CURL_BIN) for both --stage-a and --switch
            for ctx_item, mode_flag in ((ctx_a, "--stage-a"), (ctx_b, "--switch")):
                env_real_http = {k: v for k, v in ctx_item["env"].items() if k != "CURL_BIN"}
                ctx_real = dict(
                    ctx_item,
                    env=env_real_http,
                    backend_url=f"http://127.0.0.1:{port}",
                    bridge_url=f"http://127.0.0.1:{port}/bridge",
                    proxy_url=f"http://127.0.0.1:{port}/proxy",
                )
                res = _run_preflight_cli(ctx_real, mode_flag)
                assert res.returncode == 0, res.stdout + res.stderr
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        ctx_a["cleanup"]()
        ctx_b["cleanup"]()


# ---------------------------------------------------------------------------
# r4: Codex 3 High regressions (recovery post-swap rollback, plist rollback
# failure backup retention, legacy proxy launcher vars & Stage B admin token)
# ---------------------------------------------------------------------------


def test_publish_recovery_bin_fsyncs_before_swap_and_rolls_back_on_post_swap_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex r4 High 1: fsync new tree & parent dir before swap; roll back on post-swap fsync failure (symlink & real dir)."""
    import errno as _errno

    repo = tmp_path.resolve() / "repo"
    src_dir = repo / "scripts" / "dalton" / "ops" / "prod"
    gp_lock_src = repo / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py"
    src_dir.mkdir(parents=True)
    gp_lock_src.parent.mkdir(parents=True)
    for f in (
        "rollback_research_host.sh",
        "snapshot_research_host.sh",
        "_research_host_handoff_lib.sh",
        "wait_backend_idle.sh",
        "preflight.py",
    ):
        (src_dir / f).write_bytes((PROD_OPS / f).read_bytes())
    gp_lock_src.write_bytes((SCRIPTS_DALTON / "research_runtime" / "guidepoint_lock.py").read_bytes())

    snapshots_root = repo / "var" / "dalton" / "cutover-snapshots"
    rec_entry = snapshots_root / "recovery-bin"

    # Initial healthy publish -> symlink to v1
    preflight.publish_recovery_bin_atomic(
        repo_root=repo,
        src_dir=src_dir,
        gp_lock_src=gp_lock_src,
        source_commit="commit_v1",
    )
    assert rec_entry.is_symlink()
    v1_target = os.readlink(rec_entry)
    assert json.loads((rec_entry / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v1"

    # Case 1: Existing symlink entry — verify pre-swap fsync order AND rollback when post-swap parent fsync fails
    real_fsync_dir = preflight._fsync_dir
    real_replace = os.replace
    events: list[str] = []
    swapped_symlink = {"done": False}

    def _tracked_replace(src: Any, dst: Any) -> None:
        dst_p = Path(dst)
        if dst_p == rec_entry:
            events.append("swap_symlink" if not swapped_symlink["done"] else "rollback_symlink")
            swapped_symlink["done"] = True
        real_replace(src, dst)

    def _fail_post_swap_fsync_symlink(path: Path, *, strict: bool = False) -> None:
        p = Path(path)
        if p == snapshots_root:
            if not swapped_symlink["done"]:
                events.append("pre_swap_parent_fsync")
                real_fsync_dir(p, strict=strict)
                return
            if strict:
                events.append("post_swap_parent_fsync_fail")
                raise OSError(_errno.EIO, "simulated post-swap parent fsync failure (symlink)")
            events.append("rollback_parent_fsync")
            real_fsync_dir(p, strict=strict)
            return
        if p.name.startswith(".recovery-bin.rel."):
            events.append("new_tree_fsync")
        real_fsync_dir(p, strict=strict)

    monkeypatch.setattr(os, "replace", create_autospec(real_replace, side_effect=_tracked_replace))
    monkeypatch.setattr(preflight, "_fsync_dir", create_autospec(real_fsync_dir, side_effect=_fail_post_swap_fsync_symlink))
    with pytest.raises(OSError, match="simulated post-swap parent fsync failure \\(symlink\\)"):
        preflight.publish_recovery_bin_atomic(
            repo_root=repo,
            src_dir=src_dir,
            gp_lock_src=gp_lock_src,
            source_commit="commit_v2_should_rollback",
        )
    monkeypatch.undo()

    assert "new_tree_fsync" in events
    assert "pre_swap_parent_fsync" in events
    assert events.index("new_tree_fsync") < events.index("pre_swap_parent_fsync") < events.index("swap_symlink")
    assert events.index("swap_symlink") < events.index("post_swap_parent_fsync_fail") < events.index("rollback_symlink")
    assert rec_entry.is_symlink()
    assert os.readlink(rec_entry) == v1_target
    assert json.loads((rec_entry / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v1"
    # New staged dir and rollback temp symlink cleaned up; only v1 release dir remains
    rel_entries = [p.name for p in snapshots_root.iterdir() if p.name.startswith(".recovery-bin.")]
    assert rel_entries == [v1_target]

    # Case 2: Existing real-directory entry — verify pre-swap fsync order AND atomic swap-back on post-swap fsync failure
    v1_dir = snapshots_root / v1_target
    rec_entry.unlink()
    shutil.move(str(v1_dir), str(rec_entry))
    assert rec_entry.is_dir() and not rec_entry.is_symlink()

    real_swap = preflight._atomic_swap_paths
    events_dir: list[str] = []
    swapped_real = {"count": 0}

    def _tracked_swap(src: Path, dst: Path) -> None:
        swapped_real["count"] += 1
        events_dir.append("swap_real_dir" if swapped_real["count"] == 1 else "rollback_real_dir")
        real_swap(src, dst)

    def _fail_post_swap_fsync_real(path: Path, *, strict: bool = False) -> None:
        p = Path(path)
        if p == snapshots_root:
            if swapped_real["count"] == 0:
                events_dir.append("pre_swap_parent_fsync")
                real_fsync_dir(p, strict=strict)
                return
            if strict:
                events_dir.append("post_swap_parent_fsync_fail")
                raise OSError(_errno.EIO, "simulated post-swap parent fsync failure (real_dir)")
            events_dir.append("rollback_parent_fsync")
            real_fsync_dir(p, strict=strict)
            return
        if p.name.startswith(".recovery-bin.rel."):
            events_dir.append("new_tree_fsync")
        real_fsync_dir(p, strict=strict)

    monkeypatch.setattr(preflight, "_atomic_swap_paths", create_autospec(real_swap, side_effect=_tracked_swap))
    monkeypatch.setattr(preflight, "_fsync_dir", create_autospec(real_fsync_dir, side_effect=_fail_post_swap_fsync_real))
    with pytest.raises(OSError, match="simulated post-swap parent fsync failure \\(real_dir\\)"):
        preflight.publish_recovery_bin_atomic(
            repo_root=repo,
            src_dir=src_dir,
            gp_lock_src=gp_lock_src,
            source_commit="commit_v3_real_dir_rollback",
        )
    monkeypatch.undo()

    assert swapped_real["count"] == 2
    assert events_dir.index("new_tree_fsync") < events_dir.index("pre_swap_parent_fsync") < events_dir.index("swap_real_dir")
    assert events_dir.index("swap_real_dir") < events_dir.index("post_swap_parent_fsync_fail") < events_dir.index("rollback_real_dir")
    assert rec_entry.is_dir() and not rec_entry.is_symlink()
    assert json.loads((rec_entry / "recovery-manifest.json").read_text(encoding="utf-8"))["source_commit"] == "commit_v1"
    assert [p.name for p in snapshots_root.iterdir() if p.name.startswith(".recovery-bin.")] == []


def test_render_stage_a_plists_preserves_backups_and_reports_location_when_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex r4 High 2: mid-commit failure + rollback failure must NOT swallow OSError or delete backups."""
    import errno as _errno

    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
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
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")

    v1_contents = {
        "com.dalton.researchdata-bridge.plist": b"<plist>v1-bridge-orig</plist>\n",
        "com.dalton.research-host.plist": b"<plist>v1-host-orig</plist>\n",
        "com.dalton.guidepoint-mcp-proxy.plist": b"<plist>v1-proxy-orig</plist>\n",
    }

    # Case 1: Output dir loses write permission after 1st commit -> 2nd commit fails with EACCES AND rollback fails with EACCES!
    out_ro = base / "out_ro"
    out_ro.mkdir()
    for fname, raw in v1_contents.items():
        (out_ro / fname).write_bytes(raw)

    real_replace = os.replace
    commit_count = {"n": 0}

    def _chmod_ro_after_first(src: Any, dst: Any) -> None:
        commit_count["n"] += 1
        real_replace(src, dst)
        if commit_count["n"] == 1:
            os.chmod(out_ro, 0o555)

    monkeypatch.setattr(os, "replace", create_autospec(real_replace, side_effect=_chmod_ro_after_first))
    try:
        with pytest.raises(bse.StageAPlistRollbackError) as exc_info:
            bse.render_stage_a_plists_atomic(
                output_dir=out_ro,
                repo_root=repo,
                home_dir=home,
                bridge_env_path=bridge_env,
                research_host_env_path=host_env,
            )
    finally:
        os.chmod(out_ro, 0o755)
        monkeypatch.undo()

    err = exc_info.value
    assert isinstance(err, OSError)
    assert err.backup_dir.is_dir(), "backup_dir must be preserved when rollback fails"
    assert str(err.backup_dir) in str(err)
    bridge_dest = out_ro / "com.dalton.researchdata-bridge.plist"
    assert bridge_dest in err.affected_files
    assert str(bridge_dest) in str(err)
    # Original v1 bytes preserved intact inside backup_dir for operator retry!
    assert (err.backup_dir / "com.dalton.researchdata-bridge.plist").read_bytes() == v1_contents["com.dalton.researchdata-bridge.plist"]

    # Case 2: Commit fails on 3rd replace; rollback fails on 2nd (host) but succeeds on 1st (bridge) -> backups preserved!
    out_partial = base / "out_partial"
    out_partial.mkdir()
    for fname, raw in v1_contents.items():
        (out_partial / fname).write_bytes(raw)

    step = {"n": 0}

    def _fail_commit3_and_rollback_host(src: Any, dst: Any) -> None:
        step["n"] += 1
        dst_p = Path(dst)
        if step["n"] == 3:
            raise OSError(_errno.EIO, "simulated commit #3 failure")
        if step["n"] == 4 and dst_p.name == "com.dalton.research-host.plist":
            raise OSError(_errno.EACCES, "simulated rollback failure on host plist")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", create_autospec(real_replace, side_effect=_fail_commit3_and_rollback_host))
    with pytest.raises(bse.StageAPlistRollbackError) as exc_info2:
        bse.render_stage_a_plists_atomic(
            output_dir=out_partial,
            repo_root=repo,
            home_dir=home,
            bridge_env_path=bridge_env,
            research_host_env_path=host_env,
        )
    monkeypatch.undo()

    err2 = exc_info2.value
    assert err2.backup_dir.is_dir()
    assert err2.affected_files == (out_partial / "com.dalton.research-host.plist",)
    assert str(err2.backup_dir) in str(err2)
    assert str(out_partial / "com.dalton.research-host.plist") in str(err2)
    # Bridge was restored, proxy was never replaced, and host's v1 original is preserved in backup_dir!
    assert (out_partial / "com.dalton.researchdata-bridge.plist").read_bytes() == v1_contents["com.dalton.researchdata-bridge.plist"]
    assert (out_partial / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes() == v1_contents["com.dalton.guidepoint-mcp-proxy.plist"]
    assert (err2.backup_dir / "com.dalton.research-host.plist").read_bytes() == v1_contents["com.dalton.research-host.plist"]


def test_legacy_proxy_launcher_vars_env_file_tokens_reject_undefined_and_stage_b_admin_token_expansion(
    tmp_path: Path,
) -> None:
    """Codex r4 High 3: `${ENV_FILE}.tokens.json` matches bash launcher across render/post-check/preflight, rejects undefined vars, and Stage B reads expanded admin token."""
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
    (repo / ".venv" / "bin").mkdir(parents=True)
    real_py = VENV_PYTHON if VENV_PYTHON.is_file() else Path(sys.executable)
    (repo / ".venv" / "bin" / "python").symlink_to(real_py)
    (repo / "scripts" / "dalton" / "launchd").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "bridge_subprocess_env.py").symlink_to(
        SCRIPTS_DALTON / "bridge_subprocess_env.py"
    )
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
    host_env = cfg_dir / "research-host.env"
    host_env.write_text("DALTON_RESEARCH_HOST_TOKEN=t1\n", encoding="utf-8")

    live_res_home = base / "live_research"
    (live_res_home / "config").mkdir(parents=True)
    live_gp_env = live_res_home / "config" / "guidepoint_mcp.env"
    expected_tokens = Path(f"{live_gp_env}.tokens.json")
    expected_tokens.write_text(
        '{"access_token":"a","refresh_token":"r","obtained_at":1,"expires_in":600}',
        encoding="utf-8",
    )
    live_gp_env.write_text(
        'GUIDEPOINT_TOKENS_FILE="${ENV_FILE}.tokens.json"\n'
        'ADMIN_SEG="expanded_admin"\n'
        'export GUIDEPOINT_PROXY_ADMIN_TOKEN="${ADMIN_SEG}_secret_99" # inline comment\n',
        encoding="utf-8",
    )

    live_proxy_plist = home / "Library" / "LaunchAgents" / "com.dalton.guidepoint-mcp-proxy.plist"
    live_proxy_plist.parent.mkdir(parents=True, exist_ok=True)
    live_proxy_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.dalton.guidepoint-mcp-proxy",
                "ProgramArguments": [
                    "/bin/bash",
                    str(repo / "scripts" / "dalton" / "research_runtime" / "scripts" / "guidepoint_mcp_proxy_run.sh"),
                ],
                "EnvironmentVariables": {
                    "HOME": str(home),
                    "DALTON_RESEARCH_HOME": str(live_res_home),
                    "GUIDEPOINT_ENV_FILE": str(live_gp_env),
                },
            }
        )
    )

    # 1. Cross-check real bash launcher expansion of `${ENV_FILE}.tokens.json` matches renderer & preflight
    bash_probe = subprocess.run(
        [
            "/bin/bash",
            "-c",
            (
                'set -euo pipefail; '
                'RESEARCH_HOME="$DALTON_RESEARCH_HOME"; '
                'ENV_FILE="${GUIDEPOINT_ENV_FILE:-$RESEARCH_HOME/config/guidepoint_mcp.env}"; '
                'set -a; source "$ENV_FILE"; set +a; '
                'printf "%s\\n%s\\n" "$GUIDEPOINT_TOKENS_FILE" "$GUIDEPOINT_PROXY_ADMIN_TOKEN"'
            ),
        ],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "DALTON_RESEARCH_HOME": str(live_res_home),
            "GUIDEPOINT_ENV_FILE": str(live_gp_env),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert bash_probe.returncode == 0, bash_probe.stderr
    bash_tokens, bash_admin_tok = bash_probe.stdout.splitlines()
    assert bash_tokens == str(expected_tokens)
    assert bash_admin_tok == "expanded_admin_secret_99"

    out_dir = base / "rendered_env_file"
    bse.render_stage_a_plists_atomic(
        output_dir=out_dir,
        repo_root=repo,
        home_dir=home,
        bridge_env_path=bridge_env,
        research_host_env_path=host_env,
        existing_proxy_plist_path=live_proxy_plist,
    )
    p_data = plistlib.loads((out_dir / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes())
    h_data = plistlib.loads((out_dir / "com.dalton.research-host.plist").read_bytes())
    assert p_data["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == bash_tokens
    assert h_data["EnvironmentVariables"]["GUIDEPOINT_TOKENS_FILE"] == bash_tokens

    cfg_check = preflight.PreflightConfig(
        repo_root=repo,
        home_dir=home,
        plist_dir=out_dir,
        backend_url="http://127.0.0.1:8000",
        proxy_url="http://127.0.0.1:8943",
        bridge_url="http://127.0.0.1:8960",
        research_host_url="http://127.0.0.1:8951",
        budgets_file=repo / "var/dalton/cutover-snapshots/research-host/timing-budgets.env",
    )
    eff_env, _, errs = preflight._resolve_legacy_proxy_effective_env(cfg_check)
    assert errs == []
    assert eff_env["GUIDEPOINT_TOKENS_FILE"] == bash_tokens
    assert eff_env["GUIDEPOINT_PROXY_ADMIN_TOKEN"] == bash_admin_tok

    # 2. Undefined / indeterminate variable references & relative paths -> MUST refuse rendering & fail preflight!
    indeterminate_cases = [
        'GUIDEPOINT_TOKENS_FILE="${UNDEFINED_DIR}/tokens.json"\n',
        'GUIDEPOINT_TOKENS_FILE="$UNDEFINED_DIR/tokens.json"\n',
        'GUIDEPOINT_TOKENS_FILE="${TMPDIR:-/tmp}/tokens.json"\n',
        'GUIDEPOINT_TOKENS_FILE="${UNKNOWN_EXTERNAL_VAR:-/tmp}/tokens.json"\n',
        'GUIDEPOINT_TOKENS_FILE="relative/tokens.json"\n',
    ]
    for idx, bad_text in enumerate(indeterminate_cases):
        live_gp_env.write_text(bad_text, encoding="utf-8")
        out_bad = base / f"out_indet_{idx}"
        with pytest.raises(bse.EnvFileValidationError):
            bse.render_stage_a_plists_atomic(
                output_dir=out_bad,
                repo_root=repo,
                home_dir=home,
                bridge_env_path=bridge_env,
                research_host_env_path=host_env,
                existing_proxy_plist_path=live_proxy_plist,
            )
        assert not out_bad.exists() or list(out_bad.iterdir()) == []
        _, _, pre_errs = preflight._resolve_legacy_proxy_effective_env(cfg_check)
        assert pre_errs, f"preflight must reject indeterminate env: {bad_text!r}"

    # 3. Stage B preflight `/admin/drain` MUST read `GUIDEPOINT_PROXY_ADMIN_TOKEN` via shared shell-expanded resolver!
    ctx = _setup_isolated_env(tmp_path / "stage_b_admin_expand", budgets=_make_valid_budgets_dict())
    try:
        gp_env = ctx["gp_env"]
        tok_from_env_file = Path(f"{gp_env}.tokens.json")
        tok_from_env_file.write_text(
            '{"access_token":"a","refresh_token":"r","obtained_at":1,"expires_in":600}',
            encoding="utf-8",
        )
        gp_env.write_text(
            'GUIDEPOINT_TOKENS_FILE="${ENV_FILE}.tokens.json"\n'
            'ADMIN_PREFIX="gp_admin"\n'
            'export GUIDEPOINT_PROXY_ADMIN_TOKEN="${ADMIN_PREFIX}_tok_1"  # expanded admin token\n',
            encoding="utf-8",
        )
        # Re-render Stage A plists against this env so proxy & archived host both point to `${ENV_FILE}.tokens.json`
        rerender_dir = tmp_path / "rerender_b"
        bse.render_stage_a_plists_atomic(
            output_dir=rerender_dir,
            repo_root=ctx["repo"],
            home_dir=ctx["home"],
            bridge_env_path=ctx["bridge_env"],
            research_host_env_path=ctx["host_env"],
            existing_proxy_plist_path=ctx["plist_dir"] / "com.dalton.guidepoint-mcp-proxy.plist",
        )
        shutil.copy2(
            rerender_dir / "com.dalton.guidepoint-mcp-proxy.plist",
            ctx["plist_dir"] / "com.dalton.guidepoint-mcp-proxy.plist",
        )
        shutil.copy2(
            rerender_dir / "com.dalton.research-host.plist",
            ctx["archived_host_plist"],
        )

        # Fake curl already enforces `Authorization: Bearer gp_admin_tok_1` on :8943/admin/drain!
        res_b = _run_preflight_cli(ctx, "--switch", "--report-json")
        assert res_b.returncode == 0, res_b.stdout + res_b.stderr
        by_b = {c["check"]: c for c in json.loads(res_b.stdout)["checks"]}
        assert by_b["live_services_health_and_drain"]["status"] == "ok"
        assert by_b["two_sided_guidepoint_paths"]["status"] == "ok"
        assert str(tok_from_env_file) in by_b["two_sided_guidepoint_paths"]["detail"]

        # If GUIDEPOINT_PROXY_ADMIN_TOKEN references an undefined variable, Stage B fails closed!
        gp_env.write_text(
            'GUIDEPOINT_TOKENS_FILE="${ENV_FILE}.tokens.json"\n'
            'GUIDEPOINT_PROXY_ADMIN_TOKEN="${UNDEF_ADMIN_VAR}"\n',
            encoding="utf-8",
        )
        res_bad = _run_preflight_cli(ctx, "--switch", "--report-json")
        assert res_bad.returncode != 0
        by_bad = {c["check"]: c for c in json.loads(res_bad.stdout)["checks"]}
        assert by_bad["live_services_health_and_drain"]["status"] == "fail"
        assert "UNDEF_ADMIN_VAR" in by_bad["live_services_health_and_drain"]["detail"]
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
        INSTALL_BRIDGE_SH,
    ],
)
def test_shell_scripts_pass_bash_n(script_path: Path) -> None:
    res = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
