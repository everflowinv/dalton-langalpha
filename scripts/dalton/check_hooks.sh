#!/usr/bin/env bash
# Check all DALTON-HOOK(<id>) annotations across upstream files against the registered allowlist
# (P0 Appendix A + PK Section 5).
#
# Usage:
#   scripts/dalton/check_hooks.sh           # verify no unregistered hooks exist & no upstream files are modified without hooks
#   scripts/dalton/check_hooks.sh --strict  # also require all registered P0/P0D hooks to be present
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

STRICT=0
if [[ "${1:-}" == "--strict" ]]; then
  STRICT=1
fi

# Registered hook IDs from P0 Appendix A and PK Section 5
ALLOWED_HOOKS=(
  # P0 platform hooks
  "llm-overlay"
  "llm-broker-client"
  "catalog-cache"
  "router"
  "budget-gate"
  "budget-admission"
  "settings-section"
  "query-keys"
  "i18n"
  "pause-links"
  # P0D data & P1 skills hooks
  "mcp-call-timeout"
  "skills-registry"
  "provenance-declared"
  "provenance-type"
  "provenance-locator"
  # PK knowledge, publish, autonomy & UI hooks
  "dalton-tools"
  "knowledge-reconcile-mw"
  "knowledge-reconcile"
  "preflight"
  "precheck-outcome"
  "precheck-skip-reason"
  "precheck-hidden"
  "dalton-nav"
  "publish-card"
  "precheck-label"
)

P0_REQUIRED_HOOKS=(
  "llm-overlay"
  "llm-broker-client"
  "catalog-cache"
  "router"
  "budget-gate"
  "budget-admission"
  "settings-section"
  "query-keys"
  "pause-links"
  "mcp-call-timeout"
  "skills-registry"
  "provenance-declared"
  "provenance-type"
)

is_allowed() {
  local needle="$1"
  for id in "${ALLOWED_HOOKS[@]}"; do
    if [[ "$id" == "$needle" ]]; then
      return 0
    fi
  done
  return 1
}

# Find all DALTON-HOOK(<id>) markers outside docs/dalton and scripts/dalton/check_hooks.sh
MATCHES="$(git grep -n "DALTON-HOOK(" -- ':!docs/dalton/**' ':!scripts/dalton/check_hooks.sh' || true)"

echo "=== Registered DALTON-HOOK occurrences ==="
if [[ -z "$MATCHES" ]]; then
  echo "(none yet)"
else
  echo "$MATCHES"
fi

ERRORS=0
FOUND_IDS=()

while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  if [[ "$line" =~ DALTON-HOOK\(([a-z0-9-]+)\) ]]; then
    hook_id="${BASH_REMATCH[1]}"
    FOUND_IDS+=("$hook_id")
    if ! is_allowed "$hook_id"; then
      echo "ERROR: Unregistered DALTON-HOOK($hook_id) found: $line" >&2
      ERRORS=$((ERRORS + 1))
    fi
  else
    echo "ERROR: Malformed DALTON-HOOK marker (expected DALTON-HOOK(<id>)): $line" >&2
    ERRORS=$((ERRORS + 1))
  fi
done <<< "$MATCHES"

if [[ "$STRICT" -eq 1 ]]; then
  for req in "${P0_REQUIRED_HOOKS[@]}"; do
    found=0
    for f in "${FOUND_IDS[@]:-}"; do
      if [[ "$f" == "$req" ]]; then
        found=1
        break
      fi
    done
    if [[ "$found" -eq 0 ]]; then
      echo "ERROR (--strict): Required P0 hook DALTON-HOOK($req) is missing" >&2
      ERRORS=$((ERRORS + 1))
    fi
  done
fi

# Verify protected upstream config files are untouched vs upstream/main (if upstream/main ref exists)
if git rev-parse --verify upstream/main >/dev/null 2>&1; then
  UNTOUCHED_FILES=("src/llms/manifest/models.json" "src/llms/manifest/providers.json" "agent_config.yaml" "config.yaml")
  for uf in "${UNTOUCHED_FILES[@]}"; do
    if ! git diff --quiet upstream/main -- "$uf"; then
      echo "ERROR: Upstream file $uf was modified (must use overlay instead)" >&2
      ERRORS=$((ERRORS + 1))
    fi
  done
fi

if [[ "$ERRORS" -gt 0 ]]; then
  echo "FAILED: $ERRORS hook check error(s)." >&2
  exit 1
fi

echo "OK: DALTON-HOOK check passed."
