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
        if label == "建模":
            # The full-coverage model child must attest its own checks
            # (recalc / audit --strict / Checks) and declare the xlsx.
            return {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "built, checks all green",
                "sha256": "a" * 64,
                "checks_passed": True,
            }
        if label.startswith("模型审计"):
            m = re.search(r"f='([^']+)'", prompt)
            assert m, "the model-audit probe prompt must quote the model path"
            return {"files": [f"{m.group(1)}|audit_ok"]}
        if label == "memo 探测":
            m = re.search(r"f='([^']+)'", prompt)
            assert m, "the memo probe prompt must quote the draft path"
            return {"files": [f"{m.group(1)}|absent"]}
        if label == "产物核验":
            return self.evidence_default(prompt)
        if label == "支线核验":
            return self.branch_probe_default(prompt)
        return dict(OUT)

    async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
        label = str(opts.get("label") or "")
        self.calls.append((label, str(opts.get("agentType"))))
        self.prompts.append(prompt)
        # A real child takes time; an instantly-resolved host future can land
        # the engine's pending-promise check before the JS microtasks drain.
        await asyncio.sleep(0.02)
        result = self.outcome_for(label, prompt)
        # 审稿契约（提示词第 1 批 #2）：dalton-reviewer 的回复必须逐项覆盖
        # 1–9 项检查，workflows 据此判结果有效。旧桩只写 verdict；按全部
        # 通过补齐 items，使这些用例仍考察 verdict 分支本身。
        if (
            opts.get("agentType") == "dalton-reviewer"
            and isinstance(result, dict)
            and "items" not in result
        ):
            result = {
                **result,
                "items": [
                    {"check": n, "result": "pass", "reason": "ok"}
                    for n in range(1, 10)
                ],
            }
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
    return (REPO_ROOT / "workflows" / name / "workflow.js").read_text(encoding="utf-8")


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
        by_label={"审稿": revise, "复审 1": revise, "复审 2": revise}
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert result["revise_rounds"] == 2
    assert any("审稿未最终通过" in item for item in result["incomplete"])
    assert host.labels().count("修订 1") == 1
    assert host.labels().count("修订 2") == 1


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
        if label.startswith("解析"):
            return {"path": "model/META-model-v01.xlsx", "sha256": "a" * 64}
        if label == "建模":
            return {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "built, checks all green",
                "sha256": "a" * 64,
                "checks_passed": True,
            }
        if label.startswith("模型审计"):
            m = re.search(r"f='([^']+)'", prompt)
            assert m, "the model-audit probe prompt must quote the model path"
            return {"files": [f"{m.group(1)}|audit_ok"]}
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
    """Two revise rounds: round 2's revision prompt and re-review carry the
    hash round 1 produced — never the pre-review draft's."""
    round1_hash = "b" * 64
    round2_hash = "c" * 64
    revise = {"verdict": "revise", "summary": "P0", "must_fix": ["x"], "draft_sha256": "a" * 64}
    host = ScriptedHost(
        by_label={
            "审稿": revise,
            "复审 1": revise,
            "复审 2": {"verdict": "pass", "summary": "ok", "must_fix": [], "draft_sha256": round2_hash},
            # Each revision returns its own new hash.
            "修订 1": {
                "files": ["drafts/2026-10-23-initial-coverage.md"],
                "summary": "r1",
                "sha256": round1_hash,
            },
            "修订 2": {
                "files": ["drafts/2026-10-23-initial-coverage.md"],
                "summary": "r2",
                "sha256": round2_hash,
            },
        }
    )
    result = await _run("initial-coverage", IC_PARAMS, host)

    round2_prompt = host.prompts[host.labels().index("修订 2")]
    assert round1_hash in round2_prompt, "round 2 must verify round 1's hash"
    assert round2_hash not in round2_prompt
    rereview2_prompt = host.prompts[host.labels().index("复审 2")]
    assert round2_hash in rereview2_prompt, "the re-review judges the current draft"
    assert result["revise_rounds"] == 2


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


def _contract_audit_pick(name: str, workspace: Path, ticker: str = "META") -> str:
    """The file the workflow contract's `audit.py "$(ls … | sort -V | tail -1)"`
    validator would audit, evaluated in ``workspace``."""
    text = (REPO_ROOT / "workflows" / name / "contract.json").read_text(encoding="utf-8")
    commands = [
        v["command"]
        for stage in json.loads(text)["stages"]
        for v in stage.get("validators") or []
        if "audit.py" in v.get("command", "")
    ]
    assert len(commands) == 1, commands
    match = _CONTRACT_PICK.search(commands[0])
    assert match, commands[0]
    return _pick(workspace, match.group(1).replace("{ticker}", ticker))


# Supplies prompt- and disk-dependent answers; `_run` hands the engine a
# create_autospec(WorkflowHost) whose side_effects call into this.
@dataclass
class DiskHost(ScriptedHost):
    workspace: Path = Path(".")
    # Revision label -> "save" (save as the version the prompt names),
    # "keep" (memo-only: report the prompt's current model), "omit" (no model).
    revise_modes: dict[str, str] = field(default_factory=dict)
    # Label -> workspace files that appear when that label dispatches.
    writes: dict[str, list[str]] = field(default_factory=dict)

    def touch(self, rel: str) -> None:
        path = self.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"model {rel}".encode() * 100)

    def resolve(self, prompt: str) -> Any:
        match = _RESOLVER_CMD.search(prompt)
        assert match, "the resolver prompt must carry the sort -V command"
        out = _pick(self.workspace, match.group(1))
        if not out:
            return None
        return {"path": out, "sha256": hashlib.sha256((self.workspace / out).read_bytes()).hexdigest()}

    def outcome_for(self, label: str, prompt: str) -> Any:
        for rel in self.writes.get(label, []):
            self.touch(rel)
        if label in self.fail_labels:
            return None
        if label.startswith("解析"):
            return self.resolve(prompt)
        if label == "memo 探测":
            # Disk-backed, with the real probe's semantics (round 3 #2): a draft
            # counts as complete only when it clears validate_memo's structure
            # check; anything else on disk is a partial handed to the writer.
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
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return {"files": [f"{m.group(1)}|ok|{digest}"]}
        mode = self.revise_modes.get(label)
        if mode is None:
            return super().outcome_for(label, prompt)
        files = ["drafts/revised.md"]
        if mode == "save":
            target = re.search(r"另存为 (\S+?\.xlsx)", prompt)
            assert target, "the revision prompt must name the next version"
            self.touch(target.group(1))
            files.append(target.group(1))
        elif mode == "keep":
            current = re.search(r"原样沿用 (\S+?\.xlsx)", prompt)
            assert current, "the revision prompt must name the current model"
            files.append(current.group(1))
        return {"files": files, "summary": label, "sha256": hashlib.sha256(label.encode()).hexdigest()}


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
    """One revision round only (batch 2): round 1 saves v02, the re-review
    passes, the final language round edits the draft and the final re-review
    re-binds the hash — the run ends on v02 in the return value, the evidence
    list and the contract audit; no v03 round is dispatched."""
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
    assert "另存为 model/META-model-v02.xlsx" in host.prompts[labels.index("修订 1")]
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("复审 1")]
    assert "修订 2" not in labels and "复审 2" not in labels, "one revision round only"
    assert labels.index("终稿审稿") > labels.index("语言审查 2"), "final review follows round 2"
    final = "model/META-model-v02.xlsx"
    assert result["model"] == final
    assert _contract_audit_pick("full-coverage", tmp_path) == final
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
    """The revision reports v02 while the disk maximum is v03: the run stops
    at that round's resolver — the re-review is never dispatched."""
    host = _fc_host(
        tmp_path,
        by_label={
            "独立审稿": REVISE,
            "复审 1": PASS,
            "修订 1": {
                "files": ["drafts/2026-10-23-memo.md", "model/META-model-v02.xlsx"],
                "summary": "r1",
                "sha256": "b" * 64,
            },
        },
        writes={"修订 1": ["model/META-model-v02.xlsx", "model/META-model-v03.xlsx"]},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "解析模型（修订 1）" in labels
    assert "复审 1" not in labels, "a mismatched revision must not be re-reviewed"
    assert "知识层修订草案" not in labels and "产物核验" not in labels
    assert result["ok"] is False
    assert any("回报 model/META-model-v02.xlsx" in i and "磁盘最大版本 model/META-model-v03.xlsx" in i for i in result["incomplete"])
    assert result["model"] == "model/META-model-v03.xlsx" == _contract_audit_pick("full-coverage", tmp_path)


@pytest.mark.asyncio
async def test_full_memo_only_revision_keeps_the_existing_model(tmp_path: Path) -> None:
    """A model exists and the revision only touched the memo: it reports the
    unchanged v01, which is accepted and carried to the end. Round 2 of the
    language review finds nothing to change (same hash), so no final
    re-review runs."""
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
    assert result["model"] == final == _contract_audit_pick("full-coverage", tmp_path)
    assert _evidence_models(host) == [final]
    assert final in host.prompts[labels.index("复审 1")]
    assert result["language_done"] is True


@pytest.mark.asyncio
async def test_full_revision_without_model_path_fails(tmp_path: Path) -> None:
    host = _fc_host(
        tmp_path,
        by_label={"独立审稿": REVISE, "复审 1": PASS},
        revise_modes={"修订 1": "omit"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is False
    assert any("未回报模型路径" in i for i in result["incomplete"])
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
DEEP_UPDATE = {
    "files": ["research/2026-10-28-deep-FY26Q3/mapping.md", "model/META-model-v02.xlsx"],
    "summary": "updated",
    "sha256": "a" * 64,
}


def _deep_host(tmp_path: Path, by_label: dict[str, Any], **kw: Any) -> DiskHost:
    host = DiskHost(
        workspace=tmp_path,
        by_label={"模型更新": DEEP_UPDATE, **by_label},
        writes={"模型更新": ["model/META-model-v02.xlsx"], **kw.pop("writes", {})},
        **kw,
    )
    host.touch("model/META-model-v01.xlsx")
    return host


@pytest.mark.asyncio
async def test_deep_model_update_mismatch_stops_before_calibration(tmp_path: Path) -> None:
    host = _deep_host(tmp_path, {}, writes={"模型更新": ["model/META-model-v02.xlsx", "model/META-model-v03.xlsx"]})
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    assert result["ok"] is False
    assert any("磁盘最大版本" in i and "v03" in i for i in result["incomplete"])
    labels = host.labels()
    assert "解析模型（模型更新后）" in labels
    assert "校准与对账" not in labels and "审稿" not in labels and "产物核验" not in labels


@pytest.mark.asyncio
async def test_deep_two_model_revisions_and_prompts_name_the_model(tmp_path: Path) -> None:
    """Update saves v02; two revisions save v03 then v04. Calibration and the
    review name v02, each re-review names its round's model, and the return
    value, evidence list and contract audit all pick v04."""
    host = _deep_host(
        tmp_path,
        {"审稿": REVISE, "复审 1": REVISE, "复审 2": PASS, "终稿审稿": PASS},
        revise_modes={"修订 1": "save", "修订 2": "save"},
    )
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    labels = host.labels()
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("校准与对账")]
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("审稿")]
    assert "另存为 model/META-model-v03.xlsx" in host.prompts[labels.index("修订 1")]
    assert "另存为 model/META-model-v04.xlsx" in host.prompts[labels.index("修订 2")]
    assert "model/META-model-v03.xlsx" in host.prompts[labels.index("复审 1")]
    assert "model/META-model-v04.xlsx" in host.prompts[labels.index("复审 2")]
    final = "model/META-model-v04.xlsx"
    assert result["model_file"] == final == _contract_audit_pick("earnings-deep-update", tmp_path)
    assert _evidence_models(host) == [final]
    assert result["ok"] is True, result["incomplete"]


@pytest.mark.asyncio
async def test_deep_reported_v03_but_disk_v04_stops_before_rereview(tmp_path: Path) -> None:
    host = _deep_host(
        tmp_path,
        {
            "审稿": REVISE,
            "复审 1": PASS,
            "修订 1": {"files": ["drafts/x.md", "model/META-model-v03.xlsx"], "summary": "r1", "sha256": "b" * 64},
        },
        writes={"修订 1": ["model/META-model-v03.xlsx", "model/META-model-v04.xlsx"]},
    )
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    labels = host.labels()
    assert "解析模型（修订 1）" in labels
    assert "复审 1" not in labels, "a mismatched revision must not be re-reviewed"
    assert "知识层修订草案" not in labels and "产物核验" not in labels
    assert result["ok"] is False
    assert any("回报 model/META-model-v03.xlsx" in i and "磁盘最大版本 model/META-model-v04.xlsx" in i for i in result["incomplete"])
    assert result["model_file"] == "model/META-model-v04.xlsx" == _contract_audit_pick("earnings-deep-update", tmp_path)


@pytest.mark.asyncio
async def test_deep_report_only_revision_keeps_the_updated_model(tmp_path: Path) -> None:
    host = _deep_host(tmp_path, {"审稿": REVISE, "复审 1": PASS, "终稿审稿": PASS}, revise_modes={"修订 1": "keep"})
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    final = "model/META-model-v02.xlsx"
    assert result["ok"] is True, result["incomplete"]
    assert result["model_file"] == final == _contract_audit_pick("earnings-deep-update", tmp_path)
    assert _evidence_models(host) == [final]


@pytest.mark.asyncio
async def test_deep_revision_without_model_path_fails(tmp_path: Path) -> None:
    host = _deep_host(tmp_path, {"审稿": REVISE, "复审 1": PASS}, revise_modes={"修订 1": "omit"})
    result = await _run("earnings-deep-update", DEEP_PARAMS, host)

    assert result["ok"] is False
    assert any("未回报模型路径" in i for i in result["incomplete"])
    assert "复审 1" not in host.labels()


@pytest.mark.asyncio
async def test_deep_without_model_path_never_resolves() -> None:
    """No model_path: no resolver dispatch, no model in the result, and a
    revision is not asked to report a model."""
    params = {**DEEP_PARAMS, "model_path": None}
    host = ScriptedHost(by_label={"审稿": REVISE, "复审 1": PASS})
    result = await _run("earnings-deep-update", params, host)

    assert not any(label.startswith("解析") for label in host.labels())
    assert result["model_file"] is None
    assert "原样沿用" not in host.prompts[host.labels().index("修订 1")]
    assert result["ok"] is True, result["incomplete"]


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
    """建模串行在前、失败即停（第 2 批）：建模子任务 null、自检未回报通过、
    只声明脚本没有 xlsx、或声明的模型在盘缺失，四种情形都停在成稿之前——
    不派写作、审稿与产物核验。"""
    params = _WORKFLOW_PARAMS["full-coverage"]

    host = ScriptedHost(fail_labels={"建模"})
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模失败" in result["blocked"] and "未产出" in result["blocked"]
    assert "resume_hint" in result and "resume=" in result["resume_hint"]
    labels = host.labels()
    assert "成稿" not in labels, "a failed model build must not dispatch the writer"
    assert "独立审稿" not in labels and "产物核验" not in labels

    # xlsx 在盘、自检也通过，但 recalc / audit --strict 失败：checks_passed=false 拦下。
    host = ScriptedHost(
        by_label={
            "建模": {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "audit --strict failed: 3 broken links on DCF",
                "sha256": "a" * 64,
                "checks_passed": False,
            },
        },
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模自检未通过" in result["blocked"] and "audit --strict failed" in result["blocked"]
    assert "成稿" not in host.labels()

    # 只回报了建模脚本、没有模型文件：不算建模产物。
    host = ScriptedHost(
        by_label={
            "建模": {
                "files": ["research/2026-10-23-fc/build_model.py"],
                "summary": "script written",
                "sha256": "a" * 64,
                "checks_passed": True,
            },
        },
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模失败" in result["blocked"] and "未声明本次的模型文件" in result["blocked"]
    assert "成稿" not in host.labels()

    # 回报了别的目录 / 别的公司的 xlsx（第 3 轮 #3）：规范化路径必须正好等于
    # model/<本次 ticker>-model-vNN.xlsx，archive/AAPL-… 不算本次的建模产物。
    for wrong in ("archive/AAPL-model-v01.xlsx", "research/2026-10-23-fc/META-model-v01.xlsx"):
        host = ScriptedHost(
            by_label={
                "建模": {
                    "files": [wrong],
                    "summary": "built",
                    "sha256": "a" * 64,
                    "checks_passed": True,
                },
            },
        )
        result = await _run("full-coverage", params, host)
        assert result["ok"] is False, wrong
        assert "未声明本次的模型文件 model/META-model-vNN.xlsx" in result["blocked"], wrong
        assert "成稿" not in host.labels()

    # 只读探针对模型实跑 audit --strict 失败（第 3 轮 #4）：不能只信自报的 checks_passed。
    host = ScriptedHost(
        by_label={
            "建模": {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "built, checks all green",
                "sha256": "a" * 64,
                "checks_passed": True,
            },
            "模型审计": {"files": ["model/META-model-v01.xlsx|audit_failed"]},
        },
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "audit --strict" in result["blocked"] and "实跑" in result["blocked"]
    assert "成稿" not in host.labels()

    # 审计探针子任务本身失败：fail-closed，同样停在成稿之前。
    host = ScriptedHost(
        by_label={
            "建模": {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "built, checks all green",
                "sha256": "a" * 64,
                "checks_passed": True,
            },
        },
        fail_labels={"模型审计"},
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "模型审计探针失败" in result["blocked"]
    assert "成稿" not in host.labels()

    # 自检通过、也声明了 xlsx，但探针在盘上找不到（不足契约的 1000 字节下限）。
    host = _ModelProbeHost(
        by_label={
            "建模": {
                "files": ["model/META-model-v01.xlsx"],
                "summary": "built",
                "sha256": "a" * 64,
                "checks_passed": True,
            },
        },
    )
    result = await _run("full-coverage", params, host)
    assert result["ok"] is False
    assert "建模失败" in result["blocked"] and "1000" in result["blocked"]
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
    """Round 3 #1: with v02 already on disk (a previous run's revision), the
    first review sees v02 and a revision names v03 as the next version —
    never reviewing v01 or colliding with the existing v02."""
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
    review_prompt = host.prompts[labels.index("独立审稿")]
    assert "model/META-model-v02.xlsx" in review_prompt, "the first review must see the latest version"
    assert "model/META-model-v01.xlsx" not in review_prompt
    revise_prompt = host.prompts[labels.index("修订 1")]
    assert "另存为 model/META-model-v03.xlsx" in revise_prompt, "the next version rides the latest"
    assert "另存为 model/META-model-v02.xlsx" not in revise_prompt
    assert "model/META-model-v02.xlsx" in host.prompts[labels.index("check-model")]
    assert result["ok"] is True, result["incomplete"]
    assert result["model"] == "model/META-model-v02.xlsx"


@pytest.mark.asyncio
async def test_full_latest_model_failing_audit_blocks_before_the_draft(tmp_path: Path) -> None:
    """The model the gate audits must be the model the run then uses: with a
    passing v01 built and a failing v02 already on disk, the run stops before
    the draft instead of writing from an unaudited v02."""
    host = _fc_host(
        tmp_path,
        by_label={
            "模型审计（最新版本）": {"files": ["model/META-model-v02.xlsx|audit_failed"]},
        },
    )
    host.touch("model/META-model-v02.xlsx")
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is False
    assert "model/META-model-v02.xlsx" in result["blocked"]
    assert "成稿" not in host.labels()


@pytest.mark.asyncio
async def test_full_resume_with_saved_v02_accepts_latest_model(tmp_path: Path) -> None:
    """Resume after an earlier run's revision saved v02 (round 2 #4): this
    run does not revise (the review passes on the first try), and the final
    resolver adopts the disk maximum version instead of aborting on a
    mismatch with the built v01."""
    host = _fc_host(tmp_path, by_label={"独立审稿": PASS})
    host.touch("model/META-model-v02.xlsx")
    result = await _run("full-coverage", FC_PARAMS, host)

    assert result["ok"] is True, result["incomplete"]
    assert result["model"] == "model/META-model-v02.xlsx"
    assert not any("不一致" in item for item in result["incomplete"]), result["incomplete"]
    assert result["model"] == _contract_audit_pick("full-coverage", tmp_path)


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
