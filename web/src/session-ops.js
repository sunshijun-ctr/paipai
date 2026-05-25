/* Two composer-level session controls:
 *   - stopGeneration()        : abort the in-flight turn (/api/stop)
 *   - compressCurrentSession(): compress the conversation history
 *
 * Both act on the active session (window.currentSid, owned by
 * session-list.js). Bridged (main.js) for the stop / compress buttons.
 */

import { apiPost } from "./api.js";
import { toast } from "./utils.js";
import { isGenerating, removeThinking, setGenerating } from "./thinking.js";
import { updateCompression } from "./chat.js";

export async function stopGeneration() {
  if (!window.currentSid || !isGenerating()) return;
  try {
    await apiPost('/api/stop', {session_id: window.currentSid});
  } catch (e) {
    toast('停止请求失败');
  }
  removeThinking();
  setGenerating(false);
}

export async function compressCurrentSession() {
  if (!window.currentSid) return;
  const bar = document.getElementById('compress-bar');
  let d;
  try {
    d = await apiPost(`/api/sessions/${encodeURIComponent(window.currentSid)}/compress`);
  } catch (err) {
    if (err.status) toast(`压缩失败：${err.body?.detail || err.message}`);
    else toast(`压缩出错：${err.message || err}`);
    return;
  }
  updateCompression(d.compression || null);
  if (bar) bar.classList.remove('on');
  toast('对话已压缩');
}
