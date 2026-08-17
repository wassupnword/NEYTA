/* Samplette Local — client.
   Playback uses YouTube's official IFrame Player API; nothing is downloaded. */
(() => {
"use strict";

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  track: null,
  mode: "random",
  playlistId: null,
  playlists: [],
  options: { genres: [], styles: [], regions: [], keys: [], tags: [] },
  filters: blankFilters(),
  player: null,
  playerReady: false,
  pendingVideo: null,
  loading: false,
  listCache: [],
};

function blankFilters() {
  return {
    genres: { values: [], match_all: false, exclude: false },
    styles: { values: [], match_all: false, exclude: false },
    regions: { values: [], exclude: false },
    keys: { values: [], exclude: false },
    tempo: { min: null, max: null },
    views: { min: null, max: null },
    year: { min: null, max: null },
    topic_only: false,
  };
}

/* ── api ─────────────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}
const post = (path, body) =>
  api(path, { method: "POST", body: JSON.stringify(body || {}) });

/* ── toast ───────────────────────────────────────────────────── */
let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2400);
}

/* ── youtube player ──────────────────────────────────────────── */
window.onYouTubeIframeAPIReady = () => {
  state.player = new YT.Player("player", {
    height: "100%",
    width: "100%",
    playerVars: {
      autoplay: 1, rel: 0, modestbranding: 1, playsinline: 1,
      origin: window.location.origin,
    },
    events: {
      onReady: () => {
        state.playerReady = true;
        if (state.pendingVideo) {
          loadVideo(state.pendingVideo);
          state.pendingVideo = null;
        }
      },
      onStateChange: (e) => {
        if (e.data === YT.PlayerState.ENDED) nextTrack();
        if (e.data === YT.PlayerState.PLAYING) hideOverlay();
      },
      onError: () => {
        // Region-locked or embed-disabled: skip rather than stall.
        showOverlay("Video unavailable here — skipping…", "");
        setTimeout(nextTrack, 900);
      },
    },
  });
};

function loadYouTubeAPI() {
  const s = document.createElement("script");
  s.src = "https://www.youtube.com/iframe_api";
  document.head.appendChild(s);
}

function loadVideo(id) {
  if (!state.playerReady) { state.pendingVideo = id; return; }
  state.player.loadVideoById(id);
}

function showOverlay(text, sub) {
  $("#overlay-text").textContent = text;
  $("#overlay-sub").textContent = sub || "";
  $("#video-overlay").classList.remove("hidden");
}
const hideOverlay = () => $("#video-overlay").classList.add("hidden");

/* ── rendering ───────────────────────────────────────────────── */
function renderTrack(t) {
  state.track = t;

  $("#yt-title").textContent = `${t.artist} — ${t.title}`;
  $("#yt-channel").textContent =
    [t.yt_channel, t.duration_str, t.views_str].filter(Boolean).join(" · ");
  $("#chan-avatar").textContent = (t.artist || "?").trim().charAt(0) || "?";
  $("#watch-yt").href = `https://www.youtube.com/watch?v=${t.yt_video_id}`;

  const set = (field, value, isList) => {
    const el = $(`[data-f="${field}"]`);
    if (!el) return;
    if (isList && Array.isArray(value) && value.length) {
      el.innerHTML = value
        .map((v) => `<span class="tag">${escapeHtml(v)}</span>`)
        .join("");
      el.classList.remove("empty");
    } else if (!isList && value !== null && value !== undefined && value !== "") {
      el.textContent = value;
      el.classList.remove("empty");
    } else {
      el.textContent = "—";
      el.classList.add("empty");
    }
  };

  set("artist", t.artist);
  set("release", t.release);
  set("year", t.year);
  set("yt_channel", t.yt_channel);
  set("views_str", t.views_str);
  set("musical_key", t.musical_key);
  set("tempo_str", t.tempo_str);
  set("genres", t.genres, true);
  set("styles", t.styles, true);
  set("region", t.region);
  set("label", t.label);
  set("copyright", t.copyright);
  set("p_copyright", t.p_copyright);

  $("#c-fav").classList.toggle("on", !!t.is_favorite);
  $("#bpm-read").textContent = t.tempo ? Math.round(t.tempo) : "Tap";

  highlightInList();
  loadVideo(t.yt_video_id);
  post("/api/history", { track_id: t.id }).catch(() => {});
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function highlightInList() {
  $$(".tl-item").forEach((el) =>
    el.classList.toggle("active",
      state.track && Number(el.dataset.id) === state.track.id));
}

/* ── discovery ───────────────────────────────────────────────── */
async function nextTrack() {
  if (state.loading) return;
  state.loading = true;
  showOverlay("Finding something…", "");

  try {
    const data = await post("/api/next", {
      mode: state.mode,
      filters: state.filters,
      exclude_id: state.track ? state.track.id : null,
      playlist_id: state.playlistId,
    });
    updateStatsChip(data.stats);

    if (!data.track) {
      const s = data.stats || {};
      const stillWorking = (s.pending || 0) > 0 || (s.releases_pending || 0) > 0;
      showOverlay(
        s.ready ? "Nothing matches these filters" : "Building your library…",
        stillWorking
          ? `${s.ready || 0} playable so far — the crawler is still digging.`
          : "Try widening the filters.");
      if (!s.ready) setTimeout(() => { state.loading = false; nextTrack(); }, 6000);
      state.loading = false;
      return;
    }
    renderTrack(data.track);
  } catch (err) {
    showOverlay("Something went wrong", err.message);
  } finally {
    state.loading = false;
  }
}

async function playTrackId(id) {
  try {
    const { track } = await api(`/api/track/${id}`);
    renderTrack(track);
  } catch (err) { toast(err.message); }
}

/* ── track list ──────────────────────────────────────────────── */
async function refreshList() {
  const params = new URLSearchParams({ mode: state.mode, limit: "60" });
  if (state.mode === "playlist" && state.playlistId) {
    params.set("playlist_id", state.playlistId);
  }
  try {
    const { tracks } = await api(`/api/list?${params}`);
    state.listCache = tracks;
    renderList(tracks);
  } catch (_) { /* list is cosmetic; failures shouldn't interrupt playback */ }
}

function renderList(tracks) {
  const box = $("#track-list");
  if (!tracks.length) {
    box.innerHTML = `<p class="empty-note">Nothing here yet.</p>`;
    return;
  }
  box.innerHTML = tracks.map((t, i) => `
    <div class="tl-item" data-id="${t.id}">
      <span class="tl-num">${i + 1}</span>
      <div class="tl-body">
        <div class="tl-title">${escapeHtml(t.title)}</div>
        <div class="tl-sub">${escapeHtml(
          [t.artist, t.year].filter(Boolean).join(" · "))}</div>
      </div>
    </div>`).join("");
  $$(".tl-item", box).forEach((el) =>
    el.addEventListener("click", () => playTrackId(Number(el.dataset.id))));
  highlightInList();
}

function showRelated(tracks, label) {
  if (!tracks.length) { toast(`No ${label} in your library yet`); return; }
  state.listCache = tracks;
  renderList(tracks);
  toast(`${tracks.length} ${label}`);
}

/* ── filters ─────────────────────────────────────────────────── */
async function loadOptions() {
  try {
    state.options = await api("/api/filters/options");
    ["genres", "styles", "regions", "keys"].forEach(renderFacet);
  } catch (_) {}
}

function renderFacet(name, search = "") {
  const wrap = $(`.facet[data-facet="${name}"]`);
  if (!wrap) return;
  const box = $(".chips", wrap);
  const selected = state.filters[name].values;
  const term = search.toLowerCase();
  const values = (state.options[name] || [])
    .filter((v) => !term || v.toLowerCase().includes(term))
    .slice(0, 140);

  // Keep selections visible even when filtered out of the option list.
  selected.forEach((v) => { if (!values.includes(v)) values.unshift(v); });

  box.innerHTML = values.map((v) => `
    <button class="chip-opt ${selected.includes(v) ? "sel" : ""}"
            data-v="${escapeHtml(v)}">${escapeHtml(v)}</button>`).join("")
    || `<span class="empty-note">No values yet</span>`;

  $$(".chip-opt", box).forEach((btn) =>
    btn.addEventListener("click", () => {
      const v = btn.dataset.v;
      const idx = selected.indexOf(v);
      if (idx >= 0) selected.splice(idx, 1); else selected.push(v);
      btn.classList.toggle("sel");
      previewFilters();
    }));
}

function readFilterInputs() {
  $$(".facet").forEach((wrap) => {
    const name = wrap.dataset.facet;
    $$("[data-opt]", wrap).forEach((cb) => {
      state.filters[name][cb.dataset.opt] = cb.checked;
    });
  });
  $$(".range").forEach((wrap) => {
    const name = wrap.dataset.range;
    $$("[data-b]", wrap).forEach((inp) => {
      const raw = inp.value.trim();
      state.filters[name][inp.dataset.b] = raw === "" ? null : Number(raw);
    });
  });
  state.filters.topic_only = $("#f-topic").checked;
}

function writeFilterInputs() {
  $$(".facet").forEach((wrap) => {
    const name = wrap.dataset.facet;
    $$("[data-opt]", wrap).forEach((cb) => {
      cb.checked = !!state.filters[name][cb.dataset.opt];
    });
  });
  $$(".range").forEach((wrap) => {
    const name = wrap.dataset.range;
    $$("[data-b]", wrap).forEach((inp) => {
      const v = state.filters[name][inp.dataset.b];
      inp.value = v === null || v === undefined ? "" : v;
    });
  });
  $("#f-topic").checked = !!state.filters.topic_only;
  ["genres", "styles", "regions", "keys"].forEach((n) => renderFacet(n));
}

function activeFilterCount() {
  let n = 0;
  ["genres", "styles", "regions", "keys"].forEach((k) => {
    if (state.filters[k].values.length) n++;
  });
  ["tempo", "views", "year"].forEach((k) => {
    if (state.filters[k].min !== null || state.filters[k].max !== null) n++;
  });
  if (state.filters.topic_only) n++;
  return n;
}

async function previewFilters() {
  readFilterInputs();
  try {
    const { tracks } = await post("/api/search", {
      filters: state.filters, limit: 1000,
    });
    $("#filter-preview").innerHTML =
      `<b>${tracks.length}${tracks.length === 1000 ? "+" : ""}</b> tracks match`;
  } catch (_) {}
}

/* ── tap tempo ───────────────────────────────────────────────── */
const taps = [];
function tapTempo() {
  const now = performance.now();
  // A gap over 2.5s means a new count-in, not a continuation.
  if (taps.length && now - taps[taps.length - 1] > 2500) taps.length = 0;
  taps.push(now);
  if (taps.length > 8) taps.shift();
  if (taps.length < 2) { $("#bpm-read").textContent = "…"; return; }

  const spans = taps.slice(1).map((t, i) => t - taps[i]);
  const avg = spans.reduce((a, b) => a + b, 0) / spans.length;
  $("#bpm-read").textContent = Math.round(60000 / avg);
}

/* ── playlists ───────────────────────────────────────────────── */
async function loadPlaylists() {
  const { playlists } = await api("/api/playlists");
  state.playlists = playlists;
  $("#opt-playlists").innerHTML = playlists
    .map((p) => `<option value="pl:${p.id}">${escapeHtml(p.name)} (${p.n})</option>`)
    .join("");
}

function renderPlaylistPicker() {
  const inIds = new Set((state.track?.in_playlists || []).map((p) => p.id));
  $("#pl-choices").innerHTML = state.playlists.map((p) => `
    <div class="pl-row">
      <span>${escapeHtml(p.name)}</span>
      <button data-id="${p.id}" class="${inIds.has(p.id) ? "in" : ""}">
        ${inIds.has(p.id) ? "Added" : "Add"}
      </button>
    </div>`).join("") || `<p class="empty-note">No playlists yet.</p>`;

  $$("#pl-choices button").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const pid = Number(btn.dataset.id);
      const isIn = btn.classList.contains("in");
      try {
        if (isIn) {
          await api(`/api/playlists/${pid}/tracks/${state.track.id}`,
                    { method: "DELETE" });
        } else {
          await post(`/api/playlists/${pid}/tracks`, { track_id: state.track.id });
        }
        btn.classList.toggle("in");
        btn.textContent = isIn ? "Add" : "Added";
        const { track } = await api(`/api/track/${state.track.id}`);
        state.track.in_playlists = track.in_playlists;
        loadPlaylists();
      } catch (err) { toast(err.message); }
    }));
}

/* ── stats ───────────────────────────────────────────────────── */
function updateStatsChip(stats) {
  if (!stats) return;
  $("#lib-count").textContent = (stats.ready || 0).toLocaleString();
  $("#crawl-dot").classList.toggle("live", !!(stats.crawler || {}).running);
}

async function pollStats() {
  try {
    const s = await api("/api/stats");
    updateStatsChip(s);
    if (!$("#m-settings").classList.contains("hidden")) renderStats(s);
    // Once the very first tracks land, start playing without a click.
    if (!state.track && s.ready > 0 && !state.loading) nextTrack();
  } catch (_) {}
}

function renderStats(s) {
  $("#stat-grid").innerHTML = `
    <div class="stat"><b>${(s.ready || 0).toLocaleString()}</b><small>playable</small></div>
    <div class="stat"><b>${(s.total || 0).toLocaleString()}</b><small>in catalog</small></div>
    <div class="stat"><b>${(s.pending || 0).toLocaleString()}</b><small>resolving</small></div>
    <div class="stat"><b>${(s.with_key || 0).toLocaleString()}</b><small>with key/BPM</small></div>
    <div class="stat"><b>${(s.favorites || 0).toLocaleString()}</b><small>favorites</small></div>
    <div class="stat"><b>${(s.played || 0).toLocaleString()}</b><small>played</small></div>`;
  const c = s.crawler || {};
  $("#crawl-stage").textContent = c.running
    ? `Crawler: ${c.stage || "idle"}` : "Crawler stopped";
}

/* ── modal helpers ───────────────────────────────────────────── */
const openModal  = (sel) => $(sel).classList.remove("hidden");
const closeModal = (sel) => $(sel).classList.add("hidden");
const anyModalOpen = () => $$(".modal").some((m) => !m.classList.contains("hidden"));

/* ── wiring ──────────────────────────────────────────────────── */
function wire() {
  $("#c-shuffle").addEventListener("click", nextTrack);
  $("#c-filters").addEventListener("click", () => {
    writeFilterInputs(); previewFilters(); openModal("#m-filters");
  });
  $("#c-bpm").addEventListener("click", tapTempo);
  $("#c-copy").addEventListener("click", copyLink);
  $("#c-fav").addEventListener("click", toggleFavorite);
  $("#btn-settings").addEventListener("click", async () => {
    renderStats(await api("/api/stats"));
    const { seeds } = await api("/api/seeds");
    if (seeds && seeds.length) {
      $("#seed-styles").value =
        seeds.map((s) => s.style || s.genre).filter(Boolean).join(", ");
      $("#seed-year-from").value = seeds[0].year_from || "";
      $("#seed-year-to").value = seeds[0].year_to || "";
    }
    openModal("#m-settings");
  });

  $$("[data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest(".modal").classList.add("hidden")));
  $$(".modal").forEach((m) =>
    m.addEventListener("click", (e) => { if (e.target === m) m.classList.add("hidden"); }));

  // filters
  $$(".facet-search").forEach((inp) =>
    inp.addEventListener("input", () =>
      renderFacet(inp.closest(".facet").dataset.facet, inp.value)));
  $$(".facet [data-reset]").forEach((btn) =>
    btn.addEventListener("click", () => {
      const name = btn.closest(".facet").dataset.facet;
      state.filters[name].values = [];
      renderFacet(name);
      previewFilters();
    }));
  $$("[data-opt], .range input, #f-topic").forEach((el) =>
    el.addEventListener("change", previewFilters));

  $("#f-reset").addEventListener("click", () => {
    state.filters = blankFilters();
    writeFilterInputs();
    previewFilters();
  });
  $("#f-apply").addEventListener("click", () => {
    readFilterInputs();
    const n = activeFilterCount();
    $("#filter-count").textContent = n ? String(n) : "";
    closeModal("#m-filters");
    nextTrack();
  });

  // mode
  $("#mode-select").addEventListener("change", (e) => {
    const v = e.target.value;
    if (v.startsWith("pl:")) {
      state.mode = "playlist";
      state.playlistId = Number(v.slice(3));
    } else {
      state.mode = v;
      state.playlistId = null;
    }
    refreshList();
    nextTrack();
  });

  // list menu
  $("#btn-list-menu").addEventListener("click", (e) => {
    e.stopPropagation();
    $("#list-menu").classList.toggle("hidden");
  });
  document.addEventListener("click", () => $("#list-menu").classList.add("hidden"));
  $$("#list-menu button").forEach((btn) =>
    btn.addEventListener("click", () => listMenuAction(btn.dataset.act)));

  // track actions
  $$(".act").forEach((btn) =>
    btn.addEventListener("click", () => trackAction(btn.dataset.act)));

  $("#note-save").addEventListener("click", async () => {
    await post("/api/note", {
      track_id: state.track.id, body: $("#note-body").value,
    });
    state.track.note = $("#note-body").value;
    closeModal("#m-note");
    toast("Note saved");
  });

  $("#pl-create").addEventListener("click", async () => {
    const name = $("#pl-new-name").value.trim();
    if (!name) return;
    try {
      await post("/api/playlists", { name });
      $("#pl-new-name").value = "";
      await loadPlaylists();
      renderPlaylistPicker();
    } catch (err) { toast(err.message); }
  });

  $("#seed-save").addEventListener("click", saveSeeds);
  $("#seed-clear").addEventListener("click", async () => {
    await post("/api/seeds", { seeds: null });
    $("#seed-styles").value = "";
    $("#seed-year-from").value = "";
    $("#seed-year-to").value = "";
    toast("Crawler will roam across everything");
  });

  document.addEventListener("keydown", onKey);
}

async function saveSeeds() {
  const raw = $("#seed-styles").value.trim();
  const from = $("#seed-year-from").value.trim();
  const to = $("#seed-year-to").value.trim();
  if (!raw) { toast("Add at least one style, or use Roam freely"); return; }
  const seeds = raw.split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
    const seed = { style: s };
    if (from) seed.year_from = Number(from);
    if (to) seed.year_to = Number(to);
    return seed;
  });
  await post("/api/seeds", { seeds });
  toast(`Crawler now digging: ${seeds.map((s) => s.style).join(", ")}`);
}

async function toggleFavorite() {
  if (!state.track) return;
  const { is_favorite } = await post("/api/favorite", { track_id: state.track.id });
  state.track.is_favorite = is_favorite;
  $("#c-fav").classList.toggle("on", is_favorite);
  toast(is_favorite ? "Added to favorites" : "Removed from favorites");
  if (state.mode === "favorites") refreshList();
}

async function copyLink() {
  if (!state.track) return;
  const url = `https://www.youtube.com/watch?v=${state.track.yt_video_id}`;
  try {
    await navigator.clipboard.writeText(url);
    toast("YouTube link copied");
  } catch (_) {
    // Clipboard API needs a secure context; localhost qualifies, but fall back.
    const ta = document.createElement("textarea");
    ta.value = url;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    toast("YouTube link copied");
  }
}

async function trackAction(act) {
  if (!state.track) return;
  if (act === "playlist-add") {
    await loadPlaylists();
    renderPlaylistPicker();
    openModal("#m-playlist");
    return;
  }
  if (act === "note") {
    $("#note-track").textContent = `${state.track.artist} — ${state.track.title}`;
    $("#note-body").value = state.track.note || "";
    openModal("#m-note");
    return;
  }
  const kinds = {
    "rel-artist": ["artist", "by this artist"],
    "rel-release": ["release", "from this release"],
    "rel-channel": ["channel", "from this channel"],
    "rel-label": ["label", "on this label"],
    "rel-similar": ["similar", "similar tracks"],
  };
  const entry = kinds[act];
  if (!entry) return;
  const { tracks } = await api(
    `/api/related?track_id=${state.track.id}&kind=${entry[0]}`);
  showRelated(tracks, entry[1]);
}

async function listMenuAction(act) {
  if (act === "new-playlist") {
    await loadPlaylists();
    renderPlaylistPicker();
    openModal("#m-playlist");
  } else if (act === "export") {
    if (state.mode !== "playlist" || !state.playlistId) {
      toast("Pick one of your playlists first");
      return;
    }
    window.location = `/api/playlists/${state.playlistId}/export`;
  } else if (act === "delete-playlist") {
    if (state.mode !== "playlist" || !state.playlistId) {
      toast("Pick one of your playlists first");
      return;
    }
    await api(`/api/playlists/${state.playlistId}`, { method: "DELETE" });
    state.mode = "random";
    state.playlistId = null;
    $("#mode-select").value = "random";
    await loadPlaylists();
    refreshList();
    toast("Playlist deleted");
  }
}

/* Same shortcut map the website uses. */
function onKey(e) {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;

  const key = e.key.toLowerCase();
  if (key === "escape") {
    $$(".modal").forEach((m) => m.classList.add("hidden"));
    return;
  }
  if (anyModalOpen() && key !== "s" && key !== "a") return;

  const setMode = (mode, selectValue) => {
    state.mode = mode;
    state.playlistId = null;
    $("#mode-select").value = selectValue;
    refreshList();
    nextTrack();
  };

  switch (key) {
    case "d": e.preventDefault(); nextTrack(); break;
    case "q": setMode("for_you", "for_you"); break;
    case "w": setMode("popular", "popular"); break;
    case "e": toggleFavorite(); break;
    case "s":
      if ($("#m-filters").classList.contains("hidden")) {
        writeFilterInputs(); previewFilters(); openModal("#m-filters");
      } else closeModal("#m-filters");
      break;
    case "a":
      // The metadata panel is always on screen at desktop widths; on narrow
      // screens it lives further down, so scroll it into view.
      $(".col-meta").scrollIntoView({ behavior: "smooth", block: "center" });
      break;
    case "r": tapTempo(); break;
    case "c": copyLink(); break;
    default: break;
  }
}

/* ── boot ────────────────────────────────────────────────────── */
async function init() {
  wire();
  loadYouTubeAPI();
  showOverlay("Starting up…", "Crawling Discogs for records to dig through.");
  await loadPlaylists();
  await loadOptions();
  refreshList();
  await pollStats();
  setInterval(pollStats, 5000);
  setInterval(loadOptions, 60000);
}

init();
})();
