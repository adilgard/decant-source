"""The AGE graph projection is RETIRED (Build Prompt 9): write_facts is
relational-only on the default path. The projection code stays as frozen
reference behind settings.project_to_age (off, and not a casual toggle —
edges are known-stale since BP7/BP8; resurrection is a project, see
AGE_DORMANT.md). There is deliberately NO flag-on test: it would assert a
projection that retraction never updates — enshrining wrong behavior."""
from __future__ import annotations

import json

from factories import ONTOLOGY, land_document, make_entity
from knowledge_hub.models import Fact


def _one(store, tenant, body):
    ((val,),) = store.run_cypher(tenant, body, ncols=1)
    return json.loads(str(val))


def test_write_facts_is_relational_only_no_graph_writes(store, pipeline, tenant):
    doc = land_document(pipeline, store, tenant)
    acme = store.upsert_entity(make_entity(tenant, "Acme"))
    globex = store.upsert_entity(make_entity(tenant, "Globex"))

    (fact_id,) = store.write_facts([Fact(
        tenant_id=tenant, subject_entity_id=acme, predicate="owns",
        object_entity_id=globex, ontology_version=ONTOLOGY,
        source_document_id=doc.id, extractor="test", extractor_version="1")])

    # The relational row — the truth — landed.
    with store.transaction(tenant) as conn:
        row = conn.execute(
            "SELECT predicate FROM facts WHERE tenant_id = %s AND id = %s",
            (tenant, fact_id)).fetchone()
    assert row["predicate"] == "owns"

    # The retired projection wrote NOTHING: no vertices, no edge. (run_cypher
    # is the tests-only diagnostic door; the graph itself is still installed,
    # just frozen empty for new writes.)
    assert _one(store, tenant,
                f"MATCH (n:Entity) WHERE n.tenant_id = '{tenant}'"
                f" RETURN count(n)") == 0
    assert _one(store, tenant,
                f"MATCH ()-[r:REL {{fact_id: {fact_id}}}]->()"
                f" RETURN count(r)") == 0


def test_merge_projection_maintenance_is_noop(store, tenant):
    # The merge/reversal graph-maintenance helpers are gated no-ops too —
    # resolution's calls go through them unchanged and touch nothing.
    store.delete_fact_edge(tenant, 999_999)      # would MATCH..DELETE if live
    store.delete_entity_vertex(tenant, 999_999)  # would DETACH DELETE if live
    assert _one(store, tenant,
                f"MATCH (n) WHERE n.tenant_id = '{tenant}'"
                f" RETURN count(n)") == 0
