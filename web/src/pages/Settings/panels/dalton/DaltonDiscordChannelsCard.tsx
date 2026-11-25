import { useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Pencil, Plus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { apiErrorDetail, apiErrorReason, apiErrorStatus, formatApiErrorDetail, isForbiddenError } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { useCallerIdentity } from '@/hooks/useCallerIdentity';
import { queryKeys } from '@/lib/queryKeys';
import {
  fetchDaltonChannelBindings,
  mutateDaltonDiscordChannel,
  verifyDaltonDiscordChannel,
  type DaltonChannelChatBindingEntry,
} from '@/pages/Settings/utils/daltonApi';
import {
  DaltonInlineNotice,
  DaltonItemRow,
  DaltonKeyValueList,
  DaltonLoadingRow,
  DaltonRefreshButton,
  DaltonSettingsCard,
  DaltonStatusBadge,
} from '@/components/dalton/primitives';

const DISCORD_SNOWFLAKE_RE = /^[0-9]{17,20}$/;
const DISCORD_MAX_SNOWFLAKE = '18446744073709551615'; // 2^64 - 1
const DISCORD_CANONICAL_CHAT_KEY_RE = /^discord:([0-9]+)$/;
export const MAX_DISCORD_CHANNEL_NAME_LENGTH = 255;
const DISCORD_HOSTS = new Set([
  'discord.com',
  'www.discord.com',
  'ptb.discord.com',
  'canary.discord.com',
  'discordapp.com',
  'www.discordapp.com',
  'ptb.discordapp.com',
  'canary.discordapp.com',
]);

export function isValidDiscordSnowflake(raw: string): boolean {
  if (!DISCORD_SNOWFLAKE_RE.test(raw)) return false;
  return raw.length < 20 || raw <= DISCORD_MAX_SNOWFLAKE;
}

export interface ParsedDiscordChannelInput {
  channelId: string | null;
  reason?: 'dm_link' | 'invalid';
}

/**
 * Parse a raw 17-20 digit Discord snowflake (< 2^64), guild channel URL
 * (`/channels/<guild>/<channel>`), or message URL (`/channels/<guild>/<channel>/<message>`),
 * rejecting `@me` DM links.
 */
export function parseDiscordChannelInput(raw: string): ParsedDiscordChannelInput {
  const trimmed = raw.trim();
  if (!trimmed) return { channelId: null, reason: 'invalid' };
  if (isValidDiscordSnowflake(trimmed)) return { channelId: trimmed };
  if (/^https?:\/\//i.test(trimmed)) {
    try {
      const url = new URL(trimmed);
      if (!DISCORD_HOSTS.has(url.hostname.toLowerCase())) {
        return { channelId: null, reason: 'invalid' };
      }
      const parts = url.pathname.split('/').filter(Boolean);
      if (parts[0] !== 'channels') {
        return { channelId: null, reason: 'invalid' };
      }
      if (parts[1] === '@me') {
        return { channelId: null, reason: 'dm_link' };
      }
      if (
        (parts.length === 3 || parts.length === 4) &&
        isValidDiscordSnowflake(parts[1]) &&
        isValidDiscordSnowflake(parts[2]) &&
        (parts.length === 3 || isValidDiscordSnowflake(parts[3]))
      ) {
        return { channelId: parts[2] };
      }
      return { channelId: null, reason: 'invalid' };
    } catch {
      return { channelId: null, reason: 'invalid' };
    }
  }
  return { channelId: null, reason: 'invalid' };
}

/**
 * Extract a numeric Discord channel ID from a raw snowflake, channel URL, or message URL.
 */
export function extractDiscordChannelId(raw: string): string | null {
  return parseDiscordChannelInput(raw).channelId;
}

function isPlainObject(val: unknown): val is Record<string, unknown> {
  return Boolean(val) && typeof val === 'object' && !Array.isArray(val);
}

function isDmAddress(addr: string): boolean {
  const trimmed = addr.trim();
  return trimmed === 'discord:@me' || trimmed.endsWith(':@me');
}

function codePointLength(value: string): number {
  return [...value].length;
}

function truncateCodePoints(value: string, max: number): string {
  return [...value].slice(0, max).join('');
}

function extractErrorCode(err: unknown): string | null {
  const responseData = (err as { response?: { data?: unknown } })?.response?.data;
  const dataObj = isPlainObject(responseData) ? responseData : null;
  const detail = apiErrorDetail(err);
  const detailObj = isPlainObject(detail) ? detail : null;
  const rawCode = detailObj?.code ?? dataObj?.code;
  return typeof rawCode === 'string' && rawCode.trim() ? rawCode.trim() : null;
}

function formatChannelSettingsError(err: unknown, fallback: string): string {
  const responseData = (err as { response?: { data?: unknown } })?.response?.data;
  const dataObj = isPlainObject(responseData) ? responseData : null;
  const detail = apiErrorDetail(err);
  const detailObj = isPlainObject(detail) ? detail : null;

  const rawProblems = detailObj?.problems ?? dataObj?.problems;
  const problemTexts: string[] = [];
  if (Array.isArray(rawProblems)) {
    for (const item of rawProblems) {
      if (!isPlainObject(item)) continue;
      const msg = typeof item.message === 'string' ? item.message.trim() : '';
      if (!msg) continue;
      const field = typeof item.field === 'string' ? item.field.trim() : '';
      problemTexts.push(field ? `${field}: ${msg}` : msg);
    }
  }

  const msgCandidate =
    (typeof detailObj?.message === 'string' && detailObj.message.trim()) ||
    (typeof dataObj?.message === 'string' && dataObj.message.trim()) ||
    (typeof detail === 'string' && detail.trim()) ||
    '';

  if (msgCandidate && problemTexts.length > 0) {
    if (problemTexts.length === 1 && problemTexts[0] === msgCandidate) {
      return msgCandidate;
    }
    if (problemTexts.length === 1 && problemTexts[0].includes(msgCandidate)) {
      return problemTexts[0];
    }
    return `${msgCandidate} (${problemTexts.join('; ')})`;
  }
  if (problemTexts.length > 0) {
    return problemTexts.join('; ');
  }
  if (msgCandidate) {
    return msgCandidate;
  }

  return apiErrorReason(err) || fallback;
}

function isVersionConflictError(err: unknown, code: string | null): boolean {
  if (code === 'version_conflict') return true;
  if (code !== null) return false;
  return apiErrorStatus(err) === 409;
}

interface DiscordChannelListItem {
  chatAddress: string;
  channelId: string;
  name: string;
  hasSharedBinding: boolean;
}

interface NoticeState {
  tone: 'error' | 'warning' | 'success' | 'info';
  message: string;
  testId: string;
}

/** Admin-only Discord channels registration & rename card aligned with DaltonFeishuGroupsCard. */
export function DaltonDiscordChannelsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const identity = useCallerIdentity();

  const [channelInput, setChannelInput] = useState('');
  const [nameInput, setNameInput] = useState('');
  const [editingAddr, setEditingAddr] = useState<string | null>(null);
  const [editingName, setEditingName] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState<NoticeState | null>(null);

  const bindingsQuery = useQuery({
    queryKey: queryKeys.dalton.channelBindings(),
    queryFn: fetchDaltonChannelBindings,
    enabled: identity.isAdmin,
    retry: false,
  });

  if (!identity.isAdmin) {
    return null;
  }

  const rawSettings = bindingsQuery.data?.settings;
  const currentVersion = rawSettings?.version ?? 'v1';
  const currentApps = isPlainObject(rawSettings?.apps) ? rawSettings.apps : {};
  const rawDiscord = isPlainObject(currentApps.discord) ? currentApps.discord : null;
  const rawChats = rawDiscord && isPlainObject(rawDiscord.chats) ? rawDiscord.chats : {};

  const bindingByAddr = new Map<string, DaltonChannelChatBindingEntry>();
  if (Array.isArray(bindingsQuery.data?.chat_bindings)) {
    for (const item of bindingsQuery.data.chat_bindings) {
      if (
        isPlainObject(item) &&
        item.platform === 'discord' &&
        typeof item.chat_address === 'string'
      ) {
        bindingByAddr.set(item.chat_address, item as unknown as DaltonChannelChatBindingEntry);
      }
    }
  }

  const channels: DiscordChannelListItem[] = [];
  const registeredChannelIds = new Set<string>();
  let legacyBindingCount = 0;

  for (const [addr, cfg] of Object.entries(rawChats)) {
    if (isDmAddress(addr)) continue;
    const match = DISCORD_CANONICAL_CHAT_KEY_RE.exec(addr);
    if (!match) {
      legacyBindingCount += 1;
      continue;
    }
    const channelId = match[1];
    const binding = bindingByAddr.get(addr);
    const hasSharedBinding = Boolean(binding);
    if (hasSharedBinding) {
      registeredChannelIds.add(channelId);
    }
    const cfgName = isPlainObject(cfg) && typeof cfg.name === 'string' ? cfg.name.trim() : '';
    const bindingName = typeof binding?.chat_name === 'string' ? binding.chat_name.trim() : '';
    channels.push({
      chatAddress: addr,
      channelId,
      name: cfgName || bindingName,
      hasSharedBinding,
    });
  }

  const runChannelMutation = async (
    payload: { action: 'add' | 'rename'; channel_id: string; name?: string | null },
    successNotice?: NoticeState | null,
  ): Promise<boolean> => {
    try {
      await mutateDaltonDiscordChannel({
        ...payload,
        version: currentVersion,
      });
      await queryClient.invalidateQueries({ queryKey: queryKeys.dalton.channelBindings() });
      if (successNotice !== undefined) {
        setNotice(successNotice);
      }
      return true;
    } catch (err: unknown) {
      if (isForbiddenError(err)) {
        void identity.revalidate();
      }
      const code = extractErrorCode(err);
      if (isVersionConflictError(err, code)) {
        setNotice({
          tone: 'warning',
          message: t('dalton.discordChannels.versionConflict'),
          testId: 'dalton-discord-channels-version-conflict',
        });
        return false;
      }
      setNotice({
        tone: 'error',
        message: formatChannelSettingsError(err, t('dalton.discordChannels.saveFailed')),
        testId: 'dalton-discord-channels-error',
      });
      return false;
    }
  };

  const handleAdd = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting || !bindingsQuery.data) return;
    setNotice(null);

    const parsed = parseDiscordChannelInput(channelInput);
    if (!parsed.channelId) {
      setNotice({
        tone: 'error',
        message:
          parsed.reason === 'dm_link'
            ? t('dalton.discordChannels.dmLinkNotAllowed')
            : t('dalton.discordChannels.invalidChannelId'),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }
    const extractedId = parsed.channelId;

    if (registeredChannelIds.has(extractedId)) {
      setNotice({
        tone: 'error',
        message: t('dalton.discordChannels.alreadyAdded'),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }

    const trimmedName = nameInput.trim();
    if (codePointLength(trimmedName) > MAX_DISCORD_CHANNEL_NAME_LENGTH) {
      setNotice({
        tone: 'error',
        message: t('dalton.discordChannels.nameTooLong', {
          max: MAX_DISCORD_CHANNEL_NAME_LENGTH,
        }),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }

    setSubmitting(true);
    let verifyRes;
    try {
      verifyRes = await verifyDaltonDiscordChannel(extractedId);
    } catch (err: unknown) {
      setSubmitting(false);
      setNotice({
        tone: 'error',
        message: formatApiErrorDetail(err) || t('dalton.discordChannels.verifyFailed'),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }

    if (!verifyRes.verified && verifyRes.reason !== 'no_token') {
      setSubmitting(false);
      setNotice({
        tone: 'error',
        message: verifyRes.detail || t('dalton.discordChannels.verifyFailed'),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }

    const isReRegister = channels.some((item) => item.channelId === extractedId);
    const resolvedName = truncateCodePoints(
      trimmedName || (isReRegister ? '' : verifyRes.channel_name?.trim() || ''),
      MAX_DISCORD_CHANNEL_NAME_LENGTH,
    );
    const postSaveNotice: NoticeState | null =
      !verifyRes.verified && verifyRes.reason === 'no_token'
        ? {
            tone: 'warning',
            message: t('dalton.discordChannels.noTokenNotice'),
            testId: 'dalton-discord-channels-no-token-notice',
          }
        : null;

    const saved = await runChannelMutation(
      {
        action: 'add',
        channel_id: extractedId,
        ...(resolvedName ? { name: resolvedName } : {}),
      },
      postSaveNotice,
    );
    setSubmitting(false);
    if (saved) {
      setChannelInput('');
      setNameInput('');
    }
  };

  const handleRenameSubmit = async (item: DiscordChannelListItem) => {
    if (submitting || !bindingsQuery.data) return;
    const trimmedName = editingName.trim();
    if (!trimmedName) {
      setNotice({
        tone: 'error',
        message: t('dalton.discordChannels.nameRequired'),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }
    if (codePointLength(trimmedName) > MAX_DISCORD_CHANNEL_NAME_LENGTH) {
      setNotice({
        tone: 'error',
        message: t('dalton.discordChannels.nameTooLong', {
          max: MAX_DISCORD_CHANNEL_NAME_LENGTH,
        }),
        testId: 'dalton-discord-channels-error',
      });
      return;
    }

    setNotice(null);
    setSubmitting(true);
    const saved = await runChannelMutation(
      {
        action: 'rename',
        channel_id: item.channelId,
        name: trimmedName,
      },
      null,
    );
    setSubmitting(false);
    if (saved) {
      setEditingAddr(null);
      setEditingName('');
    }
  };

  return (
    <DaltonSettingsCard
      id="dalton-discord-channels"
      testId="dalton-discord-channels-card"
      title={t('dalton.discordChannels.title')}
      description={t('dalton.discordChannels.description')}
      layout="padded"
      bodyClassName="space-y-4"
      action={
        <DaltonRefreshButton
          busy={bindingsQuery.isFetching}
          onClick={() => void bindingsQuery.refetch()}
          testId="dalton-discord-channels-refresh"
        />
      }
    >
      {bindingsQuery.isLoading ? (
        <DaltonLoadingRow label={t('dalton.common.loading')} />
      ) : bindingsQuery.isError ? (
        <DaltonInlineNotice tone="error">{t('dalton.discordChannels.loadFailed')}</DaltonInlineNotice>
      ) : (
        <>
          <form onSubmit={(e) => void handleAdd(e)} className="flex flex-wrap items-center gap-2">
            <Input
              value={channelInput}
              onChange={(e) => setChannelInput(e.target.value)}
              disabled={submitting}
              aria-label={t('dalton.discordChannels.channelIdPlaceholder')}
              placeholder={t('dalton.discordChannels.channelIdPlaceholder')}
              data-testid="dalton-discord-channel-id-input"
              className="h-9 md:h-8 w-full md:w-72 text-xs font-mono"
            />
            <Input
              value={nameInput}
              onChange={(e) => setNameInput(e.target.value)}
              disabled={submitting}
              aria-label={t('dalton.discordChannels.namePlaceholder')}
              placeholder={t('dalton.discordChannels.namePlaceholder')}
              data-testid="dalton-discord-channel-name-input"
              className="h-9 md:h-8 w-full md:w-48 text-xs"
            />
            <Button
              type="submit"
              variant="outline"
              size="sm"
              className="h-8 gap-1 text-xs"
              disabled={submitting || !channelInput.trim()}
              data-testid="dalton-discord-channel-add-btn"
            >
              <Plus className="h-3.5 w-3.5" />
              <span>
                {submitting
                  ? t('dalton.discordChannels.adding')
                  : t('dalton.discordChannels.addButton')}
              </span>
            </Button>
          </form>

          {notice && (
            <DaltonInlineNotice tone={notice.tone} testId={notice.testId}>
              {notice.message}
            </DaltonInlineNotice>
          )}

          <DaltonKeyValueList
            isEmpty={channels.length === 0}
            empty={t('dalton.discordChannels.empty')}
          >
            {channels.map((item) => {
              const isEditing = editingAddr === item.chatAddress;
              const displayTitle = item.name || t('dalton.discordChannels.unnamedChannel');

              return (
                <DaltonItemRow
                  key={item.chatAddress}
                  testId={`dalton-discord-channel-row-${item.channelId}`}
                  title={displayTitle}
                  meta={
                    <span className="font-mono max-md:break-all">
                      {t('dalton.discordChannels.channelIdLabel')}: {item.channelId}
                    </span>
                  }
                  trailing={
                    <div className="flex max-md:flex-wrap items-center gap-2">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 gap-1 text-xs"
                        disabled={submitting}
                        onClick={() => {
                          setEditingAddr(item.chatAddress);
                          setEditingName(item.name);
                          setNotice(null);
                        }}
                        data-testid={`dalton-discord-channel-rename-${item.channelId}`}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                        <span>{t('dalton.discordChannels.renameButton')}</span>
                      </Button>
                      {item.hasSharedBinding ? (
                        <DaltonStatusBadge
                          dot
                          tone="success"
                          label={t('dalton.discordChannels.registeredBadge')}
                        />
                      ) : (
                        <DaltonStatusBadge
                          dot
                          tone="warning"
                          label={t('dalton.discordChannels.missingBindingBadge')}
                        />
                      )}
                    </div>
                  }
                >
                  {isEditing && (
                    <div className="flex flex-wrap items-center gap-2">
                      <Input
                        value={editingName}
                        onChange={(e) => setEditingName(e.target.value)}
                        disabled={submitting}
                        aria-label={t('dalton.discordChannels.namePlaceholder')}
                        placeholder={t('dalton.discordChannels.namePlaceholder')}
                        data-testid={`dalton-discord-channel-rename-input-${item.channelId}`}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') {
                            e.preventDefault();
                            void handleRenameSubmit(item);
                          } else if (e.key === 'Escape') {
                            setEditingAddr(null);
                            setEditingName('');
                          }
                        }}
                        className="h-9 md:h-8 w-full md:w-56 text-xs"
                      />
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="h-8 text-xs"
                        disabled={submitting}
                        onClick={() => void handleRenameSubmit(item)}
                        data-testid={`dalton-discord-channel-rename-save-${item.channelId}`}
                      >
                        {t('dalton.common.save')}
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="h-8 text-xs"
                        disabled={submitting}
                        onClick={() => {
                          setEditingAddr(null);
                          setEditingName('');
                        }}
                        data-testid={`dalton-discord-channel-rename-cancel-${item.channelId}`}
                      >
                        {t('dalton.common.cancel')}
                      </Button>
                    </div>
                  )}
                </DaltonItemRow>
              );
            })}
          </DaltonKeyValueList>

          {legacyBindingCount > 0 && (
            <p
              className="text-xs text-muted-foreground"
              data-testid="dalton-discord-channels-legacy-note"
            >
              {t('dalton.discordChannels.legacyBindingsNote', { count: legacyBindingCount })}
            </p>
          )}
        </>
      )}
    </DaltonSettingsCard>
  );
}
