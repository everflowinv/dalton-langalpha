import { build } from "esbuild";
import { chmodSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, "dist/index.mjs");
mkdirSync(dirname(out), { recursive: true });

await build({
  entryPoints: [resolve(here, "src/main.ts")],
  outfile: out,
  bundle: true,
  external: ["@larksuiteoapi/node-sdk"],
  platform: "node",
  target: "node22",
  format: "esm",
  banner: {
    js: "#!/usr/bin/env node\nimport { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);",
  },
  logLevel: "warning",
});

chmodSync(out, 0o755);
await import(pathToFileURL(out).href);

const nativeCommandsOut = resolve(here, "dist/native-commands.mjs");

await build({
  entryPoints: [resolve(here, "src/native-commands/cli.ts")],
  outfile: nativeCommandsOut,
  bundle: true,
  platform: "node",
  target: "node22",
  format: "esm",
  banner: {
    js: "#!/usr/bin/env node",
  },
  logLevel: "warning",
});

chmodSync(nativeCommandsOut, 0o755);
await import(pathToFileURL(nativeCommandsOut).href);
