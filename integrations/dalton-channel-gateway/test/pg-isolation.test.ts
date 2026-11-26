import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  buildIsolatedTestDbName,
  replaceDsnDatabase,
} from "./pg-isolation.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const GATEWAY_ROOT = resolve(__dirname, "..");

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
});
