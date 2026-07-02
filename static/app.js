let allTracks = [];
let filtered = [];
let sortCol = "added_at";
let sortDir = -1;
let pollTimer = null;
let canExport = false;          // true when spotify_user_id is available
let currentPlaylistId = null;   // id of the playlist shown in detail view
let genreLabels = {};           // cluster value -> human label (from /api/playlists/genres)

// ─────────────────────────────────────────────────────────────
// Init & auth
// ─────────────────────────────────────────────────────────────

async function init() {
  const res = await fetch("/api/me");
  const data = await res.json();
  if (data.authenticated) {
    canExport = !!data.can_export;
    document.getElementById("login-btn").style.display = "none";
    document.getElementById("logout-btn").style.display = "";
    document.getElementById("welcome").style.display = "none";
    document.getElementById("tab-bar").style.display = "";
    loadTracks();
    loadPlaylists();
  }
}

// ─────────────────────────────────────────────────────────────
// Tab switching
// ─────────────────────────────────────────────────────────────

function switchTab(tab) {
  ["library", "playlists"].forEach(t => {
    document.getElementById(t).style.display = t === tab ? "" : "none";
  });
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  if (tab === "playlists") closePLDetail();
}

// ─────────────────────────────────────────────────────────────
// Library tab
// ─────────────────────────────────────────────────────────────

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
    populateGenreFilter();
    applyFilters();
    document.getElementById("loading").style.display = "none";
    document.getElementById("library").style.display = "";

    if (allTracks.some(t => t.bpm === 0)) startAnalysis();
  } catch (e) {
    document.getElementById("loading-msg").textContent = `Error: ${e.message}`;
  }
}

function setRefreshBtn(disabled, label) {
  const btn = document.getElementById("refresh-genres-btn");
  if (!btn) return;
  btn.disabled = disabled;
  btn.textContent = label || "Actualizar géneros";
}

async function startAnalysis() {
  setRefreshBtn(true, "Actualizando…");
  let data;
  try {
    const res = await fetch("/api/analyze", { method: "POST" });
    if (!res.ok) { console.warn("analyze endpoint error", res.status); setRefreshBtn(false); return; }
    data = await res.json();
  } catch (e) {
    console.warn("startAnalysis fetch failed", e);
    setRefreshBtn(false);
    return;
  }

  const banner = document.getElementById("analysis-banner");
  banner.style.display = "";

  if (data.total === 0 && !data.backfill_count) {
    // Nothing to analyze and no genre backfill — just apply whatever's in cache
    const statusRes = await fetch("/api/analyze/status");
    const status = await statusRes.json();
    applyAnalysisResults(status.results);
    const cached = Object.keys(status.results).length;
    setBannerMsg(cached > 0 ? `${cached} canciones cargadas desde caché` : "Sin canciones para analizar", 100);
    setTimeout(() => { banner.style.display = "none"; }, 4000);
    setRefreshBtn(false);
    return;
  }

  if (data.total === 0 && data.backfill_count) {
    // No new BPM lookups but genre backfill running — poll silently until done
    setBannerMsg(`Actualizando géneros… ${data.backfill_count} canciones`, 0);
  } else {
    setBannerMsg(`Buscando metadatos… 0 / ${data.total} canciones`, 0);
  }
  pollTimer = setInterval(pollAnalysis, 2000);
}

async function pollAnalysis() {
  let data;
  try {
    const res = await fetch("/api/analyze/status");
    data = await res.json();
  } catch (e) { return; }

  applyAnalysisResults(data.results);
  const pct = data.total > 0 ? Math.round((data.done / data.total) * 100) : 100;
  setBannerMsg(`Analizando… ${data.done} / ${data.total} canciones`, pct);

  if (!data.running && !data.backfilling) {
    clearInterval(pollTimer);
    pollTimer = null;
    setRefreshBtn(false);
    setBannerMsg(`Metadatos cargados: ${Object.keys(data.results).length} canciones`, 100);
    setTimeout(() => { document.getElementById("analysis-banner").style.display = "none"; }, 3000);
    const sel = document.getElementById("key-filter");
    sel.innerHTML = '<option value="">All keys</option>';
    populateKeyFilter();
    populateGenreFilter();
  } else if (!data.running && data.backfilling) {
    setBannerMsg("Actualizando géneros…", 99);
  }
}

function applyAnalysisResults(results) {
  if (!results || !Object.keys(results).length) return;
  let changed = false;
  allTracks.forEach(t => {
    const r = results[t.id];
    if (!r || r.error) return;
    if (t.bpm === 0) {
      t.bpm = r.bpm; t.key = r.key; t.camelot = r.camelot; t.energy = r.energy;
      changed = true;
    }
    // Keep Last.fm track tags in a separate field so Spotify artist genres and
    // Last.fm track genres can be displayed side by side.
    if (r.track_genres && r.track_genres.length) {
      t.track_genres = r.track_genres;
      changed = true;
    }
  });
  if (changed) applyFilters();
}

function setBannerMsg(msg, pct) {
  document.getElementById("analysis-msg").textContent = msg;
  document.getElementById("analysis-progress").style.width = `${pct}%`;
}

async function populateGenreFilter() {
  const sel = document.getElementById("pl-genre");
  if (!sel) return;
  let genres;
  try {
    const res = await fetch("/api/playlists/genres");
    if (!res.ok) return;
    genres = (await res.json()).genres || [];
  } catch (e) { return; }

  const current = sel.value;
  sel.innerHTML = '<option value="">Todos los géneros</option>';
  genres.forEach(g => {
    genreLabels[g.value] = g.label;
    const opt = document.createElement("option");
    opt.value = g.value;
    opt.textContent = `${g.label} (${g.count})`;
    sel.appendChild(opt);
  });
  // Restore prior selection if it still exists
  if (current && genres.some(g => g.value === current)) sel.value = current;
}

function populateKeyFilter() {
  const keys = [...new Set(allTracks.map(t => t.key).filter(k => k && k !== "?"))].sort();
  const sel = document.getElementById("key-filter");
  keys.forEach(k => {
    const opt = document.createElement("option");
    opt.value = k; opt.textContent = k;
    sel.appendChild(opt);
  });
}

function applyFilters() {
  const q = document.getElementById("search").value.toLowerCase();
  const bpmMin = parseInt(document.getElementById("bpm-min").value) || 0;
  const bpmMax = parseInt(document.getElementById("bpm-max").value) || 999;
  const keyF   = document.getElementById("key-filter").value;
  const energyMin = parseInt(document.getElementById("energy-min").value) || 0;

  filtered = allTracks.filter(t => {
    if (q) {
      const hay = `${t.title} ${t.artists} ${t.genres.join(" ")} ${(t.track_genres || []).join(" ")} ${t.album}`.toLowerCase();
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
  sortDir = sortCol === col ? sortDir * -1 : (col === "title" || col === "artists" ? 1 : -1);
  sortCol = col;
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
  return `<td class="bar-cell"><div class="bar-wrap"><div class="bar-bg"><div class="bar-fill ${cls}" style="width:${val}%"></div></div><span class="bar-num">${val}</span></div></td>`;
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
    const trackGenres = (t.track_genres || []).map(g => `<span class="genre-pill">${g}</span>`).join("");
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
      <td style="max-width:180px">${trackGenres || '<span class="muted">—</span>'}</td>
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
  return String(str).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function exportCSV() {
  const cols = ["title","artists","genres","track_genres","bpm","camelot","key","energy","danceability","valence","loudness","year","duration","popularity","album","added_at","time_signature","acousticness","instrumentalness"];
  const header = cols.join(",");
  const rows = filtered.map(t =>
    cols.map(c => {
      let v = t[c];
      if (Array.isArray(v)) v = v.join("; ");
      return `"${String(v ?? "").replace(/"/g,'""')}"`;
    }).join(",")
  );
  const csv = [header,...rows].join("\n");
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(new Blob([csv], {type:"text/csv"})),
    download: "spoto-dj-library.csv",
  });
  a.click();
}

// ─────────────────────────────────────────────────────────────
// Playlists tab
// ─────────────────────────────────────────────────────────────

/**
 * Show a live preview of the auto-generated set name below the prefix field.
 * Genre is omitted here (it depends on which tracks get selected); the server
 * fills it in with the actual dominant genre when the set is generated.
 */
function updateNamePreview() {
  const preview = document.getElementById("pl-name-preview");
  if (!preview) return;

  const prefix  = document.getElementById("pl-name").value.trim();
  const dur     = document.getElementById("pl-duration").value + "min";
  const bpmMin  = document.getElementById("pl-bpm-min").value;
  const bpmMax  = document.getElementById("pl-bpm-max").value;

  const now    = new Date();
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const day    = String(now.getDate()).padStart(2, "0");
  const date   = `${months[now.getMonth()]} ${day}`;

  const parts = ["[género]", dur];
  const bpmMinP = parseInt(bpmMin), bpmMaxP = parseInt(bpmMax);
  if (!isNaN(bpmMinP) && !isNaN(bpmMaxP) && bpmMinP < bpmMaxP) {
    parts.push(`${bpmMinP}–${bpmMaxP} BPM`);
  } else if (!isNaN(bpmMinP)) {
    parts.push(`${bpmMinP}+ BPM`);
  } else if (!isNaN(bpmMaxP)) {
    parts.push(`up to ${bpmMaxP} BPM`);
  }
  parts.push(date);

  const autoName = parts.join(" · ");
  preview.textContent = "→ " + (prefix ? `${prefix} · ${autoName}` : autoName);
}

async function generatePlaylist() {
  const btn = document.getElementById("pl-generate-btn");
  const status = document.getElementById("pl-gen-status");
  btn.disabled = true;
  status.textContent = "Generating…";

  const bpmMinRaw = parseInt(document.getElementById("pl-bpm-min").value);
  const bpmMaxRaw = parseInt(document.getElementById("pl-bpm-max").value);
  const bpmMin = isNaN(bpmMinRaw) ? null : bpmMinRaw;
  const bpmMax = isNaN(bpmMaxRaw) ? null : bpmMaxRaw;

  if (bpmMin !== null && bpmMax !== null && bpmMin >= bpmMax) {
    status.textContent = "BPM min must be less than max.";
    btn.disabled = false;
    return;
  }

  let bpmRange = null;
  if (bpmMin !== null || bpmMax !== null) {
    bpmRange = [bpmMin !== null ? bpmMin : 0, bpmMax !== null ? bpmMax : 9999];
  }

  const body = {
    duration_min: parseInt(document.getElementById("pl-duration").value),
    genre_filter: document.getElementById("pl-genre").value || null,
    name:         document.getElementById("pl-name").value.trim() || null,
    bpm_range:    bpmRange,
  };

  try {
    const res = await fetch("/api/playlists/generate", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { status.textContent = `Error: ${data.detail}`; return; }

    status.textContent = `✓ "${data.name}" — ${data.track_count} tracks`;
    document.getElementById("pl-name").value = "";
    await loadPlaylists();
    // Auto-open the new set
    openPLDetail(data.id);
  } catch (e) {
    status.textContent = `Error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function loadPlaylists() {
  try {
    const res = await fetch("/api/playlists");
    if (!res.ok) return;
    const playlists = await res.json();
    renderPlaylists(playlists);
  } catch (e) { console.warn("loadPlaylists failed", e); }
}

function fmtDuration(ms) {
  const totalMin = Math.round(ms / 60_000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}min` : `${m}min`;
}

function fmtTrackDuration(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s/60)}:${String(s%60).padStart(2,"0")}`;
}

function renderPlaylists(playlists) {
  const el = document.getElementById("pl-list");
  if (!playlists.length) {
    el.innerHTML = '<p class="muted" style="padding:16px">No sets yet. Generate your first one above.</p>';
    return;
  }
  el.innerHTML = playlists.map(p => {
    // Mini section arc: render coloured chips per section in order
    const arcChips = (p.sections || []).map(s =>
      `<span class="pl-arc-chip" title="${esc(s.label)} (${s.count} tracks)">${s.emoji}</span>`
    ).join("");

    return `
    <div class="pl-card" onclick="openPLDetail('${p.id}')">
      <div class="pl-card-main">
        <div class="pl-card-name">${esc(p.name)}</div>
        <div class="pl-card-meta">
          <span>${p.track_count} tracks</span>
          <span>${fmtDuration(p.total_duration_ms)}</span>
          ${p.spotify_playlist_url ? `<a class="pl-spotify-link" href="${p.spotify_playlist_url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Open in Spotify ↗</a>` : ""}
        </div>
        ${arcChips ? `<div class="pl-arc-row">${arcChips}</div>` : ""}
        ${p.warnings && p.warnings.length ? `<div class="pl-warning">⚠ ${esc(p.warnings[0])}</div>` : ""}
      </div>
      <div class="pl-card-actions">
        <button class="btn-ghost small" onclick="event.stopPropagation(); deletePL('${p.id}')">Delete</button>
      </div>
    </div>
  `}).join("");
}

async function openPLDetail(pid) {
  currentPlaylistId = pid;
  const res = await fetch(`/api/playlists/${pid}`);
  if (!res.ok) return;
  const p = await res.json();

  document.getElementById("pl-list-wrap").style.display = "none";
  document.querySelector(".pl-generator").style.display = "none";
  document.getElementById("pl-detail").style.display = "";

  document.getElementById("pl-detail-name").textContent = p.name;

  const meta = document.getElementById("pl-detail-meta");
  const params = p.params || {};
  const bpmLabel = params.bpm_range
    ? `${params.bpm_range[0]}–${params.bpm_range[1]} BPM`
    : "";
  meta.innerHTML = `
    <span>${p.track_count} tracks</span>
    <span>${fmtDuration(p.total_duration_ms)}</span>
    ${bpmLabel ? `<span>${bpmLabel}</span>` : ""}
    ${params.genre_filter ? `<span>${esc(genreLabels[params.genre_filter] || params.genre_filter)}</span>` : ""}
  `;

  const warningsEl = document.getElementById("pl-detail-warnings");
  warningsEl.innerHTML = (p.warnings || []).map(w =>
    `<div class="pl-warning">⚠ ${esc(w)}</div>`
  ).join("");

  // Export button state
  const exportBtn = document.getElementById("pl-export-btn");
  const exportStatus = document.getElementById("pl-export-status");
  if (p.spotify_playlist_url) {
    exportBtn.textContent = "Open in Spotify";
    exportBtn.onclick = () => window.open(p.spotify_playlist_url, "_blank");
    exportStatus.textContent = "";
  } else if (!canExport) {
    exportBtn.disabled = true;
    exportStatus.textContent = "Re-login to enable Spotify export";
  } else {
    exportBtn.disabled = false;
    exportBtn.textContent = "Export to Spotify";
    exportBtn.onclick = exportPlaylist;
    exportStatus.textContent = "";
  }

  // Tracklist with section dividers
  const tbody = document.getElementById("pl-detail-tbody");
  let lastSection = null;
  const rows = [];

  p.tracks.forEach(t => {
    // Insert section header row when section changes
    if (t.section && t.section !== lastSection) {
      const emoji = t.section_emoji || "";
      const label = t.section_label || t.section;
      rows.push(`
        <tr class="pl-section-row">
          <td colspan="7">
            <span class="pl-section-badge">${emoji} ${esc(label)}</span>
          </td>
        </tr>
      `);
      lastSection = t.section;
    }

    const energyBar = `<div class="pl-energy-bar" style="width:${t.energy}%"></div>`;
    const camelot = t.camelot && t.camelot !== "?"
      ? camelotBadge(t.camelot)
      : '<span class="muted">?</span>';
    const link = t.spotify_url
      ? `<a href="${t.spotify_url}" target="_blank" rel="noopener">${esc(t.title)}</a>`
      : esc(t.title);

    rows.push(`<tr>
      <td class="muted">${t.position}</td>
      <td>${link}</td>
      <td class="muted">${esc(t.artists)}</td>
      <td><strong>${t.bpm}</strong></td>
      <td>${camelot}</td>
      <td class="pl-energy-cell"><div class="pl-energy-wrap">${energyBar}</div><span>${t.energy}</span></td>
      <td class="muted">${fmtTrackDuration(t.duration_ms)}</td>
    </tr>`);
  });

  tbody.innerHTML = rows.join("");
}

function closePLDetail() {
  currentPlaylistId = null;
  document.getElementById("pl-detail").style.display = "none";
  document.getElementById("pl-list-wrap").style.display = "";
  document.querySelector(".pl-generator").style.display = "";
  loadPlaylists();
}

async function deletePL(pid) {
  if (!confirm("Delete this set?")) return;
  const res = await fetch(`/api/playlists/${pid}`, { method: "DELETE" });
  if (res.ok) loadPlaylists();
}

async function exportPlaylist() {
  if (!currentPlaylistId) return;
  const btn = document.getElementById("pl-export-btn");
  const statusEl = document.getElementById("pl-export-status");
  btn.disabled = true;
  statusEl.textContent = "Exporting…";

  try {
    const res = await fetch(`/api/playlists/${currentPlaylistId}/export`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      statusEl.textContent = `Error: ${data.detail}`;
      btn.disabled = false;
      return;
    }
    statusEl.textContent = "✓ Exported!";
    btn.textContent = "Open in Spotify";
    btn.disabled = false;
    btn.onclick = () => window.open(data.spotify_playlist_url, "_blank");
  } catch (e) {
    statusEl.textContent = `Error: ${e.message}`;
    btn.disabled = false;
  }
}

init();
