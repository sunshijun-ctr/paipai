/* Theme switcher: 4 "rice" color themes applied as a body class, persisted
 * in localStorage. No imports. Functions are bridged (main.js) for the
 * settings theme buttons + the boot-time initTheme() call.
 */

let currentTheme = 'rice-white';

export function applyTheme(theme) {
  const themes = ['rice-white', 'rice-pink', 'rice-purple', 'rice-blue'];
  const next = themes.includes(theme) ? theme : 'rice-white';
  currentTheme = next;
  document.body.classList.remove('theme-rice-white', 'theme-rice-pink', 'theme-rice-purple', 'theme-rice-blue');
  document.body.classList.add(`theme-${next}`);
  document.querySelectorAll('.theme-choice').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === next);
  });
}

export function setTheme(theme) {
  applyTheme(theme);
  localStorage.setItem('research-agent-theme', currentTheme);
}

export function initTheme() {
  applyTheme(localStorage.getItem('research-agent-theme') || 'rice-white');
}
