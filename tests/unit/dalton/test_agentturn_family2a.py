"""OC-1 family 2a: weekly trend scan and CT semiconductor sentiment, plain text."""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import yaml

from src.dalton.database.automation_reports import REPORT_PROFILES

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILLS = REPO_ROOT / "plugins" / "dalton" / "skills"
SKILL_NAMES = (
    "dalton-weekly-trend-impact-scan",
    "dalton-ct-semi-sentiment",
)
PROFILES = ("weekly-trend-impact-scan", "ct-semi-sentiment")
BANNED = (
    "discord",
    "feishu",
    "xreach",
    ".openclaw",
    "openclaw_workspace",
    "chat_id",
    "feishu_send",
    "/Users/",
    "file://",
    "oc_a6b59323f034cb246c80e1b3684e71d1",
    "1528743490555740230",
    "1481256083589697566",
)
_RECIPIENT_OC_RE = re.compile(r"(?<![A-Za-z])oc_")
_TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
_LINE = "2026-11-12 | 3/10 | 存储回调仍是共同话题"
_REPORT_DATE = "2026-11-08"
_SUMMARY_DATE = "2026-11-12"
_END_ISO = "2026-11-12T07:45:00-05:00"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _weekly():
    return _load(
        "weekly_scan_family2a",
        SKILLS / "dalton-weekly-trend-impact-scan" / "scripts" / "weekly_scan.py",
    )


def _sentiment():
    return _load(
        "sentiment_log_family2a",
        SKILLS / "dalton-ct-semi-sentiment" / "scripts" / "sentiment_log.py",
    )


def _run(module, argv: list[str]) -> tuple[int, dict, str]:
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


def _skill_argvs(skill: str, replacements: dict[str, str]) -> list[list[str]]:
    """Split the fence first, then substitute. A value with spaces stays one argument."""
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    argvs: list[list[str]] = []
    for command in _bash_commands(text):
        script_at = next(index for index, token in enumerate(command) if token.endswith(".py"))
        argv: list[str] = []
        for token in command[script_at + 1 :]:
            token = token.strip("'\"")
            for old, new in replacements.items():
                token = token.replace(old, new)
            argv.append(token)
        argvs.append(argv)
    return argvs


def _python_blocks(text: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", text, re.DOTALL)


def _weekly_report(report_date: str, *, trends: int = 3) -> str:
    blocks = []
    for number in range(1, trends + 1):
        blocks.append(
            "\n".join(
                [
                    f"趋势 {number}：需求在过去一周变化。",
                    "承压方向：甲公司（AAA）— 事实是订单下降。传导判断是收入承压。",
                    "近期股价：2026-11-01 至 2026-11-08 收盘 -3%，未复权与复权一致。",
                    "受益方向：乙公司（BBB）— 事实是份额上升。传导判断是定价权改善。",
                    "近期股价：2026-11-01 至 2026-11-08 收盘 +4%，未复权与复权一致。",
                    "主要不确定性：政策口径。",
                ]
            )
        )
    watch = "本周更值得跟踪的方向：\n- 偏空观察：AAA — 订单继续下降\n- 偏多观察：BBB — 份额继续上升"
    return f"📊 本周趋势影响扫描 — {report_date}\n\n" + "\n\n".join(blocks) + "\n\n" + watch + "\n"


def _daily_report(summary_date: str, *, temperature: str) -> str:
    return (
        f"🧭 CT半导体情绪日报（{summary_date}）\n"
        "【共识主线】存储回调仍是共同话题。\n"
        f"【情绪温度】{temperature}\n"
        "【分歧点】今日无明显分歧。\n"
        "【个股/仓位信号】@xiaomustock：维持原有仓位。\n"
        "【数据备注】\n"
        "时间线抓取失败账号：无\n"
        "过去24小时无新推账号：无\n"
        "有新推但无半导体相关观点账号：无\n"
        "SuperGrok X Search 补充状态：补充成功，没有新增\n"
    )


def _append_argv(
    workspace: Path,
    *,
    delivery_confirmed: str = "true",
    status: str = "sent",
    content_kind: str = "text",
    date: str = _SUMMARY_DATE,
    line: str = _LINE,
    file: str | None = None,
) -> list[str]:
    argv = [
        "--json",
        "append",
        "--workspace-dir",
        str(workspace),
        "--delivery-confirmed",
        delivery_confirmed,
        "--status",
        status,
        "--content-kind",
        content_kind,
        "--date",
        date,
        "--line",
        line,
    ]
    if file is not None:
        argv.extend(["--file", file])
    return argv


def _assert_no_datetime(value: object) -> None:
    assert not isinstance(value, datetime)
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_datetime(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_datetime(item)


def _raises_value(func, message: str) -> None:
    try:
        func()
    except ValueError as exc:
        assert str(exc) == message
    else:
        raise AssertionError(message)


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
        assert meta["name"] == name
        description = meta["description"].strip()
        assert description
        assert "触发词" not in description
        assert len(description) <= 120
        assert (name, None, ()) in _DALTON_SKILL_SPECS
        assert DALTON_SKILL_DEFINITIONS[name].requires == ()
        assert DALTON_SKILL_DEFINITIONS[name].command is None
        assert DALTON_SKILL_DEFINITIONS[name].description == description


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


def test_skill_text_locks_research_and_delivery() -> None:
    weekly = _weekly()
    sentiment = _sentiment()
    weekly_text = (SKILLS / "dalton-weekly-trend-impact-scan" / "SKILL.md").read_text(encoding="utf-8")
    daily_text = (SKILLS / "dalton-ct-semi-sentiment" / "SKILL.md").read_text(encoding="utf-8")
    for text in (weekly_text, daily_text):
        assert "cards=" not in text
        assert "text=BODY" in text
        assert 'material="yes"' in text
        assert "NO_REPLY" in text
        assert "content_kind" in text
        assert "无人值守" in text

    for scope, _query in weekly.QUERIES:
        assert scope in weekly_text
        assert not re.search(r"\d{4}-\d{2}-\d{2}", _query)
    assert "Asia/Hong_Kong" in weekly_text
    assert "weekly-trend-impact-scan-<report_date>" in weekly_text
    assert "start_date" in weekly_text and "end_date" in weekly_text
    assert "可能已部分反映" in weekly_text
    assert "20%" in weekly_text
    assert "暂无" in weekly_text
    assert "不明确" in weekly_text
    assert "行情缺失" in weekly_text
    assert "get_stock_history" in weekly_text
    assert "get_dividends_and_splits" in weekly_text
    assert "`adjusted` 分别为 false 和 true" in weekly_text
    assert "0700.HK" in weekly_text
    assert "趋势影响周扫第 N 步失败" in weekly_text
    web_blocks = [block for block in _python_blocks(weekly_text) if "web_search" in block]
    assert web_blocks
    assert "start_date=START_DATE" in web_blocks[0]
    assert "end_date=END_DATE" in web_blocks[0]
    assert "freshness" not in web_blocks[0]

    assert "America/New_York" in daily_text
    assert "ct-semi-sentiment-<summary_date>" in daily_text
    assert "state/ct-semi-sentiment/sentiment.log" in daily_text
    assert all("import_openclaw" not in " ".join(command) for command in _bash_commands(daily_text))
    assert "1700" in daily_text and "1900" in daily_text
    assert "不要并行" in daily_text
    assert "delivery_confirmed" in daily_text
    assert "--content-kind text" in daily_text
    assert "无明显共识" in daily_text
    assert "今日无明显分歧" in daily_text
    assert "本轮没有取到时间线" in daily_text
    assert "本轮无法评分" in daily_text
    assert "x_query" in daily_text
    for handle, label in sentiment.HANDLES:
        assert handle in daily_text
        if label:
            assert label in daily_text
    community_blocks = [block for block in _python_blocks(daily_text) if "community(" in block]
    search_blocks = [block for block in _python_blocks(daily_text) if "x_search(" in block]
    assert len(community_blocks) == 1
    assert len(search_blocks) == 1
    assert 'route="x-timeline"' in community_blocks[0]
    assert 'operation="tweets"' in community_blocks[0]
    assert "count=50" in community_blocks[0]
    assert "include_replies" not in community_blocks[0]
    assert 'handles=[item["handle"] for item in window["handles"]]' in search_blocks[0]
    assert "x_handles" in daily_text
    assert "enable_image_understanding=True" in search_blocks[0]
    assert "enable_video_understanding=True" in search_blocks[0]
    assert "allowed_x_handles" not in search_blocks[0]
    assert "不要传 `allowed_x_handles`" in daily_text
    assert "半导体情绪日报第 N 步失败" in daily_text


def test_profiles_are_registered() -> None:
    assert set(PROFILES) <= REPORT_PROFILES


def test_helpers_match_real_signatures() -> None:
    weekly = _weekly()
    sentiment = _sentiment()
    window = sentiment.compute_window(_END_ISO)
    inspect.signature(weekly.workspace_file).bind(Path("ws"), "temp/a.txt")
    inspect.signature(weekly.validate_text).bind("text", _REPORT_DATE)
    inspect.signature(weekly.write_bytes_exclusive).bind(Path("ws/a.txt"), b"ok")
    inspect.signature(sentiment.workspace_file).bind(Path("ws"), sentiment.LOG_REL)
    inspect.signature(sentiment.validate_line).bind(_LINE, _SUMMARY_DATE)
    inspect.signature(sentiment.validate_report).bind("text", _SUMMARY_DATE, require_change=False)
    inspect.signature(sentiment.select_posts).bind({}, window)
    inspect.signature(sentiment.write_bytes_exclusive).bind(Path("ws/a.txt"), b"ok")


def test_windows_use_market_timezones() -> None:
    weekly = _weekly()
    sentiment = _sentiment()
    hong_kong = weekly.compute_window("2026-11-08T20:00:00+08:00")
    assert hong_kong["timezone"] == "Asia/Hong_Kong"
    assert hong_kong["report_date"] == "2026-11-08"
    assert hong_kong["start_date"] == "2026-11-01"
    assert hong_kong["end_date"] == "2026-11-08"
    assert hong_kong["report_run_id"] == "weekly-trend-impact-scan-2026-11-08"
    assert datetime.fromisoformat(hong_kong["end_iso"]) - datetime.fromisoformat(hong_kong["start_iso"]) == timedelta(days=7)
    assert [(item["scope"], item["query"]) for item in hong_kong["queries"]] == list(weekly.QUERIES)
    assert weekly.compute_window("2026-11-08T20:00:00+00:00")["report_date"] == "2026-11-09"
    assert weekly.compute_window("2026-11-08T20:00:00")["report_date"] == "2026-11-08"
    assert weekly.compute_window("2026-11-08T16:00:00Z")["report_date"] == "2026-11-09"

    new_york = sentiment.compute_window(_END_ISO)
    assert new_york["timezone"] == "America/New_York"
    assert new_york["summary_date"] == "2026-11-12"
    assert new_york["start_date"] == "2026-11-11"
    assert new_york["end_date"] == "2026-11-12"
    assert new_york["report_run_id"] == "ct-semi-sentiment-2026-11-12"
    assert datetime.fromisoformat(new_york["end_iso"]) - datetime.fromisoformat(new_york["start_iso"]) == timedelta(hours=24)
    assert [item["handle"] for item in new_york["handles"]] == list(sentiment.HANDLE_ORDER)
    assert new_york["x_handles"] == [item["handle"] for item in new_york["handles"]]
    assert all(isinstance(handle, str) for handle in new_york["x_handles"])
    assert new_york["handles"][7]["label"] == ""
    assert new_york["handles"][8]["label"] == ""
    assert "半导体" in new_york["x_query"]
    assert "HBM" in new_york["x_query"]
    assert "Neocloud" in new_york["x_query"]
    assert sentiment.compute_window("2026-11-12T04:45:00+08:00")["summary_date"] == "2026-11-11"
    assert sentiment.compute_window("2026-11-12T07:45:00")["summary_date"] == "2026-11-12"
    assert sentiment.compute_window("2026-11-12T04:45:00Z")["summary_date"] == "2026-11-11"
    spring = sentiment.compute_window("2026-03-08T12:00:00")
    spring_start = datetime.fromisoformat(spring["start_iso"])
    spring_end = datetime.fromisoformat(spring["end_iso"])
    assert spring_end - spring_start == timedelta(hours=24)
    assert spring_start.astimezone(sentiment.TZ).hour == 11
    assert spring["start_date"] == "2026-03-07"
    assert spring["summary_date"] == "2026-03-08"
    fall = sentiment.compute_window("2026-11-01T12:00:00")
    fall_start = datetime.fromisoformat(fall["start_iso"])
    fall_end = datetime.fromisoformat(fall["end_iso"])
    assert fall_end - fall_start == timedelta(hours=24)
    assert fall_start.astimezone(sentiment.TZ).hour == 13
    assert fall["start_date"] == "2026-10-31"
    assert fall["summary_date"] == "2026-11-01"

    for module in (weekly, sentiment):
        code, payload, stderr = _run(module, ["--json", "window", "--as-of", "not-a-date"])
        assert code == 1, stderr
        assert payload["error"] == "invalid as-of"


def test_weekly_text_keeps_three_to_five_trends(tmp_path: Path) -> None:
    weekly = _weekly()
    assert weekly.validate_text(_weekly_report(_REPORT_DATE), _REPORT_DATE) == []
    short = weekly.validate_text(_weekly_report(_REPORT_DATE, trends=2), _REPORT_DATE)
    assert any("3 到 5" in item for item in short)
    long = weekly.validate_text(_weekly_report(_REPORT_DATE, trends=6), _REPORT_DATE)
    assert any("3 到 5" in item for item in long)
    headed = _weekly_report(_REPORT_DATE).replace("趋势 1：", "# 标题\n趋势 1：", 1)
    assert any("Markdown" in item for item in weekly.validate_text(headed, _REPORT_DATE))
    dirty = _weekly_report(_REPORT_DATE) + ("fei" + "shu")
    assert weekly.validate_text(dirty, _REPORT_DATE) == ["正文含有接收方痕迹"]
    thin = _weekly_report(_REPORT_DATE).replace(
        "近期股价：2026-11-01 至 2026-11-08 收盘 -3%，未复权与复权一致。\n",
        "",
        1,
    )
    assert any("趋势 1" in item and "近期股价" in item for item in weekly.validate_text(thin, _REPORT_DATE))
    swapped = _weekly_report(_REPORT_DATE).replace(
        "承压方向：甲公司（AAA）— 事实是订单下降。传导判断是收入承压。\n"
        "近期股价：2026-11-01 至 2026-11-08 收盘 -3%，未复权与复权一致。\n"
        "受益方向：乙公司（BBB）— 事实是份额上升。传导判断是定价权改善。\n",
        "受益方向：乙公司（BBB）— 事实是份额上升。传导判断是定价权改善。\n"
        "近期股价：2026-11-01 至 2026-11-08 收盘 +4%，未复权与复权一致。\n"
        "承压方向：甲公司（AAA）— 事实是订单下降。传导判断是收入承压。\n",
        1,
    )
    assert any("趋势 1" in item and "栏目顺序" in item for item in weekly.validate_text(swapped, _REPORT_DATE))

    workspace = tmp_path / "ws"
    report = workspace / "temp" / "weekly-trend-impact-scan" / f"{_REPORT_DATE}.txt"
    report.parent.mkdir(parents=True)
    report.write_text(_weekly_report(_REPORT_DATE), encoding="utf-8")
    code, payload, stderr = _run(
        weekly,
        [
            "--json",
            "check",
            "--workspace-dir",
            str(workspace),
            "--file",
            f"temp/weekly-trend-impact-scan/{_REPORT_DATE}.txt",
            "--date",
            _REPORT_DATE,
        ],
    )
    assert code == 0, (payload, stderr)
    assert payload["trends"] == 3


def test_daily_text_cap_order_and_change_arrow(tmp_path: Path) -> None:
    sentiment = _sentiment()
    plain = _daily_report(_SUMMARY_DATE, temperature="3/10，仓位偏谨慎。")
    assert sentiment.validate_report(plain, _SUMMARY_DATE, require_change=False) == []
    unscored = _daily_report(_SUMMARY_DATE, temperature="本轮无法评分。")
    assert sentiment.validate_report(unscored, _SUMMARY_DATE, require_change=False) == []
    assert sentiment.validate_report(unscored, _SUMMARY_DATE, require_change=True) == []
    missing_temperature = plain.replace("【情绪温度】", "【已删温度】", 1)
    assert any("栏目顺序" in item for item in sentiment.validate_report(missing_temperature, _SUMMARY_DATE, require_change=False))
    fat = plain.replace("【个股/仓位信号】", "【个股/仓位信号】" + ("仓" * 1700), 1)
    assert any("1700" in item and "个股" in item for item in sentiment.validate_report(fat, _SUMMARY_DATE, require_change=False))
    assert any("↑" in item for item in sentiment.validate_report(plain, _SUMMARY_DATE, require_change=True))
    marked = _daily_report(_SUMMARY_DATE, temperature="3/10 ↓，仓位偏谨慎。")
    assert sentiment.validate_report(marked, _SUMMARY_DATE, require_change=True) == []

    workspace = tmp_path / "ws"
    report = workspace / "temp" / "ct-semi-sentiment" / _SUMMARY_DATE / "report.txt"
    report.parent.mkdir(parents=True)
    report.write_text(plain, encoding="utf-8")
    log = workspace / "state" / "ct-semi-sentiment" / "sentiment.log"
    log.parent.mkdir(parents=True)
    log.write_text("2026-11-11 | 4/10 | 旧主线\n", encoding="utf-8")
    before = log.read_bytes()
    code, payload, stderr = _run(
        sentiment,
        [
            "--json",
            "check",
            "--workspace-dir",
            str(workspace),
            "--file",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/report.txt",
            "--date",
            _SUMMARY_DATE,
        ],
    )
    assert code == 1, stderr
    assert any("↑" in item for item in payload["errors"])
    assert log.read_bytes() == before
    report.write_text(marked, encoding="utf-8")
    code, payload, stderr = _run(
        sentiment,
        [
            "--json",
            "check",
            "--workspace-dir",
            str(workspace),
            "--file",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/report.txt",
            "--date",
            _SUMMARY_DATE,
        ],
    )
    assert code == 0, (payload, stderr)
    assert payload["require_change"] is True
    assert log.read_bytes() == before


def test_select_filters_the_strict_24h_window(tmp_path: Path) -> None:
    sentiment = _sentiment()
    raw = {
        "accounts": {
            "Xiaomustock": {
                "ok": True,
                "has_more": False,
                "items": [
                    {"id": "101", "text": "存储还在交易", "created_at": "2026-11-12T06:00:00-05:00"},
                    {"id": "100", "text": "旧置顶", "createdAt": "Wed Oct 01 06:00:00 +0000 2026"},
                ],
            },
            "nft_hu": {
                "ok": True,
                "items": [
                    {"id": "201", "text": "RT @someone: 转发", "created_at": "2026-11-12T06:00:00-05:00"},
                ],
            },
            "octopusycc": {
                "ok": True,
                "items": [
                    {
                        "id": "301",
                        "text": "同意这轮存储观点",
                        "createdAt": "Wed Nov 12 11:00:00 +0000 2026",
                        "is_repost": True,
                        "quoted_id": "999",
                    }
                ],
            },
            "bitfool1": {
                "ok": True,
                "items": [
                    {"id": "401", "text": "差一秒", "created_at": "2026-11-11T07:44:59-05:00"},
                ],
            },
            "blknoiz06": {
                "ok": True,
                "items": [
                    {"id": "451", "text": "窗口已关", "created_at": "2026-11-12T07:45:01-05:00"},
                ],
            },
            "ShanghaoJin": {
                "ok": True,
                "data": {
                    "has_more": True,
                    "items": [
                        {"id": "501", "text": "设备订单", "created_at": "2026-11-12T07:45:00-05:00"},
                    ],
                },
            },
            "degentradingLSD": {
                "ok": True,
                "items": [
                    {"id": "601", "text": "刚好窗口起点", "created_at": "2026-11-11T07:45:00-05:00"},
                ],
            },
            "qinbafrank": {"ok": True, "items": []},
            "bboczeng": {"ok": True, "items": []},
        },
        "x_search": {
            "ok": True,
            "items": [
                {
                    "author": "qinbafrank",
                    "url": "https://twitter.com/qinbafrank/status/701",
                    "date": "2026-11-12T06:30:00-05:00",
                    "summary_excerpt": "存储资本开支",
                    "image_understanding": "图表显示库存下降",
                },
                {
                    "author": "xiaomustock",
                    "url": "https://x.com/xiaomustock/status/101",
                    "date": "2026-11-12T06:00:00-05:00",
                    "summary_excerpt": "重复",
                },
                {
                    "author": "outsider",
                    "url": "https://x.com/outsider/status/801",
                    "date": "2026-11-12T06:00:00-05:00",
                    "summary_excerpt": "无关",
                },
                {
                    "author": "bboczeng",
                    "url": "https://example.com/not-a-status",
                    "date": "2026-11-12T06:00:00-05:00",
                    "summary_excerpt": "没有原帖链接",
                },
            ],
        },
    }
    workspace = tmp_path / "ws"
    source = workspace / "temp" / "ct-semi-sentiment" / _SUMMARY_DATE / "raw.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    code, payload, stderr = _run(
        sentiment,
        [
            "--json",
            "select",
            "--workspace-dir",
            str(workspace),
            "--as-of",
            _END_ISO,
            "--input",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/raw.json",
            "--output",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/selected.json",
        ],
    )
    assert code == 0, (payload, stderr)
    assert payload["failed"] == ["notthreadguy"]
    assert payload["no_posts"] == ["nft_hu", "bitfool1", "blknoiz06", "bboczeng"]
    assert payload["with_posts"] == [
        "xiaomustock",
        "octopusycc",
        "ShanghaoJin",
        "degentradingLSD",
        "qinbafrank",
    ]
    assert payload["has_more"] == ["ShanghaoJin"]
    assert payload["x_search_ok"] is True
    assert payload["supplemented"] == 1
    selected = json.loads((source.parent / "selected.json").read_text(encoding="utf-8"))
    _assert_no_datetime(selected)
    assert "Xiaomustock" not in selected["accounts"]
    kept_at = datetime.fromisoformat("2026-11-12T06:00:00-05:00").astimezone(sentiment.TZ).isoformat()
    assert selected["accounts"]["xiaomustock"]["items"] == [
        {
            "id": "101",
            "url": "https://x.com/xiaomustock/status/101",
            "created_at": kept_at,
            "text": "存储还在交易\n重复",
            "source": "timeline+x_search",
        }
    ]
    quote = selected["accounts"]["octopusycc"]["items"][0]
    assert quote["source"] == "timeline"
    assert quote["created_at"].startswith("2026-11-12T06:00:00")
    assert selected["accounts"]["qinbafrank"]["items"][0]["source"] == "x_search"
    assert selected["accounts"]["qinbafrank"]["items"][0]["image_understanding"] == "图表显示库存下降"
    dumped = json.dumps(selected, ensure_ascii=False)
    for dropped in ("旧置顶", "RT @someone", "差一秒", "窗口已关", "无关", "没有原帖链接"):
        assert dropped not in dumped


def test_unscored_daily_skips_change_arrow_when_timelines_fail(tmp_path: Path) -> None:
    sentiment = _sentiment()
    body = _daily_report(_SUMMARY_DATE, temperature="本轮无法评分。").replace(
        "存储回调仍是共同话题。",
        "本轮没有取到时间线。",
    )
    assert sentiment.validate_report(body, _SUMMARY_DATE, require_change=True) == []
    workspace = tmp_path / "ws"
    report = workspace / "temp" / "ct-semi-sentiment" / _SUMMARY_DATE / "report.txt"
    report.parent.mkdir(parents=True)
    report.write_text(body, encoding="utf-8")
    log = workspace / "state" / "ct-semi-sentiment" / "sentiment.log"
    log.parent.mkdir(parents=True)
    log.write_text("2026-11-11 | 4/10 | 旧主线\n", encoding="utf-8")
    before = log.read_bytes()
    code, payload, stderr = _run(
        sentiment,
        [
            "--json",
            "check",
            "--workspace-dir",
            str(workspace),
            "--file",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/report.txt",
            "--date",
            _SUMMARY_DATE,
        ],
    )
    assert code == 0, (payload, stderr)
    assert log.read_bytes() == before


def test_supplement_merges_media_into_the_same_status() -> None:
    sentiment = _sentiment()
    accounts = {handle: {"ok": True, "has_more": False, "items": []} for handle in sentiment.HANDLE_ORDER}
    accounts["xiaomustock"] = {
        "ok": True,
        "items": [
            {
                "id": "101",
                "text": "存储还在交易",
                "created_at": "2026-11-12T06:00:00-05:00",
            }
        ],
    }
    selected = sentiment.select_posts(
        {
            "accounts": accounts,
            "x_search": {
                "ok": True,
                "items": [
                    {
                        "author": "xiaomustock",
                        "id": "101",
                        "url": "https://x.com/xiaomustock/status/101",
                        "date": "2026-11-12T06:00:00-05:00",
                        "summary_excerpt": "线程里还提到了设备交期",
                        "image_understanding": "图里是库存曲线",
                        "video_understanding": "视频口播提到稼动率",
                    }
                ],
            },
        },
        sentiment.compute_window(_END_ISO),
    )
    assert selected["x_search"]["supplemented"] == 0
    items = selected["accounts"]["xiaomustock"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == "101"
    assert items[0]["source"] == "timeline+x_search"
    assert items[0]["image_understanding"] == "图里是库存曲线"
    assert items[0]["video_understanding"] == "视频口播提到稼动率"
    assert items[0]["text"] == "存储还在交易\n线程里还提到了设备交期"
    assert selected["with_posts"] == ["xiaomustock"]


def test_failed_timeline_can_still_receive_a_supplement() -> None:
    sentiment = _sentiment()
    accounts = {handle: {"ok": True, "has_more": False, "items": []} for handle in sentiment.HANDLE_ORDER}
    accounts["notthreadguy"] = {
        "ok": False,
        "has_more": True,
        "items": [
            {
                "id": "9",
                "text": "时间线失败时这条不能留下",
                "created_at": "2026-11-12T06:00:00-05:00",
            }
        ],
    }
    selected = sentiment.select_posts(
        {
            "accounts": accounts,
            "x_search": {
                "ok": True,
                "items": [
                    {
                        "author": "notthreadguy",
                        "url": "https://x.com/notthreadguy/status/902",
                        "date": "2026-11-12T06:10:00-05:00",
                        "summary_excerpt": "补充到的设备观点",
                    }
                ],
            },
        },
        sentiment.compute_window(_END_ISO),
    )
    assert selected["failed"] == ["notthreadguy"]
    assert selected["with_posts"] == ["notthreadguy"]
    assert "notthreadguy" not in selected["no_posts"]
    assert "notthreadguy" not in selected["has_more"]
    supplemented_at = datetime.fromisoformat("2026-11-12T06:10:00-05:00").astimezone(sentiment.TZ).isoformat()
    assert selected["accounts"]["notthreadguy"]["items"] == [
        {
            "id": "902",
            "url": "https://x.com/notthreadguy/status/902",
            "created_at": supplemented_at,
            "text": "补充到的设备观点",
            "source": "x_search",
        }
    ]


def test_log_line_score_and_append_gate(tmp_path: Path) -> None:
    sentiment = _sentiment()
    assert sentiment.validate_line(_LINE, _SUMMARY_DATE) == _LINE
    assert sentiment.validate_line("2026-11-12 | 10/10 | 极度贪婪", _SUMMARY_DATE) == "2026-11-12 | 10/10 | 极度贪婪"
    for rejected, summary_date in (
        ("2026-11-12 | 0/10 | 无", _SUMMARY_DATE),
        ("2026-11-12 | 03/10 | 无", _SUMMARY_DATE),
        ("2026-11-12 | 11/10 | 无", _SUMMARY_DATE),
        ("2026-11-12 | 1/10 | 无", "2026-11-13"),
        ("2026-11-12 | 3/10 | 上\n下", _SUMMARY_DATE),
        ("2026-11-12 | 3/10 | " + ("fei" + "shu"), _SUMMARY_DATE),
    ):
        assert sentiment.validate_line(rejected, summary_date) is None
    prefix = "2026-11-12 | 3/10 | "
    exact = prefix + ("甲" * (500 - len(prefix)))
    assert len(exact) == 500
    assert sentiment.validate_line(exact, _SUMMARY_DATE) == exact
    assert sentiment.validate_line(exact + "乙", _SUMMARY_DATE) is None

    absent = tmp_path / "absent"
    for argv in (
        _append_argv(absent, delivery_confirmed="false"),
        _append_argv(absent, status="pending"),
        _append_argv(absent, content_kind="cards"),
        _append_argv(absent, content_kind="Text"),
        _append_argv(absent, line="2026-11-12 | 11/10 | 过高"),
        _append_argv(absent, line=exact + "乙"),
    ):
        code, payload, stderr = _run(sentiment, argv)
        assert code == 1, stderr
        assert payload["error"] in {"not_confirmed", "invalid_line"}
        assert not absent.exists()

    workspace = tmp_path / "score"
    workspace.mkdir()
    code, payload, stderr = _run(
        sentiment,
        _append_argv(workspace, delivery_confirmed="True", line="2026-11-12 | 10/10 | 极度贪婪"),
    )
    assert code == 0, (payload, stderr)
    assert payload["reused"] is False
    log = workspace / "state" / "ct-semi-sentiment" / "sentiment.log"
    assert log.read_text(encoding="utf-8") == "2026-11-12 | 10/10 | 极度贪婪\n"


def test_log_tail_keeps_history_and_rejects_conflicts(tmp_path: Path) -> None:
    sentiment = _sentiment()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    lines = [
        "2026-11-08 | 2/10 | 甲",
        "2026-11-09 | 4/10 | 乙",
        "2026-11-10 | 5/10 | 丙",
        "2026-11-11 | 6/10 | 丁",
        _LINE,
    ]
    for line in lines:
        code, payload, stderr = _run(sentiment, _append_argv(workspace, date=line[:10], line=line))
        assert code == 0, (payload, stderr)
        assert payload["reused"] is False
    code, payload, stderr = _run(sentiment, ["--json", "tail", "--workspace-dir", str(workspace)])
    assert code == 0, (payload, stderr)
    assert payload["lines"] == lines[-3:]
    log = workspace / "state" / "ct-semi-sentiment" / "sentiment.log"
    assert log.read_text(encoding="utf-8").splitlines() == lines
    saved = log.read_bytes()
    code, payload, stderr = _run(sentiment, _append_argv(workspace))
    assert code == 0, (payload, stderr)
    assert payload["reused"] is True
    assert log.read_bytes() == saved
    code, payload, stderr = _run(sentiment, _append_argv(workspace, line="2026-11-12 | 7/10 | 另一句"))
    assert code == 1, stderr
    assert payload["error"] == "log_conflict"
    assert log.read_bytes() == saved


def test_paths_and_symlinks_stay_inside_workspace(tmp_path: Path) -> None:
    weekly = _weekly()
    sentiment = _sentiment()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    weekly_workspace = tmp_path / "weekly-escape"
    select_escape = tmp_path / "select-escape"
    append_escape = tmp_path / "append-escape"
    for folder in (weekly_workspace, select_escape, append_escape):
        folder.mkdir()
    (select_escape / "raw.json").write_text(
        json.dumps({"accounts": {}, "x_search": {"ok": False, "items": []}}),
        encoding="utf-8",
    )

    for module, argv in (
        (
            weekly,
            [
                "--json",
                "check",
                "--workspace-dir",
                str(weekly_workspace),
                "--file",
                "../keep.txt",
                "--date",
                _REPORT_DATE,
            ],
        ),
        (
            sentiment,
            [
                "--json",
                "select",
                "--workspace-dir",
                str(select_escape),
                "--as-of",
                _END_ISO,
                "--input",
                "raw.json",
                "--output",
                "../selected.json",
            ],
        ),
        (
            sentiment,
            _append_argv(append_escape, file="../keep.txt"),
        ),
    ):
        code, payload, stderr = _run(module, argv)
        assert code == 1, stderr
        assert "path must stay inside the workspace" in payload["error"]
    assert not (tmp_path / "keep.txt").exists()
    assert not (tmp_path / "selected.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"

    linked_workspace = tmp_path / "linked-workspace"
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    os.symlink(real_workspace, linked_workspace)
    for module, argv in (
        (weekly, ["--json", "check", "--workspace-dir", str(linked_workspace), "--file", "a.txt", "--date", _REPORT_DATE]),
        (sentiment, ["--json", "tail", "--workspace-dir", str(linked_workspace)]),
    ):
        code, payload, stderr = _run(module, argv)
        assert code == 1, stderr
        assert payload["error"] == "workspace is a symlink"

    file_link_workspace = tmp_path / "file-link"
    log_dir = file_link_workspace / "state" / "ct-semi-sentiment"
    log_dir.mkdir(parents=True)
    os.symlink(sentinel, log_dir / "sentiment.log")
    code, payload, stderr = _run(sentiment, _append_argv(file_link_workspace))
    assert code == 1, stderr
    assert payload["error"] == "refusing symlink"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (log_dir / "sentiment.log").is_symlink()

    parent_link_workspace = tmp_path / "parent-link"
    parent_link_workspace.mkdir()
    parent_outside = tmp_path / "parent-outside"
    parent_outside.mkdir()
    os.symlink(parent_outside, parent_link_workspace / "state")
    code, payload, stderr = _run(sentiment, _append_argv(parent_link_workspace))
    assert code == 1, stderr
    assert "path must stay inside the workspace" in payload["error"]
    assert (parent_link_workspace / "state").is_symlink()
    assert list(parent_outside.iterdir()) == []

    report_workspace = tmp_path / "report-link"
    report = report_workspace / "temp" / "ct-semi-sentiment" / _SUMMARY_DATE / "report.txt"
    report.parent.mkdir(parents=True)
    report.write_text(_daily_report(_SUMMARY_DATE, temperature="3/10 ↓，仓位偏谨慎。"), encoding="utf-8")
    check_log_dir = report_workspace / "state" / "ct-semi-sentiment"
    check_log_dir.mkdir(parents=True)
    os.symlink(sentinel, check_log_dir / "sentiment.log")
    code, payload, stderr = _run(
        sentiment,
        [
            "--json",
            "check",
            "--workspace-dir",
            str(report_workspace),
            "--file",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/report.txt",
            "--date",
            _SUMMARY_DATE,
        ],
    )
    assert code == 1, stderr
    assert payload["error"] == "refusing symlink"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (check_log_dir / "sentiment.log").is_symlink()

    select_workspace = tmp_path / "select-link"
    select_workspace.mkdir()
    (select_workspace / "raw.json").write_text(
        json.dumps({"accounts": {}, "x_search": {"ok": False, "items": []}}),
        encoding="utf-8",
    )
    select_outside = tmp_path / "select-outside"
    select_outside.mkdir()
    os.symlink(select_outside, select_workspace / "temp")
    code, payload, stderr = _run(
        sentiment,
        [
            "--json",
            "select",
            "--workspace-dir",
            str(select_workspace),
            "--as-of",
            _END_ISO,
            "--input",
            "raw.json",
            "--output",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/selected.json",
        ],
    )
    assert code == 1, stderr
    assert "path must stay inside the workspace" in payload["error"]
    assert (select_workspace / "temp").is_symlink()
    assert list(select_outside.iterdir()) == []

    for module, folder in ((weekly, tmp_path / "weekly-bytes"), (sentiment, tmp_path / "sentiment-bytes")):
        folder.mkdir()
        os.symlink(sentinel, folder / "draft.txt")
        _raises_value(lambda path=folder / "draft.txt", writer=module.write_bytes_exclusive: writer(path, b"new"), "refusing symlink")
        assert sentinel.read_text(encoding="utf-8") == "keep"
        assert (folder / "draft.txt").is_symlink()


def test_documented_commands_run_in_skill_order(tmp_path: Path) -> None:
    weekly = _weekly()
    sentiment = _sentiment()
    workspace = tmp_path / "ws"
    weekly_report = workspace / "temp" / "weekly-trend-impact-scan" / f"{_REPORT_DATE}.txt"
    weekly_report.parent.mkdir(parents=True)
    weekly_report.write_text(_weekly_report(_REPORT_DATE), encoding="utf-8")
    day = workspace / "temp" / "ct-semi-sentiment" / _SUMMARY_DATE
    day.mkdir(parents=True)
    accounts = {handle: {"ok": True, "has_more": False, "items": []} for handle in sentiment.HANDLE_ORDER}
    (day / "raw.json").write_text(
        json.dumps({"accounts": accounts, "x_search": {"ok": False, "items": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (day / "report.txt").write_text(
        _daily_report(_SUMMARY_DATE, temperature="3/10，仓位偏谨慎。"),
        encoding="utf-8",
    )
    replacements = {
        "<workspace>": str(workspace),
        "<report_date>": _REPORT_DATE,
        "<summary_date>": _SUMMARY_DATE,
        "<end_iso>": _END_ISO,
        "<line>": _LINE,
    }
    weekly_argvs = _skill_argvs("dalton-weekly-trend-impact-scan", replacements)
    daily_argvs = _skill_argvs("dalton-ct-semi-sentiment", replacements)
    assert weekly_argvs == [
        ["--json", "window"],
        [
            "--json",
            "check",
            "--workspace-dir",
            str(workspace),
            "--file",
            f"temp/weekly-trend-impact-scan/{_REPORT_DATE}.txt",
            "--date",
            _REPORT_DATE,
        ],
    ]
    assert daily_argvs == [
        ["--json", "window"],
        ["--json", "tail", "--workspace-dir", str(workspace)],
        [
            "--json",
            "select",
            "--workspace-dir",
            str(workspace),
            "--as-of",
            _END_ISO,
            "--input",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/raw.json",
            "--output",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/selected.json",
        ],
        [
            "--json",
            "check",
            "--workspace-dir",
            str(workspace),
            "--file",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/report.txt",
            "--date",
            _SUMMARY_DATE,
        ],
        [
            "--json",
            "append",
            "--workspace-dir",
            str(workspace),
            "--delivery-confirmed",
            "true",
            "--status",
            "sent",
            "--content-kind",
            "text",
            "--date",
            _SUMMARY_DATE,
            "--line",
            _LINE,
        ],
    ]
    for argv in weekly_argvs:
        code, payload, stderr = _run(weekly, argv)
        assert code == 0, (argv, payload, stderr)
        if argv[1] == "check":
            assert payload["trends"] == 3
    for argv in daily_argvs:
        code, payload, stderr = _run(sentiment, argv)
        assert code == 0, (argv, payload, stderr)
        if argv[1] == "tail":
            assert payload["exists"] is False
        if argv[1] == "check":
            assert not (workspace / "state").exists()
        if argv[1] == "select":
            assert payload["failed"] == []
            assert payload["no_posts"] == list(sentiment.HANDLE_ORDER)
            assert payload["supplemented"] == 0
        if argv[1] == "append":
            assert payload["reused"] is False
    log = workspace / "state" / "ct-semi-sentiment" / "sentiment.log"
    saved = log.read_bytes()
    code, payload, stderr = _run(sentiment, daily_argvs[-1])
    assert code == 0, (payload, stderr)
    assert payload["reused"] is True
    assert log.read_bytes() == saved
    code, payload, stderr = _run(
        sentiment,
        ["--json", "--workspace-dir", str(workspace), "tail"],
    )
    assert code == 0, (payload, stderr)
    assert payload["lines"] == [_LINE]
    assert not (workspace / "state").is_symlink()


def test_sentiment_coverage_and_utc_window() -> None:
    from datetime import timezone

    scripts_dalton = REPO_ROOT / "scripts" / "dalton"
    if str(scripts_dalton) not in sys.path:
        sys.path.insert(0, str(scripts_dalton))
    import researchdata_bridge as rb

    sentiment = _sentiment()

    # 1. NY 20:30 is UTC next day (01:30Z): since/until must use UTC dates so late-evening NY posts survive bridge filtering.
    win_evening = sentiment.compute_window("2026-11-12T20:30:00-05:00")
    assert win_evening["summary_date"] == "2026-11-12"
    assert win_evening["start_date"] == "2026-11-12"
    assert win_evening["end_date"] == "2026-11-13"
    bridge_evening = rb._normalize_x_timeline_output(
        {
            "items": [
                {
                    "id": "888",
                    "text": "20:00 NY post (01:00 UTC next day)",
                    "createdAt": "Fri Nov 13 01:00:00 +0000 2026",
                    "user": {"screenName": "xiaomustock", "name": "xiaomustock"},
                }
            ],
            "hasMore": False,
        },
        operation="tweets",
        count=50,
        since=win_evening["start_date"],
        until=win_evening["end_date"],
    )
    assert len(bridge_evening["items"]) == 1

    # 2. Coverage matrix matching daily reports:
    #    - 49 in-window + 2 pre-window truncated to 50 -> covered (oldest_returned_at < start)
    #    - 51 in-window + 1 pre-window truncated to 50 -> incomplete (oldest_returned_at >= start)
    #    - old pinned tweet at index 0 + in-window tweet with hasMore=True -> incomplete (pinned excluded)
    #    - nested empty + has_more=True + x_search supplement -> incomplete, note omits timestamp
    #    - truncated + oldest_returned_at=None + oldest_fetched_at >= start -> incomplete, note uses oldest_fetched_at
    win = sentiment.compute_window(_END_ISO)
    start = datetime.fromisoformat(win["start_iso"])

    raw_49 = []
    for idx in range(49):
        dt_utc = (start + timedelta(minutes=25 * (49 - idx))).astimezone(timezone.utc)
        raw_49.append(
            {
                "id": str(500 + idx),
                "text": f"covered-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "xiaomustock", "name": "xiaomustock"},
            }
        )
    for idx, mins in enumerate((1, 10)):
        dt_utc = (start - timedelta(minutes=mins)).astimezone(timezone.utc)
        raw_49.append(
            {
                "id": str(498 - idx),
                "text": f"before-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "xiaomustock", "name": "xiaomustock"},
            }
        )
    bridge_covered = rb._normalize_x_timeline_output(
        {"items": raw_49, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    assert bridge_covered["truncated"] is True

    raw_51 = []
    for idx in range(51):
        dt_utc = (start + timedelta(minutes=20 * (51 - idx))).astimezone(timezone.utc)
        raw_51.append(
            {
                "id": str(600 + idx),
                "text": f"trunc-inc-{idx}",
                "createdAt": dt_utc.strftime("%a %b %d %H:%M:%S +0000 %Y"),
                "user": {"screenName": "nft_hu", "name": "nft_hu"},
            }
        )
    raw_51.append(
        {
            "id": "599",
            "text": "pre-51",
            "createdAt": (start - timedelta(minutes=1)).astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "nft_hu", "name": "nft_hu"},
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

    bridge_pinned = rb._normalize_x_timeline_output(
        {
            "items": [
                {
                    "id": "901",
                    "text": "old pinned",
                    "createdAt": "Thu Jan 01 00:00:00 +0000 2026",
                    "user": {"screenName": "octopusycc", "name": "octopusycc"},
                },
                {
                    "id": "902",
                    "text": "in-window post",
                    "createdAt": "Thu Nov 12 10:00:00 +0000 2026",  # 05:00 NY
                    "user": {"screenName": "octopusycc", "name": "octopusycc"},
                },
            ],
            "hasMore": True,
        },
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )

    accounts = {
        handle: {"ok": True, "has_more": False, "truncated": False, "oldest_fetched_at": None, "oldest_returned_at": None, "items": []}
        for handle in sentiment.HANDLE_ORDER
    }
    accounts["xiaomustock"] = {"ok": True, "status": "ok", "data": bridge_covered}
    accounts["nft_hu"] = {"ok": True, "status": "ok", "data": bridge_51}
    accounts["octopusycc"] = {"ok": True, "status": "ok", "data": bridge_pinned}
    accounts["bitfool1"] = {
        "ok": True,
        "status": "empty",
        "data": {"items": [], "returned": 0, "has_more": True, "truncated": False, "oldest_fetched_at": None, "oldest_returned_at": None},
    }
    accounts["blknoiz06"] = {
        "ok": True,
        "status": "ok",
        "data": {
            "items": [],
            "returned": 0,
            "has_more": False,
            "truncated": True,
            "oldest_fetched_at": "2026-11-12T13:00:00+00:00",  # 08:00 NY > end
            "oldest_returned_at": None,
        },
    }
    accounts["notthreadguy"] = {
        "ok": True,
        "status": "ok",
        "data": {
            "items": [],
            "has_more": True,
            "truncated": False,
            "oldest_fetched_at": (start - timedelta(minutes=5)).astimezone(timezone.utc).isoformat(),
            "oldest_returned_at": "2026-11-12T10:00:00+00:00",
        },
    }
    selected = sentiment.select_posts(
        {
            "accounts": accounts,
            "x_search": {
                "ok": True,
                "items": [
                    {
                        "author": "bitfool1",
                        "url": "https://x.com/bitfool1/status/777",
                        "date": "2026-11-12T06:00:00-05:00",
                        "summary_excerpt": "from x_search only",
                    }
                ],
            },
        },
        win,
    )
    assert selected["has_more"] == ["nft_hu", "octopusycc", "bitfool1", "blknoiz06"]
    assert selected["incomplete"] == ["nft_hu", "octopusycc", "bitfool1", "blknoiz06"]
    assert selected["accounts"]["xiaomustock"]["incomplete"] is False
    assert selected["accounts"]["notthreadguy"]["incomplete"] is False
    notes = selected["incomplete_notes"]
    expected_nft_ny = (start + timedelta(minutes=40)).astimezone(sentiment.TZ).strftime("%Y-%m-%d %H:%M:%S")
    assert f"@nft_hu 窗口内可能还有未取回的帖子，最早只取到 {expected_nft_ny}（纽约时间）" in notes
    assert "@octopusycc 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 05:00:00（纽约时间）" in notes
    assert "@bitfool1 窗口内可能还有未取回的帖子" in notes
    assert not any(n.startswith("@bitfool1 窗口内可能还有未取回的帖子，") for n in notes)
    assert "@blknoiz06 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 08:00:00（纽约时间）" in notes

    # 3. Unified possible wording for both 50+1 pre-window and 51 in-window truncated timelines (NY time).
    raw_50_plus_1 = [
        {
            "id": str(700 + idx),
            "text": f"in-50-{idx}",
            "createdAt": (start + timedelta(minutes=20 * (50 - idx))).astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "degentradingLSD", "name": "degentradingLSD"},
        }
        for idx in range(50)
    ]
    raw_50_plus_1.append(
        {
            "id": "699",
            "text": "pre-50",
            "createdAt": (start - timedelta(minutes=1)).astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "degentradingLSD", "name": "degentradingLSD"},
        }
    )
    bridge_50_plus_1 = rb._normalize_x_timeline_output(
        {"items": raw_50_plus_1, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    raw_51_only = [
        {
            "id": str(800 + idx),
            "text": f"in-51-only-{idx}",
            "createdAt": (start + timedelta(minutes=20 * (51 - idx))).astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "user": {"screenName": "qinbafrank", "name": "qinbafrank"},
        }
        for idx in range(51)
    ]
    bridge_51_only = rb._normalize_x_timeline_output(
        {"items": raw_51_only, "hasMore": False},
        operation="tweets",
        count=50,
        since=win["start_date"],
        until=win["end_date"],
    )
    dist_accounts = {
        handle: {"ok": True, "has_more": False, "truncated": False, "oldest_fetched_at": None, "oldest_returned_at": None, "items": []}
        for handle in sentiment.HANDLE_ORDER
    }
    dist_accounts["degentradingLSD"] = {"ok": True, "status": "ok", "data": bridge_50_plus_1}
    dist_accounts["qinbafrank"] = {"ok": True, "status": "ok", "data": bridge_51_only}
    dist_sel = sentiment.select_posts({"accounts": dist_accounts, "x_search": {"ok": False, "items": []}}, win)
    expected_degen_ny = (start + timedelta(minutes=20)).astimezone(sentiment.TZ).strftime("%Y-%m-%d %H:%M:%S")
    expected_qin_ny = (start + timedelta(minutes=40)).astimezone(sentiment.TZ).strftime("%Y-%m-%d %H:%M:%S")
    assert "incomplete_certain" not in dist_sel["accounts"]["qinbafrank"]
    assert f"@degentradingLSD 窗口内可能还有未取回的帖子，最早只取到 {expected_degen_ny}（纽约时间）" in dist_sel["incomplete_notes"]
    assert f"@qinbafrank 窗口内可能还有未取回的帖子，最早只取到 {expected_qin_ny}（纽约时间）" in dist_sel["incomplete_notes"]
    assert all("窗口内帖子超过已取回的数量" not in n for n in dist_sel["incomplete_notes"])


def test_sentiment_ny_dst_fallback_compares_in_utc() -> None:
    from datetime import timezone

    sentiment = _sentiment()
    # 2026-11-01 is NY DST fall-back day (06:00 UTC = 02:00 EDT -> 01:00 EST).
    # 05:35 UTC = 01:35-04:00 (EDT); 06:15 UTC = 01:15-05:00 (EST).
    # Window starting at 2026-11-01T05:45:00Z (01:45-04:00) is covered by 05:35 UTC.
    win = sentiment.compute_window("2026-11-02T00:45:00-05:00")
    assert datetime.fromisoformat(win["start_iso"]).astimezone(timezone.utc) == datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc)
    legacy_items = [
        {"id": "2", "author_handle": "xiaomustock", "created_at": "2026-11-01T06:15:00Z", "text": "post at 06:15Z"},
        {"id": "1", "author_handle": "xiaomustock", "created_at": "2026-11-01T05:35:00Z", "text": "post at 05:35Z"},
    ]
    assert sentiment._oldest_timeline_item(legacy_items, "xiaomustock") == datetime(2026, 11, 1, 5, 35, tzinfo=timezone.utc)

    accounts = {handle: {"ok": True, "has_more": False, "items": []} for handle in sentiment.HANDLE_ORDER}
    accounts["xiaomustock"] = {"ok": True, "has_more": True, "items": legacy_items}
    selected = sentiment.select_posts({"accounts": accounts, "x_search": {"ok": False, "items": []}}, win)
    assert selected["accounts"]["xiaomustock"]["incomplete"] is False
    assert selected["accounts"]["xiaomustock"]["oldest_created_at"] == "2026-11-01T01:35:00-04:00"
    assert selected["incomplete_notes"] == []

    # Also verify when both 05:35Z and 06:15Z are inside a window starting at 05:20Z: 05:35Z is chosen as oldest.
    win_in = sentiment.compute_window("2026-11-02T00:20:00-05:00")
    selected_in = sentiment.select_posts({"accounts": accounts, "x_search": {"ok": False, "items": []}}, win_in)
    assert selected_in["accounts"]["xiaomustock"]["incomplete"] is True
    assert selected_in["accounts"]["xiaomustock"]["oldest_created_at"] == "2026-11-01T01:35:00-04:00"
    assert [row["id"] for row in selected_in["accounts"]["xiaomustock"]["items"]] == ["1", "2"]
    assert "@xiaomustock 窗口内可能还有未取回的帖子，最早只取到 2026-11-01 01:35:00（纽约时间）" in selected_in["incomplete_notes"]


def test_check_requires_incomplete_notes_in_data_notes(tmp_path: Path) -> None:
    sentiment = _sentiment()
    note_a = "@xiaomustock 窗口内可能还有未取回的帖子，最早只取到 2026-11-12 05:00:00（纽约时间）"
    note_b = "@bitfool1 窗口内可能还有未取回的帖子"
    base_report = _daily_report(_SUMMARY_DATE, temperature="3/10，仓位偏谨慎。")

    # Direct validate_report checks: omitted or placed outside 【数据备注】 fails; verbatim inside 【数据备注】 passes.
    missing_errs = sentiment.validate_report(
        base_report, _SUMMARY_DATE, require_change=False, incomplete_notes=[note_a, note_b]
    )
    assert any("数据备注缺少未取全提示" in err and note_a in err and note_b in err for err in missing_errs)

    wrong_section = base_report.replace("【共识主线】今日一致关注存储价格。", f"【共识主线】今日一致关注存储价格。\n{note_a}", 1) + note_b + "\n"
    wrong_errs = sentiment.validate_report(
        wrong_section, _SUMMARY_DATE, require_change=False, incomplete_notes=[note_a, note_b]
    )
    assert any(note_a in err and note_b not in err for err in wrong_errs)

    complete_report = base_report + f"{note_a}\n{note_b}\n"
    assert sentiment.validate_report(complete_report, _SUMMARY_DATE, require_change=False, incomplete_notes=[note_a, note_b]) == []

    # End-to-end select -> check via CLI: default selected.json path enforces every incomplete_note.
    workspace = tmp_path / "ws"
    day = workspace / "temp" / "ct-semi-sentiment" / _SUMMARY_DATE
    day.mkdir(parents=True)
    accounts = {
        handle: {"ok": True, "has_more": False, "truncated": False, "oldest_fetched_at": None, "oldest_returned_at": None, "items": []}
        for handle in sentiment.HANDLE_ORDER
    }
    accounts["xiaomustock"] = {
        "ok": True,
        "has_more": True,
        "truncated": False,
        "oldest_fetched_at": "2026-11-12T10:00:00+00:00",
        "oldest_returned_at": "2026-11-12T10:00:00+00:00",
        "items": [],
    }
    accounts["bitfool1"] = {
        "ok": True,
        "has_more": False,
        "truncated": True,
        "oldest_fetched_at": None,
        "oldest_returned_at": None,
        "items": [],
    }
    (day / "raw.json").write_text(
        json.dumps({"accounts": accounts, "x_search": {"ok": True, "items": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    code, sel_out, stderr = _run(
        sentiment,
        [
            "--json",
            "select",
            "--workspace-dir",
            str(workspace),
            "--as-of",
            _END_ISO,
            "--input",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/raw.json",
            "--output",
            f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/selected.json",
        ],
    )
    assert code == 0, (sel_out, stderr)
    assert sel_out["incomplete_notes"] == [note_a, note_b]

    report_path = day / "report.txt"
    check_argv = [
        "--json",
        "check",
        "--workspace-dir",
        str(workspace),
        "--file",
        f"temp/ct-semi-sentiment/{_SUMMARY_DATE}/report.txt",
        "--date",
        _SUMMARY_DATE,
    ]
    report_path.write_text(base_report, encoding="utf-8")
    code, payload, stderr = _run(sentiment, check_argv)
    assert code == 1, (payload, stderr)
    assert payload["error"] == "invalid_report"
    assert any(note_a in err and note_b in err for err in payload["errors"])

    report_path.write_text(base_report + note_a + "\n", encoding="utf-8")
    code, payload, stderr = _run(sentiment, check_argv)
    assert code == 1, (payload, stderr)
    assert any(note_b in err and note_a not in err for err in payload["errors"])

    report_path.write_text(complete_report, encoding="utf-8")
    code, payload, stderr = _run(sentiment, check_argv)
    assert code == 0, (payload, stderr)
    assert payload["ok"] is True

    # Symlinked selected.json is refused.
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"incomplete_notes": []}), encoding="utf-8")
    (day / "selected.json").unlink()
    os.symlink(outside, day / "selected.json")
    code, payload, stderr = _run(sentiment, check_argv)
    assert code == 1, (payload, stderr)
    assert payload["error"] == "refusing symlink"
