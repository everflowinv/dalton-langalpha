"""Unit tests for AlphaEngine/prod watchdogs, host_services_switch.sh, research_runtime_migrate.sh batch 3 coordination, and bridge host_services check."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import time
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
WATCHDOG_SH = (
    SCRIPTS_DALTON / "research_runtime" / "scripts" / "alphaengine-mcp" / "desktop-watchdog.sh"
)
PROD_WATCHDOG_SH = SCRIPTS_DALTON / "ops" / "prod" / "watchdog.sh"
SWITCH_SH = SCRIPTS_DALTON / "ops" / "host_services_switch.sh"
MIGRATE_SH = SCRIPTS_DALTON / "ops" / "research_runtime_migrate.sh"

if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


def _make_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_desktop_watchdog_uses_header_file_without_token_in_argv_and_falls_back_to_osascript(
    tmp_path: Path,
) -> None:
    app_bundle = tmp_path / "AlphaEngine.app"
    app_exec = app_bundle / "Contents" / "MacOS" / "AlphaEngine"
    app_exec.parent.mkdir(parents=True)
    app_exec.write_text("", encoding="utf-8")

    research_home = tmp_path / "research_home"
    gw_env = tmp_path / "channel-gateway.env"
    gw_env.write_text("INTERNAL_SERVICE_TOKEN=gw_tok_never_in_argv\n", encoding="utf-8")
    gw_env.chmod(0o600)

    curl_argv_log = tmp_path / "curl_argv.log"
    curl_hdr_log = tmp_path / "curl_hdr.log"
    osa_log = tmp_path / "osascript.log"
    printf_argv_log = tmp_path / "printf_argv.log"

    stub_bin = tmp_path / "stub_bin"
    stub_bin.mkdir()
    _make_stub(
        stub_bin / "printf",
        f'printf "%s\\n" "$*" >> "{printf_argv_log}"\n/usr/bin/printf "$@"\n',
    )

    ps_stub = _make_stub(
        tmp_path / "ps",
        f'printf "%s\\n" "{app_exec} --type=renderer"\n',
    )
    open_stub = _make_stub(tmp_path / "open", "exit 0\n")
    osa_stub = _make_stub(
        tmp_path / "osascript",
        f'printf "%s\\n" "$*" >> "{osa_log}"\n',
    )

    curl_ok_gw = _make_stub(
        tmp_path / "curl_gw_ok",
        f"""
url="${{@: -1}}"
if [[ "$url" == *"/health" ]]; then
  printf '{{"ok":true,"authenticated":false,"desktop_renderer_processes":0}}'
  exit 0
fi
printf '%s\\n' "$*" >> "{curl_argv_log}"
for arg in "$@"; do
  if [[ "$arg" == @* ]]; then
    cat "${{arg#@}}" >> "{curl_hdr_log}"
  fi
done
printf '200'
""",
    )
    base_env = {
        "PATH": f"{stub_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DALTON_RESEARCH_HOME": str(research_home),
        "DALTON_CHANNEL_GATEWAY_ENV_FILE": str(gw_env),
        "ALPHAENGINE_APP_BUNDLE": str(app_bundle),
        "ALPHAENGINE_APP_EXECUTABLE": str(app_exec),
        "PS_BIN": str(ps_stub),
        "OPEN_BIN": str(open_stub),
        "OSASCRIPT_BIN": str(osa_stub),
        "CURL_BIN": str(curl_ok_gw),
        "ALPHAENGINE_NOTIFY_COOLDOWN_SECONDS": "21600",
    }
    res1 = subprocess.run(["/bin/bash", str(WATCHDOG_SH)], env=base_env, capture_output=True, text=True)
    assert res1.returncode == 0, res1.stderr
    state = json.loads((research_home / "state" / "alphaengine-desktop-watchdog.json").read_text(encoding="utf-8"))
    assert state["status"] == "module_page_required"
    argv_text = curl_argv_log.read_text(encoding="utf-8")
    assert "alphaengine_module_page_required" in argv_text
    assert "gw_tok_never_in_argv" not in argv_text
    # Builtin `printf` is used, so the PATH printf stub must never see the token (or even be invoked).
    assert not printf_argv_log.exists()
    assert "X-Service-Token: gw_tok_never_in_argv" in curl_hdr_log.read_text(encoding="utf-8")
    assert not osa_log.exists()

    # Immediate rerun respects cooldown
    curl_argv_log.unlink()
    res2 = subprocess.run(["/bin/bash", str(WATCHDOG_SH)], env=base_env, capture_output=True, text=True)
    assert res2.returncode == 0, res2.stderr
    assert not curl_argv_log.exists()

    # Gateway returns 503 -> fallback to osascript; DALTON_OCW_ROOT compat
    ocw_root = tmp_path / "ocw_root"
    curl_503_gw = _make_stub(
        tmp_path / "curl_gw_503",
        f"""
url="${{@: -1}}"
if [[ "$url" == *"/health" ]]; then
  printf '{{"ok":true,"authenticated":false,"desktop_renderer_processes":2}}'
  exit 0
fi
printf '%s\\n' "$*" >> "{curl_argv_log}"
printf '503'
""",
    )
    legacy_env = {**base_env, "DALTON_OCW_ROOT": str(ocw_root), "CURL_BIN": str(curl_503_gw)}
    legacy_env.pop("DALTON_RESEARCH_HOME", None)
    res3 = subprocess.run(["/bin/bash", str(WATCHDOG_SH)], env=legacy_env, capture_output=True, text=True)
    assert res3.returncode == 0, res3.stderr
    assert "gw_tok_never_in_argv" not in curl_argv_log.read_text(encoding="utf-8")
    assert "AlphaEngine 登录失效" in osa_log.read_text(encoding="utf-8")


def test_prod_watchdog_passes_both_tokens_via_header_files_not_argv(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    ops_state_dir = tmp_path / "ops_state"
    backup_dir.mkdir(parents=True)
    ops_state_dir.mkdir(parents=True)

    # Trigger outage alert directly (>=300s down, <3 failures so no docker restart)
    (ops_state_dir / ".down_since").write_text("1000\n", encoding="utf-8")
    (ops_state_dir / "alert_targets.json").write_text('{"targets":["feishu:user:ou_test"]}\n', encoding="utf-8")

    repo_env = tmp_path / ".env"
    repo_env.write_text("DALTON_BRIDGE_ADMIN_TOKEN=bridge_tok_never_in_argv\n", encoding="utf-8")
    gw_env = tmp_path / "channel-gateway.env"
    gw_env.write_text("INTERNAL_SERVICE_TOKEN=gw_tok_never_in_argv\n", encoding="utf-8")

    curl_argv_log = tmp_path / "prod_curl_argv.log"
    curl_hdr_log = tmp_path / "prod_curl_hdr.log"
    curl_stub = _make_stub(
        tmp_path / "curl",
        f"""
url="${{@: -1}}"
if [[ "$url" == *":8000/health" ]]; then
  printf '503'
  exit 0
fi
printf '%s\\n' "$*" >> "{curl_argv_log}"
for arg in "$@"; do
  if [[ "$arg" == @* ]]; then
    cat "${{arg#@}}" >> "{curl_hdr_log}"
  fi
done
printf '503'
""",
    )
    osascript_stub = _make_stub(tmp_path / "osascript", "exit 0\n")
    colima_stub = _make_stub(tmp_path / "colima", "echo Running\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "osascript").symlink_to(osascript_stub)
    (bin_dir / "colima").symlink_to(colima_stub)

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DALTON_BACKUP_DIR": str(backup_dir),
        "DALTON_OPS_STATE_DIR": str(ops_state_dir),
        "DALTON_ENV_FILE": str(repo_env),
        "DALTON_CHANNEL_GATEWAY_ENV_FILE": str(gw_env),
        "CURL_BIN": str(curl_stub),
    }
    res = subprocess.run(["/bin/bash", str(PROD_WATCHDOG_SH)], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    argv_text = curl_argv_log.read_text(encoding="utf-8")
    assert "gw_tok_never_in_argv" not in argv_text
    assert "bridge_tok_never_in_argv" not in argv_text
    hdr_text = curl_hdr_log.read_text(encoding="utf-8")
    assert "X-Service-Token: gw_tok_never_in_argv" in hdr_text
    assert "Authorization: Bearer bridge_tok_never_in_argv" in hdr_text
    assert "X-Dalton-Bridge-Token: bridge_tok_never_in_argv" in hdr_text


def _setup_switch_env(
    tmp_path: Path,
    *,
    mode: str = "new",
    preloaded_new: bool = False,
) -> dict[str, Any]:
    ocw_root = tmp_path / "ocw_ws"
    research_home = tmp_path / "dalton_research"
    launch_agents = tmp_path / "LaunchAgents"
    logs_dir = tmp_path / "Logs"
    (ocw_root / "config").mkdir(parents=True)
    (ocw_root / "state").mkdir(parents=True)
    (research_home / "config").mkdir(parents=True)
    (research_home / "state").mkdir(parents=True)
    launch_agents.mkdir(parents=True)
    logs_dir.mkdir(parents=True)

    (ocw_root / "config" / "guidepoint_mcp.env").write_text(
        "GUIDEPOINT_CLIENT_ID=secret_id_never_print\nGUIDEPOINT_CLIENT_SECRET=secret_val_never_print\n",
        encoding="utf-8",
    )
    (ocw_root / "config" / "guidepoint_mcp_tokens.json").write_text(
        '{"access_token":"secret_tok_never_print","refresh_token":"secret_ref_never_print"}\n',
        encoding="utf-8",
    )
    (research_home / "config" / "guidepoint_mcp.env").write_text(
        "GUIDEPOINT_CLIENT_ID=new_id_never_print\nGUIDEPOINT_CLIENT_SECRET=new_sec_never_print\n",
        encoding="utf-8",
    )
    (research_home / "config" / "guidepoint_mcp_tokens.json").write_text(
        '{"access_token":"new_tok_never_print","refresh_token":"new_ref_never_print"}\n',
        encoding="utf-8",
    )

    bridge_env = tmp_path / "researchdata-bridge.env"
    if mode == "legacy":
        bridge_env.write_text(
            f"DALTON_BRIDGE_TOKEN=bridge_tok_never_print\nDALTON_OCW_ROOT={ocw_root}\n",
            encoding="utf-8",
        )
        data_root = ocw_root
    else:
        bridge_env.write_text(
            f"DALTON_BRIDGE_TOKEN=bridge_tok_never_print\nDALTON_RESEARCH_CODE={REPO_ROOT / 'scripts' / 'dalton' / 'research_runtime'}\nDALTON_RESEARCH_HOME={research_home}\n",
            encoding="utf-8",
        )
        data_root = research_home

    for old_label in (
        "ai.openclaw.alphaengine-mcp",
        "ai.openclaw.alphaengine-desktop-watchdog",
        "ai.openclaw.guidepoint-mcp-proxy",
    ):
        (launch_agents / f"{old_label}.plist").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{old_label}</string></dict></plist>\n',
            encoding="utf-8",
        )

    state_dir = tmp_path / "stub_state"
    state_dir.mkdir()
    loaded_file = state_dir / "loaded.txt"
    loaded_file.write_text(
        "\n".join(
            (
                "com.dalton.alphaengine-mcp",
                "com.dalton.alphaengine-desktop-watchdog",
                "com.dalton.guidepoint-mcp-proxy",
            )
            if preloaded_new
            else (
                "ai.openclaw.alphaengine-mcp",
                "ai.openclaw.alphaengine-desktop-watchdog",
                "ai.openclaw.guidepoint-mcp-proxy",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    launchctl_log = tmp_path / "launchctl.log"
    watchdog_state_file = data_root / "state" / "alphaengine-desktop-watchdog.json"

    pid_map = {
        "ai.openclaw.alphaengine-mcp": "1101",
        "com.dalton.alphaengine-mcp": "2101",
        "ai.openclaw.alphaengine-desktop-watchdog": "1102",
        "com.dalton.alphaengine-desktop-watchdog": "2102",
        "ai.openclaw.guidepoint-mcp-proxy": "1103",
        "com.dalton.guidepoint-mcp-proxy": "2103",
    }
    pid_map_json = json.dumps(pid_map)

    launchctl_stub = _make_stub(
        tmp_path / "launchctl",
        f"""
cmd="${{1:-}}"
shift || true
printf '%s %s\\n' "$cmd" "$*" >> "{launchctl_log}"
python3 - "$cmd" "{loaded_file}" "{watchdog_state_file}" "{launch_agents}" '{pid_map_json}' "$@" <<'PY'
import json
import plistlib
import sys
from pathlib import Path

cmd, loaded_p, wd_state_p, launch_agents_p, pid_json, *args = sys.argv[1:]
loaded_file = Path(loaded_p)
loaded = set(line.strip() for line in loaded_file.read_text().splitlines() if line.strip())
pid_map = json.loads(pid_json)

def label_from(arg: str) -> str:
    return arg.split("/")[-1]

if cmd == "print":
    lbl = label_from(args[0])
    if lbl in loaded:
        print(f"gui/501/{{lbl}} = {{{{")
        if "alphaengine-desktop-watchdog" in lbl:
            print("    state = waiting")
        else:
            print("    state = running")
            print(f"    pid = {{pid_map.get(lbl, '9999')}}")
        print("}}}}")
        sys.exit(0)
    sys.exit(113)
elif cmd == "bootout":
    lbl = label_from(args[0])
    loaded.discard(lbl)
    loaded_file.write_text("\\n".join(sorted(loaded)) + "\\n")
    sys.exit(0)
elif cmd == "bootstrap":
    plist_path = Path(args[1])
    lbl = plist_path.name[:-6]
    loaded.add(lbl)
    loaded_file.write_text("\\n".join(sorted(loaded)) + "\\n")
    sys.exit(0)
elif cmd == "kickstart":
    lbl = label_from(args[-1])
    if lbl not in loaded:
        sys.exit(113)
    if "alphaengine-desktop-watchdog" in lbl:
        wd = Path(wd_state_p)
        plist_file = Path(launch_agents_p) / f"{{lbl}}.plist"
        if plist_file.is_file():
            try:
                ev = plistlib.loads(plist_file.read_bytes()).get("EnvironmentVariables", {{}})
                rt = ev.get("DALTON_RESEARCH_HOME") or ev.get("DALTON_OCW_ROOT") or ev.get("OPENCLAW_WORKSPACE")
                if ev.get("ALPHAENGINE_WATCHDOG_STATE_DIR"):
                    wd = Path(ev["ALPHAENGINE_WATCHDOG_STATE_DIR"]) / "alphaengine-desktop-watchdog.json"
                elif rt:
                    wd = Path(rt) / "state" / "alphaengine-desktop-watchdog.json"
            except Exception:
                pass
        wd.parent.mkdir(parents=True, exist_ok=True)
        wd.write_text('{{"checked_at":"2099-01-01T00:00:00Z","status":"ready","app_running":true,"mcp_authenticated":true}}\\n')
    sys.exit(0)
elif cmd in ("enable", "disable"):
    sys.exit(0)
sys.exit(0)
PY
""",
    )

    lsof_stub = _make_stub(
        tmp_path / "lsof",
        f"""
python3 - "{loaded_file}" '{pid_map_json}' "$@" <<'PY'
import json
import sys
from pathlib import Path

loaded_p, pid_json, *args = sys.argv[1:]
loaded = set(line.strip() for line in Path(loaded_p).read_text().splitlines() if line.strip())
pid_map = json.loads(pid_json)

port = ""
for a in args:
    if a.startswith("-iTCP:"):
        port = a.split(":", 1)[1]

if port == "8950":
    candidates = ("com.dalton.alphaengine-mcp", "ai.openclaw.alphaengine-mcp")
elif port == "8943":
    candidates = ("com.dalton.guidepoint-mcp-proxy", "ai.openclaw.guidepoint-mcp-proxy")
else:
    candidates = ()

hits = [pid_map[c] for c in candidates if c in loaded]
if hits:
    print("COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME")
    for p in hits:
        print(f"proc {{p}} everflow 10u IPv4 0x1 0t0 TCP 127.0.0.1:{{port}} (LISTEN)")
    sys.exit(0)
sys.exit(1)
PY
""",
    )

    curl_stub = _make_stub(
        tmp_path / "curl",
        """
url="${@: -1}"
if [[ "$url" == *":8943/mcp" ]]; then
  printf '405'
  exit 0
fi
if [[ "$url" == *":8950/health" ]]; then
  printf '{"ok":true}'
  exit 0
fi
exit 7
""",
    )

    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
        "DALTON_LAUNCH_AGENTS_DIR": str(launch_agents),
        "DALTON_LOGS_DIR": str(logs_dir),
        "LAUNCHCTL_BIN": str(launchctl_stub),
        "LSOF_BIN": str(lsof_stub),
        "CURL_BIN": str(curl_stub),
        "HOST_SERVICES_PORT_RELEASE_TIMEOUT_S": "2",
        "HOST_SERVICES_HEALTH_TIMEOUT_S": "2",
        "HOST_SERVICES_HEALTH_INTERVAL_S": "0",
    }
    return {
        "env": env,
        "ocw_root": ocw_root,
        "research_home": research_home,
        "data_root": data_root,
        "launch_agents": launch_agents,
        "launchctl_log": launchctl_log,
        "loaded_file": loaded_file,
        "watchdog_state_file": watchdog_state_file,
    }


def test_legacy_mode_renders_only_dalton_ocw_root_and_does_not_copy_files(
    tmp_path: Path,
) -> None:
    ctx = _setup_switch_env(tmp_path, mode="legacy")
    env = ctx["env"]
    ocw_root: Path = ctx["ocw_root"]
    research_home: Path = ctx["research_home"]
    launch_agents: Path = ctx["launch_agents"]

    # Remove new-home copies to verify --apply in legacy mode never copies into research_home
    (research_home / "config" / "guidepoint_mcp.env").unlink()
    (research_home / "config" / "guidepoint_mcp_tokens.json").unlink()

    plan_res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--plan"], env=env, capture_output=True, text=True)
    assert plan_res.returncode == 0, plan_res.stderr
    assert "DETECTED_MODE:     legacy" in plan_res.stdout
    assert "PLIST_RUNTIME_ENV: DALTON_OCW_ROOT=" in plan_res.stdout
    assert "bridge_tok_never_print" not in plan_res.stdout
    assert "secret_val_never_print" not in plan_res.stdout

    apply_res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--apply"], env=env, capture_output=True, text=True)
    assert apply_res.returncode == 0, apply_res.stderr + apply_res.stdout

    assert not (research_home / "config" / "guidepoint_mcp.env").exists()
    assert not (research_home / "config" / "guidepoint_mcp_tokens.json").exists()

    for label in (
        "com.dalton.alphaengine-mcp",
        "com.dalton.alphaengine-desktop-watchdog",
        "com.dalton.guidepoint-mcp-proxy",
    ):
        plist_data = plistlib.loads((launch_agents / f"{label}.plist").read_bytes())
        env_vars = plist_data["EnvironmentVariables"]
        assert env_vars["DALTON_OCW_ROOT"] == str(ocw_root.resolve())
        assert "DALTON_RESEARCH_HOME" not in env_vars

    gp_env_vars = plistlib.loads((launch_agents / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes())[
        "EnvironmentVariables"
    ]
    assert gp_env_vars["GUIDEPOINT_ENV_FILE"] == str(ocw_root.resolve() / "config" / "guidepoint_mcp.env")
    assert gp_env_vars["GUIDEPOINT_TOKENS_FILE"] == str(ocw_root.resolve() / "config" / "guidepoint_mcp_tokens.json")


def test_new_mode_injects_dalton_research_home_and_runs_disable_on_switch_and_rollback(
    tmp_path: Path,
) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new")
    env = ctx["env"]
    research_home: Path = ctx["research_home"]
    launch_agents: Path = ctx["launch_agents"]
    launchctl_log: Path = ctx["launchctl_log"]

    apply_res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--apply"], env=env, capture_output=True, text=True)
    assert apply_res.returncode == 0, apply_res.stderr + apply_res.stdout

    for label in (
        "com.dalton.alphaengine-mcp",
        "com.dalton.alphaengine-desktop-watchdog",
        "com.dalton.guidepoint-mcp-proxy",
    ):
        env_vars = plistlib.loads((launch_agents / f"{label}.plist").read_bytes())["EnvironmentVariables"]
        assert env_vars["DALTON_RESEARCH_HOME"] == str(research_home.resolve())
        assert "DALTON_OCW_ROOT" not in env_vars

    gp_env_vars = plistlib.loads((launch_agents / "com.dalton.guidepoint-mcp-proxy.plist").read_bytes())[
        "EnvironmentVariables"
    ]
    assert gp_env_vars["GUIDEPOINT_ENV_FILE"] == str(research_home.resolve() / "config" / "guidepoint_mcp.env")
    assert gp_env_vars["GUIDEPOINT_TOKENS_FILE"] == str(
        research_home.resolve() / "config" / "guidepoint_mcp_tokens.json"
    )

    apply_log = launchctl_log.read_text(encoding="utf-8")
    for old_label in (
        "ai.openclaw.alphaengine-mcp",
        "ai.openclaw.alphaengine-desktop-watchdog",
        "ai.openclaw.guidepoint-mcp-proxy",
    ):
        assert f"disable gui/" in apply_log and old_label in apply_log

    # Rollback disables new labels and enables+bootstraps old labels
    launchctl_log.unlink()
    rb_res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--rollback"], env=env, capture_output=True, text=True)
    assert rb_res.returncode == 0, rb_res.stderr + rb_res.stdout
    assert "已恢复的服务 (restored):" in rb_res.stdout
    assert "恢复失败的服务 (failed): [none]" in rb_res.stdout

    rb_log = launchctl_log.read_text(encoding="utf-8")
    for new_label in (
        "com.dalton.alphaengine-mcp",
        "com.dalton.alphaengine-desktop-watchdog",
        "com.dalton.guidepoint-mcp-proxy",
    ):
        assert f"disable " in rb_log and new_label in rb_log


def test_aborts_without_starting_new_service_when_port_not_released(tmp_path: Path) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new")
    env = ctx["env"]
    launchctl_log: Path = ctx["launchctl_log"]

    # Stub lsof that always reports port 8950 busy
    sticky_lsof = _make_stub(
        tmp_path / "lsof_sticky",
        """
echo "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME"
echo "node 1101 everflow 10u IPv4 0x1 0t0 TCP 127.0.0.1:8950 (LISTEN)"
exit 0
""",
    )
    env["LSOF_BIN"] = str(sticky_lsof)

    res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--apply"], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    log_text = launchctl_log.read_text(encoding="utf-8")
    assert "bootstrap" not in [line.split()[0] for line in log_text.splitlines() if "com.dalton." in line]
    assert "com.dalton.alphaengine-mcp" not in ctx["loaded_file"].read_text(encoding="utf-8")
    assert "ai.openclaw.alphaengine-mcp" in ctx["loaded_file"].read_text(encoding="utf-8")


def test_health_check_fails_and_rolls_back_when_port_owned_by_other_pid(tmp_path: Path) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new")
    env = ctx["env"]
    loaded_file: Path = ctx["loaded_file"]

    # Return rogue PID 9999 for port 8943 when com.dalton.guidepoint-mcp-proxy is loaded,
    # while returning real PID 1103 when ai.openclaw.guidepoint-mcp-proxy is restored.
    rogue_lsof = _make_stub(
        tmp_path / "lsof_rogue",
        f"""
python3 - "{loaded_file}" "$@" <<'PY'
import sys
from pathlib import Path
loaded = set(line.strip() for line in Path(sys.argv[1]).read_text().splitlines() if line.strip())
port = ""
for a in sys.argv[2:]:
    if a.startswith("-iTCP:"):
        port = a.split(":", 1)[1]
if port == "8950":
    if "com.dalton.alphaengine-mcp" in loaded:
        print("COMMAND PID USER\\nnode 2101 u")
        sys.exit(0)
    if "ai.openclaw.alphaengine-mcp" in loaded:
        print("COMMAND PID USER\\nnode 1101 u")
        sys.exit(0)
if port == "8943":
    if "com.dalton.guidepoint-mcp-proxy" in loaded:
        print("COMMAND PID USER\\npython 9999 u")
        sys.exit(0)
    if "ai.openclaw.guidepoint-mcp-proxy" in loaded:
        print("COMMAND PID USER\\npython 1103 u")
        sys.exit(0)
sys.exit(1)
PY
""",
    )
    env["LSOF_BIN"] = str(rogue_lsof)

    res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--apply"], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    combined = res.stdout + "\n" + res.stderr
    assert "alphaengine-mcp:com.dalton.alphaengine-mcp" in combined
    assert "guidepoint-mcp-proxy:ai.openclaw.guidepoint-mcp-proxy" in combined
    assert "ai.openclaw.guidepoint-mcp-proxy" in loaded_file.read_text(encoding="utf-8")
    assert "com.dalton.guidepoint-mcp-proxy" not in loaded_file.read_text(encoding="utf-8")


def test_rollback_failure_reports_failed_services_and_exits_nonzero(tmp_path: Path) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new", preloaded_new=True)
    env = ctx["env"]
    launch_agents: Path = ctx["launch_agents"]

    # Delete one legacy plist to force rollback failure on guidepoint-mcp-proxy
    (launch_agents / "ai.openclaw.guidepoint-mcp-proxy.plist").unlink()

    res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--rollback"], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    assert "已恢复的服务 (restored): [alphaengine-mcp:ai.openclaw.alphaengine-mcp alphaengine-desktop-watchdog:ai.openclaw.alphaengine-desktop-watchdog]" in res.stdout
    assert "恢复失败的服务 (failed): [guidepoint-mcp-proxy:ai.openclaw.guidepoint-mcp-proxy]" in res.stdout


def test_rerender_only_updates_loaded_services_and_restores_plist_on_failure(
    tmp_path: Path,
) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new", preloaded_new=True)
    env = ctx["env"]
    launch_agents: Path = ctx["launch_agents"]
    loaded_file: Path = ctx["loaded_file"]
    research_home: Path = ctx["research_home"]

    # Only alphaengine-mcp and guidepoint-mcp-proxy are loaded; watchdog is NOT loaded
    loaded_file.write_text("com.dalton.alphaengine-mcp\ncom.dalton.guidepoint-mcp-proxy\n", encoding="utf-8")
    (launch_agents / "com.dalton.alphaengine-mcp.plist").write_text("<plist><dict><key>Label</key><string>old_sentinel</string></dict></plist>\n", encoding="utf-8")
    (launch_agents / "com.dalton.guidepoint-mcp-proxy.plist").write_text("<plist><dict><key>Label</key><string>gp_sentinel</string></dict></plist>\n", encoding="utf-8")

    ok_res = subprocess.run(
        ["/bin/bash", str(SWITCH_SH), "--apply", "--rerender-only"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert ok_res.returncode == 0, ok_res.stderr + ok_res.stdout
    assert "[skip] alphaengine-desktop-watchdog: com.dalton.alphaengine-desktop-watchdog is not loaded" in ok_res.stdout
    assert not (launch_agents / "com.dalton.alphaengine-desktop-watchdog.plist").exists()
    assert plistlib.loads((launch_agents / "com.dalton.alphaengine-mcp.plist").read_bytes())["EnvironmentVariables"][
        "DALTON_RESEARCH_HOME"
    ] == str(research_home.resolve())

    # Now load all 3 services with pre-rerender sentinel plists and make service #3 fail health check:
    # verify ALL 3 (including #1 and #2 that already succeeded) are restored to their snapshots.
    loaded_file.write_text(
        "com.dalton.alphaengine-mcp\ncom.dalton.alphaengine-desktop-watchdog\ncom.dalton.guidepoint-mcp-proxy\n",
        encoding="utf-8",
    )
    ae_plist = launch_agents / "com.dalton.alphaengine-mcp.plist"
    wd_plist = launch_agents / "com.dalton.alphaengine-desktop-watchdog.plist"
    gp_plist = launch_agents / "com.dalton.guidepoint-mcp-proxy.plist"
    ae_sentinel = (
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        "<key>Label</key><string>com.dalton.alphaengine-mcp</string>"
        "<key>Sentinel</key><string>pre_ae</string>"
        "</dict></plist>\n"
    )
    wd_sentinel = (
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        "<key>Label</key><string>com.dalton.alphaengine-desktop-watchdog</string>"
        "<key>Sentinel</key><string>pre_wd</string>"
        "<key>EnvironmentVariables</key><dict>"
        f"<key>DALTON_RESEARCH_HOME</key><string>{research_home}</string>"
        "</dict></dict></plist>\n"
    )
    gp_sentinel = (
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        "<key>Label</key><string>com.dalton.guidepoint-mcp-proxy</string>"
        "<key>Sentinel</key><string>pre_rerender</string>"
        "</dict></plist>\n"
    )
    ae_plist.write_text(ae_sentinel, encoding="utf-8")
    wd_plist.write_text(wd_sentinel, encoding="utf-8")
    gp_plist.write_text(gp_sentinel, encoding="utf-8")

    toggle_curl = _make_stub(
        tmp_path / "curl_toggle",
        f"""
url="${{@: -1}}"
if [[ "$url" == *":8950/health" ]]; then
  printf '{{"ok":true}}'
  exit 0
fi
if [[ "$url" == *":8943/mcp" ]]; then
  if ! grep -q "pre_rerender" "{gp_plist}" 2>/dev/null; then
    exit 7
  fi
  printf '405'
  exit 0
fi
exit 7
""",
    )
    env["CURL_BIN"] = str(toggle_curl)
    fail_res = subprocess.run(
        ["/bin/bash", str(SWITCH_SH), "--apply", "--rerender-only"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert fail_res.returncode != 0
    assert ae_plist.read_text(encoding="utf-8") == ae_sentinel
    assert wd_plist.read_text(encoding="utf-8") == wd_sentinel
    assert gp_plist.read_text(encoding="utf-8") == gp_sentinel
    assert "alphaengine-mcp:com.dalton.alphaengine-mcp" in fail_res.stderr
    assert "alphaengine-desktop-watchdog:com.dalton.alphaengine-desktop-watchdog" in fail_res.stderr
    assert "guidepoint-mcp-proxy:com.dalton.guidepoint-mcp-proxy" in fail_res.stderr
    assert not list(launch_agents.glob("*.tmp.*"))


def test_research_runtime_migrate_batch3_copy_list_and_rerender_coordination(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src_ocw"
    dst = tmp_path / "dst_research"
    (src / "config").mkdir(parents=True)
    (src / "state").mkdir(parents=True)
    (src / "temp" / "alphaengine-mcp-downloads").mkdir(parents=True)
    (src / "wiki" / "companies").mkdir(parents=True)
    (src / "wiki" / "sectors").mkdir(parents=True)
    (src / "skills" / "market-digest" / "output").mkdir(parents=True)
    (src / "state" / "sales-notes").mkdir(parents=True)
    (src / "state" / "funda-ai").mkdir(parents=True)
    (src / "state" / "research-images").mkdir(parents=True)
    (src / "state" / "tmtb-alphaengine").mkdir(parents=True)
    (src / "skills" / "company-filings-alert" / "state").mkdir(parents=True)
    (src / "market-memory").mkdir(parents=True)

    (src / "config" / "guidepoint_mcp.env").write_text(
        "GUIDEPOINT_CLIENT_ID=id_never_print\nGUIDEPOINT_CLIENT_SECRET=sec_never_print\n",
        encoding="utf-8",
    )
    (src / "config" / "guidepoint_mcp_tokens.json").write_text(
        '{"access_token":"tok_never_print","refresh_token":"ref_never_print"}\n',
        encoding="utf-8",
    )
    (src / "state" / "alphaengine-desktop-watchdog.json").write_text(
        '{"checked_at":"2026-11-17T00:00:00Z","status":"ready"}\n',
        encoding="utf-8",
    )
    (src / "state" / "alphaengine-desktop-watchdog.last-notified").write_text("1700000000\n", encoding="utf-8")
    (src / "temp" / "alphaengine-mcp-downloads" / "temp.pdf").write_bytes(b"temp download")

    plan_res = subprocess.run(
        ["/bin/bash", str(MIGRATE_SH), "--plan", "--src", str(src), "--dst", str(dst)],
        capture_output=True,
        text=True,
    )
    assert plan_res.returncode == 0, plan_res.stderr
    assert "temp/alphaengine-mcp-downloads" in plan_res.stdout
    assert "不复制" in plan_res.stdout
    assert "config/guidepoint_mcp.env" in plan_res.stdout
    assert "config/guidepoint_mcp_tokens.json" in plan_res.stdout

    # Stub launchctl showing no active bridge/writers during --apply
    launchctl_idle = _make_stub(tmp_path / "launchctl_idle", "exit 113\n")
    apply_res = subprocess.run(
        [
            "/bin/bash",
            str(MIGRATE_SH),
            "--apply",
            "--i-have-paused-writers",
            "--src",
            str(src),
            "--dst",
            str(dst),
        ],
        env={**dict(), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "LAUNCHCTL_BIN": str(launchctl_idle)},
        capture_output=True,
        text=True,
    )
    assert apply_res.returncode == 0, apply_res.stderr + apply_res.stdout
    assert "sec_never_print" not in apply_res.stdout
    assert "tok_never_print" not in apply_res.stdout
    assert stat.S_IMODE((dst / "config" / "guidepoint_mcp.env").stat().st_mode) == 0o600
    assert stat.S_IMODE((dst / "config" / "guidepoint_mcp_tokens.json").stat().st_mode) == 0o600
    assert (dst / "state" / "alphaengine-desktop-watchdog.json").is_file()
    assert (dst / "state" / "alphaengine-desktop-watchdog.last-notified").is_file()
    assert not (dst / "temp" / "alphaengine-mcp-downloads").exists()


def test_research_runtime_migrate_switch_rerenders_loaded_host_services(
    tmp_path: Path,
) -> None:
    ctx = _setup_switch_env(tmp_path, mode="legacy", preloaded_new=True)
    dst = ctx["research_home"]
    src = ctx["ocw_root"]
    launch_agents: Path = ctx["launch_agents"]
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True)

    # Populate minimal valid target home so --check-runtime passes
    for skill, expected_ver in (
        ("findata-analyst", "3.12.9"),
        ("cn-hk-findata", "3.12.9"),
        ("company-wiki", "3.14.6"),
        ("market-digest", "3.13.5"),
        ("employee-reviews", "3.14.6"),
        ("consensus-estimates", "3.14.6"),
    ):
        py = dst / "venvs" / skill / "bin" / "python"
        py.parent.mkdir(parents=True, exist_ok=True)
        py.symlink_to(sys.executable)
        (dst / "venvs" / skill / "pyvenv.cfg").write_text(f"version = {expected_ver}\n", encoding="utf-8")
    for rel_file in (
        "wiki/vectors.db",
        "skills/market-digest/vectors.db",
        "state/sales-notes/manifest.jsonl",
        "state/funda-ai/index.sqlite",
        "state/research-images/index.sqlite",
        "config/config.json",
        "config/eve_tracking_mirror.json",
    ):
        p = dst / rel_file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"enabled": false}\n', encoding="utf-8")
    for rel_dir in (
        "wiki/companies",
        "wiki/sectors",
        "skills/market-digest/output",
        "state/tmtb-alphaengine",
        "market-memory",
    ):
        (dst / rel_dir).mkdir(parents=True, exist_ok=True)

    bridge_env = cfg_dir / "researchdata-bridge.env"
    bridge_env.write_text(
        f"DALTON_BRIDGE_TOKEN=tok_s\nDALTON_BRIDGE_ADMIN_TOKEN=adm_s\nEDGAR_IDENTITY=Test <t@example.com>\nDALTON_OCW_ROOT={src}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("DALTON_BRIDGE_TOKEN=tok_s\n", encoding="utf-8")

    # Seed legacy plists for com.dalton.* pointing to ocw_root
    for lbl in (
        "com.dalton.alphaengine-mcp",
        "com.dalton.alphaengine-desktop-watchdog",
        "com.dalton.guidepoint-mcp-proxy",
    ):
        (launch_agents / f"{lbl}.plist").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>Label</key><string>{lbl}</string></dict></plist>\n',
            encoding="utf-8",
        )

    # Extend curl stub so bridge /healthz also passes during install_bridge.sh
    curl_with_bridge = _make_stub(
        tmp_path / "curl_with_bridge",
        """
url="${@: -1}"
if [[ "$url" == *":8960/healthz" ]]; then
  printf '{"ok":true}'
  exit 0
fi
if [[ "$url" == *":8943/mcp" ]]; then
  printf '405'
  exit 0
fi
if [[ "$url" == *":8950/health" ]]; then
  printf '{"ok":true}'
  exit 0
fi
exit 7
""",
    )

    migrate_env = {
        **ctx["env"],
        "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
        "DALTON_REPO_ENV_FILE": str(tmp_path / ".env"),
        "DALTON_BRIDGE_LOGS_DIR": str(tmp_path / "Logs"),
        "DALTON_OCW_PYTHON": sys.executable,
        "DALTON_BRIDGE_HEALTH_RETRIES": "2",
        "DALTON_BRIDGE_HEALTH_SLEEP_S": "0.1",
        "CURL_BIN": str(curl_with_bridge),
    }

    sw_res = subprocess.run(
        [
            "/bin/bash",
            str(MIGRATE_SH),
            "--switch",
            "--src",
            str(src),
            "--dst",
            str(dst),
            "--bridge-env",
            str(bridge_env),
            "--prepare-config",
            str(cfg_dir / "prepare.json"),
            "--output-config",
            str(cfg_dir / "output.json"),
        ],
        env=migrate_env,
        capture_output=True,
        text=True,
    )
    assert sw_res.returncode == 0, sw_res.stderr + sw_res.stdout
    assert "host_services_switch.sh --apply --rerender-only" in sw_res.stdout

    # Verify all 3 com.dalton.* plists were rerendered onto dst (new mode)
    for lbl in (
        "com.dalton.alphaengine-mcp",
        "com.dalton.alphaengine-desktop-watchdog",
        "com.dalton.guidepoint-mcp-proxy",
    ):
        env_vars = plistlib.loads((launch_agents / f"{lbl}.plist").read_bytes())["EnvironmentVariables"]
        assert env_vars["DALTON_RESEARCH_HOME"] == str(dst.resolve())
        assert "DALTON_OCW_ROOT" not in env_vars


def test_check_runtime_host_services_reports_ports_and_launchd_labels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launchctl_mixed = _make_stub(
        tmp_path / "launchctl_mixed",
        """
target="${2:-}"
case "$target" in
  */com.dalton.alphaengine-mcp|*/com.dalton.alphaengine-desktop-watchdog|*/ai.openclaw.guidepoint-mcp-proxy)
    exit 0
    ;;
  *)
    exit 113
    ;;
esac
""",
    )
    monkeypatch.setenv("LAUNCHCTL_BIN", str(launchctl_mixed))

    def _fake_create_connection(addr: tuple[str, int], timeout: float = 0.5) -> Any:
        assert timeout == 0.5
        if addr == ("127.0.0.1", 8950):
            sock = create_autospec(socket.socket, instance=True)
            sock.__enter__.return_value = sock
            sock.__exit__.return_value = False
            return sock
        raise OSError("connection refused")

    monkeypatch.setattr(rb.socket, "create_connection", _fake_create_connection)
    hs = rb._check_host_services_status()
    assert hs["alphaengine_mcp"]["reachable"] is True
    assert hs["alphaengine_mcp"]["label_kind"] == "new"
    assert hs["alphaengine_mcp"]["active_label"] == "com.dalton.alphaengine-mcp"
    assert hs["alphaengine_desktop_watchdog"]["label_kind"] == "new"
    assert hs["guidepoint_mcp_proxy"]["reachable"] is False
    assert hs["guidepoint_mcp_proxy"]["label_kind"] == "old"
    assert hs["guidepoint_mcp_proxy"]["active_label"] == "ai.openclaw.guidepoint-mcp-proxy"

    monkeypatch.setenv("LAUNCHCTL_BIN", str(tmp_path / "nonexistent_launchctl"))
    hs_unknown = rb._check_host_services_status()
    assert hs_unknown["alphaengine_mcp"]["label_kind"] == "unknown"
    assert hs_unknown["alphaengine_mcp"]["active_label"] == "unknown"
    assert hs_unknown["guidepoint_mcp_proxy"]["label_kind"] == "unknown"


def test_desktop_watchdog_cleans_stale_lock_and_respects_live_lock(tmp_path: Path) -> None:
    app_bundle = tmp_path / "AlphaEngine.app"
    app_exec = app_bundle / "Contents" / "MacOS" / "AlphaEngine"
    app_exec.parent.mkdir(parents=True)
    app_exec.write_text("", encoding="utf-8")

    research_home = tmp_path / "research_home"
    state_dir = research_home / "state"
    state_file = state_dir / "alphaengine-desktop-watchdog.json"

    ps_stub = _make_stub(tmp_path / "ps", f'printf "%s\\n" "{app_exec}"\n')
    open_stub = _make_stub(tmp_path / "open", "exit 0\n")
    osa_stub = _make_stub(tmp_path / "osascript", "exit 0\n")
    curl_stub = _make_stub(
        tmp_path / "curl",
        'printf \'{"ok":true,"authenticated":true}\'\n',
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DALTON_RESEARCH_HOME": str(research_home),
        "ALPHAENGINE_APP_BUNDLE": str(app_bundle),
        "ALPHAENGINE_APP_EXECUTABLE": str(app_exec),
        "PS_BIN": str(ps_stub),
        "OPEN_BIN": str(open_stub),
        "OSASCRIPT_BIN": str(osa_stub),
        "CURL_BIN": str(curl_stub),
    }

    lock_file = state_dir / "alphaengine-desktop-watchdog.pid.lock"
    state_dir.mkdir(parents=True)

    # 1. Lock held by a PID that has exited -> shlock reclaims it; the run removes its own lock on exit
    finished = subprocess.Popen(["/usr/bin/true"])
    finished.wait(timeout=5)
    subprocess.run(["/usr/bin/shlock", "-f", str(lock_file), "-p", str(finished.pid)], check=True, timeout=10)
    # shlock refuses to reclaim a lock changed within the current second (its own race guard).
    time.sleep(1.2)
    res_dead = subprocess.run(["/bin/bash", str(WATCHDOG_SH)], env=env, capture_output=True, text=True, timeout=30)
    assert res_dead.returncode == 0, res_dead.stderr
    assert state_file.is_file()
    assert not lock_file.exists()

    # 2. Lock held by a live PID, however old -> never taken over; the run exits without writing state
    state_file.unlink()
    sleeper = subprocess.Popen(["/bin/sleep", "30"])
    try:
        subprocess.run(["/usr/bin/shlock", "-f", str(lock_file), "-p", str(sleeper.pid)], check=True, timeout=10)
        old_ts = time.time() - 7200
        os.utime(lock_file, (old_ts, old_ts))
        res_live = subprocess.run(["/bin/bash", str(WATCHDOG_SH)], env=env, capture_output=True, text=True, timeout=30)
        assert res_live.returncode == 0
        assert not state_file.exists()
        assert lock_file.read_text(encoding="utf-8").strip() == str(sleeper.pid)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_rollback_resolves_watchdog_state_file_from_plist_env_vars(tmp_path: Path) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new", preloaded_new=True)
    env = ctx["env"]
    launch_agents: Path = ctx["launch_agents"]
    research_home: Path = ctx["research_home"]

    # Legacy watchdog plist points OPENCLAW_WORKSPACE to a distinct directory (not research_home).
    legacy_ws = tmp_path / "distinct_openclaw_ws"
    (legacy_ws / "state").mkdir(parents=True)
    (launch_agents / "ai.openclaw.alphaengine-desktop-watchdog.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0"><dict>'
        "<key>Label</key><string>ai.openclaw.alphaengine-desktop-watchdog</string>"
        "<key>EnvironmentVariables</key><dict>"
        f"<key>OPENCLAW_WORKSPACE</key><string>{legacy_ws}</string>"
        "</dict></dict></plist>\n",
        encoding="utf-8",
    )

    # Ensure ALPHAENGINE_WATCHDOG_STATE_FILE is NOT set; host_services_switch.sh must inspect plist.
    env.pop("ALPHAENGINE_WATCHDOG_STATE_FILE", None)
    res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--rollback"], env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr + res.stdout
    assert (legacy_ws / "state" / "alphaengine-desktop-watchdog.json").is_file()
    assert not (research_home / "state" / "alphaengine-desktop-watchdog.json").exists()


def test_old_watchdog_state_write_during_bootout_does_not_pass_new_watchdog_health_check(
    tmp_path: Path,
) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new")
    env = ctx["env"]
    loaded_file: Path = ctx["loaded_file"]
    launchctl_log: Path = ctx["launchctl_log"]
    research_home: Path = ctx["research_home"]
    wd_state_file = research_home / "state" / "alphaengine-desktop-watchdog.json"

    # Custom launchctl stub:
    # - When booting out ai.openclaw.alphaengine-desktop-watchdog, the dying old watchdog writes a
    #   fresh checked_at = current UTC second right before unloading completes.
    # - When bootstrapping/kickstarting com.dalton.alphaengine-desktop-watchdog, the new watchdog
    #   does NOT write any state file.
    # Because switch_start_utc is captured AFTER old label unloads (and sleep 1 precedes new start),
    # the old watchdog's write must NOT satisfy the new watchdog's health check -> rollback occurs.
    stub_file = Path(env["LAUNCHCTL_BIN"])
    pid_map_json = json.dumps(
        {
            "ai.openclaw.alphaengine-mcp": "1101",
            "com.dalton.alphaengine-mcp": "2101",
            "ai.openclaw.guidepoint-mcp-proxy": "1103",
            "com.dalton.guidepoint-mcp-proxy": "2103",
        }
    )
    _make_stub(
        stub_file,
        f"""
cmd="${{1:-}}"
shift || true
printf '%s %s\\n' "$cmd" "$*" >> "{launchctl_log}"
python3 - "$cmd" "{loaded_file}" "{wd_state_file}" '{pid_map_json}' "$@" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

cmd, loaded_p, wd_p, pid_json, *args = sys.argv[1:]
loaded_file = Path(loaded_p)
loaded = set(line.strip() for line in loaded_file.read_text().splitlines() if line.strip())
pid_map = json.loads(pid_json)

def label_from(arg: str) -> str:
    return arg.split("/")[-1]

if cmd == "print":
    lbl = label_from(args[0])
    if lbl in loaded:
        print(f"gui/501/{{lbl}} = {{{{")
        if "alphaengine-desktop-watchdog" in lbl:
            print("    state = waiting")
        else:
            print("    state = running")
            print(f"    pid = {{pid_map.get(lbl, '9999')}}")
        print("}}}}")
        sys.exit(0)
    sys.exit(113)
elif cmd == "bootout":
    lbl = label_from(args[0])
    if lbl == "ai.openclaw.alphaengine-desktop-watchdog":
        wd = Path(wd_p)
        wd.parent.mkdir(parents=True, exist_ok=True)
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        wd.write_text(json.dumps({{"checked_at": now_utc, "status": "ready"}}) + "\\n")
    loaded.discard(lbl)
    loaded_file.write_text("\\n".join(sorted(loaded)) + "\\n")
    sys.exit(0)
elif cmd == "bootstrap":
    plist_path = Path(args[1])
    lbl = plist_path.name[:-6]
    loaded.add(lbl)
    loaded_file.write_text("\\n".join(sorted(loaded)) + "\\n")
    sys.exit(0)
elif cmd == "kickstart":
    lbl = label_from(args[-1])
    if lbl not in loaded:
        sys.exit(113)
    if lbl == "ai.openclaw.alphaengine-desktop-watchdog":
        wd = Path(wd_p)
        wd.parent.mkdir(parents=True, exist_ok=True)
        wd.write_text('{{"checked_at":"2099-01-01T00:00:00Z","status":"ready"}}\\n')
    sys.exit(0)
elif cmd in ("enable", "disable"):
    sys.exit(0)
sys.exit(0)
PY
""",
    )

    res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--apply"], env=env, capture_output=True, text=True)
    assert res.returncode != 0
    combined = res.stdout + "\n" + res.stderr
    assert "alphaengine-desktop-watchdog:ai.openclaw.alphaengine-desktop-watchdog" in combined
    # Verify watchdog never receives `kickstart -k`
    for line in launchctl_log.read_text(encoding="utf-8").splitlines():
        if "alphaengine-desktop-watchdog" in line and line.startswith("kickstart "):
            assert " -k " not in f" {line} "


def test_rerender_only_restores_earlier_services_when_a_later_template_fails_to_render(
    tmp_path: Path,
) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new", preloaded_new=True)
    env = ctx["env"]
    launch_agents: Path = ctx["launch_agents"]
    loaded_file: Path = ctx["loaded_file"]

    # A repo copy whose watchdog template (the second service) cannot be rendered.
    fake_repo = tmp_path / "fake_repo"
    shutil.copytree(REPO_ROOT / "scripts" / "dalton" / "launchd", fake_repo / "scripts" / "dalton" / "launchd")
    (fake_repo / "scripts" / "dalton" / "launchd" / "com.dalton.alphaengine-desktop-watchdog.plist.template").unlink()
    env["DALTON_REPO_ROOT"] = str(fake_repo)

    loaded_file.write_text(
        "com.dalton.alphaengine-mcp\ncom.dalton.alphaengine-desktop-watchdog\ncom.dalton.guidepoint-mcp-proxy\n",
        encoding="utf-8",
    )
    sentinels = {}
    for label in ("com.dalton.alphaengine-mcp", "com.dalton.alphaengine-desktop-watchdog", "com.dalton.guidepoint-mcp-proxy"):
        text = (
            '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
            f"<key>Label</key><string>{label}</string>"
            "<key>Sentinel</key><string>pre</string>"
            "</dict></plist>\n"
        )
        (launch_agents / f"{label}.plist").write_text(text, encoding="utf-8")
        sentinels[label] = text

    res = subprocess.run(
        ["/bin/bash", str(SWITCH_SH), "--apply", "--rerender-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert res.returncode != 0
    assert "rendering" in res.stderr
    for label, text in sentinels.items():
        assert (launch_agents / f"{label}.plist").read_text(encoding="utf-8") == text
    assert not list(launch_agents.glob("*.tmp.*"))


def test_watchdog_state_path_honours_state_dir_from_plist(tmp_path: Path) -> None:
    ctx = _setup_switch_env(tmp_path, mode="new", preloaded_new=True)
    env = ctx["env"]
    launch_agents: Path = ctx["launch_agents"]
    research_home: Path = ctx["research_home"]

    custom_state = tmp_path / "custom_watchdog_state"
    custom_state.mkdir()
    (launch_agents / "ai.openclaw.alphaengine-desktop-watchdog.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0"><dict>'
        "<key>Label</key><string>ai.openclaw.alphaengine-desktop-watchdog</string>"
        "<key>EnvironmentVariables</key><dict>"
        f"<key>OPENCLAW_WORKSPACE</key><string>{tmp_path / 'unused_ws'}</string>"
        f"<key>ALPHAENGINE_WATCHDOG_STATE_DIR</key><string>{custom_state}</string>"
        "</dict></dict></plist>\n",
        encoding="utf-8",
    )
    env.pop("ALPHAENGINE_WATCHDOG_STATE_FILE", None)
    res = subprocess.run(["/bin/bash", str(SWITCH_SH), "--rollback"], env=env, capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, res.stderr + res.stdout
    assert (custom_state / "alphaengine-desktop-watchdog.json").is_file()
    assert not (research_home / "state" / "alphaengine-desktop-watchdog.json").exists()
