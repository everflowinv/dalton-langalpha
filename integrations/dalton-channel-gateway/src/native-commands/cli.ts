import { isCliExecutedDirectly, runNativeCommandsCli } from "./cli-runner.js";
import {
  DALTON_DISCORD_BOT_TOKEN_ENV,
  DALTON_FEISHU_APP_SECRET_ENV,
} from "./config.js";
import { scrubNativeCommandSecrets } from "./redact.js";

export * from "./index.js";

if (isCliExecutedDirectly(import.meta.url)) {
  runNativeCommandsCli(process.argv.slice(2))
    .then((code) => {
      process.exitCode = code;
    })
    .catch((err) => {
      const secrets = [
        process.env[DALTON_FEISHU_APP_SECRET_ENV] ?? "",
        process.env[DALTON_DISCORD_BOT_TOKEN_ENV] ?? "",
      ].filter((s) => s.length > 0);
      const rawMsg = err instanceof Error ? err.message : String(err);
      process.stderr.write(
        `[dalton-native-commands] fatal: ${scrubNativeCommandSecrets(rawMsg, secrets, 2048)}\n`,
      );
      process.exitCode = 1;
    });
}
