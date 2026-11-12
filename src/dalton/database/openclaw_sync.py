"""User model-preference references.

``dalton_openclaw_sync_runs`` stays: native model sync (``source='native'``)
still writes it from ``native_sync.py``. Broker-model sync
(``source='openclaw'``) and its lock/run helpers were removed with the
``ocb-*`` catalog (OC-5). This module keeps the preference scan that native
reference protection still calls.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection


async def find_user_referenced_models() -> dict[str, list[str]]:
    """Return `{model_key: [reference_source, ...]}` across all users' preferences and subagents (P0.7 §3.1)."""
    from src.dalton.llm.model_prefs import DALTON_MODEL_LIST_KEYS, DALTON_SINGLE_MODEL_KEYS
    from src.server.services.llm.availability import _MODEL_PREF_KEYS

    refs: dict[str, list[str]] = {}

    def _add(model_key: Any, source: str) -> None:
        if isinstance(model_key, str) and model_key.strip():
            refs.setdefault(model_key.strip(), []).append(source)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, model_preference, other_preference, agent_preference
                FROM user_preferences
                """
            )
            for row in await cur.fetchall():
                uid = str(row.get("user_id") or "user")
                mp = row.get("model_preference") or {}
                op = row.get("other_preference") or {}
                ap = row.get("agent_preference") or {}
                for bag in (mp, op):
                    if not isinstance(bag, dict):
                        continue
                    # The same key list the per-turn resolver reads
                    # (src/server/services/llm/config.py), so a saved legacy
                    # `summarization_model` counts exactly as long as the
                    # resolver would still use it.
                    for k in _MODEL_PREF_KEYS:
                        if bag.get(k):
                            _add(bag.get(k), f"user:{uid}:{k}")
                    fb = bag.get("fallback_models")
                    if isinstance(fb, list):
                        for item in fb:
                            _add(item, f"user:{uid}:fallback_models")
                    # Dalton role models saved under Settings > Models (advanced).
                    for k in DALTON_SINGLE_MODEL_KEYS:
                        _add(bag.get(k), f"user:{uid}:{k}")
                    for k in DALTON_MODEL_LIST_KEYS:
                        vals = bag.get(k)
                        if isinstance(vals, list):
                            for item in vals:
                                _add(item, f"user:{uid}:{k}")

                if isinstance(ap, dict):
                    sub_models = ap.get("subagent_models") or ap.get("subagents") or {}
                    if isinstance(sub_models, dict):
                        for sname, sval in sub_models.items():
                            if isinstance(sval, str):
                                _add(sval, f"user:{uid}:subagent:{sname}")
                            elif isinstance(sval, dict) and sval.get("model"):
                                _add(sval.get("model"), f"user:{uid}:subagent:{sname}")

    return refs
