#!/usr/bin/env python3
# auth: oss
"""Standalone Core Batch 6 acceptance checks (`b6_tls_backup_upgrade.py`).

Note: `auth: local` HTTPS login/refresh smoke verification will be added once
Batch 4b (local sign-in UI & init wiring) merges.

Uses Python standard library + `_lib.py` only (no `httpx`, `psycopg`, or repo `src`
imports) so it runs directly on a bare Linux CI runner alongside `b1` / `b2`.

Verifies on a running Standalone Core (`auth: oss`) stack:
1. `init.sh` loopback / TLS / domain-port / invalid-mode validation, trusted
   proxy defaults (`DALTON_TRUSTED_PROXIES`, `DALTON_TRUSTED_PROXY_HOPS=1`),
   and zero leaked secrets.
2. Caddy configuration & TLS modes:
   - Loopback HTTP mode publishes only port 8088 on `127.0.0.1`.
   - `caddy adapt` succeeds on all four modes (`loopback`, `internal`, `auto`, `custom`).
   - Live `custom` mode startup with an ephemeral self-signed certificate passes
     strict TLS verification (`CERT_REQUIRED` + hostname check), HSTS, and host-port
     HTTP -> HTTPS redirect.
   - Live `internal` CA HTTPS passes strict TLS handshake against Caddy's internal
     root CA, HSTS & security headers, header stripping (`X-Service-Token`, `X-User-Id`),
     404 internal route blocks, host-port HTTP -> HTTPS redirect, access-log redaction
     of `?token` / `Location`, and `127.0.0.1` loopback boundaries.
3. `scripts/standalone/backup.sh` produces a verified zero-downtime backup:
   - Real sentinel chat thread (with persisted messages + replay content) and
     sentinel MinIO upload (`text/plain`) with recorded SHA-256.
   - Plaintext `.env` is never placed in backup folders; `--env-passphrase-file`
     produces AES-256 encrypted `env.enc` with PBKDF2 params recorded in `manifest.json`;
     `--retain` prunes older backups cleanly.
4. `scripts/standalone/restore.sh` (v1: empty target instance only):
   - Decrypts `env.enc` into `.env` (mode 0600) only after target-empty preflight
     passes, unsets all `.env` keys via `env -u` before every `docker compose` call,
     restores objects directly into original bucket name(s), restores DB directly
     into the official database, and restores Caddy CA.
   - Table row counts, sentinel thread row, real SSE `/messages/replay` assistant text,
     sentinel MinIO object SHA-256 & `ContentType`, and Caddy CA fingerprint all match.
   - Re-running `restore.sh` against a non-empty instance is refused with a pointer
     to `STANDALONE-DEPLOY.md` (`down -v`) while the live instance continues serving
     HTTPS 200.
   - Following the manual reset steps (inspect project container volumes ->
     `docker compose down -v`) and re-running `restore.sh` succeeds with identical data.
5. `scripts/standalone/upgrade.sh` performs a same-version rolling upgrade from a
   release tarball: backs up with the old `backup.sh`, tags `pre-upgrade-<ts>` images,
   snapshots `.upgrade-backup/<ts>/`, replaces services, converges with `up -d`, and
   passes HTTPS health & replay content verification.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

# Allow invoking directly as `python3 tests/standalone/checks/b6_tls_backup_upgrade.py`
_CHECKS_DIR = str(Path(__file__).resolve().parent)
if _CHECKS_DIR not in sys.path:
    sys.path.insert(0, _CHECKS_DIR)
_lib = importlib.import_module("_lib")

LOCAL_DEV_USER = "local-dev-user"
PROBE_EDGE_USER = "probe-edge-user"
FAKE_MODEL_NAME = _lib.FAKE_MODEL_NAME
SENTINEL_WORKSPACE_NAME = "b6-verify-workspace"
SENTINEL_CHAT_PROMPT = "Hello Dalton B6 backup restore sentinel check!"
SENTINEL_UPLOAD_FILENAME = "b6_sentinel_object.txt"
SENTINEL_UPLOAD_PAYLOAD = b"DALTON_B6_MINIO_SENTINEL_DATA\n" * 128
LOG_LEAK_TOKEN_SENTINEL = "b6_redir_secret_token_do_not_log_987654"

KEY_ROW_COUNT_TABLES: tuple[str, ...] = (
    "users",
    "workspaces",
    "conversation_threads",
    "dalton_chat_file_uploads",
    "alembic_version",
    "alembic_version_dalton",
)


def log(msg: str) -> None:
    print(f"[b6_tls_backup_upgrade] {msg}", flush=True)


class _LoopbackTLSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that always dials IPv4 127.0.0.1 while verifying TLS SNI for ``host``."""

    def __init__(
        self,
        server_hostname: str,
        port: int,
        *,
        context: ssl.SSLContext,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(server_hostname, port, timeout=timeout, context=context)
        self._sni_host = server_hostname

    def connect(self) -> None:
        raw_sock = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self._sni_host)


def _https_request(
    method: str,
    server_hostname: str,
    port: int,
    path: str,
    *,
    context: ssl.SSLContext,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 30.0,
) -> _lib.HttpResponse:
    req_headers: dict[str, str] = {"Host": f"{server_hostname}:{port}"}
    if headers:
        req_headers.update(headers)
    payload: bytes | None = data
    if json_body is not None:
        payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["Content-Type"] = "application/json"

    conn = _LoopbackTLSConnection(server_hostname, port, context=context, timeout=timeout)
    try:
        conn.request(method.upper(), path, body=payload, headers=req_headers)
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return _lib.HttpResponse(status=resp.status, headers=resp_headers, body=body)
    finally:
        conn.close()


def _parse_sse_bytes(raw: bytes) -> list[_lib.SSEEvent]:
    events: list[_lib.SSEEvent] = []
    buf: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if buf:
                ev = _lib.parse_sse_block(buf)
                buf = []
                if ev is not None:
                    events.append(ev)
        else:
            buf.append(line)
    if buf:
        ev = _lib.parse_sse_block(buf)
        if ev is not None:
            events.append(ev)
    return events


def _http_raw_request(
    method: str,
    port: int,
    path: str,
    *,
    host_header: str = "localhost",
    timeout: float = 15.0,
) -> _lib.HttpResponse:
    """Single-hop plain HTTP request to 127.0.0.1:<port> without following redirects."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method.upper(), path, headers={"Host": host_header})
        resp = conn.getresponse()
        body = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return _lib.HttpResponse(status=resp.status, headers=resp_headers, body=body)
    finally:
        conn.close()


def _wait_for_caddy_internal_ca(
    compose_cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> bytes:
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        proc = subprocess.run(
            [*compose_cmd, "exec", "-T", "edge", "cat", "/data/caddy/pki/authorities/local/root.crt"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            check=False,
            timeout=15,
        )
        if proc.returncode == 0 and b"BEGIN CERTIFICATE" in proc.stdout:
            return proc.stdout
        last_err = (proc.stderr or b"").decode("utf-8", errors="replace")[-300:]
        time.sleep(1.5)
    raise TimeoutError(f"Caddy internal CA root.crt not ready within {timeout}s (last: {last_err})")


def _wait_for_https_ready(
    server_hostname: str,
    port: int,
    path: str,
    *,
    context: ssl.SSLContext,
    timeout: float = 90.0,
) -> _lib.HttpResponse:
    deadline = time.monotonic() + timeout
    last_err = "no response"
    while time.monotonic() < deadline:
        try:
            resp = _https_request("GET", server_hostname, port, path, context=context, timeout=10.0)
            if resp.status == 200:
                return resp
            last_err = f"HTTP {resp.status}: {resp.text[:200]}"
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5)
    raise TimeoutError(
        f"HTTPS endpoint https://{server_hostname}:{port}{path} not ready within {timeout}s "
        f"(last: {last_err})"
    )


def _container_id(ctx: _lib.ComposeContext, service: str) -> str:
    ids = [
        line.strip()
        for line in ctx.compose("ps", "-q", service).stdout.decode("utf-8").splitlines()
        if line.strip()
    ]
    assert ids, f"No running container ID found for service {service!r}"
    return ids[0]


def _inspect_published_host_ips(container_id: str) -> list[tuple[str, str, str]]:
    """Return ``[(container_port, host_ip, host_port), ...]`` for all published bindings."""
    raw = subprocess.check_output(["docker", "inspect", container_id], text=True, timeout=30)
    data = json.loads(raw)
    assert data, f"docker inspect returned empty data for {container_id}"
    ports_map = (data[0].get("NetworkSettings") or {}).get("Ports") or {}
    bindings: list[tuple[str, str, str]] = []
    for cport, entries in ports_map.items():
        if not entries:
            continue
        for entry in entries:
            bindings.append((cport, str(entry.get("HostIp", "")), str(entry.get("HostPort", ""))))
    return bindings


def _run_cmd(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    redact_env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        out = proc.stdout or ""
        err = proc.stderr or ""
        if redact_env:
            out = _lib._redact_secrets_text(out, redact_env)
            err = _lib._redact_secrets_text(err, redact_env)
        raise RuntimeError(
            f"Command failed (rc={proc.returncode}): {cmd[0]} {cmd[1] if len(cmd) > 1 else ''}\n"
            f"STDOUT:\n{out[-2000:]}\nSTDERR:\n{err[-2000:]}"
        )
    return proc


def _tls_compose_files(ctx: _lib.ComposeContext) -> list[Path]:
    tls_file = (ctx.work_dir / "deploy" / "standalone" / "docker-compose.tls.yml").resolve()
    assert tls_file.is_file(), f"Missing {tls_file}"
    files = list(ctx.compose_files)
    if tls_file not in files:
        files.append(tls_file)
    return files


def _tls_ctx(ctx: _lib.ComposeContext) -> _lib.ComposeContext:
    return _lib.ComposeContext(
        work_dir=ctx.work_dir,
        compose_files=_tls_compose_files(ctx),
        artifacts_dir=ctx.artifacts_dir,
        edge_url=ctx.edge_url,
        project_name=ctx.project_name,
    )


def _stage_init_dir(src_work_dir: Path, dst_dir: Path) -> Path:
    script_dir = dst_dir / "scripts" / "standalone"
    script_dir.mkdir(parents=True, exist_ok=True)
    init_dst = script_dir / "init.sh"
    shutil.copy2(src_work_dir / "scripts" / "standalone" / "init.sh", init_dst)
    shutil.copy2(src_work_dir / ".env.core.example", dst_dir / ".env.core.example")
    return init_dst


def check_init_tls_modes(ctx: _lib.ComposeContext) -> None:
    log("1/6 Checking init.sh loopback/TLS modes, domain-port guard, trusted proxies & secret silence...")
    cases = (
        (
            "loopback-default",
            ["--domain", "localhost"],
            {
                "DOMAIN": "localhost",
                "DALTON_TLS_MODE": "loopback",
                "DALTON_EDGE_TLS_BIND": "127.0.0.1",
                "DALTON_CORE_INTERNAL_SUBNET": "172.31.250.0/24",
                "DALTON_TRUSTED_PROXY_HOPS": "1",
                "COMPOSE_FILE": "docker-compose.yml:docker-compose.core.yml",
                "PUBLIC_APP_URL": "http://127.0.0.1:8088",
            },
        ),
        (
            "loopback-internal",
            ["--domain", "localhost", "--tls-mode", "internal"],
            {
                "DOMAIN": "localhost",
                "DALTON_TLS_MODE": "internal",
                "DALTON_EDGE_TLS_BIND": "127.0.0.1",
                "DALTON_EDGE_HTTP_PORT": "18080",
                "DALTON_EDGE_HTTPS_PORT": "18443",
                "COMPOSE_FILE": "docker-compose.yml:docker-compose.core.yml:deploy/standalone/docker-compose.tls.yml",
                "PUBLIC_APP_URL": "https://localhost:18443",
                "DALTON_INVITE_EMAIL_DOMAIN": "team.dalton.local",
            },
        ),
        (
            "loopback-ipv6-internal",
            ["--domain", "::1", "--tls-mode", "internal"],
            {
                "DOMAIN": "::1",
                "DALTON_TLS_MODE": "internal",
                "DALTON_EDGE_TLS_BIND": "[::1]",
                "DALTON_CADDY_HOST": "[::1]",
                "DALTON_EDGE_HTTP_PORT": "18080",
                "DALTON_EDGE_HTTPS_PORT": "18443",
                "COMPOSE_FILE": "docker-compose.yml:docker-compose.core.yml:deploy/standalone/docker-compose.tls.ipv6.yml",
                "PUBLIC_APP_URL": "https://[::1]:18443",
            },
        ),
        (
            "loopback-auto-stays-loopback",
            ["--domain", "localhost", "--tls-mode", "auto"],
            {
                "DOMAIN": "localhost",
                "DALTON_TLS_MODE": "auto",
                "DALTON_EDGE_TLS_BIND": "127.0.0.1",
                "DALTON_EDGE_HTTP_PORT": "18080",
                "DALTON_EDGE_HTTPS_PORT": "18443",
                "PUBLIC_APP_URL": "https://localhost:18443",
            },
        ),
    )

    with tempfile.TemporaryDirectory(prefix="b6-init-check-") as tmpdir:
        tmp_root = Path(tmpdir)
        for name, extra_args, expected in cases:
            case_dir = tmp_root / name
            init_copy = _stage_init_dir(ctx.work_dir, case_dir)
            proc = _run_cmd(
                [
                    "bash",
                    str(init_copy),
                    "--non-interactive",
                    "--skip-sandbox-build",
                    *extra_args,
                ],
                cwd=case_dir,
                timeout=60,
            )
            env_path = case_dir / ".env"
            parsed = _lib.load_env_file(env_path)
            assert "DALTON_TRUSTED_PROXIES" not in parsed, (
                "init.sh must not hardcode DALTON_TRUSTED_PROXIES (derived from DALTON_CORE_INTERNAL_SUBNET in compose)"
            )
            for key, want in expected.items():
                got = parsed.get(key)
                assert got == want, (
                    f"init.sh {extra_args}: expected {key}={want!r}, got {got!r}"
                )
            combined_out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            for secret_key in _lib.SECRET_ENV_KEYS:
                secret_val = (parsed.get(secret_key) or "").strip()
                if len(secret_val) >= 12:
                    assert secret_val not in combined_out, (
                        f"init.sh leaked {secret_key} to stdout/stderr!"
                    )

        # Pre-existing non-loopback listen address must be rewritten to 127.0.0.1 even with no flags
        stale_dir = tmp_root / "rewrite-stale-bind"
        init_stale = _stage_init_dir(ctx.work_dir, stale_dir)
        stale_env = stale_dir / ".env"
        stale_env.write_text(
            "DOMAIN=localhost\nDALTON_TLS_MODE=auto\nDALTON_EDGE_TLS_BIND=0.0.0.0\n",
            encoding="utf-8",
        )
        os.chmod(stale_env, 0o600)
        _run_cmd(
            ["bash", str(init_stale), "--non-interactive", "--skip-sandbox-build"],
            cwd=stale_dir,
            timeout=60,
        )
        assert _lib.load_env_file(stale_env).get("DALTON_EDGE_TLS_BIND") == "127.0.0.1", (
            "init.sh must always rewrite pre-existing DALTON_EDGE_TLS_BIND to 127.0.0.1 before Batch 4b"
        )

        # Public domain must be rejected until Batch 4b ships
        pub_dir = tmp_root / "reject-public"
        init_pub = _stage_init_dir(ctx.work_dir, pub_dir)
        res_pub = _run_cmd(
            ["bash", str(init_pub), "--non-interactive", "--skip-sandbox-build", "--domain", "dalton.example.com"],
            cwd=pub_dir,
            check=False,
            timeout=30,
        )
        assert res_pub.returncode != 0 and not (pub_dir / ".env").exists(), (
            f"Expected init.sh to refuse non-loopback domain before Batch 4b: rc={res_pub.returncode}"
        )

        # Domain with port must be rejected
        port_dir = tmp_root / "reject-port"
        init_port = _stage_init_dir(ctx.work_dir, port_dir)
        res_port = _run_cmd(
            ["bash", str(init_port), "--non-interactive", "--skip-sandbox-build", "--domain", "https://localhost:8443/"],
            cwd=port_dir,
            check=False,
            timeout=30,
        )
        assert res_port.returncode == 2, f"Expected exit 2 for host:port, got {res_port.returncode}"

    log("1/6 OK (init.sh modes, loopback guards, trusted proxy defaults & secret silence verified).")


def check_loopback_ports_caddy_adapt_and_custom_tls(ctx: _lib.ComposeContext) -> None:
    log("2/6 Verifying loopback 8088-only port publish, `caddy adapt` on all 4 modes, and live `custom` TLS...")

    # 2a. Loopback HTTP mode must publish ONLY port 8088 on 127.0.0.1
    loopback_bindings = _inspect_published_host_ips(_container_id(ctx, "edge"))
    assert len(loopback_bindings) == 1, (
        f"Loopback HTTP mode should publish only port 8088, got {loopback_bindings}"
    )
    cport, host_ip, _host_port = loopback_bindings[0]
    assert cport == "8088/tcp" and host_ip == "127.0.0.1", (
        f"Expected single 127.0.0.1 -> 8088/tcp binding in loopback mode, got {loopback_bindings}"
    )

    # 2b. Run `caddy adapt` for all four modes (including loopback auto & IPv6 internal)
    adapt_cases = (
        ("loopback", "localhost", "18443"),
        ("internal", "localhost", "18443"),
        ("internal", "[::1]", "18443"),
        ("auto", "dalton.example.com", "443"),
        ("auto", "localhost", "18443"),
        ("custom", "localhost", "18443"),
    )
    for mode, domain, https_port in adapt_cases:
        proc = _run_cmd(
            ctx.compose_cmd(
                "exec",
                "-T",
                "-e",
                f"DALTON_TLS_MODE={mode}",
                "-e",
                f"DOMAIN={domain}",
                "-e",
                f"DALTON_EDGE_HTTPS_PORT={https_port}",
                "edge",
                "caddy",
                "adapt",
                "--config",
                "/etc/caddy/Caddyfile",
                "--adapter",
                "caddyfile",
            ),
            cwd=ctx.work_dir,
            timeout=30,
            redact_env=ctx.env,
        )
        adapted = json.loads(proc.stdout)
        assert "apps" in adapted and "http" in adapted["apps"], (
            f"caddy adapt output for mode={mode} missing apps.http"
        )
        if mode == "auto" and https_port == "18443":
            assert ":18443" in proc.stdout, (
                "Loopback auto mode HTTP->HTTPS redirect must include host port :18443"
            )
        if domain == "[::1]" and https_port == "18443":
            assert (
                "[{http.regexp.ipv6_host.1}]:18443" in proc.stdout
                or "[{re.ipv6_host.1}]:18443" in proc.stdout
            ), f"IPv6 HTTP->HTTPS redirect must preserve brackets around IPv6 host: {proc.stdout[-800:]}"
    log("2b OK (`caddy adapt` passed for loopback, internal, IPv6 internal, auto, loopback auto :18443, and custom).")

    # 2c. Start `custom` mode once with an ephemeral self-signed certificate and complete strict handshake
    env_file = ctx.work_dir / ".env"
    saved_env_bytes = env_file.read_bytes()
    tls_context = _tls_ctx(ctx)

    with tempfile.TemporaryDirectory(prefix="b6-custom-certs-") as cert_tmp:
        cert_dir = Path(cert_tmp)
        cert_pem = cert_dir / "cert.pem"
        key_pem = cert_dir / "key.pem"
        _run_cmd(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-days",
                "1",
                "-nodes",
                "-keyout",
                str(key_pem),
                "-out",
                str(cert_pem),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            cwd=cert_dir,
            timeout=30,
        )
        os.chmod(key_pem, 0o600)
        os.chmod(cert_pem, 0o644)

        try:
            _run_cmd(
                [
                    "bash",
                    str(ctx.work_dir / "scripts" / "standalone" / "init.sh"),
                    "--non-interactive",
                    "--skip-sandbox-build",
                    "--domain",
                    "localhost",
                    "--tls-mode",
                    "custom",
                    "--tls-cert-dir",
                    str(cert_dir),
                ],
                cwd=ctx.work_dir,
                redact_env=ctx.env,
                timeout=60,
            )
            custom_env = _lib.load_env_file(env_file)
            http_port = int(custom_env.get("DALTON_EDGE_HTTP_PORT") or "18080")
            https_port = int(custom_env.get("DALTON_EDGE_HTTPS_PORT") or "18443")

            tls_context.compose("up", "-d", "--no-deps", "--force-recreate", "edge")
            ssl_ctx = ssl.create_default_context(cafile=str(cert_pem))
            ssl_ctx.check_hostname = True
            ssl_ctx.verify_mode = ssl.CERT_REQUIRED

            _wait_for_https_ready("localhost", https_port, "/__probe/health", context=ssl_ctx, timeout=90.0)
            resp = _https_request("GET", "localhost", https_port, "/", context=ssl_ctx)
            assert resp.status == 200, f"Custom TLS GET / failed: {resp.status}"
            assert "max-age=" in resp.headers.get("strict-transport-security", ""), (
                f"Custom TLS missing HSTS header: {resp.headers}"
            )

            redir = _http_raw_request("GET", http_port, "/custom-check", host_header="localhost")
            assert redir.status in (301, 302, 308), f"Custom TLS HTTP redirect failed: {redir.status}"
            assert redir.headers.get("location") == f"https://localhost:{https_port}/custom-check", (
                f"Custom TLS redirect location mismatch: {redir.headers.get('location')!r}"
            )
            log(f"2c OK (live custom TLS handshake, HSTS, and redirect to :{https_port} verified).")
        finally:
            env_file.write_bytes(saved_env_bytes)
            os.chmod(env_file, 0o600)
            ctx.compose("up", "-d", "--no-deps", "--force-recreate", "edge")
            _lib.wait_for_http_ready(f"{ctx.edge_url}/__probe/health", timeout=60.0)


def seed_sentinel_data(ctx: _lib.ComposeContext) -> dict[str, str]:
    log("Seeding deterministic B6 workspace, real chat thread (with messages), and MinIO upload sentinel...")
    ctx.psql_exec(
        f"""
        INSERT INTO users (user_id, email, name)
        VALUES
            ('{LOCAL_DEV_USER}', 'local-dev@localhost.test', 'Local Dev User'),
            ('{PROBE_EDGE_USER}', 'probe-edge@localhost.test', 'Probe Edge User')
        ON CONFLICT (user_id) DO NOTHING;
        """
    )
    # Configure the CI fake Responses API provider/model via HTTP API (self-contained & idempotent).
    _lib.configure_fake_llm_model(ctx, model_name=FAKE_MODEL_NAME)

    ws_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/workspaces",
        json_body={"name": SENTINEL_WORKSPACE_NAME},
    )
    assert ws_resp.status == 201, f"Create sentinel workspace failed: {ws_resp.status} {ws_resp.text}"
    workspace_id = ws_resp.json()["workspace_id"]

    # Send a real chat turn via fake-llm so checkpoints & messages are persisted for replay verification
    chat_events: list[_lib.SSEEvent] = []
    with _lib.open_sse_stream(
        "POST",
        f"{ctx.edge_url}/api/v1/threads/messages",
        json_body={
            "workspace_id": workspace_id,
            "llm_model": FAKE_MODEL_NAME,
            "messages": [{"role": "user", "content": SENTINEL_CHAT_PROMPT}],
        },
        timeout=180.0,
    ) as (status, _headers, stream):
        assert status == 200, f"Sentinel chat POST failed: HTTP {status}"
        for ev in stream:
            chat_events.append(ev)
            if ev.event == "error":
                raise AssertionError(f"Sentinel chat stream emitted error: {ev.raw_data}")

    thread_id = _lib.extract_thread_id(chat_events)
    assert thread_id, f"No thread_id in sentinel chat events: {[e.event for e in chat_events]}"
    chat_text = _lib.extract_assistant_text(chat_events).strip()
    assert chat_text, "Sentinel chat stream returned empty assistant text"

    # Capture baseline /messages/replay SSE body before backup
    replay_events: list[_lib.SSEEvent] = []
    with _lib.open_sse_stream(
        "GET",
        f"{ctx.edge_url}/api/v1/threads/{thread_id}/messages/replay",
        timeout=60.0,
    ) as (rstatus, _rheaders, rstream):
        assert rstatus == 200, f"Baseline replay GET failed: HTTP {rstatus}"
        for ev in rstream:
            replay_events.append(ev)
    assert "replay_done" in [e.event for e in replay_events], "Baseline replay missing replay_done"
    baseline_replay_text = _lib.extract_assistant_text(replay_events).strip()
    assert baseline_replay_text, "Baseline replay assistant text is empty"

    payload_sha = hashlib.sha256(SENTINEL_UPLOAD_PAYLOAD).hexdigest()
    init_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads",
        json_body={
            "workspace_id": workspace_id,
            "filename": SENTINEL_UPLOAD_FILENAME,
            "size_bytes": len(SENTINEL_UPLOAD_PAYLOAD),
            "mime_type": "text/plain",
            "sha256": payload_sha,
        },
    )
    assert init_resp.status == 200, f"Initiate sentinel upload failed: {init_resp.status} {init_resp.text}"
    upload_id = init_resp.json()["upload_id"]

    part_resp = _lib.http_request(
        "PUT",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads/{upload_id}/parts/1",
        headers={
            "Content-Type": "text/plain",
            "X-Part-Sha256": payload_sha,
        },
        data=SENTINEL_UPLOAD_PAYLOAD,
    )
    assert part_resp.status == 200, f"Upload sentinel part failed: {part_resp.status} {part_resp.text}"

    comp_resp = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads/{upload_id}/complete",
        json_body={"sha256": payload_sha},
    )
    assert comp_resp.status == 200, f"Complete sentinel upload failed: {comp_resp.status} {comp_resp.text}"

    object_key = ctx.psql_query(
        f"SELECT object_key FROM dalton_chat_file_uploads WHERE upload_id = '{upload_id}';"
    )
    assert object_key, f"Could not resolve object_key for upload {upload_id}"
    bucket_name = ctx.env.get("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME") or "chat-uploads"

    # Also initiate an in-progress multipart upload (state='uploading', no final MinIO object)
    # to verify both backup.sh and restore.sh skip 'uploading' records and report the skipped count.
    uploading_init = _lib.http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/dalton/chat-uploads",
        json_body={
            "workspace_id": workspace_id,
            "filename": "b6-in-progress-upload.txt",
            "size_bytes": 64,
            "mime_type": "text/plain",
        },
    )
    assert uploading_init.status == 200, (
        f"Initiate in-progress sentinel upload failed: {uploading_init.status} {uploading_init.text}"
    )
    uploading_id = uploading_init.json()["upload_id"]
    uploading_state = ctx.psql_query(
        f"SELECT state FROM dalton_chat_file_uploads WHERE upload_id = '{uploading_id}';"
    )
    assert uploading_state == "uploading", (
        f"Expected in-progress upload {uploading_id} to have state='uploading', got {uploading_state!r}"
    )

    log(
        f"Seeded sentinel workspace={workspace_id}, thread={thread_id} "
        f"(replay_len={len(baseline_replay_text)}), upload={upload_id} (ready), "
        f"uploading={uploading_id} (uploading), key={bucket_name}/{object_key}."
    )
    return {
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "upload_id": upload_id,
        "uploading_id": uploading_id,
        "bucket": bucket_name,
        "object_key": object_key,
        "payload_sha": payload_sha,
        "replay_text": baseline_replay_text,
    }


def check_internal_ca_https_and_boundaries(
    ctx: _lib.ComposeContext,
) -> tuple[bytes, int, int]:
    log("3/6 Switching edge to Caddy internal CA HTTPS and verifying TLS, HSTS, redirect port & log redaction...")
    init_script = ctx.work_dir / "scripts" / "standalone" / "init.sh"
    _run_cmd(
        [
            "bash",
            str(init_script),
            "--non-interactive",
            "--skip-sandbox-build",
            "--domain",
            "localhost",
            "--tls-mode",
            "internal",
        ],
        cwd=ctx.work_dir,
        redact_env=ctx.env,
        timeout=60,
    )
    refreshed_env = _lib.load_env_file(ctx.work_dir / ".env")
    http_port = int(refreshed_env.get("DALTON_EDGE_HTTP_PORT") or "18080")
    https_port = int(refreshed_env.get("DALTON_EDGE_HTTPS_PORT") or "18443")

    tls_context = _tls_ctx(ctx)
    tls_context.compose("up", "-d", "--no-deps", "--force-recreate", "edge")
    ca_pem = _wait_for_caddy_internal_ca(tls_context.compose_cmd(), ctx.work_dir, timeout=60.0)

    with tempfile.NamedTemporaryFile(prefix="b6-caddy-ca-", suffix=".crt", delete=False) as fp:
        fp.write(ca_pem)
        ca_path = Path(fp.name)
    try:
        ssl_ctx = ssl.create_default_context(cafile=str(ca_path))
        ssl_ctx.check_hostname = True
        ssl_ctx.verify_mode = ssl.CERT_REQUIRED

        # 3a. HTTPS liveness, HSTS & security headers
        _wait_for_https_ready("localhost", https_port, "/__probe/health", context=ssl_ctx, timeout=90.0)
        root_resp = _https_request("GET", "localhost", https_port, "/", context=ssl_ctx)
        assert root_resp.status == 200, f"HTTPS GET / failed: {root_resp.status}"
        assert root_resp.headers.get("x-content-type-options") == "nosniff", (
            f"Missing nosniff header over HTTPS: {root_resp.headers}"
        )
        assert root_resp.headers.get("x-frame-options") == "SAMEORIGIN", (
            f"Missing X-Frame-Options header over HTTPS: {root_resp.headers}"
        )
        assert "max-age=" in root_resp.headers.get("strict-transport-security", ""), (
            f"Missing HSTS header on internal mode: {root_resp.headers}"
        )
        assert "server" not in root_resp.headers, (
            f"Server header leaked over HTTPS: {root_resp.headers}"
        )

        # 3b. Internal header stripping over HTTPS (/__probe/headers and /api/v1/users/me)
        token = ctx.internal_service_token
        probe_resp = _https_request(
            "GET",
            "localhost",
            https_port,
            "/__probe/headers",
            context=ssl_ctx,
            headers={
                "X-Service-Token": token,
                "X-User-Id": PROBE_EDGE_USER,
                "X-Probe-Sentinel": "b6-https-keep",
            },
        )
        assert probe_resp.status == 200, f"HTTPS /__probe/headers failed: {probe_resp.status}"
        echo_headers = {k.lower(): v for k, v in (probe_resp.json().get("headers") or {}).items()}
        assert echo_headers.get("x-probe-sentinel") == "b6-https-keep", (
            f"Sentinel missing over HTTPS: {echo_headers}"
        )
        assert "x-service-token" not in echo_headers, "HTTPS edge leaked X-Service-Token!"
        assert "x-user-id" not in echo_headers, "HTTPS edge leaked X-User-Id!"

        me_resp = _https_request(
            "GET",
            "localhost",
            https_port,
            "/api/v1/users/me",
            context=ssl_ctx,
            headers={
                "X-Service-Token": token,
                "X-User-Id": PROBE_EDGE_USER,
            },
        )
        assert me_resp.status == 200, f"HTTPS /api/v1/users/me failed: {me_resp.status} {me_resp.text}"
        assert me_resp.json()["user"]["user_id"] == LOCAL_DEV_USER, (
            f"HTTPS edge honored forged headers: {me_resp.json()}"
        )

        # 3c. Blocked internal routes stay 404 over HTTPS
        for blocked_path in (
            "/api/v1/dalton/chat-uploads/sandbox-object",
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/metrics",
        ):
            bresp = _https_request("GET", "localhost", https_port, blocked_path, context=ssl_ctx)
            assert bresp.status == 404, (
                f"Expected 404 for {blocked_path} over HTTPS, got {bresp.status}"
            )

        # 3d. Plain HTTP on port 80 redirects to https://localhost:<https_port> and does NOT log ?token / Location
        redir_resp = _http_raw_request(
            "GET",
            http_port,
            f"/probe-redir?token={LOG_LEAK_TOKEN_SENTINEL}",
            host_header=f"localhost:{http_port}",
        )
        assert redir_resp.status in (301, 302, 308), (
            f"Expected HTTP->HTTPS redirect on :{http_port}, got {redir_resp.status}"
        )
        location = redir_resp.headers.get("location", "")
        expected_loc = f"https://localhost:{https_port}/probe-redir?token={LOG_LEAK_TOKEN_SENTINEL}"
        assert location == expected_loc, (
            f"Expected HTTP->HTTPS redirect location {expected_loc!r}, got {location!r}"
        )

        # Check edge logs to verify neither request>uri nor resp_headers>Location leaked the token
        time.sleep(0.5)
        edge_logs = _run_cmd(
            tls_context.compose_cmd("logs", "--no-color", "--tail=200", "edge"),
            cwd=ctx.work_dir,
            timeout=30,
        ).stdout
        assert LOG_LEAK_TOKEN_SENTINEL not in edge_logs, (
            "Edge access logs leaked redirect query token (check resp_headers>Location filter)!"
        )

        # 3e. Loopback boundary unchanged: edge host ports bound exclusively to 127.0.0.1,
        #     and internal data services publish 0 host ports.
        edge_bindings = _inspect_published_host_ips(_container_id(tls_context, "edge"))
        assert edge_bindings, "Expected published ports on edge container"
        for cport, host_ip, host_port in edge_bindings:
            assert host_ip == "127.0.0.1", (
                f"Loopback boundary violated on edge ({cport} -> {host_ip}:{host_port})"
            )
        for internal_svc in ("postgres", "redis", "minio", "backend"):
            svc_bindings = _inspect_published_host_ips(_container_id(tls_context, internal_svc))
            assert not svc_bindings, (
                f"Internal service {internal_svc!r} must not publish host ports: {svc_bindings}"
            )
    finally:
        ca_path.unlink(missing_ok=True)

    log(
        f"3/6 OK (internal CA HTTPS verified on localhost:{https_port}, HSTS, redirect :{https_port}, "
        "Location log redaction, header stripping, 404 blocks, and loopback boundary verified)."
    )
    return ca_pem, http_port, https_port


def _snapshot_table_counts(
    compose_cmd: list[str],
    cwd: Path,
    db_user: str,
    db_name: str,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in KEY_ROW_COUNT_TABLES:
        proc = subprocess.run(
            [
                *compose_cmd,
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                db_user,
                "-d",
                db_name,
                "-v",
                "ON_ERROR_STOP=1",
                "-Atqc",
                f"SELECT COUNT(*) FROM {table};",
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        counts[table] = int(proc.stdout.strip())
    return counts


def _query_thread_row(
    compose_cmd: list[str],
    cwd: Path,
    db_user: str,
    db_name: str,
    thread_id: str,
    *,
    env: dict[str, str] | None = None,
) -> str:
    proc = subprocess.run(
        [
            *compose_cmd,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            db_user,
            "-d",
            db_name,
            "-v",
            "ON_ERROR_STOP=1",
            "-Atqc",
            (
                "SELECT conversation_thread_id, workspace_id, user_id, title, current_status "
                f"FROM conversation_threads WHERE conversation_thread_id = '{thread_id}';"
            ),
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return proc.stdout.strip()


def _count_minio_objects(
    compose_cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    bucket: str | None = None,
) -> int:
    script = (
        "import os, boto3\n"
        "from botocore.config import Config\n"
        "def _e(*ks, d=''):\n"
        "    for k in ks:\n"
        "        v = (os.getenv(k) or '').strip()\n"
        "        if v: return v\n"
        "    return d\n"
        "c = boto3.client('s3', endpoint_url=_e('DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL', d='http://minio:9000'), "
        "aws_access_key_id=_e('DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID', 'MINIO_ROOT_USER'), "
        "aws_secret_access_key=_e('DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY', 'MINIO_ROOT_PASSWORD'), "
        "region_name=_e('DALTON_CHAT_UPLOAD_STORAGE_REGION', d='us-east-1'), "
        "config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))\n"
        f"target_bucket = {bucket!r}\n"
        "if target_bucket is None:\n"
        "    target_bucket = _e('DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME', 'STORAGE_BUCKET_NAME', d='chat-uploads')\n"
        "total = 0\n"
        "for page in c.get_paginator('list_objects_v2').paginate(Bucket=target_bucket):\n"
        "    for entry in page.get('Contents', []):\n"
        "        if entry['Key'] != '.dalton-restore.json':\n"
        "            total += 1\n"
        "print(total)\n"
    )
    proc = subprocess.run(
        [*compose_cmd, "exec", "-T", "backend", "uv", "run", "--no-sync", "python", "-c", script],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return int(proc.stdout.strip().splitlines()[-1])


def _backend_active_bucket(
    compose_cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> str:
    proc = subprocess.run(
        [
            *compose_cmd,
            "exec",
            "-T",
            "backend",
            "uv",
            "run",
            "--no-sync",
            "python",
            "-c",
            "import os; print(os.getenv('DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME', ''))",
        ],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return proc.stdout.strip().splitlines()[-1]


def _fetch_minio_object_sha_and_ct(
    compose_cmd: list[str],
    cwd: Path,
    bucket: str,
    object_key: str,
    *,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    script = (
        "import hashlib, json, os, boto3\n"
        "from botocore.config import Config\n"
        "def _e(*ks, d=''):\n"
        "    for k in ks:\n"
        "        v = (os.getenv(k) or '').strip()\n"
        "        if v: return v\n"
        "    return d\n"
        "c = boto3.client('s3', endpoint_url=_e('DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL', d='http://minio:9000'), "
        "aws_access_key_id=_e('DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID', 'MINIO_ROOT_USER'), "
        "aws_secret_access_key=_e('DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY', 'MINIO_ROOT_PASSWORD'), "
        "region_name=_e('DALTON_CHAT_UPLOAD_STORAGE_REGION', d='us-east-1'), "
        "config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}))\n"
        f"resp = c.get_object(Bucket={bucket!r}, Key={object_key!r})\n"
        "h = hashlib.sha256()\n"
        "for ch in resp['Body'].iter_chunks(chunk_size=1024 * 1024):\n"
        "    if ch: h.update(ch)\n"
        "resp['Body'].close()\n"
        "print(json.dumps({'sha256': h.hexdigest(), 'content_type': resp.get('ContentType') or ''}))\n"
    )
    proc = subprocess.run(
        [*compose_cmd, "exec", "-T", "backend", "uv", "run", "--no-sync", "python", "-c", script],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    return str(payload["sha256"]), str(payload["content_type"])


def check_backup_script(
    ctx: _lib.ComposeContext,
    backups_root: Path,
    passphrase_file: Path,
    ca_pem: bytes,
) -> Path:
    log("4/6 Verifying scripts/standalone/backup.sh (streaming hot backup, PBKDF2 manifest, retention)...")
    backup_script = ctx.work_dir / "scripts" / "standalone" / "backup.sh"
    compose_files_str = ":".join(str(p) for p in _tls_compose_files(ctx))
    project_name = os.environ["COMPOSE_PROJECT_NAME"]
    assert project_name, "COMPOSE_PROJECT_NAME must be set by run_checks.sh"

    # 4a. Backup without passphrase file -> plaintext .env MUST NOT be in backup dir
    _run_cmd(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(ctx.work_dir),
            "--output-dir",
            str(backups_root),
            "--retain",
            "2",
            "--compose-files",
            compose_files_str,
            "--project-name",
            project_name,
        ],
        cwd=ctx.work_dir,
        redact_env=ctx.env,
        timeout=300,
    )
    dirs_after_1 = sorted(p for p in backups_root.glob("dalton-backup-*") if p.is_dir())
    assert len(dirs_after_1) == 1, f"Expected 1 backup dir, got {dirs_after_1}"
    b1 = dirs_after_1[0]
    assert not (b1 / ".env").exists(), "Plaintext .env must NEVER be placed in backup dir!"
    assert not (b1 / "env.enc").exists(), "env.enc should not exist without passphrase file!"
    m1 = json.loads((b1 / "manifest.json").read_text(encoding="utf-8"))
    assert m1["env_backup"] == "excluded", f"Expected env_backup='excluded', got {m1}"
    assert m1.get("compose_project") == project_name, (
        f"Expected manifest compose_project={project_name!r}, got {m1.get('compose_project')!r}"
    )

    time.sleep(1.1)

    # 4b. Second unencrypted backup (to exercise --retain 2 pruning on run 3)
    _run_cmd(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(ctx.work_dir),
            "--output-dir",
            str(backups_root),
            "--retain",
            "2",
            "--compose-files",
            compose_files_str,
            "--project-name",
            project_name,
        ],
        cwd=ctx.work_dir,
        redact_env=ctx.env,
        timeout=300,
    )
    time.sleep(1.1)

    # 4c. Third backup WITH --env-passphrase-file and --retain 2 -> prunes oldest, keeps 2
    proc3 = _run_cmd(
        [
            "bash",
            str(backup_script),
            "--work-dir",
            str(ctx.work_dir),
            "--output-dir",
            str(backups_root),
            "--retain",
            "2",
            "--env-passphrase-file",
            str(passphrase_file),
            "--compose-files",
            compose_files_str,
            "--project-name",
            project_name,
        ],
        cwd=ctx.work_dir,
        redact_env=ctx.env,
        timeout=300,
    )
    dirs_after_3 = sorted(p for p in backups_root.glob("dalton-backup-*") if p.is_dir())
    assert len(dirs_after_3) == 2, (
        f"Expected --retain 2 to keep exactly 2 backups, got {len(dirs_after_3)}: {dirs_after_3}"
    )
    latest_backup = dirs_after_3[-1]

    for fname in ("postgres.dump", "minio-objects.tar.gz", "caddy-data.tar.gz", "env.enc", "manifest.json", "SHA256SUMS"):
        fpath = latest_backup / fname
        assert fpath.is_file() and fpath.stat().st_size > 0, f"Missing or empty backup file: {fpath}"
    assert not (latest_backup / ".env").exists(), "Plaintext .env must NEVER be in backup dir!"

    manifest = json.loads((latest_backup / "manifest.json").read_text(encoding="utf-8"))
    expected_up, expected_da = ctx.get_alembic_heads()
    assert manifest["env_backup"] == "encrypted", f"Unexpected env_backup: {manifest}"
    enc_meta = manifest.get("env_encryption") or {}
    assert enc_meta.get("kdf") == "pbkdf2" and int(enc_meta.get("iterations", 0)) >= 100000, (
        f"manifest.json missing PBKDF2 params: {manifest}"
    )
    assert manifest["database"]["alembic_upstream"] == expected_up
    assert manifest["database"]["alembic_dalton"] == expected_da
    assert manifest["database"]["toc_entries"] > 0
    assert manifest["minio"]["object_count"] >= 1
    assert manifest["caddy"]["file_count"] >= 1

    # Confirm Caddy root CA certificate is actually inside caddy-data.tar.gz
    expected_ca_sha = hashlib.sha256(ca_pem).hexdigest()
    found_ca_sha = None
    with tarfile.open(latest_backup / "caddy-data.tar.gz", "r:gz") as tf:
        for member in tf.getmembers():
            norm = member.name.lstrip("./")
            if norm == "caddy/pki/authorities/local/root.crt":
                fp = tf.extractfile(member)
                assert fp is not None
                found_ca_sha = hashlib.sha256(fp.read()).hexdigest()
                break
    assert found_ca_sha == expected_ca_sha, (
        f"Caddy root.crt missing or mismatched in backup: {found_ca_sha!r} != {expected_ca_sha!r}"
    )

    # Confirm zero plaintext secrets appear in manifest.json, SHA256SUMS, env.enc, or stdout/stderr
    current_env = _lib.load_env_file(ctx.work_dir / ".env")
    check_texts = [
        (latest_backup / "manifest.json").read_text(encoding="utf-8"),
        (latest_backup / "SHA256SUMS").read_text(encoding="utf-8"),
        proc3.stdout or "",
        proc3.stderr or "",
    ]
    env_enc_bytes = (latest_backup / "env.enc").read_bytes()
    for secret_key in _lib.SECRET_ENV_KEYS:
        secret_val = (current_env.get(secret_key) or "").strip()
        if len(secret_val) < 12:
            continue
        for blob in check_texts:
            assert secret_val not in blob, f"Backup leaked plaintext {secret_key}!"
        assert secret_val.encode("utf-8") not in env_enc_bytes, (
            f"Encrypted env.enc contains plaintext {secret_key}!"
        )

    log(
        f"4/6 OK (backup={latest_backup.name}, db_toc={manifest['database']['toc_entries']}, "
        f"minio_objects={manifest['minio']['object_count']}, caddy_files={manifest['caddy']['file_count']}, "
        "retention=2 verified, 0 secret leaks)."
    )
    return latest_backup


def _prepare_clean_restore_dir(src_work_dir: Path, dst_dir: Path) -> None:
    """Copy only the deployment skeleton (no .env, no backups, no ci-artifacts) for empty-host restore."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    for rel_file in (
        "docker-compose.yml",
        "docker-compose.core.yml",
        "Dockerfile.sandbox",
        "agent_config.yaml",
        "agent_config.core.yaml",
        "config.core.yaml",
    ):
        src = src_work_dir / rel_file
        if src.is_file():
            shutil.copy2(src, dst_dir / rel_file)
    for rel_dir in ("deploy/standalone", "scripts/standalone"):
        src = src_work_dir / rel_dir
        dst = dst_dir / rel_dir
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)


def _rewrite_env_keys(raw_env: str, overrides: dict[str, str]) -> str:
    """Replace or append KEY=VAL lines in a `.env` file while preserving all other entries."""
    replaced: set[str] = set()
    out: list[str] = []
    for line in raw_env.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            key_part = stripped.removeprefix("export ").strip()
            for sep in ("=", ":"):
                if sep in key_part:
                    k = key_part.split(sep, 1)[0].strip()
                    if k in overrides:
                        if k not in replaced:
                            out.append(f"{k}={overrides[k]}\n")
                            replaced.add(k)
                        break
            else:
                out.append(line)
            continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"
    for k, v in overrides.items():
        if k not in replaced:
            out.append(f"{k}={v}\n")
    return "".join(out)


def _make_isolated_restore_backup(
    src_backup_dir: Path,
    dst_backup_dir: Path,
    src_env_path: Path,
    passphrase_file: Path,
    overrides: dict[str, str],
) -> None:
    """Verify original env.enc decrypts to src_env_path, then create a parallel-safe backup copy.

    Because `restore.sh` strictly reads `.env` and runs `env -u` on all `.env` keys before every
    `docker compose` call, running a restored stack alongside the still-live source stack in CI
    requires isolated project name / subnet / ports inside `env.enc` itself while keeping
    `postgres.dump`, `minio-objects.tar.gz`, `caddy-data.tar.gz`, and all secrets identical.
    """
    manifest = json.loads((src_backup_dir / "manifest.json").read_text(encoding="utf-8"))
    enc = manifest.get("env_encryption") or {}
    cipher = str(enc.get("cipher") or "aes-256-cbc")
    digest = str(enc.get("digest") or "sha256")
    iters = str(int(enc.get("iterations") or 600000))

    decrypted = subprocess.check_output(
        [
            "openssl",
            "enc",
            "-d",
            f"-{cipher}",
            "-pbkdf2",
            "-md",
            digest,
            "-iter",
            iters,
            "-in",
            str(src_backup_dir / "env.enc"),
            "-pass",
            f"file:{passphrase_file}",
        ],
        timeout=30,
    )
    assert decrypted == src_env_path.read_bytes(), (
        "Decrypted env.enc from backup does not match source .env byte-for-byte!"
    )

    shutil.copytree(src_backup_dir, dst_backup_dir)
    isolated_env_text = _rewrite_env_keys(decrypted.decode("utf-8"), overrides)
    plain_tmp = dst_backup_dir / ".isolated.plain.tmp"
    try:
        plain_tmp.write_text(isolated_env_text, encoding="utf-8")
        os.chmod(plain_tmp, 0o600)
        subprocess.run(
            [
                "openssl",
                "enc",
                f"-{cipher}",
                "-pbkdf2",
                "-md",
                digest,
                "-iter",
                iters,
                "-salt",
                "-in",
                str(plain_tmp),
                "-out",
                str(dst_backup_dir / "env.enc"),
                "-pass",
                f"file:{passphrase_file}",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        os.chmod(dst_backup_dir / "env.enc", 0o600)
    finally:
        plain_tmp.unlink(missing_ok=True)

    # Refresh env.enc entry in SHA256SUMS while keeping all other artifact digests intact.
    new_env_sha = hashlib.sha256((dst_backup_dir / "env.enc").read_bytes()).hexdigest()
    sums_path = dst_backup_dir / "SHA256SUMS"
    new_lines: list[str] = []
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        if line.endswith("  env.enc"):
            new_lines.append(f"{new_env_sha}  env.enc")
        else:
            new_lines.append(line)
    sums_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(sums_path, 0o600)


def _list_project_container_volumes(
    compose_cmd: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Mirror the manual step: resolve actual mounted volume names from this project's containers."""
    cids = [
        line.strip()
        for line in subprocess.check_output(
            [*compose_cmd, "ps", "-aq"],
            cwd=str(cwd),
            env=env,
            text=True,
            timeout=30,
        ).splitlines()
        if line.strip()
    ]
    if not cids:
        return []
    out = subprocess.check_output(
        [
            "docker",
            "inspect",
            "--format",
            '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{"\\n"}}{{end}}{{end}}',
            *cids,
        ],
        text=True,
        timeout=30,
    )
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def _verify_restored_data(
    restore_compose_cmd: list[str],
    restore_dir: Path,
    restore_env: dict[str, str],
    restore_https_port: int,
    ssl_ctx: ssl.SSLContext,
    db_user: str,
    db_name: str,
    orig_counts: dict[str, int],
    orig_thread_row: str,
    orig_minio_count: int,
    sentinel: dict[str, str],
) -> None:
    """Assert restored DB row counts, known thread + SSE replay, and MinIO object SHA-256."""
    restored_counts = _snapshot_table_counts(
        restore_compose_cmd, restore_dir, db_user, db_name, env=restore_env
    )
    assert restored_counts == orig_counts, (
        f"Restored table row counts mismatch!\n  original={orig_counts}\n  restored={restored_counts}"
    )
    restored_thread_row = _query_thread_row(
        restore_compose_cmd, restore_dir, db_user, db_name, sentinel["thread_id"], env=restore_env
    )
    assert restored_thread_row == orig_thread_row, (
        f"Restored known thread row mismatch: {restored_thread_row!r} != {orig_thread_row!r}"
    )

    ws_resp = _https_request(
        "GET", "localhost", restore_https_port, "/api/v1/workspaces", context=ssl_ctx
    )
    assert ws_resp.status == 200, f"Restored GET /workspaces failed: {ws_resp.status}"
    ws_payload = ws_resp.json()
    ws_list = ws_payload if isinstance(ws_payload, list) else ws_payload.get("workspaces", [])
    ws_ids = {w.get("workspace_id") for w in ws_list if isinstance(w, dict)}
    assert sentinel["workspace_id"] in ws_ids, (
        f"Restored /workspaces missing sentinel workspace {sentinel['workspace_id']!r}: {ws_ids}"
    )

    replay_resp = _https_request(
        "GET",
        "localhost",
        restore_https_port,
        f"/api/v1/threads/{sentinel['thread_id']}/messages/replay",
        context=ssl_ctx,
    )
    assert replay_resp.status == 200, (
        f"Restored thread replay failed: {replay_resp.status} {replay_resp.text[:300]}"
    )
    restored_replay_events = _parse_sse_bytes(replay_resp.body)
    assert "replay_done" in [e.event for e in restored_replay_events], (
        "Restored thread replay missing replay_done event"
    )
    restored_replay_text = _lib.extract_assistant_text(restored_replay_events).strip()
    assert restored_replay_text == sentinel["replay_text"], (
        f"Restored replay text mismatch!\n"
        f"  expected={sentinel['replay_text']!r}\n  got={restored_replay_text!r}"
    )

    restored_minio_count = _count_minio_objects(
        restore_compose_cmd, restore_dir, env=restore_env, bucket=sentinel["bucket"]
    )
    assert restored_minio_count == orig_minio_count, (
        f"Restored MinIO object count mismatch in {sentinel['bucket']!r}: "
        f"{restored_minio_count} != {orig_minio_count}"
    )
    got_sha, got_ct = _fetch_minio_object_sha_and_ct(
        restore_compose_cmd,
        restore_dir,
        sentinel["bucket"],
        sentinel["object_key"],
        env=restore_env,
    )
    assert got_sha == sentinel["payload_sha"], (
        f"Sentinel object SHA-256 mismatch: {got_sha!r} != {sentinel['payload_sha']!r}"
    )
    assert got_ct == "text/plain", (
        f"Sentinel object ContentType not preserved: got {got_ct!r}, expected 'text/plain'"
    )


def check_restore_on_clean_stack(
    ctx: _lib.ComposeContext,
    backup_dir: Path,
    passphrase_file: Path,
    sentinel: dict[str, str],
    orig_ca_pem: bytes,
) -> None:
    log("5/6 Verifying scripts/standalone/restore.sh (empty-instance restore, non-empty refusal, manual down -v + re-restore)...")
    orig_counts = _snapshot_table_counts(ctx.compose_cmd(), ctx.work_dir, ctx.db_user, ctx.db_name)
    orig_thread_row = _query_thread_row(
        ctx.compose_cmd(), ctx.work_dir, ctx.db_user, ctx.db_name, sentinel["thread_id"]
    )
    assert orig_thread_row, f"Known thread {sentinel['thread_id']!r} not found in source DB"
    orig_minio_count = _count_minio_objects(ctx.compose_cmd(), ctx.work_dir)
    orig_ca_sha = hashlib.sha256(orig_ca_pem).hexdigest()

    restore_project = f"dalton_b6_restore_{os.getpid()}"
    restore_edge_port = 18088
    restore_http_port = 18180
    restore_https_port = 18543

    with tempfile.TemporaryDirectory(prefix="b6-restore-stack-") as tmpdir:
        tmp_root = Path(tmpdir)
        restore_dir = tmp_root / "stack"
        restore_backup_dir = tmp_root / "backup-isolated"
        _prepare_clean_restore_dir(ctx.work_dir, restore_dir)
        assert not (restore_dir / ".env").exists(), "Restore target dir should start without .env"

        isolated_overrides = {
            "COMPOSE_PROJECT_NAME": restore_project,
            "SANDBOX_DOCKER_NETWORK": f"{restore_project}_sandbox",
            "DALTON_CORE_INTERNAL_SUBNET": "172.31.251.0/24",
            "DALTON_EDGE_PORT": str(restore_edge_port),
            "DALTON_EDGE_HTTP_PORT": str(restore_http_port),
            "DALTON_EDGE_HTTPS_PORT": str(restore_https_port),
            "DALTON_AUTH_MODE": "oss",
            "DALTON_ALLOW_DEV_OSS_AUTH": "1",
            "HOST_MODE": "oss",
        }
        _make_isolated_restore_backup(
            backup_dir,
            restore_backup_dir,
            ctx.work_dir / ".env",
            passphrase_file,
            isolated_overrides,
        )

        restore_compose_files = (
            f"{restore_dir / 'docker-compose.yml'}:"
            f"{restore_dir / 'docker-compose.core.yml'}:"
            f"{restore_dir / 'deploy' / 'standalone' / 'docker-compose.tls.yml'}"
        )
        # Environment for our direct `docker compose` assertions on the restored stack
        restore_env = os.environ.copy()
        restore_env.update(isolated_overrides)
        # Poisoned shell env passed to `restore.sh` to verify `env -u` isolates `.env` from shell exports
        restore_sh_env = os.environ.copy()
        restore_sh_env.update(
            {
                "COMPOSE_PROJECT_NAME": "wrong_shell_project_must_be_ignored",
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "wrong-shell-bucket-must-be-ignored",
                "DALTON_EDGE_HTTPS_PORT": "19999",
            }
        )
        restore_compose_cmd = [
            "docker",
            "compose",
            "-p",
            restore_project,
            "--project-directory",
            str(restore_dir),
            "-f",
            str(restore_dir / "docker-compose.yml"),
            "-f",
            str(restore_dir / "docker-compose.core.yml"),
            "-f",
            str(restore_dir / "deploy" / "standalone" / "docker-compose.tls.yml"),
        ]

        try:
            # 5a. Restore onto the brand-new empty stack (decrypting env.enc atomically);
            #     an in-progress 'uploading' record is present in the snapshot and MUST be
            #     skipped with the skipped count reported while restore still succeeds.
            restore_5a_proc = _run_cmd(
                [
                    "bash",
                    str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                    "--backup-dir",
                    str(restore_backup_dir),
                    "--work-dir",
                    str(restore_dir),
                    "--compose-files",
                    restore_compose_files,
                    "--env-passphrase-file",
                    str(passphrase_file),
                    "--skip-edge-build",
                ],
                cwd=restore_dir,
                env=restore_sh_env,
                redact_env=ctx.env,
                timeout=360,
            )
            restore_5a_stdout = restore_5a_proc.stdout or ""
            assert "skipped 1" in restore_5a_stdout.lower() and "uploading" in restore_5a_stdout.lower(), (
                f"Expected restore.sh to report skipping 1 'uploading' record, got stdout:\n{restore_5a_stdout[-1200:]}"
            )
            restored_env_file = restore_dir / ".env"
            assert restored_env_file.is_file(), "restore.sh did not write .env"
            assert (restored_env_file.stat().st_mode & 0o777) == 0o600, (
                f"Restored .env permissions should be 0600, got {oct(restored_env_file.stat().st_mode & 0o777)}"
            )
            assert not list(restore_dir.glob(".env.tmp-*")), (
                "restore.sh left behind temporary .env.tmp-* file(s)"
            )
            restored_bucket = _lib.load_env_file(restored_env_file).get(
                "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", ""
            )
            assert restored_bucket == sentinel["bucket"], (
                f"Expected restore.sh to preserve original bucket {sentinel['bucket']!r}, got {restored_bucket!r}"
            )
            assert _backend_active_bucket(restore_compose_cmd, restore_dir, env=restore_env) == sentinel["bucket"], (
                f"Running backend bucket does not match original bucket {sentinel['bucket']!r}"
            )

            # Confirm the restored postgres container is distinct from the source stack
            orig_pg_cid = _container_id(ctx, "postgres")
            restored_pg_cid = (
                subprocess.check_output(
                    [*restore_compose_cmd, "ps", "-q", "postgres"],
                    cwd=str(restore_dir),
                    env=restore_env,
                    text=True,
                    timeout=30,
                )
                .strip()
                .splitlines()[0]
            )
            assert restored_pg_cid and restored_pg_cid != orig_pg_cid, (
                f"Restored postgres container must be distinct from source ({restored_pg_cid} vs {orig_pg_cid})"
            )

            restored_ca_pem = _wait_for_caddy_internal_ca(
                restore_compose_cmd, restore_dir, env=restore_env, timeout=60.0
            )
            restored_ca_sha = hashlib.sha256(restored_ca_pem).hexdigest()
            assert restored_ca_sha == orig_ca_sha, (
                f"Restored Caddy CA root.crt SHA-256 mismatch: {restored_ca_sha} != {orig_ca_sha}"
            )

            with tempfile.NamedTemporaryFile(prefix="b6-restored-ca-", suffix=".crt", delete=False) as fp:
                fp.write(restored_ca_pem)
                restored_ca_path = Path(fp.name)
            try:
                ssl_ctx = ssl.create_default_context(cafile=str(restored_ca_path))
                ssl_ctx.check_hostname = True
                ssl_ctx.verify_mode = ssl.CERT_REQUIRED

                _wait_for_https_ready(
                    "localhost", restore_https_port, "/api/v1/auth/cli", context=ssl_ctx, timeout=90.0
                )

                _verify_restored_data(
                    restore_compose_cmd,
                    restore_dir,
                    restore_env,
                    restore_https_port,
                    ssl_ctx,
                    ctx.db_user,
                    ctx.db_name,
                    orig_counts,
                    orig_thread_row,
                    orig_minio_count,
                    sentinel,
                )
                log("5a OK (clean empty-instance restore verified: row counts, known thread replay, MinIO SHA-256, Caddy CA).")

                # 5b. Non-empty instance guard: re-running restore.sh MUST be refused,
                #     point to STANDALONE-DEPLOY.md (`down -v`), and leave the instance serving HTTPS 200.
                guard_proc = _run_cmd(
                    [
                        "bash",
                        str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                        "--backup-dir",
                        str(restore_backup_dir),
                        "--work-dir",
                        str(restore_dir),
                        "--compose-files",
                        restore_compose_files,
                        "--project-name",
                        restore_project,
                        "--skip-edge-build",
                    ],
                    cwd=restore_dir,
                    env=restore_sh_env,
                    check=False,
                    redact_env=ctx.env,
                    timeout=120,
                )
                assert guard_proc.returncode != 0, (
                    "restore.sh MUST refuse to restore into a non-empty instance!"
                )
                guard_stderr = guard_proc.stderr or ""
                assert "NOT empty" in guard_stderr, (
                    f"Non-empty refusal stderr must contain refusal reason, got:\n{guard_stderr[-800:]}"
                )
                assert "STANDALONE-DEPLOY.md" in guard_stderr and "down -v" in guard_stderr, (
                    f"Non-empty refusal stderr should point to manual steps in STANDALONE-DEPLOY.md (down -v), got:\n{guard_stderr[-800:]}"
                )
                still_live = _https_request(
                    "GET", "localhost", restore_https_port, "/api/v1/auth/cli", context=ssl_ctx
                )
                assert still_live.status == 200, (
                    f"Restored instance stopped serving after refused restore: HTTP {still_live.status}"
                )
                still_ws = _https_request(
                    "GET", "localhost", restore_https_port, "/api/v1/workspaces", context=ssl_ctx
                )
                assert still_ws.status == 200, (
                    f"Restored instance /workspaces failed after refused restore: HTTP {still_ws.status}"
                )
                log("5b OK (non-empty instance refused with manual link on stderr and remained live).")

                # 5b2. Explicit --project-name overrides both .env COMPOSE_PROJECT_NAME and shell env.
                saved_env_for_proj = restored_env_file.read_text(encoding="utf-8")
                restored_env_file.write_text(
                    _rewrite_env_keys(
                        saved_env_for_proj,
                        {"COMPOSE_PROJECT_NAME": "wrong_env_project_must_be_overridden"},
                    ),
                    encoding="utf-8",
                )
                os.chmod(restored_env_file, 0o600)
                try:
                    explicit_proj_proc = _run_cmd(
                        [
                            "bash",
                            str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                            "--backup-dir",
                            str(restore_backup_dir),
                            "--work-dir",
                            str(restore_dir),
                            "--compose-files",
                            restore_compose_files,
                            "--project-name",
                            restore_project,
                            "--skip-edge-build",
                        ],
                        cwd=restore_dir,
                        env=restore_sh_env,
                        check=False,
                        redact_env=ctx.env,
                        timeout=120,
                    )
                    assert explicit_proj_proc.returncode != 0, (
                        "restore.sh --project-name should target the running non-empty restore_project and refuse!"
                    )
                    ep_stderr = explicit_proj_proc.stderr or ""
                    assert "NOT empty" in ep_stderr, (
                        f"Expected --project-name to inspect non-empty {restore_project!r}, got:\n{ep_stderr[-800:]}"
                    )
                    assert f"label=com.docker.compose.project={restore_project}" in ep_stderr, (
                        f"Expected stderr to reference explicit project {restore_project!r}, got:\n{ep_stderr[-800:]}"
                    )
                    assert "wrong_env_project_must_be_overridden" not in ep_stderr, (
                        f"restore.sh used .env project instead of --project-name:\n{ep_stderr[-800:]}"
                    )
                    assert "wrong_shell_project_must_be_ignored" not in ep_stderr, (
                        f"restore.sh used shell project instead of --project-name:\n{ep_stderr[-800:]}"
                    )
                finally:
                    restored_env_file.write_text(saved_env_for_proj, encoding="utf-8")
                    os.chmod(restored_env_file, 0o600)
                log("5b2 OK (explicit --project-name overrides both .env and shell COMPOSE_PROJECT_NAME).")

                # 5c. Follow manual §6.2 steps: mutate live data, inspect full project volume list
                #     via `docker volume ls --filter label=com.docker.compose.project=...`, and run `down -v`.
                extra_ws = _https_request(
                    "POST",
                    "localhost",
                    restore_https_port,
                    "/api/v1/workspaces",
                    context=ssl_ctx,
                    json_body={"name": "b6-pre-down-v-ws"},
                )
                assert extra_ws.status == 201, f"Failed to create extra workspace before down -v: {extra_ws.status}"

                project_vols = _list_project_container_volumes(
                    restore_compose_cmd, restore_dir, env=restore_env
                )
                assert project_vols, "Expected project containers to report mounted Docker volumes"
                labeled_vols = sorted(
                    v.strip()
                    for v in subprocess.check_output(
                        [
                            "docker",
                            "volume",
                            "ls",
                            "-q",
                            "--filter",
                            f"label=com.docker.compose.project={restore_project}",
                        ],
                        text=True,
                        timeout=30,
                    ).splitlines()
                    if v.strip()
                )
                assert set(project_vols).issubset(set(labeled_vols)), (
                    f"Project volume label filter missed mounted volumes: mounted={project_vols}, labeled={labeled_vols}"
                )
                assert all(v.startswith(f"{restore_project}_") for v in labeled_vols), (
                    f"Unexpected volume names outside project {restore_project!r}: {labeled_vols}"
                )

                _run_cmd(
                    [*restore_compose_cmd, "--env-file", str(restored_env_file), "down", "-v"],
                    cwd=restore_dir,
                    env=restore_env,
                    redact_env=ctx.env,
                    timeout=120,
                )

                # 5d. Bucket name mismatch guard: changing the chat upload bucket in target .env
                #     must cause restore.sh to refuse BEFORE writing any data.
                saved_env_text = restored_env_file.read_text(encoding="utf-8")
                mismatched_env_text = _rewrite_env_keys(
                    saved_env_text,
                    {"DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME": "mismatched-bucket-b6-ci"},
                )
                restored_env_file.write_text(mismatched_env_text, encoding="utf-8")
                os.chmod(restored_env_file, 0o600)
                try:
                    bucket_mismatch_proc = _run_cmd(
                        [
                            "bash",
                            str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                            "--backup-dir",
                            str(restore_backup_dir),
                            "--work-dir",
                            str(restore_dir),
                            "--compose-files",
                            restore_compose_files,
                            "--project-name",
                            restore_project,
                            "--skip-edge-build",
                        ],
                        cwd=restore_dir,
                        env=restore_sh_env,
                        check=False,
                        redact_env=ctx.env,
                        timeout=120,
                    )
                    assert bucket_mismatch_proc.returncode != 0, (
                        "restore.sh MUST refuse when target .env bucket name mismatches backup manifest!"
                    )
                    bm_stderr = bucket_mismatch_proc.stderr or ""
                    assert "bucket" in bm_stderr.lower() and "--replace-env" in bm_stderr, (
                        f"Bucket mismatch refusal should explain mismatch and mention --replace-env, got:\n{bm_stderr[-800:]}"
                    )
                finally:
                    restored_env_file.write_text(saved_env_text, encoding="utf-8")
                    os.chmod(restored_env_file, 0o600)
                log("5d OK (bucket name mismatch in target .env refused before writing data).")

                # 5d2. Effective backend Compose config guard: keeping .env bucket matching the backup
                #      but adding an extra compose file that overrides `backend`'s bucket MUST be refused
                #      before writing any data (compose config --format json is authoritative).
                override_bucket = "overridden-backend-bucket-b6-ci"
                override_compose = tmp_root / "docker-compose.override-backend-bucket.yml"
                override_compose.write_text(
                    "services:\n"
                    "  backend:\n"
                    "    environment:\n"
                    f"      DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME: {override_bucket}\n",
                    encoding="utf-8",
                )
                override_proc = _run_cmd(
                    [
                        "bash",
                        str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                        "--backup-dir",
                        str(restore_backup_dir),
                        "--work-dir",
                        str(restore_dir),
                        "--compose-files",
                        f"{restore_compose_files}:{override_compose}",
                        "--project-name",
                        restore_project,
                        "--skip-edge-build",
                    ],
                    cwd=restore_dir,
                    env=restore_sh_env,
                    check=False,
                    redact_env=ctx.env,
                    timeout=120,
                )
                assert override_proc.returncode != 0, (
                    "restore.sh MUST refuse when an extra compose file overrides backend's bucket name!"
                )
                ov_stderr = override_proc.stderr or ""
                assert override_bucket in ov_stderr and "bucket" in ov_stderr.lower(), (
                    f"Compose-override bucket refusal should report {override_bucket!r}, got:\n{ov_stderr[-800:]}"
                )
                log("5d2 OK (extra compose file overriding backend bucket refused before writing data).")

                # 5e. Whole-instance PostgreSQL empty guard: when target DB_NAME is empty,
                #     another non-empty database in the same PostgreSQL instance MUST still
                #     cause restore.sh to refuse and leave that database untouched.
                _run_cmd(
                    [*restore_compose_cmd, "--env-file", str(restored_env_file), "up", "-d", "postgres"],
                    cwd=restore_dir,
                    env=restore_env,
                    redact_env=ctx.env,
                    timeout=120,
                )
                pg_ready_other = False
                for _ in range(45):
                    r_ready = subprocess.run(
                        [
                            *restore_compose_cmd,
                            "--env-file",
                            str(restored_env_file),
                            "exec",
                            "-T",
                            "postgres",
                            "pg_isready",
                            "-h",
                            "127.0.0.1",
                            "-p",
                            "5432",
                            "-U",
                            ctx.db_user,
                            "-d",
                            "template1",
                            "-q",
                        ],
                        cwd=str(restore_dir),
                        env=restore_env,
                        capture_output=True,
                        check=False,
                        timeout=15,
                    )
                    if r_ready.returncode == 0:
                        pg_ready_other = True
                        break
                    time.sleep(2)
                assert pg_ready_other, "Postgres in isolated restore stack did not become ready for other-DB guard check"

                other_db = "other_app_db"
                _run_cmd(
                    [
                        *restore_compose_cmd,
                        "--env-file",
                        str(restored_env_file),
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-U",
                        ctx.db_user,
                        "-d",
                        "template1",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-c",
                        f'CREATE DATABASE "{other_db}";',
                    ],
                    cwd=restore_dir,
                    env=restore_env,
                    redact_env=ctx.env,
                    timeout=30,
                )
                _run_cmd(
                    [
                        *restore_compose_cmd,
                        "--env-file",
                        str(restored_env_file),
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-U",
                        ctx.db_user,
                        "-d",
                        other_db,
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-c",
                        "CREATE TABLE keep_me(id int); INSERT INTO keep_me VALUES (42);",
                    ],
                    cwd=restore_dir,
                    env=restore_env,
                    redact_env=ctx.env,
                    timeout=30,
                )

                other_db_proc = _run_cmd(
                    [
                        "bash",
                        str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                        "--backup-dir",
                        str(restore_backup_dir),
                        "--work-dir",
                        str(restore_dir),
                        "--compose-files",
                        restore_compose_files,
                        "--project-name",
                        restore_project,
                        "--env-passphrase-file",
                        str(passphrase_file),
                        "--replace-env",
                        "--skip-edge-build",
                    ],
                    cwd=restore_dir,
                    env=restore_sh_env,
                    check=False,
                    redact_env=ctx.env,
                    timeout=120,
                )
                assert other_db_proc.returncode != 0, (
                    "restore.sh MUST refuse when another database in the same PostgreSQL instance has data!"
                )
                odb_stderr = other_db_proc.stderr or ""
                assert other_db in odb_stderr, (
                    f"Other-DB refusal stderr should name {other_db!r}, got:\n{odb_stderr[-800:]}"
                )
                assert ctx.db_name in odb_stderr and "backup.sh" in odb_stderr, (
                    f"Other-DB refusal stderr should state backup.sh only backs up {ctx.db_name!r}, got:\n{odb_stderr[-800:]}"
                )
                assert "STANDALONE-DEPLOY.md" in odb_stderr, (
                    f"Other-DB refusal stderr should reference STANDALONE-DEPLOY.md, got:\n{odb_stderr[-800:]}"
                )
                assert "down -v" not in odb_stderr, (
                    f"Other-DB refusal stderr MUST NOT suggest 'down -v' when other databases contain data, got:\n{odb_stderr[-800:]}"
                )
                kept_val = subprocess.check_output(
                    [
                        *restore_compose_cmd,
                        "--env-file",
                        str(restored_env_file),
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-U",
                        ctx.db_user,
                        "-d",
                        other_db,
                        "-Atqc",
                        "SELECT id FROM keep_me;",
                    ],
                    cwd=str(restore_dir),
                    env=restore_env,
                    text=True,
                    timeout=30,
                ).strip()
                assert kept_val == "42", f"Refused restore modified {other_db}: got {kept_val!r}"

                _run_cmd(
                    [*restore_compose_cmd, "--env-file", str(restored_env_file), "down", "-v"],
                    cwd=restore_dir,
                    env=restore_env,
                    redact_env=ctx.env,
                    timeout=120,
                )
                log("5e OK (another non-empty database in the same PostgreSQL instance refused restore and stayed intact).")

                # 5f. Re-run restore.sh (with --replace-env) cleanly into the empty instance.
                _run_cmd(
                    [
                        "bash",
                        str(restore_dir / "scripts" / "standalone" / "restore.sh"),
                        "--backup-dir",
                        str(restore_backup_dir),
                        "--work-dir",
                        str(restore_dir),
                        "--compose-files",
                        restore_compose_files,
                        "--project-name",
                        restore_project,
                        "--env-passphrase-file",
                        str(passphrase_file),
                        "--replace-env",
                        "--skip-edge-build",
                    ],
                    cwd=restore_dir,
                    env=restore_sh_env,
                    redact_env=ctx.env,
                    timeout=360,
                )

                restored_ca_pem_2 = _wait_for_caddy_internal_ca(
                    restore_compose_cmd, restore_dir, env=restore_env, timeout=60.0
                )
                assert hashlib.sha256(restored_ca_pem_2).hexdigest() == orig_ca_sha, (
                    "Caddy CA root.crt SHA-256 mismatch after manual down -v + restore!"
                )
                _wait_for_https_ready(
                    "localhost", restore_https_port, "/api/v1/auth/cli", context=ssl_ctx, timeout=90.0
                )
                assert _backend_active_bucket(restore_compose_cmd, restore_dir, env=restore_env) == sentinel["bucket"]
                _verify_restored_data(
                    restore_compose_cmd,
                    restore_dir,
                    restore_env,
                    restore_https_port,
                    ssl_ctx,
                    ctx.db_user,
                    ctx.db_name,
                    orig_counts,
                    orig_thread_row,
                    orig_minio_count,
                    sentinel,
                )
                log("5f OK (manual down -v followed by restore.sh succeeded with identical data).")
            finally:
                restored_ca_path.unlink(missing_ok=True)
        finally:
            subprocess.run(
                [*restore_compose_cmd, "down", "-v", "--remove-orphans"],
                cwd=str(restore_dir),
                env=restore_env,
                capture_output=True,
                check=False,
                timeout=120,
            )

    log(
        "5/6 OK (empty-instance restore verified: data consistency, non-empty refusal with live service, "
        "and manual down -v + re-restore)."
    )


def _build_same_version_release_tar(work_dir: Path, tar_path: Path) -> None:
    """Create a release tarball of ``work_dir`` excluding operator runtime state."""
    skip_top = {".env", "backups", ".upgrade-backup", ".restore-backup", "ci-artifacts", ".git"}

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        norm = info.name.lstrip("./")
        top = norm.split("/", 1)[0]
        if top in skip_top:
            return None
        return info

    with tarfile.open(tar_path, "w:gz") as tf:
        for entry in sorted(work_dir.iterdir()):
            if entry.name in skip_top:
                continue
            tf.add(entry, arcname=entry.name, filter=_filter)


def check_upgrade_same_version(
    ctx: _lib.ComposeContext,
    sentinel: dict[str, str],
    ca_pem: bytes,
    https_port: int,
) -> None:
    log("6/6 Verifying scripts/standalone/upgrade.sh (old-backup-first, pre-upgrade tags & snapshot, rolling upgrade)...")
    tls_context = _tls_ctx(ctx)
    compose_files_str = ":".join(str(p) for p in tls_context.compose_files)
    project_name = os.environ["COMPOSE_PROJECT_NAME"]
    assert project_name, "COMPOSE_PROJECT_NAME must be set by run_checks.sh"
    pre_backend_cid = _container_id(tls_context, "backend")
    pre_edge_cid = _container_id(tls_context, "edge")

    snapshot_root = ctx.work_dir / ".upgrade-backup"
    existing_snaps = set(snapshot_root.iterdir()) if snapshot_root.is_dir() else set()

    try:
        with tempfile.TemporaryDirectory(prefix="b6-upgrade-") as tmpdir:
            tmp_path = Path(tmpdir)
            release_tar = tmp_path / "dalton-core-same-version.tar.gz"
            upgrade_backups = tmp_path / "upgrade-backups"
            _build_same_version_release_tar(ctx.work_dir, release_tar)

            _run_cmd(
                [
                    "bash",
                    str(ctx.work_dir / "scripts" / "standalone" / "upgrade.sh"),
                    "--release-tar",
                    str(release_tar),
                    "--work-dir",
                    str(ctx.work_dir),
                    "--backup-dir",
                    str(upgrade_backups),
                    "--compose-files",
                    compose_files_str,
                    "--project-name",
                    project_name,
                    "--skip-build",
                ],
                cwd=ctx.work_dir,
                redact_env=ctx.env,
                timeout=360,
            )

            # Confirm pre-upgrade backup was created with the explicit project name
            pre_backups = sorted(p for p in upgrade_backups.glob("dalton-backup-*") if p.is_dir())
            assert len(pre_backups) == 1, f"Expected 1 pre-upgrade backup, got {pre_backups}"
            assert (pre_backups[0] / "postgres.dump").is_file()
            assert (pre_backups[0] / "manifest.json").is_file()
            pre_manifest = json.loads((pre_backups[0] / "manifest.json").read_text(encoding="utf-8"))
            assert pre_manifest.get("compose_project") == project_name, (
                f"Expected pre-upgrade backup compose_project={project_name!r}, "
                f"got {pre_manifest.get('compose_project')!r}"
            )

        # Confirm release snapshot directory & pre-upgrade-* image tags were created
        new_snaps = (set(snapshot_root.iterdir()) if snapshot_root.is_dir() else set()) - existing_snaps
        assert new_snaps, "Expected upgrade.sh to snapshot old release files under .upgrade-backup/<ts>/"

        tags_out = subprocess.check_output(
            ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
            text=True,
            timeout=30,
        )
        assert ":pre-upgrade-" in tags_out, "Expected pre-upgrade-<ts> image tags to be created"

        post_backend_cid = _container_id(tls_context, "backend")
        post_edge_cid = _container_id(tls_context, "edge")
        assert post_backend_cid != pre_backend_cid, (
            f"Expected backend container to be replaced during upgrade (still {post_backend_cid})"
        )
        assert post_edge_cid != pre_edge_cid, (
            f"Expected edge container to be replaced during upgrade (still {post_edge_cid})"
        )

        with tempfile.NamedTemporaryFile(prefix="b6-upg-ca-", suffix=".crt", delete=False) as fp:
            fp.write(ca_pem)
            ca_path = Path(fp.name)
        try:
            ssl_ctx = ssl.create_default_context(cafile=str(ca_path))
            ssl_ctx.check_hostname = True
            ssl_ctx.verify_mode = ssl.CERT_REQUIRED

            _wait_for_https_ready("localhost", https_port, "/__probe/health", context=ssl_ctx, timeout=90.0)
            me_resp = _https_request("GET", "localhost", https_port, "/api/v1/users/me", context=ssl_ctx)
            assert me_resp.status == 200 and me_resp.json()["user"]["user_id"] == LOCAL_DEV_USER, (
                f"Post-upgrade /users/me check failed: {me_resp.status} {me_resp.text}"
            )
            replay_resp = _https_request(
                "GET",
                "localhost",
                https_port,
                f"/api/v1/threads/{sentinel['thread_id']}/messages/replay",
                context=ssl_ctx,
            )
            assert replay_resp.status == 200, (
                f"Post-upgrade known thread replay failed: {replay_resp.status}"
            )
            post_events = _parse_sse_bytes(replay_resp.body)
            assert _lib.extract_assistant_text(post_events).strip() == sentinel["replay_text"], (
                "Post-upgrade replay text mismatch!"
            )
        finally:
            ca_path.unlink(missing_ok=True)
    finally:
        # Clean up CI-created .upgrade-backup snapshots
        if snapshot_root.is_dir():
            for snap in list(snapshot_root.iterdir()):
                if snap not in existing_snaps:
                    shutil.rmtree(snap, ignore_errors=True)
            try:
                if not any(snapshot_root.iterdir()):
                    snapshot_root.rmdir()
            except OSError:
                pass

    log("6/6 OK (same-version upgrade backed up first, tagged & snapshotted, replaced services, and preserved replay).")


def main() -> int:
    t0 = time.monotonic()
    ctx = _lib.ComposeContext.from_environment()
    env_file = ctx.work_dir / ".env"
    original_env_bytes = env_file.read_bytes()

    check_init_tls_modes(ctx)
    check_loopback_ports_caddy_adapt_and_custom_tls(ctx)
    sentinel = seed_sentinel_data(ctx)

    try:
        ca_pem, _http_port, https_port = check_internal_ca_https_and_boundaries(ctx)

        with tempfile.TemporaryDirectory(prefix="b6-backups-") as btmp:
            btmp_path = Path(btmp)
            backups_root = btmp_path / "backups"
            passphrase_file = btmp_path / "backup.pass"
            passphrase_file.write_text("b6-ci-ephemeral-passphrase-0123456789\n", encoding="utf-8")
            os.chmod(passphrase_file, 0o600)

            latest_backup = check_backup_script(ctx, backups_root, passphrase_file, ca_pem)
            check_restore_on_clean_stack(ctx, latest_backup, passphrase_file, sentinel, ca_pem)

        check_upgrade_same_version(ctx, sentinel, ca_pem, https_port)
    finally:
        # Restore primary stack .env & edge back to loopback HTTP mode for clean handoff
        env_file.write_bytes(original_env_bytes)
        os.chmod(env_file, 0o600)
        ctx.compose("up", "-d", "--no-deps", "--force-recreate", "edge", check=False)
        try:
            _lib.wait_for_http_ready(f"{ctx.edge_url}/__probe/health", timeout=60.0)
        except TimeoutError:
            log("WARN: edge did not return to loopback readiness during teardown cleanup.")

    elapsed = time.monotonic() - t0
    log(f"ALL BATCH 6 CHECKS PASSED in {elapsed:.1f}s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
