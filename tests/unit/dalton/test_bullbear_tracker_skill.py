"""MSFT tracker skill, card builder, and local state. No collector import."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "plugins/dalton/skills/dalton-bullbear-tracker"
FIXTURE_MD = Path(__file__).resolve().parent / "fixtures/msft_tracker_card.md"
FIXTURE_JSON = Path(__file__).resolve().parent / "fixtures/msft_tracker_card.json"
RUN_ID = "20261109-2250"
THESIS = "### D1 Azure 增速能不能再超指引、产能跟不跟得上\n"
CARD_REL = f"research/tracker/{RUN_ID}/card.json"
ET = ZoneInfo("America/New_York")


def _load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RENDER = _load(
    "msft_render_card",
    "plugins/dalton/skills/dalton-bullbear-tracker/scripts/render_card.py",
)
STATE = _load(
    "msft_tracker_state",
    "plugins/dalton/skills/dalton-bullbear-tracker/scripts/state.py",
)


def _run(root: Path, run_id: str = RUN_ID) -> Path:
    run = root / "research" / "tracker" / run_id
    run.mkdir(parents=True)
    (run / "packet.json").write_text('{"ticker": "MSFT"}\n', encoding="utf-8")
    (root / "research" / "tracker" / "thesis.md").write_text(THESIS, encoding="utf-8")
    return run


def _review(card: str = CARD_REL, **extra: object) -> dict:
    body = {
        "run_id": RUN_ID,
        "material": "yes",
        "summary": "测试结论",
        "thesis_path": "research/tracker/thesis.md",
        "thesis_sha256": "abc",
        "report_run_id": RUN_ID,
        "card_json": card,
        "send_status": "pending",
        "content_kind": "cards",
    }
    body.update(extra)
    return body


def test_skill_matches_report_tool_and_keeps_targets_out() -> None:
    skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    rules = (
        (SKILL / "references/common-rules.md").read_text(encoding="utf-8")
        + "\n"
        + (SKILL / "references/msft-rules.md").read_text(encoding="utf-8")
    )
    assert "references/common-rules.md" in skill
    assert "references/<ticker小写>-rules.md" in skill
    assert "automation_report(action=\"submit\", report_run_id=RUN_ID,\n                  material=\"yes\", cards=[CARD])" in skill
    assert "automation_report(action=\"status\", report_run_id=RUN_ID)" in skill
    assert 'failure_text="<TICKER> 跟踪本轮采集失败：原因，下一轮自动重试"' in skill
    assert "delivery_confirmed" in skill
    assert "content_kind` 明确为 `cards`" in skill
    assert "--material no" in skill
    assert "NO_REPLY" in skill
    assert "code` 为 `not_automation_run`" in skill
    assert ".agents/skills/dalton-bullbear-tracker/scripts/" in skill
    assert "plugins/dalton/skills/" not in skill
    assert "尚未整合" not in skill
    assert "research_pending" in skill
    assert "--content-kind failure_text" in skill
    assert "research/tracker/thesis.md" in skill
    assert "同一内容不在底稿和卡片里各写一遍" not in skill
    assert "同一内容不在底稿和卡片里各写一遍" not in rules
    assert "卡片读者看不到工作底稿" in rules
    assert "无论本轮是否重要，都用编辑工具改有变化的章节" in rules
    for banned in (
        "oc_91724960011ac1576702a372a01ddc1f",
        "932169512197955636",
        "stock_tracker.py",
        "data_gateway",
        ".openclaw",
        "feishu:chat:",
        "discord:user:",
    ):
        assert banned not in skill
        assert banned not in rules
    for anchor in (
        "907.1",
        "4.72",
        "4.70",
        "44%",
        "47%",
        "4,000 万",
        "65%",
        "2,200",
        "2,400",
        "2,600",
        "25%",
        "E_DESKTOP",
        "E_UPSTREAM_API",
        "finance leases",
        "7.45",
        "0001193125-26-323632",
        "1.0",
        "0.7",
        "2026-10-15",
        "3,000",
    ):
        assert anchor in rules
    render_src = (SKILL / "scripts/render_card.py").read_text(encoding="utf-8")
    state_src = (SKILL / "scripts/state.py").read_text(encoding="utf-8")
    assert "feishu_send_card" not in render_src
    assert "urllib" not in render_src
    assert "msft_collect_current" not in state_src
    assert ".msft-collect.lock" in state_src


def _sent_cards(report_run_id: str = RUN_ID, **extra: object) -> dict:
    body = {
        "ok": True,
        "found": True,
        "delivery_confirmed": True,
        "status": "sent",
        "content_kind": "cards",
        "report_run_id": report_run_id,
    }
    body.update(extra)
    return body


def test_resume_decisions_from_simulated_report_status() -> None:
    pending = {
        "ok": True,
        "created": True,
        "delivery_confirmed": False,
        "status": "pending",
        "content_kind": "cards",
        "report_run_id": RUN_ID,
    }
    saved = STATE.next_step(_review(send_status="pending"), pending, committed=False)
    assert saved["action"] == "retry_same_cards"
    assert saved["commit_material"] is None
    assert saved["seen_unchanged"] is True
    assert saved["keep_card_bytes"] is True
    assert saved["blocks_new_collect"] is True

    sent = _sent_cards()
    confirmed = STATE.next_step(_review(send_status="pending"), sent, committed=False)
    assert confirmed["action"] == "commit_material_yes"
    assert confirmed["commit_material"] == "yes"
    assert confirmed["report_action"] == "status"
    assert confirmed["keep_card_bytes"] is True
    assert confirmed["blocks_new_collect"] is True

    mismatched = STATE.next_step(
        _review(report_run_id="expected"),
        _sent_cards("different"),
        committed=False,
    )
    assert mismatched["action"] == "hold"
    assert mismatched["commit_material"] is None
    assert mismatched["seen_unchanged"] is True
    assert mismatched["report_action"] is None

    missing_id = _sent_cards()
    del missing_id["report_run_id"]
    incomplete = STATE.next_step(_review(report_run_id="expected"), missing_id, committed=False)
    assert incomplete["action"] == "hold"
    assert incomplete["seen_unchanged"] is True
    assert incomplete["commit_material"] is None

    omitted_kind = _sent_cards("expected")
    del omitted_kind["content_kind"]
    wrong_kind = STATE.next_step(
        _review(report_run_id="expected"),
        omitted_kind,
        committed=False,
    )
    assert wrong_kind["action"] == "hold"
    assert wrong_kind["commit_material"] is None
    other_kind = STATE.next_step(
        _review(report_run_id="expected"),
        _sent_cards("expected", content_kind="failure_text"),
        committed=False,
    )
    assert other_kind["action"] == "hold"
    assert other_kind["commit_material"] is None

    failure = {
        "ok": True,
        "found": True,
        "delivery_confirmed": True,
        "status": "sent",
        "content_kind": "failure_text",
        "report_run_id": RUN_ID,
    }
    failed = STATE.next_step(
        _review(card=None, content_kind="failure_text", material="yes"),
        failure,
        committed=False,
    )
    assert failed["action"] == "failure_closed"
    assert failed["commit_material"] is None
    assert failed["seen_unchanged"] is True
    assert failed["blocks_new_collect"] is False

    failed_other = STATE.next_step(
        _review(card=None, content_kind="failure_text", material="yes"),
        {**failure, "report_run_id": "different"},
        committed=False,
    )
    assert failed_other["action"] == "hold"
    assert failed_other["commit_material"] is None
    assert failed_other["seen_unchanged"] is True
    assert failed_other["blocks_new_collect"] is True

    failed_incomplete = dict(failure)
    del failed_incomplete["content_kind"]
    assert STATE.next_step(
        _review(card=None, content_kind="failure_text", material="yes"),
        failed_incomplete,
        committed=False,
    )["action"] == "hold"

    done = STATE.next_step(_review(), sent, committed=True)
    assert done["action"] == "done"
    assert done["commit_material"] is None
    assert done["keep_card_bytes"] is True
    assert done["blocks_new_collect"] is False

    quiet = STATE.next_step(
        _review(material="no", send_status="not_required", content_kind=None),
        None,
        committed=False,
    )
    assert quiet["action"] == "commit_material_no"
    assert quiet["report_action"] is None
    assert quiet["commit_material"] == "no"

    manual = STATE.next_step(
        _review(),
        {
            "ok": False,
            "action": "submit",
            "code": "not_automation_run",
            "delivery_confirmed": False,
            "error": "This turn is not a server-started automation run.",
        },
        committed=False,
    )
    assert manual["action"] == "hold"
    assert manual["commit_material"] is None
    assert manual["seen_unchanged"] is True

    labeled_only_on_status = STATE.next_step(
        _review(),
        _sent_cards(status="not_automation_run", delivery_confirmed=False),
        committed=False,
    )
    assert labeled_only_on_status["action"] == "retry_same_cards"
    assert labeled_only_on_status["commit_material"] is None

    code_blocks_sent = STATE.next_step(
        _review(),
        _sent_cards(code="not_automation_run"),
        committed=False,
    )
    assert code_blocks_sent["action"] == "hold"
    assert code_blocks_sent["commit_material"] is None

    absent = {
        "ok": True,
        "action": "status",
        "found": False,
        "delivery_confirmed": False,
        "report_run_id": RUN_ID,
    }
    missing_card = STATE.next_step(_review(), absent, committed=False, card_present=False)
    assert missing_card["action"] == "continue_research"
    assert missing_card["report_action"] is None
    assert missing_card["commit_material"] is None
    assert missing_card["seen_unchanged"] is True
    assert missing_card["blocks_new_collect"] is True

    pending_without_file = STATE.next_step(_review(), pending, committed=False, card_present=False)
    assert pending_without_file["action"] == "continue_research"
    assert pending_without_file["report_action"] is None

    present = STATE.next_step(_review(), absent, committed=False, card_present=True)
    assert present["action"] == "submit_cards"
    assert present["report_action"] == "submit"

    sent_file_gone = STATE.next_step(_review(send_status="sent"), sent, committed=False, card_present=False)
    assert sent_file_gone["action"] == "commit_material_yes"
    assert sent_file_gone["seen_unchanged"] is False

    local_sent_without_file = STATE.next_step(
        _review(send_status="sent"),
        None,
        committed=False,
        card_present=False,
    )
    assert local_sent_without_file["action"] == "status_only"
    assert local_sent_without_file["report_action"] == "status"
    assert local_sent_without_file["commit_material"] is None


def test_fixture_card_gloss_cap_and_reuse(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    run = _run(root)
    source = run / "card.md"
    source.write_bytes(FIXTURE_MD.read_bytes())
    output = CARD_REL
    first = RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/card.md", output)
    assert first["ok"] is True
    assert first["reused"] is False
    assert first["glossed"] == ["D1"]
    rendered = (root / output).read_text(encoding="utf-8")
    card = json.loads(rendered)
    assert list(card) == ["schema", "config", "header", "body"]
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "indigo"
    assert card["header"]["text_tag_list"][0]["color"] == "indigo"
    assert card["header"]["title"]["content"] == "📈 MSFT 跟踪｜11-09 22:50 ET"
    body = rendered
    assert "https://example.com/D1-skip" in body
    assert body.count("Azure 增速能不能再超指引、产能跟不跟得上") == 1
    assert "之后再写 D1 只保留编号" in body
    assert "<font color='grey'>" in body
    assert '"text_size": "notation"' in body
    assert "__img__" not in body
    assert '"path"' not in body
    assert "1至2" in body
    expected = RENDER.dump_card(json.loads(FIXTURE_JSON.read_text(encoding="utf-8")))
    assert rendered == expected
    assert FIXTURE_JSON.read_text(encoding="utf-8") == expected

    again = RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/card.md", output)
    assert again["reused"] is True
    assert (root / output).read_text(encoding="utf-8") == rendered

    source.write_text(source.read_text(encoding="utf-8") + "\n再写一句测试资料。\n", encoding="utf-8")
    try:
        RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/card.md", output)
    except RENDER.RenderError as exc:
        assert exc.status == "card_conflict"
    else:
        raise AssertionError("different card overwrote the saved bytes")
    assert (root / output).read_text(encoding="utf-8") == rendered

    prepared, glossed = RENDER.prepare_body("测" * 3000, root / "missing.md")
    assert prepared == "测" * 3000
    assert glossed == []
    try:
        RENDER.prepare_body("测" * 3001, root / "missing.md")
    except RENDER.RenderError as exc:
        assert exc.status == "over_cap"
        assert exc.extra["chars"] == 3001
    else:
        raise AssertionError("3001 characters were accepted")

    for name, text in (
        ("image", "IMG:/tmp/secret.png\n测试资料。\n"),
        ("table", "| 列 | 值 |\n| --- | --- |\n| 测试 | 1 |\n"),
    ):
        bad = run / f"{name}.md"
        bad.write_text("## 判断\n" + text, encoding="utf-8")
        target = f"research/tracker/{RUN_ID}/{name}.json"
        try:
            RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/{name}.md", target)
        except RENDER.RenderError as exc:
            assert exc.status == "unsupported_content"
        else:
            raise AssertionError(name)
        assert not (root / target).exists()

    placeholder = RENDER.content_to_elements("IMG:/tmp/a.png\nhello")
    assert placeholder[0]["tag"] == "__img__"
    assert "path" in placeholder[0]


def test_pool_cap_repeat_remove_and_review(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    _run(root)
    moment = datetime(2026, 11, 9, 22, 50, tzinfo=ET)
    pool_path = root / "research/tracker/x_pool.json"
    pool_path.write_text(
        json.dumps(
            {
                "Ada": {
                    "tickers": ["msft", "META"],
                    "added_at": "2026-01-02",
                    "token_supply_estimate": 42,
                    "active": True,
                    "reason": "旧依据",
                }
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "research/tracker/x_authors.json").write_text(
        json.dumps({"ed": {"handle": "@Ed", "name": "Ed Example", "followers": 12, "rest_id": "99"}}) + "\n",
        encoding="utf-8",
    )

    kept = STATE.add_author(root, RUN_ID, "@ADA", "ALL", "已有作者补记", now=moment)
    assert kept["counted"] is False
    assert kept["added_count"] == 0
    stored = json.loads(pool_path.read_text(encoding="utf-8"))
    assert list(stored) == ["ada"]
    assert stored["ada"]["token_supply_estimate"] == 42
    assert stored["ada"]["added_at"] == "2026-01-02"
    assert stored["ada"]["tickers"] == ["msft", "META", "ALL"]
    assert stored["ada"]["active"] is True

    for handle in ("@bob", "@cara", "@Ed"):
        added = STATE.add_author(root, RUN_ID, handle, "MSFT", f"{handle} 有数字的基本面分析", now=moment)
        assert added["counted"] is True
    assert added["added_count"] == 3
    before = pool_path.read_bytes()
    changes_before = (root / "research/tracker" / RUN_ID / "x_pool_changes.json").read_bytes()
    try:
        STATE.add_author(root, RUN_ID, "@dora", "MSFT", "第四位", now=moment)
    except STATE.StateError as exc:
        assert exc.status == "author_cap"
    else:
        raise AssertionError("fourth author was accepted")
    assert pool_path.read_bytes() == before
    assert (root / "research/tracker" / RUN_ID / "x_pool_changes.json").read_bytes() == changes_before

    repeat = STATE.add_author(root, RUN_ID, "Bob", "MSFT,GOOG", "重复加入同一作者", now=moment)
    assert repeat["counted"] is False
    assert repeat["added_count"] == 3
    stored = json.loads(pool_path.read_text(encoding="utf-8"))
    assert stored["bob"]["added_at"] == "2026-11-09"
    assert "GOOG" in stored["bob"]["tickers"]
    assert stored["ed"]["rest_id"] == "99"
    assert stored["ed"]["followers"] == 12
    assert stored["ed"]["profile_status"] == "cached"
    assert "rest_id" not in stored["bob"]
    assert stored["bob"]["profile_status"] == "missing"

    removed = STATE.remove_author(root, RUN_ID, "@Bob", "转成搬运", now=moment)
    assert removed["active"] is False
    stored = json.loads(pool_path.read_text(encoding="utf-8"))
    assert stored["bob"]["active"] is False
    assert stored["bob"]["removed_reason"] == "转成搬运"
    assert stored["bob"]["removed_at"].startswith("2026-11-09")
    assert stored["bob"]["added_at"] == "2026-11-09"
    assert stored["ada"]["token_supply_estimate"] == 42
    changes = json.loads((root / "research/tracker" / RUN_ID / "x_pool_changes.json").read_text(encoding="utf-8"))
    assert changes["added"] == ["bob", "cara", "ed"]
    assert changes["removed"] == ["bob"]

    empty = tmp_path / "empty"
    empty.mkdir()
    try:
        STATE.add_author(empty, RUN_ID, "ada", "MSFT", "没有数据包")
    except STATE.StateError as exc:
        assert exc.status == "packet_missing"
    else:
        raise AssertionError("missing packet wrote a pool")
    assert not (empty / "research").exists()

    try:
        STATE.add_author(root, "../outside", "ada", "MSFT", "坏的运行标识")
    except STATE.StateError as exc:
        assert exc.status == "bad_run_id"
    else:
        raise AssertionError("path run id was accepted")

    saved = STATE.save_review(
        root,
        RUN_ID,
        material="yes",
        summary="测试结论",
        report_run_id=RUN_ID,
        send_status="pending",
        card_json=CARD_REL,
        content_kind="cards",
    )
    review = json.loads((root / saved["path"]).read_text(encoding="utf-8"))
    assert review["thesis_path"] == "research/tracker/thesis.md"
    assert review["thesis_sha256"] == hashlib.sha256(THESIS.encode("utf-8")).hexdigest()
    assert review["send_status"] == "pending"
    assert review["card_json"] == CARD_REL
    assert "SECRET BODY" not in json.dumps(review)
    (root / "research/tracker" / RUN_ID / "card.md").write_text("SECRET BODY\n", encoding="utf-8")
    assert "SECRET BODY" not in (root / saved["path"]).read_text(encoding="utf-8")

    snapshot = (root / saved["path"]).read_bytes()
    try:
        STATE.save_review(
            root,
            RUN_ID,
            material="yes",
            summary="换成另一份报告",
            report_run_id="20261110-0100",
            send_status="sent",
            card_json="research/tracker/20261110-0100/card.json",
            content_kind="cards",
        )
    except STATE.StateError as exc:
        assert exc.status == "review_identity_conflict"
    else:
        raise AssertionError("a second report id replaced the review")
    assert (root / saved["path"]).read_bytes() == snapshot

    try:
        STATE.save_review(
            root,
            RUN_ID,
            material="yes",
            summary="换成另一张卡片",
            report_run_id=RUN_ID,
            send_status="sent",
            card_json="research/tracker/20261110-0100/card.json",
            content_kind="cards",
        )
    except STATE.StateError as exc:
        assert exc.status == "review_identity_conflict"
    else:
        raise AssertionError("a second card path replaced the review")
    assert (root / saved["path"]).read_bytes() == snapshot

    try:
        STATE.save_review(
            root,
            RUN_ID,
            material="yes",
            summary="换成失败文本",
            report_run_id=RUN_ID,
            send_status="sent",
            card_json=CARD_REL,
            content_kind="failure_text",
        )
    except STATE.StateError as exc:
        assert exc.status == "review_identity_conflict"
    else:
        raise AssertionError("content kind replaced the review")
    assert (root / saved["path"]).read_bytes() == snapshot

    refreshed = STATE.save_review(
        root,
        RUN_ID,
        material="yes",
        summary="测试结论",
        report_run_id=RUN_ID,
        send_status="sent",
        content_kind="cards",
    )
    review = json.loads((root / refreshed["path"]).read_text(encoding="utf-8"))
    assert review["run_id"] == RUN_ID
    assert review["report_run_id"] == RUN_ID
    assert review["card_json"] == CARD_REL
    assert review["content_kind"] == "cards"
    assert review["send_status"] == "sent"

    (root / "research/tracker/updates.log").write_text(
        f"2026-11-09 22:50 | {RUN_ID} | material=no | 已提交\n",
        encoding="utf-8",
    )
    other = "20261110-0100"
    _run(root, other)
    STATE.save_review(
        root,
        other,
        material="yes",
        summary="后一轮还没提交",
        report_run_id=other,
        send_status="sent",
        card_json=f"research/tracker/{other}/card.json",
        content_kind="cards",
    )
    open_runs = STATE.unfinished_runs(root)
    assert [item["run_id"] for item in open_runs] == [other]

    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(
            [
                "x-pool",
                "add",
                "--ticker",
                "MSFT",
                "--workspace-dir",
                str(root),
                "--run-id",
                RUN_ID,
                "--handle",
                "@cara",
                "--tickers",
                "MSFT",
                "--reason",
                "同一轮重复加入不计数",
            ]
        )
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload["counted"] is False
    assert payload["added_count"] == 3


def _complete_packet(root: Path, run_id: str, **extra: object) -> None:
    run = root / "research" / "tracker" / run_id
    run.mkdir(parents=True, exist_ok=True)
    body = {"ticker": "MSFT", "run_id": run_id, "collection_complete": True}
    body.update(extra)
    (run / "packet.json").write_text(json.dumps(body), encoding="utf-8")


def test_unreviewed_packet_resume_and_failure_record(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    tracker = root / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "thesis.md").write_text(THESIS, encoding="utf-8")
    older = "20261109-2250"
    newer = "20261110-2250"
    _complete_packet(root, older)
    _complete_packet(root, newer)
    broken = tracker / "20261108-0100"
    broken.mkdir()
    (broken / "packet.json").write_text("{}\n", encoding="utf-8")
    mismatch = tracker / "20261108-0200"
    mismatch.mkdir()
    (mismatch / "packet.json").write_text(
        json.dumps({"ticker": "MSFT", "run_id": "other", "collection_complete": True}),
        encoding="utf-8",
    )
    other_ticker = tracker / "20261108-0300"
    other_ticker.mkdir()
    (other_ticker / "packet.json").write_text(
        json.dumps({"ticker": "AAPL", "run_id": "20261108-0300", "collection_complete": True}),
        encoding="utf-8",
    )
    unfinished = tracker / "20261108-0400"
    unfinished.mkdir()
    (unfinished / "packet.json").write_text(
        json.dumps({"ticker": "MSFT", "run_id": "20261108-0400", "collection_complete": False}),
        encoding="utf-8",
    )
    linked = tracker / "20261108-0500"
    linked.mkdir()
    (linked / "packet.json").symlink_to(tracker / newer / "packet.json")

    open_runs = STATE.unfinished_runs(root)
    assert [item["run_id"] for item in open_runs] == [newer, older]
    assert open_runs[0]["research_pending"] is True
    assert open_runs[0]["packet_json"] == f"research/tracker/{newer}/packet.json"
    assert "material" not in open_runs[0]
    assert "report_run_id" not in open_runs[0]
    assert "content_kind" not in open_runs[0]
    resume = STATE.next_step(open_runs[0], None, committed=False)
    assert resume["action"] == "continue_research"
    assert resume["commit_material"] is None
    assert resume["report_action"] is None
    assert resume["seen_unchanged"] is True
    assert resume["blocks_new_collect"] is True

    (tracker / "updates.log").write_text(
        f"2026-11-10 22:50 | {newer} | material=no | 已提交\n",
        encoding="utf-8",
    )
    assert [item["run_id"] for item in STATE.unfinished_runs(root)] == [older]

    reviewed = STATE.save_review(
        root,
        older,
        material="yes",
        summary="已有判断",
        report_run_id=older,
        send_status="pending",
        card_json=f"research/tracker/{older}/card.json",
        content_kind="cards",
    )
    stored = json.loads((root / reviewed["path"]).read_text(encoding="utf-8"))
    again = STATE.unfinished_runs(root)
    assert [item["run_id"] for item in again] == [older]
    assert again[0]["report_run_id"] == older
    assert again[0]["content_kind"] == "cards"
    assert again[0]["card_json"] == stored["card_json"]
    assert "research_pending" not in again[0]

    failure_id = "msft-collect-20261110"
    saved = STATE.save_review(
        root,
        failure_id,
        material="yes",
        summary="Collection failed",
        report_run_id=failure_id,
        send_status="pending",
        content_kind="failure_text",
    )
    fail_dir = tracker / failure_id
    assert saved["ok"] is True
    assert not (fail_dir / "packet.json").exists()
    assert not (tracker / "seen.json").exists()
    assert failure_id not in (tracker / "updates.log").read_text(encoding="utf-8")
    failure = json.loads((fail_dir / "review.json").read_text(encoding="utf-8"))
    assert failure["run_id"] == failure_id
    assert failure["report_run_id"] == failure_id
    assert failure["content_kind"] == "failure_text"
    assert failure["card_json"] is None
    listed = STATE.unfinished_runs(root)
    assert listed[0]["run_id"] == failure_id
    assert [item["run_id"] for item in listed] == [failure_id, older]

    try:
        STATE.save_review(
            root,
            "msft-collect-20261111",
            material="yes",
            summary="不该没有数据包",
            report_run_id="msft-collect-20261111",
            send_status="pending",
            content_kind="cards",
        )
    except STATE.StateError as exc:
        assert exc.status == "packet_missing"
    else:
        raise AssertionError("card review without a packet was accepted")
    assert not (tracker / "msft-collect-20261111").exists()

    try:
        STATE.save_review(
            root,
            failure_id,
            material="yes",
            summary="失败记录不能改挂卡片",
            report_run_id=failure_id,
            send_status="sent",
            card_json=f"research/tracker/{failure_id}/card.json",
            content_kind="failure_text",
        )
    except STATE.StateError as exc:
        assert exc.status == "packet_missing"
    else:
        raise AssertionError("failure review accepted a card without a packet")
    assert json.loads((fail_dir / "review.json").read_text(encoding="utf-8"))["send_status"] == "pending"

    updated = STATE.save_review(
        root,
        failure_id,
        material="yes",
        summary="Collection failed",
        report_run_id=failure_id,
        send_status="sent",
        content_kind="failure_text",
    )
    assert updated["review"]["send_status"] == "sent"
    assert updated["review"]["report_run_id"] == failure_id
    assert updated["review"]["content_kind"] == "failure_text"
    assert not (fail_dir / "packet.json").exists()

    try:
        STATE.save_review(
            root,
            failure_id,
            material="yes",
            summary="换成另一份失败报告",
            report_run_id="msft-collect-20261111",
            send_status="sent",
            content_kind="failure_text",
        )
    except STATE.StateError as exc:
        assert exc.status == "review_identity_conflict"
    else:
        raise AssertionError("failure review changed report id")
    kept = json.loads((fail_dir / "review.json").read_text(encoding="utf-8"))
    assert kept["report_run_id"] == failure_id
    assert kept["send_status"] == "sent"


def test_msft_card_matches_saved_fixture_bytes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    run = _run(root)
    (run / "card.md").write_bytes(FIXTURE_MD.read_bytes())
    RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/card.md", CARD_REL, "MSFT")
    rendered = (root / CARD_REL).read_text(encoding="utf-8")
    expected = RENDER.dump_card(json.loads(FIXTURE_JSON.read_text(encoding="utf-8")))
    assert rendered == expected
    assert rendered == FIXTURE_JSON.read_text(encoding="utf-8")
    style = RENDER.load_card_style("MSFT")
    assert style == {
        "title_prefix": "📈 MSFT 跟踪｜",
        "subtitle": "Microsoft · 多空观点、盈利预测修正与催化剂",
        "tag": "MSFT",
        "header_template": "indigo",
        "tag_color": "indigo",
    }


@pytest.mark.parametrize(
    ("ticker", "template", "subtitle"),
    [
        ("META", "blue", "Meta Platforms · 多空观点、盈利预测修正与催化剂"),
        ("AMZN", "orange", "Amazon · 多空观点、盈利预测修正与催化剂"),
        ("GOOGL", "green", "Alphabet · 多空观点、盈利预测修正与催化剂"),
    ],
)
def test_card_header_follows_company_config(
    tmp_path: Path, ticker: str, template: str, subtitle: str
) -> None:
    root = tmp_path / ticker
    run = _run(root)
    (run / "card.md").write_text("## 判断\n测试资料。\n", encoding="utf-8")
    output = f"research/tracker/{RUN_ID}/card.json"
    RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/card.md", output, ticker)
    card = json.loads((root / output).read_text(encoding="utf-8"))
    assert card["header"]["template"] == template
    assert card["header"]["text_tag_list"][0]["color"] == template
    assert card["header"]["text_tag_list"][0]["text"]["content"] == ticker
    assert card["header"]["title"]["content"] == f"📈 {ticker} 跟踪｜11-09 22:50 ET"
    assert card["header"]["subtitle"]["content"] == subtitle
    assert card["header"]["title"]["content"] != "📈 MSFT 跟踪｜11-09 22:50 ET"


def test_configs_share_research_fields_and_omit_recipients() -> None:
    loaded: dict[str, dict] = {}
    for ticker in ("MSFT", "META", "AMZN", "GOOGL"):
        path = SKILL / "config" / f"{ticker}.json"
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for banned in ("feishu", "chat", "token", "secret", "discord"):
            assert banned not in lowered, path.name
        loaded[ticker] = json.loads(text)
        assert loaded[ticker]["ticker"] == ticker
        assert set(loaded[ticker]["card"]) == {
            "title_prefix",
            "subtitle",
            "tag",
            "header_template",
            "tag_color",
        }
    shared = set(loaded["MSFT"]) - {"extra_consensus_flags"}
    for ticker in ("META", "AMZN", "GOOGL"):
        assert set(loaded[ticker]) == shared
        assert "extra_consensus_flags" not in loaded[ticker]


def test_rules_keep_company_material_out_of_the_shared_file() -> None:
    common = (SKILL / "references/common-rules.md").read_text(encoding="utf-8")
    for token in ("Microsoft", "MSFT", "Azure", "FY27"):
        assert token not in common
    for name in ("meta-rules.md", "amzn-rules.md", "googl-rules.md"):
        text = (SKILL / "references" / name).read_text(encoding="utf-8")
        assert "Microsoft" not in text
        assert "FY27" not in text
        assert "剔除 OpenAI" not in text
        assert "不含 OpenAI" not in text
    for path in (SKILL / "references").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        for banned in ("/Users/", ".openclaw", "stock_tracker.py", "feishu_send_card"):
            assert banned not in text, path.name


def test_open_runs_and_pool_follow_the_company_ticker(tmp_path: Path) -> None:
    root = tmp_path / "meta"
    tracker = root / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "thesis.md").write_text(THESIS, encoding="utf-8")
    run_id = "20261109-2250"
    (tracker / run_id).mkdir()
    (tracker / run_id / "packet.json").write_text(
        json.dumps({"ticker": "META", "run_id": run_id, "collection_complete": True}),
        encoding="utf-8",
    )
    msft_run = "20261110-2250"
    (tracker / msft_run).mkdir()
    (tracker / msft_run / "packet.json").write_text(
        json.dumps({"ticker": "MSFT", "run_id": msft_run, "collection_complete": True}),
        encoding="utf-8",
    )
    listed = STATE.unfinished_runs(root, "META")
    assert [item["run_id"] for item in listed] == [run_id]
    added = STATE.add_author(
        root, run_id, "@shared", "ALL", "基金经理，连续给出数字", ticker="META"
    )
    assert added["ok"] is True
    try:
        STATE.add_author(
            root, run_id, "@msftonly", "MSFT", "基金经理，连续给出数字", ticker="META"
        )
    except STATE.StateError as exc:
        assert exc.status == "ticker_scope"
    else:
        raise AssertionError("MSFT-only author was accepted into the META pool")
    assert (tracker / ".meta-collect.lock").is_file()
    try:
        STATE.resolve_ticker("NVDA")
    except STATE.StateError as exc:
        assert exc.status == "unsupported_ticker"
    else:
        raise AssertionError("NVDA was accepted")


def test_state_rejects_other_company_and_blank_ticker_and_keeps_legacy_msft(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    run_id = "20261109-2250"
    run = root / "research" / "tracker" / run_id
    run.mkdir(parents=True)
    packet = run / "packet.json"
    packet.write_text(
        json.dumps({"ticker": "MSFT", "run_id": run_id, "collection_complete": True}),
        encoding="utf-8",
    )
    before = packet.read_bytes()

    try:
        STATE.save_review(
            root,
            run_id,
            material="yes",
            summary="不该写入",
            report_run_id=run_id,
            send_status="pending",
            content_kind="cards",
            ticker="META",
        )
    except STATE.StateError as exc:
        assert exc.status == "company_mismatch"
    else:
        raise AssertionError("META save-review wrote an MSFT run")
    assert packet.read_bytes() == before
    assert not (run / "review.json").exists()
    assert not (root / "research" / "tracker" / ".meta-collect.lock").exists()

    try:
        STATE.add_author(root, run_id, "@meta", "META", "基金经理，连续给出数字", ticker="META")
    except STATE.StateError as exc:
        assert exc.status == "company_mismatch"
    else:
        raise AssertionError("META x-pool add wrote an MSFT run")
    try:
        STATE.remove_author(root, run_id, "@meta", "不再覆盖", ticker="META")
    except STATE.StateError as exc:
        assert exc.status == "company_mismatch"
    else:
        raise AssertionError("META x-pool remove wrote an MSFT run")
    assert packet.read_bytes() == before
    assert not (root / "research" / "tracker" / "x_pool.json").exists()
    assert not (root / "research" / "tracker" / ".meta-collect.lock").exists()

    for raw in ("", "   "):
        try:
            STATE.resolve_ticker(raw)
        except STATE.StateError as exc:
            assert exc.status == "unsupported_ticker"
        else:
            raise AssertionError(f"blank ticker {raw!r} was accepted")
        buf = StringIO()
        with redirect_stdout(buf):
            code = STATE.main(["open-runs", "--ticker", raw, "--workspace-dir", str(root)])
        assert code == 2
        assert json.loads(buf.getvalue())["status"] == "unsupported_ticker"

    legacy_id = "msft-collect-20261101"
    legacy = root / "research" / "tracker" / legacy_id
    legacy.mkdir()
    (legacy / "review.json").write_text(
        json.dumps(
            {
                "run_id": legacy_id,
                "material": "yes",
                "summary": "旧失败记录",
                "thesis_path": "research/tracker/thesis.md",
                "thesis_sha256": "abc",
                "report_run_id": legacy_id,
                "card_json": None,
                "send_status": "pending",
                "content_kind": "failure_text",
            }
        ),
        encoding="utf-8",
    )
    msft_runs = [item["run_id"] for item in STATE.unfinished_runs(root, "MSFT")]
    meta_runs = [item["run_id"] for item in STATE.unfinished_runs(root, "META")]
    assert legacy_id in msft_runs
    assert legacy_id not in meta_runs
    assert "ticker" not in json.loads((legacy / "review.json").read_text(encoding="utf-8"))


def test_failure_review_without_packet_rejects_other_company(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    tracker = root / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "thesis.md").write_text(THESIS, encoding="utf-8")
    bodies = {
        "msft-collect-20261111": {
            "run_id": "msft-collect-20261111",
            "ticker": "MSFT",
            "material": "yes",
            "summary": "采集失败",
            "thesis_path": "research/tracker/thesis.md",
            "thesis_sha256": "abc",
            "report_run_id": "msft-collect-20261111",
            "card_json": None,
            "send_status": "pending",
            "content_kind": "failure_text",
        },
        "msft-collect-20261101": {
            "run_id": "msft-collect-20261101",
            "material": "yes",
            "summary": "旧失败记录",
            "thesis_path": "research/tracker/thesis.md",
            "thesis_sha256": "abc",
            "report_run_id": "msft-collect-20261101",
            "card_json": None,
            "send_status": "pending",
            "content_kind": "failure_text",
        },
    }
    before: dict[str, bytes] = {}
    for run_id, body in bodies.items():
        review = tracker / run_id / "review.json"
        review.parent.mkdir()
        review.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before[run_id] = review.read_bytes()
        buf = StringIO()
        with redirect_stdout(buf):
            code = STATE.main(
                [
                    "save-review",
                    "--ticker",
                    "META",
                    "--workspace-dir",
                    str(root),
                    "--run-id",
                    run_id,
                    "--material",
                    "yes",
                    "--summary",
                    "不该改写",
                    "--report-run-id",
                    run_id,
                    "--send-status",
                    "sent",
                    "--content-kind",
                    "failure_text",
                ]
            )
        assert code == 2
        assert json.loads(buf.getvalue())["status"] == "company_mismatch"
        assert review.read_bytes() == before[run_id]
        assert not (review.parent / "packet.json").exists()
    assert not (tracker / ".meta-collect.lock").exists()


def test_corrupt_packet_keeps_review_visible_by_review_ticker(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    tracker = root / "research" / "tracker"

    def write_run(run_id: str, packet: str, review: dict[str, object]) -> None:
        folder = tracker / run_id
        folder.mkdir(parents=True)
        (folder / "packet.json").write_text(packet, encoding="utf-8")
        (folder / "review.json").write_text(json.dumps(review), encoding="utf-8")

    meta_id = "meta-collect-20261102"
    write_run(meta_id, "not json", {"run_id": meta_id, "ticker": "META", "content_kind": "failure_text"})
    legacy_id = "msft-collect-20261103"
    write_run(legacy_id, "[]\n", {"run_id": legacy_id, "content_kind": "failure_text"})
    foreign_id = "20261104-1200"
    write_run(
        foreign_id,
        json.dumps({"ticker": "MSFT", "run_id": foreign_id}),
        {"run_id": foreign_id, "ticker": "META", "content_kind": "failure_text"},
    )

    bad_bytes_id = "meta-collect-20261105"
    write_run(bad_bytes_id, "", {"run_id": bad_bytes_id, "ticker": "META", "content_kind": "failure_text"})
    (tracker / bad_bytes_id / "packet.json").write_bytes(b"\xff\xfe{")

    meta_runs = [item["run_id"] for item in STATE.unfinished_runs(root, "META")]
    msft_runs = [item["run_id"] for item in STATE.unfinished_runs(root, "MSFT")]
    assert bad_bytes_id in meta_runs and bad_bytes_id not in msft_runs
    assert meta_id in meta_runs and meta_id not in msft_runs
    assert legacy_id in msft_runs and legacy_id not in meta_runs
    assert foreign_id in msft_runs and foreign_id not in meta_runs


def test_config_driven_supported_tickers_and_workspace_rules_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collect_mod = _load(
        "bullbear_collect_under_skill_test",
        "plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py",
    )
    for mod in (collect_mod, STATE, RENDER):
        assert not hasattr(mod, "SUPPORTED_TICKERS")

    expected_base = ("AMZN", "ANT", "GOOGL", "META", "MSFT", "SPCX")
    for mod in (collect_mod, STATE, RENDER):
        assert mod.supported_tickers() == expected_base

    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)

    # Valid config + skill reference rules -> supported
    (fake_skill / "config" / "ACME.json").write_text(
        json.dumps(
            {
                "ticker": "ACME",
                "card": {
                    "title_prefix": "📈 ACME 跟踪｜",
                    "subtitle": "Acme · 多空观点、盈利预测修正与催化剂",
                    "tag": "ACME",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    (fake_skill / "references" / "acme-rules.md").write_text("# ACME 规则\n", encoding="utf-8")

    # Missing card -> excluded
    (fake_skill / "config" / "NOCARD.json").write_text(json.dumps({"ticker": "NOCARD"}), encoding="utf-8")
    (fake_skill / "references" / "nocard-rules.md").write_text("# NOCARD\n", encoding="utf-8")

    # Mismatched ticker -> excluded
    (fake_skill / "config" / "BADT.json").write_text(
        json.dumps({"ticker": "OTHER", "card": {"tag": "BADT"}}), encoding="utf-8"
    )
    (fake_skill / "references" / "badt-rules.md").write_text("# BADT\n", encoding="utf-8")

    # Incomplete card (missing tag_color) -> excluded
    (fake_skill / "config" / "PARTIAL.json").write_text(
        json.dumps(
            {
                "ticker": "PARTIAL",
                "card": {
                    "title_prefix": "📈 PARTIAL 跟踪｜",
                    "subtitle": "Partial · 多空观点、盈利预测修正与催化剂",
                    "tag": "PARTIAL",
                    "header_template": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    (fake_skill / "references" / "partial-rules.md").write_text("# PARTIAL\n", encoding="utf-8")

    # Config with card but no reference rules -> excluded without workspace rules, included with workspace rules
    (fake_skill / "config" / "WSCO.json").write_text(
        json.dumps(
            {
                "ticker": "WSCO",
                "card": {
                    "title_prefix": "📈 WSCO 跟踪｜",
                    "subtitle": "Wsco · 多空观点、盈利预测修正与催化剂",
                    "tag": "WSCO",
                    "header_template": "indigo",
                    "tag_color": "indigo",
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)
    monkeypatch.setattr(STATE, "SKILL_DIR", fake_skill)
    monkeypatch.setattr(RENDER, "SKILL_DIR", fake_skill)

    for mod in (collect_mod, STATE, RENDER):
        assert mod.supported_tickers() == ("ACME",)

    ws = tmp_path / "ws"
    (ws / "research" / "tracker").mkdir(parents=True)
    assert collect_mod.resolve_rules_path("ACME", workspace=ws) == (
        fake_skill / "references" / "acme-rules.md",
        "skill",
    )
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["rules-path", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 0
    assert json.loads(buf.getvalue())["rules_path"] == str(fake_skill / "references" / "acme-rules.md")
    assert json.loads(buf.getvalue())["rules_source"] == "skill"

    ws_rules = ws / "research" / "tracker" / "rules.md"
    ws_rules.write_text("# 工作区专属规则\n", encoding="utf-8")
    assert collect_mod.resolve_rules_path("ACME", workspace=ws) == (ws_rules, "workspace")
    # 工作区有 rules.md、但缺 references 文件时技能与服务端都判不支持
    for mod in (collect_mod, STATE, RENDER):
        assert mod.supported_tickers() == ("ACME",)
    with pytest.raises(STATE.StateError) as wsco_state_exc:
        STATE.resolve_ticker("WSCO")
    assert wsco_state_exc.value.status == "unsupported_ticker"
    with pytest.raises(RENDER.RenderError) as wsco_render_exc:
        RENDER.load_card_style("WSCO", workspace=ws)
    assert wsco_render_exc.value.status == "unsupported_ticker"
    with pytest.raises(collect_mod.CollectError) as wsco_collect_exc:
        collect_mod._use_ticker("WSCO")
    assert wsco_collect_exc.value.status == "unsupported_ticker"

    from src.dalton import bullbear_tickers
    from src.dalton.database.automation_reports import REPORT_PROFILES

    monkeypatch.setattr(bullbear_tickers, "BULLBEAR_SKILL_DIR", fake_skill)
    assert bullbear_tickers.supported_bullbear_tickers() == ("ACME",)
    assert "wsco-tracker" not in REPORT_PROFILES

    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["open-runs", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 0
    open_payload = json.loads(buf.getvalue())
    assert open_payload == {
        "ok": True,
        "runs": [],
    }

    # Symlink on rules.md pointing OUTSIDE workspace -> rejected by collect & rules-path
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_rules = outside / "rules.md"
    outside_rules.write_text("# 外部规则\n", encoding="utf-8")
    ws_rules.unlink()
    ws_rules.symlink_to(outside_rules)
    assert collect_mod.resolve_rules_path("ACME", workspace=ws) == (
        fake_skill / "references" / "acme-rules.md",
        "skill",
    )
    with pytest.raises(collect_mod.CollectError) as sym_collect:
        collect_mod.resolve_rules_path("ACME", workspace=ws, strict=True)
    assert sym_collect.value.status == "symlink_escape"
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["rules-path", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 2
    assert json.loads(buf.getvalue())["status"] == "symlink_escape"

    # open-runs lists ALL recoverable runs (latest card.json + older pending run) even if rules.md escapes
    run_id = "20261110-2250"
    run_dir = ws / "research" / "tracker" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "packet.json").write_text(json.dumps({"ticker": "ACME", "run_id": run_id}), encoding="utf-8")
    (run_dir / "card.json").write_text(json.dumps({"schema": "2.0"}), encoding="utf-8")
    (run_dir / "review.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "ticker": "ACME",
                "material": "yes",
                "summary": "s",
                "report_run_id": "rep-1",
                "card_json": f"research/tracker/{run_id}/card.json",
                "send_status": "failed",
                "content_kind": "cards",
            }
        ),
        encoding="utf-8",
    )
    older_id = "20261109-2250"
    older_dir = ws / "research" / "tracker" / older_id
    older_dir.mkdir(parents=True)
    (older_dir / "packet.json").write_text(
        json.dumps({"ticker": "ACME", "run_id": older_id, "collection_complete": True}),
        encoding="utf-8",
    )
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["open-runs", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 0
    card_open = json.loads(buf.getvalue())
    assert [r["run_id"] for r in card_open["runs"]] == [run_id, older_id]
    assert "rules_path" not in card_open
    # Selecting the older run still fails when calling rules-path
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["rules-path", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 2
    assert json.loads(buf.getvalue())["status"] == "symlink_escape"

    (older_dir / "packet.json").unlink()
    older_dir.rmdir()

    # Symlink on rules.md pointing INSIDE workspace -> accepted with rules_source == "workspace"
    shared_dir = ws / "shared"
    shared_dir.mkdir()
    internal_rules = shared_dir / "custom-rules.md"
    internal_rules.write_text("# 工作区内软链规则\n", encoding="utf-8")
    ws_rules.unlink()
    ws_rules.symlink_to(internal_rules)
    assert collect_mod.resolve_rules_path("ACME", workspace=ws, strict=True) == (
        internal_rules.resolve(),
        "workspace",
    )
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["rules-path", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 0
    in_sym = json.loads(buf.getvalue())
    assert in_sym["rules_path"] == str(internal_rules.resolve())
    assert in_sym["rules_source"] == "workspace"

    # Symlink on parent directory research/tracker -> outside -> rejected
    ws_rules.unlink()
    (ws / "research" / "tracker" / run_id / "card.json").unlink()
    (ws / "research" / "tracker" / run_id / "review.json").unlink()
    (ws / "research" / "tracker" / run_id / "packet.json").unlink()
    (ws / "research" / "tracker" / run_id).rmdir()
    (ws / "research" / "tracker").rmdir()
    (ws / "research" / "tracker").symlink_to(outside)
    assert collect_mod.resolve_rules_path("ACME", workspace=ws) == (
        fake_skill / "references" / "acme-rules.md",
        "skill",
    )
    with pytest.raises(collect_mod.CollectError) as dir_sym_collect:
        collect_mod.resolve_rules_path("ACME", workspace=ws, strict=True)
    assert dir_sym_collect.value.status == "symlink_escape"
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["rules-path", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 2
    assert json.loads(buf.getvalue())["status"] == "symlink_escape"

    # Symlink on parent directory research/tracker -> inside workspace -> accepted with rules_source == "workspace"
    alt_tracker = ws / "alt_tracker"
    alt_tracker.mkdir()
    alt_rules = alt_tracker / "rules.md"
    alt_rules.write_text("# 区内目录软链规则\n", encoding="utf-8")
    (ws / "research" / "tracker").unlink()
    (ws / "research" / "tracker").symlink_to(alt_tracker)
    assert collect_mod.resolve_rules_path("ACME", workspace=ws, strict=True) == (
        alt_rules.resolve(),
        "workspace",
    )
    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["rules-path", "--ticker", "ACME", "--workspace-dir", str(ws)])
    assert code == 0
    alt_payload = json.loads(buf.getvalue())
    assert alt_payload["rules_path"] == str(alt_rules.resolve())
    assert alt_payload["rules_source"] == "workspace"
    (ws / "research" / "tracker").unlink()
    (ws / "research" / "tracker").mkdir()

    # Cache picks up newly added config + rules file on mtime change
    late_cfg = fake_skill / "config" / "LATE.json"
    late_text = json.dumps(
        {
            "ticker": "LATE",
            "card": {
                "title_prefix": "📈 LATE 跟踪｜",
                "subtitle": "Late · 多空观点、盈利预测修正与催化剂",
                "tag": "LATE",
                "header_template": "blue",
                "tag_color": "blue",
            },
        }
    )
    late_cfg.write_text(late_text, encoding="utf-8")
    (fake_skill / "references" / "late-rules.md").write_text("# LATE\n", encoding="utf-8")
    assert collect_mod.supported_tickers() == ("ACME", "LATE")

    # Cache invalidates via st_ino / st_ctime_ns even if size and mtime stay identical
    orig_st = late_cfg.stat()
    same_len_bad = " " * len(late_text)
    assert len(same_len_bad.encode("utf-8")) == orig_st.st_size
    late_cfg.write_text(same_len_bad, encoding="utf-8")
    os.utime(late_cfg, ns=(orig_st.st_atime_ns, orig_st.st_mtime_ns))
    assert collect_mod.supported_tickers() == ("ACME",)

    for bad in ("NOCARD", "BADT", "PARTIAL", "WSCO", "NVDA"):
        with pytest.raises(STATE.StateError) as state_exc:
            STATE.resolve_ticker(bad)
        assert state_exc.value.status == "unsupported_ticker"
        with pytest.raises(RENDER.RenderError) as render_exc:
            RENDER.load_card_style(bad, workspace=ws)
        assert render_exc.value.status == "unsupported_ticker"
        with pytest.raises(collect_mod.CollectError) as collect_exc:
            collect_mod._use_ticker(bad)
        assert collect_exc.value.status == "unsupported_ticker"

    skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "common-rules.md" in skill_md
    assert "state.py rules-path" in skill_md
    assert "rules_path" in skill_md
    assert "research/tracker/rules.md" in skill_md
    assert "references/<ticker小写>-rules.md" in skill_md
    assert 'action == "check_status"' in skill_md
    assert "execution_batch_conflict" in skill_md
    for fname in ("common-rules.md", "ant-rules.md", "spcx-rules.md"):
        first_lines = "\n".join((SKILL / "references" / fname).read_text(encoding="utf-8").splitlines()[:5])
        assert "rules_path" in first_lines
        assert "open-runs" not in first_lines
        assert "工作区若存在" not in first_lines
        assert "若工作区存在" not in first_lines


def test_single_config_validation_rejects_padded_ticker_and_non_dict_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collect_mod = _load(
        "bullbear_collect_validation_test",
        "plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py",
    )
    fake_skill = tmp_path / "skill"
    (fake_skill / "config").mkdir(parents=True)
    (fake_skill / "references").mkdir(parents=True)
    (fake_skill / "references" / "msft-rules.md").write_text("# MSFT\n", encoding="utf-8")
    (fake_skill / "config" / "MSFT.json").write_text(
        json.dumps(
            {
                "ticker": " MSFT ",
                "card": {
                    "title_prefix": "📈 MSFT 跟踪｜",
                    "subtitle": "Microsoft",
                    "tag": "MSFT",
                    "header_template": "indigo",
                    "tag_color": "indigo",
                },
            }
        ),
        encoding="utf-8",
    )
    (fake_skill / "references" / "listsrc-rules.md").write_text("# LISTSRC\n", encoding="utf-8")
    (fake_skill / "config" / "LISTSRC.json").write_text(
        json.dumps(
            {
                "ticker": "LISTSRC",
                "sources": ["internal"],
                "card": {
                    "title_prefix": "📈 LISTSRC 跟踪｜",
                    "subtitle": "ListSrc",
                    "tag": "LISTSRC",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(collect_mod, "SKILL_DIR", fake_skill)
    monkeypatch.setattr(STATE, "SKILL_DIR", fake_skill)
    monkeypatch.setattr(RENDER, "SKILL_DIR", fake_skill)

    # 1) "ticker": " MSFT " is rejected consistently across supported_tickers, collect, and render_card
    for mod in (collect_mod, STATE, RENDER):
        assert mod.supported_tickers() == ()
    with pytest.raises(RENDER.RenderError) as render_exc:
        RENDER.load_card_style("MSFT")
    assert render_exc.value.status == "unsupported_ticker"
    with pytest.raises(collect_mod.CollectError) as collect_exc:
        collect_mod._use_ticker("MSFT")
    assert collect_exc.value.status == "unsupported_ticker"

    # 2) Non-dict sources (e.g. list) is rejected consistently as unsupported
    with pytest.raises(STATE.StateError) as state_list_exc:
        STATE.resolve_ticker("LISTSRC")
    assert state_list_exc.value.status == "unsupported_ticker"
    with pytest.raises(RENDER.RenderError) as render_list_exc:
        RENDER.load_card_style("LISTSRC")
    assert render_list_exc.value.status == "unsupported_ticker"
    with pytest.raises(collect_mod.CollectError) as collect_list_exc:
        collect_mod._use_ticker("LISTSRC")
    assert collect_list_exc.value.status == "unsupported_ticker"


@pytest.mark.parametrize(
    ("ticker", "template", "subtitle"),
    [
        ("ANT", "purple", "Anthropic · 多空观点、盈利预测修正与催化剂"),
        ("SPCX", "turquoise", "SpaceX · 多空观点、盈利预测修正与催化剂"),
    ],
)
def test_ant_and_spcx_configs_rules_and_card_structure(
    tmp_path: Path, ticker: str, template: str, subtitle: str
) -> None:
    cfg_path = SKILL / "config" / f"{ticker}.json"
    raw = cfg_path.read_text(encoding="utf-8")
    for banned in ("feishu", "chat", "token", "secret", "discord"):
        assert banned not in raw.lower(), cfg_path.name
    cfg = json.loads(raw)
    assert cfg["ticker"] == ticker
    assert "internal_routes" not in cfg
    assert "sec_forms" not in cfg
    assert isinstance(cfg.get("sources"), dict) and cfg["sources"]
    assert isinstance(cfg["sources"].get("internal_routes"), list) and cfg["sources"]["internal_routes"]
    assert isinstance(cfg["sources"].get("sec_forms"), list) and cfg["sources"]["sec_forms"]
    assert isinstance(cfg.get("research_keywords"), list) and cfg["research_keywords"]
    assert isinstance(cfg.get("x_accounts"), list) and cfg["x_accounts"]
    if ticker == "SPCX":
        assert '"Elon Musk" OR xAI' in cfg["x_queries"][1]
    assert set(cfg["card"]) == {"title_prefix", "subtitle", "tag", "header_template", "tag_color"}

    rules_path = SKILL / "references" / f"{ticker.lower()}-rules.md"
    assert rules_path.is_file()
    rules_text = rules_path.read_text(encoding="utf-8")
    assert f"股票代码直接写 {ticker}" in rules_text
    assert "## 每轮必读的材料" in rules_text
    assert "## 新闻与 X" in rules_text
    assert "## 是否重要" in rules_text
    assert "## 盈利预测怎么拆" in rules_text
    assert "## 卡片正文" in rules_text

    root = tmp_path / ticker
    run = root / "research" / "tracker" / RUN_ID
    run.mkdir(parents=True)
    (run / "packet.json").write_text(json.dumps({"ticker": ticker}) + "\n", encoding="utf-8")
    (root / "research" / "tracker" / "thesis.md").write_text(
        f"### D1 {ticker} 核心分歧\n", encoding="utf-8"
    )
    (run / "card.md").write_text(
        "## 判断\n判断不变。\n\n## 新信息\n- D1 本轮新证据。\n\n## 盈利预测修正\n暂无。\n\n"
        "## 例行披露 (折叠)\n- 暂无。\n\n## 来源\n- [公告](https://example.com/1)\n",
        encoding="utf-8",
    )
    output = f"research/tracker/{RUN_ID}/card.json"
    result = RENDER.render_card(root, RUN_ID, f"research/tracker/{RUN_ID}/card.md", output, ticker)
    assert result["ok"] is True
    assert result["glossed"] == ["D1"]
    card = json.loads((root / output).read_text(encoding="utf-8"))
    assert list(card) == ["schema", "config", "header", "body"]
    assert card["schema"] == "2.0"
    assert card["config"] == {"wide_screen_mode": True, "update_multi": True}
    assert card["header"]["title"]["content"] == f"📈 {ticker} 跟踪｜11-09 22:50 ET"
    assert card["header"]["subtitle"]["content"] == subtitle
    assert card["header"]["template"] == template
    assert card["header"]["text_tag_list"][0]["text"]["content"] == ticker
    assert card["header"]["text_tag_list"][0]["color"] == template
    tags = [el["tag"] for el in card["body"]["elements"]]
    assert "markdown" in tags
    assert "collapsible_panel" in tags
    assert card["body"]["elements"][-1]["text_size"] == "notation"


@pytest.mark.parametrize(
    ("card_id", "pending_id"),
    [
        ("20261108-2250", "20261110-2250"),  # 较新待研究 + 较旧有卡片
        ("20261110-2250", "20261108-2250"),  # 较旧待研究 + 较新有卡片
    ],
)
def test_open_runs_prioritizes_existing_cards_and_commits_with_escaping_rules(
    tmp_path: Path, card_id: str, pending_id: str
) -> None:
    collect_mod = _load(
        "test_skill_collect",
        "plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py",
    )
    ws = tmp_path / "ws"
    tracker = ws / "research" / "tracker"
    tracker.mkdir(parents=True)
    thesis = tracker / "thesis.md"
    thesis.write_text("# Thesis\n", encoding="utf-8")
    outside_rules = tmp_path / "outside-rules.md"
    outside_rules.write_text("# 越界规则\n", encoding="utf-8")
    (tracker / "rules.md").symlink_to(outside_rules)

    snap = ws / "data" / "consensus" / "snap.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"ok": True}), encoding="utf-8")

    card_dir = tracker / card_id
    card_dir.mkdir(parents=True)
    (card_dir / "packet.json").write_text(
        json.dumps(
            {
                "ticker": "ANT",
                "run_id": card_id,
                "since": "2026-11-07",
                "until": "2026-11-10",
                "snapshot_file": "data/consensus/snap.json",
                "ant_ae_state": {"queries": []},
                "internal": {"new": {}, "pending": {}, "withheld": {}, "irrelevant": {}},
                "alphaengine": {"new": {}},
                "sec": {"new": {}},
                "x": {"new": {}, "noise_ids": []},
                "collection_complete": True,
            }
        ),
        encoding="utf-8",
    )
    (card_dir / "card.json").write_text(json.dumps({"schema": "2.0"}), encoding="utf-8")
    card_rel = f"research/tracker/{card_id}/card.json"
    (card_dir / "review.json").write_text(
        json.dumps(
            {
                "run_id": card_id,
                "ticker": "ANT",
                "material": "yes",
                "summary": "resend card",
                "report_run_id": f"rep-{card_id}",
                "card_json": card_rel,
                "send_status": "failed",
                "content_kind": "cards",
            }
        ),
        encoding="utf-8",
    )

    pending_dir = tracker / pending_id
    pending_dir.mkdir(parents=True)
    (pending_dir / "packet.json").write_text(
        json.dumps({"ticker": "ANT", "run_id": pending_id, "collection_complete": True}), encoding="utf-8"
    )

    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["open-runs", "--ticker", "ANT", "--workspace-dir", str(ws)])
    assert code == 0
    runs = json.loads(buf.getvalue())["runs"]
    assert [r["run_id"] for r in runs] == [card_id, pending_id]
    assert runs[0]["card_json"] == card_rel
    assert runs[1]["research_pending"] is True

    # 规则链接越界只影响进入研究的 rules-path，不阻断先补发/本地提交已有卡片
    buf_rules = StringIO()
    with redirect_stdout(buf_rules):
        rules_code = STATE.main(["rules-path", "--ticker", "ANT", "--workspace-dir", str(ws)])
    assert rules_code == 2
    assert json.loads(buf_rules.getvalue())["status"] == "symlink_escape"

    commit_buf = StringIO()
    with redirect_stdout(commit_buf):
        commit_code = collect_mod.main(
            [
                "commit",
                "--ticker",
                "ANT",
                "--workspace-dir",
                str(ws),
                "--run-id",
                card_id,
                "--material",
                "yes",
                "--summary",
                "card resent ok",
            ]
        )
    assert commit_code == 0
    assert json.loads(commit_buf.getvalue())["ok"] is True
    assert [r["run_id"] for r in STATE.unfinished_runs(ws, "ANT")] == [pending_id]


def test_open_runs_marks_check_status_allows_multiple_registered_and_limits_unregistered(
    tmp_path: Path,
) -> None:
    collect_mod = _load(
        "test_skill_collect_r8",
        "plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py",
    )
    ws = tmp_path / "ws"
    tracker = ws / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "thesis.md").write_text("# Thesis\n", encoding="utf-8")
    snap = ws / "data" / "consensus" / "snap.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"ok": True}), encoding="utf-8")

    # 两张本地仍为 pending 的旧卡片 + 一条 material=no + 一条较新待研究
    runs_spec = [
        ("20261105-2250", "no", "not_required", False, None),
        ("20261107-2250", "yes", "pending", True, "cards"),
        ("20261108-2250", "yes", "pending", True, "cards"),
    ]
    for rid, material, send_status, with_card, kind in runs_spec:
        rdir = tracker / rid
        rdir.mkdir(parents=True)
        (rdir / "packet.json").write_text(
            json.dumps(
                {
                    "ticker": "ANT",
                    "run_id": rid,
                    "since": "2026-11-07",
                    "until": "2026-11-10",
                    "snapshot_file": "data/consensus/snap.json",
                    "ant_ae_state": {"queries": []},
                    "internal": {"new": {}, "pending": {}, "withheld": {}, "irrelevant": {}},
                    "alphaengine": {"new": {}},
                    "sec": {"new": {}},
                    "x": {"new": {}, "noise_ids": []},
                    "collection_complete": True,
                }
            ),
            encoding="utf-8",
        )
        card_rel = None
        if with_card:
            card_rel = f"research/tracker/{rid}/card.json"
            (rdir / "card.json").write_text(json.dumps({"schema": "2.0"}), encoding="utf-8")
        (rdir / "review.json").write_text(
            json.dumps(
                {
                    "run_id": rid,
                    "ticker": "ANT",
                    "material": material,
                    "summary": f"s-{rid}",
                    "report_run_id": f"rep-{rid}",
                    "card_json": card_rel,
                    "send_status": send_status,
                    "content_kind": kind,
                }
            ),
            encoding="utf-8",
        )

    newer_pending = "20261110-2250"
    pdir = tracker / newer_pending
    pdir.mkdir(parents=True)
    (pdir / "packet.json").write_text(
        json.dumps({"ticker": "ANT", "run_id": newer_pending, "collection_complete": True}), encoding="utf-8"
    )

    buf = StringIO()
    with redirect_stdout(buf):
        code = STATE.main(["open-runs", "--ticker", "ANT", "--workspace-dir", str(ws)])
    assert code == 0
    payload = json.loads(buf.getvalue())
    runs = payload["runs"]
    assert [(r["run_id"], r["action"], r.get("content_kind")) for r in runs] == [
        ("20261105-2250", "commit_local", None),
        ("20261108-2250", "check_status", "cards"),
        ("20261107-2250", "check_status", "cards"),
        ("20261110-2250", "research", None),
    ]
    assert "first_submit_run" not in payload
    assert all("run_now" not in r for r in runs)

    # 场景 A：服务端查询两张卡片均已送达（found=True, status=sent）→ 两张都可本地提交（不受 1 份限制）
    for card_item in [r for r in runs if r["action"] == "check_status"]:
        decision = STATE.next_step(
            card_item,
            {
                "found": True,
                "ok": True,
                "delivery_confirmed": True,
                "status": "sent",
                "content_kind": "cards",
                "report_run_id": card_item["report_run_id"],
            },
            committed=False,
            card_present=True,
        )
        assert decision["action"] == "commit_material_yes"
        commit_buf = StringIO()
        with redirect_stdout(commit_buf):
            c_code = collect_mod.main(
                [
                    "commit",
                    "--ticker",
                    "ANT",
                    "--workspace-dir",
                    str(ws),
                    "--run-id",
                    card_item["run_id"],
                    "--material",
                    "yes",
                    "--summary",
                    card_item["summary"],
                ]
            )
        assert c_code == 0
    # 两张卡片均已提交进 updates.log，只剩 material=no 和待研究记录
    remaining = [(r["run_id"], r["action"]) for r in STATE.unfinished_runs(ws, "ANT")]
    assert remaining == [("20261105-2250", "commit_local"), ("20261110-2250", "research")]

    # 场景 B：两张卡片均未登记（found=False）+ 一条待研究 → 按新到旧只首次提交最新一张（20261108-2250），不进入研究
    check_runs = [r for r in runs if r["action"] == "check_status"]
    first_submits = [
        r["run_id"]
        for r in check_runs
        if STATE.next_step(
            r,
            {"found": False, "ok": True, "report_run_id": r["report_run_id"]},
            committed=False,
            card_present=True,
        )["action"]
        == "submit_cards"
    ]
    assert first_submits[:1] == ["20261108-2250"]


def test_open_runs_sorts_check_status_by_run_id_with_failure_text_and_mixed_registration(
    tmp_path: Path,
) -> None:
    collect_mod = _load(
        "test_skill_collect_r9",
        "plugins/dalton/skills/dalton-bullbear-tracker/scripts/collect.py",
    )
    ws = tmp_path / "ws"
    tracker = ws / "research" / "tracker"
    tracker.mkdir(parents=True)
    (tracker / "thesis.md").write_text("# Thesis\n", encoding="utf-8")
    snap = ws / "data" / "consensus" / "snap.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(json.dumps({"ok": True}), encoding="utf-8")

    # 场景 1：较新的未登记 failure_text（20261109-2250）+ 较旧的未登记卡片（20261108-2250）
    old_card_id = "20261108-2250"
    old_dir = tracker / old_card_id
    old_dir.mkdir(parents=True)
    (old_dir / "packet.json").write_text(
        json.dumps({"ticker": "ANT", "run_id": old_card_id, "collection_complete": True}), encoding="utf-8"
    )
    (old_dir / "card.json").write_text(json.dumps({"schema": "2.0"}), encoding="utf-8")
    (old_dir / "review.json").write_text(
        json.dumps(
            {
                "run_id": old_card_id,
                "ticker": "ANT",
                "material": "yes",
                "summary": "old card",
                "report_run_id": f"rep-{old_card_id}",
                "card_json": f"research/tracker/{old_card_id}/card.json",
                "send_status": "pending",
                "content_kind": "cards",
            }
        ),
        encoding="utf-8",
    )

    fail_id = "20261109-2250"
    fail_dir = tracker / fail_id
    fail_dir.mkdir(parents=True)
    (fail_dir / "review.json").write_text(
        json.dumps(
            {
                "run_id": fail_id,
                "ticker": "ANT",
                "material": "yes",
                "summary": "collect failed",
                "report_run_id": f"rep-{fail_id}",
                "card_json": None,
                "send_status": "pending",
                "content_kind": "failure_text",
            }
        ),
        encoding="utf-8",
    )

    buf = StringIO()
    with redirect_stdout(buf):
        assert STATE.main(["open-runs", "--ticker", "ANT", "--workspace-dir", str(ws)]) == 0
    runs = json.loads(buf.getvalue())["runs"]
    assert [(r["run_id"], r["action"], r["content_kind"]) for r in runs] == [
        ("20261109-2250", "check_status", "failure_text"),
        ("20261108-2250", "check_status", "cards"),
    ]
    # 未登记时，最新一条 failure_text 优先触发 submit（失败文本），不读 card.json
    d_fail = STATE.next_step(
        runs[0],
        {"found": False, "ok": True, "report_run_id": f"rep-{fail_id}"},
        committed=False,
    )
    assert d_fail["action"] == "retry_failure_text"
    assert d_fail["report_action"] == "submit"

    # 场景 2：最新卡片未登记（20261110-2250）+ 较旧卡片已送达但本地 pending（20261108-2250）
    (fail_dir / "review.json").unlink()
    new_id = "20261110-2250"
    new_dir = tracker / new_id
    new_dir.mkdir(parents=True)
    (new_dir / "packet.json").write_text(
        json.dumps({"ticker": "ANT", "run_id": new_id, "collection_complete": True}), encoding="utf-8"
    )
    (new_dir / "card.json").write_text(json.dumps({"schema": "2.0"}), encoding="utf-8")
    (new_dir / "review.json").write_text(
        json.dumps(
            {
                "run_id": new_id,
                "ticker": "ANT",
                "material": "yes",
                "summary": "new card",
                "report_run_id": f"rep-{new_id}",
                "card_json": f"research/tracker/{new_id}/card.json",
                "send_status": "pending",
                "content_kind": "cards",
            }
        ),
        encoding="utf-8",
    )
    (old_dir / "packet.json").write_text(
        json.dumps(
            {
                "ticker": "ANT",
                "run_id": old_card_id,
                "since": "2026-11-07",
                "until": "2026-11-08",
                "snapshot_file": "data/consensus/snap.json",
                "ant_ae_state": {"queries": []},
                "internal": {"new": {}, "pending": {}, "withheld": {}, "irrelevant": {}},
                "alphaengine": {"new": {}},
                "sec": {"new": {}},
                "x": {"new": {}, "noise_ids": []},
                "collection_complete": True,
            }
        ),
        encoding="utf-8",
    )

    runs2 = STATE.unfinished_runs(ws, "ANT")
    assert [(r["run_id"], r["action"], r["content_kind"]) for r in runs2] == [
        ("20261110-2250", "check_status", "cards"),
        ("20261108-2250", "check_status", "cards"),
    ]
    # 全部查询 status：20261110 未登记(found=False)，20261108 已送达(found=True, sent)
    d_new = STATE.next_step(
        runs2[0],
        {"found": False, "ok": True, "report_run_id": f"rep-{new_id}"},
        committed=False,
        card_present=True,
    )
    d_old = STATE.next_step(
        runs2[1],
        {
            "found": True,
            "ok": True,
            "delivery_confirmed": True,
            "status": "sent",
            "content_kind": "cards",
            "report_run_id": f"rep-{old_card_id}",
        },
        committed=False,
        card_present=True,
    )
    assert d_old["action"] == "commit_material_yes"
    assert d_new["action"] == "submit_cards"
    # 先本地提交已送达的旧卡片，再首次提交新卡片（旧卡片不在待办里残留）
    cbuf = StringIO()
    with redirect_stdout(cbuf):
        assert (
            collect_mod.main(
                [
                    "commit",
                    "--ticker",
                    "ANT",
                    "--workspace-dir",
                    str(ws),
                    "--run-id",
                    old_card_id,
                    "--material",
                    "yes",
                    "--summary",
                    "old card sent",
                ]
            )
            == 0
        )
    assert [r["run_id"] for r in STATE.unfinished_runs(ws, "ANT")] == [new_id]
