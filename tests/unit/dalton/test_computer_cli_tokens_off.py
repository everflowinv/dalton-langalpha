"""sync3 B9: the ``dalton.computer_cli_tokens.enabled`` switch (default off).

The three ``computer-cli-tokens-off`` guards, each proven both ways — off
refuses/touches nothing, on behaves as upstream built it — plus the surfaces
the B9 card names: no ``computer_cli_calls`` row beside the 401, forged token
kinds under ``DALTON_AUTH_MODE=supabase``, and the log-redaction of the new
token shapes.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, create_autospec

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from src.server.auth import computer_token_core as core
from src.server.auth import oauth_resource, pats
from src.server.auth.jwt_bearer import AuthInfo
from src.server.database import computer_cli_calls, computer_cli_tokens
from src.server.dependencies.principal import (
    RunPrincipal,
    WorkspaceWritePrincipal,
)
from src.server.services import cli_tokens
from src.server.services.cli_calls import CliCallRecorder
from src.server.services.cli_token_sweep import CliTokenSweeper
from src.server.services.cli_tokens import CliIdentity
from tests.unit.server.auth.tokens import (
    APP_KID,
    APP_KEY,
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    SESSION_KEY,
    StaticKeys,
    public_jwk,
)

COMPUTER = "c0ffee00-0000-4000-8000-000000000001"
USER = "user-cli-token-b9"
SANDBOX = "sandbox-cli-b9"
TOKEN = f"lac1.{COMPUTER}.secret"


def _off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", raising=False)


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED", "true")


# -- the switch plumbing ------------------------------------------------------


def test_the_committed_dalton_yaml_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag Dalton ships: false in src/dalton/dalton.yaml (sync3 B9)."""
    from src.dalton.settings import DaltonConfig, get_dalton_config

    _off(monkeypatch)
    assert get_dalton_config().computer_cli_tokens.enabled is False
    assert DaltonConfig().computer_cli_tokens.enabled is False


def test_the_env_override_turns_the_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.dalton.settings import is_computer_cli_tokens_enabled

    _on(monkeypatch)
    assert is_computer_cli_tokens_enabled() is True
    _off(monkeypatch)
    assert is_computer_cli_tokens_enabled() is False


# -- guard 1: cli_tokens.keep() ------------------------------------------------


async def _write(payload: dict) -> bool:
    raise AssertionError("the write callback must not run while the switch is off")


@pytest.mark.asyncio
async def test_keep_off_returns_false_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acquire/start/reconnect reach the sandbox only through keep(); off, it
    writes no token file and reads no database before returning False."""
    _off(monkeypatch)
    db = create_autospec(computer_cli_tokens)
    monkeypatch.setattr(cli_tokens, "db", db)

    assert await cli_tokens.keep(COMPUTER, USER, SANDBOX, "daytona", _write) is False

    db.current.assert_not_awaited()
    db.save_token.assert_not_awaited()
    db.mark_held.assert_not_awaited()


@pytest.mark.asyncio
async def test_keep_on_writes_as_upstream_built_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the guard: the same call, switch on, mints and writes."""
    _on(monkeypatch)
    db = create_autospec(computer_cli_tokens)
    db.current.return_value = computer_cli_tokens.TokenRow()
    db.save_token.return_value = True
    monkeypatch.setattr(cli_tokens, "db", db)
    monkeypatch.setattr(cli_tokens, "_client", lambda: None)
    monkeypatch.setattr(core, "_DROP_RETRY_S", 0)

    async def drop_mirror(kind, computer_id):
        return True

    monkeypatch.setattr(core, "drop_mirror", drop_mirror)
    monkeypatch.setattr(
        "src.server.services.egress.reachability.effective_relay_base_url",
        lambda provider: "http://backend.internal:8000/",
    )

    written: list[dict] = []

    async def write(payload: dict) -> bool:
        written.append(payload)
        return True

    assert await cli_tokens.keep(COMPUTER, USER, SANDBOX, "daytona", write) is True
    assert written and written[0]["token"].startswith(f"lac1.{COMPUTER}.")
    db.save_token.assert_awaited_once()


# -- guard 2: the sweep --------------------------------------------------------


@pytest.mark.asyncio
async def test_start_off_creates_no_loop_task(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _off(monkeypatch)
    sweeper = CliTokenSweeper()

    with caplog.at_level(logging.INFO, logger="src.server.services.cli_token_sweep"):
        sweeper.start()

    assert sweeper._loop_task is None
    assert "not started" in caplog.text


@pytest.mark.asyncio
async def test_sweep_once_off_neither_prunes_nor_claims_across_two_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claim_due` stamps ``attempted_at`` and ``_prune_calls`` writes
    ``computer_cli_calls``; off, neither runs — checked twice, once per cycle."""
    _off(monkeypatch)
    claim_due = create_autospec(computer_cli_tokens.claim_due)
    prune = create_autospec(computer_cli_calls.prune)
    monkeypatch.setattr(computer_cli_tokens, "claim_due", claim_due)
    monkeypatch.setattr(computer_cli_calls, "prune", prune)
    sweeper = CliTokenSweeper()

    assert await sweeper.sweep_once() == 0
    assert await sweeper.sweep_once() == 0

    claim_due.assert_not_awaited()
    prune.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_once_on_claims_as_upstream_built_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the guard: switch on, the same pass claims its rows."""
    _on(monkeypatch)
    claim_due = create_autospec(computer_cli_tokens.claim_due, return_value=[])
    prune = create_autospec(computer_cli_calls.prune)
    monkeypatch.setattr(computer_cli_tokens, "claim_due", claim_due)
    monkeypatch.setattr(computer_cli_calls, "prune", prune)
    sweeper = CliTokenSweeper()

    assert await sweeper.sweep_once() == 0

    claim_due.assert_awaited_once()
    prune.assert_awaited_once()


# -- guard 3: get_principal's lac1 branch --------------------------------------


def _guard_app(reached: dict) -> FastAPI:
    """Two probes wired with the exact dependency objects the real routes use:
    ``RunPrincipal`` (agent:run — the messaging send) and
    ``WorkspaceWritePrincipal`` (workspace:write — the automations writes)."""
    app = FastAPI()

    @app.post("/api/v1/threads/probe-send")
    async def run_probe(principal: RunPrincipal):
        reached["run"] = principal
        return {"ok": True}

    @app.post("/api/v1/automations/probe-write")
    async def write_probe(principal: WorkspaceWritePrincipal):
        reached["write"] = principal
        return {"ok": True}

    app.add_middleware(CliCallRecorder)
    return app


def _valid_when_checked(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The token is valid: authenticate would answer its computer and user."""
    authenticate = create_autospec(cli_tokens.authenticate)
    authenticate.return_value = CliIdentity(COMPUTER, USER, cli_tokens.SCOPES)
    monkeypatch.setattr(cli_tokens, "authenticate", authenticate)
    return authenticate


@pytest.fixture
def _no_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.server.utils import api as api_utils

    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", "")


def test_a_valid_cli_token_is_refused_on_run_and_workspace_write_routes(
    monkeypatch: pytest.MonkeyPatch, _no_service_token
) -> None:
    """The B9 card's live-token case: minted while on, presented while off.
    Both scoped routes answer 401, the handlers never run (so no run row, no
    automation row, no outbound message can exist), the verifier is never
    reached, and no ``computer_cli_calls`` row is written beside the refusal."""
    _off(monkeypatch)
    authenticate = _valid_when_checked(monkeypatch)
    record = create_autospec(computer_cli_calls.record)
    monkeypatch.setattr(computer_cli_calls, "record", record)
    reached: dict = {}
    client = TestClient(_guard_app(reached))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    run_resp = client.post("/api/v1/threads/probe-send", headers=headers)
    write_resp = client.post("/api/v1/automations/probe-write", headers=headers)

    assert run_resp.status_code == 401
    assert write_resp.status_code == 401
    assert run_resp.headers.get("WWW-Authenticate") == 'Bearer error="invalid_token"'
    assert reached == {}
    authenticate.assert_not_awaited()
    record.assert_not_awaited()


def test_the_same_valid_token_is_served_while_the_switch_is_on(
    monkeypatch: pytest.MonkeyPatch, _no_service_token
) -> None:
    """Control for the guard: nothing about the harness refuses the token;
    with the switch on both routes answer 200 and the handler ran."""
    _on(monkeypatch)
    _valid_when_checked(monkeypatch)
    reached: dict = {}
    client = TestClient(_guard_app(reached))
    headers = {"Authorization": f"Bearer {TOKEN}"}

    assert client.post("/api/v1/threads/probe-send", headers=headers).status_code == 200
    assert client.post("/api/v1/automations/probe-write", headers=headers).status_code == 200
    assert set(reached) == {"run", "write"}


# -- forged token kinds under DALTON_AUTH_MODE=supabase ------------------------


def _forged_app_token() -> str:
    """Names the resource issuer (so ``from_resource_issuer`` routes it to the
    app verifier) but signed by a key the server's JWKS does not hold."""
    import time

    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": USER,
        "client_id": "https://app.example.test/client.json",
        "scope": "agent:run",
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(claims, SESSION_KEY, algorithm="RS256", headers={"kid": APP_KID})


def test_forged_pat_oauth_and_cli_tokens_are_401_in_supabase_mode(
    monkeypatch: pytest.MonkeyPatch, _no_service_token
) -> None:
    """Under DALTON_AUTH_MODE=supabase no token kind falls through to the
    session check (which in oss-without-supabase would answer the local dev
    user, utils/api.py): a PAT is refused by the platform gate, a forged
    resource-issuer token by its verifier, a computer token by the B9 guard."""
    from src.config import settings as app_settings
    from src.server.utils import api as api_utils

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setattr(pats, "AUTH_SERVICE_URL", "")
    monkeypatch.setattr(oauth_resource, "OAUTH_RESOURCE_ISSUER", ISSUER)
    monkeypatch.setattr(oauth_resource, "OAUTH_RESOURCE_JWKS_URL", JWKS_URL)
    monkeypatch.setattr(oauth_resource, "OAUTH_RESOURCE_AUDIENCES", (AUDIENCE,))
    monkeypatch.setattr(
        oauth_resource, "_signing_keys", StaticKeys(public_jwk(APP_KEY, APP_KID))
    )
    _off(monkeypatch)
    _valid_when_checked(monkeypatch)

    reached: dict = {}
    client = TestClient(_guard_app(reached))

    for label, token in (
        ("pat", "la_pat_" + "s" * 24),
        ("forged oauth", _forged_app_token()),
        ("cli", TOKEN),
    ):
        resp = client.post(
            "/api/v1/threads/probe-send", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401, (label, resp.status_code, resp.text)
    assert reached == {}


def _make_request() -> Request:
    req = create_autospec(Request, instance=True)
    req.__len__.return_value = 1
    req.headers = Headers({})
    return req


@pytest.mark.asyncio
async def test_a_session_user_outside_the_allowlist_is_refused_in_supabase_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The session branch of the same principal path keeps its Dalton
    allowlist: a decoded token whose email is not whitelisted never becomes a
    user id, so nothing the scoped routes guard is reachable through it."""
    from src.config import settings as app_settings
    from src.dalton import auth as dalton_auth
    from src.server.auth import jwt_bearer
    from src.server.utils import api as api_utils
    from src.server.utils.api import get_current_user_id

    monkeypatch.setattr(app_settings, "HOST_MODE", "oss")
    monkeypatch.setattr(api_utils, "HOST_MODE", "oss")
    monkeypatch.setattr(jwt_bearer, "HOST_MODE", "oss")
    monkeypatch.setattr(app_settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("DALTON_AUTH_MODE", "supabase")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_unit_test_key")
    monkeypatch.setenv("DALTON_ALLOWED_EMAILS", "analyst1@dalton.org")
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(dalton_auth, "_auth_settings_cache", None)
    fetch = create_autospec(dalton_auth._fetch_supabase_auth_settings)
    fetch.return_value = {
        "disable_signup": True,
        "mailer_autoconfirm": False,
        "external": {"email": True, "anonymous_users": False},
    }
    monkeypatch.setattr(dalton_auth, "_fetch_supabase_auth_settings", fetch)

    raw = jwt.encode(
        {"sub": "user-intruder-b9", "aud": "authenticated", "email": "intruder@evil.test"},
        "unit-test-secret-key-at-least-32-bytes!",
        algorithm="HS256",
    )
    decode = create_autospec(jwt_bearer._decode_token)
    decode.return_value = AuthInfo(user_id="user-intruder-b9")
    monkeypatch.setattr(jwt_bearer, "_decode_token", decode)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=raw)
    with pytest.raises(Exception) as caught:
        await get_current_user_id(request=_make_request(), credentials=creds)
    assert getattr(caught.value, "status_code", None) == 403


# -- what a token-shaped line looks like after redaction ----------------------


def test_cli_token_and_pat_shapes_are_redacted_by_the_export_stack() -> None:
    """The B9 log-redaction surface: a bare or bearer computer token, a PAT,
    and the grant URL `la login` builds (authorization endpoint + code) never
    survive ``redact_text`` in plaintext."""
    from scripts.dalton.log_redaction import redact_text

    secret = "s" * 43
    pat = "la_pat_" + "p" * 30
    code = "c" * 24
    verifier = "v" * 40
    raw = (
        f"Authorization: Bearer lac1.{COMPUTER}.{secret} "
        f"token=lac1.{COMPUTER}.{secret} "
        f"pat={pat} "
        "GET https://issuer.example.test/oauth2/auth"
        f"?response_type=code&client_id=app&code_verifier={verifier}&code={code} failed"
    )
    redacted = redact_text(raw)
    assert f"lac1.{COMPUTER}" not in redacted, redacted
    assert secret not in redacted, redacted
    assert pat not in redacted, redacted
    assert code not in redacted, redacted
    assert verifier not in redacted, redacted


def test_lfs1_mount_tokens_are_redacted_too() -> None:
    from scripts.dalton.log_redaction import redact_text

    secret = "m" * 43
    redacted = redact_text(f"mirror held lfs1.{COMPUTER}.{secret} past expiry")
    assert secret not in redacted, redacted
    assert f"lfs1.{COMPUTER}" not in redacted, redacted
