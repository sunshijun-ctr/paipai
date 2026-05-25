/* Right-sidebar paper list + download / save-to-library flow.
 *
 * Two related panels live here because they share state heavily:
 *   - "Found"     : results from the latest paper_search call
 *   - "Downloaded": papers stored in the current session (PDFs on disk)
 *
 * Plus the small "recently viewed" widget used by the citation graph,
 * and the `updateLibraryProgress` mini gauge shown above the lists.
 *
 * Module owns (and bridges to window) six state values so inline code
 * that still reads `storedPapers` / `foundPapers` continues to work.
 *
 * Dependencies pulled in from window (extracted in later modules):
 *   currentSid       — from session-list.js
 *   activeLibId      — from library.js
 *   loadLibraries / renderPaperManager — from library.js
 *   openCitationGraph / openReader / draftQuestion — still inline today */

import { apiPost } from "./api.js";
import { mk, esc, js, toast } from "./utils.js";
import { act } from "./events.js";
import { loadLibraries } from "./library.js";
import { openReader } from "./reader.js";

// ── State ──────────────────────────────────────────────────────────────

let foundPapers = [];
let storedPapers = [];
let activeFoundTag = "全部";
let foundHotTags = ["全部"];
let recentViewedPapers = [];
let pendingLibraryTitles = new Set();

function _sync() {
  window.foundPapers = foundPapers;
  window.storedPapers = storedPapers;
  window.activeFoundTag = activeFoundTag;
  window.foundHotTags = foundHotTags;
  window.recentViewedPapers = recentViewedPapers;
  window.pendingLibraryTitles = pendingLibraryTitles;
}
_sync();

export const getFoundPapers = () => foundPapers;
export const getStoredPapers = () => storedPapers;

// ── Small helpers ──────────────────────────────────────────────────────

export function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export function fmtCitations(v) {
  const n = num(v);
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function shortTitle(title, limit) {
  const t = String(title || "Unknown");
  return t.length > limit ? `${t.slice(0, limit - 1)}…` : t;
}

export function paperKey(p) {
  return String(p?.paper_id || p?.paperId || p?.title || "").trim().toLowerCase();
}

export function titleKey(title) {
  return String(title || "").trim().toLowerCase();
}

export function isPaperDownloaded(p) {
  const key = paperKey(p);
  const title = titleKey(p?.title);
  return storedPapers.some(
    (s) => paperKey(s) === key || (title && titleKey(s.title) === title),
  );
}

export function isPaperSaved(p) {
  const title = titleKey(p?.title);
  return storedPapers.some(
    (s) => title && titleKey(s.title) === title && (s.lib_id || s.chunks_indexed),
  );
}

export function normalizePaperCategories(p) {
  const c = p?.categories;
  if (Array.isArray(c)) return c.map((s) => String(s).trim()).filter(Boolean);
  if (typeof c === "string") return c.split(/[;,]/).map((s) => s.trim()).filter(Boolean);
  return [];
}

export function computeFoundHotTags(papers) {
  const counts = new Map();
  (papers || []).forEach((p) => {
    normalizePaperCategories(p).forEach((c) => {
      counts.set(c, (counts.get(c) || 0) + 1);
    });
  });
  const top = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 6)
    .map(([tag]) => tag);
  return ["全部", ...top];
}

// ── Found list ─────────────────────────────────────────────────────────

export function updateFound(papers) {
  foundPapers = papers || [];
  activeFoundTag = "全部";
  foundHotTags = computeFoundHotTags(foundPapers);
  _sync();
  const list = document.getElementById("found-list");
  const sec = document.getElementById("sec-found");
  const stEl = document.getElementById("st-found");
  if (stEl) stEl.textContent = foundPapers.length;
  if (list) list.innerHTML = "";
  renderFoundTags();
  updateLibraryProgress();
  if (!foundPapers.length) {
    if (sec) sec.style.display = "none";
    return;
  }
  if (sec) sec.style.display = "";
  renderFound();
}

export function clearFoundResults() {
  updateFound([]);
  toast("已清除搜索结果");
}

export function renderFoundTags() {
  const el = document.getElementById("found-tags");
  if (!el) return;
  if (foundHotTags.length <= 1) {
    el.innerHTML = "";
    el.style.display = "none";
    return;
  }
  el.style.display = "";
  el.innerHTML = foundHotTags
    .map(
      (tag) => `
    <span class="tag${tag === activeFoundTag ? " active" : ""}" ${act('setFoundTag', tag)}>${esc(tag)}</span>
  `,
    )
    .join("");
}

export function setFoundTag(tag) {
  activeFoundTag = tag || "全部";
  _sync();
  renderFoundTags();
  renderFound();
}

export function renderFound() {
  const list = document.getElementById("found-list");
  const sort = document.getElementById("found-sort")?.value || "relevance";
  if (!list) return;
  let papers = foundPapers.map((p, idx) => ({ ...p, _idx: idx + 1 }));
  if (activeFoundTag && activeFoundTag !== "全部") {
    const q = activeFoundTag.toLowerCase();
    papers = papers.filter((p) =>
      `${p.title || ""} ${p.abstract || ""} ${normalizePaperCategories(p).join(" ")}`
        .toLowerCase()
        .includes(q),
    );
  }
  papers.sort((a, b) => {
    if (sort === "citations") return num(b.citations) - num(a.citations);
    if (sort === "year") return num(b.year) - num(a.year);
    if (sort === "relevance") return num(b.relevance_score) - num(a.relevance_score);
    return a._idx - b._idx;
  });
  list.innerHTML = "";
  if (!papers.length) {
    list.innerHTML = '<div class="paper-empty">没有匹配该标签的论文</div>';
    return;
  }
  papers.forEach((p) => {
    const card = mk("div", "result-card rp-card");
    const isDownloaded = isPaperDownloaded(p);
    const isSaved = isPaperSaved(p);
    const s2Id = p.source === "semantic" ? p.paper_id || "" : "";
    card.innerHTML = `
      <div class="card-top">
        <span class="card-num">${p._idx}</span>
        <span class="card-title" title="${esc(p.title || "Unknown")}">${esc(p.title || "Unknown")}</span>
      </div>
      <div class="card-bottom">
        <div class="card-badges">
          ${p.year ? `<span class="badge">${esc(p.year)}</span>` : ""}
          ${p.citations ? `<span class="badge badge-cite">${fmtCitations(p.citations)} 引用</span>` : ""}
          ${p.relevance_score != null ? `<span class="badge badge-sim">sim ${p.relevance_score.toFixed(2)}</span>` : ""}
        </div>
        <div class="card-actions">
          <button class="icon-btn${isDownloaded ? " downloaded" : ""}" aria-label="${isDownloaded ? "阅读 PDF" : "下载 PDF"}" title="${isDownloaded ? "阅读 PDF" : "下载 PDF"}" ${isDownloaded ? act('openFoundPaperReader', p._idx) : act('downloadFoundPaper', p._idx, '@self')}>
            <span class="fallback-icon">${isDownloaded ? "读" : "↓"}</span>
          </button>
          <button class="icon-btn" aria-label="查看引用图谱" title="查看引用图谱" ${act('openCitationGraph', p.title || "Unknown", s2Id)}>
            <span class="fallback-icon">⌘</span>
          </button>
          <button class="icon-btn${isSaved ? " saved" : ""}" aria-label="${isSaved ? "已入库" : "下载并加入知识库"}" title="${isSaved ? "已入库" : "下载并加入知识库"}" ${act('saveFoundPaperToLibrary', p._idx, '@self')} ${isSaved ? "disabled" : ""}>
            <span class="fallback-icon">${isSaved ? "✓" : "+"}</span>
          </button>
        </div>
      </div>`;
    list.appendChild(card);
  });
}

// ── Download + open ────────────────────────────────────────────────────

async function _downloadFoundPaperData(index) {
  const sid = window.currentSid;
  if (!sid) throw new Error("缺少会话");
  // apiPost throws ApiError on non-2xx; for backend's `{success: false}`
  // shape we still need to check manually.
  const d = await apiPost(`/api/sessions/${encodeURIComponent(sid)}/papers/download`, { index });
  if (!d || !d.success) throw new Error(d?.detail || d?.message || "下载失败");
  return d;
}

export async function downloadFoundPaper(index, btn = null) {
  const oldHtml = btn?.innerHTML || "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="fallback-icon">...</span>';
  }
  try {
    const data = await _downloadFoundPaperData(index);
    updateDownloaded(data.stored_papers || storedPapers);
    renderFound();
    toast(data.already_downloaded ? "论文已下载，可以阅读" : "下载完成，可以阅读");
    return data;
  } catch (e) {
    toast(e.message || "下载失败");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = oldHtml;
    }
    throw e;
  }
}

export function storedIndexForFoundPaper(index) {
  const paper = foundPapers[index - 1];
  if (!paper) return -1;
  const key = paperKey(paper);
  const title = titleKey(paper.title);
  return storedPapers.findIndex(
    (s) => paperKey(s) === key || (title && titleKey(s.title) === title),
  );
}

export function openFoundPaperReader(index) {
  const storedIndex = storedIndexForFoundPaper(index);
  if (storedIndex < 0) {
    toast("请先下载论文");
    return;
  }
  openStoredPaper(storedIndex);
}

export function openStoredPaper(index) {
  const sid = window.currentSid;
  if (!sid) return;
  const url = `/api/sessions/${encodeURIComponent(sid)}/papers/file?index=${encodeURIComponent(index)}`;
  const title = storedPapers[index]?.title || "Paper";
  if (typeof openReader === "function") {
    openReader({ url, title, docId: `session:${sid}:paper:${index}` });
  }
}

// ── Save downloaded paper to library ───────────────────────────────────

export async function addStoredPaperToLibrary(index, btn) {
  const sid = window.currentSid;
  if (!sid) return;
  const oldText = btn?.textContent || "存库";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "存入中";
  }
  try {
    const lib = window.activeLibId || "lt_docs";
    const d = await apiPost(`/api/sessions/${encodeURIComponent(sid)}/library/add`, {
      index,
      lib_id: lib,
    });
    if (!d || !d.success) throw new Error(d?.detail || d?.message || "存库失败");
    updateDownloaded(d.stored_papers || storedPapers);
    if (typeof loadLibraries === "function") loadLibraries();
    renderFound();
    updateLibraryProgress();
    toast(`已加入知识库：${d.added?.[0]?.title || "论文"}`);
  } catch (e) {
    toast(e.message || "存库失败");
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }
}

export async function saveFoundPaperToLibrary(index, btn) {
  const sid = window.currentSid;
  if (!sid) return;
  const oldHtml = btn?.innerHTML || "";
  if (btn) {
    btn.disabled = true;
    btn.classList.add("saved");
    btn.innerHTML = '<span class="fallback-icon">...</span>';
  }
  try {
    let storedIndex = storedIndexForFoundPaper(index);
    if (storedIndex < 0) {
      const data = await _downloadFoundPaperData(index);
      updateDownloaded(data.stored_papers || storedPapers);
      storedIndex = Number.isInteger(data.stored_index)
        ? data.stored_index
        : storedIndexForFoundPaper(index);
    }
    if (storedIndex < 0) throw new Error("下载完成，但没有找到本地论文记录");
    await addStoredPaperToLibrary(storedIndex, null);
    renderFound();
    updateLibraryProgress();
    toast("已下载并加入知识库");
  } catch (e) {
    toast(e.message || "入库失败");
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("saved");
      btn.innerHTML = oldHtml;
    }
  }
}

export async function flushPendingLibrarySaves() {
  const sid = window.currentSid;
  if (!pendingLibraryTitles.size || !sid) return;
  const pending = [...pendingLibraryTitles];
  for (const title of pending) {
    const idx = storedPapers.findIndex((p) => titleKey(p.title) === title);
    if (idx < 0) continue;
    try {
      pendingLibraryTitles.delete(title);
      await addStoredPaperToLibrary(idx, null);
    } catch (e) {
      pendingLibraryTitles.add(title);
      console.warn("pending library save failed", e);
    }
  }
  renderFound();
  updateLibraryProgress();
}

// ── Downloaded list ────────────────────────────────────────────────────

export function updateDownloaded(papers) {
  storedPapers = papers || [];
  _sync();
  const list = document.getElementById("dl-list");
  const sec = document.getElementById("sec-dl");
  const stEl = document.getElementById("st-dl");
  if (stEl) stEl.textContent = storedPapers.length;
  if (list) list.innerHTML = "";
  renderFound();
  updateLibraryProgress();
  flushPendingLibrarySaves();
  if (!storedPapers.length) {
    if (sec) sec.style.display = "none";
    return;
  }
  if (sec) sec.style.display = "";
  storedPapers.forEach((p, idx) => {
    const item = mk("div", "dl-item");
    const title = p.title || "Unknown";
    item.innerHTML = `
      <span style="font-size:14px">📄</span>
      <span class="dl-title" title="${esc(title)}">${esc(title)}</span>
      <div class="dl-acts">
        <button class="sm-btn pri" ${act('draftQuestion', title)}>提问</button>
        <button class="sm-btn" ${act('openCitationGraph', title, p.source === "semantic" ? p.paper_id || "" : "")}>图谱</button>
        <button class="sm-btn" ${act('addStoredPaperToLibrary', idx, '@self')}>存库</button>
      </div>`;
    list.appendChild(item);
  });
}

// ── Library compatibility shim + sidebar gauge + recents widget ────────

export function updateLibrary(_ignored) {
  // Kept for API compatibility — library state is fully managed by loadLibraries().
  if (typeof loadLibraries === "function") loadLibraries();
}

export function updateLibraryProgress() {
  const text = document.getElementById("library-progress-text");
  const fill = document.getElementById("library-progress-fill");
  const total = foundPapers.length;
  const saved = foundPapers.filter(isPaperSaved).length;
  const pct = total ? Math.round((saved / total) * 100) : 0;
  if (text) text.textContent = `${saved} / ${total}`;
  if (fill) fill.style.width = `${pct}%`;
}

export function addRecentViewedPaper(title, paperId = "") {
  const key = paperId || title;
  if (!key) return;
  recentViewedPapers = recentViewedPapers.filter((p) => (p.paperId || p.title) !== key);
  const match =
    foundPapers.find((p) => (p.paper_id || p.paperId || p.title) === key || p.title === title) || {};
  recentViewedPapers.unshift({
    title: title || match.title || "Unknown",
    paperId: paperId || match.paper_id || match.paperId || "",
    year: match.year || "",
    citationCount: match.citations || match.citationCount || 0,
  });
  recentViewedPapers = recentViewedPapers.slice(0, 5);
  _sync();
  renderRecentViewed();
}

export function renderRecentViewed() {
  const list = document.getElementById("recent-viewed-list");
  if (!list) return;
  if (!recentViewedPapers.length) {
    list.innerHTML = '<div class="empty-hint">暂无浏览记录</div>';
    return;
  }
  list.innerHTML = recentViewedPapers
    .map(
      (p) => `
    <div class="mini-paper" ${act('openCitationGraph', p.title, p.paperId || "")}>
      <div class="mini-title">${esc(shortTitle(p.title, 54))}</div>
      <div class="mini-meta">${esc(p.year || "年份未知")} · 被引 ${fmtCitations(p.citationCount || 0)}</div>
    </div>
  `,
    )
    .join("");
}
