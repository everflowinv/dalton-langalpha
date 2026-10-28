"""Each process keeps its own yfinance cache folder."""

from __future__ import annotations

import multiprocessing as mp
import os
from unittest.mock import create_autospec

import yfinance.cache as yf_cache

import src.data_client.yfinance  # noqa: F401  (importing the package configures it)
from src.dalton import yfinance_cache


def _touch_all_three_caches(tag: str) -> dict[str, str]:
    tz = yf_cache.get_tz_cache()
    tz.store(f"SYM_{tag}", "America/New_York")
    assert tz.lookup(f"SYM_{tag}") == "America/New_York"

    cookie = yf_cache.get_cookie_cache()
    # A cookie shaped as yfinance's own: the A3 guard (upstream 982b0a5cd)
    # reads one without the A3 path as a miss and deletes it, so the fork
    # test's sentinel has to be a valid entry.
    cookie.store(f"strat_{tag}", {"cache_key": {"/": {"A3": f"a3-{tag}"}}})
    assert cookie.lookup(f"strat_{tag}")["cookie"]["cache_key"]["/"]["A3"] == f"a3-{tag}"

    isin = yf_cache.get_isin_cache()
    isin.store(f"ISIN_{tag}", f"SYM_{tag}")
    assert isin.lookup(f"ISIN_{tag}") == f"SYM_{tag}"

    return {
        "pid": str(os.getpid()),
        "tz_db": os.path.abspath(tz.db.database),
        "cookie_db": os.path.abspath(cookie.db.database),
        "isin_db": os.path.abspath(isin.db.database),
        "tz_proxy": os.path.abspath(yf_cache.tz_db_proxy.obj.database),
        "cookie_proxy": os.path.abspath(yf_cache.Cookie_db_proxy.obj.database),
        "isin_proxy": os.path.abspath(yf_cache.isin_db_proxy.obj.database),
    }


def _child_worker(conn) -> None:
    try:
        conn.send(_touch_all_three_caches("child"))
    finally:
        conn.close()


def test_the_cache_lives_in_a_folder_of_this_process():
    for manager in (yf_cache._TzDBManager, yf_cache._CookieDBManager, yf_cache._ISINDBManager):
        location = manager.get_location()
        assert os.path.basename(location) == str(os.getpid())
        assert "langalpha-yfinance" in location


def test_fork_rebuilds_all_three_caches_onto_child_databases():
    parent_info = _touch_all_three_caches("parent")
    ctx = mp.get_context("fork")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_worker, args=(send_conn,))
    proc.start()
    send_conn.close()
    try:
        child_info = recv_conn.recv()
    finally:
        recv_conn.close()
        proc.join(timeout=10)

    assert proc.exitcode == 0
    assert child_info["pid"] != parent_info["pid"]
    for key in ("tz_db", "cookie_db", "isin_db", "tz_proxy", "cookie_proxy", "isin_proxy"):
        assert child_info[key] != parent_info[key]
        assert os.path.basename(os.path.dirname(child_info[key])) == child_info["pid"]
    assert yf_cache.get_tz_cache().lookup("SYM_child") is None


def _child_cookie_guard(conn):
    try:
        conn.send(bool(getattr(yf_cache.get_cookie_cache(), "checks_a3", False)))
    finally:
        conn.close()


def test_fork_keeps_the_a3_cookie_guard():
    """The fork reset drops the cookie cache; the child must get the A3 guard back."""
    import src.data_client.yfinance.yahoo  # noqa: F401  (installs the guard in the parent)

    assert getattr(yf_cache.get_cookie_cache(), "checks_a3", False)
    ctx = mp.get_context("fork")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_cookie_guard, args=(send_conn,))
    proc.start()
    send_conn.close()
    try:
        guarded_in_child = recv_conn.recv()
    finally:
        recv_conn.close()
        proc.join(timeout=10)
    assert proc.exitcode == 0
    assert guarded_in_child is True


def test_cleanup_stale_cache_dirs_removes_only_exited_pid_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        yfinance_cache,
        "_base_cache_dir",
        create_autospec(yfinance_cache._base_cache_dir, return_value=tmp_path),
    )

    exited_dir = tmp_path / "999991"
    exited_dir.mkdir()

    alive_dir = tmp_path / "999992"
    alive_dir.mkdir()

    perm_alive_dir = tmp_path / "999993"
    perm_alive_dir.mkdir()

    unparseable_dir = tmp_path / "not-a-pid"
    unparseable_dir.mkdir()

    own_dir = tmp_path / str(os.getpid())
    own_dir.mkdir()

    def _probe_pid(pid: int, sig: int) -> None:
        if pid == 999991:
            raise ProcessLookupError
        if pid == 999993:
            raise PermissionError

    monkeypatch.setattr(
        yfinance_cache.os,
        "kill",
        create_autospec(os.kill, side_effect=_probe_pid),
    )

    yfinance_cache._cleanup_stale_cache_dirs()

    assert not exited_dir.exists()
    assert alive_dir.exists()
    assert perm_alive_dir.exists()
    assert unparseable_dir.exists()
    assert own_dir.exists()
