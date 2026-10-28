"""cn-hk under researchdata 1.5: `ask` carries company and dates in the question,
current prices come from `quote`.

The gateway now refuses ticker/start/end beside an `ask`; the bridge folds them
into the question so callers written for the old contract keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import researchdata_bridge as rb  # noqa: E402


def _norm(args: dict) -> tuple[dict, dict | None]:
    normalized, _view, err = rb._normalize_and_validate_call_args("finance", dict(args))
    return normalized, err


def test_ask_folds_ticker_and_dates_into_the_question() -> None:
    normalized, err = _norm(
        {
            "route": "cn-hk",
            "operation": "ask",
            "ticker": "0700.HK",
            "start": "2026-01-01",
            "end": "2026-06-30",
            "query": "游戏收入与毛利率",
        }
    )
    assert err is None
    for k in ("ticker", "start", "end"):
        assert k not in normalized
    assert normalized["query"].startswith("00700 2026-01-01 2026-06-30 ")
    assert normalized["query"].endswith("游戏收入与毛利率")


def test_ask_does_not_repeat_a_ticker_already_in_the_question() -> None:
    normalized, err = _norm(
        {"route": "cn-hk", "operation": "ask", "ticker": "00700", "query": "腾讯00700 回购"}
    )
    assert err is None
    assert normalized["query"] == "腾讯00700 回购"


def test_quote_needs_a_ticker_and_catalog_examples_use_the_new_contract() -> None:
    _, err = _norm({"route": "cn-hk", "operation": "quote"})
    assert err is not None
    normalized, err = _norm({"route": "cn-hk", "operation": "quote", "ticker": "0700.HK"})
    assert err is None
    assert normalized["ticker"] == "00700"
    examples = rb.ROUTE_CONTRACT_SPECS["cn-hk"]["examples"]
    assert any('operation="quote"' in e for e in examples)
    assert not any('operation="ask", ticker=' in e for e in examples)


def test_ask_folds_a_ticker_that_only_appears_inside_another_code() -> None:
    normalized, err = _norm(
        {"route": "cn-hk", "operation": "ask", "ticker": "0700.HK", "query": "与300700比较近三年毛利率"}
    )
    assert err is None
    assert normalized["query"] == "00700 与300700比较近三年毛利率"


def test_filings_with_query_and_quote_with_dates_are_refused_in_the_bridge() -> None:
    _, err = _norm(
        {
            "route": "cn-hk",
            "operation": "filings",
            "ticker": "00700",
            "start": "2026-01-01",
            "end": "2026-06-30",
            "query": "回购",
        }
    )
    assert err is not None and err["unsupported_parameters"] == ["query"]
    _, err = _norm({"route": "cn-hk", "operation": "quote", "ticker": "00700", "start": "2026-01-01"})
    assert err is not None and err["unsupported_parameters"] == ["start"]
