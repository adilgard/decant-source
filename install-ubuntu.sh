#!/usr/bin/env bash
# Knowledge Hub — pilot replay script for the Ubuntu 24.04 workstations.
# Run from inside this bundle folder:  bash install-ubuntu.sh
# Idempotent-ish: safe to re-run; each phase checks before doing.
set -euo pipefail

echo "== Knowledge Hub pilot setup (Ubuntu) =="

# --- 0a. Kit trust anchor (keep in LOCK-STEP with deploy_kit.TRUSTED_PUBKEYS)
# The verifier's OWN keys — never a pubkey read from the kit being verified
# (a repacked kit can carry any pubkey it likes). This script arrived with
# khctl through the trusted channel; that is what makes these keys the anchor.
# org-2026 (minted 2026-07-24, DEPLOY_NOTES ceremony); dev-2026-07 RETIRED.
TRUSTED_PUBKEYS=(
  "RWS6/dyR5MslCZKw8pvhLnz3IIIPuXG7mh/IJDSPNUSkhLlr2BH88feP"  # org-2026
)
if [ -f manifest.json ]; then
  if [ ! -f manifest.json.minisig ] && [ "${KH_ALLOW_UNSIGNED:-0}" != "1" ]; then
    echo "!! kit manifest is UNSIGNED — refusing (KH_ALLOW_UNSIGNED=1 for a dev bench)"
    exit 1
  fi
  if [ -f manifest.json.minisig ]; then
    command -v minisign >/dev/null 2>&1 || {
      echo "!! signed kit but minisign missing: sudo apt-get install -y minisign"; exit 1; }
    verified=0
    for pk in "${TRUSTED_PUBKEYS[@]}"; do
      if minisign -Vm manifest.json -P "$pk" >/dev/null 2>&1; then verified=1; break; fi
    done
    if [ "$verified" != "1" ]; then
      echo "!! manifest signature matches NO trusted key — tampered or re-signed; REFUSING"
      exit 1
    fi
    echo "-- kit manifest signature verified against the embedded trust anchor"
  fi
fi

# --- 0. Prereqs: Docker Engine + compose plugin -----------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "-- Installing Docker Engine (official convenience script)"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "!! Log out/in (or 'newgrp docker') so your user can run docker, then re-run this script."
  exit 0
fi

# --- 1. Services: Postgres(+pgvector+AGE) / SeaweedFS / OpenBao -------------
echo "-- Building + starting services (docker compose)"
docker compose up -d --build
echo "-- Waiting for Postgres to be healthy..."
until docker exec kh-postgres pg_isready -U kh -d knowledge_hub >/dev/null 2>&1; do sleep 2; done

# --- 2. Schema (only if not applied yet) -------------------------------------
TABLES=$(docker exec kh-postgres psql -U kh -d knowledge_hub -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname='public'")
if [ "${TABLES}" -lt 5 ]; then
  echo "-- Applying baseline schema"
  docker cp knowledge_hub_baseline_schema.sql kh-postgres:/tmp/schema.sql
  docker exec kh-postgres psql -U kh -d knowledge_hub -v ON_ERROR_STOP=1 -f /tmp/schema.sql
else
  echo "-- Schema already applied (${TABLES} public tables), skipping"
fi

# --- 2b. Migrations on top of the baseline (idempotent, tracked) -------------
docker exec kh-postgres psql -U kh -d knowledge_hub -v ON_ERROR_STOP=1 -qc \
  "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
for f in migrations/*.sql; do
  name=$(basename "$f")
  applied=$(docker exec kh-postgres psql -U kh -d knowledge_hub -tAc \
    "SELECT count(*) FROM schema_migrations WHERE filename='${name}'")
  if [ "${applied}" = "0" ]; then
    echo "-- Applying migration ${name}"
    docker cp "$f" kh-postgres:/tmp/migration.sql
    docker exec kh-postgres psql -U kh -d knowledge_hub -v ON_ERROR_STOP=1 -f /tmp/migration.sql
    docker exec kh-postgres psql -U kh -d knowledge_hub -v ON_ERROR_STOP=1 -qc \
      "INSERT INTO schema_migrations (filename) VALUES ('${name}')"
  else
    echo "-- Migration ${name} already applied, skipping"
  fi
done

# --- 3. Ollama (native, GPU) --------------------------------------------------
if ! command -v ollama >/dev/null 2>&1; then
  echo "-- Installing Ollama (native)"
  curl -fsSL https://ollama.com/install.sh | sh
fi
echo "-- Pulling models"
ollama pull bge-m3
ollama pull qwen3.6   # starter extraction model (benchmark finalizes later)

# --- 4. Python 3.12 venv + deps ----------------------------------------------
if [ ! -d .venv ]; then
  echo "-- Creating Python 3.12 venv"
  # Ubuntu 24.04 ships python3.12; ensure venv module is present
  sudo apt-get update -qq && sudo apt-get install -y -qq python3.12-venv
  python3.12 -m venv .venv
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
if [ -f requirements.lock.txt ]; then
  echo "-- Installing pinned deps (requirements.lock.txt)"
  pip install --quiet -r requirements.lock.txt
else
  echo "-- Installing deps (requirements.txt)"
  pip install --quiet -r requirements.txt
fi
pip install --quiet -e ./knowledge_hub_pkg

# --- 5. End-to-end check ------------------------------------------------------
echo "-- Running stack check"
python check_stack.py
