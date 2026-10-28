"""Internal members register company-wiki aliases while ingesting, without an admin.

The aliases live in a Dalton-owned store beside the ingest audit; OpenClaw's
config.json is never written, and a name that already points at another
company is refused before anything is filed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from tests.unit.dalton.test_wiki_ingest import _make_test_bridge, rb


def _fake_cli(monkeypatch: pytest.MonkeyPatch, slug: str, doc_id: int) -> Any:
    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = 0
    fake_proc.communicate.return_value = (
        json.dumps(
            {
                "ok": True,
                "action": "added",
                "doc_id": doc_id,
                "category_type": "company",
                "category_name": slug,
                "content_type": "管理层会议纪要",
                "date": "2026-10-28",
                "filename": "2026-10-28-管理层会议纪要.md",
                "filepath": f"wiki/companies/{slug}/2026-10-28-管理层会议纪要.md",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        b"",
    )
    mock = create_autospec(asyncio.create_subprocess_exec, return_value=fake_proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock)
    return mock


_BODIES = {
    1: "东方雨虹三季报交流会：沥青成本上行，零售渠道占比提升，海外收入增长较快。",
    2: "专家访谈：防水材料行业价格战缓和，头部厂商提价落地，工程端回款仍偏慢。",
    3: "管理层交流：民建渠道下沉、经销商数量与库存周转、资产减值的计提口径。",
}


def _req(target: str, *, dry_run: bool, aliases: list[str] | None = None, n: int = 1) -> dict[str, Any]:
    body: dict[str, Any] = {
        "target_type": "company",
        "target": target,
        "content_type": "管理层会议纪要",
        "date": "2026-10-28",
        "content": _BODIES[n],
        "ingested_by": {"user_id": "u-1", "email": "u1@dalton.test"},
        "ingested_via": {"workspace_id": "ws-1", "thread_id": "th-1"},
        "dry_run": dry_run,
    }
    if not dry_run:
        body["request_id"] = f"req-alias-{n}"
    if aliases is not None:
        body["aliases"] = aliases
    return body


def _config_text(bridge: rb.ResearchDataBridge) -> str:
    return (bridge.cfg.ocw_root / "skills" / "company-wiki" / "config.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_aliases_registered_on_write_then_resolve_directly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    (bridge.cfg.ocw_root / "wiki" / "companies" / "YUHONG").mkdir(parents=True)
    cfg_before = _config_text(bridge)
    _fake_cli(monkeypatch, "YUHONG", 501)

    # The preview names what would be registered and writes nothing.
    code, res = await bridge.handle_wiki_ingest(
        _req("YUHONG", dry_run=True, aliases=["002271.SZ", "东方雨虹", "YUHONG"])
    )
    assert code == 200
    assert res["aliases_to_add"] == ["002271.SZ", "东方雨虹"]
    assert not bridge._wiki_alias_file().exists()

    code, res = await bridge.handle_wiki_ingest(
        _req("YUHONG", dry_run=False, aliases=["002271.SZ", "东方雨虹"])
    )
    assert code == 200
    assert res["ok"] is True
    assert res["aliases_added"] == ["002271.SZ", "东方雨虹"]
    assert res["aliases_not_added"] == []

    store = json.loads(bridge._wiki_alias_file().read_text(encoding="utf-8"))
    assert store["company"]["002271.sz"]["slug"] == "YUHONG"
    assert store["company"]["东方雨虹"]["added_by"]["user_id"] == "u-1"
    # OpenClaw's config.json is never written.
    assert _config_text(bridge) == cfg_before

    # Next time the Chinese name and the code resolve straight to the directory.
    for name in ("东方雨虹", "002271.sz"):
        code, res = await bridge.handle_wiki_ingest(_req(name, dry_run=True, n=2))
        assert code == 200, res
        assert res["target"] == "YUHONG"
        assert res["is_existing_slug"] is True

    audit = [
        json.loads(line)
        for line in bridge.cfg.wiki_ingest_audit_file.read_text(encoding="utf-8").splitlines()
    ]
    assert any(e.get("status") == "aliases_added" for e in audit)


@pytest.mark.asyncio
async def test_alias_pointing_elsewhere_is_refused_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    (bridge.cfg.ocw_root / "wiki" / "companies" / "YUHONG").mkdir(parents=True)
    cli = _fake_cli(monkeypatch, "YUHONG", 502)

    # "腾讯" is a config.json alias of TENCENT; "NVDA" is another directory.
    for taken in ("腾讯", "NVDA"):
        code, res = await bridge.handle_wiki_ingest(
            _req("YUHONG", dry_run=False, aliases=["002271.SZ", taken])
        )
        assert code == 409
        assert res["error_code"] == "alias_conflict"
        assert res["alias"] == taken
    cli.assert_not_called()
    assert not bridge._wiki_alias_file().exists()


@pytest.mark.asyncio
async def test_failed_write_registers_no_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    (bridge.cfg.ocw_root / "wiki" / "companies" / "YUHONG").mkdir(parents=True)
    fake_proc = create_autospec(asyncio.subprocess.Process, instance=True)
    fake_proc.returncode = 1
    fake_proc.communicate.return_value = (b"", b"boom")
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, return_value=fake_proc),
    )

    code, res = await bridge.handle_wiki_ingest(
        _req("YUHONG", dry_run=False, aliases=["002271.SZ"])
    )
    assert res["ok"] is False
    assert not bridge._wiki_alias_file().exists()
    code, res = await bridge.handle_wiki_ingest(_req("002271.SZ", dry_run=True, n=3))
    assert res["status"] == "needs_alias"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [["../etc"], ["a/b"], [""], ["x" * 61], ["ok\nnext"], "002271.SZ", [f"A{i}" for i in range(11)]],
)
async def test_malformed_aliases_are_rejected(tmp_path: Path, bad: Any) -> None:
    bridge = _make_test_bridge(tmp_path)
    code, res = await bridge.handle_wiki_ingest(_req("NVDA", dry_run=True, aliases=bad))
    assert code == 400
    assert res["error_code"] in ("invalid_alias", "invalid_aliases")


@pytest.mark.asyncio
async def test_corrupted_store_fails_closed_and_is_not_overwritten(tmp_path: Path) -> None:
    bridge = _make_test_bridge(tmp_path)
    path = bridge._wiki_alias_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert bridge._save_wiki_aliases("company", "YUHONG", ["002271.SZ"], {"user_id": "u-1"}) == []
    assert path.read_text(encoding="utf-8") == "{not json"
    # A registered English alias must not silently become a new directory of
    # the same name while the store cannot be read: ingest stops instead.
    code, res = await bridge.handle_wiki_ingest(_req("ORIENTAL", dry_run=True))
    assert code == 503
    assert res["error_code"] == "alias_store_unreadable"


def test_save_skips_alias_that_became_a_directory(tmp_path: Path) -> None:
    bridge = _make_test_bridge(tmp_path)
    # Another write created company ACME after this request's alias check.
    (bridge.cfg.ocw_root / "wiki" / "companies" / "ACME").mkdir(parents=True)
    written = bridge._save_wiki_aliases(
        "company", "YUHONG", ["ACME", "东方雨虹"], {"user_id": "u-1"}
    )
    assert written == ["东方雨虹"]
    assert "acme" not in bridge._read_wiki_aliases("company")


@pytest.mark.asyncio
async def test_concurrent_writes_cannot_both_claim_one_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    for slug in ("YUHONG", "KEDA"):
        (bridge.cfg.ocw_root / "wiki" / "companies" / slug).mkdir(parents=True)

    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _slow_exec(*args: Any, **kwargs: Any) -> Any:
        slug = args[args.index("--company") + 1]
        calls.append(slug)
        started.set()
        await release.wait()
        proc = create_autospec(asyncio.subprocess.Process, instance=True)
        proc.returncode = 0
        proc.communicate.return_value = (
            json.dumps(
                {
                    "ok": True,
                    "action": "added",
                    "doc_id": len(calls),
                    "filepath": f"wiki/companies/{slug}/2026-10-28-管理层会议纪要.md",
                    "filename": "2026-10-28-管理层会议纪要.md",
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            b"",
        )
        return proc

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        create_autospec(asyncio.create_subprocess_exec, side_effect=_slow_exec),
    )
    first = asyncio.create_task(
        bridge.handle_wiki_ingest(_req("YUHONG", dry_run=False, aliases=["东方"], n=1))
    )
    await started.wait()
    second = asyncio.create_task(
        bridge.handle_wiki_ingest(_req("KEDA", dry_run=False, aliases=["东方"], n=2))
    )
    await asyncio.sleep(0.05)
    release.set()
    (code1, res1), (code2, res2) = await asyncio.gather(first, second)

    assert code1 == 200 and res1["aliases_added"] == ["东方"]
    # The second write re-checks under the lock, is refused before filing
    # anything, and the first mapping stands.
    assert code2 == 409 and res2["error_code"] == "alias_conflict"
    assert calls == ["YUHONG"]
    assert bridge._read_wiki_aliases("company")["东方"] == "YUHONG"


@pytest.mark.asyncio
async def test_ingest_company_wiki_tool_forwards_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.tools import wiki_tools

    monkeypatch.setattr(
        wiki_tools, "get_user_tier", create_autospec(wiki_tools.get_user_tier, return_value="internal")
    )
    monkeypatch.setattr(
        wiki_tools,
        "verify_workspace_access",
        create_autospec(wiki_tools.verify_workspace_access, return_value={"workspace_id": "ws-1"}),
    )
    monkeypatch.setattr(
        wiki_tools,
        "resolve_user_email",
        create_autospec(wiki_tools.resolve_user_email, return_value="u1@dalton.test"),
    )
    mock_call_bridge = create_autospec(
        wiki_tools.call_bridge_wiki_ingest,
        return_value=(200, {"ok": True, "status": "dry_run", "dry_run": True}),
    )
    monkeypatch.setattr(wiki_tools, "call_bridge_wiki_ingest", mock_call_bridge)

    tool_fn = wiki_tools.build_ingest_company_wiki_tool()
    await tool_fn.ainvoke(
        {
            "target_type": "company",
            "target": "YUHONG",
            "content_type": "管理层会议纪要",
            "date": "2026-10-28",
            "content": "东方雨虹交流会纪要。",
            "aliases": ["002271.SZ", "东方雨虹"],
        },
        config={"configurable": {"user_id": "u-1", "workspace_id": "ws-1", "thread_id": "th-1"}},
    )
    assert mock_call_bridge.call_args.args[0]["aliases"] == ["002271.SZ", "东方雨虹"]


def test_auto_ingest_receipt_keeps_alias_outcome() -> None:
    from src.dalton.tools.ingest_tools import _render_ingest_receipt

    out = json.loads(
        _render_ingest_receipt(
            "wiki",
            {
                "ok": True,
                "status": "ingested",
                "document_id": "wiki:7",
                "target": "YUHONG",
                "aliases_added": ["东方雨虹"],
                "aliases_not_added": ["ACME"],
            },
            True,
            True,
        )
    )
    assert out["aliases_added"] == ["东方雨虹"]
    assert out["aliases_not_added"] == ["ACME"]


@pytest.mark.asyncio
async def test_retry_with_aliases_registers_them_on_idempotent_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    (bridge.cfg.ocw_root / "wiki" / "companies" / "YUHONG").mkdir(parents=True)
    _fake_cli(monkeypatch, "YUHONG", 503)

    code, res = await bridge.handle_wiki_ingest(_req("YUHONG", dry_run=False))
    assert code == 200 and res["status"] == "ingested"

    code, res = await bridge.handle_wiki_ingest(
        _req("YUHONG", dry_run=False, aliases=["东方雨虹"])
    )
    assert code == 200
    assert res["idempotent_replay"] is True
    assert res["aliases_added"] == ["东方雨虹"]
    assert bridge._read_wiki_aliases("company")["东方雨虹"] == "YUHONG"


@pytest.mark.asyncio
async def test_write_queued_past_the_wait_bound_answers_busy_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = _make_test_bridge(tmp_path)
    (bridge.cfg.ocw_root / "wiki" / "companies" / "YUHONG").mkdir(parents=True)
    cli = _fake_cli(monkeypatch, "YUHONG", 504)
    monkeypatch.setattr(rb, "WIKI_WRITE_QUEUE_WAIT_S", 0.05)

    await bridge._wiki_write_lock.acquire()  # another write in progress
    try:
        code, res = await bridge.handle_wiki_ingest(
            _req("YUHONG", dry_run=False, aliases=["东方雨虹"])
        )
        # A preview does not queue behind writes.
        pcode, _ = await bridge.handle_wiki_ingest(_req("YUHONG", dry_run=True, n=2))
    finally:
        bridge._wiki_write_lock.release()

    assert code == 503
    assert res["error_code"] == "wiki_write_busy" and res["retryable"] is True
    assert pcode == 200
    cli.assert_not_called()
    assert not bridge._wiki_alias_file().exists()
    assert not bridge._wiki_write_lock.locked()
