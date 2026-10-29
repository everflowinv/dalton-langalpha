// @vitest-environment node
/**
 * What a hand-off says about itself: the glyph each phase takes in a group's
 * column, and how a proposal, its place and its run's read come together as
 * one row's state.
 */
import { describe, it, expect } from 'vitest';
import type { PTCAgentProposalState } from '@/types/chat';
import type { DispatchReadStatus, DispatchStatusView } from '../../../hooks/usePTCDispatchStatus';
import { columnGlyph, describeHandoff, HANDOFF_PHASES, handoffTally, type HandoffPhase } from '../handoffModel';

describe('columnGlyph', () => {
  it.each<[HandoffPhase, string]>([
    ['starting', 'pending'],
    ['working', 'live'],
    ['needs_input', 'ring'],
    ['failed', 'alert'],
    ['stopped', 'stop'],
    ['not_started', 'alert'],
  ])('gives a %s run the glyph it has beside its words, %s', (phase, glyph) => {
    expect(HANDOFF_PHASES[phase].glyph).toBe(glyph);
    expect(columnGlyph(phase)).toBe(glyph);
  });

  it('fills the place of a finished run, which has none beside its words, with done', () => {
    expect(HANDOFF_PHASES.finished.glyph).toBeNull();
    expect(columnGlyph('finished')).toBe('done');
    expect(columnGlyph('finished', false)).toBe('done');
  });

  // DALTON-HOOK(handoff-report-quiescence): not done while its report says
  // work is still open — the ring its unfinished news wears elsewhere.
  it('keeps the ring for a finished run whose report says work is open', () => {
    expect(columnGlyph('finished', true)).toBe('ring');
  });

  it('leaves no phase without a glyph', () => {
    for (const phase of Object.keys(HANDOFF_PHASES) as HandoffPhase[]) {
      expect(columnGlyph(phase)).toBeTruthy();
    }
  });
});

// DALTON-HOOK(handoff-report-quiescence): a wait-cap report is tallied
// apart — reported back with work still open, never inside the finished.
describe('handoffTally', () => {
  const t = (key: string, opts?: Record<string, unknown>) =>
    opts && typeof opts.count === 'number' ? `${key}:${opts.count}` : key;

  it('counts an unfinished report beside, never inside, the finished', () => {
    const { parts } = handoffTally([
      { phase: 'finished' },
      { phase: 'finished', unfinished: true },
      { phase: 'working' },
    ], t);
    expect(parts.map((part) => part.text)).toEqual([
      'chat.ptcCard.summaryWorking:1',
      'chat.ptcCard.summaryFinished:1',
      'chat.ptcCard.summaryUnfinished:1',
    ]);
  });

  it('keeps an all-unfinished group out of the finished count entirely, its words uncut', () => {
    const { parts } = handoffTally([
      { phase: 'finished', unfinished: true },
      { phase: 'finished', unfinished: true },
    ], t);
    expect(parts.map((part) => part.text)).toEqual(['chat.ptcCard.summaryUnfinished:2']);
    expect(parts[0].keep).toBe(true);
  });

  it('adds starting and working together, since both read as working', () => {
    const { parts } = handoffTally([{ phase: 'starting' }, { phase: 'working' }], t);
    expect(parts.map((part) => part.text)).toEqual(['chat.ptcCard.summaryWorking:2']);
  });

  it('counts finished work as before when no report is unfinished', () => {
    const { parts } = handoffTally([{ phase: 'finished' }, { phase: 'finished' }], t);
    expect(parts.map((part) => part.text)).toEqual(['chat.ptcCard.summaryFinished:2']);
  });
});

describe('describeHandoff', () => {
  const PROPOSAL: PTCAgentProposalState = {
    status: 'approved',
    question: 'Is NVDA overvalued vs AMD?',
    workspace_name: 'Semis',
    thread_id: 'th-1',
    interruptId: 'i-1',
  };
  const read = (status: DispatchReadStatus): DispatchStatusView => ({ status, read: true });

  describe('steered', () => {
    it('is set for a hand-off sent into a run of the analyst\'s already going', () => {
      expect(describeHandoff({ ...PROPOSAL, steer_id: 'steer-1', run_id: 'run-1' }, {}, read('running')).steered).toBe(true);
    });

    it('is not set for a steer no run ever took in, which was sent into nothing', () => {
      expect(describeHandoff({ ...PROPOSAL, steer_id: 'steer-1', run_id: 'run-1' }, {}, read('dropped')).steered).toBe(false);
    });

    it('is not set for one that started a run of its own', () => {
      expect(describeHandoff({ ...PROPOSAL, run_id: 'run-1' }, {}, read('running')).steered).toBe(false);
      expect(describeHandoff(PROPOSAL, {}, read('running')).steered).toBe(false);
    });
  });

  describe('reportId', () => {
    it.each<[DispatchReadStatus, HandoffPhase]>([
      ['starting', 'starting'],
      ['running', 'working'],
      ['needs_input', 'needs_input'],
    ])('is left out while the run still moves (%s), though the place has one', (status, phase) => {
      const state = describeHandoff(PROPOSAL, { reportId: 'msg-report' }, read(status));
      expect(state.phase).toBe(phase);
      expect(state.reportId).toBeUndefined();
    });

    it.each<[DispatchReadStatus, HandoffPhase]>([
      ['completed', 'finished'],
      ['failed', 'failed'],
      ['stopped', 'stopped'],
    ])('answers a run that ended, however it ended (%s)', (status, phase) => {
      const state = describeHandoff(PROPOSAL, { reportId: 'msg-report' }, read(status));
      expect(state.phase).toBe(phase);
      expect(state.reportId).toBe('msg-report');
    });

    it('is absent for a run that ended with no report in the conversation yet', () => {
      expect(describeHandoff(PROPOSAL, {}, read('completed')).reportId).toBeUndefined();
    });
  });

  it('reads a completed run as finished whether or not its report is in', () => {
    expect(describeHandoff(PROPOSAL, {}, read('completed')).phase).toBe('finished');
    expect(describeHandoff(PROPOSAL, { reportId: 'msg-report' }, read('completed')).phase).toBe('finished');
  });

  // DALTON-HOOK(handoff-report-quiescence): the wait cap's report says the
  // work is unfinished, and the finished phase must carry that so its words
  // never read as finished work.
  describe('unfinished', () => {
    it('is set for a report that says background work is still open', () => {
      const state = describeHandoff(
        PROPOSAL,
        { reportId: 'msg-report', outcome: 'completed', unfinished: true },
        read('completed'),
      );
      expect(state.phase).toBe('finished');
      expect(state.unfinished).toBe(true);
    });

    it('is left out for a report that finished its work, and with no report at all', () => {
      expect(
        describeHandoff(PROPOSAL, { reportId: 'msg-report', outcome: 'completed' }, read('completed')).unfinished,
      ).toBeUndefined();
      expect(
        describeHandoff(PROPOSAL, {}, read('completed')).unfinished,
      ).toBeUndefined();
    });
  });
});
