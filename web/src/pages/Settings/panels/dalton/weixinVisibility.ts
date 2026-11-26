import { hasRequiredCapability } from '@/hooks/useCapabilities';
import type { DaltonSettingVisibilityContext } from './DaltonCosSharingCard';
import type { DaltonWeixinMemberAccountView } from '@/pages/Settings/utils/daltonApi';

/**
 * Determine whether external members are allowed to see the personal WeChat entry
 * from `GET /api/v1/dalton/channels/bindings`'s `weixin_account` payload.
 *
 * Uses the explicit `allow_external` flag returned by backend (which remains
 * accurate even when BOTH `channel_enabled` and `allow_external` are off and
 * `deny_reason` is `weixin_channel_disabled`), falling back to `deny_reason`
 * only for older payloads without `allow_external`.
 */
export function resolveWeixinAllowExternal(
  wx: DaltonWeixinMemberAccountView | null | undefined,
): boolean | null {
  if (!wx) return null;
  if (typeof wx.allow_external === 'boolean') {
    return wx.allow_external && wx.deny_reason !== 'external_access_disabled';
  }
  return wx.deny_reason !== 'external_access_disabled';
}

/**
 * Personal WeChat card & search visibility predicate (fail-closed):
 * - Hidden when `channel_weixin` capability is disabled.
 * - Hidden while caller identity tier is unresolved (loading, error, or unknown).
 * - Shown for confirmed `admin` / `internal` members.
 * - Shown for confirmed `external` members only when `weixinAllowExternal === true`.
 */
export function canShowDaltonWeixinBindCard(
  ctx?: DaltonSettingVisibilityContext | null,
): boolean {
  if (!ctx) return false;
  if (!hasRequiredCapability(ctx.capabilities, 'channel_weixin')) return false;
  if (ctx.callerTierPending || ctx.callerTierError) return false;
  if (ctx.callerTier === 'admin' || ctx.callerTier === 'internal') {
    return true;
  }
  if (ctx.callerTier === 'external') {
    return ctx.weixinAllowExternal === true;
  }
  return false;
}
