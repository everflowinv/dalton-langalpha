#!/usr/bin/env bash
# Standalone Core initializer (environment checks, TLS/HTTPS setup & secret generation).
#
# Responsibilities:
#   1. Check Docker Engine (>= 28.0 for reliable 127.0.0.1 loopback port isolation),
#      Docker Compose (>= 2.24.4), memory (>= 8 GiB recommended), and free disk
#      (>= 30 GiB recommended).
#   2. Print the /var/run/docker.sock security & permissions notice.
#   3. Create/update .env (strictly mode 0600) from .env.core.example, configuring
#      DOMAIN, edge TLS mode, trusted proxy settings, and COMPOSE_FILE:
#        - Until local sign-in ships (Batch 4b), only loopback addresses
#          (localhost, 127.0.0.1, ::1) are accepted.
#        - Loopback HTTP (default)  -> DALTON_TLS_MODE=loopback (127.0.0.1:8088 only)
#        - Loopback internal CA     -> DALTON_TLS_MODE=internal (127.0.0.1:18080/18443)
#        - Loopback custom cert     -> DALTON_TLS_MODE=custom   (127.0.0.1:18080/18443)
#        - Public / LAN (Batch 4b+) -> auto / internal / custom on TCP 80 & 443
#   4. Generate all required secrets with `openssl rand -hex 32` without overwriting
#      existing non-empty values and without printing any secret values to stdout/logs.
#   5. Optionally build the code sandbox image (langalpha-sandbox:latest).
#
# Usage:
#   bash scripts/standalone/init.sh
#   bash scripts/standalone/init.sh --non-interactive --domain localhost
#   bash scripts/standalone/init.sh --non-interactive --domain localhost --tls-mode internal
#   bash scripts/standalone/init.sh --domain localhost --tls-mode custom --tls-cert-dir /etc/dalton/certs
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.core.example"
DEFAULT_CORE_INTERNAL_SUBNET="172.31.250.0/24"

NON_INTERACTIVE=0
DOMAIN_ARG=""
TLS_MODE_ARG=""
TLS_CERT_DIR_ARG=""
BUILD_SANDBOX=1

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/init.sh [OPTIONS]

Options:
  --non-interactive          Do not prompt; use defaults / flags / existing .env values.
  --domain <host>            localhost, 127.0.0.1 or ::1 (the default). Public and LAN
                             domains are accepted once local sign-in ships (Batch 4b).
  --tls-mode <mode>          Override edge TLS mode: loopback | internal | auto | custom.
                             Default for loopback addresses is loopback (HTTP 127.0.0.1:8088);
                             use --tls-mode internal for loopback HTTPS (127.0.0.1:18443).
  --tls-cert-dir <path>      Directory containing cert.pem and key.pem (used with --tls-mode custom).
  --skip-sandbox-build       Leave the code sandbox image unbuilt (build it later with
                             docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest .).
  -h, --help                 Show this help message.
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
    --tls-mode)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --tls-mode requires a value (auto|internal|loopback|custom)" >&2
        exit 2
      fi
      TLS_MODE_ARG="$2"
      shift 2
      ;;
    --tls-mode=*)
      TLS_MODE_ARG="${1#--tls-mode=}"
      shift
      ;;
    --tls-cert-dir)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --tls-cert-dir requires a path" >&2
        exit 2
      fi
      TLS_CERT_DIR_ARG="$2"
      shift 2
      ;;
    --tls-cert-dir=*)
      TLS_CERT_DIR_ARG="${1#--tls-cert-dir=}"
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

validate_tls_mode() {
  local mode="$1" source_label="$2"
  case "$mode" in
    auto|internal|loopback|custom) return 0 ;;
    *)
      err "Invalid ${source_label} '${mode}'. Expected one of: auto, internal, loopback, custom."
      exit 2
      ;;
  esac
}

normalize_domain() {
  local raw="$1"
  # Trim surrounding whitespace, then strip http(s):// scheme and trailing path/query/fragment.
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  raw="$(printf '%s' "$raw" | sed -E 's#^[Hh][Tt][Tt][Pp][Ss]?://##')"
  raw="${raw%%/*}"
  raw="${raw%%\?*}"
  raw="${raw%%#*}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  printf '%s' "$raw"
}

validate_domain_syntax() {
  local raw="$1"
  if [[ -z "$raw" ]]; then
    err "DOMAIN cannot be empty."
    exit 2
  fi

  # Reject userinfo (user@host) or whitespace/quotes/control characters immediately.
  if [[ "$raw" == *"@"* || "$raw" =~ [[:space:]\'\"\\] ]]; then
    err "Invalid DOMAIN '${raw}': must be a bare hostname or IP without spaces, quotes, or credentials."
    exit 2
  fi

  # Reject bracketed IPv6 with port (e.g. [::1]:8443); accept bare bracketed IPv6 ([::1]).
  local host="$raw"
  if [[ "$host" == \[* ]]; then
    if [[ "$host" =~ ^\[([0-9a-fA-F:]+)\]:.*$ ]]; then
      err "Invalid DOMAIN '${raw}': do not include a port number (use '${BASH_REMATCH[1]}')."
      exit 2
    elif [[ "$host" =~ ^\[([0-9a-fA-F:]+)\]$ ]]; then
      host="${BASH_REMATCH[1]}"
    else
      err "Invalid DOMAIN '${raw}': malformed bracketed IPv6 literal."
      exit 2
    fi
  fi

  # Reject host:port (exactly one colon -> port attached, not an IPv6 address).
  local colons="${host//[^:]/}"
  if (( ${#colons} == 1 )); then
    err "Invalid DOMAIN '${raw}': do not include a port number (specify hostname or IP only)."
    exit 2
  fi

  # IPv6 literal (at least two colons, hex digits and colons only).
  if (( ${#colons} >= 2 )); then
    if [[ "$host" =~ ^[0-9a-fA-F:]+$ ]]; then
      printf '%s' "$host"
      return 0
    fi
    err "Invalid DOMAIN '${raw}': illegal characters in IPv6 address."
    exit 2
  fi

  # IPv4 literal: validate four decimal octets in 0..255.
  if [[ "$host" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    local o1="${BASH_REMATCH[1]}" o2="${BASH_REMATCH[2]}" o3="${BASH_REMATCH[3]}" o4="${BASH_REMATCH[4]}"
    if (( 10#$o1 <= 255 && 10#$o2 <= 255 && 10#$o3 <= 255 && 10#$o4 <= 255 )); then
      printf '%s' "$host"
      return 0
    fi
    err "Invalid DOMAIN '${raw}': IPv4 octet out of range (0-255)."
    exit 2
  fi

  # Reject trailing dot-only or empty labels and enforce RFC 1123 hostname rules.
  local trimmed="${host%.}"
  if [[ -z "$trimmed" || ${#trimmed} -gt 253 || "$trimmed" == .* || "$trimmed" == *..* ]]; then
    err "Invalid DOMAIN '${raw}': malformed hostname."
    exit 2
  fi

  local IFS=.
  local -a labels
  read -r -a labels <<< "$trimmed"
  local lbl
  for lbl in "${labels[@]}"; do
    if [[ -z "$lbl" || ${#lbl} -gt 63 ]]; then
      err "Invalid DOMAIN '${raw}': hostname label length must be 1-63 characters."
      exit 2
    fi
    if ! [[ "$lbl" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]]; then
      err "Invalid DOMAIN '${raw}': hostname contains illegal characters."
      exit 2
    fi
  done

  printf '%s' "$trimmed"
}

is_ip_address() {
  local host="$1"
  if [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    return 0
  fi
  if [[ "$host" == *:* ]]; then
    return 0
  fi
  return 1
}

classify_domain() {
  # Prints one of: loopback | private | public
  local host lower
  host="$1"
  lower="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  lower="${lower#[}"
  lower="${lower%]}"

  case "$lower" in
    localhost|127.0.0.1|::1)
      printf 'loopback'
      return 0
      ;;
  esac

  # IPv4 private / loopback / link-local / CGNAT ranges
  if [[ "$lower" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    local o1=$((10#${BASH_REMATCH[1]})) o2=$((10#${BASH_REMATCH[2]}))
    if (( o1 == 127 )); then
      printf 'loopback'
      return 0
    fi
    if (( o1 == 10 )) \
      || (( o1 == 192 && o2 == 168 )) \
      || (( o1 == 172 && o2 >= 16 && o2 <= 31 )) \
      || (( o1 == 169 && o2 == 254 )) \
      || (( o1 == 100 && o2 >= 64 && o2 <= 127 )); then
      printf 'private'
      return 0
    fi
    # Public IPv4 cannot use standard Let's Encrypt domain ACME; use internal CA unless custom.
    printf 'private'
    return 0
  fi

  # IPv6 literal
  if [[ "$lower" == *:* ]]; then
    printf 'private'
    return 0
  fi

  # Single-label hostname (no dot) or reserved local/private suffixes
  if [[ "$lower" != *.* ]]; then
    printf 'private'
    return 0
  fi
  case "$lower" in
    *.local|*.localhost|*.internal|*.lan|*.home.arpa|*.corp|*.localdomain|*.test|*.example|*.invalid)
      printf 'private'
      return 0
      ;;
  esac

  printf 'public'
}

is_loopback_domain() {
  local d
  d="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  d="${d#[}"
  d="${d%]}"
  [[ "$d" == "localhost" || "$d" == "127.0.0.1" || "$d" == "::1" ]]
}

validate_domain_for_auth() {
  local domain_val="$1"
  # Until local sign-in ships (Batch 4b), a core install has no login screen,
  # so it installs on a loopback address only. Batch 4b's init writes the
  # local auth mode itself before it accepts a public domain.
  if ! is_loopback_domain "$domain_val"; then
    err "当前核心版是免密单机试用，请先用 localhost 安装；正式对外部署等本地登录（Batch 4b）上线后再配置域名。（读到的域名：'${domain_val}'；.env 里请写成单独一行 DOMAIN=localhost，不加引号。）"
    exit 1
  fi
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

  # 4. System memory (>= 8 GiB recommended)
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

  # 5. Free disk space (>= 30 GiB recommended)
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
  awk -v k="$key" '
    {
      line = $0
      sub(/\r$/, "", line)
      sub(/^[[:space:]]+/, "", line)
      if (line == "" || substr(line, 1, 1) == "#") next
      if (line ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", line)
      }
      if (substr(line, 1, length(k)) != k) next
      rest = substr(line, length(k) + 1)
      if (rest !~ /^[[:space:]]*[=:]/) next
      sub(/^[[:space:]]*[=:][[:space:]]*/, "", rest)
      first = substr(rest, 1, 1)
      last = substr(rest, length(rest), 1)
      if (length(rest) >= 2 && first == last && (first == "\"" || first == "\047")) {
        rest = substr(rest, 2, length(rest) - 2)
      } else {
        sub(/[[:space:]]+#.*$/, "", rest)
        sub(/[[:space:]]+$/, "", rest)
      }
      val = rest
      found = 1
    }
    END {
      if (found) printf "%s", val
    }
  ' "$ENV_FILE"
}

get_file_mode() {
  local target="$1"
  if stat -c '%a' "$target" >/dev/null 2>&1; then
    stat -c '%a' "$target"
  elif stat -f '%Lp' "$target" >/dev/null 2>&1; then
    stat -f '%Lp' "$target"
  else
    printf '600'
  fi
}

write_env_key() {
  local key="$1" val="$2"
  local env_dir tmp orig_mode="600"
  env_dir="$(dirname "$ENV_FILE")"
  if [[ -f "$ENV_FILE" ]]; then
    orig_mode="$(get_file_mode "$ENV_FILE")"
  fi
  tmp="$(mktemp "${env_dir}/.env.tmp.XXXXXX")"
  chmod 600 "$tmp"
  if [[ -f "$ENV_FILE" ]]; then
    awk -v k="$key" -v v="$val" '
      BEGIN { replaced = 0 }
      {
        line = $0
        sub(/\r$/, "", line)
        trimmed = line
        sub(/^[[:space:]]+/, "", trimmed)
        if (trimmed ~ /^export[[:space:]]+/) {
          sub(/^export[[:space:]]+/, "", trimmed)
        }
        if (substr(trimmed, 1, length(k)) == k) {
          rest = substr(trimmed, length(k) + 1)
          if (rest ~ /^[[:space:]]*[=:]/) {
            if (!replaced) {
              print k "=" v
              replaced = 1
            }
            next
          }
        }
        print $0
      }
      END {
        if (!replaced) print k "=" v
      }
    ' "$ENV_FILE" > "$tmp"
  else
    printf '%s=%s\n' "$key" "$val" > "$tmp"
  fi
  chmod "$orig_mode" "$tmp"
  mv "$tmp" "$ENV_FILE"
}

remove_env_key() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  if [[ -z "$(get_env_value "$key")" ]] && ! grep -qE "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*[=:]" "$ENV_FILE" 2>/dev/null; then
    return 0
  fi
  local env_dir tmp orig_mode="600"
  env_dir="$(dirname "$ENV_FILE")"
  orig_mode="$(get_file_mode "$ENV_FILE")"
  tmp="$(mktemp "${env_dir}/.env.tmp.XXXXXX")"
  chmod 600 "$tmp"
  awk -v k="$key" '
    {
      line = $0
      sub(/\r$/, "", line)
      trimmed = line
      sub(/^[[:space:]]+/, "", trimmed)
      if (trimmed ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", trimmed)
      }
      if (substr(trimmed, 1, length(k)) == k) {
        rest = substr(trimmed, length(k) + 1)
        if (rest ~ /^[[:space:]]*[=:]/) {
          next
        }
      }
      print $0
    }
  ' "$ENV_FILE" > "$tmp"
  chmod "$orig_mode" "$tmp"
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

set_env_maybe_override() {
  local force="$1" key="$2" val="$3"
  if [[ "$force" -eq 1 ]]; then
    write_env_key "$key" "$val"
  else
    set_env_if_empty "$key" "$val" || true
  fi
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

print_tls_notice() {
  local tls_mode="$1" domain="$2" public_url="$3" cert_dir="$4"
  printf "\n== Edge HTTPS & Port Requirements ==\n"
  case "$tls_mode" in
    auto)
      cat <<EOF
  Mode: Automatic HTTPS via Caddy ACME (DALTON_TLS_MODE=auto)
  URL:  ${public_url}

  Notes:
  - For loopback addresses, ports remain strictly bound to 127.0.0.1.
  - Public ACME requires a publicly resolvable DNS domain (available once local
    sign-in ships in Batch 4b) with inbound TCP ports 80 and 443 open.
  - Certificates are persisted in the Compose volume \`dalton-caddy-data\`
    (locate on host with \`docker volume ls --filter name=dalton-caddy-data\`).
EOF
      ;;
    internal)
      cat <<EOF
  Mode: Internal CA HTTPS via Caddy built-in PKI (DALTON_TLS_MODE=internal)
  URL:  ${public_url}

  Notes:
  - Caddy issues a locally-signed certificate stored in \`dalton-caddy-data\`
    (locate on host with \`docker volume ls --filter name=dalton-caddy-data\`).
  - For localhost, ports remain strictly bound to 127.0.0.1 (18080 -> HTTP, 18443 -> HTTPS).
  - Export the root CA certificate after startup so client devices trust HTTPS:
      docker compose exec -T edge \\
        cat /data/caddy/pki/authorities/local/root.crt > dalton-local-ca.crt
EOF
      ;;
    custom)
      cat <<EOF
  Mode: Custom TLS certificate (DALTON_TLS_MODE=custom)
  URL:  ${public_url}
  Certs: ${cert_dir}

  Notes:
  - Place your full certificate chain at ${cert_dir}/cert.pem and private key at
    ${cert_dir}/key.pem (mode 0600) before starting the stack.
  - Back up ${cert_dir} separately; backup.sh archives Caddy /data only.
EOF
      ;;
    loopback)
      cat <<EOF
  Mode: Loopback HTTP trial (DALTON_TLS_MODE=loopback)
  URL:  ${public_url}

  Notes:
  - Published exclusively on 127.0.0.1 (port 8088 only; requires Docker Engine >= 28.0).
  - Ports 80 and 443 are not published in loopback mode.
  - Enable loopback internal-CA HTTPS anytime with:
      bash scripts/standalone/init.sh --domain localhost --tls-mode internal
EOF
      ;;
  esac
}

main() {
  if [[ -n "$TLS_MODE_ARG" ]]; then
    validate_tls_mode "$TLS_MODE_ARG" "--tls-mode"
  fi

  printf "\nDalton Standalone Core Initialization\n"
  if [[ -n "$DOMAIN_ARG" ]]; then
    local pre_domain
    pre_domain="$(normalize_domain "$DOMAIN_ARG")"
    pre_domain="$(validate_domain_syntax "$pre_domain")"
    validate_domain_for_auth "$pre_domain"
    DOMAIN_ARG="$pre_domain"
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
    info "Updating existing ${ENV_FILE} (existing permissions and secrets will be preserved)"
  fi

  # Resolve target domain
  local existing_domain domain domain_changed=0
  existing_domain="$(get_env_value "DOMAIN")"
  if [[ -n "$DOMAIN_ARG" ]]; then
    domain="$DOMAIN_ARG"
    write_env_key "DOMAIN" "$domain"
    domain_changed=1
  elif [[ -n "$existing_domain" ]]; then
    domain="$(normalize_domain "$existing_domain")"
    domain="$(validate_domain_syntax "$domain")"
    validate_domain_for_auth "$domain"
    if [[ "$domain" != "$existing_domain" ]]; then
      write_env_key "DOMAIN" "$domain"
    fi
  elif [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    domain="localhost"
    validate_domain_for_auth "$domain"
    write_env_key "DOMAIN" "$domain"
    domain_changed=1
  else
    read -r -p "  Target domain (use 'localhost' for loopback trial) [localhost]: " domain
    domain="$(normalize_domain "${domain:-localhost}")"
    domain="$(validate_domain_syntax "$domain")"
    validate_domain_for_auth "$domain"
    write_env_key "DOMAIN" "$domain"
    domain_changed=1
  fi
  ok "DOMAIN=${domain}"

  # Classify domain and resolve TLS mode (validate any pre-existing DALTON_TLS_MODE in .env too)
  local domain_class existing_tls_mode tls_mode tls_changed=0
  domain_class="$(classify_domain "$domain")"
  existing_tls_mode="$(get_env_value "DALTON_TLS_MODE")"
  if [[ -n "$existing_tls_mode" ]]; then
    validate_tls_mode "$existing_tls_mode" "DALTON_TLS_MODE in .env"
  fi

  if [[ -n "$TLS_MODE_ARG" ]]; then
    tls_mode="$TLS_MODE_ARG"
    tls_changed=1
  elif [[ "$domain_changed" -eq 1 || -z "$existing_tls_mode" ]]; then
    case "$domain_class" in
      public)   tls_mode="auto" ;;
      private)  tls_mode="internal" ;;
      loopback) tls_mode="loopback" ;;
    esac
    tls_changed=1
  else
    tls_mode="$existing_tls_mode"
  fi

  local url_host loopback_bind
  if [[ "$domain" == *:* ]]; then
    url_host="[${domain}]"
    loopback_bind="[${domain}]"
  else
    url_host="$domain"
    loopback_bind="127.0.0.1"
  fi

  if [[ "$tls_mode" == "auto" && "$domain_class" != "public" ]]; then
    warn "DALTON_TLS_MODE=auto selected for loopback/non-public host '${domain}'; binding enforced to ${loopback_bind}."
  fi

  local force_network_update=0
  if [[ "$domain_changed" -eq 1 || "$tls_changed" -eq 1 ]]; then
    force_network_update=1
  fi

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
  # Single source of truth: docker-compose.core.yml derives DALTON_TRUSTED_PROXIES
  # directly from DALTON_CORE_INTERNAL_SUBNET so the two never drift.
  set_env_if_empty "DALTON_CORE_INTERNAL_SUBNET" "$DEFAULT_CORE_INTERNAL_SUBNET" || true
  remove_env_key "DALTON_TRUSTED_PROXIES"
  set_env_if_empty "DALTON_TRUSTED_PROXY_HOPS" "1" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL" "http://minio:9000" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" "chat-uploads" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_REGION" "us-east-1" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE" "path" || true

  local edge_port tls_bind http_port https_port public_url invite_domain cert_dir compose_file tls_overlay_file
  edge_port="$(get_env_value "DALTON_EDGE_PORT")"
  edge_port="${edge_port:-8088}"
  if [[ "$domain" == *:* ]]; then
    tls_overlay_file="deploy/standalone/docker-compose.tls.ipv6.yml"
  else
    tls_overlay_file="deploy/standalone/docker-compose.tls.yml"
  fi

  case "$tls_mode" in
    loopback)
      tls_bind="$loopback_bind"
      http_port="18080"
      https_port="18443"
      public_url="http://127.0.0.1:${edge_port}"
      compose_file="docker-compose.yml:docker-compose.core.yml"
      ;;
    internal|auto|custom)
      # Before Batch 4b ships local sign-in, all published addresses stay hardcoded
      # to 127.0.0.1 (or [::1] via docker-compose.tls.ipv6.yml) in the Compose files.
      tls_bind="$loopback_bind"
      http_port="18080"
      https_port="18443"
      public_url="https://${url_host}:${https_port}"
      compose_file="docker-compose.yml:docker-compose.core.yml:${tls_overlay_file}"
      ;;
  esac

  if [[ "$domain_class" == "loopback" ]] || is_ip_address "$domain"; then
    invite_domain="team.dalton.local"
  else
    invite_domain="team.${domain}"
  fi

  set_env_maybe_override "$force_network_update" "DALTON_TLS_MODE" "$tls_mode"
  # Before Batch 4b ships local sign-in, always rewrite the listen/bind address in .env
  # to the loopback address (127.0.0.1 or [::1]) on every init.sh run regardless of any
  # pre-existing value (e.g. 0.0.0.0) so no passwordless stack can publish externally.
  local existing_tls_bind
  existing_tls_bind="$(get_env_value "DALTON_EDGE_TLS_BIND")"
  write_env_key "DALTON_EDGE_TLS_BIND" "$tls_bind"
  if [[ -n "$existing_tls_bind" && "$existing_tls_bind" != "$tls_bind" ]]; then
    warn "Rewrote DALTON_EDGE_TLS_BIND from '${existing_tls_bind}' to '${tls_bind}' (before Batch 4b, only loopback listening addresses are permitted)."
  else
    info "Enforced loopback listen address DALTON_EDGE_TLS_BIND=${tls_bind} (before Batch 4b, only loopback addresses are published)."
  fi
  if [[ "$domain" == *:* ]]; then
    write_env_key "DALTON_CADDY_HOST" "$url_host"
  else
    remove_env_key "DALTON_CADDY_HOST"
  fi
  set_env_maybe_override "$force_network_update" "DALTON_EDGE_HTTP_PORT" "$http_port"
  set_env_maybe_override "$force_network_update" "DALTON_EDGE_HTTPS_PORT" "$https_port"
  set_env_maybe_override "$force_network_update" "COMPOSE_FILE" "$compose_file"
  set_env_maybe_override "$force_network_update" "PUBLIC_APP_URL" "$public_url"
  set_env_maybe_override "$force_network_update" "SERVER_BASE_URL" "$public_url"
  set_env_maybe_override "$force_network_update" "DALTON_INVITE_EMAIL_DOMAIN" "$invite_domain"

  if [[ -n "$TLS_CERT_DIR_ARG" ]]; then
    write_env_key "DALTON_TLS_CERT_DIR" "$TLS_CERT_DIR_ARG"
  else
    set_env_if_empty "DALTON_TLS_CERT_DIR" "./deploy/standalone/certs" || true
  fi
  cert_dir="$(get_env_value "DALTON_TLS_CERT_DIR")"
  cert_dir="${cert_dir:-./deploy/standalone/certs}"

  local actual_tls_mode actual_tls_bind actual_http_port actual_https_port actual_public_url
  actual_tls_mode="$(get_env_value "DALTON_TLS_MODE")"
  actual_tls_bind="$(get_env_value "DALTON_EDGE_TLS_BIND")"
  actual_http_port="$(get_env_value "DALTON_EDGE_HTTP_PORT")"
  actual_https_port="$(get_env_value "DALTON_EDGE_HTTPS_PORT")"
  actual_public_url="$(get_env_value "PUBLIC_APP_URL")"
  ok "DALTON_TLS_MODE=${actual_tls_mode:-$tls_mode} (bind=${actual_tls_bind:-$tls_bind}, http=${actual_http_port:-$http_port}, https=${actual_https_port:-$https_port}, url=${actual_public_url:-$public_url})"

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

  local key new_val
  for key in "${secret_keys[@]}"; do
    local cur
    cur="$(get_env_value "$key")"
    # Treat obvious placeholder dev values as unset so they are replaced safely.
    if [[ -z "$cur" || "$cur" == "langalpha-local-dev-encryption-key" ]]; then
      new_val="$(gen_hex_secret)"
      write_env_key "$key" "$new_val"
      generated+=("$key")
    fi
  done

  # Mirror MinIO root credentials into DALTON_CHAT_UPLOAD_STORAGE_* when unset.
  local minio_user minio_pass
  minio_user="$(get_env_value "MINIO_ROOT_USER")"
  minio_pass="$(get_env_value "MINIO_ROOT_PASSWORD")"
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID" "$minio_user" || true
  set_env_if_empty "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY" "$minio_pass" || true

  if (( ${#generated[@]} > 0 )); then
    ok "Generated secrets (${#generated[@]}): ${generated[*]} (values stored in ${ENV_FILE}, mode $(get_file_mode "$ENV_FILE"))"
  else
    info "All secrets already present in ${ENV_FILE}; nothing overwritten."
  fi

  print_tls_notice "$tls_mode" "$domain" "$public_url" "$cert_dir"

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

  local compose_start_cmd="docker compose -f docker-compose.yml -f docker-compose.core.yml up -d"
  if [[ "$tls_mode" != "loopback" ]]; then
    compose_start_cmd="docker compose -f docker-compose.yml -f docker-compose.core.yml -f ${tls_overlay_file} up -d"
  fi

  printf "\n== Next Steps ==\n"
  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    cat <<EOF
  1. Configure LLM provider keys, optional financial data (FMP), and optional
     web search/fetch keys via the interactive setup wizard:
       make config

  2. Optional: set SEC_USER_AGENT="Dalton <your-email@example.com>" in .env
     for SEC EDGAR compliance identification.

  3. Start the Standalone Core stack:
       ${compose_start_cmd}

  4. Open the web UI at ${public_url}
EOF
  else
    info "Non-interactive initialization complete (APP_ENV=core, DOMAIN=${domain}, DALTON_TLS_MODE=${tls_mode})."
    info "Start the stack with: ${compose_start_cmd}"
  fi
}

main "$@"
