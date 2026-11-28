"""Per-call caller identity for sandbox code jobs (B5).

The researchdata bridge stamps every AlphaEngine call it serves into a usage
ledger and enforces per-caller concurrency / automation priority, keyed by who
asked for it. The only hop that knows the caller is the tool call itself (its
``RunnableConfig``); the job's environment is composed deep inside the sandbox
layer, which is generic upstream code with no notion of a user. The identity
therefore rides a ``ContextVar`` — the same pattern ``ProjectContext`` uses for
the workspace folder — set around the code/bash run and read where the job's
env is built.

Because sandbox code can mutate its own environment and request headers, the
backend signs ``{user, workspace, thread, run_origin, exp}`` into an opaque
HMAC-SHA256 token (``DALTON_CALLER_TOKEN``) using a subkey derived via HKDF
from ``DALTON_BRIDGE_ADMIN_TOKEN`` (which never enters the sandbox). The bridge
only trusts workspace/user/run_origin from a verified, unexpired token.
"""

from __future__ import annotations

import base64
import contextlib
import contextvars
import hashlib
import hmac
import json
import math
import time
from collections.abc import Iterator, Mapping
from typing import Any

__all__ = ["bound_caller", "caller_env", "sign_caller_token", "verify_caller_token"]

_CALLER_TOKEN_HKDF_INFO = b"dalton-bridge-caller-v1"
_DEFAULT_EXP_FALLBACK_S = 21600

_current: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "dalton_caller_identity", default=None
)

_ENV_NAMES: tuple[tuple[str, str], ...] = (
    ("user", "DALTON_CALLER_USER"),
    ("workspace", "DALTON_CALLER_WORKSPACE"),
    ("thread", "DALTON_CALLER_THREAD"),
)


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    # A value HTTP cannot frame, or that could smuggle another header, is
    # dropped rather than trimmed into something the caller never said.
    if any(ch in text for ch in "\r\n\t "):
        return ""
    return text


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode_strict(segment: str) -> bytes:
    if not segment or "=" in segment:
        raise ValueError("invalid base64url segment")
    padded = segment + "=" * ((4 - len(segment) % 4) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    if _b64url_encode(decoded) != segment:
        raise ValueError("non-canonical base64url segment")
    return decoded


def _derive_caller_hmac_key(admin_token: str | None = None) -> bytes | None:
    """Derive a dedicated HMAC-SHA256 subkey from admin token via HKDF."""
    if admin_token is None:
        from src.dalton.settings import (
            get_dalton_bridge_admin_token,
            get_dalton_research_admin_token,
        )

        secret = get_dalton_bridge_admin_token() or get_dalton_research_admin_token()
    else:
        secret = str(admin_token).strip()
    if not secret:
        return None
    # RFC 5869 HKDF-SHA256 with HashLen zero-byte salt and domain-separated info.
    prk = hmac.new(b"\x00" * 32, secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, _CALLER_TOKEN_HKDF_INFO + b"\x01", hashlib.sha256).digest()


def _default_exp() -> int:
    try:
        from src.config.settings import get_workflow_timeout

        ttl = int(get_workflow_timeout())
    except Exception:
        ttl = _DEFAULT_EXP_FALLBACK_S
    if ttl <= 0:
        ttl = _DEFAULT_EXP_FALLBACK_S
    return int(math.ceil(time.time())) + ttl


def _coerce_exp(exp: Any) -> int:
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and math.isfinite(exp):
        return int(math.ceil(float(exp)))
    return _default_exp()


def sign_caller_token(
    claims: Mapping[str, Any],
    *,
    exp: int | float | None = None,
    admin_token: str | None = None,
) -> str | None:
    """Sign caller identity and execution claims with the derived HKDF subkey.

    Payload fields: ``user``, ``workspace``, ``thread``, ``run_origin``,
    ``run_id``, ``turn_origin_automation_id``, ``automation_execution_id``,
    and ``exp``.
    """
    hmac_key = _derive_caller_hmac_key(admin_token)
    if hmac_key is None:
        return None
    raw_origin = _clean(claims.get("run_origin"))
    payload = {
        "user": _clean(claims.get("user")),
        "workspace": _clean(claims.get("workspace")),
        "thread": _clean(claims.get("thread")),
        "run_origin": "automation" if raw_origin == "automation" else "",
        "run_id": _clean(claims.get("run_id")),
        "turn_origin_automation_id": _clean(claims.get("turn_origin_automation_id")),
        "automation_execution_id": _clean(claims.get("automation_execution_id")),
        "exp": _coerce_exp(exp if exp is not None else claims.get("exp")),
    }
    payload_bytes = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(hmac_key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_caller_token(
    token: str,
    *,
    admin_token: str | None = None,
    now_ts: float | None = None,
) -> dict[str, str] | None:
    """Verify HMAC-SHA256 signature and ``exp`` on an opaque ``DALTON_CALLER_TOKEN``.

    Returns the full claim dictionary ``{user, workspace, thread, run_origin,
    run_id, turn_origin_automation_id, automation_execution_id}`` on success,
    or ``None`` on any format, signature, or expiry failure.
    """
    try:
        hmac_key = _derive_caller_hmac_key(admin_token)
        if hmac_key is None:
            return None
        raw = str(token or "").strip()
        parts = raw.split(".")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        payload_b64, sig_b64 = parts[0], parts[1]
        payload_ascii = payload_b64.encode("ascii")
        supplied_sig = _b64url_decode_strict(sig_b64)
        expected_sig = hmac.new(hmac_key, payload_ascii, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied_sig, expected_sig):
            return None
        claims = json.loads(_b64url_decode_strict(payload_b64).decode("utf-8"))
        if not isinstance(claims, dict):
            return None
        exp = claims.get("exp")
        if (
            not isinstance(exp, (int, float))
            or isinstance(exp, bool)
            or not math.isfinite(exp)
        ):
            return None
        now = float(now_ts) if now_ts is not None else time.time()
        if now > float(exp):
            return None
        raw_origin = _clean(claims.get("run_origin")).lower()
        return {
            "user": _clean(claims.get("user")),
            "workspace": _clean(claims.get("workspace")),
            "thread": _clean(claims.get("thread")),
            "run_origin": "automation" if raw_origin == "automation" else "",
            "run_id": _clean(claims.get("run_id")),
            "turn_origin_automation_id": _clean(claims.get("turn_origin_automation_id")),
            "automation_execution_id": _clean(claims.get("automation_execution_id")),
        }
    except Exception:
        return None


@contextlib.contextmanager
def bound_caller(
    user_id: Any,
    workspace_id: Any,
    thread_id: Any,
    *,
    run_origin: Any = None,
    run_id: Any = None,
    turn_origin_automation_id: Any = None,
    automation_execution_id: Any = None,
    exp: int | float | None = None,
) -> Iterator[None]:
    """Bind the caller's identity for the awaited body, best effort.

    Never raises and never widens: an unset identity binds nothing, and the
    reset is unconditional so one call cannot leak into the next.
    """
    values: dict[str, Any] = {
        key: cleaned
        for key, raw in (
            ("user", user_id),
            ("workspace", workspace_id),
            ("thread", thread_id),
            ("run_origin", run_origin),
            ("run_id", run_id),
            ("turn_origin_automation_id", turn_origin_automation_id),
            ("automation_execution_id", automation_execution_id),
        )
        if (cleaned := _clean(raw))
    }
    if not values:
        yield
        return
    values["exp"] = _coerce_exp(exp)
    token = _current.set(values)
    try:
        yield
    finally:
        _current.reset(token)


def caller_env() -> dict[str, str]:
    """The bound identity as the job env's ``DALTON_CALLER_*`` variables."""
    current = _current.get() or {}
    env: dict[str, str] = {
        env_name: str(current[key])
        for key, env_name in _ENV_NAMES
        if current.get(key)
    }
    if current:
        signed = sign_caller_token(current)
        if signed:
            env["DALTON_CALLER_TOKEN"] = signed
    return env
