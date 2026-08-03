"""Canonical interfaces (ABCs).

FactStore is the pipeline's only doorway to storage. Call sites depend on THIS
interface, never on a concrete backend, so the tenancy model can move from
row-level (today) to schema- or DB-per-tenant by swapping the implementation —
every method already receives the tenant either explicitly or on the model.

The capture-side seams (Build Prompt 2) live here too: SecretsProvider,
RawStore, SourceAdapter, Dispatcher. Same rule — the landing flow depends on
these ABCs, never on OpenBao/SeaweedFS/psycopg directly, so each end can be
swapped (per-tenant vaults, bucket-per-tenant, a real broker) without touching
call sites.

The processing seams (Build Prompt 3) close the set: Parser, Chunker,
Embedder. Stage B (parse -> chunk -> embed) depends on these ABCs, never on
Docling/semchunk/Ollama directly, so the parser can move to another engine,
the chunking policy can be retuned, and the embedding model can be swapped
(schema note: change vector(1024) with it) without touching the flow.

The extraction seams (Build Prompt 4): OntologyBinding, ExtractionStrategy,
Grounder. The extraction flow depends on these ABCs, never on qwen3.6/Ollama
or a hard-coded vocabulary — the ontology is swappable DATA (a new
ontology_versions row yields a new binding, no code change), the extraction
model is swappable behind ExtractionStrategy (the benchmark decides the
final one), and grounding policy is swappable behind Grounder.

The resolution seam (Build Prompt 5): Scorer. The resolution flow depends on
resolve(mention, candidates) -> ResolutionOutcome and nothing else, so the
in-house tiered resolver (Splink + embedding + LLM adjudication) can be
replaced by a whole-engine (e.g. Senzing) without touching callers. Nothing
Splink-specific may leak past this boundary — evidence travels in the
outcome's generic `features` dicts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, ClassVar, Iterator, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from knowledge_hub.models import (
    Chunk,
    Document,
    Entity,
    EntityMention,
    Fact,
    PendingFact,
    QuarantinedExtraction,
    RawDocument,
)


class AnnCandidate(BaseModel):
    """One embedding-blocking hit: a canonical entity near a query vector."""
    entity_id: int
    canonical_name: str
    entity_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    similarity: float  # cosine similarity in [-1, 1]; higher = closer


class StagedPending(BaseModel):
    """Result of stage_pending: db ids for what was written."""
    mention_ids: dict[str, int]  # extraction-local mention key -> entity_mentions.id
    pending_fact_ids: list[int]


class FactStore(ABC):
    """Storage layer for the knowledge hub pipeline (relational + graph)."""

    # -- canonical entities ---------------------------------------------------
    @abstractmethod
    def upsert_entity(self, entity: Entity) -> int:
        """Insert (id is None) or update (id set) a canonical entity, its
        embedding, and its aliases. Returns the entity id. Deduplication is
        the resolver's job, not this method's — two distinct entities may
        share a name (e.g. two people called John Smith)."""

    @abstractmethod
    def ann_candidates(
        self,
        tenant_id: str,
        embedding: Sequence[float],
        entity_type: str,
        k: int = 20,
    ) -> list[AnnCandidate]:
        """Embedding blocking for the resolver: pgvector cosine nearest
        neighbors over entities.embedding, filtered by tenant + entity_type,
        best first. Entities without an embedding never match."""

    # -- facts ----------------------------------------------------------------
    @abstractmethod
    def write_facts(self, facts: Sequence[Fact]) -> list[int]:
        """Persist resolved facts (subject/object are canonical entity ids by
        now). Relational rows are THE truth; the AGE graph projection is
        RETIRED (Build Prompt 9 — off behind settings.project_to_age, edges
        known-stale, see AGE_DORMANT.md). Returns fact ids in order."""

    # -- extraction handoff ---------------------------------------------------
    @abstractmethod
    def stage_pending(
        self,
        mentions: Mapping[str, EntityMention],
        facts: Sequence[PendingFact],
    ) -> StagedPending:
        """Write entity_mentions + candidate pending_facts atomically, all with
        resolution_status='pending'. `mentions` is keyed by extraction-local
        mention keys; fact subject_ref/object_ref values that equal a key are
        rewritten to 'mention:<id>' as the mentions are persisted. Refs already
        in 'entity:<id>' / 'mention:<id>' form pass through untouched."""

    # -- document tiers (needed by ingestion stages + round-trip tests) -------
    @abstractmethod
    def insert_document(self, document: Document) -> int:
        """Insert a superparent document row; returns its id."""

    @abstractmethod
    def insert_chunks(self, chunks: Sequence[Chunk]) -> list[int]:
        """Insert parent/child chunks (idempotent per (tenant, content_hash));
        returns ids in input order."""


# =============================================================================
# Capture path (Build Prompt 2): Secrets · RawStore · SourceAdapter · Dispatcher
# =============================================================================

# ------------------------------------------------------------------ secrets --
class SecretsError(Exception):
    """Base for credential failures. Carries WHERE (tenant/source/path), never
    a secret value. The capture flow catches this to degrade the one affected
    source instead of failing the tenant."""

    def __init__(self, tenant_id: str, source_ref: str, detail: str = ""):
        self.tenant_id, self.source_ref = tenant_id, source_ref
        super().__init__(
            f"{type(self).__name__} for source {source_ref!r} (tenant {tenant_id!r})"
            + (f": {detail}" if detail else ""))


class SecretNotFound(SecretsError):
    """No credential is provisioned at this source's vault path."""


class SecretAccessDenied(SecretsError):
    """The vault refused access (bad/expired token, missing policy)."""


class OutboundRequest:
    """Mutable carrier for the connection/auth parameters of one outbound call
    to a source system (SFTP connect kwargs, HTTP headers, ...).

    SecretsProvider.inject_credential attaches secret fields via
    `attach_secret`; keys attached that way are tracked and MASKED by
    repr()/str(), so the object itself can appear in logs and exception
    messages without leaking values. Transports unpack `.params` directly
    (e.g. `client.connect(**request.params)`) — callers must never log the
    dict itself.
    """

    __slots__ = ("params", "_secret_keys")

    def __init__(self, **params: Any):
        self.params: dict[str, Any] = dict(params)
        self._secret_keys: set[str] = set()

    def attach_secret(self, key: str, value: Any) -> None:
        self.params[key] = value
        self._secret_keys.add(key)

    @property
    def secret_keys(self) -> frozenset[str]:
        return frozenset(self._secret_keys)

    def __repr__(self) -> str:
        shown = {k: ("***" if k in self._secret_keys else v)
                 for k, v in self.params.items()}
        return f"OutboundRequest({', '.join(f'{k}={v!r}' for k, v in shown.items())})"

    __str__ = __repr__


class SecretsProvider(ABC):
    """Credential-injection seam (§8.1b). Credentials live per tenant at a
    per-source path; adapters receive them ATTACHED to an OutboundRequest and
    never handle raw values in their own logic. `get_secret` is the rare,
    explicit escape hatch. Implementations must never log secret values or
    include them in exception messages."""

    @abstractmethod
    def inject_credential(
        self, tenant_id: str, source_ref: str, request: OutboundRequest
    ) -> None:
        """Attach the credential for (tenant, source) to `request` without
        returning it. Raises SecretNotFound / SecretAccessDenied."""

    @abstractmethod
    def get_secret(self, tenant_id: str, source_ref: str) -> Mapping[str, Any]:
        """Escape hatch: return the raw secret fields. Use only where
        injection genuinely cannot work; every call site is an audit point."""


class CredentialRotator(ABC):
    """Write-path seam for sources whose credentials ROTATE as a side effect
    of normal use (OAuth refresh tokens that re-issue on every refresh —
    QuickBooks Online is the forcing case). Deliberately a SEPARATE ABC from
    SecretsProvider: reading credentials stays the default capability, and
    only adapters that declare rotation ever receive a write path. Every
    vault write from capture-flow code goes through this one auditable
    method — there is no other sanctioned write path.

    Persist-before-use is the caller's contract: a rotated token must be
    written back BEFORE the new access token is used, so a crash can never
    leave the only valid refresh token unpersisted (a lost rotation locks
    the connector out until a human re-consents)."""

    @abstractmethod
    def rotate_credential(self, tenant_id: str, source_ref: str,
                          updates: Mapping[str, Any]) -> None:
        """MERGE `updates` into the stored credential for (tenant, source):
        named fields are replaced, unnamed fields keep their stored values
        (a refresh-token rotation must never drop the client_id beside it).
        Raises SecretsError family on any vault failure — callers must treat
        that as fatal for the run (degrade), never proceed with an
        unpersisted rotation."""


# ---------------------------------------------------------------- raw store --
class RawStore(ABC):
    """Write-once landing zone for original bytes, tenant-scoped.

    Immutability is verified, not assumed (SeaweedFS upstream #8350: a bucket
    can report object-lock 'Enabled' while enforcing nothing). Implementations
    must return version-pinned URIs so a landed object stays retrievable even
    if something later writes over its key.
    """

    @abstractmethod
    def put(self, tenant_id: str, content: bytes, meta: Mapping[str, Any]) -> str:
        """Land `content` write-once under the tenant's bucket/prefix with
        object-lock retention set on the object; returns the (version-pinned)
        URI. Content-addressed: re-putting identical bytes returns the
        existing object's URI without writing."""

    @abstractmethod
    def get(self, uri: str) -> bytes:
        """Fetch the exact landed bytes a `put` URI points at."""

    @abstractmethod
    def exists(self, tenant_id: str, content_hash: str) -> Optional[RawDocument]:
        """Return the existing landing record for these bytes (idempotency
        probe used by the capture flow), or None if never landed."""


# ---------------------------------------------------------------- adapters --
class CursorInvalid(Exception):
    """The persisted cursor for (tenant, source) is no longer usable at the
    source (e.g. an expired Microsoft Graph delta token — HTTP 410
    `resyncRequired`, or an unparseable stored state). Adapters raise it
    instead of guessing; the capture flow treats it as a RESYNC signal —
    clear checkpoints, re-run backfill — which is safe end to end because
    re-landing is a content-hash no-op and changed items version up.
    Carries WHERE, never token values."""

    def __init__(self, tenant_id: str, source_ref: str, detail: str = ""):
        self.tenant_id, self.source_ref = tenant_id, source_ref
        super().__init__(
            f"cursor invalid for source {source_ref!r} (tenant {tenant_id!r})"
            + (f": {detail}" if detail else ""))


class AclGrant(BaseModel):
    """One normalized permission grant, principals BY REFERENCE (§2 #9):
    a group grant carries the group's stable id, never a flattened member
    list — membership resolution happens at serving time, so a membership
    change never requires re-ingestion. `roles` are normalized to
    read|write|owner where the source's vocabulary maps cleanly; unknown
    role tokens pass through lowercased (the faithful originals are always
    in SourceAcl.raw)."""
    principal_type: Literal["user", "group", "site_group", "application",
                            "device", "link", "domain", "anyone"]
    principal_id: Optional[str] = None  # stable id at the source/IdP; None for 'anyone'
    display: Optional[str] = None       # for humans/audit only, never enforcement
    roles: list[str] = Field(default_factory=list)
    via: Literal["direct", "inherited", "link"] = "direct"
    detail: dict[str, Any] = Field(default_factory=dict)  # link scope/expiry, inherited-from ref, ...


class SourceAcl(BaseModel):
    """The per-item ACL captured at acquisition — what downstream turns into
    the fact `security_label` (permission inheritance, §2 #9). `model` names
    the normalization schema (e.g. 'posix.v1', 'msgraph.driveItem.v1') so
    consumers can interpret `grants` without per-source if-ladders; `raw`
    preserves the untransformed source payload, which is irreplaceable
    (§8.1e) and the arbiter whenever normalization loses a nuance."""
    model: str = "opaque.v0"
    owner: Optional[str] = None
    grants: list[AclGrant] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class SourceItem(BaseModel):
    """One observed change at the source: an acquired file/object
    (change='upsert', the default) or an explicit deletion signal
    (change='tombstone').

    Tombstones follow §8.1g: they are emitted only for AUTHORITATIVE delete
    signals (a delta `@removed` entry, a CDC delete, a webhook) — never
    inferred from an item's absence in a poll, where a permission or filter
    change masquerades as a mass delete. A tombstone carries no content;
    its `mtime` is when the deletion was OBSERVED (sources rarely say when
    it happened).

    `content` is the faithful, untransformed raw bytes. `cursor` is a resume
    token meaning \"this item is safely landed — resume from me\"; the capture
    flow checkpoints it to the source registry after each item, which is what
    makes a large pull crash-resumable (see SourceAdapter for token flavors).
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    native_id: str                      # stable id at the source
    change: Literal["upsert", "tombstone"] = "upsert"
    content: bytes = b""                # empty for tombstones
    mime_type: Optional[str] = None
    size: int = 0
    mtime: datetime
    source_acl: SourceAcl = Field(default_factory=SourceAcl)
    native_metadata: dict[str, Any] = Field(default_factory=dict)
    cursor: str


class SourceAdapter(ABC):
    """Pulls items out of one source system. Proven by the filesystem pilot
    and the Microsoft Graph family (extend, don't change — later adapters
    fill in this template, they don't reopen it).

    Cursor contract (both iterators): every yielded item carries a `cursor`
    checkpoint token, and the guarantee an adapter must honor is
    AT-LEAST-ONCE — resuming from any checkpointed token must never LOSE an
    item; it may re-yield already-landed items (replay is safe: landing is
    content-hash idempotent and dispatch is idempotent). Adapters declare
    their token flavor via `cursor_ordering`:

      * 'ordered' — string order equals scan order (the filesystem adapter's
        f\"{mtime_ns:020d}:{path}\"). Resume yields strictly after the token,
        and the registry defends the checkpoint monotonically (GREATEST).
      * 'opaque'  — source-issued state (Graph delta/nextLink URLs, a
        serialized multi-drive map). No order exists; the registry stores
        the latest checkpoint verbatim (last-write-wins), which is safe
        because the adapter is the checkpoint's only writer and yields in
        sweep order. Resume may replay up to the in-flight page.

    Adapters whose high-water mark only materializes at the END of a sweep
    (Graph hands out the deltaLink on the final page) return it from
    `final_cursor()`; the capture flow persists it after the iterator is
    exhausted — including for a sweep that yielded zero items, so the token
    keeps advancing instead of quietly aging toward expiry."""

    # Adapter kind; becomes raw_documents.source_system.
    source_system: ClassVar[str]
    cursor_ordering: ClassVar[Literal["ordered", "opaque"]] = "ordered"

    def __init__(self, source_ref: str, credential_ref: Optional[str] = None):
        # `source_ref` is the registry key for this concrete source.
        # `credential_ref` is the OpenBao path leaf
        # (tenants/<tenant>/sources/<credential_ref>) and defaults to
        # source_ref; sibling sources of one auth family (msgraph-files +
        # msgraph-mail sharing one Entra app) point it at a single shared
        # credential so rotation stays a one-place edit.
        self.source_ref = source_ref
        self.credential_ref = credential_ref or source_ref
        self._prepared_tenant: Optional[str] = None

    def prepare(self, tenant_id: str, secrets: Optional[SecretsProvider]) -> None:
        """Acquire whatever access the source needs. Records the prepared
        tenant for `require_prepared`, then delegates to `_prepare` —
        subclasses override THAT, not this. Raises SecretsError when a
        needed credential is missing/denied; the capture flow catches it
        and degrades this source only."""
        self._prepared_tenant = tenant_id
        self._prepare(tenant_id, secrets)

    def _prepare(self, tenant_id: str, secrets: Optional[SecretsProvider]) -> None:
        """Override point for credential acquisition (via
        `secrets.inject_credential`), connections, ... Default: no-op for
        credential-less sources (the pilot filesystem adapter)."""

    def require_prepared(self, tenant_id: str) -> None:
        """Guard for credentialed adapters' iterators: an adapter prepared
        for one tenant must never pull for another — cached credentials
        crossing the tenant boundary is the one unforgivable leak. Stateless
        adapters may skip this; anything holding a token must call it."""
        if self._prepared_tenant != tenant_id:
            raise RuntimeError(
                f"adapter {self.source_ref!r} prepared for tenant "
                f"{self._prepared_tenant!r} but asked to pull for "
                f"{tenant_id!r} — one adapter instance serves one tenant")

    @abstractmethod
    def backfill(
        self, tenant_id: str, resume_after: Optional[str] = None
    ) -> Iterator[SourceItem]:
        """Yield EVERY item in the source (full pull). `resume_after` is a
        cursor token from a checkpoint of an interrupted backfill: resume
        there (ordered adapters: strictly after it; opaque adapters: from
        the serialized state, replaying at most the in-flight page)."""

    @abstractmethod
    def incremental(
        self, tenant_id: str, cursor: Optional[str]
    ) -> Iterator[SourceItem]:
        """Yield items changed since `cursor` (the high-water mark of the
        last completed run), including explicit deletions as tombstone items
        where the source reports them. cursor=None means everything. Raises
        CursorInvalid when the source rejects the token as expired/unusable
        — the capture flow resyncs instead of silently starting over."""

    def final_cursor(self) -> Optional[str]:
        """The end-of-sweep high-water mark (e.g. the new deltaLink), or None
        for adapters whose per-item cursors already carry it. Read by the
        capture flow after the iterator is exhausted; meaningless before."""
        return None

    def stats(self) -> dict[str, Any]:
        """Post-run diagnostics for the run result (throttle counts, skipped
        items, ...). Values must be safe to log — no secrets, no content."""
        return {}


# =============================================================================
# Processing stages (Build Prompt 3): Parser · Chunker · Embedder
# =============================================================================

# ------------------------------------------------------------------ parser --
class ParseError(Exception):
    """Raw bytes could not be turned into a Document. Carries WHERE (tenant/
    raw doc), never the content. The queue consumer nacks on this, so the
    item redelivers instead of vanishing."""

    def __init__(self, tenant_id: str, raw_document_id: Optional[int],
                 detail: str = ""):
        self.tenant_id, self.raw_document_id = tenant_id, raw_document_id
        super().__init__(
            f"parse failed for raw_document id={raw_document_id} "
            f"(tenant {tenant_id!r})" + (f": {detail}" if detail else ""))


class Parser(ABC):
    """Landed raw bytes -> superparent Document + clean text (Stage B entry).

    `content` is always the exact landed bytes fetched via the version-pinned
    raw_uri — parsers never re-fetch from the source. Declarations ride on
    `raw.native_metadata` (`data_track`, `doc_type` — the manifest tag merged
    in by the processing flow); per §8.1a they are CLAIMS, so parse() also
    runs cheap shape detection and records both views on Document.metadata
    (`declared_data_track` / `detected_data_track` / `detection_confident`)
    for the flow to arbitrate — the parser itself never enqueues reviews.
    """

    @abstractmethod
    def parse(self, raw: RawDocument, content: bytes) -> Document:
        """Build the superparent Document: doc_type + data_track, and title /
        author / source_timestamp preferring the captured native_metadata,
        falling back to what the parse derives. Raises ParseError."""

    @abstractmethod
    def extract_text(self, raw: RawDocument, content: bytes) -> str:
        """Clean text with heading/section structure preserved (Markdown-style
        `#`-headings), the chunker's input. Char offsets on chunks anchor into
        THIS text. Raises ParseError."""


# ----------------------------------------------------------------- chunker --
class Chunker(ABC):
    """Document + extracted text -> the parent/child chunk tiers.

    The superparent tier is the documents row itself (the Parser's output);
    chunk() produces the two chunk tiers below it: parents (section/procedure
    = the extraction unit) and children (~small passages = the embed/cite
    unit, each carrying a contextual_prefix to be prepended at embed time).

    Contract: the returned list is in document order with each parent
    IMMEDIATELY followed by its own children — parent ids don't exist before
    insert, so linkage travels by position and the persisting flow rewrites
    child.parent_chunk_id as parents land. Non-prose data tracks return []
    (the router sends those documents to the structured strategy instead).
    content_hash must be deterministic for (document, position, text) so
    re-chunking the same document replays as a no-op through insert_chunks.
    """

    @abstractmethod
    def chunk(self, document: Document, text: str) -> list[Chunk]:
        """Produce parent+child chunks for a prose-track document (document.id
        must already be set — hashes and linkage need it), [] for non-prose."""


# ---------------------------------------------------------------- embedder --
class EmbeddingError(Exception):
    """The embedding backend failed or returned vectors of the wrong shape."""


class Embedder(ABC):
    """Text -> dense vectors. `dim` must match the schema's vector(1024)
    columns — changing models means changing the schema with it (same
    commit), so implementations validate every returned vector against
    `dim` instead of trusting the backend."""

    model: str  # e.g. "bge-m3"; becomes chunks.embedding_model
    dim: int    # e.g. 1024;    must equal the vector(N) column dimension

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts (batched internally), preserving order. Every vector
        has exactly `dim` floats. Raises EmbeddingError."""


# =============================================================================
# Extraction stage (Build Prompt 4): OntologyBinding · ExtractionStrategy ·
# Grounder
# =============================================================================

class ExtractionError(Exception):
    """A unit could not be extracted. Carries WHERE (tenant/document/chunk),
    never content. The queue consumer nacks on this, so the item redelivers
    instead of vanishing."""

    def __init__(self, tenant_id: str, document_id: Optional[int],
                 chunk_id: Optional[int] = None, detail: str = ""):
        self.tenant_id, self.document_id, self.chunk_id = \
            tenant_id, document_id, chunk_id
        where = f"document id={document_id}"
        if chunk_id is not None:
            where += f" chunk id={chunk_id}"
        super().__init__(f"extraction failed for {where} "
                         f"(tenant {tenant_id!r})"
                         + (f": {detail}" if detail else ""))


class DigestEntry(BaseModel):
    """One carried-forward entity in the document digest: an entity
    established by an earlier unit of the SAME document, kept visible so
    later units resolve coreference (pronouns, definite descriptions) to the
    same staged mention in-pass — no separate coref step. `refs` counts how
    often facts referenced it; the most-referenced entities stay sticky when
    the digest is truncated for the prompt."""
    key: str                 # digest-local key, e.g. 'e3'
    mention_id: int          # the staged entity_mentions.id it refers to
    surface_text: str
    entity_type: str
    refs: int = 0


class ExtractionUnit(BaseModel):
    """What one ExtractionStrategy call consumes.

    Prose track: `chunk` is the parent chunk (the extraction unit) and `text`
    its content; `digest` carries the document's established entities.
    Structured track: `chunk` is None, `payload` is the exact landed bytes,
    and `config` carries the manifest's structured_map (from the source
    registry / native_metadata)."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    document: Document
    source_system: str
    chunk: Optional[Chunk] = None
    text: str = ""
    payload: Optional[bytes] = None
    digest: list[DigestEntry] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class CandidateEntity(BaseModel):
    """A NEW entity observed in this unit (not already in the digest), keyed
    by an extraction-local key that facts in the same result reference.
    `extracted_keys` holds only CONFIDENTLY extracted identifiers (email/
    domain/tax_id via deterministic patterns; source-native keys for SoR
    rows) — match-normalization stays the resolver's job."""
    key: str
    surface_text: str
    entity_type: str
    extracted_keys: dict[str, Any] = Field(default_factory=dict)


class CandidateFact(BaseModel):
    """One extracted assertion, pre-grounding and pre-envelope. subject/object
    keys reference CandidateEntity.key or DigestEntry.key; the flow rewrites
    them to mention refs as it stages. Predicates are already normalized
    toward the ontology (unambiguous surface variants only) but NOT yet
    guaranteed bound — binding was checked by the strategy, which quarantines
    unbound candidates instead of returning them here."""
    subject_key: str
    predicate: str
    object_key: Optional[str] = None
    object_literal: Optional[str] = None
    evidence: str = ""       # exact quote from the unit text ("" for SoR facts)
    confidence: float = 1.0
    locator: Optional[dict[str, Any]] = None  # {"row":..,"col":..} for SoR


class ExtractionStats(BaseModel):
    """Per-unit observability (benchmark inputs): token counts, wall-clock,
    and how many capped repairs the model needed."""
    prompt_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    wall_ms: Optional[int] = None
    repairs: int = 0


class ExtractionResult(BaseModel):
    """A strategy's output for one unit: ontology-bound candidates plus
    everything that was NOT bound or would not validate — quarantined, never
    silently dropped and never in facts."""
    entities: list[CandidateEntity] = Field(default_factory=list)
    facts: list[CandidateFact] = Field(default_factory=list)
    quarantined: list[QuarantinedExtraction] = Field(default_factory=list)
    stats: ExtractionStats = Field(default_factory=ExtractionStats)


class OntologyBinding(ABC):
    """The swappable vocabulary, generated ENTIRELY from an ontology_versions
    row: prompt schema, vocabulary checks, and surface-variant normalization
    all derive from the row's definition JSONB. A new ontology row -> a new
    binding, no code change."""

    version: str  # the ontology_versions.version this binding was built from

    @abstractmethod
    def output_schema(self, data_track: str) -> dict[str, Any]:
        """The JSON schema the extractor's output is constrained to (Ollama
        `format=`). Constrains STRUCTURE only — predicate/entity-type fields
        stay free strings so off-ontology attempts survive to the quarantine
        (hard-constraining the enum would blind us to what the ontology is
        missing)."""

    @abstractmethod
    def is_entity_type(self, entity_type: str) -> bool:
        """Vocabulary check for one entity type."""

    @abstractmethod
    def is_predicate(self, predicate: str) -> bool:
        """Vocabulary check for one predicate."""

    def is_bound(self, entity_type: str, predicate: str) -> bool:
        """Both halves of the vocabulary check."""
        return self.is_entity_type(entity_type) and self.is_predicate(predicate)

    @abstractmethod
    def normalize_predicate(self, raw: str) -> Optional[tuple[str, bool]]:
        """Deterministically map a raw predicate to (canonical, swap): exact
        ontology predicates pass through; unambiguous surface variants from
        the ontology's alias data map (swap=True flips subject/object, e.g.
        'owned by' -> owns). Genuine unknowns return None -> quarantine."""

    @abstractmethod
    def prompt_vocabulary(self) -> str:
        """The vocabulary block for the extraction prompt: entity types and
        predicates WITH the ontology's per-type examples (examples are
        ontology data, not code)."""


class ExtractionStrategy(ABC):
    """One way to turn an ExtractionUnit into candidates. The LLM strategy
    (prose/SOP/comms) and the deterministic StructuredMap strategy (SoR/
    tabular) both live behind this seam; the router picks by data_track."""

    # Stamped into every fact/mention envelope this strategy produces.
    extractor: ClassVar[str]
    version: str  # model digest for LLM strategies; code version otherwise

    @abstractmethod
    def extract(self, unit: ExtractionUnit) -> ExtractionResult:
        """Extract candidates from one unit. Raises ExtractionError when the
        unit cannot be processed at all (backend down, undecodable payload);
        per-item problems go to ExtractionResult.quarantined instead."""


class GroundingResult(BaseModel):
    """Deterministic verdict on one fact's evidence span."""
    status: str  # pass | span_missing | components_missing | construction
    char_start: Optional[int] = None  # into the DOCUMENT's extracted text
    char_end: Optional[int] = None
    note: Optional[str] = None

    @property
    def passed(self) -> bool:
        return self.status in ("pass", "construction")


class Grounder(ABC):
    """Deterministic, reproducible span verification — no LLM. Failure is a
    flag, never a rejection (legitimate paraphrase exists): the flow lowers
    confidence and routes to review."""

    @abstractmethod
    def ground(self, evidence: str, components: Sequence[str],
               source_text: str, base_offset: int = 0) -> GroundingResult:
        """Verify `evidence` exists in `source_text` (exact, then fuzzy) and
        that every string in `components` (subject/object surfaces or the
        literal) appears within it. Offsets in the result are source-local
        positions shifted by `base_offset` (the unit's char_start in the
        document text)."""


# =============================================================================
# Resolution stage (Build Prompt 5): Scorer
# =============================================================================

class BlockedCandidate(BaseModel):
    """One blocking hit handed to the Scorer: a canonical entity that reached
    the candidate set via at least one blocking path. `blocks` names the
    paths that hit ('key' = shared extracted-key value, 'ann' = pgvector
    cosine over ann_candidates, 'name' = pg_trgm name/alias similarity);
    `cosine` is set only when the ann path ran."""
    entity_id: int
    canonical_name: str
    entity_type: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    cosine: Optional[float] = None
    blocks: list[str] = Field(default_factory=list)


class ScoredCandidate(BaseModel):
    """One (mention, entity) pair after scoring: what match_candidates rows
    are made of. `features` carries the deterministic evidence — key_overlap /
    name_sim / cosine / corroboration / FS weights — in engine-neutral form."""
    entity_id: int
    score: float                # calibratable match score in [0, 1]
    method: str                 # deterministic_key|probabilistic|embedding|llm
    tier: str                   # t0 | t1 | t1b
    band: str                   # high | gray | low (vs resolution_policy)
    features: dict[str, Any] = Field(default_factory=dict)


class ResolutionOutcome(BaseModel):
    """The Scorer's verdict for one mention. decision:
      * 'resolved'   — attach the mention to entity_id;
      * 'new_entity' — no acceptable match; caller creates a new entity
                       (entity_id stays None here);
      * 'review'     — a human owns it (gray band, key conflict, missing
                       corroboration, multiple high candidates, ...).
    Bias to under-merge is the contract: uncertain outcomes must be
    'new_entity' or 'review', never a silent 'resolved'."""
    decision: str               # resolved | new_entity | review
    entity_id: Optional[int] = None
    tier: str                   # t0 | t1 | t1b | none
    method: str                 # deterministic_key|probabilistic|embedding|llm|none
    score: Optional[float] = None
    band: Optional[str] = None
    reason: Optional[str] = None
    features: dict[str, Any] = Field(default_factory=dict)
    candidates: list[ScoredCandidate] = Field(default_factory=list)


class Scorer(ABC):
    """Stage D's swappable core: score one mention against its blocked
    candidates and return a banded verdict. The tiered pilot implementation
    (deterministic keys -> Splink -> embedding+LLM, corroboration boost,
    resolution_policy banding) lives behind this; a whole-engine replacement
    (Senzing et al.) plugs in here without the flow changing. Implementations
    read resolution_policy as DATA and must bias to under-merge."""

    version: str  # stamped into entity_mentions.resolver_version + decisions

    def prime(self, tenant_id: str, mentions: Sequence[EntityMention]) -> None:
        """Optional batch hook called once per sweep before resolve() calls:
        batch-oriented implementations (Splink training, blocking caches)
        prepare here. Default: no-op — resolve() must work without it."""

    @abstractmethod
    def resolve(self, mention: EntityMention,
                candidates: Sequence[BlockedCandidate]) -> ResolutionOutcome:
        """Score `mention` against `candidates` and band the best score per
        resolution_policy. Never mutates storage — deciding is the Scorer's
        job, applying (mention updates, entity creation, match_candidates,
        labels) is the flow's."""


# --------------------------------------------------------------- dispatcher --
class Dispatcher(ABC):
    """Capture -> processing handoff. Enqueues a REFERENCE (never payload),
    at-least-once: a dispatched document is guaranteed to be delivered to a
    consumer at least once; duplicate deliveries are possible and consumers
    must be idempotent (they are — landing already is)."""

    @abstractmethod
    def dispatch(self, tenant_id: str, raw_document_id: int) -> int:
        """Enqueue the landed document for downstream processing; returns the
        queue message id. Idempotent per (tenant, raw_document_id) — a
        crash-resume re-dispatch never duplicates the record."""
