"""MSFT tracker skill, card builder, and local state. No collector import."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "plugins/dalton/skills/dalton-msft-tracker"
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
    "plugins/dalton/skills/dalton-msft-tracker/scripts/render_card.py",
)
STATE = _load(
    "msft_tracker_state",
    "plugins/dalton/skills/dalton-msft-tracker/scripts/state.py",
)


def _run(root: Path, run_id: str = RUN_ID) -> Path:
    run = root / "research" / "tracker" / run_id
    run.mkdir(parents=True)
    (run / "packet.json").write_text("{}\n", encoding="utf-8")
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
    rules = (SKILL / "references/research-rules.md").read_text(encoding="utf-8")
    assert "references/research-rules.md" in skill
    assert "automation_report(action=\"submit\", report_run_id=RUN_ID,\n                  material=\"yes\", cards=[CARD])" in skill
    assert "automation_report(action=\"status\", report_run_id=RUN_ID)" in skill
    assert 'failure_text="MSFT 跟踪本轮采集失败：原因，下一轮自动重试"' in skill
    assert "delivery_confirmed" in skill
    assert "content_kind` 明确为 `cards`" in skill
    assert "--material no" in skill
    assert "NO_REPLY" in skill
    assert "code` 为 `not_automation_run`" in skill
    assert ".agents/skills/dalton-msft-tracker/scripts/" in skill
    assert "plugins/dalton/skills/" not in skill
    assert "尚未整合" not in skill
    assert "research_pending" in skill
    assert "--content-kind failure_text" in skill
    assert "research/tracker/thesis.md" in skill
    assert "同一内容不在底稿和卡片里各写一遍" not in skill
    assert "同一内容不在底稿和卡片里各写一遍" not in rules
    assert "卡片读者看不到底稿" in rules
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
