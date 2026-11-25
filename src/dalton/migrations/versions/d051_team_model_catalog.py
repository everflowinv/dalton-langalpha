"""Dalton d051: team model catalog, revision history, variant credentials & price alerts.

Creates the four Dalton-owned tables specified in
``docs/dalton/features/team-model-catalog.md`` §2.1:

- ``dalton_team_model_catalog``: singleton current catalog payload & legacy
  native-sync import state, seeded idempotently with ``revision = 0``.
- ``dalton_team_model_catalog_revisions``: transactional revision audit trail
  and rollback history (including ``side_effects`` CAS version pairs), seeded
  idempotently with ``revision = 0`` (``action = 'init_empty'``).
- ``dalton_catalog_provider_credentials``: write-only encrypted credentials
  for catalog-defined provider variants, strictly isolated from catalog JSON
  payloads and bound to the full normalized ``canonical_bound_url``.
- ``dalton_model_price_alerts``: third-party public price-change alert ledger
  (populated in Batch 3).

Revision ID: d051
Revises: d046
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from alembic import op

revision: str = "d051"
down_revision: str | None = "d046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPTY_PAYLOAD = {"schema": "dalton-team-model-catalog/1"}
_EMPTY_PAYLOAD_JSON = json.dumps(_EMPTY_PAYLOAD, sort_keys=True, separators=(",", ":"))
_EMPTY_PAYLOAD_SHA256 = hashlib.sha256(_EMPTY_PAYLOAD_JSON.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_team_model_catalog (
            singleton_id               SMALLINT PRIMARY KEY DEFAULT 1 CHECK (singleton_id = 1),
            revision                   BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0),
            schema_version             INTEGER NOT NULL DEFAULT 1,
            payload                    JSONB NOT NULL DEFAULT '{"schema":"dalton-team-model-catalog/1"}'::jsonb,
            payload_sha256             TEXT NOT NULL,
            legacy_sync_import_status  TEXT NOT NULL DEFAULT 'pending'
                                       CHECK (legacy_sync_import_status IN (
                                           'pending', 'not_applicable', 'imported', 'failed', 'abandoned'
                                       )),
            legacy_sync_imported_at    TIMESTAMPTZ NULL,
            legacy_sync_import_error   TEXT NULL,
            last_known_legacy_retired  TEXT[] NOT NULL DEFAULT '{}',
            updated_by                 TEXT NOT NULL DEFAULT 'system:migration',
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_team_model_catalog_revisions (
            revision        BIGINT PRIMARY KEY CHECK (revision >= 0),
            action          TEXT NOT NULL CHECK (action IN (
                                'init_empty', 'bootstrap_import', 'abandon_legacy_file',
                                'create_model', 'update_model', 'hide_model', 'unhide_model',
                                'retire_model', 'restore_model', 'native_sync', 'native_retire',
                                'native_restore', 'upsert_provider_variant', 'update_curated',
                                'confirm_price', 'rollback'
                            )),
            target_keys     TEXT[] NOT NULL DEFAULT '{}',
            payload         JSONB NOT NULL,
            payload_sha256  TEXT NOT NULL,
            diff_summary    JSONB NOT NULL DEFAULT '{}'::jsonb,
            side_effects    JSONB NOT NULL DEFAULT '[]'::jsonb,
            comment         TEXT NULL,
            restored_from   BIGINT NULL REFERENCES dalton_team_model_catalog_revisions(revision),
            actor_user_id   TEXT NOT NULL,
            actor_label     TEXT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_team_catalog_revisions_created
            ON dalton_team_model_catalog_revisions (created_at DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_catalog_provider_credentials (
            provider_slug        TEXT PRIMARY KEY,
            encrypted_api_key    BYTEA NOT NULL,
            bound_base_url       TEXT NOT NULL,
            canonical_bound_url  TEXT NOT NULL,
            key_fingerprint      TEXT NOT NULL,
            updated_by           TEXT NOT NULL,
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS dalton_model_price_alerts (
            alert_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            model_key             TEXT NOT NULL,
            provider              TEXT NOT NULL,
            model_id              TEXT NOT NULL,
            card_target           TEXT NOT NULL CHECK (card_target IN ('cash', 'notional')),
            alert_kind            TEXT NOT NULL CHECK (alert_kind IN ('public_price_listed', 'public_price_changed')),
            match_confidence      TEXT NOT NULL CHECK (match_confidence IN ('exact_route', 'reference_only')),
            base_catalog_revision BIGINT NOT NULL,
            current_pricing       JSONB NOT NULL,
            detected_pricing      JSONB NOT NULL,
            source_name           TEXT NOT NULL CHECK (source_name IN ('openrouter', 'litellm')),
            source_url            TEXT NULL,
            fingerprint           TEXT NOT NULL UNIQUE,
            status                TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'applied', 'dismissed')),
            applied_revision      BIGINT NULL REFERENCES dalton_team_model_catalog_revisions(revision),
            detected_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_by           TEXT NULL,
            resolved_at           TIMESTAMPTZ NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_dalton_price_alerts_status
            ON dalton_model_price_alerts (status, detected_at DESC)
        """
    )

    op.execute(
        f"""
        INSERT INTO dalton_team_model_catalog (
            singleton_id,
            revision,
            schema_version,
            payload,
            payload_sha256,
            legacy_sync_import_status,
            updated_by
        )
        VALUES (
            1,
            0,
            1,
            '{_EMPTY_PAYLOAD_JSON}'::jsonb,
            '{_EMPTY_PAYLOAD_SHA256}',
            'pending',
            'system:migration'
        )
        ON CONFLICT (singleton_id) DO NOTHING
        """
    )
    op.execute(
        f"""
        INSERT INTO dalton_team_model_catalog_revisions (
            revision,
            action,
            target_keys,
            payload,
            payload_sha256,
            diff_summary,
            side_effects,
            comment,
            actor_user_id,
            actor_label
        )
        VALUES (
            0,
            'init_empty',
            '{{}}'::text[],
            '{_EMPTY_PAYLOAD_JSON}'::jsonb,
            '{_EMPTY_PAYLOAD_SHA256}',
            '{{}}'::jsonb,
            '[]'::jsonb,
            'Initial empty team model catalog baseline',
            'system:migration',
            'Migration'
        )
        ON CONFLICT (revision) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS dalton_model_price_alerts")
    op.execute("DROP TABLE IF EXISTS dalton_catalog_provider_credentials")
    op.execute("DROP TABLE IF EXISTS dalton_team_model_catalog_revisions")
    op.execute("DROP TABLE IF EXISTS dalton_team_model_catalog")
