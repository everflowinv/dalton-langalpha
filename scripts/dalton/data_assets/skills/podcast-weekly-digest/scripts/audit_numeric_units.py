#!/usr/bin/env python3
"""金额与量级审计：抽取源稿与成稿中的显式金额，按币种比对量级。

用途：拦截 "78 billion" 被写成 "78 亿美元" 这类量级错误，以及成稿中出现
源稿没有的金额。这是护栏，不是语义校验器——它不知道两个相同数值是否指同
一个指标，也不检查百分比、日期和数量。

支持币种：USD / HKD / CNY。跨币种不做换算，也不做汇率折算；只在同一币种
内比对。`¥` / `￥` 默认按人民币处理，日元稿件必须人工复核。

用法：
    python3 .agents/skills/dalton-transcript-polish/scripts/audit_numeric_units.py <源文件> [<成稿文件>] [--json]
    python3 .agents/skills/dalton-transcript-polish/scripts/audit_numeric_units.py self-test [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "2.0.0"

ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}

TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

EN_SCALE = {
    "thousand": Decimal("1000"),
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "trillion": Decimal("1000000000000"),
}

ZH_SCALE = {
    "": Decimal("1"),
    "千": Decimal("1000"),
    "万": Decimal("10000"),
    "百万": Decimal("1000000"),
    "千万": Decimal("10000000"),
    "亿": Decimal("100000000"),
    "百亿": Decimal("10000000000"),
    "千亿": Decimal("100000000000"),
    "万亿": Decimal("1000000000000"),
}

CURRENCIES = ("USD", "HKD", "CNY")

# 顺序敏感：长写法必须排在短写法前面（HK$ 先于 $，港元 先于 元）。
SYMBOL_TO_CURRENCY = [
    ("US$", "USD"), ("HK$", "HKD"), ("USD", "USD"), ("HKD", "HKD"),
    ("RMB", "CNY"), ("CNY", "CNY"), ("¥", "CNY"), ("￥", "CNY"), ("$", "USD"),
]
SYMBOL_PATTERN = "|".join(re.escape(sym) for sym, _ in SYMBOL_TO_CURRENCY)

EN_WORD_TO_CURRENCY = [
    ("hong kong dollars", "HKD"), ("hong kong dollar", "HKD"),
    ("us dollars", "USD"), ("us dollar", "USD"),
    ("dollars", "USD"), ("dollar", "USD"),
    ("renminbi", "CNY"), ("yuan", "CNY"),
]
EN_WORD_PATTERN = (
    r"US\s+dollars?|Hong\s+Kong\s+dollars?|dollars?|yuan|renminbi|RMB|CNY|USD|HKD"
)

ZH_WORD_TO_CURRENCY = [
    ("美元", "USD"), ("美金", "USD"),
    ("港元", "HKD"), ("港币", "HKD"),
    ("人民币", "CNY"), ("元", "CNY"), ("块", "CNY"),
]
ZH_WORD_PATTERN = "美元|美金|港元|港币|人民币|元|块"

# 这些后缀说明数字不是金额（token 数、人数、参数量等）。
NON_MONEY_TAIL = re.compile(
    r"\s+(?:\w+\s+){0,2}(?:tokens?|parameters?|people|persons?|professionals?"
    r"|employees?|staff|customers?|users?|units?|shares?)\b"
)
ZH_NON_MONEY = r"条|个|家|人|名|位|次|份|页|股|台|辆|token|tokens|参数|员工|客户|用户"

NUMBER_WORDS = sorted(set(ONES) | set(TENS) | {"hundred", "and"}, key=len, reverse=True)
NUMBER_WORD_PATTERN = (
    "(?:" + "|".join(NUMBER_WORDS) + r")(?:[ -]+(?:" + "|".join(NUMBER_WORDS) + r"))*"
)


@dataclass(frozen=True)
class Amount:
    raw: str
    values: tuple[Decimal, ...]
    line: int
    currency: str
    inferred: bool = False

    def as_dict(self) -> dict:
        return {
            "line": self.line,
            "currency": self.currency,
            "values": [str(int(v)) if v == v.to_integral_value() else str(v) for v in self.values],
            "raw": self.raw.strip(),
            "currency_inferred": self.inferred,
        }


@dataclass
class Warning_:
    line: int
    currency: str
    value: Decimal
    raw: str
    hint: str
    kind: str = "no_equivalent"

    def as_dict(self) -> dict:
        return {
            "line": self.line,
            "currency": self.currency,
            "value": fmt(self.value),
            "raw": self.raw.strip(),
            "kind": self.kind,
            "hint": self.hint,
        }


@dataclass
class AuditResult:
    source: list[Amount] = field(default_factory=list)
    target: list[Amount] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)


def parse_decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ""))


def parse_number_words(phrase: str) -> Decimal:
    tokens = phrase.lower().replace("-", " ").split()
    current = 0
    for token in tokens:
        if token == "and":
            continue
        if token in ONES:
            current += ONES[token]
        elif token in TENS:
            current += TENS[token]
        elif token == "hundred":
            current = max(current, 1) * 100
        else:
            raise ValueError(f"unsupported number word: {token}")
    return Decimal(current)


def currency_from_symbol(token: str | None) -> str | None:
    if not token:
        return None
    normalized = token.strip().upper()
    for symbol, code in SYMBOL_TO_CURRENCY:
        if normalized == symbol.upper():
            return code
    return None


def currency_from_en_word(token: str | None) -> str | None:
    if not token:
        return None
    normalized = re.sub(r"\s+", " ", token.strip().lower())
    for word, code in EN_WORD_TO_CURRENCY:
        if normalized == word:
            return code
    upper = normalized.upper()
    if upper in CURRENCIES:
        return upper
    if upper == "RMB":
        return "CNY"
    return None


def currency_from_zh_word(token: str | None) -> str | None:
    if not token:
        return None
    for word, code in ZH_WORD_TO_CURRENCY:
        if token.strip() == word:
            return code
    return None


def line_currencies(line: str) -> set[str]:
    """该行出现过哪些币种标记。用于给缺币种的数字定位上下文。"""
    found: set[str] = set()
    for symbol, code in SYMBOL_TO_CURRENCY:
        if symbol in line:
            found.add(code)
    for word, code in ZH_WORD_TO_CURRENCY:
        if word in ("元", "块"):
            continue  # 「亿美元」结尾也是「元」，单独判会误判币种
        if word in line:
            found.add(code)
    if re.search(r"\d\s*(?:万亿|千亿|百亿|亿|千万|百万|万|千)?\s*元(?!\w)", line):
        found.add("CNY")
    for match in re.finditer(EN_WORD_PATTERN, line, re.IGNORECASE):
        code = currency_from_en_word(match.group(0))
        if code:
            found.add(code)
    return found


def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(span[0] < other[1] and other[0] < span[1] for other in spans)


def line_span(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    return text[start:] if end == -1 else text[start:end]


def extract_amounts(text: str, default_currency: str = "USD") -> list[Amount]:
    results: list[Amount] = []
    used: list[tuple[int, int]] = []

    def resolve(explicit: str | None, position: int) -> tuple[str | None, bool]:
        """返回 (币种, 是否推断)。行内只出现一种币种时可以推断，否则放弃。"""
        if explicit:
            return explicit, False
        context = line_currencies(line_span(text, position))
        if len(context) == 1:
            return next(iter(context)), True
        if not context:
            return default_currency, True
        return None, True

    en_digit = re.compile(
        rf"(?<![\w.])(?P<pre>{SYMBOL_PATTERN})?\s*"
        r"(?P<a>\d[\d,]*(?:\.\d+)?)"
        r"(?:\s*(?:to|or|[-–—])\s*(?P<b>\d[\d,]*(?:\.\d+)?))?\s*"
        r"(?P<unit>thousand|million|billion|trillion)"
        rf"(?:\s+(?P<post>{EN_WORD_PATTERN}))?",
        re.IGNORECASE,
    )
    en_words = re.compile(
        rf"(?<![\w-])(?P<pre>{SYMBOL_PATTERN})?\s*(?P<a>{NUMBER_WORD_PATTERN})"
        rf"(?:\s+(?:to|or)\s+(?P<b>{NUMBER_WORD_PATTERN}))?\s+"
        r"(?P<unit>thousand|million|billion|trillion)"
        rf"(?:\s+(?P<post>{EN_WORD_PATTERN}))?",
        re.IGNORECASE,
    )

    for pattern, word_mode in ((en_digit, False), (en_words, True)):
        for match in pattern.finditer(text):
            if overlaps(match.span(), used):
                continue
            tail = text[match.end() : match.end() + 40]
            if NON_MONEY_TAIL.match(tail):
                continue
            explicit = currency_from_symbol(match.group("pre")) or currency_from_en_word(
                match.group("post")
            )
            unit_name = match.group("unit").lower()
            if unit_name in {"thousand", "trillion"} and not explicit:
                continue
            currency, inferred = resolve(explicit, match.start())
            if currency is None:
                continue
            scale = EN_SCALE[unit_name]
            try:
                if word_mode:
                    values = [parse_number_words(match.group("a")) * scale]
                    if match.group("b"):
                        values.append(parse_number_words(match.group("b")) * scale)
                else:
                    values = [parse_decimal(match.group("a")) * scale]
                    if match.group("b"):
                        values.append(parse_decimal(match.group("b")) * scale)
            except (ValueError, InvalidOperation):
                continue
            results.append(
                Amount(match.group(0), tuple(values), line_number(text, match.start()), currency, inferred)
            )
            used.append(match.span())

    zh_range = re.compile(
        rf"(?P<pre>{SYMBOL_PATTERN})?\s*(?P<a>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit_a>万亿|千亿|百亿|亿|千万|百万|万|千)?\s*"
        r"(?:至|到|~|[-–—])\s*"
        r"(?P<b>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit_b>万亿|千亿|百亿|亿|千万|百万|万|千)?\s*"
        rf"(?P<cur>{ZH_WORD_PATTERN})"
    )
    for match in zh_range.finditer(text):
        if overlaps(match.span(), used):
            continue
        currency = currency_from_zh_word(match.group("cur")) or currency_from_symbol(
            match.group("pre")
        )
        if currency is None:
            continue
        unit_a = match.group("unit_a") or match.group("unit_b") or ""
        unit_b = match.group("unit_b") or match.group("unit_a") or ""
        values = (
            parse_decimal(match.group("a")) * ZH_SCALE[unit_a],
            parse_decimal(match.group("b")) * ZH_SCALE[unit_b],
        )
        results.append(Amount(match.group(0), values, line_number(text, match.start()), currency))
        used.append(match.span())

    zh_single = re.compile(
        rf"(?P<pre>{SYMBOL_PATTERN})?\s*(?<![\d,.])(?P<a>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>万亿|千亿|百亿|亿|千万|百万|万|千)?\s*"
        rf"(?P<cur>{ZH_WORD_PATTERN})"
    )
    for match in zh_single.finditer(text):
        if overlaps(match.span(), used):
            continue
        currency = currency_from_zh_word(match.group("cur")) or currency_from_symbol(
            match.group("pre")
        )
        if currency is None:
            continue
        value = parse_decimal(match.group("a")) * ZH_SCALE[match.group("unit") or ""]
        results.append(Amount(match.group(0), (value,), line_number(text, match.start()), currency))
        used.append(match.span())

    # 双语稿常见写法：一行只写一次币种，前面的数值只带中文量级词
    # （例如「780亿到890亿，目标1100亿美元」）。
    zh_unit_only = re.compile(
        r"(?<![\d,.])(?P<a>\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>万亿|千亿|百亿|亿|千万|百万|万|千)"
        rf"(?!\s*(?:{ZH_WORD_PATTERN}|{ZH_NON_MONEY}))"
    )
    offset = 0
    for line in text.splitlines(keepends=True):
        context = line_currencies(line)
        if len(context) != 1:
            offset += len(line)
            continue
        currency = next(iter(context))
        for match in zh_unit_only.finditer(line):
            absolute_span = (offset + match.start(), offset + match.end())
            if overlaps(absolute_span, used):
                continue
            value = parse_decimal(match.group("a")) * ZH_SCALE[match.group("unit")]
            results.append(
                Amount(match.group(0), (value,), line_number(text, absolute_span[0]), currency, True)
            )
            used.append(absolute_span)
        offset += len(line)

    return sorted(results, key=lambda item: (item.line, item.raw))


def fmt(value: Decimal) -> str:
    try:
        return f"{int(value):,}"
    except (ValueError, InvalidOperation):
        return str(value)


def audit(source_text: str, target_text: str | None, default_currency: str = "USD") -> AuditResult:
    result = AuditResult(source=extract_amounts(source_text, default_currency))
    if target_text is None:
        return result
    result.target = extract_amounts(target_text, default_currency)

    by_currency: dict[str, set[Decimal]] = {code: set() for code in CURRENCIES}
    for amount in result.source:
        by_currency.setdefault(amount.currency, set()).update(amount.values)

    for amount in result.target:
        same = by_currency.get(amount.currency, set())
        for value in amount.values:
            if value in same:
                continue
            hint = f"成稿的 {amount.currency} {fmt(value)} 在源稿同币种中找不到等值表达；请核对数字账本"
            kind = "no_equivalent"
            if value * 10 in same or (value % 10 == 0 and value / 10 in same):
                factor = "×10" if value * 10 in same else "÷10"
                hint = (
                    f"疑似量级错误：源稿存在 {factor} 于该值的同币种金额"
                    f"（billion↔亿 差 10 倍）；确认是不是把 billion 直接抄成「亿」"
                )
                kind = "magnitude"
            else:
                for other, values in by_currency.items():
                    if other != amount.currency and value in values:
                        hint = (
                            f"疑似币种漂移：同一数值在源稿中以 {other} 出现，"
                            f"成稿写成了 {amount.currency}；不要做汇率折算，回原文确认"
                        )
                        kind = "currency"
                        break
            result.warnings.append(
                Warning_(amount.line, amount.currency, value, amount.raw, hint, kind)
            )
    return result


def print_inventory(label: str, amounts: list[Amount]) -> None:
    print(f"[{label}]")
    if not amounts:
        print("(未找到显式金额)")
        return
    for amount in amounts:
        normalized = " .. ".join(f"{amount.currency} {fmt(value)}" for value in amount.values)
        flag = "  [币种为推断]" if amount.inferred else ""
        print(f"L{amount.line}\t{normalized}\t{amount.raw.strip()}{flag}")


SELF_TEST_SOURCE = """Our ARR is 78 billion US dollars today, and we target 110 billion USD.
Guidance is 12 to 14 billion dollars for next year.
去年营收 12.5 亿元人民币，其中香港区贡献 3 亿港元。
He said roughly two hundred million dollars.
We serve 5 million customers.
"""

SELF_TEST_GOOD = """我们当前 ARR 为 780 亿美元，目标 1,100 亿美元。
明年指引 120 亿至 140 亿美元。
去年营收 12.5 亿元人民币，其中香港区贡献 3 亿港元。
他说大约 2 亿美元。
我们服务 500 万客户。
"""

SELF_TEST_BAD = """我们当前 ARR 为 78 亿美元，目标 1,100 亿美元。
明年指引 120 亿至 140 亿美元。
去年营收 12.5 亿港元，其中香港区贡献 3 亿港元。
他说大约 2 亿美元。
"""


def self_test() -> tuple[bool, list[dict]]:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    source = extract_amounts(SELF_TEST_SOURCE)
    by_currency: dict[str, set[str]] = {}
    for amount in source:
        for value in amount.values:
            by_currency.setdefault(amount.currency, set()).add(fmt(value))

    check(
        "USD 抽取",
        by_currency.get("USD") == {"78,000,000,000", "110,000,000,000", "12,000,000,000",
                                   "14,000,000,000", "200,000,000"},
        str(sorted(by_currency.get("USD", set()))),
    )
    check("CNY 抽取", by_currency.get("CNY") == {"1,250,000,000"}, str(by_currency.get("CNY")))
    check("HKD 抽取", by_currency.get("HKD") == {"300,000,000"}, str(by_currency.get("HKD")))
    check(
        "非金额数字不计入",
        all("customers" not in a.raw for a in source),
        "5 million customers 不应被抽取",
    )

    clean = audit(SELF_TEST_SOURCE, SELF_TEST_GOOD)
    check("正确成稿无告警", not clean.warnings, str([w.as_dict() for w in clean.warnings]))

    dirty = audit(SELF_TEST_SOURCE, SELF_TEST_BAD)
    kinds = {w.kind for w in dirty.warnings}
    check("量级错误被捕获", "magnitude" in kinds, str(sorted(kinds)))
    check("币种漂移被捕获", "currency" in kinds, str(sorted(kinds)))

    return all(item["ok"] for item in checks), checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="金额与量级审计（USD / HKD / CNY）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", help="源文件路径，或 self-test")
    parser.add_argument("target", nargs="?", help="成稿文件路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--fail-on-warn", action="store_true", help="有告警时返回非零退出码")
    parser.add_argument(
        "--default-currency",
        default="USD",
        choices=list(CURRENCIES),
        help="行内完全没有币种标记时，英文量级金额按哪种币种记账（默认 USD）",
    )
    parser.add_argument("--version", action="version", version=f"audit_numeric_units {VERSION}")
    args = parser.parse_args()

    if args.source in (None, "self-test"):
        if args.source is None:
            payload = {
                "ok": False,
                "error": "缺少源文件路径",
                "hint": (
                    "用法：python3 .agents/skills/dalton-transcript-polish/scripts/audit_numeric_units.py "
                    "<源文件> [<成稿文件>]；自检用 self-test"
                ),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(f"ERROR\t{payload['error']}\nHINT\t{payload['hint']}", file=sys.stderr)
            return 2
        ok, checks = self_test()
        if args.json:
            print(json.dumps({"ok": ok, "version": VERSION, "checks": checks},
                             ensure_ascii=False, indent=2))
        else:
            for item in checks:
                print(f"{'PASS' if item['ok'] else 'FAIL'}\t{item['name']}\t{item['detail']}")
            print("OK\tself-test 通过" if ok else "FAIL\tself-test 未通过")
        return 0 if ok else 1

    source_path = Path(args.source)
    if not source_path.is_file():
        payload = {
            "ok": False,
            "error": f"源文件不存在：{source_path}",
            "hint": "确认路径相对当前工作目录；音频转写源稿一般在 transcriptions/<job_id>/transcript.txt 下",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR\t{payload['error']}\nHINT\t{payload['hint']}", file=sys.stderr)
        return 2

    target_path = Path(args.target) if args.target else None
    if target_path is not None and not target_path.is_file():
        payload = {
            "ok": False,
            "error": f"成稿文件不存在：{target_path}",
            "hint": "只传源文件可以先看金额清单，比对需要两个文件",
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR\t{payload['error']}\nHINT\t{payload['hint']}", file=sys.stderr)
        return 2

    result = audit(
        source_path.read_text(encoding="utf-8"),
        target_path.read_text(encoding="utf-8") if target_path else None,
        args.default_currency,
    )

    if args.json:
        print(json.dumps(
            {
                "ok": True,
                "version": VERSION,
                "source": [a.as_dict() for a in result.source],
                "target": [a.as_dict() for a in result.target],
                "warnings": [w.as_dict() for w in result.warnings],
                "warning_count": len(result.warnings),
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print_inventory("SOURCE", result.source)
        if target_path:
            print_inventory("TARGET", result.target)
            for warning in result.warnings:
                print(
                    f"WARN\tTARGET L{warning.line}\t{warning.currency} {fmt(warning.value)}"
                    f"\t{warning.raw.strip()}\t{warning.hint}"
                )
            if not result.warnings:
                print("OK\t成稿中每个显式金额都能在源稿同币种中找到等值表达。")

    if args.fail_on_warn and result.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
