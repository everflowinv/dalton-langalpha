import type { ReactNode } from 'react';
import { ArrowUpRight, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { Button } from '@/components/ui/button';
import { buttonSlot } from '@/components/ui/button-class';
import { Loader } from '@/components/ui/loader';
import { SettingsCardBody, SettingsGroup } from '@/pages/Settings/components/SettingsLayout';

export interface DaltonSettingsCardProps {
  id?: string;
  testId?: string;
  title?: string;
  description?: ReactNode;
  action?: ReactNode;
  /** 'padded' wraps children in SettingsCardBody inside .settings-card; 'rows' wraps in .settings-card for SettingRow items; 'bare' renders children directly inside SettingsGroup. */
  layout?: 'padded' | 'rows' | 'bare';
  bodyClassName?: string;
  /** Optional supplementary block rendered directly below the main card inside SettingsGroup. */
  below?: ReactNode;
  children: ReactNode;
}

/** Standard Dalton settings card built on upstream SettingsGroup + SettingsCardBody. */
export function DaltonSettingsCard({
  id,
  testId,
  title,
  description,
  action,
  layout = 'padded',
  bodyClassName,
  below,
  children,
}: DaltonSettingsCardProps) {
  let content: ReactNode;
  if (layout === 'padded') {
    content = <SettingsCardBody className={bodyClassName}>{children}</SettingsCardBody>;
  } else if (layout === 'rows') {
    content = bodyClassName ? <div className={bodyClassName}>{children}</div> : children;
  } else {
    content = children;
  }

  // SettingsGroup already wraps children in <div className="settings-card"> (SettingsLayout.tsx);
  // keep `below` outside the group so alerts/footnotes sit cleanly underneath without double borders.
  const group = (
    <SettingsGroup
      id={id}
      data-testid={testId}
      title={title}
      description={description}
      action={action}
    >
      {content}
    </SettingsGroup>
  );

  if (!below) return group;
  return (
    <div className="space-y-3">
      {group}
      {below}
    </div>
  );
}

export interface DaltonRefreshButtonProps {
  onClick: () => void;
  busy?: boolean;
  disabled?: boolean;
  label?: string;
  busyLabel?: string;
  title?: string;
  testId?: string;
}

/** Refresh button using upstream ghost Button + ASCII Loader when busy. */
export function DaltonRefreshButton({
  onClick,
  busy = false,
  disabled = false,
  label,
  busyLabel,
  title,
  testId,
}: DaltonRefreshButtonProps) {
  const { t } = useTranslation();
  const resolvedLabel = label ?? t('dalton.common.refresh', { defaultValue: '刷新' });
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      disabled={disabled || busy}
      onClick={onClick}
      title={title ?? resolvedLabel}
      data-testid={testId}
      className="h-8 gap-1.5 text-xs"
    >
      {busy ? <Loader size={12} label={busyLabel ?? resolvedLabel} /> : <RefreshCw className="h-3.5 w-3.5" />}
      <span>{busy && busyLabel ? busyLabel : resolvedLabel}</span>
    </Button>
  );
}

export interface DaltonNavButtonProps {
  to: string;
  label: string;
  testId?: string;
  variant?: 'outline' | 'ghost' | 'secondary';
}

/** Navigation button pointing to another page inside the app. */
export function DaltonNavButton({
  to,
  label,
  testId,
  variant = 'outline',
}: DaltonNavButtonProps) {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      onClick={() => navigate(to)}
      data-testid={testId}
      {...buttonSlot(variant)}
      className="h-8 px-2.5 text-xs gap-1"
    >
      <span>{label}</span>
      <ArrowUpRight className="h-3.5 w-3.5" />
    </button>
  );
}
