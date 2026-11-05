#!/usr/bin/env python3
"""cn_price_fallback.py — 港股/A 股日线坏数据识别与 cn-hk 备用行情源。

线上事故（线程 63711ab2、fd9e3baa）：yfinance 的 2525.HK 日线从 10-26 起一直
停在 HK$15.47、成交量为 0，而腾讯备用行情（researchdata 桥的 cn-hk 路由）是
对的；分析师每次都要手动倒推。本模块把"识别 + 标注"固化下来，原则是
不破坏历史：

  1. `find_stagnant_tail` / `detect_bad_daily_series` 判定两类形态，一律以
     请求截止日为基准（不拿今天比；查询历史窗口时不做任何替换）：
     - 停滞：尾部 >= 3 个交易日收盘价完全不变且成交量全为 0；
     - 落后：最新日线落后请求截止日 >= 4 个工作日时段。
  2. `evaluate_cn_daily` 汇总判定并给出非破坏性处置：永远不删、不合成
     历史日线；停滞尾部只标注（"行情源 X 日起停在 Y、成交为 0"），基于
     历史的指标只用停滞段之前的有效数据（valid_until，输出注明"截至某日"）。
     只有请求截止到今天、且日线确实停滞或落后（备用报价日期确实更新）时，
     才用 cn-hk 报价单独计算"今天的涨跌"——必须同时有现价和前收，缺一项
     标"无法计算"、不以现价充当前收；结果作为单独字段输出，注明来源与
     切换原因，不写回日线。
  3. 停牌与假期的甄别：备用报价也无成交且价格不变判停牌（照实标注、不
     切换）；"落后"必须由备用报价的日期确实比最后一根日线新来证实，否则
     按连续假期/停市处理，不判坏数据。

美股不受影响：所有入口都先判断 `is_cn_symbol`，非港股/A 股代码原样返回。
标准库实现，不依赖 yfinance/pandas；重依赖一律函数内延迟导入。
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# 港股与 A 股的 yfinance 后缀（move_quant 的 NON_US_EXTENDED_UNSUPPORTED_SUFFIXES
# 覆盖更广的市场，但备用源只服务港股与 A 股，因此这里只认这几类）。
CN_SUFFIXES = (".HK", ".SH", ".SS", ".SZ")

FLAT_MIN_BARS = 3     # 停滞判定：最近 >= 3 个交易日收盘价不变且成交量为 0
LAG_MIN_SESSIONS = 4  # 落后判定：最新日线落后请求截止日 >= 4 个工作日时段（容忍单日假期）

_HK_TZ = ZoneInfo("Asia/Hong_Kong")

# cn-hk quote 的返回是上游网关定的，键名没有锁；按常见拼写逐一识别。
_PRICE_KEYS = ("price", "current_price", "current", "last", "close", "现价", "最新价", "收盘")
_PREV_KEYS = ("prev_close", "previous_close", "pre_close", "prev_price", "昨收", "前收")
_OPEN_KEYS = ("open", "今开", "开盘")
_VOL_KEYS = ("volume", "vol", "成交量")
_SRC_KEYS = ("source", "data_source", "provider", "来源")
_DATE_KEYS = ("time", "datetime", "timestamp", "date", "时间", "行情时间")


def is_cn_symbol(symbol: str) -> bool:
    """是否港股/A 股代码（.HK/.SS/.SZ/.SH 后缀，或 5 位港股、6 位 A 股裸数字）。"""
    s = (symbol or "").strip().upper()
    if s.endswith(CN_SUFFIXES):
        return True
    return s.isdigit() and len(s) in (5, 6)


def to_cn_hk_ticker(symbol: str) -> str:
    """转成 cn-hk 路由要的代码：'2525.HK'→'02525'，'600519.SS'→'600519'。"""
    s = (symbol or "").strip().upper()
    if "." in s:
        code = s.split(".", 1)[0]
    else:
        code = s
    if code.isdigit() and len(code) == 4:
        return code.zfill(5)  # '2525' → '02525'（'0700.HK' 已带 0 则不变）
    return code


def hk_today(now: datetime | None = None) -> date:
    """当前交易日按亚洲/香港时区取日期（港 A 股收盘基准时区）。"""
    return (now or datetime.now(_HK_TZ)).astimezone(_HK_TZ).date()


def _is_weekday(d: date) -> bool:
    return d.weekday() < 5


def latest_weekday_session(d: date) -> date:
    """d 当天或之前最近的一个工作日（周一至周五；港 A 假期不在此分辨，
    由 LAG_MIN_SESSIONS 的容忍度吸收单日假期）。"""
    cur = d
    for _ in range(7):
        if _is_weekday(cur):
            return cur
        cur -= timedelta(days=1)
    return cur


def _missing_weekday_sessions(last_bar: date, now: date | None) -> int:
    """last_bar 之后、请求截止日之前，隔了多少个工作日时段。"""
    expected = latest_weekday_session(hk_today() if now is None else now)
    if last_bar >= expected:
        return 0
    count = 0
    cur = last_bar + timedelta(days=1)
    while cur <= expected:
        if _is_weekday(cur):
            count += 1
        cur += timedelta(days=1)
    return count


def _as_float(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _parse_bar_date(raw: Any) -> date | None:
    text = str(raw or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _rows_up_to(
    dates: list[Any], closes: list[Any], volumes: list[Any], anchor: date
) -> tuple[list[str], list[Any], list[Any]]:
    """请求截止日（含）之前的 (日期文本, 收盘, 量)：截止日之后的行情不属于
    本次请求，判定一律不拿截止日之后的数据、也不与今天比。"""
    f_dates: list[str] = []
    f_closes: list[Any] = []
    f_vols: list[Any] = []
    for i, d in enumerate(dates):
        parsed = _parse_bar_date(d)
        if parsed is None or parsed > anchor:
            continue
        f_dates.append(parsed.isoformat())
        f_closes.append(closes[i] if i < len(closes) else None)
        f_vols.append(volumes[i] if i < len(volumes) else None)
    return f_dates, f_closes, f_vols


def find_stagnant_tail(
    dates: list[Any], closes: list[Any], volumes: list[Any]
) -> dict[str, Any] | None:
    """尾部"收盘不变 + 量为 0"连续段（>= FLAT_MIN_BARS 根）。

    这是数据形态本身的事实，不与任何外部日期比较：既是 yfinance 坏数据的
    签名，也是停牌的签名，两者靠备用报价区分（见 `evaluate_cn_daily`）。
    返回 {"bars", "from_index", "from_date", "close"}；没有则 None。
    """
    if not dates or not closes:
        return None
    flat_closes = [_as_float(c) for c in closes]
    flat_vols = [
        (0.0 if _as_float(v) is None else _as_float(v)) for v in volumes
    ] if volumes else [0.0] * len(closes)

    last = flat_closes[-1]
    if last is None:
        return None
    run = 0
    for c, v in zip(reversed(flat_closes), reversed(flat_vols)):
        if c is None or abs(c - last) > 1e-9 or v != 0:
            break
        run += 1
    if run < FLAT_MIN_BARS:
        return None
    from_index = len(dates) - run
    return {
        "bars": run,
        "from_index": from_index,
        "from_date": str(dates[from_index])[:10],
        "close": last,
    }


# ── cn-hk 备用行情源 ─────────────────────────────────────────────────────────


def build_cn_hk_quote_request(ticker: str) -> tuple[str, dict[str, Any]]:
    """构造 cn-hk quote 的 (tool_name, arguments)，与桥的 ROUTE_CONTRACT_SPECS 对齐。"""
    return (
        "finance",
        {"route": "cn-hk", "operation": "quote", "ticker": ticker},
    )


def parse_cn_hk_quote(payload: Any) -> dict[str, Any] | None:
    """从 cn-hk quote 的返回里提取 {price, prev_close, open, volume, source, as_of}。

    返回结构是上游网关定的（rows|data，键名未锁），所以在候选字典里按常见
    键名逐个识别；连现价都认不出来视为不可用（返回 None），调用方如实注明。
    """
    candidates: list[Any] = []
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        rows = data.get("rows") if isinstance(data, dict) else None
        if isinstance(rows, list):
            candidates.extend(r for r in rows if isinstance(r, dict))
        if isinstance(data, dict):
            candidates.append(data)
    elif isinstance(payload, list):
        candidates.extend(r for r in payload if isinstance(r, dict))

    for cand in candidates:
        price = next((_as_float(cand[k]) for k in _PRICE_KEYS if _as_float(cand.get(k)) is not None), None)
        if price is None:
            continue
        prev = next((_as_float(cand[k]) for k in _PREV_KEYS if _as_float(cand.get(k)) is not None), None)
        openp = next((_as_float(cand[k]) for k in _OPEN_KEYS if _as_float(cand.get(k)) is not None), None)
        vol = next((_as_float(cand[k]) for k in _VOL_KEYS if _as_float(cand.get(k)) is not None), None)
        source = next((str(cand[k]) for k in _SRC_KEYS if cand.get(k)), "cn-hk")
        as_of = next((str(cand[k]) for k in _DATE_KEYS if cand.get(k)), None)
        return {
            "price": price,
            "prev_close": prev,
            "open": openp,
            "volume": vol,
            "source": source,
            "as_of": as_of,
        }
    return None


def _http_bridge_call(bridge_url: str, tool: str, arguments: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        bridge_url,
        data=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    result = (raw or {}).get("result") or {}
    content = result.get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("text"):
        return json.loads(content[0]["text"])
    return result.get("structuredContent")


def _wrapper_call(tool: str, arguments: dict[str, Any]) -> Any:
    from tools import researchdata as wrapper  # type: ignore

    import mcp_client as mcp_client_module  # type: ignore

    flagged = getattr(mcp_client_module, "call_mcp_tool_with_error_flag", None)
    if callable(flagged):
        result = flagged("researchdata", tool, arguments)
        if isinstance(result, tuple) and len(result) == 2 and result[0]:
            return None
        return result[1] if isinstance(result, tuple) else result
    call_fn = getattr(wrapper, "_call_mcp_tool", None)
    if not callable(call_fn):
        return None
    return call_fn(tool, arguments)


def call_cn_hk_quote(ticker: str) -> dict[str, Any] | None:
    """调用 cn-hk quote（现有备用源），返回 `parse_cn_hk_quote` 的规格或 None。

    arguments 之外的失败都吞掉并返回 None——备用源不可用时调用方保留原数据
    并如实注明，不让脚本崩溃。
    """
    tool_name, arguments = build_cn_hk_quote_request(ticker)
    try:
        bridge_url = os.environ.get("DALTON_RESEARCHDATA_URL", "").strip()
        if bridge_url:
            payload = _http_bridge_call(bridge_url, tool_name, arguments)
        else:
            payload = _wrapper_call(tool_name, arguments)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return None
    return parse_cn_hk_quote(payload.get("data") if isinstance(payload.get("data"), (dict, list)) else payload)


def classify_quote_vs_daily(
    quote: dict[str, Any],
    last_bar: date | None,
    last_close: float | None,
    *,
    stagnant: dict[str, Any] | None = None,
    expected_session: date | None = None,
) -> str:
    """备用报价与最后一根日线的关系，用于甄别停牌与假期：

    - "suspended"：必须同时满足三条——(1) 报价日期能确认是请求截止日所在的
      交易日（expected_session）；(2) 日线尾部至少 3 根零成交平盘（stagnant）；
      (3) 报价确认无成交（成交量为 0，缺失不算确认无成交）且价格不变（对前收；
      缺前收时对最后一根日线收盘）。任一条不满足就不判停牌（避免把长假或开盘前
      的无量平盘报价、或成交量缺失的不完整报价误判成停牌）；
    - "newer"：报价确实有交易更新且日期比最后一根日线新——市场已交易，日线落后属实；
    - "stale"：其余情况（报价不比日线新，或长假后开盘前尚未成交），不判落后。
    """
    vol = _as_float(quote.get("volume"))
    price = _as_float(quote.get("price"))
    prev = _as_float(quote.get("prev_close"))
    anchor = prev if prev is not None else last_close
    as_of = _parse_bar_date(quote.get("as_of"))
    is_no_trade_flat = (
        price is not None
        and vol is not None
        and vol == 0.0
        and anchor is not None
        and abs(price - anchor) <= 1e-9
    )
    if is_no_trade_flat:
        if (
            stagnant is not None
            and expected_session is not None
            and as_of is not None
            and as_of == expected_session
        ):
            return "suspended"
        return "stale"
    if as_of is not None and last_bar is not None and as_of > last_bar:
        return "newer"
    return "stale"


def _shape_reason(stagnant: dict[str, Any] | None, lag: int) -> str:
    bits: list[str] = []
    if stagnant is not None:
        bits.append(
            f"行情源 {stagnant['from_date']} 起停在 {stagnant['close']:g}、成交为 0"
        )
    if lag >= LAG_MIN_SESSIONS:
        bits.append(f"最新日线落后请求截止日约 {lag} 个交易日")
    return "；".join(bits)


def evaluate_cn_daily(
    symbol: str,
    dates: list[Any],
    closes: list[Any],
    volumes: list[Any],
    *,
    end: date | None = None,
    fetch_quote: Any = None,
) -> dict[str, Any] | None:
    """判定港股/A 股日线相对请求截止日 end 的数据质量，产出非破坏性处置。

    返回 None（正常）或：
      kind: "stagnant"（尾部停滞）/"behind"（落后，备用报价日期证实）/
            "suspended"（疑似停牌）/"stagnant+behind"/"unconfirmed"（备用源
            不可用、无法确认是否落后，日线照常使用）
      stagnant_from: 停滞/停牌段首日（iso 文本，无则为 None）
      valid_until: 最后一个有效交易日（iso 文本；无停滞 = 最后一根日线；
            整段全停为 None——没有可用的历史）
      note: 写进输出注明的完整说明（来源、原因、截至某日）
      price_source: 价格来源标签（None = 不切换、无备用源参与）
      today: 仅当请求截止到今天且报价日期确认为当日交易日时的"今日涨跌"字段
            {price, prev_close, change_pct, source, as_of}；日期缺失或早于今天
            标 {"error": "无法确认当日行情…"}；缺前收/现价标
            {"error": "无法计算今日涨跌（…）"}，绝不以现价充当前收

    永远不删、不合成历史日线：本函数只判定与标注；调用方基于 valid_until
    决定"用哪些数据算"，今日涨跌作为单独字段输出，不写回日线。
    """
    today_dt = hk_today()
    anchor = today_dt if end is None else end
    expected_session = latest_weekday_session(anchor)
    f_dates, f_closes, f_vols = _rows_up_to(dates, closes, volumes, anchor)
    if not f_dates:
        return None
    stagnant = find_stagnant_tail(f_dates, f_closes, f_vols)
    last_bar = date.fromisoformat(f_dates[-1])
    lag = _missing_weekday_sessions(last_bar, anchor)
    if stagnant is None and lag < LAG_MIN_SESSIONS:
        return None

    last_close = _as_float(f_closes[-1])
    stagnant_from = stagnant["from_date"] if stagnant else None
    if stagnant:
        valid_idx = len(f_dates) - stagnant["bars"] - 1
        valid_until = f_dates[valid_idx] if valid_idx >= 0 else None
    else:
        valid_until = f_dates[-1]

    base = {
        "symbol": symbol,
        "stagnant_from": stagnant_from,
        "valid_until": valid_until,
    }
    reason = _shape_reason(stagnant, lag)

    # 历史窗口查询（截止日早于今天）：不调备用源、不做任何替换，停滞形态
    # 只标注；纯落后交给调用方既有的"数据未就绪"路径，不判坏数据。
    if anchor < today_dt:
        if stagnant is None:
            return None
        return {
            **base,
            "kind": "stagnant",
            "note": (
                f"{symbol}: yfinance 日线异常（{reason}），历史指标截至 "
                f"{valid_until or stagnant_from}；历史窗口查询只标注、不替换"
            ),
            "price_source": None,
            "today": None,
        }

    fetch = fetch_quote if fetch_quote is not None else call_cn_hk_quote
    try:
        quote = fetch(to_cn_hk_ticker(symbol))
    except Exception:
        quote = None
    if quote is None or _as_float(quote.get("price")) is None:
        if stagnant is None:
            return {
                **base,
                "kind": "unconfirmed",
                "note": (
                    f"{symbol}: 最新日线截至 {last_bar.isoformat()}，cn-hk 备用源本次不可用，"
                    "无法确认是否有更新交易日，日线照常使用"
                ),
                "price_source": None,
                "today": None,
            }
        stagnant_reason = _shape_reason(stagnant, 0)
        return {
            **base,
            "kind": "stagnant",
            "note": (
                f"{symbol}: yfinance 日线异常（{stagnant_reason}），cn-hk 备用源本次不可用，"
                "该标的行情不可信，结论需人工核对"
            ),
            "price_source": "yfinance(异常,备用源不可用)",
            "today": None,
        }

    rel = classify_quote_vs_daily(
        quote,
        last_bar,
        last_close,
        stagnant=stagnant,
        expected_session=expected_session,
    )
    src = str(quote.get("source") or "cn-hk")
    if rel == "suspended":
        stagnant_reason = _shape_reason(stagnant, 0)
        return {
            **base,
            "kind": "suspended",
            "note": (
                f"{symbol}: yfinance 日线异常（{stagnant_reason}），cn-hk 备用报价（{src}）"
                "当日亦无成交且价格不变——判为疑似停牌，照实标注、不切换；"
                f"历史指标只用停牌前的数据（截至 {valid_until or '无有效数据'}（停牌前））"
            ),
            "price_source": "yfinance(疑似停牌)",
            "today": None,
        }

    # "落后"必须由备用报价的日期确实比最后一根日线新来证实；报价不比日线
    # 新（连续假期/停市/开盘前）时撤回落后判定，只保留停滞标注（如有）。
    if lag >= LAG_MIN_SESSIONS and rel != "newer" and stagnant is None:
        return None
    if lag >= LAG_MIN_SESSIONS and rel != "newer":
        lag = 0
        reason = _shape_reason(stagnant, lag)

    kind = "+".join(
        (["stagnant"] if stagnant else []) + (["behind"] if lag >= LAG_MIN_SESSIONS else [])
    )

    quote_date = _parse_bar_date(quote.get("as_of"))
    quote_is_today = quote_date is not None and quote_date == today_dt
    price = _as_float(quote.get("price"))
    prev = _as_float(quote.get("prev_close"))
    if not quote_is_today:
        date_label = quote_date.isoformat() if quote_date is not None else "缺失"
        today: dict[str, Any] = {
            "error": (
                f"无法确认当日行情（备用报价日期为 {date_label}，"
                f"非当日 {today_dt.isoformat()}）"
            )
        }
        today_desc = (
            f"cn-hk 备用报价（{src}）日期为 {date_label}（非当日 "
            f"{today_dt.isoformat()}），无法确认当日行情，不输出今日涨跌"
        )
    elif price is not None and prev is not None:
        today = {
            "price": price,
            "prev_close": prev,
            "change_pct": round((price / prev - 1.0) * 100.0, 2),
            "source": src,
            "as_of": quote.get("as_of"),
        }
        today_desc = (
            f"cn-hk 备用报价（{src}）今日 {price:g}、前收 {prev:g}、"
            f"涨跌 {today['change_pct']:+.2f}%，作为单独字段列出，不写回日线"
        )
    elif prev is None:
        today = {"error": "无法计算今日涨跌（备用报价缺前收，不以现价充当前收）"}
        today_desc = "cn-hk 备用报价缺前收，今日涨跌无法计算（不以现价充当前收）"
    else:
        today = {"error": "无法计算今日涨跌（备用报价缺现价）"}
        today_desc = "cn-hk 备用报价缺现价，今日涨跌无法计算"

    if stagnant:
        scope = f"历史指标只用停滞段之前的有效数据（截至 {valid_until or '无有效数据'}）"
        price_label = (
            f"yfinance(截至 {valid_until or '无'})，今日涨跌用 cn-hk({src})"
            if quote_is_today
            else f"yfinance(截至 {valid_until or '无'}，当日行情无法确认)"
        )
    else:
        scope = f"历史指标截至最后一根日线（{valid_until}）"
        price_label = (
            f"yfinance(截至 {valid_until})，今日涨跌用 cn-hk({src})"
            if quote_is_today
            else f"yfinance(截至 {valid_until}，当日行情无法确认)"
        )
    return {
        **base,
        "kind": kind,
        "note": f"{symbol}: yfinance 日线异常（{reason}），{today_desc}；{scope}",
        "price_source": price_label,
        "today": today,
    }


evaluate_cn_daily_bars = evaluate_cn_daily
