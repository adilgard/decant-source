"""Resolution flow (Build Prompt 5): staged mentions -> canonical entities ->
promoted facts. Closes the vertical slice: after this stage runs, extraction's
pending_facts land in `facts` with canonical entity ids. (The AGE graph
projection this flow used to feed is RETIRED — Build Prompt 9; the
project_fact/delete_* calls below are gated no-ops kept as frozen reference.)

ResolutionService is extraction's downstream twin. Per pending mention:

    block          exact-key overlap + pgvector ANN (P1's ann_candidates,
                   the mention embedded on first touch) + pg_trgm name/alias
                   similarity -> BlockedCandidates
    scorer.resolve the tiered Scorer bands the best score per
                   resolution_policy (interfaces.py seam — whole-engine
                   replacements swap in here)
    apply          match_candidates rows + resolution_decisions row + the
                   mention update (resolve / new entity / review) + flywheel
                   labels, ALL IN ONE TRANSACTION -> a crash mid-apply
                   leaves the mention pending and the re-run replays it
    promote        Pipeline.promote_pending rewrites 'mention:<id>' refs to
                   canonical entity ids and lands facts + graph edges

Cadence: a RE-RUNNABLE BATCH SWEEP over resolution_status='pending' (the
status column is the queue; no third outbox). Tier 0 stays available inline
via resolve_mention(). Resolution is ingestion-time, not query-time: it may
lag extraction — reads never depend on it. The sweep assumes one runner per
tenant (idempotency comes from the per-mention transaction, not from leases);
a concurrent-sweeper lease protocol is a scale problem for later, noted in
RESOLUTION_NOTES.md.

Merges are REVERSIBLE: merge_entities snapshots the absorbed entity (row,
aliases, what transferred, repointed mention/fact/pending refs) into
entity_merges; reverse_merge reconstructs it, repoints everything back,
re-resolves the absorbed side's mentions, and writes the er_nonmatch label —
the flywheel's hard negatives. Nothing is a silent permanent join.

Bias to under-merge, enforced here as in the scorer: review outcomes only
ever come back through explicit human decisions (decide_match /
resolve_as_new), each of which writes a human_review label.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from knowledge_hub import drain_timing
from knowledge_hub.interfaces import (
    BlockedCandidate,
    Embedder,
    ResolutionOutcome,
    Scorer,
)
from knowledge_hub.models import (
    Entity,
    EntityAlias,
    EntityMention,
    EntityMerge,
    Label,
    MatchCandidate,
    ResolutionDecision,
)
from knowledge_hub.pipeline import Pipeline

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 30          # blocking union cap, best-cosine/name first
ANN_K = 20                   # P1's ann_candidates fan-out
NAME_SIM_FLOOR = 0.25        # pg_trgm similarity() cutoff for the name block

# Label authority (trust weight of the SOURCE): deterministic key matches are
# ground truth by construction; reversals are deliberate human corrections of
# an applied merge; review decisions are human but single-pass.
AUTHORITY = {"deterministic": 1.0, "reversal": 0.95, "human_review": 0.9}


class ResolutionSummary(BaseModel):
    """Result of one sweep (returned, safe to log)."""
    tenant_id: str
    swept: int = 0
    resolved: int = 0
    new_entities: int = 0
    review: int = 0
    errors: int = 0
    by_tier: dict[str, int] = Field(default_factory=dict)
    promoted_facts: list[int] = Field(default_factory=list)


class ResolutionService:
    def __init__(self, pipeline: Pipeline, scorer: Scorer, embedder: Embedder,
                 ontology_version: Optional[str] = None):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.scorer = scorer
        self.embedder = embedder
        self._ontology_version = ontology_version
        # keys_are_authoritative per entity type, read once per process (see
        # _keys_are_authoritative for why this one policy field is cached).
        self._authoritative_types: dict[str, bool] = {}

    # -------------------------------------------------------------- sweep --
    def sweep(self, tenant_id: str, limit: int = 500) -> ResolutionSummary:
        """Resolve up to `limit` pending mentions (id order — earlier
        resolutions feed later corroboration), then promote every fully
        resolved pending fact. Re-runnable: resolved/review mentions are
        skipped by the status filter, promotion skips promoted rows, and a
        mention that errors stays pending for the next sweep."""
        summary = ResolutionSummary(tenant_id=tenant_id)
        drain_timing.sweep_begin(tenant_id)
        with drain_timing.timed("pickup"):
            with self.store.transaction(tenant_id) as conn:
                rows = conn.execute(
                    "SELECT id FROM entity_mentions"
                    " WHERE tenant_id = %s AND resolution_status = 'pending'"
                    " ORDER BY id LIMIT %s", (tenant_id, limit)).fetchall()
            mentions = self.store.get_mentions(tenant_id,
                                               [r["id"] for r in rows])
        with drain_timing.timed("prime"):
            self.scorer.prime(tenant_id, mentions)

        for mention in mentions:
            summary.swept += 1
            try:
                outcome = self._resolve_one(mention)
            except Exception as e:
                summary.errors += 1
                logger.warning("resolution of mention id=%s failed (stays "
                               "pending): %s: %s", mention.id,
                               type(e).__name__, e)
                continue
            summary.by_tier[outcome.tier] = \
                summary.by_tier.get(outcome.tier, 0) + 1
            if outcome.decision == "review":
                summary.review += 1
            elif outcome.decision == "new_entity":
                summary.new_entities += 1
                summary.resolved += 1
            else:
                summary.resolved += 1

        with drain_timing.timed("promote"):
            summary.promoted_facts = self.pipeline.promote_pending(tenant_id)
        drain_timing.sweep_end(swept=summary.swept, resolved=summary.resolved,
                               review=summary.review, errors=summary.errors,
                               promoted=len(summary.promoted_facts))
        return summary

    def resolve_mention(self, tenant_id: str, mention_id: int) -> ResolutionOutcome:
        """Inline single-mention path (Tier 0 is cheap enough to run at
        staging time; also the re-resolution hook after a merge reversal)."""
        mention = self.store.get_mention(tenant_id, mention_id)
        if mention is None:
            raise LookupError(f"mention id={mention_id} not found for tenant "
                              f"{tenant_id!r}")
        if mention.resolution_status != "pending":
            raise ValueError(f"mention id={mention_id} is "
                             f"{mention.resolution_status!r}, not pending")
        return self._resolve_one(mention)

    # ------------------------------------------------------ resolve + apply --
    def _resolve_one(self, mention: EntityMention) -> ResolutionOutcome:
        started = time.monotonic()
        _t = drain_timing.t0()
        candidates = self._block(mention)
        drain_timing.lap("block", _t)
        _t = drain_timing.t0()
        outcome = self.scorer.resolve(mention, candidates)
        drain_timing.lap("score", _t)
        wall_ms = int((time.monotonic() - started) * 1000)
        _t = drain_timing.t0()
        self._apply(mention, outcome, wall_ms)
        drain_timing.lap("apply", _t)
        return outcome

    # ------------------------------------------------------------ blocking --
    def _block(self, mention: EntityMention) -> list[BlockedCandidate]:
        """Union of the three blocking paths, deduped, capped. Every path is
        tenant- and type-scoped; resolution is within-tenant only."""
        tenant_id = mention.tenant_id
        hits: dict[int, dict[str, Any]] = {}

        def hit(entity_id: int, block: str, cosine: Optional[float] = None):
            entry = hits.setdefault(entity_id, {"blocks": [], "cosine": None})
            if block not in entry["blocks"]:
                entry["blocks"].append(block)
            if cosine is not None:
                entry["cosine"] = cosine

        # (a) shared extracted-key values (any key — weak ones still block,
        #     they just don't Tier-0-resolve)
        keys = {k: v for k, v in (mention.extracted_keys or {}).items()
                if v not in (None, "")}
        if keys:
            clauses = " OR ".join("attributes->>%s = %s" for _ in keys)
            params: list[Any] = []
            for k, v in keys.items():
                params += [k, str(v)]
            with self.store.transaction(tenant_id) as conn:
                # prepare=False, ON PURPOSE. The key NAME is a bind parameter
                # (attributes->>%s), so a prepared statement's generic plan
                # can't use the expression index from migration 016
                # (ix_entities_key_uslm_identifier indexes the literal
                # expression attributes->>'uslm_identifier'). psycopg auto-
                # prepares after 5 executions; once the generic plan kicks in,
                # every probe seq-scans the whole registry (~7ms) instead of
                # hitting the index. prepare=False forces a per-value custom
                # plan (~0.15ms planning) that folds the key name to a literal
                # and keeps the index in play. Removing this re-breaks 016.
                rows = conn.execute(
                    f"SELECT id FROM entities"
                    f" WHERE tenant_id = %s AND entity_type = %s"
                    f"   AND valid_to IS NULL AND ({clauses})",
                    (tenant_id, mention.entity_type, *params),
                    prepare=False).fetchall()
            for r in rows:
                hit(r["id"], "key")

        # AUTHORITATIVE KEYS STOP HERE (migration 014, measured 2026-08-04).
        # When the type declares its keys decide identity, Tier 0's verdict is
        # already determined by the key block alone: hit -> resolve/conflict,
        # miss -> new_entity. The ANN and trigram paths below cannot change it
        # — the scorer never reads their candidates for this type — so running
        # them is pure cost. And the cost is the run: path (c) is a trigram
        # scan over every entity+alias with GROUP BY/HAVING, per mention, so
        # the sweep degrades O(mentions x entities) as the corpus accretes.
        # On the first full-title run it was 25 mentions/min against 65,000
        # staged — a 40-hour drain, with Ollama at 9% and Postgres doing all
        # of it. Skipping also skips _ensure_embedding: no ANN, no vector
        # needed, and bge-m3 stays out of the hot path entirely.
        if keys and self._keys_are_authoritative(tenant_id,
                                                 mention.entity_type):
            return self._candidates_for(tenant_id, hits)

        # (b) pgvector ANN over entity embeddings (P1's ann_candidates); the
        #     mention is embedded on first touch and the vector persisted
        self._ensure_embedding(mention)
        for cand in self.store.ann_candidates(
                tenant_id, mention.context_embedding, mention.entity_type,
                k=ANN_K):
            hit(cand.entity_id, "ann", cosine=cand.similarity)

        # (c) pg_trgm similarity over canonical names AND earned aliases
        with self.store.transaction(tenant_id) as conn:
            rows = conn.execute(
                """
                SELECT e.id, greatest(
                           similarity(e.canonical_name, %(s)s),
                           coalesce(max(similarity(a.alias, %(s)s)), 0)
                       ) AS name_sim
                FROM entities e
                LEFT JOIN entity_aliases a ON a.entity_id = e.id
                WHERE e.tenant_id = %(t)s AND e.entity_type = %(ty)s
                  AND e.valid_to IS NULL
                GROUP BY e.id
                HAVING greatest(
                           similarity(e.canonical_name, %(s)s),
                           coalesce(max(similarity(a.alias, %(s)s)), 0)
                       ) >= %(floor)s
                """,
                {"s": mention.surface_text, "t": tenant_id,
                 "ty": mention.entity_type, "floor": NAME_SIM_FLOOR},
            ).fetchall()
        for r in rows:
            hit(r["id"], "name")

        return self._candidates_for(tenant_id, hits)

    def _keys_are_authoritative(self, tenant_id: str,
                                entity_type: str) -> bool:
        """resolution_policy.keys_are_authoritative for one type, cached for
        the process. Policy is data and normally reread per sweep, but this
        flag is read PER MENTION on the blocking hot path, and flipping it
        mid-corpus would resolve half a corpus one way and half the other —
        restart to change it, same rule as a config edit."""
        cached = self._authoritative_types.get(entity_type)
        if cached is None:
            row = self.store.get_resolution_policy(tenant_id, entity_type)
            cached = bool(row and row.keys_are_authoritative)
            self._authoritative_types[entity_type] = cached
        return cached

    def _candidates_for(self, tenant_id: str,
                        hits: dict[int, dict[str, Any]]
                        ) -> list[BlockedCandidate]:
        candidates = []
        for entity_id, entry in hits.items():
            entity = self.store.get_entity(tenant_id, entity_id)
            if entity is None:
                continue
            candidates.append(BlockedCandidate(
                entity_id=entity_id, canonical_name=entity.canonical_name,
                entity_type=entity.entity_type, attributes=entity.attributes,
                aliases=[a.alias for a in entity.aliases],
                cosine=entry["cosine"], blocks=entry["blocks"]))
        # KEY-BLOCKED CANDIDATES ARE NEVER TRUNCATED. Sorting by cosine alone
        # put them LAST — a candidate found only by exact key has no cosine, so
        # `-(None or 0)` ranks it below every fuzzy neighbour — and then the cap
        # could drop the one candidate that resolves the mention deterministically.
        # Harmless while a tenant holds a handful of entities per type, which is
        # why the pilot never showed it. It bites at corpus scale: path (c) has
        # no cap of its own and a floor of only NAME_SIM_FLOOR, so a full Title
        # 26 offers thousands of citation strings above it, and Tier 0 would
        # start missing keys that ARE in the registry. That reads as "the
        # deterministic tier is unreliable" when the blocker is what failed.
        # An exact hit on a strong key is bounded in practice (0 or 1; more than
        # one is a key conflict, which Tier 0 must SEE to send to review), so
        # keeping all of them costs nothing and cannot be starved.
        keyed = [c for c in candidates if "key" in c.blocks]
        rest = [c for c in candidates if "key" not in c.blocks]
        keyed.sort(key=lambda c: -(c.cosine or 0))
        rest.sort(key=lambda c: -(c.cosine or 0))
        return keyed + rest[:max(0, MAX_CANDIDATES - len(keyed))]

    def _ensure_embedding(self, mention: EntityMention) -> None:
        if mention.context_embedding is not None:
            return
        mention.context_embedding = self.embedder.embed(
            [mention.surface_text])[0]
        from knowledge_hub.factstore_pg import vector_literal
        with self.store.transaction(mention.tenant_id) as conn:
            conn.execute(
                "UPDATE entity_mentions SET context_embedding = %s::vector"
                " WHERE tenant_id = %s AND id = %s",
                (vector_literal(mention.context_embedding),
                 mention.tenant_id, mention.id))

    # --------------------------------------------------------------- apply --
    def _apply(self, mention: EntityMention, outcome: ResolutionOutcome,
               wall_ms: int) -> None:
        """Persist one verdict atomically: candidate rows, the decision row,
        the mention update / entity creation, and any flywheel labels commit
        or roll back together (store helpers nest as savepoints)."""
        tenant_id = mention.tenant_id
        best_entity_id = outcome.entity_id
        if best_entity_id is None and outcome.candidates:
            best_entity_id = max(outcome.candidates,
                                 key=lambda s: s.score).entity_id
        with self.store.transaction(tenant_id):
            winner_candidate_id = None
            _t_mc = drain_timing.t0()
            for s in outcome.candidates:
                if outcome.decision == "resolved" \
                        and s.entity_id == outcome.entity_id:
                    decision = "auto_merge"
                elif outcome.decision == "review" and s.band in ("high", "gray"):
                    decision = "review"
                else:
                    decision = "auto_separate"
                mc = MatchCandidate(
                    tenant_id=tenant_id, left_type="mention",
                    left_id=mention.id, right_id=s.entity_id,
                    match_score=s.score, match_method=s.method,
                    features={**s.features, "tier": s.tier}, band=s.band,
                    decision=decision,
                    decision_reason=outcome.reason if decision != "auto_separate"
                    else None)
                self.store.insert_match_candidate(mc)
                drain_timing.count("mc_rows")
                if s.entity_id == best_entity_id:
                    winner_candidate_id = mc.id

            # A Tier-0 key conflict means the REGISTRY likely holds
            # duplicates: log the entity-entity pair for human merge review.
            if outcome.reason == "key_conflict":
                conflicting = outcome.features.get("conflicting_entities", [])
                for left, right in zip(conflicting, conflicting[1:]):
                    self.store.insert_match_candidate(MatchCandidate(
                        tenant_id=tenant_id, left_type="entity", left_id=left,
                        right_id=right, match_score=1.0,
                        match_method="deterministic_key", band="high",
                        decision="review",
                        decision_reason="tier0_key_conflict via mention "
                                        f"{mention.id}",
                        features={"via_mention": mention.id,
                                  "key_overlap": outcome.features.get(
                                      "key_overlap", {})}))
                    drain_timing.count("mc_rows")
            drain_timing.lap("mc_insert", _t_mc, n=0)

            if outcome.decision == "resolved":
                self._resolve_to_existing(mention, outcome)
                if outcome.tier == "t0":
                    # Free ground truth: a deterministic key match is a
                    # labeled positive pair, not a model opinion.
                    self._label(tenant_id, "er_match", "deterministic",
                                mention, outcome.entity_id,
                                {"key_overlap":
                                 outcome.features.get("key_overlap", {})})
            elif outcome.decision == "new_entity":
                outcome.entity_id = self._create_entity(mention)
            else:  # review
                with self.store.transaction(tenant_id) as conn:
                    conn.execute(
                        "UPDATE entity_mentions SET resolution_status = 'review'"
                        " WHERE tenant_id = %s AND id = %s",
                        (tenant_id, mention.id))

            self.store.insert_resolution_decision(ResolutionDecision(
                tenant_id=tenant_id, mention_id=mention.id,
                tier=outcome.tier, method=outcome.method,
                score=outcome.score, band=outcome.band,
                decision=outcome.decision, entity_id=outcome.entity_id,
                match_candidate_id=winner_candidate_id,
                features={**outcome.features, "reason": outcome.reason},
                resolver_version=self.scorer.version, wall_ms=wall_ms))

    def _resolve_to_existing(self, mention: EntityMention,
                             outcome: ResolutionOutcome) -> None:
        tenant_id = mention.tenant_id
        with self.store.transaction(tenant_id) as conn:
            conn.execute(
                """
                UPDATE entity_mentions SET resolved_entity_id = %s,
                    resolution_status = 'resolved', resolver_version = %s,
                    resolved_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (outcome.entity_id, self.scorer.version, tenant_id,
                 mention.id))
            # The surface form is an earned alias of the entity...
            conn.execute(
                """
                INSERT INTO entity_aliases
                    (tenant_id, entity_id, alias, source, confidence)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (entity_id, alias) DO NOTHING
                """,
                (tenant_id, outcome.entity_id, mention.surface_text,
                 mention.source_system, outcome.score))
            # ...and its keys backfill attribute gaps (existing values win:
            # jsonb || keeps the RIGHT side's value on conflict).
            keys = {k: v for k, v in (mention.extracted_keys or {}).items()
                    if v not in (None, "")}
            if keys:
                conn.execute(
                    "UPDATE entities SET attributes = %s::jsonb || attributes"
                    " WHERE tenant_id = %s AND id = %s",
                    (Jsonb(keys), tenant_id, outcome.entity_id))
        mention.resolved_entity_id = outcome.entity_id
        mention.resolution_status = "resolved"

    def _create_entity(self, mention: EntityMention) -> int:
        """Uncertain-goes-here: a new canonical entity seeded from the
        mention (surface -> canonical_name + alias, extracted_keys ->
        attributes, context_embedding -> the blocking target for future
        mentions)."""
        tenant_id = mention.tenant_id
        entity = Entity(
            tenant_id=tenant_id, canonical_name=mention.surface_text,
            entity_type=mention.entity_type,
            attributes={k: v for k, v in (mention.extracted_keys or {}).items()
                        if v not in (None, "")},
            ontology_version=self._ontology(tenant_id),
            security_label_id=None,
            embedding=mention.context_embedding,
            embedding_model=self.embedder.model
            if mention.context_embedding is not None else None,
            aliases=[EntityAlias(tenant_id=tenant_id,
                                 alias=mention.surface_text,
                                 source=mention.source_system)])
        entity_id = self.store.upsert_entity(entity)
        with self.store.transaction(tenant_id) as conn:
            conn.execute(
                """
                UPDATE entity_mentions SET resolved_entity_id = %s,
                    resolution_status = 'resolved', resolver_version = %s,
                    resolved_at = now()
                WHERE tenant_id = %s AND id = %s
                """,
                (entity_id, self.scorer.version, tenant_id, mention.id))
        mention.resolved_entity_id = entity_id
        mention.resolution_status = "resolved"
        return entity_id

    # ------------------------------------------------------ review actions --
    def decide_match(self, tenant_id: str, candidate_id: int, same: bool,
                     reviewer: str) -> None:
        """Apply a human verdict on a review-band match_candidates row. This
        is the flywheel's human_review label source; approving a
        mention-entity row resolves the mention, approving an entity-entity
        row performs the (reversible) merge."""
        mc = self.store.get_match_candidate(tenant_id, candidate_id)
        if mc is None:
            raise LookupError(f"match_candidate id={candidate_id} not found "
                              f"for tenant {tenant_id!r}")
        if mc.decision != "review":
            raise ValueError(f"match_candidate id={candidate_id} is "
                             f"{mc.decision!r}, not awaiting review")
        with self.store.transaction(tenant_id) as conn:
            conn.execute(
                "UPDATE match_candidates SET decision = %s, reviewed_by = %s,"
                " reviewed_at = now() WHERE tenant_id = %s AND id = %s",
                ("applied" if same else "auto_separate", reviewer, tenant_id,
                 candidate_id))
            self._label(
                tenant_id, "er_match" if same else "er_nonmatch",
                "human_review", None, None,
                {"left": {"type": mc.left_type, "id": mc.left_id},
                 "right": {"type": mc.right_type, "id": mc.right_id},
                 "candidate_id": candidate_id, "score": mc.match_score,
                 "method": mc.match_method, "reviewer": reviewer})
            if not same:
                return
            if mc.left_type == "mention":
                mention = self.store.get_mention(tenant_id, mc.left_id)
                outcome = ResolutionOutcome(
                    decision="resolved", entity_id=mc.right_id,
                    tier="t0", method=mc.match_method,
                    score=mc.match_score)
                self._resolve_to_existing(mention, outcome)
            else:
                self.merge_entities(
                    tenant_id, surviving_id=mc.right_id, merged_id=mc.left_id,
                    triggered_by=candidate_id, method=mc.match_method,
                    score=mc.match_score, merged_by=reviewer)

    def resolve_as_new(self, tenant_id: str, mention_id: int,
                       reviewer: str) -> int:
        """Human verdict: none of the candidates match — every open review
        pair becomes a labeled hard negative, and the mention becomes a new
        entity."""
        mention = self.store.get_mention(tenant_id, mention_id)
        if mention is None:
            raise LookupError(f"mention id={mention_id} not found for tenant "
                              f"{tenant_id!r}")
        with self.store.transaction(tenant_id) as conn:
            open_rows = conn.execute(
                "SELECT id, right_id, match_score, match_method"
                " FROM match_candidates"
                " WHERE tenant_id = %s AND left_type = 'mention'"
                "   AND left_id = %s AND decision = 'review'",
                (tenant_id, mention_id)).fetchall()
            for r in open_rows:
                conn.execute(
                    "UPDATE match_candidates SET decision = 'auto_separate',"
                    " reviewed_by = %s, reviewed_at = now()"
                    " WHERE tenant_id = %s AND id = %s",
                    (reviewer, tenant_id, r["id"]))
                self._label(
                    tenant_id, "er_nonmatch", "human_review", None, None,
                    {"left": {"type": "mention", "id": mention_id},
                     "right": {"type": "entity", "id": r["right_id"]},
                     "candidate_id": r["id"], "score": r["match_score"],
                     "method": r["match_method"], "reviewer": reviewer})
            self._ensure_embedding(mention)
            return self._create_entity(mention)

    # ---------------------------------------------------------------- merge --
    def merge_entities(self, tenant_id: str, surviving_id: int,
                       merged_id: int, merged_by: str,
                       triggered_by: Optional[int] = None,
                       method: Optional[str] = None,
                       score: Optional[float] = None) -> int:
        """Absorb `merged_id` into `surviving_id`, reversibly. The snapshot
        records the absorbed row AND exactly what transferred, so
        reverse_merge can undo the join without guessing. Returns the
        entity_merges id."""
        if surviving_id == merged_id:
            raise ValueError("cannot merge an entity into itself")
        surviving = self.store.get_entity(tenant_id, surviving_id)
        merged = self.store.get_entity(tenant_id, merged_id)
        if surviving is None or merged is None:
            raise LookupError(f"merge needs both entities: surviving="
                              f"{surviving_id} merged={merged_id} "
                              f"(tenant {tenant_id!r})")

        with self.store.transaction(tenant_id) as conn:
            # -- what will transfer (computed BEFORE mutating anything) ------
            surviving_aliases = {a.alias for a in surviving.aliases} \
                | {surviving.canonical_name}
            merged_alias_rows = [
                {"alias": a.alias, "source": a.source,
                 "confidence": a.confidence} for a in merged.aliases]
            transfer_aliases = [a.alias for a in merged.aliases
                                if a.alias not in surviving_aliases]
            if merged.canonical_name not in surviving_aliases:
                transfer_aliases.append(merged.canonical_name)
            added_attribute_keys = [k for k in merged.attributes
                                    if k not in surviving.attributes]
            mention_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM entity_mentions"
                " WHERE tenant_id = %s AND resolved_entity_id = %s ORDER BY id",
                (tenant_id, merged_id)).fetchall()]
            fact_sides = [
                {"id": r["id"], "side": side}
                for side, col in (("subject", "subject_entity_id"),
                                  ("object", "object_entity_id"))
                for r in conn.execute(
                    f"SELECT id FROM facts WHERE tenant_id = %s AND {col} = %s"
                    " ORDER BY id", (tenant_id, merged_id)).fetchall()]
            pending_refs = [
                {"id": r["id"], "col": col}
                for col in ("subject_ref", "object_ref")
                for r in conn.execute(
                    f"SELECT id FROM pending_facts"
                    f" WHERE tenant_id = %s AND {col} = %s"
                    f"   AND resolution_status = 'pending' ORDER BY id",
                    (tenant_id, f"entity:{merged_id}")).fetchall()]

            snapshot = {
                "entity": {
                    "canonical_name": merged.canonical_name,
                    "entity_type": merged.entity_type,
                    "attributes": merged.attributes,
                    "ontology_version": merged.ontology_version,
                    "security_label_id": merged.security_label_id,
                    "embedding": merged.embedding,
                    "embedding_model": merged.embedding_model,
                    "valid_from": merged.valid_from.isoformat()
                    if merged.valid_from else None,
                    "valid_to": merged.valid_to.isoformat()
                    if merged.valid_to else None,
                    "created_at": merged.created_at.isoformat()
                    if merged.created_at else None,
                },
                "aliases": merged_alias_rows,
                "transferred_aliases": transfer_aliases,
                "added_attribute_keys": added_attribute_keys,
                "mention_ids": mention_ids,
                "fact_sides": fact_sides,
                "pending_refs": pending_refs,
            }
            merge = EntityMerge(
                tenant_id=tenant_id, surviving_entity_id=surviving_id,
                merged_entity_id=merged_id, merged_snapshot=snapshot,
                triggered_by=triggered_by, method=method, score=score,
                merged_by=merged_by)
            self.store.insert_entity_merge(merge)

            # -- transfer -----------------------------------------------------
            for alias in transfer_aliases:
                conn.execute(
                    "INSERT INTO entity_aliases"
                    " (tenant_id, entity_id, alias, source, confidence)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (entity_id, alias) DO NOTHING",
                    (tenant_id, surviving_id, alias, "merge", score))
            conn.execute(
                "DELETE FROM entity_aliases"
                " WHERE tenant_id = %s AND entity_id = %s",
                (tenant_id, merged_id))
            if added_attribute_keys:
                conn.execute(
                    "UPDATE entities SET attributes = %s::jsonb || attributes"
                    " WHERE tenant_id = %s AND id = %s",
                    (Jsonb({k: merged.attributes[k]
                            for k in added_attribute_keys}),
                     tenant_id, surviving_id))
            conn.execute(
                "UPDATE entity_mentions SET resolved_entity_id = %s"
                " WHERE tenant_id = %s AND resolved_entity_id = %s",
                (surviving_id, tenant_id, merged_id))
            for col in ("subject_entity_id", "object_entity_id"):
                conn.execute(
                    f"UPDATE facts SET {col} = %s"
                    f" WHERE tenant_id = %s AND {col} = %s",
                    (surviving_id, tenant_id, merged_id))
            for col in ("subject_ref", "object_ref"):
                conn.execute(
                    f"UPDATE pending_facts SET {col} = %s"
                    f" WHERE tenant_id = %s AND {col} = %s"
                    f"   AND resolution_status = 'pending'",
                    (f"entity:{surviving_id}", tenant_id,
                     f"entity:{merged_id}"))
            # An older merge may have recorded merged_id as ITS survivor;
            # re-root the chain so its FK survives the row deletion below
            # (snapshots keep the true history).
            conn.execute(
                "UPDATE entity_merges SET surviving_entity_id = %s"
                " WHERE tenant_id = %s AND surviving_entity_id = %s",
                (surviving_id, tenant_id, merged_id))
            conn.execute(
                "DELETE FROM entities WHERE tenant_id = %s AND id = %s",
                (tenant_id, merged_id))

            # -- graph projection: drop the absorbed vertex (and its stale
            #    edges), re-project every repointed fact -----------------------
            self.store.delete_entity_vertex(tenant_id, merged_id)
            for fs in fact_sides:
                fact = self.store.get_fact(tenant_id, fs["id"])
                if fact.object_entity_id is not None:
                    self.store.delete_fact_edge(tenant_id, fact.id)
                    self.store.project_fact(fact)
        logger.info("merged entity %s into %s (tenant %s, merge id %s)",
                    merged_id, surviving_id, tenant_id, merge.id)
        return merge.id

    def reverse_merge(self, tenant_id: str, merge_id: int,
                      reversed_by: str) -> int:
        """Split a merge back apart: reconstruct the absorbed entity from its
        snapshot (same id), undo the transfer, repoint facts/refs back,
        re-resolve the absorbed side's mentions, and write the er_nonmatch
        label — a wrong merge becomes the flywheel's best training pair
        instead of a silent permanent join. Returns the restored entity id."""
        merge = self.store.get_entity_merge(tenant_id, merge_id)
        if merge is None:
            raise LookupError(f"entity_merge id={merge_id} not found for "
                              f"tenant {tenant_id!r}")
        if merge.reversed_at is not None:
            raise ValueError(f"entity_merge id={merge_id} was already "
                             f"reversed at {merge.reversed_at}")
        snap = merge.merged_snapshot
        ent, merged_id = snap["entity"], merge.merged_entity_id
        surviving_id = merge.surviving_entity_id

        from knowledge_hub.factstore_pg import vector_literal
        with self.store.transaction(tenant_id) as conn:
            emb = vector_literal(ent["embedding"]) \
                if ent.get("embedding") else None
            conn.execute(
                """
                INSERT INTO entities
                    (id, tenant_id, canonical_name, entity_type, attributes,
                     ontology_version, security_label_id, embedding,
                     embedding_model, valid_from, valid_to, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s,
                        COALESCE(%s::timestamptz, now()))
                """,
                (merged_id, tenant_id, ent["canonical_name"],
                 ent["entity_type"], Jsonb(ent["attributes"]),
                 ent["ontology_version"], ent["security_label_id"], emb,
                 ent["embedding_model"], ent["valid_from"], ent["valid_to"],
                 ent["created_at"]))
            for a in snap["aliases"]:
                conn.execute(
                    "INSERT INTO entity_aliases"
                    " (tenant_id, entity_id, alias, source, confidence)"
                    " VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (entity_id, alias) DO NOTHING",
                    (tenant_id, merged_id, a["alias"], a["source"],
                     a["confidence"]))
            for alias in snap["transferred_aliases"]:
                conn.execute(
                    "DELETE FROM entity_aliases WHERE tenant_id = %s"
                    " AND entity_id = %s AND alias = %s",
                    (tenant_id, surviving_id, alias))
            if snap["added_attribute_keys"]:
                conn.execute(
                    "UPDATE entities SET attributes = attributes - %s::text[]"
                    " WHERE tenant_id = %s AND id = %s",
                    (snap["added_attribute_keys"], tenant_id, surviving_id))
            for fs in snap["fact_sides"]:
                col = "subject_entity_id" if fs["side"] == "subject" \
                    else "object_entity_id"
                conn.execute(
                    f"UPDATE facts SET {col} = %s"
                    f" WHERE tenant_id = %s AND id = %s AND {col} = %s",
                    (merged_id, tenant_id, fs["id"], surviving_id))
            for pr in snap["pending_refs"]:
                conn.execute(
                    f"UPDATE pending_facts SET {pr['col']} = %s"
                    f" WHERE tenant_id = %s AND id = %s AND {pr['col']} = %s"
                    f"   AND resolution_status = 'pending'",
                    (f"entity:{merged_id}", tenant_id, pr["id"],
                     f"entity:{surviving_id}"))
            # The absorbed side's mentions go back to pending; they re-resolve
            # below against the SPLIT registry (both entities exist again).
            if snap["mention_ids"]:
                conn.execute(
                    "UPDATE entity_mentions SET resolved_entity_id = NULL,"
                    " resolution_status = 'pending', resolved_at = NULL"
                    " WHERE tenant_id = %s AND id = ANY(%s)",
                    (tenant_id, snap["mention_ids"]))
            conn.execute(
                "UPDATE entity_merges SET reversed_at = now(),"
                " reversed_by = %s WHERE tenant_id = %s AND id = %s",
                (reversed_by, tenant_id, merge_id))

            # graph: repointed edges move back (vertex reappears via MERGE)
            for fs in snap["fact_sides"]:
                fact = self.store.get_fact(tenant_id, fs["id"])
                if fact.object_entity_id is not None:
                    self.store.delete_fact_edge(tenant_id, fact.id)
                    self.store.project_fact(fact)

            # THE hard negative: a human decided these two are NOT the same.
            self._label(
                tenant_id, "er_nonmatch", "reversal", None, None,
                {"left": {"type": "entity", "id": surviving_id},
                 "right": {"type": "entity", "id": merged_id},
                 "merge_id": merge_id, "method": merge.method,
                 "score": merge.score, "reversed_by": reversed_by})

        # Re-resolve outside the reversal transaction: each mention's verdict
        # is its own atomic apply, and a failure leaves it pending (visible),
        # not the reversal half-done.
        for mention_id in snap["mention_ids"]:
            try:
                self.resolve_mention(tenant_id, mention_id)
            except Exception as e:
                logger.warning("re-resolution of mention id=%s after reversal "
                               "of merge id=%s failed (stays pending): %s",
                               mention_id, merge_id, e)
        logger.info("reversed merge id=%s: entity %s restored (tenant %s)",
                    merge_id, merged_id, tenant_id)
        return merged_id

    # ------------------------------------------------------------- helpers --
    def _label(self, tenant_id: str, label_type: str, source: str,
               mention: Optional[EntityMention], entity_id: Optional[int],
               payload: dict[str, Any]) -> None:
        if mention is not None:
            payload = {"left": {"type": "mention", "id": mention.id},
                       "right": {"type": "entity", "id": entity_id},
                       **payload}
        self.store.insert_label(Label(
            tenant_id=tenant_id, label_type=label_type, payload=payload,
            source=source, authority=AUTHORITY.get(source, 0.5),
            ontology_version=self._ontology(tenant_id)))

    def _ontology(self, tenant_id: str) -> str:
        """The version stamped on entities/labels this service creates. An
        explicit constructor pin wins (tests, replay tooling); otherwise
        resolve the operator's ACTIVE selection per call — deliberately
        uncached (d.s Stage 1), so a long-lived service (the operator
        console's resolver lives for weeks) follows a console swap instead
        of stamping the version that was active at first use. One
        single-row indexed SELECT per new entity/label is noise."""
        if self._ontology_version is not None:
            return self._ontology_version
        version, _ = self.store.get_ontology_definition(tenant_id)
        return version
