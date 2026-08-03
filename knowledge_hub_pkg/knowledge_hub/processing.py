"""Processing flow (Build Prompt 3): Stage B for the prose/SOP track.

ProcessingService is capture's downstream twin: where CaptureService lands
bytes, this turns a landed raw document into the persisted three-tier
hierarchy, per document:

    RawStore.get(raw.raw_uri)                exact landed bytes, version-pinned
      -> merge manifest declaration          registry config -> native_metadata
      -> Parser.parse(raw, content)          superparent Document (+ §8.1a views)
      -> insert_document / reuse existing    Prompt 1 persistence, never around it
      -> arbitrate declared vs detected      confident mismatch -> review_queue
      -> Parser.extract_text(raw, content)   heading-preserving Markdown
      -> Chunker.chunk(document, text)       parents + children ([] for non-prose)
      -> Embedder.embed(prefix + content)    children only, live bge-m3
      -> insert_chunks parents, then children (parent ids rewritten into links)
      -> raw_documents.status = 'parsed'

§8.1a arbitration (tag-as-claim): declared and detected agree -> proceed;
detection is low-confidence -> the human tag wins, proceed; CONFIDENT
disagreement -> the document row is persisted and flagged to review_queue and
chunking is WITHHELD — never silently auto-override the human, never blindly
obey a tag the content contradicts. The raw doc stays 'landed'; after a human
adjudicates (review_status -> resolved, tag corrected), process(force=True)
picks it back up.

Idempotency: re-processing a raw document whose hierarchy already exists is
a no-op (status='replayed') — and even a force=True re-run inserts zero new
rows, because chunk content hashes are deterministic and insert_chunks
replays through ON CONFLICT. That is what makes the dispatch queue's
at-least-once delivery safe to consume.

consume() is the dispatch-queue consumer (claim -> process -> ack; nack on
error so the lease redelivers): the arrival of Stage B is what the queue's
claim/ack cycle was shipped for in Prompt 2.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from pydantic import BaseModel

from knowledge_hub.chunking import embedding_text
from knowledge_hub.dispatch_pg import PostgresDispatcher
from knowledge_hub.interfaces import Chunker, Embedder, Parser, RawStore
from knowledge_hub.models import (
    PROSE_TRACK,
    Chunk,
    ChunkLevel,
    Document,
    RawDocument,
)
from knowledge_hub.pipeline import Pipeline

logger = logging.getLogger(__name__)


class ProcessResult(BaseModel):
    """Summary of processing one raw document (returned, safe to log)."""
    tenant_id: str
    raw_document_id: int
    document_id: Optional[int] = None
    status: str          # processed|replayed|review|no_chunks
    reason: Optional[str] = None
    parents: int = 0
    children: int = 0
    # §8.1g re-extraction policy inputs (a RE-VERSION of a lazy track is
    # dispatched to extraction with a deferred available_at).
    raw_version: int = 1
    data_track: Optional[str] = None


class ProcessingService:
    def __init__(self, pipeline: Pipeline, raw_store: RawStore,
                 parser: Parser, chunker: Chunker, embedder: Embedder,
                 dispatcher: Optional[PostgresDispatcher] = None,
                 extraction_dispatcher: Optional[PostgresDispatcher] = None,
                 lazy_reextract_tracks: frozenset[str] = frozenset(),
                 lazy_reextract_delay: timedelta = timedelta(hours=1)):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.raw_store = raw_store
        self.parser = parser
        self.chunker = chunker
        self.embedder = embedder
        self.dispatcher = dispatcher
        # The NEXT outbox (migration 004): documents that finish Stage B are
        # handed to extraction the same way capture handed them here.
        self.extraction_dispatcher = extraction_dispatcher
        # §8.1g eager/lazy re-extraction split, applied at the extraction
        # dispatch: supersession triggers RE-EXTRACTION (the new version
        # through the LLM) — the expensive operation the lazy option was
        # designed for. Version 1 of anything and every track NOT listed
        # here extract immediately (eager is the safe default); tracks
        # listed here batch their re-versions behind `lazy_reextract_delay`,
        # accepting a priced staleness window (the OLD version's facts stay
        # current until the deferred extraction promotes and cuts over).
        # Which tracks count as bulk-vs-high-stakes is DATA for domain
        # experts (§8.1g note) — configured here, never hardcoded.
        self.lazy_reextract_tracks = frozenset(lazy_reextract_tracks)
        self.lazy_reextract_delay = lazy_reextract_delay

    # ------------------------------------------------------------ consume --
    def consume(self, tenant_id: str, limit: int = 10) -> list[ProcessResult]:
        """Drain up to `limit` dispatched documents: claim -> process -> ack.
        A failure nacks that ONE message (the lease redelivers it) and the
        drain continues — one poison document must not wedge the queue."""
        if self.dispatcher is None:
            raise RuntimeError("ProcessingService was built without a dispatcher")
        results: list[ProcessResult] = []
        for message in self.dispatcher.claim(tenant_id, limit=limit):
            try:
                result = self.process(tenant_id, message.raw_document_id)
            except Exception as e:
                logger.warning("processing raw_document id=%s failed: %s",
                               message.raw_document_id, e)
                self.dispatcher.nack(tenant_id, message.id,
                                     error=f"{type(e).__name__}: {e}")
                continue
            self.dispatcher.ack(tenant_id, message.id)
            results.append(result)
        return results

    # ------------------------------------------------------------ process --
    def process(self, tenant_id: str, raw_document_id: int,
                force: bool = False) -> ProcessResult:
        """Turn one landed raw document into its persisted hierarchy, then
        hand it to the extraction outbox (when wired). Idempotent: an
        already-processed document replays as a no-op unless force=True
        (which still inserts nothing new — hashes are stable), and the
        downstream dispatch is idempotent per (tenant, raw_document_id).
        Review-held documents are NOT dispatched — a human owns them."""
        result = self._process(tenant_id, raw_document_id, force)
        if (self.extraction_dispatcher is not None
                and result.status in ("processed", "replayed", "no_chunks")):
            self.extraction_dispatcher.dispatch(
                tenant_id, raw_document_id,
                delay=self._reextraction_delay(result.raw_version,
                                               result.data_track))
        return result

    def _reextraction_delay(self, raw_version: int,
                            data_track: Optional[str]) -> timedelta:
        """§8.1g eager/lazy: immediate for first landings and for every
        track not configured lazy; deferred for a RE-VERSION of a lazy
        (bulk) track. The deferral is the outbox's available_at — the
        old version keeps serving until the batched extraction promotes
        and the supersession cutover retires it (a priced, chosen window)."""
        if raw_version <= 1 or data_track not in self.lazy_reextract_tracks:
            return timedelta(0)
        return self.lazy_reextract_delay

    def _process(self, tenant_id: str, raw_document_id: int,
                 force: bool = False) -> ProcessResult:
        raw = self.store.get_raw_document(tenant_id, raw_document_id)
        if raw is None:
            raise LookupError(f"raw_document id={raw_document_id} not found "
                              f"for tenant {tenant_id!r}")

        existing = self._existing_document(tenant_id, raw_document_id)
        if existing is not None and not force:
            replay = self._replay_result(tenant_id, raw, existing)
            if replay is not None:
                return replay

        # The exact landed bytes, via the version-pinned URI — never a
        # re-fetch from the source, never bytes something wrote over later.
        content = self.raw_store.get(raw.raw_uri)

        self._merge_declaration(raw)
        document = self.parser.parse(raw, content)
        if existing is not None:
            document.id = existing.id
            document.review_status = existing.review_status
        else:
            self.store.insert_document(document)

        mismatch = self._track_mismatch(document)
        if mismatch is not None:
            if document.review_status != "review":  # don't re-flag on replay
                self.pipeline._enqueue_review(tenant_id, "document",
                                              document.id, mismatch)
            logger.warning("data_track mismatch for document id=%s: %s",
                           document.id, mismatch)
            return ProcessResult(
                tenant_id=tenant_id, raw_document_id=raw_document_id,
                document_id=document.id, status="review", reason=mismatch,
                raw_version=raw.version, data_track=document.data_track)

        text = self.parser.extract_text(raw, content)
        chunks = self.chunker.chunk(document, text)
        if not chunks:  # non-prose track: the router's structured strategy
            self._mark_parsed(tenant_id, raw_document_id)
            return ProcessResult(
                tenant_id=tenant_id, raw_document_id=raw_document_id,
                document_id=document.id, status="no_chunks",
                reason=f"data_track {document.data_track!r} does not chunk",
                raw_version=raw.version, data_track=document.data_track)

        self._embed_children(chunks)
        parents, children = self._persist_hierarchy(chunks)
        self._mark_parsed(tenant_id, raw_document_id)
        return ProcessResult(
            tenant_id=tenant_id, raw_document_id=raw_document_id,
            document_id=document.id, status="processed",
            parents=parents, children=children,
            raw_version=raw.version, data_track=document.data_track)

    # ---------------------------------------------------------- internals --
    def _existing_document(self, tenant_id: str,
                           raw_document_id: int) -> Optional[Document]:
        with self.store.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT id FROM documents"
                " WHERE tenant_id = %s AND raw_document_id = %s"
                " ORDER BY id LIMIT 1",
                (tenant_id, raw_document_id),
            ).fetchone()
        return self.store.get_document(tenant_id, row["id"]) if row else None

    def _replay_result(self, tenant_id: str, raw: RawDocument,
                       document: Document) -> Optional[ProcessResult]:
        """A prior run's outcome, if this delivery is a duplicate of one:
        review-held documents stay held, chunked documents report their
        existing tiers, non-prose documents stay chunkless. Returns None when
        the prior run died between document and chunks — fall through and
        finish the job (hashes make that safe)."""
        if document.review_status == "review":
            return ProcessResult(
                tenant_id=tenant_id, raw_document_id=raw.id,
                document_id=document.id, status="review",
                reason=document.review_reason,
                raw_version=raw.version, data_track=document.data_track)
        with self.store.transaction(tenant_id) as conn:
            counts = conn.execute(
                "SELECT level, count(*) AS n FROM chunks"
                " WHERE tenant_id = %s AND document_id = %s GROUP BY level",
                (tenant_id, document.id),
            ).fetchall()
        by_level = {r["level"]: r["n"] for r in counts}
        if by_level:
            return ProcessResult(
                tenant_id=tenant_id, raw_document_id=raw.id,
                document_id=document.id, status="replayed",
                parents=by_level.get("parent", 0),
                children=by_level.get("child", 0),
                raw_version=raw.version, data_track=document.data_track)
        if document.data_track != PROSE_TRACK:
            return ProcessResult(
                tenant_id=tenant_id, raw_document_id=raw.id,
                document_id=document.id, status="no_chunks",
                reason=f"data_track {document.data_track!r} does not chunk",
                raw_version=raw.version, data_track=document.data_track)
        return None  # document row without chunks: crashed mid-run, finish it

    def _merge_declaration(self, raw: RawDocument) -> None:
        """Fold the manifest's declared tags into native_metadata (in memory,
        for the parser): a per-item declaration wins; otherwise the source's
        registry config (§8.1a: the manifest tag is a standing EXPECTATION
        checked per document) fills the gap. The registry row is found via
        the source_ref the adapter stamped into native_metadata."""
        native = dict(raw.native_metadata or {})
        source_ref = native.get("source_ref")
        if source_ref and not (native.get("data_track") and native.get("doc_type")):
            with self.store.transaction(raw.tenant_id) as conn:
                row = conn.execute(
                    "SELECT config FROM source_registry"
                    " WHERE tenant_id = %s AND source_ref = %s",
                    (raw.tenant_id, source_ref),
                ).fetchone()
            config = (row or {}).get("config") or {}
            for key in ("data_track", "doc_type"):
                if native.get(key) is None and config.get(key) is not None:
                    native[key] = config[key]
        raw.native_metadata = native

    @staticmethod
    def _track_mismatch(document: Document) -> Optional[str]:
        """§8.1a arbitration: a review reason when the declared track and a
        CONFIDENT detection disagree, else None (agree / no tag / detector
        unsure -> the human tag stands)."""
        meta = document.metadata or {}
        declared = meta.get("declared_data_track")
        detected = meta.get("detected_data_track")
        if (declared is not None and meta.get("detection_confident")
                and detected != declared):
            return (f"declared data_track {declared!r} but content shape is "
                    f"confidently {detected!r} (tag-as-claim, §8.1a)")
        return None

    def _embed_children(self, chunks: list[Chunk]) -> None:
        children = [c for c in chunks if c.level is ChunkLevel.child]
        vectors = self.embedder.embed([embedding_text(c) for c in children])
        for child, vector in zip(children, vectors):
            child.embedding = vector
            child.embedding_model = self.embedder.model
            child.embedding_version = self.embedder.version

    def _persist_hierarchy(self, chunks: list[Chunk]) -> tuple[int, int]:
        """Insert parents, rewriting each parent's new id into its children
        (Chunker contract: each parent is immediately followed by its own
        children), then insert the children. Everything goes through Prompt
        1's insert_chunks — replays are no-ops by content hash."""
        parents = children = 0
        parent_id: Optional[int] = None
        pending: list[Chunk] = []

        def flush() -> None:
            nonlocal children
            if pending:
                self.store.insert_chunks(pending)
                children += len(pending)
                pending.clear()

        for chunk in chunks:
            if chunk.level is ChunkLevel.parent:
                flush()
                parent_id = self.store.insert_chunks([chunk])[0]
                parents += 1
            else:
                if parent_id is None:
                    raise ValueError("chunker returned a child before any "
                                     "parent — ordering contract violated")
                chunk.parent_chunk_id = parent_id
                pending.append(chunk)
        flush()
        return parents, children

    def _mark_parsed(self, tenant_id: str, raw_document_id: int) -> None:
        with self.store.transaction(tenant_id) as conn:
            conn.execute(
                "UPDATE raw_documents SET status = 'parsed'"
                " WHERE tenant_id = %s AND id = %s",
                (tenant_id, raw_document_id))
