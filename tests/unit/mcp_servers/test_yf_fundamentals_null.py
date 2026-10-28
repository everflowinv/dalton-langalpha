"""yfinance fundamentals 报表的缺失值输出契约（batch 5 / F 线项 4）。

报表里的缺失值（NaN/None）序列化为 JSON null（None），不是字符串 "N/A"，也
不是 0：调用方对取到的数值做算术（b / 1e6 等）时，null 要么被显式跳过，要么
抛出可见的 TypeError，而不是把 "N/A" 传进算式后炸在周报任务里。
"""

from __future__ import annotations

import pandas as pd

import plugins.yfinance.yf_fundamentals_mcp_server as fundamentals_server


def _statement_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "2026-09-30": [512.0, float("nan")],
            "2026-06-30": [None, 480.0],
        },
        index=["Total Revenue", "Diluted EPS"],
    )


def test_missing_statement_values_serialize_as_null() -> None:
    out = fundamentals_server._serialize_dataframe(_statement_frame())
    assert out["Total Revenue"]["2026-09-30"] == 512.0
    assert out["Diluted EPS"]["2026-06-30"] == 480.0
    assert out["Diluted EPS"]["2026-09-30"] is None
    assert out["Total Revenue"]["2026-06-30"] is None


def test_no_placeholder_string_or_zero_in_serialized_values() -> None:
    out = fundamentals_server._serialize_dataframe(_statement_frame())
    values = [v for row in out.values() for v in row.values()]
    assert "N/A" not in values
    assert 0 not in values
    assert all(v is None or isinstance(v, (int, float)) for v in values)


def test_agent_arithmetic_on_a_missing_value_fails_visibly_not_silently() -> None:
    """锁住契约的动机：缺失值以 None 到达调用方，除法抛 TypeError（可见、可
    跳过），而不是 "N/A" 字符串混进算式后的隐蔽崩溃。"""
    out = fundamentals_server._serialize_dataframe(_statement_frame())
    missing = out["Diluted EPS"]["2026-09-30"]
    assert missing is None
    try:
        _ = missing / 1e6  # type: ignore[operator]
    except TypeError:
        pass
    else:  # pragma: no cover - the guard this test pins
        raise AssertionError("missing value must be None, not a numeric placeholder")
