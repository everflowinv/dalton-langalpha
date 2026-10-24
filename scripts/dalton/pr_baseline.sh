#!/usr/bin/env bash
# scripts/dalton/pr_baseline.sh
#
# Run the 4 mandatory baseline commands (plus J1 acceptance/debug runs) on a clean working tree,
# persist untruncated raw outputs under var/dalton/pr_baseline/<head_sha>/, capture exit codes,
# git rev-parse HEAD, and start/end UTC timestamps into a single markdown file, and publish
# directly via `gh pr comment <PR> --body-file <file>` without manual copy/editing
# (P0J-round4-fixes.md §8.1 #15, PR #25 Review Items 5, 6, 7, 11).
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
REMOTE_HEAD_OID="n/a"

# 2. When --pr is provided, verify remote PR headRefOid matches local HEAD before running/publishing (Review Item 6)
if [[ -n "$PR_NUMBER" ]]; then
  REMOTE_HEAD_OID="$(gh pr view "$PR_NUMBER" --json headRefOid --jq .headRefOid)"
  if [[ "$REMOTE_HEAD_OID" != "$HEAD_FULL" ]]; then
    echo "ERROR: PR #${PR_NUMBER} remote headRefOid (${REMOTE_HEAD_OID}) != local HEAD (${HEAD_FULL})." >&2
    echo "Push your local commits first (git push) before running pr_baseline.sh." >&2
    exit 1
  fi
fi

# 3. Persist untruncated raw logs under var/dalton/pr_baseline/<HEAD_FULL>/<UTC_TIMESTAMP>/ so reruns never overwrite prior logs
RUN_TS_DIR="$(date -u +"%Y%m%dT%H%M%SZ")"
RAW_LOG_DIR="var/dalton/pr_baseline/${HEAD_FULL}/${RUN_TS_DIR}"
mkdir -p "$RAW_LOG_DIR"

if [[ -z "$OUTPUT_FILE" ]]; then
  OUTPUT_FILE="${RAW_LOG_DIR}/pr_baseline_report.md"
else
  mkdir -p "$(dirname "$OUTPUT_FILE")"
fi

BODY_SECTIONS_FILE="$(mktemp "/tmp/pr_baseline_sections_${HEAD_SHORT}_XXXXXX")"
trap 'rm -f "$BODY_SECTIONS_FILE" "${CURRENT_TMP_OUT:-}"; git checkout -- README.md 2>/dev/null || true' EXIT; trap 'exit 130' INT; trap 'exit 143' TERM

OVERALL_FAILURES=0
FAILED_TITLES=()
STEP_INDEX=0

run_and_record() {
  local title="$1"
  local cmd_str="$2"
  local cmd_start_utc
  local cmd_end_utc
  local cmd_head
  local exit_code=0
  local tmp_out
  local raw_log_file
  local clean_bytes
  local clean_lines
  local clean_sha
  local slug

  STEP_INDEX=$((STEP_INDEX + 1))
  slug="$(printf "%02d_%s" "$STEP_INDEX" "$(echo "$title" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_+|_+$//g' | cut -c1-48)")"
  raw_log_file="${RAW_LOG_DIR}/${slug}.log"

  cmd_start_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  cmd_head="$(git rev-parse HEAD)"
  tmp_out="$(mktemp)"; CURRENT_TMP_OUT="$tmp_out"

  echo "[pr_baseline] Running ($STEP_INDEX): $title" >&2
  set +e
  NO_COLOR=1 FORCE_COLOR=0 bash -c "$cmd_str" >"$tmp_out" 2>&1
  exit_code=$?
  set -e
  cmd_end_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if [[ "$exit_code" -ne 0 ]]; then
    OVERALL_FAILURES=$((OVERALL_FAILURES + 1))
    FAILED_TITLES+=("${title} (exit=${exit_code})")
  fi

  perl -pe 's/\e\[[0-9;]*[a-zA-Z]//g' "$tmp_out" > "$raw_log_file"
  rm -f "$tmp_out"

  clean_bytes="$(wc -c < "$raw_log_file" | tr -d ' ')"
  clean_lines="$(wc -l < "$raw_log_file" | tr -d ' ')"
  clean_sha="$(shasum -a 256 "$raw_log_file" | awk '{print $1}')"

  {
    echo ""
    echo "### ${title}"
    echo ""
    echo "- **Command**: \`${cmd_str}\`"
    echo "- **HEAD**: \`${cmd_head}\`"
    echo "- **Started (UTC)**: \`${cmd_start_utc}\`"
    echo "- **Ended (UTC)**: \`${cmd_end_utc}\`"
    echo "- **Exit code**: \`${exit_code}\`"
    echo "- **Persisted raw log**: \`${raw_log_file}\` (\`lines=${clean_lines}\`, \`bytes=${clean_bytes}\`, \`sha256=${clean_sha}\`)"
    echo ""
    echo "\`\`\`text"
    if [[ "$clean_bytes" -le 25000 ]]; then
      cat "$raw_log_file"
    else
      local err_matches
      if [[ "$clean_lines" -gt 100 ]]; then
        head -n 40 "$raw_log_file"
        echo ""
        echo "... [middle lines omitted for GitHub 65536-char comment limit; full untruncated log saved at ${raw_log_file} (lines=${clean_lines}, bytes=${clean_bytes}, sha256=${clean_sha})] ..."
        local mid_end=$((clean_lines - 60))
        err_matches="$(sed -n "41,${mid_end}p" "$raw_log_file" | grep -nE 'FAIL|Error|ERROR|failed|FAILED|Traceback|AssertionError' || true)"
        if [[ -n "$err_matches" ]]; then
          echo "--- [Extracted FAIL/Error lines from omitted middle section] ---"
          echo "$err_matches"
          echo "----------------------------------------------------------------"
        fi
        echo ""
        tail -n 60 "$raw_log_file"
      else
        head -c 10000 "$raw_log_file"
        echo ""
        echo "... [middle bytes omitted for GitHub 65536-char comment limit; full untruncated log saved at ${raw_log_file} (lines=${clean_lines}, bytes=${clean_bytes}, sha256=${clean_sha})] ..."
        err_matches="$(grep -nE 'FAIL|Error|ERROR|failed|FAILED|Traceback|AssertionError' "$raw_log_file" || true)"
        if [[ -n "$err_matches" ]]; then
          echo "--- [Extracted FAIL/Error lines from log] ---"
          echo "$err_matches"
          echo "---------------------------------------------"
        fi
        echo ""
        tail -c 10000 "$raw_log_file"
        echo ""
      fi
    fi
    echo "\`\`\`"
  } >> "$BODY_SECTIONS_FILE"
}

# Four mandatory baseline commands
run_and_record \
  "Baseline 1/4: Hook Audit" \
  "./scripts/dalton/check_hooks.sh --strict --audit"

run_and_record \
  "Baseline 2/4: Python Unit Tests" \
  "uv run --with pytest-xdist pytest tests/unit/dalton tests/unit/llms tests/unit/middleware/skills tests/unit/mcp_servers -q -n ${DALTON_BASELINE_WORKERS:-4} --dist loadgroup --durations=25"

run_and_record \
  "Baseline 3/4: OpenClaw Broker Node Tests" \
  "node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs"

run_and_record \
  "Baseline 4/4: Web Typecheck & Vitest" \
  "pnpm --dir web typecheck && pnpm --dir web test -- --run"

# J1 acceptance & debug runs (when not --baseline-only)
if [[ "$BASELINE_ONLY" -eq 0 ]]; then
  run_and_record \
    "J1 Debug 1/9: src/ Net Line Delta (git diff origin/dalton/main...HEAD --stat --shortstat -- src/)" \
    "git diff origin/dalton/main...HEAD --stat -- src/ && git diff --shortstat origin/dalton/main...HEAD -- src/"

  run_and_record \
    "J1 Debug 2/9: Live /api/v1/dalton/openclaw/health Probe (realrun-pm-a & realrun-user-b)" \
    "uv run python -c 'import json, os, httpx; from dotenv import load_dotenv; load_dotenv(\".env\"); tok = os.environ[\"INTERNAL_SERVICE_TOKEN\"]; c = httpx.Client(base_url=\"http://127.0.0.1:8000\", timeout=15.0, trust_env=False); print(\"=== realrun-pm-a ===\"); print(json.dumps(c.get(\"/api/v1/dalton/openclaw/health\", headers={\"X-User-Id\": \"realrun-pm-a\", \"X-Service-Token\": tok}).json(), indent=2, ensure_ascii=False)); print(\"=== realrun-user-b ===\"); print(json.dumps(c.get(\"/api/v1/dalton/openclaw/health\", headers={\"X-User-Id\": \"realrun-user-b\", \"X-Service-Token\": tok}).json(), indent=2, ensure_ascii=False))'"

  run_and_record \
    "J1 Debug 3/9: Pre-flight Environment Check (check_env.py)" \
    "uv run python scripts/dalton/realrun/check_env.py"

  run_and_record \
    "J1 Debug 4/9: Dirty Working Tree Rejection on g1_budget.py --record (Expected Non-Zero Exit + Dirty Reason)" \
    "printf '\n<!-- pr_baseline dirty probe -->\n' >> README.md && git status --porcelain README.md && set +e; out=\$(uv run python scripts/dalton/realrun/g1_budget.py --record 2>&1); rc=\$?; git checkout -- README.md; printf '%s\n' \"\$out\"; echo \"rejected_exit_code=\$rc\"; test \"\$rc\" -ne 0 && grep -q 'Working tree is dirty' <<<\"\$out\""

  run_and_record \
    "J1 Debug 5/9: Empty INTERNAL_SERVICE_TOKEN Rejection on check_env.py (Expected Non-Zero Exit + Token Reason)" \
    "set +e; out=\$(INTERNAL_SERVICE_TOKEN=\"\" uv run python scripts/dalton/realrun/check_env.py 2>&1); rc=\$?; printf '%s\n' \"\$out\"; echo \"rejected_exit_code=\$rc\"; test \"\$rc\" -ne 0 && grep -q 'INTERNAL_SERVICE_TOKEN is not set' <<<\"\$out\""

  run_and_record \
    "J1 Debug 6/9: Legacy Report Comparison Reproducing 5 Mismatched Rows & 5 G3 Mismatches (render_report_tables.py)" \
    "uv run python -c 'import json, subprocess, sys; out = subprocess.check_output([sys.executable, \"scripts/dalton/realrun/render_report_tables.py\", \"--compare-legacy-report\", \"docs/dalton/dev-guide/P0I-round3-dev-report.md\", \"--evidence-dir\", \"var/dalton/trial-runs/realrun-20261019\"], text=True); print(out, end=\"\"); data = json.loads(out); assert data[\"mismatched_table_rows_count\"] == 5 and len(data[\"g3_evaluation_mismatches\"]) == 5'"

  run_and_record \
    "J1 Debug 7/9: G2 Startup Fail-Fast Check (g2_providers.py --only-startup-check)" \
    "uv run python scripts/dalton/realrun/g2_providers.py --only-startup-check"

  run_and_record \
    "J1 Debug 8/9: G3 Live Migrated Thesis Version Check (g3_publish.py --only-migrated-check)" \
    "uv run python scripts/dalton/realrun/g3_publish.py --only-migrated-check"

  run_and_record \
    "J1 Debug 9/9: G6 Financial Lenses Debug Run (g6_lenses.py)" \
    "uv run python scripts/dalton/realrun/g6_lenses.py"
fi

if [[ -n "$PR_NUMBER" ]]; then
  run_and_record \
    "PR Description Claim Check (check_report_claims.py --strict on PR #${PR_NUMBER})" \
    "tmp_pr_md=\$(mktemp /tmp/pr_${PR_NUMBER}_body_XXXXXX) && trap 'rm -f \"\$tmp_pr_md\"' EXIT && gh pr view ${PR_NUMBER} --json body --jq .body > \"\$tmp_pr_md\" && uv run python scripts/dalton/check_report_claims.py --strict \"\$tmp_pr_md\""
fi

SUITE_END_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
DIRTY_AFTER="$(git status --porcelain)"
if [[ -n "$DIRTY_AFTER" ]]; then
  echo "ERROR: Working tree became dirty after running pr_baseline.sh!" >&2
  echo "$DIRTY_AFTER" >&2
  OVERALL_FAILURES=$((OVERALL_FAILURES + 1))
  FAILED_TITLES+=("Post-run git status --porcelain clean check")
fi

OVERALL_STATUS_LINE="PASS (0 failed)"
if [[ "$OVERALL_FAILURES" -ne 0 ]]; then
  OVERALL_STATUS_LINE="FAIL (${OVERALL_FAILURES} failed: ${FAILED_TITLES[*]})"
fi

{
  echo "## Automated Baseline & Debug Run Report (\`scripts/dalton/pr_baseline.sh\`)"
  echo ""
  echo "- **Overall Status**: **${OVERALL_STATUS_LINE}**"
  echo "- **Branch**: \`${BRANCH_NAME}\`"
  echo "- **HEAD (\`git rev-parse HEAD\`)**: \`${HEAD_FULL}\` (\`${HEAD_SHORT}\`)"
  if [[ -n "$PR_NUMBER" ]]; then
    echo "- **Remote PR #${PR_NUMBER} \`headRefOid\` verified**: \`${REMOTE_HEAD_OID}\` (matches local \`HEAD\`)"
  fi
  echo "- **Working tree before/after (\`git status --porcelain\`)**: $(if [[ -z "$DIRTY_AFTER" ]]; then echo "clean (empty)"; else echo "DIRTY"; fi)"
  echo "- **Persisted raw log directory**: \`${RAW_LOG_DIR}/\`"
  echo "- **Started at (UTC)**: \`${SUITE_START_UTC}\`"
  echo "- **Finished at (UTC)**: \`${SUITE_END_UTC}\`"
  echo "- **Mode**: $(if [[ "$BASELINE_ONLY" -eq 1 ]]; then echo "\`--baseline-only\` (4 baseline commands)"; else echo "\`default / --j1-debug\` (4 baseline commands + 9 J1 acceptance/debug runs)"; fi)"
  echo ""
  echo "---"
  cat "$BODY_SECTIONS_FILE"
  echo ""
  echo "---"
  echo ""
  echo "- **Finished at (UTC)**: \`${SUITE_END_UTC}\`"
  echo "- **Working tree after run (\`git status --porcelain\`)**: $(if [[ -z "$DIRTY_AFTER" ]]; then echo "clean (empty)"; else echo "DIRTY"; fi)"
  echo "- **Failed commands count**: \`${OVERALL_FAILURES}\`"
} > "$OUTPUT_FILE"

echo "[pr_baseline] Output written to: $OUTPUT_FILE (failures=$OVERALL_FAILURES)" >&2

# Always publish report (even when OVERALL_FAILURES > 0) before exiting non-zero (Review Item 5)
if [[ -n "$PR_NUMBER" && "$NO_COMMENT" -eq 0 ]]; then
  echo "[pr_baseline] Publishing comment to PR #${PR_NUMBER} via gh pr comment --body-file ..." >&2
  gh pr comment "$PR_NUMBER" --body-file "$OUTPUT_FILE"
fi

if [[ "$OVERALL_FAILURES" -ne 0 ]]; then
  echo "ERROR: $OVERALL_FAILURES command(s) failed during pr_baseline.sh." >&2
  exit 1
fi
