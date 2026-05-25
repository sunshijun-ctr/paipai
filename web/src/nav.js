/* Left-nav + main view switching: marks the active nav item, hides all
 * main views, and shows the requested one (chat / library / figure /
 * writing / settings / notes / calendar). The active view id persists in
 * localStorage so a reload restores it.
 *
 * Each show*View kicks the owning module's loader. Those modules reference
 * the nav functions only via window (no import), so importing their loaders
 * here is acyclic. chatStarted is read via window (session-list.js state).
 */

import { loadLibraries } from "./library.js";
import { renderProfile, renderLlmConfig } from "./profile.js";
import { applyNotesSidebarState, loadNotes } from "./notes.js";
import { loadCalendar } from "./calendar.js";

const ACTIVE_VIEW_KEY = 'research-agent-active-view';

export function navClick(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  if (el?.id) localStorage.setItem(ACTIVE_VIEW_KEY, el.id);
}

export function restoreActiveView() {
  const id = localStorage.getItem(ACTIVE_VIEW_KEY) || 'nav-chat';
  const el = document.getElementById(id) || document.getElementById('nav-chat');
  if (!el) return;
  if (id === 'nav-library') return showLibraryManager(el);
  if (id === 'nav-notes') return showNotesView(el);
  if (id === 'nav-figure') return showFigureView(el);
  if (id === 'nav-writing') return showWritingView(el);
  if (id === 'nav-settings') return showSettingsView(el);
  showChatView(el);
}

export function hideMainViews() {
  document.getElementById('welcome').classList.add('gone');
  document.getElementById('chat-view').classList.remove('on');
  document.getElementById('library-view').classList.remove('on');
  document.getElementById('reading-agent-view')?.classList.remove('on');
  document.getElementById('figure-view').classList.remove('on');
  document.getElementById('writing-view')?.classList.remove('on');
  document.getElementById('settings-view').classList.remove('on');
  document.getElementById('notes-view').classList.remove('on');
  document.getElementById('calendar-view')?.classList.remove('on');
}

export function showChatView(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('input-wrap').style.display = '';
  if (window.chatStarted) {
    document.getElementById('chat-view').classList.add('on');
  } else {
    document.getElementById('welcome').classList.remove('gone');
  }
}

export function showLibraryManager(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('library-view').classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
  loadLibraries();
}

export function showFigureView(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('figure-view').classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
}

export function showWritingView(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('writing-view').classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
}

export function showSettingsView(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('settings-view').classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
  renderProfile();
  renderLlmConfig();
}

export function showNotesView(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('notes-view').classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
  applyNotesSidebarState();
  loadNotes();
}

export function showCalendarView(el) {
  navClick(el);
  hideMainViews();
  document.getElementById('calendar-view').classList.add('on');
  document.getElementById('input-wrap').style.display = 'none';
  loadCalendar();
}
