"""End-to-end reachability check for the Knowledge Hub pilot stack.

Run from the bundle folder with the venv active:  python check_stack.py

THIN RUNNER: the check bodies live in knowledge_hub.checks (one library,
two runners — `khctl verify` runs the same primitives plan-driven; this
script runs them against the all-ours pilot defaults). Same ten checks,
same order, same output, same exit semantics as always:

  0. Version      — installed knowledge_hub dist == pyproject == __version__
                    (editable-install drift has bitten twice and silently
                    mislabels benchmark provenance — so it runs FIRST)
  1. Postgres     — connect, verify extensions + a few schema tables + seed data
  2. SeaweedFS    — the real S3RawStore code path: bucket with object-lock AND
                    versioning (lock without versioning enforces NOTHING —
                    upstream #8350), round-trip, then verify_worm() PROVES a
                    locked object survives overwrite + rejects delete
  3. OpenBao      — hvac: write + read back a secret at the per-tenant path layout
  4. Ollama       — embedding (bge-m3, expect 1024 dims) + a tiny generation
  5. Processing   — Stage B seams end to end in memory: Docling parse, bge-m3
                    tokenizer (downloads/caches on first run), section+passage
                    chunking, live embedding of a prefixed child
  6. Extraction   — the ontology binding loads from the DB (examples + alias
                    data present), and qwen3.6 completes one schema-constrained
                    joint pass (think:false, format=schema, temp 0) whose
                    output validates and conforms
  7. Resolution   — the policy matrix + flywheel tables exist (migration 005),
                    Splink/DuckDB scores a record pair with the shipped
                    priors, and the adjudication model answers one
                    schema-constrained same-entity verdict
  9. Serving      — the S5 boundary end to end: the service assembles from
                    live components, answers /v1/health, REFUSES an
                    unauthenticated catalog read (fail-closed), and serves
                    one fully gated op round-trip over real HTTP
 10. Operator     — the BP19 write-twin + BP20 reads/UI, non-mutating:
                    health (migration 010), 401 unauthenticated, 403 for an
                    agent read-principal (deny-by-default role gate),
                    role-scoped action catalog, 404 on a missing target,
                    /v1/monitor answering tenant-scoped, and the console UI
                    serving from the kit with no CDN — attempts land in
                    operator_audit
Exit code 0 = all green. Each failure prints the error and continues.
"""
from __future__ import annotations

import sys

from knowledge_hub import checks
from knowledge_hub.checks import assert_versions, version_triple

PASS, FAIL, WARN = "[ OK ]", "[FAIL]", "[WARN]"

# Kept under their historical names — test_service_http.py drives the
# version-integrity logic through this module.
_assert_versions = assert_versions
_version_triple = version_triple

# The pilot gate: every check, all-ours targets (library defaults ==
# settings == the pilot .env). Order matters — version drift fails loudest
# and earliest.
PILOT_CHECKS = [
    ("version integrity", checks.check_version),
    ("postgres", checks.check_postgres),
    ("seaweedfs (s3)", checks.check_s3_worm),
    ("openbao", checks.check_openbao),
    ("ollama", checks.check_ollama),
    ("processing (parse·chunk·embed)", checks.check_processing),
    ("extraction (ontology·llm·ground)", checks.check_extraction),
    ("resolution (policy·splink·adjudicate)", checks.check_resolution),
    ("benchmark harness", checks.check_benchmark),
    ("serving service (S5)", checks.check_serving),
    ("operator write API (BP19)", checks.check_operator),
]


def main() -> int:
    print("Knowledge Hub — stack reachability check\n" + "-" * 44)
    failures: list[str] = []
    for name, fn in PILOT_CHECKS:
        result = checks.run_check(name, fn)
        if result.passed:
            print(f"{PASS} {result.detail}")
        else:
            failures.append(name)
            print(f"{FAIL} {name}: {result.detail}")
    print("-" * 44)
    if failures:
        print(f"{FAIL} {len(failures)} failing: {', '.join(failures)}")
        return 1
    print(f"{PASS} all services reachable — stack is GO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
