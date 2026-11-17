#!/usr/bin/env bash
# 惯例路径转发：Skill Workshop 只允许 scripts/ 等目录存放支持文件，
# 真正的 bootstrap wrapper 在 scripts/run.sh。这里保持 skills/<name>/run.sh 可用。
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/run.sh" "$@"
