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

# 排除只在 Darwin 上追加。Linux 不排除，跑全部 tests/unit。
# 只取消本机确认失败的用例（依赖 Linux 文件语义：xattr、lchown、setuid、命名管道、
# 沙箱内改名脚本），同文件其余用例照常运行：test_tree_runtime.py 21 条、
# test_folder_move_script.py 17 条、test_livefs_daemon.py 16 条。
# 引号写进字符串里：run_and_record 用 bash -c 执行，参数化 id 的 [...] 否则会被当成通配。
unit_cmd="uv run --with pytest-xdist pytest tests/unit -q -n ${DALTON_BASELINE_WORKERS:-4} --dist loadgroup --durations=25"
if [[ "$(uname -s)" == "Darwin" ]]; then
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_copy_the_deadline_cuts_short_resumes_where_it_stopped'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_copy_made_again_writes_only_what_changed_since'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_too_large_for_one_pass_is_copied_over_several'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_stale_copy_frees_its_room_before_the_new_one_is_measured'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_names_linked_to_one_file_stay_one_file_in_the_copy'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_written_while_it_is_read_is_read_again'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_the_deadline_cuts_off_after_its_last_chunk_is_finished'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_what_its_owner_shut_itself_out_of_is_copied_and_shut_again'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_an_entry_made_where_the_copy_already_passed_is_in_it'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_deleted_while_the_copy_walks_goes_from_it[listed]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_deleted_while_the_copy_walks_goes_from_it[opened]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_deleted_while_the_copy_walks_goes_from_it[read]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_folder_deleted_while_the_copy_walks_goes_from_it[listed]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_linked_name_moved_away_before_its_copy_leaves_the_rest_one_file'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_linked_name_replaced_while_it_is_copied_leaves_the_others_their_own_file'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_folder_changed_after_the_walk_listed_it_takes_another_walk'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_named_pipe_keeps_its_mode_past_the_umask'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_link_and_a_pipe_keep_their_times'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_link_and_a_pipe_given_another_owner_since_the_copy_are_made_again'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_copy_made_as_root_keeps_setuid_and_setgid'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_tree_runtime.py::test_a_file_that_became_a_socket_leaves_no_copy_behind'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_on_a_restored_disk_a_rename_copies_the_folder_into_place'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_on_a_restored_disk_a_deleted_workspace_is_deleted_where_it_is'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_on_a_restored_disk_a_moved_workspace_is_copied_under_leftovers'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_the_deadline_cuts_short_lands_on_a_later_pass'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_on_a_restored_disk_an_unowned_folder_is_copied_aside'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_an_unowned_folder_copied_aside_whose_delete_is_cut_short_is_not_blocked'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_with_no_room_is_deleted_before_its_row_is_blocked'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_aside_with_no_room_is_deleted_before_its_row_is_blocked'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_aside_the_deadline_cuts_short_goes_on_from_there'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_aside_cut_short_is_kept_only_while_its_row_still_has_to_move'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_aside_cut_short_goes_once_its_row_is_renamed_elsewhere'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_an_unowned_folder_whose_delete_fails_after_its_copy_is_not_copied_again'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_row_staged_across_an_archive_lands_by_copy'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_row_copied_onto_its_target_stays_there_while_staging_takes_passes_to_go'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_delete_cut_short_after_the_copy_loses_the_sync_marker_first'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_a_copy_committed_before_an_archive_still_finishes_its_delete'"
  unit_cmd+=" --deselect 'tests/unit/server/services/test_folder_move_script.py::test_on_a_restored_disk_a_kept_folder_is_copied_aside_not_deleted'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_folder_a_restored_sandbox_cannot_rename_is_copied_aside'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_read_only_folder_in_one_a_restored_sandbox_cannot_rename_is_deleted'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_named_pipe_in_one_a_restored_sandbox_cannot_rename_is_made_anew'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_file_with_two_names_in_one_a_restored_sandbox_cannot_rename_stays_one'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_copy_a_killed_pass_left_goes_before_the_next'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_folder_copied_aside_whose_delete_fails_is_not_copied_again'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_failed_delete_looks_inside_a_folder_shut_to_its_owner[False]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_failed_delete_looks_inside_a_folder_shut_to_its_owner[True]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_what_is_written_to_a_folder_whose_delete_failed_is_set_aside_not_deleted'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_folder_changed_where_the_delete_failed_is_set_aside_not_deleted[folder made]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_folder_changed_where_the_delete_failed_is_set_aside_not_deleted[mode changed]'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_delete_cut_short_in_a_folder_shut_to_its_owner_is_not_copied_again'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_copy_aside_whose_delete_is_killed_is_not_copied_again'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_copy_aside_whose_delete_is_killed_is_finished_once_its_link_is_dropped'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_file_written_where_a_copy_aside_already_passed_is_in_it'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/core/sandbox/test_livefs_daemon.py::test_a_folder_still_changing_as_it_is_copied_aside_waits_for_the_next_link'"
  unit_cmd+=" --deselect 'tests/unit/ptc_agent/agent/claude_code/test_ssh_launcher.py::test_the_cli_is_signalled_only_while_its_pid_still_runs_it[other]'"
  unit_cmd+=" --deselect 'tests/unit/core/sandbox/test_skill_two_tiers.py::TestScriptTransport::test_many_long_staging_paths_use_an_archive'"
fi

run_and_record \
  "Baseline 2/4: Python Unit Tests" \
  "$unit_cmd"

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
