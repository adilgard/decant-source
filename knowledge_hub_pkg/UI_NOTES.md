# Operator UI — the designed surfaces wired to real APIs (Build Prompt 20)

`knowledge_hub/operator_reads.py` (Part A: the operator READ layer) +
`knowledge_hub/operator_ui/` (Part B: the two day-one surfaces rebuilt in
the shipping stack). After this the on-site operator can WATCH ingestion and
RESOLVE reviews from the UI — reads via the operator reads, actions via the
BP19 write API, **no side doors**.

## Part A — the operator READ API (the side door, closed)

The UI's monitor + review data had no clean path: the mockup hardcoded it
and a naive UI would read Postgres directly — a bypass the §8.8 promise
forbids. Four read-only endpoints now live ON the operator service (:8081),
with the same identity + role + tenant enforcement as the writes:

| Endpoint | Serves |
|---|---|
| `GET /v1/monitor` | pipeline snapshot: landed, facts (confident/low), per-stage counts, per-source progress, review counts, 28-min docs/min series, serving p95 (proxied server-side so the UI stays single-origin), uptime |
| `GET /v1/monitor/activity` | recent pipeline events, newest first, in the mock's `stage · detail` voice — sourced from outbox transitions, extraction_runs, resolution_decisions, operator_audit; never synthesized |
| `GET /v1/reviews` | queue listing: merges (least-confident first) + quarantined + flagged, with counts |
| `GET /v1/reviews/<kind:id>` | the candidate-pair evidence panel: both candidates, evidence for/against derived ONLY from the resolver's recorded features, band thresholds from resolution_policy, the source passage + provenance — plus the exact BP19 action params the UI should POST |

These are operator-shaped, not envelope-shaped — which is why they live
here and not on the serving choke point. Structurally read-only (SELECTs
only), never audited (audit is the write trail), absence for anything
outside the principal's tenant.

Honest v1 approximations (documented, not hidden): per-source landed counts
group by `source_system` (raw rows don't carry source_ref); source totals
are unknown until adapters report corpus size; the health tile counts
postgres · vault · serving-reachability.

## Part B — the shipping UI

**Stack decision:** Design's mock is already template + plain-JS class +
inline styles, so the shipping stack is exactly that shape — vanilla
HTML/CSS/JS, no framework, no build step, no npm, no network.
`operator_ui/index.html` keeps Design's markup + inline styles VERBATIM
(the visual spec); `app.js` replaces the design-tool `DCLogic` runtime with
plain `fetch()`/render. Served at `GET /ui/` from the operator service
itself — **single origin** (no CORS), inside the package (so it ships in
the kit and installs air-gapped; the wheel/kit carry the package directory
wholesale). The Google-Fonts link is deliberately absent: every
font-family in the spec declares system fallbacks.

**Every `renderVals()` key mapped, every fake dead** (tested: `wobble`,
the hardcoded line pool, `Math.random`, `Math.sin` do not appear in the
shipped JS): landed/facts/stage counts/per-source ← `/v1/monitor`;
p95 ← the proxied serving number; bars/perMin ← the throughput series;
feed/statusLine ← `/v1/monitor/activity`; uptime ← `uptime_s` + a local
ticker; refreshLabel = the real 5s cadence; queue/pair ← `/v1/reviews[/id]`.

**decide() → BP19 writes, Design's keyboard flow exact** (A/R/S/space,
review tab only, never while typing): A = `resolve_merge same:true` ·
R = `resolve_merge same:false` · S = `split_merge` on the LAST merge made
this session (the session undo; the Recorded copy says so — a full
merge-history split view is a Design follow-up) · space = client-side
requeue, no write, counter untouched. On success: Recorded copy with the
`entity_merges:<id>` snapshot ref, `cleared` +1 (not on skip), advance to
the next item. Quarantine/flagged items reuse the pane with A=resolve /
R=dismiss wired to `triage_quarantine` / `resolve_flagged_document`.
Pause/Resume buttons on per-source rows post `pause_source` /
`resume_source` (operator role; a reviewer sees the refusal inline).

**Auth (Part C, minimal-functional):** locked overlay until an operator
credential resolves against `GET /v1/actions`; the token lives in memory +
`sessionStorage` for the session only, rides as a Bearer header, is never
logged or rendered. 401 mid-session re-locks. The login SCREEN is flagged
to Design for polish.

**Undesigned states (Part D, minimal):** empty queue ("Nothing is waiting
on a human.") and a non-alarming offline banner ("Can't reach the system
right now — the console keeps retrying quietly.") with SYSTEM : UNREACHABLE
in the footer. Both implemented plainly, both flagged to Design.

## The bug the browser session caught (and its fix)

Driving the real UI live surfaced what sequential tests could not: the UI
polls monitor + activity + reviews CONCURRENTLY, and psycopg transaction
contexts are not thread-safe on the operator's single shared store
connection — interleaved BEGIN/SAVEPOINT frames stranded the connection
idle-in-transaction, after which every "commit" was a savepoint inside a
transaction that never ended (writes invisible to the outside world).
BP19 had serialized writes; now **every store-touching request** (reads,
alerts, health, writes) shares one lock — invisible at UI-click + 5s-poll
volume, and the audit trail stays strictly ordered. Regression test:
`test_concurrent_ui_polling_never_strands_the_store_transaction` (hammers
the API from threads, proves external visibility of a racing write + no
stranded backend).

## Verified live (browser, real stack)

Login → live monitor (real counts, honest 2/3 health with serving down,
uptime ticking) → review queue (3 real items, least-confident first, real
policy thresholds 0.95 + corroboration, feature-derived evidence panels,
highlighted source passage) → **A** merged Zenith Widgets LLC into Zenith
Widgets (Recorded + `entity_merges:2` + er_match label + audit row, all
confirmed from a separate connection) → **S** reversed it (entity restored
under its original id, er_nonmatch reversal label) → queue counts and the
activity feed narrated every step.

## Tests

`tests/test_operator_ui.py` (7 tests; real Postgres + OpenBao + live
bge-m3, both services on real sockets; full suite 306 green + 4 skips):
monitor snapshot matches the DB (counters, stages, sources, throughput
series, review counts); activity speaks `stage · detail`, newest first;
reviews listing + candidate-pair evidence (policy thresholds, feature-only
evidence, mention passage + provenance); reads role-gated (agent 403),
tenant-scoped (absence), audit-free and mutation-free; the UI's decide()
contract end-to-end with the merge VISIBLE THROUGH READ SERVING and split
undoing it; the concurrency regression; the UI serves from the package with
no CDN, no fakes, no traversal, and renders nothing unauthenticated.

check_stack's operator check now also probes `/v1/monitor` (200 for the
operator, 403 for the agent principal) and `/ui/` (shell serves, no CDN).
Package 0.23.0. Design's `.dc.html` sources live in
`../design/operator/` (v4 pair + the all-8-surfaces root reference).

## Design follow-ups (tracked, not lost)

1. **Login screen** — function built; styling is Design's.
2. **Empty-queue + offline states** — minimal implementations in place;
   designed versions requested.
3. **Split view** — S currently reverses the session's last merge; the
   full merge-history split view (pick an earlier merge of this entity)
   needs design + a merge-history read endpoint.
4. **Startup GUI** (`Startup.dc.html`) — deferred; `khctl launch` (BP18)
   covers on-site deploy.
5. **The other 6 operator tabs** (Sources & access, Errors & health,
   System connections, Data landing, Inference, Facts & entities) —
   designed in the root reference; carry over + restyle to v4 + wire in a
   follow-on (Errors & health can reuse BP19's alerts read).
6. **Fonts** — vendor the OFL woff2s (Archivo/Cinzel/Silkscreen) into the
   kit; system fallbacks render today.

## Build Prompt 25 — console honesty fixes (2026-07-26)

The BP24 sanity check's console findings, closed in the shipping UI
(tests: `test_operator_ui.py` BP25 block + `test_onsite_hardening.py`):

- **Lock screen diagnoses login failures (F1/F3).** On a 401 the shell
  consults `/v1/health` first: a SEALED vault (routine after reboot) gets
  "unseal it — your credential is probably fine" with the working
  docker-exec command; an unreachable vault says so; only then "not
  recognized". A 403 (the AGENT serving credential — valid but no console
  role) shows the server's named-kind guidance instead of unlocking a
  blank dashboard; mid-session read-403s lock with the same message
  rather than silently early-returning.
- **The errors badge is real (F5).** It renders `monitor.alerts_open`
  (the operator_alerts view) instead of a status no code path ever
  writes. The Errors & health TAB itself remains a Design follow-up;
  `khctl alerts` covers day-2 triage meanwhile.
- **L5/L6/L7.** The dead header search is now an honest `LOOKUP : NOT YET
  WIRED` chip; the "screen styling pending Design" dev note is gone from
  the lock screen; the lock screen states the lost-token recovery
  (`khctl provision-operator ...`).

Design follow-up list above: item 1 (login screen styling) now also
covers styling the new recovery + diagnosis copy; item 5's "Errors &
health" tab can wire `alerts_open` + `/v1/alerts` directly.

## Build Prompt 32 — the BP28 console-honesty fixes (2026-07-26)

The BP28 rehearsal's three console findings (report findings 12/13/14 →
tasks #22/#23/#24), closed in the shipping UI (tests: `test_operator_ui.py`
BP32 block). Last of the three BP28 fix packs; pkg stays 0.26.1.

- **STACK : HEALTH tells the truth (#22).** The tile's third component was
  "a p95 latency sample exists" — a fully healthy deploy with no traffic
  yet read 2/3 (67%), contradicting `check_stack` (11/11) and the console's
  own `SYSTEM : NOMINAL` footer, as the first thing anyone sees on day one.
  `/v1/monitor` now carries `serving_status` (`ok` — metrics door answers
  with samples · `warming` — answers, no traffic yet · `down` —
  unreachable), sourced from the same proxied metrics call that already
  fetched the p95. The tile counts the serving PROCESS: warming counts
  green (with a "(warming)" note in the sub-line), a genuinely down
  component is NAMED (`serving DOWN` reads 2/3 with the gauge flipped to
  the alarm color), and a sealed vault says `vault SEALED` rather than a
  generic DEGRADED. `p95_ms` semantics are unchanged (the SERVING : LAYER
  tile still renders "—" until a sample exists — that one is honest).
- **DECIDE labels follow the item type (#23).** The action buttons showed
  merge labels ("Merge them" / "Split an earlier merge") on quarantined
  extractions while the instruction line correctly said "A = resolve ·
  R = dismiss". `renderDetail()` now relabels the buttons per kind from
  one map (`REVIEW_KINDS`): merge → Merge them (A) / Keep separate (R);
  quarantine → Resolve — record the correction (A) / Dismiss (R); flagged →
  Resolve — tag stands corrected (A), with the R button HIDDEN (flagged's R
  posts nothing). The two static keyboard hints (queue footer, session
  panel) no longer claim merge keys for every item.
- **The queue explains itself (#24).** A header now spans the review panel:
  what the screen is ("the calls the pipeline refuses to make alone"), why
  a human is needed, that decisions are audited and merges reversible, and
  what A/R/S/space do — plus a live key legend (`rv-keys`) that re-renders
  per item type alongside the button labels, so the header is never wrong
  about the item on screen.

S (split the session's last merge) remains available on every item type —
it acts on the session's last merge, not the current item, and its label
says so.
