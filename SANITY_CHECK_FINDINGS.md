# SANITY_CHECK_FINDINGS — user-readiness red-team (Build Prompt 24)

**Date:** 2026-07-25 · **Scope:** can a real person deploy it, log in, run it, recover, and trust it — NOT an architecture review. Ranked for the ~2026-07-29 controlled self-replay (one tenant, Diversified Botanics; operator = technical; on-site with IT + Agents-DB; Ubuntu boxes, SSD kit, no internet). Second lens: future non-technical, regulated clients (`client-later`).

**Method:** six operator journeys + the known-open items walked against the code by independent reviewers, then every finding adversarially re-verified against the whole repo (dev tree `knowledge_hub_pkg/knowledge_hub/`, shipped kit `KH_SSD_STAGING_0.25.0/`, notes, design mocks). 54 findings confirmed, **0 refuted**. Deduped across journeys to the list below. Everything cited; absences state where we looked. Note: the "PRE-LIVE READINESS BOOKMARK" referenced in the prompt does not exist as a section anywhere in the repo or progress doc — §8.9 Addendum 4 + §8.13–8.15 + the notes files were used as the readiness context instead.

---

## The 22 things to fix before the replay

The BLOCKER + on-site-IMPORTANT set, grouped into seven fix bundles (many are one-liners sharing a fix):

**Bundle A — the launcher can eat the day (B1, B2, B3, F10)**
1. **B1** Offline prereq wall: nothing on the SSD says docker/minisign/python3.12-venv/ollama must be pre-installed; every failure hint says `apt-get`/`curl` — impossible offline.
2. **B2** Print-once secrets (5 unseal shares + root token + 2 credentials) print mid-scroll with no "confirm recorded" pause, in a terminal window that self-closes at exit.
3. **B3** The advertised "d — repair / re-plan" path clobbers the live vault root token in `.env`, crashes with a raw traceback, then everything looks like mass credential revocation.
4. **F10** Any non-ApplyError failure (most likely: vault not yet listening on first wet compose-up) kills apply with a raw Python traceback — in the self-closing window.

**Bundle B — vault-sealed is misdiagnosed everywhere (F1, F2)**
5. **F1** A sealed/down vault (guaranteed after any reboot) is reported as "That credential was not recognized" at login, mid-session, and by provision-operator — inviting a re-issue spiral instead of an unseal. `/v1/health` even reports `vault: true` while sealed.
6. **F2** The one unseal instruction printed (`bao operator unseal`) names a binary that does not exist on the box; the working form (`docker exec -it kh-openbao bao operator unseal`, 3×) and the shares' location are written nowhere in the kit.

**Bundle C — the console lies when things are wrong (F3, F4, F5, F6)**
7. **F3** The agent serving credential (printed back-to-back with the operator one) logs into the console successfully — then every read 403s silently: blank dashboard, "SYSTEM : NOMINAL", no error.
8. **F4** A reachable-but-erroring backend (Postgres dies, service answers 500) freezes the numbers silently; local uptime keeps ticking; footer stays NOMINAL.
9. **F5** The errors badge is computed from a status (`error`) no code path ever writes — it is structurally always 0 while documents fail; the "Errors & health" tab is an unwired placeholder; `/v1/alerts` + retry/ack exist server-side with no UI or CLI consumer.
10. **F6** `khctl console` / `start_program` ignore their supervisor's failure, print success, and open the browser onto connection-refused — while the terminal carrying the diagnostics closes itself.

**Bundle D — "is it progressing or stuck?" (F7, F8)**
11. **F7** Ingestion only moves while a CLI sweep runs, but the console brands itself "LIVE / near-real-time"; a dead `--watch` loop is indistinguishable from a healthy quiet system (`last_run_at` is served and never rendered).
12. **F8** Per-source progress is decorative: same landed count for every same-system source, progress bar hardwired to 60%.

**Bundle E — review actions (F9, F11, F12)**
13. **F9** Decision confirmation ("Recorded… Press S to undo") is hidden milliseconds later by the auto-advance re-render — never readable.
14. **F11** One keydown = one permanent verdict: no `e.repeat` guard, no confirm; undo covers only the last merge; keep-separate/quarantine/flagged have none and write flywheel labels.
15. **F12** Flagged-document review can't show the document and can't send `corrected_data_track` — while its copy claims "tag stands corrected". The human-tag-wins path is unreachable from the UI.

**Bundle F — deploy edge cases (F13, F14, F15, F16)**
16. **F13** `phase_models` never restarts ollama after the 56GB store copy, then fails with a message that is factually wrong ("no kit ollama_models/") and prescribes an egress-only fix.
17. **F14** A deploy that died at phase 6–9 re-enters the launcher as "deployed"; plain Enter starts the program on a box with no models or credentials.
18. **F15** 54GB VRAM is a silent hard floor; the refusal's own escape hatch (`--allow-gated-tier`) can't pass through `launch.sh` (no `"$@"`) and the gated model isn't in the kit anyway. Verify the replay box's VRAM **now**.
19. **F16** The agent serving credential has NO re-mint path at all (provision-operator only mints operator/reviewer) — lose it and it's hand-rolled hvac on-site.

**Bundle G — day-2 commands and the demo question (F17, F18, F19)**
20. **F17** `khctl ingest` (the advertised day-2 command) greets any infra failure with a raw traceback; no pre-flight.
21. **F18** "Where did this fact come from?" dead-ends at `document 17, chunk 203` — no built surface dereferences a served fact's IDs to the passage (evidence→facts works; facts→evidence doesn't).
22. **F19** The on-site game-plan runbook (§8.9 Addendum 4's own "next artifact") is still unwritten — it is the artifact that absorbs half this list (prereqs, secrets ceremony, which-credential-is-which, unseal command, curl fallback, tenant naming).

---

## BLOCKERS (3)

### B1 — Offline prereq wall: the kit cannot install its own prerequisites and every failure message prescribes the internet
- **Scenario:** The operator plugs the SSD into a fresh Ubuntu box on the no-internet replay day and double-clicks `Knowledge Hub.desktop`. The box wasn't pre-provisioned — nothing told anyone it had to be.
- **What happens now:** `launch.sh` dies at the first gate if minisign is absent — "sudo apt-get install -y minisign" (`launch.sh:28-29`). Next walls: python3.12-venv (`launch.sh:53-54`), docker (`deploy_apply.py:186-195`), ollama (`deploy_apply.py:389-392`). None ship in the kit (`deploy_kit.py:107-118` BUNDLE_FILES). The one in-kit installer, `install-ubuntu.sh`, curls get.docker.com and ollama.com and runs `ollama pull` — all egress (`install-ubuntu.sh:38-44, 81-88`). The prereq list exists only in dev-repo `DEPLOY_NOTES.md:305-307`, which is not in the kit; the SSD root carries no README/runbook at all. The runbook is confirmed "To be written" (progress doc §8.9 Addendum 4, ~line 1515).
- **The gap:** nothing the operator holds on-site states the four OS prerequisites or that they must be installed BEFORE going offline; every failure path names an impossible fix; `install-ubuntu.sh` looks like the installer and silently requires egress.
- **Severity:** BLOCKER · on-site-now.
- **Fix direction:** pre-flight the box with internet before 07-29 (or carry the .debs + ollama tarball on the SSD); put a PREREQS/README at the SSD root; make `launch.sh` check all four up front with an offline-honest message.

### B2 — Print-once secrets scroll away and the launcher terminal self-closes; no acknowledgment gate; no shipped worst-case runbook
- **Scenario:** first wet deploy via the desktop shortcut. Phase 5 prints the 5 unseal shares + root token; phase 8 prints the agent credential and operator credential; verify then prints ~11 more lines and the launcher exits — and the GNOME terminal window closes with it, taking scrollback.
- **What happens now:** the ceremony text is well-flagged ("record NOW, shown once, never stored") but apply never pauses — no `input()`/confirmation exists anywhere in `deploy_apply.py` (`:329-346` shares; `:446-452, 509-519` credentials). The `.desktop` entry is `Terminal=true` + `exec bash launch.sh` (`deploy_kit.py:693-703`) so window lifetime = process lifetime; `launch.sh` has no hold-open at exit — while `console.sh` DOES (`read -r -p "press Enter to close"`, `deploy_kit.py:722-727`), proving the hazard is known. Shares lost = no recovery by design (re-init the vault); the unseal-runbook-per-box invariant (`DEPLOY_NOTES.md:95-97`) ships nothing — the SSD root has no doc files.
- **The gap:** the most losable-forever secrets in the system are delivered mid-scroll into a self-destructing window with no "confirm you recorded this" gate. Worst case is re-init + re-provision everything mid-day in front of the team.
- **Severity:** BLOCKER · on-site-now.
- **Fix direction:** after each print-once block, require "type RECORDED to continue" when stdin is a tty; end `launch.sh` with the same hold-open `console.sh` already has; ship the unseal/shares-lost runbook on the SSD. Plus a runbook step: "you will see 4 secrets — record to the password manager, confirm each."

### B3 — The advertised repair/re-plan path clobbers the live vault root token, crashes raw, and then presents as mass credential revocation
- **Scenario:** anything hiccups on 07-29 (or a tenant is added later). The deployed menu offers "d — re-run the guided deploy (repair; phases are idempotent)" and two printed hints advertise "re-plan adds them later." The operator follows the system's own guidance.
- **What happens now:** `guided_deploy` always re-runs plan (`deploy_launch.py:388-396`); `render_env` starts from `PILOT_ENV_DEFAULTS` including `BAO_ROOT_TOKEN=kh_pilot_root_token` (`deploy_cli.py:62-72`, `deploy_profiles.py:355`); `phase_env` unconditionally copies `.env.deploy` over `.env` (`deploy_apply.py:217`), destroying the real root token that `phase_openbao` appended at init (`:340-346`). The idempotent branch then calls the vault with the stale pilot token; hvac raises `Forbidden`; `run_apply` catches only `ApplyError` (`:543-552`) → raw traceback. Afterward every khctl command and both services fail auth — indistinguishable from total credential loss. The zero-tenants recovery path (see L2) walks straight into this.
- **The gap:** the system's own recovery and growth procedures silently destroy working credential config and then misdiagnose the damage — "how do I get back in?" triggered by following on-screen instructions.
- **Severity:** BLOCKER · on-site-now.
- **Fix direction:** make `phase_env` preserve an existing non-pilot `BAO_ROOT_TOKEN` (or have render_env never emit the placeholder over a deployed home); catch hvac auth failures in `run_apply` as ApplyError pointing at `.env.bak`.

---

## IMPORTANT — on-site (19)

### F1 — Sealed/unreachable vault masquerades as "credential not recognized" everywhere
- **Scenario:** the box reboots (likely on fresh hardware). The prod raft vault comes back SEALED — by design. Someone opens the console via the desktop shortcut (which never checks vault state) and pastes a perfectly valid credential.
- **What happens now:** sealed → hvac `VaultError` → `PrincipalUnresolvable` (`choke_point.py:217-223`) → 401 → "That credential was not recognized." (`app.js:97-99`); mid-session polls lock with "no longer recognized" and wipe the stored token (`app.js:68`). Vault down at transport level → `ConnectionError` → 500 → same message. The natural next move, `khctl provision-operator`, fails against the sealed vault with its own misleading custody message (`deploy_cli.py:409-413`). Worse, `/v1/health` reports `vault: true` while sealed: `ping()` bool-tests the hvac health dict, and sealed returns a non-empty 503 JSON body (`choke_point.py:241-249`).
- **The gap:** a predictable benign state is diagnosed as a bad token at exactly the moment it's most likely, steering the operator toward a pointless re-issue spiral while the health surface denies the real cause.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** on login failure, have the lock screen consult `/v1/health` first and branch the message; make `ping()` check the sealed flag and report vault:sealed distinctly; add a sealed check to `khctl console` and to provision-operator's refusal.

### F2 — The printed unseal command doesn't exist on the box; no working unseal procedure anywhere in the kit
- **Scenario:** power cut on 07-29; containers auto-restart; vault comes back sealed. Whoever is standing there (possibly IT alone) follows the refusal message.
- **What happens now:** the launcher correctly detects sealed and prints "unseal with the custody shares (`bao operator unseal`)" (`deploy_launch.py:586-590`; apply variant `deploy_apply.py:351-354`). But no `bao` binary exists on the host — `install-ubuntu.sh` installs docker/minisign/venv/ollama only; the kit ships no CLI; OpenBao runs inside container `kh-openbao` with `ui = false` (`kit/openbao/config.hcl`), so there's no web fallback either. The working form (`docker exec -it kh-openbao bao operator unseal`, 3 of 5 shares) and where the shares live exist only in the operator's head. Client-later rider: `DEPLOY_NOTES.md:95-97` declares "an unseal runbook ships with every box" as an invariant, and the CLIENT ceremony text printed at init says "the runbook says this too" (`deploy_apply.py:136-140`) — the runbook does not exist.
- **Severity:** IMPORTANT · on-site-now (runbook invariant: client-later).
- **Fix direction:** make the refusal print the exact docker-exec command + "threshold 3 of 5, recorded at the custody ceremony"; or add a `khctl unseal` wrapper; write the one-page UNSEAL_RUNBOOK.md into BUNDLE_FILES.

### F3 — The wrong-kind credential logs in successfully; the console just shows dashes forever
- **Scenario:** phase_tenants prints TWO near-identical tokens back-to-back (agent serving, then operator console). Someone pastes the agent one at the lock screen — the naive first-operator mistake the ceremony invites.
- **What happens now:** login validates via `GET /v1/actions`, which returns 200 for ANY resolvable principal — empty catalog for agents (`operator_http.py:816-820, 264-268`); `unlock()` accepts any 200 (`app.js:97`). Every subsequent read 403s (`operator_http.py:875-876`) and app.js silently early-returns on non-200 (`app.js:115, 298, 339`). Result: an unlocked, permanently blank dashboard under "SYSTEM : NOMINAL".
- **The gap:** the console cannot distinguish "wrong token type" from "broken" from "slow" — live in front of the team. The operator would eventually open devtools; a client operator never would.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** refuse credentials whose actions catalog is empty ("valid, but has no console role — use the OPERATOR CONSOLE credential"); surface read-403s instead of swallowing them.

### F4 — A reachable-but-erroring backend looks alive: frozen numbers, ticking uptime, NOMINAL footer
- **Scenario:** mid-ingest, Postgres (or vault transport) degrades while the operator service stays up. The room is watching the monitor as the "live" view.
- **What happens now:** every backend exception becomes 500 `{'error':'internal'}` (`operator_http.py:783-788`). The offline banner fires only on a network-level fetch throw (`app.js:61-67`); a 500 is a successful fetch, so all three refreshes silently early-return, last-good numbers stay, the client-side uptime ticker keeps counting (`app.js:288-294`), footer stays NOMINAL. The `/v1/health` body that says `postgres:false` is fetched and discarded (health renders only inside `renderMonitor`, which never runs on non-200).
- **The gap:** stale data is indistinguishable from live data — the exact "is it broken or just slow?" moment with no answer on screen.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** treat consecutive non-200 polls as a degraded state (reuse the offline banner with different copy); render the health tile from `/v1/health` independently; add a "last updated HH:MM:SS" stamp that goes amber when polls stop succeeding.

### F5 — Failures are invisible where the UI says to look: error badge is structurally 0, alerts have no surface
- **Scenario:** real Diversified Botanics PDFs fail parsing during the replay (docling on messy files — the realistic case). The operator watches the console to know ingestion is healthy, sees the badge at 0, clicks "Errors & health".
- **What happens now:** failures nack back to `status='queued'` + `last_error` (`dispatch_pg.py:108-121`); no pipeline code ever writes `status='error'` (repo-wide grep: only benchmark.py's own table) — but the badge counts exactly that status (`operator_reads.py:169,176`; `app.js:194`), so it reads 0 while documents fail on every sweep. The tab opens "SURFACE : NOT YET WIRED" (`index.html:315-322`). The honest signal exists — `operator_alerts` view (`migrations/010_operator_write.sql:68-81`), `GET /v1/alerts`, `retry_failed_item`, `acknowledge_alert` (`operator_http.py:525-549, 821-826`) — and has NO consumer: app.js never calls it, deploy_cli has no alerts/retry subcommand.
- **The gap:** the one number labeled as the error count actively says "all fine" while things fail; listing/retrying a failed item from any surface is impossible without hand-rolled curl + bearer token, in front of the team.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** point the badge/monitor counts at the `operator_alerts` view (one-query change); cheapest insurance for 07-29 is `khctl alerts [--retry ...] [--ack ...]` over the existing endpoints — the full tab stays a Design follow-up.

### F6 — `khctl console`/`start_program` declare success after their own supervisor failed; the diagnostic terminal closes itself
- **Scenario:** the operator service can't come up (Postgres down after reboot, port clash). The operator double-clicks "Open Console".
- **What happens now:** `ensure_operator` returns False after 15s with a WARN naming the least-likely cause ("is migration 010 applied?", `deploy_launch.py:538-541`) — `_cmd_console` ignores the return value (`deploy_cli.py:345`) and unconditionally opens the browser + prints "browser opened" (`:379-381`) → connection-refused page. Same pattern in `start_program` (`deploy_launch.py:605-606, 625-636`: results discarded, "program is up" printed regardless). `console.sh:19` bare-`exec`s khctl, so the terminal carrying the WARN, the log path, and the login guidance closes (`Terminal=true`, window = process).
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** make ensure_* failures change the outcome — skip the browser open, print the log tail, suggest `docker compose ps` / `khctl verify`, hold the terminal open, return non-zero.

### F7 — "Progressing, stuck, or just not running?" is unanswerable — CLI batch sweeps under a UI branded LIVE
- **Scenario:** The operator drops new files into the watched folder mid-afternoon and watches the console. Nothing moves. Broken or slow?
- **What happens now:** ingestion advances only while `khctl ingest` runs (`deploy_launch.py:696-803`); launch runs exactly one sweep (`:611-616`). UI-triggered pulls are explicitly not built (`operator_http.py:53-56`). Meanwhile the console says "near-real-time · refreshed every 5s", "CAPTURE → FACTS : LIVE" (`index.html:126-128, 206`); a dead `--watch` loop leaves frozen queue depths and "backfill in progress" (a DB flag, `app.js:212-214`). `last_run_at` is served (`operator_reads.py:198`) and never rendered.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** render `last_run_at` per source + a header-level "last pipeline event N min ago"; change the backfill copy to "backfill incomplete — runs on next sweep" when no recent run.

### F8 — Per-source progress is decorative and double-counted
- **Scenario:** the replay registers several folders as separate sources; the room watches per-source progress during the backfill.
- **What happens now:** every same-system source row shows the SAME tenant-wide landed count (grouping by `source_system`, `operator_reads.py:145-148, 196`); `total` is always None (`:197`) so no N-of-M/rate/ETA exists; the in-progress bar renders at a fixed 60% for any unfinished backfill (`app.js:223`), jumping to 100% at the end. Acknowledged as an approximation in `UI_NOTES.md:28-31`.
- **Severity:** IMPORTANT · on-site-now (invites "is it stuck?" and "why do both sources say 812?" from the audience).
- **Fix direction:** make the bar indeterminate instead of fake-60%; label the count "tenant-wide for filesystem"; surface docs/min next to active sources. Cheap replay dodge: register ONE filesystem source.

### F9 — Decision feedback flashes for milliseconds; the undo instruction is never readable
- **Scenario:** The operator presses A to merge a pair in front of the team and looks for confirmation.
- **What happens now:** `decide()` shows "Merged into X … Press S to undo." then immediately refreshes; `renderDetail`'s first act is hiding the decision box (`app.js:404, 491-512`). On localhost the confirmation is visible for a blink. Failure messages, ironically, persist.
- **Severity:** IMPORTANT · on-site-now (this box exists to remove exactly the "did that register?" doubt).
- **Fix direction:** don't hide the box on re-render; clear it on tab switch or replace it on the next decision.

### F10 — Any unanticipated failure crashes apply with a raw Python traceback
- **Scenario:** first wet compose-up (the raft override's first live run, per `DEPLOY_NOTES.md:263-266`). `phase_services` waits for postgres only; openbao may still be binding when `phase_openbao` calls it.
- **What happens now:** `run_apply` catches only `ApplyError` (`deploy_apply.py:543-552`); `phase_openbao`'s first hvac call has no retry/wrap (`:321-323`; same class: `psycopg.connect` in phase_schema `:277`). A connection-refused propagates as an unhandled traceback through `guided_deploy` (`deploy_launch.py:408-415` — no try/except) — in the self-closing double-click window. The operator never sees "[FAIL] apply stopped at X — fix and re-run."
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** wait for the vault socket like the postgres retry loop; add a top-level except in run_apply printing the same fix-and-re-run footer for unexpected exceptions.

### F11 — One keystroke = one permanent verdict; no repeat-guard; undo covers only the last merge
- **Scenario:** all-day keyboard triage; a stray or auto-repeating key lands with the review tab focused.
- **What happens now:** A/R/S/space fire `decide()` on keydown with no confirm and no `e.repeat` filter (`app.js:575-583`) — a held key issues concurrent POSTs. Merge undo holds exactly one id (`app.js:38, 466-469`); keep-separate, quarantine resolve/dismiss, and flagged-resolve have no undo in UI or CLI — and R writes a `human_review` hard-negative flywheel label (`operator_http.py:39-44, 648-689`).
- **Severity:** IMPORTANT · on-site-now (mis-keys pollute training labels permanently).
- **Fix direction:** `e.repeat` guard + disable keys while a decide() is in flight (tiny); a short undo-grace for the other verdict types; merge-history split view stays the tracked follow-up.

### F12 — Flagged-document review is decide-blind and its only action mislabels what it does
- **Scenario:** a real document's declared data_track mismatches the sniffed one; the reviewer opens it to adjudicate which is right.
- **What happens now:** the detail pane shows no passage/preview (`operator_reads.py:536-558`); the UI copy says "A = resolve, tag stands corrected" (`app.js:396-398`) but A posts `{document_id}` only (`:459-461`) — `corrected_data_track`, the actual §8.1a human-tag-wins mechanism (`operator_http.py:452-474, 681-689`), is never offered. The doc silently re-queues under the ORIGINAL tag; correcting it requires hand-crafted curl.
- **The gap:** the one review type where human input changes downstream routing (data tracks are a platform law) can't take that input, while claiming it did — a correctness-of-record trust gap.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** render `declared_data_track` + a track picker feeding `corrected_data_track`; fix the two copy strings either way.

### F13 — `phase_models` never restarts ollama after the model-store copy, and the failure message lies
- **Scenario:** first wet model-store copy (an admitted Ubuntu-only untested item). Apply copies 56GB into `~/.ollama/models` while ollama runs, then immediately probes.
- **What happens now:** the code's own OK-line admits "restart ollama to pick up new manifests" (`deploy_apply.py:386-388`) but nothing restarts it; the probe (`:389-392`) then fails with "no kit ollama_models/ to load from; with egress: ollama pull …" (`:396-399`) — factually wrong (the store WAS copied) and offline-impossible. The actual fix (restart ollama, re-run) is stated nowhere.
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** restart/reload ollama after the copy and re-probe once; branch the message on whether a kit store was just copied.

### F14 — A half-finished deploy re-enters the launcher as "deployed"; Enter starts a program that can't work
- **Scenario:** apply stops at models or tenants ("fix and re-run — phases are idempotent"). The operator re-runs the launcher as told.
- **What happens now:** `classify_state` says DEPLOYED whenever plan + .env exist and postgres has a `schema_migrations` table (`deploy_launch.py:83-107`) — true from phase 5. The menu default (Enter) is "start the program" (`:437-444`); `start_program` checks vault + ollama reachability but not models-present or credentials (`:544-604`). The actual fix ("d") is third in the menu, never connected to "your apply failed midway" (`deploy_apply.py:547-552` names no menu choice).
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** track apply completion (phase ledger in WORK); when the last apply didn't finish, default to "resume the deploy" and name the failed phase.

### F15 — 54GB VRAM is a silent hard floor; the escape hatch is unreachable and offline-impossible
- **Scenario:** the replay box's GPU budget is one notch under 54GB (GPU inference on the target is an admitted never-tested item).
- **What happens now:** plan refuses (fp16_27b floor 54GB, `kit/profiles.toml:24-36`; `deploy_profiles.py:192-210`) and offers `--allow-gated-tier` (`:311-314`) — but `launch.sh:69` `exec`s without `"$@"` so no flag can pass through the SSD entry point, and the gated model (qwen3.6:27b-q8_0) isn't in the kit anyway (manifest pins only bge-m3 + bf16). No operator-facing doc states the floor.
- **Severity:** IMPORTANT · on-site-now (dead-ends the day if the hardware is short).
- **Fix direction:** verify the replay box's VRAM before 07-29 (runbook pre-flight line); append `"$@"` to launch.sh; consider carrying the q8_0 blobs as the offline fallback.

### F16 — The agent serving credential is print-once with NO re-mint path
- **Scenario:** The agents engineer needs to point agents at the serving API; the agent credential printed once during phase_tenants wasn't captured.
- **What happens now:** phase_tenants re-runs say "already bootstrapped" and never re-mint (`deploy_apply.py:477-481`); `provision-operator` mints ONLY operator/reviewer roles (`deploy_cli.py:615-617`; `deploy_apply.py:425-427`). Grep of `register_principal` callers: phase_tenants, checks.py smoketests, tests — no CLI, no documented recovery. Recovery = hand-writing hvac Python on-site.
- **Severity:** IMPORTANT · on-site-now (the operator credential has an issue-more path; the one external agents need has none).
- **Fix direction:** extend provision-operator (or add provision-agent) minting serving principals via the same registry path + ceremony.

### F17 — Standalone `khctl ingest` fails as a raw traceback with no pre-flight
- **Scenario:** the launcher itself says "ingest more: khctl ingest --tenant <t>" (`deploy_launch.py:634`). The operator runs it while the stack is partially down, IT watching.
- **What happens now:** `deploy_cli.main()` dispatches with no exception handling (`deploy_cli.py:623-624`); `run_ingest` wraps nothing; capture deliberately re-raises mid-pull errors (`capture.py:270-275`). Postgres-down = full psycopg traceback. The actionable [FAIL] pre-flight checks exist only on the launcher path (`deploy_launch.py:571-603`).
- **Severity:** IMPORTANT · on-site-now.
- **Fix direction:** wrap the dispatch (or run_ingest) to catch known infra failures with the launcher's [FAIL] language; reuse start_program's liveness checks as an ingest pre-flight.

### F18 — Fact-to-source trace dead-ends at numeric IDs through every built surface
- **Scenario:** during the demo, a teammate points at a served fact: "where did this come from?" The operator has one minute and no side doors.
- **What happens now:** a served FactEnvelope carries document_id/chunk_id/char-span only — no title, no passage (`serving.py:115-146, 163-213`). No built surface dereferences the IDs: the six serving ops include facts_citing (chunk→facts, the wrong direction) but no get_chunk/get_document (`operations.py:1103-1229`); the operator API has no document/chunk read (`operator_http.py:766-781`); the Facts & entities tab is a placeholder. `store.get_chunk` exists only on the internal store (`factstore_pg.py:631`) — reaching it means psql. The evidence→fact direction works (see checked-fine); fact→evidence does not.
- **Severity:** IMPORTANT · on-site-now (it is THE trust question, and the demo will surface it).
- **Fix direction:** register one small read op (`get_passage(chunk_id)` reusing `operator_reads._passage`, or `GET /v1/passages/<chunk_id>`); later, hyperlink IDs in the UI.

### F19 — The on-site game-plan runbook is still unwritten, ~4 days out
- **Scenario:** §8.9 Addendum 4 names its own "next artifact": the runbook — command sequence, pre-flight checklist, go/no-go gates, guardrails, Entra/connector steps.
- **What happens now:** it doesn't exist — progress doc ~line 1515 "To be written"; `NOTES.md` BP21: "not present in this workspace"; workspace-wide find for runbook/game-plan files: nothing. The synthetic/real tenant-naming landmine (bench-synth vs the real Diversified Botanics tenant), the 5 non-disruption guardrails, the secrets ceremony, and teardown live only in the operator's head.
- **Severity:** IMPORTANT · on-site-now — the single artifact that absorbs most of this list (B1's prereqs, B2's ceremony, F2's unseal command, F3's which-credential-is-which, F5's curl fallback, F15's VRAM check).
- **Fix direction:** write it before 07-29, one page per phase, with the fixes above folded in as checklist lines wherever the code fix doesn't land in time.

---

## LATER — real gaps, acceptable for 07-29

### On-site-relevant but tolerable (operator = the builder)

- **L1 · khctl not on PATH; every hint prints bare `khctl`; `launch.sh` forwards no flags.** khctl exists only in `~/knowledge-hub/.venv/bin` (`launch.sh:49-69`, no symlink/PATH setup anywhere); all printed hints fail as typed (`deploy_launch.py:618-619, 634-635`; `deploy_apply.py:503-505`), and `exec` without `"$@"` blocks every flag-requiring recovery through the SSD entry point (feeds F15). *Fix:* print full venv paths in hints or symlink at bootstrap; add `"$@"`.
- **L2 · Plain Enter at the tenant prompt deploys with zero tenants** — no operator credential is ever minted; the console can never be logged into; the hint says "re-plan adds them later" but no menu entry is named "re-plan", and the real path (d) currently walks into B3's clobber (`deploy_launch.py:382-386, 433-452`; `deploy_apply.py:455-457`). *Fix:* re-ask on empty input; print the exact recovery.
- **L3 · Long silent stretches read as hangs**: 2.9GB pip install runs `--quiet` (`launch.sh:58-65`); apply's phase_kit re-hashes the ~60GB kit with no ">5GB silence is work" notice (that notice exists only in verify-kit, `deploy_cli.py:273-280`) and even under `--dry-run` (`deploy_apply.py:166-180`). *Fix:* copy the notice into phase_kit; announce the model-store copy; drop `--quiet`.
- **L4 · Consent-pending / throttled connector states exist only in the design mock** — the built console knows active/disabled/degraded only (`operator_reads.py:140-144`); Graph sources aren't registry-runnable (`deploy_launch.py:660-661`), so during the on-site Entra/consent work the wall display shows nothing useful about the connector. *Fix for the week:* the runbook names the terminal as the connector status display; medium-term, let the connector set `consent_pending`/`throttled` as registry statuses (status_reason already renders).
- **L5 · The header search field is pure decoration** — a static styled div, no input, no handler (`index.html:57-60`), advertising exactly the lookup that doesn't exist (F18). Reads as "broken" on the most prominent row. *Fix:* hide it or give it the placeholders' NOT-YET-WIRED honesty.
- **L6 · The lock screen shows a dev note to everyone**: "screen styling pending Design — function first" (`index.html:352`) will be on screen during the demo. *Fix:* delete the line (one-line edit — do it before 07-29, it's free).
- **L7 · Lost operator token recovery is CLI-only and absent from the lock screen** — the recovery itself is real and signposted (`provision-operator`, `deploy_cli.py:387-422`; pointers at `deploy_apply.py:503-505` and `deploy_cli.py:347-351`), but the person staring at the lock screen is never told (`index.html:340-354`). Also `provision-operator` depends on `BAO_ROOT_TOKEN` in `.env` — which B3 can clobber, and which contradicts the "never on disk" comment at `deploy_cli.py:333-335`. *Fix:* one recovery sentence on the lock screen; fix the stale comment.

### Client-later (don't let these inflate the on-site list)

- **C1 · Lock screen is a dead end** — no "where do credentials come from / who to call" for a locked-out SME (`index.html:340-350`; `app.js:68, 98`).
- **C2 · Skip doesn't stick** — client-side rotation resets on every refresh; the least-confident (hardest) item returns as the very next item (`app.js:471-478, 297-311`; `operator_reads.py:348`).
- **C3 · Offline banner overpromises** — "Nothing you decided has been lost" while `decide()`'s catch silently drops the in-flight decision (`app.js:481-484`; `index.html:81`).
- **C4 · A poison document retries forever** — no attempts ceiling (`dispatch_pg.py:79-117`), ack doesn't stop redelivery (`operator_http.py:525-549`), nothing can dead-letter; under `--watch` it burns an LLM call per interval.
- **C5 · Dead console URL = bare browser error** — the UI is served by the down service itself (`operator_http.py:909-924`); nothing anywhere says "run Open Console again to restart it."
- **C6 · No list/revoke for credentials** — full khctl subcommand set has neither (`deploy_cli.py:471-621`); documented revocation is raw hvac surgery keyed by sha256(token-value) the operator doesn't hold (`OPERATOR_API_NOTES.md:147-150`; `choke_point.py:202-205`). "Who has access and how do I cut one off" is an audit question the appliance can't answer.
- **C7 · No logout** — token in sessionStorage auto-relogins; no control exists (grep of index.html+app.js), and it's not on the tracked follow-up list (`UI_NOTES.md:120-136`).
- **C8 · Adding a source later is tribal** — the routine watch-points hint omits `--add-source` (`deploy_launch.py:634`); the msgraph skip message says "its own runbook" without naming `CONNECTOR_NOTES.md` (`:660-661`); the Sources tab is a stub with an uncalled write op behind it.
- **C9 · Review score-bar color zones are decorative** — fixed CSS stops at 45%/72% (`index.html:245-247`) contradict the real t_high 0.95 policy; held pairs render green; t_low is served and never displayed.
- **C10 · Trust counters have no drill-down** — low-confidence/held are counted, never itemized anywhere (`operator_reads.py:98-108, 327-358`), and the tile's counting rule differs from the serving states (`operations.py:345-369`), so the numbers won't reconcile with what agents see.
- **C11 · Internal vocabulary ships raw** — grounding verdicts, uncertainty state names, resolver tier codes (t0/t1/t1b), quarantine reasons — no legend/tooltip in the product; explanations live only in dev notes.
- **C12 · Uncalibrated confidence served as a bare number** — the "never a probability" contract is docstring-only (`serving.py:29-32, 166-173`); nothing caller-visible carries the caveat — and the agent integrations are the first consumer.

---

## Known-open items — explicit verdicts

1. **Credential TTL/expiry (not built)** — **ACCEPTABLE for 07-29 (client-later requirement).** One tenant, own company, loopback console, working revocation-by-delete (tested BP19/20). Interaction with the demo: none possible by construction — bench dev keys live only in the pilot's ephemeral dev-mode vault and die with it (`deploy_cli.py:316-318`); the site vault initializes fresh. Becomes real for regulated clients (reviewer credentials that never age out). Already bookmarked in `OPERATOR_API_NOTES.md`.
2. **Lost-token recovery** — **ACCEPTABLE for the operator console token; NOT for the rest.** `provision-operator` re-mints and is signposted in two places (verified working path via `.env` root token). The unresolved parts are what bite: the **agent serving credential has no re-mint at all (F16)**, the **unseal shares have no recovery by design and no shipped runbook (B2/F2)**, and the lock screen never mentions the recovery (L7).
3. **Empty-queue / offline / login states (built minimal)** — **ACCEPTABLE.** All three are honest, calm, and functionally correct (`index.html:230-234, 79-82`; `app.js:68, 89-109`). Two blemishes: the visible dev-note line on the lock screen (L6 — delete before the demo) and the overpromising offline copy (C3).
4. **The six placeholder tabs** — **ACCEPTABLE except one.** Five are honestly labeled stubs whose day-one needs the monitor + CLI cover. **"Errors & health" is the exception (F5):** its badge is live-but-wrong (structurally 0) and the triage backend (`/v1/alerts`, retry, ack) has no consumer anywhere — when a real document fails mid-replay there is no surface to see or retry it. Cheapest pre-replay insurance is a `khctl alerts` subcommand, not the tab.
5. **System-fallback fonts** — **ACCEPTABLE (cosmetic, not trust-eroding).** But note: the declared fallbacks are Windows fonts (Segoe UI/Consolas/Georgia — `index.html:25, 40-41`), so stock Ubuntu lands on browser-generic DejaVu, and nobody has ever rendered this console on Ubuntu. Budget 2 minutes on-site to eyeball the 7px letterspaced labels; vendor the three OFL woff2s (already tracked, `UI_NOTES.md` follow-up 6) before any client demo.

---

## Checked and found genuinely fine

Verified working, not just claimed — so the reader knows these were looked at:

**Deploy & launch**
- Adoption gate explained in plain language at the prompt, safe default, no-change quit (`deploy_launch.py:168-226`). Plan pause is a real gate — nothing wet-applies until the operator types `deploy`; `rehearse` and `--dry-run` can never wet-apply (`:229-251, 401-423`).
- Anticipated (ApplyError) failures do say "[FAIL] apply stopped at X — fix and re-run (phases are idempotent)", and idempotency mechanisms check out (env backup-once, schema ledger, op markers) (`deploy_apply.py:547-552` etc.).
- The launcher is stateful with numbered steps (1/6…6/6) and explains resume behavior (`deploy_launch.py:309-336`). The SSD/kit stays read-only through a deploy; re-seeding never clobbers engagement artifacts (`:257-282`).
- Signature verification gates everything before install, unsigned kits refused, CRLF/exec-bit portability traps pre-empted in `write_ssd_root` (`deploy_kit.py:746-781`); verify-kit warns ">5GB hashing looks like a hang" (`deploy_cli.py:273-280`).
- The vault ROOT token does not depend on scrollback — persisted into `.env` at init (`deploy_apply.py:340-346`); only the 5 shares are truly print-once. Print-once blocks are clearly flagged at the moment of printing. Custody default for the appliance profile is operator, matching the decided table.
- `khctl verify` spins ephemeral in-process servers, so it doesn't falsely fail right after apply (`checks.py:372-462`); it is offered as THE diagnostic in the deployed menu and watch-points.

**Login & credentials**
- Wrong/mistyped token: clear human message, not raw JSON. Pasted whitespace is trimmed client AND server side (`app.js:564-567`; `operator_http.py:931`). Revoked token fails cleanly at login and mid-session. Browser refresh does not log the operator out (sessionStorage).
- Dev-mint structurally cannot fire on a deployed box (plan + non-pilot token gate, `deploy_cli.py:331-337`); the deployed console explicitly refuses to mint. Credential values genuinely never touch disk (sha256-keyed registry records only; kit no-secrets guard as second net).
- `provision-operator` for SME reviewers is real, custody-gated, print-once with the loss consequence said out loud; reviewer credentials land reviewer-scoped.
- `console.sh` on an undeployed box: clear message, points at the launcher, holds its window open.

**Daily run & failure containment**
- Empty review queue: designed honest state ("Nothing is waiting on a human"). Empty monitor states honest ("no sources registered yet", "quiet — nothing has moved yet"); every mock fake verified dead.
- Full network-level unreachability IS detected: calm offline banner, "SYSTEM : UNREACHABLE" footer, auto-recovery on the 5s poll (the gap is only the reachable-but-500 case, F4).
- A poison document cannot wedge a sweep (per-message nack + continue, `processing.py:105-121`, `extraction.py:147-166`); a source failure degrades only that source, renders as a blinking DEGRADED tag with the reason, self-heals, and has an in-UI Pause/Resume with visible role refusal ("operator role required").
- Review action FAILURES are surfaced with the server's reason and persist. Keyboard shortcuts can't fire while typing the credential or off the review tab. Server strings are escaped before innerHTML. "Item N of M" counts are real and consistent. The concurrent-polling DB-stranding bug was found live and structurally fixed with a regression test.
- Log locations for both services are printed at every relevant moment; the operator service refuses to start blind rather than serving a broken API (`operator_http.py:996-999`).

**Trust surfaces (the parts that work)**
- Merge review shows real source context: the passage with the mention highlighted, document title, chunk id (`operator_reads.py:494-507`; `app.js:416-440`). Quarantine detail gives decision-grade context (passage, extractor@version, raw LLM output) with a plain-language blurb.
- Evidence panels derive ONLY from recorded resolver features — never invented. Real policy thresholds shown with honest fallback copy.
- Every served envelope structurally requires the provenance spine (validated, extra=forbid); relevance≠truth is enforced by the type system; retracted facts never serve by default and are honestly labeled on request.
- The evidence→facts direction of tracing IS complete (facts_citing + retrieve enrich). The activity feed is sourced only from real state transitions. Every operator write — including refused ones — is audited with principal/action/target/snapshot-ref.
- Vault data and the stack survive a power cut (raft + named volume + restart policies) — the issue is only the sealed-state UX (F1/F2), not durability.
- The shipped 0.25.0 kit is the code reviewed: spot-hashed files byte-identical between dev tree and kit; migrations 001–010, operator_ui, and the full notes set physically present on the SSD.
