"""Build the retrieval gold sets from the ingested synthetic corpus — one per
retrieval track (sop / prose / communication), registered as DRAFTS for
review + activation. Local models only.

    python build_gold_retrieval.py                # all tracks, 60 queries each
    python build_gold_retrieval.py --per-track 60 --version 0.1

Per track:
  * corpus = ALL child chunks of that track for tenant bench-synth (the
    retrieval universe the evaluator brute-forces; corpus_hash pinned).
  * ~90% single-hop: one leakage-guarded query per sampled chunk
    (LLMQueryGenerator: paraphrase forced, terse register, Jaccard < 0.6).
  * ~10% multi-hop: a question whose answer spans TWO chunks of the same
    document (both marked relevant; any-of/all-of recall handles it).
  * hard negatives: the relevant chunk's nearest embedded neighbor outside
    its own document (bge-m3 vectors already stored) — model-mined, flagged
    in spec as such; the human spot-check happens on the DRAFT before
    activation (methodology §6.4).

The sets stay DRAFT until a named human activates them — that's the review
gate, not a formality: edit/reject bad queries first, and author some of your
own (the methodology's floors want >=10 SME-authored per track; drafts count
model-generated only, so the reviewer's edits are what close that gap).
"""
from __future__ import annotations

import argparse
import random
import sys

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.goldsets import GoldSetError, GoldSetStore, LLMQueryGenerator

TENANT = "bench-synth"
TRACKS = ("sop", "prose", "communication")
SEED = 20260723


def _multihop_query(gen: LLMQueryGenerator, text_a: str, text_b: str) -> dict:
    """One question that needs BOTH passages; same leakage guard as single-hop
    (applied against the concatenation)."""
    import json as _json
    from knowledge_hub.goldsets import LEAKAGE_JACCARD_MAX, jaccard
    prompt = (
        "You write search queries real employees would type. Read the TWO "
        "passages (from the same document) and write ONE terse, realistic "
        "question that can only be answered by combining information from "
        "both. Do NOT reuse their wording; under 18 words.\n\n"
        f"Passage A:\n{text_a}\n\nPassage B:\n{text_b}")
    for attempt in range(1, 4):
        resp = gen._client.chat(
            model=gen.model,
            messages=[{"role": "user", "content": prompt}],
            format={"type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"]},
            think=False, options={"temperature": 0.7})
        question = _json.loads(resp.message.content)["question"].strip()
        j = jaccard(question, text_a + " " + text_b)
        if question and j < LEAKAGE_JACCARD_MAX:
            return {"question": question, "leakage_jaccard": round(j, 3),
                    "attempts": attempt}
    raise GoldSetError("multi-hop query kept leaking after 3 attempts")


def build_track(store: PostgresFactStore, gen: LLMQueryGenerator,
                gold_store: GoldSetStore, track: str, per_track: int,
                version: str, rng: random.Random) -> None:
    with store.transaction(TENANT) as conn:
        chunks = conn.execute(
            """SELECT ch.id, ch.content, ch.content_hash, ch.document_id
               FROM chunks ch JOIN documents d ON d.id = ch.document_id
               WHERE ch.tenant_id=%s AND ch.level='child'
                 AND d.doc_type=%s AND ch.embedding IS NOT NULL
               ORDER BY ch.id""", (TENANT, track)).fetchall()
    if len(chunks) < 10:
        print(f"[{track}] only {len(chunks)} chunks — skipping")
        return
    corpus_ids = [c["id"] for c in chunks]
    import hashlib
    corpus_hash = hashlib.sha256(
        "".join(c["content_hash"] for c in chunks).encode()).hexdigest()
    by_doc: dict[int, list[dict]] = {}
    for c in chunks:
        by_doc.setdefault(c["document_id"], []).append(c)

    n_multi = max(6, per_track // 10)
    n_single = per_track - n_multi
    sampled = rng.sample(chunks, min(n_single, len(chunks)))
    multi_docs = [d for d in by_doc.values() if len(d) >= 2]

    items, rejected = [], 0
    for c in sampled:
        try:
            q = gen.generate_query(c["content"])
        except GoldSetError:
            rejected += 1
            continue
        hard = _nearest_other_doc(store, c["id"], c["document_id"])
        items.append({"query": q["question"],
                      "relevant_chunk_ids": [c["id"]],
                      "hard_negative_chunk_ids": hard,
                      "multi_hop": False,
                      "leakage_jaccard": q["leakage_jaccard"]})
    made_multi, tries = 0, 0
    while made_multi < n_multi and multi_docs and tries < n_multi * 4:
        tries += 1
        doc_chunks = rng.choice(multi_docs)
        a, b = rng.sample(doc_chunks, 2)
        try:
            q = _multihop_query(gen, a["content"], b["content"])
        except GoldSetError:
            rejected += 1
            continue
        items.append({"query": q["question"],
                      "relevant_chunk_ids": [a["id"], b["id"]],
                      "hard_negative_chunk_ids": [],
                      "multi_hop": True,
                      "leakage_jaccard": q["leakage_jaccard"]})
        made_multi += 1

    spec = {"track": track, "synthetic": True, "tenant": TENANT,
            "corpus_chunk_ids": corpus_ids, "corpus_hash": corpus_hash,
            "hard_negatives": "model-mined nearest-neighbor, unreviewed",
            "sme_authored": 0,  # reviewer edits on the draft close this gap
            "query_generator": f"{gen.model} (LLMQueryGenerator)",
            "rejected_for_leakage": rejected}
    gs = gold_store.register(TENANT, "retrieval", f"synth-{track}-{version}",
                             items, generator="llm_corpus", spec=spec)
    print(f"[{track}] DRAFT {gs.version}: {gs.item_count} items "
          f"({made_multi} multi-hop, {rejected} rejected for leakage), "
          f"corpus={len(corpus_ids)} chunks, floors_met={gs.floors_met}")
    for it in items[:3]:
        print(f"    e.g. \"{it['query']}\"")


def _nearest_other_doc(store: PostgresFactStore, chunk_id: int,
                       document_id: int) -> list[int]:
    """Nearest embedded neighbor OUTSIDE the chunk's own document — a
    topically-close non-answering candidate (the human confirms on review)."""
    with store.transaction(TENANT) as conn:
        rows = conn.execute(
            """SELECT ch.id FROM chunks ch
               WHERE ch.tenant_id=%s AND ch.level='child'
                 AND ch.embedding IS NOT NULL AND ch.document_id <> %s
               ORDER BY ch.embedding <=> (SELECT embedding FROM chunks
                                          WHERE id=%s)
               LIMIT 2""", (TENANT, document_id, chunk_id)).fetchall()
    return [r["id"] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-track", type=int, default=60)
    ap.add_argument("--version", default="0.1")
    ap.add_argument("--tracks", nargs="*", default=list(TRACKS))
    args = ap.parse_args()

    store = PostgresFactStore()
    gen = LLMQueryGenerator()
    gold_store = GoldSetStore(store)
    rng = random.Random(SEED)
    for track in args.tracks:
        build_track(store, gen, gold_store, track, args.per_track,
                    args.version, rng)
    print("\nAll sets are DRAFTS. Review (edit/reject/author queries), then "
          "activate:\n  GoldSetStore.activate(tenant, 'retrieval', "
          "'synth-<track>-0.1', by='<your name>')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
