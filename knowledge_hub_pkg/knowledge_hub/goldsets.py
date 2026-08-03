"""Gold-set machinery + generators (Build Prompt 6, methodology §6.3/§6.4).

GoldSetStore is the versioning layer: register() writes an immutable DRAFT
(items content-hashed, set hash = sha256 over ordered item hashes, §6.2
floors evaluated), activate() flips it to active under a named human —
the runner refuses drafts, so review/spot-check structurally precedes use.

Generators, per the PRIVACY FORK (§3.1 of the progress doc): tooling that
touches REAL tenant data runs on LOCAL models only (Ollama). Claude Code
builds these and exercises them on synthetic seed data; the same code is
what later runs on-infra against tenant corpora.

  * SyntheticRetrievalGenerator — deterministic synthetic corpus + queries;
    powers the recording dry-run and the harness tests. No model calls
    except live bge-m3 embedding (the real pipeline's embedder).
  * LLMQueryGenerator — realistic retrieval queries from real chunks via a
    local model, with the leakage guard (near-verbatim rejected by token
    Jaccard) and terse-register prompting. For the campaign phase.
  * ERGoldGenerator — labeled pairs from (1) the labels flywheel and
    (2) corruption/augmentation of known entities (guaranteed positives +
    constructed hard negatives). Deterministic under an explicit seed.
  * ExtractionGoldDrafter — drafts expected-facts items from the pipeline's
    own observability (pending_facts + quarantine) for SME review: labeling
    is review, not authorship.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Any, Optional, Sequence

from knowledge_hub.config import settings
from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import Embedder
from knowledge_hub.models import GoldSet, GoldSetItem

GENERATOR_VERSION = "0.1.0"

# §6.2 statistical floors (per-set minimums; per-track/per-type detail rides
# in spec and is judged at methodology level — below floor, runs record as
# advisory and cannot decide).
FLOORS = {"retrieval": 50, "er": 200, "extraction": 30}

LEAKAGE_JACCARD_MAX = 0.6   # methodology §6.3: above this, a query is verbatim-ish


class GoldSetError(Exception):
    pass


def _canonical_hash(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# Versioning layer
# ---------------------------------------------------------------------------
class GoldSetStore:
    def __init__(self, store: PostgresFactStore):
        self._store = store

    def register(self, tenant_id: str, kind: str, version: str,
                 items: Sequence[dict[str, Any]], *, generator: str,
                 generator_version: str = GENERATOR_VERSION,
                 spec: Optional[dict[str, Any]] = None) -> GoldSet:
        """Write a new DRAFT gold set. Immutable: same (kind, version) again
        is a refusal, not an upsert — bump the version instead."""
        if not items:
            raise GoldSetError("a gold set needs at least one item")
        item_hashes = [_canonical_hash(it) for it in items]
        content_hash = hashlib.sha256("".join(item_hashes).encode()).hexdigest()
        floors_met = len(items) >= FLOORS[kind]
        with self._store.transaction(tenant_id) as conn:
            dup = conn.execute(
                "SELECT 1 FROM gold_sets WHERE tenant_id=%s AND kind=%s AND version=%s",
                (tenant_id, kind, version)).fetchone()
            if dup:
                raise GoldSetError(
                    f"gold set {kind}/{version} already exists for {tenant_id} "
                    "— gold sets are immutable, bump the version")
            row = conn.execute(
                """INSERT INTO gold_sets (tenant_id, kind, version, generator,
                       generator_version, item_count, content_hash, floors_met, spec)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (tenant_id, kind, version, generator, generator_version,
                 len(items), content_hash, floors_met,
                 json.dumps(spec or {}))).fetchone()
            for seq, (it, h) in enumerate(zip(items, item_hashes), start=1):
                conn.execute(
                    "INSERT INTO gold_set_items (gold_set_id, seq, item, item_hash) "
                    "VALUES (%s,%s,%s,%s)", (row["id"], seq, json.dumps(it), h))
        return GoldSet(**row)

    def activate(self, tenant_id: str, kind: str, version: str, *,
                 by: str) -> GoldSet:
        """draft -> active, under a named human. The review/spot-check step
        happens on the draft; activation is the sign-off that it happened."""
        if not by or not by.strip():
            raise GoldSetError("activation requires a named reviewer (by=...)")
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                """UPDATE gold_sets SET status='active', activated_at=now(),
                       activated_by=%s
                   WHERE tenant_id=%s AND kind=%s AND version=%s AND status='draft'
                   RETURNING *""", (by, tenant_id, kind, version)).fetchone()
        if row is None:
            raise GoldSetError(
                f"no draft gold set {kind}/{version} for {tenant_id}")
        return GoldSet(**row)

    def get(self, tenant_id: str, kind: str,
            version: str) -> tuple[GoldSet, list[GoldSetItem]]:
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                "SELECT * FROM gold_sets WHERE tenant_id=%s AND kind=%s AND version=%s",
                (tenant_id, kind, version)).fetchone()
            if row is None:
                raise GoldSetError(f"no gold set {kind}/{version} for {tenant_id}")
            items = conn.execute(
                "SELECT * FROM gold_set_items WHERE gold_set_id=%s ORDER BY seq",
                (row["id"],)).fetchall()
        return GoldSet(**row), [GoldSetItem(**i) for i in items]


# ---------------------------------------------------------------------------
# Synthetic retrieval corpus + gold set (dry-run + tests)
# ---------------------------------------------------------------------------
# Content is deliberately paraphrase-distant from its query (leakage guard is
# ASSERTED at generation time, not hoped for). Topically-similar pairs give
# every query a real hard negative.
_SYNTHETIC_CHUNKS = [
    ("Cleaning logs are retained for a period of seven years in the quality "
     "assurance archive, after which they are destroyed under supervision.",
     "How long do we keep equipment wash records?",
     1),
    ("The mixing vessel must be rinsed with purified water and inspected for "
     "residue before every production changeover.",
     "What has to happen to the tank between product runs?",
     0),
    ("Batch release requires sign-off from the quality reviewer, who confirms "
     "the record set is complete and the specifications were met.",
     "Who approves a lot for shipment?",
     3),
    ("Deviation reports are submitted within one business day of discovery "
     "and assigned a severity classification by the investigation lead.",
     "When is a deviation write-up due?",
     2),
    ("Annual requalification of the labeling machine includes a print-quality "
     "challenge and verification of the reject mechanism.",
     "What does the yearly labeler check involve?",
     4),
]


class SyntheticRetrievalGenerator:
    """Builds a small synthetic corpus in the DB (raw doc -> document ->
    parent -> embedded children, the real tables) plus a query set, and
    returns (items, spec) ready for GoldSetStore.register."""

    def __init__(self, store: PostgresFactStore, embedder: Embedder):
        self._store = store
        self._embedder = embedder

    def generate(self, tenant_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        texts = [c for c, _, _ in _SYNTHETIC_CHUNKS]
        vectors = self._embedder.embed(texts)
        chunk_ids: list[int] = []
        chunk_hashes: list[str] = []
        with self._store.transaction(tenant_id) as conn:
            doc_hash = _canonical_hash({"synthetic_corpus": tenant_id})
            raw = conn.execute(
                """INSERT INTO raw_documents (tenant_id, source_system,
                       source_native_id, mime_type, content_hash, raw_uri, status)
                   VALUES (%s,'benchmark_synthetic','dryrun-doc','text/plain',%s,
                           'synthetic://benchmark/dryrun','parsed')
                   RETURNING id""", (tenant_id, doc_hash)).fetchone()
            doc = conn.execute(
                """INSERT INTO documents (tenant_id, raw_document_id, doc_type, title)
                   VALUES (%s,%s,'prose','Synthetic benchmark corpus') RETURNING id""",
                (tenant_id, raw["id"])).fetchone()
            parent = conn.execute(
                """INSERT INTO chunks (tenant_id, document_id, level, seq,
                       content, content_hash)
                   VALUES (%s,%s,'parent',0,%s,%s) RETURNING id""",
                (tenant_id, doc["id"], " ".join(texts),
                 _canonical_hash({"parent": texts}))).fetchone()
            for seq, (text, vec) in enumerate(zip(texts, vectors), start=1):
                h = hashlib.sha256(f"{tenant_id}:{text}".encode()).hexdigest()
                row = conn.execute(
                    """INSERT INTO chunks (tenant_id, document_id, parent_chunk_id,
                           level, seq, content, content_hash, embedding,
                           embedding_model, embedding_version)
                       VALUES (%s,%s,%s,'child',%s,%s,%s,%s::vector,%s,%s)
                       RETURNING id""",
                    (tenant_id, doc["id"], parent["id"], seq, text, h,
                     "[" + ",".join(map(str, vec)) + "]",
                     getattr(self._embedder, "model", "unknown"),
                     getattr(self._embedder, "version", "unknown"))).fetchone()
                chunk_ids.append(row["id"])
                chunk_hashes.append(h)

        items: list[dict[str, Any]] = []
        for ix, (text, query, hard_ix) in enumerate(_SYNTHETIC_CHUNKS):
            j = jaccard(query, text)
            if j >= LEAKAGE_JACCARD_MAX:
                raise GoldSetError(
                    f"synthetic query {ix} leaks its chunk (jaccard {j:.2f})")
            items.append({
                "query": query,
                "relevant_chunk_ids": [chunk_ids[ix]],
                "hard_negative_chunk_ids": [chunk_ids[hard_ix]],
                "multi_hop": False,
                "leakage_jaccard": round(j, 3),
            })
        spec = {
            "track": "prose",
            "synthetic": True,
            "corpus_chunk_ids": chunk_ids,
            "corpus_hash": hashlib.sha256("".join(chunk_hashes).encode()).hexdigest(),
            "document_id": None,  # synthetic; not tied to a real source doc
        }
        return items, spec


# ---------------------------------------------------------------------------
# LLM query generation (campaign phase; local models only on real data)
# ---------------------------------------------------------------------------
_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"question": {"type": "string"}},
    "required": ["question"],
}

_QUERY_PROMPT = (
    "You write search queries real employees would type. Read the passage and "
    "write ONE terse, messy, realistic question it answers. Rules: do NOT "
    "reuse the passage's wording — paraphrase everything; under 15 words; no "
    "question mark needed; write like someone in a hurry.\n\nPassage:\n{chunk}"
)


class LLMQueryGenerator:
    """One realistic query per chunk via a LOCAL model, leakage-guarded.

    Rejection is deterministic (token Jaccard >= 0.6 vs the chunk) and
    re-prompts up to `max_attempts`; a chunk that never passes raises rather
    than silently admitting a verbatim query into ground truth.
    """

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None,
                 max_attempts: int = 3):
        import ollama
        self.model = model or settings.extraction_model
        self._client = ollama.Client(host=host or settings.ollama_host)
        self._max_attempts = max_attempts

    def generate_query(self, chunk_text: str) -> dict[str, Any]:
        last_j = None
        for attempt in range(1, self._max_attempts + 1):
            resp = self._chat_with_retry(_QUERY_PROMPT.format(chunk=chunk_text))
            question = json.loads(resp.message.content)["question"].strip()
            last_j = jaccard(question, chunk_text)
            if question and last_j < LEAKAGE_JACCARD_MAX:
                return {"question": question, "leakage_jaccard": round(last_j, 3),
                        "attempts": attempt}
        raise GoldSetError(
            f"query generation kept leaking (last jaccard {last_j:.2f} "
            f">= {LEAKAGE_JACCARD_MAX}) after {self._max_attempts} attempts")

    def _chat_with_retry(self, content: str):
        """Leakage retries are policy; SERVING retries are plumbing. A
        transient llama-server hiccup (observed twice under GPU model churn)
        must not fail a long gold-set build."""
        import time
        last: Exception | None = None
        for backoff in (0, 5, 15):
            if backoff:
                time.sleep(backoff)
            try:
                return self._client.chat(
                    model=self.model,
                    messages=[{"role": "user", "content": content}],
                    format=_QUERY_SCHEMA, think=False,
                    options={"temperature": 0.7})
            except Exception as e:
                last = e
        raise GoldSetError(f"query model unavailable after retries: {last}")


# ---------------------------------------------------------------------------
# ER gold generation: flywheel labels + corruption/augmentation
# ---------------------------------------------------------------------------
_SUFFIX_VARIANTS = {
    "corp": ["corporation", "corp.", "co"],
    "corporation": ["corp", "corp.", "co"],
    "inc": ["incorporated", "inc.", "co"],
    "llc": ["l.l.c.", "limited liability co"],
    "ltd": ["limited", "ltd."],
}


class ERGoldGenerator:
    """Labeled pairs from the two bootstrap sources that exist pre-SoR:

    from_labels()  — the flywheel (§3.4): review decisions, reversals, T0
                     positives, exported with their source + authority.
    corruption()   — known entities -> controlled variants (guaranteed
                     positives) + similar-but-distinct hard negatives.
                     Deterministic under `seed`.
    """

    def __init__(self, store: PostgresFactStore):
        self._store = store

    def from_labels(self, tenant_id: str) -> list[dict[str, Any]]:
        with self._store.transaction(tenant_id) as conn:
            rows = conn.execute(
                """SELECT label_type, payload, source, authority, confidence
                   FROM labels WHERE tenant_id=%s
                     AND label_type IN ('er_match','er_nonmatch')
                   ORDER BY id""", (tenant_id,)).fetchall()
        return [{
            "left": r["payload"].get("left"),
            "right": r["payload"].get("right"),
            "match": r["label_type"] == "er_match",
            "source": r["source"],
            "authority": r["authority"],
            "confidence": r["confidence"],
        } for r in rows]

    def corruption(self, entities: Sequence[tuple[str, str]], *,
                   seed: int, variants_per_entity: int = 2) -> list[dict[str, Any]]:
        rng = random.Random(seed)
        items: list[dict[str, Any]] = []
        for name, entity_type in entities:
            for _ in range(variants_per_entity):
                items.append({
                    "left_name": self._variant(name, rng),
                    "right_name": name,
                    "entity_type": entity_type,
                    "match": True,
                    "source": "corruption",
                    "authority": 1.0,
                })
        # Hard negatives: entities sharing a leading token but genuinely
        # distinct — the pairs that stress precision (§3.3 of the progress doc).
        for i, (a, ta) in enumerate(entities):
            for b, tb in entities[i + 1:]:
                if a.split()[0].lower() == b.split()[0].lower() and a != b:
                    items.append({
                        "left_name": a, "right_name": b,
                        "entity_type": ta if ta == tb else f"{ta}|{tb}",
                        "match": False, "hard_negative": True,
                        "source": "constructed", "authority": 1.0,
                    })
        return items

    @staticmethod
    def _variant(name: str, rng: random.Random) -> str:
        words = name.split()
        roll = rng.random()
        last = words[-1].lower().rstrip(".")
        if roll < 0.4 and last in _SUFFIX_VARIANTS:
            words[-1] = rng.choice(_SUFFIX_VARIANTS[last]).title()
            return " ".join(words)
        if roll < 0.7 and len(words) > 1:
            return " ".join(w[0].upper() + "." for w in words[:-1]) + " " + words[-1]
        # typo: drop one interior character of the longest word
        target = max(range(len(words)), key=lambda i: len(words[i]))
        w = words[target]
        if len(w) > 3:
            cut = rng.randrange(1, len(w) - 1)
            words[target] = w[:cut] + w[cut + 1:]
        return " ".join(words)


# ---------------------------------------------------------------------------
# Extraction gold drafting: SME reviews, never authors from scratch
# ---------------------------------------------------------------------------
class ExtractionGoldDrafter:
    """Drafts expected-facts items per parent chunk from what the pipeline
    already observed (staged facts + quarantined attempts). Every drafted
    item carries reviewed=false; the SME keeps/edits/rejects on the DRAFT
    gold set before a human activates it."""

    def __init__(self, store: PostgresFactStore):
        self._store = store

    def draft(self, tenant_id: str, document_id: int) -> list[dict[str, Any]]:
        with self._store.transaction(tenant_id) as conn:
            facts = conn.execute(
                """SELECT source_chunk_id, subject_ref, predicate, object_ref,
                          object_literal, grounding
                   FROM pending_facts
                   WHERE tenant_id=%s AND source_document_id=%s
                   ORDER BY source_chunk_id, id""",
                (tenant_id, document_id)).fetchall()
            quarantined = conn.execute(
                """SELECT source_chunk_id, reason, detail
                   FROM quarantined_extractions
                   WHERE tenant_id=%s AND document_id=%s ORDER BY id""",
                (tenant_id, document_id)).fetchall()
        by_chunk: dict[int, dict[str, Any]] = {}
        for f in facts:
            entry = by_chunk.setdefault(f["source_chunk_id"], {
                "parent_chunk_id": f["source_chunk_id"],
                "expected_facts": [], "off_ontology": [],
                "drafted_from": "observability", "reviewed": False,
            })
            entry["expected_facts"].append({
                "subject_ref": f["subject_ref"], "predicate": f["predicate"],
                "object_ref": f["object_ref"], "object_literal": f["object_literal"],
                "grounding": f["grounding"],
            })
        for q in quarantined:
            entry = by_chunk.setdefault(q["source_chunk_id"] or 0, {
                "parent_chunk_id": q["source_chunk_id"],
                "expected_facts": [], "off_ontology": [],
                "drafted_from": "observability", "reviewed": False,
            })
            entry["off_ontology"].append({"reason": q["reason"], "detail": q["detail"]})
        return list(by_chunk.values())
