import { lazy, useId, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { BookOpen, History, LifeBuoy, MessageSquareWarning, Sparkles } from 'lucide-react'; // DALTON-HOOK(mobile-web)
import { Select } from '@/components/ui/select';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/ui/use-toast';
import TimezonePicker from '@/components/TimezonePicker';
import { updateCurrentUser } from '@/pages/Dashboard/utils/api';
import { useUser } from '@/hooks/useUser';
import { useLocale } from '@/hooks/useLocale';
import { applyLocale } from '@/hooks/useLocaleChoice';
import { isSupported } from '@/lib/locale';
import { deviceTimezone } from '@/lib/deviceTimezone';
import { queryKeys } from '@/lib/queryKeys';
import { canDesktopSettings, isDesktopShell } from '@/lib/desktop';
import { getShareLinkOpen, setShareLinkOpen } from '@/lib/shareLinkOpen';
import { useOnboarding } from '@/pages/Onboarding';
// DALTON-HOOK(mobile-web): surface /help docs & report links in Settings -> General on phones (AppSidebar/AccountMenu is unmounted on mobile)
import { useIsMobile } from '@/hooks/useIsMobile';
import { DOCS_URL, SUPPORT_URL } from '@/config/supportLinks';
import { useReportsEnabled } from '@/hooks/useReportsEnabled';
import { MountOnceOpen } from '@/components/MountOnceOpen';
import type { ReportSubject } from '@/components/report/ReportDialog';
import { SegmentedRow, SettingRow, SettingsGroup } from '../components/SettingsLayout';
import { useSettingCopy } from '../components/useSettingCopy';
import { SETTINGS } from '../sections';
// DALTON-HOOK(settings-reorg): merge Appearance and conditional Desktop panels into General & Appearance
import { AppearancePanel } from './AppearancePanel';
import { DesktopPanel } from './DesktopPanel';
import { useSaveFailureToast } from './usePreferenceWrite';

// DALTON-HOOK(mobile-web): lazy report dialog and /help documentation links for mobile GeneralPanel
const ReportDialog = lazy(() => import('@/components/report/ReportDialog'));
const SETTINGS_REPORT: ReportSubject = { event: 'user_report', source: 'button' };
const MOBILE_HELP_ITEMS = [
  { href: `${DOCS_URL}/`, icon: BookOpen, labelKey: 'sidebar.documentation' },
  { href: `${DOCS_URL}/whats-new/`, icon: Sparkles, labelKey: 'sidebar.whatsNew' },
  { href: `${DOCS_URL}/changelog/`, icon: History, labelKey: 'sidebar.changelog' },
];

const LOCALES = [
  { value: 'en-US', label: 'English' },
  { value: 'zh-CN', label: '简体中文' },
];

/** General: language and timezone, where links open in the desktop app, and
 *  the page tips and getting-started guide. */
export function GeneralPanel() {
  const { t, i18n } = useTranslation();
  const { toast } = useToast();
  const reportFailure = useSaveFailureToast();
  const copy = useSettingCopy();
  const queryClient = useQueryClient();
  const { user: authUser } = useUser();
  // The select shows the language the UI is actually in: a `locale` cookie
  // outranks the stored value (useSyncUserLocale), so the two can differ.
  const activeLocale = useLocale();
  const isMobile = useIsMobile(); // DALTON-HOOK(mobile-web)
  const reportsEnabled = useReportsEnabled(); // DALTON-HOOK(mobile-web)
  const [showReport, setShowReport] = useState(false); // DALTON-HOOK(mobile-web)
  const supportHref = SUPPORT_URL && (activeLocale === 'zh-CN' ? `${SUPPORT_URL}?lang=zh-CN` : SUPPORT_URL); // DALTON-HOOK(mobile-web)
  const { replayGuides, resetOnboarding } = useOnboarding();
  const languageId = useId();
  // The pick shows at once; the account catches up when the save lands.
  const [timezoneDraft, setTimezoneDraft] = useState<string | null>(null);
  const timezone = timezoneDraft ?? ((authUser?.timezone as string) || '');
  const [shareLinkOpen, setShareLinkOpenState] = useState(getShareLinkOpen);

  const { mutateAsync: updateUser } = useMutation({
    mutationFn: (fields: Record<string, string>) => updateCurrentUser(fields),
    // One at a time, so an earlier pick finishing last cannot overwrite a later one.
    scope: { id: 'current-user' },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.user.me() }),
  });
  const saveUser = (fields: Record<string, string>) => reportFailure(updateUser(fields));

  return (
    <>
      <SettingsGroup title={t('settings:regionGroup')}>
        <SettingRow {...copy(SETTINGS.language)} htmlFor={languageId} wide>
          <Select
            id={languageId}
            value={activeLocale}
            onChange={(e) => {
              const next = e.target.value;
              if (isSupported(next)) void applyLocale(i18n, next);
              void saveUser({ locale: next });
            }}
          >
            {LOCALES.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </Select>
        </SettingRow>
        <SettingRow {...copy(SETTINGS.timezone)} wide>
          <TimezonePicker
            value={timezone}
            onChange={(next: string) => {
              setTimezoneDraft(next);
              void saveUser({ timezone: next });
            }}
            home={{ zone: deviceTimezone(), label: t('timezone.thisDevice') }}
            placeholder={t('settings:selectTimezone')}
            className="w-full"
            // The card fill the language select beside it takes.
            triggerClassName="bg-(--color-bg-card)"
          />
        </SettingRow>
      </SettingsGroup>

      {/* Desktop only: the browser has tabs, and keeps them for these links. */}
      {isDesktopShell() && (
        <SettingsGroup title={t('settings:desktopGroup')}>
          <SegmentedRow
            {...copy(SETTINGS.linkTarget)}
            value={shareLinkOpen}
            onChange={(v) => { setShareLinkOpen(v); setShareLinkOpenState(v); }}
            options={[
              { value: 'panel', label: t('settings:shareLinkOpenPanel') },
              { value: 'browser', label: t('settings:shareLinkOpenBrowser') },
            ]}
          />
        </SettingsGroup>
      )}

      <SettingsGroup title={t('settings:helpGroup')}>
        {/* DALTON-HOOK(mobile-web): expose /help docs links & feedback in Settings on phones where AccountMenu is hidden */}
        {isMobile && (
          <SettingRow
            id="settings-mobile-help-docs"
            label={t('settings:helpDocs')}
            description={t('settings:helpDocsDesc')}
          >
            <div className="flex flex-wrap gap-2" data-testid="settings-mobile-help-links">
              {MOBILE_HELP_ITEMS.map(({ href, icon: Icon, labelKey }) => (
                <a
                  key={labelKey}
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-xs font-medium transition-colors hover:bg-(--color-bg-hover)"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  <span>{t(labelKey)}</span>
                  <span className="sr-only"> {t('sidebar.opensInNewTab')}</span>
                </a>
              ))}
              {reportsEnabled && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-9 gap-1.5 text-xs"
                  onClick={() => setShowReport(true)}
                >
                  <MessageSquareWarning className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  <span>{t('report.menuItem')}</span>
                </Button>
              )}
              {supportHref && (
                <a
                  href={supportHref}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex h-9 items-center gap-1.5 rounded-md border px-3 text-xs font-medium transition-colors hover:bg-(--color-bg-hover)"
                  style={{
                    borderColor: 'var(--color-border-muted)',
                    color: 'var(--color-text-primary)',
                  }}
                >
                  <LifeBuoy className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                  <span>{t('sidebar.contactSupport')}</span>
                </a>
              )}
            </div>
          </SettingRow>
        )}
        <SettingRow {...copy(SETTINGS.guides)}>
          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8"
              onClick={() => {
                if (replayGuides()) toast({ description: t('onboarding.settings.replayDone') });
              }}
            >
              {t('onboarding.settings.replayGuides')}
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="h-8"
              style={{ color: 'var(--color-text-secondary)' }}
              onClick={() => {
                if (resetOnboarding()) toast({ description: t('onboarding.settings.resetDone') });
              }}
            >
              {t('onboarding.settings.reset')}
            </Button>
          </div>
        </SettingRow>
      </SettingsGroup>

      {/* DALTON-HOOK(settings-reorg): merged Appearance & conditional Desktop settings */}
      <AppearancePanel />
      {canDesktopSettings() && <DesktopPanel />}
      {/* DALTON-HOOK(mobile-web): report dialog triggered from mobile Help section */}
      {isMobile && reportsEnabled && (
        <MountOnceOpen open={showReport}>
          <ReportDialog open={showReport} onOpenChange={setShowReport} subject={SETTINGS_REPORT} />
        </MountOnceOpen>
      )}
    </>
  );
}
