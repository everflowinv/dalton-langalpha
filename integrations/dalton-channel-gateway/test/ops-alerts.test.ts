import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import type { ChannelAdapter } from "../src/channels/adapter.js";
import { FakeChannelAdapter } from "../src/channels/fake-adapter.js";
import { loadConfig } from "../src/config.js";
import {
  dispatchRedisDiagSummaryAlerts,
  evaluateRedisDiagAlerts,
  handleOpsAlert,
  REDIS_DIAG_P2_COOLDOWN_MS,
  sanitizeBackupAlertDetail,
} from "../src/ops-alerts.js";
import { createGatewayApp } from "../src/server.js";
import type { ChannelPlatform } from "../src/types.js";
import { createMockBackend, TEST_SERVICE_TOKEN } from "./helpers.js";

function appFor(
  backend: ReturnType<typeof createMockBackend>,
  adapters: Map<ChannelPlatform, ChannelAdapter>,
  extraEnv: Record<string, string> = {},
) {
  const config = loadConfig({
    GATEWAY_HOST: "127.0.0.1",
    GATEWAY_PORT: "0",
    DALTON_GATEWAY_MODE: "outgoing",
    INTERNAL_SERVICE_TOKEN: TEST_SERVICE_TOKEN,
    DALTON_BACKEND_URL: "http://127.0.0.1:8000",
    ...extraEnv,
  });
  return createGatewayApp({ config, backend, adapters });
}

describe("ops-alerts backup_failed", () => {
  it("authenticates with X-Service-Token and uses the default template on legacy configs", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-backup-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a", "feishu:chat:oc_ops_room"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sent: Array<{ targetAddress: string; text: string }> = [];
    const feishu = Object.assign(new FakeChannelAdapter("feishu"), {
      sendPlainText: async (options: { targetAddress: string; text: string }) => {
        sent.push(options);
        return { messageId: `om_${sent.length}` };
      },
    });
    const backend = createMockBackend();
    const app = appFor(
      backend,
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      { DALTON_OPS_ALERTS_FILE: configPath },
    );
    const { host, port } = await app.start();

    try {
      const unauth = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: "backup_failed", detail: "外置盘未挂载" }),
      });
      expect(unauth.status).toBe(401);

      const badToken = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": "wrong-token",
        },
        body: JSON.stringify({ code: "backup_failed", detail: "外置盘未挂载" }),
      });
      expect(badToken.status).toBe(401);
      expect(sent).toHaveLength(0);

      const res = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({
          code: "backup_failed",
          detail: "dalton_data_backup: 外置盘未挂载",
        }),
      });
      expect(res.status).toBe(200);
      const body = (await res.json()) as { ok: boolean; code: string };
      expect(body.ok).toBe(true);
      expect(body.code).toBe("backup_failed");
      expect(sent).toEqual([
        {
          targetAddress: "feishu:user:ou_ops_a",
          text: "Dalton 备份失败：dalton_data_backup: 外置盘未挂载",
        },
        {
          targetAddress: "feishu:chat:oc_ops_room",
          text: "Dalton 备份失败：dalton_data_backup: 外置盘未挂载",
        },
      ]);
    } finally {
      await app.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("uses configured backup_failed template, strips control chars, and truncates detail to 300 chars", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-backup-tmpl-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
          backup_failed: "[告警] 备份异常：{detail}",
        },
      }),
    );

    const longDetail = `第一行\n\t第二行\x00\x1b${"测".repeat(350)}`;
    const cleaned = sanitizeBackupAlertDetail(longDetail);
    expect(Array.from(cleaned).length).toBe(300);
    expect(cleaned).not.toMatch(/[\x00-\x1f\x7f]/);
    expect(cleaned.startsWith("第一行 第二行")).toBe(true);

    const sender = vi.fn(async () => ({ messageId: "om_1" }));
    const res = await handleOpsAlert({
      body: { code: "backup_failed", detail: longDetail },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(res.status).toBe(200);
    expect(sender).toHaveBeenCalledWith(
      "feishu:user:ou_ops_a",
      `[告警] 备份异常：${cleaned}`,
    );

    const dollarDetail = "成本 $& 和 $$ 不应被替换展开";
    const dollarRes = await handleOpsAlert({
      body: { code: "backup_failed", detail: dollarDetail },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(dollarRes.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith(
      "feishu:user:ou_ops_a",
      `[告警] 备份异常：${dollarDetail}`,
    );

    rmSync(dir, { recursive: true, force: true });
  });

  it("rejects non-string detail on backup_failed and rejects detail on backend_down", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-backup-bad-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_should_not_send" }));
    const badDetailType = await handleOpsAlert({
      body: { code: "backup_failed", detail: 123 },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(badDetailType.status).toBe(400);

    const detailOnBackendDown = await handleOpsAlert({
      body: { code: "backend_down", detail: "should reject" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(detailOnBackendDown.status).toBe(400);
    expect(sender).not.toHaveBeenCalled();

    rmSync(dir, { recursive: true, force: true });
  });
});

describe("ops-alerts alphaengine watchdog codes", () => {
  it("returns ops_alerts_unconfigured (503) when alphaengine message keys are omitted without breaking required keys", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-ae-missing-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_1" }));
    const backendRes = await handleOpsAlert({
      body: { code: "backend_down" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(backendRes.status).toBe(200);
    expect(sender).toHaveBeenCalledTimes(1);

    for (const code of ["alphaengine_login_required", "alphaengine_module_page_required"]) {
      const res = await handleOpsAlert({
        body: { code },
        remoteAddress: "127.0.0.1",
        configPath,
        sendPlainText: sender,
      });
      expect(res.status).toBe(503);
      expect(res.data.code).toBe("ops_alerts_unconfigured");
    }
    expect(sender).toHaveBeenCalledTimes(1);
    rmSync(dir, { recursive: true, force: true });
  });

  it("handles configured alphaengine watchdog codes via handleOpsAlert and rejects blank text or detail", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-ae-direct-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
          alphaengine_login_required: "AlphaEngine 登录失效",
          alphaengine_module_page_required: "AlphaEngine 需要打开模块页",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_ae_1" }));
    const r1 = await handleOpsAlert({
      body: { code: "alphaengine_login_required" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(r1.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith("feishu:user:ou_ops_a", "AlphaEngine 登录失效");

    const r2 = await handleOpsAlert({
      body: { code: "alphaengine_module_page_required" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(r2.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith("feishu:user:ou_ops_a", "AlphaEngine 需要打开模块页");

    const rDetail = await handleOpsAlert({
      body: { code: "alphaengine_module_page_required", detail: "no" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(rDetail.status).toBe(400);

    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
          alphaengine_login_required: "   ",
        },
      }),
    );
    const rBlank = await handleOpsAlert({
      body: { code: "alphaengine_login_required" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(rBlank.status).toBe(503);
    expect(rBlank.data.code).toBe("ops_alerts_unconfigured");

    rmSync(dir, { recursive: true, force: true });
  });

  it("sends configured alphaengine_login_required and alphaengine_module_page_required alerts and rejects detail", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-ae-ok-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a", "feishu:chat:oc_ops_room"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
          alphaengine_login_required: "AlphaEngine 登录失效，请在宿主机重新登录",
          alphaengine_module_page_required: "AlphaEngine 需要打开任意模块页",
        },
      }),
    );

    const sent: Array<{ targetAddress: string; text: string }> = [];
    const feishu = Object.assign(new FakeChannelAdapter("feishu"), {
      sendPlainText: async (options: { targetAddress: string; text: string }) => {
        sent.push(options);
        return { messageId: `om_${sent.length}` };
      },
    });
    const app = appFor(
      createMockBackend(),
      new Map<ChannelPlatform, ChannelAdapter>([["feishu", feishu]]),
      { DALTON_OPS_ALERTS_FILE: configPath },
    );
    const { host, port } = await app.start();

    try {
      const loginRes = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ code: "alphaengine_login_required" }),
      });
      expect(loginRes.status).toBe(200);

      const pageRes = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ code: "alphaengine_module_page_required" }),
      });
      expect(pageRes.status).toBe(200);

      const withDetail = await fetch(`http://${host}:${port}/internal/ops-alerts`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Service-Token": TEST_SERVICE_TOKEN,
        },
        body: JSON.stringify({ code: "alphaengine_login_required", detail: "unexpected" }),
      });
      expect(withDetail.status).toBe(400);

      expect(sent).toEqual([
        { targetAddress: "feishu:user:ou_ops_a", text: "AlphaEngine 登录失效，请在宿主机重新登录" },
        { targetAddress: "feishu:chat:oc_ops_room", text: "AlphaEngine 登录失效，请在宿主机重新登录" },
        { targetAddress: "feishu:user:ou_ops_a", text: "AlphaEngine 需要打开任意模块页" },
        { targetAddress: "feishu:chat:oc_ops_room", text: "AlphaEngine 需要打开任意模块页" },
      ]);
    } finally {
      await app.stop();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("ops-alerts model_unavailable", () => {
  it("uses default template on legacy configs and supports configured template with detail", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-model-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_m1" }));
    const resDefault = await handleOpsAlert({
      body: {
        code: "model_unavailable",
        detail: "codex-oauth: gpt-6.2-sol-oauth；需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.175.0",
      },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(resDefault.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith(
      "feishu:user:ou_ops_a",
      "Dalton 模型可用性告警：codex-oauth: gpt-6.2-sol-oauth；需要提高 DALTON_CODEX_CLIENT_VERSION 到 0.175.0",
    );

    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
          model_unavailable: "[模型告警] {detail}",
        },
      }),
    );
    const resCustom = await handleOpsAlert({
      body: { code: "model_unavailable", detail: "claude-oauth: claude-opus-5-5-oauth\n\t\x00" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(resCustom.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith(
      "feishu:user:ou_ops_a",
      "[模型告警] claude-oauth: claude-opus-5-5-oauth",
    );

    const badDetail = await handleOpsAlert({
      body: { code: "model_unavailable", detail: 42 },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(badDetail.status).toBe(400);

    rmSync(dir, { recursive: true, force: true });
  });
});

describe("ops-alerts model_released", () => {
  it("renders provider and models into default and configured templates", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-released-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_r1" }));
    const resDefault = await handleOpsAlert({
      body: {
        code: "model_released",
        provider: "antigravity-oauth",
        models: "gemini-4-neon-low-oauth, gemini-4-neon-xhigh",
      },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(resDefault.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith(
      "feishu:user:ou_ops_a",
      "订阅已开放 antigravity-oauth 的 gemini-4-neon-low-oauth, gemini-4-neon-xhigh，核对档位后将其设为可见并去掉 awaiting_release 标记",
    );

    const badProvider = await handleOpsAlert({
      body: { code: "model_unavailable", provider: "antigravity-oauth", detail: "x" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(badProvider.status).toBe(400);

    rmSync(dir, { recursive: true, force: true });
  });
});

describe("ops-alerts alphaengine_token_expiring", () => {
  it("renders default and configured templates with sanitized account label detail", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-ae-expiring-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_exp_1" }));
    const resDefault = await handleOpsAlert({
      body: { code: "alphaengine_token_expiring", detail: "尾号6789" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(resDefault.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith(
      "feishu:user:ou_ops_a",
      "请在 AlphaEngine App 里登录账号 尾号6789",
    );

    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
          alphaengine_token_expiring: "[即将过期] 请在 AlphaEngine App 里登录账号 {detail}",
        },
      }),
    );
    const resCustom = await handleOpsAlert({
      body: { code: "alphaengine_token_expiring", detail: "张三\n\t\x00" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(resCustom.status).toBe(200);
    expect(sender).toHaveBeenLastCalledWith(
      "feishu:user:ou_ops_a",
      "[即将过期] 请在 AlphaEngine App 里登录账号 张三",
    );

    rmSync(dir, { recursive: true, force: true });
  });

  it("reports any_succeeded=true on 502 partial delivery and any_succeeded=false when all targets fail", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-ae-partial-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a", "feishu:user:ou_ops_b"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const partialSender = vi.fn(async (target: string) => {
      if (target === "feishu:user:ou_ops_b") throw new Error("send failed");
      return { messageId: "om_ok_1" };
    });
    const partialRes = await handleOpsAlert({
      body: { code: "alphaengine_token_expiring", detail: "尾号6789" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: partialSender,
    });
    expect(partialRes.status).toBe(502);
    expect(partialRes.data.ok).toBe(false);
    expect(partialRes.data.any_succeeded).toBe(true);

    const allFailSender = vi.fn(async () => {
      throw new Error("network down");
    });
    const allFailRes = await handleOpsAlert({
      body: { code: "alphaengine_token_expiring", detail: "尾号6789" },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: allFailSender,
    });
    expect(allFailRes.status).toBe(502);
    expect(allFailRes.data.ok).toBe(false);
    expect(allFailRes.data.any_succeeded).toBe(false);

    rmSync(dir, { recursive: true, force: true });
  });
});

describe("ops-alerts redis diagnostics aggregation & dedup", () => {
  it("evaluates P1/P2/P3 alerts with event-window P1 dedup and 30m P2/P3 cooldown", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-redis-diag-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const summary = {
      cluster: {
        active_worker_count: 2,
        total_transport_lost_5m: 1,
        total_transport_lost_ever: 1,
        last_transport_lost_at: "2026-11-27T12:00:00+00:00",
        total_recovered_writes_5m: 6,
        max_pool_utilization: 0.85,
        max_loop_lag_5s_ms: 1650,
        max_payload_bytes_5m: 600_000,
        max_wire_ms: 520,
        max_acquire_ms: 10,
      },
      server: {
        stats: { latest_fork_usec: 250_000 },
        slowlog: [{ id: 1, start_time: 1_700_000_000, duration_us: 130_000, command: "XADD" }],
      },
      workers: [
        { worker_id: "node:1001", counts: { transport_lost_total: 1 } },
        { worker_id: "node:1002", counts: { transport_lost_total: 0 } },
      ],
    };

    const t0 = 1_700_000_000_000;
    const sent: Array<{ target: string; text: string }> = [];
    const sender = vi.fn(async (target: string, text: string) => {
      sent.push({ target, text });
      return { messageId: `om_${sent.length}` };
    });

    const first = await dispatchRedisDiagSummaryAlerts({
      summary,
      state: {},
      nowMs: t0,
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(first.results.map((r) => r.alert.code)).toEqual([
      "redis_diag_p1",
      "redis_diag_p2",
      "redis_diag_p3",
    ]);
    expect(sent).toHaveLength(3);
    expect(sent[0]?.text).toContain("Dalton Redis P1 告警：");
    expect(sent[1]?.text).toContain("Dalton Redis P2 退化告警：");
    expect(sent[2]?.text).toContain("Dalton Redis P3 预警：");

    // 60 seconds later: all 3 suppressed
    const second = evaluateRedisDiagAlerts(summary, first.nextState, t0 + 60_000);
    expect(second.alerts).toHaveLength(0);

    // 31 minutes later: P2 and P3 re-fire (other P2 signals still active); P1 stays suppressed without a new transport_lost
    const third = evaluateRedisDiagAlerts(
      summary,
      first.nextState,
      t0 + REDIS_DIAG_P2_COOLDOWN_MS + 60_000,
    );
    expect(third.alerts.map((a) => a.code)).toEqual(["redis_diag_p2", "redis_diag_p3"]);

    // Old PID expiry lowers cluster total_transport_lost_ever (from 1 -> 1 on a NEW worker) within 5m: P1 STILL fires!
    const fourth = evaluateRedisDiagAlerts(
      {
        ...summary,
        cluster: {
          ...summary.cluster,
          total_transport_lost_5m: 1,
          total_transport_lost_ever: 1,
          last_transport_lost_at: "2026-11-27T12:02:00+00:00",
        },
        workers: [{ worker_id: "node:2002", counts: { transport_lost_total: 1 } }],
      },
      first.nextState,
      t0 + 120_000,
    );
    expect(fourth.alerts.map((a) => a.code)).toEqual(["redis_diag_p1"]);

    // Slowlog-only P2: lingering same slowlog ID does NOT re-fire after 30m, but a new ID DOES fire.
    const slowOnly = {
      cluster: { active_worker_count: 1 },
      server: {
        stats: { latest_fork_usec: 1000 },
        slowlog: [{ id: 1, start_time: 1_700_000_000, duration_us: 130_000, command: "XADD" }],
      },
    };
    const sameSlowAfter31m = evaluateRedisDiagAlerts(
      slowOnly,
      first.nextState,
      t0 + REDIS_DIAG_P2_COOLDOWN_MS + 60_000,
    );
    expect(sameSlowAfter31m.alerts).toHaveLength(0);

    const newSlowAfter31m = evaluateRedisDiagAlerts(
      {
        ...slowOnly,
        server: {
          stats: { latest_fork_usec: 1000 },
          slowlog: [{ id: 2, start_time: 1_700_001_900, duration_us: 140_000, command: "SET" }],
        },
      },
      first.nextState,
      t0 + REDIS_DIAG_P2_COOLDOWN_MS + 60_000,
    );
    expect(newSlowAfter31m.alerts.map((a) => a.code)).toEqual(["redis_diag_p2"]);

    rmSync(dir, { recursive: true, force: true });
  });
});

describe("ops-alerts monitor_publish_failed", () => {
  it("renders default and configured templates with up to 600 chars detail", async () => {
    const dir = mkdtempSync(join(tmpdir(), "dalton-ops-mon-pub-"));
    const configPath = join(dir, "ops-alerts.json");
    writeFileSync(
      configPath,
      JSON.stringify({
        targets: ["feishu:user:ou_ops_a"],
        messages: {
          backend_down: "Dalton 后端不可用",
          backend_recovered: "Dalton 后端已恢复",
        },
      }),
    );

    const sender = vi.fn(async () => ({ messageId: "om_mon_1" }));
    const longDetail = "x".repeat(700);
    const resDefault = await handleOpsAlert({
      body: {
        code: "monitor_publish_failed",
        detail: longDetail,
      },
      remoteAddress: "127.0.0.1",
      configPath,
      sendPlainText: sender,
    });
    expect(resDefault.status).toBe(200);
    const sentText = sender.mock.calls[0][1];
    expect(sentText).toContain("Dalton 看板发布告警");
    expect(sentText).toContain("x".repeat(600));
    expect(sentText).not.toContain("x".repeat(601));

    rmSync(dir, { recursive: true, force: true });
  });
});
