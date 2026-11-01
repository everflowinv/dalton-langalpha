"""Shared classification of coverage-unit service errors (sector layer P5).

``src/dalton/coverage/units.py`` and its DB layer report every refusal as a
plain ``ValueError`` whose message is their contract. Two surfaces need the
same stable code for one message — the HTTP router maps it to a 4xx, the
``dalton_coverage_unit`` tool returns it to the model — so the classification
lives here once instead of being re-derived (and drifting) per surface.
"""

from __future__ import annotations

# (message prefix, error code, HTTP status). The first matching prefix wins,
# so a longer prefix sharing words with a shorter one must come first.
_ERROR_PREFIXES: tuple[tuple[str, str, int], ...] = (
    # register: input validation, decided before any DB work
    ("a sector coverage unit never carries a ticker", "sector_ticker_refused", 400),
    ("invalid company ticker", "invalid_ticker", 400),
    ("invalid slug", "invalid_slug", 400),
    ("invalid research_tz", "invalid_research_tz", 400),
    ("kind must be one of", "invalid_kind", 400),
    ("owner_user_id must not be empty", "invalid_input", 400),
    # register/bind: id shape, decided before any DB work
    ("unit_id must be a UUID", "invalid_id", 400),
    ("workspace_id must be a UUID", "invalid_id", 400),
    # unique-constraint arbitration (the DB index is the real guard)
    ("coverage unit slug already registered", "slug_conflict", 409),
    ("workspace already bound to another active coverage unit", "workspace_conflict", 409),
    ("coverage unit constraint violated", "constraint_conflict", 409),
    # bind: workspace policy, checked inside the binding transaction
    ("workspace belongs to another user", "workspace_not_owned", 403),
    ("the Home workspace never binds a coverage unit", "home_workspace_refused", 400),
)

DEFAULT_CODE = "invalid_input"
DEFAULT_STATUS = 400


def classify_coverage_unit_error(exc: ValueError) -> tuple[str, int]:
    """Map a service-layer ``ValueError`` to ``(error_code, http_status)``."""
    message = str(exc)
    for prefix, code, status in _ERROR_PREFIXES:
        if message.startswith(prefix):
            return code, status
    # The not-found messages embed the caller-supplied id before the stable
    # part, so they cannot be prefix-matched; the suffix is the stable bit
    # ("active coverage unit <id> not found for this owner" from bind,
    # "coverage unit <id> not found for this owner" from unbind/archive).
    if "does not exist or is deleted" in message:
        return "workspace_not_found", 404
    if "not found for this owner" in message:
        return "unit_not_found", 404
    return DEFAULT_CODE, DEFAULT_STATUS
