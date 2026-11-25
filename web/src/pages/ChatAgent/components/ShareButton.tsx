import { useState } from 'react';
import { Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/useIsMobile';
// DALTON-HOOK(external-share-guard): hide share trigger for external members unless an active share exists to close, and gate unknown identity fail-closed
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { useSharedLinks } from '@/hooks/useShareLink';
import { useThreadShareStatus } from '../hooks/useThreadShareStatus';
import { ThreadSharePanel } from './ThreadSharePanel';

interface ShareButtonProps {
  threadId: string;
  initialIsShared?: boolean;
  /** The workspace whose shared files the popover lists; null hides the list. */
  workspaceId?: string | null;
}

/**
 * ShareButton -- Toggle public sharing for a thread, with permission controls.
 * Shows a popover with share toggle, URL copy, and permission checkboxes.
 */
export default function ShareButton({ threadId, initialIsShared = false, workspaceId = null }: ShareButtonProps) {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const [open, setOpen] = useState(false);
  // The thread's cached status, shared with every share panel; the thread
  // load's answer stands in until it arrives.
  const { data } = useThreadShareStatus(threadId);
  const isShared = data ? data.is_shared === true : initialIsShared;
  // DALTON-HOOK(external-share-guard): external members cannot start new public shares, only close existing ones; unknown identity stays disabled unless stopping an active share
  const identity = useCallerIdentity();
  const isExternal = identity.isExternal;
  const isIdentityUnknown = identity.status !== 'ready';
  const { data: sharedLinks } = useSharedLinks(
    isExternal || isIdentityUnknown ? workspaceId : null,
  );
  const hasActiveShare = isShared || Boolean(sharedLinks?.length);

  if (isExternal && !hasActiveShare) {
    return null;
  }

  const triggerDisabled = isIdentityUnknown && !hasActiveShare;

  // Radix owns dismissal: Escape and an outside press close it, and a keyboard
  // close hands focus back to the trigger.
  return (
    <Popover open={open} onOpenChange={(next) => { if (!triggerDisabled || !next) setOpen(next); }}>
      <PopoverTrigger asChild>
        {/* Sized as the files toggle beside it is: the panel's own button
            size on desktop, a touch target on mobile. */}
        <button
          type="button"
          disabled={triggerDisabled}
          // DALTON-HOOK(mobile-web): ensure mobile share icon button clears 44px touch target
          className={cn(
            'transition-colors disabled:cursor-not-allowed',
            isMobile ? 'p-2 tap-target rounded-md' : 'p-1.5 rounded-[4px]',
          )}
          style={{
            color: isShared ? 'var(--color-accent-primary)' : 'var(--color-text-primary)',
            backgroundColor: open ? 'var(--color-border-muted)' : undefined,
            // DALTON-HOOK(external-share-guard): dim trigger when identity is unconfirmed and no active share exists
            opacity: triggerDisabled ? 0.5 : 1,
          }}
          title={triggerDisabled ? t('share.confirmingIdentity') : t('share.shareConversation')}
          onMouseEnter={(e) => { if (!open && !triggerDisabled) e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'; }}
          onMouseLeave={(e) => { if (!open) e.currentTarget.style.backgroundColor = ''; }}
        >
          <Share2 className={isMobile ? 'h-5 w-5' : 'h-4 w-4'} />
        </button>
      </PopoverTrigger>

      <PopoverContent
        align="end"
        sideOffset={8}
        className="w-80 p-0 rounded-lg shadow-lg"
        style={{
          backgroundColor: 'var(--color-bg-elevated, var(--color-bg-card))',
          borderColor: 'var(--color-border-muted)',
        }}
      >
        <ThreadSharePanel
          key={threadId}
          threadId={threadId}
          workspaceId={workspaceId}
          // DALTON-HOOK(external-share-guard): pass popover open state for identity revalidation
          open={open}
        />
      </PopoverContent>
    </Popover>
  );
}
