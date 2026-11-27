/**
 * Rewrites sandbox and workspace-relative file links in outbound channel text
 * into sign-in-protected web deep links (or plain-text hints when no public
 * web URL or conversation thread is available).
 *
 * Uses CommonMark + GFM table AST parsing (`mdast-util-from-markdown`) so
 * fenced blocks (including inside blockquotes and lists), unterminated quoted
 * fences, 4-space indented code blocks, inline code, reference link
 * definitions, titled links, escaped characters, autolinks, and nested images
 * follow Markdown semantics. Preserves rooted sandbox/machine paths in `?file=`
 * so the web frontend's `agentPaths` router resolves workspace folders, sibling
 * workspaces, and custom/former computer roots identically to web clicks.
 */

import { parseChannelMarkdown, type MdastNode } from "./text.js";

/**
 * Stock sandbox root prefixes (rootless form, trailing slash).
 * Must stay in sync with `SANDBOX_ROOT_PREFIXES` in
 * `web/src/pages/ChatAgent/utils/agentPaths.generated.ts` (generated from
 * `src/ptc_agent/core/paths.py`).
 */
export const SANDBOX_ROOT_PREFIXES = [
  "home/workspace/",
  "home/daytona/",
] as const;

const MAX_LINK_LABEL_LEN = 512;
const MAX_LINK_DEST_LEN = 4096;

const escapeRe = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const SANDBOX_ROOTS_ALTERNATION = SANDBOX_ROOT_PREFIXES.map((root) =>
  escapeRe(root.replace(/\/$/, "")),
).join("|");

/** Matches `/home/workspace/`, `home/workspace/`, `/home/daytona/`, `home/daytona/`. */
const SANDBOX_ROOT_RE = new RegExp(
  `^/?(${SANDBOX_ROOTS_ALTERNATION})(?:/|$)`,
);

/** Matches `file:///...` (triple-slash local file URI). */
const FILE_TRIPLE_SLASH_RE = /^file:\/\/(?=\/)/i;

/**
 * Matches bare absolute sandbox prefixes in prose (optional `file://` +
 * `/home/workspace/` or `/home/daytona/`) when followed by a path character.
 */
const BARE_SANDBOX_PREFIX_RE = new RegExp(
  `(?:[Ff][Ii][Ll][Ee]://)?/(?:${SANDBOX_ROOTS_ALTERNATION})/+(?=[^\\s<>"'\`()\\[\\]{}，。；：、！？（）【】《》「」『』“”‘’/])`,
  "g",
);

/** Characters that indicate a preceding URL/path rather than a standalone bare path. */
const BARE_PATH_PRECEDING_BLOCK_RE = /[A-Za-z0-9._~:/%-]/;

const BARE_PROSE_HTTP_URL_RE =
  /https?:\/\/[^\s<>"'`()[\]{}，。；：、！？（）【】《》「」『』“”‘’]+/gi;

const WSREF_RE = /^\/?__wsref__\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\/(.+)$/;

/** Matches `name.ext:42`, `name.ext:40-55`, `name.ext:42:7` (extension must contain a letter). */
const LINE_SUFFIX_RE =
  /(\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8}):\d+(?:-\d+|:\d+)?$/;

/** Filename must have a non-dot stem before `.` and a 1–8 char extension containing a letter. */
const FILE_EXT_BASENAME_RE =
  /^[^/]*[^/.]\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8}$/;

/** Common web domain TLDs that are not workspace file extensions, used to reject bare single-segment hostnames like `example.com`. */
const COMMON_HOST_TLDS = new Set([
  "com",
  "org",
  "net",
  "edu",
  "gov",
  "mil",
  "int",
  "io",
  "co",
  "ai",
  "app",
  "dev",
  "cn",
  "hk",
  "tw",
  "sg",
  "jp",
  "kr",
  "uk",
  "de",
  "fr",
  "au",
  "ca",
  "us",
  "eu",
  "br",
  "nl",
  "se",
  "ch",
  "it",
  "es",
  "ru",
  "xyz",
  "info",
  "biz",
  "me",
  "cc",
  "gg",
  "site",
  "online",
  "tech",
  "cloud",
  "pro",
  "top",
  "club",
  "news",
  "media",
  "live",
  "store",
  "shop",
  "finance",
  "capital",
  "fund",
  "group",
  "ltd",
  "inc",
]);

const BARE_LOCALHOST_PORT_RE =
  /^(?:(?:[A-Za-z0-9-]+\.)*localhost\.?|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\]|0\.0\.0\.0):\d+(?:[/?#].*)?$/i;

const TRAILING_QUOTED_TITLE_RE =
  /[ \t]+(?:"[^"\n]{0,512}"|'[^'\n]{0,512}')$/;

interface ParsedMarkdownLink {
  end: number;
  label: string;
  dest: string;
}

type DestinationClassification =
  | { kind: "keep" }
  | { kind: "local_preview" }
  | {
      kind: "workspace_file";
      /** Path placed in `?file=` (preserves rooted form so frontend `agentPaths` resolves workspace/sibling/custom roots). */
      deepLinkPath: string;
      /** Human-readable path shown in plain-text fallback hints (strips stock sandbox root). */
      displayPath: string;
      workspaceId?: string;
    };

interface TextSegmentAction {
  start: number;
  end: number;
  kind: "edit" | "protect";
  replacement?: string;
  clampToCursor?: boolean;
}

interface DefinitionInfo {
  start: number;
  end: number;
  identifier: string;
  rawLabel: string;
  url: string;
  title: string | null;
  classification: DestinationClassification;
  referencedCount: number;
}

function isLocalHostname(rawHostname: string): boolean {
  const host = rawHostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.+$/, "");
  if (!host) return true;
  if (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "127.0.0.1" ||
    host === "::1" ||
    host === "0.0.0.0" ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)
  ) {
    return true;
  }
  return false;
}

function normalizePublicWebBaseUrl(
  webBaseUrl: string | null | undefined,
): string | null {
  const trimmed = (webBaseUrl ?? "").trim();
  if (!trimmed) return null;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return null;
  }
  if (isLocalHostname(parsed.hostname)) {
    return null;
  }
  return trimmed.replace(/\/+$/, "");
}

function isLocalPreviewUrl(dest: string): boolean {
  if (BARE_LOCALHOST_PORT_RE.test(dest)) {
    return true;
  }
  if (!/^https?:\/\//i.test(dest)) {
    return false;
  }
  try {
    const parsed = new URL(dest);
    return isLocalHostname(parsed.hostname);
  } catch {
    return false;
  }
}

function neutralizeLocalUrl(url: string): string {
  return url.trim().replace(/^https?:\/\//i, "");
}

function decodeOnce(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Encodes the `?file=` query parameter using `URLSearchParams` rules (so `+`
 * becomes `%2B` and parens become `%28`/`%29`), replacing space `+` with `%20`
 * for clean Markdown links. Round-trips identically through frontend
 * `URLSearchParams.get("file")` (`useFileDeepLink.ts`) and matches `fileHref`
 * (`CommandPalette/rows.ts`).
 */
function encodeFileParam(filePath: string): string {
  return new URLSearchParams({ file: filePath })
    .toString()
    .slice("file=".length)
    .replace(/\+/g, "%20");
}

/**
 * Normalizes path segments: drops `.` and empty segments, folds `..`.
 * When `allowSingleLeadingClimb` is true, permits at most one leading `../`
 * (for sibling workspace folder references such as `../NVDA/results/report.md`,
 * matching `agentPaths.ts:141,346`); deeper climbs return `null`.
 */
function normalizePathSegments(
  raw: string,
  options: { allowSingleLeadingClimb?: boolean } = {},
): string | null {
  const isDir = raw.endsWith("/");
  const segments: string[] = [];
  let leadingClimbs = 0;

  for (const seg of raw.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      if (segments.length > 0 && segments[segments.length - 1] !== "..") {
        segments.pop();
        continue;
      }
      if (options.allowSingleLeadingClimb && leadingClimbs === 0) {
        leadingClimbs += 1;
        segments.push("..");
        continue;
      }
      return null;
    }
    segments.push(seg);
  }

  if (segments.length === 0) {
    return null;
  }
  if (leadingClimbs > 0 && segments.length < 2) {
    return null;
  }
  const joined = segments.join("/");
  return isDir ? `${joined}/` : joined;
}

function hasFileExtension(pathStr: string): boolean {
  const baseName = pathStr.split("/").pop() ?? "";
  return FILE_EXT_BASENAME_RE.test(baseName);
}

function isBareSingleSegmentHostname(pathStr: string): boolean {
  if (pathStr.includes("/")) return false;
  const dot = pathStr.lastIndexOf(".");
  if (dot <= 0) return false;
  const ext = pathStr.slice(dot + 1).toLowerCase();
  return COMMON_HOST_TLDS.has(ext);
}

function classifyLinkDestination(rawDest: string): DestinationClassification {
  const dest = rawDest.trim();
  if (!dest) {
    return { kind: "keep" };
  }

  if (isLocalPreviewUrl(dest)) {
    return { kind: "local_preview" };
  }

  // 1. Cross-workspace `__wsref__/<workspaceId>/<path>` (including legacy stale prefixes)
  const wsMatch = WSREF_RE.exec(dest);
  if (wsMatch) {
    const workspaceId = wsMatch[1]!;
    let inner = wsMatch[2]!.replace(/[?#].*$/, "");
    inner = inner.replace(LINE_SUFFIX_RE, "$1");
    inner = inner.replace(FILE_TRIPLE_SLASH_RE, "");
    const leadingStripped = inner.replace(/^\/+/, "");
    const rootMatch = SANDBOX_ROOT_RE.exec(leadingStripped);
    const withoutRoot = rootMatch
      ? leadingStripped.slice(rootMatch[0].length)
      : leadingStripped;
    const relPath = normalizePathSegments(decodeOnce(withoutRoot));
    if (!relPath) {
      return { kind: "keep" };
    }
    return {
      kind: "workspace_file",
      deepLinkPath: relPath,
      displayPath: relPath,
      workspaceId,
    };
  }

  // 2. `file://` URLs: unwrap `file://` when followed by `/`
  let candidate = dest;
  let unwrappedFileProto = false;
  if (/^file:/i.test(candidate)) {
    if (!FILE_TRIPLE_SLASH_RE.test(candidate)) {
      return { kind: "keep" };
    }
    candidate = candidate.replace(FILE_TRIPLE_SLASH_RE, "");
    unwrappedFileProto = true;
  }

  // 3. Stock sandbox-rooted paths (`/home/workspace/...`, `/home/daytona/...`, `home/workspace/...`)
  //    Preserve the rooted form in `deepLinkPath` so frontend `computeAgentArtifactRouting`
  //    folds off the workspace's own folder (e.g. `Research/`) or routes to a sibling workspace
  //    (e.g. `NVDA/`) identically to web clicks (`agentPaths.ts:120,340,861`).
  const strippedForRoot = candidate
    .replace(/[?#].*$/, "")
    .replace(LINE_SUFFIX_RE, "$1");
  const sandboxRootMatch = SANDBOX_ROOT_RE.exec(strippedForRoot);
  if (sandboxRootMatch) {
    const rootName = sandboxRootMatch[1]!;
    const rest = strippedForRoot.slice(sandboxRootMatch[0].length);
    const relPath = normalizePathSegments(decodeOnce(rest));
    if (!relPath) {
      return { kind: "keep" };
    }
    return {
      kind: "workspace_file",
      deepLinkPath: `/${rootName}/${relPath}`,
      displayPath: relPath,
    };
  }

  // 4. Custom/former computer root absolute file paths (e.g. `/home/someone/langalpha/Research/report.md`,
  //    `/srv/old/Research/report.md`, `file:///Users/alice/langalpha/Research/report.md`).
  //    Matches `filePaths.ts:138-163`: requires a file extension on the basename so web app routes
  //    (`/settings`, `/chat/t/123`) and extensionless system paths (`/etc/passwd`) are untouched.
  if (candidate.startsWith("/") && !candidate.startsWith("//")) {
    const rawAbs = strippedForRoot;
    const decodedAbs = decodeOnce(rawAbs);
    if (decodedAbs.endsWith("/") || decodedAbs.includes(":")) {
      return { kind: "keep" };
    }
    const normAbs = normalizePathSegments(decodedAbs);
    if (!normAbs || !hasFileExtension(normAbs)) {
      return { kind: "keep" };
    }
    const rootedPath = `/${normAbs}`;
    return {
      kind: "workspace_file",
      deepLinkPath: rootedPath,
      displayPath: rootedPath,
    };
  }
  if (unwrappedFileProto) {
    return { kind: "keep" };
  }

  // 5. Reject external schemes, protocol-relative URLs, anchors, queries, `www.`, emails, and colons
  const hasLine =
    LINE_SUFFIX_RE.test(candidate) && !/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate);
  if (!hasLine && /^[a-z][a-z0-9+.-]*:/i.test(candidate)) {
    return { kind: "keep" };
  }
  if (
    candidate.startsWith("//") ||
    candidate.startsWith("#") ||
    candidate.startsWith("?") ||
    /^www\./i.test(candidate) ||
    candidate.includes("@")
  ) {
    return { kind: "keep" };
  }

  const rawRel = candidate
    .replace(/[?#].*$/, "")
    .replace(LINE_SUFFIX_RE, "$1");
  const decodedRel = decodeOnce(rawRel);
  if (decodedRel.endsWith("/") || decodedRel.includes(":")) {
    return { kind: "keep" };
  }

  const explicitDotSlash =
    decodedRel.startsWith("./") || decodedRel.startsWith("../");
  const relPath = normalizePathSegments(decodedRel, {
    allowSingleLeadingClimb: true,
  });
  if (!relPath || !hasFileExtension(relPath)) {
    return { kind: "keep" };
  }
  if (!explicitDotSlash && isBareSingleSegmentHostname(relPath)) {
    return { kind: "keep" };
  }
  return {
    kind: "workspace_file",
    deepLinkPath: relPath,
    displayPath: relPath,
  };
}

function buildDeepLinkUrl(
  classification: Extract<DestinationClassification, { kind: "workspace_file" }>,
  publicWebBaseUrl: string,
  normalizedThreadId: string | null,
  normalizedWorkspaceId: string | null = null,
): string | null {
  if (classification.workspaceId) {
    if (!normalizedThreadId) {
      return null;
    }
    return `${publicWebBaseUrl}/chat/${encodeURIComponent(classification.workspaceId)}?file=${encodeFileParam(classification.deepLinkPath)}`;
  }
  if (normalizedThreadId) {
    return `${publicWebBaseUrl}/chat/t/${encodeURIComponent(normalizedThreadId)}?file=${encodeFileParam(classification.deepLinkPath)}`;
  }
  if (normalizedWorkspaceId) {
    return `${publicWebBaseUrl}/chat/${encodeURIComponent(normalizedWorkspaceId)}?file=${encodeFileParam(classification.deepLinkPath)}`;
  }
  return null;
}

function formatWorkspaceFileLink(
  label: string,
  classification: Extract<DestinationClassification, { kind: "workspace_file" }>,
  publicWebBaseUrl: string | null,
  normalizedThreadId: string | null,
  normalizedWorkspaceId: string | null = null,
): string {
  if (publicWebBaseUrl) {
    const url = buildDeepLinkUrl(
      classification,
      publicWebBaseUrl,
      normalizedThreadId,
      normalizedWorkspaceId,
    );
    if (url) {
      return `[${label || classification.displayPath}](${url})`;
    }
  }
  const hint = `（在网页工作区查看：${classification.displayPath}）`;
  return label ? `${label}${hint}` : hint;
}

/**
 * Format a standalone sign-in-protected web deep-link line for a workspace file
 * (used when group targets or DM deliverable fallbacks link to the web UI
 * instead of uploading binary attachments).
 */
export function buildWorkspaceFileDeepLinkLine(
  filename: string,
  workspacePath: string,
  webBaseUrl: string | null | undefined,
  threadId?: string | null,
  workspaceId?: string | null,
  currentWorkspaceId?: string | null,
): string {
  const cleanName = (filename || workspacePath.split("/").pop() || workspacePath).trim();
  const cleanPath = workspacePath.trim().replace(/^\/+/, "");
  const publicWebBaseUrl = normalizePublicWebBaseUrl(webBaseUrl);
  const normalizedThreadId = (threadId ?? "").trim() || null;
  const normalizedWorkspaceId = (workspaceId ?? "").trim() || null;
  const normalizedCurrentWorkspaceId = (currentWorkspaceId ?? "").trim() || null;
  const isCrossWorkspace = Boolean(
    normalizedWorkspaceId &&
      normalizedCurrentWorkspaceId &&
      normalizedWorkspaceId !== normalizedCurrentWorkspaceId,
  );
  const classification: Extract<
    DestinationClassification,
    { kind: "workspace_file" }
  > = {
    kind: "workspace_file",
    deepLinkPath: cleanPath,
    displayPath: cleanPath,
    ...(isCrossWorkspace && normalizedThreadId && normalizedWorkspaceId
      ? { workspaceId: normalizedWorkspaceId }
      : {}),
  };
  if (publicWebBaseUrl) {
    const url = buildDeepLinkUrl(
      classification,
      publicWebBaseUrl,
      normalizedThreadId,
      normalizedWorkspaceId,
    );
    if (url) {
      return `📎 ${cleanName} — 请前往网页查看：[${cleanName}](${url})`;
    }
  }
  return `📎 ${cleanName}（在网页工作区查看：${cleanPath}）`;
}

function findClosingBacktickRun(
  text: string,
  from: number,
  length: number,
  limit = text.length,
): number {
  let i = from;
  while (i < limit) {
    if (text[i] !== "`") {
      i += 1;
      continue;
    }
    let run = 0;
    while (i + run < limit && text[i + run] === "`") {
      run += 1;
    }
    if (run === length) {
      return i;
    }
    i += run;
  }
  return -1;
}

interface CodeRange {
  start: number;
  end: number;
}

function mergeSortedCodeRanges(ranges: CodeRange[]): CodeRange[] {
  if (ranges.length <= 1) return ranges;
  ranges.sort((a, b) => a.start - b.start || a.end - b.end);
  const merged: CodeRange[] = [ranges[0]!];
  for (let idx = 1; idx < ranges.length; idx += 1) {
    const cur = ranges[idx]!;
    const prev = merged[merged.length - 1]!;
    if (cur.start <= prev.end) {
      if (cur.end > prev.end) {
        prev.end = cur.end;
      }
    } else {
      merged.push({ start: cur.start, end: cur.end });
    }
  }
  return merged;
}

/**
 * Collects Markdown code zones from the CommonMark + GFM table AST BEFORE reference
 * definition / link collection strictly within Markdown block boundaries:
 * - `allCodeRanges` covers every `code` and `inlineCode` node (fenced, indented,
 *   list-nested, quoted, unterminated code blocks, and single-line or same-paragraph
 *   multiline backtick spans) without ever pairing stray backticks across paragraph,
 *   heading, list-item, blockquote, or table-cell boundaries.
 * - `protectedCodeRanges` excludes single-line inline code spans that serve purely as
 *   a Markdown link label (`[`foo`](dest)`) so the spaced-destination link fallback can
 *   still rewrite the outer link, while the fallback still reuses `allCodeRanges` to
 *   keep the inline code verbatim if the outer `[...](` turns out not to be a link.
 */
function collectAstCodeRanges(
  text: string,
  tree: MdastNode,
): { protectedCodeRanges: CodeRange[]; allCodeRanges: CodeRange[] } {
  const protectedRanges: CodeRange[] = [];
  const allRanges: CodeRange[] = [];
  const len = text.length;

  const isSingleLineSpacedLinkLabel = (start: number, end: number): boolean =>
    !text.slice(start, end).includes("\n") &&
    start > 0 &&
    text[start - 1] === "[" &&
    end + 1 < len &&
    text[end] === "]" &&
    text[end + 1] === "(";

  const collectAstCode = (node: MdastNode): void => {
    if (node.type === "code") {
      const offsets = getNodeOffsets(node);
      if (offsets) {
        const range = { start: offsets.start, end: offsets.end };
        allRanges.push(range);
        protectedRanges.push(range);
      }
      return;
    }
    if (node.type === "inlineCode") {
      const offsets = getNodeOffsets(node);
      if (offsets) {
        const range = { start: offsets.start, end: offsets.end };
        allRanges.push(range);
        if (!isSingleLineSpacedLinkLabel(offsets.start, offsets.end)) {
          protectedRanges.push(range);
        }
      }
      return;
    }
    if (Array.isArray(node.children)) {
      for (const child of node.children) {
        collectAstCode(child);
      }
    }
  };

  collectAstCode(tree);
  return {
    protectedCodeRanges: mergeSortedCodeRanges(protectedRanges),
    allCodeRanges: mergeSortedCodeRanges(allRanges),
  };
}

function firstCodeRangeIndexEndingAfter(
  offset: number,
  ranges: readonly CodeRange[],
): number {
  let lo = 0;
  let hi = ranges.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ranges[mid]!.end <= offset) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  return lo;
}

function startsInsideCodeRange(
  start: number,
  ranges: readonly CodeRange[],
): boolean {
  let lo = 0;
  let hi = ranges.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const r = ranges[mid]!;
    if (r.end <= start) {
      lo = mid + 1;
    } else if (r.start > start) {
      hi = mid - 1;
    } else {
      return true;
    }
  }
  return false;
}

function findMatchingBracketClose(
  text: string,
  bracketOpen: number,
  limit: number,
): number {
  let i = bracketOpen + 1;
  let depth = 0;
  while (i < limit) {
    const ch = text[i]!;
    if (ch === "\\" && i + 1 < limit) {
      i += 2;
      continue;
    }
    if (ch === "`") {
      let run = 0;
      while (i + run < limit && text[i + run] === "`") {
        run += 1;
      }
      const close = findClosingBacktickRun(text, i + run, run, limit);
      i = close !== -1 ? close + run : i + run;
      continue;
    }
    if (ch === "[") {
      depth += 1;
      i += 1;
      continue;
    }
    if (ch === "]") {
      if (depth === 0) {
        return i;
      }
      depth -= 1;
      i += 1;
      continue;
    }
    i += 1;
  }
  return -1;
}

function skipOptionalTitleAndClosingParen(
  text: string,
  from: number,
  lineEnd: number,
): number {
  let pos = from;
  while (pos < lineEnd && (text[pos] === " " || text[pos] === "\t")) {
    pos += 1;
  }
  if (pos < lineEnd && text[pos] === ")") {
    return pos;
  }
  if (pos === from || pos >= lineEnd) {
    return -1;
  }
  const quote = text[pos]!;
  if (quote === '"' || quote === "'") {
    pos += 1;
    while (pos < lineEnd) {
      if (text[pos] === "\\" && pos + 1 < lineEnd) {
        pos += 2;
        continue;
      }
      if (text[pos] === quote) {
        pos += 1;
        break;
      }
      pos += 1;
    }
  } else if (quote === "(") {
    pos += 1;
    while (pos < lineEnd) {
      if (text[pos] === "\\" && pos + 1 < lineEnd) {
        pos += 2;
        continue;
      }
      if (text[pos] === ")") {
        pos += 1;
        break;
      }
      pos += 1;
    }
  } else {
    return -1;
  }
  while (pos < lineEnd && (text[pos] === " " || text[pos] === "\t")) {
    pos += 1;
  }
  return pos < lineEnd && text[pos] === ")" ? pos : -1;
}

/**
 * Fallback parser for agent-emitted links whose bare destination contains
 * unescaped spaces (e.g. `[季报](results/中文 报告 (1).pdf)`), which CommonMark
 * leaves as plain text rather than `link` AST nodes.
 */
function parseSpacedMarkdownLinkAt(
  text: string,
  start: number,
  lineEnd: number,
): ParsedMarkdownLink | null {
  const isImage = text[start] === "!";
  const bracketOpen = isImage ? start + 1 : start;
  if (bracketOpen >= lineEnd || text[bracketOpen] !== "[") {
    return null;
  }

  const labelLimit = Math.min(lineEnd, bracketOpen + 1 + MAX_LINK_LABEL_LEN);
  const bracketClose = findMatchingBracketClose(text, bracketOpen, labelLimit);
  if (
    bracketClose === -1 ||
    bracketClose + 1 >= lineEnd ||
    text[bracketClose + 1] !== "("
  ) {
    return null;
  }

  const label = text.slice(bracketOpen + 1, bracketClose);
  let pos = bracketClose + 2;
  while (pos < lineEnd && (text[pos] === " " || text[pos] === "\t")) {
    pos += 1;
  }
  if (pos >= lineEnd) {
    return null;
  }

  if (text[pos] === "<") {
    const destLimit = Math.min(lineEnd, pos + 1 + MAX_LINK_DEST_LEN);
    let j = pos + 1;
    let gt = -1;
    while (j < destLimit) {
      if (text[j] === "\\" && j + 1 < destLimit) {
        j += 2;
        continue;
      }
      if (text[j] === ">") {
        gt = j;
        break;
      }
      if (text[j] === "<") {
        return null;
      }
      j += 1;
    }
    if (gt === -1) return null;
    const closeParen = skipOptionalTitleAndClosingParen(text, gt + 1, lineEnd);
    if (closeParen === -1) return null;
    return {
      end: closeParen + 1,
      label,
      dest: text.slice(pos + 1, gt),
    };
  }

  const destLimit = Math.min(lineEnd, pos + MAX_LINK_DEST_LEN);
  let j = pos;
  let parenDepth = 0;
  let closeParen = -1;

  while (j < destLimit) {
    const ch = text[j]!;
    if (ch === "\\" && j + 1 < destLimit) {
      j += 2;
      continue;
    }
    if (
      (ch === '"' || ch === "'") &&
      j > pos &&
      (text[j - 1] === " " || text[j - 1] === "\t")
    ) {
      let q = j + 1;
      let closed = false;
      while (q < destLimit) {
        if (text[q] === "\\" && q + 1 < destLimit) {
          q += 2;
          continue;
        }
        if (text[q] === ch) {
          q += 1;
          closed = true;
          break;
        }
        q += 1;
      }
      j = closed ? q : j + 1;
      continue;
    }
    if (ch === "(") {
      parenDepth += 1;
      j += 1;
      continue;
    }
    if (ch === ")") {
      if (parenDepth === 0) {
        closeParen = j;
        break;
      }
      parenDepth -= 1;
      j += 1;
      continue;
    }
    j += 1;
  }

  if (closeParen === -1) {
    return null;
  }

  const rawInner = text.slice(pos, closeParen).trim();
  const dest = rawInner.replace(TRAILING_QUOTED_TITLE_RE, "").trim();
  return {
    end: closeParen + 1,
    label,
    dest,
  };
}

function replaceBareSandboxPaths(prose: string): string {
  return prose.replace(
    BARE_SANDBOX_PREFIX_RE,
    (match: string, offset: number, source: string) => {
      if (offset > 0 && BARE_PATH_PRECEDING_BLOCK_RE.test(source[offset - 1]!)) {
        return match;
      }
      return "";
    },
  );
}

/**
 * Processes plain prose (outside code and outside Markdown links):
 * 1. Keeps full external `http(s)://...` URLs verbatim (protecting query params
 *    such as `?path=/home/workspace/report.md`).
 * 2. Neutralizes bare `http(s)://localhost...` / `127.0.0.1` / `localhost.` URLs
 *    to plain text while preserving the address text.
 * 3. Strips stock sandbox prefixes from bare absolute paths in non-URL text.
 */
function rewritePlainProse(prose: string): string {
  if (!prose) return prose;
  const out: string[] = [];
  let cursor = 0;

  for (const match of prose.matchAll(BARE_PROSE_HTTP_URL_RE)) {
    const start = match.index ?? 0;
    let url = match[0]!;
    while (url.length > 0 && /[.,;:!?]$/.test(url)) {
      if (
        url.endsWith(".") &&
        /^https?:\/\/(?:[A-Za-z0-9-]+\.)*localhost\.$/i.test(url)
      ) {
        break;
      }
      url = url.slice(0, -1);
    }
    const end = start + url.length;
    if (start > cursor) {
      out.push(replaceBareSandboxPaths(prose.slice(cursor, start)));
    }
    if (isLocalPreviewUrl(url)) {
      out.push(neutralizeLocalUrl(url));
    } else {
      out.push(url);
    }
    cursor = end;
  }

  if (cursor < prose.length) {
    out.push(replaceBareSandboxPaths(prose.slice(cursor)));
  }
  return out.join("");
}

function rewriteUnprotectedGap(
  text: string,
  gapStart: number,
  gapEnd: number,
  astCodeRanges: readonly CodeRange[],
  publicWebBaseUrl: string | null,
  normalizedThreadId: string | null,
  normalizedWorkspaceId: string | null = null,
): string {
  if (gapEnd <= gapStart) return "";
  const out: string[] = [];
  let proseStart = gapStart;
  let i = gapStart;
  let currentLineEnd = -1;
  let nextCloseBracket = -1;
  let codeIdx = firstCodeRangeIndexEndingAfter(gapStart, astCodeRanges);

  const flushProse = (until: number): void => {
    if (until > proseStart) {
      out.push(rewritePlainProse(text.slice(proseStart, until)));
    }
  };

  while (i < gapEnd) {
    // Reuse AST code ranges verbatim instead of re-pairing backticks across blocks/paragraphs.
    while (codeIdx < astCodeRanges.length && astCodeRanges[codeIdx]!.end <= i) {
      codeIdx += 1;
    }
    const activeCode =
      codeIdx < astCodeRanges.length && astCodeRanges[codeIdx]!.start <= i
        ? astCodeRanges[codeIdx]!
        : null;
    if (activeCode) {
      const codeEnd = Math.min(activeCode.end, gapEnd);
      flushProse(i);
      out.push(text.slice(i, codeEnd));
      i = codeEnd;
      proseStart = i;
      continue;
    }

    const ch = text[i]!;

    // Spaced-destination markdown link/image fallback
    if (
      ch === "[" ||
      (ch === "!" && i + 1 < gapEnd && text[i + 1] === "[")
    ) {
      if (currentLineEnd < i) {
        const nl = text.indexOf("\n", i);
        currentLineEnd = nl === -1 || nl > gapEnd ? gapEnd : nl;
      }
      if (nextCloseBracket < i) {
        const cb = text.indexOf("]", i);
        nextCloseBracket = cb === -1 || cb >= gapEnd ? -1 : cb;
      }
      if (nextCloseBracket !== -1 && nextCloseBracket < currentLineEnd) {
        const parsed = parseSpacedMarkdownLinkAt(text, i, currentLineEnd);
        if (parsed) {
          const classified = classifyLinkDestination(parsed.dest);
          flushProse(i);
          if (classified.kind === "local_preview") {
            out.push(parsed.label);
          } else if (classified.kind === "workspace_file") {
            out.push(
              formatWorkspaceFileLink(
                parsed.label,
                classified,
                publicWebBaseUrl,
                normalizedThreadId,
                normalizedWorkspaceId,
              ),
            );
          } else {
            out.push(text.slice(i, parsed.end));
          }
          i = parsed.end;
          proseStart = i;
          continue;
        }
      }
    }

    i += 1;
  }

  flushProse(gapEnd);
  return out.join("");
}

function getNodeOffsets(node: MdastNode): { start: number; end: number } | null {
  const start = node.position?.start?.offset;
  const end = node.position?.end?.offset;
  if (typeof start !== "number" || typeof end !== "number" || end <= start) {
    return null;
  }
  return { start, end };
}

function extractRawBracketLabel(
  text: string,
  start: number,
  end: number,
  isImage: boolean,
): { label: string; bracketClose: number } | null {
  const bracketOpen = isImage ? start + 1 : start;
  if (bracketOpen >= end || text[bracketOpen] !== "[") {
    return null;
  }
  const close = findMatchingBracketClose(text, bracketOpen, end);
  if (close === -1) {
    return null;
  }
  return {
    label: text.slice(bracketOpen + 1, close),
    bracketClose: close,
  };
}

function formatDefinitionTitle(title: string | null): string {
  if (!title) return "";
  const escaped = title.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  return ` "${escaped}"`;
}

function expandRemovedDefinitionBounds(
  text: string,
  start: number,
  end: number,
): { start: number; end: number } {
  const atLineStart = start === 0 || text[start - 1] === "\n";
  if (atLineStart) {
    if (text.startsWith("\r\n", end)) {
      return { start, end: end + 2 };
    }
    if (text[end] === "\n") {
      return { start, end: end + 1 };
    }
    if (end === text.length && start > 0) {
      if (start >= 2 && text[start - 2] === "\r" && text[start - 1] === "\n") {
        return { start: start - 2, end };
      }
      if (text[start - 1] === "\n") {
        return { start: start - 1, end };
      }
    }
  }
  return { start, end };
}

function hasRewriteableSandboxImageDescendant(node: MdastNode): boolean {
  if (!Array.isArray(node.children)) return false;
  for (const child of node.children) {
    if (child.type === "image" && typeof child.url === "string") {
      const cls = classifyLinkDestination(child.url);
      if (cls.kind === "workspace_file" || cls.kind === "local_preview") {
        return true;
      }
    }
    if (hasRewriteableSandboxImageDescendant(child)) {
      return true;
    }
  }
  return false;
}

/**
 * Rewrites sandbox paths and unroutable local preview links in outbound channel
 * text into clickable web deep links (or plain-text workspace hints).
 */
export function rewriteSandboxLinks(
  text: string,
  webBaseUrl: string | null | undefined,
  threadId?: string | null,
  workspaceId?: string | null,
): string {
  if (!text || typeof text !== "string") {
    return text;
  }
  // Fast path: no URL/path punctuation at all (also short-circuits pathological bracket runs).
  if (!text.includes("/") && !text.includes(":") && !text.includes(".")) {
    return text;
  }

  const publicWebBaseUrl = normalizePublicWebBaseUrl(webBaseUrl);
  const normalizedThreadId = (threadId ?? "").trim() || null;
  const normalizedWorkspaceId = (workspaceId ?? "").trim() || null;

  const tree = parseChannelMarkdown(text);

  // Pass 0 (BT3): identify all code zones (fenced blocks, indented code, inline and multiline backtick spans)
  // BEFORE collecting reference definitions so `[x]: /home/workspace/...` inside code zones is never parsed
  // as an active definition or rewritten.
  const { protectedCodeRanges, allCodeRanges } = collectAstCodeRanges(text, tree);

  // Pass 1: collect all reference link definitions and count their references (excluding code zones).
  const definitionsById = new Map<string, DefinitionInfo>();
  const collectDefinitions = (node: MdastNode): void => {
    if (node.type === "code" || node.type === "inlineCode") {
      return;
    }
    if (node.type === "definition") {
      const offsets = getNodeOffsets(node);
      if (offsets && startsInsideCodeRange(offsets.start, protectedCodeRanges)) {
        return;
      }
      const id = (node.identifier ?? "").toLowerCase();
      if (offsets && id && !definitionsById.has(id)) {
        const extracted = extractRawBracketLabel(
          text,
          offsets.start,
          offsets.end,
          false,
        );
        const url = typeof node.url === "string" ? node.url : "";
        definitionsById.set(id, {
          start: offsets.start,
          end: offsets.end,
          identifier: id,
          rawLabel: extracted?.label ?? node.label ?? id,
          url,
          title: typeof node.title === "string" ? node.title : null,
          classification: classifyLinkDestination(url),
          referencedCount: 0,
        });
      }
      return;
    }
    if (Array.isArray(node.children)) {
      for (const child of node.children) {
        collectDefinitions(child);
      }
    }
  };
  collectDefinitions(tree);

  if (definitionsById.size > 0) {
    const countRefs = (node: MdastNode): void => {
      if (node.type === "code" || node.type === "inlineCode") {
        return;
      }
      if (node.type === "linkReference" || node.type === "imageReference") {
        const offsets = getNodeOffsets(node);
        if (offsets && startsInsideCodeRange(offsets.start, protectedCodeRanges)) {
          return;
        }
        const id = (node.identifier ?? "").toLowerCase();
        const def = definitionsById.get(id);
        if (def) {
          def.referencedCount += 1;
        }
      }
      if (Array.isArray(node.children)) {
        for (const child of node.children) {
          countRefs(child);
        }
      }
    };
    countRefs(tree);
  }

  // Pass 2: walk AST top-down and record non-overlapping edits and protected ranges.
  const actions: TextSegmentAction[] = protectedCodeRanges.map((r) => ({
    start: r.start,
    end: r.end,
    kind: "protect" as const,
  }));

  const visit = (node: MdastNode, insideOuterLink: boolean): void => {
    const offsets = getNodeOffsets(node);

    if (node.type === "code") {
      if (offsets) {
        actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
      }
      return;
    }

    if (node.type === "inlineCode") {
      if (offsets) {
        // Leave `[` + inlineCode + `](` uncovered so the spaced-destination link
        // fallback can rewrite links like `[`Q3 报告.md`](/home/workspace/Q3 报告.md)`.
        const isSpacedLinkLabel =
          offsets.start > 0 &&
          text[offsets.start - 1] === "[" &&
          text[offsets.end] === "]" &&
          text[offsets.end + 1] === "(";
        if (!isSpacedLinkLabel) {
          actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
        }
      }
      return;
    }

    if (node.type === "definition") {
      if (!offsets) return;
      if (startsInsideCodeRange(offsets.start, protectedCodeRanges)) {
        return;
      }
      const id = (node.identifier ?? "").toLowerCase();
      const info = definitionsById.get(id);
      if (!info || info.start !== offsets.start) {
        actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
        return;
      }
      if (info.classification.kind === "keep") {
        actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
        return;
      }
      if (info.classification.kind === "workspace_file") {
        // Referenced workspace definitions are always expanded inline at each
        // reference site (so splitTextChunks never splits a reference from its
        // definition) and their definition line is removed.
        if (info.referencedCount > 0) {
          const bounds = expandRemovedDefinitionBounds(
            text,
            offsets.start,
            offsets.end,
          );
          actions.push({
            start: bounds.start,
            end: bounds.end,
            kind: "edit",
            replacement: "",
            clampToCursor: true,
          });
        } else {
          const deepUrl = publicWebBaseUrl
            ? buildDeepLinkUrl(
                info.classification,
                publicWebBaseUrl,
                normalizedThreadId,
                normalizedWorkspaceId,
              )
            : null;
          if (deepUrl) {
            actions.push({
              start: offsets.start,
              end: offsets.end,
              kind: "edit",
              replacement: `[${info.rawLabel}]: ${deepUrl}${formatDefinitionTitle(info.title)}`,
            });
          } else {
            actions.push({
              start: offsets.start,
              end: offsets.end,
              kind: "edit",
              replacement: `${info.rawLabel}（在网页工作区查看：${info.classification.displayPath}）`,
            });
          }
        }
        return;
      }
      // local_preview definition
      if (info.referencedCount > 0) {
        const bounds = expandRemovedDefinitionBounds(
          text,
          offsets.start,
          offsets.end,
        );
        actions.push({
          start: bounds.start,
          end: bounds.end,
          kind: "edit",
          replacement: "",
          clampToCursor: true,
        });
      } else {
        actions.push({
          start: offsets.start,
          end: offsets.end,
          kind: "edit",
          replacement: neutralizeLocalUrl(info.url),
        });
      }
      return;
    }

    if (node.type === "linkReference" || node.type === "imageReference") {
      if (!offsets) return;
      if (startsInsideCodeRange(offsets.start, protectedCodeRanges)) {
        return;
      }
      const isImage = node.type === "imageReference";
      const id = (node.identifier ?? "").toLowerCase();
      const info = definitionsById.get(id);
      if (!info || info.classification.kind === "keep") {
        if (!isImage && hasRewriteableSandboxImageDescendant(node)) {
          const extracted = extractRawBracketLabel(
            text,
            offsets.start,
            offsets.end,
            false,
          );
          if (Array.isArray(node.children)) {
            for (const child of node.children) {
              visit(child, true);
            }
          }
          if (extracted) {
            actions.push({
              start: extracted.bracketClose,
              end: offsets.end,
              kind: "protect",
            });
          }
          return;
        }
        actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
        return;
      }
      const extracted = extractRawBracketLabel(
        text,
        offsets.start,
        offsets.end,
        isImage,
      );
      const refLabel = extracted?.label ?? node.alt ?? node.label ?? "";
      if (info.classification.kind === "workspace_file") {
        if (insideOuterLink) {
          actions.push({
            start: offsets.start,
            end: offsets.end,
            kind: "edit",
            replacement: refLabel || info.classification.displayPath,
          });
          return;
        }
        actions.push({
          start: offsets.start,
          end: offsets.end,
          kind: "edit",
          replacement: formatWorkspaceFileLink(
            refLabel,
            info.classification,
            publicWebBaseUrl,
            normalizedThreadId,
            normalizedWorkspaceId,
          ),
        });
        return;
      }
      // local_preview reference
      actions.push({
        start: offsets.start,
        end: offsets.end,
        kind: "edit",
        replacement: refLabel,
      });
      return;
    }

    if (node.type === "link") {
      if (!offsets) return;
      if (startsInsideCodeRange(offsets.start, protectedCodeRanges)) {
        return;
      }
      const url = typeof node.url === "string" ? node.url : "";
      const classification = classifyLinkDestination(url);
      const isAutolink =
        text[offsets.start] === "<" && text[offsets.end - 1] === ">";

      if (classification.kind === "local_preview") {
        if (isAutolink) {
          actions.push({
            start: offsets.start,
            end: offsets.end,
            kind: "edit",
            replacement: neutralizeLocalUrl(url),
          });
        } else {
          const extracted = extractRawBracketLabel(
            text,
            offsets.start,
            offsets.end,
            false,
          );
          actions.push({
            start: offsets.start,
            end: offsets.end,
            kind: "edit",
            replacement: extracted?.label ?? "",
          });
        }
        return;
      }

      if (classification.kind === "workspace_file") {
        const extracted = isAutolink
          ? null
          : extractRawBracketLabel(text, offsets.start, offsets.end, false);
        const label = extracted?.label ?? "";
        actions.push({
          start: offsets.start,
          end: offsets.end,
          kind: "edit",
          replacement: formatWorkspaceFileLink(
            label,
            classification,
            publicWebBaseUrl,
            normalizedThreadId,
            normalizedWorkspaceId,
          ),
        });
        return;
      }

      // External / kept link: check for nested sandbox images such as `[![chart](/home/workspace/fig.png)](https://...)`
      if (!isAutolink && hasRewriteableSandboxImageDescendant(node)) {
        const extracted = extractRawBracketLabel(
          text,
          offsets.start,
          offsets.end,
          false,
        );
        if (Array.isArray(node.children)) {
          for (const child of node.children) {
            visit(child, true);
          }
        }
        if (extracted) {
          actions.push({
            start: extracted.bracketClose,
            end: offsets.end,
            kind: "protect",
          });
        }
        return;
      }

      actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
      return;
    }

    if (node.type === "image") {
      if (!offsets) return;
      if (startsInsideCodeRange(offsets.start, protectedCodeRanges)) {
        return;
      }
      const url = typeof node.url === "string" ? node.url : "";
      const classification = classifyLinkDestination(url);
      if (classification.kind === "keep") {
        actions.push({ start: offsets.start, end: offsets.end, kind: "protect" });
        return;
      }
      const extracted = extractRawBracketLabel(
        text,
        offsets.start,
        offsets.end,
        true,
      );
      const label = extracted?.label ?? node.alt ?? "";
      if (classification.kind === "local_preview") {
        actions.push({
          start: offsets.start,
          end: offsets.end,
          kind: "edit",
          replacement: label,
        });
        return;
      }
      if (insideOuterLink) {
        // Cannot nest `[a](u1)` inside `[...](u2)`; keep alt text (or path) cleanly.
        actions.push({
          start: offsets.start,
          end: offsets.end,
          kind: "edit",
          replacement: label || classification.displayPath,
        });
        return;
      }
      actions.push({
        start: offsets.start,
        end: offsets.end,
        kind: "edit",
        replacement: formatWorkspaceFileLink(
          label,
          classification,
          publicWebBaseUrl,
          normalizedThreadId,
          normalizedWorkspaceId,
        ),
      });
      return;
    }

    if (Array.isArray(node.children)) {
      for (const child of node.children) {
        visit(child, insideOuterLink);
      }
    }
  };

  visit(tree, false);

  actions.sort((a, b) => a.start - b.start || b.end - a.end);

  // Pass 3: assemble final string, running spaced-link & bare-prose passes only on gaps.
  const out: string[] = [];
  let cursor = 0;

  for (const action of actions) {
    if (action.start < cursor) {
      if (action.clampToCursor && action.end > cursor) {
        if (action.kind === "edit") {
          out.push(action.replacement ?? "");
        } else {
          out.push(text.slice(cursor, action.end));
        }
        cursor = action.end;
      }
      continue;
    }
    if (action.start > cursor) {
      out.push(
        rewriteUnprotectedGap(
          text,
          cursor,
          action.start,
          allCodeRanges,
          publicWebBaseUrl,
          normalizedThreadId,
          normalizedWorkspaceId,
        ),
      );
    }
    if (action.kind === "edit") {
      out.push(action.replacement ?? "");
    } else {
      out.push(text.slice(action.start, action.end));
    }
    cursor = action.end;
  }

  if (cursor < text.length) {
    out.push(
      rewriteUnprotectedGap(
        text,
        cursor,
        text.length,
        allCodeRanges,
        publicWebBaseUrl,
        normalizedThreadId,
        normalizedWorkspaceId,
      ),
    );
  }

  return out.join("");
}
