/**
 * Pure size and chunking limits for Dalton chat attachments.
 *
 * Kept free of API client and browser globals so pure helper modules and
 * Node-environment tests can import these limits without pulling in auth
 * or DOM dependencies.
 */

export const MAX_CHAT_UPLOAD_FILE_SIZE = 200 * 1024 * 1024; // 200 MiB inclusive
export const MAX_TOTAL_CHAT_UPLOAD_BYTES = 600 * 1024 * 1024; // 600 MiB per message
export const CHAT_UPLOAD_PART_SIZE = 8 * 1024 * 1024; // 8 MiB per chunk
export const MAX_PARALLEL_PARTS = 2;
