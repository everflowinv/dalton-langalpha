import { useCallback, useEffect, useRef, useState } from 'react';
import { useIsMutating, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from '@/components/ui/use-toast';
import { isModelAvailable, resolveComposerModel } from '@/components/ui/chat-input.models'; // DALTON-HOOK(ui-model-pick)
import type { SubscriptionProvider } from '@/api/oauth';
import type { Thread } from '@/types/api';
import { useAllModels } from '@/hooks/useAllModels';
import { useLapsedSignIn } from '@/hooks/useLapsedSignIn';
// DALTON-HOOK(antigravity-series): inspect series membership & resolve thread write permissions before issuing PATCH (Dalton builds only)
import { isDaltonBuild } from '@/config/brand';
import { useThreadWriteAccess } from '@/lib/dalton/useThreadWriteAccess';
import {
  formatAntigravityVariantLabel,
  inspectSavedAntigravityModel,
  sameAntigravitySeries,
} from '@/lib/antigravitySeries';
import { useDefaultModel } from '@/hooks/useSlotDefaultModels';
import { useSeededModel } from '@/hooks/useSeededModel';
import { useTurnDefaultModel } from '@/hooks/useTurnModels';
import { isClaudeCodeModel } from '@/lib/claudeCode';
import { modelLabel } from '@/lib/modelLabel';
import { queryKeys } from '@/lib/queryKeys';
import { threadFieldMutationKey } from '@/lib/threadFieldMutations';
import { userSessionStorage } from '@/lib/userStorage';
import { threadDetailQuery } from '../utils/threadQueries';
import { useThreadFieldSave } from './useThreadFieldSave';

/** Per thread, for the session: one dismissal answers the offer for that
 *  thread, wherever the thread is open next. */
const OFFER_DISMISSED_PREFIX = 'thread-model-offer-dismissed:';

/** A thread model that left the catalog, and the default now standing in. */
export interface RetiredModel {
  model: string;
  fallback: string;
}

/** The composer's model, on a subscription whose sign-in has to be renewed. */
export interface LapsedSignIn {
  model: string;
  provider: SubscriptionProvider;
}

/** The account's Default, a Claude Code model a Chief of Staff turn cannot
 *  run yet, and the model the composer starts on instead. */
export interface HeldDefault {
  model: string;
  fallback: string;
}

/** A pick the user may make the account default. */
export interface DefaultModelOffer {
  model: string;
  defaultModel: string;
}

interface UseThreadModelOptions {
  /** The view's thread; `__default__` until the first send creates one. */
  threadId: string | null | undefined;
  /** Whether a turn is running. Its end is when the send's model reaches the row. */
  isLoading: boolean;
  /** The model a navigation's first message went out with, which the thread
   *  row does not carry until that send has stored it. */
  initialModel?: string | null;
  /** Whether the thread is the Chief of Staff's, whose turn may start on
   *  another Default than the account's (`useTurnDefaultModel`). */
  chiefOfStaff?: boolean;
  // DALTON-HOOK(antigravity-series): workspace id to detect shared workspaces before issuing PATCH
  workspaceId?: string | null;
}

/**
 * The model a chat view's thread runs on: what its composer shows, what the
 * next send names, and the one place a pick on it is saved.
 *
 * The thread row is the source. The model starts on the row's model while it
 * is reachable (else the Default its turn starts on, the Chief of Staff's
 * for a thread in Home) and follows the row when it moves; a
 * pick holds in between and is saved on the row (`useThreadFieldSave`). A
 * thread not created yet has no row to write, so its pick is held here and
 * goes out with the first send, which stores it.
 *
 * A thread whose row names no model, or one that left the catalog, shows the
 * Default it would start on but sends none until a pick (`sendModel`), so
 * the server clears the model that left and resolves the Default itself.
 *
 * A pick also raises the offer to make that model the account default, shown
 * while the composer still shows the pick and it differs from the default.
 */
export function useThreadModel({
  threadId,
  isLoading,
  initialModel = null,
  chiefOfStaff = false,
  // DALTON-HOOK(antigravity-series): accept workspaceId to skip PATCH on shared workspaces
  workspaceId = null,
}: UseThreadModelOptions) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { catalogModelNames, metadata, customModels, rawApiResponse, seriesCatalog } = useAllModels();
  const rawMetadata = rawApiResponse?.model_metadata ?? metadata;
  const defaultModel = useDefaultModel();
  const lapsedOf = useLapsedSignIn();
  const liveThreadId = threadId && threadId !== '__default__' ? threadId : null;

  const { data: thread, isError: threadError } = useQuery({
    ...threadDetailQuery(liveThreadId ?? ''),
    enabled: !!liveThreadId,
  });
  // DALTON-HOOK(antigravity-series): distinguish confirmed writable vs shared/foreign vs unresolved permissions in Dalton builds; non-Dalton builds match upstream immediate-PATCH behavior
  const writeAccess = useThreadWriteAccess({
    workspaceId,
    liveThreadId,
    thread,
    threadError,
    checkUpstreamShared: isDaltonBuild,
  });
  const isSharedUnresolved = writeAccess.status === 'unresolved';
  const isSharedWorkspace = writeAccess.status === 'shared';
  const isSharedOrUnresolved = isSharedUnresolved || isSharedWorkspace;
  // A thread whose row has not been read (still loading, or the read failed)
  // seeds no model rather than the default: the composer names what it shows,
  // and the send would store the default over the model the thread holds.
  // With no model named, the server runs the thread's own.
  const unread = !!liveThreadId && !thread && !initialModel;
  const own = thread?.llm_model || initialModel || null;
  const startsOn = useTurnDefaultModel(chiefOfStaff);
  const { seed, retired: retiredName } = unread
    ? { seed: null, retired: null }
    : resolveComposerModel(own, startsOn, catalogModelNames);

  // DALTON-HOOK(ui-model-pick): hold an unconfirmed pick (or initialModel on a
  // new thread whose row is not read yet) across stale mid-turn row reads
  // until the save confirms it, a turn-end reread finishes, the model retires,
  // the save fails, or the view switches threads.
  const [pending, setPending] = useState<string | null>(() => (
    !thread && initialModel ? initialModel : null
  ));
  const pendingRef = useRef<string | null>(pending);
  const createdHereRef = useRef(false);

  // A thread started from another page's composer opens with the pick made
  // there. That composer seeds from the default, so a model other than the
  // default is a pick like one made here; the gate below drops the default.
  const [offered, setOffered] = useState<string | null>(initialModel);
  const [dismissed, setDismissed] = useState(false);

  // One instance can move between threads (the market panel keeps its
  // composer across them), and a pick and its offer belong to the thread they
  // were made on. The seed alone cannot carry a move: two threads whose rows
  // are both unread seed the same null, and the pick would ride to the next
  // send there. A thread being created by its first send is not a move.
  const [heldThreadId, setHeldThreadId] = useState(liveThreadId);
  // DALTON-HOOK(agent-switch-model): only include llm_model in send requests when the user manually picked a model on this page
  // DALTON-HOOK(antigravity-series): tag local picks with a monotonic seq so unsent picks matching stale cached detail are never cleared early, while consumed picks release on turn-end reread
  const pickSeqRef = useRef(0);
  const [manualPick, setManualPick] = useState<{ model: string; seq: number } | null>(null);
  // DALTON-HOOK(antigravity-series): retain a pick made while shared workspace membership is loading
  const [deferredSharedPick, setDeferredSharedPick] = useState<{
    threadId: string;
    picked: string;
    before: string | null;
    seq: number;
  } | null>(null);
  const consumedTurnPickRef = useRef<{
    threadId: string | null;
    model: string;
    seq: number;
    startThread: Thread | undefined;
  } | null>(null);
  const awaitingRereadPickRef = useRef<{
    threadId: string | null;
    model: string;
    seq: number;
    startThread: Thread | undefined;
  } | null>(null);
  let nextPending = pending;
  let nextManualPick = manualPick;
  let nextDeferredSharedPick = deferredSharedPick;

  if (heldThreadId !== liveThreadId) {
    if (heldThreadId) {
      nextPending = null;
      createdHereRef.current = false;
      setPending(null);
      nextManualPick = null;
      setManualPick(null);
      // DALTON-HOOK(antigravity-series): drop deferred shared-loading pick and turn-tracking refs when switching threads
      nextDeferredSharedPick = null;
      setDeferredSharedPick(null);
      consumedTurnPickRef.current = null;
      awaitingRereadPickRef.current = null;
    } else if (liveThreadId) {
      createdHereRef.current = true;
    }
  }

  // DALTON-HOOK(antigravity-series): clear pending/deferred/shared picks only when the pick itself is unavailable (not when an older saved row model is retired)
  if (nextPending !== null && !isModelAvailable(nextPending, catalogModelNames)) {
    nextPending = null;
    createdHereRef.current = false;
    setPending(null);
  }

  if (
    nextDeferredSharedPick !== null
    && !isModelAvailable(nextDeferredSharedPick.picked, catalogModelNames)
  ) {
    nextDeferredSharedPick = null;
    setDeferredSharedPick(null);
  }

  if (
    isSharedOrUnresolved
    && nextManualPick !== null
    && !isModelAvailable(nextManualPick.model, catalogModelNames)
  ) {
    nextManualPick = null;
    setManualPick(null);
  }

  // DALTON-HOOK(antigravity-series): only clear a shared/unresolved local pick on detail confirmation if that exact pick was already sent in a turn and a fresh thread detail landed
  const awaitingPick = awaitingRereadPickRef.current;
  if (
    awaitingPick !== null
    && liveThreadId
    && thread
    && thread !== awaitingPick.startThread
    && thread.llm_model === awaitingPick.model
  ) {
    if (nextDeferredSharedPick?.seq === awaitingPick.seq) {
      nextDeferredSharedPick = null;
      setDeferredSharedPick(null);
    }
    if (nextManualPick?.seq === awaitingPick.seq) {
      nextManualPick = null;
      setManualPick(null);
    }
    awaitingRereadPickRef.current = null;
  }

  pendingRef.current = nextPending;
  const manualPickRef = useRef(nextManualPick);
  manualPickRef.current = nextManualPick;
  const deferredSharedPickRef = useRef(nextDeferredSharedPick);
  deferredSharedPickRef.current = nextDeferredSharedPick;
  const threadRef = useRef(thread);
  threadRef.current = thread;

  // DALTON-HOOK(antigravity-series): keep deferred/shared pick driving displayed and sent model when thread detail loads before shared list resolves
  const effectiveSeed = nextPending
    ?? nextDeferredSharedPick?.picked
    ?? (isSharedOrUnresolved ? nextManualPick?.model : null)
    ?? seed;
  const [model, setModel] = useSeededModel(effectiveSeed);

  // DALTON-HOOK(antigravity-series): keep local manualPick across rerenders on shared/unresolved threads or while a deferred pick awaits flush
  if (
    !isSharedOrUnresolved
    && nextManualPick !== null
    && nextPending === null
    && nextDeferredSharedPick === null
    && seed !== nextManualPick.model
  ) {
    nextManualPick = null;
    manualPickRef.current = null;
    setManualPick(null);
  }

  if (heldThreadId !== liveThreadId) {
    setHeldThreadId(liveThreadId);
    if (heldThreadId) {
      setModel(seed);
      setOffered(null);
      setDismissed(false);
    }
  }

  // DALTON-HOOK(ui-model-pick): track the model carried by the first send so a
  // later pick made while the thread is still being created is saved once the
  // first turn's reread finishes.
  const firstSendModelRef = useRef<string | null>(initialModel ?? model);
  if (!liveThreadId && !isLoading) {
    firstSendModelRef.current = initialModel ?? model;
  }

  const saveModel = useThreadFieldSave({
    threadId: liveThreadId,
    field: 'llm_model',
    failedMessage: t('chat.threadModel.pickFailed'),
    // The row moving back carries the composer with it. With no row cached
    // there is nothing to move, so the held pick is put back directly, unless
    // the model has moved on since for another reason.
    onFailed: ({ threadId: failedThreadId, value }) => {
      const restoredRow = queryClient.getQueryData<Thread>(queryKeys.threads.detail(failedThreadId));
      const restoredModel = !restoredRow && !initialModel
        ? null
        : resolveComposerModel(
          restoredRow?.llm_model || initialModel || null,
          startsOn,
          catalogModelNames,
        ).seed;
      if (pendingRef.current === value) {
        pendingRef.current = null;
        createdHereRef.current = false;
      }
      setPending((current) => (current === value ? null : current));
      // DALTON-HOOK(antigravity-series): clear matching manualPick object on save failure
      setManualPick((current) => (current?.model === value ? null : current));
      setModel((current) => (current === value ? restoredModel : current));
      setOffered(null);
    },
    // A switch the model the thread was on writes something for first,
    // which the next send waits for: a summary only its provider could read,
    // written out for the new model, or a brief of the whole thread for a
    // Claude Code model, which has seen none of it.
    onSaved: (saved) => {
      if (
        saved.llm_model
        && queryClient.isMutating({ mutationKey: threadFieldMutationKey(saved.thread_id || liveThreadId || '', 'llm_model') }) <= 1
      ) {
        // DALTON-HOOK(ui-model-pick): the field save only rewrites a cached
        // row; with none cached the seed would fall back to initialModel the
        // moment the pick is confirmed, so the saved row becomes the cache.
        const savedKey = queryKeys.threads.detail(saved.thread_id || liveThreadId || '');
        if (!queryClient.getQueryData<Thread>(savedKey)) {
          queryClient.setQueryData<Thread>(savedKey, saved);
        }
        if (pendingRef.current === saved.llm_model) {
          pendingRef.current = null;
          createdHereRef.current = false;
        }
        setPending((current) => (current === saved.llm_model ? null : current));
      }
      // What the next call sends is the new model's.
      if (liveThreadId) void queryClient.invalidateQueries({ queryKey: queryKeys.threads.context(liveThreadId) });
      const to = saved.llm_model || startsOn;
      const key = saved.brief_handoff ? 'chat.threadModel.brief' : saved.summary_handoff ? 'chat.threadModel.handoff' : null;
      if (!key || !to) return;
      const toLabel = formatAntigravityVariantLabel(to, {
        seriesCatalog,
        rawMetadata,
        customModels,
      }) ?? modelLabel(to, metadata);
      toast({ description: t(key, { model: toLabel }) });
    },
  });

  /** Resolves false only when this pick's own save failed. */
  const pickModel = useCallback((picked: string): Promise<boolean> => {
    const previous = model;
    const seq = ++pickSeqRef.current;
    const seriesOpts = { seriesCatalog, rawMetadata, customModels };
    const isAntigravityPick = Boolean(inspectSavedAntigravityModel(picked, seriesOpts));
    const sameSeriesPick = sameAntigravitySeries(previous, picked, seriesOpts);
    // DALTON-HOOK(ui-model-pick): record the pick as pending until confirmed (skip for shared/new threads).
    // DALTON-HOOK(antigravity-series): keep pending for unsaved new threads; defer PATCH on existing threads while shared status is unresolved
    if (!isSharedOrUnresolved || !liveThreadId) {
      pendingRef.current = picked;
      setPending(picked);
      deferredSharedPickRef.current = null;
      setDeferredSharedPick(null);
    } else if (isSharedUnresolved) {
      pendingRef.current = null;
      setPending(null);
      setDeferredSharedPick((current) => {
        const next = {
          threadId: liveThreadId,
          picked,
          before: current && current.threadId === liveThreadId ? current.before : previous,
          seq,
        };
        deferredSharedPickRef.current = next;
        return next;
      });
    } else {
      pendingRef.current = null;
      setPending(null);
      deferredSharedPickRef.current = null;
      setDeferredSharedPick(null);
    }
    // DALTON-HOOK(agent-switch-model): mark as manually picked for the upcoming send
    const nextPick = { model: picked, seq };
    manualPickRef.current = nextPick;
    setManualPick(nextPick);
    setModel(picked);
    // DALTON-HOOK(antigravity-series): switching effort within the same series does not re-open the default banner
    const dismissedForThread = !!liveThreadId
      && userSessionStorage.getItem(OFFER_DISMISSED_PREFIX + liveThreadId) !== null;
    if (!dismissed && !dismissedForThread && !sameSeriesPick) {
      setOffered(picked);
    } else if (sameSeriesPick && offered && sameAntigravitySeries(offered, picked, seriesOpts)) {
      setOffered(picked);
    }
    if (!liveThreadId || isSharedOrUnresolved) return Promise.resolve(true);
    createdHereRef.current = false;
    const shouldClearEffort = isAntigravityPick && thread?.reasoning_effort != null;
    return saveModel({
      threadId: liveThreadId,
      value: picked,
      before: previous,
      ...(shouldClearEffort ? { extraUpdates: { reasoning_effort: null } } : {}),
    });
  }, [
    model,
    setModel,
    liveThreadId,
    isSharedUnresolved,
    isSharedOrUnresolved,
    dismissed,
    offered,
    saveModel,
    seriesCatalog,
    rawMetadata,
    customModels,
    thread?.reasoning_effort,
  ]);

  // DALTON-HOOK(antigravity-series): once shared workspace status settles with data, flush a deferred pick via PATCH (personal) or keep send-with-message (shared)
  useEffect(() => {
    if (!deferredSharedPick || isSharedUnresolved) return;
    if (deferredSharedPick.threadId !== liveThreadId || !liveThreadId) {
      setDeferredSharedPick(null);
      return;
    }
    if (isSharedWorkspace) {
      setDeferredSharedPick(null);
      setModel(deferredSharedPick.picked);
      return;
    }
    const { threadId: targetThreadId, picked, before, seq } = deferredSharedPick;
    const seriesOpts = { seriesCatalog, rawMetadata, customModels };
    const isAntigravityPick = Boolean(inspectSavedAntigravityModel(picked, seriesOpts));
    const shouldClearEffort = isAntigravityPick && thread?.reasoning_effort != null;
    deferredSharedPickRef.current = null;
    setDeferredSharedPick(null);
    pendingRef.current = picked;
    setPending(picked);
    const nextPick = { model: picked, seq };
    manualPickRef.current = nextPick;
    setManualPick(nextPick);
    setModel(picked);
    createdHereRef.current = false;
    // DALTON-HOOK(antigravity-series): flush confirmed-personal deferred pick with optional reasoning_effort clear
    void saveModel({
      threadId: targetThreadId,
      value: picked,
      before,
      ...(shouldClearEffort ? { extraUpdates: { reasoning_effort: null } } : {}),
    });
  }, [
    deferredSharedPick,
    isSharedUnresolved,
    isSharedWorkspace,
    liveThreadId,
    seriesCatalog,
    rawMetadata,
    customModels,
    thread?.reasoning_effort,
    saveModel,
    setModel,
  ]);

  // A send stores the model it named on the row, so the cached detail is
  // behind once a turn ends. That includes a turn refused as `model_removed`:
  // the server cleared the dead model from the row, and the fresh copy is
  // what moves the composer to the default and says why. While any field on
  // the thread is saving, a read now would land over that save, so the row is
  // only marked stale and the save's settle reads it.
  const shownThreadRef = useRef(liveThreadId);
  shownThreadRef.current = liveThreadId;
  const isLoadingRef = useRef(isLoading);
  isLoadingRef.current = isLoading;
  const startsOnRef = useRef(startsOn);
  startsOnRef.current = startsOn;
  const catalogModelNamesRef = useRef(catalogModelNames);
  catalogModelNamesRef.current = catalogModelNames;
  // DALTON-HOOK(antigravity-series): never issue turn-end repick PATCH on shared or unresolved workspaces
  const isSharedUnresolvedRef = useRef(isSharedUnresolved);
  isSharedUnresolvedRef.current = isSharedUnresolved;
  const isSharedOrUnresolvedRef = useRef(isSharedOrUnresolved);
  isSharedOrUnresolvedRef.current = isSharedOrUnresolved;

  // DALTON-HOOK(ui-model-pick): what a turn's end does with the reread row,
  // run once that reread (or, when another field was saving, the reread after
  // that save) settles. Only the reread's own result counts, never another
  // write that happens to touch the cached row meanwhile.
  // DALTON-HOOK(antigravity-series): release a consumed shared/unresolved local pick once the turn-end reread lands, following fresh.llm_model unless the user picked a newer model mid-turn
  const turnEndCheck = useCallback((
    id: string,
    invalidated: Promise<void>,
    consumedPick: { threadId: string | null; model: string; seq: number; startThread: Thread | undefined } | null,
  ) => {
    const key = queryKeys.threads.detail(id);
    const reread = queryClient.getQueryCache().find<Thread>({ queryKey: key })?.promise;
    if (!reread) return;
    void Promise.all([reread, invalidated]).then(([fresh]) => {
      if (shownThreadRef.current !== id || isLoadingRef.current) return;
      if (consumedPick && awaitingRereadPickRef.current?.seq === consumedPick.seq) {
        awaitingRereadPickRef.current = null;
      }
      const createdHere = createdHereRef.current;
      createdHereRef.current = false;
      const currentPending = pendingRef.current;
      if (!fresh) return;
      const freshModel = fresh.llm_model || null;
      if (currentPending !== null) {
        if (freshModel === currentPending) {
          pendingRef.current = null;
          setPending(null);
          if (consumedPick && manualPickRef.current?.seq === consumedPick.seq) {
            setManualPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          if (consumedPick && deferredSharedPickRef.current?.seq === consumedPick.seq) {
            setDeferredSharedPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          return;
        }
        // DALTON-HOOK(antigravity-series): preserve an unsent pre-id pick on shared/unresolved workspaces without issuing PATCH
        if (createdHere && currentPending !== firstSendModelRef.current) {
          if (!isSharedOrUnresolvedRef.current) {
            void saveModel({
              threadId: id,
              value: currentPending,
              before: freshModel ?? firstSendModelRef.current,
            });
            return;
          }
          const nextSeq = manualPickRef.current?.model === currentPending
            ? manualPickRef.current.seq
            : ++pickSeqRef.current;
          pendingRef.current = null;
          setPending(null);
          if (isSharedUnresolvedRef.current) {
            setDeferredSharedPick({
              threadId: id,
              picked: currentPending,
              before: freshModel ?? firstSendModelRef.current,
              seq: nextSeq,
            });
          }
          setManualPick({ model: currentPending, seq: nextSeq });
          setModel(currentPending);
          return;
        }
        const nextModel = resolveComposerModel(
          freshModel,
          startsOnRef.current,
          catalogModelNamesRef.current,
        ).seed;
        pendingRef.current = null;
        setPending(null);
        setManualPick(null);
        setModel(nextModel);
        return;
      }
      // DALTON-HOOK(antigravity-series): on shared/unresolved threads, clear the consumed pick once turn-end reread lands even when fresh.llm_model differs (e.g. another tab updated or turn failed before saving)
      if (consumedPick && (consumedPick.threadId === null || consumedPick.threadId === id)) {
        const consumedManual = manualPickRef.current?.seq === consumedPick.seq;
        const consumedDeferred = deferredSharedPickRef.current?.seq === consumedPick.seq;
        if (consumedManual || consumedDeferred) {
          if (consumedManual) {
            setManualPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          if (consumedDeferred) {
            setDeferredSharedPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          const nextModel = resolveComposerModel(
            freshModel,
            startsOnRef.current,
            catalogModelNamesRef.current,
          ).seed;
          setModel(nextModel);
        }
      }
    }).catch(() => {});
  }, [queryClient, saveModel, setModel]);

  const deferredCheckRef = useRef<{
    id: string;
    consumedPick: { threadId: string | null; model: string; seq: number; startThread: Thread | undefined } | null;
  } | null>(null);
  const wasLoadingRef = useRef(isLoading);
  useEffect(() => {
    const started = !wasLoadingRef.current && isLoading;
    const ended = wasLoadingRef.current && !isLoading;
    wasLoadingRef.current = isLoading;
    // DALTON-HOOK(antigravity-series): record the local pick consumed when a turn starts so unsent picks are never cleared prematurely and consumed picks always release on turn-end reread
    if (started) {
      const currentPick = manualPickRef.current;
      consumedTurnPickRef.current = currentPick
        ? { threadId: liveThreadId, model: currentPick.model, seq: currentPick.seq, startThread: threadRef.current }
        : null;
      awaitingRereadPickRef.current = null;
      return;
    }
    if (!ended) return;
    const consumedPick = consumedTurnPickRef.current;
    consumedTurnPickRef.current = null;
    if (!liveThreadId) return;
    awaitingRereadPickRef.current = consumedPick;
    const id = liveThreadId;
    const key = queryKeys.threads.detail(id);
    const saving = queryClient.isMutating({ mutationKey: threadFieldMutationKey(id) }) > 0;
    const modelSaving = queryClient.isMutating({ mutationKey: threadFieldMutationKey(id, 'llm_model') }) > 0;
    // DALTON-HOOK(agent-switch-model): clear manual pick when turn ends only if no PATCH for llm_model is in flight
    // DALTON-HOOK(antigravity-series): keep local manual pick on shared/unresolved threads or while a deferred pick awaits flush
    if (!modelSaving && !isSharedOrUnresolved && !deferredSharedPick) {
      setManualPick(null);
    }
    // DALTON-HOOK(ui-model-pick): the reread's own result decides the pick;
    // a turn ending while another field saves defers the check to that save.
    const invalidated = queryClient.invalidateQueries({
      queryKey: key,
      ...(saving ? { refetchType: 'none' as const } : {}),
    });
    // A turn can change what its next call sends ahead of the messages.
    void queryClient.invalidateQueries({ queryKey: queryKeys.threads.context(liveThreadId) });
    if (saving) {
      deferredCheckRef.current = { id, consumedPick };
      return;
    }
    // A check left over from an earlier turn's end is superseded by this one;
    // running both on the same reread would undo the repick the first saves.
    deferredCheckRef.current = null;
    turnEndCheck(id, invalidated, consumedPick);
  }, [isLoading, liveThreadId, queryClient, turnEndCheck, isSharedOrUnresolved, deferredSharedPick]);

  // A turn that ended while another field of the thread was saving runs its
  // check once that save settles, so a repick made during creation is not
  // left only in memory.
  const fieldsSaving = useIsMutating({ mutationKey: threadFieldMutationKey(liveThreadId ?? '') });
  useEffect(() => {
    const pendingCheck = deferredCheckRef.current;
    if (!pendingCheck || fieldsSaving > 0 || pendingCheck.id !== liveThreadId || isLoading) return;
    deferredCheckRef.current = null;
    // The settled save rereads the row itself; follow that read.
    turnEndCheck(pendingCheck.id, Promise.resolve(), pendingCheck.consumedPick);
  }, [fieldsSaving, liveThreadId, isLoading, turnEndCheck]);

  const dismissOffer = useCallback(() => {
    setDismissed(true);
    setOffered(null);
    if (liveThreadId) userSessionStorage.setItem(OFFER_DISMISSED_PREFIX + liveThreadId, '1');
  }, [liveThreadId]);

  // Named only once there is a default to name beside it, and never when the
  // default is the retired model too: the row would promise a move that the
  // composer cannot make.
  const retired: RetiredModel | null = retiredName && startsOn && retiredName !== startsOn
    ? { model: retiredName, fallback: startsOn }
    : null;
  const lapsedProvider = lapsedOf(model);
  const lapsed: LapsedSignIn | null = model && lapsedProvider ? { model, provider: lapsedProvider } : null;
  const offer: DefaultModelOffer | null = offered && offered === model && defaultModel && offered !== defaultModel
    ? { model: offered, defaultModel }
    : null;

  // Said while the composer shows the Default it fell back to in its place,
  // only on a thread that follows the Default: one with a model of its own,
  // even one that left the catalog (`retired` says that), follows none.
  const heldDefault: HeldDefault | null = chiefOfStaff && defaultModel && startsOn && defaultModel !== startsOn
    && model === startsOn && !own && isClaudeCodeModel(metadata?.[defaultModel])
    ? { model: defaultModel, fallback: startsOn }
    : null;

  // DALTON-HOOK(agent-switch-model): existing threads omit llm_model unless the user manually picked on this page
  // DALTON-HOOK(antigravity-series): compare manualPick.model against displayed model
  const sendModel = !liveThreadId
    ? model
    : (nextManualPick !== null && nextManualPick.model === model ? model : null);

  // DALTON-HOOK(colleague-dispatch): expose thread detail record so ChatView can inspect thread_user_id / dispatched_by
  return { model, sendModel, retired, lapsed, offer, heldDefault, pickModel, dismissOffer, thread };
}
