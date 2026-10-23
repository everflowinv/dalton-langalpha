"""SSRF egress guard for the in-house fetch layer.

The extractors and sitemap fetcher run in the backend process, so a request
they make originates from a host that can reach internal services and the
cloud metadata endpoint. ``_validate_url`` guards the *initial* URL, but httpx
follows redirects transparently — a public page can 3xx to ``127.0.0.1`` or
``169.254.169.254`` and slip past that one check.

:class:`GuardedAsyncTransport` re-validates the resolved address on *every*
hop httpx makes (initial request and each redirect), which also closes DNS
rebinding for these paths since it checks the resolved IP, not the hostname.

Scope: httpx clients only. The scrapling engine tiers (curl_cffi tier 1 and
the browser tiers) do their own DNS and redirect handling below Python and are
not covered here — that residual needs network-level egress control.
"""

import asyncio
import ipaddress
import socket

import httpx


class BlockedAddressError(httpx.RequestError):
    """A request target resolved to a private/reserved address."""


# DALTON-HOOK(fake-ip-allow): behind a fake-IP proxy every public host resolves
# into a benchmarking range that ``is_private`` flags; only the configured ranges
# are exempt (src/dalton/network.py). An unavailable or unreadable config exempts
# nothing, so the upstream check applies unchanged.
def _is_fake_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    try:
        from src.dalton.network import is_fake_ip_exempt

        return is_fake_ip_exempt(addr)
    except Exception:  # noqa: BLE001
        return False


async def _fake_ip_block_reason(host: str, addresses: list[str]) -> str | None:
    if not any(_is_fake_ip(ipaddress.ip_address(ip)) for ip in addresses):
        return None
    try:
        from src.dalton.network import fake_ip_block_reason

        return await fake_ip_block_reason(
            host, addresses, lambda ip: _ip_is_blocked(ip, allow_fake_ip=False)
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        return f"Blocked {host!r}: fake-IP check failed ({type(exc).__name__})"


def _ip_is_blocked(ip: str, *, allow_fake_ip: bool = True) -> bool:
    addr = ipaddress.ip_address(ip)
    if allow_fake_ip and _is_fake_ip(addr):  # DALTON-HOOK(fake-ip-allow)
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_unspecified
        or addr.is_multicast
    )


def _resolve(host: str) -> list[str]:
    """Resolve a host to its addresses; a raw-IP host resolves to itself."""
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]


class GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """httpx transport that blocks requests resolving to private/reserved IPs.

    httpx invokes the transport once per hop, so validating here covers the
    initial request and every redirect target uniformly.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host or host == "localhost":
            raise BlockedAddressError(f"Blocked host: {host!r}", request=request)
        try:
            addresses = await asyncio.to_thread(_resolve, host)
        except socket.gaierror as exc:
            raise BlockedAddressError(
                f"DNS resolution failed for {host!r}", request=request
            ) from exc
        # TODO(dns-rebinding): the check resolves the host, then the connection
        # below resolves it again, so a host can answer public here and private
        # there. Closing it means connecting to the address checked here.
        for ip in addresses:
            if _ip_is_blocked(ip):
                raise BlockedAddressError(
                    f"Blocked private/reserved address {ip} for host {host!r}",
                    request=request,
                )
        # DALTON-HOOK(fake-ip-allow): a fake-IP answer hides the real target the
        # proxy will connect to; check the host's real addresses as well.
        reason = await _fake_ip_block_reason(host, addresses)
        if reason:
            raise BlockedAddressError(reason, request=request)
        return await super().handle_async_request(request)
