/* Notes workspace: the "笔记" view — list + markdown editor + tag pills +
 * properties/outline panels + autosave + embed + AI shortcuts.
 *
 * All notes state is module-private (no still-inline code reads it), so —
 * like writing.js — nothing is mirrored onto window; only the functions are
 * bridged (main.js) so the onclick/oninput handlers in the notes-view HTML
 * (and the inline showNotesView) can find them by name.
 *
 * Dependencies on still-inline code (read via window):
 *   - confirmDialog (delete confirmation, still inline)
 *   - showChatView  (nav switch for the AI shortcuts, still inline)
 */

import { esc, js, fmtTime, toast } from "./utils.js";
import { apiGet, apiPost, apiPut, apiDelete } from "./api.js";
import { renderMarkdownInto } from "./markdown.js";
import { sendText } from "./chat.js";
import { act } from "./events.js";
import { confirmDialog } from "./confirm-dialog.js";
import { showChatView } from "./nav.js";

// ── State ──────────────────────────────────────────────────────────────
let notes = [];
let selectedNoteId = "";
let editingNewNote = false;
let _notePreviewMode = false;
let _nvAutoTimer = null;
const NOTES_SIDEBAR_COLLAPSED_KEY = "research-assistant-notes-sidebar-collapsed";

const _TC_DARK = [
  {bg:'rgba(29,158,117,.3)',color:'#9FE1CB'},{bg:'rgba(16,185,129,.2)',color:'#6ee7b7'},
  {bg:'rgba(245,158,11,.2)',color:'#fcd34d'},{bg:'rgba(239,68,68,.2)',color:'#fca5a5'},
  {bg:'rgba(59,130,246,.2)',color:'#93c5fd'},{bg:'rgba(236,72,153,.2)',color:'#f9a8d4'},
];
const _TC_LIGHT = [
  {bg:'#E8F6F1',color:'#0F6E56'},{bg:'#d1fae5',color:'#065f46'},
  {bg:'#fef3c7',color:'#92400e'},{bg:'#fee2e2',color:'#991b1b'},
  {bg:'#dbeafe',color:'#1e40af'},{bg:'#fce7f3',color:'#9d174d'},
];
const NOTE_DOT_COLORS = ['#1D9E75', '#5DCAA5', '#9FE1CB'];

document.addEventListener('DOMContentLoaded', bindNotesWorkspaceActions);
document.addEventListener('click', handleNotesDeleteClick, true);

function bindNotesWorkspaceActions() {
  document.querySelectorAll('#notes-view [data-act="deleteSelectedNote"]').forEach(btn => {
    btn.onclick = (ev) => deleteSelectedNote(ev);
  });
}

function handleNotesDeleteClick(ev) {
  const target = ev.target?.closest?.('[data-note-delete-id], #notes-view [data-act="deleteSelectedNote"]');
  if (!target) return;
  ev.preventDefault();
  ev.stopPropagation();
  ev.stopImmediatePropagation?.();
  if (target.hasAttribute('data-note-delete-id')) {
    deleteNoteById(target.getAttribute('data-note-delete-id'), ev);
  } else {
    deleteSelectedNote(ev);
  }
}

// ── List + sidebar ─────────────────────────────────────────────────────

export async function loadNotes() {
  const q = document.getElementById('note-search')?.value || '';
  try {
    const d = await apiGet(`/api/notes?q=${encodeURIComponent(q)}`);
    notes = d.notes || [];
    renderNotesList();
    if (selectedNoteId && !notes.some(n => n.id === selectedNoteId)) selectedNoteId = '';
  } catch (e) {
    document.getElementById('notes-list').innerHTML = '<div class="paper-empty">加载笔记失败</div>';
  }
}

export function applyNotesSidebarState() {
  const view = document.getElementById('notes-view');
  const btn = document.getElementById('notes-sidebar-toggle');
  const collapsed = localStorage.getItem(NOTES_SIDEBAR_COLLAPSED_KEY) === '1';
  view?.classList.toggle('notes-sidebar-collapsed', collapsed);
  if (btn) {
    btn.textContent = collapsed ? '»' : '«';
    btn.setAttribute('aria-expanded', String(!collapsed));
    btn.title = collapsed ? '展开笔记列表' : '收起笔记列表';
  }
}

export function toggleNotesSidebar() {
  const view = document.getElementById('notes-view');
  const collapsed = !view?.classList.contains('notes-sidebar-collapsed');
  localStorage.setItem(NOTES_SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0');
  applyNotesSidebarState();
}

export function _tcd(tag){const i=tag.split('').reduce((a,c)=>a+c.charCodeAt(0),0)%_TC_DARK.length;return _TC_DARK[i];}
export function _tcl(tag){const i=tag.split('').reduce((a,c)=>a+c.charCodeAt(0),0)%_TC_LIGHT.length;return _TC_LIGHT[i];}
export function _noteDotColor(index){return NOTE_DOT_COLORS[index % NOTE_DOT_COLORS.length];}

export function renderNotesList() {
  const list = document.getElementById('notes-list');
  const pinnedEl = document.getElementById('nv-pinned-list');
  const pinnedLbl = document.getElementById('nv-pinned-label');
  const footer = document.getElementById('nv-total-footer');
  if (!list) return;

  const q = (document.getElementById('note-search')?.value || '').toLowerCase();
  const filtered = q ? notes.filter(n =>
    (n.title||'').toLowerCase().includes(q) ||
    (n.content_markdown||'').toLowerCase().includes(q) ||
    (n.tags||[]).some(t=>t.toLowerCase().includes(q))
  ) : notes;

  const pinned = filtered.filter(n=>n.metadata?.pinned);
  const recent = filtered.filter(n=>!n.metadata?.pinned);

  if (pinnedLbl) pinnedLbl.style.display = pinned.length ? '' : 'none';
  if (pinnedEl) {
    pinnedEl.innerHTML = pinned.map((n, index) => {
      const preview = (n.content_markdown||'').replace(/^#+\s+/gm,'').replace(/\*\*/g,'').slice(0,80);
      const tagHtml = (n.tags||[]).slice(0,3).map(t=>{const c=_tcl(t);return `<span class="ntag-l" style="background:${c.bg};color:${c.color}">${esc(t)}</span>`;}).join('');
      return `<div class="nv-note-card${n.id===selectedNoteId?' active':''}" ${act('selectNote', n.id)}>
        <button class="nv-note-del" type="button" title="删除笔记" aria-label="删除笔记" data-note-delete-id="${esc(n.id)}">✕</button>
        <div class="nv-note-card-title"><span class="note-dot" style="background:${_noteDotColor(index)}"></span>${esc(n.title||'无标题')}</div>
        ${tagHtml?`<div class="nv-note-card-tags">${tagHtml}</div>`:''}
        ${preview?`<div class="nv-note-card-preview">${esc(preview)}</div>`:''}
        <div class="nv-note-card-footer">
          <span class="nv-note-card-time">${fmtTime(n.updated_at)}</span>
          <span class="nv-note-card-star" data-note-pin-id="${esc(n.id)}">★</span>
        </div></div>`;
    }).join('');
  }

  if (!recent.length) {
    list.innerHTML = '<div style="color:#7d70a8;padding:20px 14px;font-size:12px">暂无笔记</div>';
  } else {
    list.innerHTML = recent.map((n, index) => {
      const tagHtml = (n.tags||[]).slice(0,3).map(t=>{const c=_tcd(t);return `<span class="ntag-d" style="background:${c.bg};color:${c.color}">${esc(t)}</span>`;}).join('');
      return `<div class="nv-note-item${n.id===selectedNoteId?' active':''}" ${act('selectNote', n.id)}>
        <button class="nv-note-del" type="button" title="删除笔记" aria-label="删除笔记" data-note-delete-id="${esc(n.id)}">✕</button>
        <div class="nv-note-item-title"><span class="note-dot" style="background:${_noteDotColor(index)}"></span>${esc(n.title||'无标题')}</div>
        <div class="nv-note-item-row"><div class="nv-note-item-tags">${tagHtml}</div>
          <span class="nv-note-item-date">${fmtTime(n.updated_at)}</span></div></div>`;
    }).join('');
  }
  if (footer) footer.innerHTML = `<span>查看全部笔记 (${filtered.length})</span><span>→</span>`;
  bindNoteListActions();
}

function bindNoteListActions() {
  document.querySelectorAll('[data-note-delete-id]').forEach(btn => {
    btn.onclick = (ev) => deleteNoteById(btn.getAttribute('data-note-delete-id'), ev);
  });
  document.querySelectorAll('[data-note-pin-id]').forEach(btn => {
    btn.onclick = (ev) => _nvPin(btn.getAttribute('data-note-pin-id'), ev);
  });
}

// ── Properties / tags ──────────────────────────────────────────────────

export function _nvRefreshProps() {
  const note = notes.find(n=>n.id===selectedNoteId);
  const content = document.getElementById('note-content-input')?.value || '';
  const chars = content.length;
  const words = content.trim() ? content.trim().split(/[\s一-龥]/).filter(Boolean).length : 0;
  _q('#nv-p-created').textContent = note ? fmtTime(note.created_at) : '—';
  _q('#nv-p-updated').textContent = note ? fmtTime(note.updated_at) : '—';
  _q('#nv-p-words').textContent = `${words} 词`;
  _q('#nv-p-chars').textContent = `${chars} 字符`;
  _q('#nv-p-embed').textContent = note?.embedding_status || '—';
  _q('#nv-wc').textContent = `${chars} 字符`;
  const tags = (document.getElementById('note-tags-input')?.value||'').split(',').map(t=>t.trim()).filter(Boolean);
  const ptagEl = document.getElementById('nv-p-tags');
  if (ptagEl) ptagEl.innerHTML = tags.map(t=>{const c=_tcd(t);return `<span class="ntag-d" style="background:${c.bg};color:${c.color}">${esc(t)}</span>`;}).join('');
}

export function _q(sel){return document.querySelector(sel)||{textContent:''};}

export function _nvRenderTagPills() {
  const tags = (document.getElementById('note-tags-input')?.value||'').split(',').map(t=>t.trim()).filter(Boolean);
  const el = document.getElementById('nv-tag-pills');
  if (!el) return;
  el.innerHTML = tags.map((t,i)=>{const c=_tcl(t);return `<span class="ntag-editor" style="background:${c.bg};color:${c.color}">${esc(t)}<span class="ntag-x" ${act('_nvRmTag', i)}>✕</span></span>`;}).join('');
}

export function _nvRmTag(idx) {
  const inp = document.getElementById('note-tags-input');
  if (!inp) return;
  const tags = inp.value.split(',').map(t=>t.trim()).filter(Boolean);
  tags.splice(idx,1);
  inp.value = tags.join(', ');
  _nvRenderTagPills(); _nvRefreshProps();
}

export function _nvTagKey(e) {
  if (e.key==='Enter'||e.key===',') {
    e.preventDefault();
    const v = e.target.value.trim().replace(/,$/,'');
    if (!v) return;
    const inp = document.getElementById('note-tags-input');
    const tags = inp.value.split(',').map(t=>t.trim()).filter(Boolean);
    if (!tags.includes(v)) tags.push(v);
    inp.value = tags.join(', ');
    e.target.value = '';
    _nvRenderTagPills(); _nvRefreshProps(); _nvChange();
  }
}

// ── Change / autosave ──────────────────────────────────────────────────

export function _nvChange() {
  updateSaveBtnText(); _nvRefreshProps();
  clearTimeout(_nvAutoTimer);
  _nvAutoTimer = setTimeout(async ()=>{
    if (selectedNoteId && !editingNewNote) await _nvAutoSave();
  }, 2000);
}

export async function _nvAutoSave() {
  if (!selectedNoteId || editingNewNote) return;
  const title = document.getElementById('note-title-input').value.trim()||'新笔记';
  const content_markdown = document.getElementById('note-content-input').value;
  const tags = document.getElementById('note-tags-input').value.split(',').map(s=>s.trim()).filter(Boolean);
  const note_status = document.getElementById('nv-status-sel')?.value||'active';
  const note = notes.find(n=>n.id===selectedNoteId);
  try {
    await apiPut(`/api/notes/${encodeURIComponent(selectedNoteId)}`, {title,content_markdown,tags,source_type:'manual',metadata:{...(note?.metadata||{}),note_status}});
  } catch (e) { return; }
  const badge = document.getElementById('nv-autosave-badge');
  if (badge){badge.classList.add('show');setTimeout(()=>badge.classList.remove('show'),2500);}
  await loadNotes();
}

export function toggleNotePreview() {
  _notePreviewMode = !_notePreviewMode;
  const ta = document.getElementById('note-content-input');
  const pv = document.getElementById('note-preview');
  const btn = document.getElementById('preview-toggle-btn');
  if (!_notePreviewMode) {
    ta.style.display = ''; pv.style.display = 'none'; btn.textContent = '阅'; return;
  }
  renderMarkdownInto(pv, ta.value);
  ta.style.display = 'none'; pv.style.display = 'block'; btn.textContent = '写';
}

export function updateSaveBtnText() {
  const btn = document.getElementById('save-note-btn');
  if (!btn) return;
  btn.textContent = (editingNewNote||!selectedNoteId) ? '保存新笔记' : '更新笔记';
}

export function _exitPreviewMode() {
  if (_notePreviewMode) {
    _notePreviewMode = false;
    document.getElementById('note-content-input').style.display = '';
    document.getElementById('note-preview').style.display = 'none';
    const btn = document.getElementById('preview-toggle-btn');
    if (btn) btn.textContent = '阅';
  }
}

// ── Select / create / save / delete ────────────────────────────────────

export function selectNote(noteId) {
  selectedNoteId = noteId; editingNewNote = false;
  _exitPreviewMode();
  const note = notes.find(n=>n.id===noteId);
  if (!note) return;
  document.getElementById('note-title-input').value = note.title||'';
  document.getElementById('note-tags-input').value = (note.tags||[]).join(', ');
  document.getElementById('note-content-input').value = note.content_markdown||'';
  document.getElementById('note-status').textContent = `${note.embedding_status} · ${fmtTime(note.updated_at)}`;
  const pinBtn = document.getElementById('nv-pin-btn');
  if (pinBtn) {pinBtn.textContent = note.metadata?.pinned?'★':'☆'; pinBtn.classList.toggle('pinned',!!note.metadata?.pinned); pinBtn.style.color = note.metadata?.pinned ? '#f59e0b' : '';}
  const sel = document.getElementById('nv-status-sel');
  if (sel) sel.value = note.metadata?.note_status||'active';
  updateSaveBtnText(); renderNotesList(); _nvRenderTagPills(); _nvRefreshProps();
}

export function newNoteDraft() {
  selectedNoteId = ''; editingNewNote = true;
  _exitPreviewMode();
  document.getElementById('note-title-input').value = '';
  document.getElementById('note-tags-input').value = '';
  document.getElementById('note-content-input').value = '';
  document.getElementById('note-status').textContent = '新笔记';
  const pinBtn = document.getElementById('nv-pin-btn');
  if (pinBtn){pinBtn.textContent='☆';pinBtn.classList.remove('pinned');pinBtn.style.color='';}
  const sel = document.getElementById('nv-status-sel');
  if (sel) sel.value = 'active';
  updateSaveBtnText(); renderNotesList(); _nvRenderTagPills(); _nvRefreshProps();
}

export async function saveSelectedNote() {
  const title = document.getElementById('note-title-input').value.trim()||'新笔记';
  const content_markdown = document.getElementById('note-content-input').value;
  const tags = document.getElementById('note-tags-input').value.split(',').map(s=>s.trim()).filter(Boolean);
  const note_status = document.getElementById('nv-status-sel')?.value||'active';
  const note = selectedNoteId ? notes.find(n=>n.id===selectedNoteId) : null;
  const body = {title,content_markdown,tags,source_type:'manual',metadata:{...(note?.metadata||{}),note_status}};
  const shouldCreate = editingNewNote||!selectedNoteId;
  const url = shouldCreate ? '/api/notes' : `/api/notes/${encodeURIComponent(selectedNoteId)}`;
  let d;
  try {
    d = shouldCreate ? await apiPost(url, body) : await apiPut(url, body);
  } catch (e) { toast('保存笔记失败'); return; }
  toast('笔记已保存');
  await loadNotes();
  selectedNoteId = d.note.id; editingNewNote = false;
  updateSaveBtnText(); selectNote(selectedNoteId);
}

export async function deleteSelectedNote(ev) {
  ev?.preventDefault?.();
  ev?.stopPropagation?.();
  if (!selectedNoteId) {
    toast('请先在左侧选择要删除的笔记');
    return;
  }
  await deleteNoteById(selectedNoteId);
}

export async function deleteNoteById(noteId, ev) {
  ev?.preventDefault?.();
  ev?.stopPropagation?.();
  if (!noteId) return;
  const note = notes.find(n => n.id === noteId);
  const message = `笔记《${note?.title || noteId}》将被永久删除。`;
  if (!window.confirm(message)) return;
  try {
    const resp = await fetch(`/api/notes/${encodeURIComponent(noteId)}`, {
      method: 'DELETE',
      credentials: 'include',
    });
    if (resp.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.replace(`/login?next=${next}`);
      return;
    }
    if (!resp.ok && resp.status !== 204) {
      let detail = '';
      try {
        const body = await resp.json();
        detail = body?.detail ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)) : '';
      } catch {}
      throw new Error(`HTTP ${resp.status}${detail ? `：${detail}` : ''}`);
    }
  } catch (err) {
    toast(`删除失败：${err.message || err}`);
    return;
  }
  clearTimeout(_nvAutoTimer);
  notes = notes.filter(n => n.id !== noteId);
  if (selectedNoteId === noteId) {
    selectedNoteId = '';
    newNoteDraft();
  } else {
    renderNotesList();
  }
  await loadNotes();
  toast('已删除');
}

export async function embedSelectedNote() {
  if (!selectedNoteId){toast('请先选择或保存笔记');return;}
  let d;
  try {
    d = await apiPost(`/api/notes/${encodeURIComponent(selectedNoteId)}/embed`);
  } catch (e) { toast('向量化失败'); return; }
  toast(`已写入 ${d.chunks_indexed||0} 个片段`);
  await loadNotes(); selectNote(selectedNoteId);
}

export function exportSelectedNotePdf() {
  if (!selectedNoteId || editingNewNote) {
    toast('请先保存笔记，再导出 PDF');
    return;
  }
  window.location.href = `/api/notes/${encodeURIComponent(selectedNoteId)}/export.pdf`;
}

// ── Props / outline panels ─────────────────────────────────────────────

export function _nvToggleProps() {
  const panel = document.getElementById('nv-props-panel');
  const btn = document.getElementById('nv-props-toggle');
  if (!panel) return;
  panel.classList.toggle('hidden');
  if (btn) btn.classList.toggle('active', !panel.classList.contains('hidden'));
}

export async function toggleNotePin() {
  if (!selectedNoteId) return;
  await _nvPin(selectedNoteId);
}

export async function _nvPin(noteId, ev) {
  ev?.preventDefault?.();
  ev?.stopPropagation?.();
  const note = notes.find(n=>n.id===noteId);
  if (!note) return;
  const pinned = !(note.metadata?.pinned);
  try {
    await apiPut(`/api/notes/${encodeURIComponent(noteId)}`, {title:note.title,content_markdown:note.content_markdown,
      tags:note.tags,source_type:note.source_type,metadata:{...(note.metadata||{}),pinned}});
  } catch (e) {}
  await loadNotes();
  if (noteId===selectedNoteId) selectNote(selectedNoteId);
}

export function _nvTab(tab) {
  document.getElementById('nv-tab-props').classList.toggle('active',tab==='props');
  document.getElementById('nv-tab-outline').classList.toggle('active',tab==='outline');
  document.getElementById('nv-panel-props').style.display = tab==='props'?'':'none';
  document.getElementById('nv-panel-outline').style.display = tab==='outline'?'':'none';
  if (tab==='outline') _nvOutline();
}

export function _nvOutline() {
  const content = document.getElementById('note-content-input')?.value||'';
  const el = document.getElementById('nv-outline');
  if (!el) return;
  const headings = [...content.matchAll(/^(#{1,3})\s+(.+)/gm)].map(m=>({l:m[1].length,t:m[2]}));
  el.innerHTML = headings.length
    ? headings.map(h=>`<div class="nv-outline-item nv-outline-h${h.l}" ${act('_nvScrollTo', h.t)}>${esc(h.t)}</div>`).join('')
    : '<div style="color:#7d70a8;font-size:12px;padding:4px">暂无标题</div>';
}

export function _nvScrollTo(text) {
  const ta = document.getElementById('note-content-input');
  if (!ta) return;
  const idx = ta.value.indexOf(text);
  if (idx>=0){ta.focus();ta.setSelectionRange(idx,idx+text.length);}
}

// ── AI shortcuts ───────────────────────────────────────────────────────

export function _nvAI(action) {
  const title = document.getElementById('note-title-input')?.value||'这篇笔记';
  const msgs = {
    summarize:`请帮我总结笔记《${title}》的核心内容`,
    explain_formula:`请解释笔记《${title}》中的数学公式`,
    expand:`请帮我扩展笔记《${title}》的相关内容和背景知识`,
    mindmap:`请为笔记《${title}》生成一个思维导图（用Markdown列表格式）`
  };
  const chatNavEl = document.querySelector('.nav-item[onclick*="showChatView"]');
  if (chatNavEl) showChatView(chatNavEl);
  setTimeout(()=>sendText(msgs[action]||`帮我分析笔记《${title}》`),150);
}

export function _nvAIAsk() {
  const inp = document.getElementById('nv-ai-q');
  if (!inp?.value.trim()) return;
  const msg = inp.value.trim(); inp.value = '';
  const chatNavEl = document.querySelector('.nav-item[onclick*="showChatView"]');
  if (chatNavEl) showChatView(chatNavEl);
  setTimeout(()=>sendText(msg),150);
}

// ── Toolbar formatting helpers ─────────────────────────────────────────

export function _nfmt(before, after) {
  const ta = document.getElementById('note-content-input');
  if (!ta) return;
  const s=ta.selectionStart, e=ta.selectionEnd, sel=ta.value.slice(s,e)||'文本';
  ta.value = ta.value.slice(0,s)+before+sel+after+ta.value.slice(e);
  ta.setSelectionRange(s+before.length, s+before.length+sel.length);
  ta.focus(); _nvChange();
}

export function _nfmtLine(prefix) {
  const ta = document.getElementById('note-content-input');
  if (!ta) return;
  const s=ta.selectionStart;
  const ls = ta.value.lastIndexOf('\n',s-1)+1;
  const has = ta.value.slice(ls).startsWith(prefix);
  if (has){ta.value=ta.value.slice(0,ls)+ta.value.slice(ls+prefix.length);ta.setSelectionRange(s-prefix.length,s-prefix.length);}
  else{ta.value=ta.value.slice(0,ls)+prefix+ta.value.slice(ls);ta.setSelectionRange(s+prefix.length,s+prefix.length);}
  ta.focus(); _nvChange();
}

export function _nfmtBlock(before, after) {
  const ta = document.getElementById('note-content-input');
  if (!ta) return;
  const s=ta.selectionStart, e=ta.selectionEnd, sel=ta.value.slice(s,e);
  ta.value = ta.value.slice(0,s)+before+sel+after+ta.value.slice(e);
  ta.setSelectionRange(s+before.length, s+before.length+sel.length);
  ta.focus(); _nvChange();
}
