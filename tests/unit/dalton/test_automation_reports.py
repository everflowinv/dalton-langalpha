"""Pure checks for automation report profiles, card bounds, and delivery confirmation."""

from __future__ import annotations

import inspect

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


def test_profile_accepts_only_the_three_named_profiles() -> None:
    for profile_id in ("msft-tracker", "market-digest-am", "market-digest-pm"):
        parsed, err = parse_report_profile(_profile(profile_id))
        assert err is None
        assert parsed is not None
        assert parsed["profile"] == profile_id
        assert parsed["report_targets"] == ["feishu:chat:oc_msftboard"]
        assert parsed["failure_targets"] == ["discord:user:424242424242424242"]
    assert REPORT_PROFILES == {
        "msft-tracker",
        "market-digest-am",
        "market-digest-pm",
    }


def test_profile_absent_is_not_invalid_and_malformed_is() -> None:
    assert parse_report_profile({"ticker": "MSFT"}) == (None, None)
    assert parse_report_profile(None) == (None, None)
    assert automation_has_validated_report_profile({"metadata": {"ticker": "MSFT"}}) is False

    parsed, err = parse_report_profile(
        _profile(report_targets=["feishu:chat:oc_msftboard"], failure_targets=[])
    )
    assert err is None and parsed is not None
    parsed, err = parse_report_profile(
        _profile(report_targets=[], failure_targets=["discord:user:424242424242424242"])
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
    assert "cards" not in status_names
