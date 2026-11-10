import type { ChannelPlatform } from "./types.js";

/**
 * Strict gateway request body for `POST /internal/data-task-notifications`.
 * Only `run_id` and `target` are permitted; arbitrary card JSON, file paths,
 * URLs, user identities, and run-success claims are rejected.
 */
export interface DataTaskNotificationRequest {
  run_id: string;
  target: string;
}

export interface DataTaskPlanArtifactDescriptor {
  artifact_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  sha256: string;
  placeholder?: string;
}

export interface DataTaskFeishuCardPlanItem {
  item_index: number;
  delivery_key: string;
  kind: "feishu_card";
  card: Record<string, unknown>;
  images: DataTaskPlanArtifactDescriptor[];
  text?: string;
  existing_status?: string | null;
}

export interface DataTaskTextAndFilePlanItem {
  item_index: number;
  delivery_key: string;
  kind: "text_and_file";
  text: string;
  artifact: DataTaskPlanArtifactDescriptor;
  existing_status?: string | null;
}

export interface DataTaskTextPlanItem {
  item_index: number;
  delivery_key: string;
  kind: "text";
  text: string;
  existing_status?: string | null;
}

export type DataTaskPlanItem =
  | DataTaskFeishuCardPlanItem
  | DataTaskTextAndFilePlanItem
  | DataTaskTextPlanItem;

export interface DataTaskAuthorizePlanResponse {
  authorized: boolean;
  status: "authorized" | "failed" | "skipped";
  code?: string;
  message?: string;
  run_id?: string;
  job_key?: string;
  owner_user_id?: string;
  owner_platform_user_id?: string;
  platform?: ChannelPlatform;
  target_kind?: "dm" | "group";
  requested_target?: string;
  canonical_target_address?: string;
  recipient_id?: string;
  items?: DataTaskPlanItem[];
}

export interface DataTaskDownloadedArtifact {
  content: Buffer;
  mimeType: string;
  filename: string;
  sha256Header?: string | null;
}

export interface DataTaskItemDeliveryOutcome {
  item_index: number;
  delivery_key: string;
  status: "sent" | "partial" | "failed" | "unknown";
  duplicate: boolean;
  platform_message_ids: string[];
  code?: string | null;
  message?: string;
}

export interface DataTaskNotificationResponse {
  ok: boolean;
  status: "sent" | "skipped" | "failed" | "unknown";
  code: string | null;
  message: string;
  run_id: string;
  target: string;
  platform: ChannelPlatform | null;
  items_total: number;
  items_sent: number;
  items: DataTaskItemDeliveryOutcome[];
}
