"""Shared stdlib-only PostgreSQL connection resolution for backend, backup, and restore.

Single source of truth shared by:
- Backend runtime (`src/server/database/pool.py`, via `# DALTON-HOOK(standalone-core)`)
- Dalton Alembic migrations (`src/dalton/migrations/env.py`)
- Standalone Core host scripts (`scripts/standalone/storage_config.py`, `backup.sh`, `restore.sh`)
- Standalone Core consistent backup CLI (`scripts/standalone/consistent_backup.py`)

Priority (identical across backend, backup, and restore):
1. Explicit ``pg_dsn`` argument (CLI ``--pg-dsn``)
2. ``DALTON_BACKUP_PG_DSN`` (only when ``allow_backup_override=True`` for internal
   host -> ``compose-backup`` container handoff)
3. Explicit ``DB_*`` environment variables (``DB_HOST``, ``DB_PORT``, ``DB_NAME``,
   ``DB_USER``, ``DB_PASSWORD``)
4. ``DATABASE_URL`` environment variable (fallback when no ``DB_*`` variable is set)
5. Defaults (``default_host``, port ``5432``, db ``postgres``, user ``postgres``,
   password ``postgres``, sslmode ``prefer``)

When ``check_conflict=True`` (used by backup and restore entrypoints before exporting
or restoring), setting BOTH ``DB_*`` and ``DATABASE_URL`` pointing to different
``(host, port, dbname, user)`` targets raises :class:`PgConfigConflictError`.

Keep this module strictly Python standard library only so Standalone Core hosts can
run config resolution with bare system ``python3`` (no ``psycopg``/``yaml``/``boto3``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlsplit, urlunsplit

DB_VAR_KEYS: tuple[str, ...] = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)


class PgConfigConflictError(ValueError):
    """Raised when both ``DB_*`` and ``DATABASE_URL`` are set and target different databases."""


def _parse_url_fields(
    dsn: str,
    *,
    default_host: str,
    default_sslmode: str,
) -> dict[str, str]:
    parsed = urlsplit(dsn)
    host = parsed.hostname or default_host
    port = str(parsed.port or "5432")
    dbname = unquote_plus((parsed.path or "").lstrip("/")) or "postgres"
    user = unquote_plus(parsed.username) if parsed.username is not None else "postgres"
    password = unquote_plus(parsed.password) if parsed.password is not None else ""
    qs = parse_qs(parsed.query)
    sslmode = (qs.get("sslmode", [None])[0] or default_sslmode).strip() or "prefer"
    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        "user": user,
        "password": password,
        "sslmode": sslmode,
    }


def _build_dsn(
    *,
    host: str,
    port: str,
    dbname: str,
    user: str,
    password: str,
    sslmode: str,
) -> str:
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{dbname}?sslmode={quote_plus(sslmode)}"
    )


def _rewrite_container_dsn(dsn: str, *, host: str, port: str, user: str, password: str) -> str:
    parsed = urlsplit(dsn)
    if parsed.scheme in ("postgres", "postgresql") and host.lower() == "postgres":
        auth = quote_plus(user)
        if parsed.password is not None:
            auth = f"{auth}:{quote_plus(password)}"
        netloc = f"{auth}@127.0.0.1:{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return dsn


def resolve_pg_config(
    pg_dsn: str = "",
    env: Mapping[str, str] | None = None,
    *,
    default_host: str = "localhost",
    default_sslmode: str | None = None,
    allow_backup_override: bool = False,
    check_conflict: bool = False,
) -> dict[str, str]:
    """Resolve unified PostgreSQL connection fields using one rule for backend, backup, and restore."""
    em = os.environ if env is None else env
    eff_default_sslmode = (
        (default_sslmode if default_sslmode is not None else (em.get("DB_SSLMODE") or "prefer")).strip()
        or "prefer"
    )

    explicit = (pg_dsn or "").strip()
    if explicit:
        fields = _parse_url_fields(
            explicit, default_host=default_host, default_sslmode=eff_default_sslmode
        )
        container_dsn = _rewrite_container_dsn(
            explicit,
            host=fields["host"],
            port=fields["port"],
            user=fields["user"],
            password=fields["password"],
        )
        return {
            "dsn": explicit,
            "container_dsn": container_dsn,
            **fields,
            "source": "pg_dsn",
        }

    if allow_backup_override:
        override_url = (em.get("DALTON_BACKUP_PG_DSN") or "").strip()
        if override_url:
            fields = _parse_url_fields(
                override_url, default_host=default_host, default_sslmode=eff_default_sslmode
            )
            container_dsn = _rewrite_container_dsn(
                override_url,
                host=fields["host"],
                port=fields["port"],
                user=fields["user"],
                password=fields["password"],
            )
            return {
                "dsn": override_url,
                "container_dsn": container_dsn,
                **fields,
                "source": "backup_pg_dsn",
            }

    has_db_vars = any((em.get(k) or "").strip() for k in DB_VAR_KEYS)
    env_url = (em.get("DATABASE_URL") or "").strip()

    if has_db_vars:
        db_host = (em.get("DB_HOST") or "").strip() or default_host
        db_port = (em.get("DB_PORT") or "").strip() or "5432"
        db_name = (em.get("DB_NAME") or "").strip() or "postgres"
        db_user = (em.get("DB_USER") or "").strip() or "postgres"
        db_password = em["DB_PASSWORD"] if "DB_PASSWORD" in em else "postgres"
        db_sslmode = (em.get("DB_SSLMODE") or "").strip() or eff_default_sslmode
        dsn = _build_dsn(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_password,
            sslmode=db_sslmode,
        )
        if check_conflict and env_url:
            url_fields = _parse_url_fields(
                env_url, default_host=default_host, default_sslmode=eff_default_sslmode
            )
            db_target = (db_host.lower(), db_port, db_name, db_user)
            url_target = (
                url_fields["host"].lower(),
                url_fields["port"],
                url_fields["dbname"],
                url_fields["user"],
            )
            if db_target != url_target:
                raise PgConfigConflictError(
                    "Conflicting PostgreSQL configuration: DB_* targets "
                    f"{db_user}@{db_host}:{db_port}/{db_name} while DATABASE_URL targets "
                    f"{url_fields['user']}@{url_fields['host']}:{url_fields['port']}/{url_fields['dbname']}. "
                    "Remove the conflicting DATABASE_URL or align DB_* and DATABASE_URL before running backup/restore."
                )
        container_dsn = _rewrite_container_dsn(
            dsn, host=db_host, port=db_port, user=db_user, password=db_password
        )
        return {
            "dsn": dsn,
            "container_dsn": container_dsn,
            "host": db_host,
            "port": db_port,
            "dbname": db_name,
            "user": db_user,
            "password": db_password,
            "sslmode": db_sslmode,
            "source": "db_vars",
        }

    if env_url:
        fields = _parse_url_fields(
            env_url, default_host=default_host, default_sslmode=eff_default_sslmode
        )
        container_dsn = _rewrite_container_dsn(
            env_url,
            host=fields["host"],
            port=fields["port"],
            user=fields["user"],
            password=fields["password"],
        )
        return {
            "dsn": env_url,
            "container_dsn": container_dsn,
            **fields,
            "source": "database_url",
        }

    db_password = em["DB_PASSWORD"] if "DB_PASSWORD" in em else "postgres"
    dsn = _build_dsn(
        host=default_host,
        port="5432",
        dbname="postgres",
        user="postgres",
        password=db_password,
        sslmode=eff_default_sslmode,
    )
    container_dsn = _rewrite_container_dsn(
        dsn, host=default_host, port="5432", user="postgres", password=db_password
    )
    return {
        "dsn": dsn,
        "container_dsn": container_dsn,
        "host": default_host,
        "port": "5432",
        "dbname": "postgres",
        "user": "postgres",
        "password": db_password,
        "sslmode": eff_default_sslmode,
        "source": "db_vars",
    }


def resolve_pg_dsn(
    pg_dsn: str = "",
    env: Mapping[str, str] | None = None,
    *,
    default_host: str = "localhost",
    default_sslmode: str | None = None,
    allow_backup_override: bool = False,
    check_conflict: bool = False,
) -> str:
    """Resolve the unified PostgreSQL DSN string."""
    return resolve_pg_config(
        pg_dsn,
        env=env,
        default_host=default_host,
        default_sslmode=default_sslmode,
        allow_backup_override=allow_backup_override,
        check_conflict=check_conflict,
    )["dsn"]
