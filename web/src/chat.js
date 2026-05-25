/* Chat composer + message rendering + WS event router.
 *
 * Owns:
 *   send / sendText                     — outbound message
 *   addMsg                              — render one message bubble
 *   renderMessageActions / copy helpers — per-message hover actions
 *   streamAssistantText                 — typewriter effect on assistant reply
 *   renderEvaluation / evalClass        — RAG-eval chips block
 *   onMsg                               — dispatch on every WS frame
 *   updateCompression / clearPanels     — conversation-compression UI
 *   updateUsage                         — "N 次" usage gauge
 *   draftText / draftQuestion / draftLibraryQuestion — populate input box
 *   handleMessageLinkClick              — http(s) links → in-app preview
 *
 * Module-private state:
 *   turnCount, pendingImage, compressionState
 *
 * Deps imported from sibling modules (clean):
 *   utils, avatar, constants, profile, markdown, thinking, papers,
 *   session-list (currentSid / chatStarted / startChat / renderSessionList /
 *                 refreshPanel), library (loadLibraries),
 *   research-plan-card.
 *
 * Deps still read via window (extracted later or never):
 *   showChatView, hideMainViews, openWebPreview, loadNotes, ws */

import { mk, esc, toast, autoResize } from "./utils.js";
import { renderAvatar, renderAssistantAvatar } from "./avatar.js";
import { INTENT_LABELS } from "./constants.js";
import { currentProfile } from "./profile.js";
import { _mdToHtml, renderMarkdownInto } from "./markdown.js";
import { showThinking, removeThinking, setGenerating, setSend } from "./thinking.js";
import {
  updateFound, updateDownloaded, updateLibrary,
} from "./papers.js";
import { renderResearchPlanCheckpoint } from "./research-plan-card.js";
import {
  refreshPanel, renderSessionList, startChat, setChatStarted,
} from "./session-list.js";
import { loadLibraries } from "./library.js";
import { loadNotes } from "./notes.js";
import { openWebPreview } from "./web-preview.js";
import { showChatView } from "./nav.js";

// ── State ──────────────────────────────────────────────────────────────

let turnCount = 0;
let pendingImage = null;     // {image_path, image_url, filename}
let compressionState = null;

function _sync() {
  window.turnCount = turnCount;
  window.pendingImage = pendingImage;
  window.compressionState = compressionState;
}
_sync();

export const getPendingImage = () => pendingImage;
export function setPendingImage(v) {
  pendingImage = v;
  _sync();
}

// ── Outbound ───────────────────────────────────────────────────────────

export function send() {
  const inp = document.getElementById("user-input");
  const txt = inp.value.trim();
  const ws = window.ws;
  if (!txt || !ws || ws.readyState !== WebSocket.OPEN) return;
  sendText(txt);
  inp.value = "";
  autoResize(inp);
}

export function sendText(txt) {
  const sid = window.currentSid;
  if (!txt || !sid) return;
  if (!window.chatStarted) {
    startChat();
  }
  removeThinking();
  addMsg("user", txt, null);
  setGenerating(true);
  const ws = window.ws;
  if (ws?.readyState === WebSocket.OPEN) {
    const payload = { session_id: sid, message: txt };
    if (pendingImage?.image_path) payload.image_path = pendingImage.image_path;
    ws.send(JSON.stringify(payload));
  }
  pendingImage = null;
  turnCount += 1;
  _sync();
  updateUsage();
  // Update cached title on first user message
  const sessions = window.sessions || [];
  const s = sessions.find((x) => x.session_id === sid);
  if (s && s.message_count === 0) {
    s.title = txt.slice(0, 50);
    renderSessionList();
  }
  if (s) s.message_count++;
}

// ── Inbound: WS dispatcher ─────────────────────────────────────────────

export function onMsg(d) {
  if (d.type === "status") {
    showThinking(d);
    return;
  }
  if (d.type === "research_plan_checkpoint") {
    // Plan-approval checkpoint from ResearchAgent. Keep the thinking
    // indicator alive (the graph is still mid-flight) and render an
    // interactive card the user can act on.
    renderResearchPlanCheckpoint(d);
    return;
  }
  removeThinking();
  setGenerating(false);
  if (d.type === "stopped") {
    toast(d.text || "已停止回复");
    return;
  }
  if (d.type === "error") {
    addMsg("assistant", `**错误：** ${d.text}`, null);
    if (window.Sentry) {
      window.Sentry.captureMessage("chat_error: " + (d.text || "unknown"), { level: "error" });
    }
    return;
  }
  if (d.type === "reply") {
    addMsg("assistant", d.reply || "（无回复）", d.intent, d.evaluation || null, { stream: true });
    if (d.papers_found?.length) updateFound(d.papers_found);
    if (d.stored_papers) updateDownloaded(d.stored_papers);
    if (d.compression) updateCompression(d.compression);
    if (["add_to_library", "clear_temp_rag"].includes(d.intent)) {
      refreshPanel();
      loadLibraries();
    }
    if (d.intent && d.intent.includes("note") && typeof loadNotes === "function") {
      loadNotes();
    }
    if (d.intent === "clear_temp_rag") clearPanels();
  }
}

// ── Message bubble rendering ───────────────────────────────────────────

export function addMsg(role, text, intent, evaluation = null, options = {}) {
  const wrap = document.getElementById("messages");
  if (!wrap) return;
  const row = mk("div", `msg ${role}`);
  const av = mk("div", "av");
  if (role === "user") {
    av.classList.add("user-msg-av");
    renderAvatar(av, currentProfile.avatar, currentProfile.display_name || "研究者");
  } else {
    renderAssistantAvatar(av, intent);
  }

  const body = mk("div", "msg-body");
  if (role === "assistant" && intent && INTENT_LABELS[intent]) {
    const lbl = mk("span", "intent-lbl");
    lbl.textContent = INTENT_LABELS[intent];
    body.appendChild(lbl);
  }
  const bub = mk("div", "bubble");
  const inner = mk("div");
  bub.appendChild(inner);
  body.appendChild(bub);
  body.appendChild(renderMessageActions(text));
  row.appendChild(av);
  row.appendChild(body);
  wrap.appendChild(row);
  wrap.scrollTop = wrap.scrollHeight;

  const shouldStream = role === "assistant" && options.stream && text && text.length > 24;
  const appendEvaluation = () => {
    if (role === "assistant" && evaluation) {
      bub.appendChild(renderEvaluation(evaluation));
    }
    wrap.scrollTop = wrap.scrollHeight;
  };

  if (shouldStream) {
    streamAssistantText(inner, text, appendEvaluation);
  } else {
    renderMarkdownInto(inner, text);
    appendEvaluation();
  }
}

export function renderMessageActions(text) {
  const actions = mk("div", "msg-actions");
  const copyBtn = mk("button", "msg-action-btn");
  copyBtn.type = "button";
  copyBtn.title = "Copy message";
  copyBtn.textContent = "Copy";
  copyBtn.onclick = (e) => {
    e.stopPropagation();
    copyMessageText(text || "", copyBtn);
  };
  actions.appendChild(copyBtn);
  return actions;
}

async function copyMessageText(text, btn) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      fallbackCopyText(text);
    }
    const oldText = btn.textContent;
    btn.textContent = "Copied";
    btn.classList.add("copied");
    toast("已复制");
    window.setTimeout(() => {
      btn.textContent = oldText;
      btn.classList.remove("copied");
    }, 1200);
  } catch (e) {
    fallbackCopyText(text);
    toast("已复制");
  }
}

function fallbackCopyText(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  ta.style.top = "0";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}

// ── Streaming assistant reply ──────────────────────────────────────────

export function streamAssistantText(target, fullText, done) {
  const wrap = document.getElementById("messages");
  const cursor = '<span class="stream-cursor"></span>';
  let index = 0;
  const total = fullText.length;
  const step = () => {
    const remaining = total - index;
    const chunk = remaining > 500 ? 10 : remaining > 180 ? 6 : remaining > 80 ? 4 : 2;
    index = Math.min(total, index + chunk);
    const partial = fullText.slice(0, index);
    if (index < total) {
      target.innerHTML = _mdToHtml(partial) + cursor;
    } else {
      renderMarkdownInto(target, partial);
    }
    if (wrap) wrap.scrollTop = wrap.scrollHeight;
    if (index < total) {
      window.setTimeout(step, 16);
    } else if (done) {
      done();
    }
  };
  step();
}

// ── RAG-evaluation chips ───────────────────────────────────────────────

export function renderEvaluation(ev) {
  const strip = mk("div", "eval-strip");
  const worst = Math.min(
    ev.faithfulness ?? 0,
    ev.answer_relevancy ?? 0,
    ev.context_precision ?? 0,
  );
  const label = mk("span", `eval-chip ${evalClass(worst)}`);
  label.textContent = `${ev.label || "RAG评测"} · ${ev.backend || "eval"}`;
  strip.appendChild(label);
  [
    ["忠实", ev.faithfulness, "Faithfulness：回答是否被上下文支持"],
    ["相关", ev.answer_relevancy, "Answer Relevancy：回答是否切题，不是召回率"],
    ["检索", ev.context_precision, "Context Precision：检索片段是否相关"],
  ].forEach(([name, val, title]) => {
    if (typeof val !== "number") return;
    const chip = mk("span", `eval-chip ${evalClass(val)}`);
    chip.title = title || name;
    chip.textContent = `${name} ${val.toFixed(2)}`;
    strip.appendChild(chip);
  });
  if (ev.warning) {
    const warn = mk("div", "eval-warning");
    warn.textContent = ev.warning;
    strip.appendChild(warn);
  }
  if (ev.rationale) {
    const why = mk("div", "eval-warning");
    why.textContent = ev.rationale;
    strip.appendChild(why);
  }
  return strip;
}

export function evalClass(v) {
  if (v >= 0.7) return "ok";
  if (v >= 0.5) return "warn";
  return "risk";
}

// ── Side helpers used by onMsg + session reset ─────────────────────────

export function clearPanels() {
  updateFound([]);
  updateDownloaded([]);
  updateLibrary([]);
}

export function updateCompression(state) {
  compressionState = state;
  _sync();
  const bar = document.getElementById("compress-bar");
  const txt = document.getElementById("compress-text");
  if (!state) {
    if (bar) bar.style.display = "none";
    return;
  }
  if (bar) bar.style.display = "";
  if (txt) txt.textContent = `已压缩 ${state.compressed || 0}/${state.total || 0} 条历史消息`;
}

export function updateUsage() {
  const txtEl = document.getElementById("usage-txt");
  const fillEl = document.getElementById("usage-fill");
  if (txtEl) txtEl.textContent = `${turnCount} 次`;
  if (fillEl) fillEl.style.width = Math.min(turnCount * 3, 100) + "%";
}

// ── Draft helpers (populate the user input box) ────────────────────────

export function draftText(text) {
  if (typeof showChatView === "function") {
    showChatView(document.getElementById("nav-chat"));
  }
  if (!window.chatStarted) startChat();
  const inp = document.getElementById("user-input");
  if (!inp) return;
  inp.value = text;
  autoResize(inp);
  setSend(true);
  inp.focus();
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

export function draftQuestion(title) {
  draftText(`关于《${title}》，`);
  toast("已填入输入框，可补充问题后发送");
}

export function draftLibraryQuestion(title) {
  draftText(`请从我的知识库中，关于《${title}》，`);
  toast("已填入知识库提问草稿，可补充问题后发送");
}

// ── External link → in-app preview ─────────────────────────────────────

export function handleMessageLinkClick(event) {
  const link = event.target.closest?.("#messages .bubble a[href]");
  if (!link) return;
  const href = link.getAttribute("href") || "";
  if (!/^https?:\/\//i.test(href)) return;
  event.preventDefault();
  event.stopPropagation();
  if (typeof openWebPreview === "function") {
    openWebPreview(href, link.textContent?.trim() || href);
  }
}
