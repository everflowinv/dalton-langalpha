"""Offline unit and regression tests for JPX market-wide and Kioxia margin ingestion."""

from __future__ import annotations

import datetime as dt
import importlib.util
import inspect
import io
import json
from pathlib import Path
import sys
from unittest.mock import create_autospec

import openpyxl
import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "openclaw_overlays"
    / "skills"
    / "jp-leverage-monitor"
    / "scripts"
    / "jp_leverage_monitor_cli.py"
)


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("jp_leverage_monitor_cli_test", CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows_to_xlsx_bytes(sheet_name: str, rows: list[list[object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = sheet_name
    for r_idx, row in enumerate(rows, start=1):
        for c_idx, val in enumerate(row, start=1):
            if val is not None:
                ws.cell(row=r_idx, column=c_idx, value=val)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_combined_history_rows(
    data_rows: list[list[object]] | None = None,
) -> list[list[object]]:
    """Build rows matching the official JPX `/05.html` combined history layout (`信用取引現在高`)."""
    header_rows: list[list[object]] = [
        ["信用取引現在高"] + [None] * 24 + [dt.datetime(2026, 11, 4)],
        ["Current outstanding margin trading"] + [None] * 24 + ["東京証券取引所株式部"],
        [None] * 25 + ["申込日基準　Base：application date"],
        [
            "月日",
            "制度信用取引 Standardized Margin Trading",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "一般信用取引 Negotiable Margin Trading",
            None,
            None,
            None,
            "合計 Total",
            None,
            None,
            None,
            "社内対当",
            "貸借取引残高  　",
            None,
            None,
            None,
            "自己貸株",
            None,
            "自己融資",
            None,
        ],
        [
            None,
            "自己 Proprietary Account",
            None,
            None,
            None,
            "委託 Customer Account",
            None,
            None,
            None,
            "委託 Customer Account",
            None,
            None,
            None,
            "売残高",
            None,
            "買残高",
            None,
            "Amount \nMatched\nWithin\nEach\nMember",
            "Outstanding Loans by Securities Finance Co.",
            None,
            None,
            None,
            "Shares Lent\nby Members",
            None,
            "Funds Lent\nby Members",
            None,
        ],
        [
            "Month\nor\nDay",
            "売残高  Shares Sold Short",
            None,
            "買残高  Shares Bought on Margin",
            None,
            "売残高  Shares Sold Short",
            None,
            "買残高  Shares Bought on Margin",
            None,
            "売残高  Shares Sold Short",
            None,
            "買残高  Shares Bought on Margin",
            None,
            "Shares Sold Short",
            None,
            "Shares Bought on Margin",
            None,
            None,
            "貸株　　Shares",
            None,
            "融資　Funds",
            None,
            None,
            None,
            None,
            None,
        ],
        [
            None,
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数(A)",
            "金額",
            "株数(B)",
            "金額",
            None,
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
        ],
        [
            None,
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            None,
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
            "Shares",
            "Value",
        ],
        [
            None,
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
            "千株 thous.shs.",
            "百万円 \\mil.",
        ],
        ["[東京・名古屋 Tokyo & Nagoya]"] + [None] * 25,
    ]
    if data_rows is None:
        data_rows = [
            [
                dt.datetime(2026, 9, 25),
                79560,
                108176,
                1788,
                2136,
                228944,
                462841,
                1644350,
                3556056,
                236636,
                476220,
                1857331,
                2905644,
                545141,
                1047238,
                3503470,
                6463837,
                170683,
                162882,
                217547,
                256514,
                466788,
                211575,
                393582,
                3076271,
                5345429,
            ],
            [
                dt.datetime(2026, 10, 30),
                78597,
                109681,
                2324,
                2617,
                192876,
                356550,
                1706353,
                3557539,
                77755,
                177627,
                1946176,
                2945730,
                349230,
                643859,
                3654854,
                6505887,
                149805,
                137736,
                195538,
                262736,
                385576,
                61687,
                130690,
                3242312,
                5070990,
            ],
        ]
    footer_rows: list[list[object]] = [
        [None] * 26,
        ["注:", "(1)信用取引現在高は、東京・名古屋の二市場における株券..."] + [None] * 24,
    ]
    return header_rows + data_rows + footer_rows


def _build_current_week_rows(
    *,
    app_date: object = dt.datetime(2026, 10, 30),
    pub_date: object = dt.datetime(2026, 11, 4),
    scope_e6: str = "二市場 Tokyo&Nagoya",
    extra_cols: int = 0,
) -> list[list[object]]:
    """Build rows matching the official JPX `/04.html` `YYYYMMDD_mtcurrent.xlsx` layout (`レイアウト`)."""
    base_rows: list[list[object]] = [
        [None, app_date, None, None, None, None, None, None, None, None, None, None],
        [None, app_date, app_date, None, None, None, None, None, None, None, None, None],
        [None, pub_date, pub_date, None, None, None, None, None, None, None, None, None],
        [None] * 11 + ["東京証券取引所株式部 Tokyo Stock Exchange, Equities Dept."],
        [None] * 11 + ["(単位：千株、百万円）(Unit: thou. shares, JPY mil.)"],
        [None, None, None, None, scope_e6, None, None, None, "東京 Tokyo", None, None, None],
        [
            None,
            None,
            None,
            None,
            "売残高・\n貸株残高\nSales",
            "前週比\nWeekly change",
            "買残高・\n融資残高\nPurchases",
            "前週比\nWeekly change",
            "売残高・\n貸株残高\nSales",
            "前週比\nWeekly change",
            "買残高・\n融資残高\nPurchases",
            "前週比\nWeekly change",
        ],
        [
            None,
            "制度信用取引残高\nOutstanding Standardized Margin Trading",
            "自己\nProprietary Account",
            "株数Shs.",
            78597.0,
            -2593.0,
            2324.0,
            486.0,
            78597.0,
            -2593.0,
            2324.0,
            486.0,
        ],
        [
            None,
            None,
            None,
            "金額Val.",
            109681.0,
            -4229.0,
            2617.0,
            -142.0,
            109680.0,
            -4229.0,
            2617.0,
            -142.0,
        ],
        [
            None,
            None,
            "委託\nCustomer Account",
            "株数Shs.",
            192876.0,
            -14022.0,
            1706353.0,
            -11397.0,
            192874.0,
            -14022.0,
            1705742.0,
            -11363.0,
        ],
        [
            None,
            None,
            None,
            "金額Val.",
            356550.0,
            -10566.0,
            3557539.0,
            -27207.0,
            356547.0,
            -10566.0,
            3556536.0,
            -27213.0,
        ],
        [
            None,
            "一般信用取引残高\nOutstanding Negotiable Margin Trading",
            "委託\nCustomer Account",
            "株数Shs.",
            77755.0,
            -9220.0,
            1946176.0,
            5235.0,
            77755.0,
            -9220.0,
            1944061.0,
            5099.0,
        ],
        [
            None,
            None,
            None,
            "金額Val.",
            177627.0,
            -29920.0,
            2945730.0,
            -835.0,
            177626.0,
            -29919.0,
            2943296.0,
            -991.0,
        ],
        [
            None,
            "信用取引残高合計\nTotal Outstanding Margin Trading",
            None,
            "株数Shs.",
            349230.0,
            -25835.0,
            3654854.0,
            -5675.0,
            349226.0,
            -25834.0,
            3652127.0,
            -5778.0,
        ],
        [
            None,
            None,
            None,
            "金額Val.",
            643859.0,
            -44714.0,
            6505887.0,
            -28183.0,
            643855.0,
            -44714.0,
            6502450.0,
            -28345.0,
        ],
        [
            None,
            "社内対当\nMatched Internally",
            None,
            "株数Shs.",
            149805.0,
            -4744.0,
            149805.0,
            -4744.0,
            149802.0,
            -4746.0,
            149802.0,
            -4746.0,
        ],
        [
            None,
            "貸借取引残高\n Outstanding loans by JSF",
            None,
            "株数Shs.",
            137736.0,
            -11487.0,
            262736.0,
            21000.0,
            137735.0,
            -11486.0,
            262273.0,
            21036.0,
        ],
        [
            None,
            None,
            None,
            "金額Val.",
            195538.0,
            -6948.0,
            385576.0,
            38496.0,
            195537.0,
            -6947.0,
            384974.0,
            38532.0,
        ],
        [
            None,
            "自己貸株・自己融資\nNon-JSF shares,Non-JSF funds",
            None,
            "株数Shs.",
            61687.0,
            -9604.0,
            3242312.0,
            -21932.0,
            61687.0,
            -9604.0,
            3240051.0,
            -22069.0,
        ],
        [
            None,
            None,
            None,
            "金額Val.",
            130690.0,
            -28251.0,
            5070990.0,
            4503.0,
            130689.0,
            -28251.0,
            5068017.0,
            4285.0,
        ],
    ]
    if extra_cols <= 0:
        return base_rows
    return [row + [11111.0] * extra_cols for row in base_rows]


def _build_legacy_mtgk_rows() -> list[list[object]]:
    """Build rows matching legacy `20260925_mtgk.xls` detailed history through 2026-09-18."""
    return [
        ["信用取引現在高（一般信用取引・制度信用取引別）"] + [None] * 12,
        [
            "月日",
            "売残高 合計",
            None,
            "買残高 合計",
            None,
            "売残高 一般信用取引",
            None,
            "売残高 制度信用取引",
            None,
            "買残高 一般信用取引",
            None,
            "買残高 制度信用取引",
            None,
        ],
        [
            None,
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
        ],
        [
            dt.datetime(2026, 9, 18),
            510000,
            980000,
            3400000,
            6100000,
            210000,
            420000,
            300000,
            560000,
            1750000,
            2800000,
            1650000,
            3300000,
        ],
    ]


def _build_legacy_mtsk_rows() -> list[list[object]]:
    """Build rows matching legacy `20260925_mtsk.xls` credit-total / JSF history through 2026-09-18."""
    return [
        ["信用取引現在高"] + [None] * 21,
        [
            "月日",
            "自己 売残高",
            None,
            "自己 買残高",
            None,
            "委託 売残高",
            None,
            "委託 買残高",
            None,
            "合計 売残高",
            None,
            "合計 買残高",
            None,
            "社内対当",
            "貸借取引残高 貸株",
            None,
            "貸借取引残高 融資",
            None,
            "自己貸株",
            None,
            "自己融資",
            None,
        ],
        [
            None,
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
            "株数",
            "金額",
        ],
        [
            dt.datetime(2026, 9, 18),
            70000,
            95000,
            1500,
            2000,
            440000,
            885000,
            3398500,
            6098000,
            510000,
            980000,
            3400000,
            6100000,
            160000,
            155000,
            205000,
            245000,
            430000,
            200000,
            370000,
            2900000,
            5100000,
        ],
    ]


KIOXIA_SAMPLE_PAGE_TEXT = """\
                                                                                                                                                                 銘柄別信用取引残高
                                                                                                                                                        Outstanding margin trading by issue
                           2026/11/6 申込み現在                                                                                                                                                                                                                        東京証券取引所株式部                                                （単位：一株、一円）                    2026/11/9
                     As of 2026/11/6 application based                                                                                                                                                                                                            Tokyo Stock Exchange, Equities Dept.                    (Unit: 1 share, 1 JPY)
                                                          銘柄                                                                              合計 Total                                                                               売残高 Outstanding Sales                                                       買残高 Outstanding Purchases
                                           市場             種別     コード        新証券コード                                                         上場比              買残高                                 上場比
    銘     柄 Issue                                                                                         売残高               前日比                                                前日比                            一般信用               前日比              制度信用                  前日比               一般信用                前日比                 制度信用                 前日比
                                          Section       Loan/Ma   Code      New Sec. Code                                                 Ratio to       Outstanding                           Ratio to
                                                          rgin                                       Outstanding Sales   Daily change                                       Daily change                     Negotiable       Daily change      Standardized         Daily change        Negotiable        Daily change         Standardized        Daily change
                                                                                                                                        listed shares     Purchases                         listed shares
    Kioxia Holdings Corporation      Prime              Loan        285A0     JP3236330001 金額 Val.      21,507,123,538    ▲ 546,404,920             -    746,337,253,388   ▲ 96,872,210,549              -    7,595,480,020     1,118,438,010    13,911,643,518     ▲ 1,664,842,930     250,450,133,720   ▲ 38,678,475,430       495,887,119,668   ▲ 58,193,735,119
B   キオクシアホールディングス 普通株式               プライム               貸           285A0     JP3236330001 株数 Shs.           2,207,100          128,400          0.1%         48,227,800       ▲ 4,622,300            2.9%          787,400           161,700         1,419,700           ▲ 33,300           17,601,100       ▲ 1,771,700             30,626,700       ▲ 2,850,600
"""


def test_combined_history_workbook_schema_parsing() -> None:
    cli = _load_cli_module()
    xlsx_bytes = _rows_to_xlsx_bytes("信用取引現在高", _build_combined_history_rows())

    rows = cli.parse_margin_book(xlsx_bytes)
    assert len(rows) == 2

    sep25, oct30 = rows[0], rows[1]
    assert sep25 == {
        "date": "2026-09-25",
        "short_shares_k": 545141.0,
        "short_value_mjpy": 1047238.0,
        "long_shares_k": 3503470.0,
        "long_value_mjpy": 6463837.0,
        "neg_short_value_mjpy": 476220.0,
        "std_short_value_mjpy": 108176.0 + 462841.0,
        "neg_long_value_mjpy": 2905644.0,
        "std_long_value_mjpy": 2136.0 + 3556056.0,
        "jsf_stock_loan_value_mjpy": 217547.0,
        "jsf_funding_value_mjpy": 466788.0,
    }
    assert oct30 == {
        "date": "2026-10-30",
        "short_shares_k": 349230.0,
        "short_value_mjpy": 643859.0,
        "long_shares_k": 3654854.0,
        "long_value_mjpy": 6505887.0,
        "neg_short_value_mjpy": 177627.0,
        "std_short_value_mjpy": 466231.0,
        "neg_long_value_mjpy": 2945730.0,
        "std_long_value_mjpy": 3560156.0,
        "jsf_stock_loan_value_mjpy": 195538.0,
        "jsf_funding_value_mjpy": 385576.0,
    }


def test_current_weekly_workbook_uses_application_date_tokyo_nagoya_and_published_totals() -> None:
    cli = _load_cli_module()
    xlsx_bytes = _rows_to_xlsx_bytes("レイアウト", _build_current_week_rows())

    rows = cli.parse_margin_book(xlsx_bytes)
    assert len(rows) == 1
    row = rows[0]

    # B1 application date (2026-10-30) is used rather than B3 publication date (2026-11-04).
    assert row["date"] == "2026-10-30"
    # Published totals at rows 14/15 (643859 and 6505887) are used rather than rounded component sums
    # (109681 + 356550 + 177627 = 643858; 2617 + 3557539 + 2945730 = 6505886), and Tokyo-only cols I/K
    # (349226 / 643855 / 3652127 / 6502450) and weekly changes F/H are not confused.
    assert row == {
        "date": "2026-10-30",
        "short_shares_k": 349230.0,
        "short_value_mjpy": 643859.0,
        "long_shares_k": 3654854.0,
        "long_value_mjpy": 6505887.0,
        "neg_short_value_mjpy": 177627.0,
        "std_short_value_mjpy": 466231.0,
        "neg_long_value_mjpy": 2945730.0,
        "std_long_value_mjpy": 3560156.0,
        "jsf_stock_loan_value_mjpy": 195538.0,
        "jsf_funding_value_mjpy": 385576.0,
    }


def test_old_history_compatibility_and_complementary_field_merge(tmp_path: Path) -> None:
    cli = _load_cli_module()
    mtgk_bytes = _rows_to_xlsx_bytes("Sheet1", _build_legacy_mtgk_rows())
    mtsk_bytes = _rows_to_xlsx_bytes("Sheet1", _build_legacy_mtsk_rows())

    detailed_rows = cli.parse_margin_book(mtgk_bytes, True)
    jsf_rows = cli.parse_margin_book(mtsk_bytes, False)
    assert len(detailed_rows) == 1
    assert len(jsf_rows) == 1
    assert detailed_rows[0]["date"] == "2026-09-18"
    assert detailed_rows[0]["short_shares_k"] == 510000.0
    assert "jsf_stock_loan_value_mjpy" not in detailed_rows[0]
    assert jsf_rows[0] == {
        "date": "2026-09-18",
        "jsf_stock_loan_value_mjpy": 205000.0,
        "jsf_funding_value_mjpy": 430000.0,
    }

    csv_path = tmp_path / "margin_weekly.csv"
    # Pre-seed an older historical row that must be preserved.
    cli.merge_margin_rows(
        csv_path,
        [
            {
                "date": "2026-09-11",
                "short_shares_k": 500000.0,
                "short_value_mjpy": 950000.0,
                "long_shares_k": 3300000.0,
                "long_value_mjpy": 6000000.0,
                "neg_short_value_mjpy": 400000.0,
                "std_short_value_mjpy": 550000.0,
                "neg_long_value_mjpy": 2700000.0,
                "std_long_value_mjpy": 3300000.0,
                "jsf_stock_loan_value_mjpy": 200000.0,
                "jsf_funding_value_mjpy": 410000.0,
            }
        ],
    )

    # 1. Write detailed margin row first, then merge partial JSF row: margin columns must not be erased.
    cli.merge_margin_rows(csv_path, detailed_rows)
    cli.merge_margin_rows(csv_path, jsf_rows)
    stored = cli.read_rows(csv_path)
    assert set(stored) == {"2026-09-11", "2026-09-18"}
    assert float(stored["2026-09-18"]["short_shares_k"]) == 510000.0
    assert float(stored["2026-09-18"]["long_value_mjpy"]) == 6100000.0
    assert float(stored["2026-09-18"]["jsf_stock_loan_value_mjpy"]) == 205000.0
    assert float(stored["2026-09-18"]["jsf_funding_value_mjpy"]) == 430000.0

    # 2. Now merge a partial margin record (missing JSF keys and with explicit None): JSF values must not be erased.
    partial_margin_update = [
        {
            "date": "2026-09-18",
            "short_shares_k": 512000.0,
            "jsf_stock_loan_value_mjpy": None,
            "jsf_funding_value_mjpy": "",
        }
    ]
    cli.merge_rows(
        csv_path,
        cli.MARGIN_HEADER,
        partial_margin_update,
        preserve_complementary=True,
    )
    stored_after = cli.read_rows(csv_path)
    assert float(stored_after["2026-09-18"]["short_shares_k"]) == 512000.0
    assert float(stored_after["2026-09-18"]["long_value_mjpy"]) == 6100000.0
    assert float(stored_after["2026-09-18"]["jsf_stock_loan_value_mjpy"]) == 205000.0
    assert float(stored_after["2026-09-18"]["jsf_funding_value_mjpy"]) == 430000.0
    assert float(stored_after["2026-09-11"]["short_shares_k"]) == 500000.0
    # Ensure no leftover temp files remain next to the CSV.
    assert list(tmp_path.glob("*.tmp")) == []


def test_non_margin_providers_overwrite_same_day_explicit_nulls_while_margin_preserves_complementary_fields(
    tmp_path: Path,
) -> None:
    cli = _load_cli_module()

    # Validate helper signatures via inspect.signature.bind.
    inspect.signature(cli.merge_rows).bind(
        tmp_path / "jsf.csv",
        cli.JSF_HIST_HEADER,
        [],
        ("date", "code"),
        preserve_complementary=False,
    )
    inspect.signature(cli.merge_margin_rows).bind(
        tmp_path / "margin_weekly.csv",
        [],
        cli.MARGIN_HEADER,
        ("date",),
    )

    # 1. Non-margin providers (default preserve_complementary=False) must overwrite prior values
    #    when a same-key update explicitly sets a field to None (e.g. JSF loan_price_jpy=None on "-").
    jsf_csv = tmp_path / "jsf_issue_history.csv"
    cli.merge_rows(
        jsf_csv,
        cli.JSF_HIST_HEADER,
        [
            {
                "date": "2026-11-06",
                "code": "285A",
                "name": "Kioxia Holdings",
                "funding_shares": 120000,
                "stockloan_shares": 45000,
                "net_shares": 75000,
                "loan_price_jpy": 1650.0,
            }
        ],
        ("date", "code"),
    )
    assert float(cli.read_rows(jsf_csv, ("date", "code"))["2026-11-06|285A"]["loan_price_jpy"]) == 1650.0

    cli.merge_rows(
        jsf_csv,
        cli.JSF_HIST_HEADER,
        [
            {
                "date": "2026-11-06",
                "code": "285A",
                "name": "Kioxia Holdings",
                "funding_shares": 125000,
                "stockloan_shares": 40000,
                "net_shares": 85000,
                "loan_price_jpy": None,
            }
        ],
        ("date", "code"),
    )
    jsf_stored = cli.read_rows(jsf_csv, ("date", "code"))["2026-11-06|285A"]
    assert int(jsf_stored["funding_shares"]) == 125000
    assert jsf_stored["loan_price_jpy"] == ""

    # 2. JPX market-wide margin (merge_margin_rows / preserve_complementary=True) still preserves
    #    complementary fields across partial detailed-margin and JSF records on the same date.
    margin_csv = tmp_path / "margin_weekly.csv"
    cli.merge_margin_rows(
        margin_csv,
        [
            {
                "date": "2026-09-18",
                "short_shares_k": 510000.0,
                "short_value_mjpy": 980000.0,
                "long_shares_k": 3400000.0,
                "long_value_mjpy": 6100000.0,
                "neg_short_value_mjpy": 420000.0,
                "std_short_value_mjpy": 560000.0,
                "neg_long_value_mjpy": 2800000.0,
                "std_long_value_mjpy": 3300000.0,
            }
        ],
    )
    cli.merge_margin_rows(
        margin_csv,
        [
            {
                "date": "2026-09-18",
                "short_shares_k": None,
                "jsf_stock_loan_value_mjpy": 205000.0,
                "jsf_funding_value_mjpy": 430000.0,
            }
        ],
    )
    margin_stored = cli.read_rows(margin_csv)["2026-09-18"]
    assert float(margin_stored["short_shares_k"]) == 510000.0
    assert float(margin_stored["long_value_mjpy"]) == 6100000.0
    assert float(margin_stored["jsf_stock_loan_value_mjpy"]) == 205000.0
    assert float(margin_stored["jsf_funding_value_mjpy"]) == 430000.0


def test_explicit_finite_zero_preserved_in_workbooks_pdf_and_merges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module()

    zero_data_row = [
        dt.datetime(2026, 10, 30),
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    xlsx_bytes = _rows_to_xlsx_bytes("信用取引現在高", _build_combined_history_rows([zero_data_row]))
    parsed = cli.parse_margin_book(xlsx_bytes)
    assert len(parsed) == 1
    for col in cli.MARGIN_HEADER[1:]:
        assert parsed[0][col] == 0.0

    csv_path = tmp_path / "margin_weekly.csv"
    cli.merge_margin_rows(
        csv_path,
        [
            {
                "date": "2026-10-30",
                "short_shares_k": 100.0,
                "short_value_mjpy": 200.0,
                "long_shares_k": 300.0,
                "long_value_mjpy": 400.0,
                "neg_short_value_mjpy": 50.0,
                "std_short_value_mjpy": 150.0,
                "neg_long_value_mjpy": 100.0,
                "std_long_value_mjpy": 300.0,
                "jsf_stock_loan_value_mjpy": 25.0,
                "jsf_funding_value_mjpy": 75.0,
            }
        ],
    )
    # Overwrite with explicit zeros, then merge a partial row with None: zeros must be retained.
    cli.merge_margin_rows(csv_path, parsed)
    cli.merge_rows(
        csv_path,
        cli.MARGIN_HEADER,
        [{"date": "2026-10-30", "short_shares_k": None, "jsf_funding_value_mjpy": float("nan")}],
        preserve_complementary=True,
    )
    stored = cli.read_rows(csv_path)["2026-10-30"]
    for col in cli.MARGIN_HEADER[1:]:
        assert float(stored[col]) == 0.0

    zero_pdf_text = (
        "2026/11/6 申込み現在  （単位：一株、一円） 2026/11/9\n"
        "B キオクシアホールディングス 普通株式 プライム 貸 285A0 JP3236330001 株数 Shs. "
        "0 0 * 0 0 - 0 0 0 0 0 0 0 0\n"
    )
    orig_pdf_text = cli.pdf_text
    pdf_text_mock = create_autospec(orig_pdf_text, return_value=zero_pdf_text)
    monkeypatch.setattr(cli, "pdf_text", pdf_text_mock)
    kioxia_zero = cli.parse_kioxia_pdf(b"%PDF-stub", "https://www.jpx.co.jp/01/20261106_mtall.pdf")
    assert kioxia_zero == {
        "date": "2026-11-06",
        "code": "285A",
        "name": "Kioxia Holdings",
        "short_shares": 0,
        "short_change": 0,
        "long_shares": 0,
        "long_change": 0,
        "src": "https://www.jpx.co.jp/01/20261106_mtall.pdf",
    }


def test_unknown_workbook_schemas_rejected() -> None:
    cli = _load_cli_module()

    # 1. Completely unrelated spreadsheet with dates and numbers.
    unrelated = _rows_to_xlsx_bytes(
        "Unrelated",
        [
            ["Date", "Open", "High", "Low", "Close"],
            [dt.datetime(2026, 10, 30), 100, 110, 95, 105],
        ],
    )
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_book(unrelated)

    # 2. Current weekly workbook with Tokyo-only scope at E6 instead of Tokyo & Nagoya.
    tokyo_only_week = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(scope_e6="東京 Tokyo"),
    )
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_book(tokyo_only_week)

    # 3. Current weekly workbook where E7 is weekly change instead of balance.
    bad_cols_week_rows = _build_current_week_rows()
    bad_cols_week_rows[6][4] = "前週比\nWeekly change"
    bad_cols_week = _rows_to_xlsx_bytes("レイアウト", bad_cols_week_rows)
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_book(bad_cols_week)

    # 4. Combined history workbook where dates are plain numbers instead of worksheet dates.
    non_date_hist_rows = _build_combined_history_rows(
        [[20261030] + [100] * 25]
    )
    non_date_hist = _rows_to_xlsx_bytes("信用取引現在高", non_date_hist_rows)
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_book(non_date_hist)

    # 5. Current weekly workbook with text application date and >=15 columns but mismatched scope or balance header
    #    must be rejected by both parse_margin_current_book and parse_margin_book without legacy fallback.
    text_date_bad_scope = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date="2026/11/6 申込現在",
            pub_date="2026/11/10 公表",
            scope_e6="東京 Tokyo",
            extra_cols=4,
        ),
    )
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_current_book(text_date_bad_scope)
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_book(text_date_bad_scope)

    text_date_bad_header_rows = _build_current_week_rows(
        app_date="2026/11/6 申込現在",
        pub_date="2026/11/10 公表",
        extra_cols=4,
    )
    text_date_bad_header_rows[6][4] = "前週比\nWeekly change"
    text_date_bad_header = _rows_to_xlsx_bytes("レイアウト", text_date_bad_header_rows)
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_current_book(text_date_bad_header)
    with pytest.raises(ValueError, match="Unrecognized"):
        cli.parse_margin_book(text_date_bad_header)


def test_parse_kioxia_pdf_date_units_signs_ratios_and_legacy_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module()
    pdf_text = cli.pdf_text

    # 1. Official page 22 layout with English monetary row placed BEFORE the Japanese share row
    #    to verify monetary row is rejected and share row is selected.
    pdf_text_mock = create_autospec(pdf_text, return_value=KIOXIA_SAMPLE_PAGE_TEXT)
    monkeypatch.setattr(cli, "pdf_text", pdf_text_mock)
    rec = cli.parse_kioxia_pdf(
        b"%PDF-1.4",
        "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/20261106_mtall.pdf",
    )
    assert rec == {
        "date": "2026-11-06",
        "code": "285A",
        "name": "Kioxia Holdings",
        "short_shares": 2207100,
        "short_change": 128400,
        "long_shares": 48227800,
        "long_change": -4622300,
        "src": "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/20261106_mtall.pdf",
    }

    # 2. Tight `▲` sign without whitespace and `*` / `-` ratio placeholders.
    tight_sign_text = (
        "As of 2026/11/5 application based  (Unit: 1 share, 1 JPY)  2026/11/6\n"
        "B キオクシアホールディングス 普通株式 プライム 貸 285A0 JP3236330001 株数 Shs. "
        "2,078,700 ▲15,200 * 52,850,100 ▲1,100,000 - 600,000 ▲5,000 1,478,700 ▲10,200 19,000,000 0 33,850,100 ▲1,100,000\n"
    )
    monkeypatch.setattr(
        cli,
        "pdf_text",
        create_autospec(pdf_text, return_value=tight_sign_text),
    )
    rec_tight = cli.parse_kioxia_pdf(b"%PDF-1.4", "https://www.jpx.co.jp/20261105_mtall.pdf")
    assert rec_tight == {
        "date": "2026-11-05",
        "code": "285A",
        "name": "Kioxia Holdings",
        "short_shares": 2078700,
        "short_change": -15200,
        "long_shares": 52850100,
        "long_change": -1100000,
        "src": "https://www.jpx.co.jp/20261105_mtall.pdf",
    }

    # 3. Older compatible weekly PDF layout without `株数 Shs.` marker.
    legacy_pdf_text = (
        "2026/9/18 申込み現在\n"
        "キオクシアホールディングス 285A0 JP3236330001 1,500,000 ▲ 20,000 35,000,000 450,000\n"
    )
    monkeypatch.setattr(
        cli,
        "pdf_text",
        create_autospec(pdf_text, return_value=legacy_pdf_text),
    )
    rec_legacy = cli.parse_kioxia_pdf(b"%PDF-1.4", "https://www.jpx.co.jp/syumatsu2026091800.pdf")
    assert rec_legacy == {
        "date": "2026-09-18",
        "code": "285A",
        "name": "Kioxia Holdings",
        "short_shares": 1500000,
        "short_change": -20000,
        "long_shares": 35000000,
        "long_change": 450000,
        "src": "https://www.jpx.co.jp/syumatsu2026091800.pdf",
    }

    # 4. Incidental substring without security code/ISIN or only monetary row must return None.
    incidental_text = (
        "2026/11/6 申込み現在\n"
        "キオクシアホールディングス に関する注記 100 200 300 400\n"
        "Kioxia Holdings Corporation Prime Loan 285A0 JP3236330001 金額 Val. 21,507,123,538 ▲ 546,404,920 - 746,337,253,388 ▲ 96,872,210,549 -\n"
    )
    monkeypatch.setattr(
        cli,
        "pdf_text",
        create_autospec(pdf_text, return_value=incidental_text),
    )
    assert cli.parse_kioxia_pdf(b"%PDF-1.4", "https://www.jpx.co.jp/20261106_mtall.pdf") is None


def test_link_discovery_independent_of_page_ordering_and_relative_url_resolution() -> None:
    cli = _load_cli_module()

    history_html_order_a = """
    <div class="component-file">
      <table>
        <tr>
          <th>信用取引現在高</th>
          <td><a href="tvdivq0000001rq1-att/tvdivq0000015969.xlsx">Excel</a></td>
          <td><a href="tvdivq0000001rq1-att/tvdivq0000015980.pdf">PDF</a></td>
        </tr>
      </table>
      <table>
        <tr>
          <th>信用取引現在高（一般信用取引・制度信用取引別）</th>
          <td><a href="/markets/statistics-equities/margin/tvdivq0000001rq1-att/20260925_mtgk.xls">Excel</a></td>
        </tr>
        <tr>
          <th>信用取引現在高</th>
          <td><a href="./tvdivq0000001rq1-att/20260925_mtsk.xls">Excel</a></td>
        </tr>
      </table>
    </div>
    """
    history_html_order_b = """
    <div class="component-file">
      <table>
        <tr>
          <th>信用取引現在高</th>
          <td><a href="tvdivq0000001rq1-att/20260925_mtsk.xls">Excel</a></td>
        </tr>
        <tr>
          <th>信用取引現在高（一般信用取引・制度信用取引別）</th>
          <td><a href="tvdivq0000001rq1-att/20260925_mtgk.xls">Excel</a></td>
        </tr>
        <tr>
          <th>信用取引現在高</th>
          <td><a href="tvdivq0000001rq1-att/tvdivq0000015969.xlsx">Excel</a></td>
        </tr>
      </table>
    </div>
    """

    links_a = cli.discover_margin_history_links(cli.MARGIN_HISTORY_URL, html=history_html_order_a)
    links_b = cli.discover_margin_history_links(cli.MARGIN_HISTORY_URL, html=history_html_order_b)
    expected_history = {
        "combined": [
            "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rq1-att/tvdivq0000015969.xlsx"
        ],
        "detailed": [
            "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rq1-att/20260925_mtgk.xls"
        ],
        "jsf": [
            "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rq1-att/20260925_mtsk.xls"
        ],
    }
    assert links_a == expected_history
    assert links_b == expected_history

    current_html = """
    <table>
      <tr><th>Trading Ratio</th><td><a href="tvdivq-att/20261030_mtratio.xlsx">Ratio</a></td></tr>
      <tr><th>2026年10月23日申込現在</th><td><a href="tvdivq0000001rk9-att/20261023_mtcurrent.xls">Excel</a></td></tr>
      <tr><th>2026年10月30日申込現在</th><td><a href="/markets/statistics-equities/margin/tvdivq0000001rk9-att/20261030_mtcurrent.xlsx">Excel</a></td></tr>
    </table>
    """
    assert cli.discover_margin_current_links(cli.MARGIN_CURRENT_URL, html=current_html) == [
        "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rk9-att/20261023_mtcurrent.xls",
        "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rk9-att/20261030_mtcurrent.xlsx",
    ]

    stocks_html = """
    <table>
      <tr><th>一覧</th><td><a href="tvdivq0000001rnl-att/margin_issues.xlsx">Issues</a></td></tr>
      <tr><th>2026年11月6日申込分</th><td><a href="tvdivq0000001rnl-att/20261106_mtall.pdf">PDF</a></td></tr>
    </table>
    """
    assert cli.discover_kioxia_pdf_links(cli.STOCK_MARGIN_URL, html=stocks_html) == [
        "https://www.jpx.co.jp/markets/statistics-equities/margin/tvdivq0000001rnl-att/20261106_mtall.pdf"
    ]


def _seed_dashboard_inputs(cli, data_dir: Path, include_margin: bool = True) -> None:
    if include_margin:
        cli.merge_margin_rows(
            data_dir / "margin_weekly.csv",
            [
                {
                    "date": f"2026-10-{d:02d}",
                    "short_shares_k": 340000.0 + d,
                    "short_value_mjpy": 640000.0 + d,
                    "long_shares_k": 3600000.0 + d,
                    "long_value_mjpy": 6500000.0 + d,
                    "neg_short_value_mjpy": 170000.0,
                    "std_short_value_mjpy": 470000.0,
                    "neg_long_value_mjpy": 2900000.0,
                    "std_long_value_mjpy": 3600000.0,
                    "jsf_stock_loan_value_mjpy": 190000.0,
                    "jsf_funding_value_mjpy": 380000.0,
                }
                for d in range(1, 7)
            ],
        )
    cli.merge_rows(
        data_dir / "etf_nav_units_daily.csv",
        cli.ETF_HEADER,
        [
            {
                "date": f"2026-10-{d:02d}",
                "code": code,
                "name": cli.ETF_SPECS[code][0],
                "leverage": cli.ETF_SPECS[code][1],
                "aum_bnjpy": 300.0 + d,
                "shares_os": 10000000.0 + d * 1000,
                "nav": 30000.0,
                "return_pct": 0.5,
                "creation_redemption_shares": 0.0,
                "aum_after_bnjpy": 300.0 + d,
            }
            for d in range(1, 7)
            for code in ("1570", "1357")
        ],
        ("date", "code"),
    )
    cli.merge_rows(
        data_dir / "etf_prices_daily.csv",
        cli.ETF_PX_HEADER,
        [
            {
                "date": "2026-10-06",
                "code": code,
                "name": cli.ETF_SPECS[code][0],
                "open": 29900.0,
                "high": 30100.0,
                "low": 29800.0,
                "close": 30000.0,
                "volume": 1000000.0,
                "turnover_bnjpy": 30.0,
            }
            for code in ("1570", "1357")
        ],
        ("date", "code"),
    )


def _write_reference_fixture(cli, data_dir: Path) -> None:
    cli.merge_rows(
        data_dir / "market_reference.csv",
        cli.REF_HEADER,
        [
            {
                "date": f"2026-10-{d:02d}",
                "nikkei_close": 39000.0 + d * 10,
                "nikkei_open": 38900.0,
                "nikkei_high": 39100.0,
                "nikkei_low": 38800.0,
                "nikkei_vi_close": 20.0 + d * 0.1,
                "nikkei_vi_open": 20.0,
                "nikkei_vi_high": 21.0,
                "nikkei_vi_low": 19.5,
            }
            for d in range(1, 7)
        ],
    )


def test_jpx_refresh_failure_preserves_unrelated_sources_and_exposes_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()
    out_root = tmp_path / "jp-leverage"
    data_dir = out_root / "data"
    html_path = out_root / "index.html"
    monkeypatch.setattr(cli, "OUT_ROOT", out_root)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(cli, "HTML_PATH", html_path)

    _seed_dashboard_inputs(cli, data_dir, include_margin=True)

    orig_get = cli.get
    orig_pdf_text = cli.pdf_text
    orig_sync_margin_weekly = cli.sync_margin_weekly
    orig_sync_jsf_daily = cli.sync_jsf_daily
    orig_sync_jsf_issue_history = cli.sync_jsf_issue_history
    orig_sync_investor_flows = cli.sync_investor_flows
    orig_sync_short_selling = cli.sync_short_selling
    orig_sync_etf_units = cli.sync_etf_units
    orig_sync_etf_prices = cli.sync_etf_prices
    orig_sync_reference = cli.sync_reference

    def _fake_get(url: str, **kw) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        if url == cli.STOCK_MARGIN_URL:
            resp.text = '<a href="tvdivq-att/20261106_mtall.pdf">PDF</a>'
            resp.content = resp.text.encode("utf-8")
            return resp
        if url.endswith("20261106_mtall.pdf"):
            resp.content = b"%PDF-no-kioxia"
            resp.text = ""
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(cli, "get", create_autospec(orig_get, side_effect=_fake_get))
    monkeypatch.setattr(
        cli,
        "pdf_text",
        create_autospec(orig_pdf_text, return_value="2026/11/6 申込み現在\nempty page\n"),
    )

    # Kioxia sync must raise when all discovered PDFs yield 0 rows.
    with pytest.raises(RuntimeError, match="Kioxia daily margin refresh yielded 0 rows"):
        cli.sync_kioxia_weekly()

    def _stub_reference() -> None:
        _write_reference_fixture(cli, data_dir)

    monkeypatch.setattr(cli, "sync_margin_weekly", create_autospec(orig_sync_margin_weekly, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_daily", create_autospec(orig_sync_jsf_daily, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_issue_history", create_autospec(orig_sync_jsf_issue_history, return_value=None))
    monkeypatch.setattr(cli, "sync_investor_flows", create_autospec(orig_sync_investor_flows, return_value=None))
    monkeypatch.setattr(cli, "sync_short_selling", create_autospec(orig_sync_short_selling, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_units", create_autospec(orig_sync_etf_units, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_prices", create_autospec(orig_sync_etf_prices, return_value=None))
    monkeypatch.setattr(cli, "sync_reference", create_autospec(orig_sync_reference, side_effect=_stub_reference))
    monkeypatch.setattr(sys, "argv", [str(CLI_PATH), "--json", "run"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["update"]["ok"] is False
    assert "sync_reference" in payload["update"]["done"]
    assert any(w["step"] == "sync_kioxia_weekly" for w in payload["update"]["warnings"])
    assert (data_dir / "market_reference.csv").is_file()
    assert html_path.is_file()
    html_text = html_path.read_text(encoding="utf-8")
    assert "Kioxia 个股信用余额（JPX 全市场日频余额）" in html_text
    assert "信用买残(JPX日频)" in html_text


def test_latest_weekly_margin_failure_with_older_weekly_success_saves_valid_rows_and_exposes_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()
    out_root = tmp_path / "jp-leverage"
    data_dir = out_root / "data"
    html_path = out_root / "index.html"
    monkeypatch.setattr(cli, "OUT_ROOT", out_root)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(cli, "HTML_PATH", html_path)

    _seed_dashboard_inputs(cli, data_dir, include_margin=False)

    combined_bytes = _rows_to_xlsx_bytes("信用取引現在高", _build_combined_history_rows())
    older_week_bytes = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date=dt.datetime(2026, 10, 30),
            pub_date=dt.datetime(2026, 11, 4),
        ),
    )
    bad_latest_week_bytes = _rows_to_xlsx_bytes("Unrelated", [["A", "B"], [1, 2]])

    history_html = """
    <table>
      <tr><th>信用取引現在高</th><td><a href="tvdivq0000001rq1-att/tvdivq0000015969.xlsx">Excel</a></td></tr>
    </table>
    """
    current_html = """
    <table>
      <tr><th>2026年10月30日申込現在</th><td><a href="tvdivq0000001rk9-att/20261030_mtcurrent.xlsx">Excel</a></td></tr>
      <tr><th>2026年11月6日申込現在</th><td><a href="tvdivq0000001rk9-att/20261106_mtcurrent.xlsx">Excel</a></td></tr>
    </table>
    """

    orig_get = cli.get
    orig_sync_kioxia_weekly = cli.sync_kioxia_weekly
    orig_sync_jsf_daily = cli.sync_jsf_daily
    orig_sync_jsf_issue_history = cli.sync_jsf_issue_history
    orig_sync_investor_flows = cli.sync_investor_flows
    orig_sync_short_selling = cli.sync_short_selling
    orig_sync_etf_units = cli.sync_etf_units
    orig_sync_etf_prices = cli.sync_etf_prices
    orig_sync_reference = cli.sync_reference

    def _fake_get(url: str, **kw) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        if url == cli.MARGIN_HISTORY_URL:
            resp.text = history_html
            resp.content = history_html.encode("utf-8")
            return resp
        if url == cli.MARGIN_CURRENT_URL:
            resp.text = current_html
            resp.content = current_html.encode("utf-8")
            return resp
        if url.endswith("tvdivq0000015969.xlsx"):
            resp.content = combined_bytes
            return resp
        if url.endswith("20261030_mtcurrent.xlsx"):
            resp.content = older_week_bytes
            return resp
        if url.endswith("20261106_mtcurrent.xlsx"):
            resp.content = bad_latest_week_bytes
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    def _stub_reference() -> None:
        _write_reference_fixture(cli, data_dir)

    monkeypatch.setattr(cli, "get", create_autospec(orig_get, side_effect=_fake_get))
    monkeypatch.setattr(cli, "sync_kioxia_weekly", create_autospec(orig_sync_kioxia_weekly, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_daily", create_autospec(orig_sync_jsf_daily, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_issue_history", create_autospec(orig_sync_jsf_issue_history, return_value=None))
    monkeypatch.setattr(cli, "sync_investor_flows", create_autospec(orig_sync_investor_flows, return_value=None))
    monkeypatch.setattr(cli, "sync_short_selling", create_autospec(orig_sync_short_selling, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_units", create_autospec(orig_sync_etf_units, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_prices", create_autospec(orig_sync_etf_prices, return_value=None))
    monkeypatch.setattr(cli, "sync_reference", create_autospec(orig_sync_reference, side_effect=_stub_reference))
    monkeypatch.setattr(sys, "argv", [str(CLI_PATH), "--json", "run"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["update"]["ok"] is False
    assert "sync_reference" in payload["update"]["done"]
    margin_warnings = [w for w in payload["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(margin_warnings) == 1
    assert "20261106_mtcurrent.xlsx" in margin_warnings[0]["error"]
    assert "Unrecognized" in margin_warnings[0]["error"]

    # Valid rows from history and the older weekly workbook must still be saved.
    stored_margin = cli.read_rows(data_dir / "margin_weekly.csv")
    assert {"2026-09-25", "2026-10-30"} <= set(stored_margin)
    assert float(stored_margin["2026-10-30"]["short_value_mjpy"]) == 643859.0
    assert (data_dir / "market_reference.csv").is_file()


def test_latest_kioxia_pdf_failure_with_older_pdf_success_saves_valid_rows_and_exposes_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()
    out_root = tmp_path / "jp-leverage"
    data_dir = out_root / "data"
    html_path = out_root / "index.html"
    monkeypatch.setattr(cli, "OUT_ROOT", out_root)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(cli, "HTML_PATH", html_path)

    _seed_dashboard_inputs(cli, data_dir, include_margin=True)

    stocks_html = """
    <table>
      <tr><th>2026年11月6日申込分</th><td><a href="tvdivq0000001rnl-att/20261106_mtall.pdf">PDF</a></td></tr>
      <tr><th>2026年11月5日申込分</th><td><a href="tvdivq0000001rnl-att/20261105_mtall.pdf">PDF</a></td></tr>
    </table>
    """
    older_pdf_text = (
        "2026/11/5 申込み現在  （単位：一株、一円） 2026/11/6\n"
        "B キオクシアホールディングス 普通株式 プライム 貸 285A0 JP3236330001 株数 Shs. "
        "2,078,700 ▲ 15,200 * 52,850,100 ▲ 1,100,000 - 600,000 ▲ 5,000 1,478,700 ▲ 10,200 19,000,000 0 33,850,100 ▲ 1,100,000\n"
    )
    latest_pdf_no_kioxia_text = "2026/11/6 申込み現在  （単位：一株、一円） 2026/11/9\nother stock rows only\n"

    orig_get = cli.get
    orig_pdf_text = cli.pdf_text
    orig_sync_margin_weekly = cli.sync_margin_weekly
    orig_sync_jsf_daily = cli.sync_jsf_daily
    orig_sync_jsf_issue_history = cli.sync_jsf_issue_history
    orig_sync_investor_flows = cli.sync_investor_flows
    orig_sync_short_selling = cli.sync_short_selling
    orig_sync_etf_units = cli.sync_etf_units
    orig_sync_etf_prices = cli.sync_etf_prices
    orig_sync_reference = cli.sync_reference

    def _fake_get(url: str, **kw) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        if url == cli.STOCK_MARGIN_URL:
            resp.text = stocks_html
            resp.content = stocks_html.encode("utf-8")
            return resp
        if url.endswith("20261106_mtall.pdf"):
            resp.content = b"%PDF-20261106-missing-kioxia"
            resp.text = ""
            return resp
        if url.endswith("20261105_mtall.pdf"):
            resp.content = b"%PDF-20261105-valid"
            resp.text = ""
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    def _fake_pdf_text(content: bytes) -> str:
        if content == b"%PDF-20261106-missing-kioxia":
            return latest_pdf_no_kioxia_text
        if content == b"%PDF-20261105-valid":
            return older_pdf_text
        raise AssertionError(f"Unexpected PDF content: {content!r}")

    def _stub_reference() -> None:
        _write_reference_fixture(cli, data_dir)

    monkeypatch.setattr(cli, "get", create_autospec(orig_get, side_effect=_fake_get))
    monkeypatch.setattr(cli, "pdf_text", create_autospec(orig_pdf_text, side_effect=_fake_pdf_text))
    monkeypatch.setattr(cli, "sync_margin_weekly", create_autospec(orig_sync_margin_weekly, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_daily", create_autospec(orig_sync_jsf_daily, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_issue_history", create_autospec(orig_sync_jsf_issue_history, return_value=None))
    monkeypatch.setattr(cli, "sync_investor_flows", create_autospec(orig_sync_investor_flows, return_value=None))
    monkeypatch.setattr(cli, "sync_short_selling", create_autospec(orig_sync_short_selling, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_units", create_autospec(orig_sync_etf_units, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_prices", create_autospec(orig_sync_etf_prices, return_value=None))
    monkeypatch.setattr(cli, "sync_reference", create_autospec(orig_sync_reference, side_effect=_stub_reference))
    monkeypatch.setattr(sys, "argv", [str(CLI_PATH), "--json", "run"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1

    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["update"]["ok"] is False
    assert "sync_reference" in payload["update"]["done"]
    kioxia_warnings = [w for w in payload["update"]["warnings"] if w["step"] == "sync_kioxia_weekly"]
    assert len(kioxia_warnings) == 1
    assert "20261106_mtall.pdf" in kioxia_warnings[0]["error"]
    assert "Kioxia share row not found" in kioxia_warnings[0]["error"]

    # Valid row from the older PDF must be saved before reporting step failure.
    stored_kioxia = cli.read_rows(data_dir / "kioxia_margin_weekly.csv")
    assert set(stored_kioxia) == {"2026-11-05"}
    assert int(stored_kioxia["2026-11-05"]["short_shares"]) == 2078700
    assert int(stored_kioxia["2026-11-05"]["short_change"]) == -15200
    assert int(stored_kioxia["2026-11-05"]["long_shares"]) == 52850100
    assert int(stored_kioxia["2026-11-05"]["long_change"]) == -1100000
    assert (data_dir / "market_reference.csv").is_file()


def test_sync_margin_weekly_combines_legacy_and_current_workbooks_and_fails_on_empty_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _load_cli_module()
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)

    combined_bytes = _rows_to_xlsx_bytes("信用取引現在高", _build_combined_history_rows())
    mtgk_bytes = _rows_to_xlsx_bytes("Sheet1", _build_legacy_mtgk_rows())
    mtsk_bytes = _rows_to_xlsx_bytes("Sheet1", _build_legacy_mtsk_rows())
    current_bytes = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date=dt.datetime(2026, 11, 6),
            pub_date=dt.datetime(2026, 11, 10),
        ),
    )

    history_html = """
    <table>
      <tr><th>信用取引現在高</th><td><a href="tvdivq0000001rq1-att/20260925_mtsk.xls">Excel</a></td></tr>
      <tr><th>信用取引現在高（一般信用取引・制度信用取引別）</th><td><a href="tvdivq0000001rq1-att/20260925_mtgk.xls">Excel</a></td></tr>
      <tr><th>信用取引現在高</th><td><a href="tvdivq0000001rq1-att/tvdivq0000015969.xlsx">Excel</a></td></tr>
    </table>
    """
    current_html = """
    <table>
      <tr><th>2026年11月6日申込現在</th><td><a href="tvdivq0000001rk9-att/20261106_mtcurrent.xlsx">Excel</a></td></tr>
    </table>
    """

    orig_get = cli.get

    def _fake_get(url: str, **kw) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        if url == cli.MARGIN_HISTORY_URL:
            resp.text = history_html
            resp.content = history_html.encode("utf-8")
            return resp
        if url == cli.MARGIN_CURRENT_URL:
            resp.text = current_html
            resp.content = current_html.encode("utf-8")
            return resp
        if url.endswith("tvdivq0000015969.xlsx"):
            resp.content = combined_bytes
            return resp
        if url.endswith("20260925_mtgk.xls"):
            resp.content = mtgk_bytes
            return resp
        if url.endswith("20260925_mtsk.xls"):
            resp.content = mtsk_bytes
            return resp
        if url.endswith("20261106_mtcurrent.xlsx"):
            resp.content = current_bytes
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(cli, "get", create_autospec(orig_get, side_effect=_fake_get))
    cli.sync_margin_weekly()

    stored = cli.read_rows(tmp_path / "margin_weekly.csv")
    assert set(stored) == {"2026-09-18", "2026-09-25", "2026-10-30", "2026-11-06"}
    assert float(stored["2026-09-18"]["short_shares_k"]) == 510000.0
    assert float(stored["2026-09-18"]["jsf_stock_loan_value_mjpy"]) == 205000.0
    assert float(stored["2026-10-30"]["long_value_mjpy"]) == 6505887.0
    assert float(stored["2026-11-06"]["short_value_mjpy"]) == 643859.0
    assert float(stored["2026-11-06"]["jsf_funding_value_mjpy"]) == 385576.0

    # If all current weekly workbooks fail schema validation, sync_margin_weekly must raise RuntimeError.
    bad_current_bytes = _rows_to_xlsx_bytes(
        "Unrelated",
        [["A", "B"], [1, 2]],
    )
    current_bytes = bad_current_bytes
    with pytest.raises(RuntimeError, match="JPX current weekly margin workbooks yielded no rows"):
        cli.sync_margin_weekly()


def test_history_discovery_or_attachments_failure_with_valid_current_saves_current_and_exposes_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()
    out_root = tmp_path / "jp-leverage"
    data_dir = out_root / "data"
    html_path = out_root / "index.html"
    monkeypatch.setattr(cli, "OUT_ROOT", out_root)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(cli, "HTML_PATH", html_path)

    _seed_dashboard_inputs(cli, data_dir, include_margin=True)
    # Pre-seed an older historical row with an explicit finite zero to verify both are preserved.
    cli.merge_margin_rows(
        data_dir / "margin_weekly.csv",
        [
            {
                "date": "2026-09-11",
                "short_shares_k": 500000.0,
                "short_value_mjpy": 950000.0,
                "long_shares_k": 3300000.0,
                "long_value_mjpy": 6000000.0,
                "neg_short_value_mjpy": 0.0,
                "std_short_value_mjpy": 950000.0,
                "neg_long_value_mjpy": 2700000.0,
                "std_long_value_mjpy": 3300000.0,
                "jsf_stock_loan_value_mjpy": 0.0,
                "jsf_funding_value_mjpy": 410000.0,
            }
        ],
    )

    valid_current_bytes = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date="2026/11/6 申込現在",
            pub_date="2026/11/10 公表",
            extra_cols=4,
        ),
    )
    current_html = """
    <table>
      <tr><th>2026年11月6日申込現在</th><td><a href="tvdivq0000001rk9-att/20261106_mtcurrent.xlsx">Excel</a></td></tr>
    </table>
    """

    orig_get = cli.get
    orig_sync_kioxia_weekly = cli.sync_kioxia_weekly
    orig_sync_jsf_daily = cli.sync_jsf_daily
    orig_sync_jsf_issue_history = cli.sync_jsf_issue_history
    orig_sync_investor_flows = cli.sync_investor_flows
    orig_sync_short_selling = cli.sync_short_selling
    orig_sync_etf_units = cli.sync_etf_units
    orig_sync_etf_prices = cli.sync_etf_prices
    orig_sync_reference = cli.sync_reference

    # Case 1: History discovery HTTP failure while current weekly workbook is valid.
    mode = "hist_discovery_http_error"
    history_html_with_links = """
    <table>
      <tr><th>信用取引現在高</th><td><a href="tvdivq0000001rq1-att/tvdivq0000015969.xlsx">Excel</a></td></tr>
    </table>
    """
    bad_hist_bytes = _rows_to_xlsx_bytes("Unrelated", [["X", "Y"], [1, 2]])

    def _fake_get(url: str, **kw) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        if url == cli.MARGIN_HISTORY_URL:
            if mode == "hist_discovery_http_error":
                raise requests.ConnectionError("history page unreachable")
            if mode == "hist_discovery_zero_links":
                resp.text = "<table><tr><th>No links</th></tr></table>"
                resp.content = resp.text.encode("utf-8")
                return resp
            resp.text = history_html_with_links
            resp.content = history_html_with_links.encode("utf-8")
            return resp
        if url == cli.MARGIN_CURRENT_URL:
            resp.text = current_html
            resp.content = current_html.encode("utf-8")
            return resp
        if url.endswith("tvdivq0000015969.xlsx"):
            resp.content = bad_hist_bytes
            return resp
        if url.endswith("20261106_mtcurrent.xlsx"):
            resp.content = valid_current_bytes
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    def _stub_reference() -> None:
        _write_reference_fixture(cli, data_dir)

    monkeypatch.setattr(cli, "get", create_autospec(orig_get, side_effect=_fake_get))
    monkeypatch.setattr(cli, "sync_kioxia_weekly", create_autospec(orig_sync_kioxia_weekly, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_daily", create_autospec(orig_sync_jsf_daily, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_issue_history", create_autospec(orig_sync_jsf_issue_history, return_value=None))
    monkeypatch.setattr(cli, "sync_investor_flows", create_autospec(orig_sync_investor_flows, return_value=None))
    monkeypatch.setattr(cli, "sync_short_selling", create_autospec(orig_sync_short_selling, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_units", create_autospec(orig_sync_etf_units, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_prices", create_autospec(orig_sync_etf_prices, return_value=None))
    monkeypatch.setattr(cli, "sync_reference", create_autospec(orig_sync_reference, side_effect=_stub_reference))
    monkeypatch.setattr(sys, "argv", [str(CLI_PATH), "--json", "run"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code == 1

    out1 = capsys.readouterr().out.strip()
    payload1 = json.loads(out1)
    assert payload1["ok"] is False
    assert payload1["update"]["ok"] is False
    assert "sync_reference" in payload1["update"]["done"]
    w1 = [w for w in payload1["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(w1) == 1
    assert "JPX margin history workbooks not found" in w1[0]["error"]
    assert "history page unreachable" in w1[0]["error"]

    stored1 = cli.read_rows(data_dir / "margin_weekly.csv")
    assert "2026-11-06" in stored1
    assert float(stored1["2026-11-06"]["short_value_mjpy"]) == 643859.0
    assert float(stored1["2026-09-11"]["short_shares_k"]) == 500000.0
    assert float(stored1["2026-09-11"]["neg_short_value_mjpy"]) == 0.0
    assert (data_dir / "market_reference.csv").is_file()

    # Case 2: History discovery succeeds, all history attachments fail, while current weekly workbook is valid (2026-11-13).
    mode = "hist_attachments_all_fail"
    valid_current_bytes = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date="2026/11/13 申込現在",
            pub_date="2026/11/17 公表",
            extra_cols=4,
        ),
    )
    with pytest.raises(SystemExit) as exc_info2:
        cli.main()
    assert exc_info2.value.code == 1

    out2 = capsys.readouterr().out.strip()
    payload2 = json.loads(out2)
    assert payload2["ok"] is False
    assert payload2["update"]["ok"] is False
    w2 = [w for w in payload2["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(w2) == 1
    assert "JPX margin history workbooks yielded no rows" in w2[0]["error"]
    assert "tvdivq0000015969.xlsx" in w2[0]["error"]

    stored2 = cli.read_rows(data_dir / "margin_weekly.csv")
    assert {"2026-09-11", "2026-11-06", "2026-11-13"} <= set(stored2)
    assert float(stored2["2026-11-13"]["long_value_mjpy"]) == 6505887.0
    assert float(stored2["2026-09-11"]["neg_short_value_mjpy"]) == 0.0

    # Case 3: History page returns 0 workbook links while current weekly workbook is valid (2026-11-20).
    mode = "hist_discovery_zero_links"
    valid_current_bytes = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date="2026/11/20 申込現在",
            pub_date="2026/11/25 公表",
            extra_cols=4,
        ),
    )
    with pytest.raises(SystemExit) as exc_info3:
        cli.main()
    assert exc_info3.value.code == 1

    out3 = capsys.readouterr().out.strip()
    payload3 = json.loads(out3)
    assert payload3["ok"] is False
    w3 = [w for w in payload3["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(w3) == 1
    assert "JPX margin history workbooks not found" in w3[0]["error"]

    stored3 = cli.read_rows(data_dir / "margin_weekly.csv")
    assert {"2026-09-11", "2026-11-06", "2026-11-13", "2026-11-20"} <= set(stored3)
    assert float(stored3["2026-11-20"]["short_value_mjpy"]) == 643859.0
    assert float(stored3["2026-09-11"]["neg_short_value_mjpy"]) == 0.0


def test_current_discovery_failure_or_text_date_15col_mismatch_with_valid_history_saves_history_and_exposes_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _load_cli_module()
    out_root = tmp_path / "jp-leverage"
    data_dir = out_root / "data"
    html_path = out_root / "index.html"
    monkeypatch.setattr(cli, "OUT_ROOT", out_root)
    monkeypatch.setattr(cli, "DATA_DIR", data_dir)
    monkeypatch.setattr(cli, "HTML_PATH", html_path)

    _seed_dashboard_inputs(cli, data_dir, include_margin=False)
    # Pre-seed an older historical row with explicit zero and an existing 2026-11-06 row that must not be corrupted.
    cli.merge_margin_rows(
        data_dir / "margin_weekly.csv",
        [
            {
                "date": "2026-09-11",
                "short_shares_k": 500000.0,
                "short_value_mjpy": 950000.0,
                "long_shares_k": 3300000.0,
                "long_value_mjpy": 6000000.0,
                "neg_short_value_mjpy": 0.0,
                "std_short_value_mjpy": 950000.0,
                "neg_long_value_mjpy": 2700000.0,
                "std_long_value_mjpy": 3300000.0,
                "jsf_stock_loan_value_mjpy": 0.0,
                "jsf_funding_value_mjpy": 410000.0,
            },
            {
                "date": "2026-11-06",
                "short_shares_k": 349230.0,
                "short_value_mjpy": 643859.0,
                "long_shares_k": 3654854.0,
                "long_value_mjpy": 6505887.0,
                "neg_short_value_mjpy": 177627.0,
                "std_short_value_mjpy": 466231.0,
                "neg_long_value_mjpy": 2945730.0,
                "std_long_value_mjpy": 3560156.0,
                "jsf_stock_loan_value_mjpy": 195538.0,
                "jsf_funding_value_mjpy": 385576.0,
            },
        ],
    )

    combined_bytes = _rows_to_xlsx_bytes("信用取引現在高", _build_combined_history_rows())
    history_html = """
    <table>
      <tr><th>信用取引現在高</th><td><a href="tvdivq0000001rq1-att/tvdivq0000015969.xlsx">Excel</a></td></tr>
    </table>
    """
    current_html = """
    <table>
      <tr><th>2026年11月6日申込現在</th><td><a href="tvdivq0000001rk9-att/20261106_mtcurrent.xlsx">Excel</a></td></tr>
    </table>
    """
    # 16-column (>=15) current workbook with text date at B1 but Tokyo-only scope at E6.
    mismatched_16col_current_bytes = _rows_to_xlsx_bytes(
        "レイアウト",
        _build_current_week_rows(
            app_date="2026/11/6 申込現在",
            pub_date="2026/11/10 公表",
            scope_e6="東京 Tokyo",
            extra_cols=4,
        ),
    )

    orig_get = cli.get
    orig_sync_kioxia_weekly = cli.sync_kioxia_weekly
    orig_sync_jsf_daily = cli.sync_jsf_daily
    orig_sync_jsf_issue_history = cli.sync_jsf_issue_history
    orig_sync_investor_flows = cli.sync_investor_flows
    orig_sync_short_selling = cli.sync_short_selling
    orig_sync_etf_units = cli.sync_etf_units
    orig_sync_etf_prices = cli.sync_etf_prices
    orig_sync_reference = cli.sync_reference

    mode = "current_discovery_http_error"

    def _fake_get(url: str, **kw) -> requests.Response:
        resp = create_autospec(requests.Response, instance=True)
        if url == cli.MARGIN_HISTORY_URL:
            resp.text = history_html
            resp.content = history_html.encode("utf-8")
            return resp
        if url == cli.MARGIN_CURRENT_URL:
            if mode == "current_discovery_http_error":
                raise requests.ConnectionError("current weekly page unreachable")
            resp.text = current_html
            resp.content = current_html.encode("utf-8")
            return resp
        if url.endswith("tvdivq0000015969.xlsx"):
            resp.content = combined_bytes
            return resp
        if url.endswith("20261106_mtcurrent.xlsx"):
            resp.content = mismatched_16col_current_bytes
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    def _stub_reference() -> None:
        _write_reference_fixture(cli, data_dir)

    monkeypatch.setattr(cli, "get", create_autospec(orig_get, side_effect=_fake_get))
    monkeypatch.setattr(cli, "sync_kioxia_weekly", create_autospec(orig_sync_kioxia_weekly, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_daily", create_autospec(orig_sync_jsf_daily, return_value=None))
    monkeypatch.setattr(cli, "sync_jsf_issue_history", create_autospec(orig_sync_jsf_issue_history, return_value=None))
    monkeypatch.setattr(cli, "sync_investor_flows", create_autospec(orig_sync_investor_flows, return_value=None))
    monkeypatch.setattr(cli, "sync_short_selling", create_autospec(orig_sync_short_selling, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_units", create_autospec(orig_sync_etf_units, return_value=None))
    monkeypatch.setattr(cli, "sync_etf_prices", create_autospec(orig_sync_etf_prices, return_value=None))
    monkeypatch.setattr(cli, "sync_reference", create_autospec(orig_sync_reference, side_effect=_stub_reference))
    monkeypatch.setattr(sys, "argv", [str(CLI_PATH), "--json", "run"])

    # Case 1: Current discovery HTTP failure while history is valid -> history rows saved, step fails.
    with pytest.raises(SystemExit) as exc_info1:
        cli.main()
    assert exc_info1.value.code == 1

    out1 = capsys.readouterr().out.strip()
    payload1 = json.loads(out1)
    assert payload1["ok"] is False
    assert payload1["update"]["ok"] is False
    assert "sync_reference" in payload1["update"]["done"]
    w1 = [w for w in payload1["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(w1) == 1
    assert "JPX current weekly margin workbooks not found" in w1[0]["error"]
    assert "current weekly page unreachable" in w1[0]["error"]

    stored1 = cli.read_rows(data_dir / "margin_weekly.csv")
    assert {"2026-09-11", "2026-09-25", "2026-10-30", "2026-11-06"} <= set(stored1)
    assert float(stored1["2026-09-25"]["short_value_mjpy"]) == 1047238.0
    assert float(stored1["2026-10-30"]["long_value_mjpy"]) == 6505887.0
    assert float(stored1["2026-09-11"]["neg_short_value_mjpy"]) == 0.0
    assert (data_dir / "market_reference.csv").is_file()

    # Case 2: Current workbook has text date and 16 columns (>=15) with mismatched scope -> rejected,
    # existing 2026-11-06 row is not overwritten with row-7/8 numbers, and main exits non-zero.
    mode = "current_16col_text_date_mismatch"
    with pytest.raises(SystemExit) as exc_info2:
        cli.main()
    assert exc_info2.value.code == 1

    out2 = capsys.readouterr().out.strip()
    payload2 = json.loads(out2)
    assert payload2["ok"] is False
    assert payload2["update"]["ok"] is False
    w2 = [w for w in payload2["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(w2) == 1
    assert "JPX current weekly margin workbooks yielded no rows" in w2[0]["error"]
    assert "20261106_mtcurrent.xlsx" in w2[0]["error"]
    assert "Unrecognized" in w2[0]["error"]

    stored2 = cli.read_rows(data_dir / "margin_weekly.csv")
    # 2026-11-06 retains its valid pre-seeded values rather than row 8's 78597.0.
    assert float(stored2["2026-11-06"]["short_shares_k"]) == 349230.0
    assert float(stored2["2026-11-06"]["short_value_mjpy"]) == 643859.0
    assert float(stored2["2026-09-11"]["neg_short_value_mjpy"]) == 0.0

    # Case 3: Current workbook has text date and 16 columns (>=15) with mismatched balance header at E7.
    bad_header_16col_rows = _build_current_week_rows(
        app_date="2026/11/6 申込現在",
        pub_date="2026/11/10 公表",
        extra_cols=4,
    )
    bad_header_16col_rows[6][4] = "前週比\nWeekly change"
    mismatched_16col_current_bytes = _rows_to_xlsx_bytes("レイアウト", bad_header_16col_rows)
    with pytest.raises(SystemExit) as exc_info3:
        cli.main()
    assert exc_info3.value.code == 1

    out3 = capsys.readouterr().out.strip()
    payload3 = json.loads(out3)
    assert payload3["ok"] is False
    w3 = [w for w in payload3["update"]["warnings"] if w["step"] == "sync_margin_weekly"]
    assert len(w3) == 1
    assert "JPX current weekly margin workbooks yielded no rows" in w3[0]["error"]
    assert "20261106_mtcurrent.xlsx" in w3[0]["error"]
    assert "Unrecognized" in w3[0]["error"]

    stored3 = cli.read_rows(data_dir / "margin_weekly.csv")
    assert float(stored3["2026-11-06"]["short_shares_k"]) == 349230.0
    assert float(stored3["2026-11-06"]["short_value_mjpy"]) == 643859.0
    assert float(stored3["2026-09-11"]["neg_short_value_mjpy"]) == 0.0

