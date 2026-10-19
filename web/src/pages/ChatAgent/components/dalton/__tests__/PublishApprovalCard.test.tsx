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
        },
      },
    };

    render(<PublishApprovalCard data={state} />);

    expect(screen.getByTestId('publish-labeling-incomplete-warning')).toHaveTextContent(
      'Figure labeling incomplete; colloquial figures and metric labels may not be fully verified'
    );
    expect(
      screen.getByText('Official Research Publication Approval (PM Final Review)')
    ).toBeInTheDocument();
  });
});
