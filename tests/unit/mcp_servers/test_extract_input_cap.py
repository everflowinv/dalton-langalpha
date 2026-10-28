"""A1(3): the HTML extraction context cap truncates the INPUT before parsing.

The 400K-char cap used to apply only to the converted output, so a huge page
drove the full parse cost (trafilatura + whole-page conversion) first.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

import mcp_servers._extract as extract_mod
from mcp_servers._extract import _MAX_FULL_PAGE_CHARS, to_markdown


def _fake_parsers(monkeypatch: pytest.MonkeyPatch, seen: dict[str, int]) -> None:
    def _trafilatura(html: str, trace=None) -> str:
        seen["trafilatura"] = len(html)
        return "extracted body"

    def _full_page(html: str, trace=None) -> str:
        seen["full"] = len(html)
        return "full body"

    monkeypatch.setattr(
        extract_mod,
        "_try_trafilatura",
        create_autospec(extract_mod._try_trafilatura, side_effect=_trafilatura),
    )
    monkeypatch.setattr(
        extract_mod,
        "_try_full_page",
        create_autospec(extract_mod._try_full_page, side_effect=_full_page),
    )


def test_oversized_input_is_truncated_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, int] = {}
    _fake_parsers(monkeypatch, seen)
    huge = "<html>" + ("x" * (_MAX_FULL_PAGE_CHARS + 50_000)) + "</html>"
    notes: list[str] = []
    to_markdown(huge, trace=notes.append)
    assert seen["trafilatura"] == _MAX_FULL_PAGE_CHARS
    assert seen["full"] == _MAX_FULL_PAGE_CHARS
    assert any("exceeds cap" in n for n in notes)


def test_normal_input_reaches_parsers_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}
    _fake_parsers(monkeypatch, seen)
    page = "<html>" + ("y" * 1000) + "</html>"
    assert to_markdown(page) == "extracted body"
    assert seen["trafilatura"] == len(page)
    assert seen["full"] == len(page)
