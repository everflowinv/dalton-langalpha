"""The sandbox image ships ffprobe (48h review item 12, 2026-10-28).

Recording-duration probes (``scripts/dalton/asr_jobs.py``) shell out to
``ffprobe``, which upstream's sandbox image never installs, so every audio
probe on the image died at exec — not a code failure, a missing system
binary. Pinned here at the image manifest level, the same way
``test_dependency_pins.py`` pins the Python list.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_DOCKERFILE = _ROOT / "Dockerfile.sandbox"


def _apt_packages(block_marker: str) -> list[str]:
    """Package names of the apt install whose command names ``block_marker``.

    Folds backslash continuations the way the Dockerfile parser does: a
    line that starts with ``#`` inside a continuation is dropped, not
    joined — everything after ``#`` would otherwise read as shell comment
    and hide the packages behind it.
    """
    logical: list[str] = []
    buffer = ""
    for raw in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append(buffer + line)
        buffer = ""
    command = next(line for line in logical if block_marker in line)
    return re.findall(r"\S+", command.split(block_marker, 1)[1].split("&&", 1)[0])


def test_the_sandbox_image_installs_ffprobe():
    """``ffmpeg`` provides the ``ffprobe`` binary the ASR pipeline probes
    with; without it in the image every recording probe fails at exec."""
    packages = _apt_packages("apt-get install -y --no-install-recommends")
    assert "ffmpeg" in packages, (
        "Dockerfile.sandbox's --no-install-recommends block must install "
        "ffmpeg (provides ffprobe) — see 48h review item 12"
    )
