/**
 * Where a vendor's logo comes from, for every surface that draws one.
 *
 * Two sources, because the vendors differ in kind rather than in handling. An
 * LLM provider's mark is a file we ship: half of them are local runtimes with
 * no website to ask, and the art is better than any favicon. A connector's
 * mark is fetched live from the vendor's own site through the backend, because
 * the set changes without a frontend release and the good art is only
 * discoverable by reading their page.
 *
 * Both answer with the same `BrandArt`, so `BrandMark` never learns which kind
 * it is drawing. What they do not share is a lookup: provider ids and server
 * names are different keyspaces, and one map over both would silently hand a
 * connector named `openai` the model provider's logo.
 */
import { baseURL } from '@/pages/ChatAgent/utils/api/transport';

import iconOpenai from '@/assets/providers/openai.svg';
import iconAnthropic from '@/assets/providers/anthropic.svg';
import iconGemini from '@/assets/providers/gemini.svg';
import iconOpenrouter from '@/assets/providers/openrouter.svg';
import iconZai from '@/assets/providers/z-ai.svg';
import iconMinimax from '@/assets/providers/minimax.svg';
import iconQwen from '@/assets/providers/qwen.svg';
import iconKimi from '@/assets/providers/kimi.svg';
import iconDeepseek from '@/assets/providers/deepseek.svg';
import iconGroq from '@/assets/providers/groq.svg';
import iconCerebras from '@/assets/providers/cerebras.svg';
import iconOllama from '@/assets/providers/ollama.svg';
import iconLmStudio from '@/assets/providers/lmstudio.svg';
import iconVllm from '@/assets/providers/vllm.svg';
import iconXai from '@/assets/providers/xai.svg';

export type BrandArt = {
  src: string;
  /**
   * Drawn in black for a light ground, as a monochrome mark is, so a dark
   * surface has to turn it light. An accent it carries keeps its color.
   */
  ink?: boolean;
  /** A glyph with no ground of its own, so there is no edge to round, and a
   *  rounded box would only cut off whatever reaches its corners. */
  bare?: boolean;
};

/*
 * Each mark is the vendor's glyph without the app-icon tile it often sits on,
 * since every surface here gives it a ground of its own (the subagent badge's
 * white disc, a provider card) and a square inside that reads as a blot at
 * badge size. A family's own mark stands in where its models are what people
 * know (Gemini, Qwen, Kimi).
 */
const mark = (src: string, ink = false): BrandArt => ({ src, ink, bare: true });

const OPENAI = mark(iconOpenai, true);
const ANTHROPIC = mark(iconAnthropic, true);
const OPENROUTER = mark(iconOpenrouter, true);
const ZAI = mark(iconZai, true);
const MINIMAX = mark(iconMinimax);
const QWEN = mark(iconQwen);
const KIMI = mark(iconKimi, true);
const XAI = mark(iconXai, true);

/** Every manifest provider and its variants, which wear their parent's mark. */
const LLM_PROVIDER_ART: Record<string, BrandArt> = {
  openai: OPENAI,
  'codex-oauth': OPENAI,
  anthropic: ANTHROPIC,
  'claude-oauth': ANTHROPIC,
  // DALTON-HOOK(native-oauth-ui): P0N.3 Dalton provider brand icons. Muse has
  // no icon asset and falls back to its initials; xai-oauth wears xAI's mark.
  'antigravity-oauth': mark(iconGemini),
  'xai-oauth': XAI,
  gemini: mark(iconGemini),
  xai: XAI,
  'grok-oauth': XAI,
  openrouter: OPENROUTER,
  'z-ai': ZAI,
  'z-ai-coding': ZAI,
  'z-ai-cn': ZAI,
  'z-ai-cn-coding': ZAI,
  minimax: MINIMAX,
  'minimax-coding': MINIMAX,
  dashscope: QWEN,
  'dashscope-intl': QWEN,
  moonshot: KIMI,
  'moonshot-coding': KIMI,
  deepseek: mark(iconDeepseek),
  groq: mark(iconGroq),
  cerebras: mark(iconCerebras, true),
  ollama: mark(iconOllama, true),
  'lm-studio': mark(iconLmStudio, true),
  vllm: mark(iconVllm),
};

/** The bundled mark for an LLM provider id, or undefined for one we do not ship. */
export function llmProviderArt(provider: string): BrandArt | undefined {
  return Object.hasOwn(LLM_PROVIDER_ART, provider) ? LLM_PROVIDER_ART[provider] : undefined;
}

/**
 * The endpoint that proxies a shipped brokerage's own logo.
 *
 * Never a lookup: the backend holds the list of brokers it ships and whether
 * each one turned out to have usable art, and re-deciding that here would put
 * a second copy of both facts in the client. A broker with no mark 404s and
 * the row falls back, which is the same thing an unknown name does.
 *
 * Takes the vendor rather than its name so the three rows that resolve one off
 * an address hand it straight over. A row pointing somewhere we do not
 * recognise has no vendor and no art, and that is one answer here instead of
 * the same ternary at every call.
 */
export function brokerageArt(
  vendor: { name: string } | null | undefined,
): BrandArt | undefined {
  if (!vendor) return undefined;
  return {
    src: `${baseURL}/api/v1/mcp/brokerages/${encodeURIComponent(vendor.name)}/icon`,
  };
}

/**
 * The mark an MCP server declared for itself in the handshake.
 *
 * A fourth keyspace and the only one whose art we neither ship nor look up:
 * the server named it, the host fetched it, and what arrives here is a path
 * this origin already serves. There is nothing to decide, which is why this is
 * the same one-line shape as `bundleArt` rather than a lookup like the two
 * maps above.
 */
export function mcpServerArt(server: {
  icon_url?: string | null;
}): BrandArt | undefined {
  if (!server.icon_url) return undefined;
  return { src: `${baseURL}${server.icon_url}` };
}

/**
 * A shipped bundle's mark, when it wears someone else's brand.
 *
 * Only someone else's: our own bundles draw `LangAlphaMark`, which is inline
 * and takes the tile's own color, so there is nothing here to return for them.
 * A wrapper bundle is the opposite case in every way. The brand is not ours to
 * redraw, it changes without a frontend release, and the good art is only
 * discoverable by reading the vendor's page, so the manifest names a site and
 * the backend resolves it.
 *
 * A third keyspace, deliberately not folded into either map above: bundle
 * names, provider ids and server names collide by design (`openai` is a
 * plausible value in all three), and one lookup over them would hand a bundle
 * the model provider's logo.
 */
export function bundleArt(bundle: { icon_url?: string | null }): BrandArt | undefined {
  // The manifest decides, not the name.
  if (!bundle.icon_url) return undefined;
  return { src: `${baseURL}${bundle.icon_url}` };
}
