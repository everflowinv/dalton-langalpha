"""Status-code extraction and retryability classification for LLM call errors.

Used by the agent's model-resilience middleware to decide whether a failed
model call is worth retrying on the same model or should move straight to the
next fallback. Mirrors the exception-chain walking in
``src/server/services/runs/stream_errors.py::classify_stream_exception`` — kept
separate because ptc_agent code must not import from src/server.
"""

from __future__ import annotations

import re

# Client-side errors that fail identically on every retry of the same model:
# bad request shape, auth, unknown model, oversized payload, unsupported
# params. 408 and 429 are absent on purpose — those are transient.
NON_RETRYABLE_STATUSES: frozenset[int] = frozenset({400, 401, 403, 404, 405, 413, 422})

_STATUS_CODE_RE = re.compile(r"\b([45]\d{2})\b")


def _iter_exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def extract_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction from an exception chain.

    Checks ``status_code`` / ``response.status_code`` attributes on every
    exception in the chain, then falls back to parsing a 4xx/5xx code out of
    the exception messages.
    """
    for current in _iter_exception_chain(exc):
        status = getattr(current, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None) if response is not None else None
        if isinstance(status, int):
            return status

    for current in _iter_exception_chain(exc):
        # A timeout never carries a provider status, and its message can carry
        # numbers that look like one: ``chunks_received=413`` read as a 413
        # would make a stalled stream look like a payload the provider refused.
        if isinstance(current, TimeoutError):
            continue
        match = _STATUS_CODE_RE.search(str(current))
        if match:
            return int(match.group(1))

    return None


def is_stalled_stream(exc: BaseException) -> bool:
    """Whether a stream went silent past its chunk bound, before or after its
    first chunk.

    Duck-typed on the ``chunks_received`` a chunk-timeout error carries, for the
    same reason as ``is_mid_stream_error``. A stall that has repeated on a fresh
    connection at the same point is the model's output, not the network, and
    another identical request stalls there again. One that never sent a chunk
    counts too: the bound is long enough for a reasoning model's first chunk,
    so each attempt costs the whole of it.
    """
    return any(
        isinstance(current, TimeoutError)
        and isinstance(getattr(current, "chunks_received", None), int)
        for current in _iter_exception_chain(exc)
    )


def is_mid_stream_error(exc: BaseException) -> bool:
    """Whether the failure was reported inside an already-established stream.

    A provider that answers HTTP 200 and then reports failure as a stream event
    sets ``mid_stream`` on its exception. Duck-typed rather than keyed to a
    provider's exception class, so this module stays provider-agnostic and the
    alternative, matching error prose, never has to be written.
    """
    return any(
        getattr(current, "mid_stream", False) is True
        for current in _iter_exception_chain(exc)
    )


def is_retryable_error(exc: BaseException, status_code: int | None = None) -> bool:
    """Whether retrying the same model could plausibly succeed.

    Errors with no extractable status (connection resets, timeouts) stay
    retryable.
    """
    if status_code is None:
        status_code = extract_status_code(exc)
    # DALTON-HOOK(xai-oauth): route Grok billing exhaustion straight to fallback; retry rate_limit only for xAI without overriding non-retryable statuses
    try:
        from src.dalton.llm.providers.xai_grok.client import classify_xai_error

        xai_kind = classify_xai_error(exc, require_xai_origin=True)
        if xai_kind == "billing":
            return False
        if xai_kind == "rate_limit" and status_code not in NON_RETRYABLE_STATUSES:
            return True
    except Exception:
        pass
    return status_code not in NON_RETRYABLE_STATUSES
