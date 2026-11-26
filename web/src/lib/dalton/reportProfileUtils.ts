import type { ReportProfileConfig } from '@/types/automation';

export function extractReportProfile(metadata: Record<string, unknown> | null | undefined): ReportProfileConfig | null {
  if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) return null;
  const raw = metadata.report_profile;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const rec = raw as Record<string, unknown>;
  const profile = typeof rec.profile === 'string' ? rec.profile.trim() : '';
  if (!profile) return null;
  const toList = (v: unknown): string[] =>
    Array.isArray(v)
      ? v.filter((item): item is string => typeof item === 'string' && item.trim().length > 0).map((s) => s.trim())
      : [];
  const report_targets = toList(rec.report_targets);
  const failure_targets = toList(rec.failure_targets);
  const text_targets = rec.text_targets === undefined || rec.text_targets === null ? undefined : toList(rec.text_targets);
  return {
    profile,
    report_targets,
    failure_targets,
    ...(text_targets !== undefined ? { text_targets } : {}),
  };
}

function canonicalProfileJson(profile: ReportProfileConfig | null): string | null {
  if (!profile) return null;
  return JSON.stringify({
    profile: profile.profile,
    report_targets: [...profile.report_targets].sort(),
    text_targets: profile.text_targets === undefined || profile.text_targets === null ? null : [...profile.text_targets].sort(),
    failure_targets: [...profile.failure_targets].sort(),
  });
}

export function reportProfilesEqual(a: ReportProfileConfig | null, b: ReportProfileConfig | null): boolean {
  return canonicalProfileJson(a) === canonicalProfileJson(b);
}

/**
 * Return the ``report_profile`` needing server confirmation precheck when a
 * create or edit payload sets or modifies ``metadata.report_profile``.
 */
export function changedReportProfileForSubmit(
  kind: 'create' | 'edit',
  payloadMetadata: Record<string, unknown> | null | undefined,
  originalMetadata: Record<string, unknown> | null | undefined,
): ReportProfileConfig | null {
  if (!payloadMetadata || typeof payloadMetadata !== 'object' || !('report_profile' in payloadMetadata)) {
    return null;
  }
  const nextProfile = extractReportProfile(payloadMetadata);
  if (!nextProfile) return null;
  if (kind === 'create') return nextProfile;
  const prevProfile = extractReportProfile(originalMetadata);
  return reportProfilesEqual(nextProfile, prevProfile) ? null : nextProfile;
}
