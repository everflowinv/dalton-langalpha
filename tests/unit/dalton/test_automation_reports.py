"""Pure checks for automation report profiles, card bounds, and delivery confirmation."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from src.dalton.database import automation_reports as reports
from src.dalton.database.automation_reports import (
    REPORT_PROFILES,
    aggregate_report_delivery,
    automation_has_validated_report_profile,
    automation_report_content_sha256,
    held_delivery_configured,
    is_automation_report_payload,
    lock_report_execution,
    parse_report_profile,
    prepare_automation_report_content,
    read_automation_report_status,
    submit_automation_report,
)

_LINK = "See [10-K](https://example.com/msft/10k) and https://example.com/Users/foo"


def _card(content: str = _LINK, **extra: object) -> dict:
    card = {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": "MSFT"}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": content},
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "Source"},
                    "behaviors": [
                        {
                            "type": "open_url",
                            "default_url": "https://example.com/src",
                        }
                    ],
                },
            ]
        },
    }
    card.update(extra)
    return card


def _profile(
    profile: str = "msft-tracker",
    report_targets: list[str] | None = None,
    failure_targets: list[str] | None = None,
) -> dict:
    return {
        "ticker": "MSFT",
        "report_profile": {
            "profile": profile,
            "report_targets": report_targets
            if report_targets is not None
            else ["feishu:chat:oc_msftboard"],
            "failure_targets": failure_targets
            if failure_targets is not None
            else ["discord:user:424242424242424242"],
        },
    }


def test_profile_accepts_only_named_profiles() -> None:
    for profile_id in (
        "msft-tracker",
        "meta-tracker",
        "amzn-tracker",
        "googl-tracker",
        "ant-tracker",
        "spcx-tracker",
        "market-digest-am",
        "market-digest-pm",
        "ai-finance-daily",
        "us-weekly-movers",
        "x-accounts-daily",
        "humanoid-robot-weekly",
        "weekly-trend-impact-scan",
        "ct-semi-sentiment",
        "china-consumer-weekly",
        "global-consumer-weekly",
        "daily-filing-alert",
        "tob-industry-weekly",
        "vip-news-alert",
        "mark-weekly-newenergy",
        "tmeet-weekly-minutes",
        "ai-app-weekly",
        "richard-earnings",
        "richard-coverage-weekly",
        "wiki-auto-ingest",
        "podcast-weekly",
    ):
        parsed, err = parse_report_profile(_profile(profile_id))
        assert err is None
        assert parsed is not None
        assert parsed["profile"] == profile_id
        assert parsed["report_targets"] == ["feishu:chat:oc_msftboard"]
        assert parsed["failure_targets"] == ["discord:user:424242424242424242"]
    assert REPORT_PROFILES == {
        "msft-tracker",
        "meta-tracker",
        "amzn-tracker",
        "googl-tracker",
        "ant-tracker",
        "spcx-tracker",
        "market-digest-am",
        "market-digest-pm",
        "ai-finance-daily",
        "us-weekly-movers",
        "x-accounts-daily",
        "humanoid-robot-weekly",
        "weekly-trend-impact-scan",
        "ct-semi-sentiment",
        "china-consumer-weekly",
        "global-consumer-weekly",
        "daily-filing-alert",
        "tob-industry-weekly",
        "vip-news-alert",
        "mark-weekly-newenergy",
        "tmeet-weekly-minutes",
        "ai-app-weekly",
        "richard-earnings",
        "richard-coverage-weekly",
        "wiki-auto-ingest",
        "podcast-weekly",
    }


def test_report_profiles_accepts_dynamic_bullbear_config_without_code_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.dalton import bullbear_tickers

    skill_dir = tmp_path / "dalton-bullbear-tracker"
    (skill_dir / "config").mkdir(parents=True)
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "config" / "NEWCO.json").write_text(
        json.dumps(
            {
                "ticker": "NEWCO",
                "card": {
                    "title_prefix": "📈 NEWCO 跟踪｜",
                    "subtitle": "NewCo · 多空观点、盈利预测修正与催化剂",
                    "tag": "NEWCO",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "references" / "newco-rules.md").write_text("# NEWCO\n", encoding="utf-8")
    # Missing card fields or missing rules -> excluded
    (skill_dir / "config" / "NOCARD.json").write_text(
        json.dumps({"ticker": "NOCARD"}),
        encoding="utf-8",
    )
    (skill_dir / "references" / "nocard-rules.md").write_text("# NOCARD\n", encoding="utf-8")
    (skill_dir / "config" / "PARTIAL.json").write_text(
        json.dumps({"ticker": "PARTIAL", "card": {"tag": "PARTIAL"}}),
        encoding="utf-8",
    )
    (skill_dir / "references" / "partial-rules.md").write_text("# PARTIAL\n", encoding="utf-8")
    (skill_dir / "config" / "NORULES.json").write_text(
        json.dumps(
            {
                "ticker": "NORULES",
                "card": {
                    "title_prefix": "📈 NORULES 跟踪｜",
                    "subtitle": "NoRules · 多空观点、盈利预测修正与催化剂",
                    "tag": "NORULES",
                    "header_template": "blue",
                    "tag_color": "blue",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bullbear_tickers, "BULLBEAR_SKILL_DIR", skill_dir)

    assert "newco-tracker" in REPORT_PROFILES
    assert "nocard-tracker" not in REPORT_PROFILES
    assert "partial-tracker" not in REPORT_PROFILES
    assert "norules-tracker" not in REPORT_PROFILES
    parsed, err = parse_report_profile(_profile("newco-tracker"))
    assert err is None and parsed is not None
    assert parsed["profile"] == "newco-tracker"
    assert parse_report_profile(_profile("nocard-tracker")) == (None, "profile_invalid")
    assert parse_report_profile(_profile("partial-tracker")) == (None, "profile_invalid")
    assert parse_report_profile(_profile("norules-tracker")) == (None, "profile_invalid")


def test_parse_report_profile_logs_and_returns_invalid_when_tracker_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.dalton import bullbear_tickers

    missing_cfg = tmp_path / "does_not_exist" / "tracker_config.py"
    monkeypatch.setattr(bullbear_tickers, "_TRACKER_CONFIG_PATH", missing_cfg)
    with caplog.at_level("WARNING"):
        parsed, err = parse_report_profile(_profile("msft-tracker"))
    assert parsed is None and err == "profile_invalid"
    assert any("missing" in rec.message.lower() for rec in caplog.records)


def test_profile_absent_is_not_invalid_and_malformed_is() -> None:
    assert parse_report_profile({"ticker": "MSFT"}) == (None, None)
    assert parse_report_profile(None) == (None, None)
    assert automation_has_validated_report_profile({"metadata": {"ticker": "MSFT"}}) is False

    parsed, err = parse_report_profile(
        _profile(report_targets=["feishu:chat:oc_msftboard"], failure_targets=[])
    )
    assert err is None and parsed is not None

    for metadata in (
        _profile("other-task"),
        {"report_profile": {"profile": "msft-tracker"}},
        {
            "report_profile": {
                **_profile()["report_profile"],
                "recipient": "discord:user:1",
            }
        },
        _profile(report_targets=[], failure_targets=[]),
        _profile(report_targets=[], failure_targets=["discord:user:424242424242424242"]),
        _profile(report_targets=["discord:user:424242424242424242"]),
        _profile(failure_targets=["feishu:chat:oc_msftboard"]),
        _profile(report_targets=["feishu:chat:oc_msftboard"] * 2),
        _profile(report_targets=[f"feishu:chat:oc_target{i}" for i in range(6)]),
        _profile(report_targets=["feishu:@me"]),
        {"report_profile": []},
    ):
        assert parse_report_profile(metadata)[1] == "profile_invalid"
        assert automation_has_validated_report_profile({"metadata": metadata}) is False

    assert automation_has_validated_report_profile({"metadata": _profile()}) is True


def test_profile_id_rejects_an_array_or_object_without_raising() -> None:
    base = _profile()["report_profile"]
    for bad in ([], {}):
        parsed, err = parse_report_profile(
            {"report_profile": {**base, "profile": bad}}
        )
        assert parsed is None
        assert err == "profile_invalid"


def test_lock_api_takes_the_automation_row_before_the_execution() -> None:
    signature = inspect.signature(lock_report_execution)
    assert list(signature.parameters) == ["cur", "execution_id", "for_update"]
    source = inspect.getsource(lock_report_execution)
    assert source.index("_LOCK_AUTOMATION_FOR_UPDATE_SQL") < source.index(
        "_LOCK_EXECUTION_FOR_UPDATE_SQL"
    )
    assert source.index("_LOCK_AUTOMATION_FOR_SHARE_SQL") < source.index(
        "_LOCK_EXECUTION_FOR_SHARE_SQL"
    )
    module_source = inspect.getsource(reports)
    assert "FOR UPDATE OF" not in module_source
    assert "FOR SHARE OF" not in module_source
    assert "JOIN automations" not in module_source


def test_held_delivery_is_a_non_empty_methods_list() -> None:
    assert held_delivery_configured({"methods": ["feishu"]}) is True
    assert held_delivery_configured({"methods": []}) is False
    assert held_delivery_configured({"methods": [""]}) is False
    assert held_delivery_configured({}) is False
    assert held_delivery_configured(None) is False


def test_cards_keep_source_links_and_reject_attachment_overrides() -> None:
    prepared, err = prepare_automation_report_content(
        profile="market-digest-am",
        cards=[_card()],
        failure_text=None,
    )
    assert err is None and prepared is not None
    content = prepared["cards"][0]["body"]["elements"][0]["content"]
    assert content == _LINK
    assert "https://example.com/Users/foo" in content
    assert prepared["content_kind"] == "cards"
    assert prepared["expected_item_count"] == 1

    reordered = _card()
    reordered["body"] = {
        "elements": list(reversed(reordered["body"]["elements"])),
        **{k: v for k, v in reordered["body"].items() if k != "elements"},
    }
    # Key order of the wrapper does not change the digest; element order does.
    first = automation_report_content_sha256(
        profile="market-digest-am",
        content_kind="cards",
        cards=[_card()],
        failure_text=None,
    )
    swapped_keys = {
        "body": _card()["body"],
        "header": _card()["header"],
        "schema": "2.0",
    }
    second = automation_report_content_sha256(
        profile="market-digest-am",
        content_kind="cards",
        cards=[swapped_keys],
        failure_text=None,
    )
    assert first == second == prepared["content_sha256"]
    changed = automation_report_content_sha256(
        profile="market-digest-am",
        content_kind="cards",
        cards=[_card("different")],
        failure_text=None,
    )
    assert changed != first
    assert prepare_automation_report_content(
        profile="market-digest-am",
        cards=[reordered],
        failure_text=None,
    )[0]["content_sha256"] != first

    refused = [
        _card(download_url="https://example.com/a.bin"),
        _card(path="/tmp/a"),
        _card(images=[]),
        _card(chat_id="oc_msftboard"),
        _card(content="![](./chart.png)"),
        _card(content="see /Users/everflow/file"),
        _card(content="load file:///tmp/a"),
        _card(content="IMG: chart"),
        _card(content="prefix __img__ suffix"),
        {"schema": "1.0", "body": {"elements": [{"tag": "markdown", "content": "x"}]}},
        {"schema": "2.0", "body": {"elements": []}},
        {"schema": "2.0", "body": {}},
    ]
    for card in refused:
        got, code = prepare_automation_report_content(
            profile="msft-tracker",
            cards=[card],
            failure_text=None,
        )
        assert got is None and code == "invalid_card"

    button = _card()
    button["body"]["elements"][1]["behaviors"][0]["default_url"] = "/tmp/a"
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=[button], failure_text=None
        )[1]
        == "invalid_card"
    )


def test_failure_text_is_bounded_and_mutually_exclusive_with_cards() -> None:
    prepared, err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=None,
        failure_text=" Failed. Source https://example.com/status ",
    )
    assert err is None and prepared is not None
    assert prepared["content_kind"] == "failure_text"
    assert prepared["failure_text"] == "Failed. Source https://example.com/status"
    assert prepared["cards"] is None
    assert prepared["expected_item_count"] == 1

    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=[_card()],
            failure_text="boom",
        )[1]
        == "mixed_outcome"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=None, failure_text=None
        )[1]
        == "missing_content"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=None, failure_text="   "
        )[1]
        == "missing_content"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=[], failure_text=None
        )[1]
        == "invalid_card"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=None, failure_text="see /tmp/x"
        )[1]
        == "invalid_failure_text"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=None,
            failure_text="x" * 2001,
        )[1]
        == "content_too_large"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=[_card()] * 21,
            failure_text=None,
        )[1]
        == "invalid_card"
    )


def test_confirmation_requires_every_target_sent() -> None:
    targets = ["feishu:chat:oc_one", "feishu:chat:oc_two"]
    pending = [
        {"target": targets[0], "status": "pending", "error_code": None},
        {"target": targets[1], "status": "pending", "error_code": None},
    ]
    assert aggregate_report_delivery(pending, targets) == ("pending", False)
    unknown = [
        {"target": targets[0], "status": "pending", "error_code": "unknown"},
        {"target": targets[1], "status": "sent", "error_code": None},
    ]
    assert aggregate_report_delivery(unknown, targets) == ("unknown", False)
    partial = [
        {"target": targets[0], "status": "pending", "error_code": "partial"},
        {"target": targets[1], "status": "pending", "error_code": "in_flight"},
    ]
    assert aggregate_report_delivery(partial, targets) == ("unknown", False)
    failed = [
        {"target": targets[0], "status": "failed", "error_code": "http_502"},
        {"target": targets[1], "status": "sent", "error_code": None},
    ]
    assert aggregate_report_delivery(failed, targets) == ("failed", False)
    sent = [
        {"target": targets[0], "status": "sent", "error_code": None},
        {"target": targets[1], "status": "sent", "error_code": ""},
    ]
    assert aggregate_report_delivery(sent, targets) == ("sent", True)
    skipped = [
        {"target": targets[0], "status": "skipped", "error_code": "target_removed"},
        {"target": targets[1], "status": "sent", "error_code": None},
    ]
    assert aggregate_report_delivery(skipped, targets)[1] is False
    assert is_automation_report_payload(
        {"source": "automation_report", "cards": [_card()]}
    )
    assert is_automation_report_payload({"route_key": "data:job", "card": _card()}) is False
    assert is_automation_report_payload({"source": "data_task"}) is False


def test_public_functions_take_server_identity_and_no_recipient() -> None:
    submit_names = list(inspect.signature(submit_automation_report).parameters)
    status_names = list(inspect.signature(read_automation_report_status).parameters)
    identity = [
        "user_id",
        "workspace_id",
        "thread_id",
        "run_id",
        "automation_id",
        "execution_id",
    ]
    assert submit_names[:6] == identity
    assert status_names[:6] == identity
    for name in ("target", "recipient", "owner_user_id", "channel"):
        assert name not in submit_names
        assert name not in status_names
    assert "cards" in submit_names
    assert "failure_text" in submit_names
    assert "text" in submit_names
    assert "cards" not in status_names


def test_cards_keep_a_failure_snapshot_and_only_certain_failures_qualify() -> None:
    prepared, err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=[_card()],
        failure_text=None,
    )
    assert err is None and prepared is not None
    parsed, profile_err = parse_report_profile(_profile())
    assert profile_err is None and parsed is not None
    execution_id = "11111111-1111-1111-1111-111111111111"
    payload = reports._build_payload(
        profile=parsed,
        prepared=prepared,
        report_run_id="digest-1",
        execution_id=execution_id,
        owner_user_id="owner",
        automation_id="22222222-2222-2222-2222-222222222222",
        targets=list(parsed["report_targets"]),
    )
    assert payload["failure_target_snapshot"] == list(parsed["failure_targets"])
    assert payload["target_snapshot"] == list(parsed["report_targets"])
    assert payload["cards"] == prepared["cards"]
    assert payload["content_sha256"] == prepared["content_sha256"]
    assert payload["content_sha256"] == automation_report_content_sha256(
        profile="msft-tracker",
        content_kind="cards",
        cards=prepared["cards"],
        failure_text=None,
    )
    assert "failure_text" not in payload

    text_prepared, text_err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=None,
        failure_text="The digest failed closed.",
    )
    assert text_err is None and text_prepared is not None
    text_payload = reports._build_payload(
        profile=parsed,
        prepared=text_prepared,
        report_run_id="fail-1",
        execution_id=execution_id,
        owner_user_id="owner",
        automation_id="22222222-2222-2222-2222-222222222222",
        targets=list(parsed["failure_targets"]),
    )
    assert "failure_target_snapshot" not in text_payload

    assert reports.failure_target_snapshot_of({}) is None
    assert reports.failure_target_snapshot_of({"failure_target_snapshot": []}) == []
    assert reports.failure_target_snapshot_of(
        {"failure_target_snapshot": [" discord:user:424242424242424242 "]}
    ) == ["discord:user:424242424242424242"]
    assert reports.failure_target_snapshot_of({"failure_target_snapshot": [""]}) is None

    certain = {"status": "failed", "error_code": "http_500", "attempt_count": 3}
    assert reports.card_delivery_is_terminal(certain) is True
    assert reports.card_delivery_is_terminal({**certain, "attempt_count": 1}) is False
    assert reports.card_delivery_is_terminal(
        {"status": "failed", "error_code": "attempts_exhausted", "attempt_count": 1}
    ) is True
    for code in (
        "unknown",
        "in_flight",
        "partial",
        "gateway_restarted",
        "delivery_uncertain",
        "toggle_off",
        "target_removed",
        "not_started",
    ):
        assert reports.card_delivery_is_terminal({**certain, "error_code": code}) is False
    assert reports.card_delivery_is_terminal({**certain, "status": "pending"}) is False
    assert reports.card_delivery_is_terminal({**certain, "status": "skipped"}) is False

    terminal_rows = [
        {"target": "feishu:chat:oc_b", "error_code": "http_500", "attempt_count": 3},
        {"target": "feishu:chat:oc_a", "error_code": "http_502", "attempt_count": 3},
    ]
    notice = reports.build_failure_alert_payload(
        report_payload=payload,
        execution_id=execution_id,
        terminal_rows=terminal_rows,
        failure_targets=payload["failure_target_snapshot"],
    )
    assert notice["source"] == reports.FAILURE_ALERT_SOURCE
    assert notice["content_kind"] == "failure_text"
    assert notice["expected_item_count"] == 1
    assert notice["original_execution_id"] == execution_id
    assert notice["original_content_sha256"] == payload["content_sha256"]
    assert "cards" not in notice
    assert notice["failure_text"].splitlines()[3:] == [
        "- feishu:chat:oc_a: http_502 attempt 3",
        "- feishu:chat:oc_b: http_500 attempt 3",
    ]
    assert notice["content_sha256"] == automation_report_content_sha256(
        profile=notice["profile"],
        content_kind="failure_text",
        cards=None,
        failure_text=notice["failure_text"],
    )
    items, item_err = reports.native_items_from_failure_alert_payload(notice)
    assert item_err is None
    assert items == [{"item_index": 0, "kind": "text", "text": notice["failure_text"]}]
    assert reports.native_items_from_report_payload(notice) == (None, "payload_invalid")
    assert reports.is_failure_alert_payload(notice) is True
    assert reports.is_automation_report_payload(notice) is False
    assert reports.is_failure_alert_payload(payload) is False


@pytest.mark.parametrize("profile_id", ["meta-tracker", "amzn-tracker", "googl-tracker"])
def test_company_tracker_profiles_follow_the_msft_route(profile_id: str) -> None:
    parsed, err = parse_report_profile(_profile(profile_id))
    assert err is None and parsed is not None
    assert parsed["profile"] == profile_id
    assert parsed["report_targets"] == ["feishu:chat:oc_msftboard"]
    assert parsed["failure_targets"] == ["discord:user:424242424242424242"]
    prepared, prep_err = prepare_automation_report_content(
        profile=profile_id,
        cards=[_card()],
        failure_text=None,
    )
    assert prep_err is None and prepared is not None
    assert prepared["content_kind"] == "cards"
    text, text_err = prepare_automation_report_content(
        profile=profile_id,
        cards=None,
        failure_text="Tracker failed. Source https://example.com/status",
    )
    assert text_err is None and text is not None
    assert text["content_kind"] == "failure_text"
    assert text["failure_text"] == "Tracker failed. Source https://example.com/status"


def _canonical_digest(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_text_body_is_exclusive_bounded_and_keeps_card_hashes() -> None:
    markdown = "    # Digest\n\n| Ticker | P/E |\n| --- | --- |\n| AAPL | 28 |\n"
    prepared, err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=None,
        failure_text=None,
        text=markdown,
    )
    assert err is None and prepared is not None
    assert prepared["content_kind"] == "text"
    assert prepared["text"] == markdown
    assert prepared["text"].startswith("    ")
    assert prepared["cards"] is None
    assert prepared["failure_text"] is None
    assert prepared["expected_item_count"] == 1
    assert prepared["content_sha256"] == _canonical_digest(
        {
            "profile": "msft-tracker",
            "content_kind": "text",
            "cards": None,
            "failure_text": None,
            "text": prepared["text"],
        }
    )

    at_limit, limit_err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=None,
        failure_text=None,
        text="x" * 40000,
    )
    assert limit_err is None and at_limit is not None
    assert len(at_limit["text"]) == 40000
    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=None,
            failure_text=None,
            text="x" * 40001,
        )[1]
        == "content_too_large"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=None, failure_text=None, text="   "
        )[1]
        == "missing_content"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=None,
            failure_text=None,
            text=" " + ("x" * 40000),
        )[1]
        == "content_too_large"
    )
    indented, indent_err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=None,
        failure_text=None,
        text="    code",
    )
    assert indent_err is None and indented is not None
    assert indented["text"] == "    code"
    bare, bare_err = prepare_automation_report_content(
        profile="msft-tracker", cards=None, failure_text=None, text="body"
    )
    padded, padded_err = prepare_automation_report_content(
        profile="msft-tracker", cards=None, failure_text=None, text=" body "
    )
    assert bare_err is None and bare is not None
    assert padded_err is None and padded is not None
    assert bare["text"] == "body"
    assert padded["text"] == " body "
    assert bare["content_sha256"] != padded["content_sha256"]
    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=None,
            failure_text=None,
            text="See /tmp/local.md",
        )[1]
        == "invalid_text"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker",
            cards=None,
            failure_text=None,
            text="IMG: secret",
        )[1]
        == "invalid_text"
    )
    linked, link_err = prepare_automation_report_content(
        profile="msft-tracker",
        cards=None,
        failure_text=None,
        text="See https://example.com/status",
    )
    assert link_err is None and linked is not None
    assert linked["text"] == "See https://example.com/status"
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=None, failure_text=None, text=1
        )[1]
        == "invalid_text"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=[_card()], failure_text=None, text="hi"
        )[1]
        == "mixed_outcome"
    )
    assert (
        prepare_automation_report_content(
            profile="msft-tracker", cards=None, failure_text="hi", text="hi"
        )[1]
        == "mixed_outcome"
    )
    empty_cards_text, empty_err = prepare_automation_report_content(
        profile="msft-tracker", cards=[], failure_text=None, text="hi"
    )
    assert empty_err is None and empty_cards_text is not None
    assert empty_cards_text["content_kind"] == "text"
    empty_cards_failure, failure_err = prepare_automation_report_content(
        profile="msft-tracker", cards=[], failure_text="boom", text=None
    )
    assert failure_err is None and empty_cards_failure is not None
    assert empty_cards_failure["content_kind"] == "failure_text"

    stored_cards, card_err = prepare_automation_report_content(
        profile="msft-tracker", cards=[_card()], failure_text=None
    )
    assert card_err is None and stored_cards is not None
    card_digest = _canonical_digest(
        {
            "profile": "msft-tracker",
            "content_kind": "cards",
            "cards": stored_cards["cards"],
            "failure_text": None,
        }
    )
    assert stored_cards["content_sha256"] == card_digest
    assert (
        automation_report_content_sha256(
            profile="msft-tracker",
            content_kind="cards",
            cards=stored_cards["cards"],
            failure_text=None,
            text="ignored",
        )
        == card_digest
    )
    same_words = "same words"
    failure_digest = _canonical_digest(
        {
            "profile": "msft-tracker",
            "content_kind": "failure_text",
            "cards": None,
            "failure_text": same_words,
        }
    )
    assert (
        automation_report_content_sha256(
            profile="msft-tracker",
            content_kind="failure_text",
            cards=None,
            failure_text=same_words,
        )
        == failure_digest
    )
    assert (
        automation_report_content_sha256(
            profile="msft-tracker",
            content_kind="text",
            cards=None,
            failure_text=None,
            text=same_words,
        )
        != failure_digest
    )

    parsed, profile_err = parse_report_profile(_profile())
    assert profile_err is None and parsed is not None
    payload = reports._build_payload(
        profile=parsed,
        prepared=prepared,
        report_run_id="text-1",
        execution_id="11111111-1111-1111-1111-111111111111",
        owner_user_id="owner",
        automation_id="22222222-2222-2222-2222-222222222222",
        targets=["feishu:chat:oc_msftboard"],
    )
    assert payload["text"] == prepared["text"]
    assert payload["content_kind"] == "text"
    assert payload["expected_item_count"] == 1
    assert payload["content_sha256"] == prepared["content_sha256"]
    assert "cards" not in payload
    assert "failure_text" not in payload
    assert payload["failure_target_snapshot"] == list(parsed["failure_targets"])
    items, item_err = reports.native_items_from_report_payload(payload)
    assert item_err is None
    assert items == [{"item_index": 0, "kind": "text", "text": prepared["text"]}]
    assert reports._same_saved_body(
        [{"payload_json": payload}], prepared["content_sha256"]
    )
    assert (
        reports._same_saved_body([{"payload_json": payload}], "0" * 64) is False
    )
    assert reports._content_supplied(None, None, "body") is True
    assert reports._content_supplied(None, None, "   ") is False
    assert reports._content_supplied(None, None, 1) is True


def test_text_targets_parse_and_select_the_text_route() -> None:
    chat = "feishu:chat:oc_msftboard"
    discord_user = "discord:user:424242424242424242"
    channel = "discord:channel:424242424242424242"
    feishu_user = "feishu:user:ou_analyst"
    base = {
        "profile": "msft-tracker",
        "report_targets": [chat],
        "failure_targets": [discord_user],
    }
    parsed, err = parse_report_profile(
        {
            "report_profile": {
                **base,
                "text_targets": [discord_user, channel, feishu_user, chat],
            }
        }
    )
    assert err is None and parsed is not None
    assert parsed["text_targets"] == [discord_user, channel, feishu_user, chat]

    absent, absent_err = parse_report_profile(_profile())
    assert absent_err is None and absent is not None
    assert absent["text_targets"] is None
    failure_only, failure_only_err = parse_report_profile(
        _profile(report_targets=[], failure_targets=[discord_user])
    )
    assert failure_only is None and failure_only_err == "profile_invalid"

    only_text, only_err = parse_report_profile(
        {
            "report_profile": {
                "profile": "msft-tracker",
                "report_targets": [],
                "failure_targets": [],
                "text_targets": [chat],
            }
        }
    )
    assert only_err is None and only_text is not None
    assert only_text["report_targets"] == []
    assert only_text["text_targets"] == [chat]

    for metadata in (
        {"report_profile": {**base, "text_targets": ["discord:guild:424242424242424242"]}},
        {"report_profile": {**base, "text_targets": ["slack:channel:C123"]}},
        {
            "report_profile": {
                **base,
                "text_targets": [f"discord:user:{str(i).zfill(8)}" for i in range(6)],
            }
        },
        {"report_profile": {**base, "text_targets": [discord_user, discord_user]}},
        {
            "report_profile": {
                "profile": "msft-tracker",
                "report_targets": [],
                "failure_targets": [discord_user],
                "text_targets": [],
            }
        },
    ):
        assert parse_report_profile(metadata)[1] == "profile_invalid"

    explicit_empty, empty_err = parse_report_profile(
        {"report_profile": {**base, "text_targets": []}}
    )
    assert empty_err is None and explicit_empty is not None
    assert explicit_empty["text_targets"] == []
    assert reports._outcome_targets(explicit_empty, "text") == []
    assert reports._outcome_targets(absent, "text") == [chat]
    assert reports._outcome_targets(parsed, "text") == [
        discord_user,
        channel,
        feishu_user,
        chat,
    ]
    assert reports._outcome_targets(parsed, "cards") == [chat]
    assert reports._outcome_targets(parsed, "failure_text") == [discord_user]

    text_payload = {
        "source": "automation_report",
        "content_kind": "text",
        "profile": "msft-tracker",
        "target_snapshot": [channel],
    }
    ok, code = reports.saved_report_target_still_configured(
        {"metadata": {"report_profile": {**base, "text_targets": [channel]}}},
        text_payload,
        channel,
    )
    assert ok is True and code == ""
    removed, removed_code = reports.saved_report_target_still_configured(
        {"metadata": {"report_profile": {**base, "text_targets": [channel]}}},
        {**text_payload, "target_snapshot": [chat]},
        chat,
    )
    assert removed is False and removed_code == "target_removed"
    card_payload = {
        "source": "automation_report",
        "content_kind": "cards",
        "profile": "msft-tracker",
        "target_snapshot": [chat],
    }
    cards_ok, cards_code = reports.saved_report_target_still_configured(
        {"metadata": {"report_profile": {**base, "text_targets": [channel]}}},
        card_payload,
        chat,
    )
    assert cards_ok is True and cards_code == ""
    fallback_ok, fallback_code = reports.saved_report_target_still_configured(
        {"metadata": _profile()},
        {**text_payload, "target_snapshot": [chat]},
        chat,
    )
    assert fallback_ok is True and fallback_code == ""


@pytest.mark.asyncio
async def test_submit_automation_report_upgrades_failed_oversized_single_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed oversized 35 KB legacy single-card batch upgrades to split cards without content_conflict."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec
    from uuid import uuid4
    from src.dalton.data_jobs.notification_payloads import (
        feishu_card_serialized_utf8_bytes,
        markdown_to_card_chunks,
        markdown_to_card_elements,
    )

    body_lines = [
        "# 📊 市场晚报 — 2026-11-20",
        "> 2026-11-20 HKT",
    ]
    for sec_idx in range(1, 11):
        body_lines.append(f"## 板块 {sec_idx}：市场核心异动复盘")
        for line_idx in range(1, 31):
            body_lines.append(
                f"- ({sec_idx}.{line_idx}) 美股与中概重点标的表现追踪："
                "成交额放量、机构资金流向与隐含波动率变化细节补充说明分析。"
            )
    big_md = "\n".join(body_lines)
    old_single = markdown_to_card_elements(big_md, "📊 市场晚报 — 2026-11-20")
    assert feishu_card_serialized_utf8_bytes(old_single) > 35_000
    split_cards = markdown_to_card_chunks(big_md, "📊 市场晚报 — 2026-11-20")
    assert len(split_cards) > 1

    old_sha = reports.automation_report_content_sha256(
        profile="market-digest-pm",
        content_kind="cards",
        cards=[old_single],
        failure_text=None,
    )
    old_did = str(uuid4())
    user_id = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    ws_id = str(uuid4())
    th_id = str(uuid4())
    rn_id = str(uuid4())
    old_payload = {
        "source": "automation_report",
        "version": 1,
        "profile": "market-digest-pm",
        "report_run_id": "market-digest:PM:2026-11-20",
        "content_kind": "cards",
        "content_sha256": old_sha,
        "first_execution_id": exec_id,
        "owner_user_id": user_id,
        "automation_id": auto_id,
        "target_snapshot": ["feishu:chat:oc_pmboard1234"],
        "expected_item_count": 1,
        "cards": [old_single],
    }

    stored_rows: list[dict[str, Any]] = [
        {
            "delivery_id": old_did,
            "execution_id": exec_id,
            "automation_id": auto_id,
            "user_id": user_id,
            "channel": "feishu",
            "target": "feishu:chat:oc_pmboard1234",
            "status": "failed",
            "attempt_count": 1,
            "error_code": "card_limit_exceeded",
            "payload_json": old_payload,
        }
    ]
    executed_sql: list[str] = []

    class _FakeCur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []
            self._one: dict[str, Any] | None = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def execute(self, sql: object, params: Any = None) -> None:
            norm = " ".join(str(sql).split())
            executed_sql.append(norm)
            if "FROM automations a" in norm or "automation_executions" in norm and "SELECT" in norm and "FROM dalton_notify_deliveries" not in norm:
                self._one = {
                    "automation_id": auto_id,
                    "user_id": user_id,
                    "automation_execution_id": exec_id,
                    "status": "running",
                    "delivery_config": {},
                    "metadata": {
                        "report_profile": {
                            "profile": "market-digest-pm",
                            "report_targets": ["feishu:chat:oc_pmboard1234"],
                            "failure_targets": ["discord:user:424242424242424242"],
                        }
                    },
                    "name": "market-digest-pm",
                    "thread_id": str(uuid4()),
                }
            elif "DELETE FROM dalton_notify_deliveries" in norm:
                did = str(params[0])
                stored_rows[:] = [r for r in stored_rows if r["delivery_id"] != did]
            elif "INSERT INTO dalton_notify_deliveries" in norm:
                did, eid, aid, uid, ch, tgt, pjson = params
                stored_rows.append(
                    {
                        "delivery_id": str(did),
                        "execution_id": str(eid),
                        "automation_id": str(aid),
                        "user_id": str(uid),
                        "channel": str(ch),
                        "target": str(tgt),
                        "status": "pending",
                        "attempt_count": 0,
                        "error_code": None,
                        "payload_json": json.loads(pjson),
                    }
                )
            elif "FROM dalton_notify_deliveries" in norm and "payload_json->>'report_run_id' <>" in norm:
                self._one = None
            elif "FROM dalton_notify_deliveries" in norm:
                self._rows = list(stored_rows)

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._rows)

    class _FakeConn:
        def cursor(self, **_kw):
            return _FakeCur()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _fake_conn(_conn=None):
        yield _FakeConn()

    monkeypatch.setattr(reports, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        reports,
        "lock_report_execution",
        create_autospec(
            reports.lock_report_execution,
            return_value={
                "automation_id": auto_id,
                "user_id": user_id,
                "workspace_id": ws_id,
                "conversation_thread_id": th_id,
                "conversation_response_id": rn_id,
                "automation_execution_id": exec_id,
                "status": "running",
                "delivery_config": {},
                "metadata": {
                    "report_profile": {
                        "profile": "market-digest-pm",
                        "report_targets": ["feishu:chat:oc_pmboard1234"],
                        "failure_targets": ["discord:user:424242424242424242"],
                    }
                },
                "name": "market-digest-pm",
            },
        ),
    )

    async def _fake_auth(*_a, **_kw):
        return ["feishu:chat:oc_pmboard1234"], None

    monkeypatch.setattr(reports, "_authorize_all_targets", _fake_auth)
    from src.dalton.services import report_delivery as _rd

    monkeypatch.setattr(
        _rd,
        "finalize_missing_report_profile_notice",
        create_autospec(_rd.finalize_missing_report_profile_notice, return_value=None),
    )

    res = await reports.submit_automation_report(
        execution_id=exec_id,
        automation_id=auto_id,
        user_id=user_id,
        workspace_id=ws_id,
        thread_id=th_id,
        run_id=rn_id,
        report_run_id="market-digest:PM:2026-11-20",
        material="yes",
        cards=split_cards,
    )
    assert res["ok"] is True, res
    assert res["created"] is True
    assert len(stored_rows) == 1
    assert stored_rows[0]["delivery_id"] != old_did
    assert len(stored_rows[0]["payload_json"]["cards"]) == len(split_cards)
    assert any("DELETE FROM dalton_notify_deliveries" in sql for sql in executed_sql)

    # A different body still fails with content_conflict even when oversized single card is replaced?
    # Note: after upgrade, stored_rows has compliant split cards so even same-body changes must conflict.
    other_cards = markdown_to_card_chunks("# 另一份报告\n\n## 摘要\n\n不同正文。", "另一份报告")
    conflict = await reports.submit_automation_report(
        execution_id=exec_id,
        automation_id=auto_id,
        user_id=user_id,
        workspace_id=ws_id,
        thread_id=th_id,
        run_id=rn_id,
        report_run_id="market-digest:PM:2026-11-20",
        material="yes",
        cards=other_cards,
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "content_conflict"


def test_prepare_automation_report_content_splits_oversized_single_cards() -> None:
    """Any report profile (X accounts, tracker, weekly movers, humanoid, VIP news) splits oversized single cards."""
    from src.dalton.data_jobs.notification_payloads import (
        FEISHU_CARD_MAX_BYTES,
        feishu_card_serialized_utf8_bytes,
        is_feishu_card_within_envelope,
        markdown_to_card_elements,
    )

    md = "\n\n".join(
        f"## 栏目 {i}\n\n"
        + "\n".join(
            f"- 条目 {i}.{j}：核心数据与跟踪分析说明补充，包含晶圆产能、资本开支与供应链细节描述。"
            for j in range(1, 35)
        )
        for i in range(1, 12)
    )
    oversized_single = markdown_to_card_elements(md, "X 账号日报")
    assert feishu_card_serialized_utf8_bytes(oversized_single) > FEISHU_CARD_MAX_BYTES

    for profile in (
        "x-accounts-daily",
        "msft-tracker",
        "us-weekly-movers",
        "humanoid-robot-weekly",
        "vip-news-alert",
    ):
        prepared, err = reports.prepare_automation_report_content(
            profile=profile,
            cards=[oversized_single],
            failure_text=None,
        )
        assert err is None and prepared is not None
        assert len(prepared["cards"]) >= 2
        assert prepared["expected_item_count"] == len(prepared["cards"])
        assert all(is_feishu_card_within_envelope(c) for c in prepared["cards"])


@pytest.mark.asyncio
async def test_submit_automation_report_upgrade_preserves_old_rows_on_auth_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When target authorization fails during legacy oversized card upgrade, old rows are never deleted."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec
    from uuid import uuid4
    from src.dalton.data_jobs.notification_payloads import (
        markdown_to_card_chunks,
        markdown_to_card_elements,
    )

    big_md = "\n\n".join(
        f"## 板块 {sec_idx}\n\n"
        + "\n".join(
            f"- ({sec_idx}.{line_idx}) 美股与中概重点标的表现追踪：成交额放量、机构资金流向与隐含波动率变化细节补充说明分析。"
            for line_idx in range(1, 31)
        )
        for sec_idx in range(1, 11)
    )
    old_single = markdown_to_card_elements(big_md, "市场晚报")
    split_cards = markdown_to_card_chunks(big_md, "市场晚报")
    old_sha = reports.automation_report_content_sha256(
        profile="market-digest-pm",
        content_kind="cards",
        cards=[old_single],
        failure_text=None,
    )
    old_did = str(uuid4())
    user_id = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    ws_id = str(uuid4())
    th_id = str(uuid4())
    rn_id = str(uuid4())

    stored_rows = [
        {
            "delivery_id": old_did,
            "execution_id": exec_id,
            "automation_id": auto_id,
            "user_id": user_id,
            "channel": "feishu",
            "target": "feishu:chat:oc_revoked",
            "status": "failed",
            "attempt_count": 1,
            "error_code": "card_limit_exceeded",
            "payload_json": {
                "source": "automation_report",
                "version": 1,
                "profile": "market-digest-pm",
                "report_run_id": "market-digest:PM:2026-11-20",
                "content_kind": "cards",
                "content_sha256": old_sha,
                "first_execution_id": exec_id,
                "owner_user_id": user_id,
                "automation_id": auto_id,
                "target_snapshot": ["feishu:chat:oc_revoked"],
                "expected_item_count": 1,
                "cards": [old_single],
            },
        }
    ]
    executed_sql: list[str] = []

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []
            self._one: dict[str, Any] | None = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def execute(self, sql: object, params: Any = None) -> None:
            norm = " ".join(str(sql).split())
            executed_sql.append(norm)
            if "DELETE FROM dalton_notify_deliveries" in norm:
                did = str(params[0])
                stored_rows[:] = [r for r in stored_rows if r["delivery_id"] != did]
            elif "payload_json->>'report_run_id' <>" in norm:
                self._one = None
            elif "FROM dalton_notify_deliveries" in norm:
                self._rows = list(stored_rows)

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kw):
            return _Cur()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _fake_conn(_conn=None):
        yield _Conn()

    monkeypatch.setattr(reports, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        reports,
        "lock_report_execution",
        create_autospec(
            reports.lock_report_execution,
            return_value={
                "automation_id": auto_id,
                "user_id": user_id,
                "workspace_id": ws_id,
                "conversation_thread_id": th_id,
                "conversation_response_id": rn_id,
                "automation_execution_id": exec_id,
                "status": "running",
                "delivery_config": {},
                "metadata": {
                    "report_profile": {
                        "profile": "market-digest-pm",
                        "report_targets": ["feishu:chat:oc_revoked"],
                        "failure_targets": ["discord:user:424242424242424242"],
                    }
                },
                "name": "market-digest-pm",
            },
        ),
    )
    monkeypatch.setattr(
        reports,
        "_authorize_all_targets",
        create_autospec(reports._authorize_all_targets, return_value=([], "target_revoked")),
    )

    res = await reports.submit_automation_report(
        execution_id=exec_id,
        automation_id=auto_id,
        user_id=user_id,
        workspace_id=ws_id,
        thread_id=th_id,
        run_id=rn_id,
        report_run_id="market-digest:PM:2026-11-20",
        material="yes",
        cards=split_cards,
    )
    assert res["ok"] is False
    assert res["code"] == "target_revoked"
    assert len(stored_rows) == 1 and stored_rows[0]["delivery_id"] == old_did
    assert not any("DELETE FROM dalton_notify_deliveries" in s for s in executed_sql)


@pytest.mark.asyncio
async def test_submit_automation_report_upgrades_single_oversized_section_over_30kb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single >30KB section legacy card upgrades without content_conflict."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec
    from uuid import uuid4
    from src.dalton.data_jobs.notification_payloads import (
        FEISHU_CARD_MAX_BYTES,
        feishu_card_serialized_utf8_bytes,
        markdown_to_card_chunks,
        markdown_to_card_elements,
    )

    huge_single_section_md = "## 单章长复盘\n\n" + "\n".join(
        f"- 第 {i} 条：高频量化数据观察及超长单一章节分片验证文本内容补充。"
        for i in range(1, 500)
    )
    old_single = markdown_to_card_elements(huge_single_section_md, "单章晚报")
    assert feishu_card_serialized_utf8_bytes(old_single) > FEISHU_CARD_MAX_BYTES
    split_cards = markdown_to_card_chunks(huge_single_section_md, "单章晚报")
    assert len(split_cards) >= 2

    old_sha = reports.automation_report_content_sha256(
        profile="market-digest-pm",
        content_kind="cards",
        cards=[old_single],
        failure_text=None,
    )
    old_did = str(uuid4())
    user_id = str(uuid4())
    auto_id = str(uuid4())
    exec_id = str(uuid4())
    ws_id = str(uuid4())
    th_id = str(uuid4())
    rn_id = str(uuid4())
    stored_rows = [
        {
            "delivery_id": old_did,
            "execution_id": exec_id,
            "automation_id": auto_id,
            "user_id": user_id,
            "channel": "feishu",
            "target": "feishu:chat:oc_pmboard1234",
            "status": "failed",
            "attempt_count": 1,
            "error_code": "card_limit_exceeded",
            "payload_json": {
                "source": "automation_report",
                "version": 1,
                "profile": "market-digest-pm",
                "report_run_id": "market-digest:PM:2026-11-20",
                "content_kind": "cards",
                "content_sha256": old_sha,
                "first_execution_id": exec_id,
                "owner_user_id": user_id,
                "automation_id": auto_id,
                "target_snapshot": ["feishu:chat:oc_pmboard1234"],
                "expected_item_count": 1,
                "cards": [old_single],
            },
        }
    ]

    class _Cur:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []
            self._one: dict[str, Any] | None = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def execute(self, sql: object, params: Any = None) -> None:
            norm = " ".join(str(sql).split())
            if "DELETE FROM dalton_notify_deliveries" in norm:
                did = str(params[0])
                stored_rows[:] = [r for r in stored_rows if r["delivery_id"] != did]
            elif "INSERT INTO dalton_notify_deliveries" in norm:
                did, eid, aid, uid, ch, tgt, pjson = params
                stored_rows.append(
                    {
                        "delivery_id": str(did),
                        "execution_id": str(eid),
                        "automation_id": str(aid),
                        "user_id": str(uid),
                        "channel": str(ch),
                        "target": str(tgt),
                        "status": "pending",
                        "attempt_count": 0,
                        "error_code": None,
                        "payload_json": json.loads(pjson),
                    }
                )
            elif "payload_json->>'report_run_id' <>" in norm:
                self._one = None
            elif "FROM dalton_notify_deliveries" in norm:
                self._rows = list(stored_rows)

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._rows)

    class _Conn:
        def cursor(self, **_kw):
            return _Cur()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _fake_conn(_conn=None):
        yield _Conn()

    monkeypatch.setattr(reports, "get_db_connection", _fake_conn)
    monkeypatch.setattr(
        reports,
        "lock_report_execution",
        create_autospec(
            reports.lock_report_execution,
            return_value={
                "automation_id": auto_id,
                "user_id": user_id,
                "workspace_id": ws_id,
                "conversation_thread_id": th_id,
                "conversation_response_id": rn_id,
                "automation_execution_id": exec_id,
                "status": "running",
                "delivery_config": {},
                "metadata": {
                    "report_profile": {
                        "profile": "market-digest-pm",
                        "report_targets": ["feishu:chat:oc_pmboard1234"],
                        "failure_targets": ["discord:user:424242424242424242"],
                    }
                },
                "name": "market-digest-pm",
            },
        ),
    )
    monkeypatch.setattr(
        reports,
        "_authorize_all_targets",
        create_autospec(reports._authorize_all_targets, return_value=(["feishu:chat:oc_pmboard1234"], None)),
    )

    res = await reports.submit_automation_report(
        execution_id=exec_id,
        automation_id=auto_id,
        user_id=user_id,
        workspace_id=ws_id,
        thread_id=th_id,
        run_id=rn_id,
        report_run_id="market-digest:PM:2026-11-20",
        material="yes",
        cards=split_cards,
    )
    assert res["ok"] is True, res
    assert res["created"] is True
    assert len(stored_rows) == 1
    assert stored_rows[0]["delivery_id"] != old_did
    assert len(stored_rows[0]["payload_json"]["cards"]) == len(split_cards)


def test_prepare_automation_report_content_refuses_unsplittable_element_and_handles_panel_31002() -> None:
    """prepare_automation_report_content rejects unsplittable oversized cards with card_limit_exceeded
    before gateway submission, and splits ~31002-byte collapsible_panel cards cleanly."""
    from src.dalton.data_jobs.notification_payloads import (
        FEISHU_CARD_MAX_BYTES,
        feishu_card_serialized_utf8_bytes,
        is_feishu_card_within_envelope,
    )

    unsplittable = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "不可拆卡片"}, "template": "indigo"},
        "body": {
            "elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "Z" * (FEISHU_CARD_MAX_BYTES + 500)}}
            ]
        },
    }
    prepared, err = reports.prepare_automation_report_content(
        profile="x-accounts-daily",
        cards=[unsplittable],
        failure_text=None,
    )
    assert prepared is None
    assert err == "card_limit_exceeded"

    content = ("M" * 98 + "\n\n") * 304
    panel_card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "Token 用量周报"}, "template": "indigo"},
        "body": {
            "elements": [
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {
                        "title": {"tag": "markdown", "content": "**其余长尾模型（点击展开）** <font color='grey'>汇总</font>"},
                        "vertical_align": "center",
                        "padding": "4px 8px 4px 8px",
                        "icon": {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "12px 12px"},
                        "icon_position": "right",
                        "icon_expanded_angle": -180,
                    },
                    "border": {"color": "grey", "corner_radius": "6px"},
                    "vertical_spacing": "6px",
                    "padding": "8px 8px 8px 8px",
                    "elements": [{"tag": "markdown", "content": content.strip()}],
                }
            ]
        },
    }
    assert feishu_card_serialized_utf8_bytes(panel_card) > FEISHU_CARD_MAX_BYTES
    panel_prepared, panel_err = reports.prepare_automation_report_content(
        profile="x-accounts-daily",
        cards=[panel_card],
        failure_text=None,
    )
    assert panel_err is None and panel_prepared is not None
    assert len(panel_prepared["cards"]) >= 2
    assert all(is_feishu_card_within_envelope(c) for c in panel_prepared["cards"])

