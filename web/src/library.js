/* Library / knowledge-base management.
 *
 * Covers:
 *   - Left "Library" tab strip in the chat sidebar (loadLibraries /
 *     renderLibTabs / switchLib / loadLibDocs / removeLibDoc / createLib
 *     / deleteLib / uploadToLibrary)
 *   - Standalone Paper Manager view (renderPaperManager / sort+filter
 *     across all libraries)
 *
 * Module owns the state:
 *   libraries        — array of {lib_id, name, doc_count, ...}
 *   activeLibId      — current tab in the sidebar
 *   managerLibId     — current tab in the paper manager
 *   allLibraryDocs   — flat list of docs across libraries, for the manager
 *
 * Each mutation re-syncs to window so the still-inline code that reads
 * `libraries.find(...)` etc. sees the same array reference. */

import { apiGet, apiPost, apiDelete } from "./api.js";
import { mk, esc, js, fmtTime, toast } from "./utils.js";
import { act } from "./events.js";
import { confirmDialog } from "./confirm-dialog.js";
import { updateDownloaded, updateLibraryProgress } from "./papers.js";

// ── State ──────────────────────────────────────────────────────────────

let libraries = [];           // [{lib_id, name, doc_count}]
let activeLibId = "lt_docs";  // default knowledge base
let managerLibId = "all";
let allLibraryDocs = [];

function _sync() {
  window.libraries = libraries;
  window.activeLibId = activeLibId;
  window.managerLibId = managerLibId;
  window.allLibraryDocs = allLibraryDocs;
}
_sync();

export const getLibraries = () => libraries;
export const getActiveLibId = () => activeLibId;

// ── Sidebar library tabs + doc list ─────────────────────────────────────

export async function loadLibraries() {
  try {
    const d = await apiGet("/api/libraries");
    libraries = d.libraries || [];
    _sync();
    const total = libraries.reduce((s, l) => s + (l.doc_count || 0), 0);
    const stEl = document.getElementById("st-lib");
    if (stEl) stEl.textContent = total;
    renderLibTabs();
    await loadLibDocs(activeLibId);
    await loadPaperManagerDocs();
    if (typeof updateLibraryProgress === "function") updateLibraryProgress();
  } catch (e) {
    console.warn("loadLibraries", e);
  }
}

export function renderLibTabs() {
  const el = document.getElementById("lib-tabs");
  if (!el) return;
  el.innerHTML = libraries
    .map(
      (lib) => `
    <button class="lib-tab${lib.lib_id === activeLibId ? " active" : ""}"
            ${act('switchLib', lib.lib_id)}>
      ${esc(lib.name)}${
        lib.lib_id !== "lt_docs"
          ? `<span class="lib-tab-del" ${act('deleteLib', lib.lib_id, '@event')}>✕</span>`
          : ""
      }
    </button>`,
    )
    .join("");
}

export async function switchLib(lib_id) {
  activeLibId = lib_id;
  _sync();
  renderLibTabs();
  await loadLibDocs(lib_id);
}

export async function loadLibDocs(lib_id) {
  const el = document.getElementById("lib-doc-list");
  if (!el) return;
  try {
    const d = await apiGet(`/api/libraries/${lib_id}/documents`);
    const docs =
      d.documents || (d.titles || []).map((t) => ({ title: t, file_exists: false }));
    el.innerHTML = "";
    if (!docs.length) {
      el.innerHTML = '<div class="empty-tip">此知识库暂无文档</div>';
      return;
    }
    docs.forEach((doc) => {
      const t = doc.title || "Unknown";
      const item = mk("div", "lib-doc-item");
      item.innerHTML = `
        <span style="font-size:13px">📄</span>
        <span class="lib-doc-title" title="${esc(t)}">${esc(t)}</span>
        ${doc.file_exists ? `<button class="sm-btn" ${act('openLibraryDoc', lib_id, t)}>阅读</button>` : ""}
        <button class="sm-btn" ${act('openCitationGraph', t, '')}>图谱</button>
        <button class="sm-btn pri" ${act('openReadingAgent', lib_id, t)}>提问</button>
        <button class="lib-doc-del" title="移除" ${act('removeLibDoc', lib_id, t, '@self')}>✕</button>`;
      el.appendChild(item);
    });
  } catch (e) {
    el.innerHTML = '<div class="empty-tip">加载失败</div>';
  }
}

export async function removeLibDoc(lib_id, title, btn) {
  const confirmFn = confirmDialog;
  const ok = typeof confirmFn === "function"
    ? await confirmFn(`将从当前知识库中移除文档「${title}」。`, { title: "确认移除文档？", okText: "移除" })
    : window.confirm("确认移除文档？");
  if (!ok) return;
  if (btn) btn.disabled = true;
  try {
    await apiDelete(`/api/libraries/${lib_id}/documents`, { title });
    await loadLibDocs(lib_id);
    await loadLibraries();
  } catch (e) {
    toast("移除失败");
    if (btn) btn.disabled = false;
  }
}

// ── Create-new-library dialog ───────────────────────────────────────────

export function showCreateLib() {
  document.getElementById("lib-create").style.display = "";
  document.getElementById("lib-name-inp").focus();
}

export function cancelCreateLib() {
  document.getElementById("lib-create").style.display = "none";
  document.getElementById("lib-name-inp").value = "";
}

export async function confirmCreateLib() {
  const name = document.getElementById("lib-name-inp").value.trim();
  if (!name) return;
  try {
    const d = await apiPost("/api/libraries", { name });
    cancelCreateLib();
    await loadLibraries();
    if (d?.lib_id) switchLib(d.lib_id);
  } catch (e) {
    toast("创建失败");
  }
}

export async function deleteLib(lib_id, ev) {
  ev?.stopPropagation?.();
  const lib = libraries.find((l) => l.lib_id === lib_id);
  const confirmFn = confirmDialog;
  const msg = `知识库「${lib?.name || lib_id}」及其所有文档都会被删除，且无法恢复。`;
  const ok = typeof confirmFn === "function"
    ? await confirmFn(msg, { title: "删除整个知识库？", okText: "删除" })
    : window.confirm(msg);
  if (!ok) return;
  try {
    await apiDelete(`/api/libraries/${lib_id}`);
    if (activeLibId === lib_id) {
      activeLibId = "lt_docs";
      _sync();
    }
    await loadLibraries();
  } catch (e) {
    toast("删除失败");
  }
}

// ── File upload + embed ─────────────────────────────────────────────────

export async function uploadToLibrary(input) {
  const file = input.files[0];
  const sid = window.currentSid;
  if (!file || !sid) return;
  input.value = "";
  const chunkSize = parseInt(document.getElementById("lib-chunk-size")?.value || "2000", 10);
  const chunkOverlap = parseInt(document.getElementById("lib-chunk-overlap")?.value || "200", 10);
  if (!Number.isFinite(chunkSize) || chunkSize < 200 || chunkSize > 4000) {
    toast("chunk size 需要在 200 到 4000 之间");
    return;
  }
  if (!Number.isFinite(chunkOverlap) || chunkOverlap < 0 || chunkOverlap >= chunkSize) {
    toast("overlap 需要大于等于 0 且小于 chunk size");
    return;
  }
  toast(`正在上传并嵌入 ${file.name}…`);
  const form = new FormData();
  form.append("file", file);
  try {
    const url =
      `/api/upload?session_id=${encodeURIComponent(sid)}` +
      `&lib_id=${encodeURIComponent(activeLibId)}` +
      `&chunk_size=${encodeURIComponent(chunkSize)}` +
      `&chunk_overlap=${encodeURIComponent(chunkOverlap)}`;
    // Multipart, can't go through apiPost — fall back to raw fetch with creds.
    const r = await fetch(url, { method: "POST", body: form, credentials: "include" });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      toast(`上传失败：${e.detail || r.statusText}`);
      return;
    }
    const d = await r.json();
    if (d.embed_error) {
      toast(`上传成功但嵌入失败：${d.embed_error}`);
    } else {
      toast(`已嵌入 ${d.chunks_indexed} 个片段`);
    }
    await loadLibraries();
    await loadLibDocs(activeLibId);
    if (typeof updateDownloaded === "function") updateDownloaded(d.stored_papers || []);
    renderPaperManager();
  } catch (err) {
    toast(`上传出错：${err.message || err}`);
  }
}

// ── Paper Manager (cross-library table view) ────────────────────────────

export async function loadPaperManagerDocs() {
  const batches = await Promise.all(
    libraries.map(async (lib) => {
      try {
        const d = await apiGet(`/api/libraries/${encodeURIComponent(lib.lib_id)}/documents`);
        return (d.documents || []).map((doc) => ({
          ...doc,
          lib_name: lib.name,
          lib_id: lib.lib_id,
        }));
      } catch (e) {
        return [];
      }
    }),
  );
  allLibraryDocs = batches.flat();
  _sync();
  renderPaperManagerTabs();
  renderPaperManager();
}

export function renderPaperManagerTabs() {
  const el = document.getElementById("paper-lib-tabs");
  if (!el) return;
  const tabs = [{ lib_id: "all", name: "全部" }, ...libraries];
  el.innerHTML = tabs
    .map(
      (lib) => `
    <button class="lib-tab${lib.lib_id === managerLibId ? " active" : ""}"
            ${act('switchPaperManagerLib', lib.lib_id)}>${esc(lib.name)}</button>`,
    )
    .join("");
}

export function switchPaperManagerLib(lib_id) {
  managerLibId = lib_id;
  _sync();
  renderPaperManagerTabs();
  renderPaperManager();
}

export function updatePaperManagerStats(visibleDocs) {
  const allDocs = allLibraryDocs || [];
  const visible = visibleDocs || [];
  const totalEl = document.getElementById("paper-stat-total");
  const visibleEl = document.getElementById("paper-stat-visible");
  const chunksEl = document.getElementById("paper-stat-chunks");
  const missingEl = document.getElementById("paper-stat-missing");
  const chunks = visible.reduce((sum, d) => sum + Number(d.chunk_count || 0), 0);
  const missing = visible.filter((d) => !d.file_exists && d.source_type !== "note").length;
  if (totalEl) totalEl.textContent = allDocs.length;
  if (visibleEl) visibleEl.textContent = visible.length;
  if (chunksEl) chunksEl.textContent = chunks;
  if (missingEl) missingEl.textContent = missing;
  const missingCard = missingEl?.closest(".paper-stat");
  if (missingCard) missingCard.classList.toggle("has-warning", missing > 0);
}

export function renderPaperManager() {
  const body = document.getElementById("paper-manager-body");
  if (!body) return;
  const q = (document.getElementById("paper-search")?.value || "").trim().toLowerCase();
  const sort = document.getElementById("paper-sort")?.value || "newest";
  let docs = allLibraryDocs;
  if (managerLibId !== "all") docs = docs.filter((d) => d.lib_id === managerLibId);
  if (q) {
    docs = docs.filter((d) =>
      `${d.title || ""} ${d.venue || ""} ${d.journal || ""} ${d.paper_source || ""} ${d.lib_name || ""} ${d.file_ext || ""}`
        .toLowerCase()
        .includes(q),
    );
  }
  docs = [...docs].sort((a, b) => {
    if (sort === "title") return (a.title || "").localeCompare(b.title || "");
    const at = Date.parse(a.indexed_at || "") || 0;
    const bt = Date.parse(b.indexed_at || "") || 0;
    return sort === "oldest" ? at - bt : bt - at;
  });
  updatePaperManagerStats(docs);
  if (!docs.length) {
    body.innerHTML = '<tr><td colspan="7"><div class="paper-empty">暂无匹配文献</div></td></tr>';
    return;
  }
  body.innerHTML = docs
    .map((d) => {
      const title = d.title || "Unknown";
      const isNote = d.source_type === "note" || String(d.source || "").startsWith("note://");
      const ext = isNote ? "笔记" : d.file_ext ? d.file_ext.replace(".", "").toUpperCase() : "未知";
      const canRead = !!d.file_exists || isNote;
      const indexedAt = fmtTime(d.indexed_at);
      const venue = paperVenueLabel(d);
      return `
      <tr>
        <td>
          <div class="paper-name" title="${esc(title)}">${esc(title)}</div>
          <div class="paper-meta">${esc((d.sections || []).slice(0, 3).join(" / ") || "已索引文献")}</div>
        </td>
        <td><span class="venue-badge" title="${esc(venue.full)}">${esc(venue.short)}</span></td>
        <td><span class="chip">${esc(d.lib_name || d.lib_id || "")}</span></td>
        <td>${formatPaperTime(indexedAt)}</td>
        <td><button class="sm-btn" ${act('openChunkViewer', d.lib_id, title)}>${d.chunk_count || 0}</button></td>
        <td>${isNote ? '<span class="chip">笔记</span>' : canRead ? esc(ext) : '<span class="paper-meta">文件缺失</span>'}</td>
        <td>
          <div class="paper-actions">
            <button class="sm-btn pri" ${canRead ? "" : "disabled"} ${act('openLibraryDoc', d.lib_id, title)}>阅读</button>
            <button class="sm-btn" ${act('openCitationGraph', title, '')}>图谱</button>
            <button class="sm-btn" ${act('openReadingAgent', d.lib_id, title)}>提问</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

// ── Small helpers used by renderPaperManager ────────────────────────────

export function paperVenueLabel(doc) {
  const raw = String(
    doc.venue || doc.journal || doc.publication_venue || doc.publication || doc.paper_source || "",
  ).trim();
  let full = raw;
  if (!full) {
    if (doc.source_type === "note" || String(doc.source || "").startsWith("note://")) full = "Note";
    else if (doc.source_type === "upload" || doc.paper_source === "upload") full = "Uploaded file";
    else if (doc.file_ext) full = doc.file_ext.replace(".", "").toUpperCase();
    else full = "Unknown source";
  }
  const short = full.length > 24 ? `${full.slice(0, 23)}…` : full;
  return { full, short };
}

export function formatPaperTime(value) {
  const text = String(value || "").trim();
  if (!text || text === "未知") {
    return '<span class="paper-time"><span class="paper-time-date">未知</span></span>';
  }
  const parts = text.split(/\s+/);
  const date = parts[0] || text;
  const time = (parts[1] || "").slice(0, 5);
  return `<span class="paper-time" title="${esc(text)}"><span class="paper-time-date">${esc(date)}</span>${
    time ? `<span class="paper-time-clock">${esc(time)}</span>` : ""
  }</span>`;
}
