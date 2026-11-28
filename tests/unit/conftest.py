"""Socket tripwire: unit tests must not reach real network endpoints.

A mock that silently stops intercepting (e.g. after a module move breaks a
patch target) fails OPEN — the test goes green against live I/O. Blocking
socket creation turns that failure mode into a loud error. AF_UNIX stays
allowed: asyncio's event loop self-pipe is a unix socketpair.
"""

import contextlib
from pathlib import Path

import pytest
import pytest_socket


@pytest.fixture(autouse=True)
def _no_real_sockets(request):
    if request.node.get_closest_marker("enable_socket"):
        yield
        return
    pytest_socket.disable_socket(allow_unix_socket=True)
    yield
    pytest_socket.enable_socket()


@pytest.fixture(autouse=True)
def _reader_pool_follows_cache_client(monkeypatch):
    """Alias the dedicated stream-reader pool onto the injected cache client.

    Blocking XREADs moved off the cache pool in production, but unit tests
    inject their fakes through ``get_cache_client``. Without this the readers
    would build a real ``BlockingConnectionPool`` and block inside XREAD; the
    accessor deliberately has no cache fallback of its own.

    One seam, and it follows whichever client the caller resolved, because the
    accessor is handed that client rather than looking one up. A test wanting
    real reader-pool behavior overrides this single symbol.
    """
    from src.utils.cache import stream_pool

    async def _reader(cache):
        return getattr(cache, "client", None)

    monkeypatch.setattr(stream_pool, "get_stream_reader_client", _reader)


@pytest.fixture(autouse=True)
def _empty_user_skill_bundle(monkeypatch):
    """Stub the per-turn user-skill bundle to empty for unit tests.

    ``resolve_llm_config`` loads it from Postgres on every turn; unit tests
    have no pool. Both binding sites are patched: the package attribute the
    lazy imports resolve, and the defining module's global that
    ``sandbox_skill_sync_params`` calls. Tests *about* the bundle itself put
    the real function back — see ``server/services/user_skills/conftest.py``.
    """
    from src.server.services import user_skills
    from src.server.services.user_skills import materialize

    async def _empty(user_id, workspace_id=None):
        return user_skills.EMPTY_USER_SKILL_BUNDLE

    monkeypatch.setattr(user_skills, "load_user_skill_bundle", _empty)
    monkeypatch.setattr(materialize, "load_user_skill_bundle", _empty)


@pytest.fixture(autouse=True)
def _no_user_subagents(monkeypatch):
    """Stub each user's subagent rows, and their plugins' subagents, to none
    for unit tests.

    ``resolve_llm_config`` reads them from Postgres on every PTC turn, and
    unit tests have no pool. A user with no rows is also the case whose turn
    must match a build that never read any.
    """
    from src.server.database import plugin_subagents, user_subagents

    async def _none(user_id, *, conn=None):
        return []

    @contextlib.asynccontextmanager
    async def _no_snapshot():
        yield None

    monkeypatch.setattr(user_subagents, "list_user_subagents", _none)
    monkeypatch.setattr(plugin_subagents, "list_user_plugin_subagents", _none)
    monkeypatch.setattr(plugin_subagents, "subagents_snapshot", _no_snapshot)


@pytest.fixture(autouse=True)
def _no_host_claude_code_managed(monkeypatch, tmp_path_factory):
    """Point Claude Code's managed-settings paths at folders that do not exist.

    The check runs the real script, so on a machine with an organization's
    managed settings installed (``/etc/claude-code``) every computer the
    tests stand up, the backend's own included, would read as policy-blocked.
    Tests *about* the check set these paths themselves.
    """
    from ptc_agent.core import claude_code_managed

    empty = tmp_path_factory.getbasetemp() / "no-claude-code-managed"
    for name in ("CLAUDE_CODE_MANAGED_DIR_LINUX", "CLAUDE_CODE_MANAGED_DIR_MAC", "MAC_MANAGED_PREFERENCES_DIR"):
        monkeypatch.setattr(claude_code_managed, name, str(empty / name))


@pytest.fixture(autouse=True)
def _no_claude_plan_rows(monkeypatch):
    """Stub the Claude plan's rows (``claude_code_plan``) to none for unit tests.

    Settings, admission, every token save and removal, and a Claude Code
    run's reports read or write them, and unit tests have no pool. A test
    about the plan state installs its own store over this one.
    """
    from src.server.database import claude_code_plan

    async def _none(*_args, **_kwargs):
        return None

    async def _kept(*_args, **_kwargs):
        return True

    async def _no_rows(user_id, credentials=None):
        return {}

    monkeypatch.setattr(claude_code_plan, "forget_plan", _none)
    monkeypatch.setattr(claude_code_plan, "drop_plan", _none)
    monkeypatch.setattr(claude_code_plan, "update_plan", _kept)
    monkeypatch.setattr(claude_code_plan, "get_plans", _no_rows)


@pytest.fixture(autouse=True)
def _coverage_units_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``coverage_units.enabled`` off by default in unit tests.

    ``src/dalton/dalton.yaml`` enables ``coverage_units`` (``true``) in
    production, which causes publish, tracking, knowledge, and system-job paths
    to query ``dalton_coverage_units`` through the Postgres pool. Existing
    pool-less unit tests were written assuming the switch starts off; tests
    that exercise the enabled registry opt in explicitly via
    ``DALTON_COVERAGE_UNITS_ENABLED=true`` (or patching
    ``is_coverage_units_enabled``).
    """
    monkeypatch.setenv("DALTON_COVERAGE_UNITS_ENABLED", "false")


# DALTON-HOOK(deliverable-text): keep upstream unit tests on upstream semantics while Dalton unit tests exercise production defaults
@pytest.fixture(autouse=True)
def _dalton_deliverable_switches_off_for_upstream_unit_tests(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default Dalton strict deliverable-text and near-empty retry switches off
    for upstream unit tests outside ``tests/unit/dalton/`` so upstream contracts
    stay unchanged; Dalton tests continue running with production defaults.
    """
    parts = Path(str(request.path)).parts
    if "dalton" in parts:
        return
    monkeypatch.setenv("DALTON_STRICT_DELIVERABLE_TEXT", "0")
    monkeypatch.setenv("DALTON_NEAR_EMPTY_RETRY_ENABLED", "0")


@pytest.fixture(scope="session")
def shipped_skill_md():
    """Locate a shipped skill's SKILL.md on disk, by name.

    A registry entry's ``skill_md_path`` is where the *agent* reaches the file
    inside a sandbox, not where this repo keeps it: the shipped skills live in
    the bundle that declares them, and the sync flattens every source into one
    directory. Joining that suffix onto the repo root reads as correct and is
    not, so the lookup asks the bundles instead.
    """
    from ptc_agent.config.plugins import bundled_skill_dirs

    def find(name: str) -> Path:
        for root in bundled_skill_dirs():
            candidate = root / name / "SKILL.md"
            if candidate.is_file():
                return candidate
        raise AssertionError(f"no bundle ships a {name!r} skill")

    return find


@pytest.fixture(autouse=True)
def _config_caches_follow_the_test():
    """A test that loads config (often after switching APP_ENV / DALTON_PROFILE)
    leaves the result cached: the infrastructure config and the YAML file cache are
    not keyed by profile. monkeypatch restores the environment but not the caches,
    so a later test in the same worker would read that overlay. Clear them whenever
    this test loaded config; tests that load nothing pay nothing.
    """
    import src.config.core as config_core
    from ptc_agent.config import file_utils

    if not _CACHED_LOADER:
        # The lru_cache-wrapped loader as first imported, before any test patches it.
        _CACHED_LOADER.append(config_core.load_infrastructure_config)

    def _state() -> tuple[int, int]:
        return (_CACHED_LOADER[0].cache_info().misses, len(file_utils._config_cache))

    before = _state()
    yield
    if _state() != before:
        config_core.clear_config_cache()
        # Reload under the restored environment, so the next test finds the caches
        # warm (some tests patch os.getenv wholesale and must not trigger a reload).
        with contextlib.suppress(Exception):
            config_core.load_infrastructure_config()


_CACHED_LOADER: list = []
