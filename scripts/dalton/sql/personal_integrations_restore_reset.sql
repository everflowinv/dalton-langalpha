-- Shared pre-startup reset for personal integrations after PostgreSQL restore.
--
-- Run after Alembic migrations complete and before starting backend services
-- or schedulers. Clears stored OAuth token ciphertexts (marking existing rows
-- as needs_reauth), pending remote-revocation token ciphertexts, one-time
-- OAuth state rows, and Supabase post-callback confirmation staging rows.
-- Safe on databases where personal-integration tables are not yet present.

BEGIN;

DO $$
BEGIN
    IF to_regclass('public.dalton_user_integrations') IS NOT NULL THEN
        UPDATE dalton_user_integrations
        SET access_token = NULL,
            refresh_token = NULL,
            refresh_inflight_gen = NULL,
            refresh_started_at = NULL,
            status = 'needs_reauth',
            needs_reauth_at = COALESCE(needs_reauth_at, NOW()),
            updated_at = NOW();
    END IF;

    IF to_regclass('public.dalton_integration_purge_ledger') IS NOT NULL THEN
        UPDATE dalton_integration_purge_ledger
        SET encrypted_revoke_token = NULL
        WHERE encrypted_revoke_token IS NOT NULL;
    END IF;

    IF to_regclass('public.dalton_user_integration_oauth_states') IS NOT NULL THEN
        DELETE FROM dalton_user_integration_oauth_states;
    END IF;

    IF to_regclass('public.dalton_user_integration_pending_confirmations') IS NOT NULL THEN
        DELETE FROM dalton_user_integration_pending_confirmations;
    END IF;
END
$$;

COMMIT;
