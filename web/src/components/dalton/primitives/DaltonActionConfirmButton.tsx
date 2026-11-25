import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { ConfirmStrip } from '@/components/mcp/McpPrimitives';
import ConfirmDialog from '@/pages/Dashboard/components/ConfirmDialog';

export interface DaltonActionConfirmButtonProps {
  label: string;
  confirmTitle: string;
  confirmMessage: string;
  confirmLabel?: string;
  cancelLabel?: string;
  onConfirm: () => void | Promise<void>;
  busy?: boolean;
  disabled?: boolean;
  mode?: 'dialog' | 'strip';
  variant?: 'outline' | 'ghost' | 'destructive';
  tone?: 'danger';
  size?: 'sm' | 'default';
  icon?: ReactNode;
  className?: string;
  testId?: string;
}

/**
 * Destructive or irreversible action button wrapped in an explicit confirmation step
 * (upstream ConfirmDialog modal by default, or inline ConfirmStrip).
 */
export function DaltonActionConfirmButton({
  label,
  confirmTitle,
  confirmMessage,
  confirmLabel,
  cancelLabel,
  onConfirm,
  busy = false,
  disabled = false,
  mode = 'dialog',
  variant = 'outline',
  tone = 'danger',
  size = 'sm',
  icon,
  className,
  testId,
}: DaltonActionConfirmButtonProps) {
  const { t } = useTranslation();
  const [confirming, setConfirming] = useState(false);
  const resolvedCancelLabel =
    cancelLabel ?? t('dalton.common.cancel', { defaultValue: '取消' });

  const handleConfirm = async () => {
    if (disabled || busy) return;
    setConfirming(false);
    await onConfirm();
  };

  if (mode === 'strip' && confirming) {
    return (
      <ConfirmStrip
        message={confirmMessage}
        confirmLabel={confirmLabel ?? label}
        cancelLabel={resolvedCancelLabel}
        pending={Boolean(disabled || busy)}
        onConfirm={handleConfirm}
        onCancel={() => setConfirming(false)}
      />
    );
  }

  return (
    <>
      <Button
        type="button"
        variant={variant}
        tone={variant === 'destructive' ? undefined : tone}
        size={size}
        disabled={disabled || busy}
        onClick={() => setConfirming(true)}
        data-testid={testId}
        className={className ?? 'h-8 text-xs gap-1'}
      >
        {icon}
        <span>{label}</span>
      </Button>
      {mode === 'dialog' && (
        <ConfirmDialog
          open={confirming}
          title={confirmTitle}
          message={confirmMessage}
          confirmLabel={confirmLabel ?? label}
          confirmDisabled={Boolean(disabled || busy)}
          onConfirm={handleConfirm}
          onOpenChange={setConfirming}
        />
      )}
    </>
  );
}
