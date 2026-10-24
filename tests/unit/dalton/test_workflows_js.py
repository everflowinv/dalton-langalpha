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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.agent.middleware.background_subagent.workflow.engine import (
    WorkflowLimits,
    run_workflow_script,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

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

    def outcome_for(self, label: str, prompt: str) -> Any:
        if label in self.fail_labels:
            return None
        if label in self.by_label:
            return self.by_label[label]
        if label == "产物核验":
            return self.evidence_default(prompt)
        return dict(OUT)

    async def agent(self, prompt: str, opts: dict[str, Any]) -> Any:
        label = str(opts.get("label") or "")
        self.calls.append((label, str(opts.get("agentType"))))
        self.prompts.append(prompt)
        # A real child takes time; an instantly-resolved host future can land
        # the engine's pending-promise check before the JS microtasks drain.
        await asyncio.sleep(0.02)
        return self.outcome_for(label, prompt)

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
async def test_missing_branch_and_evidence_gap_surface_as_incomplete() -> None:
    host = ScriptedHost(fail_labels={"行业支线"})
    host.by_label["产物核验"] = {
        "files": [
            "research/2026-10-23-ic/lenses.json|" + "b" * 64,
            # Bare path, no pipe: the evidence child did not find/hash it.
            "./research/2026-10-23-ic/comps.xlsx",
        ],
        "summary": "1 missing",
    }
    result = await _run("initial-coverage", IC_PARAMS, host)

    assert result["ok"] is False
    assert "ind" in result["missing"], "the null branch is reported by its key"
    assert any("支线失败" in item for item in result["incomplete"])
    assert any("comps.xlsx" in item and "产物缺失" in item for item in result["incomplete"])


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
        if str(opts.get("label") or "").startswith("解析"):
            return {"path": "model/META-model-v01.xlsx", "sha256": "a" * 64}
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
async def test_full_two_model_revisions_end_on_v03(tmp_path: Path) -> None:
    """Round 1 saves v02, round 2 saves v03 (never v02 twice); the return
    value, the evidence list and the contract audit all pick v03."""
    host = _fc_host(
        tmp_path,
        by_label={"独立审稿": REVISE, "复审 1": REVISE, "复审 2": PASS, "终稿审稿": PASS},
        revise_modes={"修订 1": "save", "修订 2": "save"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    labels = host.labels()
    assert "另存为 ./model/META-model-v02.xlsx" in host.prompts[labels.index("修订 1")]
    assert "另存为 model/META-model-v03.xlsx" in host.prompts[labels.index("修订 2")]
    assert "model/META-model-v03.xlsx" in host.prompts[labels.index("复审 2")]
    final = "model/META-model-v03.xlsx"
    assert result["model"] == final
    assert _contract_audit_pick("full-coverage", tmp_path) == final
    assert _evidence_models(host) == [final]
    assert result["ok"] is True, result["incomplete"]
    assert result["revise_rounds"] == 2


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
    unchanged v01, which is accepted and carried to the end."""
    host = _fc_host(
        tmp_path,
        by_label={"独立审稿": REVISE, "复审 1": PASS, "终稿审稿": PASS},
        revise_modes={"修订 1": "keep"},
    )
    result = await _run("full-coverage", FC_PARAMS, host)

    final = "model/META-model-v01.xlsx"
    assert result["ok"] is True, result["incomplete"]
    assert result["model"] == final == _contract_audit_pick("full-coverage", tmp_path)
    assert _evidence_models(host) == [final]
    assert final in host.prompts[host.labels().index("复审 1")]


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
        by_label={"独立审稿": REVISE, "复审 1": REVISE, "复审 2": REVISE},
        revise_modes={"修订 1": "keep", "修订 2": "keep"},
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
