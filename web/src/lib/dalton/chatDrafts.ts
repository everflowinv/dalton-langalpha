import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';
import * as z from 'zod/mini';
import { useUser } from '@/hooks/useUser';

export const DRAFT_STORAGE_PREFIX = 'dalton.chatDraft.v1:';
export const DRAFT_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
export const MAX_DRAFT_BYTES = 100 * 1024; // 100 KB
export const DRAFT_SAVE_DEBOUNCE_MS = 300; // 300 ms

export const StoredChatDraftSchema = z.object({
  text: z.string(),
  savedAt: z.number(),
  inFlight: z.optional(z.boolean()),
});

export type StoredChatDraft = z.output<typeof StoredChatDraftSchema>;

export interface SaveDraftOptions {
  inFlight?: boolean;
}

interface DraftController {
  getDraftKey: () => string | undefined;
  getUserId: () => string;
  cancelAndClearInput: () => void;
}

const activeControllers = new Set<DraftController>();

function normalizeUserId(userId?: string | null): string {
  const trimmed = typeof userId === 'string' ? userId.trim() : '';
  return trimmed || 'anon';
}

export function buildDraftStorageKey(
  draftKey: string,
  userId?: string | null,
): string {
  return `${DRAFT_STORAGE_PREFIX}${normalizeUserId(userId)}:${draftKey}`;
}

function getLocalStorage(): Storage | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) {
      return null;
    }
    return window.localStorage;
  } catch {
    return null;
  }
}

function getUtf8ByteLength(text: string): number {
  try {
    if (typeof TextEncoder !== 'undefined') {
      return new TextEncoder().encode(text).byteLength;
    }
  } catch {
    // Fallback below if TextEncoder is unavailable.
  }
  return unescape(encodeURIComponent(text)).length;
}

function safeRemoveItem(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // Ignore storage removal errors.
  }
}

function parseStoredDraft(raw: string): StoredChatDraft | null {
  try {
    const json: unknown = JSON.parse(raw);
    const parsed = StoredChatDraftSchema.safeParse(json);
    if (!parsed.success || !Number.isFinite(parsed.data.savedAt)) {
      return null;
    }
    return parsed.data;
  } catch {
    return null;
  }
}

function clearStorageEntry(
  draftKey: string | null | undefined,
  userId?: string | null,
): void {
  if (!draftKey) return;
  const storage = getLocalStorage();
  if (!storage) return;
  if (userId !== undefined) {
    safeRemoveItem(storage, buildDraftStorageKey(draftKey, userId));
    return;
  }
  safeRemoveItem(storage, buildDraftStorageKey(draftKey, 'anon'));
  try {
    const suffix = `:${draftKey}`;
    const matchingKeys: string[] = [];
    for (let i = 0; i < storage.length; i += 1) {
      const k = storage.key(i);
      if (k && k.startsWith(DRAFT_STORAGE_PREFIX) && k.endsWith(suffix)) {
        matchingKeys.push(k);
      }
    }
    for (const k of matchingKeys) {
      safeRemoveItem(storage, k);
    }
  } catch {
    // Ignore storage enumeration errors.
  }
}

function loadStoredDraft(
  draftKey: string | null | undefined,
  userId?: string | null,
  now: number = Date.now(),
): StoredChatDraft | null {
  if (!draftKey) return null;
  const storage = getLocalStorage();
  if (!storage) return null;
  const storageKey = buildDraftStorageKey(draftKey, userId);
  try {
    const raw = storage.getItem(storageKey);
    if (!raw) return null;
    const parsed = parseStoredDraft(raw);
    if (
      !parsed ||
      !parsed.text ||
      !parsed.text.trim() ||
      now - parsed.savedAt > DRAFT_TTL_MS
    ) {
      safeRemoveItem(storage, storageKey);
      return null;
    }
    return parsed;
  } catch {
    safeRemoveItem(storage, storageKey);
    return null;
  }
}

/**
 * Load a saved composer draft for `(userId, draftKey)`.
 * Validates with Zod and deletes entries that are corrupt, empty, or older than 7 days.
 */
export function load(
  draftKey: string | null | undefined,
  userId?: string | null,
  now: number = Date.now(),
): string {
  return loadStoredDraft(draftKey, userId, now)?.text ?? '';
}

/**
 * Remove a composer draft for `(userId, draftKey)` and cancel any pending
 * debounced save on mounted inputs bound to the same `draftKey`.
 */
export function clear(
  draftKey: string | null | undefined,
  userId?: string | null,
): void {
  if (!draftKey) return;
  clearStorageEntry(draftKey, userId);
  const normalizedTargetUser =
    userId !== undefined ? normalizeUserId(userId) : null;
  for (const controller of activeControllers) {
    if (
      controller.getDraftKey() === draftKey &&
      (normalizedTargetUser === null ||
        controller.getUserId() === normalizedTargetUser)
    ) {
      clearStorageEntry(draftKey, controller.getUserId());
      controller.cancelAndClearInput();
    }
  }
}

/**
 * Save a composer draft for `(userId, draftKey)`.
 * Clears the stored draft when `text` is empty or exceeds 100 KB (so an older
 * draft under the same key cannot resurrect), and silently degrades if
 * `localStorage` is unavailable or full.
 */
export function save(
  draftKey: string | null | undefined,
  text: string,
  userId?: string | null,
  now: number = Date.now(),
  options?: SaveDraftOptions,
): boolean {
  if (!draftKey) return false;
  if (!text || !text.trim()) {
    clearStorageEntry(draftKey, userId ?? 'anon');
    return false;
  }
  if (getUtf8ByteLength(text) > MAX_DRAFT_BYTES) {
    clearStorageEntry(draftKey, userId ?? 'anon');
    return false;
  }
  const storage = getLocalStorage();
  if (!storage) return false;
  const storageKey = buildDraftStorageKey(draftKey, userId);
  try {
    const payload: StoredChatDraft = options?.inFlight
      ? { text, savedAt: now, inFlight: true }
      : { text, savedAt: now };
    storage.setItem(storageKey, JSON.stringify(payload));
    return true;
  } catch {
    return false;
  }
}

/**
 * Scan `localStorage` for `dalton.chatDraft.v1:*` keys and remove any expired
 * (> 7 days) or malformed entries.
 */
export function pruneExpired(now: number = Date.now()): number {
  const storage = getLocalStorage();
  if (!storage) return 0;
  let removed = 0;
  try {
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i += 1) {
      const k = storage.key(i);
      if (k && k.startsWith(DRAFT_STORAGE_PREFIX)) {
        keys.push(k);
      }
    }
    for (const key of keys) {
      try {
        const raw = storage.getItem(key);
        if (!raw) continue;
        const parsed = parseStoredDraft(raw);
        if (
          !parsed ||
          !parsed.text.trim() ||
          now - parsed.savedAt > DRAFT_TTL_MS
        ) {
          safeRemoveItem(storage, key);
          removed += 1;
        }
      } catch {
        safeRemoveItem(storage, key);
        removed += 1;
      }
    }
  } catch {
    // Ignore storage enumeration errors.
  }
  return removed;
}

export const clearExpired = pruneExpired;

function isNewWorkspaceDraftKey(key?: string): boolean {
  return typeof key === 'string' && /^ws:[^:]+:new$/.test(key);
}

function isThreadDraftKey(key?: string): boolean {
  return (
    typeof key === 'string' &&
    key.startsWith('thread:') &&
    key.length > 'thread:'.length
  );
}

export interface UseComposerDraftOptions {
  draftKey?: string;
  prefillMessage?: string;
  onDraftKeySwitch?: () => void;
}

export interface PendingSendHandle {
  finalizeSend: (succeeded: boolean) => void;
  /**
   * Settle this send as accepted before its promise resolves: the server has
   * taken it (a run_id arrived, a steer was accepted, the send was queued, a
   * duplicate was adopted, …), so its draft is safe to clear even though the
   * stream may run on for a long time — a reload mid-stream must not restore
   * an already-sent message into the composer. A later finalizeSend on the
   * same handle is a no-op.
   */
  acceptSend: () => void;
}

interface InFlightSendState {
  draftKey?: string;
  userId: string;
  sentText: string;
  settled: boolean;
}

export function settleSendResult(
  pending: PendingSendHandle,
  invoke: () => void | boolean | Promise<void | boolean>,
): void {
  let result: void | boolean | Promise<void | boolean>;
  try {
    result = invoke();
  } catch {
    pending.finalizeSend(false);
    return;
  }
  if (
    result &&
    typeof (result as Promise<void | boolean>).then === 'function'
  ) {
    (result as Promise<void | boolean>).then(
      (resolved) => pending.finalizeSend(resolved === true),
      () => pending.finalizeSend(false),
    );
    return;
  }
  pending.finalizeSend(result === true);
}

export interface UseComposerDraftResult {
  message: string;
  setMessage: Dispatch<SetStateAction<string>>;
  restoredFromInFlight: boolean;
  clearCurrentDraft: () => void;
  beginSend: () => PendingSendHandle;
  submitWithDraft: (
    invoke: (onAccepted: () => void) => void | boolean | Promise<void | boolean>,
  ) => void;
}

/**
 * Manages the composer text state backed by `localStorage` when `draftKey` is provided:
 * - Restores on mount and after `draftKey` changes unless `prefillMessage` is active.
 * - Debounces writes by 300ms on input changes and clears immediately when emptied by user.
 * - Synchronously flushes pending edits before `draftKey` changes and on unmount.
 * - Migrates post-send continuation text when transitioning from `ws:<id>:new` to `thread:<id>`.
 */
export function useComposerDraft({
  draftKey,
  prefillMessage,
  onDraftKeySwitch,
}: UseComposerDraftOptions): UseComposerDraftResult {
  const { user } = useUser({ enabled: Boolean(draftKey) });
  const userId = normalizeUserId(user?.user_id);

  const [message, setMessageState] = useState<string>(() => {
    if (prefillMessage) return prefillMessage;
    if (!draftKey) return '';
    return load(draftKey, userId);
  });
  const [restoredFromInFlight, setRestoredFromInFlightState] = useState<boolean>(() => {
    if (prefillMessage || !draftKey) return false;
    return loadStoredDraft(draftKey, userId)?.inFlight === true;
  });

  const latestMessageRef = useRef<string>(message);
  const restoredFromInFlightRef = useRef<boolean>(restoredFromInFlight);
  const dirtyRef = useRef<boolean>(false);
  const writtenSinceMountOrSendRef = useRef<boolean>(false);
  const hasSentOnKeyRef = useRef<boolean>(false);
  const inFlightSendRef = useRef<InFlightSendState | null>(null);
  // The latest send per (user, key). Kept after that send settles, so an older send on
  // the same key that completes later still knows it was superseded.
  const latestSendByKeyRef = useRef(new Map<string, InFlightSendState>());
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const activeDraftKeyRef = useRef<string | undefined>(draftKey);
  const activeUserIdRef = useRef<string>(userId);
  const prefillMessageRef = useRef<string | undefined>(prefillMessage);
  const hadPrefillRef = useRef<boolean>(Boolean(prefillMessage));
  const onDraftKeySwitchRef = useRef<(() => void) | undefined>(onDraftKeySwitch);
  const prevTargetRef = useRef<{ draftKey?: string; userId: string }>({
    draftKey,
    userId,
  });

  const updateRestoredFromInFlight = useCallback((next: boolean) => {
    restoredFromInFlightRef.current = next;
    setRestoredFromInFlightState(next);
  }, []);

  useLayoutEffect(() => {
    activeDraftKeyRef.current = draftKey;
    activeUserIdRef.current = userId;
    prefillMessageRef.current = prefillMessage;
    if (prefillMessage) {
      hadPrefillRef.current = true;
    }
    onDraftKeySwitchRef.current = onDraftKeySwitch;
  });

  useLayoutEffect(() => {
    const controller: DraftController = {
      getDraftKey: () => activeDraftKeyRef.current,
      getUserId: () => activeUserIdRef.current,
      cancelAndClearInput: () => {
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
        dirtyRef.current = false;
        writtenSinceMountOrSendRef.current = false;
        latestMessageRef.current = '';
        updateRestoredFromInFlight(false);
        setMessageState('');
      },
    };
    activeControllers.add(controller);
    return () => {
      activeControllers.delete(controller);
    };
  }, [updateRestoredFromInFlight]);

  const scheduleSave = useCallback((nextText: string) => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const key = activeDraftKeyRef.current;
    const uid = activeUserIdRef.current;
    if (!key) {
      dirtyRef.current = false;
      return;
    }
    if (!nextText || !nextText.trim()) {
      dirtyRef.current = false;
      writtenSinceMountOrSendRef.current = false;
      clearStorageEntry(key, uid);
      return;
    }
    dirtyRef.current = true;
    writtenSinceMountOrSendRef.current = true;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      if (
        dirtyRef.current &&
        activeDraftKeyRef.current === key &&
        activeUserIdRef.current === uid
      ) {
        save(key, latestMessageRef.current, uid);
        dirtyRef.current = false;
      }
    }, DRAFT_SAVE_DEBOUNCE_MS);
  }, []);

  const setMessage: Dispatch<SetStateAction<string>> = useCallback(
    (action) => {
      const next =
        typeof action === 'function'
          ? action(latestMessageRef.current)
          : action;
      const wasInFlight = restoredFromInFlightRef.current;
      latestMessageRef.current = next;
      updateRestoredFromInFlight(false);
      setMessageState(next);
      if (wasInFlight && activeDraftKeyRef.current && next && next.trim()) {
        save(activeDraftKeyRef.current, next, activeUserIdRef.current);
      }
      scheduleSave(next);
    },
    [scheduleSave, updateRestoredFromInFlight],
  );

  const clearCurrentDraft = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    dirtyRef.current = false;
    writtenSinceMountOrSendRef.current = false;
    latestMessageRef.current = '';
    updateRestoredFromInFlight(false);
    if (activeDraftKeyRef.current) {
      clearStorageEntry(activeDraftKeyRef.current, activeUserIdRef.current);
    }
    setMessageState('');
  }, [updateRestoredFromInFlight]);

  const beginSend = useCallback((): PendingSendHandle => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const sentText = latestMessageRef.current;
    const inFlight: InFlightSendState = {
      draftKey: activeDraftKeyRef.current,
      userId: activeUserIdRef.current,
      sentText,
      settled: false,
    };
    inFlightSendRef.current = inFlight;
    if (inFlight.draftKey) {
      latestSendByKeyRef.current.set(`${inFlight.userId ?? ''}|${inFlight.draftKey}`, inFlight);
    }

    // Ensure the draft is persisted in localStorage while awaiting send completion,
    // and clear the input box programmatically without deleting the stored draft.
    if (inFlight.draftKey && sentText.trim()) {
      save(inFlight.draftKey, sentText, inFlight.userId, Date.now(), { inFlight: true });
    }
    dirtyRef.current = false;
    writtenSinceMountOrSendRef.current = false;
    hasSentOnKeyRef.current = true;
    latestMessageRef.current = '';
    updateRestoredFromInFlight(false);
    setMessageState('');

    const settleSuccess = () => {
      const sendDraftKey = inFlight.draftKey;
      const sendUserId = inFlight.userId;
      const isSameKey = activeDraftKeyRef.current === sendDraftKey;
      const hasFollowUp =
        isSameKey &&
        (writtenSinceMountOrSendRef.current ||
          Boolean(latestMessageRef.current.trim()));
      if (!hasFollowUp && sendDraftKey) {
        clearStorageEntry(sendDraftKey, sendUserId);
      }
    };

    const settle = (succeeded: boolean) => {
      if (inFlight.settled) return;
      inFlight.settled = true;
      // A newer send on this key may have superseded this one while it was in
      // flight (a steer sent mid-stream, say): the key's stored draft and the
      // input are that send's now — this settle touches neither, or the first
      // send's completion would delete or restore the second send's draft.
      // Supersession is per key: a newer send on another key (the market panel's key moves
      // from its workspace form to the new thread's once the thread exists) does not own
      // this send's stored draft, so this send still settles its own key.
      const latestOnKey = inFlight.draftKey
        ? latestSendByKeyRef.current.get(`${inFlight.userId ?? ''}|${inFlight.draftKey}`)
        : undefined;
      if (latestOnKey && latestOnKey !== inFlight) return;
      if (inFlightSendRef.current === inFlight) inFlightSendRef.current = null;

      if (succeeded) {
        settleSuccess();
        return;
      }
      const sendDraftKey = inFlight.draftKey;
      const sendUserId = inFlight.userId;
      if (!sendDraftKey) {
        return;
      }
      // Send failed: keep the draft in localStorage and put the original text back.
      if (activeDraftKeyRef.current === sendDraftKey) {
        hasSentOnKeyRef.current = false;
        if (timerRef.current !== null) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
        const trimmedSent = sentText.trim();
        const currentTyped = latestMessageRef.current.trim();
        const alreadyRestored =
          Boolean(trimmedSent) &&
          (currentTyped === trimmedSent ||
            currentTyped.startsWith(`${trimmedSent}\n\n`) ||
            currentTyped.endsWith(`\n\n${trimmedSent}`));
        const restoredText = sentText
          ? alreadyRestored
            ? latestMessageRef.current
            : currentTyped
              ? `${sentText}\n\n${latestMessageRef.current}`
              : sentText
          : latestMessageRef.current;
        if (restoredText.trim()) {
          save(sendDraftKey, restoredText, sendUserId);
        }
        if (restoredText) {
          latestMessageRef.current = restoredText;
          dirtyRef.current = false;
          writtenSinceMountOrSendRef.current = true;
          updateRestoredFromInFlight(false);
          setMessageState(restoredText);
        }
      } else if (sentText.trim()) {
        save(sendDraftKey, sentText, sendUserId);
      }
    };

    return {
      finalizeSend(succeeded: boolean) {
        settle(succeeded);
      },
      acceptSend() {
        settle(true);
      },
    };
  }, [updateRestoredFromInFlight]);

  const submitWithDraft = useCallback(
    (invoke: (onAccepted: () => void) => void | boolean | Promise<void | boolean>) => {
      const pending = beginSend();
      // The send path reports acceptance early (a run started, a steer was
      // taken, the send was queued) by calling onAccepted; the promise's
      // resolved value is the fallback for a send that never reported either.
      let result: void | boolean | Promise<void | boolean>;
      try {
        result = invoke(pending.acceptSend);
      } catch {
        pending.finalizeSend(false);
        return;
      }
      settleSendResult(pending, () => result);
    },
    [beginSend],
  );

  // Sweep expired drafts once when mounted with a draftKey.
  const hasDraftKey = Boolean(draftKey);
  useEffect(() => {
    if (hasDraftKey) {
      pruneExpired();
    }
  }, [hasDraftKey]);

  // Flush current content synchronously before draftKey/userId changes or on unmount,
  // and restore (or migrate post-send continuation) when draftKey/userId changes.
  useEffect(() => {
    const prev = prevTargetRef.current;
    const draftKeyChanged = prev.draftKey !== draftKey;
    const userIdChanged = prev.userId !== userId;

    if (draftKeyChanged || userIdChanged) {
      prevTargetRef.current = { draftKey, userId };

      // Migrate post-send continuation draft when `ws:<id>:new` resolves to `thread:<id>`
      // only if a send actually occurred on `ws:<id>:new`.
      if (
        draftKeyChanged &&
        hasSentOnKeyRef.current &&
        isNewWorkspaceDraftKey(prev.draftKey) &&
        isThreadDraftKey(draftKey)
      ) {
        const inFlight = inFlightSendRef.current;
        const sendStillInFlight =
          inFlight !== null &&
          !inFlight.settled &&
          inFlight.draftKey === prev.draftKey;
        if (sendStillInFlight && inFlight) {
          clearStorageEntry(prev.draftKey, prev.userId);
          inFlight.draftKey = draftKey;
          inFlight.userId = userId;
          // The send now owns its new key's generation (it migrated with it).
          latestSendByKeyRef.current.set(`${userId ?? ''}|${draftKey}`, inFlight);
          const followUpText = writtenSinceMountOrSendRef.current
            ? latestMessageRef.current
            : '';
          hasSentOnKeyRef.current = false;
          hadPrefillRef.current = Boolean(prefillMessageRef.current);
          updateRestoredFromInFlight(false);
          onDraftKeySwitchRef.current?.();
          if (followUpText && followUpText.trim()) {
            save(draftKey, followUpText, userId);
            latestMessageRef.current = followUpText;
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = true;
            setMessageState(followUpText);
          } else {
            if (inFlight.sentText.trim()) {
              save(draftKey, inFlight.sentText, userId, Date.now(), { inFlight: true });
            }
            latestMessageRef.current = '';
            dirtyRef.current = false;
            writtenSinceMountOrSendRef.current = false;
            setMessageState('');
          }
          return () => {
            if (timerRef.current !== null) {
              clearTimeout(timerRef.current);
              timerRef.current = null;
            }
            if (dirtyRef.current && draftKey) {
              save(draftKey, latestMessageRef.current, userId);
              dirtyRef.current = false;
            }
          };
        }

        const continuationText = writtenSinceMountOrSendRef.current
          ? latestMessageRef.current || load(prev.draftKey, prev.userId)
          : load(prev.draftKey, prev.userId);
        hasSentOnKeyRef.current = false;
        writtenSinceMountOrSendRef.current = false;
        hadPrefillRef.current = Boolean(prefillMessageRef.current);
        updateRestoredFromInFlight(false);
        onDraftKeySwitchRef.current?.();

        if (continuationText && continuationText.trim()) {
          clearStorageEntry(prev.draftKey, prev.userId);
          save(draftKey, continuationText, userId);
          latestMessageRef.current = continuationText;
          dirtyRef.current = false;
          writtenSinceMountOrSendRef.current = true;
          setMessageState(continuationText);
          return () => {
            if (timerRef.current !== null) {
              clearTimeout(timerRef.current);
              timerRef.current = null;
            }
            if (dirtyRef.current && draftKey) {
              save(draftKey, latestMessageRef.current, userId);
              dirtyRef.current = false;
            }
          };
        }
      }

      if (draftKeyChanged) {
        hasSentOnKeyRef.current = false;
        writtenSinceMountOrSendRef.current = false;
        hadPrefillRef.current = Boolean(prefillMessageRef.current);
        updateRestoredFromInFlight(false);
        onDraftKeySwitchRef.current?.();
      }
      const hasExternalPrefill =
        Boolean(prefillMessageRef.current) ||
        (!draftKeyChanged && hadPrefillRef.current);
      if (
        !hasExternalPrefill &&
        !(userIdChanged && !draftKeyChanged && dirtyRef.current)
      ) {
        const restoredRecord = draftKey ? loadStoredDraft(draftKey, userId) : null;
        const restored = restoredRecord?.text ?? '';
        latestMessageRef.current = restored;
        dirtyRef.current = false;
        updateRestoredFromInFlight(Boolean(restored && restoredRecord?.inFlight));
        setMessageState(restored);
      }
    }

    return () => {
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
      if (dirtyRef.current && draftKey) {
        save(draftKey, latestMessageRef.current, userId);
        dirtyRef.current = false;
      }
    };
  }, [draftKey, userId, updateRestoredFromInFlight]);

  return {
    message,
    setMessage,
    restoredFromInFlight,
    clearCurrentDraft,
    beginSend,
    submitWithDraft,
  };
}
