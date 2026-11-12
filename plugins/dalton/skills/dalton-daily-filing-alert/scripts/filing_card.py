"""Daily filing card builder.

Pure function: structured payload in, Card Schema 2.0 dict out. Field order,
key/routine split, and amount totals match the historical filing card. This
module does not read credentials or send anything. Markdown element splitting
stays in the bull/bear renderer; this layout is the filing-specific card.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_ROUTINE_THRESHOLD_USD = 10_000_000

TAG_RULES: list[tuple[str, str, int]] = [
    (r"^(10-K|10-Q|20-F|年报|半年报|季报|业绩|财报)", "blue", 10),
    (r"^(8-K|6-K)", "orange", 20),
    (r"^(13[DG]|SC 13|权益变动|举牌)", "purple", 30),
    (r"^(S-1|S-3|424|招股|配售|增发|回购)", "green", 40),
    (r"^(A股公告|港股公告|公告|HKEX)", "green", 50),
    (r"^(Form 144|144)", "red", 60),
    (r"^(Form 4|4)$|^Form 4", "grey", 70),
]
DEFAULT_TAG_COLOR = "grey"
DEFAULT_TAG_WEIGHT = 55
SUBSTANTIVE_WEIGHT_MAX = 50

MARKET_SHORT = {"us": "美股", "cn": "A股", "hk": "港股", "cnhk": "A股/港股"}
MARKET_ORDER = ["us", "cn", "hk", "cnhk", ""]

AMOUNT_RE = re.compile(r"([\d][\d,]*(?:\.\d+)?)\s*(亿|万)?\s*美元")


def tag_style(tag: str) -> tuple[str, int]:
    """Return ``(color, sort weight)`` for a form or announcement tag."""
    name = (tag or "").strip()
    for pattern, color, weight in TAG_RULES:
        if re.match(pattern, name, flags=re.IGNORECASE):
            return color, weight
    return DEFAULT_TAG_COLOR, DEFAULT_TAG_WEIGHT


def short_date(value: str) -> str:
    """2026-08-27 → 08-27. Other text is returned unchanged."""
    matched = re.match(r"^\d{4}-(\d{2}-\d{2})$", (value or "").strip())
    return matched.group(1) if matched else (value or "").strip()


def parse_amount_usd(text: str) -> float:
    """First 「约 X 万/亿美元」 in ``text``, in dollars. Missing amounts are 0."""
    matched = AMOUNT_RE.search(text or "")
    if not matched:
        return 0.0
    try:
        base = float(matched.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    unit = matched.group(2)
    if unit == "亿":
        return base * 100_000_000
    if unit == "万":
        return base * 10_000
    return base


def format_amount_usd(value: float) -> str:
    """7110000 → 711 万美元. 591700000 → 5.92 亿美元."""
    if value <= 0:
        return ""
    if value >= 100_000_000:
        yi = f"{value / 100_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{yi} 亿美元"
    wan = value / 10_000
    if wan >= 100:
        return f"{wan:,.0f} 万美元"
    return f"{wan:,.1f} 万美元"


def company_amount_usd(company: dict[str, Any]) -> float:
    """Explicit ``amount_usd`` wins. Otherwise sum amounts parsed from item text."""
    total = 0.0
    for item in company.get("items", []) or []:
        explicit = item.get("amount_usd")
        if isinstance(explicit, (int, float)) and explicit > 0:
            total += float(explicit)
        else:
            total += parse_amount_usd(str(item.get("text", "")))
    return total


def company_weight(company: dict[str, Any]) -> int:
    weights = [tag_style(item.get("tag", ""))[1] for item in company.get("items", []) or []]
    return min(weights) if weights else DEFAULT_TAG_WEIGHT


def is_key_company(company: dict[str, Any], threshold_usd: float) -> bool:
    """Substantive filings, or sale amounts at or above the threshold, are key."""
    if company_weight(company) <= SUBSTANTIVE_WEIGHT_MAX:
        return True
    return company_amount_usd(company) >= threshold_usd


def company_market(company: dict[str, Any], section_key: str) -> str:
    market = str(company.get("market", "")).strip().lower()
    return market or str(section_key or "").strip().lower()


def tag_badges(company: dict[str, Any]) -> str:
    """Form badges on the company line, ordered by weight. Repeats show ×N."""
    counts: dict[str, int] = {}
    for item in company.get("items", []) or []:
        tag = str(item.get("tag", "")).strip()
        if tag:
            counts[tag] = counts.get(tag, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda pair: (tag_style(pair[0])[1], pair[0]))
    parts = []
    for tag, count in ordered:
        color, _weight = tag_style(tag)
        label = f"{tag} ×{count}" if count > 1 else tag
        parts.append(f"<font color='{color}'>{label}</font>")
    return " ".join(parts)


def date_link(item: dict[str, Any]) -> str:
    """The date is the source link. Without a url it is a grey date."""
    date = short_date(str(item.get("date", "")))
    url = str(item.get("url", "")).strip()
    if not date:
        return f"[原文]({url})" if url else ""
    return f"[{date}]({url})" if url else f"<font color='grey'>{date}</font>"


def render_company(company: dict[str, Any], market: str = "", show_market: bool = False) -> str:
    """Key-tier block: title line, summary when there are multiple items, grey lines."""
    code = str(company.get("code", "")).strip()
    name = str(company.get("name", "")).strip()
    head_bits = [f"**{code}**" if code else ""]
    meta = []
    if show_market and MARKET_SHORT.get(market):
        meta.append(MARKET_SHORT[market])
    if name:
        meta.append(name)
    if meta:
        head_bits.append(f"<font color='grey'>{' · '.join(meta)}</font>")
    badges = tag_badges(company)
    if badges:
        head_bits.append(badges)
    amount = format_amount_usd(company_amount_usd(company))
    if amount:
        head_bits.append(f"<font color='grey'>合计 {amount}</font>")
    lines = [" ".join(bit for bit in head_bits if bit)]

    items = [item for item in (company.get("items", []) or []) if str(item.get("text", "")).strip()]
    summary = str(company.get("summary", "")).strip()
    if summary and len(items) > 1:
        lines.append(summary)

    for item in items:
        text = str(item.get("text", "")).strip()
        trailing = date_link(item)
        line = f"<font color='grey'>· {text}</font>"
        if trailing:
            line += f"  {trailing}"
        lines.append(line)
    return "\n".join(lines)


def render_routine_company(company: dict[str, Any], market: str = "", show_market: bool = False) -> str:
    """Routine-tier company compressed to one line. Summary wins over the first item."""
    code = str(company.get("code", "")).strip()
    name = str(company.get("name", "")).strip()
    items = [item for item in (company.get("items", []) or []) if str(item.get("text", "")).strip()]
    summary = str(company.get("summary", "")).strip()
    if not summary and items:
        summary = str(items[0].get("text", "")).strip()

    head = f"**{code}**" if code else ""
    market_label = MARKET_SHORT.get(market, "") if show_market else ""
    meta = [bit for bit in (market_label, name) if bit]
    body_bits = []
    if meta:
        body_bits.append(" · ".join(meta))
    if summary:
        body_bits.append(summary)
    line = head
    if body_bits:
        line += f" <font color='grey'>{' · '.join(body_bits)}</font>"
    links = [link for link in (date_link(item) for item in items) if link]
    if links:
        line += "  " + " ".join(links[:4])
    return line.strip()


def total_scanned(stats: dict[str, Any]) -> int:
    """Sum of ``scanned`` across markets. The card shows only this total."""
    total = 0
    for entry in (stats or {}).values():
        if not isinstance(entry, dict):
            continue
        scanned = entry.get("scanned")
        if isinstance(scanned, (int, float)) and scanned > 0:
            total += int(scanned)
    return total


def label_element(text: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "text_size": "notation",
        "content": f"<font color='grey'>{text}</font>",
    }


def collapsible(title: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": title},
            "vertical_align": "center",
            "padding": "4px 8px 4px 8px",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "12px 12px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "6px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": elements,
    }


def collect(payload: dict[str, Any]) -> tuple[list[tuple[str, dict]], list[tuple[str, dict]]]:
    """Flatten companies into ``(market, company)`` and split key vs routine."""
    threshold = payload.get("routine_threshold_usd")
    if not isinstance(threshold, (int, float)):
        threshold = DEFAULT_ROUTINE_THRESHOLD_USD

    key_tier: list[tuple[str, dict]] = []
    routine_tier: list[tuple[str, dict]] = []
    for section in payload.get("sections", []) or []:
        section_key = str(section.get("key", "")).strip().lower()
        for company in section.get("companies", []) or []:
            market = company_market(company, section_key)
            bucket = key_tier if is_key_company(company, float(threshold)) else routine_tier
            bucket.append((market, company))

    def order(pair: tuple[str, dict]) -> tuple[int, int, float]:
        market, company = pair
        market_index = MARKET_ORDER.index(market) if market in MARKET_ORDER else len(MARKET_ORDER)
        return (market_index, company_weight(company), -company_amount_usd(company))

    return sorted(key_tier, key=order), sorted(routine_tier, key=order)


def build_card(payload: dict[str, Any]) -> dict[str, Any]:
    """Build one Card Schema 2.0 object. ``warnings`` and ``stats.*.hit`` are ignored."""
    date = str(payload.get("date", "")).strip()
    weekday = str(payload.get("weekday", "")).strip()
    window_note = str(payload.get("window_note", "")).strip()
    scanned = total_scanned(payload.get("stats", {}) or {})
    scan_note = f"扫描 {scanned} 家" if scanned else ""
    subtitle_bits = [bit for bit in (date, weekday, window_note, scan_note) if bit]

    key_tier, routine_tier = collect(payload)
    total_hits = len(key_tier) + len(routine_tier)

    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": "每日公告提醒"},
        "template": "indigo" if key_tier else ("wathet" if total_hits else "grey"),
    }
    if subtitle_bits:
        header["subtitle"] = {"tag": "plain_text", "content": " · ".join(subtitle_bits)}
    if total_hits:
        tag_text = f"重点 {len(key_tier)}" if key_tier else "仅例行披露"
        if routine_tier and key_tier:
            tag_text += f" · 例行 {len(routine_tier)}"
    else:
        tag_text = "无新动态"
    header["text_tag_list"] = [{
        "tag": "text_tag",
        "text": {"tag": "plain_text", "content": tag_text},
        "color": "blue" if key_tier else "neutral",
    }]

    elements: list[dict[str, Any]] = []
    lead = str(payload.get("lead", "")).strip()
    if lead:
        elements.append({"tag": "markdown", "content": f"**{lead}**"})

    if not total_hits:
        elements.append({
            "tag": "markdown",
            "content": "<font color='grey'>扫描窗口内无新动态。</font>",
        })

    if key_tier:
        if elements:
            elements.append({"tag": "hr"})
        markets = {market for market, _company in key_tier}
        show_market = len(markets) > 1
        elements.append(label_element(f"重点 · {len(key_tier)} 家"))
        for market, company in key_tier:
            block = render_company(company, market, show_market)
            if block.strip():
                elements.append({"tag": "markdown", "content": block})

    if routine_tier:
        elements.append({"tag": "hr"})
        routine_amount = sum(company_amount_usd(company) for _market, company in routine_tier)
        title = f"**例行披露 · {len(routine_tier)} 家**"
        amount_text = format_amount_usd(routine_amount)
        if amount_text:
            title += f" <font color='grey'>合计 {amount_text}</font>"
        routine_multi_market = len({market for market, _company in routine_tier}) > 1
        inner = [
            {
                "tag": "markdown",
                "content": render_routine_company(company, market, routine_multi_market),
            }
            for market, company in routine_tier
        ]
        elements.append(collapsible(title, inner))

    footer = str(payload.get("footer", "")).strip()
    if footer:
        footer_lines = [
            f"<font color='grey'>{line}</font>" if line.strip() else ""
            for line in footer.splitlines()
        ]
        elements.append({
            "tag": "markdown",
            "text_size": "notation",
            "content": "\n".join(footer_lines),
        })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "10px",
            "elements": elements,
        },
    }


def self_check() -> None:
    """Offline checks copied from the historical card self-test."""
    sample = {
        "date": "2026-01-02",
        "weekday": "周五",
        "window_note": "覆盖过去 24 小时",
        "stats": {"us": {"scanned": 34}, "cn": {"scanned": 40}},
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [
                {
                    "code": "BIG",
                    "name": "重点公司",
                    "summary": "摘要",
                    "items": [
                        {
                            "tag": "8-K",
                            "text": "重大事项",
                            "date": "2026-01-01",
                            "url": "https://example.com/a",
                        },
                        {"tag": "10-K", "text": "年报", "date": "2026-01-01"},
                    ],
                },
                {
                    "code": "SELL",
                    "name": "大额减持",
                    "items": [{
                        "tag": "Form 144",
                        "text": "拟售 1 股，约 5,000 万美元",
                        "date": "2026-01-01",
                    }],
                },
                {
                    "code": "TINY",
                    "name": "例行公司",
                    "summary": "税款代扣",
                    "items": [{"tag": "Form 4", "text": "Tax Withholding", "date": "2026-01-01"}],
                },
            ],
        }],
    }
    card = build_card(sample)
    elements = card["body"]["elements"]
    if card["header"]["template"] != "indigo":
        raise AssertionError("key tier uses indigo")
    if any(element.get("tag") == "column_set" for element in elements):
        raise AssertionError("no per-market stat block")
    if "扫描 74 家" not in card["header"]["subtitle"]["content"]:
        raise AssertionError("subtitle scanned total")
    if elements[0].get("tag") == "hr":
        raise AssertionError("no leading rule without a lead")
    panels = [element for element in elements if element.get("tag") == "collapsible_panel"]
    if len(panels) != 1 or "TINY" not in panels[0]["elements"][0]["content"]:
        raise AssertionError("routine panel")
    if "重点 2" not in card["header"]["text_tag_list"][0]["text"]["content"]:
        raise AssertionError("header tag")
    if not tag_style("Form 144")[1] < tag_style("Form 4")[1]:
        raise AssertionError("tag weight")
    if short_date("2026-08-27") != "08-27":
        raise AssertionError("short date")
    if parse_amount_usd("约 5,917 万美元") != 59_170_000:
        raise AssertionError("wan amount")
    if parse_amount_usd("约 1.5 亿美元") != 150_000_000:
        raise AssertionError("yi amount")
    if parse_amount_usd("没有金额") != 0:
        raise AssertionError("missing amount")
    if format_amount_usd(7_110_000) != "711 万美元":
        raise AssertionError("format wan")
    if format_amount_usd(59_170_000) != "5,917 万美元":
        raise AssertionError("format thousands")
    if format_amount_usd(150_000_000) != "1.5 亿美元":
        raise AssertionError("format yi")
    if "×2" not in tag_badges({"items": [{"tag": "Form 4"}, {"tag": "Form 4"}]}):
        raise AssertionError("badge count")
    if date_link({"date": "2026-01-01", "url": "u"}) != "[01-01](u)":
        raise AssertionError("date link")
    if total_scanned({"us": {"scanned": 34}, "cn": {}}) != 34 or total_scanned({}) != 0:
        raise AssertionError("scanned sum")
    empty = build_card({"date": "2026-01-02", "sections": []})
    if empty["header"]["template"] != "grey":
        raise AssertionError("empty template")
    if any(element.get("tag") == "collapsible_panel" for element in empty["body"]["elements"]):
        raise AssertionError("empty has no routine panel")
