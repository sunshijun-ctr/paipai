/* 功德木鱼 (merit woodfish) widget: a draggable floating button that plays
 * a synthesized wooden-tap sound (Web Audio) and floats a "功德 +1" label on
 * each knock. Visibility + position persist in localStorage.
 *
 * Fully standalone — no imports. Only the functions are bridged (main.js)
 * for the tool-menu toggle button and the boot-time initWoodfish() call.
 */

let woodfishAudioCtx = null;

export function playWoodfishSound() {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) return;
  woodfishAudioCtx = woodfishAudioCtx || new AudioCtx();
  const ctx = woodfishAudioCtx;
  if (ctx.state === 'suspended') ctx.resume();
  const now = ctx.currentTime;

  const makeTap = (offset, level) => {
    const t = now + offset;
    const length = Math.floor(ctx.sampleRate * .055);
    const buffer = ctx.createBuffer(1, length, ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < length; i++) {
      const decay = Math.pow(1 - i / length, 3.2);
      data[i] = (Math.random() * 2 - 1) * decay;
    }

    const noise = ctx.createBufferSource();
    const band = ctx.createBiquadFilter();
    const clickGain = ctx.createGain();
    noise.buffer = buffer;
    band.type = 'bandpass';
    band.frequency.setValueAtTime(920, t);
    band.Q.setValueAtTime(5.8, t);
    clickGain.gain.setValueAtTime(.0001, t);
    clickGain.gain.exponentialRampToValueAtTime(level, t + .004);
    clickGain.gain.exponentialRampToValueAtTime(.0001, t + .052);
    noise.connect(band).connect(clickGain).connect(ctx.destination);
    noise.start(t);
    noise.stop(t + .06);

    const body = ctx.createOscillator();
    const bodyGain = ctx.createGain();
    const lowpass = ctx.createBiquadFilter();
    body.type = 'square';
    body.frequency.setValueAtTime(185, t);
    body.frequency.exponentialRampToValueAtTime(92, t + .045);
    lowpass.type = 'lowpass';
    lowpass.frequency.setValueAtTime(430, t);
    bodyGain.gain.setValueAtTime(.0001, t);
    bodyGain.gain.exponentialRampToValueAtTime(level * .42, t + .005);
    bodyGain.gain.exponentialRampToValueAtTime(.0001, t + .075);
    body.connect(lowpass).connect(bodyGain).connect(ctx.destination);
    body.start(t);
    body.stop(t + .08);
  };

  makeTap(0, .34);
  makeTap(.075, .22);
}

export function knockWoodfish() {
  const el = document.getElementById('woodfish-widget');
  if (!el) return;
  el.classList.remove('knock');
  void el.offsetWidth;
  el.classList.add('knock');
  showWoodfishMerit(el);
  playWoodfishSound();
}

export function showWoodfishMerit(el) {
  const merit = document.createElement('span');
  merit.className = 'woodfish-merit';
  merit.textContent = '功德 +1';
  merit.style.marginLeft = `${Math.random() * 22 - 11}px`;
  el.appendChild(merit);
  setTimeout(() => merit.remove(), 900);
}

export function clampWoodfishPosition(el) {
  const pad = 8;
  const rect = el.getBoundingClientRect();
  const maxLeft = Math.max(pad, window.innerWidth - rect.width - pad);
  const maxTop = Math.max(pad, window.innerHeight - rect.height - pad);
  const left = Math.min(Math.max(rect.left, pad), maxLeft);
  const top = Math.min(Math.max(rect.top, pad), maxTop);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  el.style.right = 'auto';
  el.style.bottom = 'auto';
  localStorage.setItem('woodfish-position', JSON.stringify({left, top}));
}

export function setWoodfishVisible(visible) {
  const el = document.getElementById('woodfish-widget');
  if (el) el.style.display = visible ? '' : 'none';
  const btn = document.getElementById('tool-woodfish-toggle');
  if (btn) {
    btn.setAttribute('aria-pressed', visible ? 'true' : 'false');
    btn.classList.toggle('active', visible);
  }
  const label = document.getElementById('tool-woodfish-label');
  if (label) label.textContent = visible ? '木鱼 已开' : '木鱼 已关';
  localStorage.setItem('woodfish-visible', visible ? '1' : '0');
}

export function toggleWoodfish() {
  const visible = localStorage.getItem('woodfish-visible') !== '1';
  setWoodfishVisible(visible);
}

export function initWoodfish() {
  const el = document.getElementById('woodfish-widget');
  if (!el) return;
  setWoodfishVisible(localStorage.getItem('woodfish-visible') === '1');
  try {
    const saved = JSON.parse(localStorage.getItem('woodfish-position') || 'null');
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
      el.style.left = `${saved.left}px`;
      el.style.top = `${saved.top}px`;
      el.style.right = 'auto';
      el.style.bottom = 'auto';
      requestAnimationFrame(() => clampWoodfishPosition(el));
    }
  } catch(e) {
    localStorage.removeItem('woodfish-position');
  }

  let startX = 0, startY = 0, originX = 0, originY = 0, moved = false;
  el.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const rect = el.getBoundingClientRect();
    startX = e.clientX;
    startY = e.clientY;
    originX = rect.left;
    originY = rect.top;
    moved = false;
    el.classList.add('dragging');
    el.setPointerCapture(e.pointerId);
  });
  el.addEventListener('pointermove', e => {
    if (!el.classList.contains('dragging')) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
    const pad = 8;
    const maxLeft = Math.max(pad, window.innerWidth - el.offsetWidth - pad);
    const maxTop = Math.max(pad, window.innerHeight - el.offsetHeight - pad);
    el.style.left = `${Math.min(Math.max(originX + dx, pad), maxLeft)}px`;
    el.style.top = `${Math.min(Math.max(originY + dy, pad), maxTop)}px`;
    el.style.right = 'auto';
    el.style.bottom = 'auto';
  });
  el.addEventListener('pointerup', e => {
    if (!el.classList.contains('dragging')) return;
    el.classList.remove('dragging');
    try { el.releasePointerCapture(e.pointerId); } catch(_e) {}
    if (moved) {
      clampWoodfishPosition(el);
      return;
    }
    knockWoodfish();
  });
  el.addEventListener('pointercancel', () => el.classList.remove('dragging'));
  el.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    knockWoodfish();
  });
  window.addEventListener('resize', () => clampWoodfishPosition(el));
}
