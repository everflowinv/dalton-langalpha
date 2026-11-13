"""对话审计 F2：语言审查失败分类、盘中量比、尚未评估、半日市、卡片冲突、转写说话人。

替身一律 ``create_autospec``。本文件不声称已经跑过。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

from src.dalton.conversations.extractor import enforce_speaker_and_agent_citation
from src.dalton.conversations.render import render_conversation_digest_md
from src.dalton.database.conversations import build_card_search_text
from src.dalton.knowledge.render import render_verdicts_md
from src.dalton.knowledge.validate import validate_verdict
from src.dalton.llm.assist import AssistResult, assist_json
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
from src.dalton.publish import language as language_mod
from src.dalton.tools.publish_tools import (
    _PARSE_FAILURE_SENTENCE,
    create_publish_tools,
)
from ptc_agent.agent.backends.sandbox import SandboxBackend
from ptc_agent.core.paths import DEFAULT_LAYOUT, resolve_agent_path

REPO_ROOT = Path(__file__).resolve().parents[3]
DRAFT = "drafts/2026-10-23-call-META.md"
DRAFT_TEXT = "# Conviction Call\n\n值得注意的是，本轮实现了对回测框架的调整。\n"
WS_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "user-1"
_MOVE_QUANT = REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/scripts/move_quant.py"
_CARD_IO = REPO_ROOT / "plugins/dalton/skills/dalton-x-accounts-daily/scripts/card_io.py"
_STOCK_SKILL = REPO_ROOT / "plugins/dalton/skills/dalton-stock-move/SKILL.md"
_CONV_SKILL = REPO_ROOT / "plugins/dalton/skills/dalton-conventions/SKILL.md"
_EVENT_VERDICTS = (
    REPO_ROOT / "plugins/dalton/skills/dalton-conventions/references/event-verdicts.md"
)
_X_SKILL = REPO_ROOT / "plugins/dalton/skills/dalton-x-accounts-daily/SKILL.md"
_VERDICT_SCHEMA = REPO_ROOT / "src/dalton/knowledge/schemas/verdict.schema.json"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@lru_cache(maxsize=1)
def _move_quant() -> Any:
    return _load("move_quant_f2_under_test", _MOVE_QUANT)


@lru_cache(maxsize=1)
def _card_io() -> Any:
    return _load("card_io_f2_under_test", _CARD_IO)


def _backend(files: dict[str, str]) -> Any:
    backend = create_autospec(SandboxBackend, instance=True)
    backend.workspace_dir = DEFAULT_LAYOUT.root
    backend.computer_root = DEFAULT_LAYOUT.root

    def _normalize(path: str) -> str:
        return resolve_agent_path(
            path,
            workspace=DEFAULT_LAYOUT.root,
            root=DEFAULT_LAYOUT.root,
            allowed=DEFAULT_LAYOUT.allowed_directories,
        )

    async def _aread(path: str) -> str | None:
        return files.get(path)

    async def _awrite(path: str, text: str) -> bool:
        files[path] = text
        return True

    backend.normalize_path.side_effect = _normalize
    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    return backend


# The helper may run twice in one test; autospec the real functions, never a stub.
_REAL_VERIFY = language_mod.verify_workspace_access
_REAL_MODEL = language_mod.resolve_dalton_language_model
_REAL_TIMEOUT = language_mod.resolve_dalton_audit_timeout_s


def _patch_language_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        language_mod,
        "verify_workspace_access",
        create_autospec(_REAL_VERIFY, return_value={}),
    )

    async def _model(user_id: str | None = None) -> str:
        return "test-lang-model"

    async def _timeout(user_id: str | None = None) -> float:
        return 60.0

    monkeypatch.setattr(
        language_mod,
        "resolve_dalton_language_model",
        create_autospec(_REAL_MODEL, side_effect=_model),
    )
    monkeypatch.setattr(
        language_mod,
        "resolve_dalton_audit_timeout_s",
        create_autospec(_REAL_TIMEOUT, side_effect=_timeout),
    )


async def _invoke_check_language(
    monkeypatch: pytest.MonkeyPatch,
    result: AssistResult,
    *,
    files: dict[str, str] | None = None,
) -> str:
    _patch_language_env(monkeypatch)

    async def _assist(*args: Any, **kwargs: Any) -> AssistResult:
        return result

    monkeypatch.setattr(
        language_mod,
        "assist_json",
        create_autospec(assist_json, side_effect=_assist),
    )
    held = files if files is not None else {DRAFT: DRAFT_TEXT}
    tool = next(t for t in create_publish_tools(_backend(held)) if t.name == "check_language")
    msg = await tool.ainvoke(
        {
            "name": "check_language",
            "args": {"draft_path": DRAFT, "round": 1},
            "id": "call-f2-lang",
            "type": "tool_call",
        },
        config={"configurable": {"user_id": USER_ID, "workspace_id": "ws-1"}},
    )
    return msg.content if hasattr(msg, "content") else str(msg)


def _verdict(**overrides: Any) -> dict[str, Any]:
    base = {
        "at": "2026-11-02T00:00:00Z",
        "id": "VD-20261102-01",
        "trigger": {"kind": "price_move"},
        "event": "单日 +6% 异动归因",
        "verdict": "NO_CHANGE",
        "rationale": "板块 beta 带动，无个股层面事件",
        "next_validation": {"what": "次日开盘确认无个股公告", "due": "2026-11-03"},
        "progress": "none",
    }
    base.update(overrides)
    return base


def _completion(*, text: str, ok: bool = True, status: str = "done") -> DaltonCompleteResult:
    return DaltonCompleteResult(
        ok=ok,
        status=status,
        data=None,
        text=text,
        model="gemini-3.8-flash-low-oauth",
        cash_usd=0.0,
    )


# ── 1. 语言审查 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_language_parse_failure_is_typed_and_repeat_names_the_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次解析失败都带回 ok/类型/可重试，规定原句只出现一次。成功路径仍是纯文本。"""
    failure = AssistResult(
        ok=False,
        error="Expecting value: line 1 column 1 (char 0)",
        error_kind="JSONDecodeError",
        status="failed",
    )
    first = json.loads(await _invoke_check_language(monkeypatch, failure))
    assert first["ok"] is False
    assert first["error_type"] == "parse_failure"
    assert first["retryable"] is True
    assert first["message"].count(_PARSE_FAILURE_SENTENCE) == 1
    assert "或检查语言审查模型配置" not in first["message"]
    assert "第 1 轮语言审查调用失败" not in first["message"]
    assert "不算通过" not in first["message"]

    second = json.loads(await _invoke_check_language(monkeypatch, failure))
    assert second["ok"] is False
    assert second["error_type"] == "parse_failure"
    assert second["message"].count(_PARSE_FAILURE_SENTENCE) == 1

    success = await _invoke_check_language(
        monkeypatch,
        AssistResult(ok=True, data={"issues": []}, model="test-lang-model"),
    )
    assert "语言审查第 1 轮完成" in success
    assert not success.lstrip().startswith("{")


@pytest.mark.asyncio
async def test_assist_json_retries_structured_output_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """结构化输出失败只再送一次修复提示；超时不重试。"""
    seen: list[dict[str, Any]] = []

    async def _both_bad(*args: Any, **kwargs: Any) -> DaltonCompleteResult:
        seen.append(kwargs)
        return _completion(text="not json")

    monkeypatch.setattr(
        "src.dalton.llm.assist.dalton_complete",
        create_autospec(dalton_complete, side_effect=_both_bad),
    )
    schema = {"type": "object", "properties": {"issues": {"type": "array"}}, "required": ["issues"]}
    bad = await assist_json(
        "audit.judge",
        "只输出 JSON",
        {"excerpt": "x"},
        schema,
        user_id="user-1",
        model="gemini-3.8-flash-low-oauth",
    )
    assert bad.ok is False
    assert bad.error_kind == "JSONDecodeError"
    assert len(seen) == 2
    assert str(seen[1]["scope_ref"]).endswith(":parse_repair")
    assert "请只修正 JSON 结构" in seen[1]["system_prompt"]

    timeout_seen: list[int] = []

    async def _timeout(*args: Any, **kwargs: Any) -> DaltonCompleteResult:
        timeout_seen.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(
        "src.dalton.llm.assist.dalton_complete",
        create_autospec(dalton_complete, side_effect=_timeout),
    )
    timed = await assist_json(
        "audit.judge",
        "只输出 JSON",
        {"excerpt": "y"},
        schema,
        user_id="user-1",
        model="gemini-3.8-flash-low-oauth",
    )
    assert timed.ok is False
    assert timed.error_kind == "TimeoutError"
    assert timeout_seen == [1]


@pytest.mark.asyncio
async def test_assist_json_repair_success_sums_both_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """修复成功时费用和用量是两次调用之和，不是只留第二次。"""
    calls = {"n": 0}

    async def _repair_then_ok(*args: Any, **kwargs: Any) -> DaltonCompleteResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return DaltonCompleteResult(
                ok=True,
                status="done",
                text="not json",
                model="gemini-3.8-flash-low-oauth",
                cash_usd=0.2,
                usage={"input_tokens": 10, "output_tokens": 1},
            )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            text='{"issues": []}',
            model="gemini-3.8-flash-low-oauth",
            cash_usd=0.3,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setattr(
        "src.dalton.llm.assist.dalton_complete",
        create_autospec(dalton_complete, side_effect=_repair_then_ok),
    )
    schema = {"type": "object", "properties": {"issues": {"type": "array"}}, "required": ["issues"]}
    good = await assist_json(
        "audit.judge",
        "只输出 JSON",
        {"excerpt": "z"},
        schema,
        user_id="user-1",
        model="gemini-3.8-flash-low-oauth",
    )
    assert good.ok is True
    assert good.data == {"issues": []}
    assert good.cost_usd == pytest.approx(0.5)
    assert good.usage["input_tokens"] == 14
    assert good.usage["output_tokens"] == 3
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_assist_json_repair_after_cached_response_bills_only_live_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首个响应来自缓存时只计修复那一次真实调用。"""
    calls = {"n": 0}

    async def _cached_bad_then_live_ok(*args: Any, **kwargs: Any) -> DaltonCompleteResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return DaltonCompleteResult(
                ok=True,
                status="done",
                text="not json",
                model="gemini-3.8-flash-low-oauth",
                cash_usd=0.2,
                usage={"input_tokens": 10, "output_tokens": 1},
                cached=True,
            )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            text='{"issues": []}',
            model="gemini-3.8-flash-low-oauth",
            cash_usd=0.3,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setattr(
        "src.dalton.llm.assist.dalton_complete",
        create_autospec(dalton_complete, side_effect=_cached_bad_then_live_ok),
    )
    schema = {"type": "object", "properties": {"issues": {"type": "array"}}, "required": ["issues"]}
    good = await assist_json(
        "audit.judge", "只输出 JSON", {"excerpt": "c"}, schema,
        user_id="user-1", model="gemini-3.8-flash-low-oauth",
    )
    assert good.ok is True
    assert good.cost_usd == pytest.approx(0.3)
    assert good.usage["input_tokens"] == 4


# ── 2. 盘中成交量 ───────────────────────────────────────────────────


def _us_frames() -> tuple[Any, Any, str]:
    import pandas as pd

    dates = pd.bdate_range("2026-06-01", periods=30)
    close = pd.DataFrame(
        {"NVDA": [100.0 + i for i in range(30)], "QQQ": [400.0 + i for i in range(30)]},
        index=dates,
    )
    volume = pd.DataFrame(
        {"NVDA": [100] * 29 + [40], "QQQ": [1000] * 30},
        index=dates,
    )
    end = dates[-1].date().isoformat()
    return close, volume, end


def _quant_at(as_of: datetime) -> dict[str, Any]:
    close, volume, end = _us_frames()
    mq = _move_quant()
    return mq.compute_quant_from_frames(
        target="NVDA",
        peers=[],
        sector_etf="QQQ",
        adj_close_df=close,
        close_df=close,
        volume_df=volume,
        window_str="1d",
        end_override=end,
        as_of=as_of,
    )


def test_intraday_volume_is_progress_scaled_and_close_is_unchanged() -> None:
    import pandas as pd

    end = pd.bdate_range("2026-06-01", periods=30)[-1]
    day = end.date()
    ny = ZoneInfo("America/New_York")

    open_bar = _quant_at(datetime(day.year, day.month, day.day, 14, 0, tzinfo=ny))
    assert open_bar["ok"] is True
    assert open_bar["volume_vs_adtv"] == 0.4
    assert open_bar["session_complete"] is False
    assert open_bar["session_elapsed_minutes"] == 270
    assert open_bar["session_length_minutes"] == 390
    assert open_bar["session_progress"] == round(270 / 390, 4)
    assert open_bar["volume_vs_adtv_label"] == "截至当前的累计成交量占日均量比例"
    assert open_bar["volume_comparison"]["basis"] == "session_progress_scaled"
    assert "折算" in open_bar["volume_comparison"]["note"]
    assert open_bar["volume_comparison"]["scaled_vs_adtv"] == round(0.4 / (270 / 390), 2)
    assert open_bar["daily"][-1]["session_complete"] is False

    before = _quant_at(datetime(day.year, day.month, day.day, 8, 0, tzinfo=ny))
    assert before["session_complete"] is False
    assert before["session_elapsed_minutes"] == 0
    assert before["session_progress"] == 0.0
    assert before["volume_comparison"]["scaled_vs_adtv"] is None
    assert before["volume_vs_adtv"] == 0.4

    closed = _quant_at(datetime(day.year, day.month, day.day, 16, 30, tzinfo=ny))
    assert closed["session_complete"] is True
    assert closed["volume_vs_adtv"] == 0.4
    assert "volume_vs_adtv_label" not in closed
    assert "volume_comparison" not in closed
    assert "session_complete" not in closed["daily"][-1]


def test_same_clock_volume_ratio_uses_intraday_stamps() -> None:
    import pandas as pd

    idx = [pd.Timestamp("2026-06-01 10:00"), pd.Timestamp("2026-06-02 10:00")]
    volume = pd.DataFrame({"NVDA": [10.0, 30.0]}, index=idx)
    as_of = datetime(2026, 6, 2, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    ratio = _move_quant()._same_clock_volume_ratio("NVDA", volume, as_of.date(), as_of)
    assert ratio == 3.0


def test_us_early_close_shortens_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """提前收市日用当天实际收盘计算时段长度，过了收盘即算已收盘。"""
    import pandas as pd

    mq = _move_quant()

    def _early(day: Any, et_tz: Any) -> tuple[int, bool]:
        return 13 * 60, True

    monkeypatch.setattr(mq, "_resolve_regular_close_minutes", _early)
    end = pd.bdate_range("2026-06-01", periods=30)[-1]
    day = end.date()
    ny = ZoneInfo("America/New_York")

    midday = _quant_at(datetime(day.year, day.month, day.day, 12, 0, tzinfo=ny))
    assert midday["session_complete"] is False
    assert midday["session_length_minutes"] == 210
    assert midday["session_elapsed_minutes"] == 150
    assert midday["session_progress"] == round(150 / 210, 4)
    assert "session_calendar" not in midday

    after = _quant_at(datetime(day.year, day.month, day.day, 14, 0, tzinfo=ny))
    assert after["session_complete"] is True
    assert "volume_comparison" not in after


def test_hk_session_declares_regular_calendar_only() -> None:
    """港股没有交易所日历时标明 regular_only，并注明提前收市日折算偏高。"""
    import pandas as pd

    dates = pd.bdate_range("2026-06-01", periods=30)
    close = pd.DataFrame(
        {"0700.HK": [100.0 + i for i in range(30)], "QQQ": [400.0 + i for i in range(30)]},
        index=dates,
    )
    volume = pd.DataFrame({"0700.HK": [100] * 29 + [40], "QQQ": [1000] * 30}, index=dates)
    day = dates[-1].date()
    hk = ZoneInfo("Asia/Hong_Kong")
    res = _move_quant().compute_quant_from_frames(
        target="0700.HK",
        peers=[],
        sector_etf="QQQ",
        adj_close_df=close,
        close_df=close,
        volume_df=volume,
        window_str="1d",
        end_override=day.isoformat(),
        as_of=datetime(day.year, day.month, day.day, 10, 30, tzinfo=hk),
    )
    assert res["ok"] is True
    assert res["session_complete"] is False
    assert res["session_calendar"] == "regular_only"
    assert res["session_length_minutes"] == 330
    assert "未识别半日市，提前收市日的折算值偏高" in res["volume_comparison"]["note"]
    assert any("未识别半日市，提前收市日的折算值偏高" in item for item in res["warnings"])


def test_stock_move_skill_forbids_raw_intraday_volume_call() -> None:
    text = _STOCK_SKILL.read_text(encoding="utf-8")
    assert "session_complete" in text
    assert "单独判断缩量或放量" in text
    assert "scaled_vs_adtv" in text
    assert "session_progress_scaled" in text
    assert "按盘中时段进度折算" in text
    assert "截至当前的累计成交量占日均量比例" in text
    assert 'session_calendar` 为 `regular_only`' in text
    assert "未识别半日市，提前收市日的折算值偏高" in text


# ── 3. 尚未评估与差额 ───────────────────────────────────────────────


def test_missing_payoff_basis_is_not_yet_evaluated_and_legacy_rows_stay_valid() -> None:
    schema = json.loads(_VERDICT_SCHEMA.read_text(encoding="utf-8"))
    payoff_schema = schema["properties"]["payoff_assessment"]
    assert payoff_schema["required"] == ["changed", "reason"]
    assert payoff_schema["additionalProperties"] is False
    for key in ("comparable", "expected_return_delta", "required_probability_delta"):
        assert key in payoff_schema["properties"]
        assert key not in payoff_schema["required"]

    missing = validate_verdict(
        _verdict(payoff_assessment={"comparable": False, "changed": False, "reason": "none"})
    )
    assert missing.errors == []
    legacy = validate_verdict(_verdict(payoff_assessment={"changed": False, "reason": "none"}))
    assert legacy.errors == []
    moved = validate_verdict(
        _verdict(
            payoff_assessment={
                "changed": False,
                "reason": "none",
                "expected_return_delta": "+4.5个百分点",
                "required_probability_delta": "-10个百分点",
            }
        )
    )
    assert moved.errors == []
    illegal = validate_verdict(
        _verdict(
            payoff_assessment={
                "comparable": False,
                "changed": True,
                "reason": "price_crossed_band",
            }
        )
    )
    assert any(err["code"] == "payoff_not_comparable" for err in illegal.errors)

    unevaluated = render_verdicts_md(
        [
            _verdict(
                payoff_assessment={
                    "comparable": False,
                    "changed": False,
                    "reason": "none",
                    "expected_return_delta": "+4.5个百分点",
                    "required_probability_delta": "-10个百分点",
                }
            )
        ],
        ticker="NVDA",
    )
    assert "尚未评估" in unevaluated
    assert "+4.5个百分点" in unevaluated
    assert "-10个百分点" in unevaluated

    unchanged = render_verdicts_md(
        [_verdict(payoff_assessment={"changed": False, "reason": "none"})],
        ticker="NVDA",
    )
    assert "赔率未变" in unchanged
    assert "尚未评估" not in unchanged

    listed = render_verdicts_md(
        [
            _verdict(
                payoff_assessment={
                    "changed": False,
                    "reason": "none",
                    "expected_return_delta": "+4.5个百分点",
                }
            )
        ],
        ticker="NVDA",
    )
    assert "赔率未变" in listed
    assert "+4.5个百分点" in listed
    assert "尚未评估" not in listed


def test_payoff_skill_text_separates_unevaluated_from_unchanged() -> None:
    stock = _STOCK_SKILL.read_text(encoding="utf-8")
    assert "尚未评估" in stock
    assert '"comparable": false' in stock
    assert "expected_return_delta" in stock
    assert "+4.5 个百分点" in stock

    conventions = _CONV_SKILL.read_text(encoding="utf-8")
    assert "收益风险" not in conventions
    assert "两者都没变" not in conventions
    assert "需推送的 price_move（verdict 非 NO_CHANGE 或赔率发生实质变化）" in conventions
    assert "payoff_assessment.changed` 为 `false`" in conventions
    assert "尚未评估" in conventions

    events = _EVENT_VERDICTS.read_text(encoding="utf-8")
    assert "尚未评估" in events
    assert "仍必须向 PM 推送" in events
    assert 'payoff_assessment.changed` 为 `true`' in events
    assert "若仅因股价在原价带内波动产生差额、未触发下列五种情形之一" in events


# ── 4. 卡片冲突：不提供删除入口 ─────────────────────────────────────


def test_card_conflict_names_recovery_and_has_no_delete_entry() -> None:
    io = _card_io()
    assert not hasattr(io, "delete_card")
    source = Path(io.__file__).read_text(encoding="utf-8")
    assert "def delete_card" not in source
    assert "版本流程" not in source
    assert "report_conflict" not in source
    assert "summary.md" in source
    assert "card.json" in source
    assert "start_iso" in source
    assert "x-accounts-daily-<summary_date>" in source

    text = _X_SKILL.read_text(encoding="utf-8")
    assert "card_conflict" in text
    assert "summary.md" in text
    assert "card.json" in text
    assert "start_iso" in text
    assert "end_iso" in text
    assert "report_run_id" in text
    assert 'automation_report(action="status", report_run_id=RUN_ID)' in text
    assert 'automation_report(action="submit"' in text
    assert "禁止删除或覆盖已有卡片文件" in text
    assert "unlink" in text
    assert "content_conflict" in text
    assert "版本流程" not in text
    assert "report_conflict" not in text


# ── 6. 转写说话人与上传者 ───────────────────────────────────────────


def _owner_event(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "speaker": {
            "name": "Ever",
            "role": "owner",
            "is_owner": True,
            "sender_id_hash": "owner-1",
        },
    }


def test_transcript_separates_viewpoint_author_from_uploader() -> None:
    text = "转写：\nAlice: 我们看好云资本开支。\nBob: 我不同意。\n"
    speaker, eff_type, _sources = enforce_speaker_and_agent_citation(
        "opinion",
        [],
        _owner_event(text),
        quote="我们看好云资本开支。",
    )
    assert eff_type == "opinion"
    assert speaker["name"] == "Alice"
    assert speaker["role"] == "participant"
    assert speaker["is_owner"] is False
    assert speaker["material"] is True
    assert speaker["uploaded_by"]["name"] == "Ever"
    assert speaker["uploaded_by"]["role"] == "owner"

    both, _, _ = enforce_speaker_and_agent_citation(
        "opinion",
        [],
        _owner_event(text),
        quote="我们看好云资本开支。\nBob: 我不同意。",
    )
    assert both["name"] == "未识别"
    assert both["uploaded_by"]["name"] == "Ever"

    ordinary, _, _ = enforce_speaker_and_agent_citation(
        "opinion",
        [],
        _owner_event("结论：维持覆盖。"),
        quote="维持覆盖。",
    )
    assert ordinary["name"] == "Ever"
    assert ordinary["role"] == "owner"
    assert "uploaded_by" not in ordinary

    agent, agent_type, _ = enforce_speaker_and_agent_citation(
        "opinion",
        [],
        {"message_role": "assistant", "text": text},
        quote="我们看好云资本开支。",
    )
    assert agent["role"] == "agent"
    assert "uploaded_by" not in agent
    assert agent_type == "opinion"


def test_headings_minutes_mention_and_bold_view_keep_the_sender() -> None:
    """小标题、只提到纪要、Markdown 强调的观点，作者仍是发送者。"""
    cases = (
        "收入：增长。利润：改善。",
        "收入：增长。\n利润：改善。\n",
        "看了下专家纪要，认为公司会加快资本开支。",
        "**观点**：维持覆盖。",
    )
    for text in cases:
        speaker, _, _ = enforce_speaker_and_agent_citation(
            "opinion",
            [],
            _owner_event(text),
            quote="维持覆盖。" if "观点" in text else text[:8],
        )
        assert speaker["name"] == "Ever", text
        assert speaker["role"] == "owner", text
        assert "uploaded_by" not in speaker, text


def test_attachment_without_speakers_keeps_sender_and_records_uploader() -> None:
    event = _owner_event("请看附件。")
    event["files"] = [{"name": "memo.pdf"}]
    speaker, _, _ = enforce_speaker_and_agent_citation(
        "opinion",
        [],
        event,
        quote="请看附件。",
    )
    assert speaker["name"] == "Ever"
    assert speaker["role"] == "owner"
    assert speaker["uploaded_by"]["name"] == "Ever"
    assert "material" not in speaker


def test_search_and_digest_show_uploader_and_author() -> None:
    card = {
        "card_id": "c1",
        "type": "opinion",
        "claim": "看好云资本开支",
        "quote": "我们看好云资本开支。",
        "ts": "2026-11-08T15:00:00Z",
        "speaker": {
            "name": "Alice",
            "role": "participant",
            "material": True,
            "uploaded_by": {"name": "Ever", "role": "owner"},
        },
    }
    search_text = build_card_search_text(card)
    assert "Alice" in search_text
    assert "Ever" in search_text
    legacy = build_card_search_text(
        {"claim": "看好云资本开支", "speaker": {"name": "Ever", "role": "owner"}}
    )
    assert legacy == "看好云资本开支 | Ever"
    assert "上传" not in legacy

    digest = render_conversation_digest_md([card], date_et="2026-11-08")
    assert "观点作者" in digest
    assert "Alice" in digest
    assert "上传者" in digest
    assert "Ever" in digest

    plain = render_conversation_digest_md(
        [
            {
                "type": "opinion",
                "claim": "维持覆盖",
                "speaker": {"name": "Ever", "role": "owner"},
                "ts": "2026-11-08T15:00:00Z",
            }
        ],
        date_et="2026-11-08",
    )
    assert "说话人" in plain
    assert "上传者" not in plain


@pytest.mark.asyncio
async def test_conversation_search_annotates_uploader(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.dalton.tools.conversation_tools as tools

    card = {
        "claim": "看好云资本开支",
        "quote": "我们看好云资本开支。",
        "speaker": {
            "name": "Alice",
            "role": "participant",
            "uploaded_by": {"name": "Ever", "role": "owner", "is_owner": True},
        },
    }

    async def _search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "count": 1, "cards": [dict(card)]}

    monkeypatch.setattr(
        tools,
        "get_user_tier",
        create_autospec(tools.get_user_tier, return_value="internal"),
    )
    monkeypatch.setattr(
        tools,
        "hybrid_search_conversations",
        create_autospec(tools.hybrid_search_conversations, side_effect=_search),
    )
    raw = await tools.conversation_search.ainvoke(
        {"query": "云资本开支"},
        config={"configurable": {"user_id": USER_ID, "workspace_id": WS_ID}},
    )
    text = raw.content if hasattr(raw, "content") else raw
    body = json.loads(text)
    found = body["cards"][0]
    assert found["viewpoint_author"] == "Alice"
    assert found["uploaded_by"]["name"] == "Ever"
    assert found["attribution"]["viewpoint_author"] == "Alice"
    assert found["attribution"]["uploaded_by"]["name"] == "Ever"


def test_heading_labels_and_repeated_label_are_not_a_transcript() -> None:
    from src.dalton.conversations.extractor import _body_is_multi_speaker_transcript

    assert not _body_is_multi_speaker_transcript("看了专家纪要\n公司：增长\n产品：改善")
    assert not _body_is_multi_speaker_transcript("专家纪要\n张总：第一点\n张总：第二点")
    assert _body_is_multi_speaker_transcript("专家纪要\n张总：需求在改善\n李经理：价格还在跌")


def test_quote_said_by_two_speakers_has_no_single_author() -> None:
    from src.dalton.conversations.extractor import _viewpoint_author

    body = "会议纪要\nAlice: 同意。\nBob: 同意。"
    assert _viewpoint_author(body, "同意。") == "未识别"
    assert _viewpoint_author("会议纪要\nAlice: 需求改善。\nBob: 价格下跌。", "价格下跌。") == "Bob"


def test_card_create_never_replaces_an_existing_file(tmp_path: Any) -> None:
    io = _card_io()
    target = tmp_path / "card.json"
    assert io.write_bytes_new(target, b"first") is True
    assert io.write_bytes_new(target, b"second") is False
    assert target.read_bytes() == b"first"
    assert [p.name for p in tmp_path.iterdir()] == ["card.json"]


def test_material_marker_words_are_never_speakers() -> None:
    from src.dalton.conversations.extractor import _body_is_multi_speaker_transcript

    assert not _body_is_multi_speaker_transcript("转发：老板的邮件。\n张三：明天开会。")


def test_quote_matching_two_speakers_up_to_whitespace_has_no_single_author() -> None:
    from src.dalton.conversations.extractor import _viewpoint_author

    body = "会议纪要\nAlice: demand  improved.\nBob: demand improved."
    assert _viewpoint_author(body, "demand improved.") == "未识别"
