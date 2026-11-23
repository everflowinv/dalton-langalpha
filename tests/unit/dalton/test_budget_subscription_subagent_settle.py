"""Unit tests for Round 3 workflow-driver + usage-completeness budget settlement.

Covers:
1. Durable marker propagation:
   - ``RunWorkflow`` driver admission writes ``replay_facts={"workflow_driver": True}``.
   - ``WorkflowDriver`` child dispatch writes ``replay_facts={"workflow_driver_run_id": <driver task_run_id>}``.
   - ``subagent_usage._persist_takes`` stamps ``token_usage["task_run_id"]`` on every persisted task usage row.
   - Ordinary ``Task`` admission does not set ``workflow_driver`` or ``workflow_driver_run_id``.
2. ``reconcile_reservations_sql`` Step 2 (only drops conservative ``reserved_usd`` floor when usage is proven complete):
   - Finished workflow driver + all workflow children & direct subagents have matching ``task_run_id`` usage rows
     + all ``is_byok`` rows have ``per_call_costs`` -> settles by actual usage ($0 for OAuth, actual cash for cash).
   - ``observed_usd > usage_usd`` floor is never dropped.
   - Workflow child missing ``task_run_id`` usage row (even if ``usage_settled_at`` was stamped) -> conservative.
   - Workflow child still ``in_progress`` -> conservative.
   - Direct ``Task`` subagent stamped ``usage_settled_at`` by recovery sweep without a ``task_run_id`` usage row -> conservative.
   - ``is_byok`` usage row missing ``per_call_costs`` (``token_usage=None`` or missing key alongside valid child row) -> conservative.
   - Historical unmarked driver (no ``workflow_driver`` fact) or ``cause='resume'`` -> conservative.
   - ``in_progress`` workflow driver or child within 30m grace -> blocks settlement.
   - Step 1 stays untouched and concurrent reconciliation is DB-authoritative and idempotent.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from ptc_agent.agent.middleware.background_subagent.dispatch import (
    SubagentDispatcher,
    dispatch_background_subagent,
)
from ptc_agent.agent.middleware.background_subagent.middleware import (
    BackgroundSubagentMiddleware,
)
from ptc_agent.agent.middleware.background_subagent.registry import (
    BackgroundTaskRegistry,
    UsageTake,
)
from ptc_agent.agent.middleware.background_subagent.task import (
    WORKFLOW_SUBAGENT_TYPE,
)
from src.dalton.database.budget import reconcile_reservations_sql
from src.server.database.runs import subagent_runs as sr_db
from src.server.services.runs.subagent_usage import _persist_takes
from src.server.services.subagent_run_coordinator import SubagentRunCoordinator


# ---------------------------------------------------------------------------
# 1. Durable marker propagation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admit_task_run_forwards_workflow_driver_and_driver_run_id_selectively():
    """Middleware forwards workflow_driver=True for drivers, workflow_driver_run_id
    for workflow children, and neither for ordinary Task admissions."""
    registry = BackgroundTaskRegistry(thread_id="thread-wf-1")
    ledger = create_autospec(SubagentRunCoordinator, instance=True)
    ledger.start_task_run = AsyncMock(side_effect=["run-wf-1", "run-child-1", "run-task-1"])
    registry.run_ledger = ledger

    mw = BackgroundSubagentMiddleware(
        registry=registry,
        subagent_types=["research", "general-purpose"],
    )

    wf_task = await registry.register(
        tool_call_id="call-wf-1",
        description="Run workflow",
        prompt="workflow script",
        subagent_type=WORKFLOW_SUBAGENT_TYPE,
        run_id="parent-run-1",
    )
    child_task = await registry.register(
        tool_call_id="call-child-1",
        description="Workflow child",
        prompt="child prompt",
        subagent_type="research",
        run_id="parent-run-1",
        owner_task_id=wf_task.task_id,
    )
    normal_task = await registry.register(
        tool_call_id="call-task-1",
        description="Research task",
        prompt="research prompt",
        subagent_type="research",
        run_id="parent-run-1",
    )

    await mw.admit_task_run(
        wf_task,
        cause="init",
        description="Run workflow",
        launch_tool_call_id="call-wf-1",
        parent_run_id="parent-run-1",
        workflow_driver=True,
    )
    await mw.admit_task_run(
        child_task,
        cause="init",
        description="Workflow child",
        launch_tool_call_id="call-child-1",
        parent_run_id=None,
        workflow_driver_run_id="run-wf-1",
    )
    await mw.admit_task_run(
        normal_task,
        cause="init",
        description="Research task",
        launch_tool_call_id="call-task-1",
        parent_run_id="parent-run-1",
    )

    assert ledger.start_task_run.await_count == 3
    wf_kwargs = ledger.start_task_run.await_args_list[0].kwargs
    child_kwargs = ledger.start_task_run.await_args_list[1].kwargs
    normal_kwargs = ledger.start_task_run.await_args_list[2].kwargs

    assert wf_kwargs["workflow_driver"] is True
    assert "workflow_driver_run_id" not in wf_kwargs

    assert child_kwargs["workflow_driver_run_id"] == "run-wf-1"
    assert "workflow_driver" not in child_kwargs

    assert "workflow_driver" not in normal_kwargs
    assert "workflow_driver_run_id" not in normal_kwargs


@pytest.mark.asyncio
async def test_dispatch_background_subagent_forwards_workflow_driver_run_id():
    """dispatch_background_subagent forwards workflow_driver_run_id to mw.admit_task_run
    and omits it when None."""
    registry = BackgroundTaskRegistry(thread_id="thread-disp-1")
    mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
    mw.registry = registry
    mw.admit_task_run = AsyncMock(side_effect=["child-run-1", "plain-run-1"])
    mw.model_client_for = MagicMock(return_value=None)
    mw.check_dispatch_quota = AsyncMock(return_value=None)

    fake_graph = MagicMock()
    fake_graph.default_model = None
    graphs = {"research": fake_graph}

    with patch(
        "ptc_agent.agent.middleware.background_subagent.dispatch.spawn_task_writer",
        new=AsyncMock(return_value=None),
    ):
        await dispatch_background_subagent(
            mw,
            graphs,
            subagent_type="research",
            description="child 1",
            prompt="do child work",
            parent_thread_id="thread-disp-1",
            run_id="parent-run-1",
            owner_task_id="wf-1",
            workflow_driver_run_id="driver-run-99",
        )
        await dispatch_background_subagent(
            mw,
            graphs,
            subagent_type="research",
            description="plain dispatch",
            prompt="do plain work",
            parent_thread_id="thread-disp-1",
            run_id="parent-run-1",
        )

    assert mw.admit_task_run.await_count == 2
    assert mw.admit_task_run.await_args_list[0].kwargs["workflow_driver_run_id"] == "driver-run-99"
    assert mw.admit_task_run.await_args_list[0].kwargs["parent_run_id"] is None
    assert "workflow_driver_run_id" not in mw.admit_task_run.await_args_list[1].kwargs


@pytest.mark.asyncio
async def test_coordinator_forwards_driver_and_child_flags(monkeypatch):
    """SubagentRunCoordinator forwards workflow_driver and workflow_driver_run_id
    selectively without touching ordinary subagents."""
    start_mock = create_autospec(
        sr_db.start_task_run,
        return_value={"task_run_id": str(uuid.uuid4()), "started_at": datetime.now(UTC)},
    )
    monkeypatch.setattr(
        "src.server.services.subagent_run_coordinator.sr_db.start_task_run",
        start_mock,
    )
    coord = SubagentRunCoordinator("thread-coord-1")
    monkeypatch.setattr(coord, "_append_v2_frame", AsyncMock(return_value=None))

    driver_run_id = str(uuid.uuid4())
    await coord.start_task_run(
        task_id="wf-1",
        cause="init",
        subagent_type="workflow",
        parent_run_id="parent-1",
        workflow_driver=True,
    )
    await coord.start_task_run(
        task_id="ch-1",
        cause="init",
        subagent_type="research",
        parent_run_id=None,
        workflow_driver_run_id=driver_run_id,
    )
    await coord.start_task_run(
        task_id="res-1",
        cause="init",
        subagent_type="research",
        parent_run_id="parent-1",
    )

    assert start_mock.await_args_list[0].kwargs["workflow_driver"] is True
    assert "workflow_driver_run_id" not in start_mock.await_args_list[0].kwargs
    assert start_mock.await_args_list[1].kwargs["workflow_driver_run_id"] == driver_run_id
    assert "workflow_driver" not in start_mock.await_args_list[1].kwargs
    assert "workflow_driver" not in start_mock.await_args_list[2].kwargs
    assert "workflow_driver_run_id" not in start_mock.await_args_list[2].kwargs


@pytest.mark.asyncio
async def test_subagent_runs_start_task_run_persists_driver_and_child_replay_facts():
    """sr_db.start_task_run persists workflow_driver and workflow_driver_run_id
    in replay_facts without touching ordinary rows."""
    driver_run_id = str(uuid.uuid4())
    executed_params: list[tuple[str, Any]] = []

    class _FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None):
            executed_params.append((sql, params))

        async def fetchone(self):
            return {"task_run_id": "tr-1", "replay_facts": None}

    class _FakeConn:
        @asynccontextmanager
        async def transaction(self):
            yield

        def cursor(self, row_factory=None):
            return _FakeCursor()

    with (
        patch("src.server.database.workspace.lock_run_workspace", AsyncMock(return_value=None)),
        patch.object(sr_db, "_stand_in_fact", AsyncMock(return_value={})),
        patch.object(sr_db, "find_run_by_launch_call", AsyncMock(return_value=None)),
    ):
        await sr_db.start_task_run(
            task_run_id=driver_run_id,
            thread_id=str(uuid.uuid4()),
            task_id="wf-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=str(uuid.uuid4()),
            launch_tool_call_id="call-wf-1",
            workflow_driver=True,
            conn=_FakeConn(),
        )
        await sr_db.start_task_run(
            task_run_id=str(uuid.uuid4()),
            thread_id=str(uuid.uuid4()),
            task_id="ch-1",
            cause="init",
            subagent_type="research",
            parent_run_id=None,
            launch_tool_call_id="call-ch-1",
            workflow_driver_run_id=driver_run_id,
            conn=_FakeConn(),
        )
        await sr_db.start_task_run(
            task_run_id=str(uuid.uuid4()),
            thread_id=str(uuid.uuid4()),
            task_id="task-1",
            cause="init",
            subagent_type="workflow",
            parent_run_id=str(uuid.uuid4()),
            launch_tool_call_id="call-task-1",
            conn=_FakeConn(),
        )

    insert_runs = [
        params for sql, params in executed_params if "INSERT INTO subagent_runs" in sql
    ]
    assert len(insert_runs) == 3
    assert insert_runs[0][8].obj == {"workflow_driver": True}
    assert insert_runs[1][8].obj == {"workflow_driver_run_id": driver_run_id}
    assert insert_runs[2][8] is None


@pytest.mark.asyncio
async def test_run_workflow_tool_passes_workflow_driver_true_on_admit():
    """RunWorkflow._admit_run passes workflow_driver=True to mw.admit_task_run."""
    from ptc_agent.agent.middleware.background_subagent.workflow.tool import (
        create_run_workflow_tool,
    )

    registry = BackgroundTaskRegistry(thread_id="thread-wf-tool")
    mw = create_autospec(BackgroundSubagentMiddleware, instance=True)
    mw.checkpointer = MagicMock()
    mw.admit_task_run = AsyncMock(return_value="wf-run-id-1")

    dispatcher = create_autospec(SubagentDispatcher, instance=True)
    dispatcher.middleware = mw
    dispatcher.registry = registry

    backend = MagicMock()
    backend.awrite_text = AsyncMock(return_value=None)

    tool = create_run_workflow_tool(
        dispatcher=dispatcher,
        backend=backend,
        thread_id="thread-wf-tool",
        short_thread_id="threadwf",
    )

    script = (
        'const meta = { name: "demo", description: "demo wf" };\n'
        "async function run() { return 'ok'; }\n"
    )
    with (
        patch(
            "ptc_agent.agent.middleware.background_subagent.workflow.tool.spawn_task_writer",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ptc_agent.agent.middleware.background_subagent.workflow.tool.get_config",
            return_value={"metadata": {"run_id": "parent-run-99"}, "configurable": {}},
        ),
    ):
        await tool.ainvoke(
            {
                "name": "RunWorkflow",
                "type": "tool_call",
                "id": "call-rw-1",
                "args": {"script": script},
            }
        )

    mw.admit_task_run.assert_awaited_once()
    assert mw.admit_task_run.await_args.kwargs["workflow_driver"] is True
    assert mw.admit_task_run.await_args.kwargs["cause"] == "init"
    assert mw.admit_task_run.await_args.kwargs["parent_run_id"] == "parent-run-99"


@pytest.mark.asyncio
async def test_persist_takes_stamps_task_run_id_into_token_usage():
    """_persist_takes stamps take.settle_run_id into usage_service._token_usage['task_run_id']
    so budget reconciliation matches usage rows by exact task_run_id."""
    registry = BackgroundTaskRegistry(thread_id="thread-usage-1")
    task = await registry.register(
        tool_call_id="call-u-1",
        description="child",
        prompt="p",
        subagent_type="research",
        run_id="parent-1",
    )
    task_run_id = str(uuid.uuid4())
    take = UsageTake(
        task=task,
        records=[
            {
                "model_name": "claude-opus-5-5-oauth",
                "billing_type": "oauth",
                "input_tokens": 100,
                "output_tokens": 20,
            }
        ],
        tool_usage={},
        settle_run_id=task_run_id,
        response_id="parent-1",
    )

    captured_token_usage: list[dict[str, Any]] = []

    class _FakeUsageService:
        def __init__(self, **_kwargs):
            self._token_usage: dict[str, Any] | None = None

        async def track_llm_usage(self, _records):
            self._token_usage = {"by_model": {}, "per_call_costs": []}
            return self._token_usage

        def record_tool_usage_batch(self, _tool_usage):
            pass

        async def persist_usage(self, **_kwargs):
            captured_token_usage.append(dict(self._token_usage or {}))
            return True

    with (
        patch(
            "src.server.database.runs.subagent_runs.get_task_run_statuses",
            new=AsyncMock(return_value={task_run_id: "completed"}),
        ),
        patch(
            "src.server.services.persistence.usage.UsagePersistenceService",
            _FakeUsageService,
        ),
    ):
        await _persist_takes(
            [take],
            response_id="parent-1",
            thread_id="thread-usage-1",
            workspace_id="ws-1",
            recorded_as="user-1",
            is_byok=True,
        )

    assert len(captured_token_usage) == 1
    assert captured_token_usage[0]["task_id"] == task.task_id
    assert captured_token_usage[0]["task_run_id"] == task_run_id


# ---------------------------------------------------------------------------
# 2. reconcile_reservations_sql Step 2 unit tests
# ---------------------------------------------------------------------------


@dataclass
class _MiniLedgerDB:
    """In-memory behavioral mirror of Postgres tables for reconcile_reservations_sql."""

    reservations: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    subagent_runs: list[dict[str, Any]] = field(default_factory=list)
    usages: list[dict[str, Any]] = field(default_factory=list)
    executed_sql: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @staticmethod
    def _dalton_cash_usd(cu: dict[str, Any], rate: Decimal) -> Decimal:
        tu = cu.get("token_usage")
        if isinstance(tu, dict) and isinstance(tu.get("per_call_costs"), list):
            llm_usd = sum(
                (
                    Decimal(str(c.get("cost") or 0))
                    for c in tu["per_call_costs"]
                    if c.get("billing_type") in ("platform", "byok")
                ),
                Decimal("0"),
            )
            infra = Decimal(str(cu.get("infrastructure_credits") or 0)) / rate
            return llm_usd + infra
        return Decimal(str(cu.get("total_credits") or 0)) / rate

    @staticmethod
    def _is_finished_workflow_driver(sr: dict[str, Any]) -> bool:
        facts = sr.get("replay_facts")
        return (
            sr.get("status") != "in_progress"
            and sr.get("cause") == "init"
            and isinstance(facts, dict)
            and facts.get("workflow_driver") is True
        )

    def _has_task_run_usage(self, run_ref: str, task_run_id: str) -> bool:
        for cu in self.usages:
            if (
                cu["conversation_response_id"] == run_ref
                and cu["msg_type"] == "task"
                and isinstance(cu.get("token_usage"), dict)
                and cu["token_usage"].get("task_run_id") == task_run_id
            ):
                return True
        return False

    async def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        async with self._lock:
            self.executed_sql.append(sql)
            now = datetime.now(UTC)
            grace_cutoff = now - timedelta(minutes=30)

            if "WITH run_usage AS" in sql:
                rate, user_id = params
                for r in self.reservations.values():
                    if r["user_id"] != user_id or r["state"] not in ("open", "released"):
                        continue
                    cr = self.responses.get(r["run_ref"])
                    if not cr or cr["usage_settled_at"] is None:
                        continue
                    run_usages = [
                        u for u in self.usages if u["conversation_response_id"] == r["run_ref"]
                    ]
                    if not run_usages:
                        continue
                    children = [
                        sr for sr in self.subagent_runs if sr["parent_run_id"] == r["run_ref"]
                    ]
                    if any(
                        sr["usage_settled_at"] is None or sr["status"] == "in_progress"
                        for sr in children
                    ):
                        continue
                    usage_usd = sum(
                        (self._dalton_cash_usd(u, rate) for u in run_usages),
                        Decimal("0"),
                    )
                    r["state"] = "settled"
                    r["settled_at"] = now
                    r["settled_usd"] = usage_usd
                    r["settle_source"] = "usage"

            elif "WITH abandoned_usage AS" in sql:
                rate, user_id = params
                for r in self.reservations.values():
                    if r["user_id"] != user_id or r["state"] not in ("open", "released"):
                        continue
                    cr = self.responses.get(r["run_ref"])
                    if not cr or cr["usage_settled_at"] is None or cr["usage_settled_at"] >= grace_cutoff:
                        continue
                    direct_children = [
                        sr for sr in self.subagent_runs if sr["parent_run_id"] == r["run_ref"]
                    ]
                    if any(sr["status"] == "in_progress" for sr in direct_children):
                        continue
                    unsettled = [sr for sr in direct_children if sr["usage_settled_at"] is None]
                    if not any(
                        sr["finalized_at"] is not None and sr["finalized_at"] < grace_cutoff
                        for sr in unsettled
                    ):
                        continue
                    if any(
                        sr["finalized_at"] is None or sr["finalized_at"] >= grace_cutoff
                        for sr in unsettled
                    ):
                        continue

                    run_usages = [
                        u for u in self.usages if u["conversation_response_id"] == r["run_ref"]
                    ]
                    usage_usd = sum(
                        (self._dalton_cash_usd(u, rate) for u in run_usages),
                        Decimal("0"),
                    )
                    usage_rows = len(run_usages)

                    all_byok_have_per_call_costs = all(
                        (not bool(cu.get("is_byok")))
                        or (
                            isinstance(cu.get("token_usage"), dict)
                            and isinstance(cu["token_usage"].get("per_call_costs"), list)
                        )
                        for cu in run_usages
                    )

                    all_direct_subagents_settled_with_usage = True
                    for sr in direct_children:
                        if self._is_finished_workflow_driver(sr):
                            continue
                        if sr["usage_settled_at"] is None or not self._has_task_run_usage(
                            r["run_ref"], sr["task_run_id"]
                        ):
                            all_direct_subagents_settled_with_usage = False
                            break

                    all_workflow_children_accounted = True
                    for drv in direct_children:
                        if not self._is_finished_workflow_driver(drv):
                            continue
                        for child in self.subagent_runs:
                            facts = child.get("replay_facts")
                            if (
                                child.get("thread_id") == drv.get("thread_id")
                                and child.get("parent_run_id") is None
                                and isinstance(facts, dict)
                                and facts.get("workflow_driver_run_id") == drv["task_run_id"]
                            ):
                                if child["status"] == "in_progress" or not self._has_task_run_usage(
                                    r["run_ref"], child["task_run_id"]
                                ):
                                    all_workflow_children_accounted = False
                                    break

                    r["state"] = "settled"
                    r["settled_at"] = now
                    complete = (
                        usage_rows > 0
                        and all_byok_have_per_call_costs
                        and all_direct_subagents_settled_with_usage
                        and all_workflow_children_accounted
                    )
                    if complete:
                        r["settled_usd"] = max(r["observed_usd"], usage_usd)
                        r["settle_source"] = (
                            "usage" if usage_usd >= r["observed_usd"] else "conservative"
                        )
                    else:
                        r["settled_usd"] = max(r["reserved_usd"], r["observed_usd"], usage_usd)
                        r["settle_source"] = "conservative"

    def make_conn(self) -> Any:
        db = self

        class _Cursor:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, sql: str, params: tuple[Any, ...] = ()):
                await db.execute(sql, params)

        class _Conn:
            def cursor(self):
                return _Cursor()

        return _Conn()


def _oauth_ptc_usage(run_ref: str) -> dict[str, Any]:
    return {
        "conversation_usage_id": str(uuid.uuid4()),
        "conversation_response_id": run_ref,
        "msg_type": "ptc",
        "is_byok": True,
        "token_usage": {
            "per_call_costs": [
                {"model_name": "claude-opus-5-5-oauth", "billing_type": "oauth", "cost": "0"}
            ]
        },
        "infrastructure_credits": Decimal("0"),
        "total_credits": Decimal("0"),
    }


def _task_usage(
    run_ref: str,
    task_run_id: str,
    *,
    billing_type: str = "oauth",
    cost: str = "0",
    is_byok: bool = True,
) -> dict[str, Any]:
    return {
        "conversation_usage_id": str(uuid.uuid4()),
        "conversation_response_id": run_ref,
        "msg_type": "task",
        "is_byok": is_byok,
        "token_usage": {
            "task_run_id": task_run_id,
            "per_call_costs": [
                {"model_name": "m", "billing_type": billing_type, "cost": cost}
            ],
        },
        "infrastructure_credits": Decimal("0"),
        "total_credits": Decimal("0"),
    }


@pytest.mark.asyncio
async def test_step2_complete_workflow_and_sibling_usage_settles_at_actual_usage():
    """Workflow driver + all linked workflow children + all direct siblings have matching
    task_run_id usage rows -> settles at $0 for OAuth and at actual cash for cash runs."""
    db = _MiniLedgerDB()
    old = datetime.now(UTC) - timedelta(minutes=35)

    # (a) OAuth workflow + OAuth workflow child -> $0
    db.responses["run-oauth"] = {"usage_settled_at": old}
    db.reservations["res-oauth"] = {
        "reservation_id": "res-oauth",
        "user_id": "u-1",
        "run_ref": "run-oauth",
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0.00"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.usages.extend(
        [
            _oauth_ptc_usage("run-oauth"),
            _task_usage("run-oauth", "child-oauth-1", billing_type="oauth", cost="0"),
        ]
    )
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "wf-oauth",
                "thread_id": "t-1",
                "parent_run_id": "run-oauth",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver": True},
            },
            {
                "task_run_id": "child-oauth-1",
                "thread_id": "t-1",
                "parent_run_id": None,
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": old,
                "replay_facts": {"workflow_driver_run_id": "wf-oauth"},
            },
        ]
    )

    # (b) Workflow driver + cash workflow child ($0.045) + settled cash direct sibling ($0.11) -> $0.155
    db.responses["run-cash"] = {"usage_settled_at": old}
    db.reservations["res-cash"] = {
        "reservation_id": "res-cash",
        "user_id": "u-1",
        "run_ref": "run-cash",
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0.155"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.usages.extend(
        [
            _oauth_ptc_usage("run-cash"),
            _task_usage("run-cash", "child-cash-1", billing_type="byok", cost="0.045"),
            _task_usage("run-cash", "sib-cash-1", billing_type="byok", cost="0.110"),
        ]
    )
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "wf-cash",
                "thread_id": "t-1",
                "parent_run_id": "run-cash",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver": True},
            },
            {
                "task_run_id": "child-cash-1",
                "thread_id": "t-1",
                "parent_run_id": None,
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": old,
                "replay_facts": {"workflow_driver_run_id": "wf-cash"},
            },
            {
                "task_run_id": "sib-cash-1",
                "thread_id": "t-1",
                "parent_run_id": "run-cash",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": old,
                "replay_facts": None,
            },
        ]
    )

    await reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))

    assert db.reservations["res-oauth"]["state"] == "settled"
    assert db.reservations["res-oauth"]["settled_usd"] == Decimal("0")
    assert db.reservations["res-oauth"]["settle_source"] == "usage"

    assert db.reservations["res-cash"]["state"] == "settled"
    assert db.reservations["res-cash"]["settled_usd"] == Decimal("0.155")
    assert db.reservations["res-cash"]["settle_source"] == "usage"


@pytest.mark.asyncio
async def test_step2_missing_workflow_child_or_recovery_stamped_direct_or_degraded_byok_stays_conservative():
    """Every incomplete-usage failure mode stays conservative ($1.00):
    1. Workflow internal child missing its task_run_id usage row (even if usage_settled_at is set).
    2. Workflow internal child still in_progress.
    3. Direct Task subagent stamped usage_settled_at by recovery sweep without a usage row.
    4. BYOK parent row with token_usage=None (pricing failure).
    5. BYOK parent row missing per_call_costs key alongside a valid child row.
    6. Historical unmarked workflow driver (replay_facts=None).
    """
    db = _MiniLedgerDB()
    old = datetime.now(UTC) - timedelta(minutes=35)

    def _seed_resv(run_ref: str) -> None:
        db.responses[run_ref] = {"usage_settled_at": old}
        db.reservations[run_ref] = {
            "reservation_id": run_ref,
            "user_id": "u-1",
            "run_ref": run_ref,
            "state": "released",
            "reserved_usd": Decimal("1.00"),
            "observed_usd": Decimal("0.00"),
            "settled_usd": None,
            "settle_source": None,
        }

    # 1. Missing workflow child usage row (even with usage_settled_at stamped!)
    _seed_resv("run-missing-child")
    db.usages.append(_oauth_ptc_usage("run-missing-child"))
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "wf-1",
                "thread_id": "t-1",
                "parent_run_id": "run-missing-child",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver": True},
            },
            {
                "task_run_id": "child-lost-1",
                "thread_id": "t-1",
                "parent_run_id": None,
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": old,
                "replay_facts": {"workflow_driver_run_id": "wf-1"},
            },
        ]
    )

    # 2. Workflow child still in_progress
    _seed_resv("run-live-child")
    db.usages.append(_oauth_ptc_usage("run-live-child"))
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "wf-2",
                "thread_id": "t-1",
                "parent_run_id": "run-live-child",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver": True},
            },
            {
                "task_run_id": "child-live-2",
                "thread_id": "t-1",
                "parent_run_id": None,
                "cause": "init",
                "status": "in_progress",
                "finalized_at": None,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver_run_id": "wf-2"},
            },
        ]
    )

    # 3. Direct subagent stamped usage_settled_at by recovery sweep WITHOUT usage row
    _seed_resv("run-direct-sweep")
    db.usages.append(_oauth_ptc_usage("run-direct-sweep"))
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "wf-3",
                "thread_id": "t-1",
                "parent_run_id": "run-direct-sweep",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver": True},
            },
            {
                "task_run_id": "direct-swept-3",
                "thread_id": "t-1",
                "parent_run_id": "run-direct-sweep",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": old,
                "replay_facts": None,
            },
        ]
    )

    # 4. BYOK parent row with token_usage=None (pricing failure)
    _seed_resv("run-byok-null")
    db.usages.append(
        {
            "conversation_usage_id": str(uuid.uuid4()),
            "conversation_response_id": "run-byok-null",
            "msg_type": "ptc",
            "is_byok": True,
            "token_usage": None,
            "infrastructure_credits": Decimal("0"),
            "total_credits": Decimal("0"),
        }
    )
    db.subagent_runs.append(
        {
            "task_run_id": "wf-4",
            "thread_id": "t-1",
            "parent_run_id": "run-byok-null",
            "cause": "init",
            "status": "completed",
            "finalized_at": old,
            "usage_settled_at": None,
            "replay_facts": {"workflow_driver": True},
        }
    )

    # 5. BYOK parent row missing per_call_costs key alongside valid child row
    _seed_resv("run-byok-no-pcc")
    db.usages.extend(
        [
            {
                "conversation_usage_id": str(uuid.uuid4()),
                "conversation_response_id": "run-byok-no-pcc",
                "msg_type": "ptc",
                "is_byok": True,
                "token_usage": {"by_model": {}},
                "infrastructure_credits": Decimal("0"),
                "total_credits": Decimal("0"),
            },
            _task_usage("run-byok-no-pcc", "child-5", billing_type="oauth", cost="0"),
        ]
    )
    db.subagent_runs.extend(
        [
            {
                "task_run_id": "wf-5",
                "thread_id": "t-1",
                "parent_run_id": "run-byok-no-pcc",
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": None,
                "replay_facts": {"workflow_driver": True},
            },
            {
                "task_run_id": "child-5",
                "thread_id": "t-1",
                "parent_run_id": None,
                "cause": "init",
                "status": "completed",
                "finalized_at": old,
                "usage_settled_at": old,
                "replay_facts": {"workflow_driver_run_id": "wf-5"},
            },
        ]
    )

    # 6. Historical unmarked workflow driver
    _seed_resv("run-historical")
    db.usages.append(_oauth_ptc_usage("run-historical"))
    db.subagent_runs.append(
        {
            "task_run_id": "wf-hist-6",
            "thread_id": "t-1",
            "parent_run_id": "run-historical",
            "cause": "init",
            "status": "completed",
            "finalized_at": old,
            "usage_settled_at": None,
            "replay_facts": None,
        }
    )

    await reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000"))

    for run_ref in (
        "run-missing-child",
        "run-live-child",
        "run-direct-sweep",
        "run-byok-null",
        "run-byok-no-pcc",
        "run-historical",
    ):
        assert db.reservations[run_ref]["state"] == "settled", run_ref
        assert db.reservations[run_ref]["settled_usd"] == Decimal("1.00"), run_ref
        assert db.reservations[run_ref]["settle_source"] == "conservative", run_ref


@pytest.mark.asyncio
async def test_step2_in_progress_driver_blocks_and_observed_floor_and_sql_contract():
    """Verify in_progress driver blocks settlement, observed_usd floor holds, concurrent
    reconciliation is idempotent, and Step 2 SQL enforces all three completeness proofs."""
    db = _MiniLedgerDB()
    old = datetime.now(UTC) - timedelta(minutes=35)

    # Live driver -> must not settle
    db.responses["run-live-drv"] = {"usage_settled_at": old}
    db.reservations["run-live-drv"] = {
        "reservation_id": "run-live-drv",
        "user_id": "u-1",
        "run_ref": "run-live-drv",
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0.00"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.usages.append(_oauth_ptc_usage("run-live-drv"))
    db.subagent_runs.append(
        {
            "task_run_id": "wf-live",
            "thread_id": "t-1",
            "parent_run_id": "run-live-drv",
            "cause": "init",
            "status": "in_progress",
            "finalized_at": None,
            "usage_settled_at": None,
            "replay_facts": {"workflow_driver": True},
        }
    )

    # Complete workflow with observed_usd > usage_usd -> preserves observed_usd
    db.responses["run-obs"] = {"usage_settled_at": old}
    db.reservations["run-obs"] = {
        "reservation_id": "run-obs",
        "user_id": "u-1",
        "run_ref": "run-obs",
        "state": "released",
        "reserved_usd": Decimal("1.00"),
        "observed_usd": Decimal("0.55"),
        "settled_usd": None,
        "settle_source": None,
    }
    db.usages.append(_oauth_ptc_usage("run-obs"))
    db.subagent_runs.append(
        {
            "task_run_id": "wf-obs",
            "thread_id": "t-1",
            "parent_run_id": "run-obs",
            "cause": "init",
            "status": "completed",
            "finalized_at": old,
            "usage_settled_at": None,
            "replay_facts": {"workflow_driver": True},
        }
    )

    await asyncio.gather(
        reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000")),
        reconcile_reservations_sql(db.make_conn(), "u-1", Decimal("1000")),
    )

    assert db.reservations["run-live-drv"]["state"] == "released"
    assert db.reservations["run-live-drv"]["settled_usd"] is None

    assert db.reservations["run-obs"]["state"] == "settled"
    assert db.reservations["run-obs"]["settled_usd"] == Decimal("0.55")
    assert db.reservations["run-obs"]["settle_source"] == "conservative"

    step1_sql = next(s for s in db.executed_sql if "WITH run_usage AS" in s)
    step2_sql = next(s for s in db.executed_sql if "WITH abandoned_usage AS" in s)

    assert "workflow_driver" not in step1_sql
    assert '{"workflow_driver": true}' in step2_sql
    assert "workflow_driver_run_id" in step2_sql
    assert "all_byok_have_per_call_costs" in step2_sql
    assert "all_direct_subagents_settled_with_usage" in step2_sql
    assert "all_workflow_children_accounted" in step2_sql
    assert "COALESCE(jsonb_typeof(cu.token_usage::jsonb -> 'per_call_costs') = 'array', FALSE)" in step2_sql
    assert "->> 'task_run_id'" in step2_sql
    assert "->> 'task_id'" not in step2_sql
