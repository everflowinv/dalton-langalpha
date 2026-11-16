import { useTranslation } from 'react-i18next';
import { Favicon } from '@/pages/ChatAgent/components/Favicon';
import { MESSAGING_APPS, messagingAppDomain, messagingAppName } from '@/pages/ChatAgent/components/messaging/messageDelivery';
import { isPlatformMode } from '@/config/hostMode';
import { useLinkedChatApps } from '@/hooks/useLinkedChatApps';
import { ConsoleLink } from '../components/ConsoleLink';
import { SettingRow, SettingsGroup } from '../components/SettingsLayout';
import { useSettingCopy } from '../components/useSettingCopy';
import { SETTINGS } from '../sections';
// DALTON-HOOK(settings-reorg): consolidate personal channel bindings, personal WeChat, and notify targets into Messaging & Channels
import { DaltonChannelBindingsCard } from './dalton/DaltonChannelBindingsCard';
import { DaltonNotifyTargetsCard } from './dalton/DaltonNotifyTargetsCard';
import { DaltonWeixinBindCard } from './dalton/DaltonWeixinBindCard';

/**
 * Integrations: each chat app with whether it is linked, and the way to set it
 * up or manage it in the account console. Linked is read off the apps an
 * automation can deliver to, the one list of them this server keeps.
 */
export function IntegrationsPanel() {
  const { t } = useTranslation();
  const copy = useSettingCopy();
  const linked = useLinkedChatApps();
  // iMessage depends on the deployment, so it shows once it is linked.
  const apps = MESSAGING_APPS.filter((app) => app !== 'imessage' || linked?.has(app));
  const chatApps = copy(SETTINGS.chatApps);

  return (
    <>
      {/* DALTON-HOOK(settings-reorg): personal notify switches, channel bindings, and WeChat link */}
      <DaltonNotifyTargetsCard />
      <DaltonChannelBindingsCard />
      <DaltonWeixinBindCard />

      {isPlatformMode && (
        <SettingsGroup title={chatApps.label} description={chatApps.description}>
          {apps.map((app, index) => {
            const domain = messagingAppDomain(app);
            const isLinked = linked?.has(app);
            const action = linked === null ? 'open' : isLinked ? 'manage' : 'setUp';
            return (
              <SettingRow
                key={app}
                id={index === 0 ? chatApps.id : undefined}
                label={(
                  <span className="inline-flex items-center gap-2">
                    {domain && <Favicon domain={domain} size={14} />}
                    {messagingAppName(app)}
                  </span>
                )}
                description={linked && t(isLinked ? 'settings.integrations.connected' : 'settings.integrations.notConnected')}
              >
                <ConsoleLink path={`integrations?app=${app}`}>
                  {t(`settings.integrations.${action}`)}
                  <span className="sr-only"> {messagingAppName(app)}</span>
                </ConsoleLink>
              </SettingRow>
            );
          })}
        </SettingsGroup>
      )}

      {/* DALTON-HOOK(settings-reorg): keep connected apps hidden in OSS mode (platformOnly parity) */}
      {isPlatformMode && (
        <SettingsGroup>
          <SettingRow {...copy(SETTINGS.connectedApps)}>
            <ConsoleLink path="connected-apps">
              {t('settings.integrations.manage')}
              <span className="sr-only"> {copy(SETTINGS.connectedApps).label}</span>
            </ConsoleLink>
          </SettingRow>
        </SettingsGroup>
      )}
    </>
  );
}
