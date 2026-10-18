// integrations/openclaw/dalton-langalpha-broker/src/replay-state.mjs

/**
 * Bound LRU store for assistant turn replay state (e.g., Gemini thoughtSignature,
 * thinking blocks, or providerReplay checkpoints) keyed by `${sessionId}:${toolCallId}`
 * so multi-turn OpenAI Chat Completions requests that do not echo provider-specific
 * signature fields can still round-trip them within the same session.
 */
export function createReplayStore({ maxEntries = 512, ttlMs = 30 * 60 * 1000 } = {}) {
  const map = new Map();

  function prune(now = Date.now()) {
    for (const [k, v] of map.entries()) {
      if (now - v.ts > ttlMs) map.delete(k);
    }
    while (map.size > maxEntries) {
      const oldestKey = map.keys().next().value;
      if (oldestKey === undefined) break;
      map.delete(oldestKey);
    }
  }

  return {
    recordAssistantTurn(sessionId, assistantMsg) {
      if (!sessionId || !assistantMsg || !Array.isArray(assistantMsg.content)) return;
      const now = Date.now();
      prune(now);
      const thinkingBlocks = assistantMsg.content.filter((b) => b && b.type === "thinking");
      for (const block of assistantMsg.content) {
        if (block && block.type === "toolCall" && block.id) {
          map.set(`${sessionId}:${block.id}`, {
            ts: now,
            thoughtSignature: block.thoughtSignature,
            thinkingBlocks,
            providerReplay: assistantMsg.providerReplay,
          });
        }
      }
    },

    lookupToolCallMeta(sessionId, toolCallId) {
      if (!sessionId || !toolCallId) return null;
      const entry = map.get(`${sessionId}:${toolCallId}`);
      if (!entry) return null;
      if (Date.now() - entry.ts > ttlMs) {
        map.delete(`${sessionId}:${toolCallId}`);
        return null;
      }
      return entry;
    },
  };
}
