"""Knowledge Hub — pilot package.

Persistence layer (Build Prompt 1) is real: FactStore (interfaces.py),
PostgresFactStore (factstore_pg.py), and the pipeline persistence slice
(pipeline.py). Capture path (Build Prompt 2) is real: SecretsProvider/
RawStore/SourceAdapter/Dispatcher seams (interfaces.py) with OpenBao,
SeaweedFS S3, filesystem, and Postgres-outbox implementations, orchestrated
by CaptureService (capture.py). Processing Stage B for the prose/SOP track
(Build Prompt 3) is real: Parser/Chunker/Embedder seams (interfaces.py) with
Docling, section/passage chunking, and Ollama bge-m3 implementations,
orchestrated by ProcessingService (processing.py). Extraction Stage C
(Build Prompt 4) is real: OntologyBinding/ExtractionStrategy/Grounder seams
(interfaces.py) with the qwen3.6 joint strategy, the deterministic
structured map, and span grounding, orchestrated by ExtractionService
(extraction.py). Resolution Stage D (Build Prompt 5) is real: the Scorer
seam (interfaces.py) with the tiered Splink/embedding/LLM implementation
(scoring_tiered.py), orchestrated by ResolutionService (resolution.py) —
this closes the source -> fact vertical slice. Serving contracts (Build
Prompt S1) are real: the FactEnvelope/EvidenceEnvelope response shapes,
UncertaintyState, the ChokePoint/OperationRegistry/RetrievalService/
ServingService seams, and the envelope-usage instrumentation all live in
serving.py — S2–S5 implement those seams and add no new shapes. The choke
point (Build Prompt S2) is real: server-side identity (CredentialResolver/
OpenBaoCredentialResolver) and the mandatory permission gate + serve-path
read gateway (PostgresChokePoint) live in choke_point.py — the EXTERNAL
serve path's only door to Postgres; internal pipeline components keep using
PostgresFactStore. The operation surface (Build Prompt S3) is real: the
declarative registry + marker-enforcing generator, the base fact/graph ops
(get_facts, get_by_key, get_entity, neighbors, facts_citing, retrieve), and
the fixed-plan composite mechanism + entity_dossier live in operations.py —
every registered op transits the S2 gate. The retrieval path (Build Prompt
S4) is real: DenseRetrievalService (bge-m3 prefix-free -> gated dense ANN ->
rerank seam -> EvidenceEnvelopes, grounded facts opt-in via S3's
facts_citing; hybrid fusion dormant behind retrieval_mode) lives in
retrieval.py — served config is exactly the Axis-C decision. The API
service (Build Prompt S5) is real: KnowledgeHubServingService (the S1
ServingService seam assembled from S2–S4) + the framework-free HTTP+JSON
boundary (ServingApp / make_server / build_serving_app), registry-generated
endpoints, boundary auth via OpenBaoCredentialResolver, usage
instrumentation to a durable JSONL sink, and per-endpoint latency
percentiles against the §4 budget all live in service_http.py — agents live
OUTSIDE this boundary and hold only a credential. The deployment layer
(§8.9, net-new item 1) is real: the read-only environment probe
(deploy_probe.py), profile presets + fail-closed qualification + plan
resolution (deploy_profiles.py), and the `khctl` probe→plan→apply→verify
CLI (deploy_cli.py) — profiles are kit data (profiles.toml) including
per-offering unseal-key custody defaults; model transport + custody are
DECIDED (DEPLOY_NOTES.md). Verification primitives live in checks.py (one
library, two runners: check_stack.py = the pilot gate, khctl verify = the
plan-driven field verifier incl. the §8.8 side-door audit and the Shape-B
remote-inference check); apply is real (deploy_apply.py: nine idempotent
plan-driven phases — kit hash verification, env install, ours-services
compose incl. the production raft OpenBao override, psycopg schema replay
that also works on adopted client Postgres, the init/unseal custody
ceremony, kit model-store install, tenant principal bootstrap — with
--dry-run as the walk-in rehearsal). The kit lifecycle is real
(deploy_kit.py: `khctl make-kit` assembles the signed air-gap SSD image —
allowlisted bundle + package source + linux wheelhouse + docker-saved
images + hash-verified ollama model store + signed manifest, layout derived
from what apply reads; `khctl verify-kit` is the arrival gate: hashes +
signature + no unlisted files + the no-secrets guard). Kit signing is
enforced (deploy_kit.TRUSTED_PUBKEYS = the verifier's own versioned trust
anchor, never a key from the kit; make-kit requires a signature or the
self-recording --allow-unsigned dev override; verify-kit checks the
signature FIRST, then trusts the manifest's hashes; install-ubuntu.sh
carries the same anchor for field installs — org-2026 active since
2026-07-24, the dev throwaway retired). The operator write API (Build
Prompt 19) is real: the write-twin of the read choke point lives in
operator_http.py (OperatorGate = the single tenant-scoped, deny-by-default,
audited write dispatch; fixed WriteOperation registry — review resolution
via the resolver's reversible merges + flywheel labels, ingestion control,
alert acknowledgement, credential-free source management; OperatorApp /
build_operator_app on the same stdlib HTTP plumbing) with migration 010
(operator_audit + queue ack columns + the operator_alerts view) — the read
serving boundary stays provably read-only and untouched. The operator UI
(Build Prompt 20) is real: operator-shaped READ endpoints (monitor /
activity / reviews listing / candidate-pair evidence, operator_reads.py —
tenant + role enforced, structurally read-only, closing the UI's DB side
door) and the two day-one surfaces rebuilt from Design's v4 spec in vanilla
no-build HTML/JS (knowledge_hub/operator_ui/, served at /ui from the
operator service itself — single origin, air-gapped, ships inside the
package/kit; reads via the operator reads, decisions via the BP19 write
ops). Keep `models.py`
in lock-step with
`knowledge_hub_baseline_schema.sql` AND `migrations/*.sql` per SETUP.md —
same commit, always.
"""

__version__ = "0.29.0"

from knowledge_hub.choke_point import (
    CredentialResolver,
    EnforcementRefused,
    OpenBaoCredentialResolver,
    PostgresChokePoint,
    PrincipalUnresolvable,
    UnenforcedQuery,
)
from knowledge_hub.config import Settings, settings
from knowledge_hub.deploy_probe import ProbeReport, run_probe
from knowledge_hub.deploy_profiles import (
    DeployPlan,
    PlanError,
    load_profiles,
    resolve_plan,
)
from knowledge_hub.operations import (
    CompositeResult,
    CompositeSpec,
    CompositeStep,
    InProcessOperationCatalog,
    OperationCallError,
    OperationGenerator,
    OperationRejected,
    OperationSpec,
    ParamBinding,
    ParamSpec,
    StepResult,
    TraceEntry,
    base_operation_specs,
    entity_dossier_spec,
    fact_template,
    register_serving_defaults,
)
from knowledge_hub.interfaces import (
    AclGrant,
    AnnCandidate,
    BlockedCandidate,
    Chunker,
    CursorInvalid,
    Dispatcher,
    Embedder,
    EmbeddingError,
    FactParser,
    FactStore,
    OutboundRequest,
    ParsedFact,
    ParseError,
    Parser,
    RawStore,
    ResolutionOutcome,
    Scorer,
    ScoredCandidate,
    SecretAccessDenied,
    SecretNotFound,
    SecretsError,
    SecretsProvider,
    SourceAcl,
    SourceAdapter,
    SourceItem,
    StagedPending,
)
from knowledge_hub.plugins import (
    EXTRACTION_STRATEGIES,
    FACT_PARSERS,
    PARSERS,
    BoundaryViolation,
    PluginError,
    PluginRegistry,
)
from knowledge_hub.retrieval import (
    DenseRetrievalService,
    PassThroughReranker,
    Reranker,
)
from knowledge_hub.operator_reads import OperatorReadService
from knowledge_hub.operator_http import (
    OperatorApp,
    OperatorGate,
    OperatorService,
    UnknownWriteOperation,
    WriteCallError,
    WriteOperation,
    WriteOperationRejected,
    WriteOutcome,
    WriteParamSpec,
    WriteRefused,
    build_operator_app,
    register_operator_defaults,
)
from knowledge_hub.service_http import (
    JsonlUsageRecorder,
    KnowledgeHubServingService,
    LatencyStats,
    ServingApp,
    build_serving_app,
    make_server,
)
from knowledge_hub.serving import (
    ChokePoint,
    EntityRef,
    EnvelopeUsage,
    EvidenceEnvelope,
    FactEnvelope,
    FilteredQuery,
    Operation,
    OperationRegistry,
    Principal,
    ProvenanceSpine,
    RetrievalQuery,
    RetrievalService,
    RetrievalSignal,
    ServingResponse,
    ServingService,
    UncertaintyState,
    UnknownOperation,
    UsageRecorder,
    UsageTracker,
)

__all__ = [
    "Settings", "settings", "__version__",
    "AnnCandidate", "FactStore", "StagedPending",
    "SecretsProvider", "SecretsError", "SecretNotFound", "SecretAccessDenied",
    "OutboundRequest", "RawStore", "SourceAdapter", "SourceItem", "Dispatcher",
    "SourceAcl", "AclGrant", "CursorInvalid",
    "Parser", "ParseError", "Chunker", "Embedder", "EmbeddingError",
    "FactParser", "ParsedFact",
    "PluginRegistry", "PluginError", "BoundaryViolation",
    "PARSERS", "FACT_PARSERS", "EXTRACTION_STRATEGIES",
    "Scorer", "BlockedCandidate", "ScoredCandidate", "ResolutionOutcome",
    "ProvenanceSpine", "EntityRef", "FactEnvelope", "EvidenceEnvelope",
    "RetrievalSignal", "UncertaintyState",
    "Principal", "RetrievalQuery", "FilteredQuery", "ChokePoint",
    "CredentialResolver", "OpenBaoCredentialResolver", "PostgresChokePoint",
    "EnforcementRefused", "PrincipalUnresolvable", "UnenforcedQuery",
    "Operation", "OperationRegistry", "UnknownOperation",
    "RetrievalService", "ServingService", "ServingResponse",
    "EnvelopeUsage", "UsageRecorder", "UsageTracker",
    "OperationSpec", "CompositeSpec", "CompositeStep", "ParamBinding",
    "ParamSpec", "OperationGenerator", "InProcessOperationCatalog",
    "OperationRejected", "OperationCallError",
    "CompositeResult", "StepResult", "TraceEntry",
    "base_operation_specs", "entity_dossier_spec", "fact_template",
    "register_serving_defaults",
    "DenseRetrievalService", "Reranker", "PassThroughReranker",
    "KnowledgeHubServingService", "ServingApp", "LatencyStats",
    "JsonlUsageRecorder", "build_serving_app", "make_server",
    "ProbeReport", "run_probe",
    "DeployPlan", "PlanError", "load_profiles", "resolve_plan",
    "OperatorApp", "OperatorGate", "OperatorService", "WriteOperation",
    "WriteParamSpec", "WriteOutcome", "WriteRefused", "WriteCallError",
    "WriteOperationRejected", "UnknownWriteOperation",
    "build_operator_app", "register_operator_defaults",
    "OperatorReadService",
]
