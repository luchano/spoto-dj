let allTracks = [];
let filtered = [];
let sortCol = "added_at";
let sortDir = -1; // -1 = desc, 1 = asc

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
    const data = await res.json();
    allTracks = data.tracks;
    populateKeyFilter();
    applyFilters();
    document.getElementById("loading").style.display = "none";
    document.getElementById("library").style.display = "";
  } catch (e) {
    document.getElementById("loading-msg").textContent = "Error loading tracks. Please refresh.";
  }
}

function populateKeyFilter() {
  const keys = [...new Set(allTracks.map(t => t.key).filter(Boolean))].sort();
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
      <td>${t.camelot !== "?" ? camelotBadge(t.camelot) : '<span class="muted">—</span>'}</td>
      <td style="color:var(--muted);font-size:12px">${t.key || "—"}</td>
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
