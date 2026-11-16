import { useDaltonSettingsVisibility } from '../../components/SettingsRail';
import { DaltonInvitesCard, isDaltonInvitesSettingVisible } from './DaltonInvitesCard';
import { FeishuReadScopesCard, isDaltonFeishuScopesSettingVisible } from './FeishuReadScopesCard';

/** Team Management -> Members & Access panel (invites for internal+, Feishu scopes for admin). */
export function DaltonTeamMembersPanel() {
  const ctx = useDaltonSettingsVisibility();

  return (
    <div className="space-y-8">
      {isDaltonInvitesSettingVisible(ctx) && <DaltonInvitesCard ctx={ctx} />}
      {isDaltonFeishuScopesSettingVisible(ctx) && <FeishuReadScopesCard ctx={ctx} />}
    </div>
  );
}
