import React, { useMemo, useState } from 'react';
import { Calendar, Check, ShieldAlert, X } from 'lucide-react';
import type { ElicitationResponseEntry } from '@/types/api';
import type { ToolApprovalState } from '@/types/chat';
import type { ToolApprovalDecisionExtra } from '../../session/interrupts/answerBoard';
import { SettledStep } from '../SettledStep';
import { ResolveSwap } from '../decisionCardMotion';

interface PersonalIntegrationApprovalCardProps {
  data: ToolApprovalState;
  onApprove?: (extra?: ToolApprovalDecisionExtra) => void;
  onReject?: (message?: string, extra?: ToolApprovalDecisionExtra) => void;
}

interface ElicitationSchemaProperty {
  title?: string;
  type?: string;
  enum?: unknown[];
  enumNames?: string[];
  description?: string;
}

interface ParsedElicitationItem {
  key: string;
  message: string;
  properties: Record<string, ElicitationSchemaProperty>;
  required: string[];
}

function parseInputRequests(
  raw: Record<string, unknown> | undefined,
): ParsedElicitationItem[] {
  if (!raw || typeof raw !== 'object') return [];
  const items: ParsedElicitationItem[] = [];
  for (const [key, def] of Object.entries(raw)) {
    const defObj = def && typeof def === 'object' ? (def as Record<string, unknown>) : {};
    const params =
      defObj.params && typeof defObj.params === 'object'
        ? (defObj.params as Record<string, unknown>)
        : {};
    const schema =
      params.requestedSchema && typeof params.requestedSchema === 'object'
        ? (params.requestedSchema as Record<string, unknown>)
        : {};
    const properties =
      schema.properties && typeof schema.properties === 'object'
        ? (schema.properties as Record<string, ElicitationSchemaProperty>)
        : {};
    const required = Array.isArray(schema.required)
      ? schema.required.filter((r): r is string => typeof r === 'string')
      : [];
    items.push({
      key,
      message: typeof params.message === 'string' ? params.message : '',
      properties,
      required,
    });
  }
  return items;
}

const ACTION_TITLES: Record<string, string> = {
  dalton_calendar_create_event: '确认新建个人日程',
  dalton_calendar_update_event: '确认修改个人日程',
  dalton_calendar_delete_event: '确认删除个人日程',
  dalton_google_mcp_elicitation: 'Google 日历安全确认',
  dalton_mail_create_draft: '确认保存邮件草稿',
  dalton_mail_send: '确认发送个人邮件',
};

export function PersonalIntegrationApprovalCard({
  data,
  onApprove,
  onReject,
}: PersonalIntegrationApprovalCardProps): React.ReactElement {
  const actionType = data.actionType || data.toolName;
  const isElicitation = actionType === 'dalton_google_mcp_elicitation';
  const requiresSecondConfirm = Boolean(data.requiresSecondConfirm);
  const canAct = !!onApprove || !!onReject;
  const isApproved = data.status === 'approved';
  const isRejected = data.status === 'rejected';

  const [secondConfirmed, setSecondConfirmed] = useState(false);
  const [reason, setReason] = useState('');

  const elicitationItems = useMemo(
    () => (isElicitation ? parseInputRequests(data.inputRequests) : []),
    [isElicitation, data.inputRequests],
  );

  // Per-<request_key> form values keyed as `${reqKey}.${propName}`
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});

  const args = data.args || {};
  const providerLabel =
    typeof args.provider_label === 'string'
      ? args.provider_label
      : args.provider === 'feishu'
        ? '飞书日历'
        : 'Google 日历';
  const externalAttendees = Array.isArray(args.external_attendees)
    ? args.external_attendees.filter((a): a is string => typeof a === 'string')
    : [];
  const beforeEvent =
    args.before && typeof args.before === 'object'
      ? (args.before as Record<string, unknown>)
      : null;

  const title = ACTION_TITLES[actionType] || '确认个人日历操作';

  const handleApproveStandard = () => {
    if (!onApprove) return;
    if (requiresSecondConfirm) {
      if (!secondConfirmed || !data.payloadSha256) return;
      onApprove({
        secondConfirmAcknowledged: true,
        confirmedPayloadSha256: data.payloadSha256,
      });
      return;
    }
    onApprove();
  };

  const handleElicitationAccept = (overrideKey?: string, overrideProp?: string, overrideVal?: unknown) => {
    if (!onApprove) return;
    const responses: Record<string, ElicitationResponseEntry> = {};
    for (const item of elicitationItems) {
      const content: Record<string, unknown> = {};
      for (const propName of Object.keys(item.properties)) {
        if (overrideKey === item.key && overrideProp === propName) {
          content[propName] = overrideVal;
        } else {
          const val = formValues[`${item.key}.${propName}`];
          if (val !== undefined) {
            content[propName] = val;
          }
        }
      }
      responses[item.key] = { action: 'accept', content };
    }
    onApprove({ elicitationResponses: responses });
  };

  const handleElicitationDecline = (action: 'decline' | 'cancel') => {
    if (!onReject) return;
    const responses: Record<string, ElicitationResponseEntry> = {};
    for (const item of elicitationItems) {
      responses[item.key] = { action };
    }
    onReject(reason.trim() || undefined, { elicitationResponses: responses });
  };

  const detailContent = isElicitation ? (
    <div className="space-y-3">
      {elicitationItems.map((item) => (
        <div
          key={item.key}
          data-testid={`elicitation-request-${item.key}`}
          className="rounded-lg px-3.5 py-2.5 space-y-2 text-xs"
          style={{ border: '1px solid var(--color-border-muted)' }}
        >
          {item.message && (
            <div className="whitespace-pre-wrap text-(--color-text-primary)">{item.message}</div>
          )}
          {Object.entries(item.properties).map(([propName, prop]) => {
            const fieldId = `${item.key}.${propName}`;
            if (Array.isArray(prop.enum) && prop.enum.length > 0) {
              return (
                <div key={propName} className="space-y-1.5">
                  {prop.title && (
                    <div className="font-medium text-(--color-text-secondary)">{prop.title}</div>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {prop.enum.map((optVal, idx) => {
                      const optLabel =
                        Array.isArray(prop.enumNames) && typeof prop.enumNames[idx] === 'string'
                          ? prop.enumNames[idx]
                          : String(optVal);
                      const isSelected = formValues[fieldId] === optVal;
                      return (
                        <button
                          key={String(optVal)}
                          type="button"
                          disabled={!canAct}
                          onClick={() => {
                            if (
                              elicitationItems.length === 1 &&
                              Object.keys(item.properties).length === 1
                            ) {
                              handleElicitationAccept(item.key, propName, optVal);
                            } else {
                              setFormValues((prev) => ({ ...prev, [fieldId]: optVal }));
                            }
                          }}
                          className="press text-xs px-3 py-1.5 rounded-md font-medium border"
                          style={{
                            borderColor: isSelected
                              ? 'var(--color-btn-primary-bg)'
                              : 'var(--color-border-muted)',
                            backgroundColor: isSelected
                              ? 'var(--color-btn-primary-bg)'
                              : 'transparent',
                            color: isSelected
                              ? 'var(--color-btn-primary-text)'
                              : 'var(--color-text-primary)',
                          }}
                        >
                          {optLabel}
                        </button>
                      );
                    })}
                  </div>
                </div>
              );
            }
            return (
              <label key={propName} className="block space-y-1">
                <span className="font-medium text-(--color-text-secondary)">
                  {prop.title || propName}
                </span>
                <input
                  type="text"
                  disabled={!canAct}
                  value={String(formValues[fieldId] ?? '')}
                  onChange={(e) =>
                    setFormValues((prev) => ({ ...prev, [fieldId]: e.target.value }))
                  }
                  className="w-full text-xs px-2.5 py-1.5 rounded-md bg-transparent outline-hidden"
                  style={{
                    border: '1px solid var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                  }}
                />
              </label>
            );
          })}
        </div>
      ))}
    </div>
  ) : (
    <div
      className="rounded-lg px-4 py-3 space-y-1.5 text-xs"
      style={{ border: '1px solid var(--color-border-muted)' }}
    >
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-(--color-text-secondary)">
        <span>平台：{providerLabel}</span>
        {typeof args.calendar_id === 'string' && <span>日历：{args.calendar_id}</span>}
        {typeof args.account_email === 'string' && args.account_email && (
          <span>账号：{args.account_email}</span>
        )}
      </div>
      {typeof args.summary === 'string' && args.summary && (
        <div className="text-sm font-medium text-(--color-text-primary)">{args.summary}</div>
      )}
      {(typeof args.start_time === 'string' || typeof args.end_time === 'string') && (
        <div className="text-(--color-text-secondary)">
          时间：{String(args.start_time || '')} → {String(args.end_time || '')}
          {typeof args.timezone === 'string' ? ` (${args.timezone})` : ''}
        </div>
      )}
      {typeof args.location === 'string' && args.location && (
        <div className="text-(--color-text-secondary)">地点：{args.location}</div>
      )}
      {Array.isArray(args.reminders) && args.reminders.length > 0 && (
        <div className="text-(--color-text-secondary)">
          提醒：
          {args.reminders
            .map((r) => {
              const obj = r && typeof r === 'object' ? (r as Record<string, unknown>) : {};
              return `提前 ${String(obj.minutes ?? 10)} 分钟`;
            })
            .join('、')}
        </div>
      )}
      {beforeEvent && typeof beforeEvent.summary === 'string' && (
        <div className="pt-1 border-t border-(--color-border-muted) text-(--color-text-tertiary)">
          原日程标题：{beforeEvent.summary}
        </div>
      )}
      {externalAttendees.length > 0 && (
        <div
          data-testid="l2-attendees-warning"
          className="mt-2 rounded-md p-2.5 space-y-1 bg-amber-500/10 text-(--color-text-primary)"
        >
          <div className="flex items-center gap-1.5 font-medium">
            <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
            将向以下参会人发送日程通知（二次确认）
          </div>
          <div className="text-(--color-text-secondary)">{externalAttendees.join('、')}</div>
        </div>
      )}
    </div>
  );

  const showSubmitElicitationBtn =
    isElicitation &&
    (elicitationItems.length > 1 ||
      elicitationItems.some((item) =>
        Object.values(item.properties).some((p) => !Array.isArray(p.enum) || p.enum.length === 0),
      ));

  return (
    <ResolveSwap
      resolved={isApproved || isRejected}
      settled={
        <SettledStep
          verdict={isApproved ? 'approved' : 'rejected'}
          reason={isRejected ? data.reason : null}
          label={isApproved ? `已确认：${title}` : `已取消：${title}`}
        >
          {detailContent}
        </SettledStep>
      }
    >
      <div data-testid="personal-integration-approval-card" className="space-y-3">
        <div className="flex items-center gap-2.5">
          <Calendar className="h-4 w-4 shrink-0 text-(--color-text-secondary)" />
          <div className="min-w-0 flex-1">
            <div className="text-[0.9375rem] font-medium text-(--color-text-primary)">
              {title}
            </div>
            <div className="text-xs text-(--color-text-tertiary)">
              {providerLabel}
              {data.riskLevel ? ` · ${data.riskLevel}` : ''}
            </div>
          </div>
        </div>

        {detailContent}

        {canAct && !isElicitation && requiresSecondConfirm && (
          <label className="flex items-center gap-2 text-xs text-(--color-text-primary) cursor-pointer">
            <input
              type="checkbox"
              checked={secondConfirmed}
              onChange={(e) => setSecondConfirmed(e.target.checked)}
              data-testid="l2-second-confirm-checkbox"
            />
            <span>我已核对全部参会人并确认发送日程通知</span>
          </label>
        )}

        {canAct && !isElicitation && (
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={handleApproveStandard}
              disabled={!onApprove || (requiresSecondConfirm && !secondConfirmed)}
              className="press flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium disabled:opacity-50"
              style={{
                backgroundColor: 'var(--color-btn-primary-bg)',
                color: 'var(--color-btn-primary-text)',
              }}
            >
              <Check className="h-3.5 w-3.5 stroke-[2.5]" />
              确认执行
            </button>
            <button
              type="button"
              onClick={() => onReject?.(reason.trim() || undefined)}
              disabled={!onReject}
              className="press flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium disabled:opacity-50"
              style={{
                backgroundColor: 'var(--color-border-muted)',
                color: 'var(--color-text-tertiary)',
              }}
            >
              <X className="h-3.5 w-3.5" />
              取消
            </button>
            <input
              type="text"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              maxLength={200}
              placeholder="可选：填写取消原因或修改意见"
              aria-label="取消原因"
              className="flex-1 min-w-40 text-sm px-3 py-2 rounded-md bg-transparent outline-hidden"
              style={{
                border: '1px solid var(--color-border-muted)',
                color: 'var(--color-text-primary)',
              }}
            />
          </div>
        )}

        {canAct && isElicitation && (
          <div className="flex flex-wrap items-center gap-2">
            {showSubmitElicitationBtn && (
              <button
                type="button"
                onClick={() => handleElicitationAccept()}
                disabled={!onApprove}
                className="press flex items-center gap-1.5 text-sm px-4 py-2 rounded-md font-medium disabled:opacity-50"
                style={{
                  backgroundColor: 'var(--color-btn-primary-bg)',
                  color: 'var(--color-btn-primary-text)',
                }}
              >
                <Check className="h-3.5 w-3.5 stroke-[2.5]" />
                提交确认
              </button>
            )}
            <button
              type="button"
              onClick={() => handleElicitationDecline('decline')}
              disabled={!onReject}
              className="press flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md font-medium disabled:opacity-50"
              style={{
                backgroundColor: 'var(--color-border-muted)',
                color: 'var(--color-text-tertiary)',
              }}
            >
              <X className="h-3.5 w-3.5" />
              拒绝
            </button>
            <button
              type="button"
              onClick={() => handleElicitationDecline('cancel')}
              disabled={!onReject}
              className="press flex items-center gap-1.5 text-sm px-3 py-1.5 rounded-md font-medium disabled:opacity-50"
              style={{
                backgroundColor: 'var(--color-border-muted)',
                color: 'var(--color-text-tertiary)',
              }}
            >
              关闭并取消
            </button>
          </div>
        )}
      </div>
    </ResolveSwap>
  );
}
