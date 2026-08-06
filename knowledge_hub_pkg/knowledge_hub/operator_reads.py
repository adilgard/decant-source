"""Operator READ endpoints — the monitor + review data layer (BP20 Part A).

BP19 built operator WRITES (+ the alerts read); the UI's monitor and review
surfaces had NO clean read path — the mockup hardcoded data and a naive UI
would read the DB directly, which is a SIDE DOOR (the §8.8 no-bypass promise
forbids exactly that). This module closes it: operator-shaped, read-only,
tenant-scoped snapshots served through the SAME boundary as the writes —
identity from the resolved principal, role-gated (reviewer ⊂ operator),
tenant injected server-side, absence for anything outside the tenant.

These reads are OPERATOR-shaped, not fact-envelope-shaped — pipeline
counters, queue listings, candidate-pair evidence. That is precisely why
they live on the operator service and not the serving choke point, whose
envelope model they don't fit. Agents never call these; their read path
stays the serving layer.

Read-only is structural here: every query is a SELECT over the tenant's
rows; nothing in this module holds a write path, and reads are never
audited (the audit trail is the WRITE trail).

  * monitor(tenant)   — the pipeline snapshot: docs landed, facts (confident
    vs low), per-stage counts (capture/process/extract/resolve/facts),
    per-source progress from the registry, a 28-minute docs/min throughput
    series, review counts, serving p95 (proxied server-side from the read
    service's /v1/metrics — the UI stays single-origin), uptime.
  * activity(tenant)  — recent pipeline events, newest first, in the
    mock's copy voice (`stage · detail`): outbox transitions, extraction
    runs, resolution decisions, operator actions.
  * reviews(tenant)   — the review-queue LISTING: merges (least confident
    first) + quarantined + flagged, with the counts the queue header shows.
  * review_detail(tenant, kind, ref_id) — the candidate-pair evidence
    panel: both candidates + identifiers/aliases/fact counts, evidence-for
    / evidence-against derived from the resolver's recorded features, the
    band thresholds from resolution_policy, and the source passage with
    provenance.

Honest approximations (v1, documented not hidden): per-source landed counts
are grouped by source_system (raw rows don't carry source_ref); source
totals are unknown until adapters report corpus size (bar shows landed,
not landed/total); evidence-for/against is deterministic copy over the
recorded feature values — it never invents evidence.

Keep in lock-step with operator_http.py (routing + role scope) and the
schema tables it reads (raw_documents, documents, chunks, *_queue,
extraction_runs, entity_mentions, match_candidates, quarantined_
extractions, resolution_policy, entity_merges, operator_audit, labels).
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from knowledge_hub.config import settings
from knowledge_hub.factstore_pg import PostgresFactStore

# Grounding verdicts that mean "asserted but weakly supported" — mirrors
# operations._FLAGGED_GROUNDING (the serving state rule, reused for counts).
_FLAGGED = ("span_missing", "components_missing")

# ---------------------------------------------------------------------------
# d.s Stage 4 — plain language at the display boundary. The raw codes stay
# load-bearing everywhere else (DB rows, ops params, khctl output); ONLY the
# console-facing strings composed here translate them. An unknown code falls
# through raw — a translation table must never hide a new failure class.
# ---------------------------------------------------------------------------
_QUARANTINE_PLAIN = {
    "unbound_predicate": "uses a relationship the ontology does not allow",
    "unbound_entity_type": "names an entity type the ontology does not allow",
    "validation_failure": "output did not fit the required shape",
}
_TIER_PLAIN = {
    "t0": "exact identifier",
    "t1": "name similarity",
    "t1b": "statistical score",
    "none": "no signal",
}
_DECISION_PLAIN = {
    "resolved": "matched an existing entity",
    "new_entity": "new entity created",
    "review": "held for a human",
}
_STRATEGY_PLAIN = {
    "llm": "language model",
    "llm_joint": "language model",
    "parser_supplied": "plugin",
    "structured_map": "column mapping",
}


def _flag_plain(reason: Optional[str]) -> str:
    """Flag reasons are stored PROSE (with spec citations) — translate the
    known shape, fall through raw for anything new."""
    if reason and reason.startswith("declared data_track"):
        return "arrived labeled one way, content looks like another"
    return reason or "at capture"

_ACTIVITY_LIMIT = 30
_THROUGHPUT_WINDOW_MIN = 28
_SERVING_METRICS_TTL_S = 10.0


def _hms(ts: Optional[datetime]) -> str:
    if ts is None:
        return "--:--:--"
    return ts.astimezone().strftime("%H:%M:%S")


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


class OperatorReadService:
    """Tenant-scoped, read-only snapshots for the operator UI. Holds the
    same store the write side holds; issues SELECTs only."""

    def __init__(self, store: PostgresFactStore,
                 serving_metrics_url: Optional[str] = None):
        self._store = store
        self._metrics_url = serving_metrics_url or (
            f"http://{settings.serving_host}:{settings.serving_port}"
            f"/v1/metrics")
        self._started = time.time()
        self._metrics_cache: tuple[float, tuple[str, Optional[float]]] = \
            (0.0, ("down", None))

    # ------------------------------------------------------------- monitor --
    def monitor(self, tenant: str) -> dict[str, Any]:
        with self._store.transaction(tenant) as conn:
            landed = conn.execute(
                "SELECT count(*) AS n FROM raw_documents"
                " WHERE tenant_id = %s AND deleted_at IS NULL",
                (tenant,)).fetchone()["n"]
            facts = conn.execute(
                """
                SELECT count(*) AS current,
                       count(*) FILTER (WHERE f.oversized
                           OR pf.needs_review
                           OR pf.grounding = ANY(%s::text[])) AS low
                FROM facts f
                LEFT JOIN pending_facts pf
                  ON pf.promoted_fact_id = f.id AND pf.tenant_id = f.tenant_id
                WHERE f.tenant_id = %s AND f.valid_to IS NULL
                """, (list(_FLAGGED), tenant)).fetchone()
            processed = conn.execute(
                "SELECT count(*) AS docs,"
                " count(*) FILTER (WHERE review_status = 'review') AS flagged"
                " FROM documents WHERE tenant_id = %s AND valid_to IS NULL",
                (tenant,)).fetchone()
            chunks = conn.execute(
                "SELECT count(*) AS embedded FROM chunks"
                " WHERE tenant_id = %s AND level = 'child'"
                "   AND embedding IS NOT NULL", (tenant,)).fetchone()
            dispatch = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM dispatch_queue"
                " WHERE tenant_id = %s GROUP BY status", (tenant,))}
            extraction_q = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, count(*) AS n FROM extraction_queue"
                " WHERE tenant_id = %s GROUP BY status", (tenant,))}
            runs = conn.execute(
                "SELECT count(*) FILTER (WHERE status = 'ok') AS ok,"
                " COALESCE(sum(facts_staged), 0) AS facts_staged,"
                " COALESCE(sum(quarantined), 0) AS quarantined"
                " FROM extraction_runs WHERE tenant_id = %s",
                (tenant,)).fetchone()
            mentions = conn.execute(
                "SELECT count(*) FILTER (WHERE resolution_status = 'resolved')"
                "   AS resolved,"
                " count(*) FILTER (WHERE resolution_status = 'review')"
                "   AS held,"
                " count(*) FILTER (WHERE resolution_status = 'pending')"
                "   AS pending"
                " FROM entity_mentions WHERE tenant_id = %s",
                (tenant,)).fetchone()
            review = self._review_counts(conn, tenant)
            sources = conn.execute(
                "SELECT source_ref, source_system, status, status_reason,"
                " backfill_done, last_run_at, updated_at FROM source_registry"
                " WHERE tenant_id = %s ORDER BY source_ref",
                (tenant,)).fetchall()
            by_system = {r["source_system"]: r["n"] for r in conn.execute(
                "SELECT source_system, count(*) AS n FROM raw_documents"
                " WHERE tenant_id = %s AND deleted_at IS NULL"
                " GROUP BY source_system", (tenant,))}
            # F5: the REAL failure signal — the operator_alerts view (010):
            # unacknowledged failed queue items + degraded sources. The old
            # badge counted status='error', which no pipeline code ever
            # writes (failures nack back to 'queued' + last_error).
            alerts_open = conn.execute(
                "SELECT count(*) AS n FROM operator_alerts"
                " WHERE tenant_id = %s", (tenant,)).fetchone()["n"]
            series = self._throughput(conn, tenant)

        quarantine_open = review["quarantined"]
        low = facts["low"]
        serving_status, p95_ms = self._serving_metrics()
        return {
            "tenant_id": tenant,
            "landed": landed,
            "facts": facts["current"],
            "facts_confident": facts["current"] - low,
            "facts_low_confidence": low,
            "stages": {
                "capture": {
                    "count": landed,
                    "in_flight": dispatch.get("queued", 0)
                    + dispatch.get("inflight", 0),
                },
                "process": {
                    "count": processed["docs"],
                    "queue_depth": dispatch.get("queued", 0),
                    "chunks_embedded": chunks["embedded"],
                    "errors": dispatch.get("error", 0),
                },
                "extract": {
                    "count": runs["ok"],
                    "facts_staged": runs["facts_staged"],
                    "quarantined": quarantine_open,
                    "queue_depth": extraction_q.get("queued", 0),
                    "errors": extraction_q.get("error", 0),
                },
                "resolve": {
                    "count": mentions["resolved"],
                    "held_for_review": mentions["held"],
                    "pending": mentions["pending"],
                },
                "facts": {
                    "count": facts["current"],
                    "confident": facts["current"] - low,
                    "low_confidence": low,
                },
            },
            "review": review,
            "alerts_open": alerts_open,
            "sources": [{
                "source_ref": s["source_ref"],
                "source_system": s["source_system"],
                "status": s["status"],
                "status_reason": s["status_reason"],
                "backfill_done": s["backfill_done"],
                "landed": by_system.get(s["source_system"], 0),
                "total": None,      # unknown until adapters report corpus size
                "last_run_at": _iso(s["last_run_at"]),
            } for s in sources],
            "throughput": series,
            "serving_status": serving_status,
            "p95_ms": p95_ms,
            "p95_budget_ms": 300,
            "uptime_s": int(time.time() - self._started),
        }

    @staticmethod
    def _review_counts(conn, tenant: str) -> dict[str, int]:
        row = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM match_candidates
                WHERE tenant_id = %(t)s AND decision = 'review') AS merges,
              (SELECT count(*) FROM quarantined_extractions
                WHERE tenant_id = %(t)s AND status = 'open') AS quarantined,
              (SELECT count(*) FROM documents
                WHERE tenant_id = %(t)s
                  AND review_status = 'review') AS flagged
            """, {"t": tenant}).fetchone()
        counts = dict(row)
        counts["total"] = sum(counts.values())
        return counts

    @staticmethod
    def _throughput(conn, tenant: str) -> dict[str, Any]:
        """Docs landed per minute over the last window — the sparkline the
        mock fakes with sines, sourced from raw_documents.ingested_at."""
        rows = conn.execute(
            "SELECT date_trunc('minute', ingested_at) AS m, count(*) AS n"
            " FROM raw_documents WHERE tenant_id = %s"
            "  AND ingested_at > now() - %s::interval"
            " GROUP BY m", (tenant, f"{_THROUGHPUT_WINDOW_MIN} minutes"),
        ).fetchall()
        # Normalize to UTC minutes: date_trunc answers in the session
        # timezone and the lookup below is UTC-keyed.
        by_minute = {r["m"].astimezone(timezone.utc).replace(tzinfo=None):
                     r["n"] for r in rows}
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0,
                                                 tzinfo=None)
        series = [by_minute.get(now - timedelta(minutes=i), 0)
                  for i in range(_THROUGHPUT_WINDOW_MIN - 1, -1, -1)]
        return {"window_min": _THROUGHPUT_WINDOW_MIN, "series": series,
                "per_min": series[-1]}

    def _serving_metrics(self) -> tuple[str, Optional[float]]:
        """The read service's §4 number plus an HONEST component status,
        proxied server-side so the UI stays single-origin. Best effort with
        a short cache — the monitor must render even when the serving
        process is down.

        BP28 #22: 'answers its metrics endpoint but has served no traffic
        yet' is `warming` — a healthy-but-quiet box, NOT a failure; only an
        unreachable serving process is `down`. The health tile renders the
        distinction instead of failing the deploy for having no p95 sample.
        """
        cached_at, value = self._metrics_cache
        if time.time() - cached_at < _SERVING_METRICS_TTL_S:
            return value
        result: tuple[str, Optional[float]]
        try:
            with urllib.request.urlopen(self._metrics_url,
                                        timeout=1.5) as r:
                endpoints = json.load(r).get("endpoints", {})
            if "retrieve" in endpoints:
                result = ("ok", endpoints["retrieve"]["p95_ms"])
            elif endpoints:
                result = ("ok", max(e["p95_ms"] for e in endpoints.values()))
            else:
                result = ("warming", None)
        except Exception:
            result = ("down", None)
        self._metrics_cache = (time.time(), result)
        return result

    # ------------------------------------------------------------ activity --
    def activity(self, tenant: str,
                 limit: int = _ACTIVITY_LIMIT) -> list[dict[str, Any]]:
        """Recent pipeline events, newest first, in the mock's copy voice
        (`stage · detail`) — sourced from real state transitions, never
        synthesized."""
        events: list[tuple[datetime, str]] = []
        with self._store.transaction(tenant) as conn:
            for r in conn.execute(
                    "SELECT raw_document_id, status, last_error, created_at,"
                    " acked_at FROM dispatch_queue WHERE tenant_id = %s"
                    " ORDER BY GREATEST(created_at,"
                    " COALESCE(acked_at, created_at)) DESC LIMIT %s",
                    (tenant, limit)):
                if r["status"] == "done":
                    events.append((r["acked_at"] or r["created_at"],
                                   f"process  · raw doc {r['raw_document_id']}"
                                   f" parsed · chunked · embedded"))
                elif r["status"] == "error" or r["last_error"]:
                    detail = (r["last_error"] or "failed")[:90]
                    events.append((r["acked_at"] or r["created_at"],
                                   f"process  · raw doc {r['raw_document_id']}"
                                   f" failed: {detail}"))
                else:
                    events.append((r["created_at"],
                                   f"capture  · raw doc {r['raw_document_id']}"
                                   f" landed → dispatched for processing"))
            for r in conn.execute(
                    "SELECT facts_staged, mentions_staged, quarantined,"
                    " strategy, status, created_at FROM extraction_runs"
                    " WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s",
                    (tenant, limit)):
                strategy = _STRATEGY_PLAIN.get(r["strategy"], r["strategy"])
                if r["status"] == "ok":
                    events.append((r["created_at"],
                                   f"extract  · {r['facts_staged']} facts +"
                                   f" {r['mentions_staged']} mentions staged"
                                   f" · {r['quarantined']} quarantined"
                                   f" ({strategy})"))
                else:
                    events.append((r["created_at"],
                                   f"extract  · run failed ({strategy})"))
            for r in conn.execute(
                    "SELECT mention_id, decision, tier, score, created_at"
                    " FROM resolution_decisions WHERE tenant_id = %s"
                    " ORDER BY created_at DESC LIMIT %s", (tenant, limit)):
                score = f" · {r['score']:.2f}" if r["score"] is not None else ""
                events.append((r["created_at"],
                               f"resolve  · mention {r['mention_id']} →"
                               f" {_DECISION_PLAIN.get(r['decision'], r['decision'])}"
                               f" ({_TIER_PLAIN.get(r['tier'], r['tier'])}{score})"))
            for r in conn.execute(
                    "SELECT action, outcome, principal_id, created_at"
                    " FROM operator_audit WHERE tenant_id = %s"
                    " ORDER BY created_at DESC LIMIT %s", (tenant, limit)):
                events.append((r["created_at"],
                               f"operator · {r['action']} {r['outcome']}"
                               f" by {r['principal_id']}"))

        events.sort(key=lambda e: e[0], reverse=True)
        return [{"time": _hms(ts), "at": _iso(ts), "text": text}
                for ts, text in events[:limit]]

    # ------------------------------------------------------------- reviews --
    def reviews(self, tenant: str) -> dict[str, Any]:
        with self._store.transaction(tenant) as conn:
            counts = self._review_counts(conn, tenant)
            merges = conn.execute(
                """
                SELECT mc.id, mc.left_type, mc.match_score, mc.band,
                       mc.created_at,
                       CASE WHEN mc.left_type = 'mention'
                            THEN m.surface_text
                            ELSE le.canonical_name END AS left_name,
                       re.canonical_name AS right_name
                FROM match_candidates mc
                LEFT JOIN entity_mentions m
                  ON mc.left_type = 'mention' AND m.id = mc.left_id
                 AND m.tenant_id = mc.tenant_id
                LEFT JOIN entities le
                  ON mc.left_type = 'entity' AND le.id = mc.left_id
                 AND le.tenant_id = mc.tenant_id
                LEFT JOIN entities re
                  ON re.id = mc.right_id AND re.tenant_id = mc.tenant_id
                WHERE mc.tenant_id = %s AND mc.decision = 'review'
                ORDER BY mc.match_score ASC, mc.id LIMIT 200
                """, (tenant,)).fetchall()
            quarantined = conn.execute(
                "SELECT id, reason, detail, created_at"
                " FROM quarantined_extractions"
                " WHERE tenant_id = %s AND status = 'open'"
                " ORDER BY created_at DESC LIMIT 100", (tenant,)).fetchall()
            flagged = conn.execute(
                "SELECT id, title, review_reason, ingested_at FROM documents"
                " WHERE tenant_id = %s AND review_status = 'review'"
                " ORDER BY ingested_at DESC LIMIT 100", (tenant,)).fetchall()

        # Stage 4: listing subtitles speak plain language (the queue column
        # is where a non-technical operator first meets these) — the raw
        # codes stay on the detail payload for anyone who needs them.
        items: list[dict[str, Any]] = []
        for r in merges:
            band = " · in the undecided band" if r["band"] == "gray" else (
                f" · {r['band']} band" if r["band"] else "")
            items.append({
                "id": f"match:{r['id']}", "kind": "merge",
                "title": f"{r['left_name'] or '?'} / {r['right_name'] or '?'}",
                "subtitle": f"merge · score {r['match_score']:.2f}{band}",
                "score": r["match_score"], "at": _iso(r["created_at"])})
        for r in quarantined:
            plain = _QUARANTINE_PLAIN.get(r["reason"], r["reason"])
            items.append({
                "id": f"quarantine:{r['id']}", "kind": "quarantine",
                "title": r["detail"] or plain,
                "subtitle": f"quarantine · {plain}",
                "score": None, "at": _iso(r["created_at"])})
        for r in flagged:
            items.append({
                "id": f"document:{r['id']}", "kind": "flagged",
                "title": r["title"] or f"document {r['id']}",
                "subtitle": f"flagged · {_flag_plain(r['review_reason'])}",
                "score": None, "at": _iso(r["ingested_at"])})
        return {"tenant_id": tenant, "counts": counts, "items": items}

    def review_detail(self, tenant: str, kind: str,
                      ref_id: int) -> Optional[dict[str, Any]]:
        if kind == "match":
            return self._match_detail(tenant, ref_id)
        if kind == "quarantine":
            return self._quarantine_detail(tenant, ref_id)
        if kind == "document":
            return self._document_detail(tenant, ref_id)
        return None

    # --------------------------------------------------------- detail bodies
    def _match_detail(self, tenant: str,
                      ref_id: int) -> Optional[dict[str, Any]]:
        with self._store.transaction(tenant) as conn:
            mc = conn.execute(
                "SELECT * FROM match_candidates"
                " WHERE tenant_id = %s AND id = %s",
                (tenant, ref_id)).fetchone()
            if mc is None:
                return None
            if mc["left_type"] == "mention":
                left = self._mention_card(conn, tenant, mc["left_id"])
            else:
                left = self._entity_card(conn, tenant, mc["left_id"])
            right = self._entity_card(conn, tenant, mc["right_id"])
            entity_type = (right or left or {}).get("entity_type")
            policy = conn.execute(
                "SELECT t_high, t_low, requires_corroboration"
                " FROM resolution_policy WHERE entity_type = %s",
                (entity_type,)).fetchone() if entity_type else None
            passage = None
            if mc["left_type"] == "mention" and left and left.get("chunk_id"):
                passage = self._passage(conn, tenant, left["chunk_id"],
                                        left.get("name"))

        features = mc["features"] or {}
        ev_for, ev_against = _evidence(features)
        return {
            "id": f"match:{ref_id}", "kind": "merge",
            "decision": mc["decision"],
            "question": f"Are these the same"
                        f" {(entity_type or 'entity').lower()}?",
            "score": mc["match_score"], "band": mc["band"],
            "method": mc["match_method"],
            "thresholds": {
                "t_high": policy["t_high"] if policy else None,
                "t_low": policy["t_low"] if policy else None,
                "requires_corroboration":
                    policy["requires_corroboration"] if policy else None,
            },
            "candidate_a": right,       # the EXISTING side (merge survivor)
            "candidate_b": left,        # the new mention / duplicate
            "evidence_for": ev_for,
            "evidence_against": ev_against,
            "features": features,
            "passage": passage,
            "actions": {"merge": {"action": "resolve_merge",
                                  "params": {"candidate_id": ref_id,
                                             "same": True}},
                        "keep_separate": {"action": "resolve_merge",
                                          "params": {"candidate_id": ref_id,
                                                     "same": False}}},
        }

    def _entity_card(self, conn, tenant: str,
                     entity_id: int) -> Optional[dict[str, Any]]:
        e = conn.execute(
            "SELECT id, canonical_name, entity_type, attributes, created_at"
            " FROM entities WHERE tenant_id = %s AND id = %s",
            (tenant, entity_id)).fetchone()
        if e is None:
            return {"role": "entity", "id": entity_id, "name": "(absorbed)",
                    "entity_type": None, "identifiers": {}, "aliases": [],
                    "fact_count": 0, "document_count": 0}
        aliases = [r["alias"] for r in conn.execute(
            "SELECT alias FROM entity_aliases"
            " WHERE tenant_id = %s AND entity_id = %s ORDER BY alias",
            (tenant, entity_id))]
        stats = conn.execute(
            "SELECT count(*) AS facts,"
            " count(DISTINCT source_document_id) AS docs FROM facts"
            " WHERE tenant_id = %s AND valid_to IS NULL"
            "   AND (subject_entity_id = %s OR object_entity_id = %s)",
            (tenant, entity_id, entity_id)).fetchone()
        return {
            "role": "entity", "id": e["id"], "name": e["canonical_name"],
            "entity_type": e["entity_type"],
            "identifiers": e["attributes"] or {},
            "aliases": aliases,
            "fact_count": stats["facts"], "document_count": stats["docs"],
            "first_seen": _iso(e["created_at"]),
        }

    def _mention_card(self, conn, tenant: str,
                      mention_id: int) -> Optional[dict[str, Any]]:
        m = conn.execute(
            "SELECT id, surface_text, entity_type, extracted_keys,"
            " source_document_id, source_chunk_id, created_at"
            " FROM entity_mentions WHERE tenant_id = %s AND id = %s",
            (tenant, mention_id)).fetchone()
        if m is None:
            return None
        return {
            "role": "mention", "id": m["id"], "name": m["surface_text"],
            "entity_type": m["entity_type"],
            "identifiers": m["extracted_keys"] or {},
            "aliases": [], "fact_count": None, "document_count": 1,
            "document_id": m["source_document_id"],
            "chunk_id": m["source_chunk_id"],
            "first_seen": _iso(m["created_at"]),
        }

    def passage(self, tenant: str,
                chunk_id: int) -> Optional[dict[str, Any]]:
        """F18: fact → evidence. Dereference a served fact's chunk_id to
        its passage + document title — THE trust question ("where did this
        come from?") answered through a proper door instead of psql. Role
        + tenant scoping is enforced by the operator HTTP layer, exactly
        like the other operator reads; this is a read-only SELECT."""
        with self._store.transaction(tenant) as conn:
            return self._passage(conn, tenant, chunk_id, None)

    @staticmethod
    def _passage(conn, tenant: str, chunk_id: int,
                 highlight: Optional[str]) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT c.content, c.seq, c.document_id, d.title"
            " FROM chunks c JOIN documents d ON d.id = c.document_id"
            " WHERE c.tenant_id = %s AND c.id = %s",
            (tenant, chunk_id)).fetchone()
        if row is None:
            return None
        return {"text": row["content"], "highlight": highlight,
                "document_id": row["document_id"],
                "document_title": row["title"],
                "chunk_id": chunk_id, "chunk_seq": row["seq"]}

    def _quarantine_detail(self, tenant: str,
                           ref_id: int) -> Optional[dict[str, Any]]:
        with self._store.transaction(tenant) as conn:
            q = conn.execute(
                "SELECT * FROM quarantined_extractions"
                " WHERE tenant_id = %s AND id = %s",
                (tenant, ref_id)).fetchone()
            if q is None:
                return None
            passage = (self._passage(conn, tenant, q["source_chunk_id"], None)
                       if q["source_chunk_id"] else None)
        raw = json.dumps(q["raw_output"])[:2000] if q["raw_output"] else None
        return {
            "id": f"quarantine:{ref_id}", "kind": "quarantine",
            "status": q["status"], "reason": q["reason"],
            "detail": q["detail"], "raw_output": raw,
            "extractor": f"{q['extractor']}@{q['extractor_version']}",
            "ontology_version": q["ontology_version"],
            "passage": passage,
            "actions": {"resolve": {"action": "triage_quarantine",
                                    "params": {"quarantine_id": ref_id,
                                               "decision": "resolved"}},
                        "dismiss": {"action": "triage_quarantine",
                                    "params": {"quarantine_id": ref_id,
                                               "decision": "dismissed"}}},
        }

    def _document_detail(self, tenant: str,
                         ref_id: int) -> Optional[dict[str, Any]]:
        with self._store.transaction(tenant) as conn:
            d = conn.execute(
                "SELECT d.id, d.title, d.doc_type, d.review_status,"
                " d.review_reason, d.raw_document_id,"
                " r.native_metadata ->> 'data_track' AS declared_track"
                " FROM documents d"
                " LEFT JOIN raw_documents r ON r.id = d.raw_document_id"
                " WHERE d.tenant_id = %s AND d.id = %s",
                (tenant, ref_id)).fetchone()
        if d is None:
            return None
        return {
            "id": f"document:{ref_id}", "kind": "flagged",
            "title": d["title"], "doc_type": d["doc_type"],
            "review_status": d["review_status"],
            "review_reason": d["review_reason"],
            "declared_data_track": d["declared_track"],
            "raw_document_id": d["raw_document_id"],
            "actions": {"resolve": {"action": "resolve_flagged_document",
                                    "params": {"document_id": ref_id}}},
        }


def _evidence(features: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Deterministic copy over the resolver's RECORDED features — the
    evidence panels never invent anything; a feature that wasn't recorded
    is not mentioned."""
    ev_for: list[str] = []
    ev_against: list[str] = []
    name_sim = features.get("name_sim")
    if isinstance(name_sim, (int, float)):
        if name_sim >= 0.75:
            ev_for.append(f"Names agree after normalization"
                          f" (difflib {name_sim:.2f})")
        elif name_sim < 0.5:
            ev_against.append(f"Names disagree (difflib {name_sim:.2f})")
    cosine = features.get("cosine")
    if isinstance(cosine, (int, float)):
        if cosine >= 0.8:
            ev_for.append(f"Context embeddings are close"
                          f" (cosine {cosine:.2f})")
        elif cosine < 0.6:
            ev_against.append(f"Context embeddings are far apart"
                              f" (cosine {cosine:.2f})")
    if features.get("key_overlap"):
        ev_for.append("A strong identifier overlaps (exact key match)")
    else:
        ev_against.append("No identifier overlap")
    corroboration = features.get("corroboration")
    if isinstance(corroboration, (int, float)) and corroboration > 0:
        ev_for.append(f"{int(corroboration)} shared graph"
                      f" neighbour(s) corroborate")
    elif corroboration == 0:
        ev_against.append("No corroborating graph edge")
    adj = features.get("adjudication")
    if isinstance(adj, dict):
        verdict = adj.get("same_entity")
        conf = adj.get("confidence")
        conf_s = f" {conf:.2f}" if isinstance(conf, (int, float)) else ""
        if verdict is True:
            ev_for.append(f"Adjudicator returned same_entity = true,"
                          f"{conf_s}")
        elif verdict is False:
            ev_against.append(f"Adjudicator returned same_entity = false,"
                              f"{conf_s}")
    return ev_for, ev_against
