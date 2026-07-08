/* TikTok Trend Radar dashboard */

let DATA = null;
let CREATOR = null;
let currentNiche = null;
let currentView = null; // "creator" or a niche name
let officialPeriod = "7d";
const CREATOR_TAB = "📊 My Videos";

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
  // special "My Videos" tab first (always available — public stats need no login)
  const cb = document.createElement("button");
  cb.textContent = CREATOR_TAB;
  cb.className = "tab-special" + (currentView === "creator" ? " active" : "");
  cb.onclick = () => { currentView = "creator"; render(); };
  tabs.appendChild(cb);

  Object.keys(DATA.niches).forEach((label) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = currentView === label ? "active" : "";
    b.onclick = () => { currentView = label; currentNiche = label; render(); };
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
    ? DATA.sessionMode === "logged-in"
      ? `${niche.videosSampled} videos pulled live from this niche's hashtags`
      : `${niche.videosSampled} niche videos matched from the explore sample (log in for full hashtag data)`
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
  const creatorMode = currentView === "creator";
  $("#creator-view").classList.toggle("hidden", !creatorMode);
  $("#niche-view").classList.toggle("hidden", creatorMode);
  if (creatorMode) { renderCreator(); return; }

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
// My Videos (creator analytics)
// ---------------------------------------------------------------------------

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function renderCreator() {
  // prefill the URL box from whatever was last analyzed
  const box = $("#creator-urls");
  if (CREATOR && !box.dataset.dirty) {
    const urls = (CREATOR.videos || []).map((v) => v.url)
      .concat((CREATOR.errored || []).map((e) => e.url));
    if (urls.length && !box.value.trim()) box.value = urls.join("\n");
  }

  const hasData = CREATOR && (CREATOR.videos || []).length;
  $("#creator-note").textContent = CREATOR ? (CREATOR.note || "") : "No analysis yet — add videos and save.";

  ["creator-summary-card", "creator-recs-card", "creator-factors-card", "creator-videos-card"]
    .forEach((id) => $("#" + id).classList.toggle("hidden", !hasData));
  if (!hasData) return;

  renderCreatorSummary();
  renderCreatorRecs();
  renderCreatorFactors();
  renderCreatorTable();
}

function renderCreatorSummary() {
  const s = CREATOR.summary || {};
  const chips = [
    ["Videos", CREATOR.count],
    ["Median views", fmt(s.medianViews)],
    ["Total views", fmt(s.totalViews)],
    ["Median engagement", ((s.medianEngagementRate || 0) * 100).toFixed(1) + "%"],
    ["Median length", s.medianDuration != null ? s.medianDuration + "s" : "–"],
    ["Trending-sound use", Math.round((s.trendingSoundShare || 0) * 100) + "%"],
  ];
  $("#creator-summary").innerHTML = chips.map(
    ([k, v]) => `<div class="stat-chip"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`
  ).join("");
  $("#creator-summary-note").textContent = CREATOR.handle && CREATOR.handle !== "your_handle_here"
    ? "@" + CREATOR.handle : "";
}

function renderCreatorRecs() {
  const recs = CREATOR.recommendations || [];
  const card = $("#creator-recs-card");
  if (!recs.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  $("#creator-recs").innerHTML = recs.map((r) => `
    <li><span class="pill ${r.priority === "high" ? "high" : "medium"}">${esc(r.priority)}</span>
      <span><span class="rec-area">${esc(r.area)}.</span> ${esc(r.text)}</span></li>`).join("");
}

function renderCreatorFactors() {
  const factors = CREATOR.factors || [];
  const card = $("#creator-factors-card");
  if (!factors.length) {
    card.classList.remove("hidden");
    $("#creator-factors").innerHTML =
      `<li><span class="muted">${esc(CREATOR.note || "Add more videos to reveal patterns.")}</span></li>`;
    return;
  }
  card.classList.remove("hidden");
  $("#creator-factors").innerHTML = factors.map((f) => `
    <li><span class="pill good">${f.direction === "higher" ? "▲" : "▼"}</span>
      <span>${esc(f.insight)}</span></li>`).join("");
}

function creatorTagChips(v) {
  const hot = new Set([...(v.trendingTagsUsed || []), ...(v.risingTagsUsed || [])]);
  return (v.hashtags || []).slice(0, 6).map(
    (t) => `<span class="tag-chip ${hot.has(t) ? "hot" : ""}">#${esc(t)}</span>`
  ).join("");
}

function renderCreatorTable() {
  const tbody = $("#creator-table tbody");
  tbody.innerHTML = "";
  (CREATOR.videos || []).forEach((v, i) => {
    const growth = (v.growth || []).map((p) => ({ p: p.v }));
    const eng = (v.engagementRate * 100).toFixed(1) + "%";
    const soundBadge = v.usedTrendingSound ? ' <span class="tag-chip hot">trending</span>' : "";
    const soundName = esc((v.sound && v.sound.title) || (v.sound && v.sound.original ? "original" : "–"));
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="muted">${i + 1}</td>
      <td>
        <div class="sound-title"><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.desc ? v.desc.slice(0, 60) : "(no caption)")}</a></div>
        <div>${creatorTagChips(v)}</div>
      </td>
      <td class="mini-eng">${esc(v.postLabel || "–")}</td>
      <td>${fmt(v.views)}</td>
      <td>${eng}</td>
      <td>${fmt(v.saves)}</td>
      <td>${fmt(v.shares)}</td>
      <td>${v.duration ? v.duration + "s" : "–"}</td>
      <td class="mini-eng">${soundName}${soundBadge}</td>
      <td>${sparkline(growth, 90, 24)}</td>`;
    tbody.appendChild(tr);
  });
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

// Save the pasted URLs to my_videos.json in the repo, then trigger a scrape.
function b64(str) {
  return btoa(unescape(encodeURIComponent(str)));
}

async function saveMyVideos() {
  const repo = detectRepo();
  const token = localStorage.getItem("gh_token");
  if (!repo || !token) { openSettings(); return; }

  const urls = $("#creator-urls").value.split(/\s+/)
    .map((s) => s.trim())
    .filter((s) => /\/video\/\d+/.test(s));
  const status = $("#creator-save-status");
  if (!urls.length) { status.textContent = "Paste at least one video URL first."; return; }

  const btn = $("#creator-save");
  btn.disabled = true;
  status.textContent = "Saving to your repo…";
  const content = {
    handle: (CREATOR && CREATOR.handle) || "your_handle_here",
    videos: urls,
  };

  try {
    // fetch existing SHA (required to update an existing file)
    let sha;
    const getRes = await gh(`/repos/${repo}/contents/my_videos.json`, token);
    if (getRes.status === 200) sha = (await getRes.json()).sha;

    const putRes = await gh(`/repos/${repo}/contents/my_videos.json`, token, {
      method: "PUT",
      body: JSON.stringify({
        message: "Update my_videos.json from dashboard",
        content: b64(JSON.stringify(content, null, 2) + "\n"),
        sha,
      }),
    });
    if (putRes.status === 401 || putRes.status === 403)
      throw new Error("Token needs Contents: Read & write on this repo.");
    if (putRes.status !== 200 && putRes.status !== 201)
      throw new Error(`Save failed (GitHub ${putRes.status}).`);

    status.textContent = `Saved ${urls.length} videos. Starting analysis scrape…`;
    // reuse the workflow trigger + poll so the page reloads when done
    await triggerUpdate();
  } catch (e) {
    status.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
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
  $("#creator-save").onclick = saveMyVideos;
  $("#creator-urls").addEventListener("input", (e) => { e.target.dataset.dirty = "1"; });
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

  try {
    const cres = await fetch(`data/creator.json?t=${Date.now()}`);
    if (cres.ok) CREATOR = await cres.json();
  } catch { /* no creator data yet */ }

  currentNiche = Object.keys(DATA.niches)[0];
  // land on My Videos when there's creator data, else the first niche
  currentView = CREATOR && (CREATOR.videos || []).length ? "creator" : currentNiche;
  const mode = DATA.sessionMode === "logged-in"
    ? " · 🔓 logged-in (full hashtag data)"
    : DATA.sessionMode === "logged-out"
      ? " · 🔒 logged-out (limited)"
      : "";
  $("#last-updated").textContent = `Updated ${timeAgo(DATA.generatedAt)}${mode}`;
  const ageH = (Date.now() - new Date(DATA.generatedAt).getTime()) / 36e5;
  if (ageH > 36) $("#last-updated").textContent += " ⚠️ (stale — check the Action)";
  $("#dashboard").classList.remove("hidden");
  render();
}

init();
