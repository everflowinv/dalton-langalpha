// integrations/openclaw/dalton-langalpha-broker/test/invoke.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createPluginDefinition } from "../src/plugin-definition.mjs";

function createFakeEventStream(events, finalMessage) {
  return {
    async *[Symbol.asyncIterator]() {
      for (const ev of events) {
        yield ev;
      }
    },
    async result() {
      return finalMessage;
    },
  };
}

test("Full HTTP lifecycle: /health, /models, /chat/completions (non-stream + stream SSE + tool_calls)", async () => {
  const tmpStateDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-broker-e2e-"));
  let registeredRoute = null;
  let registeredService = null;

  const fakeDeps = {
    prepareSimpleCompletionModelForAgent: async ({ modelRef }) => ({
      model: {
        provider: modelRef.split("/")[0],
        id: modelRef.split("/")[1],
        api: "openai-responses",
        reasoning: true,
        cost: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0 },
      },
      auth: {
        apiKey: "secret-never-leaves-process",
        mode: "oauth",
        profileId: "default",
      },
    }),
    streamSimple: (model, context, _opts) => {
      const hasTools = Array.isArray(context.tools) && context.tools.length > 0;
      const lastMsg = context.messages[context.messages.length - 1];
      if (hasTools && lastMsg.role === "user") {
        const partial = {
          content: [
            {
              type: "toolCall",
              id: "call_xyz",
              name: context.tools[0].name,
              arguments: { ticker: "MSFT" },
            },
          ],
        };
        return createFakeEventStream(
          [
            { type: "start", partial },
            { type: "toolcall_start", contentIndex: 0, partial },
            { type: "toolcall_delta", contentIndex: 0, delta: '{"ticker":"MSFT"}', partial },
          ],
          {
            role: "assistant",
            stopReason: "toolUse",
            content: partial.content,
            usage: {
              input: 80,
              output: 20,
              cacheRead: 0,
              cacheWrite: 0,
              totalTokens: 100,
              cost: { input: 0.00008, output: 0.00004, cacheRead: 0, cacheWrite: 0, total: 0.00012 },
            },
          },
        );
      }

      const partial = { content: [{ type: "text", text: "MSFT is trading at $420." }] };
      return createFakeEventStream(
        [
          { type: "start", partial },
          { type: "text_delta", contentIndex: 0, delta: "MSFT is trading ", partial },
          { type: "text_delta", contentIndex: 0, delta: "at $420.", partial },
        ],
        {
          role: "assistant",
          stopReason: "stop",
          content: partial.content,
          usage: {
            input: 120,
            output: 12,
            cacheRead: 40,
            cacheWrite: 0,
            totalTokens: 172,
            cost: { input: 0.00012, output: 0.000024, cacheRead: 0, cacheWrite: 0, total: 0.000144 },
          },
        },
      );
    },
    calculateCost: () => {},
  };

  const pluginDef = createPluginDefinition(fakeDeps);
  const fakeCfg = {
    models: {
      providers: {
        xai: {
          api: "openai-responses",
          models: [{ id: "grok-4.7", name: "Grok 4.7", reasoning: true, input: ["text"] }],
        },
      },
    },
  };

  pluginDef.register({
    pluginConfig: {},
    config: fakeCfg,
    runtime: {
      config: { current: () => fakeCfg },
      state: { resolveStateDir: () => tmpStateDir },
    },
    logger: { info() {}, warn() {} },
    registerService(svc) {
      registeredService = svc;
    },
    registerHttpRoute(route) {
      registeredRoute = route;
    },
  });

  await registeredService.start({ stateDir: tmpStateDir, logger: { info() {} } });
  const token = (
    await fs.readFile(path.join(tmpStateDir, "plugins", "dalton-langalpha-broker", "broker.token"), "utf8")
  ).trim();

  const server = http.createServer(async (req, res) => {
    const handled = await registeredRoute.handler(req, res);
    if (!handled && !res.writableEnded) {
      res.writeHead(404);
      res.end();
    }
  });

  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  const baseUrl = `http://127.0.0.1:${port}/dalton-broker/v1`;

  try {
    // 1. Health check (no auth required on loopback)
    const healthResp = await fetch(`${baseUrl}/health`);
    assert.equal(healthResp.status, 200);
    const healthJson = await healthResp.json();
    assert.equal(healthJson.ok, true);
    assert.equal(healthJson.configured_models_count, 1);

    // 2. Unauthorized check on /models
    const unauthResp = await fetch(`${baseUrl}/models`);
    assert.equal(unauthResp.status, 401);

    // 3. Authorized /models
    const modelsResp = await fetch(`${baseUrl}/models?detail=1`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    assert.equal(modelsResp.status, 200);
    const modelsJson = await modelsResp.json();
    assert.equal(modelsJson.data[0].id, "xai/grok-4.7");

    // 4. Non-streaming tool call turn
    const turn1Resp = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "xai/grok-4.7",
        stream: false,
        messages: [{ role: "user", content: "What is MSFT trading at?" }],
        tools: [
          {
            type: "function",
            function: {
              name: "get_quote",
              description: "Get quote",
              parameters: { type: "object", properties: { ticker: { type: "string" } } },
            },
          },
        ],
      }),
    });
    assert.equal(turn1Resp.status, 200);
    const turn1Json = await turn1Resp.json();
    assert.equal(turn1Json.choices[0].finish_reason, "tool_calls");
    assert.equal(turn1Json.choices[0].message.tool_calls[0].function.name, "get_quote");

    // 5. Streaming SSE follow-up turn with tool result
    const turn2Resp = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "xai/grok-4.7",
        stream: true,
        messages: [
          { role: "user", content: "What is MSFT trading at?" },
          turn1Json.choices[0].message,
          {
            role: "tool",
            tool_call_id: "call_xyz",
            content: '{"price": 420.0}',
          },
        ],
      }),
    });
    assert.equal(turn2Resp.status, 200);
    const sseText = await turn2Resp.text();
    assert.ok(sseText.includes("MSFT is trading "));
    assert.ok(sseText.includes("data: [DONE]"));
    assert.equal(sseText.includes("secret-never-leaves-process"), false);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await fs.rm(tmpStateDir, { recursive: true, force: true });
  }
});
