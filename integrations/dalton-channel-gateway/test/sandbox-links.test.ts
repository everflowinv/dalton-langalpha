import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { describe, expect, it } from "vitest";
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
    const match = /export const SANDBOX_ROOT_PREFIXES = \[([\s\S]*?)\] as const;/.exec(
      source,
    );
    expect(match).not.toBeNull();
    const frontendRoots = Array.from(
      match![1]!.matchAll(/['"]([^'"]+)['"]/g),
      (m) => m[1]!,
    );
    expect([...SANDBOX_ROOT_PREFIXES]).toEqual(frontendRoots);
  });

  it("rewrites /home/workspace and /home/daytona markdown links and images to thread deep links", () => {
    const input = [
      "详见 [完整报告](/home/workspace/results/report.md) 与 [旧报告](/home/daytona/archive/old.pdf)。",
      "配图：![收益曲线](/home/workspace/charts/equity.png)",
    ].join("\n");

    const expected = [
      `详见 [完整报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.md) 与 [旧报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=archive%2Fold.pdf)。`,
      `配图：[收益曲线](${WEB_BASE}/chat/t/${THREAD_ID}?file=charts%2Fequity.png)`,
    ].join("\n");

    expect(rewriteSandboxLinks(input, `${WEB_BASE}/`, THREAD_ID)).toBe(expected);
  });

  it("rewrites file:///home/... sandbox links and leaves non-sandbox file:// links untouched", () => {
    const input = [
      "[报告](file:///home/workspace/results/nvda_analysis.md)",
      "![图表](file:///home/daytona/work/charts/fig.png)",
      "[系统文件](file:///etc/passwd)",
    ].join("\n");

    const expected = [
      `[报告](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Fnvda_analysis.md)`,
      `[图表](${WEB_BASE}/chat/t/${THREAD_ID}?file=work%2Fcharts%2Ffig.png)`,
      "[系统文件](file:///etc/passwd)",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("rewrites __wsref__/<uuid>/... links (including legacy prefixes) to workspace deep links", () => {
    const input = [
      `[跨区报告](__wsref__/${WS_ID}/results/nvda_analysis.md)`,
      `[旧前缀1](__wsref__/${WS_ID}/file:///home/workspace/results/a.md)`,
      `[旧前缀2](/__wsref__/${WS_ID}//home/daytona/charts/fig.png)`,
    ].join("\n");

    const expected = [
      `[跨区报告](${WEB_BASE}/chat/${WS_ID}?file=results%2Fnvda_analysis.md)`,
      `[旧前缀1](${WEB_BASE}/chat/${WS_ID}?file=results%2Fa.md)`,
      `[旧前缀2](${WEB_BASE}/chat/${WS_ID}?file=charts%2Ffig.png)`,
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
    // __wsref__ carries its own workspace UUID, so it resolves even without threadId.
    expect(rewriteSandboxLinks(input, WEB_BASE, null)).toBe(expected);
  });

  it("rewrites relative file paths with extensions and leaves non-file destinations untouched", () => {
    const input = [
      "[纪要](results/memo.md)",
      "[表格](./data/summary.xlsx)",
      "[模型](work/model.py:42)",
      "[章节](results/report.md#valuation)",
      "[设置](/settings)",
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
      "[设置](/settings)",
      "[目录](results/)",
      "[锚点](#summary)",
      "[普通括号](up 2.5)",
      "[说明](notes about things)",
      "[越界](../../etc/passwd)",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("URL-encodes Chinese, space, and parenthesis filenames once without double-encoding", () => {
    const input = [
      "[季报](results/中文 报告 (1).pdf)",
      "[演示](<file:///home/workspace/results/Q3 deck.pptx>)",
      "[已编码](results/%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md)",
    ].join("\n");

    const expected = [
      `[季报](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2F%E4%B8%AD%E6%96%87%20%E6%8A%A5%E5%91%8A%20%281%29.pdf)`,
      `[演示](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2FQ3%20deck.pptx)`,
      `[已编码](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2F%E4%B8%AD%E6%96%87%20%E6%96%87%E6%A1%A3.md)`,
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("converts bare sandbox absolute paths in prose to workspace-relative paths without creating links", () => {
    const input = [
      "已保存至 /home/workspace/results/report.md，图表在 /home/daytona/charts/fig.png。",
      "本地协议：file:///home/workspace/results/中文报告.pdf",
      "外部链接含路径不动：https://example.com/home/workspace/report.md",
      "非沙箱根不动：/var/home/workspace/other.md",
    ].join("\n");

    const expected = [
      "已保存至 results/report.md，图表在 charts/fig.png。",
      "本地协议：results/中文报告.pdf",
      "外部链接含路径不动：https://example.com/home/workspace/report.md",
      "非沙箱根不动：/var/home/workspace/other.md",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("strips localhost and 127.0.0.1 preview links while keeping link text", () => {
    const input = [
      "查看 [交互看板](http://localhost:8000/dashboard) 或 [本地服务](http://127.0.0.1:3000/index.html)。",
      "也可参考 [端口简写](localhost:5173/preview) 与 [IPv6](http://[::1]:8088/app)。",
    ].join("\n");

    const expected = [
      "查看 交互看板 或 本地服务。",
      "也可参考 端口简写 与 IPv6。",
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

  it("does not rewrite fenced code blocks or inline code spans, while rewriting code-formatted link labels", () => {
    const input = [
      "行内代码 `/home/workspace/results/report.md` 与 `[report](results/report.md)` 保持原样。",
      "双反引号 ``[chart](/home/daytona/fig.png)`` 也保持原样。",
      "标签含行内代码：[`report.md`](/home/workspace/results/report.md)",
      "```markdown",
      "[报告](/home/workspace/results/report.md)",
      "/home/workspace/data/raw.csv",
      "[预览](http://localhost:8000/)",
      "```",
      "~~~",
      "[另一份](results/other.md)",
      "~~~",
    ].join("\n");

    const expected = [
      "行内代码 `/home/workspace/results/report.md` 与 `[report](results/report.md)` 保持原样。",
      "双反引号 ``[chart](/home/daytona/fig.png)`` 也保持原样。",
      `标签含行内代码：[\`report.md\`](${WEB_BASE}/chat/t/${THREAD_ID}?file=results%2Freport.md)`,
      "```markdown",
      "[报告](/home/workspace/results/report.md)",
      "/home/workspace/data/raw.csv",
      "[预览](http://localhost:8000/)",
      "```",
      "~~~",
      "[另一份](results/other.md)",
      "~~~",
    ].join("\n");

    expect(rewriteSandboxLinks(input, WEB_BASE, THREAD_ID)).toBe(expected);
  });

  it("falls back to plain-text workspace hints when threadId is missing", () => {
    const input =
      "见 [完整报告](/home/workspace/results/中文 报告.md) 与 ![图表](charts/fig.png)。";
    const expected =
      "见 完整报告（在网页工作区查看：results/中文 报告.md） 与 图表（在网页工作区查看：charts/fig.png）。";

    expect(rewriteSandboxLinks(input, WEB_BASE, null)).toBe(expected);
    expect(rewriteSandboxLinks(input, WEB_BASE, undefined)).toBe(expected);
    expect(rewriteSandboxLinks(input, WEB_BASE, "   ")).toBe(expected);
  });

  it("falls back to plain-text workspace hints when webBaseUrl is localhost, 127.0.0.1, or empty", () => {
    const input = [
      "[报告](/home/workspace/results/report.md)",
      `[跨区](__wsref__/${WS_ID}/results/other.md)`,
    ].join(" ");
    const expected =
      "报告（在网页工作区查看：results/report.md） 跨区（在网页工作区查看：results/other.md）";

    for (const base of [
      "http://localhost:5173",
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

  it("documents workspace-relative links and send_message file attachment in buildSurfaceRules", () => {
    const rules = buildSurfaceRules("feishu");
    expect(rules).toContain("workspace-relative paths");
    expect(rules).toContain("require sign-in");
    expect(rules).toContain("send_message(files=[...])");
    expect(rules).not.toContain("Do NOT paste local sandbox paths");
  });
});
