"""OpenBaoSecretsProvider — hvac implementation of SecretsProvider.

Credentials live in a KV v2 mount at per-tenant paths:

    <mount>/tenants/<tenant_id>/sources/<source_ref>

The pilot runs OpenBao in dev mode (in-memory, root token), but nothing here
assumes that: the path layout is what per-tenant vault POLICIES scope to in
production (a tenant's token can only read tenants/<its-id>/...), and auth is
pluggable — pass a differently-authenticated hvac.Client (AppRole, k8s, ...)
and everything else is unchanged.

Invariant: secret VALUES never appear in logs, exception messages, or return
values — except through the explicit `get_secret` escape hatch. This module
has no logging of secret data at all; errors carry paths, never payloads.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import hvac
import hvac.exceptions

from knowledge_hub.config import settings
from knowledge_hub.interfaces import (
    CredentialRotator,
    OutboundRequest,
    SecretAccessDenied,
    SecretNotFound,
    SecretsError,
    SecretsProvider,
)


class OpenBaoSecretsProvider(SecretsProvider, CredentialRotator):
    def __init__(self, client: Optional[hvac.Client] = None,
                 mount: Optional[str] = None):
        # Default client: token auth from settings (dev mode = root token).
        # Production swaps in a client authed via AppRole/k8s with per-tenant
        # policies; the paths below are what those policies scope to.
        self._client = client or hvac.Client(
            url=settings.bao_addr, token=settings.bao_root_token)
        self._mount = mount or settings.bao_kv_mount

    @staticmethod
    def path_for(tenant_id: str, source_ref: str) -> str:
        """Vault path (relative to the KV mount) for one source's credential."""
        return f"tenants/{tenant_id}/sources/{source_ref}"

    # ------------------------------------------------------------------ seam
    def inject_credential(self, tenant_id: str, source_ref: str,
                          request: OutboundRequest) -> None:
        secret = self._read(tenant_id, source_ref)
        for key, value in secret.items():
            request.attach_secret(key, value)

    def get_secret(self, tenant_id: str, source_ref: str) -> Mapping[str, Any]:
        return self._read(tenant_id, source_ref)

    # ------------------------------------------------------------ provision
    def put_secret(self, tenant_id: str, source_ref: str,
                   secret: Mapping[str, Any]) -> None:
        """Provision/rotate a source credential (setup + tests). Not part of
        the SecretsProvider ABC — capture-flow code never writes secrets."""
        try:
            self._client.secrets.kv.v2.create_or_update_secret(
                mount_point=self._mount,
                path=self.path_for(tenant_id, source_ref),
                secret=dict(secret),
            )
        except hvac.exceptions.Forbidden as e:
            raise SecretAccessDenied(tenant_id, source_ref, "write refused") from e

    # -------------------------------------------------------------- rotation
    def rotate_credential(self, tenant_id: str, source_ref: str,
                          updates: Mapping[str, Any]) -> None:
        """CredentialRotator seam: merge `updates` over the stored credential
        (read-modify-write; unnamed fields survive). The adapter is the sole
        writer for its source — same single-writer argument that makes opaque
        cursor checkpoints safe — so no compare-and-swap is needed."""
        current = dict(self._read(tenant_id, source_ref))
        current.update(updates)
        self.put_secret(tenant_id, source_ref, current)

    # -------------------------------------------------------------- internal
    def _read(self, tenant_id: str, source_ref: str) -> dict[str, Any]:
        path = self.path_for(tenant_id, source_ref)
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                mount_point=self._mount, path=path,
                raise_on_deleted_version=True,
            )
        except hvac.exceptions.InvalidPath as e:
            raise SecretNotFound(tenant_id, source_ref,
                                 f"nothing at {self._mount}/{path}") from e
        except hvac.exceptions.Forbidden as e:
            raise SecretAccessDenied(tenant_id, source_ref,
                                     f"read refused at {self._mount}/{path}") from e
        except hvac.exceptions.VaultError as e:
            # Wrap so callers can degrade on ANY vault failure; hvac messages
            # describe transport/path state, never stored values.
            raise SecretsError(tenant_id, source_ref, type(e).__name__) from e
        data = response["data"]["data"]
        if not isinstance(data, dict) or not data:
            raise SecretNotFound(tenant_id, source_ref,
                                 f"empty secret at {self._mount}/{path}")
        return data
