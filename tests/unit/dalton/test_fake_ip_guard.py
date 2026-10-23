"""Fake-IP exemption in the in-house fetch SSRF guard."""

from __future__ import annotations

import ipaddress
import logging
from unittest.mock import create_autospec, patch

import httpx
import pytest

from src.dalton import network as dalton_network
from src.dalton import settings as dalton_settings
from src.dalton.settings import DaltonNetworkConfig
from src.tools.web.inhouse.guard import (
    BlockedAddressError,
    GuardedAsyncTransport,
    _ip_is_blocked,
)


@pytest.fixture
def configured(monkeypatch):
    nets = (ipaddress.ip_network("198.18.0.0/15"),)
    monkeypatch.setattr("src.dalton.network.get_fake_ip_networks", lambda: nets)


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr("src.dalton.network.get_fake_ip_networks", lambda: ())


def test_shipped_config_lists_fake_ip_range():
    dalton_settings.get_dalton_config.cache_clear()
    assert dalton_settings.get_dalton_config().network.fake_ip_cidrs == ["198.18.0.0/15"]


@pytest.mark.parametrize("ip", ["198.18.1.238", "198.18.4.174", "198.19.255.1"])
def test_fake_ip_allowed_when_configured(configured, ip):
    assert _ip_is_blocked(ip) is False


@pytest.mark.parametrize("ip", ["198.18.1.238", "198.19.255.1"])
def test_fake_ip_blocked_when_not_configured(unconfigured, ip):
    assert _ip_is_blocked(ip) is True


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0", "::1", "fd00::1"],
)
def test_protected_ranges_still_blocked_when_configured(configured, ip):
    assert _ip_is_blocked(ip) is True


@pytest.mark.parametrize(
    "cidr",
    ["10.0.0.0/8", "10.1.0.0/16", "172.16.0.0/12", "192.168.0.0/24", "127.0.0.0/8",
     "169.254.169.254/32", "0.0.0.0/0", "::1/128", "fc00::/7", "fd12::/16", "::/0",
     # IPv6 ranges embedding a protected IPv4 range
     "::ffff:10.0.0.0/104", "::ffff:127.0.0.0/104", "::ffff:169.254.169.254/128",
     "::ffff:0:0/96", "::10.0.0.0/104", "64:ff9b::a9fe:a9fe/128", "2002:a00::/24",
     "2002::/16", "2000::/3"],
)
def test_overlapping_cidr_dropped_and_logged(cidr, caplog):
    with caplog.at_level(logging.ERROR, logger="src.dalton.settings"):
        cfg = DaltonNetworkConfig(fake_ip_cidrs=["198.18.0.0/15", cidr])
    assert cfg.fake_ip_cidrs == ["198.18.0.0/15"]
    assert any("overlaps protected range" in r.getMessage() for r in caplog.records)


def test_malformed_cidr_dropped_and_logged(caplog):
    with caplog.at_level(logging.ERROR, logger="src.dalton.settings"):
        cfg = DaltonNetworkConfig(fake_ip_cidrs=["not-a-cidr", "198.18.0.0/15"])
    assert cfg.fake_ip_cidrs == ["198.18.0.0/15"]
    assert any("not a valid CIDR" in r.getMessage() for r in caplog.records)


def test_bad_entry_does_not_break_whole_config():
    cfg = dalton_settings.DaltonConfig.model_validate(
        {"timezone": "UTC", "network": {"fake_ip_cidrs": ["10.0.0.0/8", "198.18.0.0/15"]}}
    )
    assert cfg.timezone == "UTC"
    assert cfg.network.fake_ip_cidrs == ["198.18.0.0/15"]


def test_dropped_range_is_not_exempt(monkeypatch):
    cfg = DaltonNetworkConfig(fake_ip_cidrs=["10.0.0.0/8"])
    nets = tuple(ipaddress.ip_network(c) for c in cfg.fake_ip_cidrs)
    monkeypatch.setattr("src.dalton.network.get_fake_ip_networks", lambda: nets)
    assert _ip_is_blocked("10.1.2.3") is True


def test_default_network_config_exempts_nothing():
    assert DaltonNetworkConfig().fake_ip_cidrs == []


def test_disabled_dalton_exempts_nothing(monkeypatch):
    monkeypatch.setattr(dalton_settings, "is_dalton_enabled", lambda: False)
    assert dalton_settings.get_fake_ip_networks() == ()


@pytest.mark.parametrize(
    "ip",
    ["::ffff:10.1.2.3", "::ffff:198.18.1.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
     "64:ff9b::a00:1", "2002:a00:1::1"],
)
def test_embedded_ipv4_never_exempt(monkeypatch, ip):
    # Even a (now impossible) configured IPv6 range covering it does not exempt it.
    nets = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("::/0"))
    monkeypatch.setattr("src.dalton.network.get_fake_ip_networks", lambda: nets)
    assert dalton_network.is_fake_ip_exempt(ipaddress.ip_address(ip)) is False


def test_resolvers_default_and_non_https_dropped(caplog):
    assert DaltonNetworkConfig().fake_ip_real_resolver == [
        "https://1.1.1.1/dns-query",
        "https://dns.google/resolve",
    ]
    with caplog.at_level(logging.ERROR, logger="src.dalton.settings"):
        cfg = DaltonNetworkConfig(fake_ip_real_resolver=["http://1.1.1.1/dns-query", "https://x/dns"])
    assert cfg.fake_ip_real_resolver == ["https://x/dns"]
    assert any("not an https URL" in r.getMessage() for r in caplog.records)


def _doh(answers: dict[tuple[str, str], list[str] | Exception]):
    """An autospec'd ``doh_query`` answering per (resolver, rtype)."""

    async def side_effect(client, resolver, host, rtype):
        result = answers.get((resolver, rtype), [])
        if isinstance(result, Exception):
            raise result
        return result

    return create_autospec(dalton_network.doh_query, side_effect=side_effect)


R1, R2 = "https://1.1.1.1/dns-query", "https://dns.google/resolve"


@pytest.fixture
def resolvers(monkeypatch):
    monkeypatch.setattr("src.dalton.network.get_fake_ip_real_resolvers", lambda: [R1, R2])


async def _fetch(url: str, dns: list[str], doh):
    sentinel = httpx.Response(200, content=b"ok")
    with patch("src.tools.web.inhouse.guard._resolve", autospec=True, return_value=dns), patch.object(
        dalton_network, "doh_query", doh
    ), patch.object(
        httpx.AsyncHTTPTransport, "handle_async_request", autospec=True, return_value=sentinel
    ) as delegated:
        resp = await GuardedAsyncTransport().handle_async_request(httpx.Request("GET", url))
    assert resp is sentinel
    delegated.assert_awaited_once()


@pytest.mark.asyncio
async def test_fake_ip_host_with_public_real_ip_allowed(configured, resolvers):
    doh = _doh({(R1, "A"): ["162.159.1.1"], (R1, "AAAA"): ["2606:4700::1"]})
    await _fetch("https://www.sec.gov/", ["198.18.1.238"], doh)
    assert [c.args[3] for c in doh.await_args_list] == ["A", "AAAA"]
    assert all(c.args[2] == "www.sec.gov" for c in doh.await_args_list)


@pytest.mark.asyncio
async def test_falls_back_to_second_resolver(configured, resolvers):
    doh = _doh({(R1, "A"): httpx.ConnectTimeout("t"), (R2, "A"): ["162.159.1.1"]})
    await _fetch("https://www.sec.gov/", ["198.18.1.238"], doh)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "real",
    [["127.0.0.1"], ["10.1.2.3"], ["169.254.169.254"], ["192.168.0.5"], ["::1"],
     ["8.8.8.8", "172.16.0.1"], ["198.18.0.9"], ["::ffff:10.0.0.1"]],
)
async def test_fake_ip_host_with_private_real_ip_blocked(configured, resolvers, real):
    doh = _doh({(R1, "A"): real})
    with pytest.raises(BlockedAddressError, match="real address"):
        await _fetch("http://rebind.example/", ["198.18.3.3"], doh)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        {(R1, "A"): httpx.ConnectError("x"), (R2, "A"): httpx.ConnectError("y")},
        {(R1, "A"): dalton_network.RealResolutionError("status 3"), (R2, "AAAA"): httpx.ReadTimeout("t")},
        {},  # NOERROR but no records
    ],
)
async def test_doh_failure_or_empty_blocks(configured, resolvers, answers):
    with pytest.raises(BlockedAddressError, match="real address lookup"):
        await _fetch("https://www.sec.gov/", ["198.18.1.238"], _doh(answers))


@pytest.mark.asyncio
async def test_partial_private_answer_blocks_despite_later_public_resolver(configured, resolvers):
    doh = _doh({
        (R1, "A"): ["127.0.0.1"], (R1, "AAAA"): httpx.ReadTimeout("t"),
        (R2, "A"): ["8.8.8.8"], (R2, "AAAA"): [],
    })
    with pytest.raises(BlockedAddressError, match="real address 127.0.0.1"):
        await _fetch("http://rebind.example/", ["198.18.3.3"], doh)


@pytest.mark.asyncio
async def test_private_aaaa_after_a_timeout_blocks(configured, resolvers):
    doh = _doh({
        (R1, "A"): httpx.ReadTimeout("t"), (R1, "AAAA"): ["::1"],
        (R2, "A"): ["8.8.8.8"], (R2, "AAAA"): [],
    })
    with pytest.raises(BlockedAddressError, match="real address ::1"):
        await _fetch("http://rebind.example/", ["198.18.3.3"], doh)


@pytest.mark.asyncio
async def test_partial_public_answer_then_full_public_allowed(configured, resolvers):
    doh = _doh({
        (R1, "A"): ["8.8.4.4"], (R1, "AAAA"): httpx.ReadTimeout("t"),
        (R2, "A"): ["8.8.8.8"], (R2, "AAAA"): ["2001:4860:4860::8888"],
    })
    await _fetch("https://www.sec.gov/", ["198.18.1.238"], doh)
    assert doh.await_count == 4


@pytest.mark.asyncio
async def test_partial_public_answer_alone_does_not_allow(configured, resolvers):
    doh = _doh({
        (R1, "A"): ["8.8.4.4"], (R1, "AAAA"): httpx.ReadTimeout("t"),
        (R2, "A"): httpx.ConnectError("x"), (R2, "AAAA"): ["2001:4860:4860::8888"],
    })
    with pytest.raises(BlockedAddressError, match="no resolver gave a complete answer"):
        await _fetch("https://www.sec.gov/", ["198.18.1.238"], doh)


@pytest.mark.asyncio
async def test_nxdomain_on_one_type_falls_back(configured, resolvers):
    nx = dalton_network.RealResolutionError("answered status 3")
    doh = _doh({
        (R1, "A"): ["8.8.4.4"], (R1, "AAAA"): nx,
        (R2, "A"): ["8.8.8.8"], (R2, "AAAA"): [],
    })
    await _fetch("https://www.sec.gov/", ["198.18.1.238"], doh)


@pytest.mark.asyncio
async def test_nxdomain_everywhere_blocks(configured, resolvers):
    nx = dalton_network.RealResolutionError("answered status 3")
    doh = _doh({(r, t): nx for r in (R1, R2) for t in ("A", "AAAA")})
    with pytest.raises(BlockedAddressError, match="real address lookup"):
        await _fetch("https://nx.example/", ["198.18.1.238"], doh)


@pytest.mark.asyncio
async def test_no_resolvers_configured_blocks(configured, monkeypatch):
    monkeypatch.setattr("src.dalton.network.get_fake_ip_real_resolvers", list)
    with pytest.raises(BlockedAddressError, match="real address lookup"):
        await _fetch("https://www.sec.gov/", ["198.18.1.238"], _doh({}))


@pytest.mark.asyncio
async def test_fake_ip_literal_blocked(configured, resolvers):
    doh = _doh({(R1, "A"): ["8.8.8.8"]})
    with pytest.raises(BlockedAddressError, match="fake-IP literal"):
        await GuardedAsyncTransport().handle_async_request(httpx.Request("GET", "http://198.18.1.238/"))
    doh.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_fake_public_host_skips_doh(configured, resolvers):
    doh = _doh({})
    await _fetch("https://example.com/", ["93.184.216.34"], doh)
    doh.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_blocks_loopback_when_configured(configured):
    with pytest.raises(BlockedAddressError, match="private/reserved"):
        await GuardedAsyncTransport().handle_async_request(
            httpx.Request("GET", "http://127.0.0.1:8000/health")
        )


@pytest.mark.asyncio
async def test_doh_query_parses_json_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "www.sec.gov"
        assert request.url.params["type"] == "A"
        return httpx.Response(200, json={"Status": 0, "Answer": [
            {"type": 5, "data": "www.sec.gov.edgekey.net."},
            {"type": 1, "data": "23.1.2.3"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await dalton_network.doh_query(client, R1, "www.sec.gov", "A") == ["23.1.2.3"]


@pytest.mark.asyncio
async def test_doh_query_nonzero_status_raises():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"Status": 3}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(dalton_network.RealResolutionError):
            await dalton_network.doh_query(client, R1, "nx.example", "A")
