/* Promise-based confirmation modal — a styled replacement for window.confirm.
 * `await confirmDialog(message, {title, okText, danger, ...})` resolves true
 * on OK, false on cancel/escape/backdrop. Falls back to native confirm() if
 * the modal markup isn't present.
 *
 * No imports. Used across modules (reading-agent / notes / writing) via
 * confirmDialog. The Enter/Escape keydown handler is registered here.
 */

let _confirmResolve = null;

export function confirmDialog(message, options = {}) {
  const overlay = document.getElementById('confirm-modal');
  if (!overlay) return Promise.resolve(window.confirm(message));
  const opts = Object.assign({
    title: '确认操作',
    okText: '确定',
    cancelText: '取消',
    danger: true,
    icon: '⚠️',
  }, options);
  document.getElementById('confirm-title').textContent = opts.title;
  document.getElementById('confirm-message').textContent = message || '';
  const okBtn = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');
  const iconEl = document.getElementById('confirm-icon');
  okBtn.textContent = opts.okText;
  cancelBtn.textContent = opts.cancelText;
  iconEl.textContent = opts.icon;
  iconEl.classList.toggle('danger', !!opts.danger);
  okBtn.classList.toggle('danger', !!opts.danger);
  okBtn.onclick = (ev) => {
    ev?.preventDefault?.();
    ev?.stopPropagation?.();
    confirmDialogResolve(true);
  };
  cancelBtn.onclick = (ev) => {
    ev?.preventDefault?.();
    ev?.stopPropagation?.();
    confirmDialogResolve(false);
  };
  if (_confirmResolve) { _confirmResolve(false); _confirmResolve = null; }
  overlay.classList.add('on');
  setTimeout(() => okBtn.focus(), 0);
  return new Promise(resolve => { _confirmResolve = resolve; });
}

export function confirmDialogResolve(value) {
  const overlay = document.getElementById('confirm-modal');
  if (overlay) overlay.classList.remove('on');
  if (_confirmResolve) {
    const r = _confirmResolve;
    _confirmResolve = null;
    r(value);
  }
}

export function confirmDialogBackdrop(ev) {
  if (ev.target && ev.target.id === 'confirm-modal') confirmDialogResolve(false);
}

document.addEventListener('keydown', e => {
  const overlay = document.getElementById('confirm-modal');
  if (!overlay || !overlay.classList.contains('on')) return;
  if (e.key === 'Escape') { e.preventDefault(); confirmDialogResolve(false); }
  else if (e.key === 'Enter') { e.preventDefault(); confirmDialogResolve(true); }
});
