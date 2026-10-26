import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight, Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  useDaltonBudget,
  useDaltonBudgetToday,
  useDeleteDaltonTaskCap,
  useUpdateDaltonBudget,
  useUpsertDaltonTaskCap,
} from '../../hooks/useOpenClaw';

export function DaltonBudgetCard() {
  const { t } = useTranslation();
  const budgetQuery = useDaltonBudget(true);
  const todayQuery = useDaltonBudgetToday(true);
  const updateBudget = useUpdateDaltonBudget();
  const upsertTaskCap = useUpsertDaltonTaskCap();
  const deleteTaskCap = useDeleteDaltonTaskCap();

  const [dailyCapInput, setDailyCapInput] = useState('');
  const [defaultTaskCapInput, setDefaultTaskCapInput] = useState('');
  const [timezoneInput, setTimezoneInput] = useState('Asia/Shanghai');
  const [newScopeId, setNewScopeId] = useState('');
  const [newScopeCap, setNewScopeCap] = useState('');
  const [runsExpanded, setRunsExpanded] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  useEffect(() => {
    if (budgetQuery.data) {
      setDailyCapInput(
        budgetQuery.data.daily_cap_usd != null ? String(budgetQuery.data.daily_cap_usd) : '',
      );
      setDefaultTaskCapInput(
        budgetQuery.data.default_task_cap_usd != null
          ? String(budgetQuery.data.default_task_cap_usd)
          : '',
      );
      setTimezoneInput(budgetQuery.data.timezone || 'Asia/Shanghai');
    }
  }, [budgetQuery.data]);

  const today = todayQuery.data;
  const capUsd = today?.cap_usd ?? 0;
  const spentUsd = today?.spent_usd ?? 0;
  const pct = capUsd > 0 ? Math.min(100, Math.round((spentUsd / capUsd) * 100)) : 0;

  const handleSaveBudget = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaveMessage(null);
    const parsedDaily = parseFloat(dailyCapInput);
    if (Number.isNaN(parsedDaily) || parsedDaily < 0) return;
    const parsedTask =
      defaultTaskCapInput.trim() === '' ? null : parseFloat(defaultTaskCapInput);

    await updateBudget.mutateAsync({
      daily_cap_usd: parsedDaily,
      default_task_cap_usd: parsedTask != null && !Number.isNaN(parsedTask) ? parsedTask : null,
      timezone: timezoneInput.trim() || 'Asia/Shanghai',
    });
    setSaveMessage(t('dalton.budget.saved', '已保存日预算配置'));
  };

  const handleAddWorkspaceCap = async (e: React.FormEvent) => {
    e.preventDefault();
    const val = parseFloat(newScopeCap);
    if (!newScopeId.trim() || Number.isNaN(val) || val <= 0) return;
    await upsertTaskCap.mutateAsync({
      scopeKind: 'workspace',
      scopeId: newScopeId.trim(),
      capUsd: val,
    });
    setNewScopeId('');
    setNewScopeCap('');
  };

  return (
    <div
      id="dalton-budget"
      className="flex flex-col gap-3 p-4 rounded-lg"
      style={{
        backgroundColor: 'var(--color-bg-card)',
        border: '1px solid var(--color-border-default)',
      }}
      data-testid="dalton-budget-card"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium" style={{ color: 'var(--color-text-primary)' }}>
          {t('dalton.budget.title', 'Dalton 本地日美元预算')}
        </span>
        <span className="text-xs font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
          {today?.day ?? ''} ({today?.timezone ?? timezoneInput})
        </span>
      </div>

      {/* Progress bar */}
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between text-xs">
          <span style={{ color: 'var(--color-text-secondary)' }}>
            今日已用{' '}
            <strong className="font-mono">${spentUsd.toFixed(2)}</strong> / 日额度{' '}
            <strong className="font-mono">${capUsd.toFixed(2)}</strong>
          </span>
          <span className="font-mono" style={{ color: 'var(--color-text-tertiary)' }}>
            剩余 ${(today?.remaining_usd ?? 0).toFixed(2)}
          </span>
        </div>
        <div
          className="w-full h-2 rounded-full overflow-hidden"
          style={{ backgroundColor: 'var(--color-bg-tag)' }}
        >
          <div
            className="h-full transition-all duration-200"
            style={{
              width: `${pct}%`,
              backgroundColor:
                pct >= 90
                  ? 'var(--color-loss)'
                  : pct >= 75
                    ? 'var(--color-warning)'
                    : 'var(--color-profit)',
            }}
          />
        </div>
        <div
          className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]"
          style={{ color: 'var(--color-text-tertiary)' }}
        >
          <span>已结算：${(today?.settled_usd ?? 0).toFixed(2)}</span>
          <span>未结算预留：${(today?.open_reserved_usd ?? 0).toFixed(2)}</span>
          {(today?.expired_conservative_usd ?? 0) > 0 && (
            <span>过期保守入账：${(today?.expired_conservative_usd ?? 0).toFixed(2)}</span>
          )}
          <span>
            订阅名义用量（不计入现金额度）：$
            {(today?.subscription_notional_usd ?? 0).toFixed(2)}
          </span>
          <span>
            {t(
              'dalton.budget.byokCashNote',
              '注：设置页填写的现金类 BYOK Key 同样计入本地日美元现金额度（L24）',
            )}
          </span>
          {today?.budget_gate_enabled === false && (
            <span style={{ color: 'var(--color-warning)' }}>
              ⚠ 预算闸门未开启（DALTON_BUDGET_GATE!=1）
            </span>
          )}
        </div>
        {today?.by_provider && Object.keys(today.by_provider).length > 0 && (
          <div
            className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] font-mono pt-0.5"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            {Object.entries(today.by_provider).map(([prov, st]) => (
              <span key={prov}>
                {prov}: 现金 ${(st.cash_usd ?? 0).toFixed(2)} / 名义 $
                {(st.notional_usd ?? 0).toFixed(2)} ({st.calls ?? 0} 次)
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Budget settings form */}
      <form onSubmit={(e) => void handleSaveBudget(e)} className="grid grid-cols-1 sm:grid-cols-3 gap-2.5 pt-1">
        <div className="flex flex-col gap-1">
          <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {t('dalton.budget.dailyCapLabel', '日额度上限 (USD)')}
          </label>
          <Input
            type="number"
            step="0.01"
            min="0"
            value={dailyCapInput}
            onChange={(e) => setDailyCapInput(e.target.value)}
            placeholder="例如 20.00"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {t('dalton.budget.defaultTaskCapLabel', '默认单任务上限 (USD, 可空)')}
          </label>
          <Input
            type="number"
            step="0.01"
            min="0.01"
            value={defaultTaskCapInput}
            onChange={(e) => setDefaultTaskCapInput(e.target.value)}
            placeholder="不限"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
            {t('dalton.budget.timezoneLabel', '预算重置时区')}
          </label>
          <div className="flex items-center gap-2">
            <Input
              value={timezoneInput}
              onChange={(e) => setTimezoneInput(e.target.value)}
              placeholder="Asia/Shanghai"
            />
            <Button
              type="submit"
              size="sm"
              disabled={updateBudget.isPending}
              className="shrink-0 whitespace-nowrap"
            >
              {t('common.save', '保存')}
            </Button>
          </div>
        </div>
      </form>

      {saveMessage && (
        <div className="text-xs" style={{ color: 'var(--color-profit)' }}>
          {saveMessage}
        </div>
      )}

      {/* Workspace task caps */}
      <div className="flex flex-col gap-2 pt-1">
        <span className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>
          {t('dalton.budget.workspaceCapsTitle', '按工作区覆盖单任务上限 (Task Cap)')}
        </span>
        {(budgetQuery.data?.task_caps?.length ?? 0) > 0 && (
          <div className="space-y-1 text-xs">
            {budgetQuery.data!.task_caps.map((tc) => (
              <div
                key={`${tc.scope_kind}:${tc.scope_id}`}
                className="flex items-center justify-between gap-2 px-2.5 py-1.5 rounded"
                style={{ backgroundColor: 'var(--color-bg-tag)' }}
              >
                <span className="font-mono">
                  [{tc.scope_kind}] {tc.scope_name} — ${tc.cap_usd.toFixed(2)}
                </span>
                <button
                  type="button"
                  onClick={() =>
                    void deleteTaskCap.mutateAsync({
                      scopeKind: tc.scope_kind,
                      scopeId: tc.scope_id,
                    })
                  }
                  style={{ color: 'var(--color-loss)' }}
                  title="删除"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
        <form onSubmit={(e) => void handleAddWorkspaceCap(e)} className="flex items-center gap-2">
          <Input
            value={newScopeId}
            onChange={(e) => setNewScopeId(e.target.value)}
            placeholder="Workspace UUID"
            className="text-xs"
          />
          <Input
            type="number"
            step="0.01"
            min="0.01"
            value={newScopeCap}
            onChange={(e) => setNewScopeCap(e.target.value)}
            placeholder="上限 USD"
            className="w-32 text-xs"
          />
          <Button
            type="submit"
            size="sm"
            variant="outline"
            disabled={upsertTaskCap.isPending}
            className="shrink-0 whitespace-nowrap"
          >
            <Plus className="h-3.5 w-3.5 mr-1" />
            添加
          </Button>
        </form>
      </div>

      {/* Collapsible recent reservations */}
      <div className="pt-1">
        <button
          type="button"
          onClick={() => setRunsExpanded((v) => !v)}
          className="flex items-center gap-1 text-xs font-medium"
          style={{ color: 'var(--color-text-secondary)' }}
        >
          {runsExpanded ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
          <span>今日运行预留明细 ({today?.runs?.length ?? 0})</span>
        </button>
        {runsExpanded && (
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse font-mono">
              <thead>
                <tr
                  className="border-b"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-tertiary)',
                  }}
                >
                  <th className="py-1 pr-2">Run</th>
                  <th className="py-1 pr-2">状态</th>
                  <th className="py-1 pr-2">预留</th>
                  <th className="py-1 pr-2">已观测</th>
                  <th className="py-1">计费额</th>
                </tr>
              </thead>
              <tbody>
                {(today?.runs ?? []).map((r) => (
                  <tr
                    key={r.run_ref}
                    className="border-b"
                    style={{ borderColor: 'var(--color-border-muted)' }}
                  >
                    <td className="py-1 pr-2">{r.run_ref.slice(0, 8)}</td>
                    <td className="py-1 pr-2">{r.state}</td>
                    <td className="py-1 pr-2">${r.reserved_usd.toFixed(3)}</td>
                    <td className="py-1 pr-2">${r.observed_usd.toFixed(3)}</td>
                    <td className="py-1">${r.charge_usd.toFixed(3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
