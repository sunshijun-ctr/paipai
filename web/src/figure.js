/* Figure generation panel.
 *
 * Two-step UI:
 *   1. Upload a paper PDF (optional) — backend extracts context.
 *   2. User writes a brief; we ask the prompt LLM to flesh it out;
 *      then ask the image model for an actual rendered figure.
 *
 * History is kept in localStorage (60 most-recent entries). Each
 * entry stores the prompt + image URL so the user can reload an
 * old composition without re-paying for generation.
 *
 * NOTE: several user-visible strings in this module are damaged `?`
 *  characters in the source HTML. They were Chinese originally but got
 *  re-encoded with a lossy codec at some prior commit. Preserved as-is
 *  during extraction so the visible UI doesn't change.
 *
 * Dependencies: utils (esc, mk, toast).  Reads `window.currentSid`
 * from session-list.js. */

import { esc, mk, toast } from "./utils.js";
import { apiPost } from "./api.js";

// ── State + constants ──────────────────────────────────────────────────

let currentFigurePaperId = "";
let currentFigureDownloadUrl = "";
let currentFigureImageUrl = "";

function _sync() {
  window.currentFigurePaperId = currentFigurePaperId;
  window.currentFigureDownloadUrl = currentFigureDownloadUrl;
  window.currentFigureImageUrl = currentFigureImageUrl;
}
_sync();

const FIGURE_HISTORY_KEY = "research_assistant_figure_history_v1";

const FIGURE_BRIEF_HIDDEN_PROMPT = [
  "Interpret the user figure requirement as a scientific illustration request.",
  "If the user is vague, infer a useful paper figure goal from the uploaded paper context.",
  "Prefer concrete visual instructions: layout, modules, arrows, labels, visual hierarchy, and what to avoid.",
  "Keep the final generated prompt faithful to the paper and suitable for academic publication.",
].join("\n");

// ── Paper upload (extracts context for the prompt LLM) ─────────────────

export async function figPickPaper(input) {
  const file = input.files?.[0];
  const sid = window.currentSid;
  if (!file || !sid) return;
  currentFigurePaperId = "";
  currentFigureDownloadUrl = "";
  _sync();
  document.getElementById("fig-paper-name").textContent = `${file.name} ? uploading...`;
  const form = new FormData();
  form.append("file", file);
  try {
    // Multipart — can't go through apiPost (JSON-only). Raw fetch + credentials.
    const r = await fetch(
      `/api/figure/upload?session_id=${encodeURIComponent(sid)}`,
      { method: "POST", body: form, credentials: "include" },
    );
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const d = await r.json();
    currentFigurePaperId = d.paper_id || "";
    _sync();
    document.getElementById("fig-paper-name").textContent =
      `${d.filename || file.name} ? ${d.char_count || 0} chars ? ${(d.sections || []).slice(0, 4).join(" / ")}`;
    toast("????????? prompt");
  } catch (e) {
    document.getElementById("fig-paper-name").textContent = `${file.name} ? ???????`;
    toast(`???????${e.message || e}`);
  }
}

// ── Prompt-text generation ─────────────────────────────────────────────

export async function figGeneratePrompt() {
  const type = document.getElementById("fig-type")?.value || "method";
  const style = document.getElementById("fig-style")?.value || "paper";
  const brief = document.getElementById("fig-brief")?.value.trim() || "";
  const promptBox = document.getElementById("fig-prompt");
  promptBox.value = "Generating prompt...";
  try {
    const d = await apiPost("/api/figure/prompt", {
      paper_id: currentFigurePaperId,
      brief,
      hidden_brief_prompt: FIGURE_BRIEF_HIDDEN_PROMPT,
      figure_type: type,
      style,
    });
    promptBox.value = d.prompt || "";
  } catch (e) {
    promptBox.value = brief || "";
    toast(`?? prompt ???${e.body?.detail || e.message || e}`);
  }
}

export function figAppendPrompt() {
  const extra = prompt("??? prompt ????");
  if (!extra) return;
  const el = document.getElementById("fig-prompt");
  el.value = `${el.value.trim()}\n${extra}`.trim();
}

export function figClearPrompt() {
  document.getElementById("fig-prompt").value = "";
}

// ── Image rendering ────────────────────────────────────────────────────

export async function figGenerateImage() {
  const promptText = document.getElementById("fig-prompt")?.value.trim();
  if (!promptText) {
    toast("??????? prompt");
    return;
  }
  const ratio = document.getElementById("fig-ratio")?.value || "4:3";
  const negative = document.getElementById("fig-negative")?.value.trim() || "";
  const canvas = document.getElementById("fig-canvas");
  canvas.classList.add("is-loading");
  canvas.textContent = "Generating image...";
  currentFigureDownloadUrl = "";
  currentFigureImageUrl = "";
  _sync();
  try {
    const d = await apiPost("/api/figure/generate", { prompt: promptText, negative, ratio });
    currentFigureDownloadUrl = d.download_url || "";
    currentFigureImageUrl = d.image_url || currentFigureDownloadUrl;
    _sync();
    if (d.image_url) {
      canvas.classList.remove("is-loading");
      canvas.innerHTML = "";
      const img = document.createElement("img");
      img.src = d.image_url;
      img.alt = "Generated figure";
      canvas.appendChild(img);
      figAddHistory({
        prompt: promptText,
        negative,
        ratio,
        image_url: d.image_url,
        download_url: d.download_url || d.image_url,
        model: d.model || "",
        media_type: d.media_type || "image/png",
      });
    } else {
      canvas.classList.remove("is-loading");
      canvas.innerHTML = "<div>??????????????</div>";
    }
  } catch (e) {
    canvas.classList.remove("is-loading");
    canvas.innerHTML = `<div>???????${esc(e.body?.detail || e.message || e)}</div>`;
  }
}

export function figSaveMock(showToast = true) {
  const promptText = document.getElementById("fig-prompt")?.value.trim();
  if (currentFigureDownloadUrl) {
    const a = document.createElement("a");
    a.href = currentFigureDownloadUrl;
    a.download = currentFigureDownloadUrl.split("/").pop() || "figure.png";
    document.body.appendChild(a);
    a.click();
    a.remove();
    if (showToast) toast("??????");
    return;
  }
  if (!promptText || !showToast) return;
  toast("????????????");
}

// ── History (localStorage) ─────────────────────────────────────────────

export function figLoadHistory() {
  try {
    const raw = localStorage.getItem(FIGURE_HISTORY_KEY);
    const items = raw ? JSON.parse(raw) : [];
    return Array.isArray(items) ? items : [];
  } catch (e) {
    console.warn("figure history load failed", e);
    return [];
  }
}

export function figSaveHistory(items) {
  localStorage.setItem(FIGURE_HISTORY_KEY, JSON.stringify(items.slice(0, 60)));
}

export function figAddHistory(entry) {
  const items = figLoadHistory();
  const item = {
    id: `fig_${Date.now()}_${Math.random().toString(16).slice(2)}`,
    created_at: new Date().toISOString(),
    ...entry,
  };
  figSaveHistory([item, ...items]);
  figRenderHistory();
}

export function figRenderHistory() {
  const box = document.getElementById("fig-history");
  if (!box) return;
  const items = figLoadHistory();
  box.innerHTML = "";
  if (!items.length) {
    const empty = mk("div", "fig-thumb");
    empty.innerHTML = '<div class="fig-thumb-body">No generated figures yet.</div>';
    box.appendChild(empty);
    return;
  }
  items.forEach((item) => box.appendChild(_historyNode(item)));
}

function _historyNode(item) {
  const card = mk("div", "fig-thumb");
  const imgBox = mk("div", "fig-thumb-img");
  if (item.image_url) {
    const img = document.createElement("img");
    img.src = item.image_url;
    img.alt = "Generated figure history item";
    imgBox.appendChild(img);
  } else {
    imgBox.textContent = "No image";
  }
  const body = mk("div", "fig-thumb-body");
  const promptEl = mk("div", "fig-thumb-prompt");
  promptEl.textContent = item.prompt || "Untitled figure";
  const meta = mk("div", "fig-thumb-meta");
  meta.textContent = `${_formatTime(item.created_at)} · ${item.ratio || "4:3"}`;
  const actions = mk("div", "fig-thumb-actions");
  const loadBtn = mk("button", "sm-btn");
  loadBtn.textContent = "载入";
  loadBtn.onclick = () => figRestoreHistory(item.id);
  const dlBtn = mk("button", "sm-btn");
  dlBtn.textContent = "下载";
  dlBtn.onclick = () => figDownloadHistory(item.id);
  const delBtn = mk("button", "sm-btn");
  delBtn.textContent = "删除";
  delBtn.onclick = () => figDeleteHistory(item.id);
  actions.append(loadBtn, dlBtn, delBtn);
  body.append(promptEl, meta, actions);
  card.append(imgBox, body);
  return card;
}

export function figRestoreHistory(id) {
  const item = figLoadHistory().find((x) => x.id === id);
  if (!item) return;
  document.getElementById("fig-prompt").value = item.prompt || "";
  document.getElementById("fig-negative").value = item.negative || "";
  document.getElementById("fig-ratio").value = item.ratio || "4:3";
  currentFigureDownloadUrl = item.download_url || item.image_url || "";
  currentFigureImageUrl = item.image_url || currentFigureDownloadUrl;
  _sync();
  const canvas = document.getElementById("fig-canvas");
  if (currentFigureImageUrl) {
    canvas.classList.remove("is-loading");
    canvas.innerHTML = "";
    const img = document.createElement("img");
    img.src = currentFigureImageUrl;
    img.alt = "Generated figure";
    canvas.appendChild(img);
  }
}

export function figDownloadHistory(id) {
  const item = figLoadHistory().find((x) => x.id === id);
  const url = item?.download_url || item?.image_url;
  if (!url) return;
  const a = document.createElement("a");
  a.href = url;
  a.download = url.split("/").pop() || "figure.png";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function figDeleteHistory(id) {
  figSaveHistory(figLoadHistory().filter((x) => x.id !== id));
  figRenderHistory();
}

function _formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
