#!/usr/bin/env bash
# scripts/dalton/pr_baseline.sh
#
# Run the 4 mandatory baseline commands (plus PR debug runs) on a clean working tree,
# capture raw outputs, exit codes, git rev-parse HEAD, and start/end UTC timestamps into
# a single markdown file, and publish directly via `gh pr comment <PR> --body-file <file>`
# without manual copy/editing (P0J-round4-fixes.md §8.1 #15).
#
# Usage:
#   ./scripts/dalton/pr_baseline.sh [--pr <PR_NUMBER>] [--baseline-only] [--output <PATH>] [--no-comment]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

PR_NUMBER=""
BASELINE_ONLY=0
NO_COMMENT=0
OUTPUT_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pr)
      PR_NUMBER="${2:-}"
      shift 2
      ;;
    --baseline-only)
      BASELINE_ONLY=1
      shift
      ;;
    --j1-debug)
      BASELINE_ONLY=0
      shift
      ;;
    --output)
      OUTPUT_FILE="${2:-}"
      shift 2
      ;;
    --no-comment)
      NO_COMMENT=1
      shift
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      echo "Usage: $0 [--pr <PR_NUMBER>] [--baseline-only] [--output <PATH>] [--no-comment]" >&2
      exit 2
      ;;
  esac
done

# 1. Enforce clean working tree before running any command
DIRTY_BEFORE="$(git status --porcelain)"
if [[ -n "$DIRTY_BEFORE" ]]; then
  echo "ERROR: Working tree is dirty (git status --porcelain is non-empty). Refusing to run pr_baseline.sh." >&2
  echo "$DIRTY_BEFORE" >&2
  exit 1
fi

HEAD_FULL="$(git rev-parse HEAD)"
HEAD_SHORT="$(git rev-parse --short=8 HEAD)"
BRANCH_NAME="$(git rev-parse --abbrev-ref HEAD)"
SUITE_START_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

if [[ -z "$OUTPUT_FILE" ]]; then
  OUTPUT_FILE="/tmp/pr_baseline_${HEAD_SHORT}.md"
else
  mkdir -p "$(dirname "$OUTPUT_FILE")"
fi

OVERALL_FAILURES=0

cat > "$OUTPUT_FILE" <<EOF
## Automated Baseline & Debug Run Report (\`scripts/dalton/pr_baseline.sh\`)

- **Branch**: \`${BRANCH_NAME}\`
- **HEAD (\`git rev-parse HEAD\`)**: \`${HEAD_FULL}\` (\`${HEAD_SHORT}\`)
- **Working tree (\`git status --porcelain\`)**: clean (empty)
- **Started at (UTC)**: \`${SUITE_START_UTC}\`
- **Mode**: $(if [[ "$BASELINE_ONLY" -eq 1 ]]; then echo "\`--baseline-only\` (4 baseline commands)"; else echo "\`default / --j1-debug\` (4 baseline commands + J1 debug runs)"; fi)

---
EOF

run_and_record() {
  local title="$1"
  local cmd_str="$2"
  local cmd_start_utc
  local cmd_end_utc
  local cmd_head
  local exit_code=0
  local tmp_out

  cmd_start_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  cmd_head="$(git rev-parse HEAD)"
  tmp_out="$(mktemp)"

  echo "[pr_baseline] Running: $title" >&2
  set +e
  NO_COLOR=1 FORCE_COLOR=0 bash -c "$cmd_str" >"$tmp_out" 2>&1
  exit_code=$?
  set -e
  cmd_end_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if [[ "$exit_code" -ne 0 ]]; then
    OVERALL_FAILURES=$((OVERALL_FAILURES + 1))
  fi

  {
    echo ""
    echo "### ${title}"
    echo ""
    echo "- **Command**: \`${cmd_str}\`"
    echo "- **HEAD**: \`${cmd_head}\`"
    echo "- **Started (UTC)**: \`${cmd_start_utc}\`"
    echo "- **Ended (UTC)**: \`${cmd_end_utc}\`"
    echo "- **Exit code**: \`${exit_code}\`"
    echo ""
    echo "\`\`\`text"
    perl -pe 's/\e\[[0-9;]*[a-zA-Z]//g' "$tmp_out"
    echo "\`\`\`"
  } >> "$OUTPUT_FILE"

  rm -f "$tmp_out"
}

# Four mandatory baseline commands
run_and_record \
  "Baseline 1/4: Hook Audit" \
  "./scripts/dalton/check_hooks.sh --strict --audit"

run_and_record \
  "Baseline 2/4: Python Unit Tests" \
  "uv run pytest tests/unit/dalton tests/unit/llms tests/unit/middleware/skills tests/unit/mcp_servers -q"

run_and_record \
  "Baseline 3/4: OpenClaw Broker Node Tests" \
  "node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs"

run_and_record \
  "Baseline 4/4: Web Typecheck & Vitest" \
  "pnpm --dir web typecheck && pnpm --dir web test -- --run"

# J1 debug runs (when not --baseline-only)
if [[ "$BASELINE_ONLY" -eq 0 ]]; then
  run_and_record \
    "J1 Debug 1/5: Live /api/v1/dalton/openclaw/health Probe (realrun-pm-a & realrun-user-b)" \
    "uv run python -c 'import json, os, httpx; from dotenv import load_dotenv; load_dotenv(\".env\"); tok = os.environ[\"INTERNAL_SERVICE_TOKEN\"]; c = httpx.Client(base_url=\"http://127.0.0.1:8000\", timeout=15.0, trust_env=False); print(\"=== realrun-pm-a ===\"); print(json.dumps(c.get(\"/api/v1/dalton/openclaw/health\", headers={\"X-User-Id\": \"realrun-pm-a\", \"X-Service-Token\": tok}).json(), indent=2, ensure_ascii=False)); print(\"=== realrun-user-b ===\"); print(json.dumps(c.get(\"/api/v1/dalton/openclaw/health\", headers={\"X-User-Id\": \"realrun-user-b\", \"X-Service-Token\": tok}).json(), indent=2, ensure_ascii=False))'"

  run_and_record \
    "J1 Debug 2/5: Pre-flight Environment Check (check_env.py)" \
    "uv run python scripts/dalton/realrun/check_env.py"

  run_and_record \
    "J1 Debug 3/5: G2 Startup Fail-Fast Check (g2_providers.py --only-startup-check)" \
    "uv run python scripts/dalton/realrun/g2_providers.py --only-startup-check"

  run_and_record \
    "J1 Debug 4/5: G3 Live Migrated Thesis Version Check (g3_publish.py --only-migrated-check)" \
    "uv run python scripts/dalton/realrun/g3_publish.py --only-migrated-check"

  run_and_record \
    "J1 Debug 5/5: G6 Financial Lenses Debug Run (g6_lenses.py)" \
    "uv run python scripts/dalton/realrun/g6_lenses.py"
fi

if [[ -n "$PR_NUMBER" ]]; then
  run_and_record \
    "PR Description Claim Check (check_report_claims.py --strict on PR #${PR_NUMBER})" \
    "tmp_pr_md=\$(mktemp /tmp/pr_${PR_NUMBER}_body_XXXXXX.md) && trap 'rm -f \"\$tmp_pr_md\"' EXIT && gh pr view ${PR_NUMBER} --json body --jq .body > \"\$tmp_pr_md\" && uv run python scripts/dalton/check_report_claims.py --strict \"\$tmp_pr_md\""
fi

SUITE_END_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
DIRTY_AFTER="$(git status --porcelain)"
if [[ -n "$DIRTY_AFTER" ]]; then
  echo "ERROR: Working tree became dirty after running pr_baseline.sh!" >&2
  echo "$DIRTY_AFTER" >&2
  OVERALL_FAILURES=$((OVERALL_FAILURES + 1))
fi

{
  echo ""
  echo "---"
  echo ""
  echo "- **Finished at (UTC)**: \`${SUITE_END_UTC}\`"
  echo "- **Working tree after run (\`git status --porcelain\`)**: $(if [[ -z "$DIRTY_AFTER" ]]; then echo "clean (empty)"; else echo "DIRTY"; fi)"
  echo "- **Failed commands count**: \`${OVERALL_FAILURES}\`"
} >> "$OUTPUT_FILE"

echo "[pr_baseline] Output written to: $OUTPUT_FILE (failures=$OVERALL_FAILURES)" >&2

if [[ "$OVERALL_FAILURES" -ne 0 ]]; then
  echo "ERROR: $OVERALL_FAILURES command(s) failed during pr_baseline.sh; refusing to publish failing report." >&2
  exit 1
fi

if [[ -n "$PR_NUMBER" && "$NO_COMMENT" -eq 0 ]]; then
  echo "[pr_baseline] Publishing comment to PR #${PR_NUMBER} via gh pr comment --body-file ..." >&2
  gh pr comment "$PR_NUMBER" --body-file "$OUTPUT_FILE"
fi
