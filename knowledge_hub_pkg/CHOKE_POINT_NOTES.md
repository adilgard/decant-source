# Choke point + server-side identity — Build Prompt S2 notes

`knowledge_hub/choke_point.py` implements the S1 `ChokePoint` seam: the
ENFORCEMENT boundary of the serving layer. Every query that hits Postgres on
the serve path — base op, composite step, retrieval, traversal — transits
`enforce()` and executes only through `read()`, unconditionally.

## The boundary (internal vs external)

| | Internal pipeline | External serve/read path |
|---|---|---|
| Who | FactStore writes, resolver reads, extraction, capture | S3 ops, S4 retrieval, S5 API — anything answering a caller |
| Trust | Trusted infrastructure; processes ALL tenants by design | Untrusted callers; sees exactly one tenant's permitted slice |
| Door to Postgres | `PostgresFactStore` | `PostgresChokePoint.read()` — the ONLY one |
| Transits the choke point | **No** | **Yes, always** |

Postgres connection auth is untouched — no new DB roles, no RLS. Enforcement
is application-layer filtering above the single existing app connection. The
serving connection is name-mangled private inside `PostgresChokePoint`, is
opened with `default_transaction_read_only = on`, and has no accessor: S3/S4
hold the choke point, never a connection.

## Trust chain

```
credential ──OpenBaoCredentialResolver.resolve_principal──▶ Principal
Principal + RetrievalQuery ──PostgresChokePoint.enforce──▶ FilteredQuery
FilteredQuery + SQL template ──PostgresChokePoint.read──▶ rows
```

* **Server-side identity.** The caller presents an opaque per-tenant
  credential. It is hashed (sha256) and resolved against the hub-owned vault
  registry `<mount>/serving/principals/<digest>` (NOT under `tenants/<id>/…`
  — a tenant's own vault policy must never read the registry that says who
  anyone is). The stored record is `{tenant_id, principal_id, roles}`. The
  request says WHAT it wants; the principal determines what it may SEE — a
  caller cannot override its own tenant or labels.
* **enforce(query, principal) → FilteredQuery.** Resolves the principal's
  roles against `label_role_grants` (flat set membership, deny-by-default,
  no hierarchy/inheritance/per-row ACLs), always adds the seeded `public`
  label id, and rebuilds the query from the BASE `RetrievalQuery` fields —
  so tenant/labels asserted on the incoming object (e.g. a forged
  `FilteredQuery`) are discarded, never merged.
* **read(fq, sql, params) → rows.** Requires named params and at least one
  `{sec:<alias>}` marker; refuses non-SELECT/WITH, multi-statement SQL, and
  reserved-param collisions. Markers expand to the mandatory predicates:
  * `{sec:a}` → `(a.tenant_id = :t AND (a.security_label_id IS NULL OR
    a.security_label_id = ANY(:allowed)))` — for label-bearing tables
    (facts, entities, documents, raw_documents, pending_facts).
  * `{tenant:a}` → `a.tenant_id = :t` — for label-less tables (chunks: their
    label lives on the parent document, so evidence reads must JOIN
    documents and `{sec:}` it; a chunks-only template is refused because it
    has no `{sec:}` marker).

## Proof of passage (three layers)

1. **Type**: S4/S3 signatures accept only `FilteredQuery` (S1 guarantee) —
   a bare `RetrievalQuery` at `read()` is a `TypeError`.
2. **Mint check**: `enforce()` stamps each `FilteredQuery` with a
   per-instance sentinel; `read()` refuses hand-built queries and queries
   minted by another choke point (`UnenforcedQuery`).
3. **Tamper check**: the stamp snapshots `(tenant_id, principal_id,
   allowed_label_ids)`; mutating any of them after enforcement is refused,
   and `read()` binds the predicates from the verified snapshot, not the
   live fields. Serialize/deserialize drops the stamp → refused (enforcement
   is in-process, by design).

## Fail closed vs no access (one line, two sides)

* Missing principal, non-Principal object, blank tenant/principal id,
  unresolvable credential (unknown, revoked, malformed record, vault error),
  unreachable grants table → `EnforcementRefused` / `PrincipalUnresolvable`.
  Nothing ever runs unfiltered; no `FilteredQuery` is produced.
* A RESOLVED principal with zero role grants is NOT an error: it gets a
  valid `FilteredQuery` that sees only public/NULL items of its tenant
  (matches the S1 docstring "never raises on 'no access'").

## Permission-invisibility is silent

A filtered-out item is ABSENT from the rows — never surrogated, counted, or
reported as `unknown`. This happens logically BEFORE the S1 uncertainty
states apply (the absence rule): `unknown` means "the hub has no assertion",
never "there is something you can't read".

## Label model notes

* Grants and `allowed_label_ids` are label IDs; a NULL `security_label_id`
  serves as `public` (S1 spine representation). Both NULL and the seeded
  `public` label id are visible to every resolved principal of the tenant.
* Labels/roles are a GLOBAL vocabulary (reference tables carry no tenant);
  tenancy is enforced by the independent `tenant_id` predicate injected
  alongside the label check — the pair is never separable, so a shared role
  name can never cross tenants.

## Graph caveat

AGE `cypher()` cannot take bind parameters, so serve-path traversals go
through SQL over `facts` (joins / recursive CTEs) via the gateway. The AGE
projection stays internal-path until a literal-safe cypher builder is
designed and reviewed.

## Tests

`tests/test_choke_point.py` (real Postgres + real OpenBao, no mocks):
tenant A never sees tenant B across all four op shapes even with identical
names/vectors; missing label L → items absent (exact sets, no placeholder);
public/NULL visible to a zero-grant principal; missing/malformed principal,
unknown/malformed credential, and dead-DB grants all REFUSE; asserted
tenant/labels on the request are ignored; hand-built / foreign-minted /
post-enforce-mutated FilteredQuery all refused; template hygiene (marker
required, read-only, single statement, reserved params); the serving
session itself is read-only at the Postgres level (`WITH … INSERT` fails
with `ReadOnlySqlTransaction`).
