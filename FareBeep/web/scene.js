const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const art = document.getElementById('hero-art');
const frame = document.getElementById('art-frame');

if (art && frame && !reduceMotion) {
  const layers = [
    { el: frame.querySelector('.spatial-map'), k: -9, z: -40 },
    { el: frame.querySelector('.chip-los'), k: 16, z: 50 },
    { el: frame.querySelector('.chip-abv'), k: 22, z: 80 },
    { el: frame.querySelector('.chip-phc'), k: 13, z: 40 },
    { el: frame.querySelector('.chat-orbital'), k: 19, z: 110 },
    { el: frame.querySelector('.beep-card'), k: -17, z: 90 }
  ].filter(l => l.el);

  let tx = 0, ty = 0, cx = 0, cy = 0;
  let dragging = false, panX = 0, panY = 0, lastX = 0, lastY = 0;

  art.addEventListener('pointermove', (e) => {
    const r = art.getBoundingClientRect();
    tx = ((e.clientX - r.left) / r.width) * 2 - 1;
    ty = -(((e.clientY - r.top) / r.height) * 2 - 1);
    if (dragging) { panX += e.clientX - lastX; panY += e.clientY - lastY; }
    lastX = e.clientX;
    lastY = e.clientY;
  });
  art.addEventListener('pointerdown', (e) => { dragging = true; lastX = e.clientX; lastY = e.clientY; });
  window.addEventListener('pointerup', () => { dragging = false; });
  art.addEventListener('pointerleave', () => { tx = 0; ty = 0; });

  (function tick() {
    const t = performance.now() / 1000;
    const idleX = Math.sin(t * 0.3) * 0.22;
    const idleY = Math.cos(t * 0.24) * 0.16;
    const targetX = tx * 0.95 + idleX;
    const targetY = ty * 0.95 + idleY;
    cx += (targetX - cx) * 0.055;
    cy += (targetY - cy) * 0.055;
    frame.style.transform = `translate3d(${panX.toFixed(1)}px, ${panY.toFixed(1)}px, 0) rotateX(${(cy * 2.2).toFixed(2)}deg) rotateY(${(cx * 2.5).toFixed(2)}deg)`;
    for (const l of layers) {
      l.el.style.transform = `translate3d(${(cx * l.k).toFixed(1)}px, ${(cy * l.k).toFixed(1)}px, ${l.z}px)`;
    }
    requestAnimationFrame(tick);
  })();
}

const toast = document.getElementById('toast');
let toastTimer = null;
function showToast(msg) {
  toast.textContent = msg;
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3200);
}
const waBtn = document.getElementById('whatsapp-soon');
if (waBtn) waBtn.addEventListener('click', () => showToast('WhatsApp is being prepared — add the business link when it is ready.'));
for (const el of document.querySelectorAll('[data-toast]')) {
  el.addEventListener('click', () => showToast(el.dataset.toast));
}
for (const link of document.querySelectorAll('.nav-link')) {
  link.addEventListener('click', () => {
    const target = document.querySelector(link.dataset.scroll);
    if (target) target.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth' });
  });
}

if (!reduceMotion) {
  for (const brand of document.querySelectorAll('.site-brand')) {
    const mark = brand.querySelector('.brand-mark');
    if (!mark) continue;
    let bx = 0, by = 0, cx = 0, cy = 0;
    brand.addEventListener('mousemove', (e) => {
      const r = brand.getBoundingClientRect();
      bx = ((e.clientX - r.left) / r.width) * 2 - 1;
      by = -(((e.clientY - r.top) / r.height) * 2 - 1);
    });
    brand.addEventListener('mouseleave', () => { bx = 0; by = 0; });
    (function tiltLogo() {
      cx += (bx * 9 - cx) * 0.14;
      cy += (by * 11 - cy) * 0.14;
      mark.style.transform = `perspective(400px) rotateX(${cy.toFixed(2)}deg) rotateY(${cx.toFixed(2)}deg)`;
      requestAnimationFrame(tiltLogo);
    })();
  }

  const lockMin = document.getElementById('lock-min');
  const ringFg = document.querySelector('.lock-ring .ring-fg');
  if (lockMin && ringFg) {
    const RING_C = 339.29;
    let remain = 600;
    setInterval(() => {
      remain = remain > 0 ? remain - 1 : 600;
      const m = String(Math.floor(remain / 60)).padStart(2, '0');
      const s = String(remain % 60).padStart(2, '0');
      lockMin.textContent = `${m}:${s}`;
      ringFg.style.strokeDashoffset = (RING_C * (1 - remain / 600)).toFixed(1);
    }, 1000);
  }
}