#!/usr/bin/env python3
"""JEPQ—QQQ Put 价差三级灯信号计算、统一校验、变色状态与美股面板小块渲染。"""

from __future__ import annotations

import html
import json
import math
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

JEPQ_GAP_THRESHOLD = 0.003   # +0.3%
JEPQ_RVOL_GREEN_MAX = 0.20   # 20%
JEPQ_RVOL_YELLOW_MAX = 0.25  # 25%
JEPQ_MIN_BARS = 21
JEPQ_VALID_LIGHTS = ("🟢", "🟡", "🔴")
JEPQ_STATE_REL = "state/sentiment-dashboard/jepq_light.json"
JEPQ_CARD_TITLE = "JEPQ—QQQ Put 价差"
JEPQ_DISCLAIMER = "这只是规则筛选结果，不是独立的仓位指令。"
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_iso_date(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if _ISO_DATE_RE.match(text):
            try:
                date.fromisoformat(text)
            except ValueError:
                return ""
            return text
    return ""


def _parse_finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _clean_close_series(series: pd.Series | None) -> pd.Series:
    """将收盘价序列清洗为按日期升序、去重的有效数值序列。"""
    if not isinstance(series, pd.Series) or series.empty:
        return pd.Series(dtype=float)
    numeric = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if numeric.empty:
        return pd.Series(dtype=float)
    idx = pd.to_datetime(numeric.index, errors="coerce")
    valid = ~pd.isna(idx)
    if not bool(valid.any()):
        return pd.Series(dtype=float)
    clean = pd.Series(numeric.to_numpy(dtype=float)[valid], index=idx[valid], dtype=float)
    if getattr(clean.index, "tz", None) is not None:
        clean.index = clean.index.tz_convert("America/New_York").tz_localize(None)
    clean = clean.sort_index()
    clean = clean[~clean.index.duplicated(keep="last")]
    return clean


def _missing_result(error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "light": None,
        "reason": f"数据不足 — {error}",
        "d8_gap": None,
        "d7_gap": None,
        "rvol20": None,
        "qqq": None,
        "jepq": None,
        "asof": "",
    }


def _missing_error_text(raw: Any) -> str:
    if isinstance(raw, dict):
        for key in ("error", "reason"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return "JEPQ—QQQ 数据缺失或格式无效"


def evaluate_jepq_signal(
    qqq_series: pd.Series | None,
    jepq_series: pd.Series | None,
    *,
    reference_series: pd.Series | list[pd.Series | None] | None = None,
) -> dict[str, Any]:
    """按 D-8 gap（阈值 ≤+0.3%）与 QQQ 20D 年化 RVol（20% / 25%）计算三级灯信号。"""
    qqq_clean = _clean_close_series(qqq_series)
    jepq_clean = _clean_close_series(jepq_series)
    if qqq_clean.empty and jepq_clean.empty:
        return _missing_result("缺少 QQQ 与 JEPQ 行情")
    if qqq_clean.empty:
        return _missing_result("缺少 QQQ 行情")
    if jepq_clean.empty:
        return _missing_result("缺少 JEPQ 行情")

    qqq_last = pd.Timestamp(qqq_clean.index[-1]).date().isoformat()
    jepq_last = pd.Timestamp(jepq_clean.index[-1]).date().isoformat()
    if qqq_last != jepq_last:
        return _missing_result(f"QQQ({qqq_last}) 与 JEPQ({jepq_last}) 最新交易日不一致")

    # The reference is the latest trading day among the available US reference series.
    # The dashboard passes a list; a list with no usable series means freshness cannot be confirmed.
    if reference_series is not None:
        refs = reference_series if isinstance(reference_series, (list, tuple)) else [reference_series]
        ref_lasts = [
            pd.Timestamp(c.index[-1]).date().isoformat()
            for c in (_clean_close_series(r) for r in refs)
            if not c.empty
        ]
        if not ref_lasts:
            return _missing_result("缺少美股参考标的行情，无法确认 QQQ/JEPQ 是否为最新交易日")
        ref_last = max(ref_lasts)
    else:
        ref_last = ""
    if ref_last and qqq_last < ref_last:
        return _missing_result(f"QQQ/JEPQ 最新交易日({qqq_last})早于美股参考标的({ref_last})")

    df = pd.concat({"qqq": qqq_clean, "jepq": jepq_clean}, axis=1, sort=True).dropna()
    if len(df) < JEPQ_MIN_BARS:
        return _missing_result(f"QQQ 与 JEPQ 重叠交易日不足（{len(df)}/{JEPQ_MIN_BARS}）")

    qqq_price = float(df["qqq"].iloc[-1])
    jepq_price = float(df["jepq"].iloc[-1])
    qqq_d8_base = float(df["qqq"].iloc[-9])
    jepq_d8_base = float(df["jepq"].iloc[-9])
    qqq_d7_base = float(df["qqq"].iloc[-8])
    jepq_d7_base = float(df["jepq"].iloc[-8])
    if min(qqq_price, jepq_price, qqq_d8_base, jepq_d8_base, qqq_d7_base, jepq_d7_base) <= 0.0:
        return _missing_result("QQQ 或 JEPQ 存在非正收盘价，无法计算收益率")

    d8_gap = (qqq_price / qqq_d8_base - 1.0) - (jepq_price / jepq_d8_base - 1.0)
    d7_gap = (qqq_price / qqq_d7_base - 1.0) - (jepq_price / jepq_d7_base - 1.0)
    qqq_rets = df["qqq"].pct_change().dropna()
    if len(qqq_rets) < 20:
        return _missing_result(f"QQQ 有效收益率样本不足（{len(qqq_rets)}/20）")
    rvol20 = float(qqq_rets.tail(20).std() * np.sqrt(252))
    if not all(math.isfinite(v) for v in (d8_gap, d7_gap, rvol20)):
        return _missing_result("JEPQ—QQQ 指标计算出现非有限值")

    # 阈值比较运算符与分支顺序严格对齐原版 jepq_qqq_arb_signal()。
    if d8_gap <= JEPQ_GAP_THRESHOLD and rvol20 < JEPQ_RVOL_GREEN_MAX:
        light = "🟢"
        reason = "条件较好 — D-8 gap 未超阈值，且波动率低于 20%"
    elif d8_gap <= JEPQ_GAP_THRESHOLD and rvol20 < JEPQ_RVOL_YELLOW_MAX:
        light = "🟡"
        reason = "条件一般 — D-8 gap 未超阈值，波动率为 20%—25%"
    elif d8_gap > JEPQ_GAP_THRESHOLD:
        light = "🔴"
        reason = "暂停观察 — D-8 gap 超过阈值"
    else:
        light = "🔴"
        reason = "暂停观察 — 波动率不低于 25%"

    asof = pd.Timestamp(df.index[-1]).date().isoformat()
    return {
        "ok": True,
        "light": light,
        "reason": reason,
        "d8_gap": float(d8_gap),
        "d7_gap": float(d7_gap),
        "rvol20": float(rvol20),
        "qqq": float(qqq_price),
        "jepq": float(jepq_price),
        "asof": asof,
    }


def validate_jepq(raw: Any) -> dict[str, Any] | None:
    """统一校验 JEPQ 信号载荷；字段齐全且合法时返回归一化字典，否则返回 None。"""
    if not isinstance(raw, dict):
        return None
    if raw.get("ok") is False:
        return None
    light = raw.get("light")
    if light not in JEPQ_VALID_LIGHTS:
        return None
    reason_raw = raw.get("reason")
    if not isinstance(reason_raw, str) or not reason_raw.strip():
        return None
    asof = _valid_iso_date(raw.get("asof"))
    if not asof:
        return None
    d8_gap = _parse_finite_float(raw.get("d8_gap"))
    d7_gap = _parse_finite_float(raw.get("d7_gap"))
    rvol20 = _parse_finite_float(raw.get("rvol20"))
    qqq = _parse_finite_float(raw.get("qqq"))
    jepq = _parse_finite_float(raw.get("jepq"))
    if (
        d8_gap is None
        or d7_gap is None
        or rvol20 is None
        or qqq is None
        or jepq is None
        or qqq <= 0.0
        or jepq <= 0.0
        or rvol20 < 0.0
    ):
        return None
    out: dict[str, Any] = {
        "ok": True,
        "light": light,
        "reason": reason_raw.strip(),
        "d8_gap": d8_gap,
        "d7_gap": d7_gap,
        "rvol20": rvol20,
        "qqq": qqq,
        "jepq": jepq,
        "asof": asof,
    }
    prev_light = raw.get("prev_light")
    if prev_light in JEPQ_VALID_LIGHTS:
        out["prev_light"] = prev_light
    prev_asof = _valid_iso_date(raw.get("prev_asof"))
    if prev_asof:
        out["prev_asof"] = prev_asof
    return out


def load_jepq_state(state_path: Path) -> dict[str, str] | None:
    """读取上一次记录的 JEPQ 状态（light 与 asof）；缺失或损坏时返回 None。"""
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    light = payload.get("light")
    if light not in JEPQ_VALID_LIGHTS:
        return None
    return {
        "light": light,
        "asof": _valid_iso_date(payload.get("asof")),
    }


def load_jepq_light(state_path: Path) -> str | None:
    """读取上一次记录的 JEPQ 灯色；文件缺失或损坏时返回 None。"""
    state = load_jepq_state(state_path)
    return state["light"] if state else None


def save_jepq_light(state_path: Path, light: str, asof: str = "") -> None:
    """原子写回最新灯色与 asof，仅在卡片成功生成后调用。"""
    if light not in JEPQ_VALID_LIGHTS:
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "light": light,
        "asof": _valid_iso_date(asof),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    fd, tmp_name = tempfile.mkstemp(dir=state_path.parent, prefix=".jepq_light.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, state_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def detect_jepq_light_change(
    current_light: str | None,
    prev_light: str | None,
    *,
    current_asof: str = "",
    prev_asof: str = "",
) -> dict[str, Any]:
    """比较本次与上次灯色；首次运行、本次无有效灯色、或本次 asof <= 已保存 asof 时均按未变色处理。"""
    curr = current_light if current_light in JEPQ_VALID_LIGHTS else None
    prev = prev_light if prev_light in JEPQ_VALID_LIGHTS else None
    c_asof = _valid_iso_date(current_asof)
    p_asof = _valid_iso_date(prev_asof)
    stale_or_same_day = bool(c_asof and p_asof and c_asof <= p_asof)
    changed = bool(curr and prev and (not stale_or_same_day) and curr != prev)
    should_save = bool(curr and (not stale_or_same_day))
    return {
        "light": curr,
        "prev_light": prev,
        "changed": changed,
        "should_save": should_save,
        "stale_or_same_day": stale_or_same_day,
        "transition": f"JEPQ—QQQ 信号灯由 {prev} 转为 {curr}" if changed else "",
    }


def jepq_light_tone(light: str | None) -> str:
    if light == "🟢":
        return "green"
    if light == "🟡":
        return "orange"
    if light == "🔴":
        return "red"
    return "orange"


def format_jepq_digest_line(
    jepq: dict[str, Any] | None,
    *,
    prev_light: str | None = None,
    prev_asof: str | None = None,
) -> str:
    """渲染 markdown 退路里的单行 JEPQ—QQQ 信号。"""
    valid = validate_jepq(jepq)
    if valid is None:
        return f"- JEPQ—QQQ：数据不足（{_missing_error_text(jepq)}）"
    effective_prev = prev_light if prev_light is not None else valid.get("prev_light")
    effective_prev_asof = prev_asof if prev_asof is not None else valid.get("prev_asof", "")
    change = detect_jepq_light_change(
        valid["light"],
        effective_prev,
        current_asof=valid["asof"],
        prev_asof=effective_prev_asof,
    )
    prefix = f"- {change['transition']}：" if change["changed"] else "- JEPQ—QQQ："
    return (
        f"{prefix}{valid['light']} {valid['reason']}"
        f"｜QQQ {valid['qqq']:.2f} / JEPQ {valid['jepq']:.2f}"
        f"｜D-8 gap {valid['d8_gap'] * 100:+.2f}%（阈值 ≤+0.3%）"
        f"｜D-7 gap {valid['d7_gap'] * 100:+.2f}%"
        f"｜QQQ 20D RVol {valid['rvol20'] * 100:.1f}%"
    )


def build_jepq_card_section(
    jepq: dict[str, Any] | None,
    *,
    prev_light: str | None = None,
    prev_asof: str | None = None,
    paint: Callable[[str, str | None], str],
    warn_tone: str = "orange",
) -> dict[str, Any]:
    """构造 Card Schema 2.0 中的「JEPQ—QQQ Put 价差」分节。"""
    valid = validate_jepq(jepq)
    if valid is None:
        return {
            "title": JEPQ_CARD_TITLE,
            "lead": paint("数据不足", warn_tone),
            "lines": [],
            "quiet": [_missing_error_text(jepq), JEPQ_DISCLAIMER],
        }
    effective_prev = prev_light if prev_light is not None else valid.get("prev_light")
    effective_prev_asof = prev_asof if prev_asof is not None else valid.get("prev_asof", "")
    change = detect_jepq_light_change(
        valid["light"],
        effective_prev,
        current_asof=valid["asof"],
        prev_asof=effective_prev_asof,
    )
    light = valid["light"]
    reason = valid["reason"]
    tone = jepq_light_tone(light)
    status_text = paint(f"{light} {reason}".strip(), tone)
    detail_lines = [
        f"QQQ {valid['qqq']:.2f} ｜ JEPQ {valid['jepq']:.2f}",
        f"D-8 gap：{valid['d8_gap'] * 100:+.2f}%（阈值 ≤+0.3%）｜ D-7 gap：{valid['d7_gap'] * 100:+.2f}%",
        f"QQQ 20D RVol：{valid['rvol20'] * 100:.1f}%",
    ]
    if change["changed"]:
        return {
            "title": paint(change["transition"], tone),
            "lead": JEPQ_CARD_TITLE,
            "lines": [f"当前判断：{status_text}", *detail_lines],
            "quiet": [JEPQ_DISCLAIMER],
        }
    return {
        "title": JEPQ_CARD_TITLE,
        "lead": status_text,
        "lines": detail_lines,
        "quiet": [JEPQ_DISCLAIMER],
    }


def render_jepq_section(
    jepq: dict[str, Any] | None,
    *,
    metric_card: Callable[..., str],
    fmt: Callable[..., str],
) -> str:
    """在美股面板（us-market）渲染 JEPQ—QQQ Put 价差观察小块。"""
    head = (
        '<div class="monitor-head">'
        "<h2>JEPQ—QQQ Put 价差观察</h2>"
        "<p>基于 D-8 收益差（阈值 ≤+0.3%）与 QQQ 20日已实现波动率（20% / 25%）划分三级灯。</p>"
        "</div>"
    )
    valid = validate_jepq(jepq)
    if valid is None:
        cards = metric_card("JEPQ—QQQ 信号灯", "数据不足", _missing_error_text(jepq), "muted", "数据截至 —")
        return (
            f'<section class="monitor-section monitor-jepq">{head}'
            f'<section class="cards bottom-cards">{cards}</section>'
            f'<p class="note">{html.escape(JEPQ_DISCLAIMER)}</p></section>'
        )

    light = valid["light"]
    reason = valid["reason"]
    short_label = reason.split("—", 1)[0].strip() if "—" in reason else reason
    detail_reason = reason.split("—", 1)[1].strip() if "—" in reason else reason
    asof_meta = f"数据截至 {valid['asof']}（美东时间）"
    d8 = valid["d8_gap"]
    d7 = valid["d7_gap"]
    rvol = valid["rvol20"]
    qqq_px = valid["qqq"]
    jepq_px = valid["jepq"]

    light_tone = "neutral" if light == "🟢" else "warm" if light == "🟡" else "hot"
    gap_tone = "hot" if d8 > JEPQ_GAP_THRESHOLD else "neutral"
    rvol_tone = (
        "hot"
        if rvol >= JEPQ_RVOL_YELLOW_MAX
        else "warm"
        if rvol >= JEPQ_RVOL_GREEN_MAX
        else "neutral"
    )
    cards = "".join([
        metric_card("JEPQ—QQQ 信号灯", f"{light} {short_label}", detail_reason, light_tone, asof_meta),
        metric_card(
            "QQQ / JEPQ 收盘价",
            f"{fmt(qqq_px, 2)} / {fmt(jepq_px, 2)}",
            "纳指100 ETF 与 JEPQ 复权收盘价（美元）",
            "neutral",
            asof_meta,
        ),
        metric_card(
            "D-8 / D-7 收益差",
            f"{d8 * 100:+.2f}%",
            f"D-8 阈值 ≤+0.3%；D-7 gap {d7 * 100:+.2f}%",
            gap_tone,
            "QQQ 相对 JEPQ 近8日与近7日收益差",
        ),
        metric_card(
            "QQQ 20日已实现波动率",
            f"{rvol * 100:.1f}%",
            "绿灯 <20%；黄灯 20%—25%；红灯 ≥25%",
            rvol_tone,
            "QQQ 近20个交易日收益率年化波动率",
        ),
    ])
    return (
        f'<section class="monitor-section monitor-jepq">{head}'
        f'<section class="cards bottom-cards">{cards}</section>'
        f'<p class="note">{html.escape(JEPQ_DISCLAIMER)}</p></section>'
    )
