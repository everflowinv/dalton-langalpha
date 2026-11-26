#!/usr/bin/env bash
# Shared `.env` secret/token backfill helper for Standalone Core scripts
# (`init.sh`, `upgrade.sh`, `restore.sh`).
#
# Usage (sourced):
#   source "${SCRIPT_DIR}/env_tokens.sh"
#   added="$(ensure_env_tokens "$ENV_FILE" KEY_A KEY_B ...)"
#   added="$(ensure_research_host_tokens "$ENV_FILE")"
#
# Rules:
#   - Generates 32-byte lowercase hex secrets (`secrets.token_hex(32)`),
#     guaranteed distinct across all target keys.
#   - Fills ONLY missing, empty, or obvious dev-placeholder values
#     (`langalpha-local-dev-encryption-key`); never overwrites existing values.
#   - Writes atomically, creating the temp file with mode `0600`, preserving
#     original group ownership (`chgrp` / `os.chown(-1, gid)`) and restrictive
#     existing permissions (e.g. `0600` / `0640`), while tightening group-writable,
#     world-accessible, or unpreservable-group modes to `0600`.
#   - Prints space-separated list of newly generated key names on stdout.

ensure_env_tokens() {
  local env_path="$1"
  shift
  if [[ -z "$env_path" || ! -f "$env_path" ]]; then
    echo "ERROR: ensure_env_tokens requires an existing .env file: ${env_path}" >&2
    return 1
  fi
  if [[ "$#" -eq 0 ]]; then
    return 0
  fi
  python3 - "$env_path" "$@" <<'PY'
import os
import re
import secrets
import stat
import sys

env_path = sys.argv[1]
target_keys = [k.strip() for k in sys.argv[2:] if k.strip()]
if not target_keys:
    sys.exit(0)

PLACEHOLDER_VALUES = {"langalpha-local-dev-encryption-key"}
line_re = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")

with open(env_path, encoding="utf-8") as fh:
    lines = fh.readlines()

existing: dict[str, tuple[int, str]] = {}
for idx, raw in enumerate(lines):
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    m = line_re.match(raw.rstrip("\r\n"))
    if not m:
        continue
    key = m.group(2)
    val = m.group(4).strip()
    if val and val[0] in ("'", '"'):
        end = val.find(val[0], 1)
        val = val[1:end] if end > 0 else val[1:]
    elif val:
        val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
    existing[key] = (idx, val)

used_vals = {
    existing[k][1]
    for k in target_keys
    if k in existing and existing[k][1] and existing[k][1] not in PLACEHOLDER_VALUES
}
added_keys: list[str] = []

for key in target_keys:
    cur_idx, cur_val = existing.get(key, (-1, ""))
    if cur_val and cur_val not in PLACEHOLDER_VALUES:
        continue
    new_val = secrets.token_hex(32)
    while new_val in used_vals:
        new_val = secrets.token_hex(32)
    used_vals.add(new_val)
    new_line = f"{key}={new_val}\n"
    if cur_idx >= 0:
        lines[cur_idx] = new_line
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(new_line)
    added_keys.append(key)

if added_keys:
    st_info = os.stat(env_path)
    orig_mode = stat.S_IMODE(st_info.st_mode)
    orig_gid = st_info.st_gid
    target_mode = 0o600 if (orig_mode & 0o027) else orig_mode
    tmp_path = f"{env_path}.tmp-tokens-{os.getpid()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.writelines(lines)
            out.flush()
            os.fsync(out.fileno())
        group_preserved = False
        try:
            os.chown(tmp_path, -1, orig_gid)
            group_preserved = os.stat(tmp_path).st_gid == orig_gid
        except OSError:
            group_preserved = False
        if not group_preserved:
            target_mode = 0o600
        os.chmod(tmp_path, target_mode)
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

print(" ".join(added_keys))
PY
}

ensure_research_host_tokens() {
  local env_path="$1"
  ensure_env_tokens "$env_path" "DALTON_RESEARCH_HOST_TOKEN" "DALTON_RESEARCH_ADMIN_TOKEN"
}
