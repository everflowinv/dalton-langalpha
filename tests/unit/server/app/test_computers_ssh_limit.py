"""The SSH computer limit refuses the steps before a create as well.

The pairing command puts the key on a machine and the probe dials one, both
for a computer the create would refuse anyway.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from ptc_agent.config.core import SSHConfig
from src.server.app import computers_ssh
from tests.conftest import create_test_app

_HOST = {"host": "203.0.113.10", "port": 22, "username": "dev"}


@pytest.fixture(autouse=True)
def ssh_offered():
    with patch.object(
        computers_ssh,
        "_ssh_settings",
        AsyncMock(return_value=SSHConfig(max_computers_per_user=3)),
    ):
        yield


@pytest_asyncio.fixture
async def client():
    app = create_test_app(computers_ssh.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body", "owner", "step"),
    [
        ("pairings", None, computers_ssh.ssh_pairing, "create_pairing"),
        ("probe", _HOST, computers_ssh, "probe"),
    ],
    ids=["pairing", "probe"],
)
async def test_a_user_at_the_limit_is_refused_before_any_machine_is_touched(
    client, route, body, owner, step
):
    touched = AsyncMock()
    with (
        patch(
            "src.server.services.ssh_computers.count_live_computers_of_kind",
            AsyncMock(return_value=3),
        ),
        patch.object(owner, step, touched),
    ):
        answer = await client.post(f"/api/v1/computers/ssh/{route}", json=body)

    assert answer.status_code == 409
    assert answer.json()["detail"] == {
        "code": "ssh_computer_limit",
        "message": "You can have up to 3 SSH computers. Remove one to add another.",
        "limit": 3,
    }
    touched.assert_not_awaited()
