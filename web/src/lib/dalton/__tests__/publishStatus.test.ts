import { describe, it, expect } from 'vitest';
import zhPublish from '@/locales/publish/zh-CN.json';
import enPublish from '@/locales/publish/en-US.json';
import { resolvePublishStatus, type PublishStatusInput } from '../publishStatus';

function makeInput(overrides: Partial<PublishStatusInput> = {}): PublishStatusInput {
  return {
    unshareOnly: false,
    standingPaused: false,
    effectiveVisibility: 'own',
    activeShare: null,
    shared: false,
    legacyRefused: false,
    membersOnly: false,
    latestSeq: 0,
    visitorsAt: 0,
    statusChanged: null,
    hasVisibilityDraft: false,
    ...overrides,
  };
}

describe('resolvePublishStatus', () => {
  it('maps each of the 4 visibility levels to accurate who-can-open copy without showing Private for shared scopes', () => {
    const own = resolvePublishStatus(makeInput({ effectiveVisibility: 'own' }));
    expect(own.showLinkIcon).toBe(false);
    expect(own.titleKey).toBe('publish:private');
    expect(own.hintKey).toBe('publish:privateHint');
    expect(zhPublish.privateHint).toContain('只有你可以打开');
    expect(enPublish.privateHint).toContain('Only you can open');

    const members = resolvePublishStatus(
      makeInput({ effectiveVisibility: 'members', hasVisibilityDraft: true }),
    );
    expect(members.showLinkIcon).toBe(true);
    expect(members.titleKey).toBe('monitors:visibility.members');
    expect(members.hintKey).toBe('publish:membersHint');
    expect(zhPublish.membersHint).toContain('你选择的同事可以打开');
    expect(enPublish.membersHint).toContain('Colleagues you select can open');

    const company = resolvePublishStatus(
      makeInput({ effectiveVisibility: 'company', hasVisibilityDraft: true }),
    );
    expect(company.showLinkIcon).toBe(true);
    expect(company.titleKey).toBe('monitors:visibility.company');
    expect(company.hintKey).toBe('publish:companyHint');
    expect(zhPublish.companyHint).toContain('团队成员可以打开');
    expect(enPublish.companyHint).toContain('Team members can open');

    const pub = resolvePublishStatus(
      makeInput({ effectiveVisibility: 'public', hasVisibilityDraft: true }),
    );
    expect(pub.showLinkIcon).toBe(true);
    expect(pub.titleKey).toBe('monitors:visibility.public');
    expect(pub.hintKey).toBe('publish:sharedHint');
    expect(zhPublish.sharedHint).toContain('拥有链接的任何人都可以打开');
    expect(enPublish.sharedHint).toContain('Anyone with the link can open');
  });

  it('keeps published version alongside visibility hint once published', () => {
    const res = resolvePublishStatus(
      makeInput({
        effectiveVisibility: 'members',
        activeShare: { visibility: 'members', published_seq: 3 },
      }),
    );
    expect(res.titleKey).toBe('monitors:visibility.members');
    expect(res.hintKey).toBe('publish:membersHint');
    expect(res.publishedVersionSeq).toBe(3);
  });
});
