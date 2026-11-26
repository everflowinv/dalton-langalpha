"""Regression tests for Item 1: SEC financials Timestamp JSON keys, serialization vs argument errors, and non-SEC HK/KR list-filings."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
FINDATA_SCRIPTS = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "research_runtime"
    / "skills"
    / "findata-analyst"
    / "scripts"
)
DALTON_SEC_DIR = REPO_ROOT / "plugins" / "dalton_sec"


def _load_bridge():
    mod_name = "researchdata_bridge_item1_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if str(SCRIPTS_DALTON) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DALTON))
    spec = importlib.util.spec_from_file_location(
        mod_name, SCRIPTS_DALTON / "researchdata_bridge.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_findata_cli():
    mod_name = "findata_cli_item1_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if str(FINDATA_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(FINDATA_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        mod_name, FINDATA_SCRIPTS / "findata_cli.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_dalton_sec():
    mod_name = "dalton_sec_mcp_item1_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    if str(DALTON_SEC_DIR) not in sys.path:
        sys.path.insert(0, str(DALTON_SEC_DIR))
    spec = importlib.util.spec_from_file_location(
        mod_name, DALTON_SEC_DIR / "dalton_sec_mcp_server.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _StubYFTicker:
    def __init__(self, inc: pd.DataFrame, bal: pd.DataFrame, cf: pd.DataFrame) -> None:
        self.income_stmt = inc
        self.balance_sheet = bal
        self.cashflow = cf
        self.quarterly_income_stmt = inc
        self.quarterly_balance_sheet = bal
        self.quarterly_cashflow = cf


class _StubStmt:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _StubFinancials:
    def __init__(self, df: pd.DataFrame) -> None:
        self.income_statement = _StubStmt(df)
        self.balance_sheet = _StubStmt(df)
        self.cash_flow_statement = _StubStmt(df)


class _StubFilingObj:
    def __init__(self, df: pd.DataFrame) -> None:
        self.financials = _StubFinancials(df)


class _StubFiling:
    def __init__(self, df: pd.DataFrame) -> None:
        self.form = "10-K"
        self.filing_date = "2026-02-15"
        self.report_date = "2025-12-31"
        self.accession_no = "0001639920-26-000001"
        self._df = df

    def obj(self) -> _StubFilingObj:
        return _StubFilingObj(self._df)


class _StubFilingsList:
    def __init__(self, items: list[_StubFiling]) -> None:
        self._items = items

    def __len__(self) -> int:
        return len(self._items)

    def head(self, n: int) -> list[_StubFiling]:
        return self._items[:n]


class _StubCompany:
    def __init__(self, ticker: str, filings: _StubFilingsList) -> None:
        self.ticker = ticker
        self._filings = filings

    def get_filings(self, form: str | None = None) -> _StubFilingsList:
        return self._filings


def test_yf_financials_converts_timestamp_columns_and_index_to_iso_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DataFrames with pandas.Timestamp columns AND Timestamp index serialize cleanly to ISO date strings."""
    cli = _load_findata_cli()
    col_dates = [pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31")]
    inc_df = pd.DataFrame(
        [[15000.0, 13000.0], [3200.0, 2100.0]],
        index=["Total Revenue", "Net Income"],
        columns=col_dates,
    )
    idx_ts_df = pd.DataFrame(
        [[50000.0, 42000.0]],
        index=[pd.Timestamp("2025-12-31")],
        columns=[pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31")],
    )
    stub_ticker = _StubYFTicker(inc_df, idx_ts_df, inc_df)

    def _make_ticker(symbol: str) -> _StubYFTicker:
        return stub_ticker

    ticker_factory = create_autospec(_make_ticker, side_effect=_make_ticker)
    monkeypatch.setattr(cli, "HAS_YFINANCE", True)
    monkeypatch.setattr(cli.yf, "Ticker", ticker_factory)

    res = cli._yf_financials("SPOT", "10-K")
    assert res["ok"] is True
    # Must serialize with standard json.dumps (no default=str needed for keys!)
    dumped = json.dumps(res, ensure_ascii=False)
    parsed = json.loads(dumped)
    income = parsed["rows"][0]["statements"]["income"]
    assert "2025-12-31" in income
    assert "2024-12-31" in income
    assert income["2025-12-31"]["Total Revenue"] == 15000.0

    balance = parsed["rows"][0]["statements"]["balance"]
    assert "2025-12-31" in balance
    assert "2025-12-31" in balance["2025-12-31"]


def test_cmd_financials_converts_edgar_dataframe_timestamp_keys_to_iso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EDGAR statement DataFrames with pandas.Timestamp columns serialize with ISO date string keys."""
    cli = _load_findata_cli()
    df = pd.DataFrame(
        [
            {"label": "Revenue", pd.Timestamp("2025-12-31"): 100.0, pd.Timestamp("2024-12-31"): 80.0},
        ],
        index=[pd.Timestamp("2025-12-31")],
    )
    company_inst = _StubCompany("PDD", _StubFilingsList([_StubFiling(df)]))

    def _make_company(ticker: str) -> _StubCompany:
        return company_inst

    monkeypatch.setattr(cli, "Company", create_autospec(_make_company, side_effect=_make_company))
    args = argparse.Namespace(ticker="PDD", form="20-F", limit=1, statement="income", as_of=None)
    res = cli.cmd_financials(args)
    assert res["ok"] is True
    dumped = json.dumps(res, ensure_ascii=False)
    parsed = json.loads(dumped)
    rec = parsed["rows"][0]["statements"]["income"][0]
    assert "2025-12-31" in rec
    assert "2024-12-31" in rec
    assert rec["2025-12-31"] == 100.0


def test_bridge_separates_internal_serialization_error_from_argument_error() -> None:
    """Internal JSON Timestamp-key error must NOT be misclassified as 参数错误 (different error_code and copy)."""
    rb = _load_bridge()
    ser_payload: dict[str, Any] = {
        "ok": False,
        "status": "error",
        "error": "keys must be str, int, float, bool or None, not Timestamp",
    }
    rb._apply_error_and_empty_contract(
        "finance",
        "sec",
        "financials",
        ser_payload,
        {"route": "sec", "operation": "financials", "ticker": "SPOT"},
    )
    arg_payload: dict[str, Any] = {
        "ok": False,
        "status": "invalid_arguments",
        "error_code": "invalid_param_value",
        "error": "statement 须为 all|income|balance|cash",
    }
    rb._apply_error_and_empty_contract(
        "finance",
        "sec",
        "financials",
        arg_payload,
        {"route": "sec", "operation": "financials", "ticker": "SPOT"},
    )

    assert arg_payload["error_category"] == "参数错误"
    assert ser_payload["error_category"] != "参数错误"
    assert ser_payload.get("error_code") == "internal_serialization_error"
    assert ser_payload["error_code"] != arg_payload.get("error_code")
    assert ser_payload["hint"] != arg_payload["hint"]
    assert "序列化" in ser_payload["error"] or "序列化" in ser_payload["hint"]

    # Even if an upstream caller pre-labeled error_category="参数错误", serialization signal overrides to "内部错误".
    prelabeled_ser: dict[str, Any] = {
        "ok": False,
        "status": "error",
        "error_category": "参数错误",
        "error": "keys must be str, int, float, bool or None, not Timestamp",
    }
    rb._apply_error_and_empty_contract(
        "finance",
        "sec",
        "financials",
        prelabeled_ser,
        {"route": "sec", "operation": "financials", "ticker": "SPOT"},
    )
    assert prelabeled_ser["error_category"] == "内部错误"
    assert prelabeled_ser["error_code"] == "internal_serialization_error"


def test_findata_cli_error_json_separates_serialization_error_from_argument_error() -> None:
    cli = _load_findata_cli()
    ser_err = cli._error_json(TypeError("keys must be str, int, float, bool or None, not Timestamp"))
    arg_err = cli._error_json(ValueError("invalid form argument"))
    assert ser_err["error_code"] == "internal_serialization_error"
    assert ser_err["error_code"] != arg_err.get("error_code")
    assert ser_err["hint"] != arg_err["hint"]


@pytest.mark.parametrize("ticker", ["0700.HK", "00700", "005930.KS", "035420.KQ"])
def test_bridge_sec_list_filings_rejects_non_sec_hk_kr_without_calling_sec(
    ticker: str,
) -> None:
    """HK / KR tickers without SEC CIK return an explicit '该证券不在 SEC 申报' result and never call stdio/SEC."""
    rb = _load_bridge()
    cfg = rb.BridgeConfig()
    bridge = rb.ResearchDataBridge(cfg)
    stdio_spy = create_autospec(
        bridge._run_stdio_rpc,
        side_effect=AssertionError("SEC stdio must not be called"),
    )
    bridge._run_stdio_rpc = stdio_spy

    rpc_resp = asyncio.run(
        bridge.handle_tools_call(
            req_id=1,
            params={
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": ticker,
                    "limit": 5,
                },
            },
        )
    )
    assert stdio_spy.call_count == 0, f"SEC stdio should not be called for non-SEC ticker {ticker}"
    text = rpc_resp["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload.get("error_code") == "not_sec_filer" or payload.get("status") == "not_sec_filer"
    assert "不在 SEC 申报" in str(payload.get("error") or payload.get("message") or "")
    assert payload.get("sec_filer") is False


@pytest.mark.parametrize("ticker", ["0700.HK", "00700", "005930.KS", "035420.KQ"])
def test_findata_cli_list_filings_rejects_non_sec_hk_kr_without_calling_edgar(
    ticker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_findata_cli()

    def _boom_company(sym: str) -> Any:
        raise AssertionError(f"EDGAR Company() must not be called for {sym}")

    company_spy = create_autospec(_boom_company, side_effect=_boom_company)
    monkeypatch.setattr(cli, "Company", company_spy)

    args = argparse.Namespace(ticker=ticker, form="", limit=5, as_of=None)
    res = cli.cmd_list_filings(args)
    assert company_spy.call_count == 0
    assert res["ok"] is False
    assert res.get("error_code") == "not_sec_filer"
    assert res.get("sec_filer") is False
    assert "不在 SEC 申报" in str(res.get("error") or "")


@pytest.mark.parametrize("ticker", ["0700.HK", "00700", "005930.KS", "035420.KQ"])
def test_dalton_sec_mcp_list_filings_rejects_non_sec_hk_kr_without_requesting_sec(
    ticker: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sec_mod = _load_dalton_sec()
    resolve_spy = create_autospec(sec_mod.resolve_cik, side_effect=AssertionError("must not call SEC"))
    fetch_spy = create_autospec(sec_mod.fetch_submissions, side_effect=AssertionError("must not call SEC"))
    monkeypatch.setattr(sec_mod, "resolve_cik", resolve_spy)
    monkeypatch.setattr(sec_mod, "fetch_submissions", fetch_spy)

    res = sec_mod.list_filings(ticker=ticker, limit=5)
    assert resolve_spy.call_count == 0
    assert fetch_spy.call_count == 0
    assert res.get("error_code") == "not_sec_filer"
    assert res.get("sec_filer") is False
    assert "不在 SEC 申报" in str(res.get("detail") or "")


def test_dalton_sec_mcp_list_filings_resolves_dual_listed_9988_hk_via_baba_cik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dual-listed 9988.HK maps to BABA (CIK 0001577552) and MUST NOT be rejected as non-SEC."""
    sec_mod = _load_dalton_sec()
    fake_sub = {
        "name": "Alibaba Group Holding Limited",
        "filings": {
            "recent": {
                "accessionNumber": ["0001577552-26-000010"],
                "form": ["20-F"],
                "filingDate": ["2026-06-20"],
                "reportDate": ["2026-03-31"],
                "primaryDocument": ["baba-20260331.htm"],
            }
        },
    }
    fetch_spy = create_autospec(sec_mod.fetch_submissions, return_value=fake_sub)
    monkeypatch.setattr(sec_mod, "fetch_submissions", fetch_spy)

    res = sec_mod.list_filings(ticker="9988.HK", limit=5)
    fetch_spy.assert_called_once_with("0001577552")
    assert res.get("ok") is True or isinstance(res.get("data"), list)
    rows = res.get("data") or []
    assert len(rows) == 1
    assert rows[0]["cik"] == "0001577552"
    assert rows[0]["form"] == "20-F"


def test_bridge_sec_list_filings_maps_dual_listed_9988_hk_to_baba_before_short_circuit() -> None:
    """Bridge finance(route='sec', operation='list-filings', ticker='9988.HK') resolves 9988.HK -> BABA
    and forwards to SEC stdio instead of rejecting as not_sec_filer."""
    rb = _load_bridge()
    cfg = rb.BridgeConfig()
    bridge = rb.ResearchDataBridge(cfg)
    captured: list[dict[str, Any]] = []

    async def _fake_stdio(
        requests: list[dict[str, Any]],
        timeout_s: float,
    ) -> list[dict[str, Any]]:
        captured.extend(requests)
        req_id = requests[0].get("id") if requests else 2
        return [
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "ok": True,
                                    "ticker": "BABA",
                                    "rows": [{"form": "20-F", "filing_date": "2026-06-20"}],
                                }
                            ),
                        }
                    ],
                    "isError": False,
                },
            }
        ]

    stdio_spy = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_stdio)
    bridge._run_stdio_rpc = stdio_spy

    rpc_resp = asyncio.run(
        bridge.handle_tools_call(
            req_id=2,
            params={
                "name": "finance",
                "arguments": {
                    "route": "sec",
                    "operation": "list-filings",
                    "ticker": "9988.HK",
                    "limit": 5,
                },
            },
        )
    )
    assert stdio_spy.call_count == 1
    forwarded_args = ((captured[0].get("params") or {}).get("arguments")) or {}
    assert forwarded_args.get("ticker") == "BABA"
    text = rpc_resp["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert payload.get("error_code") != "not_sec_filer"
    assert payload.get("sec_filer") is not False


def test_sec_shared_issuer_map_resolves_toyota_7203_t_and_distinguishes_unmapped_from_non_sec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r4 Med #4: all three SEC entrypoints share src.dalton.sec_issuer_map:
    - 7203.T maps to TM (CIK 0001094517) instead of being rejected as non-SEC
    - Unmapped non-US ticker (e.g. 9432.T) returns sec_issuer_mapping_not_found with sec_filer=None (NOT sec_filer=False)
    - _CORE_WATCHLIST_ALIASES is consulted (not nonexistent _TICKER_ALIASES/TICKER_ALIASES)."""
    from src.dalton import sec_issuer_map

    # 1. Shared classifier: 7203.T -> TM; 0700.HK -> confirmed_non_sec; 9432.T -> unmapped_non_us
    assert sec_issuer_map.classify_sec_ticker("7203.T")[0:2] == ("mapped", "TM")
    assert sec_issuer_map.classify_sec_ticker("0700.HK")[0] == "confirmed_non_sec"
    assert sec_issuer_map.classify_sec_ticker("9432.T")[0] == "unmapped_non_us"

    # Verify _CORE_WATCHLIST_ALIASES lookup works via the real attribute name
    import scripts.dalton.alphaengine_search_cache as ae_cache

    monkeypatch.setitem(ae_cache._CORE_WATCHLIST_ALIASES, "SPOT", ("SPOT", "SPOT.ST", "Spotify"))
    assert sec_issuer_map.resolve_sec_issuer_ticker("SPOT.ST") == "SPOT"

    # 2. dalton_sec_mcp_server: 7203.T queries TM (0001094517); unmapped 9432.T returns sec_issuer_mapping_not_found
    sec_mod = _load_dalton_sec()
    fake_tm_sub = {
        "name": "TOYOTA MOTOR CORP",
        "filings": {
            "recent": {
                "accessionNumber": ["0001193125-26-264811"],
                "form": ["20-F"],
                "filingDate": ["2026-06-25"],
                "reportDate": ["2026-03-31"],
                "primaryDocument": ["d101983d20f.htm"],
            }
        },
    }
    fetch_spy = create_autospec(sec_mod.fetch_submissions, return_value=fake_tm_sub)
    monkeypatch.setattr(sec_mod, "fetch_submissions", fetch_spy)

    tm_res = sec_mod.list_filings(ticker="7203.T", limit=5)
    fetch_spy.assert_called_once_with("0001094517")
    assert (tm_res.get("data") or [])[0]["cik"] == "0001094517"

    unmapped_sec = sec_mod.list_filings(ticker="9432.T", limit=5)
    assert unmapped_sec.get("error_code") == "sec_issuer_mapping_not_found"
    assert unmapped_sec.get("sec_filer") is None

    # 3. researchdata_bridge: 7203.T forwards ticker="TM"; unmapped 9432.T returns sec_issuer_mapping_not_found
    rb = _load_bridge()
    bridge = rb.ResearchDataBridge(rb.BridgeConfig())
    captured: list[dict[str, Any]] = []

    async def _fake_stdio(requests: list[dict[str, Any]], timeout_s: float) -> list[dict[str, Any]]:
        captured.extend(requests)
        return [
            {
                "jsonrpc": "2.0",
                "id": requests[0]["id"],
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"ok": True, "ticker": "TM", "rows": []})}],
                    "isError": False,
                },
            }
        ]

    bridge._run_stdio_rpc = create_autospec(bridge._run_stdio_rpc, side_effect=_fake_stdio)
    asyncio.run(
        bridge.handle_tools_call(
            req_id=3,
            params={"name": "finance", "arguments": {"route": "sec", "operation": "list-filings", "ticker": "7203.T"}},
        )
    )
    assert captured[0]["params"]["arguments"]["ticker"] == "TM"

    unmapped_bridge_resp = asyncio.run(
        bridge.handle_tools_call(
            req_id=4,
            params={"name": "finance", "arguments": {"route": "sec", "operation": "list-filings", "ticker": "9432.T"}},
        )
    )
    unmapped_bridge_payload = json.loads(unmapped_bridge_resp["result"]["content"][0]["text"])
    assert unmapped_bridge_payload["error_code"] == "sec_issuer_mapping_not_found"
    assert unmapped_bridge_payload["sec_filer"] is None

    # 4. findata_cli: 7203.T calls Company("TM"); unmapped 9432.T returns sec_issuer_mapping_not_found
    cli = _load_findata_cli()
    seen_companies: list[str] = []

    def _capture_company(sym: str) -> Any:
        seen_companies.append(sym)
        return _StubCompany(sym, _StubFilingsList([]))

    monkeypatch.setattr(cli, "Company", create_autospec(_capture_company, side_effect=_capture_company))
    cli.cmd_list_filings(argparse.Namespace(ticker="7203.T", form="20-F", limit=5, as_of=None))
    assert seen_companies == ["TM"]

    unmapped_cli = cli.cmd_list_filings(argparse.Namespace(ticker="9432.T", form="20-F", limit=5, as_of=None))
    assert unmapped_cli["ok"] is False
    assert unmapped_cli["error_code"] == "sec_issuer_mapping_not_found"
    assert unmapped_cli["sec_filer"] is None
