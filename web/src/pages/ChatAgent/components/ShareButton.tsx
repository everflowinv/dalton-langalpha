import { useState } from 'react';
import { Share2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useQueryClient } from '@tanstack/react-query';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/utils';
import { useIsMobile } from '@/hooks/useIsMobile';
// DALTON-HOOK(external-share-guard): hide share trigger for external members unless an active share exists to close
import { useSharedLinks } from '@/hooks/useShareLink';
import { queryKeys } from '@/lib/queryKeys';
import { useThreadShareStatus } from '../hooks/useThreadShareStatus';
import { ThreadSharePanel } from './ThreadSharePanel';

interface ShareButtonProps {
  threadId: string;
  initialIsShared?: boolean;
  /** The workspace whose shared files the popover lists; null hides the list. */
  workspaceId?: string | null;
  // DALTON-HOOK(external-share-guard): caller tier from parent view
  callerTier?: string | null;
}

/**
 * ShareButton -- Toggle public sharing for a thread, with permission controls.
 * Shows a popover with share toggle, URL copy, and permission checkboxes.
 */
export default function ShareButton({ threadId, initialIsShared = false, workspaceId = null, callerTier: callerTierProp }: ShareButtonProps) {
  const { t } = useTranslation();
  const isMobile = useIsMobile();
  const [open, setOpen] = useState(false);
  // The thread's cached status, shared with every share panel; the thread
  // load's answer stands in until it arrives.
  const { data } = useThreadShareStatus(threadId);
  const isShared = data ? data.is_shared === true : initialIsShared;
  // DALTON-HOOK(external-share-guard): external members cannot start new public shares, only close existing ones
  const queryClient = useQueryClient();
  const callerTier =
    callerTierProp ??
    queryClient.getQueryData<{ caller_tier?: string | null }>(queryKeys.workspaces.team())?.caller_tier ??
    queryClient.getQueryData<{ caller_tier?: string | null }>(queryKeys.dalton.userSharing())?.caller_tier ??
    null;
  const isExternal = callerTier === 'external';
  const { data: sharedLinks } = useSharedLinks(isExternal ? workspaceId : null);

  if (isExternal && !isShared && !sharedLinks?.length) {
    return null;
  }

  // Radix owns dismissal: Escape and an outside press close it, and a keyboard
  // close hands focus back to the trigger.
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        {/* Sized as the files toggle beside it is: the panel's own button
            size on desktop, a touch target on mobile. */}
        <button
          type="button"
          className={cn('transition-colors', isMobile ? 'p-2 rounded-md' : 'p-1.5 rounded-[4px]')}
          style={{
            color: isShared ? 'var(--color-accent-primary)' : 'var(--color-text-primary)',
            backgroundColor: open ? 'var(--color-border-muted)' : undefined,
          }}
          title={t('share.shareConversation')}
          onMouseEnter={(e) => { if (!open) e.currentTarget.style.backgroundColor = 'var(--color-border-muted)'; }}
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
        />
      </PopoverContent>
    </Popover>
  );
}
