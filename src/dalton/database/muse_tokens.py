"""Atomic conditional invalidation query for Muse API keys (`user_oauth_tokens`)."""

from __future__ import annotations

from src.server.database.oauth_tokens import get_encryption_key
from src.server.database.pool import get_db_connection

_MARK_MUSE_KEY_INVALID_SQL = """
UPDATE user_oauth_tokens
   SET account_id = 'muse-api-key:invalid', updated_at = NOW()
 WHERE user_id = %s AND provider = 'muse-oauth'
   AND pgp_sym_decrypt(access_token, %s) = %s
"""


async def mark_muse_key_invalid_if_current(cred_user_id: str, failed_api_key: str) -> bool:
    """Atomically set `account_id = 'muse-api-key:invalid'` only if `access_token` matches `failed_api_key`."""
    enc_key = get_encryption_key()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _MARK_MUSE_KEY_INVALID_SQL,
                (cred_user_id, enc_key, failed_api_key),
            )
            return cur.rowcount == 1
