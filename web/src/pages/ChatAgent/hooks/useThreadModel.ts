import { useCallback, useEffect, useRef, useState } from 'react';
import { useIsMutating, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from '@/components/ui/use-toast';
import { isModelAvailable, resolveComposerModel } from '@/components/ui/chat-input.models'; // DALTON-HOOK(ui-model-pick)
import type { Thread } from '@/types/api';
import { useAllModels } from '@/hooks/useAllModels';
import { useLapsedSignIn, type SignInGap } from '@/hooks/useLapsedSignIn';
// DALTON-HOOK(antigravity-series): inspect series membership, thread write permissions & Dalton turn-pick helpers (Dalton builds only)
import { isDaltonBuild } from '@/config/brand';
import { useThreadWriteAccess } from '@/lib/dalton/useThreadWriteAccess';
import {
  buildDeferredModelConditions,
  createPendingSettledTracker,
  recordDaltonTurnPick,
  settleDeferredSharedPick,
  shouldFlushDeferredModelPick,
  shouldRepatchPreservedPick,
  type ActiveTurnModelInfo,
  type DeferredSharedPick,
  type ManualModelPick,
  type RepatchExpectedModel,
  type TrackedTurnPick,
} from '@/lib/dalton/threadModelDalton';
import { isDaltonBackgroundConflict } from '@/lib/dalton/threadFieldSaveDalton'; // DALTON-HOOK(model-picker-races)
import { isCreatedThreadTransition } from '@/lib/dalton/threadTuningDalton';
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

/** The composer's model, on a subscription whose sign-in cannot run it. */
export interface LapsedSignIn extends SignInGap {
  model: string;
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
  // DALTON-HOOK(antigravity-series): exact model, pick sequence, and admission status of the currently running turn so refused/queued sends never clear an unsent pick, plus createdThreadId to distinguish first-send creation from navigation
  activeTurnModel?: ActiveTurnModelInfo | null;
  createdThreadId?: string | null;
}

// DALTON-HOOK(antigravity-series): optional settlement callback for pickModel (immediate or deferred flush)
export interface PickModelOptions {
  onSettled?: (saved: boolean) => void;
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
  // DALTON-HOOK(antigravity-series): accept workspaceId, activeTurnModel, and createdThreadId for Dalton write-permission, turn-pick, and creation tracking
  workspaceId = null,
  activeTurnModel = undefined,
  createdThreadId = undefined,
}: UseThreadModelOptions) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { catalogModelNames, metadata, customModels, rawApiResponse, seriesCatalog } = useAllModels();
  const rawMetadata = rawApiResponse?.model_metadata ?? metadata;
  const defaultModel = useDefaultModel();
  const lapsedOf = useLapsedSignIn();
  const liveThreadId = threadId && threadId !== '__default__' ? threadId : null;

  const { data: thread, isError: threadError, dataUpdatedAt: threadUpdatedAt } = useQuery({
    ...threadDetailQuery(liveThreadId ?? ''),
    enabled: !!liveThreadId,
  });
  // DALTON-HOOK(antigravity-series): resolve thread write access (Dalton adds pending-meta & team-sharing guards; non-Dalton matches origin/dalton/main useSharedEntry)
  const writeAccess = useThreadWriteAccess({
    workspaceId,
    liveThreadId,
    thread,
    threadError,
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
  // DALTON-HOOK(antigravity-series): manage deferred onSettled lifecycle for pre-id picks via Dalton tracker
  const pendingSettledRef = useRef(createPendingSettledTracker());

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
  const [manualPick, setManualPick] = useState<ManualModelPick | null>(null);
  // DALTON-HOOK(antigravity-series): retain a pick made while shared workspace membership is loading, including settlement callback for effort rollback/confirm
  const [deferredSharedPick, setDeferredSharedPick] = useState<DeferredSharedPick | null>(null);
  const consumedTurnPickRef = useRef<TrackedTurnPick | null>(null);
  const awaitingRereadPickRef = useRef<TrackedTurnPick | null>(null);
  // DALTON-HOOK(antigravity-series): remember last confirmed saved row for unread-row rollback, protect an unsent newer pick across a queued earlier-model turn, and snapshot thread detail at turn start for deferred admission confirmation
  // DALTON-HOOK(model-picker-races): track the old turn's expected server model so background repatch only fires when the row still holds it
  const lastConfirmedRowRef = useRef<Thread | null>(null);
  const preservedUnsentSeqRef = useRef<number | null>(null);
  const repatchExpectedRef = useRef<RepatchExpectedModel>({ known: false, model: null });
  const turnStartSnapshotRef = useRef<{ thread: Thread | undefined; updatedAt: number }>({
    thread: undefined,
    updatedAt: 0,
  });
  let nextPending = pending;
  let nextManualPick = manualPick;
  let nextDeferredSharedPick = deferredSharedPick;

  const isMovingThread = heldThreadId !== liveThreadId && (
    Boolean(heldThreadId)
    || (isDaltonBuild && liveThreadId !== null && !isCreatedThreadTransition(heldThreadId, liveThreadId, createdThreadId))
  );
  if (heldThreadId !== liveThreadId) {
    if (isMovingThread) {
      nextPending = null;
      createdHereRef.current = false;
      setPending(null);
      nextManualPick = null;
      setManualPick(null);
      // DALTON-HOOK(antigravity-series): drop & settle deferred shared-loading pick and turn-tracking refs when switching threads
      settleDeferredSharedPick(nextDeferredSharedPick, false);
      nextDeferredSharedPick = null;
      setDeferredSharedPick(null);
      pendingSettledRef.current.revoke();
      consumedTurnPickRef.current = null;
      awaitingRereadPickRef.current = null;
      lastConfirmedRowRef.current = null;
      preservedUnsentSeqRef.current = null;
      repatchExpectedRef.current = { known: false, model: null }; // DALTON-HOOK(model-picker-races)
    } else if (liveThreadId) {
      createdHereRef.current = true;
    }
  }

  // DALTON-HOOK(antigravity-series): clear pending/deferred/shared picks only when the pick itself is unavailable (not when an older saved row model is retired)
  if (nextPending !== null && !isModelAvailable(nextPending, catalogModelNames)) {
    nextPending = null;
    createdHereRef.current = false;
    pendingSettledRef.current.revoke();
    setPending(null);
  }

  if (
    nextDeferredSharedPick !== null
    && !isModelAvailable(nextDeferredSharedPick.picked, catalogModelNames)
  ) {
    settleDeferredSharedPick(nextDeferredSharedPick, false);
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

  // DALTON-HOOK(antigravity-series): clear a consumed shared/unresolved local pick once a fresh detail confirms it (including structural-sharing reads via dataUpdatedAt), or after a failed first reread once any subsequent detail read succeeds
  const awaitingPick = awaitingRereadPickRef.current;
  if (
    awaitingPick !== null
    && liveThreadId
    && thread
    && (
      (
        (thread !== awaitingPick.startThread || (isDaltonBuild && threadUpdatedAt > awaitingPick.startUpdatedAt))
        && thread.llm_model === awaitingPick.model
      )
      || (
        isDaltonBuild
        && awaitingPick.rereadFailed === true
        && (thread !== awaitingPick.failedThread || threadUpdatedAt > (awaitingPick.failedUpdatedAt ?? 0))
      )
    )
  ) {
    if (nextDeferredSharedPick?.seq === awaitingPick.seq) {
      settleDeferredSharedPick(nextDeferredSharedPick, thread.llm_model === nextDeferredSharedPick.picked);
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
  const threadUpdatedAtRef = useRef(threadUpdatedAt);
  threadUpdatedAtRef.current = threadUpdatedAt;

  // DALTON-HOOK(antigravity-series): keep deferred/shared pick (or an unsent newer pick held across a queued earlier-model turn) driving displayed and sent model
  const preserveUnsentManual = isDaltonBuild && nextManualPick !== null && preservedUnsentSeqRef.current === nextManualPick.seq;
  const effectiveSeed = nextPending
    ?? nextDeferredSharedPick?.picked
    ?? (isSharedOrUnresolved || preserveUnsentManual ? nextManualPick?.model : null)
    ?? seed;
  const [model, setModel] = useSeededModel(effectiveSeed);

  // DALTON-HOOK(antigravity-series): keep local manualPick across rerenders on shared/unresolved threads, while a deferred pick awaits flush, or while an unsent pick is protected across a queued earlier-model turn
  if (
    !isSharedOrUnresolved
    && !preserveUnsentManual
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
    if (isMovingThread) {
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
    onFailed: ({ threadId: failedThreadId, value, background }, error) => {
      // DALTON-HOOK(model-picker-races): on transient network/5xx failure of a background repatch (not 409), keep the user's unsent manualPick & sendModel intact instead of reverting to the old turn's model
      if (isDaltonBuild && background === 'repatch' && !isDaltonBackgroundConflict(background, error)) {
        if (pendingRef.current === value) {
          pendingRef.current = null;
          createdHereRef.current = false;
        }
        setPending((current) => (current === value ? null : current));
        return;
      }
      if (isDaltonBuild && manualPickRef.current?.model === value && preservedUnsentSeqRef.current === manualPickRef.current.seq) {
        preservedUnsentSeqRef.current = null;
        repatchExpectedRef.current = { known: false, model: null };
      }
      let restoredRow = queryClient.getQueryData<Thread>(queryKeys.threads.detail(failedThreadId));
      // DALTON-HOOK(antigravity-series): when no thread row is cached yet and a later chained pick fails behind an accepted one, seed the cache from the last confirmed row so effectiveSeed & tuning both restore to the pre-save state instead of an earlier initialModel/null
      if (isDaltonBuild && !restoredRow && lastConfirmedRowRef.current?.thread_id === failedThreadId) {
        restoredRow = lastConfirmedRowRef.current;
        queryClient.setQueryData<Thread>(queryKeys.threads.detail(failedThreadId), restoredRow);
      }
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
      // DALTON-HOOK(antigravity-series): record the latest server-confirmed row even while a subsequent chained pick is still queued
      const savedThreadId = saved.thread_id || liveThreadId || '';
      if (isDaltonBuild && savedThreadId) {
        lastConfirmedRowRef.current = saved.thread_id ? saved : { ...saved, thread_id: savedThreadId };
      }
      if (
        saved.llm_model
        && queryClient.isMutating({ mutationKey: threadFieldMutationKey(savedThreadId, 'llm_model') }) <= 1
      ) {
        // DALTON-HOOK(ui-model-pick): the field save only rewrites a cached
        // row; with none cached the seed would fall back to initialModel the
        // moment the pick is confirmed, so the saved row becomes the cache.
        const savedKey = queryKeys.threads.detail(savedThreadId);
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
  const pickModel = useCallback((picked: string, options?: PickModelOptions): Promise<boolean> => {
    const previous = model;
    const seq = ++pickSeqRef.current;
    if (isDaltonBuild) {
      preservedUnsentSeqRef.current = null;
      repatchExpectedRef.current = { known: false, model: null }; // DALTON-HOOK(model-picker-races)
    }
    const seriesOpts = { seriesCatalog, rawMetadata, customModels };
    const isAntigravityPick = Boolean(inspectSavedAntigravityModel(picked, seriesOpts));
    const sameSeriesPick = sameAntigravitySeries(previous, picked, seriesOpts);
    // DALTON-HOOK(ui-model-pick): record the pick as pending until confirmed (skip for shared/new threads).
    // DALTON-HOOK(antigravity-series): keep pending for unsaved new threads; defer PATCH on existing threads while shared status is unresolved, and revoke any superseded deferred pick's optimistic effort clear
    const prevDeferred = deferredSharedPickRef.current;
    // DALTON-HOOK(antigravity-series): revoke any superseded pre-id pending pick's onSettled callback
    pendingSettledRef.current.revoke();
    if (!isSharedOrUnresolved || !liveThreadId) {
      pendingRef.current = picked;
      setPending(picked);
      deferredSharedPickRef.current = null;
      setDeferredSharedPick(null);
      settleDeferredSharedPick(prevDeferred, false);
    } else if (isSharedUnresolved) {
      pendingRef.current = null;
      setPending(null);
      // DALTON-HOOK(model-picker-races): snapshot server llm_model/reasoning_effort when staging an unresolved deferred pick for CAS protection on flush
      const keepPrevDeferred = Boolean(prevDeferred && prevDeferred.threadId === liveThreadId);
      const next: DeferredSharedPick = {
        threadId: liveThreadId,
        picked,
        before: keepPrevDeferred ? prevDeferred!.before : previous,
        seq,
        expectedModel: keepPrevDeferred && prevDeferred!.hasExpectedModel
          ? prevDeferred!.expectedModel
          : (thread !== undefined ? (thread.llm_model ?? null) : null),
        hasExpectedModel: (keepPrevDeferred && Boolean(prevDeferred!.hasExpectedModel)) || thread !== undefined,
        expectedEffort: keepPrevDeferred && prevDeferred!.hasExpectedEffort
          ? prevDeferred!.expectedEffort
          : (thread !== undefined ? (thread.reasoning_effort ?? null) : null),
        hasExpectedEffort: (keepPrevDeferred && Boolean(prevDeferred!.hasExpectedEffort)) || thread !== undefined,
        onSettled: isDaltonBuild ? options?.onSettled : undefined,
      };
      deferredSharedPickRef.current = next;
      setDeferredSharedPick(next);
      settleDeferredSharedPick(prevDeferred, false);
    } else {
      pendingRef.current = null;
      setPending(null);
      deferredSharedPickRef.current = null;
      setDeferredSharedPick(null);
      settleDeferredSharedPick(prevDeferred, false);
    }
    // DALTON-HOOK(agent-switch-model): mark as manually picked for the upcoming send
    // DALTON-HOOK(model-picker-races): capture pre-pick server row model for 409-adopted unknown-model repatch matching
    const nextPick: ManualModelPick = {
      model: picked,
      seq,
      beforeRowModel: thread !== undefined ? (thread.llm_model ?? null) : previous,
    };
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
    if (!liveThreadId || isSharedOrUnresolved) {
      // DALTON-HOOK(antigravity-series): confirm local-only picks immediately; defer onSettled while a first turn is creating the thread or shared status is unresolved
      if (!liveThreadId && isLoading) pendingSettledRef.current.register(picked, options?.onSettled);
      else if (isDaltonBuild && (!liveThreadId || !isSharedUnresolved)) options?.onSettled?.(true);
      return Promise.resolve(true);
    }
    createdHereRef.current = false;
    // DALTON-HOOK(antigravity-series): clear reasoning_effort when switching to an Antigravity variant, including before thread detail has loaded or while an earlier model/effort mutation is in flight
    const isModelOrEffortMutating = isDaltonBuild && (
      queryClient.isMutating({ mutationKey: threadFieldMutationKey(liveThreadId, 'llm_model') }) > 0
      || queryClient.isMutating({ mutationKey: threadFieldMutationKey(liveThreadId, 'reasoning_effort') }) > 0
    );
    const shouldClearEffort = isDaltonBuild && isAntigravityPick && (thread === undefined || thread.reasoning_effort != null || isModelOrEffortMutating);
    return saveModel({
      threadId: liveThreadId,
      value: picked,
      before: previous,
      ...(shouldClearEffort ? { extraUpdates: { reasoning_effort: null } } : {}),
    }).then((saved) => {
      if (isDaltonBuild) options?.onSettled?.(saved);
      return saved;
    });
  }, [
    model,
    setModel,
    liveThreadId,
    isSharedUnresolved,
    isSharedOrUnresolved,
    dismissed,
    offered,
    queryClient,
    saveModel,
    seriesCatalog,
    rawMetadata,
    customModels,
    thread,
    isLoading,
  ]);

  // DALTON-HOOK(antigravity-series): once shared workspace status settles with data, flush a deferred pick via PATCH (personal) or keep send-with-message (shared)
  useEffect(() => {
    if (!deferredSharedPick || isSharedUnresolved) return;
    if (deferredSharedPick.threadId !== liveThreadId || !liveThreadId) {
      deferredSharedPickRef.current = null;
      setDeferredSharedPick(null);
      settleDeferredSharedPick(deferredSharedPick, false);
      return;
    }
    if (isSharedWorkspace) {
      setDeferredSharedPick(null);
      setModel(deferredSharedPick.picked);
      settleDeferredSharedPick(deferredSharedPick, true);
      return;
    }
    const { threadId: targetThreadId, picked, before, seq } = deferredSharedPick;
    // DALTON-HOOK(model-picker-races): abandon deferred flush if another tab already updated the thread row while permissions were unresolved
    if (!shouldFlushDeferredModelPick(deferredSharedPick, thread)) {
      deferredSharedPickRef.current = null;
      setDeferredSharedPick(null);
      if (manualPickRef.current?.seq === seq) {
        manualPickRef.current = null;
        setManualPick(null);
      }
      const latestModel = resolveComposerModel(
        thread?.llm_model || null,
        startsOn,
        catalogModelNames,
      ).seed;
      setModel(latestModel);
      settleDeferredSharedPick(deferredSharedPick, false);
      return;
    }
    const seriesOpts = { seriesCatalog, rawMetadata, customModels };
    const isAntigravityPick = Boolean(inspectSavedAntigravityModel(picked, seriesOpts));
    // DALTON-HOOK(antigravity-series): clear reasoning_effort when flushing a deferred Antigravity pick if detail is unread or holds an effort override
    const shouldClearEffort = isDaltonBuild && isAntigravityPick && (thread === undefined || thread.reasoning_effort != null);
    const conditions = buildDeferredModelConditions(deferredSharedPick, shouldClearEffort);
    deferredSharedPickRef.current = null;
    setDeferredSharedPick(null);
    pendingRef.current = picked;
    setPending(picked);
    const nextPick: ManualModelPick = { model: picked, seq, beforeRowModel: deferredSharedPick.expectedModel };
    manualPickRef.current = nextPick;
    setManualPick(nextPick);
    setModel(picked);
    createdHereRef.current = false;
    // DALTON-HOOK(antigravity-series): flush confirmed-personal deferred pick with optional reasoning_effort clear and forward settlement outcome
    // DALTON-HOOK(model-picker-races): attach expected-value CAS conditions so a stale deferred flush yields on 409 instead of overwriting another tab
    void saveModel({
      threadId: targetThreadId,
      value: picked,
      before,
      ...(shouldClearEffort ? { extraUpdates: { reasoning_effort: null } } : {}),
      ...(conditions ? { conditions } : {}),
      background: 'deferred',
    }).then((saved) => {
      settleDeferredSharedPick(deferredSharedPick, saved);
    });
  }, [
    deferredSharedPick,
    isSharedUnresolved,
    isSharedWorkspace,
    liveThreadId,
    seriesCatalog,
    rawMetadata,
    customModels,
    thread,
    startsOn,
    catalogModelNames,
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
  // DALTON-HOOK(antigravity-series): never issue turn-end repick PATCH on shared or unresolved workspaces; keep seriesOpts ref for pre-id Antigravity flush
  const isSharedUnresolvedRef = useRef(isSharedUnresolved);
  isSharedUnresolvedRef.current = isSharedUnresolved;
  const isSharedOrUnresolvedRef = useRef(isSharedOrUnresolved);
  isSharedOrUnresolvedRef.current = isSharedOrUnresolved;
  const seriesOptsRef = useRef({ seriesCatalog, rawMetadata, customModels });
  seriesOptsRef.current = { seriesCatalog, rawMetadata, customModels };
  const activeTurnModelRef = useRef(activeTurnModel);
  activeTurnModelRef.current = activeTurnModel;

  // DALTON-HOOK(ui-model-pick): what a turn's end does with the reread row,
  // run once that reread (or, when another field was saving, the reread after
  // that save) settles. Only the reread's own result counts, never another
  // write that happens to touch the cached row meanwhile.
  // DALTON-HOOK(antigravity-series): release a consumed shared/unresolved local pick once the turn-end reread lands, following fresh.llm_model unless the user picked a newer model mid-turn, and record reread failure so a subsequent successful detail read still unpins it
  const turnEndCheck = useCallback((
    id: string,
    invalidated: Promise<void>,
    consumedPick: TrackedTurnPick | null,
    repatchExpected?: RepatchExpectedModel,
  ) => {
    const markRereadFailed = () => {
      // DALTON-HOOK(antigravity-series): settle pending pre-id callback with false on missing/empty/failed detail reread
      pendingSettledRef.current.revoke();
      if (isDaltonBuild && consumedPick && awaitingRereadPickRef.current?.seq === consumedPick.seq) {
        awaitingRereadPickRef.current = {
          ...awaitingRereadPickRef.current,
          rereadFailed: true,
          failedThread: threadRef.current,
          failedUpdatedAt: threadUpdatedAtRef.current,
        };
      }
    };
    const key = queryKeys.threads.detail(id);
    const reread = queryClient.getQueryCache().find<Thread>({ queryKey: key })?.promise;
    if (!reread) {
      markRereadFailed();
      return;
    }
    void Promise.all([reread, invalidated]).then(([fresh]) => {
      if (shownThreadRef.current !== id || isLoadingRef.current) {
        // DALTON-HOOK(antigravity-series): settle pending pre-id callback with false on same-thread reload (leaving a thread already revokes on switch)
        if (shownThreadRef.current === id) pendingSettledRef.current.revoke();
        return;
      }
      if (!fresh) {
        markRereadFailed();
        return;
      }
      if (consumedPick && awaitingRereadPickRef.current?.seq === consumedPick.seq) {
        awaitingRereadPickRef.current = null;
      }
      const createdHere = createdHereRef.current;
      createdHereRef.current = false;
      const currentPending = pendingRef.current;
      const freshModel = fresh.llm_model || null;
      if (currentPending !== null) {
        if (freshModel === currentPending) {
          const settled = pendingSettledRef.current.take(currentPending);
          pendingRef.current = null;
          setPending(null);
          if (consumedPick && manualPickRef.current?.seq === consumedPick.seq) {
            setManualPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          if (consumedPick && deferredSharedPickRef.current?.seq === consumedPick.seq) {
            settleDeferredSharedPick(deferredSharedPickRef.current, true);
            setDeferredSharedPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          if (isDaltonBuild) settled?.(true);
          return;
        }
        // DALTON-HOOK(antigravity-series): preserve an unsent pre-id pick on shared/unresolved workspaces without issuing PATCH, and forward deferred PATCH settlement to onSettled
        if (createdHere && currentPending !== firstSendModelRef.current) {
          const settled = pendingSettledRef.current.take(currentPending);
          if (!isSharedOrUnresolvedRef.current) {
            const shouldClearEffort = isDaltonBuild
              && Boolean(inspectSavedAntigravityModel(currentPending, seriesOptsRef.current))
              && fresh.reasoning_effort != null;
            void saveModel({
              threadId: id,
              value: currentPending,
              before: freshModel ?? firstSendModelRef.current,
              ...(shouldClearEffort ? { extraUpdates: { reasoning_effort: null } } : {}),
            }).then((saved) => {
              if (isDaltonBuild) settled?.(saved);
            });
            return;
          }
          const nextSeq = manualPickRef.current?.model === currentPending
            ? manualPickRef.current.seq
            : ++pickSeqRef.current;
          pendingRef.current = null;
          setPending(null);
          if (isSharedUnresolvedRef.current) {
            const nextDeferred: DeferredSharedPick = {
              threadId: id,
              picked: currentPending,
              before: freshModel ?? firstSendModelRef.current,
              seq: nextSeq,
              // DALTON-HOOK(model-picker-races): record fresh row values for CAS guard when unresolved pre-id pick later flushes
              expectedModel: freshModel,
              hasExpectedModel: true,
              expectedEffort: fresh.reasoning_effort ?? null,
              hasExpectedEffort: true,
              onSettled: isDaltonBuild ? settled : undefined,
            };
            deferredSharedPickRef.current = nextDeferred;
            setDeferredSharedPick(nextDeferred);
          } else if (isDaltonBuild) {
            settled?.(true);
          }
          const nextPick = { model: currentPending, seq: nextSeq };
          manualPickRef.current = nextPick;
          setManualPick(nextPick);
          setModel(currentPending);
          return;
        }
        // DALTON-HOOK(antigravity-series): do not clear an unsent newer manualPick when an earlier queued turn finishes
        if (
          isDaltonBuild
          && manualPickRef.current !== null
          && manualPickRef.current.model === currentPending
          && (consumedPick === null || manualPickRef.current.seq !== consumedPick.seq)
        ) {
          pendingSettledRef.current.revoke();
          return;
        }
        const settled = pendingSettledRef.current.take(currentPending);
        const nextModel = resolveComposerModel(
          freshModel,
          startsOnRef.current,
          catalogModelNamesRef.current,
        ).seed;
        pendingRef.current = null;
        setPending(null);
        setManualPick(null);
        setModel(nextModel);
        if (isDaltonBuild) settled?.(false);
        return;
      }
      // DALTON-HOOK(antigravity-series): on personal threads, if an earlier queued turn overwrote the server row while a newer unsent pick was preserved, re-persist that newer pick to the thread row
      // DALTON-HOOK(model-picker-races): only repatch when freshModel still equals the old turn's model, and carry expected_llm_model CAS guard so external edits win
      if (
        isDaltonBuild
        && !isSharedOrUnresolvedRef.current
        && preservedUnsentSeqRef.current !== null
        && manualPickRef.current !== null
        && manualPickRef.current.seq === preservedUnsentSeqRef.current
        && (consumedPick === null || manualPickRef.current.seq !== consumedPick.seq)
        && freshModel !== manualPickRef.current.model
      ) {
        if (!shouldRepatchPreservedPick(freshModel, repatchExpected)) {
          preservedUnsentSeqRef.current = null;
          repatchExpectedRef.current = { known: false, model: null };
          setManualPick(null);
          const nextModel = resolveComposerModel(
            freshModel,
            startsOnRef.current,
            catalogModelNamesRef.current,
          ).seed;
          setModel(nextModel);
          return;
        }
        const unsentModel = manualPickRef.current.model;
        const shouldClearEffort = Boolean(inspectSavedAntigravityModel(unsentModel, seriesOptsRef.current))
          && fresh.reasoning_effort != null;
        pendingRef.current = unsentModel;
        setPending(unsentModel);
        setModel(unsentModel);
        void saveModel({
          threadId: id,
          value: unsentModel,
          before: freshModel,
          ...(shouldClearEffort ? { extraUpdates: { reasoning_effort: null } } : {}),
          conditions: {
            expected_llm_model: freshModel,
            ...(shouldClearEffort ? { expected_reasoning_effort: fresh.reasoning_effort ?? null } : {}),
          },
          background: 'repatch',
        });
        return;
      }
      // DALTON-HOOK(antigravity-series): on shared/unresolved threads, clear the consumed pick once turn-end reread lands even when fresh.llm_model differs (e.g. another tab updated or turn failed before saving)
      if (consumedPick && (consumedPick.threadId === null || consumedPick.threadId === id)) {
        const consumedManual = manualPickRef.current?.seq === consumedPick.seq;
        const consumedDeferred = deferredSharedPickRef.current?.seq === consumedPick.seq;
        if (consumedManual || consumedDeferred) {
          if (consumedManual) {
            if (preservedUnsentSeqRef.current === consumedPick.seq) {
              preservedUnsentSeqRef.current = null;
            }
            setManualPick((current) => (current?.seq === consumedPick.seq ? null : current));
          }
          if (consumedDeferred) {
            settleDeferredSharedPick(deferredSharedPickRef.current, freshModel === deferredSharedPickRef.current?.picked);
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
    }).catch(() => {
      markRereadFailed();
    });
  }, [queryClient, saveModel, setModel]);

  const deferredCheckRef = useRef<{
    id: string;
    consumedPick: TrackedTurnPick | null;
    repatchExpected?: RepatchExpectedModel;
  } | null>(null);
  const wasLoadingRef = useRef(isLoading);
  // DALTON-HOOK(antigravity-series): resolve consumed pick & unsent protection via Dalton helper
  // DALTON-HOOK(model-picker-races): also capture expected old-turn model into repatchExpectedRef
  const recordAdmittedTurnPick = useCallback(
    (turnModel: ActiveTurnModelInfo | null, snapshot: { thread: Thread | undefined; updatedAt: number }): TrackedTurnPick | null =>
      recordDaltonTurnPick(turnModel, snapshot, liveThreadId, {
        manualPickRef,
        isSharedOrUnresolvedRef,
        pendingRef,
        deferredSharedPickRef,
        preservedUnsentSeqRef,
        repatchExpectedRef,
      }),
    [liveThreadId],
  );

  // DALTON-HOOK(antigravity-series): mark the turn's model pick as consumed once the server confirms run admission while loading
  useEffect(() => {
    if (!isDaltonBuild || !isLoading || !activeTurnModel || activeTurnModel.admitted !== true) return;
    if (consumedTurnPickRef.current !== null) return;
    consumedTurnPickRef.current = recordAdmittedTurnPick(activeTurnModel, turnStartSnapshotRef.current);
  }, [isLoading, activeTurnModel, recordAdmittedTurnPick]);

  useEffect(() => {
    const started = !wasLoadingRef.current && isLoading;
    const ended = wasLoadingRef.current && !isLoading;
    wasLoadingRef.current = isLoading;
    // DALTON-HOOK(antigravity-series): record only the pick actually admitted with the started turn so refused sends never clear a pick and queued sends never clear a newer unsent pick
    if (started) {
      const currentPick = manualPickRef.current;
      const turnModel = activeTurnModelRef.current;
      const snapshot = { thread: threadRef.current, updatedAt: threadUpdatedAtRef.current };
      turnStartSnapshotRef.current = snapshot;
      if (isDaltonBuild && turnModel !== undefined) {
        consumedTurnPickRef.current = recordAdmittedTurnPick(turnModel, snapshot);
      } else {
        consumedTurnPickRef.current = currentPick
          ? {
              threadId: liveThreadId,
              model: currentPick.model,
              seq: currentPick.seq,
              startThread: snapshot.thread,
              startUpdatedAt: snapshot.updatedAt,
            }
          : null;
      }
      awaitingRereadPickRef.current = null;
      return;
    }
    if (!ended) return;
    const consumedPick = consumedTurnPickRef.current
      ?? (isDaltonBuild && activeTurnModelRef.current?.admitted === true
        ? recordAdmittedTurnPick(activeTurnModelRef.current, turnStartSnapshotRef.current)
        : null);
    consumedTurnPickRef.current = null;
    // DALTON-HOOK(model-picker-races): snapshot expected old-turn model for this turn-end check
    const repatchExpected = repatchExpectedRef.current;
    if (!liveThreadId) {
      // DALTON-HOOK(antigravity-series): settle pre-id pick callback if first turn ended before creating a thread
      pendingSettledRef.current.confirmIfMatching(pendingRef.current);
      return;
    }
    awaitingRereadPickRef.current = consumedPick;
    const id = liveThreadId;
    const key = queryKeys.threads.detail(id);
    const saving = queryClient.isMutating({ mutationKey: threadFieldMutationKey(id) }) > 0;
    const modelSaving = queryClient.isMutating({ mutationKey: threadFieldMutationKey(id, 'llm_model') }) > 0;
    // DALTON-HOOK(agent-switch-model): clear manual pick when turn ends only if no PATCH for llm_model is in flight
    // DALTON-HOOK(antigravity-series): keep local manual pick on shared/unresolved threads, while a deferred pick awaits flush, or when it was not admitted & consumed by this turn
    if (!modelSaving && !isSharedOrUnresolved && !deferredSharedPick) {
      if (isDaltonBuild) {
        if (consumedPick) {
          if (preservedUnsentSeqRef.current === consumedPick.seq) {
            preservedUnsentSeqRef.current = null;
          }
          setManualPick((current) => (current?.seq === consumedPick.seq ? null : current));
        }
      } else {
        setManualPick(null);
      }
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
      deferredCheckRef.current = { id, consumedPick, repatchExpected };
      return;
    }
    // A check left over from an earlier turn's end is superseded by this one;
    // running both on the same reread would undo the repick the first saves.
    deferredCheckRef.current = null;
    turnEndCheck(id, invalidated, consumedPick, repatchExpected);
  }, [isLoading, liveThreadId, queryClient, turnEndCheck, isSharedOrUnresolved, deferredSharedPick, recordAdmittedTurnPick]);

  // A turn that ended while another field of the thread was saving runs its
  // check once that save settles, so a repick made during creation is not
  // left only in memory.
  const fieldsSaving = useIsMutating({ mutationKey: threadFieldMutationKey(liveThreadId ?? '') });
  useEffect(() => {
    const pendingCheck = deferredCheckRef.current;
    if (!pendingCheck || fieldsSaving > 0 || pendingCheck.id !== liveThreadId || isLoading) return;
    deferredCheckRef.current = null;
    // The settled save rereads the row itself; follow that read.
    turnEndCheck(pendingCheck.id, Promise.resolve(), pendingCheck.consumedPick, pendingCheck.repatchExpected);
  }, [fieldsSaving, liveThreadId, isLoading, turnEndCheck]);

  const dismissOffer = useCallback(() => {
    setDismissed(true);
    setOffered(null);
    if (liveThreadId) userSessionStorage.setItem(OFFER_DISMISSED_PREFIX + liveThreadId, '1');
  }, [liveThreadId]);
  // Running out answers nothing, so the next pick on the thread offers again.
  const expireOffer = useCallback(() => setOffered(null), []);

  // Named only once there is a default to name beside it, and never when the
  // default is the retired model too: the row would promise a move that the
  // composer cannot make.
  const retired: RetiredModel | null = retiredName && startsOn && retiredName !== startsOn
    ? { model: retiredName, fallback: startsOn }
    : null;
  const gap = lapsedOf(model);
  const lapsed: LapsedSignIn | null = model && gap ? { model, ...gap } : null;
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
  // DALTON-HOOK(antigravity-series): compare manualPick.model against displayed model and expose sendModelSeq for queued-send matching
  const sendModel = !liveThreadId
    ? model
    : (nextManualPick !== null && nextManualPick.model === model ? model : null);
  const sendModelSeq = nextManualPick !== null && nextManualPick.model === model ? nextManualPick.seq : null;

  // DALTON-HOOK(colleague-dispatch): expose thread detail record so ChatView can inspect thread_user_id / dispatched_by
  return { model, sendModel, sendModelSeq, retired, lapsed, offer, heldDefault, pickModel, dismissOffer, expireOffer, thread };
}
