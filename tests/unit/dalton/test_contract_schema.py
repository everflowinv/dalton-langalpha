"""Completion contracts: every shipped contract validates against the schema."""

from __future__ import annotations

import pytest

from src.dalton.workflows.contracts import (
    ContractError,
    list_skill_contracts,
    list_workflow_contracts,
    load_workflow_contract,
    missing_params,
    render_template,
    validate_contract,
)

WORKFLOW_CONTRACTS = [
    "initial-coverage",
    "full-coverage",
    "bullbear-doc",
    "earnings-deep-update",
    "weekly-coverage-review",
    "topic-research",
]

# The seven multi-step skill contracts moved OUT of this PR — see
# docs/dalton/workflow-completion-followups.md.
SKILL_CONTRACTS: list[str] = []


@pytest.mark.parametrize("name", WORKFLOW_CONTRACTS)
def test_workflow_contract_valid(name: str) -> None:
    contract = load_workflow_contract(name)
    assert not validate_contract(contract)
    assert contract["name"] == name
    assert contract["stages"], f"{name} declares no stages"



def test_discovery_finds_every_declared_contract() -> None:
    assert sorted(list_workflow_contracts()) == sorted(WORKFLOW_CONTRACTS)
    assert list_skill_contracts() == []


def test_contract_rejects_unknown_top_level_key() -> None:
    errors = validate_contract({"name": "x", "params_required": [], "stages": [], "closing": {}, "bogus": 1})
    assert errors


def test_render_template_substitutes_params_and_date_alias() -> None:
    params = {"as_of": "2026-10-23", "ticker": "META", "quarter": "FY26Q3"}
    assert render_template("drafts/{as_of}-memo.md", params) == "drafts/2026-10-23-memo.md"
    assert render_template("research/{date}-deep-{quarter}/reconcile.md", params) == (
        "research/2026-10-23-deep-FY26Q3/reconcile.md"
    )


def test_render_template_falls_back_to_latest_for_topic_runs() -> None:
    assert render_template("drafts/{date}-topic-{slug}.md", {"slug": "ai-capex"}) == (
        "drafts/latest-topic-ai-capex.md"
    )


def test_render_template_names_the_missing_key() -> None:
    with pytest.raises(ContractError, match="quarter"):
        render_template("research/{quarter}.md", {"as_of": "2026-10-23"})


def test_missing_params_lists_only_the_gaps() -> None:
    contract = {"params_required": ["ticker", "workspace_dir", "as_of"]}
    params = {"ticker": "META", "as_of": "2026-10-23"}
    assert missing_params(contract, params) == ["workspace_dir"]
