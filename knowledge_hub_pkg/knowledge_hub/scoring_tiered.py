"""TieredScorer — the pilot Scorer: route each mention to the cheapest tier
that resolves it correctly, band the result against resolution_policy.

    Tier 0   deterministic keys   exact match on a strong extracted_key
                                  (email / tax_id / customer_id / source-
                                  native SoR keys; domain only for
                                  Organization). Auto-resolve; keyed SoR data
                                  short-circuits here. Free, high-confidence
                                  flywheel labels.
    Tier 1   probabilistic        Splink (DuckDB backend, Fellegi-Sunter) for
                                  mentions carrying structured attributes.
    Tier 1b  embedding + LLM      thin/prose mentions: cosine (pgvector
                                  blocking) + trigram-ish name similarity;
                                  local-LLM adjudication on the ambiguous
                                  residual ONLY.
    Tier 1c  graph corroboration  shared edges (same employer/contract/...)
                                  as a bounded score boost + the gate for
                                  requires_corroboration policies.

Banding is policy DATA (resolution_policy per entity type): >= t_high
auto-merge, <= t_low new/separate, gray -> review; requires_corroboration and
auto_merge_allowed are honored. THE THRESHOLDS AND ALL WEIGHTS/PRIORS IN THIS
MODULE ARE PLACEHOLDERS until the ER benchmark (Axis B) calibrates them on
labeled pairs — do not read a green test suite as resolution quality.

Bias to under-merge is enforced structurally: uncertainty routes to
new_entity or review, never a silent merge — a missed match is recoverable, a
wrong merge is silent and dangerous. Concretely: key conflicts -> review,
multiple high-band candidates -> review, name-only matches on
requires_corroboration types without a corroborating edge -> review,
auto_merge_allowed=false -> review, unknown entity types get a conservative
fallback policy that never auto-merges.

Nothing Splink-specific leaks past the Scorer interface: evidence travels in
generic `features` dicts (key_overlap / name_sim / cosine / corroboration /
match_weight), so a whole-engine replacement (Senzing) swaps in behind
`Scorer` without touching the flow.
"""
from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher
from typing import Any, Optional, Sequence

import ollama

from knowledge_hub.config import settings
from knowledge_hub.ollama_client import make_ollama_client
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import (
    BlockedCandidate,
    ResolutionOutcome,
    Scorer,
    ScoredCandidate,
)
from knowledge_hub.models import EntityMention, ResolutionPolicy

logger = logging.getLogger(__name__)

# Bumped whenever tiering/priors/prompt change; part of resolver_version so
# decisions and mention stamps are attributable to the exact contract.
SCORER_VERSION = "tiered-0.1"

# --- Tier 0: which extracted_keys count as STRONG (exact match resolves) ----
# A strong key identifies exactly one real-world entity. 'domain' does only
# for Organizations — every person at acme.com shares it — so for other types
# it stays a Tier-1 feature, never a Tier-0 short-circuit.
WEAK_KEYS_UNLESS_TYPE = {"domain": ("Organization",)}

# --- Tier 1 (Splink): the fixed comparison columns -------------------------
# Source-native SoR keys outside this set (asset_id, ...) still drive Tier 0
# and the key_overlap feature; they are not Splink columns because the
# comparison space must be fixed per settings object.
SPLINK_KEY_FIELDS = ("email", "domain", "tax_id", "customer_id")

# PLACEHOLDER Fellegi-Sunter parameters (see module docstring). m = P(level |
# same entity), u = P(level | different entities). Deliberately conservative
# priors, not trained: u-estimation / EM belongs to benchmark calibration,
# where the labels store provides the pairs.
SPLINK_PRIOR_MATCH_PROBABILITY = 0.01
NAME_JW_THRESHOLDS = [0.92, 0.80]
NAME_M = [0.55, 0.25, 0.12, 0.08]        # exact, >=0.92, >=0.80, else
NAME_U = [0.001, 0.004, 0.015, 0.98]
KEY_M = {"email": [0.90, 0.10], "domain": [0.80, 0.20],
         "tax_id": [0.90, 0.10], "customer_id": [0.90, 0.10]}
KEY_U = {"email": [0.0001, 0.9999], "domain": [0.005, 0.995],
         "tax_id": [0.0001, 0.9999], "customer_id": [0.0001, 0.9999]}

# --- Tier 1b weights + Tier 1c boost (PLACEHOLDERS, same rule) --------------
COSINE_WEIGHT = 0.5                # blend of cosine vs name_sim when both exist
CORROBORATION_BOOST = 0.05         # per shared edge ...
CORROBORATION_BOOST_CAP = 0.10     # ... bounded
MAX_ADJUDICATIONS_PER_MENTION = 3  # LLM calls on the gray residual only
ADJUDICATION_CONTEXT_CHARS = 400

# Unknown entity type -> no policy row: conservative fallback that can send a
# mention to review or new_entity but never auto-merges.
FALLBACK_POLICY = dict(t_high=0.99, t_low=0.50, requires_corroboration=True,
                       auto_merge_allowed=False,
                       notes="fallback: entity_type missing from resolution_policy")


def name_similarity(a: str, b: str) -> float:
    """Deterministic [0,1] surface similarity (difflib ratio, casefolded).
    Cheap and dependency-free; NOT the Splink JW comparison — this drives
    Tier 1b blending and best-alias selection only."""
    return SequenceMatcher(None, a.casefold().strip(),
                           b.casefold().strip()).ratio()


def best_name_variant(surface: str, candidate: BlockedCandidate) -> tuple[str, float]:
    """The candidate name (canonical or alias) most similar to the mention
    surface, with its similarity — aliases are earned surface forms, so a
    mention should match against the best of them, not only the canonical."""
    variants = [candidate.canonical_name, *candidate.aliases]
    scored = [(v, name_similarity(surface, v)) for v in variants]
    return max(scored, key=lambda x: x[1])


class _Adjudicator:
    """Gray-band LLM adjudication (local Ollama, schema-constrained, temp 0).
    A transport/validation failure returns None — the caller keeps the
    embedding score and the mention falls where the band says (never a
    silent merge on a broken adjudicator)."""

    SCHEMA = {
        "type": "object",
        "properties": {
            "same_entity": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        },
        "required": ["same_entity", "confidence"],
    }

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None,
                 client: Optional[ollama.Client] = None):
        self.model = model or settings.adjudication_model
        self._client = client or make_ollama_client(host)

    def judge(self, surface: str, entity_type: str, context: str,
              candidate: BlockedCandidate) -> Optional[tuple[bool, float]]:
        aliases = ", ".join(candidate.aliases) or "(none)"
        attrs = json.dumps(candidate.attributes) if candidate.attributes else "{}"
        prompt = f"""Two records may refer to the same real-world {entity_type}.

Record A (a mention in a document):
  surface text: "{surface}"
  document context: "{context}"

Record B (a known entity in the registry):
  canonical name: "{candidate.canonical_name}"
  known aliases: {aliases}
  known attributes: {attrs}

Do A and B refer to the SAME real-world entity? Judge only from the evidence
above. If the evidence is insufficient to be sure, answer same_entity=false
with low confidence."""
        try:
            response = self._client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content":
                        "You adjudicate entity resolution for a knowledge "
                        "base. You answer strictly from the given evidence "
                        "and emit only JSON conforming to the schema."},
                    {"role": "user", "content": prompt},
                ],
                think=False, format=self.SCHEMA,
                options={"temperature": 0, "num_ctx": 2048})
            verdict = json.loads(response.message.content or "")
            return bool(verdict["same_entity"]), \
                min(max(float(verdict["confidence"]), 0.0), 1.0)
        except Exception as e:
            logger.warning("adjudication failed for %r vs entity %s: %s",
                           surface, candidate.entity_id, e)
            return None


class TieredScorer(Scorer):
    def __init__(self, store: PostgresFactStore,
                 adjudicator: Optional[_Adjudicator] = None,
                 max_adjudications: int = MAX_ADJUDICATIONS_PER_MENTION):
        self._store = store
        self._adjudicator = adjudicator or _Adjudicator()
        self._max_adjudications = max_adjudications
        self._policies: dict[str, ResolutionPolicy] = {}
        self._linkers: dict[str, Any] = {}   # tenant -> splink Linker
        self._version: Optional[str] = None

    @property
    def version(self) -> str:
        if self._version is None:
            import splink
            self._version = (f"{SCORER_VERSION}/splink-{splink.__version__}"
                             f"/adj-{self._adjudicator.model}")
        return self._version

    # ------------------------------------------------------------- priming --
    def prime(self, tenant_id: str, mentions: Sequence[EntityMention]) -> None:
        """Build the tenant's Splink linker from the sweep batch + the entity
        registry (Splink is batch-oriented; per-pair scoring reuses it)."""
        self._policies.clear()  # policy is data — reread every sweep
        self._linkers[tenant_id] = self._build_linker(tenant_id, mentions)

    # ------------------------------------------------------------- resolve --
    def resolve(self, mention: EntityMention,
                candidates: Sequence[BlockedCandidate]) -> ResolutionOutcome:
        policy = self._policy(mention.tenant_id, mention.entity_type)

        outcome = self._tier0(mention, candidates, policy)
        if outcome is not None:
            return outcome

        if not candidates:
            return ResolutionOutcome(decision="new_entity", tier="none",
                                     method="none", reason="no_candidates")

        corroborators = self._document_corroborators(mention)
        if self._tier1_eligible(mention):
            scored = self._tier1(mention, candidates, policy, corroborators)
        else:
            scored = self._tier1b(mention, candidates, policy, corroborators)
        return self._decide(scored, policy)

    # -------------------------------------------------- Tier 0: exact keys --
    def _strong_keys(self, mention: EntityMention) -> dict[str, Any]:
        strong = {}
        for key, value in (mention.extracted_keys or {}).items():
            if value in (None, ""):
                continue
            only_types = WEAK_KEYS_UNLESS_TYPE.get(key)
            if only_types is not None and mention.entity_type not in only_types:
                continue
            strong[key] = value
        return strong

    def _tier0(self, mention: EntityMention,
               candidates: Sequence[BlockedCandidate],
               policy: ResolutionPolicy) -> Optional[ResolutionOutcome]:
        strong = self._strong_keys(mention)
        if not strong:
            return None
        hits: dict[int, dict[str, Any]] = {}  # entity_id -> matched keys
        for cand in candidates:
            matched = {k: v for k, v in strong.items()
                       if str(cand.attributes.get(k, "")).casefold()
                       == str(v).casefold()}
            if matched:
                hits[cand.entity_id] = matched
        if not hits:
            if not policy.keys_are_authoritative:
                return None  # keys present but unseen -> fall through to Tier 1+
            # AUTHORITATIVE KEYS (migration 014): the key is complete and
            # externally unique for this type, so "nobody carries it" is not
            # missing evidence — it is the answer. Falling through here would
            # hand the decision to name similarity, which for a keyed corpus is
            # a strictly WEAKER signal than the one already in hand, and can be
            # actively misleading: sibling USLM citations differ by one or two
            # characters inside a long identical string, so two unrelated
            # provisions score ~0.97 (see 014's header for the measurements).
            # Deciding here keeps identity deterministic AND costs nothing —
            # no embedding compare, no LLM adjudication on the gray residual.
            # No score and no band, matching the other new_entity path: there
            # is no candidate here to have scored, and inventing a number
            # would put a match score on a non-match.
            return ResolutionOutcome(
                decision="new_entity", tier="t0",
                method="deterministic_key",
                reason="authoritative_key_unseen",
                features={"authoritative_keys": strong})

        scored = [ScoredCandidate(
            entity_id=eid, score=1.0, method="deterministic_key", tier="t0",
            band="high", features={"key_overlap": matched})
            for eid, matched in hits.items()]

        if len(hits) > 1:
            # The same strong identifier points at 2+ registry entities:
            # either the registry holds duplicates or the keys conflict.
            # Under-merge bias: a human decides; the flow also logs the
            # entity-entity pair as a merge candidate for review.
            return ResolutionOutcome(
                decision="review", tier="t0", method="deterministic_key",
                score=1.0, band="high", reason="key_conflict",
                features={"conflicting_entities": sorted(hits)},
                candidates=scored)
        (entity_id, matched), = hits.items()
        if not policy.auto_merge_allowed:
            return ResolutionOutcome(
                decision="review", tier="t0", method="deterministic_key",
                score=1.0, band="high", reason="auto_merge_disabled",
                features={"key_overlap": matched}, candidates=scored)
        return ResolutionOutcome(
            decision="resolved", entity_id=entity_id, tier="t0",
            method="deterministic_key", score=1.0, band="high",
            features={"key_overlap": matched}, candidates=scored)

    # ------------------------------------------------ Tier 1: Splink (F-S) --
    def _tier1_eligible(self, mention: EntityMention) -> bool:
        keys = mention.extracted_keys or {}
        return any(keys.get(f) for f in SPLINK_KEY_FIELDS)

    def _tier1(self, mention: EntityMention,
               candidates: Sequence[BlockedCandidate],
               policy: ResolutionPolicy,
               corroborators: set[int]) -> list[ScoredCandidate]:
        linker = self._linker(mention.tenant_id)
        left = self._splink_record("m", mention.id or 0,
                                   mention.surface_text,
                                   mention.extracted_keys or {})
        scored: list[ScoredCandidate] = []
        for cand in candidates:
            name_used, name_sim = best_name_variant(mention.surface_text, cand)
            right = self._splink_record("e", cand.entity_id, name_used,
                                        cand.attributes)
            row = linker.inference.compare_two_records(left, right) \
                .as_record_dict()[0]
            probability = float(row["match_probability"])
            key_overlap = {
                f: mention.extracted_keys[f] for f in SPLINK_KEY_FIELDS
                if mention.extracted_keys.get(f)
                and str(cand.attributes.get(f, "")).casefold()
                == str(mention.extracted_keys[f]).casefold()}
            features = {
                "match_weight": float(row["match_weight"]),
                "match_probability": probability,
                "name_used": name_used,
                "name_sim": round(name_sim, 4),
                "key_overlap": key_overlap,
                "cosine": cand.cosine,
                "blocks": cand.blocks,
            }
            score = self._corroborate(mention.tenant_id, probability,
                                      cand.entity_id, corroborators, features)
            scored.append(ScoredCandidate(
                entity_id=cand.entity_id, score=score, method="probabilistic",
                tier="t1", band=self._band(score, policy), features=features))
        return scored

    def _splink_record(self, prefix: str, row_id: int, name: str,
                       attrs: dict[str, Any]) -> dict[str, Any]:
        record = {"unique_id": f"{prefix}{row_id}", "name": name}
        for f in SPLINK_KEY_FIELDS:
            v = attrs.get(f)
            record[f] = str(v).casefold() if v not in (None, "") else None
        return record

    def _linker(self, tenant_id: str):
        if tenant_id not in self._linkers:
            self._linkers[tenant_id] = self._build_linker(tenant_id, [])
        return self._linkers[tenant_id]

    def _build_linker(self, tenant_id: str,
                      mentions: Sequence[EntityMention]):
        import pandas as pd
        import splink.comparison_library as cl
        from splink import DuckDBAPI, Linker, SettingsCreator, block_on

        mention_rows = [self._splink_record("m", m.id or i, m.surface_text,
                                            m.extracted_keys or {})
                        for i, m in enumerate(mentions)]
        with self._store.transaction(tenant_id) as conn:
            entity_rows = [
                self._splink_record("e", r["id"], r["canonical_name"],
                                    r["attributes"] or {})
                for r in conn.execute(
                    "SELECT id, canonical_name, attributes FROM entities"
                    " WHERE tenant_id = %s AND valid_to IS NULL"
                    " ORDER BY id LIMIT 2000", (tenant_id,)).fetchall()]
        # Splink needs non-empty frames with the full column set; the dummy
        # row only anchors the schema (per-pair scoring ignores the tables).
        dummy = [self._splink_record("x", 0, "", {})]
        settings_obj = SettingsCreator(
            link_type="link_only",
            probability_two_random_records_match=SPLINK_PRIOR_MATCH_PROBABILITY,
            comparisons=[
                cl.JaroWinklerAtThresholds("name", NAME_JW_THRESHOLDS)
                .configure(m_probabilities=NAME_M, u_probabilities=NAME_U),
                *[cl.ExactMatch(f).configure(m_probabilities=KEY_M[f],
                                             u_probabilities=KEY_U[f])
                  for f in SPLINK_KEY_FIELDS],
            ],
            blocking_rules_to_generate_predictions=[block_on("name")],
            retain_intermediate_calculation_columns=True,
        )
        return Linker(
            [pd.DataFrame(mention_rows or dummy),
             pd.DataFrame(entity_rows or dummy)],
            settings_obj, db_api=DuckDBAPI())

    # ------------------------------------- Tier 1b: embedding + name (+LLM) --
    def _tier1b(self, mention: EntityMention,
                candidates: Sequence[BlockedCandidate],
                policy: ResolutionPolicy,
                corroborators: set[int]) -> list[ScoredCandidate]:
        scored: list[ScoredCandidate] = []
        for cand in candidates:
            name_used, name_sim = best_name_variant(mention.surface_text, cand)
            if cand.cosine is None:
                base = name_sim
            else:
                base = (1 - COSINE_WEIGHT) * name_sim \
                    + COSINE_WEIGHT * max(cand.cosine, 0.0)
            features = {
                "name_used": name_used,
                "name_sim": round(name_sim, 4),
                "cosine": cand.cosine,
                "base": round(base, 4),
                "blocks": cand.blocks,
            }
            score = self._corroborate(mention.tenant_id, base,
                                      cand.entity_id, corroborators, features)
            scored.append(ScoredCandidate(
                entity_id=cand.entity_id, score=score, method="embedding",
                tier="t1b", band=self._band(score, policy), features=features))

        # LLM adjudication on the ambiguous residual ONLY: gray-band
        # candidates, best-first, capped. High and low bands never pay for a
        # model call.
        by_entity = {c.entity_id: c for c in candidates}
        gray = sorted((s for s in scored if s.band == "gray"),
                      key=lambda s: -s.score)[:self._max_adjudications]
        if gray:
            context = self._mention_context(mention)
            for s in gray:
                verdict = self._adjudicator.judge(
                    mention.surface_text, mention.entity_type, context,
                    by_entity[s.entity_id])
                if verdict is None:
                    s.features["adjudication"] = {"error": True,
                                                  "model": self._adjudicator.model}
                    continue
                same, confidence = verdict
                s.score = 0.5 + confidence / 2 if same else 0.5 - confidence / 2
                s.score = self._corroborate(
                    mention.tenant_id, s.score, s.entity_id, corroborators,
                    s.features)
                s.method = "llm"
                s.band = self._band(s.score, policy)
                s.features["adjudication"] = {
                    "same_entity": same, "confidence": confidence,
                    "model": self._adjudicator.model}
        return scored

    def _mention_context(self, mention: EntityMention) -> str:
        """A short window of the source chunk around the mention — the
        adjudicator's document-side evidence."""
        if mention.source_chunk_id is None:
            return ""
        chunk = self._store.get_chunk(mention.tenant_id, mention.source_chunk_id)
        if chunk is None:
            return ""
        text = chunk.content
        if mention.char_start is not None and chunk.char_start is not None:
            local = mention.char_start - chunk.char_start
            lo = max(0, local - ADJUDICATION_CONTEXT_CHARS // 2)
            return text[lo:lo + ADJUDICATION_CONTEXT_CHARS]
        return text[:ADJUDICATION_CONTEXT_CHARS]

    # --------------------------------------- Tier 1c: graph corroboration --
    def _document_corroborators(self, mention: EntityMention) -> set[int]:
        """Entities already resolved from OTHER mentions of the same
        document — the mention's local neighborhood, whose shared edges with
        a candidate corroborate a match."""
        if mention.source_document_id is None:
            return set()
        with self._store.transaction(mention.tenant_id) as conn:
            rows = conn.execute(
                "SELECT DISTINCT resolved_entity_id FROM entity_mentions"
                " WHERE tenant_id = %s AND source_document_id = %s"
                "   AND resolved_entity_id IS NOT NULL AND id <> %s",
                (mention.tenant_id, mention.source_document_id,
                 mention.id)).fetchall()
        return {r["resolved_entity_id"] for r in rows}

    def _corroborate(self, tenant_id: str, score: float, entity_id: int,
                     corroborators: set[int],
                     features: dict[str, Any]) -> float:
        """Count distinct shared-edge neighbors between the candidate and the
        mention's document neighborhood (relational facts are authoritative;
        the graph is their projection) and apply the bounded boost. The count
        is cached in `features` so adjudication can re-apply the boost to a
        replaced score without re-querying."""
        if "corroboration" in features:
            n = features["corroboration"]
        elif not corroborators:
            n = 0
        else:
            ids = sorted(corroborators)
            with self._store.transaction(tenant_id) as conn:
                row = conn.execute(
                    """
                    SELECT count(DISTINCT CASE
                               WHEN subject_entity_id = %s THEN object_entity_id
                               ELSE subject_entity_id END) AS n
                    FROM facts
                    WHERE tenant_id = %s AND valid_to IS NULL
                      AND ((subject_entity_id = %s AND object_entity_id = ANY(%s))
                        OR (object_entity_id = %s AND subject_entity_id = ANY(%s)))
                    """,
                    (entity_id, tenant_id, entity_id, ids, entity_id, ids),
                ).fetchone()
            n = row["n"]
        features["corroboration"] = n
        boost = min(CORROBORATION_BOOST_CAP, CORROBORATION_BOOST * n)
        features["corroboration_boost"] = round(boost, 4)
        return min(1.0, score + boost)

    # ------------------------------------------------------------- banding --
    def _policy(self, tenant_id: str, entity_type: str) -> ResolutionPolicy:
        if entity_type not in self._policies:
            row = self._store.get_resolution_policy(tenant_id, entity_type)
            if row is None:
                logger.warning("no resolution_policy row for %r — using the "
                               "conservative fallback", entity_type)
                row = ResolutionPolicy(entity_type=entity_type,
                                       **FALLBACK_POLICY)
            self._policies[entity_type] = row
        return self._policies[entity_type]

    @staticmethod
    def _band(score: float, policy: ResolutionPolicy) -> str:
        if score >= policy.t_high:
            return "high"
        if score <= policy.t_low:
            return "low"
        return "gray"

    def _decide(self, scored: list[ScoredCandidate],
                policy: ResolutionPolicy) -> ResolutionOutcome:
        best = max(scored, key=lambda s: s.score)
        common = dict(tier=best.tier, method=best.method, score=best.score,
                      band=best.band, features=best.features,
                      candidates=scored)
        if best.band == "low":
            return ResolutionOutcome(decision="new_entity",
                                     reason="below_t_low", **common)
        if best.band == "gray":
            return ResolutionOutcome(decision="review", reason="gray_band",
                                     **common)
        # high band — every remaining gate is an under-merge gate
        if not policy.auto_merge_allowed:
            return ResolutionOutcome(decision="review",
                                     reason="auto_merge_disabled", **common)
        if sum(1 for s in scored if s.band == "high") > 1:
            return ResolutionOutcome(decision="review",
                                     reason="multiple_high_candidates",
                                     **common)
        name_only = not best.features.get("key_overlap")
        if policy.requires_corroboration and name_only \
                and not best.features.get("corroboration"):
            return ResolutionOutcome(decision="review",
                                     reason="needs_corroboration", **common)
        return ResolutionOutcome(decision="resolved",
                                 entity_id=best.entity_id, **common)
