"""Dalton-owned Meta Muse client builder and session header derivation (P0N.2, G5.2 R6-10)."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import time
import uuid
from typing import Any

from src.dalton.llm.providers.muse.oauth import (
    MUSE_DEFAULT_BASE_URL,
    MUSE_PROVIDER,
    _muse_key_fp,
    get_active_muse_context,
    mark_muse_token_invalid,
)
from src.llms.extension import ChatCodexOpenAI

logger = logging.getLogger(__name__)

MUSE_CLI_USER_AGENT = (
    "muse-build/1.4.12 (non-interactive; macos-aarch64; "
    "build 80d5266d8a4cb84d6d4ec595a4ef39cc596fa128)"
)
MUSE_CLIENT_ID_HEADER = "tbh:exec"
MUSE_AGENT_ROLE_HEADER = "main"

# Fixed epoch anchor (2025-01-01T00:00:00Z in ms) used when deriving a deterministic
# 48-bit UUIDv7 timestamp from a non-UUID `cache_key`.
_MUSE_EPOCH_ANCHOR_MS = 1735689600000


def _try_parse_uuid7(val: str) -> uuid.UUID | None:
    """Return `uuid.UUID` if `val` is already a valid RFC 9562 UUIDv7 string."""
    try:
        parsed = uuid.UUID(str(val).strip())
        if parsed.version == 7:
            return parsed
    except Exception:
        pass
    return None


def _build_uuid7_from_parts(unix_ts_ms: int, rand_a: int, rand_b: int) -> str:
    """Assemble a 128-bit RFC 9562 UUIDv7 string from `(unix_ts_ms, rand_a, rand_b)`."""
    ts_48 = int(unix_ts_ms) & 0xFFFFFFFFFFFF
    ver_4 = 0x7
    ra_12 = int(rand_a) & 0x0FFF
    var_2 = 0b10
    rb_62 = int(rand_b) & 0x3FFFFFFFFFFFFFFF

    uuid_int = (
        (ts_48 << 80)
        | (ver_4 << 76)
        | (ra_12 << 64)
        | (var_2 << 62)
        | rb_62
    )
    return str(uuid.UUID(int=uuid_int))


def _new_random_uuid7() -> str:
    """Generate a fresh random RFC 9562 UUIDv7 (used when no `cache_key` is bound)."""
    uuid7_fn = getattr(uuid, "uuid7", None)
    if callable(uuid7_fn):
        return str(uuid7_fn())
    now_ms = int(time.time() * 1000)
    return _build_uuid7_from_parts(now_ms, secrets.randbits(12), secrets.randbits(62))


def derive_muse_uuid7(cache_key: str) -> str:
    """Derive a deterministic RFC 9562 UUIDv7 from `cache_key`.

    The 48-bit timestamp is deterministically derived from `SHA-256(cache_key)` (or
    extracted from a UUIDv7 prefix) rather than first-seen wall-clock time so that
    every worker process computes the identical UUIDv7 without shared state.
    """
    key = str(cache_key or "").strip()
    if not key:
        return _new_random_uuid7()

    direct_u7 = _try_parse_uuid7(key)
    if direct_u7 is not None:
        return str(direct_u7)

    digest = hashlib.sha256(f"dalton-muse-uuid7:v1:{key}".encode("utf-8")).digest()
    rand_a = int.from_bytes(digest[6:8], "big") & 0x0FFF
    rand_b = int.from_bytes(digest[8:16], "big") & 0x3FFFFFFFFFFFFFFF

    root_part = key.split(":", 1)[0].strip()
    root_u7 = _try_parse_uuid7(root_part)
    if root_u7 is not None:
        base_ts = (root_u7.int >> 80) & 0xFFFFFFFFFFFF
        offset = (int.from_bytes(digest[0:2], "big") & 0x0FFF) + 1 if ":" in key else 0
        ts_ms = (base_ts + offset) & 0xFFFFFFFFFFFF
    else:
        ts_offset = int.from_bytes(digest[0:6], "big") & 0x0000FFFFFFFF
        ts_ms = (_MUSE_EPOCH_ANCHOR_MS + ts_offset) & 0xFFFFFFFFFFFF

    return _build_uuid7_from_parts(ts_ms, rand_a, rand_b)


def build_traceparent(session_id: str, span_id: str | None = None) -> str:
    """Format W3C `traceparent` header: `00-<session-id without hyphens, 32 hex>-<random 16 hex>-01`."""
    trace_id = str(session_id).replace("-", "").strip().lower()
    if len(trace_id) != 32:
        trace_id = derive_muse_uuid7(str(session_id)).replace("-", "").lower()
    span_hex = (span_id or secrets.token_hex(8)).strip().lower()[:16].ljust(16, "0")
    return f"00-{trace_id}-{span_hex}-01"


def build_muse_session_headers(
    cache_key: str | None = None,
    *,
    root_cache_key: str | None = None,
    parent_cache_key: str | None = None,
    root_session_id: str | None = None,
    parent_thread_id: str | None = None,
    is_subagent: bool = False,
) -> dict[str, str]:
    """Build Muse Code CLI 1.4.12 UUIDv7 session affinity and tracing headers.

    - Main call:
      `x-tbh-session-id`, `x-tbh-thread-id`, `x-meta-ai-gateway-session-id`,
      and `x-tbh-root-session-id` all share the same UUIDv7, and
      `x-tbh-parent-thread-id` is omitted. When `cache_key` is empty/None,
      a fresh UUIDv7 is generated for the request.
    - Subagent call (when `is_subagent=True`, explicit parent/root keys/IDs are
      passed, or `cache_key` is narrowed as `<root>:<subagent>`):
      `x-tbh-session-id`, `x-tbh-thread-id`, and `x-meta-ai-gateway-session-id`
      take a new subagent UUIDv7; `x-tbh-root-session-id` takes the root session's
      UUIDv7; `x-tbh-parent-thread-id` takes the parent thread's UUIDv7.
    - `traceparent`: `00-<session-id without hyphens (32 hex)>-<random 16 hex>-01`.
    """
    key = str(cache_key or "").strip()
    if not key and not (root_cache_key or parent_cache_key or root_session_id or parent_thread_id or is_subagent):
        fresh_id = _new_random_uuid7()
        return {
            "x-tbh-session-id": fresh_id,
            "x-tbh-thread-id": fresh_id,
            "x-meta-ai-gateway-session-id": fresh_id,
            "x-tbh-root-session-id": fresh_id,
            "traceparent": build_traceparent(fresh_id),
        }

    session_id = derive_muse_uuid7(key) if key else _new_random_uuid7()

    inferred_root_key: str | None = root_cache_key
    inferred_parent_key: str | None = parent_cache_key
    if ":" in key and not (root_cache_key or parent_cache_key or root_session_id or parent_thread_id):
        parts = [p for p in key.split(":") if p]
        if len(parts) >= 2:
            inferred_root_key = parts[0]
            inferred_parent_key = ":".join(parts[:-1])

    subagent_mode = bool(
        is_subagent
        or root_session_id
        or parent_thread_id
        or inferred_root_key
        or inferred_parent_key
    )

    if subagent_mode:
        eff_root_id = (
            derive_muse_uuid7(root_session_id)
            if root_session_id
            else derive_muse_uuid7(
                inferred_root_key
                or inferred_parent_key
                or parent_thread_id
                or key
            )
        )
        eff_parent_id = (
            derive_muse_uuid7(parent_thread_id)
            if parent_thread_id
            else derive_muse_uuid7(
                inferred_parent_key
                or inferred_root_key
                or root_session_id
                or key
            )
        )
        if eff_parent_id == session_id and not (parent_thread_id or inferred_parent_key):
            session_id = derive_muse_uuid7(f"{key}:subagent")
        return {
            "x-tbh-session-id": session_id,
            "x-tbh-thread-id": session_id,
            "x-meta-ai-gateway-session-id": session_id,
            "x-tbh-root-session-id": eff_root_id,
            "x-tbh-parent-thread-id": eff_parent_id,
            "traceparent": build_traceparent(session_id),
        }

    return {
        "x-tbh-session-id": session_id,
        "x-tbh-thread-id": session_id,
        "x-meta-ai-gateway-session-id": session_id,
        "x-tbh-root-session-id": session_id,
        "traceparent": build_traceparent(session_id),
    }


def _extract_structured_error_code(exc: BaseException) -> str | None:
    """Extract structured error code from `exc.code` or `exc.body` without substring matching."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        top_code = body.get("code")
        if isinstance(top_code, str) and top_code:
            return top_code
        err_obj = body.get("error")
        if isinstance(err_obj, dict):
            nested_code = err_obj.get("code")
            if isinstance(nested_code, str) and nested_code:
                return nested_code
    return None


def is_muse_auth_expired_error(exc: BaseException) -> bool:
    """Return True only when `status_code == 401` or structured error code is `invalid_api_key`."""
    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status_code == 401:
        return True
    if _extract_structured_error_code(exc) == "invalid_api_key":
        return True
    return False


class MuseAuthExpiredError(RuntimeError):
    """Raised when Muse returns 401 `invalid_api_key`; classified as `auth_expired` and not retried."""

    def __init__(
        self,
        message: str = "Muse API key 已失效（401 invalid_api_key），请重新连接 Muse。",
        *,
        failed_key_fp: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = 401
        self.error_kind = "auth_expired"
        self.provider = MUSE_PROVIDER
        self.code = "invalid_api_key"
        self.failed_key_fp = str(failed_key_fp or "")[:16]


class ChatMuseOpenAI(ChatCodexOpenAI):
    """ChatCodexOpenAI subclass for Meta Muse (`/v1/responses`).

    Computes UUIDv7 session headers (`x-tbh-session-id`, `x-tbh-thread-id`,
    `x-meta-ai-gateway-session-id`, `x-tbh-root-session-id`, optional
    `x-tbh-parent-thread-id`, and per-request `traceparent`) inside
    `_get_request_payload` via `extra_headers` so `model_copy` copies sharing
    the parent's HTTP client still emit their own subagent headers on the wire.
    """

    muse_cache_key: str | None = None
    muse_root_cache_key: str | None = None
    muse_parent_cache_key: str | None = None
    muse_root_session_id: str | None = None
    muse_parent_thread_id: str | None = None
    muse_is_subagent: bool = False
    muse_user_id: str | None = None

    def model_copy(self, *, update: dict[str, Any] | None = None, deep: bool = False) -> Any:
        if isinstance(update, dict) and isinstance(update.get("model_kwargs"), dict):
            new_mk = update["model_kwargs"]
            new_ck = new_mk.get("prompt_cache_key")
            old_ck = (self.model_kwargs or {}).get("prompt_cache_key") or self.muse_cache_key
            if isinstance(new_ck, str) and new_ck and new_ck != old_ck:
                root_ck = self.muse_root_cache_key or (
                    old_ck.split(":", 1)[0] if isinstance(old_ck, str) and old_ck else new_ck.split(":", 1)[0]
                )
                parent_ck = old_ck if isinstance(old_ck, str) and old_ck else root_ck
                update = {
                    **update,
                    "muse_cache_key": new_ck,
                    "muse_root_cache_key": root_ck,
                    "muse_parent_cache_key": parent_ck,
                    "muse_is_subagent": True,
                }
        return super().model_copy(update=update, deep=deep)

    def _get_request_payload(self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        payload.pop("prompt_cache_key", None)

        session_headers = build_muse_session_headers(
            self.muse_cache_key,
            root_cache_key=self.muse_root_cache_key,
            parent_cache_key=self.muse_parent_cache_key,
            root_session_id=self.muse_root_session_id,
            parent_thread_id=self.muse_parent_thread_id,
            is_subagent=self.muse_is_subagent,
        )
        extra_headers = dict(payload.get("extra_headers") or {})
        present = {k.lower() for k in extra_headers}
        for hdr, val in session_headers.items():
            if hdr.lower() not in present:
                extra_headers[hdr] = val
        payload["extra_headers"] = extra_headers
        return payload

    def _extract_api_key_str(self) -> str:
        raw = getattr(self, "openai_api_key", None)
        if raw is None:
            return ""
        if hasattr(raw, "get_secret_value"):
            return str(raw.get_secret_value() or "")
        return str(raw)

    async def _handle_muse_401(self, exc: BaseException) -> None:
        if is_muse_auth_expired_error(exc):
            uid = self.muse_user_id or get_active_muse_context().get("user_id")
            api_key = self._extract_api_key_str()
            await mark_muse_token_invalid(
                uid,
                api_key,
                reason="invalid_api_key",
            )
            raise MuseAuthExpiredError(
                "Muse 凭据已失效（401 invalid_api_key），请重新连接 Muse。",
                failed_key_fp=_muse_key_fp(api_key),
            ) from exc

    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super()._agenerate(*args, **kwargs)
        except MuseAuthExpiredError:
            raise
        except Exception as exc:
            await self._handle_muse_401(exc)
            raise

    async def _astream(self, *args: Any, **kwargs: Any):
        try:
            # aclosing: an early-closed consumer must release the upstream response now, not at GC.
            async with contextlib.aclosing(super()._astream(*args, **kwargs)) as stream:
                async for chunk in stream:
                    yield chunk
        except MuseAuthExpiredError:
            raise
        except Exception as exc:
            await self._handle_muse_401(exc)
            raise


def build_muse_client(
    llm: Any,
    cache_key: str | None = None,
    *,
    root_cache_key: str | None = None,
    parent_cache_key: str | None = None,
    root_session_id: str | None = None,
    parent_thread_id: str | None = None,
    is_subagent: bool = False,
) -> ChatMuseOpenAI:
    """Build Muse Code (Meta subscription) LLM client using ``ChatMuseOpenAI``.

    Fixed headers (`User-Agent`, `x-client-id`, `x-tbh-agent-role`,
    `x-tbh-contributor-mode`) are placed in `default_headers`, while session
    affinity headers and `traceparent` are attached per request via
    `payload["extra_headers"]` in `ChatMuseOpenAI._get_request_payload`.
    """
    from src.llms.llm import STREAM_CHUNK_TIMEOUT_S, _merged_default_headers

    active_ctx = get_active_muse_context()
    resolved_base = llm._resolve_base_url("base_url")
    ctx_base_url = active_ctx.get("base_url")
    if ctx_base_url and resolved_base.get("base_url") in (None, "", MUSE_DEFAULT_BASE_URL):
        resolved_base["base_url"] = ctx_base_url
    elif not resolved_base.get("base_url"):
        resolved_base["base_url"] = MUSE_DEFAULT_BASE_URL

    params: dict[str, Any] = {
        "model": llm.model,
        "api_key": llm._resolve_api_key(),
        "streaming": True,
        "stream_usage": True,
        "max_retries": 5,
        "timeout": 600.0,
        "stream_chunk_timeout": STREAM_CHUNK_TIMEOUT_S,
        "muse_user_id": active_ctx.get("user_id") or None,
    }
    params.update(resolved_base)

    if llm.use_response_api:
        params["output_version"] = "responses/v1"

    params.update(llm.parameters)

    params["default_headers"] = _merged_default_headers(
        params,
        {
            "User-Agent": MUSE_CLI_USER_AGENT,
            "x-client-id": MUSE_CLIENT_ID_HEADER,
            "x-tbh-agent-role": MUSE_AGENT_ROLE_HEADER,
            "x-tbh-contributor-mode": "1" if "-contributor" in llm.model else "0",
        },
        llm.default_headers,
    )

    if cache_key:
        existing_mk = params.get("model_kwargs") or {}
        params["model_kwargs"] = {**existing_mk, "prompt_cache_key": cache_key}
        params["muse_cache_key"] = cache_key
    if root_cache_key:
        params["muse_root_cache_key"] = root_cache_key
    if parent_cache_key:
        params["muse_parent_cache_key"] = parent_cache_key

    if root_session_id:
        params["muse_root_session_id"] = root_session_id
    if parent_thread_id:
        params["muse_parent_thread_id"] = parent_thread_id
    if is_subagent:
        params["muse_is_subagent"] = True

    if llm.extra_body:
        params["extra_body"] = llm.extra_body

    return ChatMuseOpenAI(**params)
