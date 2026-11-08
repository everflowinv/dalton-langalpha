#!/usr/bin/env python3
"""Fundamentals MCP Server.

Fundamental data for programmatic analysis via MCP. A CN listing is served
first from ginlix-data (vendor Tushare), whose rows come in FMP's camelCase
shape; every other listing, and any CN miss, comes from FMP. A CN read that
failed, rather than came back empty, still falls back to FMP, but the answer
then says so (`fallback_from`), and an FMP miss after it is an error.
Historical valuation, insider trades and technical indicators come from FMP
alone. `source` names the vendor that answered, and the envelope around
`data` is the standard market-data contract (AGENT_CONTRACT.md).

Tools:
- get_financial_statements: Raw income/balance/cash flow (multi-year)
- get_financial_ratios: Raw ratios and key metrics (multi-year)
- get_growth_metrics: Raw growth rates (multi-year)
- get_historical_valuation: Raw DCF and enterprise value (multi-year)
- get_insider_trades: Insider trading transactions and aggregate stats
- get_dividends_and_splits: Dividend history and stock split history
- get_shares_float: Shares float, outstanding shares, and float percentage
- get_key_executives: Key executives with title and compensation
- get_technical_indicator: Technical indicators (RSI, EMA, MACD, etc.)
"""

# NOTE: Tool docstrings in this file are hand-tuned agent prompt surface (parsed
# into agent prompts and generated sandbox wrappers) and are content-pinned by
# tests/unit/mcp_servers/test_agent_contract.py. Read mcp_servers/AGENT_CONTRACT.md
# before editing; intentional changes must regenerate agent_docstring_lock.json.

from __future__ import annotations

try:
    from _bootstrap import MCPServer  # script launch: mcp_servers/ is sys.path[0]
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._bootstrap import MCPServer

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal  # DALTON-HOOK(market-data-fallback)

import httpx

from data_client.fmp import close_fmp_client, get_fmp_client
from data_client.lifespan import closing_lifespan
from data_client.market_data_provider import symbol_market
from data_client.normalize import (
    declared_venue,
    minor_unit_places,
    populated,
    scale_price,
    series_labels,
)
from data_client.ginlix_data import close_ginlix_mcp_client, get_ginlix_mcp_client
from data_client.ginlix_data.cn_financial import GinlixDataCnFinancialSource
from mcp_servers._envelope import error_from_exception, make_error, make_response
from mcp_servers._schemas import (
    ANY,
    BAR_LABELS,
    FALLBACK_FROM,
    INT,
    RECORDS,
    RECORDS_BY_KEY,
    STR,
    described,
    envelope_schema,
    output_model,
)
from market_protocol.enums import AssetClass
from market_protocol.symbology import (
    UNKNOWN_MIC,
    is_family_index,
    to_canonical,
    to_display,
    to_legacy_api,
)

logger = logging.getLogger(__name__)

# DALTON-HOOK(market-data-fallback): FMP's free plan does not cover every
# listing the tool names in its docstring (e.g. some HK tickers); when a 402
# says so, yfinance statements answer instead so the agent gets data rather
# than an error it cannot fix by changing arguments.
try:
    import yfinance as _yf
except ImportError:  # an image without yfinance keeps returning the 402 error
    _yf = None  # type: ignore[assignment]

try:
    from _yf_common import boundary as _yf_boundary
except ModuleNotFoundError:  # imported as a package module (tests)
    from mcp_servers._yf_common import boundary as _yf_boundary


_lifespan = closing_lifespan(close_ginlix_mcp_client, close_fmp_client)


mcp = MCPServer("FundamentalsMCP", lifespan=_lifespan)

_SOURCE = "fmp"
_SOURCE_TUSHARE = "tushare"
_CLIENT_UNAVAILABLE = "FMP client is unavailable"
_UPSTREAM_FAILED = "FMP request failed"
_CN_FAILED = "tushare failed"

# The bar sizes the hosted technicals read accepts, in the order its refusal
# lists them. FMP answers any other as a bare 400, so the refusal is made here,
# in the hosted words, before FMP is asked.
_BAR_SIZES = ("1min", "5min", "15min", "30min", "1hour", "4hour", "1day")

_tushare_financial = None

#: Echo key of the CN tools: set only when a failed tushare read left the answer to FMP.
_FALLBACK = {"fallback_from": FALLBACK_FROM}


def _canonical(symbol: str) -> str:
    """Canonical display spelling for an input ticker; echo input on failure."""
    try:
        return to_display(to_canonical(symbol))
    except Exception:  # noqa: BLE001
        return symbol


def _fmp_symbol(symbol: str) -> str:
    """FMP's spelling for an input ticker (``^GSPC``, ``AAPL``, ``600519.SH``).

    Not the display spelling: FMP reads an index as ``^GSPC``, never ``SPX``, and
    a canonical key (``AAPL.XNAS``) not at all. Resolved as an equity so a company
    ticker that collides with an index alias (COMP) stays the company; the FMP
    client respells ``.SH`` itself.
    """
    try:
        ref = to_canonical(symbol, asset_class=AssetClass.EQUITY)
    except Exception:  # noqa: BLE001
        return symbol
    legacy = to_legacy_api(ref)
    return f"^{legacy}" if is_family_index(ref) else legacy


def _tushare_for(symbol: str) -> GinlixDataCnFinancialSource | None:
    """CN fundamentals (vendor: Tushare) served by ginlix-data, for CN symbols.

    Returns ``None`` otherwise, and :func:`_cn_first` goes straight to FMP.
    """
    if symbol_market(symbol) != "cn":
        return None
    global _tushare_financial
    if _tushare_financial is None:
        _tushare_financial = GinlixDataCnFinancialSource(get_ginlix_mcp_client())
    return _tushare_financial


def _http_miss(exc: httpx.HTTPError) -> str:
    """The error class, and the status when there was one; never the URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{type(exc).__name__} {exc.response.status_code}"
    return type(exc).__name__


# DALTON-HOOK(market-data-fallback): yfinance statements stand in when FMP's
# plan does not cover the symbol (402). Each table is converted to FMP-shaped
# period records (newest-first, camelCase keys, None for missing/unmapped
# FMP fields) so downstream skills can index records the same way as FMP.
_YF_FMP_STATEMENT_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "income": {
        "revenue": ("Total Revenue", "TotalRevenue", "Operating Revenue", "OperatingRevenue"),
        "costOfRevenue": ("Cost Of Revenue", "CostOfRevenue", "Reconciled Cost Of Revenue", "ReconciledCostOfRevenue"),
        "grossProfit": ("Gross Profit", "GrossProfit"),
        "grossProfitRatio": (),
        "researchAndDevelopmentExpenses": ("Research And Development", "ResearchAndDevelopment"),
        "generalAndAdministrativeExpenses": ("General And Administrative Expense", "GeneralAndAdministrativeExpense"),
        "sellingAndMarketingExpenses": ("Selling And Marketing Expense", "SellingAndMarketingExpense"),
        "sellingGeneralAndAdministrativeExpenses": ("Selling General And Administration", "SellingGeneralAndAdministration"),
        "otherExpenses": ("Other Operating Expenses", "OtherOperatingExpenses"),
        "operatingExpenses": ("Operating Expense", "OperatingExpense"),
        "costAndExpenses": ("Total Expenses", "TotalExpenses"),
        "interestIncome": ("Interest Income", "InterestIncome", "Interest Income Non Operating", "InterestIncomeNonOperating"),
        "interestExpense": ("Interest Expense", "InterestExpense", "Interest Expense Non Operating", "InterestExpenseNonOperating"),
        "depreciationAndAmortization": (
            "Reconciled Depreciation",
            "ReconciledDepreciation",
            "Depreciation And Amortization In Income Statement",
            "DepreciationAndAmortizationInIncomeStatement",
            "Depreciation Amortization Depletion Income Statement",
            "DepreciationAmortizationDepletionIncomeStatement",
        ),
        "ebitda": ("EBITDA", "Normalized EBITDA", "NormalizedEBITDA"),
        "ebitdaratio": (),
        "operatingIncome": ("Operating Income", "OperatingIncome", "Total Operating Income As Reported", "TotalOperatingIncomeAsReported"),
        "operatingIncomeRatio": (),
        # DALTON-HOOK(market-data-fallback): EBIT stands on its own row only —
        # substituting Operating Income poses a number FMP readers treat as a
        # distinct line (it excludes non-operating items EBIT can include).
        "ebit": ("EBIT",),
        "nonOperatingIncomeExcludingInterest": ("Other Non Operating Income Expenses", "OtherNonOperatingIncomeExpenses"),
        "totalOtherIncomeExpensesNet": ("Other Income Expense", "OtherIncomeExpense"),
        "incomeBeforeTax": ("Pretax Income", "PretaxIncome"),
        "incomeBeforeTaxRatio": (),
        "incomeTaxExpense": ("Tax Provision", "TaxProvision"),
        "netIncomeFromContinuingOperations": (
            "Net Income Continuous Operations",
            "NetIncomeContinuousOperations",
            "Net Income From Continuing Operation Net Minority Interest",
            "NetIncomeFromContinuingOperationNetMinorityInterest",
        ),
        "netIncome": ("Net Income", "NetIncome", "Net Income Common Stockholders", "NetIncomeCommonStockholders"),
        "netIncomeRatio": (),
        "eps": ("Basic EPS", "BasicEPS"),
        "epsdiluted": ("Diluted EPS", "DilutedEPS"),
        "epsDiluted": ("Diluted EPS", "DilutedEPS"),
        "weightedAverageShsOut": ("Basic Average Shares", "BasicAverageShares"),
        "weightedAverageShsOutDil": ("Diluted Average Shares", "DilutedAverageShares"),
    },
    "balance": {
        "cashAndCashEquivalents": ("Cash And Cash Equivalents", "CashAndCashEquivalents", "Cash Financial", "CashFinancial"),
        "shortTermInvestments": ("Other Short Term Investments", "OtherShortTermInvestments"),
        # DALTON-HOOK(market-data-fallback): the combined line only — cash
        # alone must not pose as "cash + short-term investments". When Yahoo
        # has no combined row, the serializer sums cash and short-term
        # investments, but only when both exist (see _YF_COMPUTED below).
        "cashAndShortTermInvestments": (
            "Cash Cash Equivalents And Short Term Investments",
            "CashCashEquivalentsAndShortTermInvestments",
        ),
        "netReceivables": ("Accounts Receivable", "AccountsReceivable", "Receivables"),
        "accountsReceivables": ("Accounts Receivable", "AccountsReceivable"),
        "otherReceivables": ("Other Receivables", "OtherReceivables"),
        "inventory": ("Inventory",),
        "prepaids": ("Prepaid Assets", "PrepaidAssets"),
        "otherCurrentAssets": ("Other Current Assets", "OtherCurrentAssets"),
        "totalCurrentAssets": ("Current Assets", "CurrentAssets"),
        "propertyPlantEquipmentNet": ("Net PPE", "NetPPE"),
        "goodwill": ("Goodwill",),
        "intangibleAssets": ("Other Intangible Assets", "OtherIntangibleAssets"),
        "goodwillAndIntangibleAssets": ("Goodwill And Other Intangible Assets", "GoodwillAndOtherIntangibleAssets"),
        "longTermInvestments": ("Investments And Advances", "InvestmentsAndAdvances", "Long Term Equity Investment", "LongTermEquityInvestment"),
        "taxAssets": (
            "Non Current Deferred Taxes Assets",
            "NonCurrentDeferredTaxesAssets",
            "Current Deferred Taxes Assets",
            "CurrentDeferredTaxesAssets",
        ),
        "otherNonCurrentAssets": ("Other Non Current Assets", "OtherNonCurrentAssets"),
        "totalNonCurrentAssets": ("Total Non Current Assets", "TotalNonCurrentAssets"),
        "otherAssets": (),
        "totalAssets": ("Total Assets", "TotalAssets"),
        "totalPayables": ("Payables", "Payables And Accrued Expenses", "PayablesAndAccruedExpenses"),
        "accountPayables": ("Accounts Payable", "AccountsPayable"),
        "otherPayables": ("Other Payable", "OtherPayable"),
        "accruedExpenses": ("Current Accrued Expenses", "CurrentAccruedExpenses"),
        "shortTermDebt": ("Current Debt", "CurrentDebt", "Current Debt And Capital Lease Obligation", "CurrentDebtAndCapitalLeaseObligation"),
        "capitalLeaseObligationsCurrent": ("Current Capital Lease Obligation", "CurrentCapitalLeaseObligation"),
        "taxPayables": ("Income Tax Payable", "IncomeTaxPayable", "Total Tax Payable", "TotalTaxPayable"),
        "deferredRevenue": ("Current Deferred Revenue", "CurrentDeferredRevenue"),
        "otherCurrentLiabilities": ("Other Current Liabilities", "OtherCurrentLiabilities"),
        "totalCurrentLiabilities": ("Current Liabilities", "CurrentLiabilities"),
        "longTermDebt": ("Long Term Debt", "LongTermDebt", "Long Term Debt And Capital Lease Obligation", "LongTermDebtAndCapitalLeaseObligation"),
        "capitalLeaseObligationsNonCurrent": ("Long Term Capital Lease Obligation", "LongTermCapitalLeaseObligation"),
        "capitalLeaseObligations": ("Capital Lease Obligations", "CapitalLeaseObligations"),
        "deferredRevenueNonCurrent": ("Non Current Deferred Revenue", "NonCurrentDeferredRevenue"),
        "deferredTaxLiabilitiesNonCurrent": ("Non Current Deferred Taxes Liabilities", "NonCurrentDeferredTaxesLiabilities"),
        "otherNonCurrentLiabilities": ("Other Non Current Liabilities", "OtherNonCurrentLiabilities"),
        "totalNonCurrentLiabilities": ("Total Non Current Liabilities Net Minority Interest", "TotalNonCurrentLiabilitiesNetMinorityInterest"),
        "otherLiabilities": (),
        "totalLiabilities": ("Total Liabilities Net Minority Interest", "TotalLiabilitiesNetMinorityInterest"),
        "preferredStock": ("Preferred Stock", "PreferredStock", "Preferred Stock Equity", "PreferredStockEquity"),
        "commonStock": ("Common Stock", "CommonStock", "Capital Stock", "CapitalStock"),
        "additionalPaidInCapital": ("Additional Paid In Capital", "AdditionalPaidInCapital"),
        "retainedEarnings": ("Retained Earnings", "RetainedEarnings"),
        "accumulatedOtherComprehensiveIncomeLoss": (
            "Gains Losses Not Affecting Retained Earnings",
            "GainsLossesNotAffectingRetainedEarnings",
            "Other Equity Adjustments",
            "OtherEquityAdjustments",
        ),
        "treasuryStock": ("Treasury Stock", "TreasuryStock"),
        "othertotalStockholdersEquity": ("Other Equity Interest", "OtherEquityInterest"),
        "totalStockholdersEquity": ("Stockholders Equity", "StockholdersEquity", "Common Stock Equity", "CommonStockEquity"),
        "totalEquity": ("Total Equity Gross Minority Interest", "TotalEquityGrossMinorityInterest", "Stockholders Equity", "StockholdersEquity"),
        "minorityInterest": ("Minority Interest", "MinorityInterest"),
        "totalLiabilitiesAndStockholdersEquity": (),
        "totalInvestments": ("Investments And Advances", "InvestmentsAndAdvances"),
        "totalDebt": ("Total Debt", "TotalDebt"),
        "netDebt": ("Net Debt", "NetDebt"),
    },
    "cash": {
        "netIncome": ("Net Income From Continuing Operations", "NetIncomeFromContinuingOperations"),
        "depreciationAndAmortization": (
            "Depreciation And Amortization",
            "DepreciationAndAmortization",
            "Depreciation Amortization Depletion",
            "DepreciationAmortizationDepletion",
            "Depreciation",
        ),
        "deferredIncomeTax": ("Deferred Income Tax", "DeferredIncomeTax", "Deferred Tax", "DeferredTax"),
        "stockBasedCompensation": ("Stock Based Compensation", "StockBasedCompensation"),
        "changeInWorkingCapital": ("Change In Working Capital", "ChangeInWorkingCapital"),
        "accountsReceivables": ("Changes In Account Receivables", "ChangesInAccountReceivables", "Change In Receivables", "ChangeInReceivables"),
        "inventory": ("Change In Inventory", "ChangeInInventory"),
        "accountsPayables": (
            "Change In Account Payable",
            "ChangeInAccountPayable",
            "Change In Payable",
            "ChangeInPayable",
            "Change In Payables And Accrued Expense",
            "ChangeInPayablesAndAccruedExpense",
        ),
        "otherWorkingCapital": ("Change In Other Working Capital", "ChangeInOtherWorkingCapital"),
        "otherNonCashItems": ("Other Non Cash Items", "OtherNonCashItems"),
        "netCashProvidedByOperatingActivities": (
            "Operating Cash Flow",
            "OperatingCashFlow",
            "Cash Flow From Continuing Operating Activities",
            "CashFlowFromContinuingOperatingActivities",
        ),
        "operatingCashFlow": (
            "Operating Cash Flow",
            "OperatingCashFlow",
            "Cash Flow From Continuing Operating Activities",
            "CashFlowFromContinuingOperatingActivities",
        ),
        "investmentsInPropertyPlantAndEquipment": (
            "Capital Expenditure",
            "CapitalExpenditure",
            "Purchase Of PPE",
            "PurchaseOfPPE",
            "Net PPE Purchase And Sale",
            "NetPPEPurchaseAndSale",
        ),
        "capitalExpenditure": (
            "Capital Expenditure",
            "CapitalExpenditure",
            "Capital Expenditure Reported",
            "CapitalExpenditureReported",
            "Purchase Of PPE",
            "PurchaseOfPPE",
        ),
        "acquisitionsNet": ("Net Business Purchase And Sale", "NetBusinessPurchaseAndSale", "Purchase Of Business", "PurchaseOfBusiness"),
        "purchasesOfInvestments": ("Purchase Of Investment", "PurchaseOfInvestment"),
        "salesMaturitiesOfInvestments": ("Sale Of Investment", "SaleOfInvestment"),
        "otherInvestingActivites": ("Net Other Investing Changes", "NetOtherInvestingChanges"),
        "otherInvestingActivities": ("Net Other Investing Changes", "NetOtherInvestingChanges"),
        "netCashUsedForInvestingActivites": (
            "Investing Cash Flow",
            "InvestingCashFlow",
            "Cash Flow From Continuing Investing Activities",
            "CashFlowFromContinuingInvestingActivities",
        ),
        "netCashProvidedByInvestingActivities": (
            "Investing Cash Flow",
            "InvestingCashFlow",
            "Cash Flow From Continuing Investing Activities",
            "CashFlowFromContinuingInvestingActivities",
        ),
        "investingCashFlow": (
            "Investing Cash Flow",
            "InvestingCashFlow",
            "Cash Flow From Continuing Investing Activities",
            "CashFlowFromContinuingInvestingActivities",
        ),
        "debtRepayment": ("Repayment Of Debt", "RepaymentOfDebt", "Long Term Debt Payments", "LongTermDebtPayments"),
        "netDebtIssuance": ("Net Issuance Payments Of Debt", "NetIssuancePaymentsOfDebt", "Issuance Of Debt", "IssuanceOfDebt"),
        "longTermNetDebtIssuance": ("Net Long Term Debt Issuance", "NetLongTermDebtIssuance", "Long Term Debt Issuance", "LongTermDebtIssuance"),
        "shortTermNetDebtIssuance": ("Net Short Term Debt Issuance", "NetShortTermDebtIssuance", "Short Term Debt Issuance", "ShortTermDebtIssuance"),
        "commonStockIssued": ("Common Stock Issuance", "CommonStockIssuance", "Issuance Of Capital Stock", "IssuanceOfCapitalStock"),
        "commonStockRepurchased": ("Repurchase Of Capital Stock", "RepurchaseOfCapitalStock", "Common Stock Payments", "CommonStockPayments"),
        "dividendsPaid": ("Cash Dividends Paid", "CashDividendsPaid", "Common Stock Dividend Paid", "CommonStockDividendPaid"),
        "commonDividendsPaid": ("Common Stock Dividend Paid", "CommonStockDividendPaid", "Cash Dividends Paid", "CashDividendsPaid"),
        "preferredDividendsPaid": ("Preferred Stock Dividend Paid", "PreferredStockDividendPaid"),
        "otherFinancingActivites": ("Net Other Financing Charges", "NetOtherFinancingCharges"),
        "otherFinancingActivities": ("Net Other Financing Charges", "NetOtherFinancingCharges"),
        "netCashUsedProvidedByFinancingActivities": (
            "Financing Cash Flow",
            "FinancingCashFlow",
            "Cash Flow From Continuing Financing Activities",
            "CashFlowFromContinuingFinancingActivities",
        ),
        "netCashProvidedByFinancingActivities": (
            "Financing Cash Flow",
            "FinancingCashFlow",
            "Cash Flow From Continuing Financing Activities",
            "CashFlowFromContinuingFinancingActivities",
        ),
        "financingCashFlow": (
            "Financing Cash Flow",
            "FinancingCashFlow",
            "Cash Flow From Continuing Financing Activities",
            "CashFlowFromContinuingFinancingActivities",
        ),
        "effectOfForexChangesOnCash": ("Effect Of Exchange Rate Changes", "EffectOfExchangeRateChanges"),
        "netChangeInCash": ("Changes In Cash", "ChangesInCash"),
        "cashAtEndOfPeriod": ("End Cash Position", "EndCashPosition"),
        "cashAtBeginningOfPeriod": ("Beginning Cash Position", "BeginningCashPosition"),
        "freeCashFlow": ("Free Cash Flow", "FreeCashFlow"),
        "incomeTaxesPaid": ("Income Tax Paid Supplemental Data", "IncomeTaxPaidSupplementalData"),
        "interestPaid": ("Interest Paid Supplemental Data", "InterestPaidSupplementalData"),
    },
}


# DALTON-HOOK(market-data-fallback): FMP ships these as its own rows; yfinance
# gives only the inputs, so they are computed here — and only when every input
# exists, since a one-sided sum (cash alone posing as cash+investments) or a
# ratio over a missing denominator reads downstream as reported data.
_YF_COMPUTED_RATIOS: tuple[tuple[str, str], ...] = (
    ("ebitdaratio", "ebitda"),
    ("grossProfitRatio", "grossProfit"),
    ("operatingIncomeRatio", "operatingIncome"),
    ("incomeBeforeTaxRatio", "incomeBeforeTax"),
    ("netIncomeRatio", "netIncome"),
)


def _yf_date_of(raw: Any) -> date | None:
    """Date of a Yahoo date field; None for anything unusable."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _yf_month_end(d: date) -> bool:
    """Whether *d* is the last day of its calendar month."""
    return (d + timedelta(days=1)).day == 1


def _yf_fiscal_year_end(info: Any) -> date | None:
    """The issuer's fiscal-year end date, from Yahoo quote metadata.

    ``lastFiscalYearEnd`` is an epoch-seconds timestamp (there is no string
    month field in the live payload); its date is a fiscal year end of this
    issuer, stable in month across years. Unknown stays ``None`` rather than
    guessed as December.

    DALTON-HOOK(market-data-fallback): the full date, not just its month — a
    52/53-week calendar announces itself here (an end like 2026-01-30 never
    falls on a month-end), and month arithmetic would mislabel its quarters.
    """
    if not isinstance(info, dict):
        return None
    for key in ("lastFiscalYearEnd", "fiscalYearEnd"):
        fye = _yf_date_of(info.get(key))
        if fye is not None:
            return fye
    return None


def _yf_fiscal_period(
    date_str: str, period: str, fy_end_date: date | None
) -> tuple[str | None, str | None]:
    """(period label, fiscalYear) for a statement column, on the issuer's fiscal calendar.

    Quarters are counted from the month after the fiscal year end (June-end
    MSFT: Jul-Sep is Q1) and the fiscal year is named for the calendar year it
    ends in (a 2025-09-30 column is FY2026). Without the fiscal year end date
    both are ``None`` — a calendar-year-shaped guess would silently misjoin
    against FMP rows, which carry true fiscal labels. A 52/53-week fiscal
    calendar is also ``None`` on both: its year end and quarter ends drift
    against the calendar months (a 2026-05-01 quarter reads as Q2 by month
    where the issuer calls it Q1), so the month arithmetic does not apply.
    """
    if fy_end_date is None:
        return None, None
    try:
        end = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None, None
    if not _yf_month_end(fy_end_date) or not _yf_month_end(end):
        return None, None
    fy_end_month = fy_end_date.month
    if period != "quarter":
        return "FY", str(end.year)
    fiscal_year = end.year + 1 if end.month > fy_end_month else end.year
    quarter = (end.month - fy_end_month + 11) % 12 // 3 + 1
    return f"Q{quarter}", str(fiscal_year)


def _yf_column_date(col: Any) -> str:
    """A statement column's period-end date string (``2026-07-31``)."""
    return col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]


def _yf_stmt_off_month_end(df: Any) -> bool:
    """Whether any period-end column of a statement DataFrame is off a month-end."""
    if df is None or getattr(df, "empty", True):
        return False
    for col in df.columns:
        try:
            end = date.fromisoformat(_yf_column_date(col))
        except ValueError:
            continue
        if not _yf_month_end(end):
            return True
    return False


def _yf_serialize_statement(
    df: Any,
    stmt_kind: str,
    *,
    symbol: str = "",
    currency: str | None = None,
    period: str = "annual",
    limit: int | None = None,
    fy_end_date: date | None = None,
) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    import pandas as pd

    field_map = _YF_FMP_STATEMENT_FIELDS.get(stmt_kind, {})
    col_pairs: list[tuple[str, Any]] = []
    for col in df.columns:
        col_pairs.append((_yf_column_date(col), col))
    col_pairs.sort(key=lambda pair: pair[0], reverse=True)
    if limit is not None and limit > 0:
        col_pairs = col_pairs[:limit]

    records: list[dict[str, Any]] = []
    for date_str, col in col_pairs:
        year_str = date_str[:4] if len(date_str) >= 4 else ""
        period_label, fiscal_year = _yf_fiscal_period(date_str, period, fy_end_date)
        rec: dict[str, Any] = {
            "date": date_str,
            "symbol": symbol,
            "reportedCurrency": currency,
            "cik": None,
            "fillingDate": None,
            "acceptedDate": None,
            "calendarYear": year_str,
            "fiscalYear": fiscal_year,
            "period": period_label,
        }
        for fmp_field, yf_labels in field_map.items():
            val_out: float | None = None
            for label in yf_labels:
                if label in df.index:
                    raw_val = df.loc[label, col]
                    if hasattr(raw_val, "iloc"):
                        raw_val = raw_val.iloc[0]
                    if raw_val is not None and not pd.isna(raw_val):
                        val_out = float(raw_val)
                        break
            rec[fmp_field] = val_out
        # DALTON-HOOK(market-data-fallback): computed fields — see
        # _YF_COMPUTED_RATIOS; each needs both sides, else None.
        if stmt_kind == "income":
            revenue = rec.get("revenue")
            for ratio_field, numer_field in _YF_COMPUTED_RATIOS:
                numerator = rec.get(numer_field)
                rec[ratio_field] = (
                    round(numerator / revenue, 6)
                    if isinstance(revenue, (int, float))
                    and revenue
                    and isinstance(numerator, (int, float))
                    else None
                )
        if stmt_kind == "balance" and rec.get("cashAndShortTermInvestments") is None:
            cash = rec.get("cashAndCashEquivalents")
            short_term = rec.get("shortTermInvestments")
            rec["cashAndShortTermInvestments"] = (
                cash + short_term
                if isinstance(cash, (int, float)) and isinstance(short_term, (int, float))
                else None
            )
        records.append(rec)
    return records


_YF_STATEMENT_ATTRS: dict[str, tuple[str, str]] = {
    "income": ("quarterly_income_stmt", "income_stmt"),
    "balance": ("quarterly_balance_sheet", "balance_sheet"),
    "cash": ("quarterly_cashflow", "cashflow"),
}
_YF_ALL_KEYS: dict[str, str] = {
    "income": "income_statement",
    "balance": "balance_sheet",
    "cash": "cash_flow",
}


def _yf_statements(
    symbol: str,
    statement_type: str,
    period: str,
    limit: int = 10,
) -> tuple[list[dict[str, Any]] | dict[str, list[dict[str, Any]]] | None, bool]:
    """Synchronous yfinance statements fetch; ``(None, False)`` when yfinance cannot serve.

    Raises nothing the caller cannot treat as a soft miss back to the 402
    error. Converts each statement DataFrame into FMP-compatible period records
    (newest-first) and slices to ``limit``. The second return says whether the
    issuer is on a 52/53-week fiscal calendar (fiscal year end or any period
    end off a calendar month-end), the reason its records carry no
    period/fiscalYear labels.
    """
    if _yf is None:
        return None, False
    disp, yf_symbol, _listing_currency = _yf_boundary(symbol)
    ticker = _yf.Ticker(yf_symbol)
    quarterly = period == "quarter"
    # DALTON-HOOK(market-data-fallback): the reporting currency is the
    # financial-currency of the statements (`financialCurrency` in Yahoo
    # metadata — the same source the main chain's `_stamp_reported_currency`
    # uses), not the listing currency a HK quote trades in; unknown stays None.
    try:
        info = ticker.info or {}
    except Exception:  # noqa: BLE001 — metadata is optional; statements may still serve
        info = {}
    if not isinstance(info, dict):
        info = {}
    financial_currency = info.get("financialCurrency")
    if not isinstance(financial_currency, str) or not financial_currency:
        financial_currency = None
    fy_end_date = _yf_fiscal_year_end(info)
    if statement_type in _YF_STATEMENT_ATTRS:
        stmt_kinds = [statement_type]
    else:  # "all"
        stmt_kinds = list(_YF_STATEMENT_ATTRS)

    try:
        fetched = [
            (
                kind,
                getattr(ticker, _YF_STATEMENT_ATTRS[kind][0 if quarterly else 1]),
            )
            for kind in stmt_kinds
        ]
        # DALTON-HOOK(market-data-fallback): a fiscal year end off a
        # month-end — or any period-end column off one, on any of the fetched
        # statements — is the 52/53-week signature. Judged on the raw
        # DataFrames before serialization and the ``limit`` slice, so no
        # column can carry a month-derived label its drifting calendar does
        # not support: a month-end year end alone is not acquittal, and the
        # serializer reaches it as unknown rather than half the columns
        # guessing.
        week_based = fy_end_date is not None and (
            not _yf_month_end(fy_end_date)
            or any(_yf_stmt_off_month_end(df) for _, df in fetched)
        )
        records = {
            kind: _yf_serialize_statement(
                df,
                kind,
                symbol=disp,
                currency=financial_currency,
                period=period,
                limit=limit,
                fy_end_date=None if week_based else fy_end_date,
            )
            for kind, df in fetched
        }
    except Exception:  # noqa: BLE001 — soft miss, the 402 error answers
        logger.warning("yfinance statements failed for %s", symbol, exc_info=True)
        return None, False
    if len(records) == 1:
        data: list[dict[str, Any]] | dict[str, list[dict[str, Any]]] = next(
            iter(records.values())
        )
    else:
        data = {_YF_ALL_KEYS[kind]: records[kind] for kind in stmt_kinds}
    if not populated(data):
        return None, False
    return data, week_based


_YF_STATEMENTS_NOTE = (
    "FMP plan does not cover this request; statements served from yfinance "
    "(mapped to FMP camelCase period records; 口径与细分科目可能与 FMP 不同, "
    "unmapped fields are None). Computed from Yahoo lines, only when every "
    "input exists: ebitdaratio, grossProfitRatio, operatingIncomeRatio, "
    "incomeBeforeTaxRatio, netIncomeRatio (numerator over revenue), and "
    "cashAndShortTermInvestments (cash + short-term investments when no "
    "combined line)."
)
_YF_PERIODS_UNKNOWN_NOTE = (
    " period / fiscalYear 未知 (issuer fiscal-year end not available); "
    "calendarYear still carries the statement date's year."
)
# DALTON-HOOK(market-data-fallback): a 52/53-week fiscal calendar (year end or
# period ends off calendar month-ends) has quarters that drift against the
# months, so no month-derived label is published for its columns.
_YF_WEEK_BASED_NOTE = (
    " 财年为周制，period / fiscalYear 未知 (a 52/53-week fiscal calendar: the "
    "fiscal year end and period ends do not fall on calendar month-ends, so "
    "quarters cannot be counted by month); calendarYear still carries the "
    "statement date's year."
)


def _yf_periods_unknown(data: Any) -> bool:
    """Whether any served record lacks its fiscal period/fiscal-year labels."""
    groups = data.values() if isinstance(data, dict) else [data]
    for records in groups:
        if isinstance(records, list) and any(
            isinstance(r, dict) and r.get("period") is None for r in records
        ):
            return True
    return False


async def _from_fmp(
    disp: str, read, *, data_type: str, yf_fallback=None, **extra
) -> dict:
    """Answer a tool from FMP: *read* takes the client and returns the payload.

    The one place an FMP read gets its client, its envelope and its error, so a
    tool passes only what it reads.
    """
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE, symbol=disp)
    try:
        payload = await read(client)
    except Exception as e:  # noqa: BLE001
        err = error_from_exception(e, _UPSTREAM_FAILED, symbol=disp)
        # DALTON-HOOK(market-data-fallback): a 402 is about the FMP plan, not
        # the request — no argument change fixes it, so yfinance answers
        # instead of returning an error the agent would retry blind.
        if yf_fallback is not None and (
            err.get("error") == "plan_not_covered" or getattr(e, "status_code", None) == 402
        ):
            yf_resp = await yf_fallback()
            if yf_resp is not None:
                return yf_resp
        return err
    return make_response(
        payload or [], source=_SOURCE, symbol=disp, data_type=data_type, **extra
    )


async def _cn_first(
    disp: str, cn_read, fmp_read, *, data_type: str, yf_fallback=None, **extra
) -> dict:
    """Serve a tool from tushare for a CN listing, from FMP otherwise.

    The single definition of the cn-branch contract: gate on ``_tushare_for``
    and a configured ginlix-data, envelope a tushare hit with
    ``source="tushare"``, and let an empty payload fall through to FMP, read by
    *fmp_read*. A tushare error falls through too, but it is not a miss: FMP's
    rows then carry ``fallback_from``, an FMP error keeps its code with the
    tushare failure named, and an empty FMP answer becomes ``upstream_error``,
    since neither source answered. *disp* is the display spelling, so a
    canonical ``600519.XSHG`` still gates in as a CN listing.
    """
    def fmp():
        return _from_fmp(
            disp, fmp_read, data_type=data_type, yf_fallback=yf_fallback, **extra
        )

    ts = _tushare_for(disp)
    if ts is None or not await get_ginlix_mcp_client().ensure():
        return await fmp()
    try:
        data = await cn_read(ts)
    except httpx.HTTPError as exc:
        # An upstream miss (a 503, a 429, a dropped connection) is expected; the
        # FMP path answers instead, so one line is enough.
        failed = _http_miss(exc)
        logger.warning("ginlix-data %s unavailable for %s: %s", data_type, disp, failed)
    except Exception as exc:  # noqa: BLE001 — FMP answers instead
        failed = type(exc).__name__
        logger.warning("ginlix-data %s failed for %s", data_type, disp, exc_info=True)
    else:
        if populated(data):
            return make_response(
                data, source=_SOURCE_TUSHARE, symbol=disp, data_type=data_type, **extra
            )
        return await fmp()
    answer = await fmp()
    if "error" in answer:
        return {**answer, "detail": f"{_CN_FAILED} ({failed}) and FMP did too: {answer['detail']}"}
    if not populated(answer["data"]):
        return make_error(
            "upstream_error",
            f"{_CN_FAILED} ({failed}) and FMP has no rows for this listing",
            symbol=disp,
        )
    return {**answer, "fallback_from": _SOURCE_TUSHARE}


_OUT_GET_FINANCIAL_STATEMENTS = output_model(
    "GetFinancialStatementsOut",
    envelope_schema(
        ANY,
        frame=("symbol",),
        echo={"data_type": STR, "statement_type": STR, "period": STR, **_FALLBACK},
    ),
)


# DALTON-HOOK(market-data-fallback): route US statements to researchdata's
# SEC data first (primary-source, free); yfinance answers HK/A-share and any
# FMP plan gap.
@mcp.tool()
async def get_financial_statements(
    symbol: str,
    statement_type: Literal["income", "balance", "cash", "all"] = "all",
    period: Literal["annual", "quarter"] = "annual",
    limit: int = 10,
) -> _OUT_GET_FINANCIAL_STATEMENTS:
    """Fetch raw historical financial statements for trend analysis or model
    building. statement_type="all" returns income, balance sheet, and cash flow.
    US: prefer SEC data finance(route="sec", operation="financials") (primary,
    free); HK/A-share: yfinance statement tools.

    Args:
        symbol: Ticker, e.g. "AAPL", "0700.HK", "600519.SH".
        statement_type: "income" | "balance" | "cash" | "all".
        period: "annual" | "quarter".
        limit: Number of periods (default 10).

    Returns:
        dict: {symbol, count, data, source, data_type, statement_type, period,
        fallback_from?}. One statement_type → a list of period records; "all" →
        {income_statement, balance_sheet, cash_flow} lists with count the total.
        Common camelCase fields: date, revenue, netIncome, eps, totalAssets,
        operatingCashFlow. date is "YYYY-MM-DD"; records are newest-first.
        fallback_from: source that failed before FMP answered. On error:
        {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)

    async def _cn_statements(ts: GinlixDataCnFinancialSource):
        if statement_type == "income":
            return await ts.get_income_statements(disp, period=period, limit=limit)
        if statement_type == "balance":
            return await ts.get_balance_sheets(disp, period=period, limit=limit)
        if statement_type == "cash":
            return await ts.get_cash_flows(disp, period=period, limit=limit)
        income, balance, cash = await asyncio.gather(  # "all"
            ts.get_income_statements(disp, period=period, limit=limit),
            ts.get_balance_sheets(disp, period=period, limit=limit),
            ts.get_cash_flows(disp, period=period, limit=limit),
        )
        return {"income_statement": income, "balance_sheet": balance, "cash_flow": cash}

    async def _fmp_statements(client):
        if statement_type == "income":
            return await client.get_income_statement(api, period=period, limit=limit)
        if statement_type == "balance":
            return await client.get_balance_sheet(api, period=period, limit=limit)
        if statement_type == "cash":
            return await client.get_cash_flow(api, period=period, limit=limit)
        income = await client.get_income_statement(api, period=period, limit=limit)  # "all"
        balance = await client.get_balance_sheet(api, period=period, limit=limit)
        cash_flow = await client.get_cash_flow(api, period=period, limit=limit)
        return {
            "income_statement": income or [],
            "balance_sheet": balance or [],
            "cash_flow": cash_flow or [],
        }

    async def _yf_statements_mcp():
        yf_data, week_based = await asyncio.to_thread(
            _yf_statements, disp, statement_type, period, limit
        )
        if yf_data is None:
            return None
        note = _YF_STATEMENTS_NOTE
        # DALTON-HOOK(market-data-fallback): say so in the note rather
        # than guessing calendar-shaped period labels.
        if _yf_periods_unknown(yf_data):
            note += _YF_WEEK_BASED_NOTE if week_based else _YF_PERIODS_UNKNOWN_NOTE
        return make_response(
            yf_data,
            source="yfinance",
            symbol=disp,
            data_type="financial_statements",
            statement_type=statement_type,
            period=period,
            note=note,
        )

    return await _cn_first(
        disp,
        _cn_statements,
        _fmp_statements,
        data_type="financial_statements",
        yf_fallback=_yf_statements_mcp,
        statement_type=statement_type,
        period=period,
    )


_OUT_GET_FINANCIAL_RATIOS = output_model(
    "GetFinancialRatiosOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol",),
        echo={"data_type": STR, "period": STR, **_FALLBACK},
    ),
)


@mcp.tool()
async def get_financial_ratios(
    symbol: str,
    period: Literal["annual", "quarter"] = "annual",
    limit: int = 10,
) -> _OUT_GET_FINANCIAL_RATIOS:
    """Fetch raw historical key metrics and financial ratios.

    Args:
        symbol: Ticker, e.g. "AAPL", "0700.HK", "600519.SH".
        period: "annual" | "quarter".
        limit: Number of periods (default 10).

    Returns:
        dict: {symbol, count, data, source, data_type, period, fallback_from?}.
        data is {key_metrics, ratios}: period records, newest first.
        key_metrics fields: marketCap, enterpriseValue, evToEBITDA,
        freeCashFlowYield. ratios fields: netProfitMargin, returnOnEquity,
        currentRatio, debtToEquityRatio, priceToEarningsRatio. source "tushare"
        rows lack both lists' price-based fields; market cap and EV are in
        get_historical_valuation. date is "YYYY-MM-DD". A quarter row's
        flow-based fields cover that quarter alone; its P/E fields annualize
        the quarter's EPS (x4). fallback_from: source that failed before FMP
        answered. On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)

    async def _cn_ratios(ts: GinlixDataCnFinancialSource):
        key_metrics, ratios = await asyncio.gather(
            ts.get_key_metrics_periods(disp, period=period, limit=limit),
            ts.get_ratios_periods(disp, period=period, limit=limit),
        )
        return {"key_metrics": key_metrics, "ratios": ratios}

    async def _fmp_ratios(client):
        key_metrics = await client.get_key_metrics(api, period=period, limit=limit)
        ratios = await client.get_financial_ratios(api, period=period, limit=limit)
        return {"key_metrics": key_metrics or [], "ratios": ratios or []}

    # CN rows carry the accounting half only: ginlix-data prices no period, so
    # they have no marketCap, enterpriseValue or priceToEarningsRatio, and FMP's
    # are not mixed in to stand for them.
    return await _cn_first(
        disp, _cn_ratios, _fmp_ratios, data_type="financial_ratios", period=period
    )


_OUT_GET_GROWTH_METRICS = output_model(
    "GetGrowthMetricsOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol",),
        echo={"data_type": STR, "period": STR, **_FALLBACK},
    ),
)


@mcp.tool()
async def get_growth_metrics(
    symbol: str,
    period: Literal["annual", "quarter"] = "annual",
    limit: int = 10,
) -> _OUT_GET_GROWTH_METRICS:
    """Fetch raw historical growth rates.

    Args:
        symbol: Ticker, e.g. "AAPL", "0700.HK", "600519.SH".
        period: "annual" | "quarter".
        limit: Number of periods (default 10).

    Returns:
        dict: {symbol, count, data, source, data_type, period, fallback_from?}.
        data is {financial_growth, income_statement_growth}, each a list, newest
        first; count is the total. financial_growth fields: date, revenueGrowth,
        netIncomeGrowth, epsgrowth; income_statement_growth fields: date,
        growthRevenue, growthNetIncome, growthEPS. Growth values are decimal
        fractions (0.1 = 10%); a quarter row grows on the prior quarter, not the
        year-ago one. Field names are FMP-native camelCase; date is
        "YYYY-MM-DD". fallback_from: source that failed before FMP answered.
        On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)

    async def _fmp_growth(client):
        financial_growth = await client.get_financial_growth(api, period=period, limit=limit)
        income_growth = await client.get_income_statement_growth(api, period=period, limit=limit)
        return {
            "financial_growth": financial_growth or [],
            "income_statement_growth": income_growth or [],
        }

    return await _cn_first(
        disp,
        lambda ts: ts.get_growth_periods(disp, period=period, limit=limit),
        _fmp_growth,
        data_type="growth_metrics",
        period=period,
    )


_OUT_GET_HISTORICAL_VALUATION = output_model(
    "GetHistoricalValuationOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol",),
        echo={
            "currency": described(
                STR,
                "ISO 4217 currency of current_dcf's dcf and stockPrice;"
                " omitted when the listing's venue is unknown.",
            ),
            "data_type": STR,
            "period": STR,
        },
    ),
)


@mcp.tool()
async def get_historical_valuation(
    symbol: str,
    period: Literal["annual", "quarter"] = "annual",
    limit: int = 10,
) -> _OUT_GET_HISTORICAL_VALUATION:
    """Fetch DCF fair value and enterprise value history.

    Args:
        symbol: Ticker, e.g. "AAPL", "0700.HK", "600519.SH".
        period: "annual" | "quarter".
        limit: Number of periods (default 10).

    Returns:
        dict: {symbol, currency, count, data, source, data_type, period}. data
        is {current_dcf, historical_dcf, enterprise_value}, each a list; count
        is the total. current_dcf fields: symbol, date, dcf, stockPrice, per
        share in `currency` (absent when the venue is unknown). historical_dcf
        is always []. enterprise_value fields, newest first: date,
        reportedCurrency, stockPrice, marketCapitalization, enterpriseValue,
        priced in reportedCurrency (the filings' currency, which can differ
        from `currency`; null when unknown). Field names are FMP-native
        camelCase; date is "YYYY-MM-DD".
        On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE, symbol=disp)

    try:
        current_dcf = await client.get_dcf(api)
        historical_dcf = await client.get_historical_dcf(api, period=period, limit=limit)
        enterprise_value = await client.get_enterprise_value(api, period=period, limit=limit)
    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED, symbol=disp)

    try:
        key_metrics = await client.get_key_metrics(api, period=period, limit=limit)
    except Exception as exc:  # noqa: BLE001 - it only labels the rows, which still answer
        logger.warning("FMP key metrics unavailable for %s: %s", disp, type(exc).__name__)
        key_metrics = []

    # FMP prices the DCF row in the listing's trading currency (in pence on a
    # pence venue) and the enterprise-value rows in the filings' currency, which
    # only the key-metrics row for the same period names.
    try:
        ref = to_canonical(symbol, asset_class=AssetClass.EQUITY)
    except Exception:  # noqa: BLE001
        ref = None
    # A venue the protocol does not know resolves to a USD default, not its currency.
    currency = ref.price_currency if ref and declared_venue(ref) != UNKNOWN_MIC else None
    places = minor_unit_places(symbol)
    reported = {row.get("date"): row.get("reportedCurrency") for row in key_metrics or []}
    return make_response(
        {
            "current_dcf": [
                {
                    **{k: v for k, v in row.items() if k != "Stock Price"},
                    "dcf": scale_price(row.get("dcf"), places),
                    "stockPrice": scale_price(row.get("Stock Price", row.get("stockPrice")), places),
                }
                for row in current_dcf or []
            ],
            "historical_dcf": historical_dcf or [],
            "enterprise_value": [
                {**row, "reportedCurrency": reported.get(row.get("date"))}
                for row in enterprise_value or []
            ],
        },
        source=_SOURCE,
        symbol=disp,
        currency=currency,
        data_type="historical_valuation",
        period=period,
    )


_OUT_GET_INSIDER_TRADES = output_model(
    "GetInsiderTradesOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol",),
        echo={"data_type": STR},
    ),
)


@mcp.tool()
async def get_insider_trades(
    symbol: str,
    limit: int = 50,
) -> _OUT_GET_INSIDER_TRADES:
    """Fetch insider transactions from SEC Form 4 filings, with quarterly totals.
    HK and A-share listings return empty lists.

    Args:
        symbol: Ticker, e.g. "AAPL".
        limit: Number of recent transactions (default 50).

    Returns:
        dict: {symbol, count, data, source, data_type}. data is {trades, stats},
        each a list; count is the total across both. trades fields, newest
        first: filingDate, transactionDate, reportingName, transactionType,
        securitiesTransacted, price. Only "P-Purchase" and "S-Sale" are
        open-market trades; others are awards, exercises or tax withholding.
        stats, one row per calendar quarter: year, quarter, totalPurchases,
        totalSales (counts of P and S trades), totalAcquired, totalDisposed
        (shares, all transaction types). Field names are FMP-native camelCase;
        dates are "YYYY-MM-DD". On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE, symbol=disp)

    try:
        trades = await client.get_insider_trades(api, limit=limit)
        stats = await client.get_insider_trade_stats(api)

        return make_response(
            {"trades": trades or [], "stats": stats or []},
            source=_SOURCE,
            symbol=disp,
            data_type="insider_trades",
        )

    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED, symbol=disp)


_OUT_GET_DIVIDENDS_AND_SPLITS = output_model(
    "GetDividendsAndSplitsOut",
    envelope_schema(
        RECORDS_BY_KEY,
        frame=("symbol",),
        echo={"data_type": STR, **_FALLBACK},
    ),
)


@mcp.tool()
async def get_dividends_and_splits(
    symbol: str,
) -> _OUT_GET_DIVIDENDS_AND_SPLITS:
    """Fetch historical dividend payments and stock splits.

    Args:
        symbol: Ticker — US "AAPL", HK "0700.HK", A-share "600519.SH".

    Returns:
        dict: {symbol, count, data, source, data_type, fallback_from?}. data is
        {dividends, splits}, each a list, newest first; count is the total
        across both. dividends fields: date, recordDate, paymentDate,
        declarationDate, adjDividend (split-adjusted), dividend (as paid), yield
        (percent, 0.34 = 0.34%), frequency. splits fields: date, numerator,
        denominator. Field names are FMP-native camelCase; date is "YYYY-MM-DD".
        fallback_from: source that failed before FMP answered. On error:
        {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)

    async def _fmp_dividends(client):
        dividends = await client.get_dividends(api)
        splits = await client.get_splits(api)
        return {"dividends": dividends or [], "splits": splits or []}

    return await _cn_first(
        disp,
        lambda ts: ts.get_dividends(disp),
        _fmp_dividends,
        data_type="dividends_and_splits",
    )


_OUT_GET_SHARES_FLOAT = output_model(
    "GetSharesFloatOut",
    envelope_schema(
        RECORDS,
        frame=("symbol",),
        echo={"data_type": STR, **_FALLBACK},
    ),
)


@mcp.tool()
async def get_shares_float(
    symbol: str,
) -> _OUT_GET_SHARES_FLOAT:
    """Fetch shares float, outstanding shares, and float percentage.

    Args:
        symbol: Ticker — US "AAPL", HK "0700.HK", A-share "600519.SH".

    Returns:
        dict: {symbol, count, data, source, data_type, fallback_from?}. data is
        a list of records; count is the record total. Fields: symbol, date, freeFloat
        (percent, 99.8 = 99.8%), floatShares, outstandingShares; an ADR counts
        ADS units, not ordinary shares. date is "YYYY-MM-DD": the day FMP
        refreshed the record, not the as-of date of the counts, which can be
        months older; an A-share row's date is the trading day of its counts.
        Field names are FMP-native camelCase. fallback_from: source that failed
        before FMP answered. On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)

    async def _fmp_float(client):
        # FMP stamps the row with its refresh time; the contract is a bare date.
        return [
            {**row, "date": str(row["date"])[:10]} if row.get("date") else row
            for row in await client.get_shares_float(api) or []
        ]

    return await _cn_first(
        disp,
        lambda ts: ts.get_shares_float(disp),
        _fmp_float,
        data_type="shares_float",
    )


_OUT_GET_KEY_EXECUTIVES = output_model(
    "GetKeyExecutivesOut",
    envelope_schema(
        RECORDS,
        frame=("symbol",),
        echo={"data_type": STR, **_FALLBACK},
    ),
)


@mcp.tool()
async def get_key_executives(
    symbol: str,
) -> _OUT_GET_KEY_EXECUTIVES:
    """Fetch key executives with title and compensation — identify the
    management team or compare executive pay across peers.

    Args:
        symbol: Ticker — US "AAPL", HK "0700.HK", A-share "600519.SH".

    Returns:
        dict: {symbol, count, data, source, data_type, fallback_from?}. data is
        a list of executive records; count is the record total. Fields: name,
        title, pay, currencyPay, gender, yearBorn, titleSince. Field names are
        FMP-native camelCase; pay is an integer in currencyPay units; not
        time-ordered. fallback_from: source that failed before FMP answered.
        On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    api = _fmp_symbol(symbol)

    return await _cn_first(
        disp,
        lambda ts: ts.get_key_executives(disp),
        lambda client: client.get_key_executives(api),
        data_type="key_executives",
    )


_OUT_GET_TECHNICAL_INDICATOR = output_model(
    "GetTechnicalIndicatorOut",
    envelope_schema(
        RECORDS,
        frame=("symbol",),
        echo={
            "data_type": STR,
            "indicator": STR,
            "period": described(INT, "Indicator lookback length."),
            "timeframe": described(STR, "FMP-native bar size."),
            **BAR_LABELS,
        },
    ),
)


@mcp.tool()
async def get_technical_indicator(
    symbol: str,
    indicator: str,
    period: int = 14,
    timeframe: str = "1day",
) -> _OUT_GET_TECHNICAL_INDICATOR:
    """Fetch a technical indicator time series over OHLCV bars.

    Args:
        symbol: Ticker, e.g. "AAPL", "0700.HK", "600519.SH".
        indicator: FMP name — "rsi", "ema", "sma", "wma", "adx", "williams".
        period: Lookback length (default 14).
        timeframe: FMP-native bar — "1min"…"4hour", "1day" (default "1day").

    Returns:
        dict: {symbol, count, data, source, data_type, indicator, period,
        timeframe, price_treatment, in_progress?}. data: list of {date, open,
        high, low, close, volume, <indicator>} newest first, field names
        FMP-native camelCase. date is "YYYY-MM-DD" (1day) or
        "YYYY-MM-DD HH:MM:SS" (intraday). price_treatment:
        raw|split_adjusted|dividend_adjusted. in_progress: date of the newest
        bar while it forms. On error: {error: <code>, detail, symbol}.
    """
    disp = _canonical(symbol)
    if timeframe not in _BAR_SIZES:
        return make_error(
            "invalid_argument",
            f"timeframe must be one of {', '.join(_BAR_SIZES)}, not {timeframe!r}",
            symbol=disp,
        )
    api = _fmp_symbol(symbol)
    try:
        client = await get_fmp_client()
    except Exception:  # noqa: BLE001
        return make_error("client_unavailable", _CLIENT_UNAVAILABLE, symbol=disp)

    try:
        ref = to_canonical(symbol, asset_class=AssetClass.EQUITY)
    except Exception:  # noqa: BLE001
        ref = None
    try:
        data = await client.get_technical_indicator(
            api, indicator=indicator, period=period, timeframe=timeframe
        )
        rows = data or []
        if timeframe == "1day":
            # FMP stamps a daily row with a midnight time; the contract is a bare date.
            rows = [
                {**row, "date": str(row["date"])[:10]}
                if isinstance(row, dict) and row.get("date") else row
                for row in rows
            ]
        newest = rows[0].get("date") if rows and isinstance(rows[0], dict) else None

        return make_response(
            rows,
            source=_SOURCE,
            symbol=disp,
            data_type="technical_indicator",
            indicator=indicator,
            period=period,
            timeframe=timeframe,
            **series_labels(_SOURCE, ref, timeframe, newest),
        )

    except Exception as e:  # noqa: BLE001
        return error_from_exception(e, _UPSTREAM_FAILED, symbol=disp)


if __name__ == "__main__":
    mcp.run(transport="stdio")
