/**
 * Rewrites sandbox and workspace-relative file links in outbound channel text
 * into sign-in-protected web deep links (or plain-text hints when no public
 * web URL or conversation thread is available).
 *
 * Keeps fenced code blocks and inline code spans untouched, preserves external
 * http(s) links, strips unroutable localhost preview links to plain text, and
 * normalizes bare sandbox absolute paths in prose to workspace-relative paths.
 */

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

const FENCE_OPEN_RE = /^ {0,3}(`{3,}|~{3,})/;
const FENCE_CLOSE_RE = /^ {0,3}(`{3,}|~{3,})[ \t]*$/;

const escapeRe = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const SANDBOX_ROOTS_ALTERNATION = SANDBOX_ROOT_PREFIXES.map((root) =>
  escapeRe(root.replace(/\/$/, "")),
).join("|");

/** Matches `/home/workspace/`, `home/workspace/`, `/home/daytona/`, `home/daytona/`. */
const SANDBOX_ROOT_RE = new RegExp(
  `^/?(?:${SANDBOX_ROOTS_ALTERNATION})(?:/|$)`,
);

/** Matches `file:///home/workspace/...` or `file:///home/daytona/...`. */
const FILE_SANDBOX_PROTO_RE = new RegExp(
  `^file://(?=/(?:${SANDBOX_ROOTS_ALTERNATION})(?:/|$))`,
  "i",
);

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

const WSREF_RE = /^\/?__wsref__\/([A-Za-z0-9][A-Za-z0-9_-]{0,127})\/(.+)$/;

/** Matches `name.ext:42`, `name.ext:40-55`, `name.ext:42:7` (extension must contain a letter). */
const LINE_SUFFIX_RE =
  /(\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8}):\d+(?:-\d+|:\d+)?$/;

/** Relative filename must have a non-dot stem before `.` and a 1–8 char extension containing a letter. */
const FILE_EXT_BASENAME_RE =
  /^[^/]*[^/.]\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8}$/;

const BARE_LOCALHOST_PORT_RE =
  /^(?:localhost|127\.\d{1,3}\.\d{1,3}\.\d{1,3}|\[::1\]|0\.0\.0\.0):\d+(?:[/?#].*)?$/i;

const TRAILING_QUOTED_TITLE_RE =
  /[ \t]+(?:"[^"\n]{0,512}"|'[^'\n]{0,512}')$/;

interface Span {
  text: string;
  code: boolean;
}

interface ParsedMarkdownLink {
  end: number;
  label: string;
  dest: string;
}

type DestinationClassification =
  | { kind: "keep" }
  | { kind: "local_preview" }
  | { kind: "workspace_file"; relPath: string; workspaceId?: string };

function closesFence(line: string, opener: string): boolean {
  const match = FENCE_CLOSE_RE.exec(line.replace(/\r?\n$/, ""));
  return (
    match !== null &&
    match[1]![0] === opener[0] &&
    match[1]!.length >= opener.length
  );
}

function splitLines(text: string): string[] {
  const lines: string[] = [];
  let start = 0;
  for (
    let end = text.indexOf("\n");
    end !== -1;
    end = text.indexOf("\n", start)
  ) {
    lines.push(text.slice(start, end + 1));
    start = end + 1;
  }
  if (start < text.length) {
    lines.push(text.slice(start));
  }
  return lines;
}

function pushSpan(spans: Span[], text: string, code: boolean): void {
  if (!text) return;
  const last = spans[spans.length - 1];
  if (last && last.code === code) {
    last.text += text;
  } else {
    spans.push({ text, code });
  }
}

function splitFences(content: string): Span[] {
  const lines = splitLines(content);
  const spans: Span[] = [];
  let opener: string | null = null;

  for (const line of lines) {
    if (opener === null) {
      const open = FENCE_OPEN_RE.exec(line);
      if (open) {
        opener = open[1]!;
        pushSpan(spans, line, true);
      } else {
        pushSpan(spans, line, false);
      }
      continue;
    }
    pushSpan(spans, line, true);
    if (closesFence(line, opener)) {
      opener = null;
    }
  }
  return spans;
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

function isLocalHostname(rawHostname: string): boolean {
  const host = rawHostname.trim().toLowerCase().replace(/^\[|\]$/g, "");
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

function decodeOnce(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function encodeFileParam(relPath: string): string {
  return encodeURIComponent(relPath).replace(/[()]/g, (ch) =>
    ch === "(" ? "%28" : "%29",
  );
}

/**
 * Normalizes a workspace-relative path: drops `.` and empty segments, folds `..`.
 * Returns `null` if the path escapes the workspace root (`..` at root) or collapses to empty.
 */
function normalizeRelativePath(raw: string): string | null {
  const isDir = raw.endsWith("/");
  const segments: string[] = [];
  for (const seg of raw.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      if (segments.length === 0) {
        return null;
      }
      segments.pop();
      continue;
    }
    segments.push(seg);
  }
  if (segments.length === 0) {
    return null;
  }
  const joined = segments.join("/");
  return isDir ? `${joined}/` : joined;
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
    inner = inner.replace(FILE_SANDBOX_PROTO_RE, "");
    const leadingStripped = inner.replace(/^\/+/, "");
    const rootMatch = SANDBOX_ROOT_RE.exec(leadingStripped);
    const withoutRoot = rootMatch
      ? leadingStripped.slice(rootMatch[0].length)
      : leadingStripped;
    const relPath = normalizeRelativePath(decodeOnce(withoutRoot));
    if (!relPath) {
      return { kind: "keep" };
    }
    return { kind: "workspace_file", relPath, workspaceId };
  }

  // 2. `file://` URLs: only rewrite sandbox roots (e.g. `file:///home/workspace/...`)
  let candidate = dest;
  let unwrappedFileProto = false;
  if (/^file:/i.test(candidate)) {
    if (!FILE_SANDBOX_PROTO_RE.test(candidate)) {
      return { kind: "keep" };
    }
    candidate = candidate.replace(FILE_SANDBOX_PROTO_RE, "");
    unwrappedFileProto = true;
  }

  // 3. Sandbox-rooted paths (`/home/workspace/x`, `/home/daytona/x`, `home/workspace/x`)
  const strippedForRoot = candidate
    .replace(/[?#].*$/, "")
    .replace(LINE_SUFFIX_RE, "$1");
  const sandboxRootMatch = SANDBOX_ROOT_RE.exec(strippedForRoot);
  if (sandboxRootMatch) {
    const rest = strippedForRoot.slice(sandboxRootMatch[0].length);
    const relPath = normalizeRelativePath(decodeOnce(rest));
    if (!relPath) {
      return { kind: "keep" };
    }
    return { kind: "workspace_file", relPath };
  }
  if (unwrappedFileProto) {
    return { kind: "keep" };
  }

  // 4. Relative paths with extension and no protocol
  const hasLine =
    LINE_SUFFIX_RE.test(candidate) && !/^[a-z][a-z0-9+.-]*:\/\//i.test(candidate);
  if (!hasLine && /^[a-z][a-z0-9+.-]*:/i.test(candidate)) {
    return { kind: "keep" };
  }
  if (
    candidate.startsWith("//") ||
    candidate.startsWith("/") ||
    candidate.startsWith("#") ||
    candidate.startsWith("?") ||
    /^www\./i.test(candidate)
  ) {
    return { kind: "keep" };
  }

  const rawRel = candidate
    .replace(/[?#].*$/, "")
    .replace(LINE_SUFFIX_RE, "$1");
  const decodedRel = decodeOnce(rawRel);
  if (decodedRel.endsWith("/")) {
    return { kind: "keep" };
  }
  const relPath = normalizeRelativePath(decodedRel);
  if (!relPath) {
    return { kind: "keep" };
  }
  const baseName = relPath.split("/").pop() ?? "";
  if (!FILE_EXT_BASENAME_RE.test(baseName)) {
    return { kind: "keep" };
  }
  return { kind: "workspace_file", relPath };
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

function parseMarkdownLinkAt(
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
  let i = bracketOpen + 1;
  let bracketDepth = 0;
  let bracketClose = -1;

  while (i < labelLimit) {
    const ch = text[i]!;
    if (ch === "\\" && i + 1 < labelLimit) {
      i += 2;
      continue;
    }
    if (ch === "`") {
      let run = 0;
      while (i + run < labelLimit && text[i + run] === "`") {
        run += 1;
      }
      const close = findClosingBacktickRun(text, i + run, run, labelLimit);
      if (close !== -1) {
        i = close + run;
        continue;
      }
      i += run;
      continue;
    }
    if (ch === "[") {
      bracketDepth += 1;
      i += 1;
      continue;
    }
    if (ch === "]") {
      if (bracketDepth === 0) {
        bracketClose = i;
        break;
      }
      bracketDepth -= 1;
      i += 1;
      continue;
    }
    i += 1;
  }

  if (bracketClose === -1 || bracketClose + 1 >= lineEnd || text[bracketClose + 1] !== "(") {
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

  // Angle-bracketed destination: `(<...>)`
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
    if (gt === -1) {
      return null;
    }
    const closeParen = skipOptionalTitleAndClosingParen(text, gt + 1, lineEnd);
    if (closeParen === -1) {
      return null;
    }
    return {
      end: closeParen + 1,
      label,
      dest: text.slice(pos + 1, gt),
    };
  }

  // Bare destination: `(...)`, allowing balanced nested parens and optional quoted title
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

function formatWorkspaceFileLink(
  label: string,
  relPath: string,
  workspaceId: string | undefined,
  publicWebBaseUrl: string | null,
  normalizedThreadId: string | null,
): string {
  if (publicWebBaseUrl) {
    if (workspaceId) {
      const url = `${publicWebBaseUrl}/chat/${encodeURIComponent(workspaceId)}?file=${encodeFileParam(relPath)}`;
      return `[${label || relPath}](${url})`;
    }
    if (normalizedThreadId) {
      const url = `${publicWebBaseUrl}/chat/t/${encodeURIComponent(normalizedThreadId)}?file=${encodeFileParam(relPath)}`;
      return `[${label || relPath}](${url})`;
    }
  }
  const hint = `（在网页工作区查看：${relPath}）`;
  return label ? `${label}${hint}` : hint;
}

function rewriteProseBlock(
  block: string,
  publicWebBaseUrl: string | null,
  normalizedThreadId: string | null,
): string {
  const out: string[] = [];
  let proseStart = 0;
  let i = 0;
  let currentLineEnd = -1;
  let nextCloseBracket = -1;

  const flushProse = (until: number): void => {
    if (until > proseStart) {
      out.push(replaceBareSandboxPaths(block.slice(proseStart, until)));
    }
  };

  while (i < block.length) {
    const ch = block[i]!;

    // Inline code span: preserve verbatim
    if (ch === "`") {
      let run = 0;
      while (i + run < block.length && block[i + run] === "`") {
        run += 1;
      }
      const close = findClosingBacktickRun(block, i + run, run);
      if (close !== -1) {
        flushProse(i);
        out.push(block.slice(i, close + run));
        i = close + run;
        proseStart = i;
        continue;
      }
      i += run;
      continue;
    }

    // Potential markdown link `[...](...)` or image `![...](...)`
    if (ch === "[" || (ch === "!" && i + 1 < block.length && block[i + 1] === "[")) {
      if (currentLineEnd < i) {
        const nl = block.indexOf("\n", i);
        currentLineEnd = nl === -1 ? block.length : nl;
      }
      if (nextCloseBracket < i) {
        nextCloseBracket = block.indexOf("]", i);
      }
      if (nextCloseBracket !== -1 && nextCloseBracket < currentLineEnd) {
        const parsed = parseMarkdownLinkAt(block, i, currentLineEnd);
        if (parsed) {
          const classified = classifyLinkDestination(parsed.dest);
          flushProse(i);
          if (classified.kind === "local_preview") {
            out.push(parsed.label);
          } else if (classified.kind === "workspace_file") {
            out.push(
              formatWorkspaceFileLink(
                parsed.label,
                classified.relPath,
                classified.workspaceId,
                publicWebBaseUrl,
                normalizedThreadId,
              ),
            );
          } else {
            out.push(block.slice(i, parsed.end));
          }
          i = parsed.end;
          proseStart = i;
          continue;
        }
      }
    }

    i += 1;
  }

  flushProse(block.length);
  return out.join("");
}

/**
 * Rewrites sandbox paths and unroutable local preview links in outbound channel
 * text into clickable web deep links (or plain-text workspace hints).
 */
export function rewriteSandboxLinks(
  text: string,
  webBaseUrl: string | null | undefined,
  threadId?: string | null,
): string {
  if (!text || typeof text !== "string") {
    return text;
  }
  const publicWebBaseUrl = normalizePublicWebBaseUrl(webBaseUrl);
  const normalizedThreadId = (threadId ?? "").trim() || null;

  return splitFences(text)
    .map((span) =>
      span.code
        ? span.text
        : rewriteProseBlock(span.text, publicWebBaseUrl, normalizedThreadId),
    )
    .join("");
}
