"""Code run on an SSH computer leaves no script file behind, however it ends.

The script is written into the root folder, which is the owner's, so one left
by a timeout or a cancel would sit among their files. A timeout or a cancel
ends the run's process group with a TERM; run here against real bash, since
what is checked is the shell's trap order.
"""

from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys
import time

import pytest

from ptc_agent.core.sandbox.providers._ssh_shell import code_run_command

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell")


def _start(tmp_path, code: str) -> tuple[subprocess.Popen, str]:
    # ``python3`` as the machine has it, whatever this host's PATH names.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    script = str(tmp_path / "_exec_test.py")
    payload = base64.b64encode(code.encode()).decode()
    proc = subprocess.Popen(
        ["bash", "-c", code_run_command(script, payload)],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return proc, script


def test_a_finished_run_keeps_its_exit_and_removes_its_file(tmp_path):
    proc, script = _start(tmp_path, "print('hi'); raise SystemExit(3)")

    out, _ = proc.communicate(timeout=30)

    assert out == "hi\n"
    assert proc.returncode == 3
    assert not os.path.exists(script)


def test_a_run_ended_by_a_term_to_its_group_removes_its_file(tmp_path):
    proc, script = _start(
        tmp_path, "import time; print('up', flush=True); time.sleep(60)"
    )
    try:
        assert proc.stdout.readline() == "up\n"
        assert os.path.exists(script)

        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    assert proc.returncode == 143
    deadline = time.monotonic() + 5
    while os.path.exists(script) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(script)
