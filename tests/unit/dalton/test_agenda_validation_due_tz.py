"""验证项到期时刻的统一口径（batch 5 / F 线）。

到期验证项保留原始时刻与时区（无时区时按标的所属交易所时区解释），只有日期
时按该时区当天结束时刻（23:59:59）才算到期；不再用 autonomy.timezone（默认
上海）的本地日期比较，否则美股验证项会在美东日落后（上海已翻日）提前一天被
判到期。议程探针、总监探针与看板共用 `src/dalton/autonomy/due.py` 的同一套
判断。
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from typing import Any
from unittest.mock import create_autospec
from zoneinfo import ZoneInfo

import pytest

from src.dalton.autonomy.board import build_coverage_board
from src.dalton.autonomy.due import market_timezone, parse_due_moment, validation_is_due
from src.dalton.autonomy.probes.agenda import probe_agenda
from src.dalton.autonomy.probes.director import _count_agenda_actionable

ET = ZoneInfo("America/New_York")
SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def _doc(due: str | None) -> dict:
    return {
        "next_validations": [
            {
                "id": "V-001",
                "what": "财报电话会后核对 Search 货币化口径",
                "due": due,
                "status": "pending",
            }
        ]
    }


async def _validation_keys(ticker: str, due: str | None, now: datetime) -> list[str]:
    sigs, _ = await probe_agenda(
        workspace_id=None,
        ticker=ticker,
        watch_state={},
        agenda_doc=_doc(due),
        now=now,
    )
    return [s.signal_key for s in sigs if s.signal_key.startswith("validation:")]


# ---------------------------------------------------------------------------
# 共享到期口径（src/dalton/autonomy/due.py）
# ---------------------------------------------------------------------------


def test_market_timezone_follows_the_ticker_exchange() -> None:
    assert str(market_timezone("GOOGL")) == "America/New_York"
    assert str(market_timezone("0700.HK")) == "Asia/Hong_Kong"
    assert str(market_timezone("600519.SS")) == "Asia/Shanghai"


def test_parse_due_moment_keeps_moment_and_tz() -> None:
    from datetime import timedelta

    aware = parse_due_moment("2026-10-28T16:30:00-04:00", ET)
    assert aware is not None
    assert aware.utcoffset() == -timedelta(hours=4)
    assert aware.replace(tzinfo=None) == datetime(2026, 10, 28, 16, 30)

    naive = parse_due_moment("2026-10-28T16:30:00", ET)
    assert naive == datetime(2026, 10, 28, 16, 30, tzinfo=ET)

    garbage = parse_due_moment("not-a-date", ET)
    assert garbage is None
    assert parse_due_moment(None, ET) is None


def test_parse_due_moment_date_only_falls_due_at_exchange_18_00() -> None:
    """只有日期的 due 按交易所当地时间 18:00（收盘之后、常见盘后电话会结束之后）
    才算到期：上海周四 07:30 的晨会就能看到周三的美股验证项。"""
    HK = ZoneInfo("Asia/Hong_Kong")

    assert parse_due_moment("2026-10-28", ET) == datetime(2026, 10, 28, 18, 0, tzinfo=ET)
    assert parse_due_moment("2026-10-28", HK) == datetime(2026, 10, 28, 18, 0, tzinfo=HK)
    assert parse_due_moment("2026-10-28", SHANGHAI) == datetime(2026, 10, 28, 18, 0, tzinfo=SHANGHAI)

    # 美股：美东 10/28 17:59 不到期，18:00 起到期
    assert validation_is_due("2026-10-28", "GOOGL", datetime(2026, 10, 28, 17, 59, tzinfo=ET)) is False
    assert validation_is_due("2026-10-28", "GOOGL", datetime(2026, 10, 28, 18, 0, tzinfo=ET)) is True
    # 港股、A 股同理按当地 18:00
    assert validation_is_due("2026-10-28", "0700.HK", datetime(2026, 10, 28, 17, 59, tzinfo=HK)) is False
    assert validation_is_due("2026-10-28", "0700.HK", datetime(2026, 10, 28, 18, 0, tzinfo=HK)) is True
    assert validation_is_due("2026-10-28", "600519.SS", datetime(2026, 10, 28, 17, 59, tzinfo=SHANGHAI)) is False
    assert validation_is_due("2026-10-28", "600519.SS", datetime(2026, 10, 28, 18, 0, tzinfo=SHANGHAI)) is True
    # 带具体时刻的 due 仍按该时刻，不受日期-only 的 18:00 语义影响
    assert validation_is_due("2026-10-28T16:30:00", "GOOGL", datetime(2026, 10, 28, 16, 31, tzinfo=ET)) is True
    assert validation_is_due("2026-10-28T16:30:00", "GOOGL", datetime(2026, 10, 27, 12, 0, tzinfo=ET)) is False
    assert validation_is_due(None, "GOOGL", datetime(2026, 10, 29, tzinfo=ET)) is False


# ---------------------------------------------------------------------------
# 议程探针（probes/agenda.py）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_us_validation_with_time_not_due_before_its_et_moment() -> None:
    """GOOGL 验证项（电话会 2026-10-28 美东 16:30）：美东 10/27 12:00
    （= 上海 10/28 00:00，旧逻辑按上海日期已判到期）不到期。"""
    now_shanghai = datetime(2026, 10, 28, 0, 0, tzinfo=SHANGHAI)
    assert await _validation_keys("GOOGL", "2026-10-28T16:30:00", now_shanghai) == []

    now_et_before = datetime(2026, 10, 27, 12, 0, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28T16:30:00", now_et_before) == []


@pytest.mark.asyncio
async def test_us_validation_with_time_due_after_its_et_moment() -> None:
    """过了原始时刻（美东 10/28 16:30）才到期：10/28 16:31 起触发 P0 信号。"""
    now_et_after = datetime(2026, 10, 28, 16, 31, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28T16:30:00", now_et_after) == [
        "validation:V-001:2026-10-28"
    ]


@pytest.mark.asyncio
async def test_us_date_only_validation_due_at_et_18_00() -> None:
    """只有日期的验证项按美东 18:00 才算到期：美东 10/28 16:31（上海已翻日、
    且旧逻辑按本地日期已判到期）不到期，18:00 起到期。"""
    now_shanghai = datetime(2026, 10, 28, 0, 0, tzinfo=SHANGHAI)  # 美东 10/27 12:00
    assert await _validation_keys("GOOGL", "2026-10-28", now_shanghai) == []

    now_et_daytime = datetime(2026, 10, 28, 16, 31, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28", now_et_daytime) == []

    now_et_1759 = datetime(2026, 10, 28, 17, 59, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28", now_et_1759) == []

    now_et_1800 = datetime(2026, 10, 28, 18, 0, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28", now_et_1800) == [
        "validation:V-001:2026-10-28"
    ]


@pytest.mark.asyncio
async def test_tz_aware_due_moment_is_kept_as_is() -> None:
    """带时区偏移的到期时刻原样保留，不按交易所时区重解释。"""
    # 2026-10-28T16:30+08:00 = 美东 10/28 04:30
    now_et_before = datetime(2026, 10, 28, 4, 0, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28T16:30:00+08:00", now_et_before) == []

    now_et_after = datetime(2026, 10, 28, 4, 31, tzinfo=ET)
    assert await _validation_keys("GOOGL", "2026-10-28T16:30:00+08:00", now_et_after) == [
        "validation:V-001:2026-10-28"
    ]


@pytest.mark.asyncio
async def test_cn_date_only_validation_keeps_shanghai_calendar() -> None:
    """A 股标的的验证项按上海当地时间 18:00 判，不受美股修正影响。"""
    # UTC 10/28 09:59 = 上海 17:59（当地 18:00 未到，不到期）
    now = datetime(2026, 10, 28, 9, 59, tzinfo=UTC)
    assert await _validation_keys("600519.SS", "2026-10-28", now) == []

    # UTC 10/28 10:00 = 上海 18:00 → 到期
    now_due = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    assert await _validation_keys("600519.SS", "2026-10-28", now_due) == [
        "validation:V-001:2026-10-28"
    ]


# ---------------------------------------------------------------------------
# 总监探针（probes/director.py）与看板（board.py）共用同一口径
# ---------------------------------------------------------------------------


def _agenda_doc_with_due(due: str | None) -> dict[str, Any]:
    return {"next_validations": [{"id": "V-001", "what": "核验", "due": due, "status": "pending"}]}


def test_director_count_uses_the_shared_due_rule() -> None:
    """总监日唤醒的 due_validations 与议程探针同一口径：美股日期-only 在上海
    翻日而美东白天时不计数，美东当地 18:00 后才计数。"""
    now_shanghai_midnight = datetime(2026, 10, 28, 0, 0, tzinfo=SHANGHAI)  # 美东 10/27 12:00
    counts = _count_agenda_actionable(
        [_agenda_doc_with_due("2026-10-28")],
        now_dt=now_shanghai_midnight,
        now_date=now_shanghai_midnight.date(),
        doc_markets=[("GOOGL", ET)],
    )
    assert counts["due_validations"] == 0

    now_et_daytime = datetime(2026, 10, 28, 16, 31, tzinfo=ET)
    counts_daytime = _count_agenda_actionable(
        [_agenda_doc_with_due("2026-10-28")],
        now_dt=now_et_daytime,
        now_date=now_et_daytime.date(),
        doc_markets=[("GOOGL", ET)],
    )
    assert counts_daytime["due_validations"] == 0

    now_et_1800 = datetime(2026, 10, 28, 18, 0, tzinfo=ET)
    counts_due = _count_agenda_actionable(
        [_agenda_doc_with_due("2026-10-28")],
        now_dt=now_et_1800,
        now_date=now_et_1800.date(),
        doc_markets=[("GOOGL", ET)],
    )
    assert counts_due["due_validations"] == 1

    # 带具体时刻的 due 按该时刻
    counts_moment = _count_agenda_actionable(
        [_agenda_doc_with_due("2026-10-28T16:30:00-04:00")],
        now_dt=datetime(2026, 10, 28, 16, 31, tzinfo=ET),
        now_date=date(2026, 10, 28),
        doc_markets=[("GOOGL", ET)],
    )
    assert counts_moment["due_validations"] == 1


# ---------------------------------------------------------------------------
# 总监与看板的标的/交易所来源一致（company.ticker+exchange → thesis → 工作区名）
# ---------------------------------------------------------------------------


def test_workspace_market_timezone_sources_in_board_order() -> None:
    """共享解析的来源顺序与看板一致：company.ticker（+exchange 定交易所）→
    thesis.ticker → 工作区名。工作区名"腾讯"直接推断会当美股（美东）。"""
    from src.dalton.autonomy.due import workspace_market_timezone

    HK = ZoneInfo("Asia/Hong_Kong")

    # company 文档优先：工作区名是中文公司名也能定位港股
    ticker, tz = workspace_market_timezone(
        {"ticker": "0700.HK", "exchange": "HKEX"}, {}, "腾讯"
    )
    assert ticker == "0700.HK"
    assert tz is HK

    # exchange 缺失时按 ticker 后缀推断
    ticker_suffix, tz_suffix = workspace_market_timezone({"ticker": "0700.HK"}, {}, "腾讯")
    assert ticker_suffix == "0700.HK"
    assert tz_suffix is HK

    # 没有 company 文档看 thesis，再没有才用工作区名（"腾讯"推断不出后缀 → 美东）
    ticker_thesis, tz_thesis = workspace_market_timezone({}, {"ticker": "600519.SS"}, "腾讯")
    assert ticker_thesis == "600519.SS"
    assert tz_thesis is SHANGHAI
    ticker_ws, tz_ws = workspace_market_timezone({}, {}, "GOOGL")
    assert ticker_ws == "GOOGL"
    assert tz_ws is ET

    # 美股 exchange（NASDAQ/NYSE/OTHER）落回按 ticker 后缀推断
    _, tz_nasdaq = workspace_market_timezone({"ticker": "BABA", "exchange": "NASDAQ"}, {}, "阿里")
    assert tz_nasdaq is ET
    _, tz_hkex = workspace_market_timezone({"ticker": "9988.HK", "exchange": "HKEX"}, {}, "阿里")
    assert tz_hkex is HK


class _CursorProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    async def execute(self, query: Any, params: Any = None) -> Any: ...  # noqa: E704
    async def fetchone(self) -> Any: ...  # noqa: E704
    async def fetchall(self) -> Any: ...  # noqa: E704


class _TxProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704


class _ConnProto:
    async def __aenter__(self) -> Any: ...  # noqa: E704
    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...  # noqa: E704
    def cursor(self, *args: Any, **kwargs: Any) -> Any: ...  # noqa: E704
    def transaction(self) -> Any: ...  # noqa: E704


def _make_spec_conn(cur: Any) -> Any:
    conn = create_autospec(_ConnProto, instance=True)
    conn.__aenter__.return_value = conn
    conn.__aexit__.return_value = None
    tx = create_autospec(_TxProto, instance=True)
    tx.__aenter__.return_value = tx
    tx.__aexit__.return_value = None
    conn.transaction.return_value = tx
    conn.cursor.return_value = cur
    return conn


class _FixedDateTime(datetime):
    """datetime 子类：把 board 的 datetime.now(tz) 固定在指定时刻。"""

    _fixed: datetime = datetime(2026, 10, 28, 12, 0, tzinfo=ET)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return cls._fixed if tz is not None else cls._fixed.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_board_validation_candidate_uses_the_shared_due_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """看板"验证节点到期"候选与探针同一口径：GOOGL 日期-only 在美东 10/28
    17:59 不出候选，当地 18:00 起才出。"""
    from src.dalton.autonomy import board as board_mod

    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    last_sql: dict[str, str] = {"sql": ""}

    async def _exec(sql: str, params: Any = None) -> None:
        last_sql["sql"] = " ".join(str(sql).split())

    cur.execute.side_effect = _exec

    agenda_content = _agenda_doc_with_due("2026-10-28")

    def _fetchall() -> list[dict[str, Any]]:
        sql = last_sql["sql"]
        if "FROM workspaces" in sql:
            return [
                {
                    "workspace_id": "ws-1",
                    "name": "GOOGL",
                    "description": "",
                    "status": "active",
                    "updated_at": None,
                }
            ]
        if "dalton_knowledge_versions" in sql:
            return [
                {
                    "workspace_id": "ws-1",
                    "doc_key": "agenda",
                    "version_no": 1,
                    "content": agenda_content,
                    "created_at": None,
                }
            ]
        return []

    cur.fetchall.side_effect = _fetchall
    cur.fetchone.return_value = {"cnt": 0}
    conn = _make_spec_conn(cur)

    mock_budget = create_autospec(board_mod.get_budget_today)
    mock_budget.return_value = {"cap_usd": 10.0, "spent_usd": 1.0, "open_reserved_usd": 0.0}
    monkeypatch.setattr(board_mod, "get_budget_today", mock_budget)
    monkeypatch.setattr(board_mod, "get_db_connection", lambda: conn)
    mock_coverage = create_autospec(board_mod.get_user_coverage)
    mock_coverage.return_value = {}
    monkeypatch.setattr(board_mod, "get_user_coverage", mock_coverage)
    monkeypatch.setattr(board_mod, "datetime", _FixedDateTime)

    # 美东 10/28 12:00：日期-only 的 due 未到当地 18:00，不产生到期候选
    _FixedDateTime._fixed = datetime(2026, 10, 28, 12, 0, tzinfo=ET)
    board_daytime = await build_coverage_board(user_id="u1")
    assert [c["kind"] for c in board_daytime["candidates"]] == []

    # 美东 10/28 17:59 仍未到期
    _FixedDateTime._fixed = datetime(2026, 10, 28, 17, 59, tzinfo=ET)
    board_1759 = await build_coverage_board(user_id="u1")
    assert [c["kind"] for c in board_1759["candidates"]] == []

    # 美东 10/28 18:00：产生 validation_check 候选
    _FixedDateTime._fixed = datetime(2026, 10, 28, 18, 0, tzinfo=ET)
    board_due = await build_coverage_board(user_id="u1")
    validation_candidates = [c for c in board_due["candidates"] if c["kind"] == "validation_check"]
    assert len(validation_candidates) == 1
    assert validation_candidates[0]["ticker"] == "GOOGL"
    assert "验证节点到期" in validation_candidates[0]["task"]


@pytest.mark.asyncio
async def test_director_and_board_agree_on_a_cn_named_hk_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区名"腾讯"、company 文档 ticker=0700.HK（HKEX）：总监与看板用同一
    共享解析（company.ticker+exchange 优先），对同一条验证项按香港时区给出
    相同的到期判断——旧逻辑拿工作区名推断会当美股，比看板晚约 12 小时。"""
    from src.dalton.autonomy import board as board_mod
    from src.dalton.autonomy.due import workspace_market_timezone

    HK = ZoneInfo("Asia/Hong_Kong")
    company_content = {"ticker": "0700.HK", "exchange": "HKEX"}
    agenda_content = _agenda_doc_with_due("2026-10-28")

    cur = create_autospec(_CursorProto, instance=True)
    cur.__aenter__.return_value = cur
    cur.__aexit__.return_value = None

    last_sql: dict[str, str] = {"sql": ""}

    async def _exec(sql: str, params: Any = None) -> None:
        last_sql["sql"] = " ".join(str(sql).split())

    cur.execute.side_effect = _exec

    def _fetchall() -> list[dict[str, Any]]:
        sql = last_sql["sql"]
        if "FROM workspaces" in sql:
            return [
                {
                    "workspace_id": "ws-tx",
                    "name": "腾讯",
                    "description": "",
                    "status": "active",
                    "updated_at": None,
                }
            ]
        if "dalton_knowledge_versions" in sql:
            return [
                {
                    "workspace_id": "ws-tx",
                    "doc_key": "agenda",
                    "version_no": 1,
                    "content": agenda_content,
                    "created_at": None,
                },
                {
                    "workspace_id": "ws-tx",
                    "doc_key": "company",
                    "version_no": 3,
                    "content": company_content,
                    "created_at": None,
                },
            ]
        return []

    cur.fetchall.side_effect = _fetchall
    cur.fetchone.return_value = {"cnt": 0}
    conn = _make_spec_conn(cur)

    mock_budget = create_autospec(board_mod.get_budget_today)
    mock_budget.return_value = {"cap_usd": 10.0, "spent_usd": 1.0, "open_reserved_usd": 0.0}
    monkeypatch.setattr(board_mod, "get_budget_today", mock_budget)
    monkeypatch.setattr(board_mod, "get_db_connection", lambda: conn)
    mock_coverage = create_autospec(board_mod.get_user_coverage)
    mock_coverage.return_value = {}
    monkeypatch.setattr(board_mod, "get_user_coverage", mock_coverage)
    monkeypatch.setattr(board_mod, "datetime", _FixedDateTime)

    # 香港当地 17:59：两边都不到期
    now_hk_before = datetime(2026, 10, 28, 17, 59, tzinfo=HK)
    _FixedDateTime._fixed = now_hk_before
    board_before = await build_coverage_board(user_id="u1")
    assert [c["kind"] for c in board_before["candidates"]] == []
    counts_before = _count_agenda_actionable(
        [agenda_content],
        now_dt=now_hk_before,
        now_date=now_hk_before.date(),
        doc_markets=[workspace_market_timezone(company_content, {}, "腾讯")],
    )
    assert counts_before["due_validations"] == 0

    # 香港当地 18:00：两边都到期（按香港时区，而不是工作区名推断出的美东）
    now_hk_due = datetime(2026, 10, 28, 18, 0, tzinfo=HK)
    _FixedDateTime._fixed = now_hk_due
    board_due = await build_coverage_board(user_id="u1")
    due_candidates = [c for c in board_due["candidates"] if c["kind"] == "validation_check"]
    assert len(due_candidates) == 1
    assert due_candidates[0]["ticker"] == "0700.HK"
    counts_due = _count_agenda_actionable(
        [agenda_content],
        now_dt=now_hk_due,
        now_date=now_hk_due.date(),
        doc_markets=[workspace_market_timezone(company_content, {}, "腾讯")],
    )
    assert counts_due["due_validations"] == 1


# ---------------------------------------------------------------------------
# knowledge_update 正常路径写入带时刻的 due（schema 契约）
# ---------------------------------------------------------------------------


class _MemoryBackend:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    async def aread_text(self, path: str) -> str | None:
        return self.files.get(path)

    async def awrite_text(self, path: str, content: str) -> None:
        self.files[path] = content


def _install_knowledge_service(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, list[dict[str, Any]]], _MemoryBackend]:
    """test_knowledge_layer 同款内存版：get_workspace/latest_version/insert_version
    全部走替身，apply_update 的 schema 校验真实发生。"""
    from src.dalton.database import knowledge as db_knowledge
    from src.dalton.database import verdicts as db_verdicts
    from src.server.database import workspace as ws_db

    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws-1", "user_id": "user-1"},
        ),
    )

    store: dict[str, list[dict[str, Any]]] = {}

    async def fake_latest_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = store.get(doc_key, [])
        return rows[-1] if rows else None

    async def fake_latest_valid_version(workspace_id: str, doc_key: str) -> dict[str, Any] | None:
        rows = [r for r in store.get(doc_key, []) if r.get("schema_valid", True)]
        return rows[-1] if rows else None

    async def fake_list_all_latest(
        workspace_id: str, valid_only: bool = False
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for k, rows in store.items():
            filtered = [r for r in rows if r.get("schema_valid", True)] if valid_only else rows
            if filtered:
                out[k] = filtered[-1]
        return out

    mock_insert_version = create_autospec(db_knowledge.insert_version)

    async def _insert_version_impl(
        *,
        user_id: str,
        workspace_id: str,
        doc_key: str,
        base_version_no: int | None,
        content: dict[str, Any],
        content_sha256: str,
        ops: list[dict[str, Any]] | None,
        reason: str,
        evidence_refs: list[dict[str, Any]] | None = None,
        source: str = "tool",
        warnings: list[dict[str, Any]] | None = None,
        schema_valid: bool = True,
        edited_path: str | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        rows = store.setdefault(doc_key, [])
        v_no = len(rows) + 1
        row = {
            "knowledge_version_id": f"kv-{doc_key}-{v_no}",
            "workspace_id": workspace_id,
            "doc_key": doc_key,
            "version_no": v_no,
            "base_version_no": base_version_no,
            "content": content,
            "content_sha256": content_sha256,
            "ops": ops,
            "reason": reason,
            "source": source,
            "warnings": warnings or [],
            "schema_valid": schema_valid,
            "edited_path": edited_path,
            "files_written_at": None,
            "created_at": datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC),
        }
        rows.append(row)
        return row

    mock_insert_version.side_effect = _insert_version_impl

    async def fake_mark_files_written(kv_id: str) -> None:
        for rows in store.values():
            for r in rows:
                if r["knowledge_version_id"] == kv_id:
                    r["files_written_at"] = datetime.now(UTC)

    monkeypatch.setattr(db_knowledge, "latest_version", fake_latest_version)
    monkeypatch.setattr(db_knowledge, "latest_valid_version", fake_latest_valid_version)
    monkeypatch.setattr(db_knowledge, "list_all_latest_for_workspace", fake_list_all_latest)
    monkeypatch.setattr(db_knowledge, "insert_version", mock_insert_version)
    monkeypatch.setattr(db_knowledge, "mark_files_written", fake_mark_files_written)
    monkeypatch.setattr(
        db_verdicts, "insert_verdict", create_autospec(db_verdicts.insert_verdict)
    )
    monkeypatch.setattr(
        db_verdicts, "list_verdicts", create_autospec(db_verdicts.list_verdicts, return_value=[])
    )
    return store, _MemoryBackend()


_BASE_AGENDA = {
    "schema_version": 1,
    "as_of": "2026-10-23T00:00:00Z",
    "open_questions": [],
    "next_validations": [
        {
            "id": "V-001",
            "what": "3Q26 财报电话会后核验 Search 货币化口径",
            "due": "2026-10-28",
            "expected": "Search 增速维持 >10%",
            "status": "pending",
        }
    ],
    "watch": {},
    "wake_conditions": [],
}


@pytest.mark.asyncio
async def test_knowledge_update_accepts_a_moment_due_through_the_normal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agenda.schema.json 的 /next_validations[].due 允许带时区偏移的 ISO 时刻：
    通过 knowledge_update 的正常更新路径（apply_update，真实跑 schema 校验）
    能写入，不再被 isoDate-only 的契约挡下。"""
    from src.dalton.knowledge import service

    store, backend = _install_knowledge_service(monkeypatch)

    res_import = await service.import_knowledge_doc(
        user_id="user-1",
        workspace_id="ws-1",
        doc_key="agenda",
        content=_BASE_AGENDA,
        reason="Import agenda",
        backend=backend,
    )
    assert res_import["ok"] is True, res_import.get("message")

    res = await service.apply_update(
        user_id="user-1",
        workspace_id="ws-1",
        doc_key="agenda",
        ops=[
            {
                "op": "set",
                "path": "/next_validations/0/due",
                "value": "2026-10-28T16:30:00-04:00",
            }
        ],
        reason="财报电话会定档美东 16:30，due 写具体时刻",
        base_version=1,
        backend=backend,
    )
    assert res["ok"] is True, res.get("message")
    latest = store["agenda"][-1]["content"]
    assert latest["next_validations"][0]["due"] == "2026-10-28T16:30:00-04:00"
    assert latest["next_validations"][0]["id"] == "V-001"


@pytest.mark.asyncio
async def test_knowledge_update_rejects_non_parsable_due_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"下周"、"2026/10/28"、"16:30"、"2026-10-28T25:00:00-04:00"（形状合法但
    小时越界）都过不了 jsonschema 的 format 检查（未注册 rfc3339-validator，
    FormatChecker 直接放行），必须被写入校验的真实解析挡下；合法的日期与带
    时区时刻仍可写。"""
    from src.dalton.autonomy.due import writable_due_error
    from src.dalton.knowledge import service
    from src.dalton.knowledge.validate import validate_schema

    # 直接锁写入校验器
    for bad in ("下周", "2026/10/28", "16:30", "2026-10-28T25:00:00-04:00"):
        assert writable_due_error(bad) is not None, bad
        doc = {**_BASE_AGENDA, "next_validations": [{**_BASE_AGENDA["next_validations"][0], "due": bad}]}
        errors = validate_schema("agenda", doc)
        due_errors = [e for e in errors if e["code"] == "invalid_due"]
        assert len(due_errors) == 1, bad
        assert due_errors[0]["path"] == "/next_validations/0/due", bad
        assert "YYYY-MM-DD" in due_errors[0]["message"], bad
    # 真实日期（2026-02-30）形状合法也要拒
    assert writable_due_error("2026-02-30") is not None
    # 合法值：None、日期、带时区时刻（Z 与 ±HH:MM、可选秒/毫秒）
    for good in (None, "2026-10-28", "2026-10-28T16:30:00-04:00", "2026-10-28T16:30Z", "2026-10-28T16:30:00.123456+08:00"):
        assert writable_due_error(good) is None, good
        doc = {**_BASE_AGENDA, "next_validations": [{**_BASE_AGENDA["next_validations"][0], "due": good}]}
        assert [e for e in validate_schema("agenda", doc) if e["code"] == "invalid_due"] == [], good

    # 经 apply_update 的正常路径同样拒绝
    store, backend = _install_knowledge_service(monkeypatch)
    res_import = await service.import_knowledge_doc(
        user_id="user-1",
        workspace_id="ws-1",
        doc_key="agenda",
        content=_BASE_AGENDA,
        reason="Import agenda",
        backend=backend,
    )
    assert res_import["ok"] is True, res_import.get("message")

    res_bad = await service.apply_update(
        user_id="user-1",
        workspace_id="ws-1",
        doc_key="agenda",
        ops=[{"op": "set", "path": "/next_validations/0/due", "value": "下周"}],
        reason="Try to write a non-parsable due",
        base_version=1,
        backend=backend,
    )
    assert res_bad["ok"] is False
    assert "due 只接受两种写法" in str(res_bad.get("message"))
    # 拒绝后版本未推进
    assert len(store["agenda"]) == 1


@pytest.mark.parametrize(
    "value",
    ["2026-10-28T16:30:00-04:60", "2026-10-28T16:30:00+08:99", "2026-10-28T16:30:00+15:00"],
)
def test_writable_due_rejects_out_of_range_offsets(value: str) -> None:
    """fromisoformat normalises -04:60 to -05:00 on its own, so an invalid
    offset would be saved and silently shift the due moment."""
    from src.dalton.autonomy.due import writable_due_error

    assert writable_due_error(value) is not None


@pytest.mark.parametrize("value", ["2026-10-28T16:30:00+05:45", "2026-10-28T16:30:00+14:00"])
def test_writable_due_keeps_real_world_offsets(value: str) -> None:
    from src.dalton.autonomy.due import writable_due_error

    assert writable_due_error(value) is None
