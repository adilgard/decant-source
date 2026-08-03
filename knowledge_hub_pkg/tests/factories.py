"""Small builders for test rows (unique hashes so per-tenant idempotency
never collides across tests)."""
from __future__ import annotations

import hashlib
import uuid

from knowledge_hub.models import Chunk, ChunkLevel, DocType, Document, Entity, RawDocument

ONTOLOGY = "baseline-0.1"


def sha(text: str | None = None) -> str:
    return hashlib.sha256((text or uuid.uuid4().hex).encode()).hexdigest()


def make_raw(tenant: str, **overrides) -> RawDocument:
    fields = dict(
        tenant_id=tenant,
        source_system="sharepoint",
        source_native_id=f"DOC-{uuid.uuid4().hex[:8]}",
        mime_type="application/pdf",
        content_hash=sha(),
        raw_uri=f"s3://kh-raw/{uuid.uuid4().hex}",
    )
    fields.update(overrides)
    return RawDocument(**fields)


def land_document(pipeline, store, tenant: str, doc_type: DocType = DocType.prose) -> Document:
    raw = make_raw(tenant)
    pipeline.ingest_raw(raw)
    doc = Document(tenant_id=tenant, raw_document_id=raw.id, doc_type=doc_type,
                   title="Test doc")
    store.insert_document(doc)
    return doc


def make_chunk(tenant: str, document_id: int, level: ChunkLevel = ChunkLevel.parent,
               **overrides) -> Chunk:
    fields = dict(
        tenant_id=tenant,
        document_id=document_id,
        level=level,
        seq=0,
        content=f"chunk content {uuid.uuid4().hex}",
    )
    fields.update(overrides)
    fields.setdefault("content_hash", sha(fields["content"]))
    return Chunk(**fields)


def make_entity(tenant: str, name: str, entity_type: str = "Organization",
                **overrides) -> Entity:
    fields = dict(
        tenant_id=tenant,
        canonical_name=name,
        entity_type=entity_type,
        ontology_version=ONTOLOGY,
    )
    fields.update(overrides)
    return Entity(**fields)
