/* Central event delegation — replaces inline on*= handlers and (eventually)
 * the window.* function bridge.
 *
 * Markup declares the action on the element:
 *   <button data-act="deleteNote" data-args='["id-123"]">x</button>
 *   <input  data-act-input="updateWordCounter">
 *   <select data-act-change="loadTokenMonitor">
 *   <input  data-act-keydown="...">    (see enter-submit note below)
 *
 * One delegated listener per event type sits on document and dispatches to a
 * registered action. Actions are plain functions registered via
 * registerActions(map) — the same map main.js used to put on window.
 *
 * Args: data-args holds a JSON array (HTML-escaped). Two sentinels are
 * substituted at dispatch time:
 *   "@self"  -> the element carrying the data-act attribute
 *   "@event" -> the DOM event
 * No data-args means call with no arguments.
 *
 * stopPropagation is implicit: closest() returns the innermost element with
 * the attribute, so a child's action shadows an ancestor's (which is exactly
 * what the old `event.stopPropagation(); childFn()` handlers achieved).
 */

import { esc } from "./utils.js";

const registries = { click: {}, change: {}, input: {}, keydown: {}, contextmenu: {} };

/** Build a delegated-handler attribute for template strings.
 *  act('selectNote', n.id) -> data-act="selectNote" data-args="[&quot;..&quot;]"
 *  Use sentinels '@self' / '@event' / '@#id' as args where needed. */
function _attr(attrName, name, args) {
  return args.length
    ? `${attrName}="${name}" data-args="${esc(JSON.stringify(args))}"`
    : `${attrName}="${name}"`;
}
export const act = (name, ...args) => _attr("data-act", name, args);
export const actChange = (name, ...args) => _attr("data-act-change", name, args);
export const actInput = (name, ...args) => _attr("data-act-input", name, args);
export const actKeydown = (name, ...args) => _attr("data-act-keydown", name, args);
const ATTR = {
  click: "data-act",
  change: "data-act-change",
  input: "data-act-input",
  keydown: "data-act-keydown",
  contextmenu: "data-act-contextmenu",
};

// Built-in helpers so the conditional inline handlers convert cleanly.
// data-args carries the target action name plus "@event".
registries.keydown.__enterSubmit = (name, event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); registries.click[name]?.(); }
};
registries.keydown.__enterRun = (name, event) => {
  if (event.key === "Enter") registries.click[name]?.();
};
registries.contextmenu.__preventDefault = (event) => { event?.preventDefault?.(); };
// No-op: an inner element carrying this shadows an ancestor's action
// (closest() stops here), replicating the old event.stopPropagation().
registries.click.__stop = () => {};
// Programmatically click another element by id (label → hidden file input).
registries.click.__clickEl = (id) => document.getElementById(id)?.click();

/** Register actions. Populates every event-type registry with the same map,
 *  so a name resolves whether it's used as click/change/input/keydown. */
export function registerActions(map) {
  for (const type of Object.keys(registries)) Object.assign(registries[type], map);
}

function parseArgs(el, event) {
  const raw = el.getAttribute("data-args");
  if (!raw) return [];
  let arr;
  try { arr = JSON.parse(raw); } catch { return []; }
  if (!Array.isArray(arr)) arr = [arr];
  return arr.map(a => {
    if (a === "@self") return el;
    if (a === "@event") return event;
    if (a === "@checked") return !!el.checked;
    if (a === "@value") return el.value;
    if (typeof a === "string" && a.startsWith("@#")) return document.getElementById(a.slice(2));
    return a;
  });
}

function makeDispatcher(type) {
  const attr = ATTR[type];
  return (event) => {
    const el = event.target?.closest?.(`[${attr}]`);
    if (!el) return;
    const fn = registries[type][el.getAttribute(attr)];
    if (typeof fn === "function") fn(...parseArgs(el, event));
  };
}

document.addEventListener("click", makeDispatcher("click"));
document.addEventListener("change", makeDispatcher("change"));
document.addEventListener("input", makeDispatcher("input"));
document.addEventListener("keydown", makeDispatcher("keydown"));
document.addEventListener("contextmenu", makeDispatcher("contextmenu"));
