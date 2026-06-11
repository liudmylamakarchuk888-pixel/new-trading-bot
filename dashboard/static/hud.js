/* Polymarket Bot — Sci-Fi HUD dashboard */

const POLL_MS = 3000;
const scenes = {};

// ── Three.js wireframe viewports ─────────────────────────────────────────────

function initWireframe(canvasId, shape) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof THREE === "undefined") return;

  const rect = () => canvas.parentElement.getBoundingClientRect();
  const w = rect().width || 200;
  const h = rect().height || 120;

  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setSize(w, h, false);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 100);
  camera.position.z = 3.2;

  let geometry;
  if (shape === "ring") {
    geometry = new THREE.TorusGeometry(0.9, 0.28, 12, 24);
  } else if (shape === "crystal") {
    geometry = new THREE.OctahedronGeometry(1.1, 1);
  } else {
    geometry = new THREE.IcosahedronGeometry(1.0, 1);
  }

  const material = new THREE.MeshBasicMaterial({
    color: 0x2ee8f0,
    wireframe: true,
    transparent: true,
    opacity: 0.75,
  });
  const mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  const pts = new THREE.BufferGeometry();
  const n = 80;
  const positions = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const t = (i / n) * Math.PI * 4;
    positions[i * 3] = Math.sin(t) * 1.4;
    positions[i * 3 + 1] = Math.cos(t * 0.7) * 1.2;
    positions[i * 3 + 2] = Math.sin(t * 1.3) * 0.8;
  }
  pts.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  const ptsMat = new THREE.PointsMaterial({ color: 0x2ee8f0, size: 0.04, transparent: true, opacity: 0.6 });
  scene.add(new THREE.Points(pts, ptsMat));

  scenes[canvasId] = { renderer, scene, camera, mesh, canvas, resize: () => {
    const r = rect();
    renderer.setSize(r.width, r.height, false);
    camera.aspect = r.width / r.height;
    camera.updateProjectionMatrix();
  }};
}

function animateWireframes() {
  const t = Date.now() * 0.001;
  for (const id of Object.keys(scenes)) {
    const s = scenes[id];
    s.mesh.rotation.x = t * 0.35;
    s.mesh.rotation.y = t * 0.55;
    s.renderer.render(s.scene, s.camera);
  }
  requestAnimationFrame(animateWireframes);
}

// ── Canvas helpers ───────────────────────────────────────────────────────────

function drawSparkline(canvas, data, color = "#2ee8f0") {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  if (!data || data.length < 2) {
    ctx.strokeStyle = "rgba(46,232,240,0.2)";
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();
    return;
  }

  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.shadowColor = color;
  ctx.shadowBlur = 4;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 4) - 2;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function drawWaveform(canvas, data) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const pts = data && data.length > 4 ? data : Array.from({ length: 60 }, (_, i) => Math.sin(i * 0.3) * 0.5);
  ctx.strokeStyle = "rgba(46,232,240,0.7)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  pts.forEach((v, i) => {
    const x = (i / (pts.length - 1)) * w;
    const norm = typeof v === "number" ? v : v.c || 0;
    const min = Math.min(...pts.map(p => typeof p === "number" ? p : p.c));
    const max = Math.max(...pts.map(p => typeof p === "number" ? p : p.c));
    const range = max - min || 1;
    const y = h - ((norm - min) / range) * (h - 6) - 3;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  for (let i = 0; i < w; i += 12) {
    ctx.strokeStyle = "rgba(46,232,240,0.06)";
    ctx.beginPath();
    ctx.moveTo(i, 0);
    ctx.lineTo(i, h);
    ctx.stroke();
  }
}

function drawTimeline(canvas, days) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const n = Math.max(days.length, 24);
  const cell = w / n;
  for (let i = 0; i < n; i++) {
    const d = days[i];
    const pnl = d ? d.pnl : 0;
    const on = d && Math.abs(pnl) > 0.01;
    const pos = pnl >= 0;
    ctx.fillStyle = on
      ? (pos ? "rgba(255,140,66,0.85)" : "rgba(255,100,100,0.5)")
      : "rgba(80,100,110,0.25)";
    ctx.fillRect(i * cell + 1, h * 0.2, cell - 2, h * 0.6);
  }
}

function drawNetwork(canvas, markets) {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const left = ["BTC", "ETH", "PAPER", "ARB", "RISK"];
  const assets = [...new Set((markets || []).map(m => m.asset).filter(Boolean))];
  const right = assets.length ? assets : ["BTC", "ETH"];
  const lx = w * 0.12;
  const rx = w * 0.88;

  ctx.font = "8px Share Tech Mono, monospace";
  ctx.fillStyle = "rgba(184,232,238,0.6)";

  left.forEach((label, i) => {
    const y = (h / (left.length + 1)) * (i + 1);
    ctx.fillText(label, lx - 30, y + 3);
    ctx.beginPath();
    ctx.arc(lx, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = "#2ee8f0";
    ctx.fill();
  });

  right.forEach((label, i) => {
    const y = (h / (right.length + 1)) * (i + 1);
    ctx.fillStyle = "rgba(184,232,238,0.6)";
    ctx.fillText(label, rx + 8, y + 3);
    ctx.beginPath();
    ctx.arc(rx, y, 3, 0, Math.PI * 2);
    ctx.fillStyle = "#ff8c42";
    ctx.fill();
  });

  ctx.strokeStyle = "rgba(46,232,240,0.15)";
  ctx.lineWidth = 0.5;
  (markets || []).slice(0, 12).forEach((m, idx) => {
    const li = left.indexOf(m.asset) >= 0 ? left.indexOf(m.asset) : idx % left.length;
    const ri = right.indexOf(m.asset) >= 0 ? right.indexOf(m.asset) : idx % right.length;
    const y1 = (h / (left.length + 1)) * (li + 1);
    const y2 = (h / (right.length + 1)) * (ri + 1);
    const cp1x = w * 0.35;
    const cp2x = w * 0.65;
    const curve = (idx % 3) * 0.15;
    ctx.beginPath();
    ctx.moveTo(lx, y1);
    ctx.bezierCurveTo(cp1x, y1 + h * curve, cp2x, y2 - h * curve, rx, y2);
    ctx.stroke();
  });
}

// ── DOM renderers ────────────────────────────────────────────────────────────

function fmtUsd(v) {
  if (v == null || Number.isNaN(v)) return "—";
  const s = v >= 0 ? "+" : "";
  return `${s}$${Math.abs(v).toFixed(2)}`;
}

function fmtPrice(v) {
  if (v == null) return "—";
  return v >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : v.toFixed(2);
}

function renderTelemetry(el, rows) {
  if (!el) return;
  el.innerHTML = (rows || []).map(row =>
    `<div class="row">${row.map(c => `<span class="cell">${c}</span>`).join("")}</div>`
  ).join("");
}

function renderDots(el, count = 32, onCount = 4) {
  if (!el) return;
  el.innerHTML = Array.from({ length: count }, (_, i) => {
    const cls = i < onCount ? "on" : (i % 7 === 0 ? "ok" : "");
    return `<span class="dot ${cls}"></span>`;
  }).join("");
}

function renderBars(hEl, vEl, values) {
  if (!hEl || !vEl) return;
  const data = values && values.length ? values : [0.3, 0.6, 0.4, 0.8, 0.5, 0.7, 0.9, 0.4];
  const max = Math.max(...data, 0.01);
  hEl.innerHTML = data.slice(0, 12).map(v =>
    `<div class="h-bar" style="height:${(v / max) * 100}%"></div>`
  ).join("");
  vEl.innerHTML = data.slice(0, 20).map(v =>
    `<div class="v-bar" style="height:${(v / max) * 100}%"></div>`
  ).join("");
}

function renderMarketsTable(rows) {
  const tbody = document.querySelector("#markets-table tbody");
  if (!tbody) return;
  tbody.innerHTML = (rows || []).map((m, i) => `
    <tr class="${i % 3 === 0 ? "hl" : ""}">
      <td>${m.asset || "—"}</td>
      <td>${m.strike != null ? "$" + Number(m.strike).toLocaleString() : "—"}</td>
      <td>${m.yes_mid != null ? m.yes_mid.toFixed(3) : "—"}</td>
      <td>${m.no_mid != null ? m.no_mid.toFixed(3) : "—"}</td>
      <td>${m.ttl_h != null ? m.ttl_h + "h" : "—"}</td>
      <td title="${m.question || ""}">${(m.question || "").slice(0, 40)}</td>
    </tr>
  `).join("") || `<tr><td colspan="6">No active markets</td></tr>`;
}

function renderSignalsTable(rows) {
  const tbody = document.querySelector("#signals-table tbody");
  if (!tbody) return;
  tbody.innerHTML = (rows || []).slice(0, 10).map(s => `
    <tr>
      <td class="${s.action}">${(s.action || "").toUpperCase()}</td>
      <td class="${(s.edge || 0) >= 0 ? "pos" : "neg"}">${s.edge != null ? (s.edge * 100).toFixed(1) + "%" : "—"}</td>
      <td>${s.label || "—"}</td>
      <td title="${s.question || ""}">${(s.question || "").slice(0, 28)}</td>
    </tr>
  `).join("") || `<tr><td colspan="4">No signals yet</td></tr>`;
}

function renderArbTable(rows) {
  const tbody = document.querySelector("#arb-table tbody");
  if (!tbody) return;
  tbody.innerHTML = (rows || []).slice(0, 8).map(a => `
    <tr>
      <td class="pos">${a.edge != null ? (a.edge * 100).toFixed(1) + "%" : "—"}</td>
      <td>${a.yes_ask != null ? a.yes_ask.toFixed(2) + "/" + a.no_ask.toFixed(2) : "—"}</td>
      <td>${a.status || "—"}</td>
      <td title="${a.question || ""}">${(a.question || "").slice(0, 24)}</td>
    </tr>
  `).join("") || `<tr><td colspan="4">No arb candidates</td></tr>`;
}

function renderGauges(gauges) {
  const el = document.getElementById("gauges");
  if (!el) return;
  el.innerHTML = (gauges || []).map(g => {
    const val = g.unit === "%" ? g.value.toFixed(1) + "%" : "$" + Number(g.value).toFixed(0);
    return `
      <div class="gauge-row">
        <span class="gauge-label">${g.label}</span>
        <div class="gauge-track"><div class="gauge-fill" style="width:${Math.min(100, g.pct)}%"></div></div>
        <span class="gauge-val">${val}</span>
      </div>`;
  }).join("");
}

function renderAlertStrip(collection) {
  const el = document.getElementById("alert-strip");
  if (!el) return;
  const n = 40;
  const active = (collection || []).filter(c => c.rows > 0).length;
  el.innerHTML = Array.from({ length: n }, (_, i) =>
    `<div class="alert-cell ${i < active ? "on" : ""}"></div>`
  ).join("");
}

function updateDashboard(data) {
  const s = data.summary || {};
  const spot = data.spot || {};

  document.getElementById("hdr-mode").textContent = (data.mode || "paper").toUpperCase();
  document.getElementById("hdr-bankroll").textContent = "$" + (data.config?.bankroll || 0).toFixed(0);
  document.getElementById("hdr-pnl").textContent = fmtUsd(s.pnl);
  document.getElementById("hdr-pnl").className = (s.pnl || 0) >= 0 ? "pos" : "neg";
  document.getElementById("hdr-upnl").textContent = fmtUsd(s.unrealized_pnl);
  const kill = data.status?.kill_switch;
  document.getElementById("hdr-kill").textContent = kill ? "ACTIVE" : "OFF";
  document.getElementById("hdr-kill-wrap").classList.toggle("active", !!kill);
  document.getElementById("hdr-sync").textContent = data.error
    ? "ERR"
    : new Date().toLocaleTimeString();
  if (data.error) console.warn("dashboard:", data.error);

  // BTC column
  const btcSpot = spot.BTC?.price;
  document.getElementById("price-btc").textContent = fmtPrice(btcSpot);
  document.getElementById("coord-btc").textContent = btcSpot
    ? `${btcSpot.toFixed(2)} / age ${spot.BTC.age_s || 0}s`
    : "NO FEED";
  renderTelemetry(document.getElementById("tel-btc"), data.telemetry?.btc);
  renderDots(document.getElementById("dots-btc"), 36, btcSpot ? 6 : 0);
  drawSparkline(document.getElementById("spark-btc"), data.series?.BTC?.prices);
  drawWaveform(document.getElementById("wave-btc"), data.series?.BTC?.candles);
  renderBars(document.getElementById("bars-btc"), document.getElementById("vbars-btc"),
    data.series?.BTC?.candles?.map(c => c.v || c.c));

  // ETH column
  const ethSpot = spot.ETH?.price;
  document.getElementById("price-eth").textContent = fmtPrice(ethSpot);
  document.getElementById("coord-eth").textContent = ethSpot
    ? `${ethSpot.toFixed(2)} / age ${spot.ETH.age_s || 0}s`
    : "NO FEED";
  renderTelemetry(document.getElementById("tel-eth"), data.telemetry?.eth);
  renderDots(document.getElementById("dots-eth"), 36, ethSpot ? 5 : 0);
  drawSparkline(document.getElementById("spark-eth"), data.series?.ETH?.prices);
  drawWaveform(document.getElementById("wave-eth"), data.series?.ETH?.candles);
  renderBars(document.getElementById("bars-eth"), document.getElementById("vbars-eth"),
    data.series?.ETH?.candles?.map(c => c.v || c.c));

  // Paper column
  document.getElementById("price-paper").textContent = (s.win_rate || 0).toFixed(1);
  document.getElementById("coord-paper").textContent =
    `E ${((s.expected_edge || 0) * 100).toFixed(1)}% / R ${((s.realized_edge || 0) * 100).toFixed(1)}%`;
  renderTelemetry(document.getElementById("tel-paper"), data.telemetry?.paper);
  renderDots(document.getElementById("dots-paper"), 36, Math.min(12, (s.enter_signals || 0)));
  drawSparkline(document.getElementById("spark-paper"),
    (data.daily_pnl || []).map(d => d.pnl));
  drawWaveform(document.getElementById("wave-paper"),
    (data.edge_buckets || []).map(b => b.pnl || 0));
  renderBars(document.getElementById("bars-paper"), document.getElementById("vbars-paper"),
    (data.edge_buckets || []).map(b => b.orders || 0));

  renderMarketsTable(data.markets);
  renderSignalsTable(data.signals);
  renderArbTable(data.arb?.latest);
  renderGauges(data.risk_gauges);
  renderAlertStrip(data.collection);
  drawNetwork(document.getElementById("network"), data.markets);
  drawTimeline(document.getElementById("timeline"), data.daily_pnl);
}

async function poll() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    updateDashboard(data);
  } catch (err) {
    console.warn("poll failed:", err);
  }
}

// ── Boot ─────────────────────────────────────────────────────────────────────

function boot() {
  initWireframe("wf-btc", "sphere");
  initWireframe("wf-eth", "ring");
  initWireframe("wf-paper", "crystal");
  animateWireframes();

  window.addEventListener("resize", () => {
    Object.values(scenes).forEach(s => s.resize());
    poll();
  });

  poll();
  setInterval(poll, POLL_MS);
}

document.addEventListener("DOMContentLoaded", boot);
