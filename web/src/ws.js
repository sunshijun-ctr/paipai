/* WebSocket connection management.
 *
 * The chat WS lives at `/ws`. We auto-reconnect after a 3s delay on
 * close, refresh the side panel on connect (so a refreshed session
 * picks up server-side state), and log read errors to Sentry.
 *
 * Why this is so thin: `onMsg(d)` dispatches WS events to a dozen UI
 * functions (showThinking / addMsg / updateFound / refreshPanel / ...)
 * which are still inline in index.html. Pulling `onMsg` out now would
 * mean cross-window bridging every one of those — not worth it until
 * chat.js gets its own module. So this module owns connection
 * lifecycle ONLY; routing stays inline.
 *
 * Dependencies (read from `window` at call time):
 *   setDot(state)           — connection indicator
 *   setSend(enabled)        — input box enable/disable
 *   setGenerating(b)        — "thinking" UI state
 *   refreshPanel()          — refetch session state on reconnect
 *   onMsg(d)                — message dispatcher (inline today)
 *   Sentry                  — optional, set when DSN configured */

import { onMsg } from "./chat.js";
import { refreshPanel } from "./session-list.js";
import { setDot, setGenerating, setSend } from "./thinking.js";

let ws = null;
let reconnTimer = null;

export function connectWS() {
  if (typeof setDot === "function") setDot("conn");
  // ws:// or wss:// based on current protocol — production runs over HTTPS
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${scheme}://${location.host}/ws`);

  ws.onopen = () => {
    if (typeof setDot === "function") setDot("");
    if (reconnTimer) { clearTimeout(reconnTimer); reconnTimer = null; }
    if (typeof setSend === "function") setSend(true);
    if (typeof refreshPanel === "function") refreshPanel();
  };

  ws.onclose = () => {
    if (typeof setDot === "function") setDot("off");
    if (typeof setGenerating === "function") setGenerating(false);
    if (typeof setSend === "function") setSend(false);
    reconnTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    if (typeof setDot === "function") setDot("off");
    if (window.Sentry) {
      window.Sentry.captureMessage("ws.onerror", {
        level: "warning",
        extra: { readyState: ws ? ws.readyState : -1 },
      });
    }
  };

  ws.onmessage = (e) => {
    if (typeof onMsg === "function") {
      try {
        onMsg(JSON.parse(e.data));
      } catch (exc) {
        console.warn("onMsg threw", exc);
        if (window.Sentry) window.Sentry.captureException(exc);
      }
    }
  };

  // Re-export the live socket so chat send() can write to it. Wire on
  // every new connection (the inline `let ws` was at module top of the
  // legacy script — same idea, just owned by this module now).
  window.ws = ws;
}
