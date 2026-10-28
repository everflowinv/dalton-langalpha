/**
 * A device sign-in whose page arrives with the code filled in asks the user to
 * compare it, not paste it, and leaves the clipboard alone; one whose plan has
 * no API access offers no new code, since signing in again cannot help.
 *
 * DALTON-HOOK(xai-oauth): Dalton does not offer upstream's `grok-oauth`
 * (api.x.ai) sign-in — its Grok models are pinned to the `xai-oauth` CLI-proxy
 * line — so upstream's three Grok cases run against `xai-oauth` here. That
 * line never pre-fills the code on the provider's page
 * (`PREFILL['xai-oauth'] = false` in DeviceConnect): even when the server says
 * the page carries the code, the user copies and pastes it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderWithProviders } from '@/test/utils';

const h = vi.hoisted(() => ({
  initiateDevice: vi.fn(),
  pollDevice: vi.fn(),
}));

vi.mock('@/hooks/useAllModels', () => ({
  useAllModels: () => ({ rawModels: {}, metadata: {}, isLoading: false }),
}));
vi.mock('@/api/oauth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/oauth')>()),
  initiateDevice: h.initiateDevice,
  pollDevice: h.pollDevice,
}));

import { DeviceConnect } from '../DeviceConnect';

const device = (prefilled: boolean) => ({
  user_code: 'ABCD-1234',
  verification_url: 'https://accounts.example.com/device?code=ABCD-1234',
  verification_url_has_code: prefilled,
  interval: 60,
  expires_at: null,
});

async function getCode() {
  // user-event puts its own clipboard on the page, so it is watched from here.
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, 'writeText');
  renderWithProviders(<DeviceConnect provider="xai-oauth" onConnected={() => {}} onCancel={() => {}} />);
  await user.click(screen.getByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: /get my code/i }));
  return { user, write };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('DeviceConnect for xai-oauth (SuperGrok)', () => {
  it('still asks to copy and paste when the page fills the code in', async () => {
    // DALTON-HOOK(xai-oauth): unlike upstream's Grok line, the xai line never
    // trusts the page to pre-fill the code, so the clipboard is used and the
    // compare flow never renders, whatever the server says.
    h.initiateDevice.mockResolvedValue(device(true));
    h.pollDevice.mockResolvedValue({ status: 'pending' });
    const { write } = await getCode();

    expect(await screen.findByText('Your code')).toBeInTheDocument();
    expect(screen.getByText('ABCD-1234')).toBeInTheDocument();
    expect(screen.getAllByText('Open auth.x.ai').length).toBeGreaterThan(0);
    expect(screen.queryByText(/shows the same code/i)).toBeNull();
    expect(write).toHaveBeenCalledWith('ABCD-1234');
  });

  it('asks to copy and paste a code the page does not fill in', async () => {
    h.initiateDevice.mockResolvedValue(device(false));
    h.pollDevice.mockResolvedValue({ status: 'pending' });
    const { write } = await getCode();

    expect(await screen.findByText('Your code')).toBeInTheDocument();
    expect(screen.getAllByText('Open auth.x.ai').length).toBeGreaterThan(0);
    expect(screen.getByText('Approve the code on xAI’s page')).toBeInTheDocument();
    expect(screen.queryByText(/shows the same code/i)).toBeNull();
    expect(write).toHaveBeenCalledWith('ABCD-1234');
    expect(h.initiateDevice).toHaveBeenCalledWith('xai-oauth');
  });

  it('names a refusal by the code under error, not the detail sentence', async () => {
    h.initiateDevice.mockRejectedValue({
      response: {
        data: {
          detail: 'xAI is not offering device sign-in right now.',
          error: { code: 'device_flow_unavailable', message: 'unused', retryable: false },
        },
      },
    });
    await getCode();

    // DALTON-HOOK(xai-oauth): the coded xai copy, never the server's sentence.
    expect(await screen.findByText("xAI isn't offering code sign-in right now.")).toBeInTheDocument();
    expect(screen.queryByText('xAI is not offering device sign-in right now.')).toBeNull();
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument();
  });
});
