/**
 * Tiny pub/sub for widget context attachments. Decouples the dashboard
 * widgets that produce snapshots from the chat inputs that render them.
 *
 * Why a hand-rolled bus over a React context: chat inputs and overflow pill
 * may live in different React trees (modal, portal, hero card vs in-grid
 * widget) and need to subscribe to the *same* state without lifting an
 * ancestor. A module-scoped event emitter is the smallest unit that works
 * for cross-tree state without dragging in zustand.
 */

import type { WidgetContextSnapshot } from '@/pages/Dashboard/widgets/framework/contextSnapshot';

export type ContextBusEvent =
  | { type: 'attach'; snapshot: WidgetContextSnapshot }
  // DALTON-HOOK(chat-attachment-count): `only` rolls back one rejected attach — a subscriber
  // drops the entry only when it holds that very snapshot object, not an earlier copy.
  | { type: 'detach'; widgetId: string; only?: WidgetContextSnapshot }
  | { type: 'clear' };

export type ContextBusHandler = (event: ContextBusEvent) => unknown;

const handlers = new Set<ContextBusHandler>();

function publish(event: ContextBusEvent): boolean {
  // Snapshot the set so a handler that unsubscribes during dispatch doesn't
  // mutate the iterator. Errors in one handler must not silence the others —
  // log and continue.
  let accepted = true;
  for (const h of [...handlers]) {
    try {
      // DALTON-HOOK(chat-attachment-count): allow a mounted ChatInput to reject an over-limit image snapshot
      if (h(event) === false) accepted = false;
    } catch (err) {
      console.error('[contextBus] subscriber threw', err);
    }
  }
  return accepted;
}

export const ContextBus = {
  subscribe(handler: ContextBusHandler): () => void {
    handlers.add(handler);
    return () => {
      handlers.delete(handler);
    };
  },
  attach(snapshot: WidgetContextSnapshot): boolean {
    // DALTON-HOOK(chat-attachment-count): when a ChatInput rejects it, roll back only this
    // snapshot object (an earlier copy of the same widget stays) and tell the caller.
    const accepted = publish({ type: 'attach', snapshot });
    if (!accepted) publish({ type: 'detach', widgetId: snapshot.widget_id, only: snapshot });
    return accepted;
  },
  detach(widgetId: string): void {
    publish({ type: 'detach', widgetId });
  },
  clear(): void {
    publish({ type: 'clear' });
  },
  /** Test-only: drop every subscriber. Never call from product code. */
  __resetForTests(): void {
    handlers.clear();
  },
};

/**
 * Registry of mounted chat input root elements so the overflow pill can
 * decide whether *any* chat input is visible via IntersectionObserver.
 * Module-scoped because the pill renders at the app shell while inputs
 * mount deeper in the tree.
 */
const chatInputElements = new Set<HTMLElement>();

export const ChatInputRegistry = {
  register(el: HTMLElement): () => void {
    chatInputElements.add(el);
    notifyChatInputListeners();
    return () => {
      chatInputElements.delete(el);
      notifyChatInputListeners();
    };
  },
  list(): HTMLElement[] {
    return [...chatInputElements];
  },
  size(): number {
    return chatInputElements.size;
  },
  subscribe(listener: () => void): () => void {
    chatInputListeners.add(listener);
    return () => {
      chatInputListeners.delete(listener);
    };
  },
  /** Test-only. */
  __resetForTests(): void {
    chatInputElements.clear();
    chatInputListeners.clear();
  },
};

const chatInputListeners = new Set<() => void>();

function notifyChatInputListeners(): void {
  for (const l of [...chatInputListeners]) {
    try {
      l();
    } catch (err) {
      console.error('[chatInputRegistry] listener threw', err);
    }
  }
}
