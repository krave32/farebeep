import * as THREE from 'three';

const art = document.getElementById('hero-art');
const frame = document.getElementById('art-frame');
const canvas = document.getElementById('scene-canvas');
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const probe = document.createElement('canvas');
const supported = !!(window.WebGLRenderingContext && (probe.getContext('webgl') || probe.getContext('webgl2') || probe.getContext('experimental-webgl')));
if (!supported) {
  art.classList.add('webgl-off');
} else {
  art.classList.add('webgl-on');
  init();
}

function glowTexture(inner, outer) {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d');
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, inner);
  grad.addColorStop(0.35, inner.replace('1)', '0.35)'));
  grad.addColorStop(1, outer);
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(c);
  return tex;
}

function init() {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 60);
  camera.position.set(0, 0, 10);
  const world = new THREE.Group();
  scene.add(world);

  const starMat = new THREE.PointsMaterial({ color: 0xcfe9f5, size: 0.09, transparent: true, opacity: 0.85, depthWrite: false });
  const starGeo = () => {
    const n = 700, pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 46;
      pos[i * 3 + 1] = (Math.random() - 0.5) * 28;
      pos[i * 3 + 2] = -2 - Math.random() * 20;
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    return g;
  };
  const starsA = new THREE.Points(starGeo(), starMat);
  const starsB = new THREE.Points(starGeo(), starMat);
  scene.add(starsA, starsB);

  function node(x, y, z, color, glowColor) {
    const g = new THREE.Group();
    g.position.set(x, y, z);
    const core = new THREE.Mesh(new THREE.SphereGeometry(0.17, 24, 24), new THREE.MeshBasicMaterial({ color }));
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTexture(glowColor, 'rgba(0,0,0,0)'), transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
    glow.scale.set(2.6, 2.6, 1);
    const ring = new THREE.Mesh(new THREE.TorusGeometry(0.36, 0.02, 8, 48), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.4 }));
    ring.rotation.x = Math.PI / 2.2;
    g.add(core, glow, ring);
    world.add(g);
    return { g, ring };
  }

  const los = node(-3.5, -0.7, -1, 0xff8a5c, 'rgba(240,90,40,1)');
  const abv = node(3.3, 0.7, -1.6, 0x3fd6c4, 'rgba(47,154,141,1)');

  const curve = new THREE.QuadraticBezierCurve3(
    new THREE.Vector3(-3.5, -0.7, -1),
    new THREE.Vector3(0, 2.6, -4.4),
    new THREE.Vector3(3.3, 0.7, -1.6)
  );
  const tube = new THREE.Mesh(
    new THREE.TubeGeometry(curve, 64, 0.022, 6, false),
    new THREE.MeshBasicMaterial({ color: 0xff7a45, transparent: true, opacity: 0.35, depthWrite: false })
  );
  const guide = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(curve.getPoints(90)),
    new THREE.LineDashedMaterial({ color: 0xffffff, transparent: true, opacity: 0.14, dashSize: 0.18, gapSize: 0.14 })
  );
  guide.computeLineDistances();
  world.add(tube, guide);

  const plane = new THREE.Group();
  const body = new THREE.Mesh(new THREE.ConeGeometry(0.09, 0.34, 8), new THREE.MeshBasicMaterial({ color: 0xfff3e6 }));
  body.rotation.x = Math.PI / 2;
  const wings = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.012, 0.13), new THREE.MeshBasicMaterial({ color: 0xf05a28 }));
  wings.position.set(0, -0.01, 0.02);
  plane.add(body, wings);
  world.add(plane);

  const forward = new THREE.Vector3(0, 0, 1);
  const tangent = new THREE.Vector3();
  let planeT = 0;
  function placePlane() {
    const p = curve.getPointAt(planeT, new THREE.Vector3());
    curve.getTangentAt(planeT, tangent);
    plane.position.copy(p);
    plane.quaternion.setFromUnitVectors(forward, tangent);
    plane.rotateZ(Math.sin(planeT * Math.PI * 2) * 0.3);
  }

  let tx = 0, ty = 0, yaw = 0, gx = 0, gy = 0, dragging = false, lastX = 0;

  frame.addEventListener('pointermove', (e) => {
    const r = art.getBoundingClientRect();
    tx = ((e.clientX - r.left) / r.width) * 2 - 1;
    ty = -(((e.clientY - r.top) / r.height) * 2 - 1);
    if (dragging) yaw += (e.clientX - lastX) * 0.004;
    lastX = e.clientX;
  });
  frame.addEventListener('pointerdown', (e) => { dragging = true; lastX = e.clientX; });
  window.addEventListener('pointerup', () => { dragging = false; });

  const chips = [
    { el: document.querySelector('.chip-los'), k: 14, z: 60 },
    { el: document.querySelector('.chip-abv'), k: -12, z: 90 },
    { el: document.querySelector('.chat-orbital'), k: 18, z: 110 },
    { el: document.querySelector('.beep-card'), k: -16, z: 60 }
  ];

  function applyChips(px, py) {
    for (const c of chips) {
      if (!c.el) continue;
      c.el.style.transform = `translate3d(${(px * c.k).toFixed(1)}px, ${(py * c.k).toFixed(1)}px, ${c.z}px)`;
    }
  }

  function resize() {
    const w = frame.clientWidth || 1, h = frame.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  new ResizeObserver(resize).observe(frame);

  if (reduceMotion) {
    placePlane();
    applyChips(0, 0);
    renderer.render(scene, camera);
    return;
  }

  const clock = new THREE.Clock();
  function tick() {
    const dt = Math.min(clock.getDelta(), 0.05);
    const t = clock.elapsedTime;
    planeT = (planeT + dt * 0.05) % 1;
    placePlane();
    los.ring.rotation.y += dt * 0.6;
    abv.ring.rotation.y -= dt * 0.6;
    starsA.rotation.y += dt * 0.008;
    starsB.rotation.y -= dt * 0.005;
    const targetX = tx * 0.11 + Math.sin(t * 0.25) * 0.06 + yaw;
    const targetY = ty * 0.09 + Math.cos(t * 0.2) * 0.04;
    gx += (targetX - gx) * 0.045;
    gy += (targetY - gy) * 0.045;
    world.rotation.y = gx;
    world.rotation.x = gy;
    applyChips(-tx * 0.8, -ty * 0.8);
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  }
  tick();
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