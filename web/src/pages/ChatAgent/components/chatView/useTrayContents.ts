import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useStableArray } from '@/hooks/useStableArray';
import type { ChatMessage } from '@/types/chat';
import type { WakeupList } from '@/types/wakeups';
import { useThreadWakeups } from '../../hooks/useWakeups';
import type { MarketWatchState } from '../../session/marketWatchEvents';
import type { ReportBackOwedThread } from '../../utils/api';
import {
  analystCandidates,
  countedTurnStarts,
  rosterTasksEqual,
  taskRoster,
  type RosterTask,
} from './delegatedWork';
import { useAnalystRoster, type RosterAnalyst } from './useAnalystRoster';
import { pendingWakeups } from '../wakeupLabels';

interface TrayInputs {
  marketWatch: MarketWatchState | null;
  /** The thread whose wakeups the tray counts, and whether its chat is in front. */
  threadId: string | null;
  isActive: boolean;
  /** Live task cards, keyed `subagent-<agentId>`. */
  cards: Parameters<typeof taskRoster>[0]['cards'];
  /** The tasks the page's history loaded, by agent id. */
  taskHistory: Parameters<typeof taskRoster>[0]['history'];
  /** The main agent's transcript. */
  messages: ChatMessage[];
  isLoading: boolean;
  hasActiveSubagents: boolean;
  reportBackOwed: boolean;
  owedThreads: ReportBackOwedThread[] | null;
  /** Who reports back here: Analysts to Home, subagents to a workspace thread. */
  isHome: boolean;
}

/** The tasks chip: the rows it lists, and whether a task no row names runs
 *  (a workflow's own steps, or a row not loaded yet). */
export interface TrayTasks {
  rows: RosterTask[];
  unnamedRunning: boolean;
}

/** What the tray holds: one chip per entry that is set. */
export interface TrayContents {
  /** The market watch, while the agent watches tickers. */
  watch: MarketWatchState | null;
  /** The wakeups still pending, while there are any. */
  wakeups: WakeupList | null;
  /** The tasks this thread handed off, while one is listed or runs. */
  tasks: TrayTasks | null;
  /** The Analysts this thread handed work to (Home only). */
  analysts: RosterAnalyst[] | null;
  reportBackLabel: string | null;
}

const NO_CANDIDATES: ReturnType<typeof analystCandidates> = [];

/** How many chips the tray draws, which is how many shapes its liquid runs. */
export function trayChipCount(contents: TrayContents | null): number {
  if (!contents) return 0;
  const { watch, wakeups, tasks, analysts, reportBackLabel } = contents;
  return [watch, wakeups, tasks, analysts, reportBackLabel].filter((entry) => entry !== null).length;
}

/** Whether anything in the tray is still working, which the folded dock spins for. */
export function trayWorking({ tasks, analysts, reportBackLabel }: TrayContents): boolean {
  return !!(
    (tasks && (tasks.unnamedRunning || tasks.rows.some((task) => task.running)))
    || analysts?.some((analyst) => analyst.running)
    || reportBackLabel
  );
}

/**
 * What the thread is waiting on or handed off, which the tray shows as chips:
 * the market watch, its pending wakeups, the tasks and Analysts it delegated, and an owed report
 * neither names. Null when there is none. Read once by the host, since the
 * tray and its folded dock show the same thing.
 */
export function useTrayContents({
  marketWatch, threadId, isActive, cards, taskHistory, messages, isLoading, hasActiveSubagents, reportBackOwed, owedThreads, isHome,
}: TrayInputs): TrayContents | null {
  const { t } = useTranslation();

  const watch = marketWatch && marketWatch.symbols.length ? marketWatch : null;
  const wakeups = useThreadWakeups(threadId, isActive);
  // A wakeup fires server-side between turns, so it keeps the chip up.
  const pending = useMemo(() => {
    const items = pendingWakeups(wakeups?.items);
    return items.length ? { items, userTimezone: wakeups?.userTimezone ?? null } : null;
  }, [wakeups]);
  const turnStarts = countedTurnStarts(messages);
  // A task's status can read live after its run ended unseen; one runs only
  // while this turn does or a task channel is open.
  const tasks = useStableArray(
    taskRoster({ cards, history: taskHistory, messages, turnStarts, workRunning: hasActiveSubagents || isLoading }),
    rosterTasksEqual,
  );
  const analysts = useAnalystRoster(isHome ? analystCandidates(messages, owedThreads) : NO_CANDIDATES, turnStarts);

  // An open task channel that no running row accounts for still shows, beside
  // the rows the tray keeps.
  const unnamedRunning = hasActiveSubagents && !tasks.some((task) => task.running);
  const taskChip = tasks.length > 0 || unnamedRunning ? { rows: tasks, unnamedRunning } : null;
  const analystChip = analysts.length > 0 ? analysts : null;
  // A report the tray can only name: an owed report whose sender is no
  // longer working, so no chip is already waiting on it. An Analyst waiting
  // on the user has not finished, and its row says so.
  const working = hasActiveSubagents || tasks.some((task) => task.running)
    || analysts.some((analyst) => analyst.running || analyst.status === 'needs_input');
  const reportBackLabel = reportBackOwed && !working
    ? t(isHome ? 'agents.reportBackPending' : 'chat.taskReportBackPending')
    : null;

  if (!watch && !pending && !taskChip && !analystChip && !reportBackLabel) return null;
  return { watch, wakeups: pending, tasks: taskChip, analysts: analystChip, reportBackLabel };
}
