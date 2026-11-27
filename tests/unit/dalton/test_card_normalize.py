"""Unit tests for Feishu Schema 2.0 card normalization (mirroring gateway rules)."""

from __future__ import annotations

from copy import deepcopy

from src.dalton.notify.card_normalize import (
    normalize_feishu_card,
    normalize_feishu_cards,
)


def test_normalizes_production_200621_failure_samples_without_mutating_input() -> None:
    # 1. Production 2026-11-27 dalton-ant-tracker: header.text_tag_list[0] missing tag
    ant_card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "ANT 多空跟踪"},
            "template": "blue",
            "text_tag_list": [
                {
                    "text": {"tag": "plain_text", "content": "ANT"},
                    "color": "purple",
                }
            ],
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "多空观察要点"},
            ]
        },
    }
    ant_before = deepcopy(ant_card)
    ant_norm, ant_paths = normalize_feishu_card(ant_card)
    assert ant_card == ant_before
    assert ant_paths == ["header.text_tag_list[0].tag"]
    assert ant_norm["header"]["text_tag_list"] == [
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "ANT"},
            "color": "purple",
        }
    ]
    # Idempotent on second pass
    ant_norm_2, ant_paths_2 = normalize_feishu_card(ant_norm)
    assert ant_norm_2 == ant_norm
    assert ant_paths_2 == []

    # 2. Production 2026-11-26 X accounts daily: body elements using tag="text"
    x_daily_card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "X 账号日报"},
        },
        "body": {
            "elements": [
                {"tag": "text", "content": "今日要点汇总"},
                {
                    "tag": "text",
                    "text": {"tag": "plain_text", "content": "来自 text.content"},
                },
                {"tag": "markdown", "content": "已有正常块"},
            ]
        },
    }
    x_before = deepcopy(x_daily_card)
    x_norm, x_paths = normalize_feishu_card(x_daily_card)
    assert x_daily_card == x_before
    assert x_paths == ["body.elements[0]", "body.elements[1]"]
    assert x_norm["body"]["elements"] == [
        {"tag": "markdown", "content": "今日要点汇总"},
        {"tag": "markdown", "content": "来自 text.content"},
        {"tag": "markdown", "content": "已有正常块"},
    ]


def test_normalizes_strings_tags_nested_containers_and_preserves_unknown() -> None:
    card = {
        "schema": "2.0",
        "header": {
            "title": "主标题字符串",
            "subtitle": "副标题字符串",
            "text_tag_list": [
                {"tag": "text_tag", "text": "标签A", "color": "blue"},
                {"text": "标签B", "color": "green"},
                "标签C",
            ],
        },
        "body": {
            "elements": [
                {"tag": "plain_text", "content": "纯文本块"},
                {"tag": "lark_md", "content": "**旧版 markdown**"},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "保留 div.text"},
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "保留 note"}],
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [{"tag": "text", "content": "列内文本"}],
                        }
                    ],
                },
                {
                    "tag": "collapsible_panel",
                    "elements": [{"tag": "plain_text", "content": "折叠面板文本"}],
                },
                {"tag": "custom_unknown_widget", "payload": {"k": 1}},
            ]
        },
    }

    norm, paths = normalize_feishu_card(card)
    assert paths == [
        "header.title",
        "header.subtitle",
        "header.text_tag_list[0].text",
        "header.text_tag_list[1].tag",
        "header.text_tag_list[1].text",
        "header.text_tag_list[2]",
        "body.elements[0]",
        "body.elements[1]",
        "body.elements[4].columns[0].elements[0]",
        "body.elements[5].elements[0]",
    ]
    assert norm["header"]["title"] == {"tag": "plain_text", "content": "主标题字符串"}
    assert norm["header"]["subtitle"] == {
        "tag": "plain_text",
        "content": "副标题字符串",
    }
    assert norm["header"]["text_tag_list"] == [
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "标签A"},
            "color": "blue",
        },
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "标签B"},
            "color": "green",
        },
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "标签C"},
        },
    ]
    elems = norm["body"]["elements"]
    assert elems[0] == {"tag": "markdown", "content": "纯文本块"}
    assert elems[1] == {"tag": "markdown", "content": "**旧版 markdown**"}
    assert elems[2] == {"tag": "div", "text": {"tag": "lark_md", "content": "保留 div.text"}}
    assert elems[3] == {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "保留 note"}],
    }
    assert elems[4]["columns"][0]["elements"][0] == {
        "tag": "markdown",
        "content": "列内文本",
    }
    assert elems[5]["elements"][0] == {"tag": "markdown", "content": "折叠面板文本"}
    assert elems[6] == {"tag": "custom_unknown_widget", "payload": {"k": 1}}


def test_normalize_feishu_cards_prefixes_paths_per_card() -> None:
    cards = [
        {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "ok"},
                "text_tag_list": [{"text": {"tag": "plain_text", "content": "ANT"}}],
            },
            "body": {"elements": [{"tag": "markdown", "content": "ok"}]},
        },
        {
            "schema": "2.0",
            "body": {"elements": [{"tag": "text", "content": "second"}]},
        },
    ]
    norm_cards, warnings = normalize_feishu_cards(cards)
    assert warnings == [
        "cards[0].header.text_tag_list[0].tag",
        "cards[1].body.elements[0]",
    ]
    assert norm_cards[0]["header"]["text_tag_list"][0]["tag"] == "text_tag"
    assert norm_cards[1]["body"]["elements"][0] == {
        "tag": "markdown",
        "content": "second",
    }
