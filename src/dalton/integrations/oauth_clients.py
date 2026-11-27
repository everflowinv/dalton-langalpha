"""Injectable upstream OAuth & revocation HTTP clients for Google and Feishu (§4.2, §4.3).

All external HTTP calls (Google token exchange/refresh/JWKS/revoke, Feishu token
exchange/refresh/user_info) live behind Protocols and concrete ``httpx.AsyncClient``
implementations so unit tests inject ``create_autospec`` doubles without touching
the network. Tokens, authorization codes, and client secrets are excluded from
``repr`` and error messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
import time
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
import jwt

from src.dalton.integrations import constants as integration_constants
from src.dalton.services.integration_revoke import RemoteRevoker, RevokeOutcome

logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})

FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"

# Connect 5s, read 10s per §4.2(2) step 2
_OAUTH_HTTP_TIMEOUT = httpx.Timeout(timeout=10.0, connect=5.0)
_JWKS_CACHE_TTL_SECONDS = 3600.0

UpstreamErrorKind = Literal[
    "invalid_grant",
    "revoked",
    "needs_reauth",
    "ambiguous",
    "transient",
    "client_error",
]


@dataclass(frozen=True, slots=True)
class OAuthTokenResponse:
    """Normalized token response from Google or Feishu (secret fields hidden from repr)."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None
    expires_at: datetime | None = None
    scope: str | None = None
    id_token: str | None = field(default=None, repr=False)
    token_type: str | None = None


@dataclass(frozen=True, slots=True)
class GoogleIdTokenClaims:
    """Verified Google OIDC ``id_token`` claims (§4.2(1))."""

    sub: str
    email: str | None
    email_verified: bool
    aud: str
    iss: str
    exp: int


@dataclass(frozen=True, slots=True)
class FeishuUserInfo:
    """Feishu OAuth user identity returned by ``authen/v1/user_info``."""

    open_id: str
    union_id: str | None = None
    email: str | None = None
    name: str | None = None


class OAuthUpstreamError(RuntimeError):
    """Sanitized upstream OAuth HTTP/protocol error (never embeds secrets or raw payloads)."""

    def __init__(
        self,
        *,
        provider: str,
        kind: UpstreamErrorKind,
        error_code: str,
        status_code: int | None = None,
        feishu_code: int | None = None,
    ) -> None:
        self.provider = provider
        self.kind: UpstreamErrorKind = kind
        self.error_code = error_code
        self.status_code = status_code
        self.feishu_code = feishu_code
        super().__init__(
            f"{provider} oauth error kind={kind} code={error_code} status={status_code}"
        )


class GoogleIdTokenVerificationError(ValueError):
    """Raised when Google OIDC ``id_token`` fails signature or claim validation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Google id_token verification failed: {reason}")


@runtime_checkable
class GoogleOAuthClientProtocol(RemoteRevoker, Protocol):
    async def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokenResponse: ...

    async def refresh_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> OAuthTokenResponse: ...

    async def verify_id_token(
        self,
        *,
        id_token: str,
        expected_aud: str,
    ) -> GoogleIdTokenClaims: ...

    async def revoke(
        self,
        *,
        provider: str,
        oauth_client_id: str,
        token: str,
    ) -> RevokeOutcome: ...


@runtime_checkable
class FeishuOAuthClientProtocol(Protocol):
    async def exchange_code(
        self,
        *,
        app_id: str,
        app_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokenResponse: ...

    async def refresh_token(
        self,
        *,
        app_id: str,
        app_secret: str,
        refresh_token: str,
    ) -> OAuthTokenResponse: ...

    async def get_user_info(
        self,
        *,
        access_token: str,
    ) -> FeishuUserInfo: ...


def _compute_expires_at(expires_in: Any) -> tuple[int | None, datetime | None]:
    if expires_in is None:
        return None, None
    try:
        secs = int(expires_in)
    except (TypeError, ValueError):
        return None, None
    if secs <= 0:
        return 0, datetime.now(UTC)
    return secs, datetime.now(UTC) + timedelta(seconds=secs)


def _safe_json(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


class GoogleOAuthClient:
    """Async Google OAuth 2.0, OIDC JWKS verifier, and ``RemoteRevoker`` client."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        token_url: str = GOOGLE_TOKEN_URL,
        revoke_url: str = GOOGLE_REVOKE_URL,
        jwks_url: str = GOOGLE_JWKS_URL,
    ) -> None:
        self._http_client = http_client
        self._token_url = token_url
        self._revoke_url = revoke_url
        self._jwks_url = jwks_url
        self._jwks_cache: tuple[float, dict[str, Any]] | None = None

    async def _post_form(self, url: str, data: dict[str, str]) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_OAUTH_HTTP_TIMEOUT,
            )
        async with httpx.AsyncClient(timeout=_OAUTH_HTTP_TIMEOUT) as client:
            return await client.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    async def _get_json(self, url: str) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.get(url, timeout=_OAUTH_HTTP_TIMEOUT)
        async with httpx.AsyncClient(timeout=_OAUTH_HTTP_TIMEOUT) as client:
            return await client.get(url)

    def _classify_google_error(self, resp: httpx.Response) -> OAuthUpstreamError:
        payload = _safe_json(resp) or {}
        err = str(payload.get("error") or "").strip().lower()
        status = resp.status_code
        if status in (400, 401) and err == "invalid_grant":
            return OAuthUpstreamError(
                provider="google",
                kind="invalid_grant",
                error_code="invalid_grant",
                status_code=status,
            )
        if status == 429 or status >= 500:
            return OAuthUpstreamError(
                provider="google",
                kind="transient",
                error_code=err or f"http_{status}",
                status_code=status,
            )
        return OAuthUpstreamError(
            provider="google",
            kind="client_error",
            error_code=err or f"http_{status}",
            status_code=status,
        )

    async def exchange_code(
        self,
        *,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokenResponse:
        try:
            resp = await self._post_form(
                self._token_url,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OAuthUpstreamError(
                provider="google",
                kind="transient",
                error_code=type(exc).__name__.lower(),
            ) from None

        if resp.status_code != 200:
            raise self._classify_google_error(resp)

        data = _safe_json(resp)
        if not data or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise OAuthUpstreamError(
                provider="google",
                kind="transient",
                error_code="malformed_token_response",
                status_code=resp.status_code,
            )
        expires_in, expires_at = _compute_expires_at(data.get("expires_in"))
        raw_refresh = data.get("refresh_token")
        refresh_tok = (
            raw_refresh.strip()
            if isinstance(raw_refresh, str) and raw_refresh.strip()
            else None
        )
        raw_scope = data.get("scope")
        scope = raw_scope.strip() if isinstance(raw_scope, str) and raw_scope.strip() else None
        raw_id_token = data.get("id_token")
        id_tok = (
            raw_id_token.strip()
            if isinstance(raw_id_token, str) and raw_id_token.strip()
            else None
        )
        return OAuthTokenResponse(
            access_token=data["access_token"].strip(),
            refresh_token=refresh_tok,
            expires_in=expires_in,
            expires_at=expires_at,
            scope=scope,
            id_token=id_tok,
            token_type=str(data.get("token_type") or "Bearer"),
        )

    async def refresh_token(
        self,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> OAuthTokenResponse:
        try:
            resp = await self._post_form(
                self._token_url,
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OAuthUpstreamError(
                provider="google",
                kind="transient",
                error_code=type(exc).__name__.lower(),
            ) from None

        if resp.status_code != 200:
            raise self._classify_google_error(resp)

        data = _safe_json(resp)
        if not data or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise OAuthUpstreamError(
                provider="google",
                kind="transient",
                error_code="malformed_token_response",
                status_code=resp.status_code,
            )
        expires_in, expires_at = _compute_expires_at(data.get("expires_in"))
        raw_refresh = data.get("refresh_token")
        refresh_tok = (
            raw_refresh.strip()
            if isinstance(raw_refresh, str) and raw_refresh.strip()
            else None
        )
        raw_scope = data.get("scope")
        scope = raw_scope.strip() if isinstance(raw_scope, str) and raw_scope.strip() else None
        return OAuthTokenResponse(
            access_token=data["access_token"].strip(),
            refresh_token=refresh_tok,
            expires_in=expires_in,
            expires_at=expires_at,
            scope=scope,
            id_token=None,
            token_type=str(data.get("token_type") or "Bearer"),
        )

    async def _fetch_jwks(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if (
            not force_refresh
            and self._jwks_cache is not None
            and (now - self._jwks_cache[0]) < _JWKS_CACHE_TTL_SECONDS
        ):
            return self._jwks_cache[1]
        try:
            resp = await self._get_json(self._jwks_url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise GoogleIdTokenVerificationError(
                f"jwks_fetch_failed:{type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise GoogleIdTokenVerificationError(f"jwks_http_{resp.status_code}")
        data = _safe_json(resp)
        if not data or not isinstance(data.get("keys"), list):
            raise GoogleIdTokenVerificationError("jwks_malformed")
        self._jwks_cache = (now, data)
        return data

    def _find_jwk(self, jwks: dict[str, Any], kid: str | None) -> Any | None:
        jwk_set = jwt.PyJWKSet.from_dict(jwks)
        if kid:
            for key in jwk_set.keys:
                if key.key_id == kid:
                    return key
            return None
        return jwk_set.keys[0] if len(jwk_set.keys) == 1 else None

    async def verify_id_token(
        self,
        *,
        id_token: str,
        expected_aud: str,
    ) -> GoogleIdTokenClaims:
        if not id_token or not expected_aud:
            raise GoogleIdTokenVerificationError("missing_id_token_or_aud")
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError:
            raise GoogleIdTokenVerificationError("invalid_jwt_header") from None

        alg = str(header.get("alg") or "")
        if alg not in ("RS256", "ES256"):
            raise GoogleIdTokenVerificationError("unsupported_alg")
        kid = header.get("kid")

        jwks = await self._fetch_jwks(force_refresh=False)
        jwk_key = self._find_jwk(jwks, kid)
        if jwk_key is None:
            jwks = await self._fetch_jwks(force_refresh=True)
            jwk_key = self._find_jwk(jwks, kid)
        if jwk_key is None:
            raise GoogleIdTokenVerificationError("unknown_signing_key")

        try:
            claims = jwt.decode(
                id_token,
                key=jwk_key.key,
                algorithms=[alg],
                audience=expected_aud,
                options={"require": ["sub", "iss", "aud", "exp"]},
            )
        except jwt.ExpiredSignatureError:
            raise GoogleIdTokenVerificationError("id_token_expired") from None
        except jwt.InvalidAudienceError:
            raise GoogleIdTokenVerificationError("id_token_audience_mismatch") from None
        except jwt.PyJWTError:
            raise GoogleIdTokenVerificationError("id_token_signature_invalid") from None

        iss = str(claims.get("iss") or "")
        if iss not in GOOGLE_VALID_ISSUERS:
            raise GoogleIdTokenVerificationError("id_token_issuer_invalid")

        sub = str(claims.get("sub") or "").strip()
        if not sub:
            raise GoogleIdTokenVerificationError("id_token_missing_sub")

        email_verified = bool(claims.get("email_verified"))
        raw_email = claims.get("email")
        email = (
            str(raw_email).strip().lower()
            if email_verified and isinstance(raw_email, str) and raw_email.strip()
            else None
        )
        return GoogleIdTokenClaims(
            sub=sub,
            email=email,
            email_verified=email_verified,
            aud=expected_aud,
            iss=iss,
            exp=int(claims["exp"]),
        )

    async def revoke(
        self,
        *,
        provider: str,
        oauth_client_id: str,
        token: str,
    ) -> RevokeOutcome:
        """Revoke a Google OAuth token via ``POST https://oauth2.googleapis.com/revoke`` (§4.2(3)).

        HTTP 200 or HTTP 400 ``{"error": "invalid_token"}`` counts as ``SUCCEEDED``.
        Timeouts, transport errors, HTTP 429, and HTTP 5xx return ``RETRYABLE``.
        """
        _ = (provider, oauth_client_id)
        if not token:
            return RevokeOutcome.SUCCEEDED
        try:
            resp = await self._post_form(self._revoke_url, {"token": token})
        except (httpx.TimeoutException, httpx.TransportError):
            return RevokeOutcome.RETRYABLE

        if resp.status_code == 200:
            return RevokeOutcome.SUCCEEDED
        if resp.status_code == 400:
            payload = _safe_json(resp) or {}
            if str(payload.get("error") or "").strip().lower() == "invalid_token":
                return RevokeOutcome.SUCCEEDED
            return RevokeOutcome.PERMANENT_FAILURE
        if resp.status_code == 429 or resp.status_code >= 500:
            return RevokeOutcome.RETRYABLE
        return RevokeOutcome.PERMANENT_FAILURE


class FeishuOAuthClient:
    """Async Feishu OAuth v2 token & user_info client (§3.2, §4.2(2))."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        token_url: str = FEISHU_TOKEN_URL,
        user_info_url: str = FEISHU_USER_INFO_URL,
    ) -> None:
        self._http_client = http_client
        self._token_url = token_url
        self._user_info_url = user_info_url

    async def _post_json(self, url: str, body: dict[str, Any]) -> httpx.Response:
        if self._http_client is not None:
            return await self._http_client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=_OAUTH_HTTP_TIMEOUT,
            )
        async with httpx.AsyncClient(timeout=_OAUTH_HTTP_TIMEOUT) as client:
            return await client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )

    async def _get_with_bearer(self, url: str, access_token: str) -> httpx.Response:
        headers = {"Authorization": f"Bearer {access_token}"}
        if self._http_client is not None:
            return await self._http_client.get(
                url, headers=headers, timeout=_OAUTH_HTTP_TIMEOUT
            )
        async with httpx.AsyncClient(timeout=_OAUTH_HTTP_TIMEOUT) as client:
            return await client.get(url, headers=headers)

    def _classify_feishu_response(
        self,
        resp: httpx.Response,
        *,
        is_refresh: bool,
    ) -> dict[str, Any]:
        status = resp.status_code
        if status in (502, 504):
            raise OAuthUpstreamError(
                provider="feishu",
                kind="ambiguous" if is_refresh else "transient",
                error_code=f"http_{status}",
                status_code=status,
            )
        if status == 429 or status >= 500:
            raise OAuthUpstreamError(
                provider="feishu",
                kind="transient",
                error_code=f"http_{status}",
                status_code=status,
            )

        data = _safe_json(resp)
        if data is None:
            raise OAuthUpstreamError(
                provider="feishu",
                kind="ambiguous" if is_refresh else "transient",
                error_code="malformed_response",
                status_code=status,
            )

        raw_code = data.get("code")
        code_int: int | None = None
        if raw_code is not None:
            try:
                code_int = int(raw_code)
            except (TypeError, ValueError):
                code_int = None

        err_str = str(data.get("error") or "").strip().lower()
        if status == 200 and (code_int in (None, 0)) and not err_str:
            return data

        if code_int in integration_constants.FEISHU_CONFIRMED_REVOKED_CODES:
            raise OAuthUpstreamError(
                provider="feishu",
                kind="revoked",
                error_code=f"feishu_{code_int}",
                status_code=status,
                feishu_code=code_int,
            )
        if code_int in integration_constants.FEISHU_NEEDS_REAUTH_CODES or err_str == "invalid_grant":
            raise OAuthUpstreamError(
                provider="feishu",
                kind="needs_reauth",
                error_code=f"feishu_{code_int}" if code_int is not None else err_str,
                status_code=status,
                feishu_code=code_int,
            )
        raise OAuthUpstreamError(
            provider="feishu",
            kind="client_error",
            error_code=f"feishu_{code_int}" if code_int is not None else (err_str or f"http_{status}"),
            status_code=status,
            feishu_code=code_int,
        )

    def _parse_token_data(self, data: dict[str, Any], *, is_refresh: bool) -> OAuthTokenResponse:
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        access = inner.get("access_token")
        if not isinstance(access, str) or not access.strip():
            raise OAuthUpstreamError(
                provider="feishu",
                kind="ambiguous" if is_refresh else "transient",
                error_code="missing_access_token",
            )
        raw_refresh = inner.get("refresh_token")
        refresh_tok = (
            raw_refresh.strip()
            if isinstance(raw_refresh, str) and raw_refresh.strip()
            else None
        )
        expires_in, expires_at = _compute_expires_at(inner.get("expires_in"))
        raw_scope = inner.get("scope")
        scope = raw_scope.strip() if isinstance(raw_scope, str) and raw_scope.strip() else None
        return OAuthTokenResponse(
            access_token=access.strip(),
            refresh_token=refresh_tok,
            expires_in=expires_in,
            expires_at=expires_at,
            scope=scope,
            id_token=None,
            token_type=str(inner.get("token_type") or "Bearer"),
        )

    async def exchange_code(
        self,
        *,
        app_id: str,
        app_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokenResponse:
        try:
            resp = await self._post_json(
                self._token_url,
                {
                    "grant_type": "authorization_code",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OAuthUpstreamError(
                provider="feishu",
                kind="transient",
                error_code=type(exc).__name__.lower(),
            ) from None
        data = self._classify_feishu_response(resp, is_refresh=False)
        return self._parse_token_data(data, is_refresh=False)

    async def refresh_token(
        self,
        *,
        app_id: str,
        app_secret: str,
        refresh_token: str,
    ) -> OAuthTokenResponse:
        try:
            resp = await self._post_json(
                self._token_url,
                {
                    "grant_type": "refresh_token",
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "refresh_token": refresh_token,
                },
            )
        except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
            # Never left the wire -> safe transient failure
            raise OAuthUpstreamError(
                provider="feishu",
                kind="transient",
                error_code=type(exc).__name__.lower(),
            ) from None
        except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.TimeoutException, httpx.TransportError) as exc:
            # Request may have been processed by Feishu (consuming single-use refresh_token)
            raise OAuthUpstreamError(
                provider="feishu",
                kind="ambiguous",
                error_code=type(exc).__name__.lower(),
            ) from None
        data = self._classify_feishu_response(resp, is_refresh=True)
        return self._parse_token_data(data, is_refresh=True)

    async def get_user_info(
        self,
        *,
        access_token: str,
    ) -> FeishuUserInfo:
        try:
            resp = await self._get_with_bearer(self._user_info_url, access_token)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise OAuthUpstreamError(
                provider="feishu",
                kind="transient",
                error_code=type(exc).__name__.lower(),
            ) from None
        data = self._classify_feishu_response(resp, is_refresh=False)
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        open_id = str(inner.get("open_id") or "").strip()
        if not open_id:
            raise OAuthUpstreamError(
                provider="feishu",
                kind="client_error",
                error_code="missing_open_id",
                status_code=resp.status_code,
            )
        raw_union = inner.get("union_id")
        union_id = str(raw_union).strip() if isinstance(raw_union, str) and raw_union.strip() else None
        raw_email = inner.get("enterprise_email") or inner.get("email")
        email = (
            str(raw_email).strip().lower()
            if isinstance(raw_email, str) and raw_email.strip()
            else None
        )
        raw_name = inner.get("name")
        name = str(raw_name).strip() if isinstance(raw_name, str) and raw_name.strip() else None
        return FeishuUserInfo(open_id=open_id, union_id=union_id, email=email, name=name)


_default_google_client: GoogleOAuthClient | None = None
_default_feishu_client: FeishuOAuthClient | None = None


def get_google_oauth_client() -> GoogleOAuthClient:
    global _default_google_client
    if _default_google_client is None:
        _default_google_client = GoogleOAuthClient()
    return _default_google_client


def get_feishu_oauth_client() -> FeishuOAuthClient:
    global _default_feishu_client
    if _default_feishu_client is None:
        _default_feishu_client = FeishuOAuthClient()
    return _default_feishu_client


def get_google_remote_revoker() -> RemoteRevoker:
    return get_google_oauth_client()


__all__ = [
    "FEISHU_AUTHORIZE_URL",
    "FEISHU_TOKEN_URL",
    "FEISHU_USER_INFO_URL",
    "FeishuOAuthClient",
    "FeishuOAuthClientProtocol",
    "FeishuUserInfo",
    "GOOGLE_AUTHORIZE_URL",
    "GOOGLE_JWKS_URL",
    "GOOGLE_REVOKE_URL",
    "GOOGLE_TOKEN_URL",
    "GoogleIdTokenClaims",
    "GoogleIdTokenVerificationError",
    "GoogleOAuthClient",
    "GoogleOAuthClientProtocol",
    "OAuthTokenResponse",
    "OAuthUpstreamError",
    "UpstreamErrorKind",
    "get_feishu_oauth_client",
    "get_google_oauth_client",
    "get_google_remote_revoker",
]
