import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';
import { Check, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useOnboarding } from '../OnboardingProvider';

/**
 * Floating "Get started" checklist (bottom-left, desktop only). Tasks complete
 * via route visits or a derived signal (the saved investing profile, a
 * workspace); clicking a pending task navigates to it. Hidden once dismissed or when every task is done.
 */
export function GettingStartedCard() {
  const { gettingStarted } = useOnboarding();
  const navigate = useNavigate();
  const { t } = useTranslation();

  if (!gettingStarted.visible) return null;
  const { tasks, doneCount, dismiss, completeTask } = gettingStarted;

  return (
    <aside
      className="sidebar-tracking fixed bottom-4 z-40 hidden w-80 rounded-2xl border p-4 shadow-lg md:block"
      style={{
        left: 'calc(var(--sidebar-width) + 1rem)',
        backgroundColor: 'var(--color-bg-card)',
        borderColor: 'var(--color-border-muted)',
      }}
      aria-label={t('onboarding.gettingStarted.title', 'Get started')}
    >
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold" style={{ color: 'var(--color-text-primary)' }}>
          {t('onboarding.gettingStarted.title', 'Get started')}
        </h2>
        <div className="flex items-center gap-2">
          <span className="text-xs tabular-nums" style={{ color: 'var(--color-text-tertiary)' }}>
            {doneCount} / {tasks.length}
          </span>
          <button
            type="button"
            onClick={dismiss}
            aria-label={t('onboarding.gettingStarted.dismiss', 'Hide guide')}
            className="rounded-md p-1 transition-colors hover:bg-(--color-bg-subtle) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
            style={{ color: 'var(--color-text-tertiary)' }}
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div
        className="mt-2.5 h-1 w-full overflow-hidden rounded-full"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={tasks.length}
        aria-valuenow={doneCount}
        style={{ backgroundColor: 'var(--color-bg-elevated)' }}
      >
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{
            width: `${(doneCount / tasks.length) * 100}%`,
            backgroundColor: 'var(--color-accent-primary)',
          }}
        />
      </div>

      <ul className="mt-3 flex flex-col gap-1">
        {tasks.map(({ def, done }) => (
          <li key={def.id}>
            <button
              type="button"
              disabled={done}
              onClick={() => {
                if (def.external) {
                  // Cross-app page — no route of ours to observe, so a
                  // successful open is the completion signal. null = popup
                  // blocked; leave the task pending so the user can retry.
                  // No 'noopener' feature string: that makes open() return
                  // null even on success, which would read as "blocked" and
                  // leave the task permanently pending. Sever the opener on
                  // the returned handle instead.
                  const win = window.open(def.to, '_blank');
                  if (win) {
                    win.opener = null;
                    completeTask(def.id);
                  }
                } else {
                  navigate(def.to);
                }
              }}
              className="flex w-full items-start gap-3 rounded-lg p-2 text-left transition-colors enabled:hover:bg-(--color-bg-subtle) focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
            >
              <span
                className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border"
                style={
                  done
                    ? {
                        backgroundColor: 'var(--color-accent-primary)',
                        borderColor: 'var(--color-accent-primary)',
                      }
                    : { borderColor: 'var(--color-border-default)' }
                }
                aria-hidden
              >
                {done && (
                  <Check
                    className="h-3 w-3"
                    strokeWidth={3}
                    style={{ color: 'var(--color-text-on-accent)' }}
                  />
                )}
              </span>
              <span className="min-w-0">
                <span
                  className={cn('block text-sm font-medium', done && 'line-through')}
                  style={{
                    color: done ? 'var(--color-text-tertiary)' : 'var(--color-text-primary)',
                  }}
                >
                  {t(def.titleKey)}
                </span>
                {!done && (
                  <span
                    className="mt-0.5 block text-xs leading-relaxed"
                    style={{ color: 'var(--color-text-tertiary)' }}
                  >
                    {t(def.descKey)}
                  </span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </aside>
  );
}
