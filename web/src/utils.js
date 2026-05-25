/* Generic DOM / string utilities used everywhere in the chat UI.
 *
 * These are all pure helpers — no module-level state, no event
 * listeners, no DOM lookups at import time. Safe to extract first,
 * because every downstream module / inline call site finds them on
 * `window` (bridged from main.js) regardless of load order. */

/** Create an element with an optional className. Shorter than
 *  document.createElement + classList.add when used dozens of times. */
export function mk(tag, cls = "") {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

/** HTML-escape: use this whenever interpolating data into an innerHTML
 *  template literal. Prefer `textContent` when you can — it's automatic
 *  and immune to "I forgot to escape" bugs. */
export function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Like esc() but also escapes the chars that break a JS string literal
 *  when interpolated into `onclick="someFn('${js(x)}')"`. Still ugly —
 *  Phase 2.3 should replace inline onclick with addEventListener and
 *  this helper can be retired. */
export function js(s) {
  return esc(
    String(s)
      .replace(/\\/g, "\\\\")
      .replace(/'/g, "\\'")
      .replace(/\n/g, " "),
  );
}

/** Render a server timestamp as `YYYY-MM-DD HH:MM` in local time.
 *  Returns "未知" for falsy input and the original string for
 *  un-parseable values, so callers can pass anything safely. */
export function fmtTime(s) {
  if (!s) return "未知";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return s;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Throw up a 2.2s transient message at the bottom of the viewport.
 *  Styling lives in app.css (`.toast`). */
export function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

/** Resize a chat textarea to fit its content, capped at 120 px so the
 *  chat history stays visible. Called on `input` events. */
export function autoResize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
}
