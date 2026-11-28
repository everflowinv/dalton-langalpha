#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_CODE="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
IMAGE_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
IMAGE_PY="${IMAGE_HOME}/venvs/company-wiki/bin/python"

if [[ ! -x "${IMAGE_PY}" ]]; then
    echo '{"ok":false,"error":"wiki Python missing","hint":"Restore the existing company-wiki environment."}'
    exit 1
fi
exec "${IMAGE_PY}" -W ignore "${IMAGE_CODE}/scripts/research_images.py" "$@"
