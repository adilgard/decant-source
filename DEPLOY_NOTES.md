# DEPLOY_NOTES — khctl, the kit, and the open deployment decisions

*Companion to §8.9 of the master progress doc. Code: `deploy_probe.py`,
`deploy_profiles.py`, `deploy_cli.py` (console script `khctl`); presets:
`profiles.toml` (kit data).*

## The khctl contract

```
khctl probe    read-only sweep of the environment   -> probe_report.json
khctl plan     preset x probe x operator choices    -> deploy_plan.json + .env.deploy
khctl apply    execute the plan                     (STUB — phases printed)
khctl verify   prove the plan's claims live         (STUB — refactor below)

khctl migrations status        ledger vs database, per migration (READ-ONLY)
khctl migrations mark-applied  record DDL that reached the DB out-of-band
```

`probe_report.json` + `deploy_plan.json` + `.env.deploy` are the **engagement
record** — what we found, what we decided, what we wired. They contain
endpoints and bootstrap credentials, so they live with the deployment, not in
the carry-kit.

Design rules encoded in `deploy_profiles.py`:

- **Probe recommends, operator confirms.** When a profile allows
  `ours|theirs` and a *qualified* client-side candidate exists, `plan`
  refuses to guess and demands `--use seam=…`. Adopting a client's Postgres
  is a commitment, not a default.
- **Qualification is fail-closed.** A rule the probe couldn't evaluate
  (e.g. object-lock on a store with no bucket to inspect) FAILS. An operator
  override records itself + the failed rules in the plan; verify still has
  to prove the component live.
- **No silent extraction downgrade.** The tier ladder picks the best fitting
  tier; a `gated` (quantized) tier needs `--allow-gated-tier`; nothing fits →
  the Scenario-2 fork is surfaced as an error with the three options.
- **Secrets are never "theirs"** — see the note in profiles.toml (S2
  principal-registry isolation).

## DONE (2026-08-03) — two unbounded-wait bugs, found by running the full suite

Both surfaced while getting a clean full-suite result after the ledger work
below. Neither was caused by it. Together they made a full run take hours and
then hang; fixed, the suite is **563 passed in 5:06**.

**1. `ollama.Client` had no HTTP timeout.** The library defaults to
`httpx.Timeout(None)` — unbounded on connect AND read. A run wedged for 45
minutes on two ESTABLISHED connections to 127.0.0.1:11434 with zero bytes and
zero CPU on either side: a read that would never return and never give up.
`khctl` had already bounded every `psycopg.connect` after the same class of
hang (fe30871) and every `urllib.request.urlopen` passes a timeout — the
inference seam was the one that got missed.

New `knowledge_hub/ollama_client.py` is the ONLY place a client is built
(`make_ollama_client(host)`); eight call sites across seven modules went
through it, and four dead lazy `import ollama` lines went with them. Two
budgets, because the failure modes are unrelated:
`ollama_connect_timeout_s = 5.0` (a listener that accepts and never answers is
the dual-stack ::1 black hole, not slow work — fail fast so the next address
family gets its turn) and `ollama_read_timeout_s = 600.0` (a 36B MoE
legitimately generates for minutes; the goal is BOUNDED, not fast). Read from
settings per call, not captured at import, so a deployment's tuning survives
`reload_settings()`.

The guard is an AST walk over the package, not a grep, so neither
`ollama.Client(...)` nor `from ollama import Client` can slip past — without it
an eighth unbounded client is one convenient line away. It also asserts that
upstream STILL defaults to an unbounded read, so if that ever changes the test
says the factory's rationale moved instead of quietly passing.

**2. `reload_settings()` reset what it could not reload.** It called
`Settings()` unconditionally, so running it from a directory with no `.env`
silently reverted EVERY field to its class default. For a deployed process that
means swapping the real config for the PILOT credentials and `localhost`. A
missing `.env` is now a no-op with a warning; it also takes an explicit path and
returns what it read.

It was doing real damage in the suite. Three tests in `test_deploy_launch.py`
restored themselves with a bare `reload_settings()` while still chdir'd into a
temp deployment home (`run_launch` pins CWD there, and the `_restore_cwd`
fixture only unwinds at teardown — after the test body's `finally`). From that
point the whole process was on `localhost` instead of the `.env`'s pinned
`127.0.0.1`, and every later test paid Docker Desktop's ~10s dual-stack stall
per fresh connection. One test took **160s in that state versus 0.78s clean**.

Their restore assertions passed the entire time because they checked
`s3_access_key`, whose `.env` value (`local_dev_only_s3_admin`) is identical
to its class default — the one field that could not reveal the problem.
They now restore from a NAMED file and assert on `postgres_host`, which is the
field that differs. `tests/test_bounded_io.py` documents that blind spot
explicitly, so the next person does not re-learn it.

**Run pytest from the INFRA ROOT.** From `knowledge_hub_pkg/` there is no
`.env`, so settings fall back to `localhost` and the same stall applies to the
whole run.

## DONE (2026-08-03) — the migration ledger gets a real model (`migrations.py`)

Found on the pilot DB: migrations 011/012/013 had every object present and
**no `schema_migrations` row**. Their DDL had been executed out-of-band (psql,
not `khctl apply`), and nothing in the codebase could see it.

Why it was invisible, and what each fix closes:

| Blind spot | Fix |
|---|---|
| `phase_schema` trusted the ledger alone, so it would replay 011 onto its own tables and die on a raw psycopg `DuplicateTable`, aborting the phase with 012/013 unreached | classifies every file against the ledger **and** the live objects first; any disagreement raises `ApplyError` with the remediation |
| `khctl` could not answer "what is applied?" at all — two progress docs disagreed with no way to settle it | `khctl migrations status`, read-only (`default_transaction_read_only=on`), exit 1 on drift |
| `stack_alive` asked only whether the ledger TABLE existed, so a stack with an empty ledger read as fully deployed | requires a **non-empty** ledger |
| `khctl verify` proved the schema was PRESENT, never that the ledger was HONEST about it (`check_postgres` asserts `ontology_active` exists — it did) | `checks.check_migrations`, selected whenever the plan has a postgres seam |
| the launcher would happily start ingest onto a drifted schema | `start_program` gates on the ledger before anything starts (seam: `LaunchConfig.ledger_check`, so the gate is testable dry) |

`knowledge_hub/migrations.py` is the single owner. States: `APPLIED`,
`PENDING`, `APPLIED?` (ledger row only), and three BROKEN kinds —
`objects-without-ledger`, `ledger-without-objects`, `partial`.

**Verification is by name, and says what it does not cover.** Each file is
parsed for the tables / indexes / views it creates plus the columns it adds,
and those names are looked up in the live catalog. Two rules keep it honest:

- **Objects an earlier file created are not evidence about this file.** The
  baseline creates `review_queue`; 001/003/004 each `CREATE OR REPLACE` it. So
  a file's verifiable set excludes everything the baseline and every earlier
  file create — otherwise a deploy that died mid-replay would read as drift
  instead of a resumable stop.
- **Columns count**, because 007 only redefines 006's view: its two
  `ADD COLUMN`s on `benchmark_runs` are the sole evidence it ran, and they
  carry no `IF NOT EXISTS`, so replaying an applied 007 fails exactly like a
  duplicate `CREATE TABLE`.

Not verified by name: `ADD CONSTRAINT` (003 has the only one) and
functions/triggers (this set has none). Both are reported per file as a
coverage note rather than implied away. `test_every_bundled_migration_has_
something_verifiable` fails if a future migration lands with nothing unique
to check.

The ledger table gained a nullable `note` column, added by `ensure_ledger`'s
idempotent DDL rather than a numbered migration — the ledger is the migration
system's own bookkeeping, and a migration that repairs the ledger could not
run on a database whose ledger is the broken thing.

**The repair, 2026-08-03:** `khctl migrations mark-applied` recorded 011/012/013
with `--observed-at 2026-08-03T21:27:07.336715+00:00` (from
`ontology_active.activated_at`, the 011 seed row that timestamps the same
contiguous `pg_class` oid burst 231976–232027 that created all three) and a
`--note` saying it was a backfill. A note is **mandatory**: a backfilled row
that looks identical to a replayed one is what let this hide. `mark-applied`
refuses any file whose objects are not verifiably all present — a pending
migration must be replayed, and a half-applied one is a schema repair no
ledger row can stand in for. Result: 13 applied, 0 pending, and no data row
touched.

## DONE (2026-07-24) — checks.py refactor + `khctl verify`

One library, two runners — landed as planned (pkg 0.13.0):

- `knowledge_hub/checks.py`: every check_stack body extracted verbatim as
  `check_*(targets…) -> detail str` (raises on failure); `run_check()`
  wraps into `CheckResult`. Targets default to settings — post-apply, the
  rendered `.env` IS the plan's config, so defaults are already plan-aware.
- `check_stack.py` = the thin pilot runner. Zero behavior change, proven by
  identical 10/10 output before/after (`_version_triple`/`_assert_versions`
  kept importable for test_service_http.py).
- `khctl verify` = the plan-driven runner (`verify_checks_for(plan)` in
  deploy_cli.py, selection unit-tested): DB-backed checks only when the
  plan has a postgres seam (a hosted connector-agent footprint skips them);
  local inference → the model checks; remote → `check_remote_inference`
  (reachability + required models + honest TLS report; auth/TLS hardening
  arrives with §8.9 item 2); `local-external` → `check_local_external_
  inference` (BP46 Fix 5: same reachability proof, plus a proof the endpoint
  is ON this box, and it reports the deploy as on-premises rather than
  borrowing the remote path's off-premises wording). Version integrity FIRST
  in both runners.
- `check_side_doors` (§8.8 NEGATIVE half, every visit): pg_stat_activity
  must show no client-backend connections under non-allowlisted users, else
  isolation is void. **Rewritten 2026-08-07 — it had never been able to
  fail.** Its allowlist defaulted to the DSN's own username, and the whole
  stack connected as that one bootstrap superuser, so every connection was
  allowlisted. It now allowlists the four least-privilege roles (roles.py:
  `kh_pipeline` / `kh_serving` / `kh_operator` / `kh_report`), refuses when
  those roles are not provisioned at all, and reports a connection still on
  the bootstrap account as its own finding class (split exists, consumer
  hasn't adopted it) distinct from an unknown user (a genuine side door).
  Verified failing in both states, not just passing in the good one.
- NEW `check_usage_attribution` (§8.8 POSITIVE half, every visit): serves a
  record through the real sink and finds it again BY principal_id. side
  doors proves nothing unauthorized is connected; this proves the consumers
  that should be reading through ops actually are. Either half alone is a
  partial answer that reads like a whole one.
- Caveat to remember on adopted client stores: `check_s3_worm`'s
  `verify_worm()` writes a sacrificial object into THEIR bucket — agree
  with the client first; it is the one verify step that writes.
- First live field run: 11/11 green on the pilot box (appliance plan).

## DECIDED (2026-07-24) — OpenBao production bootstrap + unseal-key custody

The pilot runs `server -dev`: **in-memory**, auto-unsealed, root token. A
real walk-in cannot ship this — a restart vaporizes every tenant credential.
Apply phase 4 stands up production mode: **raft** storage (single node to
start; no extra service; snapshots = the backup story — snapshots stay
encrypted under the seal, safe to store anywhere), `bao operator init`,
then the custody ceremony below. Root token is used ONLY for bootstrap
(mount KV v2, write policies, mint per-tenant + serving principals), then
revoked; day-2 ops use a scoped admin token. Bootstrap script = part of
apply, idempotent.

**Decision (operator): custody is a per-offering default recorded in the deploy
plan** (`secrets_custody` on deploy_plan.json — plan-level, since hosted
plans have no client-side secrets seam yet custody still needs recording).
Defaults live in profiles.toml; `khctl plan --custody X` is the
per-engagement dial and records itself as an override.

| Offering   | Default    | Ceremony |
|------------|------------|----------|
| appliance  | `operator` | init → 5 shares / threshold 3 → shares into OUR kit custody (password manager, never on the box), test unseal, root token revoked. Every restart = our unseal (remote API or site visit). Buyers bought uptime; client custody is the offered security upgrade. |
| client-gpu | `client`   | init on install day → 5 shares / threshold 3 printed → sealed envelopes to their IT/security officer, WE RETAIN ZERO → client performs the test unseal themselves → runbook handed over. Strongest premises-local story. |
| hosted     | `auto`     | KMS auto-unseal on our infra; unattended reboots. Shape-B-appropriate only — on Shape A it undermines local-first (a TPM-bound local variant is a future option, trading stolen-box protection for uptime). |

Custody honesty (sell it right): shares protect **data at rest** — stolen
box, decommissioned drive. They do NOT protect the client from the box's
operator at runtime. Provable, not promised — never imply otherwise.

Shamir is tunable per engagement, not binary: e.g. 6 shares / threshold 3
split 3-and-3 (either party unseals alone) vs 2-and-2 / threshold 3 (joint
ceremony required). Variations beyond the defaults go in the plan record.

Invariants regardless of mode: an unseal runbook ships with every box,
including the shares-lost worst case (NO recovery — re-init + re-enter every
source credential), so nobody discovers that property during an outage.

**Custody as actually set for the first on-site validation run (2026-07-28):**
`operator`, matching the appliance default. The operator holds all five shares in a
KeePassXC vault on the ROG laptop, never on the deploy box (same pattern as the
BP28.3 rehearsal). This is the right fit while the run's data is discarded
(see the tenant-id note below on why validation runs are disposable).
**Production custody is NOT decided** and must not inherit this by default:
the real choice is operator vs client sealed-envelope (we retain zero) vs a
split ceremony requiring both parties, per the table above. Flagged to the
client's IT lead in the on-site brief so the decision is made deliberately
rather than discovered during an outage.

## DECIDED (2026-07-24) — model transport (apply phase 5, the air-gap path)

**Decision (operator): carry the Ollama model store on the kit SSD** — option (a)
below. Pin the Ollama version in the kit, hash-verify the store like any
other artifact, re-validate the layout on every Ollama version bump. The
original options analysis, kept for the record:

`ollama pull` needs egress; the kit must carry the models (tens of GB — the
reason the kit medium is an SSD). Two candidate mechanisms:

- **(a) Carry the Ollama model store** — copy a canonical `~/.ollama/models`
  (blobs + manifests) from the build machine; apply rsyncs it into place (or
  sets `OLLAMA_MODELS` at the kit path) before starting Ollama. Simple,
  proven by inspection; couples us to Ollama's on-disk layout (stable in
  practice, unversioned in theory).
- **(b) `ollama create` from Modelfiles + carried GGUF/safetensors blobs** —
  uses only public interface; but re-imports on every install (slow at
  tens of GB) and the Modelfile must reproduce the exact quantization/
  template of the registry models (drift risk vs `ollama show`).

Lean (a): pin the Ollama version in the kit, hash-verify the store like any
other artifact, and re-validate layout on each Ollama version bump. Either
way the kit manifest lists model blobs individually (they dominate kit size
and hash-verify time).

## DONE (2026-07-24) — `khctl make-kit` / `verify-kit` + THE KIT LAYOUT CONTRACT

`deploy_kit.py` builds the kit; the layout is DERIVED from what
`deploy_apply` reads (producer/consumer symmetry — if apply changes where
it looks, both change in the same commit). **The contract:**

```
<kit>/                            (= apply's --kit AND --infra-dir)
  manifest.json                   apply phase_kit: sha256 per artifact
  manifest.json.minisig           signature, verified when present
  docker-compose.yml              phase_services (base) — a DERIVED copy:
                                  stage_bundle strips `build:` keys, the kit
                                  runs LOADED images only (BP30/BP28 #10)
  docker-compose.openbao-prod.yml phase_services (secrets=ours override)
  openbao/config.hcl              mounted by the override
  postgres/init/00-extensions.sql compose bind-mount target (first-run init;
                                  BP30). seaweedfs/s3config.json does NOT
                                  ship — phase_services renders it on site
                                  from .env, before compose up (BP30/#19)
  knowledge_hub_baseline_schema.sql + migrations/*.sql   phase_schema
  profiles.toml                   khctl plan on site
  check_stack.py                  the pilot gate, runnable on the box
  install-ubuntu.sh + requirements*.txt                  bootstrap
  knowledge_hub_pkg/              SOURCE — installed EDITABLE on site
                                  (version_triple reads its pyproject.toml;
                                  a bare wheel install breaks the
                                  version-integrity check)
  python/cpython-3.12-linux-x86_64.tar.gz   BP46 Fix 3: the interpreter the
                                  wheelhouse was built for. launch.sh
                                  extracts it into WORK/.python3.12 and
                                  creates the venv with it, so the deploy
                                  does not depend on the host's system
                                  python (24.04 ships 3.12, 26.04 ships
                                  3.14 with no 3.12 available — and 26.04
                                  is likely what makes the Strix Halo GPU
                                  work, so downgrading the OS was never the
                                  answer). It stays on the box: the venv
                                  points at it by absolute path
  wheelhouse/                     linux dep wheels + requirements-linux.txt
                                  (resolved linux-natively inside a
                                  python:3.12-slim container; direct deps
                                  pinned at pilot versions, transitives
                                  locked by the wheelhouse itself — see the
                                  Build Prompt 18 Part A findings)
  images/*.tar                    docker save, loaded by phase_services
  ollama_models/{manifests,blobs} copied by phase_models to where THIS box's
                                  ollama reads: systemd service →
                                  /usr/share/ollama/.ollama/models (owned
                                  ollama:ollama), else ~/.ollama/models
                                  (BP30/#18); a retry skips a present store;
                                  blobs content-addressed, hash-verified at
                                  BUILD time
  tokenizer/bge-m3/tokenizer.json seeded into the deployment home by khctl
                                  launch; chunking token-counts offline —
                                  the runtime HF download is gone (BP30/#20)
```

Seeding (khctl launch step 2) PRESERVES files already present in the
deployment home — a field repair is never reverted by a re-run; delete the
deployed copy to re-seed it (BP30/BP28 #11).

Discipline: fail-closed (missing pinned model / blob hash mismatch /
un-saveable image stops the build); idempotent (re-runs skip content-
addressed blobs already copied — verified live: 4 copied → 0 copied);
**no secrets** (bundle is an explicit ALLOWLIST; a second-net guard fails
the build on anything env/plan/usage/key-shaped; tested with salted build
folders). Pins recorded in the manifest: package version, image IDs
(sha256 — THAT is the pin, not the :latest tag), ollama version, per-model
file counts + bytes. Skipped components are recorded, not silent.

`khctl verify-kit` = the arrival gate, stricter than apply's phase 0:
no manifest = not a kit; hash mismatch = refusal; **files not listed in
the manifest = chain-of-custody failure** (catches planted artifacts);
plus the no-secrets guard. Proven live: tampered model blob → REFUSED,
restored → clean.

## DONE (2026-07-24) — kit signing: minisign, trust anchor, enforced verification

**The security spine.** The secret signing key is the root of trust —
whoever holds it can sign a kit the verifier accepts. Signing proves a kit
came from a holder of that key, untampered; it does NOT protect against a
compromised key. **The secret key's custody IS the security.** It never
enters the repo, a kit, a session/chat environment, or a log.

**Trust anchor**: `deploy_kit.TRUSTED_PUBKEYS` — a VERSIONED SET of public
keys embedded in the verifier (and mirrored in install-ubuntu.sh, kept in
lock-step). verify-kit checks the manifest signature against THESE keys,
never a key found inside the kit — a repacked kit can carry any pubkey it
likes (the swapped-pubkey attack; tested + demonstrated live: refused).

**Order of trust** (verify-kit / apply phase 0): (1) signature against the
embedded anchor — an unverified manifest is attacker-controlled data;
(2) only then the hashes inside it; (3) the whole-tree audit.

**Enforcement**: make-kit REQUIRES a signing key (`--sign-key` or
`KH_SIGN_KEY`); verify-kit and install-ubuntu.sh REFUSE unsigned kits.
The only escape is the self-recording `--allow-unsigned` /
`KH_ALLOW_UNSIGNED=1` override — dev bench only, recorded in the manifest
and the output, NEVER a client kit. make-kit also self-verifies its own
signature against the trusted set, so signing with a stray key dies at the
build bench, not a client site.

### THE ORG KEY CEREMONY — **DONE 2026-07-24**

Performed by the operator in an interactive shell (never an assistant session):
`org-2026` minted with a passphrase-protected secret key held in a secure
folder outside the repo, offline backup on the operator. Public key wired
into BOTH anchors (deploy_kit.TRUSTED_PUBKEYS + install-ubuntu.sh);
`dev-2026-07` RETIRED — proven live: the dev-signed staged kit is now
refused and must be re-signed with org-2026 (interactive, passphrase):

```powershell
$env:KH_SIGN_KEY = "C:\Users\<you>\Secure\kh-org-2026.key"
khctl make-kit --out <kit> --models bge-m3 --skip wheelhouse
```

For reference, the ceremony as performed (for future rotations):

```bash
minisign -G -p kh-org-2026.pub -s kh-org-2026.key
```

- You will be prompted for a **passphrase** — use a strong one; it
  encrypts the secret key at rest (signing then prompts interactively).
- **Secret key custody**: `kh-org-2026.key` lives passphrase-protected
  with a secure offline backup (and/or in OpenBao under an operator-only
  path). Never committed, never kitted, never pasted into a session/log.
- **Public key** (the `RW…` line in the `.pub`) is safe to distribute:
  add it to `TRUSTED_PUBKEYS` in deploy_kit.py AND install-ubuntu.sh
  (lock-step), retire superseded ids.
- Sign builds with `KH_SIGN_KEY=/path/to/key khctl make-kit …`
  (interactive terminal — the passphrase prompt is the point).

### Key rotation (designed now, one key shipped)

The anchor is a set keyed by id (`org-2026`, …). Rotate by: mint new pair
→ ADD new pubkey to both anchors → ship khctl → re-sign kits with the new
key → REMOVE the old id once no kit signed by it remains in the field.
Retire immediately (skip the overlap) if a key is suspected compromised —
every kit signed by it must be rebuilt and re-verified.

### Validation (throwaway TEST keys only — the org key is never in a session)

Live + in tests (49 deploy tests): signed happy path (no override needed);
tampered manifest byte → signature dies FIRST (hashes never consulted);
valid signature from an untrusted key → refused; **swapped-pubkey repack
(attacker's pubkey inside the kit, internally consistent hashes, valid
attacker signature) → refused**; unsigned → refused at build and at
arrival without the recorded override; the secret key matches the
no-secrets guard (`*.key`) and cannot be packed into a kit.

## DONE (2026-07-24) — `khctl apply` (install-ubuntu.sh, ported plan-driven)

`deploy_apply.py`: nine idempotent phases, apply STOPS at first failure
(later phases depend on earlier — unlike verify, which always reports all).
`--dry-run` walks every phase with resolved values = the walk-in rehearsal.

- Kit hash verification (manifest.json sha256; minisign when present;
  kit-less = honest "DEV install" line, tamper = hard refusal).
- Schema+migrations now replay over psycopg against the plan's DSN — works
  on an adopted client Postgres too (the bash version could only
  docker-exec into our own container). search_path gotcha preserved.
- Production OpenBao: `openbao/config.hcl` (raft, TLS pending §8.9 item 2)
  + `docker-compose.openbao-prod.yml` override, used whenever secrets=ours.
  phase_openbao: init (5/3) -> custody ceremony printed ONCE (shares never
  written to disk) -> unseal -> KV v2 mount -> .env root-token patch;
  idempotent when initialized+unsealed; SEALED vault = refusal with the
  unseal instruction. State machine proven live against a throwaway
  production vault (init / idempotent-rerun / sealed-after-restart), file
  backend; the raft override itself gets its first live run on the Ubuntu
  replay. Day-2 hardening bookmarked: scoped admin token + root revocation.
- Model store from kit `ollama_models/` (copy into ~/.ollama/models);
  kit-less falls back to verifying required models already served.
- Tenant bootstrap: mint bearer -> register serving principal ->
  vault marker (idempotent); credential printed once, never persisted
  in the clear.

## DONE (2026-07-24) — `khctl launch` + THE SSD LAYOUT CONTRACT (Build Prompt 18)

The SSD is exactly two things at the root, and nothing else:

```
<SSD>/
  launch.sh                 thin wrapper (generated by `khctl make-ssd`)
  Knowledge Hub.desktop     Ubuntu double-click entry (Terminal=true)
  kit/                      the signed kit from `khctl make-kit --out <SSD>/kit`
```

**Two places, one contract** (`deploy_launch.py`, seeded lists come from
`deploy_kit.BUNDLE_FILES/BUNDLE_DIRS` — producer/consumer symmetry):

- **KIT (the SSD)** is read-only. The launcher never writes into it, so the
  SSD stays re-verifiable (`verify-kit` green before AND after a deploy)
  and reusable at the next site.
- **WORK (the box)** is the deployment home — `$KH_WORK_DIR` or
  `~/knowledge-hub`. It holds the venv, the package source (copied off the
  SSD so the editable install survives unplugging), the bundle files apply
  reads (seeded from the kit), the engagement record (probe_report.json,
  deploy_plan.json, .env.deploy), the installed .env, and serving.log.
  Engagement artifacts live with the deployment, never in the carry-kit
  (the kit's no-secrets guard enforces this by refusing them).

**launch.sh is thin by design**: (1) manifest signature against the trust
anchor — RENDERED from `deploy_kit.TRUSTED_PUBKEYS` at make-ssd time, so
this third anchor copy cannot drift (install-ubuntu.sh remains the one
hand-synced copy); an unsigned kit is refused with NO override — the dev
escape hatch deliberately does not exist on the SSD; (2) offline venv
bootstrap from `kit/wheelhouse` (the wheelhouse now carries hatchling +
editables so the on-site editable install needs no index); (3)
`exec khctl launch`. Box prereqs remain OS-level: docker + compose, minisign,
tar, ollama (native) — the kit is offline for python deps, images, and models,
not for apt packages. **python is no longer a prereq (BP46 Fix 3):** the kit
carries a portable CPython 3.12 and bootstraps the venv with it, which is what
makes the launcher run on Ubuntu 26.04. A kit built `--skip python` falls back
to a host python3.12 and refuses honestly if the host has none — the
wheelhouse is cp312-only, so 3.13/3.14 cannot install it.

**`khctl launch` is stateful** (state = artifacts in WORK + a live-stack
probe; classification unit-tested):

- fresh/probed/planned → the guided deploy, orchestrating the real
  subcommands in order: `verify-kit` (full arrival gate) → seed WORK →
  `probe` (report shown, pause) → **THE ADOPTION GATE** → `plan` →
  **THE PLAN PAUSE** → `apply` → `verify`.
- deployed → menu: start the decant.Source (dS) ingestion program (compose up when the
  plan has ours-services, liveness checks incl. SEALED-vault refusal with
  the unseal instruction, serving via `python -m knowledge_hub.service_http`
  supervised to /v1/health, the OPERATOR CONSOLE via `python -m
  knowledge_hub.operator_http` supervised to its /v1/health — BP22: on-site
  the operator watches + resolves at http://127.0.0.1:8081/ui/, so the
  deployed-state launch is not complete until /ui/ answers — one `khctl
  ingest` sweep, watch-points printed with the console first)
  / re-verify / repair.

**The gates are the §8.9-Addendum-4 guardrail in code**, proven live on the
pilot box (its own Postgres played the "client" Postgres):

- ADOPTION GATE: every reachable Postgres/object-store candidate is
  surfaced LOUDLY. On an `ours|theirs` seam (client-gpu) the operator MUST
  choose; plain Enter = self-contained, recorded as an explicit
  `--use seam=ours` override; adoption requires typing the endpoint.
  On a pinned-ours seam (appliance) it prints the non-disruption notice —
  "detected, will NOT be touched" — with no prompt. Quitting changes
  nothing.
- PLAN PAUSE: after the plan renders, nothing executes until the operator
  types `deploy` (or `rehearse` for apply --dry-run; a `--dry-run` launch
  session can never wet-apply, even on `deploy`).

**`khctl ingest`** is the dS ingestion program itself: for each tenant,
sweep registered sources (`SourceRegistry.list_for_tenant`) through
capture → processing.consume → extraction.consume → resolution.sweep —
the exact build_corpus.py wiring, registry-driven. `--add-source
<ref>=<folder>` registers a filesystem watch folder (config-merging, so
declared data_track/structured_map survive); `--watch` loops. Sources
whose adapter needs more than the registry carries (msgraph-*) are skipped
with an honest pointer to their runbook. Proven live: SOP-DEMO-001.md →
1 landed → 4 embedded chunks → extraction → 3 entities → 2 facts promoted;
re-sweep = all zeros (idempotent).

Wet-vs-rehearsed honesty: the guided flow ran on the pilot box with the
staged signed kit through apply **--dry-run** (all 9 phases resolved) and
the deployed path ran WET against the live pilot stack via a theirs-seam
plan (no compose churn on the pilot project). The first wet compose-up +
model-store copy from THIS launcher happens on the Ubuntu replay, same as
apply itself.

## DONE (2026-07-24) — FIRST FULL-SCALE KIT BUILD (Build Prompt 18 Part A)

The walk-in kit for the ~07-29 on-site: **57 GB**, built to
`KH_SSD_STAGING\kit` in **13m08s** (artifact stages; signing pending —
see final mile below). Contents: 179-wheel linux wheelhouse (2.89 GB),
3 image tars (0.63 GB), bge-m3 (1.16 GB) + **qwen3.6:27b-bf16 (55.58 GB)**
blobs hash-verified, 290 files through the no-secrets guard.

**What the first full-scale run surfaced (all fixed in deploy_kit.py):**

1. **A Windows `pip freeze` is NOT a linux lockfile.** Three distinct
   failures, in order: sdist-only pins (`antlr4-python3-runtime==4.9.3`,
   `pylatexenc==2.10` publish no wheels — `pip download --platform`
   refuses sdists); build-machine marker evaluation (pip pulled `pywin32`
   via dlt on the Windows resolver and would have silently OMITTED torch's
   linux-only nvidia/triton wheels); and outright **ResolutionImpossible**
   on linux (`fsspec==2026.6.0` vs dlt/datasets/torch/huggingface-hub) —
   meaning `install-ubuntu.sh`'s `pip install -r requirements.lock.txt`
   would have died AT THE CLIENT SITE. Fix: `stage_wheelhouse` now runs
   inside `python:3.12-slim` (linux-native resolution; docker was already
   a make-kit prereq), wheel-builds the sdist-only pins first (universal-
   wheel assertion, fail-closed) and satisfies them via --find-links, and
   `build_linux_requirements()` pins DIRECT deps (requirements.txt +
   pyproject + torch/torchvision pin-through) at pilot-validated versions
   while transitives re-resolve linux-natively — the wheelhouse itself,
   hash-pinned by the manifest, is the effective lock. The wheelhouse now
   carries hatchling+editables so the on-site editable install works
   offline.
2. **`qwen3.6:latest` ≠ "27B FP16".** The pilot's starter tag is a 36B
   MoE at Q4_K_M (23 GB). The kit model is `qwen3.6:27b-bf16` (55 GB pull,
   ~8 min at 115 MB/s). profiles.toml tiers now name EXACT registry tags
   (fp16_27b → qwen3.6:27b-bf16; quant_27b → qwen3.6:27b-q8_0, floor 30),
   and render_env writes ADJUDICATION_MODEL alongside EXTRACTION_MODEL so
   adjudication can't point at a tag the kit doesn't carry. **Honest
   smoke result on 27b-bf16** (check_extraction, machinery green): the
   p2 contract CONFORMS and adjudication works (conf 0.95), but the smoke
   doc yielded 0 facts / 1 quarantined at 32.4s vs :latest's 1 fact /
   0 quarantined at 15.5s — single-doc anecdote, NOT a quality verdict;
   Axis D owns the model call, and extractor_version re-extracts cleanly
   on digest change.
3. **Air-gap proven pre-Ubuntu:** a `--network none` python:3.12-slim
   container installed the FULL wheelhouse + the editable package from the
   kit in 93s — `import knowledge_hub` (0.21.0), `torch 2.13.0+cu130`,
   and `khctl` all working on linux with zero egress. Plus a real
   `docker load` from a kit tar. What only Ubuntu can still prove: GNOME
   double-click ("Allow Launching"), wet compose-up with the raft
   override, the model-store copy onto a fresh box, and GPU inference on
   the target hardware.
4. Arrival-gate fail-closed re-proven on this kit: pre-signature, the
   launcher REFUSES it at step 1 and stops.

**SIGNED + VERIFIED (operator, interactively, 2026-07-24):** org-2026
signature, all 290 artifacts hash-green, no unlisted files, no secrets.
The launcher round-trip then re-ran against the SIGNED kit end to end:
arrival gate → adoption gate (pilot PG detected, self-contained chosen) →
plan (qwen3.6:27b-bf16 rendered) → apply --dry-run through all 9 phases
(291 kit artifacts re-verified inside phase 0). Finding #5, caught at
this gate: `verify_kit_manifest` used `read_bytes()` — the 55GB blob
paged a 64GB box into an apparent hang. Now `sha256_stream()` everywhere
(one implementation, deploy_apply owns it) + verify-kit prints a
"hashing NGB — silence is work" notice on kits >5GB.

**Remaining (operator):** copy `KH_SSD_STAGING\` (launch.sh +
Knowledge Hub.desktop + kit\) to the physical SSD root when it arrives,
then `khctl verify-kit --kit <SSD>\kit` as the final chain-of-custody
check — the round-trip re-verifies because the launcher never writes
into the kit. For future rebuilds, the signed-build command is:

```powershell
cd "C:\Users\<you>\Documents\Documents Workspace\decant-source"
$env:KH_SIGN_KEY = "C:\Users\<you>\Secure\kh-org-2026.key"
.venv\Scripts\khctl.exe make-kit --out "..\KH_SSD_STAGING\kit" --infra-dir . --models "bge-m3,qwen3.6:27b-bf16"
```

## Not yet wired (deliberate)
- Placement is recorded (`single_box`) but not yet acted on — multi-box
  (§8.3 four-role topology) needs per-role hosts in the plan and compose
  overrides per host; the schema field exists so that is additive.
- apply's wet phases (compose up with the raft override, model-store copy
  into a fresh box) run their first full pass on the Ubuntu replay — the
  pilot box demo was --dry-run + the throwaway-vault state machine + a
  real `docker load` from the built kit.
- Kit wet-vs-rehearsed: NOTHING rehearsed-only remains at kit level —
  the full-scale build (wheelhouse incl. torch+cu130 closure, 55.58GB
  qwen3.6:27b-bf16 store) ran REAL on 2026-07-24 (see the Build Prompt 18
  Part A section above). Outstanding: the one interactive org-2026
  signing re-run, and the Ubuntu-only wet items listed there.
- Hosted (Shape B) plans render but items 2–5 of §8.9's net-new list
  (inference auth/TLS/broker/tenancy hardening) are not started — Shape A
  first, per sequencing.

## Build Prompt 25 — on-site hardening (deploy side), 2026-07-26, pkg 0.26.0

The BP24 sanity check found the deploy path could actively mislead a
careful operator. The code-fixable findings are CLOSED here (full list +
scenarios: `SANITY_CHECK_FINDINGS.md`; per-fix tests:
`tests/test_onsite_hardening.py`):

- **B3 (root-token clobber).** `phase_env` now PRESERVES a deployed home's
  non-pilot `BAO_ROOT_TOKEN` — the plan's pilot placeholder
  (`deploy_apply.PILOT_PLACEHOLDER_TOKEN`, single source; deploy_cli
  references it) never overwrites a live vault's token, on disk or in the
  in-memory env later phases read. `run_apply` additionally catches hvac
  auth failures with a message pointing at `.env.bak` instead of a raw
  traceback. Repair ("d") and re-plan are now actually safe to follow.
- **B2 (print-once secrets).** `confirm_recorded()` gates every print-once
  block — the 5 unseal shares + root token (phase_openbao) and every
  credential ceremony (`_print_once_credential`) hold for a typed
  `RECORDED` when stdin is a tty. `launch.sh` no longer `exec`s away its
  own hold-open: khctl runs, then the window waits for Enter (matching
  console.sh), so nothing print-once can vanish with a self-closing
  terminal.
- **B1 (offline prereq wall).** `launch.sh` checks docker / minisign / tar /
  ollama UP FRONT with an offline-honest refusal pointing at **PREREQS.txt**,
  which `make-ssd` now writes at the SSD root (the four host packages +
  "install BEFORE going offline" + the working unseal command). BP46 Fix 3
  removed python from that wall: the check was `python3.12` + `import
  ensurepip`, and on Ubuntu 26.04 it refused a box that was otherwise
  deployable. The kit ships the interpreter instead. No in-kit failure hint
  prescribes `apt-get`/egress anymore; `phase_preflight`'s docker message
  points at PREREQS.txt too.
- **F13 (model-store copy).** `phase_models` announces the multi-GB copy,
  RESTARTS ollama afterwards (`systemctl`, then passwordless-sudo
  fallback) and re-probes; on failure the message names the real cause
  ("restart ollama and re-run this phase") — the old text claimed "no kit
  ollama_models/" right after copying one, and prescribed an egress-only
  fix.
- **F14 (half-applied ≠ deployed).** `run_apply` writes a phase ledger
  (`.apply_progress.json` in the deployment home; wet runs only; added to
  the kit's FORBIDDEN_NAMES second net). The launcher's state detection
  reads it: an apply that died mid-phase re-enters the GUIDED flow with
  the failed phase named — plain Enter can no longer start a box with no
  models or credentials. `start_program` also live-checks that the
  required models are actually SERVED (not merely that ollama answers).
- **F6 (false success).** `start_program` and `khctl console` honor their
  supervisors: if serving/operator never came healthy, they print [FAIL] +
  the log paths and never claim success or open a browser onto
  connection-refused.
- **F1/F2 (sealed vault).** Every sealed-vault refusal now names the
  post-reboot custody state as the cause (not "bad credential"), and every
  printed unseal instruction uses the form that exists on the box:
  `deploy_apply.UNSEAL_COMMAND` = `docker exec -it kh-openbao bao operator
  unseal` (3 of 5 shares). `khctl console` and both provisioning commands
  check sealed FIRST and refuse distinctly (a sealed vault is not a
  custody refusal). Where the shares live remains runbook material.
- **F17 (day-2 ingest).** `khctl ingest` runs `ingest_preflight` (postgres
  + ollama, the launcher's liveness checks) and wraps the sweep so known
  infra failures answer in [FAIL] language; real bugs still propagate
  loudly.
- **F16 (agent credential re-mint).** `khctl provision-agent --tenant <t>`
  — same registry path + print-once ceremony as the bootstrap mint
  (`provision_agent_credential`, now also used by phase_tenants), custody
  gated. See OPERATOR_API_NOTES.md.
- **F5 (failure surface).** `khctl alerts [--retry queue:id] [--ack
  queue:id]` — the first consumer of `/v1/alerts` + retry/ack. Auth =
  the operator credential (KH_OPERATOR_TOKEN or hidden prompt).
- **L1/L2/L3.** `launch.sh` passes `"$@"` through to `khctl launch`
  (recovery flags reachable from the SSD entry point) and symlinks
  `~/.local/bin/khctl` (printed hints work as typed; `khctl_hint()` prints
  the venv path in launcher hints as the belt). The tenant prompt re-asks
  on empty input (zero-tenant deploys take an explicit `none`; recovery
  spelled out). `phase_kit` prints the ">5GB — silence is work" notice
  before re-hashing; the pip install is no longer `--quiet`.

Left for the runbook (deliberate): the shares-lost worst case, WHERE the
custody shares live, the on-site game plan itself (F19), and box
pre-provisioning. F15's VRAM pre-flight is an operational check; the
`--allow-gated-tier` escape hatch is now reachable through launch.sh.

## Build Prompt 30 — BP28 fix pack 1/3 (packaging, deploy mechanics, offline, hardening), 2026-07-26, pkg 0.26.1

The BP28 WSL2 rehearsal came back RED: kit 0.26.0 could not deploy on a
clean box. Root cause across this cluster: the pilot is a dev box — the kit
must ship what a CLEAN box needs, and each phase must VERIFY readiness
rather than trust `compose up`'s exit code. Fix pack 1 of 3 (BP31 =
production OpenBao, BP32 = console UX); all three land, then ONE rebuild +
re-sign as 0.26.1.

- **#10 — deploy compose + `--build`.** `stage_bundle` now strips `build:`
  keys from the staged compose files (fail-closed on a multi-line `build:`
  mapping it cannot strip) and `phase_services` no longer passes `--build`
  — on site every image arrives via `docker load`. The compose in the repo
  keeps `build: ./postgres` for the pilot bench; the kit copy is DERIVED.
  `postgres/init/00-extensions.sql` now ships (it is a compose bind-mount
  target; without it first-run init never creates the extensions).
  `seed_work_dir` PRESERVES files already present in the deployment home —
  a launch.sh re-run never reverts a field repair.
- **#11 — PREREQS.** Five host packages now (zstd added — Ollama's
  installer aborts without it on stock Ubuntu 24.04), plus a headless-box
  note: openssh-server (mandatory, the SSH-tunnel console) and rsync
  (optional, kit copy progress).
- **#18 — model store.** `phase_models` detects a systemd ollama
  (`systemctl is-active/is-enabled`) and installs the store to
  `/usr/share/ollama/.ollama/models` with `sudo cp -a -n` +
  `chown -R ollama:ollama` (NEVER `cp -al` — hardlinks make the chown
  corrupt the kit copy). A store already present (same names + sizes,
  content-addressed) skips the ~57GB copy entirely on retry. Known limit:
  a unit overriding OLLAMA_MODELS in its Environment is not honored —
  acceptable for the appliance profile.
- **#19 — s3config.json + the silent fatal.** `phase_services` renders
  `seaweedfs/s3config.json` from the .env credential pair BEFORE compose
  up (and repairs the docker-created directory poison state). THE
  STRUCTURAL FIX: `_await_service_ready` — after compose up, every planned
  service must answer its real client-path probe (postgres/psycopg,
  seaweedfs/boto3 list_buckets, openbao/sys-health reachability) within
  60s, and a container with RestartCount >= 2 fails IMMEDIATELY as
  restart-looping, naming the container and the `docker logs` command. A
  service that starts and dies can no longer sail through nine OK phases.
- **#20 — offline leak.** New `stage_tokenizer` ships
  `tokenizer/bge-m3/tokenizer.json` (resolved from the bench HF cache,
  smoke-loaded before shipping, pinned in the manifest); `khctl launch`
  seeds it into the deployment home; `chunking._bge_m3_token_counter`
  loads the local file (`config.bge_m3_tokenizer_json`) and only falls
  back to the hub on a dev bench. The rehearsal's entire measured egress
  (+6.46MB) is gone. Egress-blocked watch item for the re-rehearse:
  `parsing_docling.py`'s bare `DocumentConverter()` (no artifacts_path).
- **#21 — hardening (approved).** `render_env` mints a random S3 pair per
  deploy (`kh-s3-<hex>` / 48-hex secret); `phase_env` preserves a DEPLOYED
  box's pair over the fresh mint, B3-style (SeaweedFS reads s3config only
  at container start — rotation is a day-2 op, never a re-plan side
  effect); `khctl probe` also tries the deployed `.env` pair so probe-
  over-live stays truthful. `s3config.json` added to FORBIDDEN_NAMES (the
  committed pilot copy can never ride a kit). All compose `ports:` now
  bind `127.0.0.1` — the SSH-tunnel access model reaches everything via
  localhost; container-internal listeners stay 0.0.0.0 (compose-network
  traffic, not published).

Upgrade-in-place caveats for a 0.26.0 box: its static S3 creds are
preserved as "live" (rotation benefits fresh deploys), and the store
copied to $HOME by 0.26.0 stays as dead weight while the systemd copy
runs once. Version bumped to 0.26.1 (pyproject + __init__ together;
re-run `pip install -e knowledge_hub_pkg`). Runbook §5b items 1/3/4 are
expected obsolete at the 0.26.1 re-rehearse; item 2 (raft volume chown)
is BP31's.

## Build Prompt 31 — BP28 fix pack 2/3 (the production OpenBao path), 2026-07-26, pkg 0.26.1

Five of BP28's eleven defects lived in the production vault path — a
subsystem that had NEVER executed (the pilot runs dev mode: in-memory,
auto-unsealed, placeholder token). This pack makes production raft
init/unseal actually work, and makes FAILURE guidance safe: the most
dangerous defects here didn't stop the deploy, they misled an operator
during an outage. Tests: `tests/test_onsite_hardening.py` §BP31. Version
stays 0.26.1 (one rebuild + re-sign after BP32).

- **#13 — raft volume root-owned → crash-loop (Blocking).** A fresh named
  volume mounts at `/openbao/data` ROOT-owned; the image's stock
  entrypoint chowns its standard dirs but the raft path is not one of
  them, so the vault can't write `vault.db` and crash-loops forever. The
  prod override now starts the container as root, chowns the raft path,
  and hands off to the stock entrypoint — which itself drops privileges
  (su-exec), so the vault PROCESS never runs as root. **Wet-proven on
  this bench** (scratch compose project, real openbao 2.6.1 raft):
  first-try start, `drwxr-xr-x openbao openbao /openbao/data`, process
  table shows `bao` running as `openbao`.
- **#12 — no readiness wait + wrong exception match.** `_await_vault_ready`
  gives the vault the same discipline Postgres always had: poll the
  unauthenticated health endpoint up to 60s (with a "waiting for vault…"
  notice and the phase_services restart-loop tripwire), so a slow raft
  vault produces retries, never the three bare tracebacks BP28 observed —
  `requests.exceptions.ConnectionError` is not an hvac type and sailed
  past the old `type(e).__module__ == "hvac"` handler.
- **#16 — write before raft leader → HTTP 500.** `_await_vault_leader`
  blocks between "unsealed" and the first write (KV mount): polls
  `sys/leader` up to 60s until `is_self`/`leader_address` answers. Runs
  on BOTH branches (fresh init and idempotent re-entry — a vault
  unsealed by hand seconds ago is exactly the one still electing).
- **#15 — the vault-bricking advice (most dangerous).** The old hvac
  catch-all reported EVERY vault error as a token mismatch and advised
  restoring `BAO_ROOT_TOKEN` from `.env.bak` — on a mere leader-election
  500 that replaces the freshly-minted token with the pilot placeholder,
  converting a wait-and-retry into an UNRECOVERABLE vault.
  `classify_vault_error` + `vault_failure_advice` now answer per class:
  connection → wait/inspect; 503 VaultDown → sealed-is-routine + the
  working unseal command; 500 → leader election, wait; each explicitly
  says "NOT a token problem — do not change BAO_ROOT_TOKEN". Only a
  genuinely-REJECTED authenticated token (Forbidden/Unauthorized) gets
  token guidance, and even then the handler READS `.env.bak` first: a
  placeholder bak gets an explicit "do NOT restore"; a real bak gets
  "verify against custody records first". No code path writes `.env`
  or advises anything that can destroy a working root token.
- **#17 — the documented unseal command could not work.** `UNSEAL_COMMAND`
  (single source: deploy_apply; deploy_cli/deploy_launch import it,
  PREREQS.txt renders it, app.js's lock-screen message updated to match)
  now carries `-e BAO_ADDR=http://127.0.0.1:8200` — the `bao` CLI
  defaults to HTTPS while the production listener is plain HTTP.
  **Wet-proven:** the old form fails verbatim (`server gave HTTP response
  to HTTPS client`), the new form unseals 3-of-5.

**The reboot-recovery loop, wet-proven end to end on this bench** (the
real `phase_openbao` against a real raft vault, scratch project, port
18200): init cleanly on a fresh volume → container restart → raft
persistence intact, back correctly SEALED → unsealed with the EXACT
printed command → serves as leader → idempotent re-run reports
"already initialized + unsealed", waits for the leader, finds the KV
mount. The bench is Docker-on-WSL2 — same engine family as the BP28
rehearsal; the true gate remains the 0.26.1 re-rehearse on a fresh
distro.

## Build Prompt 43: the extractor is backend-dependent (2026-07-28, docs only)

The Strix Halo inference spike (node-a, 2026-07-28; record:
`.Handoff Docs/strix-halo-inference-spike-2026-07-28.md`) inverted two
assumptions and made the extraction model a function of the deployment
path. No code changed in this prompt; this section corrects the prose
record and pins what the deploy design now has evidence for.

**Model per path (the correction).** Any earlier statement in these notes
that reads "the kit model is qwen3.6:27b-bf16" is true of the NVIDIA kit
only, not of the platform:

- **NVIDIA appliance path (CUDA):** dense `qwen3.6:27b-bf16`, as built and
  signed into the 0.26.x kits. Unchanged.
- **AMD Strix Halo path (ROCm):** MoE `qwen3.6:35b-a3b-q4_K_M`, served by
  Ollama's bundled `rocm_v7_2` (no system ROCm install, nothing built from
  source). Reason: a dense 27B is bandwidth-bound on Strix Halo's ~256 GB/s
  unified memory (ceiling near 15 tok/s; 12.7 measured), the MoE measured
  58.8 tok/s. Single-sample numbers, but the bandwidth ceiling is
  arithmetic: dense models of any real size will be bandwidth-limited on
  this hardware regardless of backend. MoE is the model class for Strix
  Halo. This is a durable planning fact for hardware selection, not a tuning
  observation.

**Per-fact provenance is unaffected and already correct.** The envelope's
`extractor` / `extractor_version` record whichever model actually produced
each fact. Facts from an AMD box carry the MoE q4 identity; that is the
system working as designed, not drift to fix.

**Backend selection is now EVIDENCED, not assumed.** The three-way shape:

1. **CUDA + bf16 dense** (NVIDIA appliance / client GPU).
2. **ROCm + q4 MoE** (AMD Strix Halo), with **Vulkan as a documented,
   validated fallback** (works, lower prefill, 62.4 GiB visible-memory
   ceiling vs ROCm's 118.8 GiB on the 121 GiB box).
3. **Remote** (Shape B, inference over the network).

The probe design should let Ollama's own discovery pick the backend, then
**VERIFY the runner landed on a GPU library rather than CPU** (`ollama ps`
must say 100% GPU): the silent-CPU-fallback trap did not fire in the spike,
but nothing in the stack shouts if it ever does. Note the current code gap,
unchanged by this prompt: `deploy_probe.probe_gpu()` shells out to
`nvidia-smi` only, so an AMD box probes as `gpu: NONE` today even while
Ollama serves the iGPU at 100% GPU. Likewise `profiles.toml`'s tier ladder
(`fp16_27b` / `quant_27b`) names NVIDIA-path dense tags only; an AMD/MoE
tier is future code work.

**Both of those gaps are CLOSED IN CODE by BP46 (2026-07-29, pkg 0.27.0) and
are UNVERIFIED on AMD hardware until a deploy on node-a.** `probe_gpu()` now
falls back to amdgpu sysfs (`/sys/class/drm/card*/device/mem_info_*`, with
rocm-smi for product names when installed) and reports a tier BUDGET that
counts the GTT/shared pool on a unified-memory box — without that, node-a's
2 GB dedicated carve-out fails every tier floor on a box that measurably runs
a 23 GB model. `profiles.toml` gained `moe_35b_a3b_q4`
(`qwen3.6:35b-a3b-q4_K_M`, 26 GB floor, `memory = "unified"`); the dense
tiers are `memory = "dedicated"`, which is what keeps the CUDA ladder
unchanged. See §8.28 of the progress doc for the AMD-box checkpoint that
turns "written" into "proven".

**Honesty boundary.** The spike measured throughput, GPU engagement, and
JSON well-formedness. It did NOT measure extraction quality against ground
truth, so no claim of quality equivalence between the paths is made
anywhere in these notes. The prefill advantage that favored ROCm was
measured on a one-sentence prompt; real-document prefill (the dominant cost
for extraction) is untested.

**DECIDED 2026-07-28 (BP44): Option A, same architecture on both paths.**
MoE bf16 on NVIDIA + the same MoE q4 on AMD, so cross-path quality becomes
a one-variable (precision) question. Deploy-side consequences, recorded
here and not yet executed:

- NVIDIA appliance model changes from the proven dense `qwen3.6:27b-bf16`
  to **`qwen3.6:35b-a3b-bf16`** (71 GB on the registry, verified
  2026-07-28; vs 55.58 GB for the dense blob, so the kit grows ~15 GB and
  the pull happens at the next kit build).
- The signed 0.26.x kits still carry the dense model and remain the
  rehearsed, deployable artifact until that build; nothing on the SSD
  changes retroactively.
- `profiles.toml`'s tier ladder (`fp16_27b` / `quant_27b`, dense tags) must
  be reworked to the MoE tags, alongside the AMD tier: code work, not this
  prompt.
- The MoE bf16 needs its own extraction-contract validation on the NVIDIA
  box when it is built (p2 is proven on the dense model only; see
  EXTRACTION_NOTES on the reification mandate).
- Quality equivalence between the paths remains UNVALIDATED until Axis D
  benchmarks it; Option A makes that a clean measurement, it does not
  prove parity.

Full decision record: progress doc §8.26d; contract-side consequences:
`knowledge_hub_pkg/EXTRACTION_NOTES.md`.

**Outstanding on the AMD path:** node-b has nothing installed (Ollama, Mesa
Vulkan packages, group membership, the systemd drop-in all need
replicating); the spike's systemd drop-in on node-a still carries
`OLLAMA_VULKAN=1` (vestigial under ROCm selection) and `OLLAMA_DEBUG=1`
(diagnosis only); `OLLAMA_CONTEXT_LENGTH=8192` is a deliberate pin (the
131072 default burned 4 GiB of KV cache on a 1B model).

## Tenant id: disposable on validation runs, one-way on the real one (2026-07-28)

Recorded because the cost profile is invisible from the code and the wrong
read of it wastes a meeting. `tenant_id` is a plain `TEXT NOT NULL` column
(no FK, no enum) on ~20 tables across migrations 001 to 010, so a rename
looks like ~20 `UPDATE`s. **That is the misleading part.** The column is the
cheap half; three other places bake the id in:

1. **The immutable raw store keys by it.** `rawstore_s3._key_for` writes
   `<tenant_id>/<hash[:2]>/<content_hash>`, so the tenant id is a KEY PREFIX
   on the WORM bucket. S3 keys cannot be renamed, only copied, the bucket
   runs versioning + object lock specifically so those versions resist
   deletion, and `documents.raw_uri` is version-pinned to the old path. A
   DB-only rename therefore leaves every fact's provenance pointing at
   objects under the previous prefix.
2. **Vault source credentials** live at `tenants/<tenant_id>/sources/<ref>`
   (`secrets_openbao._path_for`), so a rename means re-entering each
   source secret (e.g. the Graph client secret).
3. **Bootstrap markers** `kh/bootstrap/tenants/<t>` and
   `kh/bootstrap/operators/<t>` (`deploy_apply`) make a renamed tenant look
   un-bootstrapped: the next apply mints a NEW operator credential, while
   the old one still resolves to a principal scoped to the old tenant and is
   effectively dead.

**Operating rule, two cases:**

- **Validation / rehearsal runs (including the first on-site deploy):** the
  data exists only to prove the pipeline works end to end and is thrown away
  after. The tenant id is a throwaway label, any sensible value works, and
  the reset is a full teardown (runbook §9, `down -v` across BOTH compose
  files) rather than a rename. Do not spend meeting time on the name.
- **The production deploy, whose facts are kept and feed downstream
  consumers:** the tenant id is effectively a ONE-WAY DOOR from the first
  ingest, for the reasons above. Use the production name from the start.
  Renaming later is not a migration script, it is a re-ingest: the raw bytes
  have to be re-landed under the new prefix and the old undeletable objects
  stay behind as garbage.

Neither case is a code limitation worth fixing today. The bucket-per-tenant
swap point (`rawstore_s3._bucket_for`) is where a future physical-isolation
model would change this, and it would inherit the same rule.
