"""Generate the synthetic benchmark corpus and (optionally) ingest it through
the REAL pipeline. Local models only (privacy fork).

    python build_corpus.py --scale pilot                 # generate to disk only
    python build_corpus.py --scale pilot --ingest        # + capture->process->extract
    python build_corpus.py --scale full  --ingest

Generation writes a directory tree (one folder per track) under
corpus/<scale>/ plus corpus_manifest.json. With --ingest, each track folder is
walked by FilesystemSourceAdapter into tenant `bench-synth` and run through
capture -> processing -> extraction (the same path SOP-014 took), so the
chunks/facts/mentions the benchmark needs actually exist. Resolution is left
to a separate sweep (it's a batch job over pending mentions).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

TENANT = "bench-synth"
# All synthetic material lives in a loudly-labeled folder OUTSIDE the infra
# tree, so fictional documents can never be mistaken for real ones.
CORPUS_ROOT = (Path(__file__).parent.parent /
               "SYNTHETIC DATA - NOT REAL - BENCHMARK ONLY")


def _source_config(corpus_dir: Path) -> dict:
    import json
    manifest = json.loads(
        (corpus_dir / "corpus_manifest.json").read_text(encoding="utf-8"))
    return manifest.get("sources", {})


def _ingest(corpus_dir: Path, scale: str) -> None:
    from knowledge_hub.capture import CaptureService
    from knowledge_hub.dispatch_pg import PostgresDispatcher
    from knowledge_hub.factstore_pg import PostgresFactStore
    from knowledge_hub.pipeline import Pipeline
    from knowledge_hub.rawstore_s3 import S3RawStore
    from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
    from knowledge_hub.sources_fs import FilesystemSourceAdapter
    from knowledge_hub.processing import ProcessingService
    from knowledge_hub.extraction import ExtractionService
    from knowledge_hub.parsing_docling import DoclingParser
    from knowledge_hub.chunking import SectionChunker
    from knowledge_hub.embedding_ollama import OllamaEmbedder
    from knowledge_hub.ontology import PostgresOntologyBinding
    from knowledge_hub.extraction_llm import LLMJointExtractionStrategy
    from knowledge_hub.extraction_structured import StructuredMapStrategy
    from knowledge_hub.grounding import SpanGrounder

    store = PostgresFactStore()
    pipeline = Pipeline(store=store)
    raw_store = S3RawStore(store=store)
    dispatcher = PostgresDispatcher(store)
    ext_dispatcher = PostgresDispatcher(store, table="extraction_queue")
    capture = CaptureService(pipeline, raw_store, dispatcher,
                             secrets=OpenBaoSecretsProvider())
    processing = ProcessingService(pipeline, raw_store, DoclingParser(),
                                   SectionChunker(), OllamaEmbedder(),
                                   dispatcher=dispatcher,
                                   extraction_dispatcher=ext_dispatcher)
    binding = PostgresOntologyBinding(store, version="baseline-0.1")
    extraction = ExtractionService(
        pipeline, raw_store, binding, LLMJointExtractionStrategy(binding),
        StructuredMapStrategy(binding), SpanGrounder(),
        dispatcher=ext_dispatcher)

    # One source per track folder. Each is PRE-REGISTERED with the manifest's
    # declared config (data_track + doc_type, and structured_map for SoR) so
    # the pipeline classifies deterministically and SoR key columns become the
    # strong extracted_keys the resolver's T0 tier matches on — instead of
    # falling back to shape detection (which can't tell comms from forms, and
    # gives SoR rows no keys).
    source_cfg = _source_config(corpus_dir)
    tracks = [d for d in sorted(corpus_dir.iterdir()) if d.is_dir()]
    total_landed = 0
    for track_dir in tracks:
        source_ref = f"synth-{track_dir.name}"
        cfg = source_cfg.get(track_dir.name)
        if cfg:
            capture.registry.register(TENANT, source_ref, "filesystem", cfg)
        adapter = FilesystemSourceAdapter(source_ref=source_ref, root=track_dir)
        res = capture.run_source(TENANT, adapter, mode="backfill")
        total_landed += res.landed
        print(f"  capture[{track_dir.name}]: landed={res.landed} "
              f"replayed={res.replayed} dispatched={res.dispatched}")
    print(f"capture total landed={total_landed}")

    proc = processing.consume(TENANT, limit=1000)
    print(f"processing: {len(proc)} documents processed")
    ext = extraction.consume(TENANT, limit=1000)
    print(f"extraction: {len(ext)} units extracted")


def _summary(scale: str) -> None:
    import psycopg
    from psycopg.rows import dict_row
    dsn = "host=localhost port=5432 dbname=knowledge_hub user=kh password=kh_pilot_pw"
    with psycopg.connect(dsn, row_factory=dict_row,
                         connect_timeout=10) as conn:
        for label, q in [
            ("documents by track",
             "SELECT doc_type, count(*) n FROM documents WHERE tenant_id=%s "
             "GROUP BY doc_type ORDER BY doc_type"),
            ("child chunks",
             "SELECT count(*) n FROM chunks WHERE tenant_id=%s AND level='child'"),
            ("embedded children",
             "SELECT count(*) n FROM chunks WHERE tenant_id=%s AND level='child' "
             "AND embedding IS NOT NULL"),
            ("pending facts",
             "SELECT count(*) n FROM pending_facts WHERE tenant_id=%s"),
            ("entity mentions",
             "SELECT count(*) n FROM entity_mentions WHERE tenant_id=%s"),
            ("mentions w/ strong keys",
             "SELECT count(*) n FROM entity_mentions WHERE tenant_id=%s "
             "AND extracted_keys <> '{}'::jsonb"),
        ]:
            rows = conn.execute(q, (TENANT,)).fetchall()
            print(f"  {label}: {rows if len(rows) > 1 else rows[0]['n']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scale", choices=["pilot", "full"], default="pilot")
    ap.add_argument("--ingest", action="store_true",
                    help="ingest through the real pipeline after generating")
    ap.add_argument("--seed", type=int, default=20260723)
    args = ap.parse_args()

    from knowledge_hub.corpus_synth import CorpusSynthesizer

    corpus_dir = CORPUS_ROOT / args.scale
    print(f"generating {args.scale} corpus -> {corpus_dir}")
    manifest = CorpusSynthesizer(seed=args.seed).generate(corpus_dir, args.scale)
    print(f"registry: {manifest['registry_sizes']}")
    print(f"docs written: {manifest['counts']}")
    print(f"planted ER noise cases: {len(manifest['er_noise'])}")

    if args.ingest:
        print("\ningesting through the real pipeline...")
        _ingest(corpus_dir, args.scale)
        print("\nDB summary (tenant bench-synth):")
        _summary(args.scale)
    return 0


if __name__ == "__main__":
    sys.exit(main())
