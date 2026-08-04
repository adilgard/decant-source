"""The credential seam's posture switch — two factories, one decision each.

d.s Stage 3. Every place that used to construct `OpenBaoSecretsProvider()` or
`OpenBaoCredentialResolver()` directly now calls one of these instead, so the
local/deployed choice is made in ONE file rather than at eight call sites.

    make_secrets_provider()    -> SecretsProvider   (source credentials)
    make_credential_resolver() -> CredentialResolver (the login registry)

Why factories and not a flag inside the existing classes: the two
implementations share nothing but their ABC. OpenBaoSecretsProvider is an hvac
client with a KV v2 path layout; LocalFileSecretsProvider is a JSON file. A
posture branch inside either one would make both carry the other's concepts. The
seam already existed — SecretsProvider and CredentialResolver have been the only
way credentials enter the system since Build Prompt 2 — so this is extend, never
modify: the vault classes are untouched, and every consumer keeps holding an ABC
and keeps not caring which side it got.

Imports are LAZY inside each function, and that one matters: importing
secrets_openbao constructs nothing but does pull hvac, and the whole promise of
local posture is that nothing needs a vault to start. A module-level import of
both would be a vault dependency at import time in the posture that is supposed
to have none.

Out of scope, named so it is not silently assumed:
  1. Per-tenant credential POLICY (a tenant's token reading only its own paths).
     That is a vault capability and comes back with the deployed posture, where
     it is a real requirement rather than a single-user formality.
  2. Rotation-by-policy and multi-user issuance: same answer.
  3. Nothing here gates a provenance, correctness, or boundary check. The
     resolver returns an identity; the choke point still enforces on it.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from knowledge_hub.config import settings

if TYPE_CHECKING:  # import-time free; for type checkers only
    from knowledge_hub.choke_point import CredentialResolver
    from knowledge_hub.interfaces import SecretsProvider

logger = logging.getLogger(__name__)


def make_secrets_provider() -> "SecretsProvider":
    """The SecretsProvider this posture uses for SOURCE credentials.

    local    -> LocalFileSecretsProvider over settings.local_secrets_file
    deployed -> OpenBaoSecretsProvider (unchanged)

    Both implement CredentialRotator, so a rotating connector (QBO re-issues its
    refresh token on every refresh) has a write path in either posture.
    """
    if settings.is_local:
        from knowledge_hub.secrets_local import LocalFileSecretsProvider
        return LocalFileSecretsProvider()
    from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
    return OpenBaoSecretsProvider()


def make_credential_resolver() -> "CredentialResolver":
    """The CredentialResolver this posture uses for the LOGIN registry.

    local    -> LocalFileCredentialResolver over settings.local_secrets_file
    deployed -> OpenBaoCredentialResolver (unchanged)

    This is the load-bearing one. Both HTTP boundaries authenticate only through
    a CredentialResolver, so before Stage 3 the operator console could not be
    opened without OpenBao running — on a single-user internal box whose only
    source adapter needs no credentials at all.
    """
    if settings.is_local:
        from knowledge_hub.secrets_local import LocalFileCredentialResolver
        return LocalFileCredentialResolver()
    from knowledge_hub.choke_point import OpenBaoCredentialResolver
    return OpenBaoCredentialResolver()


def local_session_token() -> Optional[str]:
    """A console credential for THIS box, or None outside local posture.

    The "nothing to record, nothing to type" path: mints and registers a local
    operator principal if needed and returns the token, so `khctl console` and
    the /ui/local-session handoff can log the console in without a human ever
    seeing a credential.

    Returns None in deployed posture rather than raising, so callers read as a
    capability check ("is there a session to hand over?") instead of a posture
    branch repeated at each site. Deployed posture mints only through the
    print-once ceremony, which is the whole point of it.
    """
    if not settings.is_local:
        return None
    from knowledge_hub.secrets_local import ensure_local_operator
    token, minted = ensure_local_operator()
    if minted:
        logger.info(
            "minted the local console identity in %s (local posture) — no "
            "credential to record; the console logs itself in",
            settings.local_secrets_file)
    return token
