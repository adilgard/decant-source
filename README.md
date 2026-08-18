# decant.Source (dS) — Knowledge Hub

The data-ingestion flow of the Knowledge Hub: capture → process → extract →
resolve → serve, deployed as a local, self-hosted appliance. The human-facing
name is **decant.Source**; the code deliberately keeps the working name
(`knowledge_hub` package, `khctl` CLI, `KH_*` env vars, schema names).

Everything runs on your own hardware: Postgres 16 (pgvector + pg_trgm),
SeaweedFS for object storage, OpenBao for secrets, Ollama (bge-m3) for
embeddings. No hosted services.

## Repo map

| Path | What it is |
|---|---|
| `knowledge_hub_pkg/` | The Python package (`knowledge_hub`), its tests, and per-subsystem `*_NOTES.md` design docs |
| `migrations/` | SQL migrations — `models.py` stays in lock-step with `knowledge_hub_baseline_schema.sql` + these |
| `docker-compose.yml` | The pilot stack: Postgres, SeaweedFS, OpenBao (dev mode) |
| `docker-compose.openbao-prod.yml` | Raft-backed OpenBao for real boxes |
| `openbao/`, `postgres/`, `seaweedfs/` | Service configs the compose files mount |
| `profiles.toml` | Deploy presets read by `khctl plan` |
| `install-ubuntu.sh` | Host prep for the offline SSD-kit deploy |
| `RUNBOOK_headless_ubuntu_deploy.md` | The on-site deploy, step by step, including the secrets ceremony |
| `REFCARD_credentials.md` | The four print-once secrets and how not to lose them |
| `REFCARD_vault_unseal.md` | Unsealing OpenBao after a reboot (routine, not a recovery) |
| `DEPLOY_NOTES.md`, `NOTES.md` | Design decisions and open items |
| `SANITY_CHECK_FINDINGS.md` | User-readiness red-team findings (BP24) |
| `design/` | Operator console HTML mocks |
| `hooks/` | Tracked git hooks — the commit-time secret guard. Needs a one-time install, see Quickstart |
| `.kit_manifests/` | Signed image/package pin manifests per kit version |

## Quickstart (local pilot)

```bash
git config core.hooksPath hooks   # once per clone: the secret guard
cp .env.example .env
docker compose up -d
py -3.12 -m venv .venv
.venv/Scripts/pip install -e knowledge_hub_pkg
.venv/Scripts/khctl --help
```

Run the tests:

```bash
.venv/Scripts/python -m pytest knowledge_hub_pkg/tests -x -q
```

## Secrets policy

No real credential ever enters this repo. `.env` and `.env.deploy` are
gitignored; `.env.example` carries throwaway pilot defaults only. Real
deployments mint credentials during the deploy ceremony and keep them in
OpenBao — see `REFCARD_credentials.md`. The kit-signing secret key lives
offline and is never in the repo or the kit.

A pre-commit hook enforces this at the moment it matters. It refuses any
staged file whose NAME is secret-shaped, using the SAME definition the kit
gate uses (`knowledge_hub/secret_names.py`), so the two can never drift.
Install it once per clone:

```bash
git config core.hooksPath hooks
```

This repo is public, so a committed credential is world-readable history:
fixing it means ROTATING the credential, not reverting the commit. That is
why the guard stops you before the commit rather than after.

Three tracked files deliberately wear a secret-shaped name and carry no
values (`.env.example`, `.secrets.local.example.json`,
`seaweedfs/s3config.json`). They are listed by exact path in
`COMMIT_ALLOWLIST` in that same module. If you add another placeholder file,
add it there too. `git commit --no-verify` still bypasses the hook on
purpose — it is git's escape hatch, and the goal is to stop the accident,
not the deliberate act.

## Version

Package `0.28.0` — see `knowledge_hub_pkg/pyproject.toml` and
`.kit_manifests/` for what shipped when.
