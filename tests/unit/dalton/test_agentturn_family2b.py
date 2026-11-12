"""OC-1 family 2b: China and global consumer weeklies submit plain text."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import create_autospec

import pandas as pd
import pytest
import requests
import yaml
import yfinance as yf

from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "plugins" / "dalton" / "skills"
PROFILES = ("china-consumer-weekly", "global-consumer-weekly")
SKILL_NAMES = (
    "dalton-china-consumer-weekly",
    "dalton-global-consumer-weekly",
)
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
CHINA_CARD = """# 中国消费周报

## 本周结论
本周无结构性变化

## 细分指数
消费池本周中位数持平

## 技术面
本周无技术面信号

## 变化归因
本周无达到门槛的归因

## 观察项
本周无观察项

## 待验证
下周看经营数据
"""
GLOBAL_CARD = """# 全球消费周报

## 本周结论
本周无跨市场级别的变化

## 跨市场信号
两地消费股本周读数持平

## 技术面
本周无技术面信号

## 变化归因
本周无达到门槛的归因

## 专家与纪要
本周无相关新增访谈

## 上游成本与汇率
本周大宗成本无显著变动

## 观察项
本期无降级事项

## 待验证
下周看经营数据
"""
CONFIRM = ["--content-kind", "text", "--delivery-confirmed", "--status", "sent"]


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
    text = buffer.getvalue()
    return code, json.loads(text) if text.strip() else {}


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


def _skill_argv(skill: str, subcommand: str, replacements: dict[str, str]) -> list[str]:
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    for command in _bash_commands(text):
        if subcommand not in command:
            continue
        script_at = next(index for index, token in enumerate(command) if token.endswith(".py"))
        argv: list[str] = []
        for token in command[script_at + 1 :]:
            token = token.strip("'\"")
            for old, new in replacements.items():
                token = token.replace(old, new)
            argv.append(token)
        return argv
    raise AssertionError(f"{skill} has no {subcommand} command")


def _china():
    return _load(
        "china_consumer_weekly_family2b",
        SKILLS / "dalton-china-consumer-weekly" / "scripts" / "china_consumer_weekly.py",
    )


def _global():
    return _load(
        "global_consumer_weekly_family2b",
        SKILLS / "dalton-global-consumer-weekly" / "scripts" / "global_consumer_weekly.py",
    )


def _bars(start: str, count: int, price: float) -> list[dict]:
    day = date.fromisoformat(start)
    rows = []
    for offset in range(count):
        stamp = (day + timedelta(days=offset)).isoformat()
        close = price + offset * 0.01
        rows.append({"d": stamp, "o": close, "c": close, "h": close, "l": close, "v": 100.0})
    return rows


def _seed_china_benchmarks(workspace: Path) -> None:
    folder = workspace / "state" / "china-consumer-db" / "prices"
    folder.mkdir(parents=True)
    rows = _bars("2025-10-01", 400, 3000.0)
    for symbol, name, market in (("sh000300", "沪深300", "A"), ("hkHSI", "恒生指数", "HK")):
        payload = {"symbol": symbol, "name": name, "market": market, "rows": rows,
                   "last_date": rows[-1]["d"], "fetched_at": "2026-11-13"}
        (folder / f"{symbol}.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_global_benchmarks(workspace: Path) -> None:
    folder = workspace / "state" / "global-consumer" / "prices"
    folder.mkdir(parents=True)
    rows = [{"d": row["d"], "c": row["c"], "v": row["v"]} for row in _bars("2025-10-01", 400, 5000.0)]
    for symbol in ("^GSPC", "^N225"):
        (folder / f"{symbol}.json").write_text(json.dumps(rows), encoding="utf-8")


def test_skill_frontmatter_and_registry() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        DALTON_SKILL_DEFINITIONS,
    )

    for name in SKILL_NAMES:
        text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.find("\n---\n", 4)
        meta = yaml.safe_load(text[4:end])
        assert set(meta) == {"name", "description"}
        assert meta["name"] == name
        assert "触发词" not in meta["description"]
        assert len(meta["description"]) <= 120
        assert (name, None, ()) in _DALTON_SKILL_SPECS
        assert DALTON_SKILL_DEFINITIONS[name].requires == ()
        assert DALTON_SKILL_DEFINITIONS[name].command is None
        assert DALTON_SKILL_DEFINITIONS[name].description == meta["description"].strip()
    assert set(PROFILES) <= set(REPORT_PROFILES)


def test_skill_trees_omit_delivery_targets() -> None:
    for name in SKILL_NAMES:
        root = SKILLS / name
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            assert _RECIPIENT_OC_RE.search(text) is None, path
            for token in BANNED:
                assert token not in text, f"{path} contains {token}"


def test_documented_commands_run_offline(tmp_path: Path) -> None:
    friday = "2026-11-13"
    china = _china()
    china_ws = tmp_path / "china"
    china_ws.mkdir()
    replacements = {"<workspace>": str(china_ws), "<as-of>": "2026-11-15", "<friday>": friday}
    self_argv = _skill_argv("dalton-china-consumer-weekly", "self-test", replacements)
    assert self_argv[:4] == ["--json", "self-test", "--workspace-dir", str(china_ws)]
    assert "--network" in self_argv
    code, payload, err = _run_captured(
        china, [arg for arg in self_argv if arg != "--network"]
    )
    assert code == 0 and payload["ok"] is True, f"stderr={err!r} payload={payload!r}"

    anchor_argv = _skill_argv("dalton-china-consumer-weekly", "anchor", replacements)
    assert anchor_argv[:4] == ["--json", "anchor", "--workspace-dir", str(china_ws)]
    code, payload = _run(china, anchor_argv)
    assert code == 0 and payload["friday"] == friday
    assert payload["report_run_id"] == f"china-consumer-weekly-{friday}"
    code, thursday = _run(china, ["--json", "anchor", "--workspace-dir", str(china_ws),
                                  "--as-of", "2026-11-12"])
    assert code == 0 and thursday["friday"] == "2026-11-06"
    assert thursday["report_run_id"] == "china-consumer-weekly-2026-11-06"

    card = china_ws / "temp" / "china-consumer-weekly" / f"{friday}-card.md"
    card.parent.mkdir(parents=True)
    card.write_text(CHINA_CARD, encoding="utf-8")
    (card.parent / f"{friday}.md").write_text(CHINA_CARD, encoding="utf-8")
    validate_argv = _skill_argv("dalton-china-consumer-weekly", "validate", replacements)
    assert validate_argv[:4] == ["--json", "validate", "--workspace-dir", str(china_ws)]
    code, payload = _run(china, validate_argv)
    assert code == 0 and payload["ok"] is True, payload

    code, recorded = _run(china, ["--json", "record-collection", "--workspace-dir", str(china_ws),
                                  "--as-of", friday, "--note", "采集完成"])
    assert code == 0 and recorded["ok"] is True
    assert (china_ws / "state" / "china-consumer-db" / "collection-runs" / f"{friday}.json").is_file()
    assert not (china_ws / "state" / "china-consumer-db" / "snapshots").exists()

    global_cli = _global()
    global_ws = tmp_path / "global"
    global_ws.mkdir()
    greplace = {"<workspace>": str(global_ws), "<as-of>": "2026-11-15", "<friday>": friday}
    gself = _skill_argv("dalton-global-consumer-weekly", "self-test", greplace)
    assert gself[:4] == ["--json", "self-test", "--workspace-dir", str(global_ws)]
    assert "--network" in gself
    code, payload, err = _run_captured(
        global_cli, [arg for arg in gself if arg != "--network"]
    )
    assert code == 0 and payload["ok"] is True, f"stderr={err!r} payload={payload!r}"
    ganchor = _skill_argv("dalton-global-consumer-weekly", "anchor", greplace)
    code, payload = _run(global_cli, ganchor)
    assert payload["friday"] == friday
    assert payload["report_run_id"] == f"global-consumer-weekly-{friday}"

    gcard = global_ws / "temp" / "global-consumer-weekly" / f"{friday}-card.md"
    gcard.parent.mkdir(parents=True)
    gcard.write_text(GLOBAL_CARD, encoding="utf-8")
    (gcard.parent / f"{friday}.md").write_text(GLOBAL_CARD + "\n## 细分指数\n两地读数见全文\n", encoding="utf-8")
    gvalidate = _skill_argv("dalton-global-consumer-weekly", "validate", greplace)
    assert gvalidate[:4] == ["--json", "validate", "--workspace-dir", str(global_ws)]
    code, payload = _run(global_cli, gvalidate)
    assert code == 0 and payload["ok"] is True, payload


def test_archive_and_snapshot_wait_for_delivery(tmp_path: Path) -> None:
    friday = "2026-11-13"
    china = _china()
    workspace = tmp_path / "china"
    workspace.mkdir()
    source = workspace / "temp" / "china-consumer-weekly" / f"{friday}.md"
    source.parent.mkdir(parents=True)
    source.write_text(CHINA_CARD, encoding="utf-8")
    code, refused = _run(china, ["--json", "archive", "--workspace-dir", str(workspace),
                                 "--file", f"temp/china-consumer-weekly/{friday}.md", "--as-of", friday])
    assert code == 1 and refused["error"] == "未确认发送" and refused["written"] is None
    code, refused = _run(china, ["--json", "snapshot", "--workspace-dir", str(workspace),
                                 "--as-of", friday, "--write"])
    assert code == 1 and refused["error"] == "未确认发送"
    assert not (workspace / "research-outputs").exists()
    assert not (workspace / "state" / "china-consumer-db" / "snapshots").exists()

    _seed_china_benchmarks(workspace)
    replacements = {"<workspace>": str(workspace), "<friday>": friday, "<as-of>": friday}
    archive_argv = _skill_argv("dalton-china-consumer-weekly", "archive", replacements)
    code, archived = _run(china, archive_argv)
    assert code == 0 and archived["written"] == f"research-outputs/china-consumer-weekly/{friday}.md"
    assert not (workspace / "state" / "china-consumer-db" / "snapshots" / f"{friday}.json").exists()
    snap_argv = _skill_argv("dalton-china-consumer-weekly", "snapshot", replacements)
    code, snap = _run(china, snap_argv)
    assert code == 0 and snap["written"] == f"state/china-consumer-db/snapshots/{friday}.json"
    assert (workspace / snap["written"]).is_file()

    global_cli = _global()
    gws = tmp_path / "global"
    gws.mkdir()
    gsource = gws / "temp" / "global-consumer-weekly" / f"{friday}.md"
    gsource.parent.mkdir(parents=True)
    gsource.write_text(GLOBAL_CARD + "\n## 细分指数\n两地读数见全文\n", encoding="utf-8")
    code, refused = _run(global_cli, ["--json", "archive", "--workspace-dir", str(gws),
                                      "--file", f"temp/global-consumer-weekly/{friday}.md",
                                      "--as-of", friday])
    assert code == 2 and refused["error"] == "未确认发送" and refused["written"] is None
    code, refused = _run(global_cli, ["--json", "snapshot", "--workspace-dir", str(gws),
                                      "--write", "--as-of", friday])
    assert code == 2 and refused["error"] == "未确认发送"
    _seed_global_benchmarks(gws)
    greplace = {"<workspace>": str(gws), "<friday>": friday, "<as-of>": friday}
    code, archived = _run(global_cli, _skill_argv("dalton-global-consumer-weekly", "archive", greplace))
    assert code == 0 and archived["written"].endswith(f"{friday}.md")
    assert not (gws / "state" / "global-consumer" / "snapshots").exists()
    code, snap = _run(global_cli, _skill_argv("dalton-global-consumer-weekly", "snapshot", greplace))
    assert code == 0 and snap["path"] == f"state/global-consumer/snapshots/{friday}.json"
    assert (gws / snap["path"]).is_file()


def test_paths_do_not_escape_or_follow_symlinks(tmp_path: Path) -> None:
    china = _china()
    workspace = tmp_path / "china"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    note = ["--json", "add-note", "--workspace-dir", str(workspace),
            "--subsector", "baijiu", "--source-type", "company_disclosure",
            "--evidence-grade", "A", "--claim", "批价持平", "--week-of", "2026-11-13"]
    os.symlink(outside, workspace / "state")
    code, payload = _run(china, note)
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(outside.iterdir()) == []
    (workspace / "state").unlink()

    folder = workspace / "state" / "china-consumer-db"
    folder.mkdir(parents=True)
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("sentinel", encoding="utf-8")
    os.symlink(sentinel, folder / "notes.jsonl")
    code, payload = _run(china, note)
    assert code == 1 and "refusing symlink" in payload["error"]
    assert sentinel.read_text(encoding="utf-8") == "sentinel"

    code, escaped = _run(china, ["--json", "archive", "--workspace-dir", str(workspace),
                                 "--file", "../secret.md", "--as-of", "2026-11-13", *CONFIRM])
    assert code == 1 and "path must stay inside the workspace" in escaped["error"]

    global_cli = _global()
    gws = tmp_path / "global"
    gout = tmp_path / "global-outside"
    gws.mkdir()
    gout.mkdir()
    os.symlink(gout, gws / "state")
    gnote = ["--json", "add-note", "--workspace-dir", str(gws), "--subsector", "sportswear",
             "--tier", "A", "--type", "company_disclosure", "--claim", "同店持平",
             "--source", "10-Q", "--source-date", "2026-11-13", "--as-of", "2026-11-13"]
    code, payload = _run(global_cli, gnote)
    assert code == 2 and "path must stay inside the workspace" in payload["error"]
    assert list(gout.iterdir()) == []
    (gws / "state").unlink()
    gfolder = gws / "state" / "global-consumer"
    gfolder.mkdir(parents=True)
    os.symlink(sentinel, gfolder / "notes.jsonl")
    code, payload = _run(global_cli, gnote)
    assert code == 2 and "refusing symlink" in payload["error"]
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    os.symlink(sentinel, gfolder / "snapshots")
    _seed_prices = gfolder / "prices"
    _seed_prices.mkdir()
    rows = [{"d": row["d"], "c": row["c"], "v": row["v"]} for row in _bars("2025-10-01", 40, 100.0)]
    for symbol in ("^GSPC", "^N225"):
        (_seed_prices / f"{symbol}.json").write_text(json.dumps(rows), encoding="utf-8")
    code, payload = _run(global_cli, ["--json", "snapshot", "--workspace-dir", str(gws),
                                      "--write", "--as-of", "2026-11-13", *CONFIRM])
    assert code == 2 and "path must stay inside the workspace" in payload["error"]
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_china_quotes_use_requests_autospec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _china()
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    session = create_autospec(requests.Session, instance=True)

    class _Response:
        def __init__(self):
            self.text = ""
            self.encoding = None

        def raise_for_status(self):
            return None

        def json(self):
            return self._json

    def fake_get(url, timeout=25, headers=None):
        response = _Response()
        if "qt.gtimg.cn" in url:
            fields = ["0"] * 50
            fields[1] = "测试"
            fields[3] = "10"
            fields[45] = "100"
            response.text = 'v="' + "~".join(fields) + '"'
            return response
        if "sina.com.cn" in url:
            rows = ",".join(
                f'{{"d":"{(date(2026, 1, 1) + timedelta(days=i)).isoformat()}","c":"{100 + i}"}}'
                for i in range(30)
            )
            response.text = "var _x=([" + rows + "])"
            return response
        symbol = url.split("param=")[1].split(",")[0]
        bars = []
        for offset in range(70):
            stamp = (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
            bars.append([stamp, "1", "2", "3", "1", "10"])
        response._json = {"data": {symbol: {"qfqday": bars}}}
        response.text = ""
        return response

    session.get.side_effect = fake_get
    monkeypatch.setattr(cli, "_session", lambda: session)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, payload = _run(cli, ["--json", "sync-prices", "--workspace-dir", str(workspace), "--force"])
    assert code == 0 and payload["ok"] is True, payload
    assert payload["fetched"] > 200
    assert session.get.call_count > 200
    sample = next((workspace / "state" / "china-consumer-db" / "prices").glob("sh*.json"))
    saved = json.loads(sample.read_text(encoding="utf-8"))
    assert len(saved["rows"]) >= 60
    code, commodities = _run(cli, ["--json", "commodities", "--workspace-dir", str(workspace), "--refresh"])
    assert code == 0 and commodities["ok"] is True, commodities
    assert (workspace / "state" / "china-consumer-db" / "commodities" / "TA0.json").is_file()


def test_global_market_data_uses_yfinance_autospec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _global()
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    index = pd.bdate_range("2026-09-01", periods=40)
    frame = pd.DataFrame({"Close": [100.0 + i for i in range(len(index))],
                          "Volume": [1000.0] * len(index)}, index=index)
    earnings = pd.DataFrame({"Reported EPS": [1.2]}, index=pd.to_datetime(["2026-11-10"]))
    created = []

    def ticker_factory(symbol):
        ticker = create_autospec(yf.Ticker, instance=True)
        ticker.history.return_value = frame
        ticker.get_earnings_dates.return_value = earnings
        created.append((symbol, ticker))
        return ticker

    monkeypatch.setattr(cli, "_yf", lambda: type("YF", (), {"Ticker": staticmethod(ticker_factory)})())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    code, payload = _run(cli, ["--json", "sync-prices", "--workspace-dir", str(workspace),
                               "--force", "--max-age-hours", "12"])
    assert code == 0 and payload["ok"] is True, payload
    assert payload["fetched"] > 200
    assert created[0][1].history.call_args.kwargs["auto_adjust"] is False
    saved = json.loads((workspace / "state" / "global-consumer" / "prices" / "^GSPC.json").read_text())
    assert isinstance(saved, list) and saved[0]["c"] > 0

    code, earned = _run(cli, ["--json", "earnings", "--workspace-dir", str(workspace),
                              "--as-of", "2026-11-13", "--tickers", "SBUX", "--lookback", "10", "--days", "21"])
    assert code == 0 and earned["fetched"] == 1, earned
    assert (workspace / "state" / "global-consumer" / "earnings" / "SBUX.json").is_file()

    html = (
        "<html>全1件<table><tr><td>09:00</td><td>99830</td><td>迅销</td>"
        '<td>月次売上高</td><td><a href="a.pdf">pdf</a></td></tr></table>'
    )

    class _Body:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return html.encode()

    opener = create_autospec(urllib.request.urlopen, return_value=_Body())
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    code, disclosures = _run(cli, ["--json", "jp-disclosures", "--workspace-dir", str(workspace),
                                   "--as-of", "2026-11-13", "--days", "1"])
    assert code == 0 and disclosures["ok"] is True, disclosures
    assert disclosures["as_of"] == "2026-11-13"
    assert disclosures["friday"] == "2026-11-13"
    assert disclosures["material_hits"] == 1
    assert disclosures["items"][0]["ticker"] == "9983.T"
    assert disclosures["items"][0]["kind"] == "monthly_sales"
    assert opener.call_count == 1
    assert "20261113" in opener.call_args.args[0].get_full_url()


def test_perf_reports_the_actual_price_date(tmp_path: Path) -> None:
    friday = "2026-11-13"
    china = _china()
    workspace = tmp_path / "china"
    workspace.mkdir()
    _seed_china_benchmarks(workspace)
    code, perf = _run(
        china,
        ["--json", "perf", "--workspace-dir", str(workspace), "--as-of", friday],
    )
    assert code == 0, perf
    assert perf["requested_as_of"] == friday
    assert perf["price_as_of"] == "2026-11-04"
    assert perf["as_of"] == perf["price_as_of"]
    assert perf["holiday_note"]
    assert perf["missing_count"] > 0

    code, plan = _run(
        china,
        ["--json", "plan", "--workspace-dir", str(workspace), "--as-of", friday],
    )
    assert code == 0, plan
    assert "window" not in plan
    assert plan["price_window"]["friday"] == friday
    assert plan["price_window"]["end"] == "2026-11-04"
    assert plan["source_window"]["start"] == "2026-11-08"
    assert plan["source_window"]["end_at"].endswith("+08:00")

    global_cli = _global()
    gws = tmp_path / "global"
    gws.mkdir()
    _seed_global_benchmarks(gws)
    code, gperf = _run(
        global_cli,
        ["--json", "perf", "--workspace-dir", str(gws), "--as-of", friday],
    )
    assert code == 0, gperf
    assert gperf["requested_as_of"] == friday
    assert gperf["as_of_anchor"] == friday
    assert gperf["price_as_of"] == {"US": "2026-11-04", "JP": "2026-11-04"}
    assert gperf["holiday_note"]
    assert gperf["missing_count"] > 0
    code, gplan = _run(
        global_cli,
        ["--json", "plan", "--workspace-dir", str(gws), "--as-of", friday, "--focus", "5"],
    )
    assert code == 0, gplan
    assert gplan["source_window"]["start"] == "2026-11-08"
    assert gplan["price_window"]["end"] == gperf["price_as_of"]
    assert gplan["price_window"]["friday"] == friday


def test_draft_reads_the_full_source_window(tmp_path: Path) -> None:
    friday = "2026-11-13"
    china = _china()
    workspace = tmp_path / "china"
    workspace.mkdir()

    def _add(source_date: str, week_of: str, claim: str) -> None:
        code, payload = _run(
            china,
            [
                "--json", "add-note", "--workspace-dir", str(workspace),
                "--subsector", "baijiu", "--source-type", "company_disclosure",
                "--evidence-grade", "A", "--claim", claim,
                "--source-date", source_date, "--week-of", week_of,
            ],
        )
        assert code == 0, payload

    _add("2026-11-11", friday, "周三信源")
    _add("2026-11-11", "2026-11-11", "周三采集")
    _add("2026-11-01", "2026-11-01", "窗口外")
    code, window = _run(
        china,
        ["--json", "notes", "--workspace-dir", str(workspace), "--since", "2026-11-08"],
    )
    assert code == 0, window
    assert {row["claim"] for row in window["notes"]} == {"周三信源", "周三采集"}
    code, friday_notes = _run(
        china,
        ["--json", "notes", "--workspace-dir", str(workspace), "--since", friday],
    )
    assert {row["claim"] for row in friday_notes["notes"]} == {"周三信源"}

    global_cli = _global()
    gws = tmp_path / "global"
    gws.mkdir()
    code, added = _run(
        global_cli,
        [
            "--json", "add-note", "--workspace-dir", str(gws),
            "--subsector", "sportswear", "--tier", "A", "--type", "company_disclosure",
            "--claim", "同店持平", "--source", "10-Q",
            "--source-date", "2026-11-11", "--as-of", friday,
        ],
    )
    assert code == 0, added
    code, full = _run(
        global_cli,
        ["--json", "notes", "--workspace-dir", str(gws), "--week", friday, "--full"],
    )
    assert code == 0, full
    assert full["count"] == 1
    assert full["notes"][0]["claim"] == "同店持平"
    assert full["notes"][0]["source_date"] == "2026-11-11"
    code, hidden = _run(
        global_cli,
        ["--json", "notes", "--workspace-dir", str(gws), "--week", friday],
    )
    assert hidden["count"] == 1
    assert hidden["notes"] is None
    skill = (SKILLS / "dalton-global-consumer-weekly" / "SKILL.md").read_text(encoding="utf-8")
    assert "--week <friday> --full" in skill


def test_parent_directory_symlink_outside_workspace_is_refused(tmp_path: Path) -> None:
    china_io = _load(
        "china_card_io_parent_family2b",
        SKILLS / "dalton-china-consumer-weekly" / "scripts" / "card_io.py",
    )
    workspace = tmp_path / "china"
    outside = tmp_path / "china-out"
    workspace.mkdir()
    outside.mkdir()
    os.symlink(outside, workspace / "state")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        china_io.workspace_file(workspace, "state/china-consumer-db/notes.jsonl")
    china = _china()
    code, payload = _run(
        china,
        [
            "--json", "add-note", "--workspace-dir", str(workspace),
            "--subsector", "baijiu", "--source-type", "company_disclosure",
            "--evidence-grade", "A", "--claim", "批价持平", "--week-of", "2026-11-13",
        ],
    )
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(outside.rglob("*")) == []

    global_io = _load(
        "global_card_io_parent_family2b",
        SKILLS / "dalton-global-consumer-weekly" / "scripts" / "card_io.py",
    )
    gws = tmp_path / "global"
    gout = tmp_path / "global-out"
    gws.mkdir()
    gout.mkdir()
    os.symlink(gout, gws / "state")
    with pytest.raises(ValueError, match="path must stay inside the workspace"):
        global_io.workspace_file(gws, "state/global-consumer/snapshots/2026-11-13.json")
    global_cli = _global()
    code, payload = _run(
        global_cli,
        [
            "--json", "add-note", "--workspace-dir", str(gws),
            "--subsector", "sportswear", "--tier", "A", "--type", "company_disclosure",
            "--claim", "同店持平", "--source", "10-Q",
            "--source-date", "2026-11-13", "--as-of", "2026-11-13",
        ],
    )
    assert code == 2
    assert "path must stay inside the workspace" in payload["error"]
    assert list(gout.rglob("*")) == []


def test_notes_limit_zero_returns_every_record(tmp_path: Path) -> None:
    china = _china()
    workspace = tmp_path / "china"
    workspace.mkdir()
    claims = [f"事实{index:02d}" for index in range(51)]
    for claim in claims:
        code, payload = _run(
            china,
            [
                "--json", "add-note", "--workspace-dir", str(workspace),
                "--subsector", "baijiu", "--source-type", "company_disclosure",
                "--evidence-grade", "A", "--claim", claim,
                "--source-date", "2026-11-11", "--week-of", "2026-11-13",
            ],
        )
        assert code == 0, payload
    code, tail = _run(
        china,
        ["--json", "notes", "--workspace-dir", str(workspace), "--since", "2026-11-08"],
    )
    assert code == 0, tail
    assert tail["count"] == 51
    assert len(tail["notes"]) == 50
    assert tail["notes"][0]["claim"] == "事实01"
    code, full = _run(
        china,
        [
            "--json", "notes", "--workspace-dir", str(workspace),
            "--since", "2026-11-08", "--limit", "0",
        ],
    )
    assert code == 0, full
    assert full["count"] == 51
    assert [row["claim"] for row in full["notes"]] == claims
    skill = (SKILLS / "dalton-china-consumer-weekly" / "SKILL.md").read_text(encoding="utf-8")
    assert "--since <source-start> --limit 0" in skill


def test_weekend_jp_disclosures_use_the_calendar_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _global()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(cli.time, "sleep", lambda *_args, **_kwargs: None)
    html = (
        "<html>全1件<table><tr><td>09:00</td><td>99830</td><td>迅销</td>"
        '<td>月次売上高</td><td><a href="a.pdf">pdf</a></td></tr></table>'
    )

    class _Body:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return html.encode()

    real_urlopen = urllib.request.urlopen  # spec the real function on every call, not a prior patch

    def _run_days(days: str, *extra: str) -> tuple[int, dict, list[str]]:
        opener = create_autospec(real_urlopen, return_value=_Body())
        monkeypatch.setattr(urllib.request, "urlopen", opener)
        code, payload = _run(
            cli,
            [
                "--json", "jp-disclosures", "--workspace-dir", str(workspace),
                "--days", days, *extra,
            ],
        )
        urls = [call.args[0].get_full_url() for call in opener.call_args_list]
        return code, payload, urls

    code, sunday, urls = _run_days("1", "--as-of", "2026-11-15")
    assert code == 0, sunday
    assert sunday["as_of"] == "2026-11-15"
    assert sunday["friday"] == "2026-11-13"
    assert sunday["items"][0]["date"] == "2026-11-15"
    assert urls and all("20261115" in url for url in urls)
    assert all("20261113" not in url and "20261114" not in url for url in urls)

    code, both, urls = _run_days("2", "--as-of", "2026-11-15")
    assert code == 0, both
    assert [item["date"] for item in both["days"]] == ["2026-11-15", "2026-11-14"]
    assert any("20261115" in url for url in urls)
    assert any("20261114" in url for url in urls)
    assert all("20261113" not in url for url in urls)

    monkeypatch.setattr(cli, "hk_today", lambda: date(2026, 11, 15))
    code, implied, urls = _run_days("1")
    assert code == 0, implied
    assert implied["as_of"] == "2026-11-15"
    assert implied["friday"] == "2026-11-13"
    assert urls and all("20261115" in url for url in urls)
    assert all("20261113" not in url for url in urls)

    skill = (SKILLS / "dalton-global-consumer-weekly" / "SKILL.md").read_text(encoding="utf-8")
    assert "--json jp-disclosures --workspace-dir <workspace> --days 7" in skill
    assert "截止日期是香港时间当天" in skill
