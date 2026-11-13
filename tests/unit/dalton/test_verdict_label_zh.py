"""Readers see the verdict in Chinese; the internal enum never reaches a message."""
import json

from src.dalton.notify.dispatch import (
    VERDICT_WORD_LABEL_ZH,
    build_feishu_notify_card,
    map_verdict_to_header_template,
    verdict_label_zh,
)


def test_every_schema_verdict_has_a_label():
    schema = json.load(open("src/dalton/knowledge/schemas/common.schema.json", encoding="utf-8"))
    words = schema["$defs"]["verdictWord"]["enum"]
    assert set(words) == set(VERDICT_WORD_LABEL_ZH)


def test_label_falls_back_to_given_text():
    assert verdict_label_zh("no_change") == "论点不变"
    assert verdict_label_zh("技术面偏多") == "技术面偏多"
    assert verdict_label_zh(None) == ""


def test_card_shows_chinese_and_keeps_header_colour():
    from src.dalton.notify.dispatch import classify_automation_for_notify

    classification = classify_automation_for_notify({"name": "dalton-meta-move-down"})
    assert classification is not None
    card = build_feishu_notify_card(classification, verdict_word="THESIS_WEAKENED")
    text = json.dumps(card, ensure_ascii=False)
    assert "论点削弱" in text
    assert "THESIS_WEAKENED" not in text
    assert card["header"]["template"] == map_verdict_to_header_template("THESIS_WEAKENED")
