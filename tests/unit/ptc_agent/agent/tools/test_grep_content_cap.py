"""The Grep tool refuses every-line patterns and bounds its content answer —
R3a #2.

The OOM shape from 10/30: `Grepping content pattern_length=0` over a research
workspace collected every line of every file into one answer and took the
backend to 4.15 GB against its 4 GiB cgroup.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ptc_agent.agent.tools.grep import create_grep_tool

# The budget the fix sets; spelled here rather than imported so a reverted
# grep.py fails these tests on behaviour, not on an import error.
BUDGET_CHARS = 200_000


class FakeBackend:
    """The filesystem surface the Grep tool touches, with its calls counted."""

    def __init__(self, answer=None) -> None:
        self.answer = answer if answer is not None else []
        self.calls: list[dict] = []
        self.workspace_dir = "/w"
        self.filesystem_config = SimpleNamespace(enable_path_validation=False)

    def normalize_path(self, path: str) -> str:
        return path

    def validate_path(self, path: str) -> bool:
        return True

    def virtualize_path(self, path: str) -> str:
        return path

    async def agrep_rich(self, pattern: str, path: str = ".", **kwargs):
        self.calls.append({"pattern": pattern, "path": path, **kwargs})
        return self.answer


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["", "x*", "^", "$", "foo|"])
async def test_a_pattern_that_matches_every_line_is_refused_not_searched(
    pattern,
):
    """Empty, or empty-matching: every line under the path is a hit, which is
    the OOM shape — the tool says so and no route is asked."""
    backend = FakeBackend(answer=["should never be reached"])
    result = await create_grep_tool(backend).ainvoke(
        {"pattern": pattern, "output_mode": "content"}
    )
    assert result.startswith("ERROR:")
    assert "Read / Glob" in result
    assert backend.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "mode"),
    [("", "files_with_matches"), ("x*", "count"), ("", "count")],
)
async def test_refusal_covers_the_other_output_modes_too(pattern, mode):
    backend = FakeBackend(answer=[])
    result = await create_grep_tool(backend).ainvoke(
        {"pattern": pattern, "output_mode": mode}
    )
    assert result.startswith("ERROR:")
    assert backend.calls == []


@pytest.mark.asyncio
async def test_a_concrete_pattern_searches_as_before():
    """A pattern that does not match the empty string reaches the backend
    with its arguments unchanged (pagination semantics untouched)."""
    backend = FakeBackend(answer=["/w/a.md:3:hit"])
    result = await create_grep_tool(backend).ainvoke(
        {
            "pattern": "revenue",
            "output_mode": "content",
            "head_limit": 5,
            "offset": 2,
        }
    )
    assert "revenue" in result
    assert "/w/a.md:3:hit" in result
    assert backend.calls[0]["head_limit"] == 5
    assert backend.calls[0]["offset"] == 2


@pytest.mark.asyncio
async def test_a_content_answer_is_cut_at_the_character_budget():
    """However much a route collected, the rendered answer stops at the
    budget and says it was cut."""
    lines = [f"/w/big.md:{i}:" + "x" * 60 for i in range(40_000)]
    # ~2.5 MB of rendered content — far past the 200k budget.
    assert sum(len(line) for line in lines) > BUDGET_CHARS
    backend = FakeBackend(answer=lines)

    result = await create_grep_tool(backend).ainvoke(
        {"pattern": "x{2,}", "output_mode": "content"}
    )

    assert len(result) < BUDGET_CHARS + 2_000
    assert "truncated" in result
    assert "head_limit" in result


@pytest.mark.asyncio
async def test_an_answer_inside_the_budget_carries_no_cut_note():
    lines = [f"/w/a.md:{i}:hit" for i in range(100)]
    backend = FakeBackend(answer=lines)
    result = await create_grep_tool(backend).ainvoke(
        {"pattern": "hit", "output_mode": "content"}
    )
    assert "truncated" not in result
    assert result.count(":hit") == 100


# ---------------------------------------------------------------------------
# The sandbox route's wire cap: rg itself is stopped when the event stream
# reaches the byte bound — collection never sees the rest.


class _LocalRuntime:
    """Runs a command from the runtime's own working directory, as the
    sibling grep suite's runtime does."""

    def __init__(self, root: Path, bin_dir: Path) -> None:
        self.root = root
        self._env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    async def exec(self, command: str, timeout: int = 60):
        done = subprocess.run(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=self.root,
            env=self._env,
        )
        return SimpleNamespace(stdout=done.stdout, stderr="", exit_code=done.returncode)


def _sandbox(runtime: _LocalRuntime, folder: Path) -> SimpleNamespace:
    async def _call(fn, *args, retry_policy=None, **kwargs):
        return await fn(*args, **kwargs)

    return SimpleNamespace(
        _wait_ready=_NoopAsync(),
        config=SimpleNamespace(filesystem=SimpleNamespace(enable_path_validation=False)),
        _work_dir=str(runtime.root),
        _normalize_search_path=lambda path: str(folder),
        runtime=runtime,
        _runtime_call=_call,
    )


class _NoopAsync:
    async def __call__(self, *args, **kwargs):
        return None


def _rg_emitting_megabytes(tmp_path: Path, total_mb: float) -> Path:
    """A fake rg whose content stream is `total_mb` of match events — a
    pattern that hits every line of a much larger tree."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    per_line = "y" * 900
    line = (
        '{"type":"match","data":{"path":{"text":"/w/big.txt"},'
        '"lines":{"text":"' + per_line + '"},"line_number":1,"submatches":[]}}'
    )
    count = int(total_mb * 1024 * 1024 / len(line))
    (bin_dir / "rg").write_text(
        "#!/bin/sh\n"
        f"i=0\nwhile [ $i -lt {count} ]; do\n"
        f"  printf '%s\\n' '{line}'\n"
        "  i=$((i+1))\ndone\n"
    )
    (bin_dir / "rg").chmod(0o755)
    return bin_dir


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.asyncio
async def test_the_sandbox_route_stops_collecting_at_its_wire_cap(tmp_path):
    """A stream that would carry 20 MB stops at the cap: the answer is
    bounded, and it carries the note saying so. This is the collection-stage
    bound — without it the whole tree crosses the wire before any slicing."""
    from ptc_agent.core.sandbox.files import _GREP_CONTENT_WIRE_BYTES, agrep_content

    bin_dir = _rg_emitting_megabytes(tmp_path, total_mb=20)
    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    sandbox = _sandbox(_LocalRuntime(root, bin_dir), folder)

    result = await agrep_content(sandbox, "everything", str(folder), "content")

    notes = getattr(result, "notes", ())
    assert notes and "stopped at the 2 MB limit" in notes[0]
    rendered_chars = sum(len(line) for line in result)
    # The cap is on the wire bytes; the rendered lines are a subset of them.
    assert rendered_chars < _GREP_CONTENT_WIRE_BYTES
    # 20 MB of ~930-byte events would render ~22k lines; the cap keeps ~2.2k.
    assert len(result) < 3_000


class _MemSandbox:
    """Path surface for a store route standing alone."""

    root_dir = "/w"

    def normalize_path(self, path: str) -> str:
        return path

    def virtualize_path(self, path: str) -> str:
        return path

    def validate_path(self, path: str) -> bool:
        return True

    filesystem_config = SimpleNamespace(enable_path_validation=False)


@pytest.mark.asyncio
async def test_the_in_memory_routes_stop_collecting_at_their_line_cap():
    """The store-backed routes (memory, workflows, db json) cap the lines a
    content search collects — the count of collected entries, not just the
    rendered answer, stays bounded."""
    from langgraph.store.memory import InMemoryStore

    from ptc_agent.agent.backends.langgraph_store import CONTENT_MAX_LINES, StoreBackend

    total = CONTENT_MAX_LINES + 5_000
    store = InMemoryStore()
    namespace = ("test",)
    store.put(
        namespace,
        "big.md",
        {"content": "\n".join(f"hit {i}" for i in range(total))},
    )
    backend = StoreBackend(
        store=store,
        namespace_factory=lambda: namespace,
        root_prefix="/w/mem/",
        sandbox_backend=_MemSandbox(),
    )

    result = await backend.agrep_rich(
        "hit", path="/w/mem", output_mode="content", show_line_numbers=True
    )
    assert len(result) == CONTENT_MAX_LINES  # not the 5,000 more that exist
    # And the capped collection says so (round 2, item 6).
    notes = getattr(result, "notes", ())
    assert notes and "matching-line limit" in notes[0]
    assert "head_limit and offset" in notes[0]

    # A page that asks deeper than the cap is still served as asked.
    paged = await backend.agrep_rich(
        "hit",
        path="/w/mem",
        output_mode="content",
        show_line_numbers=True,
        head_limit=10,
        offset=CONTENT_MAX_LINES - 5,
    )
    assert len(paged) == 10

    # And a page inside the cap is unchanged.
    first = await backend.agrep_rich(
        "hit",
        path="/w/mem",
        output_mode="content",
        show_line_numbers=True,
        head_limit=3,
    )
    assert len(first) == 3


# ---------------------------------------------------------------------------
# Round 2: the cap must not break paging (codex r1 #2), must cut on line
# boundaries (#3), and must announce itself in bytes (#4).


class _PlainRuntime:
    """Runs commands with the ambient PATH, so the machine's real rg runs."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def exec(self, command: str, timeout: int = 60):
        done = subprocess.run(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=self.root,
        )
        return SimpleNamespace(output=done.stdout, exit_code=done.returncode)


def _real_sandbox(root: Path, folder: Path, bin_dir: Path | None = None) -> SimpleNamespace:
    """The sandbox layer the composite drives in production: its agrep_rich
    is the real ``agrep_content``, whose rg comes from the machine's PATH or
    the fake a test puts first on it."""

    async def _call(fn, *args, retry_policy=None, **kwargs):
        return await fn(*args, **kwargs)

    runtime = _PlainRuntime(root) if bin_dir is None else _LocalRuntime(root, bin_dir)
    ptc_sandbox = SimpleNamespace(
        _wait_ready=_NoopAsync(),
        config=SimpleNamespace(filesystem=SimpleNamespace(enable_path_validation=False)),
        _work_dir=str(root),
        _normalize_search_path=lambda path: str(folder)
        if path == "."
        else path if path.startswith("/") else f"{folder}/{path}",
        runtime=runtime,
        _runtime_call=_call,
    )

    class _SandboxLayer:
        computer_root = str(root)
        workspace_dir = str(folder)
        filesystem_config = ptc_sandbox.config.filesystem

        def normalize_path(self, path: str) -> str:
            return ptc_sandbox._normalize_search_path(path)

        def virtualize_path(self, path: str) -> str:
            return path.removeprefix(f"{folder}/")

        def validate_path(self, path: str) -> bool:
            return True

        async def agrep_rich(self, pattern: str, path: str = ".", **kwargs):
            from ptc_agent.core.sandbox.files import agrep_content

            return await agrep_content(
                ptc_sandbox, pattern, path, folder=str(folder), **kwargs
            )

    return _SandboxLayer()


def _rg_line_events(tmp_path: Path) -> Path:
    """A fake rg whose content stream is rg's own --json shape: one match
    event per line, in file order, of every file under the path it is
    handed — a directory walks like the real rg walks one."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    event = (
        '{"type":"match","data":{"path":{"text":"%s"},'
        '"lines":{"text":"%s"},"line_number":%d,"submatches":[]}}'
    )
    printf_line = '      printf "' + event.replace('"', '\\"') + '\\n" "$f" "$line" "$n"'
    script = "\n".join(
        [
            "#!/bin/sh",
            "prev=''; last=''",
            'for a in "$@"; do',
            '  case "$a" in -*) ;; *) prev="$last"; last="$a";; esac',
            "done",
            'pattern="$prev"; start="$last"',
            'find "$start" -type f 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do',
            "  n=0",
            '  while IFS= read -r line; do',
            "    n=$((n+1))",
            '    case "$line" in *"$pattern"*)',
            printf_line,
            "    ;; esac",
            '  done < "$f"',
            "done",
            "",
        ]
    )
    (bin_dir / "rg").write_text(script)
    (bin_dir / "rg").chmod(0o755)
    return bin_dir


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.asyncio
async def test_deep_pages_survive_the_cap_through_the_composite(tmp_path):
    """codex r1 #2's repro: 5,000 hits over 2 MB, paged at offset 3000.
    Through the production composite → sandbox → rg chain, the byte cap
    must not fire on a paged ask: page 3001–3010 comes back whole."""
    from ptc_agent.agent.backends.composite import CompositeFilesystemBackend

    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    # ~530 bytes per line × 5,000 lines ≈ 2.6 MB of events: the unpaged ask
    # on the same tree must hit the byte cap, the paged one must not.
    (folder / "big.md").write_text(
        "\n".join(f"hit line {i:04d} alpha" + " x" * 240 for i in range(5_000)),
        encoding="utf-8",
    )
    bin_dir = _rg_line_events(tmp_path)
    composite = CompositeFilesystemBackend(
        sandbox=_real_sandbox(root, folder, bin_dir), routes=[]
    )

    page = await composite.agrep_rich(
        "alpha",
        path=str(folder),
        output_mode="content",
        show_line_numbers=True,
        head_limit=10,
        offset=3000,
    )
    assert not getattr(page, "notes", ()), "a paged ask is never byte-capped"
    assert len(page) == 10
    assert [entry.split(":")[1] for entry in page] == [str(n) for n in range(3001, 3011)]
    # The file's line N (1-based) carries the zero-based label N-1.
    for entry, number in zip(page, range(3001, 3011)):
        assert f"hit line {number - 1:04d}" in entry

    # And the unpaged ask on the same tree still hits the byte cap and says so.
    unpaged = await composite.agrep_rich(
        "alpha", path=str(folder), output_mode="content", show_line_numbers=True
    )
    notes = getattr(unpaged, "notes", ())
    assert notes and "stopped at the 2 MB limit" in notes[0]


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.asyncio
async def test_a_capped_stream_never_renders_a_torn_line(tmp_path):
    """codex r1 #3: head -c can split the last JSON event. The torn line is
    dropped — no fragment of JSON reaches the model as if it were a match
    or an error."""
    from ptc_agent.core.sandbox.files import agrep_content

    bin_dir = _rg_emitting_megabytes(tmp_path, total_mb=20)
    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    sandbox = _sandbox(_LocalRuntime(root, bin_dir), folder)

    result = await agrep_content(sandbox, "everything", str(folder), "content")

    assert getattr(result, "notes", ())
    for entry in result:
        assert '{"type"' not in entry, "a torn JSON event leaked into the answer"


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.asyncio
async def test_one_matching_line_larger_than_the_cap_says_so(tmp_path):
    """A first event that alone outgrows the cap returns the plain
    instruction — no torn JSON, and not 'no matches' either."""
    from ptc_agent.core.sandbox.files import agrep_content

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    line = '{"type":"match","data":{"path":{"text":"/w/big.txt"},"lines":{"text":"' + "y" * (3 * 1024 * 1024) + '"},"line_number":1,"submatches":[]}}'
    (bin_dir / "rg").write_text(f"#!/bin/sh\nprintf '%s\\n' '{line}'\n")
    (bin_dir / "rg").chmod(0o755)
    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    sandbox = _sandbox(_LocalRuntime(root, bin_dir), folder)

    result = await agrep_content(sandbox, "everything", str(folder), "content")

    assert list(result) == []
    (note,) = getattr(result, "notes", ())
    assert "first matching line" in note
    assert "Read the file with offset/limit" in note


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.asyncio
async def test_multibyte_content_still_announces_the_cap(tmp_path):
    """codex r1 #4's repro: 2 MB of bytes that decode to under 2 MB of
    characters. The cap proves itself in bytes, so the note still appears."""
    from ptc_agent.core.sandbox.files import _GREP_CONTENT_WIRE_BYTES, agrep_content

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # ~300 CJK chars per line: ~900 bytes, ~300 characters.
    body = "长" * 300
    line = (
        '{"type":"match","data":{"path":{"text":"/w/big.txt"},'
        '"lines":{"text":"' + body + '"},"line_number":1,"submatches":[]}}'
    )
    count = int(3 * 1024 * 1024 / len(line.encode("utf-8"))) + 10
    (bin_dir / "rg").write_text(
        "#!/bin/sh\ni=0\nwhile [ $i -lt " + str(count) + " ]; do\n"
        f"  printf '%s\\n' '{line}'\n  i=$((i+1))\ndone\n"
    )
    (bin_dir / "rg").chmod(0o755)
    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    sandbox = _sandbox(_LocalRuntime(root, bin_dir), folder)

    result = await agrep_content(sandbox, "everything", str(folder), "content")

    notes = getattr(result, "notes", ())
    assert notes and "stopped at the 2 MB limit" in notes[0]
    joined = "".join(result)
    assert len(joined.encode("utf-8")) <= _GREP_CONTENT_WIRE_BYTES
    assert len(joined) < _GREP_CONTENT_WIRE_BYTES  # the round-1 miss


# ---------------------------------------------------------------------------
# Round 3: a paged ask gets a byte cap that grows with its own depth
# (codex r2 #1), and a deep page reaches the store routes through the real
# composite (codex r2 #2).


def test_the_wire_cap_grows_with_the_page_and_is_capped():
    from ptc_agent.core.sandbox.files import (
        _GREP_CONTENT_WIRE_BYTES,
        _GREP_WIRE_MAX_BYTES,
        _grep_wire_cap_bytes,
    )

    assert _grep_wire_cap_bytes(None) == _GREP_CONTENT_WIRE_BYTES
    assert _grep_wire_cap_bytes(1) == _GREP_CONTENT_WIRE_BYTES + 4 * 1024
    # offset 3000 + head_limit 10 → 3010 events of allowance.
    assert _grep_wire_cap_bytes(3010) == _GREP_CONTENT_WIRE_BYTES + 3010 * 4 * 1024
    # Deep enough pages stop at the ceiling.
    assert _grep_wire_cap_bytes(100_000) == _GREP_WIRE_MAX_BYTES


class _SpyRuntime(_LocalRuntime):
    """The local runtime, recording how many bytes each command's output
    crossed the wire as."""

    def __init__(self, root: Path, bin_dir: Path) -> None:
        super().__init__(root, bin_dir)
        self.stdout_bytes: list[int] = []

    async def exec(self, command: str, timeout: int = 60):
        result = await super().exec(command, timeout=timeout)
        self.stdout_bytes.append(len((result.stdout or "").encode("utf-8", "replace")))
        return result


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.asyncio
async def test_a_paged_ask_with_one_huge_line_is_bounded_and_says_so(tmp_path):
    """codex r2 #1's repro: ``head_limit=1`` over a single 16 MiB matching
    line. ``-m`` bounded the event count, never the bytes — the line used
    to cross whole. The paged cap bounds the wire, and the answer is the
    long-line instruction, not a fragment."""
    from ptc_agent.core.sandbox.files import _grep_wire_cap_bytes, agrep_content

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rg").write_text(
        "#!/bin/sh\n"
        "printf '%s' '{\"type\":\"match\",\"data\":{\"path\":{\"text\":\"/w/big.txt\"},\"lines\":{\"text\":\"'\n"
        "head -c 16777216 /dev/zero | tr '\\0' 'y'\n"
        "printf '%s\\n' '\"},\"line_number\":1,\"submatches\":[]}}'\n"
    )
    (bin_dir / "rg").chmod(0o755)
    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    runtime = _SpyRuntime(root, bin_dir)
    sandbox = _sandbox(runtime, folder)

    result = await agrep_content(
        sandbox, "everything", str(folder), "content", head_limit=1
    )

    assert list(result) == []
    (note,) = getattr(result, "notes", ())
    assert "first matching line" in note
    # What crossed the wire: the page's own cap (plus the three probe
    # bytes), not the 16 MiB the line wanted to send.
    assert runtime.stdout_bytes == [_grep_wire_cap_bytes(1) + 3]


class _QuietSandbox:
    """The disk side of a composite whose only files are a route's: paths
    resolve like the real backend's and every disk search finds nothing."""

    def __init__(self, root: Path, folder: Path) -> None:
        self.computer_root = str(root)
        self.workspace_dir = str(folder)
        self.filesystem_config = SimpleNamespace(enable_path_validation=False)

    def normalize_path(self, path: str) -> str:
        if path == ".":
            return self.workspace_dir
        if path.startswith("/"):
            return path
        return f"{self.workspace_dir}/{path}"

    def virtualize_path(self, path: str) -> str:
        return path.removeprefix(f"{self.workspace_dir}/")

    def validate_path(self, path: str) -> bool:
        return True

    async def agrep_rich(self, pattern: str, path: str = ".", **kwargs):
        return []

    async def aglob_paths(self, pattern: str, path: str = ".") -> list[str]:
        return []


@pytest.mark.asyncio
async def test_deep_pages_reach_store_routes_through_the_composite(tmp_path):
    """codex r2 #2's repro, on the real chain (InMemoryStore →
    StoreBackend → CompositeFilesystemBackend, real routes): 15,000 hits,
    ``offset=10001, head_limit=10`` past the route's unpaged 10,000-line
    cap. Inside the route and from the root above it, the page comes back
    whole; the unpaged ask on the same data keeps the cap and its note."""
    from langgraph.store.memory import InMemoryStore

    from ptc_agent.agent.backends.composite import CompositeFilesystemBackend
    from ptc_agent.agent.backends.langgraph_store import (
        CONTENT_MAX_LINES,
        StoreBackend,
    )

    root = tmp_path / "root"
    folder = root / "Home"
    folder.mkdir(parents=True)
    disk = _QuietSandbox(root, folder)

    store = InMemoryStore()
    namespace = ("memtest",)
    store.put(
        namespace,
        "notes.md",
        {"content": "\n".join(f"hit row {i:05d}" for i in range(15_000))},
    )
    # A non-dot mount: rg's walk skips dot-directories below the path it
    # is given, so a search from the root never reaches an `.agents/…`
    # route — that rule is orthogonal to paging. This test needs the merge
    # path reached, so the route sits at a plain prefix.
    mem_root = f"{root}/memstore/"
    route = StoreBackend(
        store=store,
        namespace_factory=lambda: namespace,
        root_prefix=mem_root,
        sandbox_backend=disk,
    )
    composite = CompositeFilesystemBackend(sandbox=disk, routes=[route])

    def line_text(entry) -> str:
        return str(entry).rsplit(":", 1)[-1]

    # Inside the route's root: the route path.
    page = await composite.agrep_rich(
        "hit", path=mem_root, output_mode="content", show_line_numbers=True,
        head_limit=10, offset=10001,
    )
    assert not getattr(page, "notes", ())
    assert len(page) == 10
    assert [line_text(entry) for entry in page] == [
        f"hit row {i:05d}" for i in range(10_001, 10_011)
    ]

    # From the computer root above it: the merge path.
    page_above = await composite.agrep_rich(
        "hit", path=str(root), output_mode="content", show_line_numbers=True,
        head_limit=10, offset=10001,
    )
    assert len(page_above) == 10
    assert [line_text(entry) for entry in page_above] == [
        f"hit row {i:05d}" for i in range(10_001, 10_011)
    ]

    # The unpaged ask on the same data keeps its collection cap and note.
    unpaged = await composite.agrep_rich(
        "hit", path=mem_root, output_mode="content", show_line_numbers=True
    )
    assert len(unpaged) == CONTENT_MAX_LINES
    notes = getattr(unpaged, "notes", ())
    assert notes and "matching-line limit" in notes[0]


@pytest.mark.asyncio
async def test_a_match_withheld_for_size_is_not_reported_as_no_matches():
    """A matching line too long to bring back is a match the answer could not
    carry, not an absent one: the tool says only that, never "No matches"."""
    from ptc_agent.core.sandbox.files import _GREP_LONG_LINE_NOTE, _CappedGrep

    note = _GREP_LONG_LINE_NOTE.format(path="/w/huge.json", mb=2)
    backend = FakeBackend(answer=_CappedGrep([], [note]))
    result = await create_grep_tool(backend).ainvoke(
        {"pattern": "NEEDLE", "output_mode": "content", "head_limit": 1}
    )
    assert "No matches found" not in result
    assert "first matching line alone" in result
