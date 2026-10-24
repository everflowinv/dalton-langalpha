import React, { useState } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ToggleSwitch } from '@/components/ui/switch';
import { usePreferences } from '@/hooks/usePreferences';
import { useUpdatePreferences } from '@/hooks/useUpdatePreferences';
import { modelPrefs, splitPreferenceWrite } from '@/lib/modelPreferences';

export const FEISHU_NOTIFY_TARGET_RE = /^feishu:user:ou_[A-Za-z0-9_-]+$/;
export const DISCORD_NOTIFY_TARGET_RE = /^discord:user:[0-9]+$/;

export function isValidDaltonNotifyTarget(raw: string): boolean {
  const cleaned = raw.trim();
  return FEISHU_NOTIFY_TARGET_RE.test(cleaned) || DISCORD_NOTIFY_TARGET_RE.test(cleaned);
}

export function DaltonNotifyTargetsCard(): React.ReactElement {
  const { preferences, isLoading, isLoaded } = usePreferences();
  const updatePreferences = useUpdatePreferences();
  const prefsReady = Boolean(isLoaded) && !isLoading;

  // Saved values are derived directly from React Query preferences (never copied into local state).
  const prefs = modelPrefs(preferences);
  const enabled = prefs.dalton_notify_enabled === true;
  const targets = Array.isArray(prefs.dalton_notify_targets)
    ? prefs.dalton_notify_targets
    : [];

  // Local state holds only the input draft and transient feedback messages.
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  const handleToggleEnabled = async () => {
    if (!prefsReady) return;
    const next = !enabled;
    setError(null);
    setSaveMessage(null);
    try {
      await updatePreferences.mutateAsync(
        splitPreferenceWrite({ dalton_notify_enabled: next }),
      );
      setSaveMessage(next ? '已开启飞书提醒' : '已关闭飞书提醒');
    } catch (err: unknown) {
      const msg =
        err instanceof Error && err.message
          ? err.message
          : '保存飞书提醒开关失败';
      setError(msg);
    }
  };

  const persistTargets = async (nextTargets: string[]) => {
    setError(null);
    setSaveMessage(null);
    try {
      await updatePreferences.mutateAsync(
        splitPreferenceWrite({ dalton_notify_targets: nextTargets }),
      );
      setSaveMessage('已保存推送目标');
    } catch (err: unknown) {
      const msg =
        err instanceof Error && err.message
          ? err.message
          : '保存推送目标失败';
      setError(msg);
    }
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!prefsReady) return;
    setSaveMessage(null);
    const cleaned = draft.trim();
    if (!cleaned) {
      setError('请输入推送目标');
      return;
    }
    if (!isValidDaltonNotifyTarget(cleaned)) {
      setError('格式无效，请使用 feishu:user:<open_id>（如 feishu:user:ou_xxx）或 discord:user:<数字id>');
      return;
    }
    if (targets.includes(cleaned)) {
      setDraft('');
      setError(null);
      return;
    }
    const next = [...targets, cleaned];
    setDraft('');
    await persistTargets(next);
  };

  const handleRemove = async (targetToRemove: string) => {
    if (!prefsReady) return;
    const next = targets.filter((item) => item !== targetToRemove);
    await persistTargets(next);
  };

  return (
    <div
      className="flex flex-col gap-3 rounded-lg p-4"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-muted)',
      }}
      data-testid="dalton-notify-targets-card"
    >
      <div className="flex items-start justify-between gap-4 border-b pb-3" style={{ borderColor: 'var(--color-border-muted)' }}>
        <div className="flex flex-col gap-1">
          <span
            className="text-sm font-semibold"
            style={{ color: 'var(--color-text-primary)' }}
          >
            飞书提醒
          </span>
          <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
            开启后，财报快讯、深度复盘、财报前瞻、重大股价异动、每周复盘会私信给你。
          </p>
        </div>
        <ToggleSwitch
          checked={enabled}
          onChange={() => void handleToggleEnabled()}
          disabled={!prefsReady}
          ariaLabel="飞书提醒"
          className="mt-0.5"
        />
      </div>

      <div className="flex flex-col gap-1">
        <label
          htmlFor="dalton-notify-target-input"
          className="text-sm font-semibold"
          style={{ color: 'var(--color-text-primary)' }}
        >
          推送到
        </label>
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          自动任务完成后经 OpenClaw 发给您本人的飞书或 Discord 私信目标（格式：feishu:user:ou_xxx 或 discord:user:123）
        </p>
      </div>

      {targets.length > 0 ? (
        <ul className="space-y-1.5 text-xs" data-testid="dalton-notify-target-list">
          {targets.map((target) => (
            <li
              key={target}
              className="flex items-center justify-between gap-2 rounded px-2.5 py-1.5"
              style={{ backgroundColor: 'var(--color-bg-tag)' }}
            >
              <span className="font-mono" style={{ color: 'var(--color-text-primary)' }}>
                {target}
              </span>
              <button
                type="button"
                onClick={() => void handleRemove(target)}
                disabled={!prefsReady}
                aria-label={`删除 ${target}`}
                className="inline-flex items-center gap-1 text-xs disabled:cursor-not-allowed disabled:opacity-50"
                style={{ color: 'var(--color-loss)' }}
              >
                <Trash2 className="h-3.5 w-3.5" />
                删除
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
          暂未配置推送目标（不发送自动任务通知）
        </p>
      )}

      <form onSubmit={(e) => void handleAdd(e)} className="flex items-center gap-2">
        <Input
          id="dalton-notify-target-input"
          aria-label="推送到"
          value={draft}
          disabled={!prefsReady}
          onChange={(e) => {
            setDraft(e.target.value);
            if (error) setError(null);
          }}
          placeholder="例如 feishu:user:ou_xxx 或 discord:user:123"
        />
        <Button type="submit" size="sm" disabled={!prefsReady}>
          <Plus className="mr-1 h-3.5 w-3.5" />
          添加
        </Button>
      </form>

      {error && (
        <p role="alert" className="text-xs" style={{ color: 'var(--color-loss)' }}>
          {error}
        </p>
      )}
      {saveMessage && !error && (
        <p className="text-xs" style={{ color: 'var(--color-profit)' }}>
          {saveMessage}
        </p>
      )}
    </div>
  );
}
