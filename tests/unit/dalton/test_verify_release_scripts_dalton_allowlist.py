"""Core release verifier: `scripts/dalton/` is default-deny and matches the packager."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER = REPO_ROOT / "scripts/standalone/verify_release_tarball.py"
PACKAGER = REPO_ROOT / "scripts/standalone/package_release.sh"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("verify_release_tarball_under_test", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _packager_allowlist_scripts_dalton() -> set[str]:
    text = PACKAGER.read_text(encoding="utf-8")
    block = text.split("ALLOWLIST_CI=(")[0]
    return {m.group(1) for m in re.finditer(r'^\s*"scripts/dalton/([^"]+)"\s*$', block, re.M)}


def test_verifier_allowlist_matches_packager_allowlist() -> None:
    verifier = _load_verifier()
    assert set(verifier.CORE_SCRIPTS_DALTON_ALLOWED) == _packager_allowlist_scripts_dalton()


def test_scripts_dalton_default_deny() -> None:
    verifier = _load_verifier()
    match = verifier._matches_forbidden_subpath
    assert match(("scripts", "dalton", "log_redaction.py")) is None
    assert match(("scripts", "dalton", "seed_openclaw_data.sh")) is None
    assert match(("scripts", "dalton")) is None
    assert match(("scripts", "dalton", "data_assets")) is None
    assert match(("scripts", "dalton", "data_assets", "scripts")) is None
    assert match(("scripts", "dalton", "data_assets", "scripts", "china_2c_monitor.py")) is None
    assert match(("scripts", "dalton", "data_assets", "scripts", "nested", "helper.py")) is None
    assert match(("scripts", "dalton", "data_assets", "everflow_only.txt")) is None
    for name in ("research_runtime", "ops", "launchd", "some_new_private_tool.py"):
        assert match(("scripts", "dalton", name, "x.py")) == ("scripts", "dalton", name)
    assert match(("scripts", "dalton", "data_assets", "skills", "kweb", "run.py")) == (
        "scripts",
        "dalton",
        "data_assets",
        "skills",
    )
    assert match(("scripts", "dalton", "data_assets", "other.txt")) == (
        "scripts",
        "dalton",
        "data_assets",
        "other.txt",
    )
    assert match(("src", "dalton", "data_jobs", "everflow_defaults.yaml")) == (
        "src",
        "dalton",
        "data_jobs",
        "everflow_defaults.yaml",
    )
    assert match(("plugins", "dalton_data", "a.py")) == ("plugins", "dalton_data")
    assert match(("scripts", "standalone", "init.sh")) is None


def test_package_release_keeps_forbidden_scan_lists_out_of_package() -> None:
    """Decision #1: proprietary scan terms and regex rules stay in check_core_release_package.py, not package_release.sh."""
    pkg_text = PACKAGER.read_text(encoding="utf-8")
    for literal in (
        "FORBIDDEN_REGEX_PATTERNS",
        "SCAN_TERMS=",
        "everflowinv",
        "/Users/richardlu",
        "/Users/everflow",
    ):
        assert literal not in pkg_text
    assert "check_core_release_package.py" in pkg_text
    assert "--strict-scan" in pkg_text
