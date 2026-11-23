import { useTranslation } from 'react-i18next';
import { isDaltonAuth } from '@/config/daltonAuth';
import { useCapabilities } from '@/hooks/useCapabilities';
import { useTeamCredentials } from '@/hooks/useTeamCredentials';
import { useProviderStatus } from '../../hooks/useProviderStatus';
import { NativeModelsCard } from './NativeModelsCard';
import { ProviderRow } from './ProviderRow';
import {
  DaltonInlineNotice,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
} from '@/components/dalton/primitives';

/** Team provider health dashboard + model catalog sync section (admin view in Team Models). */
export function ProviderStatusCard() {
  const { t } = useTranslation();
  const { isPm, callerTier } = useTeamCredentials();
  const { hasCapability } = useCapabilities();
  const isAdmin = callerTier === 'admin' || (!isDaltonAuth && isPm);
  const showNativeModels = hasCapability('native_oauth_sync');
  const statusQuery = useProviderStatus(true);

  const providers = statusQuery.data?.providers ?? [];
  const roleWarnings = statusQuery.data?.role_warnings ?? [];

  return (
    <div className="space-y-6">
      <DaltonSettingsCard
        id="dalton-providers"
        testId="dalton-providers-card"
        title={t('dalton.providers.title')}
        description={t('dalton.providers.description')}
        layout="padded"
        bodyClassName="space-y-4"
        action={
          <DaltonRefreshButton
            busy={statusQuery.isFetching}
            onClick={() => void statusQuery.refetch()}
            testId="dalton-providers-refresh"
          />
        }
      >
        {statusQuery.isLoading ? (
          <DaltonLoadingRow label={t('dalton.common.loading')} />
        ) : statusQuery.isError ? (
          <DaltonInlineNotice tone="error">
            {t('dalton.providers.loadFailed')}
          </DaltonInlineNotice>
        ) : (
          <>
            {roleWarnings.length > 0 && (
              <div className="space-y-1.5">
                {roleWarnings.map((w, idx) => (
                  <DaltonInlineNotice key={idx} tone="warning">
                    {w.message}
                  </DaltonInlineNotice>
                ))}
              </div>
            )}

            <DaltonKeyValueList
              isEmpty={providers.length === 0}
              empty={t('dalton.providers.emptyProviders')}
            >
              {providers.map((item) => (
                <ProviderRow key={item.provider} item={item} isAdmin={isAdmin} />
              ))}
            </DaltonKeyValueList>

            <DaltonInlineNotice tone="info">
              {isAdmin
                ? t('dalton.providers.configAdminNote')
                : t('dalton.providers.configMemberNote')}
            </DaltonInlineNotice>
          </>
        )}
      </DaltonSettingsCard>

      {showNativeModels && <NativeModelsCard />}
    </div>
  );
}
