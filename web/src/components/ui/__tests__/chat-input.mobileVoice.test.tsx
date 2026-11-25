/**
 * Mobile Web P2: VoiceButton visibility strictly follows browser Web Speech API
 * availability (`window.SpeechRecognition || window.webkitSpeechRecognition`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import '@testing-library/jest-dom';
import { screen } from '@testing-library/react';
import { renderWithProviders } from '@/test/utils';
import ChatInput from '../chat-input';
import { isSpeechRecognitionSupported } from '../chat-input.useVoiceInput';

vi.mock('@/pages/ChatAgent/utils/api', () => ({
  getSkills: vi.fn().mockResolvedValue([]),
  getModelMetadata: vi.fn().mockResolvedValue({}),
}));

vi.mock('@/hooks/usePreferences', () => ({
  usePreferences: () => ({
    preferences: {
      other_preference: {
        voice_input_enabled: true,
      },
    },
    isLoading: false,
  }),
}));

vi.mock('../use-toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
  toast: vi.fn(),
}));

const originalSpeech = window.SpeechRecognition;
const originalWebkitSpeech = window.webkitSpeechRecognition;

function setSpeechApis(speech: unknown, webkitSpeech: unknown) {
  Object.defineProperty(window, 'SpeechRecognition', {
    value: speech,
    writable: true,
    configurable: true,
  });
  Object.defineProperty(window, 'webkitSpeechRecognition', {
    value: webkitSpeech,
    writable: true,
    configurable: true,
  });
}

describe('ChatInput voice button visibility (Mobile P2)', () => {
  beforeEach(() => {
    setSpeechApis(undefined, undefined);
  });

  afterEach(() => {
    setSpeechApis(originalSpeech, originalWebkitSpeech);
    vi.restoreAllMocks();
  });

  it('hides the voice button when neither SpeechRecognition nor webkitSpeechRecognition is available', () => {
    expect(isSpeechRecognitionSupported()).toBe(false);
    const { container } = renderWithProviders(<ChatInput onSend={vi.fn()} />);
    expect(container.querySelector('.mic-button')).toBeNull();
  });

  it('shows the voice button with .tap-target when webkitSpeechRecognition is available', () => {
    const FakeWebkitSpeech = vi.fn();
    setSpeechApis(undefined, FakeWebkitSpeech);
    expect(isSpeechRecognitionSupported()).toBe(true);

    const { container } = renderWithProviders(<ChatInput onSend={vi.fn()} />);
    const micBtn = container.querySelector('.mic-button');
    expect(micBtn).not.toBeNull();
    expect(micBtn!.className).toContain('tap-target');
    expect(screen.getByRole('button', { name: /voice|语音/i })).toBeInTheDocument();
  });

  it('shows the voice button with .tap-target when standard SpeechRecognition is available', () => {
    const FakeSpeech = vi.fn();
    setSpeechApis(FakeSpeech, undefined);
    expect(isSpeechRecognitionSupported()).toBe(true);

    const { container } = renderWithProviders(<ChatInput onSend={vi.fn()} />);
    const micBtn = container.querySelector('.mic-button');
    expect(micBtn).not.toBeNull();
    expect(micBtn!.className).toContain('tap-target');
  });
});
