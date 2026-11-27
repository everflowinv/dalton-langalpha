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

import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import textwrap
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

    tokens_file = gp_cfg_dir / "guidepoint_mcp_tokens.json"
    tokens_file.write_text(
        json.dumps({"access_token": "tok-before", "refresh_token": "ref-before", "obtained_at": 1700000000, "expires_in": 600}) + "\n",
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

            # :8951 research-host
            if ":8951" in url:
                up = ("com.dalton.research-host" in loaded) or (os.environ.get("FAKE_KEEP_8951_UP") == "1")
                if not up:
                    conn_refused()
                if url.endswith("/healthz"):
                    emit(200, {{"ok": True, "guidepoint": {{"owner_lock_held": True}}}})
                emit(200, {{"ok": True}})

            # :8943 legacy guidepoint proxy
            if ":8943" in url:
                if "com.dalton.guidepoint-mcp-proxy" not in loaded:
                    conn_refused()
                if url.endswith("/healthz"):
                    owner = "legacy_proxy_8943"
                    if journal_file.is_file():
                        j = json.loads(journal_file.read_text(encoding="utf-8"))
                        owner = str(j.get("target_owner") or "legacy_proxy_8943")
                    emit(200, {{"ok": True, "owner_lock_held": owner == "legacy_proxy_8943", "draining": False}})
                if url.endswith("/mcp"):
                    if os.environ.get("FAKE_FAIL_GUIDEPOINT_PROBE") == "1":
                        emit(500, {{"error": "simulated guidepoint proxy failure"}})
                    state.setdefault("probes", []).append("proxy_8943_search_library")
                    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                    emit(200, {{"jsonrpc": "2.0", "id": 3, "result": {{"content": [{{"type": "text", "text": json.dumps({{"ok": True, "data": [{{"id": "gp-1"}}]}})}}]}}}})
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
    h["tokens_file"].write_text(
        json.dumps({"access_token": "tok-after-8951", "refresh_token": "ref-after-8951", "obtained_at": 1700001000, "expires_in": 600}) + "\n",
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

    # Every tracked file and symlink matches pre-cutover snapshot byte-for-byte
    assert _snapshot_tree_hashes(h["tracked_paths"]) == baseline_files

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

    # Retry after fault clears -> succeeds and reopens gate
    r_ok1 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok1.returncode == 0, r_ok1.stdout + r_ok1.stderr
    assert _snapshot_tree_hashes(h["tracked_paths"]) == baseline_files
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is False

    # Repeat a second full rollback immediately -> identical final files & open gate
    r_ok2 = subprocess.run(
        ["/bin/bash", str(ROLLBACK_SH), snap_dir],
        env=h["env"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r_ok2.returncode == 0, r_ok2.stdout + r_ok2.stderr
    assert _snapshot_tree_hashes(h["tracked_paths"]) == baseline_files
    final_state = json.loads(h["state_file"].read_text(encoding="utf-8"))
    assert "com.dalton.research-host" not in final_state["loaded"]
    assert "com.dalton.guidepoint-mcp-proxy" in final_state["loaded"]
    assert "com.dalton.researchdata-bridge" in final_state["loaded"]
    assert json.loads(h["adm_file"].read_text(encoding="utf-8"))["paused"] is False


# ---------------------------------------------------------------------------
# 6. Parse RESEARCH_HOST_SWITCH_RUNBOOK.md and verify every script & CLI flag
# ---------------------------------------------------------------------------


def _extract_runbook_script_invocations(markdown: str) -> list[tuple[str, list[str]]]:
    """Extract every invocation of a repo script (`scripts/dalton/...`) from fenced bash blocks."""
    invocations: list[tuple[str, list[str]]] = []
    for block_match in re.finditer(r"```bash\n(.*?)```", markdown, re.DOTALL):
        block = block_match.group(1)
        # Join backslash-continued lines
        joined = re.sub(r"\\\n\s*", " ", block)
        for raw_line in joined.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Unwrap `VAR="$(cmd ...)"` command substitutions
            sub_m = re.match(r'^[A-Za-z_][A-Za-z0-9_]*="\$\((.+)\)"$', line)
            if sub_m:
                line = sub_m.group(1).strip()
            # Split pipelines
            for segment in line.split("|"):
                seg = segment.strip()
                if not seg:
                    continue
                try:
                    tokens = shlex.split(seg)
                except ValueError:
                    tokens = seg.split()
                for idx, tok in enumerate(tokens):
                    if tok.startswith("scripts/dalton/") and (tok.endswith(".sh") or tok.endswith(".py")):
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

    # Required top-level sections
    assert "切换前一天" in text
    assert "停机切换" in text
    assert "回滚" in text
    assert "503" in text
    assert "web/releases/current" in text
    assert "search_library" in text
    assert "company-wiki" in text

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
        script_abs = REPO_ROOT / rel_script
        assert script_abs.is_file(), f"runbook references non-existent script: {rel_script}"

        used_flags = [tok.split("=", 1)[0] for tok in cli_args if tok.startswith("--")]

        if rel_script == "scripts/dalton/ops/prod/preflight.py":
            if cli_args and not cli_args[0].startswith("-"):
                subcmd = cli_args[0]
                assert subcmd in preflight._SUBCOMMANDS, f"unknown preflight.py subcommand in runbook: {subcmd}"
                allowed = preflight_sub_flags.get(subcmd, set())
                for fl in used_flags:
                    assert fl in allowed, f"preflight.py {subcmd} does not accept flag {fl}"
            else:
                for fl in used_flags:
                    assert fl in preflight_root_flags, f"preflight.py does not accept flag {fl}"
        elif rel_script == "scripts/dalton/bridge_subprocess_env.py":
            assert cli_args, "bridge_subprocess_env.py requires a subcommand"
            subcmd = cli_args[0]
            assert subcmd in bridge_sub_flags, f"unknown bridge_subprocess_env.py subcommand: {subcmd}"
            for fl in used_flags:
                assert fl in bridge_sub_flags[subcmd], f"bridge_subprocess_env.py {subcmd} does not accept {fl}"
        elif rel_script.endswith(".sh"):
            allowed = _collect_shell_script_flags(script_abs)
            for fl in used_flags:
                assert fl in allowed, f"{rel_script} does not accept flag {fl} (allowed={sorted(allowed)})"

    # Confirm the runbook actually references all core scripts
    for must_ref in (
        "scripts/dalton/ops/prod/preflight.py",
        "scripts/dalton/ops/prod/snapshot_research_host.sh",
        "scripts/dalton/ops/prod/rollback_research_host.sh",
        "scripts/dalton/ops/prod/wait_backend_idle.sh",
        "scripts/dalton/ops/prod/switch_research_host.sh",
        "scripts/dalton/ops/prod/backup.sh",
        "scripts/dalton/bridge_subprocess_env.py",
    ):
        assert must_ref in referenced_scripts, f"runbook must invoke {must_ref}"


# ---------------------------------------------------------------------------
# 7. create_autospec contract test & Standalone Core isolation check
# ---------------------------------------------------------------------------


def test_probe_services_http_double_uses_create_autospec_and_core_package_zero_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_mock = create_autospec(preflight._http_request, spec_set=True)
    http_mock.side_effect = [
        (200, {"ok": True, "guidepoint": {"owner_lock_held": True}}, None),
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
