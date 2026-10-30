"""Quota/billing refusals classify as exhausted, not retryable — R3a #1.

Every message string below is the provider's own wording, taken from the
10/30 production log export (`review-24h/_data/logs_24h.txt`), so the match
is proven against what the account actually received.
"""

from __future__ import annotations

import math

from src.llms.error_classification import (
    extract_status_code,
    is_retryable_error,
    quota_exhaustion_seconds,
)

# L:2947 — grok-4.7 via cli-chat-proxy.grok.com, 1,922 times in one window.
GROK_402_MESSAGE = (
    "Error code: 402 - {'error': 'Grok Build usage balance exhausted'}"
)

# L:16487 — gemini-4-argon-high-oauth via Antigravity, 34 times: a quota 429
# whose own body says it lifts only hours later.
ANTIGRAVITY_QUOTA_429_MESSAGE = (
    "Antigravity API error 429: Individual quota reached. Please upgrade "
    "your subscription to increase your limits. Resets in 6h49m18s."
)


def _exc_with_status(message: str, status: int) -> Exception:
    # The shape the OpenAI-family SDKs leave in the chain: the status as an
    # int attribute the walker reads before the prose.
    exc = Exception(message)
    exc.status_code = status
    return exc


def test_the_production_grok_402_is_not_retryable() -> None:
    exc = _exc_with_status(GROK_402_MESSAGE, 402)
    assert extract_status_code(exc) == 402
    assert is_retryable_error(exc) is False


def test_a_402_without_any_known_prose_is_still_not_retryable() -> None:
    """The status alone decides: no provider's balance refills inside a
    backoff window, whatever the wording."""
    assert is_retryable_error(_exc_with_status("Error code: 402", 402)) is False


def test_the_grok_balance_prose_marks_billing_for_xai() -> None:
    from src.dalton.llm.providers.xai_grok.client import (
        classify_xai_error,
        mark_xai_exception,
    )

    exc = mark_xai_exception(Exception(GROK_402_MESSAGE))
    assert classify_xai_error(exc, require_xai_origin=True) == "billing"
    assert quota_exhaustion_seconds(exc) == math.inf


def test_the_grok_402_is_exhausted_even_unstamped() -> None:
    """The status route answers even when the stamp never landed (a raise
    outside the client's own stream wrappers)."""
    assert (
        quota_exhaustion_seconds(_exc_with_status(GROK_402_MESSAGE, 402))
        == math.inf
    )


def test_the_production_antigravity_quota_429_names_its_horizon() -> None:
    from src.dalton.llm.providers.antigravity.client import AntigravityApiError

    exc = AntigravityApiError(
        ANTIGRAVITY_QUOTA_429_MESSAGE,
        status=429,
        body=ANTIGRAVITY_QUOTA_429_MESSAGE,
    )
    # 6h49m18s
    assert quota_exhaustion_seconds(exc) == 6 * 3600 + 49 * 60 + 18


def test_an_antigravity_quota_429_without_a_readable_reset_is_run_long() -> None:
    from src.dalton.llm.providers.antigravity.client import AntigravityApiError

    exc = AntigravityApiError(
        "Antigravity API error 429: Individual quota reached. Please upgrade "
        "your subscription to increase your limits.",
        status=429,
    )
    assert quota_exhaustion_seconds(exc) == math.inf


def test_a_plain_429_stays_retryable_and_horizonless() -> None:
    """A short rate limit — no quota wording, no horizon — keeps today's
    handling: retry it, and nothing benches the model for the run."""
    exc = _exc_with_status(
        "Error code: 429 - {'error': 'Rate limit exceeded. Please try again.'}",
        429,
    )
    assert quota_exhaustion_seconds(exc) is None
    assert is_retryable_error(exc) is True


def test_a_quota_429_whose_reset_is_inside_the_budget_keeps_retrying() -> None:
    from src.dalton.llm.providers.antigravity.client import AntigravityApiError

    exc = AntigravityApiError(
        "Antigravity API error 429: Individual quota reached. Please upgrade "
        "your subscription to increase your limits. Resets in 8s.",
        status=429,
    )
    # The horizon is short; only the middleware's budget comparison may act
    # on it — classification itself must not mark it non-retryable.
    assert quota_exhaustion_seconds(exc) == 8.0
    assert is_retryable_error(exc) is True


def test_a_numeric_retry_after_header_on_a_429_is_a_horizon() -> None:
    class _Headers(dict):
        pass

    class _Response:
        def __init__(self) -> None:
            self.status_code = 429
            self.headers = _Headers({"Retry-After": "120"})

    exc = Exception("Error code: 429")
    exc.response = _Response()
    assert quota_exhaustion_seconds(exc) == 120.0


def test_no_status_and_no_quota_prose_answers_none() -> None:
    assert quota_exhaustion_seconds(Exception("connection reset")) is None
    assert (
        quota_exhaustion_seconds(_exc_with_status("Error code: 500", 500)) is None
    )
