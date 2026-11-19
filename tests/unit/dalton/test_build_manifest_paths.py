"""The image's SHA-256 manifest and the startup drift check must cover the same paths."""
from __future__ import annotations

import re
from pathlib import Path

from src.dalton.services.openclaw_admin import DOCKER_MANIFEST_WATCHED_PATHS

ROOT = Path(__file__).resolve().parents[3]


def _manifest_find_roots() -> list[str]:
    text = (ROOT / "scripts/dalton/Dockerfile.dalton").read_text(encoding="utf-8")
    match = re.search(r"&& find (.*?) -type f", text, re.DOTALL)
    assert match, "manifest find command not found in Dockerfile.dalton"
    return match.group(1).replace("\\\n", " ").split()


def test_manifest_roots_match_watched_paths():
    assert sorted(_manifest_find_roots()) == sorted(DOCKER_MANIFEST_WATCHED_PATHS)


def test_chartkit_is_watched_and_image_only_render_page_excluded():
    text = (ROOT / "scripts/dalton/Dockerfile.dalton").read_text(encoding="utf-8")
    assert "libs/chartkit" in DOCKER_MANIFEST_WATCHED_PATHS
    assert "libs/edgar-engine" in DOCKER_MANIFEST_WATCHED_PATHS
    assert "! -path 'libs/chartkit/chartkit/render/*'" in text
