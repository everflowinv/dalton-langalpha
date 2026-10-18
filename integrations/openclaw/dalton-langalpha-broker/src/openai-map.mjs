// integrations/openclaw/dalton-langalpha-broker/src/openai-map.mjs

const SYSTEM_ROLES = new Set(["system", "developer"]);

function extractTextFromContent(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  for (const part of content) {
    if (!part || typeof part !== "object") continue;
    if ((part.type === "text" || part.type === "input_text") && typeof part.text === "string") {
      parts.push(part.text);
    }
  }
  return parts.join("\n");
}

function normalizeUserContentBlocks(content) {
  if (typeof content === "string") {
    return [{ type: "text", text: content }];
  }
  if (!Array.isArray(content)) {
    return [{ type: "text", text: String(content ?? "") }];
  }
  const blocks = [];
  for (const part of content) {
    if (!part || typeof part !== "object") continue;
    if (part.type === "text" || part.type === "input_text") {
      blocks.push({ type: "text", text: String(part.text ?? "") });
    } else if (part.type === "image_url" && part.image_url?.url) {
      const url = String(part.image_url.url);
      const dataMatch = url.match(/^data:([^;]+);base64,(.+)$/s);
      if (dataMatch) {
        blocks.push({
          type: "image",
          mimeType: dataMatch[1],
          data: dataMatch[2],
        });
      } else {
        blocks.push({
          type: "text",
          text: `[image_url: ${url}]`,
        });
      }
    }
  }
  return blocks.length > 0 ? blocks : [{ type: "text", text: "" }];
}

function safeParseJsonArguments(rawArgs) {
  if (rawArgs && typeof rawArgs === "object" && !Array.isArray(rawArgs)) {
    return rawArgs;
  }
  if (typeof rawArgs !== "string" || !rawArgs.trim()) {
    return {};
  }
  try {
    const parsed = JSON.parse(rawArgs);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : { _value: parsed };
  } catch {
    return { _raw: rawArgs };
  }
}

/**
 * Converts OpenAI Chat Completions `messages[]` into OpenClaw `Context`:
 *   { systemPrompt?: string, messages: Message[] }
 *
 * Also builds a `toolCallNameById` map so subsequent `role: "tool"` messages
 * that omit `name` still populate `ToolResultMessage.toolName` accurately.
 */
export function normalizeOpenAiMessagesToContext(
  messages,
  { modelMeta = {}, sessionId = undefined, replayStore = undefined } = {},
) {
  if (!Array.isArray(messages) || messages.length === 0) {
    const err = new Error("messages must be a non-empty array");
    err.statusCode = 400;
    err.code = "invalid_messages";
    throw err;
  }

  const systemParts = [];
  const normalizedMessages = [];
  const toolCallNameById = new Map();
  const baseTs = Date.now();

  for (let i = 0; i < messages.length; i += 1) {
    const msg = messages[i];
    if (!msg || typeof msg !== "object") continue;
    const role = String(msg.role || "").trim().toLowerCase();

    if (SYSTEM_ROLES.has(role)) {
      const text = extractTextFromContent(msg.content).trim();
      if (text) systemParts.push(text);
      continue;
    }

    if (role === "user") {
      normalizedMessages.push({
        role: "user",
        content: normalizeUserContentBlocks(msg.content),
        timestamp: baseTs + i,
      });
      continue;
    }

    if (role === "assistant") {
      const contentBlocks = [];
      let restoredProviderReplay;

      const reasoningText =
        typeof msg.reasoning_content === "string"
          ? msg.reasoning_content
          : typeof msg.thinking === "string"
            ? msg.thinking
            : "";
      if (reasoningText) {
        contentBlocks.push({
          type: "thinking",
          thinking: reasoningText,
          ...(typeof msg.thinking_signature === "string"
            ? { thinkingSignature: msg.thinking_signature }
            : {}),
        });
      }

      const textContent = extractTextFromContent(msg.content);
      if (textContent) {
        contentBlocks.push({
          type: "text",
          text: textContent,
          ...(typeof msg.text_signature === "string"
            ? { textSignature: msg.text_signature }
            : {}),
        });
      }

      if (Array.isArray(msg.tool_calls)) {
        for (let tcIdx = 0; tcIdx < msg.tool_calls.length; tcIdx += 1) {
          const tc = msg.tool_calls[tcIdx];
          if (!tc || typeof tc !== "object") continue;
          const fn = tc.function || {};
          const tcId = String(tc.id || `call_${i}_${tcIdx}`);
          const tcName = String(fn.name || tc.name || "unknown_tool");
          const tcArgs = safeParseJsonArguments(fn.arguments ?? tc.arguments);
          toolCallNameById.set(tcId, tcName);

          const cachedMeta = replayStore?.lookupToolCallMeta?.(sessionId, tcId) || null;
          if (cachedMeta?.thinkingBlocks?.length && !contentBlocks.some((b) => b.type === "thinking")) {
            contentBlocks.unshift(...cachedMeta.thinkingBlocks);
          }
          if (cachedMeta?.providerReplay && !restoredProviderReplay) {
            restoredProviderReplay = cachedMeta.providerReplay;
          }
          const thoughtSignature =
            typeof tc.thought_signature === "string"
              ? tc.thought_signature
              : typeof fn.thought_signature === "string"
                ? fn.thought_signature
                : cachedMeta?.thoughtSignature;

          contentBlocks.push({
            type: "toolCall",
            id: tcId,
            name: tcName,
            arguments: tcArgs,
            ...(thoughtSignature ? { thoughtSignature } : {}),
          });
        }
      }

      normalizedMessages.push({
        role: "assistant",
        content: contentBlocks,
        api: modelMeta.api || "openai-completions",
        provider: modelMeta.provider || "unknown",
        model: modelMeta.id || "unknown",
        ...(restoredProviderReplay ? { providerReplay: restoredProviderReplay } : {}),
        usage: {
          input: 0,
          output: 0,
          cacheRead: 0,
          cacheWrite: 0,
          totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
        },
        stopReason: Array.isArray(msg.tool_calls) && msg.tool_calls.length > 0 ? "toolUse" : "stop",
        timestamp: baseTs + i,
      });
      continue;
    }

    if (role === "tool") {
      const toolCallId = String(msg.tool_call_id || msg.id || "");
      if (!toolCallId) {
        const err = new Error(`Message at index ${i} has role="tool" without tool_call_id`);
        err.statusCode = 400;
        err.code = "invalid_tool_message";
        throw err;
      }
      const toolName =
        String(msg.name || "").trim() ||
        toolCallNameById.get(toolCallId) ||
        "tool";
      const textResult =
        typeof msg.content === "string"
          ? msg.content
          : Array.isArray(msg.content)
            ? extractTextFromContent(msg.content)
            : JSON.stringify(msg.content ?? "");

      normalizedMessages.push({
        role: "toolResult",
        toolCallId,
        toolName,
        content: [{ type: "text", text: textResult }],
        isError: Boolean(msg.is_error),
        timestamp: baseTs + i,
      });
      continue;
    }

    const err = new Error(`Unsupported message role "${role}" at index ${i}`);
    err.statusCode = 400;
    err.code = "unsupported_role";
    throw err;
  }

  if (normalizedMessages.length === 0) {
    const err = new Error("messages must include at least one non-system message");
    err.statusCode = 400;
    err.code = "invalid_messages";
    throw err;
  }

  return {
    systemPrompt: systemParts.length > 0 ? systemParts.join("\n\n") : undefined,
    messages: normalizedMessages,
  };
}

/**
 * Converts OpenAI `tools[]` into OpenClaw `Tool[]`:
 *   { name, description, parameters, strict? }
 */
export function normalizeOpenAiTools(tools) {
  if (!Array.isArray(tools) || tools.length === 0) return undefined;
  const out = [];
  for (let i = 0; i < tools.length; i += 1) {
    const item = tools[i];
    if (!item || typeof item !== "object") continue;
    const fn = item.type === "function" && item.function ? item.function : item;
    const name = String(fn.name || "").trim();
    if (!name) {
      const err = new Error(`Tool at index ${i} is missing function.name`);
      err.statusCode = 400;
      err.code = "invalid_tools";
      throw err;
    }
    const parameters =
      fn.parameters && typeof fn.parameters === "object"
        ? fn.parameters
        : { type: "object", properties: {} };
    out.push({
      name,
      description: String(fn.description || ""),
      parameters,
      ...(typeof fn.strict === "boolean" ? { strict: fn.strict } : {}),
    });
  }
  return out.length > 0 ? out : undefined;
}

/**
 * Normalizes OpenAI `tool_choice` into the shape expected by OpenClaw transports:
 *   "auto" | "none" | "required" | { type: "function", function: { name } }
 */
export function normalizeToolChoice(toolChoice) {
  if (toolChoice === undefined || toolChoice === null) return undefined;
  if (typeof toolChoice === "string") {
    const normalized = toolChoice.trim().toLowerCase();
    if (normalized === "auto" || normalized === "none" || normalized === "required") {
      return normalized;
    }
    if (normalized === "any") {
      return "required";
    }
  }
  if (typeof toolChoice === "object" && !Array.isArray(toolChoice)) {
    if (toolChoice.type === "function") {
      const fnName = toolChoice.function?.name || toolChoice.name;
      if (fnName) {
        return { type: "function", function: { name: String(fnName) } };
      }
    }
  }
  const err = new Error(`Unsupported tool_choice value: ${JSON.stringify(toolChoice)}`);
  err.statusCode = 400;
  err.code = "invalid_tool_choice";
  throw err;
}

/**
 * Normalizes OpenAI `response_format` into `SimpleStreamOptions.responseFormat`.
 */
export function normalizeResponseFormat(responseFormat) {
  if (!responseFormat || typeof responseFormat !== "object") return undefined;
  const type = String(responseFormat.type || "").trim();
  if (type === "text" || type === "json_object") {
    return { type };
  }
  if (type === "json_schema" && responseFormat.json_schema && typeof responseFormat.json_schema === "object") {
    return {
      type: "json_schema",
      json_schema: {
        name: String(responseFormat.json_schema.name || "response_schema"),
        schema: responseFormat.json_schema.schema || { type: "object" },
        ...(typeof responseFormat.json_schema.strict === "boolean"
          ? { strict: responseFormat.json_schema.strict }
          : {}),
        ...(typeof responseFormat.json_schema.description === "string"
          ? { description: responseFormat.json_schema.description }
          : {}),
      },
    };
  }
  const err = new Error(`Unsupported response_format: ${JSON.stringify(responseFormat)}`);
  err.statusCode = 400;
  err.code = "invalid_response_format";
  throw err;
}

export function mapStopReasonToFinishReason(stopReason, hasToolCalls = false) {
  if (hasToolCalls || stopReason === "toolUse") return "tool_calls";
  switch (stopReason) {
    case "stop":
      return "stop";
    case "length":
      return "length";
    case "content_filter":
    case "sensitive":
      return "content_filter";
    default:
      return "stop";
  }
}

export function mapUsageToOpenAiUsage(usage) {
  const input = Number(usage?.input ?? 0);
  const output = Number(usage?.output ?? 0);
  const cacheRead = Number(usage?.cacheRead ?? 0);
  const cacheWrite = Number(usage?.cacheWrite ?? 0);
  const totalTokens = Number(usage?.totalTokens ?? input + output + cacheRead + cacheWrite);
  const promptTokens = input + cacheRead;
  const costTotal = usage?.cost?.total !== undefined ? Number(usage.cost.total) : null;
  return {
    prompt_tokens: promptTokens,
    completion_tokens: output,
    total_tokens: totalTokens || promptTokens + output,
    prompt_tokens_details: {
      cached_tokens: cacheRead,
      cache_write_tokens: cacheWrite,
    },
    ...(costTotal !== null ? { cost_usd: costTotal } : {}),
  };
}

export function buildNonStreamingChatCompletionResponse({
  id,
  created,
  modelRef,
  assistantMsg,
  daltonMeta,
}) {
  const contentBlocks = Array.isArray(assistantMsg?.content) ? assistantMsg.content : [];
  const textParts = [];
  const thinkingParts = [];
  const toolCalls = [];

  for (let i = 0; i < contentBlocks.length; i += 1) {
    const block = contentBlocks[i];
    if (!block || typeof block !== "object") continue;
    if (block.type === "text" && typeof block.text === "string") {
      textParts.push(block.text);
    } else if (block.type === "thinking" && typeof block.thinking === "string") {
      thinkingParts.push(block.thinking);
    } else if (block.type === "toolCall") {
      const argString =
        typeof block.arguments === "string"
          ? block.arguments
          : JSON.stringify(block.arguments ?? {});
      toolCalls.push({
        id: String(block.id || `call_${i}`),
        type: "function",
        function: {
          name: String(block.name || "unknown_tool"),
          arguments: argString,
        },
        ...(block.thoughtSignature ? { thought_signature: block.thoughtSignature } : {}),
      });
    }
  }

  const text = textParts.join("");
  const reasoningContent = thinkingParts.join("\n");
  const hasToolCalls = toolCalls.length > 0;
  const finishReason = mapStopReasonToFinishReason(assistantMsg?.stopReason, hasToolCalls);

  const message = {
    role: "assistant",
    content: hasToolCalls && !text ? null : text,
    ...(reasoningContent ? { reasoning_content: reasoningContent } : {}),
    ...(hasToolCalls ? { tool_calls: toolCalls } : {}),
  };

  return {
    id,
    object: "chat.completion",
    created,
    model: modelRef,
    choices: [
      {
        index: 0,
        message,
        finish_reason: finishReason,
      },
    ],
    usage: mapUsageToOpenAiUsage(assistantMsg?.usage),
    dalton_meta: daltonMeta,
  };
}

/**
 * Stateful translator from OpenClaw `AssistantMessageEvent` stream into
 * OpenAI `chat.completion.chunk` objects.
 */
export function createSseDeltaTranslator({ id, created, modelRef, includeUsage = true }) {
  let roleSent = false;
  let nextToolCallIndex = 0;
  const contentIndexToToolIndex = new Map();
  const streamedArgsByContentIndex = new Map();
  let sawAnyToolCalls = false;

  function makeChunk(delta, finishReason = null, extra = {}) {
    return {
      id,
      object: "chat.completion.chunk",
      created,
      model: modelRef,
      choices: [
        {
          index: 0,
          delta,
          finish_reason: finishReason,
        },
      ],
      ...extra,
    };
  }

  function ensureRoleChunks() {
    if (roleSent) return [];
    roleSent = true;
    return [makeChunk({ role: "assistant" }, null)];
  }

  return {
    onEvent(event) {
      if (!event || typeof event !== "object") return [];

      switch (event.type) {
        case "start": {
          return ensureRoleChunks();
        }
        case "text_delta": {
          if (!event.delta) return [];
          return [...ensureRoleChunks(), makeChunk({ content: String(event.delta) }, null)];
        }
        case "thinking_delta": {
          if (!event.delta) return [];
          return [
            ...ensureRoleChunks(),
            makeChunk({ reasoning_content: String(event.delta) }, null),
          ];
        }
        case "toolcall_start": {
          sawAnyToolCalls = true;
          const cIdx = event.contentIndex ?? 0;
          const tcIdx = nextToolCallIndex++;
          contentIndexToToolIndex.set(cIdx, tcIdx);
          streamedArgsByContentIndex.set(cIdx, false);
          const partialBlock = event.partial?.content?.[cIdx] || {};
          const tcId = String(partialBlock.id || `call_${tcIdx}`);
          const tcName = String(partialBlock.name || "");
          return [
            ...ensureRoleChunks(),
            makeChunk(
              {
                tool_calls: [
                  {
                    index: tcIdx,
                    id: tcId,
                    type: "function",
                    function: {
                      name: tcName,
                      arguments: "",
                    },
                  },
                ],
              },
              null,
            ),
          ];
        }
        case "toolcall_delta": {
          sawAnyToolCalls = true;
          const cIdx = event.contentIndex ?? 0;
          let tcIdx = contentIndexToToolIndex.get(cIdx);
          if (tcIdx === undefined) {
            tcIdx = nextToolCallIndex++;
            contentIndexToToolIndex.set(cIdx, tcIdx);
          }
          if (!event.delta) return [];
          streamedArgsByContentIndex.set(cIdx, true);
          return [
            ...ensureRoleChunks(),
            makeChunk(
              {
                tool_calls: [
                  {
                    index: tcIdx,
                    function: {
                      arguments: String(event.delta),
                    },
                  },
                ],
              },
              null,
            ),
          ];
        }
        case "toolcall_end": {
          sawAnyToolCalls = true;
          const cIdx = event.contentIndex ?? 0;
          let tcIdx = contentIndexToToolIndex.get(cIdx);
          const tc = event.toolCall || event.partial?.content?.[cIdx] || {};
          const chunks = [...ensureRoleChunks()];
          if (tcIdx === undefined) {
            tcIdx = nextToolCallIndex++;
            contentIndexToToolIndex.set(cIdx, tcIdx);
            chunks.push(
              makeChunk(
                {
                  tool_calls: [
                    {
                      index: tcIdx,
                      id: String(tc.id || `call_${tcIdx}`),
                      type: "function",
                      function: {
                        name: String(tc.name || ""),
                        arguments: "",
                      },
                    },
                  ],
                },
                null,
              ),
            );
          }
          if (!streamedArgsByContentIndex.get(cIdx) && tc.arguments !== undefined) {
            const fullArgs =
              typeof tc.arguments === "string"
                ? tc.arguments
                : JSON.stringify(tc.arguments ?? {});
            if (fullArgs && fullArgs !== "{}") {
              chunks.push(
                makeChunk(
                  {
                    tool_calls: [
                      {
                        index: tcIdx,
                        function: {
                          arguments: fullArgs,
                        },
                      },
                    ],
                  },
                  null,
                ),
              );
            }
          }
          return chunks;
        }
        default:
          return [];
      }
    },

    onDone(finalMsg, daltonMeta) {
      const chunks = [...ensureRoleChunks()];
      const finishReason = mapStopReasonToFinishReason(finalMsg?.stopReason, sawAnyToolCalls);
      const usage = mapUsageToOpenAiUsage(finalMsg?.usage);
      chunks.push(
        makeChunk({}, finishReason, {
          ...(includeUsage ? { usage } : {}),
          dalton_meta: daltonMeta,
        }),
      );
      return chunks;
    },
  };
}
