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
 * Validate that `baseDsn` points to a `*_test` database on a local/container host
 * (or when explicitly authorized via `allowNonLocal` with a dedicated test admin DSN)
 * and return a new DSN pointing at `newDbName`.
 */
export function replaceDsnDatabase(
  baseDsn: string,
  newDbName: string,
  options?: { allowNonLocal?: boolean },
): string {
  const parsed = new URL(baseDsn);
  if (parsed.protocol !== "postgresql:" && parsed.protocol !== "postgres:") {
    throw new Error(`Unsupported Postgres protocol "${parsed.protocol}"`);
  }
  const origDb = parsed.pathname.replace(/^\/+/, "");
  if (!origDb.endsWith("_test") || origDb.length <= "_test".length) {
    throw new Error(`Refusing to isolate non-test DB "${origDb}"`);
  }
  if (!options?.allowNonLocal && !isLocalOrContainerPgHost(parsed.hostname)) {
    throw new Error(
      `Refusing to create/drop isolated test DB on non-local host "${parsed.hostname}" without explicit adminDsn`,
    );
  }
  if (!/^[a-z][a-z0-9_]*_test$/.test(newDbName) || newDbName.length > 63) {
    throw new Error(`Invalid isolated test DB name "${newDbName}"`);
  }
  const next = new URL(parsed.toString());
  next.pathname = `/${newDbName}`;
  return next.toString();
}

const PY_CREATE_DB_SCRIPT = `
import sys
import psycopg
from psycopg import sql

admin_dsn, db_name = sys.argv[1], sys.argv[2]
assert db_name.endswith("_test")
with psycopg.connect(admin_dsn, autocommit=True) as conn:
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
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
 * Create a fresh, empty isolated `*_test` PostgreSQL database for a single gateway
 * real-Postgres test file and return a `drop()` cleanup callback.
 *
 * Supports local loopback hosts, container-network `*_test` hosts (e.g. `postgres`),
 * and arbitrary test hosts when an explicit `adminDsn` (or `DALTON_TEST_PG_ADMIN_DSN`)
 * is provided for `CREATE DATABASE` / `DROP DATABASE`.
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
  const dbName = buildIsolatedTestDbName(options.tag);
  const isolatedDsn = replaceDsnDatabase(options.baseDsn, dbName, {
    allowNonLocal: Boolean(explicitAdminDsn),
  });
  const effectiveAdminDsn = explicitAdminDsn || options.baseDsn;
  await execFileAsync(
    options.pythonBin,
    ["-c", PY_CREATE_DB_SCRIPT, effectiveAdminDsn, dbName],
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
