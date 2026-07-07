/* TikTok Trend Radar dashboard */

let DATA = null;
let currentNiche = null;
let officialPeriod = "7d";

const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmt(n) {
  if (n == null) return "-";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

function badge(label) {
  const text = {
    rising: "▲ RISING", new: "✦ NEW", steady: "STEADY", cooling: "▼ COOLING",
    active: "● ACTIVE", quiet: "QUIET",
  }[label] || label;
  return `<span class="badge ${esc(label)}">${text}</span>`;
}

// ---------------------------------------------------------------------------
// Sparkline (inline SVG)
// ---------------------------------------------------------------------------

function sparkline(points, width = 110, height = 30) {
  if (!points || points.length === 0) return '<span class="muted small">–</span>';
  if (points.length === 1) {
    return `<svg class="spark" width="${width}" height="${height}"><circle cx="${width - 6}" cy="${height / 2}" r="3" fill="var(--cyan)"/></svg>`;
  }
  const vals = points.map((p) => p.p);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = max - min || 1;
  const step = (width - 12) / (points.length - 1);
  const xy = vals.map((v, i) => [6 + i * step, height - 5 - ((v - min) / range) * (height - 10)]);
  const path = xy.map(([x, y], i) => (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1)).join(" ");
  const up = vals[vals.length - 1] >= vals[0];
  const color = up ? "var(--green)" : "var(--amber)";
  const last = xy[xy.length - 1];
  return `<svg class="spark" width="${width}" height="${height}">
    <path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.5" fill="${color}"/>
  </svg>`;
}

// Trajectory cell: sparkline + explicit change vs previous snapshot + a
// per-day tooltip. `history` is [{date, <key>: value}, ...] oldest→newest.
function trajectoryCell(history, key, unit) {
  const pts = history.map((p) => ({ p: p[key] }));
  const spark = sparkline(pts, 120, 26);
  let label;
  if (history.length < 2) {
    label = '<span class="muted small">first seen today</span>';
  } else {
    const prev = history[history.length - 2][key];
    const cur = history[history.length - 1][key];
    const diff = cur - prev;
    const pct = prev > 0 ? (diff / prev) * 100 : 0;
    const color = pct > 5 ? "var(--green)" : pct < -5 ? "var(--amber)" : "var(--muted)";
    const sign = diff >= 0 ? "+" : "";
    label = `<span class="small" style="color:${color}">${sign}${pct.toFixed(0)}% ${unit} vs prev · ${history.length}d tracked</span>`;
  }
  const tip = history.map((p) => `${p.date}: ${fmt(p[key])} ${unit}`).join("\n");
  return `<div title="${esc(tip)}">${spark}<div>${label}</div></div>`;
}

// Tracked product hashtags: views are cumulative, so show views gained per day.
function trackedTrajectory(history) {
  const pts = history.map((p) => ({ p: p.v }));
  const spark = sparkline(pts, 120, 26);
  let label;
  if (history.length < 2) {
    label = '<span class="muted small">baseline day</span>';
  } else {
    const gained = history[history.length - 1].v - history[history.length - 2].v;
    const color = gained > 0 ? "var(--green)" : "var(--muted)";
    label = `<span class="small" style="color:${color}">+${fmt(Math.max(gained, 0))} views today · ${history.length}d tracked</span>`;
  }
  const tip = history.map((p, i) => {
    const gained = i > 0 ? ` (+${fmt(p.v - history[i - 1].v)})` : "";
    return `${p.date}: ${fmt(p.v)} views${gained}`;
  }).join("\n");
  return `<div title="${esc(tip)}">${spark}<div>${label}</div></div>`;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function renderTabs() {
  const tabs = $("#niche-tabs");
  tabs.innerHTML = "";
  Object.keys(DATA.niches).forEach((label) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = label === currentNiche ? "active" : "";
    b.onclick = () => { currentNiche = label; render(); };
    tabs.appendChild(b);
  });
}

function renderTracked(niche) {
  const card = $("#tracked-card");
  const tags = niche.trackedTags || [];
  if (!tags.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("#tracked-note").textContent = "lifetime hashtag stats, snapshotted each run";
  const tbody = $("#tracked-table tbody");
  tbody.innerHTML = "";
  tags.forEach((t) => {
    const d = t.trend.deltaViews;
    const delta = d == null ? '<span class="muted">baseline</span>'
      : `<span style="color:${d > 0 ? "var(--green)" : "var(--muted)"}">${d > 0 ? "+" : ""}${fmt(d)}</span>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="sound-title"><a href="${esc(t.url)}" target="_blank" rel="noopener">#${esc(t.tag)}</a></td>
      <td>${badge(t.trend.label)}</td>
      <td>${fmt(t.views)}</td>
      <td>${delta}</td>
      <td>${fmt(t.videos)}</td>
      <td>${trackedTrajectory(t.trend.history)}</td>`;
    tbody.appendChild(tr);
  });
}

function renderSounds(niche) {
  const card = $("#sounds-card");
  if (!niche.sounds || !niche.sounds.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const tbody = $("#sounds-table tbody");
  tbody.innerHTML = "";
  const snapshots = DATA.snapshotCount || 1;
  $("#sounds-note").textContent = niche.custom
    ? `${niche.videosSampled} niche videos matched by hashtag/keyword (pooled over recent runs)`
    : snapshots < 3
      ? `Collecting baseline (${snapshots} snapshot${snapshots > 1 ? "s" : ""}) — predictions sharpen after a few daily runs`
      : `${niche.videosSampled} videos sampled`;

  niche.sounds.slice(0, 20).forEach((s, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="muted">${i + 1}</td>
      <td>
        <div class="sound-title"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
          ${s.original ? '<span class="muted small">(original)</span>' : ""}</div>
        <div class="sound-author">${esc(s.author)}</div>
      </td>
      <td>${badge(s.trend.label)}</td>
      <td>${s.videoCount}</td>
      <td>${fmt(s.totalPlays)}</td>
      <td>${trajectoryCell(s.trend.history, "p", "plays")}</td>`;
    tbody.appendChild(tr);
  });
}

function renderHashtags(niche) {
  const card = $("#hashtags-card");
  if (!niche.hashtags || !niche.hashtags.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const ul = $("#hashtag-list");
  ul.innerHTML = "";
  niche.hashtags.slice(0, 15).forEach((h) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span><a href="${esc(h.url)}" target="_blank" rel="noopener">#${esc(h.tag)}</a> ${badge(h.trend.label)}</span>
      <span class="tag-meta">${h.videoCount} videos · ${fmt(h.totalPlays)} plays</span>`;
    ul.appendChild(li);
  });
}

function renderOfficial() {
  const ul = $("#official-list");
  ul.innerHTML = "";
  const list = (DATA.official && DATA.official[officialPeriod]) || [];
  if (!list.length) {
    ul.innerHTML = '<li><span class="muted">Nothing captured on the last run.</span></li>';
    return;
  }
  list.forEach((h) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span>${h.rank}. <a href="https://www.tiktok.com/tag/${esc(h.tag)}" target="_blank" rel="noopener">#${esc(h.tag)}</a>
        ${h.rising ? badge("rising") : ""}</span>
      <span class="tag-meta">${fmt(h.posts)} posts · ${fmt(h.views)} views</span>`;
    ul.appendChild(li);
  });
}

function renderHours(niche) {
  const card = $("#hours-card");
  const ph = niche.postingHours;
  if (!ph || !ph.sampleSize) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const el = $("#hours-chart");
  $("#hours-tz").textContent = `${DATA.timezone} · ${ph.sampleSize} videos`;
  const vals = ph.byHourWeighted;
  const max = Math.max(...vals) || 1;
  const best = new Set(ph.bestHours);
  const W = 560, H = 170, bw = W / 24;
  let bars = "";
  for (let h = 0; h < 24; h++) {
    const bh = (vals[h] / max) * (H - 46);
    const x = h * bw + 2, y = H - 26 - bh;
    const color = best.has(h) ? "var(--pink)" : "var(--card-2)";
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw - 4).toFixed(1)}" height="${Math.max(bh, 2).toFixed(1)}" rx="3" fill="${color}"/>`;
    if (h % 3 === 0)
      bars += `<text x="${(x + bw / 2 - 2).toFixed(1)}" y="${H - 8}" font-size="10" fill="var(--muted)" text-anchor="middle">${h}:00</text>`;
  }
  const bestLabel = ph.bestHours.map((h) => `${h}:00`).join(", ");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">${bars}</svg>
    <p style="margin-top:8px"><b style="color:var(--pink)">Best windows:</b> ${bestLabel}</p>`;
}

function renderVideos(niche) {
  const card = $("#videos-card");
  if (!niche.topVideos || !niche.topVideos.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  const ul = $("#videos-list");
  ul.innerHTML = "";
  niche.topVideos.slice(0, 8).forEach((v) => {
    const li = document.createElement("li");
    const title = v.desc ? v.desc : "(no caption)";
    li.innerHTML = `
      <a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(title)}</a>
      <div class="video-meta">@${esc(v.author)} · ${fmt(v.plays)} plays · ${fmt(v.likes)} likes</div>`;
    ul.appendChild(li);
  });
}

function render() {
  renderTabs();
  const niche = DATA.niches[currentNiche];
  if (!niche) return;
  renderTracked(niche);
  renderSounds(niche);
  renderHashtags(niche);
  renderOfficial();
  renderHours(niche);
  renderVideos(niche);
}

// ---------------------------------------------------------------------------
// "Update now" — trigger the GitHub Action
// ---------------------------------------------------------------------------

function detectRepo() {
  const saved = localStorage.getItem("gh_repo");
  if (saved) return saved;
  // On GitHub Pages: https://<owner>.github.io/<repo>/
  const host = location.hostname;
  const m = host.match(/^([^.]+)\.github\.io$/);
  if (m) {
    const seg = location.pathname.split("/").filter(Boolean)[0];
    if (seg) return `${m[1]}/${seg}`;
  }
  return "";
}

function setStatus(msg, isError = false) {
  const el = $("#update-status");
  if (!msg) { el.classList.add("hidden"); return; }
  el.textContent = msg;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
}

async function gh(path, token, opts = {}) {
  const res = await fetch(`https://api.github.com${path}`, {
    ...opts,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      ...(opts.headers || {}),
    },
  });
  return res;
}

async function triggerUpdate() {
  const repo = detectRepo();
  const token = localStorage.getItem("gh_token");
  if (!repo || !token) { openSettings(); return; }

  const btn = $("#update-btn");
  btn.disabled = true;
  setStatus("Triggering scrape workflow…");

  try {
    const res = await gh(`/repos/${repo}/actions/workflows/scrape.yml/dispatches`, token, {
      method: "POST",
      body: JSON.stringify({ ref: "main" }),
    });
    if (res.status === 404) throw new Error("Repo or workflow not found — check the repo name in settings (and that the token can access it).");
    if (res.status === 401 || res.status === 403) throw new Error("Token rejected — it needs Actions: Read & write on this repo.");
    if (res.status !== 204) throw new Error(`GitHub API returned ${res.status}`);

    setStatus("Workflow started — scraping TikTok now (takes ~3-6 min)…");
    await pollForCompletion(repo, token);
  } catch (e) {
    setStatus(e.message, true);
    btn.disabled = false;
  }
}

async function pollForCompletion(repo, token) {
  const startedAt = Date.now();
  // wait for the run to appear, then for it to complete
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 10000));
    let run = null;
    try {
      const res = await gh(`/repos/${repo}/actions/workflows/scrape.yml/runs?per_page=1`, token);
      run = (await res.json()).workflow_runs?.[0];
    } catch { continue; }
    if (!run || new Date(run.created_at).getTime() < startedAt - 120000) continue;

    if (run.status === "completed") {
      if (run.conclusion === "success") {
        setStatus("Scrape finished! Waiting for the site to redeploy, then reloading…");
        await new Promise((r) => setTimeout(r, 45000));
        location.reload();
      } else {
        setStatus(`Workflow ${run.conclusion} — check the Actions tab on GitHub for logs.`, true);
        $("#update-btn").disabled = false;
      }
      return;
    }
    const mins = Math.round((Date.now() - startedAt) / 60000);
    setStatus(`Scraping in progress… (${mins} min elapsed)`);
  }
  setStatus("Still running — check the Actions tab on GitHub.", true);
  $("#update-btn").disabled = false;
}

// ---------------------------------------------------------------------------
// Settings modal
// ---------------------------------------------------------------------------

function openSettings() {
  $("#cfg-repo").value = detectRepo();
  $("#cfg-token").value = localStorage.getItem("gh_token") || "";
  $("#settings-modal").classList.remove("hidden");
}

function initSettings() {
  $("#settings-btn").onclick = openSettings;
  $("#cfg-cancel").onclick = () => $("#settings-modal").classList.add("hidden");
  $("#cfg-save").onclick = () => {
    const repo = $("#cfg-repo").value.trim();
    const token = $("#cfg-token").value.trim();
    if (repo) localStorage.setItem("gh_repo", repo);
    if (token) localStorage.setItem("gh_token", token);
    $("#settings-modal").classList.add("hidden");
  };
  $("#settings-modal").addEventListener("click", (e) => {
    if (e.target.id === "settings-modal") $("#settings-modal").classList.add("hidden");
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  initSettings();
  $("#update-btn").onclick = triggerUpdate;
  $("#official-period").addEventListener("click", (e) => {
    const p = e.target.dataset.p;
    if (!p) return;
    officialPeriod = p;
    document.querySelectorAll("#official-period button").forEach((b) =>
      b.classList.toggle("active", b.dataset.p === p));
    renderOfficial();
  });

  try {
    const res = await fetch(`data/latest.json?t=${Date.now()}`);
    if (!res.ok) throw new Error("no data");
    DATA = await res.json();
  } catch {
    $("#empty-state").classList.remove("hidden");
    return;
  }

  currentNiche = Object.keys(DATA.niches)[0];
  $("#last-updated").textContent = `Updated ${timeAgo(DATA.generatedAt)}`;
  const ageH = (Date.now() - new Date(DATA.generatedAt).getTime()) / 36e5;
  if (ageH > 36) $("#last-updated").textContent += " ⚠️ (stale — check the Action)";
  $("#dashboard").classList.remove("hidden");
  render();
}

init();
