"""Dalton's default language for a user who has not picked one.

``users.locale`` stays NULL until the user chooses a language in Settings
(upstream treats NULL as "no preference"). Dalton's users are Chinese-first,
so wherever the server reads that column for the agent, NULL means zh-CN —
the same default the web UI applies (``web/src/lib/dalton/userLocale.ts``).
Keep the two constants in step.
"""

from __future__ import annotations

from typing import Any, Optional

DEFAULT_USER_LOCALE = "zh-CN"


def with_default_locale(profile: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return ``profile`` with a NULL/empty ``locale`` filled with the Dalton default.

    ``None`` (the profile read failed) stays ``None`` so callers keep omitting
    the profile block. The input is not mutated: it may be a cached object.
    """
    if profile is None or profile.get("locale"):
        return profile
    return {**profile, "locale": DEFAULT_USER_LOCALE}
