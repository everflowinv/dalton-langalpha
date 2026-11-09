import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { AddressInfo, Socket } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { inspect } from "node:util";
import { build } from "esbuild";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  DALTON_NATIVE_COMMANDS,
  DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT,
  DISCORD_COMMAND_OPTION_TYPE_STRING,
  DISCORD_GLOBAL_CONTEXTS,
  DISCORD_GLOBAL_INTEGRATION_TYPES,
  FEISHU_SLASH_COMMAND_BASE_PATH,
  FEISHU_TRUSTED_API_ORIGIN,
  loadDiscordNativeCommandCredentials,
  loadFeishuNativeCommandCredentials,
  NativeCommandRegistrationError,
  resolveVerifiedApiOrigin,
  runNativeCommandsCli,
  syncDiscordNativeCommands,
  syncFeishuNativeCommands,
} from "../src/native-commands/cli.js";
import {
  createSubsystemLogger,
  type LoggerSink,
} from "../src/shared/logger.js";

interface RecordedRequest {
  method: string;
  url: string;
  pathname: string;
  query: URLSearchParams;
  headers: Record<string, string | string[] | undefined>;
  rawBody: string;
  jsonBody: unknown;
}

interface LocalHttpServerHarness {
  origin: string;
  requests: RecordedRequest[];
  handlerErrors: unknown[];
  close: () => Promise<void>;
}

const activeServers: LocalHttpServerHarness[] = [];

async function startLocalHttpServer(
  handler: (
    req: IncomingMessage,
    res: ServerResponse,
    parsed: RecordedRequest,
  ) => void | Promise<void>,
): Promise<LocalHttpServerHarness> {
  const requests: RecordedRequest[] = [];
  const handlerErrors: unknown[] = [];
  const sockets = new Set<Socket>();

  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => {
      chunks.push(chunk);
    });
    req.on("end", () => {
      const rawBody = Buffer.concat(chunks).toString("utf8");
      let jsonBody: unknown = undefined;
      if (rawBody.trim().length > 0) {
        try {
          jsonBody = JSON.parse(rawBody);
        } catch {
          jsonBody = undefined;
        }
      }
      const parsedUrl = new URL(req.url ?? "/", "http://127.0.0.1");
      const recorded: RecordedRequest = {
        method: req.method ?? "GET",
        url: req.url ?? "/",
        pathname: parsedUrl.pathname,
        query: parsedUrl.searchParams,
        headers: req.headers,
        rawBody,
        jsonBody,
      };
      requests.push(recorded);
      (async () => {
        await handler(req, res, recorded);
      })().catch((err) => {
        handlerErrors.push(err);
        if (!res.headersSent && !res.destroyed) {
          res.statusCode = 500;
          res.end(String(err));
        }
      });
    });
  });

  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.on("close", () => {
      sockets.delete(socket);
    });
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });

  const addr = server.address() as AddressInfo;
  const harness: LocalHttpServerHarness = {
    origin: `http://127.0.0.1:${addr.port}`,
    requests,
    handlerErrors,
    close: async () => {
      for (const sock of sockets) {
        sock.destroy();
      }
      sockets.clear();
      await new Promise<void>((resolve) => {
        server.close(() => resolve());
      });
    },
  };

  activeServers.push(harness);
  return harness;
}

function sendJson(
  res: ServerResponse,
  statusCode: number,
  body: unknown,
  extraHeaders?: Record<string, string>,
): void {
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    ...extraHeaders,
  });
  res.end(JSON.stringify(body));
}

function createCapturingLogger(): {
  lines: string[];
  sink: LoggerSink;
  logger: ReturnType<typeof createSubsystemLogger>;
} {
  const lines: string[] = [];
  const sink: LoggerSink = {
    debug: (line) => lines.push(line),
    info: (line) => lines.push(line),
    warn: (line) => lines.push(line),
    error: (line) => lines.push(line),
  };
  return {
    lines,
    sink,
    logger: createSubsystemLogger("test/native-commands", {
      sink,
      enableDebug: true,
    }),
  };
}

function createBufferWriter(): {
  text: () => string;
  write: (chunk: string) => boolean;
} {
  const chunks: string[] = [];
  return {
    text: () => chunks.join(""),
    write: (chunk: string) => {
      chunks.push(chunk);
      return true;
    },
  };
}

afterEach(async () => {
  const recordedErrors: unknown[] = [];
  while (activeServers.length > 0) {
    const srv = activeServers.pop()!;
    recordedErrors.push(...srv.handlerErrors);
    await srv.close();
  }
  if (recordedErrors.length > 0) {
    throw recordedErrors[0];
  }
});

describe("Native command manifest & standalone config invariants", () => {
  it("defines the exact four fixed commands without private workspace choices", () => {
    expect(DALTON_NATIVE_COMMANDS.map((c) => c.name)).toEqual([
      "main",
      "workspace",
      "new",
      "stop",
    ]);
    expect(DALTON_NATIVE_COMMANDS.map((c) => c.description)).toEqual([
      "返回 Chief of Staff",
      "查看可访问的工作区",
      "在当前工作区新建会话",
      "停止当前任务",
    ]);

    for (const cmd of DALTON_NATIVE_COMMANDS) {
      expect(cmd.name.startsWith("/")).toBe(false);
      expect(cmd.description.trim().length).toBeGreaterThan(0);
    }

    const ws = DALTON_NATIVE_COMMANDS.find((c) => c.name === "workspace")!;
    expect(ws.options).toEqual([
      {
        name: "workspace",
        description: "要切换的工作区名称，留空查看可访问列表",
        type: "string",
        required: false,
        autocomplete: true,
      },
    ]);
    expect("choices" in ws.options[0]!).toBe(false);
  });

  it("ignores non-Dalton / OpenClaw environment variables and requires Dalton credentials", () => {
    expect(() =>
      loadFeishuNativeCommandCredentials({
        OPENCLAW_FEISHU_APP_ID: "cli_openclaw",
        OPENCLAW_FEISHU_APP_SECRET: "sec_openclaw",
        FEISHU_APP_ID: "cli_other",
        FEISHU_APP_SECRET: "sec_other",
      }),
    ).toThrowError(/DALTON_FEISHU_APP_ID/);

    expect(() =>
      loadDiscordNativeCommandCredentials({
        OPENCLAW_DISCORD_TOKEN: "tok_openclaw",
        DISCORD_BOT_TOKEN: "tok_other",
        DISCORD_APPLICATION_ID: "123456789012345678",
      }),
    ).toThrowError(/DALTON_DISCORD_BOT_TOKEN/);
  });

  it("permits only trusted production origins or local loopback HTTP test servers", () => {
    expect(
      resolveVerifiedApiOrigin(FEISHU_TRUSTED_API_ORIGIN, undefined, "feishu"),
    ).toBe("https://open.feishu.cn");
    expect(
      resolveVerifiedApiOrigin(
        FEISHU_TRUSTED_API_ORIGIN,
        "http://127.0.0.1:18080",
        "feishu",
      ),
    ).toBe("http://127.0.0.1:18080");

    expect(() =>
      resolveVerifiedApiOrigin(
        FEISHU_TRUSTED_API_ORIGIN,
        "https://evil.example.com",
        "feishu",
      ),
    ).toThrowError(NativeCommandRegistrationError);

    expect(() =>
      resolveVerifiedApiOrigin(
        FEISHU_TRUSTED_API_ORIGIN,
        "http://user:pass@127.0.0.1:18080",
        "feishu",
      ),
    ).toThrowError(NativeCommandRegistrationError);
  });

  it("prints CLI --help and exits 0 without reading credentials or making HTTP requests", async () => {
    let networkCalled = false;
    const stdout = createBufferWriter();
    const stderr = createBufferWriter();

    const code = await runNativeCommandsCli(["--help"], {
      env: {},
      stdout,
      stderr,
      fetchImpl: async () => {
        networkCalled = true;
        throw new Error("Unexpected network call");
      },
    });

    expect(code).toBe(0);
    expect(networkCalled).toBe(false);
    expect(stdout.text()).toContain("DALTON_FEISHU_APP_ID");
    expect(stdout.text()).toContain("DALTON_DISCORD_APPLICATION_ID");
    expect(stderr.text()).toBe("");

    // Conflicting CLI flags (--apply + --dry-run, or --guild-id on feishu) return usage exit code 2 without network calls
    const badModeCode = await runNativeCommandsCli(
      ["--platform", "discord", "--apply", "--dry-run"],
      {
        env: {},
        stdout: createBufferWriter(),
        stderr: createBufferWriter(),
        fetchImpl: async () => {
          networkCalled = true;
          throw new Error("Unexpected network call");
        },
      },
    );
    expect(badModeCode).toBe(2);

    const badFeishuScopeCode = await runNativeCommandsCli(
      ["--platform", "feishu", "--guild-id", "123456789012345678"],
      {
        env: {},
        stdout: createBufferWriter(),
        stderr: createBufferWriter(),
        fetchImpl: async () => {
          networkCalled = true;
          throw new Error("Unexpected network call");
        },
      },
    );
    expect(badFeishuScopeCode).toBe(2);
    expect(networkCalled).toBe(false);
  });
});

describe("Feishu native slash-command registration (local HTTP server)", () => {
  const TEST_APP_ID = "cli_dalton_feishu_app_01";
  const TEST_APP_SECRET = "feishu_super_secret_val_998877";
  const TEST_TENANT_TOKEN = "t-g104_secret_tenant_token_abcdef123456";

  it("previews changes by default without writing, then applies creates/updates idempotently while preserving unmanaged commands and fields", async () => {
    const store: Array<{
      command_id: string;
      command: string;
      description: { default_value: string; i18n?: Record<string, string> };
      icon?: { icon_key: string };
    }> = [
      {
        command_id: "cmd_other_1",
        command: "ops_debug",
        description: { default_value: "运维调试命令" },
        icon: { icon_key: "debug_icon" },
      },
      {
        command_id: "cmd_main_1",
        command: "main",
        description: {
          default_value: "返回 Chief of Staff",
          i18n: { en_us: "Return to Chief of Staff" },
        },
        icon: { icon_key: "home_custom_icon" },
      },
      {
        command_id: "cmd_ws_1",
        command: "workspace",
        description: {
          default_value: "旧版工作区说明",
          i18n: { en_us: "View accessible workspaces" },
        },
        icon: { icon_key: "ws_custom_icon" },
      },
    ];

    let nextId = 100;
    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (
        rec.method === "POST" &&
        rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal"
      ) {
        expect(rec.jsonBody).toEqual({
          app_id: TEST_APP_ID,
          app_secret: TEST_APP_SECRET,
        });
        sendJson(res, 200, {
          code: 0,
          msg: "ok",
          tenant_access_token: TEST_TENANT_TOKEN,
          expire: 7200,
        });
        return;
      }

      expect(rec.headers.authorization).toBe(`Bearer ${TEST_TENANT_TOKEN}`);

      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        sendJson(res, 200, {
          code: 0,
          msg: "success",
          data: {
            items: store,
            has_more: false,
          },
        });
        return;
      }

      if (rec.method === "POST" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        const body = rec.jsonBody as {
          command: string;
          description: { default_value: string; i18n?: Record<string, string> };
          icon?: { icon_key: string };
        };
        const created = {
          command_id: `cmd_created_${nextId++}`,
          command: body.command,
          description: body.description,
          icon: body.icon ?? { icon_key: "skill_outlined" },
        };
        store.push(created);
        sendJson(res, 200, {
          code: 0,
          msg: "success",
          data: created,
        });
        return;
      }

      if (
        rec.method === "PATCH" &&
        rec.pathname.startsWith(`${FEISHU_SLASH_COMMAND_BASE_PATH}/`)
      ) {
        const id = decodeURIComponent(
          rec.pathname.slice(`${FEISHU_SLASH_COMMAND_BASE_PATH}/`.length),
        );
        const existing = store.find((item) => item.command_id === id);
        if (!existing) {
          sendJson(res, 404, { code: 40400, msg: "not found" });
          return;
        }
        const patch = rec.jsonBody as {
          description?: { default_value: string; i18n?: Record<string, string> };
          icon?: { icon_key: string };
        };
        if (patch.description) {
          existing.description = patch.description;
        }
        if (patch.icon) {
          existing.icon = patch.icon;
        }
        sendJson(res, 200, {
          code: 0,
          msg: "success",
          data: existing,
        });
        return;
      }

      sendJson(res, 404, { code: 404, msg: "unexpected route" });
    });

    // 1. Default CLI run (no --apply): preview only, zero POST/PATCH mutations, zero leaked secrets
    const previewOut = createBufferWriter();
    const previewErr = createBufferWriter();
    const previewExit = await runNativeCommandsCli(["--platform", "feishu"], {
      env: {
        DALTON_FEISHU_APP_ID: TEST_APP_ID,
        DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
      },
      stdout: previewOut,
      stderr: previewErr,
      testApiOrigin: srv.origin,
    });

    expect(previewExit).toBe(0);
    expect(
      srv.requests.map((r) => `${r.method} ${r.pathname}`),
    ).toEqual([
      "POST /open-apis/auth/v3/tenant_access_token/internal",
      `GET ${FEISHU_SLASH_COMMAND_BASE_PATH}`,
    ]);
    expect(previewOut.text()).toContain("[无需修改] /main");
    expect(previewOut.text()).toContain("[修改] /workspace");
    expect(previewOut.text()).toContain("[创建] /new");
    expect(previewOut.text()).toContain("[创建] /stop");
    expect(previewOut.text()).toContain("/ops_debug");
    expect(previewOut.text()).not.toContain(TEST_APP_SECRET);
    expect(previewOut.text()).not.toContain(TEST_TENANT_TOKEN);

    // 2. Apply run (--apply): updates `workspace` (preserving i18n & icon), creates `new` & `stop`, leaves `main` & `ops_debug` untouched
    srv.requests.length = 0;
    const applyOut = createBufferWriter();
    const applyExit = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
        },
        stdout: applyOut,
        stderr: createBufferWriter(),
        testApiOrigin: srv.origin,
      },
    );

    expect(applyExit).toBe(0);
    expect(srv.requests.map((r) => `${r.method} ${r.pathname}`)).toEqual([
      "POST /open-apis/auth/v3/tenant_access_token/internal",
      `GET ${FEISHU_SLASH_COMMAND_BASE_PATH}`,
      `PATCH ${FEISHU_SLASH_COMMAND_BASE_PATH}/cmd_ws_1`,
      `POST ${FEISHU_SLASH_COMMAND_BASE_PATH}`,
      `POST ${FEISHU_SLASH_COMMAND_BASE_PATH}`,
    ]);

    const patchReq = srv.requests[2]!;
    expect(patchReq.jsonBody).toEqual({
      description: {
        default_value: "查看可访问的工作区",
        i18n: { en_us: "View accessible workspaces" },
      },
    });

    const createNewReq = srv.requests[3]!;
    expect(createNewReq.jsonBody).toEqual({
      command: "new",
      description: { default_value: "在当前工作区新建会话" },
    });

    const createStopReq = srv.requests[4]!;
    expect(createStopReq.jsonBody).toEqual({
      command: "stop",
      description: { default_value: "停止当前任务" },
    });

    // Unmanaged icon on `workspace` and unmanaged command `ops_debug` remain intact
    expect(store.find((c) => c.command === "workspace")?.icon).toEqual({
      icon_key: "ws_custom_icon",
    });
    expect(store.find((c) => c.command === "ops_debug")).toBeDefined();
    expect(applyOut.text()).toContain("5 分钟");

    // 3. Second `--apply` run: completely idempotent, zero write requests
    srv.requests.length = 0;
    const secondApply = await syncFeishuNativeCommands({
      appId: TEST_APP_ID,
      appSecret: TEST_APP_SECRET,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(secondApply.ok).toBe(true);
    expect(secondApply.outcomes?.every((o) => o.status === "unchanged")).toBe(
      true,
    );
    expect(srv.requests.map((r) => `${r.method} ${r.pathname}`)).toEqual([
      "POST /open-apis/auth/v3/tenant_access_token/internal",
      `GET ${FEISHU_SLASH_COMMAND_BASE_PATH}`,
    ]);
  });

  it("completes multi-page pagination before diffing and rejects duplicate or runaway cursors without writing", async () => {
    let mode: "valid_pages" | "duplicate_cursor" | "too_many_pages" =
      "valid_pages";
    let writeCount = 0;

    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_TENANT_TOKEN,
        });
        return;
      }

      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        const token = rec.query.get("page_token");
        if (mode === "duplicate_cursor") {
          sendJson(res, 200, {
            code: 0,
            data: {
              items: [
                {
                  command_id: "cmd_1",
                  command: "main",
                  description: { default_value: "返回 Chief of Staff" },
                },
              ],
              has_more: true,
              page_token: "cursor_repeat",
            },
          });
          return;
        }

        if (mode === "too_many_pages") {
          const nextIdx = token ? Number.parseInt(token, 10) + 1 : 1;
          sendJson(res, 200, {
            code: 0,
            data: {
              items: [],
              has_more: true,
              page_token: String(nextIdx),
            },
          });
          return;
        }

        if (!token) {
          sendJson(res, 200, {
            code: 0,
            data: {
              items: [
                {
                  command_id: "cmd_m",
                  command: "main",
                  description: { default_value: "返回 Chief of Staff" },
                },
                {
                  command_id: "cmd_w",
                  command: "workspace",
                  description: { default_value: "查看可访问的工作区" },
                },
              ],
              has_more: true,
              page_token: "page_2",
            },
          });
          return;
        }

        if (token === "page_2") {
          sendJson(res, 200, {
            code: 0,
            data: {
              items: [
                {
                  command_id: "cmd_n",
                  command: "new",
                  description: { default_value: "在当前工作区新建会话" },
                },
                {
                  command_id: "cmd_s",
                  command: "stop",
                  description: { default_value: "停止当前任务" },
                },
              ],
              has_more: false,
            },
          });
          return;
        }
      }

      if (rec.method === "POST" || rec.method === "PATCH") {
        writeCount += 1;
        sendJson(res, 200, { code: 0, data: { command_id: "unexpected" } });
        return;
      }

      sendJson(res, 404, { code: 404 });
    });

    const okResult = await syncFeishuNativeCommands({
      appId: TEST_APP_ID,
      appSecret: TEST_APP_SECRET,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(okResult.ok).toBe(true);
    expect(okResult.outcomes?.map((o) => o.status)).toEqual([
      "unchanged",
      "unchanged",
      "unchanged",
      "unchanged",
    ]);
    expect(writeCount).toBe(0);

    mode = "duplicate_cursor";
    await expect(
      syncFeishuNativeCommands({
        appId: TEST_APP_ID,
        appSecret: TEST_APP_SECRET,
        mode: "apply",
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "pagination_error" });
    expect(writeCount).toBe(0);

    mode = "too_many_pages";
    await expect(
      syncFeishuNativeCommands({
        appId: TEST_APP_ID,
        appSecret: TEST_APP_SECRET,
        mode: "apply",
        maxPages: 3,
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "pagination_error" });
    expect(writeCount).toBe(0);
  });

  it("fails before writing on duplicate remote command names, invalid IDs, or missing Feishu scopes", async () => {
    let scenario: "duplicate" | "missing_id" | "missing_scope" = "duplicate";
    let writeCount = 0;

    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_TENANT_TOKEN,
        });
        return;
      }

      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        if (scenario === "missing_scope") {
          sendJson(res, 403, {
            code: 99991672,
            msg: "Access denied. One of the following scopes is required: [application:app_slash_command:read]",
          });
          return;
        }
        if (scenario === "duplicate") {
          sendJson(res, 200, {
            code: 0,
            data: {
              items: [
                {
                  command_id: "cmd_1",
                  command: "main",
                  description: { default_value: "a" },
                },
                {
                  command_id: "cmd_2",
                  command: "main",
                  description: { default_value: "b" },
                },
              ],
            },
          });
          return;
        }
        sendJson(res, 200, {
          code: 0,
          data: {
            items: [
              {
                command_id: "",
                command: "main",
                description: { default_value: "a" },
              },
            ],
          },
        });
        return;
      }

      writeCount += 1;
      sendJson(res, 200, { code: 0 });
    });

    await expect(
      syncFeishuNativeCommands({
        appId: TEST_APP_ID,
        appSecret: TEST_APP_SECRET,
        mode: "apply",
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "duplicate_remote_command" });

    scenario = "missing_id";
    await expect(
      syncFeishuNativeCommands({
        appId: TEST_APP_ID,
        appSecret: TEST_APP_SECRET,
        mode: "apply",
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "invalid_remote_command" });

    scenario = "missing_scope";
    const stderr = createBufferWriter();
    const exitCode = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
        },
        stdout: createBufferWriter(),
        stderr,
        testApiOrigin: srv.origin,
      },
    );
    expect(exitCode).toBe(3);
    expect(stderr.text()).toContain("application:app_slash_command:read");
    expect(writeCount).toBe(0);

    // Feishu app_id identity mismatch is rejected before reading or writing commands
    await expect(
      syncFeishuNativeCommands({
        appId: TEST_APP_ID,
        appSecret: TEST_APP_SECRET,
        expectedAppId: "cli_different_app_99",
        mode: "apply",
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "identity_mismatch" });
    expect(writeCount).toBe(0);
  });

  it("reports missing application:app_slash_command:write when Feishu rejects POST with 403", async () => {
    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_TENANT_TOKEN,
        });
        return;
      }
      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        sendJson(res, 200, { code: 0, data: { items: [], has_more: false } });
        return;
      }
      sendJson(res, 403, {
        code: 99991672,
        msg: "Access denied. Scope required: application:app_slash_command:write",
      });
    });

    const stderr = createBufferWriter();
    const exitCode = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
        },
        stdout: createBufferWriter(),
        stderr,
        testApiOrigin: srv.origin,
      },
    );
    expect(exitCode).toBe(3);
    expect(stderr.text()).toContain("application:app_slash_command:write");
  });

  it("verifies live Feishu state after a dropped POST connection without blind duplicate creation, and returns exit code 4 when unconfirmed", async () => {
    let commitBeforeSocketDrop = true;
    const store: Array<{
      command_id: string;
      command: string;
      description: { default_value: string };
    }> = [];
    let droppedOnce = false;
    let postMainCount = 0;

    const srv = await startLocalHttpServer((req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_TENANT_TOKEN,
        });
        return;
      }

      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        sendJson(res, 200, {
          code: 0,
          data: { items: store, has_more: false },
        });
        return;
      }

      if (rec.method === "POST" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        const body = rec.jsonBody as {
          command: string;
          description: { default_value: string };
        };
        if (body.command === "main") {
          postMainCount += 1;
        }
        if (!droppedOnce && body.command === "main") {
          droppedOnce = true;
          if (commitBeforeSocketDrop) {
            store.push({
              command_id: "cmd_recovered_main",
              command: body.command,
              description: body.description,
            });
          }
          req.socket.destroy();
          return;
        }

        const created = {
          command_id: `cmd_${body.command}`,
          command: body.command,
          description: body.description,
        };
        store.push(created);
        sendJson(res, 200, { code: 0, data: created });
      }
    });

    // Subcase 1: Server committed `main` before socket reset -> verified via GET, zero duplicate POSTs
    const recoveredExit = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
        },
        stdout: createBufferWriter(),
        stderr: createBufferWriter(),
        testApiOrigin: srv.origin,
      },
    );
    expect(recoveredExit).toBe(0);
    expect(postMainCount).toBe(1);
    expect(store.map((s) => s.command)).toEqual([
      "main",
      "workspace",
      "new",
      "stop",
    ]);

    // Subcase 2: Socket drops without server commit -> does NOT re-POST `main`, returns unconfirmed exit code 4
    store.length = 0;
    droppedOnce = false;
    postMainCount = 0;
    commitBeforeSocketDrop = false;

    const stdout = createBufferWriter();
    const stderr = createBufferWriter();
    const unconfirmedExit = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply", "--json"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
        },
        stdout,
        stderr,
        testApiOrigin: srv.origin,
      },
    );

    expect(unconfirmedExit).toBe(4);
    expect(postMainCount).toBe(1);
    const parsedJson = JSON.parse(stdout.text()) as {
      ok: boolean;
      status: string;
      outcomes: Array<{ name: string; status: string }>;
    };
    expect(parsedJson.ok).toBe(false);
    expect(parsedJson.status).toBe("unconfirmed");
    expect(parsedJson.outcomes).toEqual([
      {
        name: "main",
        status: "unconfirmed",
        changedFields: ["command", "description.default_value"],
        errorCode: "unconfirmed_write",
        unconfirmedReason: expect.stringContaining("写后回查未在服务端命令列表中找到"),
      },
      {
        name: "workspace",
        status: "skipped",
        changedFields: ["command", "description.default_value"],
      },
      {
        name: "new",
        status: "skipped",
        changedFields: ["command", "description.default_value"],
      },
      {
        name: "stop",
        status: "skipped",
        changedFields: ["command", "description.default_value"],
      },
    ]);
  });
});

describe("Discord API v10 native command registration (local HTTP server)", () => {
  const TEST_DISCORD_APP_ID = "123456789012345678";
  const TEST_GUILD_ID = "987654321098765432";
  // Base64 prefix intentionally encodes `123456789012345678` so we can prove
  // identity verification uses `/oauth2/applications/@me` rather than token decoding.
  const TEST_DISCORD_BOT_TOKEN =
    "MTIzNDU2Nzg5MDEyMzQ1Njc4.GhIjKl.secretDiscordBotTokenValue987654321";

  it("verifies application identity via /oauth2/applications/@me and aborts on application_id mismatch without touching commands", async () => {
    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.method === "GET" && rec.pathname === "/api/v10/oauth2/applications/@me") {
        expect(rec.headers.authorization).toBe(`Bot ${TEST_DISCORD_BOT_TOKEN}`);
        sendJson(res, 200, {
          id: "999999999999999999",
          name: "Wrong Bot App",
        });
        return;
      }
      sendJson(res, 500, { message: "Commands endpoint must not be called" });
    });

    const stderr = createBufferWriter();
    const exitCode = await runNativeCommandsCli(
      ["--platform", "discord", "--apply"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: createBufferWriter(),
        stderr,
        testApiOrigin: srv.origin,
      },
    );

    expect(exitCode).toBe(3);
    expect(srv.requests).toHaveLength(1);
    expect(srv.requests[0]!.pathname).toBe("/api/v10/oauth2/applications/@me");
    expect(stderr.text()).toContain("identity_mismatch");
    expect(stderr.text()).not.toContain(TEST_DISCORD_BOT_TOKEN);
  });

  it("distinguishes global (GUILD + BOT_DM) vs guild paths, never uses PUT/DELETE, preserves other commands & localizations, and is idempotent", async () => {
    const globalCommands: Array<Record<string, unknown>> = [
      // Unrelated CHAT_INPUT command
      {
        id: "cmd_legacy_help",
        type: 1,
        application_id: TEST_DISCORD_APP_ID,
        name: "help",
        description: "Legacy help",
      },
      // USER context-menu command sharing name "main" (type=2, must NOT collide or be modified)
      {
        id: "cmd_user_ctx_main",
        type: 2,
        application_id: TEST_DISCORD_APP_ID,
        name: "main",
        description: "",
      },
      // Existing CHAT_INPUT "main" already matching (contexts reversed [1, 0], with unmanaged localization)
      {
        id: "cmd_chat_main",
        type: 1,
        application_id: TEST_DISCORD_APP_ID,
        name: "main",
        description: "返回 Chief of Staff",
        description_localizations: { "en-US": "Return to Chief of Staff" },
        integration_types: [0],
        contexts: [1, 0],
        version: "1001",
      },
      // Existing CHAT_INPUT "workspace" missing BOT_DM context and carrying stale static choices + unmanaged option localization
      {
        id: "cmd_chat_ws",
        type: 1,
        application_id: TEST_DISCORD_APP_ID,
        name: "workspace",
        description: "旧描述",
        description_localizations: { "en-US": "List workspaces" },
        integration_types: [0],
        contexts: [0],
        options: [
          {
            name: "workspace",
            type: 3,
            description: "旧参数",
            required: false,
            autocomplete: false,
            choices: [{ name: "secret-alpha", value: "secret-alpha" }],
            description_localizations: { "en-US": "Workspace name" },
          },
        ],
      },
    ];

    const guildCommands: Array<Record<string, unknown>> = [];
    let idSeq = 500;

    const srv = await startLocalHttpServer((_req, res, rec) => {
      expect(rec.headers.authorization).toBe(`Bot ${TEST_DISCORD_BOT_TOKEN}`);
      expect(rec.method).not.toBe("PUT");
      expect(rec.method).not.toBe("DELETE");

      if (rec.method === "GET" && rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton Bot" });
        return;
      }

      const globalBase = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      const guildBase = `/api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`;

      const stripLocalizationsForDefaultList = (
        cmd: Record<string, unknown>,
      ): Record<string, unknown> => {
        const clone: Record<string, unknown> = { ...cmd };
        delete clone.name_localizations;
        delete clone.description_localizations;
        if (Array.isArray(clone.options)) {
          clone.options = clone.options.map((opt) => {
            const optClone = { ...(opt as Record<string, unknown>) };
            delete optClone.name_localizations;
            delete optClone.description_localizations;
            return optClone;
          });
        }
        return clone;
      };

      const handleCollection = (
        list: Array<Record<string, unknown>>,
        basePath: string,
      ): boolean => {
        if (rec.method === "GET" && rec.pathname === basePath) {
          // Real Discord API v10 only returns full `*_localizations` dictionaries
          // when `with_localizations=true` is explicitly passed on the query string.
          const includeLocalizations =
            rec.query.get("with_localizations") === "true";
          sendJson(
            res,
            200,
            includeLocalizations
              ? list
              : list.map((cmd) => stripLocalizationsForDefaultList(cmd)),
          );
          return true;
        }
        if (rec.method === "POST" && rec.pathname === basePath) {
          const payload = rec.jsonBody as Record<string, unknown>;
          // Simulate real Discord omitting `required: false` on returned options
          const normalizedOptions = Array.isArray(payload.options)
            ? payload.options.map((opt) => {
                const clone = { ...(opt as Record<string, unknown>) };
                if (clone.required === false) {
                  delete clone.required;
                }
                return clone;
              })
            : undefined;
          const created: Record<string, unknown> = {
            id: `discord_cmd_${idSeq++}`,
            application_id: TEST_DISCORD_APP_ID,
            version: String(idSeq),
            ...payload,
            ...(normalizedOptions ? { options: normalizedOptions } : {}),
          };
          list.push(created);
          sendJson(res, 201, created);
          return true;
        }
        if (rec.method === "PATCH" && rec.pathname.startsWith(`${basePath}/`)) {
          const id = decodeURIComponent(
            rec.pathname.slice(`${basePath}/`.length),
          );
          const target = list.find((c) => c.id === id);
          if (!target) {
            sendJson(res, 404, { message: "Unknown Application Command" });
            return true;
          }
          Object.assign(target, rec.jsonBody as Record<string, unknown>);
          sendJson(res, 200, target);
          return true;
        }
        return false;
      };

      if (
        handleCollection(globalCommands, globalBase) ||
        handleCollection(guildCommands, guildBase)
      ) {
        return;
      }

      sendJson(res, 404, { message: "Not Found" });
    });

    // 1. Default preview (global scope, no --apply): zero POST/PATCH
    const previewRes = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      testApiOrigin: srv.origin,
    });
    expect(previewRes.status).toBe("preview");
    expect(previewRes.scopeLabel).toBe("global");
    expect(previewRes.plan.map((p) => [p.name, p.action])).toEqual([
      ["main", "unchanged"],
      ["workspace", "update"],
      ["new", "create"],
      ["stop", "create"],
    ]);
    expect(previewRes.preservedCommands).toEqual([
      { name: "help", remoteId: "cmd_legacy_help", commandType: 1 },
      { name: "main", remoteId: "cmd_user_ctx_main", commandType: 2 },
    ]);
    expect(srv.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands?with_localizations=true`,
    ]);

    // 2. Global `--apply`
    srv.requests.length = 0;
    const applyRes = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(applyRes.ok).toBe(true);
    expect(srv.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands?with_localizations=true`,
      `PATCH /api/v10/applications/${TEST_DISCORD_APP_ID}/commands/cmd_chat_ws`,
      `POST /api/v10/applications/${TEST_DISCORD_APP_ID}/commands`,
      `POST /api/v10/applications/${TEST_DISCORD_APP_ID}/commands`,
    ]);

    // PATCH body updates only changed managed fields, strips static choices, adds BOT_DM context, preserves option localizations
    expect(srv.requests[2]!.jsonBody).toEqual({
      description: "查看可访问的工作区",
      contexts: [...DISCORD_GLOBAL_CONTEXTS],
      options: [
        {
          name: "workspace",
          description: "要切换的工作区名称，留空查看可访问列表",
          type: DISCORD_COMMAND_OPTION_TYPE_STRING,
          required: false,
          autocomplete: true,
          description_localizations: { "en-US": "Workspace name" },
        },
      ],
    });

    // Top-level unmanaged `description_localizations` on `workspace` remained intact on server
    const updatedWs = globalCommands.find((c) => c.id === "cmd_chat_ws")!;
    expect(updatedWs.description_localizations).toEqual({
      "en-US": "List workspaces",
    });

    // Created global commands carry `type: 1`, `integration_types: [0]`, `contexts: [0, 1]`
    expect(srv.requests[3]!.jsonBody).toEqual({
      name: "new",
      type: DISCORD_APPLICATION_COMMAND_TYPE_CHAT_INPUT,
      description: "在当前工作区新建会话",
      integration_types: [...DISCORD_GLOBAL_INTEGRATION_TYPES],
      contexts: [...DISCORD_GLOBAL_CONTEXTS],
    });

    // 3. Re-running global `--apply` is completely idempotent (zero writes)
    srv.requests.length = 0;
    const rerunGlobal = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(rerunGlobal.outcomes?.every((o) => o.status === "unchanged")).toBe(
      true,
    );
    expect(srv.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands?with_localizations=true`,
    ]);

    // 4. Guild-scoped CLI `--apply --guild-id <ID>` hits guild endpoints (including ?with_localizations=true) and omits global-only contexts/integration_types
    srv.requests.length = 0;
    const guildExit = await runNativeCommandsCli(
      [
        "--platform",
        "discord",
        "--guild-id",
        TEST_GUILD_ID,
        "--apply",
      ],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: createBufferWriter(),
        stderr: createBufferWriter(),
        testApiOrigin: srv.origin,
      },
    );
    expect(guildExit).toBe(0);
    expect(srv.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands?with_localizations=true`,
      `POST /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`,
      `POST /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`,
      `POST /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`,
      `POST /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`,
    ]);
    expect(srv.requests[2]!.jsonBody).toEqual({
      name: "main",
      type: 1,
      description: "返回 Chief of Staff",
    });
  });

  it("handles HTTP 429 Retry-After within budget, aborts when Retry-After exceeds total deadline, and rejects duplicate CHAT_INPUT names or 403 permissions", async () => {
    const sleptMs: number[] = [];
    let rateLimitRemaining = 1;
    let mode: "retry_ok" | "retry_too_long" | "forbidden" | "duplicate" =
      "retry_ok";

    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }

      if (mode === "forbidden") {
        sendJson(res, 403, { code: 50001, message: "Missing Access" });
        return;
      }

      if (mode === "retry_too_long") {
        sendJson(
          res,
          429,
          { message: "You are being rate limited.", retry_after: 120 },
          { "Retry-After": "120" },
        );
        return;
      }

      if (mode === "retry_ok" && rateLimitRemaining > 0) {
        rateLimitRemaining -= 1;
        sendJson(
          res,
          429,
          { message: "Rate limited", retry_after: 1.2 },
          { "Retry-After": "1.2" },
        );
        return;
      }

      if (mode === "duplicate") {
        sendJson(res, 200, [
          { id: "1", type: 1, name: "stop", description: "a" },
          { id: "2", type: 1, name: "stop", description: "b" },
        ]);
        return;
      }

      sendJson(res, 200, []);
    });

    const previewAfter429 = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      testApiOrigin: srv.origin,
      sleepFn: async (ms) => {
        sleptMs.push(ms);
      },
    });
    expect(previewAfter429.status).toBe("preview");
    expect(sleptMs).toEqual([1200]);

    mode = "retry_too_long";
    sleptMs.length = 0;
    await expect(
      syncDiscordNativeCommands({
        botToken: TEST_DISCORD_BOT_TOKEN,
        applicationId: TEST_DISCORD_APP_ID,
        totalTimeoutMs: 5_000,
        testApiOrigin: srv.origin,
        sleepFn: async (ms) => {
          sleptMs.push(ms);
        },
      }),
    ).rejects.toMatchObject({ code: "rate_limited" });
    expect(sleptMs).toEqual([]);

    mode = "forbidden";
    await expect(
      syncDiscordNativeCommands({
        botToken: TEST_DISCORD_BOT_TOKEN,
        applicationId: TEST_DISCORD_APP_ID,
        scope: { kind: "guild", guildId: TEST_GUILD_ID },
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "permission_denied" });

    mode = "duplicate";
    await expect(
      syncDiscordNativeCommands({
        botToken: TEST_DISCORD_BOT_TOKEN,
        applicationId: TEST_DISCORD_APP_ID,
        mode: "apply",
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "duplicate_remote_command" });
  });

  it("recovers via live GET when Discord PATCH drops connection after committing on server", async () => {
    const commands: Array<Record<string, unknown>> = [
      {
        id: "cmd_m",
        type: 1,
        name: "main",
        description: "旧说明",
        integration_types: [0],
        contexts: [0, 1],
      },
      {
        id: "cmd_w",
        type: 1,
        name: "workspace",
        description: "查看可访问的工作区",
        integration_types: [0],
        contexts: [0, 1],
        options: [
          {
            name: "workspace",
            description: "要切换的工作区名称，留空查看可访问列表",
            type: 3,
            required: false,
            autocomplete: true,
          },
        ],
      },
      {
        id: "cmd_n",
        type: 1,
        name: "new",
        description: "在当前工作区新建会话",
        integration_types: [0],
        contexts: [0, 1],
      },
      {
        id: "cmd_s",
        type: 1,
        name: "stop",
        description: "停止当前任务",
        integration_types: [0],
        contexts: [0, 1],
      },
    ];

    let patchCalls = 0;
    const srv = await startLocalHttpServer((req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }
      const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, commands);
        return;
      }
      if (rec.method === "PATCH" && rec.pathname === `${base}/cmd_m`) {
        patchCalls += 1;
        Object.assign(commands[0]!, rec.jsonBody as Record<string, unknown>);
        req.socket.destroy();
        return;
      }
      sendJson(res, 404, {});
    });

    const res = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });

    expect(res.ok).toBe(true);
    expect(patchCalls).toBe(1);
    expect(res.outcomes?.[0]).toEqual({
      name: "main",
      status: "updated",
      remoteId: "cmd_m",
      changedFields: ["description"],
      verifiedAfterDisconnect: true,
    });
  });

  it("returns unconfirmed (exit code 4) without duplicate POST when Discord POST drops connection and server remains unchanged, and handles daily limit 30034", async () => {
    let returnDailyQuotaError = false;
    let postCount = 0;

    const srv = await startLocalHttpServer((req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }
      const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, []);
        return;
      }
      if (rec.method === "POST" && rec.pathname === base) {
        postCount += 1;
        if (returnDailyQuotaError) {
          sendJson(res, 429, {
            code: 30034,
            message:
              "Max number of daily application command creates has been reached (200)",
            retry_after: 86400,
          });
          return;
        }
        req.socket.destroy();
      }
    });

    const stderr = createBufferWriter();
    const unconfirmedCode = await runNativeCommandsCli(
      ["--platform", "discord", "--apply"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: createBufferWriter(),
        stderr,
        testApiOrigin: srv.origin,
      },
    );
    expect(unconfirmedCode).toBe(4);
    expect(postCount).toBe(1);
    expect(stderr.text()).toContain("写入结果未确认");

    returnDailyQuotaError = true;
    const quotaResult = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(quotaResult.ok).toBe(false);
    expect(quotaResult.status).toBe("failed");
    expect(quotaResult.errorCode).toBe("daily_create_limit");
    expect(quotaResult.outcomes?.map((o) => [o.name, o.status])).toEqual([
      ["main", "failed"],
      ["workspace", "skipped"],
      ["new", "skipped"],
      ["stop", "skipped"],
    ]);
  });

  it("enforces per-request timeout and bounded maxRequests budget", async () => {
    let hangList = true;
    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }
      if (hangList) {
        // Never finish response so `requestTimeoutMs` aborts in-flight GET
        return;
      }
      sendJson(res, 200, []);
    });

    await expect(
      syncDiscordNativeCommands({
        botToken: TEST_DISCORD_BOT_TOKEN,
        applicationId: TEST_DISCORD_APP_ID,
        requestTimeoutMs: 60,
        totalTimeoutMs: 1_000,
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "timeout" });

    hangList = false;
    const budgetResult = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      maxRequests: 2,
      testApiOrigin: srv.origin,
    });
    expect(budgetResult.ok).toBe(false);
    expect(budgetResult.status).toBe("failed");
    expect(budgetResult.errorCode).toBe("request_limit_exceeded");
    expect(budgetResult.outcomes?.map((o) => [o.name, o.status])).toEqual([
      ["main", "failed"],
      ["workspace", "skipped"],
      ["new", "skipped"],
      ["stop", "skipped"],
    ]);
  });
});

describe("Credential redaction across errors, HTTP payloads, CLI output, and logs", () => {
  it("never leaks Feishu app_secret, opaque tenant_access_token, or Discord bot_token when echoed by upstream error responses", async () => {
    const RAW_FEISHU_SECRET = "feishu_raw_secret_echo_test_445566";
    const RAW_OPAQUE_TENANT_TOKEN = "custom_opaque_tenant_tok_77889900";
    const RAW_DISCORD_TOKEN =
      "MTIzNDU2Nzg5MDEyMzQ1Njc4.XyZaBc.rawDiscordSecretEchoTestValue112233";

    const feishuCapture = createCapturingLogger();
    const feishuSrv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: RAW_OPAQUE_TENANT_TOKEN,
        });
        return;
      }
      // Deliberately echo both secrets in error payload
      sendJson(res, 400, {
        code: 99991663,
        msg: `Rejected secret=${RAW_FEISHU_SECRET} and bearer ${RAW_OPAQUE_TENANT_TOKEN} raw=${RAW_OPAQUE_TENANT_TOKEN}`,
      });
    });

    const feishuStdout = createBufferWriter();
    const feishuStderr = createBufferWriter();
    const feishuCode = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: "cli_redact_test",
          DALTON_FEISHU_APP_SECRET: RAW_FEISHU_SECRET,
        },
        stdout: feishuStdout,
        stderr: feishuStderr,
        logger: feishuCapture.logger,
        testApiOrigin: feishuSrv.origin,
      },
    );

    expect(feishuCode).toBe(3);
    const allFeishuText = [
      feishuStdout.text(),
      feishuStderr.text(),
      ...feishuCapture.lines,
    ].join("\n");
    expect(allFeishuText).not.toContain(RAW_FEISHU_SECRET);
    expect(allFeishuText).not.toContain(RAW_OPAQUE_TENANT_TOKEN);

    const discordCapture = createCapturingLogger();
    const discordSrv = await startLocalHttpServer((_req, res) => {
      // Echo raw bot token in upstream HTTP 401 message
      sendJson(res, 401, {
        message: `401: Unauthorized for Bot ${RAW_DISCORD_TOKEN} (${RAW_DISCORD_TOKEN})`,
      });
    });

    const discordStdout = createBufferWriter();
    const discordStderr = createBufferWriter();
    const discordCode = await runNativeCommandsCli(
      ["--platform", "discord", "--json"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: RAW_DISCORD_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: "123456789012345678",
        },
        stdout: discordStdout,
        stderr: discordStderr,
        logger: discordCapture.logger,
        testApiOrigin: discordSrv.origin,
      },
    );

    expect(discordCode).toBe(3);
    const allDiscordText = [
      discordStdout.text(),
      discordStderr.text(),
      ...discordCapture.lines,
    ].join("\n");
    expect(allDiscordText).not.toContain(RAW_DISCORD_TOKEN);
  });

  it("omits raw duplicate pagination cursors and scrubs opaque dynamic Feishu tenant_access_token from stdout, stderr, logs, and Error objects when returned as page_token", async () => {
    const TEST_APP_SECRET = "feishu_cursor_secret_val_334455";
    // Intentionally opaque (does not start with `t-`) so static token regexes cannot catch it;
    // only cursor omission + dynamic `activeSecrets` redaction can prevent leakage.
    const MALICIOUS_DYN_TOKEN = "opaque_dyn_feishu_tenant_tok_90817263";
    let mode: "duplicate_cursor" | "page2_failure" = "duplicate_cursor";

    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: MALICIOUS_DYN_TOKEN,
        });
        return;
      }

      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        const cursor = rec.query.get("page_token");
        if (mode === "page2_failure" && cursor) {
          sendJson(res, 500, {
            code: 50000,
            msg: `Upstream failure on cursor ${MALICIOUS_DYN_TOKEN} with ${TEST_APP_SECRET}`,
          });
          return;
        }
        sendJson(res, 200, {
          code: 0,
          data: {
            items: [
              {
                command_id: "cmd_1",
                command: "main",
                description: { default_value: "返回 Chief of Staff" },
              },
            ],
            has_more: true,
            page_token: MALICIOUS_DYN_TOKEN,
          },
        });
        return;
      }

      sendJson(res, 404, { code: 404 });
    });

    // 1. Direct library call: inspect the thrown Error object (message, stack, cause, JSON, util.inspect)
    let caughtErr: unknown;
    try {
      await syncFeishuNativeCommands({
        appId: "cli_cursor_redact",
        appSecret: TEST_APP_SECRET,
        mode: "apply",
        testApiOrigin: srv.origin,
      });
    } catch (err) {
      caughtErr = err;
    }
    expect(caughtErr).toBeInstanceOf(NativeCommandRegistrationError);
    expect((caughtErr as NativeCommandRegistrationError).code).toBe(
      "pagination_error",
    );
    const serializedError = [
      (caughtErr as Error).message,
      (caughtErr as Error).stack ?? "",
      String((caughtErr as { cause?: unknown }).cause ?? ""),
      JSON.stringify(caughtErr),
      inspect(caughtErr, { depth: 6 }),
    ].join("\n");
    expect(serializedError).not.toContain(MALICIOUS_DYN_TOKEN);
    expect(serializedError).not.toContain(TEST_APP_SECRET);

    // 2. CLI call (`--json` and text) on duplicate cursor
    const capture = createCapturingLogger();
    const cliOut = createBufferWriter();
    const cliErr = createBufferWriter();
    const dupExit = await runNativeCommandsCli(
      ["--platform", "feishu", "--json"],
      {
        env: {
          DALTON_FEISHU_APP_ID: "cli_cursor_redact",
          DALTON_FEISHU_APP_SECRET: TEST_APP_SECRET,
        },
        stdout: cliOut,
        stderr: cliErr,
        logger: capture.logger,
        testApiOrigin: srv.origin,
      },
    );
    expect(dupExit).toBe(1);
    const combinedDupOutput = [
      cliOut.text(),
      cliErr.text(),
      ...capture.lines,
    ].join("\n");
    expect(combinedDupOutput).not.toContain(MALICIOUS_DYN_TOKEN);
    expect(combinedDupOutput).not.toContain(TEST_APP_SECRET);

    // 3. Second-page HTTP 500 failure where `?page_token=<MALICIOUS_DYN_TOKEN>` was in the request path
    mode = "page2_failure";
    let page2Err: unknown;
    try {
      await syncFeishuNativeCommands({
        appId: "cli_cursor_redact",
        appSecret: TEST_APP_SECRET,
        testApiOrigin: srv.origin,
      });
    } catch (err) {
      page2Err = err;
    }
    expect(page2Err).toBeInstanceOf(NativeCommandRegistrationError);
    const serializedPage2Err = [
      (page2Err as Error).message,
      (page2Err as Error).stack ?? "",
      inspect(page2Err, { depth: 6 }),
    ].join("\n");
    expect(serializedPage2Err).not.toContain(MALICIOUS_DYN_TOKEN);
    expect(serializedPage2Err).not.toContain(TEST_APP_SECRET);
  });
});

describe("Localization query preservation, PATCH verification, partial-apply outcomes, and CLI stream isolation", () => {
  const TEST_DISCORD_APP_ID = "123456789012345678";
  const TEST_GUILD_ID = "987654321098765432";
  const TEST_DISCORD_BOT_TOKEN =
    "MTIzNDU2Nzg5MDEyMzQ1Njc4.GhIjKl.secretDiscordBotTokenValue987654321";
  const TEST_FEISHU_APP_ID = "cli_dalton_r2_app";
  const TEST_FEISHU_SECRET = "feishu_r2_secret_value_654321";
  const TEST_FEISHU_DYN_TOKEN = "opaque_r2_tenant_access_token_112233";

  it("passes with_localizations=true on every paginated Discord GET (global and guild) so unmanaged option localizations survive PATCH", async () => {
    const makeWorkspaceEntry = (id: string) => ({
      id,
      type: 1,
      application_id: TEST_DISCORD_APP_ID,
      name: "workspace",
      description: "旧版说明",
      name_localizations: { "en-US": "workspace" },
      description_localizations: { "en-US": "Browse workspaces" },
      integration_types: [0],
      contexts: [0, 1],
      options: [
        {
          name: "workspace",
          type: 3,
          description: "旧参数说明",
          required: false,
          autocomplete: true,
          name_localizations: { "en-US": "target", ja: "ワークスペース" },
          description_localizations: {
            "en-US": "Target workspace name",
            ja: "切り替えるワークスペース名",
          },
        },
      ],
    });

    const page1Global: Array<Record<string, unknown>> = [
      {
        id: "g_main",
        type: 1,
        name: "main",
        description: "返回 Chief of Staff",
        integration_types: [0],
        contexts: [0, 1],
      },
      {
        id: "g_new",
        type: 1,
        name: "new",
        description: "在当前工作区新建会话",
        integration_types: [0],
        contexts: [0, 1],
      },
    ];
    const page2Global: Array<Record<string, unknown>> = [
      makeWorkspaceEntry("g_ws"),
      {
        id: "g_stop",
        type: 1,
        name: "stop",
        description: "停止当前任务",
        integration_types: [0],
        contexts: [0, 1],
      },
    ];
    const guildPage1: Array<Record<string, unknown>> = [
      {
        id: "guild_main",
        type: 1,
        name: "main",
        description: "返回 Chief of Staff",
      },
      {
        id: "guild_new",
        type: 1,
        name: "new",
        description: "在当前工作区新建会话",
      },
    ];
    const guildPage2: Array<Record<string, unknown>> = [
      {
        ...makeWorkspaceEntry("guild_ws"),
        integration_types: undefined,
        contexts: undefined,
      },
      {
        id: "guild_stop",
        type: 1,
        name: "stop",
        description: "停止当前任务",
      },
    ];

    const stripIfNoFlag = (
      items: Array<Record<string, unknown>>,
      withLoc: boolean,
    ) =>
      items.map((cmd) => {
        if (withLoc) {
          return cmd;
        }
        const copy: Record<string, unknown> = { ...cmd };
        delete copy.name_localizations;
        delete copy.description_localizations;
        if (Array.isArray(copy.options)) {
          copy.options = copy.options.map((o) => {
            const oc = { ...(o as Record<string, unknown>) };
            delete oc.name_localizations;
            delete oc.description_localizations;
            return oc;
          });
        }
        return copy;
      });

    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }

      const globalBase = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      const guildBase = `/api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`;

      for (const [base, p1, p2] of [
        [globalBase, page1Global, page2Global],
        [guildBase, guildPage1, guildPage2],
      ] as const) {
        if (rec.method === "GET" && rec.pathname === base) {
          const withLoc = rec.query.get("with_localizations") === "true";
          const cursor = rec.query.get("page_token");
          if (!cursor) {
            sendJson(res, 200, {
              items: stripIfNoFlag(p1, withLoc),
              has_more: true,
              page_token: "next_page_2",
            });
            return;
          }
          if (cursor === "next_page_2") {
            sendJson(res, 200, {
              items: stripIfNoFlag(p2, withLoc),
              has_more: false,
            });
            return;
          }
        }
        if (rec.method === "PATCH" && rec.pathname.startsWith(`${base}/`)) {
          const id = decodeURIComponent(rec.pathname.slice(`${base}/`.length));
          const found = [...p1, ...p2].find((c) => c.id === id);
          if (!found) {
            sendJson(res, 404, { message: "Not Found" });
            return;
          }
          Object.assign(found, rec.jsonBody as Record<string, unknown>);
          sendJson(res, 200, found);
          return;
        }
      }

      sendJson(res, 404, { message: "Unexpected route" });
    });

    const globalApply = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(globalApply.ok).toBe(true);
    expect(srv.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands?with_localizations=true`,
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands?with_localizations=true&page_token=next_page_2`,
      `PATCH /api/v10/applications/${TEST_DISCORD_APP_ID}/commands/g_ws`,
    ]);
    expect(srv.requests[3]!.jsonBody).toEqual({
      description: "查看可访问的工作区",
      options: [
        {
          name: "workspace",
          description: "要切换的工作区名称，留空查看可访问列表",
          type: 3,
          required: false,
          autocomplete: true,
          name_localizations: { "en-US": "target", ja: "ワークスペース" },
          description_localizations: {
            "en-US": "Target workspace name",
            ja: "切り替えるワークスペース名",
          },
        },
      ],
    });

    srv.requests.length = 0;
    const guildApply = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      scope: { kind: "guild", guildId: TEST_GUILD_ID },
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(guildApply.ok).toBe(true);
    expect(srv.requests.map((r) => `${r.method} ${r.url}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands?with_localizations=true`,
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands?with_localizations=true&page_token=next_page_2`,
      `PATCH /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands/guild_ws`,
    ]);
    expect(srv.requests[3]!.jsonBody).toMatchObject({
      options: [
        expect.objectContaining({
          name_localizations: { "en-US": "target", ja: "ワークスペース" },
          description_localizations: {
            "en-US": "Target workspace name",
            ja: "切り替えるワークスペース名",
          },
        }),
      ],
    });
  });

  it("verifies live state when Discord PATCH returns 200 {} and supports valid empty-data Feishu PATCH while verifying malformed Feishu PATCH without blind POSTs", async () => {
    // --- Part A: Discord PATCH returning `200 {}` ---
    let discordApplyMutationOnServer = false;
    let discordPostCount = 0;
    const makeDiscordStore = (): Array<Record<string, unknown>> => [
      {
        id: "dc_m",
        type: 1,
        name: "main",
        description: "旧描述",
        integration_types: [0],
        contexts: [0, 1],
      },
      {
        id: "dc_w",
        type: 1,
        name: "workspace",
        description: "查看可访问的工作区",
        integration_types: [0],
        contexts: [0, 1],
        options: [
          {
            name: "workspace",
            description: "要切换的工作区名称，留空查看可访问列表",
            type: 3,
            required: false,
            autocomplete: true,
          },
        ],
      },
      {
        id: "dc_n",
        type: 1,
        name: "new",
        description: "在当前工作区新建会话",
        integration_types: [0],
        contexts: [0, 1],
      },
      {
        id: "dc_s",
        type: 1,
        name: "stop",
        description: "停止当前任务",
        integration_types: [0],
        contexts: [0, 1],
      },
    ];
    let discordStore = makeDiscordStore();

    const discordSrv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }
      const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, discordStore);
        return;
      }
      if (rec.method === "POST") {
        discordPostCount += 1;
        sendJson(res, 500, {});
        return;
      }
      if (rec.method === "PATCH" && rec.pathname === `${base}/dc_m`) {
        if (discordApplyMutationOnServer) {
          Object.assign(
            discordStore[0]!,
            rec.jsonBody as Record<string, unknown>,
          );
        }
        // Return HTTP 200 with empty `{}` (missing `id` and command fields)
        sendJson(res, 200, {});
        return;
      }
      sendJson(res, 404, {});
    });

    // Subcase A1: Server returned `200 {}` WITHOUT updating remote command -> must read back via GET and return unconfirmed (exit 4)
    const staleEmptyPatchExit = await runNativeCommandsCli(
      ["--platform", "discord", "--apply"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: createBufferWriter(),
        stderr: createBufferWriter(),
        testApiOrigin: discordSrv.origin,
      },
    );
    expect(staleEmptyPatchExit).toBe(4);
    expect(discordPostCount).toBe(0);
    // Initial GET + verification GET both requested `with_localizations=true`
    expect(
      discordSrv.requests.filter((r) => r.method === "GET" && r.pathname.endsWith("/commands")),
    ).toHaveLength(2);

    // Subcase A2: Server DID update remote command before returning `200 {}` -> live GET confirms `updated`
    discordStore = makeDiscordStore();
    discordApplyMutationOnServer = true;
    const confirmedEmptyPatch = await syncDiscordNativeCommands({
      botToken: TEST_DISCORD_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: discordSrv.origin,
    });
    expect(confirmedEmptyPatch.ok).toBe(true);
    expect(confirmedEmptyPatch.outcomes?.[0]).toEqual({
      name: "main",
      status: "updated",
      remoteId: "dc_m",
      changedFields: ["description"],
      verifiedAfterDisconnect: true,
    });
    expect(discordPostCount).toBe(0);

    // --- Part B: Feishu PATCH response structures (`larksuite/cli` `CallAPITyped` contract) ---
    let feishuPatchMode:
      | "code0_nil_data"
      | "code0_empty_data"
      | "missing_code_stale"
      | "missing_code_committed" = "code0_nil_data";
    let feishuPostCount = 0;

    const makeFeishuStore = () => [
      {
        command_id: "fs_m",
        command: "main",
        description: { default_value: "旧主菜单描述" },
      },
      {
        command_id: "fs_w",
        command: "workspace",
        description: { default_value: "查看可访问的工作区" },
      },
      {
        command_id: "fs_n",
        command: "new",
        description: { default_value: "在当前工作区新建会话" },
      },
      {
        command_id: "fs_s",
        command: "stop",
        description: { default_value: "停止当前任务" },
      },
    ];
    let feishuStore = makeFeishuStore();

    const feishuSrv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_FEISHU_DYN_TOKEN,
        });
        return;
      }
      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        sendJson(res, 200, {
          code: 0,
          data: { items: feishuStore, has_more: false },
        });
        return;
      }
      if (rec.method === "POST") {
        feishuPostCount += 1;
        sendJson(res, 500, { code: 500 });
        return;
      }
      if (
        rec.method === "PATCH" &&
        rec.pathname === `${FEISHU_SLASH_COMMAND_BASE_PATH}/fs_m`
      ) {
        const body = rec.jsonBody as {
          description: { default_value: string };
        };
        if (feishuPatchMode === "code0_nil_data") {
          feishuStore[0]!.description = body.description;
          sendJson(res, 200, { code: 0, msg: "success", data: null });
          return;
        }
        if (feishuPatchMode === "code0_empty_data") {
          feishuStore[0]!.description = body.description;
          sendJson(res, 200, { code: 0, msg: "success", data: {} });
          return;
        }
        if (feishuPatchMode === "missing_code_committed") {
          feishuStore[0]!.description = body.description;
          sendJson(res, 200, {});
          return;
        }
        // `missing_code_stale`: HTTP 200 `{}` without `code` and without mutating server state
        sendJson(res, 200, {});
        return;
      }
      sendJson(res, 404, { code: 404 });
    });

    // B1: `{ code: 0, msg: "success", data: null }` succeeds directly without extra GET
    feishuSrv.requests.length = 0;
    const nilDataRes = await syncFeishuNativeCommands({
      appId: TEST_FEISHU_APP_ID,
      appSecret: TEST_FEISHU_SECRET,
      mode: "apply",
      testApiOrigin: feishuSrv.origin,
    });
    expect(nilDataRes.ok).toBe(true);
    expect(nilDataRes.outcomes?.[0]).toEqual({
      name: "main",
      status: "updated",
      remoteId: "fs_m",
      changedFields: ["description.default_value"],
    });
    expect(
      feishuSrv.requests.filter((r) => r.method === "GET"),
    ).toHaveLength(1);

    // B2: `{ code: 0, msg: "success", data: {} }` also succeeds directly without requiring `command_id` in PATCH response
    feishuStore = makeFeishuStore();
    feishuPatchMode = "code0_empty_data";
    feishuSrv.requests.length = 0;
    const emptyDataRes = await syncFeishuNativeCommands({
      appId: TEST_FEISHU_APP_ID,
      appSecret: TEST_FEISHU_SECRET,
      mode: "apply",
      testApiOrigin: feishuSrv.origin,
    });
    expect(emptyDataRes.ok).toBe(true);
    expect(emptyDataRes.outcomes?.[0]?.status).toBe("updated");
    expect(
      feishuSrv.requests.filter((r) => r.method === "GET"),
    ).toHaveLength(1);

    // B3: `200 {}` (missing `code`) when server is still stale -> reads back via GET and returns unconfirmed (exit 4)
    feishuStore = makeFeishuStore();
    feishuPatchMode = "missing_code_stale";
    const staleFeishuExit = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_FEISHU_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_FEISHU_SECRET,
        },
        stdout: createBufferWriter(),
        stderr: createBufferWriter(),
        testApiOrigin: feishuSrv.origin,
      },
    );
    expect(staleFeishuExit).toBe(4);
    expect(feishuPostCount).toBe(0);

    // B4: `200 {}` (missing `code`) when server DID commit -> live GET confirms `updated`
    feishuStore = makeFeishuStore();
    feishuPatchMode = "missing_code_committed";
    const committedFeishuRes = await syncFeishuNativeCommands({
      appId: TEST_FEISHU_APP_ID,
      appSecret: TEST_FEISHU_SECRET,
      mode: "apply",
      testApiOrigin: feishuSrv.origin,
    });
    expect(committedFeishuRes.ok).toBe(true);
    expect(committedFeishuRes.outcomes?.[0]).toEqual({
      name: "main",
      status: "updated",
      remoteId: "fs_m",
      changedFields: ["description.default_value"],
      verifiedAfterDisconnect: true,
    });
    expect(feishuPostCount).toBe(0);
  });

  it("preserves previously completed outcomes, marks failed and skipped commands clearly, redacts secrets, and exits non-zero on mid-apply explicit errors", async () => {
    // 1. Feishu: `main` created, `workspace` fails with 403 permission error echoing secrets -> `new` & `stop` skipped, exit 3
    const feishuCapture = createCapturingLogger();
    const feishuSrv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_FEISHU_DYN_TOKEN,
        });
        return;
      }
      if (rec.method === "GET" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        sendJson(res, 200, { code: 0, data: { items: [], has_more: false } });
        return;
      }
      if (rec.method === "POST" && rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH) {
        const body = rec.jsonBody as { command: string };
        if (body.command === "main") {
          sendJson(res, 200, {
            code: 0,
            data: {
              command_id: "cmd_main_ok",
              command: "main",
              description: { default_value: "返回 Chief of Staff" },
            },
          });
          return;
        }
        sendJson(res, 403, {
          code: 99991672,
          msg: `Missing application:app_slash_command:write for ${TEST_FEISHU_SECRET} / ${TEST_FEISHU_DYN_TOKEN}`,
        });
      }
    });

    const fsTextOut = createBufferWriter();
    const fsTextErr = createBufferWriter();
    const fsExit = await runNativeCommandsCli(
      ["--platform", "feishu", "--apply"],
      {
        env: {
          DALTON_FEISHU_APP_ID: TEST_FEISHU_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_FEISHU_SECRET,
        },
        stdout: fsTextOut,
        stderr: fsTextErr,
        logger: feishuCapture.logger,
        testApiOrigin: feishuSrv.origin,
      },
    );

    expect(fsExit).toBe(3);
    expect(fsTextOut.text()).toContain("[已创建] /main (id: cmd_main_ok)");
    expect(fsTextOut.text()).toContain("[失败] /workspace");
    expect(fsTextOut.text()).toContain("[已跳过] /new");
    expect(fsTextOut.text()).toContain("[已跳过] /stop");
    expect(fsTextOut.text()).toContain("5 分钟");

    const allFsLeaksCheck = [
      fsTextOut.text(),
      fsTextErr.text(),
      ...feishuCapture.lines,
    ].join("\n");
    expect(allFsLeaksCheck).not.toContain(TEST_FEISHU_SECRET);
    expect(allFsLeaksCheck).not.toContain(TEST_FEISHU_DYN_TOKEN);

    // 2. Discord: `main` unchanged, `workspace` created, `new` fails with daily quota 30034 (and later `maxRequests` budget), `stop` skipped -> exit 1
    let failMode: "quota_30034" | "budget" = "quota_30034";
    const discordSrv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }
      const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, [
          {
            id: "dc_existing_main",
            type: 1,
            name: "main",
            description: "返回 Chief of Staff",
            integration_types: [0],
            contexts: [0, 1],
          },
        ]);
        return;
      }
      if (rec.method === "POST" && rec.pathname === base) {
        const body = rec.jsonBody as Record<string, unknown>;
        if (body.name === "workspace") {
          sendJson(res, 201, {
            id: "dc_created_ws",
            ...body,
          });
          return;
        }
        if (failMode === "quota_30034") {
          sendJson(res, 429, {
            code: 30034,
            message: `Daily command create quota hit for ${TEST_DISCORD_BOT_TOKEN}`,
            retry_after: 86400,
          });
          return;
        }
        sendJson(res, 201, { id: "unexpected", ...body });
      }
    });

    const dcJsonOut = createBufferWriter();
    const dcJsonErr = createBufferWriter();
    const dcQuotaExit = await runNativeCommandsCli(
      ["--platform", "discord", "--apply", "--json"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: dcJsonOut,
        stderr: dcJsonErr,
        testApiOrigin: discordSrv.origin,
      },
    );
    expect(dcQuotaExit).toBe(1);
    const parsedDcQuota = JSON.parse(dcJsonOut.text()) as {
      ok: boolean;
      status: string;
      errorCode: string;
      outcomes: Array<{
        name: string;
        status: string;
        remoteId?: string;
        errorCode?: string;
      }>;
    };
    expect(parsedDcQuota.ok).toBe(false);
    expect(parsedDcQuota.status).toBe("failed");
    expect(parsedDcQuota.errorCode).toBe("daily_create_limit");
    expect(parsedDcQuota.outcomes.map((o) => [o.name, o.status, o.remoteId])).toEqual([
      ["main", "unchanged", "dc_existing_main"],
      ["workspace", "created", "dc_created_ws"],
      ["new", "failed", undefined],
      ["stop", "skipped", undefined],
    ]);
    expect(dcJsonOut.text()).not.toContain(TEST_DISCORD_BOT_TOKEN);
    expect(dcJsonErr.text()).not.toContain(TEST_DISCORD_BOT_TOKEN);

    // Also verify `maxRequests: 3` (`@me` + `GET` + `POST workspace` = 3, then `POST new` hits budget limit)
    failMode = "budget";
    const dcBudgetOut = createBufferWriter();
    const dcBudgetExit = await runNativeCommandsCli(
      ["--platform", "discord", "--apply"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: dcBudgetOut,
        stderr: createBufferWriter(),
        maxRequests: 3,
        testApiOrigin: discordSrv.origin,
      },
    );
    expect(dcBudgetExit).toBe(1);
    expect(dcBudgetOut.text()).toContain("[无需修改] /main (id: dc_existing_main)");
    expect(dcBudgetOut.text()).toContain("[已创建] /workspace (id: dc_created_ws)");
    expect(dcBudgetOut.text()).toContain("[失败] /new");
    expect(dcBudgetOut.text()).toContain("[已跳过] /stop");
  });

  it("writes default CLI logs to stderr so --json stdout is strictly a single parseable JSON document (both in-process and in a real Node child process)", async () => {
    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: TEST_FEISHU_DYN_TOKEN,
        });
        return;
      }
      if (
        rec.method === "GET" &&
        rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH
      ) {
        sendJson(res, 200, {
          code: 0,
          data: { items: [], has_more: false },
        });
        return;
      }
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, { id: TEST_DISCORD_APP_ID, name: "Dalton" });
        return;
      }
      const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, []);
        return;
      }
      if (rec.method === "POST" && rec.pathname === base) {
        const body = rec.jsonBody as Record<string, unknown>;
        sendJson(res, 201, {
          id: `id_${String(body.name)}`,
          ...body,
        });
        return;
      }
      sendJson(res, 404, {});
    });

    // 1. In-process call WITHOUT injecting `logger`, `stdout`, or `stderr`:
    //    intercept `process.stdout.write`, `process.stderr.write`, and `console.log/info/debug`.
    const realStdoutChunks: string[] = [];
    const realStderrChunks: string[] = [];
    const stdoutSpy = vi
      .spyOn(process.stdout, "write")
      .mockImplementation((chunk: string | Uint8Array) => {
        realStdoutChunks.push(
          typeof chunk === "string"
            ? chunk
            : Buffer.from(chunk).toString("utf8"),
        );
        return true;
      });
    const stderrSpy = vi
      .spyOn(process.stderr, "write")
      .mockImplementation((chunk: string | Uint8Array) => {
        realStderrChunks.push(
          typeof chunk === "string"
            ? chunk
            : Buffer.from(chunk).toString("utf8"),
        );
        return true;
      });
    const consoleLogSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    const consoleInfoSpy = vi
      .spyOn(console, "info")
      .mockImplementation(() => {});
    const consoleDebugSpy = vi
      .spyOn(console, "debug")
      .mockImplementation(() => {});

    try {
      const inProcCode = await runNativeCommandsCli(
        ["--platform", "discord", "--json"],
        {
          env: {
            DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
            DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
          },
          testApiOrigin: srv.origin,
        },
      );
      expect(inProcCode).toBe(0);
      expect(consoleLogSpy).not.toHaveBeenCalled();
      expect(consoleInfoSpy).not.toHaveBeenCalled();
      expect(consoleDebugSpy).not.toHaveBeenCalled();

      const rawStdout = realStdoutChunks.join("");
      const rawStderr = realStderrChunks.join("");
      const parsedInProc = JSON.parse(rawStdout) as {
        ok: boolean;
        status: string;
      };
      expect(parsedInProc.ok).toBe(true);
      expect(parsedInProc.status).toBe("preview");
      expect(rawStdout).not.toContain("[native-commands");
      expect(rawStdout).not.toContain(TEST_DISCORD_BOT_TOKEN);
      expect(rawStderr).toContain("[native-commands/discord]");
      expect(rawStderr).not.toContain(TEST_DISCORD_BOT_TOKEN);
    } finally {
      stdoutSpy.mockRestore();
      stderrSpy.mockRestore();
      consoleLogSpy.mockRestore();
      consoleInfoSpy.mockRestore();
      consoleDebugSpy.mockRestore();
    }

    // 2. Real bundled CLI artifact (`esbuild` -> temp `.mjs`):
    //    verify side-effect-free `import()` AND real OS child-process `stdout`/`stderr` pipes.
    const here = dirname(fileURLToPath(import.meta.url));
    const tempDir = mkdtempSync(join(tmpdir(), "dalton-native-cli-test-"));
    const bundlePath = join(tempDir, "native-commands.mjs");

    const runNodeChild = (
      args: readonly string[],
      env: NodeJS.ProcessEnv,
    ): Promise<{ exitCode: number; stdout: string; stderr: string }> =>
      new Promise((resolveRun, rejectRun) => {
        const child = spawn(process.execPath, [...args], {
          env,
          stdio: ["ignore", "pipe", "pipe"],
        });
        const outBufs: Buffer[] = [];
        const errBufs: Buffer[] = [];
        child.stdout.on("data", (chunk: Buffer) => outBufs.push(chunk));
        child.stderr.on("data", (chunk: Buffer) => errBufs.push(chunk));
        child.on("error", rejectRun);
        child.on("close", (code) => {
          resolveRun({
            exitCode: code ?? 1,
            stdout: Buffer.concat(outBufs).toString("utf8"),
            stderr: Buffer.concat(errBufs).toString("utf8"),
          });
        });
      });

    try {
      await build({
        entryPoints: [resolve(here, "../src/native-commands/cli.ts")],
        outfile: bundlePath,
        bundle: true,
        platform: "node",
        target: "node22",
        format: "esm",
        banner: { js: "#!/usr/bin/env node" },
        logLevel: "silent",
      });

      // Importing the compiled CLI bundle must never perform network I/O or run the CLI
      const origFetch = globalThis.fetch;
      let fetchTriggeredOnImport = false;
      globalThis.fetch = (async () => {
        fetchTriggeredOnImport = true;
        throw new Error("Network call during module import");
      }) as typeof fetch;
      try {
        await import(pathToFileURL(bundlePath).href);
      } finally {
        globalThis.fetch = origFetch;
      }
      expect(fetchTriggeredOnImport).toBe(false);

      // Direct execution of compiled CLI binary (`--json` error path without credentials)
      const directMissingCreds = await runNodeChild(
        [bundlePath, "--platform", "discord", "--json"],
        { PATH: process.env.PATH ?? "" },
      );
      expect(directMissingCreds.exitCode).toBe(3);
      expect(directMissingCreds.stdout).not.toContain("[native-commands");
      expect(JSON.parse(directMissingCreds.stdout)).toMatchObject({
        ok: false,
        status: "error",
        code: "missing_config",
      });

      // Real child process running Discord `--apply --json` with NO logger/stdout/stderr overrides
      const childApply = await runNodeChild(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'discord', '--apply', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        {
          PATH: process.env.PATH ?? "",
          CLI_BUNDLE_URL: pathToFileURL(bundlePath).href,
          TEST_ORIGIN: srv.origin,
          DALTON_DISCORD_BOT_TOKEN: TEST_DISCORD_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
      );
      expect(childApply.exitCode).toBe(0);
      const parsedChildJson = JSON.parse(childApply.stdout) as {
        ok: boolean;
        status: string;
        outcomes: Array<{ status: string }>;
      };
      expect(parsedChildJson.ok).toBe(true);
      expect(parsedChildJson.status).toBe("applied");
      expect(parsedChildJson.outcomes).toHaveLength(4);
      expect(childApply.stderr).toContain("[native-commands/discord]");
      expect(childApply.stderr).not.toContain(TEST_DISCORD_BOT_TOKEN);
      expect(childApply.stdout).not.toContain("[native-commands");
      expect(childApply.stdout).not.toContain(TEST_DISCORD_BOT_TOKEN);

      // Real child process running Feishu `--json` preview with NO logger/stdout/stderr overrides
      const childFeishuPreview = await runNodeChild(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'feishu', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        {
          PATH: process.env.PATH ?? "",
          CLI_BUNDLE_URL: pathToFileURL(bundlePath).href,
          TEST_ORIGIN: srv.origin,
          DALTON_FEISHU_APP_ID: TEST_FEISHU_APP_ID,
          DALTON_FEISHU_APP_SECRET: TEST_FEISHU_SECRET,
        },
      );
      expect(childFeishuPreview.exitCode).toBe(0);
      const parsedFeishuChild = JSON.parse(childFeishuPreview.stdout) as {
        ok: boolean;
        status: string;
        platform: string;
      };
      expect(parsedFeishuChild.ok).toBe(true);
      expect(parsedFeishuChild.status).toBe("preview");
      expect(parsedFeishuChild.platform).toBe("feishu");
      expect(childFeishuPreview.stderr).toContain("[native-commands/feishu]");
      expect(childFeishuPreview.stderr).not.toContain(TEST_FEISHU_SECRET);
      expect(childFeishuPreview.stderr).not.toContain(TEST_FEISHU_DYN_TOKEN);
      expect(childFeishuPreview.stdout).not.toContain("[native-commands");
      expect(childFeishuPreview.stdout).not.toContain(TEST_FEISHU_SECRET);
      expect(childFeishuPreview.stdout).not.toContain(TEST_FEISHU_DYN_TOKEN);
    } finally {
      rmSync(tempDir, { recursive: true, force: true });
    }
  });
});

describe("Structured result sanitization, JSON integrity, and dynamic token scrubbing in outbound results", () => {
  const TEST_DISCORD_APP_ID = "123456789012345678";

  function spawnNodeCli(
    args: readonly string[],
    env: NodeJS.ProcessEnv,
  ): Promise<{ exitCode: number; stdout: string; stderr: string }> {
    return new Promise((resolveRun, rejectRun) => {
      const child = spawn(process.execPath, [...args], {
        env,
        stdio: ["ignore", "pipe", "pipe"],
      });
      const outBufs: Buffer[] = [];
      const errBufs: Buffer[] = [];
      child.stdout.on("data", (chunk: Buffer) => outBufs.push(chunk));
      child.stderr.on("data", (chunk: Buffer) => errBufs.push(chunk));
      child.on("error", rejectRun);
      child.on("close", (code) => {
        resolveRun({
          exitCode: code ?? 1,
          stdout: Buffer.concat(outBufs).toString("utf8"),
          stderr: Buffer.concat(errBufs).toString("utf8"),
        });
      });
    });
  }

  it("preserves valid JSON in a real CLI child process when applicationName has quotes/backslashes/token=\"demo\" and output exceeds 65,536 chars", async () => {
    const OPAQUE_DISCORD_SECRET = "custom_opaque_discord_bot_secret_r4_998877";
    const TRICKY_APP_NAME = 'Dalton "Pro" \\ Ops token="demo"';
    const LONG_UNMANAGED_PREFIX = `ext_"quote"\\slash_${"A".repeat(4950)}`;
    const LONG_UNMANAGED_RAW = `${LONG_UNMANAGED_PREFIX}_${OPAQUE_DISCORD_SECRET}`;

    const preservedRemoteItems = Array.from({ length: 900 }, (_, idx) => {
      if (idx === 0) {
        return {
          id: `id_long_${OPAQUE_DISCORD_SECRET}`,
          type: 1,
          name: LONG_UNMANAGED_RAW,
          description: "Long unmanaged command",
        };
      }
      return {
        id: `id_legacy_${String(idx).padStart(4, "0")}`,
        type: 1,
        name: `legacy_custom_command_${String(idx).padStart(4, "0")}`,
        description: "Legacy command",
      };
    });

    const srv = await startLocalHttpServer((_req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, {
          id: TEST_DISCORD_APP_ID,
          name: TRICKY_APP_NAME,
        });
        return;
      }
      const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, [
          {
            id: "id_ws_existing",
            type: 1,
            name: "workspace",
            description: "旧版工作区说明",
            integration_types: [0],
            contexts: [0, 1],
            options: [
              {
                name: "workspace",
                type: 3,
                description: "旧版工作区参数说明",
                required: false,
                autocomplete: true,
                description_localizations: {
                  "en-US": `Target "ws" \\ path token="demo" ${OPAQUE_DISCORD_SECRET}`,
                },
              },
            ],
          },
          ...preservedRemoteItems,
        ]);
        return;
      }
      sendJson(res, 404, {});
    });

    const here = dirname(fileURLToPath(import.meta.url));
    const tempDir = mkdtempSync(join(tmpdir(), "dalton-native-cli-r4-"));
    const bundlePath = join(tempDir, "native-commands.mjs");

    try {
      await build({
        entryPoints: [resolve(here, "../src/native-commands/cli.ts")],
        outfile: bundlePath,
        bundle: true,
        platform: "node",
        target: "node22",
        format: "esm",
        banner: { js: "#!/usr/bin/env node" },
        logLevel: "silent",
      });

      const childJson = await spawnNodeCli(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'discord', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        {
          PATH: process.env.PATH ?? "",
          CLI_BUNDLE_URL: pathToFileURL(bundlePath).href,
          TEST_ORIGIN: srv.origin,
          DALTON_DISCORD_BOT_TOKEN: OPAQUE_DISCORD_SECRET,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
      );

      expect(childJson.exitCode).toBe(0);
      expect(childJson.stdout.length).toBeGreaterThan(65_536);
      expect(childJson.stdout).not.toContain(OPAQUE_DISCORD_SECRET);
      expect(childJson.stderr).not.toContain(OPAQUE_DISCORD_SECRET);

      const parsed = JSON.parse(childJson.stdout) as {
        ok: boolean;
        status: string;
        applicationName: string;
        plan: Array<{
          name: string;
          requestBody?: {
            options?: Array<{
              description_localizations?: Record<string, string>;
            }>;
          };
        }>;
        preservedCommands: Array<{ name: string; remoteId?: string }>;
      };

      expect(parsed.ok).toBe(true);
      expect(parsed.status).toBe("preview");
      expect(parsed.applicationName).toBe(TRICKY_APP_NAME);
      expect(parsed.preservedCommands).toHaveLength(900);
      expect(parsed.preservedCommands[899]?.name).toBe(
        "legacy_custom_command_0899",
      );
      expect(parsed.preservedCommands[0]?.remoteId).toBe("id_long_[REDACTED]");
      // Individual >4096-char free-text leaf is bounded sensibly (~4096 chars), never sliced to 128 chars
      expect(parsed.preservedCommands[0]!.name.length).toBeGreaterThan(4_000);
      expect(
        parsed.preservedCommands[0]!.name.startsWith('ext_"quote"\\slash_'),
      ).toBe(true);

      // Deep nested object structure (> depth 4) remains intact and scrubbed
      const wsPlan = parsed.plan.find((p) => p.name === "workspace")!;
      expect(
        wsPlan.requestBody?.options?.[0]?.description_localizations?.["en-US"],
      ).toBe('Target "ws" \\ path token="demo" [REDACTED]');

      // Large text output (>65,536 chars when formatted) also stays untruncated and secret-free
      const childText = await spawnNodeCli(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'discord'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        {
          PATH: process.env.PATH ?? "",
          CLI_BUNDLE_URL: pathToFileURL(bundlePath).href,
          TEST_ORIGIN: srv.origin,
          DALTON_DISCORD_BOT_TOKEN: OPAQUE_DISCORD_SECRET,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
      );
      expect(childText.exitCode).toBe(0);
      expect(childText.stdout).toContain(TRICKY_APP_NAME);
      expect(childText.stdout).toContain("legacy_custom_command_0899");
      expect(childText.stdout).not.toContain(OPAQUE_DISCORD_SECRET);
      expect(childText.stderr).not.toContain(OPAQUE_DISCORD_SECRET);
    } finally {
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it("scrubs dynamic Feishu tenant_access_token from command_id and public fields across preview, applied, failed, and unconfirmed results while sending real IDs on internal PATCH requests", async () => {
    const FEISHU_APP_ID = "cli_dalton_r4_feishu";
    const FEISHU_SECRET = "feishu_env_secret_r4_112233";
    // Opaque token without `t-` prefix: CLI `envSecrets` cannot know this value;
    // only the registrar's `activeSecrets` knows it.
    const DYN_TENANT_TOKEN = "opaque_dyn_feishu_tok_r4_445566";

    type FeishuItem = {
      command_id: string;
      command: string;
      description: { default_value: string; i18n?: Record<string, string> };
    };

    let store: FeishuItem[] = [];
    const resetStore = () => {
      store = [
        {
          command_id: `unmanaged_id_${DYN_TENANT_TOKEN}`,
          command: `ops_${DYN_TENANT_TOKEN}`,
          description: { default_value: "运维命令" },
        },
        {
          command_id: `id_main_${DYN_TENANT_TOKEN}`,
          command: "main",
          description: { default_value: "返回 Chief of Staff" },
        },
        {
          command_id: `id_ws_${DYN_TENANT_TOKEN}`,
          command: "workspace",
          description: {
            default_value: "旧工作区说明",
            i18n: { en_us: `Switch workspace ${DYN_TENANT_TOKEN}` },
          },
        },
      ];
    };
    resetStore();

    let stopBehavior: "succeed" | "fail_403" | "disconnect_unconfirmed" =
      "succeed";

    const srv = await startLocalHttpServer((req, res, rec) => {
      if (rec.pathname === "/open-apis/auth/v3/tenant_access_token/internal") {
        sendJson(res, 200, {
          code: 0,
          tenant_access_token: DYN_TENANT_TOKEN,
        });
        return;
      }

      if (
        rec.method === "GET" &&
        rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH
      ) {
        sendJson(res, 200, {
          code: 0,
          data: { items: store, has_more: false },
        });
        return;
      }

      if (
        rec.method === "PATCH" &&
        rec.pathname.startsWith(`${FEISHU_SLASH_COMMAND_BASE_PATH}/`)
      ) {
        const targetId = decodeURIComponent(
          rec.pathname.slice(`${FEISHU_SLASH_COMMAND_BASE_PATH}/`.length),
        );
        const found = store.find((item) => item.command_id === targetId);
        if (!found) {
          sendJson(res, 404, { code: 40400, msg: "command_id not found" });
          return;
        }
        const patch = rec.jsonBody as {
          description: { default_value: string; i18n?: Record<string, string> };
        };
        found.description = patch.description;
        sendJson(res, 200, { code: 0, data: found });
        return;
      }

      if (
        rec.method === "POST" &&
        rec.pathname === FEISHU_SLASH_COMMAND_BASE_PATH
      ) {
        const body = rec.jsonBody as {
          command: string;
          description: { default_value: string };
        };
        if (body.command === "stop" && stopBehavior === "fail_403") {
          sendJson(res, 403, {
            code: 99991672,
            msg: `Forbidden creating stop with ${DYN_TENANT_TOKEN}`,
          });
          return;
        }
        if (
          body.command === "stop" &&
          stopBehavior === "disconnect_unconfirmed"
        ) {
          req.socket.destroy();
          return;
        }
        const created: FeishuItem = {
          command_id: `id_${body.command}_${DYN_TENANT_TOKEN}`,
          command: body.command,
          description: body.description,
        };
        store.push(created);
        sendJson(res, 200, { code: 0, data: created });
        return;
      }

      sendJson(res, 404, { code: 404 });
    });

    const here = dirname(fileURLToPath(import.meta.url));
    const tempDir = mkdtempSync(join(tmpdir(), "dalton-feishu-cli-r4-"));
    const bundlePath = join(tempDir, "native-commands.mjs");

    try {
      await build({
        entryPoints: [resolve(here, "../src/native-commands/cli.ts")],
        outfile: bundlePath,
        bundle: true,
        platform: "node",
        target: "node22",
        format: "esm",
        banner: { js: "#!/usr/bin/env node" },
        logLevel: "silent",
      });

      const baseChildEnv = {
        PATH: process.env.PATH ?? "",
        CLI_BUNDLE_URL: pathToFileURL(bundlePath).href,
        TEST_ORIGIN: srv.origin,
        DALTON_FEISHU_APP_ID: FEISHU_APP_ID,
        DALTON_FEISHU_APP_SECRET: FEISHU_SECRET,
      };

      // 1. Preview (real child process `--json` + CLI text)
      const previewChild = await spawnNodeCli(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'feishu', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        baseChildEnv,
      );
      expect(previewChild.exitCode).toBe(0);
      expect(previewChild.stdout).not.toContain(DYN_TENANT_TOKEN);
      expect(previewChild.stdout).not.toContain(FEISHU_SECRET);
      expect(previewChild.stderr).not.toContain(DYN_TENANT_TOKEN);
      const previewJson = JSON.parse(previewChild.stdout) as {
        status: string;
        plan: Array<{
          name: string;
          remoteId?: string;
          requestPath?: string;
          requestBody?: { description?: { i18n?: Record<string, string> } };
        }>;
        preservedCommands: Array<{ name: string; remoteId?: string }>;
      };
      expect(previewJson.status).toBe("preview");
      expect(previewJson.plan.find((p) => p.name === "main")?.remoteId).toBe(
        "id_main_[REDACTED]",
      );
      const wsPreview = previewJson.plan.find((p) => p.name === "workspace")!;
      expect(wsPreview.remoteId).toBe("id_ws_[REDACTED]");
      expect(wsPreview.requestPath).toBe(
        `${FEISHU_SLASH_COMMAND_BASE_PATH}/id_ws_[REDACTED]`,
      );
      expect(wsPreview.requestBody?.description?.i18n?.en_us).toBe(
        "Switch workspace [REDACTED]",
      );
      expect(previewJson.preservedCommands).toEqual([
        { name: "ops_[REDACTED]", remoteId: "unmanaged_id_[REDACTED]" },
      ]);

      const previewTextOut = createBufferWriter();
      const previewTextErr = createBufferWriter();
      await runNativeCommandsCli(["--platform", "feishu"], {
        env: baseChildEnv,
        stdout: previewTextOut,
        stderr: previewTextErr,
        testApiOrigin: srv.origin,
      });
      expect(previewTextOut.text()).toContain("id_main_[REDACTED]");
      expect(previewTextOut.text()).not.toContain(DYN_TENANT_TOKEN);
      expect(previewTextErr.text()).not.toContain(DYN_TENANT_TOKEN);

      // 2. Partial apply failure (`stop` returns 403): internal PATCH must still hit real ID!
      resetStore();
      srv.requests.length = 0;
      stopBehavior = "fail_403";

      const failedChild = await spawnNodeCli(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'feishu', '--apply', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        baseChildEnv,
      );
      expect(failedChild.exitCode).toBe(3);
      // Verify outbound HTTP PATCH targeted the REAL unredacted command_id and i18n
      const patchReq = srv.requests.find((r) => r.method === "PATCH")!;
      expect(patchReq.pathname).toBe(
        `${FEISHU_SLASH_COMMAND_BASE_PATH}/id_ws_${DYN_TENANT_TOKEN}`,
      );
      expect(patchReq.jsonBody).toEqual({
        description: {
          default_value: "查看可访问的工作区",
          i18n: { en_us: `Switch workspace ${DYN_TENANT_TOKEN}` },
        },
      });
      expect(failedChild.stdout).not.toContain(DYN_TENANT_TOKEN);
      expect(failedChild.stderr).not.toContain(DYN_TENANT_TOKEN);
      const failedJson = JSON.parse(failedChild.stdout) as {
        ok: boolean;
        status: string;
        outcomes: Array<{ name: string; status: string; remoteId?: string }>;
      };
      expect(failedJson.ok).toBe(false);
      expect(failedJson.status).toBe("failed");
      expect(failedJson.outcomes.map((o) => [o.name, o.status, o.remoteId])).toEqual([
        ["main", "unchanged", "id_main_[REDACTED]"],
        ["workspace", "updated", "id_ws_[REDACTED]"],
        ["new", "created", "id_new_[REDACTED]"],
        ["stop", "failed", undefined],
      ]);

      // 3. Unconfirmed write (`stop` socket drops and live GET does not show `stop`)
      stopBehavior = "disconnect_unconfirmed";
      const unconfirmedChild = await spawnNodeCli(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'feishu', '--apply', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        baseChildEnv,
      );
      expect(unconfirmedChild.exitCode).toBe(4);
      expect(unconfirmedChild.stdout).not.toContain(DYN_TENANT_TOKEN);
      expect(unconfirmedChild.stderr).not.toContain(DYN_TENANT_TOKEN);
      const unconfirmedJson = JSON.parse(unconfirmedChild.stdout) as {
        ok: boolean;
        status: string;
        outcomes: Array<{ name: string; status: string; remoteId?: string }>;
      };
      expect(unconfirmedJson.ok).toBe(false);
      expect(unconfirmedJson.status).toBe("unconfirmed");
      expect(
        unconfirmedJson.outcomes.find((o) => o.name === "new")?.remoteId,
      ).toBe("id_new_[REDACTED]");

      // Also check text mode on unconfirmed result
      const unconfirmedTextOut = createBufferWriter();
      const unconfirmedTextErr = createBufferWriter();
      const unconfirmedTextCode = await runNativeCommandsCli(
        ["--platform", "feishu", "--apply"],
        {
          env: baseChildEnv,
          stdout: unconfirmedTextOut,
          stderr: unconfirmedTextErr,
          testApiOrigin: srv.origin,
        },
      );
      expect(unconfirmedTextCode).toBe(4);
      expect(unconfirmedTextOut.text()).not.toContain(DYN_TENANT_TOKEN);
      expect(unconfirmedTextErr.text()).not.toContain(DYN_TENANT_TOKEN);

      // 4. Full apply success (`resetStore` + `stopBehavior = "succeed"`)
      resetStore();
      srv.requests.length = 0;
      stopBehavior = "succeed";

      const appliedChild = await spawnNodeCli(
        [
          "--input-type=module",
          "-e",
          "const m = await import(process.env.CLI_BUNDLE_URL); process.exitCode = await m.runNativeCommandsCli(['--platform', 'feishu', '--apply', '--json'], { testApiOrigin: process.env.TEST_ORIGIN });",
        ],
        baseChildEnv,
      );
      expect(appliedChild.exitCode).toBe(0);
      expect(
        srv.requests.some(
          (r) =>
            r.method === "PATCH" &&
            r.pathname ===
              `${FEISHU_SLASH_COMMAND_BASE_PATH}/id_ws_${DYN_TENANT_TOKEN}`,
        ),
      ).toBe(true);
      expect(appliedChild.stdout).not.toContain(DYN_TENANT_TOKEN);
      expect(appliedChild.stdout).not.toContain(FEISHU_SECRET);
      expect(appliedChild.stderr).not.toContain(DYN_TENANT_TOKEN);
      expect(appliedChild.stderr).not.toContain(FEISHU_SECRET);

      const appliedJson = JSON.parse(appliedChild.stdout) as {
        ok: boolean;
        status: string;
        outcomes: Array<{ name: string; status: string; remoteId?: string }>;
      };
      expect(appliedJson.ok).toBe(true);
      expect(appliedJson.status).toBe("applied");
      expect(
        appliedJson.outcomes.map((o) => [o.name, o.status, o.remoteId]),
      ).toEqual([
        ["main", "unchanged", "id_main_[REDACTED]"],
        ["workspace", "updated", "id_ws_[REDACTED]"],
        ["new", "created", "id_new_[REDACTED]"],
        ["stop", "created", "id_stop_[REDACTED]"],
      ]);

      const appliedTextOut = createBufferWriter();
      const appliedTextErr = createBufferWriter();
      const appliedTextCode = await runNativeCommandsCli(
        ["--platform", "feishu", "--apply"],
        {
          env: baseChildEnv,
          stdout: appliedTextOut,
          stderr: appliedTextErr,
          testApiOrigin: srv.origin,
        },
      );
      expect(appliedTextCode).toBe(0);
      expect(appliedTextOut.text()).toContain("id_stop_[REDACTED]");
      expect(appliedTextOut.text()).not.toContain(DYN_TENANT_TOKEN);
      expect(appliedTextErr.text()).not.toContain(DYN_TENANT_TOKEN);
    } finally {
      rmSync(tempDir, { recursive: true, force: true });
    }
  });

  it("scrubs opaque Discord bot tokens echoed in remote command IDs, applicationName, and localizations across preview, applied, failed, and unconfirmed results without breaking internal PATCH IDs", async () => {
    const OPAQUE_BOT_TOKEN = "custom_opaque_discord_tok_r4_667788";
    type DiscordCmd = Record<string, unknown>;
    let store: DiscordCmd[] = [];
    const resetStore = () => {
      store = [
        {
          id: `id_extra_${OPAQUE_BOT_TOKEN}`,
          type: 1,
          name: `extra_${OPAQUE_BOT_TOKEN}`,
          description: "Extra command",
        },
        {
          id: `id_main_${OPAQUE_BOT_TOKEN}`,
          type: 1,
          name: "main",
          description: "返回 Chief of Staff",
          integration_types: [0],
          contexts: [0, 1],
        },
        {
          id: `id_ws_${OPAQUE_BOT_TOKEN}`,
          type: 1,
          name: "workspace",
          description: "旧描述",
          integration_types: [0],
          contexts: [0, 1],
          options: [
            {
              name: "workspace",
              type: 3,
              description: "旧参数说明",
              required: false,
              autocomplete: true,
              name_localizations: { "en-US": `ws_${OPAQUE_BOT_TOKEN}` },
            },
          ],
        },
      ];
    };
    resetStore();

    let stopBehavior: "succeed" | "fail_403" | "disconnect_unconfirmed" =
      "succeed";
    const base = `/api/v10/applications/${TEST_DISCORD_APP_ID}/commands`;

    const srv = await startLocalHttpServer((req, res, rec) => {
      if (rec.pathname === "/api/v10/oauth2/applications/@me") {
        sendJson(res, 200, {
          id: TEST_DISCORD_APP_ID,
          name: `Dalton "App" \\ ${OPAQUE_BOT_TOKEN}`,
        });
        return;
      }
      if (rec.method === "GET" && rec.pathname === base) {
        sendJson(res, 200, store);
        return;
      }
      if (rec.method === "PATCH" && rec.pathname.startsWith(`${base}/`)) {
        const targetId = decodeURIComponent(
          rec.pathname.slice(`${base}/`.length),
        );
        const found = store.find((c) => c.id === targetId);
        if (!found) {
          sendJson(res, 404, { message: "Unknown Application Command" });
          return;
        }
        Object.assign(found, rec.jsonBody as Record<string, unknown>);
        sendJson(res, 200, found);
        return;
      }
      if (rec.method === "POST" && rec.pathname === base) {
        const body = rec.jsonBody as Record<string, unknown>;
        if (body.name === "stop" && stopBehavior === "fail_403") {
          sendJson(res, 403, {
            code: 50013,
            message: `Missing Permissions for ${OPAQUE_BOT_TOKEN}`,
          });
          return;
        }
        if (body.name === "stop" && stopBehavior === "disconnect_unconfirmed") {
          req.socket.destroy();
          return;
        }
        const created: DiscordCmd = {
          id: `id_${String(body.name)}_${OPAQUE_BOT_TOKEN}`,
          ...body,
        };
        store.push(created);
        sendJson(res, 201, created);
        return;
      }
      sendJson(res, 404, {});
    });

    // 1. Preview via library
    const previewRes = await syncDiscordNativeCommands({
      botToken: OPAQUE_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "preview",
      testApiOrigin: srv.origin,
    });
    expect(previewRes.applicationName).toBe('Dalton "App" \\ [REDACTED]');
    expect(JSON.stringify(previewRes)).not.toContain(OPAQUE_BOT_TOKEN);

    // 2. Partial failure (`stop` 403) — internal PATCH uses real ID
    stopBehavior = "fail_403";
    srv.requests.length = 0;
    const failedRes = await syncDiscordNativeCommands({
      botToken: OPAQUE_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(failedRes.status).toBe("failed");
    const discordPatch = srv.requests.find((r) => r.method === "PATCH")!;
    expect(discordPatch.pathname).toBe(`${base}/id_ws_${OPAQUE_BOT_TOKEN}`);
    expect(discordPatch.jsonBody).toMatchObject({
      options: [
        {
          name_localizations: { "en-US": `ws_${OPAQUE_BOT_TOKEN}` },
        },
      ],
    });
    expect(JSON.stringify(failedRes)).not.toContain(OPAQUE_BOT_TOKEN);

    // 3. Unconfirmed (`stop` socket drops)
    stopBehavior = "disconnect_unconfirmed";
    const unconfirmedRes = await syncDiscordNativeCommands({
      botToken: OPAQUE_BOT_TOKEN,
      applicationId: TEST_DISCORD_APP_ID,
      mode: "apply",
      testApiOrigin: srv.origin,
    });
    expect(unconfirmedRes.status).toBe("unconfirmed");
    expect(JSON.stringify(unconfirmedRes)).not.toContain(OPAQUE_BOT_TOKEN);

    // 4. Full apply success
    resetStore();
    stopBehavior = "succeed";
    const outJson = createBufferWriter();
    const errJson = createBufferWriter();
    const exitCode = await runNativeCommandsCli(
      ["--platform", "discord", "--apply", "--json"],
      {
        env: {
          DALTON_DISCORD_BOT_TOKEN: OPAQUE_BOT_TOKEN,
          DALTON_DISCORD_APPLICATION_ID: TEST_DISCORD_APP_ID,
        },
        stdout: outJson,
        stderr: errJson,
        testApiOrigin: srv.origin,
      },
    );
    expect(exitCode).toBe(0);
    expect(outJson.text()).not.toContain(OPAQUE_BOT_TOKEN);
    expect(errJson.text()).not.toContain(OPAQUE_BOT_TOKEN);
    const parsedApplied = JSON.parse(outJson.text()) as {
      status: string;
      applicationName: string;
      outcomes: Array<{ name: string; remoteId?: string }>;
    };
    expect(parsedApplied.status).toBe("applied");
    expect(parsedApplied.applicationName).toBe('Dalton "App" \\ [REDACTED]');
    expect(parsedApplied.outcomes.map((o) => [o.name, o.remoteId])).toEqual([
      ["main", "id_main_[REDACTED]"],
      ["workspace", "id_ws_[REDACTED]"],
      ["new", "id_new_[REDACTED]"],
      ["stop", "id_stop_[REDACTED]"],
    ]);
  });
});
