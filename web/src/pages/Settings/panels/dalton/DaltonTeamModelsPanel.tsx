import { useTranslation } from 'react-i18next';
import { SettingsCardBody, SettingsGroup } from '@/pages/Settings/components/SettingsLayout';
import { ModelAccessSection } from '../ModelAccessSection';
import { ProviderStatusCard } from './ProviderStatusCard';
import { TeamModelCatalogCard } from './TeamModelCatalogCard';

/** Team Management -> Models & Providers panel (admin-only). */
export function DaltonTeamModelsPanel() {
  const { t } = useTranslation();
  return (
    <div className="space-y-8">
      <SettingsGroup
        id="team-model-access"
        title={t('settings:modelAccess.teamTitle')}
        description={t('settings:modelAccess.teamDesc')}
      >
        <SettingsCardBody>
          <ModelAccessSection scope="team" />
        </SettingsCardBody>
      </SettingsGroup>
      <TeamModelCatalogCard />
      <ProviderStatusCard />
    </div>
  );
}
