"""What a removed ssh computer leaves on its machine: the folders its owner
asked to have deleted, the key's line and the tunnel.

The outcome reaches the owner as is, so each one is pinned to what happened
on the machine, and nothing here may fail a removal that already happened.
"""

from __future__ import annotations

import contextlib
import shlex
from typing import Any
from unittest.mock import AsyncMock

import asyncssh
import pytest

from ptc_agent.config.core import SSHConfig
from ptc_agent.core.sandbox.providers._ssh_authorized_key import remove_script
from ptc_agent.core.sandbox.providers._ssh_shell import home_shared_script
from ptc_agent.core.sandbox.providers._ssh_transport import ScriptResult
from src.server.services import ssh_removal, ssh_tunnel_sweep

_HOST_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)
_PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGjvFCxPkqvLhGmK1FKCXvG1YwVYW0NQ5vJWbg2pDcJw"
    " langalpha"
)
_FINGERPRINT = "SHA256:abc/def+ghi"
USER = "user-1"


def _computer(
    computer_id="c-1",
    *,
    username="dev",
    tunnel="",
    name=None,
    host="203.0.113.10",
    host_key=_HOST_KEY,
):
    return {
        "computer_id": computer_id,
        "name": name or computer_id,
        "kind": "ssh",
        "status": "running",
        "provider_ref": f"ref-{computer_id}",
        "root_dir": f"/home/{username}/{computer_id}",
        "provider_config": {
            "host": host,
            "port": 22,
            "username": username,
            "host_key": host_key,
            "key_owner": USER,
            "tunnel": tunnel,
        },
    }


class _Link:
    def __init__(self, *answers: Any) -> None:
        self.scripts: list[str] = []
        self._answers = list(answers)

    async def run(self, script: str, *, timeout: float, **_: Any) -> ScriptResult:
        self.scripts.append(script)
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _said(text: str, exit_code: int = 0) -> ScriptResult:
    return ScriptResult(exit_code=exit_code, stdout=text.encode(), stderr=b"")


@pytest.fixture
def machine(monkeypatch):
    """The removed computer's machine: its open connection, the user's key and
    their other computers, and the tunnel release, recorded in order. The
    release runs the real uninstall on the connection unless ``due`` is off,
    as when another computer still uses the tunnel, or ``fails`` is on, as
    when its claim could not be read."""
    state: dict[str, Any] = {
        "link": None,
        "others": [],
        "events": [],
        "due": True,
        "fails": False,
        "owed": [],
        "completed": [],
    }

    @contextlib.asynccontextmanager
    async def _removal_link(config):
        state["events"].append(("link", config.username, config.tunnel))
        yield state["link"]
        state["events"].append("closed")

    async def _release(user_id, computer, *, settings, link, ahead=None):
        state["events"].append(("release", computer["computer_id"], link))
        if state["fails"]:
            return False
        if not state["due"]:
            return True
        with contextlib.suppress(Exception):  # as the real release swallows it
            await ssh_tunnel_sweep.uninstall_tunnel(
                computer["provider_config"]["tunnel"],
                key_owner=USER,
                login=ssh_tunnel_sweep.TunnelLogin(
                    host="box", port=22, username="dev", host_key=_HOST_KEY
                ),
                settings=settings,
                link=link,
                ahead=ahead,
            )
        return True

    monkeypatch.setattr(ssh_removal.ssh_provider, "removal_link", _removal_link)
    monkeypatch.setattr(
        ssh_removal,
        "get_user_ssh_public_key",
        AsyncMock(
            return_value={"public_key": _PUBLIC_KEY, "fingerprint": _FINGERPRINT}
        ),
    )
    monkeypatch.setattr(
        ssh_removal,
        "get_computers_for_user",
        AsyncMock(side_effect=lambda _user: state["others"]),
    )
    monkeypatch.setattr(ssh_removal, "release_tunnel", _release)

    async def _owed(computer_id):
        state["events"].append(("owed", computer_id))
        return state["owed"]

    async def _complete(workspace_id, *, computer_id, dir_name):
        state["completed"].append((workspace_id, computer_id, dir_name))
        return True

    monkeypatch.setattr(ssh_removal, "get_folder_cleanups_owed_by", _owed)
    monkeypatch.setattr(ssh_removal, "complete_workspace_folder_cleanup", _complete)
    return state


async def _leave(computer, *, remove_key=True):
    return await ssh_removal.leave_machine(
        USER, computer, settings=SSHConfig(), remove_key=remove_key
    )


@pytest.mark.asyncio
async def test_the_key_goes_on_the_deletes_connection(machine):
    machine["link"] = link = _Link(_said("removed\n"))

    left = await _leave(_computer())

    assert left.key.model_dump() == {
        "outcome": "removed",
        "used_by": None,
        "fingerprint": _FINGERPRINT,
        "comment": "langalpha",
    }
    assert link.scripts == [remove_script(_PUBLIC_KEY)]
    assert machine["events"] == [("link", "dev", ""), ("owed", "c-1"), "closed"]
    assert left.tunnel_released is False


@pytest.mark.asyncio
async def test_another_computer_on_the_login_keeps_it_and_is_named(machine):
    machine["link"] = link = _Link()
    machine["others"] = [_computer("c-2", name="Studio Mac")]

    left = await _leave(_computer())

    assert (left.key.outcome, left.key.used_by) == ("kept", "Studio Mac")
    assert link.scripts == []


@pytest.mark.asyncio
async def test_another_login_on_the_machine_does_not_keep_it(machine):
    machine["link"] = _Link(_said("removed\n"))
    machine["others"] = [_computer("c-2", username="root")]

    left = await _leave(_computer())

    assert left.key.outcome == "removed"


@pytest.mark.asyncio
async def test_a_computer_on_another_host_sharing_the_home_keeps_it(machine):
    """Hosts that mount one home read one authorized_keys; the machine says
    whose folder it found there, and that computer is named."""
    machine["link"] = link = _Link(_said("shared ref-c-2\n"))
    machine["others"] = [
        _computer("c-2", name="Node 2", host="203.0.113.11", host_key="ssh-ed25519 B")
    ]

    left = await _leave(_computer())

    assert (left.key.outcome, left.key.used_by) == ("kept", "Node 2")
    assert link.scripts == [
        home_shared_script(["ref-c-2"]) + remove_script(_PUBLIC_KEY)
    ]


@pytest.mark.asyncio
async def test_a_machine_the_delete_could_not_reach_is_not_dialed(machine):
    left = await _leave(_computer())

    assert left.key.outcome == "unreached"
    assert left.key.fingerprint == _FINGERPRINT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        _said("skipped\n"),
        _said("absent\n"),
        _said("", exit_code=1),
        _said("", exit_code=None),
    ],
    ids=["not-ours-to-change", "another-file", "failed", "no-status"],
)
async def test_a_key_the_script_left_is_skipped(machine, answer):
    machine["link"] = _Link(answer)

    left = await _leave(_computer())

    assert left.key.outcome == "skipped"
    assert (left.key.fingerprint, left.key.comment) == (_FINGERPRINT, "langalpha")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [TimeoutError(), asyncssh.DisconnectError(11, "bye"), ConnectionResetError()],
    ids=["timeout", "disconnect", "reset"],
)
async def test_a_connection_lost_under_the_script_is_unreached(machine, error):
    machine["link"] = _Link(error)

    left = await _leave(_computer())

    assert left.key.outcome == "unreached"


def _with_uninstall(script: str) -> str:
    return f"(\n{script})\n{ssh_tunnel_sweep._uninstall_sh('t-1')}"


@pytest.mark.asyncio
async def test_the_key_goes_in_the_tunnels_uninstall_just_ahead_of_it(machine):
    """One command: a channel the machine refuses keeps both, and the sweep
    can still log in to take the tunnel away later."""
    machine["link"] = link = _Link(_said("removed\n"))

    left = await _leave(_computer(tunnel="t-1"))

    assert left.key.outcome == "removed"
    assert left.tunnel_released is True
    assert machine["events"] == [
        ("link", "dev", "t-1"),
        ("owed", "c-1"),
        ("release", "c-1", link),
        "closed",
    ]
    assert link.scripts == [_with_uninstall(remove_script(_PUBLIC_KEY))]


@pytest.mark.asyncio
async def test_an_uninstall_that_never_answered_is_not_followed_by_the_key(machine):
    machine["link"] = link = _Link(asyncssh.ChannelOpenError(4, "no more sessions"))

    left = await _leave(_computer(tunnel="t-1"))

    assert left.key.outcome == "unreached"
    assert link.scripts == [_with_uninstall(remove_script(_PUBLIC_KEY))]


@pytest.mark.asyncio
async def test_a_tunnel_another_computer_keeps_leaves_the_key_on_its_own(machine):
    machine["link"] = link = _Link(_said("removed\n"))
    machine["due"] = False

    left = await _leave(_computer(tunnel="t-1"))

    assert left.key.outcome == "removed"
    assert link.scripts == [remove_script(_PUBLIC_KEY)]


@pytest.mark.asyncio
async def test_a_release_that_failed_keeps_the_key_its_retry_logs_in_with(machine):
    """Its uninstall never went out, so the release after it logs in anew."""
    machine["link"] = link = _Link()
    machine["fails"] = True

    left = await _leave(_computer(tunnel="t-1"))

    assert left.key.outcome == "skipped"
    assert link.scripts == []
    assert left.tunnel_released is False


@pytest.mark.asyncio
async def test_a_tunnel_without_a_connection_is_left_to_a_new_login(machine):
    left = await _leave(_computer(tunnel="t-1"))

    assert left.tunnel_released is False
    assert not any(e[0] == "release" for e in machine["events"] if isinstance(e, tuple))


@pytest.mark.asyncio
async def test_a_key_kept_on_request_is_not_touched(machine):
    machine["link"] = link = _Link(_said(""))

    left = await _leave(_computer(tunnel="t-1"), remove_key=False)

    assert left.key is None
    assert link.scripts == [ssh_tunnel_sweep._uninstall_sh("t-1")]
    assert left.tunnel_released is True


@pytest.mark.asyncio
async def test_a_failure_after_the_delete_never_fails_the_removal(machine):
    machine["link"] = _Link()
    ssh_removal.get_computers_for_user.side_effect = RuntimeError("database down")

    left = await _leave(_computer(tunnel="t-1"))

    assert left.key.outcome == "skipped"
    # Released again later, by a release that claims the tunnel first.
    assert left.tunnel_released is False


def _rm(*folders: str) -> str:
    """Each folder renamed out of its name, then only what moved deleted."""
    leftovers = "/home/dev/c-1/_internal/leftovers"
    script = [f"t=$(mkdir -p {leftovers} && mktemp -d {leftovers}/removed.XXXXXX) || exit 1"]
    for i, f in enumerate(folders):
        q = shlex.quote(f"/home/dev/c-1/{f}")
        script.append(f'if [ ! -e {q} ] && [ ! -L {q} ] || mv -- {q} "$t/{i}"; then echo {i}; fi')
    script.append('nohup rm -rf -- "$t" >/dev/null 2>&1 &')
    return "\n".join(script) + "\n"


@pytest.mark.asyncio
async def test_folders_deleted_while_it_was_stopped_go_before_the_tunnel(machine):
    """Nothing else would take them: the reaper runs on a running computer only,
    and the tunnel's uninstall may close the connection they need."""
    machine["owed"] = [
        {"workspace_id": "w-1", "dir_name": "Q3 research"},
        {"workspace_id": "w-2", "dir_name": "_internal/leftovers/w-2"},
    ]
    machine["link"] = link = _Link(_said("0\n1\n"), _said("removed\n"))

    left = await _leave(_computer(tunnel="t-1"), remove_key=True)

    assert link.scripts == [
        _rm("Q3 research", "_internal/leftovers/w-2"),
        _with_uninstall(remove_script(_PUBLIC_KEY)),
    ]
    assert machine["completed"] == [
        ("w-1", "c-1", "Q3 research"),
        ("w-2", "c-1", "_internal/leftovers/w-2"),
    ]
    assert left.key.outcome == "removed"


@pytest.mark.asyncio
async def test_folders_go_with_the_key_kept_too(machine):
    machine["owed"] = [{"workspace_id": "w-1", "dir_name": "Q3 research"}]
    machine["link"] = link = _Link(_said("0\n"))

    await _leave(_computer(), remove_key=False)

    assert link.scripts == [_rm("Q3 research")]
    assert machine["completed"] == [("w-1", "c-1", "Q3 research")]


@pytest.mark.asyncio
@pytest.mark.parametrize("dir_name", [".", "..", "a/b", "_internal/leftovers/w-9"])
async def test_a_folder_that_would_name_the_root_or_another_is_left(machine, dir_name):
    machine["owed"] = [{"workspace_id": "w-1", "dir_name": dir_name}]
    machine["link"] = link = _Link(_said("removed\n"))

    await _leave(_computer())

    assert link.scripts == [remove_script(_PUBLIC_KEY)]
    assert machine["completed"] == []


@pytest.mark.asyncio
async def test_a_folder_the_machine_would_not_move_stays_owed(machine):
    """Deleted where it is, it could take a new workspace's files with it."""
    machine["owed"] = [
        {"workspace_id": "w-1", "dir_name": "Q3 research"},
        {"workspace_id": "w-2", "dir_name": "Macro"},
        {"workspace_id": "w-3", "dir_name": "a/b"},
    ]
    machine["link"] = link = _Link(_said("1\n"), _said("removed\n"))

    left = await _leave(_computer())

    assert link.scripts[0] == _rm("Q3 research", "Macro")
    assert machine["completed"] == [("w-2", "c-1", "Macro")]
    assert left.key.outcome == "removed"


@pytest.mark.asyncio
async def test_a_place_to_move_them_the_machine_refused_leaves_them_owed(machine):
    machine["owed"] = [{"workspace_id": "w-1", "dir_name": "Q3 research"}]
    machine["link"] = _Link(_said("", exit_code=1), _said("removed\n"))

    left = await _leave(_computer())

    assert machine["completed"] == []
    assert left.key.outcome == "removed"


@pytest.mark.asyncio
async def test_folders_that_could_not_be_read_still_let_the_key_go(machine, monkeypatch):
    monkeypatch.setattr(
        ssh_removal,
        "get_folder_cleanups_owed_by",
        AsyncMock(side_effect=RuntimeError("database down")),
    )
    machine["link"] = link = _Link(_said("removed\n"))

    left = await _leave(_computer())

    assert link.scripts == [remove_script(_PUBLIC_KEY)]
    assert left.key.outcome == "removed"


@pytest.mark.asyncio
async def test_a_connection_lost_under_the_delete_leaves_them_owed(machine):
    machine["owed"] = [
        {"workspace_id": "w-1", "dir_name": "Q3 research"},
        {"workspace_id": "w-2", "dir_name": "Macro"},
    ]
    machine["link"] = link = _Link(
        asyncssh.DisconnectError(11, "bye"), ConnectionResetError()
    )

    left = await _leave(_computer())

    assert link.scripts == [_rm("Q3 research", "Macro"), remove_script(_PUBLIC_KEY)]
    assert machine["completed"] == []
    assert left.key.outcome == "unreached"


@pytest.mark.asyncio
async def test_a_machine_the_delete_could_not_reach_keeps_its_folders_owed(machine):
    machine["owed"] = [{"workspace_id": "w-1", "dir_name": "Q3 research"}]

    await _leave(_computer())

    assert machine["completed"] == []


@pytest.mark.asyncio
async def test_an_uninstall_on_an_open_connection_logs_in_to_nothing(monkeypatch):
    """The key it would log in with may have just gone."""
    dial = AsyncMock()
    monkeypatch.setattr(ssh_tunnel_sweep.ssh_provider, "run_script", dial)
    link = _Link(_said(""))

    await ssh_tunnel_sweep.uninstall_tunnel(
        "t-1",
        key_owner=USER,
        login=ssh_tunnel_sweep.TunnelLogin(
            host="box", port=22, username="dev", host_key=_HOST_KEY
        ),
        settings=SSHConfig(),
        link=link,
    )

    assert link.scripts == [ssh_tunnel_sweep._uninstall_sh("t-1")]
    dial.assert_not_awaited()
