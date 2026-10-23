"""Container-side conversation segmentation, LLM card extraction, quote verification, and deduplication.

Follows Dalton's LLM contract (`assist_json` -> `dalton_complete` -> `LLMService.complete`,
never `create_llm()`), defaulting to Antigravity's `gemini-4-argon-medium-oauth`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.database.conversations import build_card_search_text
from src.dalton.llm.assist import AssistResult, _normalize_schema_enums, assist_json
from src.dalton.llm.complete import resolve_model_billing_and_provider
from src.dalton.settings import get_dalton_settings

logger = logging.getLogger(__name__)

VALID_SEGMENT_CATEGORIES: frozenset[str] = frozenset({"research", "ops", "private"})

VALID_CARD_TYPES: frozenset[str] = frozenset(
    {
        "fact",
        "estimate",
        "model_assumption",
        "opinion",
        "question",
        "decision",
        "todo",
    }
)

# Private / unlisted companies must stay in `entities[]` rather than `tickers[]`
PRIVATE_COMPANIES: frozenset[str] = frozenset(
    {
        "OPENAI",
        "ANTHROPIC",
        "XAI",
        "SPACEX",
        "STRIPE",
        "DATABRICKS",
        "BYTEDANCE",
        "TIKTOK",
        "SHEIN",
        "ANDURIL",
        "SCALEAI",
        "SCALE AI",
        "PERPLEXITY",
        "CURSOR",
        "ANYSHERE",
        "MISTRAL",
        "SSI",
        "DEEPSEEK",
        "MOONSHOT",
        "KIMI",
        "ZHIPU",
        "UNITREE",
        "宇树",
        "宇树科技",
        "月之暗面",
        "智谱",
        "字节",
        "字节跳动",
        "小红书",
        "华为",
    }
)

EXTRACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "enum": ["research", "ops", "private"],
        },
        "topic_label": {
            "type": "string",
        },
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": [
                            "fact",
                            "estimate",
                            "model_assumption",
                            "opinion",
                            "question",
                            "decision",
                            "todo",
                        ],
                    },
                    "topic_label": {"type": "string"},
                    "claim": {"type": "string"},
                    "tickers": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "sectors": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "figures": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "metric": {"type": "string"},
                                "value": {"type": ["number", "string"]},
                                "unit": {"type": "string"},
                                "period": {"type": "string"},
                            },
                            "required": ["metric", "value", "unit", "period"],
                        },
                    },
                    "quote": {"type": "string"},
                    "quote_event_id": {"type": "string"},
                    "cited_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "confidence": {"type": "number"},
                },
                "required": [
                    "type",
                    "claim",
                    "quote",
                    "quote_event_id",
                ],
            },
        },
    },
    "required": ["category", "topic_label", "cards"],
}

EXTRACTION_SYSTEM_INSTRUCTIONS = """你是 Dalton 投研团队的对话历史整理器，负责把 OpenClaw 人类投研对话段整理成可追溯的知识卡片（来源类型固定为 `conversation`）。

步骤与硬性规则：
1. **话题分类 (`category`)**：
   - `"research"`：与公司基本面、财务指标、估值、行业趋势、商业模式、投资决策、研究待办相关的讨论。
   - `"ops"`：系统运维、授权批准、配对码、cron 定时任务、模型切换、环境配置。
   - `"private"`：私人生活、家庭、个人隐私、非投研闲聊。
   - 若为 `"ops"` 或 `"private"`，`cards` 必须返回空数组 `[]`。
2. **原文逐字引用 (`quote` & `quote_event_id`)、证据覆盖、保留限定与归属**：
   - 每张卡的 `quote` **必须逐字摘自输入事件 `text` 的原话子串**，绝不能改写、概括、拼接或编造！`quote_event_id` 必须填写该句原话所在事件的 `event_id`。
   - **证据覆盖**：`claim` 的每个实质分句、`figures` 的每个指标，都必须由**本卡 `quote`** 直接支持；不能把同段对话里其他位置的信息写进本卡；覆盖不到的就拆卡或删掉；`cited_sources`、`topic_label` 不能替代证据。
   - **保留限定与归属**：保留“应该、可能、怀疑、初步、约、以上、超过、一度、用途含”等限定词，不得改成确定值或“证实/已经/全部用于”；保留条件、时间范围和主体归属；指代不清时扩大连续引用，不要自行补全名称；一条 `quote` 里有多家公司时，不得张冠李戴。
3. **卡片类型 (`type`) 与说话人来源 (`cited_sources`)**（按优先级定义，内容混合时优先拆卡）：
   - `"fact"`：已经发生、且本卡 `quote` 里有具体来源的实际数（若原话来自 `agent` 如 Eve，只有当本卡 `quote` 中明确给出了具体资料来源时才可标为 `"fact"` 并填入 `cited_sources`；无明确引用来源一律不得当作事实）。
   - `"estimate"`：他人的预测、一致预期、指引、TAM（引用 Gartner 只说明“Gartner 这么估”，预测本身标为 `"estimate"` 而非 `"fact"`）。
   - `"model_assumption"`：抽取者自己的倍数、倒算、情景测算（agent 自己的情景测算不要求外部来源，但数字必须由本卡 `quote` 支持）。
   - `"opinion"`：判断、建议、技术信号。
   - `"todo"`：带验证条件和到期日的待办，证实或证伪的阈值要写进 `claim`。
4. **上市公司 (`tickers`) vs 未上市私企 (`entities`)**：
   - `tickers` 只放本卡 `quote` 正文里作为讨论对象出现的上市公司股票代码（大写）；信源机构（如 Gartner 对应 `IT`）不放；写明“剔除/未采用”的不放；`quote` 没点名公司时留空 `[]`。
   - 未上市私企（如 `OpenAI`, `Anthropic`, `ByteDance`, `SpaceX`, `Stripe`, `Databricks`, `DeepSeek`, `Unitree`）放入 `entities`，**禁止**放入 `tickers`。
5. **结构化指标 (`figures`)**：
   - 当原话包含明确数值指标时，提取 `{metric, value, unit, period}`。
   - `value` 的数字必须出现在本卡 `quote` 里；区间和上下限原样保留（可以用字符串，如 `">35"`、`"25-35"`）。
   - `quote` 没写期间时 `period` 留空 `""`，不得用消息日期或报告日期代替。
   - 不得改写口径（年增速不是 CAGR，利润率不是营业利润率）。
   - 多家公司时 `metric` 要带上公司名，或者拆卡。
"""


@dataclass
class SegmentExtractionOutcome:
    """Outcome of extracting cards from a single conversation segment."""

    ok: bool = True
    category: str = "research"
    cards: list[dict[str, Any]] = field(default_factory=list)
    cards_raw_count: int = 0
    quotes_rejected: int = 0
    cards_flagged_unsupported_number: int = 0
    soft_unsupported_numbers: int = 0
    figures_dropped: int = 0
    periods_cleared: int = 0
    tickers_dropped: int = 0
    discarded_segment: bool = False
    model_used: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    rate_limited: bool = False
    permanent_quota_error: bool = False
    status: str = ""
    error_kind: str | None = None
    in_flight: bool = False
    transient_error: bool = False
    cached: bool = False
    replayed: bool = False
    error: str | None = None


def normalize_card_enum_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip whitespace and lowercase enum fields (`category`, `cards[].type`), rejecting invalid values."""
    _normalize_schema_enums(payload, EXTRACTION_RESPONSE_SCHEMA)
    if "category" in payload and payload["category"] is not None:
        cat = str(payload["category"]).strip().lower()
        payload["category"] = cat
        if cat not in VALID_SEGMENT_CATEGORIES:
            allowed_cats = list(EXTRACTION_RESPONSE_SCHEMA["properties"]["category"]["enum"])
            raise ValueError(f"ValidationError: {cat!r} is not one of {allowed_cats}")
    raw_cards = payload.get("cards")
    if isinstance(raw_cards, list):
        allowed_types = list(
            EXTRACTION_RESPONSE_SCHEMA["properties"]["cards"]["items"]["properties"]["type"]["enum"]
        )
        for card in raw_cards:
            if isinstance(card, dict) and "type" in card and card["type"] is not None:
                c_type = str(card["type"]).strip().lower()
                card["type"] = c_type
                if c_type not in VALID_CARD_TYPES:
                    raise ValueError(f"ValidationError: {c_type!r} is not one of {allowed_types}")
    return payload


def normalize_whitespace(text: str) -> str:
    """Collapse all whitespace sequences into a single space for verbatim quote matching."""
    return re.sub(r"\s+", " ", text or "").strip()


def compute_content_hash(quote: str, quote_event_id: str) -> str:
    """Compute `sha256(normalized_quote + event_id)` for idempotent deduplication."""
    norm_q = normalize_whitespace(quote)
    ev_id = str(quote_event_id or "").strip()
    return hashlib.sha256(f"{norm_q}{ev_id}".encode("utf-8")).hexdigest()


def verify_verbatim_quote(
    quote: str,
    quote_event_id: str,
    segment_events: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any] | None]:
    """Verify that `quote` appears verbatim (after whitespace normalization) in `segment_events`.

    Checks the event matching `quote_event_id` first, then falls back to scanning other events
    in the same segment (updating `quote_event_id` to the matched event).
    """
    norm_quote = normalize_whitespace(quote)
    if not norm_quote:
        return False, None

    by_id = {str(ev.get("event_id") or ""): ev for ev in segment_events}
    target_ev = by_id.get(str(quote_event_id or ""))
    if target_ev is not None:
        ev_norm_text = normalize_whitespace(str(target_ev.get("text") or ""))
        if norm_quote in ev_norm_text:
            return True, target_ev

    for ev in segment_events:
        ev_norm_text = normalize_whitespace(str(ev.get("text") or ""))
        if norm_quote in ev_norm_text:
            return True, ev

    return False, None


def normalize_tickers_and_entities(
    raw_tickers: list[Any] | None,
    raw_entities: list[Any] | None,
) -> tuple[list[str], list[str]]:
    """Route known private companies (OpenAI, Anthropic, etc.) into `entities[]` and normalize tickers."""
    tickers: list[str] = []
    entities: list[str] = []

    for e in raw_entities or []:
        es = str(e or "").strip()
        if es and es not in entities:
            entities.append(es)

    for t in raw_tickers or []:
        ts = str(t or "").strip()
        if not ts:
            continue
        upper_t = ts.upper()
        if upper_t in PRIVATE_COMPANIES or ts in PRIVATE_COMPANIES:
            if ts not in entities and upper_t not in {x.upper() for x in entities}:
                entities.append(ts)
        else:
            if upper_t not in tickers:
                tickers.append(upper_t)

    return tickers, entities


def enforce_speaker_and_agent_citation(
    card_type: str,
    cited_sources: list[Any] | None,
    source_event: dict[str, Any],
    *,
    quote: str | None = None,
    segment_events: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Populate speaker from `source_event` and downgrade uncited or unverified agent claims to `'opinion'`."""
    ev_speaker = source_event.get("speaker")
    if isinstance(ev_speaker, dict):
        role = str(ev_speaker.get("role") or "participant").lower()
        if role not in ("owner", "participant", "agent"):
            role = "owner" if ev_speaker.get("is_owner") else "participant"
        speaker = {
            "sender_id_hash": str(ev_speaker.get("sender_id_hash") or "unknown"),
            "name": str(ev_speaker.get("name") or ("Owner" if role == "owner" else "Participant")),
            "is_owner": bool(ev_speaker.get("is_owner", role == "owner")),
            "role": role,
        }
    else:
        msg_role = str(source_event.get("message_role") or "user")
        role = "agent" if msg_role == "assistant" else "owner"
        speaker = {
            "sender_id_hash": "agent:main" if role == "agent" else "owner",
            "name": "Eve" if role == "agent" else "Owner",
            "is_owner": role == "owner",
            "role": role,
        }

    cleaned_sources = [
        str(s).strip() for s in (cited_sources or []) if s is not None and str(s).strip()
    ]
    eff_type = str(card_type if card_type is not None else "opinion").strip().lower()
    if eff_type not in VALID_CARD_TYPES:
        allowed_types = list(
            EXTRACTION_RESPONSE_SCHEMA["properties"]["cards"]["items"]["properties"]["type"]["enum"]
        )
        raise ValueError(f"ValidationError: {eff_type!r} is not one of {allowed_types}")

    # Rule: agent (Eve) fact/estimate requires non-empty cited_sources
    # where every cited source is present as a substring in the card's own quote.
    # Agent's own `model_assumption` does not require external sources (its numbers
    # still undergo `validate_candidate_card` verification against `quote`).
    if speaker["role"] == "agent" and eff_type in ("fact", "estimate"):
        quote_raw = str(quote if quote is not None else (source_event.get("text") or ""))
        quote_norm = normalize_whitespace(quote_raw)
        if not cleaned_sources:
            eff_type = "opinion"
        else:
            all_verified = all(
                (src in quote_raw) or (normalize_whitespace(src) in quote_norm)
                for src in cleaned_sources
            )
            if not all_verified:
                eff_type = "opinion"

    return speaker, eff_type, cleaned_sources


# Supplemental ticker -> company name aliases complementing `config/dalton/coverage.yaml`
# (which holds the canonical 105 Core/Watch companies and their `aliases.search_names`).
# Only company names/short names are listed here (no product names like H100/B200/B300).
SUPPLEMENTAL_TICKER_ALIASES: dict[str, tuple[str, ...]] = {
    "688008.SS": ("澜起科技", "澜起", "Montage"),
    "688008.SH": ("澜起科技", "澜起", "Montage"),
    "688041.SS": ("海光信息", "海光", "Hygon"),
    "688041.SH": ("海光信息", "海光", "Hygon"),
    "688775.SH": ("影石创新", "影石", "Insta360"),
    "688775.SS": ("影石创新", "影石", "Insta360"),
    "300750.SZ": ("宁德时代", "宁德", "CATL"),
    "0700.HK": ("腾讯控股", "腾讯", "Tencent"),
    "3606.HK": ("福耀玻璃", "福耀", "Fuyao"),
    "600660.SS": ("福耀玻璃", "福耀", "Fuyao"),
    "000333.SZ": ("美的集团", "美的", "Midea"),
    "002714.SZ": ("牧原股份", "牧原食品", "牧原", "Muyuan"),
    "300413.SZ": ("芒果超媒",),
    "002028.SZ": ("思源电气", "思源", "Sieyuan"),
    "002050.SZ": ("三花智控", "三花", "Sanhua"),
    "000719.SZ": ("中原传媒",),
    "5803.T": ("藤仓", "Fujikura"),
    "6857.T": ("爱德万", "爱德万测试", "Advantest"),
    "SNPS": ("Synopsys", "新思科技", "新思"),
    "AVGO": ("Broadcom", "博通"),
    "NVDA": ("Nvidia", "英伟达"),
    "INTC": ("Intel", "英特尔"),
    "TSM": ("Taiwan Semiconductor", "TSMC", "台积电"),
    "AMZN": ("Amazon", "亚马逊"),
    "BABA": ("阿里巴巴", "Alibaba", "阿里"),
    "IQ": ("iQIYI", "爱奇艺"),
    "HUBS": ("HubSpot",),
    "EFX": ("Equifax",),
    "TRU": ("TransUnion",),
}

# Tickers of research/data providers that are frequently cited as sources rather than discussed subjects.
SOURCE_INSTITUTION_TICKERS: dict[str, tuple[str, ...]] = {
    "IT": ("Gartner",),
}

_ENGLISH_MONTH_NUMBERS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def canonicalize_ticker_key(code: str) -> str:
    """Unify HK leading zeros (`700.HK`/`0700.HK`/`00700` -> `'0700.HK'`) and Shanghai suffixes (`.SH` -> `.SS`)."""
    s = str(code or "").strip().upper()
    if not s:
        return ""
    hk_m = re.match(r"^0*(\d{1,5})\.HK$", s)
    if hk_m:
        return f"{int(hk_m.group(1)):04d}.HK"
    if re.match(r"^0\d{3,4}$", s):
        return f"{int(s):04d}.HK"
    sh_m = re.match(r"^(\d{6})\.(?:SS|SH)$", s)
    if sh_m:
        return f"{sh_m.group(1)}.SS"
    return s


def ticker_code_variants(code: str) -> tuple[str, ...]:
    """Return equivalent ticker code representations (unifying HK leading zeros and market suffixes)."""
    s = str(code or "").strip().upper()
    if not s:
        return ()
    out: list[str] = []

    def _add(val: str) -> None:
        if val and val not in out:
            out.append(val)

    _add(s)
    hk_m = re.match(r"^0*(\d{1,5})\.HK$", s)
    if not hk_m and re.match(r"^0\d{3,4}$", s):
        hk_m = re.match(r"^0*(\d{1,5})$", s)
    if hk_m:
        num = int(hk_m.group(1))
        _add(f"{num:04d}.HK")
        _add(f"{num}.HK")
        _add(f"{num:05d}.HK")
        _add(f"{num:05d}")
        _add(f"{num:04d}")
        if num >= 100:
            _add(str(num))
        return tuple(out)

    sh_m = re.match(r"^(\d{6})\.(?:SS|SH)$", s)
    if sh_m:
        digits = sh_m.group(1)
        _add(f"{digits}.SS")
        _add(f"{digits}.SH")
        _add(digits)
        return tuple(out)

    mkt_m = re.match(r"^(\d{4,6})\.(SZ|T|TW)$", s)
    if mkt_m:
        digits, sfx = mkt_m.group(1), mkt_m.group(2)
        _add(f"{digits}.{sfx}")
        _add(digits)
        return tuple(out)

    if re.match(r"^\d{6}$", s):
        if s.startswith("6"):
            _add(f"{s}.SS")
            _add(f"{s}.SH")
        elif s.startswith(("0", "3")):
            _add(f"{s}.SZ")

    return tuple(out)


@lru_cache(maxsize=1)
def _load_ticker_company_aliases() -> dict[str, tuple[str, ...]]:
    """Load ticker -> company name aliases from `config/dalton/coverage.yaml` plus `SUPPLEMENTAL_TICKER_ALIASES`,
    keyed by `canonicalize_ticker_key`.
    """
    mapping: dict[str, list[str]] = {}

    def _register(raw_ticker: str, aliases: list[str]) -> None:
        canon_key = canonicalize_ticker_key(raw_ticker)
        if not canon_key:
            return
        cur = mapping.setdefault(canon_key, [])
        for v in ticker_code_variants(raw_ticker):
            if v not in cur:
                cur.append(v)
        for a in aliases:
            a_str = str(a or "").strip()
            if a_str and a_str not in cur:
                cur.append(a_str)

    cov_path = Path(__file__).resolve().parents[3] / "config" / "dalton" / "coverage.yaml"
    if cov_path.is_file():
        try:
            doc = yaml.safe_load(cov_path.read_text(encoding="utf-8")) or {}
            for entry in doc.get("companies") or []:
                if not isinstance(entry, dict):
                    continue
                ticker = str(entry.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                names = [ticker]
                disp_name = str(entry.get("name") or "").strip()
                if disp_name:
                    names.append(disp_name)
                search_names = (entry.get("aliases") or {}).get("search_names") or []
                for sn in search_names:
                    sn_str = str(sn or "").strip()
                    if sn_str and sn_str not in names:
                        names.append(sn_str)
                _register(ticker, names)
        except Exception:
            pass

    for ticker, extra_names in SUPPLEMENTAL_TICKER_ALIASES.items():
        _register(ticker, list(extra_names))

    return {k: tuple(v) for k, v in mapping.items()}


def normalize_number_token(raw_token: str) -> str | None:
    """Normalize a numeric token (stripping commas, signs, `%`, scientific notation) to a canonical decimal string
    (`1,200` -> `'1200'`, `35.0%` -> `'35'`, `1e6` -> `'1000000'`).
    """
    cleaned = str(raw_token or "").replace(",", "").replace("%", "").strip()
    if not cleaned:
        return None
    try:
        dec = abs(Decimal(cleaned))
    except InvalidOperation:
        return None
    if dec == dec.to_integral():
        return str(int(dec))
    return format(dec.normalize(), "f")


# Ordered longest-to-shortest: 万亿 > 亿 > 千万/百万 > 万 > 千/百
_CN_MAGNITUDE_UNITS: tuple[tuple[str, Decimal], ...] = (
    ("万亿", Decimal("1000000000000")),
    ("亿", Decimal("100000000")),
    ("千万", Decimal("10000000")),
    ("百万", Decimal("1000000")),
    ("万", Decimal("10000")),
    ("千", Decimal("1000")),
    ("百", Decimal("100")),
)

_EN_MAGNITUDE_MULTIPLIERS: dict[str, Decimal] = {
    "tn": Decimal("1000000000000"),
    "trillion": Decimal("1000000000000"),
    "bn": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
    "b": Decimal("1000000000"),
    "yi": Decimal("100000000"),
    "mn": Decimal("1000000"),
    "million": Decimal("1000000"),
    "m": Decimal("1000000"),
    "wan": Decimal("10000"),
    "k": Decimal("1000"),
    "thousand": Decimal("1000"),
}

_CN_DIGIT_MAP: dict[str, int] = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_CN_SMALL_UNIT_MAP: dict[str, int] = {
    "十": 10,
    "百": 100,
    "千": 1000,
}


def _resolve_unit_multiplier(unit_raw: str | None) -> Decimal | None:
    """Resolve a single magnitude multiplier for a Chinese or structured English unit token."""
    if not unit_raw:
        return None
    u_compact = re.sub(r"\s+", "", str(unit_raw)).strip()
    if not u_compact:
        return None
    for cn_u, mult in _CN_MAGNITUDE_UNITS:
        if u_compact.startswith(cn_u):
            return mult
    u_lower = u_compact.lower()
    if u_lower in _EN_MAGNITUDE_MULTIPLIERS:
        return _EN_MAGNITUDE_MULTIPLIERS[u_lower]
    if "_" in u_lower:
        for part in (u_lower.rsplit("_", 1)[-1], u_lower.split("_", 1)[0]):
            if part in _EN_MAGNITUDE_MULTIPLIERS:
                return _EN_MAGNITUDE_MULTIPLIERS[part]
    return None


def _parse_chinese_number(cn_text: str) -> Decimal | None:
    """Parse a Chinese numeral string (e.g. `一`, `两`, `十`, `百`, `千`, `万`, `十万`, `一百万`, `千万`) into a `Decimal`."""
    s = str(cn_text or "").strip()
    if not s:
        return None
    total = 0
    section = 0
    digit = 0
    has_any = False
    for ch in s:
        if ch in _CN_DIGIT_MAP:
            digit = _CN_DIGIT_MAP[ch]
            has_any = True
        elif ch in _CN_SMALL_UNIT_MAP:
            unit_val = _CN_SMALL_UNIT_MAP[ch]
            eff_digit = digit if digit > 0 else 1
            section += eff_digit * unit_val
            digit = 0
            has_any = True
        elif ch == "万":
            section += digit
            digit = 0
            eff_sec = section if section > 0 else (1 if total == 0 else 0)
            total += eff_sec * 10000
            section = 0
            has_any = True
        elif ch == "亿":
            section += digit
            digit = 0
            combined = total + section
            eff_comb = combined if combined > 0 else 1
            total = eff_comb * 100000000
            section = 0
            has_any = True
        else:
            return None
    if not has_any:
        return None
    return Decimal(total + section + digit)


@lru_cache(maxsize=1)
def _chinese_company_aliases_for_stripping() -> tuple[str, ...]:
    """Return multi-character Chinese company aliases sorted longest-first so company names
    containing numeral characters (e.g. `百度`, `百融云创`, `爱德万测试`, `万事达`, `三花智控`) are not parsed as numbers.
    """
    alias_map = _load_ticker_company_aliases()
    cn_names: set[str] = set()
    for aliases in alias_map.values():
        for a in aliases:
            if a and not a.isascii() and len(a) >= 2:
                cn_names.add(a)
    cn_names.update({"百融云创", "百济神州", "百胜中国", "五粮液", "三六零", "一汽"})
    return tuple(sorted(cn_names, key=len, reverse=True))


# Extensible table of metric units (percentage/ratio/multiple, currency, physical units, and counting classifiers).
METRIC_UNITS_TABLE: tuple[str, ...] = (
    # 1. Percentage, ratio, basis points, multiples
    r"%",
    r"‰",
    r"(?<![A-Za-z])(?:pct|pp|bps|bp)(?![A-Za-z])",
    r"(?<=\d)\s*[xX](?![A-Za-z0-9])",
    r"个?\s*百分点",
    r"个?\s*基点",
    r"倍",
    r"成(?!不变|为|立|功|熟|果|本|员|长|交|效|品)",
    r"折(?!旧|扣|合|算|价|让|射|叠)",
    # 2. Currency units
    r"美元(?:/[A-Za-z]+)?",
    r"港元",
    r"港币",
    r"日元",
    r"欧元",
    r"英镑",
    r"人民币",
    r"美金",
    r"元(?:/(?:Wh|kWh|W|kg|吨|件|台|股))?",
    r"块(?!板|体)",
    r"(?<![A-Za-z])(?:USD|CNY|RMB|HKD|JPY|EUR|GBP|yuan|dollars?|cents?)(?![A-Za-z])",
    # 3. Physical units
    r"(?<![A-Za-z])(?:GWh|MWh|kWh|Wh|GW|MW|kW|W|Tbps|Gbps|Mbps|TB|GB|PB|MB|KB|GHz|MHz|Hz|kg|km|nm|mm|cm|fps|G|T)(?![A-Za-z0-9])",
    r"吉瓦时",
    r"兆瓦时",
    r"千瓦时",
    r"瓦时",
    r"吉瓦",
    r"兆瓦",
    r"千瓦",
    r"瓦",
    r"万吨",
    r"吨",
    r"公斤",
    r"千克",
    r"克(?!服|隆)",
    r"千米",
    r"公里",
    r"米",
    r"平方米",
    r"平米",
    r"万平",
    # 4. Counting & duration classifiers
    r"(?<![A-Za-z])(?:shares?|pieces?|units?|lots?|tons?|downloads?|users?|subscribers?|installs?|hours?|mins?|minutes?|seconds?|days?|weeks?|months?|years?)(?![A-Za-z])",
    r"艘次",
    r"艘",
    r"个(?!别|性|体|案|月)",
    r"段(?!落|位)",
    r"家(?!庭|族|长|伙)",
    r"只(?!是|要|有|能|好|得|管|顾|怕|见|言)",
    r"次(?!日|年|月|周|席|第|级|要|数|序|元|性)",
    r"名(?!义|誉|单|字|称|额|片|气|声|次)",
    r"位(?!置|于|列|居|数|次)",
    r"条(?!件|款|例|目|文|理|线)",
    r"项(?!目|下)",
    r"款(?!项|式)",
    r"台(?!阶|湾|北|积|风|词|面)",
    r"套(?!路|现|利|保|餐)",
    r"份(?!额|子|内)",
    r"轮(?!回|流|番|廓|胎|船|动)",
    r"批(?!准|复|评|判|发|量|露)",
    r"季度",
    r"季(?!度|节|风|报)",
    r"年(?!期|代|纪|龄|轻|底|初|中|末|度|报|内|来|前|后|头|关)",
    r"个月",
    r"月(?!底|初|中|末|份|度|报|均|线)",
    r"天(?!气|花|下|然|生|津|猫|堂|线|数)",
    r"日(?!期|常|益|趋|渐|后|前|内|本|元|报|均|线)",
    r"周(?!期|末|围|边|年|转|线|报|一|二|三|四|五|六|日)",
    r"笔",
    r"单(?!日|天|月|年|周|季|位|独|纯|边|一|价|元|子|词)",
    r"股(?!份|票|东|权|价|本|息|指|市)",
    r"人(?!民|士|员|才|物|群|均|数|口|工|类|生)",
    r"户",
    r"颗",
    r"片",
    r"张",
    r"辆",
    r"架",
    r"座",
    r"件",
    r"州",
    r"席",
    r"门店",
    r"店",
    r"小时",
    r"分钟",
    r"秒",
)

_CURRENCY_PREFIX_RE = (
    r"(?:US\$|HK\$|NT\$|S\$|A\$|C\$|CA\$|\$|¥|￥|€|£"
    r"|(?<![A-Za-z])(?:USD|CNY|RMB|HKD|JPY|EUR|GBP|CHF|SGD|AUD|CAD)(?![A-Za-z])"
    r"|人民币|美元|港元|港币|日元)"
)

# Ordered longest-to-shortest: 万亿 > 亿 > 千万 > 百万 > 万 > 千 > 百, plus English magnitude tokens
_UNIT_CORE = (
    r"(?:万\s*亿|亿|千\s*万|百\s*万|万|千|百"
    r"|(?<![A-Za-z])(?:[A-Za-z]+_)?(?:tn|bn|mn|yi|wan|trillion|billion|million)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:[A-Za-z]+_)?[KMBkmb](?![A-Za-z0-9²³/]))"
)
_METRIC_POST_UNIT_RE = "(?:" + "|".join(METRIC_UNITS_TABLE) + ")"
_POST_RANGE_UNIT_GUARD = rf"(?![\d.%‰]|\s*(?:{_UNIT_CORE}|{_METRIC_POST_UNIT_RE}))"

_MA_WINDOW_CORE = (
    r"(?:(?<![A-Za-z0-9])\d+\s*(?:MA|EMA|SMA|WMA)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:MA|EMA|SMA|WMA)\s*\d+(?![A-Za-z0-9])"
    r"|(?<!\d)\d+\s*[日天周月]\s*(?:移动平均|平?均)?线)"
)


def _strip_non_metric_tokens(text: str) -> str:
    """Remove URLs, stock tickers, dates/periods, technical indicator windows, and product/model codes
    before extracting metric numbers from `quote` or `claim`.
    """
    s = str(text or "")
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\[[A-Za-z]\d+\]", " ", s)
    s = re.sub(r"(?<!\d)\d{6,10}-\d{2}-\d{4,8}(?!\d)", " ", s)
    s = re.sub(r"(?<!\d)\d{12,18}(?!\d)", " ", s)
    s = re.sub(
        r"(?<![A-Za-z0-9])\d{1,6}\.(?:SS|SZ|SH|HK|T|TW)(?![A-Za-z0-9])",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    for cn_comp in _chinese_company_aliases_for_stripping():
        if cn_comp in s:
            s = s.replace(cn_comp, " ")
    # Dates: YYYY-MM-DD, W2026-10-03, and unit-free YYYY-MM
    s = re.sub(
        r"(?<!\d)W?(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])(?!\d)",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        rf"(?<!\d)W?(?:19|20)\d{{2}}[-/.](?:0?[1-9]|1[0-2]){_POST_RANGE_UNIT_GUARD}",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    # Multi-year lists/ranges and calendar years: 2026、2027、2028年, 2029 至 2030 年, 2023-2025年, 26-28年
    s = re.sub(
        r"(?<!\d)(?:(?:(?:19|20)\d{2}|2\d)\s*(?:[-–—~～至到、，,及与和]|to)\s*)+(?:(?:19|20)\d{2}|2\d)\s*年",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        rf"(?<!\d)(?:19|20)\d{{2}}\s*[-–—~～至到]\s*(?:19|20)\d{{2}}(?:\s*[QH][1-4]E?)?{_POST_RANGE_UNIT_GUARD}",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    # Dates: YYYY年M月D日, YYYY年M月, YYYY年, YYYY上下半年
    s = re.sub(
        r"(?<!\d)(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*(?:[-–—~～至到]\s*\d{1,2}\s*)?月(?:\s*\d{1,2}\s*(?:[-–—~～至到]\s*\d{1,2}\s*)?[日号])?)?",
        " ",
        s,
    )
    s = re.sub(r"(?<!\d)(?:19|20)\d{2}\s*[上下]半年", " ", s)
    # Dates: M月D日, M月, date-contextual M/D, and unit-free MM-DD
    s = re.sub(r"(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*[日号](?:\s*[-–—~～至到]\s*\d{1,2}\s*[日号]?)?", " ", s)
    s = re.sub(r"(?<!\d)\d{1,2}\s*(?:[-–—~～至到]\s*\d{1,2}\s*)?月", " ", s)
    s = re.sub(
        rf"(?<![\d.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])\s*[-–—~～至到]\s*(?:(?:0?[1-9]|1[0-2])/)?(?:0?[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}",
        " ",
        s,
    )
    s = re.sub(
        rf"(?:(?<=截至)|(?<=截止)|(?<=于)|(?<=自)|(?<=从)|(?<=在))\s*(?<![\d.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}"
        rf"|(?<![\d.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?=\s*(?:[日号]|收盘|开盘|盘[前中后]|起|当[日天周]|公布|发布|披露|财报|法说|电话会))",
        " ",
        s,
    )
    s = re.sub(
        rf"(?<![\d.])(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}",
        " ",
        s,
    )
    # Wall-clock timestamps like 22:12 (preserve single-digit ratios like 4:1)
    s = re.sub(r"(?<!\d)(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)", " ", s)
    # Quarter / half-year / fiscal year tokens
    s = re.sub(
        r"(?<![A-Za-z0-9])(?:[12]H\d{2,4}E?|[1-4]Q\d{2,4}E?|(?:19|20)\d{2}\s*[QH][1-4]E?|Q[1-4](?:[-_\s]*(?:19|20)\d{2}E?)?|H[12](?:[-_\s]*(?:19|20)\d{2}E?)?|FY\s*\d{2,4}E?)(?![A-Za-z0-9])",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    # Moving-average windows (`50MA`, `MA50`, `50日均线`, `50日线`, `200MA`) and return windows (`30D`, `50D`):
    # when immediately followed by a price (`跌破200MA!(125.23)`, `突破50MA(76.5)`, `50日均线（247.0）`, `低于50日均线115.6`),
    # strip the window number and mark the attached price with `$` so the price itself still participates in validation.
    s = re.sub(
        rf"{_MA_WINDOW_CORE}\s*[!！=：:]?\s*[（(]?\s*(?=\d)",
        " $ ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(_MA_WINDOW_CORE, " ", s, flags=re.IGNORECASE)
    s = re.sub(r"(?<![A-Za-z0-9])\d+\s*D(?![A-Za-z0-9])", " ", s, flags=re.IGNORECASE)
    s = re.sub(
        r"(?<!\d)(?:30|50)\s*[日天](?!\s*(?:免费|试用|账期|交期|交付|内|以上|左右|到期|留存|复购))",
        " ",
        s,
    )
    # Tenors and ordinals (e.g. `十年期`, `10年期`, `第5条`, `第3年`, `第二季度`)
    s = re.sub(r"(?:(?<!\d)\d+|[一二两三四五六七八九十百]+)\s*年期", " ", s)
    s = re.sub(
        r"第\s*(?:\d+|[一二两三四五六七八九十百千万]+)\s*(?:条|章|节|款|项|页|步|轮|次|季|季度|年|天|日|周|名|位|阶段|部分|批|期|号|届)",
        " ",
        s,
    )
    # Product, model, and filing identifiers: Qwen2.5, Qwen3-8B, WiFi7, H100, B200, B300, RTX PRO 5000, Dyna-2.1, Aptitude 8, Protocol 80, Point72, 13F, 10-Q
    s = re.sub(
        r"(?:Qwen|DeepSeek|Llama|GPT|Claude|Gemini|Dyna|Wi-?Fi|Aptitude|Protocol|Point)[-\s]*\d+(?:\.\d+)?[A-Za-z]*(?:[-_][A-Za-z0-9.]+)*",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"\bRTX\s+(?:PRO\s+)?\d{3,4}(?:\s*(?:Ti|SUPER|Ada))?\b",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(?<![A-Za-z0-9])(?:13F|10-[QK]|20-F|8-K|W-8BEN|4o|14A|18A(?:-P)?)(?![A-Za-z0-9])",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(?<![A-Za-z0-9])(?!(?:USD|CNY|RMB|HKD|JPY|EUR|GBP)(?![A-Za-z]))[A-Za-z]{1,4}\d+(?:\.\d+)?[A-Za-z]*(?![A-Za-z0-9])",
        " ",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"(?:^|[\n；;])\s*[（(]?[1-9][）)]", " ", s)
    s = re.sub(
        r"(?:第?[一二三四]季(?:度|报)|[上下]半年(?:报)?|周[一二三四五六日]|星期[一二三四五六日]|[一二三四五六七八九十]{1,2}月"
        r"|(?:这|那|每|同|前|上|下|过去|看|算|试|等|缓|盯)?(?<![十零〇百千万亿])一(?:周|个?季度|年(?:前|期)?|个月|份(?!额)|下|起|点(?:儿)?|次性|系列|揽子|方面|部分|整套|家(?:之言|电|居|庭))"
        r"|逐一|唯一|统一|一致|一旦|一般|一定|一直|一起|万一|进一步|一系列|一方面|一买一卖|一分钱|之一|一半"
        r"|一度|十分|一成不变|三番五次|三番四次|百分之百地|第三方)",
        " ",
        s,
    )
    return s


_ARABIC_NUM_CORE = r"(?:(?<!,)\d{1,3}(?:,\d{3})+(?![,\d])(?!,\S)|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_ARABIC_MENTION_RE = re.compile(
    rf"(?:(?P<cur1>{_CURRENCY_PREFIX_RE})\s*[-−+]?\s*|(?<![A-Za-z0-9_]))"
    rf"(?P<n1>{_ARABIC_NUM_CORE})"
    rf"(?:[多余]?\s*(?P<u1>{_UNIT_CORE}))?"
    rf"(?:[多余]?\s*(?P<p1>{_METRIC_POST_UNIT_RE}))?"
    rf"(?:\s*[-–—~～至到]\s*(?:(?P<cur2>{_CURRENCY_PREFIX_RE})\s*[-−+]?\s*)?"
    rf"(?P<n2>{_ARABIC_NUM_CORE})"
    rf"(?:[多余]?\s*(?P<u2>{_UNIT_CORE}))?"
    rf"(?:[多余]?\s*(?P<p2>{_METRIC_POST_UNIT_RE}))?)?"
)
_CN_NUM_MENTION_RE = re.compile(
    rf"(?P<cn>[一二两三四五六七八九十][零〇一二两三四五六七八九十百千万亿]*)"
    rf"(?![零〇一二两三四五六七八九十百千万亿])"
    rf"(?:[多余]?\s*(?P<post>{_METRIC_POST_UNIT_RE})"
    rf"|(?<=[十百千万亿])(?P<approx>[多余])"
    rf"|(?<=[百千万亿])(?!分之|地|般|方|姓|科|货))"
)

_FINANCIAL_CONTEXT_RE = re.compile(
    r"(?:\$|¥|￥|€|£|(?<![A-Za-z])(?:USD|CNY|RMB|HKD|JPY|EUR|GBP|yuan|dollars?|cents?|shares?|pieces?|units?|"
    r"downloads?|users?|subscribers?|installs?|revenue|sales|profit|loss|income|cost|capex|opex|depreciation|output|adv|arr|ebitda|valuation|cash|fcf|debt)(?![A-Za-z])"
    r"|收入|营收|利润|亏损|损失|成本|支出|折旧|产值|估值|市值|融资|金额|交易量|成交|下载|用户|装机|元|美元|港元|港币|人民币)",
    flags=re.IGNORECASE,
)


def _is_short_en_mag_allowed(
    unit_str: str | None,
    *,
    text: str,
    span_start: int,
    span_end: int,
    cur_prefix: str | None,
    post_unit: str | None,
) -> bool:
    """Return True if `unit_str` is allowed as a magnitude multiplier.

    Uppercase `M`/`B`/`K` (e.g. `5M`, `> 5M downloads`, `$1.2B`) are treated as million/billion/thousand.
    Lowercase single-letter `m`/`k`/`b` (especially `m`, which can mean meters `100m`) is treated as a
    magnitude multiplier ONLY when accompanied by currency/amount/quantity context within the same clause.
    """
    if not unit_str:
        return False
    u_clean = re.sub(r"\s+", "", unit_str).strip()
    u_tail = u_clean.rsplit("_", 1)[-1]
    if u_tail.lower() not in ("k", "m", "b"):
        return True
    if u_tail in ("K", "M", "B"):
        return True
    if cur_prefix or post_unit:
        return True
    left_Window = text[max(0, span_start - 25) : span_start]
    right_window = text[span_end : min(len(text), span_end + 25)]
    left_clause = re.split(r"[，,。；;！!？?\n]", left_Window)[-1]
    right_clause = re.split(r"[，,。；;！!？?\n]", right_window)[0]
    return bool(_FINANCIAL_CONTEXT_RE.search(left_clause + " " + right_clause))


def _build_number_forms(raw_num: str, unit_str: str | None = None) -> tuple[str | None, set[str]]:
    """Build `(raw_canon, {raw_canon, expanded_canon})` for a single numeric token and optional unit."""
    canon = normalize_number_token(raw_num)
    if canon is None:
        return None, set()
    forms = {canon}
    mult = _resolve_unit_multiplier(unit_str)
    if mult is not None:
        try:
            scaled = normalize_number_token(str(Decimal(canon) * mult))
            if scaled is not None:
                forms.add(scaled)
        except InvalidOperation:
            pass
    return canon, forms


def _parse_number_mentions_detailed(
    text: str, *, strip_non_metrics: bool = True
) -> list[tuple[str, set[str], bool]]:
    """Parse number mentions from `text` returning `(raw_canon, legal_forms, has_unit)` for each mention."""
    cleaned = _strip_non_metric_tokens(text) if strip_non_metrics else str(text or "")
    mentions: list[tuple[str, set[str], bool]] = []
    masked_chars = list(cleaned)

    for match in _ARABIC_MENTION_RE.finditer(cleaned):
        cur1, n1, u1, p1 = match.group("cur1"), match.group("n1"), match.group("u1"), match.group("p1")
        cur2, n2, u2, p2 = match.group("cur2"), match.group("n2"), match.group("u2"), match.group("p2")
        eff_cur = cur1 or cur2
        eff_post = p1 or p2

        if u1 and not _is_short_en_mag_allowed(
            u1,
            text=cleaned,
            span_start=match.start(),
            span_end=match.end(),
            cur_prefix=eff_cur,
            post_unit=eff_post,
        ):
            u1 = None
        if u2 and not _is_short_en_mag_allowed(
            u2,
            text=cleaned,
            span_start=match.start(),
            span_end=match.end(),
            cur_prefix=eff_cur,
            post_unit=eff_post,
        ):
            u2 = None

        eff_u1 = u1 if u1 is not None else u2
        has_unit_1 = bool(eff_cur or eff_u1 or eff_post)
        c1, f1 = _build_number_forms(n1, eff_u1)
        if c1 is not None:
            mentions.append((c1, f1, has_unit_1))
        if n2:
            has_unit_2 = bool(eff_cur or u2 or eff_u1 or eff_post)
            c2, f2 = _build_number_forms(n2, u2 or eff_u1)
            if c2 is not None:
                mentions.append((c2, f2, has_unit_2))
        for idx in range(match.start(), match.end()):
            masked_chars[idx] = " "

    remaining = "".join(masked_chars)
    for cn_match in _CN_NUM_MENTION_RE.finditer(remaining):
        cn_tok = cn_match.group("cn")
        full_dec = _parse_chinese_number(cn_tok)
        if full_dec is None:
            continue
        expanded_canon = normalize_number_token(str(full_dec))
        if expanded_canon is None:
            continue
        forms = {expanded_canon}
        raw_canon = expanded_canon
        for cn_u in ("万亿", "亿", "万", "千", "百"):
            if cn_tok.endswith(cn_u) and len(cn_tok) > len(cn_u):
                prefix_dec = _parse_chinese_number(cn_tok[: -len(cn_u)])
                if prefix_dec is not None:
                    p_canon = normalize_number_token(str(prefix_dec))
                    if p_canon is not None:
                        raw_canon = p_canon
                        forms.add(p_canon)
                break
        has_cn_unit = bool(cn_match.group("post") or any(cn_tok.endswith(u) for u in ("万亿", "亿", "万", "千", "百")))
        mentions.append((raw_canon, forms, has_cn_unit))

    return mentions


def parse_number_mentions(text: str, *, strip_non_metrics: bool = True) -> list[tuple[str, set[str]]]:
    """Parse number mentions from `text` (used identically for `quote` and `claim`).

    Each mention yields `(raw_canon, legal_forms)` where `legal_forms` contains ONLY the raw value
    and the single unit-expanded value (`万亿` > `亿` > `千万`/`百万` > `万` > `千`/`百` and `B`/`M`/`K`/`bn`/`mn`).
    """
    return [
        (raw_canon, forms)
        for raw_canon, forms, _has_unit in _parse_number_mentions_detailed(
            text, strip_non_metrics=strip_non_metrics
        )
    ]


def extract_quote_supported_numbers(quote: str) -> set[str]:
    """Extract all legal normalized number forms (raw + unit-expanded) supported by `quote`."""
    supported: set[str] = set()
    for _raw_canon, forms, _has_unit in _parse_number_mentions_detailed(quote, strip_non_metrics=True):
        supported.update(forms)
    return supported


def extract_claim_numbers(claim: str) -> list[str]:
    """Extract normalized quantitative metric number tokens (attached to units) from `claim`."""
    return [
        raw_canon
        for raw_canon, _forms, has_unit in _parse_number_mentions_detailed(claim, strip_non_metrics=True)
        if has_unit
    ]


def extract_figure_value_numbers(value: Any, unit: str | None = None) -> list[str]:
    """Extract normalized numeric tokens from `figures[].value` (supporting scalars, ranges, `>`/`<` bounds, and `1e6`)."""
    if value is None or isinstance(value, bool):
        return []
    mentions = parse_number_mentions(str(value), strip_non_metrics=False)
    return [raw_canon for raw_canon, _forms in mentions]


def _extract_figure_value_mentions(value: Any, unit: str | None = None) -> list[tuple[str, set[str]]]:
    """Extract `(raw_canon, forms_set)` for each numeric value in `figures[].value`, incorporating `figures[].unit`."""
    if value is None or isinstance(value, bool):
        return []
    base_mentions = parse_number_mentions(str(value), strip_non_metrics=False)
    unit_mult = _resolve_unit_multiplier(unit)
    if unit_mult is None:
        return base_mentions
    enriched: list[tuple[str, set[str]]] = []
    for raw_canon, forms in base_mentions:
        new_forms = set(forms)
        try:
            scaled = normalize_number_token(str(Decimal(raw_canon) * unit_mult))
            if scaled is not None:
                new_forms.add(scaled)
        except InvalidOperation:
            pass
        enriched.append((raw_canon, new_forms))
    return enriched


_HALF_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<h1>[12])H(?:(?:19|20)?\d{2}E?)?|(?P<y_pre>(?:19|20)?\d{2})?H(?P<h2>[12])E?)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_QUARTER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:(?P<q1>[1-4])Q(?:(?:19|20)?\d{2}E?)?|(?P<y_pre>(?:19|20)?\d{2})?Q(?P<q2>[1-4])E?)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_CN_QUARTER_MAP: tuple[tuple[str, str], ...] = (
    (r"第?一季度|首季", "1"),
    (r"第?二季度", "2"),
    (r"第?三季度", "3"),
    (r"第?四季度", "4"),
)


def _is_year_supported_in_quote(yr4: str, quote: str) -> bool:
    """Return True if 4-digit year `yr4` (e.g. `'2026'`) is explicitly present in `quote` (`2026`, `26年`, `26-28年`, `FY26`, `1H26`, `Q2 26`, `(23)`)."""
    yr2 = yr4[2:]
    if re.search(rf"(?<!\d){re.escape(yr4)}(?!\d)", quote):
        return True
    if re.search(rf"(?<!\d){re.escape(yr2)}\s*(?:[-–—~～至到]\s*\d{{2}}\s*)?年", quote):
        return True
    if re.search(
        rf"(?<![A-Za-z0-9])(?:FY|[12]H|H[12]|[1-4]Q|Q[1-4])\s*{re.escape(yr2)}E?(?![A-Za-z0-9])",
        quote,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        rf"(?<![A-Za-z0-9]){re.escape(yr2)}\s*(?:[QH][1-4])E?(?![A-Za-z0-9])",
        quote,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(rf"[（(]\s*{re.escape(yr2)}(?:[QH][1-4])?\s*[）)]{_POST_RANGE_UNIT_GUARD}", quote, flags=re.IGNORECASE):
        return True
    return False


def is_period_supported_by_quote(period: str, quote: str) -> bool:
    """Return True if `period` is empty, appears literally in `quote` (with alphanumeric boundaries),
    or has every structured component (year, half-year, quarter, month, day/range endpoint) supported by `quote`.
    """
    p = str(period or "").strip()
    if not p:
        return True
    q = str(quote or "")
    if not q.strip():
        return False

    # 1. Literal match (case-insensitive, spaces ignored within period, with alphanumeric boundaries at both ends):
    #    e.g. `30D`, `7月`, `2H2026`, or `current`, `annual`, `historical` (`H1` does NOT match inside `H100`).
    p_compact = re.sub(r"\s+", "", p)
    if re.match(r"^[A-Za-z]+(?:_[A-Za-z]+)*$", p_compact):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(p_compact)}(?![A-Za-z0-9])", q, flags=re.IGNORECASE):
            return True
    else:
        p_flex = r"\s*".join(re.escape(ch) for ch in p_compact)
        lead_bound = r"(?<![A-Za-z0-9])" if (p_compact[0].isascii() and p_compact[0].isalnum()) else ""
        trail_bound = r"(?![A-Za-z0-9])" if (p_compact[-1].isascii() and p_compact[-1].isalnum()) else ""
        if re.search(rf"{lead_bound}{p_flex}{trail_bound}", q, flags=re.IGNORECASE):
            return True

    # 2. Structured period component validation: every component present in `period` must be supported by `quote`
    years: list[str] = []
    for ym in re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", p):
        y4 = ym.group(1)
        if y4 not in years:
            years.append(y4)
    for y2m in re.finditer(
        r"(?<![A-Za-z0-9])(?:FY|[12]H|[1-4]Q)\s*(\d{2})E?(?![A-Za-z0-9])|(?<![A-Za-z0-9])(\d{2})\s*[QH][1-4]E?(?![A-Za-z0-9])",
        p,
        flags=re.IGNORECASE,
    ):
        y2 = y2m.group(1) or y2m.group(2)
        y4 = f"20{y2}"
        if y4 not in years:
            years.append(y4)

    halves: list[str] = []
    for hm in _HALF_TOKEN_RE.finditer(p):
        h_num = hm.group("h1") or hm.group("h2")
        if h_num and h_num not in halves:
            halves.append(h_num)
    if "上半年" in p and "1" not in halves:
        halves.append("1")
    if "下半年" in p and "2" not in halves:
        halves.append("2")

    quarters: list[str] = []
    for qm in _QUARTER_TOKEN_RE.finditer(p):
        q_num = qm.group("q1") or qm.group("q2")
        if q_num and q_num not in quarters:
            quarters.append(q_num)
    for cn_pat, q_num in _CN_QUARTER_MAP:
        if re.search(cn_pat, p) and q_num not in quarters:
            quarters.append(q_num)

    md_dates: list[tuple[int, int]] = []
    p_for_dates = p
    _ymd_re = re.compile(
        r"(?<!\d)((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})"
        r"(?:\s*(?:[-–—~～至到]|to)\s*(?:(\d{1,2})[-/])?(\d{1,2})(?![-/\d]))?(?!\d)",
        flags=re.IGNORECASE,
    )
    for dm in _ymd_re.finditer(p):
        mon_val = int(dm.group(2))
        md_dates.append((mon_val, int(dm.group(3))))
        if dm.group(5):
            mon2_val = int(dm.group(4)) if dm.group(4) else mon_val
            md_dates.append((mon2_val, int(dm.group(5))))
    p_for_dates = _ymd_re.sub(" ", p_for_dates)
    for mdm in re.finditer(
        r"(?<!\d)(?:(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])|(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01]))(?!\d|\s*月)",
        p_for_dates,
    ):
        mon_s = mdm.group(1) or mdm.group(3)
        day_s = mdm.group(2) or mdm.group(4)
        md_dates.append((int(mon_s), int(day_s)))
    for cn_dm in re.finditer(
        r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号](?:\s*[-–—~～至到]\s*(\d{1,2})\s*[日号]?)?",
        p,
    ):
        mon_val = int(cn_dm.group(1))
        md_dates.append((mon_val, int(cn_dm.group(2))))
        if cn_dm.group(3):
            md_dates.append((mon_val, int(cn_dm.group(3))))

    months: list[int] = []
    if not md_dates:
        for ymm in re.finditer(
            r"(?:19|20)\d{2}-(\d{1,2})(?:\s*[-–—~～至到]\s*(\d{1,2}))?\b",
            p,
        ):
            months.append(int(ymm.group(1)))
            if ymm.group(2):
                months.append(int(ymm.group(2)))
    for mm in re.finditer(r"(?<![A-Za-z0-9])M(\d{1,2})\b", p, flags=re.IGNORECASE):
        months.append(int(mm.group(1)))
    p_no_cn_dates = re.sub(
        r"(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*[日号](?:\s*[-–—~～至到]\s*\d{1,2}\s*[日号]?)?",
        " ",
        p,
    )
    for cn_mm in re.finditer(
        r"(?<!\d)(\d{1,2})\s*(?:[-–—~～至到]\s*(\d{1,2})\s*)?月",
        p_no_cn_dates,
    ):
        months.append(int(cn_mm.group(1)))
        if cn_mm.group(2):
            months.append(int(cn_mm.group(2)))
    for m_name, mon_num in _ENGLISH_MONTH_NUMBERS.items():
        if re.search(rf"\b{m_name}\b", p, flags=re.IGNORECASE):
            months.append(mon_num)

    # Check if `p` contains unrecognized words outside valid period syntax
    p_residual = _HALF_TOKEN_RE.sub(" ", p)
    p_residual = _QUARTER_TOKEN_RE.sub(" ", p_residual)
    p_residual = re.sub(
        r"(?:(?<!\d)(?:19|20)\d{2}(?!\d)|(?<![A-Za-z0-9])FY\s*\d{2,4}E?(?![A-Za-z0-9])"
        r"|(?<![A-Za-z0-9])M\d{1,2}\b|\b(?:to|by|W|E|CY)\b|[上下]半年|第?[一二三四]季度|首季"
        r"|\d{1,2}\s*[月日号]|年|\d{1,2}|[-–—~～至到/_\s])",
        " ",
        p_residual,
        flags=re.IGNORECASE,
    )
    for m_name in _ENGLISH_MONTH_NUMBERS:
        p_residual = re.sub(rf"\b{m_name}\b", " ", p_residual, flags=re.IGNORECASE)
    if p_residual.strip():
        return False

    if not (years or halves or quarters or md_dates or months):
        return False

    # Every year in `period` must be supported in `quote` ("全年"/"今年"/"明年" do NOT prove a specific year)
    for yr4 in years:
        if not _is_year_supported_in_quote(yr4, q):
            return False

    # Every half-year in `period` must be supported with strict alphanumeric boundaries (`H100`/`H20`/`H200` do NOT match)
    q_halves = {hm.group("h1") or hm.group("h2") for hm in _HALF_TOKEN_RE.finditer(q)}
    if "上半年" in q:
        q_halves.add("1")
    if "下半年" in q:
        q_halves.add("2")
    for h_num in halves:
        if h_num not in q_halves:
            return False

    # Every quarter in `period` must be supported with strict alphanumeric boundaries
    q_quarters = {qm.group("q1") or qm.group("q2") for qm in _QUARTER_TOKEN_RE.finditer(q)}
    for cn_pat, q_num in _CN_QUARTER_MAP:
        if re.search(cn_pat, q):
            q_quarters.add(q_num)
    for q_num in quarters:
        if q_num not in q_quarters:
            return False

    # Every date / range endpoint `(mon, day)` in `period` must be supported in `quote`
    for mon, day in md_dates:
        has_date = (
            bool(re.search(rf"(?<!\d)0?{mon}\s*[/月]\s*0?{day}(?!\d)", q))
            or bool(re.search(rf"(?<!\d)0?{mon}-0?{day}{_POST_RANGE_UNIT_GUARD}", q))
            or bool(
                re.search(
                    rf"(?<!\d)0?{mon}\s*[/月]\s*\d{{1,2}}\s*[日号]?\s*[-–—~～至到]\s*0?{day}(?!\d)",
                    q,
                )
            )
        )
        if not has_date:
            return False

    # Every month in `period` must be supported in `quote` (`7-12%` does NOT prove month 7)
    q_lower = q.lower()
    for mon in months:
        en_names = [k for k, v in _ENGLISH_MONTH_NUMBERS.items() if v == mon]
        has_en = any(bool(re.search(rf"\b{en}\b", q_lower)) for en in en_names)
        has_num_mon = (
            bool(
                re.search(
                    rf"(?<!\d)(?:19|20)\d{{2}}-0?{mon}(?:-(?:0?[1-9]|[12]\d|3[01]))?{_POST_RANGE_UNIT_GUARD}",
                    q,
                )
            )
            or bool(re.search(rf"(?<!\d)0?{mon}\s*(?:[-–—~～至到]\s*\d{{1,2}}\s*)?月", q))
            or bool(re.search(rf"(?<![\d.]){mon:02d}-(?:0[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}", q))
        )
        if not (has_en or has_num_mon):
            return False

    return True


AMBIGUOUS_SHORT_ALIAS_WORDS: dict[str, tuple[str, ...]] = {
    "美的": ("完美的", "精美的", "优美的", "甜美的", "柔美的"),
}


def _alias_matches_in_text(alias: str, text: str) -> bool:
    """Return True if company `alias` (or ticker code) matches in `text` with proper boundary rules."""
    if not alias or not text:
        return False
    if alias.isascii():
        if len(alias) <= 2 or alias.isdigit():
            return bool(re.search(rf"(?<![A-Za-z0-9.]){re.escape(alias)}(?![A-Za-z0-9.])", text))
        return bool(
            re.search(
                rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                text,
                flags=re.IGNORECASE,
            )
        )
    # Short Chinese company names (海光, 腾讯, 美的) are ordinary substrings in running text ("在海光",
    # "的腾讯"), so a CJK-boundary rule loses real mentions. Only names that collide with common
    # words carry an explicit list of the words to ignore.
    if len(alias) < 3:
        masked = text
        for word in AMBIGUOUS_SHORT_ALIAS_WORDS.get(alias, ()):
            masked = masked.replace(word, " ")
        return alias in masked
    return alias in text


_EXCLUSION_STOP_RE = (
    r"[，。；;：:！!？?\n后外并但而（）()]"
    r"|同时|继续|保留|持有|买入|增持|加仓|建仓|看多|看好"
)


def validate_tickers_against_quote(
    tickers: list[str],
    quote: str,
    *,
    cited_sources: list[str] | None = None,
) -> tuple[list[str], int]:
    """Filter `tickers` so only symbols or mapped company names (`coverage.yaml` + `SUPPLEMENTAL_TICKER_ALIASES`)
    present as discussed subjects in `quote` remain. Returns `(kept_tickers, tickers_dropped)`.
    """
    q = str(quote or "")
    # Extract short excluded target phrases immediately following "剔除/不含/排除/除去/未采用" (ignoring "不排除").
    # Phrase stops at "后/外/并/但/而/同时/继续/保留/持有..." or clause punctuation, not the end of the sentence.
    exclusion_Phrase_re = re.compile(
        rf"(?<!不)(?:剔除|排除|除去|不含|未采用)\s*((?:(?!{_EXCLUSION_STOP_RE}).)*)"
    )
    excluded_spans = [m.group(1) for m in exclusion_Phrase_re.finditer(q) if m.group(1).strip()]
    q_without_exclusions = exclusion_Phrase_re.sub(" ", q)

    q_no_generic_it = re.sub(
        r"(?:数据中心\s*IT|IT\s*(?:容量|服务|支出|基础设施|系统|设备|行业|部门|预算|人员|外包|架构|成本))",
        " ",
        q_without_exclusions,
        flags=re.IGNORECASE,
    )
    srcs = [str(s or "") for s in (cited_sources or [])]
    alias_map = _load_ticker_company_aliases()

    kept: list[str] = []
    dropped = 0
    for raw_t in tickers or []:
        t = str(raw_t or "").strip().upper()
        if not t:
            continue
        canon_t = canonicalize_ticker_key(t)

        if canon_t in SOURCE_INSTITUTION_TICKERS:
            src_names = SOURCE_INSTITUTION_TICKERS[canon_t]
            is_source_attr = any(any(sn.lower() in s.lower() for sn in src_names) for s in srcs) or any(
                bool(re.search(rf"\b{re.escape(sn)}\s*(?:估算|预测|预计|表示|称|报告|数据)", q))
                for sn in src_names
            )
            if is_source_attr and not re.search(r"(?<![A-Za-z0-9])IT(?![A-Za-z0-9])", q_no_generic_it):
                dropped += 1
                continue

        cand_names: list[str] = []
        for v in ticker_code_variants(t):
            if v not in cand_names:
                cand_names.append(v)
        for a in alias_map.get(canon_t, ()):
            if a not in cand_names:
                cand_names.append(a)

        # If any alias/code of this ticker is the target of an explicit exclusion phrase (e.g. "剔除WIT后...", "已剔除WIT。"), drop it.
        if excluded_spans and any(
            _alias_matches_in_text(alias, span) for span in excluded_spans for alias in cand_names
        ):
            dropped += 1
            continue

        target_q = q_no_generic_it if canon_t == "IT" else q_without_exclusions
        matched = any(_alias_matches_in_text(alias, target_q) for alias in cand_names)

        if matched or canon_t not in alias_map:
            # No name mapping means we cannot tell a missing mention from a name we do not know
            # (e.g. 海光 for 688041.SS); keep it rather than lose a real subject. Exclusions and
            # source institutions above still drop.
            if t not in kept:
                kept.append(t)
        else:
            dropped += 1

    return kept, dropped


@dataclass
class CardValidationResult:
    """Result of programmatic fidelity validation on a candidate card against its `quote`."""

    ok: bool = True
    status: str = "active"
    confidence: float = 0.8
    figures: list[dict[str, Any]] = field(default_factory=list)
    tickers: list[str] = field(default_factory=list)
    unsupported_claim_numbers: list[str] = field(default_factory=list)
    soft_unsupported_claim_numbers: list[str] = field(default_factory=list)
    soft_unsupported_numbers: int = 0
    figures_dropped: int = 0
    periods_cleared: int = 0
    tickers_dropped: int = 0


def validate_candidate_card(
    card: dict[str, Any],
    quote: str | None = None,
) -> CardValidationResult:
    """Validate candidate card's numbers (`claim` & `figures.value`), `figures.period`, and `tickers` against `quote`.

    - Figures whose `value` contains numbers absent from `quote` are deleted (`figures_dropped`).
    - Remaining figures whose non-empty `period` lacks year/quarter/month support in `quote` have `period` cleared (`periods_cleared`).
    - Tickers whose symbol and mapped company names (`coverage.yaml` + `SUPPLEMENTAL_TICKER_ALIASES`) are absent from `quote`
      (or appear only as excluded items / source institutions) are removed (`tickers_dropped`).
    - When `claim` contains metric numbers with units unsupported by `quote` (`unsupported_claim_numbers`), the card is
      NOT rejected (`ok=True`, `status='active'`); instead its `confidence` is capped at `min(confidence, 0.4)` and a
      `{"metric": "_verification", "value": "unsupported:<nums>", "unit": "", "period": ""}` marker is appended to `figures`.
      Unmatched bare numbers, years, dates, identifiers, and indicator windows are recorded in `soft_unsupported_numbers`.
    """
    eff_quote = str(quote if quote is not None else (card.get("quote") or ""))
    q_numbers = extract_quote_supported_numbers(eff_quote)

    raw_figures = card.get("figures") if isinstance(card.get("figures"), list) else []
    kept_figures: list[dict[str, Any]] = []
    figures_dropped = 0
    periods_cleared = 0

    for fig in raw_figures:
        if not isinstance(fig, dict) or fig.get("metric") is None or fig.get("value") is None:
            continue
        if str(fig.get("metric") or "").strip() == "_verification":
            continue
        fig_unit = str(fig.get("unit") or "").strip()
        fig_mentions = _extract_figure_value_mentions(fig.get("value"), unit=fig_unit)
        if not fig_mentions or any(forms.isdisjoint(q_numbers) for _raw, forms in fig_mentions):
            figures_dropped += 1
            continue
        raw_period = str(fig.get("period") or "").strip()
        if raw_period and not is_period_supported_by_quote(raw_period, eff_quote):
            eff_period = ""
            periods_cleared += 1
        else:
            eff_period = raw_period
        kept_figures.append(
            {
                "metric": str(fig.get("metric") or "").strip(),
                "value": fig.get("value"),
                "unit": fig_unit,
                "period": eff_period,
            }
        )

    raw_tickers = card.get("tickers") if isinstance(card.get("tickers"), list) else []
    raw_sources = card.get("cited_sources") if isinstance(card.get("cited_sources"), list) else []
    kept_tickers, tickers_dropped = validate_tickers_against_quote(
        [str(t) for t in raw_tickers if t is not None],
        eff_quote,
        cited_sources=[str(s) for s in raw_sources if s is not None],
    )

    claim_text = str(card.get("claim") or "")
    claim_mentions = _parse_number_mentions_detailed(claim_text, strip_non_metrics=True)
    unsupported_claim_nums: list[str] = []
    soft_unsupported_claim_nums: list[str] = []
    for raw_canon, forms, has_unit in claim_mentions:
        if forms.isdisjoint(q_numbers):
            if has_unit:
                # No enumeration-count exemption: it could not be bound to one mention, so a list of
                # three names also excused an unrelated "3%" in the same claim. A count like 三家 is
                # only flagged (confidence 0.4), never dropped, so a missed exemption costs little.
                unsupported_claim_nums.append(raw_canon)
            else:
                soft_unsupported_claim_nums.append(raw_canon)

    # Also count non-metric numeric tokens (years, dates, codes, indicator windows stripped from `claim`)
    # that do not appear in `quote` towards `soft_unsupported_numbers`.
    q_raw_tokens = {
        c
        for c in (normalize_number_token(m.group(0)) for m in re.finditer(_ARABIC_NUM_CORE, eff_quote))
        if c is not None
    } | q_numbers
    accounted_raw = [raw_canon for raw_canon, _f, _u in claim_mentions]
    remaining_accounted = list(accounted_raw)
    for m in re.finditer(_ARABIC_NUM_CORE, claim_text):
        c_tok = normalize_number_token(m.group(0))
        if c_tok is None:
            continue
        if c_tok in remaining_accounted:
            remaining_accounted.remove(c_tok)
            continue
        if c_tok in q_raw_tokens:
            continue
        if re.match(r"^(?:19|20)\d{2}$", c_tok) and _is_year_supported_in_quote(c_tok, eff_quote):
            continue
        soft_unsupported_claim_nums.append(c_tok)

    soft_unsupported_numbers = len(soft_unsupported_claim_nums)
    raw_conf = card.get("confidence", 0.8)
    try:
        orig_conf = float(raw_conf if raw_conf is not None else 0.8)
    except (TypeError, ValueError):
        orig_conf = 0.8
    orig_conf = max(0.0, min(1.0, orig_conf))

    if unsupported_claim_nums:
        eff_conf = min(orig_conf, 0.4)
        kept_figures.append(
            {
                "metric": "_verification",
                "value": f"unsupported:{','.join(unsupported_claim_nums)}",
                "unit": "",
                "period": "",
            }
        )
    else:
        eff_conf = orig_conf

    ok = True
    status = "active"

    card["figures"] = kept_figures
    card["tickers"] = kept_tickers
    card["confidence"] = eff_conf
    card["status"] = status

    return CardValidationResult(
        ok=ok,
        status=status,
        confidence=eff_conf,
        figures=kept_figures,
        tickers=kept_tickers,
        unsupported_claim_numbers=unsupported_claim_nums,
        soft_unsupported_claim_numbers=soft_unsupported_claim_nums,
        soft_unsupported_numbers=soft_unsupported_numbers,
        figures_dropped=figures_dropped,
        periods_cleared=periods_cleared,
        tickers_dropped=tickers_dropped,
    )


def _split_oversized_event(
    ev: dict[str, Any],
    max_chars: int,
    *,
    persisted_plan: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Split a single event whose `text` exceeds `max_chars` into chunks `<= max_chars` with stable `chunk_idx` and `chunk_spans`."""
    if ev.get("_chunk_expanded") is True and persisted_plan is None:
        return [ev]

    text = str(ev.get("text") or "")
    if isinstance(persisted_plan, dict):
        plan_limit = max(1, int(persisted_plan.get("chunk_max_chars") or max_chars))
        raw_spans = persisted_plan.get("chunk_spans")
        if isinstance(raw_spans, list) and raw_spans:
            valid_spans: list[list[int]] = []
            for sp in raw_spans:
                if isinstance(sp, (list, tuple)) and len(sp) == 2:
                    s = max(0, min(len(text), int(sp[0])))
                    e = max(s, min(len(text), int(sp[1])))
                    valid_spans.append([s, e])
            if valid_spans:
                return [
                    {
                        **ev,
                        "text": text[s:e],
                        "chunk_idx": idx,
                        "chunk_count": len(valid_spans),
                        "chunk_max_chars": plan_limit,
                        "chunk_spans": valid_spans,
                        "_chunk_expanded": True,
                    }
                    for idx, (s, e) in enumerate(valid_spans)
                    if text[s:e] or len(valid_spans) == 1
                ]
        limit = plan_limit
    else:
        limit = max(1, int(max_chars))

    if len(text) <= limit:
        spans = [[0, len(text)]]
        ev["chunk_idx"] = int(ev.get("chunk_idx", 0))
        ev["chunk_count"] = 1
        ev["chunk_max_chars"] = limit
        ev["chunk_spans"] = spans
        ev["_chunk_expanded"] = True
        return [ev]

    spans = []
    pos = 0
    rem_end = len(text)
    while (rem_end - pos) > limit:
        window = text[pos : pos + limit]
        split_at = -1
        for sep in ("\n\n", "\n", "。", "！", "？", ". ", "; ", "，", " "):
            found_pos = window.rfind(sep)
            if found_pos >= limit // 2:
                split_at = found_pos + len(sep)
                break
        if split_at <= 0:
            split_at = limit

        c_start = pos
        c_end = pos + split_at
        while c_start < c_end and text[c_start].isspace():
            c_start += 1
        while c_end > c_start and text[c_end - 1].isspace():
            c_end -= 1
        if c_end > c_start:
            spans.append([c_start, c_end])

        pos = pos + split_at
        rem_end = len(text.rstrip())
        while pos < rem_end and text[pos].isspace():
            pos += 1

    if pos < rem_end:
        c_start = pos
        c_end = rem_end
        while c_start < c_end and text[c_start].isspace():
            c_start += 1
        while c_end > c_start and text[c_end - 1].isspace():
            c_end -= 1
        if c_end > c_start:
            spans.append([c_start, c_end])

    return [
        {
            **ev,
            "text": text[s:e],
            "chunk_idx": idx,
            "chunk_count": len(spans),
            "chunk_max_chars": limit,
            "chunk_spans": spans,
            "_chunk_expanded": True,
        }
        for idx, (s, e) in enumerate(spans)
    ]


def expand_event_chunks(
    events: list[dict[str, Any]],
    *,
    max_segment_chars: int = 6000,
    persisted_plans: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expand raw events into deterministic per-event chunks (`chunk_idx = 0..N-1`), reusing `persisted_plans` when present."""
    eff_max_chars = max(1, int(max_segment_chars))
    expanded: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ev_key = (
            str(ev.get("agent") or "main"),
            str(ev.get("session_id") or ""),
            str(ev.get("event_id") or f"seq:{int(ev.get('seq', 0))}"),
        )
        plan = (persisted_plans or {}).get(ev_key)
        expanded.extend(
            _split_oversized_event(ev, eff_max_chars, persisted_plan=plan)
        )
    return expanded


def segment_events(
    events: list[dict[str, Any]],
    *,
    max_gap_minutes: int = 30,
    max_segment_chars: int = 6000,
    max_segment_events: int = 25,
) -> list[list[dict[str, Any]]]:
    """Group events by `(agent, session_id)` and split into time-adjacent topic segments."""
    by_session: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        key = (str(ev.get("agent") or "main"), str(ev.get("session_id") or ""))
        by_session.setdefault(key, []).append(ev)

    segments: list[list[dict[str, Any]]] = []
    max_gap_ms = max(1, max_gap_minutes) * 60 * 1000
    eff_max_chars = max(1, int(max_segment_chars))

    for _, sess_events in sorted(by_session.items()):
        ordered_raw = sorted(
            sess_events,
            key=lambda x: (
                int(x.get("seq", 0)),
                int(x.get("created_at_ms", 0)),
                int(x.get("chunk_idx", 0)),
            ),
        )
        ordered: list[dict[str, Any]] = []
        for raw_ev in ordered_raw:
            ordered.extend(_split_oversized_event(raw_ev, eff_max_chars))

        current_seg: list[dict[str, Any]] = []
        current_chars = 0
        last_ms: int | None = None

        for ev in ordered:
            ev_ms = int(ev.get("created_at_ms") or 0)
            ev_chars = len(str(ev.get("text") or ""))
            gap_exceeded = (
                last_ms is not None
                and ev_ms > 0
                and last_ms > 0
                and (ev_ms - last_ms) > max_gap_ms
            )
            size_exceeded = (
                len(current_seg) >= max_segment_events
                or (current_chars + ev_chars > eff_max_chars and len(current_seg) > 0)
            )
            if current_seg and (gap_exceeded or size_exceeded):
                segments.append(current_seg)
                current_seg = []
                current_chars = 0

            current_seg.append(ev)
            current_chars += ev_chars
            if ev_ms > 0:
                last_ms = ev_ms

        if current_seg:
            segments.append(current_seg)

    segments.sort(
        key=lambda seg: (
            int(seg[0].get("created_at_ms") or 0),
            str(seg[0].get("ts") or ""),
            str(seg[0].get("agent") or ""),
            str(seg[0].get("session_id") or ""),
            int(seg[0].get("seq") or 0),
            int(seg[0].get("chunk_idx") or 0),
        )
    )
    return segments


def _normalize_figure_key(metric: Any, period: Any) -> tuple[str, str]:
    m = re.sub(r"[\s_-]+", "", str(metric or "").strip().lower())
    p = re.sub(r"[\s_-]+", "", str(period or "").strip().upper())
    return m, p


def _normalize_figure_value(val: Any) -> str:
    if isinstance(val, (int, float)):
        return f"{float(val):.6g}"
    s = str(val or "").strip()
    try:
        return f"{float(s):.6g}"
    except ValueError:
        return s.lower()


def _parse_card_ts_epoch(card: dict[str, Any]) -> float:
    ts_val = card.get("ts")
    if isinstance(ts_val, datetime):
        dt = ts_val if ts_val.tzinfo else ts_val.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(ts_val, str) and ts_val.strip():
        s = ts_val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
    return 0.0


def _card_max_seq(card: dict[str, Any]) -> int:
    seq_range = card.get("seq_range")
    if isinstance(seq_range, list) and seq_range:
        try:
            return max(int(x) for x in seq_range)
        except Exception:
            return 0
    return 0


def _card_event_sort_key(card: dict[str, Any]) -> float:
    """Return strict event timestamp epoch for `supersedes` eligibility comparison."""
    return _parse_card_ts_epoch(card)


def _card_display_sort_key(card: dict[str, Any]) -> tuple[float, int, str]:
    """Return `(ts_epoch, max_seq, card_id)` for deterministic display ordering (`seq` never breaks ties for `supersedes`)."""
    return (_parse_card_ts_epoch(card), _card_max_seq(card), str(card.get("card_id") or ""))


def deduplicate_candidate_cards(
    candidate_cards: list[dict[str, Any]],
    *,
    existing_hashes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Deduplicate `candidate_cards` by `content_hash` and initialize all new cards with `supersedes=None`."""
    seen_hashes: set[str] = set(existing_hashes or set())
    accepted: list[dict[str, Any]] = []
    deduped_count = 0

    for card in candidate_cards:
        c_hash = str(card.get("content_hash") or "")
        if not c_hash:
            c_hash = compute_content_hash(
                str(card.get("quote") or ""), str(card.get("quote_event_id") or "")
            )
            card["content_hash"] = c_hash

        if c_hash in seen_hashes:
            deduped_count += 1
            continue

        seen_hashes.add(c_hash)
        card["status"] = "active"
        card["supersedes"] = None
        card["superseded_card_ids"] = []
        card["superseded_by_card_id"] = None
        accepted.append(card)

    return accepted, deduped_count


def compute_batch_supersedes(
    new_cards: list[dict[str, Any]],
    *,
    existing_active_cards: list[dict[str, Any]] | None = None,
) -> int:
    """Compute `(ticker, metric, period)` `supersedes` relationships after all `new_cards` exist.

    Eligibility strictly compares event timestamps (`_card_event_sort_key`): cards with identical
    event timestamps never supersede one another, and all active cards in the same single-ticker
    `(ticker, metric, period)` group with a strictly earlier event timestamp are marked
    `status='superseded'` regardless of whether their numeric value changed.
    Returns `superseded_count`.
    """
    active_pool: list[dict[str, Any]] = list(existing_active_cards or [])
    superseded_count = 0

    indexed_new = list(enumerate(new_cards))
    indexed_new.sort(key=lambda pair: (_card_event_sort_key(pair[1]), pair[0]))

    for _, card in indexed_new:
        card.setdefault("status", "active")
        card.setdefault("supersedes", None)
        card.setdefault("superseded_card_ids", [])
        card.setdefault("superseded_by_card_id", None)

        card_tickers = [str(t).upper() for t in (card.get("tickers") or []) if str(t).strip()]
        card_figures = [f for f in (card.get("figures") or []) if isinstance(f, dict)]

        if len(card_tickers) == 1 and card_figures:
            single_ticker = card_tickers[0]
            card_ts = _card_event_sort_key(card)
            superseded_targets: list[dict[str, Any]] = []
            superseded_seen_ids: set[str] = set()
            newer_superseding_cards: list[dict[str, Any]] = []
            newer_seen_ids: set[str] = set()

            pool_ordered = sorted(
                active_pool,
                key=_card_display_sort_key,
                reverse=True,
            )
            for fig in card_figures:
                m_key, p_key = _normalize_figure_key(fig.get("metric"), fig.get("period"))
                if not m_key or not p_key:
                    continue

                for prev in pool_ordered:
                    if prev.get("status") != "active":
                        continue
                    prev_tickers = [
                        str(t).upper() for t in (prev.get("tickers") or []) if str(t).strip()
                    ]
                    if len(prev_tickers) != 1 or prev_tickers[0] != single_ticker:
                        continue
                    prev_ts = _card_event_sort_key(prev)

                    prev_figs = [pf for pf in (prev.get("figures") or []) if isinstance(pf, dict)]
                    for pf in prev_figs:
                        pm_key, pp_key = _normalize_figure_key(pf.get("metric"), pf.get("period"))
                        if (pm_key, pp_key) == (m_key, p_key):
                            pid = str(prev.get("card_id") or "")
                            if card_ts > prev_ts:
                                if pid and pid not in superseded_seen_ids:
                                    superseded_seen_ids.add(pid)
                                    superseded_targets.append(prev)
                            elif card_ts < prev_ts:
                                if pid and pid not in newer_seen_ids:
                                    newer_seen_ids.add(pid)
                                    newer_superseding_cards.append(prev)
                            break

            if superseded_targets:
                card["supersedes"] = str(superseded_targets[0]["card_id"])
                card["superseded_card_ids"] = [str(t["card_id"]) for t in superseded_targets]
                for target in superseded_targets:
                    target["status"] = "superseded"
                    superseded_count += 1

            if newer_superseding_cards and card.get("status") == "active":
                card["status"] = "superseded"
                card["supersedes"] = None
                card["superseded_card_ids"] = []
                newer_ordered_asc = sorted(newer_superseding_cards, key=_card_display_sort_key)
                earliest_newer = newer_ordered_asc[0]
                card["superseded_by_card_id"] = str(earliest_newer["card_id"])
                if not earliest_newer.get("supersedes"):
                    earliest_newer["supersedes"] = str(card["card_id"])
                superseded_count += 1

        active_pool.append(card)

    return superseded_count


def resolve_dedup_and_supersedes(
    candidate_cards: list[dict[str, Any]],
    *,
    existing_hashes: set[str] | None = None,
    existing_active_cards: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Deduplicate cards by `content_hash` first, then compute `(ticker, metric, period)` `supersedes`."""
    accepted, deduped_count = deduplicate_candidate_cards(
        candidate_cards,
        existing_hashes=existing_hashes,
    )
    superseded_count = compute_batch_supersedes(
        accepted,
        existing_active_cards=existing_active_cards,
    )
    return accepted, deduped_count, superseded_count


def _extract_error_Inspection(
    res: AssistResult | SegmentExtractionOutcome | BaseException | str,
) -> tuple[bool, int | None, str, str, str, str]:
    """Normalize `AssistResult`, `SegmentExtractionOutcome`, exception, or error string into `(is_ok, status_code, cls_name, status, error_kind, err_text)`."""
    if isinstance(res, AssistResult):
        err_text = str(res.error or "").lower()
        m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", err_text, re.IGNORECASE)
        code = int(m.group(1)) if m else None
        return (
            bool(res.ok),
            code,
            "assistresult",
            str(res.status or "").lower(),
            str(res.error_kind or "").lower(),
            err_text,
        )
    if isinstance(res, SegmentExtractionOutcome):
        err_text = str(res.error or "").lower()
        m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", err_text, re.IGNORECASE)
        code = int(m.group(1)) if m else None
        return (
            bool(res.ok),
            code,
            "segmentextractionoutcome",
            str(res.status or "").lower(),
            str(res.error_kind or "").lower(),
            err_text,
        )
    if isinstance(res, BaseException):
        status_code = getattr(res, "status_code", None) or getattr(
            getattr(res, "response", None), "status_code", None
        )
        if not isinstance(status_code, int):
            m = re.search(
                r"(?:http_|status[=_:\s]*)?([45]\d{2})\b",
                str(res or ""),
                re.IGNORECASE,
            )
            status_code = int(m.group(1)) if m else None
        cls_name = type(res).__name__.lower()
        status = str(getattr(res, "status", "") or "").lower()
        ek = str(getattr(res, "error_kind", "") or "").lower()
        err = f"{cls_name} {res}".lower()
        return False, status_code, cls_name, status, ek, err
    err_str = str(res or "").lower()
    m = re.search(r"(?:http_|status[=_:\s]*)?([45]\d{2})\b", err_str, re.IGNORECASE)
    code = int(m.group(1)) if m else None
    return False, code, "str", "", "", err_str


def is_in_flight_error(
    res: AssistResult | SegmentExtractionOutcome | BaseException | str,
) -> bool:
    """Return True if an extraction result or exception indicates a journal `in_flight` collision."""
    if isinstance(res, SegmentExtractionOutcome) and res.in_flight:
        return True
    is_ok, _status_code, cls_name, status, ek, err = _extract_error_Inspection(res)
    if is_ok:
        return False
    if status == "in_flight" or ek == "in_flight" or cls_name == "journalinflight":
        return True
    for marker in (
        "in_flight",
        "journal state is in_flight",
        "正在由另一个进程执行中",
    ):
        if marker in err:
            return True
    return False


def is_transient_network_error(
    res: AssistResult | SegmentExtractionOutcome | BaseException | str,
) -> bool:
    """Return True for transient network/upstream errors (`RemoteProtocolError`, `ReadError`, `ConnectError`, `ReadTimeout`, HTTP 502/503/504)."""
    if isinstance(res, SegmentExtractionOutcome) and res.transient_error:
        return True
    is_ok, status_code, cls_name, status, ek, err = _extract_error_Inspection(res)
    if (
        is_ok
        or is_permanent_quota_error(res)
        or is_rate_limit_or_quota_error(res)
        or is_in_flight_error(res)
    ):
        return False
    if status == "indeterminate" and ek == "indeterminate":
        return False
    if status_code in (502, 503, 504):
        return True
    if ek in ("stream_cut", "timeout", "http_502", "http_503", "http_504"):
        return True
    if cls_name in (
        "remoteprotocolerror",
        "readerror",
        "connecterror",
        "readtimeout",
        "connecttimeout",
        "writeerror",
        "writetimeout",
        "pooltimeout",
        "protocolerror",
        "timeouterror",
        "connectionerror",
        "connectionreseterror",
    ):
        return True
    for marker in (
        "remoteprotocolerror",
        "peer closed connection",
        "incomplete chunked read",
        "server disconnected",
        "readerror",
        "connecterror",
        "readtimeout",
        "connecttimeout",
        "writetimeout",
        "pooltimeout",
        "timed out",
        "http_502",
        "http_503",
        "http_504",
        "502 bad gateway",
        "503 service unavailable",
        "503 upstream",
        "504 gateway timeout",
    ):
        if marker in err:
            return True
    return False


def is_permanent_quota_error(res: AssistResult | BaseException | str) -> bool:
    """Return True if an `AssistResult` or exception failed due to a permanent/hard quota or billing limit."""
    is_ok, status_code, cls_name, status, ek, err = _extract_error_Inspection(res)
    if is_ok:
        return False
    if cls_name in ("permanentquotaerror", "budgetexhaustederror"):
        return True
    if status == "budget_exhausted" or ek in (
        "budget_exhausted",
        "task_cap_exhausted",
        "budget_db_error",
        "permanent_quota",
    ):
        return True
    if status_code == 402:
        return True
    for marker in (
        "permanent_quota",
        "billing_not_active",
        "billing disabled",
        "account_deactivated",
        "exceeded your current quota, please check your plan and billing",
        "monthly_spend_limit",
        "daily_budget_exhausted",
        "今日现金额度已用完",
    ):
        if marker in err:
            return True
    return False


def is_rate_limit_or_quota_error(res: AssistResult | BaseException | str) -> bool:
    """Return True if an `AssistResult` or exception failed due to transient 429 / rate-limit exhaustion."""
    is_ok, status_code, cls_name, _status, ek, err = _extract_error_Inspection(res)
    if is_ok or is_permanent_quota_error(res):
        return False
    if status_code == 429 or ek == "http_429" or "429" in ek:
        return True
    if any(
        k in cls_name
        for k in ("ratelimit", "rate_limit", "resourceexhausted", "toomanyrequests")
    ):
        return True
    for marker in (
        "429",
        "resource_exhausted",
        "quota",
        "rate limit",
        "rate_limit",
        "too many requests",
        "tokens per",
    ):
        if marker in err:
            return True
    return False


async def extract_segment_cards(
    segment: list[dict[str, Any]],
    *,
    extractor_model: str | None = None,
    prompt_version: str | None = None,
    user_id: str = LOCAL_DEV_USER_ID,
    llm_service: Any = None,
    resend_indeterminate: bool = False,
    persist_raw: bool = False,
    skip_journal: bool = False,
) -> SegmentExtractionOutcome:
    """Extract and validate knowledge cards from one conversation segment via `assist_json`."""
    cfg = get_dalton_settings().conversations
    eff_model = (extractor_model or cfg.extractor_model).strip()
    eff_prompt_ver = (prompt_version or cfg.prompt_version).strip()

    if not segment:
        return SegmentExtractionOutcome(ok=True, model_used=eff_model)

    first_ev = segment[0]
    last_ev = segment[-1]
    agent = str(first_ev.get("agent") or "main")
    session_id = str(first_ev.get("session_id") or "")
    session_key = str(first_ev.get("session_key") or session_id)
    generation = str(first_ev.get("generation") or "")
    channel = str(first_ev.get("channel") or "")
    chat_type = str(first_ev.get("chat_type") or "")
    seq_range = [int(first_ev.get("seq", 0)), int(last_ev.get("seq", 0))]
    all_event_ids = list(
        dict.fromkeys(str(ev.get("event_id") or "") for ev in segment if ev.get("event_id"))
    )

    payload = {
        "agent": agent,
        "session_id": session_id,
        "channel": channel,
        "seq_range": seq_range,
        "events": [
            {
                "event_id": str(ev.get("event_id") or ""),
                "seq": int(ev.get("seq", 0)),
                "ts": str(ev.get("ts") or ""),
                "speaker": ev.get("speaker") or {},
                "text": str(ev.get("text") or ""),
            }
            for ev in segment
        ],
    }

    seg_digest = hashlib.sha256(
        f"{agent}:{session_id}:{seq_range[0]}:{seq_range[1]}:{eff_prompt_ver}".encode("utf-8")
    ).hexdigest()[:20]
    scope_ref = f"conv_extract:{user_id}:{seg_digest}"

    billing_type, _ = resolve_model_billing_and_provider(eff_model)
    eff_resend_indeterminate = bool(resend_indeterminate and billing_type == "oauth")

    res = await assist_json(
        task="conversation.extract_cards",
        instructions=EXTRACTION_SYSTEM_INSTRUCTIONS,
        payload=payload,
        schema=EXTRACTION_RESPONSE_SCHEMA,
        user_id=user_id,
        role="conversation_extract",
        scope_ref=scope_ref,
        timeout_s=180.0,
        model=eff_model,
        llm_service=llm_service,
        resend_indeterminate=eff_resend_indeterminate,
        persist_raw=persist_raw,
        skip_journal=skip_journal,
    )

    usage = res.usage or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    tot_tok = int(usage.get("total_tokens") or (in_tok + out_tok))
    used_model = str(res.model or eff_model)
    is_cached = bool(getattr(res, "cached", False) or getattr(res, "replayed", False))
    is_replayed = bool(getattr(res, "replayed", False) or getattr(res, "cached", False))

    if not res.ok or not isinstance(res.data, dict):
        return SegmentExtractionOutcome(
            ok=False,
            model_used=used_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            rate_limited=is_rate_limit_or_quota_error(res),
            permanent_quota_error=is_permanent_quota_error(res),
            status=str(res.status or ""),
            error_kind=res.error_kind,
            in_flight=is_in_flight_error(res),
            transient_error=is_transient_network_error(res),
            cached=is_cached,
            replayed=is_replayed,
            error=res.error or res.status,
        )

    try:
        normalize_card_enum_fields(res.data)
    except ValueError as exc:
        return SegmentExtractionOutcome(
            ok=False,
            model_used=used_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            status="error",
            error_kind="ValidationError",
            cached=is_cached,
            replayed=is_replayed,
            error=str(exc),
        )

    category = str(res.data.get("category") or "research").strip().lower()
    seg_topic_label = str(res.data.get("topic_label") or "").strip()
    raw_cards = res.data.get("cards") if isinstance(res.data.get("cards"), list) else []

    if category in ("ops", "private"):
        return SegmentExtractionOutcome(
            ok=True,
            category=category,
            cards=[],
            cards_raw_count=len(raw_cards),
            discarded_segment=True,
            model_used=used_model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=tot_tok,
            cached=is_cached,
            replayed=is_replayed,
        )

    valid_cards: list[dict[str, Any]] = []
    quotes_rejected = 0
    cards_flagged_unsupported_number = 0
    soft_unsupported_numbers = 0
    figures_dropped = 0
    periods_cleared = 0
    tickers_dropped = 0

    for raw_c in raw_cards:
        if not isinstance(raw_c, dict):
            continue
        raw_quote = str(raw_c.get("quote") or "").strip()
        raw_ev_id = str(raw_c.get("quote_event_id") or "").strip()

        quote_ok, matched_ev = verify_verbatim_quote(raw_quote, raw_ev_id, segment)
        if not quote_ok or matched_ev is None:
            quotes_rejected += 1
            continue

        verified_ev_id = str(matched_ev.get("event_id") or raw_ev_id)
        c_hash = compute_content_hash(raw_quote, verified_ev_id)
        card_id = f"cc_{c_hash[:24]}"

        speaker, eff_type, cited_sources = enforce_speaker_and_agent_citation(
            str(raw_c.get("type") or "opinion"),
            raw_c.get("cited_sources") if isinstance(raw_c.get("cited_sources"), list) else [],
            matched_ev,
            quote=raw_quote,
            segment_events=segment,
        )
        tickers, entities = normalize_tickers_and_entities(
            raw_c.get("tickers") if isinstance(raw_c.get("tickers"), list) else [],
            raw_c.get("entities") if isinstance(raw_c.get("entities"), list) else [],
        )
        sectors = [
            str(s).strip()
            for s in (raw_c.get("sectors") or [])
            if s is not None and str(s).strip()
        ]
        figures: list[dict[str, Any]] = []
        for f in raw_c.get("figures") or []:
            if isinstance(f, dict) and f.get("metric") is not None and f.get("value") is not None:
                figures.append(
                    {
                        "metric": str(f.get("metric") or "").strip(),
                        "value": f.get("value"),
                        "unit": str(f.get("unit") or "").strip(),
                        "period": str(f.get("period") or "").strip(),
                    }
                )

        conf = float(raw_c.get("confidence", 0.8))
        conf = max(0.0, min(1.0, conf))

        ev_ts = str(matched_ev.get("ts") or first_ev.get("ts") or datetime.now(timezone.utc).isoformat())
        ev_date_et = str(matched_ev.get("date_et") or first_ev.get("date_et") or ev_ts[:10])
        topic_label = str(raw_c.get("topic_label") or seg_topic_label or "").strip()
        claim = str(raw_c.get("claim") or "").strip()
        if not claim:
            continue

        candidate_for_validation: dict[str, Any] = {
            "claim": claim,
            "figures": figures,
            "tickers": tickers,
            "quote": raw_quote,
            "cited_sources": cited_sources,
            "confidence": conf,
        }
        val_res = validate_candidate_card(candidate_for_validation, quote=raw_quote)
        figures_dropped += val_res.figures_dropped
        periods_cleared += val_res.periods_cleared
        tickers_dropped += val_res.tickers_dropped
        soft_unsupported_numbers += val_res.soft_unsupported_numbers
        if val_res.unsupported_claim_numbers:
            cards_flagged_unsupported_number += 1

        tickers = val_res.tickers
        figures = val_res.figures
        conf = val_res.confidence

        card_dict: dict[str, Any] = {
            "card_id": card_id,
            "source_type": "conversation",
            "agent": agent,
            "session_id": session_id,
            "session_key": session_key,
            "generation": str(matched_ev.get("generation") or generation),
            "channel": str(matched_ev.get("channel") or channel),
            "chat_type": str(matched_ev.get("chat_type") or chat_type),
            "event_ids": all_event_ids,
            "seq_range": seq_range,
            "ts": ev_ts,
            "date_et": ev_date_et,
            "speaker": speaker,
            "speaker_role": speaker["role"],
            "tickers": tickers,
            "entities": entities,
            "sectors": sectors,
            "topic_label": topic_label,
            "type": eff_type,
            "claim": claim,
            "figures": figures,
            "quote": raw_quote,
            "quote_event_id": verified_ev_id,
            "cited_sources": cited_sources,
            "confidence": conf,
            "status": val_res.status,
            "supersedes": None,
            "superseded_card_ids": [],
            "extractor_model": used_model,
            "prompt_version": eff_prompt_ver,
            "content_hash": c_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        card_dict["search_text"] = build_card_search_text(card_dict)
        valid_cards.append(card_dict)

    return SegmentExtractionOutcome(
        ok=True,
        category=category,
        cards=valid_cards,
        cards_raw_count=len(raw_cards),
        quotes_rejected=quotes_rejected,
        cards_flagged_unsupported_number=cards_flagged_unsupported_number,
        soft_unsupported_numbers=soft_unsupported_numbers,
        figures_dropped=figures_dropped,
        periods_cleared=periods_cleared,
        tickers_dropped=tickers_dropped,
        discarded_segment=False,
        model_used=used_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        cached=is_cached,
        replayed=is_replayed,
    )
