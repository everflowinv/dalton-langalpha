import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryKeys';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import {
  getAntigravityOAuthStatus,
  getMuseOAuthStatus,
  getXaiOAuthStatus,
} from '../utils/daltonOAuthApi';

export function useDaltonOAuthProviders() {
  const { data: antigravityStatus, isLoading: antigravityLoading } = useQuery({
    queryKey: queryKeys.oauth.antigravity(),
    queryFn: getAntigravityOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: museStatus, isLoading: museLoading } = useQuery({
    queryKey: queryKeys.oauth.muse(),
    queryFn: getMuseOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const { data: xaiStatus, isLoading: xaiLoading } = useQuery({
    queryKey: queryKeys.oauth.xai(),
    queryFn: getXaiOAuthStatus,
    staleTime: 60_000,
    retry: false,
  });

  const extraProviders = useMemo<ConfiguredProvider[]>(() => {
    const list: ConfiguredProvider[] = [];
    if (antigravityStatus?.connected) {
      list.push({
        provider: 'antigravity-oauth',
        display_name: 'Google Antigravity',
        access_type: 'oauth',
        plan_type: antigravityStatus.plan_type ?? null,
      });
    }
    if (museStatus?.connected) {
      list.push({
        provider: 'muse-oauth',
        display_name: 'Muse Code (Meta)',
        access_type: 'oauth',
        plan_type: museStatus.plan_type ?? null,
      });
    }
    if (xaiStatus?.connected) {
      list.push({
        provider: 'xai-oauth',
        display_name: 'xAI Grok (SuperGrok)',
        access_type: 'oauth',
        plan_type: xaiStatus.plan_type ?? null,
      });
    }
    return list;
  }, [antigravityStatus, museStatus, xaiStatus]);

  return {
    extraProviders,
    isLoading: antigravityLoading || museLoading || xaiLoading,
  };
}
