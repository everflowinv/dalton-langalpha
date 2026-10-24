"""``check_language``: quote anchoring, the two-round sidecar, and fail-closed
behavior (no sidecar on a failed review; round 2 refuses to precede round 1).

Every LLM call goes through an autospec'd ``assist_json``; the backend is an
autospec'd sandbox backend serving one draft from a dict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.llm.assist import AssistResult, assist_json
from src.dalton.publish import language as language_mod
from src.dalton.publish.language import run_check_language
from ptc_agent.agent.backends.sandbox import SandboxBackend

DRAFT = "drafts/2026-10-23-call-META.md"
DRAFT_TEXT = "# Conviction Call\n\n值得注意的是，本轮实现了对回测框架的调整。\n\nFY26Q3 revenue beat consensus by 4%（公司 8-K [引用3]）。\n"


def _backend(files: dict[str, str], fail_writes: frozenset[str] = frozenset()) -> Any:
    backend = create_autospec(SandboxBackend, instance=True)

    async def _aread(path: str) -> str | None:
        return files.get(path)

    async def _awrite(path: str, text: str) -> bool:
        if path in fail_writes:
            return False  # the real backend's refused write
        files[path] = text
        return True

    backend.aread_text.side_effect = _aread
    backend.awrite_text.side_effect = _awrite
    return backend


def _ok_result(issues: list[dict[str, Any]]) -> AssistResult:
    return AssistResult(ok=True, data={"issues": issues}, model="test-lang-model")


def _issue(quote: str, issue_type: str = "negation_defensive") -> dict[str, str]:
    return {
        "section": "Conviction Call",
        "quote": quote,
        "issue_type": issue_type,
        "why": "不承载独有信息的预告",
        "suggestion": "删除该引导语",
    }


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _verify(workspace_id: str, user_id: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(language_mod, "verify_workspace_access", _verify)
    async def _model(user_id: str | None = None) -> str:
        return "test-lang-model"

    async def _timeout(user_id: str | None = None) -> float:
        return 60.0

    monkeypatch.setattr(language_mod, "resolve_dalton_language_model", _model)
    monkeypatch.setattr(language_mod, "resolve_dalton_audit_timeout_s", _timeout)


async def _run_round(
    files: dict[str, str],
    draft: str,
    round_no: int,
    monkeypatch: pytest.MonkeyPatch,
    results: list[AssistResult],
    backend: Any = None,
) -> dict[str, Any]:
    stub = create_autospec(
        assist_json,
        side_effect=lambda *a, **k: results.pop(0) if results else _ok_result([]),
    )
    monkeypatch.setattr(language_mod, "assist_json", stub)
    return await run_check_language(
        workspace_id="ws-1",
        draft_path=draft,
        round=round_no,
        backend=backend if backend is not None else _backend(files),
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_round1_drops_unquotable_issues_and_counts_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT}
    good = _issue("值得注意的是，")
    fabricated = _issue("这句话不在草稿里")
    res = await _run_round(
        files, DRAFT, 1, monkeypatch, [_ok_result([good, fabricated])]
    )

    assert res["ok"] is True
    assert res["dropped_quotes"] == 1
    assert [i["quote"] for i in res["issues"]] == [good["quote"]]
    doc = json.loads(files[f"{DRAFT}.language.json"])
    assert doc["rounds"][0]["round"] == 1
    assert doc["rounds"][0]["dropped_quotes"] == 1
    assert doc["rounds"][0]["draft_sha256"] == hashlib.sha256(
        DRAFT_TEXT.encode("utf-8")
    ).hexdigest()
    # The human checklist quotes the surviving issue verbatim.
    assert good["quote"] in files[f"{DRAFT}.language.md"]


@pytest.mark.asyncio
async def test_two_rounds_append_into_the_same_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT}
    res1 = await _run_round(files, DRAFT, 1, monkeypatch, [_ok_result([])])
    assert res1["ok"] is True

    # The analyst revised between rounds: a different draft text.
    files[DRAFT] = DRAFT_TEXT + "\n新增一段。\n"
    res2 = await _run_round(files, DRAFT, 2, monkeypatch, [_ok_result([])])

    assert res2["ok"] is True
    doc = json.loads(files[f"{DRAFT}.language.json"])
    assert [r["round"] for r in doc["rounds"]] == [1, 2]
    assert doc["rounds"][1]["draft_sha256"] != doc["rounds"][0]["draft_sha256"]


@pytest.mark.asyncio
async def test_round2_on_unchanged_draft_still_runs_and_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT}
    await _run_round(files, DRAFT, 1, monkeypatch, [_ok_result([])])

    res = await _run_round(files, DRAFT, 2, monkeypatch, [_ok_result([])])

    assert res["ok"] is True
    assert res["unchanged_since_round_1"] is True
    assert "第 1 轮之后未修改" in res["message"]
    doc = json.loads(files[f"{DRAFT}.language.json"])
    assert doc["rounds"][1]["unchanged_since_round_1"] is True


@pytest.mark.asyncio
async def test_model_failure_writes_no_sidecar_and_is_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT}
    res = await _run_round(
        files,
        DRAFT,
        1,
        monkeypatch,
        [AssistResult(ok=False, data=None, error="model down", status="error")],
    )

    assert res["ok"] is False
    assert "ERROR" in res["message"]
    assert f"{DRAFT}.language.json" not in files
    assert f"{DRAFT}.language.md" not in files


@pytest.mark.asyncio
async def test_round2_before_round1_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {DRAFT: DRAFT_TEXT}
    res = await _run_round(files, DRAFT, 2, monkeypatch, [])

    assert res["ok"] is False
    assert "第 2 轮必须在第 1 轮之后" in res["message"]
    assert f"{DRAFT}.language.json" not in files


@pytest.mark.asyncio
async def test_round_must_be_1_or_2(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {DRAFT: DRAFT_TEXT}
    res = await _run_round(files, DRAFT, 3, monkeypatch, [])
    assert res["ok"] is False
    assert "round 只能是 1 或 2" in res["message"]


@pytest.mark.asyncio
async def test_unconfigured_model_is_an_error_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT}

    async def _none(user_id: str | None = None) -> str:
        return ""

    monkeypatch.setattr(language_mod, "resolve_dalton_language_model", _none)
    stub = create_autospec(assist_json)
    monkeypatch.setattr(language_mod, "assist_json", stub)

    res = await run_check_language(
        workspace_id="ws-1",
        draft_path=DRAFT,
        round=1,
        backend=_backend(files),
        user_id="user-1",
    )

    assert res["ok"] is False
    assert "语言审查未配置模型" in res["message"]
    stub.assert_not_called()
    assert f"{DRAFT}.language.json" not in files


@pytest.mark.asyncio
async def test_missing_draft_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    res = await _run_round({}, DRAFT, 1, monkeypatch, [])
    assert res["ok"] is False
    assert "not found" in res["message"]


@pytest.mark.asyncio
async def test_unparseable_existing_sidecar_is_never_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT, f"{DRAFT}.language.json": "{not json"}
    res = await _run_round(files, DRAFT, 1, monkeypatch, [])
    assert res["ok"] is False
    assert "不可解析" in res["message"]
    assert files[f"{DRAFT}.language.json"] == "{not json"


def test_split_sections_keeps_chunks_within_budget() -> None:
    section = "# 标题\n\n" + "段落内容。\n\n" * 60
    chunks = language_mod.split_sections(section, budget=400)
    assert len(chunks) > 1
    assert all(name == "标题" for name, _ in chunks)
    assert all(len(body) <= 500 for _, body in chunks)
    # Every chunk's text is a verbatim slice of the draft, so model quotes
    # stay findable in the full text.
    for _, body in chunks:
        assert body in section


# --------------------------------------------- formal-deliverable scope rule


def test_is_reviewable_deliverable_covers_the_user_scope() -> None:
    f = language_mod.is_reviewable_deliverable
    # Agent-written, human-readable Markdown under the three roots.
    for path in (
        "drafts/2026-10-23-background.md",
        "drafts/2026-10-23-initial-coverage.md",
        "research/2026-10-23-ic/bullbear-quick.md",
        "research/2026-10-23-ic/industry.md",
        "research/2026-10-23-ic/valuation.md",
        "research/tracker/2026-10-23-delta.md",
        "research/2026-10-23-flash-FY26Q3.md",
        "research/2026-10-23-topic-ai-capex/cross-check.md",
    ):
        assert f(path), path
    # Raw/machine files, scraped-data directories, renders and sidecars are not.
    for path in (
        "research/2026-10-23-ic/lenses.json",
        "research/2026-10-23-ic/comps.xlsx",
        "research/2026-10-23-ic/knowledge-proposal.json",
        "research/bg_data/xxx/transcript.md",
        "data/thesis.json",
        "research/2026-10-23-ic/data/raw.md",
        "lenses_work/note.md",
        "model_dump/summary.md",
        "thesis.md",  # knowledge-layer render, served at the workspace root
        "agenda.md",
        "verdicts.md",
        "agent.md",
        "published/INDEX.md",
        "published/2026-10-23-call-META.md",  # archive: its review is the draft's
        "research/topics/conversations/abc12345.md",  # machine-rendered digest
        "drafts/2026-10-23-initial-coverage.audit.md",
        "drafts/2026-10-23-initial-coverage.annotated.md",
        "drafts/2026-10-23-initial-coverage.md.language.md",
        "drafts/2026-10-23-initial-coverage.md.language.json",
        ".agents/scratchpad/abc12345/note.md",
        "scripts/dalton/notes.md",
    ):
        assert not f(path), path


def test_is_reviewable_deliverable_normalizes_spellings() -> None:
    f = language_mod.is_reviewable_deliverable
    assert f("./drafts/x.md")
    assert f("drafts/x.md") and f("/workspaces/ws-1/drafts/x.md", "/workspaces/ws-1")
    assert f("research/r/note.md|abcdef")  # evidence entry, sha tail stripped
    assert not f("/workspaces/other/drafts/x.md", "/workspaces/ws-1")


# ------------------------------------- negation_defensive / translationese


RULES_SAMPLE = (
    "# 深度点评\n\n"
    "值得注意的是，本季的驱动并不是价格因素，而是基本面驱动。客观来说，我们并不认为这意味着需求见顶。\n\n"
    "3Q 打印显示营收落地在 512 亿美元，beat 了预期；指引的上修这对于估值来说是一个积极的信号。\n\n"
    "公司在电话会上未披露 TPU 外售金额；本轮未取得分部毛利数据，不加仓（管理层 8-K [引用3]）。\n"
)


def test_review_instructions_carry_the_two_new_rule_families() -> None:
    """The prompt embeds the SKILL rules plus an explicit pointer, so both new
    issue types and their must-keep boundaries reach the review model."""
    instructions = language_mod._review_instructions(1)
    for token in ("negation_defensive", "translationese", "3Q 打印", "beat 了预期"):
        assert token in instructions, token
    # The boundary: fact-bearing negations stay, and English terms are exempt.
    assert "承载事实" in instructions and "未披露" in instructions
    assert "术语" in instructions and "guidance" in instructions


@pytest.mark.asyncio
async def test_new_issue_types_accepted_and_bogus_types_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: RULES_SAMPLE}
    res = await _run_round(
        files,
        DRAFT,
        1,
        monkeypatch,
        [
            _ok_result(
                [
                    _issue("值得注意的是，", "negation_defensive"),
                    _issue("3Q 打印", "translationese"),
                    _issue("beat 了预期", "translationese"),
                    _issue("公司在电话会上未披露 TPU 外售金额", "negation_defensive"),
                    {**_issue("指引的上修", "made_up_type")},
                ]
            )
        ],
    )

    assert res["ok"] is True
    types = [i["issue_type"] for i in res["issues"]]
    assert types.count("negation_defensive") == 2
    assert types.count("translationese") == 2
    assert "made_up_type" not in types
    assert res["dropped_issue_types"] == 1
    assert "issue_type 非法" in res["message"]
    doc = json.loads(files[f"{DRAFT}.language.json"])
    assert doc["rounds"][0]["dropped_issue_types"] == 1
    # The checklist renders the two new families.
    assert "3Q 打印" in files[f"{DRAFT}.language.md"]


# ------------------------------- round-4: sidecar write failures, real schema


@pytest.mark.asyncio
async def test_checklist_write_failure_fails_the_round_without_a_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {DRAFT: DRAFT_TEXT}
    backend = _backend(files, fail_writes=frozenset({f"{DRAFT}.language.md"}))
    res = await _run_round(files, DRAFT, 1, monkeypatch, [], backend=backend)

    assert res["ok"] is False
    assert "清单写入失败" in res["message"]
    # Neither half of the pair exists: no json-without-checklist state.
    assert f"{DRAFT}.language.json" not in files
    assert f"{DRAFT}.language.md" not in files


@pytest.mark.asyncio
async def test_record_write_failure_fails_the_round_checklist_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The checklist is written first, the record last — a failed record
    write leaves the harmless half (checklist without record), never a
    record the contract would trust."""
    files = {DRAFT: DRAFT_TEXT}
    backend = _backend(files, fail_writes=frozenset({f"{DRAFT}.language.json"}))
    res = await _run_round(files, DRAFT, 1, monkeypatch, [], backend=backend)

    assert res["ok"] is False
    assert "记录写入失败" in res["message"]
    assert f"{DRAFT}.language.md" in files
    assert f"{DRAFT}.language.json" not in files


@pytest.mark.asyncio
async def test_invalid_issue_type_dropped_through_real_assist_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """assist_json's own jsonschema validation runs (only the lowest-level
    model call is stubbed): the schema keeps issue_type a string, so one
    stray value survives validation and is dropped per item by the tool."""
    from src.dalton.llm import assist as assist_mod
    from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
    from src.dalton.settings import get_dalton_settings

    real_model = get_dalton_settings().publish.audit_model

    async def _resolve(user_id: str | None = None) -> str:
        return real_model

    monkeypatch.setattr(language_mod, "resolve_dalton_language_model", _resolve)

    issues = [
        {
            "section": "深度点评",
            "quote": "值得注意的是，",
            "issue_type": "negation_defensive",
            "why": "预防式开场",
            "suggestion": "删除",
        },
        {
            "section": "深度点评",
            "quote": "3Q 打印",
            "issue_type": "translationese",
            "why": "print 直译",
            "suggestion": "改为 3Q 财报",
        },
        {
            "section": "深度点评",
            "quote": "落地在 512 亿美元",
            "issue_type": "made_up_type",
            "why": "x",
            "suggestion": "x",
        },
    ]

    async def _complete(**kwargs: Any) -> DaltonCompleteResult:
        return DaltonCompleteResult(
            ok=True,
            status="done",
            data={"issues": issues},
            text=json.dumps({"issues": issues}),
            model=real_model,
        )

    monkeypatch.setattr(
        assist_mod, "dalton_complete", create_autospec(dalton_complete, side_effect=_complete)
    )

    files = {DRAFT: RULES_SAMPLE}
    res = await run_check_language(
        workspace_id="ws-1",
        draft_path=DRAFT,
        round=1,
        backend=_backend(files),
        user_id="user-1",
    )

    assert res["ok"] is True, res["message"]
    assert [i["issue_type"] for i in res["issues"]] == [
        "negation_defensive",
        "translationese",
    ]
    assert res["dropped_issue_types"] == 1


@pytest.mark.asyncio
async def test_check_language_tool_resolves_home_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from src.dalton.auth import (
        resolve_caller_home_workspace_id as _real_resolve,
        verify_workspace_access as _real_verify,
    )
    from src.dalton.tools import publish_tools as publish_tools_mod
    from src.dalton.tools.publish_tools import create_language_tools, create_publish_tools

    push_draft = "research/push/2026-10-23-director-weekly.md"
    files = {push_draft: "## 结论\n本周核心覆盖无重大证伪。\n"}
    stub_assist = create_autospec(assist_json, return_value=_ok_result([]))
    monkeypatch.setattr(language_mod, "assist_json", stub_assist)

    stub_resolve = create_autospec(_real_resolve, return_value="ws-home-1")
    monkeypatch.setattr(publish_tools_mod, "resolve_caller_home_workspace_id", stub_resolve)

    seen_ws: list[str] = []

    async def _verify(workspace_id: str, user_id: str) -> dict[str, Any]:
        seen_ws.append(workspace_id)
        return {}

    monkeypatch.setattr(
        language_mod,
        "verify_workspace_access",
        create_autospec(_real_verify, side_effect=_verify),
    )

    call_payload = {
        "name": "check_language",
        "args": {"draft_path": push_draft, "round": 1},
        "id": "call-home-1",
        "type": "tool_call",
    }

    # Analyst check_language must NOT resolve Home when workspace_id is missing.
    analyst_lang = next(
        t for t in create_publish_tools(_backend(files)) if t.name == "check_language"
    )
    analyst_msg = await analyst_lang.ainvoke(
        call_payload,
        config={"configurable": {"user_id": "user-1"}},
    )
    assert analyst_msg.content.startswith("ERROR:")
    stub_resolve.assert_not_awaited()
    assert f"{push_draft}.language.json" not in files

    # Chief of Staff check_language resolves Home when workspace_id is missing.
    cos_tool = create_language_tools(_backend(files))[0]
    msg = await cos_tool.ainvoke(
        call_payload,
        config={"configurable": {"user_id": "user-1"}},
    )
    assert "语言审查第 1 轮完成" in msg.content
    stub_resolve.assert_awaited_once_with("user-1")
    assert seen_ws == ["ws-home-1"]
    assert f"{push_draft}.language.json" in files

    # Exception from resolve_caller_home_workspace_id is caught and returned as ERROR string.
    stub_resolve_err = create_autospec(
        _real_resolve,
        side_effect=HTTPException(status_code=503, detail="Database unavailable"),
    )
    monkeypatch.setattr(
        publish_tools_mod, "resolve_caller_home_workspace_id", stub_resolve_err
    )
    err_msg = await cos_tool.ainvoke(
        call_payload,
        config={"configurable": {"user_id": "user-1"}},
    )
    assert err_msg.content == "ERROR: Database unavailable"
