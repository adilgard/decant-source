"""Dispatcher outbox semantics on the real Postgres: idempotent enqueue,
reference-only rows, and at-least-once delivery via lease expiry."""
from __future__ import annotations

from datetime import timedelta

from factories import make_raw
from knowledge_hub.dispatch_pg import PostgresDispatcher


def landed_doc(pipeline, tenant) -> int:
    raw = make_raw(tenant)
    return pipeline.ingest_raw(raw)


def test_dispatch_is_idempotent_per_document(dispatcher, pipeline, tenant):
    raw_id = landed_doc(pipeline, tenant)
    first = dispatcher.dispatch(tenant, raw_id)
    second = dispatcher.dispatch(tenant, raw_id)  # crash-resume re-dispatch

    assert first == second
    message = dispatcher.get_message(tenant, first)
    assert message.raw_document_id == raw_id  # a reference, not a payload
    assert message.status == "queued" and message.attempts == 0


def test_claim_ack_lifecycle(dispatcher, pipeline, tenant):
    raw_id = landed_doc(pipeline, tenant)
    message_id = dispatcher.dispatch(tenant, raw_id)

    claimed = dispatcher.claim(tenant)
    assert [m.id for m in claimed] == [message_id]
    assert claimed[0].status == "inflight" and claimed[0].attempts == 1
    assert dispatcher.claim(tenant) == []  # leased: nobody else can claim it

    dispatcher.ack(tenant, message_id)
    assert dispatcher.get_message(tenant, message_id).status == "done"
    assert dispatcher.claim(tenant) == []  # done is done


def test_expired_lease_redelivers(store, pipeline, tenant):
    # Zero-length lease: an unacked claim is immediately claimable again —
    # the at-least-once property, compressed in time.
    impatient = PostgresDispatcher(store, lease=timedelta(0))
    raw_id = landed_doc(pipeline, tenant)
    impatient.dispatch(tenant, raw_id)

    first = impatient.claim(tenant)
    assert len(first) == 1 and first[0].attempts == 1
    redelivered = impatient.claim(tenant)  # consumer died; lease lapsed
    assert len(redelivered) == 1 and redelivered[0].attempts == 2
    assert redelivered[0].id == first[0].id


def test_nack_requeues_with_error(dispatcher, pipeline, tenant):
    raw_id = landed_doc(pipeline, tenant)
    message_id = dispatcher.dispatch(tenant, raw_id)
    dispatcher.claim(tenant)

    dispatcher.nack(tenant, message_id, error="parser exploded")

    message = dispatcher.get_message(tenant, message_id)
    assert message.status == "queued" and message.last_error == "parser exploded"
    assert [m.id for m in dispatcher.claim(tenant)] == [message_id]


def test_queue_is_tenant_scoped(dispatcher, pipeline, tenant):
    other = f"{tenant}-b"
    dispatcher.dispatch(tenant, landed_doc(pipeline, tenant))
    dispatcher.dispatch(other, landed_doc(pipeline, other))

    assert len(dispatcher.claim(tenant)) == 1
    assert len(dispatcher.claim(other)) == 1
