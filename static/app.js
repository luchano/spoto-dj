let allTracks = [];
let filtered = [];
let sortCol = "added_at";
let sortDir = -1;
let pollTimer = null;

async function init() {
  const res = await fetch("/api/me");
  const data = await res.json();
  if (data.authenticated) {
    document.getElementById("login-btn").style.display = "none";
    document.getElementById("logout-btn").style.display = "";
    document.getElementById("welcome").style.display = "none";
    loadTracks();
  }
}

async function loadTracks() {
  document.getElementById("loading").style.display = "";
  document.getElementById("library").style.display = "none";

  try {
    const res = await fetch("/api/tracks");
    if (res.status === 401) { location.href = "/login"; return; }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      document.getElementById("loading-msg").textContent = `Error ${res.status}: ${err.detail}`;
      return;
    }
    const data = await res.json();
    allTracks = data.tracks;
    populateKeyFilter();
    applyFilters();
    document.getElementById("loading").style.display = "none";
    document.getElementById("library").style.display = "";

    // Start analysis if any track is missing BPM — backend decides what needs work
    if (allTracks.some(t => t.bpm === 0)) startAnalysis();
  } catch (e) {
    document.getElementById("loading-msg").textContent = `Error: ${e.message}`;
  }
}

async function startAnalysis() {
  const res = await fetch("/api/analyze", { method: "POST" });
  const data = await res.json();

  const banner = document.getElementById("analysis-banner");
  banner.style.display = "";

  if (data.total === 0) {
    // Nothing new to analyze — fetch status to apply cached results
    const statusRes = await fetch("/api/analyze/status");
    const status = await statusRes.json();
    applyAnalysisResults(status.results);
    banner.style.display = "none";
    return;
  }

  updateBanner(0, data.total);
  pollTimer = setInterval(pollAnalysis, 2000);
}

async function pollAnalysis() {
  const res = await fetch("/api/analyze/status");
  const data = await res.json();

  applyAnalysisResults(data.results);
  updateBanner(data.done, data.total);

  if (!data.running) {
    clearInterval(pollTimer);
    pollTimer = null;
    setTimeout(() => {
      document.getElementById("analysis-banner").style.display = "none";
    }, 2000);
    // Refresh key filter with newly discovered keys
    const sel = document.getElementById("key-filter");
    sel.innerHTML = '<option value="">All keys</option>';
    populateKeyFilter();
  }
}

function applyAnalysisResults(results) {
  if (!results || !Object.keys(results).length) return;
  let changed = false;
  allTracks.forEach(t => {
    const r = results[t.id];
    if (r && t.bpm === 0) {
      t.bpm = r.bpm;
      t.key = r.key;
      t.camelot = r.camelot;
      t.energy = r.energy;
      changed = true;
    }
  });
  if (changed) {
    applyFilters();
  }
}

function updateBanner(done, total) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 100;
  document.getElementById("analysis-msg").textContent =
    `Analizando audio… ${done} / ${total} canciones`;
  document.getElementById("analysis-progress").style.width = `${pct}%`;
}

function populateKeyFilter() {
  const keys = [...new Set(allTracks.map(t => t.key).filter(k => k && k !== "?"))].sort();
  const sel = document.getElementById("key-filter");
  keys.forEach(k => {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = k;
    sel.appendChild(opt);
  });
}

function applyFilters() {
  const q = document.getElementById("search").value.toLowerCase();
  const bpmMin = parseInt(document.getElementById("bpm-min").value) || 0;
  const bpmMax = parseInt(document.getElementById("bpm-max").value) || 999;
  const keyF = document.getElementById("key-filter").value;
  const energyMin = parseInt(document.getElementById("energy-min").value) || 0;

  filtered = allTracks.filter(t => {
    if (q) {
      const hay = `${t.title} ${t.artists} ${t.genres.join(" ")} ${t.album}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (t.bpm < bpmMin || t.bpm > bpmMax) return false;
    if (keyF && t.key !== keyF) return false;
    if (t.energy < energyMin) return false;
    return true;
  });

  sortTracks();
}

function sortBy(col) {
  if (sortCol === col) {
    sortDir *= -1;
  } else {
    sortCol = col;
    sortDir = col === "title" || col === "artists" ? 1 : -1;
  }
  document.querySelectorAll("thead th").forEach(th => th.classList.remove("sorted"));
  const th = document.querySelector(`thead th[data-col="${col}"]`);
  if (th) th.classList.add("sorted");
  sortTracks();
}

function sortTracks() {
  filtered.sort((a, b) => {
    let va = a[sortCol], vb = b[sortCol];
    if (typeof va === "string") return va.localeCompare(vb) * sortDir;
    return (va - vb) * sortDir;
  });
  renderTable();
}

function barCell(val, cls) {
  return `<td class="bar-cell">
    <div class="bar-wrap">
      <div class="bar-bg"><div class="bar-fill ${cls}" style="width:${val}%"></div></div>
      <span class="bar-num">${val}</span>
    </div>
  </td>`;
}

function camelotBadge(c) {
  const type = c.endsWith("A") ? "A" : "B";
  return `<span class="camelot ${type}">${c}</span>`;
}

function renderTable() {
  const tbody = document.getElementById("track-tbody");
  document.getElementById("track-count").textContent = `${filtered.length} of ${allTracks.length} tracks`;

  tbody.innerHTML = filtered.map(t => {
    const genres = t.genres.map(g => `<span class="genre-pill">${g}</span>`).join("");
    const img = t.image_url
      ? `<img class="cover" src="${t.image_url}" alt="" loading="lazy" />`
      : `<div class="cover" style="background:var(--bg3)"></div>`;
    const link = t.spotify_url
      ? `<a class="track-link" href="${t.spotify_url}" target="_blank" rel="noopener">${esc(t.title)}</a>`
      : esc(t.title);

    return `<tr>
      <td class="cover-cell">${img}</td>
      <td style="max-width:200px">${link}</td>
      <td style="max-width:160px">${esc(t.artists)}</td>
      <td style="max-width:180px">${genres || '<span class="muted">—</span>'}</td>
      <td><strong>${t.bpm || "—"}</strong></td>
      <td>${t.camelot && t.camelot !== "?" ? camelotBadge(t.camelot) : '<span class="muted">—</span>'}</td>
      <td style="color:var(--muted);font-size:12px">${t.key && t.key !== "?" ? t.key : "—"}</td>
      ${barCell(t.energy, "energy")}
      ${barCell(t.danceability, "dance")}
      ${barCell(t.valence, "valence")}
      <td style="color:var(--muted)">${t.loudness} dB</td>
      <td>${t.year}</td>
      <td style="color:var(--muted)">${t.duration}</td>
      <td>${t.popularity}</td>
    </tr>`;
  }).join("");
}

function esc(str) {
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function exportCSV() {
  const cols = ["title","artists","genres","bpm","camelot","key","energy","danceability","valence","loudness","year","duration","popularity","album","added_at","time_signature","acousticness","instrumentalness"];
  const header = cols.join(",");
  const rows = filtered.map(t =>
    cols.map(c => {
      let v = t[c];
      if (Array.isArray(v)) v = v.join("; ");
      return `"${String(v ?? "").replace(/"/g, '""')}"`;
    }).join(",")
  );
  const csv = [header, ...rows].join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "spoto-dj-library.csv";
  a.click();
  URL.revokeObjectURL(url);
}

init();
