"""Protocol, HMAC signing, and Unix-socket transport for the OpenClaw web search broker.

Pure Python standard-library module located in ``scripts/dalton/`` so the
host-side launchd HTTP bridge (``scripts/dalton/web_search_bridge.py``) can
import it directly as a sibling module without touching ``src/`` package
``__init__.py`` files, third-party dependencies, or ``load_dotenv()``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import stat
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

PROTOCOL_VERSION = "0.1"
TOOL_NAME = "web_search"
DEFAULT_PROFILE_ID = "profile:web-search"
DEFAULT_CLIENT_ID = "client:dalton-core"
DEFAULT_TIMEOUT_MS = 60_000
DEFAULT_MAX_TIMEOUT_MS = 240_000
DEFAULT_QUEUE_TIMEOUT_SECONDS = 30.0
DEFAULT_QUEUE_TIMEOUT_S = DEFAULT_QUEUE_TIMEOUT_SECONDS
HTTP_TIMEOUT_MARGIN_SECONDS = 15.0
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB


def compute_http_timeout_seconds(
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
    margin_seconds: float = HTTP_TIMEOUT_MARGIN_SECONDS,
) -> float:
    """Derive the full client HTTP timeout from queue timeout + broker ``timeoutMs`` + transport margin."""
    return (
        max(0.0, float(queue_timeout_seconds))
        + max(1, int(timeout_ms)) / 1000.0
        + max(0.0, float(margin_seconds))
    )


DEFAULT_HTTP_TIMEOUT_SECONDS = compute_http_timeout_seconds(DEFAULT_TIMEOUT_MS)

_OMITTED = object()

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_CLIENT_RE = re.compile(r"^client:[A-Za-z0-9._-]+$")
_PROFILE_RE = re.compile(r"^profile:[A-Za-z0-9._-]+$")
_CALL_REF_RE = re.compile(r"^credential-use:[A-Za-z0-9._:-]+$")
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


class WebSearchBrokerError(RuntimeError):
    """Raised when the broker socket, key, request, or reply fails validation."""

    def __init__(self, message: str, *, code: str = "BROKER_ERROR") -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    """Serialize ``value`` with sorted keys and compact separators matching ``protocol.mjs``."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def authentication_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical request dict with ``auth.mac`` stripped before HMAC calculation."""
    core = {k: v for k, v in request.items() if k != "auth"}
    raw_auth = request.get("auth")
    unsigned_auth = (
        {k: v for k, v in raw_auth.items() if k != "mac"}
        if isinstance(raw_auth, Mapping)
        else {}
    )
    return {**core, "auth": unsigned_auth}


def compute_mac(secret: str, request: Mapping[str, Any]) -> str:
    """Compute HMAC-SHA-256 hex digest over ``canonical_json(authentication_payload(request))``."""
    payload = canonical_json(authentication_payload(request))
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), sha256).hexdigest()


def sign_request(
    core: Mapping[str, Any],
    *,
    secret: str,
    client_id: str = DEFAULT_CLIENT_ID,
    timestamp_ms: int,
    nonce: str,
) -> dict[str, Any]:
    """Attach the ``hmac-sha256-v1`` authentication envelope to ``core``."""
    if not isinstance(secret, str) or _KEY_RE.fullmatch(secret) is None:
        raise WebSearchBrokerError("web search broker auth key is invalid", code="INVALID_CONFIG")
    if not isinstance(client_id, str) or _CLIENT_RE.fullmatch(client_id) is None:
        raise WebSearchBrokerError("web search broker client_id is invalid", code="INVALID_CONFIG")
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
        raise WebSearchBrokerError("timestamp_ms must be an integer", code="INVALID_REQUEST")
    if not isinstance(nonce, str) or _NONCE_RE.fullmatch(nonce) is None:
        raise WebSearchBrokerError("nonce must be 32 lowercase hex characters", code="INVALID_REQUEST")

    unsigned = {
        "scheme": "hmac-sha256-v1",
        "clientId": client_id,
        "timestampMs": timestamp_ms,
        "nonce": nonce,
    }
    request_with_auth = {**dict(core), "auth": unsigned}
    mac = compute_mac(secret, request_with_auth)
    return {**dict(core), "auth": {**unsigned, "mac": mac}}


def _require_owner_only(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise WebSearchBrokerError(f"{label} is unavailable", code="BROKER_UNAVAILABLE") from exc
    if not stat.S_ISREG(info.st_mode):
        raise WebSearchBrokerError(f"{label} must be a regular file", code="UNSAFE_STATE_FILE")
    if info.st_mode & 0o077:
        raise WebSearchBrokerError(f"{label} must be owner-only", code="UNSAFE_STATE_FILE")
    if info.st_uid != os.getuid():
        raise WebSearchBrokerError(f"{label} must be owned by this user", code="UNSAFE_STATE_FILE")


def load_broker_key(path: str | Path) -> str:
    """Read the broker's mode-0600 shared HMAC key without logging its value."""
    target = Path(path).expanduser()
    _require_owner_only(target, "web search broker auth key")
    secret = target.read_text(encoding="utf-8").strip()
    if _KEY_RE.fullmatch(secret) is None:
        raise WebSearchBrokerError("web search broker auth key is invalid", code="INVALID_STATE_FILE")
    return secret


def new_call_ref() -> str:
    """Generate a fresh credential-use reference for one physical search attempt."""
    return f"credential-use:dalton-web-search-{uuid.uuid4().hex}"


def build_core_request(
    *,
    query: str,
    count: int = 5,
    date_after: Any = _OMITTED,
    date_before: Any = _OMITTED,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    call_ref: str | None = None,
    profile_id: str = DEFAULT_PROFILE_ID,
    expected_provider: Any = _OMITTED,
) -> dict[str, Any]:
    """Validate and construct an unsigned broker request dict."""
    resolved_call_ref = call_ref or new_call_ref()
    if (
        not isinstance(resolved_call_ref, str)
        or _CALL_REF_RE.fullmatch(resolved_call_ref) is None
        or len(resolved_call_ref) > 200
    ):
        raise WebSearchBrokerError("callRef is invalid", code="INVALID_REQUEST")
    if (
        not isinstance(profile_id, str)
        or _PROFILE_RE.fullmatch(profile_id) is None
        or len(profile_id) > 200
    ):
        raise WebSearchBrokerError("profileId is invalid", code="INVALID_REQUEST")
    if not isinstance(query, str) or not query.strip():
        raise WebSearchBrokerError("query is required", code="INVALID_REQUEST")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise WebSearchBrokerError("count must be a positive integer", code="INVALID_REQUEST")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
        raise WebSearchBrokerError("timeoutMs must be a positive integer", code="INVALID_REQUEST")

    clamped_timeout_ms = min(timeout_ms, DEFAULT_MAX_TIMEOUT_MS)
    request: dict[str, Any] = {
        "schemaVersion": PROTOCOL_VERSION,
        "callRef": resolved_call_ref,
        "profileId": profile_id,
        "query": query,
        "count": count,
        "timeoutMs": clamped_timeout_ms,
    }
    if expected_provider is not _OMITTED:
        if (
            not isinstance(expected_provider, str)
            or _PROVIDER_RE.fullmatch(expected_provider) is None
        ):
            raise WebSearchBrokerError("expectedProvider is invalid", code="INVALID_REQUEST")
        request["expectedProvider"] = expected_provider

    has_date_after = date_after is not _OMITTED
    has_date_before = date_before is not _OMITTED
    if has_date_after != has_date_before:
        raise WebSearchBrokerError(
            "dateAfter and dateBefore must be supplied together",
            code="INVALID_REQUEST",
        )
    if has_date_after and has_date_before:
        if (date_after is None) != (date_before is None):
            raise WebSearchBrokerError(
                "dateAfter and dateBefore must be supplied together",
                code="INVALID_REQUEST",
            )
        if not isinstance(date_after, str) or not isinstance(date_before, str):
            raise WebSearchBrokerError(
                "dateAfter and dateBefore must be YYYY-MM-DD strings",
                code="INVALID_REQUEST",
            )
        if _DATE_RE.fullmatch(date_after) is None or _DATE_RE.fullmatch(date_before) is None:
            raise WebSearchBrokerError(
                "dateAfter and dateBefore must be YYYY-MM-DD",
                code="INVALID_REQUEST",
            )
        try:
            datetime.strptime(date_after, "%Y-%m-%d")
            datetime.strptime(date_before, "%Y-%m-%d")
        except ValueError as exc:
            raise WebSearchBrokerError(
                "dateAfter and dateBefore must be valid calendar dates in YYYY-MM-DD format",
                code="INVALID_REQUEST",
            ) from exc
        if date_after > date_before:
            raise WebSearchBrokerError(
                "dateAfter must not be later than dateBefore",
                code="INVALID_REQUEST",
            )
        request["dateAfter"] = date_after
        request["dateBefore"] = date_before

    return request


def exchange_unix_socket(
    socket_path: str | Path,
    frame: bytes,
    *,
    timeout_seconds: float,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> bytes:
    """Write ``frame`` to the broker Unix socket and read the newline-terminated reply.

    Keeps the client write side open until the broker ends its side, avoiding
    the half-close race documented in the broker README.
    """
    resolved_socket = str(Path(socket_path).expanduser())
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(max(0.001, timeout_seconds))
    try:
        try:
            connection.connect(resolved_socket)
        except OSError as exc:
            raise WebSearchBrokerError(
                "web search broker socket is unavailable",
                code="BROKER_UNAVAILABLE",
            ) from exc
        connection.sendall(frame)
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = connection.recv(65536)
            except socket.timeout as exc:
                raise WebSearchBrokerError(
                    "web search broker did not reply before the deadline",
                    code="BROKER_TIMEOUT",
                ) from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_response_bytes:
                raise WebSearchBrokerError(
                    "web search broker reply exceeds the byte limit",
                    code="RESPONSE_TOO_LARGE",
                )
            chunks.append(chunk)
            if chunks[-1].endswith(b"\n"):
                break
        return b"".join(chunks)
    finally:
        connection.close()


def invoke_broker_over_socket(
    *,
    socket_path: str | Path,
    auth_key_path: str | Path,
    query: str,
    count: int = 5,
    date_after: Any = None,
    date_before: Any = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    client_id: str = DEFAULT_CLIENT_ID,
    profile_id: str = DEFAULT_PROFILE_ID,
    expected_provider: Any = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """Sign and execute one ``web_search`` request against the local broker socket."""
    core_kwargs: dict[str, Any] = {
        "query": query,
        "count": count,
        "timeout_ms": timeout_ms,
        "call_ref": new_call_ref(),
        "profile_id": profile_id,
    }
    if date_after is not None or date_before is not None:
        core_kwargs["date_after"] = date_after
        core_kwargs["date_before"] = date_before
    if expected_provider is not None:
        core_kwargs["expected_provider"] = expected_provider
    core = build_core_request(**core_kwargs)
    secret = load_broker_key(auth_key_path)
    signed = sign_request(
        core,
        secret=secret,
        client_id=client_id,
        timestamp_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        nonce=os.urandom(16).hex(),
    )
    frame = (canonical_json(signed) + "\n").encode("utf-8")
    timeout_seconds = max(5.0, core["timeoutMs"] / 1000.0 + 5.0)
    raw = exchange_unix_socket(
        socket_path,
        frame,
        timeout_seconds=timeout_seconds,
        max_response_bytes=max_response_bytes,
    )
    body = raw.rstrip(b"\n")
    if not body:
        raise WebSearchBrokerError("web search broker returned an empty reply", code="EMPTY_REPLY")
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebSearchBrokerError("web search broker reply is not JSON", code="INVALID_JSON") from exc
    if not isinstance(response, Mapping):
        raise WebSearchBrokerError("web search broker reply is not an object", code="INVALID_REPLY")
    if response.get("schemaVersion") != PROTOCOL_VERSION:
        raise WebSearchBrokerError(
            "web search broker reply uses an unsupported protocol",
            code="UNSUPPORTED_VERSION",
        )
    return dict(response)


__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MAX_TIMEOUT_MS",
    "DEFAULT_PROFILE_ID",
    "DEFAULT_QUEUE_TIMEOUT_S",
    "DEFAULT_QUEUE_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_MS",
    "HTTP_TIMEOUT_MARGIN_SECONDS",
    "PROTOCOL_VERSION",
    "TOOL_NAME",
    "WebSearchBrokerError",
    "authentication_payload",
    "build_core_request",
    "canonical_json",
    "compute_http_timeout_seconds",
    "compute_mac",
    "exchange_unix_socket",
    "invoke_broker_over_socket",
    "load_broker_key",
    "new_call_ref",
    "sign_request",
]
