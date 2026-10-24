"""A real-shell injection probe for `dalton_check_done`'s validators.

The contract's validator re-runs in a shell; a param like ``$(touch PWNED)``
must arrive quoted, and the only honest proof is a real shell executing the
really-rendered command. The backend here is a minimal real implementation
(bash in a tmpdir), not a mock.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from src.dalton.tools.check_done_tools import run_check_done

REPO_ROOT = Path(__file__).resolve().parents[3]


class LocalShellBackend:
    """A real filesystem+shell backend rooted at a tmpdir.

    Implements exactly the surface check_done touches (read/glob/execute);
    commands run through ``bash`` with the tmpdir as cwd, so what the
    validator command can and cannot execute is the truth, not an assertion
    about quoting shapes.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    async def aread_text(self, path: str) -> str | None:
        target = self.root / path
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    async def awrite_text(self, path: str, content: str) -> bool:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True

    async def aglob_paths(self, pattern: str, path: str = ".") -> list[str]:
        import fnmatch
        from pathlib import PurePosixPath

        if pattern.startswith(".agents/threads/"):
            # The review-journal scan; no journals exist in this fixture.
            return []
        return [
            str(PurePosixPath(str(p.relative_to(self.root))))
            for p in self.root.rglob("*")
            if p.is_file() and fnmatch.fnmatch(str(p.relative_to(self.root)), pattern)
        ]

    async def aexecute(self, command: str, *, timeout: int | None = None) -> Any:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            command,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout or 60)

        class _Response:
            def __init__(self, output: str, exit_code: int) -> None:
                self.output = output
                self.exit_code = exit_code

        return _Response(out.decode("utf-8", "replace"), proc.returncode or 0)


@pytest.mark.asyncio
async def test_injected_param_never_executes_in_a_real_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """slug carries ``$(touch PWNED)``; the topic contract's validator runs
    for real, and no PWNED file may appear."""
    # Real skill scripts, so the validator is the one production runs.
    skill_src = REPO_ROOT / "plugins/dalton/skills/dalton-topic-research/scripts"
    dst = tmp_path / ".agents/skills/dalton-topic-research/scripts"
    dst.mkdir(parents=True)
    for script in skill_src.glob("*.py"):
        shutil.copy(script, dst / script.name)

    slug = "x$(touch PWNED)"
    draft = tmp_path / f"drafts/2026-10-23-topic-{slug}.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("# 专题\n\n正文" + "内容" * 100, encoding="utf-8")
    # The other topic artifacts, so the validator is the failing check and
    # nothing else dominates the report.
    for rel in (
        "research/2026-10-23-topic-{s}/plan.md",
        "research/2026-10-23-topic-{s}/primary.md",
        "research/2026-10-23-topic-{s}/cluster-c1.md",
        "research/2026-10-23-topic-{s}/cluster-c2.md",
        "research/2026-10-23-topic-{s}/cluster-c3.md",
        "research/2026-10-23-topic-{s}/internal-memory.md",
        "research/2026-10-23-topic-{s}/guidepoint.md",
        "research/2026-10-23-topic-{s}/number-audit.md",
    ):
        target = tmp_path / rel.format(s=slug)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("内容" * 100, encoding="utf-8")
    cross = tmp_path / f"research/2026-10-23-topic-{slug}/cross-check.md"
    cross.parent.mkdir(parents=True, exist_ok=True)
    cross.write_text("交叉验证 盲区检查完成" + "内容" * 100, encoding="utf-8")

    backend = LocalShellBackend(tmp_path)
    result = await run_check_done(
        workflow="topic-research",
        params={
            "topic": "AI capex",
            "slug": slug,
            "workspace_dir": ".",
            "as_of": "2026-10-23",
        },
        backend=backend,
        workspace_id=None,
    )
    assert isinstance(result["missing"], list)
    assert not (tmp_path / "PWNED").exists(), "the injected command executed"
    # And the quoted form really reached bash: the validator ran against a
    # file whose name IS the injection payload.
    assert draft.exists()
    assert json.dumps(result, ensure_ascii=False)
