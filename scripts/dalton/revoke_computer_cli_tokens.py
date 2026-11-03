#!/usr/bin/env python3
"""One-shot full-table revocation of computer CLI tokens (sync3 B9, §11.4).

Run when ``dalton.computer_cli_tokens.enabled`` flips from on to off: deletes
every ``computer_cli_tokens`` row in one transaction, which is the same
revocation a computer's stop or delete performs per computer (upstream
``cli_tokens.revoke`` → ``delete_token``). Both the current and the previous
token of each computer die with the row. The Redis admission mirror is left
to lapse on its own (``MIRROR_TTL``, 5 minutes), so run this after the switch
is already off — ``get_principal`` then refuses every CLI token before the
mirror is read — and keep it off for at least 5 minutes afterwards. Every row goes, live or lapsed — a lapsed row's
computer would only ever get a fresh token with the switch back on, and an
empty table is what "all tokens revoked" reads as afterwards.

Without ``--yes`` the script only prints what it would do and exits 1 to
remind the operator to confirm. The affected-row assertion re-reads the table
inside the same transaction: the DELETE must have cleared every row the
in-transaction count saw.

Usage:

    uv run python scripts/dalton/revoke_computer_cli_tokens.py           # dry run
    uv run python scripts/dalton/revoke_computer_cli_tokens.py --yes     # revoke
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row

# What the operator is really revoking: rows whose current or previous token
# can still authenticate. Reported in the dry run beside the total row count.
LIVE_SQL = """
SELECT t.computer_id, t.user_id, t.expires_at, t.prev_expires_at
FROM computer_cli_tokens t
WHERE (t.token_sha256 IS NOT NULL AND t.expires_at > NOW())
   OR (t.prev_token_sha256 IS NOT NULL AND t.prev_expires_at > NOW())
ORDER BY t.computer_id
"""

COUNT_SQL = "SELECT count(*) AS n FROM computer_cli_tokens"
DELETE_SQL = "DELETE FROM computer_cli_tokens"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Revoke every computer CLI token (computer_cli_tokens) in one transaction."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL URL (defaults to the DATABASE_URL environment variable).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete; without it the script only reports what it would do.",
    )
    args = parser.parse_args(argv)

    url = (args.database_url or os.environ.get("DATABASE_URL", "")).strip()
    if not url:
        print("error: DATABASE_URL is not set (or pass --database-url).", file=sys.stderr)
        return 2

    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(LIVE_SQL)
            live = cur.fetchall()
        if not args.yes:
            with conn.cursor() as cur:
                cur.execute(COUNT_SQL)
                total = cur.fetchone()["n"]
            print(f"computer_cli_tokens rows with a still-live token: {len(live)} (of {total} total)")
            for row in live:
                print(
                    f"  computer={row['computer_id']} user={row['user_id']} "
                    f"expires_at={row['expires_at']} prev_expires_at={row['prev_expires_at']}"
                )
            print("Dry run: nothing deleted. Re-run with --yes to revoke.")
            return 1

        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(COUNT_SQL)
                expected = cur.fetchone()["n"]
                cur.execute(DELETE_SQL)
                deleted = cur.rowcount
                cur.execute(COUNT_SQL)
                remaining = cur.fetchone()["n"]
                if deleted != expected or remaining != 0:
                    raise RuntimeError(
                        "revocation assertion failed: "
                        f"expected {expected} rows, DELETE reported {deleted}, "
                        f"{remaining} remain — transaction rolled back"
                    )
        print(f"revoked {deleted} computer_cli_tokens row(s) ({len(live)} held a live token); table is empty.")

    # The sandbox token files stay behind by design: the third
    # computer-cli-tokens-off guard refuses what they hold. Name the mirror
    # TTL so "it still worked for just under an hour" surprises nobody.
    print(
        "Sandbox LANGALPHA_TOKEN_FILE copies stay on disk but authenticate nothing "
        "while the switch is off; the admission mirror lapses within the hour."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
