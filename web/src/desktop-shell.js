/* Native desktop shell (pywebview): custom title-bar window controls
 * (minimize / maximize / close / preset sizes), draggable title bar, and a
 * bottom-right resize grip — all driven through window.pywebview.api.
 * Only active when the page is opened with ?desktop=1 inside pywebview.
 *
 * No imports — pure DOM + the pywebview bridge. Functions are bridged
 * (main.js) for the title-bar onclick handlers + the boot-time
 * enableNativeDesktopShell() call. The pywebviewready listener is
 * registered here at module load (it used to be a top-level inline line,
 * which would fire before the bundle bridged the function).
 */

export function isNativeDesktopShell() {
  return new URLSearchParams(location.search).get('desktop') === '1' && !!window.pywebview;
}

export function enableNativeDesktopShell() {
  if (new URLSearchParams(location.search).get('desktop') !== '1' || !window.pywebview) return;
  document.body.classList.add('desktop-window', 'native-shell', 'system-frame');
  initNativeWindowDrag();
  initNativeWindowResizeGrip();
}

export function desktopWindowMinimize() {
  if (event) event.stopPropagation();
  window.pywebview?.api?.minimize?.();
}

export function desktopWindowMaximize() {
  if (event) event.stopPropagation();
  window.pywebview?.api?.toggle_maximize?.();
}

export function desktopWindowClose() {
  if (event) event.stopPropagation();
  window.pywebview?.api?.close?.();
}

export function toggleDesktopSizeMenu(event) {
  event?.stopPropagation();
  document.getElementById('desktop-size-menu')?.classList.toggle('on');
}

export function hideDesktopSizeMenu() {
  document.getElementById('desktop-size-menu')?.classList.remove('on');
}

export function desktopWindowResize(width, height) {
  hideDesktopSizeMenu();
  window.pywebview?.api?.resize?.(width, height);
}

export function initNativeWindowDrag() {
  const bar = document.getElementById('desktop-title-drag');
  if (!bar || !window.pywebview?.api) return;
  if (bar.classList.contains('pywebview-drag-region')) return;
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let lastMove = 0;

  bar.addEventListener('pointerdown', async e => {
    if (e.button !== 0) return;
    dragging = true;
    startX = e.screenX;
    startY = e.screenY;
    lastMove = 0;
    bar.setPointerCapture?.(e.pointerId);
    await window.pywebview.api.begin_drag();
  });

  bar.addEventListener('pointermove', e => {
    if (!dragging) return;
    const now = performance.now();
    if (now - lastMove < 16) return;
    lastMove = now;
    window.pywebview.api.drag_to(Math.round(e.screenX - startX), Math.round(e.screenY - startY));
  });

  const stopDrag = e => {
    if (!dragging) return;
    dragging = false;
    try { bar.releasePointerCapture?.(e.pointerId); } catch(_e) {}
    window.pywebview.api.end_drag();
  };
  bar.addEventListener('pointerup', stopDrag);
  bar.addEventListener('pointercancel', stopDrag);
}

export function initNativeWindowResizeGrip() {
  const grip = document.getElementById('desktop-resize-grip');
  if (!grip || !window.pywebview?.api) return;
  let resizing = false;
  let startX = 0;
  let startY = 0;
  let startW = 0;
  let startH = 0;
  let lastMove = 0;

  grip.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    resizing = true;
    startX = e.screenX;
    startY = e.screenY;
    startW = window.outerWidth || window.innerWidth || 1440;
    startH = window.outerHeight || window.innerHeight || 920;
    lastMove = 0;
    grip.setPointerCapture?.(e.pointerId);
  });

  grip.addEventListener('pointermove', e => {
    if (!resizing) return;
    const now = performance.now();
    if (now - lastMove < 24) return;
    lastMove = now;
    const nextW = Math.max(980, Math.round(startW + e.screenX - startX));
    const nextH = Math.max(680, Math.round(startH + e.screenY - startY));
    window.pywebview.api.resize(nextW, nextH);
  });

  const stopResize = e => {
    if (!resizing) return;
    resizing = false;
    try { grip.releasePointerCapture?.(e.pointerId); } catch(_e) {}
  };
  grip.addEventListener('pointerup', stopResize);
  grip.addEventListener('pointercancel', stopResize);
}

// Was a top-level inline line; registered here so it survives even though
// enableNativeDesktopShell is no longer a hoisted inline function.
window.addEventListener('pywebviewready', enableNativeDesktopShell);
