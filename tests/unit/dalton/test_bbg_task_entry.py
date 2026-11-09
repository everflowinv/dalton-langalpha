"""Run the BBG shell entry with local transfer and converter substitutes."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest


ENTRY = (
    Path(__file__).resolve().parents[3]
    / "scripts/dalton/openclaw_overlays/scripts/bbg_remote_refresh.sh"
)


def _executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o700)


@pytest.mark.parametrize("conversion_succeeds", [True, False])
def test_bbg_uses_task_workspace_and_python(tmp_path, conversion_succeeds):
    workspace = tmp_path / "task workspace"
    (workspace / "scripts").mkdir(parents=True)
    (workspace / "temp").mkdir()
    output = workspace / "temp/bbg_data_latest.csv"
    output.write_text("existing data\n")
    transfers = tmp_path / "transfers.log"
    python_calls = tmp_path / "python.log"
    commands = tmp_path / "commands"
    commands.mkdir()
    _executable(commands / "ssh", "#!/bin/sh\nexit 0\n")
    _executable(
        commands / "scp",
        "#!/bin/sh\nfor dest do :; done\n"
        'case "$dest" in "$TEST_WORKSPACE"/temp/*) ;; *) exit 90 ;; esac\n'
        'printf "%s\\n" "$dest" >> "$TEST_TRANSFERS"\n'
        'printf "new data\\n" > "$dest"\n',
    )
    for command in ("tee", "rm", "mv"):
        binary = f"/bin/{command}" if command in ("rm", "mv") else "/usr/bin/tee"
        _executable(
            commands / command,
            "#!/bin/sh\n"
            'for arg do\n'
            '  case "$arg" in -*) continue ;; "$TEST_WORKSPACE"/temp/*) ;; *) exit 90 ;; esac\n'
            'done\n'
            f'exec {binary} "$@"\n',
        )
    python = commands / "task-python"
    _executable(
        python,
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_PYTHON_CALLS"\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )
    (workspace / "scripts/bbg_export_to_csv.py").write_text(
        "import argparse, json, os\n"
        "from pathlib import Path\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--input', required=True)\n"
        "parser.add_argument('--output', required=True)\n"
        "args = parser.parse_args()\n"
        "if os.environ['TEST_CONVERSION_SUCCEEDS'] != '1': raise SystemExit(1)\n"
        "Path(args.output).write_bytes(Path(args.input).read_bytes())\n"
        "print(json.dumps({'ok': True, 'output': args.output}))\n"
    )
    env = {
        **os.environ,
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "OPENCLAW_WORKSPACE": str(workspace),
        "DALTON_PYTHON_BIN": str(python),
        "TEST_WORKSPACE": str(workspace),
        "TEST_TRANSFERS": str(transfers),
        "TEST_PYTHON_CALLS": str(python_calls),
        "TEST_CONVERSION_SUCCEEDS": "1" if conversion_succeeds else "0",
    }
    result = subprocess.run(
        ["bash", str(ENTRY), "--pull-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (0 if conversion_succeeds else 1), result.stderr
    assert output.read_text() == ("new data\n" if conversion_succeeds else "existing data\n")
    assert len(python_calls.read_text().splitlines()) == (1 if conversion_succeeds else 2)
    assert all(str(workspace / "temp") in p for p in transfers.read_text().splitlines())
    assert str(workspace / "scripts/bbg_export_to_csv.py") in python_calls.read_text()
    assert (workspace / "temp/bbg_remote_refresh.log").is_file()

    foreign_workspace = tmp_path / "foreign workspace"
    foreign_temp = foreign_workspace / "temp"
    foreign_temp.mkdir(parents=True)
    for name in ("bbg_remote_refresh.log", "bbg_data_remote.incoming.csv", "bbg_data_remote.incoming.xlsx"):
        (foreign_temp / name).write_text("preserved\n")
    altered_entry = tmp_path / "fixed-workspace.sh"
    altered_entry.write_text(ENTRY.read_text().replace(
        'WORKSPACE="${OPENCLAW_WORKSPACE:-/Users/everflow/.openclaw/workspace}"',
        f"WORKSPACE={shlex.quote(str(foreign_workspace))}",
    ))
    rejected = subprocess.run(
        ["bash", str(altered_entry), "--pull-only"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert rejected.returncode != 0
    assert all(path.read_text() == "preserved\n" for path in foreign_temp.iterdir())
