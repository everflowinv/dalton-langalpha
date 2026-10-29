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
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import date
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
    _require_owner_only,
    build_core_request,
    compute_http_timeout_seconds,
    invoke_broker_over_socket,
    load_broker_key,
)

import log_redaction as _log_redaction  # noqa: E402  (sibling, pure stdlib)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] web_search_bridge: %(message)s",
)
logger = logging.getLogger("web_search_bridge")

MAX_REQUEST_BYTES = 64 * 1024  # 64 KiB
DEFAULT_QUEUE_TIMEOUT_S = DEFAULT_QUEUE_TIMEOUT_SECONDS
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_X_HANDLES = 20

# Upstream status passthrough (backlog 三-6, 2026-10-28): the bridge maps its
# own HTTP status from the broker error *code* alone, so a PROVIDER_ERROR could
# never be attributed to a 4xx/5xx at the provider. Whatever upstream signal
# the broker frame carries is copied into new optional top-level
# `upstream_status`/`upstream_code` fields on the bridge reply; existing fields
# are untouched and a frame that says nothing adds nothing.
_UPSTREAM_STATUS_KEYS = ("upstream_status", "providerStatus", "statusCode", "status", "httpStatus")
_UPSTREAM_CODE_KEYS = ("upstream_code", "providerCode", "provider_code")


def _extract_upstream_error_signal(
    frame: Mapping[str, Any],
) -> tuple[int | None, str | None]:
    """Best-effort (upstream_status, upstream_code) from a broker reply frame.

    Explicit frame fields first; then (P0 fix1 9) the broker frame today only
    carries ``error.code``/``error.message``, so an HTTP status named in the
    message text (``HTTP 429``, ``status 503``, ``429 Too Many Requests``,
    a stringified dict's ``status_code``) is extracted via the shared
    `log_redaction.http_status_from_text` — without it a PROVIDER_ERROR can
    never be attributed to a 4xx/5xx."""
    err_obj = frame.get("error") if isinstance(frame.get("error"), Mapping) else {}
    upstream_status: int | None = None
    for source in (frame, err_obj):
        for key in _UPSTREAM_STATUS_KEYS:
            raw = source.get(key)
            if isinstance(raw, bool) or not isinstance(raw, int):
                continue
            if 100 <= raw <= 599:
                upstream_status = raw
                break
        if upstream_status is not None:
            break
    if upstream_status is None:
        message = str(err_obj.get("message") or "") if isinstance(err_obj, Mapping) else ""
        if message:
            upstream_status = _log_redaction.http_status_from_text(message)
    upstream_code: str | None = None
    for source in (frame, err_obj):
        for key in _UPSTREAM_CODE_KEYS:
            raw = source.get(key)
            if isinstance(raw, str) and raw.strip():
                upstream_code = raw.strip()
                break
        if upstream_code is not None:
            break
    return upstream_status, upstream_code


def _attach_upstream_error_fields(broker_reply: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``broker_reply`` with optional top-level ``upstream_status`` /
    ``upstream_code`` added when the frame carried them; the frame itself
    otherwise, so a signal-free reply goes out verbatim."""
    upstream_status, upstream_code = _extract_upstream_error_signal(broker_reply)
    if upstream_status is None and upstream_code is None:
        return broker_reply
    enriched = dict(broker_reply)
    if upstream_status is not None and "upstream_status" not in enriched:
        enriched["upstream_status"] = upstream_status
    if upstream_code is not None and "upstream_code" not in enriched:
        enriched["upstream_code"] = upstream_code
    return enriched


def _is_valid_yyyy_mm_dd(value: Any) -> bool:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def _normalize_x_handles(raw: Any, field_name: str) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise WebSearchBrokerError(
            f"{field_name} must be a list of strings", code="INVALID_REQUEST"
        )
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise WebSearchBrokerError(
                f"{field_name} entries must be strings", code="INVALID_REQUEST"
            )
        handle = item.strip().lstrip("@").strip()
        if not handle:
            raise WebSearchBrokerError(
                f"{field_name} entries must not be empty", code="INVALID_REQUEST"
            )
        cleaned.append(handle)
    if len(cleaned) > _MAX_X_HANDLES:
        raise WebSearchBrokerError(
            f"{field_name} cannot exceed {_MAX_X_HANDLES} handles",
            code="INVALID_REQUEST",
        )
    return cleaned or None


def invoke_broker_x_search(
    *,
    broker_url: str,
    broker_token: str,
    payload: Mapping[str, Any],
    timeout_ms: int,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Invoke ``POST /dalton-broker/v1/x-search`` on the local OpenClaw gateway."""
    endpoint = (
        broker_url
        if broker_url.endswith("/x-search")
        else f"{broker_url.rstrip('/')}/x-search"
    )
    req_bytes = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=req_bytes,
        headers={
            "Authorization": f"Bearer {broker_token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )
    timeout_s = max(5.0, float(timeout_ms) / 1000.0 + 5.0)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw_bytes = resp.read(max_response_bytes + 1)
    except urllib.error.HTTPError as exc:
        try:
            err_bytes = exc.read(max_response_bytes + 1)
            parsed_err = json.loads(err_bytes.decode("utf-8", errors="replace"))
            if isinstance(parsed_err, Mapping):
                if parsed_err.get("ok") is False and isinstance(parsed_err.get("error"), Mapping):
                    # P0 fix1 8: a verbatim ok:false broker frame keeps the
                    # HTTP status it arrived with as an optional field too —
                    # the error body's own code alone never names it.
                    verbatim = dict(parsed_err)
                    if (
                        isinstance(exc.code, int)
                        and not isinstance(exc.code, bool)
                        and "upstream_status" not in verbatim
                    ):
                        verbatim["upstream_status"] = exc.code
                    return verbatim
                inner_err = parsed_err.get("error")
                if isinstance(inner_err, Mapping):
                    synthesized: dict[str, Any] = {
                        "ok": False,
                        "error": {
                            "code": str(inner_err.get("code") or f"HTTP_{exc.code}"),
                            "message": str(inner_err.get("message") or f"Broker HTTP {exc.code}"),
                        },
                    }
                    # Upstream passthrough (三-6): the broker's HTTP status is
                    # dropped once inner_err carries its own code — keep it as
                    # an optional field instead.
                    if isinstance(exc.code, int) and not isinstance(exc.code, bool):
                        synthesized["upstream_status"] = exc.code
                    return synthesized
        except Exception:
            pass
        raise WebSearchBrokerError(
            f"OpenClaw broker returned HTTP {exc.code}",
            code="BROKER_HTTP_ERROR",
            upstream_status=exc.code if isinstance(exc.code, int) else None,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WebSearchBrokerError(
            f"OpenClaw broker unreachable: {type(exc).__name__}",
            code="BROKER_UNAVAILABLE",
        ) from exc

    if len(raw_bytes) > max_response_bytes:
        raise WebSearchBrokerError(
            "OpenClaw broker response exceeded max_response_bytes",
            code="FRAME_TOO_LARGE",
        )
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise WebSearchBrokerError(
            "OpenClaw broker returned non-JSON response", code="INVALID_JSON"
        ) from exc
    if not isinstance(parsed, Mapping):
        raise WebSearchBrokerError(
            "OpenClaw broker response must be a JSON object", code="INVALID_JSON"
        )
    return dict(parsed)


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
        explicit_env_file = os.environ.get("DALTON_WEB_SEARCH_BRIDGE_ENV_FILE")
        env_path = Path(
            explicit_env_file
            or os.path.expanduser("~/.config/dalton/web-search-bridge.env")
        ).expanduser()
        self.env_path: Path = env_path
        if explicit_env_file or "PYTEST_CURRENT_TEST" not in os.environ:
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
        self.broker_url: str = (
            os.environ.get(
                "DALTON_BROKER_URL",
                "http://127.0.0.1:18789/dalton-broker/v1",
            )
            .strip()
            .rstrip("/")
        )
        self.broker_token: str = os.environ.get("DALTON_BROKER_TOKEN", "").strip()
        self.broker_token_path: Path = Path(
            os.environ.get(
                "DALTON_BROKER_TOKEN_PATH",
                os.path.expanduser(
                    "~/.openclaw/state/plugins/dalton-langalpha-broker/broker.token"
                ),
            )
        ).expanduser()


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
                "search broker error code=%s elapsed_ms=%d upstream_status=%s upstream_code=%s",
                _log_redaction.clean_log_field(exc.code, max_len=64) or "-",
                elapsed_ms,
                exc.upstream_status if exc.upstream_status is not None else "-",
                _log_redaction.clean_log_field(exc.upstream_code or "", max_len=64) or "-",
            )
            status = 400 if exc.code == "INVALID_REQUEST" else 503
            error_payload: dict[str, Any] = {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
            }
            if exc.upstream_status is not None:
                error_payload["upstream_status"] = exc.upstream_status
            if exc.upstream_code is not None:
                error_payload["upstream_code"] = exc.upstream_code
            return status, error_payload
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
        # P0 fix2 E: HTTP status mapping must use the raw error.code so
        # trailing whitespace/control chars keep their baseline 502 mapping;
        # only the copy written to logs goes through clean_log_field.
        err_code = str(err_obj.get("code") or "-") if not ok else "-"
        log_err_code = (
            _log_redaction.clean_log_field(str(err_obj.get("code") or ""), max_len=64) or "-"
            if not ok
            else "-"
        )
        upstream_status, upstream_code = _extract_upstream_error_signal(broker_reply)
        upstream_suffix = ""
        upstream_args: tuple[Any, ...] = ()
        if not ok and (upstream_status is not None or upstream_code is not None):
            upstream_suffix = " upstream_status=%s upstream_code=%s"
            upstream_args = (
                upstream_status if upstream_status is not None else "-",
                _log_redaction.clean_log_field(upstream_code or "", max_len=64) or "-",
            )
        elif not ok:
            # P0 fix1 9: no status anywhere — the sanitized message head at
            # least names the reason text for the next PROVIDER_ERROR.
            upstream_suffix = " message=%s"
            upstream_args = (
                _log_redaction.clean_log_field(
                    str(err_obj.get("message") or ""), max_len=200
                )
                or "-",
            )
        logger.info(
            "search completed status=%s count=%d error_code=%s elapsed_ms=%d" + upstream_suffix,
            "ok" if ok else "error",
            count,
            log_err_code,
            elapsed_ms,
            *upstream_args,
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
            return http_status, _attach_upstream_error_fields(broker_reply)
        # Downgrade visibility (backlog 三-6): the broker picks the effective
        # provider per call; a reply served by a fallback (e.g. last30days
        # where the profile's primary is gemini) is a degradation worth a line.
        # The broker does not report why it fell back, so the reason is
        # best-effort and "unknown" when the frame says nothing.
        reply_provider = str(broker_reply.get("provider") or "").strip()
        expected_provider_name = (
            expected_provider or self.cfg.expected_provider or "gemini"
        ).strip()
        if reply_provider and reply_provider != expected_provider_name:
            reason_raw = str(
                broker_reply.get("fallbackReason")
                or broker_reply.get("degradedReason")
                or broker_reply.get("reason")
                or ""
            ).strip()
            logger.warning(
                "search provider_downgraded expected=%s effective=%s reason_category=%s reason=%s",
                _log_redaction.clean_log_field(expected_provider_name, max_len=64) or "-",
                _log_redaction.clean_log_field(reply_provider, max_len=64) or "-",
                "provider_fallback" if reason_raw else "unknown",
                # P0 fix1 2: the reason is broker-controlled text — redacted,
                # control-char-stripped, truncated before it reaches the log.
                _log_redaction.clean_log_field(reason_raw, max_len=200) or "-",
            )
        return 200, broker_reply

    def _load_broker_bearer_token(self) -> str:
        if self.cfg.broker_token:
            return self.cfg.broker_token
        _require_owner_only(self.cfg.broker_token_path, "openclaw broker token")
        try:
            token = self.cfg.broker_token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise WebSearchBrokerError(
                f"Cannot read OpenClaw broker token file: {type(exc).__name__}",
                code="BROKER_AUTH_UNAVAILABLE",
            ) from exc
        if not token:
            raise WebSearchBrokerError(
                "OpenClaw broker token file is empty",
                code="BROKER_AUTH_UNAVAILABLE",
            )
        return token

    async def handle_x_search(self, body: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Validate ``{query, handles?/allowed_x_handles?, excluded_x_handles?, from_date?, to_date?, timeoutMs?}``, queue on ``self.sem``, and call broker."""
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            return 400, {
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "query is required"},
            }

        raw_timeout = body.get("timeoutMs", self.cfg.default_timeout_ms)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or raw_timeout < 1:
            return 400, {
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "timeoutMs must be a positive integer"},
            }
        timeout_ms = min(raw_timeout, DEFAULT_MAX_TIMEOUT_MS)

        try:
            raw_allowed = (
                body.get("allowed_x_handles")
                if "allowed_x_handles" in body
                else body.get("handles")
            )
            allowed_handles = _normalize_x_handles(raw_allowed, "allowed_x_handles")
            excluded_handles = _normalize_x_handles(
                body.get("excluded_x_handles"), "excluded_x_handles"
            )
        except WebSearchBrokerError as exc:
            return 400, {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
            }

        if allowed_handles and excluded_handles:
            return 400, {
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "allowed_x_handles and excluded_x_handles cannot be combined",
                },
            }

        from_date = body.get("from_date") if "from_date" in body else body.get("fromDate")
        to_date = body.get("to_date") if "to_date" in body else body.get("toDate")
        if from_date is not None and not _is_valid_yyyy_mm_dd(from_date):
            return 400, {
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "from_date must be a YYYY-MM-DD string",
                },
            }
        if to_date is not None and not _is_valid_yyyy_mm_dd(to_date):
            return 400, {
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "to_date must be a YYYY-MM-DD string",
                },
            }
        if from_date is not None and to_date is not None and from_date > to_date:
            return 400, {
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "from_date must not be later than to_date",
                },
            }

        broker_payload: dict[str, Any] = {
            "query": query.strip(),
            "timeoutMs": timeout_ms,
        }
        if allowed_handles:
            broker_payload["allowed_x_handles"] = allowed_handles
        if excluded_handles:
            broker_payload["excluded_x_handles"] = excluded_handles
        if from_date is not None:
            broker_payload["from_date"] = from_date
        if to_date is not None:
            broker_payload["to_date"] = to_date

        t0 = time.monotonic()
        deadline = t0 + self.cfg.queue_timeout_s
        try:
            await asyncio.wait_for(self.sem.acquire(), timeout=self.cfg.queue_timeout_s)
        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "x_search bridge busy queue_timeout_s=%.1f elapsed_ms=%d",
                self.cfg.queue_timeout_s,
                elapsed_ms,
            )
            return 503, {
                "ok": False,
                "error": {
                    "code": "bridge_busy",
                    "message": "X search bridge is busy (concurrency queue timeout)",
                },
            }

        try:
            broker_token = self._load_broker_bearer_token()
            backoff_s = 0.05
            while True:
                broker_reply = await asyncio.to_thread(
                    invoke_broker_x_search,
                    broker_url=self.cfg.broker_url,
                    broker_token=broker_token,
                    payload=broker_payload,
                    timeout_ms=timeout_ms,
                    max_response_bytes=DEFAULT_MAX_RESPONSE_BYTES,
                )
                ok_attempt = broker_reply.get("ok") is True
                err_attempt = (
                    broker_reply.get("error")
                    if isinstance(broker_reply.get("error"), Mapping)
                    else {}
                )
                err_code_attempt = (
                    str(err_attempt.get("code") or "") if not ok_attempt else ""
                )
                if not ok_attempt and err_code_attempt == "bridge_busy":
                    remaining_s = deadline - time.monotonic()
                    if remaining_s <= 0:
                        elapsed_ms = int((time.monotonic() - t0) * 1000)
                        logger.warning(
                            "x_search broker busy retry exhausted queue_timeout_s=%.1f elapsed_ms=%d",
                            self.cfg.queue_timeout_s,
                            elapsed_ms,
                        )
                        return 503, {
                            "ok": False,
                            "error": {
                                "code": "bridge_busy",
                                "message": "X search broker is busy (concurrency queue timeout)",
                            },
                        }
                    await asyncio.sleep(min(backoff_s, remaining_s))
                    backoff_s = min(backoff_s * 2.0, 0.5)
                    continue
                break
        except WebSearchBrokerError as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "x_search broker error code=%s elapsed_ms=%d upstream_status=%s upstream_code=%s",
                _log_redaction.clean_log_field(exc.code, max_len=64) or "-",
                elapsed_ms,
                exc.upstream_status if exc.upstream_status is not None else "-",
                _log_redaction.clean_log_field(exc.upstream_code or "", max_len=64) or "-",
            )
            status = 400 if exc.code == "INVALID_REQUEST" else 503
            error_payload: dict[str, Any] = {
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
            }
            # P0 fix1 8: the upstream HTTP status rides the reply as an
            # optional field, mirroring handle_search's error payloads.
            if exc.upstream_status is not None:
                error_payload["upstream_status"] = exc.upstream_status
            if exc.upstream_code is not None:
                error_payload["upstream_code"] = exc.upstream_code
            return status, error_payload
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                "x_search bridge unexpected error type=%s elapsed_ms=%d",
                type(exc).__name__,
                elapsed_ms,
            )
            return 503, {
                "ok": False,
                "error": {
                    "code": "BRIDGE_INTERNAL_ERROR",
                    "message": "X search bridge internal error",
                },
            }
        finally:
            self.sem.release()

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        ok = broker_reply.get("ok") is True
        err_obj = broker_reply.get("error") if isinstance(broker_reply.get("error"), Mapping) else {}
        # P0 fix2 E: keep raw err_code for HTTP status mapping; sanitize only the logged copy.
        err_code = str(err_obj.get("code") or "-") if not ok else "-"
        log_err_code = (
            _log_redaction.clean_log_field(str(err_obj.get("code") or ""), max_len=64) or "-"
            if not ok
            else "-"
        )
        upstream_status, upstream_code = _extract_upstream_error_signal(broker_reply)
        upstream_suffix = ""
        upstream_args: tuple[Any, ...] = ()
        if not ok and (upstream_status is not None or upstream_code is not None):
            upstream_suffix = " upstream_status=%s upstream_code=%s"
            upstream_args = (
                upstream_status if upstream_status is not None else "-",
                _log_redaction.clean_log_field(upstream_code or "", max_len=64) or "-",
            )
        elif not ok:
            upstream_suffix = " message=%s"
            upstream_args = (
                _log_redaction.clean_log_field(
                    str(err_obj.get("message") or ""), max_len=200
                )
                or "-",
            )
        logger.info(
            "x_search completed status=%s error_code=%s elapsed_ms=%d" + upstream_suffix,
            "ok" if ok else "error",
            log_err_code,
            elapsed_ms,
            *upstream_args,
        )
        if not ok:
            if err_code in ("PROVIDER_RATE_LIMITED", "RATE_LIMITED"):
                http_status = 429
            elif err_code in ("PROVIDER_PERMISSION_DENIED", "AUTH_REQUIRED", "AUTH_INVALID", "AUTH_EXPIRED"):
                http_status = 403
            elif err_code in ("INVALID_REQUEST", "MISSING_FIELD", "UNKNOWN_FIELD"):
                http_status = 400
            elif err_code == "bridge_busy":
                http_status = 503
            else:
                http_status = 502
            return http_status, _attach_upstream_error_fields(broker_reply)
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

        if path not in ("/search", "/v1/search", "/x-search", "/v1/x-search"):
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

        if path in ("/x-search", "/v1/x-search"):
            status_code, reply_payload = await bridge.handle_x_search(body_obj)
        else:
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
