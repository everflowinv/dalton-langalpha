import { useQuery } from '@tanstack/react-query';
import type { CapabilityRequirement } from '@/hooks/useCapabilities';
import { queryKeys } from '@/lib/queryKeys';
import { fetchDaltonChannelBindings } from '@/pages/Settings/utils/daltonApi';
import { resolveWeixinAllowExternal } from './weixinVisibility';

export { canShowDaltonWeixinBindCard, resolveWeixinAllowExternal } from './weixinVisibility';

/**
 * Resolve `weixinAllowExternal` for Settings search, CommandPalette, and card
 * visibility. Queries `/channels/bindings` only when the caller is a verified
 * external member and `channel_weixin` is enabled; fails closed (`false`) while
 * loading or on error so the entry never flashes for disallowed external users.
 */
export function useWeixinExternalVisibility(
  isExternal: boolean,
  hasCapability: (required: CapabilityRequirement) => boolean,
): boolean {
  const canWeixin = hasCapability('channel_weixin');
  const query = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: isExternal && canWeixin,
    retry: false,
  });

  if (!isExternal) {
    return true;
  }
  if (!canWeixin || query.isLoading || query.isError) {
    return false;
  }
  return resolveWeixinAllowExternal(query.data?.weixin_account) === true;
}
