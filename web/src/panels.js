/* Collapsible-chrome toggles: the right-side research panel and the left
 * nav sidebar. Collapsed state persists in localStorage. No imports.
 * Functions are bridged (main.js) for the toggle buttons + the boot-time
 * init calls.
 */

const LEFT_NAV_COLLAPSED_KEY = 'research-agent-left-nav-collapsed';
const RESEARCH_PANEL_COLLAPSED_KEY = 'research-agent-panel-collapsed';

export function setResearchPanelCollapsed(collapsed) {
  const panel = document.getElementById('panel');
  const btn = document.getElementById('panel-toggle');
  if (!panel || !btn) return;
  panel.classList.toggle('panel-collapsed', collapsed);
  btn.title = collapsed ? '展开研究看板' : '收起研究看板';
  const text = btn.querySelector('.panel-toggle-text');
  if (text) text.textContent = '';
  localStorage.setItem(RESEARCH_PANEL_COLLAPSED_KEY, collapsed ? '1' : '0');
}

export function toggleResearchPanel() {
  const panel = document.getElementById('panel');
  setResearchPanelCollapsed(!panel?.classList.contains('panel-collapsed'));
}

export function initResearchPanelCollapse() {
  setResearchPanelCollapsed(localStorage.getItem(RESEARCH_PANEL_COLLAPSED_KEY) === '1');
}

export function setLeftNavCollapsed(collapsed) {
  const nav = document.getElementById('nav');
  const btn = document.getElementById('nav-collapse-btn');
  if (!nav || !btn) return;
  nav.classList.toggle('nav-collapsed', collapsed);
  btn.title = collapsed ? '展开侧边栏' : '收起侧边栏';
  btn.setAttribute('aria-label', btn.title);
  localStorage.setItem(LEFT_NAV_COLLAPSED_KEY, collapsed ? '1' : '0');
}

export function toggleLeftNav(event) {
  event?.stopPropagation();
  const nav = document.getElementById('nav');
  setLeftNavCollapsed(!nav?.classList.contains('nav-collapsed'));
}

export function initLeftNavCollapse() {
  setLeftNavCollapsed(localStorage.getItem(LEFT_NAV_COLLAPSED_KEY) === '1');
}
