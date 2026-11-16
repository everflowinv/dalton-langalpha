import React, { type ReactElement, type ReactNode } from 'react';
import { render, renderHook, type RenderOptions } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
// DALTON-HOOK(monitors): provide default AuthContext so components calling useTeamCredentials() work in tests
import * as authModule from '@/contexts/AuthContext';

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

function createWrapper(queryClient: QueryClient, route = '/') {
  return function Wrapper({ children }: { children: ReactNode }) {
    const inner = (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>
          {children}
        </MemoryRouter>
      </QueryClientProvider>
    );
    // A test may mock '@/contexts/AuthContext' without these exports, and a
    // vitest module mock throws on reading a missing export, so probe with `in`.
    if ('AuthContext' in authModule && '_localDevValue' in authModule) {
      const { AuthContext, _localDevValue } = authModule;
      return <AuthContext value={_localDevValue}>{inner}</AuthContext>;
    }
    return inner;
  };
}

interface RenderWithProvidersOptions extends Omit<RenderOptions, 'wrapper'> {
  route?: string;
  queryClient?: QueryClient;
}

export function renderWithProviders(ui: ReactElement, { route = '/', queryClient, ...options }: RenderWithProvidersOptions = {}) {
  const client = queryClient || createTestQueryClient();
  const Wrapper = createWrapper(client, route);
  return { ...render(ui, { wrapper: Wrapper, ...options }), queryClient: client };
}

export function renderHookWithProviders<TResult>(hook: () => TResult, { route = '/', queryClient, ...options }: RenderWithProvidersOptions = {}) {
  const client = queryClient || createTestQueryClient();
  const wrapper = createWrapper(client, route);
  return { ...renderHook(hook, { wrapper, ...options }), queryClient: client };
}

export { createTestQueryClient };
