"""Completion contracts: every shipped contract validates against the schema."""

from __future__ import annotations

import pytest

from src.dalton.workflows.contracts import (
    SKILLS_DIR,
    ContractError,
    list_skill_contracts,
    list_workflow_contracts,
    load_contract,
    load_skill_contract,
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

SKILL_CONTRACTS = [
    "dalton-earnings-preview",
    "dalton-earnings-flash",
    "dalton-stock-move",
    "dalton-filing-review",
    "dalton-thesis-delta",
    "dalton-call",
    "dalton-publish",
    "dalton-earnings-calendar",
]

PUSH_CONTRACTS = [
    ("dalton-earnings-preview", "research/push/{as_of}-earnings-preview-{quarter}.md", False, "drafts/{as_of}-preview-{quarter}.md"),
    ("dalton-earnings-flash", "research/push/{as_of}-earnings-flash-{quarter}.md", False, None),
    ("earnings-deep-update", "research/push/{release_date}-earnings-deep-{quarter}.md", False, "drafts/{release_date}-deep-{quarter}.md"),
    ("dalton-stock-move", "research/push/{as_of}-stock-move.md", True, None),
    ("weekly-coverage-review", "research/push/{end}-director-weekly.md", False, None),
    ("dalton-earnings-calendar", "research/push/{as_of}-earnings-calendar.md", False, None),
]


@pytest.mark.parametrize(
    ("name", "expected_push_path", "expected_skip_no_change", "expected_lang_draft"),
    PUSH_CONTRACTS,
)
def test_push_contracts_declare_push_draft_and_language_review_scope(
    name: str,
    expected_push_path: str,
    expected_skip_no_change: bool,
    expected_lang_draft: str | None,
) -> None:
    contract = load_contract(name)
    assert not validate_contract(contract)
    closing = contract.get("closing") or {}
    push_draft = closing.get("push_draft")
    assert isinstance(push_draft, dict)
    assert push_draft.get("path") == expected_push_path
    assert bool(push_draft.get("skip_when_no_change", False)) is expected_skip_no_change
    lang_review = closing.get("language_review")
    if expected_lang_draft is None:
        assert lang_review is None
    else:
        assert isinstance(lang_review, dict)
        assert lang_review.get("draft") == expected_lang_draft


def test_push_draft_rejects_unknown_fields_and_undeclared_params() -> None:
    unknown_field = {
        "name": "x",
        "params_required": ["as_of"],
        "stages": [{"name": "s"}],
        "closing": {
            "push_draft": {
                "path": "research/push/{as_of}-x.md",
                "unknown_key": True,
            }
        },
    }
    assert validate_contract(unknown_field)

    undeclared_param = {
        "name": "x",
        "params_required": ["as_of"],
        "stages": [{"name": "s"}],
        "closing": {
            "push_draft": {
                "path": "research/push/{as_of}-{automation_name}.md",
            }
        },
    }
    assert validate_contract(undeclared_param)


@pytest.mark.parametrize("name", WORKFLOW_CONTRACTS)
def test_workflow_contract_valid(name: str) -> None:
    contract = load_workflow_contract(name)
    assert not validate_contract(contract)
    assert contract["name"] == name
    assert contract["stages"], f"{name} declares no stages"


@pytest.mark.parametrize("name", SKILL_CONTRACTS)
def test_skill_contract_valid(name: str) -> None:
    contract = load_skill_contract(name)
    assert not validate_contract(contract)
    assert contract["name"] == name
    assert contract["stages"], f"{name} declares no stages"
    # The bare spelling resolves to the same contract.
    assert load_skill_contract(name.removeprefix("dalton-")) == contract


@pytest.mark.parametrize("name", SKILL_CONTRACTS)
def test_skill_md_closing_requires_check_done(name: str) -> None:
    """Each multi-step skill's closing step names its own contract."""
    text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    assert f'dalton_check_done(workflow="{name}"' in text
    assert "不得汇报“完成”" in text


def test_discovery_finds_every_declared_contract() -> None:
    assert sorted(list_workflow_contracts()) == sorted(WORKFLOW_CONTRACTS)
    assert sorted(list_skill_contracts()) == sorted(SKILL_CONTRACTS)


def test_conditional_knowledge_needs_verdict_kinds() -> None:
    """knowledge_docs_any_unless_no_change reads this round's verdict words,
    so a contract declaring it without verdict_kinds_any is invalid."""
    errors = validate_contract(
        {
            "name": "x",
            "params_required": [],
            "stages": [{"name": "s"}],
            "closing": {"knowledge_docs_any_unless_no_change": ["thesis"]},
        }
    )
    assert errors


def test_requires_json_allowed_values_and_sidecars_validate() -> None:
    """The two round-2 keys — a conditional field's allowed values (both on a
    requires_json artifact and a knowledge_docs_if_json when) and check_draft
    sidecars derived off the draft path — are legal contract surface."""
    contract = {
        "name": "x",
        "draft": "{draft_path}",
        "params_required": ["draft_path"],
        "stages": [
            {
                "name": "s",
                "artifacts": [
                    {
                        "path": "{draft_path}",
                        "min_bytes": 100,
                        "sidecars": [{"suffix": ".annotated.md", "min_bytes": 50}],
                        "requires_json": {
                            "json": "research/tracker/last_run.json",
                            "field": "material",
                            "equals": "yes",
                            "allowed_values": ["yes", "no"],
                        },
                    }
                ],
            }
        ],
        "closing": {
            "knowledge_docs_if_json": [
                {
                    "json": "research/tracker/last_run.json",
                    "when": {
                        "field": "material",
                        "equals": "yes",
                        "allowed_values": ["yes", "no"],
                    },
                    "docs_any": ["thesis"],
                }
            ]
        },
    }
    assert not validate_contract(contract)

    empty_values = {
        "name": "x",
        "params_required": [],
        "stages": [
            {
                "name": "s",
                "artifacts": [
                    {
                        "path": "a.md",
                        "requires_json": {
                            "json": "s.json",
                            "field": "material",
                            "equals": "yes",
                            "allowed_values": [],
                        },
                    }
                ],
            }
        ],
        "closing": {},
    }
    assert validate_contract(empty_values)


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
