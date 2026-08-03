"""Core data contracts.

A faithful mirror of knowledge_hub_baseline_schema.sql (v0.2) PLUS
migrations/001_persistence_addenda.sql (tenancy, raw versioning, pending_facts)
PLUS migrations/002_capture_registry.sql (native_metadata, source_registry,
dispatch_queue) PLUS migrations/003_document_review.sql (document review
feeder: Document.review_status/review_reason) PLUS
migrations/004_extraction.sql (PendingFact grounding/needs_review/size alarm,
QuarantinedExtraction, ExtractionRun, extraction_queue — reuses
DispatchMessage) PLUS migrations/005_resolution_flywheel.sql (Label,
ResolutionDecision — and the v0.2 baseline resolution tables Stage D now
exercises get models here too: MatchCandidate, EntityMerge,
ResolutionPolicy). These Pydantic models are the
Python-side view of the same contracts the SQL encodes. IMPORTANT: keep this
file in lock-step with the schema — if one changes, change both in the same
commit (see SETUP.md "Keep in lock-step").

Reconciliations vs the v0.1 placeholder (flagged, not silent):
  * tenant_id on every persisted model — migration 001 adds the columns; the
    baseline schema had none but the persistence spec requires per-tenant
    filtering on every query.
  * RawDocument.version — migration 001; supports _next_version.
  * Fact gains char_start/char_end/locator (schema had them; placeholder
    didn't) + serialized_lines/oversized (v0.2 size soft-alarm).
  * Chunk gains embedding_version/speaker/event_time (schema had them).
  * Entity gains embedding_model + transient `aliases` (persisted to
    entity_aliases, not a column on entities).
  * EntityMention gains char_start/char_end/locator/resolver_version/
    resolved_at (schema had them).
  * PendingFact is NEW (migration 001): extraction emits facts whose refs are
    mention-keys; facts.subject_entity_id is NOT NULL so they stage here.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

DEFAULT_TENANT = "default"


class DocType(str, Enum):
    prose = "prose"
    sop = "sop"
    communication = "communication"
    tabular = "tabular"
    sor = "sor"
    form = "form"


# Data tracks (§8.1a): the coarse processing route. Prose-track documents get
# the superparent/parent/child chunk treatment; structured-track documents
# (SoR rows, tabular, form headers) produce facts with NO chunks and are
# routed to the structured strategy instead. Track is DATA on the document
# (documents.metadata->>'data_track'), not schema — the taxonomy has to stay
# human-pickable in a manifest.
PROSE_TRACK = "prose"
STRUCTURED_TRACK = "structured"
DATA_TRACKS = (PROSE_TRACK, STRUCTURED_TRACK)

_DOC_TYPE_TRACKS = {
    DocType.prose: PROSE_TRACK,
    DocType.sop: PROSE_TRACK,
    DocType.communication: PROSE_TRACK,
    DocType.tabular: STRUCTURED_TRACK,
    DocType.sor: STRUCTURED_TRACK,
    DocType.form: STRUCTURED_TRACK,
}


def data_track_for(doc_type: DocType) -> str:
    """The processing track a doc_type belongs to."""
    return _DOC_TYPE_TRACKS[doc_type]


class ChunkLevel(str, Enum):
    parent = "parent"
    child = "child"


class RawDocument(BaseModel):
    """Immutable raw landing record; idempotent by (tenant_id, content_hash)."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    source_system: str
    source_native_id: Optional[str] = None
    mime_type: Optional[str] = None
    content_hash: str
    raw_uri: str
    source_acl: Optional[dict[str, Any]] = None
    security_label_id: Optional[int] = None
    captured_at: Optional[datetime] = None
    ingested_at: Optional[datetime] = None
    status: str = "landed"  # landed|parsed|extracted|error
    version: int = 1        # migration 001; bumped by Pipeline._next_version
    # Migration 002: everything else the adapter observed at acquisition
    # (absolute path, size, timestamps, owner, ...). source_acl stays ACL-only.
    native_metadata: Optional[dict[str, Any]] = None
    # Migration 008: soft tombstone (§8.1g) — set when the source explicitly
    # deleted the logical doc, cleared if it reappears. Bytes stay in WORM.
    deleted_at: Optional[datetime] = None


class Document(BaseModel):
    """Superparent tier: the whole-document metadata/provenance roll-up.

    `metadata` carries the routing + §8.1a arbitration record for processed
    documents: data_track (effective), declared_data_track (the manifest
    claim, if any), detected_data_track + detection_confident (cheap shape
    detection), outline (heading structure), parser.
    """
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    raw_document_id: int
    doc_type: DocType
    title: Optional[str] = None
    author: Optional[str] = None
    source_timestamp: Optional[datetime] = None
    thread_id: Optional[str] = None
    security_label_id: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None
    ingested_at: Optional[datetime] = None
    # Migration 003: the document feeder of review_queue (declared-vs-detected
    # data_track disagreements, per §8.1a tag-as-claim).
    review_status: str = "none"  # none|review|resolved
    review_reason: Optional[str] = None
    # Migration 009: temporal currency. valid_to set = this document is no
    # longer current (source tombstoned, or — future — superseded by a newer
    # version); retraction_reason says which writer set it, so revival only
    # ever reverses its own trigger ('source_tombstone').
    valid_to: Optional[datetime] = None
    retraction_reason: Optional[str] = None

    @property
    def data_track(self) -> str:
        """Effective processing track: metadata['data_track'] when the parser
        recorded one, else derived from doc_type."""
        if self.metadata and self.metadata.get("data_track") in DATA_TRACKS:
            return self.metadata["data_track"]
        return data_track_for(self.doc_type)


class Chunk(BaseModel):
    """Parent (extraction unit) or child (embed/cite unit)."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    document_id: int
    parent_chunk_id: Optional[int] = None  # NULL for parents
    level: ChunkLevel
    seq: int
    content: str
    contextual_prefix: Optional[str] = None
    content_hash: str
    token_count: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    locator: Optional[dict[str, Any]] = None
    speaker: Optional[str] = None
    event_time: Optional[datetime] = None
    embedding: Optional[list[float]] = None  # vector(1024) when embedded
    embedding_model: Optional[str] = None
    embedding_version: Optional[str] = None


class EntityAlias(BaseModel):
    """One observed surface form of a canonical entity."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    entity_id: Optional[int] = None  # filled when persisted with its entity
    alias: str
    source: Optional[str] = None
    confidence: Optional[float] = None


class Entity(BaseModel):
    """Canonical entity registry row."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    canonical_name: str
    entity_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    ontology_version: str
    security_label_id: Optional[int] = None
    embedding: Optional[list[float]] = None
    embedding_model: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    created_at: Optional[datetime] = None
    # Transient convenience: persisted to entity_aliases, NOT a column on
    # entities. FactStore.upsert_entity writes these alongside the row.
    aliases: list[EntityAlias] = Field(default_factory=list)


class Fact(BaseModel):
    """Unified triple: subject --predicate--> (object_entity | object_literal)."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    subject_entity_id: int
    predicate: str
    object_entity_id: Optional[int] = None
    object_literal: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    ontology_version: str
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    # Migration 009: which writer set valid_to ('source_tombstone' = tombstone
    # propagation, reversible on revival; future 'superseded' = re-version).
    retraction_reason: Optional[str] = None
    source_document_id: Optional[int] = None
    source_chunk_id: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    locator: Optional[dict[str, Any]] = None
    extractor: str
    extractor_version: str
    confidence: Optional[float] = None
    security_label_id: Optional[int] = None
    # v0.2 size soft-alarm: computed at insert by the FactStore; oversized
    # facts are written intact and surface in the review_queue view.
    serialized_lines: Optional[int] = None
    oversized: bool = False

    @model_validator(mode="after")
    def _check_object_and_provenance(self) -> "Fact":
        # Mirrors CHECK chk_object_present + chk_provenance_present in the schema.
        if self.object_entity_id is None and self.object_literal is None:
            raise ValueError("fact must have object_entity_id OR object_literal")
        if self.source_chunk_id is None and self.source_document_id is None:
            raise ValueError("fact must have source_chunk_id OR source_document_id")
        return self


class EntityMention(BaseModel):
    """Raw pre-resolution observation; the entity-resolution input layer."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    surface_text: str
    entity_type: str
    source_system: str
    source_document_id: Optional[int] = None
    source_chunk_id: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    locator: Optional[dict[str, Any]] = None
    extracted_keys: dict[str, Any] = Field(default_factory=dict)
    context_embedding: Optional[list[float]] = None
    resolved_entity_id: Optional[int] = None
    resolution_status: str = "pending"  # pending|resolved|review|rejected
    resolver_version: Optional[str] = None
    resolved_at: Optional[datetime] = None


class PendingFact(BaseModel):
    """Pre-resolution candidate fact (extraction handoff; migration 001).

    subject_ref / object_ref grammar:
      * an extraction-local mention key (any string) when handed to
        FactStore.stage_pending together with the mentions dict — rewritten to
        'mention:<id>' as the mentions are persisted;
      * 'mention:<entity_mentions.id>' once staged;
      * 'entity:<entities.id>' for keyed sources that resolve deterministically.
    Promoted into `facts` by Pipeline._rewrite_refs once mentions resolve.
    """
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    subject_ref: str
    predicate: str
    object_ref: Optional[str] = None
    object_literal: Optional[str] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    ontology_version: str
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    source_document_id: Optional[int] = None
    source_chunk_id: Optional[int] = None
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    locator: Optional[dict[str, Any]] = None
    extractor: str
    extractor_version: str
    confidence: Optional[float] = None
    security_label_id: Optional[int] = None
    resolution_status: str = "pending"  # pending|promoted|rejected
    promoted_fact_id: Optional[int] = None
    # Migration 004: grounding observability + review flag + size soft-alarm.
    # grounding: pass|span_missing|components_missing|construction (SoR).
    grounding: Optional[str] = None
    needs_review: bool = False
    serialized_lines: Optional[int] = None
    oversized: bool = False

    @model_validator(mode="after")
    def _check_object_and_provenance(self) -> "PendingFact":
        # Mirrors chk_pending_object + chk_pending_provenance (migration 001).
        if self.object_ref is None and self.object_literal is None:
            raise ValueError("pending fact must have object_ref OR object_literal")
        if self.source_chunk_id is None and self.source_document_id is None:
            raise ValueError("pending fact must have source_chunk_id OR source_document_id")
        return self


QUARANTINE_REASONS = ("unbound_entity_type", "unbound_predicate",
                      "validation_failure")


class QuarantinedExtraction(BaseModel):
    """One off-ontology / won't-validate extraction (migration 004).

    Never silently dropped: carries the raw model output so the review queue
    (kind='quarantine') shows exactly what the model tried to say — the
    signal that grows the ontology and labels the flywheel."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    document_id: Optional[int] = None
    source_chunk_id: Optional[int] = None
    reason: str  # one of QUARANTINE_REASONS
    detail: Optional[str] = None
    raw_output: Optional[dict[str, Any]] = None
    extractor: str
    extractor_version: str
    ontology_version: str
    status: str = "open"  # open|resolved|dismissed
    created_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_reason(self) -> "QuarantinedExtraction":
        if self.reason not in QUARANTINE_REASONS:
            raise ValueError(f"reason must be one of {QUARANTINE_REASONS}")
        return self


class ExtractionRun(BaseModel):
    """Per-unit extraction observability + idempotency record (migration 004).

    unit_hash keys idempotency: one status='ok' row per (tenant, unit_hash,
    extractor, extractor_version, ontology_version) — re-extracting the same
    content with the same model+ontology replays instead of re-staging.
    Token counts and wall-clock are the extraction-benchmark inputs."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    document_id: int
    source_chunk_id: Optional[int] = None  # NULL for structured (whole-doc) runs
    unit_hash: str
    strategy: str            # llm_joint | structured_map
    extractor: str
    extractor_version: str   # the model digest for LLM runs
    ontology_version: str
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    wall_ms: Optional[int] = None
    facts_staged: int = 0
    mentions_staged: int = 0
    quarantined: int = 0
    grounding_flags: int = 0
    repairs: int = 0         # capped at 1 by policy
    status: str = "ok"       # ok|error
    error: Optional[str] = None
    created_at: Optional[datetime] = None


# =============================================================================
# Resolution (Build Prompt 5): the v0.2 baseline ER tables + migration 005
# =============================================================================

class ResolutionPolicy(BaseModel):
    """The threshold matrix for one entity type (resolution_policy, v0.2).

    Read as DATA at resolve time — retuning is an UPDATE, not a deploy. The
    seeded numbers are PLACEHOLDERS until the ER benchmark calibrates them
    against labeled pairs (precision_target documents what t_high was — or
    will be — calibrated to hit)."""
    entity_type: str
    t_high: float                          # >= this -> auto-merge
    t_low: float                           # <= this -> new/separate
    precision_target: Optional[float] = None
    requires_corroboration: bool = False   # name-only match needs a graph edge
    auto_merge_allowed: bool = True
    notes: Optional[str] = None
    updated_at: Optional[datetime] = None


MATCH_METHODS = ("deterministic_key", "deterministic_name", "probabilistic",
                 "embedding", "llm")
BANDS = ("high", "gray", "low")


class MatchCandidate(BaseModel):
    """One scored (mention|entity, entity) pair + banded decision (v0.2).

    left/right are polymorphic ('mention' | 'entity') and NOT FK-enforced in
    the schema — the resolver/app layer owns referential integrity."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    left_type: str                      # 'mention' | 'entity'
    left_id: int
    right_type: str = "entity"
    right_id: int
    match_score: float
    match_method: str                   # one of MATCH_METHODS
    features: Optional[dict[str, Any]] = None
    band: Optional[str] = None          # high | gray | low
    decision: str = "pending"           # auto_merge|auto_separate|review|applied
    decision_reason: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class EntityMerge(BaseModel):
    """Reversible merge log row (v0.2). merged_snapshot carries everything a
    reversal needs to reconstruct the absorbed entity AND undo the transfer:
    the entity row (incl. embedding), its aliases, which aliases/attribute
    keys actually moved to the survivor, the repointed mention ids, the
    repointed fact ids (with side), and rewritten pending_fact refs."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    surviving_entity_id: int
    merged_entity_id: int
    merged_snapshot: dict[str, Any]
    triggered_by: Optional[int] = None  # match_candidates.id
    method: Optional[str] = None
    score: Optional[float] = None
    merged_by: str                      # 'auto' | reviewer id
    merged_at: Optional[datetime] = None
    reversed_at: Optional[datetime] = None
    reversed_by: Optional[str] = None


LABEL_TYPES = ("er_match", "er_nonmatch", "retrieval_relevance", "correction")
LABEL_SOURCES = ("human_review", "reversal", "agent_feedback",
                 "deterministic", "explicit")


class Label(BaseModel):
    """One flywheel label (migration 005, §3.4). `authority` weights the
    SOURCE's trustworthiness; `confidence` is the labeler's own confidence in
    this item. er_match/er_nonmatch payloads carry {left, right} refs in the
    match_candidates polymorphic shape."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    label_type: str
    payload: dict[str, Any]
    source: str
    authority: float = 1.0
    confidence: Optional[float] = None
    ontology_version: str
    created_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_vocab(self) -> "Label":
        if self.label_type not in LABEL_TYPES:
            raise ValueError(f"label_type must be one of {LABEL_TYPES}")
        if self.source not in LABEL_SOURCES:
            raise ValueError(f"source must be one of {LABEL_SOURCES}")
        return self


RESOLUTION_TIERS = ("t0", "t1", "t1b", "none")


class ResolutionDecision(BaseModel):
    """Per-mention resolver observability (migration 005): tier, score, band,
    decision, and the deterministic evidence — the ER benchmark's (Axis B)
    per-decision signal, extraction_runs' principle one stage downstream.
    entity_id is not FK-enforced: merges may delete the entity row later and
    this history must survive."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    mention_id: int
    tier: str                           # t0 | t1 | t1b | none
    method: str                         # deterministic_key|probabilistic|embedding|llm|none
    score: Optional[float] = None
    band: Optional[str] = None
    decision: str                       # resolved | new_entity | review
    entity_id: Optional[int] = None
    match_candidate_id: Optional[int] = None
    features: Optional[dict[str, Any]] = None
    resolver_version: str
    wall_ms: Optional[int] = None
    created_at: Optional[datetime] = None


class SourceRegistryEntry(BaseModel):
    """One registered source for a tenant (migration 002).

    `config` is adapter configuration only — credentials NEVER live here;
    they live in OpenBao under tenants/<tenant_id>/sources/<source_ref>.
    A missing/denied credential flips `status` to 'degraded' for THIS source
    only; the tenant's other sources keep running.
    """
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    source_ref: str
    source_system: str
    config: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"  # active|degraded|disabled
    status_reason: Optional[str] = None
    # Opaque adapter cursor tokens (see SourceAdapter): `cursor` is the
    # incremental high-water mark across completed runs; `backfill_cursor` is
    # the mid-backfill resume point, cleared once the backfill completes.
    cursor: Optional[str] = None
    backfill_cursor: Optional[str] = None
    backfill_done: bool = False
    last_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DispatchMessage(BaseModel):
    """One capture->processing handoff record (migration 002).

    Carries a REFERENCE (raw_document_id), never payload. One record per
    landed doc (enqueue is idempotent); at-least-once delivery comes from
    lease expiry — an unacked claim becomes claimable again at available_at.
    """
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    raw_document_id: int
    status: str = "queued"  # queued|inflight|done|error
    attempts: int = 0
    available_at: Optional[datetime] = None
    claimed_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    # Operator alert acknowledgement (migration 010): a failed item stays on
    # operator_alerts until acknowledged, retried, or done.
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None


AUDIT_OUTCOMES = ("applied", "refused", "failed")


class OperatorAudit(BaseModel):
    """One attempted operator write action (migration 010) — applied,
    refused (role/scope gate), or failed (domain refusal / error). `target`
    is 'kind:id'; `snapshot_ref` points at the domain's reversibility record
    ('entity_merges:<id>') rather than duplicating it. Review decisions also
    write `labels` rows (005) — audit answers who/what/when, labels feed the
    flywheel."""
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    principal_id: str
    roles: list[str] = Field(default_factory=list)
    action: str
    target: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    outcome: str
    error: Optional[str] = None
    snapshot_ref: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    created_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_outcome(self) -> "OperatorAudit":
        if self.outcome not in AUDIT_OUTCOMES:
            raise ValueError(f"outcome must be one of {AUDIT_OUTCOMES}")
        return self


GOLD_SET_KINDS = ("retrieval", "er", "extraction")
BENCHMARK_AXES = ("a_index", "b_er", "c_embedder", "d_extraction")


class GoldSet(BaseModel):
    """One versioned, immutable gold set (migration 006, methodology §6.3/6.4).

    Editing means a NEW version row — content_hash (sha256 over ordered item
    hashes) is what runs pin. Runs are refused unless status='active';
    floors_met mirrors the §6.2 statistical floors and stamps runs advisory
    when false.
    """
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    kind: str                            # retrieval | er | extraction
    version: str
    status: str = "draft"                # draft | active | retired
    generator: str
    generator_version: str
    item_count: int = 0
    content_hash: Optional[str] = None
    floors_met: bool = False
    spec: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    activated_at: Optional[datetime] = None
    activated_by: Optional[str] = None

    @model_validator(mode="after")
    def _check_kind(self) -> "GoldSet":
        if self.kind not in GOLD_SET_KINDS:
            raise ValueError(f"kind must be one of {GOLD_SET_KINDS}")
        return self


class GoldSetItem(BaseModel):
    """One gold item; `item` shape is kind-specific (methodology §6.3)."""
    id: Optional[int] = None
    gold_set_id: int
    seq: int
    item: dict[str, Any]
    item_hash: str


class PinProfile(BaseModel):
    """A named frozen snapshot of every axis's setting (methodology §7.3) —
    'pinned' as a recorded fact the runner verifies, not a claim."""
    name: str
    profile: dict[str, Any]
    notes: Optional[str] = None
    created_at: Optional[datetime] = None


class BenchmarkRun(BaseModel):
    """One (config x gold set) execution with full provenance (migration 006).

    Written status='running' BEFORE execution and finalized after — a crash
    leaves a visible error row, never a phantom run. metrics carries
    headline_name/headline_value for the leaderboard; aggregates must be
    recomputable from benchmark_run_items.
    """
    id: Optional[int] = None
    tenant_id: str = DEFAULT_TENANT
    axis: str
    config: dict[str, Any]
    pin_profile_name: str
    pin_profile: dict[str, Any]
    gold_set_id: int
    gold_set_hash: Optional[str] = None
    metrics: Optional[dict[str, Any]] = None
    advisory: bool = False
    model_digests: Optional[dict[str, Any]] = None
    ontology_version: Optional[str] = None
    package_version: str
    code_hash: str
    runner_version: str
    hardware: Optional[dict[str, Any]] = None
    wall_ms: Optional[int] = None
    status: str = "running"              # running | ok | error
    error: Optional[str] = None
    notes: Optional[str] = None
    # Supersession (migration 007): a run is never deleted or re-scored, but a
    # structurally-flawed round must not read as a current decision.
    superseded_by_run_id: Optional[int] = None
    superseded_note: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @model_validator(mode="after")
    def _check_axis(self) -> "BenchmarkRun":
        if self.axis not in BENCHMARK_AXES:
            raise ValueError(f"axis must be one of {BENCHMARK_AXES}")
        return self


class BenchmarkRunItem(BaseModel):
    """Per-gold-item outcome; what bootstrap CIs resample and audits replay."""
    id: Optional[int] = None
    run_id: int
    gold_set_item_id: int
    outcome: dict[str, Any]
