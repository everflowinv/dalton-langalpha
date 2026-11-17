"""Offline parse of A-share next-day disclosure text.

The agent supplies the notice body. This module writes the same fields the
old enrich step wrote: body_excerpt, and for a next-day form, ndr and digest.
It does not fetch pages.
"""

from __future__ import annotations

from datetime import datetime
import re
import unicodedata

DATE_RE = r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
CUR_RE = r"(RMB|HKD|USD)"
NUM_RE = r"[\d,]+(?:\.\d+)?"
NUM_TOKEN_RE = r"[\d,.]+"
_INT_STRICT_RE = re.compile(r"^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)$")
_NUM_STRICT_RE = re.compile(r"^(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d+)?$")
_EVENT_TAIL_WITH_PCT_RE = re.compile(r"^(.*?)\s+(-?[\d,.\s-]*[%％](?:\s*.*)?)$")
_EVENT_MARKER_RE = re.compile(r"^[\s\ufeff\u200b\u200c\u200d\u2060]*\d+[)）]\.")
_EVENT_HEAD_RE = re.compile(r"^\s*\d+\)\.\s*(\S.*)$")
_PAGE_BREAK_RE = re.compile(r"^(?:第\s*\d+\s*[頁页]\s*共\s*\d+\s*[頁页](?:\s+v\d+(?:\.\d+)*)?|FF305)\s*$")
_CHANGE_DATE_EXACT_RE = re.compile(r"^\s*[變变][動动]日期\s+" + DATE_RE + r"\s*$")
_MISPLACED_NUM_TOKEN_RE = re.compile(r"^(?:-?[\d,.]+[A-Za-z0-9,.]*|RMB|HKD|USD|[%％])$")
_METHOD_RE = (
    r"(?:[於于](?:(?!(?:HKD|RMB|USD|\d+\)\.)).|\n){1,40}?[進进]行"
    r"|以(?:(?!(?:HKD|RMB|USD|\d+\)\.)).|\n){1,40}?方式[進进]行)"
)
BUYBACK_TRADE_RE = re.compile(
    r"^[^\S\n]*\d+\)\.[^\S\n]*"
    + DATE_RE
    + r"\s*(?:(" + NUM_TOKEN_RE + r")\s*)?"
    + r"(" + _METHOD_RE + r")\s*"
    + CUR_RE + r"\s*(" + NUM_TOKEN_RE + r")\s*"
    + CUR_RE + r"\s*(" + NUM_TOKEN_RE + r")\s*"
    + CUR_RE + r"\s*(" + NUM_TOKEN_RE + r")"
    + r"(?:\s*\n\s*(\S+交易所))?",
    re.MULTILINE,
)
TOTAL_SHARES_RE = re.compile(
    r"合\s*共\s*[購购]\s*回\s*股\s*份\s*[總总]\s*[數数]\s*(?!\d+\)\.)(" + NUM_TOKEN_RE + r")"
)
TOTAL_AMOUNT_RE = re.compile(
    r"合\s*共\s*付\s*出\s*的?\s*[價价]\s*格\s*[總总]\s*[額额]"
    r"(?:\s*[（(]\s*(?:元|港元|人民幣|人民币|美元|HKD|RMB|USD)\s*[）)])?\s*"
    + CUR_RE
    + r"\s*("
    + NUM_TOKEN_RE
    + r")"
)
TO_CANCEL_RE = re.compile(
    r"[購购]\s*回\s*股\s*份\s*[（(]\s*[擬拟]\s*[註注]\s*[銷销]\s*[）)]\s*"
    r"(?:[數数]\s*目)?[^\S\n]*(?:\n[^\S\n]*)?(?!\d+\)\.)(" + NUM_TOKEN_RE + r")",
    re.UNICODE,
)
TO_TREASURY_RE = re.compile(
    r"[購购]\s*回\s*股\s*份\s*[（(]\s*[擬拟]\s*持\s*作\s*[庫库]\s*存\s*股\s*份\s*[）)]\s*"
    r"(?:[數数]\s*目)?[^\S\n]*(?:\n[^\S\n]*)?(?!\d+\)\.)(" + NUM_TOKEN_RE + r")",
    re.UNICODE,
)
MANDATE_SHARES_RE = re.compile(
    r"[購购]\s*回\s*(?:股\s*份|[該该]\s*等\s*[證证]\s*券)\s*的\s*[數数]\s*目\s*\(a\)\s*"
    r"(?!\d+\)\.)(" + NUM_TOKEN_RE + r")"
)
MANDATE_PCT_RE = re.compile(r"百\s*分\s*比\s*(?!\d+\)\.)(" + NUM_TOKEN_RE + r")\s*%")
_MANDATE_PCT_LABEL_RE = re.compile(r"百\s*分\s*比")
_NEXT_ITEM_PREFIX_RE = re.compile(r"\d+[)）]\.")
_DISPOSITION_LABEL_RE = re.compile(
    r"[購购]\s*回\s*股\s*份\s*[（(]\s*[擬拟]\s*(?:[註注]\s*[銷销]|持\s*作\s*[庫库]\s*存\s*股\s*份)"
)
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


def _is_ws_or_cf(ch: str) -> bool:
    """True for any Unicode whitespace or format character (category Cf)."""
    return bool(ch) and (ch.isspace() or unicodedata.category(ch) == "Cf")


def _has_cf(text: str) -> bool:
    """True if ``text`` contains any Unicode format character (category Cf)."""
    return any(unicodedata.category(ch) == "Cf" for ch in text)


def _strict_num(
    raw: str | None,
    *,
    allow_decimal: bool = True,
    allow_negative: bool = False,
) -> int | float | None:
    """Parse a number only if its comma grouping, decimal structure, and boundaries are intact."""
    if raw is None:
        return None
    stripped = raw.strip(" ")
    if not stripped or any(_is_ws_or_cf(ch) for ch in stripped):
        return None
    if stripped.startswith("-"):
        if not allow_negative:
            return None
        body = stripped[1:]
        if not body or not body[0].isdigit():
            return None
    else:
        body = stripped
    pattern = _NUM_STRICT_RE if allow_decimal else _INT_STRICT_RE
    if not pattern.fullmatch(body):
        return None
    return _num(stripped)


def _has_broken_boundary(text: str, span: tuple[int, int]) -> bool:
    start, end = span
    if start > 0 and text[start - 1] in "0123456789,.":
        return True
    left = start
    while left > 0 and _is_ws_or_cf(text[left - 1]):
        left -= 1
    if left < start and left > 0:
        if any(unicodedata.category(ch) == "Cf" for ch in text[left:start]) and text[left - 1] in "0123456789,.":
            return True
        if text[left - 1] in "0123456789," or (text[left - 1] == "." and not (left >= 2 and text[left - 2] in ")）")):
            return True
    tail = text[end:]
    if not tail:
        return False
    if tail[0] in "0123456789,.":
        return True
    idx = 0
    while idx < len(tail) and _is_ws_or_cf(tail[idx]):
        idx += 1
    if idx == 0:
        return False
    ws_prefix = tail[:idx]
    rest = tail[idx:]
    if not rest:
        return False
    if rest[0] in ",.":
        return True
    if rest[0].isdigit():
        if any(unicodedata.category(ch) == "Cf" for ch in ws_prefix):
            return True
        if re.match(r"^(?:\d+[)）]\.|\d+\.\s*股份分[類类])", rest):
            return False
        return True
    return False


def _buyback_parse():
    import importlib.util
    import sys
    from pathlib import Path

    alias = "dalton_daily_filing_buyback_parse"
    target = Path(__file__).resolve().parent / "buyback_parse.py"
    existing = sys.modules.get(alias)
    if existing is not None and getattr(existing, "__file__", None) == str(target):
        return existing
    spec = importlib.util.spec_from_file_location(alias, target)
    if spec is None or spec.loader is None:
        raise RuntimeError("missing buyback_parse.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _date(found):
    matched = re.search(DATE_RE, found) if isinstance(found, str) else found
    if not matched:
        return None
    year, month, day = matched.groups()[:3]
    try:
        return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


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
    parts = re.split(r"\n\s*\d+\.\s*股份分[類类]", "\n" + section)
    return [part for part in parts[1:] if part.strip()]


def _numbered_event_lines(text: str) -> list[str]:
    """Return numbered event lines (`1).`, `2).`, ...) excluding mandate boilerplate."""
    return [
        ln.strip()
        for ln in (text or "").splitlines()
        if _EVENT_MARKER_RE.match(ln) and "購回授權" not in ln and "购回授权" not in ln
    ]


def _class_meta(block: str) -> dict:
    meta = {}
    share_class = re.search(r"股份[類类][別别]\s+(\S+)", block)
    meta["share_class"] = share_class.group(1) if share_class else None
    listed = re.search(r"[於于]香港[聯联]交所上市\s+(是|否)", block)
    meta["hk_listed"] = (listed.group(1) == "是") if listed else None
    code = re.search(r"[證证]券代[號号]\s*[（(]如上市[）)]\s+(\S+)", block)
    meta["stock_code"] = code.group(1) if code else None
    description = re.search(r"[說说]明\s+(\S+)", block)
    meta["description"] = description.group(1) if description else None
    return meta


def _balance(block: str, label: str) -> dict:
    lines = block.splitlines()
    label_re = re.compile(label)
    for idx, line in enumerate(lines):
        if not label_re.search(line):
            continue
        if _has_cf(line):
            raise ValueError(f"第一章節结存数字含异常格式字符: {line.strip()}")
        matched = re.search(label + r"[^\n]*?" + DATE_RE + r"([^\n]*)", line)
        if not matched:
            raise ValueError(f"第一章節结存缺少日期或跨行: {line.strip()}")
        tail = matched.group(4)
        numbers = []
        for token in tail.strip().split():
            val = _strict_num(token, allow_decimal=False, allow_negative=True)
            if val is None:
                raise ValueError(f"第一章節结存数字格式异常: {token}")
            numbers.append(val)
        if len(numbers) not in (2, 3):
            raise ValueError(f"第一章節结存数字数量异常: {tail.strip()}")
        for nxt in lines[idx + 1 :]:
            stripped = nxt.strip()
            if not stripped or _PAGE_BREAK_RE.match(stripped):
                continue
            if re.match(r"^\d+\)\.\s*", stripped) or re.search(
                r"於下列日期|于下列日期|^\s*[A-C]\.\s|^\s*[確确][認认]\s*$", stripped
            ):
                break
            raise ValueError(f"第一章節结存数字跨行或格式异常: {stripped}")
        parsed_date = _date(matched)
        if not parsed_date:
            raise ValueError(f"第一章節结存日期无效: {line.strip()}")
        out = {"date": parsed_date}
        if len(numbers) >= 3:
            out.update(issued_ex_treasury=numbers[0], treasury=numbers[1], total=numbers[2])
        else:
            out.update(issued_ex_treasury=numbers[0], total=numbers[1])
        return out
    return {}


def _parse_event_rest(rest: str) -> dict | None:
    if not rest or not rest.strip():
        return None
    if _has_cf(rest):
        raise ValueError(f"第一章節数字含异常格式字符: {rest.strip()}")
    pct_matches = list(re.finditer(r"[%％]", rest))
    if not pct_matches:
        if re.search(r"[\d]|RMB|HKD|USD", rest):
            raise ValueError(f"第一章節数字格式不完整或异常: {rest.strip()}")
        return None
    if len(pct_matches) > 1:
        raise ValueError(f"第一章節数字格式不完整或异常: {rest.strip()}")
    pct_pos = pct_matches[0].start()
    left_pct = rest[:pct_pos].strip()
    right_pct = rest[pct_pos + 1 :].strip()
    if not left_pct and not right_pct:
        return None
    left_tokens = left_pct.split()
    if len(left_tokens) != 2:
        raise ValueError(f"第一章節股数或比例格式异常: {rest.strip()}")
    shares = _strict_num(left_tokens[0], allow_decimal=False, allow_negative=True)
    pct = _strict_num(left_tokens[1], allow_decimal=True, allow_negative=False)
    if shares is None or pct is None:
        raise ValueError(f"第一章節股数或比例格式异常: {rest.strip()}")

    parsed: dict = {"shares": shares, "pct": pct}
    right_tokens = right_pct.split()
    if not right_tokens:
        return parsed
    if len(right_tokens) == 1:
        t_chg = _strict_num(right_tokens[0], allow_decimal=False, allow_negative=True)
        if t_chg is None:
            raise ValueError(f"第一章節库存股变动或价格格式异常: {rest.strip()}")
        parsed["treasury_change"] = t_chg
        return parsed
    if len(right_tokens) == 2:
        cur, price_tok = right_tokens
        price = _strict_num(price_tok, allow_decimal=True, allow_negative=False)
        if cur not in ("RMB", "HKD", "USD") or price is None:
            raise ValueError(f"第一章節价格格式异常: {rest.strip()}")
        parsed["currency"] = cur
        parsed["price"] = price
        return parsed
    if len(right_tokens) == 3:
        t_tok, cur, price_tok = right_tokens
        t_chg = _strict_num(t_tok, allow_decimal=False, allow_negative=True)
        price = _strict_num(price_tok, allow_decimal=True, allow_negative=False)
        if t_chg is None or cur not in ("RMB", "HKD", "USD") or price is None:
            raise ValueError(f"第一章節价格或库存股变动格式异常: {rest.strip()}")
        parsed["treasury_change"] = t_chg
        parsed["currency"] = cur
        parsed["price"] = price
        return parsed
    raise ValueError(f"第一章節数字格式不完整或异常: {rest.strip()}")


def _events(part: str) -> list:
    """Count numbered events first (including cross-line markers), then strictly parse every one."""
    events = []
    lines = part.splitlines()
    marker_indices = [
        idx
        for idx, ln in enumerate(lines)
        if _EVENT_MARKER_RE.match(ln) and "購回授權" not in ln and "购回授权" not in ln
    ]
    expected_count = len(marker_indices)
    for index in marker_indices:
        line = lines[index]
        if _has_cf(line):
            raise ValueError(f"第一章節数字含异常格式字符: {line.strip()}")
        head_match = _EVENT_HEAD_RE.match(line)
        if not head_match:
            raise ValueError(f"第一章節事件编号与内容跨行或格式异常: {line.strip()}")
        body = head_match.group(1)
        tail_match = _EVENT_TAIL_WITH_PCT_RE.match(body)
        if tail_match:
            name, rest = tail_match.group(1).strip(), tail_match.group(2)
        else:
            col_match = re.match(r"^(\S.*?)\s{2,}(.*)$", body)
            if col_match:
                name, rest = col_match.group(1).strip(), col_match.group(2)
            else:
                name, rest = body.strip(), ""
                if re.search(r"(?:^|\s)-?[\d,.]+(?:\s|$)|(?:RMB|HKD|USD|[%％])", body):
                    raise ValueError(f"第一章節数字格式不完整或异常: {line.strip()}")
        if any(_MISPLACED_NUM_TOKEN_RE.match(tok) for tok in name.split()):
            raise ValueError(f"第一章節事件行数字格式异常: {line.strip()}")
        event = {"event": name}
        numbers = _parse_event_rest(rest)
        if numbers:
            event.update(numbers)
        extra = []
        for nxt in lines[index + 1 :]:
            stripped = nxt.strip()
            if _EVENT_MARKER_RE.match(nxt) or re.search(
                r"於下列日期|于下列日期|^\s*[A-C]\.\s|^\s*[確确][認认]\s*$|第一章節註釋|第一章节注释|第二章節|第二章节",
                nxt,
            ):
                break
            if not stripped or _PAGE_BREAK_RE.match(stripped):
                continue
            if _has_cf(nxt):
                raise ValueError(f"第一章節数字含异常格式字符: {stripped}")
            change_date = _CHANGE_DATE_EXACT_RE.match(nxt)
            if change_date:
                if "date" in event:
                    raise ValueError(f"第一章節變動日期重复: {stripped}")
                parsed_date = _date(change_date)
                if not parsed_date:
                    raise ValueError(f"第一章節變動日期无效: {stripped}")
                event["date"] = parsed_date
                continue
            if "變動日期" in nxt or "变动日期" in nxt:
                raise ValueError(f"第一章節變動日期格式异常: {stripped}")
            if "date" in event:
                raise ValueError(f"第一章節變動日期后存在跨行内容: {stripped}")
            if numbers is not None:
                raise ValueError(f"第一章節数字事件存在跨行内容: {stripped}")
            if re.search(r"[\d%％]|RMB|HKD|USD", stripped) or re.match(r"^[.,+\-]", stripped):
                raise ValueError(f"第一章節事件数字跨行或格式异常: {stripped}")
            extra.append(stripped)
        if extra and not numbers:
            event["note"] = "".join(extra)
        events.append(event)
    if len(events) != expected_count:
        raise ValueError(f"第一章節事件解析不完整: 预期 {expected_count} 条，实得 {len(events)} 条")
    return events


def _find_mandate_pct(norm: str) -> tuple[str, tuple[int, int]] | None:
    """Locate mandate cumulative percentage token, preserving internal whitespace/Cf so strict check flags it."""
    for label_m in _MANDATE_PCT_LABEL_RE.finditer(norm):
        pos = label_m.end()
        while pos < len(norm) and _is_ws_or_cf(norm[pos]):
            pos += 1
        if pos >= len(norm) or _NEXT_ITEM_PREFIX_RE.match(norm[pos:]):
            continue
        if norm[pos] not in "0123456789,.":
            continue
        end = pos
        while end < len(norm) and (norm[end] in "0123456789,." or _is_ws_or_cf(norm[end])):
            end += 1
        if end < len(norm) and norm[end] in ("%", "％"):
            trim_end = end
            while trim_end > pos and norm[trim_end - 1] == " ":
                trim_end -= 1
            return norm[pos:trim_end], (pos, trim_end)
    return None


def _sec2_trade_span(norm: str) -> tuple[int, int]:
    """Return the character span of Section II A buyback trade rows (excluding Section II B mandate items)."""
    start = 0
    sec_a = re.search(r"A\.\s*[購购]回[報报]告", norm)
    if sec_a:
        start = sec_a.end()
    end = len(norm)
    for pat in (
        r"合\s*共\s*[購购]\s*回\s*股\s*份|合\s*共\s*付\s*出",
        r"^\s*B\.",
        r"第二章節註釋|第二章节注释|在場内出售|在场内出售",
    ):
        matched = re.search(pat, norm[start:], re.MULTILINE)
        if matched and start + matched.start() < end:
            end = start + matched.start()
    return start, end


def _extract_buyback_block(block: str) -> dict:
    """Parse one FF305 Section II buyback block, keeping raw numbers for exact reporting."""
    norm = (block or "").replace("\r\n", "\n").replace("\r", "\n")
    number_errors: list[dict[str, str]] = []

    def _checked(
        field: str,
        raw_val: str | None,
        *,
        allow_decimal: bool = True,
        span: tuple[int, int] | None = None,
    ) -> int | float | None:
        if raw_val is None:
            return None
        val = _strict_num(raw_val, allow_decimal=allow_decimal)
        if val is None or (span is not None and _has_broken_boundary(norm, span)):
            number_errors.append({"field": field, "raw": raw_val.strip()})
            return None
        return val

    region_start, region_end = _sec2_trade_span(norm)
    trade_region = norm[region_start:region_end]
    expected_trade_lines = [
        (idx, ln.strip())
        for idx, ln in enumerate(trade_region.splitlines())
        if _EVENT_MARKER_RE.match(ln)
    ]

    trades = []
    matched_line_indices: set[int] = set()
    for matched in BUYBACK_TRADE_RE.finditer(trade_region):
        matched_line_indices.add(trade_region[: matched.start()].count("\n"))
        groups = matched.groups()
        method = re.sub(r"\s+", "", groups[4] or "")
        shares_raw = groups[3].strip() if groups[3] else None
        hi_raw = groups[6].strip()
        lo_raw = groups[8].strip()
        amt_raw = groups[10].strip()

        def _tspan(grp_idx: int, _m: re.Match[str] = matched) -> tuple[int, int]:
            s, e = _m.span(grp_idx)
            return region_start + s, region_start + e

        trades.append({
            "date": _date(matched),
            "shares": _checked(
                "trade_shares",
                shares_raw,
                allow_decimal=False,
                span=_tspan(4) if shares_raw is not None else None,
            ),
            "shares_raw": shares_raw,
            "method": method,
            "venue": groups[11],
            "currency": groups[5],
            "price_high": _checked("price_high", hi_raw, allow_decimal=True, span=_tspan(7)),
            "price_high_raw": hi_raw,
            "price_low": _checked("price_low", lo_raw, allow_decimal=True, span=_tspan(9)),
            "price_low_raw": lo_raw,
            "amount": _checked("trade_amount", amt_raw, allow_decimal=True, span=_tspan(11)),
            "amount_raw": amt_raw,
        })

    if len(trades) != len(expected_trade_lines):
        unmatched = next(
            (ln for idx, ln in expected_trade_lines if idx not in matched_line_indices),
            expected_trade_lines[0][1] if expected_trade_lines else f"预期 {len(expected_trade_lines)} 笔，实得 {len(trades)} 笔",
        )
        number_errors.append({"field": "trades", "raw": unmatched})

    shares_matches = []
    for m in TOTAL_SHARES_RE.finditer(norm):
        raw_sh = m.group(1).strip()
        val_sh = _checked("total_shares", raw_sh, allow_decimal=False, span=m.span(1))
        shares_matches.append({"value": val_sh, "raw": raw_sh})

    amount_matches = []
    for m in TOTAL_AMOUNT_RE.finditer(norm):
        raw_amt = m.group(2).strip()
        val_amt = _checked("total_amount", raw_amt, allow_decimal=True, span=m.span(2))
        amount_matches.append({"currency": m.group(1), "value": val_amt, "raw": raw_amt})

    to_cancel_m = TO_CANCEL_RE.search(norm)
    to_treasury_m = TO_TREASURY_RE.search(norm)
    cum_shares_m = MANDATE_SHARES_RE.search(norm)
    cum_pct_found = _find_mandate_pct(norm)

    to_cancel_raw = to_cancel_m.group(1).strip() if to_cancel_m else None
    to_treasury_raw = to_treasury_m.group(1).strip() if to_treasury_m else None
    cum_shares_raw = cum_shares_m.group(1).strip() if cum_shares_m else None
    cum_pct_raw = cum_pct_found[0].strip(" ") if cum_pct_found else None

    to_cancel_val = (
        _checked("to_cancel", to_cancel_raw, allow_decimal=False, span=to_cancel_m.span(1))
        if to_cancel_m
        else 0
    )
    to_treasury_val = (
        _checked("to_treasury", to_treasury_raw, allow_decimal=False, span=to_treasury_m.span(1))
        if to_treasury_m
        else 0
    )
    cum_shares_val = (
        _checked("mandate_cum_shares", cum_shares_raw, allow_decimal=False, span=cum_shares_m.span(1))
        if cum_shares_m
        else None
    )
    cum_pct_val = (
        _checked("mandate_cum_pct", cum_pct_raw, allow_decimal=True, span=cum_pct_found[1])
        if cum_pct_found
        else None
    )

    report: dict = {
        "trades": trades,
        "total_shares": shares_matches[0]["value"] if shares_matches else None,
        "total_shares_raw": shares_matches[0]["raw"] if shares_matches else None,
        "total_shares_matches": shares_matches,
        "total_amount_matches": amount_matches,
        "to_cancel": to_cancel_val if to_cancel_val is not None else 0,
        "to_cancel_raw": to_cancel_raw,
        "to_cancel_matched": bool(to_cancel_m),
        "to_treasury": to_treasury_val if to_treasury_val is not None else 0,
        "to_treasury_raw": to_treasury_raw,
        "to_treasury_matched": bool(to_treasury_m),
        "has_disposition_section": bool(_DISPOSITION_LABEL_RE.search(norm)),
        "mandate_cum_shares": cum_shares_val,
        "mandate_cum_shares_raw": cum_shares_raw,
        "mandate_cum_pct": cum_pct_val,
        "mandate_cum_pct_raw": cum_pct_raw,
        "number_errors": number_errors,
    }
    if amount_matches and amount_matches[0]["value"] is not None:
        report["currency"] = amount_matches[0]["currency"]
        report["total_amount"] = amount_matches[0]["value"]
        report["total_amount_raw"] = amount_matches[0]["raw"]
    return report


def _split_section_two(text: str) -> str:
    for marker in ("第二章節", "第二章节"):
        if marker in text:
            return _split(
                text,
                marker,
                ["第二章節註釋", "第二章节注释", "在場内出售庫存股份報告", "在场内出售库存股份报告", "第三章節", "第三章节"],
            )
    sec_a = re.search(r"A\.\s*[購购]回[報报]告", text)
    if sec_a:
        return _split(
            text,
            sec_a.group(0),
            ["第二章節註釋", "第二章节注释", "在場内出售庫存股份報告", "在场内出售库存股份报告", "第三章節", "第三章节"],
        )
    if "第一章節" in text or "第一章节" in text:
        return ""
    return text


def parse_section_two_buybacks(text: str) -> list[dict]:
    """Parse Section II buyback blocks (both with and without share-class headers)."""
    sec2 = _split_section_two(text or "")
    ch1_meta: dict = {}
    for ch1_marker in ("第一章節", "第一章节"):
        if ch1_marker in (text or ""):
            ch1 = _split(text, ch1_marker, ["第一章節註釋", "第一章节注释", "第二章節", "第二章节"])
            ch1_blocks = _class_blocks(ch1)
            if len(ch1_blocks) == 1:
                ch1_meta = _class_meta(ch1_blocks[0])
            break
    parts = re.split(r"\n\s*\d+\.\s*股份分[類类]", "\n" + sec2)
    blocks = [part for part in parts[1:] if part.strip()]
    if blocks:
        prefix = parts[0]
        prefix_before_b = re.split(r"(?:^|\n)\s*B\.", prefix, maxsplit=1)[0]
        prefix_has_a = bool(
            re.search(r"(?:^|\n)\s*A\.", prefix)
            and not re.search(r"(?:^|\n)\s*A\.(?:[^\n]*|\s*[購购]回[報报]告\s*)不[適适]用", prefix)
        )
        prefix_has_content = bool(_numbered_event_lines(prefix_before_b) or prefix_has_a or TOTAL_SHARES_RE.search(prefix))
        items = ([(prefix, ch1_meta)] if prefix_has_content else []) + [(block, _class_meta(block)) for block in blocks]
    else:
        items = [(sec2, ch1_meta)]
    out = []
    for block, meta in items:
        report = _extract_buyback_block(block)
        if (
            report["trades"]
            or report["total_shares"] is not None
            or report.get("total_amount_matches")
            or report.get("number_errors")
        ):
            report["share_class"] = meta.get("share_class")
            report["stock_code"] = meta.get("stock_code")
            report["description"] = meta.get("description")
            out.append(report)
        elif blocks and block is parts[0]:
            report["number_errors"] = [{"field": "trades", "raw": "第二章節小节截取失败"}]
            report["share_class"] = meta.get("share_class")
            report["stock_code"] = meta.get("stock_code")
            report["description"] = meta.get("description")
            out.append(report)
    return out


def parse_ndr(text: str) -> dict:
    if "翌日披露報表" not in text[:400]:
        raise ValueError("正文开头没有「翌日披露報表」")
    out = {"form": "翌日披露報表"}
    company = re.search(r"公司名稱：\s*(\S+?)（", text)
    out["company"] = company.group(1) if company else None
    submitted = re.search(r"呈交日期：\s*" + DATE_RE, text)
    out["submitted"] = _date(submitted) if submitted else None

    chapter_one = ""
    has_ch1_marker = False
    for ch1_marker in ("第一章節", "第一章节"):
        if ch1_marker in text:
            has_ch1_marker = True
            chapter_one = _split(text, ch1_marker, ["第一章節註釋", "第一章节注释", "第二章節", "第二章节"])
            break
    ch1_parts = re.split(r"\n\s*\d+\.\s*股份分[類类]", "\n" + chapter_one)
    ch1_prefix = ch1_parts[0]
    ch1_blocks = [part for part in ch1_parts[1:] if part.strip()]
    if has_ch1_marker:
        has_ch1_heading = bool(re.search(r"(?:^|\n)\s*第一章[節节]\s*(?:\n|$)", text))
        if not ch1_blocks and (
            _numbered_event_lines(chapter_one)
            or has_ch1_heading
            or re.search(r"(?:^|\n)\s*[AB]\.|股份分[類类]|於下列日期|于下列日期", chapter_one)
        ):
            raise ValueError("第一章節小节截取失败")
        if ch1_blocks and (
            _numbered_event_lines(ch1_prefix)
            or re.search(r"(?:^|\n)\s*[AB]\.", ch1_prefix)
        ):
            raise ValueError("第一章節小节截取失败")
    out["classes"] = []
    for block in ch1_blocks:
        share_class = _class_meta(block)
        issued = _split(block, "A. 已發行股份", ["B. 贖回", "B. 赎回", "確認", "确认"])
        if not issued:
            issued = _split(block, "A. 已发行股份", ["B. 赎回", "B. 赎回", "確認", "确认"])
        redeemed = _split(block, "B. 贖回", ["確認", "确认"])
        if not redeemed:
            redeemed = _split(block, "B. 赎回", ["確認", "确认"])
        if (
            re.search(r"(?:^|\n)\s*A\.", block)
            and not re.search(r"(?:^|\n)\s*A\.[^\n]*不[適适]用", block)
            and not issued.strip()
        ):
            raise ValueError("第一章節 A 小节截取失败")
        if (
            re.search(r"(?:^|\n)\s*B\.", block)
            and not re.search(r"(?:^|\n)\s*B\.[^\n]*不[適适]用", block)
            and not redeemed.strip()
        ):
            raise ValueError("第一章節 B 小节截取失败")
        share_class["opening"] = _balance(block, "於下列日期開始時的結存") or _balance(block, "于下列日期开始时的结存")
        share_class["closing"] = _balance(block, "於下列日期結束時的結存") or _balance(block, "于下列日期结束时的结存")
        share_class["changes"] = _events(issued)
        share_class["pending_cancel"] = _events(redeemed)
        block_events = _numbered_event_lines(block)
        parsed_count = len(share_class["changes"]) + len(share_class["pending_cancel"])
        if block_events and ((not issued.strip() and not redeemed.strip()) or parsed_count != len(block_events)):
            raise ValueError(f"第一章節事件截取不完整: 原文 {len(block_events)} 条，实得 {parsed_count} 条")
        out["classes"].append(share_class)

    sec2_reports = parse_section_two_buybacks(text)
    if sec2_reports:
        validated = _buyback_parse().parse_for_ndr(text)
        if "error" in validated:
            raise ValueError(validated["error"])
        out["buybacks"] = validated["groups"]
    else:
        sec2_text = _split_section_two(text)
        has_ch2_heading = bool(re.search(r"(?:^|\n)\s*第二章[節节]\s*(?:\n|$)", text))
        sec2_body = re.sub(r"^\s*第二章[節节]\s*", "", sec2_text).strip()
        sec2_before_b = re.split(r"(?:^|\n)\s*B\.", sec2_text, maxsplit=1)[0]
        sec2_has_a = bool(re.search(r"(?:^|\n)\s*A\.", sec2_text))
        sec2_a_na = bool(re.search(r"(?:^|\n)\s*A\.(?:[^\n]*|\s*[購购]回[報报]告\s*)不[適适]用", sec2_text))
        if ("第二章節" in text or "第二章节" in text) and (
            (has_ch2_heading and (not sec2_body or (not re.fullmatch(r"不[適适]用", sec2_body) and not sec2_a_na and (sec2_has_a or bool(_numbered_event_lines(sec2_before_b))))))
            or (sec2_has_a and not sec2_a_na)
            or bool(_numbered_event_lines(sec2_before_b))
        ):
            raise ValueError("未解析到第二章節购回明细")
        out["buybacks"] = []

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
        shares_raw = report.get("total_shares_raw") or f"{report['total_shares']:,}"
        sentence = f"{_class_label(share_class)}回购：{when}{where}回购{shares_raw}股"
        if pct:
            sentence += f"（占已发行股份{pct:g}%）"
        highs = [
            trade.get("price_high") or trade.get("hi")
            for trade in report["trades"]
            if trade.get("price_high") or trade.get("hi")
        ]
        lows = [
            trade.get("price_low") or trade.get("lo")
            for trade in report["trades"]
            if trade.get("price_low") or trade.get("lo")
        ]
        currency = report.get("currency") or next((trade["currency"] for trade in report["trades"]), None)
        if highs and lows:
            sentence += f"，每股{min(lows):g}至{max(highs):g}{CUR_UNIT.get(currency, '')}"
        amt_raw = report.get("total_amount_raw")
        if amt_raw:
            sentence += f"，总额{amt_raw}{CUR_CN.get(currency, currency or '')}"
        elif report.get("total_amount"):
            sentence += f"，总额{report['total_amount']:,}{CUR_CN.get(currency, currency or '')}"
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
    for needle in ("794,631,549.33元人民币", "持作库存股", "921.85至935", "0.077%"):
        if needle not in digest:
            raise AssertionError(needle)
    if "7.95亿" in digest:
        raise AssertionError("digest must use verbatim total_amount_raw")
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
    try:
        parse_ndr(SAMPLE_TREASURY.replace("合共付出的價格總額 (元) RMB 794,631,549.33\n", ""))
    except ValueError:
        pass
    else:
        raise AssertionError("reject missing total amount in ndr")
    for sep in ("\n", "\u00a0", "\u202f", "\u3000", "\f", "\u2060", "\u200b", "\u200c", "\u200d", "\ufeff"):
        try:
            parse_ndr(SAMPLE_TREASURY.replace("794,631,549.33", f"794{sep}631549.33"))
        except ValueError:
            pass
        else:
            raise AssertionError(f"reject broken number in ndr ({sep!r})")
    for bad_sec1 in (
        SAMPLE_OPTION.replace("15.5", "15. 5"),
        SAMPLE_OPTION.replace("15.5", "15.\u20605"),
        SAMPLE_OPTION.replace("15.5", "1\u200b5.5"),
        SAMPLE_OPTION.replace("12,000", "12, 000"),
        SAMPLE_OPTION.replace("1.2 %", "1.\u20602 %"),
        SAMPLE_OPTION.replace(
            "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
            "1). 根據股份期權計劃行使期權\n12,000  1.2 %  HKD  15. 5",
        ),
        SAMPLE_OPTION.replace(
            "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
            "1). 根據股份期權計劃行使期權\n12,000  1.2 %  HKD  15.5",
        ),
        SAMPLE_OPTION.replace(
            "HKD  15.5\n變動日期 2026年9月22日",
            "HKD  15\n變動日期 2026年9月22日\n.5",
        ),
        SAMPLE_OPTION.replace(
            "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
            "1).\n根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
        ),
        SAMPLE_TREASURY.replace(
            "1). 購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927.2463",
            "1).\n購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927. 2463",
        ),
    ):
        try:
            parse_ndr(bad_sec1)
        except ValueError:
            pass
        else:
            raise AssertionError("reject broken Section I number in ndr")
    excerpt = body_excerpt("本公司及董事会全体成员保证信息披露的内容真实、准确、完整。公司拟回购股份")
    if excerpt != "公司拟回购股份":
        raise AssertionError("excerpt strips boilerplate")
