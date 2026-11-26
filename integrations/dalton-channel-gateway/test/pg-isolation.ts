import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const LOCAL_PG_HOSTS = new Set([
  "127.0.0.1",
  "localhost",
  "::1",
  "[::1]",
  "host.docker.internal",
]);

function isLocalOrContainerPgHost(hostname: string): boolean {
  const lower = hostname.trim().toLowerCase();
  if (!lower) return false;
  // Local Unix domain socket directory (libpq `host=/var/run/postgresql`)
  if (lower.startsWith("/")) return true;
  if (LOCAL_PG_HOSTS.has(lower)) return true;
  // Single-label DNS hostnames inside container networks (e.g. `postgres`, `db`, `pg-test`)
  if (/^[a-z0-9][a-z0-9-]*$/.test(lower)) return true;
  // Internal / local domain suffixes
  if (lower.endsWith(".local") || lower.endsWith(".internal") || lower.endsWith(".localhost")) {
    return true;
  }
  // RFC1918 private IPv4 ranges used by Docker / Compose / CI bridge networks
  const ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(lower);
  if (ipv4) {
    const a = Number(ipv4[1]);
    const b = Number(ipv4[2]);
    if (a === 10) return true;
    if (a === 172 && b >= 16 && b <= 31) return true;
    if (a === 192 && b === 168) return true;
    if (a === 127) return true;
  }
  return false;
}

export interface IsolatedPostgresDatabase {
  dsn: string;
  dbName: string;
  drop: () => Promise<void>;
}

/**
 * Build a PostgreSQL-safe isolated database name (<= 63 chars) ending with `_test`.
 */
export function buildIsolatedTestDbName(tag: string, uniqueSuffix?: string): string {
  const cleanTag = tag
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 20) || "gw";
  const suffix =
    (uniqueSuffix ?? `${process.pid}_${randomBytes(4).toString("hex")}`)
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, "")
      .slice(0, 24);
  const raw = `dalton_gw_${cleanTag}_${suffix}`;
  const prefix = raw.slice(0, 63 - "_test".length).replace(/_+$/, "");
  return `${prefix}_test`;
}

/**
 * Parse a libpq connection string (URI `postgresql://...` / `postgres://...` or
 * keyword=value conninfo) into its effective connection parameters, mirroring
 * libpq's precedence where query parameters (`?dbname=...`, `?host=...`,
 * `?hostaddr=...`) override URI path/authority values.
 */
export function parseEffectiveLibpqParams(dsn: string): Record<string, string> {
  const trimmed = dsn.trim();
  if (!trimmed) {
    throw new Error("Empty Postgres DSN");
  }

  if (/^postgres(?:ql)?:\/\//i.test(trimmed)) {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "postgresql:" && parsed.protocol !== "postgres:") {
      throw new Error(`Unsupported Postgres protocol "${parsed.protocol}"`);
    }
    const params: Record<string, string> = {};
    if (parsed.username) {
      params.user = decodeURIComponent(parsed.username);
    }
    if (parsed.password) {
      params.password = decodeURIComponent(parsed.password);
    }
    if (parsed.hostname) {
      const cleanHost =
        parsed.hostname.startsWith("[") && parsed.hostname.endsWith("]")
          ? parsed.hostname.slice(1, -1)
          : parsed.hostname;
      params.host = decodeURIComponent(cleanHost);
    }
    if (parsed.port) {
      params.port = parsed.port;
    }
    const pathDb = decodeURIComponent(parsed.pathname.replace(/^\/+/, ""));
    if (pathDb) {
      params.dbname = pathDb;
    }
    // Query parameters override URI components per libpq URI precedence (last wins).
    for (const [rawKey, rawVal] of parsed.searchParams.entries()) {
      const key = rawKey.trim();
      if (!key) continue;
      if (rawVal === "" && key in params) {
        continue;
      }
      params[key] = rawVal;
    }
    return params;
  }

  // Keyword/value libpq conninfo (e.g. `host=127.0.0.1 port=5432 dbname=shared_test`)
  const params: Record<string, string> = {};
  let i = 0;
  const len = trimmed.length;
  while (i < len) {
    while (i < len && /\s/.test(trimmed[i]!)) i += 1;
    if (i >= len) break;
    const eqIdx = trimmed.indexOf("=", i);
    if (eqIdx === -1) {
      throw new Error(`Invalid libpq conninfo near "${trimmed.slice(i, i + 24)}"`);
    }
    const key = trimmed.slice(i, eqIdx).trim();
    if (!/^[a-z_][a-z0-9_]*$/i.test(key)) {
      throw new Error(`Invalid libpq conninfo parameter "${key}"`);
    }
    i = eqIdx + 1;
    while (i < len && /\s/.test(trimmed[i]!)) i += 1;
    let val = "";
    if (i < len && trimmed[i] === "'") {
      i += 1;
      let closed = false;
      while (i < len) {
        const ch = trimmed[i]!;
        if (ch === "\\" && i + 1 < len) {
          val += trimmed[i + 1]!;
          i += 2;
          continue;
        }
        if (ch === "'") {
          i += 1;
          closed = true;
          break;
        }
        val += ch;
        i += 1;
      }
      if (!closed) {
        throw new Error(`Unterminated quoted value for libpq parameter "${key}"`);
      }
    } else {
      const start = i;
      while (i < len && !/\s/.test(trimmed[i]!)) i += 1;
      val = trimmed.slice(start, i);
    }
    params[key] = val;
  }
  if (Object.keys(params).length === 0) {
    throw new Error(`Unsupported Postgres DSN "${dsn}"`);
  }
  return params;
}

function buildUriFromLibpqParams(params: Record<string, string>, dbName: string): string {
  const remaining: Array<[string, string]> = [];
  const user = params.user;
  const password = params.password;
  const host = params.host;
  const port = params.port;

  for (const [k, v] of Object.entries(params)) {
    if (k === "user" || k === "password" || k === "host" || k === "port" || k === "dbname") {
      continue;
    }
    remaining.push([k, v]);
  }

  let auth = "";
  if (user !== undefined && user !== "") {
    auth = encodeURIComponent(user);
    if (password !== undefined && password !== "") {
      auth += `:${encodeURIComponent(password)}`;
    }
    auth += "@";
  }

  let netloc = auth;
  if (host !== undefined && host !== "") {
    if (host.includes(",") || host.startsWith("/")) {
      remaining.unshift(["host", host]);
      if (port !== undefined && port !== "") {
        remaining.push(["port", port]);
      }
    } else {
      const formattedHost =
        host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
      netloc += formattedHost;
      if (port !== undefined && port !== "") {
        netloc += `:${port}`;
      }
    }
  } else if (port !== undefined && port !== "") {
    remaining.push(["port", port]);
  }

  const query = new URLSearchParams();
  for (const [k, v] of remaining) {
    query.append(k, v);
  }
  const qs = query.toString();
  return `postgresql://${netloc}/${encodeURIComponent(dbName)}${qs ? `?${qs}` : ""}`;
}

function validateEffectiveHosts(
  params: Record<string, string>,
  allowNonLocal: boolean,
): void {
  if (allowNonLocal) return;
  const candidates: string[] = [];
  if (params.host) {
    candidates.push(...params.host.split(",").map((h) => h.trim()).filter(Boolean));
  }
  if (params.hostaddr) {
    candidates.push(...params.hostaddr.split(",").map((h) => h.trim()).filter(Boolean));
  }
  if (candidates.length === 0) {
    throw new Error(
      `Refusing to create/drop isolated test DB without explicit local/container host`,
    );
  }
  for (const h of candidates) {
    if (!isLocalOrContainerPgHost(h)) {
      throw new Error(
        `Refusing to create/drop isolated test DB on non-local host "${h}" without explicit adminDsn`,
      );
    }
  }
}

/**
 * Validate `baseDsn`'s effective libpq connection parameters (including query-param
 * `dbname`, `host`, `hostaddr`, and keyword/value conninfo) and rebuild a clean
 * `postgresql://` DSN targeting `newDbName` with no overriding `dbname` query param.
 */
export function replaceDsnDatabase(
  baseDsn: string,
  newDbName: string,
  options?: { allowNonLocal?: boolean },
): string {
  const params = parseEffectiveLibpqParams(baseDsn);
  if (params.service) {
    throw new Error(
      `Refusing to isolate DSN using libpq service="${params.service}"; specify explicit host and *_test dbname`,
    );
  }
  const origDb = (params.dbname ?? "").trim();
  if (!origDb.endsWith("_test") || origDb.length <= "_test".length) {
    throw new Error(`Refusing to isolate non-test DB "${origDb}"`);
  }
  validateEffectiveHosts(params, Boolean(options?.allowNonLocal));
  if (!/^[a-z][a-z0-9_]*_test$/.test(newDbName) || newDbName.length > 63) {
    throw new Error(`Invalid isolated test DB name "${newDbName}"`);
  }

  const rebuilt = buildUriFromLibpqParams(params, newDbName);
  const verified = parseEffectiveLibpqParams(rebuilt);
  if (verified.dbname !== newDbName) {
    throw new Error(
      `Rebuilt isolated DSN resolved to "${verified.dbname ?? ""}" instead of "${newDbName}"`,
    );
  }
  return rebuilt;
}

const PY_REBUILD_AND_VERIFY_DSN_SCRIPT = `
import json
import sys
from urllib.parse import quote, urlencode
import psycopg.conninfo as ci

base_dsn, db_name, allow_non_local_raw = sys.argv[1], sys.argv[2], sys.argv[3]
allow_non_local = allow_non_local_raw == "1"
assert db_name.endswith("_test") and len(db_name) <= 63

params = dict(ci.conninfo_to_dict(base_dsn))
if params.get("service"):
    raise ValueError(f"Refusing to isolate DSN with libpq service={params['service']!r}")
orig_db = str(params.get("dbname") or "").strip()
if not orig_db.endswith("_test") or len(orig_db) <= len("_test"):
    raise ValueError(f"Refusing to isolate non-test DB {orig_db!r}")

def _is_local_host(h: str) -> bool:
    low = h.strip().lower()
    if not low:
        return False
    if low.startswith("/"):
        return True
    if low in ("127.0.0.1", "localhost", "::1", "[::1]", "host.docker.internal"):
        return True
    if low.endswith((".local", ".internal", ".localhost")):
        return True
    if all(c.isalnum() or c == "-" for c in low) and low[0].isalnum():
        return True
    parts = low.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a in (10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168):
            return True
    return False

if not allow_non_local:
    hosts = []
    for k in ("host", "hostaddr"):
        raw = str(params.get(k) or "").strip()
        if raw:
            hosts.extend([x.strip() for x in raw.split(",") if x.strip()])
    if not hosts:
        raise ValueError("Refusing to isolate DB without explicit local/container host")
    for h in hosts:
        if not _is_local_host(h):
            raise ValueError(f"Refusing to create/drop isolated test DB on non-local host {h!r}")

user = params.pop("user", None)
password = params.pop("password", None)
host = params.pop("host", None)
port = params.pop("port", None)
params.pop("dbname", None)

auth = ""
if user is not None and str(user) != "":
    auth = quote(str(user), safe="")
    if password is not None and str(password) != "":
        auth += ":" + quote(str(password), safe="")
    auth += "@"

netloc = auth
extra = [(str(k), str(v)) for k, v in params.items()]
if host is not None and str(host) != "":
    host_str = str(host)
    if "," in host_str or host_str.startswith("/"):
        extra.insert(0, ("host", host_str))
        if port is not None and str(port) != "":
            extra.append(("port", str(port)))
    else:
        fmt_host = f"[{host_str}]" if (":" in host_str and not host_str.startswith("[")) else host_str
        netloc += fmt_host
        if port is not None and str(port) != "":
            netloc += f":{port}"
elif port is not None and str(port) != "":
    extra.append(("port", str(port)))

qs = urlencode(extra)
rebuilt = f"postgresql://{netloc}/{quote(db_name, safe='')}"
if qs:
    rebuilt += f"?{qs}"

effective = ci.conninfo_to_dict(rebuilt)
assert effective.get("dbname") == db_name, (
    f"Expected rebuilt libpq dbname={db_name!r}, got {effective.get('dbname')!r}"
)
print(json.dumps({"dsn": rebuilt, "dbname": effective["dbname"]}))
`;

const PY_CREATE_DB_SCRIPT = `
import sys
import psycopg
import psycopg.conninfo as ci
from psycopg import sql

admin_dsn, isolated_dsn, db_name = sys.argv[1], sys.argv[2], sys.argv[3]
assert db_name.endswith("_test")
effective = ci.conninfo_to_dict(isolated_dsn)
assert effective.get("dbname") == db_name, (
    f"Isolated DSN resolves to {effective.get('dbname')!r} instead of {db_name!r}"
)
with psycopg.connect(admin_dsn, autocommit=True) as conn:
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
with psycopg.connect(isolated_dsn, autocommit=True) as verify_conn:
    row = verify_conn.execute("SELECT current_database()").fetchone()
    actual_db = row[0] if row else None
    assert actual_db == db_name, (
        f"Connected to {actual_db!r} instead of newly created isolated DB {db_name!r}"
    )
`;

const PY_DROP_DB_SCRIPT = `
import sys
import psycopg
from psycopg import sql

admin_dsn, db_name = sys.argv[1], sys.argv[2]
assert db_name.endswith("_test")
with psycopg.connect(admin_dsn, autocommit=True) as conn:
    conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        (db_name,),
    )
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
`;

/**
 * Rebuild an isolated DSN via libpq's own connection parameter parser (`psycopg.conninfo`)
 * and assert the final effective database name equals `dbName`.
 */
export async function rebuildIsolatedDsnWithLibpq(options: {
  baseDsn: string;
  dbName: string;
  pythonBin: string;
  repoRoot: string;
  allowNonLocal?: boolean;
}): Promise<string> {
  const { stdout } = await execFileAsync(
    options.pythonBin,
    [
      "-c",
      PY_REBUILD_AND_VERIFY_DSN_SCRIPT,
      options.baseDsn,
      options.dbName,
      options.allowNonLocal ? "1" : "0",
    ],
    { cwd: options.repoRoot, timeout: 15_000 },
  );
  const parsed = JSON.parse(stdout.trim()) as { dsn?: string; dbname?: string };
  if (!parsed.dsn || parsed.dbname !== options.dbName) {
    throw new Error(
      `libpq rebuilt DSN dbname mismatch: expected "${options.dbName}", got "${parsed.dbname ?? ""}"`,
    );
  }
  return parsed.dsn;
}

/**
 * Create a fresh, empty isolated `*_test` PostgreSQL database for a single gateway
 * real-Postgres test file and return a `drop()` cleanup callback.
 *
 * Rebuilds the isolated DSN using libpq's effective connection parameters (properly
 * handling query-param `dbname`, `host`, `hostaddr`, and keyword/value conninfo)
 * and verifies that connecting to the rebuilt DSN lands on the newly created DB.
 */
export async function createIsolatedPostgresDatabase(options: {
  baseDsn: string;
  tag: string;
  pythonBin: string;
  repoRoot: string;
  adminDsn?: string;
}): Promise<IsolatedPostgresDatabase> {
  const explicitAdminDsn =
    options.adminDsn?.trim() || process.env.DALTON_TEST_PG_ADMIN_DSN?.trim() || "";
  const allowNonLocal = Boolean(explicitAdminDsn);
  const dbName = buildIsolatedTestDbName(options.tag);
  // Rebuild & validate strictly via libpq (`psycopg.conninfo`) so multi-host URIs,
  // query-param `dbname`/`host`/`hostaddr`, and keyword/value conninfo all match libpq.
  const isolatedDsn = await rebuildIsolatedDsnWithLibpq({
    baseDsn: options.baseDsn,
    dbName,
    pythonBin: options.pythonBin,
    repoRoot: options.repoRoot,
    allowNonLocal,
  });
  const effectiveAdminDsn = explicitAdminDsn || options.baseDsn;
  await execFileAsync(
    options.pythonBin,
    ["-c", PY_CREATE_DB_SCRIPT, effectiveAdminDsn, isolatedDsn, dbName],
    { cwd: options.repoRoot, timeout: 30_000 },
  );
  let dropped = false;
  return {
    dsn: isolatedDsn,
    dbName,
    drop: async () => {
      if (dropped) return;
      dropped = true;
      try {
        await execFileAsync(
          options.pythonBin,
          ["-c", PY_DROP_DB_SCRIPT, effectiveAdminDsn, dbName],
          { cwd: options.repoRoot, timeout: 30_000 },
        );
      } catch {
        // Best-effort cleanup on teardown
      }
    },
  };
}
