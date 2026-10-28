import { useId } from 'react';
import { FileText, Code2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Select } from '@/components/ui/select';
import { usePreferences } from '@/hooks/usePreferences';
import { useAllModels } from '@/hooks/useAllModels';
import { useSearchTierAllows } from '@/hooks/useSearchTierAllows';
import { readSubagentsDefault, subagentsDefaultPatch } from '@/lib/subagentsDefault';
import { readSearchPreference, searchDepthPatch, searchProviderPatch } from '@/lib/searchPreferences';
import { SegmentedRow, SettingRow, SettingsGroup, ToggleRow } from '../components/SettingsLayout';
import { useSettingCopy } from '../components/useSettingCopy';
import { SETTINGS } from '../sections';
import { usePreferenceWrite } from './usePreferenceWrite';
import { AutoApproveSettings } from './AutoApproveSettings';
import type { Preferences } from './types';

/** Behavior: how the agent answers, whether it delegates, what it may do
 *  without asking, and how it searches the web. */
export function AgentPanel() {
  const { t } = useTranslation();
  const copy = useSettingCopy();
  const { preferences: prefsData } = usePreferences();
  const write = usePreferenceWrite();
  const tierAllows = useSearchTierAllows();
  const { searchProviders } = useAllModels();
  const depthId = useId();

  const outputFormat = (prefsData as Preferences | null)?.agent_preference?.output_format === 'html' ? 'html' : 'markdown';
  const subagentsDefault = readSubagentsDefault(prefsData);
  const search = readSearchPreference(prefsData, searchProviders);
  const providers = Object.entries(searchProviders ?? {});
  const providerCopy = copy(SETTINGS.searchProvider);

  return (
    <>
      <SettingsGroup title={t('settings.answersGroup')}>
        <SegmentedRow
          {...copy(SETTINGS.outputFormat)}
          value={outputFormat}
          onChange={(format) => { void write({ agent_preference: { output_format: format === 'html' ? 'html' : null } }); }}
          options={[
            { value: 'markdown', label: <><FileText className="h-3.5 w-3.5" />{t('settings.outputFormatDefault')}</> },
            { value: 'html', label: <><Code2 className="h-3.5 w-3.5" />{t('settings.outputFormatHtml')}</> },
          ]}
        />
        <ToggleRow
          {...copy(SETTINGS.subagents)}
          checked={subagentsDefault}
          onChange={() => { void write(subagentsDefaultPatch(!subagentsDefault)); }}
        />
      </SettingsGroup>

      <AutoApproveSettings />

      <SettingsGroup title={t('settings.modelTuning.sectionSearch')}>
        <SettingRow
          {...providerCopy}
          description={providers.some(([, p]) => !tierAllows(p.min_tier))
            ? <>{providerCopy.description} <span>{t('settings.searchProviderUpgradeHint')}</span></>
            : providerCopy.description}
          htmlFor={`${providerCopy.id}-select`}
          wide
        >
          <Select
            id={`${providerCopy.id}-select`}
            value={search.provider}
            onChange={(e) => { void write(searchProviderPatch(e.target.value, searchProviders)); }}
            disabled={!providers.some(([, p]) => tierAllows(p.min_tier))}
          >
            <option value="">{t('settings.searchProviderDefault')}</option>
            {providers.map(([value, p]) => (
              <option key={value} value={value} disabled={!tierAllows(p.min_tier)}>
                {p.display_name}
              </option>
            ))}
          </Select>
        </SettingRow>
        {search.depths.length > 0 && (
          <SettingRow
            label={t('settings.searchDepth')}
            description={search.depths.some((d) => !tierAllows(d.min_tier))
              ? <>{t('settings.searchDepthDesc')} <span>{t('settings.searchDepthUpgradeHint')}</span></>
              : t('settings.searchDepthDesc')}
            htmlFor={depthId}
            wide
          >
            <Select
              id={depthId}
              value={search.depth}
              onChange={(e) => { void write(searchDepthPatch(e.target.value)); }}
              disabled={!search.depths.some((d) => tierAllows(d.min_tier))}
            >
              <option value="">{t('settings.searchDepthDefault')}</option>
              {search.depths.map((d) => (
                <option key={d.name} value={d.name} disabled={!tierAllows(d.min_tier)}>
                  {t(`settings.searchDepthLevel.${d.name}`, d.display_name)}
                </option>
              ))}
            </Select>
          </SettingRow>
        )}
      </SettingsGroup>
    </>
  );
}
