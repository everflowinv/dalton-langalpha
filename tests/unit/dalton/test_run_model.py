"""Unit tests for pure run-row model extraction helpers."""

from __future__ import annotations

import pytest

from src.dalton.llm.run_model import adopted_run_llm_model


def test_adopted_run_llm_model_returns_value_when_present():
    existing = {"metadata": {"llm_model": "gemini-4-neon-high-oauth"}}
    assert adopted_run_llm_model(existing) == "gemini-4-neon-high-oauth"


@pytest.mark.parametrize(
    "existing",
    [
        {},
        {"metadata": None},
        {"metadata": {}},
        None,
    ],
    ids=["missing-metadata", "none-metadata", "empty-metadata", "none-row"],
)
def test_adopted_run_llm_model_returns_none_without_metadata(existing):
    assert adopted_run_llm_model(existing) is None


@pytest.mark.parametrize(
    "metadata",
    ["gemini-4-neon-high-oauth", ["gemini-4-neon-high-oauth"], 42, True],
    ids=["str", "list", "int", "bool"],
)
def test_adopted_run_llm_model_returns_none_when_metadata_not_dict(metadata):
    assert adopted_run_llm_model({"metadata": metadata}) is None


@pytest.mark.parametrize(
    "llm_model",
    ["", None, 123, ["gemini-4-neon-high-oauth"]],
    ids=["empty-str", "none", "int", "list"],
)
def test_adopted_run_llm_model_returns_none_when_llm_model_empty_or_non_string(llm_model):
    assert adopted_run_llm_model({"metadata": {"llm_model": llm_model}}) is None
