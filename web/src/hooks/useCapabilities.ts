import { useContext, useMemo } from 'react';
import { QueryClient, QueryClientContext, useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import {
  BRAND_NAME,
  DALTON_PROFILE,
  isCoreProfileBuild,
  type DaltonProfile,
} from '@/config/brand';
import { queryKeys } from '@/lib/queryKeys';

/**
 * Capability identifiers published by `GET /api/v1/dalton/capabilities`
 * (mirrors `src/dalton/capabilities.py:CapabilityId`).
 */
export const CAPABILITY_IDS = [
  'researchdata_bridge',
  'alphaengine',
  'guidepoint',
  'feishu_docs',
  'channel_gateway',
  'channel_feishu',
  'channel_discord',
  'channel_weixin',
  'data_jobs',
  'conversation_archive',
  'native_oauth_sync',
  'transcription',
  'colleague_sharing',
] as const;

export type CapabilityId = (typeof CAPABILITY_IDS)[number];

/**
 * Everflow build default capability set (all optional capabilities except
 * opt-in `transcription`). Used on first paint in Everflow builds so existing
 * Everflow views render immediately with zero flicker or extra wait.
 */
export const EVERFLOW_DEFAULT_CAPABILITIES: readonly CapabilityId[] = [
  'researchdata_bridge',
  'alphaengine',
  'guidepoint',
  'feishu_docs',
  'channel_gateway',
  'channel_feishu',
  'channel_discord',
  'channel_weixin',
  'data_jobs',
  'conversation_archive',
  'native_oauth_sync',
  'colleague_sharing',
];

/**
 * Core build default capability set (all optional capabilities disabled until
 * `/api/v1/dalton/capabilities` confirms otherwise).
 */
export const CORE_DEFAULT_CAPABILITIES: readonly CapabilityId[] = [];

export const EVERFLOW_DEFAULT_CAPABILITY_SET: ReadonlySet<CapabilityId> = new Set(
  EVERFLOW_DEFAULT_CAPABILITIES,
);
export const CORE_DEFAULT_CAPABILITY_SET: ReadonlySet<CapabilityId> = new Set(
  CORE_DEFAULT_CAPABILITIES,
);

export function getBuildDefaultCapabilitySet(): ReadonlySet<CapabilityId> {
  return isCoreProfileBuild ? CORE_DEFAULT_CAPABILITY_SET : EVERFLOW_DEFAULT_CAPABILITY_SET;
}

export type CapabilityRequirement = CapabilityId | readonly CapabilityId[] | null | undefined;

/**
 * Evaluate whether `capabilities` satisfies a single capability or an OR-list
 * of capabilities (any one enabled satisfies an array requirement).
 */
export function hasRequiredCapability(
  capabilities: ReadonlySet<CapabilityId> | null | undefined,
  required: CapabilityRequirement,
): boolean {
  if (!required) return true;
  const active = capabilities ?? getBuildDefaultCapabilitySet();
  if (typeof required === 'string') {
    return active.has(required);
  }
  if (required.length === 0) return true;
  return required.some((cap) => active.has(cap));
}

export interface CapabilitiesApiResponse {
  profile: DaltonProfile;
  capabilities: CapabilityId[];
  fingerprint?: string;
  brand_name?: string;
  brand?: {
    name?: string;
  };
}

export async function fetchDaltonCapabilities(): Promise<CapabilitiesApiResponse> {
  const { data } = await api.get<CapabilitiesApiResponse>('/api/v1/dalton/capabilities');
  return data;
}

export interface UseCapabilitiesResult {
  profile: DaltonProfile;
  capabilities: ReadonlySet<CapabilityId>;
  hasCapability: (required: CapabilityRequirement) => boolean;
  brandName: string;
  fingerprint: string | null;
  /**
   * True only during initial load of a Core profile build so Core UI can render
   * a lightweight placeholder while avoiding any flash of proprietary cards.
   * Always false on first paint in Everflow builds.
   */
  isLoading: boolean;
  isLoaded: boolean;
  isCoreBuild: boolean;
  isEverflowBuild: boolean;
}

const FALLBACK_QUERY_CLIENT = new QueryClient({
  defaultOptions: {
    queries: {
      enabled: false,
      retry: false,
    },
  },
});

export function useCapabilities(options: { enabled?: boolean } = {}): UseCapabilitiesResult {
  const { enabled = true } = options;
  const contextClient = useContext(QueryClientContext);
  const queryEnabled = enabled && contextClient !== undefined;

  const query = useQuery<CapabilitiesApiResponse>(
    {
      queryKey: queryKeys.dalton.capabilities(),
      queryFn: fetchDaltonCapabilities,
      enabled: queryEnabled,
      staleTime: 5 * 60_000,
      retry: 1,
    },
    contextClient ?? FALLBACK_QUERY_CLIENT,
  );

  const capabilities = useMemo<ReadonlySet<CapabilityId>>(() => {
    if (query.data?.capabilities) {
      return new Set(query.data.capabilities);
    }
    return getBuildDefaultCapabilitySet();
  }, [query.data?.capabilities]);

  const profile: DaltonProfile = query.data?.profile ?? DALTON_PROFILE;
  const brandName =
    query.data?.brand_name?.trim() ||
    query.data?.brand?.name?.trim() ||
    BRAND_NAME;

  const isLoading = isCoreProfileBuild && queryEnabled && query.isPending;
  const isLoaded = query.data !== undefined;

  const hasCapability = useMemo(
    () => (required: CapabilityRequirement) => hasRequiredCapability(capabilities, required),
    [capabilities],
  );

  return {
    profile,
    capabilities,
    hasCapability,
    brandName,
    fingerprint: query.data?.fingerprint ?? null,
    isLoading,
    isLoaded,
    isCoreBuild: isCoreProfileBuild,
    isEverflowBuild: !isCoreProfileBuild,
  };
}
