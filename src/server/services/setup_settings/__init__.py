"""Vendor keys and the sandbox choice a self-hosted deployment sets in the app.

They live in one encrypted table and reach existing code as environment values,
so the code that reads a key stays as it is. A value set outside the app wins.

Across workers: Postgres is the truth, and each worker applies the whole table
to itself. A save nudges the others over Redis, and every worker also re-reads
the table on a timer, so a missed nudge only delays a change (``follow``).

Only an open-source server has any of this: a hosted server's keys and sandbox
provider are its operator's, set in its environment. ``boot``, ``start`` and
``stop`` are what the server's lifespan calls, and do nothing on a hosted one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.server.services.setup_settings.apply import ConfigReading


def self_hosted() -> bool:
    from src.config.settings import HOST_MODE
    # DALTON-HOOK(dalton-auth): the setup routes trust whoever reaches them, so
    # they stay off under any Dalton deployment (even OSS dev config) or sign-in mode.
    from src.dalton.settings import get_dalton_auth_mode, is_dalton_enabled

    return (
        HOST_MODE == "oss"
        and not is_dalton_enabled()
        and get_dalton_auth_mode() == "oss"
    )


# DALTON-HOOK(deployment-credentials): keep /api/v1/setup/* closed under Dalton while enabling
# saved deployment settings loading and preserving Dalton's autodetect=False sandbox policy.
def loads_saved_settings() -> bool:
    from src.config.settings import HOST_MODE

    return HOST_MODE == "oss"


def autodetect_sandbox() -> bool:
    from src.dalton.settings import is_dalton_enabled

    return not is_dalton_enabled()


async def boot() -> ConfigReading:
    """Before config load: apply the saved settings, and say how the loader
    reads what the config file leaves open."""
    from src.server.services.setup_settings.apply import ConfigReading, load_at_boot

    # DALTON-HOOK(deployment-credentials): load saved settings in HOST_MODE=oss even under Dalton
    if not loads_saved_settings():
        return ConfigReading(autodetect=False)
    return await load_at_boot()


def start() -> None:
    """Once the server is up: keep this worker on the saved settings."""
    from src.server.services.setup_settings.follow import start_following

    # DALTON-HOOK(deployment-credentials): follow Redis/60s updates whenever saved settings are loaded
    if loads_saved_settings():
        start_following()


async def stop() -> None:
    from src.server.services.setup_settings.follow import stop_following

    await stop_following()
