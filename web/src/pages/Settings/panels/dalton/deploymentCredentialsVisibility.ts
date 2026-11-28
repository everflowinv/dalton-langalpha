import { hasRequiredCapability } from '@/hooks/useCapabilities';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';

/**
 * Append channel-specific search terms to the Deployment Credentials entry strictly
 * when the corresponding capability is active so Core default search stays neutral.
 */
export function buildDeploymentCredentialsSearchKeywords(
  baseKeywords: string,
  ctx?: DaltonSettingVisibilityContext | null,
): string {
  const extra: string[] = [];
  if (hasRequiredCapability(ctx?.capabilities, 'channel_feishu')) {
    extra.push('飞书 feishu');
  }
  if (hasRequiredCapability(ctx?.capabilities, 'channel_discord')) {
    extra.push('Discord discord');
  }
  if (hasRequiredCapability(ctx?.capabilities, 'channel_weixin')) {
    extra.push('微信 weixin wechat');
  }
  return extra.length > 0 ? `${baseKeywords} ${extra.join(' ')}`.trim() : baseKeywords;
}
