import { useCallback, useEffect, useRef, useState } from 'react';
import { useIsMutating, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { toast } from '@/components/ui/use-toast';
import { isModelAvailable, resolveComposerModel } from '@/components/ui/chat-input.models'; // DALTON-HOOK(ui-model-pick)
import type { SubscriptionProvider } from '@/api/oauth';
import type { Thread } from '@/types/api';
import { useAllModels } from '@/hooks/useAllModels';
import { useLapsedSignIn } from '@/hooks/useLapsedSignIn';
import { useDefaultModel } from '@/hooks/useSlotDefaultModels';
import { useSeededModel } from '@/hooks/useSeededModel';
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
}

/**
 * The model a chat view's thread runs on: what its composer shows, what the
 * next send names, and the one place a pick on it is saved.
 *
 * The thread row is the source. The model starts on the row's model while it
 * is reachable (else the account default) and follows the row when it moves; a
 * pick holds in between and is saved on the row (`useThreadFieldSave`). A
 * thread not created yet has no row to write, so its pick is held here and
 * goes out with the first send, which stores it.
 *
 * A pick also raises the offer to make that model the account default, shown
 * while the composer still shows the pick and it differs from the default.
 */
export function useThreadModel({
  threadId,
  isLoading,
  initialModel = null,
}: UseThreadModelOptions) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { catalogModelNames, metadata } = useAllModels();
  const defaultModel = useDefaultModel();
  const lapsedOf = useLapsedSignIn();
  const liveThreadId = threadId && threadId !== '__default__' ? threadId : null;

  const { data: thread } = useQuery({
    ...threadDetailQuery(liveThreadId ?? ''),
    enabled: !!liveThreadId,
  });
  // A thread whose row has not been read (still loading, or the read failed)
  // seeds no model rather than the default: the composer names what it shows,
  // and the send would store the default over the model the thread holds.
  // With no model named, the server runs the thread's own.
  const unread = !!liveThreadId && !thread && !initialModel;
  const { seed, retired: retiredName } = unread
    ? { seed: null, retired: null }
    : resolveComposerModel(
      thread?.llm_model || initialModel || null,
      defaultModel,
      catalogModelNames,
    );

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
  let nextPending = pending;

  if (heldThreadId !== liveThreadId) {
    if (heldThreadId) {
      nextPending = null;
      createdHereRef.current = false;
      setPending(null);
    } else if (liveThreadId) {
      createdHereRef.current = true;
    }
  }

  if (nextPending !== null && (Boolean(retiredName) || !isModelAvailable(nextPending, catalogModelNames))) {
    nextPending = null;
    createdHereRef.current = false;
    setPending(null);
  }

  pendingRef.current = nextPending;

  const [model, setModel] = useSeededModel(nextPending ?? seed);

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
          defaultModel,
          catalogModelNames,
        ).seed;
      if (pendingRef.current === value) {
        pendingRef.current = null;
        createdHereRef.current = false;
      }
      setPending((current) => (current === value ? null : current));
      setModel((current) => (current === value ? restoredModel : current));
      setOffered(null);
    },
    // A switch away from a model whose provider compacted the thread into a
    // summary only it can read: that model writes it out for the new one
    // first, which the next send waits for.
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
      const to = saved.llm_model || defaultModel;
      if (!saved.summary_handoff || !to) return;
      toast({ description: t('chat.threadModel.handoff', { model: modelLabel(to, metadata) }) });
    },
  });

  /** Resolves false only when this pick's own save failed. */
  const pickModel = useCallback((picked: string): Promise<boolean> => {
    const previous = model;
    // DALTON-HOOK(ui-model-pick): record the pick as pending until confirmed.
    pendingRef.current = picked;
    setPending(picked);
    setModel(picked);
    const dismissedForThread = !!liveThreadId
      && userSessionStorage.getItem(OFFER_DISMISSED_PREFIX + liveThreadId) !== null;
    if (!dismissed && !dismissedForThread) setOffered(picked);
    if (!liveThreadId) return Promise.resolve(true);
    createdHereRef.current = false;
    return saveModel({ threadId: liveThreadId, value: picked, before: previous });
  }, [model, setModel, liveThreadId, dismissed, saveModel]);

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
  const defaultModelRef = useRef(defaultModel);
  defaultModelRef.current = defaultModel;
  const catalogModelNamesRef = useRef(catalogModelNames);
  catalogModelNamesRef.current = catalogModelNames;

  // DALTON-HOOK(ui-model-pick): what a turn's end does with the reread row,
  // run once that reread (or, when another field was saving, the reread after
  // that save) settles. Only the reread's own result counts, never another
  // write that happens to touch the cached row meanwhile.
  const turnEndCheck = useCallback((id: string, invalidated: Promise<void>) => {
    const key = queryKeys.threads.detail(id);
    const reread = queryClient.getQueryCache().find<Thread>({ queryKey: key })?.promise;
    if (!reread) return;
    void Promise.all([reread, invalidated]).then(([fresh]) => {
      if (shownThreadRef.current !== id || isLoadingRef.current) return;
      const createdHere = createdHereRef.current;
      createdHereRef.current = false;
      const currentPending = pendingRef.current;
      if (currentPending === null || !fresh) return;
      const freshModel = fresh.llm_model || null;
      if (freshModel === currentPending) {
        pendingRef.current = null;
        setPending(null);
        return;
      }
      if (createdHere && currentPending !== firstSendModelRef.current) {
        void saveModel({
          threadId: id,
          value: currentPending,
          before: freshModel ?? firstSendModelRef.current,
        });
        return;
      }
      const nextModel = resolveComposerModel(
        freshModel,
        defaultModelRef.current,
        catalogModelNamesRef.current,
      ).seed;
      pendingRef.current = null;
      setPending(null);
      setModel(nextModel);
    }).catch(() => {});
  }, [queryClient, saveModel, setModel]);

  const deferredCheckRef = useRef<string | null>(null);
  const wasLoadingRef = useRef(isLoading);
  useEffect(() => {
    const ended = wasLoadingRef.current && !isLoading;
    wasLoadingRef.current = isLoading;
    if (!ended || !liveThreadId) return;
    // DALTON-HOOK(ui-model-pick): the reread's own result decides the pick;
    // a turn ending while another field saves defers the check to that save.
    const id = liveThreadId;
    const key = queryKeys.threads.detail(id);
    const saving = queryClient.isMutating({ mutationKey: threadFieldMutationKey(id) }) > 0;
    const invalidated = queryClient.invalidateQueries({
      queryKey: key,
      ...(saving ? { refetchType: 'none' as const } : {}),
    });
    if (saving) {
      deferredCheckRef.current = id;
      return;
    }
    // A check left over from an earlier turn's end is superseded by this one;
    // running both on the same reread would undo the repick the first saves.
    deferredCheckRef.current = null;
    turnEndCheck(id, invalidated);
  }, [isLoading, liveThreadId, queryClient, turnEndCheck]);

  // A turn that ended while another field of the thread was saving runs its
  // check once that save settles, so a repick made during creation is not
  // left only in memory.
  const fieldsSaving = useIsMutating({ mutationKey: threadFieldMutationKey(liveThreadId ?? '') });
  useEffect(() => {
    const id = deferredCheckRef.current;
    if (!id || fieldsSaving > 0 || id !== liveThreadId || isLoading) return;
    deferredCheckRef.current = null;
    // The settled save rereads the row itself; follow that read.
    turnEndCheck(id, Promise.resolve());
  }, [fieldsSaving, liveThreadId, isLoading, turnEndCheck]);

  const dismissOffer = useCallback(() => {
    setDismissed(true);
    setOffered(null);
    if (liveThreadId) userSessionStorage.setItem(OFFER_DISMISSED_PREFIX + liveThreadId, '1');
  }, [liveThreadId]);

  // Named only once there is a default to name beside it, and never when the
  // default is the retired model too: the row would promise a move that the
  // composer cannot make.
  const retired: RetiredModel | null = retiredName && defaultModel && retiredName !== defaultModel
    ? { model: retiredName, fallback: defaultModel }
    : null;
  const lapsedProvider = lapsedOf(model);
  const lapsed: LapsedSignIn | null = model && lapsedProvider ? { model, provider: lapsedProvider } : null;
  const offer: DefaultModelOffer | null = offered && offered === model && defaultModel && offered !== defaultModel
    ? { model: offered, defaultModel }
    : null;

  // DALTON-HOOK(colleague-dispatch): expose thread detail record so ChatView can inspect thread_user_id / dispatched_by
  return { model, retired, lapsed, offer, pickModel, dismissOffer, thread };
}
