"""Collision-free object-storage user-id mapping for Dalton system owners.

Kept strictly import-free (stdlib only at module import time) so
:mod:`src.server.database.blob_keys` and operator scripts under ``scripts/ops/``
never transitively pull in ``database.pool`` or ``load_dotenv()``.
"""

from __future__ import annotations

# Matches src.dalton.company_identity.COMPANY_SYSTEM_USER_ID (verified by unit test).
COMPANY_SYSTEM_USER_ID = "system:dalton-company"
COMPANY_SYSTEM_STORAGE_USER_ID = "sys_dalton-company"
RESERVED_STORAGE_USER_PREFIX = "sys_"


def storage_user_id(user_id: str) -> str:
    """Map an application ``user_id`` to its object-storage key segment.

    - ``system:dalton-company`` maps to ``sys_dalton-company`` (valid under
      ``blob_keys._USER_ID_RE``, ``memo_binary_storage._USER_ID_RE``, and
      ``skill_archive_storage._USER_ID_RE``).
    - Inputs already beginning with the reserved ``sys_`` namespace raise
      :class:`src.server.database.blob_keys.BlobError` to guarantee zero
      collision with mapped system identities.
    - All other values are returned unchanged; database columns continue to
      store the original ``user_id``.
    """
    if not isinstance(user_id, str):
        return user_id
    if user_id == COMPANY_SYSTEM_USER_ID:
        return COMPANY_SYSTEM_STORAGE_USER_ID
    if user_id.startswith(RESERVED_STORAGE_USER_PREFIX):
        from src.server.database.blob_keys import BlobError

        raise BlobError(
            f"Refusing reserved storage user namespace for user_id: {user_id!r}"
        )
    return user_id
