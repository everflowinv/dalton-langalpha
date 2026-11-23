#!/usr/bin/env bash
# Standalone Core initializer (environment checks + secret generation).
#
# Responsibilities:
#   1. Check Docker Engine (>= 28.0 for reliable 127.0.0.1 loopback port isolation),
#      Docker Compose (>= 2.24.4), memory (>= 8 GB recommended), and free disk
#      (>= 30 GB recommended).
#   2. Print the /var/run/docker.sock security & permissions notice.
#   3. Create/update .env (strictly mode 0600) from .env.core.example, generating
#      all required secrets with `openssl rand -hex 32` without overwriting
#      existing non-empty values, writing APP_ENV=core / DALTON_PROFILE=core and
#      per-instance COMPOSE_PROJECT_NAME / SANDBOX_DOCKER_NETWORK.
#   4. Hand interactive LLM / financial-data / web-search setup off to `make config`.
#
# Usage:
#   bash scripts/standalone/init.sh
#   bash scripts/standalone/init.sh --non-interactive --domain localhost
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.core.example"

NON_INTERACTIVE=0
DOMAIN_ARG=""
BUILD_SANDBOX=1

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/init.sh [OPTIONS]

Options:
  --non-interactive       Do not prompt; use defaults / flags / existing .env values.
  --domain <host>         Target domain (use 'localhost' for single-machine trial).
  --skip-sandbox-build    Leave the code sandbox image unbuilt (build it later with
                          docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest .).
  -h, --help              Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --skip-sandbox-build)
      BUILD_SANDBOX=0
      shift
      ;;
    --domain)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --domain requires a value" >&2
        exit 2
      fi
      DOMAIN_ARG="$2"
      shift 2
      ;;
    --domain=*)
      DOMAIN_ARG="${1#--domain=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

info()  { printf "  [INFO] %s\n" "$1"; }
ok()    { printf "  [OK]   %s\n" "$1"; }
warn()  { printf "  [WARN] %s\n" "$1" >&2; }
err()   { printf "  [ERR]  %s\n" "$1" >&2; }

version_ge() {
  # Compare dotted numeric versions: returns 0 if $1 >= $2.
  local v1="$1" v2="$2"
  local IFS=.
  local -a a1 a2
  read -r -a a1 <<< "$v1"
  read -r -a a2 <<< "$v2"
  local i max="${#a1[@]}"
  if (( ${#a2[@]} > max )); then max="${#a2[@]}"; fi
  for (( i = 0; i < max; i++ )); do
    local n1="${a1[i]:-0}" n2="${a2[i]:-0}"
    n1="${n1%%[^0-9]*}"; n2="${n2%%[^0-9]*}"
    n1="${n1:-0}"; n2="${n2:-0}"
    if (( 10#$n1 > 10#$n2 )); then return 0; fi
    if (( 10#$n1 < 10#$n2 )); then return 1; fi
  done
  return 0
}

check_environment() {
  printf "\n== Environment Checks ==\n"

  # 1. openssl
  if command -v openssl >/dev/null 2>&1; then
    ok "openssl found ($(openssl version 2>/dev/null || echo 'ok'))"
  else
    err "openssl is required to generate cryptographic secrets."
    exit 1
  fi

  # 2. Docker Engine server (>= 28.0 — required for reliable 127.0.0.1 loopback port isolation)
  local docker_check_msg=""
  if command -v docker >/dev/null 2>&1; then
    local docker_ver_raw docker_ver
    docker_ver_raw="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
    docker_ver="$(printf '%s' "$docker_ver_raw" | sed -nE 's/^v?([0-9]+\.[0-9]+(\.[0-9]+)?).*/\1/p' | head -n 1)"
    if [[ -n "$docker_ver" ]]; then
      if version_ge "$docker_ver" "28.0.0"; then
        ok "Docker Engine server ${docker_ver} (>= 28.0)"
      else
        docker_check_msg="Docker Engine server ${docker_ver} detected; >= 28.0 is required for reliable 127.0.0.1 loopback port isolation (older versions may allow L2 neighbors to reach loopback-published ports)."
      fi
    else
      docker_check_msg="Could not confirm Docker Engine server version via 'docker version --format {{.Server.Version}}' (>= 28.0 required; ensure the Docker daemon is running and accessible)."
    fi
  else
    docker_check_msg="Docker CLI not found in PATH — cannot confirm Docker Engine server version >= 28.0."
  fi
  if [[ -n "$docker_check_msg" ]]; then
    if [[ "$NON_INTERACTIVE" -eq 1 ]]; then
      err "$docker_check_msg"
      exit 1
    else
      warn "$docker_check_msg"
      local docker_confirm=""
      read -r -p "  Docker Engine server >= 28.0 could not be verified. Type 'yes' to continue anyway: " docker_confirm || true
      if [[ "$docker_confirm" != "yes" ]]; then
        err "Aborted: Docker Engine server >= 28.0 is required."
        exit 1
      fi
    fi
  fi

  # 3. Docker Compose (>= 2.24.4 for !reset / !override)
  if command -v docker >/dev/null 2>&1; then
    local compose_raw compose_ver
    compose_raw="$(docker compose version --short 2>/dev/null || docker compose version 2>/dev/null || true)"
    compose_ver="$(printf '%s' "$compose_raw" | sed -nE 's/.*v?([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n 1)"
    if [[ -n "$compose_ver" ]]; then
      if version_ge "$compose_ver" "2.24.4"; then
        ok "Docker Compose v${compose_ver} (>= v2.24.4)"
      else
        warn "Docker Compose v${compose_ver} detected; >= v2.24.4 is required for !reset / !override."
      fi
    else
      warn "Docker Compose v2 plugin not detected; install docker-compose-plugin >= v2.24.4."
    fi
  fi

  # 4. System memory (>= 8 GB recommended)
  local mem_gb=""
  if [[ -r /proc/meminfo ]]; then
    local mem_kb
    mem_kb="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
    if [[ -n "$mem_kb" && "$mem_kb" =~ ^[0-9]+$ ]]; then
      mem_gb="$(( (mem_kb + 524288) / 1048576 ))"
    fi
  elif command -v sysctl >/dev/null 2>&1; then
    local mem_bytes
    mem_bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    if [[ -n "$mem_bytes" && "$mem_bytes" =~ ^[0-9]+$ ]]; then
      mem_gb="$(( (mem_bytes + 536870912) / 1073741824 ))"
    fi
  fi
  if [[ -n "$mem_gb" ]]; then
    if (( mem_gb >= 8 )); then
      ok "Memory: ~${mem_gb} GiB (>= 8 GiB recommended)"
    else
      warn "Memory: ~${mem_gb} GiB detected (< 8 GiB recommended for backend + sandboxes)."
    fi
  else
    info "Memory: could not determine total RAM on this host."
  fi

  # 5. Free disk space (>= 30 GB recommended)
  local target_dir
  target_dir="$(dirname "$ENV_FILE")"
  [[ -d "$target_dir" ]] || target_dir="$REPO_ROOT"
  local free_kb free_gb
  free_kb="$(df -Pk "$target_dir" 2>/dev/null | awk 'NR==2 {print $4}' || true)"
  if [[ -n "$free_kb" && "$free_kb" =~ ^[0-9]+$ ]]; then
    free_gb="$(( free_kb / 1048576 ))"
    if (( free_gb >= 30 )); then
      ok "Free disk space: ${free_gb} GiB (>= 30 GiB recommended)"
    else
      warn "Free disk space: ${free_gb} GiB (< 30 GiB recommended for images + sandboxes)."
    fi
  else
    info "Disk space: could not determine free disk on ${target_dir}."
  fi
}

print_docker_sock_notice() {
  printf "\n== Docker Socket Security & Permission Notice ==\n"
  cat <<'EOF'
  Standalone Core mounts /var/run/docker.sock into the `backend` container
  (Docker-outside-of-Docker) so it can create and reap isolated code sandboxes
  on the dedicated per-instance sandbox network.

  - Security boundary: holding /var/run/docker.sock is equivalent to host Docker
    daemon control. By default, deploy one Standalone Core stack per dedicated
    Linux host or VM (Docker Engine >= 28.0 required for loopback port isolation).
  - Permissions: make sure the user running `docker compose` can access
    /var/run/docker.sock (e.g. `sudo usermod -aG docker "$USER"` then re-login),
    or set DOCKER_HOST_SOCKET in .env for rootless Docker.
EOF
}

get_env_value() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

write_env_key() {
  local key="$1" val="$2"
  local env_dir tmp
  env_dir="$(dirname "$ENV_FILE")"
  tmp="$(mktemp "${env_dir}/.env.tmp.XXXXXX")"
  chmod 600 "$tmp"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    awk -v k="$key" -v v="$val" '
      BEGIN { replaced = 0 }
      index($0, k "=") == 1 {
        print k "=" v
        replaced = 1
        next
      }
      { print }
      END {
        if (!replaced) print k "=" v
      }
    ' "$ENV_FILE" > "$tmp"
  else
    cat "$ENV_FILE" > "$tmp"
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

set_env_if_empty() {
  local key="$1" val="$2"
  local cur
  cur="$(get_env_value "$key")"
  if [[ -n "$cur" ]]; then
    return 1
  fi
  write_env_key "$key" "$val"
  return 0
}

gen_hex_secret() {
  openssl rand -hex 32
}

sanitize_project_name() {
  local raw="$1"
  local cleaned
  cleaned="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^[^a-z0-9]+//; s/[-_]+$//')"
  if [[ -z "$cleaned" ]]; then
    cleaned="dalton-core"
  fi
  printf '%s' "$cleaned"
}

is_loopback_domain() {
  local d
  d="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [[ "$d" == "localhost" || "$d" == "127.0.0.1" || "$d" == "::1" ]]
}

validate_domain_for_auth() {
  local domain_val="$1"
  local auth_mode=""
  # The installed stack reads DALTON_AUTH_MODE from .env, so judge by the file
  # (or the template it will be seeded from), never by this shell's environment.
  if [[ -f "$ENV_FILE" ]]; then
    auth_mode="$(get_env_value "DALTON_AUTH_MODE")"
  elif [[ -f "$ENV_EXAMPLE" ]]; then
    auth_mode="$(sed -n 's/^DALTON_AUTH_MODE=//p' "$ENV_EXAMPLE" | tail -n 1)"
  fi
  # Compose strips surrounding quotes, so compare the unquoted value.
  auth_mode="$(printf '%s' "$auth_mode" | sed -E "s/^[[:space:]]*[\"']?//; s/[\"']?[[:space:]]*$//")"
  auth_mode="${auth_mode:-oss}"
  auth_mode="$(printf '%s' "$auth_mode" | tr '[:upper:]' '[:lower:]')"
  if [[ "$auth_mode" == "oss" ]] && ! is_loopback_domain "$domain_val"; then
    err "当前核心版是免密单机试用，请先用 localhost 安装；正式对外部署等本地登录（Batch 4b）上线后再配置域名。"
    exit 1
  fi
}

main() {
  printf "\nDalton Standalone Core Initialization\n"
  if [[ -n "$DOMAIN_ARG" ]]; then
    validate_domain_for_auth "$DOMAIN_ARG"
  fi
  check_environment
  print_docker_sock_notice

  printf "\n== Configuring %s ==\n" "$ENV_FILE"
  mkdir -p "$(dirname "$ENV_FILE")"
  if [[ ! -f "$ENV_FILE" ]]; then
    local seed_tmp
    seed_tmp="$(mktemp "$(dirname "$ENV_FILE")/.env.seed.XXXXXX")"
    chmod 600 "$seed_tmp"
    if [[ -f "$ENV_EXAMPLE" ]]; then
      cat "$ENV_EXAMPLE" > "$seed_tmp"
      chmod 600 "$seed_tmp"
      mv "$seed_tmp" "$ENV_FILE"
      ok "Created ${ENV_FILE} from $(basename "$ENV_EXAMPLE")"
    else
      chmod 600 "$seed_tmp"
      mv "$seed_tmp" "$ENV_FILE"
      ok "Created empty ${ENV_FILE}"
    fi
  else
    chmod 600 "$ENV_FILE"
    info "Updating existing ${ENV_FILE} (existing non-empty values will be preserved)"
  fi

  # Resolve target domain
  local existing_domain domain
  existing_domain="$(get_env_value "DOMAIN")"
  if [[ -n "$DOMAIN_ARG" ]]; then
    domain="$DOMAIN_ARG"
    validate_domain_for_auth "$domain"
    write_env_key "DOMAIN" "$domain"
  elif [[ -n "$existing_domain" ]]; then
    domain="$existing_domain"
    validate_domain_for_auth "$domain"
  elif [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    domain="localhost"
    validate_domain_for_auth "$domain"
    write_env_key "DOMAIN" "$domain"
  else
    read -r -p "  Target domain (use 'localhost' for loopback trial) [localhost]: " domain
    domain="${domain:-localhost}"
    validate_domain_for_auth "$domain"
    write_env_key "DOMAIN" "$domain"
  fi
  ok "DOMAIN=${domain}"

  # Per-instance project & sandbox network name
  local default_project project_name
  default_project="$(sanitize_project_name "$(basename "$(cd "$(dirname "$ENV_FILE")" && pwd)")")"
  set_env_if_empty "COMPOSE_PROJECT_NAME" "$default_project" || true
  project_name="$(get_env_value "COMPOSE_PROJECT_NAME")"
  project_name="${project_name:-$default_project}"
  set_env_if_empty "SANDBOX_DOCKER_NETWORK" "${project_name}_sandbox" || true

  # Ensure Standalone Core profile & networking defaults (without clobbering existing values)
  set_env_if_empty "APP_ENV" "core" || true
  set_env_if_empty "DALTON_PROFILE" "core" || true
  set_env_if_empty "HOST_MODE" "oss" || true
  set_env_if_empty "SANDBOX_PROVIDER" "docker" || true
  set_env_if_empty "EGRESS_RELAY_BASE_URL" "http://backend:8000" || true
  set_env_if_empty "BRAND_NAME" "Dalton" || true
  set_env_if_empty "VITE_BRAND_NAME" "Dalton" || true
  set_env_if_empty "VITE_BRAND_MARK" "langalpha" || true
  set_env_if_empty "VITE_DALTON_UI" "true" || true
  set_env_if_empty "DALTON_TIMEZONE" "UTC" || true
  set_env_if_empty "DALTON_EDGE_PORT" "8088" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL" "http://minio:9000" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" "chat-uploads" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_REGION" "us-east-1" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE" "path" || true

  local edge_port
  edge_port="$(get_env_value "DALTON_EDGE_PORT")"
  edge_port="${edge_port:-8088}"

  if is_loopback_domain "$domain"; then
    set_env_if_empty "PUBLIC_APP_URL" "http://127.0.0.1:${edge_port}" || true
    set_env_if_empty "SERVER_BASE_URL" "http://127.0.0.1:${edge_port}" || true
    set_env_if_empty "DALTON_INVITE_EMAIL_DOMAIN" "team.dalton.local" || true
  else
    set_env_if_empty "PUBLIC_APP_URL" "https://${domain}" || true
    set_env_if_empty "SERVER_BASE_URL" "https://${domain}" || true
    set_env_if_empty "DALTON_INVITE_EMAIL_DOMAIN" "team.${domain}" || true
  fi

  # Generate all secrets with `openssl rand -hex 32` without overwriting existing values.
  local generated=()
  local secret_keys=(
    "DB_PASSWORD"
    "REDIS_PASSWORD"
    "MINIO_ROOT_USER"
    "MINIO_ROOT_PASSWORD"
    "DALTON_CHAT_UPLOAD_SIGNING_SECRET"
    "INTERNAL_SERVICE_TOKEN"
    "BYOK_ENCRYPTION_KEY"
    "EGRESS_RELAY_SECRET"
    "JWT_SIGNING_SECRET"
    "BOOTSTRAP_SETUP_TOKEN"
  )

  local key new_val bootstrap_generated=""
  for key in "${secret_keys[@]}"; do
    local cur
    cur="$(get_env_value "$key")"
    # Treat obvious placeholder dev values as unset so they are replaced safely.
    if [[ -z "$cur" || "$cur" == "langalpha-local-dev-encryption-key" ]]; then
      new_val="$(gen_hex_secret)"
      write_env_key "$key" "$new_val"
      generated+=("$key")
      if [[ "$key" == "BOOTSTRAP_SETUP_TOKEN" ]]; then
        bootstrap_generated="$new_val"
      fi
    fi
  done

  # Mirror MinIO root credentials into DALTON_CHAT_UPLOAD_STORAGE_* when unset.
  local minio_user minio_pass
  minio_user="$(get_env_value "MINIO_ROOT_USER")"
  minio_pass="$(get_env_value "MINIO_ROOT_PASSWORD")"
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID" "$minio_user" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY" "$minio_pass" || true

  # Enforce strict 0600 permissions after all writes (fail loudly if chmod fails).
  chmod 600 "$ENV_FILE"

  if (( ${#generated[@]} > 0 )); then
    ok "Generated secrets (${#generated[@]}): ${generated[*]}"
  else
    info "All secrets already present in ${ENV_FILE}; nothing overwritten."
  fi

  if [[ -n "$bootstrap_generated" && "$NON_INTERACTIVE" -eq 0 ]]; then
    printf "\n  First-admin bootstrap token (saved in .env as BOOTSTRAP_SETUP_TOKEN):\n"
    printf "    %s\n" "$bootstrap_generated"
  fi

  # The Docker provider starts every code sandbox from langalpha-sandbox:latest.
  # Building it here keeps the first chat from waiting 20-40 minutes for an
  # on-demand build. The core install keeps the agent browser off, so the image
  # is built without it.
  if [[ "$BUILD_SANDBOX" -eq 1 ]]; then
    if docker image inspect langalpha-sandbox:latest >/dev/null 2>&1; then
      info "Sandbox image langalpha-sandbox:latest already present; skipping build."
    else
      info "Building the code sandbox image (first build takes 20-40 minutes)..."
      if docker build -f "${REPO_ROOT}/Dockerfile.sandbox" \
          --build-arg INSTALL_AGENT_BROWSER=0 \
          -t langalpha-sandbox:latest "${REPO_ROOT}"; then
        ok "Built langalpha-sandbox:latest."
      else
        warn "Sandbox image build failed; rerun: docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest ."
      fi
    fi
  fi

  printf "\n== Next Steps ==\n"
  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    cat <<'EOF'
  1. Configure LLM provider keys, optional financial data (FMP), and optional
     web search/fetch keys via the interactive setup wizard:
       make config

  2. Optional: set SEC_USER_AGENT="Dalton <your-email@example.com>" in .env
     for SEC EDGAR compliance identification.

  3. Start the Standalone Core stack:
       docker compose -f docker-compose.yml -f docker-compose.core.yml up -d

  4. Open the web UI at http://127.0.0.1:8088 (loopback mode).
EOF
  else
    info "Non-interactive initialization complete (APP_ENV=core, DOMAIN=${domain})."
    info "Start the stack with: docker compose -f docker-compose.yml -f docker-compose.core.yml up -d"
  fi
}

main "$@"
