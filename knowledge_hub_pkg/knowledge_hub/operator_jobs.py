"""JobRunner — the background worker behind console-triggered work (d.s
Stage 2; the machinery Stage 3's scoped re-extraction reuses).

This closes the gap operator_http.py bookmarked as `start_pull`: an operator
action inserts an operator_jobs row (audited, through the write gate); THIS
runner — one daemon thread inside the operator process — claims it (FOR
UPDATE SKIP LOCKED), executes it, and writes progress back onto the row.
The console polls the row. Every piece of orchestration state lives in
Postgres, never in memory: a killed process restarts, requeues its stale
'running' rows, and re-executes them — safe because every job kind must be
idempotent to re-run (folder ingest is content-hash + cursor idempotent by
construction; that is a REGISTRATION REQUIREMENT for future kinds, not a
hope).

Determinism posture (the four laws): job orchestration is plain code —
claim, run, record. The LLM appears exactly once, inside per-document
extraction, same as every other ingest path. The ontology version a job
extracts under is FIXED at job creation (resolved and written into
params by the write op) and stamped onto every raw document the job lands
(ontology_version_override in native_metadata) — the job never reads a
global "current" version mid-run. This is the split-brain defense on the
operation most likely to trigger it.

Threading: the runner builds its OWN PostgresFactStore. The console's
store serializes behind OperatorApp._store_lock precisely because psycopg
transaction contexts are not thread-safe on a shared connection — so the
runner never touches that store. Two connections, coordinated through the
job table, which is what Postgres is for.

Single-runner assumption, stated: one operator process = one runner
thread. requeue_stale_jobs() at start would fight a second live runner;
if a multi-runner deployment ever exists, staleness needs a heartbeat
column first.

Heavy imports (Docling, embedder, scorer) are deferred to the first job,
mirroring run_ingest's deferred-import pattern — the operator console
stays instant to start.

Out of scope, named so it is not silently assumed (build prompt):
  * watched folders / auto re-ingest on change — a console folder source
    is registered with config.job_only=true and the CLI sweep skips it;
    it re-runs ONLY when an operator creates a new job;
  * job cancellation — Stage 3 brings resumability; cancel rides that;
  * purging superseded facts — retention is a later build.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.sources_fs import ELIGIBLE_EXTENSIONS

logger = logging.getLogger(__name__)

# One full capture->process->extract->resolve pass yielding no work ends
# the drain; the cap is a runaway backstop, not a tuning knob.
_MAX_DRAIN_PASSES = 200


class JobRunner:
    """Claims and executes operator_jobs rows. start() spawns the daemon
    thread; run_pending() executes synchronously (tests, one-shot use)."""

    def __init__(self, dsn: Optional[str] = None,
                 poll_interval: float = 2.0,
                 s3_bucket: Optional[str] = None,
                 s3_retention=None):
        # s3_bucket/s3_retention override the settings defaults — the test
        # suite points the runner at the short-retention test bucket so a
        # test run never stamps decade-long WORM holds on the dev volume.
        self._dsn = dsn
        self._poll = poll_interval
        self._s3_bucket = s3_bucket
        self._s3_retention = s3_retention
        self._store = PostgresFactStore(dsn=dsn)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._services: Optional[dict[str, Any]] = None  # lazy heavy stack
        # Registered kinds. A kind's executor MUST be idempotent to re-run
        # (see module docstring).
        self._kinds: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "folder_ingest": self._run_folder_ingest,
            "reextract_scope": self._run_reextract_scope,
        }

    # ---------------------------------------------------------- lifecycle --
    def start(self) -> None:
        requeued = self._store.requeue_stale_jobs()
        if requeued:
            logger.warning("job runner: requeued %d stale running job(s) "
                           "from a previous process", requeued)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="kh-job-runner")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.run_pending():
                    self._stop.wait(self._poll)
            except Exception:
                # A broken claim/poll cycle must not kill the thread; the
                # traceback goes to the log, the loop breathes and retries.
                logger.exception("job runner: poll cycle failed")
                self._stop.wait(self._poll)

    def run_pending(self) -> int:
        """Claim-and-execute until the queue is empty. Returns jobs run."""
        ran = 0
        while True:
            job = self._store.claim_next_job()
            if job is None:
                return ran
            self._execute(job)
            ran += 1

    # ----------------------------------------------------------- dispatch --
    def _execute(self, job: dict[str, Any]) -> None:
        tenant, job_id, kind = job["tenant_id"], job["id"], job["kind"]
        executor = self._kinds.get(kind)
        if executor is None:
            self._store.finish_job(tenant, job_id, status="failed",
                                   error=f"unknown job kind {kind!r}")
            return
        logger.info("job %d (%s, tenant %s): starting", job_id, kind, tenant)
        try:
            counts = executor(job)
        except Exception as e:
            logger.exception("job %d failed", job_id)
            self._store.finish_job(
                tenant, job_id, status="failed",
                error=f"{type(e).__name__}: {e}\n"
                      f"{traceback.format_exc(limit=5)}")
            return
        self._store.finish_job(tenant, job_id, status="done", counts=counts)
        logger.info("job %d done: %s", job_id, counts)

    # ------------------------------------------------------ heavy services --
    def _stack(self) -> dict[str, Any]:
        """The shared pipeline stack, built once on first use (deferred so
        the operator process starts instantly). Extraction/resolution are
        NOT here — they are rebuilt per job so the default binding follows
        the operator's active selection, exactly like run_ingest's
        per-sweep rebuild."""
        if self._services is None:
            from knowledge_hub.capture import CaptureService
            from knowledge_hub.chunking import SectionChunker
            from knowledge_hub.dispatch_pg import PostgresDispatcher
            from knowledge_hub.embedding_ollama import OllamaEmbedder
            from knowledge_hub.parsing_docling import DoclingParser
            from knowledge_hub.pipeline import Pipeline
            from knowledge_hub.processing import ProcessingService
            from knowledge_hub.rawstore_s3 import S3RawStore
            from knowledge_hub.scoring_tiered import TieredScorer
            from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider

            store = self._store
            pipeline = Pipeline(store=store)
            raw_store = S3RawStore(store=store, bucket=self._s3_bucket,
                                   retention=self._s3_retention)
            dispatcher = PostgresDispatcher(store)
            ext_dispatcher = PostgresDispatcher(store,
                                                table="extraction_queue")
            embedder = OllamaEmbedder()
            self._services = {
                "pipeline": pipeline,
                "raw_store": raw_store,
                "ext_dispatcher": ext_dispatcher,
                "embedder": embedder,
                "scorer": TieredScorer(store),
                "capture": CaptureService(pipeline, raw_store, dispatcher,
                                          secrets=OpenBaoSecretsProvider()),
                "processing": ProcessingService(
                    pipeline, raw_store, DoclingParser(), SectionChunker(),
                    embedder, dispatcher=dispatcher,
                    extraction_dispatcher=ext_dispatcher),
            }
        return self._services

    def _stage_services(self):
        """Per-job extraction + resolution with a fresh default binding
        (the active selection) AND the version factory that honors each
        document's capture-time pin."""
        from knowledge_hub.extraction import ExtractionService
        from knowledge_hub.extraction_llm import LLMJointExtractionStrategy
        from knowledge_hub.extraction_structured import StructuredMapStrategy
        from knowledge_hub.grounding import SpanGrounder
        from knowledge_hub.ontology import PostgresOntologyBinding
        from knowledge_hub.resolution import ResolutionService

        svc = self._stack()

        def trio_for(version: str):
            binding = PostgresOntologyBinding(self._store, version=version)
            return (binding, LLMJointExtractionStrategy(binding),
                    StructuredMapStrategy(binding))

        binding = PostgresOntologyBinding(self._store)  # active selection
        extraction = ExtractionService(
            svc["pipeline"], svc["raw_store"], binding,
            LLMJointExtractionStrategy(binding),
            StructuredMapStrategy(binding), SpanGrounder(),
            dispatcher=svc["ext_dispatcher"], strategy_factory=trio_for)
        resolution = ResolutionService(svc["pipeline"], svc["scorer"],
                                       svc["embedder"])
        return extraction, resolution

    # -------------------------------------------------------- folder ingest --
    def _run_folder_ingest(self, job: dict[str, Any]) -> dict[str, Any]:
        """One console-triggered folder pull, end to end: capture the
        folder's eligible files into the raw store (content-hash idempotent,
        WORM, extraction against the stored copy — the existing capture
        path, not a parallel one), then drain process/extract/resolve until
        a pass yields nothing.

        params (validated + resolved by the write op at creation):
          path              absolute folder, exists/dir/readable
          recurse           bool
          include, exclude  glob lists over the relative path (or null)
          extensions        eligible file suffixes, or null for the shipped
                            default. Per job so that a folder needing an
                            unusual format says so itself, instead of every
                            folder job quietly widening with it
          ontology_version  RESOLVED at creation — never 'whatever is
                            active now'; stamped on every landed document
          source_ref        registry key (stable per path by default)
        """
        from knowledge_hub.sources_fs import FilesystemSourceAdapter

        tenant, job_id, p = job["tenant_id"], job["id"], job["params"]
        svc = self._stack()
        extraction, resolution = self._stage_services()

        extensions = frozenset(p.get("extensions") or ELIGIBLE_EXTENSIONS)
        adapter = FilesystemSourceAdapter(
            source_ref=p["source_ref"], root=p["path"],
            recurse=p.get("recurse", True),
            include=p.get("include"), exclude=p.get("exclude"),
            extensions=extensions,
            extra_metadata={"ontology_version_override":
                            p["ontology_version"]})

        # Register/refresh the source: job_only=true keeps the CLI sweep
        # away (watched folders are out of scope — this folder re-runs only
        # via a new job); the config records what the operator asked for.
        svc["capture"].registry.register(
            tenant, p["source_ref"], "filesystem",
            {"root": str(Path(p["path"]).resolve()), "job_only": True,
             "recurse": p.get("recurse", True),
             "include": p.get("include"), "exclude": p.get("exclude"),
             "extensions": sorted(extensions),
             "ontology_version": p["ontology_version"]})

        result = svc["capture"].run_source(tenant, adapter)
        counts: dict[str, Any] = {
            "ontology_version": p["ontology_version"],
            "files_landed": result.landed,
            "files_replayed": result.replayed,   # skipped-as-duplicate
            "capture_status": result.status,
            **result.source_stats,               # skipped_unknown/glob/unreadable
            "docs_processed": 0, "docs_extracted": 0,
            "mentions_resolved": 0, "facts_promoted": 0,
            "drain_passes": 0,
        }
        self._store.update_job_counts(tenant, job_id, counts)

        # Drain the tenant's queues until quiet. This sweeps whatever is
        # queued for the tenant (not just this job's documents) — a sweep
        # is a sweep; each document extracts under ITS OWN pin or the
        # active default, so interleaving is provenance-safe.
        for _ in range(_MAX_DRAIN_PASSES):
            processed = svc["processing"].consume(tenant, limit=100)
            extracted = extraction.consume(tenant, limit=100)
            summary = resolution.sweep(tenant, limit=500)
            counts["docs_processed"] += len(processed)
            counts["docs_extracted"] += len(extracted)
            counts["mentions_resolved"] += summary.resolved
            counts["facts_promoted"] += len(summary.promoted_facts)
            counts["drain_passes"] += 1
            self._store.update_job_counts(tenant, job_id, counts)
            if not processed and not extracted and summary.swept == 0:
                break

        # Honesty check: anything still queued/errored for this tenant is
        # reported, never silently dropped from the summary.
        with self._store.transaction(tenant) as conn:
            leftovers = conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM dispatch_queue
                   WHERE tenant_id = %s AND status IN ('queued', 'error'))
                      AS dispatch,
                  (SELECT count(*) FROM extraction_queue
                   WHERE tenant_id = %s AND status IN ('queued', 'error'))
                      AS extraction
                """, (tenant, tenant)).fetchone()
        counts["queue_leftover_dispatch"] = leftovers["dispatch"]
        counts["queue_leftover_extraction"] = leftovers["extraction"]
        return counts

    # ---------------------------------------------------- scoped re-extract --
    def _run_reextract_scope(self, job: dict[str, Any]) -> dict[str, Any]:
        """Re-extract a FIXED document population under the job's frozen
        target version (d.s Stage 3), never overwriting: each document
        re-runs the exact per-document extraction path proven in Stage 2
        with a call-time version pin; when its new facts PROMOTE, the old
        vocabulary's facts retire through the promotion cutover
        (Pipeline._promote_document_group, reason='ontology_superseded') —
        retained and queryable, one transaction, one timestamp.

        Resumable + idempotent: the population is materialized once
        (ON CONFLICT DO NOTHING); each document extracts and is marked in
        its own step (a crash at N resumes at N+1); a re-run replays
        already-extracted documents as no-ops via the idempotency ledger
        (keyed on the ontology version), so nothing double-writes.

        A document that stages nothing under the target keeps its old
        facts current (promotion-gated supersession — an empty or failed
        new yield never erases a served corpus); it still shows as done
        with its B-run on the ledger, so A-vs-B queries surface it.

        params (frozen by the write op at creation):
          ontology_version   the TARGET — resolved at creation, never
                             re-read mid-run (the split-brain defense)
          scope_version      docs whose ledger has an ok-run under this
                             version (the default scope shape)
          source_ref / source_system   optional narrowing
          all_documents      explicit blanket (never the default)
          affected           the count the operator confirmed
        """
        tenant, job_id, p = job["tenant_id"], job["id"], job["params"]
        target = p["ontology_version"]
        extraction, resolution = self._stage_services()

        total = self._store.materialize_job_documents(
            tenant, job_id,
            {"scope_version": p.get("scope_version"),
             "source_ref": p.get("source_ref"),
             "source_system": p.get("source_system")})
        counts: dict[str, Any] = {
            "ontology_version": target,
            "scope_version": p.get("scope_version"),
            "docs_total": total,
            "units_extracted": 0, "units_replayed": 0,
            "facts_staged": 0, "facts_promoted": 0,
            "facts_superseded": 0, "failed_documents": [],
            **self._store.job_document_counts(tenant, job_id),
        }
        self._store.update_job_counts(tenant, job_id, counts)

        def sweep_and_count() -> None:
            summary = resolution.sweep(tenant, limit=500)
            counts["facts_promoted"] += len(summary.promoted_facts)

        processed_since_sweep = 0
        while True:
            batch = self._store.pending_job_documents(tenant, job_id,
                                                      limit=50)
            if not batch:
                break
            for raw_id in batch:
                try:
                    summary = extraction.extract(tenant, raw_id,
                                                 ontology_version=target)
                except Exception as e:
                    logger.warning("job %d: re-extract of raw_document "
                                   "id=%s failed: %s", job_id, raw_id, e)
                    self._store.mark_job_document(
                        tenant, job_id, raw_id, status="failed",
                        error=f"{type(e).__name__}: {e}")
                    if len(counts["failed_documents"]) < 25:
                        counts["failed_documents"].append(raw_id)
                    continue
                if summary.status == "review":
                    # Held for a human (§8.1a): not silently skipped — the
                    # document stays visible as failed with the way forward.
                    self._store.mark_job_document(
                        tenant, job_id, raw_id, status="failed",
                        error="document is held for human review — resolve "
                              "its flag, then re-run this job (idempotent)")
                    if len(counts["failed_documents"]) < 25:
                        counts["failed_documents"].append(raw_id)
                    continue
                counts["units_extracted"] += (summary.units
                                              - summary.units_replayed)
                counts["units_replayed"] += summary.units_replayed
                counts["facts_staged"] += summary.facts
                self._store.mark_job_document(tenant, job_id, raw_id,
                                              status="done")
                processed_since_sweep += 1
                if processed_since_sweep >= 25:
                    sweep_and_count()
                    processed_since_sweep = 0
                counts.update(self._store.job_document_counts(tenant,
                                                              job_id))
                self._store.update_job_counts(tenant, job_id, counts)

        # Final drain: resolve + promote until quiet — promotion is where
        # the old vocabulary's facts retire, so the job is not done until
        # the resolver is.
        for _ in range(_MAX_DRAIN_PASSES):
            summary = resolution.sweep(tenant, limit=500)
            counts["facts_promoted"] += len(summary.promoted_facts)
            if summary.swept == 0 and not summary.promoted_facts:
                break

        with self._store.transaction(tenant) as conn:
            superseded = conn.execute(
                "SELECT count(*) AS n FROM facts"
                " WHERE tenant_id = %s AND retraction_reason = %s"
                "   AND valid_to >= %s",
                (tenant, "ontology_superseded",
                 job["started_at"])).fetchone()
        counts["facts_superseded"] = superseded["n"]
        counts.update(self._store.job_document_counts(tenant, job_id))
        return counts
