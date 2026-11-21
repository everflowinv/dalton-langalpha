"""OC-1 family 1: three unattended skills, cards, and state import."""

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

import pandas as pd
import pytest
import requests
import yaml
import yfinance as yf

from src.dalton.database.automation_reports import (
    REPORT_PROFILES,
    prepare_automation_report_content,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "plugins" / "dalton" / "skills"
PROFILES = ("us-weekly-movers", "x-accounts-daily", "humanoid-robot-weekly")
SKILL_NAMES = (
    "dalton-us-weekly-movers",
    "dalton-x-accounts-daily",
    "dalton-humanoid-robot-weekly",
)
BANNED = (
    "discord",
    "feishu_send_card",
    ".openclaw",
    "openclaw_workspace",
    "xreach",
    "chat_id",
)
# Recipient ids look like oc_abc. A parameter named doc_id is not one.
_RECIPIENT_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}


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
    """Run one CLI in-process and keep stderr for the assertion message."""
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


def _response(payload: dict):
    class Response:
        status_code = 200

        def json(self):
            return payload

    return Response()


def test_skill_frontmatter_and_registry() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        _DALTON_SKILL_SPECS,
        DALTON_SKILL_DEFINITIONS,
    )

    for name in SKILL_NAMES:
        skill = SKILLS / name / "SKILL.md"
        assert skill.is_file()
        text = skill.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.find("\n---\n", 4)
        meta = yaml.safe_load(text[4:end])
        assert meta["name"] == name
        assert isinstance(meta["description"], str) and meta["description"].strip()
        assert "触发词" not in meta["description"]
        assert len(meta["description"]) <= 120
        assert (name, None, ("dalton-bullbear-tracker",)) in _DALTON_SKILL_SPECS
        assert DALTON_SKILL_DEFINITIONS[name].requires == ("dalton-bullbear-tracker",)
        assert DALTON_SKILL_DEFINITIONS[name].description == meta["description"].strip()
        assert DALTON_SKILL_DEFINITIONS[name].command is None


def test_skill_trees_omit_delivery_targets() -> None:
    for name in SKILL_NAMES:
        root = SKILLS / name
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise AssertionError(f"{path} is not UTF-8 text") from exc
            assert _RECIPIENT_OC_RE.search(text) is None, f"{path} contains a recipient id"
            for token in BANNED:
                assert token not in text, f"{path} contains {token}"


def test_universe_uses_requests_autospec_and_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load(
        "us_weekly_family1",
        SKILLS / "dalton-us-weekly-movers" / "scripts" / "us_weekly_movers_cli.py",
    )
    rows = [{"symbol": "ZZ", "name": "Nope", "marketCap": "10"} for _ in range(3000)]
    rows.extend(
        [
            {
                "symbol": "AAA",
                "name": "Alpha Common Stock",
                "marketCap": "2000000000",
                "sector": "Technology",
                "industry": "Semiconductors",
            },
            {
                "symbol": "BBB",
                "name": "Beta Warrant",
                "marketCap": "9000000000",
                "sector": "Technology",
                "industry": "Semiconductors",
            },
            {
                "symbol": "BRK/B",
                "name": "Berkshire Hathaway",
                "marketCap": "5000000000",
                "sector": "Finance",
                "industry": "Insurance",
            },
        ]
    )
    real_get = requests.get
    fake_get = create_autospec(real_get, return_value=_response({"data": {"rows": rows}}))
    monkeypatch.setattr(requests, "get", fake_get)
    code, payload = _run(
        cli,
        ["--json", "universe", "--workspace-dir", str(tmp_path)],
    )
    assert code == 0 and payload["ok"] is True
    assert payload["path"] == "state/us-weekly-movers/universe.json"
    assert payload["universe_n"] == 2
    saved = json.loads((tmp_path / payload["path"]).read_text(encoding="utf-8"))
    symbols = {row["symbol"]: row["yf_symbol"] for row in saved["rows"]}
    assert symbols == {"AAA": "AAA", "BRK/B": "BRK-B"}
    assert fake_get.call_count == 1
    target = tmp_path / "state" / "us-weekly-movers" / "universe.json"
    target.write_text('{"marker":"keep"}\n', encoding="utf-8")
    monkeypatch.setattr(requests, "get", create_autospec(real_get, side_effect=ConnectionError("offline")))
    failed, error = _run(cli, ["--json", "universe", "--workspace-dir", str(tmp_path)])
    assert failed == 1 and error["ok"] is False
    assert target.read_text(encoding="utf-8") == '{"marker":"keep"}\n'


def _price_frame() -> pd.DataFrame:
    index = pd.bdate_range("2026-09-14", "2026-11-06")
    finals = {"AAA": 112.0, "BBB": 115.0, "CCC": 118.0, "DDD": 88.0, "EEE": 85.0, "FFF": 82.0}
    columns = pd.MultiIndex.from_product([list(finals), ["Close", "Volume"]])
    frame = pd.DataFrame(index=index, columns=columns, dtype=float)
    end = pd.Timestamp("2026-11-06")
    for symbol, final in finals.items():
        frame[(symbol, "Close")] = [100.0 if stamp != end else final for stamp in index]
        frame[(symbol, "Volume")] = 1_000_000.0
    return frame


def test_scan_and_themes_are_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load(
        "us_weekly_scan_family1",
        SKILLS / "dalton-us-weekly-movers" / "scripts" / "us_weekly_movers_cli.py",
    )
    industries = {
        "AAA": "Semiconductors",
        "BBB": "Semiconductors",
        "CCC": "Semiconductors",
        "DDD": "Biotechnology",
        "EEE": "Biotechnology",
        "FFF": "Biotechnology",
    }
    sectors = {symbol: "Technology" if industry == "Semiconductors" else "Health Care" for symbol, industry in industries.items()}
    universe = {
        "as_of": "2026-11-01",
        "min_cap": 1e9,
        "rows": [
            {
                "symbol": symbol,
                "yf_symbol": symbol,
                "name": symbol,
                "market_cap": 2e9,
                "sector": sectors[symbol],
                "industry": industries[symbol],
            }
            for symbol in industries
        ],
    }
    relative = "state/us-weekly-movers/universe.json"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(universe), encoding="utf-8")
    monkeypatch.setattr(yf, "download", create_autospec(yf.download, return_value=_price_frame()))
    monkeypatch.setattr(
        cli,
        "earnings_flags",
        create_autospec(cli.earnings_flags, return_value={}),
    )
    code, payload = _run(
        cli,
        [
            "--json",
            "--quiet",
            "scan",
            "--workspace-dir",
            str(tmp_path),
            "--universe-file",
            relative,
        ],
    )
    assert code == 0 and payload["ok"] is True
    assert payload["week_start"] == "2026-10-30"
    assert payload["week_end"] == "2026-11-06"
    assert payload["priced_n"] == 6 and payload["universe_n"] == 6
    assert [row["symbol"] for row in payload["gainers"]] == ["CCC", "BBB", "AAA"]
    assert [row["symbol"] for row in payload["losers"]] == ["FFF", "EEE", "DDD"]
    assert payload["clusters"]["gainers"]["industry"][0]["symbols"][0] == "CCC"
    stored = (tmp_path / "state" / "us-weekly-movers" / "scan_2026-11-06.json").read_text(encoding="utf-8")
    assert "/Users/" not in stored and "oc_" not in stored
    themes = {
        "themes": [
            {
                "week_end": "2026-11-06",
                "side": "gainers",
                "label": "半导体",
                "symbols": ["CCC", "BBB", "AAA"],
                "catalyst_type": "公司公告",
                "median_ret_week": 0.15,
                "verdict": "新主题",
            }
        ]
    }
    theme_file = tmp_path / "temp" / "us-weekly-movers" / "themes.json"
    theme_file.parent.mkdir(parents=True, exist_ok=True)
    theme_file.write_text(json.dumps(themes), encoding="utf-8")
    added, added_payload = _run(
        cli,
        ["--json", "append-themes", "--workspace-dir", str(tmp_path), "--file", "temp/us-weekly-movers/themes.json"],
    )
    again, again_payload = _run(
        cli,
        ["--json", "append-themes", "--workspace-dir", str(tmp_path), "--file", "temp/us-weekly-movers/themes.json"],
    )
    assert added == 0 and added_payload["added"] == 1
    assert again == 0 and again_payload["added"] == 0 and again_payload["skipped"] == 1
    recent_code, recent = _run(cli, ["--json", "recent-themes", "--workspace-dir", str(tmp_path)])
    assert recent_code == 0 and recent["week_end"] == "2026-11-06"
    assert recent["themes"][0]["label"] == "半导体"


def test_earnings_flags_uses_ticker_property(monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load(
        "us_weekly_earnings_family1",
        SKILLS / "dalton-us-weekly-movers" / "scripts" / "us_weekly_movers_cli.py",
    )

    frame = pd.DataFrame({"eps": [1.0]}, index=pd.to_datetime(["2026-11-04"]))
    ticker_cls = create_autospec(yf.Ticker)
    ticker_cls.return_value.earnings_dates = frame
    monkeypatch.setattr(yf, "Ticker", ticker_cls)
    flags = cli.earnings_flags(["AAA"], "2026-10-30", "2026-11-06")
    assert flags["AAA"]["earnings_in_week"] is True
    assert flags["AAA"]["earnings_date"] == "2026-11-04"
    ticker_cls.assert_called_with("AAA")


def test_us_weekly_card_is_schema_2(tmp_path: Path) -> None:
    cli = _load(
        "us_weekly_card_family1",
        SKILLS / "dalton-us-weekly-movers" / "scripts" / "us_weekly_movers_cli.py",
    )
    relative = "temp/us-weekly-movers/2026-11-06-delivery.md"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        "📈 **美股周涨跌幅共性｜2026-10-30 至 2026-11-06**\n"
        "池 6 只（市值 ≥10 亿美元），全池中位 1%，上涨占比 50%\n\n"
        "**▲ 涨幅主题**：无\n\n"
        "**▼ 跌幅主题**：无\n\n"
        "**下周看点**\n观察半导体是否延续。\n",
        encoding="utf-8",
    )
    code, payload = _run(
        cli,
        [
            "--json",
            "render-card",
            "--workspace-dir",
            str(tmp_path),
            "--file",
            relative,
            "--output",
            "temp/us-weekly-movers/2026-11-06-card.json",
            "--title",
            "美股周涨跌幅共性周报",
        ],
    )
    assert code == 0 and payload["ok"] is True
    card = payload["card"]
    prepared, err = prepare_automation_report_content(
        profile="us-weekly-movers",
        cards=[card],
        failure_text=None,
    )
    assert err is None and prepared is not None
    assert card["schema"] == "2.0"
    assert card["header"]["text_tag_list"][0]["text"]["content"] == "周度 Tracking"
    encoded = json.dumps(card, ensure_ascii=False)
    assert "oc_" not in encoded and "discord" not in encoded and "/Users/" not in encoded
    conflict, conflict_payload = _run(
        cli,
        [
            "--json",
            "render-card",
            "--workspace-dir",
            str(tmp_path),
            "--file",
            relative,
            "--output",
            "temp/us-weekly-movers/2026-11-06-card.json",
            "--title",
            "另一张标题",
        ],
    )
    assert conflict == 1 and conflict_payload["error"] == "card_conflict"
    assert "美股周涨跌幅共性周报" in (tmp_path / "temp/us-weekly-movers/2026-11-06-card.json").read_text(encoding="utf-8")


def test_x_daily_select_compose_and_card(tmp_path: Path) -> None:
    cli = _load(
        "x_daily_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    code, window = _run(cli, ["--json", "window", "--as-of", "2026-11-12T08:30:00+08:00"])
    assert code == 0
    assert window["window"]["summary_date"] == "2026-11-12"
    assert window["window"]["start_iso"].startswith("2026-11-11T08:30:00")
    raw = {
        "accounts": {
            "aleabitoreddit": {
                "ok": True,
                "items": [
                    {
                        "id": "1",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T09:00:00+08:00",
                        "text": "kept view",
                        "url": "https://x.com/aleabitoreddit/status/1",
                    },
                    {
                        "id": "2",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T08:00:00+08:00",
                        "text": "too early",
                        "url": "https://x.com/aleabitoreddit/status/2",
                    },
                    {
                        "id": "3",
                        "author_handle": "aleabitoreddit",
                        "createdAt": "Wed Nov 11 00:30:00 +0000 2026",
                        "text": "boundary",
                        "url": "https://x.com/aleabitoreddit/status/3",
                    },
                    {
                        "id": "4",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T12:00:00+08:00",
                        "text": "RT @other: plain",
                        "url": "https://x.com/aleabitoreddit/status/4",
                    },
                    {
                        "id": "5",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T12:30:00+08:00",
                        "text": "quoted view",
                        "is_repost": True,
                        "quoted_id": "50",
                        "url": "https://x.com/aleabitoreddit/status/5",
                    },
                    {
                        "id": "6",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-12T08:31:00+08:00",
                        "text": "too late",
                        "url": "https://x.com/aleabitoreddit/status/6",
                    },
                ],
            },
            "jukan05": {"ok": True, "items": []},
            "mingchikuo": {"ok": True, "items": []},
            "citrini": {"ok": True, "items": []},
        },
        "x_search": {
            "ok": True,
            "items": [
                {
                    "id": "7",
                    "author": "jukan05",
                    "date": "2026-11-11T10:00:00+08:00",
                    "summary_excerpt": "supplement",
                    "url": "https://x.com/jukan05/status/7",
                },
                {
                    "id": "8",
                    "author": "someoneelse",
                    "date": "2026-11-11T10:00:00+08:00",
                    "text": "wrong author",
                    "url": "https://x.com/someoneelse/status/8",
                },
            ],
        },
    }
    day = "temp/x-accounts-daily/2026-11-12"
    raw_path = tmp_path / day / "raw.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    selected_code, selected = _run(
        cli,
        [
            "--json",
            "select",
            "--workspace-dir",
            str(tmp_path),
            "--as-of",
            "2026-11-12T08:30:00+08:00",
            "--input",
            f"{day}/raw.json",
            "--output",
            f"{day}/selected.json",
        ],
    )
    assert selected_code == 0
    kept = [item["id"] for item in selected["selected"]["accounts"]["aleabitoreddit"]["items"]]
    assert kept == ["3", "1", "5"]
    assert [item["id"] for item in selected["selected"]["accounts"]["jukan05"]["items"]] == ["7"]
    summaries = {
        "summaries": {
            "3": "边界这一条",
            "1": "九点这一条 [原推](https://example.com/fake)",
            "5": "引用这一条",
            "7": "补充这一条",
        }
    }
    (tmp_path / day / "summaries.json").write_text(
        json.dumps(summaries),
        encoding="utf-8",
    )
    compose_code, composed = _run(
        cli,
        [
            "--json",
            "compose",
            "--workspace-dir",
            str(tmp_path),
            "--selected",
            f"{day}/selected.json",
            "--summaries",
            f"{day}/summaries.json",
            "--output",
            f"{day}/summary.md",
        ],
    )
    assert compose_code == 0
    markdown = (tmp_path / day / "summary.md").read_text(encoding="utf-8")
    assert "四个账号均抓取成功；SuperGrok X Search 补充成功" in markdown
    assert "至" in markdown and "~" not in markdown
    assert "https://example.com/fake" not in markdown
    assert "https://x.com/aleabitoreddit/status/1" in markdown
    assert markdown.count("\n1. ") == 2
    render_code, rendered = _run(
        cli,
        [
            "--json",
            "render",
            "--workspace-dir",
            str(tmp_path),
            "--file",
            f"{day}/summary.md",
            "--output",
            f"{day}/card.json",
            "--title",
            composed["title"],
        ],
    )
    assert render_code == 0 and rendered["card"]["schema"] == "2.0"
    assert rendered["card"]["header"]["text_tag_list"][0]["text"]["content"] == "每日总结"
    prepared, err = prepare_automation_report_content(
        profile="x-accounts-daily",
        cards=[rendered["card"]],
        failure_text=None,
    )
    assert err is None and prepared is not None
    assert "oc_" not in json.dumps(rendered["card"], ensure_ascii=False)


def test_humanoid_plan_validate_render_and_state(tmp_path: Path) -> None:
    cli = _load(
        "humanoid_family1",
        SKILLS / "dalton-humanoid-robot-weekly" / "scripts" / "humanoid_robot_weekly.py",
    )
    self_code, checks = _run(cli, ["--json", "self-test", "--workspace-dir", str(tmp_path)])
    assert self_code == 0 and checks["checks"]["card_renderer"] is True
    plan_code, plan = _run(cli, ["plan", "--as-of", "2026-11-15T18:00:00"])
    assert plan_code == 0
    assert plan["window"]["timezone"] == "Asia/Hong_Kong"
    assert plan["window"]["end_date"] == "2026-11-15"
    assert plan["window"]["start_date"] == "2026-11-08"
    assert len(plan["tool_calls"]["x_timeline"]) == 15
    assert "params" not in plan["tool_calls"]["x_timeline"][0]
    assert plan["tool_calls"]["guidepoint"][0]["limit"] == 30
    assert plan["reddit_covered"] is False
    assert "allowed_x_handles" in plan["missing_params"]
    valid_code, valid = _run(
        cli,
        ["--json", "validate", "--workspace-dir", str(tmp_path), "--file", "templates/weekly-card.md"],
    )
    assert valid_code == 0 and valid["ok"] is True
    render_code, rendered = _run(
        cli,
        [
            "--json",
            "render-card",
            "--workspace-dir",
            str(tmp_path),
            "--validate",
            "--file",
            "templates/weekly-card.md",
            "--output",
            "temp/humanoid-robot-weekly/2026-11-15-card.json",
        ],
    )
    assert render_code == 0 and rendered["card"]["schema"] == "2.0"
    prepared, err = prepare_automation_report_content(
        profile="humanoid-robot-weekly",
        cards=[rendered["card"]],
        failure_text=None,
    )
    assert err is None and prepared is not None
    assert "周度 Tracking" == rendered["card"]["header"]["text_tag_list"][0]["text"]["content"]
    archive = tmp_path / "research-outputs" / "humanoid-robot-weekly" / "2026-11-15.md"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        (SKILLS / "dalton-humanoid-robot-weekly" / "templates" / "weekly-card.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    sent_code, sent = _run(
        cli,
        [
            "--json",
            "mark-sent",
            "--workspace-dir",
            str(tmp_path),
            "--report-date",
            "2026-11-15",
            "--archive",
            "research-outputs/humanoid-robot-weekly/2026-11-15.md",
            "--window-start",
            "2026-11-08T18:00:00+08:00",
            "--window-end",
            "2026-11-15T18:00:00+08:00",
            "--validation",
            "passed",
        ],
    )
    assert sent_code == 0 and sent["reused"] is False
    state_path = tmp_path / "state" / "humanoid-robot-weekly" / "state.json"
    body = state_path.read_text(encoding="utf-8")
    assert "chat_id" not in body and "oc_" not in body
    again_code, again = _run(
        cli,
        [
            "--json",
            "mark-sent",
            "--workspace-dir",
            str(tmp_path),
            "--report-date",
            "2026-11-15",
            "--archive",
            "research-outputs/humanoid-robot-weekly/2026-11-15.md",
            "--window-start",
            "2026-11-08T18:00:00+08:00",
            "--window-end",
            "2026-11-15T18:00:00+08:00",
        ],
    )
    assert again_code == 0 and again["reused"] is True
    archive.write_text(archive.read_text(encoding="utf-8") + "\n补充一句。\n", encoding="utf-8")
    conflict_code, conflict = _run(
        cli,
        [
            "--json",
            "mark-sent",
            "--workspace-dir",
            str(tmp_path),
            "--report-date",
            "2026-11-15",
            "--archive",
            "research-outputs/humanoid-robot-weekly/2026-11-15.md",
            "--window-start",
            "2026-11-08T18:00:00+08:00",
            "--window-end",
            "2026-11-15T18:00:00+08:00",
        ],
    )
    assert conflict_code == 1 and conflict["error"] == "state_conflict"
    assert state_path.read_text(encoding="utf-8") == body


def test_skill_commands_use_documented_argument_order(tmp_path: Path) -> None:
    us = _load(
        "us_weekly_argv_family1",
        SKILLS / "dalton-us-weekly-movers" / "scripts" / "us_weekly_movers_cli.py",
    )
    us_argv = _skill_argv("dalton-us-weekly-movers", "recent-themes", {"<workspace>": str(tmp_path)})
    assert us_argv[:4] == ["--json", "recent-themes", "--workspace-dir", str(tmp_path)]
    code, payload, err = _run_captured(us, us_argv)
    assert code == 0 and payload.get("ok") is True, f"stderr={err!r} payload={payload!r}"
    before, before_payload, before_err = _run_captured(
        us, ["--json", "--workspace-dir", str(tmp_path), "recent-themes"]
    )
    assert before == 0 and before_payload.get("ok") is True, (
        f"stderr={before_err!r} payload={before_payload!r}"
    )

    summary = "2026-11-12"
    x_cli = _load(
        "x_daily_argv_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    x_argv = _skill_argv(
        "dalton-x-accounts-daily",
        "select",
        {
            "<workspace>": str(tmp_path),
            "<end_iso>": "2026-11-12T08:30:00+08:00",
            "<summary_date>": summary,
        },
    )
    assert x_argv[:4] == ["--json", "select", "--workspace-dir", str(tmp_path)]
    raw_rel = f"temp/x-accounts-daily/{summary}/raw.json"
    raw_path = tmp_path / raw_rel
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(json.dumps({"accounts": {}, "x_search": {"ok": False}}), encoding="utf-8")
    x_code, x_payload, x_err = _run_captured(x_cli, x_argv)
    assert x_code == 0 and x_payload.get("ok") is True, f"stderr={x_err!r} payload={x_payload!r}"
    assert x_payload["path"] == f"temp/x-accounts-daily/{summary}/selected.json"

    report = "2026-11-15"
    humanoid = _load(
        "humanoid_argv_family1",
        SKILLS / "dalton-humanoid-robot-weekly" / "scripts" / "humanoid_robot_weekly.py",
    )
    humanoid_argv = _skill_argv(
        "dalton-humanoid-robot-weekly",
        "validate",
        {"<workspace>": str(tmp_path), "<report_date>": report},
    )
    assert humanoid_argv[:4] == ["--json", "validate", "--workspace-dir", str(tmp_path)]
    report_path = tmp_path / "temp" / "humanoid-robot-weekly" / f"{report}.md"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        (SKILLS / "dalton-humanoid-robot-weekly" / "templates" / "weekly-card.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    humanoid_code, humanoid_payload, humanoid_err = _run_captured(humanoid, humanoid_argv)
    assert humanoid_code == 0 and humanoid_payload.get("ok") is True, (
        f"stderr={humanoid_err!r} payload={humanoid_payload!r}"
    )


def test_x_daily_consecutive_days_each_get_a_card(tmp_path: Path) -> None:
    cli = _load(
        "x_daily_two_days_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    handles = ("aleabitoreddit", "jukan05", "mingchikuo", "citrini")
    for summary, as_of in (
        ("2026-11-12", "2026-11-12T08:30:00+08:00"),
        ("2026-11-13", "2026-11-13T08:30:00+08:00"),
    ):
        root = f"temp/x-accounts-daily/{summary}"
        folder = tmp_path / root
        folder.mkdir(parents=True)
        raw = {
            "accounts": {handle: {"ok": True, "items": []} for handle in handles},
            "x_search": {"ok": True, "items": []},
        }
        (folder / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
        (folder / "summaries.json").write_text(json.dumps({"summaries": {}}), encoding="utf-8")
        selected_code, selected = _run(
            cli,
            [
                "--json",
                "select",
                "--workspace-dir",
                str(tmp_path),
                "--as-of",
                as_of,
                "--input",
                f"{root}/raw.json",
                "--output",
                f"{root}/selected.json",
            ],
        )
        assert selected_code == 0 and selected["ok"] is True
        compose_code, composed = _run(
            cli,
            [
                "--json",
                "compose",
                "--workspace-dir",
                str(tmp_path),
                "--selected",
                f"{root}/selected.json",
                "--summaries",
                f"{root}/summaries.json",
                "--output",
                f"{root}/summary.md",
            ],
        )
        assert compose_code == 0 and summary in composed["title"]
        render_code, rendered = _run(
            cli,
            [
                "--json",
                "render",
                "--workspace-dir",
                str(tmp_path),
                "--file",
                f"{root}/summary.md",
                "--output",
                f"{root}/card.json",
                "--title",
                composed["title"],
            ],
        )
        assert render_code == 0 and rendered["ok"] is True
        assert (folder / "raw.json").is_file()
        assert (folder / "selected.json").is_file()
        assert (folder / "summary.md").is_file()
        assert (folder / "card.json").is_file()
    first = (tmp_path / "temp/x-accounts-daily/2026-11-12/card.json").read_bytes()
    second = (tmp_path / "temp/x-accounts-daily/2026-11-13/card.json").read_bytes()
    assert first != second


def test_x_daily_handle_match_keeps_original_spelling(tmp_path: Path) -> None:
    cli = _load(
        "x_daily_case_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    raw = {
        "accounts": {
            "Jukan05": {
                "ok": True,
                "items": [
                    {
                        "id": "9",
                        "author_handle": "Jukan05",
                        "created_at": "2026-11-11T10:00:00+08:00",
                        "text": "kept",
                        "url": "https://x.com/Jukan05/status/9",
                    }
                ],
            }
        },
        "x_search": {
            "ok": True,
            "items": [
                {
                    "id": "10",
                    "author": "Jukan05",
                    "date": "2026-11-11T11:00:00+08:00",
                    "summary_excerpt": "search",
                    "url": "https://x.com/Jukan05/status/10",
                }
            ],
        },
    }
    folder = tmp_path / "temp" / "x-accounts-daily" / "2026-11-12"
    folder.mkdir(parents=True)
    (folder / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    code, selected = _run(
        cli,
        [
            "--json",
            "select",
            "--workspace-dir",
            str(tmp_path),
            "--as-of",
            "2026-11-12T08:30:00+08:00",
            "--input",
            "temp/x-accounts-daily/2026-11-12/raw.json",
            "--output",
            "temp/x-accounts-daily/2026-11-12/selected.json",
        ],
    )
    assert code == 0
    account = selected["selected"]["accounts"]["jukan05"]
    assert [item["id"] for item in account["items"]] == ["9", "10"]
    assert account["display_handle"] == "Jukan05"
    (folder / "summaries.json").write_text(
        json.dumps({"summaries": {"9": "时间线原名", "10": "检索原名"}}),
        encoding="utf-8",
    )
    compose_code, _composed = _run(
        cli,
        [
            "--json",
            "compose",
            "--workspace-dir",
            str(tmp_path),
            "--selected",
            "temp/x-accounts-daily/2026-11-12/selected.json",
            "--summaries",
            "temp/x-accounts-daily/2026-11-12/summaries.json",
            "--output",
            "temp/x-accounts-daily/2026-11-12/summary.md",
        ],
    )
    assert compose_code == 0
    assert "## @Jukan05" in (folder / "summary.md").read_text(encoding="utf-8")


def test_temp_symlink_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel", encoding="utf-8")
    workspace = tmp_path / "ws"
    for skill in SKILL_NAMES:
        card_io = _load(
            "card_io_symlink_" + skill.replace("-", "_"),
            SKILLS / skill / "scripts" / "card_io.py",
        )
        day = workspace / "temp" / skill
        day.mkdir(parents=True)
        os.symlink(outside, day / ".card.json.tmp")
        target = day / "card.json"
        card_io.write_bytes_exclusive(target, b"{}\n")
        assert outside.read_text(encoding="utf-8") == "sentinel"
        assert target.read_bytes() == b"{}\n"
        assert not target.is_symlink()
        assert (day / ".card.json.tmp").is_symlink()


def test_parent_directory_symlink_stays_inside_workspace(tmp_path: Path) -> None:
    us = _load(
        "us_weekly_parent_link_family1",
        SKILLS / "dalton-us-weekly-movers" / "scripts" / "us_weekly_movers_cli.py",
    )
    workspace = tmp_path / "us"
    outside = tmp_path / "us-outside"
    workspace.mkdir()
    outside.mkdir()
    os.symlink(outside, workspace / "state")
    themes = workspace / "temp" / "us-weekly-movers" / "themes.json"
    themes.parent.mkdir(parents=True)
    themes.write_text(
        json.dumps(
            {
                "themes": [
                    {
                        "week_end": "2026-11-06",
                        "side": "gainers",
                        "label": "半导体",
                        "symbols": ["AAA", "BBB", "CCC"],
                        "catalyst_type": "公司公告",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    code, payload = _run(
        us,
        [
            "--json",
            "append-themes",
            "--workspace-dir",
            str(workspace),
            "--file",
            "temp/us-weekly-movers/themes.json",
        ],
    )
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(outside.rglob("*")) == []

    x_cli = _load(
        "x_daily_parent_link_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    x_workspace = tmp_path / "x"
    x_outside = tmp_path / "x-outside"
    x_workspace.mkdir()
    x_outside.mkdir()
    os.symlink(x_outside, x_workspace / "temp")
    (x_workspace / "raw.json").write_text(
        json.dumps({"accounts": {}, "x_search": {"ok": False}}),
        encoding="utf-8",
    )
    x_code, x_payload = _run(
        x_cli,
        [
            "--json",
            "select",
            "--workspace-dir",
            str(x_workspace),
            "--as-of",
            "2026-11-12T08:30:00+08:00",
            "--input",
            "raw.json",
            "--output",
            "temp/x-accounts-daily/2026-11-12/selected.json",
        ],
    )
    assert x_code == 1
    assert "path must stay inside the workspace" in x_payload["error"]
    assert list(x_outside.rglob("*")) == []

    humanoid = _load(
        "humanoid_parent_link_family1",
        SKILLS / "dalton-humanoid-robot-weekly" / "scripts" / "humanoid_robot_weekly.py",
    )
    probe_workspace = tmp_path / "probe"
    probe_outside = tmp_path / "probe-outside"
    probe_workspace.mkdir()
    probe_outside.mkdir()
    os.symlink(probe_outside, probe_workspace / "state")
    probe_code, probe_payload = _run(
        humanoid,
        ["--json", "self-test", "--workspace-dir", str(probe_workspace)],
    )
    assert probe_code == 1
    assert probe_payload["checks"]["state_dir_writable"] is False
    assert list(probe_outside.rglob("*")) == []

    copy_workspace = tmp_path / "copy"
    copy_outside = tmp_path / "copy-outside"
    copy_workspace.mkdir()
    copy_outside.mkdir()
    os.symlink(copy_outside, copy_workspace / "temp")
    copy_code, copy_payload = _run(
        humanoid,
        [
            "--json",
            "render-card",
            "--workspace-dir",
            str(copy_workspace),
            "--file",
            "templates/weekly-card.md",
            "--output",
            "temp/humanoid-robot-weekly/2026-11-15-card.json",
        ],
    )
    assert copy_code == 1
    assert "path must stay inside the workspace" in copy_payload["error"]
    assert list(copy_outside.rglob("*")) == []


def test_humanoid_empty_section_does_not_skip_other_labels() -> None:
    cli = _load(
        "humanoid_labels_family1",
        SKILLS / "dalton-humanoid-robot-weekly" / "scripts" / "humanoid_robot_weekly.py",
    )
    template = (SKILLS / "dalton-humanoid-robot-weekly" / "templates" / "weekly-card.md").read_text(encoding="utf-8")
    assert cli.validate_text(template) == []
    mixed_order = template.replace(
        "- 本周无符合门槛的规模化订单。",
        "- 本周无符合门槛的规模化订单。\n- 客户甲签署 1200 台订单，2027 年交付。",
    )
    order_errors = cli.validate_text(mixed_order)
    assert any("订单，尚非部署" in item for item in order_errors)
    assert not any("演示级证据" in item for item in order_errors)
    labeled = mixed_order.replace(
        "- 客户甲签署 1200 台订单，2027 年交付。",
        "- 客户甲签署 1200 台订单，尚非部署，2027 年交付。",
    )
    assert cli.validate_text(labeled) == []
    mixed_demo = template.replace(
        "- 本周无符合门槛的重大新增。",
        "- 本周无符合门槛的重大新增。\n- 公司发布了行走演示。",
    )
    demo_errors = cli.validate_text(mixed_demo)
    assert any("演示级证据" in item for item in demo_errors)
    assert not any("订单，尚非部署" in item for item in demo_errors)
    inline_demo = template.replace(
        "- 本周无符合门槛的重大新增。",
        "- 公司本周无新融资，仅发布行走演示。",
    )
    assert any("演示级证据" in item for item in cli.validate_text(inline_demo))


@pytest.mark.asyncio
async def test_bullbear_renderer_stays_when_a_dependent_is_enabled(tmp_path: Path) -> None:
    from ptc_agent.agent.middleware.skills.registry import (
        SKILL_REGISTRY,
        resolve_disabled_skills,
    )
    from ptc_agent.core.sandbox.assets import _collect_local_skill_names

    layer = "dalton-bullbear-tracker"
    dependents = {name for name, skill in SKILL_REGISTRY.items() if layer in skill.requires}
    assert set(SKILL_NAMES) <= dependents
    assert layer not in resolve_disabled_skills({layer})
    assert resolve_disabled_skills({layer, *dependents}) == frozenset({layer, *dependents})
    for name in (layer, *SKILL_NAMES):
        skill_dir = tmp_path / name
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    kept = await _collect_local_skill_names([str(tmp_path)], disabled=frozenset({layer}))
    assert kept == {layer, *SKILL_NAMES}
    removed = await _collect_local_skill_names(
        [str(tmp_path)],
        disabled=frozenset({layer, *dependents}),
    )
    assert layer not in removed
    assert not (set(SKILL_NAMES) & removed)


def test_profiles_are_registered() -> None:
    assert set(PROFILES) <= REPORT_PROFILES


def test_x_accounts_daily_card_io_splits_oversized_report_and_locks_render_card_constants(
    tmp_path: Path,
) -> None:
    """Oversized X accounts daily markdown (>33 KB) splits into compliant cards via card_io."""
    from src.dalton.data_jobs.notification_payloads import (
        FEISHU_CARD_MAX_BYTES,
        FEISHU_CARD_MAX_ELEMENTS,
        FEISHU_CARD_TABLE_LIMIT,
        feishu_card_serialized_utf8_bytes,
        is_feishu_card_within_envelope,
    )

    render_mod = _load(
        "bullbear_render_card_lock",
        SKILLS / "dalton-bullbear-tracker" / "scripts" / "render_card.py",
    )
    assert render_mod.FEISHU_CARD_MAX_BYTES == FEISHU_CARD_MAX_BYTES
    assert render_mod.FEISHU_CARD_MAX_ELEMENTS == FEISHU_CARD_MAX_ELEMENTS
    assert render_mod.FEISHU_CARD_TABLE_LIMIT == FEISHU_CARD_TABLE_LIMIT

    x_card_io = _load(
        "x_accounts_card_io_split_test",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "card_io.py",
    )
    lines = ["# 📋 推特每日总结 — 2026-11-20", ""]
    for handle in ("aleabitoreddit", "jukan05", "mingchikuo", "citrini"):
        lines.extend(["", f"## @{handle}", ""])
        for idx in range(1, 26):
            lines.append(
                f"{idx}. @{handle} 第 {idx} 条深度摘要：追踪 AI 算力资本开支、晶圆代工产能、"
                f"存储芯片报价与消费电子供应链最新动态变化分析补充说明，"
                f"涵盖北美云厂商资本开支指引、先进封装 CoWoS 扩产节奏、HBM 高带宽内存价格走势"
                f"以及终端需求与库存周期的交叉验证细节。"
                f" [原推](https://x.com/{handle}/status/100000{idx:02d})"
            )
    md_text = "\n".join(lines) + "\n"
    assert len(md_text.encode("utf-8")) > 30_000
    (tmp_path / "summary.md").write_text(md_text, encoding="utf-8")

    result = x_card_io.render_markdown(
        workspace=tmp_path,
        markdown_rel="summary.md",
        output_rel="card.json",
        title="📋 推特每日总结 — 2026-11-20",
    )
    assert result["ok"] is True
    cards = result["cards"]
    assert len(cards) >= 2
    for idx, card in enumerate(cards, start=1):
        assert is_feishu_card_within_envelope(card)
        assert feishu_card_serialized_utf8_bytes(card) <= FEISHU_CARD_MAX_BYTES
        assert len(card["body"]["elements"]) <= FEISHU_CARD_MAX_ELEMENTS
        assert f"（{idx}/{len(cards)}）" in card["header"]["title"]["content"]

    saved_json = json.loads((tmp_path / "card.json").read_text(encoding="utf-8"))
    assert isinstance(saved_json, list) and len(saved_json) == len(cards)


def test_x_daily_has_more_checks_oldest_timestamp(tmp_path: Path) -> None:
    cli = _load(
        "x_daily_has_more_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    as_of = "2026-11-12T08:30:00+08:00"
    day = "temp/x-accounts-daily/2026-11-12"
    base_dir = tmp_path / day
    base_dir.mkdir(parents=True)

    # 1. has_more=True + oldest item earlier than window start -> treated as success, no incomplete note
    covered_raw = {
        "accounts": {
            "aleabitoreddit": {
                "ok": True,
                "has_more": True,
                "items": [
                    {"id": "1", "author_handle": "aleabitoreddit", "text": "undated"},
                    {
                        "id": "2",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T09:00:00+08:00",
                        "text": "in window",
                        "url": "https://x.com/aleabitoreddit/status/2",
                    },
                    {
                        "id": "3",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T08:29:59+08:00",
                        "text": "before window start",
                        "url": "https://x.com/aleabitoreddit/status/3",
                    },
                ],
            },
            "jukan05": {"ok": True, "has_more": False, "items": []},
            "mingchikuo": {"ok": True, "has_more": False, "items": []},
            "citrini": {"ok": True, "has_more": False, "items": []},
        },
        "x_search": {"ok": True, "items": []},
    }
    (base_dir / "raw.json").write_text(json.dumps(covered_raw), encoding="utf-8")
    (base_dir / "summaries.json").write_text(json.dumps({"summaries": {"2": "窗口内推文"}}), encoding="utf-8")
    code, selected = _run(
        cli,
        [
            "--json", "select", "--workspace-dir", str(tmp_path),
            "--as-of", as_of,
            "--input", f"{day}/raw.json",
            "--output", f"{day}/selected.json",
        ],
    )
    assert code == 0
    assert selected["selected"]["accounts"]["aleabitoreddit"]["incomplete"] is False
    code, _ = _run(
        cli,
        [
            "--json", "compose", "--workspace-dir", str(tmp_path),
            "--selected", f"{day}/selected.json",
            "--summaries", f"{day}/summaries.json",
            "--output", f"{day}/summary.md",
        ],
    )
    assert code == 0
    md_covered = (base_dir / "summary.md").read_text(encoding="utf-8")
    assert "四个账号均抓取成功；SuperGrok X Search 补充成功" in md_covered
    assert "超过已取回的数量" not in md_covered
    assert "未取回" not in md_covered

    # 2. has_more=True + oldest timestamped item inside window (undated ignored);
    #    plus has_more=True with undated-only; plus has_more=False unchanged.
    incomplete_raw = {
        "accounts": {
            "aleabitoreddit": {
                "ok": True,
                "has_more": True,
                "items": [
                    {"id": "10", "author_handle": "aleabitoreddit", "text": "undated item"},
                    {
                        "id": "11",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-12T01:00:00+08:00",
                        "text": "latest",
                        "url": "https://x.com/aleabitoreddit/status/11",
                    },
                    {
                        "id": "12",
                        "author_handle": "aleabitoreddit",
                        "created_at": "2026-11-11T04:15:30Z",
                        "text": "oldest in window",
                        "url": "https://x.com/aleabitoreddit/status/12",
                    },
                ],
            },
            "jukan05": {
                "ok": True,
                "has_more": True,
                "items": [{"id": "20", "author_handle": "jukan05", "text": "no timestamp"}],
            },
            "mingchikuo": {
                "ok": True,
                "has_more": False,
                "items": [
                    {
                        "id": "30",
                        "author_handle": "mingchikuo",
                        "created_at": "2026-11-11T18:00:00+08:00",
                        "text": "complete page",
                        "url": "https://x.com/mingchikuo/status/30",
                    }
                ],
            },
            "citrini": {"ok": True, "has_more": False, "items": []},
        },
        "x_search": {"ok": True, "items": []},
    }
    (base_dir / "raw_inc.json").write_text(json.dumps(incomplete_raw), encoding="utf-8")
    (base_dir / "summaries_inc.json").write_text(
        json.dumps({"summaries": {"11": "最新", "12": "窗口内最早", "30": "完整页"}}),
        encoding="utf-8",
    )
    code, selected_inc = _run(
        cli,
        [
            "--json", "select", "--workspace-dir", str(tmp_path),
            "--as-of", as_of,
            "--input", f"{day}/raw_inc.json",
            "--output", f"{day}/selected_inc.json",
        ],
    )
    assert code == 0
    assert selected_inc["selected"]["accounts"]["aleabitoreddit"]["incomplete"] is True
    assert selected_inc["selected"]["accounts"]["jukan05"]["incomplete"] is True
    assert selected_inc["selected"]["accounts"]["mingchikuo"]["incomplete"] is False
    code, _ = _run(
        cli,
        [
            "--json", "compose", "--workspace-dir", str(tmp_path),
            "--selected", f"{day}/selected_inc.json",
            "--summaries", f"{day}/summaries_inc.json",
            "--output", f"{day}/summary_inc.md",
        ],
    )
    assert code == 0
    md_inc = (base_dir / "summary_inc.md").read_text(encoding="utf-8")
    assert "- @aleabitoreddit 窗口内可能还有未取回的帖子，最早只取到 2026-11-11 12:15:30（香港时间）" in md_inc
    assert "- @jukan05 窗口内可能还有未取回的帖子\n" in md_inc
    assert "- @mingchikuo 时间线抓取成功" in md_inc
    assert "- @citrini 时间线抓取成功" in md_inc
    assert "四个账号均抓取成功" not in md_inc


def test_x_daily_nested_bridge_coverage_and_utc_window(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    scripts_dalton = REPO_ROOT / "scripts" / "dalton"
    if str(scripts_dalton) not in sys.path:
        sys.path.insert(0, str(scripts_dalton))
    import researchdata_bridge as rb

    cli = _load(
        "x_daily_nested_utc_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )

    # 1. Nested bridge shape (truncated, covered): 49 items inside window + 2 items before window start -> 50th returned item is before start.
    as_of = "2026-11-12T08:30:00+08:00"
    win = cli.compute_window(as_of)
    raw_tweets = []
    base = datetime(2026, 11, 11, 8, 30, 0, tzinfo=cli.TZ)
    for idx in range(49):
        dt_utc = (base + timedelta(minutes=25 * (49 - idx))).astimezone(timezone.utc)
        raw_tweets.append(
            {
                "id": str(500 + idx),
                "text": f"in-window-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "aleabitoreddit", "name": "aleabitoreddit"},
            }
        )
    for idx, mins in enumerate((1, 10)):
        dt_utc = (base - timedelta(minutes=mins)).astimezone(timezone.utc)
        raw_tweets.append(
            {
                "id": str(498 - idx),
                "text": f"before-window-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "aleabitoreddit", "name": "aleabitoreddit"},
            }
        )
    bridge_data = rb._normalize_x_timeline_output(
        {"items": raw_tweets, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    assert len(bridge_data["items"]) == 50
    assert bridge_data["truncated"] is True
    assert bridge_data["has_more"] is True
    assert bridge_data["oldest_fetched_at"] == "2026-11-11T00:20:00+00:00"
    assert bridge_data["oldest_returned_at"] == "2026-11-11T00:29:00+00:00"

    # 1b. Codex middle-1: 51 in-window + 1 before-window truncated to 50 -> oldest_returned_at is still in window -> warns!
    raw_51 = []
    for idx in range(51):
        dt_utc = (base + timedelta(minutes=20 * (51 - idx))).astimezone(timezone.utc)
        raw_51.append(
            {
                "id": str(600 + idx),
                "text": f"in-window-51-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "citrini", "name": "citrini"},
            }
        )
    raw_51.append(
        {
            "id": "599",
            "text": "before-window-51",
            "createdAt": "Wed Nov 11 00:29:00 +0000 2026",
            "user": {"screenName": "citrini", "name": "citrini"},
        }
    )
    bridge_51 = rb._normalize_x_timeline_output(
        {"items": raw_51, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    assert bridge_51["truncated"] is True
    assert bridge_51["oldest_fetched_at"] == "2026-11-11T00:29:00+00:00"
    assert bridge_51["oldest_returned_at"] == "2026-11-11T01:10:00+00:00"  # 09:10 HKT >= start

    # 1c. Codex middle-2: old pinned tweet at index 0 + in-window tweets with hasMore=True -> pinned excluded -> warns!
    bridge_pinned = rb._normalize_x_timeline_output(
        {
            "items": [
                {
                    "id": "901",
                    "text": "old pinned post",
                    "createdAt": "Thu Jan 01 00:00:00 +0000 2026",
                    "user": {"screenName": "mingchikuo", "name": "mingchikuo"},
                },
                {
                    "id": "902",
                    "text": "in-window post",
                    "createdAt": "Thu Nov 12 00:00:00 +0000 2026",  # 08:00 HKT
                    "user": {"screenName": "mingchikuo", "name": "mingchikuo"},
                },
            ],
            "hasMore": True,
        },
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    assert bridge_pinned["truncated"] is False
    assert bridge_pinned["oldest_fetched_at"] == "2026-11-12T00:00:00+00:00"
    assert bridge_pinned["oldest_returned_at"] == "2026-11-12T00:00:00+00:00"

    raw_nested = {
        "accounts": {
            "aleabitoreddit": {"ok": True, "status": "ok", "data": bridge_data},
            # Codex middle-3: nested empty timeline with has_more=True + x_search item for jukan05.
            "jukan05": {
                "ok": True,
                "status": "empty",
                "empty_reason": "no_matching_items",
                "data": {
                    "items": [],
                    "returned": 0,
                    "has_more": True,
                    "truncated": False,
                    "oldest_fetched_at": None,
                    "oldest_returned_at": None,
                },
            },
            "mingchikuo": {"ok": True, "status": "ok", "data": bridge_pinned},
            "citrini": {"ok": True, "status": "ok", "data": bridge_51},
        },
        "x_search": {
            "ok": True,
            "items": [
                {
                    "id": "777",
                    "author": "jukan05",
                    "date": "2026-11-11T15:00:00+08:00",
                    "summary_excerpt": "from x_search only",
                    "url": "https://x.com/jukan05/status/777",
                }
            ],
        },
    }
    selected = cli.select_posts(raw_nested, win)
    assert selected["accounts"]["aleabitoreddit"]["incomplete"] is False
    assert selected["accounts"]["jukan05"]["incomplete"] is True
    assert selected["accounts"]["jukan05"]["oldest_created_at"] == ""
    assert selected["accounts"]["mingchikuo"]["incomplete"] is True
    assert selected["accounts"]["citrini"]["incomplete"] is True

    summaries = {str(500 + idx): f"摘要{idx}" for idx in range(49)}
    summaries.update({str(600 + idx): f"摘要51-{idx}" for idx in range(50)})
    summaries["902"] = "置顶同行"
    summaries["777"] = "补充摘要"
    composed = cli.compose_markdown(selected, summaries)
    md = composed["markdown"]
    assert "- @aleabitoreddit 时间线抓取成功" in md
    assert "- @jukan05 窗口内可能还有未取回的帖子\n" in md
    assert "2026-11-11 15:00:00" not in md  # x_search timestamp never leaks into timeline note
    assert "- @mingchikuo 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 08:00:00（香港时间）" in md
    assert "- @citrini 窗口内可能还有未取回的帖子，最早只取到 2026-11-11 09:10:00（香港时间）" in md

    # 1d. Post-end oldest_fetched_at (>= start) + truncated=True with empty items (oldest_returned_at=None) falls back to oldest_fetched_at.
    selected_post_end = cli.select_posts(
        {
            "accounts": {
                "aleabitoreddit": {"ok": True, "items": []},
                "jukan05": {"ok": True, "items": []},
                "mingchikuo": {
                    "ok": True,
                    "status": "ok",
                    "data": {
                        "items": [],
                        "returned": 0,
                        "has_more": False,
                        "truncated": True,
                        "oldest_fetched_at": "2026-11-12T01:00:00+00:00",  # 09:00 HKT > end
                        "oldest_returned_at": None,
                    },
                },
                "citrini": {"ok": True, "items": []},
            },
            "x_search": {"ok": True, "items": []},
        },
        win,
    )
    assert selected_post_end["accounts"]["mingchikuo"]["incomplete"] is True
    md_post_end = cli.compose_markdown(selected_post_end, {})["markdown"]
    assert "- @mingchikuo 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 09:00:00（香港时间）" in md_post_end

    # 1e. Unified possible wording for both 50+1 pre-window and 51 in-window truncated timelines.
    raw_50_plus_1 = []
    for idx in range(50):
        dt_utc = (base + timedelta(minutes=20 * (50 - idx))).astimezone(timezone.utc)
        raw_50_plus_1.append(
            {
                "id": str(700 + idx),
                "text": f"in-win-50-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "aleabitoreddit", "name": "aleabitoreddit"},
            }
        )
    raw_50_plus_1.append(
        {
            "id": "699",
            "text": "pre-win-1",
            "createdAt": "Wed Nov 11 00:29:00 +0000 2026",
            "user": {"screenName": "aleabitoreddit", "name": "aleabitoreddit"},
        }
    )
    bridge_50_plus_1 = rb._normalize_x_timeline_output(
        {"items": raw_50_plus_1, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    raw_51_in_window = []
    for idx in range(51):
        dt_utc = (base + timedelta(minutes=20 * (51 - idx))).astimezone(timezone.utc)
        raw_51_in_window.append(
            {
                "id": str(800 + idx),
                "text": f"in-win-51-only-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "jukan05", "name": "jukan05"},
            }
        )
    bridge_51_in_window = rb._normalize_x_timeline_output(
        {"items": raw_51_in_window, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    selected_distinction = cli.select_posts(
        {
            "accounts": {
                "aleabitoreddit": {"ok": True, "status": "ok", "data": bridge_50_plus_1},
                "jukan05": {"ok": True, "status": "ok", "data": bridge_51_in_window},
                "mingchikuo": {"ok": True, "items": []},
                "citrini": {"ok": True, "items": []},
            },
            "x_search": {"ok": True, "items": []},
        },
        win,
    )
    assert selected_distinction["accounts"]["aleabitoreddit"]["incomplete"] is True
    assert selected_distinction["accounts"]["jukan05"]["incomplete"] is True
    assert "incomplete_certain" not in selected_distinction["accounts"]["jukan05"]
    dist_summaries = {str(700 + idx): f"五十摘要{idx}" for idx in range(50)}
    dist_summaries.update({str(800 + idx): f"五一摘要{idx}" for idx in range(50)})
    md_dist = cli.compose_markdown(selected_distinction, dist_summaries)["markdown"]
    assert "- @aleabitoreddit 窗口内可能还有未取回的帖子，最早只取到 2026-11-11 08:50:00（香港时间）" in md_dist
    assert "- @jukan05 窗口内可能还有未取回的帖子，最早只取到 2026-11-11 09:10:00（香港时间）" in md_dist
    assert "窗口内帖子超过已取回的数量" not in md_dist

    # 2. HKT 07:30 window end: since/until use UTC dates so tweets near window start (previous UTC day) survive bridge filtering.
    win_0730 = cli.compute_window("2026-11-12T07:30:00+08:00")
    assert win_0730["start_date"] == "2026-11-10"
    assert win_0730["end_date"] == "2026-11-11"
    assert win_0730["summary_date"] == "2026-11-12"
    bridge_0730 = rb._normalize_x_timeline_output(
        {
            "items": [
                {
                    "id": "888",
                    "text": "07:45 HKT tweet (23:45 UTC prev day)",
                    "createdAt": "Tue Nov 10 23:45:00 +0000 2026",
                    "user": {"screenName": "aleabitoreddit", "name": "aleabitoreddit"},
                }
            ],
            "hasMore": False,
        },
        operation="tweets",
        count=50,
        since=win_0730["start_date"],
        until=win_0730["end_date"],
    )
    assert len(bridge_0730["items"]) == 1
    sel_0730 = cli.select_posts(
        {
            "accounts": {
                "aleabitoreddit": {"ok": True, "status": "ok", "data": bridge_0730},
                "jukan05": {"ok": True, "items": []},
                "mingchikuo": {"ok": True, "items": []},
                "citrini": {"ok": True, "items": []},
            },
            "x_search": {"ok": True, "items": []},
        },
        win_0730,
    )
    assert [row["id"] for row in sel_0730["accounts"]["aleabitoreddit"]["items"]] == ["888"]


def test_x_daily_compose_ignores_merged_items_for_legacy_selected() -> None:
    cli = _load(
        "x_daily_legacy_compose_family1",
        SKILLS / "dalton-x-accounts-daily" / "scripts" / "x_accounts_daily.py",
    )
    win = cli.compute_window("2026-11-12T08:30:00+08:00")
    # Exact shape generated by old select_posts (no incomplete / oldest_fetched_at / oldest_returned_at / oldest_created_at)
    # when timeline was empty with has_more=True and x_search merged a post into `items`.
    legacy_selected = {
        "window": win,
        "accounts": {
            "aleabitoreddit": {"ok": True, "has_more": False, "display_handle": "aleabitoreddit", "items": []},
            "jukan05": {
                "ok": True,
                "has_more": True,
                "display_handle": "jukan05",
                "items": [
                    {
                        "id": "777",
                        "url": "https://x.com/jukan05/status/777",
                        "created_at": "2026-11-11T15:00:00+08:00",
                        "text": "supplemented via x_search",
                    }
                ],
            },
            "mingchikuo": {"ok": True, "has_more": False, "display_handle": "mingchikuo", "items": []},
            "citrini": {"ok": True, "has_more": False, "display_handle": "citrini", "items": []},
        },
        "x_search": {"ok": True},
    }
    md = cli.compose_markdown(legacy_selected, {"777": "补充帖"})["markdown"]
    assert "- @jukan05 窗口内可能还有未取回的帖子\n" in md
    assert "2026-11-11 15:00:00" not in md
