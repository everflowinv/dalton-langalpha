#!/usr/bin/env bash
# scripts/dalton/install_broker_v2.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLUGIN_REL="integrations/openclaw/dalton-langalpha-broker"
PLUGIN_SRC="${REPO_ROOT}/${PLUGIN_REL}"
NODE_BIN="${HOME}/.openclaw/tools/node/bin/node"

if [[ ! -x "${NODE_BIN}" ]]; then
  echo "ERROR: OpenClaw Node binary not found at ${NODE_BIN}" >&2
  exit 1
fi

echo "[1/5] Running broker v2 unit tests..."
"${NODE_BIN}" --test "${PLUGIN_SRC}/test/"*.test.mjs

if git -C "${REPO_ROOT}" cat-file -e "HEAD:${PLUGIN_REL}/openclaw.plugin.json" 2>/dev/null && \
   [[ -z "$(git -C "${REPO_ROOT}" status --porcelain "${PLUGIN_REL}")" ]]; then
  GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD)"
  ARCHIVE_MODE="git-archive"
else
  GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD 2>/dev/null || echo "worktree")"
  ARCHIVE_MODE="working-tree"
fi

MANAGED_BASE="${HOME}/.openclaw/managed-plugins"
DEST_DIR="${MANAGED_BASE}/dalton-langalpha-broker-${GIT_SHA}"

echo "[2/5] Exporting clean copy (${ARCHIVE_MODE}) to ${DEST_DIR}..."
mkdir -p "${MANAGED_BASE}"
rm -rf "${DEST_DIR}"
mkdir -p "${DEST_DIR}"

if [[ "${ARCHIVE_MODE}" == "git-archive" ]]; then
  git -C "${REPO_ROOT}" archive "HEAD:${PLUGIN_REL}" | tar -x -C "${DEST_DIR}"
else
  tar -C "${PLUGIN_SRC}" --exclude="node_modules" --exclude=".DS_Store" -cf - . | tar -C "${DEST_DIR}" -xf -
fi

echo "[3/5] Enforcing OpenClaw 2026.10.4 anti-quarantine permissions..."
find "${DEST_DIR}" -type l -delete
find "${DEST_DIR}" -type d -exec chmod 0755 {} +
find "${DEST_DIR}" -type f -exec chmod 0644 {} +

echo "[4/5] Verifying installed artifact..."
"${NODE_BIN}" -e '
const fs = require("node:fs");
const path = require("node:path");
const dest = process.argv[1];
const manifest = JSON.parse(fs.readFileSync(path.join(dest, "openclaw.plugin.json"), "utf8"));
if (manifest.id !== "dalton-langalpha-broker") {
  throw new Error("Unexpected plugin id: " + manifest.id);
}
console.log("Verified manifest:", manifest.id, manifest.version, "at", dest);
' "${DEST_DIR}"

echo "[5/5] Ready. Installed plugin path:"
echo "  ${DEST_DIR}"
echo ""
echo "Ensure ~/.openclaw/openclaw.json contains:"
echo "  plugins.load.paths includes \"${DEST_DIR}\""
echo "  plugins.allow includes \"dalton-langalpha-broker\""
echo "  plugins.entries[\"dalton-langalpha-broker\"].enabled = true"
