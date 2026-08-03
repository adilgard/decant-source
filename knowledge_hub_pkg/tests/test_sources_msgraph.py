"""MsGraphFilesAdapter unit tests over a scripted HTTP double — the
deterministic halves of the connector: token mint/refresh, throttle
discipline, delta paging + composite cursor resume, @removed -> tombstone
mapping, ACL normalization (groups by-reference), skip counters, and
410 -> CursorInvalid. No network, no stack. The live-tenant half of
validation (admin consent, real throttling, real ACL edge cases) cannot be
faked — its runbook is CONNECTOR_NOTES.md.
"""
from __future__ import annotations

import json

import pytest

from knowledge_hub.interfaces import (
    CursorInvalid,
    OutboundRequest,
    SecretsError,
    SecretsProvider,
    SourceItem,
)
from knowledge_hub.sources_msgraph import (
    GRAPH,
    LOGIN,
    GraphAuth,
    MsGraphFilesAdapter,
    ThrottledTransport,
)

TENANT = "t-graph"
DIR_ID = "dir-123"
TOKEN_URL = f"{LOGIN}/{DIR_ID}/oauth2/v2.0/token"
SITE_URL = f"{GRAPH}/sites/site-a"
DRIVES_URL = f"{GRAPH}/sites/site-a/drives?$select=id,name,driveType,webUrl"
DELTA_URL = f"{GRAPH}/drives/d1/root/delta"
NEXT_URL = f"{GRAPH}/drives/d1/root/delta?$skiptoken=page2"
DELTA_LINK = f"{GRAPH}/drives/d1/root/delta?token=FRESH"


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b"", headers=None):
        self.status_code = status_code
        self._body = body
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    """url -> queue of responses (consumed in order; the last one repeats).
    An un-routed URL raises KeyError — unexpected calls fail loudly."""

    def __init__(self):
        self.routes: dict[str, list[FakeResponse]] = {}
        self.calls: list[tuple[str, str, dict | None]] = []

    def add(self, url, *responses):
        self.routes[url] = list(responses)
        return self

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, headers))
        return self._pop(url)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, dict(data or {})))
        return self._pop(url)

    def _pop(self, url):
        queue = self.routes[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeSecrets(SecretsProvider):
    def __init__(self, fields):
        self.fields = fields

    def inject_credential(self, tenant_id, source_ref, request):
        for key, value in self.fields.items():
            request.attach_secret(key, value)

    def get_secret(self, tenant_id, source_ref):
        return self.fields


def token_resp(token="tok-1", expires_in=3600):
    return FakeResponse(200, {"access_token": token, "expires_in": expires_in})


def file_entry(fid, name, size=10):
    return {
        "id": fid, "name": name, "size": size,
        "file": {"mimeType": "text/plain",
                 "hashes": {"quickXorHash": "qx=="}},
        "lastModifiedDateTime": "2026-07-20T10:00:00Z",
        "createdDateTime": "2026-07-01T00:00:00Z",
        "createdBy": {"user": {"id": "u-creator", "displayName": "Creator"}},
        "parentReference": {"path": "/drives/d1/root:/folder"},
        "webUrl": f"https://contoso/{name}", "eTag": "e1", "cTag": "c1",
    }


PERMISSIONS = {"value": [
    {"id": "p1", "roles": ["write"],
     "grantedToV2": {"user": {"id": "u1", "displayName": "Uma User"}}},
    {"id": "p2", "roles": ["read"], "inheritedFrom": {"id": "parent-item"},
     "grantedToV2": {"group": {"id": "g1", "displayName": "Engineering"}}},
    {"id": "p3", "roles": [],
     "link": {"scope": "anonymous", "type": "view", "preventsDownload": True},
     "expirationDateTime": "2026-08-01T00:00:00Z",
     "grantedToIdentitiesV2": [{"user": {"id": "u2", "displayName": "Redeemed"}}]},
]}


def base_session():
    """Token + site/drive discovery for one site with one drive."""
    return (FakeSession()
            .add(TOKEN_URL, token_resp())
            .add(SITE_URL, FakeResponse(200, {"id": "site-a",
                                              "webUrl": "https://contoso/sites/a",
                                              "name": "a"}))
            .add(DRIVES_URL, FakeResponse(200, {"value": [
                {"id": "d1", "name": "Documents",
                 "driveType": "documentLibrary",
                 "webUrl": "https://contoso/sites/a/Shared"}]})))


def make_adapter(session, **kwargs):
    adapter = MsGraphFilesAdapter("msgraph-files", sites=["site-a"],
                                  session=session, **kwargs)
    adapter.prepare(TENANT, FakeSecrets({"directory_id": DIR_ID,
                                         "client_id": "app-id",
                                         "client_secret": "s3cret"}))
    return adapter


def make_auth(session):
    request = OutboundRequest()
    for key, value in (("directory_id", DIR_ID), ("client_id", "app-id"),
                       ("client_secret", "s3cret")):
        request.attach_secret(key, value)
    return GraphAuth(request, session, TENANT, "msgraph-files")


# ---------------------------------------------------------------- sweeping --
def full_sweep_session():
    return (base_session()
            .add(DELTA_URL, FakeResponse(200, {
                "value": [{"id": "folder1", "folder": {"childCount": 2}},
                          file_entry("f1", "one.txt")],
                "@odata.nextLink": NEXT_URL}))
            .add(NEXT_URL, FakeResponse(200, {
                "value": [file_entry("f2", "two.txt"),
                          {"id": "r1", "@removed": {"reason": "deleted"}}],
                "@odata.deltaLink": DELTA_LINK}))
            .add(f"{GRAPH}/drives/d1/items/f1/content",
                 FakeResponse(200, content=b"file one bytes"))
            .add(f"{GRAPH}/drives/d1/items/f2/content",
                 FakeResponse(200, content=b"file two bytes"))
            .add(f"{GRAPH}/drives/d1/items/f1/permissions",
                 FakeResponse(200, PERMISSIONS))
            .add(f"{GRAPH}/drives/d1/items/f2/permissions",
                 FakeResponse(200, {"value": []})))


def test_backfill_sweeps_pages_maps_items_and_tombstones():
    adapter = make_adapter(full_sweep_session())
    items = list(adapter.backfill(TENANT))

    # Folder skipped; two files + one explicit removal survived.
    assert [(i.native_id, i.change) for i in items] == [
        ("drives/d1/items/f1", "upsert"),
        ("drives/d1/items/f2", "upsert"),
        ("drives/d1/items/r1", "tombstone"),
    ]
    f1 = items[0]
    assert f1.content == b"file one bytes" and f1.size == 14
    assert f1.mime_type == "text/plain"
    assert f1.mtime.isoformat().startswith("2026-07-20T10:00")
    assert f1.native_metadata["drive_id"] == "d1"
    assert f1.native_metadata["site_id"] == "site-a"
    assert f1.native_metadata["quickxor_hash"] == "qx=="

    # Every item's cursor resumes its OWN page (at-least-once, page replay).
    first_page = json.loads(f1.cursor)
    assert first_page["cur"] == {"drive": "d1", "link": DELTA_URL}
    second_page = json.loads(items[2].cursor)
    assert second_page["cur"] == {"drive": "d1", "link": NEXT_URL}

    # End of sweep: the fresh deltaLink is the incremental high-water mark.
    final = json.loads(adapter.final_cursor())
    assert final == {"v": 1, "done": {"d1": DELTA_LINK}, "cur": None}


def test_resume_replays_only_the_inflight_page():
    # Cursor checkpointed mid-sweep on page 2: page 1 must NOT be re-fetched
    # (its URL is deliberately un-routed and would raise KeyError).
    session = (base_session()
               .add(NEXT_URL, FakeResponse(200, {
                   "value": [file_entry("f2", "two.txt")],
                   "@odata.deltaLink": DELTA_LINK}))
               .add(f"{GRAPH}/drives/d1/items/f2/content",
                    FakeResponse(200, content=b"file two bytes"))
               .add(f"{GRAPH}/drives/d1/items/f2/permissions",
                    FakeResponse(200, {"value": []})))
    adapter = make_adapter(session)
    checkpoint = json.dumps(
        {"v": 1, "done": {}, "cur": {"drive": "d1", "link": NEXT_URL}})

    items = list(adapter.incremental(TENANT, checkpoint))

    assert [i.native_id for i in items] == ["drives/d1/items/f2"]
    assert json.loads(adapter.final_cursor())["done"] == {"d1": DELTA_LINK}


def test_new_drive_backfilled_vanished_drive_dropped_not_tombstoned():
    old_d1 = f"{GRAPH}/drives/d1/root/delta?token=OLD"
    d2_scratch = f"{GRAPH}/drives/d2/root/delta"
    session = (base_session()
               .add(DRIVES_URL, FakeResponse(200, {"value": [
                   {"id": "d1", "name": "Documents",
                    "driveType": "documentLibrary"},
                   {"id": "d2", "name": "NewLibrary",
                    "driveType": "documentLibrary"}]}))
               .add(old_d1, FakeResponse(200, {
                   "value": [], "@odata.deltaLink": DELTA_LINK}))
               .add(d2_scratch, FakeResponse(200, {
                   "value": [], "@odata.deltaLink": d2_scratch + "?token=D2"})))
    adapter = make_adapter(session)
    checkpoint = json.dumps({"v": 1, "cur": None,
                             "done": {"d1": old_d1,
                                      "d-gone": "https://old/link"}})

    items = list(adapter.incremental(TENANT, checkpoint))

    assert items == []  # the vanished drive produced NO tombstones (§8.1g)
    final = json.loads(adapter.final_cursor())
    assert set(final["done"]) == {"d1", "d2"}  # d-gone dropped, d2 enumerated


# --------------------------------------------------------------- failures --
def test_delta_410_raises_cursor_invalid():
    expired = f"{GRAPH}/drives/d1/root/delta?token=EXPIRED"
    session = base_session().add(expired, FakeResponse(410))
    adapter = make_adapter(session)
    checkpoint = json.dumps({"v": 1, "done": {"d1": expired}, "cur": None})

    with pytest.raises(CursorInvalid, match="410"):
        list(adapter.incremental(TENANT, checkpoint))


def test_unparseable_cursor_raises_cursor_invalid():
    adapter = make_adapter(base_session())
    with pytest.raises(CursorInvalid, match="unparseable"):
        list(adapter.incremental(TENANT, "not json at all"))


def test_oversized_deferred_and_unreadable_skipped():
    session = (base_session()
               .add(DELTA_URL, FakeResponse(200, {
                   "value": [file_entry("fbig", "huge.bin", size=10 ** 9),
                             file_entry("f403", "forbidden.txt"),
                             file_entry("f2", "two.txt")],
                   "@odata.deltaLink": DELTA_LINK}))
               .add(f"{GRAPH}/drives/d1/items/f403/content",
                    FakeResponse(403))
               .add(f"{GRAPH}/drives/d1/items/f2/content",
                    FakeResponse(200, content=b"file two bytes"))
               .add(f"{GRAPH}/drives/d1/items/f2/permissions",
                    FakeResponse(200, {"value": []})))
    adapter = make_adapter(session, max_content_bytes=1000)

    items = list(adapter.backfill(TENANT))

    # The pull neither wedged nor lied: one item landed, two skips counted.
    assert [i.native_id for i in items] == ["drives/d1/items/f2"]
    assert adapter.skipped_oversized == ["drives/d1/items/fbig"]
    assert adapter.skipped_unreadable == ["drives/d1/items/f403"]
    assert adapter.final_cursor() is not None
    stats = adapter.stats()
    assert stats["skipped_oversized"] == ["drives/d1/items/fbig"]


def test_missing_credential_fields_raise_secrets_error():
    adapter = MsGraphFilesAdapter("msgraph-files", sites=["site-a"],
                                  session=FakeSession())
    with pytest.raises(SecretsError, match="client_secret"):
        adapter.prepare(TENANT, FakeSecrets({"directory_id": DIR_ID,
                                             "client_id": "app-id"}))


# -------------------------------------------------------------------- ACL --
def test_acl_normalized_groups_by_reference():
    adapter = make_adapter(full_sweep_session())
    acl = list(adapter.backfill(TENANT))[0].source_acl

    assert acl.model == "msgraph.driveItem.v1"
    assert acl.owner == "u-creator"
    by_principal = {g.principal_id: g for g in acl.grants}

    direct = by_principal["u1"]
    assert (direct.principal_type, direct.roles, direct.via) == \
        ("user", ["write"], "direct")

    # The load-bearing one: a group grant is the group's id, BY REFERENCE —
    # no member list anywhere in the normalized ACL.
    group = by_principal["g1"]
    assert (group.principal_type, group.roles, group.via) == \
        ("group", ["read"], "inherited")
    assert group.detail["inherited_from"] == "parent-item"

    # Anonymous sharing link: an 'anyone' grant carrying scope/type/expiry,
    # link type 'view' normalized to role 'read'.
    link = by_principal["p3"]
    assert (link.principal_type, link.roles, link.via) == \
        ("anyone", ["read"], "link")
    assert link.detail["scope"] == "anonymous"
    assert link.detail["expires"] == "2026-08-01T00:00:00Z"

    # The already-redeemed identity behind the link is captured too.
    redeemed = by_principal["u2"]
    assert (redeemed.principal_type, redeemed.via) == ("user", "link")

    # Faithful payload preserved for arbitration.
    assert acl.raw["permissions"] == PERMISSIONS["value"]


def test_acl_fetch_failure_recorded_honestly():
    session = (base_session()
               .add(DELTA_URL, FakeResponse(200, {
                   "value": [file_entry("f1", "one.txt")],
                   "@odata.deltaLink": DELTA_LINK}))
               .add(f"{GRAPH}/drives/d1/items/f1/content",
                    FakeResponse(200, content=b"file one bytes"))
               .add(f"{GRAPH}/drives/d1/items/f1/permissions",
                    FakeResponse(403)))
    adapter = make_adapter(session)

    acl = list(adapter.backfill(TENANT))[0].source_acl

    assert acl.grants == []
    assert "HTTP 403" in acl.raw["error"]


# -------------------------------------------------------- auth + throttle --
def test_throttle_honors_retry_after_then_succeeds():
    url = f"{GRAPH}/anything"
    session = (FakeSession()
               .add(TOKEN_URL, token_resp())
               .add(url, FakeResponse(429, headers={"Retry-After": "3"}),
                    FakeResponse(200, {"ok": True})))
    sleeps: list[float] = []
    transport = ThrottledTransport(make_auth(session), TENANT, "msgraph-files",
                                   session=session, sleep=sleeps.append)

    resp = transport.get(url)

    assert resp.status_code == 200
    assert sleeps == [3.0]
    assert transport.throttled == 1


def test_mid_pull_401_refreshes_token_exactly_once():
    url = f"{GRAPH}/anything"
    session = (FakeSession()
               .add(TOKEN_URL, token_resp("tok-1"), token_resp("tok-2"))
               .add(url, FakeResponse(401), FakeResponse(200, {"ok": True})))
    transport = ThrottledTransport(make_auth(session), TENANT, "msgraph-files",
                                   session=session, sleep=lambda s: None)

    resp = transport.get(url)

    assert resp.status_code == 200
    last_get = [c for c in session.calls if c[0] == "GET"][-1]
    assert last_get[2]["Authorization"] == "Bearer tok-2"

    # A second 401 in the same call means the credential is dead: degrade,
    # don't loop.
    session2 = (FakeSession()
                .add(TOKEN_URL, token_resp("tok-1"), token_resp("tok-2"))
                .add(url, FakeResponse(401)))
    transport2 = ThrottledTransport(make_auth(session2), TENANT,
                                    "msgraph-files", session=session2,
                                    sleep=lambda s: None)
    with pytest.raises(SecretsError, match="401"):
        transport2.get(url)


def test_token_cached_until_expiry_and_never_in_stats():
    session = full_sweep_session()
    adapter = make_adapter(session)
    list(adapter.backfill(TENANT))

    mints = [c for c in session.calls if c[0] == "POST"]
    assert len(mints) == 1  # one mint served the whole sweep

    stats = json.dumps(adapter.stats())
    assert "s3cret" not in stats and "tok-" not in stats
