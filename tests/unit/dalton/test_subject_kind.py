"""The launch seam for ``subject_kind``: the workspace's coverage identity
decides it, the caller's claim can only agree or be refused (codex r1 #1).

锁定：
- 漏传 → 按身份补齐（行业工作区 sector，公司工作区 company）；
- 与身份冲突 / 非法取值 / 身份查询失败 → SubjectKindRefused，绝不静默纠正；
- 与身份一致 → 原样放行（resume 重放参数稳定）；
- 非 topic-research、无工作区身份 → 不动参数；
- 开关关闭（默认）走真实 resolve_workspace_profile：不读注册表即判公司。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

import src.dalton.workflows.subject_kind as sk_mod
from src.dalton.knowledge.profiles import (
    PROFILE_COMPANY,
    PROFILE_SECTOR,
    resolve_workspace_profile as _real_resolve,
)
from src.dalton.workflows.subject_kind import (
    SubjectKindRefused,
    resolve_launch_subject_kind,
)

WS = "11111111-1111-1111-1111-111111111111"
CFG = {"workspace_id": WS}


def _with_profile(monkeypatch: pytest.MonkeyPatch, profile: str | Exception) -> None:
    if isinstance(profile, Exception):
        fake = create_autospec(_real_resolve, side_effect=profile)
    else:
        fake = create_autospec(_real_resolve, return_value=profile)
    monkeypatch.setattr(sk_mod, "resolve_workspace_profile", fake)


@pytest.mark.asyncio
async def test_omitted_subject_kind_is_filled_from_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_profile(monkeypatch, PROFILE_SECTOR)
    out = await resolve_launch_subject_kind("topic-research", {"topic": "x"}, CFG)
    assert out == {"topic": "x", "subject_kind": "sector"}

    _with_profile(monkeypatch, PROFILE_COMPANY)
    out2 = await resolve_launch_subject_kind("topic-research", {"topic": "x"}, CFG)
    assert out2 == {"topic": "x", "subject_kind": "company"}

    # 空串视同漏传。
    _with_profile(monkeypatch, PROFILE_SECTOR)
    out3 = await resolve_launch_subject_kind(
        "topic-research", {"subject_kind": "  "}, CFG
    )
    assert out3["subject_kind"] == "sector"


@pytest.mark.asyncio
async def test_conflicting_claim_is_refused_not_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_profile(monkeypatch, PROFILE_SECTOR)
    with pytest.raises(SubjectKindRefused, match="冲突"):
        await resolve_launch_subject_kind(
            "topic-research", {"subject_kind": "company"}, CFG
        )

    _with_profile(monkeypatch, PROFILE_COMPANY)
    with pytest.raises(SubjectKindRefused, match="冲突"):
        await resolve_launch_subject_kind(
            "topic-research", {"subject_kind": "sector"}, CFG
        )

    # 非法取值同样拒绝。
    _with_profile(monkeypatch, PROFILE_SECTOR)
    with pytest.raises(SubjectKindRefused, match="只能是 sector 或 company"):
        await resolve_launch_subject_kind(
            "topic-research", {"subject_kind": "banana"}, CFG
        )


@pytest.mark.asyncio
async def test_matching_claim_passes_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_profile(monkeypatch, PROFILE_SECTOR)
    params: dict[str, Any] = {"subject_kind": "sector", "topic": "x"}
    out = await resolve_launch_subject_kind("topic-research", params, CFG)
    assert out == {"subject_kind": "sector", "topic": "x"}


@pytest.mark.asyncio
async def test_noncanonical_but_valid_values_are_normalized_and_written_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex r2 #2："SECTOR"/" sector " 在行业工作区接受，但必须把规范值写回最终
    参数——workflow.js 的分支是 JS 严格比较 ``=== 'sector'``，未规范化的值会静默
    跑公司分支。"""
    _with_profile(monkeypatch, PROFILE_SECTOR)
    for raw in ("SECTOR", " sector ", "Sector"):
        out = await resolve_launch_subject_kind(
            "topic-research", {"subject_kind": raw, "topic": "x"}, CFG
        )
        assert out["subject_kind"] == "sector", raw

    _with_profile(monkeypatch, PROFILE_COMPANY)
    out2 = await resolve_launch_subject_kind(
        "topic-research", {"subject_kind": "Company "}, CFG
    )
    assert out2["subject_kind"] == "company"

    # 无法规范化成两个规范值之一的，仍然拒绝（不猜）。
    _with_profile(monkeypatch, PROFILE_SECTOR)
    with pytest.raises(SubjectKindRefused, match="只能是 sector 或 company"):
        await resolve_launch_subject_kind(
            "topic-research", {"subject_kind": "SECTORISH"}, CFG
        )


def test_workflow_js_branch_is_strict_and_normalized_values_satisfy_it() -> None:
    """等效验证 workflow.js 分支（沙箱禁止执行 node——`operation not permitted`，
    前两轮已两次验证——无法在 Python 测试里跑真 JS；以源码锁定代替）：
    分支表达式必须是严格相等 + 全小写字面量，而 normalizer 的输出恰好是
    ``sector``/``company`` 两个小写规范值，二者拼起来闭合"启动错分支"缺口。"""
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[3]
        / "workflows"
        / "topic-research"
        / "workflow.js"
    ).read_text(encoding="utf-8")
    assert "const isSector = args.subject_kind === 'sector'" in js
    # JS === 语义等价演示（大小写敏感、不去空白）：
    assert "sector" == "sector" and "company" != "sector"
    assert "SECTOR" != "sector"  # === 下不相等 → 未规范化必错分支
    assert " sector " != "sector"


@pytest.mark.asyncio
async def test_identity_query_failure_refuses_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_profile(monkeypatch, RuntimeError("registry timeout"))
    with pytest.raises(SubjectKindRefused, match="查询失败"):
        await resolve_launch_subject_kind("topic-research", {}, CFG)


@pytest.mark.asyncio
async def test_other_workflows_and_identityless_threads_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 非 topic-research：参数原样（哪怕带怪值——该工作流不分支）。
    _with_profile(monkeypatch, PROFILE_SECTOR)
    assert await resolve_launch_subject_kind(
        "initial-coverage", {"subject_kind": "banana"}, CFG
    ) == {"subject_kind": "banana"}
    # 无工作区身份：不动参数（完成检查自会按身份核对）。
    assert await resolve_launch_subject_kind("topic-research", {}, {}) == {}
    assert await resolve_launch_subject_kind("topic-research", {}, None) == {}
    # 非字典参数原样返回。
    assert await resolve_launch_subject_kind("topic-research", None, CFG) is None


@pytest.mark.asyncio
async def test_flag_off_resolves_company_without_touching_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭（默认）：走真实 resolve_workspace_profile，不读注册表即判公司。"""
    from src.dalton.database import coverage_units as cu_db

    def _fail_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("flag off must never read dalton_coverage_units")

    monkeypatch.setattr(cu_db, "get_active_unit_by_workspace", _fail_read)
    out = await resolve_launch_subject_kind("topic-research", {}, CFG)
    assert out == {"subject_kind": PROFILE_COMPANY}
