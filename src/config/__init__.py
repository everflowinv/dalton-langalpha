import os
from pathlib import Path

from .core import load_yaml_config

from dotenv import load_dotenv

# DALTON-HOOK(conversation-archive): bind explicit dotenv file when configured
_explicit_dotenv = (
    os.environ.get("DALTON_DOTENV_PATH", "").strip()
    or os.environ.get("DALTON_REPO_ENV_FILE", "").strip()
)
if _explicit_dotenv:
    load_dotenv(Path(_explicit_dotenv).expanduser())
else:
    load_dotenv()


__all__ = [
    # Utilities
    "load_yaml_config",
]
