"""Offline parse of A-share next-day disclosure text.

The agent supplies the notice body. This module writes the same fields the
old enrich step wrote: body_excerpt, and for a next-day form, ndr and digest.
It does not fetch pages.
"""

from __future__ import annotations

import re

DATE_RE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
CUR_RE = r"(RMB|HKD|USD)"
BOILERPLATE_RE = re.compile(
    r"本公司及董事会全体成员保证[^。]*。|本公司及董事会全体成员保证[^\n]*"
    r"|没有虚假记载、误导性陈述或重大遗漏[^。]*。"
)
CUR_CN = {"RMB": "元人民币", "HKD": "港元", "USD": "美元"}
CUR_UNIT = {"RMB": "元", "HKD": "港元", "USD": "美元"}

SAMPLE_TREASURY = """翌日披露報表
公司名稱： 中际旭创（「本公司」）
呈交日期： 2026年9月23日
第一章節

1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300308 說明 A股

A. 已發行股份或庫存股份變動
於下列日期開始時的結存(註1) 2026年9月21日 1,110,438,558 4,796,083 1,115,234,641
1). 購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927.2463
變動日期 2026年9月23日
於下列日期結束時的結存 (註5及6) 2026年9月23日 1,109,581,578 5,653,063 1,115,234,641
B. 贖回/購回股份
確認
第一章節註釋：
購回報告
第二章節

1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300308 說明 A股

A. 購回報告
1). 2026年9月23日 於其他證券交易所進行 RMB 935 RMB 921.85 RMB 794,631,549.33
深圳證券交易所
合共購回股份總數 856,980
合共付出的價格總額 (元) RMB 794,631,549.33
購回股份（擬註銷）數目
購回股份（擬持作庫存股份） 856,980
第二章節註釋：
在場内出售庫存股份報告 不適用
"""

SAMPLE_CANCEL = """翌日披露報表
公司名稱： 宁德时代（「本公司」）
呈交日期： 2026年9月23日
第一章節

1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300750 說明 A股

A. 已發行股份或庫存股份變動
於下列日期開始時的結存(註1) 2026年9月21日 4,380,621,225 28,319,044 4,408,940,269
1). 其他 (請註明)
變動日期 2026年9月23日
於下列日期結束時的結存 (註5及6) 2026年9月23日 4,380,621,225 28,319,044 4,408,940,269
B. 贖回/購回股份 (擬註銷但截至期終結存日期尚未註銷)
1). 購回股份 (擬註銷但尚未註銷)  604,293  0.01 %  RMB  330.93
變動日期 2026年9月11日
2). 購回股份 (擬註銷但尚未註銷)  665,060  0.02 %  RMB  300.71
變動日期 2026年9月23日
確認
第一章節註釋：
購回報告
第二章節

1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300750 說明 A股

A. 購回報告
1). 2026年9月23日 665,060 於其他證券交易所進行 RMB 301 RMB 300.23 RMB 199,987,058.39
深圳證券交易所
合共購回股份總數 665,060
合共付出的價格總額 (元) RMB 199,987,058.39
購回股份（擬註銷）數目 665,060
購回股份（擬持作庫存股份） 0
第二章節註釋：
在場内出售庫存股份報告 不適用
"""

SAMPLE_OPTION = """翌日披露報表
公司名稱： 示例科技（「本公司」）
呈交日期： 2026年9月23日
第一章節

1. 股份分類 普通股 股份類別 H 於香港聯交所上市 是
證券代號 (如上市) 09992 說明 H股

A. 已發行股份或庫存股份變動
於下列日期開始時的結存(註1) 2026年9月1日 1,000,000 1,000,000
1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5
變動日期 2026年9月22日
於下列日期結束時的結存 (註5及6) 2026年9月22日 1,012,000 1,012,000
確認
在場内出售庫存股份報告 不適用
"""


def body_excerpt(text: str, limit: int = 600) -> str:
    flat = re.sub(r"\s+", " ", BOILERPLATE_RE.sub("", text or "")).strip()
    return flat[:limit]


def _num(raw):
    if raw is None or raw == "":
        return None
    token = raw.replace(",", "")
    try:
        return int(token) if re.fullmatch(r"-?\d+", token) else float(token)
    except ValueError:
        return None


def _date(found):
    matched = re.search(DATE_RE, found) if isinstance(found, str) else found
    if not matched:
        return None
    year, month, day = matched.groups()[:3]
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _split(text: str, start: str, end_markers: list) -> str:
    start_at = text.find(start)
    if start_at < 0:
        return ""
    end_at = len(text)
    for marker in end_markers:
        found = text.find(marker, start_at + len(start))
        if 0 <= found < end_at:
            end_at = found
    return text[start_at:end_at]


def _class_blocks(section: str) -> list:
    """Split one chapter into share-class blocks (A shares, H shares)."""
    parts = re.split(r"\n\s*\d+\.\s*股份分類", "\n" + section)
    return [part for part in parts[1:] if part.strip()]


def _class_meta(block: str) -> dict:
    meta = {}
    share_class = re.search(r"股份類別\s+(\S+)", block)
    meta["share_class"] = share_class.group(1) if share_class else None
    listed = re.search(r"於香港聯交所上市\s+(是|否)", block)
    meta["hk_listed"] = (listed.group(1) == "是") if listed else None
    code = re.search(r"證券代號\s*\(如上市\)\s+(\S+)", block)
    meta["stock_code"] = code.group(1) if code else None
    description = re.search(r"說明\s+(\S+)", block)
    meta["description"] = description.group(1) if description else None
    return meta


def _balance(block: str, label: str) -> dict:
    matched = re.search(label + r"[^\n]*?" + DATE_RE + r"([^\n]*)", block)
    if not matched:
        return {}
    numbers = [_num(token) for token in re.findall(r"-?[\d,]{2,}", matched.group(4))]
    out = {"date": _date(matched)}
    if len(numbers) >= 3:
        out.update(issued_ex_treasury=numbers[0], treasury=numbers[1], total=numbers[2])
    elif len(numbers) == 2:
        out.update(issued_ex_treasury=numbers[0], total=numbers[1])
    return out


def _events(part: str) -> list:
    """Parse numbered events and the following change-date line."""
    events = []
    lines = part.splitlines()
    for index, line in enumerate(lines):
        matched = re.match(r"^\s*\d+\)\.\s+(\S.*?)(?:\s{2,}(.*))?$", line)
        if not matched or "購回授權" in line:
            continue
        name, rest = matched.group(1).strip(), (matched.group(2) or "")
        event = {"event": name}
        numbers = re.match(
            r"\s*(-?[\d,]+)\s+([\d.]+)\s*%\s*(?:(-?[\d,]+)\s+)?(?:" + CUR_RE + r"\s+([\d.,]+))?",
            rest,
        )
        if numbers:
            event["shares"] = _num(numbers.group(1))
            event["pct"] = _num(numbers.group(2))
            if numbers.group(3):
                event["treasury_change"] = _num(numbers.group(3))
            if numbers.group(4):
                event["currency"] = numbers.group(4)
                event["price"] = _num(numbers.group(5))
        extra = []
        for nxt in lines[index + 1 : index + 8]:
            if re.match(r"^\s*\d+\)\.\s+", nxt) or re.search(r"於下列日期|^\s*[A-C]\.\s", nxt):
                break
            change_date = re.search(r"變動日期\s+" + DATE_RE, nxt)
            if change_date:
                event["date"] = _date(change_date)
                break
            if nxt.strip():
                extra.append(nxt.strip())
        if extra and not numbers:
            event["note"] = "".join(extra)
        events.append(event)
    return events


def _buyback_report(block: str) -> dict:
    report = {"trades": []}
    trade_re = (
        r"^\s*\d+\)\.\s+" + DATE_RE + r"\s+(?:([\d,]+)\s+)?(於\S+進行|以\S+方式進行)\s+"
        + CUR_RE + r"\s+([\d.,]+)\s+" + CUR_RE + r"\s+([\d.,]+)\s+" + CUR_RE + r"\s+([\d.,]+)"
        r"(?:\s*\n\s*(\S+交易所))?"
    )
    for matched in re.finditer(trade_re, block, re.MULTILINE):
        groups = matched.groups()
        report["trades"].append({
            "date": _date(matched),
            "shares": _num(groups[3]),
            "method": groups[4],
            "venue": groups[11],
            "currency": groups[5],
            "price_high": _num(groups[6]),
            "price_low": _num(groups[8]),
            "amount": _num(groups[10]),
        })
    total_shares = re.search(r"合共購回股份總數\s+([\d,]+)", block)
    report["total_shares"] = _num(total_shares.group(1)) if total_shares else None
    total_amount = re.search(r"合共付出的價格總額\s*\(元\)\s+" + CUR_RE + r"\s+([\d.,]+)", block)
    if total_amount:
        report["currency"] = total_amount.group(1)
        report["total_amount"] = _num(total_amount.group(2))
    to_cancel = re.search(r"購回股份（擬註銷）數目[ \t]+([\d,]+)", block)
    report["to_cancel"] = _num(to_cancel.group(1)) if to_cancel else 0
    to_treasury = re.search(r"購回股份（擬持作庫存股份）[ \t]+([\d,]+)", block)
    report["to_treasury"] = _num(to_treasury.group(1)) if to_treasury else 0
    return report


def parse_ndr(text: str) -> dict:
    if "翌日披露報表" not in text[:400]:
        raise ValueError("正文开头没有「翌日披露報表」")
    out = {"form": "翌日披露報表"}
    company = re.search(r"公司名稱：\s*(\S+?)（", text)
    out["company"] = company.group(1) if company else None
    submitted = re.search(r"呈交日期：\s*" + DATE_RE, text)
    out["submitted"] = _date(submitted) if submitted else None

    chapter_one = _split(text, "第一章節", ["第一章節註釋", "第二章節"])
    out["classes"] = []
    for block in _class_blocks(chapter_one):
        share_class = _class_meta(block)
        issued = _split(block, "A. 已發行股份", ["B. 贖回", "確認"])
        redeemed = _split(block, "B. 贖回", ["確認"])
        share_class["opening"] = _balance(block, "於下列日期開始時的結存")
        share_class["closing"] = _balance(block, "於下列日期結束時的結存")
        share_class["changes"] = _events(issued)
        share_class["pending_cancel"] = _events(redeemed)
        out["classes"].append(share_class)

    chapter_two = _split(text, "購回報告", ["第二章節註釋", "在場内出售庫存股份報告", "第三章節"])
    out["buybacks"] = []
    for block in _class_blocks(chapter_two):
        report = _buyback_report(block)
        if report["trades"] or report["total_shares"]:
            meta = _class_meta(block)
            report.update({key: meta[key] for key in ("share_class", "stock_code")})
            out["buybacks"].append(report)

    resale = re.search(r"在場内出售庫存股份報告\s+(\S+)", text)
    if resale and resale.group(1) == "不適用":
        out["treasury_resale"] = None
    elif resale:
        out["treasury_resale"] = resale.group(1)
    else:
        out["treasury_resale"] = None
    if not out["classes"] and not out["buybacks"]:
        raise ValueError("第一、二章节都没解析出内容")
    out["digest"] = ndr_digest(out)
    return out


def _money(amount, currency) -> str:
    unit = CUR_CN.get(currency, currency or "")
    if amount is None:
        return ""
    if amount >= 1e8:
        return f"约{amount / 1e8:.2f}亿{unit}"
    if amount >= 1e4:
        return f"约{amount / 1e4:,.0f}万{unit}"
    return f"{amount:,.0f}{unit}"


def _month_day(iso: str) -> str:
    if not iso:
        return ""
    _year, month, day = iso.split("-")
    return f"{int(month)}月{int(day)}日"


def _venue_short(venue: str) -> str:
    names = {
        "深圳證券交易所": "深交所",
        "上海證券交易所": "上交所",
        "香港聯合交易所": "港交所",
    }
    return names.get(venue or "", venue or "")


def _class_label(share_class: str) -> str:
    if share_class in ("A", "H"):
        return f"{share_class}股"
    if share_class:
        return f"{share_class}类股份"
    return ""


def ndr_digest(parsed: dict) -> str:
    parts = []
    classes = {row.get("share_class"): row for row in parsed.get("classes", [])}
    for report in parsed.get("buybacks", []):
        share_class = report.get("share_class")
        trade_dates = sorted({trade["date"] for trade in report["trades"] if trade.get("date")})
        venue = next((_venue_short(trade["venue"]) for trade in report["trades"] if trade.get("venue")), "")
        when = "、".join(_month_day(day) for day in trade_dates)
        where = f"在{venue}" if venue else ""
        share = classes.get(share_class) or {}
        pct = next(
            (event.get("pct") for event in share.get("changes", []) if event.get("pct") and "購回" in event["event"]),
            None,
        )
        if pct is None:
            pct = next(
                (
                    event.get("pct")
                    for event in share.get("pending_cancel", [])
                    if event.get("date") in trade_dates and event.get("pct")
                ),
                None,
            )
        sentence = f"{_class_label(share_class)}回购：{when}{where}回购{report['total_shares']:,}股"
        if pct:
            sentence += f"（占已发行股份{pct:g}%）"
        highs = [trade["price_high"] for trade in report["trades"] if trade.get("price_high")]
        lows = [trade["price_low"] for trade in report["trades"] if trade.get("price_low")]
        currency = report.get("currency") or next((trade["currency"] for trade in report["trades"]), None)
        if highs and lows:
            sentence += f"，每股{min(lows):g}至{max(highs):g}{CUR_UNIT.get(currency, '')}"
        if report.get("total_amount"):
            sentence += f"，总额{_money(report['total_amount'], currency)}"
        if report.get("to_treasury") and not report.get("to_cancel"):
            sentence += "，全部持作库存股"
        elif report.get("to_cancel") and not report.get("to_treasury"):
            sentence += "，拟注销"
        elif report.get("to_cancel") and report.get("to_treasury"):
            sentence += f"，其中{report['to_cancel']:,}股拟注销、{report['to_treasury']:,}股持作库存股"
        pending = [event for event in share.get("pending_cancel", []) if event.get("shares")]
        if len(pending) > 1:
            total = sum(event["shares"] for event in pending)
            anchor = parsed.get("submitted") or trade_dates[-1]
            sentence += (
                f"；截至{_month_day(anchor)}已回购未注销累计{total:,}股"
                f"（{_month_day(pending[0].get('date'))}起{len(pending)}笔）"
            )
        closing = share.get("closing") or {}
        if report.get("to_treasury") and closing.get("treasury"):
            sentence += f"；库存股增至{closing['treasury']:,}股"
        parts.append(sentence)

    bought = {report.get("share_class") for report in parsed.get("buybacks", [])}
    for share in parsed.get("classes", []):
        for event in share.get("changes", []):
            if "購回" in event["event"] or (event["event"].startswith("其他") and share.get("share_class") in bought):
                continue
            sentence = f"{_class_label(share.get('share_class'))}{event['event']}"
            if event.get("note"):
                sentence += f"（{event['note']}）"
            if event.get("shares"):
                sentence += f"：{event['shares']:,}股"
                if event.get("pct"):
                    sentence += f"，占已发行股份{event['pct']:g}%"
                if event.get("price"):
                    sentence += f"，每股{event['price']:g}{CUR_UNIT.get(event.get('currency'), '')}"
            if event.get("date"):
                sentence += f"，{_month_day(event['date'])}"
            parts.append(sentence)
    if parsed.get("treasury_resale"):
        parts.append(f"场内出售库存股：{parsed['treasury_resale']}")
    return "；".join(parts) if parts else "翌日披露报表未列出股份变动或回购"


def self_check() -> None:
    """Lock the treasury, cancellation, and option digests."""
    treasury = parse_ndr(SAMPLE_TREASURY)
    buyback = treasury["buybacks"][0]
    if buyback["total_shares"] != 856980:
        raise AssertionError("treasury total_shares")
    if round(buyback["total_amount"], 2) != 794631549.33:
        raise AssertionError("treasury amount")
    if not (buyback["to_treasury"] == 856980 and not buyback["to_cancel"]):
        raise AssertionError("treasury to_treasury")
    if treasury["classes"][0]["closing"]["treasury"] != 5653063:
        raise AssertionError("treasury closing")
    digest = treasury["digest"]
    for needle in ("7.95亿元人民币", "持作库存股", "921.85至935", "0.077%"):
        if needle not in digest:
            raise AssertionError(needle)
    cancelled = parse_ndr(SAMPLE_CANCEL)
    pending = [event["shares"] for event in cancelled["classes"][0]["pending_cancel"]]
    if pending != [604293, 665060]:
        raise AssertionError("cancel pending")
    cancel_digest = cancelled["digest"]
    if "拟注销" not in cancel_digest or "累计1,269,353股" not in cancel_digest or "深交所" not in cancel_digest:
        raise AssertionError("cancel digest")
    if "其他" in cancel_digest:
        raise AssertionError("cancel digest keeps 其他")
    option = parse_ndr(SAMPLE_OPTION)
    if option["classes"][0]["changes"][0]["shares"] != 12000:
        raise AssertionError("option event")
    option_digest = option["digest"]
    if "H股根據股份期權計劃行使期權：12,000股" not in option_digest or "15.5港元" not in option_digest:
        raise AssertionError("option digest")
    try:
        parse_ndr("普通公告正文")
    except ValueError:
        pass
    else:
        raise AssertionError("reject non-ndr")
    excerpt = body_excerpt("本公司及董事会全体成员保证信息披露的内容真实、准确、完整。公司拟回购股份")
    if excerpt != "公司拟回购股份":
        raise AssertionError("excerpt strips boilerplate")
