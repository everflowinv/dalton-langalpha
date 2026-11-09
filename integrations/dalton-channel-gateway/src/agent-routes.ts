import type { BackendClient, I4StateResponse } from "./backend-client.js";
import type { ChannelAdapter } from "./channels/adapter.js";
import { DeliveryService } from "./delivery-service.js";
import {
  type AgentAutomationOutputPutRequest,
  type AgentAutomationOutputPutResponse,
  type AgentAutomationRunFinishRequest,
  type AgentAutomationRunFinishTargetRow,
  type AgentAutomationRunStartRequest,
  type AgentAutomationTargetsResponse,
  type AgentCheckTargetRequest,
  type AgentCheckTargetResponse,
  type AgentSendRequest,
  type AgentSendResponse,
  type AgentSettingsAvailableResponse,
  type AgentSettingsPutResponse,
  type AgentSettingsResponse,
  type AgentTargetItem,
  type AgentTargetsResponse,
  type AgentTargetUnavailable,
  type AutomationRunTargetRecord,
  type AvailableChatItem,
  type ChannelPlatform,
  type DeliveryDefaultTarget,
  isSupportedPlatform,
  MAX_AUTOMATION_FINAL_TEXT_CHARS,
  SUPPORTED_PLATFORMS,
} from "./types.js";

export class AgentRoutesHandler {
  private readonly backend: BackendClient;
  private readonly adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
  private readonly deliveryService: DeliveryService;
  private readonly webBaseUrl: string;

  /**
   * Process-local set of `execution_id`s currently executing `/finish` (§1.2.7).
   * Distinguishes Shape A (claim & execute) from Shape B (concurrent caller sees mid-post rows).
   */
  readonly activeFinishExecutors = new Set<string>();

  constructor(options: {
    backend: BackendClient;
    adapters: ReadonlyMap<ChannelPlatform, ChannelAdapter>;
    deliveryService: DeliveryService;
    webBaseUrl: string;
  }) {
    this.backend = options.backend;
    this.adapters = options.adapters;
    this.deliveryService = options.deliveryService;
    this.webBaseUrl = options.webBaseUrl.replace(/\/+$/, "");
  }

  // -------------------------------------------------------------------------
  // §1.2.1 POST /agent/send
  // -------------------------------------------------------------------------
  async handleAgentSend(
    userId: string,
    body: AgentSendRequest,
  ): Promise<{ status: number; data: AgentSendResponse }> {
    const deliveryKey = DeliveryService.makeAgentSendDeliveryKey(body);
    const result = await this.deliveryService.executeDelivery({
      deliveryKey,
      kind: "agent_send",
      userId,
      threadId: body.thread_id,
      runId: body.run_id,
      automationExecutionId: body.automation_execution_id ?? null,
      platform: body.turn_platform ?? null,
      target: body.target ?? null,
      text: body.text ?? "",
      files: body.files ?? [],
      replyToMessageId: null,
      newThread: Boolean(body.new_thread),
    });
    return { status: 200, data: result };
  }

  // -------------------------------------------------------------------------
  // §1.2.2 GET /agent/targets
  // -------------------------------------------------------------------------
  async handleGetTargets(
    userId: string,
    query: {
      thread_id?: string | null;
      run_id?: string | null;
      turn_platform?: string | null;
    },
  ): Promise<{ status: number; data: AgentTargetsResponse }> {
    const state = await this.backend.getChannelState({
      user_id: userId,
      thread_id: query.thread_id ?? null,
      run_id: query.run_id ?? null,
      platform: query.turn_platform ?? null,
    });

    const current = state.current_conversation?.address
      ? { address: state.current_conversation.address }
      : null;

    const targets: AgentTargetItem[] = [];
    const unavailable: AgentTargetUnavailable[] = [];
    const isExternalUser = state.user_tier === "external";
    const writableWsIds = await this.resolveWritableWorkspaceIds(userId, state);

    for (const platform of SUPPORTED_PLATFORMS) {
      const identity = state.identities?.[platform];
      if (!identity) {
        unavailable.push({
          platform,
          reason: "not_linked",
          message: `Your ${platform} account is not linked. Open ${this.webBaseUrl}/settings/channels to link it.`,
        });
        continue;
      }

      const adapter = this.adapters.get(platform);
      if (!adapter || !adapter.isConfigured() || !adapter.isConnected()) {
        unavailable.push({
          platform,
          reason: "unavailable",
          message: `The ${platform} service is currently unavailable.`,
        });
        continue;
      }

      const appCfg = this.getAppConfig(state, platform);
      // When proactive `agent_messages.enabled === false`, omit non-current targets for this platform
      if (!appCfg.agent_messages.enabled) {
        continue;
      }

      const allowedSet = new Set(appCfg.agent_messages.allowed);
      const preferred = appCfg.preferred;
      const dmAddress = `${platform}:@me`;
      const dmName = identity.platform_username
        ? `${identity.platform_username} (DM)`
        : `${platform} DM`;

      if (allowedSet.has(dmAddress)) {
        targets.push({
          address: dmAddress,
          kind: "dm",
          name: dmName,
          preferred: preferred === dmAddress || preferred === null,
          thread: null,
        });
      }

      // External users and WeChat are never allowed group chat targets
      if (platform === "weixin" || isExternalUser) {
        continue;
      }

      const seenAddresses = new Set<string>([dmAddress]);
      const bindingByAddress = new Map<
        string,
        { name: string | null; workspace_id: string | null; last_used_at: number | null }
      >();
      for (const b of state.chat_bindings ?? []) {
        if (b.platform === platform && b.chat_address) {
          bindingByAddress.set(b.chat_address, {
            name: b.chat_name,
            workspace_id: b.workspace_id,
            last_used_at: b.last_used_at,
          });
        }
      }

      // Only list group chats that are in `agent_messages.allowed`
      // AND registered in BOTH the user's personal `chats` AND global `chat_bindings`
      // (`src/dalton/database/channel_gateway.py:348-349, 2879-2897`), with write access to any bound workspace
      const candidateAddresses = new Set<string>();
      for (const addr of allowedSet) {
        if (addr !== dmAddress) {
          candidateAddresses.add(addr);
        }
      }

      for (const addr of candidateAddresses) {
        if (seenAddresses.has(addr)) continue;
        const inUserChats = Object.prototype.hasOwnProperty.call(appCfg.chats, addr);
        const meta = bindingByAddress.get(addr);
        if (!inUserChats || !meta) {
          continue;
        }
        const boundWs = meta.workspace_id ?? appCfg.chats[addr]?.workspace_id ?? null;
        if (boundWs && !writableWsIds.has(boundWs)) {
          continue;
        }
        seenAddresses.add(addr);
        targets.push({
          address: addr,
          kind: platform === "discord" ? "channel" : "group",
          name: meta.name || appCfg.chats[addr]?.name || addr,
          preferred: preferred === addr,
          thread:
            typeof meta.last_used_at === "number"
              ? { last_used_at: meta.last_used_at }
              : null,
        });
      }
    }

    return {
      status: 200,
      data: {
        current,
        targets,
        unavailable,
        settings_url: `${this.webBaseUrl}/settings/channels`,
      },
    };
  }

  // -------------------------------------------------------------------------
  // §1.2.3 GET /agent/settings & PUT /agent/settings
  // -------------------------------------------------------------------------
  async handleGetSettings(
    userId: string,
  ): Promise<{ status: number; data: AgentSettingsResponse }> {
    const state = await this.backend.getChannelState({ user_id: userId });
    const apps = structuredClone(state.settings?.apps ?? {});
    const nameByAddr = new Map<string, string>();
    for (const b of state.chat_bindings ?? []) {
      if (b.chat_address && b.chat_name) {
        nameByAddr.set(b.chat_address, b.chat_name);
      }
    }

    for (const [key, val] of Object.entries(apps)) {
      if (key === "default" || !val || typeof val !== "object") continue;
      const appObj = val as Record<string, unknown>;
      if (appObj.chats && typeof appObj.chats === "object") {
        const chatsObj = appObj.chats as Record<string, Record<string, unknown>>;
        for (const [addr, binding] of Object.entries(chatsObj)) {
          if (binding && typeof binding === "object") {
            binding.name = nameByAddr.get(addr) ?? binding.name ?? null;
          }
        }
      }
    }

    return {
      status: 200,
      data: {
        version: state.settings?.version ?? "0",
        settings: apps,
      },
    };
  }

  async handlePutSettings(
    userId: string,
    body: { version: string; settings: Record<string, unknown> },
  ): Promise<{ status: number; data: AgentSettingsPutResponse }> {
    const res = await this.backend.putChannelSettings({
      user_id: userId,
      version: body.version,
      settings: body.settings,
    });

    if (res.status === 200) {
      return {
        status: 200,
        data: {
          version: res.data.version ?? body.version,
          changes: res.data.changes ?? [],
        },
      };
    }

    if (res.status === 409) {
      return {
        status: 409,
        data: {
          code: "version_conflict",
          message:
            res.data.message ??
            "Channel settings were modified elsewhere; read channels.json again and retry.",
        },
      };
    }

    if (res.status === 403) {
      const msg =
        res.data.detail ??
        res.data.message ??
        "You do not have permission to modify one or more group chat bindings.";
      return {
        status: 400,
        data: {
          code: "invalid",
          message: msg,
          problems: [{ field: "chats", message: msg }],
        },
      };
    }

    if (res.status === 400) {
      return {
        status: 400,
        data: {
          code: res.data.code ?? "invalid",
          message: res.data.message ?? "Invalid channel settings.",
          problems: res.data.problems ?? [],
        },
      };
    }

    return {
      status: res.status,
      data: {
        message: res.data.message ?? res.data.detail ?? "Failed to save channel settings.",
        ...(res.data.applied ? { applied: res.data.applied } : {}),
      },
    };
  }

  // -------------------------------------------------------------------------
  // §1.2.4 GET /agent/settings/available
  // -------------------------------------------------------------------------
  async handleGetSettingsAvailable(
    userId: string,
  ): Promise<{ status: number; data: AgentSettingsAvailableResponse }> {
    const state = await this.backend.getChannelState({ user_id: userId });
    const apps: AgentSettingsAvailableResponse["apps"] = {};

    for (const platform of SUPPORTED_PLATFORMS) {
      const identity = state.identities?.[platform];
      if (!identity) continue;

      const listing = await this.collectAvailableChatsForPlatform(
        userId,
        platform,
        identity.platform_user_id,
        identity.platform_username,
        state,
      );
      apps[platform] = listing;
    }

    return { status: 200, data: { apps } };
  }

  // -------------------------------------------------------------------------
  // §1.2.5 POST /agent/check-target
  // -------------------------------------------------------------------------
  async handleCheckTarget(
    userId: string,
    body: AgentCheckTargetRequest,
  ): Promise<{ status: number; data: AgentCheckTargetResponse }> {
    const rawAddress = (body.address ?? "").trim();
    if (!rawAddress) {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: "Target address cannot be empty.",
        },
      };
    }

    const [rawPlat, ...restParts] = rawAddress.split(":");
    const platform = (rawPlat ?? "").toLowerCase();
    let rest = restParts.join(":").trim();

    if (!isSupportedPlatform(platform)) {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: `Unsupported platform "${platform}". Supported platforms: feishu, discord, weixin.`,
        },
      };
    }

    const state = await this.backend.getChannelState({
      user_id: userId,
      platform,
    });
    const identity = state.identities?.[platform];
    if (!identity) {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: `You have not linked your ${platform} account yet.`,
        },
      };
    }

    const appCfg = this.getAppConfig(state, platform);
    if (
      !rest &&
      appCfg.preferred &&
      appCfg.preferred !== `${platform}:@me` &&
      appCfg.preferred.startsWith(`${platform}:`)
    ) {
      rest = appCfg.preferred.slice(platform.length + 1).trim();
    }

    // Bare platform name (with default DM preferred) or `<platform>:@me` resolves to `<platform>:@me`
    if (!rest || rest.toLowerCase() === "@me") {
      return {
        status: 200,
        data: {
          ok: true,
          address: `${platform}:@me`,
          message: null,
        },
      };
    }

    if (platform === "weixin") {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: "WeChat supports direct messages only (weixin:@me).",
        },
      };
    }

    if (state.user_tier === "external") {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: "External users cannot deliver to group chats.",
        },
      };
    }

    const canonicalAddress = `${platform}:${rest}`;
    const writableWsIds = await this.resolveWritableWorkspaceIds(userId, state);
    const binding = (state.chat_bindings ?? []).find(
      (b) => b.platform === platform && b.chat_address === canonicalAddress,
    );
    const inUserChats = Object.prototype.hasOwnProperty.call(
      appCfg.chats,
      canonicalAddress,
    );

    // Group target must be registered in BOTH user's personal `chats` AND global `chat_bindings`
    // (`src/dalton/database/channel_gateway.py:348-349, 2897` `_verify_group_target_deliverable`)
    if (!binding || !inUserChats) {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: `Group chat ${canonicalAddress} is not registered in your channel settings or you do not have permission to use it.`,
        },
      };
    }

    // Check workspace write permission if the group is bound to a workspace
    const boundWs =
      binding.workspace_id ?? appCfg.chats[canonicalAddress]?.workspace_id ?? null;
    if (boundWs && !writableWsIds.has(boundWs)) {
      return {
        status: 200,
        data: {
          ok: false,
          address: null,
          message: "You do not have write access to the group's bound workspace.",
        },
      };
    }

    const adapter = this.adapters.get(platform);
    if (adapter?.checkTargetLive) {
      const liveCheck = await adapter.checkTargetLive({
        userId,
        platformUserId: identity.platform_user_id,
        address: canonicalAddress,
        purpose: body.purpose ?? "automation",
      });
      if (!liveCheck.ok) {
        return {
          status: 200,
          data: {
            ok: false,
            address: null,
            message: liveCheck.message ?? `Target ${canonicalAddress} is not reachable.`,
          },
        };
      }
      return {
        status: 200,
        data: {
          ok: true,
          address: liveCheck.address ?? canonicalAddress,
          name: binding.chat_name ?? appCfg.chats[canonicalAddress]?.name ?? null,
          message: null,
        },
      };
    }

    return {
      status: 200,
      data: {
        ok: true,
        address: canonicalAddress,
        name: binding.chat_name ?? appCfg.chats[canonicalAddress]?.name ?? null,
        message: null,
      },
    };
  }

  // -------------------------------------------------------------------------
  // §1.2.6 GET /agent/automation-targets & PUT /agent/automation-output
  // -------------------------------------------------------------------------
  async handleGetAutomationTargets(
    userId: string,
    workspaceId?: string | null,
  ): Promise<{ status: number; data: AgentAutomationTargetsResponse }> {
    const state = await this.backend.getChannelState({ user_id: userId });
    const apps: AgentAutomationTargetsResponse["apps"] = {};

    for (const platform of SUPPORTED_PLATFORMS) {
      const identity = state.identities?.[platform];
      if (!identity) continue;

      const listing = await this.collectAvailableChatsForPlatform(
        userId,
        platform,
        identity.platform_user_id,
        identity.platform_username,
        state,
      );
      const nameByAddr = new Map<string, string>();
      for (const c of listing.chats) {
        nameByAddr.set(c.address, c.name);
      }

      const appCfg = this.getAppConfig(state, platform);
      const defaultAddress =
        workspaceId && appCfg.automation_output[workspaceId]
          ? appCfg.automation_output[workspaceId]!
          : null;

      const dmAddress = `${platform}:@me`;
      const dmName = nameByAddr.get(dmAddress) ?? "Direct Messages";

      let defaultTarget: DeliveryDefaultTarget;

      if (defaultAddress) {
        defaultTarget = {
          address: defaultAddress,
          name: nameByAddr.get(defaultAddress) ?? defaultAddress,
          via: "workspace",
        };
      } else if (appCfg.preferred && appCfg.preferred !== dmAddress) {
        defaultTarget = {
          address: appCfg.preferred,
          name: nameByAddr.get(appCfg.preferred) ?? appCfg.preferred,
          via: "preferred",
        };
      } else {
        defaultTarget = {
          address: dmAddress,
          name: dmName,
          via: "dm",
        };
      }

      apps[platform] = {
        chats: listing.chats,
        default: defaultTarget,
        default_address: defaultAddress,
        resolves_to: defaultTarget,
        error: listing.error,
      };
    }

    return { status: 200, data: { apps } };
  }

  async handlePutAutomationOutput(
    userId: string,
    body: AgentAutomationOutputPutRequest,
  ): Promise<{ status: number; data: AgentAutomationOutputPutResponse }> {
    const platform = (body.platform ?? "").trim().toLowerCase();
    if (!isSupportedPlatform(platform)) {
      const msg = `Unsupported platform "${body.platform}".`;
      return {
        status: 400,
        data: {
          message: msg,
          problems: [{ field: "platform", message: msg }],
        },
      };
    }

    let normalizedAddress: string | null = null;
    if (body.address !== null && body.address !== undefined && body.address !== "") {
      const checkRes = await this.handleCheckTarget(userId, {
        address: body.address,
        purpose: "automation",
      });
      if (!checkRes.data.ok || !checkRes.data.address) {
        const msg = checkRes.data.message ?? "Invalid automation output address.";
        return {
          status: 400,
          data: {
            message: msg,
            problems: [{ field: "address", message: msg }],
          },
        };
      }
      normalizedAddress = checkRes.data.address;
    }

    const putRes = await this.backend.putChannelSettings({
      user_id: userId,
      automation_output_update: {
        workspace_id: body.workspace_id,
        platform,
        address: normalizedAddress,
      },
    });

    if (putRes.status === 200) {
      let friendlyName: string | null = null;
      if (normalizedAddress) {
        const state = await this.backend.getChannelState({
          user_id: userId,
          platform,
        });
        if (normalizedAddress === `${platform}:@me`) {
          const uname = state.identities?.[platform]?.platform_username;
          friendlyName = uname ? `Direct Messages (${uname})` : "Direct Messages";
        } else {
          const found = (state.chat_bindings ?? []).find(
            (b) => b.platform === platform && b.chat_address === normalizedAddress,
          );
          friendlyName = found?.chat_name ?? putRes.data.name ?? normalizedAddress;
        }
      }
      return {
        status: 200,
        data: {
          address: normalizedAddress,
          name: friendlyName,
        },
      };
    }

    if (putRes.status === 409) {
      return {
        status: 409,
        data: {
          message:
            putRes.data.message ??
            "The default is being changed elsewhere. Try again.",
        },
      };
    }

    return {
      status: 400,
      data: {
        message: putRes.data.message ?? putRes.data.detail ?? "The default was not saved.",
        problems: putRes.data.problems ?? [],
      },
    };
  }

  // -------------------------------------------------------------------------
  // §1.2.7 POST /agent/automation-runs & POST /agent/automation-runs/{id}/finish
  // -------------------------------------------------------------------------
  async handleStartAutomationRun(
    userId: string,
    body: AgentAutomationRunStartRequest,
  ): Promise<{ status: number; data: { targets: AutomationRunTargetRecord[] } }> {
    const res = await this.backend.startAutomationRun({
      execution_id: body.execution_id,
      user_id: userId,
      workspace_id: body.workspace_id ?? null,
      thread_id: body.thread_id ?? null,
      automation_name: body.automation_name ?? "",
      entries: body.entries ?? [],
    });
    return { status: 200, data: { targets: res.targets } };
  }

  async handleFinishAutomationRun(
    userId: string,
    executionId: string,
    body: AgentAutomationRunFinishRequest,
  ): Promise<{
    status: number;
    data: { targets?: AgentAutomationRunFinishTargetRow[]; detail?: string };
  }> {
    // Shape B check: if another caller in this gateway process is already finishing `executionId`
    if (this.activeFinishExecutors.has(executionId)) {
      const current = await this.backend.finishAutomationRun(
        executionId,
        {
          status: body.status,
          thread_id: body.thread_id ?? null,
          final_text: body.final_text ?? null,
          mark_finished: false,
        },
        userId,
      );
      if (current.status === 404 || !current.data) {
        return { status: 404, data: { detail: "Unknown execution_id" } };
      }
      const inFlightRows: AgentAutomationRunFinishTargetRow[] = (
        current.data.targets ?? []
      ).map((t) => {
        if (!t.ok) {
          return {
            entry: t.entry,
            address: t.address,
            name: t.name,
            reached: false,
            via: null,
            error: t.message ?? "Target was not resolved at start.",
          };
        }
        if (t.reached || t.via) {
          return {
            entry: t.entry,
            address: t.address,
            name: t.name,
            reached: Boolean(t.reached),
            via: t.via ?? null,
            error: t.error ?? null,
          };
        }
        // Still being posted (`reached: false, via: null, error: null` -> `_still_posting` is true)
        return {
          entry: t.entry,
          address: t.address,
          name: t.name,
          reached: false,
          via: null,
          error: null,
        };
      });
      return { status: 200, data: { targets: inFlightRows } };
    }

    const claimed = await this.backend.finishAutomationRun(
      executionId,
      {
        status: body.status,
        thread_id: body.thread_id ?? null,
        final_text: body.final_text ?? null,
        mark_finished: false,
      },
      userId,
    );

    if (claimed.status === 404 || !claimed.data) {
      return { status: 404, data: { detail: "Unknown execution_id" } };
    }

    // Shape C: already `finished` in DB
    if (
      claimed.data.status === "finished" &&
      claimed.data.previous_status === "finished"
    ) {
      const finishedRows: AgentAutomationRunFinishTargetRow[] = (
        claimed.data.targets ?? []
      ).map((t) => ({
        entry: t.entry,
        address: t.address ?? null,
        name: t.name ?? null,
        reached: Boolean(t.reached),
        via: t.via ?? null,
        error: t.error ?? (t.ok === false ? (t.message ?? "Failed") : null),
      }));
      return { status: 200, data: { targets: finishedRows } };
    }

    // Shape A: execute finish deliveries and finalize in DB
    const finalRows = await this.executeAutomationRunFinish({
      executionId,
      userId: claimed.data.user_id || userId,
      threadId: claimed.data.thread_id ?? body.thread_id ?? null,
      automationName: claimed.data.automation_name,
      finishStatus:
        (body.status ??
          claimed.data.finish_status ??
          "completed") as AgentAutomationRunFinishRequest["status"],
      finalText: body.final_text ?? claimed.data.final_text ?? null,
      targets: claimed.data.targets ?? [],
    });

    return { status: 200, data: { targets: finalRows } };
  }

  /**
   * Shared executor for Shape A of `POST /agent/automation-runs/{execution_id}/finish`
   * and startup recovery of `dalton_channel_automation_runs` in `status == 'finishing'` (§5.2 Step 2.4).
   */
  async executeAutomationRunFinish(params: {
    executionId: string;
    userId: string;
    threadId: string | null;
    automationName: string;
    finishStatus: AgentAutomationRunFinishRequest["status"];
    finalText: string | null;
    targets: AutomationRunTargetRecord[];
  }): Promise<AgentAutomationRunFinishTargetRow[]> {
    const {
      executionId,
      userId,
      threadId,
      automationName,
      finishStatus,
      finalText,
      targets,
    } = params;

    this.activeFinishExecutors.add(executionId);
    try {
      const results: AutomationRunTargetRecord[] = [];
      let truncatedFinalText = (finalText ?? "").trim();
      if (truncatedFinalText.length > MAX_AUTOMATION_FINAL_TEXT_CHARS) {
        truncatedFinalText =
          truncatedFinalText.slice(0, MAX_AUTOMATION_FINAL_TEXT_CHARS - 1).trimEnd() +
          "…";
      }

      for (const t of targets) {
        if (!t.ok || !t.address) {
          results.push({
            ...t,
            reached: false,
            via: null,
            error: t.message ?? "Target could not be resolved.",
          });
          continue;
        }

        if (finishStatus === "completed") {
          if (t.agent_sent) {
            results.push({
              ...t,
              reached: true,
              via: "agent",
              error: null,
            });
            continue;
          }

          if (truncatedFinalText.length > 0) {
            const deliveryRes = await this.deliveryService.executeDelivery({
              deliveryKey: `automation:${executionId}:${t.entry}:summary`,
              kind: "automation",
              userId,
              threadId,
              executionId,
              target: t.address,
              text: truncatedFinalText,
            });

            if (
              deliveryRes.status === "sent" ||
              deliveryRes.status === "partial"
            ) {
              results.push({
                ...t,
                address: deliveryRes.address ?? t.address,
                reached: true,
                via: "fallback",
                error: null,
              });
            } else {
              results.push({
                ...t,
                reached: false,
                via: null,
                error: deliveryRes.message || "Failed to deliver automation summary.",
              });
            }
            continue;
          }

          results.push({
            ...t,
            reached: false,
            via: null,
            error: "Automation completed without sending a message or final summary.",
          });
          continue;
        }

        // `failed`, `stopped`, `interrupted`, `cancelled` -> send short notice
        const label = automationName ? `"${automationName}"` : "Automation";
        const noticeText = `${label} run ${finishStatus}.`;
        const noticeRes = await this.deliveryService.executeDelivery({
          deliveryKey: `automation:${executionId}:${t.entry}:notice`,
          kind: "automation",
          userId,
          threadId,
          executionId,
          target: t.address,
          text: noticeText,
        });

        if (noticeRes.status === "sent" || noticeRes.status === "partial") {
          results.push({
            ...t,
            address: noticeRes.address ?? t.address,
            reached: true,
            via: "notice",
            error: null,
          });
        } else {
          results.push({
            ...t,
            reached: false,
            via: null,
            error: noticeRes.message || "Failed to deliver automation status notice.",
          });
        }
      }

      await this.backend.finishAutomationRun(
        executionId,
        {
          status: finishStatus,
          thread_id: threadId,
          final_text: finalText,
          targets: results,
          mark_finished: true,
        },
        userId,
      );

      return results.map((r) => ({
        entry: r.entry,
        address: r.address,
        name: r.name,
        reached: Boolean(r.reached),
        via: r.via ?? null,
        error: r.error ?? null,
      }));
    } finally {
      this.activeFinishExecutors.delete(executionId);
    }
  }

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------
  /**
   * Queries `I-3 sessionCommand({ cmd: "workspace_list" })` (`src/dalton/database/channel_gateway.py:1390-1427`),
   * which invokes `_get_writable_workspace_names` (`src/server/services/channel_settings.py:352-400`)
   * and returns the user's owned workspaces plus colleague `rw` workspaces when colleague dispatch is enabled.
   */
  private async resolveWritableWorkspaceIds(
    userId: string,
    state: I4StateResponse,
  ): Promise<Set<string>> {
    const ids = new Set<string>();
    if (state.home_workspace_id) {
      ids.add(state.home_workspace_id);
    }
    try {
      const wsList = await this.backend.sessionCommand({
        cmd: "workspace_list",
        platform: "feishu",
        dalton_user_id: userId,
        chat_type: "dm",
        scope_key: "dm:targets",
      });
      if (wsList.home_workspace_id) {
        ids.add(wsList.home_workspace_id);
      }
      for (const ws of wsList.workspaces ?? []) {
        if (ws?.workspace_id) {
          ids.add(ws.workspace_id);
        }
      }
    } catch {
      // Fallback to home_workspace_id if sessionCommand is unavailable
    }
    return ids;
  }

  private getAppConfig(
    state: I4StateResponse,
    platform: ChannelPlatform,
  ): {
    preferred: string | null;
    chats: Record<string, { workspace_id: string | null; name?: string | null }>;
    automation_output: Record<string, string>;
    agent_messages: { enabled: boolean; allowed: string[] };
  } {
    const raw = (state.settings?.apps?.[platform] ?? {}) as Record<string, unknown>;
    const preferred = typeof raw.preferred === "string" ? raw.preferred : null;
    const chats =
      raw.chats && typeof raw.chats === "object"
        ? (raw.chats as Record<string, { workspace_id: string | null; name?: string | null }>)
        : {};
    const automation_output =
      raw.automation_output && typeof raw.automation_output === "object"
        ? (raw.automation_output as Record<string, string>)
        : {};
    const rawMsg =
      raw.agent_messages && typeof raw.agent_messages === "object"
        ? (raw.agent_messages as Record<string, unknown>)
        : {};
    return {
      preferred,
      chats,
      automation_output,
      agent_messages: {
        enabled: rawMsg.enabled !== false,
        allowed: Array.isArray(rawMsg.allowed)
          ? rawMsg.allowed.filter((x): x is string => typeof x === "string")
          : [`${platform}:@me`],
      },
    };
  }

  private async collectAvailableChatsForPlatform(
    userId: string,
    platform: ChannelPlatform,
    platformUserId: string,
    platformUsername: string | null,
    state: I4StateResponse,
  ): Promise<{
    chats: AvailableChatItem[];
    complete: boolean;
    error: string | null;
  }> {
    const dmAddress = `${platform}:@me`;
    const dmItem: AvailableChatItem = {
      address: dmAddress,
      name: platformUsername
        ? `Direct Messages (${platformUsername})`
        : "Direct Messages",
      kind: "dm",
    };

    // WeChat and external users only ever get direct messages (`<platform>:@me`)
    if (platform === "weixin" || state.user_tier === "external") {
      return {
        chats: [dmItem],
        complete: true,
        error: null,
      };
    }

    const chatsMap = new Map<string, AvailableChatItem>();
    chatsMap.set(dmAddress, dmItem);

    const appCfg = this.getAppConfig(state, platform);
    // Include bindings from DB that belong to this user's chats or were bound by this user
    for (const b of state.chat_bindings ?? []) {
      if (
        b.platform === platform &&
        b.chat_address &&
        b.chat_address !== dmAddress &&
        (b.bound_by_user_id === userId ||
          Object.prototype.hasOwnProperty.call(appCfg.chats, b.chat_address))
      ) {
        chatsMap.set(b.chat_address, {
          address: b.chat_address,
          name: b.chat_name || b.chat_address,
          kind: platform === "discord" ? "channel" : "group",
        });
      }
    }

    const adapter = this.adapters.get(platform);
    let complete = true;
    let error: string | null = null;

    if (adapter && adapter.isConfigured() && adapter.isConnected()) {
      try {
        const live = await adapter.listAvailableChats({
          userId,
          platformUserId,
        });
        complete = live.complete;
        error = live.error;
        for (const item of live.chats) {
          if (item.address) {
            chatsMap.set(item.address, item);
          }
        }
      } catch (err) {
        complete = false;
        error = err instanceof Error ? err.message : String(err);
      }
    } else {
      complete = false;
      error = `The ${platform} adapter is not connected.`;
    }

    return {
      chats: Array.from(chatsMap.values()),
      complete,
      error,
    };
  }
}
