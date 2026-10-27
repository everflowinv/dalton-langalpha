"""MCP dependency install, schema discovery, and builtin server startup.

The wrapper union and the per-workspace overlay live in ``tool_overlay.py``.

Functions take the owning ``PTCSandbox`` as their explicit first argument;
``PTCSandbox`` exposes same-name delegators, so call sites and patch
semantics are unchanged.
"""

import asyncio
import json
import posixpath
import re
import shlex
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import structlog

from ptc_agent.core.sandbox._defaults import DEFAULT_DEPENDENCIES
from ptc_agent.core.sandbox.retry import RetryPolicy

from ..paths import SandboxLayout
from . import livefs_mount
from .supervisor_runtime import protocol as supervisor_protocol

if TYPE_CHECKING:
    from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox

logger = structlog.get_logger(__name__)


async def _install_dependencies(sandbox: "PTCSandbox") -> None:
    """Install required Python packages in sandbox (no-snapshot fallback)."""
    logger.info("Installing dependencies (no snapshot)")

    # yfinance pins curl_cffi<0.14 but scrapling[all] requires >=0.14.
    # Override resolves the conflict (tested, yfinance works with 0.14+).
    # DALTON-HOOK(sandbox-pip-system): pass --system to match Dockerfile.sandbox
    # when UV_SYSTEM_PYTHON / VIRTUAL_ENV is not set in the container.
    install_cmd = (
        "echo 'curl_cffi>=0.14' > /tmp/_overrides.txt && "
            f"uv pip install --system -q --override /tmp/_overrides.txt {' '.join(DEFAULT_DEPENDENCIES)} && "
            "rm -f /tmp/_overrides.txt"
    )

    try:
        assert sandbox.runtime is not None
        result = await sandbox._runtime_call(
            sandbox.runtime.exec,
            install_cmd,
            retry_policy=RetryPolicy.SAFE,
        )
        if result.exit_code != 0:
            logger.warning(
                "Dependency install exited with non-zero code",
                exit_code=result.exit_code,
                output=result.stdout[:500],
            )
        else:
            logger.info("Dependencies installed")
    except OSError as e:
        logger.error(f"Failed to install dependencies: {e}")
        raise

    # Install Scrapling browsers (Camoufox for StealthyFetcher)
    try:
        result = await sandbox._runtime_call(
            sandbox.runtime.exec,
            "scrapling install",
            retry_policy=RetryPolicy.SAFE,
        )
        if result.exit_code != 0:
            logger.warning(
                "Scrapling browser install failed",
                output=result.stdout[:300] if result.stdout else "",
            )
        else:
            logger.info("Scrapling browsers installed")
    except Exception as e:
        logger.warning(f"Scrapling browser install skipped: {e}")


async def _upload_discovery_client(
    sandbox: "PTCSandbox", extra_servers: list[Any] | None = None
) -> str:
    """Upload a config-only discovery client to a UNIQUE path; return it.

        The client depends only on the effective server configs (no schemas),
        so it can be generated before any discovery has run. It is written to
        a per-call ``_internal`` temp path — never ``tools/mcp_client.py`` —
        because concurrent discoveries (a bulk import fires several probes plus
        the background kick at once) would otherwise clobber one another's
        config and report spurious ``unknown server`` errors, and a probe must
        never replace the runtime client the agent's wrappers import.

        ``extra_servers`` carries freshly-resolved configs for an on-demand
        discovery whose session may predate the edit. They are merged over the
        session's enabled set by name (override an edited server, append a new
        one) so the probe sees pending changes.
        """
    assert sandbox.runtime is not None
    work_dir = sandbox._work_dir
    enabled_servers = [
        server for server in sandbox.config.mcp.servers if server.enabled
    ]
    if extra_servers:
        for srv in extra_servers:
            if not getattr(srv, "enabled", True):
                continue
            idx = next(
                (i for i, s in enumerate(enabled_servers) if s.name == srv.name),
                None,
            )
            if idx is None:
                enabled_servers.append(srv)
            else:
                enabled_servers[idx] = srv
    # Pass the sandbox's real work dir (Lane A handoff): the client embeds
    # the vault path + mcp_servers path from it. Defaulting would point the
    # vault/server paths at the wrong directory after a working-dir change.
    # The union is not folded in: it holds the pre-edit config of the very
    # server this probe is meant to see edited.
    mcp_client_code = sandbox.tool_generator.generate_mcp_client_code(
        enabled_servers, working_dir=work_dir, fold_union=False
    )
    layout = SandboxLayout(work_dir)
    client_path = (
        f"{layout.internal}/.mcp_discover_client_{uuid.uuid4().hex}.py"
    )
    await _upload_client(sandbox, mcp_client_code, client_path)
    logger.debug("MCP discovery client installed", path=client_path)
    return client_path


async def _upload_client(sandbox: "PTCSandbox", code: str, path: str) -> None:
    assert sandbox.runtime is not None
    await sandbox._runtime_call(
        sandbox.runtime.exec,
        f"mkdir -p {shlex.quote(posixpath.dirname(path))}",
        retry_policy=RetryPolicy.SAFE,
    )
    await sandbox._runtime_call(
        sandbox.runtime.upload_file,
        code.encode("utf-8"),
        path,
        retry_policy=RetryPolicy.SAFE,
    )


def _discover_call(client_path: str, name: str, out_path: str) -> str:
    # python3, not python: the no-snapshot fallback image never gets the
    # /usr/bin/python alias the snapshot build adds.
    q = shlex.quote
    return f"python3 {q(client_path)} discover {q(name)} {q(out_path)}"


def _read_listing(raw: bytes | None) -> dict[str, Any] | None:
    """Normalize the discovery CLI's result file, or None when it wrote none.

    None rather than an error, because only the caller knows what a missing
    file means. The CLI writes status ``ok`` or ``error`` alone, so anything
    else reads as an error.
    """
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("the discovery result is not a JSON object")
    info = parsed.get("server_info")
    return {
        "status": "ok" if parsed.get("status") == "ok" else "error",
        "error": str(parsed.get("error") or ""),
        "tools": parsed.get("tools") or [],
        "server_info": info if isinstance(info, dict) else None,
    }


async def discover_user_mcp_schemas(
    sandbox: "PTCSandbox", servers: list[Any]
) -> dict[str, dict[str, Any]]:
    """Discover tool schemas for user MCP servers via the in-sandbox client.

        For each server: run ``mcp_client.py discover <name> <out>`` (file IPC —
        the CLI writes its result JSON to a temp file, never stdout), read the
        file back, delete it. Per-server error isolation; one hung/broken server
        never blocks the others. Returns
        ``{name: {"status","error","tools","server_info"}}``.
        No vault file is needed — the client substitutes inert placeholders.
        """
    await sandbox._wait_ready()
    assert sandbox.runtime is not None
    work_dir = sandbox._work_dir
    layout = SandboxLayout(work_dir)

    # Upload a config-current discovery client FIRST (it depends only on
    # config, not on schemas) so discovery runs against the latest server
    # set. Pass the servers being discovered so an on-demand probe reflects
    # a pending add/edit the live session has not re-resolved yet (≤30s
    # window). The path is unique per call: concurrent discoveries must not
    # read each other's config (spurious "unknown server" otherwise).
    client_path = await sandbox._upload_discovery_client(extra_servers=servers)

    sem = asyncio.Semaphore(sandbox._DISCOVERY_CONCURRENCY)

    async def _discover_one(server: Any) -> tuple[str, dict[str, Any]]:
        name = server.name
        # Unique per invocation: concurrent discoveries of the same server
        # (background kick + on-demand /discover) must not share a file.
        out_path = f"{layout.internal}/.mcp_discover_{uuid.uuid4().hex}.json"
        async with sem:
            try:
                await sandbox._runtime_call(
                    sandbox.runtime.exec,
                    f"cd {shlex.quote(work_dir)} && "
                    + _discover_call(client_path, name, out_path),
                    timeout=sandbox._DISCOVERY_EXEC_TIMEOUT_S,
                    retry_policy=RetryPolicy.SAFE,
                    total_timeout=float(sandbox._DISCOVERY_EXEC_TIMEOUT_S + 30),
                )
                listing = _read_listing(await sandbox.adownload_file_bytes(out_path))
                if listing is None:
                    return name, {
                        "status": "error",
                        "error": "discovery produced no output",
                        "tools": [],
                    }
                return name, listing
            except Exception as e:  # noqa: BLE001 — isolate one bad server
                logger.warning(
                    "MCP discovery failed for server", server=name, error=str(e)
                )
                return name, {"status": "error", "error": str(e), "tools": []}
            finally:
                # Best-effort temp-file cleanup; never fail discovery on it.
                try:
                    await sandbox._runtime_call(
                        sandbox.runtime.exec,
                        f"rm -f {shlex.quote(out_path)}",
                        retry_policy=RetryPolicy.SAFE,
                    )
                except Exception:
                    pass

    try:
        pairs = await asyncio.gather(*[_discover_one(s) for s in servers])
    finally:
        # Best-effort removal of this call's discovery client; never fail
        # discovery on cleanup.
        try:
            await sandbox._runtime_call(
                sandbox.runtime.exec,
                f"rm -f {shlex.quote(client_path)}",
                retry_policy=RetryPolicy.SAFE,
            )
        except Exception:
            pass
    return dict(pairs)


# Runs the discovery CLI as the leader of a session of its own, so everything
# the server's command spawned (npx's node, uvx's python) stays in that session
# and is stopped as a whole once the listing is in or the budget is spent. A
# server that keeps running after its stdin closes would otherwise outlive the
# check. The session, not the process group: the client gives each server a
# group of its own. Its last line says how the client ended, which is all a
# missing result file cannot.
_CHECK_LEADER = """\
import os, signal, subprocess, sys, time
budget = float(sys.argv[1])
child = subprocess.Popen(
    sys.argv[2:], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
    start_new_session=True,
)
try:
    rc = str(child.wait(timeout=budget))
except subprocess.TimeoutExpired:
    rc = "timeout"
def members():
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        ps = subprocess.run(["ps", "-A", "-o", "pid="], capture_output=True, text=True)
        pids = [int(p) for p in ps.stdout.split()]
    found = []
    for pid in pids:
        try:
            if pid != child.pid and os.getsid(pid) == child.pid:
                found.append(pid)
        except OSError:
            pass
    return found
for sig in (signal.SIGTERM, signal.SIGKILL):
    child.send_signal(sig)
    found = members()
    for pid in found:
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    if found or child.poll() is None:
        time.sleep(0.5)
child.wait()
print("MCP_CHECK_RC=" + rc, flush=True)
"""

_CHECK_RC_RE = re.compile(r"MCP_CHECK_RC=(\S+)")

# Strong refs so a cleanup outlives the check that stopped waiting for it;
# never consulted.
_check_cleanups: set[asyncio.Task] = set()

# How long a check waits for its files to go before it answers. The run takes
# the client and the vault file with it, so the removal after it is a backstop
# that keeps going in the background; its SAFE retries can take minutes, which
# no request should spend.
CHECK_CLEANUP_WAIT_S = 3.0


async def check_user_mcp_server(
    sandbox: "PTCSandbox",
    server: Any,
    vault: Mapping[str, str],
    *,
    budget_s: float,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Start one unsaved server the way the computer runs it and list its tools.

    The client is the one a turn's calls go through, composed for this server
    alone, and it resolves ``${vault:NAME}`` refs from a vault file exactly as
    it does at runtime. That file is this check's own, holding only ``vault``,
    so a value the caller has not stored yet resolves without touching the
    computer's vault file, and a sibling's check never reads it. The command
    runs from the folder the MCP supervisor runs servers from.

    Nothing is left behind: the run removes the client and the vault file the
    moment it ends, the server's process group is stopped, and the result file
    is removed after it is read, even when the caller is cancelled. Returns
    ``{"status", "error", "tools", "server_info"}``; status is ``ok``,
    ``error``, or ``timeout`` when ``budget_s`` ran out first.

    ``deadline`` (event-loop time) bounds the run, which raises TimeoutError
    past it. The removal is waited on after the run and outside it, for at
    most ``CHECK_CLEANUP_WAIT_S``, so a run that answered in time keeps its
    answer however slowly the computer deletes three files.
    """
    async with asyncio.timeout_at(deadline):
        await sandbox._wait_ready()
    assert sandbox.runtime is not None
    work_dir = sandbox._work_dir
    layout = SandboxLayout(work_dir)
    stem = f"{SandboxLayout.MCP_CHECK_PREFIX}{uuid.uuid4().hex}"
    vault_rel = f"{stem}.vault.json"
    client_path = layout.join(f"{stem}.py")
    vault_path = layout.join(vault_rel)
    out_path = layout.join(f"{stem}.out.json")

    # The discovery entry point blanks every vault ref unless the server
    # opts in, and a check has to start the server with its real values.
    probe = server.model_copy(update={"discovery_uses_secrets": True})
    config = sandbox.tool_generator.generate_client_config(
        [probe], working_dir=work_dir, fold_union=False
    )
    emitted = dict(config.get("layout") or {})
    emitted["SandboxLayout"] = {
        **(emitted.get("SandboxLayout") or {}),
        "VAULT_SECRETS_FILE": vault_rel,
    }
    config = {**config, "layout": emitted}
    code = sandbox.tool_generator.compose_mcp_client_code(config)

    run_s = max(1, int(budget_s))
    q = shlex.quote
    script = (
        f"cd {q(layout.internal_src)} 2>/dev/null || cd {q(work_dir)} || exit 97\n"
        f"python3 -c {q(_CHECK_LEADER)} {run_s} "
        f"{_discover_call(client_path, server.name, out_path)}\n"
        f"rm -f {q(client_path)} {q(vault_path)}\n"
    )
    try:
        async with asyncio.timeout_at(deadline):
            await _upload_client(sandbox, code, client_path)
            if vault:
                await sandbox._runtime_call(
                    sandbox.runtime.upload_file,
                    json.dumps(dict(vault)).encode("utf-8"),
                    vault_path,
                    retry_policy=RetryPolicy.SAFE,
                )
            # UNSAFE: a retried run would find the files the first one removed.
            result = await sandbox._runtime_call(
                sandbox.runtime.exec,
                f"sh -c {q(script)}",
                timeout=run_s + 15,
                retry_policy=RetryPolicy.UNSAFE,
                total_timeout=float(run_s + 20),
            )
            raw = await sandbox.adownload_file_bytes(out_path)
    finally:
        cleanup = asyncio.ensure_future(
            _remove_check_files(sandbox, (client_path, vault_path, out_path))
        )
        _check_cleanups.add(cleanup)
        cleanup.add_done_callback(_check_cleanups.discard)
        # A bounded wait, not a shield: it gives up without cancelling the
        # removal, and it returns rather than raising when the time is up.
        await asyncio.wait({cleanup}, timeout=CHECK_CLEANUP_WAIT_S)

    try:
        listing = _read_listing(raw)
    except ValueError:
        listing = None
    if listing is not None:
        return listing
    output = getattr(result, "stdout", "") or ""
    rc = _CHECK_RC_RE.findall(output)
    if rc and rc[-1] == "timeout":
        return {"status": "timeout", "error": "", "tools": [], "server_info": None}
    logger.warning(
        "MCP command check produced no result",
        server=server.name,
        exit_code=getattr(result, "exit_code", None),
        output=output[-500:],
    )
    return {
        "status": "error",
        "error": "the check produced no result",
        "tools": [],
        "server_info": None,
    }


async def _remove_check_files(sandbox: "PTCSandbox", paths: tuple[str, ...]) -> None:
    try:
        await sandbox._runtime_call(
            sandbox.runtime.exec,
            "rm -f " + " ".join(shlex.quote(p) for p in paths),
            retry_policy=RetryPolicy.SAFE,
        )
    except Exception as e:  # noqa: BLE001 - the run removed the secret-bearing files
        logger.warning("MCP command check cleanup failed", error=str(e))


async def _start_internal_mcp_servers(
    sandbox: "PTCSandbox", *, replace: bool = False
) -> bool | None:
    """Start the computer's MCP supervisor, if it is not already listening.

    One daemon per computer owns every sandbox-side MCP server process, so a
    server is handshaken once per idle window instead of once per
    ``execute_code``. Starting it is idempotent: the daemon takes an exclusive
    lock before binding, so a second start exits without touching the socket.

    A failure here is logged, never raised. The generated client falls back to
    spawning servers in the execution's own interpreter, which is what every
    call did before this daemon existed, so a computer whose supervisor cannot
    start is slower rather than broken.

    Answers whether the file mount answered at its path (None: the exec
    failed). A reconnect runs this exec anyway, and a mount gone with a boot
    or a dead daemon meets no command that would say so.

    ``replace`` first retires a running daemon, for a computer whose env
    changed under it: the daemon and its servers keep the env they started
    with.
    """
    assert sandbox.runtime is not None
    work_dir = sandbox._work_dir
    layout = SandboxLayout(work_dir)
    socket_path = f"{work_dir}/{supervisor_protocol.SOCKET_REL_PATH}"
    log_path = f"{work_dir}/{supervisor_protocol.LOG_REL_PATH}"
    src_root = layout.internal_src
    quoted_socket = shlex.quote(socket_path)
    # A machine that never mounts has no mount to lose, so it is not asked.
    probe = sandbox.mounts_livefs
    pythonpath = f"PYTHONPATH={shlex.quote(src_root)}"
    daemon = f"python3 -m {supervisor_protocol.PACKAGE_NAME} {shlex.quote(work_dir)}"
    to_log = f">> {shlex.quote(log_path)} 2>&1"
    command = (
        (f"{livefs_mount.PROBE} ; " if probe else "")
        + f"mkdir -p {shlex.quote(posixpath.dirname(socket_path))} && "
        f"cd {shlex.quote(src_root)} && "
        # A daemon that would not retire leaves this start the loser of the
        # lock, which is no worse than not replacing it.
        + (f"{{ {pythonpath} {daemon} --retire {to_log} || true ; }} && " if replace else "")
        + f"{{ {pythonpath} nohup {daemon} {to_log} < /dev/null & }} ; "
        # Poll rather than sleep a fixed amount: a warm start binds in
        # milliseconds and only a genuinely failing one pays the full wait.
        f"for _ in $(seq 30); do [ -S {quoted_socket} ] && break; sleep 0.1; done; "
        f"[ -S {quoted_socket} ]"
    )
    sandbox.mcp_server_sessions = {
        "supervisor": {"socket": socket_path, "log": log_path, "started": False}
    }
    try:
        result = await sandbox._runtime_call(
            sandbox.runtime.exec,
            command,
            retry_policy=RetryPolicy.SAFE,
        )
    except Exception as e:  # noqa: BLE001 - the client's fallback covers this
        logger.warning("MCP supervisor start failed", error=str(e))
        return None
    mount_answers = (
        livefs_mount.answered(getattr(result, "stdout", None)) if probe else None
    )
    exit_code = getattr(result, "exit_code", 0)
    if exit_code:
        logger.warning(
            "MCP supervisor did not come up; calls fall back to in-process spawn",
            exit_code=exit_code,
            log=log_path,
        )
        return mount_answers
    sandbox.mcp_server_sessions["supervisor"]["started"] = True
    logger.info("MCP supervisor listening", socket=socket_path)
    return mount_answers



def _known_local_names(sandbox: "PTCSandbox | None") -> set[str]:
    """Names of the workspace directory itself and sandbox layout tiers."""
    # DALTON-HOOK(sandbox-pip-system): skip workspace directory names (e.g.
    # ai_strategy_lp) and sandbox layout folders when auto-installing imports.
    names: set[str] = {"tools", "_internal", "mcp_servers"}
    if sandbox is None:
        return names
    ws_fn = getattr(sandbox, "workspace", None)
    if callable(ws_fn):
        try:
            ws = ws_fn()
            for candidate in (
                getattr(ws, "dir_name", None),
                posixpath.basename((getattr(ws, "workspace", "") or "").rstrip("/")),
            ):
                if isinstance(candidate, str) and candidate:
                    names.add(candidate)
        except Exception:
            pass
    for attr in ("workspace_dir_name", "working_dir"):
        val = getattr(sandbox, attr, None)
        if isinstance(val, str) and val:
            names.add(posixpath.basename(val.rstrip("/")))
    return names


async def _filter_local_packages(
    sandbox: "PTCSandbox", packages: list[str]
) -> list[str]:
    """Filter out package names that match a workspace directory or local module."""
    # DALTON-HOOK(sandbox-pip-system): avoid running `uv pip install` on local
    # directories or `.py` modules inside the workspace or computer root.
    known_local = _known_local_names(sandbox)
    remaining = [pkg for pkg in packages if pkg not in known_local]
    if not remaining or getattr(sandbox, "runtime", None) is None:
        return remaining

    search_dirs: list[str] = []
    ws_fn = getattr(sandbox, "workspace", None)
    if callable(ws_fn):
        try:
            ws = ws_fn()
            ws_dir = getattr(ws, "workspace", None)
            if isinstance(ws_dir, str) and ws_dir:
                search_dirs.append(ws_dir)
        except Exception:
            pass
    layout = getattr(sandbox, "layout", None)
    root_dir = getattr(layout, "root", None)
    if isinstance(root_dir, str) and root_dir and root_dir not in search_dirs:
        search_dirs.append(root_dir)

    if not search_dirs:
        return remaining

    checks: list[str] = []
    for pkg in remaining:
        q_pkg = shlex.quote(pkg)
        conds = " || ".join(
            f"[ -d {shlex.quote(d)}/{q_pkg} ] || [ -f {shlex.quote(d)}/{q_pkg}.py ]"
            for d in search_dirs
        )
        checks.append(f"if {conds}; then printf '%s\\n' {q_pkg}; fi")

    try:
        res = await sandbox._runtime_call(
            sandbox.runtime.exec,
            "; ".join(checks),
            retry_policy=RetryPolicy.SAFE,
        )
        if getattr(res, "exit_code", 0) != 0:
            logger.warning(
                "Local workspace module probe failed; skipping auto-install",
                packages=remaining,
                exit_code=getattr(res, "exit_code", None),
                stderr=getattr(res, "stderr", "") or "",
            )
            return []
        stdout = getattr(res, "stdout", "") or ""
        found_local = {
            line.strip() for line in stdout.splitlines() if line.strip()
        }
        if found_local:
            logger.info(
                "Skipping auto-install for local workspace directory/module",
                skipped=sorted(found_local),
            )
            return [pkg for pkg in remaining if pkg not in found_local]
    except Exception:
        logger.warning(
            "Local workspace module probe raised an error; skipping auto-install",
            packages=remaining,
            exc_info=True,
        )
        return []
    return remaining


def _detect_missing_imports(sandbox: "PTCSandbox", stderr: str) -> list[str]:
    """Extract missing module names from the executed script's own traceback.

        Args:
            stderr: Standard error output from code execution

        Returns:
            List of missing package names (base package only, e.g., 'foo' from 'foo.bar')
        """
    import re

    # Whatever comes out of here is handed to `uv pip install` in the sandbox,
    # so the input is trusted as narrowly as possible against two attacks:
    #
    # 1. Shell injection — the capture is a real dotted module path, never
    #    "anything between the quotes". A name with a space or ``;`` fails to
    #    match at all (and could never name an installable package anyway).
    # 2. Dependency confusion — a third-party MCP server spawned during the run
    #    shares this stderr stream and could inject a fake
    #    "ModuleNotFoundError: No module named 'evil'" to make us install an
    #    attacker-registered package (whose sdist build runs code). So only the
    #    executed script's OWN unhandled exception is trusted: the FINAL
    #    traceback block of stderr, and only when it carries a genuine frame
    #    line. A server's mid-stream text, or a preceding block, is ignored.
    marker = "Traceback (most recent call last):"
    start = stderr.rfind(marker)
    if start == -1:
        return []
    final_tb = stderr[start:]
    if '\n  File "' not in final_tb:
        # No real frame → not a Python interpreter traceback; don't trust it.
        return []

    patterns = [
        r"ModuleNotFoundError: No module named ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]",
        r"ImportError: No module named ['\"]([A-Za-z_][A-Za-z0-9_.]*)['\"]",
    ]

    matches = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, final_tb))

    # Handle submodule imports (e.g., "foo.bar" -> "foo")
    # DALTON-HOOK(sandbox-pip-system): also deduplicate and filter known workspace/layout folder names
    known_local = _known_local_names(sandbox) if sandbox is not None else set()
    base_packages = [
        pkg for pkg in {m.split(".")[0] for m in matches} if pkg not in known_local
    ]

    if base_packages:
        logger.info(
            "Detected missing imports",
            packages=base_packages,
        )

    return base_packages


async def _install_package(sandbox: "PTCSandbox", package: str) -> bool:
    """Install a Python package in the sandbox.

        Args:
            package: Package name to install

        Returns:
            True if installation succeeded, False otherwise
        """
    try:
        logger.info(f"Auto-installing missing package: {package}")
        assert sandbox.runtime is not None
        result = await sandbox._runtime_call(
            sandbox.runtime.exec,
            # Quoted at the sink as well as filtered at the parser: this is the
            # only place a caller-supplied name becomes shell text, so it should
            # be safe for callers that don't come through _detect_missing_imports.
            # DALTON-HOOK(sandbox-pip-system): pass --system to uv pip install.
            f"uv pip install --system -q {shlex.quote(package)}",
            retry_policy=RetryPolicy.SAFE,
        )
        exit_code = getattr(result, "exit_code", 1)
        if exit_code == 0:
            logger.info(f"Successfully installed package: {package}")
            return True
        logger.warning(
            f"Failed to install package: {package}, exit_code={exit_code}"
        )
        return False
    except OSError as e:
        logger.warning(f"Failed to install {package}: {e}")
        return False
