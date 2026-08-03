# Knowledge Hub — pilot setup notes

> **Naming (2026-07-25):** the data-ingestion flow built here is
> **decant.Source (dS)** — canonical spelling exactly `decant.Source` / `dS`.
> "Knowledge Hub" stays the working name for the broader system. Human-facing
> text only: the package `knowledge_hub`, `khctl`, `KH_*` env vars, paths,
> and schema names are deliberately unchanged.

Reproducible bundle for standing up the `knowledge_hub` pilot infrastructure.
Built on a Windows PC (WSL2 + Docker Desktop) as a practice run; designed to be
copied to the Ubuntu workstations and re-run with minimal changes.

## What's in this folder
- `docker-compose.yml` — defines the services (Postgres now; SeaweedFS + OpenBao later).
- `postgres/Dockerfile` — custom Postgres 16 image with pgvector + Apache AGE (pg_trgm ships built-in).
- `postgres/init/00-extensions.sql` — enables the three extensions on first boot.
- `.env` — service credentials (change the password on real hardware).

## How to run (from this folder)
```
docker compose up -d --build      # build images + start services in the background
docker compose ps                 # see what's running
docker compose logs -f postgres   # follow a service's logs
docker compose down               # stop (data is kept in the named volume)
```

## Host / environment
- Windows 11, WSL2, Docker Desktop.
- CPU: Ryzen 9 9950X3D (16c/32t) · RAM: 64 GB · NVMe SSD.
- GPU: 2x NVIDIA RTX PRO 6000 Blackwell (for Ollama, decided at the Ollama step).

## Open items / caveats
- **SeaweedFS WORM enforcement:** object-lock is enabled structurally (versioning +
  object-lock buckets), but strict COMPLIANCE-mode delete-blocking has a known open
  bug (seaweedfs issue #8350, open as of 2026-02). RE-VERIFY strict WORM on the exact
  version deployed to the Ubuntu boxes before trusting it for retention/compliance.
- **SeaweedFS IAM/STS log error (v4.40):** "no signing key found for STS service" is
  BENIGN for us — we use static access-key/secret creds, not STS temporary tokens.
  Confirmed working via the boto3 reachability test. (If we ever need AssumeRole/STS,
  supply a signingKey in the IAM config.)
- **SeaweedFS SSE-S3 (encryption at rest):** disabled; KEK would be plaintext. Optional
  hardening for production — set s3.sse.kek passphrase in security.toml if required.

## Ollama decision (GPU)
- Ollama runs **natively on Windows**, using the 2x RTX PRO 6000 GPUs directly — NOT in
  a container. Everything else runs in Docker. The Python app (on the Windows host)
  reaches Ollama at http://localhost:11434.
- On the Ubuntu boxes: install Ollama natively on the GPU host (systemd service); if the
  Python app runs elsewhere, set OLLAMA_HOST=0.0.0.0 and point the client at that host.
- **Models:** embeddings = `bge-m3` (1024-dim). Extraction STARTER = `qwen3.6`
  (cleanest JSON in smoke test; qwen3:14b close 2nd). Both are reasoning models with
  large think blocks — for extraction, test with `think: false` + Ollama structured
  outputs (`format` = JSON schema). FINAL extraction model decided by the benchmark
  (Axes A/B/C), not this smoke test.
- **Extraction model is BACKEND-DEPENDENT (2026-07-28):** MoE architecture on both
  paths per the §8.26d decision (Option A, decided same day): `qwen3.6:35b-a3b-bf16`
  is the NVIDIA-path TARGET (CUDA; the signed 0.26.x kits still carry the proven
  dense `qwen3.6:27b-bf16` until a future kit build executes the switch), and
  `qwen3.6:35b-a3b-q4_K_M` runs on the AMD Strix Halo path (ROCm; dense is
  bandwidth-bound there). There is no single universal extractor, and cross-path
  quality equivalence is UNVALIDATED until Axis D measures it. Details:
  DEPLOY_NOTES.md (BP43/BP44 section), knowledge_hub_pkg/EXTRACTION_NOTES.md,
  progress doc §8.26.
- **Extraction layer (done, Build Prompt 4):** predicate vocabulary validated in
  deterministic code against ontology_versions data; unambiguous variants normalize
  via the ontology's own alias map ("owned by" -> `owns`, swapped); genuine unknowns
  quarantine instead of being force-mapped. See knowledge_hub_pkg/EXTRACTION_NOTES.md.

## Progress log
- [x] WSL2 confirmed, Docker Desktop installed & running.
- [x] Postgres up + 3 extensions verified (age 1.5.0, pg_trgm 1.6, vector 0.8.0).
- [x] Baseline schema applied (ontology baseline-0.1, 11 policy rows, graph knowledge_hub).
- [x] SeaweedFS up (S3 gateway on 8333, master UI v4.40). Object-lock bucket created at e2e step.
- [x] OpenBao up (dev mode, unsealed, KV round-trip verified).
- [x] Ollama + models: bge-m3 (verified 1024-dim), qwen3.6 (starter extraction). Both 100% GPU.
- [x] Python 3.12.13 venv (via uv) + 139 deps + knowledge_hub package (PLACEHOLDER
      scaffold in knowledge_hub_pkg/ — swap in the real package when it arrives).
- [x] End-to-end reachability check: ALL GREEN (2026-07-22). `python check_stack.py`
      -> postgres 13 tables / kh-raw bucket object-lock=Enabled / openbao KV ok /
      bge-m3 1024-dim + qwen3.6 on GPU.
- [x] Persistence layer (Build Prompt 1, 2026-07-22): FactStore interface + Postgres
      implementation + pipeline persistence stubs in knowledge_hub_pkg/, plus
      migrations/001_persistence_addenda.sql (tenancy, raw versioning, pending_facts;
      applied to the pilot DB, tracked in schema_migrations). 19 pytest tests green
      against a throwaway kh_factstore_test DB. See knowledge_hub_pkg/PERSISTENCE_NOTES.md
      — includes an AGE 1.5.0 MERGE+SET gotcha worth reading before touching the graph.
- [x] Processing Stage B, prose/SOP track (Build Prompt 3, 2026-07-22): Parser/Chunker/
      Embedder seams + Docling parser, bge-m3-tokenized section/passage chunker
      (~300-tok children, 15% overlap, contextual prefixes), Ollama bge-m3 embedder,
      ProcessingService (also the dispatch-queue consumer), migrations/003_document_review.sql
      (§8.1a tag-as-claim -> review_queue 'document' feeder; applied to the pilot DB).
      52 tests green (8 new, live bge-m3, no mocks); sample SOP-014 run: 1 superparent /
      6 parents / 7 embedded children, replay = no-op. requirements.lock.txt REWRITTEN
      (was UTF-16 with a Windows-path -e line + unmarked win-only pins — would have
      broken the Ubuntu replay); check_stack.py gained a 5th check (processing).
      See knowledge_hub_pkg/PROCESSING_NOTES.md.
- [x] Extraction stage (Build Prompt 4, 2026-07-22): OntologyBinding/
      ExtractionStrategy/Grounder seams + binding generated from ontology DATA
      (examples + predicate aliases live on the ontology row — migration 004),
      qwen3.6 joint extraction (think:false, format=schema, temp 0, digest
      coref, 1-repair cap), deterministic StructuredMap for SoR, SpanGrounder
      (flag-don't-reject), ExtractionService consuming the new extraction_queue
      (outbox #2), quarantine + grounding review feeders, extraction_runs
      observability/idempotency ledger. migrations/004_extraction.sql applied
      to the pilot DB + tracked. 62 tests green (10 new, live qwen3.6, no
      mocks); check_stack.py gained check 6 (extraction). Pilot SOP-014 run:
      26 staged pending facts / 3 quarantined (incl. the 'retained_for'
      ontology gap) / 7 grounding flags; replay = no-op. IMPORTANT for the
      benchmark: prompt contract p1 -> p2 story + honest quality observations
      in knowledge_hub_pkg/EXTRACTION_NOTES.md — extraction quality is NOT
      established, only the machinery.

- [x] Metrics report pipeline (2026-07-22): `python metrics_report.py` snapshots the
      observability tables (extraction_runs, pending_facts, quarantine, resolution_decisions,
      match_candidates, labels, review_queue) into `reports/KnowledgeHub_Metrics_<date>.xlsx` —
      Raw_ sheets (data) -> Metrics sheet (COUNTIFS/SUMIFS formulas) -> Dashboard (KPI tiles +
      7 charts; headline = extraction contract p1 vs p2). Auto-recalcs via headless LibreOffice
      (recalc_uno.py — Calc doesn't recompute xlsx on open). Detects benchmark_runs (future
      migration 006) and adds a sheet automatically when it lands. openpyxl + psutil promoted
      to direct deps in requirements.txt (already in the lock). NOTE: benchmark methodology
      doc drafted, AWAITING APPROVAL — see .Progress Docs/Ongoing/
      KnowledgeHub_Benchmark_Methodology_v0.1_2026-07-22.md; Deliverables 2-4 gated on it.

- [x] Benchmark recording harness (Build Prompt 6, 2026-07-22): methodology doc APPROVED
      (v1.0) -> migration 006 (gold_sets/gold_set_items/pin_profiles/benchmark_runs/
      benchmark_run_items + benchmark_leaderboard view; applied + tracked), goldsets.py
      (versioned immutable gold sets, activation by a named human, floors -> advisory;
      synthetic/LLM-leakage-guarded/ER-corruption/extraction-drafter generators),
      benchmark.py (runner: one-axis knob schemas, pin-profile verification, full
      provenance, exact-kNN retrieval evaluator + bootstrap CIs, error rows on crash,
      duplicate refusal), benchmark_dryrun.py (Deliverable 4 — VALIDATED on the pilot DB,
      tenant bench-dryrun, aggregates recompute from per-item rows). 85 tests green
      (9 new); check_stack check 8; package 0.6.0 (editable reinstall fixed stale 0.0.1
      dist metadata the dry-run caught). metrics_report.py now renders axis leaderboards
      (Benchmarks sheet: one chart per gold set + pin profile). Campaign phase (install
      VectorChord/Zingg/alt models, real gold sets, run configs) is NEXT.
      See knowledge_hub_pkg/BENCHMARK_NOTES.md.

- [x] Benchmark CAMPAIGN phase started (2026-07-23): Axis-C evaluator corrected to
      re-embed the corpus with the config's OWN embedder (was reading stored bge-m3
      vectors — cross-space nonsense for challengers) + per-model dim auto-detect
      (nomic 768 / bge-m3 1024) + dense-only guard. Advisory shake-out recorded 4
      embedders (bge-m3/mxbai/snowflake-arctic/nomic) on the synthetic set — all 1.00
      (trivial 5-query set, k>corpus), validates multi-config leaderboard, decides
      nothing. metrics_report.py hardened: clears stale .~lock. before recalc + removes
      temp LO profile after. 86 tests green. BLOCKER for real numbers: pilot has ONE
      doc → no gold set meets the §6.2 floors; need a representative multi-doc corpus
      per track. See knowledge_hub_pkg/BENCHMARK_NOTES.md "campaign update".

- [x] FIRST REAL AXIS-C BENCHMARK (2026-07-23): synthetic corpus generated (corpus_synth.py
      + build_corpus.py; 121 fictional docs → tenant bench-synth; ALL synthetic material in
      `..\SYNTHETIC DATA - NOT REAL - BENCHMARK ONLY\` — NEVER mix real docs in there) →
      3 floors-meeting retrieval gold sets (build_gold_retrieval.py; activated by the operator) →
      15 non-advisory runs (run_axis_c.py) → **bge-m3 HOLDS ALL TRACKS** (R@10 .917/.967/.900);
      nomic-embed-text near-tie at 768-dim/faster but below the frozen +.02 margin;
      snowflake-arctic collapse likely missing task-prefix usage (future config knob).
      metrics_report.py now tenant-scoped (--tenant). See knowledge_hub_pkg/BENCHMARK_NOTES.md.

- [x] Axis-C ROUND 2, prefix-aware rematch (Build Prompt 7, 2026-07-23): prompt_style
      knob (per-model OFFICIAL query/doc prefixes sourced from model cards; exact strings
      ride in run config as provenance), migration 007 (superseded_by_run_id/note +
      leaderboard `superseded` flag; round 1's 15 runs marked superseded, never deleted),
      qwen3-embedding wired (4096-dim, instruct template). Dense rematch bge-m3 vs
      nomic[prefixed] vs qwen3[instruct]: **bge-m3 HOLDS ALL TRACKS in a fair fight**
      (qwen3+instruct jumped +5..13 pts vs bare — round 1 measured prompts, not models;
      on sop it hit 0.950 vs 0.917 but the paired CI touched zero). nomic's official
      prefixes HURT on this corpus (recorded, unexplained, verdict-neutral). No new deps.
      88 tests green. See knowledge_hub_pkg/BENCHMARK_NOTES.md round-2 entry.

- [x] Axis-C ROUND 3, bge-m3 HYBRID vs DENSE (Build Prompt 8, 2026-07-23): mode knob
      dense|hybrid + fusion params (RRF default) + engine seam (ollama|flagembedding;
      Ollama is dense-only so sparse comes from FlagEmbedding 1.4.0 — new deps in
      requirements + lock; Windows needs torch cu130 from the pytorch index). Per-query
      latency p95 instrumented (carried TODO paid; gate live). **DENSE HOLDS — hybrid
      lost sop OUTRIGHT (0.717 vs 0.917, CI excludes 0; identifier-crowding under RRF),
      noise elsewhere; latency decided nothing (hybrid p95 18ms).** Engine diagnostic:
      GGUF vs fp16 dense = IDENTICAL rankings (zero confound). Hybrid = tested-and-not-
      adopted; no pin change. Gotcha: FlagEmbedding multi-GPU spawns a mp pool that
      deadlocks on Windows — pin devices="cuda:0". See BENCHMARK_NOTES round-3 entry.

- [x] API SERVICE — the serving boundary (Build Prompt S5, 2026-07-24): the S1
      ServingService seam + a stdlib-only HTTP+JSON boundary in service_http.py
      (Decision 6: agents live OUTSIDE the boundary; the service IS the boundary,
      zero framework magic between request and gate). Endpoints GENERATED from the
      S3 registry (one POST per registered op/composite + /v1/retrieve with the
      enrich knob; no ad-hoc/raw-SQL surface exists); bearer token → S2 OpenBao
      principal resolution at the boundary (401 generic on unknown/revoked;
      identity unassertable in the body); ONE choke point = ONE serving connection
      for all tenants (bounded, tested: zero backend growth across 30 calls × 10
      tenants); UsageTracker wired (serialization IS the read — EnvelopeUsage per
      served envelope to append-only JSONL); per-endpoint p50/p95/p99 on
      /v1/metrics vs the §4 budget (p95<=300ms). check_stack: version-integrity
      check now runs FIRST (editable drift bit twice) + serving is check 9 (health,
      fail-closed 401, gated HTTP round-trip). 173 tests green (13 new, real HTTP +
      real vault + live bge-m3). Package 0.11.0. httpx promoted to direct dep
      (tests only). RIDER (required follow-up, scoped in SERVICE_NOTES.md): migrate
      already-acting agents onto the service as their single read path and REVOKE
      their direct-Postgres access — every external DSN holder is a side door
      around the choke point until this is done. See knowledge_hub_pkg/
      SERVICE_NOTES.md.

- [x] OPERATOR WRITE API — the write-twin (Build Prompt 19, 2026-07-24): the
      operator UI's actions as a separate, enforced, audited write path in
      operator_http.py — the read boundary stays provably read-only, untouched.
      OperatorGate = the single write choke point (same OpenBao identity; write
      roles reviewer ⊂ operator, agent read-principals refused; tenant injected
      from the principal ONLY — identity params unregistrable, unscoped specs
      unconstructable; cross-tenant target = 404 absence, zero mutation). Fixed
      write-op registry → generated endpoints (POST /v1/actions/<name>): review
      resolution via the resolver's REVERSIBLE merges (resolve_merge /
      resolve_as_new / split_merge — decisions land as flywheel labels),
      triage_quarantine (correction label), resolve_flagged_document (§8.1a:
      human tag wins, claim corrected at source, doc requeued), pause/resume
      source (capture really skips), retry_failed_item, acknowledge_alert
      (operator_alerts VIEW over real state), add_source/edit_scope
      (credential-shaped config keys refused; vault path + presence only —
      secrets NEVER transit; start_pull bookmarked pending a pull queue).
      Migration 010 (operator_audit incl. refused attempts + queue ack columns
      + alerts view) applied + tracked. 12 new tests (both services live over
      HTTP; headline: a merge made through the operator API is immediately
      visible through the READ serving layer, and split_merge undoes it
      end-to-end) — 299 green. check_stack check 11 + khctl verify adoption.
      Package 0.22.0. THE OPERATOR UI IS NOW WIREABLE END TO END (reads :8080,
      actions :8081) — the review queue can resolve real items during the
      on-site self-replay. See knowledge_hub_pkg/OPERATOR_API_NOTES.md and
      progress doc §8.12.

- [x] OPERATOR UI — monitor + review live end to end (Build Prompt 20, 2026-07-25):
      Design's v4 mockups (design/operator/) rebuilt in the shipping stack — vanilla
      no-build HTML/JS, markup + inline styles verbatim, design-tool runtime replaced
      by fetch/render (knowledge_hub/operator_ui/, served at /ui/ from the operator
      service: single origin, air-gapped, ships inside the package/kit; no CDN).
      Part A closed the UI's DB side door first: operator READ endpoints
      (/v1/monitor, /v1/monitor/activity, /v1/reviews[/id]) — read-only, tenant +
      role enforced, evidence panels derived only from the resolver's recorded
      features. Every mock fake killed (tested against the shipped JS); decide() =
      the BP19 writes with Design's exact keyboard flow (A/R/S/space; S = session
      undo via split_merge; skip writes nothing); minimal auth (session-only bearer,
      locked state) + empty-queue/offline states, all flagged to Design for polish.
      LIVE-VERIFIED IN THE BROWSER: login → real monitor → A merged a pair (snapshot
      + er_match label + audit row confirmed from a separate connection) → S reversed
      it. En route the browser session caught a real concurrency bug (UI polling
      interleaved psycopg transaction frames on the shared store connection →
      stranded idle-in-transaction, writes invisible) — fixed by serializing ALL
      store-touching requests + a regression test. 306 tests green; check_stack
      operator check extended (reads + UI probes); pkg 0.23.0. Design follow-ups
      tracked in knowledge_hub_pkg/UI_NOTES.md (login screen, empty/offline polish,
      split-history view, Startup GUI, the 6 remaining tabs, vendored fonts).
      THE ON-SITE OPERATOR CAN WATCH INGESTION AND RESOLVE REVIEWS FROM THE UI at
      the 07-29 self-replay. See UI_NOTES.md + progress doc §8.13.

- [x] NAMING — decant.Source (dS) (Build Prompt 21, 2026-07-25): the ingestion flow
      is now named decant.Source (dS) on HUMAN-FACING surfaces only — progress doc
      (title + decision recorded in its header), DEPLOY_NOTES + this file's prose,
      the operator console <title>, and Design's reference labels. Code identifiers
      provably untouched (package knowledge_hub, khctl, KH_* env vars, paths,
      schema, migrations, tests — verified by grep + full suite + check_stack).
      pkg 0.23.1 (docs/naming only). KIT NOTE: the staged proven kit (built at pkg
      0.21.0) is untouched and still verifies; it predates BP19/20 anyway, so
      carrying dS + the operator API/UI on-site rides the SAME rebuild + org-2026
      re-sign already needed for those — the operator's interactive signing ceremony.
      Left for review (ambiguous, machine-adjacent): "Knowledge Hub.desktop" +
      its Name= label (rendered by khctl make-ssd — renaming touches launcher
      code + the SSD layout DEPLOY_NOTES documents), CLI banners/argparse text in
      deploy_*/check_stack/metrics_report (umbrella-system wording), the
      KnowledgeHub_Metrics_*.xlsx report filename pattern, design/*.dc.html
      FILENAMES (cross-referenced by href). Deck / on-site game plan / kickoff
      briefs: not present in this workspace — nothing to rename there yet.

- [x] FINAL WALK-IN KIT — rebuilt + re-signed at 0.24.0 (Build Prompt 22,
      2026-07-25): the SSD now carries the whole dS stack. NEW INTEGRATION first:
      the deployed-state launcher starts the OPERATOR CONSOLE beside serving
      (deploy_launch.ensure_operator → python -m knowledge_hub.operator_http; the
      watch points now lead with http://127.0.0.1:8081/ui/) — 2 new tests, 308
      green, check_stack 11/11, pkg 0.24.0. SECOND FULL-SCALE BUILD: 6m25s wall
      for 60.48GB / 300 artifacts (both prior fixes held on a fresh build: 180
      linux wheels resolved in-container with no ResolutionImpossible; the
      55.58GB qwen3.6:27b-bf16 blob streamed + hash-verified). Kit NOW CONTAINS
      operator_http.py, operator_reads.py, operator_ui/, migrations 001–010.
      SIGNED org-2026 (operator entered the passphrase in a console window —
      the secret never touched the session); verify-kit green in 36.7s
      (signature-first, all 300 hashed, no unlisted files; the unsigned kit was
      REFUSED first, proving the gate). ROUND-TRIP vs the fresh signed kit:
      launcher verify-kit → probe → ADOPTION GATE fired on the pilot's own
      Postgres and was declined (choice ours, operator_override recorded) →
      plan (tenant ops) → apply --dry-run through all 9 phases incl. "replay 10
      migration(s)" (010) + version integrity 0.24.0 + both models. make-ssd
      RESTAGED: KH_SSD_STAGING_0.25.0 = launch.sh + Knowledge Hub.desktop +
      kit/ (57G), trust anchor org-2026; post-restage verify-kit re-run
      (launcher never writes into the kit). The proven 0.21.0 staging
      (KH_SSD_STAGING) kept as fallback until the physical SSD copy. STILL
      UBUNTU-ONLY PROOFS: GNOME double-click launch, wet compose + raft vault,
      model-store copy to the box, GPU inference, and the operator UI served
      on the target.

- [x] OPERATOR ACCESS — credentials + the console door (Build Prompt 23,
      2026-07-25, pkg 0.25.0): answers "what do I log in with?" + the one-click
      way in. Deploy bootstrap (phase_tenants) now mints the FIRST operator
      credential per tenant — print-once (the unseal-share ceremony), vault-
      markered idempotent, attributed (provisioned_by/at ride the registry
      record; the value exists only hashed-in-vault + on the terminal).
      `khctl provision-operator --tenant t --role operator|reviewer` = the
      issue-more path (SME reviewers; vault custody is the gate).
      `khctl console` = the door: REUSES ensure_operator (no second start
      path) + opens the browser at :8081/ui/; dev/pilot context mints+prints a
      throwaway dev key (pilot vault is dev-mode/ephemeral — keys die with it);
      DEPLOYED context NEVER mints (gate: deploy_plan.json present AND no
      dev-vault literal in .env — a real deploy's root token lives with
      custody, never on disk, so the bench's stray test plans can't misfire
      it, found live and tested). SSD shortcut #2: make-ssd now also writes
      console.sh + 'Open Console.desktop' (decant.Source — Operator Console;
      refuses an undeployed box, points at the launcher). Windows dev
      shortcut: console.cmd (this folder). VALIDATED: 10 new tests (317
      green) — bootstrap print-once + idempotent re-run + the printed token
      logs in end to end; reviewer credential is reviewer-scoped only;
      dev-mint context matrix; no credential value on disk (work dirs +
      operator.log + usage logs grepped clean); LIVE first look: khctl
      console on the pilot, dev key logged into the real console — tenant
      bench-synth rendering 139 docs / 12 sources / 1,558 staged facts / 324
      review items. Credential-lifecycle security note in
      knowledge_hub_pkg/OPERATOR_API_NOTES.md. check_stack 11/11.

- [x] ON-SITE HARDENING — the BP24 sanity-check code fixes (Build Prompt 25,
      2026-07-26, pkg 0.26.0): the 18 code-fixable findings CLOSED, each with
      a test proving the NEW behavior (tests/test_onsite_hardening.py + BP25
      blocks in test_operator_ui/test_operator_access). The
      deceive-the-operator set: repair/re-plan PRESERVES the deployed vault
      root token + hvac auth failures answer with .env.bak (B3); sealed vault
      reported as SEALED everywhere — health vault_status, lock-screen
      branch, console/provisioning refusals, docker-exec unseal command (F1/
      F2-code); agent tokens REFUSED at console login with named-kind
      guidance (F3); console/start_program honor supervisor failures — no
      browser onto connection-refused (F6); a half-applied deploy re-enters
      REPAIR via the new .apply_progress.json ledger + a models-actually-
      served live check (F14). Crashes & evidence: print-once secrets gate
      on typed RECORDED + launch.sh holds its window (B2); offline prereq
      wall + PREREQS.txt at the SSD root, no apt-get hints anywhere (B1);
      phase_models restarts ollama after the 56GB copy + honest failure text
      (F13); khctl ingest pre-flight + [FAIL] language (F17). New surfaces:
      khctl alerts (first consumer of /v1/alerts + retry/ack) + the errors
      badge counts operator_alerts (F5); khctl provision-agent re-mints the
      serving credential (F16); GET /v1/passages/<chunk_id> answers "where
      did this fact come from?" through a proper door (F18). Friction: khctl
      symlinked onto PATH + "$@" passthrough + full-path hints (L1); tenant
      prompt re-asks on empty (L2); silence-is-work notices + un-quiet pip
      (L3); lock-screen dev note deleted, dead search made honest, recovery
      sentence added (L5/L6/L7). Left for the runbook: shares custody/loss,
      the game plan (F19), box pre-provisioning, VRAM pre-flight. FULL suite
      + check_stack green; the FINAL kit rebuild + re-sign can now capture
      the hardened build. See DEPLOY_NOTES/OPERATOR_API_NOTES/UI_NOTES BP25
      sections + progress doc §8.16.

## Running the serving service (operational component)

The serving API is a host-side Python process (like the pipeline, not a
container). From this folder with the venv active:

```
python -m knowledge_hub.service_http --tenant <tenant-id> [--port 8080]
```

- Config via env or flags: SERVING_HOST (default 127.0.0.1 — exposing beyond
  the host is a deployment decision), SERVING_PORT (8080), SERVING_TENANTS
  (comma-separated; the tenants whose op surface gets registered at boot),
  SERVING_USAGE_LOG (append-only JSONL of per-envelope usage records).
- It refuses to start if the read-only serving connection won't warm.
- Observability: GET /v1/health (components + installed version) and
  GET /v1/metrics (per-endpoint latency percentiles vs the 300ms p95 budget).
- Callers authenticate with an opaque bearer token provisioned via
  OpenBaoCredentialResolver.register_principal (hub-owned vault registry).
- On the Ubuntu boxes run it under systemd, same pattern as Ollama:
  ExecStart=<bundle>/.venv/bin/python -m knowledge_hub.service_http,
  Environment=SERVING_TENANTS=..., After=docker.service ollama.service.
  check_stack.py's serving check (self-hosted, ephemeral port) proves the
  component without needing the daemon running.

The OPERATOR WRITE API (BP19) runs beside it as a second process:

```
python -m knowledge_hub.operator_http [--port 8081]
```

- Config: OPERATOR_HOST (default 127.0.0.1) / OPERATOR_PORT (8081). No tenant
  list — write ops are tenant-scoped per request from the resolved principal.
- Operator/reviewer principals are provisioned the same way as agent tokens
  (OpenBaoCredentialResolver.register_principal) but with roles ["reviewer"]
  or ["operator"] — agent read-principals can perform NO write action.
- It refuses to start if migration 010 isn't applied (operator_audit missing).
- Same systemd pattern; the UI points reads at :8080 and actions at :8081.
- THE OPERATOR CONSOLE (BP20) is served by this same process at
  http://127.0.0.1:8081/ui/ — no extra server, no build step, works
  air-gapped. Log in with an operator/reviewer credential; the monitor and
  review queue are live (reads via the operator read endpoints, decisions
  via the write actions, everything audited).
- WHAT YOU LOG IN WITH (BP23): a real deploy prints the first operator
  credential ONCE at tenant bootstrap; issue more with
  `khctl provision-operator --tenant <t> --role operator|reviewer`.
  On this pilot bench: double-click `console.cmd` (or
  `.venv\Scripts\python.exe -m knowledge_hub.deploy_cli console
  --tenant bench-synth`) — it mints + prints a throwaway DEV key and opens
  the browser. On the SSD: 'Open Console.desktop' is the deployed box's
  one-click (it never mints — paste your provisioned credential).

## Replay on Ubuntu (next week)
1. Copy this whole folder to the workstation.
2. `bash install-ubuntu.sh`  (installs Docker if needed — may ask you to re-login once,
   then re-run; then builds services, applies schema, installs Ollama + models,
   creates the 3.12 venv, installs deps + package, runs check_stack.py).
3. Success = same "stack is GO" output (now 11 checks: version integrity first,
   the serving + operator boundaries last).
4. Optional: start the serving service and the operator write API (see
   "Running the serving service" above) once tenants + operator principals
   are provisioned.
Gotcha found on the practice run (already fixed in the init script): ag_catalog must
come LAST on the search_path or the schema lands in the wrong Postgres schema.
