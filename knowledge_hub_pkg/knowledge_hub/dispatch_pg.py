"""PostgresDispatcher — outbox implementation of Dispatcher on dispatch_queue
(migration 002).

Why an outbox and not an in-process queue: at-least-once must survive a crash
between "file landed" and "processing started". A queue row in the same
Postgres as the landing row is durable, transactional, and adds no new infra;
if dispatch later moves to Dagster/Prefect/a broker, this table becomes the
outbox those consume from — the Dispatcher ABC call sites don't change.

Semantics:
  * dispatch() is IDEMPOTENT per (tenant, raw_document_id) — re-landing
    replays and crash-resume re-dispatches never duplicate the record.
  * Delivery is AT-LEAST-ONCE via leases: claim() marks rows inflight until
    a deadline (available_at); an unacked claim becomes claimable again once
    the lease expires, so a dead consumer redelivers rather than loses.
    Consumers must be idempotent.

The same outbox contract repeats one stage downstream (migration 004):
`extraction_queue` carries processing -> extraction handoffs with identical
shape and semantics, so ONE implementation serves both — pick the stage with
the `table` argument.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from knowledge_hub.factstore_pg import PostgresFactStore
from knowledge_hub.interfaces import Dispatcher
from knowledge_hub.models import DispatchMessage

DEFAULT_LEASE = timedelta(minutes=5)

# The two outbox stages. Table names are interpolated into SQL, so they come
# from this allowlist only — never from caller-supplied strings.
QUEUE_TABLES = ("dispatch_queue", "extraction_queue")


class PostgresDispatcher(Dispatcher):
    def __init__(self, store: PostgresFactStore, lease: timedelta = DEFAULT_LEASE,
                 table: str = "dispatch_queue"):
        if table not in QUEUE_TABLES:
            raise ValueError(f"table must be one of {QUEUE_TABLES}, got {table!r}")
        self._store = store
        self._lease = lease
        self._table = table

    # ---------------------------------------------------------------- produce
    def dispatch(self, tenant_id: str, raw_document_id: int,
                 delay: timedelta = timedelta(0)) -> int:
        """Enqueue (idempotent). `delay` defers claimability (available_at =
        now() + delay) — the §8.1g lazy re-extraction lever: a deferred
        message batches into a later drain instead of re-running the LLM
        immediately. A message that already exists keeps its schedule
        (idempotent re-dispatch never reschedules)."""
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                f"""
                INSERT INTO {self._table}
                    (tenant_id, raw_document_id, available_at)
                VALUES (%s, %s, now() + %s)
                ON CONFLICT (tenant_id, raw_document_id) DO NOTHING
                RETURNING id
                """,
                (tenant_id, raw_document_id, delay),
            ).fetchone()
            if row is None:  # already enqueued for this landing — idempotent
                row = conn.execute(
                    f"SELECT id FROM {self._table}"
                    " WHERE tenant_id = %s AND raw_document_id = %s",
                    (tenant_id, raw_document_id),
                ).fetchone()
        return row["id"]

    # ---------------------------------------------------------------- consume
    # Consumers arrive with the processing stages (Build Prompt 3+); the
    # claim/ack/nack cycle is implemented now because at-least-once is a
    # property of the whole loop, not of enqueueing alone.
    def claim(self, tenant_id: str, limit: int = 10) -> list[DispatchMessage]:
        """Lease up to `limit` deliverable messages: queued rows AND inflight
        rows whose lease expired (that expiry IS the redelivery path)."""
        with self._store.transaction(tenant_id) as conn:
            rows = conn.execute(
                f"""
                UPDATE {self._table} SET
                    status = 'inflight',
                    attempts = attempts + 1,
                    claimed_at = now(),
                    available_at = now() + %s
                WHERE id IN (
                    SELECT id FROM {self._table}
                    WHERE tenant_id = %s
                      AND status IN ('queued', 'inflight')
                      AND available_at <= now()
                    ORDER BY id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (self._lease, tenant_id, limit),
            ).fetchall()
        return [DispatchMessage(**r) for r in rows]

    def ack(self, tenant_id: str, message_id: int) -> None:
        self._finish(tenant_id, message_id, "done", None)

    def nack(self, tenant_id: str, message_id: int, error: Optional[str] = None,
             retry_in: timedelta = timedelta(0)) -> None:
        """Return a claim to the queue (immediately claimable by default)."""
        with self._store.transaction(tenant_id) as conn:
            updated = conn.execute(
                f"UPDATE {self._table} SET status = 'queued',"
                " available_at = now() + %s, last_error = %s"
                " WHERE tenant_id = %s AND id = %s",
                (retry_in, error, tenant_id, message_id),
            ).rowcount
        if updated == 0:
            raise LookupError(
                f"dispatch message id={message_id} not found for tenant {tenant_id!r}")

    def _finish(self, tenant_id: str, message_id: int, status: str,
                error: Optional[str]) -> None:
        with self._store.transaction(tenant_id) as conn:
            updated = conn.execute(
                f"UPDATE {self._table} SET status = %s, acked_at = now(),"
                " last_error = %s WHERE tenant_id = %s AND id = %s",
                (status, error, tenant_id, message_id),
            ).rowcount
        if updated == 0:
            raise LookupError(
                f"dispatch message id={message_id} not found for tenant {tenant_id!r}")

    # ------------------------------------------------------------- inspection
    def get_message(self, tenant_id: str, message_id: int) -> Optional[DispatchMessage]:
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                f"SELECT * FROM {self._table} WHERE tenant_id = %s AND id = %s",
                (tenant_id, message_id),
            ).fetchone()
        return DispatchMessage(**row) if row else None

    def pending_for(self, tenant_id: str,
                    raw_document_id: int) -> Optional[DispatchMessage]:
        with self._store.transaction(tenant_id) as conn:
            row = conn.execute(
                f"SELECT * FROM {self._table}"
                " WHERE tenant_id = %s AND raw_document_id = %s",
                (tenant_id, raw_document_id),
            ).fetchone()
        return DispatchMessage(**row) if row else None
