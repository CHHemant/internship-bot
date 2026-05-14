"""Dashboard HTML — single-file embedded UI served by FastAPI."""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Internship Bot — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3e;
    --text: #e2e8f0; --muted: #64748b; --accent: #6366f1;
    --green: #22c55e; --red: #ef4444; --amber: #f59e0b; --blue: #3b82f6;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif; font-size: 14px; }
  header { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 12px; }
  header h1 { font-size: 16px; font-weight: 600; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--red); }
  .dot.live { background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .layout { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 16px; padding: 20px 24px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin-bottom: 10px; }
  .stat { font-size: 32px; font-weight: 700; line-height: 1; }
  .stat-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .section { padding: 0 24px 24px; }
  .section h2 { font-size: 14px; font-weight: 600; margin-bottom: 12px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid3 { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 8px 12px; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); }
  tr:hover td { background: rgba(255,255,255,.02); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
  .badge-submitted  { background:#22c55e22; color:#22c55e; }
  .badge-queued     { background:#6366f122; color:#6366f1; }
  .badge-pending_human { background:#f59e0b22; color:#f59e0b; }
  .badge-error      { background:#ef444422; color:#ef4444; }
  .badge-interview  { background:#3b82f622; color:#3b82f6; }
  .badge-rejected   { background:#64748b22; color:#64748b; }
  .badge-verifying  { background:#8b5cf622; color:#8b5cf6; }
  .badge-generating { background:#ec489922; color:#ec4899; }
  .btn { padding: 6px 14px; border-radius: 6px; border: none; cursor: pointer; font-size: 12px; font-weight: 500; }
  .btn-approve { background: var(--green); color: #000; }
  .btn-skip    { background: var(--border); color: var(--muted); }
  .btn-run     { background: var(--accent); color: #fff; padding: 8px 20px; font-size: 13px; }
  .btn:hover   { opacity: .85; }
  #log { background: #000; border-radius: 8px; padding: 12px; font-family: monospace; font-size: 12px; height: 160px; overflow-y: auto; color: #22c55e; }
  .log-line { margin-bottom: 4px; }
  .log-line span { color: var(--muted); margin-right: 8px; }
  canvas { max-height: 220px; }
  .ats-bar { height: 6px; border-radius: 3px; background: var(--border); margin-top: 4px; }
  .ats-fill { height: 100%; border-radius: 3px; background: var(--accent); transition: width .3s; }
  select, input { background: var(--surface); border: 1px solid var(--border); color: var(--text); padding: 6px 10px; border-radius: 6px; font-size: 13px; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
  .empty { color: var(--muted); text-align: center; padding: 24px; font-size: 13px; }
</style>
</head>
<body>

<header>
  <div class="dot" id="wsDot"></div>
  <h1>🎓 Internship Auto-Apply Bot</h1>
  <div style="flex:1"></div>
  <button class="btn btn-run" onclick="triggerRun()">▶ Run Now</button>
  <span style="color:var(--muted);font-size:12px" id="lastUpdate"></span>
</header>

<!-- KPI cards -->
<div class="layout">
  <div class="card">
    <h2>Total Applications</h2>
    <div class="stat" id="statTotal">—</div>
    <div class="stat-sub">across all platforms</div>
  </div>
  <div class="card">
    <h2>Submitted</h2>
    <div class="stat" id="statSubmitted" style="color:var(--green)">—</div>
    <div class="stat-sub" id="statSubmittedSub">—</div>
  </div>
  <div class="card">
    <h2>Response Rate</h2>
    <div class="stat" id="statResponseRate" style="color:var(--accent)">—</div>
    <div class="stat-sub">interviews + offers</div>
  </div>
  <div class="card">
    <h2>Human Queue</h2>
    <div class="stat" id="statHumanQueue" style="color:var(--amber)">—</div>
    <div class="stat-sub">need your review</div>
  </div>
</div>

<!-- Charts row -->
<div class="section">
  <div class="grid2">
    <div class="card">
      <h2>Applications by Status</h2>
      <canvas id="statusChart"></canvas>
    </div>
    <div class="card">
      <h2>Response Rate by Country</h2>
      <canvas id="countryChart"></canvas>
    </div>
  </div>
</div>

<!-- Applications table + Human queue -->
<div class="section">
  <div class="grid3">
    <div>
      <div class="toolbar">
        <h2 style="margin:0">Applications</h2>
        <select id="filterStatus" onchange="loadApps()">
          <option value="">All statuses</option>
          <option value="submitted">Submitted</option>
          <option value="interview">Interview</option>
          <option value="pending_human">Pending review</option>
          <option value="error">Error</option>
          <option value="queued">Queued</option>
        </select>
        <select id="filterCountry" onchange="loadApps()">
          <option value="">All countries</option>
          <option value="usa">USA</option>
          <option value="germany">Germany</option>
          <option value="canada">Canada</option>
          <option value="netherlands">Netherlands</option>
          <option value="other">Other</option>
        </select>
      </div>
      <div class="card" style="padding:0">
        <table>
          <thead><tr>
            <th>Company</th><th>Role</th><th>Country</th><th>ATS</th><th>Status</th>
          </tr></thead>
          <tbody id="appsTable"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>

    <div>
      <h2 style="margin-bottom:12px">⚠ Human Review Queue</h2>
      <div id="humanQueue">
        <div class="card"><div class="empty">Loading…</div></div>
      </div>
    </div>
  </div>
</div>

<!-- Live event log -->
<div class="section">
  <h2 style="margin-bottom:8px">Live Events</h2>
  <div id="log"><div class="log-line"><span>—</span>Connecting to WebSocket…</div></div>
</div>

<script>
const API = '';
let statusChart, countryChart;

// ── WebSocket ──────────────────────────────────────────────────────────────
const ws = new WebSocket(`ws://${location.host}/ws/updates`);
ws.onopen = () => {
  document.getElementById('wsDot').classList.add('live');
  logEvent('system', 'WebSocket connected');
};
ws.onclose = () => {
  document.getElementById('wsDot').classList.remove('live');
  logEvent('system', 'WebSocket disconnected — refresh to reconnect');
};
ws.onmessage = (e) => {
  const d = JSON.parse(e.data);
  if (d.event === 'ping') return;
  logEvent(d.event, JSON.stringify(d));
  if (['approved','skipped','submitted','run_started'].includes(d.event)) {
    setTimeout(() => { loadAll(); }, 1000);
  }
};

// ── Data loading ───────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadApps(), loadAnalytics(), loadHumanQueue()]);
  document.getElementById('lastUpdate').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

async function loadApps() {
  const status = document.getElementById('filterStatus').value;
  const country = document.getElementById('filterCountry').value;
  let url = `${API}/api/applications?limit=50`;
  if (status) url += `&status=${status}`;
  if (country) url += `&country=${country}`;
  const data = await fetch(url).then(r => r.json()).catch(() => []);
  renderAppsTable(data);
  renderStatusChart(data);
  document.getElementById('statTotal').textContent = data.length;
  const submitted = data.filter(a => ['submitted','interview','offer'].includes(a.status)).length;
  document.getElementById('statSubmitted').textContent = submitted;
  document.getElementById('statSubmittedSub').textContent = `${((submitted/Math.max(data.length,1))*100).toFixed(0)}% of total`;
}

async function loadAnalytics() {
  const data = await fetch(`${API}/api/analytics`).then(r => r.json()).catch(() => ({}));
  if (data.response_rate !== undefined) {
    document.getElementById('statResponseRate').textContent =
      (data.response_rate * 100).toFixed(1) + '%';
    renderCountryChart(data.country_rates || {});
  }
}

async function loadHumanQueue() {
  const data = await fetch(`${API}/api/queue/human`).then(r => r.json()).catch(() => []);
  document.getElementById('statHumanQueue').textContent = data.length;
  renderHumanQueue(data);
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderAppsTable(apps) {
  const tbody = document.getElementById('appsTable');
  if (!apps.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No applications yet. Click ▶ Run Now to start.</td></tr>';
    return;
  }
  tbody.innerHTML = apps.slice(0,40).map(a => `
    <tr>
      <td><strong>${esc(a.company)}</strong></td>
      <td style="color:var(--muted);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(a.title)}</td>
      <td>${a.country.toUpperCase()}</td>
      <td>
        <div style="font-size:12px">${a.ats_score.toFixed(0)}</div>
        <div class="ats-bar"><div class="ats-fill" style="width:${a.ats_score}%"></div></div>
      </td>
      <td><span class="badge badge-${a.status}">${a.status}</span></td>
    </tr>`).join('');
}

function renderHumanQueue(items) {
  const el = document.getElementById('humanQueue');
  if (!items.length) {
    el.innerHTML = '<div class="card"><div class="empty">✓ Queue empty</div></div>';
    return;
  }
  el.innerHTML = items.map(a => `
    <div class="card" style="margin-bottom:10px">
      <div style="font-weight:600;margin-bottom:6px">${esc(a.company)}</div>
      <div style="color:var(--muted);font-size:12px;margin-bottom:4px">${esc(a.title)}</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
        ATS: ${a.ats_score.toFixed(0)} · Retries: ${a.retry_count} · ${a.country.toUpperCase()}
      </div>
      ${(a.error_log||[]).slice(-1).map(e => `<div style="font-size:11px;color:var(--red);margin-bottom:8px">⚠ ${esc(e)}</div>`).join('')}
      <div style="display:flex;gap:8px">
        <button class="btn btn-approve" onclick="approve('${a.id}')">✓ Approve</button>
        <button class="btn btn-skip"    onclick="skip('${a.id}')">Skip</button>
        <a href="${a.url}" target="_blank" style="font-size:11px;color:var(--accent);text-decoration:none;line-height:28px">View ↗</a>
      </div>
    </div>`).join('');
}

function renderStatusChart(apps) {
  const counts = {};
  apps.forEach(a => { counts[a.status] = (counts[a.status]||0)+1; });
  const colors = {submitted:'#22c55e',queued:'#6366f1',pending_human:'#f59e0b',error:'#ef4444',interview:'#3b82f6',rejected:'#64748b',generating:'#ec4899',verifying:'#8b5cf6'};
  const labels = Object.keys(counts);
  const data = { labels, datasets:[{ data: labels.map(l=>counts[l]), backgroundColor: labels.map(l=>colors[l]||'#64748b'), borderWidth:0 }] };
  if (statusChart) statusChart.destroy();
  const ctx = document.getElementById('statusChart').getContext('2d');
  statusChart = new Chart(ctx, { type:'doughnut', data, options:{ plugins:{ legend:{ position:'right', labels:{ color:'#94a3b8', font:{size:11} } } }, cutout:'65%' } });
}

function renderCountryChart(rates) {
  const labels = Object.keys(rates).map(c => c.toUpperCase());
  const values = Object.values(rates).map(v => +(v*100).toFixed(1));
  const data = { labels, datasets:[{ label:'Response %', data: values, backgroundColor:'#6366f1', borderRadius:4 }] };
  if (countryChart) countryChart.destroy();
  const ctx = document.getElementById('countryChart').getContext('2d');
  countryChart = new Chart(ctx, { type:'bar', data, options:{
    plugins:{ legend:{ display:false } },
    scales:{ x:{ ticks:{color:'#94a3b8'}, grid:{color:'#2a2d3e'} }, y:{ ticks:{color:'#94a3b8',callback:v=>v+'%'}, grid:{color:'#2a2d3e'} } }
  }});
}

// ── Actions ────────────────────────────────────────────────────────────────
async function approve(id) {
  await fetch(`${API}/api/applications/${id}/approve`, {method:'POST'});
  logEvent('approve', `Approved ${id}`);
  await loadHumanQueue();
}

async function skip(id) {
  await fetch(`${API}/api/applications/${id}/skip`, {method:'POST'});
  logEvent('skip', `Skipped ${id}`);
  await loadHumanQueue();
}

async function triggerRun() {
  const r = await fetch(`${API}/api/run`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({resume_path:'resume.pdf'})});
  const d = await r.json();
  logEvent('run', `Dispatched ${d.dispatched} tasks`);
}

// ── Log ────────────────────────────────────────────────────────────────────
function logEvent(event, msg) {
  const el = document.getElementById('log');
  const line = document.createElement('div');
  line.className = 'log-line';
  const ts = new Date().toLocaleTimeString();
  line.innerHTML = `<span>${ts}</span><strong>[${event}]</strong> ${esc(msg)}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Init + auto-refresh ────────────────────────────────────────────────────
loadAll();
setInterval(loadAll, 30000); // refresh every 30s
</script>
</body>
</html>"""
