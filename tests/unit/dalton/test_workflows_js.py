"""The shipped Dalton workflows, run through the real QuickJS engine with a
scripted host: a `revise` verdict triggers revise→re-review, `block` stops
the run, and the return value carries `incomplete` from the evidence check.

The host answers by dispatch label, so these tests read the workflow the way
the driver drives it — same helpers, same failure semantics.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
    WorkflowLimits,
    run_workflow_script,
)

pytestmark = pytest.mark.xdist_group("serial")

REPO_ROOT = Path(__file__).resolve().parents[3]

# The full-coverage memo probe validates a draft's structure in process; the
# validator lives with the skill's scripts.
_FC_SCRIPTS = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-full-coverage" / "scripts"
if str(_FC_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_FC_SCRIPTS))

# A structure-complete full-coverage memo (passes validate_memo with 0 FAIL):
# the probe's "complete" fixture for the resume tests.
FC_COMPLETE_MEMO = """# 虚拟公司（TEST）投资备忘录

## 摘要

锚点：TEST。2Q26 收入同比增长 18%，OPM 13.2% [引用1]。

## 公司与商业模式

公司靠订阅赚钱，margin 来自切换成本。2Q26 续费率 95% [引用1]。

### 行业与竞争

行业分散，前三家合计份额 30% [引用2]。

## 业务质量

### Good Business：高切换成本支撑订阅续费与高回报

2Q26 续费率 95%，详见「公司与商业模式」[引用1]。

### Good People：创始人执掌且指引一贯稳健

详见「管理层」[引用1]。

### Good Price：现价隐含合理增长预期

详见「财务预测与估值」[引用3]。

### 综合判断

| 维度 | 评级 | 一句理由 |
|---|---|---|
| Good Business | 强 | 订阅续费率 95% [引用1] |
| Good People | 强 | 创始人执掌且指引稳健 [引用1] |
| Good Price | 合理 | 现价隐含合理增长 [引用3] |

好生意、好团队且价格合理，对应看多。

## 核心投资逻辑与争议

市场认为增长见顶 [引用2]；我们不同，因为新客户结构变了 [引用1]。

## 财务预测与估值

Base 目标价 120 美元 [引用3]。

## 风险

大客户流失：单一客户占收入 20%，丢单直接砍利润 [引用1]。

## 管理层

CEO 创始人，指引历来保守 [引用1]。

## 跟踪

季度净增客户数跌破 X 即证伪 [假设]。

## 附录

计算明细放这里，正文只写结论。

## 引用

[引用1]: f:rev_q8 （数字底表｜最近一季收入｜2Q26｜research/2026-10-23-fc/facts.md）
[引用2]: evidence:E-20261023-01 （法定财报｜虚拟公司｜FY2025 10-K｜2026-02-01｜accession 000000000-26-000001）
[引用3]: model:model/META-model-v01.xlsx!Scenarios!C12 （本方模型｜Base 目标价｜2026-10-23｜model/META-model-v01.xlsx）
"""

OUT = {"files": ["research/2026-10-23-ic/industry.md"], "summary": "done", "sha256": "a" * 64}


@dataclass
class ScriptedHost:
    """Answers ``agent()`` by label; records every dispatch."""

    by_label: dict[str, Any] = field(default_factory=dict)
    # Labels whose dispatch resolves to null (a failed child).
    fail_labels: set[str] = field(default_factory=set)
    calls: list[tuple[str, str]] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def evidence_default(self, prompt: str) -> Any:
        """The evidence child's honest answer when everything it was asked
        about exists: one ``path|sha256`` entry per ``- path`` prompt line."""
        paths = [
            line[2:].strip()
            for line in prompt.splitlines()
            if line.startswith("- ") and "|" not in line
        ]
        return {"files": [f"{p}|{'e' * 64}" for p in paths]}

    def branch_probe_default(self, prompt: str) -> Any:
        """The branch probe's answer when every probed file is on disk and
        non-empty: one ``path|ok`` entry per ``- path（至少 N 字节）`` line."""
        paths = [
            line[2:].split("（")[0].strip()
            for line in prompt.splitlines()
            if line.startswith("- ") and "|" not in line
        ]
        return {"files": [f"{p}|ok" for p in paths]}

    def outcome_for(self, label: str, prompt: str) -> Any:
        if label in self.fail_labels:
            return None
        if label in self.by_label:
            return self.by_label[label]
        if label == "规划":
            # Three clusters keep the topic run past the ≥3-cluster floor the
            # workflow gates on before cross-checking.
            return {
                "file": "research/2026-10-23-topic-ai-capex/plan.md",
                "clusters": [
                    {"id": "a", "name": "簇 A", "queries": ["q1", "q2"]},
                    {"id": "b", "name": "簇 B", "queries": ["q1", "q2"]},
                    {"id": "c", "name": "簇 C", "queries": ["q1", "q2"]},
                ],
                "questions": ["Q1"],
                "summary": "plan",
            }
        if label == "建模准备":
            m_map = re.search(r"写入 (\S*mapping\.md)", prompt)
            files = ["research/2026-10-23-fc/model_dump/META-fingerprint.json"]
            if m_map:
                files.append(m_map.group(1))
            return {
                "files": files,
                "summary": "prep ok",
                "mode": "update" if "起点路径为" in prompt else "build",
                "baseline_path": "model/META-model-v01.xlsx" if "起点路径为" in prompt else None,
                "baseline_sha256": "1" * 64 if "起点路径为" in prompt else None,
                "fingerprint_path": files[0],
                "consensus_path": "data/consensus/2026-10-23_0900.json",
                "baseline_health_warning": None,
            }
        if label == "读取运行ID":
            return {"run_id": "run-test-1"}
        if label == "模型构建与落盘":
            cand = "model/META-model-v02.xlsx" if "模式：update" in prompt else "model/META-model-v01.xlsx"
            m_cm = re.search(r'--coord-map-out "([^"]+)"', prompt)
            files = [cand]
            if m_cm:
                files.append(m_cm.group(1))
            return {
                "files": files,
                "summary": "model ok",
                "candidate_path": cand,
                "model_sha256": "a" * 64,
                "recalc_status": "ok",
            }
        if label == "一致预期对照":
            m_cand = re.search(r"候选模型 (\S+?\.xlsx)", prompt)
            cand = m_cand.group(1) if m_cand else "model/META-model-v01.xlsx"
            m_cm = re.search(r"生成 (\S*consensus-diff\.md) 与 (\S*consensus-diff\.json)", prompt)
            files = [cand]
            if m_cm:
                files.extend([m_cm.group(1), m_cm.group(2)])
            return {
                "files": files,
                "summary": "consensus ok",
                "candidate_path": cand,
                "model_sha256": "a" * 64,
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if label in ("模型本轮范围检查", "模型联动修订检查") or label.startswith("模型本轮范围复查 "):
            m_rd = re.search(r'--research-dir "([^"]+)"', prompt)
            rdir = m_rd.group(1) if m_rd else "."
            return {
                "files": [f"{rdir}/model_dump/model-check.md", f"{rdir}/model_dump/model-verification.json"],
                "summary": "check pass",
                "readable": True,
                "final_status": "pass",
                "model_sha256": "a" * 64,
                "actionable_count": 0,
                "actionable_issues": [],
                "baseline_legacy_issues": [],
                "missing_evidence_issues": [],
                "not_applicable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": None,
                "summary_10lines": "summary",
            }
        if label.startswith("模型修改 "):
            return {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "fixed",
                "candidate_path": "model/META-model-v01.xlsx",
                "model_sha256": "b" * 64,
                "recalc_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "模型变动拆解":
            m_delta = re.search(r"至 (\S*model-delta\.md)", prompt)
            return {
                "files": [m_delta.group(1) if m_delta else "research/2026-10-28-deep-FY26Q3/model-delta.md"],
                "summary": "delta ok",
            }
        if label == "写入降级账本":
            m_led = re.search(r'"([^"]*model_dump/model-verification\.json)"', prompt)
            m_rem = re.search(r'"([^"]*model_dump/remaining-issues\.md)"', prompt)
            return {
                "files": [
                    m_led.group(1) if m_led else "model_dump/model-verification.json",
                    m_rem.group(1) if m_rem else "model_dump/remaining-issues.md",
                ],
                "summary": "fallback ledger written",
                "final_status": "delivered_with_open_issues",
                "model_sha256": "a" * 64,
                "run_id": "run-test-1",
            }
        if label == "模型联动修订":
            m_cur = re.search(r"当前模型 (\S+?\.xlsx)", prompt)
            cur = m_cur.group(1) if m_cur else "model/META-model-v01.xlsx"
            return {
                "files": [cur],
                "summary": "no model change",
                "changed": False,
                "model_path": cur,
                "model_sha256": "a" * 64,
                "report_sha256": None,
                "recalc_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "memo 探测":
            m = re.search(r"f='([^']+)'", prompt)
            assert m, "the memo probe prompt must quote the draft path"
            return {"files": [f"{m.group(1)}|absent"]}
        if label == "产物核验":
            return self.evidence_default(prompt)
        if label == "支线核验":
            return self.branch_probe_default(prompt)
        if label == "草稿探测":
            return {
                "files": ["./drafts/2026-10-23-initial-coverage.md"],
                "draft_content": "absent",
            }
        if label == "赔率摘要核验":
            return {
                "files": ["./research/2026-10-23-ic/underwriting-summary.md"],
                "exists": True,
                "first_line": "已保存 thesis 版本 1",
            }
        if label == "赔率摘要复核":
            return {
                "files": [
                    "./research/2026-10-23-ic/underwriting-summary.md",
                    "./drafts/2026-10-23-initial-coverage.md",
                ],
                "exists": True,
                "first_line": "已保存 thesis 版本 1",
            }
        if label == "终稿审稿":
            return {"verdict": "pass", "summary": "ok", "must_fix": []}
        if label.startswith("修订 "):
            return {
                **OUT,
                "files": [
                    "./drafts/2026-10-23-initial-coverage.md",
                    "./research/2026-10-23-ic/questions.md",
                    "./research/2026-10-23-ic/underwriting-summary.md",
                ],
            }
        return dict(OUT)

    async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
        label = str(opts.get("label") or "")
        self.calls.append((label, str(opts.get("agentType"))))
        self.prompts.append(prompt)
        # A real child takes time; an instantly-resolved host future can land
        # the engine's pending-promise check before the JS microtasks drain.
        await asyncio.sleep(0.02)
        result = self.outcome_for(label, prompt)
        if (
            label == "初筛成稿"
            and isinstance(result, dict)
            and "underwriting_version" not in result
        ):
            result = {**result, "underwriting_version": 1}
        if label == "初筛成稿" and isinstance(result, dict) and result.pop("_strict_files", False):
            pass  # the test pins the writer's files as given
        elif label == "初筛成稿" and isinstance(result, dict):
            # The writer declares the payoff summary it read (the journal only
            # re-runs a dispatch whose declared files changed).
            m = re.search(r"(\S*underwriting-summary\.md)", prompt)
            files = list(result.get("files") or [])
            if m and m.group(1) not in files:
                result = {**result, "files": [*files, m.group(1)]}
        if label.startswith("修订 ") and isinstance(result, dict):
            if "needs_reunderwrite" not in result:
                result = {**result, "needs_reunderwrite": False}
            if "underwriting_version" not in result:
                result = {**result, "underwriting_version": 1}
        # 审稿契约（提示词第 1 批 #2）：dalton-reviewer 的回复必须逐项覆盖
        # 1–9 项检查，workflows 据此判结果有效。旧桩只写 verdict；按全部
        # 通过补齐 items，使这些用例仍考察 verdict 分支本身。
        if opts.get("agentType") == "dalton-reviewer" and isinstance(result, dict):
            if "items" not in result:
                result = {
                    **result,
                    "items": [
                        {"check": n, "result": "pass", "reason": "ok"}
                        for n in range(1, 10)
                    ],
                }
            if "files" not in result:
                review_files = [
                    "./drafts/2026-10-23-initial-coverage.md",
                    "./research/2026-10-23-ic/questions.md",
                    "./research/2026-10-23-ic/underwriting-summary.md",
                ]
                review_draft = opts.get("reviewDraft")
                if isinstance(review_draft, str) and review_draft not in review_files:
                    review_files.append(review_draft)
                result = {**result, "files": review_files}
        return result

    def phase(self, title: str) -> None:
        pass

    def log(self, message: str) -> None:
        self.logs.append(message)

    def labels(self) -> list[str]:
        return [label for label, _ in self.calls]


IC_PARAMS: dict[str, Any] = {
    "ticker": "META",
    "company": "Meta Platforms",
    "workspace_dir": ".",
    "as_of": "2026-10-23",
}


def _script(name: str) -> str:
    from ptc_agent.agent.middleware.background_subagent.workflow.prebuilt import (
        read_assembled_workflow_file,
    )

    return read_assembled_workflow_file(REPO_ROOT / "workflows" / name / "workflow.js")


async def _run(name: str, params: dict[str, Any], host: ScriptedHost) -> Any:
    """Run one shipped workflow against ``host``.

    These run the raw engine, not the driver — so the fixtures never put an
    empty string in a result: quickjs_rs's async host-call marshal deadlocks
    on ``""`` (the driver normalizes it to ``None`` at its boundary; see
    ``engine_safe`` in src/dalton/workflows/journal.py).
    """
    # The engine sees a create_autospec of the real WorkflowHost Protocol; the
    # scripted host only supplies the answers through side_effect.
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
    )

    spec_host = create_autospec(WorkflowHost, instance=True)
    spec_host.agent.side_effect = host.agent
    spec_host.phase.side_effect = host.phase
    spec_host.log.side_effect = host.log
    outcome = await run_workflow_script(
        _script(name), params, spec_host, WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0)
    )
    assert outcome.status == "completed", outcome.error
    return outcome.result


@pytest.mark.asyncio
async def test_revise_verdict_triggers_revision_and_re_review() -> None:
    host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "P0 未解决", "must_fix": ["补 Q3"]},
            "复审 1": {"verdict": "pass", "summary": "已解决", "must_fix": []},
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    labels = host.labels()
    assert labels.index("修订 1") > labels.index("审稿"), "revision must follow the review"
    assert labels.index("复审 1") > labels.index("修订 1"), "re-review must follow the revision"
    assert result["ok"] is True
    assert result["review_verdict"] == "pass"
    assert result["revise_rounds"] == 1
    assert result["incomplete"] == []
    # The evidence check is the last dispatch, and it is a dalton-evidence child.
    assert host.calls[-1] == ("产物核验", "dalton-evidence")


@pytest.mark.asyncio
async def test_two_revise_rounds_then_still_revise_reports_incomplete() -> None:
    revise = {"verdict": "revise", "summary": "仍有 P0", "must_fix": ["x"]}
    host = ScriptedHost(
        by_label={"审稿": revise, "复审 1": revise}
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["revise_rounds"] == 1
    assert any("审稿未最终通过" in item for item in result["incomplete"])
    assert host.labels().count("修订 1") == 1
    assert host.labels().count("修订 2") == 0


@pytest.mark.asyncio
async def test_block_verdict_stops_the_run_with_the_reason() -> None:
    host = ScriptedHost(
        by_label={"审稿": {"verdict": "block", "summary": "数字与底表矛盾", "must_fix": []}}
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert "数字与底表矛盾" in result["blocked"]
    assert any(item.startswith("审稿 block") for item in result["incomplete"])
    # Stopped means stopped: no revision, no evidence dispatch after the block.
    labels = host.labels()
    assert "修订 1" not in labels
    assert "产物核验" not in labels


@pytest.mark.asyncio
async def test_review_missing_check_items_is_a_failed_review_not_a_pass() -> None:
    """审稿契约（提示词第 1 批 #2）：items 未覆盖 1–9 项检查的审稿结果按
    审稿失败处理，不得作为 verdict 进入修订循环或收尾。"""
    host = ScriptedHost(
        by_label={
            # items 显式为空数组，绕过 harness 对旧桩的自动补齐。
            "审稿": {"verdict": "pass", "summary": "只看了前几项", "must_fix": [], "items": []}
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["review_verdict"] is None
    assert any("未覆盖 1–9 项检查" in item for item in result["incomplete"])
    # 缺项的 pass 不得触发修订，也不得当作通过收尾。
    labels = host.labels()
    assert "修订 1" not in labels


@pytest.mark.asyncio
async def test_pass_with_must_fix_reads_as_revise_and_enters_the_revision_loop() -> None:
    """verdictOf 的 must_fix 规则（提示词第 1 批第 2 轮 #2）：pass 但 must_fix
    非空按 revise 处理——与服务端 publish/review.py 一致，修订必须发生。"""
    host = ScriptedHost(
        by_label={
            "审稿": {
                "verdict": "pass",
                "summary": "总体通过但还有必改项",
                "must_fix": ["补风险章节"],
                "items": [
                    {"check": n, "result": "revise", "reason": "must_fix 项"}
                    for n in range(1, 10)
                ],
            },
            "复审 1": {
                "verdict": "pass",
                "summary": "已解决",
                "must_fix": [],
                "items": [
                    {"check": n, "result": "pass", "reason": "ok"}
                    for n in range(1, 10)
                ],
            },
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    labels = host.labels()
    assert labels.index("修订 1") > labels.index("审稿"), (
        "a pass carrying must_fix must trigger the revision loop"
    )
    assert result["ok"] is True, result["incomplete"]
    assert result["review_verdict"] == "pass"
    assert result["revise_rounds"] == 1


@pytest.mark.asyncio
async def test_missing_branch_blocks_before_the_draft() -> None:
    """B1: a null branch is not success. The run stops before the draft phase
    with a block naming the branch, so the main thread can fill the gap and
    resume (finished dispatches replay from the journal)."""
    host = ScriptedHost(fail_labels={"行业支线"})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["missing"] == ["ind"]
    assert "行业支线" in result["blocked"]
    assert any("关键支线缺失" in item and "resume" in item for item in result["incomplete"])
    assert "resume_hint" in result and "resume=" in result["resume_hint"]
    labels = host.labels()
    assert "bull/bear 快速版" not in labels and "初筛成稿" not in labels
    assert "产物核验" not in labels, "a blocked run must not run the closing checks"


@pytest.mark.asyncio
async def test_all_branches_missing_uses_the_same_block_exit() -> None:
    """The all-missing exit shares the partial-missing block shape: it names
    every branch and carries the resume hint."""
    host = ScriptedHost(
        fail_labels={"公司背景", "行业支线", "财务镜头", "估值取数"}
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["missing"] == ["bg", "ind", "lens", "val"]
    assert result["blocked"].startswith("关键支线缺失：")
    assert "公司背景" in result["blocked"] and "估值历史与可比" in result["blocked"]
    assert "resume_hint" in result
    assert "初筛成稿" not in host.labels()


@pytest.mark.asyncio
async def test_branch_with_empty_files_blocks_before_the_draft() -> None:
    """B1: a child that answers ok with no declared files (the timed-out
    valuation branch's shape) is missing, not success."""
    host = ScriptedHost(by_label={"估值历史与可比": {"files": [], "summary": "未落盘，本轮超时"}})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["missing"] == ["val"]
    assert "估值历史与可比" in result["blocked"]
    assert "初筛成稿" not in host.labels()


@pytest.mark.asyncio
async def test_probe_reported_missing_file_blocks_the_branch() -> None:
    """B1: the branch answered with files, but the on-disk probe says a
    declared artifact is missing/empty — the branch is missing anyway."""
    host = ScriptedHost(
        by_label={
            "支线核验": {
                "files": [
                    "./research/2026-10-23-ic/valuation-data.json|ok",
                    "./drafts/2026-10-23-background.md|ok",
                    "./research/2026-10-23-ic/industry.md|missing",
                    "./research/2026-10-23-ic/lenses.json|ok",
                    "./drafts/2026-10-23-lenses.md|ok",
                    "./research/2026-10-23-ic/valuation.md|ok",
                    "./research/2026-10-23-ic/comps.xlsx|ok",
                ]
            }
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["missing"] == ["ind"]
    assert "初筛成稿" not in host.labels()


@pytest.mark.asyncio
async def test_probe_failure_blocks_fail_closed() -> None:
    """The probe child itself failed: existence cannot be confirmed, so the
    run blocks instead of drafting on unverified evidence."""
    host = ScriptedHost(fail_labels={"支线核验"})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert "支线核验" in result["blocked"]
    assert "初筛成稿" not in host.labels()


@pytest.mark.asyncio
async def test_earnings_deep_block_short_circuits_before_knowledge_draft() -> None:
    params: dict[str, Any] = {
        "ticker": "META",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-10-28",
        "model_path": None,
        "preview_predictions": None,
    }
    host = ScriptedHost(
        by_label={"审稿": {"verdict": "block", "summary": "beat/miss 口径错误", "must_fix": []}}
    )
    result = await _run("earnings-deep-update", params, host)

    assert result["ok"] is False
    assert "beat/miss 口径错误" in result["blocked"]
    assert "知识层修订草案" not in host.labels(), "a block must not run the closing stages"


# ------------------------------------------------- revise-loop failure exits
# The three ways a revise loop can end badly each land in `incomplete`, and
# `ok` stays false without a final `pass`.


@pytest.mark.asyncio
async def test_rereview_block_after_revision_reports_incomplete() -> None:
    host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "P0 未解决", "must_fix": ["补 Q3"]},
            "复审 1": {"verdict": "block", "summary": "修订引入事实错误", "must_fix": []},
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any(item.startswith("复审 block") for item in result["incomplete"])
    assert "修订 1" in host.labels()


@pytest.mark.asyncio
async def test_revision_child_null_reports_incomplete_and_stops() -> None:
    host = ScriptedHost(
        by_label={"审稿": {"verdict": "revise", "summary": "P0", "must_fix": ["x"]}},
        fail_labels={"修订 1"},
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any("修订失败" in item and "未复审" in item for item in result["incomplete"])
    assert "复审 1" not in host.labels(), "a failed revision must not be re-reviewed"


@pytest.mark.asyncio
async def test_rereview_child_null_reports_incomplete() -> None:
    host = ScriptedHost(
        by_label={"审稿": {"verdict": "revise", "summary": "P0", "must_fix": ["x"]}},
        fail_labels={"复审 1"},
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any("复审失败" in item for item in result["incomplete"])


@pytest.mark.asyncio
async def test_empty_evidence_answer_counts_every_deliverable_missing() -> None:
    host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "pass", "summary": "ok", "must_fix": []},
            "产物核验": {"files": []},
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    missing = [item for item in result["incomplete"] if "产物缺失或未报哈希" in item]
    assert missing, "an empty evidence list must not read as all-present"
    assert any("initial-coverage.md" in item for item in missing)


# ------------------------------------------------- dispatch schema admission
# Every schema any shipped workflow hands to agent() must clear the real
# validate_dispatch against the real caps — a HASH nested one level too deep
# is a runtime WorkflowUsageError, not a test-time discovery.


@dataclass
class CapturingHost:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
        self.calls.append((prompt, dict(opts)))
        await asyncio.sleep(0.02)
        label = str(opts.get("label") or "")
        if label == "建模准备":
            m_map = re.search(r"写入 (\S*mapping\.md)", prompt)
            files = ["research/2026-10-23-fc/model_dump/META-fingerprint.json"]
            if m_map:
                files.append(m_map.group(1))
            return {
                "files": files,
                "summary": "prep ok",
                "mode": "update" if "起点路径为" in prompt else "build",
                "baseline_path": "model/META-model-v01.xlsx" if "起点路径为" in prompt else None,
                "baseline_sha256": "1" * 64 if "起点路径为" in prompt else None,
                "fingerprint_path": files[0],
                "consensus_path": "data/consensus/2026-10-23_0900.json",
                "baseline_health_warning": None,
            }
        if label == "读取运行ID":
            return {"run_id": "run-cap-1"}
        if label == "模型构建与落盘":
            return {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "model ok",
                "candidate_path": "model/META-model-v01.xlsx",
                "model_sha256": "a" * 64,
                "recalc_status": "ok",
            }
        if label == "一致预期对照":
            m_cm = re.search(r"生成 (\S*consensus-diff\.md) 与 (\S*consensus-diff\.json)", prompt)
            files = ["model/META-model-v01.xlsx"]
            if m_cm:
                files.extend([m_cm.group(1), m_cm.group(2)])
            return {
                "files": files,
                "summary": "consensus ok",
                "candidate_path": "model/META-model-v01.xlsx",
                "model_sha256": "a" * 64,
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if label in ("模型本轮范围检查", "模型联动修订检查") or label.startswith("模型本轮范围复查 "):
            m_rd = re.search(r'--research-dir "([^"]+)"', prompt)
            rdir = m_rd.group(1) if m_rd else "."
            return {
                "files": [f"{rdir}/model_dump/model-check.md", f"{rdir}/model_dump/model-verification.json"],
                "summary": "check pass",
                "readable": True,
                "final_status": "pass",
                "model_sha256": "a" * 64,
                "actionable_count": 0,
                "actionable_issues": [],
                "baseline_legacy_issues": [],
                "missing_evidence_issues": [],
                "not_applicable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": None,
                "summary_10lines": "summary",
            }
        if label == "模型变动拆解":
            m_delta = re.search(r"至 (\S*model-delta\.md)", prompt)
            return {
                "files": [m_delta.group(1) if m_delta else "research/2026-10-28-deep-FY26Q3/model-delta.md"],
                "summary": "delta ok",
            }
        if label == "模型联动修订":
            return {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "no change",
                "changed": False,
                "model_path": "model/META-model-v01.xlsx",
                "model_sha256": "a" * 64,
                "report_sha256": None,
                "recalc_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "memo 探测":
            m = re.search(r"f='([^']+)'", prompt)
            assert m, "the memo probe prompt must quote the draft path"
            return {"files": [f"{m.group(1)}|absent"]}
        if label == "规划":
            return {
                "file": "research/2026-10-23-topic-ai-capex/plan.md",
                "clusters": [
                    {"id": "a", "name": "簇 A", "queries": ["q1", "q2"]},
                    {"id": "b", "name": "簇 B", "queries": ["q1", "q2"]},
                    {"id": "c", "name": "簇 C", "queries": ["q1", "q2"]},
                ],
                "questions": ["Q1"],
                "summary": "plan",
            }
        if label == "支线核验":
            # Every probed file on disk: one ``path|ok`` per
            # ``- path（至少 N 字节）`` line.
            paths = [
                line[2:].split("（")[0].strip()
                for line in prompt.splitlines()
                if line.startswith("- ") and "|" not in line
            ]
            return {"files": [f"{p}|ok" for p in paths]}
        if label == "草稿探测":
            return {
                "files": ["./drafts/2026-10-23-initial-coverage.md"],
                "draft_content": "absent",
            }
        if label == "赔率摘要核验":
            return {
                "files": ["./research/2026-10-23-ic/underwriting-summary.md"],
                "exists": True,
                "first_line": "已保存 thesis 版本 1",
            }
        if label == "赔率摘要复核":
            return {
                "files": [
                    "./research/2026-10-23-ic/underwriting-summary.md",
                    "./drafts/2026-10-23-initial-coverage.md",
                ],
                "exists": True,
                "first_line": "已保存 thesis 版本 2",
            }
        # A generic non-null answer keeps every script running to its last
        # dispatch, so the whole schema surface is captured.
        return {"files": ["x"], "summary": "s", "sha256": "a" * 64}

    def phase(self, title: str) -> None:
        pass

    def log(self, message: str) -> None:
        pass


_WORKFLOW_PARAMS: dict[str, dict[str, Any]] = {
    "initial-coverage": {
        "ticker": "META", "company": "Meta", "workspace_dir": ".", "as_of": "2026-10-23",
    },
    "full-coverage": {
        "ticker": "META", "company": "Meta", "workspace_dir": ".", "as_of": "2026-10-23",
    },
    "bullbear-doc": {
        "ticker": "META", "company": "Meta", "workspace_dir": ".", "as_of": "2026-10-23",
    },
    "earnings-deep-update": {
        "ticker": "META", "company": "Meta", "quarter": "FY26Q3", "workspace_dir": ".",
        "release_date": "2026-10-28", "model_path": "model/META-model-v01.xlsx",
        "preview_predictions": "research/predictions.json",
    },
    "company-model": {
        "ticker": "META", "company": "Meta", "workspace_dir": ".", "as_of": "2026-10-23",
        "progress_path": "research/2026-10-23-model-META/.progress.json",
    },
    "weekly-coverage-review": {"home_dir": "dalton", "end": "2026-10-23", "coverage_path": "dalton/coverage.yaml"},
    "topic-research": {
        "topic": "AI capex", "slug": "ai-capex", "workspace_dir": ".", "as_of": "2026-10-23",
        "related_tickers": ["META"],
    },
}


class _ScreeningHost(CapturingHost):
    """The weekly screen child answers a real screening payload so the
    per-ticker dispatches (and their schemas) are reached."""

    async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
        if str(opts.get("label") or "") == "筛选":
            self.calls.append((prompt, dict(opts)))
            return {
                "ok": True,
                "base": "2026-10-16",
                "core": [{"ticker": "META", "workspace_dir": ".", "held": True}],
                "flagged": [{"ticker": "NFLX", "event_hint": "异动", "workspace_dir": "."}],
                "warnings": [],
            }
        return await super().agent(prompt, opts)


@pytest.mark.parametrize("name", sorted(_WORKFLOW_PARAMS))
@pytest.mark.asyncio
async def test_every_dispatch_schema_passes_real_validate_dispatch(name: str) -> None:
    from ptc_agent.agent.middleware.background_subagent.workflow.validation import (
        validate_dispatch,
    )
    from src.config.models import WorkflowOrchestrationConfig

    host = _ScreeningHost() if name == "weekly-coverage-review" else CapturingHost()
    outcome = await run_workflow_script(
        _script(name), _WORKFLOW_PARAMS[name], host,
        WorkflowLimits(memory_limit_mb=64, cpu_budget_s=10.0),
    )
    assert outcome.status == "completed", outcome.error
    assert host.calls, f"{name} dispatched nothing"

    caps = WorkflowOrchestrationConfig()
    known = ["general-purpose", "dalton-evidence", "dalton-reviewer"]
    for prompt, opts in host.calls:
        rec = validate_dispatch(
            prompt=prompt,
            opts=opts,
            known_subagent_types=known,
            default_subagent_type="general-purpose",
            caps=caps,
        )
        assert rec["schema"] is not None or "schema" not in opts


# ------------------------------------------------- round-3 invariants


@pytest.mark.asyncio
async def test_evidence_entries_with_empty_hashes_are_all_missing() -> None:
    """The codex reproduction: nine entries whose hashes are empty (`p|`)
    must not read as present — every deliverable is a gap and ok stays
    false."""
    host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": "a" * 64},
            "产物核验": None,  # replaced below
        }
    )
    # Derive the deliverable list from a first pass: run once to capture the
    # evidence prompt, then answer with empty-hash entries.
    probe = ScriptedHost(
        by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": "a" * 64}}
    )
    await _run("initial-coverage", IC_PARAMS, probe)
    evidence_prompt = probe.prompts[-1]
    paths = [
        line[2:].strip()
        for line in evidence_prompt.splitlines()
        if line.startswith("- ") and "|" not in line
    ]
    assert len(paths) >= 9
    host.by_label["产物核验"] = {"files": [f"{p}|" for p in paths]}

    result = await _run("initial-coverage", IC_PARAMS, host)
    assert result["ok"] is False
    gaps = [item for item in result["incomplete"] if "产物缺失或未报哈希" in item]
    assert len(gaps) == len(paths), "every empty-hash entry must be a gap"


@pytest.mark.asyncio
async def test_writer_without_valid_sha256_is_a_failed_draft() -> None:
    """A draft whose writer did not return a 64-hex sha256 is a writing
    failure: no review, ok false, and the gap names the draft."""
    host = ScriptedHost(
        by_label={
            "初筛成稿": {"files": ["drafts/2026-10-23-initial-coverage.md"], "summary": "s", "sha256": "not-hex"},
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any("sha256 缺失/非法" in item for item in result["incomplete"])
    assert "审稿" not in host.labels(), "an unhashable draft must not be reviewed"


# ------------------------------------------------- round-4 invariants


@pytest.mark.asyncio
async def test_topic_knowledge_proposal_dispatches_after_a_revision_pass() -> None:
    """The proposal depends only on the final pass: a revise-then-pass run
    dispatches the knowledge draft even though `fix` already holds a result."""
    host = ScriptedHost(
        by_label={
            "独立审稿": {"verdict": "revise", "summary": "P0", "must_fix": ["x"], "draft_sha256": "a" * 64},
            "复审 1": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": "a" * 64},
        }
    )
    params = {
        "topic": "AI capex",
        "slug": "ai-capex",
        "workspace_dir": ".",
        "as_of": "2026-10-23",
        "related_tickers": ["META"],
    }
    result = await _run("topic-research", params, host)

    labels = host.labels()
    assert "修订 1" in labels and "复审 1" in labels
    assert "知识层草案" in labels, "the proposal must dispatch on pass, revision or not"
    assert result["review_verdict"] == "pass"
    assert result["knowledge_revision"].endswith("knowledge-revision.json")


@pytest.mark.asyncio
async def test_revision_without_valid_hash_stops_without_falling_back() -> None:
    """A revision whose sha256 is missing/invalid ends the loop right there —
    the re-review never sees the pre-revision hash."""
    host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "P0", "must_fix": ["x"], "draft_sha256": "a" * 64},
            # The revision answers without a hash (host bypasses schemas).
            "修订 1": {"files": ["drafts/2026-10-23-initial-coverage.md"], "summary": "fixed"},
        },
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any("sha256 缺失/非法" in item for item in result["incomplete"])
    assert "复审 1" not in host.labels()
    review_prompts = [p for p in host.prompts if "复审" in p]
    assert not review_prompts, "no re-review may run off the stale hash"


# ------------------------------------------------- round-6 invariants


@pytest.mark.asyncio
async def test_second_revision_round_uses_the_first_rounds_hash() -> None:
    """Single revision round + round-2 language review: revision 1 verifies
    lang1's hash, re-review 1 verifies revision 1's hash, and final review
    verifies lang2's hash (with no second revision round)."""
    lang1_hash = "d" * 64
    round1_hash = "b" * 64
    round2_hash = "c" * 64
    revise = {"verdict": "revise", "summary": "P0", "must_fix": ["x"], "draft_sha256": lang1_hash}
    host = ScriptedHost(
        by_label={
            "语言审查 1": {
                "files": ["drafts/2026-10-23-initial-coverage.md"],
                "summary": "l1",
                "sha256": lang1_hash,
            },
            "审稿": revise,
            "修订 1": {
                "files": [
                    "drafts/2026-10-23-initial-coverage.md",
                    "research/2026-10-23-ic/underwriting-summary.md",
                ],
                "summary": "r1",
                "sha256": round1_hash,
            },
            "复审 1": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": round1_hash},
            "语言审查 2": {
                "files": ["drafts/2026-10-23-initial-coverage.md"],
                "summary": "l2",
                "sha256": round2_hash,
            },
            "终稿审稿": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": round2_hash},
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    round1_prompt = host.prompts[host.labels().index("修订 1")]
    assert lang1_hash in round1_prompt, "round 1 must verify lang1's hash"
    assert round1_hash not in round1_prompt
    rereview1_prompt = host.prompts[host.labels().index("复审 1")]
    assert round1_hash in rereview1_prompt, "the re-review judges the current draft"
    final_review_prompt = host.prompts[host.labels().index("终稿审稿")]
    assert round2_hash in final_review_prompt, "final review judges the lang2 draft"
    assert "修订 2" not in host.labels()
    assert result["revise_rounds"] == 1
    draft_prompt = host.prompts[host.labels().index("初筛成稿")]
    notes_prompt = host.prompts[host.labels().index("笔记语言审查")]
    verify_prompt = host.prompts[host.labels().index("产物核验")]
    assert "research/2026-10-23-ic/questions.md" in draft_prompt
    assert "research/2026-10-23-ic/questions.md" in notes_prompt
    assert "research/2026-10-23-ic/questions.md" in verify_prompt


# ------------------------------------------------- round-8: the final model
# A disk-backed host: revisions save the version their prompt names into a
# real temp workspace, and every resolver dispatch runs the prompt's own
# `ls … | sort -V | tail -1` there — so the return value, the evidence list
# and the contract's audit command are compared on one real file listing.

_RESOLVER_CMD = re.compile(r"执行：(ls model/[\w.-]+-model-v\*\.xlsx 2>/dev/null \| sort -V \| tail -1)")
_CONTRACT_PICK = re.compile(r'"\$\((ls model/\{ticker\}-model-v\*\.xlsx \| sort -V \| tail -1)\)"')


def _pick(workspace: Path, command: str) -> str:
    done = subprocess.run(
        ["bash", "-c", command], cwd=workspace, capture_output=True, text=True, check=False
    )
    return done.stdout.strip()


def _contract_verify_done_cmd(name: str) -> str:
    """The workflow contract's modeling closure validator (`model_check.py verify-done`)."""
    text = (REPO_ROOT / "workflows" / name / "contract.json").read_text(encoding="utf-8")
    commands = [
        v["command"]
        for stage in json.loads(text)["stages"]
        for v in stage.get("validators") or []
        if "model_check.py verify-done" in v.get("command", "")
    ]
    assert len(commands) == 1, commands
    assert "model_scan.py" not in commands[0] and "audit.py" not in commands[0], commands[0]
    return commands[0]


# Supplies prompt- and disk-dependent answers; `_run` hands the engine a
# create_autospec(WorkflowHost) whose side_effects call into this.
@dataclass
class DiskHost(ScriptedHost):
    workspace: Path = Path(".")
    # Revision round index (1-based) or label -> "save" (bump version via reviseModelForReport),
    # "keep" (memo-only: unchanged model), or "unusable" (report_revision failed_unusable).
    revise_modes: dict[str, str] = field(default_factory=dict)
    # Label -> workspace files that appear when that label dispatches.
    writes: dict[str, list[str]] = field(default_factory=dict)
    _rev_model_count: int = 0

    def touch(self, rel: str) -> None:
        path = self.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"model {rel}".encode() * 100)

    def _latest_disk_model(self) -> tuple[str, str]:
        candidates = sorted(
            self.workspace.glob("model/*-model-v*.xlsx"),
            key=lambda p: int(re.search(r"-v(\d+)\.xlsx$", p.name).group(1)),
        )
        if not candidates:
            self.touch("model/META-model-v01.xlsx")
            candidates = [self.workspace / "model" / "META-model-v01.xlsx"]
        latest = candidates[-1]
        rel = latest.relative_to(self.workspace).as_posix()
        return rel, hashlib.sha256(latest.read_bytes()).hexdigest()

    def _next_model_version(self, cur: str) -> str:
        return re.sub(
            r"-model-v(\d+)\.xlsx$",
            lambda m: f"-model-v{int(m.group(1)) + 1:0{len(m.group(1))}d}.xlsx",
            cur,
        )

    def outcome_for(self, label: str, prompt: str) -> Any:
        for rel in self.writes.get(label, []):
            self.touch(rel)
        if label in self.fail_labels:
            return None
        if label == "读取运行ID" and label not in self.by_label:
            return {"run_id": "run-disk-1"}
        if label in ("绑定输出路径", "计算联动新路径") and label not in self.by_label:
            m_rec = re.search(r"写入独立记录文件 (\S+\.json)", prompt)
            rec_path = m_rec.group(1) if m_rec else "research/planned-output.json"
            m_base = re.search(r'--baseline "([^"]+)"', prompt)
            if m_base:
                nxt = self._next_model_version(m_base.group(1))
            else:
                nxt, _ = self._latest_disk_model()
            return {"files": [rec_path], "summary": f"bound {nxt}", "next_path": nxt}
        if label in ("核验候选落盘", "核验联动候选落盘") and label not in self.by_label:
            m_cand = re.search(r"核验候选模型 (\S+?\.xlsx)", prompt)
            rel = m_cand.group(1) if m_cand else ""
            p = self.workspace / rel if rel else None
            ex = bool(p and p.is_file())
            sha = hashlib.sha256(p.read_bytes()).hexdigest() if ex and p is not None else None
            return {"path": rel, "exists": ex, "readable": ex, "model_sha256": sha}
        if label == "完整覆盖页签核验" and label not in self.by_label:
            m_cand = re.search(r'--candidate "([^"]+)"', prompt)
            cand = m_cand.group(1) if m_cand else self._latest_disk_model()[0]
            all_sheets = [
                "README & Changelog",
                "Cover",
                "Facts",
                "Revenue Model",
                "Income Statement",
                "Cash Flow Statement",
                "Balance Sheet",
                "Scenarios",
                "Guidance vs Actual",
                "Street vs House",
                "DCF Inputs",
                "DCF",
                "Sensitivity Analysis",
                "Comparable Companies",
                "Valuation Summary",
                "Checks",
            ]
            return {
                "candidate_path": cand,
                "status": "complete",
                "readable": True,
                "workflow": "full-coverage",
                "mode": "build",
                "sheet_names": all_sheets,
                "missing_required_sheets": [],
                "summary": "all required sheets present",
            }
        if label == "模型联动差异刷新" and label not in self.by_label:
            m_cm = re.search(r"刷新一致预期差异表 (\S*consensus-diff\.md) 与 (\S*consensus-diff\.json)", prompt)
            files = [m_cm.group(1), m_cm.group(2)] if m_cm else ["model_dump/consensus-diff.md", "model_dump/consensus-diff.json"]
            return {
                "files": files,
                "summary": "diff refreshed",
                "consensus_diff_generated": True,
            }
        if label == "核验差异表落盘" and label not in self.by_label:
            return {
                "path": "model_dump/consensus-diff.json",
                "json_ok": True,
                "md_ok": True,
                "matched": True,
            }
        if label == "模型联动报告同步" and label not in self.by_label:
            m_rpt = re.search(r"同步更新 (\S+?\.md) 正文", prompt)
            files = [m_rpt.group(1)] if m_rpt else ["drafts/report.md"]
            return {
                "files": files,
                "summary": "report synced",
                "report_sha256": "d" * 64,
            }
        if label == "核验报告同步落盘" and label not in self.by_label:
            m_rpt = re.search(r"核验报告 (\S+?\.md)", prompt)
            rel = m_rpt.group(1) if m_rpt else "drafts/report.md"
            p = self.workspace / rel
            rsha = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "d" * 64
            return {
                "path": rel,
                "exists": True,
                "synced": True,
                "report_sha256": rsha,
            }
        if label == "模型构建与落盘" and label not in self.by_label:
            cand, sha = self._latest_disk_model()
            return {
                "files": [cand],
                "summary": "model ok",
                "candidate_path": cand,
                "model_sha256": sha,
                "recalc_status": "ok",
            }
        if label == "一致预期对照" and label not in self.by_label:
            cand, sha = self._latest_disk_model()
            m_cm = re.search(r"生成 (\S*consensus-diff\.md) 与 (\S*consensus-diff\.json)", prompt)
            files = [cand]
            if m_cm:
                files.extend([m_cm.group(1), m_cm.group(2)])
            return {
                "files": files,
                "summary": "consensus ok",
                "candidate_path": cand,
                "model_sha256": sha,
                "recalc_status": "ok",
                "consensus_status": "ok",
                "consensus_diff_generated": True,
            }
        if (
            label in ("模型本轮范围检查", "模型联动修订检查")
            or label.startswith("模型本轮范围复查 ")
        ) and label not in self.by_label:
            cand, sha = self._latest_disk_model()
            m_rd = re.search(r'--research-dir "([^"]+)"', prompt)
            rdir = m_rd.group(1) if m_rd else "."
            return {
                "files": [f"{rdir}/model_dump/model-check.md", f"{rdir}/model_dump/model-verification.json"],
                "summary": "check pass",
                "readable": True,
                "final_status": "pass",
                "model_sha256": sha,
                "actionable_count": 0,
                "actionable_issues": [],
                "baseline_legacy_issues": [],
                "missing_evidence_issues": [],
                "not_applicable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": None,
                "summary_10lines": f"{cand} pass",
            }
        if label == "模型联动修订" and label not in self.by_label:
            self._rev_model_count += 1
            mode = self.revise_modes.get(f"修订 {self._rev_model_count}") or self.revise_modes.get(label) or "keep"
            m_cur = re.search(r"当前模型 (\S+?\.xlsx)", prompt)
            cur = m_cur.group(1) if m_cur else self._latest_disk_model()[0]
            m_cm = re.search(r"刷新一致预期差异表 (\S*consensus-diff\.md) 与 (\S*consensus-diff\.json)", prompt)
            if mode == "save":
                nxt = self._next_model_version(cur)
                self.touch(nxt)
                sha = hashlib.sha256((self.workspace / nxt).read_bytes()).hexdigest()
                files = [nxt]
                if m_cm:
                    files.extend([m_cm.group(1), m_cm.group(2)])
                return {
                    "files": files,
                    "summary": f"saved {nxt}",
                    "changed": True,
                    "model_path": nxt,
                    "model_sha256": sha,
                    "report_sha256": None,
                    "recalc_status": "ok",
                    "consensus_diff_generated": True,
                }
            if mode == "unusable":
                return {
                    "files": [cur],
                    "summary": "unusable",
                    "changed": True,
                    "model_path": None,
                    "model_sha256": None,
                    "report_sha256": None,
                    "recalc_status": "recalc_failed",
                    "consensus_diff_generated": False,
                }
            sha = hashlib.sha256((self.workspace / cur).read_bytes()).hexdigest() if (self.workspace / cur).exists() else "a" * 64
            return {
                "files": [cur],
                "summary": "unchanged",
                "changed": False,
                "model_path": cur,
                "model_sha256": sha,
                "report_sha256": None,
                "recalc_status": "ok",
                "consensus_diff_generated": True,
            }
        if label == "memo 探测":
            m = re.search(r"f='([^']+)'", prompt)
            assert m, "the memo probe prompt must quote the draft path"
            rel = m.group(1)[2:] if m.group(1).startswith("./") else m.group(1)
            path = self.workspace / rel
            if not path.exists():
                return {"files": [f"{m.group(1)}|absent"]}
            content = path.read_text(encoding="utf-8")
            if len(content.encode("utf-8")) < 500:
                return {"files": [f"{m.group(1)}|incomplete"]}
            from validate_memo import validate_memo_text

            fails, _, _ = validate_memo_text(content)
            if fails:
                return {"files": [f"{m.group(1)}|incomplete"]}
            model_m = re.search(r"m='([^']+)'", prompt)
            msha_m = re.search(r"msha='([0-9a-f]{64})'", prompt)
            meta_m = re.search(r"meta='([^']+)'", prompt)
            if model_m and msha_m:
                expected_model = model_m.group(1).lstrip("./")
                expected_sha = msha_m.group(1)
                cited = [r.lstrip("./") for r in re.findall(r"model:([^\s!]+\.xlsx)!", content)]
                if any(r != expected_model for r in cited):
                    return {"files": [f"{m.group(1)}|stale_model"]}
                if meta_m:
                    meta_rel = meta_m.group(1)[2:] if meta_m.group(1).startswith("./") else meta_m.group(1)
                    meta_path = self.workspace / meta_rel
                    if meta_path.exists():
                        meta_doc = json.loads(meta_path.read_text(encoding="utf-8"))
                        if meta_doc.get("path") != expected_model or meta_doc.get("sha256") != expected_sha:
                            return {"files": [f"{m.group(1)}|stale_model"]}
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return {"files": [f"{m.group(1)}|ok|{digest}"]}
        if label.startswith("修订 ") and label in self.revise_modes:
            return {
                "files": ["drafts/revised.md"],
                "summary": label,
                "sha256": hashlib.sha256(label.encode()).hexdigest(),
            }
        return super().outcome_for(label, prompt)


def test_scripted_hosts_match_the_workflow_host_protocol() -> None:
    from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
        WorkflowHost,
    )

    for name in ("agent", "phase", "log"):
        want = inspect.signature(getattr(WorkflowHost, name))
        for host in (ScriptedHost, DiskHost, CapturingHost):
            got = inspect.signature(getattr(host, name))
            assert list(got.parameters) == list(want.parameters), (host.__name__, name)


FC_PARAMS: dict[str, Any] = {"ticker": "META", "company": "Meta", "workspace_dir": ".", "as_of": "2026-10-23"}
REVISE = {"verdict": "revise", "summary": "P0", "must_fix": ["x"], "draft_sha256": "a" * 64}
PASS = {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": "b" * 64}


def _evidence_models(host: ScriptedHost) -> list[str]:
    prompt = host.prompts[host.labels().index("产物核验")]
    return [line[2:].strip() for line in prompt.splitlines() if line.startswith("- ") and line.endswith(".xlsx")]


def _fc_host(tmp_path: Path, **kw: Any) -> DiskHost:
    host = DiskHost(workspace=tmp_path, **kw)
    host.touch("model/META-model-v01.xlsx")
    return host


@pytest.mark.asyncio
async def test_full_model_revision_saves_v02_and_ends_there(tmp_path: Path) -> None:
    """One revision round only: shared reviseModelForReport saves v02, the re-review
    passes, the final language round edits the draft and the final re-review
    re-binds the hash — the run ends on v02 in the return value and evidence list;
    no v03 round is dispatched."""
    host = _fc_host(
        tmp_path,
        by_label={
            "独立审稿": REVISE,
            "复审 1": PASS,
            "语言审查 2": {**OUT, "sha256": "c" * 64},
            "终稿审稿": PASS,
        },
        revise_modes={"修订 1": "save"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "模型联动修订" in labels
    assert "只读审模型" not in labels
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("修订 1")]
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("复审 1")]
    assert "修订 2" not in labels and "复审 2" not in labels, "one revision round only"
    assert labels.index("终稿审稿") > labels.index("语言审查 2"), "final review follows round 2"
    final = "model/META-model-v02.xlsx"
    assert result["model"] == final
    assert result["model_status"] == "pass"
    assert result["model_verification"].endswith("/model_dump/model-verification.json")
    assert _contract_verify_done_cmd("full-coverage")
    assert _evidence_models(host) == [final]
    assert result["ok"] is True, result["incomplete"]
    assert result["revise_rounds"] == 1
    assert result["language_done"] is True


@pytest.mark.asyncio
async def test_full_rereview_still_revise_has_no_second_round(tmp_path: Path) -> None:
    """The re-review after the single revision round still says revise: the
    run ends incomplete with 审稿未最终通过 — no second revision dispatch."""
    host = _fc_host(tmp_path, by_label={"独立审稿": REVISE, "复审 1": REVISE},
                    revise_modes={"修订 1": "keep"})
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is False
    assert result["revise_rounds"] == 1
    assert "修订 2" not in host.labels()
    assert result["tracking"]["reason"] == "审稿未最终通过"


@pytest.mark.asyncio
async def test_full_reported_v02_but_disk_v03_stops_before_rereview(tmp_path: Path) -> None:
    """If shared report-revision model edit fails to maintain a readable candidate,
    the revision loop stops before re-review and records incomplete."""
    host = _fc_host(
        tmp_path,
        by_label={"独立审稿": REVISE, "复审 1": PASS},
        revise_modes={"修订 1": "unusable"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "模型联动修订" in labels
    assert "复审 1" not in labels, "an unusable model revision must not be re-reviewed"
    assert "知识层修订草案" not in labels and "产物核验" not in labels
    assert result["ok"] is False
    assert any("报告联动改模未能维持可读候选模型" in i for i in result["incomplete"])


@pytest.mark.asyncio
async def test_full_memo_only_revision_keeps_the_existing_model(tmp_path: Path) -> None:
    """A model exists and the revision only touched the memo: reviseModelForReport
    returns changed=false keeping v01, carried to the end. Round 2 of the
    language review finds nothing to change (same hash), so no final re-review runs."""
    round1_sha = hashlib.sha256("修订 1".encode()).hexdigest()
    host = _fc_host(
        tmp_path,
        by_label={"独立审稿": REVISE, "复审 1": PASS, "语言审查 2": {**OUT, "sha256": round1_sha}},
        revise_modes={"修订 1": "keep"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "语言审查 2" in labels, "the memo keeps its two language rounds"
    assert "终稿审稿" not in labels, "an unchanged draft needs no final re-review"
    final = "model/META-model-v01.xlsx"
    assert result["ok"] is True, result["incomplete"]
    assert result["model"] == final
    assert _evidence_models(host) == [final]
    assert final in host.prompts[labels.index("复审 1")]
    assert result["language_done"] is True


@pytest.mark.asyncio
async def test_full_revision_without_model_path_fails(tmp_path: Path) -> None:
    """When report revision fails to return a valid revised draft sha256, the loop stops before re-review."""
    host = _fc_host(
        tmp_path,
        by_label={
            "独立审稿": REVISE,
            "复审 1": PASS,
            "修订 1": {"files": ["drafts/2026-10-23-memo.md"], "summary": "missing sha"},
        },
        revise_modes={},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is False
    assert any("sha256" in i for i in result["incomplete"])
    assert "复审 1" not in host.labels()
    assert result["model"] == "model/META-model-v01.xlsx"


DEEP_PARAMS: dict[str, Any] = {
    "ticker": "META",
    "quarter": "FY26Q3",
    "workspace_dir": ".",
    "release_date": "2026-10-28",
    "model_path": "model/META-model-v01.xlsx",
    "preview_predictions": None,
}


def _deep_host(tmp_path: Path, by_label: dict[str, Any], **kw: Any) -> DiskHost:
    host = DiskHost(
        workspace=tmp_path,
        by_label=by_label,
        writes={"模型构建与落盘": ["model/META-model-v02.xlsx"], **kw.pop("writes", {})},
        **kw,
    )
    host.touch("model/META-model-v01.xlsx")
    return host


@pytest.mark.asyncio
async def test_deep_model_update_mismatch_stops_before_calibration(tmp_path: Path) -> None:
    """When modeling produces no readable candidate (failed_unusable), deep-update
    records incomplete without crashing the report path."""
    host = _deep_host(
        tmp_path,
        {},
        writes={"模型构建与落盘": []},
        fail_labels={"模型构建与落盘"},
    )
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    assert result["ok"] is False
    assert result["model_status"] == "failed_unusable"
    assert any("无可读候选模型" in i for i in result["incomplete"])


@pytest.mark.asyncio
async def test_deep_two_model_revisions_and_prompts_name_the_model(tmp_path: Path) -> None:
    """Update saves v02; two report revisions via shared reviseModelForReport save
    v03 then v04. Calibration and the review name v02, each re-review names its
    round's model, and the return value and evidence list pick v04."""
    host = _deep_host(
        tmp_path,
        {"审稿": REVISE, "复审 1": REVISE, "复审 2": PASS, "终稿审稿": PASS},
        revise_modes={"修订 1": "save", "修订 2": "save"},
    )
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    labels = host.labels()
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("校准与对账")]
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("审稿")]
    assert labels.count("模型联动修订") == 2
    assert "model/META-model-v03.xlsx" in host.prompts[labels.index("复审 1")]
    assert "model/META-model-v04.xlsx" in host.prompts[labels.index("复审 2")]
    final = "model/META-model-v04.xlsx"
    assert result["model_file"] == final
    assert _contract_verify_done_cmd("earnings-deep-update")
    assert _evidence_models(host) == [final]
    assert result["ok"] is True, result["incomplete"]


@pytest.mark.asyncio
async def test_deep_reported_v03_but_disk_v04_stops_before_rereview(tmp_path: Path) -> None:
    """If shared reviseModelForReport fails to maintain a readable candidate, the
    deep-update revision loop stops before re-review."""
    host = _deep_host(
        tmp_path,
        {"审稿": REVISE, "复审 1": PASS},
        revise_modes={"修订 1": "unusable"},
    )
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    labels = host.labels()
    assert "模型联动修订" in labels
    assert "复审 1" not in labels
    assert "知识层修订草案" not in labels and "产物核验" not in labels
    assert result["ok"] is False
    assert any("报告联动改模未能维持可读候选模型" in i for i in result["incomplete"])


@pytest.mark.asyncio
async def test_deep_report_only_revision_keeps_the_updated_model(tmp_path: Path) -> None:
    host = _deep_host(tmp_path, {"审稿": REVISE, "复审 1": PASS, "终稿审稿": PASS}, revise_modes={"修订 1": "keep"})
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    final = "model/META-model-v02.xlsx"
    assert result["ok"] is True, result["incomplete"]
    assert result["model_file"] == final
    assert _evidence_models(host) == [final]


@pytest.mark.asyncio
async def test_deep_revision_without_model_path_fails(tmp_path: Path) -> None:
    """A revision returning no valid draft sha256 stops before re-review."""
    host = _deep_host(
        tmp_path,
        {
            "审稿": REVISE,
            "复审 1": PASS,
            "修订 1": {"files": ["drafts/x.md"], "summary": "missing sha"},
        },
        revise_modes={},
    )
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    assert result["ok"] is False
    assert any("sha256" in i for i in result["incomplete"])
    assert "复审 1" not in host.labels()


@pytest.mark.asyncio
async def test_deep_without_model_path_never_resolves() -> None:
    """No model_path: runModelingPipeline returns skipped_no_model with zero modeling
    dispatches and no model in the result."""
    params = {**DEEP_PARAMS, "model_path": None}
    host = ScriptedHost(by_label={"审稿": REVISE, "复审 1": PASS})
    result = await _run("earnings-deep-update", params, host)

    assert "建模准备" not in host.labels()
    assert "模型联动修订" not in host.labels()
    assert result["model_file"] is None
    assert result["model_status"] == "skipped_no_model"
    assert result["ok"] is True, result["incomplete"]


@pytest.mark.asyncio
async def test_deep_first_review_pass_writes_calibration_into_draft_and_binds_its_sha() -> None:
    """When the first review passes directly (no revision loop), calibration
    writes the reconcile/thesis-calibration/management-delta conclusions into
    the draft's 投资逻辑的验证 section, returns the updated draft sha256, and
    the subsequent review binds that sha256."""
    draft_sha = "a" * 64
    lang1_sha = "b" * 64
    calib_sha = "d" * 64
    params = {**DEEP_PARAMS, "model_path": None}
    host = ScriptedHost(
        by_label={
            "八节点评": {**OUT, "sha256": draft_sha},
            "语言审查 1": {**OUT, "sha256": lang1_sha},
            "校准与对账": {**OUT, "sha256": calib_sha},
            "审稿": {"verdict": "pass", "summary": "ok", "must_fix": []},
            "语言审查 2": {**OUT, "sha256": calib_sha},
        }
    )
    result = await _run("earnings-deep-update", params, host)

    labels = host.labels()
    assert "修订 1" not in labels
    calib_prompt = host.prompts[labels.index("校准与对账")]
    assert "reconcile.md" in calib_prompt
    assert "thesis-calibration.md" in calib_prompt
    assert "management-delta.md" in calib_prompt
    assert "投资逻辑的验证" in calib_prompt
    assert "./drafts/2026-10-28-deep-FY26Q3.md" in calib_prompt
    review_prompt = host.prompts[labels.index("审稿")]
    assert calib_sha in review_prompt, "review must bind the post-calibration draft sha"
    assert lang1_sha not in review_prompt
    kp_prompt = host.prompts[labels.index("知识层修订草案")]
    assert calib_sha in kp_prompt
    assert result["ok"] is True, result["incomplete"]
    assert result["revise_rounds"] == 0


@pytest.mark.asyncio
async def test_full_absolute_workspace_dir_zero_revisions_is_not_a_mismatch(tmp_path: Path) -> None:
    """workspace_dir may be an absolute folder: the initial model path carries
    that prefix while the resolver reports model/…; with no revision at all the
    two still name the same file, so the run must not stop on a "mismatch"."""
    host = _fc_host(tmp_path, by_label={"独立审稿": PASS})
    params = {**FC_PARAMS, "workspace_dir": str(tmp_path)}

    result = await _run("full-coverage", params, host)

    assert not any("不一致" in item for item in result["incomplete"]), result["incomplete"]
    assert result["model"] == "model/META-model-v01.xlsx"


# ------------------------------------------- tracking hint follows `ok`
# The "完成 → enable tracking" hint is only produced when the run's own `ok`
# is true; a run whose review never passes skips tracking and never logs 完成.

_ENABLE = {
    "action": "enable",
    "source": "coverage_finish",
    "tool": "dalton_coverage_tracking",
    "call": 'dalton_coverage_tracking(action="enable", source="coverage_finish")',
    "respect_user_pause": True,
}


@pytest.mark.asyncio
async def test_initial_review_never_passes_skips_tracking() -> None:
    host = ScriptedHost(by_label={"审稿": REVISE, "复审 1": REVISE, "复审 2": REVISE})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["tracking"]["ok"] is False
    assert result["tracking"]["skipped"] is True
    assert result["tracking"]["reason"] == "审稿未最终通过"
    assert "action" not in result["tracking"]
    assert not any("初筛完成" in m for m in host.logs), host.logs


@pytest.mark.asyncio
async def test_initial_clean_run_enables_tracking() -> None:
    host = ScriptedHost(by_label={"审稿": PASS})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is True, result["incomplete"]
    assert result["tracking"] == _ENABLE
    assert any("初筛完成" in m for m in host.logs), host.logs


@pytest.mark.asyncio
async def test_initial_missing_deliverable_skips_tracking_even_when_review_passes() -> None:
    host = ScriptedHost(by_label={"审稿": PASS, "产物核验": {"files": []}})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["tracking"]["skipped"] is True
    assert result["tracking"]["reason"].startswith("流程未完成：")
    assert not any("初筛完成" in m for m in host.logs), host.logs


@pytest.mark.asyncio
async def test_full_review_never_passes_skips_tracking(tmp_path: Path) -> None:
    host = _fc_host(
        tmp_path,
        by_label={"独立审稿": REVISE, "复审 1": REVISE},
        revise_modes={"修订 1": "keep"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is False
    assert result["tracking"]["ok"] is False
    assert result["tracking"]["skipped"] is True
    assert result["tracking"]["reason"] == "审稿未最终通过"
    assert "action" not in result["tracking"]
    assert not any("完整覆盖完成" in m for m in host.logs), host.logs


@pytest.mark.asyncio
async def test_full_clean_run_enables_tracking(tmp_path: Path) -> None:
    host = _fc_host(tmp_path, by_label={"独立审稿": PASS})
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is True, result["incomplete"]
    assert result["tracking"] == _ENABLE
    assert any("完整覆盖完成" in m for m in host.logs), host.logs


# ------------------------------------------------- language review round order


@pytest.mark.asyncio
async def test_language_rounds_wrap_the_reviews_in_order() -> None:
    """Round 1 lands after the draft and before the FIRST review; round 2
    after the revise loop and before the FINAL review, and the final review
    binds the post-language-edit hash (语言 → 修改 → 审稿)."""
    host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "pass", "summary": "ok", "must_fix": []},
            "语言审查 1": {**OUT, "sha256": "b" * 64},
            "语言审查 2": {**OUT, "sha256": "c" * 64},
            "终稿审稿": {
                "verdict": "pass",
                "summary": "语言修改未动事实",
                "must_fix": [],
                "draft_sha256": "c" * 64,
            },
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    labels = host.labels()
    assert labels.index("语言审查 1") > labels.index("初筛成稿")
    assert labels.index("审稿") > labels.index("语言审查 1"), "review must follow round 1"
    assert labels.index("语言审查 2") > labels.index("审稿")
    assert labels.index("终稿审稿") > labels.index("语言审查 2"), "final review must follow round 2"
    # Each dispatch drives the round through the server-side tool.
    round1_prompt = host.prompts[labels.index("语言审查 1")]
    round2_prompt = host.prompts[labels.index("语言审查 2")]
    assert "check_language" in round1_prompt and "round=1" in round1_prompt
    assert "check_language" in round2_prompt and "round=2" in round2_prompt
    # The first review binds the round-1-edited hash; the final one the final hash.
    assert "b" * 64 in host.prompts[labels.index("审稿")]
    assert "c" * 64 in host.prompts[labels.index("终稿审稿")]
    assert result["ok"] is True
    assert result["review_verdict"] == "pass"
    assert result["language_done"] is True


@pytest.mark.asyncio
async def test_language_round2_without_edits_skips_the_final_review() -> None:
    """Round 2 finds nothing to change: the draft hash is unchanged, the
    loop's pass record still binds the current draft, so no re-review runs."""
    host = ScriptedHost(by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}})
    result = await _run("initial-coverage", IC_PARAMS, host)

    labels = host.labels()
    assert "语言审查 1" in labels and "语言审查 2" in labels
    assert "终稿审稿" not in labels
    assert result["ok"] is True
    assert result["language_done"] is True


@pytest.mark.asyncio
async def test_language_round2_failure_reports_incomplete() -> None:
    host = ScriptedHost(
        by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}},
        fail_labels={"语言审查 2"},
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any("语言审查第 2 轮失败" in item for item in result["incomplete"])
    assert "终稿审稿" not in host.labels()


@pytest.mark.asyncio
async def test_first_review_pass_still_runs_round2_and_final_review() -> None:
    """The "review passed on the first try" case: round 2 and the final
    review still run, so the last review record binds the final hash."""
    host = ScriptedHost(
        by_label={
            "独立审稿": {"verdict": "pass", "summary": "ok", "must_fix": []},
            "终稿审稿": {
                "verdict": "pass",
                "summary": "ok",
                "must_fix": [],
                "draft_sha256": "c" * 64,
            },
            "语言审查 2": {**OUT, "sha256": "c" * 64},
        }
    )
    result = await _run("topic-research", {"topic": "AI capex", "slug": "ai-capex", "workspace_dir": ".", "as_of": "2026-10-23"}, host)

    labels = host.labels()
    assert labels.index("语言审查 2") > labels.index("独立审稿")
    assert labels.index("终稿审稿") > labels.index("语言审查 2")
    assert result["ok"] is True, result["incomplete"]
    assert result["language_done"] is True


# ------------------------------------------------- notes two-round batch


@pytest.mark.asyncio
async def test_notes_get_two_rounds_after_the_loop_before_the_draft_round2() -> None:
    """The write-once notes (background/lenses/branches/bullbear-quick) get
    their own two-round batch after the revise loop and before the draft's
    round 2; the batch names each note and excludes machine outputs."""
    host = ScriptedHost(by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}})
    result = await _run("initial-coverage", IC_PARAMS, host)

    labels = host.labels()
    idx = labels.index("笔记语言审查")
    assert idx > labels.index("审稿")
    assert idx < labels.index("语言审查 2"), "notes batch precedes the draft's round 2"
    prompt = host.prompts[idx]
    assert "check_language" in prompt and "round=1" in prompt and "round=2" in prompt
    for note in (
        "background.md",
        "lenses.md",
        "industry.md",
        "valuation.md",
        "bullbear-quick.md",
    ):
        assert note in prompt, note
    # Machine outputs are not listed for review; the exclusion sentence stands.
    assert "comps.xlsx" not in prompt and "knowledge-proposal.json" not in prompt
    assert "不审" in prompt
    assert result["ok"] is True
    assert result["language_done"] is True


@pytest.mark.asyncio
async def test_notes_language_review_failure_reports_incomplete() -> None:
    host = ScriptedHost(
        by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}},
        fail_labels={"笔记语言审查"},
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert any("笔记语言审查失败" in item for item in result["incomplete"])


# ------------------------------------------------- A1: SKILL params reach the children


@pytest.mark.asyncio
async def test_pm_question_sector_and_revision_brief_reach_every_child() -> None:
    """A1: the SKILL declares pm_question/sector and an optional revision_brief
    path; every subtask prompt (evidence branches included) must carry them —
    round 2's must_fix list rides pm_question, and losing it made the agent
    re-derive requirements from a chance Glob hit."""
    params = {
        **IC_PARAMS,
        "pm_question": "云资本开支的回报周期怎么量化",
        "sector": "互联网服务",
        "revision_brief": "research/review-must-fix.json",
    }
    host = ScriptedHost(by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}})
    result = await _run("initial-coverage", params, host)

    assert result["ok"] is True, result["incomplete"]
    # base-carrying children are identifiable by the conventions prefix; the
    # mechanical verifiers (probe, evidence, language, review) never took base.
    directed = [
        (label, prompt)
        for label, prompt in zip(host.labels(), host.prompts)
        if "先读 .agents/skills/dalton-conventions" in prompt
    ]
    assert directed
    for label, prompt in directed:
        assert "云资本开支的回报周期怎么量化" in prompt, label
        assert "互联网服务" in prompt, label
        assert "research/review-must-fix.json" in prompt, label


@pytest.mark.asyncio
async def test_pm_sections_absent_when_params_missing() -> None:
    host = ScriptedHost(by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}})
    await _run("initial-coverage", IC_PARAMS, host)

    assert all("PM 的问题" not in p for p in host.prompts)


# ------------------------------------------------- B1 supplement: valuation split


@pytest.mark.asyncio
async def test_valuation_branch_dispatches_fetch_then_write() -> None:
    """child_timeout cannot be overridden per dispatch, so the valuation
    branch splits into two children — each with its own full timeout window —
    and a failed fetch stage never dispatches the write stage."""
    labels_expected = ["估值取数", "估值历史与可比"]
    host = ScriptedHost()
    await _run("initial-coverage", IC_PARAMS, host)
    labels = host.labels()
    assert labels_expected[0] in labels and labels_expected[1] in labels
    assert labels.index(labels_expected[1]) > labels.index(labels_expected[0])

    failed = ScriptedHost(fail_labels={"估值取数"})
    result = await _run("initial-coverage", IC_PARAMS, failed)
    assert result["missing"] == ["val"]
    assert "估值历史与可比" not in failed.labels(), "a failed fetch must not dispatch the write"

    # The B1 shared judgment: a fetch that answers ok with NO declared
    # artifacts (the timeout shape) is just as missing.
    empty = ScriptedHost(by_label={"估值取数": {"files": [], "summary": "未落盘"}})
    result = await _run("initial-coverage", IC_PARAMS, empty)
    assert result["missing"] == ["val"]
    assert "估值历史与可比" not in empty.labels(), "an empty-artifact fetch must not dispatch the write"
    assert "估值历史与可比" in result["blocked"]


@pytest.mark.asyncio
async def test_valuation_fetch_data_file_is_an_intermediate_not_a_deliverable() -> None:
    """valuation-data.json feeds the write stage; the evidence check and the
    deliverables list stay keyed on the contract's artifacts."""
    host = ScriptedHost(by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}})
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is True, result["incomplete"]
    evidence_prompt = host.prompts[host.labels().index("产物核验")]
    assert "valuation-data.json" not in evidence_prompt
    assert "./research/2026-10-23-ic/valuation.md" in evidence_prompt


# ------------------------------------------------- audit #12: weekly call children
# The call children are general-purpose subagents with no Dalton direct tools;
# the prompt must scope them to writing call-<T>.md only.


@pytest.mark.asyncio
async def test_weekly_call_children_are_scoped_off_dalton_tools() -> None:
    params = {
        "home_dir": "dalton",
        "end": "2026-10-23",
        "coverage_path": "dalton/coverage.yaml",
    }

    class _CallHost(_ScreeningHost):
        async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
            if str(opts.get("label") or "") == "META":
                return {
                    "ticker": "META",
                    "file": "research/weekly-2026-10-23/core-META.md",
                    "call_candidate": True,
                }
            return await super().agent(prompt, opts)

    host = _CallHost()
    await _run("weekly-coverage-review", params, host)

    call_prompts = [
        prompt
        for prompt, opts in host.calls
        if str(opts.get("label") or "").startswith("call ")
    ]
    assert call_prompts, "the call child must have dispatched"
    prompt = call_prompts[0]
    assert "不调用 knowledge_append_verdict" in prompt
    assert "check_draft" in prompt and "publish_report" in prompt
    assert "Step 2.2" in prompt and "Step 9" in prompt
    assert "call-META.md" in prompt


# ------------------------------------------------- round 2: unified branch gates


@pytest.mark.asyncio
async def test_full_facts_failure_blocks_before_frame() -> None:
    """The facts ledger is contract-mandatory (≥500 bytes): null answers, an
    on-disk miss, and a failed probe each stop the run before the frame
    phase, with the three causes reported separately."""
    params = _WORKFLOW_PARAMS["full-coverage"]

    host = ScriptedHost(fail_labels={"数字底表"})
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "数字底表" in result["blocked"] and "未产出" in result["blocked"]
    assert "resume_hint" in result
    assert "frame" not in host.labels()

    # ok answer whose declared file is not on disk: the probe catches it
    # (fixture path spelled exactly as the script probes it, './'-prefixed).
    host = ScriptedHost(
        by_label={
            "数字底表": {"files": ["./research/2026-10-23-fc/facts.md"], "summary": "written"},
            "支线核验": {"files": ["./research/2026-10-23-fc/facts.md|missing"]},
        }
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "数字底表" in result["blocked"] and "500" in result["blocked"]
    assert "核验子任务失败" not in result["blocked"]
    assert "frame" not in host.labels()

    # the probe child itself failing is its own message, not "file missing".
    host = ScriptedHost(
        by_label={"数字底表": {"files": ["./research/2026-10-23-fc/facts.md"], "summary": "written"}},
        fail_labels={"支线核验"},
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "支线核验子任务失败" in result["blocked"] and "数字底表" in result["blocked"]
    assert "不存在" not in result["blocked"]
    assert "frame" not in host.labels()


@pytest.mark.asyncio
async def test_full_frame_failure_blocks_before_evidence() -> None:
    """questions.md is contract-mandatory (≥200 bytes): a null frame stops
    the run before the evidence branches."""
    params = _WORKFLOW_PARAMS["full-coverage"]
    host = ScriptedHost(
        fail_labels={"frame"},
        by_label={
            "支线核验": {"files": ["./research/2026-10-23-fc/facts.md|ok"]},
        },
    )
    result = await _run("full-coverage", params, host)

    assert result["ok"] is False
    assert "问题清单 frame" in result["blocked"] and "未产出" in result["blocked"]
    assert "resume_hint" in result
    labels = host.labels()
    assert not any(label.startswith("支线 ") for label in labels), "no evidence branch may run"


class _ModelProbeHost(ScriptedHost):
    """Answers every branch probe ok except a model/… path, which is missing."""

    def outcome_for(self, label: str, prompt: str) -> Any:
        if label == "支线核验":
            paths = [
                line[2:].split("（")[0].strip()
                for line in prompt.splitlines()
                if line.startswith("- ") and "|" not in line
            ]
            return {
                "files": [
                    f"{p}|{'missing' if p.startswith('model/') else 'ok'}" for p in paths
                ]
            }
        return super().outcome_for(label, prompt)


@pytest.mark.asyncio
async def test_full_model_failure_blocks_before_the_draft() -> None:
    """建模流水线仅在无可读候选（failed_unusable）时 gateBlock 停在成稿之前：
    准备阶段失败、建模与一致预期对照返回空、或检查返回 readable=false，均停在成稿之前。"""
    params = _WORKFLOW_PARAMS["full-coverage"]

    # 1. Stage 1 prepare fails
    host = ScriptedHost(fail_labels={"建模准备"})
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模失败" in result["blocked"]
    assert "resume_hint" in result and "resume=" in result["resume_hint"]
    assert "成稿" not in host.labels()
    assert "独立审稿" not in host.labels() and "产物核验" not in host.labels()

    # 2. Stage 2a modeling build returns null
    host = ScriptedHost(fail_labels={"模型构建与落盘"})
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模失败" in result["blocked"]
    assert "成稿" not in host.labels()

    # 3. Stage 3 check reports candidate unreadable
    host = ScriptedHost(
        by_label={
            "模型本轮范围检查": {
                "files": ["model_dump/model-check.md"],
                "summary": "workbook unreadable",
                "readable": False,
                "final_status": "failed_unusable",
                "model_sha256": None,
                "actionable_count": 1,
            },
        },
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模失败" in result["blocked"]
    assert "成稿" not in host.labels()


@pytest.mark.asyncio
async def test_full_resume_with_existing_draft_never_rewrites_it(tmp_path: Path) -> None:
    """Resume guard (round 2 #4): with a structure-complete drafts/<D>-memo.md
    already on disk — edited by the language round, the revision or a human,
    so the journal's draft hash no longer replays — the writer is never
    re-dispatched; the run carries the on-disk draft into review."""
    host = _fc_host(tmp_path, by_label={"独立审稿": PASS})
    draft = tmp_path / "drafts" / "2026-10-23-memo.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(FC_COMPLETE_MEMO, encoding="utf-8")
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "memo 探测" in labels
    assert "成稿" not in labels, "an existing complete draft must never be rewritten"
    assert "语言审查 1" in labels and "独立审稿" in labels
    assert result["ok"] is True, result["incomplete"]


@pytest.mark.asyncio
async def test_full_partial_draft_is_completed_not_rewritten(tmp_path: Path) -> None:
    """Round 3 #2: an empty or half-written memo on disk (a crashed writer's
    leftover) is NOT treated as the finished draft — the writer is dispatched
    to complete it on top of the existing content, never from scratch."""
    for name, content in (
        ("空文件", ""),
        ("半成品", "# 虚拟公司（TEST）投资备忘录\n\n## 摘要\n\n写了一半。\n"),
    ):
        host = _fc_host(tmp_path, by_label={"独立审稿": PASS})
        draft = tmp_path / "drafts" / "2026-10-23-memo.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(content, encoding="utf-8")
        result = await _run("full-coverage", FC_PARAMS, host)

        labels = host.labels()
        assert "成稿" in labels, f"{name}: the writer must be dispatched for a partial draft"
        writer_prompt = host.prompts[labels.index("成稿")]
        assert "保留其中可用的部分" in writer_prompt and "不要推倒重写" in writer_prompt, name
        assert result["ok"] is True, (name, result["incomplete"])


@pytest.mark.asyncio
async def test_full_resume_reviews_and_revises_the_latest_model_version(tmp_path: Path) -> None:
    """With v02 as the pipeline candidate, the first review sees v02 and shared
    reviseModelForReport operates on v02 (never v01), with no legacy 只读审模型."""
    round1_sha = hashlib.sha256("修订 1".encode()).hexdigest()
    host = _fc_host(
        tmp_path,
        by_label={
            "独立审稿": REVISE,
            "复审 1": PASS,
            "语言审查 2": {**OUT, "sha256": round1_sha},
        },
        revise_modes={"修订 1": "keep"},
    )
    host.touch("model/META-model-v02.xlsx")
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "只读审模型" not in labels
    review_prompt = host.prompts[labels.index("独立审稿")]
    assert "model/META-model-v02.xlsx" in review_prompt, "the first review must see the pipeline candidate"
    assert "model/META-model-v01.xlsx" not in review_prompt
    rev_model_prompt = host.prompts[labels.index("模型联动修订")]
    assert "当前模型 model/META-model-v02.xlsx" in rev_model_prompt
    assert result["ok"] is True, result["incomplete"]
    assert result["model"] == "model/META-model-v02.xlsx"


@pytest.mark.asyncio
async def test_full_latest_model_failing_audit_blocks_before_the_draft(tmp_path: Path) -> None:
    """When the pipeline candidate is unreadable (readable=false / failed_unusable),
    the run stops before drafting."""
    host = _fc_host(
        tmp_path,
        by_label={
            "模型本轮范围检查": {
                "files": ["model_dump/model-check.md"],
                "summary": "v02 unreadable",
                "readable": False,
                "final_status": "failed_unusable",
                "model_sha256": None,
                "actionable_count": 1,
            },
        },
    )
    host.touch("model/META-model-v02.xlsx")
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is False
    assert "成稿" not in host.labels()


@pytest.mark.asyncio
async def test_full_resume_with_saved_v02_accepts_latest_model(tmp_path: Path) -> None:
    """When the pipeline returns v02 and the first review passes, full-coverage
    returns v02 cleanly without a legacy resolver mismatch."""
    host = _fc_host(tmp_path, by_label={"独立审稿": PASS})
    host.touch("model/META-model-v02.xlsx")
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is True, result["incomplete"]
    assert result["model"] == "model/META-model-v02.xlsx"
    assert not any("不一致" in item for item in result["incomplete"]), result["incomplete"]


TOPIC_PARAMS: dict[str, Any] = {
    "topic": "AI capex",
    "slug": "ai-capex",
    "workspace_dir": ".",
    "as_of": "2026-10-23",
}

_PLAN_4 = {
    "file": "research/2026-10-23-topic-ai-capex/plan.md",
    "clusters": [
        {"id": "a", "name": "簇 A", "queries": ["q"]},
        {"id": "b", "name": "簇 B", "queries": ["q"]},
        {"id": "c", "name": "簇 C", "queries": ["q"]},
        {"id": "d", "name": "簇 D", "queries": ["q"]},
    ],
    "questions": ["Q1"],
    "summary": "plan",
}


@pytest.mark.asyncio
async def test_topic_primary_failure_blocks_before_cross_check() -> None:
    """primary.md is contract-mandatory: missing, the run stops before the
    cross-check phase."""
    host = ScriptedHost(fail_labels={"一手材料"})
    result = await _run("topic-research", TOPIC_PARAMS, host)

    assert result["ok"] is False
    assert "一手材料" in result["blocked"]
    assert "resume_hint" in result
    assert "交叉验证与盲区" not in host.labels()


@pytest.mark.asyncio
async def test_topic_cluster_gap_above_floor_surfaces_but_does_not_block() -> None:
    """Four clusters planned, one fails: the ≥3 floor still holds, the run
    proceeds, and the gap rides the writing prompt, incomplete and the return
    value."""
    host = ScriptedHost(
        by_label={
            "规划": _PLAN_4,
            "独立审稿": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": "b" * 64},
            "复审 1": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": "b" * 64},
        },
        fail_labels={"取证 簇 B"},
    )
    result = await _run("topic-research", TOPIC_PARAMS, host)

    labels = host.labels()
    assert "交叉验证与盲区" in labels and "专题写作" in labels, "the floor held; the run proceeded"
    write_prompt = host.prompts[labels.index("专题写作")]
    assert "缺失簇：簇 B" in write_prompt and "信息缺口与局限" in write_prompt
    assert result["missing_clusters"] == ["b"]
    assert any("分簇取证失败（簇 B）" in item for item in result["incomplete"])
    assert result["ok"] is False, "the named gap keeps the run incomplete"


WEEKLY_PARAMS: dict[str, Any] = {
    "home_dir": "dalton",
    "end": "2026-10-23",
    "coverage_path": "dalton/coverage.yaml",
}


class _WeeklyLabels:
    """Label view over a CapturingHost's (prompt, opts) call log."""

    def __init__(self, host: Any) -> None:
        self._host = host

    def labels(self) -> list[str]:
        return [str(opts.get("label") or "") for _p, opts in self._host.calls]

    def prompt_of(self, label: str) -> str:
        for prompt, opts in self._host.calls:
            if str(opts.get("label") or "") == label:
                return prompt
        raise AssertionError(f"no dispatch labeled {label!r}")


@pytest.mark.asyncio
async def test_weekly_off_disk_core_counts_failed_and_gets_no_call() -> None:
    """A core whose file the on-disk probe reports missing (or undersized)
    counts as failed: no conviction call for it, and the weekly doc names it."""

    class _ProbeHost(_ScreeningHost):
        async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
            if str(opts.get("label") or "") == "支线核验":
                return {
                    "files": [
                        "dalton/research/weekly-2026-10-23/core-META.md|missing",
                        "dalton/research/weekly-2026-10-23/watch-NFLX.md|ok",
                    ]
                }
            return await super().agent(prompt, opts)

    host = _ProbeHost()
    result = await _run("weekly-coverage-review", WEEKLY_PARAMS, host)

    view = _WeeklyLabels(host)
    labels = view.labels()
    assert not any(label.startswith("call ") for label in labels), "a failed core gets no call"
    assert "周会稿" in labels, "a single failed company does not block the whole sweep"
    assert "Core 失败的公司：META" in view.prompt_of("周会稿")
    assert any("Core 逐家失败：META" in item for item in result["incomplete"])
    assert result["core_done"] == 0 and result["watch_done"] == 1


# ------------------------------------------------- round 3: fetch-data gate, budget


@pytest.mark.asyncio
async def test_valuation_fetch_data_must_be_on_disk_before_write() -> None:
    """The fetch stage answered ok with files, but valuation-data.json is
    not on disk (or the probe child failed): the write stage never
    dispatches and the run stops through the unified branch block."""
    host = ScriptedHost(
        by_label={
            "估值取数": {
                "files": ["./research/2026-10-23-ic/valuation-data.json"],
                "summary": "fetched",
            },
            "支线核验": {
                "files": ["./research/2026-10-23-ic/valuation-data.json|missing"]
            },
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["missing"] == ["val"]
    assert "估值历史与可比" not in host.labels(), "no write without on-disk data"
    assert "初筛成稿" not in host.labels()

    # The probe child failing takes the same unified exit.
    dead = ScriptedHost(
        by_label={
            "估值取数": {
                "files": ["./research/2026-10-23-ic/valuation-data.json"],
                "summary": "fetched",
            }
        },
        fail_labels={"支线核验"},
    )
    result = await _run("initial-coverage", IC_PARAMS, dead)
    assert result["missing"] == ["val"]
    assert "估值历史与可比" not in dead.labels()

    # The fetch prompt carries the same resume hint as every branch.
    healthy = ScriptedHost()
    await _run("initial-coverage", IC_PARAMS, healthy)
    fetch_prompt = healthy.prompts[healthy.labels().index("估值取数")]
    assert "先读并核对" in fetch_prompt, "a resumed fetch must not clobber the filled file"


@pytest.mark.asyncio
async def test_weekly_full_budget_run_completes_with_three_calls() -> None:
    """27 core companies, 37 watch hits sliced to the 30-slot room, and 3
    conviction calls: exactly the 64-dispatch platform cap, and the run
    still completes normally (no script error from the last dispatches)."""

    class _FullHouse(_ScreeningHost):
        async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
            label = str(opts.get("label") or "")
            if label == "筛选":
                self.calls.append((prompt, dict(opts)))
                return {
                    "ok": True,
                    "base": "2026-10-16",
                    "core": [
                        {"ticker": f"C{i:02d}", "workspace_dir": ".", "held": False}
                        for i in range(27)
                    ],
                    "flagged": [
                        {"ticker": f"W{i:02d}", "event_hint": "异动", "workspace_dir": "."}
                        for i in range(37)
                    ],
                    "warnings": [],
                }
            if label in ("C00", "C01", "C02"):
                self.calls.append((prompt, dict(opts)))
                return {
                    "ticker": label,
                    "file": f"research/weekly-2026-10-23/core-{label}.md",
                    "call_candidate": True,
                }
            if label.startswith("call "):
                self.calls.append((prompt, dict(opts)))
                return {
                    "ticker": label.removeprefix("call "),
                    "file": f"research/weekly-2026-10-23/call-{label.removeprefix('call ')}.md",
                }
            return await super().agent(prompt, opts)

    host = _FullHouse()
    result = await _run("weekly-coverage-review", WEEKLY_PARAMS, host)

    # 1 screening + 27 core + 30 watch (sliced) + 1 probe + 3 calls + 1 doc
    # + 1 evidence = exactly the cap; the room math is what keeps it there.
    assert len(host.calls) == 64, len(host.calls)
    assert result["watch_done"] == 30
    assert result["calls"] == ["C00", "C01", "C02"]
    assert result["core_total"] == 27 and result["core_done"] == 27


@pytest.mark.asyncio
async def test_weekly_probe_failure_blocks_calls_and_marks_unverified() -> None:
    """With the probe child failed, the companies still count into the sweep
    by their reported artifacts, but none is verified on disk: no calls go
    out and the return names the unverified companies. META IS a call
    candidate here, so deleting the probeFailed short-circuit would dispatch
    its call and fail this test."""

    class _DeadProbeHost(_ScreeningHost):
        async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
            label = str(opts.get("label") or "")
            if label == "支线核验":
                await asyncio.sleep(0.02)
                return None
            if label == "META":
                self.calls.append((prompt, dict(opts)))
                return {
                    "ticker": "META",
                    "file": "research/weekly-2026-10-23/core-META.md",
                    "call_candidate": True,
                }
            return await super().agent(prompt, opts)

    host = _DeadProbeHost()
    result = await _run("weekly-coverage-review", WEEKLY_PARAMS, host)

    labels = _WeeklyLabels(host).labels()
    assert "周会稿" in labels, "the sweep continues without the probe"
    assert not any(label.startswith("call ") for label in labels), "unverified companies get no call"
    assert any("支线核验子任务失败" in item and "不派 call" in item for item in result["incomplete"])
    assert result["unverified_by_probe"] == ["META", "NFLX"]
    assert result["core_done"] == 1 and result["watch_done"] == 1


@pytest.mark.asyncio
async def test_topic_probe_failure_exit_carries_missing_clusters() -> None:
    """The probe-failure exit runs after the clusters: it carries the
    reported-missing clusters, and its copy does not claim the gap was
    passed to the writing stage (we never got there)."""
    host = ScriptedHost(
        by_label={"规划": _PLAN_4},
        fail_labels={"取证 簇 B", "支线核验"},
    )
    result = await _run("topic-research", TOPIC_PARAMS, host)

    assert result["ok"] is False
    assert "支线核验子任务失败" in result["blocked"]
    assert result["missing_clusters"] == ["b"]
    assert any(
        "支线核验子任务失败" in item and "簇 B" in item for item in result["incomplete"]
    )
    assert not any("已传给写作" in item for item in result["incomplete"])
    assert "交叉验证与盲区" not in host.labels()


@pytest.mark.asyncio
async def test_initial_coverage_pauses_for_underwriting_summary_before_draft_and_resumes() -> None:
    """Round 5 #2, #5: before drafting, the workflow verifies underwriting-summary.md
    starts with the fixed header '已保存 thesis 版本 N' and declares its file path in
    `files`; missing file, uncommitted dry_run ('尚未保存' / '没有保存成功'), or missing
    file declaration all pause before 初筛成稿 and instruct the main agent to formally
    write agenda/debate and then thesis (dry_run=True then dry_run=False). New-draft
    prompts never mention '一、12 问初筛'."""
    uw_path = "./research/2026-10-23-ic/underwriting-summary.md"
    # 1) Missing underwriting-summary.md -> pauses before 初筛成稿
    missing_host = ScriptedHost(
        by_label={
            "赔率摘要核验": {
                "files": [uw_path],
                "exists": False,
                "first_line": "none",
            }
        }
    )
    paused = await _run("initial-coverage", IC_PARAMS, missing_host)
    assert paused["ok"] is False
    assert paused["knowledge_proposal"] == "./research/2026-10-23-ic/knowledge-proposal.json"
    assert paused["underwriting_summary"] == uw_path
    assert "agenda" in paused["blocked"] and "debate" in paused["blocked"]
    assert "dry_run=False" in paused["blocked"] and "已保存 thesis 版本 N" in paused["blocked"]
    assert "resume=" in paused["resume_hint"]
    assert "知识层草案" in missing_host.labels()
    assert "初筛成稿" not in missing_host.labels()

    # 2) Uncommitted dry_run ("尚未保存" / "没有保存成功") -> refused, pauses before 初筛成稿
    uncommitted_host = ScriptedHost(
        by_label={
            "赔率摘要核验": {
                "files": [uw_path],
                "exists": True,
                "first_line": "赔率计算摘要（试写通过，尚未保存｜立场：中性）",
            }
        }
    )
    refused = await _run("initial-coverage", IC_PARAMS, uncommitted_host)
    assert refused["ok"] is False
    assert "尚未正式保存" in refused["blocked"]
    assert "初筛成稿" not in uncommitted_host.labels()

    # 3) Missing file declaration in 赔率摘要核验 -> refused, pauses before 初筛成稿
    undeclared_host = ScriptedHost(
        by_label={
            "赔率摘要核验": {
                "files": [],
                "exists": True,
                "first_line": "已保存 thesis 版本 1",
            }
        }
    )
    undeclared = await _run("initial-coverage", IC_PARAMS, undeclared_host)
    assert undeclared["ok"] is False
    assert "赔率摘要核验子任务失败" in undeclared["blocked"]
    assert "初筛成稿" not in undeclared_host.labels()

    # 4) Formal save ("已保存 thesis 版本 1；暂不表态，不计算赔率") -> proceeds to 初筛成稿
    saved_host = ScriptedHost(
        by_label={
            "赔率摘要核验": {
                "files": [uw_path],
                "exists": True,
                "first_line": "已保存 thesis 版本 1；暂不表态，不计算赔率",
            },
            "审稿": {"verdict": "pass", "summary": "ok", "must_fix": []},
        }
    )
    resumed = await _run("initial-coverage", IC_PARAMS, saved_host)
    assert resumed["ok"] is True, resumed["incomplete"]
    labels = saved_host.labels()
    assert "初筛成稿" in labels
    draft_prompt = saved_host.prompts[labels.index("初筛成稿")]
    review_prompt = saved_host.prompts[labels.index("审稿")]
    assert "一、12 问初筛" not in draft_prompt
    assert "一、12 问初筛" not in review_prompt
    evidence_prompt = saved_host.prompts[labels.index("产物核验")]
    assert uw_path in evidence_prompt


@pytest.mark.asyncio
async def test_initial_coverage_pauses_before_final_review_when_revision_requests_payoff_rewrite() -> None:
    """Round 6 #2, #3, #4:
    - 修订 1 must declare `underwriting-summary.md` in `files` and return required
      `underwriting_version` and `needs_reunderwrite`.
    - When `needs_reunderwrite: True`, workflow pauses immediately after 修订 1 and
      before 复审 1.
    - Before 终稿审稿, 赔率摘要复核 verifies `uwFinal.version === finalDraftVersion`
      (and declares `draftPath` in `files`); mismatch pauses before 终稿审稿, while
      matching versions proceed to 终稿审稿 and complete."""
    uw_path = "./research/2026-10-23-ic/underwriting-summary.md"
    draft_path = "./drafts/2026-10-23-initial-coverage.md"

    # 1) 修订 1 omitting underwriting-summary.md from `files` fails revision (Item 4)
    missing_summary_decl_host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "下调倍数假设", "must_fix": ["下调 Bull 倍数"]},
            "修订 1": {
                "files": [draft_path],
                "summary": "未声明 underwriting-summary.md",
                "sha256": "a" * 64,
                "needs_reunderwrite": False,
                "underwriting_version": 1,
            },
        }
    )
    missing_decl_res = await _run("initial-coverage", IC_PARAMS, missing_summary_decl_host)
    assert missing_decl_res["ok"] is False
    assert any("underwriting-summary.md 文件声明" in item for item in missing_decl_res["incomplete"])
    assert "复审 1" not in missing_summary_decl_host.labels()

    # 2) 修订 1 setting needs_reunderwrite: True pauses immediately before 复审 1 (Item 2 & #3)
    needs_rewrite_host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "下调倍数假设", "must_fix": ["下调 Bull 倍数"]},
            "修订 1": {
                "files": [draft_path, uw_path],
                "summary": "已按审稿意见更新草案，等待重写赔率",
                "sha256": "a" * 64,
                "needs_reunderwrite": True,
                "underwriting_version": 1,
            },
        }
    )
    paused = await _run("initial-coverage", IC_PARAMS, needs_rewrite_host)
    assert paused["ok"] is False
    assert "needs_reunderwrite=true" in paused["blocked"]
    assert "不要贴试写的返回" in paused["blocked"]
    assert "已保存 thesis 版本 N" in paused["blocked"]
    assert "复审 1" not in needs_rewrite_host.labels(), "must pause immediately after 修订 1 and before 复审 1"
    assert "终稿审稿" not in needs_rewrite_host.labels()

    # 3) Before 终稿审稿, 赔率摘要复核 omitting draftPath from `files` is rejected
    incomplete_files_host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "下调倍数假设", "must_fix": ["下调 Bull 倍数"]},
            "修订 1": {
                "files": [draft_path, uw_path],
                "summary": "已按新摘要改好数字",
                "sha256": "b" * 64,
                "needs_reunderwrite": False,
                "underwriting_version": 2,
            },
            "复审 1": {"verdict": "pass", "summary": "已修改", "must_fix": []},
            "赔率摘要复核": {
                "files": [uw_path],
                "exists": True,
                "first_line": "已保存 thesis 版本 2",
            },
        }
    )
    probe_failed = await _run("initial-coverage", IC_PARAMS, incomplete_files_host)
    assert probe_failed["ok"] is False
    assert any("赔率摘要复核子任务失败" in item for item in probe_failed["incomplete"])
    assert "终稿审稿" not in incomplete_files_host.labels()

    # 4) Before 终稿审稿, if finalDraftVersion (1) != summary first-line version (2), pause before 终稿审稿
    version_mismatch_host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "下调倍数假设", "must_fix": ["下调 Bull 倍数"]},
            "修订 1": {
                "files": [draft_path, uw_path],
                "summary": "仍基于版本 1",
                "sha256": "b" * 64,
                "needs_reunderwrite": False,
                "underwriting_version": 1,
            },
            "复审 1": {"verdict": "pass", "summary": "已修改", "must_fix": []},
            "赔率摘要复核": {
                "files": [uw_path, draft_path],
                "exists": True,
                "first_line": "已保存 thesis 版本 2",
            },
        }
    )
    mismatch_paused = await _run("initial-coverage", IC_PARAMS, version_mismatch_host)
    assert mismatch_paused["ok"] is False
    assert "版本 1" in mismatch_paused["blocked"] and "版本 2" in mismatch_paused["blocked"]
    assert "终稿审稿" not in version_mismatch_host.labels()

    # 5) When finalDraftVersion (2) == summary first-line version (2), runs 终稿审稿 and completes
    rewritten_host = ScriptedHost(
        by_label={
            "审稿": {"verdict": "revise", "summary": "下调倍数假设", "must_fix": ["下调 Bull 倍数"]},
            "修订 1": {
                "files": [draft_path, uw_path],
                "summary": "已按版本 2 赔率摘要更新正文与三情景表",
                "sha256": "b" * 64,
                "needs_reunderwrite": False,
                "underwriting_version": 2,
            },
            "复审 1": {"verdict": "pass", "summary": "已修改", "must_fix": []},
            "赔率摘要复核": {
                "files": [uw_path, draft_path],
                "exists": True,
                "first_line": "已保存 thesis 版本 2",
            },
            "终稿审稿": {"verdict": "pass", "summary": "数字一致", "must_fix": []},
        }
    )
    completed = await _run("initial-coverage", IC_PARAMS, rewritten_host)
    assert completed["ok"] is True, completed["incomplete"]
    labels = rewritten_host.labels()
    assert labels.index("修订 1") < labels.index("复审 1") < labels.index("赔率摘要复核") < labels.index("终稿审稿")


@pytest.mark.asyncio
async def test_initial_coverage_legacy_in_flight_run_skips_pre_draft_pause_and_rejects_self_reported_legacy_tag() -> None:
    """Round 4 #3: legacy draft detection is done by workflow.js matching
    LEGACY_HEADING_RE against the raw draft_content returned by 草稿探测 (which
    declares `files: [draftPath]`). In-flight legacy runs skip the pre-draft
    知识层草案 and pause point, and omit questions.md & underwriting-summary.md
    from deliverables."""
    legacy_host = ScriptedHost(
        by_label={
            "草稿探测": {
                "files": ["./drafts/2026-10-23-initial-coverage.md"],
                "draft_content": "# 初筛\n\n## 核心判断\n判断。\n\n## 一、12 问初筛\n### Q1. 业务",
            },
            "审稿": {"verdict": "pass", "summary": "ok", "must_fix": []},
        }
    )
    legacy_res = await _run("initial-coverage", IC_PARAMS, legacy_host)
    assert legacy_res["ok"] is True, legacy_res["incomplete"]
    labels = legacy_host.labels()
    assert "赔率摘要核验" not in labels
    assert "赔率摘要复核" not in labels
    assert labels.index("初筛成稿") < labels.index("知识层草案")
    evidence_prompt = legacy_host.prompts[labels.index("产物核验")]
    assert "questions.md" not in evidence_prompt
    assert "underwriting-summary.md" not in evidence_prompt

    # On a non-legacy run, a child self-reporting `<path>|legacy` is rejected as invalid
    class _BogusLegacyChildHost(ScriptedHost):
        def evidence_default(self, prompt: str) -> Any:
            paths = [
                line[2:].strip()
                for line in prompt.splitlines()
                if line.startswith("- ") and "|" not in line
            ]
            return {
                "files": [
                    f"{p}|legacy" if "questions.md" in p else f"{p}|{'e' * 64}"
                    for p in paths
                ]
            }

    bogus_host = _BogusLegacyChildHost(
        by_label={"审稿": {"verdict": "pass", "summary": "ok", "must_fix": []}}
    )
    bogus_res = await _run("initial-coverage", IC_PARAMS, bogus_host)
    assert bogus_res["ok"] is False
    assert any("产物核验返回无效条目" in item for item in bogus_res["incomplete"])

@pytest.mark.asyncio
async def test_initial_coverage_writer_must_declare_the_payoff_summary() -> None:
    """The writer reads underwriting-summary.md, so its `files` must name it: the
    journal only re-runs a dispatch when a declared file changes. A writer that
    leaves it out is not accepted, and nothing after drafting runs."""
    uw_path = "./research/2026-10-23-ic/underwriting-summary.md"
    host = ScriptedHost(
        by_label={
            "赔率摘要核验": {
                "files": [uw_path],
                "exists": True,
                "first_line": "已保存 thesis 版本 1",
            },
            "初筛成稿": {
                "_strict_files": True,
                "files": ["./drafts/2026-10-23-initial-coverage.md"],
                "summary": "成稿",
                "sha256": "a" * 64,
                "underwriting_version": 1,
            },
        }
    )
    out = await _run("initial-coverage", IC_PARAMS, host)
    assert out["ok"] is False
    assert "初筛成稿" in host.labels()
    assert "审稿" not in host.labels()


@pytest.mark.asyncio
async def test_full_model_scan_failure_or_non_ok_checks_continues_to_draft(
    tmp_path: Path,
) -> None:
    """Modeling v2 (§3.2 / §3.3): when the modeling pipeline finishes with
    delivered_with_open_issues (e.g. check timeout fallback or remaining open issues
    on a readable candidate), full-coverage logs the notice and proceeds to drafting."""
    # Case 1: check child times out -> writeFallbackLedger -> delivered_with_open_issues -> proceeds to draft
    ws1 = tmp_path / "case_timeout"
    ws1.mkdir()
    host1 = _fc_host(ws1, by_label={"独立审稿": PASS}, fail_labels={"模型本轮范围检查"})
    res1 = await _run("full-coverage", FC_PARAMS, host1)
    assert res1["ok"] is True, res1["incomplete"]
    assert res1["model_status"] == "delivered_with_open_issues"
    assert res1["model_check"] is None
    assert res1["remaining_issues"] is not None
    assert "写入降级账本" in host1.labels()
    assert "成稿" in host1.labels()
    assert any("带问题交付" in line for line in host1.logs)

    # Case 2: recalc_failed on readable candidate -> delivered_with_open_issues -> proceeds to draft
    ws2 = tmp_path / "case_recalc_failed"
    ws2.mkdir()
    host2 = _fc_host(
        ws2,
        by_label={
            "模型构建与落盘": {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "recalc failed",
                "candidate_path": "model/META-model-v01.xlsx",
                "model_sha256": "a" * 64,
                "recalc_status": "recalc_failed",
            },
            "模型本轮范围检查": {
                "files": [
                    "research/2026-10-23-fc/model_dump/model-check.md",
                    "research/2026-10-23-fc/model_dump/model-verification.json",
                    "research/2026-10-23-fc/model_dump/remaining-issues.md",
                ],
                "summary": "delivered with recalc_failed",
                "readable": True,
                "final_status": "delivered_with_open_issues",
                "model_sha256": "a" * 64,
                "actionable_count": 0,
                "actionable_issues": [],
                "missing_required_sheets": [],
                "remaining_issues_path": "research/2026-10-23-fc/model_dump/remaining-issues.md",
            },
            "独立审稿": PASS,
        },
    )
    res2 = await _run("full-coverage", FC_PARAMS, host2)
    assert res2["ok"] is True, res2["incomplete"]
    assert res2["model_status"] == "delivered_with_open_issues"
    assert "成稿" in host2.labels()


@pytest.mark.asyncio
async def test_modeling_batch2_workflows_and_contracts_route_to_dalton_model(
    tmp_path: Path,
) -> None:
    """Modeling v2 P0-b (§3.3): earnings-deep-update, full-coverage, and company-model
    share runModelingPipeline & reviseModelForReport (dalton-model scripts, model_dump/
    artifacts, terminal model-verification.json ledger, and model_check.py verify-done)."""
    # 1. earnings-deep-update: shared pipeline + shared report revision entry
    deep_host = _deep_host(
        tmp_path,
        {"审稿": REVISE, "复审 1": PASS, "终稿审稿": PASS},
        revise_modes={"修订 1": "save"},
    )
    deep_res = await _run("earnings-deep-update", DEEP_PARAMS, deep_host)
    assert deep_res["ok"] is True, deep_res["incomplete"]
    deep_labels = deep_host.labels()

    prep_prompt = deep_host.prompts[deep_labels.index("建模准备")]
    assert ".agents/skills/dalton-model/SKILL.md" in prep_prompt
    assert "model_fingerprint.py" in prep_prompt
    assert "mapping.md" in prep_prompt
    rid_prompt = deep_host.prompts[deep_labels.index("读取运行ID")]
    assert ".progress.json" in rid_prompt
    s2a_prompt = deep_host.prompts[deep_labels.index("模型构建与落盘")]
    assert "model_edit.py apply" in s2a_prompt
    s2b_prompt = deep_host.prompts[deep_labels.index("一致预期对照")]
    assert "model_consensus.py" in s2b_prompt
    chk_prompt = deep_host.prompts[deep_labels.index("模型本轮范围检查")]
    assert "model_check.py check" in chk_prompt
    rev_model_prompt = deep_host.prompts[deep_labels.index("模型联动修订")]
    assert "model_fingerprint.py" in rev_model_prompt
    assert "model_edit.py apply" in rev_model_prompt
    assert "model-update/SKILL.md" not in rev_model_prompt
    rev_chk_prompt = deep_host.prompts[deep_labels.index("模型联动修订检查")]
    assert "--append-revision" in rev_chk_prompt

    # 2. full-coverage: shared pipeline + no legacy 只读审模型 + shared report revision entry
    fc_dir = tmp_path / "fc"
    fc_dir.mkdir()
    fc_host = _fc_host(
        fc_dir,
        by_label={"独立审稿": REVISE, "复审 1": PASS, "终稿审稿": PASS},
        revise_modes={"修订 1": "save"},
    )
    fc_res = await _run("full-coverage", FC_PARAMS, fc_host)
    assert fc_res["ok"] is True, fc_res["incomplete"]
    fc_labels = fc_host.labels()

    assert "只读审模型" not in fc_labels
    assert (
        "建模准备" in fc_labels
        and "读取运行ID" in fc_labels
        and "模型构建与落盘" in fc_labels
        and "一致预期对照" in fc_labels
        and "模型本轮范围检查" in fc_labels
    )
    assert "模型联动修订" in fc_labels and "模型联动修订检查" in fc_labels
    draft_prompt = fc_host.prompts[fc_labels.index("成稿")]
    assert "原模型未设情景页" in draft_prompt and "模型里没有对应计算" in draft_prompt
    fc_notes_prompt = fc_host.prompts[fc_labels.index("笔记语言审查")]
    assert "model-check.md" not in fc_notes_prompt
    assert "model_dump/" in fc_notes_prompt

    # 3. All three contracts use model-verification.json + model_check.py verify-done
    for wf in ("company-model", "earnings-deep-update", "full-coverage"):
        contract_text = (REPO_ROOT / "workflows" / wf / "contract.json").read_text(encoding="utf-8")
        assert "model_dump/model-verification.json" in contract_text, wf
        assert "model_check.py verify-done" in contract_text, wf
        assert "model_scan.py" not in contract_text, wf
        assert "audit.py" not in contract_text, wf


@pytest.mark.asyncio
async def test_full_existing_draft_with_stale_model_updates_affected_sections(
    tmp_path: Path,
) -> None:
    """Modeling Batch 2 r3 (#4): reusing a same-as_of memo verifies that its
    cited model path and recorded model sha256 match the current final model;
    either mismatch dispatches 成稿 to sync predictions/valuation/citations
    before review."""
    # Case A: memo cites v01 while current final model is v02
    ws_path_drift = tmp_path / "path_drift"
    ws_path_drift.mkdir()
    host_a = _fc_host(ws_path_drift, by_label={"独立审稿": PASS})
    host_a.touch("model/META-model-v02.xlsx")
    draft_a = ws_path_drift / "drafts" / "2026-10-23-memo.md"
    draft_a.parent.mkdir(parents=True, exist_ok=True)
    draft_a.write_text(FC_COMPLETE_MEMO, encoding="utf-8")

    res_a = await _run("full-coverage", FC_PARAMS, host_a)
    assert res_a["ok"] is True, res_a["incomplete"]
    labels_a = host_a.labels()
    assert "成稿" in labels_a, "stale model path in existing memo must trigger update"
    prompt_a = host_a.prompts[labels_a.index("成稿")]
    assert "model/META-model-v02.xlsx" in prompt_a
    assert "更新受影响的预测、估值、表格与全部 model: 引用" in prompt_a

    # Case B: memo cites v01 and .memo-model.json records an older sha256 for v01
    ws_sha_drift = tmp_path / "sha_drift"
    ws_sha_drift.mkdir()
    host_b = _fc_host(ws_sha_drift, by_label={"独立审稿": PASS})
    draft_b = ws_sha_drift / "drafts" / "2026-10-23-memo.md"
    draft_b.parent.mkdir(parents=True, exist_ok=True)
    draft_b.write_text(FC_COMPLETE_MEMO, encoding="utf-8")
    meta_b = ws_sha_drift / "research" / "2026-10-23-fc" / ".memo-model.json"
    meta_b.parent.mkdir(parents=True, exist_ok=True)
    meta_b.write_text(
        json.dumps({"path": "model/META-model-v01.xlsx", "sha256": "0" * 64}),
        encoding="utf-8",
    )

    res_b = await _run("full-coverage", FC_PARAMS, host_b)
    assert res_b["ok"] is True, res_b["incomplete"]
    labels_b = host_b.labels()
    assert "成稿" in labels_b, "stale model sha256 in existing memo must trigger update"
    assert "更新受影响的预测、估值、表格与全部 model: 引用" in host_b.prompts[labels_b.index("成稿")]

