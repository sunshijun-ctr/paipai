/* Avatar rendering for chat message rows.
 *
 * Two distinct avatars:
 *   - renderAssistantAvatar(el, intent)  paipai SVG "pp" logo, colored
 *                                        gradient per-intent (well, all
 *                                        intents share the same color
 *                                        today — keyed by intent so we
 *                                        can vary later)
 *   - renderAvatar(el, avatar, name)     user avatar: image URL if set,
 *                                        otherwise first-letter fallback
 *
 * Both write to `innerHTML` for the SVG case — that's safe here because
 * the only interpolated values are (1) a monotonic counter and (2) a
 * known intent enum key. User-controlled values go through `esc()`. */

import { INTENT_LABELS } from "./constants.js";
import { esc } from "./utils.js";

// Counter ensures each rendered SVG has a unique linear-gradient id
// (Chrome/Safari otherwise reuse the first gradient across copies and
// the rest render solid).
let assistantAvatarSeq = 0;

export function renderAssistantAvatar(el, intent) {
  const id = `assistant-pp-${assistantAvatarSeq++}`;
  el.innerHTML = `
    <svg class="pp-avatar-svg" viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="${id}-back" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#AFA9EC"/>
          <stop offset="100%" stop-color="#9B6FD4"/>
        </linearGradient>
        <linearGradient id="${id}-front" x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stop-color="#534AB7"/>
          <stop offset="65%" stop-color="#7B5FC0"/>
          <stop offset="100%" stop-color="#9B6FD4"/>
        </linearGradient>
      </defs>
      <text class="pp-avatar-back" x="3" y="40" fill="url(#${id}-back)">p</text>
      <text class="pp-avatar-front" x="18" y="34" fill="url(#${id}-front)">p</text>
    </svg>`;
  el.title = INTENT_LABELS[intent] || "Assistant";
  el.dataset.agentIntent = intent || "assistant";
}

export function renderAvatar(el, avatar, name) {
  if (!el) return;
  if (avatar) {
    el.innerHTML = `<img src="${esc(avatar)}" alt="">`;
  } else {
    el.textContent = (name || "R").trim().slice(0, 1).toUpperCase() || "R";
  }
}
