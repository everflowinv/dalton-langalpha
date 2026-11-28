"""``python -m src.dalton.transcription_service``: run the standalone transcription service.

Single uvicorn worker on purpose: ``AsrOwnerLock`` (``<DALTON_ASR_JOBS_DIR>/.owner.lock``)
and the in-process asyncio task table / transcode semaphore assume one process owns
the jobs directory at a time.

Optional host env file (used by macOS launchd): set ``DALTON_TRANSCRIPTION_ENV_FILE``
(e.g. ``~/.config/dalton/transcription.env``); explicit process env vars always take
precedence over values loaded from the env file.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn

from src.dalton.uvicorn_runtime import configure_uv_threadpool_size, default_uvicorn_loop

from .app import TranscriptionServiceConfig, create_app

logger = logging.getLogger("dalton-transcription")


def _load_optional_env_file(env_file_path: str) -> None:
    """Populate unset ``os.environ`` keys from an explicit ``KEY=VALUE`` env file."""
    cleaned = str(env_file_path or "").strip()
    if not cleaned:
        return
    path = Path(cleaned).expanduser()
    if not path.is_file():
        logger.warning("DALTON_TRANSCRIPTION_ENV_FILE=%s does not exist; continuing with process env", path)
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    env_file = os.environ.get("DALTON_TRANSCRIPTION_ENV_FILE", "").strip()
    if env_file:
        _load_optional_env_file(env_file)

    cfg = TranscriptionServiceConfig.from_env()
    if not cfg.token:
        logger.error(
            "DALTON_TRANSCRIPTION_TOKEN is empty: every /transcriptions* request will be "
            "refused (401). Set DALTON_TRANSCRIPTION_TOKEN in the service environment."
        )
    logger.info(
        "Starting dalton-transcription on http://%s:%d (jobs_dir=%s, disabled=%s, worker_mode=%s)",
        cfg.host,
        cfg.port,
        cfg.jobs_dir,
        cfg.disabled,
        cfg.worker_mode,
    )
    configure_uv_threadpool_size()
    uvicorn.run(
        create_app(config=cfg),
        host=cfg.host,
        port=cfg.port,
        workers=1,
        log_level="info",
        loop=default_uvicorn_loop(),
    )


if __name__ == "__main__":
    main()
