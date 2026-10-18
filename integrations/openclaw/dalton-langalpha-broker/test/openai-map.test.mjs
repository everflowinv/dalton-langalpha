// integrations/openclaw/dalton-langalpha-broker/test/openai-map.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import {
  normalizeOpenAiMessagesToContext,
  normalizeOpenAiTools,
  normalizeToolChoice,
  normalizeResponseFormat,
  buildNonStreamingChatCompletionResponse,
  createSseDeltaTranslator,
} from "../src/openai-map.mjs";
import { createReplayStore } from "../src/replay-state.mjs";

test("normalizeOpenAiMessagesToContext splits system/developer and maps user+assistant+tool turns", () => {
  const replayStore = createReplayStore();
  replayStore.recordAssistantTurn("sess_1", {
    content: [
      { type: "thinking", thinking: "Step 1 reasoning", thinkingSignature: "sig_think" },
      {
        type: "toolCall",
        id: "call_abc",
        name: "get_price",
        arguments: { ticker: "AAPL" },
        thoughtSignature: "sig_tool_123",
      },
    ],
  });

  const ctx = normalizeOpenAiMessagesToContext(
    [
      { role: "system", content: "You are a financial analyst." },
      { role: "developer", content: "Always verify ticker symbols." },
      { role: "user", content: "Check AAPL price." },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "call_abc",
            type: "function",
            function: { name: "get_price", arguments: '{"ticker":"AAPL"}' },
          },
        ],
      },
      {
        role: "tool",
        tool_call_id: "call_abc",
        content: '{"price": 225.5}',
      },
    ],
    {
      modelMeta: { provider: "google", id: "gemini-3.8-flash", api: "google-generative-ai" },
      sessionId: "sess_1",
      replayStore,
    },
  );

  assert.equal(ctx.systemPrompt, "You are a financial analyst.\n\nAlways verify ticker symbols.");
  assert.equal(ctx.messages.length, 3);
  assert.equal(ctx.messages[0].role, "user");
  assert.equal(ctx.messages[1].role, "assistant");
  // Replay store restores thinking block + thoughtSignature for Gemini 3 tool replay
  assert.equal(ctx.messages[1].content[0].type, "thinking");
  assert.equal(ctx.messages[1].content[1].type, "toolCall");
  assert.equal(ctx.messages[1].content[1].thoughtSignature, "sig_tool_123");
  assert.deepEqual(ctx.messages[1].content[1].arguments, { ticker: "AAPL" });

  assert.equal(ctx.messages[2].role, "toolResult");
  assert.equal(ctx.messages[2].toolCallId, "call_abc");
  assert.equal(ctx.messages[2].toolName, "get_price");
  assert.equal(ctx.messages[2].content[0].text, '{"price": 225.5}');
});

test("normalizeOpenAiTools and normalizeToolChoice validate and map schemas", () => {
  const tools = normalizeOpenAiTools([
    {
      type: "function",
      function: {
        name: "get_price",
        description: "Get stock price",
        parameters: {
          type: "object",
          properties: { ticker: { type: "string" } },
          required: ["ticker"],
        },
        strict: true,
      },
    },
  ]);
  assert.equal(tools.length, 1);
  assert.equal(tools[0].name, "get_price");
  assert.equal(tools[0].strict, true);

  assert.equal(normalizeToolChoice("auto"), "auto");
  assert.equal(normalizeToolChoice("required"), "required");
  assert.deepEqual(
    normalizeToolChoice({ type: "function", function: { name: "get_price" } }),
    { type: "function", function: { name: "get_price" } },
  );
  assert.deepEqual(normalizeResponseFormat({ type: "json_object" }), { type: "json_object" });
});

test("buildNonStreamingChatCompletionResponse formats text, reasoning_content, tool_calls, and usage", () => {
  const res = buildNonStreamingChatCompletionResponse({
    id: "chatcmpl-123",
    created: 1700000000,
    modelRef: "xai/grok-4.7",
    assistantMsg: {
      role: "assistant",
      stopReason: "toolUse",
      content: [
        { type: "thinking", thinking: "Need to query AAPL" },
        {
          type: "toolCall",
          id: "call_1",
          name: "get_price",
          arguments: { ticker: "AAPL" },
        },
      ],
      usage: {
        input: 100,
        output: 40,
        cacheRead: 20,
        cacheWrite: 0,
        totalTokens: 160,
      },
    },
    daltonMeta: { provider: "xai", latency_ms: 450 },
  });

  assert.equal(res.choices[0].finish_reason, "tool_calls");
  assert.equal(res.choices[0].message.content, null);
  assert.equal(res.choices[0].message.reasoning_content, "Need to query AAPL");
  assert.equal(res.choices[0].message.tool_calls[0].function.name, "get_price");
  assert.equal(res.choices[0].message.tool_calls[0].function.arguments, '{"ticker":"AAPL"}');
  assert.equal(res.usage.prompt_tokens, 120);
  assert.equal(res.usage.completion_tokens, 40);
  assert.equal(res.usage.prompt_tokens_details.cached_tokens, 20);
});

test("createSseDeltaTranslator emits OpenAI-compatible role, reasoning, tool_calls, and final usage chunk", () => {
  const translator = createSseDeltaTranslator({
    id: "chatcmpl-sse",
    created: 1700000000,
    modelRef: "openai/gpt-6-astra",
    includeUsage: true,
  });

  const partial = {
    content: [
      { type: "thinking", thinking: "thinking..." },
      { type: "toolCall", id: "call_99", name: "lookup_filing", arguments: { cik: "0000320193" } },
    ],
  };

  const c1 = translator.onEvent({ type: "start", partial });
  assert.equal(c1.length, 1);
  assert.equal(c1[0].choices[0].delta.role, "assistant");

  const c2 = translator.onEvent({ type: "thinking_delta", contentIndex: 0, delta: "thinking...", partial });
  assert.equal(c2[0].choices[0].delta.reasoning_content, "thinking...");

  const c3 = translator.onEvent({ type: "toolcall_start", contentIndex: 1, partial });
  assert.equal(c3[0].choices[0].delta.tool_calls[0].id, "call_99");
  assert.equal(c3[0].choices[0].delta.tool_calls[0].function.name, "lookup_filing");

  const c4 = translator.onEvent({
    type: "toolcall_delta",
    contentIndex: 1,
    delta: '{"cik":"0000320193"}',
    partial,
  });
  assert.equal(c4[0].choices[0].delta.tool_calls[0].function.arguments, '{"cik":"0000320193"}');

  const cDone = translator.onDone(
    {
      stopReason: "toolUse",
      usage: { input: 50, output: 20, cacheRead: 0, cacheWrite: 0, totalTokens: 70 },
    },
    { provider: "openai", latency_ms: 310 },
  );
  assert.equal(cDone[0].choices[0].finish_reason, "tool_calls");
  assert.equal(cDone[0].usage.total_tokens, 70);
  assert.equal(cDone[0].dalton_meta.provider, "openai");
});
