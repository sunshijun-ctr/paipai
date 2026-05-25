/* Thinking / progress bubble + small UI control helpers.
 *
 * The "thinking" bubble is the placeholder message that appears after
 * the user sends a query and before the assistant reply lands. It
 * shows:
 *   - a Chinese text label per pipeline stage (intent / route / plan …)
 *   - a percentage bar
 *   - the last five stage labels as breadcrumbs
 *
 * The WS server sends `{type:"status", step, text, pct}` events; this
 * module renders them. When the server falls silent (slow backend),
 * `startLocalProgress` animates a fake sequence so the UI doesn't sit
 * frozen.
 *
 * Also lives here because they share state with the thinking bubble:
 *   - setDot(cls)        WS connection indicator
 *   - setSend(on)        toggle send-button disabled state
 *   - setGenerating(on)  toggles input lock + stop button
 *   - isGenerating()     read accessor for stopGeneration() */

import { mk, esc } from "./utils.js";
import { renderAssistantAvatar } from "./avatar.js";

// Module-private state. Was top-level globals in the legacy script.
let thinkingEl = null;
let generating = false;
let progressSteps = [];
let progressTimer = null;

// ── Connection indicator + send/stop UI bits ────────────────────────────

export function setDot(cls) {
  const el = document.getElementById("ws-dot");
  if (el) el.className = cls;
}

export function setSend(on) {
  const el = document.getElementById("send-btn");
  if (el) el.disabled = !on;
}

export function setGenerating(on) {
  generating = on;
  setSend(!on);
  const stopBtn = document.getElementById("stop-btn");
  if (stopBtn) stopBtn.classList.toggle("on", on);
}

/** Read accessor so legacy `stopGeneration()` doesn't need to import
 *  the module-private `generating` variable. */
export function isGenerating() {
  return generating;
}

// ── Progress bubble ─────────────────────────────────────────────────────

const STAGE_LABELS = {
  start:      "接收问题，准备分析任务",
  intent:     "识别用户意图和需要调用的 Agent",
  route:      "确定执行路径",
  plan:       "规划工具和 Agent 调用",
  evaluation: "进行回答质量评测",
  done:       "完成",
};

function normalizeProgressText(text, step) {
  if (text && text !== "思考中…") return text;
  return STAGE_LABELS[step] || "正在分析任务";
}

function nextLocalProgressPct() {
  const last = progressSteps[progressSteps.length - 1]?.pct;
  if (typeof last === "number") return Math.min(last + 7, 88);
  return 12;
}

/** Animated fallback that runs every 1.8s when the server hasn't sent
 *  a real progress update yet. Caps at 82% so the bar doesn't sit at
 *  100% while we're still waiting. */
function startLocalProgress() {
  const fallback = [
    ["intent",   "识别用户意图和任务类型",       18],
    ["route",    "选择合适的 Agent 和工具",      30],
    ["work",     "检索上下文并组织中间结果",     48],
    ["answer",   "生成回答并检查依据",           68],
    ["finalize", "整理回复内容",                 82],
  ];
  let idx = 0;
  progressTimer = setInterval(() => {
    if (!thinkingEl || idx >= fallback.length) return;
    const [step, text, pct] = fallback[idx++];
    showThinking({ step, text, pct });
  }, 1800);
}

export function showThinking(status = {}) {
  const hasRealPct = typeof status.pct === "number";
  const text = normalizeProgressText(status.text || "思考中…", status.step);
  const pct = Math.max(0, Math.min(100, Number(status.pct ?? nextLocalProgressPct())));

  if (hasRealPct && progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }

  if (!thinkingEl) {
    progressSteps = [];
    const wrap = document.getElementById("messages");
    const row = mk("div", "msg assistant"); row.id = "think-row";
    const av  = mk("div", "av"); renderAssistantAvatar(av, status.intent);
    const body = mk("div", "msg-body");
    const bub  = mk("div", "bubble");
    bub.innerHTML = `
      <div class="progress-box">
        <div class="progress-head">
          <div class="progress-title" id="progress-title"></div>
          <div class="progress-pct" id="progress-pct"></div>
        </div>
        <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
        <div class="progress-steps" id="progress-steps"></div>
      </div>`;
    body.appendChild(bub);
    row.appendChild(av);
    row.appendChild(body);
    wrap.appendChild(row);
    thinkingEl = row;
  }

  const last = progressSteps[progressSteps.length - 1];
  if (!last || last.text !== text) {
    progressSteps.push({ step: status.step || "", text, pct });
    progressSteps = progressSteps.slice(-5);
  }

  const title = document.getElementById("progress-title");
  const pctEl = document.getElementById("progress-pct");
  const fill  = document.getElementById("progress-fill");
  const steps = document.getElementById("progress-steps");
  if (title) title.textContent = text;
  if (pctEl) pctEl.textContent = `${pct}%`;
  if (fill)  fill.style.width  = `${pct}%`;
  if (steps) {
    steps.innerHTML = progressSteps
      .map(
        (s, i) => `
      <div class="progress-step${i === progressSteps.length - 1 ? " active" : ""}">
        <span class="progress-dot"></span><span>${esc(s.text)}</span>
      </div>`,
      )
      .join("");
  }

  const wrap = document.getElementById("messages");
  wrap.scrollTop = wrap.scrollHeight;

  if (!hasRealPct && !progressTimer) startLocalProgress();
}

export function removeThinking() {
  if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
  progressSteps = [];
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
}
