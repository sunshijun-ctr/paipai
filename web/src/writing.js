/* Academic writing workspace: the "学术写作" view.
 *
 * A self-contained chat surface (polish / rewrite / supplement / imitate)
 * with its own per-browser history (localStorage), settings pills, and
 * material uploads. None of this state is read by the still-inline code,
 * so unlike the other islands it does NOT mirror onto window — only the
 * functions are bridged (main.js) so the onclick handlers in the HTML it
 * generates, plus the DOMContentLoaded boot, can find them by name.
 *
 * Dependencies on still-inline code (read via window):
 *   - window.currentSid     (owned by session-list.js, bridged)
 *   - confirmDialog  (delete confirmation modal, still inline)
 *   - showWritingView (nav view switch, still inline)
 */

import { mk, esc, js, toast } from "./utils.js";
import { apiPost } from "./api.js";
import { WRITING_LABELS } from "./constants.js";
import { updateDownloaded } from "./papers.js";
import { act, actChange } from "./events.js";
import { confirmDialog } from "./confirm-dialog.js";

// ── State ──────────────────────────────────────────────────────────────
let writingKbEnabled = false;
let writingChatMessages = [];
let writingBusy = false;
let writingUploadedFiles = [];

const WRITING_HISTORY_KEY = "writing-history-v1";
const WRITING_SIDEBAR_KEY = "writing-history-sidebar-collapsed";
let writingHistory = { sessions: [], activeId: null };

// ── View rendering ─────────────────────────────────────────────────────

export function writingPillGroupHtml(id, label, options, activeValue) {
  return `
    <div class="academic-pill-group">
      <span class="academic-pill-label">${label}</span>
      <span class="academic-pill-options" id="${id}">
        ${options.map(([value, text]) => `<button class="academic-pill${value === activeValue ? ' active' : ''}" type="button" data-value="${value}" ${act('writingPick', '@self', `${id}`)}>${text}</button>`).join('')}
      </span>
    </div>`;
}

export function writingDividerHtml() {
  return '<span class="academic-setting-divider" aria-hidden="true"></span>';
}

export function writingRenderAcademicChat(view) {
  view.innerHTML = `
    <aside id="writing-history-sidebar" class="writing-history-sidebar" aria-label="历史对话">
      <div class="wh-head">
        <span class="wh-title">历史对话</span>
        <button class="wh-toggle" type="button" ${act('toggleWritingHistorySidebar')} title="收起 / 展开" aria-label="收起或展开">‹</button>
      </div>
      <button class="wh-new" type="button" ${act('newWritingSession')} title="新建写作对话">
        <span aria-hidden="true">＋</span><span class="wh-new-text">新建对话</span>
      </button>
      <div id="writing-history-list" class="wh-list"></div>
    </aside>
    <section class="academic-chat-card" aria-label="学术写作助手">
      <div class="academic-top-zone">
        <div class="academic-chat-top">
          <div class="academic-chat-heading">
            <div class="academic-chat-title">● 学术写作</div>
            <div class="academic-chat-subtitle">面向论文润色、改写、补充论述与风格模仿</div>
          </div>
          <button class="academic-chat-clear" type="button" ${act('writingReset')}>清空</button>
        </div>
        <div class="academic-chat-settings">
          ${writingPillGroupHtml('writing-lang','语言',[['zh','中文'],['en','English']],'zh')}
          ${writingDividerHtml()}
          ${writingPillGroupHtml('writing-style','写作风格',[['academic','学术'],['formal','正式'],['concise','简洁'],['review','综述']],'academic')}
          ${writingDividerHtml()}
          ${writingPillGroupHtml('writing-length','篇幅',[['short','短'],['medium','中'],['long','长']],'short')}
          ${writingDividerHtml()}
          ${writingPillGroupHtml('writing-mode','生成模式',[['polish','润色'],['rewrite','改写'],['supplement','补充论述'],['imitate','模仿写作']],'polish')}
        </div>
        <div class="academic-top-tools">
          <button class="academic-tool-btn" type="button" ${act('__clickEl','writing-file-input')}>📎 上传素材</button>
          <button id="academic-kb-btn" class="academic-tool-btn" type="button" ${act('writingToggleKb')}>🗄 知识库</button>
          <input id="writing-file-input" type="file" multiple accept=".pdf,.pptx,.txt,.md,.text,.rst" style="display:none" ${actChange('uploadWritingFiles', '@self')}>
          <div id="writing-upload-list" class="write-upload-list" style="align-items:center"></div>
        </div>
        <div class="academic-top-divider"></div>
      </div>
      <div id="academic-chat-thread" class="academic-chat-thread"></div>
      <div class="academic-chat-input">
        <div class="academic-compose">
          <textarea id="academic-chat-input" rows="3" placeholder="输入需要处理的段落、写作要求或风格样例…"></textarea>
          <button id="academic-send-btn" class="academic-send-btn" type="button" ${act('writingSubmit')} aria-label="发送">↑</button>
        </div>
      </div>
    </section>`;
  writingKbEnabled = false;
  renderWritingUploads();
  writingBindInput();
  loadWritingHistory();
  applyWritingSidebarCollapsed();
  if (writingHistory.activeId && writingHistory.sessions.some(s => s.id === writingHistory.activeId)) {
    switchWritingSession(writingHistory.activeId);
  } else {
    writingAppendGreeting();
  }
  renderWritingHistoryList();
}

export function renderSimpleWritingView() {
  const view = document.getElementById('writing-view');
  if (!view || view.dataset.simple === '1') return;
  view.dataset.simple = '1';
  writingRenderAcademicChat(view);
}

export function writingPick(btn, groupId) {
  const group = document.getElementById(groupId);
  if (!group) return;
  group.querySelectorAll('.write-chip-btn,.academic-pill').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

export function writingVal(groupId) {
  return document.querySelector(`#${groupId} .write-chip-btn.active,#${groupId} .academic-pill.active`)?.dataset.value || '';
}

export function writingSettings() {
  return {
    lang: writingVal('writing-lang') || 'zh',
    style: writingVal('writing-style') || 'academic',
    length: writingVal('writing-length') || 'short',
    mode: writingVal('writing-mode') || 'polish',
    kb: writingKbEnabled
  };
}

export function writingSettingsTag(settings = writingSettings()) {
  return `${WRITING_LABELS[settings.mode]} · ${WRITING_LABELS[settings.lang]} · ${WRITING_LABELS[settings.style]} · ${WRITING_LABELS[settings.length]}`;
}

export function writingBindInput() {
  const input = document.getElementById('academic-chat-input');
  if (!input) return;
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      writingSubmit();
    }
  });
}

export function writingThread() {
  return document.getElementById('academic-chat-thread');
}

export function writingAppendGreeting() {
  writingChatMessages = [];
  const thread = writingThread();
  if (thread) thread.innerHTML = '';
  writingAppendBubble('ai', '你好，我可以帮你做学术润色、改写、补充论述和模仿写作。选择上方设置后，把原文或写作要求发给我就行。', writingSettings());
}

export function writingAppendBubble(role, text, settings = null) {
  const thread = writingThread();
  if (!thread) return null;
  const row = mk('div', `academic-msg ${role}`);
  const bubble = mk('div', 'academic-bubble');
  if (role === 'ai' && settings) {
    const tag = mk('div', 'academic-tag');
    tag.textContent = writingSettingsTag(settings);
    bubble.appendChild(tag);
  }
  const body = mk('div');
  body.textContent = text || '';
  bubble.appendChild(body);
  row.appendChild(bubble);
  thread.appendChild(row);
  thread.scrollTop = thread.scrollHeight;
  return row;
}

export function writingShowTyping(settings) {
  const row = writingAppendBubble('ai', '', settings);
  const body = row?.querySelector('.academic-bubble > div:last-child');
  if (body) body.innerHTML = '<span class="academic-typing"><span></span><span></span><span></span></span>';
  return row;
}

export function writingToggleKb() {
  writingKbEnabled = !writingKbEnabled;
  document.getElementById('academic-kb-btn')?.classList.toggle('active', writingKbEnabled);
}

export async function writingCallAnthropic(settings) {
  let data;
  try {
    data = await apiPost('/api/academic-writing-chat', { settings, messages: writingChatMessages });
  } catch (err) {
    const detail = err.body?.detail || err.body?.error || err.message;
    throw new Error(detail || `后端请求失败：${err.status}`);
  }
  return String(data.reply || '').trim() || '（无回复）';
}

// ── Material uploads ───────────────────────────────────────────────────

export function renderWritingUploads() {
  const box = document.getElementById('writing-upload-list');
  if (!box) return;
  box.innerHTML = writingUploadedFiles.map((f, i) => `
    <div class="write-file-pill ok">
      <span title="${esc(f.name)}">${esc(f.name)}</span>
      <button class="write-file-del" title="移除" ${act('removeWritingFile', i)}>×</button>
    </div>
  `).join('');
}

export function removeWritingFile(index) {
  const item = writingUploadedFiles[index];
  writingUploadedFiles.splice(index, 1);
  renderWritingUploads();
  if (item) toast(`已从写作素材中移除：${item.name}`);
}

export async function uploadWritingFiles(input) {
  const files = [...(input.files || [])];
  input.value = '';
  if (!files.length || !window.currentSid) return;
  const box = document.getElementById('writing-upload-list');
  for (const file of files) {
    if (box) box.insertAdjacentHTML('beforeend', `<div class="write-file-pill"><span>${esc(file.name)} 上传中...</span></div>`);
    const form = new FormData();
    form.append('file', file);
    try {
      const r = await fetch(`/api/upload?session_id=${encodeURIComponent(window.currentSid)}`, { method: 'POST', body: form, credentials: 'include' });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        if (box) box.insertAdjacentHTML('beforeend', `<div class="write-file-pill err"><span>${esc(file.name)} 上传失败</span></div>`);
        toast(`上传失败：${e.detail || file.name}`);
        continue;
      }
      const d = await r.json();
      writingUploadedFiles.push({ name: file.name, paper: d.paper });
      updateDownloaded(d.stored_papers || []);
      renderWritingUploads();
      toast(`已上传写作素材：${file.name}`);
    } catch (err) {
      if (box) box.insertAdjacentHTML('beforeend', `<div class="write-file-pill err"><span>${esc(file.name)} 上传出错</span></div>`);
      toast(`上传出错：${err.message}`);
    }
  }
}

// ── Compose / submit ───────────────────────────────────────────────────

export async function writingSubmit() {
  const input = document.getElementById('academic-chat-input');
  if (!input) return;
  const text = input.value.trim();
  if (!text || writingBusy) return;
  const settings = writingSettings();
  input.value = '';
  ensureWritingActiveSession();
  writingAppendBubble('user', text);
  writingChatMessages.push({ role: 'user', content: text });
  persistActiveWritingSession();
  const typing = writingShowTyping(settings);
  writingBusy = true;
  const sendBtn = document.getElementById('academic-send-btn');
  if (sendBtn) sendBtn.disabled = true;
  try {
    const reply = await writingCallAnthropic(settings);
    typing?.remove();
    writingAppendBubble('ai', reply, settings);
    writingChatMessages.push({ role: 'assistant', content: reply, settings });
    persistActiveWritingSession();
  } catch (err) {
    typing?.remove();
    const errMsg = `请求失败：${err.message || err}`;
    writingAppendBubble('ai', errMsg, settings);
    writingChatMessages.push({ role: 'assistant', content: errMsg, settings });
    persistActiveWritingSession();
  } finally {
    writingBusy = false;
    if (sendBtn) sendBtn.disabled = false;
    input.focus();
  }
}

export function writingClear() {
  const chatInput = document.getElementById('academic-chat-input');
  if (chatInput) chatInput.value = '';
}

export function writingReset() {
  writingKbEnabled = false;
  document.getElementById('academic-kb-btn')?.classList.remove('active');
  [['writing-lang', 'zh'], ['writing-style', 'academic'], ['writing-length', 'short'], ['writing-mode', 'polish']].forEach(([id, val]) => {
    document.querySelectorAll(`#${id} .academic-pill`).forEach(b => b.classList.remove('active'));
    document.querySelector(`#${id} .academic-pill[data-value="${val}"]`)?.classList.add('active');
  });
  const input = document.getElementById('academic-chat-input');
  if (input) input.value = '';
  newWritingSession();
}

// ── Writing history (per-browser, localStorage) ─────────────────────────

export function loadWritingHistory() {
  try {
    const raw = localStorage.getItem(WRITING_HISTORY_KEY);
    const data = raw ? JSON.parse(raw) : null;
    if (data && Array.isArray(data.sessions)) {
      writingHistory = { sessions: data.sessions, activeId: data.activeId || null };
      return;
    }
  } catch (_e) {}
  writingHistory = { sessions: [], activeId: null };
}

export function saveWritingHistory() {
  try {
    localStorage.setItem(WRITING_HISTORY_KEY, JSON.stringify(writingHistory));
  } catch (_e) {}
}

export function activeWritingSession() {
  return writingHistory.sessions.find(s => s.id === writingHistory.activeId) || null;
}

export function deriveWritingTitle(messages) {
  const firstUser = (messages || []).find(m => m && m.role === 'user');
  const text = String(firstUser?.content || '').trim().replace(/\s+/g, ' ');
  if (!text) return '新对话';
  return text.length > 28 ? text.slice(0, 28) + '…' : text;
}

export function ensureWritingActiveSession() {
  let s = activeWritingSession();
  if (s) return s;
  s = {
    id: `ws-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    title: '新对话',
    createdAt: Date.now(),
    updatedAt: Date.now(),
    messages: [],
  };
  writingHistory.sessions.unshift(s);
  writingHistory.activeId = s.id;
  saveWritingHistory();
  renderWritingHistoryList();
  return s;
}

export function persistActiveWritingSession() {
  const s = activeWritingSession();
  if (!s) return;
  s.messages = writingChatMessages.slice();
  s.title = deriveWritingTitle(s.messages);
  s.updatedAt = Date.now();
  writingHistory.sessions = [s, ...writingHistory.sessions.filter(x => x.id !== s.id)];
  saveWritingHistory();
  renderWritingHistoryList();
}

export function formatWritingTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  if (d.toDateString() === now.toDateString()) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function renderWritingHistoryList() {
  const box = document.getElementById('writing-history-list');
  if (!box) return;
  if (!writingHistory.sessions.length) {
    box.innerHTML = '<div class="wh-empty">暂无历史对话<br>开始一段新写作吧</div>';
    return;
  }
  box.innerHTML = writingHistory.sessions.map(s => {
    const userCount = (s.messages || []).filter(m => m && m.role === 'user').length;
    return `
      <div class="wh-item${s.id === writingHistory.activeId ? ' active' : ''}" ${act('switchWritingSession', s.id)}>
        <div class="wh-item-title">${esc(s.title || '新对话')}</div>
        <div class="wh-item-meta">${esc(formatWritingTime(s.updatedAt || s.createdAt))} · ${userCount} 条提问</div>
        <button class="wh-item-del" type="button" title="删除该对话" aria-label="删除该对话" ${act('deleteWritingSession', s.id)}>✕</button>
      </div>`;
  }).join('');
}

export function switchWritingSession(id) {
  const s = writingHistory.sessions.find(x => x.id === id);
  if (!s) return;
  writingHistory.activeId = id;
  saveWritingHistory();
  renderWritingHistoryList();
  writingChatMessages = (s.messages || []).slice();
  const thread = writingThread();
  if (!thread) return;
  thread.innerHTML = '';
  if (!writingChatMessages.length) {
    writingAppendBubble('ai', '你好，我可以帮你做学术润色、改写、补充论述和模仿写作。选择上方设置后，把原文或写作要求发给我就行。', writingSettings());
    return;
  }
  writingChatMessages.forEach(m => {
    const role = m.role === 'user' ? 'user' : 'ai';
    const settings = m.role === 'assistant' ? (m.settings || writingSettings()) : null;
    writingAppendBubble(role, m.content, settings);
  });
}

export async function deleteWritingSession(id) {
  const s = writingHistory.sessions.find(x => x.id === id);
  const ok = await confirmDialog(`将永久删除对话「${s?.title || '未命名'}」及其所有消息。`, { title: '删除该写作对话？', okText: '删除' });
  if (!ok) return;
  writingHistory.sessions = writingHistory.sessions.filter(x => x.id !== id);
  if (writingHistory.activeId === id) {
    writingHistory.activeId = writingHistory.sessions[0]?.id || null;
    if (writingHistory.activeId) {
      switchWritingSession(writingHistory.activeId);
    } else {
      writingChatMessages = [];
      const thread = writingThread();
      if (thread) thread.innerHTML = '';
      writingAppendGreeting();
    }
  }
  saveWritingHistory();
  renderWritingHistoryList();
}

export function newWritingSession() {
  writingHistory.activeId = null;
  saveWritingHistory();
  writingChatMessages = [];
  const thread = writingThread();
  if (thread) thread.innerHTML = '';
  writingAppendGreeting();
  renderWritingHistoryList();
}

export function applyWritingSidebarCollapsed() {
  const sb = document.getElementById('writing-history-sidebar');
  if (!sb) return;
  const collapsed = localStorage.getItem(WRITING_SIDEBAR_KEY) === '1';
  sb.classList.toggle('collapsed', collapsed);
  const t = sb.querySelector('.wh-toggle');
  if (t) t.textContent = collapsed ? '›' : '‹';
}

export function toggleWritingHistorySidebar() {
  const sb = document.getElementById('writing-history-sidebar');
  if (!sb) return;
  const collapsed = !sb.classList.contains('collapsed');
  sb.classList.toggle('collapsed', collapsed);
  localStorage.setItem(WRITING_SIDEBAR_KEY, collapsed ? '1' : '0');
  const t = sb.querySelector('.wh-toggle');
  if (t) t.textContent = collapsed ? '›' : '‹';
}

// The 写作助手 nav item is wired directly in index.html now
// (id="nav-writing" data-act="showWritingView"), so the old runtime rewire
// (initWritingNav) is gone. renderSimpleWritingView fills #writing-view on load.
document.addEventListener('DOMContentLoaded', renderSimpleWritingView);
