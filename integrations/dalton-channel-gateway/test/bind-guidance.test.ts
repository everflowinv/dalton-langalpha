import { describe, expect, it } from "vitest";
import {
  expiredBindCodeUsage,
  missingBindCodeUsage,
  notLinkedBindHelp,
  notLinkedTargetMessage,
  parseSlashCommand,
  PERSONAL_BIND_COMMAND_SHAPE,
  PERSONAL_CHANNEL_BINDINGS_PATH,
  PERSONAL_CHANNEL_BINDINGS_PLACE,
  personalChannelBindingsUrl,
} from "../src/commands.js";

describe("personal channel bind guidance", () => {
  it("parses /bind the same way and points missing-code help at Models", () => {
    const missing = parseSlashCommand("/bind");
    expect(missing).toMatchObject({ kind: "invalid", reason: "missing_bind_code" });
    if (missing?.kind !== "invalid") throw new Error("expected an invalid /bind");
    expect(missing.message).toBe(missingBindCodeUsage());
    expect(missing.message).toContain(PERSONAL_CHANNEL_BINDINGS_PATH);
    expect(missing.message).toContain(PERSONAL_CHANNEL_BINDINGS_PLACE);
    expect(missing.message).toContain(PERSONAL_BIND_COMMAND_SHAPE);
    expect(missing.message).not.toContain("/settings/channels");
    expect(missing.message).not.toContain("6-CHAR");

    expect(parseSlashCommand("/bind DLT-AB12-CD34")).toEqual({
      kind: "bind",
      code: "DLT-AB12-CD34",
      canonicalCommand: "/bind DLT-AB12-CD34",
    });
    expect(parseSlashCommand("  /bind   not-a-shaped-code  ")).toMatchObject({
      kind: "bind",
      code: "not-a-shaped-code",
    });
    expect(parseSlashCommand("/bind", "group")).toMatchObject({
      kind: "invalid",
      reason: "missing_bind_code",
    });
    expect(parseSlashCommand("/unbind")).toMatchObject({ kind: "unbind", canonicalCommand: "/unbind" });
    expect(parseSlashCommand("/main")).toMatchObject({
      kind: "session",
      cmd: "workspace_switch",
      canonicalCommand: "/main",
    });
    expect(parseSlashCommand("hello")).toBeNull();
  });

  it("builds the real settings link and keeps the unbound Dalton prefix", () => {
    expect(personalChannelBindingsUrl("https://app.ginlix.ai/")).toBe(
      "https://app.ginlix.ai/settings?tab=model#dalton-channel-bindings",
    );
    const denial = notLinkedBindHelp("https://app.ginlix.ai", "weixin");
    expect(denial.startsWith("Your weixin account is not linked to Dalton yet.")).toBe(true);
    expect(denial).toContain("/settings?tab=model#dalton-channel-bindings");
    expect(denial).toContain("/bind DLT-XXXX-XXXX");
    expect(denial).toContain("direct message");
    expect(denial).not.toContain("/settings/channels");
    expect(expiredBindCodeUsage("https://app.ginlix.ai")).toContain(PERSONAL_CHANNEL_BINDINGS_PATH);
    expect(expiredBindCodeUsage()).toContain(PERSONAL_CHANNEL_BINDINGS_PATH);
    expect(expiredBindCodeUsage()).not.toContain("https://");
    expect(notLinkedTargetMessage("https://app.ginlix.ai/", "discord")).toBe(
      "Your discord account is not linked. Open https://app.ginlix.ai/settings?tab=model#dalton-channel-bindings to link it.",
    );
  });
});
