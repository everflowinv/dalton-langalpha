#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    IMAGE_CODE="${DALTON_OCW_ROOT}"
    IMAGE_PY="${DALTON_OCW_ROOT}/skills/company-wiki/venv/bin/python"
else
    IMAGE_CODE="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
    IMAGE_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    IMAGE_PY="${IMAGE_HOME}/venvs/company-wiki/bin/python"
fi

if [[ ! -x "${IMAGE_PY}" ]]; then
    echo '{"ok":false,"error":"wiki Python missing","hint":"Restore the existing company-wiki environment."}'
    exit 1
fi
exec "${IMAGE_PY}" -W ignore "${IMAGE_CODE}/scripts/research_images.py" "$@"
