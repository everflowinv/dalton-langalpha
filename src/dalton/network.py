"""Fake-IP exemption for the in-house fetch SSRF guard.

The host proxy runs in fake-IP mode, so inside the backend container DNS
answers every public host with an address from ``network.fake_ip_cidrs``
(198.18.0.0/15), which ``ipaddress`` classes as private. A fake-IP says
nothing about where the proxy will connect: the proxy resolves the hostname
itself. So an exempt address is only half the check; the guard must also look
up the host's real addresses (DNS-over-HTTPS, past the fake-IP resolver) and
apply its normal blocked-address test to each of them, failing closed when
that lookup cannot be done. A configured range overlapping the protected
ranges is dropped when the config loads, and the address is checked again here.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable

import httpx

from src.dalton.settings import (
    _EMBEDDED_IPV4_PREFIXES,
    NEVER_EXEMPT_NETWORKS,
    get_fake_ip_networks,
    get_fake_ip_real_resolvers,
)

logger = logging.getLogger(__name__)

DOH_TIMEOUT_S = 3.0
_RECORD_TYPES = {"A": 1, "AAAA": 28}


def is_fake_ip_exempt(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether ``addr`` is a proxy fake-IP the guard should let through."""
    networks = get_fake_ip_networks()
    if not networks:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
        return False
    # An IPv6 address carrying an IPv4 one (mapped, compatible, NAT64, 6to4) can
    # reach whatever that IPv4 address is; none of them is a fake-IP.
    if addr.version == 6 and any(addr in prefix for prefix, _ in _EMBEDDED_IPV4_PREFIXES):
        return False
    if any(addr in n for n in NEVER_EXEMPT_NETWORKS if n.version == addr.version):
        return False
    return any(addr in n for n in networks if n.version == addr.version)


class RealResolutionError(Exception):
    """The real addresses of a fake-IP host could not be established."""


async def doh_query(client: httpx.AsyncClient, resolver: str, host: str, rtype: str) -> list[str]:
    """One DoH JSON query; raises unless the resolver answered NOERROR."""
    resp = await client.get(
        resolver,
        params={"name": host, "type": rtype},
        headers={"accept": "application/dns-json"},
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("Status") != 0:
        raise RealResolutionError(f"{resolver} answered status {body.get('Status')} for {host} {rtype}")
    want = _RECORD_TYPES[rtype]
    return [str(a["data"]) for a in body.get("Answer") or [] if a.get("type") == want]


async def real_target_block_reason(host: str, is_blocked: Callable[[str], bool]) -> str | None:
    """Why the host's real A/AAAA records forbid the request, or None to allow.

    Every record any resolver returns is checked as it arrives, partial answers
    included, and one blocked address blocks the request whatever a later
    resolver says. Falling back only serves to get an answer at all: allowing
    takes one resolver answering both A and AAAA without error, with records.
    """
    resolvers = get_fake_ip_real_resolvers()
    if not resolvers:
        return _lookup_failed(host, "no network.fake_ip_real_resolver configured")
    errors: list[str] = []
    # The resolver client is deliberately unguarded: its own target is a fixed
    # configured URL, and routing it through the guard would recurse.
    async with httpx.AsyncClient(timeout=DOH_TIMEOUT_S) as client:
        for resolver in resolvers:
            found: list[str] = []
            complete = True
            for rtype in ("A", "AAAA"):
                try:
                    records = await doh_query(client, resolver, host, rtype)
                except Exception as exc:  # noqa: BLE001 - recorded; the next resolver is tried
                    errors.append(f"{resolver} {rtype}: {type(exc).__name__}: {exc}")
                    complete = False
                    continue
                for ip in records:
                    try:
                        blocked = is_blocked(ip)
                    except ValueError:
                        return f"Blocked {host!r}: resolver returned an unparseable address {ip!r}"
                    if blocked:
                        return f"Blocked private/reserved real address {ip} for host {host!r}"
                found.extend(records)
            if complete and found:
                return None
            if complete:
                errors.append(f"{resolver}: no A/AAAA records")
    return _lookup_failed(host, f"no resolver gave a complete answer ({'; '.join(errors)})")


def _lookup_failed(host: str, detail: str) -> str:
    logger.warning("fake-IP real resolution failed for %s: %s", host, detail)
    return f"Blocked {host!r}: real address lookup for a fake-IP answer failed ({detail})"


async def fake_ip_block_reason(
    host: str, addresses: list[str], is_blocked: Callable[[str], bool]
) -> str | None:
    """Why a request whose DNS answer is a fake-IP must be refused, or None.

    ``is_blocked`` is the guard's own test without the fake-IP exemption.
    """
    if not any(is_fake_ip_exempt(ipaddress.ip_address(ip)) for ip in addresses):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return f"Blocked fake-IP literal {host!r}: no hostname to check the real target of"
    return await real_target_block_reason(host, is_blocked)
