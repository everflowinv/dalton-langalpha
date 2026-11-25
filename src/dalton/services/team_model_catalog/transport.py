"""Catalog variant credential binding, Fake-IP/HeldTransport guard, and custom-model blocking.

Implements §2.5, §3.2, §3.3, and Decision 2:
- Construct-time + send-time double invariant across all 5 outbound paths.
- ``CatalogVariantTransport``: enforces HTTPS, ``endpoint_matches_prefix`` against
  the credential's ``bound_base_url``, ``follow_redirects=False``, no external HTTP
  proxy, live ``(canonical_bound_url, key_fingerprint)`` revocation check, and
  configuration-driven Fake-IP DoH real public IP dialing (Path A) vs
  ``HeldTransport`` public unicast pinning (Path B) with original-hostname TLS SNI.
- Personal ``custom_models`` rejection against catalog variants AND their
  ``bound_base_url`` (including base URLs inherited from the team credential holder).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
import httpx2
from fastapi import HTTPException

from src.dalton.llm.egress_policy import (
    canonical_url_key,
    canonicalize_endpoint,
    endpoint_matches_prefix,
)
from src.dalton.network import (
    DOH_TIMEOUT_S,
    fake_ip_block_reason,
    is_fake_ip_exempt,
)
from src.dalton.services.team_model_catalog.constants import (
    CATALOG_VARIANT_CREDENTIAL_SOURCE_PREFIX,
    CatalogVariantCredentialRevokedError,
    CatalogVariantSecurityError,
    catalog_variant_source_tag,
)
from src.dalton.settings import get_fake_ip_networks, get_fake_ip_real_resolvers
from src.server.utils.egress_guard import (
    EgressBlockedError,
    is_public_address,
)

logger = logging.getLogger(__name__)


def _is_blocked_public_ip(raw_ip: str) -> bool:
    """Return True when ``raw_ip`` is NOT a public unicast IP (strict fail-closed)."""
    addr = ipaddress.ip_address(str(raw_ip).strip().strip("[]"))
    return not is_public_address(addr)


def _resolve_host_sync(host: str) -> list[str]:
    cleaned = str(host or "").strip().strip("[]")
    try:
        ipaddress.ip_address(cleaned)
        return [cleaned]
    except ValueError:
        pass
    infos = socket.getaddrinfo(cleaned, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos if info and info[4]))


def _doh_real_public_ips_sync(host: str) -> tuple[str | None, list[str] | None]:
    """Synchronous DoH A+AAAA real public IP check (mirrors ``src.dalton.network.real_target_block_reason``)."""
    resolvers = get_fake_ip_real_resolvers()
    if not resolvers:
        return f"Blocked {host!r}: no network.fake_ip_real_resolver configured", None
    record_types = (("A", 1), ("AAAA", 28))
    errors: list[str] = []
    with httpx.Client(timeout=DOH_TIMEOUT_S) as client:
        for resolver in resolvers:
            found: list[str] = []
            complete = True
            for rtype, want in record_types:
                try:
                    resp = client.get(
                        resolver,
                        params={"name": host, "type": rtype},
                        headers={"accept": "application/dns-json"},
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    if body.get("Status") != 0:
                        raise RuntimeError(f"DNS status {body.get('Status')}")
                    records = [
                        str(a["data"])
                        for a in (body.get("Answer") or [])
                        if isinstance(a, Mapping) and a.get("type") == want and a.get("data")
                    ]
                except Exception as exc:
                    errors.append(f"{resolver} {rtype}: {type(exc).__name__}: {exc}")
                    complete = False
                    continue
                for ip in records:
                    try:
                        if _is_blocked_public_ip(ip):
                            return (
                                f"Blocked private/reserved real address {ip} for host {host!r}",
                                None,
                            )
                    except ValueError:
                        return (
                            f"Blocked {host!r}: resolver returned unparseable address {ip!r}",
                            None,
                        )
                found.extend(records)
            if complete and found:
                return None, list(dict.fromkeys(found))
            if complete:
                errors.append(f"{resolver}: no A/AAAA records")
    return (
        f"Blocked {host!r}: real address lookup failed ({'; '.join(errors)})",
        None,
    )


def validate_https_variant_base_url(raw_url: str) -> tuple[str, str]:
    """Validate a catalog variant ``base_url`` is strict HTTPS with no query/fragment/userinfo/traversal.

    Returns ``(cleaned_url, canonical_bound_url)``.
    """
    cleaned = str(raw_url or "").strip()
    if not cleaned:
        raise ValueError("base_url is required")
    parts = urlsplit(cleaned)
    if (parts.scheme or "").lower() != "https":
        raise ValueError("Catalog variant base_url must use https://")
    if parts.query or parts.fragment:
        raise ValueError("Catalog variant base_url must not contain ?query or #fragment")
    parsed = canonicalize_endpoint(cleaned)
    if parsed is None or parsed[0] != "https":
        raise ValueError(
            "Catalog variant base_url is invalid (must be https://host[:port][/path] without userinfo, backslashes, or encoded traversal)"
        )
    host = parsed[1]
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Catalog variant base_url cannot target localhost")
    canonical = canonical_url_key(cleaned)
    assert canonical is not None
    return cleaned, canonical


async def preflight_catalog_variant_url(url: str) -> list[str]:
    """Validate URL format and resolve verified public unicast IP(s) via Path A (Fake-IP DoH) or Path B.

    Returns the list of verified public IP strings, or raises ``EgressBlockedError``.
    """
    cleaned, _ = validate_https_variant_base_url(url)
    parsed = canonicalize_endpoint(cleaned)
    if parsed is None:
        raise EgressBlockedError(f"Invalid catalog variant URL: {url!r}")
    _, host, _, _ = parsed
    try:
        addresses = await asyncio.to_thread(_resolve_host_sync, host)
    except OSError as exc:
        raise EgressBlockedError(f"DNS resolution failed for {host!r}: {exc}") from exc
    if not addresses:
        raise EgressBlockedError(f"DNS returned no addresses for {host!r}")

    has_fake_ip_cfg = bool(get_fake_ip_networks())
    hits_fake_ip = has_fake_ip_cfg and any(
        is_fake_ip_exempt(ipaddress.ip_address(ip)) for ip in addresses
    )
    if hits_fake_ip:
        # Path A: Fake-IP container network -> DoH real A/AAAA verification
        reason, real_ips = await fake_ip_block_reason(host, addresses, _is_blocked_public_ip)
        if reason or not real_ips:
            raise EgressBlockedError(reason or f"Blocked fake-IP target {host!r}")
        return list(dict.fromkeys(real_ips))

    # Path B: Standard direct / Core public unicast check
    for ip in addresses:
        if _is_blocked_public_ip(ip):
            raise EgressBlockedError(
                f"Blocked non-public address {ip} for catalog variant host {host!r}"
            )
    return list(dict.fromkeys(addresses))


def _resolve_verified_ips_sync(host: str) -> tuple[bool, list[str]]:
    """Sync counterpart returning ``(used_fake_ip_path, verified_public_ips)``."""
    if not host or host.lower().rstrip(".") in ("localhost",) or host.lower().endswith(".localhost"):
        raise EgressBlockedError(f"Blocked host {host!r}")
    try:
        addresses = _resolve_host_sync(host)
    except OSError as exc:
        raise EgressBlockedError(f"DNS resolution failed for {host!r}: {exc}") from exc
    if not addresses:
        raise EgressBlockedError(f"DNS returned no addresses for {host!r}")

    has_fake_ip_cfg = bool(get_fake_ip_networks())
    hits_fake_ip = has_fake_ip_cfg and any(
        is_fake_ip_exempt(ipaddress.ip_address(ip)) for ip in addresses
    )
    if hits_fake_ip:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise EgressBlockedError(f"Blocked fake-IP literal {host!r}")
        reason, real_ips = _doh_real_public_ips_sync(host)
        if reason or not real_ips:
            raise EgressBlockedError(reason or f"Blocked fake-IP target {host!r}")
        return True, real_ips

    for ip in addresses:
        if _is_blocked_public_ip(ip):
            raise EgressBlockedError(
                f"Blocked non-public address {ip} for catalog variant host {host!r}"
            )
    return False, list(dict.fromkeys(addresses))


async def _resolve_verified_ips_async(host: str) -> tuple[bool, list[str]]:
    """Async counterpart returning ``(used_fake_ip_path, verified_public_ips)``."""
    if not host or host.lower().rstrip(".") in ("localhost",) or host.lower().endswith(".localhost"):
        raise EgressBlockedError(f"Blocked host {host!r}")
    try:
        addresses = await asyncio.to_thread(_resolve_host_sync, host)
    except OSError as exc:
        raise EgressBlockedError(f"DNS resolution failed for {host!r}: {exc}") from exc
    if not addresses:
        raise EgressBlockedError(f"DNS returned no addresses for {host!r}")

    has_fake_ip_cfg = bool(get_fake_ip_networks())
    hits_fake_ip = has_fake_ip_cfg and any(
        is_fake_ip_exempt(ipaddress.ip_address(ip)) for ip in addresses
    )
    if hits_fake_ip:
        reason, real_ips = await fake_ip_block_reason(host, addresses, _is_blocked_public_ip)
        if reason or not real_ips:
            raise EgressBlockedError(reason or f"Blocked fake-IP target {host!r}")
        return True, list(dict.fromkeys(real_ips))

    for ip in addresses:
        if _is_blocked_public_ip(ip):
            raise EgressBlockedError(
                f"Blocked non-public address {ip} for catalog variant host {host!r}"
            )
    return False, list(dict.fromkeys(addresses))


def _format_ip_for_url(ip: str) -> str:
    return f"[{ip}]" if ":" in ip and not ip.startswith("[") else ip


def _make_catalog_variant_transport_class(hx: Any, *, family: str = "generic") -> type:
    """Build an ``httpx`` or ``httpx2`` dual transport enforcing all catalog-variant invariants."""

    class CatalogVariantTransport(hx.BaseTransport, hx.AsyncBaseTransport):
        is_catalog_variant_transport: bool = True

        def __init__(
            self,
            *,
            provider_slug: str,
            bound_base_url: str,
            expected_key_fingerprint: str | None = None,
            credential_source: str | None = None,
            verify_db_binding: bool = True,
            db_binding_reader: Callable[[str], dict[str, Any] | None] | None = None,
            db_binding_reader_async: Callable[[str], Any] | None = None,
            held_transport: Any = None,
            direct_sync_transport: Any = None,
            direct_async_transport: Any = None,
        ) -> None:
            cleaned_url, canonical_bound = validate_https_variant_base_url(bound_base_url)
            expected_tag = catalog_variant_source_tag(provider_slug)
            if credential_source != expected_tag:
                raise CatalogVariantSecurityError(
                    f"CatalogVariantTransport credential source mismatch for {provider_slug!r}: "
                    f"expected {expected_tag!r}, got {credential_source!r}"
                )
            self.provider_slug = str(provider_slug).strip()
            self.bound_base_url = cleaned_url
            self.canonical_bound_url = canonical_bound
            self.expected_key_fingerprint = expected_key_fingerprint
            self.credential_source = expected_tag
            self.verify_db_binding = verify_db_binding
            self._db_reader_sync = db_binding_reader
            self._db_reader_async = db_binding_reader_async
            self._family = family
            self._held = held_transport

            ssl_ctx = hx.create_ssl_context(trust_env=False)
            # Zero keepalive on direct-IP transport so TLS sessions never cross hosts
            no_keepalive = hx.Limits(max_keepalive_connections=0)
            self._direct_sync = direct_sync_transport or hx.HTTPTransport(
                verify=ssl_ctx, trust_env=False, limits=no_keepalive
            )
            self._direct_async = direct_async_transport or hx.AsyncHTTPTransport(
                verify=ssl_ctx, trust_env=False, limits=no_keepalive
            )

        def _get_held_transport(self):
            if self._held is not None:
                return self._held
            from src.server.services.llm.egress import (
                EndpointRefused,
                _HeldHttpx,
                _HeldHttpx2,
            )

            cls = _HeldHttpx2 if hx is httpx2 else _HeldHttpx
            self._held = cls(EndpointRefused, shared=False, ip_policy="public")
            return self._held

        def _check_request_invariants(self, request: Any) -> str:
            url_str = str(request.url)
            if str(request.url.scheme).lower() != "https":
                raise CatalogVariantSecurityError(
                    f"Catalog variant '{self.provider_slug}' refuses non-HTTPS request: {url_str!r}"
                )
            if not endpoint_matches_prefix(url_str, self.bound_base_url):
                raise CatalogVariantSecurityError(
                    f"Catalog variant '{self.provider_slug}' request URL {url_str!r} "
                    f"does not match bound_base_url {self.bound_base_url!r}"
                )
            host = str(request.url.host or "").strip()
            if not host or host.lower().rstrip(".") == "localhost" or host.lower().endswith(".localhost"):
                raise CatalogVariantSecurityError(
                    f"Catalog variant '{self.provider_slug}' refuses host {host!r}"
                )
            return host

        def _verify_live_binding_result(self, live_row: Mapping[str, Any] | None) -> None:
            if live_row is None:
                raise CatalogVariantCredentialRevokedError(
                    self.provider_slug, "credential was deleted"
                )
            live_canonical = str(live_row.get("canonical_bound_url") or "")
            if not live_canonical and live_row.get("bound_base_url"):
                live_canonical = canonical_url_key(str(live_row["bound_base_url"])) or ""
            if live_canonical != self.canonical_bound_url:
                raise CatalogVariantCredentialRevokedError(
                    self.provider_slug,
                    f"bound_base_url changed ({self.canonical_bound_url} -> {live_canonical})",
                )
            if self.expected_key_fingerprint:
                live_fp = str(live_row.get("key_fingerprint") or "")
                if live_fp != self.expected_key_fingerprint:
                    raise CatalogVariantCredentialRevokedError(
                        self.provider_slug, "API key was rotated"
                    )

        def _check_live_binding_sync(self) -> None:
            if not self.verify_db_binding:
                return
            reader = self._db_reader_sync
            if reader is None:
                from src.dalton.database.team_model_catalog import (
                    get_catalog_provider_credential_sync,
                )

                reader = lambda slug: get_catalog_provider_credential_sync(slug, decrypt=False)
            live_row = reader(self.provider_slug)
            self._verify_live_binding_result(live_row)

        async def _check_live_binding_async(self) -> None:
            if not self.verify_db_binding:
                return
            if self._db_reader_async is not None:
                live_row = await self._db_reader_async(self.provider_slug)
            elif self._db_reader_sync is not None:
                live_row = self._db_reader_sync(self.provider_slug)
            else:
                from src.dalton.database.team_model_catalog import (
                    get_catalog_provider_credential,
                )

                live_row = await get_catalog_provider_credential(self.provider_slug, decrypt=False)
            self._verify_live_binding_result(live_row)

        @staticmethod
        def _pinned_request(request: Any, ip: str, host: str) -> Any:
            extensions = dict(getattr(request, "extensions", None) or {})
            extensions["sni_hostname"] = host
            headers = request.headers.copy()
            if "host" not in {k.lower() for k in headers.keys()}:
                port = request.url.port
                headers["Host"] = f"{host}:{port}" if port and port != 443 else host
            return hx.Request(
                request.method,
                request.url.copy_with(host=_format_ip_for_url(ip)),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )

        def handle_request(self, request: Any):
            host = self._check_request_invariants(request)
            self._check_live_binding_sync()
            used_fake_ip, ips = _resolve_verified_ips_sync(host)
            if not used_fake_ip:
                return self._get_held_transport().handle_request(request)
            *earlier, last = ips
            for ip in earlier:
                try:
                    return self._direct_sync.handle_request(self._pinned_request(request, ip, host))
                except (hx.ConnectError, hx.ConnectTimeout):
                    continue
            return self._direct_sync.handle_request(self._pinned_request(request, last, host))

        async def handle_async_request(self, request: Any):
            host = self._check_request_invariants(request)
            await self._check_live_binding_async()
            used_fake_ip, ips = await _resolve_verified_ips_async(host)
            if not used_fake_ip:
                return await self._get_held_transport().handle_async_request(request)
            *earlier, last = ips
            for ip in earlier:
                try:
                    return await self._direct_async.handle_async_request(
                        self._pinned_request(request, ip, host)
                    )
                except (hx.ConnectError, hx.ConnectTimeout):
                    continue
            return await self._direct_async.handle_async_request(
                self._pinned_request(request, last, host)
            )

        def close(self) -> None:
            self._direct_sync.close()
            if self._held is not None:
                self._held.close()

        async def aclose(self) -> None:
            await self._direct_async.aclose()
            if self._held is not None:
                await self._held.aclose()

    return CatalogVariantTransport


CatalogVariantHttpxTransport = _make_catalog_variant_transport_class(httpx)
CatalogVariantHttpx2Transport = _make_catalog_variant_transport_class(httpx2)


def make_catalog_variant_http_transport(
    *,
    provider_slug: str,
    bound_base_url: str,
    expected_key_fingerprint: str | None = None,
    verify_db_binding: bool = True,
    db_binding_reader: Callable[[str], dict[str, Any] | None] | None = None,
    db_binding_reader_async: Callable[[str], Any] | None = None,
) -> Any:
    """Create a plain ``httpx`` ``CatalogVariantTransport`` (for model list & test calls)."""
    return CatalogVariantHttpxTransport(
        provider_slug=provider_slug,
        bound_base_url=bound_base_url,
        expected_key_fingerprint=expected_key_fingerprint,
        credential_source=catalog_variant_source_tag(provider_slug),
        verify_db_binding=verify_db_binding,
        db_binding_reader=db_binding_reader,
        db_binding_reader_async=db_binding_reader_async,
    )


def make_catalog_variant_transport_factory(
    *,
    provider_slug: str,
    bound_base_url: str,
    expected_key_fingerprint: str | None = None,
    verify_db_binding: bool = True,
    db_binding_reader: Callable[[str], dict[str, Any] | None] | None = None,
    db_binding_reader_async: Callable[[str], Any] | None = None,
) -> Callable[[str], Any]:
    """Return an ``egress_transport(family)`` factory for ``LLM(..., egress_transport=...)``."""
    tag = catalog_variant_source_tag(provider_slug)

    def _build(family: str) -> Any:
        if family == "openai":
            cls = CatalogVariantHttpx2Transport
        elif family in ("anthropic", "openrouter"):
            cls = CatalogVariantHttpxTransport
        elif family == "gemini":
            cls = (
                CatalogVariantHttpxTransport
                if os.environ.get("GOOGLE_GENAI_HTTP_CLIENT", "").strip().lower() == "httpx"
                else CatalogVariantHttpx2Transport
            )
        else:
            raise CatalogVariantSecurityError(
                f"Catalog variant '{provider_slug}' cannot attach controlled transport for SDK family {family!r}"
            )
        return cls(
            provider_slug=provider_slug,
            bound_base_url=bound_base_url,
            expected_key_fingerprint=expected_key_fingerprint,
            credential_source=tag,
            verify_db_binding=verify_db_binding,
            db_binding_reader=db_binding_reader,
            db_binding_reader_async=db_binding_reader_async,
        )

    _build.is_catalog_variant_factory = True  # type: ignore[attr-defined]
    _build.provider_slug = provider_slug  # type: ignore[attr-defined]
    _build.bound_base_url = bound_base_url  # type: ignore[attr-defined]
    _build.credential_source = tag  # type: ignore[attr-defined]
    return _build


def _binding_matches_provider_info(
    cred: Mapping[str, Any] | None,
    prov_info: Mapping[str, Any] | None = None,
) -> bool:
    """Return True iff ``cred`` exists and its canonical bound URL matches ``prov_info``."""
    if not isinstance(cred, Mapping):
        return False
    live_canonical = str(
        cred.get("canonical_bound_url")
        or canonical_url_key(str(cred.get("bound_base_url") or ""))
        or ""
    )
    if not live_canonical:
        return False
    if isinstance(prov_info, Mapping) and prov_info.get("base_url"):
        target_canonical = canonical_url_key(str(prov_info["base_url"]))
        if not target_canonical or live_canonical != target_canonical:
            return False
    return True


async def has_valid_catalog_variant_binding_async(
    provider_slug: str,
    *,
    prov_info: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> bool:
    """Check whether catalog variant ``provider_slug`` has a valid bound credential (no LLM build)."""
    from src.dalton.database.team_model_catalog import get_catalog_provider_credential

    slug = str(provider_slug or "").strip()
    if not slug:
        return False
    try:
        cred = await get_catalog_provider_credential(slug, conn=conn, decrypt=False)
    except Exception:
        return False
    return _binding_matches_provider_info(cred, prov_info)


def has_valid_catalog_variant_binding_sync(
    provider_slug: str,
    *,
    prov_info: Mapping[str, Any] | None = None,
) -> bool:
    """Synchronous credential-binding availability check for ``platform_key_configured``."""
    from unittest.mock import NonCallableMock

    from src.dalton.database import team_model_catalog as tmc_db

    slug = str(provider_slug or "").strip()
    if not slug:
        return False
    reader = tmc_db.get_catalog_provider_credential_sync
    is_mocked = isinstance(reader, NonCallableMock) or (
        getattr(reader, "__module__", None) != tmc_db.__name__
    )
    if not is_mocked and not tmc_db.is_conversation_db_pool_open():
        return False
    try:
        cred = reader(slug, decrypt=False)
    except Exception:
        return False
    return _binding_matches_provider_info(cred, prov_info)


async def resolve_catalog_variant_binding_async(
    provider_slug: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
    conn: Any = None,
    model_key: str | None = None,
) -> dict[str, Any]:
    """Load and verify a catalog variant's encrypted credential and build its transport factory."""
    from src.dalton.database.team_model_catalog import get_catalog_provider_credential
    from src.dalton.services.team_model_catalog.snapshot import (
        lookup_snapshot_model_entry,
        lookup_snapshot_variant_binding,
    )

    if model_key:
        snap_model = lookup_snapshot_model_entry(model_key, snapshot=snapshot)
        if snap_model is not None:
            snap_prov = str(snap_model.get("provider") or "").strip()
            if snap_prov and snap_prov != provider_slug:
                raise CatalogVariantCredentialRevokedError(
                    provider_slug,
                    f"model {model_key!r} route changed since turn snapshot ({snap_prov} -> {provider_slug})",
                )

    cred = await get_catalog_provider_credential(provider_slug, conn=conn, decrypt=True)
    snap_binding = lookup_snapshot_variant_binding(provider_slug, snapshot=snapshot)

    if cred is None or not cred.get("api_key"):
        if snap_binding is not None:
            raise CatalogVariantCredentialRevokedError(provider_slug, "credential was deleted")
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider_slug}' has no bound credential configured"
        )

    bound_url = str(cred["bound_base_url"])
    canonical_bound = str(cred.get("canonical_bound_url") or canonical_url_key(bound_url) or "")
    key_fp = str(cred["key_fingerprint"])

    if snap_binding is not None:
        snap_canonical = str(
            snap_binding.get("canonical_bound_url")
            or canonical_url_key(str(snap_binding.get("bound_base_url") or ""))
            or ""
        )
        snap_fp = str(snap_binding.get("key_fingerprint") or "")
        if snap_canonical and snap_canonical != canonical_bound:
            raise CatalogVariantCredentialRevokedError(
                provider_slug,
                f"bound_base_url changed since turn snapshot ({snap_canonical} -> {canonical_bound})",
            )
        if snap_fp and snap_fp != key_fp:
            raise CatalogVariantCredentialRevokedError(
                provider_slug,
                "API key rotated since turn snapshot",
            )

    factory = make_catalog_variant_transport_factory(
        provider_slug=provider_slug,
        bound_base_url=bound_url,
        expected_key_fingerprint=key_fp,
        verify_db_binding=True,
    )
    return {
        "provider_slug": provider_slug,
        "api_key": str(cred["api_key"]),
        "bound_base_url": bound_url,
        "canonical_bound_url": canonical_bound,
        "key_fingerprint": key_fp,
        "credential_source": catalog_variant_source_tag(provider_slug),
        "egress_transport": factory,
        "follow_redirects": False,
    }


def resolve_catalog_variant_binding_sync(
    provider_slug: str,
    *,
    snapshot: Mapping[str, Any] | None = None,
    model_key: str | None = None,
) -> dict[str, Any]:
    """Synchronous counterpart for ``create_llm`` direct/system path (#2)."""
    from src.dalton.database.team_model_catalog import get_catalog_provider_credential_sync
    from src.dalton.services.team_model_catalog.snapshot import (
        lookup_snapshot_model_entry,
        lookup_snapshot_variant_binding,
    )

    if model_key:
        snap_model = lookup_snapshot_model_entry(model_key, snapshot=snapshot)
        if snap_model is not None:
            snap_prov = str(snap_model.get("provider") or "").strip()
            if snap_prov and snap_prov != provider_slug:
                raise CatalogVariantCredentialRevokedError(
                    provider_slug,
                    f"model {model_key!r} route changed since turn snapshot ({snap_prov} -> {provider_slug})",
                )

    cred = get_catalog_provider_credential_sync(provider_slug, decrypt=True)
    snap_binding = lookup_snapshot_variant_binding(provider_slug, snapshot=snapshot)

    if cred is None or not cred.get("api_key"):
        if snap_binding is not None:
            raise CatalogVariantCredentialRevokedError(provider_slug, "credential was deleted")
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider_slug}' has no bound credential configured"
        )

    bound_url = str(cred["bound_base_url"])
    canonical_bound = str(cred.get("canonical_bound_url") or canonical_url_key(bound_url) or "")
    key_fp = str(cred["key_fingerprint"])

    if snap_binding is not None:
        snap_canonical = str(
            snap_binding.get("canonical_bound_url")
            or canonical_url_key(str(snap_binding.get("bound_base_url") or ""))
            or ""
        )
        snap_fp = str(snap_binding.get("key_fingerprint") or "")
        if snap_canonical and snap_canonical != canonical_bound:
            raise CatalogVariantCredentialRevokedError(
                provider_slug,
                f"bound_base_url changed since turn snapshot ({snap_canonical} -> {canonical_bound})",
            )
        if snap_fp and snap_fp != key_fp:
            raise CatalogVariantCredentialRevokedError(
                provider_slug,
                "API key rotated since turn snapshot",
            )

    factory = make_catalog_variant_transport_factory(
        provider_slug=provider_slug,
        bound_base_url=bound_url,
        expected_key_fingerprint=key_fp,
        verify_db_binding=True,
    )
    return {
        "provider_slug": provider_slug,
        "api_key": str(cred["api_key"]),
        "bound_base_url": bound_url,
        "canonical_bound_url": canonical_bound,
        "key_fingerprint": key_fp,
        "credential_source": catalog_variant_source_tag(provider_slug),
        "egress_transport": factory,
        "follow_redirects": False,
    }


def enforce_catalog_variant_construction(
    *,
    provider: str,
    provider_info: Mapping[str, Any],
    base_url: str | None,
    egress_transport: Any,
    follow_redirects: bool,
    credential_source: str | None,
    api_key_override: str | None,
) -> None:
    """Enforce construct-time invariants whenever ``provider_info.get('catalog_defined')`` is True."""
    if not provider_info.get("catalog_defined"):
        return
    expected_tag = catalog_variant_source_tag(provider)
    if credential_source != expected_tag:
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider}' requires credential_source={expected_tag!r} "
            f"(got {credential_source!r})"
        )
    if not api_key_override or not str(api_key_override).strip():
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider}' requires its dedicated bound API key"
        )
    if follow_redirects:
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider}' must disable HTTP redirects (follow_redirects=False)"
        )
    if egress_transport is None or not getattr(egress_transport, "is_catalog_variant_factory", False):
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider}' requires CatalogVariantTransport"
        )
    factory_slug = getattr(egress_transport, "provider_slug", None)
    factory_bound = getattr(egress_transport, "bound_base_url", None)
    if factory_slug != provider:
        raise CatalogVariantSecurityError(
            f"Catalog variant transport bound to {factory_slug!r}, not {provider!r}"
        )
    effective_url = str(base_url or "").strip()
    if not effective_url or not factory_bound or not endpoint_matches_prefix(effective_url, factory_bound):
        raise CatalogVariantSecurityError(
            f"Catalog variant '{provider}' base_url {effective_url!r} does not match "
            f"bound_base_url {factory_bound!r}"
        )


def url_matches_catalog_variant(
    url: str | None,
    variant_bound_urls: Mapping[str, str],
) -> str | None:
    """Return matching catalog variant slug if ``url`` lands on any variant's ``bound_base_url``."""
    cleaned = str(url or "").strip()
    if not cleaned or not variant_bound_urls:
        return None
    ck = canonical_url_key(cleaned)
    for slug, bound_url in variant_bound_urls.items():
        if not bound_url:
            continue
        if ck and ck == canonical_url_key(bound_url):
            return slug
        if endpoint_matches_prefix(cleaned, bound_url) or endpoint_matches_prefix(bound_url, cleaned):
            return slug
    return None


def get_catalog_variant_bound_urls(mc: Any = None) -> dict[str, str]:
    """Return ``{provider_slug: bound_base_url}`` for every active ``catalog_defined`` variant."""
    if mc is None:
        from src.llms.llm import LLM

        mc = LLM.get_model_config()
    out: dict[str, str] = {}
    for slug, info in (getattr(mc, "flat_providers", None) or {}).items():
        if isinstance(info, Mapping) and info.get("catalog_defined"):
            raw = str(info.get("base_url") or "").strip()
            if raw:
                out[str(slug)] = raw
    return out


def check_custom_model_against_catalog_variants(
    *,
    resolved_provider: str | None,
    resolved_base_url: str | None,
    parameter_urls: Iterable[str] | None = None,
    mc: Any = None,
    model_name: str | None = None,
) -> None:
    """Reject any personal custom model/provider targeting a catalog variant or its ``bound_base_url``.

    Enforces §2.2 dimension ④ & §2.5 at both save-time (``custom_catalog.py``) and
    request-time (``clients.py``, ``LLM.from_custom_config``), covering URLs inherited
    from the credential holder's saved ``base_url``.
    """
    if mc is None:
        from src.llms.llm import LLM

        mc = LLM.get_model_config()
    prov = str(resolved_provider or "").strip()
    if prov:
        pinfo = mc.get_provider_info(prov)
        if isinstance(pinfo, Mapping) and pinfo.get("catalog_defined"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "catalog_variant_custom_model_forbidden",
                    "message": (
                        f"Personal custom model {model_name or ''!r} cannot target "
                        f"catalog-defined provider variant '{prov}'. Use the system catalog entry."
                    ).strip(),
                    "provider": prov,
                },
            )

    variant_urls = get_catalog_variant_bound_urls(mc)
    if not variant_urls:
        return

    candidate_urls: list[str] = []
    if resolved_base_url and str(resolved_base_url).strip():
        candidate_urls.append(str(resolved_base_url).strip())
    for purl in parameter_urls or ():
        if purl and str(purl).strip():
            candidate_urls.append(str(purl).strip())

    for url in candidate_urls:
        matched_slug = url_matches_catalog_variant(url, variant_urls)
        if matched_slug is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "catalog_variant_custom_model_forbidden",
                    "message": (
                        f"Personal custom model {model_name or ''!r} resolves to URL {url!r}, "
                        f"which overlaps catalog-defined provider variant '{matched_slug}'. "
                        "Catalog variants may only be invoked via system catalog models."
                    ).strip(),
                    "provider": matched_slug,
                },
            )


def is_catalog_variant_credential_tag(tag: str | None, provider_slug: str) -> bool:
    return str(tag or "") == f"{CATALOG_VARIANT_CREDENTIAL_SOURCE_PREFIX}{provider_slug}"
