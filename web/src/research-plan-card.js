/* ResearchAgent plan-approval card (Phase D HITL).
 *
 * Appended to the chat thread when the backend sends a
 * `research_plan_checkpoint` WS message. Users can approve / modify
 * (textarea JSON) / cancel; on click we POST to /api/research/{task_id}/resume.
 * On timeout (no click) the backend auto-approves.
 *
 * Island-mode dependencies — the legacy inline code in index.html
 * provides `mk`, `renderAssistantAvatar`, and `toast` on the global
 * `window`. We read them here at call time so this module stays usable
 * during the transition. They migrate into their own modules in later
 * Phase 2.3 commits. */

import { mk, toast } from "./utils.js";
import { renderAssistantAvatar } from "./avatar.js";

const TOOL_ZH_LABELS = {
  paper_search:   "搜索论文（外部）",
  library_search: "搜索文献库",
  web_search:     "搜索网页",
  web_fetch:      "抓取网页",
  note_search:    "查找便签",
  note_list:      "列出便签",
};

export function renderResearchPlanCheckpoint(d) {
  const wrap = document.getElementById("messages");
  if (!wrap) return;

  // Helpers still live on the legacy inline script for now.
  const mk = mk;
  const renderAssistantAvatar = renderAssistantAvatar;
  const toast = toast;
  if (typeof mk !== "function") {
    console.error("plan-card: mk is missing — legacy bridge broken?");
    return;
  }

  // LangGraph re-runs the approve_node after resume, which fires the
  // plan-checkpoint event a second time. Dedupe per task_id.
  const existing = wrap.querySelector(`.research-plan-card[data-task-id="${d.task_id || ""}"]`);
  if (existing) return;
  wrap.querySelectorAll(".research-plan-card").forEach((n) => n.remove());

  const card = mk("div", "msg assistant research-plan-card");
  card.dataset.taskId = d.task_id || "";

  const av = mk("div", "av");
  if (typeof renderAssistantAvatar === "function") {
    renderAssistantAvatar(av, "research_task");
  }

  const body = mk("div", "msg-body");
  const bub = mk("div", "bubble plan-bubble");

  const title = mk("div", "plan-title");
  const steps = (d.plan && d.plan.steps) || [];
  title.textContent = `调研计划已就绪 · ${steps.length} 步，等待你的确认`;
  bub.appendChild(title);

  const thinking = (d.plan && d.plan.thinking) || "";
  if (thinking) {
    const t = mk("div", "plan-thinking");
    t.textContent = thinking;
    bub.appendChild(t);
  }

  const list = mk("ol", "plan-steps");
  steps.forEach((s) => {
    const li = mk("li");
    const tag = mk("code", "plan-tool");
    tag.textContent = TOOL_ZH_LABELS[s.tool] || s.tool;
    const txt = document.createTextNode(" " + JSON.stringify(s.args || {}, null, 0));
    li.appendChild(tag);
    li.appendChild(txt);
    list.appendChild(li);
  });
  bub.appendChild(list);

  const countdown = mk("div", "plan-countdown");
  let remaining = parseInt(d.timeout_secs || 60, 10);
  countdown.textContent = `${remaining}s 后自动按原计划执行`;
  bub.appendChild(countdown);

  const actions = mk("div", "plan-actions");
  const btnGo = mk("button", "plan-btn plan-btn-go"); btnGo.textContent = "▶ 开始";
  const btnModify = mk("button", "plan-btn plan-btn-modify"); btnModify.textContent = "✏ 修改";
  const btnCancel = mk("button", "plan-btn plan-btn-cancel"); btnCancel.textContent = "✕ 取消";
  actions.appendChild(btnGo);
  actions.appendChild(btnModify);
  actions.appendChild(btnCancel);
  bub.appendChild(actions);

  // The interval needs to see `actions` AND `list` in scope — define after.
  const timerId = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearInterval(timerId);
      // Don't call /resume here — backend's own timeout handles it.
      countdown.textContent = "已自动按原计划开始执行…";
      list.querySelectorAll("button").forEach((b) => (b.disabled = true));
      actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
    } else {
      countdown.textContent = `${remaining}s 后自动按原计划执行`;
    }
  }, 1000);

  const finalize = (label) => {
    clearInterval(timerId);
    countdown.textContent = label;
    actions.querySelectorAll("button").forEach((b) => (b.disabled = true));
  };

  const postResume = async (payload) => {
    try {
      const r = await fetch(`/api/research/${encodeURIComponent(d.task_id)}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const detail = await r.json().catch(() => ({}));
        throw new Error(detail.detail || `HTTP ${r.status}`);
      }
      return true;
    } catch (exc) {
      if (typeof toast === "function") toast("确认失败：" + (exc.message || exc));
      return false;
    }
  };

  btnGo.onclick = async () => {
    finalize("已开始执行…");
    await postResume({ action: "approve" });
  };

  btnCancel.onclick = async () => {
    finalize("已取消");
    await postResume({ action: "cancel" });
  };

  btnModify.onclick = () => {
    list.style.display = "none";
    btnModify.style.display = "none";
    const editor = mk("textarea", "plan-editor");
    editor.rows = Math.min(12, steps.length + 4);
    editor.value = JSON.stringify(d.plan, null, 2);
    bub.insertBefore(editor, countdown);
    const btnSave = mk("button", "plan-btn plan-btn-go");
    btnSave.textContent = "✓ 提交修改后的计划";
    actions.insertBefore(btnSave, btnGo);
    btnGo.style.display = "none";
    btnSave.onclick = async () => {
      let parsed;
      try {
        parsed = JSON.parse(editor.value);
      } catch (e) {
        if (typeof toast === "function") toast("JSON 解析失败：" + e.message);
        return;
      }
      finalize("已用修改后的计划执行…");
      await postResume({ action: "modify", modified_plan: parsed });
    };
  };

  body.appendChild(bub);
  card.appendChild(av);
  card.appendChild(body);
  wrap.appendChild(card);
  wrap.scrollTop = wrap.scrollHeight;
}
