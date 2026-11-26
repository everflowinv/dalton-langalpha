import { execFile } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { describe, expect, it } from "vitest";
import {
  buildIsolatedTestDbName,
  parseEffectiveLibpqParams,
  rebuildIsolatedDsnWithLibpq,
  replaceDsnDatabase,
} from "./pg-isolation.js";

const execFileAsync = promisify(execFile);
const __dirname = dirname(fileURLToPath(import.meta.url));
const GATEWAY_ROOT = resolve(__dirname, "..");
const REPO_ROOT = resolve(GATEWAY_ROOT, "../..");
const VENV_PYTHON = resolve(REPO_ROOT, ".venv/bin/python");

describe("BI2: gateway real-Postgres test isolation and vitest config", () => {
  it("configures vitest.config.ts to run real-Postgres test files serially", async () => {
    const configPath = resolve(GATEWAY_ROOT, "vitest.config.ts");
    expect(existsSync(configPath)).toBe(true);

    const mod = (await import(configPath)) as {
      REAL_POSTGRES_TEST_FILES?: string[];
      default?: {
        test?: {
          fileParallelism?: boolean;
          projects?: Array<{
            test?: {
              name?: string;
              include?: string[];
              exclude?: string[];
              fileParallelism?: boolean;
            };
          }>;
        };
      };
    };

    const expectedRealPgFiles = [
      "test/real-http-postgres.test.ts",
      "test/discord-thread-persistence.test.ts",
      "test/weixin-real-backend.integration.test.ts",
    ];
    expect(mod.REAL_POSTGRES_TEST_FILES).toEqual(expectedRealPgFiles);

    const cfg = mod.default?.test;
    expect(cfg).toBeDefined();
    // Vitest 3.2.7 reads `fileParallelism` from the root config; root MUST disable file parallelism.
    expect(cfg?.fileParallelism).toBe(false);
    if (Array.isArray(cfg?.projects)) {
      const pgProject = cfg.projects.find((p) => p.test?.name === "real-postgres");
      expect(pgProject?.test?.fileParallelism).toBe(false);
      expect(pgProject?.test?.include).toEqual(expectedRealPgFiles);
      const unitProject = cfg.projects.find((p) => p.test?.name === "unit");
      expect(unitProject?.test?.exclude).toEqual(expectedRealPgFiles);
    }
  });

  it("generates unique *_test database names and rewrites local/container *_test DSNs safely", () => {
    const nameA = buildIsolatedTestDbName("real-http-postgres");
    const nameB = buildIsolatedTestDbName("real-http-postgres");
    expect(nameA).not.toBe(nameB);
    expect(nameA.endsWith("_test")).toBe(true);
    expect(nameB.endsWith("_test")).toBe(true);
    expect(nameA.length).toBeLessThanOrEqual(63);
    expect(/^[a-z][a-z0-9_]*_test$/.test(nameA)).toBe(true);

    const isolated = replaceDsnDatabase(
      "postgresql://postgres:secret@127.0.0.1:5432/dalton_shared_test",
      nameA,
    );
    expect(isolated).toBe(`postgresql://postgres:secret@127.0.0.1:5432/${nameA}`);

    // Container-network `*_test` DSNs (single-label service hostname / RFC1918 IP) are supported
    expect(
      replaceDsnDatabase("postgresql://postgres:secret@postgres:5432/dalton_shared_test", nameA),
    ).toBe(`postgresql://postgres:secret@postgres:5432/${nameA}`);
    expect(
      replaceDsnDatabase("postgresql://postgres:secret@172.18.0.2:5432/dalton_shared_test", nameA),
    ).toBe(`postgresql://postgres:secret@172.18.0.2:5432/${nameA}`);

    // Non-local public host requires explicit admin authorization (`allowNonLocal: true`)
    expect(() =>
      replaceDsnDatabase("postgresql://postgres:secret@db.example.com:5432/dalton_shared_test", nameA),
    ).toThrow(/non-local host/);
    expect(
      replaceDsnDatabase("postgresql://postgres:secret@db.example.com:5432/dalton_shared_test", nameA, {
        allowNonLocal: true,
      }),
    ).toBe(`postgresql://postgres:secret@db.example.com:5432/${nameA}`);

    expect(() =>
      replaceDsnDatabase("postgresql://postgres:secret@127.0.0.1:5432/production", nameA),
    ).toThrow(/non-test DB/);
  });

  it("rebuilds isolated DSN from effective libpq parameters (stripping query dbname) and verifies via psycopg.conninfo", async () => {
    const nameA = buildIsolatedTestDbName("libpq-query-dbname");

    // Query param `dbname` overrides URI path in libpq:
    // 1) `?dbname=shared_test&sslmode=disable` MUST be stripped so rebuilt DSN targets `nameA` (not `shared_test`).
    const dsnWithQueryDb =
      "postgresql://postgres:secret@127.0.0.1:5432/ignored_test?dbname=shared_test&sslmode=disable";
    const rebuiltTs = replaceDsnDatabase(dsnWithQueryDb, nameA);
    expect(rebuiltTs).toBe(
      `postgresql://postgres:secret@127.0.0.1:5432/${nameA}?sslmode=disable`,
    );
    expect(parseEffectiveLibpqParams(rebuiltTs).dbname).toBe(nameA);
    expect(new URL(rebuiltTs).searchParams.has("dbname")).toBe(false);

    // 2) Path looks like `*_test`, but query `?dbname=production` overrides it -> MUST refuse!
    expect(() =>
      replaceDsnDatabase(
        "postgresql://postgres:secret@127.0.0.1:5432/fake_test?dbname=production",
        nameA,
      ),
    ).toThrow(/non-test DB/);

    // 3) Path is non-test, but effective query `?dbname=shared_test` IS a test DB -> rebuilds cleanly to `nameA`.
    const fromQueryTest = replaceDsnDatabase(
      "postgresql://postgres:secret@127.0.0.1:5432/postgres?dbname=shared_test",
      nameA,
    );
    expect(fromQueryTest).toBe(`postgresql://postgres:secret@127.0.0.1:5432/${nameA}`);
    expect(parseEffectiveLibpqParams(fromQueryTest).dbname).toBe(nameA);

    // 4) Query `host` / `hostaddr` overriding local authority to a non-local host MUST be refused.
    expect(() =>
      replaceDsnDatabase(
        "postgresql://postgres:secret@127.0.0.1:5432/shared_test?host=db.example.com",
        nameA,
      ),
    ).toThrow(/non-local host/);
    expect(() =>
      replaceDsnDatabase(
        "postgresql://postgres:secret@127.0.0.1:5432/shared_test?hostaddr=8.8.8.8",
        nameA,
      ),
    ).toThrow(/non-local host/);

    // 5) Unresolved libpq `service` param MUST be refused.
    expect(() =>
      replaceDsnDatabase(
        "postgresql://postgres:secret@127.0.0.1:5432/shared_test?service=prod",
        nameA,
      ),
    ).toThrow(/service/);

    // 6) Keyword/value libpq conninfo rebuilds to a clean URI with effective dbname == nameA.
    const kvRebuilt = replaceDsnDatabase(
      "host=127.0.0.1 port=5432 user=postgres password=secret dbname=shared_test sslmode=disable",
      nameA,
    );
    expect(kvRebuilt).toBe(
      `postgresql://postgres:secret@127.0.0.1:5432/${nameA}?sslmode=disable`,
    );

    // 7) Verify with real libpq (`psycopg.conninfo.conninfo_to_dict`) when `.venv/bin/python` is present.
    if (existsSync(VENV_PYTHON)) {
      const rebuiltLibpq = await rebuildIsolatedDsnWithLibpq({
        baseDsn: dsnWithQueryDb,
        dbName: nameA,
        pythonBin: VENV_PYTHON,
        repoRoot: REPO_ROOT,
      });
      expect(rebuiltLibpq).toBe(
        `postgresql://postgres:secret@127.0.0.1:5432/${nameA}?sslmode=disable`,
      );
      const { stdout } = await execFileAsync(
        VENV_PYTHON,
        [
          "-c",
          "import json, sys, psycopg.conninfo as ci; print(json.dumps(ci.conninfo_to_dict(sys.argv[1])))",
          rebuiltLibpq,
        ],
        { cwd: REPO_ROOT, timeout: 10_000 },
      );
      const effective = JSON.parse(stdout.trim()) as Record<string, string>;
      expect(effective.dbname).toBe(nameA);
      expect(effective.sslmode).toBe("disable");

      // libpq helper also rejects `?dbname=production` even when URI path ends with `_test`
      await expect(
        rebuildIsolatedDsnWithLibpq({
          baseDsn: "postgresql://postgres:secret@127.0.0.1:5432/fake_test?dbname=production",
          dbName: nameA,
          pythonBin: VENV_PYTHON,
          repoRoot: REPO_ROOT,
        }),
      ).rejects.toThrow();
    }
  });
});
