"""Unit tests for Dalton X search formatting (`map_x_search_response`, snowflake decoding, excerpts)."""

from __future__ import annotations

import json

from src.dalton.web_search.formatting import (
    decode_snowflake_date,
    map_x_search_response,
)


def test_decode_snowflake_date_with_real_url_and_bounds() -> None:
    """Real URL `https://x.com/NickRokke/status/2112925226740683159` decodes to 2026-10-20..2026-10-22 UTC."""
    broker_reply = {
        "ok": True,
        "provider": "xai",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"query":"GOOG","provider":"xai",'
                        '"content":"<<<EXTERNAL_UNTRUSTED_CONTENT id=\\"1\\">>>\\nSource: X Search\\n---\\n'
                        "Summary text.\\n"
                        '<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\\"1\\">>>",'
                        '"citations":["https://x.com/NickRokke/status/2112925226740683159"]}'
                    ),
                }
            ]
        },
    }
    results, _ = map_x_search_response(broker_reply, query="GOOG")
    assert len(results) == 1
    assert results[0]["author"] == "@NickRokke"
    assert results[0]["url"] == "https://x.com/NickRokke/status/2112925226740683159"
    decoded_date = results[0]["date"]
    assert isinstance(decoded_date, str)
    assert "2026-10-20T00:00:00Z" <= decoded_date <= "2026-10-22T23:59:59Z"
    assert results[0]["summary_excerpt"] is None
    assert "content" not in results[0]

    # Out-of-bounds snowflake IDs return None
    assert decode_snowflake_date("123") is None
    assert decode_snowflake_date("9999999999999999999999999") is None


def test_codex_shared_citation_line_and_date_from_snowflake_only() -> None:
    """Codex case: `市场正在讨论 2026-11-01 的听证会。[1][2]` + 2 status URLs.

    Both posts' `date` values come from snowflake ID decoding (neither is 2026-11-01),
    and both `summary_excerpt` values are None because the sentence cites both `[1][2]`.
    """
    broker_reply = {
        "ok": True,
        "provider": "xai",
        "result": {
            "details": {
                "query": "GOOG hearing",
                "provider": "xai",
                "fromDate": "2026-10-20",
                "toDate": "2026-10-22",
                "content": "市场正在讨论 2026-11-01 的听证会。[1][2]",
                "citations": [
                    "https://x.com/NickRokke/status/2112925226740683159",
                    "https://twitter.com/elerianm/status/2112800000000000000",
                    "https://x.com/sundarpichai",
                    "https://www.reuters.com/technology/google-2026-10-22/",
                ],
            }
        },
    }

    results, _ = map_x_search_response(broker_reply, query="GOOG hearing")
    assert len(results) == 2

    assert results[0]["author"] == "@NickRokke"
    assert results[0]["date"] == decode_snowflake_date("2112925226740683159")
    assert results[0]["date"] is not None and not results[0]["date"].startswith("2026-11-01")
    assert results[0]["summary_excerpt"] is None

    assert results[1]["author"] == "@elerianm"
    assert results[1]["date"] == decode_snowflake_date("2112800000000000000")
    assert results[1]["date"] is not None and not results[1]["date"].startswith("2026-11-01")
    assert results[1]["summary_excerpt"] is None


def test_ten_or_more_citations_do_not_cross_match_footnote_1_and_10() -> None:
    """With >=10 citations, `[10]` must not match citation 1, and `/status/id` uses digit boundaries."""
    citations = [
        f"https://x.com/user{i}/status/{2112900000000000000 + i}"
        for i in range(1, 11)
    ]
    broker_reply = {
        "ok": True,
        "provider": "xai",
        "result": {
            "details": {
                "query": "GOOG",
                "provider": "xai",
                "content": (
                    "总体讨论集中在云业务增速。\n"
                    "- 第十条引用的专门观点 [10]\n"
                    "- 第一条引用的专门观点 [1]"
                ),
                "citations": citations,
            }
        },
    }

    results, _ = map_x_search_response(broker_reply, query="GOOG")
    assert len(results) == 10
    assert results[0]["author"] == "@user1"
    assert results[0]["summary_excerpt"] == "第一条引用的专门观点"
    for idx in range(1, 9):
        assert results[idx]["summary_excerpt"] is None
    assert results[9]["author"] == "@user10"
    assert results[9]["summary_excerpt"] == "第十条引用的专门观点"


# Sentence-level attribution (lead, round 3 review): a line may hold several sentences and a
# sentence may wrap across lines; neither may hand an uncited claim or a shared sentence to a post.
_U1 = "https://x.com/a/status/2112925226740683159"
_U2 = "https://x.com/b/status/2112609917127590040"


def test_summary_excerpt_takes_only_the_cited_sentence_of_a_line() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    # Running prose gets no excerpt at all: the uncited first sentence must never ride along.
    got = _extract_unique_summary_excerpt("公司已发布财报。市场讨论听证会。[1]", _U1, "2112925226740683159", 1)
    assert got is None


def test_summary_excerpt_empty_when_one_sentence_wraps_across_lines_citing_two_posts() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = "讨论听证会[1]，\n并关注监管影响[2]。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(text, _U2, "2112609917127590040", 2) is None


def test_summary_excerpt_english_sentences_with_trailing_footnotes() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = "Pichai says Cloud is forgoing revenue. [1] Analysts disagree. [2]"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(text, _U2, "2112609917127590040", 2) is None


def test_summary_excerpt_keeps_a_multi_sentence_list_item_quote_whole() -> None:
    """xAI's real shape: one list item per post, a multi-sentence quote, a `[[n]](url)` citation link."""
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = (
        "- **@a** (high engagement: 52 likes):  \n"
        "  “AI demand is outrunning compute. Cloud is forgoing revenue. Capex is guided up…”"
        f"[[1]]({_U1})\n"
        f"- **@b** adds: “Compute is the scarcest asset.”[[2]]({_U2})"
    )
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got is not None
    assert got.startswith("@a (high engagement: 52 likes):")
    assert "AI demand is outrunning compute. Cloud is forgoing revenue. Capex is guided up" in got
    assert "[" not in got and "**" not in got and "scarcest" not in got


def test_summary_excerpt_keeps_leading_dates_and_numbers() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    got = _extract_unique_summary_excerpt(f"- 10/21 已发布财报。[[1]]({_U1})", _U1, "2112925226740683159", 1)
    assert got == "10/21 已发布财报。"



def test_summary_excerpt_none_for_codex_prose_counterexamples() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    for text in (
        f"甲。[[1]]({_U1})乙。",
        "Revenue grew in 2025. Demand fell. [1]",
        "According to Acme Inc. [1] and Beta [2], demand rose.",
    ):
        assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None, text



def test_summary_excerpt_stops_at_unindented_prose_after_the_cited_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- **@a** says: “Compute is scarce.”[[1]]({_U1})\nOverall, sentiment turned negative after earnings."
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got == "@a says: “Compute is scarce.”"



def test_summary_excerpt_item_ends_after_bare_footnote_or_url_citation() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    got = _extract_unique_summary_excerpt("- 甲认为需求增长。[1]\n总体市场情绪转弱。", _U1, "2112925226740683159", 1)
    assert got == "甲认为需求增长。"
    got = _extract_unique_summary_excerpt(f"- 甲认为需求增长。 {_U1}\n总体市场情绪转弱。", _U1, "2112925226740683159", 1)
    assert got == "甲认为需求增长。"


def test_summary_excerpt_none_when_list_item_cites_a_second_post_on_the_next_line() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n[[2]]({_U2})"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(text, _U2, "2112609917127590040", 2) is None



def test_summary_excerpt_year_at_line_start_is_not_a_list_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    assert _extract_unique_summary_excerpt("2025. Demand fell. [1]", _U1, "2112925226740683159", 1) is None
    assert _extract_unique_summary_excerpt(f"2025. Demand fell. [[1]]({_U1})", _U1, "2112925226740683159", 1) is None
    # A real 1-2 digit ordered item still works.
    assert _extract_unique_summary_excerpt(f"1. Demand fell. [[1]]({_U1})", _U1, "2112925226740683159", 1) == "Demand fell."



def test_summary_excerpt_item_ends_after_www_status_url() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    u = "https://www.x.com/a/status/2112925226740683159"
    got = _extract_unique_summary_excerpt(f"- 需求增长。 {u}\n总体情绪转弱。", u, "2112925226740683159", 1)
    assert got == "需求增长。"


def test_summary_excerpt_none_when_indented_continuation_after_blank_line_cites_another_post() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = "- 两位作者都认为需求增长。[1]\n\n  [2]"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None



def test_summary_excerpt_citation_only_line_with_trailing_stop_still_joins_the_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n[[2]]({_U2})。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None


def test_summary_excerpt_non_status_url_with_prose_is_not_a_citation_line() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 需求增长。[[1]]({_U1})\nhttps://reuters.com/a。总体市场情绪转弱。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) == "需求增长。"



def test_summary_excerpt_url_query_suffix_does_not_swallow_following_prose() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 需求增长。[[1]]({_U1})\n{_U1}?s=20。总体市场情绪转弱。"
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got is None or "总体市场情绪转弱" not in got


def test_summary_excerpt_bracketed_second_citation_joins_the_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n（[[2]]({_U2})）。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None



def test_summary_excerpt_status_url_with_query_then_prose_is_not_a_citation_line() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 需求增长。[[1]]({_U1})\n{_U1}?s=20。总体市场情绪转弱。"
    got = _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1)
    assert got == "需求增长。"


def test_summary_excerpt_quoted_second_citation_line_joins_the_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n「[[2]]({_U2})」…"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None



def test_summary_excerpt_crlf_citation_only_line_joins_the_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    text = "- 两位作者都认为需求增长。[1]\r\n[2]\r\n总体情绪转弱。"
    assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None


def test_summary_excerpt_second_bare_status_url_with_path_and_query_joins_the_item() -> None:
    from src.dalton.web_search.formatting import _extract_unique_summary_excerpt

    for suffix in ("/photo/1?s=20", "?s=20#details"):
        text = f"- 两位作者都认为需求增长。[[1]]({_U1})\n{_U2}{suffix}\n总体情绪转弱。"
        assert _extract_unique_summary_excerpt(text, _U1, "2112925226740683159", 1) is None, suffix


# --- Generated summary vs post evidence (prod incident: answer-only output
# read as normal results — fabricated "CEO quotes", <｜eos｜> markers, repeats)


def _broker_reply(answer: str, citations: list) -> dict:
    """A broker reply whose answer is wrapped the way the gateway sends it."""
    return {
        "ok": True,
        "provider": "xai",
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"query":"T","provider":"xai",'
                        '"content":"<<<EXTERNAL_UNTRUSTED_CONTENT id=\\"1\\">>>\\n'
                        + answer.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                        + '\\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\\"1\\">>>",'
                        + '"citations":' + json.dumps(citations) + "}"
                    ),
                }
            ]
        },
    }


def test_no_evidence_answer_is_marked_and_unquotes_only_long_quotes() -> None:
    """No citations: the result is marked no_evidence, unquotes >=20-char quoted
    spans (keeping their text so it is not presented as a direct quote), and
    leaves short quoted terms (<20 chars) like "AI", "priced in", "EPS" intact
    across straight and Chinese quote styles."""
    answer = (
        'The CEO said "we will double capex next fiscal year" per traders on "AI" '
        'and "EPS" being "priced in". '
        '另有传闻称“管理层预计四季度云业务增速将超过市场预期五个百分点”，'
        '以及「明年资本开支指引将进一步上调至百亿美元级别」与『下半年整体毛利率预计将稳定维持在百分之六十以上的高位区间』，'
        '但「毛利率」本身仍是焦点。Sentiment bullish.'
    )
    cleaned, artifact = map_x_search_response(
        _broker_reply(answer, []), query="NVDA"
    )
    assert cleaned == []
    assert artifact["no_evidence"] is True
    assert artifact["evidence_count"] == 0
    ans = artifact["answer"]
    # Long quotes (>= 20 chars) lose their surrounding quote marks but keep their text.
    assert '"we will double capex next fiscal year"' not in ans
    assert "we will double capex next fiscal year" in ans
    assert "“管理层预计四季度云业务增速将超过市场预期五个百分点”" not in ans
    assert "管理层预计四季度云业务增速将超过市场预期五个百分点" in ans
    assert "「明年资本开支指引将进一步上调至百亿美元级别」" not in ans
    assert "明年资本开支指引将进一步上调至百亿美元级别" in ans
    assert "『下半年整体毛利率预计将稳定维持在百分之六十以上的高位区间』" not in ans
    assert "下半年整体毛利率预计将稳定维持在百分之六十以上的高位区间" in ans
    # Short quotes (< 20 chars) keep their quotes and text untouched.
    assert '"AI"' in ans
    assert '"EPS"' in ans
    assert '"priced in"' in ans
    assert "「毛利率」" in ans
    assert "Sentiment bullish" in ans


def test_control_token_fullwidth_and_ascii_in_sourced_and_unsourced_summaries() -> None:
    """Full-width <｜eos｜> and ASCII <|eos|> markers:
    - Sourced summaries strip only the marker itself and keep trailing content.
    - No-evidence summaries truncate at the marker."""
    from src.dalton.web_search.formatting import X_SUMMARY_TRUNCATED_NOTE

    for marker in ("<｜eos｜>", "<|eos|>"):
        sourced_answer = f"First solid paragraph.{marker}Second valid paragraph."
        cleaned, artifact = map_x_search_response(
            _broker_reply(
                sourced_answer,
                ["https://x.com/NickRokke/status/2112925226740683159"],
            ),
            query="NVDA",
        )
        assert artifact["summary_issues"] == ["control_marker"]
        assert marker not in artifact["answer"]
        assert "First solid paragraph." in artifact["answer"]
        assert "Second valid paragraph." in artifact["answer"]
        assert X_SUMMARY_TRUNCATED_NOTE in artifact["answer"]
        assert cleaned and cleaned[0]["type"] == "x_post"

        unsourced_answer = f"First solid paragraph.{marker}Garbage after the marker"
        cleaned_no_ev, artifact_no_ev = map_x_search_response(
            _broker_reply(unsourced_answer, []),
            query="NVDA",
        )
        assert cleaned_no_ev == []
        assert artifact_no_ev["summary_issues"] == ["control_marker"]
        assert marker not in artifact_no_ev["answer"]
        assert "First solid paragraph." in artifact_no_ev["answer"]
        assert "Garbage after the marker" not in artifact_no_ev["answer"]


def test_repeated_paragraph_sourced_keeps_later_paragraphs_while_unsourced_truncates() -> None:
    """For a sourced summary, only the duplicate paragraph is removed and
    subsequent unique paragraphs are preserved; for a no-evidence summary, it
    truncates from the repeat onward."""
    from src.dalton.web_search.formatting import X_SUMMARY_TRUNCATED_NOTE

    answer = (
        "Traders report demand is strong into quarter end.\n\n"
        "Traders report demand is strong into quarter end.\n\n"
        "Second unique paragraph with distinct takeaway."
    )
    _cleaned, sourced_artifact = map_x_search_response(
        _broker_reply(
            answer,
            ["https://x.com/NickRokke/status/2112925226740683159"],
        ),
        query="NVDA",
    )
    assert sourced_artifact["summary_issues"] == ["repeated_block"]
    assert sourced_artifact["answer"].count("Traders report demand is strong") == 1
    assert "Second unique paragraph with distinct takeaway." in sourced_artifact["answer"]
    assert X_SUMMARY_TRUNCATED_NOTE in sourced_artifact["answer"]

    _cleaned_no_ev, unsourced_artifact = map_x_search_response(
        _broker_reply(answer, []),
        query="NVDA",
    )
    assert unsourced_artifact["summary_issues"] == ["repeated_block"]
    assert unsourced_artifact["answer"].count("Traders report demand is strong") == 1
    assert "Second unique paragraph with distinct takeaway." not in unsourced_artifact["answer"]


def test_inline_source_url_at_end_extracted_before_summary_cleanup() -> None:
    """When citations=[] and the post URL is at the end of answer_text after a
    repeated block or <｜eos｜> marker, sources are extracted first so the
    response is NOT misclassified as no_evidence."""
    answer = (
        "Traders report demand is strong into quarter end.\n\n"
        "Traders report demand is strong into quarter end.<｜eos｜>\n\n"
        "Source post: https://x.com/NickRokke/status/2112925226740683159"
    )
    cleaned, artifact = map_x_search_response(
        _broker_reply(answer, []),
        query="NVDA",
    )
    assert len(cleaned) == 1
    assert cleaned[0]["url"] == "https://x.com/NickRokke/status/2112925226740683159"
    assert artifact.get("no_evidence") is not True
    assert artifact["evidence_count"] == 1
    assert "https://x.com/NickRokke/status/2112925226740683159" in artifact["answer"]


def test_short_quoted_terms_pair_correctly_and_stay_quoted() -> None:
    """Two short quoted terms on one line pair each with its OWN closing quote
    (never spanning from one term's closing quote to the next term's opener),
    and both stay quoted verbatim."""
    from src.dalton.web_search.formatting import _strip_quoted_spans

    answer = (
        '"AI" demand continues to grow over the next several quarters "GPU"'
    )
    assert _strip_quoted_spans(answer) == answer

    # A long quote between short terms: only the long one loses its marks.
    mixed = 'Short "AI" note. "the datacenter buildout is far from finished per management" end.'
    out = _strip_quoted_spans(mixed)
    assert '"AI"' in out
    assert '"the datacenter buildout' not in out
    assert "the datacenter buildout is far from finished per management" in out

    # Chinese pairs behave the same.
    zh = "“AI”需求持续，“管理层在电话会上表示未来三个季度的资本开支将明显高于市场预期”结束。"
    zh_out = _strip_quoted_spans(zh)
    assert "“AI”" in zh_out
    assert "“管理层在电话会上" not in zh_out
    assert "管理层在电话会上表示未来三个季度的资本开支将明显高于市场预期" in zh_out
