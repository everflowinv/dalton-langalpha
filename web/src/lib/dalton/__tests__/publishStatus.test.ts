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
  it('shows active permission in the status row and previews unsaved visibility changes with afterSave copy', () => {
    // Private file before saving a broader visibility stays Private and previews "after saving".
    const own = resolvePublishStatus(makeInput({ effectiveVisibility: 'own' }));
    expect(own.showLinkIcon).toBe(false);
    expect(own.titleKey).toBe('publish:private');
    expect(own.hintKey).toBe('publish:privateHint');
    expect(own.pendingVisibility).toBeNull();
    expect(own.pendingHintKey).toBeNull();
    expect(zhPublish.privateHint).toContain('只有你可以打开');
    expect(enPublish.privateHint).toContain('Only you can open');

    const draftMembers = resolvePublishStatus(
      makeInput({ effectiveVisibility: 'members', hasVisibilityDraft: true }),
    );
    expect(draftMembers.showLinkIcon).toBe(false);
    expect(draftMembers.titleKey).toBe('publish:private');
    expect(draftMembers.hintKey).toBe('publish:privateHint');
    expect(draftMembers.pendingVisibility).toBe('members');
    expect(draftMembers.pendingHintKey).toBe('publish:membersHint');
    expect(zhPublish.membersHint).toContain('你选择的同事可以打开');
    expect(enPublish.membersHint).toContain('Colleagues you select can open');

    const draftCompany = resolvePublishStatus(
      makeInput({ effectiveVisibility: 'company', hasVisibilityDraft: true }),
    );
    expect(draftCompany.titleKey).toBe('publish:private');
    expect(draftCompany.pendingVisibility).toBe('company');
    expect(draftCompany.pendingHintKey).toBe('publish:companyHint');
    expect(zhPublish.companyHint).toContain('团队成员可以打开');
    expect(enPublish.companyHint).toContain('Team members can open');

    const draftPublic = resolvePublishStatus(
      makeInput({ effectiveVisibility: 'public', hasVisibilityDraft: true }),
    );
    expect(draftPublic.titleKey).toBe('publish:private');
    expect(draftPublic.pendingVisibility).toBe('public');
    expect(draftPublic.pendingHintKey).toBe('publish:sharedHint');
    expect(zhPublish.sharedHint).toContain('拥有链接的任何人都可以打开');
    expect(enPublish.sharedHint).toContain('Anyone with the link can open');
    expect(zhPublish.afterSave).toContain('保存后：');
    expect(enPublish.afterSave).toContain('After saving:');
  });

  it('keeps active public status when switching draft to Only me before submit', () => {
    const res = resolvePublishStatus(
      makeInput({
        effectiveVisibility: 'own',
        activeShare: { visibility: 'public', published_seq: 2 },
        shared: true,
        hasVisibilityDraft: true,
      }),
    );
    expect(res.showLinkIcon).toBe(true);
    expect(res.titleKey).toBe('monitors:visibility.public');
    expect(res.hintKey).toBe('publish:sharedHint');
    expect(res.publishedVersionSeq).toBe(2);
    expect(res.pendingVisibility).toBe('own');
    expect(res.pendingHintKey).toBe('publish:privateHint');
  });

  it('keeps legacy upstream shared status active when switching draft to Only me before submit', () => {
    const res = resolvePublishStatus(
      makeInput({
        effectiveVisibility: 'own',
        activeShare: null,
        shared: true,
        visitorsAt: 4,
        hasVisibilityDraft: true,
      }),
    );
    expect(res.showLinkIcon).toBe(true);
    expect(res.titleKey).toBe('publish:publishedAt');
    expect(res.titleParams).toEqual({ seq: 4 });
    expect(res.hintKey).toBe('publish:sharedHint');
    expect(res.pendingVisibility).toBe('own');
    expect(res.pendingHintKey).toBe('publish:privateHint');
  });

  it('clears pending preview once selection matches active published visibility', () => {
    const res = resolvePublishStatus(
      makeInput({
        effectiveVisibility: 'members',
        activeShare: { visibility: 'members', published_seq: 3 },
      }),
    );
    expect(res.titleKey).toBe('monitors:visibility.members');
    expect(res.hintKey).toBe('publish:membersHint');
    expect(res.publishedVersionSeq).toBe(3);
    expect(res.pendingVisibility).toBeNull();
    expect(res.pendingHintKey).toBeNull();
  });
});
