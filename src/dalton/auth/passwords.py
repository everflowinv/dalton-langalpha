"""Versioned password hashing using stdlib ``hashlib.scrypt`` with bounded off-loop execution.

Stored format::

    scrypt$v1$n=32768,r=8,p=1$<salt_b64>$<hash_b64>

Uses random 16-byte salts, constant-time digest comparison via ``hmac.compare_digest``,
and a concurrency-bounded ``ThreadPoolExecutor`` for async callers so CPU/memory-heavy
scrypt calls never block the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import os
import secrets
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from fastapi import HTTPException

SCRYPT_ALGORITHM = "scrypt"
SCRYPT_VERSION = "v1"
SCRYPT_N = 2**15  # 32768 (~32 MiB)
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16
# 128 * N * R = 33,554,432 bytes (~32 MiB) plus OpenSSL overhead; 64 MiB leaves safe headroom.
SCRYPT_MAXMEM = 64 * 1024 * 1024

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 256

# Strict safety bounds when verifying stored hashes to guard against malformed parameters.
_MIN_N = 2**10
_MAX_N = 2**18
_MIN_R = 1
_MAX_R = 32
_MIN_P = 1
_MAX_P = 8

_scrypt_executor: ThreadPoolExecutor | None = None
_scrypt_inflight_lock = threading.Lock()
_scrypt_inflight: int = 0
DEFAULT_SCRYPT_MAX_QUEUE = 16


def get_scrypt_max_workers() -> int:
    """Return bounded concurrency cap for the off-loop scrypt executor (1..8, default <=4)."""
    raw = os.getenv("DALTON_SCRYPT_MAX_WORKERS", "").strip()
    if raw:
        try:
            return max(1, min(8, int(raw)))
        except ValueError:
            pass
    return min(4, max(1, os.cpu_count() or 2))


def get_scrypt_max_queue() -> int:
    """Return bounded wait-queue cap for the off-loop scrypt executor (0..256, default 16)."""
    raw = os.getenv("DALTON_SCRYPT_MAX_QUEUE", "").strip()
    if raw:
        try:
            return max(0, min(256, int(raw)))
        except ValueError:
            pass
    return DEFAULT_SCRYPT_MAX_QUEUE


def get_scrypt_max_pending() -> int:
    """Maximum total running + queued scrypt jobs before rejecting immediately with HTTP 429."""
    return get_scrypt_max_workers() + get_scrypt_max_queue()


def get_scrypt_executor() -> ThreadPoolExecutor:
    """Return the singleton bounded ``ThreadPoolExecutor`` used for scrypt hashing."""
    global _scrypt_executor
    if _scrypt_executor is None:
        _scrypt_executor = ThreadPoolExecutor(
            max_workers=get_scrypt_max_workers(),
            thread_name_prefix="dalton-scrypt",
        )
    return _scrypt_executor


@contextmanager
def acquire_scrypt_slot() -> Iterator[None]:
    """Atomically reserve one capacity slot in the bounded scrypt queue, or raise HTTP 429 immediately.

    Callers such as local login and password change acquire this slot BEFORE recording
    any rate-limit attempts so a saturated scrypt executor rejects immediately with 429
    without touching IP/account failure counters or re-arming expired lockouts.
    """
    global _scrypt_inflight
    max_pending = get_scrypt_max_pending()
    with _scrypt_inflight_lock:
        if _scrypt_inflight >= max_pending:
            raise HTTPException(
                status_code=429,
                detail="Authentication service is busy; please retry shortly",
                headers={"Retry-After": "1"},
            )
        _scrypt_inflight += 1
    try:
        yield
    finally:
        with _scrypt_inflight_lock:
            _scrypt_inflight = max(0, _scrypt_inflight - 1)


async def _run_in_bounded_scrypt_executor(fn, *, slot_held: bool = False):
    """Run ``fn`` in the scrypt executor with a hard upper bound on queued + running jobs.

    When ``slot_held=True``, the caller already owns a slot from :func:`acquire_scrypt_slot`
    and will not be double-counted or rejected with 429.
    """
    loop = asyncio.get_running_loop()
    if slot_held:
        return await loop.run_in_executor(get_scrypt_executor(), fn)
    with acquire_scrypt_slot():
        return await loop.run_in_executor(get_scrypt_executor(), fn)


def validate_password_policy(password: str) -> None:
    """Validate password length constraints; raises ``ValueError`` with user-facing message."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64decode(encoded: str) -> bytes:
    return base64.urlsafe_b64decode(encoded.encode("ascii"))


def hash_password(
    password: str,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
    salt: bytes | None = None,
) -> str:
    """Hash ``password`` into a versioned ``scrypt$v1$...`` string."""
    if not isinstance(password, str) or not password:
        raise ValueError("Password must be a non-empty string")
    raw_salt = secrets.token_bytes(SCRYPT_SALT_BYTES) if salt is None else salt
    if len(raw_salt) < SCRYPT_SALT_BYTES:
        raise ValueError(f"Salt must be at least {SCRYPT_SALT_BYTES} bytes")
    maxmem = max(SCRYPT_MAXMEM, 128 * n * r * 2)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=raw_salt,
        n=n,
        r=r,
        p=p,
        maxmem=maxmem,
        dklen=SCRYPT_DKLEN,
    )
    return (
        f"{SCRYPT_ALGORITHM}${SCRYPT_VERSION}$n={n},r={r},p={p}"
        f"${_b64encode(raw_salt)}${_b64encode(derived)}"
    )


def _parse_scrypt_params(param_str: str) -> tuple[int, int, int] | None:
    parts = param_str.split(",")
    parsed: dict[str, int] = {}
    for item in parts:
        if "=" not in item:
            return None
        k, v = item.split("=", 1)
        try:
            parsed[k] = int(v)
        except ValueError:
            return None
    if set(parsed.keys()) != {"n", "r", "p"}:
        return None
    n, r, p = parsed["n"], parsed["r"], parsed["p"]
    if n < _MIN_N or n > _MAX_N or (n & (n - 1)) != 0:
        return None
    if not (_MIN_R <= r <= _MAX_R) or not (_MIN_P <= p <= _MAX_P):
        return None
    return n, r, p


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify ``password`` against a versioned ``scrypt$v1$...`` hash in constant time."""
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    if not password or len(password) > MAX_PASSWORD_LENGTH:
        return False

    segments = stored_hash.split("$")
    if len(segments) != 5:
        return False
    algo, version, param_str, salt_b64, digest_b64 = segments
    if algo != SCRYPT_ALGORITHM or version != SCRYPT_VERSION:
        return False

    params = _parse_scrypt_params(param_str)
    if params is None:
        return False
    n, r, p = params

    try:
        salt = _b64decode(salt_b64)
        expected = _b64decode(digest_b64)
    except Exception:
        return False
    if not salt or not expected:
        return False

    try:
        maxmem = max(SCRYPT_MAXMEM, 128 * n * r * 2)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=maxmem,
            dklen=len(expected),
        )
    except (ValueError, OverflowError):
        return False

    return hmac.compare_digest(actual, expected)


async def hash_password_async(
    password: str,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
    salt: bytes | None = None,
    slot_held: bool = False,
) -> str:
    """Run :func:`hash_password` inside the bounded scrypt thread-pool executor."""
    fn = functools.partial(hash_password, password, n=n, r=r, p=p, salt=salt)
    return await _run_in_bounded_scrypt_executor(fn, slot_held=slot_held)


async def verify_password_async(
    password: str, stored_hash: str, *, slot_held: bool = False
) -> bool:
    """Run :func:`verify_password` inside the bounded scrypt thread-pool executor."""
    fn = functools.partial(verify_password, password, stored_hash)
    return await _run_in_bounded_scrypt_executor(fn, slot_held=slot_held)


# Pre-computed valid scrypt hash used to equalize login timing when a user is not found.
DUMMY_PASSWORD_HASH: str = hash_password(
    "dalton-dummy-password-never-used",
    salt=b"\x00" * SCRYPT_SALT_BYTES,
)
