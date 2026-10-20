import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import i18n from '@/i18n';
import { PublishApprovalCard } from '../PublishApprovalCard';
import type { ToolApprovalState } from '@/types/chat';

describe('PublishApprovalCard', () => {
  it('renders 4-category figure_summary counts, uncertain_items list, reviewer summary, and labeling_incomplete banner from English keys', async () => {
    await i18n.changeLanguage('zh-CN');
    const onApprove = vi.fn();
    const onReject = vi.fn();

    const state: ToolApprovalState = {
      status: 'pending',
      toolName: 'mcp__dalton-publish__publish_report',
      server: 'dalton-publish',
      tool: 'publish_report',
      actionIndex: 0,
      actionCount: 1,
      args: {
        request_id: 'req-abc1234567890',
        ticker: 'META',
        title: 'META 3Q26 深度前瞻',
        kind: 'earnings',
        draft_path: 'drafts/meta-3q26.md',
        published_path: 'published/2026-10-18-meta-3q26.md',
        labeling_incomplete: true,
        figure_summary: {
          verified: 42,
          contradicted: 0,
          unverified: 0,
          uncertain: 2,
          exempt: 3,
          labeling_incomplete: true,
          uncertain_items: [
            {
              id: 'F-010',
              raw: '88.8%',
              line: 18,
              metric: '北美广告留存率',
              period: '3Q26',
              sentence: '3Q26 北美广告留存率达 88.8%。',
              hint: '未能判断（llm_timeout）— 交由 PM 决定',
            },
          ],
        },
        reviewer: {
          model: 'gpt-6.1-sol-oauth',
          family: 'openai-gpt',
          verdict: 'pass',
          summary: '论证扎实，反方回应充分',
          points: ['核心分歧 D1 证据充分', '估值锚定模型单元格'],
        },
        updates_thesis: true,
        thesis_version: 'v4',
      },
    };

    render(<PublishApprovalCard data={state} onApprove={onApprove} onReject={onReject} />);

    expect(screen.getByTestId('publish-approval-card')).toBeInTheDocument();
    expect(screen.getByTestId('publish-labeling-incomplete-warning')).toHaveTextContent(
      '标注未完成，口语数字和指标可能未核验'
    );
    expect(
      screen.getByText('42 已核实 · 0 冲突 · 0 未核实 · 2 未能判断 · 3 豁免')
    ).toBeInTheDocument();
    expect(screen.getByTestId('publish-uncertain-items')).toBeInTheDocument();
    expect(screen.getByText('88.8%')).toBeInTheDocument();
    expect(screen.getAllByText(/北美广告留存率/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/gpt-6.1-sol-oauth/)).toBeInTheDocument();
    expect(screen.getByText('v4')).toBeInTheDocument();

    fireEvent.click(screen.getByText('批准正式发布'));
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it('renders P0I additions: labeling coverage, verified breakdown, collapsible incidental list, side-by-side LLM judge draft vs quote, and unverified candidates', async () => {
    await i18n.changeLanguage('zh-CN');

    const state: ToolApprovalState = {
      status: 'pending',
      toolName: 'mcp__dalton-publish__publish_report',
      server: 'dalton-publish',
      tool: 'publish_report',
      actionIndex: 0,
      actionCount: 1,
      args: {
        request_id: 'req-p0i9876543210',
        ticker: 'GOOGL',
        title: 'Alphabet 2Q26 深度复盘',
        kind: 'earnings_review',
        draft_path: 'drafts/googl-2q26.md',
        published_path: 'published/2026-10-19-googl-2q26.md',
        figure_summary: {
          verified: 45,
          contradicted: 0,
          unverified: 1,
          uncertain: 0,
          exempt: 2,
          incidental: 2,
          labeling_incomplete: false,
          labeling: {
            total: 50,
            labeled: 50,
            batches: 2,
            failed_batches: 0,
            incidental_excluded: 2,
          },
          verified_by_counts: {
            footnote: 30,
            llm_judge: 10,
            derived: 5,
          },
          incidental_items: [
            {
              id: 'F-003',
              raw: '600',
              line: 5,
              incidental_category: '社交互动计数',
              sentence: 'X 帖子获得 600 赞。',
            },
            {
              id: 'F-004',
              raw: '500',
              line: 7,
              incidental_category: 'HTTP 状态码',
              sentence: '接口曾短暂返回 HTTP 500。',
            },
          ],
          llm_verified_items: [
            {
              id: 'F-012',
              raw: '968.3 亿美元',
              line: 14,
              metric: '合并总收入',
              period: '2Q26',
              sentence: '2Q26 Alphabet 合并总收入达 968.3 亿美元。',
              matched_source: 'evidence:E-GOOGL-001',
              quote_metric_raw: 'Consolidated revenues',
              quote: 'Consolidated revenues in Q2 2026 were $96,830 million ($96.83 billion).',
            },
          ],
          unverified_items: [
            {
              id: 'F-049',
              raw: '390 亿美元',
              line: 88,
              metric: '云计算收入',
              period: '2Q26',
              sentence: '2Q26 云计算收入约 390 亿美元。',
              hint: '未核实（unverified）',
              candidates: [
                {
                  source_id: 'E-GOOGL-002',
                  value: 142.7,
                  unit: 'USD_100mn',
                  text: 'Google Cloud revenues were $14.27 billion in Q2 2026.',
                },
              ],
            },
          ],
        },
      },
    };

    render(<PublishApprovalCard data={state} />);

    // 1. Labeling coverage
    expect(screen.getByTestId('publish-labeling-coverage')).toHaveTextContent(
      '已标注 50 / 50 个数字'
    );

    // 2. Verified breakdown (footnote / LLM judge / derived)
    expect(screen.getByTestId('publish-verified-breakdown')).toHaveTextContent(
      '脚注 30 · LLM 判断 10 · 推算 5'
    );

    // 3. Incidental collapsible list (collapsed initially, expands on click, shows category + sentence)
    expect(screen.getByTestId('publish-incidental-section')).toBeInTheDocument();
    expect(screen.queryByTestId('publish-incidental-list')).toBeNull();
    fireEvent.click(screen.getByTestId('publish-incidental-toggle'));
    expect(screen.getByTestId('publish-incidental-list')).toHaveTextContent('[社交互动计数]');
    expect(screen.getByTestId('publish-incidental-list')).toHaveTextContent('X 帖子获得 600 赞');
    expect(screen.getByTestId('publish-incidental-list')).toHaveTextContent('[HTTP 状态码]');
    expect(screen.getByTestId('publish-incidental-list')).toHaveTextContent('HTTP 500');

    // 4. Side-by-side LLM judge draft sentence vs quote
    const llmRow = screen.getByTestId('publish-llm-verified-row');
    expect(llmRow).toHaveTextContent('2Q26 Alphabet 合并总收入达 968.3 亿美元。');
    expect(llmRow).toHaveTextContent('Consolidated revenues in Q2 2026 were $96,830 million');
    expect(llmRow).toHaveTextContent('Consolidated revenues');

    // 5. Unverified items with candidates
    const unvSection = screen.getByTestId('publish-unverified-items');
    expect(unvSection).toHaveTextContent('390 亿美元');
    expect(screen.getByTestId('publish-item-candidates')).toHaveTextContent('E-GOOGL-002');
    expect(screen.getByTestId('publish-item-candidates')).toHaveTextContent(
      'Google Cloud revenues were $14.27 billion in Q2 2026.'
    );
  });

  it('supports legacy Chinese fallback keys when figure_summary object is omitted', async () => {
    await i18n.changeLanguage('zh-CN');
    const state: ToolApprovalState = {
      status: 'pending',
      toolName: 'mcp__dalton-publish__publish_report',
      server: 'dalton-publish',
      tool: 'publish_report',
      actionIndex: 0,
      actionCount: 1,
      args: {
        title: 'AMZN 初次覆盖',
        公司: 'AMZN',
        类型: 'initiation',
        草稿: 'drafts/amzn-init.md',
        发布为: 'published/2026-10-18-amzn-init.md',
        数字核验: '18 个数字：18 已核实，0 冲突，0 未核实，0 未能判断，0 豁免',
        独立审稿: '通过 · claude-opus-4-7 (anthropic-claude)',
        'thesis 正式版本': 'v1',
      },
    };

    render(<PublishApprovalCard data={state} />);

    expect(screen.queryByTestId('publish-labeling-incomplete-warning')).toBeNull();
    expect(screen.getByText('[AMZN] AMZN 初次覆盖')).toBeInTheDocument();
    expect(
      screen.getByText('18 个数字：18 已核实，0 冲突，0 未核实，0 未能判断，0 豁免')
    ).toBeInTheDocument();
    expect(
      screen.getByText('通过 · claude-opus-4-7 (anthropic-claude)')
    ).toBeInTheDocument();
  });

  it('renders English banner and labels when real i18n is loaded with en-US', async () => {
    await i18n.changeLanguage('en-US');
    const state: ToolApprovalState = {
      status: 'pending',
      toolName: 'mcp__dalton-publish__publish_report',
      server: 'dalton-publish',
      tool: 'publish_report',
      actionIndex: 0,
      actionCount: 1,
      args: {
        request_id: 'req-en123456',
        ticker: 'GOOGL',
        title: 'GOOGL 2Q26 Review',
        kind: 'earnings',
        labeling_incomplete: true,
        figure_summary: {
          verified: 10,
          contradicted: 0,
          unverified: 0,
          uncertain: 0,
          exempt: 0,
          labeling_incomplete: true,
          labeling: {
            total: 40,
            labeled: 20,
            batches: 2,
            failed_batches: 1,
            failed_batch_details: [
              { batch_index: 2, start_fig: 21, end_fig: 40, section: '估值与模型' },
              { batch_index: 3, start_fig: 0, end_fig: 0, section: '渠道观察' },
            ],
          },
          incidental: 1,
          incidental_items: [
            {
              id: 'F-099',
              raw: '404',
              line: 12,
              metric: '增速',
              incidental_category: '',
              sentence: '页面返回 404。',
            },
          ],
        },
      },
    };

    render(<PublishApprovalCard data={state} />);

    expect(screen.getByTestId('publish-labeling-incomplete-warning')).toHaveTextContent(
      'Figure labeling incomplete; colloquial figures and metric labels may not be fully verified'
    );
    expect(screen.getByTestId('publish-failed-batch-details')).toHaveTextContent(
      'Batch 2 (figures 21–40) labeling failed'
    );
    expect(screen.getByTestId('publish-failed-batch-details')).toHaveTextContent(
      'Batch 3 (colloquial figure scan) labeling failed'
    );
    fireEvent.click(screen.getByTestId('publish-incidental-toggle'));
    expect(screen.getByTestId('publish-incidental-list')).toHaveTextContent('[Uncategorized]');
    expect(screen.getByTestId('publish-incidental-list')).not.toHaveTextContent('[增速]');
    expect(
      screen.getByText('Official Research Publication Approval (PM Final Review)')
    ).toBeInTheDocument();
  });
});
