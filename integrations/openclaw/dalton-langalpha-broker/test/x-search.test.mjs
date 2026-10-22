// integrations/openclaw/dalton-langalpha-broker/test/x-search.test.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import test from "node:test";
import { createPluginDefinition } from "../src/plugin-definition.mjs";

function makeMockRequest({
  method = "POST",
  url = "/dalton-broker/v1/x-search",
  headers = {},
  body = {},
  remoteAddress = "127.0.0.1",
} = {}) {
  const raw = typeof body === "string" ? body : JSON.stringify(body);
  const stream = Readable.from(raw ? [Buffer.from(raw, "utf8")] : []);
  stream.method = method;
  stream.url = url;
  stream.headers = {
    host: "127.0.0.1:18789",
    "content-type": "application/json",
    ...headers,
  };
  stream.socket = { remoteAddress };
  return stream;
}

function makeMockResponse() {
  const chunks = [];
  const res = {
    statusCode: 200,
    headers: {},
    writableEnded: false,
    destroyed: false,
    writeHead(status, hdrs = {}) {
      this.statusCode = status;
      this.headers = { ...this.headers, ...hdrs };
    },
    write(chunk) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk)));
      return true;
    },
    end(chunk) {
      if (chunk !== undefined) {
        this.write(chunk);
      }
      this.writableEnded = true;
    },
    json() {
      return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    },
  };
  return res;
}

async function setupHarness({
  maxConcurrent = 4,
  modelAuth = undefined,
  createXSearchTool,
  prepareSimpleCompletionModelForAgent,
  logger = { info() {}, warn() {}, error() {} },
} = {}) {
  const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-xsearch-test-"));
  let routeHandler = null;
  const plugin = createPluginDefinition({
    createXSearchTool,
    prepareSimpleCompletionModelForAgent,
  });

  const api = {
    pluginConfig: {
      maxConcurrent,
      authTokenPath: path.join(tmpDir, "broker.token"),
      journalDir: path.join(tmpDir, "journal"),
    },
    config: {
      plugins: {
        entries: {
          xai: {
            config: {
              xSearch: { enabled: true, model: "grok-4-1-fast-non-reasoning" },
            },
          },
        },
      },
    },
    runtime: {
      version: "2026.10.10",
      config: { current: () => api.config },
      state: { resolveStateDir: () => tmpDir },
      modelAuth,
    },
    logger,
    registerService() {},
    registerHttpRoute(spec) {
      routeHandler = spec.handler;
    },
  };

  plugin.register(api);
  assert.equal(typeof routeHandler, "function");

  const healthReq = makeMockRequest({
    method: "GET",
    url: "/dalton-broker/v1/health",
    body: "",
  });
  const healthRes = makeMockResponse();
  await routeHandler(healthReq, healthRes);
  const token = (await fs.readFile(path.join(tmpDir, "broker.token"), "utf8")).trim();

  return {
    tmpDir,
    token,
    routeHandler,
    async cleanup() {
      await fs.rm(tmpDir, { recursive: true, force: true });
    },
  };
}

test("POST /dalton-broker/v1/x-search executes x_search with resolved modelAuth key", async () => {
  let capturedAuthKey = null;
  let capturedArgs = null;

  const harness = await setupHarness({
    modelAuth: {
      async resolveApiKeyForProvider({ provider }) {
        assert.equal(provider, "xai");
        return { apiKey: "xai-oauth-token-123" };
      },
    },
    createXSearchTool({ auth }) {
      return {
        name: "x_search",
        async execute(_callId, args) {
          capturedAuthKey = await auth.resolveApiKeyForProvider("xai");
          capturedArgs = args;
          const payload = {
            query: args.query,
            provider: "xai",
            model: "grok-4-1-fast-non-reasoning",
            tookMs: 420,
            content:
              "<<<EXTERNAL_UNTRUSTED_CONTENT id=\"1\">>>\nSource: X Search\n---\n@sundarpichai (2026-10-22T07:30:00Z): Google Cloud Q3 update https://x.com/sundarpichai/status/1848000001\n<<<END_EXTERNAL_UNTRUSTED_CONTENT id=\"1\">>>",
            citations: ["https://x.com/sundarpichai/status/1848000001"],
          };
          return {
            content: [{ type: "text", text: JSON.stringify(payload) }],
            details: payload,
          };
        },
      };
    },
  });

  try {
    const req = makeMockRequest({
      headers: { authorization: `Bearer ${harness.token}` },
      body: {
        query: "GOOG premarket drop",
        handles: ["@sundarpichai", "googlecloud"],
        from_date: "2026-10-20",
        to_date: "2026-10-22",
      },
    });
    const res = makeMockResponse();
    await harness.routeHandler(req, res);

    assert.equal(res.statusCode, 200);
    const body = res.json();
    assert.equal(body.ok, true);
    assert.equal(body.provider, "xai");
    assert.equal(capturedAuthKey, "xai-oauth-token-123");
    assert.deepEqual(capturedArgs, {
      query: "GOOG premarket drop",
      allowed_x_handles: ["sundarpichai", "googlecloud"],
      from_date: "2026-10-20",
      to_date: "2026-10-22",
    });
    assert.deepEqual(body.result.details.citations, [
      "https://x.com/sundarpichai/status/1848000001",
    ]);
  } finally {
    await harness.cleanup();
  }
});

test("POST /dalton-broker/v1/x-search falls back to prepareSimpleCompletionModelForAgent for auth", async () => {
  let capturedAuthKey = null;

  const harness = await setupHarness({
    modelAuth: undefined,
    async prepareSimpleCompletionModelForAgent({ modelRef }) {
      assert.equal(modelRef, "xai/grok-4-1-fast-non-reasoning");
      return { apiKey: "xai-fallback-key-456", model: { id: "grok-4-1-fast-non-reasoning" } };
    },
    createXSearchTool({ auth }) {
      return {
        name: "x_search",
        async execute(_callId, args) {
          capturedAuthKey = await auth.resolveApiKeyForProvider("xai");
          const payload = {
            query: args.query,
            provider: "xai",
            content: "summary",
            citations: [],
          };
          return {
            content: [{ type: "text", text: JSON.stringify(payload) }],
            details: payload,
          };
        },
      };
    },
  });

  try {
    const req = makeMockRequest({
      headers: { authorization: `Bearer ${harness.token}` },
      body: { query: "NVDA Blackwell" },
    });
    const res = makeMockResponse();
    await harness.routeHandler(req, res);

    assert.equal(res.statusCode, 200);
    assert.equal(capturedAuthKey, "xai-fallback-key-456");
  } finally {
    await harness.cleanup();
  }
});

test("POST /dalton-broker/v1/x-search classifies errors by status/code without natural-language scanning or message leakage", async () => {
  const warnLogs = [];
  const harness = await setupHarness({
    logger: {
      info() {},
      warn(msg) {
        warnLogs.push(String(msg));
      },
      error() {},
    },
    createXSearchTool() {
      return {
        name: "x_search",
        async execute(_callId, args) {
          if (args.query === "missing_key") {
            const payload = {
              error: "missing_xai_api_key",
              message: "x_search requires an xAI API key",
            };
            return {
              content: [{ type: "text", text: JSON.stringify(payload) }],
              details: payload,
            };
          }
          if (args.query === "rate_limit") {
            const err = new Error("upstream 429 with secret=sk-live-9999");
            err.status = 429;
            throw err;
          }
          if (args.query === "5xx_with_invalid_word") {
            const err = new Error("502 Bad Gateway: invalid upstream response with token=xai-secret-777");
            err.status = 502;
            throw err;
          }
          return { details: { query: args.query, provider: "xai", content: "ok" } };
        },
      };
    },
  });

  try {
    // 1. Missing query
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: { query: "   " },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 400);
      assert.equal(res.json().error.code, "INVALID_REQUEST");
    }

    // 2. Combining allowed_x_handles and excluded_x_handles
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: {
          query: "TSLA",
          allowed_x_handles: ["elonmusk"],
          excluded_x_handles: ["spambot"],
        },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 400);
      assert.equal(res.json().error.code, "INVALID_REQUEST");
    }

    // 3. > 20 handles
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: {
          query: "TSLA",
          handles: Array.from({ length: 21 }, (_, i) => `user${i}`),
        },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 400);
      assert.equal(res.json().error.code, "INVALID_REQUEST");
    }

    // 4. Invalid date range (from_date > to_date)
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: {
          query: "TSLA",
          from_date: "2026-10-25",
          to_date: "2026-10-20",
        },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 400);
      assert.equal(res.json().error.code, "INVALID_REQUEST");
    }

    // 5. Missing API key error mapping (403)
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: { query: "missing_key" },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 403);
      assert.equal(res.json().error.code, "PROVIDER_PERMISSION_DENIED");
    }

    // 6. Rate limit error mapping (429) without leaking raw err.message
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: { query: "rate_limit" },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 429);
      const errBody = res.json();
      assert.equal(errBody.error.code, "PROVIDER_RATE_LIMITED");
      assert.equal(errBody.error.message.includes("sk-live-9999"), false);
    }

    // 7. 5xx error containing the word "invalid" is NOT misclassified as 400, and raw message is neither returned nor logged
    {
      const req = makeMockRequest({
        headers: { authorization: `Bearer ${harness.token}` },
        body: { query: "5xx_with_invalid_word" },
      });
      const res = makeMockResponse();
      await harness.routeHandler(req, res);
      assert.equal(res.statusCode, 502);
      const errBody = res.json();
      assert.equal(errBody.error.code, "PROVIDER_ERROR");
      assert.equal(errBody.error.message.includes("xai-secret-777"), false);
      for (const entry of warnLogs) {
        assert.equal(entry.includes("xai-secret-777"), false);
        assert.equal(entry.includes("sk-live-9999"), false);
      }
    }
  } finally {
    await harness.cleanup();
  }
});
