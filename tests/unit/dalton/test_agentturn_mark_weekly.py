"""OC-1 Mark new-energy weekly: offline prep, check, restyle, and doc receipt."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import sys
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-mark-weekly-newenergy"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "mark_weekly.py"
TARGETS_PATH = SKILL_ROOT / "references" / "targets.json"
PROFILE = "mark-weekly-newenergy"
END = "2026-11-07"
ORIGINAL_DOC = "Qd31dpe3dod18xxrIqIc2dttnhb"
RESTYLED_DOC = "ACehd71zho9YzTxTC6sc7a26neg"
BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")
BANNED_RECIPIENTS = ("discord", "feishu:", "feishu_send", ".openclaw", "chat_id")
_RECIPIENT_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
NEW_ENERGY = {"battery", "solar", "equip", "ev", "av", "power", "engine", "nuclear"}
SELL = ["sell_side_report", "sell_side_comment", "meeting_minutes"]
NEWS = ["news", "announcement"]


def _load():
    name = "dalton_mark_weekly_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, CLI_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(module, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    text = buffer.getvalue()
    return code, json.loads(text)


def _bash_commands(text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
        tokens: list[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                stripped = stripped[:-1].strip()
            tokens.extend(stripped.split())
        current: list[str] = []
        for token in tokens:
            if token == "python3" and current:
                commands.append(current)
                current = [token]
            else:
                current.append(token)
        if current:
            commands.append(current)
    return commands


def _argv_from(command: list[str], replacements: dict[str, str]) -> list[str]:
    script_at = next(index for index, token in enumerate(command) if token.endswith(".py"))
    argv: list[str] = []
    for token in command[script_at + 1 :]:
        token = token.strip("'\"")
        for old, new in replacements.items():
            token = token.replace(old, new)
        argv.append(token)
    return argv


def _write(workspace: Path, relative: str, text: str) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(workspace: Path, relative: str, payload: dict) -> None:
    _write(workspace, relative, json.dumps(payload, ensure_ascii=False))


def _targets() -> dict:
    return json.loads(TARGETS_PATH.read_text(encoding="utf-8"))


def _company_block(company: dict) -> list[str]:
    return [
        f"### {company['name']}（{company['ticker']}）近一周 +0.1%",
        "- **判断：中性，本周没有新增重大订单。**",
        "- NA（已查：AlphaEngine 研报/公告、内部库、web_search 中英文）",
        "",
    ]


def _passing_draft(cfg: dict | None = None) -> str:
    cfg = cfg or _targets()
    lines = [
        "# 新能源与中概股周度动态（10/31至11/7）",
        "窗口是上一个周六到本周六。doc 指 AlphaEngine 文档号。",
        "",
        "## 一、本周结论",
        "补充说明。" * 1600,
        "",
        "## 二、行业动态",
    ]
    for title in (
        "中国锂电与储能",
        "海外锂电与储能",
        "中国新能源车",
        "海外新能源车",
        "燃油车与汽车出口",
        "AI 电力链",
    ):
        lines.append(f"### {title}")
        lines.append("本周行业保持原有节奏。")
        lines.append("")
    lines.append("## 三、新能源公司")
    lines.append("")
    for company in cfg["companies"]:
        if company["grp"] in NEW_ENERGY:
            lines.extend(_company_block(company))
    lines.append("## 四、中概股公司")
    lines.append("")
    for company in cfg["companies"]:
        if company["grp"] not in NEW_ENERGY:
            lines.extend(_company_block(company))
    lines.extend(
        [
            "## 五、业绩日历",
            "### 本周已发布",
            "无。",
            "### 未来两周",
            "无。",
            "### 只发中期和年度业绩",
            "地平线、华润万象生活、滨江服务、华润置地、建发国际只发中期和年度业绩。",
            "",
            "## 六、近一周涨跌幅",
            "| 公司 | 代码 | 收盘日 | 收盘价 | 近一周涨跌 |",
            "|---|---|---|---|---|",
            "| 宁德时代 | 300750.SZ | 2026-11-06 | 100.00 | +0.1% |",
            "",
            "## 七、未核实与检索失败",
            "本周没有待核项。",
            "",
        ]
    )
    return "\n".join(lines)


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def _no_leak(payload: dict, workspace: Path) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    assert "/Users/" not in blob
    assert str(workspace) not in blob
    assert _RECIPIENT_OC_RE.search(blob) is None


def _price_frame(tickers: list[str], start: str, end: str, close_for):
    import pandas as pd

    index = pd.bdate_range(start, pd.Timestamp(end) - pd.Timedelta(days=1))
    columns = pd.MultiIndex.from_product([list(tickers), ["Close"]])
    rows = [[close_for(symbol, stamp) for symbol in tickers] for stamp in index]
    return pd.DataFrame(rows, index=index, columns=columns)


class _CalendarTicker:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.calendar = {
            "Earnings Date": [dt.date(2026, 11, 20)],
            "Earnings Average": 1.25,
        }


class _CninfoResponse:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(
            {
                "prbookinfos": [
                    {
                        "f002d_0102": "2026-11-10",
                        "f003d_0102": "",
                        "f004d_0102": None,
                        "f005d_0102": None,
                        "f006d_0102": None,
                    }
                ]
            }
        ).encode()


def _install_market(monkeypatch, close_for) -> tuple[object, object]:
    import urllib.request

    import yfinance as yf

    def _download(tickers, start=None, end=None, **_kwargs):
        symbols = [tickers] if isinstance(tickers, str) else list(tickers)
        return _price_frame(symbols, str(start), str(end), close_for)

    def _open(*_args, **_kwargs):
        return _CninfoResponse()

    fake_download = create_autospec(yf.download, side_effect=_download)
    fake_open = create_autospec(urllib.request.urlopen, side_effect=_open)
    monkeypatch.setattr(yf, "download", fake_download)
    monkeypatch.setattr(yf, "Ticker", create_autospec(yf.Ticker, side_effect=_CalendarTicker))
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    return fake_download, fake_open


def _flat_close(_symbol: str, _stamp) -> float:
    return 100.0


def _ningde_close(symbol: str, stamp) -> float:
    if symbol == "300750.SZ" and stamp.date() > dt.date(2026, 10, 31):
        return 110.0
    return 100.0


_EMPTY_RESEARCH = {
    "ok": True,
    "status": "ok",
    "error": None,
    "data": {"ok": True, "results": [], "result_count": 0, "_declared_sources": []},
}


def _fresh_empty(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
    assert tool_name == "research"
    assert isinstance(arguments, dict)
    assert timeout == 120
    return json.loads(json.dumps(_EMPTY_RESEARCH))


_RD_ORIGINALS: dict[int, tuple] = {}


def _install_research(monkeypatch, module, *, mode: str | None = "wrapper", responder=None):
    """Stub the research transport. Workers are serialized so the call log is exact."""
    monkeypatch.setattr(module, "AE_WORKERS", 1)
    rd = module._rd()
    # A test may install the stub twice; spec from the real functions, not the
    # stubs a previous install left on the module.
    originals = _RD_ORIGINALS.setdefault(
        id(rd),
        (rd.resolve_researchdata_transport, rd.call_bridge_jsonrpc),
    )
    fake_resolve = create_autospec(
        originals[0],
        return_value={"mode": mode, "error": None},
    )
    fake_call = create_autospec(
        originals[1],
        side_effect=responder or _fresh_empty,
    )
    monkeypatch.setattr(rd, "resolve_researchdata_transport", fake_resolve)
    monkeypatch.setattr(rd, "call_bridge_jsonrpc", fake_call)
    return fake_resolve, fake_call


def _collect(module, workspace: Path, budget: str | None = None) -> tuple[int, dict]:
    argv = ["--json", "collect", "--workspace-dir", str(workspace), "--end", END]
    if budget is not None:
        argv.extend(["--budget-seconds", budget])
    return _run(module, argv)


def _company(name: str = "宁德时代") -> dict:
    return next(item for item in _targets()["companies"] if item["name"] == name)


def _search_rows(rows: list[dict]) -> dict:
    return {
        "ok": True,
        "status": "ok",
        "results": rows,
        "result_count": len(rows),
        "_sources": ["sales-notes"],
    }


def _read_body(text: str) -> dict:
    return {
        "ok": True,
        "status": "ok",
        "data": {
            "structuredContent": {"text": text, "next_offset": None, "complete": True},
            "_declared_sources": ["sales-notes"],
        },
    }


def _summary_input(**extra: object) -> dict:
    payload = {
        "highlights": ["储能招标增加"],
        "next_earnings": ["11/9 比亚迪"],
        "unverified": ["某价格只有一家报价"],
        "publish_failure": "",
        "restyle_failure": "",
    }
    payload.update(extra)
    return payload


def test_frontmatter_registry_and_profile() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )

    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    description = meta["description"].strip()
    assert description == read_skill_description(SKILL_NAME)
    assert len(description) <= 120
    assert "触发词" not in description
    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert len(_DALTON_SKILL_SPECS) == 45
    assert PROFILE in REPORT_PROFILES
    body, _heading, rules = text.partition("## 撰写硬规则")
    assert text.count("## 撰写硬规则") == 1
    for phrase in BANNED_PROSE:
        assert phrase not in body
        assert phrase in rules
    assert rules.strip().endswith("沿用原有子任务权限与限制。")


def test_skill_tree_has_no_recipient() -> None:
    seen = False
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        seen = True
        text = path.read_text(encoding="utf-8")
        for banned in BANNED_RECIPIENTS:
            assert banned not in text, path.name
        assert _RECIPIENT_OC_RE.search(text) is None, path.name
    assert seen


def test_plan_window_and_tool_params(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, window = _run(
        module,
        ["--json", "window", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and window["ok"] is True
    assert window["saturday"] == END
    assert window["is_saturday"] is True
    assert window["timezone"] == "America/New_York"
    assert window["report_run_id"] == f"{PROFILE}-{END}"
    assert window["title"] == "新能源与中概股周度动态（10/31至11/7）"
    assert module.title_for(dt.date(2026, 11, 7), restyled=True) == "新能源与中概股周度动态（10/31至11/7，改写版）"
    assert window["window"]["start"] == "2026-10-31"
    assert window["window"]["date_from"] == "2026-10-31"
    assert window["window"]["date_to"] == "2026-11-08"
    assert window["window"]["web_date_after"] == "2026-10-31"
    assert window["window"]["web_date_before"] == "2026-11-08"
    assert window["window"]["calendar_until"] == "2026-11-22"
    _no_leak(window, workspace)

    code, plan = _run(
        module,
        ["--json", "plan", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and plan["ok"] is True
    assert plan["companies"] == 35
    assert plan["ae_queries"] == 81
    assert len(plan["alphaengine"]) == 81
    assert plan["industry_sections"] == [
        "中国锂电与储能",
        "海外锂电与储能",
        "中国新能源车",
        "海外新能源车",
        "燃油车与汽车出口",
        "AI 电力链",
    ]
    ningde_sell = next(item for item in plan["alphaengine"] if item["key"] == "宁德时代|sell")
    assert ningde_sell["call"] == {
        "route": "alphaengine",
        "operation": "search_library",
        "query": "宁德时代",
        "start": "2026-10-31",
        "end": "2026-11-08",
        "limit": 8,
        "params": {"sort": "relevance", "document_categories": SELL},
    }
    ningde_news = next(item for item in plan["alphaengine"] if item["key"] == "宁德时代|news")
    assert ningde_news["call"]["limit"] == 8
    assert ningde_news["call"]["params"]["document_categories"] == NEWS
    for job in plan["alphaengine"]:
        assert "date_from" not in job["call"]["params"]
        assert "date_to" not in job["call"]["params"]
        assert job["call"]["params"]["sort"] == "relevance"
        if job["kind"] == "industry":
            assert job["call"]["limit"] == 10
            assert "document_categories" not in job["call"]["params"]
        else:
            assert job["call"]["limit"] == 8
    assert plan["get_document"]["max"] == 6
    assert plan["get_document"]["call"]["operation"] == "get_document"
    assert plan["get_document"]["call"]["params"]["doc_id"] == "290001790954279_inst_134249"
    assert plan["get_document"]["call"]["params"]["content_field"] == "original_text"
    assert plan["get_document"]["call"]["params"]["offset"] == 0
    assert plan["get_document"]["call"]["params"]["max_chars"] == 30000
    internal = plan["internal"]
    assert internal["handled_by"] == "collect"
    assert internal["mail_sources"] == ["sales-notes", "tmtb", "funda-ai"]
    assert internal["search_limit"] == 100
    assert internal["per_company"] == 5
    assert internal["read"] == {
        "route": "all",
        "operation": "read",
        "params": {"offset": 0, "max_chars": 30000},
    }
    assert "fallback_routes" not in internal
    wiki = internal["wiki_list"]
    assert wiki["route"] == "company-wiki"
    assert wiki["operation"] == "list"
    assert wiki["limit"] == 100
    assert wiki["params"] == {"indexed_since": "2026-10-31", "media": "text"}
    ningde_internal = next(item for item in internal["companies"] if item["company"] == "宁德时代")
    assert ningde_internal == {"company": "宁德时代", "key": "宁德时代|internal"}
    assert len(internal["companies"]) == 35
    assert len(plan["web_search"]) == 46
    for item in plan["web_search"]:
        assert set(item["params"]) == {"query", "start_date", "end_date"}
        assert item["params"]["start_date"] == "2026-10-31"
        assert item["params"]["end_date"] == "2026-11-08"
    ningde_web = next(item for item in plan["web_search"] if item.get("company") == "宁德时代")
    assert ningde_web["params"]["query"] == "宁德时代"
    panasonic = next(item for item in plan["web_search"] if item.get("company") == "松下")
    assert panasonic["params"]["query"] == "Panasonic Energy"
    samsung = next(item for item in plan["web_search"] if item.get("company") == "三星SDI")
    assert samsung["params"]["query"] == "Samsung SDI"
    ge_web = next(item for item in plan["web_search"] if item.get("company") == "GE Vernova")
    assert ge_web["params"]["query"] == "GE Vernova"
    queries = [item["params"]["query"] for item in plan["web_search"]]
    assert any(any("\u4e00" <= char <= "\u9fff" for char in query) for query in queries)
    assert any(query.isascii() for query in queries)
    _no_leak(plan, workspace)


def test_prices_and_cninfo_use_autospec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    fake_download, fake_open = _install_market(monkeypatch, _ningde_close)
    code, prices = _run(
        module,
        ["--json", "prices", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and prices["ok"] is True
    assert prices["cached"] is False
    assert ["宁德时代", 10.0] in prices["big_moves_ge_8pct"]
    kwargs = fake_download.call_args.kwargs
    assert kwargs["auto_adjust"] is True
    assert kwargs["group_by"] == "ticker"
    assert kwargs["progress"] is False
    assert "threads" not in kwargs
    assert fake_download.call_args.args[0]
    assert "300750.SZ" in list(fake_download.call_args.args[0])
    code, again = _run(
        module,
        ["--json", "prices", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and again["cached"] is True
    assert fake_download.call_count == 1
    code, earnings = _run(
        module,
        ["--json", "earnings", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and earnings["ok"] is True
    assert fake_open.call_count == 18
    assert "getPrbookInfo" in fake_open.call_args_list[0].args[0].full_url
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/raw.json",
        {"宁德时代|internal": {"ok": True, "matched_docs": 0, "picks": [], "limit_hit": ["sales-notes"]}},
    )
    code, manifest = _run(
        module,
        [
            "--json",
            "assemble",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--raw",
            f"state/mark-weekly-news/runs/{END}/raw.json",
        ],
    )
    assert code == 0 and manifest["ok"] is True
    assert fake_download.call_count == 1
    assert fake_open.call_count == 18
    assert ["宁德时代", 10.0] in manifest["big_moves_ge_8pct"]
    evidence = "\n".join((workspace / relative).read_text(encoding="utf-8") for relative in manifest["evidence_files"])
    assert "巨潮预约" in evidence
    assert "检索满额：sales-notes" in evidence
    assert "上周稿没有「五、业绩日历」" in evidence
    assert "/Users/" not in evidence
    _no_leak(manifest, workspace)


def test_documented_commands_run_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _install_market(monkeypatch, _flat_close)
    _install_research(monkeypatch, module)
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    commands = _bash_commands(skill)
    replacements = {"<workspace>": str(workspace), "<reason>": "probe"}
    saturday = ""
    plan: dict = {}
    names: list[str] = []
    for command in commands:
        if saturday:
            replacements["<saturday>"] = saturday
        argv = _argv_from(command, replacements)
        name = argv[1]
        names.append(name)
        if name == "assemble":
            raw: dict = {}
            for job in plan["alphaengine"]:
                raw[job["key"]] = {
                    "hits": [
                        {
                            "doc_id": "310001530369877",
                            "date": "2026-11-01",
                            "src": "test",
                            "title": "hit",
                            "snippet": "s",
                        }
                    ]
                }
            for company in plan["internal"]["companies"]:
                raw[company["key"]] = {"matched_docs": 0, "picks": []}
            _write_json(workspace, _flag(argv, "--raw"), raw)
        if name == "check" and "restyled" not in argv:
            relative = f"state/mark-weekly-news/runs/{saturday}/draft.md"
            if not (workspace / relative).is_file():
                _write(workspace, relative, _passing_draft())
        if name == "save-doc":
            kind = _flag(argv, "--kind")
            document_id = ORIGINAL_DOC if kind == "original" else RESTYLED_DOC
            _write_json(workspace, _flag(argv, "--file"), {"ok": True, "document_id": document_id})
        if name == "restyle-apply":
            chunks = json.loads(
                (workspace / f"state/mark-weekly-news/runs/{saturday}/restyle/chunks.json").read_text(encoding="utf-8")
            )
            rewritten = {"chunks": [{"index": item["index"], "text": item["text"]} for item in chunks["chunks"]]}
            _write_json(workspace, _flag(argv, "--rewritten"), rewritten)
        if name == "summary":
            _write_json(
                workspace,
                _flag(argv, "--input"),
                {
                    "highlights": ["储能招标增加", "宁德时代订单增加", "中概地产销售持平"],
                    "next_earnings": ["11/16 宁德时代", "11/9 比亚迪"],
                    "unverified": ["某价格只有一家报价"],
                    "publish_failure": "",
                    "restyle_failure": "",
                },
            )
        code, payload = _run(module, argv)
        assert code == 0 and payload["ok"] is True, payload
        _no_leak(payload, workspace)
        if name == "window":
            saturday = payload["saturday"]
        if name == "plan":
            plan = payload
            assert payload["companies"] == 35
            assert payload["ae_queries"] == 81
        if name == "assemble":
            assert payload["ae_failed"] == 0
            assert payload["web_date_after"] == payload["window"]["web_date_after"]
        if name == "summary":
            assert "已覆盖 35/35 家；AlphaEngine 检索 81 次，失败 0 次。" in payload["text"]
            assert "改写版（推荐读这份）：<https://feishu.cn/docx/" in payload["text"]
            assert "原版：<https://feishu.cn/docx/" in payload["text"]
            assert payload["text"].index("11/9") < payload["text"].index("11/16")
    assert names == [
        "self-test",
        "window",
        "plan",
        "collect",
        "prices",
        "earnings",
        "assemble",
        "check",
        "doc-status",
        "save-doc",
        "restyle-segment",
        "restyle-apply",
        "check",
        "doc-status",
        "save-doc",
        "summary",
        "note-failure",
    ]


def test_check_pass_and_fail(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cfg = _targets()
    relative = f"state/mark-weekly-news/runs/{END}/draft.md"
    draft = _passing_draft(cfg)
    _write(workspace, relative, draft)
    code, passed = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and passed["ok"] is True
    assert passed["companies_found"] == "35/35"
    assert passed["style_warnings"] == {}
    assert passed["missing_h2"] == []

    beike = next(company for company in cfg["companies"] if company["name"] == "贝壳")
    _write(workspace, relative, draft.replace("\n".join(_company_block(beike)), "", 1))
    code, missing = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 1 and missing["ok"] is False
    assert "贝壳" in missing["missing_companies"]
    assert missing["companies_found"] == "34/35"

    renamed = draft.replace(
        "### 宁德时代（300750.SZ / 3750.HK）近一周 +0.1%",
        "### 300750.SZ / 3750.HK 近一周 +0.1%",
        1,
    )
    _write(workspace, relative, renamed)
    code, untitled = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 1 and untitled["ok"] is False
    assert "宁德时代" in untitled["missing_companies"]

    catl = draft.replace(
        "### 宁德时代（300750.SZ / 3750.HK）近一周 +0.1%",
        "### CATL（300750.SZ / 3750.HK）近一周 +0.1%",
        1,
    )
    _write(workspace, relative, catl)
    code, alias = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and alias["ok"] is True
    assert alias["companies_found"] == "35/35"

    _write(workspace, relative, draft.replace("## 七、未核实与检索失败", "未核实与检索失败", 1))
    code, no_h2 = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 1 and no_h2["ok"] is False
    assert "未核实" in no_h2["missing_h2"]

    _write(workspace, relative, draft + "\n这里改成正面陈述。不能只留口头说法。\n")
    code, styled = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and styled["ok"] is True
    assert styled["style_warnings"]["不能"] >= 1

    _write(workspace, relative, draft.replace("补充说明。", "价格 $1 补充说明。", 1))
    code, dollars = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 1 and dollars["ok"] is False
    assert dollars["dollar_signs"] >= 1


def test_restyle_keeps_invalid_chunks(tmp_path: Path) -> None:
    module = _load()
    src = "## 标题\n- 增长 10%【公告】[IR](LINK_1)\n"
    assert module.validate_chunk(src, src.replace("10", "12"))
    assert module.validate_chunk(src, src.replace("【公告】", ""))
    assert module.validate_chunk(src, src.replace("LINK_1", "LINK_9"))
    assert module.validate_chunk(src, src + "- 再写一条\n")
    assert module.validate_chunk(src, src.replace("增长", "上升")) == []
    signed = "- 增长 +10%。\n"
    assert module.validate_chunk(signed, signed.replace("+10%", "-10%"))
    assert module.validate_chunk("- 增长 -10%。\n", "- 增长 \u221210%。\n") == []
    assert module.validate_chunk("- 增长 -10%。\n", "- 增长 \uff0d10%。\n") == []
    named = "- 宁德时代本周订单 10%。\n"
    assert any("公司名" in item for item in module.validate_chunk(named, named.replace("宁德时代", "比亚迪")))
    dated = "事件 [10/31] 对照 11 与 7。\n"
    swapped = "事件 [11/7] 对照 10 与 31。\n"
    assert any("日期" in item for item in module.validate_chunk(dated, swapped))
    with_doc = src + "- 文档 310001530369877\n"
    assert module.validate_chunk(with_doc, with_doc.replace("310001530369877", "310001530369878"))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    draft = _passing_draft().replace(
        "## 一、本周结论\n",
        "## 一、本周结论\n- 增长 10%【公告】[来源](https://example.com/a)\n对比 0.1 个百分点。\n",
        1,
    )
    _write(workspace, f"state/mark-weekly-news/runs/{END}/draft.md", draft)
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/doc-original.json",
        {"ok": True, "document_id": ORIGINAL_DOC},
    )
    code, segment = _run(
        module,
        ["--json", "restyle-segment", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and segment["restyle"] is True
    stored = json.loads(
        (workspace / f"state/mark-weekly-news/runs/{END}/restyle/chunks.json").read_text(encoding="utf-8")
    )
    chunks = stored["chunks"]
    bad = next(item for item in chunks if "0.1" in item["text"] and not item["verbatim"])
    verbatim = next(item for item in chunks if item["verbatim"])
    good = next(
        item
        for item in chunks
        if "本周没有新增重大订单" in item["text"] and item["index"] != bad["index"] and not item["verbatim"]
    )
    rewritten = []
    for item in chunks:
        text = item["text"]
        if item["index"] == bad["index"]:
            text = text.replace("0.1", "0.2", 1)
        elif item["index"] == verbatim["index"]:
            text = text + "\nTAMPER"
        elif item["index"] == good["index"]:
            text = "===BEGIN===\n" + text.replace("本周没有新增重大订单", "本周没有新的重大订单", 1) + "\n===END==="
        rewritten.append({"index": item["index"], "text": text})
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/restyle/rewritten.json",
        {"chunks": rewritten},
    )
    code, applied = _run(
        module,
        [
            "--json",
            "restyle-apply",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--rewritten",
            f"state/mark-weekly-news/runs/{END}/restyle/rewritten.json",
        ],
    )
    assert code == 0 and applied["ok"] is True
    assert applied["prices_equal"] is True
    assert applied["headings_equal"] is True
    assert applied["urls_equal"] is True
    assert applied["leftover_placeholders"] == []
    assert bad["index"] in applied["chunks_kept_original"]
    assert verbatim["index"] not in applied["chunks_kept_original"]
    body = (workspace / applied["draft"]).read_text(encoding="utf-8")
    assert "0.2" not in body
    assert "TAMPER" not in body
    assert "本周没有新的重大订单" in body
    assert "https://example.com/a" in body
    assert "LINK_" not in body
    _no_leak(applied, workspace)


def test_doc_record_is_not_recreated(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    code, status = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(workspace), "--end", END, "--kind", "original"],
    )
    assert code == 0 and status["create"] is True
    raw = json.dumps({"ok": True, "document_id": ORIGINAL_DOC}, ensure_ascii=False).encode()
    _write(workspace, f"state/mark-weekly-news/runs/{END}/feishu-return.json", raw.decode())
    # Keep the exact bytes the command will read.
    (workspace / f"state/mark-weekly-news/runs/{END}/feishu-return.json").write_bytes(raw)
    code, saved = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--kind",
            "original",
            "--file",
            f"state/mark-weekly-news/runs/{END}/feishu-return.json",
        ],
    )
    assert code == 0 and saved["ok"] is True
    assert saved["skipped"] is False and saved["create"] is False
    assert saved["url"] == f"https://feishu.cn/docx/{ORIGINAL_DOC}"
    lock = workspace / f"state/mark-weekly-news/runs/{END}/doc-original.json"
    assert lock.read_bytes() == raw
    code, after = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(workspace), "--end", END, "--kind", "original"],
    )
    assert code == 0 and after["create"] is False
    assert after["url"] == f"https://feishu.cn/docx/{ORIGINAL_DOC}"
    other = json.dumps({"ok": True, "document_id": RESTYLED_DOC}).encode()
    (workspace / f"state/mark-weekly-news/runs/{END}/feishu-return-other.json").write_bytes(other)
    code, skipped = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--kind",
            "original",
            "--file",
            f"state/mark-weekly-news/runs/{END}/feishu-return-other.json",
        ],
    )
    assert code == 0 and skipped["ok"] is True
    assert skipped["skipped"] is True and skipped["create"] is False
    assert lock.read_bytes() == raw
    assert (
        module.feishu_doc_url({"node_token": RESTYLED_DOC, "document_id": ORIGINAL_DOC})
        == f"https://feishu.cn/wiki/{RESTYLED_DOC}"
    )
    assert module.feishu_doc_url({"document_id": "abc", "ok": True}) is None
    assert module.feishu_doc_url({"ok": False, "document_id": ORIGINAL_DOC}) == f"https://feishu.cn/docx/{ORIGINAL_DOC}"

    history = workspace / "state/mark-weekly-news/history.json"
    history.write_text("{", encoding="utf-8")
    bad = json.dumps({"ok": False, "error": "call_failed", "document_id": ORIGINAL_DOC}).encode()
    (workspace / f"state/mark-weekly-news/runs/{END}/feishu-return-restyled.json").write_bytes(bad)
    code, failed = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--kind",
            "restyled",
            "--file",
            f"state/mark-weekly-news/runs/{END}/feishu-return-restyled.json",
        ],
    )
    assert code == 0 and failed["stored"] is True
    assert failed["draft_copied"] is False
    assert failed["url"] == f"https://feishu.cn/docx/{ORIGINAL_DOC}"
    restyled_lock = workspace / f"state/mark-weekly-news/runs/{END}/doc-restyled.json"
    assert restyled_lock.read_bytes() == bad
    assert history.read_text(encoding="utf-8") == "{"
    restyled_lock.unlink()
    good = json.dumps({"ok": True, "document_id": RESTYLED_DOC}, ensure_ascii=False).encode()
    (workspace / f"state/mark-weekly-news/runs/{END}/feishu-return-restyled-ok.json").write_bytes(good)
    code, kept_history = _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--kind",
            "restyled",
            "--file",
            f"state/mark-weekly-news/runs/{END}/feishu-return-restyled-ok.json",
        ],
    )
    assert code == 0 and kept_history["ok"] is True
    assert kept_history["stored"] is True
    assert kept_history["history_updated"] is False
    assert kept_history["history_error"] == "history_unreadable"
    assert kept_history["draft_copied"] is False
    assert history.read_text(encoding="utf-8") == "{"
    assert restyled_lock.read_bytes() == good


def test_paths_do_not_escape(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        module.workspace_file(workspace, "../secret.json")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        module.workspace_file(workspace, "/etc/passwd")
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, workspace / "state")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        module.workspace_file(workspace, "state/mark-weekly-news/history.json")

    workspace2 = tmp_path / "ws2"
    workspace2.mkdir()
    (workspace2 / "real.md").write_text("x", encoding="utf-8")
    os.symlink(workspace2 / "real.md", workspace2 / "link.md")
    with pytest.raises(ValueError, match="refusing symlink"):
        module.workspace_file(workspace2, "link.md")
    code, payload = _run(
        module,
        [
            "--json",
            "assemble",
            "--workspace-dir",
            str(workspace2),
            "--end",
            END,
            "--raw",
            "../secret.json",
        ],
    )
    assert code == 1 and payload["ok"] is False
    assert not (tmp_path / "secret.json").exists()
    assert not (outside / "secret.json").exists()
    assert "/Users/" not in json.dumps(payload, ensure_ascii=False)
    link_ws = tmp_path / "link-ws"
    os.symlink(workspace2, link_ws)
    code, linked = _run(
        module,
        ["--json", "window", "--workspace-dir", str(link_ws), "--end", END],
    )
    assert code == 1 and linked["ok"] is False
    assert "path must stay inside the workspace" in linked["error"]
    assert "/Users/" not in linked["error"]


def test_summary_format(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write(workspace, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/manifest.json",
        {"ae_queries": 81, "ae_failed": 0, "ok": True},
    )
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/doc-original.json",
        {"ok": True, "document_id": ORIGINAL_DOC},
    )
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/doc-restyled.json",
        {"ok": True, "document_id": RESTYLED_DOC},
    )
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/restyle/report.json",
        {"chunks_kept_original": [1]},
    )
    _write_json(
        workspace,
        f"state/mark-weekly-news/runs/{END}/summary-input.json",
        {
            "highlights": ["储能招标增加", "订单增加", "销售持平"],
            "next_earnings": ["11/16 宁德时代", "11/9 比亚迪"],
            "unverified": ["某价格只有一家报价"],
            "publish_failure": "建文档超时",
        },
    )
    code, payload = _run(
        module,
        [
            "--json",
            "summary",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--input",
            f"state/mark-weekly-news/runs/{END}/summary-input.json",
        ],
    )
    assert code == 0 and payload["ok"] is True
    text = payload["text"]
    assert text.splitlines()[0].startswith("飞书发布失败：建文档超时")
    assert f"改写版（推荐读这份）：<https://feishu.cn/docx/{RESTYLED_DOC}>" in text
    assert f"原版：<https://feishu.cn/docx/{ORIGINAL_DOC}>" in text
    assert "已覆盖 35/35 家；AlphaEngine 检索 81 次，失败 0 次。" in text
    assert "第 2 段改写没通过校验，保留原文" in text
    assert text.index("11/9") < text.index("11/16")
    assert "/Users/" not in text
    _no_leak(payload, workspace)


def test_prev_run_reads_last_draft(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_json(
        workspace,
        "state/mark-weekly-news/history.json",
        {
            "runs": [
                {"end": "2026-11-07", "draft": "state/mark-weekly-news/drafts/2026-11-07.md", "title": "later"},
                {"end": "2026-10-31", "draft": "state/mark-weekly-news/drafts/2026-10-31.md", "title": "earlier"},
            ]
        },
    )
    found = module.previous_run(workspace, dt.date(2026, 11, 14))
    assert found["end"] == "2026-11-07"
    assert found["title"] == "later"

    other = tmp_path / "drafts-only"
    other.mkdir()
    _write(other, "state/mark-weekly-news/drafts/2026-10-31.md", "old")
    _write(other, "state/mark-weekly-news/drafts/2026-11-07.md", "new")
    _write(other, "state/mark-weekly-news/drafts/2026-11-07.restyled.md", "styled")
    found = module.previous_run(other, dt.date(2026, 11, 14))
    assert found == {"end": "2026-11-07", "draft": "state/mark-weekly-news/drafts/2026-11-07.md"}

    broken = tmp_path / "broken"
    broken.mkdir()
    history = broken / "state/mark-weekly-news/history.json"
    history.parent.mkdir(parents=True)
    history.write_text("{", encoding="utf-8")
    _write(broken, "state/mark-weekly-news/drafts/2026-11-07.md", "fallback")
    found = module.previous_run(broken, dt.date(2026, 11, 14))
    assert found["end"] == "2026-11-07"
    assert history.read_text(encoding="utf-8") == "{"


def _is_ningde_sell(arguments: dict) -> bool:
    categories = (arguments.get("params") or {}).get("document_categories") or []
    return (
        arguments.get("route") == "alphaengine"
        and arguments.get("operation") == "search_library"
        and arguments.get("query") == "宁德时代"
        and "sell_side_report" in categories
    )


def test_collect_raw_retries_budget_and_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    assert module.AE_WORKERS == 4
    workspace = tmp_path / "success"
    workspace.mkdir()
    seen: list[dict] = []

    def _record(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        assert tool_name == "research"
        assert timeout == 120
        seen.append(arguments)
        return json.loads(json.dumps(_EMPTY_RESEARCH))

    _install_research(monkeypatch, module, responder=_record)
    code, payload = _collect(module, workspace)
    assert code == 0 and payload["ok"] is True
    assert payload["remaining_keys"] == 0
    assert payload["exhausted_keys"] == 0
    assert payload["failed"] == 0
    assert len(seen) == 187
    assert len([item for item in seen if item.get("route") == "alphaengine"]) == 81
    wiki_calls = [item for item in seen if item.get("operation") == "list"]
    assert len(wiki_calls) == 1
    assert wiki_calls[0]["route"] == "company-wiki"
    assert wiki_calls[0]["limit"] == 100
    assert len([item for item in seen if item.get("route") in {"sales-notes", "tmtb", "funda-ai"}]) == 105
    raw_path = workspace / payload["raw"]
    stored = json.loads(raw_path.read_text(encoding="utf-8"))
    assert len(stored) == 117
    assert all(isinstance(item, dict) and item.get("ok") is not False for item in stored.values())
    assert "/Users/" not in raw_path.read_text(encoding="utf-8")
    _no_leak(payload, workspace)

    retry_ws = tmp_path / "retry"
    retry_ws.mkdir()
    retry_seen: list[dict] = []
    fail = {"sell": True}

    def _fail_sell(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        assert tool_name == "research"
        assert timeout == 120
        retry_seen.append(arguments)
        if fail["sell"] and _is_ningde_sell(arguments):
            return {"ok": False, "status": "error", "error": "ae_down", "data": None}
        return json.loads(json.dumps(_EMPTY_RESEARCH))

    _install_research(monkeypatch, module, responder=_fail_sell)
    code, first = _collect(module, retry_ws)
    assert code == 1 and first["ok"] is False
    assert first["remaining_keys"] == 1
    assert len(retry_seen) == 187
    raw = json.loads((retry_ws / first["raw"]).read_text(encoding="utf-8"))
    assert [key for key, value in raw.items() if value.get("ok") is False] == ["宁德时代|sell"]
    assert raw["宁德时代|sell"] == {"ok": False, "error": "ae_down"}
    attempts = json.loads(
        (retry_ws / f"state/mark-weekly-news/runs/{END}/collect-state.json").read_text(encoding="utf-8")
    )
    assert attempts["attempts"]["宁德时代|sell"] == 1
    fail["sell"] = False
    marked = len(retry_seen)
    code, second = _collect(module, retry_ws)
    assert code == 0 and second["ok"] is True
    assert second["remaining_keys"] == 0
    assert second["exhausted_keys"] == 0
    assert len(retry_seen) == marked + 1
    assert _is_ningde_sell(retry_seen[-1])
    raw = json.loads((retry_ws / second["raw"]).read_text(encoding="utf-8"))
    assert raw["宁德时代|sell"]["ok"] is True
    _no_leak(second, retry_ws)

    exhaust_ws = tmp_path / "exhaust"
    exhaust_ws.mkdir()
    exhaust_seen: list[dict] = []

    def _always_fail_sell(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        assert tool_name == "research"
        assert timeout == 120
        exhaust_seen.append(arguments)
        if _is_ningde_sell(arguments):
            return {"ok": False, "status": "error", "error": "ae_down", "data": None}
        return json.loads(json.dumps(_EMPTY_RESEARCH))

    _install_research(monkeypatch, module, responder=_always_fail_sell)
    code, round1 = _collect(module, exhaust_ws)
    assert code == 1 and round1["remaining_keys"] == 1
    assert round1["exhausted_keys"] == 0
    assert round1["failed"] == 1
    state_path = exhaust_ws / f"state/mark-weekly-news/runs/{END}/collect-state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["attempts"]["宁德时代|sell"] == 1
    marked = len(exhaust_seen)
    code, round2 = _collect(module, exhaust_ws)
    assert code == 1 and round2["ok"] is False
    assert round2["remaining_keys"] == 0
    assert round2["exhausted_keys"] == 1
    assert round2["failed"] == 1
    assert len(exhaust_seen) == marked + 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["attempts"]["宁德时代|sell"] == 2
    marked = len(exhaust_seen)
    code, round3 = _collect(module, exhaust_ws)
    assert code == 1 and round3["ok"] is False
    assert round3["remaining_keys"] == 0
    assert round3["exhausted_keys"] == 1
    assert len(exhaust_seen) == marked
    _no_leak(round3, exhaust_ws)

    budget_ws = tmp_path / "budget"
    budget_ws.mkdir()
    budget_seen: list[dict] = []

    def _budget(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        budget_seen.append(arguments)
        return _fresh_empty(tool_name, arguments, timeout)

    _install_research(monkeypatch, module, responder=_budget)
    code, stopped = _collect(module, budget_ws, budget="0")
    assert code == 1 and stopped["ok"] is False
    assert stopped["remaining_keys"] == 117
    assert stopped["completed"] == 0
    assert stopped["failed"] == 0
    assert stopped["exhausted_keys"] == 0
    assert budget_seen == []
    assert not (budget_ws / f"state/mark-weekly-news/runs/{END}/raw.json").exists()
    assert not (budget_ws / f"state/mark-weekly-news/runs/{END}/collect-state.json").exists()
    _no_leak(stopped, budget_ws)

    blocked_ws = tmp_path / "blocked"
    blocked_ws.mkdir()

    def _refuse(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        raise AssertionError(tool_name or arguments or timeout)

    _resolve, fake_call = _install_research(monkeypatch, module, mode=None, responder=_refuse)
    code, blocked = _collect(module, blocked_ws)
    assert code == 1 and blocked["ok"] is False
    assert blocked["error"] == "researchdata_transport_unconfigured"
    assert blocked["remaining_keys"] == 117
    assert blocked["exhausted_keys"] == 0
    assert blocked["failed"] == 0
    assert fake_call.call_count == 0
    assert _resolve.call_count == 1
    assert json.dumps(blocked).count("researchdata_transport_unconfigured") == 1
    assert not (blocked_ws / f"state/mark-weekly-news/runs/{END}/raw.json").exists()
    _no_leak(blocked, blocked_ws)


def test_envelopes_and_wiki_excerpts(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    ae = {
        "ok": True,
        "data": {
            "structuredContent": {
                "results": [
                    {
                        "doc_id": "290001790954279_inst_134249",
                        "title": "CATL",
                        "title_cn": "宁德时代点评",
                        "publish_time": "2026-11-02 09:00:00",
                        "type_show_name": "卖方点评",
                    }
                ],
                "cursor": None,
                "has_more": False,
            },
            "_declared_sources": ["alphaengine"],
        },
    }
    normalized = module.normalize_ae(ae)
    assert normalized["error"] is None
    assert len(normalized["hits"]) == 1
    assert normalized["hits"][0]["doc_id"] == "290001790954279_inst_134249"
    assert normalized["hits"][0]["title"] == "CATL"
    assert normalized["hits"][0]["src"] == "卖方点评"
    assert normalized["hits"][0]["date"] == "2026-11-02"
    assert module.normalize_ae(None)["error"] == "未执行"
    assert module.normalize_ae({"ok": True, "data": {"_declared_sources": ["alphaengine"]}}) == {
        "hits": [],
        "error": None,
    }

    notes = {
        "ok": True,
        "status": "ok",
        "data": {
            "structuredContent": {
                "results": [
                    {
                        "document_id": "mail:9",
                        "title": "订单",
                        "date": "2026-11-02",
                        "source": "sales-notes",
                        "excerpt": "宁德时代订单",
                    }
                ]
            },
            "_declared_sources": ["sales-notes"],
        },
        "result_count": 1,
    }
    parsed = module.normalize_notes(notes)
    assert parsed["error"] is None
    assert parsed["matched_docs"] == 1
    assert parsed["picks"][0]["document_id"] == "mail:9"
    assert parsed["picks"][0]["excerpt"] == "宁德时代订单"
    assert module.normalize_notes("x")["error"] == "未执行"

    full = [
        {"document_id": f"wiki:{index}", "title": "x", "company": "其他"} for index in range(100)
    ]
    listed = module.wiki_record_from_call(
        {
            "ok": True,
            "data": {"structuredContent": {"results": full}, "_declared_sources": ["company-wiki"]},
        }
    )
    assert listed["ok"] is True
    assert len(listed["results"]) == 100
    assert module.WIKI_FULL_WARNING in listed["warnings"]
    short = module.wiki_record_from_call({"ok": True, "results": full[:99], "result_count": 99})
    assert short["warnings"] == []

    company = _company()
    window = {"date_from": "2026-10-31", "date_to": "2026-11-08"}
    calls: list[dict] = []
    own = module._own_tokens(company)
    assert {"300750.SZ", "300750", "3750.HK"} <= own
    byd = module._own_tokens(_company("比亚迪"))
    assert {"1211.HK", "1211"} <= byd

    def _caller(arguments: dict) -> dict:
        calls.append(arguments)
        if arguments.get("operation") == "read":
            document = arguments["document"]
            if document == "wiki:once":
                return _read_body("只提一次宁德时代。")
            if document == "wiki:own":
                return _read_body("目录页。")
            if document == "wiki:twice":
                return _read_body("宁德时代供应宁德时代。")
            if document == "wiki:code":
                return _read_body("正文里宁德时代只出现一次。")
            return _read_body("邮件里写了宁德时代本周订单。")
        if arguments.get("route") == "sales-notes":
            return _search_rows(
                [
                    {
                        "document_id": "mail:1001",
                        "title": "宁德时代订单",
                        "date": "2026-11-03",
                        "original_source": "卖方",
                    },
                    {"document_id": "image:skip", "title": "图", "date": "2026-11-03"},
                ]
            )
        return _search_rows([])

    wiki_docs = [
        {"document_id": "wiki:own", "company": "宁德时代", "title": "本公司目录", "date": "2026-11-01"},
        {"document_id": "wiki:once", "company": "比亚迪", "title": "旁支", "date": "2026-11-01"},
        {"document_id": "wiki:twice", "company": "行业", "title": "产业链", "date": "2026-11-04"},
        {"document_id": "wiki:code", "company": "300750.SZ", "title": "代码页", "date": "2026-11-02"},
    ]
    cache: dict[str, str] = {}
    caller = create_autospec(module._call_research, side_effect=_caller)
    scanned = module.scan_company(company, wiki_docs, window, caller, cache)
    assert scanned["ok"] is True
    assert scanned.get("partial") is not True
    assert [item["document_id"] for item in scanned["picks"]] == ["mail:1001", "wiki:twice", "wiki:code", "wiki:own"]
    assert "宁德时代" in scanned["picks"][0]["excerpt"]
    assert scanned["picks"][0]["source"] == "sales-notes"
    reads = [item["document"] for item in calls if item.get("operation") == "read"]
    assert reads == ["mail:1001", "wiki:own", "wiki:once", "wiki:twice", "wiki:code"]
    read_call = next(item for item in calls if item.get("operation") == "read")
    assert read_call["route"] == "all"
    assert read_call["params"] == {"offset": 0, "max_chars": 30000}
    search_call = next(item for item in calls if item.get("route") == "sales-notes")
    assert search_call["operation"] == "search"
    assert search_call["start"] == "2026-10-31"
    assert search_call["end"] == "2026-11-08"
    assert search_call["limit"] == 100
    assert search_call["params"] == {"search_mode": "keyword", "media": "text"}
    assert "宁德时代" in search_call["query"]
    before = len(calls)
    module.scan_company(company, wiki_docs, window, caller, cache)
    assert not any(item.get("operation") == "read" for item in calls[before:])

    def _raise_read(arguments: dict) -> dict:
        if arguments.get("operation") == "read":
            raise RuntimeError("read failed")
        if arguments.get("route") == "sales-notes":
            return _search_rows([{"document_id": "mail:bad", "title": "x", "date": "2026-11-01"}])
        return _search_rows([])

    failed_cache: dict[str, str] = {}
    failed_read = module.scan_company(
        company,
        [{"document_id": "wiki:own", "company": "宁德时代", "title": "目录", "date": "2026-11-01"}],
        window,
        create_autospec(module._call_research, side_effect=_raise_read),
        failed_cache,
    )
    assert failed_read["ok"] is True
    assert failed_read["partial"] is True
    assert failed_read["picks"] == []
    assert {item["document_id"] for item in failed_read["read_errors"]} == {"mail:bad", "wiki:own"}
    assert all(item["error"] == "RuntimeError" for item in failed_read["read_errors"])
    assert failed_cache == {}

    def _mail_down(arguments: dict) -> dict:
        if arguments.get("operation") == "read":
            return _read_body("目录页。")
        return {"ok": False, "status": "error", "error": "timeout"}

    down = module.scan_company(
        company,
        wiki_docs,
        window,
        create_autospec(module._call_research, side_effect=_mail_down),
        {},
    )
    assert down["ok"] is True
    assert down["partial"] is True
    assert any("all mail sources failed" in item for item in down["errors"])

    monkeypatch.setattr(module, "SEARCH_LIMIT", 1)

    def _full(arguments: dict) -> dict:
        if arguments.get("operation") == "read":
            return _read_body("宁德时代。")
        return _search_rows([{"document_id": "mail:9", "title": "宁德时代", "date": "2026-11-02"}])

    capped = module.scan_company(
        company,
        [],
        window,
        create_autospec(module._call_research, side_effect=_full),
        {},
    )
    assert capped["ok"] is True
    assert capped.get("partial") is not True
    assert capped["limit_hit"] == ["sales-notes", "tmtb", "funda-ai"]
    assert capped["matched_docs"] == 1


def test_collect_stops_when_budget_runs_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    workspace = tmp_path / "mid"
    workspace.mkdir()
    tick = {"t": 0.0}
    seen: list[dict] = []

    def _advance(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        assert tick["t"] < 100
        tick["t"] += 40
        seen.append(arguments)
        return _fresh_empty(tool_name, arguments, timeout)

    clock = create_autospec(time.monotonic, side_effect=lambda: tick["t"])
    monkeypatch.setattr(module, "monotonic_now", clock)
    _install_research(monkeypatch, module, responder=_advance)
    code, payload = _collect(module, workspace, budget="100")
    assert code == 1 and payload["ok"] is False
    assert payload["failed"] == 0
    assert payload["exhausted_keys"] == 0
    assert payload["completed"] == 3
    assert payload["remaining_keys"] == 114
    assert len(seen) == 3
    assert clock.call_count > 0
    plan = module.build_plan(dt.date.fromisoformat(END))
    expected = [job["key"] for job in plan["alphaengine"][:3]]
    stored = json.loads((workspace / payload["raw"]).read_text(encoding="utf-8"))
    assert list(stored) == expected
    assert plan["alphaengine"][3]["key"] not in stored
    assert not (workspace / f"state/mark-weekly-news/runs/{END}/collect-state.json").exists()
    _no_leak(payload, workspace)


def test_partial_internal_retries_until_attempt_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    end = dt.date.fromisoformat(END)
    plan = module.build_plan(end)
    raw: dict = {}
    for job in plan["alphaengine"]:
        raw[job["key"]] = {"ok": True, "data": {"results": []}}
    raw[module.WIKI_LIST_KEY] = {"ok": True, "results": [], "warnings": []}
    for company in plan["internal"]["companies"]:
        raw[company["key"]] = {"ok": True, "matched_docs": 0, "picks": []}
    raw["宁德时代|internal"] = {
        "ok": True,
        "partial": True,
        "errors": ["read"],
        "matched_docs": 0,
        "picks": [],
    }

    retry_ws = tmp_path / "retry-partial"
    retry_ws.mkdir()
    _write_json(retry_ws, f"state/mark-weekly-news/runs/{END}/raw.json", raw)
    _write_json(
        retry_ws,
        f"state/mark-weekly-news/runs/{END}/collect-state.json",
        {"attempts": {"宁德时代|internal": 1}},
    )
    seen: list[dict] = []

    def _record(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        seen.append(arguments)
        return _fresh_empty(tool_name, arguments, timeout)

    _install_research(monkeypatch, module, responder=_record)
    code, payload = _collect(module, retry_ws)
    assert code == 0 and payload["ok"] is True
    assert payload["remaining_keys"] == 0
    assert payload["exhausted_keys"] == 0
    assert len(seen) == 3
    assert {item["route"] for item in seen} == {"sales-notes", "tmtb", "funda-ai"}
    stored = json.loads((retry_ws / payload["raw"]).read_text(encoding="utf-8"))
    assert stored["宁德时代|internal"]["ok"] is True
    assert stored["宁德时代|internal"].get("partial") is not True

    capped_ws = tmp_path / "capped-partial"
    capped_ws.mkdir()
    _write_json(capped_ws, f"state/mark-weekly-news/runs/{END}/raw.json", raw)
    _write_json(
        capped_ws,
        f"state/mark-weekly-news/runs/{END}/collect-state.json",
        {"attempts": {"宁德时代|internal": 2}},
    )
    capped_seen: list[dict] = []

    def _refuse(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        capped_seen.append(arguments)
        return _fresh_empty(tool_name, arguments, timeout)

    _install_research(monkeypatch, module, responder=_refuse)
    code, capped = _collect(module, capped_ws)
    assert code == 1 and capped["ok"] is False
    assert capped["remaining_keys"] == 0
    assert capped["exhausted_keys"] == 1
    assert capped["failed"] == 0
    assert capped_seen == []


def test_price_download_failure_keeps_the_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    import yfinance as yf

    boom = create_autospec(yf.download, side_effect=RuntimeError("disk /Users/secret"))
    monkeypatch.setattr(yf, "download", boom)
    code, prices = _run(
        module,
        ["--json", "prices", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and prices["ok"] is True
    assert prices["error"] == "RuntimeError"
    assert prices["big_moves_ge_8pct"] == []
    assert prices["cached"] is False
    stored = json.loads((workspace / prices["path"]).read_text(encoding="utf-8"))
    assert stored["error"] == "RuntimeError"
    assert stored["rows"]["300750.SZ"]["error"] == "RuntimeError"
    assert stored["rows"]["^GSPC"]["error"] == "RuntimeError"
    assert "/Users/" not in json.dumps(stored)
    _no_leak(prices, workspace)
    assert module.fmt_earn({"yf_error": "RuntimeError"}) == "yfinance 日历查询失败：RuntimeError"
    code, again = _run(
        module,
        ["--json", "prices", "--workspace-dir", str(workspace), "--end", END],
    )
    assert code == 0 and again["ok"] is True and again["cached"] is True
    assert again["error"] == "RuntimeError"
    assert boom.call_count == 1


def test_partial_internal_is_written_into_evidence(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run = f"state/mark-weekly-news/runs/{END}"
    _write_json(workspace, f"{run}/prices.json", {"300750.SZ": {"error": "no data"}})
    _write_json(workspace, f"{run}/earnings.json", {"宁德时代": {"name": "宁德时代", "yf_error": "RuntimeError"}})
    _write_json(
        workspace,
        f"{run}/raw.json",
        {
            "宁德时代|internal": {
                "ok": True,
                "partial": True,
                "errors": ["all mail sources failed: sales-notes timeout"],
                "warnings": ["正文有一页没读到"],
                "matched_docs": 0,
                "picks": [],
            }
        },
    )
    code, manifest = _run(
        module,
        ["--json", "assemble", "--workspace-dir", str(workspace), "--end", END, "--raw", f"{run}/raw.json"],
    )
    assert code == 0 and manifest["ok"] is True
    evidence = "\n".join((workspace / relative).read_text(encoding="utf-8") for relative in manifest["evidence_files"])
    assert "内部库检索不完整：" in evidence
    assert "all mail sources failed: sales-notes timeout" in evidence
    assert "yfinance 日历查询失败：RuntimeError" in evidence
    assert any(
        item.get("stage") == "internal" and "内部库检索不完整" in str(item.get("error"))
        for item in manifest["errors"]
    )
    _no_leak(manifest, workspace)


def _seed_prices_and_earnings(workspace: Path) -> None:
    run = f"state/mark-weekly-news/runs/{END}"
    _write_json(workspace, f"{run}/prices.json", {"300750.SZ": {"error": "no data"}})
    _write_json(workspace, f"{run}/earnings.json", {})


def test_wiki_list_failure_is_written_after_two_attempts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    end = dt.date.fromisoformat(END)
    plan = module.build_plan(end)
    raw = {job["key"]: {"ok": True, "data": {"results": []}} for job in plan["alphaengine"]}
    run = f"state/mark-weekly-news/runs/{END}"
    _write_json(workspace, f"{run}/raw.json", raw)
    seen: list[dict] = []

    def _fail_list(tool_name: str, arguments: dict, timeout: int = 120) -> dict:
        assert tool_name == "research"
        assert timeout == 120
        seen.append(arguments)
        if arguments.get("operation") == "list":
            return {"ok": False, "status": "error", "error": "wiki_down", "data": None}
        return _fresh_empty(tool_name, arguments, timeout)

    _install_research(monkeypatch, module, responder=_fail_list)
    code, first = _collect(module, workspace)
    assert code == 1 and first["remaining_keys"] == 36
    assert first["exhausted_keys"] == 0
    state_path = workspace / f"{run}/collect-state.json"
    assert json.loads(state_path.read_text(encoding="utf-8"))["attempts"][module.WIKI_LIST_KEY] == 1
    listed = len(seen)
    code, second = _collect(module, workspace)
    assert code == 1 and second["remaining_keys"] == 0
    assert second["exhausted_keys"] == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["attempts"][module.WIKI_LIST_KEY] == 2
    assert len([item for item in seen[listed:] if item.get("operation") == "list"]) == 1
    assert len([item for item in seen[listed:] if item.get("operation") == "search"]) == 105
    stored = json.loads((workspace / f"{run}/raw.json").read_text(encoding="utf-8"))
    assert stored["宁德时代|internal"]["ok"] is True
    _seed_prices_and_earnings(workspace)
    code, manifest = _run(
        module,
        ["--json", "assemble", "--workspace-dir", str(workspace), "--end", END, "--raw", f"{run}/raw.json"],
    )
    sentence = "company-wiki 新入库列表获取失败：wiki_down，各公司内部库结果缺少维基来源"
    assert code == 0 and manifest["ok"] is True
    battery = next(item for item in manifest["evidence_files"] if item.endswith("/battery.md"))
    text = (workspace / battery).read_text(encoding="utf-8")
    assert text.count(sentence) == 1
    assert text.index(sentence) < text.index("## 宁德时代")
    assert any(
        item.get("stage") == "internal" and item.get("key") == module.WIKI_LIST_KEY and item.get("error") == sentence
        for item in manifest["errors"]
    )
    _no_leak(manifest, workspace)


def test_missing_internal_key_is_not_shown_as_zero_hits(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    end = dt.date.fromisoformat(END)
    plan = module.build_plan(end)
    raw = {job["key"]: {"ok": True, "hits": []} for job in plan["alphaengine"]}
    raw[module.WIKI_LIST_KEY] = {"ok": True, "results": [], "warnings": []}
    for company in plan["internal"]["companies"]:
        if company["company"] == "宁德时代":
            continue
        raw[company["key"]] = {"ok": True, "matched_docs": 0, "picks": []}
    run = f"state/mark-weekly-news/runs/{END}"
    _write_json(workspace, f"{run}/raw.json", raw)
    _seed_prices_and_earnings(workspace)
    code, manifest = _run(
        module,
        ["--json", "assemble", "--workspace-dir", str(workspace), "--end", END, "--raw", f"{run}/raw.json"],
    )
    assert code == 0 and manifest["ok"] is True
    battery = next(item for item in manifest["evidence_files"] if item.endswith("/battery.md"))
    text = (workspace / battery).read_text(encoding="utf-8")
    block = text[text.index("## 宁德时代") : text.index("## 亿纬锂能")]
    assert "内部库尚未完成检索（预算用尽或重试已满）" in block
    assert "命中 0" not in block
    assert "  - （无）" not in block
    assert any(
        item.get("stage") == "internal"
        and item.get("key") == "宁德时代"
        and item.get("error") == "内部库尚未完成检索（预算用尽或重试已满）"
        for item in manifest["errors"]
    )
    _no_leak(manifest, workspace)


def test_manifest_keeps_every_error(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    end = dt.date.fromisoformat(END)
    plan = module.build_plan(end)
    raw = {job["key"]: {"ok": True, "hits": []} for job in plan["alphaengine"][10:]}
    raw[module.WIKI_LIST_KEY] = {"ok": True, "results": [], "warnings": []}
    run = f"state/mark-weekly-news/runs/{END}"
    _write_json(workspace, f"{run}/raw.json", raw)
    _seed_prices_and_earnings(workspace)
    code, manifest = _run(
        module,
        ["--json", "assemble", "--workspace-dir", str(workspace), "--end", END, "--raw", f"{run}/raw.json"],
    )
    assert code == 0 and manifest["ok"] is True
    assert len(manifest["errors"]) == 45
    assert manifest["errors_summary"] == manifest["errors"][:30]
    assert len(manifest["errors_summary"]) == 30
    battery = next(item for item in manifest["evidence_files"] if item.endswith("/battery.md"))
    text = (workspace / battery).read_text(encoding="utf-8")
    block = text[text.index("## 宁德时代") : text.index("## 亿纬锂能")]
    assert "检索失败：未执行" in block
    assert "命中 0" not in block
    _no_leak(manifest, workspace)


def test_empty_document_is_a_read_error() -> None:
    module = _load()
    company = _company()
    window = {"date_from": "2026-10-31", "date_to": "2026-11-08"}

    def _blank(arguments: dict) -> dict:
        if arguments.get("operation") == "read":
            return {"ok": True, "status": "ok", "data": {"document": {}}}
        if arguments.get("route") == "sales-notes":
            return _search_rows(
                [{"document_id": "mail:empty", "title": "空", "date": "2026-11-01"}]
            )
        return _search_rows([])

    cache: dict[str, str] = {}
    scanned = module.scan_company(
        company,
        [],
        window,
        create_autospec(module._call_research, side_effect=_blank),
        cache,
    )
    assert scanned["read_errors"] == [{"document_id": "mail:empty", "error": "empty_document"}]
    assert cache == {}
    assert scanned["picks"] == []
    assert scanned["partial"] is True


@pytest.mark.parametrize(
    "second_page",
    [
        {"ok": False, "status": "error", "error": "upstream_timeout"},
        {"ok": True, "status": "ok", "data": {"document": {}}},
    ],
)
def test_failed_continuation_page_is_a_read_error(second_page: dict) -> None:
    module = _load()
    first = _read_body("第一页正文。")
    first["data"]["structuredContent"]["next_offset"] = 30000
    first["data"]["structuredContent"]["complete"] = False

    def _paged(arguments: dict) -> dict:
        offset = arguments["params"]["offset"]
        return json.loads(json.dumps(first if offset == 0 else second_page))

    caller = create_autospec(module._call_research, side_effect=_paged)
    cache: dict[str, str] = {}
    with pytest.raises(module._ReadFailed):
        module.read_full("mail:paged", caller, cache)
    assert cache == {}
    assert caller.call_count == 2


def test_previous_earnings_section(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    previous = _passing_draft().replace("### 本周已发布\n无。", "### 本周已发布\n11/3 宁德时代已发布。", 1)
    _write(workspace, "state/mark-weekly-news/drafts/2026-10-31.md", previous)
    text = module.previous_earnings_section(workspace, dt.date.fromisoformat(END))
    assert "五、业绩日历" in text
    assert "11/3 宁德时代已发布" in text


def test_restyle_price_table_blocks_publish(tmp_path: Path) -> None:
    module = _load()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    draft = _passing_draft()
    _write(workspace, f"state/mark-weekly-news/runs/{END}/draft.md", draft)
    _write(workspace, f"state/mark-weekly-news/runs/{END}/restyle/draft.md", draft)
    code, passed = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END, "--which", "restyled"],
    )
    assert code == 0 and passed["ok"] is True
    assert passed["prices_equal"] is True
    changed = draft.replace("| +0.1% |", "| +9.9% |", 1)
    _write(workspace, f"state/mark-weekly-news/runs/{END}/restyle/draft.md", changed)
    code, blocked = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END, "--which", "restyled"],
    )
    assert code == 1 and blocked["ok"] is False
    assert blocked["prices_equal"] is False
    assert blocked["reason"] == "整体校验未过，不要发布"
    code, status = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(workspace), "--end", END, "--kind", "restyled"],
    )
    assert code == 0 and status["ok"] is True
    assert status["create"] is False
    assert status["reason"] == "整体校验未过，不要发布"

    signed = draft.replace(
        "- **判断：中性，本周没有新增重大订单。**",
        "- **判断：中性，本周订单 +10%。**",
        1,
    )
    flipped = signed.replace("本周订单 +10%。", "本周订单 -10%。", 1)
    _write(workspace, f"state/mark-weekly-news/runs/{END}/draft.md", signed)
    _write(workspace, f"state/mark-weekly-news/runs/{END}/restyle/draft.md", flipped)
    code, hand = _run(
        module,
        ["--json", "check", "--workspace-dir", str(workspace), "--end", END, "--which", "restyled"],
    )
    assert code == 1 and hand["ok"] is False
    assert hand["headings_equal"] is True
    assert hand["prices_equal"] is True
    assert hand["reason"] == "整体校验未过，不要发布"


def _save(module, workspace: Path, kind: str, payload: dict, name: str) -> tuple[int, dict]:
    relative = f"state/mark-weekly-news/runs/{END}/{name}"
    raw = json.dumps(payload, ensure_ascii=False).encode()
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _run(
        module,
        [
            "--json",
            "save-doc",
            "--workspace-dir",
            str(workspace),
            "--end",
            END,
            "--kind",
            kind,
            "--file",
            relative,
        ],
    )


def test_doc_failures_corrupt_record_and_summary_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    failed_ws = tmp_path / "failed"
    failed_ws.mkdir()
    _write(failed_ws, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    code, once = _save(module, failed_ws, "original", {"ok": False, "error": "call_failed"}, "feishu-1.json")
    assert code == 0 and once["ok"] is True
    assert once["create"] is True
    assert once["attempts"] == 1
    assert once["path"].endswith("doc-original.failed.json")
    assert not (failed_ws / f"state/mark-weekly-news/runs/{END}/doc-original.json").exists()
    code, status = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(failed_ws), "--end", END, "--kind", "original"],
    )
    assert code == 0 and status["create"] is True
    code, twice = _save(module, failed_ws, "original", {"ok": False, "error": "call_failed"}, "feishu-2.json")
    assert code == 0 and twice["create"] is False
    assert twice["reason"] == "create_failed_twice"
    assert twice["attempts"] == 2
    code, status = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(failed_ws), "--end", END, "--kind", "original"],
    )
    assert code == 0 and status["create"] is False
    assert status["reason"] == "create_failed_twice"
    assert not (failed_ws / f"state/mark-weekly-news/runs/{END}/doc-original.json").exists()

    short_ws = tmp_path / "short"
    short_ws.mkdir()
    _write(short_ws, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    code, short = _save(module, short_ws, "original", {"ok": True, "document_id": "abc"}, "feishu-short.json")
    assert code == 0 and short["ok"] is True
    assert short["url"] is None
    assert short["attempts"] == 1
    assert not (short_ws / f"state/mark-weekly-news/runs/{END}/doc-original.json").exists()
    official = short_ws / f"state/mark-weekly-news/runs/{END}/doc-original.json"
    official.write_text("{", encoding="utf-8")
    code, unreadable = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(short_ws), "--end", END, "--kind", "original"],
    )
    assert code == 0 and unreadable["create"] is False
    assert unreadable["reason"] == "doc_record_unreadable"
    code, skipped = _save(module, short_ws, "original", {"ok": True, "document_id": ORIGINAL_DOC}, "feishu-new.json")
    assert code == 0 and skipped["skipped"] is True
    assert skipped["create"] is False
    assert official.read_text(encoding="utf-8") == "{"

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    _write(incomplete, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    code, body = _save(
        module,
        incomplete,
        "restyled",
        {"ok": False, "error": "content_insert_failed", "document_id": RESTYLED_DOC},
        "feishu-restyled.json",
    )
    assert code == 0 and body["stored"] is True
    assert body["create"] is False
    assert body["draft_copied"] is False
    assert body["url"] == f"https://feishu.cn/docx/{RESTYLED_DOC}"
    code, status = _run(
        module,
        ["--json", "doc-status", "--workspace-dir", str(incomplete), "--end", END, "--kind", "restyled"],
    )
    assert code == 0 and status["create"] is False
    _write_json(incomplete, f"state/mark-weekly-news/runs/{END}/summary-input.json", _summary_input())
    code, summary = _run(
        module,
        [
            "--json",
            "summary",
            "--workspace-dir",
            str(incomplete),
            "--end",
            END,
            "--input",
            f"state/mark-weekly-news/runs/{END}/summary-input.json",
        ],
    )
    assert code == 0 and summary["ok"] is True
    assert summary["over_limit"] is False
    assert "正文未完成" in summary["text"]
    assert RESTYLED_DOC not in summary["text"]
    assert "改写版（推荐读这份）：未发布" in summary["text"]

    long_ws = tmp_path / "long"
    long_ws.mkdir()
    _write(long_ws, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    _write_json(
        long_ws,
        f"state/mark-weekly-news/runs/{END}/doc-restyled.json",
        {"ok": True, "document_id": RESTYLED_DOC},
    )
    _write_json(
        long_ws,
        f"state/mark-weekly-news/runs/{END}/summary-input.json",
        _summary_input(highlights=["测" * 1600]),
    )
    code, long_summary = _run(
        module,
        [
            "--json",
            "summary",
            "--workspace-dir",
            str(long_ws),
            "--end",
            END,
            "--input",
            f"state/mark-weekly-news/runs/{END}/summary-input.json",
        ],
    )
    assert code == 1 and long_summary["ok"] is False
    assert long_summary["over_limit"] is True
    assert long_summary["chars"] > 1500
    assert long_summary["reason"] == "摘要超过 1500 字，先压缩再提交"
    assert "测" in long_summary["text"]

    gate_ws = tmp_path / "gate"
    gate_ws.mkdir()
    draft = _passing_draft()
    _write(gate_ws, f"state/mark-weekly-news/runs/{END}/draft.md", draft)
    _write(
        gate_ws,
        f"state/mark-weekly-news/runs/{END}/restyle/draft.md",
        draft.replace("| +0.1% |", "| +9.9% |", 1),
    )
    _write_json(
        gate_ws,
        f"state/mark-weekly-news/runs/{END}/doc-restyled.json",
        {"ok": True, "document_id": RESTYLED_DOC},
    )
    _write_json(gate_ws, f"state/mark-weekly-news/runs/{END}/summary-input.json", _summary_input())
    code, gated = _run(
        module,
        [
            "--json",
            "summary",
            "--workspace-dir",
            str(gate_ws),
            "--end",
            END,
            "--input",
            f"state/mark-weekly-news/runs/{END}/summary-input.json",
        ],
    )
    assert code == 0 and gated["ok"] is True
    assert "整体校验未过，不要发布" in gated["text"]
    assert f"改写版（推荐读这份）：<https://feishu.cn/docx/{RESTYLED_DOC}>" in gated["text"]

    archive_ws = tmp_path / "archive"
    archive_ws.mkdir()
    _write(archive_ws, f"state/mark-weekly-news/runs/{END}/draft.md", _passing_draft())
    fake_archive = create_autospec(module._archive_draft, side_effect=OSError("disk"))
    monkeypatch.setattr(module, "_archive_draft", fake_archive)
    code, archived = _save(
        module,
        archive_ws,
        "original",
        {"ok": True, "document_id": ORIGINAL_DOC},
        "feishu-ok.json",
    )
    assert code == 0 and archived["stored"] is True
    assert archived["archive_error"] == "OSError"
    assert archived["draft_copied"] is False
    assert archived["history_updated"] is True
    assert archived["create"] is False
    assert fake_archive.call_count == 1
    receipt = archive_ws / f"state/mark-weekly-news/runs/{END}/doc-original.json"
    assert json.loads(receipt.read_text(encoding="utf-8"))["document_id"] == ORIGINAL_DOC
    _no_leak(archived, archive_ws)
