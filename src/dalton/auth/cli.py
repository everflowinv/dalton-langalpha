"""Server-side operator CLI for local authentication recovery and bootstrap management.

Usage::

    python -m src.dalton.auth.cli reset-password --email admin@example.com
    python -m src.dalton.auth.cli grant-admin --email member@example.com
    python -m src.dalton.auth.cli reissue-bootstrap-token

Opens and closes the shared PostgreSQL async connection pool inside its own event loop
so every command runs cleanly as a standalone process.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.auth.bootstrap import reissue_bootstrap_token
from src.dalton.auth.passwords import hash_password_async, validate_password_policy
from src.dalton.auth.roles import set_user_role
from src.dalton.auth.sessions import (
    account_advisory_lock_key,
    account_attempt_key_hash,
    hash_attempt_key,
    revoke_all_user_sessions,
)
from src.dalton.company_identity import (
    COMPANY_SYSTEM_EMAIL,
    is_company_system_user,
)
from src.server.database.pool import get_db_connection, get_or_create_pool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@asynccontextmanager
async def _cli_db_pool():
    """Open the shared conversation DB pool on entry and close it cleanly on exit."""
    pool = get_or_create_pool()
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _prompt_new_password(
    prompt_fn: Callable[[str], str] | None = None,
    *,
    password_stdin: bool = False,
    stdin: object = None,
) -> str:
    """Read and validate a new password from stdin (``--password-stdin``) or interactive TTY."""
    in_stream = sys.stdin if stdin is None else stdin
    if password_stdin:
        raw_line = in_stream.readline()  # type: ignore[attr-defined]
        password = raw_line.rstrip("\r\n") if isinstance(raw_line, str) else ""
        if not password:
            raise ValueError("No password received on standard input")
        validate_password_policy(password)
        return password

    if prompt_fn is None:
        isatty = getattr(in_stream, "isatty", None)
        if not callable(isatty) or not isatty():
            raise ValueError(
                "Standard input is not a TTY; pass --password-stdin to read password from stdin"
            )
        prompt_fn = getpass.getpass

    first = prompt_fn("New password: ")
    validate_password_policy(first)
    second = prompt_fn("Confirm new password: ")
    if first != second:
        raise ValueError("Passwords do not match")
    return first


async def reset_password_for_email(
    email: str,
    new_password: str,
    *,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Reset password for ``email`` under the per-account lock and revoke ALL active sessions.

    Returns ``(user_id, revoked_sessions_count)``.
    """
    clean_email = str(email or "").strip().lower()
    if not clean_email:
        raise ValueError("Email is required")
    if clean_email == COMPANY_SYSTEM_EMAIL:
        raise ValueError("Cannot reset password for the company system identity")
    validate_password_policy(new_password)

    ts = now or _utcnow()
    new_hash = await hash_password_async(new_password)

    # Locate user_id first without holding row locks so the write transaction acquires
    # locks strictly in unified order: account advisory lock -> credentials row -> attempts row.
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, email, username
                FROM dalton_local_credentials
                WHERE email = %s
                """,
                (clean_email,),
            )
            cred = await cur.fetchone()
    if cred is None:
        raise LookupError(f"No local account found for email {clean_email!r}")
    user_id = str(cred["user_id"]).strip()
    if is_company_system_user(user_id):
        raise ValueError("Cannot reset password for the company system identity")

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # 1. Per-account advisory lock (same order as login / change-password / rate-limit).
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (account_advisory_lock_key(user_id),),
                )
                # 2. Lock credentials row BEFORE rate-limit attempts row.
                await cur.execute(
                    """
                    SELECT user_id, email, username
                    FROM dalton_local_credentials
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                )
                locked_cred = await cur.fetchone()
                if locked_cred is None:
                    raise LookupError(f"No local account found for email {clean_email!r}")
                await cur.execute(
                    """
                    UPDATE dalton_local_credentials
                    SET password_hash = %s,
                        password_changed_at = %s,
                        failed_attempts = 0,
                        locked_until = NULL,
                        updated_at = %s
                    WHERE user_id = %s
                    """,
                    (new_hash, ts, ts, user_id),
                )
                # 3. Clear account rate-limit rows AFTER credentials row lock.
                keys = [
                    account_attempt_key_hash(user_id=user_id),
                    hash_attempt_key(clean_email),
                ]
                if locked_cred.get("username"):
                    keys.append(hash_attempt_key(str(locked_cred["username"])))
                for key_hash in keys:
                    await cur.execute(
                        """
                        DELETE FROM dalton_auth_attempts
                        WHERE dimension = 'account' AND key_hash = %s
                        """,
                        (key_hash,),
                    )
            revoked = await revoke_all_user_sessions(
                user_id, reason="admin_reset", conn=conn, now=ts
            )
    return user_id, revoked


async def grant_admin_for_email(email: str) -> str:
    """Grant ``admin`` role in ``dalton_user_roles`` to the user with ``email``."""
    clean_email = str(email or "").strip().lower()
    if not clean_email:
        raise ValueError("Email is required")
    if clean_email == COMPANY_SYSTEM_EMAIL:
        raise ValueError("Cannot grant admin to the company system identity")

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT user_id FROM dalton_local_credentials WHERE email = %s LIMIT 1",
                    (clean_email,),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        "SELECT user_id FROM users WHERE LOWER(email) = %s LIMIT 1",
                        (clean_email,),
                    )
                    row = await cur.fetchone()
                if row is None or not row.get("user_id"):
                    raise LookupError(f"No user found for email {clean_email!r}")
                user_id = str(row["user_id"]).strip()
                if is_company_system_user(user_id):
                    raise ValueError("Cannot grant admin to the company system identity")

            await set_user_role(user_id, "admin", granted_by="cli", conn=conn)
    return user_id


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.auth.cli",
        description="Server-side local authentication operator commands.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_reset = sub.add_parser(
        "reset-password",
        help="Set a new password for a local user and revoke all sessions.",
    )
    p_reset.add_argument("--email", required=True, help="Account email address")
    p_reset.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the new password from a single line on stdin instead of prompting a TTY.",
    )

    p_grant = sub.add_parser(
        "grant-admin",
        help="Grant the admin role in dalton_user_roles to the user with --email.",
    )
    p_grant.add_argument("--email", required=True, help="Account email address")

    sub.add_parser(
        "reissue-bootstrap-token",
        help="Reissue a 24h bootstrap token and reset failure count (only before bootstrap completes).",
    )
    return parser


async def _run_command(
    args: argparse.Namespace,
    *,
    prompt_fn: Callable[[str], str] | None = None,
    stdin: object = None,
    out: object = None,
    err: object = None,
    manage_pool: bool = True,
) -> int:
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err

    async def _dispatch() -> int:
        if args.command == "reset-password":
            new_password = _prompt_new_password(
                prompt_fn=prompt_fn,
                password_stdin=bool(getattr(args, "password_stdin", False)),
                stdin=stdin,
            )
            user_id, revoked = await reset_password_for_email(args.email, new_password)
            print(
                f"Password reset for {args.email.strip().lower()} "
                f"(user_id={user_id}); revoked {revoked} active session(s).",
                file=stdout,  # type: ignore[arg-type]
            )
            return 0

        if args.command == "grant-admin":
            user_id = await grant_admin_for_email(args.email)
            print(
                f"Granted admin role to {args.email.strip().lower()} (user_id={user_id}).",
                file=stdout,  # type: ignore[arg-type]
            )
            return 0

        if args.command == "reissue-bootstrap-token":
            token, expires_at = await reissue_bootstrap_token()
            print(
                f"BOOTSTRAP_SETUP_TOKEN={token}\n"
                f"Expires at (UTC): {expires_at.isoformat()}\n"
                "Failed bootstrap attempts reset to 0.",
                file=stdout,  # type: ignore[arg-type]
            )
            return 0

        print(f"Error: unknown command {args.command!r}", file=stderr)  # type: ignore[arg-type]
        return 2

    try:
        if manage_pool:
            async with _cli_db_pool():
                return await _dispatch()
        return await _dispatch()
    except HTTPException as exc:
        print(f"Error ({exc.status_code}): {exc.detail}", file=stderr)  # type: ignore[arg-type]
        return 1
    except (ValueError, LookupError) as exc:
        print(f"Error: {exc}", file=stderr)  # type: ignore[arg-type]
        return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt_fn: Callable[[str], str] | None = None,
    stdin: object = None,
    out: object = None,
    err: object = None,
    manage_pool: bool = True,
) -> int:
    """CLI entry point returning an integer exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(
        _run_command(
            args,
            prompt_fn=prompt_fn,
            stdin=stdin,
            out=out,
            err=err,
            manage_pool=manage_pool,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
