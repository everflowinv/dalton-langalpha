#!/usr/bin/env python3
"""China and global ToB industry weekly: collect facts, then submit text."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
REF_DIR = SKILL_DIR / "references"
HK = ZoneInfo("Asia/Hong_Kong")
PROFILE = "tob-industry-weekly"
STATE_REL = "state/tob-industry-weekly"
NOTES_REL = f"{STATE_REL}/notes.jsonl"
SNAP_REL = f"{STATE_REL}/snapshots"
RUNS_REL = f"{STATE_REL}/collection-runs"
SESSIONS_REL = f"{STATE_REL}/sessions.json"
ARCHIVE_REL = "research-outputs/tob-industry-tracker"
MARKETS = ("A", "HK", "US")

BANNED_PHRASES = [
    "大幅增长", "显著提升", "爆发式增长", "明显改善", "持续向好", "大幅改善",
    "强劲复苏", "全面提升", "遥遥领先", "超预期增长",
]
CALIBER_RULES = [
    {
        "key": "guidance_caliber",
        "trigger": r"(指引|目标|展望|预计|有望|力争)",
        "require": r"(指引|预测|预期|管理层|尚未实现|非实际|规划目标|目标价)",
        "fix": "前瞻数字要标成指引或预测，与已发生的实际数分开写",
    },
    {
        "key": "revision_caliber",
        "trigger": r"(上修|下修|上调|下调)",
        "require": r"(机构|券商|目标价|盈利预测|预期|评级|estimate|指引|管理层|区间|中值)",
        "fix": "修正要写清是谁改的：卖方带机构名和日期，公司改指引写明「指引」并给新旧区间",
    },
    {
        "key": "beat_caliber",
        "trigger": r"(超预期|低于预期|beat|miss|符合预期)",
        "require": r"(预期|共识|指引|市场|consensus|estimate)",
        "fix": "beat/miss 必须写清对比的是什么预期（市场共识还是公司指引）",
    },
]
CARD_MAX_CHARS = 10000
CALIBER_NUM_WINDOW = 30
CALIBER_CTX_WINDOW = 45
DATE_PATTERNS = [
    r"20\d{2}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?",
    r"\d{1,2}\s*月\s*\d{1,2}\s*日",
]
PERIOD_MARKERS = (
    r"(Q[1-4]|季度|半年|年度|1[0-2]月|同比|环比|FY|TTM|截至|期间|窗口|日历|"
    r"全年|上半年|下半年|当季|上季)"
)
EVENT_TYPES = {"earnings", "guidance", "management", "sellside", "revision", "calendar"}
GRADES = {"A", "B", "C", "D"}
SOURCES = {
    "company_disclosure", "earnings_call", "sellside_note", "roadshow_minutes",
    "expert_call", "news", "web", "community",
}
DIRECTIONS = {
    "beat", "miss", "inline", "upgrade", "downgrade", "positive", "negative", "neutral",
}
QUOTE_SOURCES = {"earnings_call", "roadshow_minutes", "expert_call"}
WORKSPACE: Path | None = None


class CliExit(SystemExit):
    """Exit the CLI after a JSON error has already been printed."""


def _load_script(filename: str, module_name: str):
    path = HERE / filename
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{filename} is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _card_io():
    return _load_script("card_io.py", "card_io_dalton_tob_industry_weekly")


def _cninfo():
    return _load_script("cninfo_a_scan.py", "cninfo_a_scan_dalton_tob_industry_weekly")


def emit(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def fail(msg: str, hint: str, extra: dict | None = None) -> None:
    out = {"ok": False, "error": msg, "hint": hint}
    if extra:
        out.update(extra)
    emit(out)
    raise CliExit(1)


def hk_today():
    return datetime.now(HK).date()


def parse_date(raw: str):
    return datetime.strptime(raw, "%Y-%m-%d").date()


def anchor_friday(as_of: str | None = None):
    """Most recent Friday on or before the Hong Kong calendar date."""
    day = parse_date(as_of) if as_of else hk_today()
    return day - timedelta(days=(day.weekday() - 4) % 7)


def bind_workspace(raw: str | None) -> Path:
    global WORKSPACE
    if not raw:
        fail("missing --workspace-dir", "每个命令都要带 --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        fail("workspace is a symlink", "工作区不能是符号链接")
    if not path.is_dir():
        fail("workspace not found", "先创建工作区目录")
    WORKSPACE = path.resolve()
    return WORKSPACE


def rel_file(relative: str) -> Path:
    if WORKSPACE is None:
        fail("missing --workspace-dir", "每个命令都要带 --workspace-dir")
    try:
        return _card_io().workspace_file(WORKSPACE, relative)
    except ValueError as exc:
        fail(str(exc), "路径必须留在工作区内")


def write_text_rel(relative: str, text: str) -> None:
    path = rel_file(relative)
    _card_io().write_bytes_exclusive(path, text.encode("utf-8"))


def write_json_rel(relative: str, payload) -> None:
    write_text_rel(relative, json.dumps(payload, ensure_ascii=False, indent=1))


def read_text_rel(relative: str) -> str | None:
    path = rel_file(relative)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def read_json_rel(relative: str):
    text = read_text_rel(relative)
    if text is None:
        return None
    return json.loads(text)


def dir_names(relative_dir: str, suffix: str) -> list[str]:
    folder = rel_file(relative_dir + "/.dir").parent
    if folder.is_symlink():
        fail("refusing symlink", "目录不能是符号链接")
    if not folder.is_dir():
        return []
    names = []
    for child in folder.iterdir():
        if child.is_symlink():
            fail("refusing symlink", "目录里的符号链接不能当状态文件")
        if child.name.endswith(suffix):
            names.append(child.name)
    return sorted(names)


def delivery_ok(args) -> bool:
    return (
        getattr(args, "content_kind", None) == "text"
        and bool(getattr(args, "delivery_confirmed", False))
        and getattr(args, "status", None) == "sent"
    )


def require_delivery(args, action: str) -> None:
    if delivery_ok(args):
        return
    fail(
        "未确认发送",
        f"{action} 需要 --content-kind text --delivery-confirmed --status sent",
        {"written": None},
    )


def load_ref(name: str):
    if REF_DIR.is_symlink():
        fail("refusing symlink", "references 目录不能是符号链接")
    path = REF_DIR / name
    if path.is_symlink():
        fail("refusing symlink", "参考文件不能是符号链接")
    if not path.is_file():
        fail(f"缺少 {name}", "确认技能 references 目录完整")
    return json.loads(path.read_text(encoding="utf-8"))


def universe() -> dict:
    return load_ref("universe.json")


def subsectors() -> dict:
    return load_ref("subsectors.json")


def mask_dates(text: str) -> str:
    out = text
    for pattern in DATE_PATTERNS:
        out = re.sub(pattern, "DATE", out)
    return out


def word_count(text: str) -> int:
    english = re.findall(r"[A-Za-z0-9']+", text)
    chinese = re.findall(r"[一-鿿]", text)
    return len(english) + len(chinese)


def _network_probe() -> dict[str, str]:
    """Filings come from finance(). This probe does not open a socket."""
    return {"detail": "公告由 finance 取数，脚本不打开网络连接"}


def load_sessions(friday: str) -> dict[str, str]:
    """Sessions for this report week only. Last week's dates stay on their own key."""
    payload = read_json_rel(SESSIONS_REL)
    if not isinstance(payload, dict):
        return {}
    weeks = payload.get("weeks")
    if not isinstance(weeks, dict):
        return {}
    week = weeks.get(friday)
    if not isinstance(week, dict):
        return {}
    raw = week.get("markets") if isinstance(week.get("markets"), dict) else week
    if not isinstance(raw, dict):
        return {}
    found = {}
    for market in MARKETS:
        value = raw.get(market)
        if isinstance(value, str) and len(value) >= 10:
            found[market] = value[:10]
    return found


def session_status(requested, sessions: dict[str, str]) -> dict:
    """Common last session on or before the requested Friday, across A, HK and US."""
    usable = {}
    missing = []
    for market in MARKETS:
        raw = sessions.get(market)
        if not raw:
            missing.append(market)
            continue
        try:
            day = parse_date(raw)
        except ValueError:
            missing.append(market)
            continue
        if day > requested:
            missing.append(market)
            continue
        usable[market] = day
    if len(usable) < 3:
        return {
            "price_as_of": None,
            "missing_count": len(missing),
            "missing_markets": missing,
            "holiday_note": (
                "A、HK、US 的最近交易日还没有齐。"
                "业绩窗口终点暂用当周周五，正文不要把请求日写成已核对的行情日期。"
            ),
        }
    price = min(usable.values())
    note = None
    if price < requested:
        note = (
            f"请求日期 {requested.isoformat()} 晚于三市共同最近交易日 {price.isoformat()}。"
            "正文按这个交易日叙述行情日期。"
        )
    return {
        "price_as_of": price.isoformat(),
        "missing_count": 0,
        "missing_markets": [],
        "holiday_note": note,
    }


def collection_windows(as_of: str | None, status: dict) -> dict:
    friday = anchor_friday(as_of)
    now = datetime.now(HK)
    start = (friday - timedelta(days=7)).isoformat()
    price_as_of = status.get("price_as_of")
    if status.get("price_window_missing"):
        price_window = None
    else:
        price_window = {
            "start": start,
            "end": price_as_of or friday.isoformat(),
            "friday": friday.isoformat(),
            "price_as_of": price_as_of,
            "note": "业绩窗口从当周周五往前 7 天起。终点是三市共同最近交易日 price_as_of。",
        }
    return {
        "price_window": price_window,
        "price_window_missing": bool(status.get("price_window_missing")),
        "source_window": {
            "start": start,
            "end": now.date().isoformat(),
            "end_at": now.isoformat(timespec="seconds"),
            "note": (
                "信源窗口从上一期截稿日（上周五，香港日期）到本次采集时刻。"
                "上周五当天的记录若已写入上期归档正文（research-outputs/tob-industry-tracker/<上周五>.md），不重复写。"
            ),
        },
    }


def note_in_source_window(rec: dict, start: str) -> bool:
    """Keep a note when its source date or its collect week falls in the window."""
    for stamp in (rec.get("source_date"), rec.get("week_of")):
        if stamp and stamp >= start:
            return True
    return False


def read_notes() -> list[dict]:
    text = read_text_rel(NOTES_REL)
    if not text:
        return []
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def cmd_self_test(args) -> None:
    checks = []

    def add(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "hint": hint})

    add("python_version", sys.version_info >= (3, 10), sys.version.split()[0], "需要 Python 3.10+")
    try:
        uni = universe()
        companies = uni["companies"]
        add("universe_loaded", len(companies) == 120, f"{len(companies)} 家", "覆盖池是 120 家")
        markets = Counter(row["market"] for row in companies)
        add(
            "market_coverage",
            markets["A"] == 56 and markets["HK"] == 16 and markets["US"] == 48,
            str(dict(markets)),
            "A 56 / HK 16 / US 48",
        )
        dup = [ticker for ticker, count in Counter(row["ticker"] for row in companies).items() if count > 1]
        add("no_duplicate_ticker", not dup, f"重复: {dup}" if dup else "OK")
        known = set(subsectors()["subsectors"])
        bad = sorted({row["subsector"] for row in companies} - known)
        add("taxonomy_consistent", not bad, f"未知细分: {bad}" if bad else "OK",
            "subsectors.json 未定义该 key")
    except CliExit:
        raise
    except Exception as exc:  # noqa: BLE001  # 自检要把名单文件的任意读取错误收成检查项
        add("universe_loaded", False, str(exc), "检查 references/universe.json")
    try:
        probe = rel_file(f"{STATE_REL}/.write-probe")
        probe.parent.mkdir(parents=True, exist_ok=True)
        add("db_writable", os.access(probe.parent, os.W_OK), STATE_REL, "确认该目录可写")
    except CliExit:
        raise
    except OSError as exc:
        add("db_writable", False, str(exc), "确认 state/ 在工作区内且可写")
    try:
        for item in _cninfo().offline_checks():
            add(item["check"], item["ok"], item.get("detail", ""), item.get("hint", ""))
    except Exception as exc:  # noqa: BLE001  # 自检要把筛公告的任意错误收成检查项
        add("cninfo_offline", False, str(exc), "检查 cninfo_a_scan.py")
    if args.network:
        probed = _network_probe()
        add("network_fetch_disabled", True, probed.get("detail", ""), "不要改成脚本直连公告接口")
    ok = all(item["ok"] for item in checks)
    payload = {"ok": ok, "checks": checks}
    if not ok:
        payload["error"] = "self-test 未全部通过"
        payload["hint"] = "; ".join(item["hint"] for item in checks if not item["ok"] and item["hint"])
    emit(payload)
    raise CliExit(0 if ok else 1)


def cmd_anchor(args) -> None:
    friday = anchor_friday(args.as_of)
    emit({
        "ok": True,
        "hk_today": hk_today().isoformat(),
        "friday": friday.isoformat(),
        "report_run_id": f"{PROFILE}-{friday.isoformat()}",
    })


def _status_for(as_of: str | None) -> tuple:
    friday = anchor_friday(as_of)
    requested = parse_date(as_of) if as_of else friday
    status = session_status(requested, load_sessions(friday.isoformat()))
    start = (friday - timedelta(days=7)).isoformat()
    price = status.get("price_as_of")
    if isinstance(price, str) and price < start:
        status = {
            **status,
            "price_as_of": None,
            "price_window_missing": True,
            "holiday_note": (
                f"三市共同最近交易日 {price} 早于业绩窗口起点 {start}，本期不输出业绩窗口。"
            ),
        }
    else:
        status = {**status, "price_window_missing": False}
    return friday, requested, status


def cmd_plan(args) -> None:
    friday, requested, status = _status_for(args.as_of)
    windows = collection_windows(friday.isoformat(), status)
    uni = universe()
    emit({
        "ok": True,
        "as_of": (parse_date(args.as_of) if args.as_of else hk_today()).isoformat(),
        "friday": friday.isoformat(),
        "requested_as_of": requested.isoformat(),
        "price_as_of": status["price_as_of"],
        "holiday_note": status["holiday_note"],
        "missing_count": status["missing_count"],
        "missing_markets": status["missing_markets"],
        "report_run_id": f"{PROFILE}-{friday.isoformat()}",
        **windows,
        "universe_size": len(uni["companies"]),
        "markets": list(MARKETS),
        "subsectors": list(subsectors()["subsectors"]),
        "note": "成稿用 source_window.start 读采集记录。price_as_of 与请求日不同时，按 price_as_of 叙述行情日期。",
    })


def cmd_perf(args) -> None:
    friday, requested, status = _status_for(args.as_of)
    windows = collection_windows(friday.isoformat(), status)
    emit({
        "ok": True,
        "requested_as_of": requested.isoformat(),
        "friday": friday.isoformat(),
        "price_as_of": status["price_as_of"],
        "as_of": status["price_as_of"] or friday.isoformat(),
        "holiday_note": status["holiday_note"],
        "missing_count": status["missing_count"],
        "missing_markets": status["missing_markets"],
        "price_window": windows["price_window"],
        "price_window_missing": windows["price_window_missing"],
        "note": "本报不计算涨跌幅。perf 只核对三市最近交易日。",
    })


def cmd_record_sessions(args) -> None:
    friday = anchor_friday(args.as_of).isoformat()
    incoming = {"A": args.a_date, "HK": args.hk_date, "US": args.us_date}
    incoming = {key: value for key, value in incoming.items() if value}
    if not incoming:
        fail("缺少交易日", "至少给 --a-date、--hk-date、--us-date 中的一个")
    for value in incoming.values():
        try:
            parse_date(value)
        except ValueError:
            fail("交易日格式不对", "用 YYYY-MM-DD")
    current = load_sessions(friday)
    current.update(incoming)
    stored = read_json_rel(SESSIONS_REL)
    if not isinstance(stored, dict):
        stored = {}
    weeks = stored.get("weeks")
    if not isinstance(weeks, dict):
        weeks = {}
    markets = {market: current[market] for market in MARKETS if market in current}
    weeks[friday] = {
        "markets": markets,
        "updated_at": datetime.now(HK).isoformat(timespec="seconds"),
    }
    stored["weeks"] = weeks
    stored.pop("markets", None)
    write_json_rel(SESSIONS_REL, stored)
    emit({"ok": True, "written": SESSIONS_REL, "friday": friday, "markets": markets})


def cmd_universe(args) -> None:
    rows = universe()["companies"]
    if args.market:
        rows = [row for row in rows if row["market"] == args.market]
    if args.subsector:
        rows = [row for row in rows if row["subsector"] == args.subsector]
    emit({
        "ok": True,
        "count": len(rows),
        "total": universe()["meta"]["count"],
        "companies": rows,
    })


def cmd_calendar(args) -> None:
    friday, _requested, status = _status_for(args.as_of)
    windows = collection_windows(friday.isoformat(), status)
    pool = [
        {
            "ticker": row["ticker"],
            "name": row["name"],
            "market": row["market"],
            "subsector": row["subsector"],
            "ae_query": row.get("ae_query"),
        }
        for row in universe()["companies"]
    ]
    emit({
        "ok": True,
        "friday": friday.isoformat(),
        "days": args.days,
        **windows,
        "pool_count": len(pool),
        "pool": pool,
        "note": "财报日历由 finance 与 research 拼，再用 add-note --event-type calendar 入库。",
    })


def cmd_add_note(args) -> None:
    if args.evidence_grade == "D":
        fail("D 级证据拒收", "D 级是转述、无口径或无链接，不入库")
    if args.evidence_grade not in GRADES:
        fail("evidence-grade 不合法", "只接受 A、B、C")
    if args.event_type not in EVENT_TYPES:
        fail("event-type 不合法", "见命令帮助里的可选值")
    if args.source_type not in SOURCES:
        fail("source-type 不合法", "见命令帮助里的可选值")
    if args.direction not in DIRECTIONS:
        fail("direction 不合法", "见命令帮助里的可选值")
    if args.source_type in QUOTE_SOURCES and not args.source_attribution:
        fail("纪要与专家类必须带 --source-attribution", "来源属性缺失")
    if args.verbatim:
        count = word_count(args.verbatim)
        if count > 20:
            fail(f"verbatim 超过 20 词（当前 {count}）", "逐字引用不超过 20 词")
    if args.evidence_grade in {"B", "C"} and not (args.cross_check and args.cross_check.strip()):
        fail(
            "B/C 级证据缺 cross-check",
            "要写卖方、客户、渠道或上游的印证；本周没有印证就传 --cross-check none",
        )
    try:
        parse_date(args.source_date)
    except ValueError:
        fail("source-date 格式不对", "用 YYYY-MM-DD")
    week_of = args.week_of or anchor_friday(args.source_date).isoformat()
    try:
        parse_date(week_of)
    except ValueError:
        fail("week-of 格式不对", "用 YYYY-MM-DD")
    cross = (args.cross_check or "").strip()
    record = {
        "ts": datetime.now(HK).isoformat(timespec="seconds"),
        "week_of": week_of,
        "ticker": args.ticker,
        "company": args.company,
        "market": args.market or "unknown",
        "event_type": args.event_type,
        "evidence_grade": args.evidence_grade,
        "direction": args.direction,
        "source_type": args.source_type,
        "claim": args.claim,
        "source_url": args.source_url,
        "source_date": args.source_date,
        "numbers": args.numbers,
        "verbatim": args.verbatim,
        "source_attribution": args.source_attribution,
        "cross_check": args.cross_check,
        "cross_checked": bool(cross) and cross.lower() != "none",
    }
    existing = read_text_rel(NOTES_REL) or ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    write_text_rel(NOTES_REL, existing + json.dumps(record, ensure_ascii=False) + "\n")
    emit({"ok": True, "written": NOTES_REL, "record": record})


def cmd_notes(args) -> None:
    rows = read_notes()
    if args.ticker:
        rows = [row for row in rows if row.get("ticker") == args.ticker]
    if args.event_type:
        rows = [row for row in rows if row.get("event_type") == args.event_type]
    if args.since:
        rows = [row for row in rows if note_in_source_window(row, args.since)]
    emit({"ok": True, "count": len(rows), "notes": rows})


def cmd_validate(args) -> None:
    path = rel_file(args.file)
    if not path.is_file():
        fail("报告文件不存在", f"确认路径：{args.file}")
    text = path.read_text(encoding="utf-8")
    problems = []
    for phrase in BANNED_PHRASES:
        if phrase in text:
            problems.append({
                "type": "vague_phrase",
                "detail": phrase,
                "fix": "换成带口径的数字，或直接删掉",
            })
    if args.profile == "card":
        required = [["本周总览"], ["业绩"], ["管理层"], ["卖方"], ["下周"]]
        template = "templates/card.md"
        if len(text) > CARD_MAX_CHARS:
            problems.append({
                "type": "card_too_long",
                "detail": f"卡片正文 {len(text)} 字，上限 {CARD_MAX_CHARS} 字",
                "fix": "先压只有营收利润两行、没有卖方点评的小公司，细节留在全文稿",
            })
    else:
        required = [["本周总览"], ["业绩"], ["管理层"], ["卖方"], ["下周"], ["信源"], ["口径"], ["免责"]]
        template = "templates/report.md"
    for names in required:
        if not any(name in text for name in names):
            problems.append({
                "type": "missing_section",
                "detail": " / ".join(names),
                "fix": f"按 {template} 补齐该章节",
            })
    if "$" in text:
        problems.append({
            "type": "feishu_latex_risk",
            "detail": "正文含 $",
            "fix": "金额写「美元」「港元」「元」",
        })
    for line in text.splitlines():
        if not re.search(r"\d", line):
            continue
        masked = mask_dates(line)
        for rule in CALIBER_RULES:
            hit = None
            for match in re.finditer(rule["trigger"], line):
                near = masked[max(0, match.start() - CALIBER_NUM_WINDOW):match.end() + CALIBER_NUM_WINDOW]
                if not re.search(r"\d", near):
                    continue
                ctx = line[max(0, match.start() - CALIBER_CTX_WINDOW):match.end() + CALIBER_CTX_WINDOW]
                if not re.search(rule["require"], ctx):
                    hit = match.group(0)
                    break
            if hit:
                problems.append({
                    "type": "caliber_label",
                    "detail": f'{rule["key"]}（触发词 {hit}）: {line.strip()[:60]}',
                    "fix": rule["fix"],
                })
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not re.search(r"\d+(\.\d+)?%", line):
            continue
        if re.search(PERIOD_MARKERS, line):
            continue
        exempt = False
        for previous in range(index - 1, -1, -1):
            prev = lines[previous]
            if not prev.strip():
                continue
            if re.search(PERIOD_MARKERS, prev):
                exempt = True
                break
            if not re.match(r"\s*([-*•]|\d+[.、)])\s", prev):
                break
        if exempt:
            continue
        problems.append({
            "type": "number_without_period",
            "detail": line.strip()[:60],
            "fix": "百分比要带时间窗或口径，或在本段小标题里写清窗口",
        })
    blocking = [item for item in problems if item["type"] != "number_without_period"]
    emit({
        "ok": not blocking,
        "file": args.file,
        "problem_count": len(problems),
        "problems": problems,
        "hint": (
            "number_without_period 是提醒项；vague_phrase / missing_section / "
            "feishu_latex_risk / caliber_label / card_too_long 必须清零才能提交"
        ),
    })


def _collection_run_rel(friday: str) -> str:
    return f"{RUNS_REL}/{friday}.json"


def cmd_start_collection(args) -> None:
    friday = anchor_friday(args.as_of).isoformat()
    rel = _collection_run_rel(friday)
    existing = read_json_rel(rel)
    if not isinstance(existing, dict):
        existing = {}
    started = existing.get("started_at")
    kept = isinstance(started, str) and started != ""
    if not kept:
        existing["started_at"] = datetime.now(HK).isoformat(timespec="seconds")
    existing["as_of"] = friday
    existing["mode"] = "collect"
    existing.pop("recorded_at", None)
    write_json_rel(rel, existing)
    emit({
        "ok": True,
        "written": rel,
        "as_of": friday,
        "mode": "collect",
        "started_at": existing["started_at"],
        "kept": kept,
    })


def cmd_record_collection(args) -> None:
    friday = anchor_friday(args.as_of).isoformat()
    rel = _collection_run_rel(friday)
    existing = read_json_rel(rel)
    started = existing.get("started_at") if isinstance(existing, dict) else None
    if not isinstance(started, str) or started == "":
        fail("缺少采集开始时间", "先运行 start-collection，再跑 record-collection")
    existing["as_of"] = friday
    existing["mode"] = "collect"
    existing["finished_at"] = datetime.now(HK).isoformat(timespec="seconds")
    existing["note"] = args.note or ""
    existing.pop("recorded_at", None)
    write_json_rel(rel, existing)
    emit({
        "ok": True,
        "written": rel,
        "as_of": friday,
        "mode": "collect",
        "started_at": existing["started_at"],
        "finished_at": existing["finished_at"],
        "note": existing["note"],
    })


def cmd_archive(args) -> None:
    require_delivery(args, "archive")
    source = rel_file(args.file)
    if not source.is_file():
        fail("报告文件不存在", f"确认路径：{args.file}")
    friday = anchor_friday(args.as_of)
    rel = f"{ARCHIVE_REL}/{friday.isoformat()}.md"
    write_text_rel(rel, source.read_text(encoding="utf-8"))
    emit({"ok": True, "written": rel, "source": args.file})


def cmd_snapshot(args) -> None:
    if args.write:
        require_delivery(args, "snapshot --write")
    friday, requested, status = _status_for(args.as_of)
    windows = collection_windows(friday.isoformat(), status)
    start = windows["source_window"]["start"]
    window_notes = [row for row in read_notes() if note_in_source_window(row, start)]
    tickers = sorted({row.get("ticker") for row in window_notes if row.get("ticker")})
    snap = {
        "ok": True,
        "window_friday": friday.isoformat(),
        "requested_as_of": requested.isoformat(),
        "price_as_of": status["price_as_of"],
        "created_at": datetime.now(HK).isoformat(timespec="seconds"),
        "notes_count": len(read_notes()),
        "window_notes": len(window_notes),
        "tickers": tickers,
        "source_window": windows["source_window"],
        "price_window": windows["price_window"],
    }
    rel = f"{SNAP_REL}/{friday.isoformat()}.json"
    if args.write:
        existing = read_text_rel(rel)
        if existing is not None and not args.overwrite:
            fail("快照已存在", "加 --overwrite 覆盖，或确认本期是否已经写过", {"path": rel})
        write_json_rel(rel, snap)
        snap["written"] = rel
    else:
        snap["written"] = None
        snap["note"] = "未加 --write，本次不落盘"
    emit(snap)


def cmd_diff(args) -> None:
    friday = anchor_friday(args.as_of)
    files = [name for name in dir_names(SNAP_REL, ".json") if name[:-5] < friday.isoformat()]
    if not files:
        emit({"ok": True, "has_baseline": False, "note": "无可比历史快照"})
        return
    base = read_json_rel(f"{SNAP_REL}/{files[-1]}") or {}
    emit({
        "ok": True,
        "has_baseline": True,
        "previous": files[-1],
        "baseline": base.get("window_friday"),
        "previous_tickers": base.get("tickers", []),
        "note": "公司级对比用 notes.jsonl 的信源窗口，快照只记录报告日和当时入库的代码",
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tob-industry-weekly")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    delivery = argparse.ArgumentParser(add_help=False)
    delivery.add_argument("--content-kind", default=None)
    delivery.add_argument("--delivery-confirmed", action="store_true")
    delivery.add_argument("--status", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    check = sub.add_parser("self-test", parents=[shared])
    check.add_argument("--network", action="store_true")
    check.set_defaults(func=cmd_self_test)

    anchor = sub.add_parser("anchor", parents=[shared])
    anchor.add_argument("--as-of")
    anchor.set_defaults(func=cmd_anchor)

    plan = sub.add_parser("plan", parents=[shared])
    plan.add_argument("--as-of")
    plan.set_defaults(func=cmd_plan)

    perf = sub.add_parser("perf", parents=[shared])
    perf.add_argument("--as-of")
    perf.set_defaults(func=cmd_perf)

    sessions = sub.add_parser("record-sessions", parents=[shared])
    sessions.add_argument("--as-of")
    sessions.add_argument("--a-date")
    sessions.add_argument("--hk-date")
    sessions.add_argument("--us-date")
    sessions.set_defaults(func=cmd_record_sessions)

    names = sub.add_parser("universe", parents=[shared])
    names.add_argument("--market", choices=["A", "HK", "US"])
    names.add_argument("--subsector")
    names.set_defaults(func=cmd_universe)

    calendar = sub.add_parser("calendar", parents=[shared])
    calendar.add_argument("--as-of")
    calendar.add_argument("--days", type=int, default=7)
    calendar.set_defaults(func=cmd_calendar)

    note = sub.add_parser("add-note", parents=[shared])
    note.add_argument("--ticker", required=True)
    note.add_argument("--company", required=True)
    note.add_argument("--market", choices=["A", "HK", "US"])
    note.add_argument("--event-type", required=True, choices=sorted(EVENT_TYPES))
    note.add_argument("--evidence-grade", required=True)
    note.add_argument("--direction", required=True, choices=sorted(DIRECTIONS))
    note.add_argument("--source-type", required=True, choices=sorted(SOURCES))
    note.add_argument("--claim", required=True)
    note.add_argument("--source-url")
    note.add_argument("--source-date", required=True)
    note.add_argument("--week-of")
    note.add_argument("--numbers")
    note.add_argument("--verbatim")
    note.add_argument("--source-attribution")
    note.add_argument("--cross-check")
    note.set_defaults(func=cmd_add_note)

    listed = sub.add_parser("notes", parents=[shared])
    listed.add_argument("--ticker")
    listed.add_argument("--event-type", choices=sorted(EVENT_TYPES))
    listed.add_argument("--since")
    listed.set_defaults(func=cmd_notes)

    validate = sub.add_parser("validate", parents=[shared])
    validate.add_argument("--file", required=True)
    validate.add_argument("--profile", choices=["card", "report"], default="report")
    validate.set_defaults(func=cmd_validate)

    started = sub.add_parser("start-collection", parents=[shared])
    started.add_argument("--as-of")
    started.set_defaults(func=cmd_start_collection)

    recorded = sub.add_parser("record-collection", parents=[shared])
    recorded.add_argument("--as-of")
    recorded.add_argument("--note", default="")
    recorded.set_defaults(func=cmd_record_collection)

    archive = sub.add_parser("archive", parents=[shared, delivery])
    archive.add_argument("--file", required=True)
    archive.add_argument("--as-of")
    archive.set_defaults(func=cmd_archive)

    snap = sub.add_parser("snapshot", parents=[shared, delivery])
    snap.add_argument("--as-of")
    snap.add_argument("--write", action="store_true")
    snap.add_argument("--overwrite", action="store_true")
    snap.set_defaults(func=cmd_snapshot)

    diff = sub.add_parser("diff", parents=[shared])
    diff.add_argument("--as-of")
    diff.set_defaults(func=cmd_diff)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code is None else (code if isinstance(code, int) else 1)
    try:
        bind_workspace(getattr(args, "workspace_dir", None))
        args.func(args)
    except CliExit as exc:
        code = exc.code
        return 1 if code is None else (code if isinstance(code, int) else 1)
    except Exception as exc:  # noqa: BLE001  # 命令行边界把未分类错误收成 JSON，避免栈信息直接吐出
        emit({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "检查输入参数；路径必须留在工作区内",
        })
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
