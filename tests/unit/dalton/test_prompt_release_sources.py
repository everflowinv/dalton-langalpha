"""sync3 B5/D16①: Dalton data-map sources ride the prompt release digest.

Upstream #655 (``release.py``) decides from ``_sources()`` whether a deploy
changed the static prompt, and a pinned thread rebuilds on the next turn
when it did. The Dalton data map renders into ``system.md.j2`` and
``components/tool_guide.md.j2`` (loader.py's ``data-map`` hook), so its
render inputs are deploy state of the digest — without them a Dalton deploy
that only changed the map's sources would leave stale pins unrebuilt.
"""

from __future__ import annotations

from pathlib import Path

from ptc_agent.agent.prompts import release


def _data_map_sources() -> set[Path]:
    return {p for p in release._sources() if "dalton" in p.parts}


def test_the_data_map_render_inputs_are_registered_sources() -> None:
    """The four files the data map renders from — ``__init__.py`` (the module
    the loader imports), ``builder.py`` (renders the map), ``health.py``
    (builds the records the daily-cached map carries), ``sources.yaml`` (the
    spec) — are each in ``_sources()``, and the digest reads them all without
    a missing file."""
    names = {p.name for p in _data_map_sources()}
    assert {"__init__.py", "builder.py", "health.py", "sources.yaml"} <= names

    # Every listed source exists and the digest computes over the union.
    release.source_digest.cache_clear()
    try:
        assert release.source_digest()
    finally:
        release.source_digest.cache_clear()


def test_the_runtime_regenerated_overlay_is_not_a_prompt_source() -> None:
    """``src/dalton/llm/openclaw_overlay.json`` is regenerated at runtime
    (plan B5/D16① excludes it); hashing it would flip the digest on every
    deploy's runtime state, not on a source change."""
    assert all("openclaw_overlay.json" not in p.parts for p in release._sources())


def test_the_bundle_files_beyond_code_are_registered_sources() -> None:
    """The non-``.py`` bundle files that write into the static prompt: the
    Dalton subagent plans (``bundled_agent_plans`` → ``format_subagent_summary``)
    and every bundle's manifests, whose server names/descriptions/instructions
    the tool summary renders (codex r2)."""
    sources = release._sources()
    names = {p.name for p in sources}
    assert {"dalton-reviewer.md", "dalton-evidence.md"} <= names

    from ptc_agent.config.plugins import BUNDLES_DIR

    for pattern in ("*/agents/*.md", "*/plugin.json", "*/mcp.json"):
        listed = {p.resolve() for p in BUNDLES_DIR.glob(pattern)}
        registered = {p.resolve() for p in sources}
        assert listed <= registered, f"{pattern} files missing from _sources()"


def test_a_reviewer_description_edit_changes_the_digest() -> None:
    """B5 r2 acceptance: 只改 ``plugins/dalton/agents/dalton-reviewer.md`` 的
    ``description`` 一行，``source_digest()`` 随之变化 — the description
    renders into the main agent's static prompt, so its deploy changes must
    read as a prompt-source change."""
    plan = next(
        p
        for p in release._sources()
        if p.name == "dalton-reviewer.md" and "agents" in p.parts
    )
    text = plan.read_text(encoding="utf-8")
    (desc_line,) = [ln for ln in text.splitlines() if ln.startswith("description:")]
    probed = text.replace(desc_line, desc_line + " (sync3 B5 r2 digest probe)", 1)

    release.source_digest.cache_clear()
    try:
        before = release.source_digest()
        try:
            plan.write_text(probed, encoding="utf-8")
            release.source_digest.cache_clear()
            assert release.source_digest() != before
        finally:
            plan.write_text(text, encoding="utf-8")
    finally:
        release.source_digest.cache_clear()


def test_sources_yaml_content_changes_the_digest() -> None:
    """B5 acceptance: 改 ``sources.yaml`` 内容后 ``source_digest()`` 变化 —
    a data-map deploy change reads as a prompt-source change."""
    yaml_path = next(
        p
        for p in release._sources()
        if p.name == "sources.yaml" and "data_map" in p.parts
    )
    release.source_digest.cache_clear()
    try:
        before = release.source_digest()
        original = yaml_path.read_bytes()
        try:
            yaml_path.write_bytes(original + b"\n# sync3 B5 digest probe\n")
            release.source_digest.cache_clear()
            assert release.source_digest() != before
        finally:
            yaml_path.write_bytes(original)
    finally:
        release.source_digest.cache_clear()
