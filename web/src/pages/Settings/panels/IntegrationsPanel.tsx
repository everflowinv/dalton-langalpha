import { useTranslation } from 'react-i18next';
import { Favicon } from '@/pages/ChatAgent/components/Favicon';
import { MESSAGING_APPS, messagingAppDomain, messagingAppName } from '@/pages/ChatAgent/components/messaging/messageDelivery';
import { isPlatformMode } from '@/config/hostMode';
import { useCapabilities } from '@/hooks/useCapabilities'; // DALTON-HOOK(standalone-core)
import { useLinkedChatApps } from '@/hooks/useLinkedChatApps';
import { DaltonLoadingRow } from '@/components/dalton/primitives'; // DALTON-HOOK(standalone-core)
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
  // DALTON-HOOK(standalone-core): gate personal channel cards on enabled channel capabilities
  // DALTON-HOOK(weixin-multi-account): show personal notify targets when channel_weixin is enabled too
  const { hasCapability, isLoading: capsLoading } = useCapabilities();
  const showNotifyTargets = hasCapability(['channel_feishu', 'channel_discord', 'channel_weixin']);
  const showChannelBindings = hasCapability(['channel_feishu', 'channel_discord']);
  const showWeixinBind = hasCapability('channel_weixin');
  // iMessage depends on the deployment, so it shows once it is linked.
  const apps = MESSAGING_APPS.filter((app) => app !== 'imessage' || linked?.has(app));
  const chatApps = copy(SETTINGS.chatApps);

  if (capsLoading) {
    return <DaltonLoadingRow label={t('dalton.common.loading')} />;
  }

  return (
    <>
      {/* DALTON-HOOK(settings-reorg): personal notify switches, channel bindings, and WeChat link */}
      {showNotifyTargets && <DaltonNotifyTargetsCard />}
      {showChannelBindings && <DaltonChannelBindingsCard />}
      {showWeixinBind && <DaltonWeixinBindCard />}

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
                description={linked && t(isLinked ? 'settings:integrations.connected' : 'settings:integrations.notConnected')}
              >
                <ConsoleLink path={`integrations?app=${app}`}>
                  {t(`settings:integrations.${action}`)}
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
              {t('settings:integrations.manage')}
              <span className="sr-only"> {copy(SETTINGS.connectedApps).label}</span>
            </ConsoleLink>
          </SettingRow>
        </SettingsGroup>
      )}
    </>
  );
}
