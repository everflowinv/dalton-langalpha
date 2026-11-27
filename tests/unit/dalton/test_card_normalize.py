"""Unit tests for Feishu Schema 2.0 card normalization (mirroring gateway rules)."""

from __future__ import annotations

from copy import deepcopy

from src.dalton.notify.card_normalize import (
    normalize_feishu_card,
    normalize_feishu_cards,
    normalize_feishu_markdown_inline_code,
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


# Shared sample matrix mirrored identically in
# integrations/dalton-channel-gateway/test/feishu.test.ts
_INLINE_CODE_SAMPLES: list[tuple[str, str]] = [
    # Numbers / amounts / percentages / multiples / ranges -> stripped
    (
        "营收 `16,956.0`（`+46.7%` / `-11.9%`），估值 `3.2x`，目标价 `150.0`。",
        "营收 16,956.0（+46.7% / -11.9%），估值 3.2x，目标价 150.0。",
    ),
    (
        "金额 `$150.0`、`-$2.3B`、`$150.0 至 $170.0`、`USD 45.2B`、`452 亿美元`，利差 `+50bps`、`-2.5 ppt`。",
        "金额 `$150.0`、`-$2.3B`、`$150.0 至 $170.0`、USD 45.2B、452 亿美元，利差 +50bps、-2.5 ppt。",
    ),
    (
        "区间 `15%–18%`、`15% 至 18%`、`10-20`、`15%~18%`、`15% ~ 18%`、`15%～18%`、`2.5x~3.5x`、`120亿至130亿美元`。",
        "区间 15%–18%、15% 至 18%、10-20、15%至18%、15%至18%、15%至18%、2.5x至3.5x、120亿至130亿美元。",
    ),
    # Stock tickers -> stripped
    (
        "标的 `ANT`、`NVDA`、`V`、`GOOGL`、`0700.HK`、`601100.SS`、`600519.SH`、`300750.SZ`、`2330.TW`、`005930.KS`、`9984.T`、`ASML.AS`、`BRK.B`。",
        "标的 ANT、NVDA、V、GOOGL、0700.HK、601100.SS、600519.SH、300750.SZ、2330.TW、005930.KS、9984.T、ASML.AS、BRK.B。",
    ),
    # Periods -> stripped
    (
        "期间 `2026Q3`、`2026M11`、`FY26E`、`4Q27E`、`2026H1`、`1H26`、`FY2025`、`2026FY`、`2025Q1–2026Q3`、`2025Q1~2026Q3`、`2026-11-27`。",
        "期间 2026Q3、2026M11、FY26E、4Q27E、2026H1、1H26、FY2025、2026FY、2025Q1–2026Q3、2025Q1至2026Q3、2026-11-27。",
    ),
    # Paths, commands, snake_case & technical identifiers -> preserved
    (
        "路径 `state/ai_app_db/companies/NET/metrics.jsonl`、`src/dalton/notify/card_normalize.py`、`./scripts/run.py`、`SKILL.md`、`README.MD`。",
        "路径 `state/ai_app_db/companies/NET/metrics.jsonl`、`src/dalton/notify/card_normalize.py`、`./scripts/run.py`、`SKILL.md`、`README.MD`。",
    ),
    (
        "命令 `pytest -q`、`git diff --check`、`npx vitest run`、`python3 scripts/cli.py --json`。",
        "命令 `pytest -q`、`git diff --check`、`npx vitest run`、`python3 scripts/cli.py --json`。",
    ),
    (
        "字段 `revenue_yoy_pct`、`report_run_id`、`value_usd_mn`、`NO_REPLY`、`THESIS_STRENGTHENED`、`normalizeFeishuCard`。",
        "字段 `revenue_yoy_pct`、`report_run_id`、`value_usd_mn`、`NO_REPLY`、`THESIS_STRENGTHENED`、`normalizeFeishuCard`。",
    ),
    (
        "技术标识 `v1.2.3`、`127.0.0.1`、`0x1f`、`10-K`、`P1`、`D1`、`f:rev_q8`。",
        "技术标识 `v1.2.3`、`127.0.0.1`、`0x1f`、`10-K`、`P1`、`D1`、`f:rev_q8`。",
    ),
    # Mixed prose + fenced code blocks + multi-backtick + escaped backticks
    (
        "摘要：`ANT` 在 `2026Q3` 营收 `16,956.0`（`+46.7%`），字段 `revenue_yoy_pct`。\n"
        "```python\n"
        "sample = '`16,956.0` `ANT` `+46.7%` `2026Q3`'\n"
        "```\n"
        "~~~text\n"
        "`0700.HK` `FY26E`\n"
        "~~~\n"
        "双反引号 `` `16,956.0` `` 与转义 \\`150.0\\` 保持原样，末尾 `601100.SS` 去反引号。",
        "摘要：ANT 在 2026Q3 营收 16,956.0（+46.7%），字段 `revenue_yoy_pct`。\n"
        "```python\n"
        "sample = '`16,956.0` `ANT` `+46.7%` `2026Q3`'\n"
        "```\n"
        "~~~text\n"
        "`0700.HK` `FY26E`\n"
        "~~~\n"
        "双反引号 `` `16,956.0` `` 与转义 \\`150.0\\` 保持原样，末尾 601100.SS 去反引号。",
    ),
]


def test_strips_decorative_inline_code_backticks_and_preserves_code() -> None:
    for raw, expected in _INLINE_CODE_SAMPLES:
        assert normalize_feishu_markdown_inline_code(raw) == expected


def test_normalize_feishu_card_strips_markdown_inline_code_without_mutating_or_leaking_body() -> None:
    card = {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "AI 应用周报 — 2026-11-27"},
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "**`ANT`** `2026Q3` 收入 `16,956.0`（`+46.7%`），"
                        "字段 `revenue_yoy_pct`，路径 `state/ai_app_db/`。\n"
                        "```bash\n"
                        "echo `16,956.0` `ANT`\n"
                        "```"
                    ),
                },
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": "港股 `0700.HK` 估值 `3.2x`（`FY26E`）",
                                }
                            ],
                        }
                    ],
                },
                {
                    "tag": "text",
                    "content": "A 股 `601100.SS` 同比 `-11.9%`（`4Q27E`），命令 `pytest -q`",
                },
                {
                    "tag": "markdown",
                    "content": "纯代码引用 `report_run_id` 与 `src/app.py` 无需改动",
                },
            ]
        },
    }
    before = deepcopy(card)
    # Default strip_inline_code=False preserves inline code backticks (digest contract)
    # while still converting structural tags like text -> markdown.
    norm_default, paths_default = normalize_feishu_card(card)
    assert card == before
    assert paths_default == ["body.elements[2]"]
    assert "`16,956.0`" in norm_default["body"]["elements"][0]["content"]
    assert norm_default["body"]["elements"][2] == {
        "tag": "markdown",
        "content": "A 股 `601100.SS` 同比 `-11.9%`（`4Q27E`），命令 `pytest -q`",
    }

    norm, paths = normalize_feishu_card(card, strip_inline_code=True)

    assert card == before
    assert paths == [
        "body.elements[0].content",
        "body.elements[1].columns[0].elements[0].content",
        "body.elements[2]",
    ]
    assert all("16,956.0" not in p and "ANT" not in p for p in paths)

    elems = norm["body"]["elements"]
    assert elems[0]["content"] == (
        "**ANT** 2026Q3 收入 16,956.0（+46.7%），"
        "字段 `revenue_yoy_pct`，路径 `state/ai_app_db/`。\n"
        "```bash\n"
        "echo `16,956.0` `ANT`\n"
        "```"
    )
    assert elems[1]["columns"][0]["elements"][0]["content"] == (
        "港股 0700.HK 估值 3.2x（FY26E）"
    )
    assert elems[2] == {
        "tag": "markdown",
        "content": "A 股 601100.SS 同比 -11.9%（4Q27E），命令 `pytest -q`",
    }
    assert elems[3]["content"] == (
        "纯代码引用 `report_run_id` 与 `src/app.py` 无需改动"
    )

    # Idempotent on second pass
    norm_2, paths_2 = normalize_feishu_card(norm, strip_inline_code=True)
    assert norm_2 == norm
    assert paths_2 == []
