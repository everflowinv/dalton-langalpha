import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Send } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ToggleSwitch } from '@/components/ui/switch';
import ConfirmDialog from '@/pages/Dashboard/components/ConfirmDialog';
import { useCapabilities } from '@/hooks/useCapabilities';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { modelPrefs, splitPreferenceWrite } from '@/lib/modelPreferences';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonChannelBindings,
  sendDaltonNotifyTest,
} from '@/pages/Settings/utils/daltonApi';
import { SettingRow } from '@/pages/Settings/components/SettingsLayout';
import {
  DaltonActionConfirmButton,
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

const PERSONAL_TARGET_RE = /^(feishu:user:ou_[A-Za-z0-9_-]+|discord:user:[0-9]+)$/;
const GROUP_TARGET_RE = /^(feishu:chat:|discord:channel:)/i;

/** Personal DM targets only; group pushes are configured per task. */
function isValidDaltonNotifyTarget(raw: string): boolean {
  return PERSONAL_TARGET_RE.test(raw.trim());
}

/** A label is a plain printable name, at most 40 chars (matching server validation). */
function cleanDaltonNotifyLabel(raw: string): string {
  return raw.trim().replace(/[\p{Cc}\p{Cf}]/gu, '').slice(0, 40);
}

export interface DaltonNotifyTargetsCardProps {
  /** Only admins in Team Channels & Ops can manually enter raw target IDs. */
  allowManualInput?: boolean;
}

interface BoundCandidate {
  platform: 'feishu' | 'discord';
  target: string;
  label: string;
  accountId: string;
}

/**
 * Push notification switches & linked-account recipient toggles.
 * Replaces raw `feishu:user:ou_xxx` text entry for members by deriving bound accounts
 * from `fetchDaltonChannelBindings()`.
 */
export function DaltonNotifyTargetsCard({
  allowManualInput = false,
}: DaltonNotifyTargetsCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasCapability } = useCapabilities();
  const canFeishu = hasCapability('channel_feishu');
  const canDiscord = hasCapability('channel_discord');
  const { preferences, isLoading: prefsLoading, isLoaded } = usePreferences();
  const updatePrefs = useUpdatePreferences();

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: canFeishu || canDiscord,
    retry: false,
  });

  const [manualInput, setManualInput] = useState('');
  const [labelDrafts, setLabelDrafts] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState(false);
  const [pendingDisable, setPendingDisable] = useState<BoundCandidate | null>(null);
  const [testNotice, setTestNotice] = useState<{
    tone: 'success' | 'error';
    message: string;
  } | null>(null);

  const mp = modelPrefs(preferences);
  // Backend defaults dalton_notify_enabled to False when unset or null (src/dalton/llm/model_prefs.py).
  const masterEnabled = mp.dalton_notify_enabled === true;
  const targets = useMemo(
    () => (Array.isArray(mp.dalton_notify_targets) ? mp.dalton_notify_targets : []),
    [mp.dalton_notify_targets],
  );
  const labels = useMemo(
    () => mp.dalton_notify_target_labels ?? {},
    [mp.dalton_notify_target_labels],
  );

  const boundCandidates = useMemo<BoundCandidate[]>(() => {
    const bindings = bindingsQuery.data?.bindings;
    if (!bindings) return [];
    const list: BoundCandidate[] = [];
    if (canFeishu && bindings.feishu?.linked && bindings.feishu.platform_user_id) {
      list.push({
        platform: 'feishu',
        target: `feishu:user:${bindings.feishu.platform_user_id}`,
        label: bindings.feishu.platform_username || bindings.feishu.platform_user_id,
        accountId: bindings.feishu.platform_user_id,
      });
    }
    if (canDiscord && bindings.discord?.linked && bindings.discord.platform_user_id) {
      list.push({
        platform: 'discord',
        target: `discord:user:${bindings.discord.platform_user_id}`,
        label: bindings.discord.platform_username || bindings.discord.platform_user_id,
        accountId: bindings.discord.platform_user_id,
      });
    }
    return list;
  }, [bindingsQuery.data?.bindings, canFeishu, canDiscord]);

  const boundTargetSet = useMemo(
    () => new Set(boundCandidates.map((c) => c.target)),
    [boundCandidates],
  );

  const customTargets = useMemo(
    () => targets.filter((item) => !boundTargetSet.has(item)),
    [targets, boundTargetSet],
  );

  const saveTargets = async (nextTargets: string[]) => {
    setFormError(null);
    setSavedNotice(false);
    try {
      await updatePrefs.mutateAsync(
        splitPreferenceWrite({ dalton_notify_targets: nextTargets }),
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      setSavedNotice(true);
    } catch {
      setFormError(t('dalton.notifyTargets.saveFailed'));
    }
  };

  const handleMasterToggle = async (checked: boolean) => {
    setFormError(null);
    setSavedNotice(false);
    try {
      await updatePrefs.mutateAsync(
        splitPreferenceWrite({ dalton_notify_enabled: checked }),
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      setSavedNotice(true);
    } catch {
      setFormError(t('dalton.notifyTargets.saveFailed'));
    }
  };

  const handleCandidateToggle = async (item: BoundCandidate, checked: boolean) => {
    if (!checked) {
      setPendingDisable(item);
      return;
    }
    const next = Array.from(new Set([...targets, item.target]));
    await saveTargets(next);
  };

  const confirmDisableCandidate = async () => {
    if (!pendingDisable) return;
    const targetToRemove = pendingDisable.target;
    setPendingDisable(null);
    await saveTargets(targets.filter((tItem) => tItem !== targetToRemove));
  };

  const handleManualAdd = async () => {
    const cleaned = manualInput.trim();
    if (GROUP_TARGET_RE.test(cleaned)) {
      setFormError(t('dalton.notifyTargets.groupTargetHint'));
      return;
    }
    if (!isValidDaltonNotifyTarget(cleaned)) {
      setFormError(t('dalton.notifyTargets.manualAddError'));
      return;
    }
    if (targets.includes(cleaned)) {
      setManualInput('');
      setFormError(null);
      return;
    }
    const next = Array.from(new Set([...targets, cleaned]));
    await saveTargets(next);
    setManualInput('');
  };

  const commitLabel = async (target: string) => {
    const saved = labels[target] ?? '';
    const draftLabel = labelDrafts[target] ?? saved;
    const cleaned = cleanDaltonNotifyLabel(draftLabel);
    setLabelDrafts((prev) => ({ ...prev, [target]: cleaned }));
    if (cleaned === saved) return;
    setFormError(null);
    setSavedNotice(false);
    try {
      await updatePrefs.mutateAsync(
        splitPreferenceWrite({
          dalton_notify_target_labels: { ...labels, [target]: cleaned || null },
        }),
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.user.preferences() });
      setSavedNotice(true);
    } catch {
      setFormError(t('dalton.notifyTargets.saveFailed'));
    }
  };

  const renderAliasInput = (target: string) => (
    <Input
      value={labelDrafts[target] ?? labels[target] ?? ''}
      disabled={updatePrefs.isPending}
      aria-label={t('dalton.notifyTargets.aliasAria', { target })}
      placeholder={t('dalton.notifyTargets.aliasPlaceholder')}
      data-testid={`dalton-notify-label-${target}`}
      onChange={(e) =>
        setLabelDrafts((prev) => ({ ...prev, [target]: e.target.value }))
      }
      onBlur={() => void commitLabel(target)}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur();
      }}
      className="mt-1 h-9 md:h-7 w-full md:w-52 max-w-full text-xs"
    />
  );

  const handleRemoveTarget = async (target: string) => {
    await saveTargets(targets.filter((item) => item !== target));
  };

  const testMutation = useMutation({
    mutationFn: (target: string) => sendDaltonNotifyTest(target),
    onSuccess: (res, target) => {
      if (res.ok) {
        setTestNotice({
          tone: 'success',
          message: t('dalton.notifyTargets.testSuccess', { target }),
        });
      } else {
        setTestNotice({
          tone: 'error',
          message: res.detail || t('dalton.notifyTargets.testFailed'),
        });
      }
    },
    onError: () => {
      setTestNotice({ tone: 'error', message: t('dalton.notifyTargets.testFailed') });
    },
  });

  if (!canFeishu && !canDiscord) {
    return null;
  }

  return (
    <DaltonSettingsCard
      id={allowManualInput ? 'dalton-notify-targets-admin' : 'dalton-notify-targets'}
      testId={allowManualInput ? 'dalton-notify-targets-admin-card' : 'dalton-notify-targets-card'}
      title={
        allowManualInput
          ? t('dalton.notifyTargets.manualAddTitle')
          : t('dalton.notifyTargets.title')
      }
      description={
        allowManualInput
          ? t('dalton.notifyTargets.manualAddDesc')
          : t('dalton.notifyTargets.description')
      }
      layout="padded"
      bodyClassName="space-y-4"
      action={
        !allowManualInput && !prefsLoading && isLoaded ? (
          <DaltonStatusBadge
            dot
            tone={masterEnabled ? 'success' : 'neutral'}
            label={
              masterEnabled
                ? t('dalton.notifyTargets.masterOn')
                : t('dalton.notifyTargets.masterOff')
            }
          />
        ) : undefined
      }
    >
      {prefsLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : !isLoaded ? (
        <DaltonInlineNotice tone="error">{t('dalton.notifyTargets.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          {!allowManualInput && (
            <>
              <div
                className="rounded-md border"
                style={{
                  borderColor: 'var(--color-border-muted)',
                  backgroundColor: 'var(--color-bg-elevated)',
                }}
              >
                <SettingRow
                  id="dalton-notify-master-toggle"
                  label={t('dalton.notifyTargets.masterSwitchLabel')}
                  description={t('dalton.notifyTargets.masterSwitchDesc')}
                >
                  <ToggleSwitch
                    id="dalton-notify-master-toggle-switch"
                    ariaLabel={t('dalton.notifyTargets.masterSwitchLabel')}
                    checked={masterEnabled}
                    disabled={updatePrefs.isPending}
                    onChange={() => void handleMasterToggle(!masterEnabled)}
                  />
                </SettingRow>
              </div>

              <div className="space-y-2">
                <div>
                  <h4
                    className="text-xs font-medium"
                    style={{ color: 'var(--color-text-primary)' }}
                  >
                    {t('dalton.notifyTargets.boundAccountsTitle')}
                  </h4>
                  <p
                    className="text-xs mt-0.5"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {t('dalton.notifyTargets.boundAccountsDesc')}
                  </p>
                </div>

                {bindingsQuery.isLoading ? (
                  <DaltonLoadingRow label={t('dalton.common.loading')} />
                ) : boundCandidates.length === 0 ? (
                  <DaltonInlineNotice tone="info" testId="dalton-notify-no-bound-accounts">
                    {t('dalton.notifyTargets.noBoundAccounts')}
                  </DaltonInlineNotice>
                ) : (
                  <DaltonKeyValueList>
                    {boundCandidates.map((item) => {
                      const active = targets.includes(item.target);
                      const isTesting =
                        testMutation.isPending && testMutation.variables === item.target;
                      return (
                        <DaltonItemRow
                          key={item.target}
                          testId={`dalton-notify-bound-${item.platform}`}
                          title={
                            <span>
                              {item.platform === 'feishu'
                                ? t('dalton.notifyTargets.platformFeishu')
                                : t('dalton.notifyTargets.platformDiscord')}
                              {' · '}
                              {item.label}
                              {labels[item.target] && (
                                <span
                                  className="ml-1.5 text-xs font-normal"
                                  style={{ color: 'var(--color-text-secondary)' }}
                                >
                                  ({labels[item.target]})
                                </span>
                              )}
                            </span>
                          }
                          subtitle={renderAliasInput(item.target)}
                          meta={<span className="font-mono max-md:break-all">{item.accountId}</span>}
                          trailing={
                            <div className="flex flex-wrap items-center gap-2">
                              <Button
                                type="button"
                                variant="ghost"
                                size="sm"
                                className="h-8 gap-1 text-xs"
                                disabled={isTesting}
                                onClick={() => testMutation.mutate(item.target)}
                                data-testid={`dalton-notify-test-${item.platform}`}
                              >
                                <Send className="h-3.5 w-3.5" />
                                <span>
                                  {isTesting
                                    ? t('dalton.notifyTargets.testing')
                                    : t('dalton.notifyTargets.testButton')}
                                </span>
                              </Button>
                              <ToggleSwitch
                                checked={active}
                                disabled={updatePrefs.isPending}
                                ariaLabel={item.label}
                                onChange={() =>
                                  void handleCandidateToggle(item, !active)
                                }
                              />
                            </div>
                          }
                        />
                      );
                    })}
                  </DaltonKeyValueList>
                )}
              </div>
            </>
          )}

          {allowManualInput && (
            <div className="flex flex-wrap items-center gap-2">
              <Input
                value={manualInput}
                onChange={(e) => setManualInput(e.target.value)}
                placeholder={t('dalton.notifyTargets.manualAddPlaceholder')}
                data-testid="dalton-notify-manual-input"
                className="h-9 md:h-8 w-full md:w-72 text-xs font-mono"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="h-8 text-xs"
                disabled={updatePrefs.isPending}
                onClick={() => void handleManualAdd()}
                data-testid="dalton-notify-manual-add"
              >
                {t('dalton.notifyTargets.manualAddButton')}
              </Button>
            </div>
          )}

          {(allowManualInput ? targets : customTargets).length > 0 && (
            <div className="space-y-2">
              {!allowManualInput && (
                <div>
                  <h4
                    className="text-xs font-medium"
                    style={{ color: 'var(--color-text-primary)' }}
                  >
                    {t('dalton.notifyTargets.customTargetsTitle')}
                  </h4>
                  <p
                    className="text-xs mt-0.5"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {t('dalton.notifyTargets.customTargetsDesc')}
                  </p>
                </div>
              )}
              <DaltonKeyValueList>
                {(allowManualInput ? targets : customTargets).map((target) => {
                  const isTesting =
                    testMutation.isPending && testMutation.variables === target;
                  return (
                    <DaltonItemRow
                      key={target}
                      testId={`dalton-notify-custom-${target}`}
                      title={
                        labels[target] ? (
                          <span className="text-xs font-medium break-words">{labels[target]}</span>
                        ) : (
                          <code className="text-xs font-mono max-md:break-all">{target}</code>
                        )
                      }
                      subtitle={renderAliasInput(target)}
                      meta={<code className="font-mono max-md:break-all">{target}</code>}
                      trailing={
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-8 gap-1 text-xs"
                            disabled={isTesting}
                            onClick={() => testMutation.mutate(target)}
                          >
                            <Send className="h-3.5 w-3.5" />
                            <span>
                              {isTesting
                                ? t('dalton.notifyTargets.testing')
                                : t('dalton.notifyTargets.testButton')}
                            </span>
                          </Button>
                          <DaltonActionConfirmButton
                            label={t('dalton.notifyTargets.removeLabel')}
                            confirmTitle={t('dalton.notifyTargets.removeTitle')}
                            confirmMessage={t('dalton.notifyTargets.removeConfirm', {
                              target,
                            })}
                            confirmLabel={t('dalton.notifyTargets.removeLabel')}
                            busy={updatePrefs.isPending}
                            onConfirm={() => handleRemoveTarget(target)}
                            testId={`dalton-notify-remove-${target}`}
                          />
                        </div>
                      }
                    />
                  );
                })}
              </DaltonKeyValueList>
            </div>
          )}

          {formError && <DaltonInlineNotice tone="error">{formError}</DaltonInlineNotice>}
          {savedNotice && (
            <DaltonInlineNotice tone="success">{t('dalton.notifyTargets.saved')}</DaltonInlineNotice>
          )}
          {testNotice && (
            <DaltonInlineNotice tone={testNotice.tone}>{testNotice.message}</DaltonInlineNotice>
          )}
        </>
      )}

      <ConfirmDialog
        open={Boolean(pendingDisable)}
        title={t('dalton.notifyTargets.disableBoundTitle')}
        message={t('dalton.notifyTargets.disableBoundConfirm', {
          label: pendingDisable?.label ?? '',
        })}
        confirmLabel={t('dalton.common.confirm')}
        onOpenChange={(nextOpen) => {
          if (!nextOpen) setPendingDisable(null);
        }}
        onConfirm={() => void confirmDisableCandidate()}
      />
    </DaltonSettingsCard>
  );
}
