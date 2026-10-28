/**
 * A key the deployment supplies from its environment has no row of the user's,
 * so the form never offers to move its base URL without a key of their own:
 * the save would land on nothing and the URL would stay where it was. A key
 * the user saved stays when only the URL changes, a local server's included.
 */
import { describe, expect, it, vi } from 'vitest';
import { act } from '@testing-library/react';
import { renderHookWithProviders } from '@/test/utils';
import type { ProviderCatalogEntry } from '@/components/model/types';
import type { ConfiguredProvider } from '@/hooks/useConfiguredProviders';
import type { KeyOption } from '../providerOptions';

const h = vi.hoisted(() => ({ mutateAsync: vi.fn(async (_payload: unknown) => ({})) }));

vi.mock('@/hooks/useApiKeys', () => ({ useUpdateApiKeys: () => ({ mutateAsync: h.mutateAsync }) }));
vi.mock('@/hooks/useModelAccessActions', () => ({ useRefreshAfterAccessChange: () => vi.fn() }));

import { useApiKeyForm } from '../ApiKeyForm';

const option = {
  entry: { provider: 'deepseek', display_name: 'DeepSeek', access_type: 'api_key' } as ProviderCatalogEntry,
  provider: 'deepseek',
  plan: 'standard',
  region: 'global',
  base_url: 'https://api.deepseek.example',
  sdk: 'openai',
  use_response_api: false,
} as unknown as KeyOption;

const saved = (masked_key: string): ConfiguredProvider => ({
  provider: 'deepseek', display_name: 'DeepSeek', access_type: 'api_key', masked_key,
});

function movedUrl(configured: ConfiguredProvider[]) {
  const { result } = renderHookWithProviders(() => useApiKeyForm(option, configured));
  act(() => result.current.setBaseUrl('https://gateway.example/v1'));
  return result.current;
}

describe('useApiKeyForm', () => {
  it('moves the base URL of a key the user saved without asking for it again', () => {
    expect(movedUrl([saved('sk-...7e48')]).canSave).toBe(true);
  });

  it('asks for a key before moving the URL of the deployment\'s own key', () => {
    const form = movedUrl([saved('(server)')]);
    expect(form.canSave).toBe(false);
    expect(form.ownKey).toBe(false);
  });

  it("keeps a local server's saved key when only its URL changes", async () => {
    const local = {
      ...option,
      entry: { provider: 'vllm', display_name: 'vLLM', access_type: 'local' } as ProviderCatalogEntry,
      provider: 'vllm',
      base_url: 'http://localhost:8000/v1',
    } as unknown as KeyOption;
    const savedLocal: ConfiguredProvider = {
      provider: 'vllm', display_name: 'vLLM', access_type: 'local', masked_key: 'tok...9f2a',
      base_url: 'http://localhost:8000/v1',
    };
    const { result } = renderHookWithProviders(() => useApiKeyForm(local, [savedLocal]));
    act(() => result.current.setBaseUrl('http://gpu-box:8000/v1'));
    await act(async () => { await result.current.save(); });

    expect(h.mutateAsync).toHaveBeenCalledWith({
      byok_enabled: true,
      base_urls: { vllm: 'http://gpu-box:8000/v1' },
    });
  });

  it("writes the user's own row when moving the URL of the deployment's local server", async () => {
    h.mutateAsync.mockClear();
    const local = {
      ...option,
      entry: { provider: 'vllm', display_name: 'vLLM', access_type: 'local' } as ProviderCatalogEntry,
      provider: 'vllm',
      base_url: 'http://localhost:8000/v1',
    } as unknown as KeyOption;
    const served: ConfiguredProvider = {
      provider: 'vllm', display_name: 'vLLM', access_type: 'local', masked_key: '(server)',
      base_url: 'http://localhost:8000/v1',
    };
    const { result } = renderHookWithProviders(() => useApiKeyForm(local, [served]));
    act(() => result.current.setBaseUrl('http://gpu-box:8000/v1'));
    await act(async () => { await result.current.save(); });

    expect(h.mutateAsync).toHaveBeenCalledWith({
      byok_enabled: true,
      api_keys: { vllm: '' },
      base_urls: { vllm: 'http://gpu-box:8000/v1' },
    });
  });

  it('saves a new key at the custom URL that arrives after the form opened', async () => {
    h.mutateAsync.mockClear();
    let configured: ConfiguredProvider[] = [];
    const { result, rerender } = renderHookWithProviders(() => useApiKeyForm(option, configured));
    configured = [{ ...saved('sk-...7e48'), base_url: 'https://gateway.example/v1' }];
    rerender();
    act(() => result.current.setApiKey('sk-new'));
    await act(async () => { await result.current.save(); });

    expect(h.mutateAsync).toHaveBeenCalledWith({
      byok_enabled: true,
      api_keys: { deepseek: 'sk-new' },
      base_urls: { deepseek: 'https://gateway.example/v1' },
    });
  });
});
