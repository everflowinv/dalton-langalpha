#!/usr/bin/env python3
"""MSFT 跟踪采集与提交。

只服务 MSFT。通用逻辑来自同级技能 dalton-thesis-delta 的 collect.py
（仓库里是 plugins/dalton/skills，沙箱里是 .agents/skills，都按同级目录加载）。
本脚本不读不写旧 OpenClaw 根目录，也不调用 gateway、xreach 或子进程。

collect 只写本轮 packet 和一致预期快照。seen.json、last_run.json、updates.log
留到 commit。同一 run_id 已有完成的 packet 时直接读回，不覆盖、不重新取数。
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
SKILL_DIR = Path(__file__).resolve().parent.parent
RESEARCH_CONFIG_PATH = SKILL_DIR / "config" / "research.json"
GENERIC_COLLECT_PATH = SKILL_DIR.parent / "dalton-thesis-delta" / "scripts" / "collect.py"

TICKER = "MSFT"
EXCERPT_MAX = 6000
PARA_MAX = 1800
MAX_INTERNAL_PAGES = 5
MAX_READ_PAGES = 20
READ_CHARS = 30000
AE_LIMIT = 20
SEC_LIMIT = 3
SEC_LOOKBACK_DAYS = 2
AE_LOOKBACK_DAYS = 3
X_SEARCH_COUNT = 40
X_TIMELINE_COUNT = 20
FIRST_LOOKBACK_DAYS = 3
KEYWORD_ROUTES = ("tmtb", "sales-notes", "funda-ai")

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_RUN_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})$")
_LOG_RUN_RE = re.compile(r"(?m)^.* \| (?P<run>[^|]+?) \| material=")
_TRUNCATION_TOKENS = ("列表有更多文档", "更多文档", "缩小筛选", "提高 limit")
_HEADER_RE = re.compile(r"(From|Subject|Date|AlphaEngine doc_id):")
_OY_RE = re.compile(r"0y\s*=\s*FY(\d{2,4})[^0-9]{0,80}?(\d{4})-(\d{2})\s*止")
_PY_RE = re.compile(r"\+1y\s*=\s*FY(\d{2,4})")
_QUOTA_MARKERS = ("额度", "E_DESKTOP", "E_UPSTREAM_API")
FUND_TERMS = re.compile(
    r"(?i)\b(capex|opex|eps|estimates?|consensus|street|guid(?:e|ance)|margins?|fcf|free cash flow|revenue|ebitda|"
    r"depreciation|d&a|multiple|p/?e|ev/|valuation|price target|upgrade|downgrade|buyback|dilution|arr|dau|mau|cpm|"
    r"impressions|backlog|rpo|roi|roic|unit economics|bps|y/y|yoy|q/q|qoq|10-q|8-k|run-?rate|whisper|bogey)\b"
    r"|\d+(?:\.\d+)?\s?%|\$\d"
)
_THRESHOLD_MAP = {
    "fy1_eps_vs_last_run_pct": "eps_fy1_vs_last_pct",
    "fy1_eps_vs_7d_pct": "eps_fy1_vs_7d_pct",
    "fy1_rev_vs_last_run_pct": "rev_fy1_or_q1_vs_last_pct",
    "pt_mean_vs_last_run_pct": "target_price_vs_last_pct",
    "fy1_down_revisions_7d": "down_revisions_7d_min",
}


class CollectError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _load_generic():
    if not GENERIC_COLLECT_PATH.is_file():
        raise CollectError(
            "generic_collect_missing",
            f"找不到同级通用采集模块 {GENERIC_COLLECT_PATH}",
        )
    name = "dalton_thesis_delta_collect_for_msft"
    spec = importlib.util.spec_from_file_location(name, GENERIC_COLLECT_PATH)
    if spec is None or spec.loader is None:
        raise CollectError("generic_collect_missing", f"无法加载 {GENERIC_COLLECT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_g = _load_generic()


def call_bridge_jsonrpc(tool_name: str, arguments: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    return _g.call_bridge_jsonrpc(tool_name, arguments, timeout=timeout)


def fetch_consensus_snapshot(ticker: str, benchmark: str = "QQQ") -> dict[str, Any]:
    return _g.fetch_consensus_snapshot(ticker, benchmark)


def resolve_researchdata_transport() -> dict[str, Any]:
    return _g.resolve_researchdata_transport()


def now_et() -> datetime:
    return _g.now_et()


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def fail(status: str, message: str, **extra: Any) -> int:
    body: dict[str, Any] = {"ok": False, "status": status, "ticker": TICKER, "error": message}
    body.update(extra)
    return emit(body, 2)


def _inside(workspace: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False
    return True


def _reject_openclaw(path: Path) -> None:
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise CollectError("workspace_unreadable", str(exc)[:200]) from exc
    if ".openclaw" in resolved.parts:
        raise CollectError("openclaw_root", "拒绝读写旧 OpenClaw 根目录")


def open_workspace(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    _reject_openclaw(path)
    resolved = path.resolve()
    _reject_openclaw(resolved)
    if not resolved.is_dir():
        raise CollectError("workspace_not_directory", f"工作区不是目录：{raw}")
    return resolved


def assert_no_escaping_symlinks(workspace: Path, root: Path) -> None:
    """工作区里 research/tracker 和 data/consensus 的符号链接不能指向外面。"""
    if not root.exists():
        return
    if root.is_symlink() and not _inside(workspace, root):
        raise CollectError("symlink_escape", f"目录指向工作区外：{root}")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + list(filenames):
            cand = Path(dirpath) / name
            if cand.is_symlink() and not _inside(workspace, cand):
                raise CollectError("symlink_escape", f"符号链接指向工作区外：{cand}")


def ensure_relative(workspace: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise CollectError("path_escape", f"拒绝越出工作区的路径：{relative}")
    current = workspace
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise CollectError("path_escape", f"拒绝越出工作区的路径：{relative}")
        current = current / part
        if current.is_symlink() and not _inside(workspace, current):
            raise CollectError("symlink_escape", f"符号链接指向工作区外：{current}")
        if current.is_symlink():
            current = current.resolve()
            if not _inside(workspace, current):
                raise CollectError("symlink_escape", f"符号链接指向工作区外：{relative}")
    if not _inside(workspace, current):
        raise CollectError("path_escape", f"拒绝越出工作区的路径：{relative}")
    return current


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None or ".." in run_id:
        raise CollectError("bad_run_id", "run_id 只能是一段不含路径的字母、数字、点、下划线或连字符")
    return run_id


def parse_day(value: str, label: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise CollectError("bad_date", f"{label} 需要 YYYY-MM-DD") from exc


def atomic_write_text(workspace: Path, path: Path, text: str) -> None:
    if not _inside(workspace, path.parent):
        raise CollectError("path_escape", f"拒绝写入工作区外：{path}")
    if path.is_symlink():
        raise CollectError("symlink_escape", f"拒绝透过符号链接写入：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_json(workspace: Path, path: Path, obj: Any) -> None:
    atomic_write_text(
        workspace,
        path,
        json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n",
    )


@contextmanager
def workspace_lock(workspace: Path, tracker: Path) -> Iterator[None]:
    tracker.mkdir(parents=True, exist_ok=True)
    lock_path = tracker / ".msft-collect.lock"
    if not _inside(workspace, lock_path):
        raise CollectError("path_escape", "锁文件越出工作区")
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def load_research_config() -> dict[str, Any]:
    try:
        data = json.loads(RESEARCH_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("research_config_unreadable", str(exc)[:200]) from exc
    if not isinstance(data, dict) or str(data.get("ticker") or "").upper() != TICKER:
        raise CollectError("company_not_msft", "研究配置不是 MSFT")
    return data


def load_company(workspace: Path) -> dict[str, Any]:
    path = workspace / "data" / "company.json"
    if not path.exists():
        return {}
    full = ensure_relative(workspace, Path("data") / "company.json")
    try:
        data = json.loads(full.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("company_json_unreadable", str(exc)[:200]) from exc
    if not isinstance(data, dict):
        raise CollectError("company_json_unreadable", "data/company.json 不是对象")
    ticker = str(data.get("ticker") or TICKER).upper()
    if ticker != TICKER:
        raise CollectError("company_not_msft", f"工作区公司是 {ticker}，本采集器只处理 MSFT")
    return data


def benchmark_of(company: dict[str, Any]) -> str:
    benchmarks = company.get("benchmarks")
    if isinstance(benchmarks, list) and benchmarks:
        return str(benchmarks[0])
    return "QQQ"


def map_thresholds(raw: Any) -> dict[str, Any]:
    """把旧 MSFT 阈值键映射到通用 compute_consensus_deltas 的键。"""
    src = raw if isinstance(raw, dict) else {}
    mapped = {
        "eps_fy1_vs_last_pct": 1.0,
        "eps_fy1_vs_7d_pct": 2.0,
        "rev_fy1_or_q1_vs_last_pct": 0.7,
        "target_price_vs_last_pct": 2.0,
        "down_revisions_7d_min": 5,
        "price_1d_pct": 4.0,
        "price_5d_pct": 8.0,
        "any_new_sec_filing": True,
    }
    for old, new in _THRESHOLD_MAP.items():
        if old in src:
            mapped[new] = src[old]
    for key in ("price_1d_pct", "price_5d_pct", "any_new_sec_filing"):
        if key in src:
            mapped[key] = src[key]
    for key in list(mapped):
        if key in src:
            mapped[key] = src[key]
    return mapped


def consensus_deltas(
    cur: dict[str, Any],
    prev: dict[str, Any] | None,
    thr: dict[str, Any],
    sec_new_count: int = 0,
) -> tuple[dict[str, Any], list[str]]:
    deltas, flags = _g.compute_consensus_deltas(cur, prev, thr, sec_new_count=sec_new_count)
    flags = list(flags)
    _append_msft_flags(deltas, flags, thr)
    return deltas, flags


def _append_msft_flags(deltas: dict[str, Any], flags: list[str], thr: dict[str, Any]) -> None:
    # 通用函数已用 eps_fy1_vs_last_pct 报过「本财年(0y) EPS 较上次运行」，这里不重复。
    def add(key: str, limit: Any, label: str) -> None:
        val = deltas.get(key)
        if val is None:
            return
        if abs(float(val)) >= float(limit):
            flags.append(f"{label} {float(val):+.2f}%（阈值 ±{float(limit)}%）")

    add("eps_0y_vs_7d_pct", thr.get("eps_fy1_vs_7d_pct", 2.0), "本财年(0y) EPS 较 7 天前")
    add("rev_0y_vs_last_pct", thr.get("rev_fy1_or_q1_vs_last_pct", 0.7), "本财年(0y) 收入较上次运行")


def _fy_label(raw: str) -> str:
    year = int(raw)
    if year < 100:
        return f"FY{year:02d}"
    return f"FY{year}"


def applicable_years(model: dict[str, Any]) -> list[dict[str, Any]]:
    """从来源原文解析 0y/+1y 各自对应的财年末，不把相对键永久当成某一个财年。"""
    source = str(model.get("source") or "")
    other = model.get("other_income_bn") if isinstance(model.get("other_income_bn"), dict) else {}
    match = _OY_RE.search(source)
    if not match:
        return []
    month = match.group(3)
    years = [{
        "key": "0y",
        "fiscal_year": _fy_label(match.group(1)),
        "period_end_prefix": f"{match.group(2)}-{month}",
        "other_income_bn": other.get("0y"),
    }]
    plus = _PY_RE.search(source)
    if plus:
        fy = int(plus.group(1))
        end_year = fy if fy >= 100 else 2000 + fy
        years.append({
            "key": "+1y",
            "fiscal_year": _fy_label(plus.group(1)),
            "period_end_prefix": f"{end_year}-{month}",
            "other_income_bn": other.get("+1y"),
        })
    return years


def _period_row(table: Any, key: str) -> dict[str, Any]:
    if not isinstance(table, dict):
        return {}
    row = table.get(key)
    return row if isinstance(row, dict) else {}


def build_implied_model(cons: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    """沿用旧公式。其他收入只用于来源写明的财年，不随 0y/+1y 标签滚到下一年。"""
    shares = model.get("diluted_shares_bn")
    tax = model.get("tax_rate")
    other = model.get("other_income_bn") if isinstance(model.get("other_income_bn"), dict) else {}
    source = str(model.get("source") or "")
    years = applicable_years(model)
    assumptions = {
        "diluted_shares_bn": shares,
        "tax_rate": tax,
        "other_income_bn": other,
        "source": source,
        "applicable_years": years,
        "note": "隐含总费用 = 一致收入 − EPS×股数/(1−税率) + 其他收入；单位十亿美元。其他收入只用于来源写明的财年末。",
    }
    out: dict[str, Any] = {"assumptions": assumptions, "periods": {}}
    try:
        shares_f = float(shares)
        tax_f = float(tax)
    except (TypeError, ValueError):
        out["error"] = "股数或税率缺失，未计算隐含费用"
        return out
    if shares_f <= 0 or not 0 <= tax_f < 1:
        out["error"] = "股数须为正数，税率须满足 0≤税率<1，未计算隐含费用"
        return out
    if not years:
        out["error"] = "来源原文里没有解析出 0y/+1y 对应的财年末，未计算隐含费用"
        for key in ("0y", "+1y"):
            out["periods"][key] = {"status": "applicable_years_unparsed", "reason": out["error"]}
        return out

    by_prefix = {item["period_end_prefix"]: item for item in years}
    ee = cons.get("earnings_estimate")
    re_ = cons.get("revenue_estimate")
    for key, label in (("0y", "本财年"), ("+1y", "下一财年")):
        eps_row = _period_row(ee, key)
        rev_row = _period_row(re_, key)
        eps_end = _g._period_end_of(eps_row)
        rev_end = _g._period_end_of(rev_row)
        entry: dict[str, Any] = {"label": label, "status": "ok"}
        if eps_end is None or rev_end is None:
            entry.update({"status": "missing_period", "reason": f"{key} 缺少 period_end，无法确认财年，未计算隐含费用"})
        elif eps_end[:7] != rev_end[:7]:
            entry.update({
                "status": "period_mismatch",
                "earnings_period_end": eps_end,
                "revenue_period_end": rev_end,
                "reason": f"{key} 的 EPS 期间 {eps_end} 与收入期间 {rev_end} 不是同一个月，未计算隐含费用",
            })
        else:
            prefix = eps_end[:7]
            entry["period_end"] = eps_end
            matched = by_prefix.get(prefix)
            currencies = [eps_row.get("currency"), rev_row.get("currency")]
            eps = eps_row.get("avg")
            rev = rev_row.get("avg")
            if matched is None:
                known = "、".join(f"{item['fiscal_year']} 截至 {item['period_end_prefix']}" for item in years)
                entry.update({
                    "status": "period_incompatible",
                    "reason": (
                        f"{key} 的 period_end {eps_end} 对不上来源里写明的财年（{known}），"
                        "未把来源里另一个财年的其他收入套到这个财年"
                    ),
                })
            elif currencies != ["USD", "USD"]:
                entry.update({
                    "status": "currency_incomparable",
                    "currencies": currencies,
                    "reason": f"{key} 的 EPS/收入币种为 {currencies}，需要两行都是 USD 才计算",
                })
            elif eps is None or rev is None or rev == 0:
                entry.update({"status": "missing_inputs", "reason": f"{key} 缺少 EPS 或收入，未计算隐含费用"})
            elif matched.get("other_income_bn") is None:
                entry.update({
                    "status": "other_income_missing",
                    "fiscal_year": matched.get("fiscal_year"),
                    "reason": f"{matched.get('fiscal_year')} 没有其他收入假设，未计算隐含费用",
                })
            else:
                oth = float(matched["other_income_bn"])
                rev_bn = float(rev) / 1e9
                pretax = float(eps) * shares_f / (1 - tax_f)
                entry.update({
                    "fiscal_year": matched["fiscal_year"],
                    "other_income_key": matched["key"],
                    "other_income_bn": oth,
                    "consensus_revenue_bn": round(rev_bn, 1),
                    "consensus_eps": round(float(eps), 2),
                    "implied_pretax_bn": round(pretax, 1),
                    "implied_total_expenses_bn": round(rev_bn - pretax + oth, 1),
                    "implied_op_margin_pct": round((pretax - oth) / rev_bn * 100, 1),
                    "eps_per_10bn_expense": round(-10 * (1 - tax_f) / shares_f, 2),
                    "eps_per_1pct_revenue_no_cost": round(rev_bn * 0.01 * (1 - tax_f) / shares_f, 2),
                })
        out["periods"][key] = entry
        if entry.get("status") == "ok":
            out[key] = entry
    return out


def excerpt(text: str, pat: re.Pattern[str], limit: int = EXCERPT_MAX) -> str:
    paras = re.split(r"\n\s*\n|(?<=\n)(?=[A-Z]{2,5}[:：/])", text)
    hits: list[str] = []
    dup: set[str] = set()
    for para in paras:
        cleaned = "\n".join(
            line for line in para.strip().splitlines() if _HEADER_RE.match(line) is None
        ).strip()
        if cleaned and cleaned not in dup and pat.search(cleaned):
            dup.add(cleaned)
            hits.append(cleaned)
    out: list[str] = []
    used = 0
    for hit in hits:
        hit = hit[:PARA_MAX]
        if used + len(hit) > limit:
            out.append("…（后续命中段落已截断，需要时用 read 全文）")
            break
        out.append(hit)
        used += len(hit)
    return "\n\n".join(out)


def unwrap_content(data: dict[str, Any]) -> dict[str, Any]:
    """通用提取不看对象自身顶层的 content 文本块，这里补上旧 MCP 封装。"""
    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return data


def extract_items_and_page(data: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(data, dict):
        return [], {"cursor": None, "has_more": False, "total_upstream": None, "returned": None}
    items = _g._extract_route_items(data)
    page = _g._extract_route_page_info(data)
    if not isinstance(items, list):
        items = []
    if items:
        return [item for item in items if isinstance(item, dict)], page
    unwrapped = unwrap_content(data)
    if unwrapped is not data:
        nested = _g._extract_route_items(unwrapped)
        page2 = _g._extract_route_page_info(unwrapped)
        if isinstance(nested, list) and nested:
            return [item for item in nested if isinstance(item, dict)], page2
        if page2.get("has_more") or page2.get("cursor"):
            return [], page2
    return [], page


def _payload_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found = [payload]
    structured = payload.get("structuredContent")
    if isinstance(structured, dict):
        found.append(structured)
    inner = payload.get("data")
    if isinstance(inner, dict):
        found.append(inner)
        nested = inner.get("structuredContent")
        if isinstance(nested, dict):
            found.append(nested)
    return found


def extract_document(data: Any) -> tuple[str, Any, dict[str, Any]]:
    bases: list[dict[str, Any]] = []
    if isinstance(data, dict):
        bases.append(data)
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            bases.append(unwrapped)
    for base in bases:
        for candidate in _payload_dicts(base):
            doc = candidate.get("document") if isinstance(candidate.get("document"), dict) else candidate
            if not isinstance(doc, dict):
                continue
            if not any(key in doc for key in ("content", "text", "next_offset")):
                continue
            text = doc.get("content") if isinstance(doc.get("content"), str) else doc.get("text")
            if not isinstance(text, str):
                text = ""
            nxt = doc.get("next_offset")
            if nxt is None:
                nxt = candidate.get("next_offset")
            meta = {
                key: value
                for key, value in doc.items()
                if key not in ("content", "text", "images") and not isinstance(value, (dict, list))
            }
            return text, nxt, meta
    return "", None, {}


def is_no_match(res: dict[str, Any], data: Any) -> bool:
    payloads = [data] if isinstance(data, dict) else []
    if isinstance(data, dict):
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            payloads.append(unwrapped)
    return any(_g._is_bridge_no_match(res, payload) for payload in payloads)


def is_cursor_invalid(res: dict[str, Any], data: Any) -> bool:
    payloads = [data] if isinstance(data, dict) else [{}]
    if isinstance(data, dict):
        unwrapped = unwrap_content(data)
        if unwrapped is not data:
            payloads.append(unwrapped)
    return any(_g._is_cursor_invalid_error(res, payload) for payload in payloads)


def bridge_error(res: dict[str, Any], data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        return str(data.get("error"))[:500]
    return str(res.get("error") or res.get("status") or "unknown_error")[:500]


def _known_truncation(res: dict[str, Any], data: Any) -> str | None:
    """沿用通用采集对 partial / 截断警告的判断。没有 cursor 时这种响应也是未完成。"""
    payload = data if isinstance(data, dict) else {}
    status_val, warnings = _g._extract_route_warnings_and_status(res, payload)
    status_text = str(status_val or "").strip().lower()
    matched = [item for item in warnings if any(token in item for token in _TRUNCATION_TOKENS)]
    if status_text not in ("partial", "incomplete", "未完成") and not matched:
        return None
    detail = "; ".join(matched or warnings) or f"status={status_text or 'partial'}"
    return f"未完成: 结果被截断或覆盖不完整（{detail}）"


def _parse_aware(value: Any) -> datetime | None:
    """把采集/提交时间收成美东时区。没有时区的字符串按美东理解，非法值返回 None。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _run_id_stamp(run_id: Any) -> datetime | None:
    """导入的 last_run 可能没有采集时间。YYYYMMDD-HHMM 形式的 run_id 当作美东时刻。"""
    if not isinstance(run_id, str):
        return None
    match = _RUN_STAMP_RE.fullmatch(run_id)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            tzinfo=ET,
        )
    except ValueError:
        return None


def _order_key(record: dict[str, Any]) -> datetime | None:
    """同一 until 日内的先后。采集时间优先；导入记录没有它时用 run_id 里的时刻，提交时间只作最后兜底。"""
    if not isinstance(record, dict):
        return None
    parsed = _parse_aware(record.get("collected_at"))
    if parsed is not None:
        return parsed
    stamped = _run_id_stamp(record.get("run_id"))
    if stamped is not None:
        return stamped
    return _parse_aware(record.get("committed_at"))


def _coerce_collected_at(packet: dict[str, Any]) -> str | None:
    parsed = _parse_aware(packet.get("collected_at"))
    if parsed is None:
        parsed = _order_key(packet)
    return parsed.isoformat() if parsed is not None else None


def _is_newer_collection(packet: dict[str, Any], last_run: dict[str, Any]) -> bool:
    """until 更早的包不能当最新；同一天或更晚的 until 还要采集时刻严格更晚。"""
    if not last_run or not last_run.get("run_id"):
        return True
    packet_until = str(packet.get("until") or "")
    prev_until = str(last_run.get("until") or "")
    if prev_until and packet_until and packet_until < prev_until:
        return False
    new_key = _order_key(packet)
    old_key = _order_key(last_run)
    later_until = bool(packet_until and prev_until and packet_until > prev_until)
    if new_key is None and old_key is None:
        return later_until
    if old_key is None:
        return True
    if new_key is None:
        return False
    return new_key > old_key


def _log_has_run(text: str, run_id: str) -> bool:
    needle = f" | {run_id} | material="
    return any(needle in line for line in text.splitlines())


def _coerce_retry_since(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return parse_day(value.strip()[:10], "internal_retry_since")
    except CollectError:
        return None


def _internal_since(last_run: dict[str, Any], global_since: str) -> str:
    """内部库可以停在更早的重试日。显式更早的全局 since 仍然取更早的那天。"""
    retry = _coerce_retry_since(last_run.get("internal_retry_since") if isinstance(last_run, dict) else None)
    if retry is None:
        return global_since
    return min(global_since, retry)


def _internal_needs_retry(internal: dict[str, Any]) -> bool:
    if internal.get("pending"):
        return True
    status = internal.get("status") or {}
    if not isinstance(status, dict):
        return bool(internal.get("pending"))
    for row in status.values():
        if isinstance(row, dict) and (row.get("ok") is False or row.get("incomplete")):
            return True
    return False


def _ae_overlap_start(covered_until: str) -> str:
    day = date.fromisoformat(str(covered_until).strip()[:10])
    return (day - timedelta(days=AE_LOOKBACK_DAYS)).isoformat()


def _completed_ae_start(default_start: str, covered_until: Any) -> str:
    """已完成查询的下一轮起点不晚于上次覆盖日，并保留一小段重叠。"""
    if not isinstance(covered_until, str) or not covered_until.strip():
        return default_start
    try:
        covered_day = date.fromisoformat(covered_until.strip()[:10]).isoformat()
        overlap = _ae_overlap_start(covered_until)
    except ValueError:
        return default_start
    return min(default_start, overlap, covered_day)


def read_document(doc_id: str) -> dict[str, Any]:
    parts: list[str] = []
    offset: Any = 0
    meta: dict[str, Any] = {}
    pages = 0
    error: str | None = None
    more = False
    quota = False
    while pages < MAX_READ_PAGES:
        res = call_bridge_jsonrpc(
            "research",
            {
                "route": "all",
                "operation": "read",
                "document": doc_id,
                "params": {"offset": offset, "max_chars": READ_CHARS},
            },
        )
        pages += 1
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        text, nxt, meta_part = extract_document(data)
        if meta_part and not meta:
            meta = meta_part
        if not res.get("ok") and not text:
            error = bridge_error(res, data)
            quota = any(marker in error for marker in _QUOTA_MARKERS)
            more = False
            break
        parts.append(text)
        more = nxt not in (None, "", offset)
        if not more:
            break
        offset = nxt
    if more:
        error = "read_page_cap"
    incomplete = bool(error) or more
    return {
        "text": "".join(parts),
        "meta": meta,
        "pages": pages,
        "incomplete": incomplete,
        "error": error,
        "quota": quota,
        "ok": not incomplete,
    }


def _safe_doc_name(doc_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", doc_id)[:180]


def _read_state_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("seen_unreadable", f"{path.name} 无法读取") from exc
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise CollectError("seen_unreadable", f"{path.name} 必须是字符串列表")
    return data


def _read_state_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("state_unreadable", f"{path.name} 无法读取") from exc
    if not isinstance(data, dict):
        raise CollectError("state_unreadable", f"{path.name} 必须是对象")
    return data


def _local_state(workspace: Path, name: str, default: Any) -> Any:
    for relative in (Path("research") / "tracker" / name, Path(name)):
        candidate = workspace / relative
        if not candidate.exists():
            continue
        full = ensure_relative(workspace, relative)
        try:
            return json.loads(full.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectError("state_unreadable", f"{name} 无法读取") from exc
    return default


def _keyword_args(route: str, query: str, since: str, until: str, limit: int, cursor: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"media": "text", "search_mode": "keyword"}
    if cursor:
        params["cursor"] = cursor
    return {
        "route": route,
        "operation": "search",
        "query": query,
        "start": since,
        "end": until,
        "limit": limit,
        "params": params,
    }


def _wiki_args(since: str, cursor: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"indexed_since": since, "media": "text"}
    if cursor:
        params["cursor"] = cursor
    return {"route": "company-wiki", "operation": "list", "limit": 100, "params": params}


def _fetch_pages(tool: str, build_args, page_cap: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    ok = True
    error: str | None = None
    incomplete = False
    reason: str | None = None
    warnings: list[str] = []
    last_args: dict[str, Any] = {}
    while pages < page_cap:
        last_args = build_args(cursor)
        res = call_bridge_jsonrpc(tool, last_args)
        pages += 1
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        page_items, page = extract_items_and_page(data)
        _status, page_warnings = _g._extract_route_warnings_and_status(res, data if isinstance(data, dict) else {})
        for item in page_warnings:
            if item not in warnings:
                warnings.append(item)
        if not res.get("ok"):
            if is_no_match(res, data):
                break
            ok = False
            error = bridge_error(res, data)
            if is_cursor_invalid(res, data):
                cursor = None
                reason = f"游标失效，下一轮从同一窗口起点重查：{error}"
            incomplete = True
            break
        items.extend(page_items)
        has_more = bool(page.get("has_more"))
        cursor = page.get("cursor") if has_more else None
        trunc = _known_truncation(res, data)
        if has_more and cursor and pages < page_cap:
            continue
        if has_more and not cursor:
            incomplete = True
            reason = trunc or "上游 has_more 为真但没有下一页 cursor"
            cursor = None
        elif has_more and pages >= page_cap:
            incomplete = True
            reason = f"已达翻页上限 {page_cap}，仍有下一页"
        elif trunc:
            incomplete = True
            reason = trunc
            cursor = None
        break
    status = "error" if not ok else "incomplete" if incomplete else "ok"
    return {
        "items": items,
        "request_ok": ok,
        "status": status,
        "incomplete": incomplete,
        "error": error or reason,
        "warnings": warnings,
        "pages": pages,
        "cursor": cursor,
        "request": {key: value for key, value in last_args.items() if key != "params"} | {
            "params": last_args.get("params")
        },
    }


def collect_internal(
    cfg: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    workspace: Path,
    run_dir: Path,
) -> dict[str, Any]:
    pat = re.compile(str(cfg["aliases_regex"]))
    excluded = {str(item) for item in cfg.get("excluded_documents") or []}
    query = " ".join(str(term) for term in cfg.get("alias_terms") or [])
    limit = int(cfg.get("internal_limit") or 60)
    cap = int(cfg.get("internal_read_cap_per_route") or 12)
    min_hits = int(cfg.get("wiki_min_alias_hits") or 2)
    found: dict[str, dict[str, Any]] = {}
    status: dict[str, Any] = {}
    stop_reads = False

    for route in cfg.get("internal_routes") or []:
        route = str(route)
        if route == "company-wiki":
            fetched = _fetch_pages("research", lambda cursor: _wiki_args(since, cursor), MAX_INTERNAL_PAGES)
            operation = "list"
        elif route in KEYWORD_ROUTES:
            fetched = _fetch_pages(
                "research",
                lambda cursor, route=route: _keyword_args(route, query, since, until, limit, cursor),
                MAX_INTERNAL_PAGES,
            )
            operation = "keyword-search"
        else:
            status[route] = {"operation": "unsupported", "ok": False, "status": "error", "error": "未知内部路线"}
            continue
        returned = len(fetched["items"])
        status[route] = {
            "operation": operation,
            "ok": bool(fetched["request_ok"]),
            "status": fetched["status"],
            "returned": returned,
            "pages": fetched["pages"],
            "incomplete": fetched["incomplete"],
            "error": fetched["error"],
            "warnings": fetched.get("warnings") or [],
            "limit": 100 if route == "company-wiki" else limit,
            "read_cap": cap,
            "request": fetched["request"],
            "pending_reads": [],
        }
        if not fetched["request_ok"]:
            continue
        for row in fetched["items"]:
            doc_id = str(row.get("document_id") or "")
            if not doc_id or doc_id.startswith("image:") or doc_id in seen or doc_id in found:
                continue
            location = row.get("location")
            if doc_id in excluded or (location is not None and str(location) in excluded):
                continue
            owned = str(row.get("company") or "").upper() == TICKER
            found[doc_id] = {
                "route": route,
                "title": row.get("title"),
                "date": row.get("date"),
                "indexed_at": row.get("indexed_at"),
                "from": row.get("original_source"),
                "location": location,
                "score": row.get("keyword_hits") or 0,
                "owned": owned,
                "queries": [f"indexed_since {since}"] if route == "company-wiki" else ["alias-keyword"],
            }

    new: dict[str, Any] = {}
    irrelevant: dict[str, Any] = {}
    pending: dict[str, Any] = {}
    withheld: dict[str, Any] = {}
    counts: dict[str, int] = {}
    ordered = sorted(
        found.items(),
        key=lambda kv: (1 if kv[1].get("owned") else 0, kv[1].get("score") or 0, kv[1].get("date") or ""),
        reverse=True,
    )
    for doc_id, info in ordered:
        route = str(info["route"])
        counts[route] = counts.get(route, 0) + 1
        if counts[route] > cap:
            info["excerpt"] = None
            info["skipped"] = "超过单来源全文读取上限，只记标题"
            info["analyzed"] = False
            info["incomplete"] = True
            info["pending_reason"] = "read_cap"
            pending[doc_id] = info
            status[route]["pending_reads"].append(doc_id)
            status[route]["incomplete"] = True
            status[route]["status"] = "incomplete"
            if not status[route].get("error"):
                status[route]["error"] = "超过单来源全文读取上限，未读文档留在待处理"
            continue
        if stop_reads:
            info["analyzed"] = False
            info["incomplete"] = True
            info["pending_reason"] = "quota_stop"
            pending[doc_id] = info
            status[route]["pending_reads"].append(doc_id)
            status[route]["incomplete"] = True
            status[route]["status"] = "incomplete"
            continue
        read = read_document(doc_id)
        info["chars"] = len(read["text"])
        info["read_pages"] = read["pages"]
        if not read["ok"]:
            info["read_error"] = read["error"]
            info["incomplete"] = True
            info["pending_reason"] = "read_truncated" if read["error"] == "read_page_cap" else "read_failed"
            pending[doc_id] = info
            status[route]["pending_reads"].append(doc_id)
            status[route]["incomplete"] = True
            status[route]["status"] = "incomplete"
            status[route]["error"] = status[route].get("error") or read["error"]
            if read["quota"]:
                stop_reads = True
            continue
        text = read["text"]
        hits = len(pat.findall(text))
        info["alias_hits"] = hits
        excerpt_text = excerpt(text, pat) if text else ""
        info["excerpt"] = excerpt_text
        info["excerpt_chars"] = len(excerpt_text)
        info["analyzed"] = True
        if route == "company-wiki" and not info.get("owned") and hits < min_hits:
            if hits == 0:
                irrelevant[doc_id] = info
            else:
                info["pending_reason"] = "wiki_below_min_alias_hits"
                withheld[doc_id] = info
            continue
        if route != "company-wiki" and not pat.search(text):
            irrelevant[doc_id] = info
            continue
        relative = Path("research") / "tracker" / run_dir.name / "excerpts" / f"{_safe_doc_name(doc_id)}.txt"
        target = ensure_relative(workspace, relative)
        header = f"{doc_id} | {info.get('date')} | {info.get('title')} | {info.get('from')}\n\n"
        atomic_write_text(workspace, target, header + excerpt_text)
        info["excerpt_file"] = relative.as_posix()
        new[doc_id] = info
    return {
        "status": status,
        "new": new,
        "irrelevant": irrelevant,
        "pending": pending,
        "withheld": withheld,
    }


def _has_msft_ae_state(last_run: dict[str, Any]) -> bool:
    state = last_run.get("msft_ae_state") if isinstance(last_run, dict) else None
    return isinstance(state, dict) and isinstance(state.get("queries"), list)


def _ae_prev_map(last_run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    state = last_run.get("msft_ae_state") if isinstance(last_run, dict) else None
    queries = state.get("queries") if isinstance(state, dict) else None
    if not isinstance(queries, list):
        return {}
    return {
        str(item.get("query")): item
        for item in queries
        if isinstance(item, dict) and isinstance(item.get("query"), str)
    }


def _legacy_ae_map(last_run: dict[str, Any], queries: list[str]) -> dict[str, dict[str, Any]]:
    """没有 msft_ae_state 时，用导入的覆盖日和未完成窗口初始化每一条查询。

    旧 cursor 只对当时那一次筛选有效，这里从窗口起点重放，不带 cursor。
    """
    if not isinstance(last_run, dict) or _has_msft_ae_state(last_run):
        return {}
    covered = _coerce_day(last_run.get("ae_covered_until"))
    resume = last_run.get("ae_resume") if isinstance(last_run.get("ae_resume"), dict) else None
    window = resume.get("window") if isinstance(resume, dict) and isinstance(resume.get("window"), dict) else {}
    start = _coerce_day(window.get("start") or window.get("since"))
    end = _coerce_day(window.get("end") or window.get("until"))
    found: dict[str, dict[str, Any]] = {}
    for query in queries:
        if start and end and start <= end:
            found[query] = {
                "query": query,
                "complete": False,
                "ok": True,
                "cursor": None,
                "covered_until": covered if covered and covered < end else None,
                "window": {"start": start, "end": end},
            }
        elif covered:
            found[query] = {
                "query": query,
                "complete": True,
                "ok": True,
                "cursor": None,
                "covered_until": covered,
            }
    return found


def collect_alphaengine(
    cfg: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    last_run: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """每条查询一轮只搜一次。未完成的查询下一轮重放原窗口和 cursor，不改用新的 until。"""
    pat = re.compile(str(cfg["aliases_regex"]))
    names = {str(name) for name in cfg.get("ae_company_names") or []}
    queries = [str(q) for q in (cfg.get("ae_queries") or [])][: int(cfg.get("ae_max_searches") or 2)]
    prev_map = _ae_prev_map(last_run) or _legacy_ae_map(last_run, queries)
    found: dict[str, Any] = {}
    statuses: list[dict[str, Any]] = []
    state_queries: list[dict[str, Any]] = []
    default_start = (date.fromisoformat(since) - timedelta(days=AE_LOOKBACK_DAYS)).isoformat()
    for query in queries:
        prev = prev_map.get(query)
        if prev and not prev.get("complete") and isinstance(prev.get("window"), dict):
            start = str(prev["window"].get("start") or default_start)
            end = str(prev["window"].get("end") or until)
            saved_cursor = prev.get("cursor")
            cursor = (
                saved_cursor
                if prev.get("ok", True) is not False and isinstance(saved_cursor, str) and saved_cursor
                else None
            )
            carried_covered = prev.get("covered_until")
        else:
            carried_covered = prev.get("covered_until") if isinstance(prev, dict) else None
            start = _completed_ae_start(default_start, carried_covered)
            end, cursor = until, None
        params: dict[str, Any] = {"sort": "relevance"}
        if cursor:
            params["cursor"] = cursor
        arguments = {
            "route": "alphaengine",
            "operation": "search_library",
            "query": query,
            "start": start,
            "end": end,
            "limit": AE_LIMIT,
            "params": params,
        }
        res = call_bridge_jsonrpc("research", arguments)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        items, page = extract_items_and_page(data)
        ok = bool(res.get("ok"))
        error = None
        if not ok and is_no_match(res, data):
            ok = True
            items = []
        if not ok and cursor and is_cursor_invalid(res, data):
            error = bridge_error(res, data)
            cursor = None
            ok = False
        elif not ok:
            error = bridge_error(res, data)
        trunc = _known_truncation(res, data) if ok else None
        has_more = bool(page.get("has_more")) if ok else False
        next_cursor = page.get("cursor") if ok and has_more else None
        if ok and has_more and not next_cursor:
            error = trunc or "has_more 为真但没有 cursor"
            next_cursor = None
            has_more = False
        elif ok and trunc and not (has_more and next_cursor):
            error = trunc
            next_cursor = None
            has_more = False
        complete = bool(ok and not has_more and not error)
        covered = end if complete else carried_covered
        if complete and isinstance(carried_covered, str) and carried_covered.strip():
            boundary = carried_covered.strip()[:10]
            if start > boundary:
                complete = False
                error = "窗口起点晚于已覆盖日期，未推进覆盖"
                covered = carried_covered
        if isinstance(covered, str) and not covered.strip():
            covered = None
        entry = {
            "query": query,
            "window": {"start": start, "end": end},
            "cursor": next_cursor,
            "has_more": has_more,
            "complete": complete,
            "covered_until": covered,
            "ok": ok,
            "returned": len(items) if ok else 0,
            "error": error,
            "resumed_cursor": cursor,
        }
        state_queries.append(entry)
        statuses.append({
            "query": query,
            "ok": ok,
            "returned": entry["returned"],
            "has_more": has_more,
            "incomplete": not complete,
            "cursor": next_cursor,
            "window": entry["window"],
            "error": error,
            "request": arguments,
        })
        if not ok:
            continue
        for row in items:
            raw_id = row.get("doc_id")
            if not raw_id:
                continue
            doc_id = str(raw_id)
            if not doc_id.startswith("alphaengine:"):
                doc_id = f"alphaengine:{doc_id}"
            if doc_id in seen or doc_id in found:
                continue
            companies = {str(name) for name in (row.get("companies") or [])}
            title = str(row.get("title") or "")
            if names and not (companies & names) and pat.search(title) is None:
                continue
            attribution = row.get("source_attribution")
            found[doc_id] = {
                "title": row.get("title"),
                "publish_time": row.get("publish_time"),
                "sources": row.get("sources"),
                "document_code": row.get("document_code"),
                "companies": row.get("companies"),
                "snippet": str(row.get("snippet") or "")[:1500],
                "reference_url": row.get("reference_url"),
                "source_attribution": attribution,
                "query": query,
            }
    ae_state = {"queries": state_queries}
    return {"status": statuses, "new": found}, ae_state


def collect_sec(cfg: dict[str, Any], since: str, seen: set[str]) -> dict[str, Any]:
    floor = (date.fromisoformat(since) - timedelta(days=SEC_LOOKBACK_DAYS)).isoformat()
    found: dict[str, Any] = {}
    statuses: list[dict[str, Any]] = []
    for form in cfg.get("sec_forms") or []:
        tool_name, arguments = _g.build_sec_filing_request(TICKER, str(form), limit=SEC_LIMIT)
        res = call_bridge_jsonrpc(tool_name, arguments)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        rows, _page = extract_items_and_page(data)
        ok = bool(res.get("ok"))
        error = None
        if not ok and is_no_match(res, data):
            ok = True
            rows = []
        elif not ok:
            error = bridge_error(res, data)
        trunc = _known_truncation(res, data) if ok else None
        if trunc:
            error = trunc
        statuses.append({
            "form": form,
            "ok": ok,
            "count": len(rows) if ok else 0,
            "incomplete": bool(trunc),
            "status": "incomplete" if trunc else ("ok" if ok else "error"),
            "error": error,
            "request": arguments,
            "floor": floor,
        })
        if not ok:
            continue
        for row in rows:
            accession = row.get("accession_no") or row.get("accession")
            if not accession:
                continue
            acc = str(accession)
            doc_id = acc if acc.startswith("sec:") else f"sec:{acc}"
            filing_date = str(row.get("filing_date") or "")
            if doc_id in seen or doc_id in found or filing_date < floor:
                continue
            found[doc_id] = {
                "form": row.get("form") or form,
                "filing_date": filing_date,
                "report_date": row.get("report_date"),
                "accession": acc,
            }
    return {"status": statuses, "new": found}


def _parse_x_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.strptime(value.strip(), "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _in_window(parsed: datetime | None, since: str, until: str) -> bool:
    if parsed is None:
        return True
    day = parsed.date().isoformat()
    return since <= day <= until


def _coerce_day(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return None


def _x_upstream_until(until: str) -> tuple[str, str]:
    """把美东自然日扩成上游仍能覆盖到当天结束的 UTC 日期。

    时间线按推文的 UTC 日历日做含当日过滤。搜索的 until: 排除该 UTC 日的零点。
    返回 (时间线 until, 搜索 until)。调用方仍用美东日期做最终过滤。
    """
    end = datetime.combine(date.fromisoformat(until) + timedelta(days=1), datetime.min.time(), tzinfo=ET)
    timeline = (end - timedelta(microseconds=1)).astimezone(UTC).date().isoformat()
    end_utc = end.astimezone(UTC)
    boundary = end_utc.date()
    if end_utc > datetime.combine(boundary, datetime.min.time(), tzinfo=UTC):
        boundary += timedelta(days=1)
    return timeline, boundary.isoformat()


def _metric(metrics: Any, key: str) -> int | None:
    if not isinstance(metrics, dict) or key not in metrics:
        return None
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _load_pool(workspace: Path, cfg: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    raw = _local_state(workspace, "x_pool.json", {})
    if not isinstance(raw, dict):
        raise CollectError("state_unreadable", "x_pool.json 必须是对象")
    eligible: list[str] = []
    for handle, info in raw.items():
        if not isinstance(info, dict) or info.get("active", True) is False:
            continue
        tickers = {str(item).upper() for item in (info.get("tickers") or [])}
        if tickers & {TICKER, "ALL"}:
            eligible.append(str(handle))
    return eligible[: int(cfg.get("x_pool_max") or 25)], raw


def _load_authors(workspace: Path) -> dict[str, Any]:
    raw = _local_state(workspace, "x_authors.json", {})
    if not isinstance(raw, dict):
        raise CollectError("state_unreadable", "x_authors.json 必须是对象")
    return raw


def _x_items(res: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else None
    if isinstance(inner, dict) and isinstance(inner.get("items"), list):
        items = [item for item in inner["items"] if isinstance(item, dict)]
        page = _g._extract_route_page_info(data)
        if not page.get("has_more") and not page.get("cursor"):
            page = _g._extract_route_page_info(inner)
        return items, page
    return extract_items_and_page(data)


def collect_x(
    cfg: dict[str, Any],
    since: str,
    until: str,
    seen: set[str],
    workspace: Path,
) -> dict[str, Any]:
    timeline_until, search_until = _x_upstream_until(until)
    raw: dict[str, dict[str, Any]] = {}
    statuses: list[dict[str, Any]] = []
    failures: list[str] = []
    unprocessed = False
    for query in cfg.get("x_queries") or []:
        arguments = {
            "route": "x-timeline",
            "operation": "search",
            "query": query,
            "since": since,
            "until": search_until,
            "count": X_SEARCH_COUNT,
        }
        res = call_bridge_jsonrpc("community", arguments)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        if not res.get("ok"):
            error = bridge_error(res, data)
            statuses.append({"query": query, "operation": "search", "ok": False, "error": error, "request": arguments})
            failures.append(f"search: {error}")
            continue
        items, page = _x_items(res)
        if page.get("has_more"):
            unprocessed = True
        statuses.append({
            "query": query,
            "operation": "search",
            "ok": True,
            "count": len(items),
            "has_more": bool(page.get("has_more")),
            "request": arguments,
        })
        for item in items:
            post_id = item.get("id")
            if post_id is None:
                continue
            raw.setdefault(str(post_id), {"item": item, "via": "search", "pool_handle": None})

    handles, pool = _load_pool(workspace, cfg)
    pool_keys = {str(handle).lower() for handle in pool}
    for handle in handles:
        arguments = {
            "route": "x-timeline",
            "operation": "tweets",
            "handle": handle,
            "since": since,
            "until": timeline_until,
            "count": X_TIMELINE_COUNT,
        }
        res = call_bridge_jsonrpc("community", arguments)
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        if not res.get("ok"):
            error = bridge_error(res, data)
            statuses.append({"pool": handle, "operation": "tweets", "ok": False, "error": error, "request": arguments})
            failures.append(f"tweets {handle}: {error}")
            continue
        items, page = _x_items(res)
        if page.get("has_more"):
            unprocessed = True
        kept = 0
        for item in items:
            post_id = item.get("id")
            if post_id is None:
                continue
            if not _in_window(_parse_x_time(item.get("created_at") or item.get("createdAt")), since, until):
                continue
            kept += 1
            raw[str(post_id)] = {"item": item, "via": "pool", "pool_handle": handle}
        statuses.append({
            "pool": handle,
            "operation": "tweets",
            "ok": True,
            "count": len(items),
            "kept": kept,
            "has_more": bool(page.get("has_more")),
            "request": arguments,
        })

    candidates = []
    outside = 0
    for post_id, packed in raw.items():
        item = packed["item"]
        if item.get("is_repost") or item.get("isRetweet"):
            continue
        if f"x:{post_id}" in seen:
            continue
        parsed = _parse_x_time(item.get("created_at") or item.get("createdAt"))
        if not _in_window(parsed, since, until):
            outside += 1
            continue
        text = str(item.get("text") or "")
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        likes = _metric(metrics, "likes")
        reposts = _metric(metrics, "reposts")
        candidates.append({
            "id": post_id,
            "via": packed["via"],
            "pool_handle": packed["pool_handle"],
            "item": item,
            "text": text,
            "fund_hits": len(FUND_TERMS.findall(text)),
            "engagement": (likes or 0) + 3 * (reposts or 0),
            "likes": likes,
            "reposts": reposts,
            "replies": _metric(metrics, "replies"),
            "views": _metric(metrics, "views"),
        })
    substantive = sorted(
        [row for row in candidates if row["via"] == "pool" or row["fund_hits"] > 0],
        key=lambda row: (row["via"] == "pool", row["engagement"]),
        reverse=True,
    )
    keep_max = int(cfg.get("x_keep_max") or 30)
    enrich_max = int(cfg.get("x_enrich_max") or 20)
    noise_rows = [row for row in candidates if row not in substantive]
    overflow = substantive[keep_max:]
    # 超过保留上限的候选按旧程序进入 noise，不写入 failed_sources。
    noise_ids = [f"x:{row['id']}" for row in noise_rows + overflow]
    authors = _load_authors(workspace)
    new: dict[str, Any] = {}
    followup: list[dict[str, Any]] = []
    for index, row in enumerate(substantive[:keep_max]):
        item = row["item"]
        handle = row["pool_handle"] or item.get("author_handle")
        cache = authors.get(str(handle).lower()) if isinstance(handle, str) else None
        if not isinstance(cache, dict):
            cache = {}
        enrich = index < enrich_max
        followers = cache.get("followers") if enrich and "followers" in cache else None
        bio = cache.get("bio") if enrich and "bio" in cache else None
        if enrich and (followers is None or bio is None):
            followup.append({
                "handle": handle,
                "id": f"x:{row['id']}",
                "reason": "本地作者缓存没有粉丝数或简介" if cache else "本地作者缓存没有这个账号",
            })
        elif not enrich and handle:
            followup.append({"handle": handle, "id": f"x:{row['id']}", "reason": "超过作者资料补充上限"})
        display = cache.get("handle") or handle
        new[f"x:{row['id']}"] = {
            "url": item.get("url") or f"https://x.com/{display or 'i'}/status/{row['id']}",
            "created_at": item.get("created_at") or item.get("createdAt"),
            "author": display,
            "author_name": item.get("author_name") or cache.get("name"),
            "followers": followers if isinstance(followers, (int, float)) and not isinstance(followers, bool) else None,
            "bio": bio if isinstance(bio, str) else None,
            "bookmarks": None,
            "profile_source": "x_authors_cache" if cache else "unavailable",
            "profile_fetched_at": cache.get("fetched_at") if cache else None,
            "in_pool": bool(display and str(display).lower() in pool_keys),
            "via": row["via"],
            "likes": row["likes"],
            "reposts": row["reposts"],
            "replies": row["replies"],
            "views": row["views"],
            "fund_hits": row["fund_hits"],
            "engagement": row["engagement"],
            "is_reply": item.get("is_reply"),
            "text": row["text"][:900],
        }
    return {
        "status": statuses,
        "new": new,
        "noise_ids": noise_ids,
        "pool_size": len(handles),
        "queries": list(cfg.get("x_queries") or []),
        "outside_window": outside,
        "unprocessed_upstream": unprocessed,
        "profile_followup": followup,
        "failures": failures,
        "metrics_note": (
            "likes、reposts、replies、views 按 x-timeline 归一化结果保存；"
            "bookmarks 上游没有返回，记为 null，排序只用 likes+3*reposts。"
        ),
        "note": "专业投资人观点作多空论据，权重低于 TMTB、卖方邮件和 AlphaEngine；券商动作和公司数字回原始来源核对。",
    }


def failed_sources(
    internal: dict[str, Any],
    ae: dict[str, Any],
    sec: dict[str, Any],
    xdata: dict[str, Any],
    consensus: dict[str, Any],
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for route, row in (internal.get("status") or {}).items():
        if not isinstance(row, dict):
            continue
        if not row.get("ok", False) or row.get("incomplete"):
            failures.append({"source": str(route), "error": str(row.get("error") or row.get("status") or "incomplete")})
    for row in ae.get("status") or []:
        if isinstance(row, dict) and (not row.get("ok", False) or row.get("incomplete")):
            failures.append({
                "source": "alphaengine",
                "error": f"{row.get('query')}: {row.get('error') or '未完成，覆盖日期不推进'}",
            })
    sec_bits = [
        f"{row.get('form')}: {row.get('error') or row.get('status') or 'failed'}"
        for row in sec.get("status") or []
        if isinstance(row, dict) and (not row.get("ok", False) or row.get("incomplete"))
    ]
    if sec_bits:
        failures.append({"source": "sec", "error": "; ".join(sec_bits)})
    if xdata.get("failures"):
        failures.append({"source": "x-timeline", "error": "; ".join(xdata["failures"])})
    status_l = str(consensus.get("status") or "").strip().lower()
    if consensus.get("ok") is False or status_l in ("error", "partial", "incomplete"):
        detail = str(consensus.get("error") or consensus.get("status") or "incomplete")
        if consensus.get("field_status") is not None:
            detail = f"{detail}: field_status={json.dumps(consensus.get('field_status'), ensure_ascii=False)}"
        failures.append({"source": "estimates", "error": detail})
    return failures


def _previous_snapshot(
    workspace: Path,
    consensus_dir: Path,
    current: Path,
    hint: Any,
) -> dict[str, Any] | None:
    safe_hint = None
    if isinstance(hint, str) and hint and not hint.startswith("/") and ".." not in Path(hint).parts:
        try:
            full = ensure_relative(workspace, Path(hint))
        except CollectError:
            full = None
        if full is not None and _inside(consensus_dir, full) and full.resolve() != current.resolve() and full.is_file():
            safe_hint = hint
    loaded = _g.latest_previous_snapshot(consensus_dir, current, safe_hint, workspace)
    return loaded if isinstance(loaded, dict) else None


def render_packet_md(pk: dict[str, Any]) -> str:
    lines = [
        f"# {pk['ticker']} 跟踪数据包 {pk['run_id']}",
        "",
        f"- 窗口：{pk['since']} 至 {pk['until']}（美东）",
        f"- 采集时间：{pk.get('collected_at') or '未知'}",
        f"- 内部库重试起点：{pk.get('internal_retry_since') or '无'}",
        f"- 一致预期快照：`{pk.get('snapshot_file')}`",
        "- 是否允许短路结束（short_circuit_eligible）：false",
        "- 新闻和待核实线索仍要在研究步骤里核对，采集阶段不短路。",
    ]
    failures = pk.get("failed_sources") or []
    if failures:
        lines.append("- 取数失败或未完成：")
        for item in failures:
            lines.append(f"  - {item.get('source')}：{item.get('error')}")
    lines.extend(["", "## 自动触发项"])
    lines.extend([f"- {flag}" for flag in pk.get("auto_flags") or []] or ["- 无"])
    cons = pk.get("consensus") or {}
    price = cons.get("price") if isinstance(cons.get("price"), dict) else {}
    lines.extend(["", "## 一致预期"])
    if price:
        lines.append(
            f"- 收盘 {price.get('as_of')}：{price.get('close')}，1 日 {price.get('chg_1d_pct')}%，5 日 {price.get('chg_5d_pct')}%"
        )
    lines.append(f"- 修正差值：`{json.dumps(pk.get('deltas') or {}, ensure_ascii=False)}`")
    model = pk.get("implied_model") or {}
    lines.extend(["", "## 隐含费用"])
    assumptions = model.get("assumptions") or {}
    if assumptions.get("source"):
        lines.append(f"- 来源：{assumptions['source']}")
    for item in assumptions.get("applicable_years") or []:
        lines.append(
            f"- 假设对应财年：{item.get('key')} = {item.get('fiscal_year')}，截至 {item.get('period_end_prefix')}，其他收入 {item.get('other_income_bn')}"
        )
    for key in ("0y", "+1y"):
        row = (model.get("periods") or {}).get(key) or {}
        if row.get("status") == "ok":
            lines.append(
                f"- {row.get('label')}（{row.get('fiscal_year')}，period_end {row.get('period_end')}，其他收入取 {row.get('other_income_key')}）："
                f"收入 {row.get('consensus_revenue_bn')}、EPS {row.get('consensus_eps')}、税前 {row.get('implied_pretax_bn')}、"
                f"总费用 {row.get('implied_total_expenses_bn')}、经营利润率 {row.get('implied_op_margin_pct')}%、"
                f"费用每多 100 亿 EPS {row.get('eps_per_10bn_expense')}、收入每多 1% EPS {row.get('eps_per_1pct_revenue_no_cost')}"
            )
        else:
            lines.append(f"- {key}：{row.get('reason') or row.get('status') or '未计算'}")
    if model.get("error"):
        lines.append(f"- 模型状态：{model['error']}")

    internal = pk.get("internal") or {}
    lines.extend(["", "## 内部库"])
    for doc_id, info in (internal.get("new") or {}).items():
        lines.append(
            f"- {doc_id} | {info.get('date')} | {info.get('route')} | {info.get('title')} | {info.get('excerpt_file') or info.get('skipped')}"
        )
        if info.get("excerpt"):
            lines.append(info["excerpt"])
    if not internal.get("new"):
        lines.append("- 无新文档")
    lines.append(f"- 全文读完没有别名、记为无关：{len(internal.get('irrelevant') or {})} 篇")
    pending = internal.get("pending") or {}
    if pending:
        lines.append("- 读全文未完成，保持待处理，没有记成无关：")
        for doc_id, info in pending.items():
            lines.append(f"  - {doc_id} | {info.get('title')} | {info.get('pending_reason')} | {info.get('read_error')}")
    withheld = internal.get("withheld") or {}
    if withheld:
        lines.append("- wiki 全文别名不足 2 次，保留可见、不记成无关：")
        for doc_id, info in withheld.items():
            lines.append(f"  - {doc_id} | {info.get('title')} | 命中 {info.get('alias_hits')}")
    lines.append(f"- 来源状态：`{json.dumps(internal.get('status') or {}, ensure_ascii=False)}`")

    ae = pk.get("alphaengine") or {}
    lines.extend(["", "## AlphaEngine"])
    lines.append("固定查询：")
    for row in (pk.get("msft_ae_state") or {}).get("queries") or []:
        window = row.get("window") or {}
        lines.append(
            f"- {row.get('query')} | 窗口 {window.get('start')} 至 {window.get('end')} | "
            f"complete={str(bool(row.get('complete'))).lower()} | cursor={row.get('cursor')}"
        )
    for doc_id, info in (ae.get("new") or {}).items():
        lines.append(f"- {doc_id} | {info.get('publish_time')} | {info.get('title')}")
        if info.get("snippet"):
            lines.append(f"  摘要：{info['snippet'][:600]}")
    if not ae.get("new"):
        lines.append("- 无")

    sec = pk.get("sec") or {}
    lines.extend(["", "## SEC"])
    for doc_id, info in (sec.get("new") or {}).items():
        lines.append(f"- {doc_id} | {info.get('form')} | {info.get('filing_date')}")
    if not sec.get("new"):
        lines.append("- 无")
    lines.append(f"- 状态：`{json.dumps(sec.get('status') or [], ensure_ascii=False)}`")

    news = pk.get("news") or {}
    lines.extend(["", "## 新闻"])
    lines.append(f"- 状态：{news.get('status')}，需要代理用 WebSearch/WebFetch 打开原文")
    lines.append(f"- 窗口：{news.get('since')} 至 {news.get('until')}")
    for query in news.get("queries") or []:
        lines.append(f"- {query}")

    xdata = pk.get("x") or {}
    lines.extend(["", "## X"])
    lines.append(f"- 账号池 {xdata.get('pool_size', 0)} 个；噪声 {len(xdata.get('noise_ids') or [])} 条，提交前不写入 seen")
    if xdata.get("unprocessed_upstream"):
        lines.append("- 上游还有未取回的候选。这些 id 不进入失败列表；已取回但超过保留上限的进入噪声。")
    lines.append(f"- {xdata.get('metrics_note')}")
    for query in xdata.get("queries") or []:
        lines.append(f"- 查询：{query}")
    for doc_id, info in (xdata.get("new") or {}).items():
        lines.append(
            f"- {doc_id} | {info.get('via')} | @{info.get('author')} | {info.get('created_at')} | "
            f"赞 {info.get('likes')} 转 {info.get('reposts')} 收藏 {info.get('bookmarks')} | 信号词 {info.get('fund_hits')}"
        )
        if info.get("bio"):
            lines.append(f"  简介：{info['bio']}")
        lines.append(f"  {info.get('text')}")
    if xdata.get("profile_followup"):
        lines.append("- 作者资料待补充：")
        for row in xdata["profile_followup"]:
            lines.append(f"  - {row.get('handle')} | {row.get('reason')}")
    if not xdata.get("new"):
        lines.append("- 无")
    lines.append(f"- 状态：`{json.dumps(xdata.get('status') or [], ensure_ascii=False)}`")
    lines.append("")
    lines.append("collect 不修改 seen.json、last_run.json、updates.log。")
    return "\n".join(lines) + "\n"


def _completed_packet(path: Path, run_id: str) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectError("packet_unreadable", "已有 packet.json 无法读取，没有覆盖") from exc
    if not isinstance(data, dict):
        raise CollectError("packet_exists_not_complete", "已有 packet.json 不是完成的数据包，没有覆盖")
    if data.get("collection_complete") is not True:
        raise CollectError("packet_exists_not_complete", "已有 packet.json 未标记完成，没有覆盖、也没有重新取数")
    if data.get("run_id") != run_id or str(data.get("ticker") or "").upper() != TICKER:
        raise CollectError("packet_run_mismatch", "已有 packet 的 run_id 或公司与本轮不一致，没有覆盖")
    return data


def _summary(workspace: Path, packet: dict[str, Any], recovered: bool) -> dict[str, Any]:
    run_id = str(packet["run_id"])
    tracker = workspace / "research" / "tracker" / run_id
    internal = packet.get("internal") or {}
    return {
        # 完成的数据包保留来源告警，供后续研究披露。
        "ok": True,
        "recovered": recovered,
        "ticker": TICKER,
        "run_id": run_id,
        "since": packet.get("since"),
        "until": packet.get("until"),
        "short_circuit_eligible": False,
        "failed_sources": packet.get("failed_sources") or [],
        "packet_json": (Path("research") / "tracker" / run_id / "packet.json").as_posix(),
        "packet_md": (Path("research") / "tracker" / run_id / "packet.md").as_posix(),
        "snapshot_file": packet.get("snapshot_file"),
        "counts": {
            "internal": len(internal.get("new") or {}),
            "internal_irrelevant": len(internal.get("irrelevant") or {}),
            "internal_pending": len(internal.get("pending") or {}),
            "alphaengine": len((packet.get("alphaengine") or {}).get("new") or {}),
            "sec": len((packet.get("sec") or {}).get("new") or {}),
            "x": len((packet.get("x") or {}).get("new") or {}),
            "x_noise": len((packet.get("x") or {}).get("noise_ids") or []),
        },
        "tracker_dir": str(tracker.relative_to(workspace)),
    }


def _window(last_run: dict[str, Any], since_arg: str | None, today: str) -> tuple[str, str]:
    if since_arg:
        since = parse_day(since_arg, "since")
    elif last_run.get("until"):
        since = (date.fromisoformat(str(last_run["until"])) - timedelta(days=1)).isoformat()
    else:
        since = (date.fromisoformat(today) - timedelta(days=FIRST_LOOKBACK_DAYS)).isoformat()
    if since > today:
        raise CollectError("bad_date", "since 晚于本轮 until")
    return since, today


def cmd_collect(args: argparse.Namespace) -> int:
    workspace = open_workspace(args.workspace_dir)
    company = load_company(workspace)
    cfg = load_research_config()
    run_id = validate_run_id(args.run_id or now_et().strftime("%Y%m%d-%H%M"))
    paths = _g.tracker_paths(workspace)
    assert_no_escaping_symlinks(workspace, paths["tracker"])
    assert_no_escaping_symlinks(workspace, paths["consensus"])
    with workspace_lock(workspace, paths["tracker"]):
        packet_path = paths["tracker"] / run_id / "packet.json"
        existing = _completed_packet(packet_path, run_id)
        if existing is not None:
            md_path = paths["tracker"] / run_id / "packet.md"
            if not md_path.exists():
                atomic_write_text(workspace, md_path, render_packet_md(existing))
            return emit(_summary(workspace, existing, True))
        transport = resolve_researchdata_transport()
        if transport.get("mode") is None:
            return fail(
                _g.RESEARCHDATA_TRANSPORT_UNCONFIGURED,
                str(transport.get("error") or _g.RESEARCHDATA_TRANSPORT_UNCONFIGURED),
                hint="本轮未采集任何来源",
            )
        last_run = _read_state_object(paths["last_run"])
        seen = set(_read_state_list(paths["seen"]))
        before = {
            "seen": paths["seen"].read_text(encoding="utf-8") if paths["seen"].exists() else None,
            "last": paths["last_run"].read_text(encoding="utf-8") if paths["last_run"].exists() else None,
            "log": paths["log"].read_text(encoding="utf-8") if paths["log"].exists() else None,
        }
        current = now_et()
        today = current.date().isoformat()
        since, until = _window(last_run, args.since, today)
        collected_at = current.isoformat()
        internal_since = _internal_since(last_run, since)
        run_dir = paths["tracker"] / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        consensus = fetch_consensus_snapshot(TICKER, benchmark=benchmark_of(company))
        stamp = current.strftime("%Y-%m-%d_%H%M")
        snap_rel = Path("data") / "consensus" / f"{stamp}.json"
        snap_path = ensure_relative(workspace, snap_rel)
        previous = _previous_snapshot(workspace, paths["consensus"], snap_path, last_run.get("snapshot_file"))
        internal = collect_internal(cfg, internal_since, until, seen, workspace, run_dir)
        ae, ae_state = collect_alphaengine(cfg, since, until, seen, last_run)
        sec = collect_sec(cfg, since, seen)
        xdata = collect_x(cfg, since, until, seen, workspace)
        thr = map_thresholds(cfg.get("thresholds"))
        deltas, flags = consensus_deltas(consensus, previous, thr, sec_new_count=len(sec["new"]))
        model = build_implied_model(consensus, cfg.get("model") or {})
        failures = failed_sources(internal, ae, sec, xdata, consensus)
        packet = {
            "ticker": TICKER,
            "run_id": run_id,
            "since": since,
            "until": until,
            "snapshot_file": snap_rel.as_posix(),
            "consensus": consensus,
            "implied_model": model,
            "deltas": deltas,
            "auto_flags": flags,
            "thresholds": thr,
            "internal": internal,
            "alphaengine": ae,
            "sec": sec,
            "x": xdata,
            "news": {
                "status": "pending_agent_search",
                "queries": list(cfg.get("news_queries") or []),
                "since": since,
                "until": until,
                "requires_original_fetch": True,
            },
            "failed_sources": failures,
            "short_circuit_eligible": False,
            "collected_at": collected_at,
            "internal_since": internal_since,
            "internal_retry_since": internal_since if _internal_needs_retry(internal) else None,
            "msft_ae_state": ae_state,
            "limits": {
                "internal_limit": cfg.get("internal_limit"),
                "internal_read_cap_per_route": cfg.get("internal_read_cap_per_route"),
                "internal_pages": MAX_INTERNAL_PAGES,
                "read_pages": MAX_READ_PAGES,
                "excerpt_max": EXCERPT_MAX,
                "ae_max_searches": cfg.get("ae_max_searches"),
                "ae_limit": AE_LIMIT,
                "sec_limit": SEC_LIMIT,
                "x_search_count": X_SEARCH_COUNT,
                "x_timeline_count": X_TIMELINE_COUNT,
                "x_pool_max": cfg.get("x_pool_max"),
                "x_keep_max": cfg.get("x_keep_max"),
                "x_enrich_max": cfg.get("x_enrich_max"),
            },
            "collection_complete": True,
        }
        paths["consensus"].mkdir(parents=True, exist_ok=True)
        atomic_write_json(workspace, snap_path, consensus)
        atomic_write_text(workspace, run_dir / "packet.md", render_packet_md(packet))
        atomic_write_json(workspace, run_dir / "packet.json", packet)
        after = {
            "seen": paths["seen"].read_text(encoding="utf-8") if paths["seen"].exists() else None,
            "last": paths["last_run"].read_text(encoding="utf-8") if paths["last_run"].exists() else None,
            "log": paths["log"].read_text(encoding="utf-8") if paths["log"].exists() else None,
        }
        if after != before:
            raise CollectError("state_mutated", "collect 写入了 seen、last_run 或 updates.log")
        return emit(_summary(workspace, packet, False))


def _internal_uncommitted(info: Any) -> bool:
    """未读完、被跳过或明确标成未分析的文档不能进入 seen。"""
    if not isinstance(info, dict):
        return False
    if info.get("analyzed") is False:
        return True
    return bool(info.get("read_error") or info.get("incomplete") or info.get("skipped"))


def _commit_ids(packet: dict[str, Any]) -> set[str]:
    internal = packet.get("internal") if isinstance(packet.get("internal"), dict) else {}
    pending = set((internal.get("pending") or {}))
    withheld = set((internal.get("withheld") or {}))
    ids: set[str] = set()
    for doc_id, info in (internal.get("new") or {}).items():
        if doc_id in pending or doc_id in withheld or _internal_uncommitted(info):
            continue
        ids.add(str(doc_id))
    for doc_id, info in (internal.get("irrelevant") or {}).items():
        if _internal_uncommitted(info):
            continue
        ids.add(str(doc_id))
    for bucket in (
        (packet.get("alphaengine") or {}).get("new"),
        (packet.get("sec") or {}).get("new"),
        (packet.get("x") or {}).get("new"),
    ):
        if isinstance(bucket, dict):
            ids.update(str(doc_id) for doc_id in bucket)
    noise = (packet.get("x") or {}).get("noise_ids") if isinstance(packet.get("x"), dict) else []
    if isinstance(noise, list):
        ids.update(str(doc_id) for doc_id in noise if isinstance(doc_id, str))
    return ids


def _committed_ae_covered(last_run: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for query, row in _ae_prev_map(last_run).items():
        day = _coerce_day(row.get("covered_until"))
        if day:
            found[query] = day
    return found


def _validate_ae_state(state: dict[str, Any], last_run: dict[str, Any] | None = None) -> None:
    retained = _committed_ae_covered(last_run or {})
    queries = state.get("queries")
    if not isinstance(queries, list):
        raise CollectError("bad_ae_state", "msft_ae_state.queries 必须是列表")
    for row in queries:
        if not isinstance(row, dict) or not isinstance(row.get("query"), str):
            raise CollectError("bad_ae_state", "AE 查询状态缺少 query")
        window = row.get("window")
        if not isinstance(window, dict):
            raise CollectError("bad_ae_state", "AE 查询状态缺少 window")
        start = parse_day(str(window.get("start")), "ae.window.start")
        end = parse_day(str(window.get("end")), "ae.window.end")
        covered = row.get("covered_until")
        if row.get("complete") is True:
            if covered != end:
                raise CollectError("ae_covered_until_mismatch", "完成的 AE 查询只能把 covered_until 记到本窗口终点")
        elif covered not in (None, ""):
            covered_day = parse_day(str(covered), "ae.covered_until")
            # 同一天重新采集未完成时，保留已经提交的覆盖日期。
            # 从更早的覆盖日抬到本窗口终点，或超过终点，仍然拒绝。
            if covered_day > end or (covered_day == end and retained.get(row["query"], "") < covered_day):
                raise CollectError("ae_incomplete_advanced", "未完成的 AE 查询不能把覆盖日期推进到窗口终点")
        if start > end:
            raise CollectError("bad_ae_state", "AE 窗口起点晚于终点")


def _snapshot_for_commit(workspace: Path, packet: dict[str, Any]) -> str:
    raw = packet.get("snapshot_file")
    if not isinstance(raw, str) or not raw or raw.startswith("/") or "\\" in raw:
        raise CollectError("bad_snapshot", "snapshot_file 必须是工作区内的相对路径")
    relative = Path(raw)
    if relative.parts[:2] != ("data", "consensus"):
        raise CollectError("bad_snapshot", "snapshot_file 必须位于 data/consensus")
    full = ensure_relative(workspace, relative)
    if not full.is_file():
        raise CollectError("bad_snapshot", "snapshot_file 不存在")
    return relative.as_posix()


def cmd_commit(args: argparse.Namespace) -> int:
    if args.material not in ("yes", "no"):
        return fail("bad_material", "material 只能是 yes 或 no")
    summary = str(args.summary or "").strip()
    if not summary:
        return fail("bad_summary", "summary 不能为空")
    workspace = open_workspace(args.workspace_dir)
    load_company(workspace)
    run_id = validate_run_id(args.run_id)
    paths = _g.tracker_paths(workspace)
    assert_no_escaping_symlinks(workspace, paths["tracker"])
    assert_no_escaping_symlinks(workspace, paths["consensus"])
    with workspace_lock(workspace, paths["tracker"]):
        packet_path = paths["tracker"] / run_id / "packet.json"
        packet = _completed_packet(packet_path, run_id)
        if packet is None:
            return fail("packet_missing", f"找不到完成的数据包 {run_id}")
        if packet.get("run_id") != run_id:
            return fail("packet_run_mismatch", "packet 的 run_id 与参数不一致")
        snapshot = _snapshot_for_commit(workspace, packet)
        ae_state = packet.get("msft_ae_state")
        if not isinstance(ae_state, dict):
            return fail("bad_ae_state", "packet 缺少 msft_ae_state")
        seen = _read_state_list(paths["seen"])
        last_run = _read_state_object(paths["last_run"])
        _validate_ae_state(ae_state, last_run)
        log_text = paths["log"].read_text(encoding="utf-8") if paths["log"].exists() else ""
        new_ids = _commit_ids(packet)
        merged_set = set(seen) | new_ids
        merged = sorted(merged_set)
        current = last_run.get("run_id") == run_id
        # 同一 run 已经写进 last_run 时只补日志和 seen，不把状态改回这轮。
        advance = _is_newer_collection(packet, last_run) and not current
        already_logged = _log_has_run(log_text, run_id)
        if not paths["seen"].exists() or merged_set != set(seen):
            atomic_write_json(workspace, paths["seen"], merged)
        if advance:
            last_body = {
                "ticker": TICKER,
                "run_id": run_id,
                "committed_at": now_et().isoformat(),
                "collected_at": _coerce_collected_at(packet),
                "since": packet.get("since"),
                "until": packet.get("until"),
                "snapshot_file": snapshot,
                "material": args.material,
                "summary": summary,
                "msft_ae_state": ae_state,
            }
            if "internal_retry_since" in packet:
                last_body["internal_retry_since"] = _coerce_retry_since(packet.get("internal_retry_since"))
            elif "internal_retry_since" in last_run:
                last_body["internal_retry_since"] = last_run.get("internal_retry_since")
            atomic_write_json(workspace, paths["last_run"], last_body)
        log_repaired = False
        # updates.log 是完成回执。比当前 last_run 更早的首次提交也记一行，否则该轮会一直算未完成。
        # 这一行不改 last_run，也不改其中的 AE cursor 或内部库重试起点。
        if not already_logged:
            paths["log"].parent.mkdir(parents=True, exist_ok=True)
            with paths["log"].open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{now_et().strftime('%Y-%m-%d %H:%M')} | {run_id} | material={args.material} | {summary}\n"
                )
            log_repaired = current and not advance
        packet_rel = (Path("research") / "tracker" / run_id / "packet.json").as_posix()
        body = {
            "ok": True,
            "committed": run_id,
            "material": args.material if advance or not current else last_run.get("material"),
            "seen_total": len(merged),
            "new_ids_marked_seen": len(merged_set - set(seen)),
            "packet_json": packet_rel,
            "last_run_file": "research/tracker/last_run.json",
            "updates_log": "research/tracker/updates.log",
            "stale": not advance and not current,
            "idempotent": current and already_logged and merged_set == set(seen),
            "log_repaired": log_repaired,
            "last_run_unchanged": not advance,
        }
        if advance:
            body["counts"] = _summary(workspace, packet, False)["counts"]
        return emit(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MSFT 跟踪采集与提交")
    sub = parser.add_subparsers(dest="cmd", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("--workspace-dir", required=True)
    collect.add_argument("--since")
    collect.add_argument("--run-id")
    commit = sub.add_parser("commit")
    commit.add_argument("--workspace-dir", required=True)
    commit.add_argument("--run-id", required=True)
    commit.add_argument("--material", required=True)
    commit.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "collect":
            return cmd_collect(args)
        return cmd_commit(args)
    except CollectError as exc:
        return fail(exc.status, exc.message)


if __name__ == "__main__":
    sys.exit(main())
