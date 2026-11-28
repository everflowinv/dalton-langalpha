// Preserve upstream original HTML for TMTB images. Tokens stay in the existing reader's memory.
import { fetchDocumentDetail, isValidDocumentId } from './server.mjs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = process.env.DALTON_RESEARCH_HOME?.trim()
  ? path.resolve(process.env.DALTON_RESEARCH_HOME.trim())
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

try {
  const id = process.argv[2];
  if (!isValidDocumentId(id)) throw new Error('invalid document id');
  const detail = await fetchDocumentDetail(id);
  const html = typeof detail.original_text === 'string' ? detail.original_text : '';
  if (!/<img\b/i.test(html)) {
    console.log(JSON.stringify({ ok: true, status: 'no_inline_images', attachment_available: !!detail.file_path }));
  } else {
    const folder = path.join(root, 'state/research-images/upstream');
    await mkdir(folder, { recursive: true, mode: 0o700 });
    const file = path.join(folder, id + '.html');
    await writeFile(file, html, { mode: 0o600 });
    console.log(JSON.stringify({ ok: true, status: 'html_saved', local_path: file }));
  }
} catch {
  console.log(JSON.stringify({ ok: false, status: 'source_unavailable', hint: 'Check the existing AlphaEngine Desktop session.' }));
  process.exitCode = 1;
}
