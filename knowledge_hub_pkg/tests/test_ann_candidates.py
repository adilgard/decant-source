"""Embedding blocking: cosine nearest-neighbors with tenant + type filters."""
from __future__ import annotations

import pytest

from conftest import mix_vec, unit_vec
from factories import make_entity


def test_ann_orders_by_cosine_and_filters(store, tenant):
    near = store.upsert_entity(make_entity(
        tenant, "Acme Corporation", "Organization", embedding=unit_vec(0)))
    mid = store.upsert_entity(make_entity(
        tenant, "Acme Ltd", "Organization", embedding=mix_vec(0, 1, 0.6, 0.8)))
    far = store.upsert_entity(make_entity(
        tenant, "Globex", "Organization", embedding=unit_vec(1)))

    # Same vector as `near`, but excluded by each filter in turn:
    other_tenant = store.upsert_entity(make_entity(
        f"{tenant}-other", "Acme Shadow", "Organization", embedding=unit_vec(0)))
    other_type = store.upsert_entity(make_entity(
        tenant, "Acme The Person", "Person", embedding=unit_vec(0)))
    no_embedding = store.upsert_entity(make_entity(tenant, "Embeddingless"))

    query = mix_vec(0, 1, 0.95, 0.05)
    hits = store.ann_candidates(tenant, query, "Organization", k=10)

    ids = [h.entity_id for h in hits]
    assert ids == [near, mid, far]
    assert hits[0].similarity > hits[1].similarity > hits[2].similarity
    assert hits[0].similarity == pytest.approx(0.9987, abs=1e-3)
    assert {other_tenant, other_type, no_embedding}.isdisjoint(ids)
    assert hits[0].canonical_name == "Acme Corporation"

    # k caps the result set
    assert len(store.ann_candidates(tenant, query, "Organization", k=2)) == 2


def test_ann_excludes_retired_entities(store, tenant):
    from datetime import datetime, timezone
    store.upsert_entity(make_entity(
        tenant, "Retired Corp", "Organization", embedding=unit_vec(2),
        valid_to=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    live = store.upsert_entity(make_entity(
        tenant, "Live Corp", "Organization", embedding=unit_vec(2)))

    hits = store.ann_candidates(tenant, unit_vec(2), "Organization", k=5)
    assert [h.entity_id for h in hits] == [live]
