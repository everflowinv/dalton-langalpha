"""Shared PostgreSQL test-database guard for Dalton integration tests.

Uses ``psycopg.conninfo.conninfo_to_dict`` (libpq's real DSN parser) so query
parameters like ``?dbname=prod``, keyword/value DSNs, or ``service=`` entries
cannot bypass URL-path checks and hit a non-test database.
"""

from __future__ import annotations

from psycopg.conninfo import conninfo_to_dict

ALL_DALTON_DSN_ENV_VARS: tuple[str, ...] = (
    "TEST_DB_DSN",
    "DALTON_TEST_DB_DSN",
    "TEST_DATABASE_URL",
    "DALTON_DATA_JOBS_TEST_DSN",
    "DALTON_CHANNEL_GATEWAY_TEST_DSN",
    "DALTON_CHAT_UPLOAD_TEST_DSN",
    "DALTON_TEST_PG_DSN",
)


def extract_effective_test_db_name(dsn: str, *, source: str = "DSN") -> str:
    """Return the effective libpq ``dbname`` for ``dsn`` after verifying it ends with ``_test``.

    Raises ``ValueError`` if ``dsn`` is empty, unparseable, specifies a libpq
    ``service`` (which can redirect ``dbname`` via ``pg_service.conf``), omits
    ``dbname``, or resolves to a database name not ending with ``_test``.
    """
    raw = (dsn or "").strip()
    if not raw:
        raise ValueError(f"Refusing to run integration tests with empty DSN from {source}")
    try:
        params = conninfo_to_dict(raw)
    except Exception as exc:
        raise ValueError(
            f"Refusing to run integration tests with invalid PostgreSQL DSN from {source}: {exc}"
        ) from exc
    if params.get("service"):
        raise ValueError(
            f"Refusing to run integration tests with libpq service={params.get('service')!r} "
            f"from {source} (service entries can override dbname)"
        )
    db_name = str(params.get("dbname") or "").strip()
    if not db_name or not db_name.endswith("_test"):
        raise ValueError(
            f"Refusing to run integration tests against non-test database {db_name!r} "
            f"from {source} (must end with '_test')"
        )
    return db_name
