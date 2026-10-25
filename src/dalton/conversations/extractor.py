"""Container-side conversation segmentation, LLM card extraction, quote verification, and deduplication.

Follows Dalton's LLM contract (`assist_json` -> `dalton_complete` -> `LLMService.complete`,
never `create_llm()`), resolving the extraction model via `resolve_dalton_extract_model`.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.database.conversations import build_card_search_text
from src.dalton.llm.assist import AssistResult, _normalize_schema_enums, assist_json
from src.dalton.llm.complete import resolve_model_billing_and_provider
from src.dalton.llm.model_prefs import resolve_dalton_extract_model
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

DEFAULT_PROMPT_VERSION: str = "conv_card_v2_3"
DEFAULT_SEGMENT_DEADLINE_S: float = 600.0

# Private / unlisted companies must stay in `entities[]` rather than `tickers[]`
PRIVATE_COMPANIES: frozenset[str] = frozenset(
    {
        "OPENAI",
        "ANTHROPIC",
        "XAI",
        "SPACEX",
        "SPCX",
        "STRIPE",
        "DATABRICKS",
        "BYTEDANCE",
        "TIKTOK",
        "SHEIN",
        "0625.HK",
        "希音",
        "SHEIN 希音",
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
        "UNITREE",
        "宇树",
        "宇树科技",
        "月之暗面",
        "字节",
        "字节跳动",
        "小红书",
        "华为",
    }
)

# Trading / brokerage channel names and their corresponding listed ticker codes.
# Never backfilled or retained in `tickers[]` unless the company appears in `claim`
# as a discussed subject (i.e. without "买/下单/开户/可买/渠道" in the same sentence).
TRADING_CHANNEL_NAMES: tuple[str, ...] = (
    "盈透",
    "IBKR",
    "Interactive Brokers",
    "富途",
    "老虎",
    "雪球",
    "长桥",
    "嘉信",
    "Schwab",
    "Robinhood",
)

TRADING_CHANNEL_TICKERS: dict[str, tuple[str, ...]] = {
    "IBKR": ("盈透", "盈透证券", "IBKR", "Interactive Brokers"),
    "FUTU": ("富途", "富途控股", "Futu", "FUTU"),
    "TIGR": ("老虎", "老虎证券", "UP Fintech", "TIGR"),
    "SCHW": ("嘉信", "嘉信理财", "Charles Schwab", "Schwab", "SCHW"),
    "HOOD": ("Robinhood", "HOOD"),
}

_TRADING_CHANNEL_VERB_RE = re.compile(
    r"(?:(?<!不)(?:可买|可以买|能买|能够买)|买卖|(?<![存潜实现内外自费作信])(?:在|用|通过)[^。！？!?；;\n，,]{0,20}买(?![点入回方盘家单断壳])|下单|开户|渠道)"
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
   - **证据覆盖**：`claim` 的每个实质分句、`figures` 的每个指标，都必须由**本卡 `quote`** 直接支持；不能把同段对话里其他位置的信息写进本卡；覆盖不到的就拆卡或删掉；`cited_sources`、`topic_label` 不能替代证据。`claim` 和 `topic_label` 不得出现 `quote` 里没有的公司、机构、产品、说话人；不得出现 `quote` 里没有的结论（如“见顶”“止跌”“资金流”“费率对比”）；quote 里写的是英文全称时，claim 不要自行缩写成 quote 里没有出现的缩写词。
   - **主体补全禁令**：主体在 `quote` 里只是代词或省略时，扩大连续引用；扩大后仍然覆盖不到，就删掉这张卡或这一句，不得根据 `topic_label` 或相邻卡片补全。
   - **保留限定与归属**：保留“应该、可能、怀疑、初步、约、以上、超过、一度、用途含”等限定词，以及“尚无公开成果”“原文没打开”一类会影响证据强度的限制语，不得改成确定值或“证实/已经/全部用于”；保留条件、时间范围和主体归属；访谈体按“某某：”分段，上一段里的“我认为”不能并到下一位说话人；一条 `quote` 里有多家公司时，不得张冠李戴。
3. **卡片类型 (`type`) 与说话人来源 (`cited_sources`)**（按优先级定义，内容混合时优先拆卡）：
   - `"fact"`：已经发生、且本卡 `quote` 里有具体来源的实际数（若原话来自 `agent` 如 Eve，只有当本卡 `quote` 中明确给出了具体资料来源时才可标为 `"fact"` 并填入 `cited_sources`；无明确引用来源一律不得当作事实）。事实来源必须覆盖 `claim` 里每一句当作 `fact` 的数字句，只覆盖了其中一句就拆卡；“引用某报告”只覆盖报告明确披露的那几句，不覆盖随后自行推导的结论。
   - `"estimate"`：第三方模型预测、公司经营指引、他人的预测、一致预期、TAM 一律标 `"estimate"`（引用 Gartner 或卖方模型只说明来源，预测和指引本身标为 `"estimate"` 而非 `"fact"` 或 `"opinion"`）。
   - `"model_assumption"`：原说话人的测算，即原说话人自己做的倍数、倒算、情景推演（“我算”“隐含”“按全价算”“粗算”）（agent 自己的情景测算不要求外部来源，但数字必须由本卡 `quote` 支持）。
   - `"opinion"`：判断、建议、技术信号。
   - `"todo"`：带验证条件和到期日的待办，证实或证伪的阈值要写进 `claim`。
   - `cited_sources` 只填 `quote` 里实际出现的来源名称。
4. **上市公司 (`tickers`) vs 未上市私企 (`entities`)**：
   - `tickers` 只放本卡 `quote` 正文里作为讨论对象（本卡主体）出现的上市公司股票代码（大写）；股东、客户、被比较方如果不是本卡的主体，不放进 `tickers`；信源机构（如 Gartner 对应 `IT`）不放；写明“剔除/未采用”的不放；没有别名映射的代码，只有代码本身出现在 `quote` 里时才保留，否则删除；`quote` 没点名公司时留空 `[]`。
   - 未上市私企（如 `OpenAI`, `Anthropic`, `ByteDance`, `SpaceX`, `Stripe`, `Databricks`, `DeepSeek`, `Unitree`）放入 `entities`，**禁止**放入 `tickers`。
5. **结构化指标 (`figures`)**：
   - 当原话包含明确数值指标时，提取 `{metric, value, unit, period}`。
   - `value` 的数字必须出现在本卡 `quote` 里；“约、大约、左右、以上”保留在 `value` 字符串里（例如 `"约142"`、`"约95"`、`"10%左右"`、`"240以上"`），区间和上下限原样保留（可以用字符串，如 `">35"`、`"25-35"`）。
   - `quote` 没写期间时 `period` 留空 `""`，不得用消息日期或报告日期代替；“同期、上期”只能继承本卡 `quote` 里写明的期间。
   - 不得改写口径（年增速不是 CAGR，利润率不是营业利润率，自身增速不得改成某业务增速，未写同比/环比不得补写，仅写“亿”且本卡无法确定币种时不得补“美元”）；权重、占比、系数（如“权重为 0.25”）不要把 unit 写成“倍”。
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
    cards_flagged_unsupported_names: int = 0
    cards_flagged_unsupported_conclusion: int = 0
    soft_unsupported_numbers: int = 0
    figures_dropped: int = 0
    periods_cleared: int = 0
    currencies_stripped: int = 0
    tickers_dropped: int = 0
    tickers_dropped_private: int = 0
    tickers_dropped_channel: int = 0
    tickers_backfilled: int = 0
    types_corrected: int = 0
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


def _is_private_company_ticker(
    raw_ticker: str,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> bool:
    """Return True if `raw_ticker` or any of its mapped aliases resolves to `PRIVATE_COMPANIES`."""
    ts = str(raw_ticker or "").strip()
    if not ts:
        return False
    upper_t = ts.upper()
    if upper_t in PRIVATE_COMPANIES or ts in PRIVATE_COMPANIES:
        return True
    canon_t = canonicalize_ticker_key(upper_t)
    if canon_t in PRIVATE_COMPANIES:
        return True
    eff_alias_map = alias_map if alias_map is not None else _call_load_ticker_company_aliases(user_id)
    for a in eff_alias_map.get(canon_t, ()):
        if a in PRIVATE_COMPANIES or a.upper() in PRIVATE_COMPANIES:
            return True
    return False


def _is_trading_channel_discussed_in_claim(
    canon_ticker: str,
    claim: str | None,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> bool:
    """Return True if `canon_ticker` (in `TRADING_CHANNEL_TICKERS`) appears in `claim` and the sentence
    containing it does NOT include `'买'`, `'下单'`, `'开户'`, `'可买'`, or `'渠道'`.
    """
    c_text = str(claim or "").strip()
    if not c_text:
        return False
    eff_alias_map = alias_map if alias_map is not None else _call_load_ticker_company_aliases(user_id)
    names: list[str] = list(TRADING_CHANNEL_TICKERS.get(canon_ticker, ()))
    for a in eff_alias_map.get(canon_ticker, ()):
        if a not in names:
            names.append(a)
    for ch_name in TRADING_CHANNEL_NAMES:
        if ch_name not in names and any(ch_name.lower() == n.lower() for n in names):
            names.append(ch_name)
    sents = _split_sentences(c_text)
    if not sents:
        return False
    matched_sents = [
        s for s in sents if any(_alias_matches_in_text(na, s) for na in names)
    ]
    if not matched_sents:
        return False
    return any(not _TRADING_CHANNEL_VERB_RE.search(s) for s in matched_sents)


def normalize_tickers_and_entities(
    raw_tickers: list[Any] | None,
    raw_entities: list[Any] | None,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], list[str]]:
    """Route known private companies (OpenAI, Anthropic, SpaceX/SPCX, etc.) into `entities[]` and normalize tickers."""
    tickers: list[str] = []
    entities: list[str] = []

    for e in raw_entities or []:
        es = str(e or "").strip()
        if es and es not in entities:
            entities.append(es)

    eff_alias_map = alias_map if alias_map is not None else _call_load_ticker_company_aliases(user_id)
    for t in raw_tickers or []:
        ts = str(t or "").strip()
        if not ts:
            continue
        upper_t = ts.upper()
        if _is_private_company_ticker(ts, user_id=user_id, alias_map=eff_alias_map):
            canon_t = canonicalize_ticker_key(upper_t)
            entity_label = ts
            if upper_t == "SPCX":
                entity_label = "SpaceX"
            elif canon_t in eff_alias_map:
                for a in eff_alias_map[canon_t]:
                    if a.upper() != upper_t and not re.match(r"^\d", a):
                        entity_label = a
                        break
            if entity_label not in entities and entity_label.upper() not in {x.upper() for x in entities}:
                entities.append(entity_label)
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
    "6809.HK": ("澜起科技", "澜起", "Montage"),
    "688041.SS": ("海光信息", "海光", "Hygon"),
    "688041.SH": ("海光信息", "海光", "Hygon"),
    "688775.SH": ("影石创新", "影石", "Insta360"),
    "688775.SS": ("影石创新", "影石", "Insta360"),
    "300750.SZ": ("宁德时代", "宁德", "CATL"),
    "3750.HK": ("宁德时代", "宁德", "CATL"),
    "0700.HK": ("腾讯控股", "腾讯", "Tencent", "Tencent 腾讯"),
    "2513.HK": ("智谱", "智谱华章", "Zhipu", "Z.ai", "Zhipu 智谱 (Z.ai)"),
    "3696.HK": ("英矽智能", "英矽", "Insilico", "Insilico Medicine 英矽智能"),
    "GOOGL": ("Alphabet (Google)", "Alphabet", "Google", "谷歌"),
    "DASH": ("DoorDash",),
    "AFRM": ("Affirm", "Affirm Holdings"),
    "ECHO": ("EchoStar",),
    "DHI": ("D.R. Horton",),
    "VIPS": ("唯品会", "Vipshop"),
    "ATAT": ("亚朵", "Atour"),
    "BZ": ("Boss直聘", "BOSS直聘", "Kanzhun"),
    "AAPL": ("Apple",),
    "MSFT": ("Microsoft", "微软"),
    "SPCX": ("SpaceX",),
    "META": ("Meta Platforms", "Meta"),
    "TSLA": ("Tesla",),
    "0100.HK": ("Minimax", "MiniMax"),
    "NFLX": ("Netflix",),
    "SPOT": ("Spotify",),
    "MELI": ("MercadoLibre",),
    "SE": ("Sea",),
    "CPNG": ("Coupang",),
    "UBER": ("Uber",),
    "BKNG": ("Booking",),
    "ABNB": ("Airbnb",),
    "EXPE": ("Expedia",),
    "RBLX": ("Roblox",),
    "DUOL": ("Duolingo",),
    "APP": ("AppLovin",),
    "U": ("Unity Software", "Unity"),
    "RDDT": ("Reddit",),
    "MTCH": ("Match Group", "Match"),
    "JPM": ("JPMorgan",),
    "BAC": ("Bank of America", "BofA"),
    "V": ("Visa",),
    "MA": ("Mastercard", "万事达"),
    "SHOP": ("Shopify",),
    "PYPL": ("PayPal",),
    "XYZ": ("Block",),
    "CHYM": ("Chime",),
    "SOFI": ("SoFi",),
    "KLAR": ("Klarna",),
    "SEZL": ("Sezzle",),
    "UPST": ("Upstart",),
    "HOOD": ("Robinhood",),
    "RKT": ("Rocket Companies", "Rocket"),
    "FIGR": ("Figure",),
    "IBKR": ("Interactive Brokers", "盈透"),
    "BX": ("Blackstone",),
    "APO": ("Apollo",),
    "SPGI": ("S&P Global", "标普全球"),
    "MCO": ("Moody's", "穆迪"),
    "BRK-B": ("Berkshire Hathaway", "Berkshire", "BRK.B"),
    "PGR": ("Progressive",),
    "MRSH": ("Marsh McLennan", "Marsh"),
    "AON": ("Aon",),
    "AJG": ("Arthur J. Gallagher", "Gallagher"),
    "CME": ("CME Group",),
    "ICE": ("Intercontinental Exchange",),
    "CBOE": ("Cboe Global Markets", "Cboe"),
    "NDAQ": ("Nasdaq Inc", "纳斯达克", "Nasdaq"),
    "PLTR": ("Palantir",),
    "CRM": ("Salesforce",),
    "NET": ("Cloudflare",),
    "HURN": ("Huron Consulting", "Huron"),
    "PM": ("Philip Morris",),
    "WMT": ("Walmart",),
    "MAR": ("Marriott",),
    "HLT": ("Hilton",),
    "RKLB": ("Rocket Lab",),
    "PL": ("Planet Labs",),
    "PWR": ("Quanta Services", "Quanta"),
    "FIX": ("Comfort Systems",),
    "STRL": ("Sterling Infra", "Sterling"),
    "PHM": ("PulteGroup", "Pulte"),
    "TOL": ("Toll Brothers",),
    "BABA": ("阿里巴巴", "Alibaba", "阿里"),
    "PDD": ("拼多多", "PDD Holdings"),
    "NTES": ("网易", "NetEase", "NetEase 网易"),
    "3690.HK": ("美团", "Meituan", "MPNGY"),
    "JD": ("京东", "JD.com"),
    "BIDU": ("百度", "Baidu 百度"),
    "TCOM": ("携程", "Trip.com"),
    "BEKE": ("贝壳", "KE Holdings"),
    "0625.HK": ("希音", "Shein 希音"),
    "1024.HK": ("快手", "Kuaishou 快手"),
    "DIDIY": ("滴滴", "DiDi Global 滴滴"),
    "FUTU": ("富途", "Futu"),
    "ZTO": ("中通", "ZTO Express"),
    "6618.HK": ("京东健康", "JD Health 京东健康"),
    "TME": ("腾讯音乐", "Tencent Music 腾讯音乐"),
    "1519.HK": ("极兔速递", "J&T Global Express 极兔速递"),
    "YMM": ("满帮", "Full Truck Alliance 满帮"),
    "BILI": ("哔哩哔哩", "Bilibili 哔哩哔哩"),
    "JOYY": ("欢聚", "JOYY 欢聚"),
    "1860.HK": ("汇量科技", "Mobvista 汇量科技"),
    "2400.HK": ("心动", "XD Inc 心动"),
    "2556.HK": ("迈富时", "Marketingforce 迈富时"),
    "9992.HK": ("泡泡玛特", "Pop Mart 泡泡玛特"),
    "HTHT": ("华住", "H World", "H World Group"),
    "6181.HK": ("老铺黄金", "Laopu Gold 老铺黄金"),
    "0388.HK": ("港交所", "HKEX 港交所"),
    "601100.SS": ("恒立液压", "Hengli Hydraulic", "601100.SH"),
    "3606.HK": ("福耀玻璃", "福耀", "Fuyao"),
    "600660.SS": ("福耀玻璃", "福耀", "Fuyao"),
    "600660.SH": ("福耀玻璃", "福耀", "Fuyao"),
    "000333.SZ": ("美的集团", "美的", "Midea"),
    "0300.HK": ("美的集团", "美的", "Midea"),
    "000651.SZ": ("格力电器", "格力", "Gree"),
    "002714.SZ": ("牧原股份", "牧原食品", "牧原", "Muyuan"),
    "300413.SZ": ("芒果超媒", "芒果TV", "芒果 TV", "芒果台", "芒果"),
    "002028.SZ": ("思源电气", "思源", "Sieyuan"),
    "002050.SZ": ("三花智控", "三花", "Sanhua"),
    "2050.HK": ("三花智控", "三花", "Sanhua"),
    "600690.SS": ("海尔智家", "海尔", "Haier"),
    "600690.SH": ("海尔智家", "海尔", "Haier"),
    "6690.HK": ("海尔智家", "海尔", "Haier"),
    "002594.SZ": ("比亚迪", "BYD"),
    "1211.HK": ("比亚迪", "BYD"),
    "601888.SS": ("中国中免", "中免"),
    "601888.SH": ("中国中免", "中免"),
    "1880.HK": ("中国中免", "中免"),
    "600941.SS": ("中国移动", "中移动", "China Mobile"),
    "600941.SH": ("中国移动", "中移动", "China Mobile"),
    "0941.HK": ("中国移动", "中移动", "China Mobile"),
    "002352.SZ": ("顺丰控股", "顺丰", "SF Holding"),
    "6936.HK": ("顺丰控股", "顺丰", "SF Holding"),
    "603259.SS": ("药明康德", "WuXi AppTec"),
    "603259.SH": ("药明康德", "WuXi AppTec"),
    "2359.HK": ("药明康德", "WuXi AppTec"),
    "601899.SS": ("紫金矿业", "紫金", "Zijin"),
    "601899.SH": ("紫金矿业", "紫金", "Zijin"),
    "2899.HK": ("紫金矿业", "紫金", "Zijin"),
    "688981.SS": ("中芯国际", "中芯", "SMIC"),
    "688981.SH": ("中芯国际", "中芯", "SMIC"),
    "0981.HK": ("中芯国际", "中芯", "SMIC"),
    "3931.HK": ("中创新航",),
    "300014.SZ": ("亿纬锂能", "亿纬"),
    "300450.SZ": ("先导智能",),
    "603659.SS": ("璞泰来",),
    "603659.SH": ("璞泰来",),
    "600809.SS": ("山西汾酒", "汾酒"),
    "600809.SH": ("山西汾酒", "汾酒"),
    "000568.SZ": ("泸州老窖", "老窖"),
    "002304.SZ": ("洋河股份", "洋河"),
    "300308.SZ": ("中际旭创", "旭创", "Innolight"),
    "002463.SZ": ("沪电股份", "沪电", "WUS"),
    "002371.SZ": ("北方华创", "Naura"),
    "688012.SS": ("中微公司", "中微", "AMEC"),
    "688012.SH": ("中微公司", "中微", "AMEC"),
    "9880.HK": ("优必选", "UBTECH"),
    "9988.HK": ("阿里巴巴", "Alibaba", "阿里"),
    "9618.HK": ("京东", "JD.com", "JD"),
    "9999.HK": ("网易", "NetEase"),
    "9888.HK": ("百度", "Baidu"),
    "9961.HK": ("携程", "Trip.com"),
    "2423.HK": ("贝壳", "KE Holdings"),
    "9626.HK": ("哔哩哔哩", "Bilibili"),
    "1179.HK": ("华住", "H World"),
    "000719.SZ": ("中原传媒",),
    "300033.SZ": ("同花顺",),
    "1357.HK": ("美图公司", "美图", "Meitu"),
    "3317.HK": ("迅策", "Xunce"),
    "002230.SZ": ("科大讯飞", "讯飞"),
    "300058.SZ": ("蓝色光标",),
    "0268.HK": ("金蝶国际", "金蝶", "Kingdee"),
    "0354.HK": ("中软国际", "Chinasoft"),
    "2228.HK": ("晶泰控股", "晶泰科技", "晶泰", "XtalPi"),
    "9638.HK": ("法拉帝", "Ferretti"),
    "8035.T": ("Tokyo Electron", "东京电子"),
    "000660.KS": ("SK海力士", "SK Hynix", "Hynix", "海力士"),
    "5803.T": ("藤仓", "Fujikura"),
    "6857.T": ("爱德万", "爱德万测试", "Advantest"),
    "7581.T": ("萨莉亚", "Saizeriya"),
    "GTLB": ("GitLab",),
    "ESTC": ("Elastic",),
    "SNPS": ("Synopsys", "新思科技", "新思"),
    "AVGO": ("Broadcom", "博通"),
    "NVDA": ("Nvidia", "英伟达"),
    "INTC": ("Intel", "英特尔"),
    "TSM": ("Taiwan Semiconductor", "TSMC", "台积电"),
    "AMZN": ("Amazon", "亚马逊"),
    "NOW": ("ServiceNow",),
    "WDAY": ("Workday",),
    "PANW": ("Palo Alto", "Palo Alto Networks"),
    "SOUN": ("SoundHound",),
    "EL": ("雅诗兰黛", "Estée Lauder", "Estee Lauder"),
    "MKSI": ("MKS Instruments", "MKS"),
    "BBY": ("Best Buy",),
    "IQ": ("iQIYI", "爱奇艺"),
    "HUBS": ("HubSpot",),
    "AMD": ("AMD", "Advanced Micro Devices", "超威半导体"),
    "EFX": ("Equifax",),
    "TRU": ("TransUnion",),
}

# A+H dual-listed pairs: when backfilling a company name in `quote` whose ticker is missing from `tickers`,
# both the A-share and H-share codes are backfilled.
DUAL_LISTED_TICKER_GROUPS: tuple[tuple[str, str], ...] = (
    ("300750.SZ", "3750.HK"),
    ("688008.SS", "6809.HK"),
    ("000333.SZ", "0300.HK"),
    ("002050.SZ", "2050.HK"),
    ("600660.SS", "3606.HK"),
    ("002594.SZ", "1211.HK"),
    ("601888.SS", "1880.HK"),
    ("600941.SS", "0941.HK"),
    ("002352.SZ", "6936.HK"),
    ("603259.SS", "2359.HK"),
    ("601899.SS", "2899.HK"),
    ("688981.SS", "0981.HK"),
)

# Tickers of research/data/sell-side institutions that are frequently cited as sources rather than discussed subjects.
SOURCE_INSTITUTION_TICKERS: dict[str, tuple[str, ...]] = {
    "IT": ("Gartner",),
    "SPGI": ("S&P", "S&P Global", "标普"),
    "MCO": ("Moody's", "穆迪"),
    "MS": ("Morgan Stanley", "摩根士丹利", "大摩"),
    "GS": ("Goldman Sachs", "Goldman", "高盛"),
    "JPM": ("JPMorgan", "JP Morgan", "J.P. Morgan", "摩根大通", "小摩"),
    "BAC": ("Bank of America", "BofA", "美银", "美银证券"),
    "C": ("Citigroup", "Citi", "花旗"),
    "UBS": ("UBS", "瑞银"),
    "BCS": ("Barclays", "巴克莱"),
    "DB": ("Deutsche Bank", "德银"),
    "HSBC": ("HSBC", "汇丰"),
}

# Extensible table of media/research institutions and named speakers checked in `claim`/`topic_label` against `quote`.
KNOWN_INSTITUTION_PROPER_NAMES_TABLE: tuple[tuple[str, ...], ...] = (
    ("晚点聊",),
    ("陈哲",),
    ("Bernstein", "伯恩斯坦"),
    ("Jefferies", "杰富瑞"),
    ("Gartner",),
    ("SemiAnalysis",),
    ("Apptopia",),
    ("VisibleAlpha", "Visible Alpha"),
    ("Piper Sandler", "Piper"),
    ("TD Cowen",),
    ("Guggenheim",),
    ("Guidepoint",),
    ("中泰",),
    ("招商",),
)

# Extensible table of conclusion words forbidden in `claim` when absent from `quote`.
FORBIDDEN_CONCLUSION_WORDS_TABLE: tuple[str, ...] = (
    "止跌",
    "见顶",
    "触底",
    "筑底",
    "反转",
    "资金流",
    "资金流入",
    "资金流出",
    "主力流入",
    "主力流出",
    "费率更低",
    "费率更高",
)

# Extensible keyword tables for programmatic card `type` correction.
FACT_TO_ESTIMATE_KEYWORDS_TABLE: tuple[str, ...] = (
    "指引",
    "预计",
    "预期",
    "有望",
    "将",
    "计划",
    "将达",
    "guidance",
    "expects",
    "expected to",
    "target",
    "今明两年",
    "以后会有",
)

_REALIZED_EXPECTATION_MASK_RE = re.compile(
    r"(?:原预期内|落在原预期|原预期|此前预期"
    r"|(?:超|超出|好于|优于|低于|不及|弱于|符合|合乎)了?(?:市场|一致|分析师|卖方|机构|此前|原|的)*(?:预期)"
    r"|预期内"
    r"|(?:将)?目标价"
    r"|(?:训练|激励|股权激励|期权激励|限制性股票|回购|股份回购|股票回购|员工持股|持股)计划"
    r"|(?<![A-Za-z0-9])(?:price\s+targets?|target\s+prices?)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])in\s+line\s+with\s+(?:the\s+|market\s+|street\s+|consensus\s+|analyst\s+)?(?:expectations?|targets?|guidance|estimates?)(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:beat|beats|beating|missed|misses|missing|miss|above|below|ahead\s+of|short\s+of|met|meets|meeting)"
    r"(?:\s+(?:the\s+|market\s+|street\s+|consensus\s+|analyst\s+)?(?:expectations?|targets?|guidance|estimates?))?(?![A-Za-z0-9]))",
    flags=re.IGNORECASE,
)

_FORWARD_JIANG_RE = re.compile(
    r"(?:即将|(?<!已)(?<!曾)(?<!已经)(?<!曾经)将(?:要|会|于|在|达|达到|增长|增持|提升|提高|扩大|扩产|推出|发布|上线|量产|投产|实现|录得|进入|开启|启动|翻倍|突破|保持|维持|继续|持续|迎来|受益|贡献|带来|成为|达产|交付|出货|放量))"
)

REALIZED_FACT_PHRASES_TABLE: tuple[str, ...] = (
    "事实：",
    "已经实现",
    "已实现",
    "实际实现",
    "实际录得",
    "已公布",
    "已经完成",
    "已完成",
    "已经达成",
    "已达成",
    "结果 Q1",
    "结果 Q2",
    "结果 Q3",
    "结果 Q4",
    "结果 1Q",
    "结果 2Q",
    "结果 3Q",
    "结果 4Q",
)

ESTIMATE_TO_FACT_PHRASES_TABLE: tuple[str, ...] = (
    "事实：",
    "已实现",
    "已经实现",
    "实际实现",
    "实际录得",
    "已公布",
    "财报显示",
    "reported",
    "actual",
)

THIRD_PARTY_FORECAST_PHRASES_TABLE: tuple[str, ...] = (
    "卖方预计",
    "卖方预测",
    "卖方口径",
    "它估计",
    "一致预期为",
    "consensus expects",
    "consensus forecasts",
    "consensus is",
)

_THIRD_PARTY_FORECAST_EN_RE = re.compile(
    r"\b(?<!already\s)(?<!is\s)(?<!are\s)"
    r"(?:"
    r"(?:(?:street|market|bloomberg|sell[-\s]?side)\s+)?consensus\s+"
    r"(?:(?:for\s+[^.!?;,\n]{1,30}?\s+)?(?:expects?|forecasts?|estimates?|projects?|models?|sees?|is|at|of|calls\s+for)\b)"
    r"|(?:sell[-\s]?side|street)\s+(?:consensus\s+)?(?:expects?|forecasts?|estimates?|projects?|models?)\b"
    r")"
    r"[^.!?;,\n]*?\d",
    flags=re.IGNORECASE,
)

SELF_CALCULATION_KEYWORDS_TABLE: tuple[str, ...] = (
    "我算",
    "我粗估",
    "我们算",
    "倒算",
)

_UNVERIFIED_SOURCE_RE = re.compile(
    r"(?:原文[^。！？；;\n]{0,6}(?:没|未)打开"
    r"|(?:没|未)打开原文"
    r"|(?:没|未)[^。！？；;\n]{0,6}核对"
    r"|未核实|没核实|未经核实"
    r"|(?:没|未)看原文"
    r"|\bunverified\b)",
    flags=re.IGNORECASE,
)

OPINION_TO_MODEL_ASSUMPTION_KEYWORDS_TABLE: tuple[str, ...] = (
    "隐含",
    "假设",
    "倒算",
    "情景",
)

_AN_SUAN_RE = re.compile(r"按[^。！？；;\n，,]{1,25}算(?!力)")
_THIRD_PARTY_AN_SUAN_RE = re.compile(r"它说[^。！？；;\n]*?按[^。！？；;\n，,]{1,25}算(?!力)")

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


_HK_TICKER_CODE_RE = re.compile(r"^0*(\d{1,5})\.HK$")
_HK_BARE_ZERO_CODE_RE = re.compile(r"^0\d{3,4}$")
_HK_DIGITS_CAPTURE_RE = re.compile(r"^0*(\d{1,5})$")
_SH_TICKER_CODE_RE = re.compile(r"^(\d{6})\.(?:SS|SH)$")
_MKT_TICKER_CODE_RE = re.compile(r"^(\d{4,6})\.(SZ|T|TW)$")
_SIX_DIGIT_CODE_RE = re.compile(r"^\d{6}$")
_SECONDARY_TICKER_ALIAS_RE = re.compile(
    r"^(?:\d{1,6}\.(?:HK|SS|SH|SZ|T|TW|KS)|[A-Z]{2,5}(?:[.-][A-Z])?)$"
)

_ALIAS_CACHE_TTL_SECONDS: float = 300.0


@dataclass
class _TickerAliasesCacheEntry:
    """Cached per-user alias table and its lazily computed derived structures for one TTL bucket."""

    user_id: str
    time_bucket: int
    expires_at: float
    alias_map: dict[str, tuple[str, ...]]
    cn_strip_aliases: tuple[str, ...] | None = None
    entity_alias_groups: tuple[tuple[str, ...], ...] | None = None


_TICKER_ALIASES_CACHE: dict[tuple[str, int], _TickerAliasesCacheEntry] = {}
_STATIC_TICKER_ALIASES_BASE: dict[str, tuple[str, ...]] | None = None


@lru_cache(maxsize=4096)
def canonicalize_ticker_key(code: str) -> str:
    """Unify HK leading zeros (`700.HK`/`0700.HK`/`00700` -> `'0700.HK'`) and Shanghai suffixes (`.SH` -> `.SS`)."""
    s = str(code or "").strip().upper()
    if not s:
        return ""
    hk_m = _HK_TICKER_CODE_RE.match(s)
    if hk_m:
        return f"{int(hk_m.group(1)):04d}.HK"
    if _HK_BARE_ZERO_CODE_RE.match(s):
        return f"{int(s):04d}.HK"
    sh_m = _SH_TICKER_CODE_RE.match(s)
    if sh_m:
        return f"{sh_m.group(1)}.SS"
    return s


@lru_cache(maxsize=4096)
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
    hk_m = _HK_TICKER_CODE_RE.match(s)
    if not hk_m and _HK_BARE_ZERO_CODE_RE.match(s):
        hk_m = _HK_DIGITS_CAPTURE_RE.match(s)
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

    sh_m = _SH_TICKER_CODE_RE.match(s)
    if sh_m:
        digits = sh_m.group(1)
        _add(f"{digits}.SS")
        _add(f"{digits}.SH")
        _add(digits)
        return tuple(out)

    mkt_m = _MKT_TICKER_CODE_RE.match(s)
    if mkt_m:
        digits, sfx = mkt_m.group(1), mkt_m.group(2)
        _add(f"{digits}.{sfx}")
        _add(digits)
        return tuple(out)

    if _SIX_DIGIT_CODE_RE.match(s):
        if s.startswith("6"):
            _add(f"{s}.SS")
            _add(f"{s}.SH")
        elif s.startswith(("0", "3")):
            _add(f"{s}.SZ")

    return tuple(out)


def _get_static_ticker_aliases_base() -> dict[str, tuple[str, ...]]:
    global _STATIC_TICKER_ALIASES_BASE
    if _STATIC_TICKER_ALIASES_BASE is not None:
        return _STATIC_TICKER_ALIASES_BASE

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

    for ticker, extra_names in SUPPLEMENTAL_TICKER_ALIASES.items():
        names = [ticker, *list(extra_names)]
        secondary_tickers = [
            str(sn).strip().upper()
            for sn in extra_names
            if _SECONDARY_TICKER_ALIAS_RE.match(str(sn).strip())
        ]
        _register(ticker, names)
        for sec_t in secondary_tickers:
            _register(sec_t, names)
    for ticker, inst_names in SOURCE_INSTITUTION_TICKERS.items():
        _register(ticker, list(inst_names))

    _STATIC_TICKER_ALIASES_BASE = {k: tuple(v) for k, v in mapping.items()}
    return _STATIC_TICKER_ALIASES_BASE


def _current_alias_cache_bucket(now_mono: float | None = None) -> tuple[float, int]:
    now = time.monotonic() if now_mono is None else now_mono
    return now, int(now // _ALIAS_CACHE_TTL_SECONDS)


def invalidate_ticker_company_aliases_cache(user_id: str | None = None) -> None:
    """Clear cached ticker/company alias tables and derived structures (for `user_id` + union fallback, or all users when `None`)."""
    global _STATIC_TICKER_ALIASES_BASE
    if user_id is None:
        _TICKER_ALIASES_CACHE.clear()
        _STATIC_TICKER_ALIASES_BASE = None
    else:
        norm_uid = str(user_id).strip()
        stale_keys = [
            k for k in _TICKER_ALIASES_CACHE if k[0] in (norm_uid, "")
        ]
        for k in stale_keys:
            _TICKER_ALIASES_CACHE.pop(k, None)


def _load_ticker_aliases_cache_entry(user_id: str | None = None) -> _TickerAliasesCacheEntry:
    """Return the cached `_TickerAliasesCacheEntry` for `(user_id, time_bucket)`, loading from DB on miss/expiry."""
    from src.dalton.coverage import get_user_aliases_sync

    norm_uid = str(user_id or "").strip()
    now_mono, time_bucket = _current_alias_cache_bucket()
    cache_key = (norm_uid, time_bucket)
    cached_entry = _TICKER_ALIASES_CACHE.get(cache_key)
    if cached_entry is not None and now_mono < cached_entry.expires_at:
        return cached_entry

    for stale_key in [k for k in _TICKER_ALIASES_CACHE if k[0] == norm_uid and k != cache_key]:
        _TICKER_ALIASES_CACHE.pop(stale_key, None)

    db_aliases: dict[str, tuple[str, ...]] = {}
    try:
        db_aliases = get_user_aliases_sync(user_id, include_supplemental=False) or {}
    except Exception:
        db_aliases = {}

    static_base = _get_static_ticker_aliases_base()
    if not db_aliases:
        result = dict(static_base)
    else:
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

        for raw_ticker, alias_tuple in db_aliases.items():
            ticker = str(raw_ticker or "").strip().upper()
            if not ticker:
                continue
            names = [ticker]
            secondary_tickers: list[str] = []
            for sn in alias_tuple or ():
                sn_str = str(sn or "").strip()
                if sn_str and sn_str not in names:
                    names.append(sn_str)
                if _SECONDARY_TICKER_ALIAS_RE.match(sn_str):
                    secondary_tickers.append(sn_str.upper())
            _register(ticker, names)
            for sec_t in secondary_tickers:
                _register(sec_t, names)

        for canon_key, static_vals in static_base.items():
            cur = mapping.get(canon_key)
            if cur is None:
                mapping[canon_key] = list(static_vals)
            else:
                for sv in static_vals:
                    if sv not in cur:
                        cur.append(sv)

        result = {k: tuple(v) for k, v in mapping.items()}

    entry = _TickerAliasesCacheEntry(
        user_id=norm_uid,
        time_bucket=time_bucket,
        expires_at=now_mono + _ALIAS_CACHE_TTL_SECONDS,
        alias_map=result,
    )
    _TICKER_ALIASES_CACHE[cache_key] = entry
    return entry


def _load_ticker_company_aliases(user_id: str | None = None) -> dict[str, tuple[str, ...]]:
    """Load ticker -> company name aliases from `get_user_aliases(user_id)` plus `SUPPLEMENTAL_TICKER_ALIASES`,
    keyed by `canonicalize_ticker_key`.

    Cached per `(user_id, time_bucket)` with a 5-minute TTL (`_ALIAS_CACHE_TTL_SECONDS = 300.0`) in the same
    cache entry (`_TickerAliasesCacheEntry`) as its lazily computed derived results (`cn_strip_aliases` and
    `entity_alias_groups`), so the alias map and derived results expire together without any `id()`-keyed cache.
    Coverage roster edits actively invalidate the current worker's cache via `invalidate_ticker_company_aliases_cache`;
    cross-worker active invalidation is intentionally not performed (名单修改后，其他 worker 最多 5 分钟后看到新别名，
    这是可以接受的).
    """
    return _load_ticker_aliases_cache_entry(user_id).alias_map


_load_ticker_company_aliases.cache_clear = invalidate_ticker_company_aliases_cache  # type: ignore[attr-defined]
_ORIG_LOAD_TICKER_COMPANY_ALIASES = _load_ticker_company_aliases


def _call_load_ticker_company_aliases(user_id: str | None = None) -> dict[str, tuple[str, ...]]:
    """Invoke `_load_ticker_company_aliases`, tolerating zero-arg test doubles."""
    try:
        return _load_ticker_company_aliases(user_id)
    except TypeError:
        return _load_ticker_company_aliases()


def _resolve_alias_cache_entry_or_map(
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[_TickerAliasesCacheEntry | None, dict[str, tuple[str, ...]]]:
    """Return `(cache_entry, alias_map)` when backed by `_TICKER_ALIASES_CACHE`, or `(None, eff_alias_map)`
    when an explicit custom `alias_map` or monkeypatched `_load_ticker_company_aliases` is used.
    """
    if alias_map is not None:
        norm_uid = str(user_id or "").strip()
        now_mono, time_bucket = _current_alias_cache_bucket()
        cached_entry = _TICKER_ALIASES_CACHE.get((norm_uid, time_bucket))
        if (
            cached_entry is not None
            and now_mono < cached_entry.expires_at
            and cached_entry.alias_map is alias_map
        ):
            return cached_entry, alias_map
        return None, alias_map

    if _load_ticker_company_aliases is _ORIG_LOAD_TICKER_COMPANY_ALIASES:
        entry = _load_ticker_aliases_cache_entry(user_id)
        return entry, entry.alias_map

    return None, _call_load_ticker_company_aliases(user_id)


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


def _chinese_company_aliases_for_stripping(
    user_id: str | None = None,
    *,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Return multi-character Chinese company aliases sorted longest-first so company names
    containing numeral characters (e.g. `百度`, `百融云创`, `爱德万测试`, `万事达`, `三花智控`) are not parsed as numbers.
    """
    cache_entry, eff_alias_map = _resolve_alias_cache_entry_or_map(user_id, alias_map)
    if cache_entry is not None and cache_entry.cn_strip_aliases is not None:
        return cache_entry.cn_strip_aliases
    cn_names: set[str] = set()
    for aliases in eff_alias_map.values():
        for a in aliases:
            if a and not a.isascii() and len(a) >= 2:
                cn_names.add(a)
    cn_names.update({"百融云创", "百济神州", "百胜中国", "五粮液", "三六零", "一汽"})
    res = tuple(sorted(cn_names, key=len, reverse=True))
    if cache_entry is not None:
        cache_entry.cn_strip_aliases = res
    return res


_chinese_company_aliases_for_stripping.cache_clear = invalidate_ticker_company_aliases_cache  # type: ignore[attr-defined]


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

# Extensible table of indicator names that qualify a following number as a metric value
# even without a trailing `%` or currency unit (e.g. `价格=66.3`, `价格为66.3`, `RSI=85.6`, `PE 25`, `EV/EBIT 15`).
METRIC_PREFIX_INDICATORS_TABLE: tuple[str, ...] = (
    # Price & valuation levels
    r"收盘价",
    r"开盘价",
    r"目标价",
    r"行权价",
    r"成本价",
    r"成交价",
    r"发行价",
    r"转换价",
    r"转股价",
    r"赎回价",
    r"现价",
    r"股价",
    r"价格",
    r"均价",
    r"收于",
    # Technical indicators
    r"(?<![A-Za-z0-9])RSI(?![A-Za-z0-9])",
    r"量比",
    # Valuation multiples & financial ratios
    r"市盈率",
    r"市净率",
    r"市销率",
    r"(?<![A-Za-z0-9])(?:EV\s*/\s*(?:EBITDA|EBIT|Sales|Revenue)|P\s*/\s*[EBS]|PE|PB|PS|PEG|ROE|ROIC|ROA|ETR|EPS|BPS|DPS|NAV)(?![A-Za-z0-9])",
)

_METRIC_INDICATOR_PREFIX_CORE = "(?:" + "|".join(METRIC_PREFIX_INDICATORS_TABLE) + ")"
_METRIC_INDICATOR_PREFIX_TAIL_RE = re.compile(
    rf"{_METRIC_INDICATOR_PREFIX_CORE}"
    rf"(?:\s*(?:[=:：]|为|是|约|约为|大约|大概|达|达到|报|收于|跌至|升至|降至|回升至|回落至|高于|低于|突破|跌破|从|由|在|仅|只有|给|给到|维持))*"
    rf"\s*[（(]?\s*[-−+~～≈><≥≤]?\s*$",
    flags=re.IGNORECASE,
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


_STRIP_URL_RE = re.compile(r"https?://\S+")
_STRIP_BRACKET_REF_RE = re.compile(r"\[[A-Za-z]\d+\]")
_STRIP_LONG_HYPHEN_ID_RE = re.compile(r"(?<!\d)\d{6,10}-\d{2}-\d{4,8}(?!\d)")
_STRIP_LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{12,18}(?!\d)")
_STRIP_TICKER_SUFFIX_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{1,6}\.(?:SS|SZ|SH|HK|T|TW)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_STRIP_DATE_YMD_RE = re.compile(
    r"(?<!\d)W?(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])(?!\d)",
    flags=re.IGNORECASE,
)
_STRIP_DATE_YM_RE = re.compile(
    rf"(?<!\d)W?(?:19|20)\d{{2}}[-/.](?:0?[1-9]|1[0-2]){_POST_RANGE_UNIT_GUARD}",
    flags=re.IGNORECASE,
)
_STRIP_MULTI_YEAR_LIST_RE = re.compile(
    r"(?<!\d)(?:(?:(?:19|20)\d{2}|2\d)\s*(?:[-–—~～至到、，,及与和]|to)\s*)+(?:(?:19|20)\d{2}|2\d)\s*年",
    flags=re.IGNORECASE,
)
_STRIP_YEAR_RANGE_RE = re.compile(
    rf"(?<!\d)(?:19|20)\d{{2}}\s*[-–—~～至到]\s*(?:19|20)\d{{2}}(?:\s*[QH][1-4]E?)?{_POST_RANGE_UNIT_GUARD}",
    flags=re.IGNORECASE,
)
_STRIP_CN_YEAR_MD_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*(?:[-–—~～至到]\s*\d{1,2}\s*)?月(?:\s*\d{1,2}\s*(?:[-–—~～至到]\s*\d{1,2}\s*)?[日号])?)?"
)
_STRIP_CN_HALF_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[上下]半年")
_STRIP_CN_MD_RE = re.compile(r"(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*[日号](?:\s*[-–—~～至到]\s*\d{1,2}\s*[日号]?)?")
_STRIP_CN_M_RE = re.compile(r"(?<!\d)\d{1,2}\s*(?:[-–—~～至到]\s*\d{1,2}\s*)?月")
_STRIP_SLASH_MD_RANGE_RE = re.compile(
    rf"(?<![\d.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])\s*[-–—~～至到]\s*(?:(?:0?[1-9]|1[0-2])/)?(?:0?[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}"
)
_STRIP_SLASH_MD_CTX_RE = re.compile(
    rf"(?:(?<=截至)|(?<=截止)|(?<=于)|(?<=自)|(?<=从)|(?<=在))\s*(?<![\d.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}"
    rf"|(?<![\d.])(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?=\s*(?:[日号]|收盘|开盘|盘[前中后]|起|当[日天周]|公布|发布|披露|财报|法说|电话会))"
)
_STRIP_DASH_MD_RE = re.compile(
    rf"(?<![\d.])(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]){_POST_RANGE_UNIT_GUARD}"
)
_STRIP_WALL_CLOCK_RE = re.compile(r"(?<!\d)(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)")
_STRIP_Q_H_FY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[12]H\d{2,4}E?|[1-4]Q\d{2,4}E?|(?:19|20)\d{2}\s*[QH][1-4]E?|Q[1-4](?:[-_\s]*(?:19|20)\d{2}E?)?|H[12](?:[-_\s]*(?:19|20)\d{2}E?)?|FY\s*\d{2,4}E?)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_STRIP_MA_ATTACHED_PRICE_RE = re.compile(
    rf"{_MA_WINDOW_CORE}\s*[!！=：:]?\s*[（(]?\s*(?=\d)",
    flags=re.IGNORECASE,
)
_STRIP_MA_WINDOW_RE = re.compile(_MA_WINDOW_CORE, flags=re.IGNORECASE)
_STRIP_RETURN_DAYS_EN_RE = re.compile(r"(?<![A-Za-z0-9])\d+\s*D(?![A-Za-z0-9])", flags=re.IGNORECASE)
_STRIP_RETURN_DAYS_CN_RE = re.compile(
    r"(?<!\d)(?:30|50)\s*[日天](?!\s*(?:免费|试用|账期|交期|交付|内|以上|左右|到期|留存|复购))"
)
_STRIP_TENOR_RE = re.compile(r"(?:(?<!\d)\d+|[一二两三四五六七八九十百]+)\s*年期")
_STRIP_ORDINAL_RE = re.compile(
    r"第\s*(?:\d+|[一二两三四五六七八九十百千万]+)\s*(?:条|章|节|款|项|页|步|轮|次|季|季度|年|天|日|周|名|位|阶段|部分|批|期|号|届)"
)
_STRIP_MODEL_NAMES_RE = re.compile(
    r"(?:Qwen|DeepSeek|Llama|GPT|Claude|Gemini|Dyna|Wi-?Fi|Aptitude|Protocol|Point)[-\s]*\d+(?:\.\d+)?[A-Za-z]*(?:[-_][A-Za-z0-9.]+)*",
    flags=re.IGNORECASE,
)
_STRIP_RTX_RE = re.compile(
    r"\bRTX\s+(?:PRO\s+)?\d{3,4}(?:\s*(?:Ti|SUPER|Ada))?\b",
    flags=re.IGNORECASE,
)
_STRIP_FILING_CODES_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:13F|10-[QK]|20-F|8-K|W-8BEN|4o|14A|18A(?:-P)?)(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_STRIP_ALNUM_CODES_RE = re.compile(
    r"(?<![A-Za-z0-9])(?!(?:USD|CNY|RMB|HKD|JPY|EUR|GBP)(?![A-Za-z]))[A-Za-z]{1,4}\d+(?:\.\d+)?[A-Za-z]*(?![A-Za-z0-9])",
    flags=re.IGNORECASE,
)
_STRIP_LIST_BULLET_RE = re.compile(r"(?:^|[\n；;])\s*[（(]?[1-9][）)]")
_STRIP_CN_IDIOM_NUMS_RE = re.compile(
    r"(?:第?[一二三四]季(?:度|报)|[上下]半年(?:报)?|周[一二三四五六日]|星期[一二三四五六日]|[一二三四五六七八九十]{1,2}月"
    r"|(?:这|那|每|同|前|上|下|过去|看|算|试|等|缓|盯)?(?<![十零〇百千万亿])一(?:周|个?季度|年(?:前|期)?|个月|份(?!额)|下|起|点(?:儿)?|次性|系列|揽子|方面|部分|整套|家(?:之言|电|居|庭))"
    r"|逐一|唯一|统一|一致|一旦|一般|一定|一直|一起|万一|进一步|一系列|一方面|一买一卖|一分钱|之一|一半"
    r"|一度|十分|一成不变|三番五次|三番四次|百分之百地|第三方)"
)


def _strip_non_metric_tokens(
    text: str,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """Remove URLs, stock tickers, dates/periods, technical indicator windows, and product/model codes
    before extracting metric numbers from `quote` or `claim`.
    """
    s = str(text or "")
    s = _STRIP_URL_RE.sub(" ", s)
    s = _STRIP_BRACKET_REF_RE.sub(" ", s)
    s = _STRIP_LONG_HYPHEN_ID_RE.sub(" ", s)
    s = _STRIP_LONG_DIGITS_RE.sub(" ", s)
    s = _STRIP_TICKER_SUFFIX_RE.sub(" ", s)
    for cn_comp in _chinese_company_aliases_for_stripping(user_id=user_id, alias_map=alias_map):
        if cn_comp in s:
            s = s.replace(cn_comp, " ")
    # Dates: YYYY-MM-DD, W2026-10-03, and unit-free YYYY-MM
    s = _STRIP_DATE_YMD_RE.sub(" ", s)
    s = _STRIP_DATE_YM_RE.sub(" ", s)
    # Multi-year lists/ranges and calendar years: 2026、2027、2028年, 2029 至 2030 年, 2023-2025年, 26-28年
    s = _STRIP_MULTI_YEAR_LIST_RE.sub(" ", s)
    s = _STRIP_YEAR_RANGE_RE.sub(" ", s)
    # Dates: YYYY年M月D日, YYYY年M月, YYYY年, YYYY上下半年
    s = _STRIP_CN_YEAR_MD_RE.sub(" ", s)
    s = _STRIP_CN_HALF_YEAR_RE.sub(" ", s)
    # Dates: M月D日, M月, date-contextual M/D, and unit-free MM-DD
    s = _STRIP_CN_MD_RE.sub(" ", s)
    s = _STRIP_CN_M_RE.sub(" ", s)
    s = _STRIP_SLASH_MD_RANGE_RE.sub(" ", s)
    s = _STRIP_SLASH_MD_CTX_RE.sub(" ", s)
    s = _STRIP_DASH_MD_RE.sub(" ", s)
    # Wall-clock timestamps like 22:12 (preserve single-digit ratios like 4:1)
    s = _STRIP_WALL_CLOCK_RE.sub(" ", s)
    # Quarter / half-year / fiscal year tokens
    s = _STRIP_Q_H_FY_RE.sub(" ", s)
    # Moving-average windows (`50MA`, `MA50`, `50日均线`, `50日线`, `200MA`) and return windows (`30D`, `50D`):
    # when immediately followed by a price (`跌破200MA!(125.23)`, `突破50MA(76.5)`, `50日均线（247.0）`, `低于50日均线115.6`),
    # strip the window number and mark the attached price with `$` so the price itself still participates in validation.
    s = _STRIP_MA_ATTACHED_PRICE_RE.sub(" $ ", s)
    s = _STRIP_MA_WINDOW_RE.sub(" ", s)
    s = _STRIP_RETURN_DAYS_EN_RE.sub(" ", s)
    s = _STRIP_RETURN_DAYS_CN_RE.sub(" ", s)
    # Tenors and ordinals (e.g. `十年期`, `10年期`, `第5条`, `第3年`, `第二季度`)
    s = _STRIP_TENOR_RE.sub(" ", s)
    s = _STRIP_ORDINAL_RE.sub(" ", s)
    # Product, model, and filing identifiers: Qwen2.5, Qwen3-8B, WiFi7, H100, B200, B300, RTX PRO 5000, Dyna-2.1, Aptitude 8, Protocol 80, Point72, 13F, 10-Q
    s = _STRIP_MODEL_NAMES_RE.sub(" ", s)
    s = _STRIP_RTX_RE.sub(" ", s)
    s = _STRIP_FILING_CODES_RE.sub(" ", s)
    s = _STRIP_ALNUM_CODES_RE.sub(" ", s)
    s = _STRIP_LIST_BULLET_RE.sub(" ", s)
    s = _STRIP_CN_IDIOM_NUMS_RE.sub(" ", s)
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
_FORECAST_NUM_CORE = rf"(?:{_ARABIC_NUM_CORE}|{re.sub(r'\(\?P<\w+>', '(?:', _CN_NUM_MENTION_RE.pattern)})"
_THIRD_PARTY_FORECAST_RE = re.compile(
    r"(?:"
    r"(?:(?:卖方|机构|券商|投行|报告|分析师)\s*(?:口径)?\s*(?:称[^。！？!?；;\n]{0,40}?)?(?:预计|预测|估计|估算)"
    r"|它\s*(?:预计|预测|估计|估算)"
    r"|(?<!已是)(?<!是)(?<!为)(?<!成)(?:市场|彭博|卖方)?一致预期\s*(?:为|是|在|达|达到|约|预计|预测|增长|增速|营收|收入|利润|净利))"
    r"(?:(?![。！？!?；;\n，]|,(?!\d)).){0,120}?"
    rf"{_FORECAST_NUM_CORE}"
    r")"
    r"|(?:(?:预计|预测|估计)"
    r"(?=[^。！？!?；;\n，]{0,120}?[（(]?卖方(?:口径|预计|预测))"
    r"(?:(?![。！？!?；;\n，]|,(?!\d)).){0,120}?"
    rf"{_FORECAST_NUM_CORE}"
    r"(?:(?![。！？!?；;\n，]|,(?!\d)).){0,120}?[（(]?卖方(?:口径|预计|预测))"
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


_EN_NUM_WORD_MAP: dict[str, str] = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}

_EN_ORDINAL_QUARTER_MAP: dict[str, str] = {
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
}

_EN_NUM_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9\-])"
    r"(?:"
    r"(?P<and_other>and\s+(?P<other_num>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+other\b)"
    r"|(?P<dozen>a\s+dozen)"
    r"|(?P<half>half(?!\s*-\s*year\b|\s+year\b))"
    r"|(?P<ord>first|second|third|fourth)(?=\s+quarters?\b)"
    r"|(?P<card>(?:one(?!\s*-\s*(?:time|off)\b|\s+of\b)|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))"
    r")"
    r"(?![A-Za-z0-9\-])"
    rf"(?:\s*(?P<u_en>{_UNIT_CORE}))?"
    rf"(?:\s*(?P<p_en>{_METRIC_POST_UNIT_RE}))?",
    flags=re.IGNORECASE,
)


def _parse_number_mentions_detailed(
    text: str,
    *,
    strip_non_metrics: bool = True,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, set[str], bool]]:
    """Parse number mentions from `text` returning `(raw_canon, legal_forms, has_unit)` for each mention."""
    cleaned = (
        _strip_non_metric_tokens(text, user_id=user_id, alias_map=alias_map)
        if strip_non_metrics
        else str(text or "")
    )
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
        has_indicator_prefix = bool(_METRIC_INDICATOR_PREFIX_TAIL_RE.search(cleaned[: match.start()]))
        has_unit_1 = bool(eff_cur or eff_u1 or eff_post or has_indicator_prefix)
        c1, f1 = _build_number_forms(n1, eff_u1)
        if c1 is not None:
            mentions.append((c1, f1, has_unit_1))
        if n2:
            has_unit_2 = bool(eff_cur or u2 or eff_u1 or eff_post or has_indicator_prefix)
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
        for idx in range(cn_match.start(), cn_match.end()):
            masked_chars[idx] = " "

    remaining_en = "".join(masked_chars)
    for en_match in _EN_NUM_MENTION_RE.finditer(remaining_en):
        u_en = en_match.group("u_en")
        p_en = en_match.group("p_en")
        if u_en and not _is_short_en_mag_allowed(
            u_en,
            text=remaining_en,
            span_start=en_match.start(),
            span_end=en_match.end(),
            cur_prefix=None,
            post_unit=p_en,
        ):
            u_en = None
        has_en_unit = bool(u_en or p_en)
        if en_match.group("and_other"):
            other_w = en_match.group("other_num").lower()
            base_num = _EN_NUM_WORD_MAP[other_w]
            c_en, f_en = _build_number_forms(base_num, u_en)
            if c_en is not None:
                f_en.add(str(int(base_num) + 1))
                mentions.append((c_en, f_en, has_en_unit))
        elif en_match.group("dozen"):
            c_en, f_en = _build_number_forms("12", u_en)
            if c_en is not None:
                mentions.append((c_en, f_en, has_en_unit))
        elif en_match.group("half"):
            c_en, f_en = _build_number_forms("0.5", u_en)
            if c_en is not None:
                mentions.append((c_en, f_en, has_en_unit))
        elif en_match.group("ord"):
            ord_w = en_match.group("ord").lower()
            c_en, f_en = _build_number_forms(_EN_ORDINAL_QUARTER_MAP[ord_w], u_en)
            if c_en is not None:
                mentions.append((c_en, f_en, has_en_unit))
        elif en_match.group("card"):
            card_w = en_match.group("card").lower()
            c_en, f_en = _build_number_forms(_EN_NUM_WORD_MAP[card_w], u_en)
            if c_en is not None:
                mentions.append((c_en, f_en, has_en_unit))

    return mentions


def parse_number_mentions(
    text: str,
    *,
    strip_non_metrics: bool = True,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[str, set[str]]]:
    """Parse number mentions from `text` (used identically for `quote` and `claim`).

    Each mention yields `(raw_canon, legal_forms)` where `legal_forms` contains ONLY the raw value
    and the single unit-expanded value (`万亿` > `亿` > `千万`/`百万` > `万` > `千`/`百` and `B`/`M`/`K`/`bn`/`mn`).
    """
    return [
        (raw_canon, forms)
        for raw_canon, forms, _has_unit in _parse_number_mentions_detailed(
            text, strip_non_metrics=strip_non_metrics, user_id=user_id, alias_map=alias_map
        )
    ]


def extract_quote_supported_numbers(
    quote: str,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> set[str]:
    """Extract all legal normalized number forms (raw + unit-expanded) supported by `quote`."""
    supported: set[str] = set()
    for _raw_canon, forms, _has_unit in _parse_number_mentions_detailed(
        quote, strip_non_metrics=True, user_id=user_id, alias_map=alias_map
    ):
        supported.update(forms)
    return supported


def extract_claim_numbers(
    claim: str,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """Extract normalized quantitative metric number tokens (attached to units) from `claim`."""
    return [
        raw_canon
        for raw_canon, _forms, has_unit in _parse_number_mentions_detailed(
            claim, strip_non_metrics=True, user_id=user_id, alias_map=alias_map
        )
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


def _is_period_supported_in_text(period: str, quote: str) -> bool:
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


_SENTENCE_SPLIT_RE = re.compile(
    r"[。！？；\n]+"
    r"|(?<=\bInc\.)\s+(?=[A-Z])|(?<=\bLtd\.)\s+(?=[A-Z])|(?<=\bCorp\.)\s+(?=[A-Z])|(?<=\bCo\.)\s+(?=[A-Z])"
    r"|(?<![A-Za-z]\.[A-Za-z])(?<!\bInc)(?<!\bLtd)(?<!\bCorp)(?<!\bCo)(?<!\bvs)(?<!\bMr)(?<!\bDr)(?<!\bSt)(?<!\bNo)"
    r"\.(?!\d|[A-Za-z])(?:\s+|$)"
    r"|[!?;\n]+(?:\s+|$)"
)
_CLAUSE_SPLIT_CN_COMMA_RE = re.compile(r"[，,]+")


def _split_sentences(text: str, *, split_cn_comma: bool = False) -> list[str]:
    """Split `text` into sentences (or clauses when `split_cn_comma=True`) in linear time,
    splitting Chinese `。！？；` (and `，` when `split_cn_comma=True`) and English `. ! ? ;` followed by whitespace
    while preserving decimals (`3.5`), abbreviations (`U.S.`, `Inc.`), and ticker suffixes (`.HK`, `.SS`).
    """
    s = str(text or "")
    if not s:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(s) if p and p.strip()]
    if not split_cn_comma:
        return parts
    clauses: list[str] = []
    for p in parts:
        for sub in re.split(r"，+", p):
            if sub and sub.strip():
                clauses.append(sub.strip())
    return clauses


def is_period_supported_by_quote(
    period: str,
    quote: str,
    *,
    value: Any = None,
    unit: str = "",
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> bool:
    """Return True if `period` is supported by `quote` and anchored in the same sentence as `value`."""
    p = str(period or "").strip()
    if not p:
        return True
    q = str(quote or "")
    if not _is_period_supported_in_text(p, q):
        return False

    sents = _split_sentences(q, split_cn_comma=False)
    if not sents:
        return False

    if value is not None:
        fig_mentions = _extract_figure_value_mentions(value, unit=str(unit or "").strip())
        if fig_mentions:
            matching_sents = [
                s
                for s in sents
                if all(
                    not forms.isdisjoint(extract_quote_supported_numbers(s, user_id=user_id, alias_map=alias_map))
                    for _raw, forms in fig_mentions
                )
            ]
            if not matching_sents:
                return False
            return any(_is_period_supported_in_text(p, s) for s in matching_sents)

    return any(_is_period_supported_in_text(p, s) for s in sents)


_CURRENCY_UNIT_SPECS: tuple[tuple[str, str], ...] = (
    (r"美元|美金|USD[_\s]*|US\$|\$", r"(?:美元|美金|刀|(?<![A-Za-z])USD(?![A-Za-z])|US\$|(?<![A-Za-z])\$|dollars?\b)"),
    (r"港元|港币|HKD[_\s]*|HK\$", r"(?:港元|港币|(?<![A-Za-z])HKD(?![A-Za-z])|HK\$)"),
    (
        r"人民币|RMB[_\s]*|CNY[_\s]*",
        r"(?:人民币|元人民币|(?<![A-Za-z])(?:RMB|CNY)(?![A-Za-z])|(?<![美港日欧韩澳加新台币])元|块|(?<![A-Za-z])yuan(?![A-Za-z]))",
    ),
    (r"日元|JPY[_\s]*", r"(?:日元|(?<![A-Za-z])JPY(?![A-Za-z])|円|(?<![A-Za-z])yen(?![A-Za-z]))"),
    (r"欧元|EUR[_\s]*|€", r"(?:欧元|(?<![A-Za-z])EUR(?![A-Za-z])|€)"),
    (r"英镑|GBP[_\s]*|£", r"(?:英镑|(?<![A-Za-z])GBP(?![A-Za-z])|£)"),
    (
        r"元",
        r"(?:人民币|元人民币|(?<![A-Za-z])(?:RMB|CNY)(?![A-Za-z])|(?<![美港日欧韩澳加新台币])元|块|(?<![A-Za-z])yuan(?![A-Za-z]))",
    ),
)


_NON_RMB_CURRENCY_SPAN_RE = re.compile(
    r"(?:新台币|台币|韩元|新加坡元|澳元|加元|美元|美金|港元|港币|日元|欧元|英镑)"
    r"(?:\s*[-−+~～≈><≥≤]?\s*[\d,.]+(?:\s*(?:万\s*亿|亿|千\s*万|百\s*万|万|千|百))?\s*元)?",
    flags=re.IGNORECASE,
)


def strip_unsupported_currency_from_unit(
    unit: str,
    value: Any,
    quote: str,
    *,
    period: str = "",
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, bool]:
    """If `unit` contains a currency (`美元`, `元`, `港元`, `USD`, `USD_bn`, `CNY_yi`, etc.)
    but the sentence in `quote` containing `value` lacks that currency token, strip the currency from `unit`.
    Also clears `倍`/`x`/`X` when the sentence in `quote` containing `value` lacks `倍`/`x`/`X`/`times`,
    returning `("", True)`. Returns `(cleaned_unit, was_stripped)`.
    """
    u = str(unit or "").strip()
    if not u:
        return u, False
    q = str(quote or "")
    sents = _split_sentences(q, split_cn_comma=False)
    if not sents:
        return u, False

    fig_mentions = _extract_figure_value_mentions(value, unit=u)
    matching_sents = [
        s
        for s in sents
        if fig_mentions
        and all(
            not forms.isdisjoint(extract_quote_supported_numbers(s, user_id=user_id, alias_map=alias_map))
            for _raw, forms in fig_mentions
        )
    ]
    if period:
        period_sents = [s for s in matching_sents if _is_period_supported_in_text(period, s)]
        if period_sents:
            matching_sents = period_sents
    if not matching_sents:
        matching_sents = sents

    if u in ("倍", "x", "X"):
        sent_has_mult = any(
            bool(re.search(r"(?:倍|(?<![A-Za-z])[xX](?![A-Za-z])|\btimes\b)", s, flags=re.IGNORECASE))
            for s in matching_sents
        )
        if not sent_has_mult:
            return "", True
        return u, False

    for unit_pat, q_cur_pat in _CURRENCY_UNIT_SPECS:
        if not re.search(unit_pat, u, flags=re.IGNORECASE):
            continue
        if unit_pat == r"元" and re.search(r"(?:美元|港元|日元|欧元|韩元|新台币)", u):
            continue
        is_rmb_spec = unit_pat in (r"人民币|RMB[_\s]*|CNY[_\s]*", r"元")
        sent_has_cur = any(
            bool(
                re.search(
                    q_cur_pat,
                    _NON_RMB_CURRENCY_SPAN_RE.sub(" ", s) if is_rmb_spec else s,
                    flags=re.IGNORECASE,
                )
            )
            for s in matching_sents
        )
        if not sent_has_cur:
            new_u = re.sub(unit_pat, "", u, flags=re.IGNORECASE).strip(" _/")
            return new_u, new_u != u
        break
    return u, False


AMBIGUOUS_SHORT_ALIAS_WORDS: dict[str, tuple[str, ...]] = {
    "美的": ("完美的", "精美的", "优美的", "甜美的", "柔美的"),
    "芒果": ("超媒", "TV", "台"),
}

_TRAD_TO_SIMP_TABLE = str.maketrans(
    "騰訊阿裡團網東攜濟藥寧時亞長車農華潤島撈體申國瑪優鋪黃電移動聯礦鋁兗匯豐團險壽銀恆設業郵儲興發證廣財際導積詠鴻業富達緯創穎碩學聲電訊爾藍韋荊紀瀾創軟雲網萬數紀湯曠視訊飛匯術邁療醫達實製翰攜錦順遞龍置貝務綠鴻產氣鐵航遠電實創敏機義萊協綠陽風爾濱車思變繼電丹銀萊麥傑標譽萬愛測東科學勝倉製作產發電機臺偉馬遜圖譜滬億為鋰來瀘選殼嗶傳試薩詩蘭藝滙點陳節紅書螞蟻遊階躍樹擴張續貨賣買計預報會議與對開關價",
    "腾讯阿里团网东携济药宁时亚长车农华润岛捞体申国玛优铺黄电移动联矿铝兖汇丰团险寿银恒设业邮储兴发证广财际导积咏鸿业富达纬创颖硕学声电讯尔蓝韦荆纪澜创软云网万数纪汤旷视讯飞汇术迈疗医达实制翰携锦顺递龙置贝务绿鸿产气铁航远电实创敏机义莱协绿阳风尔滨车思变继电丹银莱麦杰标誉万爱测东科学胜仓制作产发电机台伟马逊图谱沪亿为锂来泸选壳哔传试萨诗兰艺汇点陈节红书蚂蚁游阶跃树扩张续货卖买计预报会议与对开关价",
)


def _normalize_trad_to_simp(text: str) -> str:
    return str(text or "").translate(_TRAD_TO_SIMP_TABLE)


@lru_cache(maxsize=4096)
def _compiled_alias_regex(alias: str) -> re.Pattern[str]:
    if alias.isascii():
        if alias.isdigit():
            return re.compile(rf"(?<![A-Za-z0-9.]){re.escape(alias)}(?![A-Za-z0-9.])")
        if len(alias) <= 2:
            return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])")
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", flags=re.IGNORECASE)
    if alias == "芒果":
        req_suffixes = AMBIGUOUS_SHORT_ALIAS_WORDS.get("芒果", ("超媒", "TV", "台"))
        sfx_pat = "|".join(re.escape(s) for s in req_suffixes)
        return re.compile(rf"芒果\s*(?:{sfx_pat})", flags=re.IGNORECASE)
    return re.compile(re.escape(alias))


def _alias_matches_in_text(alias: str, text: str) -> bool:
    """Return True if company `alias` (or ticker code) matches in `text` with proper boundary rules."""
    if not alias or not text:
        return False
    if alias.isascii():
        return bool(_compiled_alias_regex(alias).search(text))
    norm_alias = _normalize_trad_to_simp(alias)
    norm_text = _normalize_trad_to_simp(text)
    if norm_alias == "芒果":
        return bool(_compiled_alias_regex("芒果").search(norm_text))
    # Short Chinese company names (海光, 腾讯, 美的) are ordinary substrings in running text ("在海光",
    # "的腾讯"), so a CJK-boundary rule loses real mentions. Only names that collide with common
    # words carry an explicit list of the words to ignore.
    if len(norm_alias) < 3:
        masked = norm_text
        for word in AMBIGUOUS_SHORT_ALIAS_WORDS.get(norm_alias, ()):
            masked = masked.replace(word, " ")
        return norm_alias in masked
    return norm_alias in norm_text


_EXCLUSION_STOP_RE = (
    r"[，。；;：:！!？?\n后外并但而（）()]"
    r"|,(?!\d)|\.(?![A-Za-z0-9])"
    r"|同时|继续|保留|持有|买入|增持|加仓|建仓|看多|看好"
)

_EXCLUSION_PREFIX_RE = re.compile(
    rf"(?:(?<!不)(?:剔除|排除|除去|不含|未采用)|(?<![A-Za-z])(?:excluding|excludes|ex-))"
    rf"\s*((?:(?!{_EXCLUSION_STOP_RE}).)*)",
    flags=re.IGNORECASE,
)

_EN_PASSIVE_EXCLUSION_RE = re.compile(
    r"(?<![A-Za-z])(?:(?:is|are|was|were|be|been)\s+)?not\s+(?:used|included)(?![A-Za-z])",
    flags=re.IGNORECASE,
)
_EN_ACTIVE_EXCLUSION_RE = re.compile(
    r"(?<![A-Za-z])(?:doesn['’]t|don['’]t|does\s+not|do\s+not|did\s+not|didn['’]t)\s+use(?![A-Za-z])",
    flags=re.IGNORECASE,
)
_EN_PRONOUN_OBJECT_RE = re.compile(
    r"^\s*(?:them|it|they|these|those|either|both)\b",
    flags=re.IGNORECASE,
)

_CUSTOMER_ORDER_EXCLUSION_RE = re.compile(
    r"(?:的\s*([A-Za-z0-9\u4e00-\u9fff]{2,12})\s*订单|['’]s\s+([A-Za-z0-9]{2,15})\s+orders?\b)",
    flags=re.IGNORECASE,
)


def _extract_excluded_spans_and_clean_quote(quote: str) -> tuple[list[str], str]:
    """Extract excluded spans (Chinese and English prefix/clause exclusions and customer-order modifiers)
    and return `(excluded_spans, cleaned_quote)` using linear clause splitting.
    """
    q = str(quote or "")
    if not q:
        return [], ""
    excluded_spans: list[str] = []
    for m in _EXCLUSION_PREFIX_RE.finditer(q):
        span = m.group(1)
        if span and span.strip():
            excluded_spans.append(span)
    for m in _CUSTOMER_ORDER_EXCLUSION_RE.finditer(q):
        span = m.group(1) or m.group(2)
        if span and span.strip():
            excluded_spans.append(span)

    clauses = _split_sentences(q, split_cn_comma=True)
    cleaned_clauses: list[str] = []
    for clause in clauses:
        if "未予采用" in clause:
            excluded_spans.append(clause)
            continue
        pas_m = _EN_PASSIVE_EXCLUSION_RE.search(clause)
        if pas_m is not None:
            before_verb = clause[: pas_m.start()].strip(" .!?;,，。！？；")
            after_verb = clause[pas_m.end() :].strip(" .!?;,，。！？；")
            if before_verb:
                excluded_spans.append(before_verb)
            clause = after_verb
        else:
            act_m = _EN_ACTIVE_EXCLUSION_RE.search(clause)
            if act_m is not None:
                before_verb = clause[: act_m.start()]
                after_verb_full = clause[act_m.end() :]
                obj_parts = re.split(r",(?!\d)", after_verb_full, maxsplit=1)
                obj_span = obj_parts[0].strip(" .!?;,，。！？；")
                rest_after_comma = obj_parts[1].strip() if len(obj_parts) > 1 else ""
                if not obj_span or _EN_PRONOUN_OBJECT_RE.match(obj_span):
                    so_parts = re.split(r",\s*so\b", before_verb, maxsplit=1, flags=re.IGNORECASE)
                    if len(so_parts) > 1:
                        if so_parts[0].strip():
                            excluded_spans.append(so_parts[0].strip())
                        clause = rest_after_comma
                    else:
                        clause = f"{before_verb} {rest_after_comma}".strip()
                else:
                    excluded_spans.append(obj_span)
                    clause = f"{before_verb} {rest_after_comma}".strip()
        clause_cleaned = _EXCLUSION_PREFIX_RE.sub(" ", clause)
        clause_cleaned = _CUSTOMER_ORDER_EXCLUSION_RE.sub(" ", clause_cleaned)
        if clause_cleaned.strip():
            cleaned_clauses.append(clause_cleaned)

    cleaned = " ".join(cleaned_clauses)
    return excluded_spans, cleaned


_INSTITUTION_SOURCE_VERB_AFTER_RE = (
    r"(?:\s*(?:\d{4}\s*年\s*)?(?:\d{1,2}\s*[月/.-]\s*\d{1,2}\s*日?\s*)?)?"
    r"(?:\s*(?:的分析师|分析师|的研报|研报|的研究|研究报告|报告|的晨报|晨报))?"
    r"\s*(?:预计|预测|估算|估计|称|认为|指出|表示)"
)
_INSTITUTION_SOURCE_PREP_BEFORE_RE = (
    r"(?:据|根据|按照|引用|引述|援引|来自|来源[：:]?[^。\n]{0,20}|卖方[：:]?\s*)\s*"
)


def _is_institution_used_as_source(canon_t: str, text: str, cited_sources: list[str]) -> bool:
    """Return True only when an institution (`SOURCE_INSTITUTION_TICKERS`) is used as an information source
    (e.g. `高盛预计/称/认为/指出/表示/估计`, `在高盛的研报/会上`, `according to Goldman`, or listed in `cited_sources`),
    and False when the institution itself is the subject of the card (e.g. `摩根大通Q3净利息收入增长`).
    """
    src_names = SOURCE_INSTITUTION_TICKERS.get(canon_t, ())
    if not src_names:
        return False
    t_str = _normalize_trad_to_simp(str(text or ""))
    in_cited = any(
        any(_normalize_trad_to_simp(sn).lower() in _normalize_trad_to_simp(s).lower() for sn in src_names if len(sn) >= 2)
        for s in cited_sources
    )
    if in_cited:
        return True

    for sn in src_names:
        norm_sn = _normalize_trad_to_simp(sn)
        if not _alias_matches_in_text(norm_sn, t_str):
            continue
        esc = re.escape(norm_sn)
        zai_full_pat = rf"在\s*{esc}\s*(?:的)?(?:研报|研究报告|报告|会上|会议)"
        if norm_sn.isascii():
            src_pat = re.compile(
                rf"(?:(?<![A-Za-z])according\s+to\s+{esc}(?![A-Za-z])"
                rf"|(?<![A-Za-z]){esc}(?:['’]s)?\s+(?:(?:analysts?|research|reports?|notes?)\s+)?(?:estimates?|forecasts?|expects?|believes?|says?|said|noted|points?\s+out)(?![A-Za-z])"
                rf"|{_INSTITUTION_SOURCE_PREP_BEFORE_RE}{esc}(?![A-Za-z])"
                rf"|{zai_full_pat}"
                rf"|(?<![A-Za-z]){esc}{_INSTITUTION_SOURCE_VERB_AFTER_RE})",
                flags=re.IGNORECASE,
            )
        else:
            src_pat = re.compile(
                rf"(?:{_INSTITUTION_SOURCE_PREP_BEFORE_RE}{esc}|{zai_full_pat}|{esc}{_INSTITUTION_SOURCE_VERB_AFTER_RE})",
                flags=re.IGNORECASE,
            )
        if src_pat.search(t_str):
            return True

    return False


def _validate_tickers_against_quote_detailed(
    tickers: list[str],
    quote: str,
    *,
    cited_sources: list[str] | None = None,
    claim: str | None = None,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], int, int, int]:
    """Filter `tickers` against `quote`, dropping private companies (`tickers_dropped_private`),
    trading channels not discussed as subjects in `claim` (`tickers_dropped_channel`),
    source institutions, excluded companies, and unsupported symbols.
    Returns `(kept_tickers, tickers_dropped, tickers_dropped_private, tickers_dropped_channel)`.
    """
    q = str(quote or "")
    excluded_spans, q_without_exclusions = _extract_excluded_spans_and_clean_quote(q)

    q_no_generic_it = re.sub(
        r"(?:数据中心\s*IT|IT\s*(?:容量|服务|支出|基础设施|系统|设备|行业|部门|预算|人员|外包|架构|成本))",
        " ",
        q_without_exclusions,
        flags=re.IGNORECASE,
    )
    srcs = [str(s or "") for s in (cited_sources or [])]
    eff_alias_map = alias_map if alias_map is not None else _call_load_ticker_company_aliases(user_id)

    kept: list[str] = []
    dropped = 0
    dropped_private = 0
    dropped_channel = 0
    seen_priv: set[str] = set()
    for raw_t in tickers or []:
        t = str(raw_t or "").strip().upper()
        if not t:
            continue
        canon_t = canonicalize_ticker_key(t)

        if _is_private_company_ticker(t, user_id=user_id, alias_map=eff_alias_map):
            if canon_t not in seen_priv:
                seen_priv.add(canon_t)
                dropped += 1
                dropped_private += 1
            continue

        if canon_t in TRADING_CHANNEL_TICKERS:
            check_text = claim if claim is not None else q
            if not _is_trading_channel_discussed_in_claim(
                canon_t, check_text, user_id=user_id, alias_map=eff_alias_map
            ):
                dropped += 1
                dropped_channel += 1
                continue

        if canon_t in SOURCE_INSTITUTION_TICKERS:
            if _is_institution_used_as_source(canon_t, q, srcs) and not re.search(
                r"(?<![A-Za-z0-9])IT(?![A-Za-z0-9])", q_no_generic_it
            ):
                dropped += 1
                continue

        cand_names: list[str] = []
        for v in ticker_code_variants(t):
            if v not in cand_names:
                cand_names.append(v)
        for a in eff_alias_map.get(canon_t, ()):
            if a not in cand_names:
                cand_names.append(a)

        # If any alias/code of this ticker is the target of an explicit exclusion phrase (e.g. "剔除WIT后...", "doesn't use them"), drop it.
        if excluded_spans and any(
            _alias_matches_in_text(alias, span) for span in excluded_spans for alias in cand_names
        ):
            dropped += 1
            continue

        target_q = q_no_generic_it if canon_t == "IT" else q_without_exclusions
        matched = any(_alias_matches_in_text(alias, target_q) for alias in cand_names)

        if matched:
            if t not in kept:
                kept.append(t)
        else:
            dropped += 1

    return kept, dropped, dropped_private, dropped_channel


def validate_tickers_against_quote(
    tickers: list[str],
    quote: str,
    *,
    cited_sources: list[str] | None = None,
    claim: str | None = None,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], int]:
    """Filter `tickers` so only symbols or mapped company names (`get_user_aliases` + `SUPPLEMENTAL_TICKER_ALIASES`)
    present as discussed subjects in `quote` remain. Returns `(kept_tickers, tickers_dropped)`.
    """
    kept, dropped, _priv, _chan = _validate_tickers_against_quote_detailed(
        tickers,
        quote,
        cited_sources=cited_sources,
        claim=claim,
        user_id=user_id,
        alias_map=alias_map,
    )
    return kept, dropped


_COMPANY_ACRONYM_NAMES_FOR_BACKFILL: frozenset[str] = frozenset(
    {"MKS", "CATL", "SMIC", "TSMC", "BYD", "ASML", "WUS", "AMEC"}
)
_BARE_TICKER_CODE_FOR_BACKFILL_RE = re.compile(
    r"^(?:\d{1,6}(?:\.[A-Z]{1,3})?|[A-Z]{1,5}(?:[.-][A-Z])?)$"
)


@lru_cache(maxsize=4096)
def _is_company_name_for_backfill(alias: str) -> bool:
    """Return True if `alias` is a company name (Chinese or English company name) rather than a bare ticker code."""
    s = str(alias or "").strip()
    if not s:
        return False
    if not s.isascii():
        return True
    if s.upper() == "PULTE":
        # "Pulte" alone in FHFA news refers to Director Bill Pulte; "PulteGroup" is the company name.
        return False
    if s.upper() in _COMPANY_ACRONYM_NAMES_FOR_BACKFILL:
        return True
    if _BARE_TICKER_CODE_FOR_BACKFILL_RE.match(s):
        return False
    return len(s) >= 4


def backfill_tickers_from_quote(
    kept_tickers: list[str],
    quote: str,
    *,
    cited_sources: list[str] | None = None,
    claim: str | None = None,
    topic_label: str | None = None,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], int]:
    """Backfill ticker code(s) when a known company name (`get_user_aliases` + `SUPPLEMENTAL_TICKER_ALIASES`)
    appears as the card subject in `claim` or `topic_label` (and in `quote`) and its ticker is missing from `kept_tickers`.
    - Dual-listed A+H companies (`DUAL_LISTED_TICKER_GROUPS`) get both A and H tickers backfilled.
    - Private companies (`PRIVATE_COMPANIES`) and trading channels (`TRADING_CHANNEL_TICKERS`, unless discussed as a subject in `claim`) are never backfilled.
    - Source institutions (`SOURCE_INSTITUTION_TICKERS` / `cited_sources`) are skipped only when used as a source, and backfilled when they are the subject of the card.
    - Companies appearing only in excluded spans (`剔除/不含/排除/除去/未采用/excluding/doesn't use/...`) are never backfilled.
    """
    q = str(quote or "")
    if not q.strip():
        return list(kept_tickers or []), 0

    _excluded_spans, q_for_backfill = _extract_excluded_spans_and_clean_quote(q)
    # Mask conditional brokerage-account clauses ("如果你在用 IBKR", "如果你在用富途/老虎等券商")
    q_for_backfill = re.sub(r"如果你在用[^：:\n]{1,25}", " ", q_for_backfill)

    has_subject_scope = claim is not None or topic_label is not None
    raw_subject_text = f"{claim or ''}\n{topic_label or ''}" if has_subject_scope else q_for_backfill
    _, subject_text = _extract_excluded_spans_and_clean_quote(raw_subject_text)

    srcs = [str(s or "") for s in (cited_sources or [])]
    eff_alias_map = alias_map if alias_map is not None else _call_load_ticker_company_aliases(user_id)

    dual_pair_by_canon: dict[str, tuple[str, str]] = {}
    for a_code, h_code in DUAL_LISTED_TICKER_GROUPS:
        ca = canonicalize_ticker_key(a_code)
        ch = canonicalize_ticker_key(h_code)
        pair = (ca, ch)
        dual_pair_by_canon[ca] = pair
        dual_pair_by_canon[ch] = pair

    out_tickers = list(kept_tickers or [])
    existing_canon: set[str] = {canonicalize_ticker_key(t) for t in out_tickers if t}
    # Expand `existing_canon` with any co-aliases sharing company names so we don't duplicate ADR/HK/A codes
    # when the card already holds one ticker for that company.
    for ct in list(existing_canon):
        if ct in dual_pair_by_canon:
            existing_canon.update(dual_pair_by_canon[ct])
        ct_names = {a for a in eff_alias_map.get(ct, ()) if _is_company_name_for_backfill(a)}
        if ct_names:
            for other_k, other_aliases in eff_alias_map.items():
                if ct_names & {a for a in other_aliases if _is_company_name_for_backfill(a)}:
                    existing_canon.add(other_k)

    backfilled = 0
    visited_groups: set[tuple[str, ...]] = set()

    for canon_t in eff_alias_map:
        if canon_t in SOURCE_INSTITUTION_TICKERS:
            check_src_text = f"{claim or ''}\n{q_for_backfill}" if claim is not None else q_for_backfill
            if _is_institution_used_as_source(canon_t, check_src_text, srcs):
                continue
        if _is_private_company_ticker(canon_t, user_id=user_id, alias_map=eff_alias_map):
            continue
        if canon_t in TRADING_CHANNEL_TICKERS:
            check_claim = claim if claim is not None else q_for_backfill
            if not _is_trading_channel_discussed_in_claim(
                canon_t, check_claim, user_id=user_id, alias_map=eff_alias_map
            ):
                continue
        group_keys: tuple[str, ...] = dual_pair_by_canon.get(canon_t, (canon_t,))
        if group_keys in visited_groups:
            continue
        visited_groups.add(group_keys)

        if any(gk in existing_canon for gk in group_keys):
            continue

        group_aliases: list[str] = []
        for gk in group_keys:
            for a in eff_alias_map.get(gk, ()):
                if a not in group_aliases:
                    group_aliases.append(a)

        name_aliases = [a for a in group_aliases if _is_company_name_for_backfill(a)]
        if not name_aliases:
            continue

        # Skip if any name alias is cited as a source institution in `cited_sources`
        if any(any(na.lower() in s.lower() for na in name_aliases if len(na) >= 2) for s in srcs):
            continue

        # Item 1: Only backfill companies that appear in `claim` or `topic_label` AND in `q_for_backfill`
        if has_subject_scope and not any(_alias_matches_in_text(na, subject_text) for na in name_aliases):
            continue

        matched_in_quote = any(_alias_matches_in_text(na, q_for_backfill) for na in name_aliases)
        if not matched_in_quote:
            continue

        for gk in group_keys:
            if gk not in out_tickers:
                out_tickers.append(gk)
                backfilled += 1
            existing_canon.add(gk)
        # Also mark any co-ticker sharing the same company name as covered
        group_name_set = set(name_aliases)
        for other_k, other_aliases in eff_alias_map.items():
            if group_name_set & {a for a in other_aliases if _is_company_name_for_backfill(a)}:
                existing_canon.add(other_k)

    return out_tickers, backfilled


_STANDALONE_YEAR_RE = re.compile(
    r"(?<![A-Za-z0-9.$¥￥€£])(?:"
    r"(?:FY|CY)\s*(?:(?:19|20)?\d{2})(?:[EFef](?![A-Za-z0-9]))?"
    r"|(?:19|20)\d{2}(?:\s*年|[EFef](?![A-Za-z0-9]))"
    r")(?!\s*(?:\d|\.|%|亿|万|千|百|元|美元|港元|日元|欧元|英镑|倍|bn|mn|million|billion|trillion)\b)",
    flags=re.IGNORECASE,
)


def _has_third_party_forecast_in_quote(q_text: str) -> bool:
    has_candidate = any(
        (bool(re.search(rf"\b{re.escape(kw)}\b", q_text, flags=re.IGNORECASE)) if kw.isascii() else (kw in q_text))
        for kw in THIRD_PARTY_FORECAST_PHRASES_TABLE
    ) or bool(
        re.search(
            r"(?:卖方|机构|券商|投行|报告|分析师|一致预期|它|consensus|sell[-\s]?side|street)",
            q_text,
            flags=re.IGNORECASE,
        )
    )
    if not has_candidate:
        return False
    for s in _split_sentences(q_text, split_cn_comma=True):
        if "已是" in s or re.search(r"\balready\b", s, flags=re.IGNORECASE):
            continue
        s_no_year = _STANDALONE_YEAR_RE.sub(" ", s)
        has_num = bool(re.search(r"\d", s_no_year)) or bool(_CN_NUM_MENTION_RE.search(s_no_year))
        if not has_num:
            continue
        if _THIRD_PARTY_FORECAST_RE.search(s_no_year) or _THIRD_PARTY_FORECAST_EN_RE.search(s_no_year):
            return True
    return False


def _has_self_calculation_in_quote(q_text: str) -> bool:
    if any(kw in q_text for kw in SELF_CALCULATION_KEYWORDS_TABLE):
        return True
    q_masked = _THIRD_PARTY_AN_SUAN_RE.sub(" ", q_text)
    if "它估计" in q_text or "卖方" in q_text:
        return False
    return bool(_AN_SUAN_RE.search(q_masked))


def has_unverified_source_in_quote(quote: str) -> bool:
    """Return True if `quote` contains a disclaimer that the primary text/source was not opened or verified."""
    return bool(_UNVERIFIED_SOURCE_RE.search(str(quote or "")))


def _has_forward_estimate_keyword(text: str) -> bool:
    masked = _REALIZED_EXPECTATION_MASK_RE.sub(" ", str(text or ""))
    if _FORWARD_JIANG_RE.search(masked):
        return True
    for kw in FACT_TO_ESTIMATE_KEYWORDS_TABLE:
        if kw == "将":
            continue
        if kw.isascii():
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", masked, flags=re.IGNORECASE):
                return True
        elif kw in masked:
            return True
    return False


def correct_card_type_by_keywords(
    card_type: str,
    claim: str,
    quote: str,
) -> tuple[str, int]:
    """Bidirectionally correct `card_type` by keywords:
    - `fact` with `has_unverified_source_in_quote(quote)` stays `fact` (flagged via `unverified_source`).
    - `fact` with `FACT_TO_ESTIMATE_KEYWORDS_TABLE` in `claim` or `quote` (after masking realized expectation
      comparison phrases like `超预期`/`beat expectations`) and no `REALIZED_FACT_PHRASES_TABLE` in `quote` -> `estimate`.
    - `opinion` or `model_assumption` with explicit third-party forecast phrasing + number in `quote` and no
      self-calculation phrasing -> `estimate`.
    - `opinion` with `OPINION_TO_MODEL_ASSUMPTION_KEYWORDS_TABLE` or self `按…算` in `quote` -> `model_assumption`.
    - `estimate` with `ESTIMATE_TO_FACT_PHRASES_TABLE` in `quote` and no `FACT_TO_ESTIMATE_KEYWORDS_TABLE` -> `fact`.
    Returns `(corrected_type, types_corrected_count)`.
    """
    eff_t = str(card_type or "").strip().lower()
    c_text = str(claim or "")
    q_text = str(quote or "")
    combined = f"{c_text}\n{q_text}"

    if eff_t == "fact":
        if has_unverified_source_in_quote(q_text):
            return "fact", 0
        has_realized = any(p in q_text for p in REALIZED_FACT_PHRASES_TABLE)
        if not has_realized and _has_forward_estimate_keyword(combined):
            return "estimate", 1

    elif eff_t in ("opinion", "model_assumption"):
        if _has_third_party_forecast_in_quote(q_text) and not _has_self_calculation_in_quote(q_text):
            return "estimate", 1
        if eff_t == "opinion":
            q_masked = _THIRD_PARTY_AN_SUAN_RE.sub(" ", q_text)
            has_calc = any(kw in q_text for kw in OPINION_TO_MODEL_ASSUMPTION_KEYWORDS_TABLE) or bool(
                _AN_SUAN_RE.search(q_masked)
            )
            if has_calc:
                return "model_assumption", 1

    elif eff_t == "estimate":
        has_fact_marker = False
        for p in ESTIMATE_TO_FACT_PHRASES_TABLE:
            if p.isascii():
                if re.search(rf"\b{re.escape(p)}\b", q_text, flags=re.IGNORECASE):
                    has_fact_marker = True
                    break
            elif p in q_text:
                has_fact_marker = True
                break
        if has_fact_marker:
            check_scope = c_text if ("事实：" in q_text and c_text.strip()) else f"{c_text}\n{q_text}"
            if not _has_forward_estimate_keyword(check_scope):
                return "fact", 1

    return eff_t, 0


_ENGLISH_NON_PROPER_ALLOWLIST: frozenset[str] = frozenset(
    {
        "AI",
        "AI4S",
        "API",
        "ARR",
        "ASP",
        "AWS",
        "B2B",
        "B2C",
        "BM25",
        "BOJ",
        "BP",
        "BPS",
        "CAGR",
        "CAPEX",
        "CEO",
        "CFO",
        "CNY",
        "COO",
        "CPA",
        "CPC",
        "CPI",
        "CPM",
        "CPU",
        "CRM",
        "CTO",
        "CY",
        "DAU",
        "DC",
        "DCF",
        "DRAM",
        "EBIT",
        "EBITDA",
        "EPS",
        "ERP",
        "ESG",
        "ETF",
        "EUR",
        "EUV",
        "EV",
        "EXW",
        "FCF",
        "FDE",
        "FOB",
        "FX",
        "FY",
        "GAAP",
        "GBP",
        "GDP",
        "GMV",
        "GPU",
        "GW",
        "GWH",
        "HBM",
        "HG",
        "HK",
        "HKD",
        "HKT",
        "HY",
        "IAA",
        "IAP",
        "IDC",
        "IG",
        "II",
        "III",
        "IP",
        "IPO",
        "IR",
        "IT",
        "IV",
        "JPY",
        "KRW",
        "KWH",
        "LLM",
        "LP",
        "LTM",
        "MA",
        "MACD",
        "MAU",
        "MI",
        "MOM",
        "MRR",
        "MWH",
        "NAND",
        "NDR",
        "NGS",
        "NIM",
        "NON",
        "NRR",
        "NTM",
        "OP",
        "OPEX",
        "OS",
        "OTC",
        "PB",
        "PC",
        "PCE",
        "PE",
        "PEG",
        "PMI",
        "PP",
        "PPI",
        "PR",
        "PS",
        "PT",
        "PV",
        "QOQ",
        "RAM",
        "RMB",
        "ROE",
        "ROI",
        "ROIC",
        "RPO",
        "RSI",
        "SAAS",
        "SBC",
        "SEC",
        "SKU",
        "SMB",
        "SOHO",
        "SQL",
        "SS",
        "SH",
        "SSD",
        "SZ",
        "TAM",
        "TCO",
        "TMT",
        "TPU",
        "TTM",
        "TV",
        "TWD",
        "UAE",
        "UK",
        "URL",
        "US",
        "USA",
        "USD",
        "UTC",
        "VAT",
        "VC",
        "VIP",
        "VLA",
        "VR",
        "AR",
        "WACC",
        "WFE",
        "WH",
        "WHT",
        "WIFI",
        "YOY",
        "YTD",
        "EVE",
        "OWNER",
        "PARTICIPANT",
        "BEAR",
        "BULL",
        "BASE",
        "DEBATE",
        "STREET",
        "APP",
        "STORE",
        "WEB",
        "CLOUD",
        "IOS",
        "PRO",
        "MAX",
        "MINI",
        "PLUS",
        "ULTRA",
        "LITE",
        "PRIME",
        "DAY",
        "VS",
        # Common financial & sentence-initial English terms
        "REVENUE",
        "REVENUES",
        "SALES",
        "PROFIT",
        "PROFITS",
        "MARGIN",
        "MARGINS",
        "INCOME",
        "EARNINGS",
        "GROWTH",
        "COST",
        "COSTS",
        "CASH",
        "FLOW",
        "DEBT",
        "EQUITY",
        "ASSET",
        "ASSETS",
        "VALUATION",
        "TARGET",
        "PRICE",
        "SHARE",
        "SHARES",
        "STOCK",
        "MARKET",
        "TOTAL",
        "NET",
        "GROSS",
        "OPERATING",
        "FREE",
        "ANNUAL",
        "QUARTER",
        "QUARTERLY",
        "YEAR",
        "HALF",
        "MONTH",
        "WEEK",
        "GUIDANCE",
        "ESTIMATE",
        "ESTIMATES",
        "CONSENSUS",
        "RESULT",
        "RESULTS",
        "REPORT",
        "DATA",
        "MODEL",
        "VOLUME",
        "ORDER",
        "ORDERS",
        "DEMAND",
        "SUPPLY",
        "CAPACITY",
        "RATIO",
        "RATE",
        "RATES",
        "YIELD",
        "RETURN",
        "LOSS",
        "TAX",
        "FEE",
        "FEES",
        "USER",
        "USERS",
        "CUSTOMER",
        "CUSTOMERS",
        "PRODUCT",
        "PRODUCTS",
        "SERVICE",
        "SERVICES",
        "BUSINESS",
        "SEGMENT",
        "COMPANY",
        "GROUP",
        "SECTOR",
        "INDUSTRY",
        "GLOBAL",
        "CHINA",
        "DOMESTIC",
        "OVERSEAS",
        "HIGH",
        "LOW",
        "AVERAGE",
        "KEY",
        "CORE",
        "MAIN",
        "NEW",
        "OLD",
        "FIRST",
        "SECOND",
        "THIRD",
        "FOURTH",
        "ONE",
        "TWO",
        "THREE",
        "FOUR",
        "FIVE",
        "SIX",
        "SEVEN",
        "EIGHT",
        "NINE",
        "TEN",
        "THE",
        "AND",
        "FOR",
        "WITH",
        "FROM",
        "INTO",
        "OVER",
        "UNDER",
        "AFTER",
        "BEFORE",
        "DURING",
        "WHILE",
        "SINCE",
        "UNTIL",
        "ABOVE",
        "BELOW",
        "BETWEEN",
        "AMONG",
        "THROUGH",
        "AGAINST",
        "DESPITE",
        "WITHOUT",
        "WITHIN",
    }
)


_NON_PROPER_EN_PERIOD_CODE_RE = re.compile(
    r"^(?:[1-4]Q|Q[1-4]|[12]H|H[12]|FY\d{2,4}E?|\d{2,4}(?:Q[1-4]|H[12]|FY|E|A)|\d+D|\d+MA|MA\d+|\d+X|\d+BP|M\d{1,2}|W\d{1,2}|V\d+|P\d+|B\d{2,3}|H\d{2,3}|A\d{2,3})$"
)
_NON_PROPER_EN_MONTH_RE = re.compile(
    r"^(?:JAN|JANUARY|FEB|FEBRUARY|MAR|MARCH|APR|APRIL|MAY|JUN|JUNE|JUL|JULY|AUG|AUGUST|SEP|SEPT|SEPTEMBER|OCT|OCTOBER|NOV|NOVEMBER|DEC|DECEMBER)$"
)


@lru_cache(maxsize=4096)
def _is_non_proper_english_token(tok: str) -> bool:
    u = tok.strip().upper()
    if not u or len(u) < 2:
        return True
    if u in _ENGLISH_NON_PROPER_ALLOWLIST:
        return True
    if _NON_PROPER_EN_PERIOD_CODE_RE.match(u):
        return True
    return bool(_NON_PROPER_EN_MONTH_RE.match(u))


def _build_entity_alias_groups(
    user_id: str | None = None,
    *,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """Group all aliases and ticker codes belonging to the same company/institution so that if any alias
    of the entity appears in `quote` (e.g. `SMIC`, `Sieyuan`, `Hynix`, `Tencent`, `澜起`), another alias
    of the same entity in `claim`/`topic_label` (e.g. `中芯国际`, `思源电气`, `SK海力士`, `腾讯`, `澜起科技`) is supported.
    """
    cache_entry, eff_alias_map = _resolve_alias_cache_entry_or_map(user_id, alias_map)
    if cache_entry is not None and cache_entry.entity_alias_groups is not None:
        return cache_entry.entity_alias_groups
    raw_groups: list[set[str]] = []

    for aliases in eff_alias_map.values():
        g = {str(a).strip() for a in aliases if str(a).strip()}
        if g:
            raw_groups.append(g)
    for canon_inst, inst_aliases in SOURCE_INSTITUTION_TICKERS.items():
        g = {canon_inst} | {str(a).strip() for a in inst_aliases if str(a).strip()}
        raw_groups.append(g)
    for inst_group in KNOWN_INSTITUTION_PROPER_NAMES_TABLE:
        g = {str(a).strip() for a in inst_group if str(a).strip()}
        if g:
            raw_groups.append(g)

    # Merge overlapping groups that share any non-trivial company/institution name
    merged: list[set[str]] = []
    for g in raw_groups:
        g_names = {a for a in g if (not a.isascii()) or len(a) >= 4}
        placed = False
        for mg in merged:
            mg_names = {a for a in mg if (not a.isascii()) or len(a) >= 4}
            if (g_names and (g_names & mg_names)) or (g & mg):
                mg.update(g)
                placed = True
                break
        if not placed:
            merged.append(set(g))

    res = tuple(tuple(sorted(mg, key=lambda x: (-len(x), x))) for mg in merged)
    if cache_entry is not None:
        cache_entry.entity_alias_groups = res
    return res


_build_entity_alias_groups.cache_clear = invalidate_ticker_company_aliases_cache  # type: ignore[attr-defined]


def _is_acronym_supported_by_quote_phrase(acronym: str, quote: str) -> bool:
    """Return True if `acronym` is an all-uppercase 3-6 letter token and `quote` contains a contiguous
    word window of exact length `len(acronym)` whose initial letters equal `acronym` (2-letter acronyms
    are never supported by generic phrases regardless of capitalization, e.g. `Market Share` does not support `MS`).
    """
    tok = str(acronym or "").strip()
    if not re.match(r"^[A-Z]{3,6}$", tok):
        return False
    q = str(quote or "")
    if not q:
        return False
    k = len(tok)
    for m in re.finditer(r"[A-Za-z]+(?:[\s\-]+[A-Za-z]+)+", q):
        words = [w for w in re.split(r"[\s\-]+", m.group(0)) if w]
        if len(words) < k:
            continue
        for i in range(len(words) - k + 1):
            window = words[i : i + k]
            if "".join(w[0].upper() for w in window) == tok:
                return True
    return False


def _validate_proper_nouns_and_clean_topic(
    claim_text: str,
    topic_label: str,
    quote: str,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> tuple[list[str], str]:
    """Check company/institution names (from known alias tables) and uppercase/capitalized English proper nouns/codes
    in `claim_text` and `topic_label` against `quote`.
    - Unsupported names in `topic_label` (plus `A股` and `FORBIDDEN_CONCLUSION_WORDS_TABLE` when absent from `quote`)
      are stripped from `topic_label`.
    - Unsupported names in `claim_text` are returned in `unsupported_names`.
    """
    q = _normalize_trad_to_simp(str(quote or ""))
    c_text = _normalize_trad_to_simp(str(claim_text or ""))
    t_text = _normalize_trad_to_simp(str(topic_label or ""))
    raw_t_text = str(topic_label or "")

    groups = _build_entity_alias_groups(user_id=user_id, alias_map=alias_map)
    unsupported_in_claim: list[str] = []
    unsupported_in_topic: list[str] = []

    def _group_supported_in_quote(group: tuple[str, ...]) -> bool:
        return any(_alias_matches_in_text(a, q) for a in group)

    # 1. Check known company/institution names from alias groups
    for group in groups:
        # Only match proper company/institution names (non-ASCII names or unambiguous English names, not bare ticker codes)
        check_names = [a for a in group if (not a.isascii()) or _is_company_name_for_backfill(a)]
        if not check_names:
            continue
        claim_hit = next((a for a in check_names if _alias_matches_in_text(a, c_text)), None)
        topic_hits = [a for a in check_names if _alias_matches_in_text(a, t_text)]
        if not claim_hit and not topic_hits:
            continue
        if _group_supported_in_quote(group):
            continue
        if claim_hit and claim_hit not in unsupported_in_claim:
            unsupported_in_claim.append(claim_hit)
        for th in topic_hits:
            if th not in unsupported_in_topic:
                unsupported_in_topic.append(th)

    # 2. Check uppercase/capitalized English proper nouns and exchange-suffixed ticker codes
    def _find_unsupported_english_tokens(text: str, *, skip_sentence_initial: bool = True) -> list[str]:
        if not text:
            return []
        found: list[str] = []
        # First check exchange-suffixed codes like `9638.HK`, `YACHT.MI`, `300750.SZ`
        for cm in re.finditer(
            r"(?<![A-Za-z0-9])(?:\d{4,6}\.(?:SZ|SS|SH|HK|T|TW|KS)|[A-Z]{1,6}\.(?:MI|L|PA|DE|AS|SW|TO|AX))(?![A-Za-z0-9])",
            text,
        ):
            code_tok = cm.group(0)
            matching_groups = [g for g in groups if any(code_tok.upper() == a.upper() for a in g)]
            is_sup = _alias_matches_in_text(code_tok, q) or any(
                _group_supported_in_quote(g) for g in matching_groups
            )
            if not is_sup and code_tok not in found:
                found.append(code_tok)

        text_no_codes = re.sub(
            r"(?<![A-Za-z0-9])(?:\d{4,6}\.(?:SZ|SS|SH|HK|T|TW|KS)|[A-Z]{1,6}\.(?:MI|L|PA|DE|AS|SW|TO|AX))(?![A-Za-z0-9])",
            " ",
            text,
        )
        # Mask model/product version tokens like `Qwen3-8B`, `GPT-6`, `Fable 5.1`, `H100`, `B200`, `WiFi7`
        text_no_codes = re.sub(
            r"\b(?:Qwen\d*(?:-\d+[A-Za-z]*)?|GPT-\d+[A-Za-z]*|WiFi\d+)\b",
            " ",
            text_no_codes,
            flags=re.IGNORECASE,
        )
        sents_to_scan = _split_sentences(text_no_codes, split_cn_comma=False) if skip_sentence_initial else [text_no_codes]
        for sent in sents_to_scan:
            for wm in re.finditer(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9]{1,}(?![A-Za-z0-9_])", sent):
                word = wm.group(0)
                if _is_non_proper_english_token(word):
                    continue
                if skip_sentence_initial and not word.isupper():
                    prefix = sent[: wm.start()]
                    after = sent[wm.end() :]
                    if not prefix.strip() and not re.match(r"^\s*[\u4e00-\u9fff]", after):
                        continue
                matching_groups = [g for g in groups if any(word.lower() == a.lower() for a in g)]
                is_sup = (
                    _alias_matches_in_text(word, q)
                    or any(_group_supported_in_quote(g) for g in matching_groups)
                    or _is_acronym_supported_by_quote_phrase(word, q)
                )
                if not is_sup and word not in found:
                    found.append(word)
        return found

    for en_tok in _find_unsupported_english_tokens(c_text, skip_sentence_initial=True):
        if not any(en_tok.lower() in existing.lower() or existing.lower() in en_tok.lower() for existing in unsupported_in_claim):
            unsupported_in_claim.append(en_tok)

    for en_tok in _find_unsupported_english_tokens(t_text, skip_sentence_initial=False):
        if en_tok not in unsupported_in_topic:
            unsupported_in_topic.append(en_tok)

    # Also strip "A股" / "A 股" and forbidden conclusion words from `topic_label` when absent from `quote`
    cleaned_topic = raw_t_text
    if cleaned_topic:
        for bad_t in sorted(unsupported_in_topic, key=len, reverse=True):
            if bad_t.isascii():
                cleaned_topic = re.sub(
                    rf"(?<![A-Za-z0-9]){re.escape(bad_t)}(?![A-Za-z0-9])",
                    "",
                    cleaned_topic,
                )
            else:
                cleaned_topic = cleaned_topic.replace(bad_t, "")
                # Also strip Traditional variants if `raw_t_text` had Traditional characters
                if _normalize_trad_to_simp(cleaned_topic) != cleaned_topic:
                    norm_topic = _normalize_trad_to_simp(cleaned_topic)
                    while bad_t in norm_topic:
                        idx = norm_topic.index(bad_t)
                        cleaned_topic = cleaned_topic[:idx] + cleaned_topic[idx + len(bad_t) :]
                        norm_topic = _normalize_trad_to_simp(cleaned_topic)
        for concl_word in FORBIDDEN_CONCLUSION_WORDS_TABLE:
            if concl_word in _normalize_trad_to_simp(cleaned_topic) and concl_word not in q:
                cleaned_topic = cleaned_topic.replace(concl_word, "")
        if ("A股" in cleaned_topic or "A 股" in cleaned_topic) and ("A股" not in q and "A 股" not in q):
            cleaned_topic = re.sub(r"A\s*股", "", cleaned_topic)
        # Clean up empty parentheses, leftover slashes, and leading/trailing punctuation
        cleaned_topic = re.sub(r"[（(]\s*[/|、，,\-–—\s]*\s*[）)]", "", cleaned_topic)
        cleaned_topic = re.sub(r"\s+", " ", cleaned_topic).strip(" ·|/-–—、，,：:")

    return unsupported_in_claim, cleaned_topic


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
    unsupported_names: list[str] = field(default_factory=list)
    unsupported_conclusions: list[str] = field(default_factory=list)
    unverified_source: bool = False
    soft_unsupported_numbers: int = 0
    figures_dropped: int = 0
    periods_cleared: int = 0
    currencies_stripped: int = 0
    tickers_dropped: int = 0
    tickers_dropped_private: int = 0
    tickers_dropped_channel: int = 0
    tickers_backfilled: int = 0
    types_corrected: int = 0


def validate_candidate_card(
    card: dict[str, Any],
    quote: str | None = None,
    *,
    user_id: str | None = None,
    alias_map: dict[str, tuple[str, ...]] | None = None,
) -> CardValidationResult:
    """Validate candidate card's numbers (`claim` & `figures.value`), `figures.period`, `figures.unit` currency,
    `tickers` (validation + alias backfill), proper nouns (`claim` & `topic_label`), forbidden conclusions,
    and keyword-based `type` corrections against `quote`.
    """
    eff_quote = str(quote if quote is not None else (card.get("quote") or ""))
    eff_alias_map = alias_map if alias_map is not None else _call_load_ticker_company_aliases(user_id)
    q_numbers = extract_quote_supported_numbers(eff_quote, user_id=user_id, alias_map=eff_alias_map)

    raw_figures = card.get("figures") if isinstance(card.get("figures"), list) else []
    kept_figures: list[dict[str, Any]] = []
    figures_dropped = 0
    periods_cleared = 0
    currencies_stripped = 0

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
        if raw_period and not is_period_supported_by_quote(
            raw_period,
            eff_quote,
            value=fig.get("value"),
            unit=fig_unit,
            user_id=user_id,
            alias_map=eff_alias_map,
        ):
            eff_period = ""
            periods_cleared += 1
        else:
            eff_period = raw_period
        eff_unit, was_cur_stripped = strip_unsupported_currency_from_unit(
            fig_unit,
            fig.get("value"),
            eff_quote,
            period=eff_period,
            user_id=user_id,
            alias_map=eff_alias_map,
        )
        if was_cur_stripped:
            currencies_stripped += 1
        kept_figures.append(
            {
                "metric": str(fig.get("metric") or "").strip(),
                "value": fig.get("value"),
                "unit": eff_unit,
                "period": eff_period,
            }
        )

    claim_text = str(card.get("claim") or "")
    raw_topic_label = str(card.get("topic_label") or "")
    unsupported_names, cleaned_topic_label = _validate_proper_nouns_and_clean_topic(
        claim_text, raw_topic_label, eff_quote, user_id=user_id, alias_map=eff_alias_map
    )
    if "topic_label" in card:
        card["topic_label"] = cleaned_topic_label

    raw_tickers = card.get("tickers") if isinstance(card.get("tickers"), list) else []
    raw_sources = card.get("cited_sources") if isinstance(card.get("cited_sources"), list) else []
    clean_sources = [str(s) for s in raw_sources if s is not None]
    (
        kept_tickers,
        tickers_dropped,
        tickers_dropped_private,
        tickers_dropped_channel,
    ) = _validate_tickers_against_quote_detailed(
        [str(t) for t in raw_tickers if t is not None],
        eff_quote,
        cited_sources=clean_sources,
        claim=claim_text,
        user_id=user_id,
        alias_map=eff_alias_map,
    )
    kept_tickers, tickers_backfilled = backfill_tickers_from_quote(
        kept_tickers,
        eff_quote,
        cited_sources=clean_sources,
        claim=claim_text,
        topic_label=cleaned_topic_label,
        user_id=user_id,
        alias_map=eff_alias_map,
    )

    unsupported_conclusions: list[str] = []
    for word in FORBIDDEN_CONCLUSION_WORDS_TABLE:
        if word in claim_text and word not in eff_quote and word not in unsupported_conclusions:
            unsupported_conclusions.append(word)

    types_corrected = 0
    if "type" in card and card.get("type") is not None:
        corrected_type, types_corrected = correct_card_type_by_keywords(
            str(card.get("type") or ""), claim_text, eff_quote
        )
        card["type"] = corrected_type

    claim_mentions = _parse_number_mentions_detailed(
        claim_text, strip_non_metrics=True, user_id=user_id, alias_map=eff_alias_map
    )
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

    eff_conf = orig_conf
    unverified_source = False
    if str(card.get("type") or "").strip().lower() == "fact" and has_unverified_source_in_quote(eff_quote):
        unverified_source = True
        eff_conf = min(eff_conf, 0.5)
        kept_figures.append(
            {
                "metric": "_verification",
                "value": "unverified_source",
                "unit": "",
                "period": "",
            }
        )
    if unsupported_names:
        eff_conf = min(eff_conf, 0.4)
        kept_figures.append(
            {
                "metric": "_verification",
                "value": f"unsupported_names:{','.join(unsupported_names)}",
                "unit": "",
                "period": "",
            }
        )
    if unsupported_conclusions:
        eff_conf = min(eff_conf, 0.4)
        kept_figures.append(
            {
                "metric": "_verification",
                "value": f"unsupported_conclusion:{','.join(unsupported_conclusions)}",
                "unit": "",
                "period": "",
            }
        )
    if unsupported_claim_nums:
        eff_conf = min(eff_conf, 0.4)
        kept_figures.append(
            {
                "metric": "_verification",
                "value": f"unsupported:{','.join(unsupported_claim_nums)}",
                "unit": "",
                "period": "",
            }
        )

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
        unsupported_names=unsupported_names,
        unsupported_conclusions=unsupported_conclusions,
        unverified_source=unverified_source,
        soft_unsupported_numbers=soft_unsupported_numbers,
        figures_dropped=figures_dropped,
        periods_cleared=periods_cleared,
        currencies_stripped=currencies_stripped,
        tickers_dropped=tickers_dropped,
        tickers_dropped_private=tickers_dropped_private,
        tickers_dropped_channel=tickers_dropped_channel,
        tickers_backfilled=tickers_backfilled,
        types_corrected=types_corrected,
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


def is_fallback_eligible(outcome: SegmentExtractionOutcome) -> bool:
    """Return True when a failed segment extraction outcome is eligible for a single fallback-model retry."""
    if outcome.ok or outcome.permanent_quota_error or is_permanent_quota_error(outcome):
        return False
    if outcome.rate_limited or is_rate_limit_or_quota_error(outcome):
        return False
    if is_in_flight_error(outcome):
        return False
    if (
        str(outcome.status or "").strip().lower() == "indeterminate"
        or str(outcome.error_kind or "").strip().lower() in ("indeterminate", "timeout")
        or is_transient_network_error(outcome)
        or bool(outcome.transient_error)
    ):
        return True
    kind = f"{outcome.error_kind or ''} {outcome.error or ''}".lower()
    return "timeout" in kind or "timed out" in kind or "上次调用在返回前中断" in kind


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
    deadline: float | None = None,
) -> SegmentExtractionOutcome:
    """Extract and validate knowledge cards from one conversation segment via `assist_json`."""
    cfg = get_dalton_settings().conversations
    eff_model = (extractor_model or "").strip()
    if not eff_model:
        eff_model = (await resolve_dalton_extract_model(user_id)).strip()
    eff_prompt_ver = (prompt_version or cfg.prompt_version or DEFAULT_PROMPT_VERSION).strip()

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
        role="extract",
        scope_ref=scope_ref,
        timeout_s=180.0,
        deadline=deadline,
        model=eff_model or None,
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
    cards_flagged_unsupported_names = 0
    cards_flagged_unsupported_conclusion = 0
    soft_unsupported_numbers = 0
    figures_dropped = 0
    periods_cleared = 0
    currencies_stripped = 0
    tickers_dropped = 0
    tickers_dropped_private = 0
    tickers_dropped_channel = 0
    tickers_backfilled = 0
    types_corrected = 0

    segment_alias_map = _call_load_ticker_company_aliases(user_id) if raw_cards else None

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
        raw_tickers_list = raw_c.get("tickers") if isinstance(raw_c.get("tickers"), list) else []
        private_dropped_in_norm = 0
        seen_priv_norm: set[str] = set()
        for rt in raw_tickers_list:
            rt_str = str(rt or "").strip()
            if not rt_str:
                continue
            rt_canon = canonicalize_ticker_key(rt_str.upper())
            if rt_canon not in seen_priv_norm and _is_private_company_ticker(
                rt_str, user_id=user_id, alias_map=segment_alias_map
            ):
                seen_priv_norm.add(rt_canon)
                private_dropped_in_norm += 1
        tickers, entities = normalize_tickers_and_entities(
            raw_tickers_list,
            raw_c.get("entities") if isinstance(raw_c.get("entities"), list) else [],
            user_id=user_id,
            alias_map=segment_alias_map,
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

        raw_conf = raw_c.get("confidence", 0.8)
        conf = float(0.8 if raw_conf is None else raw_conf)
        conf = max(0.0, min(1.0, conf))

        ev_ts = str(matched_ev.get("ts") or first_ev.get("ts") or datetime.now(timezone.utc).isoformat())
        ev_date_et = str(matched_ev.get("date_et") or first_ev.get("date_et") or ev_ts[:10])
        topic_label = str(raw_c.get("topic_label") or "").strip()
        claim = str(raw_c.get("claim") or "").strip()
        if not claim:
            continue

        pre_corr_type = eff_type
        candidate_for_validation: dict[str, Any] = {
            "claim": claim,
            "topic_label": topic_label,
            "type": eff_type,
            "figures": figures,
            "tickers": tickers,
            "quote": raw_quote,
            "cited_sources": cited_sources,
            "confidence": conf,
        }
        val_res = validate_candidate_card(
            candidate_for_validation,
            quote=raw_quote,
            user_id=user_id,
            alias_map=segment_alias_map,
        )
        figures_dropped += val_res.figures_dropped
        periods_cleared += val_res.periods_cleared
        currencies_stripped += val_res.currencies_stripped
        tickers_dropped += private_dropped_in_norm + val_res.tickers_dropped
        tickers_dropped_private += private_dropped_in_norm + val_res.tickers_dropped_private
        tickers_dropped_channel += val_res.tickers_dropped_channel
        tickers_backfilled += val_res.tickers_backfilled
        soft_unsupported_numbers += val_res.soft_unsupported_numbers
        if val_res.unsupported_claim_numbers:
            cards_flagged_unsupported_number += 1
        if val_res.unsupported_names:
            cards_flagged_unsupported_names += 1
        if val_res.unsupported_conclusions:
            cards_flagged_unsupported_conclusion += 1

        tickers = val_res.tickers
        figures = val_res.figures
        conf = val_res.confidence
        topic_label = str(candidate_for_validation.get("topic_label") or "").strip()
        eff_type = str(candidate_for_validation.get("type") or eff_type)
        # Re-enforce source citation constraints once on the final post-correction card type (no loop).
        speaker, eff_type, cited_sources = enforce_speaker_and_agent_citation(
            eff_type,
            cited_sources,
            matched_ev,
            quote=raw_quote,
            segment_events=segment,
        )
        if eff_type != pre_corr_type:
            types_corrected += val_res.types_corrected

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
        cards_flagged_unsupported_names=cards_flagged_unsupported_names,
        cards_flagged_unsupported_conclusion=cards_flagged_unsupported_conclusion,
        soft_unsupported_numbers=soft_unsupported_numbers,
        figures_dropped=figures_dropped,
        periods_cleared=periods_cleared,
        currencies_stripped=currencies_stripped,
        tickers_dropped=tickers_dropped,
        tickers_dropped_private=tickers_dropped_private,
        tickers_dropped_channel=tickers_dropped_channel,
        tickers_backfilled=tickers_backfilled,
        types_corrected=types_corrected,
        discarded_segment=False,
        model_used=used_model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        total_tokens=tot_tok,
        cached=is_cached,
        replayed=is_replayed,
    )
