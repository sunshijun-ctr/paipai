/* Semantic Scholar citation network overlay.
 *
 * Self-contained ECharts force-directed graph that pops up over the
 * main UI. Wired from anywhere via `openCitationGraph(title, paperId?)`:
 *   - right-click a node       → context menu (expand / open / download PDF)
 *   - left-click a node        → drill into that paper's network
 *   - "back" button            → previous paper in history stack
 *
 * Reaches the network via three backend endpoints:
 *   GET /api/citation/search?title=…  → resolve title → paperId
 *   GET /api/citation/graph/{id}      → nodes + edges
 *   GET /api/citation/pdf/{id}        → open-access PDF url (optional)
 *
 * State + helpers stay inside this module; ECharts is loaded as a
 * global from a `<script>` tag in index.html so we read `window.echarts`. */

import { esc, toast } from "./utils.js";
import { num, fmtCitations, shortTitle, addRecentViewedPaper } from "./papers.js";

// ── Module state ───────────────────────────────────────────────────────

let citationChart = null;
let citationPaper = { title: "", paperId: "" };
let citationHistory = [];
let citationMenuNode = null;

// ── Status banner above the chart ──────────────────────────────────────

export function setCitationState(message, isError = false) {
  const el = document.getElementById("citation-state");
  if (!el) return;
  el.textContent = message || "";
  el.style.color = isError ? "#dc2626" : "#64748b";
  el.classList.toggle("on", !!message);
}

// ── Overlay open / close / navigate ────────────────────────────────────

export async function openCitationGraph(title, paperId = "") {
  citationPaper = { title: title || "Unknown", paperId: paperId || "" };
  addRecentViewedPaper(title, paperId);
  citationHistory = [];
  updateCitationBackButton();
  document.getElementById("citation-title").textContent = citationPaper.title;
  document.getElementById("citation-sub").textContent = "Semantic Scholar citation network";
  document.getElementById("citation-overlay").classList.add("on");
  setCitationState("正在加载引用图谱...");
  await loadCitationGraph();
}

export async function reloadCitationGraph() {
  if (!document.getElementById("citation-overlay")?.classList.contains("on")) return;
  closeCitationMenu();
  setCitationState("正在刷新引用图谱...");
  await loadCitationGraph();
}

export function closeCitationGraph(event) {
  if (event && event.target !== document.getElementById("citation-overlay")) return;
  closeCitationMenu();
  document.getElementById("citation-overlay")?.classList.remove("on");
  citationChart?.dispose();
  citationChart = null;
  citationHistory = [];
  updateCitationBackButton();
}

export async function navigateCitationGraph(paperId, title) {
  if (!paperId || paperId === citationPaper.paperId) return;
  closeCitationMenu();
  citationHistory.push({ ...citationPaper });
  citationPaper = { title: title || "Unknown", paperId };
  updateCitationBackButton();
  document.getElementById("citation-title").textContent = citationPaper.title;
  document.getElementById("citation-sub").textContent = "Semantic Scholar citation network";
  setCitationState("正在加载引用图谱...");
  await loadCitationGraph();
}

export async function goBackCitationGraph() {
  if (!citationHistory.length) return;
  closeCitationMenu();
  citationPaper = citationHistory.pop();
  updateCitationBackButton();
  document.getElementById("citation-title").textContent = citationPaper.title || "Unknown";
  document.getElementById("citation-sub").textContent = "Semantic Scholar citation network";
  setCitationState("正在返回上一篇...");
  await loadCitationGraph();
}

export function updateCitationBackButton() {
  const btn = document.getElementById("citation-back");
  if (btn) btn.style.display = citationHistory.length ? "" : "none";
}

// ── Right-click context menu on a node ─────────────────────────────────

export function openCitationNodeMenu(node, x, y) {
  citationMenuNode = node;
  const menu = document.getElementById("citation-menu");
  const title = document.getElementById("citation-menu-title");
  const expand = document.getElementById("citation-menu-expand");
  const error = document.getElementById("citation-menu-error");
  if (!menu || !node) return;
  if (title) title.textContent = shortTitle(node.fullTitle || node.name || "Unknown", 92);
  if (expand) expand.style.display = node.isRoot ? "none" : "";
  if (error) {
    error.textContent = "";
    error.classList.remove("on");
  }
  menu.style.left = `${Math.min(x, window.innerWidth - 300)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - 190)}px`;
  menu.classList.add("on");
}

export function closeCitationMenu() {
  document.getElementById("citation-menu")?.classList.remove("on");
  citationMenuNode = null;
}

function setCitationMenuError(message) {
  const el = document.getElementById("citation-menu-error");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("on", !!message);
}

export function expandCitationMenuNode() {
  const node = citationMenuNode;
  closeCitationMenu();
  if (node) navigateCitationGraph(node.id, node.fullTitle);
}

export function openCitationMenuNode() {
  const node = citationMenuNode;
  closeCitationMenu();
  if (node?.url) window.open(node.url, "_blank", "noopener");
  else if (node?.id)
    window.open(`https://www.semanticscholar.org/paper/${node.id}`, "_blank", "noopener");
}

export async function downloadCitationMenuNode() {
  const node = citationMenuNode;
  const btn = document.getElementById("citation-menu-download");
  if (!node?.id || !btn) return;
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "正在获取...";
  setCitationMenuError("");
  try {
    const res = await fetch(`/api/citation/pdf/${encodeURIComponent(node.id)}`, {
      credentials: "include",
    });
    const data = await _readJsonResponse(res);
    if (!res.ok) throw new Error(data.detail || "该论文暂无开放获取 PDF");
    await _downloadPdfFromUrl(data.url, data.title || node.fullTitle || "paper");
    closeCitationMenu();
    toast("已开始下载论文 PDF");
  } catch (e) {
    setCitationMenuError(e.message || "下载失败，请稍后重试");
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

// ── Internal helpers ───────────────────────────────────────────────────

/** Lenient JSON parse: backend may return plain text on error.
 *  Wraps non-JSON text into `{detail: …}` so callers can read uniformly. */
async function _readJsonResponse(response) {
  const text = await response.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (e) {
    return { detail: text.slice(0, 240) };
  }
}

async function _downloadPdfFromUrl(url, title) {
  const filename = `${_safeFileName(title || "paper").slice(0, 80)}.pdf`;
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error("download request failed");
    const blob = await response.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(blobUrl);
  } catch (e) {
    // Fall back to opening the URL in a new tab if blob fetch fails
    // (CORS, network, or non-200). User can right-click → save.
    window.open(url, "_blank", "noopener");
  }
}

function _safeFileName(name) {
  return (
    String(name || "paper")
      .replace(/[\\/:*?"<>|]/g, "_")
      .replace(/\s+/g, " ")
      .trim() || "paper"
  );
}

// ── Fetch + render ─────────────────────────────────────────────────────

export async function loadCitationGraph() {
  try {
    if (!window.echarts) throw new Error("ECharts 未加载，请检查网络连接");
    let paperId = citationPaper.paperId;
    if (!paperId) {
      const search = await fetch(
        `/api/citation/search?title=${encodeURIComponent(citationPaper.title)}`,
        { credentials: "include" },
      );
      const data = await _readJsonResponse(search);
      if (!search.ok) throw new Error(data.detail || "未找到论文");
      paperId = data.paperId;
      citationPaper.paperId = paperId;
      document.getElementById("citation-sub").textContent =
        `${data.year || "年份未知"} · ${fmtCitations(data.citationCount || 0)} citations`;
    }
    const depth = document.getElementById("citation-depth")?.value || "1";
    const limit = document.getElementById("citation-limit")?.value || "20";
    const graph = await fetch(
      `/api/citation/graph/${encodeURIComponent(paperId)}?depth=${depth}&limit=${limit}`,
      { credentials: "include" },
    );
    const graphData = await _readJsonResponse(graph);
    if (!graph.ok) throw new Error(graphData.detail || "获取引用关系失败");
    renderCitationGraph(graphData);
    setCitationState("");
  } catch (e) {
    citationChart?.dispose();
    citationChart = null;
    setCitationState(e.message || "引用图谱加载失败", true);
  }
}

export function renderCitationGraph({ nodes, edges, root }) {
  const chartEl = document.getElementById("citation-chart");
  if (!chartEl) return;
  citationChart?.dispose();
  citationChart = window.echarts.init(chartEl);

  const chartNodes = (nodes || []).map((n) => {
    const citations = num(n.citationCount);
    const isRoot = n.id === root || n.isRoot;
    return {
      id: n.id,
      name: shortTitle(n.title, isRoot ? 54 : 34),
      fullTitle: n.title || "Unknown",
      year: n.year,
      citationCount: citations,
      authors: (n.authors || []).join(", "),
      url: n.url || `https://www.semanticscholar.org/paper/${n.id}`,
      isRoot,
      symbolSize: isRoot ? 48 : Math.max(14, Math.min(38, 12 + Math.sqrt(citations) * 1.7)),
      itemStyle: { color: isRoot ? "#1D9E75" : citations >= 100 ? "#f59e0b" : "#0ea5e9" },
      label: { show: isRoot },
    };
  });
  const chartEdges = (edges || []).map((e) => ({
    source: e.source,
    target: e.target,
    lineStyle: {
      color: e.type === "citations" ? "#14b8a6" : "#0ea5e9",
      width: e.type === "citations" ? 1.6 : 1.2,
      opacity: 0.68,
    },
  }));

  citationChart.setOption({
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (p) => {
        if (p.dataType !== "node") return "";
        const actionHint = p.data.isRoot
          ? "左键打开 Semantic Scholar · 右键更多操作"
          : "左键展开图谱 · 右键下载/跳转";
        return `<div style="max-width:280px;line-height:1.5">
          <b>${esc(p.data.fullTitle)}</b><br>
          ${p.data.authors ? `${esc(p.data.authors)}<br>` : ""}
          年份: ${p.data.year || "未知"}<br>
          被引: ${p.data.citationCount || 0}<br>
          <span style="color:#1D9E75;font-size:12px;display:block;margin-top:5px">${actionHint}</span>
        </div>`;
      },
    },
    series: [
      {
        type: "graph",
        layout: "force",
        nodes: chartNodes,
        edges: chartEdges,
        roam: true,
        draggable: true,
        force: { repulsion: 300, gravity: 0.05, edgeLength: [100, 250], layoutAnimation: true, friction: 0.6 },
        label: { position: "bottom", fontSize: 10, color: "#334155" },
        emphasis: { focus: "adjacency", label: { show: true } },
        lineStyle: { curveness: 0.16 },
        edgeSymbol: ["none", "arrow"],
        edgeSymbolSize: 7,
      },
    ],
  });
  citationChart.off("click");
  citationChart.off("contextmenu");
  citationChart.on("contextmenu", (p) => {
    if (p.dataType !== "node") return;
    p.event?.event?.preventDefault?.();
    const evt = p.event?.event;
    openCitationNodeMenu(p.data, evt?.clientX || 0, evt?.clientY || 0);
  });
  citationChart.on("click", (p) => {
    closeCitationMenu();
    if (p.dataType !== "node") return;
    if (p.data?.isRoot && p.data?.url) {
      window.open(p.data.url, "_blank", "noopener");
      return;
    }
    navigateCitationGraph(p.data.id, p.data.fullTitle);
  });
  setTimeout(() => citationChart?.resize(), 40);
}
