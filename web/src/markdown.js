/* Markdown rendering pipeline: marked → KaTeX → DOMPurify → mermaid.
 *
 * Why all-in-one module: these functions form a hard cycle —
 * `renderMarkdownInto` calls `_mdToHtml` which calls `_purify`, and
 * `renderMarkdownInto` also calls `renderMermaidBlocks` which uses
 * the same DOMPurify wrapper internally. Splitting them would mean
 * either crossing import boundaries every call or extracting an
 * even smaller utility module — not worth it.
 *
 * External deps (all loaded as global `<script>` tags in index.html
 * BEFORE the Vite bundle): `marked`, `DOMPurify`, `katex`, `mermaid`.
 * We test each lookup with `typeof X !== 'undefined'` so the module
 * still works if a CDN script fails to load. */

// ── Pre-processing ──────────────────────────────────────────────────────

/** Strip uniform leading indentation when the model accidentally indents
 *  every line (e.g. response inside a quoted block on the backend).
 *  Skip when the indent is < 2 spaces or fewer than 55% of lines are
 *  indented — these signal intentional formatting (code in lists etc.). */
export function normalizeMarkdownIndent(text) {
  const raw = String(text || "").replace(/\r\n/g, "\n");
  const lines = raw.split("\n");
  const nonEmpty = lines.filter((line) => line.trim());
  if (!nonEmpty.length) return raw;

  const indents = nonEmpty
    .filter((line) => !/^ {0,3}```/.test(line))
    .map((line) => (line.match(/^ +/) || [""])[0].length)
    .filter((n) => n > 0);
  if (!indents.length) return raw;

  const common = Math.min(...indents);
  const indentedRatio = indents.length / nonEmpty.length;
  const hasMarkdownSignals = nonEmpty.some(
    (line) =>
      /^ +#{1,6}\s/.test(line) ||
      /^ +[-*+]\s/.test(line) ||
      /^ +>\s/.test(line) ||
      /^ +\|/.test(line) ||
      /^ +---+\s*$/.test(line) ||
      /^ +(title|tags|date):/i.test(line),
  );

  if (common < 2 || (indentedRatio < 0.55 && !hasMarkdownSignals)) return raw;
  return lines
    .map((line) => (line.startsWith(" ".repeat(common)) ? line.slice(common) : line))
    .join("\n");
}

/** Some LLMs emit `** word **` with stray spaces; marked won't bold it.
 *  Tighten the pattern back to `**word**`. */
export function fixMarkdownBoldSpacing(text) {
  return String(text || "").replace(/\*\*\s+(.+?)\s+\*\*/g, "**$1**");
}

// ── Sanitisation (Phase-1 #1.1) ─────────────────────────────────────────

/** DOMPurify wrapper. Strips <script>, on*= handlers, javascript: URLs,
 *  and other XSS vectors while preserving the markdown / KaTeX / Mermaid
 *  output the chat UI actually needs. ADD_ATTR keeps the few KaTeX
 *  data-* attrs that DOMPurify would otherwise drop. */
export function _purify(html) {
  if (typeof DOMPurify === "undefined") return html;
  return DOMPurify.sanitize(html, {
    ADD_TAGS: [
      "math", "semantics", "mrow", "mi", "mo", "mn",
      "msup", "msub", "mfrac", "msqrt", "mtext", "annotation",
    ],
    ADD_ATTR: ["target", "data-i", "data-call-id"],
  });
}

// ── Markdown → HTML ─────────────────────────────────────────────────────

/** Render a markdown string into sanitised HTML, with KaTeX math
 *  expressions resolved inline. Math placeholders are swapped to spans
 *  BEFORE marked.parse so they survive markdown processing intact, then
 *  swapped back to KaTeX HTML afterwards. */
export function _mdToHtml(text) {
  let s = fixMarkdownBoldSpacing(normalizeMarkdownIndent(text))
    .replace(/\\\[([\s\S]*?)\\\]/g, (_, m) => `$$${m}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_, m) => `$${m}$`);

  const _ms = [];
  // Block math first ($$...$$)
  s = s.replace(/\$\$([\s\S]*?)\$\$/g, (_, m) => {
    _ms.push({ d: true, t: m });
    return `<span class="__km" data-i="${_ms.length - 1}"></span>`;
  });
  // Inline math ($...$) — no newlines, no nested $
  s = s.replace(/\$([^$\n]+?)\$/g, (_, m) => {
    _ms.push({ d: false, t: m });
    return `<span class="__km" data-i="${_ms.length - 1}"></span>`;
  });

  const wrap = document.createElement("div");
  wrap.innerHTML =
    typeof marked !== "undefined" ? marked.parse(s) : s.replace(/\n/g, "<br>");

  if (_ms.length) {
    if (typeof katex !== "undefined") {
      wrap.querySelectorAll(".__km").forEach((el) => {
        const { d, t } = _ms[+el.dataset.i];
        try {
          const tmp = document.createElement("span");
          tmp.innerHTML = katex.renderToString(t, { displayMode: d, throwOnError: false });
          el.replaceWith(...tmp.childNodes);
        } catch (e) {
          el.outerHTML = d ? `$$${t}$$` : `$${t}$`;
        }
      });
    } else {
      // KaTeX not ready — restore raw delimiters so auto-render can pick them up
      wrap.querySelectorAll(".__km").forEach((el) => {
        const { d, t } = _ms[+el.dataset.i];
        el.outerHTML = d ? `$$${t}$$` : `$${t}$`;
      });
    }
  }

  return _purify(wrap.innerHTML);
}

// ── Mermaid diagrams ────────────────────────────────────────────────────

let mermaidRenderSeq = 0;

/** Replace `<code class="language-mermaid">` blocks with rendered SVG.
 *  Mermaid's output is run through DOMPurify in svg-friendly mode —
 *  mermaid has had escape bugs historically. */
export async function renderMermaidBlocks(root) {
  if (!root || typeof mermaid === "undefined") return;
  const blocks = Array.from(root.querySelectorAll("code.language-mermaid"));
  for (const code of blocks) {
    const pre = code.parentElement;
    if (!pre || pre.dataset.mermaidRendered === "1") continue;
    pre.dataset.mermaidRendered = "1";
    const source = code.textContent || "";
    if (!source.trim()) continue;
    try {
      const id = `mermaid-${Date.now()}-${mermaidRenderSeq++}`;
      const { svg } = await mermaid.render(id, source);
      const diagram = document.createElement("div");
      diagram.className = "mermaid-diagram";
      diagram.innerHTML =
        typeof DOMPurify !== "undefined"
          ? DOMPurify.sanitize(svg, { USE_PROFILES: { svg: true, svgFilters: true } })
          : svg;
      pre.replaceWith(diagram);
    } catch (e) {
      pre.dataset.mermaidRendered = "0";
      pre.classList.add("mermaid-error");
      console.warn("Mermaid render failed", e);
    }
  }
}

/** One-call rendering: feed sanitised HTML into `target`, then run
 *  any mermaid blocks. Pass `{mermaid: false}` to skip the post-pass
 *  for chat-streaming partial output where the diagram source isn't
 *  complete yet. */
export function renderMarkdownInto(target, text, options = {}) {
  target.innerHTML = _mdToHtml(text);
  if (options.mermaid !== false) renderMermaidBlocks(target);
}
