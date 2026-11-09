import { build } from "esbuild";
import { chmodSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "dist/index.mjs");
mkdirSync(dirname(out), { recursive: true });

await build({
  entryPoints: [resolve(here, "src/main.ts")],
  outfile: out,
  bundle: true,
  platform: "node",
  target: "node22",
  format: "esm",
  banner: { js: "#!/usr/bin/env node" },
  logLevel: "warning",
});

chmodSync(out, 0o755);
