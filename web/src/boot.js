/* Application boot: the single DOMContentLoaded entry point that wires the
 * composer / reader / global DOM listeners and kicks every module's init.
 *
 * This is the last piece of the old inline <script> in index.html. It imports
 * each init/handler directly (no window bridge needed) and runs on
 * DOMContentLoaded — which, for a deferred module bundle, fires after the
 * bundle has evaluated, so everything below is ready.
 *
 * main.js imports this module for its side effect (the listener registration);
 * it exports nothing.
 */

import { QUOTES } from "./constants.js";
import { autoResize } from "./utils.js";
import { initTheme } from "./theme.js";
import { enableNativeDesktopShell, hideDesktopSizeMenu } from "./desktop-shell.js";
import { send, handleMessageLinkClick } from "./chat.js";
import { handleImagePaste } from "./upload.js";
import {
  handleReaderSelection, scheduleReaderProgressSave,
  hideReaderPop, hideReaderTranslation,
} from "./reader.js";
import { closeWebPreview } from "./web-preview.js";
import { connectWS } from "./ws.js";
import { loadProfile, loadLlmConfig } from "./profile.js";
import { initLeftNavCollapse, initResearchPanelCollapse } from "./panels.js";
import { initReadingAgentSplit } from "./reading-agent.js";
import { applyNotesSidebarState } from "./notes.js";
import { initSessions } from "./session-list.js";
import { restoreActiveView } from "./nav.js";
import { loadLibraries } from "./library.js";
import { figRenderHistory } from "./figure.js";
import { initWoodfish } from "./woodfish.js";
import { refreshStorageUsage } from "./file-manager.js";

// Configure the CDN markdown libs (loaded synchronously in <head>) before
// anything renders. Guarded so a missing CDN doesn't abort module eval.
window.marked?.setOptions?.({ breaks: true, gfm: true });
if (typeof window.mermaid !== 'undefined') {
  window.mermaid.initialize({ startOnLoad: false, securityLevel: 'loose', theme: 'default' });
}

document.addEventListener('DOMContentLoaded', () => {
  // Quote — bundle has run by now, so QUOTES is set
  const quote = QUOTES[new Date().getDate() % QUOTES.length];
  if (quote) {
    document.getElementById('q-text').textContent = quote.t;
    document.getElementById('q-by').textContent = quote.by;
  }
  initTheme();
  if (new URLSearchParams(location.search).get('desktop') === '1') {
    document.body.classList.add('desktop-window');
  }
  enableNativeDesktopShell();
  const inp = document.getElementById('user-input');
  inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  inp.addEventListener('input', () => autoResize(inp));
  inp.addEventListener('paste', handleImagePaste);
  document.getElementById('send-btn').addEventListener('click', send);
  document.getElementById('messages')?.addEventListener('click', handleMessageLinkClick);
  document.getElementById('reader-scroll')?.addEventListener('mouseup', handleReaderSelection);
  document.getElementById('reader-scroll')?.addEventListener('scroll', scheduleReaderProgressSave);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeWebPreview();
  });
  document.addEventListener('mousedown', e => {
    if (!e.target.closest?.('#reader-pop')) hideReaderPop();
    if (!e.target.closest?.('#reader-translate-box')) hideReaderTranslation();
    if (!e.target.closest?.('#desktop-size-menu') && !e.target.closest?.('.desktop-window-btn')) hideDesktopSizeMenu();
  });
  connectWS();
  loadProfile();
  loadLlmConfig();
  initLeftNavCollapse();
  initResearchPanelCollapse();
  initReadingAgentSplit();
  applyNotesSidebarState();
  initSessions().then(restoreActiveView);
  loadLibraries();
  figRenderHistory();
  initWoodfish();
  refreshStorageUsage();
});
