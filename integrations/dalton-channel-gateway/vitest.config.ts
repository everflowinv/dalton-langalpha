import { defineConfig } from "vitest/config";

export const REAL_POSTGRES_TEST_FILES = [
  "test/real-http-postgres.test.ts",
  "test/discord-thread-persistence.test.ts",
  "test/weixin-real-backend.integration.test.ts",
];

export default defineConfig({
  test: {
    // BI2: Vitest 3.2.7 reads `fileParallelism` from the root config; keep it false
    // at root so real-Postgres test files never execute concurrently in any invocation.
    fileParallelism: false,
    projects: [
      {
        test: {
          name: "unit",
          include: ["test/**/*.test.ts"],
          exclude: REAL_POSTGRES_TEST_FILES,
        },
      },
      {
        test: {
          name: "real-postgres",
          include: REAL_POSTGRES_TEST_FILES,
          fileParallelism: false,
          maxConcurrency: 1,
          sequence: {
            concurrent: false,
          },
        },
      },
    ],
  },
});
