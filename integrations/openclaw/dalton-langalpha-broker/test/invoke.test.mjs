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

test("G5.1 & B8: gateway restart pending -> 409 indeterminate, client disconnect mid-stream finishes & replays 200, reasoning_effort echo", async () => {
  const tmpStateDir = await fs.mkdtemp(path.join(os.tmpdir(), "dalton-broker-g51-"));
  let streamCalls = 0;
  let releaseMidStream = null;
  const midStreamStarted = new Promise((resolve) => {
    releaseMidStream = resolve;
  });
  let finishUpstream = null;
  const upstreamGate = new Promise((resolve) => {
    finishUpstream = resolve;
  });
  let upstreamFinishedSignal = null;
  const upstreamCompleted = new Promise((resolve) => {
    upstreamFinishedSignal = resolve;
  });

  const fakeCfg = {
    meta: { lastTouchedVersion: "2.4.0" },
    models: {
      providers: {
        xai: {
          api: "openai-responses",
          models: [{ id: "grok-4.7", name: "Grok 4.7", reasoning: true, input: ["text"] }],
        },
      },
    },
  };

  function startBrokerInstance(customStreamFn) {
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
          apiKey: "secret-key",
          mode: "oauth",
          profileId: "default",
        },
      }),
      streamSimple: customStreamFn,
      calculateCost: () => {},
    };
    const pluginDef = createPluginDefinition(fakeDeps);
    pluginDef.register({
      pluginConfig: {},
      config: fakeCfg,
      runtime: {
        version: "2.4.0",
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
    return { registeredRoute, registeredService };
  }

  const inst1 = startBrokerInstance((_model, _ctx, opts) => {
    streamCalls += 1;
    const callNum = streamCalls;
    const partial = { content: [{ type: "text", text: `Answer #${callNum}` }] };
    if (callNum === 2) {
      // Streaming call where client will disconnect after first chunk
      return {
        async *[Symbol.asyncIterator]() {
          yield { type: "start", partial };
          yield { type: "text_delta", contentIndex: 0, delta: "Part 1 ", partial };
          releaseMidStream();
          await upstreamGate;
          assert.equal(opts.signal.aborted, false, "Client disconnect must NOT abort upstream signal");
          yield { type: "text_delta", contentIndex: 0, delta: "Part 2", partial };
        },
        async result() {
          setTimeout(() => upstreamFinishedSignal(), 20);
          return {
            role: "assistant",
            stopReason: "stop",
            content: [{ type: "text", text: "Part 1 Part 2" }],
            usage: {
              input: 10,
              output: 5,
              cacheRead: 0,
              cacheWrite: 0,
              totalTokens: 15,
              cost: { input: 0.0001, output: 0.0001, cacheRead: 0, cacheWrite: 0, total: 0.0002 },
            },
          };
        },
      };
    }
    return createFakeEventStream(
      [{ type: "start", partial }, { type: "text_delta", contentIndex: 0, delta: `Answer #${callNum}`, partial }],
      {
        role: "assistant",
        stopReason: "stop",
        content: partial.content,
        usage: {
          input: 10,
          output: 5,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 15,
          cost: { input: 0.0001, output: 0.0001, cacheRead: 0, cacheWrite: 0, total: 0.0002 },
        },
      },
    );
  });

  await inst1.registeredService.start({ stateDir: tmpStateDir, logger: { info() {} } });
  const brokerDir = path.join(tmpStateDir, "plugins", "dalton-langalpha-broker");
  const token = (await fs.readFile(path.join(brokerDir, "broker.token"), "utf8")).trim();

  let activeHandler = inst1.registeredRoute.handler;
  const server = http.createServer(async (req, res) => {
    const handled = await activeHandler(req, res);
    if (!handled && !res.writableEnded) {
      res.writeHead(404);
      res.end();
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  const baseUrl = `http://127.0.0.1:${port}/dalton-broker/v1`;

  try {
    // (c) Reasoning effort echo & /health alignment
    const lowResp = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-Dalton-Invocation-Id": "inv-reasoning-low",
      },
      body: JSON.stringify({
        model: "xai/grok-4.7",
        reasoning_effort: "low",
        messages: [{ role: "user", content: "17 * 19?" }],
      }),
    });
    assert.equal(lowResp.status, 200);
    const lowJson = await lowResp.json();
    assert.equal(lowJson.dalton_meta.reasoning_effort, "low");
    assert.equal(lowJson.openclaw.reasoning, "low");

    // (b) Client disconnect mid-stream -> upstream finishes and retry with same ID returns 200 replayed response
    const abortCtrl = new AbortController();
    const disconnectReqPromise = fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-Dalton-Invocation-Id": "inv-disconnect-1",
      },
      body: JSON.stringify({
        model: "xai/grok-4.7",
        stream: true,
        reasoning_effort: "high",
        messages: [{ role: "user", content: "Stream test" }],
      }),
      signal: abortCtrl.signal,
    }).catch(() => null);

    await midStreamStarted;
    // Disconnect client mid-stream
    abortCtrl.abort();
    await disconnectReqPromise;
    await new Promise((r) => setTimeout(r, 30));

    // Release upstream to finish and persist completed response to disk
    finishUpstream();
    await upstreamCompleted;
    await new Promise((r) => setTimeout(r, 30));

    const callsBeforeRetry = streamCalls;
    const retryResp = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-Dalton-Invocation-Id": "inv-disconnect-1",
      },
      body: JSON.stringify({
        model: "xai/grok-4.7",
        stream: false,
        reasoning_effort: "high",
        messages: [{ role: "user", content: "Stream test" }],
      }),
    });
    assert.equal(retryResp.status, 200);
    assert.equal(retryResp.headers.get("x-dalton-replayed"), "1");
    const retryJson = await retryResp.json();
    assert.equal(retryJson.openclaw.replayed, true);
    assert.equal(retryJson.dalton_meta.reasoning_effort, "high");
    assert.equal(retryJson.choices[0].message.content, "Part 1 Part 2");
    assert.equal(streamCalls, callsBeforeRetry, "Retry after client disconnect must not re-invoke upstream");

    // (a) Simulate gateway crash leaving a "pending" entry on disk -> restart converts to "indeterminate" -> retry returns 409
    const invPath = path.join(brokerDir, "invocations.json");
    const diskData = JSON.parse(await fs.readFile(invPath, "utf8"));
    // Compute the exact reqHash by copying the hash structure from inv-reasoning-low and changing status to pending
    const crashPayload = {
      model: "xai/grok-4.7",
      reasoning_effort: "low",
      messages: [{ role: "user", content: "17 * 19?" }],
    };
    diskData.entries["inv-crashed-pending"] = {
      ...diskData.entries["inv-reasoning-low"],
      invocationId: "inv-crashed-pending",
      status: "pending",
      responsePayload: undefined,
    };
    await fs.writeFile(invPath, JSON.stringify(diskData, null, 2), { mode: 0o600 });

    // Start new broker instance on the same stateDir (simulating gateway restart)
    const inst2 = startBrokerInstance(() => {
      throw new Error("Should not call upstream on indeterminate invocation");
    });
    await inst2.registeredService.start({ stateDir: tmpStateDir, logger: { info() {} } });
    activeHandler = inst2.registeredRoute.handler;

    const indetResp = await fetch(`${baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-Dalton-Invocation-Id": "inv-crashed-pending",
      },
      body: JSON.stringify(crashPayload),
    });
    assert.equal(indetResp.status, 409);
    const indetJson = await indetResp.json();
    assert.equal(indetJson.error.code, "idempotency_indeterminate");

    // Verify /health fields alignment
    const healthResp = await fetch(`${baseUrl}/health`);
    const healthJson = await healthResp.json();
    assert.equal(healthJson.status, "ok");
    assert.equal(healthJson.ok, true);
    assert.equal(healthJson.broker_version, "0.2.0");
    assert.equal(healthJson.openclaw_version, "2.4.0");
    assert.equal(typeof healthJson.journal_entries, "number");
    assert.equal(healthJson.journal.indeterminate, 1);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    await fs.rm(tmpStateDir, { recursive: true, force: true });
  }
});

