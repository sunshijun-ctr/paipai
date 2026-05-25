/* RAG chunk inspector overlay: shows the indexed chunks for one library
 * document (text + section/type/page/parser metadata) with a text filter.
 * Opened from the "chunk count" button in the paper-manager (library.js).
 *
 * State (currentChunks) is module-private; only the functions are bridged
 * (main.js) for the library onclick + the filter's oninput handler.
 */

import { esc } from "./utils.js";
import { apiGet } from "./api.js";

let currentChunks = [];

export async function openChunkViewer(lib_id, title) {
  const overlay = document.getElementById('chunk-overlay');
  const list = document.getElementById('chunk-list');
  document.getElementById('chunk-title').textContent = title;
  document.getElementById('chunk-filter').value = '';
  document.getElementById('chunk-stat').textContent = 'Loading...';
  currentChunks = [];
  list.innerHTML = '<div class="paper-empty">Loading chunks...</div>';
  overlay.classList.add('on');
  try {
    const d = await apiGet(`/api/libraries/${encodeURIComponent(lib_id)}/documents/chunks?title=${encodeURIComponent(title)}`);
    currentChunks = d.chunks || [];
    renderChunks();
  } catch (e) {
    const detail = e?.body?.detail || (typeof e?.body === 'string' ? e.body : '') || e?.message || String(e);
    document.getElementById('chunk-stat').textContent = '';
    list.innerHTML = `<div class="paper-empty">加载 chunk 失败：${esc(detail)}</div>`;
  }
}

export function renderChunks() {
  const list = document.getElementById('chunk-list');
  const stat = document.getElementById('chunk-stat');
  const q = (document.getElementById('chunk-filter')?.value || '').trim().toLowerCase();
  let chunks = currentChunks;
  if (q) {
    chunks = chunks.filter(c => {
      const meta = c.metadata || {};
      return `${c.text || ''} ${c.section || ''} ${c.chunk_type || ''} ${meta.parser || ''}`.toLowerCase().includes(q);
    });
  }
  stat.textContent = `${chunks.length} / ${currentChunks.length} chunks`;
  if (!chunks.length) {
    list.innerHTML = '<div class="paper-empty">没有匹配的 chunk</div>';
    return;
  }
  list.innerHTML = chunks.map((c, i) => {
    const meta = c.metadata || {};
    const bits = [
      `section: ${c.section || '-'}`,
      `type: ${c.chunk_type || 'text'}`,
      `page: ${c.page || '-'}`,
      `len: ${c.length || 0}`,
      meta.chunk_size ? `size: ${meta.chunk_size}` : '',
      meta.chunk_overlap !== undefined ? `overlap: ${meta.chunk_overlap}` : '',
      meta.parser ? `parser: ${meta.parser}` : '',
      c.id || '',
    ].filter(Boolean);
    return `
      <div class="chunk-card">
        <div class="chunk-card-head">
          <span class="chunk-no">#${c.global_chunk ?? i}</span>
          ${bits.map(b => `<span>${esc(String(b))}</span>`).join('')}
        </div>
        <div class="chunk-text">${esc(c.text || '')}</div>
      </div>`;
  }).join('');
}

export function closeChunkViewer(event) {
  if (event && event.target !== document.getElementById('chunk-overlay')) return;
  document.getElementById('chunk-overlay').classList.remove('on');
  currentChunks = [];
}
