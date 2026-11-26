/**
 * Cross-session team model catalog revision tracker.
 *
 * Gated by the explicit server capability flag (`team_catalog_enabled: true`
 * returned by `GET /api/v1/models`, or any 2xx response from `/api/v1/dalton/team-models`).
 * When active, tracks the highest `X-Dalton-Catalog-Revision` integer observed
 * from REST and SSE responses and invalidates model/setup queries as soon as a
 * higher revision arrives, or when a Dalton-catalog 400 (`model_removed` /
 * `model_retired` / `catalog_route_changed`) error is returned.
 *
 * Module-scoped (like `accountSuspension.ts`) so `api/client.ts` and SSE
 * `transport.ts` can report headers without pulling React into the entry bundle.
 */
import type { QueryClient } from '@tanstack/react-query';
import { registerAuthReset } from '@/lib/authResets';
import { queryKeys } from '@/lib/queryKeys';

export const CATALOG_REVISION_HEADER = 'x-dalton-catalog-revision';

// Intentionally excludes upstream `model_unavailable` (used by org Claude Code
// policies and non-catalog checks) so generic REST calls never trigger catalog
// refreshes on upstream 400s.
const INVALIDATING_MODEL_ERROR_CODES = new Set([
  'model_removed',
  'model_retired',
  'catalog_route_changed',
]);

let teamCatalogEnabled = false;
let highestObservedRevision: number | null = null;
const attachedClients = new Map<QueryClient, number>();

/** Set whether the connected backend advertises `team_catalog_enabled`. */
export function setTeamCatalogEnabled(enabled: boolean): void {
  teamCatalogEnabled = Boolean(enabled);
}

/** Whether the connected backend explicitly enabled the team model catalog. */
export function isTeamCatalogEnabled(): boolean {
  return teamCatalogEnabled;
}

/** Attach a QueryClient to receive catalog-revision invalidations (ref-counted). Returns an unsubscribe fn. */
export function attachCatalogRevisionQueryClient(queryClient: QueryClient): () => void {
  attachedClients.set(queryClient, (attachedClients.get(queryClient) ?? 0) + 1);
  let released = false;
  return () => {
    if (released) return;
    released = true;
    const next = (attachedClients.get(queryClient) ?? 0) - 1;
    if (next <= 0) {
      attachedClients.delete(queryClient);
    } else {
      attachedClients.set(queryClient, next);
    }
  };
}

/** Return the highest catalog revision seen so far in this session (null before first header). */
export function getObservedCatalogRevision(): number | null {
  return highestObservedRevision;
}

/** Invalidate all model catalog & setup availability queries on every attached QueryClient. */
export function invalidateCatalogQueries(options?: { includeTeamCatalog?: boolean }): void {
  const includeTeamCatalog = options?.includeTeamCatalog ?? true;
  for (const qc of attachedClients.keys()) {
    void qc.invalidateQueries({ queryKey: queryKeys.models.all });
    void qc.invalidateQueries({ queryKey: queryKeys.providerModels.all });
    void qc.invalidateQueries({ queryKey: [...queryKeys.dalton.all, 'team-models-available'] });
    if (includeTeamCatalog) {
      void qc.invalidateQueries({ queryKey: queryKeys.dalton.teamModelCatalog() });
    }
  }
}

function parseRevisionHeader(
  headers: Record<string, string> | Headers | null | undefined,
): number | null {
  if (!headers) return null;
  let raw: string | null | undefined;
  if (typeof Headers !== 'undefined' && headers instanceof Headers) {
    raw = headers.get(CATALOG_REVISION_HEADER);
  } else {
    const record = headers as Record<string, string>;
    raw = record[CATALOG_REVISION_HEADER] ?? record['X-Dalton-Catalog-Revision'];
  }
  if (raw == null || raw === '') return null;
  const parsed = Number.parseInt(String(raw).trim(), 10);
  if (!Number.isSafeInteger(parsed) || parsed < 0) return null;
  return parsed;
}

function extractPathname(url?: string): string {
  if (!url) return '';
  try {
    return /^https?:\/\//i.test(url) ? new URL(url).pathname : url.split('?')[0];
  } catch {
    return '';
  }
}

function isModelsListEndpoint(url?: string): boolean {
  return extractPathname(url) === '/api/v1/models';
}

function isTeamCatalogAdminEndpoint(url?: string): boolean {
  const pathname = extractPathname(url);
  return pathname === '/api/v1/dalton/team-models' || pathname.startsWith('/api/v1/dalton/team-models/');
}

/**
 * Update the explicit server capability flag from a REST response before header
 * or error observation runs.
 */
export function syncTeamCatalogCapabilityFromResponse(
  url: string | undefined,
  status: number,
  data: unknown,
): void {
  if (status < 200 || status >= 300) return;
  if (isModelsListEndpoint(url) && data && typeof data === 'object') {
    const flag = (data as { team_catalog_enabled?: unknown }).team_catalog_enabled;
    setTeamCatalogEnabled(flag === true);
    return;
  }
  if (isTeamCatalogAdminEndpoint(url)) {
    setTeamCatalogEnabled(true);
  }
}

/**
 * Observe `X-Dalton-Catalog-Revision` on any API or SSE response.
 *
 * No-op unless the server explicitly enabled the team model catalog.
 * The very first observed revision initializes the baseline without triggering
 * a redundant refetch. Any strictly higher revision triggers immediate
 * invalidation of cached model queries (skipping redundant invalidation when
 * the response itself came from `GET /api/v1/models`, which is already writing
 * fresh data into `queryKeys.models.all`).
 */
export function observeCatalogRevisionHeader(
  headers: Record<string, string> | Headers | null | undefined,
  options?: { url?: string },
): boolean {
  if (!teamCatalogEnabled) return false;
  const rev = parseRevisionHeader(headers);
  if (rev === null) return false;

  if (highestObservedRevision === null) {
    highestObservedRevision = rev;
    return false;
  }

  if (rev > highestObservedRevision) {
    highestObservedRevision = rev;
    if (!isModelsListEndpoint(options?.url)) {
      invalidateCatalogQueries();
    } else {
      // Models list itself is updating; still refresh dependent team-models availability caches.
      for (const qc of attachedClients.keys()) {
        void qc.invalidateQueries({ queryKey: [...queryKeys.dalton.all, 'team-models-available'] });
      }
    }
    return true;
  }

  return false;
}

/**
 * Observe a 400 error payload; when team catalog is enabled and the error
 * indicates a removed, retired, or route-changed catalog model, immediately
 * invalidate model caches.
 */
export function observeCatalogModelError(status: number | undefined, detail: unknown): boolean {
  if (!teamCatalogEnabled) return false;
  if (status !== 400 || !detail || typeof detail !== 'object') return false;
  const d = detail as Record<string, unknown>;
  const code =
    typeof d.code === 'string'
      ? d.code
      : typeof d.type === 'string'
        ? d.type
        : typeof d.error_type === 'string'
          ? d.error_type
          : null;
  if (code && INVALIDATING_MODEL_ERROR_CODES.has(code)) {
    invalidateCatalogQueries();
    return true;
  }
  return false;
}

/** Reset session revision state (called on sign-out/account switch and in unit tests). */
export function resetCatalogRevisionTracker(options?: { clearClients?: boolean }): void {
  teamCatalogEnabled = false;
  highestObservedRevision = null;
  if (options?.clearClients) {
    attachedClients.clear();
  }
}

registerAuthReset(() => resetCatalogRevisionTracker());
