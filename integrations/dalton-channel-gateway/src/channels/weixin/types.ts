/**
 * Weixin iLink Bot HTTP JSON protocol and adapter types.
 *
 * Adapted from `@tencent-weixin/openclaw-weixin` (`src/api/types.ts`, MIT, Copyright (c) 2026 Tencent).
 */

export const ILINK_CHANNEL_VERSION = "2.1.5";
export const ILINK_DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com";
export const ILINK_DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c";
export const ILINK_DEFAULT_BOT_TYPE = "3";
export const SESSION_EXPIRED_ERRCODE = -14;

export const OFFICIAL_ILINK_API_DOMAINS: readonly string[] = [
  "ilinkai.weixin.qq.com",
  "weixin.qq.com",
];

export const OFFICIAL_WEIXIN_CDN_DOMAINS: readonly string[] = [
  "novac2c.cdn.weixin.qq.com",
  "cdn.weixin.qq.com",
  "wx.qlogo.cn",
  "qpic.cn",
];

export const MessageType = {
  NONE: 0,
  USER: 1,
  BOT: 2,
} as const;

export const MessageItemType = {
  NONE: 0,
  TEXT: 1,
  IMAGE: 2,
  VOICE: 3,
  FILE: 4,
  VIDEO: 5,
} as const;

export const MessageState = {
  NEW: 0,
  GENERATING: 1,
  FINISH: 2,
} as const;

export const UploadMediaType = {
  IMAGE: 1,
  VIDEO: 2,
  FILE: 3,
  VOICE: 4,
} as const;

export interface BaseInfo {
  channel_version?: string;
}

export interface CDNMedia {
  encrypt_query_param?: string;
  aes_key?: string;
  encrypt_type?: number;
  full_url?: string;
}

export interface TextItem {
  text?: string;
}

export interface ImageItem {
  media?: CDNMedia;
  thumb_media?: CDNMedia;
  aeskey?: string;
  url?: string;
  mid_size?: number;
  thumb_size?: number;
  thumb_height?: number;
  thumb_width?: number;
  hd_size?: number;
}

export interface VoiceItem {
  media?: CDNMedia;
  encode_type?: number;
  bits_per_sample?: number;
  sample_rate?: number;
  playtime?: number;
  text?: string;
}

export interface FileItem {
  media?: CDNMedia;
  file_name?: string;
  md5?: string;
  len?: string;
}

export interface VideoItem {
  media?: CDNMedia;
  video_size?: number;
  play_length?: number;
  video_md5?: string;
  thumb_media?: CDNMedia;
  thumb_size?: number;
  thumb_height?: number;
  thumb_width?: number;
}

export interface RefMessage {
  message_item?: MessageItem;
  title?: string;
  message_id?: string | number;
  msg_id?: string | number;
  svr_id?: string | number;
}

export interface MessageItem {
  type?: number;
  create_time_ms?: number;
  update_time_ms?: number;
  is_completed?: boolean;
  msg_id?: string;
  ref_msg?: RefMessage;
  text_item?: TextItem;
  image_item?: ImageItem;
  voice_item?: VoiceItem;
  file_item?: FileItem;
  video_item?: VideoItem;
}

export interface WeixinMessage {
  seq?: number;
  message_id?: string | number;
  msg_id?: string | number;
  svr_id?: string | number;
  from_user_id?: string;
  to_user_id?: string;
  group_id?: string;
  chat_room_id?: string;
  room_id?: string;
  client_id?: string;
  create_time_ms?: number;
  update_time_ms?: number;
  delete_time_ms?: number;
  session_id?: string;
  message_type?: number;
  message_state?: number;
  item_list?: MessageItem[];
  context_token?: string;
}

export interface GetUpdatesReq {
  get_updates_buf?: string;
  base_info?: BaseInfo;
}

export interface GetUpdatesResp {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  msgs?: WeixinMessage[];
  get_updates_buf?: string;
  longpolling_timeout_ms?: number;
}

export interface SendMessageReq {
  msg?: WeixinMessage;
  base_info?: BaseInfo;
}

export interface SendMessageResp {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  message_id?: string | number;
  msg_id?: string | number;
}

export interface GetUploadUrlReq {
  filekey?: string;
  media_type?: number;
  to_user_id?: string;
  rawsize?: number;
  rawfilemd5?: string;
  filesize?: number;
  thumb_rawsize?: number;
  thumb_rawfilemd5?: string;
  thumb_filesize?: number;
  no_need_thumb?: boolean;
  aeskey?: string;
  base_info?: BaseInfo;
}

export interface GetUploadUrlResp {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  upload_param?: string;
  thumb_upload_param?: string;
  upload_full_url?: string;
}

export interface QrCodeStartResp {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  qrcode?: string;
  qrcode_img_content?: string;
}

export type WeixinQrPollStatus =
  | "wait"
  | "scaned"
  | "verify_code"
  | "confirmed"
  | "expired"
  | "scaned_but_redirect";

export interface QrCodeStatusResp {
  ret?: number;
  errcode?: number;
  errmsg?: string;
  status?: WeixinQrPollStatus;
  bot_token?: string;
  ilink_bot_id?: string;
  baseurl?: string;
  ilink_user_id?: string;
  ilink_username?: string;
  redirect_host?: string;
}

export interface WeixinPersistedAttachmentMeta {
  id: string;
  kind: "image" | "file" | "video" | "voice";
  filename: string;
  mimeType?: string;
  sizeBytes?: number;
  encryptQueryParam?: string;
  fullUrl?: string;
  aesKeyHex?: string;
  aesKeyBase64?: string;
}

export interface WeixinQrPublicResult {
  session_key: string;
  status: "wait" | "scaned" | "verify_code" | "confirmed" | "expired";
  qrcode_url?: string;
  qrcode_img_content?: string;
  expires_at?: string;
  ilink_bot_id?: string | null;
  ilink_user_id?: string | null;
  ilink_username?: string | null;
  connected?: boolean;
  already_connected?: boolean;
  need_rescan?: boolean;
  message?: string;
}

export interface WeixinQrInternalConfirmedResult extends WeixinQrPublicResult {
  bot_token?: string;
  base_url?: string;
  bot_generation?: number;
}
