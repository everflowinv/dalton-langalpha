import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { AddressInfo, Socket } from "node:net";
import { afterEach, describe, expect, it } from "vitest";
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

      const handleCollection = (
        list: Array<Record<string, unknown>>,
        basePath: string,
      ): boolean => {
        if (rec.method === "GET" && rec.pathname === basePath) {
          sendJson(res, 200, list);
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
    expect(srv.requests.map((r) => `${r.method} ${r.pathname}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands`,
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
    expect(srv.requests.map((r) => `${r.method} ${r.pathname}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands`,
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
    expect(srv.requests.map((r) => `${r.method} ${r.pathname}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/commands`,
    ]);

    // 4. Guild-scoped CLI `--apply --guild-id <ID>` hits guild endpoints and omits global-only contexts/integration_types
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
    expect(srv.requests.map((r) => `${r.method} ${r.pathname}`)).toEqual([
      "GET /api/v10/oauth2/applications/@me",
      `GET /api/v10/applications/${TEST_DISCORD_APP_ID}/guilds/${TEST_GUILD_ID}/commands`,
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
    await expect(
      syncDiscordNativeCommands({
        botToken: TEST_DISCORD_BOT_TOKEN,
        applicationId: TEST_DISCORD_APP_ID,
        mode: "apply",
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "daily_create_limit" });
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
    await expect(
      syncDiscordNativeCommands({
        botToken: TEST_DISCORD_BOT_TOKEN,
        applicationId: TEST_DISCORD_APP_ID,
        mode: "apply",
        maxRequests: 2,
        testApiOrigin: srv.origin,
      }),
    ).rejects.toMatchObject({ code: "request_limit_exceeded" });
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
});
