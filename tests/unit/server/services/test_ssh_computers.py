import pytest

from ptc_agent.core.sandbox.providers._ssh_authorized_key import authorized_key_line

from src.server.services.ssh_computers import (
    computer_sharing_folder,
    computer_sharing_login,
    suggested_root_dir,
)

_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)


def _ssh(root_dir, *, host="203.0.113.10", port=22, host_key=_KEY, kind="ssh"):
    return {
        "kind": kind,
        "name": "box",
        "root_dir": root_dir,
        "provider_config": {"host": host, "port": port, "host_key": host_key},
    }


@pytest.mark.parametrize(
    "root_dir",
    ["/home/ubuntu/langalpha", "/home/ubuntu/langalpha/sub", "/home/ubuntu"],
)
def test_a_folder_in_or_around_anothers_is_refused(root_dir):
    sibling = _ssh("/home/ubuntu/langalpha")

    found = computer_sharing_folder(
        [sibling], host="203.0.113.10", port=22, host_key=_KEY, root_dir=root_dir
    )

    assert found is sibling


def test_a_name_prefix_is_not_a_parent():
    found = computer_sharing_folder(
        [_ssh("/home/ubuntu/langalpha")],
        host="203.0.113.10",
        port=22,
        host_key=_KEY,
        root_dir="/home/ubuntu/langalpha-2",
    )

    assert found is None


def test_the_same_machine_by_another_name_still_counts():
    # The pinned host key identifies it however the user spelled the address.
    found = computer_sharing_folder(
        [_ssh("/srv/la", host="box.example.com")],
        host="203.0.113.10",
        port=22,
        host_key=_KEY,
        root_dir="/srv/la",
    )

    assert found is not None


def test_other_machines_and_hosted_computers_are_ignored():
    found = computer_sharing_folder(
        [
            _ssh("/srv/la", host="198.51.100.7", host_key="ssh-ed25519 AAAAother"),
            _ssh("/home/workspace", kind="daytona"),
        ],
        host="203.0.113.10",
        port=22,
        host_key=_KEY,
        root_dir="/srv/la",
    )

    assert found is None


def test_a_new_computer_works_in_langalpha_in_the_home():
    assert (
        suggested_root_dir(
            [], home="/home/ubuntu", host_key=_KEY, host="203.0.113.10", port=22
        )
        == "/home/ubuntu/langalpha"
    )


def test_a_folder_another_computer_uses_is_passed_over():
    assert (
        suggested_root_dir(
            [_ssh("/home/ubuntu/langalpha")],
            home="/home/ubuntu",
            host_key=_KEY,
            host="203.0.113.10",
            port=22,
        )
        == "/home/ubuntu/langalpha-2"
    )


def test_a_leading_double_slash_names_the_same_folder():
    # normpath keeps "//", which the nesting check would read as another tree.
    from src.server.models.computer import ComputerSSHSpec

    spec = ComputerSSHSpec(
        host="203.0.113.10",
        port=22,
        username="ubuntu",
        host_key=_KEY,
        root_dir="//home/ubuntu/langalpha/sub/",
    )

    assert spec.root_dir == "/home/ubuntu/langalpha/sub"
    assert (
        computer_sharing_folder(
            [_ssh("/home/ubuntu/langalpha")],
            host="203.0.113.10",
            port=22,
            host_key=_KEY,
            root_dir=spec.root_dir,
        )
        is not None
    )


def _login(computer_id, *, username="ubuntu", tunnel="", status="running", **target):
    computer = _ssh("/home/ubuntu/langalpha", **target)
    computer["provider_config"].update(username=username, tunnel=tunnel)
    return {**computer, "computer_id": computer_id, "name": computer_id, "status": status}


@pytest.mark.parametrize(
    "other",
    [
        _login("b"),
        _login("b", host="box.example.com"),
        _login("b", host_key="ssh-ed25519 AAAArekeyed"),
    ],
    ids=["same-target", "another-address", "another-host-key"],
)
def test_another_computer_on_the_login_keeps_the_key(other):
    """The host key or the address is enough, as for a folder: a clone taken
    for the same machine only leaves the key in place."""
    removed = _login("a")

    assert computer_sharing_login([removed, other], removed) is other


@pytest.mark.parametrize(
    "other",
    [
        _login("b", username="root"),
        _login("b", host="198.51.100.7", host_key="ssh-ed25519 AAAAother"),
        _login("b", status="deleted"),
        {**_login("b"), "kind": "daytona"},
    ],
    ids=["another-user", "another-machine", "deleted", "hosted"],
)
def test_nothing_else_keeps_it(other):
    removed = _login("a")

    assert computer_sharing_login([removed, other], removed) is None


def test_a_tunneled_machine_counts_by_its_tunnel_not_its_name():
    """Its host is only a name, which another machine may have too."""
    removed = _login("a", tunnel="t-1", host_key="ssh-ed25519 AAAAone")
    same = _login("b", tunnel="t-1", host_key="ssh-ed25519 AAAArekeyed")
    namesake = _login("c", tunnel="t-2", host_key="ssh-ed25519 AAAAtwo")

    assert computer_sharing_login([namesake, same], removed) is same
    assert computer_sharing_login([namesake], removed) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "held", "refused"),
    [(20, 19, False), (20, 20, True), (1, 1, True), (0, 500, False)],
    ids=["under", "at", "one", "no-limit"],
)
async def test_the_limit_counts_live_ssh_computers(monkeypatch, limit, held, refused):
    from unittest.mock import AsyncMock

    from ptc_agent.config.core import SSHConfig
    from src.server.services import ssh_computers

    count = AsyncMock(return_value=held)
    monkeypatch.setattr(ssh_computers, "count_live_computers_of_kind", count)
    settings = SSHConfig(max_computers_per_user=limit)

    if not refused:
        await ssh_computers.check_ssh_capacity("user-1", settings)
        return
    with pytest.raises(ssh_computers.SSHComputerLimit) as caught:
        await ssh_computers.check_ssh_capacity("user-1", settings)
    count.assert_awaited_once_with("user-1", "ssh")
    assert caught.value.detail() == {
        "code": "ssh_computer_limit",
        "message": str(caught.value),
        "limit": limit,
    }
    assert str(caught.value) == (
        "You can have only 1 SSH computer. Remove it to add another."
        if limit == 1
        else f"You can have up to {limit} SSH computers. Remove one to add another."
    )


@pytest.mark.parametrize(
    "existing",
    [None, b"", b"ssh-rsa AAAAold old@host", b"ssh-rsa AAAAold old@host\n"],
    ids=["no-file", "empty", "no-trailing-newline", "trailing-newline"],
)
def test_the_key_command_adds_one_line_whatever_the_file_holds(tmp_path, existing):
    """A file whose last line lacks its newline would otherwise have the key
    glued onto that line, and neither key would work."""
    import subprocess

    from src.server.services.ssh_computers import authorized_keys_command

    key = authorized_key_line(f"{_KEY} langalpha")
    keys = tmp_path / ".ssh" / "authorized_keys"
    if existing is not None:
        keys.parent.mkdir()
        keys.write_bytes(existing)
    command = authorized_keys_command(key)

    for _ in range(2):
        subprocess.run(["sh", "-c", command], env={"HOME": str(tmp_path)}, check=True)

    old = [existing.decode().strip()] if existing else []
    assert keys.read_text() == "".join(f"{line}\n" for line in [*old, key])
    assert keys.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("login_from", [[], ["203.0.113.7", "2001:db8::/32"]])
@pytest.mark.parametrize("shell", ["sh", "dash", "bash", "zsh", "csh", "tcsh", "fish"])
def test_the_key_command_writes_the_line_as_is_from_any_login_shell(
    tmp_path, shell, login_from
):
    """The user pastes the command into whatever shell their login has, and
    the line's ``from="..."`` holds quotes each must pass through untouched."""
    import shutil
    import subprocess

    from src.server.services.ssh_computers import authorized_keys_command

    path = shutil.which(shell)
    if path is None:
        pytest.skip(f"{shell} is not installed")
    line = authorized_key_line(f"{_KEY} langalpha", login_from)

    subprocess.run(
        [path, "-c", authorized_keys_command(line)],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        check=True,
    )

    assert (tmp_path / ".ssh" / "authorized_keys").read_text() == f"{line}\n"


@pytest.mark.asyncio
async def test_a_host_key_that_does_not_parse_is_refused_before_dialing(monkeypatch):
    """It matches the model's shape check, then fails to parse in the dial as
    a ValueError, which create would answer as a 404. Nothing is dialed, so
    none of the hour's dials is spent."""
    from unittest.mock import AsyncMock

    from ptc_agent.config.core import SSHConfig
    from ptc_agent.core.sandbox.providers.ssh_errors import SSHConnectError
    from src.server.models.computer import ComputerSSHSpec
    from src.server.services import ssh_computers

    dial = AsyncMock()
    spend = AsyncMock()
    monkeypatch.setattr(ssh_computers, "probe", dial)
    monkeypatch.setattr(ssh_computers, "spend_dials", spend)
    monkeypatch.setattr(ssh_computers, "has_client_key", AsyncMock(return_value=True))
    monkeypatch.setattr(ssh_computers, "client_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(
        ssh_computers, "count_live_computers_of_kind", AsyncMock(return_value=0)
    )
    spec = ComputerSSHSpec(
        host="203.0.113.10", port=22, username="dev", host_key="ssh-ed25519 AAAA"
    )

    with pytest.raises(SSHConnectError) as caught:
        await ssh_computers.plan_create("user-1", spec, settings=SSHConfig())

    assert caught.value.code == "host_key_invalid"
    dial.assert_not_awaited()
    spend.assert_not_awaited()


def test_a_listing_fingerprints_a_key_as_the_provider_does():
    """The listing works it out without the provider's key parser, so the two
    must not drift: the dialog compares one with the other."""
    from ptc_agent.core.sandbox.providers import ssh
    from src.server.services.ssh_computers import host_key_fingerprint

    minted = ssh.generate_client_key("langalpha").public_key
    for line in (_KEY, f"{_KEY} root@box", minted):
        assert host_key_fingerprint(line) == ssh.public_key_fingerprint(line)
    assert host_key_fingerprint("ssh-ed25519 not*base64") is None
