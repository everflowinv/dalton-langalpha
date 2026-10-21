#!/usr/bin/env python3
"""Host-side HTTP bridge for OpenClaw's Unix-socket web search broker.

Listens on ``127.0.0.1:8961`` (configurable via ``DALTON_WEB_SEARCH_BRIDGE_HOST`` /
``DALTON_WEB_SEARCH_BRIDGE_PORT``; loopback-only).
Endpoints:
  - ``GET /healthz`` (unauthenticated loopback health check)
  - ``POST /search`` (authenticated via ``Authorization: Bearer <DALTON_WEB_SEARCH_BRIDGE_TOKEN>``
    or ``X-Bridge-Token`` request header)

Why ``DALTON_WEB_SEARCH_BRIDGE_TOKEN`` instead of ``INTERNAL_SERVICE_TOKEN``:
``INTERNAL_SERVICE_TOKEN`` authorizes privileged internal backend service-to-service
dispatch across user threads and automations; isolating the host launchd bridge
behind its own dedicated ``DALTON_WEB_SEARCH_BRIDGE_TOKEN`` (matching
``researchdata_bridge.py``'s ``DALTON_BRIDGE_TOKEN``) enforces least privilege
and independent secret rotation.

Security:
  - Never logs query strings, search results, bearer tokens, or broker HMAC keys.
  - Pure Python 3.11+ standard library only; never imports ``src.*`` or reads repo ``.env``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping

# Ensure sibling `web_search_broker_protocol.py` is importable regardless of cwd
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from web_search_broker_protocol import (  # noqa: E402
    DEFAULT_CLIENT_ID,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_TIMEOUT_MS,
    DEFAULT_PROFILE_ID,
    DEFAULT_QUEUE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_MS,
    WebSearchBrokerError,
    build_core_request,
    compute_http_timeout_seconds,
    invoke_broker_over_socket,
    load_broker_key,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] web_search_bridge: %(message)s",
)
logger = logging.getLogger("web_search_bridge")

MAX_REQUEST_BYTES = 64 * 1024  # 64 KiB
DEFAULT_QUEUE_TIMEOUT_S = DEFAULT_QUEUE_TIMEOUT_SECONDS
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines into os.environ if not already set."""
    if not path.is_file():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as exc:
        logger.warning("Failed reading env file %s: %s", path, type(exc).__name__)


class WebSearchBridgeConfig:
    """Configuration for the host-side OpenClaw web search HTTP bridge.

    Reads only from ``DALTON_WEB_SEARCH_BRIDGE_ENV_FILE``
    (default ``~/.config/dalton/web-search-bridge.env``) and process environment.
    """

    def __init__(self) -> None:
        env_path = Path(
            os.environ.get(
                "DALTON_WEB_SEARCH_BRIDGE_ENV_FILE",
                os.path.expanduser("~/.config/dalton/web-search-bridge.env"),
            )
        ).expanduser()
        self.env_path: Path = env_path
        _load_env_file(env_path)

        raw_host = os.environ.get("DALTON_WEB_SEARCH_BRIDGE_HOST", "127.0.0.1").strip()
        if raw_host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"DALTON_WEB_SEARCH_BRIDGE_HOST must be loopback (got {raw_host!r})"
            )
        self.host: str = raw_host
        self.port: int = int(os.environ.get("DALTON_WEB_SEARCH_BRIDGE_PORT", "8961"))
        self.token: str = os.environ.get("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "").strip()
        self.socket_path: Path = Path(
            os.environ.get(
                "DALTON_WEB_SEARCH_BROKER_SOCKET",
                os.path.expanduser("~/.openclaw/dalton-web-search-broker.sock"),
            )
        ).expanduser()
        self.auth_key_path: Path = Path(
            os.environ.get(
                "DALTON_WEB_SEARCH_BROKER_KEY",
                os.path.expanduser("~/.openclaw/dalton-web-search-broker.sock.key"),
            )
        ).expanduser()
        self.client_id: str = (
            os.environ.get("DALTON_WEB_SEARCH_BROKER_CLIENT_ID", DEFAULT_CLIENT_ID).strip()
            or DEFAULT_CLIENT_ID
        )
        self.profile_id: str = (
            os.environ.get("DALTON_WEB_SEARCH_BROKER_PROFILE_ID", DEFAULT_PROFILE_ID).strip()
            or DEFAULT_PROFILE_ID
        )
        raw_expected = os.environ.get("DALTON_WEB_SEARCH_EXPECTED_PROVIDER", "").strip()
        self.expected_provider: str | None = raw_expected or None
        self.default_timeout_ms: int = max(
            1,
            min(
                int(os.environ.get("DALTON_WEB_SEARCH_DEFAULT_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))),
                DEFAULT_MAX_TIMEOUT_MS,
            ),
        )
        self.max_inflight: int = max(
            1, int(os.environ.get("DALTON_WEB_SEARCH_BRIDGE_MAX_INFLIGHT", "4"))
        )
        self.queue_timeout_s: float = max(
            0.001,
            float(
                os.environ.get(
                    "DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S",
                    str(DEFAULT_QUEUE_TIMEOUT_S),
                )
            ),
        )
        self.http_timeout_s: float = compute_http_timeout_seconds(
            self.default_timeout_ms,
            queue_timeout_seconds=self.queue_timeout_s,
        )


class WebSearchBridge:
    """Forwards authenticated HTTP search requests to the OpenClaw Unix-socket broker."""

    def __init__(self, cfg: WebSearchBridgeConfig) -> None:
        self.cfg = cfg
        self.sem = asyncio.Semaphore(cfg.max_inflight)

    def probe_broker_files(self) -> dict[str, bool]:
        """Check whether the broker socket and HMAC key file are ready without reading query/result data."""
        socket_ready = False
        try:
            st = self.cfg.socket_path.lstat()
            socket_ready = stat.S_ISSOCK(st.st_mode) and (st.st_mode & 0o077) == 0
        except OSError:
            socket_ready = False

        key_ready = False
        try:
            _ = load_broker_key(self.cfg.auth_key_path)
            key_ready = True
        except Exception:
            key_ready = False

        return {
            "broker_socket_ready": socket_ready,
            "broker_key_ready": key_ready,
        }

    async def handle_search(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Validate ``{query, count, dateAfter?, dateBefore?, timeoutMs?}``, call broker, and return ``(http_status, payload)``."""
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            return 400, {
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "query is required"},
            }

        raw_count = body.get("count", 5)
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
            return 400, {
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "count must be a positive integer"},
            }
        count = min(raw_count, 10)

        raw_timeout = body.get("timeoutMs", self.cfg.default_timeout_ms)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or raw_timeout < 1:
            return 400, {
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "timeoutMs must be a positive integer"},
            }
        timeout_ms = min(raw_timeout, DEFAULT_MAX_TIMEOUT_MS)

        has_date_after = "dateAfter" in body or "date_after" in body
        has_date_before = "dateBefore" in body or "date_before" in body
        if has_date_after != has_date_before:
            return 400, {
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "dateAfter and dateBefore must be supplied together",
                },
            }
        if has_date_after:
            date_after = body["dateAfter"] if "dateAfter" in body else body["date_after"]
            date_before = body["dateBefore"] if "dateBefore" in body else body["date_before"]
            if not isinstance(date_after, str) or not isinstance(date_before, str):
                return 400, {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "dateAfter and dateBefore must be YYYY-MM-DD strings",
                    },
                }
        else:
            date_after = None
            date_before = None

        if "expectedProvider" in body:
            raw_expected_provider = body["expectedProvider"]
            if not isinstance(raw_expected_provider, str) or not raw_expected_provider.strip():
                return 400, {
                    "ok": False,
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "expectedProvider must be a non-empty string",
                    },
                }
            expected_provider: str | None = raw_expected_provider.strip()
        else:
            expected_provider = self.cfg.expected_provider

        core_kwargs: dict[str, Any] = {
            "query": query,
            "count": count,
            "timeout_ms": timeout_ms,
            "profile_id": self.cfg.profile_id,
        }
        if has_date_after:
            core_kwargs["date_after"] = date_after
            core_kwargs["date_before"] = date_before
        if expected_provider is not None:
            core_kwargs["expected_provider"] = expected_provider

        try:
            _ = build_core_request(**core_kwargs)
        except WebSearchBrokerError as exc:
            return 400, {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
            }

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(self.sem.acquire(), timeout=self.cfg.queue_timeout_s)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "search bridge busy queue_timeout_s=%.1f elapsed_ms=%d",
                self.cfg.queue_timeout_s,
                elapsed_ms,
            )
            return 503, {
                "ok": False,
                "error": {
                    "code": "bridge_busy",
                    "message": "web search bridge is busy (concurrency queue timeout)",
                },
            }

        try:
            broker_reply = await asyncio.to_thread(
                invoke_broker_over_socket,
                socket_path=self.cfg.socket_path,
                auth_key_path=self.cfg.auth_key_path,
                query=query,
                count=count,
                date_after=date_after,
                date_before=date_before,
                timeout_ms=timeout_ms,
                client_id=self.cfg.client_id,
                profile_id=self.cfg.profile_id,
                expected_provider=expected_provider,
                max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
            )
        except WebSearchBrokerError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "search broker error code=%s elapsed_ms=%d",
                exc.code,
                elapsed_ms,
            )
            status = 400 if exc.code == "INVALID_REQUEST" else 503
            return status, {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
            }
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                "search bridge unexpected error type=%s elapsed_ms=%d",
                type(exc).__name__,
                elapsed_ms,
            )
            return 503, {
                "ok": False,
                "error": {
                    "code": "BRIDGE_INTERNAL_ERROR",
                    "message": "web search bridge internal error",
                },
            }
        finally:
            self.sem.release()

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        ok = broker_reply.get("ok") is True
        err_obj = broker_reply.get("error") if isinstance(broker_reply.get("error"), Mapping) else {}
        err_code = str(err_obj.get("code") or "-") if not ok else "-"
        logger.info(
            "search completed status=%s count=%d error_code=%s elapsed_ms=%d",
            "ok" if ok else "error",
            count,
            err_code,
            elapsed_ms,
        )
        if not ok:
            if err_code == "PROVIDER_RATE_LIMITED":
                http_status = 429
            elif err_code in ("PROVIDER_PERMISSION_DENIED", "AUTH_REQUIRED", "AUTH_INVALID", "AUTH_EXPIRED"):
                http_status = 403
            elif err_code in ("INVALID_REQUEST", "MISSING_FIELD", "UNKNOWN_FIELD"):
                http_status = 400
            else:
                http_status = 502
            return http_status, broker_reply
        return 200, broker_reply


async def _send_http(
    writer: asyncio.StreamWriter,
    status_code: int,
    status_text: str,
    body_obj: dict[str, Any] | None = None,
) -> None:
    body_bytes = (
        json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        if body_obj is not None
        else b""
    )
    headers = [
        f"HTTP/1.1 {status_code} {status_text}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body_bytes)}",
        "Connection: close",
    ]
    header_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
    writer.write(header_bytes + body_bytes)
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


def _extract_supplied_token(headers: Mapping[str, str]) -> str:
    """Extract bridge authentication token from request headers only."""
    auth_header = headers.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    bridge_header = headers.get("x-bridge-token", "").strip()
    if bridge_header:
        return bridge_header
    return ""


async def _handle_client(
    bridge: WebSearchBridge,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=15.0)
        if not req_line_bytes:
            writer.close()
            return
        req_line = req_line_bytes.decode("utf-8", errors="replace").strip()
        parts = req_line.split(" ")
        if len(parts) < 2:
            await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Bad Request"})
            return
        method, raw_path = parts[0].upper(), parts[1]
        path = raw_path.split("?", 1)[0]

        headers: dict[str, str] = {}
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=15.0)
            if not hline or hline in (b"\r\n", b"\n"):
                break
            decoded = hline.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                hk, hv = decoded.split(":", 1)
                headers[hk.strip().lower()] = hv.strip()

        content_length = int(headers.get("content-length") or "0")
        if content_length > MAX_REQUEST_BYTES:
            await _send_http(
                writer,
                413,
                "Payload Too Large",
                {"ok": False, "error": "Request body exceeds 64 KiB"},
            )
            return

        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=15.0
            )

        if method == "GET" and path == "/healthz":
            probe = bridge.probe_broker_files()
            await _send_http(
                writer,
                200,
                "OK",
                {
                    "ok": True,
                    "status": "ok",
                    "max_inflight": bridge.cfg.max_inflight,
                    "queue_timeout_s": bridge.cfg.queue_timeout_s,
                    "http_timeout_s": bridge.cfg.http_timeout_s,
                    **probe,
                },
            )
            return

        if path not in ("/search", "/v1/search"):
            await _send_http(writer, 404, "Not Found", {"ok": False, "error": "Not Found"})
            return

        supplied_token = _extract_supplied_token(headers)
        expected_token = bridge.cfg.token
        if (
            not expected_token
            or not supplied_token
            or not hmac.compare_digest(
                supplied_token.encode("utf-8"), expected_token.encode("utf-8")
            )
        ):
            await _send_http(writer, 401, "Unauthorized", {"ok": False, "error": "Unauthorized"})
            return

        if method != "POST":
            await _send_http(
                writer, 405, "Method Not Allowed", {"ok": False, "error": "Method Not Allowed"}
            )
            return

        try:
            body_obj = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            await _send_http(
                writer,
                400,
                "Bad Request",
                {"ok": False, "error": {"code": "INVALID_JSON", "message": "Invalid JSON body"}},
            )
            return

        if not isinstance(body_obj, Mapping):
            await _send_http(
                writer,
                400,
                "Bad Request",
                {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "JSON body must be an object"}},
            )
            return

        status_code, reply_payload = await bridge.handle_search(body_obj)
        status_text = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            403: "Forbidden",
            429: "Too Many Requests",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }.get(status_code, "Error")
        await _send_http(writer, status_code, status_text, reply_payload)
    except Exception as exc:
        logger.error("Unhandled bridge error: %s", type(exc).__name__)
        try:
            await _send_http(
                writer,
                500,
                "Internal Server Error",
                {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "Internal bridge error"}},
            )
        except Exception:
            pass


async def main() -> None:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("Usage: web_search_bridge.py (configured via ~/.config/dalton/web-search-bridge.env)")
        return

    cfg = WebSearchBridgeConfig()
    if not cfg.token:
        logger.error(
            "DALTON_WEB_SEARCH_BRIDGE_TOKEN is not set (checked env and ~/.config/dalton/web-search-bridge.env)"
        )
        sys.exit(1)

    bridge = WebSearchBridge(cfg)
    probe = bridge.probe_broker_files()
    server = await asyncio.start_server(
        lambda r, w: _handle_client(bridge, r, w),
        host=cfg.host,
        port=cfg.port,
    )
    logger.info(
        "Listening on http://%s:%d (socket_ready=%s, key_ready=%s, max_inflight=%d)",
        cfg.host,
        cfg.port,
        probe["broker_socket_ready"],
        probe["broker_key_ready"],
        cfg.max_inflight,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
