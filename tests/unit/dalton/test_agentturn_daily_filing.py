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
        assert card == original.build_card(SAMPLE)


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
        "300308", "300502", "300750", "09992", "00700",
    ]
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
    replacements = {
        "<workspace>": str(tmp_path),
        "{LOOKBACK_DAYS}": "1",
        "<report_date>": "2026-11-12",
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
                "start": "2026-11-11",
                "end": "2026-11-12",
                "limit": 50,
                "calls": [
                    {"ticker": "300308", "ok": True, "rows": [{
                        "date": "2026-11-12",
                        "title": "关于回购股份",
                        "category": "回购",
                        "pdf_link": "https://static.cninfo.com.cn/finalpage/2026-11-12/a.pdf",
                    }]},
                    {"ticker": "00700", "ok": True, "data": [{
                        "date": "2026-11-12",
                        "title": "翌日披露",
                        "pdf_link": "https://www.hkexnews.hk/1.pdf",
                    }]},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            last_check = tmp_path / "state" / "daily-filing-alert" / "last_check.json"
            before_last_check = last_check.read_bytes()
        if argv[1] == "render-card":
            payload_path = tmp_path / "temp" / "daily-filing-alert" / "2026-11-12-payload.json"
            payload_path.parent.mkdir(parents=True, exist_ok=True)
            payload_path.write_text(
                json.dumps({
                    "date": "2026-11-12",
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
    sec_path = tmp_path / "temp" / "daily-filing-alert" / "2026-11-12-sec.json"
    sec = json.loads(sec_path.read_text(encoding="utf-8"))
    symbols = [
        line.strip()
        for line in (SKILL_ROOT / "watchlist.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert sec["tickers_scanned"] == len(symbols) == 34
    assert sec["tickers_with_alerts"] == 0
    assert all(result["errors"] for result in sec["results"])
    state = tmp_path / "state" / "daily-filing-alert"
    for name in ("last_check.json", "filings.json", "cik_map.json"):
        _no_leak((state / name).read_text(encoding="utf-8"))
    cn = json.loads((tmp_path / "temp" / "daily-filing-alert" / "2026-11-12-cn-hk.json").read_text(encoding="utf-8"))
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
    card_text = (tmp_path / "temp" / "daily-filing-alert" / "2026-11-12-card.json").read_text(encoding="utf-8")
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
    assert payload["hk_stocks_scanned"] == 2
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
                    "notice_text": notice.SAMPLE_TREASURY,
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
    for needle in ("7.95亿元人民币", "持作库存股", "921.85至935", "0.077%"):
        assert needle in ndr_row["digest"]
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
