"""QboAdapter unit tests over a scripted HTTP double — the deterministic
halves of the connector: refresh-token mint + persist-before-use rotation
write-back, throttle discipline, query paging + composite cursor resume, CDC
changed/Deleted -> upsert/tombstone mapping, company-scope ACL, and every
CursorInvalid resync trigger (stale since, entity-set change, CDC cap,
garbage state). No network, no stack. The live half (real consent ceremony,
real throttling, sandbox gauntlet) is CONNECTOR_NOTES.md's runbook.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from knowledge_hub.interfaces import (
    CredentialRotator,
    CursorInvalid,
    SecretsError,
    SecretNotFound,
    SecretsProvider,
)
from knowledge_hub.sources_qbo import (
    BASE_URLS,
    MINOR_VERSION,
    OAUTH_TOKEN_URL,
    OVERLAP_SECONDS,
    QboAdapter,
)

TENANT = "t-qbo"
REALM = "9341453889"
BASE = BASE_URLS["sandbox"]


def query_url(entity, pos, page_size):
    from urllib.parse import urlencode
    q = (f"SELECT * FROM {entity} ORDERBY Id"
         f" STARTPOSITION {pos} MAXRESULTS {page_size}")
    return (f"{BASE}/v3/company/{REALM}/query?"
            + urlencode({"query": q, "minorversion": MINOR_VERSION}))


def cdc_url(entities, since):
    from urllib.parse import urlencode
    return (f"{BASE}/v3/company/{REALM}/cdc?"
            + urlencode({"entities": ",".join(entities),
                         "changedSince": since,
                         "minorversion": MINOR_VERSION}))


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeSession:
    """url -> queue of responses (consumed in order; the last one repeats).
    An un-routed URL raises KeyError — unexpected calls fail loudly."""

    def __init__(self):
        self.routes: dict[str, list[FakeResponse]] = {}
        self.calls: list[tuple[str, str]] = []

    def add(self, url, *responses):
        self.routes[url] = list(responses)
        return self

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url))
        return self._pop(url)

    def post(self, url, data=None, headers=None, auth=None, timeout=None):
        self.calls.append(("POST", url))
        return self._pop(url)

    def _pop(self, url):
        queue = self.routes[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeRotatingSecrets(SecretsProvider, CredentialRotator):
    """Read + rotation double: records every rotation, in order, alongside
    an event log shared with nothing (rotation order vs data calls is
    asserted through the session's call list)."""

    def __init__(self, fields):
        self.fields = dict(fields)
        self.rotations: list[dict] = []

    def inject_credential(self, tenant_id, source_ref, request):
        for key, value in self.fields.items():
            request.attach_secret(key, value)

    def get_secret(self, tenant_id, source_ref):
        return self.fields

    def rotate_credential(self, tenant_id, source_ref, updates):
        self.rotations.append({"tenant": tenant_id, "ref": source_ref,
                               **updates})
        self.fields.update(updates)


class PlainSecrets(SecretsProvider):
    """Read-only provider — must be REFUSED by the QBO adapter."""

    def __init__(self, fields):
        self.fields = fields

    def inject_credential(self, tenant_id, source_ref, request):
        for key, value in self.fields.items():
            request.attach_secret(key, value)

    def get_secret(self, tenant_id, source_ref):
        return self.fields


CREDENTIAL = {"client_id": "app-id", "client_secret": "s3cret",
              "refresh_token": "rt-old", "realm_id": REALM,
              "environment": "sandbox"}


def token_resp(token="tok-1", refresh_token="rt-old", expires_in=3600):
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in,
                              "refresh_token": refresh_token,
                              "x_refresh_token_expires_in": 8640000})


def qbo_obj(entity, oid, **fields):
    obj = {"Id": oid, "SyncToken": "0",
           "MetaData": {"CreateTime": "2026-07-01T00:00:00-07:00",
                        "LastUpdatedTime": "2026-08-01T10:00:00-07:00"}}
    obj.update(fields)
    return obj


def recent_iso(minutes_ago=60):
    return (datetime.now(tz=timezone.utc)
            - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


SERVER_TIME = "2026-08-03T09:00:00-07:00"


def make_adapter(session, secrets=None, **kwargs):
    kwargs.setdefault("entities", ["Customer", "Invoice"])
    kwargs.setdefault("page_size", 2)
    adapter = QboAdapter("qbo-books", session=session, **kwargs)
    adapter.prepare(TENANT, secrets if secrets is not None
                    else FakeRotatingSecrets(CREDENTIAL))
    return adapter


# ---------------------------------------------------------------- backfill --
def test_backfill_pages_entities_and_maps_items():
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp())
               .add(query_url("Customer", 1, 2), FakeResponse(200, {
                   "QueryResponse": {"Customer": [
                       qbo_obj("Customer", "c1", DisplayName="Acme Farms",
                               Active=True)]},
                   "time": SERVER_TIME}))
               .add(query_url("Invoice", 1, 2), FakeResponse(200, {
                   "QueryResponse": {"Invoice": [
                       qbo_obj("Invoice", "i1", DocNumber="1001"),
                       qbo_obj("Invoice", "i2", DocNumber="1002")]},
                   "time": SERVER_TIME}))
               .add(query_url("Invoice", 3, 2), FakeResponse(200, {
                   "QueryResponse": {"Invoice": [
                       qbo_obj("Invoice", "i3", DocNumber="1003")]},
                   "time": SERVER_TIME})))
    adapter = make_adapter(session)

    items = list(adapter.backfill(TENANT))

    assert [(i.native_id, i.change) for i in items] == [
        ("Customer/c1", "upsert"), ("Invoice/i1", "upsert"),
        ("Invoice/i2", "upsert"), ("Invoice/i3", "upsert")]
    c1 = items[0]
    # Canonical bytes: identical record -> identical hash across pulls.
    assert json.loads(c1.content)["DisplayName"] == "Acme Farms"
    assert c1.content == json.dumps(json.loads(c1.content), sort_keys=True,
                                    separators=(",", ":")).encode()
    assert c1.mime_type == "application/json"
    assert c1.mtime.isoformat().startswith("2026-08-01")
    # Company-scope ACL, realm by reference.
    assert c1.source_acl.model == "qbo.company.v1"
    assert c1.source_acl.grants[0].principal_type == "domain"
    assert c1.source_acl.grants[0].principal_id == REALM
    # SoR declaration rides native_metadata (§8.1a claim).
    assert c1.native_metadata["data_track"] == "sor"
    assert c1.native_metadata["doc_type"] == "qbo.customer"
    assert c1.native_metadata["display"] == "Acme Farms"

    # Every item's cursor resumes its OWN page.
    assert json.loads(items[1].cursor)["cur"] == {"entity": "Invoice",
                                                  "pos": 1}
    assert json.loads(items[3].cursor)["cur"] == {"entity": "Invoice",
                                                  "pos": 3}

    # End of sweep: since = first response's SERVER time minus the pad.
    final = json.loads(adapter.final_cursor())
    assert final["cur"] is None
    assert final["entities"] == ["Customer", "Invoice"]
    expected_since = (datetime.fromisoformat(SERVER_TIME)
                      - timedelta(seconds=OVERLAP_SECONDS))
    assert datetime.fromisoformat(final["since"]) == expected_since


def test_backfill_resume_skips_completed_entities():
    # Checkpoint mid-Invoice: Customer already swept — its URL is
    # deliberately un-routed and would raise KeyError if touched.
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp())
               .add(query_url("Invoice", 3, 2), FakeResponse(200, {
                   "QueryResponse": {"Invoice": [
                       qbo_obj("Invoice", "i3")]},
                   "time": SERVER_TIME})))
    adapter = make_adapter(session)
    checkpoint = json.dumps({"v": 1, "since": recent_iso(),
                             "entities": ["Customer", "Invoice"],
                             "cur": {"entity": "Invoice", "pos": 3}})

    items = list(adapter.backfill(TENANT, resume_after=checkpoint))

    assert [i.native_id for i in items] == ["Invoice/i3"]
    # The original sweep's since survives the resume (conservative mark).
    assert json.loads(adapter.final_cursor())["since"] == json.loads(
        checkpoint)["since"]


# ------------------------------------------------------------------- CDC ----
def test_incremental_cdc_maps_upserts_and_tombstones():
    since = recent_iso()
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp())
               .add(cdc_url(["Customer", "Invoice"], since),
                    FakeResponse(200, {
                        "CDCResponse": [{"QueryResponse": [
                            {"Customer": [qbo_obj("Customer", "c9",
                                                  DisplayName="Rennamed Co",
                                                  Active=False)],
                             "startPosition": 1, "maxResults": 1},
                            {"Invoice": [
                                {"Id": "i7", "status": "Deleted",
                                 "domain": "QBO",
                                 "MetaData": {"LastUpdatedTime":
                                              "2026-08-02T08:00:00-07:00"}}]},
                        ]}],
                        "time": SERVER_TIME})))
    adapter = make_adapter(session)
    cursor = json.dumps({"v": 1, "since": since,
                         "entities": ["Customer", "Invoice"], "cur": None})

    items = list(adapter.incremental(TENANT, cursor))

    # Deactivation = upsert (state change); Deleted = explicit tombstone.
    assert [(i.native_id, i.change) for i in items] == [
        ("Customer/c9", "upsert"), ("Invoice/i7", "tombstone")]
    tomb = items[1]
    assert tomb.content == b""
    assert tomb.mtime.isoformat().startswith("2026-08-02")
    assert tomb.native_metadata["entity"] == "Invoice"
    # Item cursors keep the OLD since (whole-sweep replay on resume)...
    assert json.loads(items[0].cursor)["since"] == since
    # ...the new mark only lands at end of sweep.
    final = json.loads(adapter.final_cursor())
    expected = (datetime.fromisoformat(SERVER_TIME)
                - timedelta(seconds=OVERLAP_SECONDS))
    assert datetime.fromisoformat(final["since"]) == expected


def test_incremental_with_no_cursor_runs_the_full_query_sweep():
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp())
               .add(query_url("Customer", 1, 2), FakeResponse(200, {
                   "QueryResponse": {"Customer": [qbo_obj("Customer", "c1")]},
                   "time": SERVER_TIME}))
               .add(query_url("Invoice", 1, 2), FakeResponse(200, {
                   "QueryResponse": {}, "time": SERVER_TIME})))
    adapter = make_adapter(session)

    items = list(adapter.incremental(TENANT, None))

    assert [i.native_id for i in items] == ["Customer/c1"]
    assert json.loads(adapter.final_cursor())["since"] is not None


# ------------------------------------------------------- resync triggers ----
def sweep_all(adapter, cursor):
    return list(adapter.incremental(TENANT, cursor))


def test_stale_since_raises_cursor_invalid_before_any_call():
    session = FakeSession().add(OAUTH_TOKEN_URL, token_resp())
    adapter = make_adapter(session)  # CDC URL un-routed: a call would KeyError
    stale = (datetime.now(tz=timezone.utc)
             - timedelta(days=40)).isoformat(timespec="seconds")
    cursor = json.dumps({"v": 1, "since": stale,
                         "entities": ["Customer", "Invoice"], "cur": None})
    with pytest.raises(CursorInvalid, match="lookback"):
        sweep_all(adapter, cursor)


def test_entity_set_change_raises_cursor_invalid():
    session = FakeSession().add(OAUTH_TOKEN_URL, token_resp())
    adapter = make_adapter(session)
    cursor = json.dumps({"v": 1, "since": recent_iso(),
                         "entities": ["Customer"], "cur": None})
    with pytest.raises(CursorInvalid, match="entity set changed"):
        sweep_all(adapter, cursor)


def test_cdc_at_entity_cap_raises_cursor_invalid():
    since = recent_iso()
    rows = [qbo_obj("Customer", f"c{n}") for n in range(2)]  # == page_size
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp())
               .add(cdc_url(["Customer", "Invoice"], since),
                    FakeResponse(200, {
                        "CDCResponse": [{"QueryResponse": [
                            {"Customer": rows}]}],
                        "time": SERVER_TIME})))
    adapter = make_adapter(session)
    cursor = json.dumps({"v": 1, "since": since,
                         "entities": ["Customer", "Invoice"], "cur": None})
    with pytest.raises(CursorInvalid, match="truncation"):
        sweep_all(adapter, cursor)


def test_unparseable_cursor_raises_cursor_invalid():
    adapter = make_adapter(FakeSession().add(OAUTH_TOKEN_URL, token_resp()))
    with pytest.raises(CursorInvalid):
        sweep_all(adapter, "not json at all")
    with pytest.raises(CursorInvalid):
        sweep_all(adapter, json.dumps({"v": 99}))
    with pytest.raises(CursorInvalid, match="changedSince"):
        sweep_all(adapter, json.dumps(
            {"v": 1, "since": "garbage-timestamp",
             "entities": ["Customer", "Invoice"], "cur": None}))


# ---------------------------------------------------------------- auth ------
def test_rotated_refresh_token_persisted_before_first_data_call():
    secrets = FakeRotatingSecrets(CREDENTIAL)
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp(refresh_token="rt-NEW"))
               .add(query_url("Customer", 1, 2), FakeResponse(200, {
                   "QueryResponse": {}, "time": SERVER_TIME}))
               .add(query_url("Invoice", 1, 2), FakeResponse(200, {
                   "QueryResponse": {}, "time": SERVER_TIME})))

    def rotate_and_check(tenant_id, source_ref, updates,
                         _orig=secrets.rotate_credential):
        # Persist-before-use: at rotation time, no data GET has happened yet.
        assert all(m == "POST" for m, _ in session.calls)
        _orig(tenant_id, source_ref, updates)
    secrets.rotate_credential = rotate_and_check

    adapter = make_adapter(session, secrets=secrets)
    list(adapter.backfill(TENANT))

    assert secrets.rotations == [{"tenant": TENANT, "ref": "qbo-books",
                                  "refresh_token": "rt-NEW"}]
    assert secrets.fields["refresh_token"] == "rt-NEW"


def test_unrotated_refresh_token_writes_nothing():
    secrets = FakeRotatingSecrets(CREDENTIAL)
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp(refresh_token="rt-old"))
               .add(query_url("Customer", 1, 2), FakeResponse(200, {
                   "QueryResponse": {}, "time": SERVER_TIME}))
               .add(query_url("Invoice", 1, 2), FakeResponse(200, {
                   "QueryResponse": {}, "time": SERVER_TIME})))
    adapter = make_adapter(session, secrets=secrets)
    list(adapter.backfill(TENANT))
    assert secrets.rotations == []


def test_read_only_provider_refused_at_prepare():
    # No write-back path = guaranteed lockout within ~24h; refuse NOW.
    adapter = QboAdapter("qbo-books", entities=["Customer"],
                         session=FakeSession())
    with pytest.raises(SecretsError, match="CredentialRotator"):
        adapter.prepare(TENANT, PlainSecrets(CREDENTIAL))


def test_missing_credential_fields_raise_secret_not_found():
    partial = {k: v for k, v in CREDENTIAL.items() if k != "realm_id"}
    adapter = QboAdapter("qbo-books", entities=["Customer"],
                         session=FakeSession())
    with pytest.raises(SecretNotFound, match="realm_id"):
        adapter.prepare(TENANT, FakeRotatingSecrets(partial))


def test_invalid_grant_raises_secrets_error_with_reconsent_hint():
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL,
                    FakeResponse(400, {"error": "invalid_grant"})))
    adapter = make_adapter(session)
    with pytest.raises(SecretsError, match="re-consent"):
        list(adapter.backfill(TENANT))


def test_mid_pull_401_reminted_once_then_fatal():
    ok_page = FakeResponse(200, {"QueryResponse": {}, "time": SERVER_TIME})
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp("tok-1"), token_resp("tok-2"))
               .add(query_url("Customer", 1, 2),
                    FakeResponse(401), ok_page)
               .add(query_url("Invoice", 1, 2), ok_page))
    adapter = make_adapter(session)
    assert list(adapter.backfill(TENANT)) == []  # one 401 healed by re-mint

    session2 = (FakeSession()
                .add(OAUTH_TOKEN_URL, token_resp("tok-1"), token_resp("tok-2"))
                .add(query_url("Customer", 1, 2), FakeResponse(401)))
    adapter2 = make_adapter(session2)
    with pytest.raises(SecretsError, match="twice"):
        list(adapter2.backfill(TENANT))


def test_throttling_honored_and_counted():
    ok_page = FakeResponse(200, {"QueryResponse": {}, "time": SERVER_TIME})
    session = (FakeSession()
               .add(OAUTH_TOKEN_URL, token_resp())
               .add(query_url("Customer", 1, 2),
                    FakeResponse(429, headers={"Retry-After": "3"}), ok_page)
               .add(query_url("Invoice", 1, 2), ok_page))
    adapter = make_adapter(session)
    naps: list[float] = []
    adapter._transport._sleep = naps.append

    list(adapter.backfill(TENANT))

    assert naps == [3.0]
    assert adapter.stats()["throttled"] == 1


# ------------------------------------------------------------ tenant guard --
def test_cross_tenant_reuse_is_refused():
    adapter = make_adapter(FakeSession().add(OAUTH_TOKEN_URL, token_resp()))
    with pytest.raises(RuntimeError, match="one adapter instance"):
        adapter.backfill("some-other-tenant")
