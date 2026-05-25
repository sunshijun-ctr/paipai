/* PDF reader overlay: pdf.js-based renderer + text-layer selection +
 * persistent highlights / annotations / per-doc reading progress.
 *
 * The overlay has three layout layers stacked on top of the canvas:
 *   1. canvas              — pdf.js renders the page bitmap here
 *   2. reader-highlight-layer — yellow/green/etc rects for annotations
 *   3. reader-text-layer   — invisible text spans for selection
 *
 * When the user selects text, `handleReaderSelection` resolves the
 * client rects → page-relative percentages and shows a small popover.
 * Saving stores both the rects and the selected text on the backend,
 * so a future render can paint the highlight even if the PDF was
 * re-extracted with a different layout.
 *
 * Translation pop uses the same selection, plus the chat LLM endpoint.
 *
 * Dependencies on still-inline code (read via window):
 *   - openWebPreview (only via menu links)
 *   - confirmDialog (no current call here, but kept for safety)
 *   - pdfjsLib is a global from a <script src="…/pdf.min.js"> tag */

import { mk, esc, js, toast } from "./utils.js";
import { apiGet, apiPost, apiPatch, apiDelete } from "./api.js";
import { act } from "./events.js";

// ── State ──────────────────────────────────────────────────────────────

let currentReaderUrl = "";
let currentReaderDocId = "";
let currentReaderTitle = "";
let readerPdf = null;
let readerScale = 1.15;
let readerRendering = false;
let readerAnnotations = [];
let readerSelected = null;
let readerTranslation = null;
let readerProgressTimer = null;

function _sync() {
  window.currentReaderUrl = currentReaderUrl;
  window.currentReaderDocId = currentReaderDocId;
  window.currentReaderTitle = currentReaderTitle;
  window.readerPdf = readerPdf;
  window.readerScale = readerScale;
  window.readerAnnotations = readerAnnotations;
  window.readerSelected = readerSelected;
  window.readerTranslation = readerTranslation;
}
_sync();

// ── Open / close / fallback ────────────────────────────────────────────

export async function openReader({ url, title, docId }) {
  readerPdf?.destroy?.();
  readerPdf = null;
  currentReaderUrl = url;
  currentReaderDocId = docId || url;
  currentReaderTitle = title || "Paper";
  readerSelected = null;
  _sync();
  hideReaderPop();
  document.getElementById("reader-title").textContent = currentReaderTitle;
  document.getElementById("reader-overlay").classList.add("on");
  document.getElementById("reader-fallback").classList.remove("on");
  document.getElementById("reader-frame").classList.remove("on");
  document.getElementById("reader-frame").src = "about:blank";
  document.getElementById("reader-workbench").style.display = "grid";
  document.getElementById("reader-pages").innerHTML = "";
  document.getElementById("reader-status").textContent = "正在加载文献...";
  document.getElementById("reader-status").style.display = "block";
  document.getElementById("reader-page-indicator").textContent = "- / -";

  try {
    await ensurePdfJs();
    const [annData, progressData] = await Promise.all([
      loadReaderAnnotations(),
      loadReaderProgress(),
    ]);
    const progress = progressData || {};
    if (progress.scale) {
      readerScale = Math.min(2.4, Math.max(0.7, Number(progress.scale) || readerScale));
    }
    readerAnnotations = annData || [];
    _sync();
    readerPdf = await window.pdfjsLib.getDocument(url).promise;
    _sync();
    await renderReaderPdf();
    renderReaderAnnotations();
    const targetPage = Math.min(readerPdf.numPages, Math.max(1, Number(progress.page) || 1));
    requestAnimationFrame(() => scrollReaderToPage(targetPage, progress.scroll_top));
  } catch (e) {
    console.warn("PDF reader failed, falling back to inline frame", e);
    openReaderFallback(url, e.message || e);
  }
}

export async function ensurePdfJs() {
  if (!window.pdfjsLib) throw new Error("PDF.js 未加载");
  window.pdfjsLib.GlobalWorkerOptions.workerSrc =
    window.pdfjsLib.GlobalWorkerOptions.workerSrc ||
    "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
}

export function openReaderFallback(url, reason = "") {
  document.getElementById("reader-workbench").style.display = "none";
  const frame = document.getElementById("reader-frame");
  frame.src = url;
  frame.classList.add("on");
  document.getElementById("reader-fallback").textContent =
    `应用阅读器暂时无法渲染该文件，可使用"新窗口"打开。${reason ? `（${String(reason).slice(0, 80)}）` : ""}`;
  document.getElementById("reader-fallback").classList.add("on");
}

export function closeReader(event) {
  if (event && event.target !== document.getElementById("reader-overlay")) return;
  saveReaderProgress();
  document.getElementById("reader-overlay").classList.remove("on");
  document.getElementById("reader-frame").src = "about:blank";
  document.getElementById("reader-frame").classList.remove("on");
  document.getElementById("reader-pages").innerHTML = "";
  document.getElementById("reader-status").style.display = "none";
  document.getElementById("reader-fallback").classList.remove("on");
  hideReaderPop();
  hideReaderTranslation();
  readerPdf?.destroy?.();
  readerPdf = null;
  readerAnnotations = [];
  readerSelected = null;
  readerTranslation = null;
  currentReaderDocId = "";
  _sync();
}

export function openReaderInNewWindow() {
  if (currentReaderUrl) window.open(currentReaderUrl, "_blank", "noopener");
}

// ── Backend round-trips (annotations + progress) ───────────────────────

async function loadReaderAnnotations() {
  if (!currentReaderDocId) return [];
  try {
    const d = await apiGet(`/api/reading/annotations?doc_id=${encodeURIComponent(currentReaderDocId)}`);
    return d.annotations || [];
  } catch (e) {
    return [];
  }
}

async function loadReaderProgress() {
  if (!currentReaderDocId) return {};
  try {
    const d = await apiGet(`/api/reading/progress?doc_id=${encodeURIComponent(currentReaderDocId)}`);
    return d.progress || {};
  } catch (e) {
    return {};
  }
}

// ── PDF page rendering ─────────────────────────────────────────────────

export async function renderReaderPdf() {
  if (!readerPdf || readerRendering) return;
  readerRendering = true;
  const pages = document.getElementById("reader-pages");
  const status = document.getElementById("reader-status");
  pages.innerHTML = "";
  status.style.display = "block";
  try {
    for (let pageNo = 1; pageNo <= readerPdf.numPages; pageNo++) {
      status.textContent = `正在渲染 ${pageNo} / ${readerPdf.numPages} 页...`;
      const page = await readerPdf.getPage(pageNo);
      const viewport = page.getViewport({ scale: readerScale });
      const pageEl = mk("div", "reader-page");
      pageEl.dataset.page = String(pageNo);
      pageEl.style.width = `${viewport.width}px`;
      pageEl.style.height = `${viewport.height}px`;

      const canvas = document.createElement("canvas");
      const outputScale = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
      canvas.width = Math.ceil(viewport.width * outputScale);
      canvas.height = Math.ceil(viewport.height * outputScale);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      pageEl.appendChild(canvas);

      const highlightLayer = mk("div", "reader-highlight-layer");
      pageEl.appendChild(highlightLayer);
      const textLayer = mk("div", "reader-text-layer");
      pageEl.appendChild(textLayer);
      pages.appendChild(pageEl);

      await page.render({
        canvasContext: canvas.getContext("2d"),
        viewport,
        transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null,
      }).promise;
      const textContent = await page.getTextContent();
      await window.pdfjsLib.renderTextLayer({
        textContentSource: textContent,
        container: textLayer,
        viewport,
        textDivs: [],
      }).promise;
    }
  } finally {
    readerRendering = false;
  }
  status.style.display = "none";
  updateReaderPageIndicator();
}

// ── Highlight + annotation rendering ───────────────────────────────────

export function renderReaderHighlights() {
  document.querySelectorAll(".reader-highlight-layer").forEach((layer) => (layer.innerHTML = ""));
  for (const ann of readerAnnotations) {
    for (const rect of ann.rects || []) {
      const pageNo = rect.page || ann.page || 1;
      const pageEl = document.querySelector(`.reader-page[data-page="${pageNo}"]`);
      const layer = pageEl?.querySelector(".reader-highlight-layer");
      if (!layer) continue;
      const h = mk("div", `reader-highlight ${ann.color || ""}`);
      h.title = ann.note || ann.selected_text || "";
      h.style.left = `${rect.x * 100}%`;
      h.style.top = `${rect.y * 100}%`;
      h.style.width = `${rect.w * 100}%`;
      h.style.height = `${rect.h * 100}%`;
      layer.appendChild(h);
    }
  }
}

export function renderReaderAnnotations() {
  renderReaderHighlights();
  const list = document.getElementById("reader-ann-list");
  if (!list) return;
  if (!readerAnnotations.length) {
    list.innerHTML = '<div class="reader-ann-empty">暂无批注。选中文本后可以高亮或添加批注。</div>';
    return;
  }
  list.innerHTML = readerAnnotations
    .map(
      (ann) => `
    <div class="reader-ann" ${act('jumpToAnnotation', ann.id)}>
      <div class="reader-ann-top">
        <span class="reader-ann-color" style="background:${readerColorValue(ann.color)}"></span>
        <span>第 ${esc(ann.page || 1)} 页</span>
        <span>${ann.type === "note" ? "批注" : "高亮"}</span>
      </div>
      <div class="reader-ann-text">${esc(ann.selected_text || "")}</div>
      ${ann.note ? `<div class="reader-ann-note">${esc(ann.note)}</div>` : ""}
      <div class="reader-ann-actions">
        <button class="reader-tool" ${act('editReaderAnnotation', ann.id)}>编辑</button>
        <button class="reader-tool" ${act('deleteReaderAnnotation', ann.id)}>删除</button>
      </div>
    </div>`,
    )
    .join("");
}

export function readerColorValue(color) {
  return { green: "#2dd4bf", blue: "#60a5fa", pink: "#f472b6" }[color] || "#facc15";
}

// ── Text selection → rect mapping ──────────────────────────────────────

export function handleReaderSelection(event) {
  if (!currentReaderDocId || !document.getElementById("reader-overlay")?.classList.contains("on")) return;
  const sel = window.getSelection();
  const text = String(sel?.toString() || "").trim();
  if (!sel || !text || sel.rangeCount === 0) {
    hideReaderPop();
    return;
  }
  const range = sel.getRangeAt(0);
  const rects = [];
  let firstPage = null;
  for (const clientRect of Array.from(range.getClientRects())) {
    if (clientRect.width < 2 || clientRect.height < 2) continue;
    const pageEl = Array.from(document.querySelectorAll(".reader-page")).find((page) => {
      const p = page.getBoundingClientRect();
      return clientRect.bottom > p.top && clientRect.top < p.bottom && clientRect.right > p.left && clientRect.left < p.right;
    });
    if (!pageEl) continue;
    const p = pageEl.getBoundingClientRect();
    const page = Number(pageEl.dataset.page || 1);
    firstPage = firstPage || page;
    const left = Math.max(clientRect.left, p.left);
    const top = Math.max(clientRect.top, p.top);
    const right = Math.min(clientRect.right, p.right);
    const bottom = Math.min(clientRect.bottom, p.bottom);
    rects.push({
      page,
      x: (left - p.left) / p.width,
      y: (top - p.top) / p.height,
      w: Math.max(0, (right - left) / p.width),
      h: Math.max(0, (bottom - top) / p.height),
    });
  }
  if (!rects.length) {
    hideReaderPop();
    return;
  }
  readerSelected = { text, rects, page: firstPage || rects[0].page || 1 };
  _sync();
  const pop = document.getElementById("reader-pop");
  const x = event?.clientX || range.getBoundingClientRect().left;
  const y = event?.clientY || range.getBoundingClientRect().top;
  pop.style.left = `${Math.min(window.innerWidth - 150, Math.max(12, x))}px`;
  pop.style.top = `${Math.max(12, y - 44)}px`;
  pop.classList.add("on");
}

export function hideReaderPop() {
  document.getElementById("reader-pop")?.classList.remove("on");
}

// ── Save / edit / delete annotations ───────────────────────────────────

export async function saveReaderSelection(type = "highlight") {
  if (!readerSelected) return;
  const note = type === "note" ? prompt("写下这条批注：", "") : "";
  if (type === "note" && note === null) return;
  const body = {
    doc_id: currentReaderDocId,
    title: currentReaderTitle,
    source_url: currentReaderUrl,
    page: readerSelected.page,
    type,
    // Default colors: plain highlights stay yellow, annotated notes use
    // green so they're visually distinguishable on stacked / overlapping
    // selections. Translations (saveReaderTranslationAsNote) already use
    // blue. User-customisable colors would replace this default.
    color: type === "note" ? "green" : "yellow",
    selected_text: readerSelected.text,
    note: note || "",
    rects: readerSelected.rects,
  };
  let d;
  try {
    d = await apiPost("/api/reading/annotations", body);
  } catch (e) {
    toast("保存批注失败");
    return;
  }
  readerAnnotations.push(d.annotation);
  _sync();
  renderReaderAnnotations();
  hideReaderPop();
  window.getSelection()?.removeAllRanges();
  toast(type === "note" ? "批注已保存" : "高亮已保存");
}

export async function editReaderAnnotation(id) {
  const ann = readerAnnotations.find((a) => a.id === id);
  if (!ann) return;
  const note = prompt("编辑批注：", ann.note || "");
  if (note === null) return;
  let d;
  try {
    d = await apiPatch(`/api/reading/annotations/${encodeURIComponent(id)}`, {
      note,
      type: note ? "note" : ann.type,
    });
  } catch (e) {
    toast("更新批注失败");
    return;
  }
  readerAnnotations = readerAnnotations.map((a) => (a.id === id ? d.annotation : a));
  _sync();
  renderReaderAnnotations();
}

export async function deleteReaderAnnotation(id) {
  try {
    await apiDelete(`/api/reading/annotations/${encodeURIComponent(id)}`);
  } catch (e) {
    toast("删除失败");
    return;
  }
  readerAnnotations = readerAnnotations.filter((a) => a.id !== id);
  _sync();
  renderReaderAnnotations();
}

// ── Translation popover ────────────────────────────────────────────────

export async function translateReaderSelection() {
  if (!readerSelected?.text) return;
  const box = document.getElementById("reader-translate-box");
  const body = document.getElementById("reader-translate-body");
  const pop = document.getElementById("reader-pop");
  const popRect = pop.getBoundingClientRect();
  body.textContent = "正在翻译...";
  box.style.left = `${Math.min(window.innerWidth - 434, Math.max(14, popRect.left))}px`;
  box.style.top = `${Math.min(window.innerHeight - 376, Math.max(14, popRect.bottom + 8))}px`;
  box.classList.add("on");
  hideReaderPop();
  try {
    const d = await apiPost("/api/reading/translate", {
      text: readerSelected.text,
      title: currentReaderTitle,
      target_lang: "中文",
    });
    readerTranslation = {
      selected: { ...readerSelected },
      translation: d.translation || "",
      model: d.model || "",
    };
    _sync();
    body.textContent = readerTranslation.translation || "没有返回翻译结果";
  } catch (e) {
    readerTranslation = null;
    _sync();
    body.textContent = `翻译失败：${e.body?.detail || e.message || e}`;
  }
}

export function hideReaderTranslation() {
  document.getElementById("reader-translate-box")?.classList.remove("on");
}

export async function copyReaderTranslation() {
  const text =
    readerTranslation?.translation ||
    document.getElementById("reader-translate-body")?.textContent ||
    "";
  if (!text.trim()) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("翻译已复制");
  } catch (e) {
    toast("复制失败");
  }
}

export async function saveReaderTranslationAsNote() {
  if (!readerTranslation?.selected || !readerTranslation.translation) return;
  const oldSelected = readerSelected;
  readerSelected = readerTranslation.selected;
  const body = {
    doc_id: currentReaderDocId,
    title: currentReaderTitle,
    source_url: currentReaderUrl,
    page: readerSelected.page,
    type: "note",
    color: "blue",
    selected_text: readerSelected.text,
    note: readerTranslation.translation,
    rects: readerSelected.rects,
  };
  let d;
  try {
    d = await apiPost("/api/reading/annotations", body);
  } catch (e) {
    readerSelected = oldSelected;
    _sync();
    toast("保存翻译批注失败");
    return;
  }
  readerSelected = oldSelected;
  readerAnnotations.push(d.annotation);
  _sync();
  renderReaderAnnotations();
  hideReaderTranslation();
  toast("翻译已保存为批注");
}

// ── Navigation, zoom, progress ─────────────────────────────────────────

export function jumpToAnnotation(id) {
  const ann = readerAnnotations.find((a) => a.id === id);
  if (ann) scrollReaderToPage(ann.page || ann.rects?.[0]?.page || 1);
}

export function scrollReaderToPage(page, scrollTop) {
  const scroller = document.getElementById("reader-scroll");
  const pageEl = document.querySelector(`.reader-page[data-page="${page}"]`);
  if (!scroller || !pageEl) return;
  scroller.scrollTop =
    Number.isFinite(Number(scrollTop)) && Number(scrollTop) > 0
      ? Number(scrollTop)
      : pageEl.offsetTop - 14;
  updateReaderPageIndicator();
}

export async function readerZoom(delta) {
  if (!readerPdf || readerRendering) return;
  readerScale = Math.min(2.4, Math.max(0.7, readerScale + delta));
  _sync();
  const currentPage = currentReaderPage();
  await renderReaderPdf();
  renderReaderAnnotations();
  scrollReaderToPage(currentPage);
  saveReaderProgress();
}

export function currentReaderPage() {
  const scroller = document.getElementById("reader-scroll");
  const pages = Array.from(document.querySelectorAll(".reader-page"));
  if (!scroller || !pages.length) return 1;
  const anchor = scroller.scrollTop + 80;
  let current = 1;
  for (const page of pages) {
    if (page.offsetTop <= anchor) current = Number(page.dataset.page || 1);
  }
  return current;
}

export function updateReaderPageIndicator() {
  const total = readerPdf?.numPages || 0;
  const el = document.getElementById("reader-page-indicator");
  if (el) el.textContent = total ? `${currentReaderPage()} / ${total}` : "- / -";
}

export function scheduleReaderProgressSave() {
  updateReaderPageIndicator();
  clearTimeout(readerProgressTimer);
  readerProgressTimer = setTimeout(saveReaderProgress, 450);
}

export async function saveReaderProgress() {
  if (!currentReaderDocId || !readerPdf) return;
  try {
    await apiPost("/api/reading/progress", {
      doc_id: currentReaderDocId,
      page: currentReaderPage(),
      scale: readerScale,
      scroll_top: document.getElementById("reader-scroll")?.scrollTop || 0,
    });
  } catch (e) {
    // Best-effort — swallow.
  }
}
