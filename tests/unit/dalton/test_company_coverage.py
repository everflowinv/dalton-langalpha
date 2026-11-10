"""Company execution coverage is the internal/admin roster union."""

from __future__ import annotations

import copy
import inspect
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml

import src.dalton.autonomy.board as board_mod
import src.dalton.autonomy.preflight as preflight_mod
import src.dalton.coverage as coverage_pkg
import src.dalton.coverage.service as coverage_svc
import src.dalton.database.coverage as coverage_db
from src.dalton.access_tiers import get_user_tier as get_identity_tier
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.coverage.models import DEDICATED_COMPANY_KEYS, build_coverage_doc

ALICE = "alice"
BOB = "bob"


def _coverage_row(
    user_id: str | None,
    ticker: str,
    *,
    tier: str = "watch",
    market: str | None = "US",
    name: str | None = None,
    updated_at: datetime | None = None,
    peers: list[str] | None = None,
    aliases: dict[str, Any] | None = None,
    earnings: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    peer_group: str = "",
    group_id: str | None = None,
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "ticker": ticker,
        "name": ticker if name is None else name,
        "tier": tier,
        "market": market,
        "peer_group": peer_group,
        "peers": list(peers or []),
        "aliases": dict(aliases or {}),
        "earnings": dict(earnings or {}),
        "extra": dict(extra or {}),
        "source": "chat",
        "group_id": group_id,
        "added_at": updated_at,
        "updated_at": updated_at,
    }


def _install_union(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    tiers: dict[str, str],
    failing: frozenset[str] = frozenset(),
) -> tuple[list[str], Any, Any]:
    calls: list[str] = []

    async def _identity(user_id: str | None) -> str:
        uid = str(user_id or "")
        calls.append(uid)
        if uid in failing:
            raise RuntimeError("identity lookup failed")
        return tiers[uid]

    identity = create_autospec(get_identity_tier, side_effect=_identity)
    monkeypatch.setattr(coverage_svc, "get_identity_tier", identity)

    async def _list_all(*, conn: Any = None) -> list[dict[str, Any]]:
        del conn
        return rows

    listed = create_autospec(coverage_db.list_all_coverage_rows, side_effect=_list_all)
    monkeypatch.setattr(coverage_db, "list_all_coverage_rows", listed)
    return calls, identity, listed


def _forbid_member_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    called: list[str] = []

    def _spy(label: str, func: Any) -> Any:
        async def _record(*_args: Any, **_kwargs: Any) -> None:
            called.append(label)

        return create_autospec(func, side_effect=_record)

    for name in (
        "upsert_coverage_rows",
        "delete_coverage_tickers",
        "update_coverage_tier",
        "mark_coverage_rows_group_stale",
        "insert_coverage_group",
        "update_coverage_group_resolved",
        "delete_coverage_group_and_exclusive_members",
        "upsert_earnings_calendar_mirror",
        "list_coverage_groups",
        "list_coverage_rows",
    ):
        func = getattr(coverage_db, name)
        monkeypatch.setattr(coverage_db, name, _spy(f"coverage_db.{name}", func))
    for name in (
        "refresh_due_coverage_groups_for_preflight",
        "ensure_user_earnings_calendar_automation",
        "sync_user_coverage_to_home",
    ):
        func = getattr(coverage_svc, name)
        monkeypatch.setattr(coverage_svc, name, _spy(f"coverage_svc.{name}", func))
    return called


def _companies_by_pair(doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(c["ticker"], c["market"]): c for c in doc["companies"]}


def _assert_normal_empty(doc: dict[str, Any]) -> None:
    normal = build_coverage_doc([])
    assert doc["schema_version"] == normal["schema_version"] == 1
    assert doc["companies"] == []
    assert doc["sources"] == []
    assert doc["tiers"] == normal["tiers"]
    assert doc["tiers"]["core"]["count"] == 0
    assert doc["tiers"]["watch"]["count"] == 0
    assert doc["as_of"] == date.today().isoformat()
    generated = datetime.fromisoformat(doc["generated_at"])
    delta = datetime.now(timezone.utc) - generated
    assert abs(delta.total_seconds()) < 120
    assert "2099" not in doc["generated_at"]


@pytest.mark.asyncio
async def test_company_coverage_unions_internal_and_admin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal and admin rows merge; other identities are left out."""
    reader = inspect.signature(coverage_svc.get_company_coverage)
    renderer = inspect.signature(coverage_svc.render_company_coverage_yaml)
    assert list(reader.parameters) == ["conn"]
    assert reader.parameters["conn"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(renderer.parameters) == ["conn"]
    assert coverage_pkg.get_company_coverage is coverage_svc.get_company_coverage
    assert coverage_pkg.render_company_coverage_yaml is coverage_svc.render_company_coverage_yaml

    included_at = datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc)
    excluded_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    rows = [
        _coverage_row("internal-1", "AAA", tier="core", name="Alpha", updated_at=included_at),
        _coverage_row("internal-1", "AAB", tier="watch", name="Aab", updated_at=included_at),
        _coverage_row("internal-2", "BBB", tier="core", name="Beta", updated_at=included_at),
        _coverage_row("admin-1", "CCC", tier="watch", name="Cee", updated_at=included_at),
        _coverage_row("external-1", "DDD", name="External", updated_at=excluded_at),
        _coverage_row(COMPANY_SYSTEM_USER_ID, "EEE", tier="core", name="Company", updated_at=excluded_at),
        _coverage_row("unknown-1", "FFF", name="Unknown", updated_at=excluded_at),
        _coverage_row("blank-tier-1", "GGG", name="Blank tier", updated_at=excluded_at),
        _coverage_row("failing-1", "HHH", name="Failing", updated_at=excluded_at),
        _coverage_row("failing-1", "HHI", name="Failing again", updated_at=excluded_at),
        _coverage_row(None, "III", name="No user", updated_at=excluded_at),
        _coverage_row("   ", "JJJ", name="Blank user", updated_at=excluded_at),
    ]
    tiers = {
        "internal-1": "internal",
        "internal-2": "internal",
        "admin-1": "admin",
        "external-1": "external",
        # Resolver says admin; the company system user is still excluded.
        COMPANY_SYSTEM_USER_ID: "admin",
        "unknown-1": "unknown",
        "blank-tier-1": "",
        "failing-1": "internal",
    }
    calls, identity, _listed = _install_union(
        monkeypatch,
        rows,
        tiers,
        failing=frozenset({"failing-1"}),
    )
    expected_lookups = [
        "internal-1",
        "internal-2",
        "admin-1",
        "external-1",
        COMPANY_SYSTEM_USER_ID,
        "unknown-1",
        "blank-tier-1",
        "failing-1",
    ]

    first = await coverage_svc.get_company_coverage()
    second = await coverage_svc.get_company_coverage()

    assert calls == expected_lookups + expected_lookups
    assert identity.await_count == len(calls) == 16
    assert len(set(expected_lookups)) == len(expected_lookups)
    assert "" not in calls
    assert "None" not in calls
    assert [c["ticker"] for c in first["companies"]] == ["AAA", "AAB", "BBB", "CCC"]
    assert first["companies"] == second["companies"]
    assert first["tiers"]["core"]["count"] == 2
    assert first["tiers"]["watch"]["count"] == 2
    assert first["generated_at"] == "2026-05-05T08:00:00+00:00"
    assert second["generated_at"] == first["generated_at"]
    assert {c["name"] for c in first["companies"]} == {"Alpha", "Aab", "Beta", "Cee"}


@pytest.mark.asyncio
async def test_company_coverage_duplicate_pair_precedence_and_market_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core beats a newer watch; ties use user_id; distinct markets both remain."""
    same = datetime(2026, 4, 4, tzinfo=timezone.utc)
    latest = datetime(2026, 8, 8, 15, 0, tzinfo=timezone.utc)
    external_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
    rows = [
        _coverage_row(
            "internal-w",
            "nvda",
            tier="watch",
            market=None,
            name="Newer Watch",
            updated_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
            peers=["WATCH"],
            aliases={"search_names": ["WatchAlias"]},
            earnings={"group": "B"},
            peer_group="watch-group",
            extra={"workspace": {"name": "NVDA"}, "note": "watch-note", "group_ids": ["g-watch"]},
            group_id="g-watch",
        ),
        _coverage_row(
            "admin-c",
            "NVDA",
            tier="core",
            market="us",
            name="Older Core",
            updated_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
            peers=["CORE"],
            aliases={"search_names": ["CoreAlias"]},
            earnings={"group": "A"},
            peer_group="core-group",
            extra={"note": "core-note", "private_flag": "core-secret"},
            group_id="g-core",
        ),
        _coverage_row(
            "user-b",
            "AMD",
            tier="core",
            name="From B",
            updated_at=same,
            peers=["B"],
        ),
        _coverage_row(
            "user-a",
            "AMD",
            tier="core",
            name="From A",
            updated_at=same,
            peers=["A"],
        ),
        _coverage_row(
            "internal-us",
            "BABA",
            tier="core",
            market="US",
            name="Alibaba US",
            updated_at=datetime(2026, 3, 3, tzinfo=timezone.utc),
        ),
        _coverage_row(
            "admin-hk",
            "BABA",
            tier="watch",
            market="HK",
            name="Alibaba HK",
            updated_at=latest,
        ),
        _coverage_row(
            "internal-infer",
            "0700.hk",
            tier="watch",
            market=None,
            name="Inferred Watch",
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        _coverage_row(
            "admin-infer",
            "0700.HK",
            tier="core",
            market="hk",
            name="Inferred Core",
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        _coverage_row(
            "external-1",
            "EXT",
            name="External Only",
            updated_at=external_at,
            extra={"workspace": {"name": "EXT"}, "note": "secret-ext", "group_ids": ["g-ext"]},
            group_id="g-ext",
        ),
        _coverage_row(
            "external-1",
            "NVDA",
            market="US",
            name="External NVDA",
            updated_at=external_at,
            peers=["EXT"],
            extra={"private_flag": "ext-secret"},
        ),
    ]
    tiers = {
        "internal-w": "internal",
        "admin-c": "admin",
        "user-a": "internal",
        "user-b": "admin",
        "internal-us": "internal",
        "admin-hk": "admin",
        "internal-infer": "internal",
        "admin-infer": "admin",
        "external-1": "external",
    }
    _install_union(monkeypatch, rows, tiers)

    first = await coverage_svc.get_company_coverage()
    rows.reverse()
    second = await coverage_svc.get_company_coverage()

    assert first["companies"] == second["companies"]
    assert first["tiers"] == second["tiers"]
    assert first["generated_at"] == second["generated_at"] == "2026-08-08T15:00:00+00:00"
    assert first["as_of"] == "2026-08-08"
    assert [(c["ticker"], c["market"], c["tier"]) for c in first["companies"]] == [
        ("0700.HK", "HK", "core"),
        ("AMD", "US", "core"),
        ("BABA", "HK", "watch"),
        ("BABA", "US", "core"),
        ("NVDA", "US", "core"),
    ]
    assert first["tiers"]["core"]["count"] == 4
    assert first["tiers"]["watch"]["count"] == 1
    assert len(first["companies"]) == 5

    by_pair = _companies_by_pair(first)
    nvda = by_pair[("NVDA", "US")]
    assert nvda["name"] == "Older Core"
    assert nvda["peers"] == ["CORE"]
    assert nvda["aliases"] == {"search_names": ["CoreAlias"]}
    assert nvda["earnings"] == {"group": "A"}
    assert nvda["peer_group"] == "core-group"
    assert by_pair[("AMD", "US")]["name"] == "From A"
    assert by_pair[("AMD", "US")]["peers"] == ["A"]
    assert by_pair[("BABA", "US")]["name"] == "Alibaba US"
    assert by_pair[("BABA", "HK")]["name"] == "Alibaba HK"
    assert by_pair[("0700.HK", "HK")]["name"] == "Inferred Core"
    for company in first["companies"]:
        assert list(company.keys()) == list(DEDICATED_COMPANY_KEYS)
    rendered = json.dumps(first)
    for leaked in (
        "2099",
        "External NVDA",
        "External Only",
        "secret-ext",
        "ext-secret",
        "core-secret",
        "watch-note",
        "core-note",
        "g-ext",
        "g-core",
        "g-watch",
        "workspace",
        "user-a",
        "external-1",
        "private_flag",
        "group_ids",
    ):
        assert leaked not in rendered


@pytest.mark.asyncio
async def test_company_coverage_microsecond_updated_at_orders_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later microsecond wins in either order; an exact tie uses user_id."""
    older = datetime(2026, 11, 1, 0, 0, 0, 100000, tzinfo=timezone.utc)
    newer = datetime(2026, 11, 1, 0, 0, 0, 900000, tzinfo=timezone.utc)
    same = datetime(2026, 11, 1, 0, 0, 0, 100000, tzinfo=timezone.utc)
    rows = [
        _coverage_row(
            "user-a",
            "MSFT",
            tier="core",
            market="US",
            name="Older A",
            updated_at=older,
            peers=["OLD"],
            aliases={"search_names": ["OldAlias"]},
            earnings={"group": "old"},
            peer_group="old-group",
        ),
        _coverage_row(
            "user-z",
            "MSFT",
            tier="core",
            market="US",
            name="Newer Z",
            updated_at=newer,
            peers=["NEW"],
            aliases={"search_names": ["NewAlias"]},
            earnings={"group": "new"},
            peer_group="new-group",
        ),
        _coverage_row(
            "user-z",
            "TIE",
            tier="watch",
            market="US",
            name="Tie Z",
            updated_at=same,
            peers=["Z"],
            aliases={"search_names": ["TieZ"]},
            earnings={"group": "z"},
            peer_group="tie-z",
        ),
        _coverage_row(
            "user-a",
            "TIE",
            tier="watch",
            market="US",
            name="Tie A",
            updated_at=datetime(2026, 11, 1, 0, 0, 0, 100000, tzinfo=timezone.utc),
            peers=["A"],
            aliases={"search_names": ["TieA"]},
            earnings={"group": "a"},
            peer_group="tie-a",
        ),
    ]
    _install_union(
        monkeypatch,
        rows,
        {"user-a": "internal", "user-z": "internal"},
    )

    forward = await coverage_svc.get_company_coverage()
    rows.reverse()
    backward = await coverage_svc.get_company_coverage()

    assert forward["companies"] == backward["companies"]
    by_pair = _companies_by_pair(forward)
    msft = by_pair[("MSFT", "US")]
    assert msft["name"] == "Newer Z"
    assert msft["tier"] == "core"
    assert msft["peers"] == ["NEW"]
    assert msft["aliases"] == {"search_names": ["NewAlias"]}
    assert msft["earnings"] == {"group": "new"}
    assert msft["peer_group"] == "new-group"
    tie = by_pair[("TIE", "US")]
    assert tie["name"] == "Tie A"
    assert tie["tier"] == "watch"
    assert tie["peers"] == ["A"]
    assert tie["aliases"] == {"search_names": ["TieA"]}
    assert tie["earnings"] == {"group": "a"}
    assert tie["peer_group"] == "tie-a"
    assert forward["generated_at"] == backward["generated_at"] == "2026-11-01T00:00:00+00:00"
    assert forward["as_of"] == "2026-11-01"


@pytest.mark.asyncio
async def test_company_coverage_does_not_mutate_rows_or_refresh_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the union does not write member rows, groups, or calendar tasks."""
    extra = {
        "workspace": {"name": "NVDA", "status": "active"},
        "note": "secret-note",
        "private_flag": "secret-flag",
        "group_ids": ["g-secret"],
    }
    peers = ["AMD"]
    aliases = {"search_names": ["SecretName"]}
    earnings = {"group": "A"}
    row = _coverage_row(
        "internal-1",
        "NVDA",
        tier="core",
        name="NVIDIA",
        peers=peers,
        aliases=aliases,
        earnings=earnings,
        extra=extra,
        group_id="g-secret",
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    rows = [
        row,
        _coverage_row(
            "external-1",
            "EXT",
            name="External",
            updated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            extra={"note": "ext-note"},
            group_id="g-ext",
        ),
    ]
    snapshot = copy.deepcopy(rows)
    extra_id = id(row["extra"])
    peers_id = id(row["peers"])
    names_id = id(row["aliases"]["search_names"])
    workspace_id = id(row["extra"]["workspace"])
    _install_union(
        monkeypatch,
        rows,
        {"internal-1": "internal", "external-1": "external"},
    )
    writes = _forbid_member_writes(monkeypatch)

    doc = await coverage_svc.get_company_coverage()
    yaml_text = await coverage_svc.render_company_coverage_yaml()

    assert writes == []
    assert rows == snapshot
    assert id(row["extra"]) == extra_id
    assert id(row["peers"]) == peers_id
    assert id(row["aliases"]["search_names"]) == names_id
    assert id(row["extra"]["workspace"]) == workspace_id
    assert row["group_id"] == "g-secret"
    company = doc["companies"][0]
    company["peers"].append("MUTATED")
    company["aliases"]["search_names"].append("MUTATED")
    company["earnings"]["group"] = "Z"
    assert row["peers"] == ["AMD"]
    assert row["aliases"]["search_names"] == ["SecretName"]
    assert row["earnings"] == {"group": "A"}
    assert row["extra"]["private_flag"] == "secret-flag"
    assert "secret-flag" not in yaml_text
    assert "g-secret" not in yaml_text
    assert "ext-note" not in yaml_text
    assert "workspace" not in yaml_text


@pytest.mark.asyncio
async def test_company_preflight_syncs_union_personal_preflight_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Company Home gets the union; a personal calendar run stays on the personal path."""
    included_at = datetime(2026, 6, 6, tzinfo=timezone.utc)
    rows = [
        _coverage_row(
            "internal-1",
            "NVDA",
            tier="core",
            name="NVIDIA",
            updated_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
            extra={"note": "member-note", "workspace": {"name": "NVDA"}},
            group_id="g-member",
        ),
        _coverage_row("admin-1", "AMD", tier="watch", name="AMD", updated_at=included_at),
        _coverage_row(
            "external-1",
            "EXT",
            name="External",
            updated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    _calls, _identity, listed = _install_union(
        monkeypatch,
        rows,
        {"internal-1": "internal", "admin-1": "admin", "external-1": "external"},
    )
    rendered_users: list[str] = []
    refresh_calls: list[tuple[str, bool]] = []
    syncs: list[tuple[str, str | None]] = []

    async def _personal_render(user_id: str, *, conn: Any = None) -> str:
        del conn
        rendered_users.append(user_id)
        return "personal-yaml"

    async def _refresh(
        user_id: str,
        *,
        now: datetime | None = None,
        sync_home: bool = False,
    ) -> dict[str, Any]:
        del now
        refresh_calls.append((user_id, sync_home))
        return {"summary_text": "groups"}

    async def _sync(
        user_id: str,
        *,
        yaml_text: str | None = None,
        backend: Any = None,
        rel_path: str = "dalton/coverage.yaml",
    ) -> bool:
        del backend, rel_path
        syncs.append((user_id, yaml_text))
        return True

    personal_render = create_autospec(
        preflight_mod.render_coverage_yaml,
        side_effect=_personal_render,
    )
    refresh = create_autospec(
        preflight_mod.refresh_due_coverage_groups_for_preflight,
        side_effect=_refresh,
    )
    sync = create_autospec(preflight_mod.sync_user_coverage_to_home, side_effect=_sync)
    ensure = create_autospec(
        coverage_svc.ensure_user_earnings_calendar_automation,
        side_effect=AssertionError("member calendar task"),
    )
    groups = create_autospec(
        coverage_db.list_coverage_groups,
        side_effect=AssertionError("member group refresh"),
    )
    upsert = create_autospec(
        coverage_db.upsert_coverage_rows,
        side_effect=AssertionError("member coverage write"),
    )
    monkeypatch.setattr(preflight_mod, "render_coverage_yaml", personal_render)
    monkeypatch.setattr(preflight_mod, "refresh_due_coverage_groups_for_preflight", refresh)
    monkeypatch.setattr(preflight_mod, "sync_user_coverage_to_home", sync)
    monkeypatch.setattr(coverage_svc, "ensure_user_earnings_calendar_automation", ensure)
    monkeypatch.setattr(coverage_db, "list_coverage_groups", groups)
    monkeypatch.setattr(coverage_db, "upsert_coverage_rows", upsert)

    sector = await preflight_mod._sync_home_coverage_for_preflight(
        user_id=COMPANY_SYSTEM_USER_ID,
        kind="sector_delta",
        ticker=None,
        persist=True,
    )
    assert sector is None
    assert syncs == []
    assert refresh_calls == []

    company_yaml: list[str] = []
    for kind in ("director_daily", "director_weekly", "calendar"):
        summary = await preflight_mod._sync_home_coverage_for_preflight(
            user_id=COMPANY_SYSTEM_USER_ID,
            kind=kind,
            ticker=None,
            persist=True,
        )
        assert summary is None
        assert syncs[-1][0] == COMPANY_SYSTEM_USER_ID
        assert syncs[-1][1] is not None
        company_yaml.append(syncs[-1][1] or "")

    assert company_yaml[0] == company_yaml[1] == company_yaml[2]
    parsed = yaml.safe_load(company_yaml[0])
    assert [c["ticker"] for c in parsed["companies"]] == ["AMD", "NVDA"]
    assert parsed["tiers"]["core"]["count"] == 1
    assert parsed["tiers"]["watch"]["count"] == 1
    assert "2026-06-06T00:00:00+00:00" in company_yaml[0]
    assert "2099" not in company_yaml[0]
    assert "member-note" not in company_yaml[0]
    assert "g-member" not in company_yaml[0]
    assert "workspace" not in company_yaml[0]
    assert rendered_users == []
    assert refresh_calls == []
    assert personal_render.await_count == 0
    assert refresh.await_count == 0
    assert ensure.await_count == 0
    assert groups.await_count == 0
    assert upsert.await_count == 0
    assert listed.await_count == 3

    personal_summary = await preflight_mod._sync_home_coverage_for_preflight(
        user_id=ALICE,
        kind="calendar",
        ticker=None,
        persist=True,
    )
    assert personal_summary == "groups"
    assert rendered_users == [ALICE]
    assert personal_render.await_args.args == (ALICE,)
    assert personal_render.await_args.kwargs == {}
    assert refresh_calls == [(ALICE, False)]
    assert refresh.await_args.args == (ALICE,)
    assert refresh.await_args.kwargs == {"sync_home": False}
    assert syncs[-1] == (ALICE, "personal-yaml")
    assert ensure.await_count == 0
    assert groups.await_count == 0
    assert upsert.await_count == 0
    assert listed.await_count == 3


@pytest.mark.asyncio
async def test_company_board_map_is_market_specific_personal_map_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Company board metadata is per market; a member map stays ticker-keyed."""
    rows = [
        _coverage_row(
            ALICE,
            "BABA",
            tier="core",
            market="US",
            name="Alice US",
            peers=["USPEER"],
            extra={
                "workspace": {"name": "BABA", "status": "active"},
                "note": "alice-private",
                "private_flag": "secret",
                "group_ids": ["g-alice"],
            },
            group_id="g-alice",
        ),
        _coverage_row(ALICE, "NVDA", tier="core", name="Alice NVDA"),
        _coverage_row(BOB, "BABA", tier="watch", market="HK", name="Bob HK"),
        _coverage_row(
            "internal-hk",
            "0700.hk",
            tier="core",
            market=None,
            name="Tencent",
        ),
        _coverage_row(
            "external-1",
            "BABA",
            market="CN",
            name="Ext CN",
            updated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    _install_union(
        monkeypatch,
        rows,
        {
            ALICE: "internal",
            BOB: "admin",
            "internal-hk": "internal",
            "external-1": "external",
        },
    )

    async def _list_user(user_id: str, *, conn: Any = None) -> list[dict[str, Any]]:
        del conn
        uid = str(user_id or "").strip()
        return [row for row in rows if str(row.get("user_id") or "").strip() == uid]

    monkeypatch.setattr(
        coverage_db,
        "list_coverage_rows",
        create_autospec(coverage_db.list_coverage_rows, side_effect=_list_user),
    )
    company_loads = {"n": 0}
    user_loads = {"n": 0}

    async def _company(*, conn: Any = None) -> dict[str, Any]:
        company_loads["n"] += 1
        return await coverage_svc.get_company_coverage(conn=conn)

    async def _user(user_id: str | None, *, conn: Any = None) -> dict[str, Any]:
        user_loads["n"] += 1
        return await coverage_svc.get_user_coverage(user_id, conn=conn)

    monkeypatch.setattr(
        board_mod,
        "get_company_coverage",
        create_autospec(coverage_svc.get_company_coverage, side_effect=_company),
    )
    monkeypatch.setattr(
        board_mod,
        "get_user_coverage",
        create_autospec(coverage_svc.get_user_coverage, side_effect=_user),
    )

    cmap = await board_mod._load_user_coverage_map(COMPANY_SYSTEM_USER_ID)
    assert user_loads["n"] == 0
    assert company_loads["n"] == 1
    assert "BABA" not in cmap
    assert "BABA|CN" not in cmap
    assert cmap["BABA|US"]["name"] == "Alice US"
    assert cmap["BABA|US"]["tier"] == "core"
    assert cmap["BABA|HK"]["name"] == "Bob HK"
    assert cmap["BABA|HK"]["tier"] == "watch"
    assert cmap["NVDA|US"]["name"] == "Alice NVDA"
    assert cmap["0700.HK|HK"]["name"] == "Tencent"
    assert cmap["0700.HK|HK"]["market"] == "HK"
    assert "private_flag" not in cmap["BABA|US"]
    assert "workspace" not in cmap["BABA|US"]
    assert "note" not in cmap["BABA|US"]

    assert board_mod._company_coverage_meta(cmap, "BABA", {"market": "HK"})["name"] == "Bob HK"
    assert board_mod._company_coverage_meta(cmap, "BABA", {"market": "us"})["name"] == "Alice US"
    assert board_mod._company_coverage_meta(cmap, "BABA", {})["name"] == "Alice US"
    assert board_mod._company_coverage_meta(cmap, "BABA", None)["peers"] == ["USPEER"]
    assert board_mod._company_coverage_meta(cmap, "BABA", {"market": "CN"}) == {}
    assert board_mod._company_coverage_meta(
        cmap,
        "BABA",
        {"exchange": "HKEX", "ticker": "BABA"},
    )["name"] == "Alice US"
    assert board_mod._company_coverage_meta(cmap, "0700.HK", {})["name"] == "Tencent"
    assert board_mod._company_coverage_meta(cmap, "0700.HK", {"market": "US"}) == {}

    pmap = await board_mod._load_user_coverage_map(ALICE)
    bmap = await board_mod._load_user_coverage_map(BOB)
    assert company_loads["n"] == 1
    assert user_loads["n"] == 2
    assert sorted(pmap) == ["BABA", "NVDA"]
    assert "BABA|US" not in pmap
    assert pmap["BABA"]["name"] == "Alice US"
    assert pmap["BABA"]["workspace"]["status"] == "active"
    assert pmap["BABA"]["note"] == "alice-private"
    assert pmap["BABA"]["private_flag"] == "secret"
    assert pmap["NVDA"]["name"] == "Alice NVDA"
    assert list(bmap) == ["BABA"]
    assert bmap["BABA"]["name"] == "Bob HK"
    assert "NVDA" not in bmap
    assert "0700.HK" not in bmap


@pytest.mark.asyncio
async def test_company_coverage_empty_document_and_stable_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty included pool uses the normal document; counts follow the union."""
    rows: list[dict[str, Any]] = []
    calls, _identity, _listed = _install_union(
        monkeypatch,
        rows,
        {"external-1": "external", "stable-a": "internal", "stable-b": "admin"},
    )

    empty = await coverage_svc.get_company_coverage()
    _assert_normal_empty(empty)
    assert calls == []

    rows.append(
        _coverage_row(
            "external-1",
            "EXT",
            name="External",
            updated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            extra={"note": "ext-only"},
        )
    )
    excluded = await coverage_svc.get_company_coverage()
    _assert_normal_empty(excluded)
    assert calls == ["external-1"]
    assert "ext-only" not in json.dumps(excluded)

    stamp = datetime(2026, 4, 1, tzinfo=timezone.utc)
    rows.clear()
    rows.extend(
        [
            _coverage_row("stable-b", "BBB", tier="watch", name="Bee", updated_at=stamp),
            _coverage_row(
                "stable-a",
                "AAA",
                tier="core",
                name="Aye",
                updated_at=stamp + timedelta(days=1),
            ),
        ]
    )
    forward = await coverage_svc.get_company_coverage()
    rows.reverse()
    backward = await coverage_svc.get_company_coverage()
    assert [c["ticker"] for c in forward["companies"]] == ["AAA", "BBB"]
    assert forward["companies"] == backward["companies"]
    assert forward["tiers"]["core"]["count"] == backward["tiers"]["core"]["count"] == 1
    assert forward["tiers"]["watch"]["count"] == backward["tiers"]["watch"]["count"] == 1
    assert forward["generated_at"] == backward["generated_at"] == "2026-04-02T00:00:00+00:00"
    assert len(forward["companies"]) == (
        forward["tiers"]["core"]["count"] + forward["tiers"]["watch"]["count"]
    )
