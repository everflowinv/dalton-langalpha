"""The declared-artifact existence probe — R3a round 2, item 1.

Existence is judged by ``test -s`` on the backend-normalized path, never by
a hash: a binary artifact (codex r1 #1: a real .xlsx was judged missing
because ``sha256sum`` falls back to a UTF-8 read) must answer, and a read
that cannot tell absent from unreadable must never count as missing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.dalton.workflows.journal import missing_declared_artifacts


def _probe(paths_backend, paths):
    """Imported per call so a reverted journal.py fails these tests on the
    probe's behaviour, not on module import."""
    from src.dalton.workflows.journal import _probe_declared_paths

    return _probe_declared_paths(paths_backend, paths)


class _ShelfBackend:
    """A backend over a real directory: ``normalize_path`` maps the agent's
    virtual ``/w`` root onto it, ``aexecute`` runs a real ``/bin/sh``, and
    ``aread_text`` reads the file — the three things the probe consults."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace_dir = str(root)
        self.filesystem_config = SimpleNamespace(enable_path_validation=False)

    def normalize_path(self, path: str) -> str:
        if path.startswith("/w/"):
            return str(self.root / path[len("/w/"):])
        return path if path.startswith("/") else str(self.root / path)

    def virtualize_path(self, path: str) -> str:
        return path.removeprefix(f"{self.root}/")

    def validate_path(self, path: str) -> bool:
        return True

    async def aexecute(self, command: str, **_: object) -> object:
        done = subprocess.run(
            ["/bin/sh", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=self.root,
            env={**os.environ},
        )
        # The real exec result's shape: ``output`` and ``exit_code``, as
        # ``digest_paths`` and ``_probe_exists`` read it.
        return SimpleNamespace(output=done.stdout, exit_code=done.returncode)

    async def aread_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None


class _TextOnlyBackend:
    """The same shelf through reads alone — no ``aexecute`` attribute at
    all, as a store route has none, so the probe's read fallback is all it
    gets."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace_dir = str(root)
        self.filesystem_config = SimpleNamespace(enable_path_validation=False)

    def normalize_path(self, path: str) -> str:
        if path.startswith("/w/"):
            return str(self.root / path[len("/w/"):])
        return path if path.startswith("/") else str(self.root / path)

    def virtualize_path(self, path: str) -> str:
        return path.removeprefix(f"{self.root}/")

    def validate_path(self, path: str) -> bool:
        return True

    async def aread_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None


def _declared(files: list[str]) -> dict:
    return {"files": files, "summary": "written"}


@pytest.fixture
def shelf(tmp_path: Path) -> _ShelfBackend:
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "note.md").write_text("证据" * 100, encoding="utf-8")
    (tmp_path / "research" / "empty.md").write_text("", encoding="utf-8")
    # A real binary artifact: bytes no UTF-8 reader can decode.
    (tmp_path / "model").write_bytes(os.urandom(2048))
    return _ShelfBackend(tmp_path)


@pytest.mark.asyncio
async def test_a_binary_artifact_answers_ok_not_missing(shelf: _ShelfBackend):
    """codex r1 #1: the .xlsx class. ``test -s`` never decodes, so a real
    binary deliverable is present and non-empty."""
    verdicts = await _probe(shelf, ["model/x.xlsx", "model"])
    assert verdicts["model/x.xlsx"] == "missing"  # that path genuinely is not there
    assert verdicts["model"] == "ok"  # the binary that exists
    assert await missing_declared_artifacts(shelf, _declared(["model"])) == []


@pytest.mark.asyncio
async def test_missing_and_empty_are_told_apart(shelf: _ShelfBackend):
    verdicts = await _probe(
        shelf, ["research/note.md", "research/empty.md", "research/gone.md"]
    )
    assert verdicts == {
        "research/note.md": "ok",
        "research/empty.md": "empty",
        "research/gone.md": "missing",
    }
    assert await missing_declared_artifacts(
        shelf, _declared(["research/gone.md"])
    ) == ["research/gone.md（不存在）"]
    assert await missing_declared_artifacts(
        shelf, _declared(["research/empty.md"])
    ) == ["research/empty.md（空文件）"]


@pytest.mark.asyncio
async def test_a_virtual_path_probes_where_the_write_landed(shelf: _ShelfBackend):
    """A child declaring the agent-facing virtual spelling is probed at the
    backend-normalized absolute path — the same place a Write of that
    declaration resolved to."""
    verdicts = await _probe(shelf, ["/w/research/note.md"])
    assert verdicts == {"/w/research/note.md": "ok"}


@pytest.mark.asyncio
async def test_a_normalization_the_backend_refuses_is_unknown_not_missing(
    shelf: _ShelfBackend, monkeypatch
):
    def _refuses(path: str) -> str:
        raise ValueError("Path traversal (..) is not allowed on mounted paths")

    monkeypatch.setattr(shelf, "normalize_path", _refuses)
    verdicts = await _probe(shelf, ["../sibling/research/note.md"])
    assert verdicts == {"../sibling/research/note.md": "unknown"}
    assert await missing_declared_artifacts(
        shelf, _declared(["../sibling/research/note.md"])
    ) == []


@pytest.mark.asyncio
async def test_a_read_that_cannot_tell_is_unknown_not_missing(tmp_path: Path):
    """Without a shell, only FileNotFoundError says missing: a decode
    failure (``None`` from the generic layer) never does — the conservative
    half that keeps the binary-artifact class from misfiring on every
    text-only backend."""
    (tmp_path / "model").write_bytes(os.urandom(2048))
    backend = _TextOnlyBackend(tmp_path)

    verdicts = await _probe(backend, ["model", "gone.md"])
    assert verdicts == {"model": "unknown", "gone.md": "unknown"}
    assert await missing_declared_artifacts(
        backend, _declared(["model", "gone.md"])
    ) == []
