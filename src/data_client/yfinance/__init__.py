"""Yahoo Finance data source backed by the yfinance library (free, no API key)."""

# DALTON-HOOK(yfinance-cache): one sqlite cache folder per worker process, so
# concurrent workers never contend for yfinance's shared cache databases.
from src.dalton.yfinance_cache import configure_process_cache as _configure_cache

_configure_cache()
