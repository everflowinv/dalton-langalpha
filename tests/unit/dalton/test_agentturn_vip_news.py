"""OC-1 VIP news alert: offline filter, card columns, and seen state."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton.database.automation_reports import (
    REPORT_PROFILES,
    automation_report_content_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-vip-news-alert"
SKILL_ROOT = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
CLI_PATH = SKILL_ROOT / "scripts" / "vip_news_alert.py"
AS_OF = "2026-11-12T08:45:00+08:00"
PROFILE = "vip-news-alert"
RUN_ID = "vip-news-alert-2026-11-12"
BANNED = (
    "discord",
    # The skill may call feishu_create_doc for the transcript doc; a delivery
    # target ("feishu:chat:...", "feishu:user:...") must still never appear.
    "feishu:",
    "feishu_send_card",
    ".openclaw",
    "openclaw_workspace",
    "xreach",
    "yt-dlp",
    "chat_id",
    "oc_397207208fb52a0b09969794ef3cd421",
)
BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")
_RECIPIENT_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
YT_QUERIES = (
    "Trump interview today",
    "Trump press conference today",
    "Trump speech today",
    "Scott Bessent interview",
    "Treasury Secretary Bessent",
    "Bessent press conference",
    "Bessent remarks today",
    "Kevin Warsh interview",
    "Kevin Warsh Fed",
    "Kevin Warsh speech",
    "Kevin Warsh remarks",
    "Demis Hassabis interview",
    "Hassabis DeepMind",
    "Hassabis keynote",
    "Hassabis talk",
    "Elon Musk interview today",
    "Elon Musk speech",
    "Elon Musk press conference",
    "Elon Musk remarks today",
    "Sam Altman interview",
    "Sam Altman OpenAI",
    "Sam Altman keynote",
    "Sam Altman talk today",
    "Dario Amodei interview",
    "Amodei Anthropic",
    "Dario Amodei talk",
    "Dario Amodei keynote",
)
WIRE_HANDLES = (
    "DeItaone",
    "CNN",
    "WSJ",
    "Bloomberg",
    "SkyNews",
    "FT",
    "FoxNews",
    "TrumpDailyPosts",
)
_WEB_FORBIDDEN = {
    "count",
    "country",
    "language",
    "domain_filter",
    "search_lang",
    "ui_lang",
    "time_range",
    "topic",
    "freshness",
    "date_after",
    "date_before",
}


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


def _write_json(workspace: Path, relative: str, payload: dict) -> None:
    path = workspace / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _ok(items: list, *, has_more: bool = False) -> dict:
    return {"ok": True, "has_more": has_more, "items": items}


def _tweet(status: str, text: str, when: str, handle: str = "DeItaone") -> dict:
    return {
        "id": status,
        "text": text,
        "created_at": when,
        "author_handle": handle,
        "url": f"https://x.com/{handle}/status/{status}",
    }


def _person(result: dict, key: str) -> dict:
    return next(row for row in result["people"] if row["key"] == key)


def _no_leak(text: str) -> None:
    assert "/Users/" not in text
    assert _RECIPIENT_OC_RE.search(text) is None


def _card_text(card: dict) -> str:
    chunks: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            content = node.get("content")
            if isinstance(content, str):
                chunks.append(content)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(card.get("body", {}).get("elements", []))
    return "\n".join(chunks)


def test_frontmatter_registry_and_profile() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
    )

    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.find("\n---\n", 4)
    meta = yaml.safe_load(text[4:end])
    description = "无人值守的 VIP 动态速递：汇总七位人物过去 24 小时的采访、新闻与社交动态，并用 automation_report 提交卡片。"
    assert meta["name"] == SKILL_NAME
    assert meta["description"] == description
    assert "触发词" not in meta["description"]
    assert len(meta["description"]) <= 120
    spec = (SKILL_NAME, None, ("dalton-bullbear-tracker",))
    assert spec in _DALTON_SKILL_SPECS
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].requires == ("dalton-bullbear-tracker",)
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].command is None
    assert DALTON_SKILL_DEFINITIONS[SKILL_NAME].description == description
    assert PROFILE in REPORT_PROFILES
    body, marker, rule = text.partition("## 撰写硬规则")
    assert marker
    for phrase in BANNED_PROSE:
        assert phrase not in body
        assert phrase in rule
    assert "【撰写硬规则：严禁防御性否定表达】" in rule


def test_skill_tree_has_no_recipient() -> None:
    seen = False
    for path in SKILL_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        seen = True
        text = path.read_text(encoding="utf-8")
        folded = text.casefold()
        for needle in BANNED:
            assert needle not in folded, path
        assert _RECIPIENT_OC_RE.search(text) is None, path
    assert seen


def test_window_plan_and_queries() -> None:
    cli = _load("vip_news_plan", CLI_PATH)
    window = cli.compute_window(AS_OF)
    assert window["timezone"] == "Asia/Hong_Kong"
    assert window["summary_date"] == "2026-11-12"
    assert window["start_date"] == "2026-11-11"
    assert window["end_date"] == "2026-11-12"
    assert window["search_start_date"] == "2026-11-11"
    assert window["search_end_date"] == "2026-11-13"
    assert window["x_from_date"] == "2026-11-11"
    assert window["x_to_date"] == "2026-11-12"
    assert window["report_run_id"] == RUN_ID
    assert cli.compute_window("2026-11-12T08:45:00")["end_iso"] == window["end_iso"]

    plan = cli.plan_requests(AS_OF)
    assert [row["handle"] for row in plan["wires"]] == list(WIRE_HANDLES)
    assert {row["count"] for row in plan["wires"]} == {50}
    assert plan["wires"][0]["since"] == "2026-11-11"
    assert plan["wires"][0]["until"] == "2026-11-12"
    assert plan["web_search"][0]["query"] == '"Donald Trump" (interview OR speech OR says OR remarks)'
    assert plan["web_search"][0]["start_date"] == "2026-11-11"
    assert plan["web_search"][0]["end_date"] == "2026-11-13"
    for row in plan["web_search"] + plan["youtube"]:
        assert _WEB_FORBIDDEN.isdisjoint(row)
        assert set(row) == {"person", "query", "start_date", "end_date"}
    queries = [row["query"] for row in plan["youtube"]]
    assert len(queries) == 27
    assert [query[: -len(" site:youtube.com")] for query in queries] == list(YT_QUERIES)
    search = plan["x_search"]
    assert search["enable_image_understanding"] is True
    assert search["enable_video_understanding"] is True
    assert "handles" not in search
    assert search["from_date"] == "2026-11-11"
    assert search["to_date"] == "2026-11-12"
    assert "Donald Trump" in search["query"]
    assert "Dario Amodei" in search["query"]


def test_documented_commands_run_offline(tmp_path: Path) -> None:
    cli = _load("vip_news_documented", CLI_PATH)
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    commands = _bash_commands(text)
    replacements = {"<workspace>": str(tmp_path)}
    seen: list[str] = []
    for command in commands:
        argv = _argv_from(command, replacements)
        assert argv[0] == "--json"
        assert argv[2] == "--workspace-dir"
        assert argv[3] == str(tmp_path)
        seen.append(argv[1])
        if argv[1] == "filter":
            end_iso = argv[argv.index("--as-of") + 1]
            _write_json(tmp_path, argv[argv.index("--input") + 1], {
                "wires": {
                    "DeItaone": _ok([
                        _tweet("42", "TRUMP spoke at the white house", end_iso),
                    ]),
                },
            })
        if argv[1] == "render":
            summary_date = replacements["<summary_date>"]
            _write_json(tmp_path, argv[argv.index("--events") + 1], {
                "summary_date": summary_date,
                "people": [{
                    "key": "Trump",
                    "headlines": [{
                        "summary": "特朗普发表讲话",
                        "source": "DeItaone",
                        "title": "TRUMP spoke at the white house",
                        "url": "https://x.com/DeItaone/status/42",
                    }],
                }],
            })
        if argv[1] == "commit-seen":
            summary_date = replacements["<summary_date>"]
            assert not (tmp_path / "state" / "vip-news-alert" / "seen.json").exists()
            _write_json(tmp_path, argv[argv.index("--confirm") + 1], {
                "ok": True,
                "delivery_confirmed": True,
                "status": "sent",
                "content_kind": "cards",
                "report_run_id": f"vip-news-alert-{summary_date}",
            })
        code, payload = _run(cli, argv)
        assert code == 0 and payload["ok"] is True, payload
        _no_leak(json.dumps(payload, ensure_ascii=False))
        if argv[1] == "window":
            replacements["<end_iso>"] = payload["end_iso"]
            replacements["<summary_date>"] = payload["summary_date"]
            assert payload["report_run_id"] == f"vip-news-alert-{payload['summary_date']}"
        if argv[1] == "self-test":
            assert {row["name"] for row in payload["checks"]} >= {
                "vips",
                "wires",
                "seen_path",
                "renderer",
            }
        if argv[1] == "list-vips":
            assert [row["key"] for row in payload["vips"]] == [
                "Trump",
                "Bessent",
                "Warsh",
                "Demis",
                "Elon",
                "Sam",
                "Dario",
            ]
        if argv[1] == "plan":
            assert len(payload["wires"]) == 8
            assert len(payload["youtube"]) == 27
            assert "handles" not in payload["x_search"]
            assert payload["x_search"]["enable_image_understanding"] is True
            assert payload["x_search"]["enable_video_understanding"] is True
        if argv[1] == "filter":
            assert payload["counts"]["wire"] == 1
            assert payload["sources_all_failed"] is False
        if argv[1] == "render":
            assert payload["empty"] is False
            markdown = (tmp_path / payload["markdown"]).read_text(encoding="utf-8")
            assert "[source: *DeItaone]" in markdown
            assert "https://x.com/DeItaone/status/42" not in markdown
            _no_leak(markdown)
        if argv[1] == "commit-seen":
            seen_path = tmp_path / "state" / "vip-news-alert" / "seen.json"
            saved = json.loads(seen_path.read_text(encoding="utf-8"))
            assert payload["added"] == 2
            assert len(saved) == 2
            _no_leak(seen_path.read_text(encoding="utf-8"))
    assert seen == [
        "window",
        "self-test",
        "list-vips",
        "plan",
        "filter",
        "render",
        "commit-seen",
    ]


def test_exact_window_dedup_and_duration() -> None:
    cli = _load("vip_news_filter", CLI_PATH)
    window = cli.compute_window(AS_OF)
    start = datetime.fromisoformat(window["start_iso"])
    end = datetime.fromisoformat(window["end_iso"])
    start_iso = window["start_iso"]
    end_iso = window["end_iso"]
    early = (start - timedelta(seconds=1)).isoformat()
    late = (end + timedelta(seconds=1)).isoformat()
    cutoff = (end - timedelta(days=7)).isoformat()
    fresh_seen = (end - timedelta(days=1)).isoformat()
    blocked_url = "https://x.com/DeItaone/status/77"
    expired_url = "https://x.com/DeItaone/status/78"
    raw = {
        "window": {"start_iso": "2000-01-01T00:00:00+00:00", "end_iso": "2099-01-01T00:00:00+00:00"},
        "wires": {
            "DeItaone": _ok([
                _tweet("1", "TRUMP at the exact boundary", start_iso),
                _tweet("2", "TRUMP one second early", early),
                _tweet("3", "TRUMP at the window end", end_iso),
                _tweet("4", "TRUMP one second late", late),
                _tweet("5", "TRUMP naive utc boundary", "2026-11-11T00:45:00"),
                {
                    "id": "6",
                    "text": "TRUMP twitter clock",
                    "createdAt": "Wed Nov 11 00:45:00 +0000 2026",
                    "user": {"screenName": "DeItaone"},
                },
                {
                    "id": "7",
                    "text": "TRUMP twitter early",
                    "createdAt": "Wed Nov 11 00:44:59 +0000 2026",
                    "user": {"screenName": "DeItaone"},
                },
                _tweet("8", "TRUMP date only early", "2026-11-11"),
                _tweet("9", "TRUMP met MUSK", end_iso),
                _tweet("10", "TRUMP duplicate status", end_iso),
                {**_tweet("10", "TRUMP another wording", end_iso), "id": "10"},
                _tweet("77", "TRUMP seen by url", end_iso),
                _tweet("78", "TRUMP expired seen", end_iso),
                _tweet("79", "TRUMP repeated headline", end_iso),
                _tweet("80", "TRUMP stale seen title", end_iso),
                {
                    "id": "81",
                    "text": "",
                    "created_at": end_iso,
                    "author_handle": "DeItaone",
                    "url": "https://x.com/DeItaone/status/81",
                },
                {
                    "id": "82",
                    "text": "TRUMP no clock",
                    "author_handle": "DeItaone",
                    "url": "https://x.com/DeItaone/status/82",
                },
            ]),
            "CNN": _ok([_tweet("20", "market update", "2026-11-11T12:00:00+08:00", "CNN")], has_more=True),
            "Bloomberg": _ok([_tweet("30", "MUSK launched a rocket", end_iso, "Bloomberg")]),
        },
        "web": {
            "Trump": _ok([
                {"url": "https://example.com/same", "title": "First title", "date": end_iso},
                {"url": "https://example.com/same", "title": "Second title", "date": end_iso},
                {"url": "https://example.com/p1", "title": "Trump Speaks!", "date": end_iso},
                {"url": "https://example.com/p2", "title": "trump speaks", "date": end_iso},
                {"url": "https://example.com/dated", "title": "Dated page", "published_date": end_iso},
                {"url": "https://example.com/day", "title": "Date only", "date": "2026-11-11"},
                {"url": "https://example.com/none", "title": "No clock", "content": "TRUMP"},
                {"url": "https://example.com/grounded", "title": "Grounded page"},
                {"url": "https://example.com/old-page", "title": "Old page", "date": early},
                {
                    "url": "https://www.youtube.com/watch?v=sharedvid01",
                    "title": "Web copy",
                    "date": end_iso,
                },
                {"url": "https://youtu.be/webonlyvid1", "title": "From the web"},
            ]),
            "Bessent": _ok([
                {"url": "https://example.com/bessent", "title": "market recap", "date": end_iso},
            ]),
        },
        "videos": {
            "Trump": _ok([
                {"url": "https://youtu.be/abcdefghijk", "title": "Talk one", "date": end_iso},
                {
                    "url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "title": "Talk two",
                    "date": end_iso,
                },
                {
                    "url": "https://www.youtube.com/watch?v=sharedvid01",
                    "title": "Video copy",
                    "date": end_iso,
                },
                {"url": "https://www.youtube.com/watch?v=groundedvid", "title": "Grounded clip"},
                {
                    "url": "https://www.youtube.com/watch?v=oldvideo001",
                    "title": "Old video",
                    "date": early,
                },
            ]),
            "Elon": _ok([
                {"url": "https://www.youtube.com/watch?v=shortclip01", "title": "short", "date": end_iso, "duration": 119},
                {"url": "https://www.youtube.com/watch?v=longclip001", "title": "long", "date": end_iso, "duration": 120},
                {"url": "https://www.youtube.com/watch?v=zeroclip001", "title": "zero", "date": end_iso, "duration": 0},
                {"url": "https://www.youtube.com/watch?v=noduration1", "title": "missing", "date": end_iso},
                {"url": "https://www.youtube.com/watch?v=boolclip01", "title": "flag", "date": end_iso, "duration": True},
                {"url": "https://www.youtube.com/watch?v=undatedshr1", "title": "undated short", "duration": 30},
            ]),
        },
        "x_search": _ok([
            {
                "author": "@realDonaldTrump",
                "url": "https://x.com/realDonaldTrump/status/501",
                "date": end_iso,
                "title": "@realDonaldTrump (2026-11-12)",
                "summary_excerpt": "TRUMP first remark",
            },
            {
                "author": "@realDonaldTrump",
                "url": "https://x.com/realDonaldTrump/status/502",
                "date": end_iso,
                "title": "@realDonaldTrump (2026-11-12)",
                "summary_excerpt": "TRUMP second remark",
            },
            {
                "author": "@WhiteHouse",
                "url": "https://x.com/WhiteHouse/status/700",
                "date": end_iso,
                "summary_excerpt": "schedule update",
                "title": "@WhiteHouse (2026-11-12)",
            },
            {
                "url": "https://x.com/WhiteHouse/status/701",
                "date": end_iso,
                "summary_excerpt": "TRUMP without an author",
            },
            {
                "author": "@realDonaldTrump",
                "url": "https://x.com/realDonaldTrump/status/599",
                "summary_excerpt": "TRUMP undated remark",
                "title": "tool title",
            },
        ]),
    }
    seen = {
        cli.url_hash(blocked_url): {"ts": fresh_seen, "url": blocked_url, "title": "x"},
        cli.url_hash(expired_url): {"ts": cutoff, "url": expired_url, "title": "x"},
        cli.title_hash("TRUMP repeated headline"): {"ts": fresh_seen, "title": "TRUMP repeated headline"},
        cli.title_hash("TRUMP stale seen title"): {"ts": "yesterday", "title": "TRUMP stale seen title"},
    }
    result = cli.filter_raw(raw, window, seen)
    trump = _person(result, "Trump")
    elon = _person(result, "Elon")
    bessent = _person(result, "Bessent")
    wire_ids = [row["status_id"] for row in trump["wire_headlines"]]
    assert wire_ids == ["1", "3", "5", "6", "9", "10", "78", "80"]
    assert [row["status_id"] for row in elon["wire_headlines"]] == ["30"]
    assert all(row["key"] != "Elon" or row["wire_headlines"][0]["text"] == "MUSK launched a rocket" for row in result["people"])
    assert "TRUMP met MUSK" not in [row["text"] for row in elon["wire_headlines"]]
    web_titles = [row["title"] for row in trump["web_articles"]]
    assert web_titles == ["First title", "Trump Speaks!", "Dated page", "No clock", "Grounded page"]
    assert "Date only" not in web_titles
    assert "Old page" not in web_titles
    assert all("youtube.com" not in row["url"] and "youtu.be" not in row["url"] for row in trump["web_articles"])
    unverified_web = {row["title"]: row for row in trump["web_articles"] if row.get("time_unverified") is True}
    assert set(unverified_web) == {"No clock", "Grounded page"}
    assert "created_at" not in unverified_web["Grounded page"]
    assert "time_unverified" not in trump["web_articles"][0]
    assert [row["title"] for row in bessent["web_articles"]] == ["market recap"]
    assert [row["url"] for row in trump["videos"]] == [
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://www.youtube.com/watch?v=sharedvid01",
        "https://www.youtube.com/watch?v=groundedvid",
        "https://www.youtube.com/watch?v=webonlyvid1",
    ]
    assert [row["title"] for row in trump["videos"]] == [
        "Talk one",
        "Video copy",
        "Grounded clip",
        "From the web",
    ]
    assert trump["videos"][2]["time_unverified"] is True
    assert trump["videos"][3]["time_unverified"] is True
    assert "created_at" not in trump["videos"][2]
    assert "Old video" not in [row["title"] for row in trump["videos"]]
    empty_wire = cli._normalize_wire(
        {
            "id": "81",
            "text": "",
            "created_at": end_iso,
            "url": "https://x.com/DeItaone/status/81",
        },
        "DeItaone",
    )
    assert empty_wire is not None and empty_wire["text"] == ""
    assert cli._normalize_wire({"id": "82", "text": "TRUMP no clock"}, "DeItaone") is None
    elon_videos = {row["title"]: row for row in elon["videos"]}
    assert set(elon_videos) == {"long", "zero", "missing", "flag"}
    assert elon_videos["long"]["duration"] == 120.0
    assert elon_videos["zero"]["duration"] == 0.0
    assert "duration" not in elon_videos["flag"]
    excerpts = [row["text"] for row in trump["x_posts"]]
    assert excerpts == ["TRUMP first remark", "TRUMP second remark"]
    assert "TRUMP undated remark" not in excerpts
    assert "81" not in wire_ids
    assert "82" not in wire_ids
    assert [row["text"] for row in result["unmatched_x"]] == ["schedule update"]
    assert result["sources_all_failed"] is False
    assert "CNN 窗口内可能还有未取回的帖子，最早只取到 2026-11-11 12:00:00（香港时间）" in result["notes"]
    assert "WSJ 时间线没有返回" in result["notes"]
    assert "Kevin Warsh 的视频检索没有返回" in result["notes"]
    assert cli.filter_raw({}, window, {})["sources_all_failed"] is True
    quiet = cli.filter_raw({"wires": {"DeItaone": _ok([])}}, window, {})
    assert quiet["sources_all_failed"] is False
    assert cli.filter_raw({}, window, {}, person="Trump")["sources_all_failed"] is False


def test_seen_is_written_only_after_confirmed_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load("vip_news_seen", CLI_PATH)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    moment = datetime.fromisoformat(AS_OF)
    cutoff = moment - timedelta(days=7)
    events = workspace / "events.json"
    confirm = workspace / "confirm.json"
    seen_path = workspace / "state" / "vip-news-alert" / "seen.json"
    headline = {
        "summary_date": "2026-11-12",
        "people": [{
            "key": "Trump",
            "headlines": [{
                "summary": "讲话",
                "source": "DeItaone",
                "title": "TRUMP spoke today",
                "url": "https://x.com/DeItaone/status/900",
            }],
        }],
    }
    good = {
        "ok": True,
        "delivery_confirmed": True,
        "status": "sent",
        "content_kind": "cards",
        "report_run_id": RUN_ID,
    }
    real_save = cli.save_seen
    fake_save = create_autospec(real_save)
    monkeypatch.setattr(cli, "save_seen", fake_save)
    _write_json(workspace, "events.json", {"summary_date": "2026-11-12", "people": []})
    _write_json(workspace, "confirm.json", good)
    code, payload = _run(cli, [
        "--json", "commit-seen", "--workspace-dir", str(workspace),
        "--confirm", "confirm.json", "--as-of", AS_OF,
    ])
    assert code == 1
    assert payload["error"] == payload["message"] == "nothing was sent"
    assert not seen_path.exists()
    fake_save.assert_not_called()

    seed = {
        "exact": {"ts": cutoff.isoformat(), "url": "https://example.com/old", "title": "old"},
        "fresh": {
            "ts": (cutoff + timedelta(seconds=1)).isoformat(),
            "url": "https://example.com/fresh",
            "title": "fresh",
        },
        "legacy": int(moment.timestamp()),
        "bad": {"ts": "nope"},
        "flag": True,
    }
    _write_json(workspace, "state/vip-news-alert/seen.json", seed)
    before = seen_path.read_bytes()
    _write_json(workspace, "events.json", headline)
    _write_json(workspace, "confirm.json", {**good, "content_kind": "failure_text"})
    code, payload = _run(cli, [
        "--json", "commit-seen", "--workspace-dir", str(workspace),
        "--confirm", "confirm.json", "--as-of", AS_OF,
    ])
    assert code == 1
    assert payload["error"] == payload["message"] == "delivery not confirmed"
    assert seen_path.read_bytes() == before
    fake_save.assert_not_called()
    monkeypatch.setattr(cli, "save_seen", real_save)
    code, rendered = _run(cli, [
        "--json", "render", "--workspace-dir", str(workspace),
        "--events", "events.json",
        "--markdown", "temp/vip-news-alert/2026-11-12/card.md",
        "--output", "temp/vip-news-alert/2026-11-12/card.json",
        "--as-of", AS_OF,
    ])
    assert code == 0 and rendered["empty"] is False, rendered

    _write_json(workspace, "confirm.json", good)
    code, payload = _run(cli, [
        "--json", "commit-seen", "--workspace-dir", str(workspace),
        "--confirm", "confirm.json", "--as-of", AS_OF,
    ])
    assert code == 0 and payload["ok"] is True, payload
    saved = json.loads(seen_path.read_text(encoding="utf-8"))
    url_key = cli.url_hash("https://x.com/DeItaone/status/900")
    title_key = cli.title_hash("TRUMP spoke today")
    assert "exact" not in saved
    assert "bad" not in saved
    assert "flag" not in saved
    assert "fresh" in saved
    assert "legacy" in saved
    assert saved[url_key]["kind"] == "url"
    assert saved[title_key]["kind"] == "title"
    assert payload["added"] == 2
    assert payload["kept"] == 4

    code, payload = _run(cli, [
        "--json", "commit-seen", "--workspace-dir", str(workspace),
        "--confirm", "confirm.json", "--as-of", AS_OF,
    ])
    assert code == 0 and payload["added"] == 0 and payload["kept"] == 4
    assert events.is_file() and confirm.is_file()


def test_commit_seen_follows_the_rendered_manifest(tmp_path: Path) -> None:
    cli = _load("vip_news_manifest", CLI_PATH)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    headline_url = "https://x.com/DeItaone/status/900"
    headline_title = "TRUMP spoke today"
    extra_url = "https://x.com/DeItaone/status/901"
    extra_title = "TRUMP outside the card"
    headline = {
        "summary_date": "2026-11-12",
        "people": [{
            "key": "Trump",
            "headlines": [{
                "summary": "讲话",
                "source": "DeItaone",
                "title": headline_title,
                "url": headline_url,
            }],
        }],
    }
    good = {
        "ok": True,
        "delivery_confirmed": True,
        "status": "sent",
        "content_kind": "cards",
        "report_run_id": RUN_ID,
    }
    render_argv = [
        "--json", "render", "--workspace-dir", str(workspace),
        "--events", "events.json",
        "--markdown", "temp/vip-news-alert/2026-11-12/card.md",
        "--output", "temp/vip-news-alert/2026-11-12/card.json",
        "--as-of", AS_OF,
    ]
    commit_argv = [
        "--json", "commit-seen", "--workspace-dir", str(workspace),
        "--confirm", "confirm.json", "--as-of", AS_OF,
    ]
    _write_json(workspace, "events.json", headline)
    code, payload = _run(cli, render_argv)
    assert code == 0 and payload["empty"] is False, payload
    card_path = workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.json"
    manifest_path = workspace / "temp" / "vip-news-alert" / "2026-11-12" / "manifest.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = automation_report_content_sha256(
        profile=PROFILE,
        content_kind="cards",
        cards=[card],
        failure_text=None,
    )
    assert manifest["content_sha256"] == digest
    assert manifest["report_run_id"] == RUN_ID
    manifest_keys = [row["key"] for row in manifest["seen_keys"]]
    assert cli.url_hash(headline_url) in manifest_keys
    assert cli.title_hash(headline_title) in manifest_keys
    seen_path = workspace / "state" / "vip-news-alert" / "seen.json"
    other = automation_report_content_sha256(
        profile=PROFILE,
        content_kind="cards",
        cards=[{"schema": "2.0"}],
        failure_text=None,
    )
    assert other != digest
    _write_json(workspace, "confirm.json", {**good, "content_sha256": other})
    code, payload = _run(cli, commit_argv)
    assert code == 1
    assert payload["error"] == payload["message"] == "card digest mismatch"
    assert not seen_path.exists()

    original_card = card_path.read_bytes()
    card_path.write_text('{"schema": "2.0"}\n', encoding="utf-8")
    _write_json(workspace, "confirm.json", good)
    code, payload = _run(cli, commit_argv)
    assert code == 1
    assert payload["error"] == payload["message"] == "card digest mismatch"
    assert not seen_path.exists()
    card_path.write_bytes(original_card)

    wider = json.loads(json.dumps(headline))
    wider["people"][0]["headlines"].append({
        "summary": "另一条",
        "source": "DeItaone",
        "title": extra_title,
        "url": extra_url,
    })
    _write_json(workspace, "events.json", wider)
    code, payload = _run(cli, commit_argv)
    assert code == 0 and payload["added"] == 2, payload
    saved = json.loads(seen_path.read_text(encoding="utf-8"))
    assert cli.url_hash(extra_url) not in saved
    assert cli.title_hash(extra_title) not in saved
    assert cli.url_hash(headline_url) in saved
    assert cli.title_hash(headline_title) in saved

    card_path.unlink()
    (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.md").unlink()
    transcript = workspace / "research-outputs" / "vip-news-alert" / "2026-11-12-transcripts.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("全文\n", encoding="utf-8")
    interview_url = "https://www.youtube.com/watch?v=abcdefghijk"
    _write_json(workspace, "events.json", {
        "summary_date": "2026-11-12",
        "people": [{
            "key": "Trump",
            "interviews": [{
                "summary": "采访摘要",
                "url": interview_url,
                "title": "Trump interview",
                "points": ["要点一", "要点二", "要点三"],
            }],
        }],
    })
    code, payload = _run(cli, render_argv)
    assert code == 0 and payload["ok"] is True, payload
    transcript.unlink()
    code, payload = _run(cli, commit_argv)
    assert code == 0 and payload["ok"] is True, payload
    saved = json.loads(seen_path.read_text(encoding="utf-8"))
    assert cli.url_hash(interview_url) in saved
    assert cli.title_hash("Trump interview") in saved
    assert cli.url_hash(extra_url) not in saved


def test_filter_reads_seen_and_tolerates_corrupt_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli = _load("vip_news_seen_read", CLI_PATH)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    blocked = "https://x.com/DeItaone/status/100"
    real_load = cli.load_seen

    def _load_seen(workspace_path: Path) -> dict:
        return {
            cli.url_hash(blocked): {
                "url": blocked,
                "title": "TRUMP old line",
                "ts": AS_OF,
                "kind": "url",
            },
        }

    fake = create_autospec(real_load, side_effect=_load_seen)
    monkeypatch.setattr(cli, "load_seen", fake)
    _write_json(workspace, "raw.json", {
        "wires": {"DeItaone": _ok([
            _tweet("100", "TRUMP old line", AS_OF),
            _tweet("101", "TRUMP fresh line", AS_OF),
        ])},
    })
    code, payload = _run(cli, [
        "--json", "filter", "--workspace-dir", str(workspace),
        "--input", "raw.json", "--output", "filtered.json", "--as-of", AS_OF,
    ])
    assert code == 0 and payload["ok"] is True, payload
    fake.assert_called_once_with(workspace.resolve())
    filtered = json.loads((workspace / "filtered.json").read_text(encoding="utf-8"))
    assert [row["status_id"] for row in _person(filtered, "Trump")["wire_headlines"]] == ["101"]

    monkeypatch.setattr(cli, "load_seen", real_load)
    seen_path = workspace / "state" / "vip-news-alert" / "seen.json"
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text("{", encoding="utf-8")
    assert cli.load_seen(workspace.resolve()) == {}
    code, payload = _run(cli, [
        "--json", "filter", "--workspace-dir", str(workspace),
        "--input", "raw.json", "--output", "filtered.json", "--as-of", AS_OF,
    ])
    assert code == 0 and payload["counts"]["wire"] == 2


def test_card_column_order_reuse_and_empty(tmp_path: Path) -> None:
    cli = _load("vip_news_card", CLI_PATH)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    transcript = workspace / "research-outputs" / "vip-news-alert" / "2026-11-12-transcripts.md"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(" \n", encoding="utf-8")
    events = {
        "summary_date": "2026-11-12",
        "source_notes": ["CNN 时间线没有返回"],
        "people": [
            {
                "key": "Bessent",
                "articles": [{
                    "summary": "财政表态",
                    "url": "https://example.com/bessent",
                    "title": "Bessent remarks",
                }],
            },
            {
                "key": "Trump",
                "interviews": [{
                    "summary": "采访摘要",
                    "url": "https://www.youtube.com/watch?v=abcdefghijk",
                    "title": "Trump interview",
                    "points": ["要点一", "要点二", "要点三"],
                }],
                "articles": [{
                    "summary": "报道摘要",
                    "url": "https://example.com/trump",
                    "title": "Trump article",
                }],
                "headlines": [{
                    "summary": "动态摘要",
                    "source": "DeItaone",
                    "title": "TRUMP spoke",
                    "url": "https://x.com/DeItaone/status/1",
                }],
            },
        ],
    }
    _write_json(workspace, "events.json", events)
    argv = [
        "--json", "render", "--workspace-dir", str(workspace),
        "--events", "events.json", "--markdown", "temp/vip-news-alert/2026-11-12/card.md",
        "--output", "temp/vip-news-alert/2026-11-12/card.json", "--as-of", AS_OF,
    ]
    code, payload = _run(cli, argv)
    assert code == 1
    assert "transcript file missing" in payload["error"]
    assert not (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.json").exists()

    transcript.write_text(
        "# VIP 采访记录 — 2026-11-12\n\n### 核心要点\n- 文件要点六\n",
        encoding="utf-8",
    )
    before_transcript = transcript.read_bytes()
    code, payload = _run(cli, argv)
    assert code == 0 and payload["empty"] is False, payload
    assert transcript.read_bytes() == before_transcript
    markdown = (workspace / payload["markdown"]).read_text(encoding="utf-8")
    card = json.loads((workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.json").read_text(encoding="utf-8"))
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "📡 VIP 动态速递 — 2026-11-12"
    assert card["header"]["subtitle"]["content"] == "2026-11-12 · 严格 24 小时"
    trump_at = markdown.index("## ◎ Donald Trump（3条动态）")
    bessent_at = markdown.index("## ◎ Scott Bessent（1条动态）")
    assert trump_at < bessent_at
    assert markdown.index(cli.LABEL_INTERVIEW) < markdown.index(cli.LABEL_ARTICLE) < markdown.index(cli.LABEL_HEADLINE)
    assert markdown.index(cli.LABEL_HEADLINE) < bessent_at
    assert "[source: *DeItaone]" in markdown
    assert "https://x.com/DeItaone/status/1" not in markdown
    assert "https://www.youtube.com/watch?v=abcdefghijk" in markdown
    assert "https://example.com/trump" in markdown
    assert "https://example.com/bessent" in markdown
    record_at = markdown.index("## 完整采访记录")
    sentence = "完整记录已存工作区 research-outputs/vip-news-alert/2026-11-12-transcripts.md"
    assert record_at > bessent_at
    assert markdown.index("**Donald Trump：Trump interview**") > record_at
    assert markdown.index("- 要点一") < markdown.index("- 要点二") < markdown.index("- 要点三")
    assert "文件要点六" not in markdown
    assert markdown.index(sentence) > record_at
    assert markdown.index("## 来源") > markdown.index(sentence)
    assert "\n###" not in markdown
    rendered = _card_text(card)
    assert rendered.index("Donald Trump") < rendered.index("Scott Bessent")
    assert rendered.index("采访/演讲") < rendered.index("新闻报道") < rendered.index("动态:")
    assert rendered.index("完整采访记录") < rendered.index(sentence)
    assert rendered.index("来源") > rendered.index(sentence)
    _no_leak(markdown)
    first_card = (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.json").read_bytes()
    first_md = (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.md").read_bytes()

    code, again = _run(cli, argv)
    assert code == 0 and again["reused"] is True

    changed = json.loads(json.dumps(events))
    changed["people"][1]["interviews"][0]["summary"] = "另一句摘要"
    _write_json(workspace, "events.json", changed)
    code, conflict = _run(cli, argv)
    assert code == 1
    assert conflict["error"] == "card_conflict"
    assert conflict["message"]
    assert (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.json").read_bytes() == first_card
    assert (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "card.md").read_bytes() == first_md

    _write_json(workspace, "events.json", events)
    code, restored = _run(cli, argv)
    assert code == 0 and restored["reused"] is True

    stale = workspace / "temp" / "vip-news-alert" / "2026-11-12" / "empty.json"
    stale.write_bytes(b"STALE\n")
    _write_json(workspace, "empty-events.json", {"summary_date": "2026-11-12", "people": []})
    code, empty = _run(cli, [
        "--json", "render", "--workspace-dir", str(workspace),
        "--events", "empty-events.json",
        "--markdown", "temp/vip-news-alert/2026-11-12/empty.md",
        "--output", "temp/vip-news-alert/2026-11-12/empty.json",
        "--as-of", AS_OF,
    ])
    assert code == 0 and empty["empty"] is True
    assert stale.read_bytes() == b"STALE\n"
    assert not (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "empty.md").exists()

    short = json.loads(json.dumps(events))
    short["people"][1]["interviews"][0]["points"] = ["只有一条", "只有两条"]
    _write_json(workspace, "events.json", short)
    code, bad_points = _run(cli, [
        "--json", "render", "--workspace-dir", str(workspace),
        "--events", "events.json",
        "--markdown", "temp/vip-news-alert/2026-11-12/bad.md",
        "--output", "temp/vip-news-alert/2026-11-12/bad.json",
        "--as-of", AS_OF,
    ])
    assert code == 1
    assert "transcript points must be 3" in bad_points["error"]
    assert not (workspace / "temp" / "vip-news-alert" / "2026-11-12" / "bad.json").exists()


def test_paths_do_not_escape(tmp_path: Path) -> None:
    cli = _load("vip_news_paths", CLI_PATH)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("sentinel", encoding="utf-8")
    _write_json(workspace, "raw.json", {"wires": {"DeItaone": _ok([_tweet("1", "TRUMP", AS_OF)])}})

    code, payload = _run(cli, [
        "--json", "filter", "--workspace-dir", str(workspace),
        "--input", "raw.json", "--output", "../escaped.json", "--as-of", AS_OF,
    ])
    assert code == 1
    assert payload["error"] == payload["message"]
    assert "path must stay inside the workspace" in payload["error"]
    assert not (tmp_path / "escaped.json").exists()

    code, payload = _run(cli, [
        "--json", "filter", "--workspace-dir", str(workspace),
        "--input", "raw.json", "--output", str(outside / "absolute.json"), "--as-of", AS_OF,
    ])
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert not (outside / "absolute.json").exists()

    linked = workspace / "linked.json"
    os.symlink(outside / "sentinel", linked)
    code, payload = _run(cli, [
        "--json", "filter", "--workspace-dir", str(workspace),
        "--input", "raw.json", "--output", "linked.json", "--as-of", AS_OF,
    ])
    assert code == 1
    assert "refusing symlink" in payload["error"]
    assert (outside / "sentinel").read_text(encoding="utf-8") == "sentinel"

    temp_outside = tmp_path / "temp-outside"
    temp_outside.mkdir()
    os.symlink(temp_outside, workspace / "temp")
    code, payload = _run(cli, [
        "--json", "filter", "--workspace-dir", str(workspace),
        "--input", "raw.json",
        "--output", "temp/vip-news-alert/2026-11-12/filtered.json",
        "--as-of", AS_OF,
    ])
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(temp_outside.rglob("*")) == []

    state_workspace = tmp_path / "state-ws"
    state_outside = tmp_path / "state-outside"
    state_workspace.mkdir()
    state_outside.mkdir()
    os.symlink(state_outside, state_workspace / "state")
    _write_json(state_workspace, "events.json", {
        "summary_date": "2026-11-12",
        "people": [{
            "key": "Trump",
            "headlines": [{
                "summary": "讲话",
                "source": "DeItaone",
                "title": "TRUMP spoke",
                "url": "https://x.com/DeItaone/status/5",
            }],
        }],
    })
    _write_json(state_workspace, "confirm.json", {
        "ok": True,
        "delivery_confirmed": True,
        "status": "sent",
        "content_kind": "cards",
        "report_run_id": RUN_ID,
    })
    code, rendered = _run(cli, [
        "--json", "render", "--workspace-dir", str(state_workspace),
        "--events", "events.json",
        "--markdown", "temp/vip-news-alert/2026-11-12/card.md",
        "--output", "temp/vip-news-alert/2026-11-12/card.json",
        "--as-of", AS_OF,
    ])
    assert code == 0 and rendered["ok"] is True, rendered
    code, payload = _run(cli, [
        "--json", "commit-seen", "--workspace-dir", str(state_workspace),
        "--confirm", "confirm.json", "--as-of", AS_OF,
    ])
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(state_outside.rglob("*")) == []

    notes_workspace = tmp_path / "notes-ws"
    notes_outside = tmp_path / "notes-outside"
    notes_workspace.mkdir()
    notes_outside.mkdir()
    os.symlink(notes_outside, notes_workspace / "research-outputs")
    _write_json(notes_workspace, "events.json", {
        "summary_date": "2026-11-12",
        "people": [{
            "key": "Trump",
            "interviews": [{
                "summary": "采访",
                "url": "https://www.youtube.com/watch?v=abcdefghijk",
                "title": "Trump interview",
                "points": ["要点一", "要点二", "要点三"],
            }],
        }],
    })
    code, payload = _run(cli, [
        "--json", "render", "--workspace-dir", str(notes_workspace),
        "--events", "events.json",
        "--markdown", "card.md",
        "--output", "card.json",
        "--as-of", AS_OF,
    ])
    assert code == 1
    assert "path must stay inside the workspace" in payload["error"]
    assert list(notes_outside.rglob("*")) == []
    assert not (notes_workspace / "card.json").exists()


def test_a_youtube_link_from_web_search_beats_a_same_title_article() -> None:
    cli = _load("vip_news_video_first", CLI_PATH)
    window = cli.compute_window(AS_OF)
    raw = {
        "web": {
            "Trump": _ok([
                {"url": "https://example.com/report", "title": "Trump interview"},
                {"url": "https://www.youtube.com/watch?v=interview01", "title": "Trump interview"},
            ]),
        },
    }
    result = cli.filter_raw(raw, window, {}, person="Trump")
    trump = _person(result, "Trump")
    assert [video["url"] for video in trump["videos"]] == ["https://www.youtube.com/watch?v=interview01"]
    assert trump["web_articles"] == []


def test_the_card_links_the_feishu_transcript_doc_when_one_was_made(tmp_path: Path) -> None:
    cli = _load("vip_news_doc_link", CLI_PATH)
    interview = {
        "summary": "采访摘要",
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "title": "Trump interview",
        "points": ["要点一", "要点二", "要点三"],
    }
    workspace_line = "完整记录已存工作区 research-outputs/vip-news-alert/2026-11-12-transcripts.md"
    cases = (
        # The bridge's success shape: document_id, no url (feishu_doc_url builds the link).
        ({"ok": True, "document_id": "DoxAbCdEf123456", "title": "VIP 采访记录 — 2026-11-12"},
         "📝 完整采访记录：https://feishu.cn/docx/DoxAbCdEf123456"),
        ({"ok": True, "document_id": "DoxAbCdEf123456", "space_id": "7000", "node_token": "WikNode1234567",
          "warning": "share failed"},
         "📝 完整采访记录：https://feishu.cn/wiki/WikNode1234567"),
        ({"ok": False, "error": "content_insert_failed", "document_id": "DoxAbCdEf123456"}, workspace_line),
        ({"ok": True, "document_id": "../../etc"}, workspace_line),
        (None, workspace_line),
    )
    for index, (record, expected) in enumerate(cases):
        workspace = tmp_path / f"ws-{index}"
        transcript = workspace / "research-outputs" / "vip-news-alert" / "2026-11-12-transcripts.md"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("# VIP 采访记录 — 2026-11-12\n\n### 核心要点\n- 要点\n", encoding="utf-8")
        if record is not None:
            _write_json(workspace, "temp/vip-news-alert/2026-11-12/doc.json", record)
        events = {"summary_date": "2026-11-12", "people": [{"key": "Trump", "interviews": [interview]}]}
        _write_json(workspace, "events.json", events)
        code, payload = _run(cli, [
            "--json", "render", "--workspace-dir", str(workspace),
            "--events", "events.json", "--markdown", "temp/vip-news-alert/2026-11-12/card.md",
            "--output", "temp/vip-news-alert/2026-11-12/card.json", "--as-of", AS_OF,
        ])
        assert code == 0, payload
        markdown = (workspace / payload["markdown"]).read_text(encoding="utf-8")
        assert expected in markdown, (record, markdown)
        if expected != workspace_line:
            assert workspace_line not in markdown


def test_wire_has_more_checks_oldest_timestamp() -> None:
    cli = _load("vip_news_has_more", CLI_PATH)
    window = cli.compute_window(AS_OF)
    start = datetime.fromisoformat(window["start_iso"])
    earlier = (start - timedelta(seconds=1)).isoformat()

    raw = {
        "wires": {
            # has_more=True, oldest item is earlier than window start -> covered, no note
            "DeItaone": _ok([
                {"id": "10", "text": "undated entry", "author_handle": "DeItaone"},
                _tweet("11", "TRUMP spoke", "2026-11-11T04:00:00Z", "DeItaone"),
                _tweet("12", "earlier outside window", earlier, "DeItaone"),
            ], has_more=True),
            # has_more=True, oldest timestamped item is inside window (undated item ignored)
            "CNN": _ok([
                {"id": "20", "text": "undated should be ignored", "author_handle": "CNN"},
                _tweet("21", "TRUMP latest", "2026-11-12T01:00:00+08:00", "CNN"),
                _tweet("22", "market brief", "2026-11-11T04:15:30Z", "CNN"),
            ], has_more=True),
            # has_more=True, no timestamped items -> incomplete fallback note
            "WSJ": _ok([{"id": "30", "text": "undated only", "author_handle": "WSJ"}], has_more=True),
            # has_more=False -> unchanged (no incomplete note even when items stay in window)
            "Bloomberg": _ok([_tweet("40", "in window", "2026-11-11T15:00:00+08:00", "Bloomberg")], has_more=False),
            "SkyNews": _ok([], has_more=False),
            "FT": _ok([], has_more=False),
            "FoxNews": _ok([], has_more=False),
            "TrumpDailyPosts": _ok([], has_more=False),
        },
        "web": {},
        "videos": {},
        "x_search": _ok([]),
    }

    result = cli.filter_raw(raw, window, {})
    notes = result["notes"]
    assert not any(note.startswith("DeItaone") for note in notes)
    assert "CNN 窗口内可能还有未取回的帖子，最早只取到 2026-11-11 12:15:30（香港时间）" in notes
    assert "WSJ 窗口内可能还有未取回的帖子" in notes
    assert not any(note.startswith("Bloomberg") for note in notes)


def test_vip_news_nested_bridge_coverage_and_utc_window() -> None:
    from datetime import timezone

    scripts_dalton = REPO_ROOT / "scripts" / "dalton"
    if str(scripts_dalton) not in sys.path:
        sys.path.insert(0, str(scripts_dalton))
    import researchdata_bridge as rb

    cli = _load("vip_news_nested_utc", CLI_PATH)
    window = cli.compute_window(AS_OF)
    start = datetime.fromisoformat(window["start_iso"])

    # 1. Nested bridge shape (truncated, covered): 49 items inside window + 2 items before window start -> 50th returned item is before start.
    raw_tweets = []
    for idx in range(49):
        dt_utc = (start + timedelta(minutes=25 * (49 - idx))).astimezone(timezone.utc)
        raw_tweets.append(
            {
                "id": str(600 + idx),
                "text": f"TRUMP update {idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "DeItaone", "name": "DeItaone"},
            }
        )
    before_utc = (start - timedelta(minutes=1)).astimezone(timezone.utc)
    older_utc = (start - timedelta(minutes=10)).astimezone(timezone.utc)
    for idx, dt_utc in enumerate((before_utc, older_utc)):
        raw_tweets.append(
            {
                "id": str(599 - idx),
                "text": f"TRUMP before window {idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "DeItaone", "name": "DeItaone"},
            }
        )
    plan = cli.plan_requests(AS_OF)
    bridge_data = rb._normalize_x_timeline_output(
        {"items": raw_tweets, "hasMore": False},
        operation="tweets",
        count=50,
        since=plan["wires"][0]["since"],
        until=plan["wires"][0]["until"],
    )
    assert len(bridge_data["items"]) == 50
    assert bridge_data["truncated"] is True
    assert bridge_data["has_more"] is True
    assert bridge_data["oldest_fetched_at"] == older_utc.isoformat(timespec="seconds")
    assert bridge_data["oldest_returned_at"] == before_utc.isoformat(timespec="seconds")

    # 1b. Codex middle-1: 51 in-window + 1 before-window truncated to 50 -> oldest_returned_at in window -> warns!
    raw_51 = []
    for idx in range(51):
        dt_utc = (start + timedelta(minutes=20 * (51 - idx))).astimezone(timezone.utc)
        raw_51.append(
            {
                "id": str(700 + idx),
                "text": f"TRUMP 51-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "FT", "name": "FT"},
            }
        )
    raw_51.append(
        {
            "id": "699",
            "text": "TRUMP pre-window 51",
            "createdAt": before_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "FT", "name": "FT"},
        }
    )
    bridge_51 = rb._normalize_x_timeline_output(
        {"items": raw_51, "hasMore": False},
        operation="tweets",
        count=50,
        since=plan["wires"][0]["since"],
        until=plan["wires"][0]["until"],
    )
    assert bridge_51["truncated"] is True
    assert bridge_51["oldest_fetched_at"] == before_utc.isoformat(timespec="seconds")
    assert bridge_51["oldest_returned_at"] == (start + timedelta(minutes=40)).astimezone(timezone.utc).isoformat(timespec="seconds")

    # 1c. Codex middle-2: old pinned tweet at index 0 + in-window tweets with hasMore=True -> pinned excluded -> warns!
    bridge_pinned = rb._normalize_x_timeline_output(
        {
            "items": [
                {
                    "id": "901",
                    "text": "TRUMP old pinned",
                    "createdAt": "Thu Jan 01 00:00:00 +0000 2026",
                    "user": {"screenName": "SkyNews", "name": "SkyNews"},
                },
                {
                    "id": "902",
                    "text": "TRUMP in window",
                    "createdAt": "Thu Nov 12 00:00:00 +0000 2026",  # 08:00 HKT
                    "user": {"screenName": "SkyNews", "name": "SkyNews"},
                },
            ],
            "hasMore": True,
        },
        operation="tweets",
        count=50,
        since=plan["wires"][0]["since"],
        until=plan["wires"][0]["until"],
    )
    assert bridge_pinned["truncated"] is False
    assert bridge_pinned["oldest_fetched_at"] == "2026-11-12T00:00:00+00:00"
    assert bridge_pinned["oldest_returned_at"] == "2026-11-12T00:00:00+00:00"

    raw = {
        "wires": {
            "DeItaone": {"ok": True, "status": "ok", "data": bridge_data},
            # truncated=True with has_more=False + oldest_fetched_at >= start + oldest_returned_at=None -> incomplete note
            "CNN": {
                "ok": True,
                "status": "ok",
                "data": {
                    "items": [],
                    "returned": 0,
                    "has_more": False,
                    "truncated": True,
                    "oldest_fetched_at": "2026-11-12T01:30:00+00:00",  # 09:30 HKT > end
                    "oldest_returned_at": None,
                },
            },
            # Nested empty + has_more=True + oldest_fetched_at=None -> omits earliest suffix even if x_search has items
            "WSJ": {
                "ok": True,
                "status": "empty",
                "empty_reason": "no_matching_items",
                "data": {"items": [], "returned": 0, "has_more": True, "truncated": False, "oldest_fetched_at": None, "oldest_returned_at": None},
            },
            # Non-truncated with oldest_fetched_at < start and oldest_returned_at >= start -> covered, no note
            "Bloomberg": {
                "ok": True,
                "status": "ok",
                "data": {
                    "items": [],
                    "has_more": True,
                    "truncated": False,
                    "oldest_fetched_at": before_utc.isoformat(timespec="seconds"),
                    "oldest_returned_at": "2026-11-11T10:00:00+00:00",
                },
            },
            "SkyNews": {"ok": True, "status": "ok", "data": bridge_pinned},
            "FT": {"ok": True, "status": "ok", "data": bridge_51},
            "FoxNews": _ok([], has_more=False),
            "TrumpDailyPosts": _ok([], has_more=False),
        },
        "web": {},
        "videos": {},
        "x_search": _ok([
            {
                "id": "799",
                "author": "WSJ",
                "date": "2026-11-11T15:00:00+08:00",
                "text": "TRUMP from x_search",
                "url": "https://x.com/WSJ/status/799",
            }
        ]),
    }
    res = cli.filter_raw(raw, window, {})
    notes = res["notes"]
    assert not any(note.startswith("DeItaone") for note in notes)
    assert not any(note.startswith("Bloomberg") for note in notes)
    assert "CNN 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 09:30:00（香港时间）" in notes
    assert "SkyNews 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 08:00:00（香港时间）" in notes
    expected_ft_hk = (start + timedelta(minutes=40)).astimezone(cli.TZ).strftime("%Y-%m-%d %H:%M:%S")
    assert f"FT 窗口内可能还有未取回的帖子，最早只取到 {expected_ft_hk}（香港时间）" in notes
    assert "WSJ 窗口内可能还有未取回的帖子" in notes
    assert not any(note.startswith("WSJ 窗口内可能还有未取回的帖子，") for note in notes)

    # 1d. Unified possible wording for both 50+1 pre-window and 51 in-window truncated timelines.
    raw_50_plus_1 = [
        {
            "id": str(750 + idx),
            "text": f"TRUMP 50-{idx}",
            "createdAt": (start + timedelta(minutes=20 * (50 - idx))).astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "FoxNews", "name": "FoxNews"},
        }
        for idx in range(50)
    ]
    raw_50_plus_1.append(
        {
            "id": "749",
            "text": "TRUMP pre-50",
            "createdAt": before_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "FoxNews", "name": "FoxNews"},
        }
    )
    bridge_50_plus_1 = rb._normalize_x_timeline_output(
        {"items": raw_50_plus_1, "hasMore": False},
        operation="tweets",
        count=50,
        since=plan["wires"][0]["since"],
        until=plan["wires"][0]["until"],
    )
    raw_51_only = [
        {
            "id": str(850 + idx),
            "text": f"TRUMP 51-only-{idx}",
            "createdAt": (start + timedelta(minutes=20 * (51 - idx))).astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "TrumpDailyPosts", "name": "TrumpDailyPosts"},
        }
        for idx in range(51)
    ]
    bridge_51_only = rb._normalize_x_timeline_output(
        {"items": raw_51_only, "hasMore": False},
        operation="tweets",
        count=50,
        since=plan["wires"][0]["since"],
        until=plan["wires"][0]["until"],
    )
    res_dist = cli.filter_raw(
        {
            "wires": {
                "FoxNews": {"ok": True, "status": "ok", "data": bridge_50_plus_1},
                "TrumpDailyPosts": {"ok": True, "status": "ok", "data": bridge_51_only},
                **{acc: _ok([]) for acc in WIRE_HANDLES if acc not in ("FoxNews", "TrumpDailyPosts")},
            },
            "web": {},
            "videos": {},
            "x_search": _ok([]),
        },
        window,
        {},
    )
    expected_fox_hk = (start + timedelta(minutes=20)).astimezone(cli.TZ).strftime("%Y-%m-%d %H:%M:%S")
    expected_tdp_hk = (start + timedelta(minutes=40)).astimezone(cli.TZ).strftime("%Y-%m-%d %H:%M:%S")
    assert f"FoxNews 窗口内可能还有未取回的帖子，最早只取到 {expected_fox_hk}（香港时间）" in res_dist["notes"]
    assert f"TrumpDailyPosts 窗口内可能还有未取回的帖子，最早只取到 {expected_tdp_hk}（香港时间）" in res_dist["notes"]
    assert all("窗口内帖子超过已取回的数量" not in n for n in res_dist["notes"])

    # 2. HKT 07:30 window end: since/until use UTC dates so tweets near window start (previous UTC day) survive bridge filtering.
    as_of_0730 = "2026-11-12T07:30:00+08:00"
    plan_0730 = cli.plan_requests(as_of_0730)
    assert plan_0730["window"]["summary_date"] == "2026-11-12"
    assert plan_0730["wires"][0]["since"] == "2026-11-10"
    assert plan_0730["wires"][0]["until"] == "2026-11-11"
    bridge_0730 = rb._normalize_x_timeline_output(
        {
            "items": [
                {
                    "id": "899",
                    "text": "TRUMP 07:45 HKT remarks",
                    "createdAt": "Tue Nov 10 23:45:00 +0000 2026",
                    "user": {"screenName": "DeItaone", "name": "DeItaone"},
                }
            ],
            "hasMore": False,
        },
        operation="tweets",
        count=50,
        since=plan_0730["wires"][0]["since"],
        until=plan_0730["wires"][0]["until"],
    )
    assert len(bridge_0730["items"]) == 1
    filtered_0730 = cli.filter_raw(
        {
            "wires": {
                "DeItaone": {"ok": True, "status": "ok", "data": bridge_0730},
                **{acc: _ok([]) for acc in WIRE_HANDLES if acc != "DeItaone"},
            },
            "web": {},
            "videos": {},
            "x_search": _ok([]),
        },
        plan_0730["window"],
        {},
    )
    trump_wire = _person(filtered_0730, "Trump")["wire_headlines"]
    assert [row["status_id"] for row in trump_wire] == ["899"]


@pytest.mark.asyncio
async def test_vip_card_with_inline_code_number_matches_automation_report_digest_and_commits_seen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any
    from unittest.mock import create_autospec

    from src.dalton.access_tiers import is_internal_or_admin
    from src.dalton.database.automation_reports import (
        read_automation_report_status,
        submit_automation_report,
    )
    from src.dalton.notify.dispatch import dispatch_automation_report
    from src.dalton.tools import automation_report_tools as report_tools

    cli = _load("vip_news_inline_digest", CLI_PATH)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    headline_url = "https://x.com/DeItaone/status/912"
    headline_title = "TRUMP cited 16,956.0 figure"
    events_payload = {
        "summary_date": "2026-11-12",
        "people": [
            {
                "key": "Trump",
                "headlines": [
                    {
                        "summary": "提到核心指标 `16,956.0`（同比 `+46.7%`）与标的 `ANT`",
                        "source": "DeItaone",
                        "title": headline_title,
                        "url": headline_url,
                    }
                ],
            }
        ],
    }
    _write_json(workspace, "events.json", events_payload)
    code, rendered = _run(
        cli,
        [
            "--json",
            "render",
            "--workspace-dir",
            str(workspace),
            "--events",
            "events.json",
            "--markdown",
            "temp/vip-news-alert/2026-11-12/card.md",
            "--output",
            "temp/vip-news-alert/2026-11-12/card.json",
            "--as-of",
            AS_OF,
        ],
    )
    assert code == 0 and rendered["empty"] is False, rendered
    card = json.loads(
        (workspace / "temp/vip-news-alert/2026-11-12/card.json").read_text(encoding="utf-8")
    )
    assert "`16,956.0`" in json.dumps(card, ensure_ascii=False)
    expected_digest = cli.card_content_sha256(card)

    saved_state: dict[str, Any] = {}

    async def _fake_submit(**kwargs: Any) -> dict[str, Any]:
        forwarded_cards = kwargs["cards"]
        digest = automation_report_content_sha256(
            profile=PROFILE,
            content_kind="cards",
            cards=forwarded_cards,
            failure_text=None,
        )
        saved_state["digest"] = digest
        return {
            "ok": True,
            "action": "submit",
            "quiet": False,
            "report_run_id": kwargs["report_run_id"],
            "profile": PROFILE,
            "content_kind": "cards",
            "content_sha256": digest,
            "execution_id": kwargs["execution_id"],
            "delivery_confirmed": False,
            "status": "pending",
            "targets": ["feishu:chat:oc_vip"],
            "created": True,
        }

    async def _fake_status(**kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "action": "status",
            "quiet": False,
            "found": True,
            "report_run_id": kwargs["report_run_id"],
            "profile": PROFILE,
            "content_kind": "cards",
            "content_sha256": saved_state["digest"],
            "execution_id": kwargs["execution_id"],
            "delivery_confirmed": True,
            "status": "sent",
            "targets": ["feishu:chat:oc_vip"],
        }

    monkeypatch.setattr(
        report_tools,
        "submit_automation_report",
        create_autospec(submit_automation_report, side_effect=_fake_submit),
    )
    monkeypatch.setattr(
        report_tools,
        "read_automation_report_status",
        create_autospec(read_automation_report_status, side_effect=_fake_status),
    )
    monkeypatch.setattr(
        report_tools,
        "dispatch_automation_report",
        create_autospec(
            dispatch_automation_report,
            return_value={"ok": True, "delivery_confirmed": True, "status": "sent"},
        ),
    )
    monkeypatch.setattr(
        report_tools,
        "is_internal_or_admin",
        create_autospec(is_internal_or_admin, return_value=True),
    )

    tool = report_tools.create_automation_report_tools()[0]
    cfg = {
        "configurable": {
            "user_id": "user-vip-1",
            "workspace_id": "ws-vip-1",
            "thread_id": "thread-vip-1",
            "run_id": "run-vip-1",
            "turn_origin_automation_id": "22222222-2222-4222-8222-222222222222",
            "automation_execution_id": "11111111-1111-4111-8111-111111111111",
        }
    }
    raw_res = await tool.ainvoke(
        {
            "action": "submit",
            "report_run_id": RUN_ID,
            "material": "yes",
            "cards": [card],
        },
        config=cfg,
    )
    confirm = json.loads(raw_res)
    assert confirm["ok"] is True
    assert confirm["delivery_confirmed"] is True
    assert confirm["status"] == "sent"
    assert confirm["content_sha256"] == expected_digest

    committed = cli.commit_seen(workspace, confirm, as_of=AS_OF)
    assert committed["ok"] is True
    assert committed["added"] >= 1
