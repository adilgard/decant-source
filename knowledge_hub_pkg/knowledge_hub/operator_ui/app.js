/* Operator console logic (BP20) — replaces the design-tool DCLogic runtime.
 *
 * Every renderVals() key from Design's spec maps to a real source; every
 * fake is gone:
 *   landed/facts + stage counts + per-source progress -> GET /v1/monitor
 *   p95            -> /v1/monitor.p95_ms (proxied serving §4 number)
 *   perMin/bars[]  -> /v1/monitor.throughput (no sine or random fakes)
 *   feed[]/statusLine -> GET /v1/monitor/activity (the mock's hardcoded
 *                     line pool is gone)
 *   uptime         -> /v1/monitor.uptime_s + a local ticker
 *   refreshLabel   -> the real poll cadence below
 *   queue counts + items + candidate pair -> GET /v1/reviews[/id]
 *   decide()       -> POST /v1/actions/resolve_merge | split_merge (BP19);
 *                     skip requeues client-side, no write, no counter.
 *
 * Identity: the operator credential is held in memory + sessionStorage for
 * THIS session only, sent as a Bearer header, never logged, never rendered.
 * All data rides the operator service (:8081, same origin as this page) —
 * there is no DB side door and no cross-origin call.
 */
"use strict";

const POLL_MS = 5000;
const REVIEW_POLL_MS = 15000;
const TOKEN_KEY = "kh_operator_token";

const state = {
  token: null,
  tenant: null,
  tab: "monitor",
  monitor: null,
  health: null,
  uptimeBase: null,      // {uptime_s, at}
  reviews: { counts: null, items: [] },
  queue: [],             // client-side working queue (skip rotates it)
  current: null,         // loaded review detail
  cleared: 0,
  lastMergeId: null,     // S reverses the last merge made this session
  offline: false,
  timers: [],
};

const $ = (id) => document.getElementById(id);
const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString();

const OTHER_TITLES = {
  sources: "Sources & access",
  health: "Errors & health",
  topology: "System connections",
  landing: "Data landing",
  inference: "Inference",
  facts: "Facts & entities",
};

/* ------------------------------------------------------------------ fetch */
async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers["Authorization"] = "Bearer " + state.token;
  if (opts.body) opts.headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    setOffline(true);
    throw e;
  }
  setOffline(false);
  if (resp.status === 401) {
    // F1: a sealed/unreachable vault refuses every credential — diagnose
    // via the health surface before blaming the token.
    lock("");
    credentialFailureMessage("The credential is no longer recognized.")
      .then((msg) => { $("lock-msg").textContent = msg; });
    throw new Error("unauthorized");
  }
  return resp;
}

async function credentialFailureMessage(fallback) {
  // F1: /v1/health answers unauthenticated and knows whether the vault is
  // SEALED (routine after any reboot) or unreachable — both make every
  // credential "not recognized" while the credential itself is fine.
  try {
    const h = await (await fetch("/v1/health")).json();
    if (h.vault_status === "sealed")
      // BP28 #17: BAO_ADDR is required — without it the CLI talks HTTPS
      // to a plain-HTTP listener and the printed recovery command fails.
      return "The vault is SEALED (normal after a reboot) — your credential is probably fine. Unseal with 3 custody shares: docker exec -it -e BAO_ADDR=http://127.0.0.1:8200 kh-openbao bao operator unseal (run 3x), then try again.";
    if (h.vault === false)
      return "The vault is unreachable, so no credential can be checked. Bring the stack up (docker compose up -d in the deployment home), then try again.";
  } catch (e) { /* health itself unreachable — fall through to the fallback */ }
  return fallback;
}

function setOffline(isOffline) {
  state.offline = isOffline;
  $("offline").classList.toggle("kh-hide", !isOffline);
  $("system-state").textContent = isOffline ? "SYSTEM : UNREACHABLE" : "SYSTEM : NOMINAL";
  $("system-state").style.color = isOffline ? "#ff9b83" : "#7be0c8";
}

/* ------------------------------------------------------------------- auth */
function lock(message) {
  state.token = null;
  sessionStorage.removeItem(TOKEN_KEY);
  $("lock").classList.remove("kh-hide");
  $("lock-msg").textContent = message || "";
  $("token-input").value = "";
  $("token-input").focus();
}

async function unlock(token) {
  let resp;
  try {
    resp = await fetch("/v1/actions", { headers: { Authorization: "Bearer " + token } });
  } catch (e) {
    $("lock-msg").textContent = "Can't reach the system — is the operator service running?";
    return;
  }
  if (resp.status === 403) {
    // F3: valid token, wrong KIND — the agent serving credential resolves
    // but has no console role. The server says so; show its words.
    const body = await resp.json().catch(() => ({}));
    $("lock-msg").textContent = body.detail ||
      "This credential has no console role — log in with the OPERATOR CONSOLE credential.";
    return;
  }
  if (resp.status !== 200) {
    $("lock-msg").textContent =
      await credentialFailureMessage("That credential was not recognized.");
    return;
  }
  const body = await resp.json();
  state.token = token;
  state.tenant = body.tenant_id;
  sessionStorage.setItem(TOKEN_KEY, token);
  $("lock").classList.add("kh-hide");
  $("tenant-name").textContent = String(body.tenant_id).toUpperCase();
  $("viewport-line").textContent = "viewport 01 · tenant : " + body.tenant_id;
  startPolling();
}

/* ---------------------------------------------------------------- monitor */
const NO_ROLE_MSG = "This credential has no console role — log in with the OPERATOR CONSOLE credential.";

async function refreshMonitor() {
  const [monResp, healthResp] = await Promise.all([
    api("/v1/monitor"), fetch("/v1/health"),
  ]);
  // F3: a read-403 must never leave a blank dashboard under NOMINAL.
  if (monResp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (monResp.status !== 200) return;
  const mon = await monResp.json();
  const health = healthResp.ok ? await healthResp.json() : null;
  state.monitor = mon;
  state.health = health;
  state.uptimeBase = { uptime_s: mon.uptime_s, at: Date.now() };
  renderMonitor();
}

function gauge(el, pctEl, pct, color) {
  const p = Math.max(0, Math.min(100, Math.round(pct)));
  el.style.background = "conic-gradient(" + color + " " + p + "%,rgba(255,255,255,.08) 0)";
  pctEl.textContent = p + "%";
}

function renderMonitor() {
  const m = state.monitor;
  if (!m) return;
  const h = state.health;

  // Tile 1 — live component health (postgres · vault · serving), consistent
  // with check_stack and the footer. BP28 #22: the third component is the
  // serving process itself (monitor.serving_status), not whether a p95
  // sample exists — a healthy-but-quiet box reads fully green ("warming"),
  // and only a component that is actually down degrades the tile.
  const comps = [
    { name: "postgres", ok: !!(h && h.postgres) },
    { name: "vault", ok: !!(h && h.vault),
      bad: h && h.vault_status === "sealed" ? "SEALED" : "DEGRADED" },
    { name: "serving", ok: m.serving_status !== "down", bad: "DOWN",
      note: m.serving_status === "warming" ? "warming" : null },
  ];
  const green = comps.filter((c) => c.ok).length;
  const allGreen = green === comps.length;
  $("tile-health-num").textContent = green + " / " + comps.length;
  gauge($("tile-health-gauge"), $("tile-health-pct"), 100 * green / comps.length,
    allGreen ? "#7be0c8" : "#ff9b83");
  $("tile-health-sub").textContent = comps.map((c) =>
    c.ok ? c.name + (c.note ? " (" + c.note + ")" : "")
         : c.name + " " + (c.bad || "DEGRADED")
  ).join(" · ") + (h ? " · v" + h.version : "");

  // Tile 2 — documents landed.
  $("tile-landed").textContent = fmt(m.landed);
  const nSources = m.sources.length;
  const done = m.sources.filter((s) => s.backfill_done).length;
  $("tile-landed-sub").textContent = nSources
    ? nSources + " source" + (nSources === 1 ? "" : "s") + " registered · " + done + " fully swept"
    : "no sources registered yet";
  gauge($("tile-landed-gauge"), $("tile-landed-pct"), nSources ? 100 * done / nSources : 0, "#9fc0ff");

  // Tile 3 — awaiting human.
  const r = m.review;
  $("tile-review-num").textContent = fmt(r.total);
  $("tile-review-sub").textContent =
    fmt(r.merges) + " merges · " + fmt(r.quarantined) + " quarantined · " + fmt(r.flagged) + " flagged";
  gauge($("tile-review-gauge"), $("tile-review-pct"), r.total ? 100 * r.merges / r.total : 0, "#eadf9a");
  $("badge-review").textContent = fmt(r.total);

  // Tile 4 — serving p95 vs the §4 budget.
  $("tile-p95").textContent = m.p95_ms === null ? "—" : Math.round(m.p95_ms);
  gauge($("tile-p95-gauge"), $("tile-p95-pct"),
    m.p95_ms === null ? 0 : 100 * m.p95_ms / m.p95_budget_ms, "#c9b8ff");

  // Pipeline strip.
  const st = m.stages;
  const counts = [st.capture.count, st.process.count, st.extract.count, st.resolve.count, st.facts.count];
  const top = Math.max.apply(null, counts.concat([1]));
  const bar = (id, n) => { $(id).style.inset = "0 " + Math.round(100 - 100 * n / top) + "% 0 0"; };
  $("st-capture-n").textContent = fmt(st.capture.count);
  $("st-capture-foot").textContent = "in flight: " + fmt(st.capture.in_flight);
  bar("st-capture-bar", st.capture.count);
  $("st-process-n").textContent = fmt(st.process.count);
  $("st-process-foot").textContent = "queue depth " + fmt(st.process.queue_depth) + " · bge-m3 1024-dim";
  bar("st-process-bar", st.process.count);
  $("st-extract-n").textContent = fmt(st.extract.count);
  $("st-extract-sub").textContent = fmt(st.extract.facts_staged) + " facts staged";
  $("st-extract-foot").textContent = fmt(st.extract.quarantined) + " quarantined · qwen3.6";
  bar("st-extract-bar", st.extract.count);
  $("st-resolve-n").textContent = fmt(st.resolve.count);
  $("st-resolve-foot").textContent = fmt(st.resolve.held_for_review) + " held for review — not merged";
  bar("st-resolve-bar", st.resolve.count);
  $("st-facts-n").textContent = fmt(st.facts.count);
  $("st-facts-foot").textContent = fmt(st.facts.confident) + " confident · " + fmt(st.facts.low_confidence) + " low-confidence";
  bar("st-facts-bar", st.facts.count);

  $("pipeline-status").textContent =
    fmt(st.capture.in_flight) + " document(s) in capture flight · process queue depth " +
    fmt(st.process.queue_depth) + " · resolve is holding " + fmt(st.resolve.held_for_review) +
    " pair(s) it will not decide alone.";

  renderSources(m.sources);
  renderBars(m.throughput);
  // F5: the badge counts the operator_alerts view (unacknowledged failed
  // queue items + degraded sources) — the old status='error' count was
  // structurally 0 while documents failed.
  $("badge-health").textContent = fmt(m.alerts_open);
}

function sourceRow(s) {
  const row = document.createElement("div");
  const paused = s.status === "disabled";
  const degraded = s.status === "degraded";
  const dot = degraded
    ? '<div style="width:10px;height:10px;border-radius:50%;background:#ff9b83;box-shadow:0 0 10px rgba(255,140,110,.8);animation:khBlink 1.4s ease-in-out infinite"></div>'
    : '<div style="width:10px;height:10px;border-radius:50%;background:' + (paused ? "#8fa8d8" : "#7be0c8") + ';box-shadow:0 0 8px rgba(110,230,200,.8)"></div>';
  const tag = degraded
    ? '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:#ffcabb;padding:2px 8px;border-radius:8px;border:1px solid rgba(255,150,130,.4);background:rgba(255,110,90,.1)">DEGRADED</div>'
    : paused
      ? '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:#8fa8d8;padding:2px 8px;border-radius:8px;border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.06)">PAUSED</div>'
      : '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:#7be0c8;padding:2px 8px;border-radius:8px;border:1px solid rgba(123,224,200,.35);background:rgba(123,224,200,.08)">' + (s.backfill_done ? "WATCH" : "PULL & WATCH") + "</div>";
  const barStyle = degraded
    ? "background:linear-gradient(90deg,#d96a52,#ff9b83);box-shadow:0 0 12px rgba(255,140,110,.55)"
    : "background:linear-gradient(90deg,#4fbfa4,#7be0c8);box-shadow:0 0 12px rgba(110,230,200,.5)";
  const note = s.status_reason
    ? s.status_reason
    : (s.backfill_done ? "idle · idempotent re-sweeps" : "backfill in progress");
  const btnLabel = paused ? "Resume" : "Pause";
  row.innerHTML =
    '<div>' +
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">' + dot +
    '<div style="font-size:12.5px;color:#dbe7ff;font-weight:500">' + esc(s.source_system) + " · " + esc(s.source_ref) + "</div>" + tag +
    '<div style="flex:1"></div>' +
    '<div style="font-size:12px;color:#c9d8f8;font-family:Consolas,\'Lucida Console\',monospace">' + fmt(s.landed) + (s.total ? " / " + fmt(s.total) : "") + " landed</div></div>" +
    '<div style="position:relative;height:12px;border-radius:6px;overflow:hidden;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1)">' +
    '<div style="position:absolute;inset:0 ' + (paused ? 100 : s.backfill_done ? 0 : 40) + '% 0 0;border-radius:6px;' + barStyle + '"></div></div>' +
    '<div style="display:flex;align-items:center;gap:10px;margin-top:7px">' +
    '<div style="font-size:11px;color:' + (degraded ? "#ffcabb" : "#8fa8d8") + '">' + esc(note) + "</div>" +
    '<div style="flex:1"></div>' +
    '<div data-toggle="' + esc(s.source_ref) + '" data-paused="' + paused + '" style="cursor:pointer;font-size:11px;color:#dbe7ff;padding:4px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.22);background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.04))">' + btnLabel + "</div></div></div>";
  row.querySelector("[data-toggle]").addEventListener("click", async (e) => {
    const ref = e.target.getAttribute("data-toggle");
    const wasPaused = e.target.getAttribute("data-paused") === "true";
    const action = wasPaused ? "resume_source" : "pause_source";
    try {
      const resp = await api("/v1/actions/" + action, {
        method: "POST", body: JSON.stringify({ source_ref: ref }) });
      if (resp.status === 403) e.target.textContent = "operator role required";
      else await refreshMonitor();
    } catch (err) { /* offline banner already up */ }
  });
  return row;
}

function renderSources(sources) {
  const list = $("sources-list");
  list.textContent = "";
  if (!sources.length) {
    list.innerHTML = '<div style="font-size:11px;color:#5c6f9e">no sources registered for this tenant yet</div>';
    return;
  }
  sources.forEach((s) => list.appendChild(sourceRow(s)));
}

function renderBars(throughput) {
  const wrap = $("bars");
  wrap.textContent = "";
  const series = throughput.series;
  const top = Math.max.apply(null, series.concat([1]));
  series.forEach((n) => {
    const bar = document.createElement("div");
    bar.setAttribute("style",
      "flex:1;border-radius:1px;background:linear-gradient(180deg,#9fc0ff,#4d6bff);opacity:.8;height:" +
      Math.max(4, Math.round(100 * n / top)) + "%");
    wrap.appendChild(bar);
  });
  $("permin").textContent = fmt(throughput.per_min);
}

async function refreshActivity() {
  const resp = await api("/v1/monitor/activity");
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) return;
  const events = (await resp.json()).events;
  const feed = $("feed");
  feed.textContent = "";
  events.slice(0, 9).forEach((line) => {
    const row = document.createElement("div");
    row.setAttribute("style", "display:flex;gap:9px;padding:5px 8px;border-left:2px solid rgba(140,170,255,.5)");
    row.innerHTML = '<div style="color:#7db4e8;flex:none">' + esc(line.time) + "</div>" +
      '<div style="color:#b9cdf5">' + esc(line.text) + "</div>";
    feed.appendChild(row);
  });
  if (!events.length) {
    feed.innerHTML = '<div style="color:#5c6f9e;padding:5px 8px">quiet — nothing has moved yet</div>';
    $("status-line").textContent = "watching";
  } else {
    $("status-line").textContent = events[0].text.replace(/\s+/g, " ");
  }
}

function tickUptime() {
  if (!state.uptimeBase) return;
  const s = state.uptimeBase.uptime_s + Math.floor((Date.now() - state.uptimeBase.at) / 1000);
  const d = Math.floor(s / 86400);
  const pad = (n) => String(n).padStart(2, "0");
  $("uptime").textContent = d + "d " + pad(Math.floor(s / 3600) % 24) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60);
}

/* ----------------------------------------------------------------- review */
// BP28 #23/#24: the DECIDE button labels and the key legend follow the item
// TYPE — the same keys post different actions per kind (see decide()), so
// the words must say which. merge → merge / keep separate; quarantine →
// resolve / dismiss; flagged → resolve only (R does nothing there).
const REVIEW_KINDS = {
  merge: {
    a: "Merge them", r: "Keep separate",
    keys: "A merge · R keep separate · S split · space skip",
  },
  quarantine: {
    a: "Resolve — record the correction", r: "Dismiss",
    keys: "A resolve · R dismiss · space skip",
  },
  flagged: {
    a: "Resolve — tag stands corrected", r: null,
    keys: "A resolve · space skip",
  },
};
async function refreshReviews(keepCurrent) {
  const resp = await api("/v1/reviews");
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) return;
  const body = await resp.json();
  state.reviews = body;
  const currentId = state.current && keepCurrent ? state.current.id : null;
  state.queue = body.items.slice();
  $("qc-merges").textContent = fmt(body.counts.merges);
  $("qc-quarantine").textContent = fmt(body.counts.quarantined);
  $("qc-flagged").textContent = fmt(body.counts.flagged);
  $("badge-review").textContent = fmt(body.counts.total);
  renderQueueList();
  if (!state.queue.length) { showEmptyQueue(); return; }
  const still = currentId && state.queue.find((i) => i.id === currentId);
  await selectItem(still ? currentId : state.queue[0].id);
}

function showEmptyQueue() {
  state.current = null;
  $("queue-empty").classList.remove("kh-hide");
  $("review-detail").classList.add("kh-hide");
  $("queue-list").textContent = "";
}

function renderQueueList() {
  const list = $("queue-list");
  list.textContent = "";
  state.queue.slice(0, 7).forEach((item, ix) => {
    const selected = state.current && state.current.id === item.id || (!state.current && ix === 0);
    const row = document.createElement("div");
    row.setAttribute("style", selected
      ? "cursor:pointer;padding:9px 11px;border-radius:12px;border:1px solid rgba(160,190,255,.55);background:rgba(120,150,255,.14);box-shadow:0 0 12px rgba(110,140,255,.2)"
      : "cursor:pointer;padding:9px 11px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.03)");
    row.innerHTML =
      '<div style="font-size:12px;color:' + (selected ? "#e9f0ff;font-weight:600" : "#b9cdf5") + '">' + esc(item.title) + "</div>" +
      '<div style="font-size:11px;color:' + (selected ? "#8fa8d8" : "#5c6f9e") + '">' + esc(item.subtitle) + "</div>";
    row.addEventListener("click", () => selectItem(item.id));
    list.appendChild(row);
  });
}

async function selectItem(id) {
  const resp = await api("/v1/reviews/" + id);
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) { await refreshReviews(false); return; }
  state.current = await resp.json();
  $("queue-empty").classList.add("kh-hide");
  $("review-detail").classList.remove("kh-hide");
  renderQueueList();
  renderDetail();
}

function candRows(el, cand) {
  el.textContent = "";
  if (!cand) return;
  const rows = [];
  const ids = cand.identifiers || {};
  Object.keys(ids).sort().slice(0, 4).forEach((k) => rows.push(k + " · " + String(ids[k])));
  if (!Object.keys(ids).length) rows.push('identifiers · <span style="color:#5c6f9e">none recorded</span>');
  rows.push("aliases · " + (cand.aliases && cand.aliases.length ? esc(cand.aliases.join(", ")) : "—"));
  rows.push("appears in · " + fmt(cand.document_count) + " document(s)");
  rows.forEach((text) => {
    const div = document.createElement("div");
    div.innerHTML = text;
    el.appendChild(div);
  });
}

function renderDetail() {
  const d = state.current;
  if (!d) return;
  const ix = state.queue.findIndex((i) => i.id === d.id);
  $("rv-itempos").textContent = "item " + (ix + 1) + " of " + fmt(state.reviews.counts.total);

  if (d.kind === "merge") {
    $("rv-question").textContent = d.question;
    $("rv-blurb").textContent = "The resolver stopped here on purpose: the score fell in the gray band and it will not decide alone.";
    $("rv-scorebar").classList.remove("kh-hide");
    $("rv-cands").classList.remove("kh-hide");
    $("rv-evidence").classList.remove("kh-hide");
    $("rv-score").textContent = d.score.toFixed(2);
    $("rv-marker").style.left = Math.round(100 * Math.max(0, Math.min(1, d.score))) + "%";
    const t = d.thresholds || {};
    $("rv-threshold-note").textContent = t.t_high
      ? "auto-merge needs " + t.t_high.toFixed(2) + (t.requires_corroboration ? " + a corroborating identifier or graph edge" : "")
      : "no policy row for this type — conservative fallback held it";
    const a = d.candidate_a, b = d.candidate_b;
    $("ca-name").textContent = a ? a.name : "—";
    $("ca-sub").textContent = a ? (a.entity_type || "?") + " · " + fmt(a.fact_count) + " facts" + (a.first_seen ? " · first seen " + a.first_seen.slice(0, 10) : "") : "";
    candRows($("ca-rows"), a);
    $("cb-name").textContent = b ? b.name : "—";
    $("cb-sub").textContent = b ? (b.entity_type || "?") + " · " + (b.role === "mention" ? "1 mention" : fmt(b.fact_count) + " facts") + (b.first_seen ? " · seen " + b.first_seen.slice(0, 10) : "") : "";
    candRows($("cb-rows"), b);
    fillList($("ev-for"), d.evidence_for, "nothing recorded in favour");
    fillList($("ev-against"), d.evidence_against, "nothing recorded against");
    renderPassage(d.passage, b ? b.name : null);
  } else {
    $("rv-question").textContent = d.kind === "quarantine"
      ? "Quarantined extraction — " + (d.detail || d.reason)
      : "Flagged document — " + (d.title || "");
    $("rv-blurb").textContent = d.kind === "quarantine"
      ? "The extractor produced something the ontology does not bind (" + d.reason + "). A = resolve · R = dismiss."
      : "Capture detected a data-track mismatch (" + (d.review_reason || "") + "). A = resolve, tag stands corrected.";
    $("rv-scorebar").classList.add("kh-hide");
    $("rv-cands").classList.add("kh-hide");
    $("rv-evidence").classList.add("kh-hide");
    renderPassage(d.passage, null);
  }

  // BP28 #23/#24: label the buttons + the header key legend for THIS kind.
  const kind = REVIEW_KINDS[d.kind] || REVIEW_KINDS.merge;
  const keyHint = (label, key) =>
    esc(label) + ' <span style="font-weight:400;opacity:.7">(' + key + ")</span>";
  $("btn-merge").innerHTML = keyHint(kind.a, "A");
  $("btn-separate").classList.toggle("kh-hide", !kind.r);
  if (kind.r) $("btn-separate").innerHTML = keyHint(kind.r, "R");
  $("rv-keys").textContent = kind.keys;
  $("decision-box").classList.add("kh-hide");
}

function fillList(el, items, emptyText) {
  el.textContent = "";
  (items && items.length ? items : [emptyText]).forEach((text) => {
    const div = document.createElement("div");
    div.textContent = text;
    el.appendChild(div);
  });
}

function renderPassage(passage, highlight) {
  if (!passage) {
    $("rv-passage").classList.add("kh-hide");
    return;
  }
  $("rv-passage").classList.remove("kh-hide");
  const el = $("passage-text");
  el.textContent = "";
  const text = passage.text || "";
  const mark = highlight || passage.highlight;
  const at = mark ? text.indexOf(mark) : -1;
  if (at < 0) {
    el.textContent = "“" + text + "”";
  } else {
    el.append("“" + text.slice(0, at));
    const span = document.createElement("span");
    span.setAttribute("style", "background:rgba(159,192,255,.22);padding:1px 5px;border-radius:4px;box-shadow:0 0 0 1px rgba(159,192,255,.45);font-style:normal");
    span.textContent = mark;
    el.appendChild(span);
    el.append(text.slice(at + mark.length) + "”");
  }
  $("passage-src").textContent =
    (passage.document_title || "document " + passage.document_id) +
    " · chunk " + passage.chunk_id + " (seq " + passage.chunk_seq + ") · the immutable original is one hop away";
}

/* --------------------------------------------------------------- decide() */
async function decide(kind) {
  const d = state.current;
  if (!d && kind !== "split") return;
  let action = null, params = null, copy = null, clears = false;

  if (d && d.kind === "merge") {
    const cid = parseInt(d.id.split(":")[1], 10);
    if (kind === "merge") {
      action = "resolve_merge"; params = { candidate_id: cid, same: true }; clears = true;
    } else if (kind === "separate") {
      action = "resolve_merge"; params = { candidate_id: cid, same: false }; clears = true;
    }
  } else if (d && d.kind === "quarantine") {
    const qid = parseInt(d.id.split(":")[1], 10);
    if (kind === "merge") { action = "triage_quarantine"; params = { quarantine_id: qid, decision: "resolved" }; clears = true; }
    else if (kind === "separate") { action = "triage_quarantine"; params = { quarantine_id: qid, decision: "dismissed" }; clears = true; }
  } else if (d && d.kind === "flagged") {
    const did = parseInt(d.id.split(":")[1], 10);
    if (kind === "merge") { action = "resolve_flagged_document"; params = { document_id: did }; clears = true; }
  }

  if (kind === "split") {
    if (!state.lastMergeId) {
      showDecision("Nothing to split yet — S reverses the last merge you made this session.");
      return;
    }
    action = "split_merge"; params = { merge_id: state.lastMergeId }; clears = false;
  }
  if (kind === "skipped") {
    // Client-side requeue: no write travels, the counter does not move.
    if (state.queue.length > 1) state.queue.push(state.queue.shift());
    showDecision("Skipped. The item goes back into the queue at the same confidence.");
    renderQueueList();
    if (state.queue.length) await selectItem(state.queue[0].id);
    return;
  }
  if (!action) return;

  let resp;
  try {
    resp = await api("/v1/actions/" + action, { method: "POST", body: JSON.stringify(params) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status !== 200) {
    showDecision("Not recorded — " + (body.detail || body.error || resp.status) + ".");
    return;
  }

  if (action === "resolve_merge" && params.same) {
    state.lastMergeId = body.result.merge_id;
    const survivor = d.candidate_a ? d.candidate_a.name : "the surviving record";
    showDecision("Merged into " + survivor + ". The change is reversible — the snapshot is " + (body.snapshot_ref || "recorded") + ". Press S to undo.");
  } else if (action === "resolve_merge") {
    showDecision("Kept separate. Both records stand; the pair is now a labeled hard negative for the flywheel.");
  } else if (action === "split_merge") {
    showDecision("Split. Both records are restored and the dependent facts re-resolved. (" + (body.target || "") + ")");
    state.lastMergeId = null;
  } else if (action === "triage_quarantine") {
    showDecision(params.decision === "resolved"
      ? "Resolved. The verdict is recorded as extraction feedback (a correction label)."
      : "Dismissed. The item leaves the queue; the decision is on the audit trail.");
  } else if (action === "resolve_flagged_document") {
    showDecision("Resolved. The document re-queues for processing with the adjudicated tag.");
  }
  if (clears) {
    state.cleared += 1;
    $("cleared").textContent = state.cleared;
    $("cleared-bar").style.inset = "0 " + Math.max(0, 100 - state.cleared * 4) + "% 0 0";
  }
  await refreshReviews(false);   // advance to the next item, refresh counts
}

function showDecision(text) {
  $("decision-box").classList.remove("kh-hide");
  $("decision-text").textContent = text;
}

/* ------------------------------------------------------------------- tabs */
function setTab(name) {
  state.tab = name;
  document.querySelectorAll("#tabs [data-tab]").forEach((el) => {
    const active = el.getAttribute("data-tab") === name;
    el.style.background = active
      ? "linear-gradient(180deg,rgba(120,150,255,.32),rgba(70,100,235,.18))"
      : "rgba(255,255,255,.04)";
    el.style.border = active ? "1px solid rgba(160,190,255,.6)" : "1px solid rgba(255,255,255,.12)";
  });
  $("panel-monitor").classList.toggle("kh-hide", name !== "monitor");
  $("panel-review").classList.toggle("kh-hide", name !== "review");
  $("panel-other").classList.toggle("kh-hide", name === "monitor" || name === "review");
  if (name !== "monitor" && name !== "review") $("other-title").textContent = OTHER_TITLES[name] || "";
  if (name === "review" && state.token) refreshReviews(true).catch(() => {});
}

/* ------------------------------------------------------------------- boot */
function esc(text) {
  const div = document.createElement("div");
  div.textContent = text === null || text === undefined ? "" : String(text);
  return div.innerHTML;
}

function startPolling() {
  state.timers.forEach(clearInterval);
  state.timers = [];
  const safe = (fn) => () => fn().catch(() => {});
  safe(refreshMonitor)();
  safe(refreshActivity)();
  safe(() => refreshReviews(true))();
  state.timers.push(setInterval(safe(refreshMonitor), POLL_MS));
  state.timers.push(setInterval(safe(refreshActivity), POLL_MS));
  state.timers.push(setInterval(() => {
    if (state.tab === "review") refreshReviews(true).catch(() => {});
  }, REVIEW_POLL_MS));
  state.timers.push(setInterval(tickUptime, 1000));
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("#tabs [data-tab]").forEach((el) =>
    el.addEventListener("click", () => setTab(el.getAttribute("data-tab"))));
  setTab("monitor");

  $("token-go").addEventListener("click", () => unlock($("token-input").value.trim()));
  $("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") unlock($("token-input").value.trim());
  });
  $("btn-merge").addEventListener("click", () => decide("merge"));
  $("btn-separate").addEventListener("click", () => decide("separate"));
  $("btn-split").addEventListener("click", () => decide("split"));
  $("btn-skip").addEventListener("click", () => decide("skipped"));

  // Design's keyboard flow, exactly: A merge · R keep separate · S split ·
  // space skip — only on the review tab, never while typing a credential.
  window.addEventListener("keydown", (e) => {
    if (state.tab !== "review" || !state.token) return;
    if (document.activeElement && document.activeElement.tagName === "INPUT") return;
    const k = e.key.toLowerCase();
    if (k === "a") decide("merge");
    else if (k === "r") decide("separate");
    else if (k === "s") decide("split");
    else if (e.key === " ") { e.preventDefault(); decide("skipped"); }
  });

  const saved = sessionStorage.getItem(TOKEN_KEY);
  if (saved) unlock(saved); else $("token-input").focus();
});
