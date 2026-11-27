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
    gp_env.write_text("GUIDEPOINT_CLIENT_ID=fake-cid\nGUIDEPOINT_CLIENT_SECRET=fake-csec\n", encoding="utf-8")
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
            cmd = args[0] if args else ""

            if cmd == "list":
                print("PID\\tStatus\\tLabel")
                for idx, lbl in enumerate(sorted(loaded.keys()), start=1001):
                    print(f"{{idx}}\\t0\\t{{lbl}}")
                sys.exit(0)
            if cmd == "print":
                target = args[1] if len(args) > 1 else ""
                lbl = target.split("/")[-1]
                if lbl in loaded:
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
                    if not state.get("backend_running", True):
                        sys.exit(1)
                    if adm_file.is_file():
                        print(adm_file.read_text(encoding="utf-8"), end="")
                        sys.exit(0)
                    sys.exit(1)
                if "psql" in args:
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
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    sys.exit(0)
                sys.exit(0)

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

            args = sys.argv[1:]
            with argv_log.open("a", encoding="utf-8") as fh:
                fh.write(" ".join(args) + "\\n")

            method = "GET"
            data = ""
            cfg_headers = {{}}
            write_fmt = ""
            out_file = ""
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

            def emit(code: int, payload: object) -> None:
                body_str = json.dumps(payload) if not isinstance(payload, str) else payload
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

            # :8951 research-host (real production /healthz JSON shape)
            if ":8951" in url:
                up = ("com.dalton.research-host" in loaded) or (os.environ.get("FAKE_KEEP_8951_UP") == "1")
                if not up:
                    conn_refused()
                if url.endswith("/admin/drain"):
                    state["draining_8951"] = True
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(200, {{"ok": True, "draining": True, "in_flight": int(state.get("in_flight_8951", 0))}})
                if url.endswith("/healthz"):
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

            # :8943 legacy guidepoint proxy
            if ":8943" in url:
                if "com.dalton.guidepoint-mcp-proxy" not in loaded:
                    conn_refused()
                if url.endswith("/admin/drain"):
                    state["draining_8943"] = True
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(200, {{"ok": True, "draining": True, "in_flight": int(state.get("in_flight_8943", 0))}})
                if url.endswith("/healthz"):
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
                    payload_obj = {{"jsonrpc": "2.0", "id": 3, "result": {{"content": [{{"type": "text", "text": json.dumps({{"ok": True, "data": [{{"id": "gp-1"}}]}})}}]}}}}
                    if os.environ.get("FAKE_PROXY_SSE") == "1":
                        emit(200, "event: message\\ndata: " + json.dumps(payload_obj) + "\\n\\n")
                    emit(200, payload_obj)
                emit(200, {{"ok": True}})

            # :8960 legacy bridge
            if ":8960" in url:
                if "com.dalton.researchdata-bridge" not in loaded:
                    conn_refused()
                if url.endswith("/healthz"):
                    emit(200, {{"ok": True, "inflight": 0, "queued": 0}})
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
                    emit(200, {{"status": "ok"}})
                if "/maintenance/admission/probe" in url:
                    adm = json.loads(adm_file.read_text(encoding="utf-8")) if adm_file.is_file() else {{"paused": False, "revision": 0}}
                    if adm.get("paused") is True:
                        emit(503, {{"detail": {{"error_code": "maintenance_in_progress", "revision": adm.get("revision", 0)}}}})
                    if os.environ.get("FAKE_FAIL_OPEN_VERIFY") == "1":
                        emit(503, {{"detail": {{"error_code": "maintenance_in_progress", "revision": adm.get("revision", 0)}}}})
                    emit(200, {{"ok": True, "admitted": True, "revision": adm.get("revision", 0)}})
                emit(200, {{"ok": True}})

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
        "DOCKER_BIN": str(fake_docker),
        "CURL_BIN": str(fake_curl),
        "DALTON_ROLLBACK_SERVICE_TIMEOUT_S": "3",
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
        "secrets": [bridge_token, rh_token, rh_admin_token, svc_token],
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
    h["proxy_plist"].unlink()
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
    assert "wait-lock-free" in text
    assert "wait-owner" in text
    assert "compose-args" in text
    assert "set -euo pipefail" in text
    assert "var/dalton/cutover-snapshots/recovery-bin/rollback_research_host.sh" in text
    assert "docker compose -f docker-compose.yml -f docker-compose.dalton.yml" not in text

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
        assert forbidden not in text, f"runbook should not reference retired/internal token: {forbidden}"

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


def _prod_rh_healthz(*, owner_lock_held: bool = True, standby: bool = False, in_flight: int = 0) -> dict[str, Any]:
    """Return the exact production `/healthz` JSON shape emitted by `:8951`."""
    return {
        "ok": True,
        "status": "ok",
        "service": "dalton-research-host",
        "version": "1.0.0",
        "profile": "everflow",
        "draining": False,
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
                return 200, _prod_rh_healthz(owner_lock_held=True, standby=False, in_flight=1), None
            if n_health == 2:
                # Still in_flight=1 with a temp file present
                tmp_tok.write_text("partial", encoding="utf-8")
                return 200, _prod_rh_healthz(owner_lock_held=True, standby=False, in_flight=1), None
            # Third poll: tmp gone and in_flight == 0
            if tmp_tok.exists():
                tmp_tok.unlink()
            return 200, _prod_rh_healthz(owner_lock_held=True, standby=False, in_flight=0), None
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
    """Item 7: `:8943/mcp` SSE (`text/event-stream`) responses decode cleanly via shared JSON/SSE decoder."""
    sse_ok = (
        "event: message\n"
        'data: {"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"{\\"ok\\":true,\\"data\\":[{\\"id\\":\\"gp-sse\\"}]}"}]}}\n\n'
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

    orig_http = preflight._http_request
    http_ok = create_autospec(orig_http, spec_set=True)
    http_ok.side_effect = [
        (200, {"ok": True, "owner_lock_held": True, "draining": False, "in_flight": 0}, None),
        (200, {"ok": True}, None),
        (200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, None),
        (200, {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, None),
        (200, sse_ok, None),
    ]
    monkeypatch.setattr(preflight, "_http_request", http_ok)
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

    # Also verify SSE isError fails cleanly
    http_bad = create_autospec(orig_http, spec_set=True)
    http_bad.side_effect = [
        (200, {"ok": True, "owner_lock_held": True, "draining": False, "in_flight": 0}, None),
        (200, {"ok": True}, None),
        (200, {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, None),
        (200, {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": json.dumps({"ok": True})}]}}, None),
        (200, sse_err, None),
    ]
    monkeypatch.setattr(preflight, "_http_request", http_bad)
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

    # Now delete rel-before manually -> rollback MUST fail closed (gate CLOSED, no ROLLBACK_OK)
    _simulate_post_cutover_state(h)
    shutil.rmtree(rel_dir / "rel-before")
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
    assert "3/7 restore-snapshot" in r_rb.stderr
    assert "MAINTENANCE ADMISSION GATE REMAINS CLOSED" in r_rb.stderr
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
