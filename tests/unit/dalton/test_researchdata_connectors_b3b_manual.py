"""Tests for Batch 3b manual downtime cutover runbook, atomic snapshot, and one-click rollback.

Covers:
- Atomic snapshot creation, fsync/rename cleanliness, SHA-256 manifest verification,
  and tamper rejection (`scripts/dalton/ops/prod/snapshot_research_host.sh`).
- End-to-end snapshot -> simulated post-cutover mutation -> one-click rollback
  (`scripts/dalton/ops/prod/rollback_research_host.sh`) using isolated fake
  `launchctl`, `docker`, and `curl` under `tmp_path`, asserting every file,
  symlink, service state, container recreate, and live Wiki/Guidepoint probe
  returns to the exact snapshot baseline with zero credentials in CLI argv.
- Default selection of the latest valid snapshot.
- Mid-rollback failure injection across every major step keeping the
  maintenance admission gate CLOSED and emitting manual remediation guidance.
- `--dry-run` performing zero file or service mutations.
- Idempotent repeated rollback execution (both after success and after recovery).
- Automated parsing of every command line in
  `scripts/dalton/ops/prod/RESEARCH_HOST_SWITCH_RUNBOOK.md` asserting every
  referenced repository script, subcommand, and `--flag` actually exists,
  and verifying no retired automation commands or internal codenames appear.
- `create_autospec` double contract test and Standalone Core zero-hit check.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PROD_OPS = REPO_ROOT / "scripts" / "dalton" / "ops" / "prod"
PREFLIGHT_PY = PROD_OPS / "preflight.py"
SNAPSHOT_SH = PROD_OPS / "snapshot_research_host.sh"
ROLLBACK_SH = PROD_OPS / "rollback_research_host.sh"
SWITCH_SH = PROD_OPS / "switch_research_host.sh"
RUNBOOK_MD = PROD_OPS / "RESEARCH_HOST_SWITCH_RUNBOOK.md"
CHECK_CORE_PY = REPO_ROOT / "scripts" / "dalton" / "ops" / "check_core_release_package.py"
BRIDGE_SUBPROC_PY = REPO_ROOT / "scripts" / "dalton" / "bridge_subprocess_env.py"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


preflight = _load_module("dalton_preflight_manual_under_test", PREFLIGHT_PY)


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_plist(path: Path, label: str, env_vars: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": label,
        "ProgramArguments": ["/usr/bin/true"],
        "EnvironmentVariables": env_vars,
    }
    path.write_bytes(plistlib.dumps(payload))
    path.chmod(0o644)
    return path


def _snapshot_tree_hashes(paths: list[Path]) -> dict[str, tuple[str, bytes | str | None]]:
    out: dict[str, tuple[str, bytes | str | None]] = {}
    for p in paths:
        if p.is_symlink():
            out[str(p)] = ("symlink", os.readlink(p))
        elif p.is_file():
            out[str(p)] = ("file", p.read_bytes())
        else:
            out[str(p)] = ("absent", None)
    return out


def _build_isolated_harness(tmp_path: Path) -> dict[str, Any]:
    """Create a fully isolated fake repo/home + stateful fake launchctl/docker/curl."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    state_file = tmp_path / "fake_state.json"
    calls_log = tmp_path / "calls.log"
    curl_argv_log = tmp_path / "curl_argv.log"

    # Mirror minimal repo structure so scripts and `src.dalton.maintenance_gate` work cleanly
    (repo / "var" / "dalton" / "cutover-snapshots" / "research-host").mkdir(parents=True)
    (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: test\n", encoding="utf-8")
    (repo / "docker-compose.dalton.yml").write_text("services:\n  backend:\n    image: test\n", encoding="utf-8")
    # Symlink `src` and `scripts` read-only targets into fake repo so `preflight.py` can import
    # `src.dalton.maintenance_gate` and `guidepoint_lock` without touching real repo `var/dalton`.
    os.symlink(REPO_ROOT / "src", repo / "src")
    os.symlink(REPO_ROOT / "scripts", repo / "scripts")

    # Frontend releases with `web/releases/current -> rel-before`
    rel_before = repo / "web" / "releases" / "rel-before"
    rel_after = repo / "web" / "releases" / "rel-after"
    rel_before.mkdir(parents=True)
    rel_after.mkdir(parents=True)
    (rel_before / "version.json").write_text(json.dumps({"build": "before-111"}) + "\n", encoding="utf-8")
    (rel_after / "version.json").write_text(json.dumps({"build": "after-222"}) + "\n", encoding="utf-8")
    web_cur = repo / "web" / "releases" / "current"
    os.symlink("rel-before", web_cur)

    # Config & secret files (all synthetic test tokens)
    bridge_token = "test-bridge-secret-token-alpha"
    rh_token = "test-rh-secret-token-beta"
    rh_admin_token = "test-rh-admin-secret-gamma"
    proxy_admin_token = "test-proxy-admin-secret-epsilon"
    svc_token = "test-internal-svc-secret-delta"

    repo_env = repo / ".env"
    repo_env.write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={svc_token}\n",
        encoding="utf-8",
    )
    repo_env.chmod(0o600)

    cfg_dir = home / ".config" / "dalton"
    cfg_dir.mkdir(parents=True)
    bridge_env = cfg_dir / "researchdata-bridge.env"
    bridge_env.write_text(
        f"DALTON_BRIDGE_TOKEN={bridge_token}\n"
        f"DALTON_BRIDGE_ADMIN_TOKEN=test-bridge-admin-tok\n"
        f"DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8943/mcp\n",
        encoding="utf-8",
    )
    bridge_env.chmod(0o600)

    rh_env = cfg_dir / "research-host.env"
    rh_env.write_text(
        f"DALTON_RESEARCH_HOST_TOKEN={rh_token}\n"
        f"DALTON_RESEARCH_ADMIN_TOKEN={rh_admin_token}\n"
        f"DALTON_BRIDGE_TOKEN={bridge_token}\n"
        f"GUIDEPOINT_CLIENT_ID=fake-cid\n"
        f"GUIDEPOINT_CLIENT_SECRET=fake-csec\n",
        encoding="utf-8",
    )
    rh_env.chmod(0o600)

    research_home = home / "Library" / "Application Support" / "Everflow" / "research"
    gp_cfg_dir = research_home / "config"
    gp_cfg_dir.mkdir(parents=True)
    gp_env = gp_cfg_dir / "guidepoint_mcp.env"
    gp_env.write_text(
        f"GUIDEPOINT_CLIENT_ID=fake-cid\n"
        f"GUIDEPOINT_CLIENT_SECRET=fake-csec\n"
        f"GUIDEPOINT_PROXY_ADMIN_TOKEN={proxy_admin_token}\n",
        encoding="utf-8",
    )
    gp_env.chmod(0o600)

    now_ts = int(time.time())
    tokens_file = gp_cfg_dir / "guidepoint_mcp_tokens.json"
    tokens_file.write_text(
        json.dumps(
            {
                "access_token": "tok-before",
                "refresh_token": "ref-before",
                "obtained_at": now_ts - 100,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    tokens_file.chmod(0o600)

    journal_file = repo / "var" / "dalton" / "cutover-snapshots" / "research-host" / "handoff-journal.json"
    journal_file.write_text(
        json.dumps({"operation": "baseline", "target_owner": "legacy_proxy_8943", "completed": True}) + "\n",
        encoding="utf-8",
    )
    journal_file.chmod(0o600)

    plist_dir = home / "Library" / "LaunchAgents"
    proxy_plist = _make_plist(
        plist_dir / "com.dalton.guidepoint-mcp-proxy.plist",
        "com.dalton.guidepoint-mcp-proxy",
        {
            "GUIDEPOINT_TOKENS_FILE": str(tokens_file),
            "GUIDEPOINT_ENV_FILE": str(gp_env),
            "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(journal_file),
            "DALTON_RESEARCH_HOME": str(research_home),
        },
    )
    bridge_plist = _make_plist(
        plist_dir / "com.dalton.researchdata-bridge.plist",
        "com.dalton.researchdata-bridge",
        {
            "DALTON_REPO_ENV_FILE": str(repo_env),
            "DALTON_BRIDGE_ENV_FILE": str(bridge_env),
            "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(journal_file),
        },
    )
    rh_plist = plist_dir / "com.dalton.research-host.plist"
    # Before cutover, research-host plist is absent from LaunchAgents.
    assert not rh_plist.exists()

    adm_file = repo / "var" / "dalton" / "maintenance-admission.json"
    adm_file.write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 1,
                "paused": False,
                "pause_host_direct": False,
                "allow_workspace_ids": [],
                "reason": "normal",
                "operation": "",
                "rehearsal_keep_paused_until": None,
                "updated_at": "2026-11-27T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    initial_state = {
        "loaded": {
            "com.dalton.guidepoint-mcp-proxy": str(proxy_plist),
            "com.dalton.researchdata-bridge": str(bridge_plist),
        },
        "backend_running": True,
        "recreate_count": 0,
        "probes": [],
    }
    state_file.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")

    fake_launchctl = _write_exec(
        bin_dir / "launchctl",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, sys
            from pathlib import Path

            state_path = Path({str(state_file)!r})
            calls_log = Path({str(calls_log)!r})
            args = sys.argv[1:]
            with calls_log.open("a", encoding="utf-8") as fh:
                fh.write("launchctl " + " ".join(args) + "\\n")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            loaded = state.setdefault("loaded", {{}})
            keepalive_waiting = set(state.get("keepalive_waiting", []))
            cmd = args[0] if args else ""

            if cmd == "list":
                print("PID\\tStatus\\tLabel")
                for idx, lbl in enumerate(sorted(loaded.keys()), start=1001):
                    if lbl in keepalive_waiting:
                        print(f"-\\t0\\t{{lbl}}")
                    else:
                        print(f"{{idx}}\\t0\\t{{lbl}}")
                sys.exit(0)
            if cmd == "print":
                target = args[1] if len(args) > 1 else ""
                lbl = target.split("/")[-1]
                if lbl in loaded:
                    if lbl in keepalive_waiting:
                        print("gui/501/" + lbl + " = {{\\n\\tstate = waiting\\n\\tkeepalive = true\\n}}")
                    else:
                        print(f"service {{lbl}} = active")
                    sys.exit(0)
                sys.exit(113)
            if cmd == "bootout":
                target = args[1] if len(args) > 1 else ""
                lbl = target.split("/")[-1]
                if len(args) > 2 and "/" not in target:
                    lbl = Path(args[2]).stem
                fail_lbl = os.environ.get("FAKE_FAIL_BOOTOUT_LABEL", "")
                if fail_lbl and lbl == fail_lbl:
                    print(f"simulated bootout failure for {{lbl}}", file=sys.stderr)
                    sys.exit(1)
                if lbl in loaded:
                    del loaded[lbl]
                    if lbl in keepalive_waiting:
                        state["keepalive_waiting"] = [x for x in state.get("keepalive_waiting", []) if x != lbl]
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    sys.exit(0)
                sys.exit(3)
            if cmd == "bootstrap":
                plist_path = args[2] if len(args) > 2 else ""
                lbl = Path(plist_path).stem
                fail_lbl = os.environ.get("FAKE_FAIL_BOOTSTRAP_LABEL", "")
                if fail_lbl and lbl == fail_lbl:
                    print(f"simulated bootstrap failure for {{lbl}}", file=sys.stderr)
                    sys.exit(1)
                if not Path(plist_path).is_file():
                    print(f"missing plist {{plist_path}}", file=sys.stderr)
                    sys.exit(1)
                loaded[lbl] = plist_path
                if lbl in keepalive_waiting:
                    state["keepalive_waiting"] = [x for x in state.get("keepalive_waiting", []) if x != lbl]
                if lbl == "com.dalton.guidepoint-mcp-proxy":
                    state["draining_8943"] = False
                elif lbl == "com.dalton.research-host":
                    state["draining_8951"] = False
                elif lbl == "com.dalton.researchdata-bridge":
                    state["draining_8960"] = False
                state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                sys.exit(0)
            sys.exit(0)
            """
        ),
    )

    fake_docker = _write_exec(
        bin_dir / "docker",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, sys
            from pathlib import Path

            state_path = Path({str(state_file)!r})
            calls_log = Path({str(calls_log)!r})
            adm_file = Path({str(adm_file)!r})
            args = sys.argv[1:]
            with calls_log.open("a", encoding="utf-8") as fh:
                fh.write("docker " + " ".join(args) + "\\n")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            if not args:
                sys.exit(1)
            cmd = args[0]

            if cmd == "inspect":
                if "{{{{.State.Running}}}}" in " ".join(args):
                    print("true" if state.get("backend_running", True) else "false")
                    sys.exit(0)
                if "com.docker.compose.project" in " ".join(args):
                    print("dalton-test")
                    sys.exit(0)
                print("")
                sys.exit(0)

            if cmd == "exec":
                if "cat" in args and any("maintenance-admission.json" in a for a in args):
                    if os.environ.get("FAKE_FAIL_DOCKER_EXEC_CAT") == "1":
                        print("simulated docker exec cat failure", file=sys.stderr)
                        sys.exit(1)
                    if not state.get("backend_running", True):
                        sys.exit(1)
                    if adm_file.is_file():
                        print(adm_file.read_text(encoding="utf-8"), end="")
                        sys.exit(0)
                    sys.exit(1)
                if "psql" in args:
                    if os.environ.get("FAKE_PG_DOWN") == "1":
                        print("psql: error: connection to server failed", file=sys.stderr)
                        sys.exit(2)
                    if os.environ.get("FAKE_PG_BUSY") == "1":
                        print("1")
                    else:
                        print("0")
                    sys.exit(0)
                sys.exit(0)

            if cmd == "compose":
                if "up" in args and "backend" in args:
                    if os.environ.get("FAKE_FAIL_COMPOSE") == "1":
                        print("simulated docker compose up failure", file=sys.stderr)
                        sys.exit(1)
                    state["recreate_count"] = int(state.get("recreate_count", 0)) + 1
                    state["backend_running"] = True
                    if state.get("backend_adopts_repo_env_token"):
                        repo_env_p = Path({str(repo_env)!r})
                        if repo_env_p.is_file():
                            for raw_ln in repo_env_p.read_text(encoding="utf-8").splitlines():
                                ln = raw_ln.strip()
                                if ln.startswith("INTERNAL_SERVICE_TOKEN="):
                                    state["backend_service_token"] = ln.split("=", 1)[1].strip()
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    sys.exit(0)
                sys.exit(0)

            sys.exit(0)
            """
        ),
    )

    fake_ps = _write_exec(
        bin_dir / "ps",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, sys
            from pathlib import Path

            state = json.loads(Path({str(state_file)!r}).read_text(encoding="utf-8"))
            loaded = state.get("loaded", {{}})
            waiting = set(state.get("keepalive_waiting", []))
            if "com.dalton.guidepoint-mcp-proxy" in loaded and "com.dalton.guidepoint-mcp-proxy" not in waiting:
                print("2001 /opt/homebrew/bin/python3.12 /repo/scripts/dalton/research_runtime/scripts/guidepoint_mcp_proxy.py")
            if "com.dalton.research-host" in loaded and "com.dalton.research-host" not in waiting:
                print("2002 /repo/.venv/bin/python -m src.dalton.research_host")
            if "com.dalton.researchdata-bridge" in loaded and "com.dalton.researchdata-bridge" not in waiting:
                print("2003 /repo/.venv/bin/python /repo/scripts/dalton/researchdata_bridge.py")
            for extra in state.get("extra_ps_lines", []):
                print(extra)
            sys.exit(0)
            """
        ),
    )

    fake_curl = _write_exec(
        bin_dir / "curl",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, re, sys
            from pathlib import Path

            state_path = Path({str(state_file)!r})
            calls_log = Path({str(calls_log)!r})
            argv_log = Path({str(curl_argv_log)!r})
            adm_file = Path({str(adm_file)!r})
            journal_file = Path({str(journal_file)!r})
            repo_env_file = Path({str(repo_env)!r})
            tokens_file = Path({str(tokens_file)!r})
            web_cur_path = Path({str(web_cur)!r})
            expected_rh_admin = {rh_admin_token!r}
            expected_proxy_admin = {proxy_admin_token!r}
            expected_bridge_token = {bridge_token!r}
            expected_bridge_admin = "test-bridge-admin-tok"
            expected_svc_token = {svc_token!r}

            args = sys.argv[1:]
            with argv_log.open("a", encoding="utf-8") as fh:
                fh.write(" ".join(args) + "\\n")

            method = "GET"
            data = ""
            cfg_headers = {{}}
            write_fmt = ""
            out_file = ""
            hdr_dump_file = ""
            url = ""

            i = 0
            while i < len(args):
                a = args[i]
                if a == "-X" and i + 1 < len(args):
                    method = args[i + 1]
                    i += 2
                elif a in ("--data-binary", "-d") and i + 1 < len(args):
                    data = args[i + 1]
                    i += 2
                elif a == "-K" and i + 1 < len(args):
                    cfg_txt = Path(args[i + 1]).read_text(encoding="utf-8")
                    for m in re.finditer(r'header\\s*=\\s*"([^"]+)"', cfg_txt):
                        hdr = m.group(1)
                        if ":" in hdr:
                            hk, hv = hdr.split(":", 1)
                            cfg_headers[hk.strip().lower()] = hv.strip()
                    i += 2
                elif a == "-D" and i + 1 < len(args):
                    hdr_dump_file = args[i + 1]
                    i += 2
                elif a == "-w" and i + 1 < len(args):
                    write_fmt = args[i + 1]
                    i += 2
                elif a == "-o" and i + 1 < len(args):
                    out_file = args[i + 1]
                    i += 2
                elif a in ("--max-time", "-H"):
                    i += 2
                elif a.startswith("-"):
                    i += 1
                else:
                    url = a
                    i += 1

            with calls_log.open("a", encoding="utf-8") as fh:
                fh.write(f"curl {{method}} {{url}}\\n")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            loaded = state.get("loaded", {{}})

            def emit(code: int, payload: object, resp_headers: dict | None = None) -> None:
                body_str = json.dumps(payload) if not isinstance(payload, str) else payload
                if hdr_dump_file:
                    hdr_lines = [f"HTTP/1.1 {{code}} OK"]
                    for hk, hv in (resp_headers or {{}}).items():
                        hdr_lines.append(f"{{hk}}: {{hv}}")
                    Path(hdr_dump_file).write_text("\\r\\n".join(hdr_lines) + "\\r\\n\\r\\n", encoding="utf-8")
                if out_file and out_file != "/dev/null":
                    Path(out_file).write_text(body_str, encoding="utf-8")
                elif not out_file:
                    sys.stdout.write(body_str)
                if write_fmt:
                    sys.stdout.write(write_fmt.replace("%{{http_code}}", str(code)).replace("\\\\n", "\\n"))
                sys.exit(0)

            def conn_refused() -> None:
                if write_fmt:
                    sys.stdout.write(write_fmt.replace("%{{http_code}}", "000").replace("\\\\n", "\\n"))
                sys.exit(7)

            def timed_out() -> None:
                sys.stderr.write("curl: (28) Operation timed out\\n")
                sys.exit(28)

            # :8951 research-host (real production /healthz JSON shape)
            if ":8951" in url:
                up = ("com.dalton.research-host" in loaded) or (os.environ.get("FAKE_KEEP_8951_UP") == "1")
                if not up:
                    conn_refused()
                if url.endswith("/admin/drain"):
                    if cfg_headers.get("authorization") != f"Bearer {{expected_rh_admin}}":
                        emit(401, {{"ok": False, "error": "Unauthorized"}})
                    state["draining_8951"] = True
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(200, {{"ok": True, "draining": True, "in_flight": int(state.get("in_flight_8951", 0))}})
                if url.endswith("/healthz"):
                    if os.environ.get("FAKE_TIMEOUT_8951_HEALTHZ") == "1":
                        timed_out()
                    draining_8951 = bool(state.get("draining_8951", False))
                    in_flight_8951 = int(state.get("in_flight_8951", 0))
                    if in_flight_8951 > 0:
                        state["in_flight_8951"] = in_flight_8951 - 1
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(
                        200,
                        {{
                            "ok": not draining_8951,
                            "status": "draining" if draining_8951 else "ok",
                            "service": "dalton-research-host",
                            "version": "1.0.0",
                            "profile": "everflow",
                            "draining": draining_8951,
                            "in_flight": in_flight_8951,
                            "connectors": {{"company-wiki": True, "guidepoint": True}},
                            "probes": {{
                                "guidepoint": {{
                                    "owner_lock_held": True,
                                    "standby": False,
                                    "target_owner": "research_host_8951",
                                    "token_file_exists": tokens_file.is_file(),
                                }}
                            }},
                        }},
                    )
                if url.endswith("/mcp/guidepoint"):
                    if os.environ.get("FAKE_REFRESH_TOKENS_ON_MCP") == "1" and tokens_file.is_file():
                        import time as _t
                        cur_tok = json.loads(tokens_file.read_text(encoding="utf-8"))
                        cur_tok["access_token"] = "tok-refreshed-controlled"
                        cur_tok["refresh_token"] = "ref-refreshed-controlled"
                        cur_tok["obtained_at"] = int(_t.time())
                        cur_tok["expires_in"] = 600
                        tokens_file.write_text(json.dumps(cur_tok) + "\\n", encoding="utf-8")
                    emit(200, {{"jsonrpc": "2.0", "id": 102, "result": {{"content": [{{"type": "text", "text": json.dumps({{"ok": True, "data": []}})}}]}}}})
                emit(200, {{"ok": True}})

            # :8943 legacy guidepoint proxy (enforces MCP session handshake on /mcp)
            if ":8943" in url:
                waiting_set = set(state.get("keepalive_waiting", []))
                if "com.dalton.guidepoint-mcp-proxy" not in loaded or "com.dalton.guidepoint-mcp-proxy" in waiting_set:
                    conn_refused()
                if url.endswith("/admin/drain"):
                    if cfg_headers.get("authorization") != f"Bearer {{expected_proxy_admin}}":
                        emit(401, {{"ok": False, "error": "Unauthorized"}})
                    state["draining_8943"] = True
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(200, {{"ok": True, "draining": True, "in_flight": int(state.get("in_flight_8943", 0))}})
                if url.endswith("/healthz"):
                    if os.environ.get("FAKE_TIMEOUT_8943_HEALTHZ") == "1":
                        timed_out()
                    owner = "legacy_proxy_8943"
                    if journal_file.is_file():
                        j = json.loads(journal_file.read_text(encoding="utf-8"))
                        owner = str(j.get("target_owner") or "legacy_proxy_8943")
                    draining_8943 = bool(state.get("draining_8943", False))
                    in_flight_8943 = int(state.get("in_flight_8943", 0))
                    if in_flight_8943 > 0:
                        state["in_flight_8943"] = in_flight_8943 - 1
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(
                        200,
                        {{
                            "ok": not draining_8943,
                            "status": "draining" if draining_8943 else ("ok" if owner == "legacy_proxy_8943" else "standby"),
                            "service": "com.dalton.guidepoint-mcp-proxy",
                            "owner_lock_held": owner == "legacy_proxy_8943",
                            "standby": owner != "legacy_proxy_8943",
                            "target_owner": owner,
                            "draining": draining_8943,
                            "in_flight": in_flight_8943,
                        }},
                    )
                if url.endswith("/mcp"):
                    if os.environ.get("FAKE_FAIL_GUIDEPOINT_PROBE") == "1":
                        emit(500, {{"error": "simulated guidepoint proxy failure"}})
                    req_obj = json.loads(data) if data else {{}}
                    rpc_method = str(req_obj.get("method") or "")
                    session_id = "fake-gp-session-8943"
                    if rpc_method == "initialize":
                        init_obj = {{
                            "jsonrpc": "2.0",
                            "id": req_obj.get("id", 1),
                            "result": {{
                                "protocolVersion": "2025-03-26",
                                "capabilities": {{"tools": {{}}}},
                                "serverInfo": {{"name": "guidepoint-mcp", "version": "1.0"}},
                            }},
                        }}
                        hdrs = {{"Mcp-Session-Id": session_id, "Content-Type": "text/event-stream" if os.environ.get("FAKE_PROXY_SSE") == "1" else "application/json"}}
                        if os.environ.get("FAKE_PROXY_SSE") == "1":
                            emit(200, "event: message\\ndata: " + json.dumps(init_obj) + "\\n\\n", hdrs)
                        emit(200, init_obj, hdrs)
                    if rpc_method == "notifications/initialized":
                        if cfg_headers.get("mcp-session-id") != session_id:
                            emit(400, {{"jsonrpc": "2.0", "error": {{"code": -32000, "message": "Missing Mcp-Session-Id"}}}})
                        emit(202, "", {{"Mcp-Session-Id": session_id}})
                    if rpc_method == "tools/call":
                        if cfg_headers.get("mcp-session-id") != session_id:
                            emit(400, {{"jsonrpc": "2.0", "error": {{"code": -32000, "message": "Session required before tools/call"}}}})
                        if os.environ.get("FAKE_REFRESH_TOKENS_ON_MCP") == "1" and tokens_file.is_file():
                            import time as _t
                            cur_tok = json.loads(tokens_file.read_text(encoding="utf-8"))
                            obt = float(cur_tok.get("obtained_at", 0) or 0)
                            exp = float(cur_tok.get("expires_in", 600) or 600)
                            if (obt + exp) - _t.time() <= 60:
                                cur_tok["access_token"] = "tok-refreshed-legacy"
                                cur_tok["refresh_token"] = "ref-refreshed-legacy"
                                cur_tok["obtained_at"] = int(_t.time())
                                cur_tok["expires_in"] = 600
                                tokens_file.write_text(json.dumps(cur_tok) + "\\n", encoding="utf-8")
                        state.setdefault("probes", []).append("proxy_8943_search_library")
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                        # Real Guidepoint MCP tools/call shape (structuredContent.data + text JSON without ok field)
                        payload_obj = {{
                            "jsonrpc": "2.0",
                            "id": req_obj.get("id", 3),
                            "result": {{
                                "structuredContent": {{"data": [{{"transcriptId": "gp-1", "title": "Cloud Call"}}]}},
                                "content": [{{"type": "text", "text": json.dumps({{"data": [{{"transcriptId": "gp-1", "title": "Cloud Call"}}]}})}}],
                            }},
                        }}
                        hdrs = {{"Mcp-Session-Id": session_id, "Content-Type": "text/event-stream" if os.environ.get("FAKE_PROXY_SSE") == "1" else "application/json"}}
                        if os.environ.get("FAKE_PROXY_SSE") == "1":
                            emit(200, "event: message\\ndata: " + json.dumps(payload_obj) + "\\n\\n", hdrs)
                        emit(200, payload_obj, hdrs)
                    emit(400, {{"jsonrpc": "2.0", "error": {{"code": -32601, "message": f"Unsupported method {{rpc_method}}"}}}})
                emit(200, {{"ok": True}})

            # :8960 legacy bridge
            if ":8960" in url:
                waiting_set = set(state.get("keepalive_waiting", []))
                if "com.dalton.researchdata-bridge" not in loaded or "com.dalton.researchdata-bridge" in waiting_set:
                    conn_refused()
                if url.endswith("/admin/drain"):
                    eff_bridge_admin = str(state.get("bridge_admin_token") or expected_bridge_admin)
                    if cfg_headers.get("authorization") == f"Bearer {{expected_bridge_token}}" and eff_bridge_admin != expected_bridge_token:
                        emit(403, {{"ok": False, "error": "Forbidden: MCP token cannot call admin endpoint"}})
                    if cfg_headers.get("authorization") != f"Bearer {{eff_bridge_admin}}":
                        emit(401, {{"ok": False, "error": "Unauthorized"}})
                    if method == "POST":
                        state["draining_8960"] = True
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    inf_8960 = int(state.get("inflight_8960", 0))
                    q_8960 = int(state.get("queued_8960", 0))
                    ing_8960 = int(state.get("ingest_inflight_8960", 0))
                    if method == "GET" and os.environ.get("FAKE_KEEP_8960_BUSY") != "1":
                        changed = False
                        if inf_8960 > 0:
                            state["inflight_8960"] = inf_8960 - 1
                            changed = True
                        if q_8960 > 0:
                            state["queued_8960"] = q_8960 - 1
                            changed = True
                        if ing_8960 > 0:
                            state["ingest_inflight_8960"] = ing_8960 - 1
                            changed = True
                        if changed:
                            state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(
                        200,
                        {{
                            "ok": True,
                            "draining": bool(state.get("draining_8960", False)),
                            "inflight": inf_8960,
                            "queued": q_8960,
                            "ingest_inflight": ing_8960,
                        }},
                    )
                if url.endswith("/healthz"):
                    delay_n = int(os.environ.get("FAKE_DELAY_8960_HEALTHZ_POLLS", "0") or "0")
                    seen_n = int(state.get("seen_8960_healthz", 0))
                    if seen_n < delay_n:
                        state["seen_8960_healthz"] = seen_n + 1
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                        conn_refused()
                    emit(
                        200,
                        {{
                            "ok": not bool(state.get("draining_8960", False)),
                            "draining": bool(state.get("draining_8960", False)),
                            "inflight": int(state.get("inflight_8960", 0)),
                            "queued": int(state.get("queued_8960", 0)),
                            "ingest_inflight": int(state.get("ingest_inflight_8960", 0)),
                        }},
                    )
                if url.endswith("/mcp"):
                    req_obj = json.loads(data) if data else {{}}
                    arguments = ((req_obj.get("params") or {{}}).get("arguments") or {{}})
                    route = arguments.get("route", "")
                    repo_env_txt = repo_env_file.read_text(encoding="utf-8") if repo_env_file.is_file() else ""
                    migrated = "DALTON_RESEARCH_HOST_URL=" in repo_env_txt
                    if migrated:
                        emit(200, {{"jsonrpc": "2.0", "id": 1, "result": {{"content": [{{"type": "text", "text": json.dumps({{"ok": False, "error_code": "route_migrated"}})}}]}}}})
                    if route == "company-wiki":
                        if os.environ.get("FAKE_FAIL_WIKI_PROBE") == "1":
                            emit(200, {{"jsonrpc": "2.0", "id": 1, "result": {{"isError": True, "content": [{{"type": "text", "text": "wiki search failure"}}]}}}})
                        state.setdefault("probes", []).append("bridge_8960_wiki_search")
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                        emit(200, {{"jsonrpc": "2.0", "id": 1, "result": {{"content": [{{"type": "text", "text": json.dumps({{"ok": True, "results": [{{"id": "wiki:1"}}]}})}}]}}}})
                    if route == "guidepoint":
                        if os.environ.get("FAKE_FAIL_GUIDEPOINT_PROBE") == "1":
                            emit(200, {{"jsonrpc": "2.0", "id": 2, "result": {{"isError": True, "content": [{{"type": "text", "text": "guidepoint failure"}}]}}}})
                        state.setdefault("probes", []).append("bridge_8960_guidepoint_search_library")
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                        emit(200, {{"jsonrpc": "2.0", "id": 2, "result": {{"content": [{{"type": "text", "text": json.dumps({{"ok": True, "data": [{{"id": "gp-1"}}]}})}}]}}}})
                emit(200, {{"ok": True}})

            # :8000 backend
            if ":8000" in url:
                if not state.get("backend_running", True):
                    conn_refused()
                if url.endswith("/health"):
                    delay_n = int(os.environ.get("FAKE_DELAY_8000_HEALTH_POLLS", "0") or "0")
                    seen_n = int(state.get("seen_8000_health", 0))
                    if seen_n < delay_n:
                        state["seen_8000_health"] = seen_n + 1
                        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                        conn_refused()
                    emit(200, {{"status": "ok"}})
                if "/maintenance/admission/probe" in url:
                    if os.environ.get("FAKE_REQUIRE_SERVICE_TOKEN") == "1":
                        eff_svc_token = str(state.get("backend_service_token") or expected_svc_token)
                        if cfg_headers.get("x-service-token") != eff_svc_token:
                            emit(401, {{"detail": "Invalid service token"}})
                    adm = json.loads(adm_file.read_text(encoding="utf-8")) if adm_file.is_file() else {{"paused": False, "revision": 0}}
                    if adm.get("paused") is True:
                        emit(503, {{"detail": {{"error_code": "maintenance_in_progress", "revision": adm.get("revision", 0)}}}})
                    if os.environ.get("FAKE_FAIL_OPEN_VERIFY") == "1":
                        emit(503, {{"detail": {{"error_code": "maintenance_in_progress", "revision": adm.get("revision", 0)}}}})
                    emit(200, {{"ok": True, "admitted": True, "revision": adm.get("revision", 0)}})
                emit(200, {{"ok": True}})

            # :8088 edge serving frontend /version.json
            if ":8088" in url and url.endswith("/version.json"):
                if os.environ.get("FAKE_FAIL_EDGE_VERSION") == "1":
                    emit(503, {{"error": "edge unavailable"}})
                override_ver_json = os.environ.get("FAKE_SERVED_FRONTEND_VERSION_JSON", "").strip()
                if override_ver_json:
                    emit(200, json.loads(override_ver_json))
                override_build = os.environ.get("FAKE_SERVED_FRONTEND_BUILD", "").strip()
                if override_build:
                    emit(200, {{"build": override_build}})
                ver_file = web_cur_path / "version.json"
                if ver_file.is_file():
                    emit(200, json.loads(ver_file.read_text(encoding="utf-8")))
                emit(404, {{"error": "missing version.json"}})

            conn_refused()
            """
        ),
    )

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "DALTON_HOME": str(home),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PYTHON_BIN": sys.executable,
        "DALTON_LAUNCH_AGENTS_DIR": str(plist_dir),
        "DALTON_LAUNCHD_DOMAIN": "gui/501",
        "DALTON_MAINTENANCE_NO_DB_LOCK": "1",
        "LAUNCHCTL_BIN": str(fake_launchctl),
        "PS_BIN": str(fake_ps),
        "DOCKER_BIN": str(fake_docker),
        "CURL_BIN": str(fake_curl),
        "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "3",
        "DALTON_ROLLBACK_BRIDGE_DRAIN_TIMEOUT_S": "3",
        "DALTON_ROLLBACK_GATE_VERIFY_TIMEOUT_S": "3",
        "DALTON_ROLLBACK_PROBE_TIMEOUT_S": "3",
        "DALTON_ROLLBACK_PROBE_RETRIES": "1",
        "DALTON_ROLLBACK_PROBE_RETRY_DELAY_S": "0",
        "WAIT_IDLE_POLL_S": "0",
    }

    return {
        "repo": repo,
        "home": home,
        "env": env,
        "state_file": state_file,
        "calls_log": calls_log,
        "curl_argv_log": curl_argv_log,
        "repo_env": repo_env,
        "bridge_env": bridge_env,
        "rh_env": rh_env,
        "gp_env": gp_env,
        "tokens_file": tokens_file,
        "journal_file": journal_file,
        "plist_dir": plist_dir,
        "proxy_plist": proxy_plist,
        "bridge_plist": bridge_plist,
        "rh_plist": rh_plist,
        "web_cur": web_cur,
        "adm_file": adm_file,
        "secrets": [bridge_token, rh_token, rh_admin_token, proxy_admin_token, svc_token],
        "tracked_paths": [
            repo_env,
            bridge_env,
            rh_env,
            gp_env,
            proxy_plist,
            bridge_plist,
            rh_plist,
            tokens_file,
            journal_file,
            web_cur,
        ],
    }


def _simulate_post_cutover_state(h: dict[str, Any]) -> None:
    """Mutate all files, symlinks, and loaded launchd services to simulate a completed cutover."""
    h["repo_env"].write_text(
        h["repo_env"].read_text(encoding="utf-8")
        + "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951\n"
        + "DALTON_EXTRA_PLUGINS_DIR=/app/scripts/dalton/research_runtime/extra_plugins\n",
        encoding="utf-8",
    )
    h["bridge_env"].write_text(
        "DALTON_BRIDGE_TOKEN=test-bridge-secret-token-alpha\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=test-bridge-admin-tok\n"
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n",
        encoding="utf-8",
    )
    now_ts = int(time.time())
    h["tokens_file"].write_text(
        json.dumps(
            {
                "access_token": "tok-after-8951",
                "refresh_token": "ref-after-8951",
                "obtained_at": now_ts - 10,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    h["journal_file"].write_text(
        json.dumps({"operation": "manual_switch", "target_owner": "research_host_8951", "completed": False}) + "\n",
        encoding="utf-8",
    )
    # Remove legacy proxy plist, install research-host plist, mutate bridge plist
    h["proxy_plist"].unlink(missing_ok=True)
    _make_plist(
        h["rh_plist"],
        "com.dalton.research-host",
        {
            "GUIDEPOINT_TOKENS_FILE": str(h["tokens_file"]),
            "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(h["journal_file"]),
        },
    )
    _make_plist(
        h["bridge_plist"],
        "com.dalton.researchdata-bridge",
        {
            "DALTON_REPO_ENV_FILE": str(h["repo_env"]),
            "DALTON_BRIDGE_ENV_FILE": str(h["bridge_env"]),
            "MUTATED_BY_SWITCH": "1",
        },
    )
    # Switch web/releases/current to rel-after
    tmp_link = h["web_cur"].parent / ".tmp-switch"
    os.symlink("rel-after", tmp_link)
    os.replace(tmp_link, h["web_cur"])

    # Update fake launchd state: :8951 & :8960 loaded, :8943 stopped
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["loaded"] = {
        "com.dalton.research-host": str(h["rh_plist"]),
        "com.dalton.researchdata-bridge": str(h["bridge_plist"]),
    }
    st["draining_8951"] = False
    st["draining_8943"] = False
    st["in_flight_8951"] = 0
    st["in_flight_8943"] = 0
    st["probes"] = []
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Snapshot creation, atomicity & SHA-256 verification
# ---------------------------------------------------------------------------


def test_snapshot_atomic_creation_and_tamper_detection(tmp_path: Path) -> None:
    h = _build_isolated_harness(tmp_path)

    res = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    snap_dir = Path(res.stdout.strip())
    assert snap_dir.is_dir()
    assert snap_dir.parent == h["repo"] / "var" / "dalton" / "cutover-snapshots"
    assert stat.S_IMODE(snap_dir.stat().st_mode) == 0o700
    # No leftover hidden temp directories
    leftovers = [p for p in snap_dir.parent.iterdir() if p.name.startswith(".snapshot.tmp.")]
    assert leftovers == []

    # Verify manifest permissions and contents
    manifest_path = snap_dir / "snapshot-manifest.json"
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"]["repo_env"]["existed"] is True
    assert manifest["files"]["plist_proxy"]["existed"] is True
    assert manifest["files"]["plist_bridge"]["existed"] is True
    assert manifest["files"]["plist_research_host"]["existed"] is False
    assert manifest["files"]["guidepoint_tokens"]["existed"] is True
    assert manifest["files"]["handoff_journal"]["existed"] is True
    assert manifest["web_releases_current"]["link_target"] == "rel-before"

    # --verify succeeds
    r_ver = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--verify", str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_ver.returncode == 0, r_ver.stdout + r_ver.stderr

    # Tamper with a saved file -> both --verify and rollback reject it
    saved_env = snap_dir / "files" / "repo.env"
    saved_env.write_text("TAMPERED=1\n", encoding="utf-8")

    r_bad_ver = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--verify", str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_bad_ver.returncode != 0
    assert "sha256 mismatch" in r_bad_ver.stderr or "size mismatch" in r_bad_ver.stderr

    r_bad_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_bad_rb.returncode != 0
    assert "verification failed" in r_bad_rb.stderr


# ---------------------------------------------------------------------------
# 2. Full end-to-end: snapshot -> cutover -> one-click rollback & latest selection
# ---------------------------------------------------------------------------


def test_full_snapshot_cutover_and_one_click_rollback_restores_exact_state(tmp_path: Path) -> None:
    h = _build_isolated_harness(tmp_path)
    baseline_files = _snapshot_tree_hashes(h["tracked_paths"])

    # Create an older precheck snapshot, then the newer cutover snapshot
    r1 = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "precheck", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r1.returncode == 0, r1.stderr

    r2 = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r2.returncode == 0, r2.stderr
    latest_snap = Path(r2.stdout.strip())

    # Simulate cutover mutations
    _simulate_post_cutover_state(h)
    assert _snapshot_tree_hashes(h["tracked_paths"]) != baseline_files

    # Run rollback WITHOUT passing a snapshot dir -> must pick latest snapshot automatically
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    assert "ROLLBACK_OK" in r_rb.stdout
    assert str(latest_snap) in r_rb.stdout

    # Non-token tracked files and symlinks match pre-cutover snapshot byte-for-byte
    non_token_paths = [p for p in h["tracked_paths"] if p != h["tokens_file"]]
    baseline_non_token = {str(p): baseline_files[str(p)] for p in non_token_paths}
    assert _snapshot_tree_hashes(non_token_paths) == baseline_non_token

    # Rotated token written by :8951 after snapshot MUST be preserved (never overwritten by older snapshot)
    live_tokens = json.loads(h["tokens_file"].read_text(encoding="utf-8"))
    assert live_tokens["access_token"] == "tok-after-8951"
    assert live_tokens["refresh_token"] == "ref-after-8951"

    # Service state: :8951 stopped, :8943 & :8960 running, backend recreated, live probes ran
    final_state = json.loads(h["state_file"].read_text(encoding="utf-8"))
    assert "com.dalton.research-host" not in final_state["loaded"]
    assert "com.dalton.guidepoint-mcp-proxy" in final_state["loaded"]
    assert "com.dalton.researchdata-bridge" in final_state["loaded"]
    assert final_state["recreate_count"] == 1
    assert "bridge_8960_wiki_search" in final_state["probes"]
    assert "bridge_8960_guidepoint_search_library" in final_state["probes"]
    assert "proxy_8943_search_library" in final_state["probes"]

    # Maintenance admission gate reopened cleanly after verification
    adm = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm["paused"] is False
    assert adm["pause_host_direct"] is False
    assert adm["allow_workspace_ids"] == []

    # Zero credentials leaked into curl CLI argv
    curl_argv_text = h["curl_argv_log"].read_text(encoding="utf-8")
    for secret in h["secrets"]:
        assert secret not in curl_argv_text


# ---------------------------------------------------------------------------
# 3. Mid-rollback failure keeps admission gate CLOSED with manual instructions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fault_env,expected_step",
    [
        ({"FAKE_KEEP_8951_UP": "1"}, "2/7"),
        ({"FAKE_FAIL_BOOTSTRAP_LABEL": "com.dalton.guidepoint-mcp-proxy"}, "4/7"),
        ({"FAKE_FAIL_COMPOSE": "1"}, "5/7"),
        ({"FAKE_FAIL_WIKI_PROBE": "1"}, "6/7"),
        ({"FAKE_FAIL_GUIDEPOINT_PROBE": "1"}, "6/7"),
    ],
)
def test_rollback_mid_failure_keeps_admission_gate_closed(
    tmp_path: Path,
    fault_env: dict[str, str],
    expected_step: str,
) -> None:
    h = _build_isolated_harness(tmp_path)

    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    _simulate_post_cutover_state(h)
    # Start with admission gate OPEN so we verify rollback closes it first AND keeps it closed on failure
    adm_before = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm_before["paused"] is False

    env_with_fault = {**h["env"], **fault_env}
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=env_with_fault,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode != 0
    assert expected_step in r_rb.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_rb.stderr
    assert "Manual guidance:" in r_rb.stderr

    # Gate MUST remain closed with empty allowlist
    adm_after = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm_after["paused"] is True
    assert adm_after["pause_host_direct"] is True
    assert adm_after["allow_workspace_ids"] == []


# ---------------------------------------------------------------------------
# 4. --dry-run modifies zero files and invokes zero mutating commands
# ---------------------------------------------------------------------------


def test_rollback_dry_run_modifies_nothing(tmp_path: Path) -> None:
    h = _build_isolated_harness(tmp_path)

    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    _simulate_post_cutover_state(h)
    post_cutover_files = _snapshot_tree_hashes(h["tracked_paths"] + [h["adm_file"]])
    state_before = h["state_file"].read_text(encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_dry = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir, "--dry-run"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_dry.returncode == 0, r_dry.stdout + r_dry.stderr
    assert "DRY-RUN" in r_dry.stdout
    assert "zero files modified" in r_dry.stdout

    assert _snapshot_tree_hashes(h["tracked_paths"] + [h["adm_file"]]) == post_cutover_files
    assert h["state_file"].read_text(encoding="utf-8") == state_before
    # No ops lock file created and no launchctl bootout/bootstrap or docker compose up called
    ops_lock = h["repo"] / "var" / "dalton" / "cutover-snapshots" / "research-host" / ".ops.lock"
    assert not ops_lock.exists()
    calls_txt = h["calls_log"].read_text(encoding="utf-8")
    assert "bootout" not in calls_txt
    assert "bootstrap" not in calls_txt
    assert "docker compose" not in calls_txt


# ---------------------------------------------------------------------------
# 5. Idempotency: repeated rollback (and retry after failure) converges identically
# ---------------------------------------------------------------------------


def test_rollback_is_idempotent_across_repeated_and_retry_runs(tmp_path: Path) -> None:
    h = _build_isolated_harness(tmp_path)
    baseline_files = _snapshot_tree_hashes(h["tracked_paths"])

    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    _simulate_post_cutover_state(h)

    # First attempt fails on probe -> gate stays closed
    r_fail = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "FAKE_FAIL_WIKI_PROBE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_fail.returncode != 0
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True

    non_token_paths = [p for p in h["tracked_paths"] if p != h["tokens_file"]]
    baseline_non_token = {str(p): baseline_files[str(p)] for p in non_token_paths}

    # Retry after fault clears -> succeeds, preserves rotated token, and reopens gate
    r_ok1 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok1.returncode == 0, r_ok1.stdout + r_ok1.stderr
    assert _snapshot_tree_hashes(non_token_paths) == baseline_non_token
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-after-8951"
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is False

    # Repeat a second full rollback immediately -> rotated token still preserved & gate open
    r_ok2 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok2.returncode == 0, r_ok2.stdout + r_ok2.stderr
    assert _snapshot_tree_hashes(non_token_paths) == baseline_non_token
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-after-8951"
    final_state = json.loads(h["state_file"].read_text(encoding="utf-8"))
    assert "com.dalton.research-host" not in final_state["loaded"]
    assert "com.dalton.guidepoint-mcp-proxy" in final_state["loaded"]
    assert "com.dalton.researchdata-bridge" in final_state["loaded"]
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is False


# ---------------------------------------------------------------------------
# 6. Parse RESEARCH_HOST_SWITCH_RUNBOOK.md and verify every script & CLI flag
# ---------------------------------------------------------------------------


def _extract_runbook_script_invocations(markdown: str) -> list[tuple[str, list[str]]]:
    """Extract every invocation of a repo or recovery-bin script from fenced bash blocks."""
    invocations: list[tuple[str, list[str]]] = []
    for block_match in re.finditer(r"```bash\n(.*?)```", markdown, re.DOTALL):
        block = block_match.group(1)
        # Join backslash-continued lines
        joined = re.sub(r"\\\n\s*", " ", block)
        for raw_line in joined.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Split compound shell lists (&&, ||, ;) and pipelines (|)
            for part in re.split(r"\s*(?:&&|\|\||;|\|)\s*", line):
                seg = part.strip()
                if not seg:
                    continue
                # Unwrap `VAR="$(cmd ...)"` command substitutions
                sub_m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*="\$\((.+)\)"$', seg)
                if sub_m:
                    seg = sub_m.group(1).strip()
                try:
                    tokens = shlex.split(seg)
                except ValueError:
                    tokens = seg.split()
                for idx, tok in enumerate(tokens):
                    if (
                        tok.startswith("scripts/dalton/")
                        or tok.startswith("var/dalton/cutover-snapshots/recovery-bin/")
                    ) and (tok.endswith(".sh") or tok.endswith(".py")):
                        invocations.append((tok, tokens[idx + 1 :]))
                        break
    return invocations


def _collect_argparse_flags(parser: Any) -> dict[str, set[str]]:
    """Return mapping `{subcommand_or_root: set_of_option_strings}` from an ArgumentParser."""
    result: dict[str, set[str]] = {"": set()}
    for action in parser._actions:
        for opt in action.option_strings:
            result[""].add(opt)
        if getattr(action, "choices", None) and isinstance(action.choices, dict):
            for sub_name, sub_parser in action.choices.items():
                sub_opts: set[str] = set()
                for sub_act in sub_parser._actions:
                    for opt in sub_act.option_strings:
                        sub_opts.add(opt)
                result[sub_name] = sub_opts
    return result


def _collect_shell_script_flags(script_path: Path) -> set[str]:
    """Run `<script> --help` and also scan `case` option arms to collect supported `--flags`."""
    flags: set[str] = set()
    res = subprocess.run(
        ["/bin/bash", str(script_path), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if res.returncode == 0:
        flags.update(re.findall(r"--[a-z][a-z0-9-]*", res.stdout))
    src = script_path.read_text(encoding="utf-8")
    for m in re.finditer(r"^\s*(--[a-z][a-z0-9-]*)", src, re.MULTILINE):
        flags.add(m.group(1))
    return flags


def test_runbook_commands_scripts_and_flags_all_exist_and_follow_writing_rules() -> None:
    assert RUNBOOK_MD.is_file(), f"missing runbook: {RUNBOOK_MD}"
    text = RUNBOOK_MD.read_text(encoding="utf-8")

    # Required top-level sections & key invariants
    assert "切换前一天" in text
    assert "停机切换" in text
    assert "回滚" in text
    assert "503" in text
    assert "web/releases/current" in text
    assert "search_library" in text
    assert "company-wiki" in text
    assert "probes.guidepoint.owner_lock_held" in text
    assert "drain-refresher" in text
    assert "drain-bridge" in text
    assert "verify-frontend-version" in text
    assert "wait-lock-free" in text
    assert "wait-owner" in text
    assert "wait-health" in text
    assert "manifest-verify" in text
    assert "compose-args" in text
    assert "set -euo pipefail" in text
    assert "var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh" in text
    assert "docker compose -f docker-compose.yml -f docker-compose.dalton.yml" not in text

    # Row 6 (R3): recovery-bin publish & manifest-verify MUST occur BEFORE the first gate-set --paused true
    pub_pos = text.index("recovery-publish")
    mver_pos = text.index("manifest-verify")
    first_close_pos = text.index("--paused true")
    assert pub_pos < mver_pos < first_close_pos, "recovery-bin publish & verify must precede closing gate"

    # Row 5 (R4) + R5 Item 1: Step 5 MUST drain & bootout legacy bridge (:8960) BEFORE draining & booting out upstream (:8943)
    step5_pos = text.index("### 第 5 步")
    step6_pos = text.index("### 第 6 步")
    step5_text = text[step5_pos:step6_pos]
    step5_bash_match = re.search(r"```bash\n(.*?)```", step5_text, re.DOTALL)
    assert step5_bash_match is not None, "Step 5 must contain a bash command block"
    step5_bash = step5_bash_match.group(1)
    drain_bridge_pos = step5_bash.index("drain-bridge")
    bridge_bootout_pos = step5_bash.index('launchctl bootout "${DOMAIN}/com.dalton.researchdata-bridge"')
    drain_refresher_pos = step5_bash.index("drain-refresher")
    proxy_bootout_pos = step5_bash.index('launchctl bootout "${DOMAIN}/com.dalton.guidepoint-mcp-proxy"')
    assert drain_bridge_pos < bridge_bootout_pos < drain_refresher_pos < proxy_bootout_pos, (
        "Step 5 must drain and stop legacy bridge (:8960) BEFORE draining/stopping upstream (:8943)"
    )
    assert "--timeout 240" in step5_bash
    assert "入库未结束时先等待再重跑" in step5_text

    # R5 Item 6: Step 3 (wait_backend_idle.sh) MUST note it does not cover bridge ingest, guaranteed by Step 5 drain
    step3_pos = text.index("### 第 3 步")
    step4_pos = text.index("### 第 4 步")
    step3_text = text[step3_pos:step4_pos]
    assert "wait_backend_idle.sh" in step3_text
    assert "不覆盖" in step3_text and "ingest_inflight" in step3_text and ":8960/admin/drain" in step3_text

    # Row 4 (R3): wait-health MUST appear after launchctl bootstrap (:8960) and docker compose up (:8000) before gate-verify
    step8_pos = text.index("docker compose \"${COMPOSE_ARGS[@]}\" up -d --no-deps --force-recreate backend")
    wh_8960_pos = text.index("--url http://127.0.0.1:8960/healthz")
    wh_8000_pos = text.index("--url http://127.0.0.1:8000/health")
    gate_ver_after_compose = text.index("gate-verify", step8_pos)
    assert wh_8960_pos < step8_pos < wh_8000_pos < gate_ver_after_compose

    # Row 7 (R4): verify-frontend-version MUST precede Step 10 gate-set --paused false
    step10_pos = text.index("### 第 10 步")
    vfv_pos = text.index("verify-frontend-version", step10_pos)
    open_gate_pos = text.index("--paused false", step10_pos)
    assert step10_pos < vfv_pos < open_gate_pos, "verify-frontend-version must precede Step 10 gate open"

    # Lead Decision 7: researchdata-connectors.md §6.4b begins with manual cutover pointer to runbook
    design_doc = REPO_ROOT / "docs" / "dalton" / "features" / "researchdata-connectors.md"
    design_text = design_doc.read_text(encoding="utf-8")
    sec_pos = design_text.index("### 6.4b")
    sec_head = design_text[sec_pos : sec_pos + 600]
    assert "scripts/dalton/ops/prod/RESEARCH_HOST_SWITCH_RUNBOOK.md" in sec_head
    assert "停机手动切换" in sec_head

    # Must not contain retired automated orchestration flags or internal codenames
    for forbidden in (
        "--prepare-stage-a",
        "--measure-budgets",
        "--approve-budgets",
        "--recover",
        "b3b",
        "s34-r1",
        "s34-r2",
        "lane56",
        "§6.4b",
        "E16",
    ):
        assert re.search(rf"(?<![A-Za-z0-9_-]){re.escape(forbidden)}(?![A-Za-z0-9_-])", text) is None, (
            f"runbook should not reference retired/internal token: {forbidden}"
        )

    invocations = _extract_runbook_script_invocations(text)
    assert len(invocations) >= 10, f"expected >= 10 script invocations in runbook, got {len(invocations)}"

    # Build authoritative flag sets for Python CLI scripts
    preflight_root_flags = _collect_argparse_flags(preflight._build_preflight_parser())[""]
    preflight_sub_flags = _collect_argparse_flags(preflight._build_subcommand_parser())
    bridge_subproc = _load_module("dalton_bridge_subproc_runbook_check", BRIDGE_SUBPROC_PY)
    bridge_sub_flags = _collect_argparse_flags(bridge_subproc.build_cli_parser())

    referenced_scripts: set[str] = set()
    for rel_script, cli_args in invocations:
        referenced_scripts.add(rel_script)
        effective_rel = rel_script
        if rel_script.startswith("var/dalton/cutover-snapshots/recovery-bin/"):
            fname = rel_script.rsplit("/", 1)[-1]
            assert fname in preflight.RECOVERY_REQUIRED_FILES, (
                f"runbook invokes recovery-bin script not in RECOVERY_REQUIRED_FILES: {fname}"
            )
            effective_rel = f"scripts/dalton/ops/prod/{fname}"
        script_abs = REPO_ROOT / effective_rel
        assert script_abs.is_file(), f"runbook references non-existent script: {rel_script}"

        used_flags = [tok.split("=", 1)[0] for tok in cli_args if tok.startswith("--")]

        if effective_rel == "scripts/dalton/ops/prod/preflight.py":
            if cli_args and not cli_args[0].startswith("-"):
                subcmd = cli_args[0]
                assert subcmd in preflight._SUBCOMMANDS, f"unknown preflight.py subcommand in runbook: {subcmd}"
                allowed = preflight_sub_flags.get(subcmd, set())
                for fl in used_flags:
                    assert fl in allowed, f"preflight.py {subcmd} does not accept flag {fl}"
            else:
                for fl in used_flags:
                    assert fl in preflight_root_flags, f"preflight.py does not accept flag {fl}"
        elif effective_rel == "scripts/dalton/bridge_subprocess_env.py":
            assert cli_args, "bridge_subprocess_env.py requires a subcommand"
            subcmd = cli_args[0]
            assert subcmd in bridge_sub_flags, f"unknown bridge_subprocess_env.py subcommand: {subcmd}"
            for fl in used_flags:
                assert fl in bridge_sub_flags[subcmd], f"bridge_subprocess_env.py {subcmd} does not accept {fl}"
        elif effective_rel.endswith(".sh"):
            allowed = _collect_shell_script_flags(script_abs)
            for fl in used_flags:
                assert fl in allowed, f"{rel_script} does not accept flag {fl} (allowed={sorted(allowed)})"

    # Confirm the runbook actually references all core scripts (and recovery-bin rollback)
    for must_ref in (
        "scripts/dalton/ops/prod/preflight.py",
        "scripts/dalton/ops/prod/snapshot_research_host.sh",
        "var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh",
        "scripts/dalton/ops/prod/wait_backend_idle.sh",
        "scripts/dalton/ops/prod/switch_research_host.sh",
        "scripts/dalton/ops/prod/backup.sh",
        "scripts/dalton/bridge_subprocess_env.py",
    ):
        assert must_ref in referenced_scripts, f"runbook must invoke {must_ref}"


# ---------------------------------------------------------------------------
# 7. create_autospec contract test & Standalone Core isolation check
# ---------------------------------------------------------------------------


def _prod_rh_healthz(
    *,
    owner_lock_held: bool = True,
    standby: bool = False,
    in_flight: int = 0,
    draining: bool = False,
) -> dict[str, Any]:
    """Return the exact production `/healthz` JSON shape emitted by `:8951`."""
    return {
        "ok": not draining,
        "status": "draining" if draining else "ok",
        "service": "dalton-research-host",
        "version": "1.0.0",
        "profile": "everflow",
        "draining": draining,
        "in_flight": in_flight,
        "connectors": {"company-wiki": True, "guidepoint": True},
        "probes": {
            "guidepoint": {
                "owner_lock_held": owner_lock_held,
                "standby": standby,
                "target_owner": "research_host_8951",
                "token_file_exists": True,
            }
        },
    }


def test_probe_services_http_double_uses_create_autospec_and_core_package_zero_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_mock = create_autospec(preflight._http_request, spec_set=True)
    http_mock.side_effect = [
        (200, _prod_rh_healthz(owner_lock_held=True, standby=False), None),
        (
            200,
            {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}},
            None,
        ),
        (
            200,
            {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}},
            None,
        ),
        (
            200,
            {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"ok": False, "error_code": "route_migrated"})}]}},
            None,
        ),
        (200, {"status": "ok"}, None),
    ]
    monkeypatch.setattr(preflight, "_http_request", http_mock)

    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".config" / "dalton").mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / ".env").write_text("APP_ENV=dalton\n", encoding="utf-8")
    (home / ".config" / "dalton" / "research-host.env").write_text(
        "DALTON_RESEARCH_HOST_TOKEN=rh-tok\n", encoding="utf-8"
    )
    (home / ".config" / "dalton" / "researchdata-bridge.env").write_text(
        "DALTON_BRIDGE_TOKEN=br-tok\n", encoding="utf-8"
    )

    rc = preflight.main(
        [
            "probe-services",
            "--repo",
            str(repo),
            "--home",
            str(home),
            "--target",
            "research-host",
            "--retries",
            "1",
            "--retry-delay",
            "0",
        ]
    )
    assert rc == 0
    assert http_mock.call_count == 5

    # Standalone Core release scan: zero proprietary hits in migrated public dirs
    checker = _load_module("dalton_core_release_checker_manual", CHECK_CORE_PY)
    report = checker.check_core_release_package(repo_root=REPO_ROOT)
    assert report.ok is True, report.to_dict()


# ---------------------------------------------------------------------------
# 8. Round-2 review fixes: items 1..11 targeted regression tests
# ---------------------------------------------------------------------------


def test_rollback_token_fallback_on_missing_corrupt_and_mtime_comparison(tmp_path: Path) -> None:
    """Item 1: missing/corrupt live token falls back to snapshot; mtime fallback preserves newer live token; busy lock blocks restore."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    # Case A: live token missing -> restored from snapshot fallback
    _simulate_post_cutover_state(h)
    h["tokens_file"].unlink()
    r_miss = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_miss.returncode == 0, r_miss.stdout + r_miss.stderr
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-before"

    # Case B: live token corrupted JSON -> restored from snapshot fallback
    _simulate_post_cutover_state(h)
    h["tokens_file"].write_text("{corrupt-json", encoding="utf-8")
    r_corrupt = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_corrupt.returncode == 0, r_corrupt.stdout + r_corrupt.stderr
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-before"

    # Case C: obtained_at absent -> compares mtime; newer live mtime is preserved
    h["tokens_file"].write_text(
        json.dumps({"access_token": "tok-no-obt-snap", "refresh_token": "ref-no-obt-snap"}) + "\n",
        encoding="utf-8",
    )
    snap_ts = time.time() - 300
    os.utime(h["tokens_file"], (snap_ts, snap_ts))
    snap_no_obt, _ = preflight.create_cutover_snapshot(
        repo_root=h["repo"],
        home_dir=h["home"],
        label="no-obt",
        plist_dir=h["plist_dir"],
    )
    h["tokens_file"].write_text(
        json.dumps({"access_token": "tok-no-obt-live-newer", "refresh_token": "ref-no-obt-live-newer"}) + "\n",
        encoding="utf-8",
    )
    live_ts = time.time()
    os.utime(h["tokens_file"], (live_ts, live_ts))
    actions = preflight.restore_cutover_snapshot(snap_no_obt)
    assert any("keep-live guidepoint_tokens" in a for a in actions)
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-no-obt-live-newer"

    # Case D: .guidepoint.owner.lock still held -> rollback fails cleanly at wait-owner-lock-free with gate closed
    _simulate_post_cutover_state(h)
    lock_file = h["tokens_file"].parent / ".guidepoint.owner.lock"
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r_lock = subprocess.run(
            ["/bin/bash", str(ROLLBACK_SH), snap_dir],
            env={**h["env"], "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "1"},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert r_lock.returncode != 0
        assert "3/7 wait-owner-lock-free" in r_lock.stderr
        assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_research_host_healthz_requires_probes_guidepoint_owner_lock_held_and_wait_owner_polls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 2: top-level-only or missing probes.guidepoint.owner_lock_held fails; wait-owner polls until True."""
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".config" / "dalton").mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / ".env").write_text("APP_ENV=dalton\n", encoding="utf-8")
    (home / ".config" / "dalton" / "research-host.env").write_text("DALTON_RESEARCH_HOST_TOKEN=rh\n", encoding="utf-8")
    (home / ".config" / "dalton" / "researchdata-bridge.env").write_text("DALTON_BRIDGE_TOKEN=br\n", encoding="utf-8")

    orig_http = preflight._http_request
    # Missing `probes` (even if top-level `guidepoint.owner_lock_held` is True!) MUST FAIL
    for bad_body in (
        {"ok": True, "guidepoint": {"owner_lock_held": True}},
        {"ok": True, "probes": {}},
        _prod_rh_healthz(owner_lock_held=False, standby=True),
    ):
        http_mock = create_autospec(orig_http, spec_set=True)
        http_mock.side_effect = [
            (200, bad_body, None),
            (200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, None),
            (200, {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, None),
            (200, {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps({"ok": False, "error_code": "route_migrated"})}]}}, None),
        ]
        monkeypatch.setattr(preflight, "_http_request", http_mock)
        rc = preflight.main(
            [
                "probe-services",
                "--repo", str(repo),
                "--home", str(home),
                "--target", "research-host",
                "--skip-backend",
                "--retries", "1",
                "--retry-delay", "0",
            ]
        )
        assert rc == 1, f"expected failure for healthz={bad_body!r}"

    # wait-owner polls until probes.guidepoint.owner_lock_held becomes True
    poll_mock = create_autospec(orig_http, spec_set=True)
    poll_mock.side_effect = [
        (None, None, "connection refused"),
        (200, _prod_rh_healthz(owner_lock_held=False, standby=True), None),
        (200, _prod_rh_healthz(owner_lock_held=True, standby=False), None),
    ]
    monkeypatch.setattr(preflight, "_http_request", poll_mock)
    rc_wait = preflight.main(
        [
            "wait-owner",
            "--target", "research-host",
            "--url", "http://127.0.0.1:8951",
            "--timeout", "5",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_wait == 0
    assert poll_mock.call_count == 3


def test_drain_refresher_waits_in_flight_tmp_and_performs_controlled_refresh_before_bootout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: drain-refresher triggers controlled refresh when TTL<=60s, drains, waits in_flight==0 & zero .tmp."""
    h = _build_isolated_harness(tmp_path)
    # Set token near expiry (TTL = 20s <= 60s)
    now_ts = int(time.time())
    h["tokens_file"].write_text(
        json.dumps(
            {
                "access_token": "tok-expiring",
                "refresh_token": "ref-expiring",
                "obtained_at": now_ts - 580,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # Simulate a leftover .tmp file that disappears on second poll
    tmp_tok = h["tokens_file"].parent / ".guidepoint_mcp_tokens.abc123.tmp"
    call_seq: list[tuple[str, str]] = []
    drain_state = {"draining": False}

    def _fake_http(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        call_seq.append((method, url))
        if method == "GET" and url.endswith("/healthz"):
            n_health = sum(1 for m, u in call_seq if m == "GET" and u.endswith("/healthz"))
            if n_health == 1:
                return 200, _prod_rh_healthz(owner_lock_held=True, standby=False, in_flight=1, draining=drain_state["draining"]), None
            if n_health == 2:
                # Still in_flight=1 with a temp file present
                tmp_tok.write_text("partial", encoding="utf-8")
                return 200, _prod_rh_healthz(owner_lock_held=True, standby=False, in_flight=1, draining=drain_state["draining"]), None
            # Third poll: tmp gone, draining=True, and in_flight == 0
            if tmp_tok.exists():
                tmp_tok.unlink()
            return 200, _prod_rh_healthz(owner_lock_held=True, standby=False, in_flight=0, draining=drain_state["draining"]), None
        if method == "POST" and url.endswith("/mcp/guidepoint"):
            # Controlled refresh persists fresh token to disk
            h["tokens_file"].write_text(
                json.dumps(
                    {
                        "access_token": "tok-fresh-controlled",
                        "refresh_token": "ref-fresh-controlled",
                        "obtained_at": int(time.time()),
                        "expires_in": 600,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            return (
                200,
                {"jsonrpc": "2.0", "id": 102, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}},
                None,
            )
        if method == "POST" and url.endswith("/admin/drain"):
            drain_state["draining"] = True
            return 200, {"ok": True, "draining": True, "in_flight": 1}, None
        return 404, {}, "unexpected"

    http_double = create_autospec(preflight._http_request, spec_set=True, side_effect=_fake_http)
    monkeypatch.setattr(preflight, "_http_request", http_double)

    rc = preflight.main(
        [
            "drain-refresher",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--target", "research-host",
            "--url", "http://127.0.0.1:8951",
            "--tokens-file", str(h["tokens_file"]),
            "--research-host-env", str(h["rh_env"]),
            "--timeout", "5",
            "--poll-interval", "0.05",
        ]
    )
    assert rc == 0
    # Verify exact sequence: healthz -> controlled refresh (/mcp/guidepoint) -> /admin/drain -> healthz poll
    urls = [u for _, u in call_seq]
    assert urls[0].endswith("/healthz")
    assert urls[1].endswith("/mcp/guidepoint")
    assert urls[2].endswith("/admin/drain")
    assert urls[-1].endswith("/healthz")
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-fresh-controlled"

    # Also verify rollback_research_host.sh drains :8951 BEFORE launchctl bootout
    monkeypatch.undo()
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "drain-check", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()
    _simulate_post_cutover_state(h)
    # Near-expiry token + 1 in-flight request on :8951 before rollback
    h["tokens_file"].write_text(
        json.dumps(
            {
                "access_token": "tok-expiring-8951",
                "refresh_token": "ref-expiring-8951",
                "obtained_at": int(time.time()) - 580,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["in_flight_8951"] = 1
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "FAKE_REFRESH_TOKENS_ON_MCP": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    calls = h["calls_log"].read_text(encoding="utf-8").splitlines()
    drain_idx = next(i for i, c in enumerate(calls) if "POST http://127.0.0.1:8951/admin/drain" in c)
    mcp_idx = next(i for i, c in enumerate(calls) if "POST http://127.0.0.1:8951/mcp/guidepoint" in c)
    bootout_idx = next(i for i, c in enumerate(calls) if "launchctl bootout gui/501/com.dalton.research-host" in c)
    assert mcp_idx < drain_idx < bootout_idx
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-refreshed-controlled"


def test_stage_a_preflight_respects_research_host_plist_when_absent_from_launch_agents(
    tmp_path: Path,
) -> None:
    """Item 4: `--stage-a` honors `--research-host-plist` when LaunchAgents does not yet contain research-host plist."""
    base = tmp_path.resolve()
    repo = base / "repo"
    home = base / "home"
    (repo / ".venv" / "bin").mkdir(parents=True)
    real_py = REPO_ROOT / ".venv" / "bin" / "python"
    venv_py = repo / ".venv" / "bin" / "python"
    venv_py.symlink_to(real_py if real_py.is_file() else Path(sys.executable))
    (repo / "var" / "dalton" / "cutover-snapshots" / "research-host").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "launchd").mkdir(parents=True)
    for tpl_name in (
        "com.dalton.guidepoint-mcp-proxy.plist.template",
        "com.dalton.researchdata-bridge.plist.template",
        "com.dalton.research-host.plist.template",
    ):
        shutil.copy2(
            REPO_ROOT / "scripts" / "dalton" / "launchd" / tpl_name,
            repo / "scripts" / "dalton" / "launchd" / tpl_name,
        )
    (repo / "scripts" / "dalton" / "research_runtime" / "scripts").mkdir(parents=True)
    (repo / "scripts" / "dalton" / "research_runtime" / "scripts" / "guidepoint_mcp_proxy_run.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (repo / "scripts" / "dalton" / "research_runtime" / "scripts" / "researchdata_mcp.py").write_text("# stub\n", encoding="utf-8")
    os.symlink(REPO_ROOT / "src", repo / "src")

    cfg_dir = home / ".config" / "dalton"
    cfg_dir.mkdir(parents=True)
    bridge_env = cfg_dir / "researchdata-bridge.env"
    bridge_env.write_text("DALTON_BRIDGE_TOKEN=br-tok\n", encoding="utf-8")
    bridge_env.chmod(0o600)
    rh_env = cfg_dir / "research-host.env"
    rh_env.write_text(
        "DALTON_RESEARCH_HOST_TOKEN=rh-tok\n"
        "DALTON_RESEARCH_ADMIN_TOKEN=adm-tok\n"
        "DALTON_BRIDGE_TOKEN=br-tok\n"
        "GUIDEPOINT_CLIENT_ID=cid\n"
        "GUIDEPOINT_CLIENT_SECRET=csec\n",
        encoding="utf-8",
    )
    rh_env.chmod(0o600)
    (repo / ".env").write_text("APP_ENV=dalton\n", encoding="utf-8")

    research_home = home / "Library" / "Application Support" / "Everflow" / "research"
    (research_home / "config").mkdir(parents=True)
    (research_home / "config" / "guidepoint_mcp.env").write_text("GUIDEPOINT_CLIENT_ID=cid\n", encoding="utf-8")
    (research_home / "config" / "guidepoint_mcp_tokens.json").write_text(
        json.dumps({"access_token": "a", "refresh_token": "r", "obtained_at": 1700000000, "expires_in": 600}),
        encoding="utf-8",
    )

    plist_dir = home / "Library" / "LaunchAgents"
    staged_dir = repo / "var" / "dalton" / "cutover-snapshots" / "research-host" / "staged-plists"
    plist_dir.mkdir(parents=True)
    staged_dir.mkdir(parents=True)

    # Render all 3 plists into staged-plists/ using the exact runbook Step 1.2 command,
    # and install ONLY proxy & bridge into LaunchAgents (leaving research-host ONLY in staged-plists/)
    r_render = subprocess.run(
        [
            sys.executable,
            str(BRIDGE_SUBPROC_PY),
            "render-stage-a-plists",
            "--output-dir",
            str(staged_dir),
            "--repo-root",
            str(repo),
            "--home",
            str(home),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_render.returncode == 0, r_render.stdout + r_render.stderr
    shutil.copy2(staged_dir / "com.dalton.guidepoint-mcp-proxy.plist", plist_dir / "com.dalton.guidepoint-mcp-proxy.plist")
    shutil.copy2(staged_dir / "com.dalton.researchdata-bridge.plist", plist_dir / "com.dalton.researchdata-bridge.plist")
    assert not (plist_dir / "com.dalton.research-host.plist").exists()
    staged_rh = staged_dir / "com.dalton.research-host.plist"
    assert staged_rh.is_file()

    cfg_with_flag = preflight.PreflightConfig(
        repo_root=repo,
        home_dir=home,
        plist_dir=plist_dir,
        backend_url="http://127.0.0.1:8000",
        proxy_url="http://127.0.0.1:8943",
        bridge_url="http://127.0.0.1:8960",
        research_host_url="http://127.0.0.1:8951",
        budgets_file=repo / preflight.DEFAULT_BUDGETS_REL_PATH,
        research_host_plist=staged_rh,
    )
    res_ok = preflight._check_stage_a_plists(cfg_with_flag)
    assert res_ok["status"] == "ok", res_ok

    # Without --research-host-plist, checking LaunchAgents fails cleanly because plist is absent there
    cfg_without_flag = preflight.PreflightConfig(
        repo_root=repo,
        home_dir=home,
        plist_dir=plist_dir,
        backend_url="http://127.0.0.1:8000",
        proxy_url="http://127.0.0.1:8943",
        bridge_url="http://127.0.0.1:8960",
        research_host_url="http://127.0.0.1:8951",
        budgets_file=repo / preflight.DEFAULT_BUDGETS_REL_PATH,
        research_host_plist=None,
    )
    res_fail = preflight._check_stage_a_plists(cfg_without_flag)
    assert res_fail["status"] == "fail"
    assert "com.dalton.research-host.plist" in res_fail["detail"]


def test_snapshot_resolves_guidepoint_env_token_override_and_requires_mandatory_files(
    tmp_path: Path,
) -> None:
    """Item 5: snapshot honors GUIDEPOINT_TOKENS_FILE override inside guidepoint_mcp.env and rejects missing mandatory files."""
    h = _build_isolated_harness(tmp_path)
    custom_tokens = tmp_path / "custom-vault" / "my_guidepoint_tokens.json"
    custom_tokens.parent.mkdir(parents=True)
    custom_tokens.write_text(
        json.dumps(
            {
                "access_token": "tok-custom-env-override",
                "refresh_token": "ref-custom-env-override",
                "obtained_at": int(time.time()) - 50,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    custom_tokens.chmod(0o600)

    # Remove GUIDEPOINT_TOKENS_FILE from proxy plist and place override in guidepoint_mcp.env
    _make_plist(
        h["proxy_plist"],
        "com.dalton.guidepoint-mcp-proxy",
        {
            "GUIDEPOINT_ENV_FILE": str(h["gp_env"]),
            "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(h["journal_file"]),
            "DALTON_RESEARCH_HOME": str(h["tokens_file"].parent.parent),
        },
    )
    h["gp_env"].write_text(
        f"GUIDEPOINT_CLIENT_ID=fake-cid\nGUIDEPOINT_CLIENT_SECRET=fake-csec\nGUIDEPOINT_TOKENS_FILE={custom_tokens}\n",
        encoding="utf-8",
    )

    snap_dir, manifest = preflight.create_cutover_snapshot(
        repo_root=h["repo"],
        home_dir=h["home"],
        label="env-override",
        plist_dir=h["plist_dir"],
    )
    assert Path(manifest["files"]["guidepoint_tokens"]["source_path"]).resolve() == custom_tokens.resolve()
    saved_tok = json.loads((snap_dir / "files" / "guidepoint_mcp_tokens.json").read_text(encoding="utf-8"))
    assert saved_tok["refresh_token"] == "ref-custom-env-override"

    # Missing ANY of the 4 mandatory files MUST fail snapshot creation
    for missing_target in (custom_tokens, h["repo_env"], h["proxy_plist"], h["bridge_plist"]):
        saved_bytes = missing_target.read_bytes()
        missing_target.unlink()
        try:
            with pytest.raises((FileNotFoundError, RuntimeError)):
                preflight.create_cutover_snapshot(
                    repo_root=h["repo"],
                    home_dir=h["home"],
                    label="must-fail",
                    plist_dir=h["plist_dir"],
                )
        finally:
            missing_target.write_bytes(saved_bytes)


def test_rollback_closes_gate_first_on_bad_snapshot_or_compose_and_errors_on_reclose_failure(
    tmp_path: Path,
) -> None:
    """Item 6: rollback closes gate BEFORE snapshot verify / compose resolution; re-close failure errors without claiming closed."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())

    # 1. Corrupt snapshot with gate OPEN -> rollback closes gate FIRST, then fails verify-snapshot with gate CLOSED
    bad_snap = h["repo"] / "var" / "dalton" / "cutover-snapshots" / "20990101-000000Z-corrupt"
    shutil.copytree(snap_dir, bad_snap)
    (bad_snap / "files" / "repo.env").write_text("CORRUPT=1\n", encoding="utf-8")
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is False

    r_bad = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(bad_snap)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_bad.returncode != 0
    assert "1/7 verify-snapshot" in r_bad.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_bad.stderr
    adm = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm["paused"] is True and adm["pause_host_direct"] is True

    # 2. Broken COMPOSE_FILE in post-cutover .env -> gate closes first AND rollback restores good .env from snapshot and succeeds!
    shutil.rmtree(bad_snap)
    _simulate_post_cutover_state(h)
    h["adm_file"].write_text(
        json.dumps({**adm, "paused": False, "pause_host_direct": False, "allow_workspace_ids": []}) + "\n",
        encoding="utf-8",
    )
    h["repo_env"].write_text(
        h["repo_env"].read_text(encoding="utf-8") + "COMPOSE_FILE=nonexistent-broken.yml\n",
        encoding="utf-8",
    )
    r_fix_env = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_fix_env.returncode == 0, r_fix_env.stdout + r_fix_env.stderr
    assert "ROLLBACK_OK" in r_fix_env.stdout

    # 3. Step 7 open-gate verification fails AND re-closing gate fails -> prints CRITICAL, NOT "MAINTENANCE ADMISSION GATE REMAINS CLOSED"
    _simulate_post_cutover_state(h)
    # Remove DALTON_MAINTENANCE_NO_DB_LOCK after open? Instead simulate via wrapper python that fails only on rollback_open_verify_failed!
    fail_reclose_py = _write_exec(
        tmp_path / "bin_fail_reclose_py",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, sys
            if "rollback_open_verify_failed" in sys.argv:
                print("ERROR: simulated DB disconnect during re-close", file=sys.stderr)
                sys.exit(1)
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )
    r_reclose_fail = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "DALTON_PYTHON_BIN": str(fail_reclose_py),
            "FAKE_FAIL_OPEN_VERIFY": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_reclose_fail.returncode != 0
    assert "7/7 reclose-gate" in r_reclose_fail.stderr
    assert "CRITICAL" in r_reclose_fail.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" not in r_reclose_fail.stderr
    assert "已闭闸" not in r_reclose_fail.stderr


def test_probe_services_legacy_proxy_decodes_sse_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 7 + R3 Row 3: `:8943/mcp` performs MCP handshake (Mcp-Session-Id) and decodes real JSON/SSE shapes; double rejects sessionless tools/call."""
    init_sse = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"guidepoint-mcp","version":"1.0"}}}\n\n'
    )
    # Real Guidepoint upstream search_library shape: structuredContent.data + text JSON (no "ok" key)
    sse_ok = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"data":[{"transcriptId":"gp-sse-1"}]},"content":[{"type":"text","text":"{\\"data\\":[{\\"transcriptId\\":\\"gp-sse-1\\"}]}"}]}}\n\n'
    )
    sse_err = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":3,"result":{"isError":true,"content":[{"type":"text","text":"upstream error"}]}}\n\n'
    )
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    (home / ".config" / "dalton").mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / ".env").write_text("APP_ENV=dalton\n", encoding="utf-8")
    (home / ".config" / "dalton" / "researchdata-bridge.env").write_text("DALTON_BRIDGE_TOKEN=br-tok\n", encoding="utf-8")

    orig_http_hdr = preflight._http_request_with_headers
    seen_mcp_methods: list[tuple[str, str | None]] = []

    def _make_legacy_double(*, call_body: Any) -> Any:
        def _fake(
            method: str,
            url: str,
            *,
            headers: Any = None,
            body: bytes | None = None,
            timeout_s: float = 5.0,
            capture_headers: bool = True,
        ) -> tuple[int | None, Any, dict[str, str], str | None]:
            hdrs = {k.lower(): v for k, v in (headers or {}).items()}
            if url.endswith(":8943/healthz"):
                return 200, {"ok": True, "owner_lock_held": True, "draining": False, "in_flight": 0}, {}, None
            if url.endswith(":8960/healthz"):
                return 200, {"ok": True}, {}, None
            if url.endswith(":8960/mcp"):
                req_obj = json.loads(body.decode("utf-8")) if body else {}
                rid = req_obj.get("id", 1)
                return 200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, {}, None
            if url.endswith(":8943/mcp"):
                req_obj = json.loads(body.decode("utf-8")) if body else {}
                rpc_method = str(req_obj.get("method") or "")
                sid = hdrs.get("mcp-session-id")
                seen_mcp_methods.append((rpc_method, sid))
                assert "text/event-stream" in hdrs.get("accept", "")
                if rpc_method == "initialize":
                    return 200, init_sse, {"mcp-session-id": "sess-gp-real-1", "content-type": "text/event-stream"}, None
                if rpc_method == "notifications/initialized":
                    if sid != "sess-gp-real-1":
                        return 400, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "missing session"}}, {}, None
                    return 202, "", {"mcp-session-id": "sess-gp-real-1"}, None
                if rpc_method == "tools/call":
                    # Double explicitly REJECTS sessionless tools/call
                    if sid != "sess-gp-real-1":
                        return 400, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "session required"}}, {}, None
                    return 200, call_body, {"mcp-session-id": "sess-gp-real-1", "content-type": "text/event-stream"}, None
                return 400, {"error": "unknown method"}, {}, None
            return 404, {}, {}, "unexpected url"

        return create_autospec(orig_http_hdr, spec_set=True, side_effect=_fake)

    monkeypatch.setattr(preflight, "_http_request_with_headers", _make_legacy_double(call_body=sse_ok))
    rc_ok = preflight.main(
        [
            "probe-services",
            "--repo", str(repo),
            "--home", str(home),
            "--target", "legacy",
            "--skip-backend",
            "--retries", "1",
            "--retry-delay", "0",
        ]
    )
    assert rc_ok == 0
    assert seen_mcp_methods == [
        ("initialize", None),
        ("notifications/initialized", "sess-gp-real-1"),
        ("tools/call", "sess-gp-real-1"),
    ]

    # Also verify SSE isError fails cleanly
    monkeypatch.setattr(preflight, "_http_request_with_headers", _make_legacy_double(call_body=sse_err))
    rc_bad = preflight.main(
        [
            "probe-services",
            "--repo", str(repo),
            "--home", str(home),
            "--target", "legacy",
            "--skip-backend",
            "--retries", "1",
            "--retry-delay", "0",
        ]
    )
    assert rc_bad == 1


def test_recovery_bin_includes_rollback_and_snapshot_excludes_retired_and_executes(
    tmp_path: Path,
) -> None:
    """Item 8: recovery-bin publishes rollback & snapshot scripts in SHA-256 manifest, omits retired switch script, and executes rollback."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    rec_dir = preflight.publish_recovery_bin_atomic(
        repo_root=h["repo"],
        src_dir=PROD_OPS,
        gp_lock_src=REPO_ROOT / "scripts" / "dalton" / "research_runtime" / "guidepoint_lock.py",
        source_commit="test-sha-r2",
    )
    manifest = json.loads((rec_dir / "recovery-manifest.json").read_text(encoding="utf-8"))
    assert "rollback_research_host.sh" in manifest["files"]
    assert "snapshot_research_host.sh" in manifest["files"]
    assert "switch_research_host.sh" not in manifest["files"]
    assert not (rec_dir / "switch_research_host.sh").exists()
    assert preflight._verify_recovery_manifest_dir(rec_dir) == []

    # Execute rollback directly from recovery-bin/ (with SSE from :8943 too)
    _simulate_post_cutover_state(h)
    r_rb = subprocess.run(
        ["/bin/bash", str(rec_dir / "rollback_research_host.sh"), snap_dir],
        env={**h["env"], "FAKE_PROXY_SSE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    assert "ROLLBACK_OK" in r_rb.stdout


def test_snapshot_pins_frontend_release_against_prune_and_rollback_fails_if_target_missing(
    tmp_path: Path,
) -> None:
    """Item 9: snapshot pins pre-cutover frontend release so deploy prune keeps it; missing release fails rollback with gate closed."""
    h = _build_isolated_harness(tmp_path)
    rel_dir = h["repo"] / "web" / "releases"
    # Create snapshot while current -> rel-before
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cutover", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    pin_file = rel_dir / ".pinned-releases" / "rel-before"
    assert pin_file.is_file()

    # Simulate 4 newer frontend deployments and run deploy_main.sh's prune_old_frontend_releases(2)
    base_t = time.time()
    os.utime(rel_dir / "rel-before", (base_t - 500, base_t - 500))
    for idx in range(1, 5):
        d = rel_dir / f"rel-new-{idx}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "version.json").write_text(json.dumps({"build": f"new-{idx}"}), encoding="utf-8")
        os.utime(d, (base_t + idx * 10, base_t + idx * 10))
    tmp_lnk = rel_dir / ".cur-tmp"
    os.symlink("rel-new-4", tmp_lnk)
    os.replace(tmp_lnk, h["web_cur"])

    deploy_sh = PROD_OPS / "deploy_main.sh"
    lock_file = tmp_path / "deploy.lock"
    r_prune = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{deploy_sh}"; prune_old_frontend_releases 2',
        ],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(h["home"]),
            "DALTON_REPO_ROOT": str(h["repo"]),
            "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_prune.returncode == 0, r_prune.stdout + r_prune.stderr
    # Pinned pre-cutover release MUST survive pruning, while unpinned older releases are pruned
    assert (rel_dir / "rel-before").is_dir()
    assert not (rel_dir / "rel-new-1").exists()

    # Now delete rel-before manually -> rollback MUST fail closed BEFORE stopping any service (1/7 verify-snapshot, gate CLOSED, no ROLLBACK_OK)
    _simulate_post_cutover_state(h)
    shutil.rmtree(rel_dir / "rel-before")
    h["calls_log"].write_text("", encoding="utf-8")
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode != 0
    assert "ROLLBACK_OK" not in r_rb.stdout
    assert "1/7 verify-snapshot" in r_rb.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_rb.stderr
    assert "launchctl bootout" not in h["calls_log"].read_text(encoding="utf-8")
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True


def test_snapshot_latest_skips_corrupt_newest_and_selects_next_valid(tmp_path: Path) -> None:
    """Item 10: omitting snapshot dir skips invalid/corrupt newest snapshot and picks the next valid one."""
    h = _build_isolated_harness(tmp_path)
    r1 = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "good-older", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r1.returncode == 0, r1.stderr
    good_snap = Path(r1.stdout.strip())

    # Create a newer snapshot and tamper with it
    bad_snap = h["repo"] / "var" / "dalton" / "cutover-snapshots" / "20991231-235959Z-corrupt-newer"
    shutil.copytree(good_snap, bad_snap)
    m_path = bad_snap / "snapshot-manifest.json"
    m_data = json.loads(m_path.read_text(encoding="utf-8"))
    m_data["created_at"] = "2099-12-31T23:59:59+00:00"
    m_path.write_text(json.dumps(m_data, indent=2), encoding="utf-8")
    (bad_snap / "files" / "repo.env").write_text("TAMPERED=1\n", encoding="utf-8")

    # --latest skips corrupt newer snapshot and returns good_snap
    r_lat = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--latest"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_lat.returncode == 0, r_lat.stderr
    assert Path(r_lat.stdout.strip()).resolve() == good_snap.resolve()

    # Rollback without explicit snapshot dir selects good_snap and succeeds
    _simulate_post_cutover_state(h)
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    assert str(good_snap) in r_rb.stdout


def test_compose_args_and_rollback_honor_compose_file_env(tmp_path: Path) -> None:
    """Item 11: preflight.py compose-args and rollback_research_host.sh both honor COMPOSE_FILE."""
    h = _build_isolated_harness(tmp_path)
    extra_compose = h["repo"] / "docker-compose.override.prod.yml"
    extra_compose.write_text("services:\n  backend:\n    image: test-override\n", encoding="utf-8")
    h["repo_env"].write_text(
        h["repo_env"].read_text(encoding="utf-8")
        + "COMPOSE_FILE=docker-compose.yml:docker-compose.dalton.yml:docker-compose.override.prod.yml\n",
        encoding="utf-8",
    )

    r_ca = subprocess.run(
        [sys.executable, "-B", str(PREFLIGHT_PY), "compose-args", "--repo", str(h["repo"])],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_ca.returncode == 0, r_ca.stderr
    assert "docker-compose.override.prod.yml" in r_ca.stdout

    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "compose", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    _simulate_post_cutover_state(h)
    h["calls_log"].write_text("", encoding="utf-8")
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    compose_lines = [l for l in h["calls_log"].read_text(encoding="utf-8").splitlines() if l.startswith("docker compose ")]
    assert compose_lines, "expected docker compose invocation during rollback"
    assert "docker-compose.override.prod.yml" in compose_lines[-1]


# ---------------------------------------------------------------------------
# 9. Round-3 review fixes: Codex rows 1..6 + Lead decisions 1..7 tests
# ---------------------------------------------------------------------------


def test_drain_refresher_allow_down_rejects_healthz_timeout_while_process_alive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Row 1: `--allow-down` skips drain ONLY when launchctl + ps confirm process exited; alive process with /healthz timeout keeps polling and exits non-zero (keeping gate closed in rollback)."""
    h = _build_isolated_harness(tmp_path)

    # A. Direct unit check of `_is_refresher_process_stopped` with create_autospec(_run_cmd)
    orig_run_cmd = preflight._run_cmd
    # A1: launchctl print shows running pid -> NOT stopped
    rc_alive = create_autospec(orig_run_cmd, spec_set=True)
    rc_alive.side_effect = [
        (0, "gui/501/com.dalton.guidepoint-mcp-proxy = {\n\tstate = running\n\tpid = 43210\n}\n", ""),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_alive)
    stopped, detail = preflight._is_refresher_process_stopped("legacy")
    assert stopped is False and "43210" in detail

    # A2: launchctl unloaded, but stray proxy process still in `ps` -> NOT stopped
    rc_stray = create_autospec(orig_run_cmd, spec_set=True)
    rc_stray.side_effect = [
        (113, "", "Could not find service"),
        (0, "PID\tStatus\tLabel\n", ""),
        (0, "98765 /opt/homebrew/bin/python3.12 /opt/scripts/guidepoint_mcp_proxy.py\n", ""),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_stray)
    stopped2, detail2 = preflight._is_refresher_process_stopped("legacy")
    assert stopped2 is False and "98765" in detail2

    # A3: ps fails -> fail-closed (NOT stopped)
    rc_ps_err = create_autospec(orig_run_cmd, spec_set=True)
    rc_ps_err.side_effect = [
        (113, "", ""),
        (0, "PID\tStatus\tLabel\n", ""),
        (1, "", "operation not permitted"),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_ps_err)
    stopped3, _ = preflight._is_refresher_process_stopped("legacy")
    assert stopped3 is False

    # A4: launchctl unloaded AND ps clean -> confirmed stopped
    rc_clean = create_autospec(orig_run_cmd, spec_set=True)
    rc_clean.side_effect = [
        (113, "", ""),
        (0, "PID\tStatus\tLabel\n", ""),
        (0, "100 /sbin/launchd\n", ""),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_clean)
    stopped4, _ = preflight._is_refresher_process_stopped("legacy")
    assert stopped4 is True

    # B. drain-refresher --allow-down with /healthz timeout while process alive -> polls & exits 1
    orig_http = preflight._http_request
    timeout_http = create_autospec(orig_http, spec_set=True, return_value=(None, None, "TimeoutError: timed out"))
    alive_check = create_autospec(
        preflight._is_refresher_process_stopped,
        spec_set=True,
        return_value=(False, "launchctl print reports com.dalton.research-host running (pid=2002)"),
    )
    monkeypatch.setattr(preflight, "_http_request", timeout_http)
    monkeypatch.setattr(preflight, "_is_refresher_process_stopped", alive_check)
    rc_to = preflight.main(
        [
            "drain-refresher",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--target", "research-host",
            "--url", "http://127.0.0.1:8951",
            "--tokens-file", str(h["tokens_file"]),
            "--research-host-env", str(h["rh_env"]),
            "--timeout", "0.5",
            "--poll-interval", "0.1",
            "--allow-down",
        ]
    )
    assert rc_to == 1
    assert timeout_http.call_count >= 2
    assert alive_check.call_count >= 2

    # C. End-to-end rollback_research_host.sh: :8951 alive but /healthz times out -> MUST NOT bootout and MUST keep gate CLOSED
    monkeypatch.undo()
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r3-timeout", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()
    _simulate_post_cutover_state(h)
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={
            **h["env"],
            "FAKE_TIMEOUT_8951_HEALTHZ": "1",
            "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_rb.returncode != 0
    assert "2/7 drain-research-host" in r_rb.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_rb.stderr
    calls_txt = h["calls_log"].read_text(encoding="utf-8")
    assert "launchctl bootout gui/501/com.dalton.research-host" not in calls_txt
    adm = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm["paused"] is True and adm["pause_host_direct"] is True


def test_forward_drain_resolves_guidepoint_env_admin_token_and_requires_draining_true(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Row 2: forward drain reuses resolve_legacy_proxy_effective_paths (including GUIDEPOINT_ENV_FILE with tokens in another dir) and requires draining=true + in_flight==0 + zero .tmp."""
    h = _build_isolated_harness(tmp_path)
    # Move tokens file to a separate directory from guidepoint_mcp.env; put GUIDEPOINT_PROXY_ADMIN_TOKEN ONLY in guidepoint_mcp.env
    vault_tokens = tmp_path / "separate-vault" / "guidepoint_mcp_tokens.json"
    vault_tokens.parent.mkdir(parents=True)
    vault_tokens.write_text(
        json.dumps(
            {
                "access_token": "tok-separate",
                "refresh_token": "ref-separate",
                "obtained_at": int(time.time()) - 10,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    vault_tokens.chmod(0o600)
    custom_gp_env = tmp_path / "custom-gp-cfg" / "guidepoint_mcp.env"
    custom_gp_env.parent.mkdir(parents=True)
    custom_gp_env.write_text(
        f"GUIDEPOINT_CLIENT_ID=fake-cid\n"
        f"GUIDEPOINT_CLIENT_SECRET=fake-csec\n"
        f"GUIDEPOINT_TOKENS_FILE={vault_tokens}\n"
        f"GUIDEPOINT_PROXY_ADMIN_TOKEN=admin-from-guidepoint-env-only\n",
        encoding="utf-8",
    )
    custom_gp_env.chmod(0o600)
    _make_plist(
        h["proxy_plist"],
        "com.dalton.guidepoint-mcp-proxy",
        {
            "GUIDEPOINT_ENV_FILE": str(custom_gp_env),
            "DALTON_GUIDEPOINT_OWNERSHIP_FILE": str(h["journal_file"]),
        },
    )

    # Case 1: resolves admin token via GUIDEPOINT_ENV_FILE, calls POST /admin/drain, confirms draining=True + in_flight=0
    seen_calls: list[tuple[str, str, str | None]] = []
    drain_flag = {"draining": False}

    def _fake_http_ok(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        auth = (headers or {}).get("Authorization")
        seen_calls.append((method, url, auth))
        if method == "GET" and url.endswith("/healthz"):
            return (
                200,
                {
                    "ok": not drain_flag["draining"],
                    "owner_lock_held": True,
                    "draining": drain_flag["draining"],
                    "in_flight": 0,
                },
                None,
            )
        if method == "POST" and url.endswith("/admin/drain"):
            assert auth == "Bearer admin-from-guidepoint-env-only"
            drain_flag["draining"] = True
            return 200, {"ok": True, "draining": True, "in_flight": 0}, None
        return 404, {}, "unexpected"

    orig_http = preflight._http_request
    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_http_ok))
    rc_ok = preflight.main(
        [
            "drain-refresher",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--target", "legacy",
            "--url", "http://127.0.0.1:8943",
            "--timeout", "2",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_ok == 0
    assert any(m == "POST" and u.endswith("/admin/drain") and a == "Bearer admin-from-guidepoint-env-only" for m, u, a in seen_calls)

    # Case 2: /healthz stays draining=False (even though in_flight==0 and zero .tmp) -> MUST FAIL (exit 1)
    def _fake_http_never_draining(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        if method == "GET" and url.endswith("/healthz"):
            return 200, {"ok": True, "owner_lock_held": True, "draining": False, "in_flight": 0}, None
        if method == "POST" and url.endswith("/admin/drain"):
            return 200, {"ok": True, "draining": False, "in_flight": 0}, None
        return 404, {}, "unexpected"

    monkeypatch.setattr(
        preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_http_never_draining)
    )
    rc_not_draining = preflight.main(
        [
            "drain-refresher",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--target", "legacy",
            "--url", "http://127.0.0.1:8943",
            "--timeout", "0.5",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_not_draining == 1

    # Case 3: Missing GUIDEPOINT_PROXY_ADMIN_TOKEN while draining=False -> MUST FAIL immediately (never skip /admin/drain)
    custom_gp_env.write_text(
        f"GUIDEPOINT_CLIENT_ID=fake-cid\nGUIDEPOINT_CLIENT_SECRET=fake-csec\nGUIDEPOINT_TOKENS_FILE={vault_tokens}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_http_never_draining)
    )
    rc_missing_tok = preflight.main(
        [
            "drain-refresher",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--target", "legacy",
            "--url", "http://127.0.0.1:8943",
            "--timeout", "1",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_missing_tok == 1


def test_legacy_proxy_controlled_refresh_uses_mcp_session_handshake_with_real_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Row 3: legacy :8943 controlled refresh uses initialize -> Mcp-Session-Id -> notifications/initialized -> tools/call before /admin/drain."""
    h = _build_isolated_harness(tmp_path)
    h["tokens_file"].write_text(
        json.dumps(
            {
                "access_token": "tok-expiring-legacy",
                "refresh_token": "ref-expiring-legacy",
                "obtained_at": int(time.time()) - 580,
                "expires_in": 600,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events: list[str] = []
    state = {"draining": False}
    orig_http_hdr = preflight._http_request_with_headers

    def _fake_http_hdr(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
        capture_headers: bool = True,
    ) -> tuple[int | None, Any, dict[str, str], str | None]:
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        if method == "GET" and url.endswith("/healthz"):
            events.append(f"healthz:draining={state['draining']}")
            return 200, {"ok": not state["draining"], "owner_lock_held": True, "draining": state["draining"], "in_flight": 0}, {}, None
        if method == "POST" and url.endswith("/mcp"):
            req_obj = json.loads(body.decode("utf-8")) if body else {}
            m = str(req_obj.get("method") or "")
            sid = hdrs.get("mcp-session-id")
            events.append(f"mcp:{m}:sid={sid}")
            if m == "initialize":
                return (
                    200,
                    {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}},
                    {"mcp-session-id": "sess-ctrl-refresh"},
                    None,
                )
            if m == "notifications/initialized":
                assert sid == "sess-ctrl-refresh"
                return 202, "", {"mcp-session-id": "sess-ctrl-refresh"}, None
            if m == "tools/call":
                # Reject sessionless tools/call
                if sid != "sess-ctrl-refresh":
                    return 400, {"jsonrpc": "2.0", "error": {"code": -32000, "message": "missing session"}}, {}, None
                h["tokens_file"].write_text(
                    json.dumps(
                        {
                            "access_token": "tok-refreshed-via-session",
                            "refresh_token": "ref-refreshed-via-session",
                            "obtained_at": int(time.time()),
                            "expires_in": 600,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                sse_real = (
                    "event: message\n"
                    'data: {"jsonrpc":"2.0","id":3,"result":{"structuredContent":{"data":[{"transcriptId":"T-99"}]},"content":[{"type":"text","text":"{\\"data\\":[{\\"transcriptId\\":\\"T-99\\"}]}"}]}}\n\n'
                )
                return 200, sse_real, {"mcp-session-id": "sess-ctrl-refresh", "content-type": "text/event-stream"}, None
        if method == "POST" and url.endswith("/admin/drain"):
            assert hdrs.get("authorization") == "Bearer test-proxy-admin-secret-epsilon"
            state["draining"] = True
            events.append("admin_drain")
            return 200, {"ok": True, "draining": True, "in_flight": 0}, {}, None
        return 404, {}, {}, "unexpected"

    monkeypatch.setattr(
        preflight, "_http_request_with_headers", create_autospec(orig_http_hdr, spec_set=True, side_effect=_fake_http_hdr)
    )
    rc = preflight.main(
        [
            "drain-refresher",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--target", "legacy",
            "--url", "http://127.0.0.1:8943",
            "--timeout", "5",
            "--poll-interval", "0.05",
        ]
    )
    assert rc == 0
    assert events == [
        "healthz:draining=False",
        "mcp:initialize:sid=None",
        "mcp:notifications/initialized:sid=sess-ctrl-refresh",
        "mcp:tools/call:sid=sess-ctrl-refresh",
        "admin_drain",
        "healthz:draining=True",
    ]
    assert json.loads(h["tokens_file"].read_text(encoding="utf-8"))["refresh_token"] == "ref-refreshed-via-session"


def test_wait_health_and_rollback_poll_cold_start_until_ready_before_gate_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 Row 4: wait-health polls through cold startup delays (refused/503/ok=false) until HTTP 200, and rollback waits before gate-verify."""
    orig_http = preflight._http_request
    poll_ok = create_autospec(orig_http, spec_set=True)
    poll_ok.side_effect = [
        (None, None, "connection refused"),
        (503, {"status": "starting"}, None),
        (200, {"ok": False}, None),
        (200, {"ok": True, "status": "ok"}, None),
    ]
    monkeypatch.setattr(preflight, "_http_request", poll_ok)
    rc_ok = preflight.main(
        ["wait-health", "--url", "http://127.0.0.1:8960/healthz", "--timeout", "5", "--poll-interval", "0.05"]
    )
    assert rc_ok == 0
    assert poll_ok.call_count == 4

    poll_timeout = create_autospec(orig_http, spec_set=True, return_value=(None, None, "connection refused"))
    monkeypatch.setattr(preflight, "_http_request", poll_timeout)
    rc_to = preflight.main(
        ["wait-health", "--url", "http://127.0.0.1:8000/health", "--timeout", "0.5", "--poll-interval", "0.05"]
    )
    assert rc_to == 1

    # End-to-end rollback with cold-start delays on BOTH :8960/healthz and :8000/health
    monkeypatch.undo()
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "cold-start", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()
    _simulate_post_cutover_state(h)
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={
            **h["env"],
            "FAKE_DELAY_8960_HEALTHZ_POLLS": "2",
            "FAKE_DELAY_8000_HEALTH_POLLS": "2",
            "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "5",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    assert "ROLLBACK_OK" in r_rb.stdout
    calls = h["calls_log"].read_text(encoding="utf-8").splitlines()
    # :8000/health polled multiple times and succeeded BEFORE post-recreate /maintenance/admission/probe
    compose_idx = next(i for i, c in enumerate(calls) if c.startswith("docker compose "))
    health_after_compose = [i for i, c in enumerate(calls) if i > compose_idx and "GET http://127.0.0.1:8000/health" in c]
    probe_after_compose = [i for i, c in enumerate(calls) if i > compose_idx and "/maintenance/admission/probe" in c]
    assert probe_after_compose
    health_before_first_gate_probe = [i for i in health_after_compose if i < probe_after_compose[0]]
    assert len(health_before_first_gate_probe) >= 3


def test_rollback_open_gate_command_failure_recloses_and_verifies_gate(tmp_path: Path) -> None:
    """R3 Row 5: if `gate-set --paused false` mutates disk then errors (e.g. PG commit failure), rollback re-closes and verifies closed gate (or prints CRITICAL if re-close also fails)."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r3-open-fail", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    # Subcase A: open gate-set writes paused=false to disk then fails (simulating PG commit failure); re-close succeeds!
    _simulate_post_cutover_state(h)
    partial_open_py = _write_exec(
        tmp_path / "py_partial_open_then_fail",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, subprocess, sys
            if "rollback_verified" in sys.argv:
                # Run the real command so maintenance-admission.json IS atomically replaced with paused=false,
                # then exit 1 to simulate PostgreSQL COMMIT failure after file replace!
                subprocess.run([{sys.executable!r}] + sys.argv[1:], check=False)
                print("ERROR: simulated PG commit failure after file replace", file=sys.stderr)
                sys.exit(1)
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )
    r_a = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "DALTON_PYTHON_BIN": str(partial_open_py)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_a.returncode != 0
    assert "7/7 open-gate" in r_a.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_a.stderr
    adm_a = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm_a["paused"] is True and adm_a["pause_host_direct"] is True and adm_a["allow_workspace_ids"] == []

    # Subcase B (R4 Row 3): open gate-set writes paused=false then fails (PG commit failure), AND locked re-close
    # fails because PG is still unreachable -> rollback automatically falls back to emergency `--no-db-lock`,
    # writes closed state, verifies readback, and ends with `paused=true`!
    _simulate_post_cutover_state(h)
    reclose_args_log = tmp_path / "reclose_argv.log"
    pg_down_reclose_py = _write_exec(
        tmp_path / "py_pg_down_reclose_fallback",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, subprocess, sys
            from pathlib import Path
            if "rollback_verified" in sys.argv:
                subprocess.run([{sys.executable!r}] + sys.argv[1:], check=False)
                print("ERROR: simulated PG commit failure after file replace", file=sys.stderr)
                sys.exit(1)
            if "rollback_open_verify_failed" in sys.argv:
                with Path({str(reclose_args_log)!r}).open("a", encoding="utf-8") as fh:
                    fh.write(" ".join(sys.argv[1:]) + "\\n")
                if "--no-db-lock" not in sys.argv:
                    print("ERROR: simulated PG connection refused during locked reclose", file=sys.stderr)
                    sys.exit(1)
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )
    r_b = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "DALTON_PYTHON_BIN": str(pg_down_reclose_py)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_b.returncode != 0
    assert "7/7 open-gate" in r_b.stderr
    assert "--no-db-lock" in r_b.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_b.stderr
    reclose_lines = reclose_args_log.read_text(encoding="utf-8").splitlines()
    assert len(reclose_lines) == 2
    assert "--no-db-lock" not in reclose_lines[0]
    assert "--no-db-lock" in reclose_lines[1]
    adm_b = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm_b["paused"] is True and adm_b["pause_host_direct"] is True and adm_b["allow_workspace_ids"] == []

    # Subcase C: even emergency `--no-db-lock` write fails (e.g. disk I/O error) -> prints CRITICAL, NOT "REMAINS CLOSED"
    _simulate_post_cutover_state(h)
    open_and_total_reclose_fail_py = _write_exec(
        tmp_path / "py_open_and_total_reclose_fail",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, subprocess, sys
            if "rollback_verified" in sys.argv:
                subprocess.run([{sys.executable!r}] + sys.argv[1:], check=False)
                print("ERROR: simulated PG commit failure after file replace", file=sys.stderr)
                sys.exit(1)
            if "rollback_open_verify_failed" in sys.argv:
                print("ERROR: simulated read-only filesystem on reclose", file=sys.stderr)
                sys.exit(1)
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )
    r_c = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "DALTON_PYTHON_BIN": str(open_and_total_reclose_fail_py)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_c.returncode != 0
    assert "7/7 reclose-gate" in r_c.stderr
    assert "CRITICAL" in r_c.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" not in r_c.stderr


# ---------------------------------------------------------------------------
# 10. Round-4 review fixes: Codex rows 1..7 + Lead decisions 1..7 tests
# ---------------------------------------------------------------------------


def test_allow_down_waits_for_launchd_registered_keepalive_service_without_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 Row 1: `--allow-down` skips drain ONLY when service is unloaded from launchd AND absent from ps; KeepAlive waiting-for-restart (no PID yet) keeps waiting and fails closed without bootout."""
    h = _build_isolated_harness(tmp_path)
    orig_run_cmd = preflight._run_cmd

    # A1: launchctl print exits 0 with state=waiting and NO pid (KeepAlive pending restart) -> NOT stopped
    rc_keepalive_print = create_autospec(orig_run_cmd, spec_set=True)
    rc_keepalive_print.side_effect = [
        (0, "gui/501/com.dalton.guidepoint-mcp-proxy = {\n\tstate = waiting\n\tkeepalive = true\n}\n", ""),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_keepalive_print)
    stopped1, detail1 = preflight._is_refresher_process_stopped("legacy")
    assert stopped1 is False and "still registered" in detail1

    # A2: launchctl print returns 113, but launchctl list STILL lists label with `-` (no PID) -> NOT stopped
    rc_keepalive_list = create_autospec(orig_run_cmd, spec_set=True)
    rc_keepalive_list.side_effect = [
        (113, "", "Could not find service"),
        (0, "PID\tStatus\tLabel\n-\t0\tcom.dalton.guidepoint-mcp-proxy\n", ""),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_keepalive_list)
    stopped2, detail2 = preflight._is_refresher_process_stopped("legacy")
    assert stopped2 is False and "still registered" in detail2

    # A3: launchctl print fails unexpectedly (rc=1, not 'not found') -> fail-closed (NOT stopped)
    rc_print_err = create_autospec(orig_run_cmd, spec_set=True)
    rc_print_err.side_effect = [
        (1, "", "permission denied"),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_print_err)
    stopped3, detail3 = preflight._is_refresher_process_stopped("legacy")
    assert stopped3 is False and "unexpectedly" in detail3

    # A4: unloaded in launchctl print (113) + absent in launchctl list + absent in ps -> stopped
    rc_unloaded = create_autospec(orig_run_cmd, spec_set=True)
    rc_unloaded.side_effect = [
        (113, "", "Could not find service"),
        (0, "PID\tStatus\tLabel\n", ""),
        (0, "100 /sbin/launchd\n", ""),
    ]
    monkeypatch.setattr(preflight, "_run_cmd", rc_unloaded)
    stopped4, _ = preflight._is_refresher_process_stopped("legacy")
    assert stopped4 is True

    # B. End-to-end rollback: legacy proxy :8943 is registered in launchd with KeepAlive waiting to restart
    # (no PID in launchctl print/list, no process in ps, :8943/healthz refused). Rollback MUST wait until
    # timeout, fail at 3/7 drain-legacy-proxy, MUST NOT call bootout on :8943, and MUST keep gate CLOSED!
    monkeypatch.undo()
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r4-keepalive", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()
    _simulate_post_cutover_state(h)
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["loaded"]["com.dalton.guidepoint-mcp-proxy"] = str(h["proxy_plist"])
    st["keepalive_waiting"] = ["com.dalton.guidepoint-mcp-proxy"]
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "1"},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_rb.returncode != 0
    assert "3/7 drain-legacy-proxy" in r_rb.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_rb.stderr
    calls_txt = h["calls_log"].read_text(encoding="utf-8")
    assert "launchctl bootout gui/501/com.dalton.guidepoint-mcp-proxy" not in calls_txt
    adm = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm["paused"] is True and adm["pause_host_direct"] is True


def test_rollback_closes_host_gate_verifies_snapshot_then_verifies_old_backend_with_snapshot_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 Row 2: rollback closes host gate FIRST, verifies snapshot, then verifies still-running old backend using SNAPSHOT .env (immune to corrupted live .env INTERNAL_SERVICE_TOKEN)."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r4-snap-env", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())

    # Unit check: cmd_gate_verify --repo-env prefers explicit --repo-env over os.environ INTERNAL_SERVICE_TOKEN
    import src.dalton.maintenance_gate as mg

    captured_tokens: list[str | None] = []
    orig_verify = mg.verify_container_maintenance_gate

    def _fake_verify(**kwargs: Any) -> Any:
        captured_tokens.append(kwargs.get("service_token"))
        return mg.ContainerGateVerificationResult(
            status="verified",
            container_available=True,
            container_revision=1,
            probe_status_code=503,
            reason="paused_verified",
            elapsed_s=0.01,
        )

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "stale-shell-env-token")
    monkeypatch.setattr(mg, "verify_container_maintenance_gate", create_autospec(orig_verify, spec_set=True, side_effect=_fake_verify))
    rc_gv = preflight.main(
        [
            "gate-verify",
            "--repo", str(h["repo"]),
            "--repo-env", str(snap_dir / "files" / "repo.env"),
            "--expected-paused", "true",
        ]
    )
    assert rc_gv == 0
    assert captured_tokens == ["test-internal-svc-secret-delta"]

    # End-to-end rollback: Step 8 corrupts INTERNAL_SERVICE_TOKEN in live repo/.env while old backend still
    # requires the pre-cutover INTERNAL_SERVICE_TOKEN (returning HTTP 401 on any wrong token).
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    h["repo_env"].write_text(
        "APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN=CORRUPTED-BY-STEP-8\n",
        encoding="utf-8",
    )
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_REQUIRE_SERVICE_TOKEN": "1",
            "INTERNAL_SERVICE_TOKEN": "WRONG-SHELL-TOKEN",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    assert "ROLLBACK_OK" in r_rb.stdout
    assert "INTERNAL_SERVICE_TOKEN=test-internal-svc-secret-delta" in h["repo_env"].read_text(encoding="utf-8")


def test_rollback_runs_db_idle_check_independently_of_backend_http_unless_skip_idle_wait(
    tmp_path: Path,
) -> None:
    """R4 Row 4: rollback runs DB idle check even when backend HTTP is down/unreachable; ONLY `--skip-idle-wait` skips it."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r4-db-idle", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    # Backend container is DOWN (HTTP unreachable), but PostgreSQL still has active threads/data tasks
    _simulate_post_cutover_state(h)
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["backend_running"] = False
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    # 1. Default (without --skip-idle-wait): MUST run DB idle check via psql and stop at 1/7 wait-backend-idle with gate CLOSED!
    r_busy = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**h["env"], "FAKE_PG_BUSY": "1", "DALTON_ROLLBACK_IDLE_WAIT_S": "1"},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_busy.returncode != 0
    assert "1/7 wait-backend-idle" in r_busy.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_busy.stderr
    calls_busy = h["calls_log"].read_text(encoding="utf-8")
    assert "psql" in calls_busy
    assert "launchctl bootout" not in calls_busy
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True

    # 2. Explicit --skip-idle-wait: skips DB idle check and completes rollback cleanly
    h["calls_log"].write_text("", encoding="utf-8")
    r_skip = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir, "--skip-idle-wait"],
        env={**h["env"], "FAKE_PG_BUSY": "1", "DALTON_ROLLBACK_IDLE_WAIT_S": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_skip.returncode == 0, r_skip.stdout + r_skip.stderr
    assert "ROLLBACK_OK" in r_skip.stdout
    assert "psql" not in h["calls_log"].read_text(encoding="utf-8")


def test_drain_bridge_requires_inflight_queued_ingest_zero_in_forward_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 Row 5: `drain-bridge` calls POST /admin/drain and polls GET /admin/drain until draining=true and inflight==queued==ingest_inflight==0; rollback drains :8960 BEFORE bootout."""
    h = _build_isolated_harness(tmp_path)
    orig_http = preflight._http_request

    # A. Unit check: POST /admin/drain -> GET /admin/drain polls through non-zero inflight, queued, ingest_inflight until all 0
    bridge_calls: list[tuple[str, str, str | None]] = []
    poll_seq = iter(
        [
            {"ok": True, "draining": True, "inflight": 1, "queued": 1, "ingest_inflight": 1},
            {"ok": True, "draining": True, "inflight": 0, "queued": 1, "ingest_inflight": 1},
            {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 1},
            {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 0},
        ]
    )

    def _fake_bridge_http(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        auth = (headers or {}).get("Authorization")
        bridge_calls.append((method, url, auth))
        assert auth == "Bearer test-bridge-admin-tok"
        if method == "POST" and url.endswith("/admin/drain"):
            return 200, {"ok": True, "draining": True, "inflight": 1, "queued": 1, "ingest_inflight": 1}, None
        if method == "GET" and url.endswith("/admin/drain"):
            return 200, next(poll_seq), None
        return 404, {}, "unexpected"

    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_bridge_http))
    rc_ok = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "5",
            "--poll-interval", "0.02",
        ]
    )
    assert rc_ok == 0
    assert bridge_calls[0][0] == "POST" and bridge_calls[0][1].endswith("/admin/drain")
    assert len([c for c in bridge_calls if c[0] == "GET" and c[1].endswith("/admin/drain")]) == 4

    # B. Non-zero ingest_inflight until timeout -> exits 1
    def _fake_bridge_ingest_stuck(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        return 200, {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 1}, None

    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_bridge_ingest_stuck))
    rc_stuck = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "0.5",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_stuck == 1

    # C. End-to-end rollback: drains :8960 via POST+GET /admin/drain BEFORE bootout; stuck ingest_inflight blocks bootout with gate CLOSED
    monkeypatch.undo()
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r4-bridge-drain", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    # C1: stuck ingest_inflight on :8960 -> aborts at 2/7 drain-legacy-bridge BEFORE stopping any service, gate CLOSED
    _simulate_post_cutover_state(h)
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["ingest_inflight_8960"] = 2
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")
    r_fail = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={
            **h["env"],
            "FAKE_KEEP_8960_BUSY": "1",
            "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "1",
            "DALTON_ROLLBACK_BRIDGE_DRAIN_TIMEOUT_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_fail.returncode != 0
    assert "2/7 drain-legacy-bridge" in r_fail.stderr
    assert "入库未结束时先等待再重跑" in r_fail.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_fail.stderr
    fail_calls = h["calls_log"].read_text(encoding="utf-8")
    assert "launchctl bootout gui/501/com.dalton.researchdata-bridge" not in fail_calls
    assert "launchctl bootout gui/501/com.dalton.research-host" not in fail_calls
    assert "launchctl bootout gui/501/com.dalton.guidepoint-mcp-proxy" not in fail_calls
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True

    # C2: transient inflight/queued/ingest_inflight drains cleanly BEFORE bridge bootout AND BEFORE upstream bootout
    _simulate_post_cutover_state(h)
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["inflight_8960"] = 1
    st["queued_8960"] = 1
    st["ingest_inflight_8960"] = 1
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")
    r_ok = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok.returncode == 0, r_ok.stdout + r_ok.stderr
    calls = h["calls_log"].read_text(encoding="utf-8").splitlines()
    post_drain_idx = next(i for i, c in enumerate(calls) if "POST http://127.0.0.1:8960/admin/drain" in c)
    get_drain_indices = [i for i, c in enumerate(calls) if "GET http://127.0.0.1:8960/admin/drain" in c]
    bridge_bootout_idx = next(i for i, c in enumerate(calls) if "launchctl bootout gui/501/com.dalton.researchdata-bridge" in c)
    rh_bootout_idx = next(i for i, c in enumerate(calls) if "launchctl bootout gui/501/com.dalton.research-host" in c)
    assert post_drain_idx < get_drain_indices[-1] < bridge_bootout_idx < rh_bootout_idx


def test_runbook_step9_keeps_pause_host_direct_true_and_step10_covers_journal_failure_recovery() -> None:
    """R4 Row 6: Step 9 single-workspace verification keeps pause_host_direct=true; Step 10 provides recovery commands & state verification for journal-set failure."""
    text = RUNBOOK_MD.read_text(encoding="utf-8")
    invocations = _extract_runbook_script_invocations(text)

    # Step 9.2 single-workspace allowlist gate-set MUST keep --pause-host-direct true
    allow_ws_calls = [
        cli_args
        for script, cli_args in invocations
        if script.endswith("preflight.py") and cli_args and cli_args[0] == "gate-set" and "--allow-workspace-id" in cli_args
    ]
    assert len(allow_ws_calls) == 1
    ws_args = allow_ws_calls[0]
    phd_idx = ws_args.index("--pause-host-direct")
    assert ws_args[phd_idx + 1] == "true", f"Step 9.2 must keep --pause-host-direct true, got {ws_args}"

    # Step 10 MUST include a journal-set failure recovery branch with gate-status, journal-read, journal-set retry, and readback checks
    step10_start = text.index("### 第 10 步")
    sec3_start = text.index("## 三、回滚")
    step10_text = text[step10_start:sec3_start]
    assert "journal-set" in step10_text
    assert "gate-status" in step10_text
    assert "journal-read" in step10_text
    assert "--key completed" in step10_text
    assert "--key target_owner" in step10_text


def test_snapshot_requires_valid_web_releases_current_and_rollback_verifies_served_frontend_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 Row 7: snapshot creation & verification require valid web/releases/current symlink & version.json; rollback verifies actually served /version.json BEFORE opening gate."""
    h = _build_isolated_harness(tmp_path)
    rel_dir = h["repo"] / "web" / "releases"

    # A. Snapshot creation MUST reject missing, regular-directory, dangling, escaping, or version.json-less current
    # A1: missing current
    h["web_cur"].unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        preflight.create_cutover_snapshot(repo_root=h["repo"], home_dir=h["home"], label="no-cur", plist_dir=h["plist_dir"])

    # A2: current replaced with regular directory (not a symlink)
    h["web_cur"].mkdir()
    (h["web_cur"] / "version.json").write_text(json.dumps({"build": "dir-not-link"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="symlink"):
        preflight.create_cutover_snapshot(repo_root=h["repo"], home_dir=h["home"], label="dir-cur", plist_dir=h["plist_dir"])
    shutil.rmtree(h["web_cur"])

    # A3: dangling symlink
    os.symlink("rel-does-not-exist", h["web_cur"])
    with pytest.raises(FileNotFoundError, match="dangling"):
        preflight.create_cutover_snapshot(repo_root=h["repo"], home_dir=h["home"], label="dangling", plist_dir=h["plist_dir"])
    h["web_cur"].unlink()

    # A4: symlink escaping web/releases/
    outside_dir = tmp_path / "outside-release"
    outside_dir.mkdir()
    (outside_dir / "version.json").write_text(json.dumps({"build": "escape-1"}) + "\n", encoding="utf-8")
    os.symlink(str(outside_dir), h["web_cur"])
    with pytest.raises(ValueError, match="escapes"):
        preflight.create_cutover_snapshot(repo_root=h["repo"], home_dir=h["home"], label="escape", plist_dir=h["plist_dir"])
    h["web_cur"].unlink()

    # A5: valid symlink but missing version.json
    os.symlink("rel-before", h["web_cur"])
    saved_ver = (rel_dir / "rel-before" / "version.json").read_bytes()
    (rel_dir / "rel-before" / "version.json").unlink()
    try:
        with pytest.raises(ValueError, match="version.json"):
            preflight.create_cutover_snapshot(repo_root=h["repo"], home_dir=h["home"], label="no-ver", plist_dir=h["plist_dir"])
    finally:
        (rel_dir / "rel-before" / "version.json").write_bytes(saved_ver)

    # B. Valid snapshot succeeds, and verify_cutover_snapshot rejects manifests lacking valid symlink or version.json
    snap_dir, _manifest = preflight.create_cutover_snapshot(
        repo_root=h["repo"],
        home_dir=h["home"],
        label="valid-web",
        plist_dir=h["plist_dir"],
        repo_env=h["repo_env"],
        bridge_env=h["bridge_env"],
        research_host_env=h["rh_env"],
        guidepoint_env=h["gp_env"],
    )
    assert preflight.verify_cutover_snapshot(snap_dir) == []

    for mutate_name, mutator in (
        ("not_symlink", lambda m: m["web_releases_current"].update({"is_symlink": False, "link_target": ""})),
        ("missing_ver_rel", lambda m: m["web_releases_current"].pop("version_json_rel_path", None)),
        ("missing_web_block", lambda m: m.pop("web_releases_current", None)),
    ):
        bad_dir = tmp_path / f"bad_snap_{mutate_name}"
        shutil.copytree(snap_dir, bad_dir)
        mp = bad_dir / "snapshot-manifest.json"
        m_obj = json.loads(mp.read_text(encoding="utf-8"))
        mutator(m_obj)
        mp.write_text(json.dumps(m_obj, indent=2), encoding="utf-8")
        errs = preflight.verify_cutover_snapshot(bad_dir)
        assert errs, f"verify_cutover_snapshot must reject {mutate_name}"

    # C. Unit check: verify-frontend-version with create_autospec(_http_request) checks served /version.json
    orig_http = preflight._http_request
    monkeypatch.setattr(
        preflight,
        "_http_request",
        create_autospec(orig_http, spec_set=True, return_value=(200, {"build": "before-111"}, None)),
    )
    assert preflight.main(
        [
            "verify-frontend-version",
            "--repo", str(h["repo"]),
            "--snapshot-dir", str(snap_dir),
            "--edge-url", "http://127.0.0.1:8088",
            "--timeout", "1",
            "--poll-interval", "0.05",
        ]
    ) == 0

    monkeypatch.setattr(
        preflight,
        "_http_request",
        create_autospec(orig_http, spec_set=True, return_value=(200, {"build": "wrong-served-999"}, None)),
    )
    assert preflight.main(
        [
            "verify-frontend-version",
            "--repo", str(h["repo"]),
            "--snapshot-dir", str(snap_dir),
            "--edge-url", "http://127.0.0.1:8088",
            "--timeout", "0.5",
            "--poll-interval", "0.05",
        ]
    ) == 1

    # D. End-to-end rollback: served /version.json mismatches restored snapshot -> fails BEFORE opening gate, leaving paused=True!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    r_bad_fe = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_SERVED_FRONTEND_BUILD": "stale-edge-999",
            "DALTON_ROLLBACK_PROBE_TIMEOUT_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_bad_fe.returncode != 0
    assert "6/7 verify-frontend-version" in r_bad_fe.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_bad_fe.stderr
    assert "ROLLBACK_OK" not in r_bad_fe.stdout
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True


# ---------------------------------------------------------------------------
# 11. Round-5 review fixes: Items 1..6 targeted regression tests
# ---------------------------------------------------------------------------


def test_r5_stop_order_drains_bridge_before_upstreams_with_240s_timeout_and_runbook_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R5 Items 1 & 6: drain :8960 (default >=240s, ingest failure note) BEFORE stopping :8951/:8943; runbook notes wait_backend_idle.sh excludes bridge ingest."""
    # 1. Verify preflight.py drain-bridge default --timeout is at least 240s
    parser = preflight._build_subcommand_parser()
    ns = parser.parse_args(["drain-bridge", "--repo", str(tmp_path)])
    assert ns.timeout >= 240.0

    # 2. Verify rollback_research_host.sh default bridge drain timeout is at least 240s in --dry-run
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r5-order", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    env_default_timeout = {k: v for k, v in h["env"].items() if k != "DALTON_ROLLBACK_BRIDGE_DRAIN_TIMEOUT_S"}
    r_dry = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir, "--dry-run"],
        env=env_default_timeout,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_dry.returncode == 0, r_dry.stdout + r_dry.stderr
    assert "timeout 240s" in r_dry.stdout

    # 3. Unit check: drain-bridge timeout stderr instructs waiting when ingest is still running
    orig_http = preflight._http_request
    monkeypatch.setattr(
        preflight,
        "_http_request",
        create_autospec(
            orig_http,
            spec_set=True,
            return_value=(200, {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 1}, None),
        ),
    )
    rc_to = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "0.5",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_to == 1
    assert "入库未结束时先等待再重跑" in capsys.readouterr().err

    # 4. End-to-end rollback with BOTH :8951 and :8943 loaded + queued/ingest work on :8960:
    #    MUST finish :8960 drain & bootout BEFORE draining/booting out :8951 and :8943!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["loaded"]["com.dalton.guidepoint-mcp-proxy"] = str(h["proxy_plist"])
    st["inflight_8960"] = 1
    st["queued_8960"] = 1
    st["ingest_inflight_8960"] = 1
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    calls = h["calls_log"].read_text(encoding="utf-8").splitlines()
    post_8960 = next(i for i, c in enumerate(calls) if "POST http://127.0.0.1:8960/admin/drain" in c)
    last_get_8960 = [i for i, c in enumerate(calls) if "GET http://127.0.0.1:8960/admin/drain" in c][-1]
    bootout_8960 = next(i for i, c in enumerate(calls) if "launchctl bootout gui/501/com.dalton.researchdata-bridge" in c)
    drain_8951 = next(i for i, c in enumerate(calls) if "POST http://127.0.0.1:8951/admin/drain" in c)
    bootout_8951 = next(i for i, c in enumerate(calls) if "launchctl bootout gui/501/com.dalton.research-host" in c)
    drain_8943 = next(i for i, c in enumerate(calls) if "POST http://127.0.0.1:8943/admin/drain" in c)
    bootout_8943 = next(i for i, c in enumerate(calls) if "launchctl bootout gui/501/com.dalton.guidepoint-mcp-proxy" in c)
    assert post_8960 < last_get_8960 < bootout_8960 < drain_8951 < bootout_8951 < drain_8943 < bootout_8943


def test_r5_rollback_backend_token_fallback_on_401_across_recreate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 Item 2: rollback verifies running backend with snapshot token first, falls back to current .env on HTTP 401, and stops closed if both fail."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r5-backend-tok", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())

    # A. Unit test of `preflight.py gate-verify --repo-env <snap> --fallback-repo-env <live>` using create_autospec
    new_svc_tok = "test-internal-svc-rotated-after-recreate"
    h["repo_env"].write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={new_svc_tok}\n",
        encoding="utf-8",
    )
    h["adm_file"].write_text(
        json.dumps({"version": 1, "revision": 2, "paused": True, "pause_host_direct": True, "allow_workspace_ids": []}) + "\n",
        encoding="utf-8",
    )

    seen_tokens: list[str] = []
    orig_http = preflight._http_request
    orig_run_cmd = preflight._run_cmd

    def _fake_docker(cmd: list[str], *, timeout_s: float = 10.0) -> tuple[int, str, str]:
        if "inspect" in cmd:
            return 0, "true\n", ""
        if "cat" in cmd:
            return 0, h["adm_file"].read_text(encoding="utf-8"), ""
        return 0, "", ""

    def _fake_http_fallback_ok(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        tok = str((headers or {}).get("X-Service-Token") or "")
        seen_tokens.append(tok)
        if tok == new_svc_tok:
            return 503, {"detail": {"error_code": "maintenance_in_progress", "revision": 2}}, None
        return 401, {"detail": "Invalid service token"}, None

    monkeypatch.setattr(preflight, "_run_cmd", create_autospec(orig_run_cmd, spec_set=True, side_effect=_fake_docker))
    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_http_fallback_ok))
    rc_ok = preflight.main(
        [
            "gate-verify",
            "--repo", str(h["repo"]),
            "--repo-env", str(snap_dir / "files" / "repo.env"),
            "--fallback-repo-env", str(h["repo_env"]),
            "--expected-paused", "true",
            "--deadline-s", "2",
            "--poll-interval-s", "0.05",
        ]
    )
    assert rc_ok == 0
    assert seen_tokens == ["test-internal-svc-secret-delta", new_svc_tok]

    # B. End-to-end rollback when backend was ALREADY recreated with a DIFFERENT INTERNAL_SERVICE_TOKEN:
    #    Step 1c tries snapshot token -> 401 -> falls back to current .env token -> 503 verified ->
    #    restores snapshot .env -> recreates backend (adopts snapshot token) -> succeeds!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    h["repo_env"].write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={new_svc_tok}\n"
        "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951\n",
        encoding="utf-8",
    )
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["backend_service_token"] = new_svc_tok
    st["backend_adopts_repo_env_token"] = True
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["curl_argv_log"].write_text("", encoding="utf-8")

    r_ok = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={**h["env"], "FAKE_REQUIRE_SERVICE_TOKEN": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok.returncode == 0, r_ok.stdout + r_ok.stderr
    assert "ROLLBACK_OK" in r_ok.stdout
    assert "INTERNAL_SERVICE_TOKEN=test-internal-svc-secret-delta" in h["repo_env"].read_text(encoding="utf-8")
    assert new_svc_tok not in h["curl_argv_log"].read_text(encoding="utf-8")

    # C. End-to-end rollback when running backend expects a third token (BOTH snapshot & current .env return 401):
    #    MUST stop at 1/7 verify-gate with host gate CLOSED and without touching services or .env!
    _simulate_post_cutover_state(h)
    h["repo_env"].write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={new_svc_tok}\n",
        encoding="utf-8",
    )
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["backend_service_token"] = "third-unmatched-token"
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_both_401 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_REQUIRE_SERVICE_TOKEN": "1",
            "DALTON_ROLLBACK_GATE_VERIFY_TIMEOUT_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_both_401.returncode != 0
    assert "1/7 verify-gate" in r_both_401.stderr
    assert "launchctl bootout" not in h["calls_log"].read_text(encoding="utf-8")
    assert new_svc_tok in h["repo_env"].read_text(encoding="utf-8")
    adm = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm["paused"] is True and adm["pause_host_direct"] is True


def test_r5_rollback_initial_close_gate_uses_no_db_lock_fallback_when_db_down(
    tmp_path: Path,
) -> None:
    """R5 Item 3: starting from paused=false with Postgres unreachable, Step 1a falls back to --no-db-lock + readback, then still runs DB idle check by default."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r5-init-close", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()

    _simulate_post_cutover_state(h)
    # Start explicitly from paused=false (e.g. rollback triggered during observation window)
    h["adm_file"].write_text(
        json.dumps(
            {
                "version": 1,
                "revision": 3,
                "paused": False,
                "pause_host_direct": False,
                "allow_workspace_ids": [],
                "reason": "cutover_verified",
                "operation": "manual_switch",
                "rehearsal_keep_paused_until": None,
                "updated_at": "2026-11-27T01:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    gate_set_log = tmp_path / "initial_close_argv.log"
    pg_down_py = _write_exec(
        tmp_path / "py_pg_down_initial_close",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, sys
            from pathlib import Path
            if "gate-set" in sys.argv:
                with Path({str(gate_set_log)!r}).open("a", encoding="utf-8") as fh:
                    fh.write(" ".join(sys.argv[1:]) + "\\n")
                if "--no-db-lock" not in sys.argv:
                    print("ERROR: simulated Postgres connection refused during initial gate close", file=sys.stderr)
                    sys.exit(1)
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )
    env_pg_down = {
        **{k: v for k, v in h["env"].items() if k != "DALTON_MAINTENANCE_NO_DB_LOCK"},
        "DALTON_PYTHON_BIN": str(pg_down_py),
        "FAKE_PG_DOWN": "1",
        "DALTON_ROLLBACK_IDLE_WAIT_S": "1",
    }

    # Case A: default run (no --skip-idle-wait) -> Step 1a falls back to --no-db-lock and closes + reads back gate,
    # then Step 1d STILL runs DB idle check and stops at 1/7 wait-backend-idle with gate CLOSED!
    r_a = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=env_pg_down,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_a.returncode != 0
    assert "1/7 wait-backend-idle" in r_a.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_a.stderr
    assert "--no-db-lock" in r_a.stdout or "--no-db-lock" in r_a.stderr
    log_lines = gate_set_log.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 2
    assert "--no-db-lock" not in log_lines[0]
    assert "--no-db-lock" in log_lines[1]
    adm_after = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm_after["paused"] is True and adm_after["pause_host_direct"] is True and adm_after["allow_workspace_ids"] == []

    # Case B: starting again from paused=false, if even emergency --no-db-lock fails -> fails at 1/7 close-gate with CRITICAL
    h["adm_file"].write_text(
        json.dumps({"version": 1, "revision": 5, "paused": False, "pause_host_direct": False, "allow_workspace_ids": []}) + "\n",
        encoding="utf-8",
    )
    all_close_fail_py = _write_exec(
        tmp_path / "py_all_close_fail",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os, sys
            if "gate-set" in sys.argv:
                print("ERROR: simulated read-only disk on gate-set", file=sys.stderr)
                sys.exit(1)
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )
    r_b = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env={**env_pg_down, "DALTON_PYTHON_BIN": str(all_close_fail_py)},
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_b.returncode != 0
    assert "1/7 close-gate" in r_b.stderr
    assert "CRITICAL" in r_b.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" not in r_b.stderr


def test_r5_drain_bridge_falls_back_to_verified_snapshot_token_before_stopping_refreshers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 Item 4: drain-bridge retries with verified snapshot admin token on HTTP 401; tampered snapshot is rejected; double-401 aborts before stopping any refresher."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r5-bridge-tok", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())

    # Corrupt live bridge admin token (simulating Step 8 mistake while running :8960 still uses original token)
    h["bridge_env"].write_text(
        "DALTON_BRIDGE_TOKEN=test-bridge-secret-token-alpha\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=CORRUPTED-LIVE-ADMIN-TOK\n"
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n",
        encoding="utf-8",
    )

    # A. Unit check (create_autospec): current token -> 401 -> retries verified snapshot token -> succeeds
    seen_auths: list[str] = []
    orig_http = preflight._http_request

    def _fake_bridge_http(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        auth = str((headers or {}).get("Authorization") or "")
        seen_auths.append(f"{method}:{auth}")
        if auth != "Bearer test-bridge-admin-tok":
            return 401, {"ok": False, "error": "Unauthorized"}, None
        return 200, {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 0}, None

    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_bridge_http))
    rc_ok = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--bridge-env", str(h["bridge_env"]),
            "--snapshot-dir", str(snap_dir),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "3",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_ok == 0
    assert seen_auths == [
        "POST:Bearer CORRUPTED-LIVE-ADMIN-TOK",
        "POST:Bearer test-bridge-admin-tok",
        "GET:Bearer test-bridge-admin-tok",
    ]

    # B. Tampered snapshot MUST NOT be trusted for admin token fallback
    tampered_snap = tmp_path / "tampered-snap"
    shutil.copytree(snap_dir, tampered_snap)
    (tampered_snap / "files" / "repo.env").write_text("TAMPERED=1\n", encoding="utf-8")
    seen_auths.clear()
    rc_tampered = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--bridge-env", str(h["bridge_env"]),
            "--snapshot-dir", str(tampered_snap),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "3",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_tampered == 1
    assert seen_auths == ["POST:Bearer CORRUPTED-LIVE-ADMIN-TOK"]

    # C. End-to-end rollback Case 1: live bridge admin token corrupted while running :8960 holds snapshot token -> succeeds!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    h["bridge_env"].write_text(
        "DALTON_BRIDGE_TOKEN=test-bridge-secret-token-alpha\n"
        "DALTON_BRIDGE_ADMIN_TOKEN=CORRUPTED-LIVE-ADMIN-TOK\n"
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n",
        encoding="utf-8",
    )
    r_rb_ok = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb_ok.returncode == 0, r_rb_ok.stdout + r_rb_ok.stderr
    assert "ROLLBACK_OK" in r_rb_ok.stdout
    assert "DALTON_BRIDGE_ADMIN_TOKEN=test-bridge-admin-tok" in h["bridge_env"].read_text(encoding="utf-8")

    # D. End-to-end rollback Case 2: running :8960 rejects BOTH current & snapshot tokens (401) ->
    #    MUST abort at 2/7 drain-legacy-bridge BEFORE stopping ANY refresher (:8951 or :8943) and keep gate CLOSED!
    _simulate_post_cutover_state(h)
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["loaded"]["com.dalton.guidepoint-mcp-proxy"] = str(h["proxy_plist"])
    st["bridge_admin_token"] = "third-unmatched-bridge-admin-tok"
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb_401 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_rb_401.returncode != 0
    assert "2/7 drain-legacy-bridge" in r_rb_401.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_rb_401.stderr
    calls_txt = h["calls_log"].read_text(encoding="utf-8")
    assert "launchctl bootout gui/501/com.dalton.research-host" not in calls_txt
    assert "launchctl bootout gui/501/com.dalton.guidepoint-mcp-proxy" not in calls_txt
    assert "launchctl bootout gui/501/com.dalton.researchdata-bridge" not in calls_txt
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True


def test_r5_frontend_rollback_target_verified_before_stopping_services_and_on_auto_select(
    tmp_path: Path,
) -> None:
    """R5 Item 5: snapshot verification & auto-selection validate frontend target release dir & matching version.json BEFORE stopping any service."""
    h = _build_isolated_harness(tmp_path)
    rel_dir = h["repo"] / "web" / "releases"

    # Create snapshot 1 pointing to rel-before (build=before-111)
    r_s1 = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "older-valid", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_s1.returncode == 0, r_s1.stderr
    snap_older = Path(r_s1.stdout.strip())

    # Switch current -> rel-after (build=after-222) and create snapshot 2 (newer)
    tmp_lnk = rel_dir / ".cur-r5"
    os.symlink("rel-after", tmp_lnk)
    os.replace(tmp_lnk, h["web_cur"])
    time.sleep(0.05)
    r_s2 = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "newer-target-broken", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_s2.returncode == 0, r_s2.stderr
    snap_newer = Path(r_s2.stdout.strip())

    # Ensure snap_newer sorts strictly newer than snap_older
    m2_path = snap_newer / "snapshot-manifest.json"
    m2 = json.loads(m2_path.read_text(encoding="utf-8"))
    m2["created_at"] = "2099-12-31T23:59:59+00:00"
    m2_path.write_text(json.dumps(m2, indent=2) + "\n", encoding="utf-8")

    # Case A: corrupt rel-after/version.json (mismatched build) -> explicit rollback on snap_newer MUST fail at 1/7 verify-snapshot BEFORE stopping any service!
    _simulate_post_cutover_state(h)
    (rel_dir / "rel-after" / "version.json").write_text(json.dumps({"build": "corrupted-999"}) + "\n", encoding="utf-8")
    env_before = h["repo_env"].read_text(encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_explicit = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_newer)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_explicit.returncode != 0
    assert "1/7 verify-snapshot" in r_explicit.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_explicit.stderr
    assert "launchctl bootout" not in h["calls_log"].read_text(encoding="utf-8")
    assert h["repo_env"].read_text(encoding="utf-8") == env_before
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True

    # Case B: auto-select (--latest and rollback without explicit dir) skips snap_newer (mismatched version.json) and selects snap_older!
    r_lat = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--latest"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_lat.returncode == 0, r_lat.stderr
    assert Path(r_lat.stdout.strip()).resolve() == snap_older.resolve()

    r_auto = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_auto.returncode == 0, r_auto.stdout + r_auto.stderr
    assert "ROLLBACK_OK" in r_auto.stdout
    assert str(snap_older) in r_auto.stdout
    assert os.readlink(h["web_cur"]) == "rel-before"

    # Case C: if rel-before is ALSO deleted, auto-select fails at 1/7 resolve-snapshot BEFORE stopping any service!
    _simulate_post_cutover_state(h)
    shutil.rmtree(rel_dir / "rel-before")
    h["calls_log"].write_text("", encoding="utf-8")
    r_none = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_none.returncode != 0
    assert "1/7 resolve-snapshot" in r_none.stderr
    assert "launchctl bootout" not in h["calls_log"].read_text(encoding="utf-8")
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True


# ---------------------------------------------------------------------------
# 12. Round-6 review fixes: Items 1..5 targeted regression tests
# ---------------------------------------------------------------------------


def test_r6_rerun_rollback_preserves_pre_restore_backend_token_until_recreate_verified(
    tmp_path: Path,
) -> None:
    """R6 Item 1: Step 3 saves pre-restore INTERNAL_SERVICE_TOKEN to a 0600 file in snapshot dir so verbatim reruns after Step 4/5 failures pass 1/7 verify-gate until Step 5 post-recreate verification succeeds with snapshot token."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r6-rerun-tok", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())
    pre_restore_file = snap_dir / ".pre-restore-backend-token.env"

    _simulate_post_cutover_state(h)
    new_svc_tok = "test-internal-svc-rotated-r6-rerun"
    h["repo_env"].write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={new_svc_tok}\n"
        "DALTON_RESEARCH_HOST_URL=http://host.docker.internal:8951\n",
        encoding="utf-8",
    )
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["backend_service_token"] = new_svc_tok
    st["backend_adopts_repo_env_token"] = True
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["curl_argv_log"].write_text("", encoding="utf-8")

    # Attempt 1: Step 1c passes via live .env, Step 3 saves pre-restore token (0600) & restores .env,
    # then Step 4 fails before backend is recreated (backend container still uses new_svc_tok).
    r_fail_step4 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_REQUIRE_SERVICE_TOKEN": "1",
            "FAKE_FAIL_BOOTSTRAP_LABEL": "com.dalton.researchdata-bridge",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_fail_step4.returncode != 0
    assert "4/7 start-legacy-bridge" in r_fail_step4.stderr
    assert "INTERNAL_SERVICE_TOKEN=test-internal-svc-secret-delta" in h["repo_env"].read_text(encoding="utf-8")
    assert pre_restore_file.is_file() and not pre_restore_file.is_symlink()
    assert stat.S_IMODE(pre_restore_file.stat().st_mode) == 0o600
    assert pre_restore_file.read_text(encoding="utf-8").strip() == f"INTERNAL_SERVICE_TOKEN={new_svc_tok}"
    st_mid = json.loads(h["state_file"].read_text(encoding="utf-8"))
    assert st_mid["recreate_count"] == 0
    assert st_mid["backend_service_token"] == new_svc_tok

    # Attempt 2: Verbatim rerun now sees restored .env on disk, passes 1/7 via saved pre-restore file,
    # and fails at Step 5 (docker compose up) — MUST keep pre-restore file intact without overwriting it!
    r_fail_step5 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_REQUIRE_SERVICE_TOKEN": "1",
            "FAKE_FAIL_COMPOSE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_fail_step5.returncode != 0
    assert "5/7 recreate-backend" in r_fail_step5.stderr
    assert pre_restore_file.is_file()
    assert stat.S_IMODE(pre_restore_file.stat().st_mode) == 0o600
    assert pre_restore_file.read_text(encoding="utf-8").strip() == f"INTERNAL_SERVICE_TOKEN={new_svc_tok}"

    # Attempt 3: Verbatim rerun succeeds through Step 5 recreate & snapshot-token gate verification,
    # removes the saved pre-restore token file, and finishes ROLLBACK_OK without leaking token in argv.
    r_ok = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={**h["env"], "FAKE_REQUIRE_SERVICE_TOKEN": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok.returncode == 0, r_ok.stdout + r_ok.stderr
    assert "ROLLBACK_OK" in r_ok.stdout
    assert not pre_restore_file.exists()
    st_done = json.loads(h["state_file"].read_text(encoding="utf-8"))
    assert st_done["recreate_count"] == 1
    assert st_done["backend_service_token"] == "test-internal-svc-secret-delta"
    assert new_svc_tok not in h["curl_argv_log"].read_text(encoding="utf-8")


def test_r6_gate_verify_fails_when_both_tokens_401_even_if_container_file_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R6 Item 2: gate-verify MUST fail (even with --allow-unavailable) whenever HTTP 401 was received and verification did not succeed, even if docker exec cat fails."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r6-double-401", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())

    live_tok = "test-internal-svc-r6-other"
    h["repo_env"].write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={live_tok}\n",
        encoding="utf-8",
    )
    h["adm_file"].write_text(
        json.dumps({"version": 1, "revision": 2, "paused": True, "pause_host_direct": True, "allow_workspace_ids": []}) + "\n",
        encoding="utf-8",
    )

    orig_run_cmd = preflight._run_cmd
    orig_http = preflight._http_request
    seen_tokens: list[str] = []

    def _fake_docker_cat_fails(cmd: list[str], *, timeout_s: float = 10.0) -> tuple[int, str, str]:
        if "inspect" in cmd:
            return 0, "true\n", ""
        if "cat" in cmd:
            return 1, "", "cat: cannot read /app/var/dalton/maintenance-admission.json"
        return 0, "", ""

    def _fake_http_both_401(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        seen_tokens.append(str((headers or {}).get("X-Service-Token") or ""))
        return 401, {"detail": "Invalid service token"}, None

    monkeypatch.setattr(preflight, "_run_cmd", create_autospec(orig_run_cmd, spec_set=True, side_effect=_fake_docker_cat_fails))
    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_http_both_401))

    rc_401 = preflight.main(
        [
            "gate-verify",
            "--repo", str(h["repo"]),
            "--repo-env", str(snap_dir / "files" / "repo.env"),
            "--fallback-repo-env", str(h["repo_env"]),
            "--expected-paused", "true",
            "--deadline-s", "0.2",
            "--poll-interval-s", "0.05",
            "--allow-unavailable",
        ]
    )
    out_401 = json.loads(capsys.readouterr().out)
    assert rc_401 == 1
    assert out_401["status"] == "mismatch"
    assert out_401["probe_status_code"] == 401
    assert "test-internal-svc-secret-delta" in seen_tokens
    assert live_tok in seen_tokens

    # Control: when HTTP probe genuinely cannot connect (no 401) and cat fails, --allow-unavailable still allows 0.
    monkeypatch.setattr(
        preflight,
        "_http_request",
        create_autospec(orig_http, spec_set=True, return_value=(None, None, "Connection refused")),
    )
    rc_unavail = preflight.main(
        [
            "gate-verify",
            "--repo", str(h["repo"]),
            "--repo-env", str(snap_dir / "files" / "repo.env"),
            "--fallback-repo-env", str(h["repo_env"]),
            "--expected-paused", "true",
            "--deadline-s", "0.2",
            "--poll-interval-s", "0.05",
            "--allow-unavailable",
        ]
    )
    out_unavail = json.loads(capsys.readouterr().out)
    assert rc_unavail == 0
    assert out_unavail["status"] == "unavailable"

    # End-to-end rollback: both tokens return 401 AND docker exec cat fails -> MUST abort at 1/7 verify-gate!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    h["repo_env"].write_text(
        f"APP_ENV=dalton\nDALTON_ENABLED=true\nINTERNAL_SERVICE_TOKEN={live_tok}\n",
        encoding="utf-8",
    )
    st = json.loads(h["state_file"].read_text(encoding="utf-8"))
    st["backend_service_token"] = "third-unmatched-r6-token"
    h["state_file"].write_text(json.dumps(st, indent=2), encoding="utf-8")
    h["calls_log"].write_text("", encoding="utf-8")

    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_REQUIRE_SERVICE_TOKEN": "1",
            "FAKE_FAIL_DOCKER_EXEC_CAT": "1",
            "DALTON_ROLLBACK_GATE_VERIFY_TIMEOUT_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_rb.returncode != 0
    assert "1/7 verify-gate" in r_rb.stderr
    assert "launchctl bootout" not in h["calls_log"].read_text(encoding="utf-8")
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True


def test_r6_initial_close_gate_times_out_locked_db_and_waits_exit_before_emergency_write(
    tmp_path: Path,
) -> None:
    """R6 Item 3: DB-locked initial gate close sets explicit timeout (SQL lock/statement timeout + process watchdog) and confirms the hung process exited BEFORE running emergency --no-db-lock write & readback."""
    from src.dalton import maintenance_gate as mg

    # A. Unit check: update_maintenance_state_under_db_lock sets local lock_timeout & statement_timeout before pg_advisory_xact_lock
    executed_sql: list[tuple[str, Any]] = []

    class _FakeCursor:
        def __enter__(self) -> "_FakeCursor":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            executed_sql.append((sql, params))

    class _FakeTxn:
        def __enter__(self) -> "_FakeTxn":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _FakeConn:
        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def transaction(self) -> _FakeTxn:
            return _FakeTxn()

        def cursor(self) -> _FakeCursor:
            return _FakeCursor()

    adm_unit = tmp_path / "unit-adm.json"
    st_unit = mg.update_maintenance_state_under_db_lock(
        paused=True,
        pause_host_direct=True,
        reason="r6_lock_timeout_check",
        operation="rollback",
        state_file=adm_unit,
        conn_factory=_FakeConn,
        lock_timeout_s=2.5,
    )
    assert st_unit.paused is True
    assert executed_sql == [
        ("SELECT set_config('lock_timeout', %s, true)", ("2500ms",)),
        ("SELECT set_config('statement_timeout', %s, true)", ("2500ms",)),
        (mg.MAINTENANCE_EXCLUSIVE_LOCK_SQL, None),
    ]

    # B. End-to-end rollback: locked gate-set hangs (even ignoring SIGTERM) holding an OS flock;
    #    rollback times out, kills & confirms exit, and ONLY THEN executes emergency --no-db-lock write + readback!
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r6-lock-to", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = r_snap.stdout.strip()
    _simulate_post_cutover_state(h)
    h["adm_file"].write_text(
        json.dumps({"version": 1, "revision": 3, "paused": False, "pause_host_direct": False, "allow_workspace_ids": []}) + "\n",
        encoding="utf-8",
    )

    hung_pid_file = tmp_path / "hung_locked_gate_set.pid"
    hung_lock_file = tmp_path / "hung_locked_gate_set.lock"
    emergency_check_file = tmp_path / "emergency_saw_hung_exited.txt"

    hung_py = _write_exec(
        tmp_path / "py_hung_locked_gate_set",
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import fcntl, os, signal, sys, time
            from pathlib import Path

            pid_file = Path({str(hung_pid_file)!r})
            lock_file = Path({str(hung_lock_file)!r})
            check_file = Path({str(emergency_check_file)!r})

            if "gate-set" in sys.argv:
                if "--no-db-lock" not in sys.argv:
                    assert "--lock-timeout-s" in sys.argv
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    fh = lock_file.open("w", encoding="utf-8")
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    pid_file.write_text(str(os.getpid()), encoding="utf-8")
                    time.sleep(30.0)
                    sys.exit(0)
                else:
                    # Emergency fallback MUST only run after hung locked process has exited & released its flock
                    hung_pid = int(pid_file.read_text(encoding="utf-8").strip())
                    alive = True
                    try:
                        os.kill(hung_pid, 0)
                    except ProcessLookupError:
                        alive = False
                    lock_free = False
                    with lock_file.open("a+", encoding="utf-8") as lfh:
                        try:
                            fcntl.flock(lfh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            lock_free = True
                        except BlockingIOError:
                            lock_free = False
                    check_file.write_text(f"alive={{alive}} lock_free={{lock_free}}\\n", encoding="utf-8")
            os.execv({sys.executable!r}, [{sys.executable!r}] + sys.argv[1:])
            """
        ),
    )

    env_hung = {
        **{k: v for k, v in h["env"].items() if k != "DALTON_MAINTENANCE_NO_DB_LOCK"},
        "DALTON_PYTHON_BIN": str(hung_py),
        "DALTON_ROLLBACK_GATE_CLOSE_LOCK_TIMEOUT_S": "1",
        "FAKE_PG_DOWN": "1",
        "DALTON_ROLLBACK_IDLE_WAIT_S": "1",
    }
    t0 = time.monotonic()
    r_to = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=env_hung,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 10.0, f"rollback hung too long: {elapsed:.2f}s"
    assert r_to.returncode != 0
    assert "1/7 wait-backend-idle" in r_to.stderr
    assert "timed out" in r_to.stderr
    assert "--no-db-lock" in r_to.stderr or "--no-db-lock" in r_to.stdout
    assert emergency_check_file.read_text(encoding="utf-8").strip() == "alive=False lock_free=True"
    adm_after = json.loads(h["adm_file"].read_text(encoding="utf-8"))
    assert adm_after["paused"] is True and adm_after["pause_host_direct"] is True and adm_after["allow_workspace_ids"] == []


def test_r6_drain_bridge_retries_snapshot_token_on_http_403_for_post_and_get(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 Item 4: drain-bridge retries verified snapshot admin token on HTTP 403 (e.g. MCP token mistakenly placed in DALTON_BRIDGE_ADMIN_TOKEN) for both POST and GET."""
    h = _build_isolated_harness(tmp_path)
    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r6-bridge-403", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())

    # Mistakenly put MCP token (DALTON_BRIDGE_TOKEN) into DALTON_BRIDGE_ADMIN_TOKEN
    mcp_tok = "test-bridge-secret-token-alpha"
    h["bridge_env"].write_text(
        f"DALTON_BRIDGE_TOKEN={mcp_tok}\n"
        f"DALTON_BRIDGE_ADMIN_TOKEN={mcp_tok}\n"
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n",
        encoding="utf-8",
    )

    # A. Unit check (POST -> 403 with MCP token -> retries snapshot admin token -> succeeds)
    orig_http = preflight._http_request
    seen_post_403: list[str] = []

    def _fake_post_403(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        auth = str((headers or {}).get("Authorization") or "")
        seen_post_403.append(f"{method}:{auth}")
        if auth == f"Bearer {mcp_tok}":
            return 403, {"ok": False, "error": "Forbidden"}, None
        if auth == "Bearer test-bridge-admin-tok":
            return 200, {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 0}, None
        return 401, {"ok": False, "error": "Unauthorized"}, None

    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_post_403))
    rc_post = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--bridge-env", str(h["bridge_env"]),
            "--snapshot-dir", str(snap_dir),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "3",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_post == 0
    assert seen_post_403 == [
        f"POST:Bearer {mcp_tok}",
        "POST:Bearer test-bridge-admin-tok",
        "GET:Bearer test-bridge-admin-tok",
    ]

    # B. Unit check (GET -> 403 -> retries snapshot admin token -> succeeds)
    seen_get_403: list[str] = []

    def _fake_get_403(
        method: str,
        url: str,
        *,
        headers: Any = None,
        body: bytes | None = None,
        timeout_s: float = 5.0,
    ) -> tuple[int | None, Any, str | None]:
        auth = str((headers or {}).get("Authorization") or "")
        seen_get_403.append(f"{method}:{auth}")
        if method == "POST":
            return 200, {"ok": True, "draining": True, "inflight": 1, "queued": 0, "ingest_inflight": 0}, None
        if auth == f"Bearer {mcp_tok}":
            return 403, {"ok": False, "error": "Forbidden"}, None
        return 200, {"ok": True, "draining": True, "inflight": 0, "queued": 0, "ingest_inflight": 0}, None

    monkeypatch.setattr(preflight, "_http_request", create_autospec(orig_http, spec_set=True, side_effect=_fake_get_403))
    rc_get = preflight.main(
        [
            "drain-bridge",
            "--repo", str(h["repo"]),
            "--home", str(h["home"]),
            "--plist-dir", str(h["plist_dir"]),
            "--bridge-env", str(h["bridge_env"]),
            "--snapshot-dir", str(snap_dir),
            "--url", "http://127.0.0.1:8960",
            "--timeout", "3",
            "--poll-interval", "0.05",
        ]
    )
    assert rc_get == 0
    assert seen_get_403 == [
        f"POST:Bearer {mcp_tok}",
        f"GET:Bearer {mcp_tok}",
        "GET:Bearer test-bridge-admin-tok",
    ]

    # C. End-to-end rollback with MCP token mistakenly placed in live DALTON_BRIDGE_ADMIN_TOKEN:
    #    fake :8960 returns 403 for MCP token -> rollback retries snapshot token, drains :8960, and succeeds!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    h["bridge_env"].write_text(
        f"DALTON_BRIDGE_TOKEN={mcp_tok}\n"
        f"DALTON_BRIDGE_ADMIN_TOKEN={mcp_tok}\n"
        "DALTON_GUIDEPOINT_MCP_URL=http://127.0.0.1:8951/mcp/guidepoint\n",
        encoding="utf-8",
    )
    r_rb = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_rb.returncode == 0, r_rb.stdout + r_rb.stderr
    assert "ROLLBACK_OK" in r_rb.stdout
    assert "DALTON_BRIDGE_ADMIN_TOKEN=test-bridge-admin-tok" in h["bridge_env"].read_text(encoding="utf-8")


def test_r6_frontend_version_compares_full_version_json_including_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6 Item 5: both pre-stop target verification and pre-open served /version.json verification compare the full version.json object including `entry`."""
    h = _build_isolated_harness(tmp_path)
    rel_before = h["repo"] / "web" / "releases" / "rel-before"
    full_ver = {
        "build": "index-AAA111.js+help-1234567890abcdef@rel-before",
        "release": "index-AAA111.js+help-1234567890abcdef@rel-before",
        "release_id": "rel-before",
        "help_hash": "1234567890abcdef",
        "entry": "index-AAA111.js",
    }
    (rel_before / "version.json").write_text(json.dumps(full_ver) + "\n", encoding="utf-8")

    r_snap = subprocess.run(
        ["/bin/bash", str(SNAPSHOT_SH), "--label", "r6-fe-entry", "--print-path"],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert r_snap.returncode == 0, r_snap.stderr
    snap_dir = Path(r_snap.stdout.strip())
    assert preflight.verify_cutover_snapshot(snap_dir) == []

    # A. Only `entry` modified in target release dir -> verify_cutover_snapshot & rollback Step 1b MUST fail before stopping services!
    _simulate_post_cutover_state(h)
    (rel_before / "version.json").write_text(
        json.dumps({**full_ver, "entry": "index-TAMPERED999.js"}) + "\n",
        encoding="utf-8",
    )
    errs_mod = preflight.verify_cutover_snapshot(snap_dir)
    assert errs_mod and any("entry" in e for e in errs_mod)

    h["calls_log"].write_text("", encoding="utf-8")
    r_pre_stop = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_pre_stop.returncode != 0
    assert "1/7 verify-snapshot" in r_pre_stop.stderr
    assert "launchctl bootout" not in h["calls_log"].read_text(encoding="utf-8")
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True

    # B. Only `entry` missing in target release dir -> verify_cutover_snapshot MUST fail!
    ver_no_entry = {k: v for k, v in full_ver.items() if k != "entry"}
    (rel_before / "version.json").write_text(json.dumps(ver_no_entry) + "\n", encoding="utf-8")
    errs_missing = preflight.verify_cutover_snapshot(snap_dir)
    assert errs_missing and any("entry" in e for e in errs_missing)

    # Restore exact target version.json and web/releases/current -> rel-before for served-response checks
    (rel_before / "version.json").write_text(json.dumps(full_ver) + "\n", encoding="utf-8")
    tmp_lnk = h["web_cur"].parent / ".cur-r6-restore"
    os.symlink("rel-before", tmp_lnk)
    os.replace(tmp_lnk, h["web_cur"])
    assert preflight.verify_cutover_snapshot(snap_dir) == []

    # C. Unit check (create_autospec): verify-frontend-version fails when served /version.json differs ONLY in `entry` (modified or missing), passes on exact match
    orig_http = preflight._http_request
    for bad_served in ({**full_ver, "entry": "index-WRONG.js"}, ver_no_entry):
        monkeypatch.setattr(
            preflight,
            "_http_request",
            create_autospec(orig_http, spec_set=True, return_value=(200, bad_served, None)),
        )
        assert (
            preflight.main(
                [
                    "verify-frontend-version",
                    "--repo", str(h["repo"]),
                    "--snapshot-dir", str(snap_dir),
                    "--edge-url", "http://127.0.0.1:8088",
                    "--timeout", "0.5",
                    "--poll-interval", "0.05",
                ]
            )
            == 1
        )

    monkeypatch.setattr(
        preflight,
        "_http_request",
        create_autospec(orig_http, spec_set=True, return_value=(200, dict(full_ver), None)),
    )
    assert (
        preflight.main(
            [
                "verify-frontend-version",
                "--repo", str(h["repo"]),
                "--snapshot-dir", str(snap_dir),
                "--edge-url", "http://127.0.0.1:8088",
                "--timeout", "1",
                "--poll-interval", "0.05",
            ]
        )
        == 0
    )

    # D. End-to-end rollback: served /version.json matches build/release/help_hash/release_id but differs in `entry` ->
    #    MUST stop at 6/7 verify-frontend-version BEFORE opening gate!
    monkeypatch.undo()
    _simulate_post_cutover_state(h)
    r_bad_entry = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), str(snap_dir)],
        env={
            **h["env"],
            "FAKE_SERVED_FRONTEND_VERSION_JSON": json.dumps({**full_ver, "entry": "index-SERVED-STALE.js"}),
            "DALTON_ROLLBACK_PROBE_TIMEOUT_S": "1",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert r_bad_entry.returncode != 0
    assert "6/7 verify-frontend-version" in r_bad_entry.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_bad_entry.stderr
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is True
