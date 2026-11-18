"""OC-1 daily filing alert: card parity, offline scan, and path jail."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton.database.automation_reports import REPORT_PROFILES
from src.dalton.settings import get_sec_user_agent

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-daily-filing-alert"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "daily_filing_alert.py"
ORIG_CARD = REPO_ROOT / ".agy" / "openclaw_src_at3" / "scripts" / "feishu_filing_card.py"
BANNED = (
    "discord",
    "feishu_send_card",
    ".openclaw",
    "openclaw_workspace",
    "xreach",
    "chat_id",
)
_RECIPIENT_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}

SAMPLE = {
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

_BIG = (
    "**BIG** <font color='grey'>重点公司</font> <font color='blue'>10-K</font> <font color='orange'>8-K</font>\n"
    "摘要\n"
    "<font color='grey'>· 重大事项</font>  [01-01](https://example.com/a)\n"
    "<font color='grey'>· 年报</font>  <font color='grey'>01-01</font>"
)
_SELL = (
    "**SELL** <font color='grey'>大额减持</font> <font color='red'>Form 144</font> <font color='grey'>合计 5,000 万美元</font>\n"
    "<font color='grey'>· 拟售 1 股，约 5,000 万美元</font>  <font color='grey'>01-01</font>"
)
_TINY = "**TINY** <font color='grey'>例行公司 · 税款代扣</font>  <font color='grey'>01-01</font>"
_PANEL = {
    "tag": "collapsible_panel",
    "expanded": False,
    "header": {
        "title": {"tag": "markdown", "content": "**例行披露 · 1 家**"},
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
    "elements": [{"tag": "markdown", "content": _TINY}],
}
EXPECTED = {
    "schema": "2.0",
    "config": {"wide_screen_mode": True, "update_multi": True},
    "header": {
        "title": {"tag": "plain_text", "content": "每日公告提醒"},
        "template": "indigo",
        "subtitle": {
            "tag": "plain_text",
            "content": "2026-01-02 · 周五 · 覆盖过去 24 小时 · 扫描 74 家",
        },
        "text_tag_list": [{
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "重点 2 · 例行 1"},
            "color": "blue",
        }],
    },
    "body": {
        "direction": "vertical",
        "vertical_spacing": "10px",
        "elements": [
            {
                "tag": "markdown",
                "text_size": "notation",
                "content": "<font color='grey'>重点 · 2 家</font>",
            },
            {"tag": "markdown", "content": _BIG},
            {"tag": "markdown", "content": _SELL},
            {"tag": "hr"},
            _PANEL,
            {
                "tag": "markdown",
                "text_size": "notation",
                "content": "<font color='grey'>数据源：SEC EDGAR · 巨潮公告 · HKEXnews</font>",
            },
        ],
    },
}

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Ada Example</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><officerTitle>CFO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>10</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""
def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(module, argv: list[str]) -> tuple[int, dict]:
    buffer = StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, json.loads(buffer.getvalue())


def _run_captured(module, argv: list[str]) -> tuple[int, dict, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = module.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    text = stdout.getvalue()
    try:
        payload = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        payload = {"raw": text}
    return code, payload, stderr.getvalue()


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


def _cli(name: str):
    module = _load(name, CLI_PATH)
    module._locals()
    return module


_FETCH_ORIG = None


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, side_effect):
    global _FETCH_ORIG
    net = sys.modules["dalton_daily_filing_netio"]
    if _FETCH_ORIG is None:
        _FETCH_ORIG = net.fetch_bytes
    fake = create_autospec(_FETCH_ORIG, side_effect=side_effect)
    monkeypatch.setattr(net, "fetch_bytes", fake)
    return fake


def _bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _no_leak(text: str) -> None:
    assert "/Users/" not in text
    assert _RECIPIENT_OC_RE.search(text) is None


def test_card_matches_historical_sample() -> None:
    filing_card = _load(
        "dalton_daily_filing_card_sample",
        SKILL_ROOT / "scripts" / "filing_card.py",
    )
    card = filing_card.build_card(SAMPLE)
    assert card == EXPECTED
    filing_card.self_check()
    if ORIG_CARD.is_file():
        original = _load("feishu_filing_card_historical", ORIG_CARD)
        orig_card = original.build_card(SAMPLE)
        assert card["schema"] == orig_card["schema"]
        assert card["config"] == orig_card["config"]
        assert card["header"] == orig_card["header"]
        assert card["body"]["elements"][:-1] == orig_card["body"]["elements"]


def test_frontmatter_registry_and_profile() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        DALTON_SKILL_DEFINITIONS,
    )

    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    assert meta["name"] == SKILL_NAME
    assert isinstance(meta["description"], str) and meta["description"].strip()
    assert "触发词" not in meta["description"]
    assert len(meta["description"]) <= 120
    spec = (SKILL_NAME, None, ("dalton-bullbear-tracker",))
    assert spec in _DALTON_SKILL_SPECS
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].requires == ("dalton-bullbear-tracker",)
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].description == meta["description"].strip()
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].command is None
    assert "daily-filing-alert" in REPORT_PROFILES


def test_skill_tree_omits_delivery_targets() -> None:
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert _RECIPIENT_OC_RE.search(text) is None, path
        for token in BANNED:
            assert token not in text, f"{path} contains {token}"


def test_window_monday_and_form_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_window")

    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    code, payload = _run(
        cli,
        ["--json", "window", "--workspace-dir", str(tmp_path), "--as-of", "2026-11-09T20:30:00+08:00"],
    )
    assert code == 0 and payload["ok"] is True
    assert payload["lookback_days"] == 3
    assert payload["window_note"] == "覆盖上周五至今"
    assert payload["weekday"] == "周一"
    assert payload["report_run_id"] == "daily-filing-alert-2026-11-09"
    assert payload["report_date"] == "2026-11-09"
    assert payload["start"] == "2026-11-06"
    assert payload["end"] == "2026-11-09"
    assert payload["limit"] == 50
    a_bounds = {(row["start"], row["end"]) for row in payload["cn_hk"] if row["market"] == "A"}
    hk_bounds = {(row["start"], row["end"]) for row in payload["cn_hk"] if row["market"] == "HK"}
    assert a_bounds == {("2026-11-07", "2026-11-09")}
    assert hk_bounds == {("2026-11-06", "2026-11-10")}
    assert [row["ticker"] for row in payload["cn_hk"]] == [
        "300308", "300502", "300750", "09992", "00700", "03690", "00788",
    ]
    by_code = {row["code"]: row for row in payload["cn_hk"]}
    assert by_code["3690"] == {
        "ticker": "03690",
        "code": "3690",
        "name": "美团",
        "market": "HK",
        "start": "2026-11-06",
        "end": "2026-11-10",
    }
    assert by_code["0788"] == {
        "ticker": "00788",
        "code": "0788",
        "name": "中国铁塔",
        "market": "HK",
        "start": "2026-11-06",
        "end": "2026-11-10",
    }
    edgar = sys.modules["dalton_daily_filing_edgar_client"]
    assert edgar.form_matches("424B2", "4") is False
    assert edgar.form_matches("424B5", "424B") is True
    assert edgar.form_matches("4", "4") is True
    assert edgar.form_matches("4/A", "4") is True
    assert cli.categorize_filing("424B5") == "offering"
    assert cli.categorize_filing("4") == "insider"
    assert cli.categorize_filing("4/A") == "insider"


def test_documented_commands_run_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_documented")

    def route(url, timeout=20, headers=None, min_interval=0.0):
        if "company_tickers.json" in url:
            return _bytes({"0": {"ticker": "ZZZZ", "cik_str": 1}})
        raise AssertionError(url)

    fake = _patch_fetch(monkeypatch, route)
    report_date = cli.describe_window(cli.parse_as_of(None))["report_date"]
    replacements = {
        "<workspace>": str(tmp_path),
        "{LOOKBACK_DAYS}": "1",
        "<report_date>": report_date,
    }
    commands = _bash_commands((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8"))
    seen: list[str] = []
    for command in commands:
        argv = _argv_from(command, replacements)
        assert argv[0] == "--json"
        assert argv[2] == "--workspace-dir"
        assert argv[3] == str(tmp_path)
        seen.append(argv[1])
        if argv[1] == "scan-cn-hk":
            raw_path = tmp_path / argv[argv.index("--input") + 1]
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(json.dumps({
                "start": report_date,
                "end": report_date,
                "limit": 50,
                "calls": [
                    {"ticker": "300308", "ok": True, "rows": [{
                        "date": report_date,
                        "title": "关于回购股份",
                        "category": "回购",
                        "pdf_link": f"https://static.cninfo.com.cn/finalpage/{report_date}/a.pdf",
                    }]},
                    {"ticker": "00700", "ok": True, "data": [{
                        "date": report_date,
                        "title": "翌日披露",
                        "pdf_link": "https://www.hkexnews.hk/1.pdf",
                    }]},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            last_check = tmp_path / "state" / "daily-filing-alert" / "last_check.json"
            before_last_check = last_check.read_bytes()
        if argv[1] == "render-card":
            payload_path = tmp_path / "temp" / "daily-filing-alert" / f"{report_date}-payload.json"
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            payload_path.write_text(
                json.dumps({
                    "date": report_date,
                    "weekday": "周四",
                    "window_note": "覆盖过去 24 小时",
                    "stats": {"us": {"scanned": 1}},
                    "sections": [{"key": "us", "companies": []}],
                    "footer": "数据源：SEC EDGAR · 巨潮公告 · HKEXnews",
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        code, payload = _run(cli, argv)
        assert code == 0 and payload["ok"] is True, payload
        if argv[1] == "scan-cn-hk":
            assert last_check.read_bytes() == before_last_check
        blob = json.dumps(payload, ensure_ascii=False)
        assert str(tmp_path) not in blob
        _no_leak(blob)
    assert seen == ["window", "self-test", "scan", "scan-cn-hk", "render-card"]
    assert fake.call_count >= 1
    sec_path = tmp_path / "temp" / "daily-filing-alert" / f"{report_date}-sec.json"
    sec = json.loads(sec_path.read_text(encoding="utf-8"))
    symbols = [
        line.strip()
        for line in (SKILL_ROOT / "watchlist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert sec["tickers_scanned"] == len(symbols) == 58
    assert {"ECHO", "CRWV", "BAM"}.issubset(symbols) and "SATS" not in symbols
    assert sec["tickers_with_alerts"] == 0
    assert all(result["errors"] for result in sec["results"])
    state = tmp_path / "state" / "daily-filing-alert"
    for name in ("last_check.json", "filings.json", "cik_map.json"):
        _no_leak((state / name).read_text(encoding="utf-8"))
    cn = json.loads((tmp_path / "temp" / "daily-filing-alert" / f"{report_date}-cn-hk.json").read_text(encoding="utf-8"))
    hk = next(row for row in cn["results"] if row["ticker"] == "0700")
    assert hk["market"] == "HK"
    assert hk["name"] == "腾讯控股"
    assert hk["count"] == 1
    assert hk["announcements"][0]["title"] == "翌日披露"
    assert hk["announcements"][0]["url"] == "https://www.hkexnews.hk/1.pdf"
    assert "web_search_required" not in hk
    a_share = next(row for row in cn["results"] if row["ticker"] == "300308")
    assert a_share["announcements"][0]["title"] == "关于回购股份"
    assert a_share["announcements"][0]["type"] == "回购"
    assert "eastmoney" not in json.dumps(cn)
    card_text = (tmp_path / "temp" / "daily-filing-alert" / f"{report_date}-card.json").read_text(encoding="utf-8")
    _no_leak(card_text)
    assert "无新动态" in card_text


def test_sec_scan_parses_form4_and_skips_last_check_on_map_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_form4")

    def route(url, timeout=20, headers=None, min_interval=0.0):
        if "company_tickers.json" in url:
            return _bytes({"0": {"ticker": "ADA", "cik_str": 12345}})
        if url.endswith("/CIK0000012345.json"):
            return _bytes({
                "filings": {"recent": {
                    "accessionNumber": ["0000000000-26-000001"],
                    "form": ["4"],
                    "filingDate": ["2026-11-12"],
                    "items": [""],
                    "reportDate": [""],
                    "primaryDocument": ["form4.xml"],
                    "primaryDocDescription": ["FORM 4"],
                }},
            })
        if url.endswith("/index.json"):
            return _bytes({"directory": {"item": [{"name": "form4.xml"}]}})
        if url.endswith("/form4.xml"):
            return FORM4_XML.encode("utf-8")
        raise AssertionError(url)

    _patch_fetch(monkeypatch, route)
    output = "temp/daily-filing-alert/form4.json"
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA", "--days", "3", "--output", output,
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True, payload
    assert str(tmp_path) not in json.dumps(payload)
    saved = json.loads((tmp_path / output).read_text(encoding="utf-8"))
    filing = saved["results"][0]["new_filings"][0]
    assert filing["type"] == "Form 4"
    assert filing["parsed"]["insider"] == "Ada Example"
    assert filing["parsed"]["position"] == "CFO"
    assert filing["parsed"]["summary"] == "卖出 1,000 股，约 $10,000"
    assert filing["parsed"]["tx_type"] == "SALE"
    assert filing["headline"] == "卖出 1,000 股，约 $10,000"
    _no_leak((tmp_path / output).read_text(encoding="utf-8"))
    last_check = tmp_path / "state" / "daily-filing-alert" / "last_check.json"
    stamp = json.loads(last_check.read_text(encoding="utf-8"))
    check_code, check_payload = _run(cli, [
        "--json", "check", "--workspace-dir", str(tmp_path),
        "ADA", "--days", "3", "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert check_code == 0 and check_payload["ok"] is True
    assert json.loads(last_check.read_text(encoding="utf-8")) == stamp

    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise TimeoutError("offline")

    (tmp_path / "state" / "daily-filing-alert" / "cik_map.json").unlink()
    _patch_fetch(monkeypatch, offline)
    failed_out = "temp/daily-filing-alert/map-down.json"
    failed, error = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA", "--days", "1", "--output", failed_out,
        "--as-of", "2026-11-12T21:00:00+08:00",
    ])
    assert failed == 1 and error["ok"] is False
    assert error["error"] == "RuntimeError"
    assert "SEC ticker map unavailable" in error["message"]
    assert json.loads(last_check.read_text(encoding="utf-8")) == stamp
    assert not (tmp_path / failed_out).exists()


def test_cn_hk_window_and_excerpt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_cnhk")

    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    fake = _patch_fetch(monkeypatch, offline)
    raw_path = tmp_path / "temp" / "daily-filing-alert" / "cn-raw.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps({
        "start": "2026-11-11",
        "end": "2026-11-12",
        "limit": 50,
        "calls": [
            {"ticker": "300308", "ok": True, "rows": [
                {
                    "date": "2026-11-12",
                    "title": "关于回购股份",
                    "category": "回购",
                    "pdf_link": "https://static.cninfo.com.cn/finalpage/2026-11-12/a.pdf",
                },
                {"date": "2026-01-01", "title": "窗口外", "pdf_link": "https://static.cninfo.com.cn/old.pdf"},
            ]},
            {"ticker": "300502", "ok": False, "error": "timeout"},
            {"ticker": "09992", "ok": True, "limit": 1, "rows": [{
                "date": "2026-11-12",
                "title": "业绩公告",
                "pdf_link": "https://www.hkexnews.hk/2.pdf",
            }]},
            {"ticker": "00700", "ok": True, "status": "ok", "data": [{
                "filing_date": "2026-11-12",
                "title": "翌日披露",
                "pdf_link": "https://www.hkexnews.hk/1.pdf",
            }]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    output = "temp/daily-filing-alert/cn.json"
    code, payload = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", "temp/daily-filing-alert/cn-raw.json",
        "--output", output,
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True, payload
    assert payload["report_run_id"] == "daily-filing-alert-2026-11-12"
    assert payload["cn_stocks_scanned"] == 3
    assert payload["hk_stocks_scanned"] == 4
    assert fake.call_count == 0
    saved = json.loads((tmp_path / output).read_text(encoding="utf-8"))
    by_ticker = {row["ticker"]: row for row in saved["results"]}
    a_share = by_ticker["300308"]
    assert a_share["count"] == 1
    assert a_share["announcements"][0]["title"] == "关于回购股份"
    assert a_share["announcements"][0]["type"] == "回购"
    assert a_share["announcements"][0]["url"].endswith("/a.pdf")
    assert a_share["name"] == "中际旭创"
    assert by_ticker["300502"]["errors"] == ["timeout"]
    assert by_ticker["300502"]["announcements"] == []
    assert by_ticker["300750"]["errors"] == ["finance result missing"]
    hk = by_ticker["0700"]
    assert hk["count"] == 1
    assert hk["announcements"][0]["title"] == "翌日披露"
    assert hk["announcements"][0]["date"] == "2026-11-12"
    assert "web_search_required" not in hk
    assert by_ticker["9992"]["truncated"] is True
    assert by_ticker["9992"]["announcements"][0]["title"] == "业绩公告"
    assert saved["truncated"] == ["9992"]
    assert "300502: timeout" in saved["day_errors"]
    assert "300750: finance result missing" in saved["day_errors"]
    assert not (tmp_path / "state" / "daily-filing-alert" / "last_check.json").exists()
    _no_leak((tmp_path / output).read_text(encoding="utf-8"))


def test_paths_do_not_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_paths")

    def route(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    fake = _patch_fetch(monkeypatch, route)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    escaped = tmp_path / "outside.json"
    code, payload, _stderr = _run_captured(cli, [
        "--json", "scan", "--workspace-dir", str(workspace),
        "--tickers", "ADA", "--days", "1", "--no-enrich",
        "--output", "../outside.json",
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 1 and payload["ok"] is False
    assert payload["message"] == "path must stay inside the workspace"
    assert not escaped.exists()
    assert fake.call_count == 0

    outside_state = tmp_path / "outside-state"
    outside_state.mkdir()
    (workspace / "state").mkdir()
    os.symlink(outside_state, workspace / "state" / "daily-filing-alert")
    linked, link_payload, _stderr = _run_captured(cli, [
        "--json", "scan", "--workspace-dir", str(workspace),
        "--tickers", "ADA", "--days", "1", "--no-enrich",
        "--output", "temp/daily-filing-alert/sec.json",
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert linked == 1 and link_payload["ok"] is False
    assert link_payload["message"] == "path must stay inside the workspace"
    assert list(outside_state.rglob("*")) == []
    assert fake.call_count == 0
    (workspace / "state" / "daily-filing-alert").unlink()

    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        cli.parse_tickers(workspace, "../outside.txt")
    with pytest.raises(RuntimeError, match="watchlist file not found"):
        cli.parse_tickers(workspace, "missing\\list.txt")

    card_outside = tmp_path / "card-outside.json"
    card_outside.write_text("sentinel", encoding="utf-8")
    card_dir = workspace / "temp" / "daily-filing-alert"
    card_dir.mkdir(parents=True)
    payload_path = card_dir / "payload.json"
    payload_path.write_text(
        json.dumps({"date": "2026-11-12", "sections": [], "footer": "数据源：SEC EDGAR · 巨潮公告 · HKEXnews"}),
        encoding="utf-8",
    )
    os.symlink(card_outside, card_dir / "card.json")
    refused, refused_payload, _stderr = _run_captured(cli, [
        "--json", "render-card", "--workspace-dir", str(workspace),
        "--payload", "temp/daily-filing-alert/payload.json",
        "--output", "temp/daily-filing-alert/card.json",
    ])
    assert refused == 1 and refused_payload["ok"] is False
    assert refused_payload["message"] == "refusing symlink"
    assert card_outside.read_text(encoding="utf-8") == "sentinel"
    (card_dir / "card.json").unlink()

    first, first_payload = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(workspace),
        "--payload", "temp/daily-filing-alert/payload.json",
        "--output", "temp/daily-filing-alert/card.json",
    ])
    assert first == 0 and first_payload["reused"] is False
    original_bytes = (card_dir / "card.json").read_bytes()
    again, again_payload = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(workspace),
        "--payload", "temp/daily-filing-alert/payload.json",
        "--output", "temp/daily-filing-alert/card.json",
    ])
    assert again == 0 and again_payload["reused"] is True
    payload_path.write_text(
        json.dumps({"date": "2026-11-12", "lead": "另一张", "sections": []}),
        encoding="utf-8",
    )
    conflict, conflict_payload = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(workspace),
        "--payload", "temp/daily-filing-alert/payload.json",
        "--output", "temp/daily-filing-alert/card.json",
    ])
    assert conflict == 1 and conflict_payload["error"] == "card_conflict"
    assert (card_dir / "card.json").read_bytes() == original_bytes


def test_submission_errors_still_record_last_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_partial")

    def route(url, timeout=20, headers=None, min_interval=0.0):
        if "company_tickers.json" in url:
            return _bytes({"0": {"ticker": "ADA", "cik_str": 9}})
        raise TimeoutError("submissions down")

    _patch_fetch(monkeypatch, route)
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA,ZZZ", "--days", "1", "--no-enrich",
        "--output", "temp/daily-filing-alert/partial.json",
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True, payload
    saved = json.loads((tmp_path / "temp/daily-filing-alert/partial.json").read_text(encoding="utf-8"))
    by_ticker = {row["ticker"]: row for row in saved["results"]}
    assert any("TimeoutError" in item for item in by_ticker["ADA"]["errors"])
    assert any("no CIK" in item for item in by_ticker["ZZZ"]["errors"])
    state = json.loads((tmp_path / "state/daily-filing-alert/last_check.json").read_text(encoding="utf-8"))
    assert set(state) == {"ADA", "ZZZ"}
    _no_leak(json.dumps(saved))


GIFT_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Bea Example</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><officerTitle>Director</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>G</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

DISPOSE_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Bea Example</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><officerTitle>Director</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <derivativeTable>
    <derivativeTransaction>
      <transactionCoding><transactionCode>D</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>20</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>
</ownershipDocument>
"""


def test_form4_uncovered_types_keep_the_raw_code() -> None:
    edgar = _cli("daily_filing_tx")._locals()["edgar_client"]
    gift = edgar._parse_form4_xml(GIFT_XML)
    assert gift["transactions"][0]["type"] == "GIFT"
    assert gift["tx_type"] == "GIFT"
    assert gift["summary"] == "GIFT"
    assert "获授" not in gift["summary"]
    assert "DERIVATIVE ACQUISITION" not in gift["summary"]
    dispose = edgar._parse_form4_xml(DISPOSE_XML)
    assert dispose["transactions"][0]["type"] == "DISPOSE"
    assert dispose["tx_type"] == "DISPOSE"
    assert dispose["summary"] == "DISPOSE"
    assert "DERIVATIVE ACQUISITION" not in dispose["summary"]


def test_form4_doc4_xml_uses_primary_document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_doc4")
    fetched: list[str] = []

    def route(url, timeout=20, headers=None, min_interval=0.0):
        fetched.append(url)
        if "company_tickers.json" in url:
            return _bytes({"0": {"ticker": "ADA", "cik_str": 12345}})
        if url.endswith("/CIK0000012345.json"):
            return _bytes({
                "filings": {"recent": {
                    "accessionNumber": ["0000000000-26-000004"],
                    "form": ["4"],
                    "filingDate": ["2026-11-12"],
                    "items": [""],
                    "reportDate": [""],
                    "primaryDocument": ["xslF345X05/doc4.xml"],
                    "primaryDocDescription": ["FORM 4"],
                }},
            })
        if url.endswith("/index.json"):
            return _bytes({"directory": {"item": [
                {"name": "doc4.xml"},
                {"name": "primary_doc.xml"},
            ]}})
        if url.endswith("/doc4.xml"):
            return FORM4_XML.encode("utf-8")
        if url.endswith("/primary_doc.xml"):
            return b"<html>rendered form</html>"
        raise AssertionError(url)

    _patch_fetch(monkeypatch, route)
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA", "--days", "3", "--output", "temp/daily-filing-alert/doc4.json",
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True, payload
    saved = json.loads((tmp_path / "temp/daily-filing-alert/doc4.json").read_text(encoding="utf-8"))
    parsed = saved["results"][0]["new_filings"][0]["parsed"]
    assert parsed["insider"] == "Ada Example"
    assert parsed["summary"] == "卖出 1,000 股，约 $10,000"
    assert any(url.endswith("/doc4.xml") for url in fetched)
    assert not any(url.endswith("/primary_doc.xml") for url in fetched)
    assert not any("xslF345X05" in url for url in fetched)


def test_a_share_body_and_market_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_body")

    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    code, window = _run(cli, [
        "--json", "window", "--workspace-dir", str(tmp_path),
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and window["ok"] is True
    assert window["lookback_days"] == 1
    assert window["start"] == "2026-11-11"
    assert window["end"] == "2026-11-12"
    assert {(row["start"], row["end"]) for row in window["cn_hk"] if row["market"] == "A"} == {
        ("2026-11-12", "2026-11-12"),
    }
    assert {(row["start"], row["end"]) for row in window["cn_hk"] if row["market"] == "HK"} == {
        ("2026-11-11", "2026-11-13"),
    }
    notice = cli._locals()["cn_ndr"]
    notice.self_check()
    raw_path = tmp_path / "temp" / "daily-filing-alert" / "body-raw.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps({
        "end": "2026-11-12",
        "limit": 50,
        "calls": [
            {"ticker": "300308", "ok": True, "rows": [
                {
                    "date": "2026-11-11",
                    "title": "昨天的回购",
                    "pdf_link": "https://static.cninfo.com.cn/y.pdf",
                },
                {
                    "date": "2026-11-12",
                    "title": "今天的回购",
                    "pdf_link": "https://static.cninfo.com.cn/t.pdf",
                    "notice_text": "本公司及董事会全体成员保证信息披露的内容真实、准确、完整。公司拟回购股份",
                },
                {
                    "date": "2026-11-12",
                    "title": "无正文",
                    "pdf_link": "https://static.cninfo.com.cn/empty.pdf",
                },
            ]},
            {"ticker": "300750", "ok": True, "rows": [
                {
                    "date": "2026-11-12",
                    "title": "H股公告-翌日披露报表",
                    "pdf_link": "https://static.cninfo.com.cn/ndr.pdf",
                    "notice_text": notice.SAMPLE_TREASURY.replace("300308", "300750"),
                },
                {
                    "date": "2026-11-12",
                    "title": "坏的翌日披露",
                    "pdf_link": "https://static.cninfo.com.cn/bad.pdf",
                    "notice_text": "普通公告正文",
                },
            ]},
            {"ticker": "300502", "ok": True, "limit": 50, "rows": [
                {"date": "2026-11-12", "title": "第一段", "pdf_link": "https://static.cninfo.com.cn/s1.pdf"},
            ]},
            {"ticker": "300502", "ok": True, "limit": 50, "rows": [
                {"date": "2026-11-12", "title": "第一段", "pdf_link": "https://static.cninfo.com.cn/s1.pdf"},
                {"date": "2026-11-12", "title": "第二段", "pdf_link": "https://static.cninfo.com.cn/s2.pdf"},
            ]},
            {"ticker": "09992", "ok": True, "limit": 1, "rows": [
                {"date": "2026-11-12", "title": "业绩公告", "pdf_link": "https://www.hkexnews.hk/2.pdf"},
            ]},
            {"ticker": "00700", "ok": True, "rows": [
                {"date": "2026-11-11", "title": "港股昨天", "pdf_link": "https://www.hkexnews.hk/y.pdf"},
                {"date": "2026-11-13", "title": "港股明天", "pdf_link": "https://www.hkexnews.hk/n.pdf"},
                {"date": "2026-11-14", "title": "港股后天", "pdf_link": "https://www.hkexnews.hk/x.pdf"},
                {
                    "date": "2026-11-12",
                    "title": "翌日披露",
                    "pdf_link": "https://www.hkexnews.hk/1.pdf",
                    "notice_text": notice.SAMPLE_TREASURY,
                },
            ]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    output = "temp/daily-filing-alert/body.json"
    code, payload = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", "temp/daily-filing-alert/body-raw.json",
        "--output", output,
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True, payload
    saved = json.loads((tmp_path / output).read_text(encoding="utf-8"))
    by_ticker = {row["ticker"]: row for row in saved["results"]}
    repurchase = by_ticker["300308"]["announcements"]
    assert [item["title"] for item in repurchase] == ["今天的回购", "无正文"]
    assert repurchase[0]["body_excerpt"] == "公司拟回购股份"
    assert repurchase[0]["pdf_url"].endswith("/t.pdf")
    assert "digest" not in repurchase[0]
    assert repurchase[1]["enrich_error"] == "正文为空（可能只有 PDF）"
    assert repurchase[1]["pdf_url"].endswith("/empty.pdf")
    ndr_row, bad_row = by_ticker["300750"]["announcements"]
    assert ndr_row["ndr"]["buybacks"][0]["total_shares"] == 856980
    assert ndr_row["ndr"]["buybacks"][0]["to_treasury"] == 856980
    assert not ndr_row["ndr"]["buybacks"][0]["to_cancel"]
    for needle in ("794,631,549.33元人民币", "持作库存股", "921.85至935", "0.077%"):
        assert needle in ndr_row["digest"]
    assert "7.95亿" not in ndr_row["digest"]
    assert ndr_row["pdf_url"].endswith("/ndr.pdf")
    assert bad_row["enrich_error"].startswith("翌日披露报表解析失败")
    segments = by_ticker["300502"]
    assert [item["title"] for item in segments["announcements"]] == ["第一段", "第二段"]
    assert segments["truncated"] is False
    assert by_ticker["9992"]["truncated"] is True
    hk_titles = [item["title"] for item in by_ticker["0700"]["announcements"]]
    assert hk_titles == ["港股昨天", "港股明天", "翌日披露"]
    assert "digest" not in by_ticker["0700"]["announcements"][-1]
    assert "300502" not in saved["truncated"]
    assert saved["truncated"] == ["9992"]
    _no_leak((tmp_path / output).read_text(encoding="utf-8"))


def test_last_check_waits_for_both_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _cli("daily_filing_bookmark")

    def route(url, timeout=20, headers=None, min_interval=0.0):
        if "company_tickers.json" in url:
            return _bytes({"0": {"ticker": "ADA", "cik_str": 9}})
        raise TimeoutError("submissions down")

    _patch_fetch(monkeypatch, route)
    card_io = sys.modules["dalton_daily_filing_card_io"]
    real_write = card_io.write_bytes_exclusive

    def fail_filings(path, payload):
        if path.name == "filings.json":
            raise OSError("filings.json failed")
        return real_write(path, payload)

    monkeypatch.setattr(card_io, "write_bytes_exclusive", fail_filings)
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA", "--days", "1", "--no-enrich",
        "--output", "temp/daily-filing-alert/first.json",
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    state = tmp_path / "state" / "daily-filing-alert" / "last_check.json"
    assert code == 1 and payload["ok"] is False
    assert not state.exists()
    assert not (tmp_path / "temp/daily-filing-alert/first.json").exists()
    assert not (tmp_path / "state" / "daily-filing-alert" / "filings.json").exists()

    monkeypatch.setattr(card_io, "write_bytes_exclusive", real_write)
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA", "--days", "1", "--no-enrich",
        "--output", "temp/daily-filing-alert/second.json",
        "--as-of", "2026-11-12T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True, payload
    assert state.is_file()
    assert (tmp_path / "state/daily-filing-alert/filings.json").is_file()
    assert (tmp_path / "temp/daily-filing-alert/second.json").is_file()
    stamp = state.read_bytes()

    def fail_output(workspace, relative, document):
        raise OSError("output failed")

    monkeypatch.setattr(cli, "write_json", fail_output)
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "ADA", "--days", "1", "--no-enrich",
        "--output", "temp/daily-filing-alert/third.json",
        "--as-of", "2026-11-12T21:00:00+08:00",
    ])
    assert code == 1 and payload["ok"] is False
    assert state.read_bytes() == stamp
    assert not (tmp_path / "temp/daily-filing-alert/third.json").exists()


def test_sec_user_agent_fallback_matches_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    net = _load("dalton_daily_filing_netio_fallback", SKILL_ROOT / "scripts" / "netio.py")
    for key in ("EDGAR_IDENTITY", "SEC_USER_AGENT", "DALTON_SEC_USER_AGENT"):
        monkeypatch.delenv(key, raising=False)
    assert net.sec_user_agent() == get_sec_user_agent()
    assert net.sec_user_agent() == "DaltonResearch research@everflowinv.com"


def test_buyback_parse_fixtures_text_json_and_missing_pdf_dep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]

    cancel_text = (fixtures / "ff305_hk_buyback_cancel.txt").read_text(encoding="utf-8")
    parsed_cancel = bp.parse(cancel_text)
    assert parsed_cancel["total_shares"] == 1_005_000
    assert parsed_cancel["total_amount"] == 630_450_000.0
    assert parsed_cancel["currency"] == "HKD"
    assert parsed_cancel["avg_price"] == 627.31
    assert parsed_cancel["to_cancel"] == 1_005_000
    assert parsed_cancel["to_treasury"] == 0
    assert parsed_cancel["mandate_cum_shares"] == 84_320_000
    assert parsed_cancel["mandate_cum_pct"] == 0.91
    for needle in (
        "11-16 回购 1,005,000 股",
        "630,450,000.00 港元",
        "均价 627.31 港元",
        "624.00–631.50",
        "拟注销",
        "84,320,000 股",
        "0.91%",
    ):
        assert needle in parsed_cancel["line"]

    treasury_text = (fixtures / "ff305_hk_buyback_treasury.txt").read_text(encoding="utf-8")
    parsed_treasury = bp.parse(treasury_text)
    assert len(parsed_treasury["trades"]) == 2
    assert parsed_treasury["total_shares"] == 130_000
    assert parsed_treasury["total_amount"] == 15_630_000.0
    assert parsed_treasury["avg_price"] == 120.23
    assert parsed_treasury["lo"] == 118.0
    assert parsed_treasury["hi"] == 122.0
    for needle in (
        "11-15、11-16 回购 130,000 股",
        "15,630,000 港元",
        "30,000 股拟注销",
        "100,000 股持作库存股",
        "0.14%",
    ):
        assert needle in parsed_treasury["line"]

    # Reuse cn_ndr samples (including row without inline shares in SAMPLE_TREASURY).
    ndr_treasury = bp.parse(ndr.SAMPLE_TREASURY)
    assert ndr_treasury["total_shares"] == 856_980
    assert ndr_treasury["total_amount"] == 794_631_549.33
    assert ndr_treasury["currency"] == "RMB"
    assert ndr_treasury["to_treasury"] == 856_980
    assert ndr_treasury["to_cancel"] == 0
    assert "856,980 股" in ndr_treasury["line"]
    assert "794,631,549.33 元人民币" in ndr_treasury["line"]

    ndr_cancel = bp.parse(ndr.SAMPLE_CANCEL)
    assert ndr_cancel["total_shares"] == 665_060
    assert ndr_cancel["total_amount"] == 199_987_058.39
    assert ndr_cancel["currency"] == "RMB"
    assert ndr_cancel["to_cancel"] == 665_060
    assert "665,060 股" in ndr_cancel["line"]
    assert "199,987,058.39 元人民币" in ndr_cancel["line"]

    # Wrapped method across newline + verbatim precision + wrapped cancel/treasury numbers.
    wrapped_text = """翌日披露報表
第二章節
A. 購回報告
1). 2026年11月15日 10,001 於香港聯合交易所
進行 HKD 125.00 HKD 120.00 HKD 1,234,567.89
合共購回股份總數 10,001
合共付出的價格總額 (元) HKD 1,234,567.89
購回股份（擬註銷）數目
10,001
購回股份（擬持作庫存股份）
0
"""
    parsed_exact = bp.parse(wrapped_text)
    assert parsed_exact["total_shares"] == 10_001
    assert parsed_exact["total_amount"] == 1_234_567.89
    assert parsed_exact["to_cancel"] == 10_001
    assert "10,001 股" in parsed_exact["line"]
    assert "1,234,567.89 港元" in parsed_exact["line"]
    assert "均价 123.44 港元" in parsed_exact["line"]

    # Cross-check failure against PDF 合共 returns error + discrepancy without totals.
    mismatch_text = treasury_text.replace("2). 2026年11月16日 80,000 於本交易所進行", "2). 2026年11月16日 80,000 坏行")
    bad_mismatch = bp.parse(mismatch_text)
    assert "error" in bad_mismatch and "discrepancy" in bad_mismatch
    assert "total_shares" not in bad_mismatch and "line" not in bad_mismatch

    # Multi-share-class / multi-currency PDF returns separate groups and never sums across currencies.
    multi_class_text = """翌日披露報表
第二章節
1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300308 說明 A股
A. 購回報告
1). 2026年11月15日 10,000 於其他證券交易所進行 RMB 10.00 RMB 9.50 RMB 98,000.00
深圳證券交易所
合共購回股份總數 10,000
合共付出的價格總額 (元) RMB 98,000.00
2. 股份分類 普通股 股份類別 H 於香港聯交所上市 是
證券代號 (如上市) 00700 說明 H股
A. 購回報告
1). 2026年11月16日 40,000 於本交易所進行 HKD 100.00 HKD 98.00 HKD 3,960,000.00
香港聯合交易所
合共購回股份總數 40,000
合共付出的價格總額 (元) HKD 3,960,000.00
"""
    parsed_multi = bp.parse(multi_class_text)
    assert len(parsed_multi["groups"]) == 2
    assert "total_amount" not in parsed_multi
    assert parsed_multi["groups"][0]["currency"] == "RMB"
    assert parsed_multi["groups"][0]["total_amount"] == 98_000.0
    assert parsed_multi["groups"][1]["currency"] == "HKD"
    assert parsed_multi["groups"][1]["total_amount"] == 3_960_000.0

    monthly_text = (fixtures / "ff305_monthly_return_non_buyback.txt").read_text(encoding="utf-8")
    parsed_monthly = bp.parse(monthly_text)
    assert "error" in parsed_monthly
    assert "trades" not in parsed_monthly

    text_json_path = tmp_path / "raw_texts.json"
    text_json_path.write_text(
        json.dumps({"hk_700": cancel_text, "hk_3690": {"text": monthly_text}}, ensure_ascii=False),
        encoding="utf-8",
    )
    buf = StringIO()
    with redirect_stdout(buf):
        rc = bp.main(["--text-json", str(text_json_path)])
    assert rc == 0
    rows = json.loads(buf.getvalue())
    assert rows[0]["key"] == "hk_700" and rows[0]["total_shares"] == 1_005_000
    assert rows[1]["key"] == "hk_3690" and "error" in rows[1]

    # --text-json argument errors return structured JSON errors with non-zero exit code.
    bad_json_path = tmp_path / "bad_list.json"
    bad_json_path.write_text("[1, 2]", encoding="utf-8")
    for bad_args in (
        ["--text-json", str(tmp_path / "missing.json")],
        ["--text-json", str(bad_json_path)],
    ):
        bad_buf = StringIO()
        with redirect_stdout(bad_buf):
            rc_bad = bp.main(bad_args)
        assert rc_bad == 1
        err_payload = json.loads(bad_buf.getvalue())
        assert "error" in err_payload[0]

    fake_fetch = _patch_fetch(monkeypatch, lambda url, timeout=20, headers=None, min_interval=0.0: b"%PDF-1.4")
    fake_extract = create_autospec(bp.extract_pdf_text, return_value=cancel_text)
    monkeypatch.setattr(bp, "extract_pdf_text", fake_extract)
    url_buf = StringIO()
    with redirect_stdout(url_buf):
        rc_url = bp.main(["https://www.hkexnews.hk/listedco/listconews/sehk/2026/1116/sample.pdf"])
    assert rc_url == 0
    assert fake_fetch.call_count == 1
    assert fake_extract.call_count == 1
    url_rows = json.loads(url_buf.getvalue())
    assert url_rows[0]["total_shares"] == 1_005_000

    monkeypatch.setattr(bp, "extract_pdf_text", _load("buyback_dep_probe", SKILL_ROOT / "scripts" / "buyback_parse.py").extract_pdf_text)
    real_import = builtins.__import__

    def no_pdf_libs(name, *args, **kwargs):
        if name in {"pdfplumber", "pypdf"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pdf_libs)
    err_buf = StringIO()
    with redirect_stdout(err_buf):
        rc_err = bp.main(["https://www.hkexnews.hk/listedco/listconews/sehk/2026/1116/missing-dep.pdf"])
    assert rc_err == 0
    err_rows = json.loads(err_buf.getvalue())
    assert "缺少 PDF 解析依赖" in err_rows[0]["error"]


def test_buyback_parse_popmart_real_ff305_no_space() -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_popmart")
    bp = cli._locals()["buyback_parse"]

    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    parsed = bp.parse(popmart_text)
    assert "error" not in parsed
    assert parsed["stock_code"] == "09992"
    assert len(parsed["trades"]) == 1
    trade = parsed["trades"][0]
    assert trade["date"] == "2026-11-16"
    assert trade["shares"] == 340_000
    assert trade["hi"] == 144.8
    assert trade["lo"] == 143.4
    assert trade["amount"] == 48_972_020.0
    assert trade["currency"] == "HKD"

    assert parsed["total_shares"] == 340_000
    assert parsed["total_shares_raw"] == "340,000"
    assert parsed["total_amount"] == 48_972_020.0
    assert parsed["total_amount_raw"] == "48,972,020"
    assert parsed["currency"] == "HKD"
    assert parsed["hi"] == 144.8
    assert parsed["lo"] == 143.4
    assert parsed["to_cancel"] == 340_000
    assert parsed["to_treasury"] == 0
    assert parsed["mandate_cum_shares"] == 4_960_000
    assert parsed["mandate_cum_pct"] == 0.3699
    assert "48,972,020.00" not in parsed["line"]
    for needle in (
        "11-16 回购 340,000 股",
        "金额 48,972,020 港元",
        "均价 144.04 港元（区间 143.4–144.8）",
        "拟注销",
        "本次授权累计回购 4,960,000 股（占股本 0.3699%）",
    ):
        assert needle in parsed["line"]


def test_sats_ambiguous_cik_mapping_skips_and_marks_exact_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _cli("daily_filing_sats_cik")
    fetched_ciks: list[str] = []

    def route(url, timeout=20, headers=None, min_interval=0.0):
        if "company_tickers.json" in url:
            return _bytes({
                "0": {"ticker": "SATS", "cik_str": 1415404, "title": "ECHOSTAR CORP"},
                "1": {"ticker": "SATS", "cik_str": 1920406, "title": "STRIVE INC"},
                "2": {"ticker": "ECHO", "cik_str": 1415404, "title": "ECHOSTAR CORP"},
                "3": {"ticker": " AFRM ", "cik_str": 1820953, "title": "AFRM HOLDINGS"},
                "4": {"ticker": "SATS2", "cik_str": 9999999, "title": "OTHER CORP"},
            })
        if "/CIK" in url:
            fetched_ciks.append(url)
            return _bytes({"filings": {"recent": {
                "accessionNumber": [],
                "form": [],
                "filingDate": [],
                "items": [],
                "reportDate": [],
                "primaryDocument": [],
                "primaryDocDescription": [],
            }}})
        raise AssertionError(url)

    fake = _patch_fetch(monkeypatch, route)
    code, payload = _run(cli, [
        "--json", "scan", "--workspace-dir", str(tmp_path),
        "--tickers", "SATS,ECHO,AFRM,SAT", "--days", "1", "--no-enrich",
        "--output", "temp/daily-filing-alert/sats-check.json",
        "--as-of", "2026-11-17T20:30:00+08:00",
    ])
    assert code == 0 and payload["ok"] is True
    saved = json.loads((tmp_path / "temp/daily-filing-alert/sats-check.json").read_text(encoding="utf-8"))
    by_ticker = {row["ticker"]: row for row in saved["results"]}

    assert by_ticker["SATS"]["count"] == 0
    assert by_ticker["SATS"]["ambiguous_ciks"] == ["0001415404", "0001920406"]
    assert any("ambiguous CIK for SATS" in err for err in by_ticker["SATS"]["errors"])
    assert not any("0001920406" in url for url in fetched_ciks)

    assert by_ticker["ECHO"]["errors"] == []
    assert by_ticker["AFRM"]["errors"] == []
    assert any("0001415404" in url for url in fetched_ciks)
    assert any("0001820953" in url for url in fetched_ciks)
    assert any("no CIK for SAT" in err for err in by_ticker["SAT"]["errors"])

    # Cached v2 cik_map.json preserves ambiguous markers without hitting company_tickers.json again.
    initial_calls = fake.call_count
    code2, _payload2 = _run(cli, [
        "--json", "check", "--workspace-dir", str(tmp_path),
        "SATS", "--days", "1", "--no-enrich",
        "--as-of", "2026-11-17T20:30:00+08:00",
    ])
    assert code2 == 0
    assert fake.call_count == initial_calls

    # Expired v2 cache still protects ambiguous SATS when fetch fails; legacy v1 cache is rejected.
    cik_cache_path = tmp_path / "state" / "daily-filing-alert" / "cik_map.json"
    v2_blob = json.loads(cik_cache_path.read_text(encoding="utf-8"))
    v2_blob["fetched_at"] = "2020-01-01T00:00:00+08:00"
    cik_cache_path.write_text(json.dumps(v2_blob, ensure_ascii=False), encoding="utf-8")

    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise TimeoutError("sec down")

    cli_v2_stale = _cli("daily_filing_sats_v2_stale")
    _patch_fetch(monkeypatch, offline)
    code_v2, out_v2 = _run(cli_v2_stale, [
        "--json", "check", "--workspace-dir", str(tmp_path),
        "SATS", "--days", "1", "--no-enrich",
        "--as-of", "2026-11-17T20:30:00+08:00",
    ])
    assert code_v2 == 0 and out_v2["ambiguous_ciks"] == ["0001415404", "0001920406"]

    # Legacy v1 cache without version/ambiguous cannot bypass collision check when live fetch fails.
    cik_cache_path.write_text(json.dumps({
        "fetched_at": "2026-11-17T20:00:00+08:00",
        "map": {"SATS": "0001415404", "ECHO": "0001415404"},
    }, ensure_ascii=False), encoding="utf-8")
    cli_v1_stale = _cli("daily_filing_sats_v1_stale")
    _patch_fetch(monkeypatch, offline)
    code_v1, out_v1 = _run(cli_v1_stale, [
        "--json", "check", "--workspace-dir", str(tmp_path),
        "SATS", "--days", "1", "--no-enrich",
        "--as-of", "2026-11-17T20:30:00+08:00",
    ])
    assert code_v1 == 1 and out_v1["ok"] is False
    assert "SEC ticker map unavailable" in out_v1["message"]


def test_filing_card_purchase_buyback_and_lead_priority() -> None:
    filing_card = _load(
        "dalton_daily_filing_card_rules",
        SKILL_ROOT / "scripts" / "filing_card.py",
    )
    payload = {
        "date": "2026-11-16",
        "weekday": "周一",
        "window_note": "覆盖上周五至今",
        "lead": "NVDA 发布 8-K；3690 回购 5,000 万港元",
        "stats": {"us": {"scanned": 59}, "hk": {"scanned": 4}},
        "sections": [
            {
                "key": "us",
                "title": "美股 · SEC",
                "companies": [
                    {
                        "code": "NVDA",
                        "name": "NVDA",
                        "summary": "发布 8-K",
                        "items": [{"tag": "8-K", "text": "Item 8.01 其他事项", "date": "2026-11-16"}],
                    },
                    {
                        "code": "AFRM",
                        "name": "AFRM",
                        "summary": "CEO Jane Doe 买入 10,000 股，约 $500,000",
                        "items": [
                            {
                                "tag": "权益变动（Form 4 买入）",
                                "tx_type": "PURCHASE",
                                "code": "P",
                                "text": "Jane Doe (CEO) 买入 10,000 股，单价 $50.00",
                                "date": "2026-11-16",
                                "amount_usd": 500_000,
                            },
                            {
                                "tag": "Form 4",
                                "tx_type": "SALE",
                                "code": "S",
                                "text": "Bob Smith (CFO) 卖出 2,000 股，约 $100,000",
                                "date": "2026-11-16",
                                "amount_usd": 100_000,
                            },
                        ],
                    },
                    {
                        "code": "TINY",
                        "name": "TINY",
                        "summary": "高管卖出",
                        "items": [
                            {
                                "tag": "Form 4",
                                "tx_type": "SALE",
                                "text": "卖出 1,000 股，约 $50,000",
                                "date": "2026-11-16",
                                "amount_usd": 50_000,
                            }
                        ],
                    },
                ],
            },
            {
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [
                    {
                        "code": "3690",
                        "name": "美团",
                        "market": "hk",
                        "summary": "季度营收 100 亿港元；回购 0.5 亿港元",
                        "items": [
                            {
                                "id": "bb-3690",
                                "tag": "回购",
                                "title": "翌日披露報表",
                                "date": "2026-11-16",
                                "url": "https://www.hkexnews.hk/3690.pdf",
                            }
                        ],
                    },
                    {
                        "code": "0700",
                        "name": "腾讯控股",
                        "market": "hk",
                        "summary": "1 天合计回购 1,005,000 股，合计 630,450,000.00 港元，加权均价 627.31 港元",
                        "items": [
                            {
                                "id": "bb-0700",
                                "tag": "港股公告",
                                "title": "翌日披露報表",
                                "date": "2026-11-16",
                                "url": "https://www.hkexnews.hk/0700.pdf",
                            }
                        ],
                    },
                ],
            },
        ],
    }
    scan_data_r3 = {
        "buyback_results": {
            "bb-3690": {
                "id": "bb-3690",
                "code": "3690",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/3690.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-16", "shares": 500_000, "amount": 50_000_000.0}],
                    "total_shares": 500_000,
                    "total_shares_raw": "500,000",
                    "total_amount": 50_000_000.0,
                    "total_amount_raw": "50,000,000",
                    "currency": "HKD",
                    "avg_price": 100.0,
                    "lo": 99.0,
                    "hi": 101.0,
                }],
            },
            "bb-0700": {
                "id": "bb-0700",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/0700.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-16", "shares": 1_005_000, "amount": 630_450_000.0}],
                    "total_shares": 1_005_000,
                    "total_shares_raw": "1,005,000",
                    "total_amount": 630_450_000.0,
                    "total_amount_raw": "630,450,000.00",
                    "currency": "HKD",
                    "avg_price": 627.31,
                    "lo": 624.0,
                    "lo_raw": "624.00",
                    "hi": 631.5,
                    "hi_raw": "631.50",
                }],
            },
        }
    }
    card = filing_card.build_card(payload, scan_data=scan_data_r3)
    elements = card["body"]["elements"]
    # Largest buyback (0700, 6.30 亿港元) leads using structured fields, ignoring 3690's 100 亿港元 revenue in summary.
    lead_text = elements[0]["content"].strip("*")
    assert lead_text.startswith("腾讯控股 单日回购 6.30 亿港元；")
    assert len(lead_text) <= 40
    # Key tier has NVDA, AFRM (Form 4 buy), 3690, 0700 (4 companies); TINY (Form 4 sale) stays routine.
    assert "重点 4 · 例行 1" in card["header"]["text_tag_list"][0]["text"]["content"]
    # HKD buyback does not print fake USD total on company line.
    hk_blocks = [el["content"] for el in elements if el.get("tag") == "markdown" and "0700" in el.get("content", "")]
    assert hk_blocks and "万美元" not in hk_blocks[-1]

    # Mentioning company without buyback event does NOT bypass priority.
    bypass_card = filing_card.build_card({**payload, "lead": "0700 公布季报；NVDA 发布 8-K"}, scan_data=scan_data_r3)
    assert bypass_card["body"]["elements"][0]["content"].startswith("**腾讯控股 单日回购 6.30 亿港元；")

    # Priority events exist -> lead is generated completely by code, discarding model lead text.
    short_name_card = filing_card.build_card({**payload, "lead": "NVDA 发布 8-K；腾讯回购 6.30 亿港元"}, scan_data=scan_data_r3)
    short_lead = short_name_card["body"]["elements"][0]["content"]
    assert short_lead == "**腾讯控股 单日回购 6.30 亿港元；美团 单日回购 5,000 万港元**"

    # Without buybacks, Form 4 buy uses tx_type/code (no text guessing) -> 内部人买入;
    # A-share 增持 -> 增持; generic 权益变动/减持 is NOT treated as purchase.
    no_buyback_payload = {
        "date": "2026-11-16",
        "sections": [payload["sections"][0]],
    }
    no_bb_card = filing_card.build_card(no_buyback_payload)
    assert "AFRM 内部人买入 50.0 万美元" in no_bb_card["body"]["elements"][0]["content"]

    # Plain Form 4 without tx_type/code is NOT guessed as purchase even if text contains 买入.
    no_guess_card = filing_card.build_card({
        "date": "2026-11-16",
        "sections": [{
            "key": "us",
            "title": "美股",
            "companies": [{
                "code": "NOPE",
                "name": "NOPE",
                "items": [{"tag": "Form 4", "text": "买入 10,000 股，约 $500,000", "amount_usd": 500_000}],
            }],
        }],
    })
    assert "重点" not in no_guess_card["header"]["text_tag_list"][0]["text"]["content"]

    # A/HK stake increase uses 增持 in lead, and 权益变动/减持 is never treated as purchase.
    cn_stake_card = filing_card.build_card({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "items": [{"tag": "权益变动", "text": "股东减持 300 万股", "date": "2026-11-16"}],
                },
                {
                    "code": "300308",
                    "name": "中际旭创",
                    "items": [{"tag": "增持", "text": "控股股东增持 100 万股", "date": "2026-11-16"}],
                },
            ],
        }],
    })
    assert cn_stake_card["body"]["elements"][0]["content"] == "**中际旭创 增持**"

    # Priority events in lead are always generated by code from structured data (not model phrasing).
    ok_lead_payload = {
        **payload,
        "lead": "0700 腾讯控股回购 6.30 亿港元；AFRM 高管买入 50 万美元",
    }
    ok_card = filing_card.build_card(ok_lead_payload, scan_data=scan_data_r3)
    assert ok_card["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 6.30 亿港元；美团 单日回购 5,000 万港元**"


def test_filing_card_lead_override_and_form4_known_tx_priority() -> None:
    filing_card = _load(
        "dalton_daily_filing_card_r4",
        SKILL_ROOT / "scripts" / "filing_card.py",
    )
    single_bb_payload = {
        "date": "2026-11-16",
        "sections": [
            {
                "key": "us",
                "title": "美股 · SEC",
                "companies": [
                    {
                        "code": "NVDA",
                        "name": "NVDA",
                        "items": [{"tag": "8-K", "text": "Item 8.01 其他事项", "date": "2026-11-16"}],
                    },
                    {
                        "code": "AFRM",
                        "name": "AFRM",
                        "items": [{
                            "tag": "权益变动（Form 4 买入）",
                            "tx_type": "PURCHASE",
                            "code": "P",
                            "text": "买入 10,000 股",
                            "date": "2026-11-16",
                            "amount_usd": 500_000,
                        }],
                    },
                ],
            },
            {
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [
                    {
                        "code": "0700",
                        "name": "腾讯控股",
                        "market": "hk",
                        "items": [{
                            "id": "bb-0700-r4",
                            "tag": "港股公告",
                            "title": "翌日披露報表",
                            "date": "2026-11-16",
                            "url": "https://www.hkexnews.hk/0700-r4.pdf",
                        }],
                    },
                ],
            },
        ],
    }
    scan_data_r4 = {
        "buyback_results": {
            "bb-0700-r4": {
                "id": "bb-0700-r4",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/0700-r4.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-16", "shares": 1_005_000, "amount": 630_450_000.0}],
                    "total_shares": 1_005_000,
                    "total_shares_raw": "1,005,000",
                    "total_amount": 630_450_000.0,
                    "total_amount_raw": "630,450,000.00",
                    "currency": "HKD",
                }],
            }
        }
    }

    # Case 1: Model writes conflicting amount or extra clause (`0700 回购，金额 100 亿港元` / `NVDA 发布 8-K`) ->
    # entire lead is code-generated from structured priority events; model lead text is discarded.
    wrong_amt_card = filing_card.build_card({**single_bb_payload, "lead": "0700 回购 100 亿港元；NVDA 发布 8-K"}, scan_data=scan_data_r4)
    assert wrong_amt_card["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 6.30 亿港元；AFRM 内部人买入 50.0 万美元**"

    # Case 2: Wrong event order (comma-separated `NVDA 发布 8-K，0700 回购 6.30 亿港元`, or Form 4 buy before buyback).
    wrong_order_card = filing_card.build_card({
        **single_bb_payload,
        "lead": "NVDA 发布 8-K，0700 回购 6.30 亿港元",
    }, scan_data=scan_data_r4)
    assert wrong_order_card["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 6.30 亿港元；AFRM 内部人买入 50.0 万美元**"

    wrong_buy_first = filing_card.build_card({
        **single_bb_payload,
        "lead": "AFRM 高管买入 50 万美元；0700 回购 6.30 亿港元",
    }, scan_data=scan_data_r4)
    assert wrong_buy_first["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 6.30 亿港元；AFRM 内部人买入 50.0 万美元**"

    # Without AFRM, code generates only the buyback lead (no model clauses kept when priority event exists).
    bb_and_nvda = {
        "date": "2026-11-16",
        "sections": [
            {
                "key": "us",
                "title": "美股 · SEC",
                "companies": [single_bb_payload["sections"][0]["companies"][0]],
            },
            single_bb_payload["sections"][1],
        ],
    }
    conflict_clause = filing_card.build_card({**bb_and_nvda, "lead": "0700 回购，金额 100 亿港元"}, scan_data=scan_data_r4)
    assert conflict_clause["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 6.30 亿港元**"

    reordered = filing_card.build_card({**bb_and_nvda, "lead": "NVDA 发布 8-K，0700 回购 6.30 亿港元"}, scan_data=scan_data_r4)
    assert reordered["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 6.30 亿港元**"

    # When NO priority events exist (only 8-K), model lead IS used (capped at 40 chars).
    no_priority_card = filing_card.build_card({
        "date": "2026-11-16",
        "lead": "NVDA 发布 8-K 重大事项",
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [single_bb_payload["sections"][0]["companies"][0]],
        }],
    })
    assert no_priority_card["body"]["elements"][0]["content"] == "**NVDA 发布 8-K 重大事项**"

    # Form 4 known tx_type/code (e.g. DERIVATIVE ACQUISITION, code A) never triggers A/HK 增持 classification.
    form4_grant_card = filing_card.build_card({
        "date": "2026-11-16",
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [{
                "code": "ACME",
                "name": "ACME",
                "items": [{
                    "tag": "Form 4",
                    "tx_type": "DERIVATIVE ACQUISITION",
                    "code": "A",
                    "text": "股权激励增持 10,000 股",
                    "date": "2026-11-16",
                    "amount_usd": 200_000,
                }],
            }],
        }],
    })
    assert "重点" not in form4_grant_card["header"]["text_tag_list"][0]["text"]["content"]
    assert "例行" in form4_grant_card["header"]["text_tag_list"][0]["text"]["content"]
    assert all("增持" not in el.get("content", "") for el in form4_grant_card["body"]["elements"] if el.get("tag") == "markdown")

    # Even without tx_type/code, US items never use A/HK 增持 text heuristic.
    assert filing_card.is_stake_increase_item({"tag": "Form 4", "text": "增持 10,000 股"}) is False
    assert filing_card.is_stake_increase_item({"tag": "公告", "text": "增持 10,000 股"}, market="us") is False
    assert filing_card.is_stake_increase_item({"tag": "A股公告", "text": "控股股东增持 100 万股"}, market="cnhk") is True


def test_buyback_parse_strict_numbers_amendments_a_share_validation_and_verbatim_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r5")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]

    # Cross-announcement aggregation is removed.
    assert not hasattr(bp, "aggregate_company_buybacks")

    # 1. Missing 合共 shares or amount MUST return error (never sum inline rows).
    treasury_text = (fixtures / "ff305_hk_buyback_treasury.txt").read_text(encoding="utf-8")
    no_tot_sh = treasury_text.replace("合共購回股份總數 130,000\n", "")
    err_sh = bp.parse(no_tot_sh)
    assert "error" in err_sh and "total_shares" not in err_sh

    no_tot_amt = treasury_text.replace("合共付出的價格總額 (元) HKD 15,630,000\n", "")
    err_amt = bp.parse(no_tot_amt)
    assert "error" in err_amt and "total_amount" not in err_amt

    # Wrapped 合共 labels across newlines still work when numbers themselves are intact.
    wrapped_label_with_gap = (
        treasury_text
        .replace("2). 2026年11月16日 80,000 於本交易所進行", "2). 2026年11月16日 80,000 分页打断")
        .replace("合共購回股份總數 130,000", "合共購回股份\n總數 130,000")
        .replace("合共付出的價格總額 (元) HKD 15,630,000", "合共付出的價格\n總額 (元) HKD 15,630,000")
    )
    err_gap = bp.parse(wrapped_label_with_gap)
    assert "error" in err_gap and "discrepancy" in err_gap and "total_shares" not in err_gap

    wrapped_label_ok = (
        treasury_text
        .replace("合共購回股份總數 130,000", "合共購回股份\n總數 130,000")
        .replace("合共付出的價格總額 (元) HKD 15,630,000", "合共付出的價格\n總額 (元) HKD 15,630,000")
    )
    ok_wrapped_label = bp.parse(wrapped_label_ok)
    assert ok_wrapped_label["total_shares"] == 130_000
    assert ok_wrapped_label["total_amount"] == 15_630_000.0

    # 2. Strict numbers only: any cross-line/split/incomplete number MUST return error (no rejoining).
    for broken_ndr in (
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\n631549.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794,631,549\n.33"),
        ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794,631,549.\n33"),
        ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份總數 856\n980").replace(
            "購回股份（擬持作庫存股份） 856,980", "購回股份（擬持作庫存股份） 856\n980"
        ),
        ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份總數 856,\n980"),
        ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", "合共購回股份總數 856 980"),
    ):
        err_broken = bp.parse(broken_ndr)
        assert "error" in err_broken and "total_shares" not in err_broken and "total_amount" not in err_broken

    for bad_token in ("856,", "856,\n98", "1234,567", "856,980.", ".33", "856\n,980"):
        bad_sample = ndr.SAMPLE_TREASURY.replace("合共購回股份總數 856,980", f"合共購回股份總數 {bad_token}")
        bad_res = bp.parse(bad_sample)
        assert "error" in bad_res and "total_shares" not in bad_res, bad_token

    bad_disp = treasury_text.replace("購回股份（擬持作庫存股份） 100,000", "購回股份（擬持作庫存股份） 90,000")
    err_disp = bp.parse(bad_disp)
    assert "error" in err_disp and err_disp["discrepancy"]["field"] == "share_disposition"

    # 3. Amendment rule: when a company has a buyback filing with 更正/修訂/補充 in title,
    # only the amendment is kept (both in scan-cn-hk and in filing_card).
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    popmart_orig = bp.parse(popmart_text)
    popmart_corr_text = popmart_text.replace("340,000", "350,000").replace("48,972,020", "50,400,000")
    popmart_corr = bp.parse(popmart_corr_text)
    assert popmart_corr["total_amount"] == 50_400_000.0

    for amend_title in ("翌日披露報表（更正）", "翌日披露報表（修訂）", "翌日披露報表（補充）"):
        scan_r5_amend = {
            "buyback_results": {
                "pm-orig": bp.scan_record_from_parsed(popmart_orig, ann_id="pm-orig", code="09992", url="https://www.hkexnews.hk/pm-orig.pdf", title="翌日披露報表"),
                "pm-corr": bp.scan_record_from_parsed(popmart_corr, ann_id="pm-corr", code="09992", url="https://www.hkexnews.hk/pm-corr.pdf", title=amend_title),
            }
        }
        card = fc.build_card({
            "date": "2026-11-17",
            "lead": "09992 回购 9,937 万港元",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "09992",
                    "name": "泡泡玛特",
                    "market": "hk",
                    "summary": popmart_corr["line"],
                    "items": [
                        {
                            "id": "pm-orig",
                            "tag": "回购",
                            "title": "翌日披露報表",
                            "date": "2026-11-16",
                            "url": "https://www.hkexnews.hk/pm-orig.pdf",
                        },
                        {
                            "id": "pm-corr",
                            "tag": "回购",
                            "title": amend_title,
                            "date": "2026-11-17",
                            "url": "https://www.hkexnews.hk/pm-corr.pdf",
                        },
                        {
                            "tag": "港股公告",
                            "title": "季度业绩公告",
                            "text": "披露季度业绩",
                            "date": "2026-11-17",
                        },
                    ],
                }],
            }],
        }, scan_data=scan_r5_amend)
        lead_content = card["body"]["elements"][0]["content"]
        assert lead_content == "**泡泡玛特 单日回购 5,040 万港元**"
        rendered_md = "\n".join(
            el.get("content", "") for el in card["body"]["elements"] if el.get("tag") == "markdown"
        )
        assert "50,400,000 港元" in rendered_md
        assert "48,972,020 港元" not in rendered_md
        assert "季度业绩" in rendered_md

    # scan-cn-hk filters original buybacks when an amendment buyback exists for the same company.
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    raw_amend_path = tmp_path / "temp" / "daily-filing-alert" / "amend-raw.json"
    raw_amend_path.parent.mkdir(parents=True, exist_ok=True)
    raw_amend_path.write_text(json.dumps({
        "end": "2026-11-17",
        "limit": 50,
        "calls": [
            {"ticker": "09992", "ok": True, "rows": [
                {"date": "2026-11-16", "title": "翌日披露報表", "pdf_link": "https://www.hkexnews.hk/orig.pdf"},
                {"date": "2026-11-17", "title": "翌日披露報表（更正）", "pdf_link": "https://www.hkexnews.hk/corr.pdf"},
                {"date": "2026-11-17", "title": "业绩公告", "pdf_link": "https://www.hkexnews.hk/earn.pdf"},
            ]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    amend_out = "temp/daily-filing-alert/amend-out.json"
    rc_am, payload_am = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", "temp/daily-filing-alert/amend-raw.json",
        "--output", amend_out, "--as-of", "2026-11-17T20:30:00+08:00",
    ])
    assert rc_am == 0 and payload_am["ok"] is True
    am_saved = json.loads((tmp_path / amend_out).read_text(encoding="utf-8"))
    pop_res = next(r for r in am_saved["results"] if r["ticker"] == "9992")
    assert [a["title"] for a in pop_res["announcements"]] == ["翌日披露報表（更正）", "业绩公告"]
    assert any("2026-11-16 翌日披露報表" in err for err in pop_res["errors"])
    assert any("9992:" in err and "2026-11-16 翌日披露報表" in err for err in am_saved["day_errors"])
    assert payload_am["day_errors"] == am_saved["day_errors"]

    # 4. A-share _apply_a_share_body uses the same validated parser; validation failure sets enrich_error and no digest.
    missing_amt_ndr = ndr.SAMPLE_TREASURY.replace("合共付出的價格總額 (元) RMB 794,631,549.33\n", "")
    broken_num_ndr = ndr.SAMPLE_TREASURY.replace("794,631,549.33", "794\n631549.33")
    multi_ccy_ndr_ok = """翌日披露報表
公司名稱： 示例双币（「本公司」）
呈交日期： 2026年11月16日
第一章節
1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300308 說明 A股
A. 已發行股份或庫存股份變動
於下列日期開始時的結存(註1) 2026年11月15日 1,000,000 0 1,000,000
於下列日期結束時的結存 (註5及6) 2026年11月16日 1,000,000 0 1,000,000
第二章節
1. 股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300308 說明 A股
A. 購回報告
1). 2026年11月16日 10,000 於其他證券交易所進行 RMB 10.00 RMB 9.50 RMB 98,000.00
深圳證券交易所
2). 2026年11月16日 20,000 於其他證券交易所進行 HKD 11.00 HKD 10.50 HKD 215,000.00
香港聯合交易所
合共購回股份總數 30,000
合共付出的價格總額 (元) RMB 98,000.00
合共付出的價格總額 (元) HKD 215,000.00
購回股份（擬註銷）數目 30,000
購回股份（擬持作庫存股份） 0
"""
    multi_ccy_ndr_missing = multi_ccy_ndr_ok.replace("合共付出的價格總額 (元) HKD 215,000.00\n", "")

    with pytest.raises(ValueError, match="合共"):
        ndr.parse_ndr(missing_amt_ndr)
    with pytest.raises(ValueError, match="数字格式"):
        ndr.parse_ndr(broken_num_ndr)
    with pytest.raises(ValueError, match="币种不完整"):
        ndr.parse_ndr(multi_ccy_ndr_missing)

    parsed_multi_ndr = ndr.parse_ndr(multi_ccy_ndr_ok)
    assert len(parsed_multi_ndr["buybacks"]) == 2
    assert "9.5至10元" in parsed_multi_ndr["digest"]
    assert "10.5至11港元" in parsed_multi_ndr["digest"]
    assert "98,000.00元人民币" in parsed_multi_ndr["digest"]
    assert "215,000.00港元" in parsed_multi_ndr["digest"]
    assert "约10万" not in parsed_multi_ndr["digest"] and "22万" not in parsed_multi_ndr["digest"]

    raw_a_path = tmp_path / "temp" / "daily-filing-alert" / "a-val-raw.json"
    raw_a_path.write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [
            {"ticker": "300308", "ok": True, "rows": [
                {
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表（缺合共）",
                    "pdf_link": "https://static.cninfo.com.cn/noamt.pdf",
                    "notice_text": missing_amt_ndr,
                },
                {
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表（断行数字）",
                    "pdf_link": "https://static.cninfo.com.cn/broken.pdf",
                    "notice_text": broken_num_ndr,
                },
                {
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表（多币种缺合计）",
                    "pdf_link": "https://static.cninfo.com.cn/mccy-bad.pdf",
                    "notice_text": multi_ccy_ndr_missing,
                },
            ]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    a_val_out = "temp/daily-filing-alert/a-val-out.json"
    rc_av, payload_av = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", "temp/daily-filing-alert/a-val-raw.json",
        "--output", a_val_out, "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_av == 0 and payload_av["ok"] is True
    av_saved = json.loads((tmp_path / a_val_out).read_text(encoding="utf-8"))
    a_anns = next(r for r in av_saved["results"] if r["ticker"] == "300308")["announcements"]
    assert len(a_anns) == 3
    for ann in a_anns:
        assert ann["enrich_error"].startswith("翌日披露报表解析失败")
        assert "digest" not in ann
        assert "ndr" not in ann
        assert "body_excerpt" not in ann

    # 5. Verbatim PDF 合共 total amount is preserved (not replaced by sum of trade rows).
    popmart_01 = bp.parse(
        popmart_text.replace("合共付出的價格總額 (元) HKD 48,972,020", "合共付出的價格總額 (元) HKD 48,972,020.01")
    )
    assert popmart_01["total_amount"] == 48_972_020.01
    assert popmart_01["total_amount_raw"] == "48,972,020.01"
    assert "48,972,020.01 港元" in popmart_01["line"]
    card_01 = fc.build_card({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "id": "pm-01",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/pm-01.pdf",
                }],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "pm-01": bp.scan_record_from_parsed(popmart_01, ann_id="pm-01", code="09992", url="https://www.hkexnews.hk/pm-01.pdf", title="翌日披露報表"),
        }
    })
    card_01_md = "\n".join(
        el.get("content", "") for el in card_01["body"]["elements"] if el.get("tag") == "markdown"
    )
    assert "48,972,020.01 港元" in card_01_md


def test_r6_unicode_ws_lead_no_text_extract_summary_rewrite_scan_keywords_usd_no_sum_and_a_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r6")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]

    # 1. Unicode whitespace characters (\u00a0 NBSP, \u202f narrow NBSP, \u3000 fullwidth space, \f form feed, \u200b)
    #    breaking any number MUST return error (both in buyback_parse and cn_ndr).
    for sep in ("\u00a0", "\u202f", "\u3000", "\f", "\u200b"):
        broken_amt = ndr.SAMPLE_TREASURY.replace("794,631,549.33", f"794{sep}631549.33")
        err_amt = bp.parse(broken_amt)
        assert "error" in err_amt and "total_amount" not in err_amt, repr(sep)
        with pytest.raises(ValueError, match="数字格式"):
            ndr.parse_ndr(broken_amt)

        broken_shares = (
            ndr.SAMPLE_TREASURY
            .replace("合共購回股份總數 856,980", f"合共購回股份總數 856{sep}980")
            .replace("購回股份（擬持作庫存股份） 856,980", f"購回股份（擬持作庫存股份） 856{sep}980")
        )
        err_sh = bp.parse(broken_shares)
        assert "error" in err_sh and "total_shares" not in err_sh, repr(sep)
        with pytest.raises(ValueError, match="数字格式"):
            ndr.parse_ndr(broken_shares)

    # 2. Lead never extracts amounts from text/title when structured validated total is absent.
    lead_no_struct_card = fc.build_card({
        "date": "2026-11-17",
        "lead": "09992 回购 0.0 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "tag": "回购",
                    "title": "以每股最高150港元回购股份的公告",
                    "text": "以每股最高150港元回购股份的公告",
                    "date": "2026-11-17",
                }],
            }],
        }],
    })
    assert lead_no_struct_card["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"

    # 3. Filtering original buyback strips/regenerates summary references to dropped announcement.
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    popmart_orig = bp.parse(popmart_text)
    popmart_corr = bp.parse(popmart_text.replace("340,000", "350,000").replace("48,972,020", "50,400,000"))

    scan_r6_amend = {
        "buyback_results": {
            "r6-orig": bp.scan_record_from_parsed(popmart_orig, ann_id="r6-orig", code="09992", url="https://www.hkexnews.hk/r6-orig.pdf", title="翌日披露報表"),
            "r6-corr": bp.scan_record_from_parsed(popmart_corr, ann_id="r6-corr", code="09992", url="https://www.hkexnews.hk/r6-corr.pdf", title="翌日披露報表（更正）"),
        }
    }
    for stale_summary in (
        f"11-16 回购 340,000 股，金额 48,972,020 港元；披露季度业绩",
        f"11-16 回购 48,972,020 港元，后更正为 50,400,000 港元并披露季度业绩",
        f"两日累计涉及 9,937 万港元；披露季度业绩",
    ):
        filtered_card = fc.build_card({
            "date": "2026-11-17",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "09992",
                    "name": "泡泡玛特",
                    "market": "hk",
                    "summary": stale_summary,
                    "items": [
                        {
                            "id": "r6-orig",
                            "tag": "回购",
                            "title": "翌日披露報表",
                            "date": "2026-11-16",
                            "url": "https://www.hkexnews.hk/r6-orig.pdf",
                        },
                        {
                            "id": "r6-corr",
                            "tag": "回购",
                            "title": "翌日披露報表（更正）",
                            "date": "2026-11-17",
                            "url": "https://www.hkexnews.hk/r6-corr.pdf",
                        },
                        {
                            "tag": "港股公告",
                            "title": "季度业绩公告",
                            "text": "披露季度业绩",
                            "date": "2026-11-17",
                        },
                    ],
                }],
            }],
        }, scan_data=scan_r6_amend)
        md_all = "\n".join(
            el.get("content", "") for el in filtered_card["body"]["elements"] if el.get("tag") == "markdown"
        )
        assert "48,972,020" not in md_all
        assert "340,000" not in md_all
        assert "9,937" not in md_all
        assert "50,400,000 港元" in md_all
        assert "披露季度业绩" in md_all

    # 4. scan-cn-hk recognizes simplified "回购" in announcement title and filters original when amendment exists.
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    raw_repo = tmp_path / "temp" / "daily-filing-alert" / "r6-repurchase.json"
    raw_repo.parent.mkdir(parents=True, exist_ok=True)
    raw_repo.write_text(json.dumps({
        "end": "2026-11-17",
        "limit": 50,
        "calls": [
            {"ticker": "300308", "ok": True, "rows": [
                {"date": "2026-11-17", "title": "关于回购股份的公告", "pdf_link": "https://static.cninfo.com.cn/orig.pdf"},
                {"date": "2026-11-17", "title": "关于回购股份的更正公告", "pdf_link": "https://static.cninfo.com.cn/corr.pdf"},
                {"date": "2026-11-17", "title": "关于召开股东大会的通知", "pdf_link": "https://static.cninfo.com.cn/egm.pdf"},
            ]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    out_repo = "temp/daily-filing-alert/r6-repurchase-out.json"
    rc_repo, payload_repo = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", "temp/daily-filing-alert/r6-repurchase.json",
        "--output", out_repo, "--as-of", "2026-11-17T20:30:00+08:00",
    ])
    assert rc_repo == 0 and payload_repo["ok"] is True
    repo_saved = json.loads((tmp_path / out_repo).read_text(encoding="utf-8"))
    a_300308 = next(r for r in repo_saved["results"] if r["ticker"] == "300308")["announcements"]
    assert [a["title"] for a in a_300308] == ["关于回购股份的更正公告", "关于召开股东大会的通知"]

    # 5. USD buybacks do NOT aggregate across announcements on company title; Form 4 keeps aggregating.
    usd_scan_r6 = {
        "buyback_results": {
            "usd-1": {
                "id": "usd-1",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/usd-1.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-15", "shares": 100_000, "amount": 10_000_000.0}],
                    "total_shares": 100_000,
                    "total_shares_raw": "100,000",
                    "total_amount": 10_000_000.0,
                    "total_amount_raw": "10,000,000",
                    "currency": "USD",
                    "avg_price": 100.0,
                }],
            },
            "usd-2": {
                "id": "usd-2",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/usd-2.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-16", "shares": 200_000, "amount": 20_000_000.0}],
                    "total_shares": 200_000,
                    "total_shares_raw": "200,000",
                    "total_amount": 20_000_000.0,
                    "total_amount_raw": "20,000,000",
                    "currency": "USD",
                    "avg_price": 100.0,
                }],
            },
        }
    }
    usd_card = fc.build_card({
        "date": "2026-11-17",
        "sections": [
            {
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "0700",
                    "name": "腾讯控股",
                    "market": "hk",
                    "items": [
                        {
                            "id": "usd-1",
                            "tag": "回购",
                            "title": "翌日披露報表",
                            "date": "2026-11-15",
                            "url": "https://www.hkexnews.hk/usd-1.pdf",
                        },
                        {
                            "id": "usd-2",
                            "tag": "回购",
                            "title": "翌日披露報表",
                            "date": "2026-11-16",
                            "url": "https://www.hkexnews.hk/usd-2.pdf",
                        },
                    ],
                }],
            },
            {
                "key": "us",
                "title": "美股 · SEC",
                "companies": [{
                    "code": "SELL",
                    "name": "SELL",
                    "items": [
                        {"tag": "Form 4", "text": "高管 A 卖出，约 1,000 万美元", "date": "2026-11-16", "amount_usd": 10_000_000},
                        {"tag": "Form 4", "text": "高管 B 卖出，约 2,000 万美元", "date": "2026-11-17", "amount_usd": 20_000_000},
                    ],
                }],
            },
        ],
    }, scan_data=usd_scan_r6)
    usd_blocks = [el.get("content", "") for el in usd_card["body"]["elements"] if el.get("tag") == "markdown"]
    tencent_block = next(b for b in usd_blocks if "**0700**" in b)
    sell_block = next(b for b in usd_blocks if "**SELL**" in b)
    assert "3,000 万美元" not in tencent_block
    assert "合计" not in tencent_block.splitlines()[0]
    assert "10,000,000 美元" in tencent_block
    assert "20,000,000 美元" in tencent_block
    assert "合计 3,000 万美元" in sell_block.splitlines()[0]

    # 6. A-share digest uses verbatim total_amount_raw without approximate numbers.
    parsed_ndr = ndr.parse_ndr(ndr.SAMPLE_TREASURY)
    assert "794,631,549.33元人民币" in parsed_ndr["digest"]
    assert "7.95亿" not in parsed_ndr["digest"]


def test_r7_amendment_dropped_errors_is_amendment_item_and_single_day_lead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r7")
    bp = cli._locals()["buyback_parse"]
    fc = cli._locals()["filing_card"]

    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)

    # 1. Multi-day window: an amendment NDR filters out an earlier day's NDR ->
    #    dropped filing is recorded in that ticker's errors and day_errors.
    raw_path = tmp_path / "temp" / "daily-filing-alert" / "r7-amend-gap.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [
            {"ticker": "00700", "ok": True, "rows": [
                {"date": "2026-11-13", "title": "翌日披露報表", "pdf_link": "https://www.hkexnews.hk/fri.pdf"},
                {"date": "2026-11-16", "title": "翌日披露報表（期權行使更正）", "pdf_link": "https://www.hkexnews.hk/mon-corr.pdf"},
            ]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    out_rel = "temp/daily-filing-alert/r7-amend-gap-out.json"
    rc, payload_out = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "3", "--input", "temp/daily-filing-alert/r7-amend-gap.json",
        "--output", out_rel, "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc == 0 and payload_out["ok"] is True
    saved = json.loads((tmp_path / out_rel).read_text(encoding="utf-8"))
    tx_res = next(r for r in saved["results"] if r["ticker"] == "0700")
    assert [a["title"] for a in tx_res["announcements"]] == ["翌日披露報表（期權行使更正）"]
    assert len(tx_res["errors"]) == 1
    assert "2026-11-13 翌日披露報表" in tx_res["errors"][0]
    assert any(err.startswith("0700:") and "2026-11-13 翌日披露報表" in err for err in saved["day_errors"])
    assert payload_out["day_errors"] == saved["day_errors"]

    # 2. Item carrying is_amendment=True (from buyback_parse) filters non-amendment items
    #    even when title lacks 更正/修訂/補充; lead uses 单日回购.
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    orig = bp.parse(popmart_text)
    corr_text = (
        popmart_text
        .replace("340,000", "350,000")
        .replace("48,972,020", "50,400,000")
        .replace("狀態： 新提交", "狀態： 更正")
        .replace("呈交日期： 2026年11月16日", "呈交日期： 2026年11月17日")
    )
    corr = bp.parse(corr_text)
    assert corr.get("is_amendment") is True

    card = fc.build_card({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": f"{orig['line']}；{corr['line']}",
                "items": [
                    {
                        "id": "r7-orig",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/r7-orig.pdf",
                    },
                    {
                        "id": "r7-corr",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-17",
                        "url": "https://www.hkexnews.hk/r7-corr.pdf",
                    },
                ],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r7-orig": bp.scan_record_from_parsed(orig, ann_id="r7-orig", code="09992", url="https://www.hkexnews.hk/r7-orig.pdf", title="翌日披露報表"),
            "r7-corr": bp.scan_record_from_parsed(corr, ann_id="r7-corr", code="09992", url="https://www.hkexnews.hk/r7-corr.pdf", title="翌日披露報表"),
        }
    })
    assert card["body"]["elements"][0]["content"] == "**泡泡玛特 单日回购 5,040 万港元**"
    md_text = "\n".join(el.get("content", "") for el in card["body"]["elements"] if el.get("tag") == "markdown")
    assert "50,400,000 港元" in md_text
    assert "48,972,020" not in md_text

    # 3. Multiple non-amendment buybacks use 单日回购 with max single-filing amount (not window sum).
    multi_bb_card = fc.build_card({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "0700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [
                    {
                        "id": "r7-m1",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-15",
                        "url": "https://www.hkexnews.hk/r7-m1.pdf",
                    },
                    {
                        "id": "r7-m2",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/r7-m2.pdf",
                    },
                ],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r7-m1": {
                "id": "r7-m1",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/r7-m1.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-15", "shares": 100_000, "amount": 10_000_000.0}],
                    "total_shares": 100_000,
                    "total_shares_raw": "100,000",
                    "total_amount": 10_000_000.0,
                    "total_amount_raw": "10,000,000",
                    "currency": "HKD",
                }],
            },
            "r7-m2": {
                "id": "r7-m2",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/r7-m2.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [{
                    "trades": [{"date": "2026-11-16", "shares": 200_000, "amount": 20_000_000.0}],
                    "total_shares": 200_000,
                    "total_shares_raw": "200,000",
                    "total_amount": 20_000_000.0,
                    "total_amount_raw": "20,000,000",
                    "currency": "HKD",
                }],
            },
        }
    })
    assert multi_bb_card["body"]["elements"][0]["content"] == "**腾讯控股 单日回购 2,000 万港元**"


def test_r8_summary_multiclass_multiday_option_ndr_and_render_gap_notes(tmp_path: Path) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r8")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]

    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    orig = bp.parse(popmart_text)
    corr = bp.parse(
        popmart_text
        .replace("340,000", "350,000")
        .replace("48,972,020", "50,400,000")
        .replace("狀態： 新提交", "狀態： 更正")
        .replace("呈交日期： 2026年11月16日", "呈交日期： 2026年11月17日")
    )

    # 1. Buyback company summary is ALWAYS generated by code (ignoring Chinese-numeral stale model summary),
    #    both with and without amendment filtering; non-buyback companies keep model summary.
    scan_r8_sum = {
        "buyback_results": {
            "r8-orig": bp.scan_record_from_parsed(orig, ann_id="r8-orig", code="09992", url="https://www.hkexnews.hk/r8-orig.pdf", title="翌日披露報表"),
            "r8-corr": bp.scan_record_from_parsed(corr, ann_id="r8-corr", code="09992", url="https://www.hkexnews.hk/r8-corr.pdf", title="翌日披露報表"),
        }
    }
    for items in (
        [
            {
                "id": "r8-orig",
                "tag": "回购",
                "title": "翌日披露報表",
                "date": "2026-11-16",
                "url": "https://www.hkexnews.hk/r8-orig.pdf",
            },
            {
                "id": "r8-corr",
                "tag": "回购",
                "title": "翌日披露報表",
                "date": "2026-11-17",
                "url": "https://www.hkexnews.hk/r8-corr.pdf",
            },
            {
                "tag": "港股公告",
                "title": "季度业绩公告",
                "text": "披露季度业绩",
                "date": "2026-11-17",
            },
        ],
        [
            {
                "id": "r8-corr",
                "tag": "回购",
                "title": "翌日披露報表",
                "date": "2026-11-17",
                "url": "https://www.hkexnews.hk/r8-corr.pdf",
            },
            {
                "tag": "港股公告",
                "title": "季度业绩公告",
                "text": "披露季度业绩",
                "date": "2026-11-17",
            },
        ],
    ):
        card_sum = fc.build_card({
            "date": "2026-11-17",
            "sections": [
                {
                    "key": "cnhk",
                    "title": "A股 / 港股",
                    "companies": [{
                        "code": "09992",
                        "name": "泡泡玛特",
                        "market": "hk",
                        "summary": "耗资四千八百九十七万二千零二十港元；披露季度业绩",
                        "items": items,
                    }],
                },
                {
                    "key": "us",
                    "title": "美股 · SEC",
                    "companies": [{
                        "code": "NVDA",
                        "name": "NVDA",
                        "summary": "模型保留的常规公告摘要",
                        "items": [
                            {"tag": "8-K", "text": "Item 8.01 合作进展", "date": "2026-11-17"},
                            {"tag": "10-Q", "text": "三季报", "date": "2026-11-17"},
                        ],
                    }],
                },
            ],
        }, scan_data=scan_r8_sum)
        md_all = "\n".join(el.get("content", "") for el in card_sum["body"]["elements"] if el.get("tag") == "markdown")
        assert "四千八百九十七万" not in md_all
        assert "48,972,020" not in md_all
        assert "50,400,000 港元" in md_all
        assert "披露季度业绩" in md_all
        assert "模型保留的常规公告摘要" in md_all

    # 2. Multi-share-class groups are NOT summed; lead uses largest single group + share-class label.
    multi_class_card = fc.build_card({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "0700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "r8-mc",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/r8-mc.pdf",
                }],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r8-mc": {
                "id": "r8-mc",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/r8-mc.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [
                    {
                        "share_class": "A",
                        "trades": [{"date": "2026-11-17", "shares": 10_000, "amount": 1_000_000.0}],
                        "total_shares": 10_000,
                        "total_shares_raw": "10,000",
                        "total_amount": 1_000_000.0,
                        "total_amount_raw": "1,000,000",
                        "currency": "HKD",
                    },
                    {
                        "share_class": "H",
                        "trades": [{"date": "2026-11-17", "shares": 20_000, "amount": 2_000_000.0}],
                        "total_shares": 20_000,
                        "total_shares_raw": "20,000",
                        "total_amount": 2_000_000.0,
                        "total_amount_raw": "2,000,000",
                        "currency": "HKD",
                    },
                ],
            }
        }
    })
    assert multi_class_card["body"]["elements"][0]["content"] == "**腾讯控股 H股 单日回购 200 万港元**"

    # 3. Multi-trading-day buyback (ff305_hk_buyback_treasury.txt) renders "N 个交易日回购".
    treasury_text = (fixtures / "ff305_hk_buyback_treasury.txt").read_text(encoding="utf-8")
    treasury_parsed = bp.parse(treasury_text)
    multi_day_card = fc.build_card({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "示例科技控股",
                "market": "hk",
                "items": [{
                    "id": "r8-md",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/r8-md.pdf",
                }],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r8-md": bp.scan_record_from_parsed(treasury_parsed, ann_id="r8-md", code="09992", url="https://www.hkexnews.hk/r8-md.pdf", title="翌日披露報表"),
        }
    })
    assert multi_day_card["body"]["elements"][0]["content"] == "**泡泡玛特 2 个交易日回购 1,563 万港元**"

    # Multi-class + multi-day also combines share-class label with N-day wording.
    multi_class_day_card = fc.build_card({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "0700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "r8-mcd",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/r8-mcd.pdf",
                }],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r8-mcd": {
                "id": "r8-mcd",
                "code": "0700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/r8-mcd.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [
                    {
                        "share_class": "A",
                        "trades": [{"date": "2026-11-16", "shares": 10_000, "amount": 1_000_000.0}],
                        "total_shares": 10_000,
                        "total_shares_raw": "10,000",
                        "total_amount": 1_000_000.0,
                        "total_amount_raw": "1,000,000",
                        "currency": "HKD",
                    },
                    {
                        "share_class": "H",
                        "trades": [{"date": "2026-11-15", "shares": 10_000, "amount": 1_000_000.0}, {"date": "2026-11-16", "shares": 10_000, "amount": 1_000_000.0}],
                        "total_shares": 20_000,
                        "total_shares_raw": "20,000",
                        "total_amount": 2_000_000.0,
                        "total_amount_raw": "2,000,000",
                        "currency": "HKD",
                    },
                ],
            }
        }
    })
    assert multi_class_day_card["body"]["elements"][0]["content"] == "**腾讯控股 H股 2 个交易日回购 200 万港元**"

    # 4. Option-exercise NDR (SAMPLE_OPTION) is NOT classified as a buyback when scan parser confirmed is_buyback=False.
    opt_300308 = ndr.SAMPLE_OPTION.replace("09992", "300308")
    option_parsed = ndr.parse_ndr(opt_300308)
    assert option_parsed["buybacks"] == []
    assert "error" in bp.parse(opt_300308)
    _opt_raw, opt_scan_rec = bp.validate_for_scan(
        opt_300308,
        ann_id="r8-opt",
        url="https://static.cninfo.com.cn/r8-opt.pdf",
        code="300308",
        date="2026-09-23",
        title="H股公告-翌日披露报表",
        ndr_data=option_parsed,
    )
    assert opt_scan_rec["validated"] is True and opt_scan_rec["is_buyback"] is False

    option_card, opt_notes_r8 = fc.build_card_with_notes({
        "date": "2026-09-23",
        "lead": "300308 期权行权 1.2 万股",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "summary": "H股根据股份期权计划行使期权 12,000 股",
                "items": [{
                    "id": "r8-opt",
                    "tag": "A股公告",
                    "title": "H股公告-翌日披露报表",
                    "date": "2026-09-23",
                    "url": "https://static.cninfo.com.cn/r8-opt.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"r8-opt": opt_scan_rec}})
    opt_md = "\n".join(el.get("content", "") for el in option_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "300308 期权行权 1.2 万股" not in opt_md
    assert "H股根據股份期權計劃行使期權：12,000股" in opt_md
    assert "回购" not in opt_md
    assert opt_notes_r8 == []

    # 5. Render-time filtering via scan is_amendment writes dropped original to day_errors (via render-card CLI).
    scan_rel = "temp/daily-filing-alert/r8-render-amend-scan.json"
    scan_file = tmp_path / scan_rel
    scan_file.parent.mkdir(parents=True, exist_ok=True)
    scan_file.write_text(json.dumps({
        "ok": True,
        "buyback_results": {
            "r8-cli-orig": bp.scan_record_from_parsed(orig, ann_id="r8-cli-orig", code="09992", url="https://www.hkexnews.hk/r8-cli-orig.pdf", title="翌日披露報表"),
            "r8-cli-corr": bp.scan_record_from_parsed(corr, ann_id="r8-cli-corr", code="09992", url="https://www.hkexnews.hk/r8-cli-corr.pdf", title="翌日披露報表"),
        },
    }, ensure_ascii=False), encoding="utf-8")
    payload_rel = "temp/daily-filing-alert/r8-render-amend.json"
    card_rel = "temp/daily-filing-alert/r8-render-amend-card.json"
    payload_file = tmp_path / payload_rel
    payload_file.write_text(json.dumps({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "耗资四千八百九十七万二千零二十港元",
                "items": [
                    {
                        "id": "r8-cli-orig",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/r8-cli-orig.pdf",
                    },
                    {
                        "id": "r8-cli-corr",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-17",
                        "url": "https://www.hkexnews.hk/r8-cli-corr.pdf",
                    },
                ],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_ren, out_ren = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", payload_rel, "--cn-hk", scan_rel, "--output", card_rel,
    ])
    assert rc_ren == 0 and out_ren["ok"] is True
    assert out_ren["day_errors"] == ["09992: 存在更正回购公告，已过滤: 2026-11-16 翌日披露報表"]
    saved_card = json.loads((tmp_path / card_rel).read_text(encoding="utf-8"))
    assert "day_errors" not in saved_card
    saved_md = "\n".join(el.get("content", "") for el in saved_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "50,400,000 港元" in saved_md
    assert "48,972,020" not in saved_md


def test_r9_failed_buyback_ndr_unverified_summary_lead_and_gap_notes(tmp_path: Path) -> None:
    cli = _cli("daily_filing_buyback_r9")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]

    # 1. Failed-parse NDR with buyback keyword in title & enrich_error:
    #    - classified as unverified buyback (not ordinary announcement)
    #    - company summary & item text regenerated without digits
    #    - lead renders "<code > 回购股份" with no amount (ignores model "0.0 万港元")
    #    - gap note lists the announcement in day_errors (both single-item and multi-item)
    failed_item = {
        "tag": "翌日披露",
        "title": "翌日披露報表（股份購回）",
        "text": "回购 0.0 万港元",
        "date": "2026-11-17",
        "url": "https://www.hkexnews.hk/fail.pdf",
        "enrich_error": "翌日披露报表解析失败: 缺少合共",
    }
    assert fc.is_buyback_item(failed_item) is True

    single_card, single_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "lead": "09992 回购 0.0 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "昨日回购 340,000 股，金额 48,972,020 港元",
                "items": [failed_item],
            }],
        }],
    })
    assert single_card["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
    single_md = "\n".join(
        el.get("content", "") for el in single_card["body"]["elements"] if el.get("tag") == "markdown"
    )
    assert "0.0" not in single_md
    assert "48,972,020" not in single_md
    assert "回购相关公告（金额未能从原文核对）" in single_md
    assert len(single_notes) == 1
    assert "09992" in single_notes[0]
    assert "2026-11-17 回购相关公告" in single_notes[0]
    assert "金额未核对" in single_notes[0]

    # Multi-item company + render-card CLI path + buyback.error variant (A-share title with 回购):
    payload_rel = "temp/daily-filing-alert/r9-failed-buyback.json"
    card_rel = "temp/daily-filing-alert/r9-failed-buyback-card.json"
    payload_file = tmp_path / payload_rel
    payload_file.parent.mkdir(parents=True, exist_ok=True)
    payload_file.write_text(json.dumps({
        "date": "2026-11-17",
        "lead": "300308 回购 0.0 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "summary": "回购 12,345,678 元人民币；披露季度业绩",
                "items": [
                    {
                        "tag": "A股公告",
                        "title": "H股公告-关于回购股份的翌日披露报表",
                        "text": "回购 0.0 万港元",
                        "date": "2026-11-17",
                        "enrich_error": "取正文失败: timeout",
                    },
                    {
                        "tag": "A股公告",
                        "title": "季度业绩公告",
                        "text": "披露季度业绩",
                        "date": "2026-11-17",
                    },
                ],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_r9, out_r9 = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", payload_rel, "--output", card_rel,
    ])
    assert rc_r9 == 0 and out_r9["ok"] is True
    assert len(out_r9["day_errors"]) == 1
    assert "300308" in out_r9["day_errors"][0]
    assert "2026-11-17 回购相关公告" in out_r9["day_errors"][0]
    assert "取正文失败: timeout" in out_r9["day_errors"][0]
    saved_r9 = json.loads((tmp_path / card_rel).read_text(encoding="utf-8"))
    assert saved_r9["body"]["elements"][0]["content"] == "**中际旭创 回购股份**"
    r9_md = "\n".join(el.get("content", "") for el in saved_r9["body"]["elements"] if el.get("tag") == "markdown")
    assert "发布回购公告，金额未能从原文核对；披露季度业绩" in r9_md
    assert "0.0" not in r9_md
    assert "12,345,678" not in r9_md

    # Also catches buyback dict parse error when title contains buyback keyword.
    assert fc.is_buyback_item({
        "tag": "港股公告",
        "title": "翌日披露報表（股份購回）",
        "text": "回购 0.0 万港元",
        "buyback": {"error": "数字格式不完整"},
    }) is True

    # 2. Confirmed no-buyback NDR (e.g., option exercise in scan results)
    #    stays ordinary announcement, keeps model lead, replaces text with parser digest, and adds no gap note.
    opt_300308 = ndr.SAMPLE_OPTION.replace("09992", "300308")
    option_parsed = ndr.parse_ndr(opt_300308)
    _opt_raw, opt_scan_rec = bp.validate_for_scan(
        opt_300308,
        ann_id="r9-opt",
        url="https://static.cninfo.com.cn/r9-opt.pdf",
        code="300308",
        date="2026-09-23",
        title="H股公告-翌日披露报表",
        ndr_data=option_parsed,
    )
    opt_card, opt_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "lead": "300308 期权行权 1.2 万股",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "summary": "H股根据股份期权计划行使期权 12,000 股",
                "items": [{
                    "id": "r9-opt",
                    "tag": "A股公告",
                    "title": "H股公告-翌日披露报表",
                    "date": "2026-09-23",
                    "url": "https://static.cninfo.com.cn/r9-opt.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"r9-opt": opt_scan_rec}})
    opt_md = "\n".join(el.get("content", "") for el in opt_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "300308 期权行权 1.2 万股" not in opt_md
    assert "H股根據股份期權計劃行使期權：12,000股" in opt_md
    assert opt_notes == []


def test_r10_unified_ndr_buyback_guard_and_codex_repros(tmp_path: Path) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r10")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]

    # Codex repro 1:
    # Title is only "翌日披露報表", tag is ordinary ("港股公告"), and parsing failed ->
    # must be protected: fixed item text "<标题>（金额未能从原文核对）", no amounts in summary/lead,
    # and listed in gap notes.
    codex1_card, codex1_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "lead": "09992 回购 0.0 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "昨日回购 340,000 股，金额 48,972,020 港元",
                "items": [{
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "text": "回购 0.0 万港元",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/codex1.pdf",
                    "enrich_error": "取正文失败: timeout",
                }],
            }],
        }],
    })
    assert codex1_card["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
    codex1_md = "\n".join(el.get("content", "") for el in codex1_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "回购相关公告（金额未能从原文核对）" in codex1_md
    assert "0.0" not in codex1_md
    assert "48,972,020" not in codex1_md
    assert "340,000" not in codex1_md
    assert len(codex1_notes) == 1
    assert "09992" in codex1_notes[0] and "2026-11-17 回购相关公告" in codex1_notes[0]

    # Codex repro 2:
    # Identified as buyback (via tag/title/text) WITHOUT verified structured total AND WITHOUT error field ->
    # must NOT keep text amounts verbatim; text -> "回购相关公告（金额未能从原文核对）", no amounts in summary/lead,
    # and listed in gap notes (both single-item and multi-item via render-card CLI).
    for item_variant, expected_text in (
        (
            {
                "tag": "回购",
                "title": "关于回购股份的公告",
                "text": "11-17 回购 500,000 股，金额 5,000 万港元",
                "date": "2026-11-17",
            },
            "回购相关公告（金额未能从原文核对）",
        ),
        (
            {
                "tag": "港股公告",
                "title": "翌日披露報表",
                "text": "11-17 购回 340,000 股，金额 48,972,020 港元",
                "date": "2026-11-17",
            },
            "回购相关公告（金额未能从原文核对）",
        ),
        (
            {
                "tag": "港股公告",
                "title": "翌日披露報表",
                "text": "11-17 股份变动，涉及金额 1,234 万港元",
                "date": "2026-11-17",
            },
            "回购相关公告（金额未能从原文核对）",
        ),
    ):
        card_v, notes_v = fc.build_card_with_notes({
            "date": "2026-11-17",
            "lead": "00700 回购 5,000 万港元",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "00700",
                    "name": "腾讯控股",
                    "market": "hk",
                    "summary": "11-17 回购 500,000 股，金额 5,000 万港元",
                    "items": [
                        item_variant,
                        {
                            "tag": "港股公告",
                            "title": "股东大会通告",
                            "text": "发布股东大会通告",
                            "date": "2026-11-17",
                        },
                    ],
                }],
            }],
        })
        assert card_v["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
        md_v = "\n".join(el.get("content", "") for el in card_v["body"]["elements"] if el.get("tag") == "markdown")
        assert expected_text in md_v
        assert "发布回购公告，金额未能从原文核对；发布股东大会通告" in md_v
        for bad_num in ("500,000", "5,000", "340,000", "48,972,020", "1,234"):
            assert bad_num not in md_v
        assert len(notes_v) == 1 and "00700" in notes_v[0]

    # render-card CLI path on Codex 1 + Codex 2 without error field:
    payload_rel = "temp/daily-filing-alert/r10-codex.json"
    card_rel = "temp/daily-filing-alert/r10-codex-card.json"
    payload_file = tmp_path / payload_rel
    payload_file.parent.mkdir(parents=True, exist_ok=True)
    payload_file.write_text(json.dumps({
        "date": "2026-11-17",
        "lead": "09992 回购 4,897 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "回购 340,000 股，金额 48,972,020 港元",
                "items": [{
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "text": "11-17 回购 340,000 股，金额 48,972,020 港元",
                    "date": "2026-11-17",
                }],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_r10, out_r10 = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", payload_rel, "--output", card_rel,
    ])
    assert rc_r10 == 0 and out_r10["ok"] is True
    assert len(out_r10["day_errors"]) == 1
    assert "09992" in out_r10["day_errors"][0] and "2026-11-17 回购相关公告" in out_r10["day_errors"][0]
    saved_r10 = json.loads((tmp_path / card_rel).read_text(encoding="utf-8"))
    r10_md = "\n".join(el.get("content", "") for el in saved_r10["body"]["elements"] if el.get("tag") == "markdown")
    assert saved_r10["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
    assert "回购相关公告（金额未能从原文核对）" in r10_md
    assert "48,972,020" not in r10_md and "340,000" not in r10_md and "4,897" not in r10_md

    # Validated scan buyback overrides tampered model text/summary/lead completely:
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    parsed_pm = bp.parse(popmart_text)
    tampered_card, tampered_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "lead": "09992 回购 9.99 亿港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "11-16 回购 999,999 股，金额 999,999,999 港元",
                "items": [{
                    "id": "r10-pm",
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "text": "11-16 回购 999,999 股，金额 999,999,999 港元",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/r10-pm.pdf",
                }],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r10-pm": bp.scan_record_from_parsed(parsed_pm, ann_id="r10-pm", code="09992", url="https://www.hkexnews.hk/r10-pm.pdf", title="翌日披露報表"),
        }
    })
    assert tampered_notes == []
    assert tampered_card["body"]["elements"][0]["content"] == "**泡泡玛特 单日回购 4,897 万港元**"
    tampered_md = "\n".join(el.get("content", "") for el in tampered_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "999,999" not in tampered_md and "9.99" not in tampered_md
    assert "340,000 股" in tampered_md and "48,972,020 港元" in tampered_md

    # Confirmed no-buyback NDR (option exercise) acts as ordinary announcement,
    # and ignores model text/summary/lead trying to sneak in "回购" + amount:
    opt_300308 = ndr.SAMPLE_OPTION.replace("09992", "300308")
    option_parsed = ndr.parse_ndr(opt_300308)
    _opt10_raw, opt10_scan = bp.validate_for_scan(
        opt_300308,
        ann_id="r10-opt",
        url="https://static.cninfo.com.cn/r10-opt.pdf",
        code="300308",
        date="2026-09-23",
        title="H股公告-翌日披露报表",
        ndr_data=option_parsed,
    )
    dirty_opt_card, dirty_opt_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "lead": "300308 回购 5,000 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "summary": "回购 5,000 万港元",
                "items": [{
                    "id": "r10-opt",
                    "tag": "A股公告",
                    "title": "H股公告-翌日披露报表",
                    "text": "H股行使期权 12,000 股；回购 5,000 万港元",
                    "date": "2026-09-23",
                    "url": "https://static.cninfo.com.cn/r10-opt.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"r10-opt": opt10_scan}})
    dirty_opt_md = "\n".join(el.get("content", "") for el in dirty_opt_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "5,000" not in dirty_opt_md
    assert "回购" not in dirty_opt_md
    assert "行使期權" in dirty_opt_md
    assert dirty_opt_notes == []


def test_r11_scan_only_buyback_guard_codex_repros_and_popmart_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r11")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Codex Repro 1: Tampered `line` (148,972,020 vs validated 48,972,020)
    # Must ALWAYS format from structured fields and completely ignore model `line`/`text`/`summary`/`lead`.
    # ------------------------------------------------------------------
    pm_parsed = bp.parse(popmart_text)
    pm_scan_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-repro1",
        url="https://www.hkexnews.hk/pm-repro1.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    pm_scan_rec["line"] = pm_scan_rec["line"].replace("48,972,020", "148,972,020")
    repro1_card, repro1_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "lead": "09992 单日回购 1.49 亿港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "11-16 回购 340,000 股，金额 148,972,020 港元",
                "items": [{
                    "id": "pm-repro1",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "text": "11-16 回购 340,000 股，金额 148,972,020 港元",
                    "line": "11-16 回购 340,000 股，金额 148,972,020 港元",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/pm-repro1.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-repro1": pm_scan_rec}})
    assert repro1_notes == []
    assert repro1_card["body"]["elements"][0]["content"] == "**泡泡玛特 单日回购 4,897 万港元**"
    repro1_md = "\n".join(el.get("content", "") for el in repro1_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "148,972,020" not in repro1_md and "1.49" not in repro1_md
    assert "48,972,020 港元" in repro1_md and "340,000 股" in repro1_md

    # ------------------------------------------------------------------
    # Codex Repro 2: Plain numeric fields in payload (total_amount=50000000, currency="HKD")
    # WITHOUT validated scan result must NEVER be accepted as validated.
    # ------------------------------------------------------------------
    repro2_card, repro2_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "lead": "00700 单日回购 5,000 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "summary": "11-17 回购 500,000 股，金额 50,000,000 港元",
                "items": [{
                    "id": "missing-in-scan",
                    "tag": "回购",
                    "title": "翌日披露報表（股份購回）",
                    "text": "11-17 回购 500,000 股，金额 50,000,000 港元",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/missing.pdf",
                    "total_amount": 50_000_000.0,
                    "total_amount_raw": "50,000,000",
                    "total_shares": 500_000,
                    "total_shares_raw": "500,000",
                    "currency": "HKD",
                    "trades": [{"date": "2026-11-17", "shares": 500_000}],
                }],
            }],
        }],
    }, scan_data={"buyback_results": {}})
    assert repro2_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
    repro2_md = "\n".join(el.get("content", "") for el in repro2_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "回购相关公告（金额未能从原文核对）" in repro2_md
    assert "50,000,000" not in repro2_md and "500,000" not in repro2_md and "5,000" not in repro2_md
    assert len(repro2_notes) == 1 and "00700" in repro2_notes[0]

    # ------------------------------------------------------------------
    # Codex Repro 3: "不适用" phrasing bypass attempt on real ndr.buybacks=[] AND without scan validation
    # ------------------------------------------------------------------
    opt_300308 = ndr.SAMPLE_OPTION.replace("09992", "300308")
    option_parsed = ndr.parse_ndr(opt_300308)
    _opt11_raw, opt11_scan = bp.validate_for_scan(
        opt_300308,
        ann_id="r11-opt",
        url="https://static.cninfo.com.cn/r11-opt.pdf",
        code="300308",
        date="2026-09-23",
        title="H股公告-翌日披露报表",
        ndr_data=option_parsed,
    )
    repro3_with_scan, repro3_scan_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "lead": "300308 回购 500,000 股，金额 5,000 万港元，注销安排不适用",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "summary": "回购 500,000 股，金额 5,000 万港元，注销安排不适用",
                "items": [{
                    "id": "r11-opt",
                    "tag": "A股公告",
                    "title": "H股公告-翌日披露报表",
                    "text": "回购 500,000 股，金额 5,000 万港元，注销安排不适用",
                    "date": "2026-09-23",
                    "url": "https://static.cninfo.com.cn/r11-opt.pdf",
                    "ndr": option_parsed,
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"r11-opt": opt11_scan}})
    repro3_scan_md = "\n".join(el.get("content", "") for el in repro3_with_scan["body"]["elements"] if el.get("tag") == "markdown")
    assert "500,000" not in repro3_scan_md and "5,000" not in repro3_scan_md and "回购" not in repro3_scan_md
    assert "行使期權" in repro3_scan_md
    assert repro3_scan_notes == []

    repro3_no_scan, repro3_no_scan_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "lead": "300308 回购 500,000 股，金额 5,000 万港元，注销安排不适用",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "summary": "回购 500,000 股，金额 5,000 万港元，注销安排不适用",
                "items": [{
                    "tag": "A股公告",
                    "title": "H股公告-翌日披露报表",
                    "text": "回购 500,000 股，金额 5,000 万港元，注销安排不适用",
                    "date": "2026-09-23",
                    "ndr": {"buybacks": []},
                }],
            }],
        }],
    })
    repro3_no_scan_md = "\n".join(el.get("content", "") for el in repro3_no_scan["body"]["elements"] if el.get("tag") == "markdown")
    assert "500,000" not in repro3_no_scan_md and "5,000" not in repro3_no_scan_md
    assert "回购相关公告（金额未能从原文核对）" in repro3_no_scan_md
    assert len(repro3_no_scan_notes) == 1

    # ------------------------------------------------------------------
    # Codex Repro 4: Multi-group item where 1 group has amount and 1 lacks total
    # Body, summary, AND lead must ALL omit amounts and list gap note.
    # ------------------------------------------------------------------
    repro4_card, repro4_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "lead": "00700 单日回购 5,000 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "summary": "A股回购 5,000 万港元",
                "items": [{
                    "id": "r11-partial-groups",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/partial.pdf",
                    "groups": [
                        {
                            "share_class": "A",
                            "trades": [{"date": "2026-11-17", "shares": 500_000}],
                            "total_shares": 500_000,
                            "total_amount": 50_000_000.0,
                            "currency": "HKD",
                        },
                        {
                            "share_class": "H",
                            "trades": [{"date": "2026-11-17", "shares": 100_000}],
                            "total_shares": 100_000,
                        },
                    ],
                }],
            }],
        }],
    }, scan_data={
        "buyback_results": {
            "r11-partial-groups": {
                "id": "r11-partial-groups",
                "code": "00700",
                "title": "翌日披露報表",
                "url": "https://www.hkexnews.hk/partial.pdf",
                "validated": True,
                "is_buyback": True,
                "groups": [
                    {
                        "share_class": "A",
                        "trades": [{"date": "2026-11-17", "shares": 500_000, "amount": 50_000_000.0}],
                        "total_shares": 500_000,
                        "total_shares_raw": "500,000",
                        "total_amount": 50_000_000.0,
                        "total_amount_raw": "50,000,000",
                        "currency": "HKD",
                    },
                    {
                        "share_class": "H",
                        "trades": [{"date": "2026-11-17", "shares": 100_000}],
                        "total_shares": 100_000,
                    },
                ],
            }
        }
    })
    assert repro4_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
    repro4_md = "\n".join(el.get("content", "") for el in repro4_card["body"]["elements"] if el.get("tag") == "markdown")
    assert "翌日披露報表（金额未能从原文核对）" in repro4_md
    assert "5,000" not in repro4_md and "50,000,000" not in repro4_md
    assert len(repro4_notes) == 1

    # ------------------------------------------------------------------
    # End-to-End Offline: A-share notice_text via scan-cn-hk -> render-card
    # (HK rows ignore notice_text in scan-cn-hk and are tested via PDF CLI in #2 below)
    # ------------------------------------------------------------------
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)

    raw_e2e_rel = "temp/daily-filing-alert/2026-11-16-cn-hk-raw.json"
    scan_e2e_rel = "temp/daily-filing-alert/2026-11-16-cn-hk.json"
    payload_e2e_rel = "temp/daily-filing-alert/2026-11-16-payload.json"
    card_e2e_rel = "temp/daily-filing-alert/2026-11-16-card.json"

    raw_e2e_path = tmp_path / raw_e2e_rel
    raw_e2e_path.parent.mkdir(parents=True, exist_ok=True)
    raw_e2e_path.write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [
            {
                "ticker": "300750",
                "ok": True,
                "rows": [{
                    "id": "cn-300750-20261116",
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表",
                    "category": "翌日披露",
                    "pdf_link": "https://static.cninfo.com.cn/300750-treasury.pdf",
                    "notice_text": ndr.SAMPLE_TREASURY,
                }],
            },
            {
                "ticker": "300308",
                "ok": True,
                "rows": [{
                    "id": "cn-300308-20261116",
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表",
                    "pdf_link": "https://static.cninfo.com.cn/300308-opt.pdf",
                    "notice_text": ndr.SAMPLE_OPTION.replace("09992", "300308"),
                }],
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")

    rc_scan, out_scan = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", raw_e2e_rel, "--output", scan_e2e_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_scan == 0 and out_scan["ok"] is True
    scan_doc = json.loads((tmp_path / scan_e2e_rel).read_text(encoding="utf-8"))
    # H1: SAMPLE_TREASURY (300308) attached to 300750 MUST be rejected on stock_code mismatch and logged as gap.
    cn_rec = scan_doc["buyback_results"]["cn-300750-20261116"]
    assert cn_rec["validated"] is False
    assert cn_rec["stock_code"] == "300308"
    assert "300308" in cn_rec["error"] and "300750" in cn_rec["error"]
    assert "total_amount" not in cn_rec and "groups" not in cn_rec
    assert any("300750" in err and "300308" in err for err in out_scan["day_errors"])
    res_300750 = next(r for r in scan_doc["results"] if r["ticker"] == "300750")
    assert "身份冲突" in res_300750["announcements"][0]["enrich_error"]
    assert "ndr" not in res_300750["announcements"][0]
    opt_rec = scan_doc["buyback_results"]["cn-300308-20261116"]
    assert opt_rec["validated"] is True and opt_rec["is_buyback"] is False

    # Model writes minimal payload with NO buyback numbers at all (only id/url/title/date/tag)
    (tmp_path / payload_e2e_rel).write_text(json.dumps({
        "date": "2026-11-16",
        "weekday": "周一",
        "window_note": "覆盖上周五至今",
        "stats": {"hk": {"scanned": 4}, "cn": {"scanned": 3}},
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [
                {
                    "code": "300750",
                    "name": "宁德时代",
                    "market": "cn",
                    "items": [{
                        "id": "cn-300750-20261116",
                        "tag": "回购",
                        "title": "H股公告-翌日披露报表",
                        "date": "2026-11-16",
                        "url": "https://static.cninfo.com.cn/300750-treasury.pdf",
                    }],
                },
                {
                    "code": "300308",
                    "name": "中际旭创",
                    "market": "cn",
                    "items": [{
                        "id": "cn-300308-20261116",
                        "tag": "A股公告",
                        "title": "H股公告-翌日披露报表",
                        "date": "2026-11-16",
                        "url": "https://static.cninfo.com.cn/300308-opt.pdf",
                    }],
                },
            ],
        }],
        "footer": "数据源：SEC EDGAR · 巨潮公告 · HKEXnews",
    }, ensure_ascii=False), encoding="utf-8")

    rc_ren, out_ren = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", payload_e2e_rel, "--cn-hk", scan_e2e_rel, "--output", card_e2e_rel,
    ])
    assert rc_ren == 0 and out_ren["ok"] is True
    assert len(out_ren["day_errors"]) == 1 and "300750" in out_ren["day_errors"][0]
    assert "300308" in out_ren["day_errors"][0]
    card_e2e = json.loads((tmp_path / card_e2e_rel).read_text(encoding="utf-8"))
    assert card_e2e["body"]["elements"][0]["content"] == "**宁德时代 回购股份**"
    e2e_md = "\n".join(el.get("content", "") for el in card_e2e["body"]["elements"] if el.get("tag") == "markdown")
    assert "回购相关公告（金额未能从原文核对）" in e2e_md
    for leaked in ("856,980", "794,631,549.33", "927.25", "7.95", "300750-treasury.pdf"):
        assert leaked not in e2e_md
    assert "H股根據股份期權計劃行使期權：12,000股" in e2e_md

    # 2) Also verify HK PDF CLI workflow: scan-cn-hk (without notice_text) ->
    #    buyback_parse.py --workspace-dir ... --scan-output ... <url> (mocked PDF) -> render-card
    hk_url = "https://www.hkexnews.hk/listedco/listconews/sehk/2026/1116/popmart-pdf.pdf"
    raw_pdf_rel = "temp/daily-filing-alert/2026-11-16-pdf-raw.json"
    scan_pdf_rel = "temp/daily-filing-alert/2026-11-16-pdf-cn-hk.json"
    payload_pdf_rel = "temp/daily-filing-alert/2026-11-16-pdf-payload.json"
    card_pdf_rel = "temp/daily-filing-alert/2026-11-16-pdf-card.json"

    (tmp_path / raw_pdf_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [{
            "ticker": "09992",
            "ok": True,
            "rows": [{
                "date": "2026-11-16",
                "title": "翌日披露報表",
                "pdf_link": hk_url,
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")

    rc_s2, out_s2 = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", raw_pdf_rel, "--output", scan_pdf_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_s2 == 0 and out_s2["ok"] is True
    pre_scan = json.loads((tmp_path / scan_pdf_rel).read_text(encoding="utf-8"))
    assert pre_scan["buyback_results"][hk_url]["validated"] is False

    _patch_fetch(monkeypatch, lambda url, timeout=20, headers=None, min_interval=0.0: b"%PDF-1.4")
    monkeypatch.setattr(bp, "extract_pdf_text", create_autospec(bp.extract_pdf_text, return_value=popmart_text))
    bp_buf = StringIO()
    with redirect_stdout(bp_buf):
        rc_bp = bp.main(["--workspace-dir", str(tmp_path), "--scan-output", scan_pdf_rel, hk_url])
    assert rc_bp == 0
    post_scan = json.loads((tmp_path / scan_pdf_rel).read_text(encoding="utf-8"))
    assert post_scan["buyback_results"][hk_url]["validated"] is True
    assert post_scan["buyback_results"][hk_url]["total_amount_raw"] == "48,972,020"

    (tmp_path / payload_pdf_rel).write_text(json.dumps({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": hk_url,
                }],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_r2, out_r2 = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", payload_pdf_rel, "--cn-hk", scan_pdf_rel, "--output", card_pdf_rel,
    ])
    assert rc_r2 == 0 and out_r2["ok"] is True and out_r2["day_errors"] == []
    card_pdf = json.loads((tmp_path / card_pdf_rel).read_text(encoding="utf-8"))
    assert card_pdf["body"]["elements"][0]["content"] == "**泡泡玛特 单日回购 4,897 万港元**"
    pdf_md = "\n".join(el.get("content", "") for el in card_pdf["body"]["elements"] if el.get("tag") == "markdown")
    assert "340,000 股" in pdf_md and "48,972,020 港元" in pdf_md


def test_r12_codex_comprehensive_fixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r12")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    pm_parsed = bp.parse(popmart_text)

    def _all_md(card: dict) -> str:
        return "\n".join(el.get("content", "") for el in card["body"]["elements"] if el.get("tag") == "markdown")

    # 1. Render-time scan self-consistency checks:
    #    (a) total_amount_raw vs total_amount mismatch -> unverified + gap note
    #    (b) both total_amount_raw and total_amount tampered while trades sum differs -> unverified + gap note
    #    (c) corrupt non-dict element in groups -> unverified + gap note (never silently dropped)
    base_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-r12",
        url="https://www.hkexnews.hk/pm-r12.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    pm_payload = {
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "id": "pm-r12",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/pm-r12.pdf",
                }],
            }],
        }],
    }

    rec_1a = json.loads(json.dumps(base_rec))
    rec_1a["groups"][0]["total_amount_raw"] = "148,972,020"
    rec_1a["total_amount_raw"] = "148,972,020"
    card_1a, notes_1a = fc.build_card_with_notes(pm_payload, scan_data={"buyback_results": {"pm-r12": rec_1a}})
    md_1a = _all_md(card_1a)
    assert card_1a["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
    assert "翌日披露報表（金额未能从原文核对）" in md_1a
    assert "148,972,020" not in md_1a and "48,972,020" not in md_1a and "4,897" not in md_1a
    assert len(notes_1a) == 1 and "09992" in notes_1a[0]

    rec_1b = json.loads(json.dumps(base_rec))
    rec_1b["groups"][0]["total_amount_raw"] = "148,972,020"
    rec_1b["groups"][0]["total_amount"] = 148_972_020.0
    rec_1b["total_amount_raw"] = "148,972,020"
    rec_1b["total_amount"] = 148_972_020.0
    card_1b, notes_1b = fc.build_card_with_notes(pm_payload, scan_data={"buyback_results": {"pm-r12": rec_1b}})
    md_1b = _all_md(card_1b)
    assert card_1b["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
    assert "翌日披露報表（金额未能从原文核对）" in md_1b
    assert "148,972,020" not in md_1b and "1.49" not in md_1b and "48,972,020" not in md_1b
    assert len(notes_1b) == 1 and "09992" in notes_1b[0]

    rec_1c = json.loads(json.dumps(base_rec))
    rec_1c["groups"] = ["corrupt-element", rec_1c["groups"][0]]
    card_1c, notes_1c = fc.build_card_with_notes(pm_payload, scan_data={"buyback_results": {"pm-r12": rec_1c}})
    md_1c = _all_md(card_1c)
    assert card_1c["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
    assert "翌日披露報表（金额未能从原文核对）" in md_1c
    assert "48,972,020" not in md_1c
    assert len(notes_1c) == 1 and "09992" in notes_1c[0]

    # 2. Match consistency (company code + URL must match) and official URL/title from scan record:
    #    (a) Tencent item steals Popmart's valid id while keeping Tencent URL -> conflict -> unverified
    conflict_card, conflict_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "pm-r12",
                    "tag": "回购",
                    "title": "腾讯回购 48,972,020 港元",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/tencent-fake.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-r12": base_rec}})
    conflict_md = _all_md(conflict_card)
    assert conflict_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
    assert "回购相关公告（金额未能从原文核对）" in conflict_md
    assert "48,972,020" not in conflict_md
    assert "pm-r12.pdf" not in conflict_md and "tencent-fake.pdf" not in conflict_md
    assert len(conflict_notes) == 1 and "00700" in conflict_notes[0]

    #    (b) Consistent id without item url takes official url and title from scan record (ignores model title)
    official_card, official_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "id": "pm-r12",
                    "tag": "回购",
                    "title": "模型篡改标题 999,999 港元",
                    "date": "2026-11-16",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-r12": base_rec}})
    official_md = _all_md(official_card)
    assert official_notes == []
    assert "https://www.hkexnews.hk/pm-r12.pdf" in official_md
    assert "999,999" not in official_md
    assert "48,972,020 港元" in official_md

    # 3. Input sanitization: forged underscore-prefixed internal fields in payload are stripped at render entry.
    forged_card, forged_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "lead": "09992 单日回购 1.49 亿港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "tag": "港股公告",
                    "title": "普通公告",
                    "text": "普通文本",
                    "date": "2026-11-16",
                    "_buyback_state": "validated",
                    "_buyback_data": {
                        "groups": [{
                            "trades": [{"date": "2026-11-16", "shares": 340_000, "amount": 148_972_020.0}],
                            "total_shares": 340_000,
                            "total_shares_raw": "340,000",
                            "total_amount": 148_972_020.0,
                            "total_amount_raw": "148,972,020",
                            "currency": "HKD",
                        }]
                    },
                    "_is_amendment": True,
                }],
            }],
        }],
    })
    forged_md = _all_md(forged_card)
    assert "148,972,020" not in forged_md and "1.49" not in forged_md
    assert forged_notes == []

    # 4. Unverified item title & tag whitelist: model numbers in title and tag must never appear in card or day_errors.
    unverified_inject_card, unverified_inject_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "tag": "回购 148,972,020 港元",
                    "title": "回购 148,972,020 港元",
                    "text": "今日回购 148,972,020 港元",
                    "date": "2026-11-17",
                }],
            }],
        }],
    })
    unverified_md = _all_md(unverified_inject_card)
    assert "148,972,020" not in unverified_md
    assert "回购相关公告（金额未能从原文核对）" in unverified_md
    assert "<font color='green'>回购</font>" in unverified_md
    assert len(unverified_inject_notes) == 1 and "148,972,020" not in unverified_inject_notes[0]

    # When a failed scan record exists, use scan record's title (not model's) and whitelisted tag.
    failed_scan_rec = {
        "id": "fail-r12",
        "code": "00700",
        "title": "翌日披露報表",
        "date": "2026-11-17",
        "url": "https://www.hkexnews.hk/fail-r12.pdf",
        "validated": False,
        "is_buyback": True,
        "error": "缺少合共",
    }
    failed_scan_card, failed_scan_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "fail-r12",
                    "tag": "港股公告 99,999,999",
                    "title": "回购 99,999,999 港元",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/fail-r12.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"fail-r12": failed_scan_rec}})
    failed_scan_md = _all_md(failed_scan_card)
    assert "99,999,999" not in failed_scan_md
    assert "翌日披露報表（金额未能从原文核对）" in failed_scan_md
    assert "<font color='green'>回购</font>" in failed_scan_md
    assert len(failed_scan_notes) == 1 and "99,999,999" not in failed_scan_notes[0]

    # 5. Negation bypass blocked without scan record; scan announcement type governs when present.
    neg_bypass_card, neg_bypass_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "tag": "港股公告",
                    "title": "公告",
                    "text": "此前未回购，今日回购 148,972,020 港元",
                    "date": "2026-11-17",
                }],
            }],
        }],
    })
    neg_md = _all_md(neg_bypass_card)
    assert "148,972,020" not in neg_md
    assert "回购相关公告（金额未能从原文核对）" in neg_md
    assert len(neg_bypass_notes) == 1

    # 6. Unicode Cf format characters inside numbers (\u2060, \u200b, \u200c, \u200d, \ufeff) must fail parsing.
    for cf_ch in ("\u2060", "\u200b", "\u200c", "\u200d", "\ufeff", "\u00ad"):
        cf_popmart = popmart_text.replace("48,972,020", f"48{cf_ch},972,020")
        cf_res = bp.parse(cf_popmart)
        assert "error" in cf_res and "total_amount" not in cf_res, repr(cf_ch)
        cf_ndr = ndr.SAMPLE_TREASURY.replace("794,631,549.33", f"794{cf_ch},631,549.33")
        with pytest.raises(ValueError):
            ndr.parse_ndr(cf_ndr)

    # 7. Non-buyback type (增持) carrying total_amount is NOT treated as buyback.
    stake_card, stake_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "stake-1",
                    "tag": "增持",
                    "title": "关于控股股东增持股份的公告",
                    "text": "控股股东增持 500 万港元",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/stake-1.pdf",
                    "total_amount": 5_000_000.0,
                    "currency": "HKD",
                }],
            }],
        }],
    }, scan_data={
        "results": [{
            "ticker": "0700",
            "announcements": [{
                "id": "stake-1",
                "code": "00700",
                "title": "关于控股股东增持股份的公告",
                "type": "增持",
                "date": "2026-11-17",
                "url": "https://www.hkexnews.hk/stake-1.pdf",
            }],
        }],
        "buyback_results": {},
    })
    stake_md = _all_md(stake_card)
    assert stake_notes == []
    assert stake_card["body"]["elements"][0]["content"] == "**腾讯控股 增持 500 万港元**"
    assert "控股股东增持 500 万港元" in stake_md
    assert "未能从原文核对" not in stake_md

    # 8. Amendment detection from body "狀態：更正" even when confirmed no-buyback (is_buyback=False),
    #    and removing original buyback in both render-card and scan-cn-hk.
    amend_opt_text = ndr.SAMPLE_OPTION.replace(
        "呈交日期： 2026年9月23日",
        "呈交日期： 2026年11月16日\n狀態： 更正",
    )
    _raw_opt, amend_opt_rec = bp.validate_for_scan(
        amend_opt_text,
        ann_id="pm-corr-nobuy",
        url="https://www.hkexnews.hk/pm-corr-nobuy.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    assert amend_opt_rec["validated"] is True
    assert amend_opt_rec["is_buyback"] is False
    assert amend_opt_rec["is_amendment"] is True

    amend_card, amend_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "11-16 回购 340,000 股，金额 48,972,020 港元",
                "items": [
                    {
                        "id": "pm-r12",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/pm-r12.pdf",
                    },
                    {
                        "id": "pm-corr-nobuy",
                        "tag": "港股公告",
                        "title": "翌日披露報表",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/pm-corr-nobuy.pdf",
                    },
                ],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-r12": base_rec, "pm-corr-nobuy": amend_opt_rec}})
    amend_md = _all_md(amend_card)
    assert "48,972,020" not in amend_md and "4,897" not in amend_md
    assert "行使期權" in amend_md
    assert len(amend_notes) == 1 and "存在更正回购公告，已过滤" in amend_notes[0]

    # Also verify A-share scan-cn-hk detects body "狀態：更正" on no-buyback NDR and filters original buyback.
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    raw_am_rel = "temp/daily-filing-alert/r12-amend-raw.json"
    out_am_rel = "temp/daily-filing-alert/r12-amend-scan.json"
    (tmp_path / raw_am_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / raw_am_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [{
            "ticker": "300308",
            "ok": True,
            "rows": [
                {
                    "id": "cn-orig-scan",
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表",
                    "pdf_link": "https://static.cninfo.com.cn/cn-orig-scan.pdf",
                    "notice_text": ndr.SAMPLE_TREASURY,
                },
                {
                    "id": "cn-corr-scan",
                    "date": "2026-11-16",
                    "title": "H股公告-翌日披露报表",
                    "pdf_link": "https://static.cninfo.com.cn/cn-corr-scan.pdf",
                    "notice_text": amend_opt_text.replace("09992", "300308"),
                },
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_am12, out_am12 = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", raw_am_rel, "--output", out_am_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_am12 == 0 and out_am12["ok"] is True
    am12_doc = json.loads((tmp_path / out_am_rel).read_text(encoding="utf-8"))
    cn12_res = next(r for r in am12_doc["results"] if r["ticker"] == "300308")
    assert [a["id"] for a in cn12_res["announcements"]] == ["cn-corr-scan"]
    assert am12_doc["buyback_results"]["cn-corr-scan"]["is_amendment"] is True
    assert am12_doc["buyback_results"]["cn-corr-scan"]["is_buyback"] is False
    assert any("存在更正回购公告，已过滤" in err for err in am12_doc["day_errors"])


def test_r13_glm_medium_fixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r13")
    bp = cli._locals()["buyback_parse"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    pm_parsed = bp.parse(popmart_text)

    def _all_md(card: dict) -> str:
        return "\n".join(el.get("content", "") for el in card["body"]["elements"] if el.get("tag") == "markdown")

    # H1 confirmation (wrong URL pointing to another company's valid scan record without item id):
    pm_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-r13",
        url="https://www.hkexnews.hk/pm-r13.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    h1_card, h1_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "tag": "港股公告",
                    "title": "公告",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/pm-r13.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-r13": pm_rec}})
    h1_md = _all_md(h1_card)
    assert h1_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
    assert "48,972,020" not in h1_md and "pm-r13.pdf" not in h1_md
    assert "回购相关公告（金额未能从原文核对）" in h1_md
    assert len(h1_notes) == 1 and "00700" in h1_notes[0]

    # M1: Keyword tightening catches English & Chinese evasion variants ("repurchased HK$50M",
    # "以 5 亿港元购入自家股份", "bought back", buyback tags) in item text, ordinary-ann match, and fallback lead.
    for ev_tag, ev_title, ev_text in (
        ("港股公告", "自愿公告", "repurchased HK$50M of shares on open market (约 5,000 万美元)"),
        ("港股公告", "自愿公告", "以 5 亿港元购入自家股份，约 6,410 万美元"),
        ("港股公告", "自愿公告", "company bought back 100,000 shares for USD 5,000 万美元"),
        ("Repurchase", "Announcement", "spent HK$50M (约 641 万美元)"),
        ("Next Day Disclosure", "Disclosure", "completed transaction for 641 万美元"),
    ):
        ev_card, ev_notes = fc.build_card_with_notes({
            "date": "2026-11-17",
            "lead": "00700 repurchased HK$50M; NVDA 发布 8-K",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "00700",
                    "name": "腾讯控股",
                    "market": "hk",
                    "items": [{
                        "tag": ev_tag,
                        "title": ev_title,
                        "text": ev_text,
                        "date": "2026-11-17",
                        "amount_usd": 50_000_000,
                    }],
                }],
            }],
        })
        ev_md = _all_md(ev_card)
        assert ev_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**", ev_text
        assert "回购相关公告（金额未能从原文核对）" in ev_md, ev_text
        assert "合计" not in ev_md, ev_text
        assert "50M" not in ev_md and "5 亿港元" not in ev_md and "641" not in ev_md, ev_text
        assert len(ev_notes) == 1

    # Ordinary scan announcement matched by id, where model tries to smuggle English/Chinese buyback text:
    smuggle_card, smuggle_notes = fc.build_card_with_notes({
        "date": "2026-11-17",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "ord-1",
                    "tag": "港股公告",
                    "title": "普通公告",
                    "text": "以 5 亿港元购入自家股份",
                    "date": "2026-11-17",
                    "url": "https://www.hkexnews.hk/ord-1.pdf",
                }],
            }],
        }],
    }, scan_data={
        "results": [{
            "ticker": "0700",
            "announcements": [{
                "id": "ord-1",
                "code": "00700",
                "title": "普通公告",
                "type": "港股公告",
                "date": "2026-11-17",
                "url": "https://www.hkexnews.hk/ord-1.pdf",
            }],
        }],
        "buyback_results": {},
    })
    smuggle_md = _all_md(smuggle_card)
    assert "5 亿港元" not in smuggle_md and "普通公告（金额未能从原文核对）" in smuggle_md
    assert len(smuggle_notes) == 1

    # Fallback lead (no priority events) strips English/Chinese buyback clauses while preserving non-buyback clauses.
    lead_card = fc.build_card({
        "date": "2026-11-17",
        "lead": "00700 repurchased HK$50M；00700 以 5 亿港元购入自家股份；NVDA 发布 8-K",
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [{
                "code": "NVDA",
                "name": "NVDA",
                "items": [{"tag": "8-K", "text": "Item 8.01 其他事项", "date": "2026-11-17"}],
            }],
        }],
    })
    assert lead_card["body"]["elements"][0]["content"] == "**NVDA 发布 8-K**"

    # M2: HK rows MUST ignore notice_text in scan-cn-hk (both validation and body amendment detection).
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    hk_raw_rel = "temp/daily-filing-alert/r13-m2-raw.json"
    hk_scan_rel = "temp/daily-filing-alert/r13-m2-scan.json"
    (tmp_path / hk_raw_rel).parent.mkdir(parents=True, exist_ok=True)
    hk_url = "https://www.hkexnews.hk/r13-pm.pdf"
    hk_corr_url = "https://www.hkexnews.hk/r13-pm-corr.pdf"
    (tmp_path / hk_raw_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [{
            "ticker": "09992",
            "ok": True,
            "rows": [
                {
                    "id": "hk-r13-1",
                    "date": "2026-11-16",
                    "title": "翌日披露報表",
                    "pdf_link": hk_url,
                    "notice_text": popmart_text,
                },
                {
                    "id": "hk-r13-2",
                    "date": "2026-11-16",
                    "title": "普通公告",
                    "pdf_link": hk_corr_url,
                    "notice_text": "翌日披露報表\n狀態： 更正",
                },
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_m2, out_m2 = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", hk_raw_rel, "--output", hk_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_m2 == 0 and out_m2["ok"] is True
    m2_doc = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    # HK notice_text is ignored: hk-r13-1 stays unvalidated awaiting PDF, hk-r13-2 does not trigger notice_text amendment.
    assert m2_doc["buyback_results"]["hk-r13-1"]["validated"] is False
    assert m2_doc["buyback_results"]["hk-r13-1"]["error"] == "未解析回购PDF"
    assert "hk-r13-2" not in m2_doc["buyback_results"]
    pm_r13_anns = next(r for r in m2_doc["results"] if r["ticker"] == "9992")["announcements"]
    assert [a["id"] for a in pm_r13_anns] == ["hk-r13-1", "hk-r13-2"]

    # M3: After buyback_parse.py validates hk-r13-1 via PDF, rerunning scan-cn-hk preserves the validated key,
    # and a transient failure in buyback_parse.py does not downgrade an already-validated key.
    _patch_fetch(monkeypatch, lambda url, timeout=20, headers=None, min_interval=0.0: b"%PDF-1.4")
    monkeypatch.setattr(bp, "extract_pdf_text", create_autospec(bp.extract_pdf_text, return_value=popmart_text))
    with redirect_stdout(StringIO()):
        rc_bp1 = bp.main(["--workspace-dir", str(tmp_path), "--scan-output", hk_scan_rel, hk_url])
    assert rc_bp1 == 0
    post_bp1 = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    assert post_bp1["buyback_results"]["hk-r13-1"]["validated"] is True
    assert post_bp1["buyback_results"]["hk-r13-1"]["total_amount_raw"] == "48,972,020"

    # Rerun scan-cn-hk with the same raw JSON (HK emits validated: False placeholder) -> must preserve validated: True!
    _patch_fetch(monkeypatch, offline)
    rc_m3a, out_m3a = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", hk_raw_rel, "--output", hk_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_m3a == 0 and out_m3a["ok"] is True
    post_m3a = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    assert post_m3a["buyback_results"]["hk-r13-1"]["validated"] is True
    assert post_m3a["buyback_results"]["hk-r13-1"]["total_amount_raw"] == "48,972,020"

    # Rerun scan-cn-hk with partial raw JSON omitting 09992 -> must STILL preserve hk-r13-1 validated entry!
    partial_raw_rel = "temp/daily-filing-alert/r13-m3-partial-raw.json"
    (tmp_path / partial_raw_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [],
    }), encoding="utf-8")
    rc_m3b, out_m3b = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", partial_raw_rel, "--output", hk_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_m3b == 0 and out_m3b["ok"] is True
    post_m3b = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    assert post_m3b["buyback_results"]["hk-r13-1"]["validated"] is True
    assert post_m3b["buyback_results"]["hk-r13-1"]["total_amount_raw"] == "48,972,020"

    # Transient failure on buyback_parse.py --scan-output does NOT downgrade existing validated key.
    def _fail_fetch(url, timeout=20, headers=None, min_interval=0.0):
        raise RuntimeError("transient 502")

    _patch_fetch(monkeypatch, _fail_fetch)
    fail_buf = StringIO()
    with redirect_stdout(fail_buf):
        rc_bp_fail = bp.main(["--workspace-dir", str(tmp_path), "--scan-output", hk_scan_rel, hk_url])
    assert rc_bp_fail == 0
    assert "transient 502" in fail_buf.getvalue()
    post_bp_fail = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    assert post_bp_fail["buyback_results"]["hk-r13-1"]["validated"] is True
    assert post_bp_fail["buyback_results"]["hk-r13-1"]["total_amount_raw"] == "48,972,020"

    # When a title amendment supersedes hk-r13-1 on rerun, the dropped key is NOT preserved.
    amend_raw_rel = "temp/daily-filing-alert/r13-m3-amend-raw.json"
    (tmp_path / amend_raw_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [{
            "ticker": "09992",
            "ok": True,
            "rows": [
                {
                    "id": "hk-r13-1",
                    "date": "2026-11-16",
                    "title": "翌日披露報表",
                    "pdf_link": hk_url,
                },
                {
                    "id": "hk-r13-corr",
                    "date": "2026-11-16",
                    "title": "翌日披露報表（更正）",
                    "pdf_link": "https://www.hkexnews.hk/r13-pm-title-corr.pdf",
                },
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_m3c, out_m3c = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", amend_raw_rel, "--output", hk_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_m3c == 0 and out_m3c["ok"] is True
    post_m3c = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    assert "hk-r13-1" not in post_m3c["buyback_results"]
    assert "hk-r13-corr" in post_m3c["buyback_results"]


def test_r14_unified_model_amount_gate_and_codex_repros(tmp_path: Path) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r14")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    treasury_text = (fixtures / "ff305_hk_buyback_treasury.txt").read_text(encoding="utf-8")
    pm_parsed = bp.parse(popmart_text)

    def _all_md(card: dict) -> str:
        parts = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
            elif el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if sub.get("tag") == "markdown":
                        parts.append(sub.get("content", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Codex repro 1: Matching an ordinary scan announcement (by id & url) while setting
    # tag="回购" or title="回购..." and text="金额 148,972,020 港元" must be caught as unverified.
    # ------------------------------------------------------------------
    for bad_tag, bad_title in (("回购", "普通公告"), ("港股公告", "回购公告")):
        c1, n1 = fc.build_card_with_notes({
            "date": "2026-11-16",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "09992",
                    "name": "泡泡玛特",
                    "market": "hk",
                    "items": [{
                        "id": "ord-r14-1",
                        "tag": bad_tag,
                        "title": bad_title,
                        "text": "金额 148,972,020 港元",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/ord-r14-1.pdf",
                    }],
                }],
            }],
        }, scan_data={
            "results": [{
                "ticker": "9992",
                "name": "泡泡玛特",
                "announcements": [{
                    "id": "ord-r14-1",
                    "code": "09992",
                    "title": "普通公告",
                    "type": "港股公告",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/ord-r14-1.pdf",
                }],
            }],
            "buyback_results": {},
        })
        md1 = _all_md(c1)
        assert "148,972,020" not in md1
        assert "普通公告（金额未能从原文核对）" in md1
        assert len(n1) == 1 and "09992" in n1[0]

    # ------------------------------------------------------------------
    # Codex repro 2: Amendment relation determined from scan_data per company even when
    # payload omits the amendment entry; original buyback amount must not appear & gap logged.
    # ------------------------------------------------------------------
    orig_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-r14-orig",
        url="https://www.hkexnews.hk/pm-r14-orig.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    corr_text = (
        popmart_text
        .replace("340,000", "350,000")
        .replace("48,972,020", "50,400,000")
        .replace("狀態： 新提交", "狀態： 更正")
    )
    _corr_raw, corr_rec = bp.validate_for_scan(
        corr_text,
        ann_id="pm-r14-corr",
        url="https://www.hkexnews.hk/pm-r14-corr.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    assert corr_rec["is_amendment"] is True

    c2, n2 = fc.build_card_with_notes({
        "date": "2026-11-16",
        "lead": "09992 单日回购 4,897 万港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "11-16 回购 340,000 股，金额 48,972,020 港元",
                "items": [{
                    "id": "pm-r14-orig",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/pm-r14-orig.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-r14-orig": orig_rec, "pm-r14-corr": corr_rec}})
    md2 = _all_md(c2)
    assert "48,972,020" not in md2 and "4,897" not in md2 and "340,000" not in md2
    assert "存在更正回购公告，原公告已过滤" in md2
    assert len(n2) == 1 and "存在更正回购公告，已过滤" in n2[0]

    # ------------------------------------------------------------------
    # Codex repro 3: Fallback lead strips Chinese-numeral buyback phrases without requiring \d.
    # ------------------------------------------------------------------
    c3_only = fc.build_card({
        "date": "2026-11-17",
        "lead": "泡泡玛特回购五亿港元",
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [{
                "code": "NVDA",
                "name": "NVDA",
                "items": [{"tag": "8-K", "text": "Item 8.01 其他事项", "date": "2026-11-17"}],
            }],
        }],
    })
    assert "泡泡玛特" not in _all_md(c3_only) and "五亿" not in _all_md(c3_only)

    c3_mixed = fc.build_card({
        "date": "2026-11-17",
        "lead": "泡泡玛特回购五亿港元；NVDA 发布 8-K",
        "sections": [{
            "key": "us",
            "title": "美股 · SEC",
            "companies": [{
                "code": "NVDA",
                "name": "NVDA",
                "items": [{"tag": "8-K", "text": "Item 8.01 其他事项", "date": "2026-11-17"}],
            }],
        }],
    })
    assert c3_mixed["body"]["elements"][0]["content"] == "**NVDA 发布 8-K**"

    # ------------------------------------------------------------------
    # Codex repro 4: Unified render-end gate on date (strict YYYY-MM-DD), company name
    # (prefers watchlist/scan), footer, summary, ordinary item text/title/tag on buyback-related
    # companies (including Chinese numerals, fullwidth digits/units, spaces), while validated
    # code-generated buyback sentences bypass the gate cleanly.
    # ------------------------------------------------------------------
    c4, n4 = fc.build_card_with_notes({
        "date": "2026-11-16，金额 148,972,020 港元",
        "lead": "泡泡玛特耗资五亿港元；NVDA 8-K",
        "footer": "数据源：SEC EDGAR · 09992 回购 148,972,020 港元；五亿港元；１００％",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [
                {
                    "code": "09992",
                    "name": "泡泡玛特（回购 148,972,020 港元）",
                    "market": "hk",
                    "summary": "耗资四千八百九十七万二千零二十港元，占比 0.91％",
                    "items": [
                        {
                            "id": "pm-r14-valid",
                            "tag": "回购",
                            "title": "翌日披露報表",
                            "date": "2026-11-16，金额 148,972,020 港元",
                            "url": "https://www.hkexnews.hk/pm-r14-valid.pdf",
                        },
                        {
                            "tag": "其他 999 万港元",
                            "title": "普通通告 五亿港元",
                            "text": "公告涉及 148 972 020 港元 及 １４８，９７２，０２０　港元 和 两万五千股",
                            "date": "2026-11-16，金额 99,999,999 港元",
                            "amount_usd": 50_000_000,
                        },
                    ],
                },
                {
                    "code": "01234",
                    "name": "测试公司 回购五亿港元",
                    "market": "hk",
                    "summary": "昨日耗资 88,888,888 港元",
                    "items": [{
                        "tag": "港股公告",
                        "title": "公告",
                        "text": "披露业绩 88,888,888 港元",
                        "date": "2026-11-99",
                    }],
                },
            ],
        }],
    }, scan_data={
        "report_date": "2026-11-16",
        "buyback_results": {
            "pm-r14-valid": bp.scan_record_from_parsed(
                pm_parsed,
                ann_id="pm-r14-valid",
                url="https://www.hkexnews.hk/pm-r14-valid.pdf",
                code="09992",
                date="2026-11-16",
                title="翌日披露報表",
            ),
            "other-bb": {
                "id": "other-bb",
                "code": "01234",
                "title": "翌日披露報表",
                "date": "2026-11-16",
                "url": "https://www.hkexnews.hk/01234.pdf",
                "validated": False,
                "is_buyback": True,
                "error": "解析失败",
            },
        },
    })
    assert n4 == []
    assert c4["header"]["subtitle"]["content"] == "2026-11-16"
    md4 = _all_md(c4)
    for leaked in (
        "148,972,020",
        "148 972 020",
        "１４８，９７２，０２０",
        "99,999,999",
        "999 万港元",
        "五亿港元",
        "四千八百九十七万",
        "两万五千股",
        "0.91％",
        "100％",
        "１００％",
        "88,888,888",
        "2026-11-99",
        "合计",
    ):
        assert leaked not in md4, leaked
    # Watchlist name & scan date preserved; code-generated validated buyback sentence preserved!
    assert "泡泡玛特" in md4
    assert "测试公司" in md4
    assert "48,972,020 港元" in md4
    assert "340,000 股" in md4
    assert "[11-16](https://www.hkexnews.hk/pm-r14-valid.pdf)" in md4

    # ------------------------------------------------------------------
    # Codex repro 5: Broken mandate cumulative percentage (internal space, newline, Cf chars)
    # MUST raise an error rather than silently omitting mandate_cum_pct.
    # ------------------------------------------------------------------
    for bad_pct in ("0.9 1 %", "0.9\n1 %", "0.9\u20601 %", "0.\u200b91 %", "0.91\u2060%"):
        broken_tr = treasury_text.replace("0.14 %", bad_pct)
        res_tr = bp.parse(broken_tr)
        assert "error" in res_tr and "mandate_cum_pct" not in res_tr, repr(bad_pct)

        broken_pm = popmart_text.replace("0.3699 %", bad_pct)
        res_pm = bp.parse(broken_pm)
        assert "error" in res_pm and "mandate_cum_pct" not in res_pm, repr(bad_pct)

        with pytest.raises(ValueError, match="数字格式"):
            ndr.parse_ndr(
                ndr.SAMPLE_TREASURY.replace(
                    "第二章節註釋：",
                    f"B. 其他資料\n百分比 {bad_pct}\n第二章節註釋：",
                )
            )


def test_r15_glm_final_review_fixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r15")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    pm_parsed = bp.parse(popmart_text)

    def _all_md(card: dict) -> str:
        parts = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
            elif el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if sub.get("tag") == "markdown":
                        parts.append(sub.get("content", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # H1a: Matching A-share 300308 with SAMPLE_TREASURY succeeds end-to-end.
    # ------------------------------------------------------------------
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    a_raw_rel = "temp/daily-filing-alert/r15-a-raw.json"
    a_scan_rel = "temp/daily-filing-alert/r15-a-scan.json"
    a_payload_rel = "temp/daily-filing-alert/r15-a-payload.json"
    a_card_rel = "temp/daily-filing-alert/r15-a-card.json"
    (tmp_path / a_raw_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / a_raw_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [{
            "ticker": "300308",
            "ok": True,
            "rows": [{
                "id": "cn-300308-ok",
                "date": "2026-11-16",
                "title": "H股公告-翌日披露报表",
                "pdf_link": "https://static.cninfo.com.cn/300308-treasury.pdf",
                "notice_text": ndr.SAMPLE_TREASURY,
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_a, out_a = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", a_raw_rel, "--output", a_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_a == 0 and out_a["ok"] is True
    assert not any("300308" in err for err in out_a["day_errors"])
    (tmp_path / a_payload_rel).write_text(json.dumps({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "300308",
                "name": "中际旭创",
                "market": "cn",
                "items": [{
                    "id": "cn-300308-ok",
                    "tag": "回购",
                    "title": "H股公告-翌日披露报表",
                    "date": "2026-11-16",
                    "url": "https://static.cninfo.com.cn/300308-treasury.pdf",
                }],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_ar, out_ar = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", a_payload_rel, "--cn-hk", a_scan_rel, "--output", a_card_rel,
    ])
    assert rc_ar == 0 and out_ar["ok"] is True and out_ar["day_errors"] == []
    a_card = json.loads((tmp_path / a_card_rel).read_text(encoding="utf-8"))
    assert a_card["body"]["elements"][0]["content"] == "**中际旭创 单日回购 7.95 亿元人民币**"
    assert "794,631,549.33 元人民币" in _all_md(a_card)

    # ------------------------------------------------------------------
    # H1b: HK PDF cross-company URL mix-up (PopMart 09992 PDF attached to Tencent 00700)
    # caught at BOTH buyback_parse.py --scan-output AND render-card!
    # ------------------------------------------------------------------
    hk_mix_url = "https://www.hkexnews.hk/listedco/listconews/sehk/2026/1116/wrong-pm-on-00700.pdf"
    hk_raw_rel = "temp/daily-filing-alert/r15-hk-raw.json"
    hk_scan_rel = "temp/daily-filing-alert/r15-hk-scan.json"
    hk_payload_rel = "temp/daily-filing-alert/r15-hk-payload.json"
    hk_card_rel = "temp/daily-filing-alert/r15-hk-card.json"
    (tmp_path / hk_raw_rel).write_text(json.dumps({
        "end": "2026-11-16",
        "limit": 50,
        "calls": [{
            "ticker": "00700",
            "ok": True,
            "rows": [{
                "id": "hk-00700-mix",
                "date": "2026-11-16",
                "title": "翌日披露報表",
                "pdf_link": hk_mix_url,
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_hs, out_hs = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", hk_raw_rel, "--output", hk_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_hs == 0 and out_hs["ok"] is True

    _patch_fetch(monkeypatch, lambda url, timeout=20, headers=None, min_interval=0.0: b"%PDF-1.4")
    monkeypatch.setattr(bp, "extract_pdf_text", create_autospec(bp.extract_pdf_text, return_value=popmart_text))
    with redirect_stdout(StringIO()):
        rc_hbp = bp.main(["--workspace-dir", str(tmp_path), "--scan-output", hk_scan_rel, hk_mix_url])
    assert rc_hbp == 0
    hk_scan_doc = json.loads((tmp_path / hk_scan_rel).read_text(encoding="utf-8"))
    mix_rec = hk_scan_doc["buyback_results"]["hk-00700-mix"]
    assert mix_rec["validated"] is False
    assert mix_rec["stock_code"] == "09992"
    assert "09992" in mix_rec["error"] and "0700" in mix_rec["error"]
    assert "total_amount" not in mix_rec and "groups" not in mix_rec
    assert any("09992" in err and "0700" in err for err in hk_scan_doc["day_errors"])

    (tmp_path / hk_payload_rel).write_text(json.dumps({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "hk-00700-mix",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": hk_mix_url,
                }],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_hr, out_hr = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", hk_payload_rel, "--cn-hk", hk_scan_rel, "--output", hk_card_rel,
    ])
    assert rc_hr == 0 and out_hr["ok"] is True
    assert len(out_hr["day_errors"]) == 1 and "00700" in out_hr["day_errors"][0]
    hk_card = json.loads((tmp_path / hk_card_rel).read_text(encoding="utf-8"))
    assert hk_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
    hk_md = _all_md(hk_card)
    assert "回购相关公告（金额未能从原文核对）" in hk_md
    assert "48,972,020" not in hk_md and "340,000" not in hk_md and "wrong-pm-on-00700" not in hk_md

    # ------------------------------------------------------------------
    # H1c: Render-time check rejects validated: True scan record whose groups[].stock_code
    # or top-level stock_code conflicts with company code, and does not let a conflicting
    # amendment suppress the real company's buyback.
    # Also verify normalized zero-padding ("0700" == "00700") and placeholder ("不適用") pass.
    # ------------------------------------------------------------------
    forged_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="forged-00700",
        url="https://www.hkexnews.hk/forged.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    forged_rec["code"] = "00700"  # simulates scan record attributed to 00700 while groups[].stock_code="09992"
    render_h1_card, render_h1_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "forged-00700",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/forged.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"forged-00700": forged_rec}})
    assert render_h1_card["body"]["elements"][0]["content"] == "**腾讯控股 回购股份**"
    assert "48,972,020" not in _all_md(render_h1_card)
    assert "回购相关公告（金额未能从原文核对）" in _all_md(render_h1_card)
    assert len(render_h1_notes) == 1 and "09992" in render_h1_notes[0]

    # Normalized zero-padded ("09992" vs "9992") passes cleanly; placeholder "不適用" is skipped cleanly.
    norm_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-norm",
        url="https://www.hkexnews.hk/pm-norm.pdf",
        code="9992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    assert norm_rec["validated"] is True
    norm_card, norm_notes = fc.build_card_with_notes({
        "date": "2026-11-16",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "9992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "id": "pm-norm",
                    "tag": "回购",
                    "title": "翌日披露報表",
                    "date": "2026-11-16",
                    "url": "https://www.hkexnews.hk/pm-norm.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-norm": norm_rec}})
    assert norm_notes == []
    assert norm_card["body"]["elements"][0]["content"] == "**泡泡玛特 单日回购 4,897 万港元**"

    placeholder_text = ndr.SAMPLE_TREASURY.replace("證券代號 (如上市) 300308", "證券代號 (如上市) 不適用")
    _ph_raw, ph_rec = bp.validate_for_scan(
        placeholder_text,
        ann_id="ph-ok",
        url="https://static.cninfo.com.cn/ph.pdf",
        code="300750",
        date="2026-11-16",
        title="翌日披露报表",
    )
    assert ph_rec["validated"] is True

    # ------------------------------------------------------------------
    # M3: _report_end anchors on --as-of HK date, ignores raw JSON `end`, and logs gap when mismatched.
    # ------------------------------------------------------------------
    _patch_fetch(monkeypatch, offline)
    m3_raw_rel = "temp/daily-filing-alert/r15-m3-raw.json"
    m3_scan_rel = "temp/daily-filing-alert/r15-m3-scan.json"
    (tmp_path / m3_raw_rel).write_text(json.dumps({
        "end": "2026-11-10",
        "limit": 50,
        "calls": [{
            "ticker": "300308",
            "ok": True,
            "rows": [
                {"date": "2026-11-10", "title": "旧窗口公告", "pdf_link": "https://static.cninfo.com.cn/old10.pdf"},
                {"date": "2026-11-16", "title": "当日公告", "pdf_link": "https://static.cninfo.com.cn/new16.pdf"},
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_m3, out_m3 = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", m3_raw_rel, "--output", m3_scan_rel,
        "--as-of", "2026-11-16T20:30:00+08:00",
    ])
    assert rc_m3 == 0 and out_m3["ok"] is True
    m3_doc = json.loads((tmp_path / m3_scan_rel).read_text(encoding="utf-8"))
    m3_300308 = next(r for r in m3_doc["results"] if r["ticker"] == "300308")
    assert [a["title"] for a in m3_300308["announcements"]] == ["当日公告"]
    assert any("2026-11-10" in err and "2026-11-16" in err for err in m3_doc["day_errors"])
    assert out_m3["day_errors"] == m3_doc["day_errors"]

    # ------------------------------------------------------------------
    # L1: SKILL.md distinguishes buyback validated totals vs structured fields for Form 4 / 增持.
    # ------------------------------------------------------------------
    skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "仅根据结构化的已校验「合共」金额按优先级" not in skill_md
    assert "回购仅根据结构化的已校验「合共」金额" in skill_md
    assert "Form 4 买入与 A 股/港股增持根据结构化字段" in skill_md

    # ------------------------------------------------------------------
    # L3: Company with empty items whose summary contains buyback/NDR amount
    # replaces summary with fixed text, strips amount, and emits a gap note.
    # ------------------------------------------------------------------
    for bad_summary in (
        "今日回购 340,000 股，金额 48,972,020 港元",
        "昨日回购五亿港元",
        "发布翌日披露报表，涉资 5,000 万港元",
    ):
        l3_card, l3_notes = fc.build_card_with_notes({
            "date": "2026-11-16",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "09992",
                    "name": "泡泡玛特",
                    "market": "hk",
                    "summary": bad_summary,
                    "items": [],
                }],
            }],
        })
        l3_md = _all_md(l3_card)
        assert l3_card["body"]["elements"][0]["content"] == "**泡泡玛特 回购股份**"
        assert "发布回购公告，金额未能从原文核对" in l3_md
        for leaked in ("48,972,020", "340,000", "五亿", "5,000"):
            assert leaked not in l3_md
        assert len(l3_notes) == 1 and "09992" in l3_notes[0]


def test_r16_codex_r11_review_fixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r16")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    pm_parsed = bp.parse(popmart_text)

    def _all_md(card: dict) -> str:
        parts = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
            elif el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if sub.get("tag") == "markdown":
                        parts.append(sub.get("content", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 1. Codex repro 1: Expanded amount-gate unit table (CN/EN currencies, scales, prefix/suffix,
    #    thousands/decimals/Chinese numerals) — including "148,972,020 HKD" in footer & ordinary item.
    # ------------------------------------------------------------------
    pm_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-r16-valid",
        url="https://www.hkexnews.hk/pm-r16-valid.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    c1, n1 = fc.build_card_with_notes({
        "date": "2026-11-16",
        "footer": "数据源：SEC EDGAR · 回购金额 148,972,020 HKD",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "涉及回购金额 148,972,020 HKD 及 5,000 港币",
                "items": [
                    {
                        "id": "pm-r16-valid",
                        "tag": "回购",
                        "title": "翌日披露報表",
                        "date": "2026-11-16",
                        "url": "https://www.hkexnews.hk/pm-r16-valid.pdf",
                    },
                    {
                        "tag": "港股公告",
                        "title": "普通公告 148,972,020 HKD",
                        "text": "涉资 148,972,020 HKD",
                        "date": "2026-11-16",
                    },
                ],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-r16-valid": pm_rec}})
    assert n1 == []
    md1 = _all_md(c1)
    assert "148,972,020" not in md1
    assert "5,000 港币" not in md1
    assert "48,972,020 港元" in md1
    assert "340,000 股" in md1

    # Full matrix of unit variants (before/after number, Arabic thousands/decimals, Chinese numerals)
    gate_samples = [
        "148,972,020 HKD",
        "HKD 148,972,020",
        "148,972,020 港币",
        "港币 148,972,020",
        "148,972,020 港元",
        "港元 148,972,020",
        "148,972,020 元",
        "148,972,020 人民币",
        "人民币 148,972,020",
        "148,972,020 元人民币",
        "148,972,020 RMB",
        "RMB 148,972,020",
        "148,972,020 CNY",
        "CNY 148,972,020",
        "148,972,020 美元",
        "美元 148,972,020",
        "148,972,020 美金",
        "美金 148,972,020",
        "148,972,020 USD",
        "USD 148,972,020",
        "148,972,020 日元",
        "日元 148,972,020",
        "148,972,020 欧元",
        "欧元 148,972,020",
        "148,972,020 英镑",
        "英镑 148,972,020",
        "HK$ 148,972,020",
        "148,972,020 HK$",
        "US$ 148,972,020",
        "148,972,020 US$",
        "$148,972,020",
        "148,972,020$",
        "¥148,972,020",
        "148,972,020¥",
        "￥148,972,020",
        "148,972,020￥",
        "1.5 bn",
        "1.5bn USD",
        "USD 1.5 bn",
        "148.97 m",
        "148.97m HKD",
        "HK$ 148.97m",
        "500 k",
        "500k",
        "500 千",
        "500 千股",
        "5 百万",
        "5 百万港币",
        "1.5 亿",
        "4,897 万",
        "12,000 股",
        "0.91%",
        "百分之 0.91",
        "五亿港币",
        "五百万日元",
        "港币五百万",
        "人民币三点五亿",
        "百分之五",
    ]
    digit_check_re = re.compile(r"[0-9０-９〇零一二三四五六七八九十百千万亿两壹貳參肆伍陸柒捌玖拾佰仟萬億兩]")
    for expr in gate_samples:
        stripped = fc._strip_model_digits(f"前缀 {expr} 后缀")
        assert not digit_check_re.search(stripped), (expr, stripped)
        assert stripped.startswith("前缀") and stripped.endswith("后缀"), (expr, stripped)

    # Strict digit removal also strips bare/chinese digits regardless of unit context
    sample_mixed = "2026-11-16 8-K 10-K Form 4 09992 覆盖过去 24 小时 千万不要 万科"
    assert not digit_check_re.search(fc._strip_model_digits(sample_mixed))

    # ------------------------------------------------------------------
    # 2. Codex repro 2: Non-buyback NDR (option exercise) retains & checks Section I
    #    `classes[].stock_code` against company code at both scan and render stages.
    # ------------------------------------------------------------------
    _opt_raw, opt_conflict = bp.validate_for_scan(
        ndr.SAMPLE_OPTION,
        ann_id="tx-opt-conflict",
        url="https://www.hkexnews.hk/tx-opt.pdf",
        code="00700",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert opt_conflict["validated"] is False
    assert opt_conflict["is_buyback"] is False
    assert opt_conflict["stock_code"] == "09992"
    assert opt_conflict["classes"][0]["stock_code"] == "09992"
    assert "09992" in opt_conflict["error"] and "00700" in opt_conflict["error"]

    # CLI buyback_parse.py --scan-output catches cross-company non-buyback NDR and render-card blocks it
    def offline(url, timeout=20, headers=None, min_interval=0.0):
        raise AssertionError(url)

    _patch_fetch(monkeypatch, offline)
    opt_url = "https://www.hkexnews.hk/listedco/listconews/sehk/2026/0923/tx-opt.pdf"
    opt_raw_rel = "temp/daily-filing-alert/r16-opt-raw.json"
    opt_scan_rel = "temp/daily-filing-alert/r16-opt-scan.json"
    opt_payload_rel = "temp/daily-filing-alert/r16-opt-payload.json"
    opt_card_rel = "temp/daily-filing-alert/r16-opt-card.json"
    (tmp_path / opt_raw_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / opt_raw_rel).write_text(json.dumps({
        "end": "2026-09-23",
        "limit": 50,
        "calls": [{
            "ticker": "00700",
            "ok": True,
            "rows": [{
                "id": "hk-00700-opt",
                "date": "2026-09-23",
                "title": "翌日披露報表",
                "pdf_link": opt_url,
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_os, out_os = _run(cli, [
        "--json", "scan-cn-hk", "--workspace-dir", str(tmp_path),
        "--days", "1", "--input", opt_raw_rel, "--output", opt_scan_rel,
        "--as-of", "2026-09-23T20:30:00+08:00",
    ])
    assert rc_os == 0 and out_os["ok"] is True

    _patch_fetch(monkeypatch, lambda url, timeout=20, headers=None, min_interval=0.0: b"%PDF-1.4")
    monkeypatch.setattr(bp, "extract_pdf_text", create_autospec(bp.extract_pdf_text, return_value=ndr.SAMPLE_OPTION))
    with redirect_stdout(StringIO()):
        rc_obp = bp.main(["--workspace-dir", str(tmp_path), "--scan-output", opt_scan_rel, opt_url])
    assert rc_obp == 0
    opt_scan_doc = json.loads((tmp_path / opt_scan_rel).read_text(encoding="utf-8"))
    rec_00700 = opt_scan_doc["buyback_results"]["hk-00700-opt"]
    assert rec_00700["validated"] is False
    assert rec_00700["stock_code"] == "09992"
    assert rec_00700["classes"][0]["stock_code"] == "09992"
    assert any("09992" in err and "0700" in err for err in opt_scan_doc["day_errors"])

    (tmp_path / opt_payload_rel).write_text(json.dumps({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "hk-00700-opt",
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "date": "2026-09-23",
                    "url": opt_url,
                }],
            }],
        }],
    }, ensure_ascii=False), encoding="utf-8")
    rc_or, out_or = _run(cli, [
        "--json", "render-card", "--workspace-dir", str(tmp_path),
        "--payload", opt_payload_rel, "--cn-hk", opt_scan_rel, "--output", opt_card_rel,
    ])
    assert rc_or == 0 and out_or["ok"] is True
    assert len(out_or["day_errors"]) == 1 and "00700" in out_or["day_errors"][0]
    opt_card_md = _all_md(json.loads((tmp_path / opt_card_rel).read_text(encoding="utf-8")))
    for leaked in ("12,000", "1.2%", "15.5", "行使期權"):
        assert leaked not in opt_card_md
    assert "未能从原文核对" in opt_card_md

    # Even a forged validated=True, is_buyback=False record carrying classes[].stock_code="09992"
    # under company "00700" is caught at render time.
    forged_opt_card, forged_opt_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "id": "forged-opt",
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "date": "2026-09-23",
                    "url": opt_url,
                }],
            }],
        }],
    }, scan_data={"buyback_results": {
        "forged-opt": {
            "id": "forged-opt",
            "url": opt_url,
            "code": "00700",
            "date": "2026-09-23",
            "title": "翌日披露報表",
            "validated": True,
            "is_buyback": False,
            "digest": "H股根據股份期權計劃行使期權：12,000股，占已发行股份1.2%，每股15.5港元",
            "classes": [{"share_class": "H", "stock_code": "09992"}],
        },
    }})
    forged_opt_md = _all_md(forged_opt_card)
    assert "12,000" not in forged_opt_md and "15.5" not in forged_opt_md
    assert len(forged_opt_notes) == 1 and "09992" in forged_opt_notes[0]

    # Matching company code ("09992") preserves classes[].stock_code and renders option digest cleanly.
    _ok_raw, ok_opt = bp.validate_for_scan(
        ndr.SAMPLE_OPTION,
        ann_id="pm-opt-ok",
        url="https://www.hkexnews.hk/pm-opt-ok.pdf",
        code="09992",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert ok_opt["validated"] is True and ok_opt["is_buyback"] is False
    assert ok_opt["stock_code"] == "09992"
    assert ok_opt["classes"][0]["stock_code"] == "09992"
    ok_opt_card, ok_opt_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "id": "pm-opt-ok",
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "date": "2026-09-23",
                    "url": "https://www.hkexnews.hk/pm-opt-ok.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"pm-opt-ok": ok_opt}})
    assert ok_opt_notes == []
    assert "H股根據股份期權計劃行使期權：12,000股" in _all_md(ok_opt_card)

    # ------------------------------------------------------------------
    # 3. Codex repro 3: Strict number format & boundary checks on Section I
    #    (price, shares, percentage, balance) — internal space/newline/Cf MUST error.
    # ------------------------------------------------------------------
    bad_sec1_cases = [
        ndr.SAMPLE_OPTION.replace("15.5", "15. 5"),
        ndr.SAMPLE_OPTION.replace("15.5", "15.\u20605"),
        ndr.SAMPLE_OPTION.replace("15.5", "1\u200b5.5"),
        ndr.SAMPLE_OPTION.replace("15.5", "15\n.5"),
        ndr.SAMPLE_OPTION.replace("15.5", "15.\n5"),
        ndr.SAMPLE_OPTION.replace("15.5", "15.5abc"),
        ndr.SAMPLE_OPTION.replace("12,000", "12, 000"),
        ndr.SAMPLE_OPTION.replace("12,000", "12,\u2060000"),
        ndr.SAMPLE_OPTION.replace("12,000", "1\u200b2,000"),
        ndr.SAMPLE_OPTION.replace("12,000", "12 000"),
        ndr.SAMPLE_OPTION.replace("12,000", "12,0000"),
        ndr.SAMPLE_OPTION.replace("1.2 %", "1. 2 %"),
        ndr.SAMPLE_OPTION.replace("1.2 %", "1.\u20602 %"),
        ndr.SAMPLE_OPTION.replace("1.2 %", "1.2\u2060%"),
        ndr.SAMPLE_OPTION.replace("1.2 %", "1 2 %"),
        ndr.SAMPLE_OPTION.replace("1,000,000 1,000,000", "1,000,\u2060000 1,000,000"),
        ndr.SAMPLE_OPTION.replace("1,000,000 1,000,000", "1,000, 000 1,000,000"),
        ndr.SAMPLE_TREASURY.replace("-856,980", "-856, 980"),
        ndr.SAMPLE_TREASURY.replace("0.077 %", "0.07\u20607 %"),
        ndr.SAMPLE_TREASURY.replace("927.2463", "927. 2463"),
    ]
    for bad_text in bad_sec1_cases:
        with pytest.raises(ValueError, match="第一章節"):
            ndr.parse_ndr(bad_text)
        _raw_bad, bad_rec = bp.validate_for_scan(
            bad_text,
            ann_id="bad-sec1",
            url="https://www.hkexnews.hk/bad-sec1.pdf",
            code="09992",
            date="2026-09-23",
            title="翌日披露報表",
        )
        assert bad_rec["validated"] is False
        assert bad_rec.get("error")


def test_r17_strict_digit_gate_code_footer_lead_and_sec1_single_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r17")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    pm_parsed = bp.parse(popmart_text)
    digit_re = re.compile(r"[0-9０-９〇零一二三四五六七八九十百千万亿两壹貳參肆伍陸柒捌玖拾佰仟萬億兩]")

    def _all_md(card: dict) -> str:
        parts = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
            elif el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if sub.get("tag") == "markdown":
                        parts.append(sub.get("content", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 1. Codex repro 1: Currency + punctuation + number (and ANY model digits)
    #    on buyback/NDR companies are completely removed; footer is code-generated;
    #    validated code-generated sentences keep their numbers via _code_fields.
    # ------------------------------------------------------------------
    pm_rec = bp.scan_record_from_parsed(
        pm_parsed,
        ann_id="pm-r17-valid",
        url="https://www.hkexnews.hk/pm-r17-valid.pdf",
        code="09992",
        date="2026-11-16",
        title="翌日披露報表",
    )
    for bad_footer in (
        "回购金额（港元）：148,972,020",
        "回购金额为 HKD：148,972,020",
    ):
        c1, n1 = fc.build_card_with_notes({
            "date": "2026-11-16",
            "footer": bad_footer,
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [
                    {
                        "code": "09992",
                        "name": "泡泡玛特 148,972,020",
                        "market": "hk",
                        "summary": "回购金额（港元）：148,972,020；回购金额为 HKD：148,972,020",
                        "items": [
                            {
                                "id": "pm-r17-valid",
                                "tag": "回购",
                                "title": "翌日披露報表",
                                "date": "2026-11-16",
                                "url": "https://www.hkexnews.hk/pm-r17-valid.pdf",
                            },
                            {
                                "tag": "港股公告 888",
                                "title": "公告（港元）：148,972,020",
                                "text": "涉资金额（港元）：148,972,020；涉资金额为 HKD：148,972,020；１２３；二〇二六年",
                                "date": "2026-11-15",
                            },
                        ],
                    },
                    {
                        "code": "NVDA",
                        "name": "NVDA",
                        "market": "us",
                        "summary": "发布 10-K 年报，营收 350 亿美元",
                        "items": [
                            {
                                "tag": "10-K",
                                "text": "全年营收 350 亿美元",
                                "date": "2026-11-16",
                                "url": "https://www.sec.gov/nvda-10k.htm",
                            },
                        ],
                    },
                ],
            }],
        }, scan_data={"report_date": "2026-11-16", "buyback_results": {"pm-r17-valid": pm_rec}})
        assert n1 == []
        md1 = _all_md(c1)
        assert "148,972,020" not in md1
        assert "回购金额" not in md1
        assert "888" not in md1
        assert "１２３" not in md1
        assert "二〇二六" not in md1
        assert "2026-11-15" not in md1 and "11-15" not in md1
        ordinary_line = next(ln for ln in md1.splitlines() if ln.startswith("<font color='grey'>· 涉资金额"))
        assert not digit_re.search(ordinary_line), ordinary_line
        # Code-generated footer, watchlist name, scan date, and validated PopMart buyback numbers remain
        assert "数据源：SEC EDGAR · 巨潮公告 · HKEXnews" in md1
        assert "泡泡玛特" in md1
        assert "48,972,020 港元" in md1
        assert "340,000 股" in md1
        assert "[11-16](https://www.hkexnews.hk/pm-r17-valid.pdf)" in md1
        # Non-NDR/non-buyback company (NVDA) keeps its digits intact
        assert "全年营收 350 亿美元" in md1

    # Footer is ALWAYS code-generated even when payload omits footer or has no buyback companies
    empty_footer_card = fc.build_card({
        "date": "2026-11-16",
        "footer": "模型私自填写的页脚 999,999 港元",
        "sections": [],
    })
    empty_md = _all_md(empty_footer_card)
    assert "模型私自填写" not in empty_md and "999,999" not in empty_md
    assert "数据源：SEC EDGAR · 巨潮公告 · HKEXnews" in empty_md

    # ------------------------------------------------------------------
    # 2. Codex repro 2: Confirmed non-buyback NDR (12,000 shares, HKD 15.5) STILL
    #    marks the company as NDR-related and suppresses model lead/footer/fields:
    #    model "999,999 股，每股 155 港元" in lead, footer, summary, ordinary items, name
    #    must NEVER appear anywhere on the card.
    # ------------------------------------------------------------------
    _opt_raw, opt_rec = bp.validate_for_scan(
        ndr.SAMPLE_OPTION,
        ann_id="pm-r17-opt",
        url="https://www.hkexnews.hk/pm-r17-opt.pdf",
        code="09992",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert opt_rec["validated"] is True and opt_rec["is_buyback"] is False

    c2, n2 = fc.build_card_with_notes({
        "date": "2026-09-23",
        "lead": "09992 行权 999,999 股，每股 155 港元",
        "footer": "数据源：HKEXnews · 行权 999,999 股，每股 155 港元",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特 999,999",
                "market": "hk",
                "summary": "行权 999,999 股，每股 155 港元",
                "items": [
                    {
                        "id": "pm-r17-opt",
                        "tag": "港股公告",
                        "title": "翌日披露報表",
                        "date": "2026-09-23",
                        "url": "https://www.hkexnews.hk/pm-r17-opt.pdf",
                    },
                    {
                        "tag": "公告 888",
                        "title": "普通公告 777",
                        "text": "补充说明 999,999 股，每股 155 港元，二十万股",
                        "date": "2026-09-22",
                    },
                ],
            }],
        }],
    }, scan_data={"report_date": "2026-09-23", "buyback_results": {"pm-r17-opt": opt_rec}})
    assert n2 == []
    md2 = _all_md(c2)
    for leaked in ("999,999", "155", "888", "777", "二十万", "09-22"):
        assert leaked not in md2, leaked
    # Model lead ignored completely when any NDR/buyback company exists
    assert "行权 999" not in md2
    # Code-generated digest and footer remain intact
    assert "H股根據股份期權計劃行使期權：12,000股" in md2
    assert "15.5港元" in md2
    assert "数据源：SEC EDGAR · 巨潮公告 · HKEXnews" in md2

    # ------------------------------------------------------------------
    # 3. Codex repro 3: Section I strict single-line numeric rows —
    #    cross-line numbers, split prices across 變動日期, or unparseable rows
    #    MUST reject the entire record as validated: False (no partial acceptance).
    # ------------------------------------------------------------------
    repro3_cases = [
        # (a) Full numeric columns moved to next line after event title (broken "15. 5" AND clean "15.5")
        ndr.SAMPLE_OPTION.replace(
            "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
            "1). 根據股份期權計劃行使期權\n12,000  1.2 %  HKD  15. 5",
        ),
        ndr.SAMPLE_OPTION.replace(
            "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
            "1). 根據股份期權計劃行使期權\n12,000  1.2 %  HKD  15.5",
        ),
        # (b) Price split across 變動日期 line: "HKD 15", "變動日期 ...", ".5"
        ndr.SAMPLE_OPTION.replace(
            "HKD  15.5\n變動日期 2026年9月22日",
            "HKD  15\n變動日期 2026年9月22日\n.5",
        ),
        # (c) Trailing fragment on 變動日期 line
        ndr.SAMPLE_OPTION.replace(
            "HKD  15.5\n變動日期 2026年9月22日",
            "HKD  15\n變動日期 2026年9月22日 .5",
        ),
        # (d) Price split before 變動日期 line
        ndr.SAMPLE_OPTION.replace(
            "HKD  15.5\n變動日期 2026年9月22日",
            "HKD  15\n.5\n變動日期 2026年9月22日",
        ),
        # (e) Balance numbers wrapped onto next line
        ndr.SAMPLE_OPTION.replace(
            "於下列日期開始時的結存(註1) 2026年9月1日 1,000,000 1,000,000",
            "於下列日期開始時的結存(註1) 2026年9月1日\n1,000,000 1,000,000",
        ),
        # (f) Single-space numeric line missing %
        ndr.SAMPLE_OPTION.replace(
            "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
            "1). 根據股份期權計劃行使期權 12,000 1.2 HKD 15.5",
        ),
        # (g) Buyback NDR with valid Section II but broken/cross-line Section I (matching code 300308!)
        ndr.SAMPLE_TREASURY.replace(
            "1). 購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927.2463",
            "1). 購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927\n變動日期 2026年9月23日\n.2463",
        ).replace("變動日期 2026年9月23日\n變動日期 2026年9月23日", "變動日期 2026年9月23日"),
    ]
    for idx, bad_text in enumerate(repro3_cases):
        with pytest.raises(ValueError, match="第一章節"):
            ndr.parse_ndr(bad_text)
        target_code = "300308" if idx == len(repro3_cases) - 1 else "09992"
        _raw_bad, bad_rec = bp.validate_for_scan(
            bad_text,
            ann_id=f"r17-bad-{idx}",
            url=f"https://www.hkexnews.hk/r17-bad-{idx}.pdf",
            code=target_code,
            date="2026-09-23",
            title="翌日披露報表",
        )
        assert bad_rec["validated"] is False, idx
        assert bad_rec.get("error"), idx
        assert "digest" not in bad_rec and "total_amount" not in bad_rec, idx

        # End-to-end render-card treats failed Section I as unverified fixed sentence + day_errors
        bad_card, bad_notes = fc.build_card_with_notes({
            "date": "2026-09-23",
            "lead": "模型不该出现的导语 999,999",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": target_code,
                    "name": "测试",
                    "market": "hk",
                    "items": [{
                        "id": f"r17-bad-{idx}",
                        "tag": "港股公告",
                        "title": "翌日披露報表",
                        "date": "2026-09-23",
                        "url": f"https://www.hkexnews.hk/r17-bad-{idx}.pdf",
                    }],
                }],
            }],
        }, scan_data={"buyback_results": {f"r17-bad-{idx}": bad_rec}})
        bad_md = _all_md(bad_card)
        assert "翌日披露報表（金额未能从原文核对）" in bad_md
        for leaked in ("15.5", "15港元", "12,000", "927", "794,631,549", "999,999"):
            assert leaked not in bad_md
        assert len(bad_notes) == 1

    # SKILL.md documents the Round 17 strict rule
    skill_md = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "这类公司的模型文字不会显示数字" in skill_md
    assert "页脚" in skill_md and "完全由" in skill_md


def test_r18_codex_r13_numeric_fields_cn_digits_and_event_completeness() -> None:
    """Round 18 (codex r13): strip all model numeric fields on NDR/buyback companies,
    complete _CN_DIGITS uppercase/variants, and enforce Section I/II event completeness.
    """
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r18")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    treasury_text = (fixtures / "ff305_hk_buyback_treasury.txt").read_text(encoding="utf-8")
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")

    def _all_md(card: dict) -> str:
        parts = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
            elif el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if sub.get("tag") == "markdown":
                        parts.append(sub.get("content", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 1. Codex repro 1: NDR/buyback-related company clears ALL model numeric
    #    fields (total_amount, total_amount_raw, shares, price, currency, amount_usd, ...)
    #    so lead/totals/sorting use ONLY code-generated validated data.
    # ------------------------------------------------------------------
    _opt_raw, opt_rec = bp.validate_for_scan(
        ndr.SAMPLE_OPTION,
        ann_id="r18-opt",
        url="https://www.hkexnews.hk/r18-opt.pdf",
        code="09992",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert opt_rec["validated"] is True and opt_rec["is_buyback"] is False

    stake_payload = {
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "total_amount": 999999999,
                "amount_usd": 88888888,
                "items": [
                    {
                        "id": "r18-opt",
                        "tag": "港股公告",
                        "title": "翌日披露報表",
                        "date": "2026-09-23",
                        "url": "https://www.hkexnews.hk/r18-opt.pdf",
                        "amount_usd": 55555555,
                        "total_amount": 77777777,
                        "currency": "HKD",
                    },
                    {
                        "tag": "增持",
                        "title": "股东增持公告",
                        "text": "股东增持股份",
                        "date": "2026-09-23",
                        "total_amount": 987654321,
                        "total_amount_raw": "987,654,321",
                        "shares": 1000000,
                        "price": 987.65,
                        "currency": "HKD",
                        "amount_usd": 126622348,
                    },
                ],
            }],
        }],
    }
    c_stake, n_stake = fc.build_card_with_notes(
        stake_payload,
        scan_data={"report_date": "2026-09-23", "buyback_results": {"r18-opt": opt_rec}},
    )
    assert n_stake == []
    md_stake = _all_md(c_stake)
    for leaked in ("9.88", "亿港元", "987", "126,622", "合计", "5,555", "7,777", "9,999", "8,888"):
        assert leaked not in md_stake, leaked
    assert "**泡泡玛特 增持**" in md_stake
    assert "H股根據股份期權計劃行使期權：12,000股" in md_stake

    # Inspect collect() directly: numeric fields cleared and sort amount ignores model/non-buyback amounts.
    kt, rt = fc.collect(
        stake_payload,
        scan_data={"report_date": "2026-09-23", "buyback_results": {"r18-opt": opt_rec}},
    )
    assert len(kt) == 1 and rt == []
    _mkt, eff_comp = kt[0]
    assert eff_comp["_buyback_related"] is True
    assert fc.company_amount_usd(eff_comp) == 0.0
    assert fc._company_sort_amount(eff_comp) == 0.0
    for forbidden in ("amount_usd", "total_amount", "total_amount_raw", "shares", "price", "currency"):
        assert forbidden not in eff_comp
        for it in eff_comp["items"]:
            assert forbidden not in it, (forbidden, it)

    # Non-NDR/non-buyback company with 增持 total_amount still generates lead normally.
    plain_stake_card = fc.build_card({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "00700",
                "name": "腾讯控股",
                "market": "hk",
                "items": [{
                    "tag": "增持",
                    "title": "股东增持公告",
                    "text": "股东增持股份",
                    "date": "2026-09-23",
                    "total_amount": 987654321,
                    "currency": "HKD",
                }],
            }],
        }],
    })
    assert "**腾讯控股 增持 9.88 亿港元**" in _all_md(plain_stake_card)

    # ------------------------------------------------------------------
    # 2. Codex repro 2: Complete Chinese numeral table (simplified/traditional uppercase & variants).
    # ------------------------------------------------------------------
    all_cn_numerals = "〇零一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億兩貳參叄貮陸"
    cn_card, _cn_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [
                    {
                        "id": "r18-opt",
                        "tag": "港股公告",
                        "title": "翌日披露報表",
                        "date": "2026-09-23",
                        "url": "https://www.hkexnews.hk/r18-opt.pdf",
                    },
                    {
                        "tag": "港股公告",
                        "title": "普通公告",
                        "text": f"补充说明：每股贰叁陆港元；参考参与；全表{all_cn_numerals}港元",
                        "date": "2026-09-22",
                    },
                ],
            }],
        }],
    }, scan_data={"report_date": "2026-09-23", "buyback_results": {"r18-opt": opt_rec}})
    cn_md = _all_md(cn_card)
    for ch in all_cn_numerals:
        assert ch not in cn_md, f"Chinese numeral {ch!r} leaked in card"
    assert "补充说明：每股港元" in cn_md
    assert "参考参与" in cn_md

    # ------------------------------------------------------------------
    # 3. Codex repro 3: Section I & Section II event completeness (never skip unparseable events).
    # ------------------------------------------------------------------
    # 3a. Section I option NDR with cross-line numbering ("1).\n根據股份期權計劃...")
    sec1_opt_split = ndr.SAMPLE_OPTION.replace(
        "1). 根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
        "1).\n根據股份期權計劃行使期權  12,000  1.2 %  HKD  15.5",
    )
    with pytest.raises(ValueError, match="第一章節"):
        ndr.parse_ndr(sec1_opt_split)
    _raw_opt_split, rec_opt_split = bp.validate_for_scan(
        sec1_opt_split,
        ann_id="r18-sec1-opt-split",
        url="https://www.hkexnews.hk/r18-sec1-opt-split.pdf",
        code="09992",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert rec_opt_split["validated"] is False
    assert "digest" not in rec_opt_split

    # 3b. Section I buyback NDR with cross-line numbering AND broken price ("927. 2463")
    sec1_bb_split = ndr.SAMPLE_TREASURY.replace(
        "1). 購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927.2463",
        "1).\n購回股份 (股份被持作庫存股份)  -856,980  0.077 %  856,980 RMB  927. 2463",
    )
    with pytest.raises(ValueError, match="第一章節"):
        ndr.parse_ndr(sec1_bb_split)
    _raw_bb_split, rec_bb_split = bp.validate_for_scan(
        sec1_bb_split,
        ann_id="r18-sec1-bb-split",
        url="https://www.hkexnews.hk/r18-sec1-bb-split.pdf",
        code="300308",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert rec_bb_split["validated"] is False
    assert "total_amount" not in rec_bb_split and "groups" not in rec_bb_split

    # 3c. Multi-event Section I: valid event 1 + cross-line event 2 still fails whole record
    sec1_two_events_bad = ndr.SAMPLE_CANCEL.replace(
        "2). 購回股份 (擬註銷但尚未註銷)  665,060  0.02 %  RMB  300.71",
        "2).\n購回股份 (擬註銷但尚未註銷)  665,060  0.02 %  RMB  300.71",
    )
    with pytest.raises(ValueError, match="第一章節"):
        ndr.parse_ndr(sec1_two_events_bad)
    _raw_two, rec_two = bp.validate_for_scan(
        sec1_two_events_bad,
        ann_id="r18-sec1-two",
        url="https://www.hkexnews.hk/r18-sec1-two.pdf",
        code="300750",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert rec_two["validated"] is False

    # 3d. Section II event completeness: cross-line numbering or broken price in any trade fails whole record
    sec2_cases = [
        # Single trade with numbering split across line
        ndr.SAMPLE_TREASURY.replace("1). 2026年9月23日", "1).\n2026年9月23日"),
        # Single trade with broken price (and no fallback to non-buyback NDR)
        ndr.SAMPLE_TREASURY.replace("RMB 921.85", "RMB 921. 85"),
        # Two trades: trade 1 valid, trade 2 has cross-line numbering or broken price
        treasury_text.replace("2). 2026年11月16日", "2).\n2026年11月16日"),
        treasury_text.replace("HKD 119.50", "HKD 119. 50"),
    ]
    for idx, bad_sec2 in enumerate(sec2_cases):
        parsed_sec2 = bp.parse(bad_sec2)
        assert "error" in parsed_sec2 and "discrepancy" in parsed_sec2, idx
        assert "total_amount" not in parsed_sec2 and "total_shares" not in parsed_sec2, idx
        target_code = "300308" if idx < 2 else "09992"
        _raw_s2, rec_s2 = bp.validate_for_scan(
            bad_sec2,
            ann_id=f"r18-sec2-{idx}",
            url=f"https://www.hkexnews.hk/r18-sec2-{idx}.pdf",
            code=target_code,
            date="2026-11-16",
            title="翌日披露報表",
        )
        assert rec_s2["validated"] is False and rec_s2["is_buyback"] is True, idx
        assert "total_amount" not in rec_s2 and "digest" not in rec_s2, idx

    # Valid fixtures (including Section II B numbered mandate items 1)...5).) still pass cleanly
    assert bp.parse(popmart_text)["total_shares"] == 340_000
    assert bp.parse(treasury_text)["total_shares"] == 130_000


def test_r19_codex_r14_summary_neg_cn_numerals_and_section_split() -> None:
    """Round 19 (codex r14): summary buyback detection matches items (no negation bypass),
    complete counting numerals (廿卅卌皕 etc.), and fail whole record when chapter/subsection split fails.
    """
    fixtures = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "filing_alert"
    cli = _cli("daily_filing_buyback_r19")
    bp = cli._locals()["buyback_parse"]
    ndr = cli._locals()["cn_ndr"]
    fc = cli._locals()["filing_card"]
    popmart_text = (fixtures / "ff305_popmart_20261116.txt").read_text(encoding="utf-8")
    hk_cancel_text = (fixtures / "ff305_hk_buyback_cancel.txt").read_text(encoding="utf-8")

    def _all_md(card: dict) -> str:
        parts = []
        for el in card["body"]["elements"]:
            if el.get("tag") == "markdown":
                parts.append(el.get("content", ""))
            elif el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if sub.get("tag") == "markdown":
                        parts.append(sub.get("content", ""))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # 1. Codex repro 1: Summary negation wording ("未回购/无回购") triggers
    #    _buyback_related=True and full model-digit stripping (consistent with items).
    # ------------------------------------------------------------------
    for neg_summary in (
        "未回购999999股，每股155港元",
        "无回购五亿港元，每股廿港元",
        "不购回 888,888 股",
    ):
        payload_neg = {
            "date": "2026-09-23",
            "lead": "09992 未回购 999,999 股",
            "footer": "模型页脚 999,999 港元",
            "sections": [{
                "key": "cnhk",
                "title": "A股 / 港股",
                "companies": [{
                    "code": "09992",
                    "name": "泡泡玛特 999",
                    "market": "hk",
                    "summary": neg_summary,
                    "items": [{
                        "tag": "港股公告 777",
                        "title": "普通公告 666",
                        "text": "披露一般事项 555",
                        "date": "2026-09-23",
                    }],
                }],
            }],
        }
        kt, rt = fc.collect(payload_neg)
        assert len(kt) + len(rt) == 1
        _mkt, eff = (kt + rt)[0]
        assert eff["_buyback_related"] is True, neg_summary
        for leaked in ("999999", "999,999", "155", "五亿", "廿", "888,888", "777", "666", "555"):
            assert leaked not in eff["summary"], (neg_summary, eff)
            assert leaked not in json.dumps(eff["items"], ensure_ascii=False, default=str), (neg_summary, eff)
        card_neg, _notes_neg = fc.build_card_with_notes(payload_neg)
        md_neg = _all_md(card_neg)
        for leaked in ("999999", "999,999", "155", "五亿", "廿", "888,888", "777", "666", "555", "模型页脚"):
            assert leaked not in md_neg, (neg_summary, leaked)

    # ------------------------------------------------------------------
    # 2. Codex repro 2: Complete Chinese counting numerals (廿 卅 卌 皕 etc.)
    #    stripped from model text and regenerated summary on NDR/buyback companies.
    # ------------------------------------------------------------------
    _opt_raw, opt_rec = bp.validate_for_scan(
        ndr.SAMPLE_OPTION,
        ann_id="r19-opt",
        url="https://www.hkexnews.hk/r19-opt.pdf",
        code="09992",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert opt_rec["validated"] is True and opt_rec["is_buyback"] is False

    extra_cn_numerals = "廿卅卌皕卄丗弌弍弎叅〡〢〣〤〥〦〧〨〩〸〹〺㈠㈡㈢㈣㈤㈥㈦㈧㈨㈩㊀㊁㊂㊃㊄㊅㊆㊇㊈㊉"
    cn_card, cn_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "summary": "每股廿港元，另卅股，卌万，皕亿",
                "items": [
                    {
                        "id": "r19-opt",
                        "tag": "港股公告",
                        "title": "翌日披露報表",
                        "date": "2026-09-23",
                        "url": "https://www.hkexnews.hk/r19-opt.pdf",
                    },
                    {
                        "tag": "港股公告",
                        "title": "普通公告",
                        "text": f"每股廿港元，另卅股；参考参与；全扩{extra_cn_numerals}港元",
                        "date": "2026-09-22",
                    },
                ],
            }],
        }],
    }, scan_data={"report_date": "2026-09-23", "buyback_results": {"r19-opt": opt_rec}})
    assert cn_notes == []
    cn_md = _all_md(cn_card)
    for ch in extra_cn_numerals:
        assert ch not in cn_md, f"Chinese counting numeral {ch!r} leaked"
    assert "每股港元，另股" in cn_md
    assert "参考参与" in cn_md

    # ------------------------------------------------------------------
    # 3. Codex repro 3: Chapter I & II section/subsection split failure or
    #    empty captured subsection while numbered events exist -> validated: False.
    # ------------------------------------------------------------------
    # 3a. Exact codex repro: "A.\n已發行股份…" + "15. 5"
    codex_sec1_repro = ndr.SAMPLE_OPTION.replace("A. 已發行股份", "A.\n已發行股份").replace("15.5", "15. 5")
    with pytest.raises(ValueError, match="第一章節"):
        ndr.parse_ndr(codex_sec1_repro)
    _r_codex, rec_codex = bp.validate_for_scan(
        codex_sec1_repro,
        ann_id="r19-codex-repro",
        url="https://www.hkexnews.hk/r19-codex-repro.pdf",
        code="09992",
        date="2026-09-23",
        title="翌日披露報表",
    )
    assert rec_codex["validated"] is False
    assert rec_codex.get("error")
    assert "digest" not in rec_codex

    codex_card, codex_notes = fc.build_card_with_notes({
        "date": "2026-09-23",
        "sections": [{
            "key": "cnhk",
            "title": "A股 / 港股",
            "companies": [{
                "code": "09992",
                "name": "泡泡玛特",
                "market": "hk",
                "items": [{
                    "id": "r19-codex-repro",
                    "tag": "港股公告",
                    "title": "翌日披露報表",
                    "date": "2026-09-23",
                    "url": "https://www.hkexnews.hk/r19-codex-repro.pdf",
                }],
            }],
        }],
    }, scan_data={"buyback_results": {"r19-codex-repro": rec_codex}})
    codex_md = _all_md(codex_card)
    assert "翌日披露报表未列出股份变动或回购" not in codex_md
    assert "翌日披露報表（金额未能从原文核对）" in codex_md
    assert len(codex_notes) == 1

    # 3b. Additional Section I split-failure & uncaptured numbered-event cases
    sec1_split_fail_cases = [
        # Split A header even with clean numbers
        ("09992", ndr.SAMPLE_OPTION.replace("A. 已發行股份", "A.\n已發行股份")),
        # Renamed/unknown A header with numbered event
        ("09992", ndr.SAMPLE_OPTION.replace("A. 已發行股份或庫存股份變動", "A. 股份變動")),
        # Missing A header completely, numbered event still in block
        ("09992", ndr.SAMPLE_OPTION.replace("A. 已發行股份或庫存股份變動\n", "")),
        # Unmatched class header "1. 類別" leaving _class_blocks empty while events exist
        ("09992", ndr.SAMPLE_OPTION.replace("1. 股份分類", "1. 類別")),
        # Empty Chapter I body immediately followed by Chapter I notes
        ("09992", ndr.SAMPLE_OPTION.replace("1. 股份分類", "第一章節註釋：\n1. 股份分類")),
        # Broken B header "B.\n贖回" in SAMPLE_CANCEL
        ("300750", ndr.SAMPLE_CANCEL.replace("B. 贖回/購回股份", "B.\n贖回/購回股份")),
        # Valid Section II but broken Section I A header in SAMPLE_TREASURY
        ("300308", ndr.SAMPLE_TREASURY.replace("A. 已發行股份", "A.\n已發行股份")),
    ]
    for idx, (target_code, bad_txt) in enumerate(sec1_split_fail_cases):
        with pytest.raises(ValueError, match="第一章節"):
            ndr.parse_ndr(bad_txt)
        _raw_i, rec_i = bp.validate_for_scan(
            bad_txt,
            ann_id=f"r19-s1-{idx}",
            url=f"https://www.hkexnews.hk/r19-s1-{idx}.pdf",
            code=target_code,
            date="2026-09-23",
            title="翌日披露報表",
        )
        assert rec_i["validated"] is False, idx
        assert "digest" not in rec_i and "total_amount" not in rec_i, idx

    # 3c. Section II split-failure & uncaptured numbered-event cases
    sec2_split_fail_cases = [
        # Valid Ch1 + Ch2 with split A header ("A. 購回\n報告") and broken trade
        (
            "09992",
            ndr.SAMPLE_OPTION
            + "\n第二章節\nA. 購回\n報告\n1). 2026年9月23日 10,000 於本交易所進行 HKD 10.00\n",
        ),
        # Valid Ch1 + Ch2 where 第二章節註釋 precedes A. 購回報告 (sec2 split becomes empty)
        (
            "09992",
            ndr.SAMPLE_OPTION
            + "\n第二章節\n第二章節註釋：\nA. 購回報告\n1). 2026年9月23日 10,000 於本交易所進行 HKD 10.00 HKD 9.50 HKD 98,000\n",
        ),
        # Valid Ch1 + Ch2 with numbered event directly under 第二章節 (no A. header)
        (
            "09992",
            ndr.SAMPLE_OPTION
            + "\n第二章節\n1). 2026年9月23日 10,000 坏行\n",
        ),
        # Multi-class Ch2 where first class header is broken ("1.\n股份分類") before "2. 股份分類"
        (
            "300308",
            """翌日披露報表
第二章節
1.
股份分類 普通股 股份類別 A 於香港聯交所上市 否
證券代號 (如上市) 300308 說明 A股
A. 購回報告
1). 2026年11月15日 10,000 坏行
2. 股份分類 普通股 股份類別 H 於香港聯交所上市 是
證券代號 (如上市) 00700 說明 H股
A. 購回報告
1). 2026年11月16日 40,000 於本交易所進行 HKD 100.00 HKD 98.00 HKD 3,960,000.00
合共購回股份總數 40,000
合共付出的價格總額 (元) HKD 3,960,000.00
""",
        ),
    ]
    for idx, (target_code, bad_s2) in enumerate(sec2_split_fail_cases):
        _raw_s2, rec_s2 = bp.validate_for_scan(
            bad_s2,
            ann_id=f"r19-s2-{idx}",
            url=f"https://www.hkexnews.hk/r19-s2-{idx}.pdf",
            code=target_code,
            date="2026-09-23",
            title="翌日披露報表",
        )
        assert rec_s2["validated"] is False, idx
        assert "total_amount" not in rec_s2 and "digest" not in rec_s2, idx

    # Valid fixtures and explicit "第二章節\nA. 購回報告 不適用" continue to pass cleanly.
    _raw_pm, rec_pm = bp.validate_for_scan(popmart_text, code="09992", title="翌日披露報表")
    assert rec_pm["validated"] is True and rec_pm["is_buyback"] is True
    _raw_hkc, rec_hkc = bp.validate_for_scan(hk_cancel_text, code="00700", title="翌日披露報表")
    assert rec_hkc["validated"] is True and rec_hkc["is_buyback"] is True
    _raw_na, rec_na = bp.validate_for_scan(
        ndr.SAMPLE_OPTION + "\n第二章節\nA. 購回報告 不適用\n",
        code="09992",
        title="翌日披露報表",
    )
    assert rec_na["validated"] is True and rec_na["is_buyback"] is False











