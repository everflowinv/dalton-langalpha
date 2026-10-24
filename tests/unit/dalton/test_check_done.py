"""`dalton_check_done`: each gap class comes back as its own missing item, and
a satisfied contract reports done.

The filesystem seam is a spec'd ``SandboxBackend`` and the database seams are
spec'd ``AsyncConnection``/``AsyncCursor`` (create_autospec), so a renamed
backend method or a changed query shape fails these tests rather than
silently passing. Verdict rows carry the REAL ``dalton_verdicts`` columns
(``word``, ``trigger_kind``, ...) exactly as ``_serialize_row`` emits them.
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import asynccontextmanager
from fnmatch import fnmatch
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

from ptc_agent.agent.backends.sandbox import SandboxBackend
from src.dalton.tools import check_done_tools
from src.dalton.tools.check_done_tools import run_check_done

from src.dalton.database.verdicts import list_verdicts as _REAL_LIST_VERDICTS

WS = "ws-1"
PARAMS = {"ticker": "META", "workspace_dir": ".", "as_of": "2026-10-23"}

DRAFT = "drafts/2026-10-23-initial-coverage.md"
# A journal carrying a reviewer `pass` — the evidence the review stage reads.
JOURNAL_PASS = ".agents/threads/short-1/workflows/task-run-1/journal/审稿.json"


def _draft_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_backend(
    files: dict[str, str],
    *,
    validator_exit: int = 0,
    mtimes: dict[str, int] | None = None,
) -> Any:
    """A spec'd sandbox backend serving ``files`` and scripted commands.

    ``wc -c`` answers sizes from the file table, ``stat -c %Y`` mtimes from
    ``mtimes`` (defaulting to now); every other command carries the validator
    verdict, so a contract command's exit code is the one knob a test needs.
    """
    backend = create_autospec(SandboxBackend, instance=True)
    now = int(time.time())
    mtimes = mtimes or {}

    async def _aread_text(path: str) -> str | None:
        return files.get(path)

    async def _aglob_paths(pattern: str, path: str = ".") -> list[str]:
        return [key for key in files if fnmatch(key, pattern)]

    async def _aexecute(command: str, *, timeout: int | None = None) -> Any:
        class _Response:
            def __init__(self, output: str, exit_code: int) -> None:
                self.output = output
                self.exit_code = exit_code

        # Real commands may carry the cd-into-workspace prefix; a fake that
        # ignored it would mis-answer every probe (the round-4 lesson).
        body = command
        marker = " && "
        if body.startswith("cd ") and marker in body:
            body = body.split(marker, 1)[1]
        if body.startswith("wc -c"):
            path = body.removeprefix("wc -c < ").strip().strip("'\"")
            content = files.get(path)
            if content is None:
                return _Response("", 1)
            return _Response(str(len(content.encode("utf-8"))), 0)
        if body.startswith("stat -c %Y"):
            path = body.removeprefix("stat -c %Y ").strip().strip("'\"")
            if path not in files:
                return _Response("", 1)
            return _Response(str(mtimes.get(path, now)), 0)
        if body.startswith("test -e"):
            path = body.removeprefix("test -e ").strip().strip("'\"")
            return _Response("", 0 if path in files else 1)
        return _Response("stub validator output", validator_exit)

    backend.aread_text.side_effect = _aread_text
    backend.aglob_paths.side_effect = _aglob_paths
    backend.aexecute.side_effect = _aexecute
    return backend


def _patch_knowledge(monkeypatch: pytest.MonkeyPatch, docs: list[str]) -> None:
    """Patch the version query: ``docs`` are the ones with a tool version.

    The connection and cursor are spec'd against psycopg's real async types;
    the rows are (doc_key,) tuples, the shape the query fetches."""

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)

    async def _execute(*_args: Any, **_kwargs: Any) -> None:
        pass

    async def _fetchall() -> list[tuple[str]]:
        return [(doc,) for doc in docs]

    cursor.execute.side_effect = _execute
    cursor.fetchall.side_effect = _fetchall

    # The tool enters the cursor as an async context manager, exactly as it
    # would a real one; the autospec instance carries the dunder mocks.
    cursor.__aenter__.return_value = cursor

    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_db_connection() -> Any:
        yield conn

    monkeypatch.setattr(check_done_tools, "get_db_connection", _get_db_connection)


def _patch_knowledge_error(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _get_db_connection() -> Any:
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover

    monkeypatch.setattr(check_done_tools, "get_db_connection", _get_db_connection)


def _verdict_row(word: str, trigger_kind: str = "manual") -> dict[str, Any]:
    """One row exactly as ``verdicts._serialize_row`` emits it (real columns)."""
    return {
        "verdict_id": "VD-20261023-01",
        "user_id": "user-1",
        "workspace_id": WS,
        "verdict_at": "2026-10-23T10:00:00+00:00",
        "word": word,
        "trigger_kind": trigger_kind,
        "trigger_ref": None,
        "progress": None,
        "next_due": None,
        "payload": {},
        "conversation_thread_id": None,
        "run_id": None,
        "automation_execution_id": None,
        "origin_type": None,
        "created_at": "2026-10-23T10:00:00+00:00",
    }


def _patch_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    from src.dalton.database import verdicts as verdicts_db

    # A spec'd stand-in for the real query function, its side effect carrying
    # the real row shape (or the error arm). The spec source was captured at
    # test-module import — a second patch in one test must not spec the first.
    listed = create_autospec(_REAL_LIST_VERDICTS)

    async def _rows(workspace_id: str, limit: int = 60, since: Any = None):
        if error is not None:
            raise error
        return rows or []

    listed.side_effect = _rows
    monkeypatch.setattr(verdicts_db, "list_verdicts", listed)


IC_DOCS = ["company", "thesis", "debate", "drivers", "management", "agenda"]


def _full_files(
    *,
    draft: str = "# 初筛\n" + "内容" * 400,
    audit_hash: str | None = None,
    with_review_pass: bool = True,
) -> dict[str, str]:
    """Every artifact initial-coverage's contract declares, in healthy shape."""
    files = {
        "drafts/2026-10-23-background.md": "背景" * 200,
        "research/2026-10-23-ic/industry.md": "行业" * 200,
        "research/2026-10-23-ic/lenses.json": json.dumps({"lenses": ["l" * 20] * 3}),
        "drafts/2026-10-23-lenses.md": "镜头" * 200,
        "research/2026-10-23-ic/valuation.md": "估值" * 200,
        "research/2026-10-23-ic/comps.xlsx": "x" * 1200,
        "research/2026-10-23-ic/bullbear-quick.md": "多空" * 200,
        "research/2026-10-23-ic/knowledge-proposal.json": json.dumps(
            {"company": [{"op": "append", "value": "x" * 40}]}
        ),
        DRAFT: draft,
    }
    digest = audit_hash or _draft_hash(draft)
    files["drafts/2026-10-23-initial-coverage.audit.md"] = (
        "# 数字核验报告\n- **草稿哈希**：`" + digest[:16] + "…`\n"
    )
    if with_review_pass:
        # Run-scoped review evidence: the run's status.json names the
        # workflow, and its journal's LAST review record is a pass whose
        # draft_sha256 matches the draft above (hash computed by the tool,
        # the record echoes the writer's).
        files[".agents/threads/short-1/workflows/task-run-1/status.json"] = json.dumps(
            {"run_id": "task-run-1", "name": "initial-coverage", "status": "completed", "updated_at": 1.0}
        )
        files[JOURNAL_PASS] = json.dumps(
            {
                "label": "审稿",
                "dispatches": [
                    {
                        "label": "审稿",
                        "status": "ok",
                        "seq": 7,
                        "recorded_at": 2.0,
                        "result": {
                            "verdict": "pass",
                            "summary": "通过",
                            "draft_sha256": _draft_hash(draft),
                        },
                    }
                ],
            },
            ensure_ascii=False,
        )
    return files


@pytest.mark.asyncio
async def test_all_satisfied_reports_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert result["done"], result
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_missing_artifact_is_reported_with_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _full_files()
    del files["research/2026-10-23-ic/comps.xlsx"]
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("comps.xlsx" in item and "不存在" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_failing_validator_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _full_files(with_review_pass=False)
    files.update(
        {
            "research/2026-10-23-fc/facts.md": "<!-- FACTS-LEDGER v1 -->\n" + "底表" * 200,
            "research/2026-10-23-fc/questions.md": "问题" * 200,
            "research/2026-10-23-fc/ea.md": "A " * 200 + "\n<!-- 完成 -->",
            "research/2026-10-23-fc/eb.md": "B " * 200 + "\n<!-- 完成 -->",
            "research/2026-10-23-fc/ec.md": "C " * 200 + "\n<!-- 完成 -->",
            "research/2026-10-23-fc/ed.md": "D " * 200 + "\n<!-- 完成 -->",
            "model/META-model-v01.xlsx": "x" * 1200,
            "drafts/2026-10-23-memo.md": "# 备忘录\n" + "内容" * 400,
            "research/2026-10-23-fc/model-check.md": "审模型" * 100,
            "research/2026-10-23-fc/knowledge-revision.json": json.dumps(
                {"thesis": {"ops": [{"op": "set", "value": "x" * 40}]}}
            ),
            "drafts/2026-10-23-memo.audit.md": (
                "# 数字核验报告\n- **草稿哈希**：`"
                + _draft_hash("# 备忘录\n" + "内容" * 400)[:16]
                + "…`\n"
            ),
        }
    )
    _patch_knowledge(
        monkeypatch, ["company", "thesis", "debate", "drivers", "management", "models"]
    )
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="full-coverage",
        params=PARAMS,
        backend=_make_backend(files, validator_exit=1),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("校验未通过" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_missing_knowledge_version_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, ["company", "thesis"])
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("debate" in item and "source=tool" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_knowledge_query_failure_is_missing_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge_error(monkeypatch)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("知识层版本无法核对" in item and "请重试" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_verdict_query_failure_is_missing_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, error=RuntimeError("db down"))

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("verdict 无法核对" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_verdict_matched_on_real_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The earnings-deep contract wants word in the five with trigger_kind=
    earnings — rows are matched on the real ``word``/``trigger_kind`` columns,
    so a row with the right word but the wrong trigger does not satisfy it."""
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="manual")])
    files = {"drafts/2026-10-28-deep-FY26Q3.md": "深度" * 300}

    result = await run_check_done(
        workflow="earnings-deep-update",
        params={
            "ticker": "META",
            "quarter": "FY26Q3",
            "workspace_dir": ".",
            "release_date": "2026-10-28",
        },
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("verdict" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_missing_review_pass_is_missing_not_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files(with_review_pass=False)),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("审稿" in item and ("run 目录" in item or "审稿/复审记录" in item) for item in result["missing"])
    assert all("审稿" not in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_stale_check_draft_hash_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """full-coverage's check_draft is required: a sidecar holding the
    pre-revision hash is a missing item, not a warning."""
    draft = "# 备忘录（修订后）\n" + "内容" * 400
    files = {
        "research/2026-10-23-fc/facts.md": "<!-- FACTS-LEDGER v1 -->\n" + "底表" * 200,
        "research/2026-10-23-fc/questions.md": "问题" * 200,
        "research/2026-10-23-fc/ea.md": "A " * 200 + "\n<!-- 完成 -->",
        "research/2026-10-23-fc/eb.md": "B " * 200 + "\n<!-- 完成 -->",
        "research/2026-10-23-fc/ec.md": "C " * 200 + "\n<!-- 完成 -->",
        "research/2026-10-23-fc/ed.md": "D " * 200 + "\n<!-- 完成 -->",
        "model/META-model-v01.xlsx": "x" * 1200,
        "drafts/2026-10-23-memo.md": draft,
        "research/2026-10-23-fc/model-check.md": "审模型" * 100,
        "research/2026-10-23-fc/knowledge-revision.json": json.dumps(
            {"thesis": {"ops": [{"op": "set", "value": "x" * 40}]}}
        ),
        "drafts/2026-10-23-memo.audit.md": (
            "# 数字核验报告\n- **草稿哈希**：`"
            + _draft_hash("# 备忘录\n" + "内容" * 400)[:16]
            + "…`\n"
        ),
    }
    _patch_knowledge(
        monkeypatch, ["company", "thesis", "debate", "drivers", "management", "models"]
    )
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="full-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
    )
    assert not result["done"]
    assert any("旧草稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_latest_anchor_uses_today_and_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``as_of="latest"`` (a topic run without a date) must not raise: paths
    render to the ``latest`` directory and the since-floor is today UTC."""
    files = {
        "research/latest-topic-ai-capex/plan.md": "规划" * 100,
        "research/latest-topic-ai-capex/primary.md": "一手" * 100,
        "research/latest-topic-ai-capex/cluster-c1.md": "簇" * 100,
        "research/latest-topic-ai-capex/cluster-c2.md": "簇" * 100,
        "research/latest-topic-ai-capex/cluster-c3.md": "簇" * 100,
        "research/latest-topic-ai-capex/internal-memory.md": "记忆" * 50,
        "research/latest-topic-ai-capex/guidepoint.md": "专家" * 50,
        "research/latest-topic-ai-capex/cross-check.md": "交叉验证 盲区检查完成",
        "drafts/latest-topic-ai-capex.md": "专题" * 300,
        "research/latest-topic-ai-capex/number-audit.md": "核验" * 50,
    }
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS", trigger_kind="agenda")])

    result = await run_check_done(
        workflow="topic-research",
        params={"topic": "AI capex", "slug": "ai-capex", "workspace_dir": ".", "as_of": "latest"},
        backend=_make_backend(files),
        workspace_id=None,
    )
    assert result["done"] is False  # knowledge unverified (no workspace) but no crash
    assert result["checked"]["since"] == time.strftime("%Y-%m-%d", time.gmtime())


@pytest.mark.asyncio
async def test_validator_params_are_shell_quoted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A param carrying ``$(touch x)`` must arrive as one quoted shell word —
    the validator re-runs in the sandbox, so an unquoted param executes."""
    from src.dalton.workflows.contracts import render_command

    rendered = render_command(
        "python tool.py --ticker {ticker}", {"ticker": "META$(touch x)"}
    )
    assert "'META$(touch x)'" in rendered

    # And the backend never sees an unquoted substitution when the tool runs.
    files = {"research/2026-10-23-flash-FY26Q3.md": "快讯" * 200}
    backend = _make_backend(files)
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    await run_check_done(
        workflow="dalton-earnings-flash",
        params={"ticker": "META", "quarter": "FY26Q3", "as_of": "2026-10-23"},
        backend=backend,
        workspace_id=WS,
    )
    for call in backend.aexecute.call_args_list:
        assert "$(touch" not in call.args[0]


@pytest.mark.asyncio
async def test_weekly_core_files_derive_from_screen_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-ticker core deliverables come off the screening result: a
    ticker in screen.json without its core file is a missing item."""
    files = {
        "dalton/research/weekly-2026-10-23/screen.json": json.dumps(
            {"core": [{"ticker": "META"}, {"ticker": "NFLX"}], "flagged": []}
        ),
        "dalton/research/weekly-2026-10-23/attribution.json": json.dumps({"rows": []}),
        "dalton/research/weekly-2026-10-23/core-META.md": " META " * 100,
        "dalton/drafts/2026-10-23-weekly.md": "周会" * 300,
    }

    result = await run_check_done(
        workflow="weekly-coverage-review",
        params={"home_dir": "dalton", "end": "2026-10-23", "coverage_path": "dalton/coverage.yaml"},
        backend=_make_backend(files),
        workspace_id=None,
    )
    assert not result["done"]
    assert any("core-NFLX.md" in item for item in result["missing"])
    assert not any("core-META.md" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_internal_error_returns_structured_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever breaks inside, the tool answers {done:false, missing:[...]} —
    never an exception the model has to catch."""
    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("contract loader exploded")

    monkeypatch.setattr(check_done_tools, "load_contract", _explode)
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
    )
    assert result["done"] is False
    assert any("无法核对" in item and "请重试" in item for item in result["missing"])


# ------------------------------------------------- round-3 invariants


@pytest.mark.asyncio
async def test_review_pass_from_another_run_does_not_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass recorded under a DIFFERENT workflow's run directory is not this
    run's review — the newest same-workflow run is the scope."""
    files = _full_files()
    # Rename the run's workflow marker so the pass sits in another workflow's
    # directory as far as discovery is concerned.
    files[".agents/threads/short-1/workflows/task-run-1/status.json"] = json.dumps(
        {"run_id": "task-run-1", "name": "some-other-workflow", "status": "completed", "updated_at": 1.0}
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("run 目录" in item and "审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_review_pass_hash_must_match_current_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded pass whose draft_sha256 is the PRE-revision draft is a
    missing item: the review covered a draft that is no longer there."""
    files = _full_files(draft="# 初筛（修订后）\n" + "内容" * 400)
    # The journal still records the pre-revision draft's hash.
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": 2.0,
                    "result": {
                        "verdict": "pass",
                        "summary": "通过",
                        "draft_sha256": _draft_hash("# 初筛\n" + "内容" * 400),
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("旧草稿" in item and "重新审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_run_id_scopes_the_review_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """With run_id given, only that run's journal is consulted — an older
    run's pass does not cover this one."""
    files = _full_files()
    # A second, newer run whose review is a revise.
    files[".agents/threads/short-1/workflows/task-run-2/status.json"] = json.dumps(
        {"run_id": "task-run-2", "name": "initial-coverage", "status": "completed", "updated_at": 9.0}
    )
    files[".agents/threads/short-1/workflows/task-run-2/journal/审稿.json"] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 1,
                    "recorded_at": 8.0,
                    "result": {
                        "verdict": "revise",
                        "summary": "P0",
                        "draft_sha256": "f" * 64,
                    },
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        run_id="task-run-2",
    )
    assert not result["done"]
    assert any("不是 pass" in item for item in result["missing"])




# ------------------------------------------------- round-4 invariants


@pytest.mark.asyncio
async def test_unreadable_review_journal_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A journal file that exists but cannot be read (or parsed) is 无法核对
    — never a silent skip that reads as 'no review'."""
    files = _full_files()
    files[JOURNAL_PASS] = "{broken json"

    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        run_id="task-run-1",
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("不可解析" in item and "请重试" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_last_review_record_judged_even_when_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LAST review record is selected before any filtering: an earlier
    pass followed by a failed re-review dispatch is NOT a pass."""
    files = _full_files()
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 5,
                    "recorded_at": 1.0,
                    "result": {"verdict": "pass", "summary": "ok", "draft_sha256": "a" * 64},
                },
                {
                    "label": "复审 1",
                    "status": "timeout",
                    "seq": 6,
                    "recorded_at": 2.0,
                    "error": "timed out",
                },
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        run_id="task-run-1",
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("最后一次审稿派发未成功" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_review_scan_stays_inside_the_calling_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without run_id, the newest-run discovery globs only this thread's
    directories — a newer run in ANOTHER thread cannot stand in."""
    files = _full_files()
    # Another thread's run, newer than ours.
    files[".agents/threads/other01/workflows/task-run-9/status.json"] = json.dumps(
        {"run_id": "task-run-9", "name": "initial-coverage", "status": "completed", "updated_at": 99.0}
    )
    files[".agents/threads/other01/workflows/task-run-9/journal/审稿.json"] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 1,
                    "recorded_at": 90.0,
                    "result": {"verdict": "pass", "summary": "ok", "draft_sha256": "a" * 64},
                }
            ],
        },
        ensure_ascii=False,
    )
    # Our thread's pass carries a MISMATCHED hash → missing.
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": 2.0,
                    "result": {"verdict": "pass", "summary": "ok", "draft_sha256": "f" * 64},
                }
            ],
        },
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )
    assert not result["done"]
    assert any("旧草稿" in item or "重新审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_unknown_name_reports_undefined_contract() -> None:
    result = await run_check_done(
        workflow="dalton-earnings-preview",
        params={"ticker": "META", "quarter": "FY26Q3", "as_of": "2026-10-23"},
        backend=_make_backend({}),
        workspace_id=WS,
    )
    assert result == {
        "done": False,
        "missing": ["dalton-earnings-preview 尚未定义完成契约"],
        "warnings": [],
        "checked": {
            "workflow": "dalton-earnings-preview",
            "since": result["checked"]["since"],
            "run_id": None,
        },
    }


def _freeze_execution_day(monkeypatch: pytest.MonkeyPatch, day: str) -> None:
    """Pin the tool's execution clock so mtime math cannot drift across a
    real midnight; mtimes then state exactly where they sit between the
    report day and the execution day."""
    from datetime import datetime as _dt

    frozen = _dt.fromisoformat(f"{day}T00:00:00+00:00")

    def _today() -> Any:
        return frozen

    monkeypatch.setattr(check_done_tools, "_today_start_utc", _today)


# ------------------------------------------------- round-5 invariants


@pytest.mark.asyncio
async def test_modified_on_today_ignores_report_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``modified_on: "today"`` floors at the EXECUTION day: a model written
    on the release date — between release_date and today — is not fresh,
    even though release_date sits in the params."""
    release_day = "2026-10-20"
    params = {
        "ticker": "META",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": release_day,
        "model_path": "model/META-model-v01.xlsx",
    }
    files = {
        "drafts/2026-10-20-deep-FY26Q3.md": "深度" * 300,
        "research/2026-10-20-deep-FY26Q3/reconcile.md": "对账" * 50,
        "research/2026-10-20-deep-FY26Q3/reconcile.json": '{"wrong": 0, "partial": 0}',
        "research/2026-10-20-deep-FY26Q3/thesis-calibration.md": "校准" * 100,
        "research/2026-10-20-deep-FY26Q3/management-delta.md": "指引" * 50,
        "research/2026-10-20-deep-FY26Q3/knowledge-revision.json": '{"thesis": {}}',
        "research/2026-10-20-deep-FY26Q3/mapping.md": "映射" * 50,
        "research/2026-10-20-deep-FY26Q3/model-delta.md": "模型增量" * 50,
        "model/META-model-v01.xlsx": "x" * 1200,
    }
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    _freeze_execution_day(monkeypatch, "2026-10-23")

    from datetime import datetime as _dt, timezone as _tz

    # Explicitly between the release day (10-20) and the execution day (10-23).
    stale = int(_dt(2026, 10, 20, 12, tzinfo=_tz.utc).timestamp())
    execution_day_start = int(_dt(2026, 10, 23, tzinfo=_tz.utc).timestamp())
    mtimes = {"model/META-model-v01.xlsx": stale}
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(files, mtimes=mtimes),
        workspace_id=WS,
    )
    assert any("不是本轮写的" in item and "model-v01" in item for item in result["missing"])

    # Written ON the execution day (any time of it) is fresh.
    mtimes["model/META-model-v01.xlsx"] = execution_day_start
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(files, mtimes=mtimes),
        workspace_id=WS,
    )
    assert not any("不是本轮写的" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_one_final_model_version_across_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A higher version on disk is THE final model everywhere: mtime and
    size speak only about it — an old-mtime v01 does not fail freshness,
    and a stale v02 does."""
    from datetime import datetime as _dt, timezone as _tz

    params = {
        "ticker": "META",
        "quarter": "FY26Q3",
        "workspace_dir": ".",
        "release_date": "2026-10-20",
        "model_path": "model/META-model-v01.xlsx",
    }
    files = {
        "drafts/2026-10-20-deep-FY26Q3.md": "深度" * 300,
        "research/2026-10-20-deep-FY26Q3/reconcile.md": "对账" * 50,
        "research/2026-10-20-deep-FY26Q3/reconcile.json": '{"wrong": 0, "partial": 0}',
        "research/2026-10-20-deep-FY26Q3/thesis-calibration.md": "校准" * 100,
        "research/2026-10-20-deep-FY26Q3/management-delta.md": "指引" * 50,
        "research/2026-10-20-deep-FY26Q3/knowledge-revision.json": '{"thesis": {}}',
        "research/2026-10-20-deep-FY26Q3/mapping.md": "映射" * 50,
        "research/2026-10-20-deep-FY26Q3/model-delta.md": "模型增量" * 50,
        "model/META-model-v01.xlsx": "x" * 1200,
        "model/META-model-v02.xlsx": "y" * 1200,
    }
    _patch_knowledge(monkeypatch, [])
    _patch_verdicts(monkeypatch, [_verdict_row("NO_CHANGE", trigger_kind="earnings")])
    _freeze_execution_day(monkeypatch, "2026-10-23")

    old = int(_dt(2026, 10, 20, 12, tzinfo=_tz.utc).timestamp())
    fresh = int(_dt(2026, 10, 23, tzinfo=_tz.utc).timestamp())
    # v01 old, v02 fresh: only v02 is checked — v01's stale mtime is history.
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(
            files,
            mtimes={"model/META-model-v01.xlsx": old, "model/META-model-v02.xlsx": fresh},
        ),
        workspace_id=WS,
    )
    assert not any("不是本轮写的" in item for item in result["missing"])

    # v02 stale → the ONE final file fails freshness.
    result = await run_check_done(
        workflow="earnings-deep-update",
        params=params,
        backend=_make_backend(
            files,
            mtimes={"model/META-model-v01.xlsx": old, "model/META-model-v02.xlsx": old},
        ),
        workspace_id=WS,
    )
    flagged = [item for item in result["missing"] if "不是本轮写的" in item]
    assert flagged and all("model-v02" in item for item in flagged), flagged


@pytest.mark.asyncio
async def test_no_thread_identity_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without run_id AND without thread_id the run cannot be located — a
    missing entry, never a widened cross-thread glob."""
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])

    result = await run_check_done(
        workflow="initial-coverage",
        params=PARAMS,
        backend=_make_backend(_full_files()),
        workspace_id=WS,
        run_id=None,
        thread_id=None,
    )
    assert not result["done"]
    assert any("缺少线程身份" in item for item in result["missing"])


# ------------------------------------------------- manual fallback path
# workflow.enabled=false: no run journal exists, so `manual: true` lets the
# reviewer JSON the analyst saved at the contract's manual_sidecar stand in —
# checked the same way (pass + draft_sha256 of the CURRENT draft).

MANUAL_SIDECAR = "research/2026-10-23-ic/review.json"
MANUAL_PARAMS = {**PARAMS, "manual": True}


def _manual_files(draft_sha: str | None = None, verdict: str = "pass") -> dict[str, str]:
    files = _full_files(with_review_pass=False)
    files[MANUAL_SIDECAR] = json.dumps(
        {
            "verdict": verdict,
            "summary": "ok",
            "must_fix": [],
            "draft_sha256": draft_sha or _draft_hash(files[DRAFT]),
        },
        ensure_ascii=False,
    )
    return files


async def _manual_check(monkeypatch: pytest.MonkeyPatch, files: dict[str, str], params: dict[str, Any]) -> dict[str, Any]:
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    return await run_check_done(
        workflow="initial-coverage",
        params=params,
        backend=_make_backend(files),
        workspace_id=WS,
        thread_id="short-1",
    )


@pytest.mark.asyncio
async def test_manual_matching_pass_sidecar_satisfies_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _manual_check(monkeypatch, _manual_files(), MANUAL_PARAMS)
    assert result["done"], result
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_manual_stale_sidecar_hash_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _manual_check(monkeypatch, _manual_files(draft_sha="f" * 64), MANUAL_PARAMS)
    assert not result["done"]
    assert any("旧草稿" in item and "重新审稿" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_sidecar_last_verdict_must_be_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _manual_files()
    passed = json.loads(files[MANUAL_SIDECAR])
    files[MANUAL_SIDECAR] = json.dumps([passed, {**passed, "verdict": "revise"}])
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"]
    assert any("不是 pass" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_without_sidecar_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _manual_files()
    del files[MANUAL_SIDECAR]
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"]
    assert any(MANUAL_SIDECAR in item for item in result["missing"])


@pytest.mark.asyncio
async def test_without_manual_flag_sidecar_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _manual_check(monkeypatch, _manual_files(), PARAMS)
    assert not result["done"]
    assert any("run 目录" in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_with_existing_run_dir_still_uses_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow run exists for this thread: its journal (here a revise)
    decides, and a passing manual sidecar cannot override it."""
    files = _full_files()
    files[JOURNAL_PASS] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {
                    "label": "审稿",
                    "status": "ok",
                    "seq": 7,
                    "recorded_at": 2.0,
                    "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64},
                }
            ],
        },
        ensure_ascii=False,
    )
    files[MANUAL_SIDECAR] = _manual_files()[MANUAL_SIDECAR]
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"]
    assert any("不是 pass" in item and "本次 run" in item for item in result["missing"])


# ------------------------------------------------- chief_of_staff binding


def _tool_call(workflow: str) -> dict[str, Any]:
    return {
        "name": "dalton_check_done",
        "args": {"workflow": workflow, "params": {"home_dir": ".", "end": "2026-10-23", "coverage_path": "c.json"}},
        "id": "call-1",
        "type": "tool_call",
    }


@pytest.mark.asyncio
async def test_chief_of_staff_check_done_only_accepts_weekly_review() -> None:
    from src.dalton.tools import build_dalton_tools

    tools = {t.name: t for t in build_dalton_tools("chief_of_staff", enabled=True)}
    assert "dalton_check_done" in tools
    tool = tools["dalton_check_done"]

    refused = json.loads((await tool.ainvoke(_tool_call("initial-coverage"))).content)
    assert refused["done"] is False
    assert refused["missing"] == ["该角色只能核对 weekly-coverage-review"]

    # weekly-coverage-review passes the role gate and reaches the real check
    # (no backend bound here → the no-backend missing, not the role refusal).
    weekly = json.loads((await tool.ainvoke(_tool_call("weekly-coverage-review"))).content)
    assert weekly["done"] is False
    assert weekly["missing"] == ["当前没有可用的文件后端，无法核对工作区产物。"]


@pytest.mark.asyncio
async def test_analyst_check_done_has_no_role_restriction() -> None:
    tool = check_done_tools.create_check_done_tools(None)[0]
    result = json.loads((await tool.ainvoke(_tool_call("initial-coverage"))).content)
    assert all("该角色" not in item for item in result["missing"])


@pytest.mark.asyncio
async def test_manual_sidecar_refused_while_an_unfinished_run_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An in-flight or crashed run has workflow.js and a journal but no
    status.json yet: that is still a run, so a passing manual review.json
    must not stand in for the journal's revise."""
    files = _manual_files()
    files[".agents/threads/short-1/workflows/task-run-3/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-3/journal/审稿.json"] = json.dumps(
        {
            "label": "审稿",
            "dispatches": [
                {"label": "审稿", "status": "ok", "seq": 1, "recorded_at": 1.0,
                 "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64}}
            ],
        },
        ensure_ascii=False,
    )
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"], result


@pytest.mark.asyncio
async def test_unfinished_newer_run_blocks_falling_back_to_an_older_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older finished run passed review; a newer run on the same thread is
    still going (no status.json) and its review said revise. Without run_id the
    check must not fall back to the older pass."""
    files = _full_files()  # includes the finished task-run-1 with a pass
    files[".agents/threads/short-1/workflows/task-run-4/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-4/journal/审稿.json"] = json.dumps(
        {"label": "审稿", "dispatches": [{"label": "审稿", "status": "ok", "seq": 1, "recorded_at": 9.0,
          "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64}}]},
        ensure_ascii=False,
    )
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    result = await run_check_done(
        workflow="initial-coverage", params=PARAMS, backend=_make_backend(files), workspace_id=WS, thread_id="short-1"
    )
    assert not result["done"]
    assert any("未结束" in item for item in result["missing"]), result["missing"]


@pytest.mark.asyncio
async def test_corrupt_newer_status_blocks_falling_back_to_an_older_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer run's status.json is truncated: it cannot be told apart from the
    newest run of this workflow, so the older pass must not stand in."""
    files = _full_files()
    files[".agents/threads/short-1/workflows/task-run-6/workflow.js"] = "return 1;"
    files[".agents/threads/short-1/workflows/task-run-6/status.json"] = '{"name": "initial-cov'
    _patch_knowledge(monkeypatch, IC_DOCS)
    _patch_verdicts(monkeypatch, [_verdict_row("NEW_THESIS")])
    result = await run_check_done(
        workflow="initial-coverage", params=PARAMS, backend=_make_backend(files), workspace_id=WS, thread_id="short-1"
    )
    assert not result["done"]
    assert any("损坏" in item for item in result["missing"]), result["missing"]


@pytest.mark.asyncio
async def test_manual_sidecar_refused_when_only_a_journal_marks_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch snapshot (workflow.js) failed to write, but the run's journal
    exists: that is a run, so the manual review.json does not count."""
    files = _manual_files()
    files[".agents/threads/short-1/workflows/task-run-5/journal/审稿.json"] = json.dumps(
        {"label": "审稿", "dispatches": [{"label": "审稿", "status": "ok", "seq": 1, "recorded_at": 1.0,
          "result": {"verdict": "revise", "summary": "P0", "draft_sha256": "f" * 64}}]},
        ensure_ascii=False,
    )
    result = await _manual_check(monkeypatch, files, MANUAL_PARAMS)
    assert not result["done"], result
