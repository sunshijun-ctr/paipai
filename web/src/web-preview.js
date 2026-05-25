/* In-app web page preview overlay (iframe). Opened from message links
 * (chat.js) and citation/menu links; falls back to a "open in browser"
 * hint if the page refuses to frame within ~1.2s.
 *
 * State (currentWebPreviewUrl) is module-private; only the functions are
 * bridged (main.js) for the overlay's onclick handlers and the cross-module
 * openWebPreview callers.
 */

let currentWebPreviewUrl = "";

export function openWebPreview(url, title = '网页预览') {
  if (!/^https?:\/\//i.test(url || '')) return;
  currentWebPreviewUrl = url;
  document.getElementById('web-preview-title').textContent = title || url;
  document.getElementById('web-preview-fallback').classList.remove('on');
  document.getElementById('web-preview-frame').src = url;
  document.getElementById('web-preview-overlay').classList.add('on');
  window.setTimeout(() => {
    if (document.getElementById('web-preview-overlay')?.classList.contains('on')) {
      document.getElementById('web-preview-fallback')?.classList.add('on');
    }
  }, 1200);
}

export function closeWebPreview(event) {
  if (event && event.target !== document.getElementById('web-preview-overlay')) return;
  document.getElementById('web-preview-overlay')?.classList.remove('on');
  document.getElementById('web-preview-frame').src = 'about:blank';
  document.getElementById('web-preview-fallback')?.classList.remove('on');
}

export function openWebPreviewExternal() {
  if (currentWebPreviewUrl) window.open(currentWebPreviewUrl, '_blank', 'noopener');
}
