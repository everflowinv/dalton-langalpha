"""B6: an import name whose PyPI spelling differs maps to the real package —
but only after the local-module filter, so a workspace's own `fitz.py` is
never pip-installed over (`fitz` the wheel ships the wrong library; a bare
`pip install fitz` is worse than useless)."""

from __future__ import annotations

import dataclasses
from unittest.mock import create_autospec, patch

import pytest

from ptc_agent.core.sandbox import jobs, mcp_setup
from tests.unit.ptc_agent.agent.job_fakes import outcome
from tests.unit.ptc_agent.core.sandbox.test_turn_cwd import (  # noqa: F401 - fixtures
    runtime,
    sandbox,
)


def _tb(name: str) -> str:
    return (
        "Traceback (most recent call last):\n"
        '  File "/w/code.py", line 1, in <module>\n'
        f"ModuleNotFoundError: No module named '{name}'\n"
    )


class TestPipPackageForImport:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("fitz", "pymupdf"),
            ("cv2", "opencv-python-headless"),
            ("PIL", "pillow"),
            ("yaml", "pyyaml"),
            ("sklearn", "scikit-learn"),
            ("bs4", "beautifulsoup4"),
            ("pandas", "pandas"),
            # Unknown names pass through: `missing_import` already reduced the
            # record to its base package before the map sees it.
            ("someinternalpkg", "someinternalpkg"),
        ],
    )
    def test_the_map_covers_the_common_mismatches(self, name, expected):
        assert mcp_setup.pip_package_for_import(name) == expected


@pytest.mark.asyncio
async def test_a_fitz_import_installs_pymupdf(sandbox):
    failed = dataclasses.replace(
        outcome(exit_code=1, output=_tb("fitz")), raised="fitz"
    )
    ok = outcome(exit_code=0, output="done")
    sandbox._install_package = create_autospec(
        sandbox._install_package, return_value=True
    )
    with patch.object(
        jobs,
        "run_job",
        new=create_autospec(jobs.run_job, side_effect=[failed, ok]),
    ):
        result = await sandbox.start_code_job("import fitz", wait_s=60)
    sandbox._install_package.assert_awaited_once_with("pymupdf")
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_a_local_fitz_module_is_never_installed(sandbox, runtime):
    # The workspace probe answers "fitz exists here": the filter drops the
    # name before the import→package map ever sees it, so nothing installs
    # and the run's own failure stands.
    from ptc_agent.core.sandbox.runtime import ExecResult

    failed = dataclasses.replace(
        outcome(exit_code=1, output=_tb("fitz")), raised="fitz"
    )
    sandbox._install_package = create_autospec(
        sandbox._install_package, return_value=True
    )
    runtime.exec.return_value = ExecResult("fitz\n", "", 0)
    with patch.object(
        jobs,
        "run_job",
        new=create_autospec(jobs.run_job, return_value=failed),
    ):
        result = await sandbox.start_code_job("import fitz", wait_s=60)
    sandbox._install_package.assert_not_awaited()
    assert result.exit_code == 1
