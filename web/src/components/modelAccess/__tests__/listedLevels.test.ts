import { describe, expect, it } from 'vitest';
import type { ListedModel } from '@/api/model';
import { entryForListed, withListedSpecs } from '../useModelPicker';

const ladder = { efforts: ['none', 'low', 'high', 'max'], default: 'high', write: 'parameters.reasoning.effort' };

describe('effort levels from a provider list', () => {
  it('a pick is saved with the levels the list gives it', () => {
    const listed: ListedModel = { id: 'deepseek/deepseek-v4-flash', context_window: 128000, reasoning: ladder };
    expect(entryForListed(listed, 'openrouter', new Set())).toEqual({
      name: 'deepseek/deepseek-v4-flash',
      model_id: 'deepseek/deepseek-v4-flash',
      provider: 'openrouter',
      context: 128000,
      reasoning: ladder,
    });
    expect(entryForListed({ id: 'plain/chat' }, 'openrouter', new Set())).not.toHaveProperty('reasoning');
  });

  it('a pick whose provider-prefixed name is taken too is numbered, not saved over it', () => {
    expect(entryForListed({ id: 'foo' }, 'openai', new Set(['foo', 'openai/foo', 'openai/foo-2'])).name).toBe('openai/foo-3');
  });

  it('a saved pick takes the levels listed now, and one the list does not name stays as saved', () => {
    const old = { name: 'a', model_id: 'deepseek/deepseek-v4-flash', provider: 'openrouter' };
    const manual = { name: 'b', model_id: 'acme/house', provider: 'openrouter', reasoning: { efforts: ['low'], write: 'parameters.reasoning.effort' } };
    const listed: ListedModel[] = [{ id: 'deepseek/deepseek-v4-flash', reasoning: ladder }, { id: 'acme/house' }];
    expect(withListedSpecs([old, manual], listed)).toEqual([{ ...old, reasoning: ladder }, manual]);
  });

  it('a saved pick takes the window listed now, so a lowered limit is the one sent', () => {
    const saved = { name: 'a', model_id: 'deepseek/deepseek-v4-flash', provider: 'openrouter', context: 256000 };
    expect(withListedSpecs([saved], [{ id: 'deepseek/deepseek-v4-flash', context_window: 128000 }]))
      .toEqual([{ ...saved, context: 128000 }]);
    expect(withListedSpecs([saved], [{ id: 'deepseek/deepseek-v4-flash' }])).toEqual([saved]);
  });

  it('a saved pick takes the inputs listed now, and keeps its own when the list names none', () => {
    const saved = { name: 'a', model_id: 'acme/vision', provider: 'openrouter', input_modalities: ['text', 'image'] };
    const [textOnly] = withListedSpecs([saved], [{ id: 'acme/vision', input_modalities: ['text'] }]);
    expect(textOnly).not.toHaveProperty('input_modalities');
    expect(withListedSpecs([saved], [{ id: 'acme/vision', input_modalities: ['text', 'pdf'] }]))
      .toEqual([{ ...saved, input_modalities: ['text', 'pdf'] }]);
    expect(withListedSpecs([saved], [{ id: 'acme/vision' }])).toEqual([saved]);
    // OpenRouter's word for a PDF is `file`.
    expect(withListedSpecs([saved], [{ id: 'acme/vision', input_modalities: ['text', 'file'] }]))
      .toEqual([{ ...saved, input_modalities: ['text', 'pdf'] }]);
  });
});
