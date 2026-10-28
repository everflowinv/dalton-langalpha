"""验证项到期时刻的统一口径（agenda 探针、总监探针与看板共用）。

到期验证项保留原始时刻与时区；无时区的时刻按标的所属交易所的时区解释——
美股的美东日落后上海已是次日，按上海日期判到期会把当天才到期的验证项提前
一天多判成到期。只有日期时按该时区当地时间 18:00（A 股、港股、美股都在收盘
之后，也在常见的盘后电话会结束之后）才算到期：上海周四 07:30 的晨会就能看
到周三的美股验证项，而不是要等到当天 23:59:59 过完。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.dalton.coverage.models import infer_market

_MARKET_TIMEZONES = {
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
}

# company 文档 exchange 枚举（company.schema.json）里的非美股交易所 → 时区；
# NASDAQ/NYSE/OTHER 落回按 ticker 后缀推断（美股 → 美东）。
_EXCHANGE_TIMEZONES = {
    "HKEX": "Asia/Hong_Kong",
    "SSE": "Asia/Shanghai",
    "SZSE": "Asia/Shanghai",
}


def market_timezone(ticker: str) -> ZoneInfo:
    """到期时刻缺时区时的解释时区：按标的所属市场（US → 美东）。"""
    return ZoneInfo(_MARKET_TIMEZONES.get(infer_market(ticker), "America/New_York"))


def workspace_market_timezone(
    company_content: Any,
    thesis_content: Any,
    ws_name: str,
) -> tuple[str, ZoneInfo]:
    """工作区 → (标的, 交易所时区)，议程探针、总监探针与看板共用的同一来源
    顺序：company 文档的 ticker（有 exchange 字段时用它定交易所）→ 论点文档
    的 ticker → 工作区名。工作区名常是中文公司名（如"腾讯"），拿它推断会把
    港股 / A 股当美股（美东），比正确答案晚约 12 小时。"""
    company = company_content if isinstance(company_content, dict) else {}
    thesis = thesis_content if isinstance(thesis_content, dict) else {}
    ticker = str(
        company.get("ticker") or thesis.get("ticker") or ws_name or ""
    ).strip().upper()
    exchange = str(company.get("exchange") or "").strip().upper()
    if exchange in _EXCHANGE_TIMEZONES:
        return ticker, ZoneInfo(_EXCHANGE_TIMEZONES[exchange])
    return ticker, market_timezone(ticker)


def parse_due_moment(val: Any, default_tz: ZoneInfo) -> Optional[datetime]:
    """到期验证项的原始时刻：带时区偏移的原样保留；只有日期或无时区的时刻按
    交易所时区解释，日期按该时区当地时间 18:00 算（收盘之后才算到期）。"""
    if not val:
        return None
    s = str(val).strip().replace("Z", "+00:00")
    date_only = len(s) == 10
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        d = _parse_date(s)
        if d is None:
            return None
        return datetime(d.year, d.month, d.day, 18, 0, 0, tzinfo=default_tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    if date_only:
        dt = dt.replace(hour=18, minute=0, second=0, microsecond=0)
    return dt


def validation_is_due(
    raw_due: Any,
    ticker: str,
    now_dt: datetime,
    *,
    tz: Optional[ZoneInfo] = None,
) -> bool:
    """验证项是否已过它的到期时刻（无 due 或解析不了返回 False）。`tz` 显式
    给出时优先（看板 / 总监已从 company.exchange 解析出的交易所时区）。"""
    moment = parse_due_moment(raw_due, tz or market_timezone(ticker))
    return moment is not None and now_dt >= moment


# ---------------------------------------------------------------------------
# 写入侧校验：jsonschema 未注册 rfc3339-validator，`format: date-time` 的检查
# 不生效、FormatChecker 直接放行，schema 单靠 format 挡不住 "下周"、"16:30"
# 这类字符串——它们进库后解析失败，验证项永不到期。写入前在这里真实解析。
# ---------------------------------------------------------------------------

_WRITABLE_DUE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WRITABLE_DUE_MOMENT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-](?:0\d|1[0-4]):[0-5]\d)$"
)
_WRITABLE_DUE_HINT = (
    "due 只接受两种写法：日期 YYYY-MM-DD，或带时区偏移的完整时刻 "
    "YYYY-MM-DDTHH:MM[:SS][.fff](Z|±HH:MM)，如 2026-10-28 或 2026-10-28T16:30:00-04:00"
)


def writable_due_error(val: Any) -> Optional[str]:
    """写入侧校验 `next_validations[].due`：返回 None 表示合法，否则返回给
    调用方的错误说明（已含允许的两种写法）。"""
    if val is None:
        return None
    s = str(val)
    if _WRITABLE_DUE_DATE_RE.match(s):
        try:
            date.fromisoformat(s)
            return None
        except ValueError:
            return f"due={s!r} 不是真实日期（如 2026-02-30）。{_WRITABLE_DUE_HINT}"
    if not _WRITABLE_DUE_MOMENT_RE.match(s):
        return _WRITABLE_DUE_HINT
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return f"due={s!r} 不是真实时刻（小时须 ≤23 等）。{_WRITABLE_DUE_HINT}"
    if dt.tzinfo is None:
        return f"时刻必须带时区偏移（如 -04:00 或 Z）。{_WRITABLE_DUE_HINT}"
    return None


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    s = str(val).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None
