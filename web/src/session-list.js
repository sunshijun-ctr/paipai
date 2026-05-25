/* Chat session lifecycle + left-sidebar session list.
 *
 * Module owns:
 *   sessions        — full session list cached from /api/sessions
 *   currentSid      — active session id
 *   chatStarted     — has the user sent a first message in this session
 *
 * Each of these three is also kept on `window.*` after every mutation so
 * the still-inline `sendText()` / `send()` / `updateUsage()` keep
 * working without imports. When those move to chat.js, drop the
 * window-bridge code.
 *
 * Reader-agent sessions (one per PDF read session) are hidden from the
 * UI list — only normal chats show in the sidebar. */

import { apiGet, apiPost, apiDelete } from "./api.js";
import { esc } from "./utils.js";
import { act } from "./events.js";
import { addMsg, clearPanels, updateCompression } from "./chat.js";
import { confirmDialog } from "./confirm-dialog.js";
import { showChatView } from "./nav.js";
import { updateDownloaded, updateFound, updateLibrary } from "./papers.js";

// ── Constants (used here + by inline chat code) ─────────────────────────

export const READING_AGENT_SESSION_PREFIX = "research-agent-reading-session:";
export const READING_AGENT_SPLIT_KEY = "research-agent-reading-split";

// ── Module-owned state ──────────────────────────────────────────────────

let sessions = [];      // [{session_id, title, message_count, updated_at}]
let currentSid = null;
let chatStarted = false;

function _sync() {
  // Push state to window so legacy inline reads stay consistent. Reassign
  // each variable on each call — reading `window.sessions` from inline
  // chat code reflects the latest value.
  window.sessions = sessions;
  window.currentSid = currentSid;
  window.chatStarted = chatStarted;
}
_sync();

// Public accessors for code that prefers explicit imports later
export const getSessions = () => sessions;
export const getCurrentSid = () => currentSid;
export const getChatStarted = () => chatStarted;
export function setChatStarted(v) {
  chatStarted = !!v;
  _sync();
}

// ── Helpers ─────────────────────────────────────────────────────────────

/** A "reading-agent session" is one we create per PDF reader open;
 *  it's hidden from the main sidebar. Either the id starts with
 *  "reading_" or localStorage has a mapping flagging it. */
export function isReadingAgentSession(sessionId) {
  const sid = String(sessionId || "");
  if (sid.startsWith("reading_")) return true;
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i) || "";
    if (key.startsWith(READING_AGENT_SESSION_PREFIX) && localStorage.getItem(key) === sid) {
      return true;
    }
  }
  return false;
}

// ── Rendering ───────────────────────────────────────────────────────────

export function renderSessionList() {
  const el = document.getElementById("recent-list");
  if (!el) return;
  const visible = sessions.filter((s) => !isReadingAgentSession(s.session_id));
  if (!visible.length) {
    el.innerHTML = '<div class="recent-empty">暂无历史对话</div>';
    return;
  }
  el.innerHTML = visible
    .map(
      (s) => `
    <div class="recent-item${s.session_id === currentSid ? " active" : ""}"
         ${act('switchSession', s.session_id)}>
      <span class="recent-title">${esc(s.title || "新建对话")}</span>
      <button class="recent-del" title="删除" ${act('deleteSession', s.session_id, '@event')}>✕</button>
    </div>`,
    )
    .join("");
}

function resetChatUI() {
  const messages = document.getElementById("messages");
  if (messages) messages.innerHTML = "";
  document.getElementById("welcome")?.classList.remove("gone");
  document.getElementById("chat-view")?.classList.remove("on");
  chatStarted = false;
  // Reach back into still-inline helpers — these move into their own
  // modules later.
  if (typeof clearPanels === "function") clearPanels();
  if (typeof updateCompression === "function") updateCompression(null);
  // Inline state still owned by the chat code; nuke from there too.
  window.storedPapers = [];
  window.pendingImage = null;
  _sync();
}

export function startChat() {
  chatStarted = true;
  document.getElementById("welcome")?.classList.add("gone");
  document.getElementById("chat-view")?.classList.add("on");
  _sync();
}

// ── Session CRUD ────────────────────────────────────────────────────────

export async function initSessions() {
  try {
    const d = await apiGet("/api/sessions");
    sessions = d.sessions || [];
    _sync();
    const visible = sessions.filter((s) => !isReadingAgentSession(s.session_id));
    if (visible.length > 0) {
      await switchSession(visible[0].session_id, false, true);
    } else {
      await createSession(true);
    }
  } catch (e) {
    console.warn("initSessions failed", e);
    await createSession(true);
  }
}

export async function createSession(preserveView = false) {
  try {
    const d = await apiPost("/api/sessions");
    currentSid = d.session_id;
    sessions.unshift({
      session_id: d.session_id,
      title: "新建对话",
      message_count: 0,
      updated_at: "",
    });
    sessions = sessions.slice(0, 30);
    _sync();
    renderSessionList();
    resetChatUI();
    if (!preserveView && typeof showChatView === "function") {
      showChatView(document.getElementById("nav-chat"));
    }
  } catch (e) {
    console.warn("createSession failed", e);
  }
}

export async function switchSession(sid, scrollBottom = true, preserveView = false) {
  currentSid = sid;
  _sync();
  resetChatUI();
  if (!preserveView && typeof showChatView === "function") {
    showChatView(document.getElementById("nav-chat"));
  }
  try {
    const d = await apiGet(`/api/sessions/${sid}`);
    const hist = d.conversation_history || [];
    if (hist.length > 0) {
      chatStarted = true;
      _sync();
      document.getElementById("welcome")?.classList.add("gone");
      document.getElementById("chat-view")?.classList.add("on");
      hist.forEach((m) => {
        if (typeof addMsg === "function") addMsg(m.role, m.content, null);
      });
      if (scrollBottom) {
        const wrap = document.getElementById("messages");
        if (wrap) wrap.scrollTop = wrap.scrollHeight;
      }
    }
    if (typeof updateFound === "function") updateFound(d.found_papers || []);
    if (typeof updateDownloaded === "function") updateDownloaded(d.stored_papers || []);
    if (typeof updateLibrary === "function") updateLibrary(d.library || []);
    if (typeof updateCompression === "function") updateCompression(d.compression || null);
  } catch (e) {
    console.warn("switchSession failed", e);
  }
  renderSessionList();
}

export async function deleteSession(sid, ev) {
  ev?.stopPropagation?.();
  const ok = typeof confirmDialog === "function"
    ? await confirmDialog("删除后将无法恢复。", { title: "删除这条对话记录？", okText: "删除" })
    : window.confirm("删除这条对话记录？");
  if (!ok) return;
  try {
    await apiDelete(`/api/sessions/${sid}`);
  } catch (e) {
    // intentionally swallow — server may have already deleted; we drop locally
  }
  sessions = sessions.filter((s) => s.session_id !== sid);
  _sync();
  if (currentSid === sid) {
    const visible = sessions.filter((s) => !isReadingAgentSession(s.session_id));
    if (visible.length > 0) {
      await switchSession(visible[0].session_id);
    } else {
      await createSession();
    }
  } else {
    renderSessionList();
  }
}

export async function refreshPanel() {
  if (!currentSid) return;
  try {
    const d = await apiGet(`/api/sessions/${currentSid}`);
    if (typeof updateFound === "function") updateFound(d.found_papers || []);
    if (typeof updateDownloaded === "function") updateDownloaded(d.stored_papers || []);
    if (typeof updateLibrary === "function") updateLibrary(d.library || []);
  } catch (e) {
    // silent — used as a soft refresh on WS reconnect
  }
}

export async function newChat() {
  await createSession();
}
