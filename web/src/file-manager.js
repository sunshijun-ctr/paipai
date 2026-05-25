/* "我的文件" manager: the storage-usage bar in the sidebar + the file
 * manager modal (list / filter by category / delete) backed by /api/files
 * and /api/storage/usage.
 *
 * State (fmCurrentCategory) and FM_CAT_ICONS are module-private; only the
 * functions are bridged (main.js) for the sidebar/modal onclick handlers
 * and the boot-time refreshStorageUsage() call.
 */

import { esc, js } from "./utils.js";
import { apiGet, apiDelete } from "./api.js";
import { FM_CAT_LABELS } from "./constants.js";
import { act } from "./events.js";

const FM_CAT_ICONS = { upload: '📄', image: '🖼️', figure: '✨' };
let fmCurrentCategory = 'all';

export function fmFmtSize(bytes) {
  if (!bytes && bytes !== 0) return '—';
  const kb = bytes / 1024;
  if (kb < 1024) return kb.toFixed(1) + ' KB';
  const mb = kb / 1024;
  if (mb < 1024) return mb.toFixed(1) + ' MB';
  return (mb / 1024).toFixed(2) + ' GB';
}

export function fmFmtDate(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }).replace(/:\d+$/, ''); }
  catch (_) { return iso; }
}

export async function refreshStorageUsage() {
  let data;
  try {
    data = await apiGet('/api/storage/usage');
  } catch (_) { return; }
  if (!data) return;

  const txt = document.getElementById('storage-text');
  const fill = document.getElementById('storage-fill');
  if (!txt || !fill) return;

  if (data.is_admin) {
    txt.textContent = `${fmFmtSize(data.used_bytes)} · 管理员`;
    fill.style.width = '100%';
    fill.classList.remove('warn');
    fill.style.background = 'linear-gradient(90deg,#534AB7,#9B6FD4,#1D9E75)';
  } else {
    txt.textContent = `${data.used_mb}MB / ${data.limit_mb}MB`;
    fill.style.width = Math.min(data.percent, 100) + '%';
    if (data.percent >= 90) fill.classList.add('warn');
    else fill.classList.remove('warn');
  }
}

export function openFileManager() {
  const ov = document.getElementById('fm-overlay');
  if (!ov) return;
  ov.classList.add('open');
  fmCurrentCategory = 'all';
  document.querySelectorAll('.fm-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.cat === 'all');
  });
  loadFileList();
}

export function closeFileManager() {
  document.getElementById('fm-overlay')?.classList.remove('open');
}

export function fmSwitchTab(btn) {
  document.querySelectorAll('.fm-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  fmCurrentCategory = btn.dataset.cat;
  loadFileList();
}

export async function loadFileList() {
  const body = document.getElementById('fm-body');
  if (!body) return;
  body.innerHTML = '<div class="fm-empty">加载中…</div>';

  const url = fmCurrentCategory === 'all'
    ? '/api/files'
    : `/api/files?category=${encodeURIComponent(fmCurrentCategory)}`;

  let list = [];
  try {
    const d = await apiGet(url);
    list = d.files || [];
  } catch (_) { }

  if (!list.length) {
    body.innerHTML = '<div class="fm-empty">还没有文件 · 在对话里上传 PDF / 图片即可</div>';
  } else {
    body.innerHTML = list.map(f => `
      <div class="fm-row">
        <div class="fm-icon cat-${esc(f.category||'')}">${FM_CAT_ICONS[f.category] || '📎'}</div>
        <div class="fm-meta">
          <div class="fm-name" title="${esc(f.original_name||'')}">${esc(f.original_name || '未命名')}</div>
          <div class="fm-sub">${esc(FM_CAT_LABELS[f.category] || f.category || '')} · ${fmFmtSize(f.size_bytes)} · ${fmFmtDate(f.created_at)}</div>
        </div>
        <button class="fm-del" title="删除" ${act('fmDelete', f.id, '@self')}>🗑</button>
      </div>
    `).join('');
  }

  // Refresh the foot quota line
  try {
    const q = await apiGet('/api/storage/usage');
    const foot = document.getElementById('fm-quota');
    if (foot && q) {
      foot.innerHTML = q.is_admin
        ? `已用 <b>${fmFmtSize(q.used_bytes)}</b> · 管理员（无限额）`
        : `已用 <b>${q.used_mb}MB</b> / ${q.limit_mb}MB · 剩 ${q.free_mb}MB`;
    }
  } catch (_) { }
}

export async function fmDelete(fileId, btn) {
  if (!confirm('确认删除这个文件吗？')) return;
  btn.disabled = true;
  try {
    await apiDelete('/api/files/' + encodeURIComponent(fileId));
  } catch (err) {
    if (err.status) alert('删除失败');
    btn.disabled = false;
    return;
  }
  await loadFileList();
  await refreshStorageUsage();
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeFileManager();
});
