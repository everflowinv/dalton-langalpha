"""Give each backend process its own yfinance cache directory.

yfinance keeps its timezone, cookie and ISIN caches in sqlite files under one
per-user folder, and every new connection runs ``PRAGMA journal_mode = wal``.
Under ``--workers N`` the workers open those files at the same moment, and
that pragma needs a lock sqlite's busy timeout does not wait out, so the
losing worker's first quote raises ``database is locked``. The caches only
hold what yfinance can fetch again (a ticker's timezone, a session cookie),
so sharing them buys little; a folder per process removes the contention.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIRNAME = "langalpha-yfinance"

# Verified against yfinance 1.7.0 (yfinance/cache.py): each cache keeps a
# manager singleton (_TzCache / _CookieCache / _ISINCache holding .db and
# .initialised), a DB manager (_TzDBManager / _CookieDBManager / _ISINDBManager
# holding ._db), and a peewee Proxy bound to its model class.
_YFINANCE_CACHE_TRIPLETS = (
    ("_TzCacheManager", "_tz_cache", "_TzDBManager", "tz_db_proxy"),
    ("_CookieCacheManager", "_Cookie_cache", "_CookieDBManager", "Cookie_db_proxy"),
    ("_ISINCacheManager", "_isin_cache", "_ISINDBManager", "isin_db_proxy"),
)


def _base_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / _BASE_DIRNAME


def _process_cache_dir() -> Path:
    # Keyed by pid, under the temp dir: a live pid is unique, a reused pid
    # only finds a cache that is still valid, and the OS clears the rest.
    return _base_cache_dir() / str(os.getpid())


def _cleanup_stale_cache_dirs() -> None:
    """Remove sibling pid directories whose processes have exited; ignore errors."""
    base = _base_cache_dir()
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    current_pid = str(os.getpid())
    for entry in entries:
        if entry.name == current_pid:
            continue
        try:
            pid = int(entry.name)
        except ValueError:
            continue
        if pid <= 0:
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(entry, ignore_errors=True)
        except (PermissionError, OSError):
            continue


def _reset_cache_singletons() -> None:
    """Reset yfinance's cached singletons, sqlite connections and peewee proxies.

    Calling ``set_tz_cache_location`` alone only updates the DB managers' paths;
    if the parent process already touched a cache before ``fork()``, the cached
    ``_TzCache`` / ``_CookieCache`` / ``_ISINCache`` instances keep ``db`` and
    ``initialised == 1`` and the peewee model proxies stay bound to the parent's
    sqlite database. Targets yfinance 1.7.0 internals and degrades with a
    warning if a different version renames them.
    """
    try:
        import yfinance as yf
        import yfinance.cache as yf_cache

        if hasattr(yf_cache, "_cache_init_lock"):
            yf_cache._cache_init_lock = threading.Lock()

        for mgr_name, cache_attr, db_mgr_name, proxy_name in _YFINANCE_CACHE_TRIPLETS:
            mgr = getattr(yf_cache, mgr_name, None)
            db_mgr = getattr(yf_cache, db_mgr_name, None)
            proxy = getattr(yf_cache, proxy_name, None)
            if (
                mgr is None
                or db_mgr is None
                or proxy is None
                or not hasattr(mgr, cache_attr)
                or not hasattr(db_mgr, "_db")
                or not hasattr(proxy, "initialize")
            ):
                logger.warning(
                    "yfinance_cache: unexpected yfinance.cache layout (version=%s, missing %s/%s/%s); "
                    "falling back to set_tz_cache_location only",
                    getattr(yf, "__version__", "unknown"),
                    mgr_name,
                    db_mgr_name,
                    proxy_name,
                )
                return

            cache_obj = getattr(mgr, cache_attr, None)
            if cache_obj is not None:
                db = getattr(cache_obj, "db", None)
                if db is not None:
                    try:
                        db.close()
                    except Exception:
                        pass
                cache_obj.db = None
                cache_obj.initialised = -1
            setattr(mgr, cache_attr, None)

            db = getattr(db_mgr, "_db", None)
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
                db_mgr._db = None

            proxy.initialize(None)
    except Exception:
        logger.warning(
            "yfinance_cache: failed to reset cache singletons; degrading to set_tz_cache_location",
            exc_info=True,
        )


def _point_at_process_dir() -> None:
    import yfinance as yf

    _reset_cache_singletons()
    # yfinance creates the folder on first use, and if it cannot, runs
    # without the cache rather than failing the call.
    yf.set_tz_cache_location(str(_process_cache_dir()))


def configure_process_cache() -> None:
    """Point yfinance's caches at this process's folder, now and in any
    forked child, before the first ticker call opens the databases."""
    _cleanup_stale_cache_dirs()
    _point_at_process_dir()
    os.register_at_fork(after_in_child=_point_at_process_dir)
