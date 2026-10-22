#!/usr/bin/env python3
"""Host-side Streamable HTTP MCP bridge for OpenClaw's `researchdata_mcp.py`.

Listens on `127.0.0.1:8960` (configurable via `DALTON_BRIDGE_HOST` / `DALTON_BRIDGE_PORT`).
Endpoints:
  - `GET /healthz` (unauthenticated health & quota check)
  - `POST /mcp` (Streamable HTTP JSON-RPC 2.0 endpoint; authenticated via
    `Authorization: Bearer <token>` or `X-Dalton-Bridge-Token` header; legacy
    `POST /mcp/<token>` path remains accepted during transition)

Pure Python 3.11+ standard library only.
"""

from __future__ import annotations

import asyncio
import csv
import hmac
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

# Ensure sibling `source_identity.py` is importable regardless of cwd
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import source_identity  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] researchdata_bridge: %(message)s",
)
logger = logging.getLogger("researchdata_bridge")

MAX_REQUEST_BYTES = 256 * 1024  # 256 KiB
MAX_EMBED_REQUEST_BYTES = 128 * 1024  # 128 KiB dedicated limit for /embed

DEFAULT_ROUTE_TIMEOUT_S = 240
LONG_READ_TIMEOUT_S = 600
FANOUT_ALPHAENGINE_TIMEOUT_S = 60
FANOUT_GUIDEPOINT_TIMEOUT_S = 90

LONG_READ_OPERATIONS: frozenset[str] = frozenset(
    {
        "read",
        "read-item",
        "read_item",
        "get_document",
        "get-document",
        "get_transcript",
        "get-transcript",
        "transcript",
        "read_filing",
        "read-filing",
        "search-text",
        "search_text",
        "attachments",
        "financials",
        "download_document",
        "download-document",
    }
)

# Route-level (default_s, max_s) timeout budgets aligned with P0-D.1 §274 & R3-2 (240s default, 600s full-text reads)
ROUTE_TIMEOUTS: dict[str, tuple[int, int]] = {
    "catalog": (15, 30),
    "company-wiki": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "company-wiki:read": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sales-notes": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "tmtb": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "funda-ai": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "all": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "alphaengine": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "alphaengine:get_document": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "alphaengine:download_document": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "guidepoint": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript:transcript": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript:get_transcript": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "transcript:read": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "sec:read-item": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:read_filing": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:search-text": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:attachments": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "sec:financials": (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S),
    "estimates": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "fred": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "treasury": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
    "market-regime": (15, 30),
    "cn-hk": (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S),
}

MARKET_REGIME_OPERATIONS: tuple[str, ...] = (
    "snapshot",
    "us_topbottom",
    "crowding",
    "cn_leverage",
)

CN_LEVERAGE_KEY_COLUMNS: tuple[str, ...] = (
    "date",
    "unwind_pressure_score",
    "unwind_direction",
    "regime",
    "gross_leverage_score",
    "neutral_crowding_score",
    "crowding_score",
)

_LOCAL_PATH_RE = re.compile(r"/Users/[^\s,;:\"')\]>]+")


def _scrub_local_paths(text: Any) -> str:
    """Replace host absolute paths starting with `/Users/` with `<local>`."""
    if text is None:
        return ""
    return _LOCAL_PATH_RE.sub("<local>", str(text)).strip()

MARKET_REGIME_CATALOG_ENTRY: dict[str, Any] = {
    "id": "market-regime",
    "category": "macro",
    "tool": "macro",
    "kind": "bridge_readonly",
    "name": "Market Regime & Crowding Monitor",
    "description": "大盘过热/超卖、顶部底部阶段、拥挤度、两融杠杆；回答大盘和仓位问题时先读。",
    "operations": list(MARKET_REGIME_OPERATIONS),
    "default_operation": "snapshot",
    "parameters": {
        "operation": {
            "type": "string",
            "enum": list(MARKET_REGIME_OPERATIONS),
            "default": "snapshot",
        },
        "top": {
            "type": "integer",
            "default": 10,
            "maximum": 30,
            "description": "crowding 返回条数（默认 10，最多 30）",
        },
        "order": {
            "type": "string",
            "enum": ["high", "low"],
            "default": "high",
            "description": "crowding 排序方向（high / low）",
        },
        "category": {
            "type": "string",
            "description": "crowding 按类别过滤（可选）",
        },
        "query": {
            "type": "string",
            "description": "crowding 按指数名称或代码过滤（可选）",
        },
    },
}


def _easter_sunday(year: int) -> date:
    """Compute Easter Sunday for a given Gregorian year (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th (1-based) occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of weekday (0=Mon..6=Sun) in (year, month)."""
    next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(d: date, allow_prior_year: bool = False) -> date | None:
    """NYSE weekend observance rule: Saturday -> Friday, Sunday -> Monday."""
    if d.weekday() == 5:
        obs = d - timedelta(days=1)
        if obs.year < d.year and not allow_prior_year:
            return None
        return obs
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _us_nyse_holidays(year: int) -> set[date]:
    """Return the set of NYSE full-day holidays for `year` using stdlib."""
    holidays: set[date] = set()
    nyd = _observed(date(year, 1, 1), allow_prior_year=False)
    if nyd:
        holidays.add(nyd)
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))
    holidays.add(_nth_weekday(year, 2, 0, 3))
    holidays.add(_easter_sunday(year) - timedelta(days=2))
    holidays.add(_last_weekday(year, 5, 0))
    if year >= 2022:
        jt = _observed(date(year, 6, 19))
        if jt:
            holidays.add(jt)
    ind = _observed(date(year, 7, 4))
    if ind:
        holidays.add(ind)
    holidays.add(_nth_weekday(year, 9, 0, 1))
    holidays.add(_nth_weekday(year, 11, 3, 4))
    xmas = _observed(date(year, 12, 25))
    if xmas:
        holidays.add(xmas)
    return holidays


def _cn_fixed_holidays(year: int) -> set[date]:
    """Return a Gregorian fixed-holiday subset approximation for A-shares (SSE/SZSE).

    Note: This is an approximation covering only fixed Gregorian holidays (Jan 1,
    May 1, Oct 1-3); it excludes lunar holidays (Spring Festival, Qingming, Dragon
    Boat, Mid-Autumn) and State Council lieu-day adjustments (调休/补假).
    """
    return {
        date(year, 1, 1),
        date(year, 5, 1),
        date(year, 10, 1),
        date(year, 10, 2),
        date(year, 10, 3),
    }


def _is_us_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _us_nyse_holidays(d.year)


def _is_cn_trading_day(d: date) -> bool:
    # Approximation: weekdays minus Gregorian fixed CN holidays (excludes lunar/调休).
    return d.weekday() < 5 and d not in _cn_fixed_holidays(d.year)


def _latest_us_trading_day(ref_date: date) -> date:
    """Return `ref_date` if it is an NYSE trading day, otherwise step back to the most recent NYSE trading day."""
    d = ref_date
    while not _is_us_trading_day(d):
        d -= timedelta(days=1)
    return d


def _latest_cn_trading_day(ref_date: date) -> date:
    """Return `ref_date` if it is an A-share trading day (approximate), otherwise step back."""
    d = ref_date
    while not _is_cn_trading_day(d):
        d -= timedelta(days=1)
    return d


def _prev_cn_trading_day(ref_date: date, steps: int = 1) -> date:
    """Return `steps` A-share trading days before `_latest_cn_trading_day(ref_date)`."""
    d = _latest_cn_trading_day(ref_date)
    for _ in range(max(0, steps)):
        d -= timedelta(days=1)
        while not _is_cn_trading_day(d):
            d -= timedelta(days=1)
    return d


def _parse_csv_scalar(val: Any) -> Any:
    """Convert numeric CSV cell strings to int/float while keeping text values intact."""
    if not isinstance(val, str):
        return val
    s = val.strip()
    if s == "":
        return None
    try:
        if "." not in s and "e" not in s.lower():
            return int(s)
        return float(s)
    except ValueError:
        return s



async def _reap_process(proc: Any) -> None:
    """Wait for a killed subprocess to exit even if this task is cancelled meanwhile.

    A cancel during the wait would otherwise leave the child unreaped; the
    cancellation is re-raised once the child is gone.
    """
    waiter = asyncio.ensure_future(proc.wait())
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        raise asyncio.CancelledError()


async def _cancel_and_reap(tasks: "tuple[asyncio.Task[Any], ...]") -> None:
    """Cancel unfinished fan-out tasks and wait for them, surviving a cancel of the caller."""
    pending = [t for t in tasks if not t.done()]
    for t in pending:
        t.cancel()
    if not pending:
        return
    gathered = asyncio.ensure_future(asyncio.gather(*pending, return_exceptions=True))
    cancelled = False
    while not gathered.done():
        try:
            await asyncio.shield(gathered)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()


class SubprocessExitError(RuntimeError):
    """Raised when researchdata_mcp exits without valid JSON-RPC output (never carries stderr)."""

    def __init__(self, exit_code: int | None) -> None:
        self.exit_code = exit_code
        super().__init__(f"researchdata_mcp exited without JSON response (exit_code={exit_code})")


def _read_env_file_dict(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from `path` without consulting `os.environ`."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k:
                out[k] = v
    except Exception as exc:
        logger.warning("Failed reading env file %s: %s", path, type(exc).__name__)
    return out


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE lines into `os.environ`.

    `GEMINI_API_KEY` is authoritative from `path` (never inherited from process environment),
    while non-secret `DALTON_BRIDGE_*` runtime knobs only populate `os.environ` if unset.
    """
    parsed = _read_env_file_dict(path)
    if "GEMINI_API_KEY" in parsed and parsed["GEMINI_API_KEY"].strip():
        os.environ["GEMINI_API_KEY"] = parsed["GEMINI_API_KEY"].strip()
    else:
        os.environ.pop("GEMINI_API_KEY", None)
    for k, v in parsed.items():
        if k == "GEMINI_API_KEY":
            continue
        if k not in os.environ:
            os.environ[k] = v


class BridgeConfig:
    def __init__(self) -> None:
        env_path = Path(
            os.environ.get(
                "DALTON_BRIDGE_ENV_FILE",
                os.path.expanduser("~/.config/dalton/researchdata-bridge.env"),
            )
        )
        self.env_path: Path = env_path
        _load_env_file(env_path)

        self.host: str = os.environ.get("DALTON_BRIDGE_HOST", "127.0.0.1")
        self.port: int = int(os.environ.get("DALTON_BRIDGE_PORT", "8960"))
        self.token: str = os.environ.get("DALTON_BRIDGE_TOKEN", "").strip()
        self.ocw_root: Path = Path(
            os.environ.get(
                "DALTON_OCW_ROOT",
                os.path.expanduser("~/.openclaw/workspace"),
            )
        ).resolve()
        self.python_bin: str = os.environ.get(
            "DALTON_OCW_PYTHON", "/opt/homebrew/bin/python3"
        )
        if not os.path.exists(self.python_bin):
            self.python_bin = sys.executable

        self.mcp_script: Path = Path(
            os.environ.get(
                "DALTON_MCP_SCRIPT",
                str(self.ocw_root / "scripts" / "researchdata_mcp.py"),
            )
        ).resolve()
        self.data_sources_json: Path = (
            self.ocw_root / "config" / "data_sources.json"
        )
        self.sentiment_signals_json: Path = Path(
            os.environ.get("DALTON_SENTIMENT_SIGNALS_JSON")
            or os.environ.get("DALTON_MARKET_REGIME_SIGNALS_JSON")
            or str(
                self.ocw_root
                / "research-outputs"
                / "sentiment-dashboard"
                / "signals.json"
            )
        )
        self.index_crowding_json: Path = Path(
            os.environ.get("DALTON_INDEX_CROWDING_JSON")
            or os.environ.get("DALTON_MARKET_REGIME_CROWDING_JSON")
            or str(
                self.ocw_root
                / "research-outputs"
                / "index-crowding"
                / "official-all.json"
            )
        )
        self.cn_leverage_status_json: Path = Path(
            os.environ.get("DALTON_CN_LEVERAGE_STATUS_JSON")
            or os.environ.get("DALTON_MARKET_REGIME_LEVERAGE_STATUS_JSON")
            or str(
                self.ocw_root
                / "state"
                / "cn-leverage"
                / "pipeline_status.json"
            )
        )
        self.cn_leverage_unwind_csv: Path = Path(
            os.environ.get("DALTON_CN_LEVERAGE_UNWIND_CSV")
            or os.environ.get("DALTON_MARKET_REGIME_UNWIND_CSV")
            or str(
                self.ocw_root
                / "state"
                / "cn-leverage"
                / "quant_unwind_signals.csv"
            )
        )
        self.max_inflight: int = max(
            1, int(os.environ.get("DALTON_BRIDGE_MAX_INFLIGHT", "2"))
        )
        self.embed_max_inflight: int = max(
            1, int(os.environ.get("DALTON_BRIDGE_EMBED_MAX_INFLIGHT", "2"))
        )
        self.embed_max_bytes: int = max(
            4096,
            int(os.environ.get("DALTON_BRIDGE_EMBED_MAX_BYTES", str(MAX_EMBED_REQUEST_BYTES))),
        )
        # Default 625s matches agent_config.dalton.yaml call_timeout_s=625 (625 - 25 = 600s long-read ceiling)
        self.caller_budget_s: int = max(
            35, int(os.environ.get("DALTON_BRIDGE_CALLER_BUDGET_S", "625"))
        )
        self.usage_file: Path = Path(
            os.environ.get(
                "DALTON_BRIDGE_USAGE_FILE",
                os.path.expanduser("~/.cache/dalton/bridge-usage.json"),
            )
        )
        self.test_mode: bool = os.environ.get("DALTON_BRIDGE_TEST_MODE", "0").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self.test_delay_s: float = 0.0
        if self.test_mode:
            try:
                self.test_delay_s = max(
                    0.0, float(os.environ.get("DALTON_BRIDGE_TEST_DELAY_S", "0") or 0.0)
                )
            except ValueError:
                self.test_delay_s = 0.0

    def resolve_test_delay_s(self) -> float:
        """Return active test delay in seconds (0.0 unless self.test_mode is True)."""
        if not self.test_mode:
            return 0.0
        if self.test_delay_s > 0.0:
            return self.test_delay_s
        try:
            return max(0.0, float(os.environ.get("DALTON_BRIDGE_TEST_DELAY_S", "0") or 0.0))
        except ValueError:
            return 0.0


RESEARCH_EVIDENCE_AND_ROUTE_GUIDE: str = (
    '各来源提供什么证据：事实 → filings/公司公告（dalton_sec / finance(route="sec") / get_sec_filing）；'
    "解读 → alphaengine 卖方研报与会议纪要；一线视角 → guidepoint 专家访谈；"
    "管理层原话 → transcript 业绩会转写稿；日常信息流 → sales-notes / tmtb / funda-ai；"
    "市场在反应什么 → X（x-api 技能）与网页新闻（WebSearch）；"
    "all 覆盖本地四库（company-wiki + sales-notes + tmtb + funda-ai）外加并行的 AlphaEngine 与 Guidepoint 检索（不含 transcript）。\n"
    "何时选用哪个路由（按研究情境，基于 catalog）：\n"
    "- 需要卖方观点、研报、会议纪要、目标价与预测变化 → alphaengine：卖方研报与会议纪要（来源：AlphaEngine，先 search_library 再 get_document）；\n"
    "- 需要行业一线的专业判断与经营反馈（供应链、渠道、客户、竞品、技术路线） → guidepoint：专家访谈与一线经营反馈（来源：Guidepoint）；\n"
    "- 需要管理层原话与业绩会口径 → transcript：业绩电话会转写稿（来源：AlphaEngine 业绩会 ASR 稿（主）/ roic.ai（兜底））；\n"
    "- 需要已入库的公司与行业历史沉淀资料 → company-wiki：已入库公司与行业资料（来源：本地 company-wiki，只读，params.company=TICKER）；\n"
    "- 需要交易台反馈、买方预期（bogey/whisper）、卖方销售邮件与市场摘要 → sales-notes：卖方销售邮件与市场摘要（来源：本地 market-digest 私有库）；\n"
    "- 需要 TMT 行业每日焦点与科技股卖方观点汇编 → tmtb：TMT Breakout 本地原文缓存（来源：AlphaEngine TMTB 已入库副本）；\n"
    "- 需要独立研究机构 FUNDA AI 的深度邮件与图表 → funda-ai：FUNDA AI 原邮件与图表（来源：fundaai@substack.com，MBOX 历史及 Gmail 增量，独立本地库）；\n"
    "- 不确定资料在哪个库，或需一次全面初筛 → all：内部研究资料跨库检索（本地库 company-wiki + sales-notes + tmtb + funda-ai，外加并行的 AlphaEngine 与 Guidepoint 检索；不含 transcript 电话会转写稿，需查电话会时单独调用 transcript）。"
)


def build_researchdata_plugin_instruction() -> str:
    """Build the canonical `researchdata` server instruction for `plugins/dalton_data/plugin.json`."""
    return (
        "Fund-internal sources; call from execute_code only (no direct path). "
        "Start with `catalog()` once per thread to see routes. "
        "Research: `research(route=..., operation=..., query=..., start=YYYY-MM-DD, end=..., limit=...)`. "
        f"{RESEARCH_EVIDENCE_AND_ROUTE_GUIDE.replace(chr(10), ' ')} "
        'Finance: `finance(route="sec"|"estimates"|"cn-hk")`. '
        'Macro: `macro(route="fred"|"treasury")`. '
        "Read only ids that search returned (`wiki:`, `mail:`, `tmtb:`, `funda:`, `alphaengine` doc_id). "
        "Page long text with params.offset/max_chars. "
        "Leave `timeout` unset: the bridge sets per-route limits and long reads may take minutes. "
        "AlphaEngine 检索可以放开，全文阅读（get_document）计配额，读最相关的几篇。"
        "Every result has `_sources` (identity, document_id, as_of, url). "
        "Cite with identity, publisher and date. Expert calls: identity, not name. "
        "Guidepoint verbatim <=20 words. "
        "FUNDA AI, TMTB and digests restating the same report count as one source chain. "
        "Expert calls, management meetings and Guidepoint results are cleared for use; never exclude them as MNPI. "
        "The fund wiki is read-only; never try to write to it. "
        '`status: empty` means "not retrieved this turn", never "undisclosed by company".'
    )


class ResearchDataBridge:
    RESEARCH_DESCRIPTION_SUFFIX: str = "\n\n" + RESEARCH_EVIDENCE_AND_ROUTE_GUIDE

    def __init__(self, cfg: BridgeConfig) -> None:
        self.cfg = cfg
        self.sem = asyncio.Semaphore(cfg.max_inflight)
        self.embed_sem = asyncio.Semaphore(cfg.embed_max_inflight)
        self.server_version: str = "unknown"
        self._cached_tools: list[dict[str, Any]] | None = None
        self._cached_version: str | None = None
        self._usage_lock = asyncio.Lock()
        self._resolved_embed_model: str | None = None
        self._resolved_embed_dims: int | None = None
        self._file_cache: dict[str, tuple[int, int, Any]] = {}

    def handoff_routes(self) -> set[str]:
        """Return route IDs whose `kind` is `agent_tool` or `skill` in `data_sources.json`."""
        try:
            raw = json.loads(
                self.cfg.data_sources_json.read_text(encoding="utf-8")
            )
            routes = raw.get("routes") if isinstance(raw, dict) else []
            if not isinstance(routes, list):
                return set()
            return {
                str(r["id"])
                for r in routes
                if isinstance(r, dict)
                and r.get("id")
                and r.get("kind") in ("agent_tool", "skill")
            }
        except Exception as exc:
            logger.warning("Failed to load data_sources.json: %s", type(exc).__name__)
            return {
                "web-news",
                "web-page",
                "filings",
                "last30days",
                "agent-reach",
                "x-timeline",
                "x-search",
                "xueqiu",
                "employee",
            }

    def compute_timeout(self, tool_name: str, arguments: dict[str, Any]) -> int:
        """Compute clamped timeout in seconds for a tool call."""
        ceiling = max(10, self.cfg.caller_budget_s - 25)
        if tool_name == "catalog":
            default_s, max_s = ROUTE_TIMEOUTS["catalog"]
        else:
            route = str(arguments.get("route") or "").strip()
            op = str(arguments.get("operation") or arguments.get("action") or "").strip()
            key = f"{route}:{op}"
            if key in ROUTE_TIMEOUTS:
                default_s, max_s = ROUTE_TIMEOUTS[key]
            elif op in LONG_READ_OPERATIONS:
                default_s, max_s = (LONG_READ_TIMEOUT_S, LONG_READ_TIMEOUT_S)
            else:
                default_s, max_s = ROUTE_TIMEOUTS.get(
                    route, (DEFAULT_ROUTE_TIMEOUT_S, DEFAULT_ROUTE_TIMEOUT_S)
                )

        req_timeout = arguments.get("timeout")
        if isinstance(req_timeout, int) and req_timeout > 0:
            chosen = min(req_timeout, max_s)
        else:
            chosen = default_s
        return max(5, min(chosen, max_s, ceiling))

    async def _run_stdio_rpc(
        self, requests: list[dict[str, Any]], timeout_s: float
    ) -> list[dict[str, Any]]:
        """Spawn a single `researchdata_mcp.py` subprocess, send JSON-RPC lines, and read responses."""
        proc = await asyncio.create_subprocess_exec(
            self.cfg.python_bin,
            str(self.cfg.mcp_script),
            cwd=str(self.cfg.ocw_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            payload_bytes = (
                "\n".join(json.dumps(r, ensure_ascii=False) for r in requests)
                + "\n"
            ).encode("utf-8")
            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=payload_bytes),
                timeout=timeout_s,
            )
        except BaseException:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await _reap_process(proc)
            raise

        responses: list[dict[str, Any]] = []
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    responses.append(parsed)
            except Exception:
                continue
        if not responses:
            # R3-1: NEVER decode or log stderr content; log/report only exception class and exit code
            logger.warning(
                "researchdata_mcp subprocess returned no JSON response (exit_code=%s)",
                proc.returncode,
            )
            raise SubprocessExitError(proc.returncode)
        return responses

    async def ensure_initialized(self) -> dict[str, Any]:
        """Run `initialize` against `researchdata_mcp.py` and track `server_version`."""
        reqs = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "dalton-researchdata-bridge", "version": "0.1.0"},
                },
            }
        ]
        resps = await self._run_stdio_rpc(reqs, timeout_s=15.0)
        init_res = (resps[0].get("result") or {}) if resps else {}
        srv_info = init_res.get("serverInfo") if isinstance(init_res, dict) else {}
        ver = str((srv_info or {}).get("version") or "unknown")
        if self._cached_version and self._cached_version != ver:
            logger.info(
                "researchdata server_version changed (%s -> %s); invalidating tools/list cache",
                self._cached_version,
                ver,
            )
            self._cached_tools = None
        self.server_version = ver
        self._cached_version = ver
        return init_res

    async def get_filtered_tools(self) -> list[dict[str, Any]]:
        """Fetch `tools/list` from `researchdata_mcp.py` and strip handoff routes."""
        await self.ensure_initialized()
        if self._cached_tools is not None:
            return self._cached_tools

        reqs = [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
        resps = await self._run_stdio_rpc(reqs, timeout_s=15.0)
        raw_tools = (
            ((resps[0].get("result") or {}).get("tools") or []) if resps else []
        )
        handoff = self.handoff_routes()
        filtered: list[dict[str, Any]] = []
        for tool in raw_tools:
            if not isinstance(tool, dict):
                continue
            tool_copy = json.loads(json.dumps(tool))
            props = (
                tool_copy.get("inputSchema", {}).get("properties", {})
                if isinstance(tool_copy.get("inputSchema"), dict)
                else {}
            )
            route_prop = props.get("route")
            if isinstance(route_prop, dict) and isinstance(route_prop.get("enum"), list):
                route_prop["enum"] = [
                    r for r in route_prop["enum"] if r not in handoff
                ]
            if tool_copy.get("name") == "research":
                desc = str(tool_copy.get("description") or "").rstrip()
                if self.RESEARCH_DESCRIPTION_SUFFIX.strip() not in desc:
                    tool_copy["description"] = desc + self.RESEARCH_DESCRIPTION_SUFFIX
            elif tool_copy.get("name") == "macro":
                if isinstance(route_prop, dict) and isinstance(route_prop.get("enum"), list):
                    if "market-regime" not in route_prop["enum"]:
                        route_prop["enum"].append("market-regime")
                if isinstance(props, dict):
                    op_prop = props.get("operation")
                    if isinstance(op_prop, dict):
                        op_desc = str(op_prop.get("description") or "").rstrip()
                        if "market-regime" not in op_desc:
                            op_prop["description"] = (
                                f"{op_desc} For market-regime: snapshot (default), us_topbottom, crowding, cn_leverage."
                            ).strip()
                    else:
                        props["operation"] = {
                            "type": "string",
                            "description": "Operation name (for market-regime: snapshot, us_topbottom, crowding, cn_leverage).",
                        }
                    props.setdefault(
                        "top",
                        {
                            "type": "integer",
                            "default": 10,
                            "maximum": 30,
                            "description": "For market-regime crowding: number of rows to return (default 10, max 30).",
                        },
                    )
                    props.setdefault(
                        "order",
                        {
                            "type": "string",
                            "enum": ["high", "low"],
                            "default": "high",
                            "description": "For market-regime crowding: sort order by crowding_percentile (high or low).",
                        },
                    )
                    props.setdefault(
                        "category",
                        {
                            "type": "string",
                            "description": "For market-regime crowding: optional category filter.",
                        },
                    )
                    props.setdefault(
                        "query",
                        {
                            "type": "string",
                            "description": "For market-regime crowding: optional filter by index name or code.",
                        },
                    )
                m_desc = str(tool_copy.get("description") or "").rstrip()
                if "market-regime" not in m_desc:
                    tool_copy["description"] = (
                        f"{m_desc} Market regime & crowding: route='market-regime' (operations: snapshot, us_topbottom, crowding, cn_leverage)."
                    ).strip()
            filtered.append(tool_copy)

        self._cached_tools = filtered
        return filtered

    def _should_fanout_alphaengine(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> bool:
        """Return True when a `research(route='all')` call is a search that should fan out to AlphaEngine and Guidepoint."""
        if tool_name != "research":
            return False
        if str(arguments.get("route") or "").strip() != "all":
            return False
        op = str(arguments.get("operation") or "").strip()
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if op == "search":
            return bool(query or ticker)
        if not op:
            return bool(query)
        return False

    def _compute_fanout_timeout(self, arguments: dict[str, Any], default_cap_s: int) -> int:
        """Compute clamped fanout timeout capped at `default_cap_s` (60s for AlphaEngine, 90s for Guidepoint)."""
        ceiling = max(10, self.cfg.caller_budget_s - 25)
        req_timeout = arguments.get("timeout")
        if isinstance(req_timeout, int) and not isinstance(req_timeout, bool) and req_timeout > 0:
            chosen = min(req_timeout, default_cap_s)
        else:
            chosen = default_cap_s
        return max(5, min(chosen, default_cap_s, ceiling))

    def _build_alphaengine_fanout_args(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Map `research(route='all')` search arguments to `route='alphaengine'` parameters.

        Mapping basis:
        - `data_gateway.py` sets `accepted['alphaengine'] = {'query', 'start', 'end'}` and rejects
          `--ticker` (and `series`/`concept`/`item`/`document`/`form`/`dataset`/`as_of`/`vintage`)
          with ValueError; if `ticker` was provided on `all`, fold it into `query`.
        - `data_gateway.py` and `config/data_sources.json` restrict `alphaengine` operations to
          `['search_library', 'get_document', 'auth_status', 'list_filters']`, so `operation` is
          mapped to `'search_library'`.
        - `alphaengine-mcp/server.mjs` (`requireDatePair`) requires `start` (`date_from`) and `end`
          (`date_to`) as a pair; if `start` is given without `end`, default `end` to today's UTC
          date, and omit a lone `end` without `start`.
        - Local-library `params` keys (`sources`, `layer`, `search_mode`, etc.) are omitted because
          `search_library` sets `additionalProperties: false`.
        - Timeout is capped at `FANOUT_ALPHAENGINE_TIMEOUT_S` (60s).
        """
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if ticker and ticker.lower() not in query.lower():
            query = f"{ticker} {query}".strip() if query else ticker

        ae_args: dict[str, Any] = {
            "route": "alphaengine",
            "operation": "search_library",
            "query": query,
        }
        start = str(arguments.get("start") or "").strip()
        end = str(arguments.get("end") or "").strip()
        if start and end:
            ae_args["start"] = start
            ae_args["end"] = end
        elif start and not end:
            ae_args["start"] = start
            ae_args["end"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        limit = arguments.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            ae_args["limit"] = min(limit, 100)

        ae_args["timeout"] = self._compute_fanout_timeout(
            arguments, FANOUT_ALPHAENGINE_TIMEOUT_S
        )
        return ae_args

    def _build_guidepoint_fanout_args(
        self, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Map `research(route='all')` search arguments to `route='guidepoint'` parameters.

        Mapping basis:
        - `catalog()` (`config/data_sources.json`) registers `guidepoint` with
          `operations: ['search_library']` (forwarded to `mcp__guidepoint__search_library`).
        - `researchdata_mcp.py` (`call_tool`) forwards top-level string fields + `limit` + `timeout`
          + `params` to `data_gateway.py fetch guidepoint`.
        - `data_gateway.py` (`build_command`) sets `accepted['guidepoint'] = {'query', 'start', 'end'}`
          and raises ValueError if `--ticker` (or `series`/`concept`/`item`/`document`/`form`/
          `dataset`/`as_of`/`vintage`) is passed; if `ticker` was provided on `all`, fold it into `query`.
        - `data_gateway.py` (`native_request`) maps `start` -> `start_date`, `end` -> `end_date`,
          and `limit` -> `size` (clamped to `min(limit, 40)`).
        - Local-library `params` keys (`sources`, `layer`, `media`, `search_mode`, `company`,
          `sector`, `tag`, `indexed_since`, `sender`, `no_decay`, `offset`, `max_chars`) are omitted;
          only Guidepoint-native optional keys (`keywords`, `recency_bias`) are forwarded if present.
        - Timeout is capped at `FANOUT_GUIDEPOINT_TIMEOUT_S` (90s).
        """
        query = str(arguments.get("query") or "").strip()
        ticker = str(arguments.get("ticker") or "").strip()
        if ticker and ticker.lower() not in query.lower():
            query = f"{ticker} {query}".strip() if query else ticker

        gp_args: dict[str, Any] = {
            "route": "guidepoint",
            "operation": "search_library",
            "query": query,
        }
        start = str(arguments.get("start") or "").strip()
        end = str(arguments.get("end") or "").strip()
        if start and end and start <= end:
            gp_args["start"] = start
            gp_args["end"] = end
        elif start and not end:
            gp_args["start"] = start
        elif end and not start:
            gp_args["end"] = end

        limit = arguments.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            gp_args["limit"] = min(limit, 40)

        raw_params = arguments.get("params")
        if isinstance(raw_params, dict):
            gp_params: dict[str, Any] = {}
            if isinstance(raw_params.get("keywords"), list):
                kw = [str(k).strip() for k in raw_params["keywords"] if str(k).strip()]
                if kw:
                    gp_params["keywords"] = kw
            if isinstance(raw_params.get("recency_bias"), str) and raw_params["recency_bias"].strip():
                gp_params["recency_bias"] = raw_params["recency_bias"].strip()
            if gp_params:
                gp_args["params"] = gp_params

        gp_args["timeout"] = self._compute_fanout_timeout(
            arguments, FANOUT_GUIDEPOINT_TIMEOUT_S
        )
        return gp_args

    async def _run_alphaengine_fanout(
        self, req_id: Any, ae_args: dict[str, Any], ae_budget_s: float
    ) -> dict[str, Any]:
        """Run the parallel `route='alphaengine'` search and normalize its result or failure state."""
        ae_timeout = int(ae_args.get("timeout") or FANOUT_ALPHAENGINE_TIMEOUT_S)
        ae_rpc_req = {
            "jsonrpc": "2.0",
            "id": f"{req_id}:alphaengine" if req_id is not None else "1:alphaengine",
            "method": "tools/call",
            "params": {"name": "research", "arguments": ae_args},
        }
        try:
            ae_resps = await self._run_stdio_rpc(
                [ae_rpc_req], timeout_s=ae_budget_s
            )
        except asyncio.TimeoutError:
            reason = f"alphaengine timed out after {ae_timeout}s"
            logger.warning("alphaengine fanout timeout timeout_s=%d", ae_timeout)
            return {
                "ok": False,
                "status": "timeout",
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except SubprocessExitError as exc:
            reason = f"SubprocessExitError(exit_code={exc.exit_code})"
            logger.warning("alphaengine fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning("alphaengine fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        if not ae_resps or not isinstance(ae_resps[0], dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": "Empty response from researchdata_mcp",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        ae_resp = ae_resps[0]
        if isinstance(ae_resp.get("error"), dict):
            err_msg = str(ae_resp["error"].get("message") or "RPC error")[:200]
            status = "timeout" if "timeout" in err_msg.lower() or "timed out" in err_msg.lower() else "failed"
            return {
                "ok": False,
                "status": status,
                "route": "alphaengine",
                "reason": err_msg,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        res_obj = ae_resp.get("result")
        ae_payload: dict[str, Any] | None = None
        if isinstance(res_obj, dict):
            blocks = res_obj.get("content")
            if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    parsed = json.loads(blocks[0]["text"])
                    if isinstance(parsed, dict):
                        ae_payload = parsed
                except Exception:
                    ae_payload = None
            if ae_payload is None and isinstance(res_obj.get("structuredContent"), dict):
                ae_payload = res_obj["structuredContent"]

        if not isinstance(ae_payload, dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "alphaengine",
                "reason": "Invalid JSON payload from alphaengine",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        inner = ae_payload.get("data") if isinstance(ae_payload.get("data"), dict) else ae_payload
        is_err = (
            bool(isinstance(res_obj, dict) and res_obj.get("isError"))
            or ae_payload.get("ok") is False
            or bool(isinstance(inner, dict) and inner.get("isError"))
        )
        if is_err:
            err_text = str(
                ae_payload.get("error")
                or ae_payload.get("reason")
                or ""
            ).strip()
            if not err_text and isinstance(inner, dict) and isinstance(inner.get("content"), list):
                c_blocks = inner["content"]
                if c_blocks and isinstance(c_blocks[0], dict):
                    err_text = str(c_blocks[0].get("text") or "").strip()
            reason = (err_text or "alphaengine call returned error")[:200]
            raw_status = str(ae_payload.get("status") or "").strip().lower()
            status = (
                "timeout"
                if raw_status == "timeout" or "timeout" in reason.lower() or "timed out" in reason.lower()
                else "failed"
            )
            return {
                "ok": False,
                "status": status,
                "route": "alphaengine",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": ae_payload,
            }

        sc = (
            source_identity._unwrap_mcp_proxy_payload(inner)
            if isinstance(inner, dict)
            else source_identity._unwrap_mcp_proxy_payload(ae_payload)
        )
        raw_items = (
            sc.get("results")
            if isinstance(sc, dict) and isinstance(sc.get("results"), list)
            else None
        )
        if not isinstance(raw_items, list):
            top_sc = source_identity._unwrap_mcp_proxy_payload(ae_payload)
            raw_items = (
                top_sc.get("results")
                if isinstance(top_sc, dict) and isinstance(top_sc.get("results"), list)
                else []
            )

        ae_items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            norm = dict(item)
            doc_id = str(
                norm.get("doc_id")
                or norm.get("id")
                or norm.get("document_id")
                or ""
            ).strip()
            norm["route"] = "alphaengine"
            norm.setdefault("source", "alphaengine")
            if doc_id:
                full_doc_id = (
                    doc_id
                    if doc_id.startswith("alphaengine:")
                    else f"alphaengine:{doc_id}"
                )
                norm.setdefault("document_id", full_doc_id)
                norm.setdefault("canonical_id", full_doc_id)
            if norm.get("publish_time") and not norm.get("published_at"):
                norm["published_at"] = norm["publish_time"]
            ae_items.append(norm)

        return {
            "ok": True,
            "status": "ok",
            "route": "alphaengine",
            "reason": None,
            "count": len(ae_items),
            "results": ae_items,
            "raw_payload": ae_payload,
        }

    async def _run_guidepoint_fanout(
        self, req_id: Any, gp_args: dict[str, Any], gp_budget_s: float
    ) -> dict[str, Any]:
        """Run the parallel `route='guidepoint'` search and normalize its result or failure state."""
        gp_timeout = int(gp_args.get("timeout") or FANOUT_GUIDEPOINT_TIMEOUT_S)
        gp_rpc_req = {
            "jsonrpc": "2.0",
            "id": f"{req_id}:guidepoint" if req_id is not None else "1:guidepoint",
            "method": "tools/call",
            "params": {"name": "research", "arguments": gp_args},
        }
        try:
            gp_resps = await self._run_stdio_rpc(
                [gp_rpc_req], timeout_s=gp_budget_s
            )
        except asyncio.TimeoutError:
            reason = f"guidepoint timed out after {gp_timeout}s"
            logger.warning("guidepoint fanout timeout timeout_s=%d", gp_timeout)
            return {
                "ok": False,
                "status": "timeout",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except SubprocessExitError as exc:
            reason = f"SubprocessExitError(exit_code={exc.exit_code})"
            logger.warning("guidepoint fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }
        except Exception as exc:
            reason = type(exc).__name__
            logger.warning("guidepoint fanout failed: %s", reason)
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        if not gp_resps or not isinstance(gp_resps[0], dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": "Empty response from researchdata_mcp",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        gp_resp = gp_resps[0]
        if isinstance(gp_resp.get("error"), dict):
            err_msg = str(gp_resp["error"].get("message") or "RPC error")[:200]
            status = "timeout" if "timeout" in err_msg.lower() or "timed out" in err_msg.lower() else "failed"
            return {
                "ok": False,
                "status": status,
                "route": "guidepoint",
                "reason": err_msg,
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        res_obj = gp_resp.get("result")
        gp_payload: dict[str, Any] | None = None
        if isinstance(res_obj, dict):
            blocks = res_obj.get("content")
            if isinstance(blocks, list) and blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    parsed = json.loads(blocks[0]["text"])
                    if isinstance(parsed, dict):
                        gp_payload = parsed
                except Exception:
                    gp_payload = None
            if gp_payload is None and isinstance(res_obj.get("structuredContent"), dict):
                gp_payload = res_obj["structuredContent"]

        if not isinstance(gp_payload, dict):
            return {
                "ok": False,
                "status": "failed",
                "route": "guidepoint",
                "reason": "Invalid JSON payload from guidepoint",
                "count": 0,
                "results": [],
                "raw_payload": None,
            }

        inner = gp_payload.get("data") if isinstance(gp_payload.get("data"), dict) else gp_payload
        is_err = (
            bool(isinstance(res_obj, dict) and res_obj.get("isError"))
            or gp_payload.get("ok") is False
            or bool(isinstance(inner, dict) and inner.get("isError"))
        )
        if is_err:
            err_text = str(
                gp_payload.get("error")
                or gp_payload.get("reason")
                or ""
            ).strip()
            if not err_text and isinstance(inner, dict) and isinstance(inner.get("content"), list):
                c_blocks = inner["content"]
                if c_blocks and isinstance(c_blocks[0], dict):
                    err_text = str(c_blocks[0].get("text") or "").strip()
            reason = (err_text or "guidepoint call returned error")[:200]
            raw_status = str(gp_payload.get("status") or "").strip().lower()
            status = (
                "timeout"
                if raw_status == "timeout" or "timeout" in reason.lower() or "timed out" in reason.lower()
                else "failed"
            )
            return {
                "ok": False,
                "status": status,
                "route": "guidepoint",
                "reason": reason,
                "count": 0,
                "results": [],
                "raw_payload": gp_payload,
            }

        sc = source_identity._unwrap_mcp_proxy_payload(inner) if isinstance(inner, dict) else gp_payload
        raw_items = sc.get("data") if isinstance(sc, dict) and isinstance(sc.get("data"), list) else None
        if not isinstance(raw_items, list) and isinstance(sc, dict):
            raw_items = sc.get("results") if isinstance(sc.get("results"), list) else None
        if not isinstance(raw_items, list):
            raw_items = (
                gp_payload.get("data")
                if isinstance(gp_payload.get("data"), list)
                else gp_payload.get("results")
                if isinstance(gp_payload.get("results"), list)
                else []
            )

        gp_items: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            norm = dict(item)
            raw_id = str(
                norm.get("id")
                or norm.get("transcript_id")
                or norm.get("document_id")
                or ""
            ).strip()
            m = source_identity._GP_ID_RE.search(raw_id)
            base_id = m.group(1) if m else raw_id
            norm["route"] = "guidepoint"
            norm.setdefault("source", "guidepoint")
            if base_id:
                full_doc_id = (
                    base_id
                    if base_id.startswith("guidepoint:")
                    else f"guidepoint:{base_id}"
                )
                norm.setdefault("document_id", full_doc_id)
                norm.setdefault("canonical_id", full_doc_id)
            if norm.get("transcript_name") and not norm.get("title"):
                norm["title"] = norm["transcript_name"]
            if norm.get("date") and not norm.get("published_at"):
                norm["published_at"] = norm["date"]
            if isinstance(norm.get("respondent"), dict) and "full_name" in norm["respondent"]:
                norm["respondent"] = {
                    k: v for k, v in norm["respondent"].items() if k != "full_name"
                }
            gp_items.append(norm)

        return {
            "ok": True,
            "status": "ok",
            "route": "guidepoint",
            "reason": None,
            "count": len(gp_items),
            "results": gp_items,
            "raw_payload": gp_payload,
        }

    def _merge_alphaengine_into_all(
        self,
        payload: dict[str, Any],
        declared_local: list[dict[str, Any]],
        ae_outcome: dict[str, Any],
        ae_args: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Merge AlphaEngine fanout results/status into `route='all'` payload and declared sources."""
        if ae_outcome.get("ok"):
            ae_items = list(ae_outcome.get("results") or [])
            ae_section: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "route": "alphaengine",
                "count": len(ae_items),
                "results": ae_items,
            }
        else:
            st = str(ae_outcome.get("status") or "failed")
            reason = str(ae_outcome.get("reason") or st)
            ae_items = []
            ae_section = {
                "ok": False,
                "status": st,
                "route": "alphaengine",
                "reason": reason,
                "error": reason,
                "count": 0,
                "results": [],
            }

        payload["alphaengine"] = ae_section
        if isinstance(payload.get("source_status"), dict):
            payload["source_status"]["alphaengine"] = {
                k: v for k, v in ae_section.items() if k != "results"
            }

        if ae_items and isinstance(payload.get("results"), list):
            existing_ids = {
                str(r.get("canonical_id") or r.get("document_id") or "")
                for r in payload["results"]
                if isinstance(r, dict)
            }
            existing_ids.discard("")
            for item in ae_items:
                cid = str(item.get("canonical_id") or item.get("document_id") or "")
                if cid and cid in existing_ids:
                    continue
                if cid:
                    existing_ids.add(cid)
                payload["results"].append(item)
            if "result_count" in payload:
                payload["result_count"] = len(payload["results"])
            if payload.get("ok") is True and payload.get("status") == "empty":
                payload["status"] = "ok"

        merged_declared: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        for src in declared_local:
            if not isinstance(src, dict):
                continue
            doc_id = str(src.get("document_id") or "")
            if doc_id:
                seen_doc_ids.add(doc_id)
            merged_declared.append(src)

        raw_ae = ae_outcome.get("raw_payload")
        if ae_outcome.get("ok") and isinstance(raw_ae, dict):
            declared_ae = source_identity.declare("research", ae_args, raw_ae)
            for src in declared_ae:
                if not isinstance(src, dict):
                    continue
                src_copy = dict(src)
                src_copy["route"] = "alphaengine"
                doc_id = str(src_copy.get("document_id") or "")
                if doc_id and doc_id in seen_doc_ids:
                    continue
                if doc_id:
                    seen_doc_ids.add(doc_id)
                merged_declared.append(src_copy)
                if len(merged_declared) >= source_identity.MAX_SOURCES_PER_CALL:
                    break

        return merged_declared[: source_identity.MAX_SOURCES_PER_CALL]

    def _merge_guidepoint_into_all(
        self,
        payload: dict[str, Any],
        declared_so_far: list[dict[str, Any]],
        gp_outcome: dict[str, Any],
        gp_args: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Merge Guidepoint fanout results/status into `route='all'` payload and declared sources."""
        if gp_outcome.get("ok"):
            gp_items = list(gp_outcome.get("results") or [])
            gp_section: dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "route": "guidepoint",
                "count": len(gp_items),
                "results": gp_items,
            }
        else:
            st = str(gp_outcome.get("status") or "failed")
            reason = str(gp_outcome.get("reason") or st)
            gp_items = []
            gp_section = {
                "ok": False,
                "status": st,
                "route": "guidepoint",
                "reason": reason,
                "error": reason,
                "count": 0,
                "results": [],
            }

        payload["guidepoint"] = gp_section
        if isinstance(payload.get("source_status"), dict):
            payload["source_status"]["guidepoint"] = {
                k: v for k, v in gp_section.items() if k != "results"
            }

        if gp_items and isinstance(payload.get("results"), list):
            existing_ids = {
                str(r.get("id") or r.get("canonical_id") or r.get("document_id") or "")
                for r in payload["results"]
                if isinstance(r, dict)
            }
            existing_ids.discard("")
            for item in gp_items:
                cid = str(item.get("id") or item.get("canonical_id") or item.get("document_id") or "")
                if cid and cid in existing_ids:
                    continue
                if cid:
                    existing_ids.add(cid)
                payload["results"].append(item)
            if "result_count" in payload:
                payload["result_count"] = len(payload["results"])
            if payload.get("ok") is True and payload.get("status") == "empty":
                payload["status"] = "ok"

        merged_declared: list[dict[str, Any]] = []
        seen_doc_ids: set[str] = set()
        for src in declared_so_far:
            if not isinstance(src, dict):
                continue
            doc_id = str(src.get("document_id") or "")
            if doc_id:
                seen_doc_ids.add(doc_id)
            merged_declared.append(src)

        raw_gp = gp_outcome.get("raw_payload")
        if gp_outcome.get("ok") and isinstance(raw_gp, dict):
            declared_gp = source_identity.declare("research", gp_args, raw_gp)
            for src in declared_gp:
                if not isinstance(src, dict):
                    continue
                src_copy = dict(src)
                src_copy["route"] = "guidepoint"
                doc_id = str(src_copy.get("document_id") or "")
                if doc_id and doc_id in seen_doc_ids:
                    continue
                if doc_id:
                    seen_doc_ids.add(doc_id)
                merged_declared.append(src_copy)
                if len(merged_declared) >= source_identity.MAX_SOURCES_PER_CALL:
                    break

        return merged_declared[: source_identity.MAX_SOURCES_PER_CALL]

    def _read_usage_today(self) -> dict[str, int]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            if self.cfg.usage_file.is_file():
                data = json.loads(self.cfg.usage_file.read_text(encoding="utf-8"))
                day_entry = data.get(today) if isinstance(data, dict) else None
                if isinstance(day_entry, dict):
                    return {
                        "calls": int(day_entry.get("calls", 0)),
                        "chars": int(day_entry.get("chars", 0)),
                    }
        except Exception:
            pass
        return {"calls": 0, "chars": 0}

    async def _record_alphaengine_get_doc(self, chars: int) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        async with self._usage_lock:
            try:
                self.cfg.usage_file.parent.mkdir(parents=True, exist_ok=True)
                data: dict[str, Any] = {}
                if self.cfg.usage_file.is_file():
                    try:
                        loaded = json.loads(
                            self.cfg.usage_file.read_text(encoding="utf-8")
                        )
                        if isinstance(loaded, dict):
                            data = loaded
                    except Exception:
                        data = {}
                day_entry = data.get(today)
                if not isinstance(day_entry, dict):
                    day_entry = {"calls": 0, "chars": 0}
                day_entry["calls"] = int(day_entry.get("calls", 0)) + 1
                day_entry["chars"] = int(day_entry.get("chars", 0)) + max(0, int(chars))
                data[today] = day_entry
                tmp_path = self.cfg.usage_file.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp_path.replace(self.cfg.usage_file)
            except Exception as exc:
                logger.warning("Failed to record alphaengine usage: %s", type(exc).__name__)

    def _reference_date(self) -> date:
        """Return the current `America/New_York` calendar date for market-regime freshness checks."""
        return datetime.now(ZoneInfo("America/New_York")).date()

    def _read_cached_json_sync(
        self, path: Path, *, strip_crowding_history: bool = False
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """Read and parse a JSON object file synchronously with in-process `(mtime_ns, size)` caching."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except OSError as exc:
            return ("source_missing", None, f"Cannot access {path.name}: {type(exc).__name__}")
        if not path.is_file():
            return ("source_missing", None, f"Not a regular file: {path.name}")

        cache_key = f"json:{int(strip_crowding_history)}:{path}"
        cached = self._file_cache.get(cache_key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return ("ok", cached[2], None)

        try:
            raw_text = path.read_text(encoding="utf-8")
            parsed = json.loads(raw_text)
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except Exception as exc:
            return ("invalid_source", None, f"Failed to parse JSON in {path.name}: {type(exc).__name__}")

        if not isinstance(parsed, dict):
            return ("invalid_source", None, f"Expected JSON object in {path.name}")

        if strip_crowding_history and isinstance(parsed.get("rows"), list):
            parsed = dict(parsed)
            parsed["rows"] = [
                {k: v for k, v in r.items() if k != "history_1y"}
                for r in parsed["rows"]
                if isinstance(r, dict)
            ]

        self._file_cache[cache_key] = (st.st_mtime_ns, st.st_size, parsed)
        return ("ok", parsed, None)

    def _read_cached_csv_sync(
        self, path: Path
    ) -> tuple[str, list[dict[str, str]] | None, str | None]:
        """Read and parse a CSV file synchronously with in-process `(mtime_ns, size)` caching."""
        try:
            st = path.stat()
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except OSError as exc:
            return ("source_missing", None, f"Cannot access {path.name}: {type(exc).__name__}")
        if not path.is_file():
            return ("source_missing", None, f"Not a regular file: {path.name}")

        cache_key = f"csv:{path}"
        cached = self._file_cache.get(cache_key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            return ("ok", cached[2], None)

        try:
            raw_text = path.read_text(encoding="utf-8")
            lines = [ln for ln in raw_text.splitlines() if ln.strip()]
            if not lines:
                return ("invalid_source", None, f"Empty CSV in {path.name}")
            reader = csv.DictReader(lines)
            if not reader.fieldnames or "date" not in reader.fieldnames:
                return ("invalid_source", None, f"Invalid CSV header in {path.name}")
            rows: list[dict[str, str]] = []
            for r in reader:
                if not isinstance(r, dict) or None in r:
                    return ("invalid_source", None, f"Malformed CSV row in {path.name}")
                if not str(r.get("date") or "").strip():
                    return ("invalid_source", None, f"Missing date value in {path.name}")
                rows.append({str(k): str(v or "") for k, v in r.items() if k is not None})
        except FileNotFoundError:
            return ("source_missing", None, f"Source file not found: {path.name}")
        except Exception as exc:
            return ("invalid_source", None, f"Failed to parse CSV in {path.name}: {type(exc).__name__}")

        if not rows:
            return ("invalid_source", None, f"No data rows in {path.name}")

        self._file_cache[cache_key] = (st.st_mtime_ns, st.st_size, rows)
        return ("ok", rows, None)

    def _build_market_regime_sync(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Build the `macro(route='market-regime')` response synchronously (called via `asyncio.to_thread`)."""
        raw_params = arguments.get("params")
        params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
        op = (
            str(arguments.get("operation") or params.get("operation") or "snapshot")
            .strip()
            .lower()
            or "snapshot"
        )
        if op not in MARKET_REGIME_OPERATIONS:
            return {
                "ok": False,
                "status": "invalid_source",
                "route": "market-regime",
                "operation": op,
                "error": f"Unsupported market-regime operation '{op}' (expected one of {list(MARKET_REGIME_OPERATIONS)})",
            }

        ref_date = self._reference_date()
        latest_us_td = _latest_us_trading_day(ref_date).isoformat()
        latest_cn_td_m1 = _prev_cn_trading_day(ref_date, 1).isoformat()

        if op in ("snapshot", "us_topbottom"):
            status, data, err = self._read_cached_json_sync(self.cfg.sentiment_signals_json)
            if status != "ok" or data is None:
                return {
                    "ok": False,
                    "status": status,
                    "route": "market-regime",
                    "operation": op,
                    "error": err or status,
                }
            if data.get("ok") is False:
                return {
                    "ok": False,
                    "status": "invalid_source",
                    "route": "market-regime",
                    "operation": op,
                    "error": "signals.json reported ok=false",
                }
            us_market = data.get("us_market")
            topbottom = us_market.get("topbottom") if isinstance(us_market, dict) else None
            top_fw = topbottom.get("top_framework") if isinstance(topbottom, dict) else None
            bot_fw = topbottom.get("bottom_framework") if isinstance(topbottom, dict) else None
            if not isinstance(top_fw, dict) or not isinstance(bot_fw, dict):
                return {
                    "ok": False,
                    "status": "invalid_source",
                    "route": "market-regime",
                    "operation": op,
                    "error": "Malformed signals.json: missing us_market.topbottom frameworks",
                }

            sig_date = str(data.get("date") or "").strip()[:10]
            raw_as_of = data.get("as_of")
            as_of_dict = dict(raw_as_of) if isinstance(raw_as_of, dict) else {}
            raw_warnings = data.get("warnings")
            warnings: list[str] = (
                [_scrub_local_paths(w) for w in raw_warnings if str(w).strip()]
                if isinstance(raw_warnings, list)
                else []
            )
            us_stale = False
            if not sig_date or sig_date < latest_us_td:
                us_stale = True
                warnings.append(
                    f"signals.json date ({sig_date or 'missing'}) is earlier than latest US trading day ({latest_us_td})"
                )

            if op == "snapshot":
                cn_crowding_as_of = str(as_of_dict.get("cn_crowding") or "").strip()[:10]
                cn_margin_as_of = str(as_of_dict.get("cn_margin") or "").strip()[:10]
                cn_stale = False
                if cn_crowding_as_of and cn_crowding_as_of < latest_cn_td_m1:
                    cn_stale = True
                    warnings.append(
                        f"cn_crowding as_of ({cn_crowding_as_of}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
                    )
                if cn_margin_as_of and cn_margin_as_of < latest_cn_td_m1:
                    cn_stale = True
                    warnings.append(
                        f"cn_margin as_of ({cn_margin_as_of}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
                    )

                mom = us_market.get("momentum") if isinstance(us_market.get("momentum"), dict) else {}
                ndx_spw = us_market.get("ndx_spw") if isinstance(us_market.get("ndx_spw"), dict) else {}
                cn_a = data.get("cn_a") if isinstance(data.get("cn_a"), dict) else {}
                hk = data.get("hk") if isinstance(data.get("hk"), dict) else {}
                asia = data.get("asia") if isinstance(data.get("asia"), dict) else {}
                snap_res: dict[str, Any] = {
                    "ok": True,
                    "status": "ok",
                    "route": "market-regime",
                    "operation": "snapshot",
                    "date": sig_date,
                    "as_of": as_of_dict,
                    "stale": us_stale,
                    "us_topbottom": {
                        "top_framework": {
                            "stage": top_fw.get("stage"),
                            "active": top_fw.get("active"),
                            "summary": top_fw.get("summary"),
                            "window_remaining_td": top_fw.get("window_remaining_td"),
                        },
                        "bottom_framework": {
                            "stage": bot_fw.get("stage"),
                            "active": bot_fw.get("active"),
                            "summary": bot_fw.get("summary"),
                            "window_remaining_td": bot_fw.get("window_remaining_td"),
                        },
                    },
                    "put_hedge": top_fw.get("put_hedge"),
                    "momentum": {
                        "reading": mom.get("reading"),
                        "gscbhmom": mom.get("gscbhmom"),
                    },
                    "cn_a": {
                        "reading": cn_a.get("reading"),
                        "margin_rsi14": cn_a.get("margin_rsi14"),
                        "stale": cn_stale,
                    },
                    "hk": {
                        "reading": hk.get("reading"),
                        "ah_rsi30": hk.get("ah_rsi30"),
                    },
                    "asia": {
                        "reading": asia.get("reading"),
                    },
                    "warnings": warnings,
                }
                if ndx_spw.get("rsi14") is not None:
                    snap_res["ndx_spw"] = {
                        "rsi14": ndx_spw.get("rsi14"),
                    }
                return snap_res

            def _strip_large_arrays(fw: dict[str, Any]) -> dict[str, Any]:
                return {
                    k: v
                    for k, v in fw.items()
                    if k not in ("cycles", "signals", "history", "rows", "series")
                    and (k == "warnings" or not isinstance(v, list))
                }

            return {
                "ok": True,
                "status": "ok",
                "route": "market-regime",
                "operation": "us_topbottom",
                "date": sig_date,
                "generated_at": data.get("generated_at"),
                "as_of": as_of_dict,
                "stale": us_stale,
                "top_framework": _strip_large_arrays(top_fw),
                "bottom_framework": _strip_large_arrays(bot_fw),
                "warnings": warnings,
            }

        if op == "crowding":
            status, data, err = self._read_cached_json_sync(
                self.cfg.index_crowding_json, strip_crowding_history=True
            )
            if status != "ok" or data is None:
                return {
                    "ok": False,
                    "status": status,
                    "route": "market-regime",
                    "operation": "crowding",
                    "error": err or status,
                }
            raw_rows = data.get("rows")
            if data.get("ok") is False or not isinstance(raw_rows, list):
                return {
                    "ok": False,
                    "status": "invalid_source",
                    "route": "market-regime",
                    "operation": "crowding",
                    "error": "Malformed official-all.json: missing rows list",
                }

            trade_date = str(data.get("trade_date") or "").strip()[:10]
            warnings = []
            stale = False
            if not trade_date or trade_date < latest_cn_td_m1:
                stale = True
                warnings.append(
                    f"crowding trade_date ({trade_date or 'missing'}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
                )

            raw_top = arguments.get("top")
            if raw_top is None:
                raw_top = params.get("top")
            if raw_top is None:
                raw_top = arguments.get("limit") if arguments.get("limit") is not None else params.get("limit")
            try:
                top_n = max(1, min(int(raw_top), 30)) if raw_top is not None and not isinstance(raw_top, bool) else 10
            except (TypeError, ValueError):
                top_n = 10

            raw_order = str(arguments.get("order") or params.get("order") or "high").strip().lower()
            order = "low" if raw_order == "low" else "high"
            category_filter = str(arguments.get("category") or params.get("category") or "").strip()
            query_filter = str(arguments.get("query") or params.get("query") or "").strip()

            filtered_rows: list[dict[str, Any]] = []
            cat_lower = category_filter.lower()
            q_lower = query_filter.lower()
            for r in raw_rows:
                if not isinstance(r, dict):
                    continue
                clean_r = {k: v for k, v in r.items() if k != "history_1y"}
                if cat_lower:
                    row_cat = f"{clean_r.get('category') or ''} {clean_r.get('category_code') or ''}".lower()
                    if cat_lower not in row_cat:
                        continue
                if q_lower:
                    row_q = f"{clean_r.get('index_name') or ''} {clean_r.get('index_code') or ''}".lower()
                    if q_lower not in row_q:
                        continue
                filtered_rows.append(clean_r)

            def _crowding_sort_key(item: dict[str, Any]) -> float:
                val = item.get("crowding_percentile")
                try:
                    return float(val) if val is not None else (-1e9 if order == "high" else 1e9)
                except (TypeError, ValueError):
                    return -1e9 if order == "high" else 1e9

            filtered_rows.sort(key=_crowding_sort_key, reverse=(order == "high"))
            selected_rows = filtered_rows[:top_n]
            return {
                "ok": True,
                "status": "ok",
                "route": "market-regime",
                "operation": "crowding",
                "trade_date": trade_date,
                "as_of": trade_date,
                "stale": stale,
                "order": order,
                "top": top_n,
                "category": category_filter or None,
                "query": query_filter or None,
                "index_count": data.get("index_count", len(raw_rows)),
                "matched_count": len(filtered_rows),
                "count": len(selected_rows),
                "rows": selected_rows,
                "warnings": warnings,
            }

        # op == "cn_leverage"
        p_status, pipe_data, p_err = self._read_cached_json_sync(self.cfg.cn_leverage_status_json)
        if p_status != "ok" or pipe_data is None:
            return {
                "ok": False,
                "status": p_status,
                "route": "market-regime",
                "operation": "cn_leverage",
                "error": p_err or p_status,
            }
        if not pipe_data or not all(
            isinstance(v, dict) and "status" in v for v in pipe_data.values()
        ):
            return {
                "ok": False,
                "status": "invalid_source",
                "route": "market-regime",
                "operation": "cn_leverage",
                "error": "Malformed pipeline_status.json: expected mapping of pipeline status dicts",
            }

        c_status, csv_rows, c_err = self._read_cached_csv_sync(self.cfg.cn_leverage_unwind_csv)
        if c_status != "ok" or csv_rows is None:
            return {
                "ok": False,
                "status": c_status,
                "route": "market-regime",
                "operation": "cn_leverage",
                "error": c_err or c_status,
            }

        tail_raw = csv_rows[-5:]
        unwind_rows: list[dict[str, Any]] = []
        for r in tail_raw:
            item: dict[str, Any] = {}
            for col in CN_LEVERAGE_KEY_COLUMNS:
                if col in r:
                    item[col] = _parse_csv_scalar(r[col])
            if len(item) <= 1 and len(r) > 1:
                for k, v in list(r.items())[:6]:
                    item[k] = _parse_csv_scalar(v)
            unwind_rows.append(item)

        latest_date = str(unwind_rows[-1].get("date") or "").strip()[:10] if unwind_rows else ""
        warnings = []
        stale = False
        if not latest_date or latest_date < latest_cn_td_m1:
            stale = True
            warnings.append(
                f"cn_leverage unwind date ({latest_date or 'missing'}) is earlier than latest A-share trading day minus 1 ({latest_cn_td_m1})"
            )

        pipeline_rows: list[dict[str, Any]] = []
        for p_name, p_info in pipe_data.items():
            if not isinstance(p_info, dict):
                continue
            st_val = str(p_info.get("status") or "").strip()
            pipeline_rows.append(
                {
                    "name": str(p_name),
                    "status": st_val,
                    "updated": p_info.get("updated"),
                }
            )
            if st_val.lower() not in ("completed", "ok", "success", ""):
                detail_clean = _scrub_local_paths(p_info.get("detail") or "")
                warn_msg = f"pipeline {p_name} status={st_val}"
                if detail_clean:
                    warn_msg = f"{warn_msg}: {detail_clean}"
                warnings.append(warn_msg)

        return {
            "ok": True,
            "status": "ok",
            "route": "market-regime",
            "operation": "cn_leverage",
            "date": latest_date,
            "as_of": latest_date,
            "stale": stale,
            "pipeline_status": pipeline_rows,
            "latest_unwind": unwind_rows,
            "warnings": warnings,
        }

    async def _handle_market_regime(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Read and parse OpenClaw market-regime files in a background thread (`asyncio.to_thread`)."""
        return await asyncio.to_thread(self._build_market_regime_sync, arguments)

    async def handle_tools_call(
        self, req_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        tool_name = str(params.get("name") or "").strip()
        raw_args = params.get("arguments")
        arguments: dict[str, Any] = (
            dict(raw_args) if isinstance(raw_args, dict) else {}
        )
        route = str(arguments.get("route") or "").strip()
        operation = str(arguments.get("operation") or "").strip()

        handoff = self.handoff_routes()
        if route and route in handoff:
            err_payload = {
                "_declared_sources": [],
                "_sources": [],
                "ok": False,
                "status": "handoff_filtered",
                "error": f"Route '{route}' is an OpenClaw handoff route and is disabled in LangAlpha.",
            }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(err_payload, ensure_ascii=False),
                        }
                    ],
                    "isError": True,
                },
            }

        eff_timeout = self.compute_timeout(tool_name, arguments)
        call_args = dict(arguments)
        if tool_name != "catalog":
            call_args["timeout"] = eff_timeout

        do_fanout = self._should_fanout_alphaengine(tool_name, arguments)
        ae_args: dict[str, Any] | None = (
            self._build_alphaengine_fanout_args(arguments) if do_fanout else None
        )
        gp_args: dict[str, Any] | None = (
            self._build_guidepoint_fanout_args(arguments) if do_fanout else None
        )
        ae_outcome: dict[str, Any] | None = None
        gp_outcome: dict[str, Any] | None = None

        t0 = time.monotonic()
        test_delay_s = self.cfg.resolve_test_delay_s()
        async with self.sem:
            try:
                if test_delay_s > 0.0:
                    await asyncio.wait_for(
                        asyncio.sleep(test_delay_s),
                        timeout=float(eff_timeout),
                    )
                elapsed_so_far = time.monotonic() - t0
                remaining_budget_s = max(
                    5.0, float(eff_timeout) - elapsed_so_far + 5.0
                )
                rpc_req = {
                    "jsonrpc": "2.0",
                    "id": req_id if req_id is not None else 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": call_args},
                }
                if route == "market-regime":
                    mr_payload = await asyncio.wait_for(
                        self._handle_market_regime(arguments),
                        timeout=remaining_budget_s,
                    )
                    resps = [
                        {
                            "jsonrpc": "2.0",
                            "id": req_id if req_id is not None else 1,
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": json.dumps(
                                            mr_payload, ensure_ascii=False
                                        ),
                                    }
                                ],
                                "structuredContent": dict(mr_payload),
                                "isError": not bool(mr_payload.get("ok")),
                            },
                        }
                    ]
                elif ae_args is not None and gp_args is not None:
                    ae_timeout = int(
                        ae_args.get("timeout") or FANOUT_ALPHAENGINE_TIMEOUT_S
                    )
                    ae_budget_s = max(
                        5.0, float(ae_timeout) - elapsed_so_far + 5.0
                    )
                    gp_timeout = int(
                        gp_args.get("timeout") or FANOUT_GUIDEPOINT_TIMEOUT_S
                    )
                    gp_budget_s = max(
                        5.0, float(gp_timeout) - elapsed_so_far + 5.0
                    )
                    ae_task = asyncio.create_task(
                        self._run_alphaengine_fanout(req_id, ae_args, ae_budget_s)
                    )
                    gp_task = asyncio.create_task(
                        self._run_guidepoint_fanout(req_id, gp_args, gp_budget_s)
                    )
                    try:
                        try:
                            resps = await self._run_stdio_rpc(
                                [rpc_req], timeout_s=remaining_budget_s
                            )
                        except (asyncio.TimeoutError, Exception) as local_exc:
                            # Let online calls that are already done finish before cancelling the rest.
                            await asyncio.sleep(0)
                            if not ae_task.done():
                                ae_task.cancel()
                            if not gp_task.done():
                                gp_task.cancel()
                            raw_ae, raw_gp = await asyncio.gather(
                                ae_task, gp_task, return_exceptions=True
                            )
                            is_local_timeout = isinstance(local_exc, asyncio.TimeoutError)
                            local_st = "timeout" if is_local_timeout else "failed"
                            local_reason = (
                                f"researchdata_bridge timed out after {eff_timeout}s on {tool_name}/{route}"
                                if is_local_timeout
                                else (
                                    f"SubprocessExitError(exit_code={local_exc.exit_code})"
                                    if isinstance(local_exc, SubprocessExitError)
                                    else type(local_exc).__name__
                                )
                            )
                            ae_outcome = (
                                raw_ae
                                if isinstance(raw_ae, dict)
                                else {
                                    "ok": False,
                                    "status": "canceled",
                                    "route": "alphaengine",
                                    "reason": f"canceled after local all {local_st}",
                                    "count": 0,
                                    "results": [],
                                    "raw_payload": None,
                                }
                            )
                            gp_outcome = (
                                raw_gp
                                if isinstance(raw_gp, dict)
                                else {
                                    "ok": False,
                                    "status": "canceled",
                                    "route": "guidepoint",
                                    "reason": f"canceled after local all {local_st}",
                                    "count": 0,
                                    "results": [],
                                    "raw_payload": None,
                                }
                            )
                            any_online_ok = bool(
                                ae_outcome.get("ok") or gp_outcome.get("ok")
                            )
                            fallback_payload: dict[str, Any] = {
                                "ok": any_online_ok,
                                "status": "ok" if any_online_ok else local_st,
                                "error": local_reason,
                                "results": [],
                                "result_count": 0,
                                "source_status": {
                                    "local_all": {
                                        "ok": False,
                                        "status": local_st,
                                        "reason": local_reason,
                                        "error": local_reason,
                                        "count": 0,
                                    }
                                },
                            }
                            resps = [
                                {
                                    "jsonrpc": "2.0",
                                    "id": req_id if req_id is not None else 1,
                                    "result": {
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": json.dumps(
                                                    fallback_payload,
                                                    ensure_ascii=False,
                                                ),
                                            }
                                        ],
                                        "structuredContent": dict(fallback_payload),
                                        "isError": not any_online_ok,
                                    },
                                }
                            ]
                        except BaseException:
                            ae_task.cancel()
                            gp_task.cancel()
                            await asyncio.gather(
                                ae_task, gp_task, return_exceptions=True
                            )
                            raise
                        else:
                            ae_outcome, gp_outcome = await asyncio.gather(ae_task, gp_task)
                    finally:
                        # Whatever ends the fan-out (an error, a cancel mid-handler), no online
                        # subprocess outlives the call: cancel what is still running and reap it.
                        await _cancel_and_reap((ae_task, gp_task))
                else:
                    resps = await self._run_stdio_rpc(
                        [rpc_req], timeout_s=remaining_budget_s
                    )
            except asyncio.TimeoutError:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                logger.warning(
                    "tools/call timeout tool=%s route=%s op=%s timeout_s=%d elapsed_ms=%d",
                    tool_name,
                    route,
                    operation,
                    eff_timeout,
                    elapsed_ms,
                )
                timeout_payload = {
                    "_declared_sources": [],
                    "_sources": [],
                    "ok": False,
                    "status": "timeout",
                    "error": f"researchdata_bridge timed out after {eff_timeout}s on {tool_name}/{route}",
                }
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    timeout_payload, ensure_ascii=False
                                ),
                            }
                        ],
                        "isError": True,
                    },
                }

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp = resps[0] if resps else {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": "Empty response from researchdata_mcp"}}
        resp["id"] = req_id

        result_obj = resp.get("result")
        status_str = "ok"
        declared_count = 0

        if isinstance(result_obj, dict) and isinstance(result_obj.get("content"), list):
            blocks = result_obj["content"]
            if blocks and isinstance(blocks[0], dict) and isinstance(blocks[0].get("text"), str):
                try:
                    payload = json.loads(blocks[0]["text"])
                except Exception:
                    payload = None

                if isinstance(payload, dict):
                    # Filter handoff routes from catalog() output and include market-regime
                    if tool_name == "catalog" and isinstance(payload.get("routes"), list):
                        payload["routes"] = [
                            r
                            for r in payload["routes"]
                            if isinstance(r, dict) and r.get("id") not in handoff
                        ]
                        cat_filter = str(arguments.get("category") or "").strip().lower()
                        if not cat_filter or cat_filter == "macro":
                            existing_ids = {
                                str(r.get("id") or "")
                                for r in payload["routes"]
                                if isinstance(r, dict)
                            }
                            if "market-regime" not in existing_ids:
                                payload["routes"].append(dict(MARKET_REGIME_CATALOG_ENTRY))
                        payload["route_count"] = len(payload["routes"])

                    declared = source_identity.declare(tool_name, arguments, payload)
                    if ae_outcome is not None and ae_args is not None:
                        declared = self._merge_alphaengine_into_all(
                            payload, declared, ae_outcome, ae_args
                        )
                    if gp_outcome is not None and gp_args is not None:
                        declared = self._merge_guidepoint_into_all(
                            payload, declared, gp_outcome, gp_args
                        )
                    declared_count = len(declared)
                    status_str = str(payload.get("status") or ("ok" if payload.get("ok") else "error"))

                    # Track AlphaEngine get_document quota usage
                    if route == "alphaengine" and operation == "get_document":
                        inner = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                        sc = inner.get("structuredContent") if isinstance(inner, dict) else None
                        chars = 0
                        if isinstance(sc, dict):
                            chars = int(sc.get("returned_chars") or len(str(sc.get("text") or "")))
                        await self._record_alphaengine_get_doc(chars)

                    # Place _declared_sources and _sources first so 64 KiB truncation preserves them
                    enriched = {
                        "_declared_sources": declared,
                        "_sources": declared,
                        **payload,
                    }
                    blocks[0]["text"] = json.dumps(enriched, ensure_ascii=False)
                    if isinstance(result_obj.get("structuredContent"), dict):
                        result_obj["structuredContent"] = dict(enriched)

        logger.info(
            "tools/call tool=%s route=%s op=%s status=%s declared_sources=%d elapsed_ms=%d",
            tool_name,
            route or "-",
            operation or "-",
            status_str,
            declared_count,
            elapsed_ms,
        )
        return resp

    def _resolve_company_wiki_python_and_cli(self) -> tuple[str, Path]:
        """Locate the OpenClaw company-wiki skill Python binary and CLI script without modifying them."""
        skill_dir = self.cfg.ocw_root / "skills" / "company-wiki"
        cli_script = skill_dir / "scripts" / "company_wiki_cli.py"
        for candidate in (
            skill_dir / "venv" / "bin" / "python",
            self.cfg.ocw_root / "skills" / "market-digest" / "venv" / "bin" / "python",
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate), cli_script
        return self.cfg.python_bin, cli_script

    async def _run_company_wiki_embed(
        self,
        texts: list[str],
        task_type: str,
        requested_model: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Invoke OpenClaw company-wiki's embedding functions in a read-only subprocess.

        Reads `GEMINI_API_KEY` strictly from `self.cfg.env_path` so process environment variables
        can never override or supply the `/embed` key.
        """
        env_file_vars = _read_env_file_dict(self.cfg.env_path)
        gemini_api_key = env_file_vars.get("GEMINI_API_KEY", "").strip()
        if not gemini_api_key:
            return {
                "ok": False,
                "status": "error",
                "error": "GEMINI_API_KEY not configured in bridge env file",
            }

        py_bin, cli_script = self._resolve_company_wiki_python_and_cli()
        if not cli_script.is_file():
            return {
                "ok": False,
                "status": "error",
                "error": "company_wiki_cli.py not found under OpenClaw workspace",
            }

        driver_code = (
            "import contextlib, importlib.util, io, json, pathlib, sys\n"
            "sys.dont_write_bytecode = True\n"
            "req = json.loads(sys.stdin.read())\n"
            "spec = importlib.util.spec_from_file_location('company_wiki_cli', req['cli_script'])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "try:\n"
            "    cfg_path = pathlib.Path(getattr(mod, 'CONFIG_PATH', pathlib.Path(req['cli_script']).resolve().parent.parent / 'config.json'))\n"
            "    if not cfg_path.is_file():\n"
            "        raise FileNotFoundError('company-wiki config.json not found')\n"
            "    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))\n"
            "    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):\n"
            "        model_name = req.get('model')\n"
            "        dims = req.get('dims')\n"
            "        if not model_name:\n"
            "            model_name, dims = mod.resolve_embedding_model(cfg)\n"
            "        else:\n"
            "            dims = int(dims or cfg.get('embedding', {}).get('dimensions', 3072))\n"
            "        texts = req.get('texts') or []\n"
            "        if req.get('task_type') == 'RETRIEVAL_QUERY':\n"
            "            vecs = [[float(x) for x in mod.embed_query(t, model_name)] for t in texts]\n"
            "        else:\n"
            "            vecs = [[float(x) for x in v] for v in mod.embed_texts(texts, model_name)]\n"
            "    if vecs and vecs[0]:\n"
            "        dims = len(vecs[0])\n"
            "    print(json.dumps({'ok': True, 'status': 'ok', 'model': model_name, 'dimensions': dims, 'embeddings': vecs}))\n"
            "except FileNotFoundError:\n"
            "    print(json.dumps({'ok': False, 'status': 'error', 'error': 'company_wiki_config_missing'}))\n"
            "except SystemExit:\n"
            "    print(json.dumps({'ok': False, 'status': 'error', 'error': 'company-wiki embedding exited'}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'status': 'error', 'error': type(e).__name__}))\n"
        )

        cmd = [py_bin, "-B", "-W", "ignore", "-c", driver_code]
        subproc_env = {
            **os.environ,
            **env_file_vars,
            "GEMINI_API_KEY": gemini_api_key,
            "PYTHONDONTWRITEBYTECODE": "1",
        }

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.cfg.ocw_root),
            env=subproc_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        req_payload = {
            "cli_script": str(cli_script),
            "texts": texts,
            "task_type": task_type,
            "model": requested_model or self._resolved_embed_model,
            "dims": self._resolved_embed_dims,
        }
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(input=json.dumps(req_payload, ensure_ascii=False).encode("utf-8")),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {"ok": False, "status": "timeout", "error": "Embedding subprocess timed out"}
        except Exception as exc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {"ok": False, "status": "error", "error": type(exc).__name__}

        for line in reversed(stdout_bytes.decode("utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
        return {
            "ok": False,
            "status": "error",
            "error": f"Embedding subprocess returned no JSON (exit_code={proc.returncode})",
        }

    async def handle_embed(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Handle `POST /embed` requests without logging input text or secrets."""
        raw_texts = body.get("texts")
        if raw_texts is None and "input" in body:
            raw_texts = body.get("input")
        if raw_texts is None and "text" in body:
            raw_texts = [body.get("text")]
        if isinstance(raw_texts, str):
            texts = [raw_texts]
        elif isinstance(raw_texts, list) and all(isinstance(x, str) for x in raw_texts):
            texts = [x for x in raw_texts]
        else:
            return 400, {"ok": False, "status": "bad_request", "error": "texts must be a string or list of strings"}

        if not texts or len(texts) > 100:
            return 400, {"ok": False, "status": "bad_request", "error": "texts must contain 1..100 items"}

        task_type = str(body.get("task_type") or "RETRIEVAL_DOCUMENT").strip().upper()
        if task_type not in ("RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"):
            task_type = "RETRIEVAL_DOCUMENT"
        req_model = str(body.get("model") or "").strip() or None

        t0 = time.monotonic()
        async with self.embed_sem:
            res = await self._run_company_wiki_embed(
                texts=texts,
                task_type=task_type,
                requested_model=req_model,
                timeout_s=120.0,
            )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if res.get("ok") and isinstance(res.get("embeddings"), list):
            model_used = str(res.get("model") or "gemini-embedding-001")
            dims_used = int(res.get("dimensions") or 3072)
            if not req_model:
                self._resolved_embed_model = model_used
                self._resolved_embed_dims = dims_used
            logger.info(
                "embed count=%d task_type=%s model=%s dims=%d status=ok elapsed_ms=%d",
                len(texts),
                task_type,
                model_used,
                dims_used,
                elapsed_ms,
            )
            return 200, {
                "ok": True,
                "status": "ok",
                "model": model_used,
                "dimensions": dims_used,
                "embeddings": res["embeddings"],
            }

        err_msg = str(res.get("error") or "embedding_failed")
        logger.warning(
            "embed count=%d task_type=%s status=error error=%s elapsed_ms=%d",
            len(texts),
            task_type,
            err_msg,
            elapsed_ms,
        )
        return 502, {
            "ok": False,
            "status": str(res.get("status") or "error"),
            "error": err_msg,
        }


async def _send_http(
    writer: asyncio.StreamWriter,
    status_code: int,
    status_text: str,
    body_Obj: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> None:
    body_bytes = (
        json.dumps(body_Obj, ensure_ascii=False).encode("utf-8")
        if body_Obj is not None
        else b""
    )
    headers = [
        f"HTTP/1.1 {status_code} {status_text}",
        "Content-Type: application/json; charset=utf-8",
        f"Content-Length: {len(body_bytes)}",
        "Connection: close",
    ]
    if extra_headers:
        for k, v in extra_headers.items():
            headers.append(f"{k}: {v}")
    header_bytes = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
    writer.write(header_bytes + body_bytes)
    await writer.drain()
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass


def _extract_supplied_token(headers: Mapping[str, str]) -> str:
    """Extract bridge authentication token from `Authorization: Bearer <token>` or `X-Dalton-Bridge-Token`."""
    lowered = {k.lower(): v for k, v in headers.items()}
    auth_header = lowered.get("authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    bridge_header = lowered.get("x-dalton-bridge-token", "").strip()
    if bridge_header:
        return bridge_header
    return ""


async def _handle_client(
    bridge: ResearchDataBridge,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        req_line_bytes = await asyncio.wait_for(reader.readline(), timeout=15.0)
        if not req_line_bytes:
            writer.close()
            return
        req_line = req_line_bytes.decode("utf-8", errors="replace").strip()
        parts = req_line.split(" ")
        if len(parts) < 2:
            await _send_http(writer, 400, "Bad Request", {"error": "Bad Request"})
            return
        method, raw_path = parts[0].upper(), parts[1]
        path = raw_path.split("?", 1)[0]

        # Read headers
        headers: dict[str, str] = {}
        while True:
            hline = await asyncio.wait_for(reader.readline(), timeout=15.0)
            if not hline or hline in (b"\r\n", b"\n"):
                break
            decoded = hline.decode("utf-8", errors="replace").strip()
            if ":" in decoded:
                hk, hv = decoded.split(":", 1)
                headers[hk.strip().lower()] = hv.strip()

        content_length = int(headers.get("content-length") or "0")
        max_bytes = bridge.cfg.embed_max_bytes if path == "/embed" else MAX_REQUEST_BYTES
        if content_length > max_bytes:
            await _send_http(
                writer,
                413,
                "Payload Too Large",
                {"error": f"Request body exceeds {max_bytes} bytes"},
            )
            return

        body_bytes = b""
        if content_length > 0:
            body_bytes = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=15.0
            )

        # Route 1: GET /healthz
        if method == "GET" and path == "/healthz":
            if bridge.server_version == "unknown":
                try:
                    await bridge.ensure_initialized()
                except Exception as exc:
                    logger.warning(
                        "Healthz init probe failed: %s (exit_code=%s)",
                        type(exc).__name__,
                        getattr(exc, "exit_code", None),
                    )
            health_payload = {
                "ok": True,
                "status": "ok",
                "server_version": bridge.server_version,
                "max_inflight": bridge.cfg.max_inflight,
                "embed_max_inflight": bridge.cfg.embed_max_inflight,
                "embed_max_bytes": bridge.cfg.embed_max_bytes,
                "caller_budget_s": bridge.cfg.caller_budget_s,
                "alphaengine_get_document_today": bridge._read_usage_today(),
            }
            await _send_http(writer, 200, "OK", health_payload)
            return

        # Route 2: POST /embed (header token only; path tokens rejected)
        if path == "/embed":
            supplied_token = _extract_supplied_token(headers)
            expected_token = bridge.cfg.token
            if not expected_token or not supplied_token or not hmac.compare_digest(
                supplied_token.encode("utf-8"), expected_token.encode("utf-8")
            ):
                await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
                return
            if method != "POST":
                await _send_http(
                    writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
                )
                return
            try:
                embed_body = json.loads(body_bytes.decode("utf-8"))
                if not isinstance(embed_body, dict):
                    raise ValueError("Expected JSON object")
            except Exception:
                await _send_http(writer, 400, "Bad Request", {"ok": False, "error": "Parse error"})
                return
            code, payload = await bridge.handle_embed(embed_body)
            status_label = "OK" if code == 200 else ("Bad Request" if code == 400 else "Bad Gateway")
            await _send_http(writer, code, status_label, payload)
            return

        # Route 3: /mcp (header token) or legacy /mcp/<token> (path token, deprecated)
        if path in ("/mcp", "/mcp/"):
            supplied_token = _extract_supplied_token(headers)
        elif path.startswith("/mcp/"):
            supplied_token = path[len("/mcp/") :].strip("/") or _extract_supplied_token(headers)
        else:
            await _send_http(writer, 404, "Not Found", {"error": "Not Found"})
            return

        expected_token = bridge.cfg.token
        if not expected_token or not supplied_token or not hmac.compare_digest(
            supplied_token.encode("utf-8"), expected_token.encode("utf-8")
        ):
            # Constant-time rejection; NEVER log supplied_token or path
            await _send_http(writer, 401, "Unauthorized", {"error": "Unauthorized"})
            return

        if method in ("GET", "DELETE"):
            await _send_http(
                writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
            )
            return

        if method != "POST":
            await _send_http(
                writer, 405, "Method Not Allowed", {"error": "Method Not Allowed"}
            )
            return

        try:
            rpc_msg = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            await _send_http(
                writer,
                400,
                "Bad Request",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                },
            )
            return

        if not isinstance(rpc_msg, dict):
            await _send_http(
                writer,
                400,
                "Bad Request",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
            )
            return

        req_id = rpc_msg.get("id")
        rpc_method = str(rpc_msg.get("method") or "")
        rpc_params = rpc_msg.get("params") if isinstance(rpc_msg.get("params"), dict) else {}

        # JSON-RPC notifications (no id, e.g., notifications/initialized) -> 202 Accepted
        if "id" not in rpc_msg or req_id is None:
            await _send_http(writer, 202, "Accepted", None)
            return

        if rpc_method == "initialize":
            init_res = await bridge.ensure_initialized()
            offered_proto = str(rpc_params.get("protocolVersion") or "2024-11-05")
            adopted_proto = (
                offered_proto
                if offered_proto in ("2024-11-05", "2025-03-26", "2025-11-25")
                else str(init_res.get("protocolVersion") or "2024-11-05")
            )
            reply = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": adopted_proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "researchdata",
                        "version": bridge.server_version,
                    },
                },
            }
            await _send_http(writer, 200, "OK", reply)
            return

        if rpc_method == "ping":
            await _send_http(
                writer, 200, "OK", {"jsonrpc": "2.0", "id": req_id, "result": {}}
            )
            return

        if rpc_method == "tools/list":
            tools = await bridge.get_filtered_tools()
            await _send_http(
                writer,
                200,
                "OK",
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}},
            )
            return

        if rpc_method == "tools/call":
            reply = await bridge.handle_tools_call(req_id, rpc_params)
            await _send_http(writer, 200, "OK", reply)
            return

        # Unsupported method (e.g. server/discover probe -> -32601 so client falls back to legacy initialize)
        await _send_http(
            writer,
            200,
            "OK",
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {rpc_method}"},
            },
        )
    except Exception as exc:
        logger.error("Unhandled bridge error: %s", type(exc).__name__)
        try:
            await _send_http(
                writer,
                500,
                "Internal Server Error",
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32603, "message": "Internal bridge error"},
                },
            )
        except Exception:
            pass


async def main() -> None:
    cfg = BridgeConfig()
    if not cfg.token:
        logger.error(
            "DALTON_BRIDGE_TOKEN is not set (checked env and ~/.config/dalton/researchdata-bridge.env)"
        )
        sys.exit(1)

    bridge = ResearchDataBridge(cfg)
    try:
        await bridge.ensure_initialized()
    except Exception as exc:
        logger.warning(
            "Startup initialize probe warning: %s (exit_code=%s)",
            type(exc).__name__,
            getattr(exc, "exit_code", None),
        )

    server = await asyncio.start_server(
        lambda r, w: _handle_client(bridge, r, w),
        host=cfg.host,
        port=cfg.port,
    )
    logger.warning(
        "路径令牌已弃用 (POST /mcp/<token> is deprecated; use POST /mcp with Authorization: Bearer <token> or X-Dalton-Bridge-Token)"
    )
    logger.info(
        "Listening on http://%s:%d (server_version=%s, max_inflight=%d, caller_budget_s=%d)",
        cfg.host,
        cfg.port,
        bridge.server_version,
        cfg.max_inflight,
        cfg.caller_budget_s,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
