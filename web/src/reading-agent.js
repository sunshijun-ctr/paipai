/* Single-paper reading agent: the "文献阅读" workbench (PDF on one side,
 * Q&A chat on the other) plus the small "open a library doc in the reader"
 * helper. One backend chat session per paper, persisted in localStorage so
 * re-opening a paper restores its Q&A history.
 *
 * State (readingAgentState) is module-private — no still-inline code reads
 * it — so only the functions are bridged (main.js) for the onclick handlers
 * in the reading-agent-view HTML and the library.js doc-list buttons.
 *
 * Dependencies on still-inline code (read via window):
 *   - confirmDialog     (clear-history confirmation)
 *   - hideMainViews     (view switching)
 *   - showLibraryManager(返回 library on close)
 *   - window.sessions / window.currentSid (session-list.js state, mutated
 *     here exactly as the inline code did — see clearReadingAgentSession)
 */

import { mk } from "./utils.js";
import { apiGet, apiPost, apiDelete } from "./api.js";
import { renderMarkdownInto } from "./markdown.js";
import { openReader } from "./reader.js";
import { confirmDialog } from "./confirm-dialog.js";
import { hideMainViews, showLibraryManager } from "./nav.js";
import {
  READING_AGENT_SESSION_PREFIX, READING_AGENT_SPLIT_KEY,
  isReadingAgentSession, renderSessionList, switchSession, createSession,
} from "./session-list.js";

// ── State ──────────────────────────────────────────────────────────────
let readingAgentState = { paperId: '', sessionId: '', title: '', fileUrl: '', busy: false };

// ── Open a library doc directly in the PDF reader ──────────────────────

export function openLibraryDoc(lib_id, title) {
  const url = `/api/libraries/${encodeURIComponent(lib_id)}/documents/file?title=${encodeURIComponent(title)}`;
  openReader({ url, title, docId: `library:${lib_id}:${title}` });
}

// ── Reading-agent workbench ────────────────────────────────────────────

export async function openReadingAgent(lib_id, title) {
  const fileUrl = `/api/libraries/${encodeURIComponent(lib_id)}/documents/file?title=${encodeURIComponent(title)}`;
  const paperId = `${lib_id}:${title}`;
  const savedSessionId = localStorage.getItem(readingAgentSessionKey(paperId)) || '';
  readingAgentState = {
    paperId,
    sessionId: savedSessionId,
    title,
    fileUrl,
    busy: false,
  };
  hideMainViews();
  document.getElementById('reading-agent-view')?.classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
  const titleEl = document.getElementById('reading-agent-title');
  const subEl = document.getElementById('reading-agent-sub');
  const list = document.getElementById('reading-agent-messages');
  const paperTitle = document.getElementById('reading-agent-paper-title');
  initReadingAgentSplit();
  if (titleEl) titleEl.textContent = title || '文献阅读';
  if (subEl) subEl.textContent = `知识库：${lib_id}`;
  if (paperTitle) paperTitle.textContent = title || 'PDF';
  if (list) list.innerHTML = '';
  setReadingAgentFrame(fileUrl);
  if (savedSessionId) {
    appendReadingAgentMessage('system', '正在恢复这篇文献的历史问答...');
    await loadReadingAgentHistory(savedSessionId);
  } else {
    appendReadingAgentMessage('system', '已进入单篇文献问答。你可以直接追问方法、结论、实验设置或某个段落细节。');
  }
  const input = document.getElementById('reading-agent-input');
  if (input) {
    input.value = '';
    input.focus();
  }
}

export function readingAgentSessionKey(paperId) {
  return `${READING_AGENT_SESSION_PREFIX}${paperId}`;
}

export async function clearReadingAgentSession() {
  if (!readingAgentState.paperId) return;
  if (!await confirmDialog('当前文献的所有问答记录都会被清空。', { title: '清空问答历史？', okText: '清空' })) return;
  const sessionId = readingAgentState.sessionId;
  if (sessionId) {
    try {
      await apiDelete(`/api/sessions/${encodeURIComponent(sessionId)}`);
    } catch (e) {}
    window.sessions = (window.sessions || []).filter(s => s.session_id !== sessionId);
    if (window.currentSid === sessionId) {
      window.currentSid = null;
      const visibleSessions = (window.sessions || []).filter(s => !isReadingAgentSession(s.session_id));
      if (visibleSessions.length) await switchSession(visibleSessions[0].session_id, false, true);
      else await createSession(true);
    }
  }
  localStorage.removeItem(readingAgentSessionKey(readingAgentState.paperId));
  readingAgentState.sessionId = '';
  const list = document.getElementById('reading-agent-messages');
  if (list) list.innerHTML = '';
  appendReadingAgentMessage('system', '已清空这篇文献的问答历史，可以重新开始提问。');
  renderSessionList();
}

export function closeReadingAgent() {
  setReadingAgentFrame('');
  readingAgentState = { paperId: '', sessionId: '', title: '', fileUrl: '', busy: false };
  const nav = document.getElementById('nav-library');
  if (nav) showLibraryManager(nav);
}

export function setReadingAgentFrame(url) {
  const frame = document.getElementById('reading-agent-frame');
  const paper = document.querySelector('.reading-agent-paper');
  const empty = document.getElementById('reading-agent-frame-empty');
  if (!frame || !paper) return;
  if (!url) {
    frame.removeAttribute('src');
    paper.classList.add('is-empty');
    if (empty) empty.textContent = '未选择文献。';
    return;
  }
  paper.classList.remove('is-empty');
  if (empty) empty.textContent = '正在加载文献...';
  frame.src = url;
}

export function setReadingAgentSplit(percent, persist = false) {
  const workbench = document.querySelector('.reading-agent-workbench');
  if (!workbench) return;
  const value = Math.max(34, Math.min(72, Number(percent) || 58));
  workbench.style.setProperty('--reading-paper-width', `${value}%`);
  if (persist) localStorage.setItem(READING_AGENT_SPLIT_KEY, String(Math.round(value)));
}

export function initReadingAgentSplit() {
  setReadingAgentSplit(localStorage.getItem(READING_AGENT_SPLIT_KEY) || 58);
  const resizer = document.getElementById('reading-agent-resizer');
  const workbench = document.querySelector('.reading-agent-workbench');
  if (!resizer || !workbench || resizer.dataset.ready === '1') return;
  resizer.dataset.ready = '1';
  let dragging = false;

  const update = clientX => {
    const rect = workbench.getBoundingClientRect();
    if (!rect.width) return;
    const percent = ((clientX - rect.left) / rect.width) * 100;
    setReadingAgentSplit(percent);
  };

  resizer.addEventListener('pointerdown', e => {
    e.preventDefault();
    dragging = true;
    resizer.classList.add('dragging');
    resizer.setPointerCapture?.(e.pointerId);
    update(e.clientX);
  });
  resizer.addEventListener('pointermove', e => {
    if (dragging) update(e.clientX);
  });
  const stop = e => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove('dragging');
    try { resizer.releasePointerCapture?.(e.pointerId); } catch (_e) {}
    const raw = getComputedStyle(workbench).getPropertyValue('--reading-paper-width');
    setReadingAgentSplit(parseFloat(raw), true);
  };
  resizer.addEventListener('pointerup', stop);
  resizer.addEventListener('pointercancel', stop);
}

export function openReadingAgentFile() {
  if (!readingAgentState.fileUrl) return;
  openReader({
    url: readingAgentState.fileUrl,
    title: readingAgentState.title || '文献阅读',
    docId: `library:${readingAgentState.paperId}`,
  });
}

export async function loadReadingAgentHistory(sessionId) {
  const list = document.getElementById('reading-agent-messages');
  try {
    const d = await apiGet(`/api/sessions/${encodeURIComponent(sessionId)}`);
    const history = (d.conversation_history || [])
      .filter(m => ['user', 'assistant'].includes(m.role) && m.content);
    if (list) list.innerHTML = '';
    if (!history.length) {
      appendReadingAgentMessage('system', '已进入单篇文献问答。你可以直接追问方法、结论、实验设置或某个段落细节。');
      return;
    }
    history.forEach(m => appendReadingAgentMessage(m.role, m.content));
    appendReadingAgentMessage('system', '已恢复历史问答，可以继续追问。');
  } catch (err) {
    if (list) list.innerHTML = '';
    if (readingAgentState.paperId) {
      localStorage.removeItem(readingAgentSessionKey(readingAgentState.paperId));
      readingAgentState.sessionId = '';
    }
    appendReadingAgentMessage('system', '历史问答恢复失败，已开启新的文献问答。');
  }
}

export function setReadingAgentBusy(on) {
  readingAgentState.busy = on;
  const btn = document.getElementById('reading-agent-send');
  const input = document.getElementById('reading-agent-input');
  if (btn) {
    btn.disabled = on;
    btn.textContent = on ? '思考中...' : '发送';
  }
  if (input) input.disabled = on;
}

export function appendReadingAgentMessage(role, content, sources = []) {
  const list = document.getElementById('reading-agent-messages');
  if (!list) return;
  const item = mk('div', `reading-msg ${role}`);
  const body = mk('div', 'msg-content');
  if (role === 'assistant') renderMarkdownInto(body, content || '');
  else body.textContent = content || '';
  item.appendChild(body);
  if (role === 'assistant' && sources && sources.length) {
    const wrap = mk('div', 'reading-agent-sources');
    sources.slice(0, 5).forEach((src, idx) => {
      const detail = mk('details', 'reading-source');
      const summary = mk('summary');
      const page = src.page ? ` · p.${src.page}` : '';
      const section = src.section ? ` · ${src.section}` : '';
      summary.textContent = `来源 ${idx + 1}${page}${section}`;
      const snippet = mk('div', 'reading-source-snippet');
      snippet.textContent = src.snippet || src.chunk_id || '';
      detail.appendChild(summary);
      detail.appendChild(snippet);
      wrap.appendChild(detail);
    });
    item.appendChild(wrap);
  }
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;
}

export async function sendReadingAgentQuestion() {
  if (readingAgentState.busy || !readingAgentState.paperId) return;
  const input = document.getElementById('reading-agent-input');
  const question = (input?.value || '').trim();
  if (!question) return;
  if (input) input.value = '';
  appendReadingAgentMessage('user', question);
  setReadingAgentBusy(true);
  try {
    const d = await apiPost('/api/library_qa', {
      paper_id: readingAgentState.paperId,
      question,
      session_id: readingAgentState.sessionId,
    });
    readingAgentState.sessionId = d.session_id || readingAgentState.sessionId;
    if (readingAgentState.paperId && readingAgentState.sessionId) {
      localStorage.setItem(readingAgentSessionKey(readingAgentState.paperId), readingAgentState.sessionId);
    }
    appendReadingAgentMessage('assistant', d.answer || '没有生成回答。', d.sources || []);
  } catch (err) {
    if (input && !input.value) input.value = question;
    const msg = err.body?.detail || err.body?.message || err.message || err;
    appendReadingAgentMessage('system', `请求失败：${msg}`);
  } finally {
    setReadingAgentBusy(false);
    input?.focus();
  }
}
