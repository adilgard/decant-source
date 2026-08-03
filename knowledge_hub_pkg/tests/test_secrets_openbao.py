"""SecretsProvider against real OpenBao (dev mode): the injection seam never
exposes raw values, and a missing/denied credential degrades ONE source in
the registry while the tenant's other sources keep landing."""
from __future__ import annotations

import logging
from typing import Iterator, Optional

import hvac
import pytest

from knowledge_hub.config import settings
from knowledge_hub.interfaces import (
    OutboundRequest,
    SecretAccessDenied,
    SecretNotFound,
    SecretsProvider,
    SourceAdapter,
    SourceItem,
)
from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider

SENTINEL = "s3kr1t-hunter2-do-not-leak"


def test_inject_attaches_without_returning(secrets, tenant):
    secrets.put_secret(tenant, "sftp-hr", {"username": "svc-hr",
                                           "password": SENTINEL})
    request = OutboundRequest(host="files.internal", port=22)

    returned = secrets.inject_credential(tenant, "sftp-hr", request)

    assert returned is None  # nothing comes back to the caller
    # ... but the transport sees the full connect kwargs:
    assert request.params == {"host": "files.internal", "port": 22,
                              "username": "svc-hr", "password": SENTINEL}
    assert request.secret_keys == {"username", "password"}


def test_carrier_masks_secrets_in_repr_and_str(secrets, tenant):
    secrets.put_secret(tenant, "sftp-hr", {"password": SENTINEL})
    request = OutboundRequest(host="files.internal")
    secrets.inject_credential(tenant, "sftp-hr", request)

    assert SENTINEL not in repr(request)
    assert SENTINEL not in str(request)
    assert "host" in repr(request)  # non-secret params stay visible


def test_get_secret_escape_hatch(secrets, tenant):
    secrets.put_secret(tenant, "legacy-api", {"api_key": "k-123"})
    assert secrets.get_secret(tenant, "legacy-api") == {"api_key": "k-123"}


def test_rotate_credential_merges_updates(secrets, tenant):
    # CredentialRotator seam (QBO refresh-token rotation): named fields are
    # replaced, unnamed fields survive the write.
    secrets.put_secret(tenant, "qbo-books", {"client_id": "app-id",
                                             "realm_id": "9341",
                                             "refresh_token": "rt-old"})
    secrets.rotate_credential(tenant, "qbo-books", {"refresh_token": "rt-new"})
    assert secrets.get_secret(tenant, "qbo-books") == {
        "client_id": "app-id", "realm_id": "9341", "refresh_token": "rt-new"}


def test_rotate_unprovisioned_credential_raises_not_found(secrets, tenant):
    # Rotation is an UPDATE, never a create: rotating a credential that was
    # never provisioned is a wiring bug and must fail loudly.
    with pytest.raises(SecretNotFound):
        secrets.rotate_credential(tenant, "never-there", {"refresh_token": "x"})


def test_missing_secret_raises_not_found_without_values(secrets, tenant):
    with pytest.raises(SecretNotFound) as excinfo:
        secrets.inject_credential(tenant, "never-provisioned", OutboundRequest())
    message = str(excinfo.value)
    assert "never-provisioned" in message and tenant in message
    assert SENTINEL not in message


def test_denied_secret_raises_access_denied(tenant):
    bad = OpenBaoSecretsProvider(client=hvac.Client(
        url=settings.bao_addr, token="not-a-valid-token"))
    with pytest.raises(SecretAccessDenied):
        bad.get_secret(tenant, "sftp-hr")


def test_secrets_are_tenant_scoped(secrets, tenant):
    secrets.put_secret(tenant, "shared-ref", {"password": "tenant-a-value"})
    with pytest.raises(SecretNotFound):
        secrets.get_secret(f"{tenant}-other", "shared-ref")


# ---------------------------------------------------------------------------
# Graceful degradation through the capture flow
# ---------------------------------------------------------------------------
class CredentialedStubAdapter(SourceAdapter):
    """Exercises the seam exactly the way a real SFTP adapter will: prepare()
    builds an OutboundRequest and asks the provider to fill it. Yields one
    fixed item once prepared, so a successful run is observable."""

    source_system = "stub-sftp"

    def prepare(self, tenant_id: str, secrets: Optional[SecretsProvider]) -> None:
        self.request = OutboundRequest(host="stub.internal", port=22)
        secrets.inject_credential(tenant_id, self.source_ref, self.request)

    def backfill(self, tenant_id: str,
                 resume_after: Optional[str] = None) -> Iterator[SourceItem]:
        from datetime import datetime, timezone
        content = f"stub payload for {self.source_ref}".encode()
        yield SourceItem(
            native_id=f"{self.source_ref}/doc.txt", content=content,
            size=len(content), mtime=datetime.now(timezone.utc),
            source_acl={"mode": "0o600"}, cursor="00000000000000000001:doc.txt",
        )

    def incremental(self, tenant_id: str,
                    cursor: Optional[str]) -> Iterator[SourceItem]:
        return iter(())


def test_missing_secret_degrades_source_not_tenant(
        capture, secrets, store, tenant, caplog):
    # Two sources, one tenant: 'good' has a credential, 'broken' does not.
    secrets.put_secret(tenant, "good", {"username": "svc", "password": SENTINEL})
    good, broken = CredentialedStubAdapter("good"), CredentialedStubAdapter("broken")

    with caplog.at_level(logging.DEBUG):
        broken_result = capture.run_source(tenant, broken)
        good_result = capture.run_source(tenant, good)

    # The broken source is degraded and marked in the registry...
    assert broken_result.status == "degraded" and broken_result.landed == 0
    entry = capture.registry.get(tenant, "broken")
    assert entry.status == "degraded"
    assert "SecretNotFound" in entry.status_reason

    # ...the tenant is unaffected: its other source landed and dispatched.
    assert good_result.status == "ok" and good_result.landed == 1
    with store.transaction(tenant) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM raw_documents WHERE tenant_id = %s",
            (tenant,)).fetchone()["n"]
    assert n == 1
    assert capture.registry.get(tenant, "good").status == "active"

    # And nothing along the way leaked a secret value.
    assert SENTINEL not in caplog.text
    assert SENTINEL not in (entry.status_reason or "")
    assert SENTINEL not in repr(good.request)


def test_degraded_source_recovers_once_credential_appears(
        capture, secrets, tenant):
    adapter = CredentialedStubAdapter("late-cred")
    assert capture.run_source(tenant, adapter).status == "degraded"

    secrets.put_secret(tenant, "late-cred", {"password": SENTINEL})
    result = capture.run_source(tenant, adapter)

    assert result.status == "ok"
    assert capture.registry.get(tenant, "late-cred").status == "active"
