#!/usr/bin/env bash
# Standalone Core initializer (environment checks, TLS/HTTPS setup & secret generation).
#
# Responsibilities:
#   1. Check Docker Engine (>= 28.0 for reliable 127.0.0.1 loopback port isolation),
#      Docker Compose (>= 2.24.4), memory (>= 8 GiB recommended), and free disk
#      (>= 30 GiB recommended).
#   2. Print the /var/run/docker.sock security & permissions notice.
#   3. Create/update .env (strictly mode 0600) from .env.core.example, configuring
#      DOMAIN, authentication mode, edge TLS mode, trusted proxy settings, and
#      COMPOSE_FILE:
#        - Loopback HTTP (default)  -> DALTON_AUTH_MODE=oss, DALTON_TLS_MODE=loopback (127.0.0.1:8088 only)
#        - Loopback internal CA     -> DALTON_AUTH_MODE=oss, DALTON_TLS_MODE=internal (127.0.0.1:18080/18443)
#        - Loopback custom cert     -> DALTON_AUTH_MODE=oss, DALTON_TLS_MODE=custom   (127.0.0.1:18080/18443)
#        - Public / LAN domain      -> DALTON_AUTH_MODE=local & VITE_DALTON_AUTH=local,
#                                      auto / internal / custom TLS on TCP 80 & 443
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=scripts/standalone/env_tokens.sh
source "${SCRIPT_DIR}/env_tokens.sh"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.core.example"
DEFAULT_CORE_INTERNAL_SUBNET="172.31.250.0/24"

NON_INTERACTIVE=0
DOMAIN_ARG=""
TLS_MODE_ARG=""
TLS_CERT_DIR_ARG=""
BIND_ARG=""
BUILD_SANDBOX=1
SCRIPT_WROTE_LOCAL_AUTH=0

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/init.sh [OPTIONS]

Options:
  --non-interactive          Do not prompt; use defaults / flags / existing .env values.
  --domain <host>            localhost, 127.0.0.1 or ::1 (loopback passwordless trial, default),
                             or a public/LAN domain (automatically enables DALTON_AUTH_MODE=local
                             and VITE_DALTON_AUTH=local).
  --tls-mode <mode>          Override edge TLS mode: loopback | internal | auto | custom.
                             Default is loopback (HTTP 127.0.0.1:8088) for loopback hosts,
                             auto (ACME HTTPS) for public domains, internal for private/LAN hosts;
                             use --tls-mode internal for loopback HTTPS (127.0.0.1:18443).
  --tls-cert-dir <path>      Directory containing cert.pem and key.pem (used with --tls-mode custom).
  --bind <address>           Override DALTON_EDGE_TLS_BIND (for example '[::]' for public domains
                             with AAAA records only; default is derived from --domain).
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
    --bind)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --bind requires an address (e.g. 0.0.0.0 or '[::]')" >&2
        exit 2
      fi
      BIND_ARG="$2"
      shift 2
      ;;
    --bind=*)
      BIND_ARG="${1#--bind=}"
      if [[ -z "$BIND_ARG" ]]; then
        echo "ERROR: --bind requires an address (e.g. 0.0.0.0 or '[::]')" >&2
        exit 2
      fi
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

detect_host_platform() {
  local raw_os
  raw_os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  case "$raw_os" in
    darwin) printf 'macos' ;;
    linux)  printf 'linux' ;;
    *)
      warn "Unrecognized host OS '${raw_os:-unknown}' from uname -s; defaulting DALTON_HOST_PLATFORM=linux."
      printf 'linux'
      ;;
  esac
}

validate_host_platform() {
  local raw="$1" source_label="$2"
  local lowered
  lowered="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    macos|linux)
      printf '%s' "$lowered"
      return 0
      ;;
    *)
      err "Invalid ${source_label} '${raw}'. Expected one of: macos, linux."
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

  # Reject userinfo (user@host) or whitespace/quotes/shell metacharacters immediately.
  if [[ "$raw" == *"@"* || "$raw" =~ [[:space:]\'\"\$\\] ]]; then
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

validate_bind_address() {
  local raw="$1"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  if [[ -z "$raw" ]]; then
    err "Invalid --bind: address cannot be empty."
    exit 2
  fi
  if [[ "$raw" =~ [[:space:]\'\"\$\\@] ]]; then
    err "Invalid --bind '${raw}': expected an IPv4 or IPv6 address without spaces or quotes."
    exit 2
  fi
  local inner="$raw"
  local bracketed=0
  if [[ "$raw" == \[* ]]; then
    if [[ "$raw" =~ ^\[([0-9a-fA-F:]+)\]$ ]]; then
      inner="${BASH_REMATCH[1]}"
      bracketed=1
    else
      err "Invalid --bind '${raw}': malformed bracketed IPv6 address (omit any port number)."
      exit 2
    fi
  fi
  if [[ "$bracketed" -eq 0 && "$inner" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    local o1="${BASH_REMATCH[1]}" o2="${BASH_REMATCH[2]}" o3="${BASH_REMATCH[3]}" o4="${BASH_REMATCH[4]}"
    if [[ "$o1" =~ ^(0|[1-9][0-9]*)$ && "$o2" =~ ^(0|[1-9][0-9]*)$ && "$o3" =~ ^(0|[1-9][0-9]*)$ && "$o4" =~ ^(0|[1-9][0-9]*)$ ]] \
      && (( o1 <= 255 && o2 <= 255 && o3 <= 255 && o4 <= 255 )); then
      printf '%s' "$inner"
      return 0
    fi
    err "Invalid --bind '${raw}': IPv4 octet out of range (0-255)."
    exit 2
  fi
  if awk -v s="$inner" '
    BEGIN {
      low = tolower(s)
      if (index(low, ":") == 0) exit 1
      if (low !~ /^[0-9a-f:]+$/) exit 1
      if (low ~ /:::/) exit 1
      if (substr(low, 1, 1) == ":" && substr(low, 1, 2) != "::") exit 1
      if (substr(low, length(low), 1) == ":" && substr(low, length(low) - 1, 2) != "::") exit 1
      pos = index(low, "::")
      if (pos > 0) {
        left = substr(low, 1, pos - 1)
        right = substr(low, pos + 2)
        if (index(right, "::") > 0) exit 1
        nl = (left == "" ? 0 : split(left, L, ":"))
        nr = (right == "" ? 0 : split(right, R, ":"))
        if (nl + nr >= 8) exit 1
        for (i = 1; i <= nl; i++) if (length(L[i]) < 1 || length(L[i]) > 4) exit 1
        for (i = 1; i <= nr; i++) if (length(R[i]) < 1 || length(R[i]) > 4) exit 1
      } else {
        ng = split(low, G, ":")
        if (ng != 8) exit 1
        for (i = 1; i <= 8; i++) if (length(G[i]) < 1 || length(G[i]) > 4) exit 1
      }
      exit 0
    }
  '; then
    printf '[%s]' "$inner"
    return 0
  fi
  err "Invalid --bind '${raw}': expected an IPv4 address (e.g. 0.0.0.0, 127.0.0.1) or IPv6 address (e.g. '[::]', '[::1]')."
  exit 2
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
  if is_loopback_domain "$host"; then
    printf 'loopback'
    return 0
  fi
  lower="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]')"
  lower="${lower#[}"
  lower="${lower%]}"

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
  awk -v raw="$1" '
    BEGIN {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", raw)
      if (raw == "") exit 1
      low = tolower(raw)
      bracketed = 0
      if (substr(low, 1, 1) == "[" && substr(low, length(low), 1) == "]") {
        bracketed = 1
        low = substr(low, 2, length(low) - 2)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", low)
      }
      if (!bracketed) {
        if (low == "localhost") exit 0
        if (low ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) {
          n = split(low, oct, ".")
          if (n == 4 && oct[1] ~ /^(0|[1-9][0-9]*)$/ && oct[1] + 0 == 127) {
            ok = 1
            for (i = 2; i <= 4; i++) {
              if (oct[i] !~ /^(0|[1-9][0-9]*)$/ || oct[i] + 0 > 255) ok = 0
            }
            if (ok) exit 0
          }
          exit 1
        }
      }
      if (index(low, ":") == 0) exit 1
      if (low !~ /^[0-9a-f:]+$/) exit 1
      if (low ~ /:::/) exit 1
      if (substr(low, 1, 1) == ":" && substr(low, 1, 2) != "::") exit 1
      if (substr(low, length(low), 1) == ":" && substr(low, length(low) - 1, 2) != "::") exit 1
      pos = index(low, "::")
      if (pos > 0) {
        left = substr(low, 1, pos - 1)
        right = substr(low, pos + 2)
        if (index(right, "::") > 0) exit 1
        nl = (left == "" ? 0 : split(left, L, ":"))
        nr = (right == "" ? 0 : split(right, R, ":"))
        if (nl + nr >= 8) exit 1
        ng = 0
        for (i = 1; i <= nl; i++) G[++ng] = L[i]
        for (i = 1; i <= 8 - nl - nr; i++) G[++ng] = "0"
        for (i = 1; i <= nr; i++) G[++ng] = R[i]
      } else {
        ng = split(low, G, ":")
        if (ng != 8) exit 1
      }
      for (i = 1; i <= 8; i++) {
        g = G[i]
        if (length(g) < 1 || length(g) > 4 || g !~ /^[0-9a-f]+$/) exit 1
        sub(/^0+/, "", g)
        if (i < 8 && g != "") exit 1
        if (i == 8 && g != "1") exit 1
      }
      exit 0
    }
  '
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

get_env_value_any() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  # Parse `.env` following Docker Compose env-file syntax:
  # - ignores blank lines and `#` comments, strips CR (`\r`), supports `export`
  # - accepts both `KEY=value` and `KEY: value` assignments
  # - preserves `#` inside single/double quotes and strips unquoted inline `#` comments
  awk -v k="$key" '
    function parse_val(s,    q, out, i, n, ch, nxt) {
      sub(/^[[:space:]]+/, "", s)
      if (s == "") return ""
      q = substr(s, 1, 1)
      if (q == "\"") {
        out = ""
        n = length(s)
        for (i = 2; i <= n; i++) {
          ch = substr(s, i, 1)
          if (ch == "\\" && i < n) {
            nxt = substr(s, i + 1, 1)
            out = out nxt
            i++
          } else if (ch == "\"") {
            return out
          } else {
            out = out ch
          }
        }
        return out
      }
      if (q == "\047") {
        n = index(substr(s, 2), "\047")
        if (n > 0) {
          return substr(s, 2, n - 1)
        }
        return substr(s, 2)
      }
      if (substr(s, 1, 1) == "#") return ""
      sub(/[[:space:]]+#.*$/, "", s)
      sub(/[[:space:]]+$/, "", s)
      return s
    }
    {
      line = $0
      sub(/\r$/, "", line)
      sub(/^[[:space:]]+/, "", line)
      if (line == "" || substr(line, 1, 1) == "#") next
      if (line ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", line)
      }
      if (substr(line, 1, length(k)) == k) {
        rest = substr(line, length(k) + 1)
        if (rest ~ /^[[:space:]]*[=:]/) {
          sub(/^[[:space:]]*[=:]/, "", rest)
          val = parse_val(rest)
        }
      }
    }
    END {
      if (val != "") print val
    }
  ' "$ENV_FILE" 2>/dev/null || true
}

get_env_value() {
  if declare -F get_env_value_any >/dev/null 2>&1; then
    get_env_value_any "$1"
    return $?
  fi
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  printf '%s' "${line#*=}"
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

count_env_key_assignments() {
  local key="$1"
  if [[ ! -f "$ENV_FILE" ]]; then
    printf '0'
    return 0
  fi
  awk -v k="$key" '
    BEGIN { count = 0 }
    {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      if (line ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", line)
      }
      if (substr(line, 1, length(k)) == k) {
        rest = substr(line, length(k) + 1)
        if (rest ~ /^[[:space:]]*[=:]/) {
          count++
        }
      }
    }
    END { print count }
  ' "$ENV_FILE"
}

remove_env_key_all() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  local env_dir tmp
  env_dir="$(dirname "$ENV_FILE")"
  tmp="$(mktemp "${env_dir}/.env.tmp.XXXXXX")"
  chmod 600 "$tmp"
  awk -v k="$key" '
    {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      if (line ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", line)
      }
      if (substr(line, 1, length(k)) == k) {
        rest = substr(line, length(k) + 1)
        if (rest ~ /^[[:space:]]*[=:]/) {
          next
        }
      }
      print $0
    }
  ' "$ENV_FILE" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

append_env_key_unique() {
  local key="$1" val="$2"
  remove_env_key_all "$key"
  local env_dir tmp
  env_dir="$(dirname "$ENV_FILE")"
  tmp="$(mktemp "${env_dir}/.env.tmp.XXXXXX")"
  chmod 600 "$tmp"
  if [[ -f "$ENV_FILE" ]]; then
    cat "$ENV_FILE" > "$tmp"
  fi
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

configure_public_domain_local_auth() {
  local domain_val="$1"
  remove_env_key_all "DALTON_AUTH_MODE"
  remove_env_key_all "DALTON_ALLOW_DEV_OSS_AUTH"
  remove_env_key_all "DOMAIN"
  remove_env_key_all "VITE_DALTON_AUTH"
  append_env_key_unique "DALTON_AUTH_MODE" "local"
  append_env_key_unique "DOMAIN" "$domain_val"
  append_env_key_unique "VITE_DALTON_AUTH" "local"
  SCRIPT_WROTE_LOCAL_AUTH=1
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
  - Public ACME requires a publicly resolvable DNS domain with inbound TCP
    ports 80 and 443 open.
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

validate_domain_for_auth() {
  local domain_val="$1"
  if is_loopback_domain "$domain_val"; then
    return 0
  fi
  if [[ "$SCRIPT_WROTE_LOCAL_AUTH" -ne 1 ]]; then
    err "公网域名 '${domain_val}' 仅在脚本本次写入 DALTON_AUTH_MODE=local 后才放行，拒绝在免密模式或未规范化的 .env 下继续。"
    exit 1
  fi
  local auth_count allow_dev_count auth_val
  auth_count="$(count_env_key_assignments "DALTON_AUTH_MODE")"
  allow_dev_count="$(count_env_key_assignments "DALTON_ALLOW_DEV_OSS_AUTH")"
  auth_val="$(get_env_value "DALTON_AUTH_MODE")"
  if [[ "$auth_count" -ne 1 || "$auth_val" != "local" || "$allow_dev_count" -ne 0 ]]; then
    err "公网域名 '${domain_val}' 校验失败：.env 必须仅包含唯一的 DALTON_AUTH_MODE=local 且不得包含 DALTON_ALLOW_DEV_OSS_AUTH。"
    exit 1
  fi
}

write_edge_tls_bind() {
  local domain_class="$1" tls_mode="$2" loopback_bind="$3" desired_bind="$4" force_update="$5"
  local actual_auth_mode existing_tls_bind
  actual_auth_mode="$(get_env_value "DALTON_AUTH_MODE")"
  existing_tls_bind="$(get_env_value "DALTON_EDGE_TLS_BIND")"
  if [[ "$domain_class" == "loopback" || "$tls_mode" == "loopback" || "$actual_auth_mode" != "local" || "$SCRIPT_WROTE_LOCAL_AUTH" -ne 1 ]]; then
    # Loopback hosts, loopback TLS mode, or any non-local auth configuration
    # always enforce a loopback listen address (127.0.0.1 or [::1]) in .env.
    write_env_key "DALTON_EDGE_TLS_BIND" "$loopback_bind"
    if [[ -n "$existing_tls_bind" && "$existing_tls_bind" != "$loopback_bind" ]]; then
      warn "Rewrote DALTON_EDGE_TLS_BIND from '${existing_tls_bind}' to '${loopback_bind}' (loopback host or non-local auth requires a loopback listening address)."
    else
      info "Enforced loopback listen address DALTON_EDGE_TLS_BIND=${loopback_bind}."
    fi
    return 0
  fi
  if [[ "$force_update" -eq 1 || -z "$existing_tls_bind" || "$existing_tls_bind" == "127.0.0.1" || "$existing_tls_bind" == "[::1]" ]]; then
    write_env_key "DALTON_EDGE_TLS_BIND" "$desired_bind"
  fi
}

main() {
  if [[ -n "$TLS_MODE_ARG" ]]; then
    validate_tls_mode "$TLS_MODE_ARG" "--tls-mode"
  fi
  if [[ -n "$BIND_ARG" ]]; then
    BIND_ARG="$(validate_bind_address "$BIND_ARG")"
  fi

  printf "\nDalton Standalone Core Initialization\n"
  if [[ -n "$DOMAIN_ARG" ]]; then
    local pre_domain
    pre_domain="$(normalize_domain "$DOMAIN_ARG")"
    pre_domain="$(validate_domain_syntax "$pre_domain")"
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
  existing_domain="$(get_env_value_any "DOMAIN")"
  if [[ -n "$DOMAIN_ARG" ]]; then
    domain="$DOMAIN_ARG"
    domain_changed=1
  elif [[ -n "$existing_domain" ]]; then
    domain="$(normalize_domain "$existing_domain")"
    domain="$(validate_domain_syntax "$domain")"
  elif [[ "$NON_INTERACTIVE" -eq 1 ]]; then
    domain="localhost"
    domain_changed=1
  else
    read -r -p "  Target domain (use 'localhost' for loopback trial, or a public domain for local auth) [localhost]: " domain
    domain="$(normalize_domain "${domain:-localhost}")"
    domain="$(validate_domain_syntax "$domain")"
    domain_changed=1
  fi

  if is_loopback_domain "$domain"; then
    write_env_key "DOMAIN" "$domain"
    set_env_if_empty "DALTON_AUTH_MODE" "oss" || true
    set_env_if_empty "DALTON_ALLOW_DEV_OSS_AUTH" "1" || true
    validate_domain_for_auth "$domain"
  else
    configure_public_domain_local_auth "$domain"
    validate_domain_for_auth "$domain"
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
  elif [[ "$domain_changed" -eq 1 || -z "$existing_tls_mode" || ( "$domain_class" != "loopback" && "$existing_tls_mode" == "loopback" ) ]]; then
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
    loopback_bind="[::1]"
  else
    url_host="$domain"
    loopback_bind="127.0.0.1"
  fi

  if [[ "$tls_mode" == "auto" && "$domain_class" == "loopback" ]]; then
    warn "DALTON_TLS_MODE=auto selected for loopback host '${domain}'; binding enforced to ${loopback_bind}."
  elif [[ "$tls_mode" == "auto" && "$domain_class" != "public" ]]; then
    warn "DALTON_TLS_MODE=auto selected for non-public host '${domain}'; public ACME issuance requires a publicly resolvable DNS domain (use --tls-mode internal or custom for LAN hosts)."
  fi

  local force_network_update=0
  if [[ "$domain_changed" -eq 1 || "$tls_changed" -eq 1 || -n "$BIND_ARG" ]]; then
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
  local existing_host_platform detected_host_platform
  existing_host_platform="$(get_env_value "DALTON_HOST_PLATFORM")"
  if [[ -n "$existing_host_platform" ]]; then
    existing_host_platform="$(validate_host_platform "$existing_host_platform" "DALTON_HOST_PLATFORM in .env")"
    write_env_key "DALTON_HOST_PLATFORM" "$existing_host_platform"
  else
    detected_host_platform="$(detect_host_platform)"
    write_env_key "DALTON_HOST_PLATFORM" "$detected_host_platform"
  fi
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
  set_env_if_empty "DALTON_CAP_COMPANY_WIKI" "true" || true
  set_env_if_empty "DALTON_RESEARCH_HOST_URL" "http://research-host:8951" || true

  local edge_port tls_bind http_port https_port public_url invite_domain cert_dir compose_file tls_overlay_file
  edge_port="$(get_env_value "DALTON_EDGE_PORT")"
  edge_port="${edge_port:-8088}"

  local auth_mode_now
  auth_mode_now="$(get_env_value "DALTON_AUTH_MODE")"

  if [[ -n "$BIND_ARG" ]]; then
    if [[ "$domain_class" == "loopback" || "$tls_mode" == "loopback" || "$auth_mode_now" != "local" || "$SCRIPT_WROTE_LOCAL_AUTH" -ne 1 ]]; then
      if ! is_loopback_domain "$BIND_ARG"; then
        err "Non-loopback --bind '${BIND_ARG}' requires a non-loopback domain with DALTON_AUTH_MODE=local and HTTPS enabled."
        exit 2
      fi
      loopback_bind="$BIND_ARG"
    fi
  fi

  case "$tls_mode" in
    loopback)
      tls_bind="${BIND_ARG:-$loopback_bind}"
      http_port="18080"
      https_port="18443"
      public_url="http://127.0.0.1:${edge_port}"
      ;;
    internal|auto|custom)
      if [[ "$domain_class" == "loopback" || "$auth_mode_now" != "local" || "$SCRIPT_WROTE_LOCAL_AUTH" -ne 1 ]]; then
        tls_bind="${BIND_ARG:-$loopback_bind}"
        http_port="18080"
        https_port="18443"
        public_url="https://${url_host}:${https_port}"
      else
        if [[ -n "$BIND_ARG" ]]; then
          tls_bind="$BIND_ARG"
        elif [[ "$domain" == *:* ]]; then
          tls_bind="[::]"
        else
          tls_bind="0.0.0.0"
        fi
        http_port="80"
        https_port="443"
        public_url="https://${url_host}"
      fi
      ;;
  esac

  local effective_bind="$tls_bind"
  if [[ "$force_network_update" -eq 0 ]]; then
    local cur_bind
    cur_bind="$(get_env_value "DALTON_EDGE_TLS_BIND")"
    if [[ -n "$cur_bind" ]]; then
      effective_bind="$cur_bind"
    fi
  fi
  if [[ "$domain" == *:* || "$effective_bind" == *:* ]]; then
    tls_overlay_file="deploy/standalone/docker-compose.tls.ipv6.yml"
  else
    tls_overlay_file="deploy/standalone/docker-compose.tls.yml"
  fi
  if [[ "$tls_mode" == "loopback" ]]; then
    compose_file="docker-compose.yml:docker-compose.core.yml"
  else
    compose_file="docker-compose.yml:docker-compose.core.yml:${tls_overlay_file}"
  fi

  if [[ "$domain_class" == "loopback" ]] || is_ip_address "$domain"; then
    invite_domain="team.dalton.local"
  else
    invite_domain="team.${domain}"
  fi

  set_env_maybe_override "$force_network_update" "DALTON_TLS_MODE" "$tls_mode"
  write_edge_tls_bind "$domain_class" "$tls_mode" "$loopback_bind" "$tls_bind" "$force_network_update"
  if [[ "$domain" == *:* ]]; then
    write_env_key "DALTON_CADDY_HOST" "$url_host"
  else
    remove_env_key "DALTON_CADDY_HOST"
  fi
  set_env_maybe_override "$force_network_update" "DALTON_EDGE_HTTP_PORT" "$http_port"
  set_env_maybe_override "$force_network_update" "DALTON_EDGE_HTTPS_PORT" "$https_port"
  set_env_maybe_override "$force_network_update" "COMPOSE_FILE" "$compose_file"
  if [[ "$force_network_update" -eq 1 ]]; then
    write_env_key "PUBLIC_APP_URL" "$public_url"
    write_env_key "SERVER_BASE_URL" "$public_url"
    write_env_key "DALTON_INVITE_EMAIL_DOMAIN" "$invite_domain"
  elif ! is_loopback_domain "$domain"; then
    local cur_pub cur_srv cur_inv
    cur_pub="$(get_env_value "PUBLIC_APP_URL")"
    cur_srv="$(get_env_value "SERVER_BASE_URL")"
    cur_inv="$(get_env_value "DALTON_INVITE_EMAIL_DOMAIN")"
    if [[ -z "$cur_pub" || "$cur_pub" == "http://127.0.0.1:8088" || "$cur_pub" == "http://127.0.0.1:${edge_port}" ]]; then
      write_env_key "PUBLIC_APP_URL" "$public_url"
    fi
    if [[ -z "$cur_srv" || "$cur_srv" == "http://127.0.0.1:8088" || "$cur_srv" == "http://127.0.0.1:${edge_port}" ]]; then
      write_env_key "SERVER_BASE_URL" "$public_url"
    fi
    if [[ -z "$cur_inv" || "$cur_inv" == "team.dalton.local" ]]; then
      write_env_key "DALTON_INVITE_EMAIL_DOMAIN" "$invite_domain"
    fi
  else
    set_env_if_empty "PUBLIC_APP_URL" "$public_url" || true
    set_env_if_empty "SERVER_BASE_URL" "$public_url" || true
    set_env_if_empty "DALTON_INVITE_EMAIL_DOMAIN" "$invite_domain" || true
  fi

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
  if [[ "$domain" == *:* || "${actual_tls_bind:-$tls_bind}" == *:* ]]; then
    tls_overlay_file="deploy/standalone/docker-compose.tls.ipv6.yml"
  else
    tls_overlay_file="deploy/standalone/docker-compose.tls.yml"
  fi
  ok "DALTON_TLS_MODE=${actual_tls_mode:-$tls_mode} (bind=${actual_tls_bind:-$tls_bind}, http=${actual_http_port:-$http_port}, https=${actual_https_port:-$https_port}, url=${actual_public_url:-$public_url})"

  # Generate all secrets via shared env_tokens.sh helper without overwriting existing values.
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
    "DALTON_RESEARCH_HOST_TOKEN"
    "DALTON_RESEARCH_ADMIN_TOKEN"
  )

  local added_str=""
  added_str="$(ensure_env_tokens "$ENV_FILE" "${secret_keys[@]}")"
  if [[ -n "$added_str" ]]; then
    read -r -a generated <<< "$added_str"
  fi

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
        err "Sandbox image build failed."
        err "If Docker Hub rate-limited the pull (429 Too Many Requests), run 'docker login' or configure registry-mirrors in /etc/docker/daemon.json (see docs/dalton/dev-guide/STANDALONE-DEPLOY.md §7)."
        err "Rerun sandbox build with: docker build -f Dockerfile.sandbox --build-arg INSTALL_AGENT_BROWSER=0 -t langalpha-sandbox:latest ."
        err "Or rerun init.sh with --skip-sandbox-build to complete configuration first."
        exit 1
      fi
    fi
  fi

  local compose_base_cmd="docker compose -p ${project_name} -f docker-compose.yml -f docker-compose.core.yml --env-file .env"
  if [[ "$tls_mode" != "loopback" ]]; then
    compose_base_cmd="docker compose -p ${project_name} -f docker-compose.yml -f docker-compose.core.yml -f ${tls_overlay_file} --env-file .env"
  fi
  local compose_build_cmd="env -u VITE_DALTON_AUTH ${compose_base_cmd} build"
  local compose_start_cmd="${compose_base_cmd} up -d"
  local vite_auth
  vite_auth="$(get_env_value_any "VITE_DALTON_AUTH")"

  printf "\n== Next Steps ==\n"
  if [[ "$NON_INTERACTIVE" -eq 0 ]]; then
    cat <<EOF
  1. Configure LLM provider keys, optional financial data (FMP), and optional
     web search/fetch keys via the interactive setup wizard:
       make config

  2. Optional: set SEC_USER_AGENT="Dalton <your-email@example.com>" in .env
     for SEC EDGAR compliance identification.

  3. Build the images (clearing any exported shell VITE_DALTON_AUTH so Compose
     reads the value from .env) and start the Standalone Core stack:
       ${compose_build_cmd}
       ${compose_start_cmd}
     (Whenever you switch DALTON_AUTH_MODE / VITE_DALTON_AUTH in .env, rebuild edge
      with \`env -u VITE_DALTON_AUTH ${compose_base_cmd} build edge && ${compose_start_cmd}\`
      so the frontend bundle picks up the new authentication mode.)

  4. Open the web UI at ${public_url}
EOF
  else
    info "Non-interactive initialization complete (APP_ENV=core, DOMAIN=${domain}, DALTON_TLS_MODE=${tls_mode}, VITE_DALTON_AUTH=${vite_auth:-<empty>})."
    info "Build images with: ${compose_build_cmd}"
    info "Start the stack with: ${compose_start_cmd}"
  fi
}

main "$@"
