import React from 'react';
import { ToggleSwitch } from '@/components/ui/switch';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import {
  modelPrefs,
  resolveDaltonAutomationNotifyState,
  splitPreferenceWrite,
} from '@/lib/modelPreferences';
import type { Automation } from '@/types/automation';

interface AutomationNotifyToggleProps {
  automation: Pick<
    Automation,
    'name' | 'dalton_notify_category' | 'dalton_notify_effective' | 'dalton_notify_source'
  >;
}

export function AutomationNotifyToggle({
  automation,
}: AutomationNotifyToggleProps): React.ReactElement | null {
  const { preferences, isLoading, isLoaded } = usePreferences();
  const updatePreferences = useUpdatePreferences();

  if (!automation.dalton_notify_category) {
    return null;
  }

  const prefsReady = Boolean(isLoaded) && !isLoading;
  const prefs = modelPrefs(preferences);
  const masterEnabled = prefs.dalton_notify_enabled === true;
  const overrides = prefs.dalton_notify_overrides;
  const { enabled: taskEnabled } = resolveDaltonAutomationNotifyState(automation, overrides);
  const canInteract = prefsReady && masterEnabled;

  const handleToggle = () => {
    if (!canInteract) return;
    // Submit only the single changed key; backend deep-merges model_preference.dalton_notify_overrides.
    void updatePreferences.mutateAsync(
      splitPreferenceWrite({
        dalton_notify_overrides: {
          [automation.name]: !taskEnabled,
        },
      }),
    );
  };

  return (
    <div
      className="flex shrink-0 items-center gap-1.5"
      data-testid={`dalton-notify-toggle-${automation.name}`}
      title={!masterEnabled ? '在设置里打开飞书提醒' : undefined}
    >
      <span
        className="text-xs"
        style={{
          color: canInteract
            ? 'var(--color-text-secondary)'
            : 'var(--color-text-tertiary)',
        }}
      >
        推送
      </span>
      <ToggleSwitch
        checked={taskEnabled}
        onChange={handleToggle}
        disabled={!canInteract}
        ariaLabel={`推送 ${automation.name}`}
        className={!canInteract ? 'cursor-not-allowed opacity-50' : undefined}
      />
      {!masterEnabled && (
        <span
          className="text-[11px]"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          在设置里打开飞书提醒
        </span>
      )}
    </div>
  );
}
