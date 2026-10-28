"""The structured profile: the keys the onboarding form, the Settings page and
the agent's profile file share, the values each may hold, and the one check
every writer holds them to.

They sit in the same JSONB columns as the free-text keys written before them
(``risk_tolerance``, ``holding_period``, ``tone``, ...), which keep no
vocabulary and stay readable and writable as they were.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

RISK_LEVELS = ("conservative", "moderate", "growth", "aggressive")
INVESTOR_TYPES = ("individual", "professional", "student")
EXPERIENCE_LEVELS = ("under_1y", "1_3y", "3_10y", "over_10y")
# What the user wants the agent's help with, most wanted first.
HELP_AREAS = ("ideas", "research", "portfolio", "news", "trading", "learning")
# Both what the user has traded (``familiar_instruments``) and what they want
# to trade (``asset_classes``).
INSTRUMENTS = (
    "stock",
    "etf",
    "index",
    "option",
    "future",
    "bond",
    "fund",
    "forex",
    "crypto",
    "commodity",
)
MARKETS = ("us", "hk", "cn", "jp", "eu")
APPROACHES = ("fundamental", "macro", "quantitative", "technical", "event_driven")
INVESTING_STYLES = ("growth", "value", "income", "trading", "index")
HORIZONS = ("intraday", "days", "months", "years")
# Where the user holds accounts, connected or not: ``other`` is one not
# listed here, ``none`` no account yet.
BROKERAGES = (
    "fidelity",
    "schwab",
    "vanguard",
    "etrade",
    "robinhood",
    "ibkr",
    "webull",
    "moomoo",
    "futu",
    "tiger",
    "longbridge",
    "tastytrade",
    "other",
    "none",
)

# How the user wants the agent to work with them by default: how much
# research a question gets, what an answer usually is, and when the agent
# reaches out.
RESEARCH_DEPTHS = ("light", "adaptive", "thorough")
# A report is not one of these: it is ``output_format: "html"``, which
# Settings toggles and which selects a section of the prompt of its own.
DELIVERABLES = ("text", "visual")
PROACTIVITY_LEVELS = ("proactive", "when_needed", "on_request")
# Absent is markdown: Settings' switch writes null to go back to it.
OUTPUT_FORMATS = ("markdown", "html")

# Sectors are the user's own words, so they are bounded rather than listed:
# each one is a line item in the prompt's profile block.
MAX_SECTORS = 20
MAX_SECTOR_LENGTH = 40
_SECTOR_SLUG = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class _Vocabulary:
    # None: a short slug of the user's own (sectors).
    choices: tuple[str, ...] | None
    many: bool = False
    # What the key means and the values it takes, in markdown: the agent's
    # profile README lists it beside the key, and the API schema describes
    # the field with it.
    doc: str = ""


def _ticks(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


STRUCTURED_PREFERENCES: dict[str, dict[str, _Vocabulary]] = {
    "risk_preference": {
        "risk_level": _Vocabulary(RISK_LEVELS, doc=_ticks(RISK_LEVELS)),
    },
    "investment_preference": {
        "investor_type": _Vocabulary(
            INVESTOR_TYPES, doc=f"Who the user is: {_ticks(INVESTOR_TYPES)}"
        ),
        "experience": _Vocabulary(
            EXPERIENCE_LEVELS,
            doc=f"Years investing or trading: {_ticks(EXPERIENCE_LEVELS)}",
        ),
        "help_with": _Vocabulary(
            HELP_AREAS,
            many=True,
            doc=f"What the user wants help with, most wanted first: any of {_ticks(HELP_AREAS)}",
        ),
        "familiar_instruments": _Vocabulary(
            INSTRUMENTS,
            many=True,
            doc=f"Instruments the user has traded: any of {_ticks(INSTRUMENTS)}",
        ),
        "markets": _Vocabulary(MARKETS, many=True, doc=f"Any of {_ticks(MARKETS)}"),
        "asset_classes": _Vocabulary(
            INSTRUMENTS,
            many=True,
            doc="Instruments the user wants to trade: the same values as `familiar_instruments`",
        ),
        "approaches": _Vocabulary(
            APPROACHES,
            many=True,
            doc=f"Kinds of analysis the user leans on: any of {_ticks(APPROACHES)}",
        ),
        "styles": _Vocabulary(
            INVESTING_STYLES, many=True, doc=f"Any of {_ticks(INVESTING_STYLES)}"
        ),
        "horizon": _Vocabulary(
            HORIZONS, doc=f"Usual holding horizon: {_ticks(HORIZONS)}"
        ),
        "sectors": _Vocabulary(
            None,
            many=True,
            doc=(
                "Lowercase slugs such as `technology` or `real_estate`; at most "
                f"{MAX_SECTORS}, each at most {MAX_SECTOR_LENGTH} characters"
            ),
        ),
        "brokerages": _Vocabulary(
            BROKERAGES,
            many=True,
            doc=(
                "Brokerages the user holds accounts with: any of "
                f"{_ticks(b for b in BROKERAGES if b not in ('other', 'none'))}; "
                "`other` for one not listed, `none` for no account yet"
            ),
        ),
    },
    "agent_preference": {
        "research_depth": _Vocabulary(
            RESEARCH_DEPTHS,
            doc=(
                "How much research a question gets by default: `light` (a quick answer, "
                "with the full research offered rather than done unasked), `adaptive` "
                "(as much as the question needs), `thorough` (research before every answer)"
            ),
        ),
        "deliverable": _Vocabulary(
            DELIVERABLES,
            doc=(
                "What an answer with substance usually is: `text` (text in the chat) or "
                "`visual` (interactive charts and dashboards in the chat). A report is not "
                'one of these: it is `"output_format": "html"`, which overrides this key'
            ),
        ),
        "proactivity": _Vocabulary(
            PROACTIVITY_LEVELS,
            doc=(
                "When to reach out: `proactive` (keep them posted), `when_needed` (only "
                "when something needs their attention), `on_request` (only when they ask)"
            ),
        ),
        "output_format": _Vocabulary(
            OUTPUT_FORMATS,
            doc=(
                "`markdown` (the default) or `html`, which makes a styled HTML report the "
                "user's usual deliverable"
            ),
        ),
    },
}

# The columns ``structured_preferences`` flattens into the prompt's Investment
# Profile. ``agent_preference`` reaches the prompt whole, where its structured
# keys select guidance rather than print as profile lines.
_INVESTMENT_PROFILE_COLUMNS = ("risk_preference", "investment_preference")


def check_profile(
    patches: Mapping[str, Any], stored: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """``patches`` (column to its values) with each structured key in its
    stored spelling, and what is wrong with the rest, as (path, message)
    pairs, every column's together so one refusal can name them all.

    The one check the preferences endpoint and the agent's ``preference.json``
    both run, so the two writers cannot disagree about what a key may hold;
    each raises its own error from the pairs. Free-text keys pass untouched.
    A null passes: the endpoint's merge reads it as a deletion, and the file
    as no value. So does a value equal to the one ``stored`` holds, because
    ``horizon`` and ``sectors`` were free-form before they had a vocabulary
    (the profile README once suggested ``"horizon": "long_term"``), and a
    save that leaves such a value as it is must not be refused for it. In a
    list the rule holds item by item: a form that keeps an old sector and
    toggles another sends the whole list, and the old one passes as written.
    """
    checked = dict(patches)
    problems: list[tuple[str, str]] = []
    for column in STRUCTURED_PREFERENCES:
        patch = patches.get(column)
        if not patch:
            continue
        checked[column] = _checked_column(
            column, patch, (stored or {}).get(column), problems
        )
    return checked, problems


def _checked_column(
    column: str, values: Mapping[str, Any], stored: Any, problems: list[tuple[str, str]]
) -> dict[str, Any]:
    before = stored if isinstance(stored, Mapping) else {}
    checked = dict(values)
    for key, vocab in STRUCTURED_PREFERENCES[column].items():
        value = values.get(key)
        if value is None or (key in before and value == before[key]):
            continue
        path = f"{column}.{key}"
        if vocab.many:
            held = before.get(key) if isinstance(before.get(key), list) else ()
            checked[key] = _checked_list(value, vocab.choices, path, problems, held)
        else:
            checked[key] = _checked_choice(value, vocab.choices or (), path, problems)
    return checked


def _checked_choice(
    value: Any, choices: tuple[str, ...], path: str, problems: list[tuple[str, str]]
) -> Any:
    if not isinstance(value, str):
        problems.append(
            (path, f"expected one of {', '.join(choices)}, got {type(value).__name__}")
        )
        return value
    spelled = value.strip().lower()
    if spelled not in choices:
        problems.append((path, f"{value!r} is not one of {', '.join(choices)}"))
    return spelled


def _checked_list(
    value: Any,
    choices: tuple[str, ...] | None,
    path: str,
    problems: list[tuple[str, str]],
    held: Sequence[Any] = (),
) -> Any:
    if not isinstance(value, list):
        expected = f"any of {', '.join(choices)}" if choices else "short slugs"
        problems.append(
            (path, f"expected a list of {expected}, got {type(value).__name__}")
        )
        return value
    kept: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            problems.append(
                (f"{path}[{i}]", f"expected a string, got {type(item).__name__}")
            )
            continue
        if item in held:
            # Saved before the key had a vocabulary, and left as it was.
            spelled = item
        elif choices is not None:
            spelled = item.strip().lower()
            if spelled not in choices:
                problems.append(
                    (f"{path}[{i}]", f"{item!r} is not one of {', '.join(choices)}")
                )
                continue
        else:
            # A sector typed as words ("Real Estate") is the slug the form
            # would have sent ("real_estate").
            spelled = "_".join(item.lower().split())
            if len(spelled) > MAX_SECTOR_LENGTH or not _SECTOR_SLUG.fullmatch(spelled):
                problems.append(
                    (
                        f"{path}[{i}]",
                        f"{item!r} is not a short slug: lowercase letters and digits joined by "
                        f"'_' or '-', at most {MAX_SECTOR_LENGTH} characters",
                    )
                )
                continue
        if spelled not in kept:
            kept.append(spelled)
    if choices is None and len(kept) > MAX_SECTORS:
        problems.append((path, f"{len(kept)} sectors; at most {MAX_SECTORS}"))
    return kept


def structured_preferences(preferences: Mapping[str, Any] | None) -> dict[str, Any]:
    """The structured investing keys of a preferences row that hold a value, flat.

    For the prompt's Investment Profile, which prints each as one line: a
    string, or a list of strings. A value saved before the key had a
    vocabulary is kept as the user left it, with its whitespace folded so it
    stays on its line.
    """
    flat: dict[str, Any] = {}
    for column in _INVESTMENT_PROFILE_COLUMNS:
        section = (preferences or {}).get(column)
        if not isinstance(section, Mapping):
            continue
        for key in STRUCTURED_PREFERENCES[column]:
            value = section.get(key)
            if isinstance(value, str):
                value = " ".join(value.split())
            elif isinstance(value, list):
                value = [
                    " ".join(v.split())
                    for v in value
                    if isinstance(v, str) and v.strip()
                ]
            else:
                continue
            if value:
                flat[key] = value
    return flat


def prompt_preferences(
    agent_preference: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """``agent_preference`` split the way the prompt reads it: the keys it
    prints as the user wrote them, and the structured keys whose value
    selects guidance instead (``output_format: "html"``, the report, among
    them).

    A structured key holding a value outside its vocabulary, such as free text
    written before the key had one, stays with the printed keys, so the user's
    words still reach the model.
    """
    printed = dict(agent_preference) if isinstance(agent_preference, Mapping) else {}
    steering: dict[str, str] = {}
    for key, vocab in STRUCTURED_PREFERENCES["agent_preference"].items():
        if printed.get(key) in (vocab.choices or ()):
            steering[key] = printed.pop(key)
    return printed, steering
