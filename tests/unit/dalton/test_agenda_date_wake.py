"""Schema-legal date wake conditions and question due_dates actually fire.

提示词第 1 批 #7 + 第 2 轮 #4：`wake_conditions` 的日期在 schema 里只能放进
`params`（顶层 due_date/due/target_date 不存在，探针读不到，到期永不触发）；
`open_questions` 增加可选 `due_date`。到期判断复用 `validation_is_due` 的统一
时刻口径（完整 ISO 时刻按时刻与时区偏移比较；纯日期按标的交易所当地 18:00），
到期超过 30 天的历史项不再逐条唤醒，汇总为每个工作区一条低优先级清理信号。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.knowledge.validate import check_all_schemas, validate_schema

TODAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _doc(wake_conditions: list, questions: list | None = None) -> dict:
    return {
        "schema_version": 1,
        "as_of": "2026-10-20T00:00:00Z",
        "open_questions": questions or [],
        "next_validations": [],
        "watch": {},
        "wake_conditions": wake_conditions,
    }


def _wake(params: dict, **extra) -> dict:
    cond = {
        "id": "C1",
        "kind": "date",
        "params": params,
        "action": "到期复核 P1",
        "enabled": True,
    }
    cond.update(extra)
    return cond


def _question(qid: str = "Q-001", due_date: str | None = "2026-10-19") -> dict:
    return {
        "id": qid,
        "question": "Capex 指引是否兑现",
        "why": "决定 P2",
        "value": "high",
        "urgency": "this_week",
        "origin": "analyst",
        "owner": "analyst",
        "status": "open",
        "attempts": [],
        "created_at": "2026-10-01T00:00:00Z",
        "due_date": due_date,
    }


async def _signals(doc: dict, *, now: datetime = NOW, today: date = TODAY) -> list[str]:
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker="META",
        watch_state={},
        agenda_doc=doc,
        today=today,
        now=now,
    )
    return sorted(s.signal_key for s in sigs)


async def _all(doc: dict, *, now: datetime = NOW, today: date = TODAY) -> list:
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker="META",
        watch_state={},
        agenda_doc=doc,
        today=today,
        now=now,
    )
    return sigs


@pytest.mark.asyncio
async def test_params_date_past_day_fires() -> None:
    keys = await _signals(_doc([_wake({"date": "2026-10-19"})]))
    assert keys == ["action:C1:2026-10-19"]


@pytest.mark.asyncio
async def test_params_at_and_params_due_compatible_keys_fire() -> None:
    keys = await _signals(
        _doc(
            [
                _wake({"at": "2026-10-19"}, id="C2"),
                _wake({"due": "2026-10-18"}, id="C3"),
            ]
        )
    )
    assert keys == ["action:C2:2026-10-19", "action:C3:2026-10-18"]


@pytest.mark.asyncio
async def test_same_day_date_only_due_at_local_midnight() -> None:
    """纯日期按当地日期一开始就算到期（第 3 轮 #8 恢复议程语义）：META 的
    交易所时区是美东，当地早上 9 点（UTC 13:00）当天纯日期已到期。"""
    doc = _doc([_wake({"date": "2026-10-20"})])
    keys = await _signals(doc, now=datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))
    assert keys == ["action:C1:2026-10-20"]
    # 前一个当地日还没开始（UTC 10-20 03:00 = 10-19 23:00 ET）不触发。
    assert (
        await _signals(doc, now=datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc))
        == []
    )


@pytest.mark.asyncio
async def test_full_timestamp_compares_by_its_own_clock() -> None:
    """完整 ISO 时刻按时刻和时区偏移比较（第 2 轮 #4）：
    - 已过的时刻（昨天东京早上）触发；
    - 同“日期”但时刻未到（今晚美东 23:00 = 明天 03:00 UTC）不触发。"""
    past = _doc([_wake({"date": "2026-10-20T01:00:00+08:00"}, id="C4")])
    assert await _signals(past) == ["action:C4:2026-10-20"]

    future = _doc([_wake({"date": "2026-10-20T23:00:00-04:00"}, id="C5")])
    assert await _signals(future) == []


@pytest.mark.asyncio
async def test_future_params_date_does_not_fire() -> None:
    keys = await _signals(_doc([_wake({"date": "2026-10-21"})]))
    assert keys == []


@pytest.mark.asyncio
async def test_disabled_condition_still_respected() -> None:
    keys = await _signals(
        _doc(
            [
                _wake({"date": "2026-10-18"}, enabled=False),
            ]
        )
    )
    assert keys == []


@pytest.mark.asyncio
async def test_legacy_top_level_due_still_fires() -> None:
    # Pre-schema rows (legacy `next_actions`) carried top-level dates; the
    # probe keeps reading them as a fallback.
    doc = _doc([])
    doc["next_actions"] = [
        {"id": "A1", "due_date": "2026-10-18", "action": "旧数据动作", "enabled": True}
    ]
    keys = await _signals(doc)
    assert keys == ["action:A1:2026-10-18"]


@pytest.mark.asyncio
async def test_question_due_date_fires_when_due() -> None:
    keys = await _signals(_doc([], [_question()]))
    assert keys == ["question:Q-001:2026-10-19"]
    question_future = _question(due_date="2026-10-30")
    assert await _signals(_doc([], [question_future])) == []


@pytest.mark.asyncio
async def test_question_due_date_same_day_due_at_local_midnight() -> None:
    doc = _doc([], [_question(due_date="2026-10-20")])
    keys = await _signals(doc, now=datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))
    assert keys == ["question:Q-001:2026-10-20"]


@pytest.mark.asyncio
async def test_question_due_date_full_moment_writes_and_fires_end_to_end() -> None:
    """第 3 轮 #9 的贯通：due_date 写带时区偏移的完整时刻——schema 接受，
    探针按该时刻判断（未到不触发、已到触发）。"""
    question = _question(due_date="2026-10-20T16:30:00-04:00")  # = 20:30 UTC
    assert validate_schema("agenda", _doc([], [question])) == []
    doc = _doc([], [question])
    assert (
        await _signals(doc, now=datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))
        == []
    )
    keys = await _signals(doc, now=datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc))
    assert keys == ["question:Q-001:2026-10-20"]


@pytest.mark.asyncio
async def test_stale_overdue_items_collapse_into_one_cleanup_signal() -> None:
    """到期超过 30 天的历史项不再逐条唤醒（第 3 轮 #11）：汇总成每个工作区
    一条 P2 清理信号，signal_key 按 ISO 周去重；只汇总本来会发信号的项
    （P0/P1 问题、启用的日期唤醒、验证项），低优先级/禁用项不进汇总。"""
    doc = {
        **_doc(
            [
                _wake({"date": "2026-09-01"}, id="C6"),
                _wake({"date": "2026-08-20"}, id="C8", enabled=False),
                _wake({"date": "2026-10-19"}, id="C7"),
            ],
            [
                _question("Q-001", due_date="2026-08-15"),
                _question("Q-002", due_date="2026-10-18"),
                # 低优先级问题：本来就不发信号，过期也不进汇总。
                {**_question("Q-003", due_date="2026-07-01"), "value": "low"},
            ],
        ),
        "next_validations": [
            {"id": "V-001", "what": "旧验证", "due": "2026-08-01", "status": "pending"},
            {"id": "V-002", "what": "新验证", "due": "2026-10-19", "status": "pending"},
        ],
    }
    sigs = await _all(doc)
    keys = sorted(s.signal_key for s in sigs)
    # 近期到期的照常逐条触发。
    assert "action:C7:2026-10-19" in keys
    assert "question:Q-002:2026-10-18" in keys
    assert "validation:V-002:2026-10-19" in keys
    # 过期很久的项只出现在汇总信号里；低优先级与禁用项完全不入账。
    assert "action:C6:2026-09-01" not in keys
    assert "question:Q-001:2026-08-15" not in keys
    assert "validation:V-001:2026-08-01" not in keys
    assert not any("Q-003" in k for k in keys)
    assert not any("C8" in k for k in keys)
    summary = next(s for s in sigs if s.payload.get("kind") == "stale_todos")
    assert summary.priority == "P2"
    assert set(summary.payload["ids"]) == {"C6", "Q-001", "V-001"}
    iso_year, iso_week, _ = TODAY.isocalendar()
    assert summary.signal_key == f"stale_todos:META:{iso_year}-W{iso_week:02d}"
    assert "清理或改期" in summary.summary


@pytest.mark.asyncio
async def test_stale_summary_key_repeats_only_once_per_iso_week() -> None:
    """同一 ISO 周内的两天，汇总信号的 signal_key 相同（一周只提示一次）；
    跨周后换新 key。2026-10-19（周一）与 10-22（周四）同属 2026-W43。"""
    doc = _doc([_wake({"date": "2026-09-01"})])
    monday = await _all(doc, today=date(2026, 10, 19))
    thursday = await _all(doc, today=date(2026, 10, 22))
    next_monday = await _all(doc, today=date(2026, 10, 26))
    assert monday[0].signal_key == thursday[0].signal_key == "stale_todos:META:2026-W43"
    assert next_monday[0].signal_key == "stale_todos:META:2026-W44"


@pytest.mark.asyncio
async def test_today_without_now_shares_one_clock() -> None:
    """第 3 轮 #12：只传 today、不传 now 时，用 today 构造同一时钟——当天
    的纯日期到期项照常触发，日期窗口与时刻判断不各走各的钟。"""
    doc = _doc([_wake({"date": "2026-10-20"})])
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker="META",
        watch_state={},
        agenda_doc=doc,
        today=TODAY,
    )
    assert [s.signal_key for s in sigs] == ["action:C1:2026-10-20"]


@pytest.mark.asyncio
async def test_workspace_id_names_the_summary_signal() -> None:
    doc = _doc([_wake({"date": "2026-09-01"})])
    sigs, _ = await probe_agenda(
        workspace_id="ws-77",
        ticker="META",
        watch_state={},
        agenda_doc=doc,
        today=TODAY,
        now=NOW,
    )
    (summary,) = sigs
    assert summary.signal_key.startswith("stale_todos:ws-77:")


class TestSchemaSide:
    def test_all_schemas_still_valid(self) -> None:
        assert check_all_schemas() != {}

    def _valid_doc(self) -> dict:
        return _doc(
            [_wake({"date": "2026-10-20"})],
            [_question()],
        )

    def test_question_due_date_and_params_date_are_schema_legal(self) -> None:
        assert validate_schema("agenda", self._valid_doc()) == []

    def test_top_level_date_on_wake_condition_is_rejected(self) -> None:
        doc = _doc([])
        doc["wake_conditions"] = [
            {
                "id": "C9",
                "kind": "date",
                "action": "顶层日期不再合法",
                "enabled": True,
                "due_date": "2026-10-18",
            }
        ]
        errors = validate_schema("agenda", doc)
        assert any("due_date" in str(e.get("path", "")) or "additional" in str(e).lower() for e in errors)


class TestWritableDueValidation:
    """写入侧（第 2 轮 #4）：三个日期字段共用 writable_due_error 的口径。"""

    def test_bad_question_due_date_is_rejected(self) -> None:
        doc = self._valid_doc()
        doc["open_questions"][0]["due_date"] = "下周"
        errors = validate_schema("agenda", doc)
        assert any(
            e.get("code") == "invalid_due"
            and str(e.get("path")) == "/open_questions/0/due_date"
            for e in errors
        )

    def test_bad_wake_params_date_is_rejected(self) -> None:
        doc = self._valid_doc()
        doc["wake_conditions"][0]["params"] = {"date": "2026-10-28T25:00:00-04:00"}
        errors = validate_schema("agenda", doc)
        assert any(
            e.get("code") == "invalid_due"
            and str(e.get("path")) == "/wake_conditions/0/params/date"
            for e in errors
        )

    def test_kind_date_without_any_date_key_is_rejected(self) -> None:
        """第 3 轮 #10：kind=date 的唤醒条件必须带至少一个非空日期键，
        否则拒绝写入（没有日期的 date 条件到期探针永远读不到）。"""
        doc = self._valid_doc()
        doc["wake_conditions"][0]["params"] = {"note": "没有日期"}
        errors = validate_schema("agenda", doc)
        assert any(
            e.get("code") == "missing_due"
            and str(e.get("path")) == "/wake_conditions/0/params"
            for e in errors
        )

        doc["wake_conditions"][0]["params"] = {"date": "   "}
        errors = validate_schema("agenda", doc)
        assert any(e.get("code") == "missing_due" for e in errors)

    def test_good_moment_and_null_pass(self) -> None:
        doc = self._valid_doc()
        doc["wake_conditions"][0]["params"] = {"date": "2026-10-28T16:30:00-04:00"}
        doc["open_questions"][0]["due_date"] = None
        assert validate_schema("agenda", doc) == []

    def _valid_doc(self) -> dict:
        return _doc([_wake({"date": "2026-10-20"})], [_question()])
