"""The agent's glob, run as the sandbox runs it against a real directory tree."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from ptc_agent.core.sandbox.files import aglob_files
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox


class _LocalRuntime:
    """Runs the shipped glob script with this interpreter."""

    async def exec(self, command: str, timeout: int = 60):
        command = command.replace("python3", sys.executable, 1)
        done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
        return SimpleNamespace(
            stdout=done.stdout, stderr=done.stderr, exit_code=done.returncode
        )


def _sandbox() -> SimpleNamespace:
    async def _call(fn, *args, retry_policy=None, **kwargs):
        return await fn(*args, **kwargs)

    return SimpleNamespace(
        _wait_ready=create_autospec(PTCSandbox, instance=True)._wait_ready,
        config=SimpleNamespace(
            filesystem=SimpleNamespace(enable_path_validation=False)
        ),
        _normalize_search_path=lambda path: path,
        runtime=_LocalRuntime(),
        _runtime_call=_call,
    )


@pytest.mark.asyncio
async def test_a_folder_named_with_glob_characters_is_searched_literally(tmp_path):
    """A workspace folder is its name, so a glob rooted in one reads it literally."""
    folder = tmp_path / "Q3 [draft] *?"
    (folder / "results").mkdir(parents=True)
    (folder / "results" / "report.md").write_text("x")

    matches = await aglob_files(_sandbox(), "results/*.md", str(folder))

    assert matches == [str(folder / "results" / "report.md")]


@pytest.mark.asyncio
async def test_history_is_hidden_until_the_path_or_pattern_names_it(tmp_path):
    """History dirs count only as children of ``.agents``: a workspace's own
    ``threads/`` stays visible."""
    for rel in (
        "src/a.json",
        "threads/own.json",
        ".agents/skills/s/SKILL.md",
        ".agents/threads/abc/transcript/turn-0001.json",
        ".agents/large_tool_results/call_1.json",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}")

    async def glob(pattern, path="", hide=True):
        found = await aglob_files(
            _sandbox(), pattern, str(tmp_path / path), hide_history=hide
        )
        return sorted(f.removeprefix(f"{tmp_path}/") for f in found)

    assert await glob("*.json") == ["src/a.json", "threads/own.json"]
    assert await glob("**/*", ".agents") == [".agents/skills/s/SKILL.md"]
    assert await glob(".agents/threads/*/transcript/*.json") == [
        ".agents/threads/abc/transcript/turn-0001.json"
    ]
    assert await glob("*.json", ".agents/large_tool_results") == [
        ".agents/large_tool_results/call_1.json"
    ]
    assert len(await glob("**/*.json", hide=False)) == 4


@pytest.mark.asyncio
async def test_a_wildcard_never_walks_into_the_file_mount(tmp_path, monkeypatch):
    """Every stat below the mount is a server request, so only a pattern or
    path that names a mounted folder reads it."""
    from ptc_agent.core.sandbox import files

    mount = tmp_path / "mnt"
    (mount / "memory").mkdir(parents=True)
    (mount / "memory" / "notes.csv").write_text("x")
    (mount / "computer").mkdir()
    (mount / "computer" / "threads.csv").write_text("x")
    workspace = tmp_path / "ws"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "a.csv").write_text("x")
    (workspace / ".agents").mkdir()
    (workspace / ".agents" / "memory").symlink_to(mount / "memory")
    (workspace / ".agents" / "threads.csv").symlink_to(mount / "computer" / "threads.csv")
    monkeypatch.setattr(files, "MOUNT", str(mount))

    async def glob(pattern, path=""):
        found = await aglob_files(_sandbox(), pattern, str(workspace / path))
        return sorted(f.removeprefix(f"{workspace}/") for f in found)

    assert await glob("**/*.csv") == ["data/a.csv"]
    assert await glob("**/*") == ["data/a.csv"]
    assert await glob("*/*/*.csv") == []
    assert await glob("**/memory/*.csv") == []
    assert await glob(".agents/memory/*.csv") == [".agents/memory/notes.csv"]
    assert await glob("*.csv", ".agents/memory") == [".agents/memory/notes.csv"]


@pytest.mark.asyncio
async def test_a_link_reaching_the_mount_another_way_is_still_the_mount(
    tmp_path, monkeypatch
):
    """A link to a link into the mount, or straight to the generation the
    mount link points at, costs the same requests as a link into the mount."""
    from ptc_agent.core.sandbox import files

    generation = tmp_path / ".mnt" / "1"
    (generation / "memory").mkdir(parents=True)
    (generation / "memory" / "notes.csv").write_text("x")
    mount = tmp_path / "mnt"
    mount.symlink_to(generation)
    workspace = tmp_path / "ws"
    (workspace / "data").mkdir(parents=True)
    (workspace / "data" / "a.csv").write_text("x")
    (workspace / "direct").symlink_to(mount / "memory")
    (workspace / "alias").symlink_to("direct")
    (workspace / "generation").symlink_to(generation / "memory")
    monkeypatch.setattr(files, "MOUNT", str(mount))

    found = await aglob_files(_sandbox(), "*/*.csv", str(workspace))

    assert found == [str(workspace / "data" / "a.csv")]


def test_expand_braces_nested_and_commas():
    from ptc_agent.core.sandbox.glob_runtime import expand_braces

    assert expand_braces("**/{debate,thesis,drivers,agenda}.json") == [
        "**/debate.json",
        "**/thesis.json",
        "**/drivers.json",
        "**/agenda.json",
    ]
    # Nested braces and empty alternatives
    assert expand_braces("data/{a,{b,c},}.json") == [
        "data/a.json",
        "data/b.json",
        "data/c.json",
        "data/.json",
    ]
    # Multiple brace groups in one pattern
    assert expand_braces("{data,notes}/{thesis,drivers}.{json,md}") == [
        "data/thesis.json",
        "data/thesis.md",
        "data/drivers.json",
        "data/drivers.md",
        "notes/thesis.json",
        "notes/thesis.md",
        "notes/drivers.json",
        "notes/drivers.md",
    ]
    # Single item without comma or unclosed brace is left untouched
    assert expand_braces("**/{thesis}.json") == ["**/{thesis}.json"]
    assert expand_braces("**/{thesis,drivers.json") == ["**/{thesis,drivers.json"]
    # Commas inside character classes [...] do not trigger brace expansion
    assert expand_braces("data/[a,b]{1,2}.json") == [
        "data/[a,b]1.json",
        "data/[a,b]2.json",
    ]
    assert expand_braces("data/{[a,b],c}.json") == [
        "data/[a,b].json",
        "data/c.json",
    ]


@pytest.mark.asyncio
async def test_glob_brace_expansion_deduplicates_and_preserves_mtime_sort(tmp_path):
    import os

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    f_debate = data_dir / "debate.json"
    f_thesis = data_dir / "thesis.json"
    f_drivers = data_dir / "drivers.json"
    f_agenda = data_dir / "agenda.json"
    f_other = data_dir / "company.json"

    for idx, f in enumerate([f_debate, f_thesis, f_drivers, f_agenda, f_other], start=1):
        f.write_text("{}")
        ts = 1_700_000_000 + idx * 10
        os.utime(f, (ts, ts))

    found = await aglob_files(
        _sandbox(),
        "**/{debate,thesis,drivers,agenda,thesis,*s}.json",
        str(tmp_path),
    )

    assert found == [
        str(f_agenda),
        str(f_drivers),
        str(f_thesis),
        str(f_debate),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern",
    [
        "{*.json,notes/*.json}",
        "{thesis.schema.json,unused/path.json}",
        "**/{debate,thesis}.json",
        "*.{md,json}",
        "data/{a,b}.json",
    ],
)
async def test_glob_sandbox_and_diskglob_parity(tmp_path, pattern):
    # Upstream expands braces at the caller (composite.py) and hands each
    # expanded pattern its own DiskGlob; mirror that here.
    from ptc_agent.agent.backends.search_match import DiskGlob
    from ptc_agent.core.sandbox.glob_runtime import expand_braces

    files = [
        tmp_path / "root.json",
        tmp_path / "readme.md",
        tmp_path / "thesis.schema.json",
        tmp_path / "notes" / "n1.json",
        tmp_path / "notes" / "n2.md",
        tmp_path / "sub" / "thesis.schema.json",
        tmp_path / "data" / "debate.json",
        tmp_path / "data" / "thesis.json",
        tmp_path / "data" / "a.json",
        tmp_path / "data" / "b.json",
        tmp_path / "data" / "c.json",
    ]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}")

    sandbox_found = set(await aglob_files(_sandbox(), pattern, str(tmp_path)))
    wanted = [DiskGlob(each, str(tmp_path)) for each in expand_braces(pattern)]
    diskglob_found = {str(f) for f in files if any(w.finds(str(f)) for w in wanted)}

    assert sandbox_found == diskglob_found


