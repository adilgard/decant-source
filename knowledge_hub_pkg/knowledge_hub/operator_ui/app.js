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
  inference: null,       // /v1/inference — the monitor strip's model names
  uptimeBase: null,      // {uptime_s, at}
  reviews: { counts: null, items: [] },
  queue: [],             // client-side working queue (skip rotates it)
  current: null,         // loaded review detail
  cleared: 0,
  lastMergeId: null,     // S reverses the last merge made this session
  retiredVersion: null,  // the ontology a swap just retired (re-extract prefill)
  offline: false,
  timers: [],
};

const $ = (id) => document.getElementById(id);
const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString();

/* d.s Stage 1 — tab disposition. The placeholder surface is GONE: every
 * visible tab is fully wired. Sources & access is a future build; System
 * connections collapsed into the Inference tab; Facts & entities is the
 * NEXT PASS, to be designed against the real Title 26 corpus (see
 * index.html's tab-row comment). */

/* ------------------------------------------------------------------ fetch */
async function api(path, options) {
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers["Authorization"] = "Bearer " + state.token;
  if (opts.body) opts.headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (e) {
    setOffline("unreachable");
    throw e;
  }
  // Stage 2: a 5xx is NOT "all clear". The old code flipped the footer to
  // NOMINAL on any fetch that returned — a permanently-erroring server
  // showed green over stale numbers. Erroring and unreachable are
  // different truths and get different words; only a real 2xx/4xx answer
  // clears the banner.
  setOffline(resp.status >= 500 ? "erroring" : false);
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

/* mode: false (all clear) | "unreachable" (no answer) | "erroring" (the
 * service answers, but with server errors — data on screen may be stale). */
function setOffline(mode) {
  state.offline = mode;
  $("offline").classList.toggle("kh-hide", !mode);
  if (mode) {
    $("offline-msg").textContent = mode === "erroring"
      ? "The system is answering with errors — what you see may be out of "
        + "date. The console keeps retrying quietly; if this persists, the "
        + "operator log has the details."
      : "Can’t reach the system right now — the console keeps retrying "
        + "quietly. Nothing you decided has been lost.";
  }
  $("system-state").textContent = mode === "erroring" ? "SYSTEM : ERRORING"
    : mode ? "SYSTEM : UNREACHABLE" : "SYSTEM : NOMINAL";
  $("system-state").style.color = mode ? "#ff9b83" : "#7be0c8";
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
  // Stage 2: the footer names the address the page is actually served
  // from — this page IS the operator service, so location is the truth.
  $("footer-operator").textContent =
    "operator " + location.host + " · act is audited";
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

  // Tile 2 — documents landed. (Stage 3: its gauge showed a different
  // metric than its number — the sub-line carries that count in words.)
  $("tile-landed").textContent = fmt(m.landed);
  const nSources = m.sources.length;
  const done = m.sources.filter((s) => s.backfill_done).length;
  $("tile-landed-sub").textContent = nSources
    ? nSources + " source" + (nSources === 1 ? "" : "s") + " registered · " + done + " fully swept"
    : "no sources registered yet";

  // Tile 3 — awaiting human.
  const r = m.review;
  $("tile-review-num").textContent = fmt(r.total);
  $("tile-review-sub").textContent =
    fmt(r.merges) + " merges · " + fmt(r.quarantined) + " quarantined · " + fmt(r.flagged) + " flagged";
  $("badge-review").textContent = fmt(r.total);

  // Tile 4 — serving p95 vs the budget the SERVER declares (never a number
  // frozen into the shell).
  $("tile-p95").textContent = m.p95_ms === null ? "—" : Math.round(m.p95_ms);
  // Stage 4: say what p95 means instead of assuming the reader knows.
  $("tile-p95-budget").textContent =
    "19 of 20 reads answer within this · budget " + m.p95_budget_ms + " ms";
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
  // Stage 2: the model names come from /v1/inference (this instance's
  // configured roles), never frozen into the console — until that read
  // has answered, the footers claim nothing about models.
  const inf = state.inference;
  $("st-process-n").textContent = fmt(st.process.count);
  $("st-process-foot").textContent =
    "queue depth " + fmt(st.process.queue_depth)
    + (inf ? " · " + inf.embedding.model + " " + inf.embedding_dim + "-dim"
           : "");
  bar("st-process-bar", st.process.count);
  $("st-extract-n").textContent = fmt(st.extract.count);
  $("st-extract-sub").textContent = fmt(st.extract.facts_staged) + " facts staged";
  $("st-extract-foot").textContent = fmt(st.extract.quarantined)
    + " quarantined" + (inf ? " · " + inf.extraction.model : "");
  bar("st-extract-bar", st.extract.count);
  $("st-resolve-n").textContent = fmt(st.resolve.count);
  $("st-resolve-foot").textContent = fmt(st.resolve.held_for_review) + " held for review — not merged";
  bar("st-resolve-bar", st.resolve.count);
  $("st-facts-n").textContent = fmt(st.facts.count);
  $("st-facts-foot").textContent = fmt(st.facts.confident) + " confident · " + fmt(st.facts.low_confidence) + " low-confidence";
  bar("st-facts-bar", st.facts.count);
  // Stage 3: the summary sentence that followed repeated the stage
  // footers word for word — one home per number, the footers keep it.

  renderSources(m.sources);
  renderSourcePicker(m.sources);
  renderBars(m.throughput);
  // F5: the badge counts the operator_alerts view (unacknowledged failed
  // queue items + degraded sources) — the old status='error' count was
  // structurally 0 while documents failed.
  $("badge-health").textContent = fmt(m.alerts_open);
  // Stage 2: the footer's posture line is sourced from /v1/health, not a
  // static claim. Rendered VERBATIM — the server phrases it, because the
  // browser keeps no posture logic of its own (posture-login contract).
  if (h && h.posture_line) $("footer-posture").textContent = h.posture_line;
}

function sourceRow(s) {
  const row = document.createElement("div");
  const paused = s.status === "disabled";
  const degraded = s.status === "degraded";
  const done = s.backfill_done;
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
  // Stage 2: the note carries the REAL last-run time (served since BP20,
  // never rendered until now) instead of leaving "idle" unanchored.
  const lastRun = s.last_run_at
    ? "last run " + s.last_run_at.slice(0, 16).replace("T", " ")
    : "never run yet";
  const note = s.status_reason
    ? s.status_reason
    : (done ? "idle between sweeps · " + lastRun
            : "first full sweep in progress · " + lastRun);
  const btnLabel = paused ? "Resume" : "Pause";
  // Stage 2: no fabricated percentage. Without a corpus total (adapters
  // don't report one yet) a determinate bar is unknowable — the old bar
  // sat at a hardwired 60% for every in-progress source. Now: paused =
  // empty, done = full, in progress = an animated working stripe that
  // claims activity, not extent.
  const stripeRGB = degraded ? "255,140,110" : "159,192,255";
  const barFill = paused
    ? ""
    : done
      ? '<div style="position:absolute;inset:0;border-radius:6px;' + barStyle + '"></div>'
      : '<div style="position:absolute;inset:0;border-radius:6px;opacity:.55;background-image:repeating-linear-gradient(90deg,rgba(' + stripeRGB + ',.7) 0 8px,rgba(' + stripeRGB + ',.15) 8px 16px);background-size:32px 100%;animation:khSweep 1.2s linear infinite"></div>';
  row.innerHTML =
    '<div>' +
    '<div style="display:flex;align-items:center;gap:10px;margin-bottom:7px">' + dot +
    '<div style="font-size:12.5px;color:#dbe7ff;font-weight:500">' + esc(s.source_system) + " · " + esc(s.source_ref) + "</div>" + tag +
    '<div style="flex:1"></div>' +
    '<div style="font-size:12px;color:#c9d8f8;font-family:Consolas,\'Lucida Console\',monospace">' + fmt(s.landed) + (s.total ? " / " + fmt(s.total) : "") + " landed</div></div>" +
    '<div style="position:relative;height:12px;border-radius:6px;overflow:hidden;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1)">' +
    barFill + "</div>" +
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
    // Stage 4: an empty state says what to DO, not just what is absent.
    list.innerHTML = '<div style="font-size:11px;color:#5c6f9e">no sources yet — land a folder on the Data landing tab and it appears here with live progress</div>';
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
    feed.innerHTML = '<div style="color:#5c6f9e;padding:5px 8px">quiet — nothing has moved yet. Start an ingest on the Data landing tab and this feed narrates every step.</div>';
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

/* Stage 4: plain language where a human reads, the raw code kept in
 * parens where a human might need to quote it (to khctl, to a teammate,
 * to a bug report). Unknown codes fall through raw — the glossary must
 * never hide a new failure class. */
const QUARANTINE_PLAIN = {
  unbound_predicate: "uses a relationship the ontology does not allow",
  unbound_entity_type: "names an entity type the ontology does not allow",
  validation_failure: "came back in a shape that could not be checked",
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
    $("rv-blurb").textContent = "The resolver stopped here on purpose: the score landed in the undecided middle — too high to keep separate automatically, too low to merge automatically (the gray band) — so a human makes the call.";
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
      ? "Quarantined extraction — " + (d.detail || QUARANTINE_PLAIN[d.reason] || d.reason)
      : "Flagged document — " + (d.title || "");
    $("rv-blurb").textContent = d.kind === "quarantine"
      ? "This fact was held out of the corpus: it "
        + (QUARANTINE_PLAIN[d.reason] || d.reason)
        + ". Nothing the vocabulary does not permit is ever stored. "
        + "Resolve records the correction as extraction feedback; Dismiss "
        + "just clears it. (reason code: " + d.reason + ")"
      : "The document arrived labeled one way, but its content looks like "
        + "another — a wrong label would send it down the wrong pipeline. "
        + "Resolving records the corrected label and re-queues it. "
        + "(capture said: " + (d.review_reason || "no detail") + ")";
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
    showDecision("Kept separate. Both records stand, and the pair is remembered as a confirmed 'not the same' example that sharpens future matching.");
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
    // Stage 3: the count alone — the bar under it filled 4% per item,
    // progress toward an invented 25-item denominator.
    state.cleared += 1;
    $("cleared").textContent = state.cleared;
  }
  await refreshReviews(false);   // advance to the next item, refresh counts
}

function showDecision(text) {
  $("decision-box").classList.remove("kh-hide");
  $("decision-text").textContent = text;
}

/* --------------------------------------------------------------- ontology */
// d.s Stage 1. Two rules the UI must keep saying: importing is INERT
// (nothing extracts under a version until it is selected), and selecting
// applies to FUTURE ingests only (facts keep the version that produced
// them). Both actions travel the audited write channel.
function ontoSay(ok, text) {
  $("onto-msg-box").classList.toggle("kh-hide", !ok);
  $("onto-err-box").classList.toggle("kh-hide", ok);
  $(ok ? "onto-msg" : "onto-err").textContent = text;
}

async function refreshOntology() {
  const resp = await api("/v1/ontology");
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) return;
  const body = await resp.json();
  $("onto-active").textContent = body.active || "none selected";
  const list = $("onto-list");
  list.textContent = "";
  if (!body.versions.length) {
    list.innerHTML = '<div style="font-size:11px;color:#5c6f9e">no ontology versions loaded yet — import one with the box on the right, then select it to make it count</div>';
    return;
  }
  body.versions.forEach((v) => list.appendChild(ontologyRow(v)));
  populateReextractScope(body.versions, body.active);
}

function ontologyRow(v) {
  const row = document.createElement("div");
  const tag = v.active
    ? '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:#7be0c8;padding:2px 8px;border-radius:8px;border:1px solid rgba(123,224,200,.35);background:rgba(123,224,200,.08)">ACTIVE</div>'
    : '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:#8fa8d8;padding:2px 8px;border-radius:8px;border:1px solid rgba(255,255,255,.25);background:rgba(255,255,255,.06)">LOADED</div>';
  const when = v.effective_from ? v.effective_from.slice(0, 10) : "—";
  row.setAttribute("style", v.active
    ? "padding:11px 13px;border-radius:12px;border:1px solid rgba(123,224,200,.45);background:rgba(123,224,200,.06)"
    : "padding:11px 13px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.03)");
  row.innerHTML =
    '<div style="display:flex;align-items:center;gap:10px">' +
    '<div style="font-size:13px;color:#e9f0ff;font-weight:600;font-family:Consolas,\'Lucida Console\',monospace">' + esc(v.version) + "</div>" + tag +
    '<div style="flex:1"></div>' +
    '<div style="font-size:11px;color:#8fa8d8;font-family:Consolas,monospace">' + fmt(v.entity_types) + " types · " + fmt(v.predicates) + " predicates · loaded " + esc(when) + "</div>" +
    (v.active ? "" :
      '<div data-select="' + esc(v.version) + '" style="cursor:pointer;font-size:11px;color:#dbe7ff;padding:4px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.22);background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.04))">Select as active</div>') +
    "</div>" +
    (v.notes ? '<div style="font-size:11px;color:#5c6f9e;margin-top:6px">' + esc(v.notes) + "</div>" : "");
  const btn = row.querySelector("[data-select]");
  if (btn) btn.addEventListener("click", () => selectOntology(v.version));
  return row;
}

async function selectOntology(version) {
  const retiring = $("onto-active").textContent;   // the version being retired
  let resp;
  try {
    resp = await api("/v1/actions/select_ontology", {
      method: "POST", body: JSON.stringify({ version: version }) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status !== 200) {
    ontoSay(false, "Not selected — " + (body.detail || body.error || resp.status) + ".");
    return;
  }
  // d.s Stage 3: OFFER the two paths, never force. Path 1 (nothing runs)
  // is this message; path 2 is the re-extract box, prefilled with the
  // version that was just retired.
  ontoSay(true, "Active ontology is now " + version + ". This applies to future ingests only — " +
    "facts already extracted keep the version that produced them. If you want existing data " +
    "brought under " + version + ", use the re-extract box below (scope preselected to " + retiring + "); " +
    "otherwise nothing re-runs. The selection is on the audit trail.");
  state.retiredVersion = retiring && retiring !== "none selected" ? retiring : null;
  await refreshOntology();
}

/* ------------------------------------------------- re-extract (Stage 3) */
function rxSay(ok, text) {
  $("rx-msg-box").classList.toggle("kh-hide", !ok);
  $("rx-err-box").classList.toggle("kh-hide", ok);
  $(ok ? "rx-msg" : "rx-err").textContent = text;
}

function rxScopeParams() {
  const params = {};
  if ($("rx-all").checked) params.all_documents = true;
  else if ($("rx-scope").value) params.scope_version = $("rx-scope").value;
  if ($("rx-source").value.trim()) params.source_ref = $("rx-source").value.trim();
  return params;
}

function populateReextractScope(versions, active) {
  const sel = $("rx-scope");
  const current = sel.value || state.retiredVersion || "";
  sel.textContent = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "— pick the version to re-extract from —";
  sel.appendChild(none);
  versions.forEach((v) => {
    if (v.version === active) return;   // scope == target is a no-op, refused server-side
    const opt = document.createElement("option");
    opt.value = v.version;
    opt.textContent = v.version;
    sel.appendChild(opt);
  });
  sel.value = current;
  refreshReextractPreview().catch(() => {});
}

async function refreshReextractPreview() {
  const p = rxScopeParams();
  if (!p.all_documents && !p.scope_version) {
    $("rx-count").textContent = "pick a scope to see the affected count";
    return;
  }
  const qs = new URLSearchParams();
  if (p.scope_version) qs.set("scope_version", p.scope_version);
  if (p.source_ref) qs.set("source_ref", p.source_ref);
  const resp = await api("/v1/reextract-preview?" + qs.toString());
  if (resp.status !== 200) return;
  const n = (await resp.json()).affected_documents;
  $("rx-count").textContent = fmt(n) + " document(s) affected — a background job re-extracts them";
}

async function confirmReextract() {
  const p = rxScopeParams();
  if (!p.all_documents && !p.scope_version) {
    rxSay(false, "Pick a scope first — the version to re-extract from, or tick all documents. A blanket run never happens by default.");
    return;
  }
  let resp;
  try {
    resp = await api("/v1/actions/reextract_scope", {
      method: "POST", body: JSON.stringify(p) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status === 403) { rxSay(false, "Operator role required."); return; }
  if (resp.status !== 200) {
    rxSay(false, "Not started — " + (body.detail || body.error || resp.status) + ".");
    return;
  }
  const r = body.result;
  rxSay(true, "Job " + r.job_id + " queued: " + fmt(r.affected_documents) + " document(s) re-extract under " +
    r.ontology_version + ". Old facts are retained and marked superseded as new ones promote. " +
    "Progress lives on the Data landing tab; the job is resumable and safe to re-run.");
}

async function importOntologyFile(file) {
  let parsed;
  try {
    parsed = JSON.parse(await file.text());
  } catch (e) {
    ontoSay(false, "Not imported — " + file.name + " is not valid JSON: " + e.message);
    return;
  }
  let resp;
  try {
    resp = await api("/v1/actions/import_ontology", {
      method: "POST", body: JSON.stringify({ ontology: parsed }) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status !== 200) {
    ontoSay(false, "Not imported — " + (body.detail || body.error || resp.status) + ".");
    return;
  }
  const r = body.result;
  ontoSay(true, (r.status === "already_imported"
    ? "Version " + r.version + " was already loaded with identical content — nothing changed."
    : "Imported " + r.version + " (" + r.entity_types + " entity types, " + r.predicates +
      " predicates). It is loaded but NOT active — select it to apply to future ingests."));
  await refreshOntology();
}

/* ------------------------------------------- folder path check + browse */
/* d.s Stage 6. The typed path is validated LIVE by the server — the same
 * classifier ingest_folder refuses with, so the green light and the job
 * gate can never disagree. Browse opens a NATIVE OS dialog through the
 * operator service (console + files are colocated); the button exists
 * only where the server's probe says a dialog can render. */
let pathCheckTimer = null;
let pathCheckSeq = 0;

function schedulePathCheck() {
  clearTimeout(pathCheckTimer);
  pathCheckTimer = setTimeout(() => { validateFolderPath().catch(() => {}); },
                              400);
}

async function validateFolderPath() {
  const input = $("ld-path");
  const status = $("ld-path-status");
  const raw = input.value.trim();
  const seq = ++pathCheckSeq;
  if (!raw) {
    input.style.border = "1px solid rgba(160,190,255,.4)";
    status.style.color = "#5c6f9e";
    status.textContent = "checked on this machine as you type — green "
      + "means the ingest will accept it";
    return;
  }
  const resp = await api("/v1/validate-folder?path=" + encodeURIComponent(raw));
  if (resp.status !== 200) return;
  const v = await resp.json();
  if (seq !== pathCheckSeq) return;   // a newer keystroke owns the field
  input.style.border = v.ok ? "1px solid rgba(123,224,200,.6)"
                            : "1px solid rgba(255,150,130,.6)";
  status.style.color = v.ok ? "#7be0c8" : "#ffcabb";
  status.textContent = (v.ok ? "✓ " : "✗ ") + v.detail;
}

async function probeBrowse() {
  try {
    const resp = await api("/v1/pick-folder?probe=1");
    if (resp.status !== 200) return;
    const p = await resp.json();
    $("ld-browse").classList.toggle("kh-hide", !p.available);
  } catch (e) { /* offline banner already up */ }
}

async function browseFolder() {
  const btn = $("ld-browse");
  const status = $("ld-path-status");
  btn.style.opacity = "0.5";
  status.style.color = "#8fa8d8";
  status.textContent = "a folder dialog is open on this machine — it may "
    + "be behind this window";
  try {
    const initial = $("ld-path").value.trim();
    const resp = await api("/v1/pick-folder"
      + (initial ? "?initial=" + encodeURIComponent(initial) : ""));
    if (resp.status !== 200) return;
    const body = await resp.json();
    if (body.status === "picked") {
      $("ld-path").value = body.path;
      await validateFolderPath();
    } else if (body.status === "busy") {
      status.textContent = body.reason;
    } else if (body.status === "unavailable") {
      btn.classList.add("kh-hide");   // the probe was wrong — stop offering
      status.style.color = "#ffcabb";
      status.textContent = body.reason;
    } else {
      await validateFolderPath();     // cancelled: restore the live check
    }
  } finally {
    btn.style.opacity = "1";
  }
}

/* ---------------------------------------------------------- data landing */
// d.s Stage 2: console folder ingest. The path is typed (no browser
// folder picker — it cannot return a real server path) and validated
// server-side. Job creation is the audited write; progress is polled.
function landSay(ok, text) {
  $("ld-msg-box").classList.toggle("kh-hide", !ok);
  $("ld-err-box").classList.toggle("kh-hide", ok);
  $(ok ? "ld-msg" : "ld-err").textContent = text;
}

async function populateOntologySelect() {
  const resp = await api("/v1/ontology");
  if (resp.status !== 200) return;
  const body = await resp.json();
  const sel = $("ld-ontology");
  const current = sel.value;
  sel.textContent = "";
  const dflt = document.createElement("option");
  dflt.value = "";
  dflt.textContent = "active selection (default" +
    (body.active ? ": " + body.active : "") + ")";
  sel.appendChild(dflt);
  body.versions.forEach((v) => {
    const opt = document.createElement("option");
    opt.value = v.version;
    // Stage 5: each option says what it is, not just its name.
    opt.textContent = v.version + (v.active ? " (active)" : "")
      + " · " + fmt(v.entity_types) + " types · "
      + fmt(v.predicates) + " predicates";
    sel.appendChild(opt);
  });
  sel.value = current;   // keep the operator's pick across refreshes
}

async function startIngest() {
  const params = { path: $("ld-path").value.trim(),
                   recurse: $("ld-recurse").checked };
  if ($("ld-include").value.trim()) params.include = $("ld-include").value.trim();
  if ($("ld-exclude").value.trim()) params.exclude = $("ld-exclude").value.trim();
  if ($("ld-extensions").value.trim()) params.extensions = $("ld-extensions").value.trim();
  if ($("ld-ontology").value) params.ontology_version = $("ld-ontology").value;
  // Naming the source is what lets a folder be SET UP before its first job.
  // Omitted, the server derives one from the path — fine for a folder on the
  // deployment defaults, useless for one that needs a plugin, because the
  // derived name cannot be known (and so cannot be configured) until a run
  // has already read every file the wrong way.
  if ($("ld-source").value.trim()) params.source_ref = $("ld-source").value.trim();
  if (!params.path) { landSay(false, "Type the folder's absolute path first."); return; }
  let resp;
  try {
    resp = await api("/v1/actions/ingest_folder", {
      method: "POST", body: JSON.stringify(params) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status === 403) { landSay(false, "Operator role required."); return; }
  if (resp.status !== 200) {
    landSay(false, "Not started — " + (body.detail || body.error || resp.status) + ".");
    return;
  }
  const r = body.result;
  landSay(true, "Job " + r.job_id + " queued for " + r.path + " as source " +
    r.source_ref + ", under ontology " + r.ontology_version +
    ". The runner picks it up within seconds; progress shows on the right.");
  await refreshJobs();
}

/* ---------------------------------------------------------------------------
 * Extraction setup: which registered components a source uses.
 *
 * The pickers are filled from GET /v1/components, which reports what THIS
 * build has registered — never a hardcoded list, so a deployment carrying an
 * extra plugin shows it without a console change. A plugin installed as its
 * own package is typed as 'package.module:Attribute'; nothing can enumerate
 * what is installable, so the datalist offers the known names and the field
 * still accepts free text. Validation is the server's either way: it
 * resolves and type-checks every name on save.
 *
 * Saving goes through set_extraction_setup, not edit_scope, because this
 * form knows three keys and a source's config holds more than three. The
 * server merges.
 * ------------------------------------------------------------------------ */
let knownSources = [];

async function loadComponents() {
  let resp;
  try {
    resp = await api("/v1/components");
  } catch (e) { return; }
  if (resp.status !== 200) return;      // reviewer role, or an older server
  const c = await resp.json();
  const strategy = $("xs-strategy");
  strategy.innerHTML = '<option value="">— deployment default —</option>';
  (c.extraction_strategies || []).forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name === "parser_supplied"
      ? "parser_supplied — a plugin, deterministic, no model"
      : (name === "llm" ? "llm — language model reads the prose"
                        : "structured_map — declared column mapping");
    strategy.appendChild(opt);
  });
  fillDatalist("xs-parser-list", c.parsers || []);
  fillDatalist("xs-plugin-list", c.fact_parsers || []);
  $("xs-parser").placeholder = c.default_parser || "docling";
  fillModelPicker();   // Stage 5: served models, from state.inference
}

/* Stage 5: the model picker offers ONLY what the inference box serves
 * right now (state.inference, the /v1/inference read) — never a hardcoded
 * list. A model pulled onto the box appears on the next refresh; one
 * removed disappears (and the server refuses it at save time either way).
 * Blank = the deployment default, named so picking it is informed. */
function fillModelPicker() {
  const sel = $("xs-model");
  if (!sel) return;
  const previous = sel.value;
  const inf = state.inference;
  sel.innerHTML = "";
  const dflt = document.createElement("option");
  dflt.value = "";
  dflt.textContent = "— deployment default"
    + (inf ? " (" + inf.extraction.model + ")" : "") + " —";
  sel.appendChild(dflt);
  (inf && inf.reachable ? inf.models : []).forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
  sel.value = previous;
  if (sel.value !== previous) sel.value = "";  // its model left the box
}

function fillDatalist(id, names) {
  const el = $(id);
  el.innerHTML = "";
  names.forEach((name) => {
    const opt = document.createElement("option");
    opt.value = name;
    el.appendChild(opt);
  });
}

function renderSourcePicker(sources) {
  knownSources = sources || [];
  const sel = $("xs-source");
  const previous = sel.value;
  sel.innerHTML = "";
  if (!knownSources.length) {
    sel.innerHTML = '<option value="">no sources registered yet</option>';
    return;
  }
  knownSources.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.source_ref;
    opt.textContent = s.source_system + " · " + s.source_ref;
    sel.appendChild(opt);
  });
  if (previous) sel.value = previous;   // a refresh must not move the cursor
  // The ingest form's Source box offers the same list. A datalist, not a
  // select: the name may be one that does not exist yet, and typing it is
  // how a folder gets a stable ref on its very first run.
  fillDatalist("ld-source-list", knownSources.map((s) => s.source_ref));
}

/* Register a source so it can be configured BEFORE its first ingest.
 *
 * add_source takes adapter config only, and this form deliberately sends
 * none: the components go on next, through set_extraction_setup, which
 * merges. Two steps rather than one wide form, because the second one
 * resolves and type-checks every component server-side and its errors are
 * the ones worth reading. */
async function addSource() {
  const ref = $("xs-new-ref").value.trim();
  if (!ref) { xsSay(false, "Give the source a name first."); return; }
  let resp;
  try {
    resp = await api("/v1/actions/add_source", {
      method: "POST",
      body: JSON.stringify({ source_ref: ref,
                             source_system: $("xs-new-system").value }) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status === 403) { xsSay(false, "Operator role required."); return; }
  if (resp.status !== 200) {
    xsSay(false, body.detail || body.error || ("HTTP " + resp.status));
    return;
  }
  $("xs-new-ref").value = "";
  await refreshMonitor();          // the one reader of /v1/monitor.sources
  $("xs-source").value = body.result.source_ref;   // land on what was just made
  xsSay(true, "Registered " + body.result.source_ref +
        " — set its components below, then name it on the ingest form.");
}

function xsSay(ok, text) {
  $("xs-say").style.color = ok ? "#7be0c8" : "#ff9b83";
  $("xs-say").textContent = text;
}

async function saveExtractionSetup() {
  const ref = $("xs-source").value;
  if (!ref) { xsSay(false, "Register a source first."); return; }
  const params = {
    source_ref: ref,
    extraction_strategy: $("xs-strategy").value,
    parser: $("xs-parser").value.trim(),
    fact_parser: $("xs-plugin").value.trim(),
    extraction_model: $("xs-model").value,
  };
  let resp;
  try {
    resp = await api("/v1/actions/set_extraction_setup", {
      method: "POST", body: JSON.stringify(params) });
  } catch (e) { return; }
  const body = await resp.json();
  if (resp.status === 403) { xsSay(false, "Operator role required."); return; }
  if (resp.status !== 200) {
    // The server's message is the useful one here — it names the component
    // that would not resolve, which is nearly always the actual mistake.
    xsSay(false, body.detail || body.error || ("HTTP " + resp.status));
    return;
  }
  const changed = Object.keys(body.result.changed || {});
  xsSay(true, changed.length
    ? "Saved — " + changed.join(", ") + ". Applies to the next document "
      + "this source ingests; already-extracted documents keep their "
      + "provenance until re-extracted."
    : "Nothing to change.");
}

function jobRow(j) {
  const colors = {
    queued:  ["#8fa8d8", "rgba(255,255,255,.25)", "rgba(255,255,255,.06)"],
    running: ["#eadf9a", "rgba(230,220,150,.4)", "rgba(230,215,140,.08)"],
    done:    ["#7be0c8", "rgba(123,224,200,.35)", "rgba(123,224,200,.08)"],
    failed:  ["#ffcabb", "rgba(255,150,130,.4)", "rgba(255,110,90,.1)"],
  }[j.status] || ["#8fa8d8", "rgba(255,255,255,.25)", "rgba(255,255,255,.06)"];
  const c = j.counts || {};
  const line = j.status === "failed"
    ? (j.error || "failed").split("\n")[0]
    : ["landed " + fmt(c.files_landed), "duplicates " + fmt(c.files_replayed),
       "unknown skipped " + fmt(c.skipped_unknown),
       "processed " + fmt(c.docs_processed), "extracted " + fmt(c.docs_extracted),
       "facts " + fmt(c.facts_promoted)].join(" · ");
  const row = document.createElement("div");
  row.setAttribute("style",
    "padding:11px 13px;border-radius:12px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.03)");
  row.innerHTML =
    '<div style="display:flex;align-items:center;gap:10px">' +
    '<div style="font-family:Consolas,monospace;font-size:12px;color:#e9f0ff;font-weight:600">job ' + j.id + "</div>" +
    '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:' + colors[0] +
      ';padding:2px 8px;border-radius:8px;border:1px solid ' + colors[1] + ";background:" + colors[2] + '">' +
      esc(j.status.toUpperCase()) + "</div>" +
    '<div style="flex:1"></div>' +
    '<div style="font-size:10.5px;color:#5c6f9e;font-family:Consolas,monospace">' + esc((j.created_at || "").slice(0, 19).replace("T", " ")) + "</div></div>" +
    '<div style="font-size:11px;color:#b9cdf5;font-family:Consolas,monospace;margin-top:6px;word-break:break-all">' + esc((j.params || {}).path || "") + "</div>" +
    '<div style="font-size:11px;color:' + (j.status === "failed" ? "#ffcabb" : "#8fa8d8") + ';margin-top:5px">' + esc(line) +
      (c.ontology_version ? ' <span style="color:#5c6f9e">· ontology ' + esc(c.ontology_version) + "</span>" : "") + "</div>" +
    // Only where there is something to stop. A finished job gets no button,
    // because the honest answer for one is "undoing this is a different
    // action" rather than a control that looks like it would.
    (j.status === "queued" || j.status === "running"
      ? '<div data-kill="' + j.id + '" style="cursor:pointer;display:inline-block;margin-top:9px;font-size:11px;color:#ffd9cd;padding:5px 14px;border-radius:12px;border:1px solid rgba(255,150,130,.45);background:rgba(255,110,90,.1)">Kill job</div>' +
        '<span data-kill-say="' + j.id + '" style="font-size:11px;color:#8fa8d8;margin-left:10px"></span>'
      : "");
  const kill = row.querySelector("[data-kill]");
  if (kill) kill.addEventListener("click", () => killJob(j, kill));
  return row;
}

/* Kill a job, behind a confirm.
 *
 * The confirm is not ceremony: an ingest is minutes-to-hours of work and the
 * button sits in a list where the row under the cursor changes as the poll
 * refreshes. It names the job and what survives, because "are you sure?" with
 * no object is a question nobody can answer. */
async function killJob(j, btn) {
  const c = j.counts || {};
  const done = fmt(c.docs_processed) + " document(s) processed, " +
    fmt(c.facts_promoted) + " fact(s) promoted";
  const ok = window.confirm(
    "Kill job " + j.id + "?\n\n" +
    ((j.params || {}).path || "") + "\n\n" +
    (j.status === "running"
      ? "It stops at the next drain-pass boundary — seconds, not instantly.\n" +
        "KEPT: " + done + ".\n" +
        "Anything still queued stays queued for a later job.\n\n" +
        "This stops the run. It does not undo it."
      : "It has not started, so nothing has been ingested."));
  if (!ok) return;
  const say = document.querySelector('[data-kill-say="' + j.id + '"]');
  const tell = (t, bad) => {
    if (say) { say.textContent = t; say.style.color = bad ? "#ff9b83" : "#eadf9a"; }
  };
  btn.style.opacity = "0.5";
  let resp;
  try {
    resp = await api("/v1/actions/cancel_job", {
      method: "POST", body: JSON.stringify({ job_id: j.id }) });
  } catch (e) { btn.style.opacity = "1"; return; }
  const body = await resp.json();
  if (resp.status !== 200) {
    btn.style.opacity = "1";
    // A 404 here is a real race, not a typo: the list is polled, so a job can
    // finish between the refresh that drew this row and the click on it. The
    // raw 'not_found' reads like a bug in the console.
    tell(resp.status === 404
      ? "job " + j.id + " is no longer there — it finished between the last "
        + "refresh and this click"
      : (body.detail || body.error || ("HTTP " + resp.status)), true);
    return;
  }
  tell("stopping — " + body.result.stopped);
  await refreshJobs();
}

async function refreshJobs() {
  const resp = await api("/v1/jobs");
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) return;
  const body = await resp.json();
  const list = $("ld-jobs");
  list.textContent = "";
  if (!body.jobs.length) {
    list.innerHTML = '<div style="font-size:11px;color:#5c6f9e">no jobs yet — type a folder path on the left and Start; progress shows here and survives a page reload</div>';
    return;
  }
  body.jobs.forEach((j) => list.appendChild(jobRow(j)));
}

/* -------------------------------------------------- errors & health (S1) */
/* An alert is real state, not a log line: a dispatch/extraction queue item
 * carrying an error that nobody retried or acknowledged, or a degraded
 * source (the operator_alerts view). Retry re-queues the item (the ack is
 * cleared server-side); Acknowledge marks it seen. A source alert has no
 * retry — the remedy is fixing the cause, then Resume on the monitor. */
const ALERT_KINDS = {
  dispatch: {
    label: "PROCESSING FAILURE", retry: true,
    what: "this document failed while being read, chunked or embedded",
  },
  extraction: {
    label: "EXTRACTION FAILURE", retry: true,
    what: "this document failed while facts were being extracted",
  },
  source: {
    label: "SOURCE DEGRADED", retry: false,
    what: "this source's runs are failing — fix the cause, then Resume it "
      + "on the Ingestion monitor",
  },
};

async function refreshAlerts() {
  const resp = await api("/v1/alerts");
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) return;
  const alerts = (await resp.json()).alerts;
  $("al-loading").classList.add("kh-hide");
  $("al-empty").classList.toggle("kh-hide", alerts.length > 0);
  $("al-count").textContent = alerts.length
    ? alerts.length + " open alert" + (alerts.length === 1 ? "" : "s")
    : "0 open";
  const list = $("al-list");
  list.textContent = "";
  alerts.forEach((a) => list.appendChild(alertRow(a)));
}

function alertRow(a) {
  const kind = ALERT_KINDS[a.kind] ||
    { label: String(a.kind || "").toUpperCase(), retry: false, what: "" };
  const when = (a.created_at || "").slice(0, 19).replace("T", " ");
  const row = document.createElement("div");
  row.setAttribute("style",
    "padding:12px 14px;clip-path:polygon(10px 0,100% 0,100% calc(100% - 10px),calc(100% - 10px) 100%,0 100%,0 10px);border:1px solid rgba(255,150,130,.3);background:rgba(255,110,90,.05)");
  row.innerHTML =
    '<div style="display:flex;align-items:center;gap:10px">' +
    '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:#ffcabb;padding:2px 8px;border-radius:8px;border:1px solid rgba(255,150,130,.4);background:rgba(255,110,90,.1)">' + esc(kind.label) + "</div>" +
    '<div style="font-size:11px;color:#8fa8d8">' + esc(kind.what) + "</div>" +
    '<div style="flex:1"></div>' +
    '<div style="font-size:10.5px;color:#5c6f9e;font-family:Consolas,monospace">' + esc(when) + "</div></div>" +
    '<div style="font-size:11.5px;color:#ffd9cd;font-family:Consolas,\'Lucida Console\',monospace;margin-top:7px;word-break:break-all">' + esc(a.detail || "(no error text recorded)") + "</div>" +
    '<div style="display:flex;align-items:center;gap:10px;margin-top:9px">' +
    (kind.retry
      ? '<div data-al-retry style="cursor:pointer;font-size:11px;color:#dbe7ff;padding:4px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.22);background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.04))">Retry</div>' +
        '<div data-al-ack style="cursor:pointer;font-size:11px;color:#8fa8d8;padding:4px 14px;border-radius:12px;border:1px solid rgba(255,255,255,.12);background:rgba(255,255,255,.03)">Acknowledge — seen, don’t retry</div>'
      : "") +
    '<span data-al-say style="font-size:11px;color:#8fa8d8"></span></div>';
  const say = row.querySelector("[data-al-say]");
  const act = async (action, params, btn) => {
    btn.style.opacity = "0.5";
    let resp;
    try {
      resp = await api("/v1/actions/" + action, {
        method: "POST", body: JSON.stringify(params) });
    } catch (e) { btn.style.opacity = "1"; return; }
    const body = await resp.json().catch(() => ({}));
    if (resp.status === 403) { btn.style.opacity = "1"; say.textContent = "operator role required"; return; }
    if (resp.status !== 200) {
      btn.style.opacity = "1";
      // 404/409 here is usually a race with the pipeline (it finished or
      // someone else acted) — the refresh below shows the current truth.
      say.textContent = body.detail || body.error || ("HTTP " + resp.status);
      await refreshAlerts();
      return;
    }
    say.style.color = "#7be0c8";
    say.textContent = action === "retry_failed_item"
      ? "back in line — it will be picked up again shortly"
      : "acknowledged";
    await refreshAlerts();
    await refreshMonitor();   // the tab badge counts the same view
  };
  const retryBtn = row.querySelector("[data-al-retry]");
  if (retryBtn) retryBtn.addEventListener("click", () =>
    act("retry_failed_item", { queue: a.kind, item_id: a.ref_id }, retryBtn));
  const ackBtn = row.querySelector("[data-al-ack]");
  if (ackBtn) ackBtn.addEventListener("click", () =>
    act("acknowledge_alert", { kind: a.kind, item_id: a.ref_id }, ackBtn));
  return row;
}

/* ------------------------------------------------------- inference (S1) */
/* The THIN honest tab: target, reachability, and what the box serves —
 * all from GET /v1/inference, which asks the box itself (never a
 * hardcoded list). No GPU telemetry by design. */
async function refreshInference() {
  const resp = await api("/v1/inference");
  if (resp.status === 403) { lock(NO_ROLE_MSG); return; }
  if (resp.status !== 200) return;
  const inf = await resp.json();
  state.inference = inf;    // the monitor strip's model names read this
  // Stage 5: the shared reassurance line (footer) + the model picker both
  // follow the same live read.
  $("footer-inference").textContent = "inference : " + inf.target
    + (inf.reachable ? " · answering" : " · not answering");
  $("footer-inference").style.color = inf.reachable ? "#5c6f9e" : "#ffcabb";
  fillModelPicker();
  $("inf-checked").textContent =
    "checked " + new Date().toTimeString().slice(0, 8);
  $("inf-target").textContent = inf.target;
  $("inf-dot").style.background = inf.reachable ? "#7be0c8" : "#ff9b83";
  $("inf-dot").style.boxShadow = inf.reachable
    ? "0 0 8px rgba(110,230,200,.9)" : "0 0 8px rgba(255,140,110,.8)";
  $("inf-status").textContent = inf.reachable
    ? "answering" + (inf.server_version ? " · ollama " + inf.server_version : "")
      + " · serving " + inf.models.length + " model"
      + (inf.models.length === 1 ? "" : "s")
    : "not answering — check that the machine is on and its model server "
      + "(Ollama) is running. Ingestion needing a model waits; nothing is "
      + "lost. (" + (inf.error || "no detail") + ")";
  const roles = $("inf-roles");
  roles.textContent = "";
  [{ title: "Embedding", r: inf.embedding,
     what: "turns text into searchable vectors — every document needs it" },
   { title: "Extraction", r: inf.extraction,
     what: "reads prose and proposes facts — prose sources need it; "
       + "plugin (parser-supplied) sources don't" },
  ].forEach((entry) => {
    const ok = entry.r.served;
    const div = document.createElement("div");
    div.innerHTML =
      '<div style="display:flex;align-items:center;gap:10px">' +
      '<div style="width:9px;height:9px;border-radius:50%;background:' + (ok ? "#7be0c8" : "#ff9b83") + '"></div>' +
      '<div style="font-size:12.5px;color:#dbe7ff;font-weight:500">' + entry.title + "</div>" +
      '<div style="font-family:Consolas,monospace;font-size:12px;color:#c9d8f8">' + esc(entry.r.model) + "</div>" +
      '<div style="flex:1"></div>' +
      '<div style="font-family:Silkscreen,Consolas,monospace;font-size:7px;letter-spacing:.1em;color:' + (ok ? "#7be0c8" : "#ffcabb") + '">' + (ok ? "SERVED" : (inf.reachable ? "NOT SERVED" : "UNKNOWN — BOX UNREACHABLE")) + "</div></div>" +
      '<div style="font-size:10.5px;color:#5c6f9e;margin:3px 0 0 19px">' + entry.what + "</div>";
    roles.appendChild(div);
  });
  const models = $("inf-models");
  models.textContent = "";
  if (!inf.reachable) {
    models.innerHTML = '<div style="font-size:11px;color:#5c6f9e">unknown until the box answers</div>';
  } else if (!inf.models.length) {
    models.innerHTML = '<div style="font-size:11px;color:#5c6f9e">the box is answering but serving no models yet — pull one onto it and it appears here</div>';
  } else {
    inf.models.forEach((m) => {
      const chip = document.createElement("div");
      chip.setAttribute("style",
        "font-family:Consolas,monospace;font-size:11.5px;color:#b9cdf5;padding:5px 12px;border-radius:12px;border:1px solid rgba(160,190,255,.3);background:rgba(120,150,255,.08)");
      chip.textContent = m;
      models.appendChild(chip);
    });
  }
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
  $("panel-ontology").classList.toggle("kh-hide", name !== "ontology");
  $("panel-landing").classList.toggle("kh-hide", name !== "landing");
  $("panel-health").classList.toggle("kh-hide", name !== "health");
  $("panel-inference").classList.toggle("kh-hide", name !== "inference");
  if (name === "review" && state.token) refreshReviews(true).catch(() => {});
  if (name === "ontology" && state.token) refreshOntology().catch(() => {});
  if (name === "landing" && state.token) {
    refreshJobs().catch(() => {});
    populateOntologySelect().catch(() => {});
    loadComponents().catch(() => {});
    probeBrowse();   // Stage 6: offer Browse only where a dialog renders
  }
  if (name === "health" && state.token) refreshAlerts().catch(() => {});
  if (name === "inference" && state.token) refreshInference().catch(() => {});
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
  // Once at unlock: the monitor strip names this instance's models from
  // /v1/inference (Stage 2 — they were frozen into the shell before).
  safe(refreshInference)();
  state.timers.push(setInterval(safe(refreshMonitor), POLL_MS));
  state.timers.push(setInterval(safe(refreshActivity), POLL_MS));
  state.timers.push(setInterval(() => {
    if (state.tab === "review") refreshReviews(true).catch(() => {});
  }, REVIEW_POLL_MS));
  state.timers.push(setInterval(() => {
    if (state.tab === "landing") refreshJobs().catch(() => {});
  }, POLL_MS));
  state.timers.push(setInterval(() => {
    if (state.tab === "health") refreshAlerts().catch(() => {});
  }, POLL_MS));
  state.timers.push(setInterval(() => {
    // The server caches the probe ~5s; 15s here keeps the tab live
    // without turning the console into a load test for the box.
    if (state.tab === "inference") refreshInference().catch(() => {});
  }, REVIEW_POLL_MS));
  state.timers.push(setInterval(tickUptime, 1000));
}

/* Stage 3: disclosure groups — advanced fields stay one click away, and
 * the arrow says which state the group is in. */
function wireDisclosure(toggleId, bodyId) {
  $(toggleId).addEventListener("click", () => {
    const hidden = $(bodyId).classList.toggle("kh-hide");
    const arrow = $(toggleId).querySelector("[data-arrow]");
    if (arrow) arrow.textContent = hidden ? "▸" : "▾";
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("#tabs [data-tab]").forEach((el) =>
    el.addEventListener("click", () => setTab(el.getAttribute("data-tab"))));
  setTab("monitor");
  wireDisclosure("ld-more-toggle", "ld-more");
  wireDisclosure("xs-toggle", "xs-body");

  $("token-go").addEventListener("click", () => unlock($("token-input").value.trim()));
  $("token-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") unlock($("token-input").value.trim());
  });
  $("rx-confirm").addEventListener("click", () => confirmReextract().catch(() => {}));
  ["rx-scope", "rx-all", "rx-source"].forEach((id) =>
    $(id).addEventListener("change", () => refreshReextractPreview().catch(() => {})));
  $("rx-all").addEventListener("change", () => {
    $("rx-scope").disabled = $("rx-all").checked;   // one or the other, like the server
  });

  $("xs-save").addEventListener("click", () => saveExtractionSetup().catch(() => {}));
  $("xs-new-save").addEventListener("click", () => addSource().catch(() => {}));
  $("xs-new-ref").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addSource().catch(() => {});
  });

  $("ld-start").addEventListener("click", () => startIngest().catch(() => {}));
  $("ld-path").addEventListener("keydown", (e) => {
    if (e.key === "Enter") startIngest().catch(() => {});
  });
  $("ld-path").addEventListener("input", schedulePathCheck);
  $("ld-browse").addEventListener("click", () => browseFolder().catch(() => {}));

  $("onto-import").addEventListener("click", () => $("onto-file").click());
  $("onto-file").addEventListener("change", (e) => {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";       // same file re-selected still fires change
    if (file) importOntologyFile(file).catch(() => {});
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

  bootAuth();
});

/* Boot order: a session token this tab already holds, then the local-posture
   handoff, then the lock screen (d.s Stage 3).

   The handoff endpoint only EXISTS when the server decided this process
   qualifies — local posture, bound to loopback. In a deployed console it 404s,
   this falls through, and the lock screen appears exactly as it always has. So
   there is no posture logic in the browser: the UI asks, and the server's
   answer is the whole decision. Nothing here can weaken a deployed console,
   because a deployed console never answers.

   The credential arrives in a response BODY, so it never touches the URL,
   browser history, or a request line in a log. */
async function bootAuth() {
  const saved = sessionStorage.getItem(TOKEN_KEY);
  if (saved) { unlock(saved); return; }
  try {
    const resp = await fetch("/ui/local-session");
    if (resp.ok) {
      const body = await resp.json();
      if (body && body.credential) { unlock(body.credential); return; }
    }
  } catch (e) {
    /* Offline or no such route — the lock screen is the correct fallback, and
       unlock() already owns every "can't reach the system" message. */
  }
  $("token-input").focus();
}
