import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { describe, expect, it } from "vitest";
import { splitTextByLimit } from "../src/channels/adapter.js";
import {
  chunkDiscordText,
  normalizeDiscordBold,
} from "../src/channels/discord/chunk.js";
import {
  buildFeishuPostMessageContent,
  buildStructuredCard,
  chunkFeishuCardMarkdown,
  chunkFeishuPostMarkdown,
  FEISHU_CARD_MAX_BYTES,
  FEISHU_POST_MAX_BYTES,
} from "../src/channels/feishu/format.js";
import {
  rewriteSandboxLinks,
  SANDBOX_ROOT_PREFIXES,
} from "../src/shared/sandbox-links.js";
import { buildSurfaceRules } from "../src/types.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const WEB_BASE = "https://app.ginlix.ai";
const THREAD_ID = "11111111-2222-3333-4444-555555555555";
const WS_ID = "20cc68e8-d057-41f4-9bb1-57aa8d310704";

describe("rewriteSandboxLinks", () => {
  it("keeps SANDBOX_ROOT_PREFIXES in sync with frontend agentPaths.generated.ts", () => {
    const generatedPath = resolve(
      __dirname,
      "../../../web/src/pages/ChatAgent/utils/agentPaths.generated.ts",
    );
    const source = readFileSync(generatedPath, "utf8");
    const match =
      /export const SANDBOX_ROOT_PREFIXES = \[([\s\S]*?)\] as const;/.exec(
        source,
      );
    expect(match).not.toBeNull();
    const frontendRoots = Array.from(
      match![1]!.matchAll(/['"]([^'"]+)['"]/g),
      (m) => m[1]!,
    );
    expect([...SANDBOX_ROOT_PREFIXES]).toEqual(frontendRoots);
  });

  it("rewrites /home/workspace and /home/daytona links preserving rooted paths for frontend agentPaths resolution", () => {
    const input = [
      "详见 [完整报告](/home/workspace/Research/results/report.md) 与 [同机工作区](/home/workspace/NVDA/results/nvda.md)。",
      "旧根：[旧报告](/home/daytona/archive/old.pdf)，配图：![收益曲线](/home/workspace/charts/equity.png)",
    ].join("\n");

    const expected = [
      `详见 [完整报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2FResearch%2Fresults%2Freport.md) 与 [同机工作区](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2FNVDA%2Fresults%2Fnvda.md)。`,
      `旧根：[旧报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fdaytona%2Farchive%2Fold.pdf)，配图：[收益曲线](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fcharts%2Fequity.png)`,
    ].join("\n");

    expect(rewriteSandboxLinks(input, `${WEB_BASE}/`, THREAD_ID)).toBe(expected);
  });

  it("rewrites custom computer roots, moved former roots, and single-level sibling climbs", () => {
    const input = [
      "[自定义根](/home/someone/langalpha/Research/results/report.md)",
      "[自定义file](file:///Users/alice/langalpha/Research/results/memo.pdf)",
      "[迁移前根](/srv/old/Research/results/q3.csv)",
      "[同机相对](../NVDA/results/summary.md)",
    ].join("\n");

    const expected = [
      `[自定义根](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fsomeone%2Flangalpha%2FResearch%2Fresults%2Freport.md)`,
      `[自定义file](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2FUsers%2Falice%2Flangalpha%2FResearch%2Fresults%2Fmemo.pdf)`,
      `[迁移前根](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fsrv%2Fold%2FResearch%2Fresults%2Fq3.csv)`,
      `[同机相对](${WEB_BASE}/chat/t/${THREAD_ID}?file=..%2FNVDA%2Fresults%2Fsummary.md)`,
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("rewrites file:///home/... sandbox links and leaves extensionless system file:// links untouched", () => {
    const input = [
      "[报告](file:///home/workspace/results/nvda_analysis.md)",
      "![图表](file:///home/daytona/work/charts/fig.png)",
      "[系统文件](file:///etc/passwd)",
    ].join("\n");

    const expected = [
      `[报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Fnvda_analysis.md)`,
      `[图表](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fdaytona%2Fwork%2Fcharts%2Ffig.png)`,
      "[系统文件](file:///etc/passwd)",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("rewrites __wsref__/<uuid>/... links when threadId is present and degrades uniformly when threadId is missing", () => {
    const input = [
      `[跨区报告](__wsref__/${WS_ID}/results/nvda_analysis.md)`,
      `[旧前缀1](__wsref__/${WS_ID}/file:///home/workspace/results/a.md)`,
      `[旧前缀2](/__wsref__/${WS_ID}//home/daytona/charts/fig.png)`,
    ].join("\n");

    const expectedWithThread = [
      `[跨区报告](${WEB_BASE}/chat/${WS_ID}?file=results%2Fnvda_analysis.md)`,
      `[旧前缀1](${WEB_BASE}/chat/${WS_ID}?file=results%2Fa.md)`,
      `[旧前缀2](${WEB_BASE}/chat/${WS_ID}?file=charts%2Ffig.png)`,
    ].join("\n");

    const expectedWithoutThread = [
      "跨区报告（在网页工作区查看：results/nvda_analysis.md）",
      "旧前缀1（在网页工作区查看：results/a.md）",
      "旧前缀2（在网页工作区查看：charts/fig.png）",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(
      expectedWithThread,
    );
    expect(rewriteSandboxLinks(input, WEB_BASE, null)).toBe(
      expectedWithoutThread,
    );
  });

  it("rewrites relative file paths with extensions and excludes emails, bare hostnames, colons, and non-file targets", () => {
    const input = [
      "[纪要](results/memo.md)",
      "[表格](./data/summary.xlsx)",
      "[模型](work/model.py:42)",
      "[章节](results/report.md#valuation)",
      "[邮箱](team@example.com)",
      "[邮址](mailto:team@example.com)",
      "[官网](example.com)",
      "[文档](docs.python.org)",
      "[状态](状态:ready.md)",
      "[设置](/settings)",
      "[会话链接](/chat/t/abc?file=results%2Fa.md)",
      "[目录](results/)",
      "[锚点](#summary)",
      "[普通括号](up 2.5)",
      "[说明](notes about things)",
      "[越界](../../etc/passwd)",
    ].join("\n");

    const expected = [
      `[纪要](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Fmemo.md)`,
      `[表格](${WEB_BASE}/chat/t/${THREAD_ID}?file=data%2Fsummary.xlsx)`,
      `[模型](${WEB_BASE}/chat/t/${THREAD_ID}?file=work%2Fmodel.py)`,
      `[章节](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.md)`,
      "[邮箱](team@example.com)",
      "[邮址](mailto:team@example.com)",
      "[官网](example.com)",
      "[文档](docs.python.org)",
      "[状态](状态:ready.md)",
      "[设置](/settings)",
      "[会话链接](/chat/t/abc?file=results%2Fa.md)",
      "[目录](results/)",
      "[锚点](#summary)",
      "[普通括号](up 2.5)",
      "[说明](notes about things)",
      "[越界](../../etc/passwd)",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("encodes +, Chinese, spaces, and parentheses using URLSearchParams rules without double-encoding", () => {
    const input = [
      "[加号](results/a+b.md)",
      "[季报](results/中文 报告 (1).pdf)",
      "[演示](<file:///home/workspace/results/Q3 deck.pptx>)",
      "[已编码](results/%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md)",
    ].join("\n");

    const expected = [
      `[加号](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Fa%2Bb.md)`,
      `[季报](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2F%E4%B8%AD%E6%96%87%20%E6%8A%A5%E5%91%8A%20%281%29.pdf)`,
      `[演示](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2FQ3%20deck.pptx)`,
      `[已编码](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2F%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md)`,
    ].join("\n");

    const actual = rewriteSandboxLinks(input, WEB_BASE, THREAD_ID);
    expect(actual).toBe(expected);

    // Round-trip check via URLSearchParams matching web `useFileDeepLink.ts`
    const plusUrl = new URL(
      `${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Fa%2Bb.md`,
    );
    expect(plusUrl.searchParams.get("file")).toBe("results/a+b.md");

    // Percent-encoded `#` and `?` in filenames survive gateway rewrite and single-decode via URLSearchParams
    const specialCharsInput =
      "[问题](/home/workspace/Research/results/issue%231.md) 与 [问答](/srv/old/Research/results/%E4%B8%AD%E6%96%87%20q%3Fa%232.md)";
    const specialCharsOut = rewriteSandboxLinks(specialCharsInput, WEB_BASE, THREAD_ID);
    const urls = Array.from(specialCharsOut.matchAll(/\((https?:\/\/[^)]+)\)/g)).map(
      (m) => new URL(m[1]!).searchParams.get("file"),
    );
    expect(urls).toEqual([
      "/home/workspace/Research/results/issue#1.md",
      "/srv/old/Research/results/中文 q?a#2.md",
    ]);
  });

  it("converts bare sandbox paths to relative paths while preserving external URLs including query params", () => {
    const input = [
      "已保存至 /home/workspace/results/report.md，图表在 /home/daytona/charts/fig.png。",
      "本地协议：file:///home/workspace/results/中文报告.pdf",
      "外部路径不动：https://example.com/home/workspace/report.md",
      "外部查询参数不动：https://example.com/download?path=/home/workspace/report.md",
      "非沙箱根不动：/var/home/workspace/other.md",
    ].join("\n");

    const expected = [
      "已保存至 results/report.md，图表在 charts/fig.png。",
      "本地协议：results/中文报告.pdf",
      "外部路径不动：https://example.com/home/workspace/report.md",
      "外部查询参数不动：https://example.com/download?path=/home/workspace/report.md",
      "非沙箱根不动：/var/home/workspace/other.md",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("strips localhost/127.0.0.1/localhost. links, autolinks, and neutralizes bare local URLs to plain text", () => {
    const input = [
      "查看 [交互看板](http://localhost:8000/dashboard) 或 [本地服务](http://127.0.0.1:3000/index.html)。",
      "端口简写 [短链](localhost:5173/preview)、[尾点](http://localhost.:8088/app) 与 [IPv6](http://[::1]:8088/app)。",
      "自动链接：<http://localhost:8000/dashboard>",
      "裸地址：http://localhost:8000/live、http://127.0.0.1:3000/report 与 http://localhost.:8088/v1。",
    ].join("\n");

    const expected = [
      "查看 交互看板 或 本地服务。",
      "端口简写 短链、尾点 与 IPv6。",
      "自动链接：localhost:8000/dashboard",
      "裸地址：localhost:8000/live、127.0.0.1:3000/report 与 localhost.:8088/v1。",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("preserves external http/https and mailto links and images verbatim", () => {
    const input = [
      "参考 [Reuters](https://www.reuters.com/article/123) 与 [HTTP](http://example.org/doc.pdf)。",
      "![logo](https://example.com/logo.png)",
      "[联系](mailto:team@example.com)",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(input);
  });

  it("protects fenced blocks, quoted tilde/unterminated fences, 4-space indented code, and inline code via Markdown AST", () => {
    const input = [
      "行内代码 `/home/workspace/results/report.md`、`[report](results/report.md)` 与 `http://localhost:8000/dev` 保持原样。",
      "双反引号 ``[chart](/home/daytona/fig.png)`` 也保持原样。",
      "标签含行内代码：[`report.md`](/home/workspace/results/report.md)",
      "",
      "    [缩进代码](/home/workspace/indented.md)",
      "    http://localhost:8000/indented",
      "",
      "> ~~~",
      "> [引用波浪](/home/workspace/quoted_tilde.md)",
      "> ~~~",
      "",
      "> ```python",
      "> [引用未闭合](/home/workspace/quoted_open.md)",
      "",
      "```markdown",
      "[报告](/home/workspace/results/report.md)",
      "/home/workspace/data/raw.csv",
      "[预览](http://localhost:8000/)",
      "```",
    ].join("\n");

    const expected = [
      "行内代码 `/home/workspace/results/report.md`、`[report](results/report.md)` 与 `http://localhost:8000/dev` 保持原样。",
      "双反引号 ``[chart](/home/daytona/fig.png)`` 也保持原样。",
      `标签含行内代码：[\`report.md\`](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md)`,
      "",
      "    [缩进代码](/home/workspace/indented.md)",
      "    http://localhost:8000/indented",
      "",
      "> ~~~",
      "> [引用波浪](/home/workspace/quoted_tilde.md)",
      "> ~~~",
      "",
      "> ```python",
      "> [引用未闭合](/home/workspace/quoted_open.md)",
      "",
      "```markdown",
      "[报告](/home/workspace/results/report.md)",
      "/home/workspace/data/raw.csv",
      "[预览](http://localhost:8000/)",
      "```",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("rewrites reference link definitions, titled links without merging title, escaped parens, and nested sandbox images", () => {
    const input = [
      "见 [完整报告][r1] 与 ![趋势图][r2]。",
      '带标题：[双引号](/home/workspace/report.md "Q3")、[括号标题](/home/workspace/report.md (Q3 summary))。',
      "转义括号：[备忘](results/memo\\(1\\).md)",
      "外链包图：[![图表](/home/workspace/charts/fig.png)](https://example.com/view)",
      "",
      '[r1]: /home/workspace/results/report.md "Q3"',
      "[r2]: /home/workspace/charts/trend.png",
    ].join("\n");

    const expected = [
      `见 [完整报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md) 与 [趋势图](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fcharts%2Ftrend.png)。`,
      `带标题：[双引号](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Freport.md)、[括号标题](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Freport.md)。`,
      `转义括号：[备忘](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Fmemo%281%29.md)`,
      "外链包图：[图表](https://example.com/view)",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID).trim()).toBe(expected);

    // Without threadId, reference links degrade to plain-text hints and remove dead definition lines.
    const degradedRef = rewriteSandboxLinks(
      "见 [完整报告][r1]。\n\n[r1]: /home/workspace/results/report.md",
      WEB_BASE,
      null,
    );
    expect(degradedRef.trim()).toBe(
      "见 完整报告（在网页工作区查看：results/report.md）。",
    );
  });

  it("falls back to plain-text workspace hints when threadId is missing or webBaseUrl is local/empty", () => {
    const input = [
      "[报告](/home/workspace/results/中文 报告.md)",
      `[跨区](__wsref__/${WS_ID}/results/other.md)`,
    ].join(" ");
    const expected =
      "报告（在网页工作区查看：results/中文 报告.md） 跨区（在网页工作区查看：results/other.md）";

    expect(rewriteSandboxLinks(input, WEB_BASE, null)).toBe(expected);
    expect(rewriteSandboxLinks(input, WEB_BASE, undefined)).toBe(expected);
    expect(rewriteSandboxLinks(input, WEB_BASE, "   ")).toBe(expected);

    for (const base of [
      "http://localhost:5173",
      "http://localhost.:8088",
      "http://127.0.0.1:8088",
      "http://[::1]:8088",
      "",
      null,
      undefined,
    ]) {
      expect(rewriteSandboxLinks(input, base, THREAD_ID)).toBe(expected);
    }
  });

  it("stays linear on prose containing many unmatched opening brackets", () => {
    const input = "[".repeat(80_000);
    const started = performance.now();
    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(input);
    expect(performance.now() - started).toBeLessThan(2_000);
  });

  it("documents Markdown link syntax, DM deliverable auto-attach, and send_message in buildSurfaceRules", () => {
    const rules = buildSurfaceRules("feishu");
    expect(rules).toContain("[Report](results/report.md)");
    expect(rules).toContain("Write or Edit");
    expect(rules).toContain("sign-in required");
    expect(rules).toContain("send_message(text=..., files=[...])");
    expect(rules).not.toContain("Do NOT paste local sandbox paths");
  });
});

describe("channel chunkers preserve rewritten Markdown links and underscores", () => {
  const sampleLink = `[报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.md)`;

  it("Weixin splitTextByLimit moves boundary-straddling links intact and degrades oversized single links", () => {
    const prefix = "中".repeat(1954);
    const chunks = splitTextByLimit(`${prefix}${sampleLink}`, 2000);
    expect(chunks).toHaveLength(2);
    expect(chunks[0]!.length).toBeLessThanOrEqual(2000);
    expect(chunks[1]!.length).toBeLessThanOrEqual(2000);
    expect(chunks[1]).toBe(sampleLink);

    const longName = `results/${"a".repeat(2100)}.md`;
    const oversizedLink = `[长报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=${encodeURIComponent(longName)})`;
    const degraded = splitTextByLimit(oversizedLink, 2000);
    expect(degraded.join("")).toContain("长报告（在网页工作区查看：results/");
    expect(degraded.every((c) => c.length <= 2000)).toBe(true);
  });

  it("Discord chunkDiscordText keeps links intact, degrades oversized links, and skips __ inside link targets", () => {
    const boldAndLink =
      "__重要__ 详见 [文档](https://app.ginlix.ai/chat/t/11111111-2222-3333-4444-555555555555?file=docs%2Fa__b__c.md)";
    expect(normalizeDiscordBold(boldAndLink)).toBe(
      "**重要** 详见 [文档](https://app.ginlix.ai/chat/t/11111111-2222-3333-4444-555555555555?file=docs%2Fa__b__c.md)",
    );

    const prefix = "中".repeat(1954);
    const chunks = chunkDiscordText(`${prefix}${sampleLink}`, { maxChars: 2000 });
    expect(chunks).toHaveLength(2);
    expect(chunks[0]!.length).toBeLessThanOrEqual(2000);
    expect(chunks[1]).toBe(sampleLink);

    const longLink = `[超长](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2F${"x".repeat(2050)}.md)`;
    const degraded = chunkDiscordText(longLink, { maxChars: 2000 });
    expect(degraded.join("")).toContain("超长（在网页工作区查看：results/");
    expect(degraded.every((c) => c.length <= 2000)).toBe(true);
  });

  it("Feishu post and card chunkers keep links intact across 30KB byte boundaries and degrade oversized single links", () => {
    // Reproduce post boundary (~30,684 serialized bytes + link exceeding 30,720 bytes)
    const postFiller = "a".repeat(30_600);
    const postChunks = chunkFeishuPostMarkdown(`${postFiller}${sampleLink}`);
    expect(postChunks.length).toBeGreaterThanOrEqual(2);
    expect(postChunks.some((c) => c.includes(sampleLink))).toBe(true);
    for (const chunk of postChunks) {
      expect(
        Buffer.byteLength(
          buildFeishuPostMessageContent({ messageText: chunk }),
          "utf8",
        ),
      ).toBeLessThanOrEqual(FEISHU_POST_MAX_BYTES);
    }

    // Card chunker keeps link intact across 30,720-byte card envelope boundary
    const cardFiller = "a".repeat(30_600);
    expect(
      Buffer.byteLength(JSON.stringify(buildStructuredCard(cardFiller)), "utf8"),
    ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    expect(
      Buffer.byteLength(
        JSON.stringify(buildStructuredCard(`${cardFiller}${sampleLink}`)),
        "utf8",
      ),
    ).toBeGreaterThan(FEISHU_CARD_MAX_BYTES);
    const cardChunks = chunkFeishuCardMarkdown(`${cardFiller}${sampleLink}`);
    expect(cardChunks.length).toBeGreaterThanOrEqual(2);
    expect(cardChunks.some((c) => c.includes(sampleLink))).toBe(true);
    for (const chunk of cardChunks) {
      expect(
        Buffer.byteLength(JSON.stringify(buildStructuredCard(chunk)), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    }

    // Single link exceeding maxBytes degrades cleanly to plain-text workspace hint
    const hugeLink = `[大报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2F${"z".repeat(400)}.md)`;
    const tinyChunks = chunkFeishuPostMarkdown(hugeLink, 350);
    expect(tinyChunks.join("")).toContain("大报告（在网页工作区查看：results/");
    for (const chunk of tinyChunks) {
      expect(
        Buffer.byteLength(
          buildFeishuPostMessageContent({ messageText: chunk }),
          "utf8",
        ),
      ).toBeLessThanOrEqual(350);
    }

    const tinyCardChunks = chunkFeishuCardMarkdown(hugeLink, { maxBytes: 350 });
    expect(tinyCardChunks.join("")).toContain("大报告（在网页工作区查看：results/");
    for (const chunk of tinyCardChunks) {
      expect(
        Buffer.byteLength(JSON.stringify(buildStructuredCard(chunk)), "utf8"),
      ).toBeLessThanOrEqual(350);
    }
  });

  it("preserves 550-char label links and multiline-label links intact across Feishu, Discord, and Weixin chunkers", () => {
    const longLabel = "标".repeat(550);
    const longLabelLink = rewriteSandboxLinks(
      `[${longLabel}](/home/workspace/results/report.md)`,
      WEB_BASE,
      THREAD_ID,
    );
    const multilineLink = rewriteSandboxLinks(
      `[第一行标题\n第二行补充说明](/home/workspace/results/report.md)`,
      WEB_BASE,
      THREAD_ID,
    );

    // 1. Weixin (`splitTextByLimit`, 2000-char boundary)
    const wxLong = splitTextByLimit(`${"a".repeat(1600)}${longLabelLink}`, 2000);
    expect(wxLong).toHaveLength(2);
    expect(wxLong[1]).toBe(longLabelLink);
    expect(wxLong.every((c) => c.length <= 2000)).toBe(true);

    const wxMulti = splitTextByLimit(`${"a".repeat(1980)}${multilineLink}`, 2000);
    expect(wxMulti).toHaveLength(2);
    expect(wxMulti[1]).toBe(multilineLink);
    expect(wxMulti.every((c) => c.length <= 2000)).toBe(true);

    // 2. Discord (`chunkDiscordText`, 2000-char boundary)
    const dcLong = chunkDiscordText(`${"a".repeat(1600)}${longLabelLink}`, { maxChars: 2000 });
    expect(dcLong).toHaveLength(2);
    expect(dcLong[1]).toBe(longLabelLink);
    expect(dcLong.every((c) => c.length <= 2000)).toBe(true);

    const dcMulti = chunkDiscordText(`${"a".repeat(1980)}${multilineLink}`, { maxChars: 2000 });
    expect(dcMulti).toHaveLength(2);
    expect(dcMulti[1]).toBe(multilineLink);
    expect(dcMulti.every((c) => c.length <= 2000)).toBe(true);

    // 3. Feishu post & card (`chunkFeishuPostMarkdown`, `chunkFeishuCardMarkdown`, 30KB byte boundary)
    const fsPostLong = chunkFeishuPostMarkdown(`${"a".repeat(30_000)}${longLabelLink}`);
    expect(fsPostLong.length).toBeGreaterThanOrEqual(2);
    expect(fsPostLong.some((c) => c.includes(longLabelLink))).toBe(true);
    for (const chunk of fsPostLong) {
      expect(
        Buffer.byteLength(buildFeishuPostMessageContent({ messageText: chunk }), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_POST_MAX_BYTES);
    }

    const fsCardLong = chunkFeishuCardMarkdown(`${"a".repeat(30_000)}${longLabelLink}`);
    expect(fsCardLong.length).toBeGreaterThanOrEqual(2);
    expect(fsCardLong.some((c) => c.includes(longLabelLink))).toBe(true);
    for (const chunk of fsCardLong) {
      expect(
        Buffer.byteLength(JSON.stringify(buildStructuredCard(chunk)), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    }

    const fsPostMulti = chunkFeishuPostMarkdown(`${"a".repeat(30_600)}${multilineLink}`);
    expect(fsPostMulti.length).toBeGreaterThanOrEqual(2);
    expect(fsPostMulti.some((c) => c.includes(multilineLink))).toBe(true);
    for (const chunk of fsPostMulti) {
      expect(
        Buffer.byteLength(buildFeishuPostMessageContent({ messageText: chunk }), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_POST_MAX_BYTES);
    }

    const fsCardMulti = chunkFeishuCardMarkdown(`${"a".repeat(30_600)}${multilineLink}`);
    expect(fsCardMulti.length).toBeGreaterThanOrEqual(2);
    expect(fsCardMulti.some((c) => c.includes(multilineLink))).toBe(true);
    for (const chunk of fsCardMulti) {
      expect(
        Buffer.byteLength(JSON.stringify(buildStructuredCard(chunk)), "utf8"),
      ).toBeLessThanOrEqual(FEISHU_CARD_MAX_BYTES);
    }
  });

  it("Discord chunkDiscordText preserves overlong Markdown links inside fenced code blocks without degrading", () => {
    const codeLine = `[样例](https://example.com/${"a".repeat(2100)})`;
    const input = ["```markdown", codeLine, "```"].join("\n");
    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(input);

    const chunks = chunkDiscordText(input, { maxChars: 2000 });
    expect(chunks.length).toBeGreaterThanOrEqual(2);
    expect(chunks.every((c) => c.length <= 2000)).toBe(true);
    const restored = chunks
      .map((chunk) => chunk.replace(/^```markdown\n/, "").replace(/\n```$/, ""))
      .join("");
    expect(restored).toBe(codeLine);
  });

  it("BT3: preserves reference link definitions inside same-paragraph multiline backtick spans and fenced code blocks verbatim", () => {
    const singleBacktickMultiline = [
      "示例：",
      "`line 1",
      "[x]: /home/workspace/secret.md",
      "`",
      "",
      "正文提到 [x]。",
    ].join("\n");
    expect(rewriteSandboxLinks(singleBacktickMultiline, WEB_BASE, THREAD_ID)).toBe(
      singleBacktickMultiline,
    );

    const doubleBacktickMultiline = [
      "说明：``first line",
      "[def]: /home/workspace/internal.pdf",
      "last line``",
    ].join("\n");
    expect(rewriteSandboxLinks(doubleBacktickMultiline, WEB_BASE, THREAD_ID)).toBe(
      doubleBacktickMultiline,
    );

    // Same-paragraph multiline backtick span inside a blockquote also stays protected.
    const quotedMultiline = [
      "> `line 1",
      "> [q1]: /home/workspace/quoted_inline.md",
      "> line 2`",
    ].join("\n");
    expect(rewriteSandboxLinks(quotedMultiline, WEB_BASE, THREAD_ID)).toBe(
      quotedMultiline,
    );

    // A definition inside a same-paragraph multiline backtick span must NOT shadow a real definition outside code.
    const mixedInput = [
      "代码里的样例：`header",
      "[r1]: /home/workspace/inside_code.md",
      "`",
      "",
      "真实引用见 [外部报告][r1]。",
      "",
      "[r1]: /home/workspace/outside_real.md",
    ].join("\n");
    const mixedExpected = [
      "代码里的样例：`header",
      "[r1]: /home/workspace/inside_code.md",
      "`",
      "",
      `真实引用见 [外部报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Foutside_real.md)。`,
      "",
    ].join("\n");
    expect(rewriteSandboxLinks(mixedInput, WEB_BASE, THREAD_ID)).toBe(mixedExpected);

    // Fenced code blocks containing reference definitions also stay untouched.
    const fencedDef = [
      "```markdown",
      "[f1]: /home/workspace/fenced.md",
      "```",
      "",
      "正文提到 [f1]。",
    ].join("\n");
    expect(rewriteSandboxLinks(fencedDef, WEB_BASE, THREAD_ID)).toBe(fencedDef);
  });

  it("BT3: stray backticks across different Markdown paragraphs or blocks never pair or suppress link rewriting in between", () => {
    // 1) Two stray backticks in separate paragraphs with a normal file link AND a reference
    //    link/definition between them: neither backtick may pair across paragraph boundaries.
    const strayAcrossParagraphs = [
      "第一段包含一个孤立反引号 ` 未闭合。",
      "",
      "第二段有普通文件链接 [完整报告](/home/workspace/results/report.md) 和引用 [摘要][r1]。",
      "",
      "[r1]: /home/workspace/results/summary.md",
      "",
      "第三段也有一个孤立反引号 ` 未闭合。",
    ].join("\n");
    const strayAcrossParagraphsExpected = [
      "第一段包含一个孤立反引号 ` 未闭合。",
      "",
      `第二段有普通文件链接 [完整报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Freport.md) 和引用 [摘要](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Fsummary.md)。`,
      "",
      "",
      "第三段也有一个孤立反引号 ` 未闭合。",
    ].join("\n");
    expect(rewriteSandboxLinks(strayAcrossParagraphs, WEB_BASE, THREAD_ID)).toBe(
      strayAcrossParagraphsExpected,
    );

    // 2) Stray backticks across list items, heading-to-paragraph, and GFM table cells
    //    must NOT pair across block boundaries.
    const strayAcrossListItems = [
      "- 第一项有孤立反引号 `",
      "- 第二项链接：[列表报告](/home/workspace/results/list.md)",
      "- 第三项有孤立反引号 `",
    ].join("\n");
    expect(rewriteSandboxLinks(strayAcrossListItems, WEB_BASE, THREAD_ID)).toBe(
      [
        "- 第一项有孤立反引号 `",
        `- 第二项链接：[列表报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Flist.md)`,
        "- 第三项有孤立反引号 `",
      ].join("\n"),
    );

    const strayHeadingAndParagraph = [
      "# 标题里的孤立反引号 `",
      "[紧邻正文](/home/workspace/results/body.md)",
      "结尾孤立反引号 `",
    ].join("\n");
    expect(rewriteSandboxLinks(strayHeadingAndParagraph, WEB_BASE, THREAD_ID)).toBe(
      [
        "# 标题里的孤立反引号 `",
        `[紧邻正文](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Fbody.md)`,
        "结尾孤立反引号 `",
      ].join("\n"),
    );

    const strayTableCells = [
      "| 左 ` | [表格报告](/home/workspace/results/table.md) | 右 ` |",
      "| --- | --- | --- |",
      "| 1 | 2 | 3 |",
    ].join("\n");
    expect(rewriteSandboxLinks(strayTableCells, WEB_BASE, THREAD_ID)).toBe(
      [
        `| 左 \` | [表格报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Ftable.md) | 右 \` |`,
        "| --- | --- | --- |",
        "| 1 | 2 | 3 |",
      ].join("\n"),
    );
  });

  it("BT3: stray backtick before a fenced code block does not pair into the fence and three chained multiline backtick spans all stay protected", () => {
    // 1) Stray single backtick before a fenced code block containing a backtick inside:
    //    must NOT pair into the fence or suppress rewriting the normal link after the fence.
    const strayBeforeFence = [
      "这里有一个孤立反引号 ` 在围栏前面。",
      "",
      "```bash",
      "echo `date` /home/workspace/inside_fence.md",
      "```",
      "",
      "围栏后的普通链接：[报告](/home/workspace/results/after_fence.md)",
    ].join("\n");
    const strayBeforeFenceExpected = [
      "这里有一个孤立反引号 ` 在围栏前面。",
      "",
      "```bash",
      "echo `date` /home/workspace/inside_fence.md",
      "```",
      "",
      `围栏后的普通链接：[报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Fafter_fence.md)`,
    ].join("\n");
    expect(rewriteSandboxLinks(strayBeforeFence, WEB_BASE, THREAD_ID)).toBe(
      strayBeforeFenceExpected,
    );

    // 2) Three chained consecutive same-paragraph multiline backtick spans: the 3rd span's
    //    reference definition MUST stay verbatim while links outside all three spans are still rewritten.
    const threeChainedSpans = [
      "`span 1 line 1",
      "span 1 line 2` `span 2 line 1",
      "span 2 line 2` `span 3 line 1",
      "[r3]: /home/workspace/inside_third_span.md",
      "span 3 line 2`",
      "",
      "正文提到 [r3]，以及外部链接 [外部](/home/workspace/results/outside.md)。",
    ].join("\n");
    const threeChainedExpected = [
      "`span 1 line 1",
      "span 1 line 2` `span 2 line 1",
      "span 2 line 2` `span 3 line 1",
      "[r3]: /home/workspace/inside_third_span.md",
      "span 3 line 2`",
      "",
      `正文提到 [r3]，以及外部链接 [外部](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Foutside.md)。`,
    ].join("\n");
    expect(rewriteSandboxLinks(threeChainedSpans, WEB_BASE, THREAD_ID)).toBe(
      threeChainedExpected,
    );

    // 3) Top-level fenced code block containing a `> ``` ` line inside it:
    //    must NOT close the fence early or treat the real closing ``` as a new fence;
    //    normal links after the fence MUST still be rewritten.
    const quotedFenceInsideTopLevel = [
      "```markdown",
      "[inside](/home/workspace/results/inside_fence.md)",
      "> ```",
      "still inside fence",
      "```",
      "",
      "围栏后的普通链接：[报告](/home/workspace/results/after_quoted_fence.md)",
    ].join("\n");
    const quotedFenceInsideTopLevelExpected = [
      "```markdown",
      "[inside](/home/workspace/results/inside_fence.md)",
      "> ```",
      "still inside fence",
      "```",
      "",
      `围栏后的普通链接：[报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=%2Fhome%2Fworkspace%2Fresults%2Fafter_quoted_fence.md)`,
    ].join("\n");
    expect(rewriteSandboxLinks(quotedFenceInsideTopLevel, WEB_BASE, THREAD_ID)).toBe(
      quotedFenceInsideTopLevelExpected,
    );
  });
});
