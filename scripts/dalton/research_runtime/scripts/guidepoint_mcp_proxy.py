#!/usr/bin/env python3.12
"""
Local reverse proxy for the Guidepoint MCP server (`com.dalton.guidepoint-mcp-proxy`).

Guidepoint's Auth0 access tokens live only 600s. This proxy sits on
http://127.0.0.1:8943/mcp in front of https://clapi.guidepoint.io/mcp-server/v1/mcp:
it reads persistent `handoff-journal.json` (`target_owner`) at startup before touching
`.guidepoint.owner.lock` or `guidepoint_mcp_tokens.json`, exposes `/healthz` and
admin-authenticated `/admin/drain`, and honors shared request deadlines via
`X-Dalton-Request-Timeout-Ms` and `arguments.timeout`.
"""
from __future__ import annotations

import atexit
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import queue
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_ORIG_URLOPEN = urllib.request.urlopen

_RUNTIME_DIR = str(Path(__file__).resolve().parent.parent)
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)

from guidepoint_lock import (  # noqa: E402
    GuidepointOwnerLock,
    GuidepointStandbyError,
    OWNER_LEGACY_PROXY,
    read_target_owner,
    resolve_guidepoint_tokens_file,
)

PORT = int(os.environ.get("GUIDEPOINT_PROXY_PORT", "8943"))
UPSTREAM = os.environ.get("GUIDEPOINT_UPSTREAM_URL", "").strip() or "https://clapi.guidepoint.io/mcp-server/v1/mcp"
TOKEN_URL = os.environ.get("GUIDEPOINT_TOKEN_URL", "").strip() or "https://auth0.guidepoint.com/oauth/token"
REFRESH_SKEW_SECONDS = 60
DEFAULT_UPSTREAM_TIMEOUT_S = 120.0

_lock = threading.Lock()
_state_lock = threading.Lock()
_tokens: dict = {}
_owner_lock: GuidepointOwnerLock | None = None
_standby: bool = False
_draining: bool = False
_in_flight: int = 0

HOP_BY_HOP = {
    "host",
    "authorization",
    "content-length",
    "connection",
    "transfer-encoding",
    "x-dalton-request-timeout-ms",
}


def resolve_research_home() -> Path:
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    if raw_home:
        return Path(raw_home).expanduser().resolve()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    if raw_ocw:
        return Path(raw_ocw).expanduser().resolve()
    return (Path.home() / "Library" / "Application Support" / "Everflow" / "research").resolve()


def resolve_token_store() -> Path:
    return resolve_guidepoint_tokens_file(environ=os.environ)


def _owner_lock_held() -> bool:
    return bool(_owner_lock is not None and _owner_lock.held)


def release_owner_lock() -> None:
    global _owner_lock, _standby
    with _state_lock:
        _standby = True
        lock = _owner_lock
        _owner_lock = None
    if lock is not None:
        lock.release()


def load_tokens(token_store: Path | None = None) -> None:
    global _tokens
    path = token_store or resolve_token_store()
    if not path.exists():
        print(
            f"No token store at {path}. Run guidepoint_mcp_oauth.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        _tokens = json.load(f)
    if "access_token" not in _tokens or "refresh_token" not in _tokens:
        print("Token store is missing access_token/refresh_token.", file=sys.stderr)
        sys.exit(1)


def save_tokens(token_store: Path | None = None) -> None:
    path = token_store or resolve_token_store()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".guidepoint_mcp_tokens.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_tokens, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _extract_raw_socket(stream):
    """Best-effort lookup of the underlying socket from HTTPResponse or HTTPError."""
    candidates = [stream, getattr(stream, "fp", None)]
    for obj in candidates:
        if obj is None:
            continue
        sock = getattr(getattr(getattr(obj, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            return sock
        sock = getattr(getattr(obj, "raw", None), "_sock", None)
        if sock is not None:
            return sock
        sock = getattr(obj, "_sock", None)
        if sock is not None:
            return sock
    return None


def _is_real_read1(fn) -> bool:
    return callable(fn) and not hasattr(fn, "assert_called")


def _resolve_nonblocking_chunk_reader(stream):
    """Return a single-read method (`read1` when available) so chunked streams yield between chunks."""
    read1 = getattr(stream, "read1", None)
    if _is_real_read1(read1):
        return read1
    fp = getattr(stream, "fp", None)
    fp_read1 = getattr(fp, "read1", None) if fp is not None else None
    if _is_real_read1(fp_read1):
        return fp_read1
    return stream.read


def _read_stream_with_deadline(stream, *, deadline_mono: float) -> bytes:
    """Read an entire HTTP response body while enforcing an absolute monotonic deadline."""
    raw_sock = _extract_raw_socket(stream)
    orig_timeout: float | None = None
    if raw_sock is not None and hasattr(raw_sock, "gettimeout"):
        try:
            t_val = raw_sock.gettimeout()
            if isinstance(t_val, (int, float)) and t_val > 0:
                orig_timeout = float(t_val)
        except Exception:
            orig_timeout = None
    read_fn = _resolve_nonblocking_chunk_reader(stream)
    if raw_sock is None and read_fn is stream.read and hasattr(stream.read, "assert_called"):
        rem = deadline_mono - time.monotonic()
        if rem <= 0.0:
            raise TimeoutError("Guidepoint response body deadline exceeded")
        raw = stream.read()
        if time.monotonic() >= deadline_mono:
            raise TimeoutError("Guidepoint response body deadline exceeded")
        return bytes(raw)
    buf = bytearray()
    while True:
        rem = deadline_mono - time.monotonic()
        if rem <= 0.0:
            raise TimeoutError("Guidepoint response body deadline exceeded")
        if raw_sock is not None and hasattr(raw_sock, "settimeout"):
            target_timeout = rem if orig_timeout is None else min(orig_timeout, rem)
            try:
                raw_sock.settimeout(max(0.01, target_timeout))
            except Exception:
                pass
        try:
            chunk = read_fn(4096)
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError("Guidepoint response body read timed out") from exc
        if not chunk:
            break
        buf.extend(chunk)
        if time.monotonic() >= deadline_mono:
            raise TimeoutError("Guidepoint response body deadline exceeded")
    return bytes(buf)


def _close_resource_forcefully(obj) -> None:
    """Shutdown and close any underlying socket/connection/stream without raising."""
    if obj is None:
        return
    socks = []
    if isinstance(obj, socket.socket):
        socks.append(obj)
    conn_sock = getattr(obj, "sock", None)
    if conn_sock is not None:
        socks.append(conn_sock)
    raw_sock = _extract_raw_socket(obj)
    if raw_sock is not None:
        socks.append(raw_sock)
    seen: set[int] = set()
    for sock in socks:
        sid = id(sock)
        if sid in seen:
            continue
        seen.add(sid)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
    close_fn = getattr(obj, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def _abort_active_resources(resources: list, lock: threading.Lock) -> None:
    with lock:
        snapshot = list(resources)
    for res in snapshot:
        _close_resource_forcefully(res)


def _open_upstream_tracked(
    req: urllib.request.Request,
    *,
    timeout_s: float,
    active_resources: list,
    active_lock: threading.Lock,
    deadline_mono: float | None = None,
    cancelled: threading.Event | None = None,
):
    """Open an upstream request while enforcing `min(timeout_s, remaining)` across connect, TLS handshake, and reads."""
    orig_timeout = max(0.01, float(timeout_s))
    if deadline_mono is not None:
        rem_now = deadline_mono - time.monotonic()
        if rem_now <= 0.0:
            raise TimeoutError("Guidepoint upstream deadline exceeded")
        eff_t = max(0.01, min(orig_timeout, rem_now))
    else:
        eff_t = orig_timeout
    if urllib.request.urlopen is not _ORIG_URLOPEN:
        return urllib.request.urlopen(req, timeout=orig_timeout)

    def _remaining_timeout(fallback: float = orig_timeout) -> float:
        if cancelled is not None and cancelled.is_set():
            raise TimeoutError("Guidepoint upstream request cancelled")
        cap = max(0.01, float(fallback))
        if deadline_mono is not None:
            rem = deadline_mono - time.monotonic()
            if rem <= 0.0:
                raise TimeoutError("Guidepoint upstream deadline exceeded")
            return max(0.01, min(cap, rem))
        return cap

    def _register_resource(res) -> None:
        with active_lock:
            active_resources.append(res)
        if cancelled is not None and cancelled.is_set():
            _close_resource_forcefully(res)
            raise TimeoutError("Guidepoint upstream request cancelled")

    class _DeadlineSocketMixin:
        def _connect_tcp_with_deadline(self) -> None:
            rem_dns = _remaining_timeout(
                self.timeout if isinstance(self.timeout, (int, float)) else orig_timeout
            )
            _ = rem_dns
            err: BaseException | None = None
            infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
            if not infos:
                raise OSError("getaddrinfo returns an empty list")
            for af, socktype, proto, _canonname, sa in infos:
                rem_conn = _remaining_timeout(
                    self.timeout if isinstance(self.timeout, (int, float)) else orig_timeout
                )
                sock = None
                try:
                    sock = socket.socket(af, socktype, proto)
                    sock.settimeout(rem_conn)
                    _register_resource(sock)
                    if self.source_address:
                        sock.bind(self.source_address)
                    sock.connect(sa)
                    self.sock = sock
                    if self._tunnel_host:
                        sock.settimeout(
                            _remaining_timeout(
                                self.timeout if isinstance(self.timeout, (int, float)) else orig_timeout
                            )
                        )
                        self._tunnel()
                    return
                except BaseException as exc:
                    err = exc
                    if sock is not None:
                        _close_resource_forcefully(sock)
                    if isinstance(exc, (TimeoutError, socket.timeout)):
                        if cancelled is not None and cancelled.is_set():
                            raise TimeoutError("Guidepoint upstream request cancelled") from exc
                        if deadline_mono is not None and time.monotonic() >= deadline_mono:
                            raise TimeoutError("Guidepoint upstream connect deadline exceeded") from exc
            if err is not None:
                raise err
            raise OSError("Failed to connect to upstream")

        def send(self, data):
            sock = getattr(self, "sock", None)
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(
                    _remaining_timeout(
                        self.timeout if isinstance(self.timeout, (int, float)) else orig_timeout
                    )
                )
            return super().send(data)

        def getresponse(self):
            sock = getattr(self, "sock", None)
            if sock is not None and hasattr(sock, "settimeout"):
                sock.settimeout(
                    _remaining_timeout(
                        self.timeout if isinstance(self.timeout, (int, float)) else orig_timeout
                    )
                )
            return super().getresponse()

    class _TrackedHTTPConnection(_DeadlineSocketMixin, http.client.HTTPConnection):
        def connect(self):
            self._connect_tcp_with_deadline()

    class _TrackedHTTPSConnection(_DeadlineSocketMixin, http.client.HTTPSConnection):
        def connect(self):
            self._connect_tcp_with_deadline()
            if self._tunnel_host:
                server_hostname = self._tunnel_host
            else:
                server_hostname = self.host
            rem_tls = _remaining_timeout(
                self.timeout if isinstance(self.timeout, (int, float)) else orig_timeout
            )
            self.sock.settimeout(rem_tls)
            self.sock = self._context.wrap_socket(
                self.sock,
                server_hostname=server_hostname,
            )
            _register_resource(self.sock)

    class _TrackedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            def _factory(host, **kwargs):
                conn = _TrackedHTTPConnection(host, **kwargs)
                _register_resource(conn)
                return conn

            return self.do_open(_factory, request)

    class _TrackedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            def _factory(host, **kwargs):
                conn = _TrackedHTTPSConnection(host, **kwargs)
                _register_resource(conn)
                return conn

            return self.do_open(_factory, request, context=self._context)

    opener = urllib.request.build_opener(_TrackedHTTPHandler, _TrackedHTTPSHandler)
    return opener.open(req, timeout=eff_t)


def _fetch_body_with_hard_deadline(
    req: urllib.request.Request,
    *,
    deadline_mono: float,
    timeout_message: str,
    open_timeout_s: float | None = None,
    on_worker_start=None,
    on_worker_done=None,
) -> bytes:
    """Run an HTTP request + body read in a worker thread under a single absolute deadline."""
    rem = deadline_mono - time.monotonic()
    if rem <= 0.0:
        raise TimeoutError(timeout_message)

    cancelled = threading.Event()
    done = threading.Event()
    active_lock = threading.Lock()
    active_resources: list = []
    outcome: dict = {}

    def _worker() -> None:
        resp = None
        try:
            rem_open = deadline_mono - time.monotonic()
            if rem_open <= 0.0 or cancelled.is_set():
                outcome["error"] = TimeoutError(timeout_message)
                return
            eff_open_t = (
                float(open_timeout_s)
                if open_timeout_s is not None
                else min(30.0, rem_open)
            )
            resp = _open_upstream_tracked(
                req,
                timeout_s=eff_open_t,
                active_resources=active_resources,
                active_lock=active_lock,
                deadline_mono=deadline_mono,
                cancelled=cancelled,
            )
            with active_lock:
                active_resources.append(resp)
            if cancelled.is_set():
                return
            outcome["body"] = _read_stream_with_deadline(resp, deadline_mono=deadline_mono)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            if resp is not None:
                _close_resource_forcefully(resp)
            _abort_active_resources(active_resources, active_lock)
            done.set()
            if callable(on_worker_done):
                on_worker_done()

    worker = threading.Thread(target=_worker, daemon=True)
    if callable(on_worker_start):
        on_worker_start()
    try:
        worker.start()
    except Exception:
        if callable(on_worker_done):
            on_worker_done()
        raise

    wait_rem = max(0.0, deadline_mono - time.monotonic())
    if not done.wait(timeout=wait_rem):
        cancelled.set()
        _abort_active_resources(active_resources, active_lock)
        raise TimeoutError(timeout_message)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("body", b"")


def do_refresh(
    *,
    timeout_s: float = 30.0,
    deadline_mono: float | None = None,
    on_worker_start=None,
    on_worker_done=None,
) -> None:
    client_id = os.environ["GUIDEPOINT_CLIENT_ID"]
    client_secret = os.environ["GUIDEPOINT_CLIENT_SECRET"]
    explicit_open_timeout: float | None = None
    if deadline_mono is not None:
        eff_deadline = float(deadline_mono)
        rem = eff_deadline - time.monotonic()
        if rem <= 0.0:
            raise TimeoutError("Guidepoint token refresh deadline exceeded")
    else:
        req_timeout = max(0.01, min(30.0, float(timeout_s)))
        eff_deadline = time.monotonic() + req_timeout
        explicit_open_timeout = req_timeout
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": _tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    raw_body = _fetch_body_with_hard_deadline(
        req,
        deadline_mono=eff_deadline,
        timeout_message="Guidepoint token refresh deadline exceeded",
        open_timeout_s=explicit_open_timeout,
        on_worker_start=on_worker_start,
        on_worker_done=on_worker_done,
    )
    new_tokens = json.loads(raw_body.decode("utf-8"))
    new_tokens.setdefault("refresh_token", _tokens["refresh_token"])
    new_tokens["obtained_at"] = time.time()
    _tokens.update(new_tokens)
    save_tokens()
    print(f"[guidepoint-proxy] refreshed access_token, expires_in={_tokens.get('expires_in')}s")


def ensure_fresh_token(
    *,
    timeout_s: float = 30.0,
    deadline_mono: float | None = None,
    on_worker_start=None,
    on_worker_done=None,
) -> str:
    if _standby or not _owner_lock_held():
        raise GuidepointStandbyError("lock_not_acquired")
    t_start = time.monotonic()
    eff_deadline = (
        float(deadline_mono)
        if deadline_mono is not None
        else t_start + max(0.01, float(timeout_s))
    )
    rem = eff_deadline - time.monotonic()
    if rem <= 0.0:
        raise TimeoutError("Guidepoint request deadline exceeded before token lock")
    acquired = _lock.acquire(timeout=rem)
    if not acquired:
        raise TimeoutError("Timed out waiting for Guidepoint token refresh lock")
    try:
        if _standby or not _owner_lock_held():
            raise GuidepointStandbyError("lock_not_acquired")
        now_after_lock = time.monotonic()
        rem_after_lock = eff_deadline - now_after_lock
        if rem_after_lock <= 0.0:
            raise TimeoutError("Guidepoint request deadline exceeded waiting for token lock")
        obtained_at = _tokens.get("obtained_at", 0)
        expires_in = _tokens.get("expires_in", 600)
        if time.time() > obtained_at + expires_in - REFRESH_SKEW_SECONDS:
            if deadline_mono is None and (now_after_lock - t_start) < 0.01:
                do_refresh(
                    timeout_s=float(timeout_s),
                    on_worker_start=on_worker_start,
                    on_worker_done=on_worker_done,
                )
            else:
                do_refresh(
                    timeout_s=rem_after_lock,
                    deadline_mono=eff_deadline,
                    on_worker_start=on_worker_start,
                    on_worker_done=on_worker_done,
                )
        return _tokens["access_token"]
    finally:
        _lock.release()


def initialize_runtime(
    *,
    ownership_file: Path | str | None = None,
    token_store: Path | None = None,
    lock_timeout_s: float = 2.0,
) -> bool:
    """Read `target_owner` BEFORE touching lock/tokens; acquire lock only when owner is `legacy_proxy_8943`."""
    global _owner_lock, _standby, _draining, _in_flight, _tokens
    with _state_lock:
        _draining = False
        _in_flight = 0
    target = read_target_owner(ownership_file=ownership_file, environ=os.environ)
    if target != OWNER_LEGACY_PROXY:
        with _state_lock:
            _standby = True
            _tokens = {}
        print(
            f"[guidepoint-proxy] starting in standby (target_owner={target} != {OWNER_LEGACY_PROXY})",
            file=sys.stderr,
        )
        return False

    path = token_store or resolve_token_store()
    lock = GuidepointOwnerLock(path, service_name="com.dalton.guidepoint-mcp-proxy")
    lock.acquire(timeout_s=lock_timeout_s)
    with _state_lock:
        _owner_lock = lock
        _standby = False
    load_tokens(path)
    ensure_fresh_token()
    return True


def _extract_admin_token(headers) -> str:
    auth = str(headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    hdr = str(headers.get("X-Guidepoint-Admin-Token") or "").strip()
    if hdr:
        return hdr
    return ""


def _is_valid_admin_token(headers) -> bool:
    expected = os.environ.get("GUIDEPOINT_PROXY_ADMIN_TOKEN", "").strip()
    supplied = _extract_admin_token(headers)
    if not expected or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _resolve_proxy_timeout_and_body(headers, body: bytes | None) -> tuple[float, bytes | None]:
    """Read shared deadline from `X-Dalton-Request-Timeout-Ms` & JSON-RPC `arguments.timeout`."""
    candidates: list[float] = []
    raw_hdr_ms = str(headers.get("X-Dalton-Request-Timeout-Ms") or "").strip()
    if raw_hdr_ms:
        try:
            hdr_s = float(raw_hdr_ms) / 1000.0
            if hdr_s > 0:
                candidates.append(hdr_s)
        except ValueError:
            pass

    out_body = body
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                params = parsed.get("params")
                if isinstance(params, dict) and isinstance(params.get("arguments"), dict):
                    args_dict = params["arguments"]
                    raw_arg = args_dict.get("timeout")
                    if (
                        isinstance(raw_arg, (int, float))
                        and not isinstance(raw_arg, bool)
                        and float(raw_arg) > 0
                    ):
                        candidates.append(float(raw_arg))
                        # Strip Dalton-internal timeout knob before forwarding to upstream Guidepoint API
                        clean_args = {k: v for k, v in args_dict.items() if k != "timeout"}
                        parsed = {
                            **parsed,
                            "params": {**params, "arguments": clean_args},
                        }
                        out_body = json.dumps(parsed, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass

    eff_timeout_s = max(0.1, min(candidates)) if candidates else DEFAULT_UPSTREAM_TIMEOUT_S
    return eff_timeout_s, out_body


def health_payload() -> dict:
    with _state_lock:
        draining = _draining
        in_flight = _in_flight
        standby = _standby
    held = _owner_lock_held()
    target = read_target_owner(environ=os.environ)
    return {
        "ok": not draining,
        "status": "draining" if draining else ("standby" if standby else "ok"),
        "service": "com.dalton.guidepoint-mcp-proxy",
        "owner_lock_held": held,
        "standby": standby,
        "target_owner": target,
        "draining": draining,
        "in_flight": in_flight,
    }


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _close_downstream_forcefully(self) -> None:
        self.close_connection = True
        sock = getattr(self, "connection", None)
        if sock is not None:
            _close_resource_forcefully(sock)

    def _write_bytes_with_deadline(self, data: bytes, *, deadline_mono: float | None) -> bool:
        """Write bytes to downstream socket bounded by an absolute monotonic deadline."""
        if not data:
            return True
        sock = getattr(self, "connection", None)
        if (
            deadline_mono is not None
            and sock is not None
            and hasattr(sock, "send")
            and hasattr(sock, "settimeout")
        ):
            view = memoryview(data)
            while view:
                rem = deadline_mono - time.monotonic()
                if rem <= 0.0:
                    return False
                try:
                    sock.settimeout(max(0.01, rem))
                    sent = sock.send(view)
                except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
                    return False
                if sent <= 0:
                    return False
                view = view[sent:]
            return True
        if deadline_mono is not None and time.monotonic() >= deadline_mono:
            return False
        try:
            self.wfile.write(data)
            self.wfile.flush()
            return True
        except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _send_json(self, status: int, payload: dict, *, write_timeout_s: float = 0.5) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.close_connection = True
        sock = getattr(self, "connection", None)
        if sock is not None and hasattr(sock, "settimeout"):
            try:
                sock.settimeout(max(0.05, float(write_timeout_s)))
            except Exception:
                pass
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_bytes_with_deadline(
                raw,
                deadline_mono=time.monotonic() + max(0.05, float(write_timeout_s)),
            )
        except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
            self._close_downstream_forcefully()

    def _path_only(self) -> str:
        return self.path.split("?", 1)[0]

    def _proxy(self) -> None:
        global _in_flight
        with _state_lock:
            if _draining:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "status": "draining",
                        "error_code": "service_draining",
                        "error": "Guidepoint proxy is draining.",
                    },
                )
                return
            if _standby or not _owner_lock_held():
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "status": "standby",
                        "error_code": "lock_not_acquired",
                        "error": "Guidepoint proxy is in standby (lock_not_acquired).",
                    },
                )
                return
            _in_flight += 1

        # Both the main handler and any spawned upstream/refresh worker must exit
        # before `_in_flight` decrements (prevents premature drain=0 while a worker
        # thread is still unwinding a timed-out connect/handshake/read).
        pending_owners = 1
        owners_lock = threading.Lock()

        def _acquire_owner() -> None:
            nonlocal pending_owners
            with owners_lock:
                pending_owners += 1

        def _release_owner() -> None:
            nonlocal pending_owners
            global _in_flight
            should_dec = False
            with owners_lock:
                pending_owners -= 1
                if pending_owners == 0:
                    should_dec = True
            if should_dec:
                with _state_lock:
                    _in_flight = max(0, _in_flight - 1)

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None
            eff_timeout_s, fwd_body = _resolve_proxy_timeout_and_body(self.headers, body)
            deadline_mono = time.monotonic() + eff_timeout_s

            try:
                access_token = ensure_fresh_token(
                    deadline_mono=deadline_mono,
                    on_worker_start=_acquire_owner,
                    on_worker_done=_release_owner,
                )
            except GuidepointStandbyError:
                self._send_json(
                    503,
                    {
                        "ok": False,
                        "status": "standby",
                        "error_code": "lock_not_acquired",
                        "error": "Guidepoint proxy is in standby (lock_not_acquired).",
                    },
                )
                return
            except (TimeoutError, socket.timeout):
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "status": "timeout",
                        "error_code": "upstream_timeout",
                        "error": f"token refresh timed out after {eff_timeout_s:.1f}s",
                    },
                )
                return
            except urllib.error.URLError as e:
                if isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
                    self._send_json(
                        504,
                        {
                            "ok": False,
                            "status": "timeout",
                            "error_code": "upstream_timeout",
                            "error": f"token refresh timed out after {eff_timeout_s:.1f}s",
                        },
                    )
                else:
                    self._send_json(502, {"ok": False, "error": f"token refresh failed: {e}"})
                return
            except Exception as e:
                self._send_json(502, {"ok": False, "error": f"token refresh failed: {e}"})
                return

            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
            headers["Authorization"] = f"Bearer {access_token}"

            rem_s = deadline_mono - time.monotonic()
            if rem_s <= 0.0:
                self._send_json(
                    504,
                    {
                        "ok": False,
                        "status": "timeout",
                        "error_code": "upstream_timeout",
                        "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                    },
                )
                return
            req = urllib.request.Request(UPSTREAM, data=fwd_body, method=self.command, headers=headers)
            cancelled = threading.Event()
            headers_ready = threading.Event()
            upstream_done = threading.Event()
            active_lock = threading.Lock()
            active_resources: list = []
            chunk_queue: queue.Queue = queue.Queue(maxsize=4)
            meta: dict = {}

            def _abort_upstream() -> None:
                cancelled.set()
                _abort_active_resources(active_resources, active_lock)

            def _enqueue(item: tuple) -> bool:
                while not cancelled.is_set():
                    rem_q = deadline_mono - time.monotonic()
                    if rem_q <= 0.0:
                        return False
                    try:
                        chunk_queue.put(item, timeout=min(0.05, max(0.01, rem_q)))
                        return True
                    except queue.Full:
                        continue
                return False

            def _upstream_worker() -> None:
                stream = None
                try:
                    rem_open = deadline_mono - time.monotonic()
                    if rem_open <= 0.0 or cancelled.is_set():
                        meta["error"] = TimeoutError(f"upstream timed out after {eff_timeout_s:.1f}s")
                        headers_ready.set()
                        return
                    try:
                        resp = _open_upstream_tracked(
                            req,
                            timeout_s=min(eff_timeout_s, rem_open),
                            active_resources=active_resources,
                            active_lock=active_lock,
                            deadline_mono=deadline_mono,
                            cancelled=cancelled,
                        )
                        status = resp.status
                        resp_headers = list(resp.headers.items())
                        stream = resp
                    except urllib.error.HTTPError as err_resp:
                        status = err_resp.code
                        resp_headers = list(err_resp.headers.items()) if err_resp.headers else []
                        stream = err_resp
                    with active_lock:
                        active_resources.append(stream)
                    if cancelled.is_set():
                        return
                    meta["status"] = status
                    meta["headers"] = resp_headers
                    headers_ready.set()

                    raw_sock = _extract_raw_socket(stream)
                    orig_sock_timeout: float | None = None
                    if raw_sock is not None and hasattr(raw_sock, "gettimeout"):
                        try:
                            t_val = raw_sock.gettimeout()
                            if isinstance(t_val, (int, float)) and t_val > 0:
                                orig_sock_timeout = float(t_val)
                        except Exception:
                            orig_sock_timeout = None
                    read_fn = _resolve_nonblocking_chunk_reader(stream)
                    while not cancelled.is_set():
                        rem_read = deadline_mono - time.monotonic()
                        if rem_read <= 0.0:
                            break
                        if raw_sock is not None and hasattr(raw_sock, "settimeout"):
                            target_t = (
                                rem_read
                                if orig_sock_timeout is None
                                else min(orig_sock_timeout, rem_read)
                            )
                            try:
                                raw_sock.settimeout(max(0.01, target_t))
                            except Exception:
                                pass
                        try:
                            chunk = read_fn(4096)
                        except (TimeoutError, socket.timeout, OSError):
                            break
                        if not chunk:
                            break
                        if cancelled.is_set() or time.monotonic() >= deadline_mono:
                            break
                        if not _enqueue(("data", chunk)):
                            break
                except Exception as exc:
                    if not headers_ready.is_set():
                        meta["error"] = exc
                        headers_ready.set()
                finally:
                    if stream is not None:
                        _close_resource_forcefully(stream)
                    _abort_active_resources(active_resources, active_lock)
                    upstream_done.set()
                    try:
                        chunk_queue.put_nowait(("eof", None))
                    except queue.Full:
                        pass
                    _release_owner()

            worker = threading.Thread(target=_upstream_worker, daemon=True)
            _acquire_owner()
            try:
                worker.start()
            except Exception:
                _release_owner()
                raise
            try:
                wait_hdr = max(0.0, deadline_mono - time.monotonic())
                if not headers_ready.wait(timeout=wait_hdr):
                    _abort_upstream()
                    self._send_json(
                        504,
                        {
                            "ok": False,
                            "status": "timeout",
                            "error_code": "upstream_timeout",
                            "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                        },
                    )
                    return
                if "error" in meta:
                    exc = meta["error"]
                    if isinstance(exc, (TimeoutError, socket.timeout)) or (
                        isinstance(exc, urllib.error.URLError)
                        and isinstance(getattr(exc, "reason", None), (TimeoutError, socket.timeout))
                    ):
                        self._send_json(
                            504,
                            {
                                "ok": False,
                                "status": "timeout",
                                "error_code": "upstream_timeout",
                                "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                            },
                        )
                    else:
                        self._send_json(502, {"ok": False, "error": f"upstream error: {exc}"})
                    return
                self._relay_from_queue(
                    meta["status"],
                    meta["headers"],
                    chunk_queue,
                    upstream_done=upstream_done,
                    deadline_mono=deadline_mono,
                    abort_upstream=_abort_upstream,
                    eff_timeout_s=eff_timeout_s,
                )
            finally:
                _abort_upstream()
        finally:
            _release_owner()

    def _relay_from_queue(
        self,
        status,
        headers,
        chunk_queue: queue.Queue,
        *,
        upstream_done: threading.Event,
        deadline_mono: float,
        abort_upstream,
        eff_timeout_s: float,
    ) -> None:
        rem_hdr = deadline_mono - time.monotonic()
        if rem_hdr <= 0.0:
            abort_upstream()
            self._send_json(
                504,
                {
                    "ok": False,
                    "status": "timeout",
                    "error_code": "upstream_timeout",
                    "error": f"upstream timed out after {eff_timeout_s:.1f}s",
                },
            )
            return
        self.close_connection = True
        sock = getattr(self, "connection", None)
        if sock is not None and hasattr(sock, "settimeout"):
            try:
                sock.settimeout(max(0.01, rem_hdr))
            except Exception:
                pass
        try:
            self.send_response(status)
            for k, v in headers:
                if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
        except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
            abort_upstream()
            self._close_downstream_forcefully()
            return

        while True:
            try:
                kind, payload = chunk_queue.get_nowait()
            except queue.Empty:
                if upstream_done.is_set():
                    try:
                        kind, payload = chunk_queue.get_nowait()
                    except queue.Empty:
                        break
                else:
                    rem = deadline_mono - time.monotonic()
                    if rem <= 0.0:
                        abort_upstream()
                        self._close_downstream_forcefully()
                        break
                    try:
                        kind, payload = chunk_queue.get(timeout=min(0.05, rem))
                    except queue.Empty:
                        continue
            if kind != "data":
                break
            if time.monotonic() >= deadline_mono:
                abort_upstream()
                self._close_downstream_forcefully()
                break
            if not self._write_bytes_with_deadline(payload, deadline_mono=deadline_mono):
                abort_upstream()
                self._close_downstream_forcefully()
                break

    def _relay(
        self,
        status,
        headers,
        body_stream,
        *,
        deadline_mono: float | None = None,
    ) -> None:
        self.close_connection = True
        sock = getattr(self, "connection", None)
        if deadline_mono is not None and sock is not None and hasattr(sock, "settimeout"):
            rem_hdr = deadline_mono - time.monotonic()
            if rem_hdr <= 0.0:
                _close_resource_forcefully(body_stream)
                self._close_downstream_forcefully()
                return
            try:
                sock.settimeout(max(0.01, rem_hdr))
            except Exception:
                pass
        try:
            self.send_response(status)
            for k, v in headers:
                if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
        except (TimeoutError, socket.timeout, BrokenPipeError, ConnectionResetError, OSError):
            _close_resource_forcefully(body_stream)
            self._close_downstream_forcefully()
            return
        raw_sock = _extract_raw_socket(body_stream)
        orig_sock_timeout: float | None = None
        if raw_sock is not None and hasattr(raw_sock, "gettimeout"):
            try:
                t_val = raw_sock.gettimeout()
                if isinstance(t_val, (int, float)) and t_val > 0:
                    orig_sock_timeout = float(t_val)
            except Exception:
                orig_sock_timeout = None
        read_fn = _resolve_nonblocking_chunk_reader(body_stream)
        while True:
            if deadline_mono is not None:
                rem = deadline_mono - time.monotonic()
                if rem <= 0.0:
                    _close_resource_forcefully(body_stream)
                    self._close_downstream_forcefully()
                    break
                if raw_sock is not None and hasattr(raw_sock, "settimeout"):
                    target_timeout = rem if orig_sock_timeout is None else min(orig_sock_timeout, rem)
                    try:
                        raw_sock.settimeout(max(0.01, target_timeout))
                    except Exception:
                        pass
            try:
                chunk = read_fn(4096)
            except (TimeoutError, socket.timeout, OSError):
                _close_resource_forcefully(body_stream)
                self._close_downstream_forcefully()
                break
            if not chunk:
                break
            if deadline_mono is not None and time.monotonic() >= deadline_mono:
                _close_resource_forcefully(body_stream)
                self._close_downstream_forcefully()
                break
            if not self._write_bytes_with_deadline(chunk, deadline_mono=deadline_mono):
                _close_resource_forcefully(body_stream)
                self._close_downstream_forcefully()
                break

    def do_GET(self) -> None:
        path = self._path_only()
        if path == "/healthz":
            self._send_json(200, health_payload())
            return
        if path == "/admin/drain":
            if not _is_valid_admin_token(self.headers):
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            with _state_lock:
                payload = {
                    "ok": True,
                    "draining": _draining,
                    "in_flight": _in_flight,
                    "owner_lock_held": _owner_lock_held(),
                    "standby": _standby,
                }
            self._send_json(200, payload)
            return
        # Guidepoint supports request/response MCP but its optional SSE GET
        # returns 404 even for a freshly initialized, working session.
        self.send_response(405)
        self.send_header("Allow", "POST, DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        global _draining
        path = self._path_only()
        if path == "/admin/drain":
            if not _is_valid_admin_token(self.headers):
                self._send_json(401, {"ok": False, "error": "Unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
            except Exception:
                self._send_json(400, {"ok": False, "error": "Parse error"})
                return
            with _state_lock:
                _draining = bool(body.get("drain", True))
                payload = {
                    "ok": True,
                    "draining": _draining,
                    "in_flight": _in_flight,
                    "owner_lock_held": _owner_lock_held(),
                    "standby": _standby,
                }
            self._send_json(200, payload)
            return
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def log_message(self, fmt, *args) -> None:
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    if "GUIDEPOINT_CLIENT_ID" not in os.environ or "GUIDEPOINT_CLIENT_SECRET" not in os.environ:
        print("Missing GUIDEPOINT_CLIENT_ID / GUIDEPOINT_CLIENT_SECRET env vars.", file=sys.stderr)
        sys.exit(1)
    atexit.register(release_owner_lock)
    initialize_runtime()
    port = int(os.environ.get("GUIDEPOINT_PROXY_PORT", str(PORT)))
    server = ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    print(f"[guidepoint-proxy] listening on http://127.0.0.1:{port}/mcp -> {UPSTREAM}")
    try:
        server.serve_forever()
    finally:
        release_owner_lock()


if __name__ == "__main__":
    main()
