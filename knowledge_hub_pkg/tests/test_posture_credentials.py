"""d.s Stage 3 — credentials from a local file in local posture; OpenBao in deployed.

The claim under test is narrow and load-bearing: **local posture can start, run,
and authenticate with no vault reachable at all**, and it does so without
weakening the enforcement boundary or asking a human to record anything.

So the central test is test_everything_works_with_the_vault_unreachable, which
points the vault at a dead port and proves the whole credential path still works.
Everything else supports it, plus the contract-parity suite: the local and vault
implementations must be indistinguishable to callers, because every consumer
holds an ABC and the whole design rests on it not caring which side it got.

No services. Postgres, OpenBao and Ollama are all absent here by design — a test
file about "works without infrastructure" that needed infrastructure would be
proving the opposite thing.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from knowledge_hub.choke_point import (
    CredentialResolver,
    OpenBaoCredentialResolver,
    PrincipalUnresolvable,
)
from knowledge_hub.config import POSTURE_DEPLOYED, POSTURE_LOCAL, settings
from knowledge_hub.credentials import (
    local_session_token,
    make_credential_resolver,
    make_secrets_provider,
)
from knowledge_hub.interfaces import (
    CredentialRotator,
    OutboundRequest,
    SecretAccessDenied,
    SecretNotFound,
    SecretsError,
    SecretsProvider,
)
from knowledge_hub.secrets_local import (
    LOCAL_PRINCIPAL_ID,
    LocalCredentialStore,
    LocalFileCredentialResolver,
    LocalFileSecretsProvider,
    credential_digest,
    ensure_local_operator,
    provision_local_credential,
)
from knowledge_hub.secrets_openbao import OpenBaoSecretsProvider
from knowledge_hub.serving import Principal


@pytest.fixture()
def store(tmp_path) -> LocalCredentialStore:
    return LocalCredentialStore(tmp_path / ".secrets.local.json")


@pytest.fixture()
def local(monkeypatch, tmp_path):
    """Local posture with the store in a temp dir — never the repo's."""
    monkeypatch.setattr(settings, "posture", POSTURE_LOCAL)
    monkeypatch.setattr(settings, "local_secrets_file",
                        str(tmp_path / ".secrets.local.json"))
    return settings


@pytest.fixture()
def deployed(monkeypatch):
    monkeypatch.setattr(settings, "posture", POSTURE_DEPLOYED)
    return settings


# ===========================================================================
# THE CENTRAL CLAIM
# ===========================================================================
def test_everything_works_with_the_vault_unreachable(local, monkeypatch):
    """The point of Stage 3, in one test.

    Points bao_addr at a closed port and drives the whole credential path:
    provision a source credential, inject it, mint a console identity, resolve
    it back to a Principal, and report health. Before Stage 3 the resolver step
    alone made this impossible — both HTTP boundaries authenticate only through
    a CredentialResolver, so the operator console could not be opened without
    OpenBao running, on a single-user box whose only source adapter needs no
    credentials at all.
    """
    # A port nothing listens on. Any attempt to reach a vault fails here.
    monkeypatch.setattr(settings, "bao_addr", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "bao_root_token", "not-a-real-token")

    provider = make_secrets_provider()
    provider.put_secret("t1", "src1", {"username": "u", "password": "p"})
    request = OutboundRequest()
    provider.inject_credential("t1", "src1", request)
    assert request.params["password"] == "p"

    resolver = make_credential_resolver()
    token = local_session_token()
    assert token, "local posture must be able to mint its own console identity"

    principal = resolver.resolve_principal(token)
    assert principal.principal_id == LOCAL_PRINCIPAL_ID
    assert "operator" in principal.roles
    assert resolver.ping() is True
    assert resolver.status() == "ok"


def test_no_vault_module_is_even_imported_for_the_local_path(local,
                                                             monkeypatch):
    """Stronger than "it works": the local path must not so much as construct a
    vault client. The factories import lazily for exactly this reason — a
    module-level import of secrets_openbao would make hvac a start-up
    dependency in the posture whose whole promise is not needing one."""
    import hvac

    def explode(*a, **kw):
        raise AssertionError("the local path constructed an hvac client")

    monkeypatch.setattr(hvac, "Client", explode)

    provider = make_secrets_provider()
    provider.put_secret("t", "s", {"k": "v"})
    resolver = make_credential_resolver()
    resolver.resolve_principal(local_session_token())


# ===========================================================================
# The factories
# ===========================================================================
def test_factories_pick_the_local_implementations(local):
    assert isinstance(make_secrets_provider(), LocalFileSecretsProvider)
    assert isinstance(make_credential_resolver(), LocalFileCredentialResolver)


def test_factories_pick_openbao_in_deployed_posture(deployed):
    """Constructing these contacts nothing — hvac.Client is lazy — so this is
    safe with no vault running, and it is the whole 'deployed is unchanged'
    claim for the credential seam."""
    assert isinstance(make_secrets_provider(), OpenBaoSecretsProvider)
    assert isinstance(make_credential_resolver(), OpenBaoCredentialResolver)


def test_local_session_token_is_none_in_deployed_posture(deployed):
    """A capability check, not a posture branch repeated at each call site.
    Deployed posture mints only through the print-once ceremony."""
    assert local_session_token() is None


def test_both_implementations_satisfy_their_abcs():
    assert issubclass(LocalFileSecretsProvider, SecretsProvider)
    assert issubclass(LocalFileSecretsProvider, CredentialRotator)
    assert issubclass(LocalFileCredentialResolver, CredentialResolver)


# ===========================================================================
# Contract parity — callers must not be able to tell them apart
# ===========================================================================
def test_the_two_providers_expose_the_same_surface():
    """Anything the vault provider offers, the local one must too. A caller
    holding a SecretsProvider that reached for a method and found it missing is
    a posture-dependent AttributeError at runtime — the worst possible place to
    discover the seam leaked.

    NO CARVE-OUTS, and this test learned that the hard way. Its first version
    read `hasattr(local, name) or name == "path_for"`, excusing the single
    member the local provider was missing. It passed. Meanwhile
    operator_http._credential_info calls path_for on every add_source, so the
    console answered HTTP 500 in local posture until test_operator_http caught
    it. An exemption in a parity test is the parity failing quietly."""
    vault_surface = {name for name in dir(OpenBaoSecretsProvider)
                     if not name.startswith("_")}
    missing = {name for name in vault_surface
               if not hasattr(LocalFileSecretsProvider, name)}
    assert not missing, (
        f"OpenBaoSecretsProvider members with no local counterpart: "
        f"{sorted(missing)} — callers hold the ABC and cannot tell which "
        f"implementation they got")


def test_path_for_names_a_real_location_in_both_postures():
    """It is what the console shows an operator when a source has no credential
    yet, so it has to point somewhere they can actually go."""
    from pathlib import Path

    local_path = LocalFileSecretsProvider(
        LocalCredentialStore(Path("x") / ".secrets.local.json")
    ).path_for("t1", "src1")
    assert "t1" in local_path and "src1" in local_path
    assert ".secrets.local.json" in local_path

    vault_path = OpenBaoSecretsProvider.path_for("t1", "src1")
    assert "t1" in vault_path and "src1" in vault_path


def test_the_vault_path_layout_is_exact():
    """The literal per-tenant vault layout, pinned.

    It matters beyond cosmetics: production vault POLICIES scope to this exact
    shape (a tenant's token may read only tenants/<its-id>/...), so changing it
    silently would widen or break tenant isolation.

    It lives here because it had no assertion anywhere else. The only test that
    pinned it was an incidental line in test_operator_http's pause/resume test,
    which now asserts against the ACTIVE provider so it holds in both postures —
    correct for that test, but it would have left this contract uncovered.
    """
    assert (OpenBaoSecretsProvider.path_for("t1", "src1")
            == "tenants/t1/sources/src1")


def test_the_principal_registry_layout_is_exact():
    """Same reasoning for the identity registry: it lives OUTSIDE tenants/ on
    purpose, so a tenant's own vault policy can never read the registry that
    says who anyone is."""
    digest = credential_digest("some-token")
    path = OpenBaoCredentialResolver.path_for("some-token")
    assert path == f"serving/principals/{digest}"
    assert not path.startswith("tenants/")


def test_the_two_resolvers_expose_the_same_surface():
    for name in ("resolve_principal", "register_principal", "status", "ping"):
        assert hasattr(OpenBaoCredentialResolver, name)
        assert hasattr(LocalFileCredentialResolver, name), (
            f"OpenBaoCredentialResolver.{name} has no local counterpart")


@pytest.mark.parametrize("missing_what,expected", [
    ("tenant", SecretNotFound),
    ("source", SecretNotFound),
])
def test_a_missing_credential_raises_secret_not_found(store, missing_what,
                                                      expected):
    """Same exception type as the vault path for the same condition. Capture-flow
    code catches SecretsError to degrade ONE source instead of failing the
    tenant; a different type here would silently change that blast radius."""
    provider = LocalFileSecretsProvider(store)
    provider.put_secret("t1", "src1", {"k": "v"})
    tenant = "nope" if missing_what == "tenant" else "t1"
    ref = "src1" if missing_what == "tenant" else "nope"
    with pytest.raises(expected):
        provider.get_secret(tenant, ref)


def test_an_empty_credential_is_refused_on_write(store):
    provider = LocalFileSecretsProvider(store)
    with pytest.raises(SecretAccessDenied):
        provider.put_secret("t", "s", {})


def test_an_empty_stored_credential_reads_as_not_found(store):
    """Hand-edited files happen — this file is meant to be edited by hand. An
    empty dict must be 'nothing provisioned', not an injection of no fields."""
    store.write({"sources": {"t": {"s": {}}}, "principals": {}})
    with pytest.raises(SecretNotFound):
        LocalFileSecretsProvider(store).get_secret("t", "s")


# ===========================================================================
# The no-leak invariant
# ===========================================================================
def test_injected_values_are_masked_in_repr(store):
    """The invariant that keeps credential values out of logs and exception
    messages. Backend-agnostic, and asserted on the local path because a JSON
    file feels more casual than a vault and the invariant is identical."""
    provider = LocalFileSecretsProvider(store)
    provider.put_secret("t", "s", {"password": "hunter2"})
    request = OutboundRequest()
    provider.inject_credential("t", "s", request)
    assert "hunter2" not in repr(request)
    assert "hunter2" not in str(request)


def test_no_error_message_carries_a_credential_value(store):
    """Errors name the FILE and the tenant/source, never a value — the same
    rule the vault implementation follows for paths."""
    provider = LocalFileSecretsProvider(store)
    provider.put_secret("t", "s", {"password": "hunter2"})
    for tenant, ref in (("t", "missing"), ("missing", "s")):
        with pytest.raises(SecretsError) as e:
            provider.get_secret(tenant, ref)
        assert "hunter2" not in str(e.value)


def test_the_token_is_never_stored_and_never_in_an_error(store):
    """Principals are keyed on sha256, exactly as the vault PATH is. So the
    live token appears nowhere in the file, and a failed resolution can quote
    the digest without quoting the secret."""
    resolver = LocalFileCredentialResolver(store)
    token = "kh-operator-t1-abcdef0123456789"
    resolver.register_principal(token, Principal(
        tenant_id="t1", principal_id="p1", roles=["operator"]))

    raw = store.path.read_text(encoding="utf-8")
    assert token not in raw, "the credential VALUE must never be stored"
    assert credential_digest(token) in raw

    with pytest.raises(PrincipalUnresolvable) as e:
        resolver.resolve_principal("some-other-token")
    assert "some-other-token" not in str(e.value)


def test_resolution_failures_all_look_the_same(store):
    """One refusal for every failure mode, matching the vault resolver: unknown,
    revoked, malformed, blank, unreadable store. A caller must not be able to
    probe the registry by reading the difference between two rejections."""
    resolver = LocalFileCredentialResolver(store)
    resolver.register_principal("good", Principal(
        tenant_id="t", principal_id="p", roles=[]))

    for bad in ("", "   ", "unknown-token", None, 12345):
        with pytest.raises(PrincipalUnresolvable):
            resolver.resolve_principal(bad)

    store.write({"sources": {},
                 "principals": {credential_digest("malformed"): "not-a-dict"}})
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal("malformed")

    store.write({"sources": {}, "principals": {
        credential_digest("blank"): {"tenant_id": " ", "principal_id": "p",
                                     "roles": []}}})
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal("blank")


def test_a_corrupt_store_refuses_every_credential_and_is_not_read_as_empty(
        store):
    """A missing file is empty (first run must start). A DAMAGED file is an
    error, and the distinction matters: treating unparseable JSON as empty would
    auto-provision a second identity beside a registry still sitting on disk and
    report success."""
    store.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(SecretsError):
        store.read()

    resolver = LocalFileCredentialResolver(store)
    assert resolver.status() == "unreachable"
    assert resolver.ping() is False
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal("anything")


def test_a_missing_file_is_empty_not_an_error(store):
    assert not store.exists()
    assert store.read()["principals"] == {}
    assert store.read()["sources"] == {}


def test_an_unknown_store_version_is_refused(store):
    """Forward-compatibility honesty: a file written by a newer build must not
    be silently half-read."""
    store.path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(SecretsError, match="version"):
        store.read()


# ===========================================================================
# Writes
# ===========================================================================
def test_writes_are_atomic_and_leave_no_temp_files(store):
    provider = LocalFileSecretsProvider(store)
    for i in range(5):
        provider.put_secret("t", f"src{i}", {"k": str(i)})
    leftovers = [p.name for p in store.path.parent.iterdir()
                 if p.name != store.path.name]
    assert not leftovers, f"temp files left behind: {leftovers}"


def test_a_failed_write_leaves_the_previous_store_intact(store, monkeypatch):
    """The reason atomicity is not ceremony here: the principal registry is the
    one thing on the box that cannot be re-derived from anything else. A torn
    write would leave the console with no identity and nothing to rebuild from."""
    provider = LocalFileSecretsProvider(store)
    provider.put_secret("t", "keep", {"k": "original"})
    before = store.path.read_text(encoding="utf-8")

    monkeypatch.setattr("os.replace",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        provider.put_secret("t", "new", {"k": "v"})

    assert store.path.read_text(encoding="utf-8") == before
    leftovers = [p.name for p in store.path.parent.iterdir()
                 if p.name != store.path.name]
    assert not leftovers, f"a failed write left {leftovers} behind"


def test_rotation_merges_and_keeps_unnamed_fields(store):
    """QBO re-issues its refresh token on every refresh, and a rotation that
    dropped the client_id beside it would lock the connector out until a human
    re-consents. Same contract as the vault rotator."""
    provider = LocalFileSecretsProvider(store)
    provider.put_secret("t", "qbo", {"client_id": "cid",
                                     "client_secret": "sec",
                                     "refresh_token": "old"})
    provider.rotate_credential("t", "qbo", {"refresh_token": "new"})
    secret = provider.get_secret("t", "qbo")
    assert secret == {"client_id": "cid", "client_secret": "sec",
                      "refresh_token": "new"}


def test_two_tenants_do_not_see_each_other(store):
    provider = LocalFileSecretsProvider(store)
    provider.put_secret("t1", "shared-name", {"who": "one"})
    provider.put_secret("t2", "shared-name", {"who": "two"})
    assert provider.get_secret("t1", "shared-name")["who"] == "one"
    assert provider.get_secret("t2", "shared-name")["who"] == "two"


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX permission bits; on Windows the file is "
                           "protected exactly as much as the .env beside it")
def test_the_store_is_owner_only_on_posix(store):
    LocalFileSecretsProvider(store).put_secret("t", "s", {"k": "v"})
    mode = stat.S_IMODE(store.path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


# ===========================================================================
# Auto-provisioning — nothing for a human to record
# ===========================================================================
def test_the_first_run_mints_an_identity_with_no_human_step(store):
    token, minted = ensure_local_operator(store)
    assert minted is True
    assert token
    principal = LocalFileCredentialResolver(store).resolve_principal(token)
    assert principal.principal_id == LOCAL_PRINCIPAL_ID
    assert "operator" in principal.roles


def test_the_console_identity_has_the_same_token_shape_as_every_other(store):
    """Caught by the live browser round-trip, not by a unit test.

    ensure_local_operator used to generate its own token with token_urlsafe
    while a comment three screens up claimed shape parity with the vault path.
    Everything WORKED — the resolver keys on a digest, so any string resolves —
    so nothing failed. It was just a lie in a comment, and the shape has real
    uses: a token in some other program's config should be recognizable as a d.s
    credential with its role legible, and a deployed credential and a local one
    should be indistinguishable downstream.

    One minting function now, so the claim cannot drift again."""
    token, _ = ensure_local_operator(store, tenant_id="t1")
    assert token.startswith("kh-operator-t1-")

    agent, _ = provision_local_credential("t1", (), "actor", store=store)
    op, _ = provision_local_credential("t1", ("operator",), "actor",
                                       store=store)
    for other in (agent, op):
        assert other.startswith("kh-"), (
            "every credential in the system shares one prefix")
    assert len({t.count("-") for t in (token, agent, op)}) == 1, (
        "same segment structure, so nothing can tell them apart by shape")


def test_the_console_identity_keeps_a_stable_principal_id(store):
    """It must be recognizable across runs — that is how ensure_local_operator
    knows an identity already exists rather than claiming every run is the
    first. Hence the principal_id override, mirroring the vault twin's."""
    ensure_local_operator(store, tenant_id="t1")
    ensure_local_operator(store, tenant_id="t1")
    ids = {r["principal_id"] for r in store.read()["principals"].values()}
    assert ids == {LOCAL_PRINCIPAL_ID}


def test_a_later_run_reports_it_did_not_mint_the_first_one(store):
    """Honest about what it is: because the store keeps DIGESTS, an existing
    token cannot be read back, so a second call mints a fresh one. It reports
    minted=False to say the identity already existed — the flag is about the
    IDENTITY, not the token."""
    first, first_minted = ensure_local_operator(store)
    second, second_minted = ensure_local_operator(store)
    assert first_minted is True
    assert second_minted is False
    assert first != second


def test_an_earlier_token_keeps_working_after_a_re_mint(store):
    """Why the old row is left in place rather than cleaned up: it may be the
    token a console tab is still holding in sessionStorage. Invalidating a
    working session to keep a single-user file tidy is the worse trade."""
    first, _ = ensure_local_operator(store)
    second, _ = ensure_local_operator(store)
    resolver = LocalFileCredentialResolver(store)
    assert resolver.resolve_principal(first).principal_id == LOCAL_PRINCIPAL_ID
    assert resolver.resolve_principal(second).principal_id == LOCAL_PRINCIPAL_ID


def test_a_token_cannot_be_recovered_from_the_store(store):
    """Made explicit rather than left for a caller to wonder about. Recovering a
    token would mean storing it in plaintext, which is the one thing the digest
    keying exists to avoid — in either posture."""
    ensure_local_operator(store)
    assert LocalFileCredentialResolver(store).find_principal_credential() is None


def test_revocation_removes_exactly_one_credential(store):
    resolver = LocalFileCredentialResolver(store)
    keep, _ = ensure_local_operator(store)
    drop, _ = ensure_local_operator(store)

    assert resolver.revoke_principal(drop) is True
    assert resolver.revoke_principal(drop) is False, "already gone"
    with pytest.raises(PrincipalUnresolvable):
        resolver.resolve_principal(drop)
    assert resolver.resolve_principal(keep).principal_id == LOCAL_PRINCIPAL_ID


def test_provisioned_credentials_match_the_vault_paths_shape(store):
    """A local credential must be indistinguishable downstream from a vault one
    — same token shape, same principal_id shape — so the same console, role
    gate, and audit trail serve both postures."""
    token, pid = provision_local_credential("t1", ("operator",), "someone",
                                            store=store)
    assert token.startswith("kh-operator-t1-")
    assert pid.startswith("t1-operator-")
    principal = LocalFileCredentialResolver(store).resolve_principal(token)
    assert principal.roles == ["operator"]


def test_an_agent_credential_has_no_console_role(store):
    """Empty roles by design: agent principals read through the serving boundary
    and can perform no operator write. The console's F3 branch relies on this
    distinction being real, in both postures."""
    token, _ = provision_local_credential("t1", (), "someone", store=store)
    assert LocalFileCredentialResolver(store).resolve_principal(token).roles == []


def test_attribution_rides_the_record_and_the_resolver_ignores_it(store):
    provision_local_credential("t1", ("operator",), "an-actor", store=store)
    record = next(iter(store.read()["principals"].values()))
    assert record["provisioned_by"] == "an-actor"
    assert "provisioned_at" in record
    # The resolver reads only the identity triple; extra fields are inert.
    assert set(record) > {"tenant_id", "principal_id", "roles"}


# ===========================================================================
# khctl console / provision-* in local posture
# ===========================================================================
def test_console_says_it_logs_itself_in_and_mints_nothing(local, tmp_path,
                                                          monkeypatch, capsys):
    """No credential printed, nothing to paste. Printing one here would put a
    human back in the loop for no benefit: the browser and the service are both
    on this machine and both can read the credential file."""
    import knowledge_hub.deploy_cli as dc
    import knowledge_hub.deploy_launch as dl

    monkeypatch.setattr(dl, "ensure_operator", lambda *a: True)
    monkeypatch.setattr(dc, "_vault_status",
                        lambda addr: pytest.fail(
                            "local posture must not probe a vault seal"))
    monkeypatch.chdir(tmp_path)

    rc = dc.main(["console", "--work-dir", str(tmp_path), "--no-browser"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "logs itself in" in out
    assert "Nothing to record or paste" in out
    assert "record NOW" not in out, "local posture must mint nothing here"
    assert "Connector credentials" in out, (
        "name the one thing a human DOES still enter, or 'nothing to record' "
        "overpromises")


def test_console_still_diagnoses_a_sealed_vault_when_deployed(deployed,
                                                             tmp_path,
                                                             monkeypatch,
                                                             capsys):
    """The F1 diagnosis is untouched wherever a vault is actually in play."""
    import knowledge_hub.deploy_cli as dc
    import knowledge_hub.deploy_launch as dl

    monkeypatch.setattr(dc, "_vault_status", lambda addr: "sealed")
    monkeypatch.setattr(dl, "ensure_operator",
                        lambda *a: pytest.fail("must refuse before the door"))
    monkeypatch.chdir(tmp_path)

    rc = dc.main(["console", "--work-dir", str(tmp_path), "--no-browser"])
    assert rc == 1
    assert "SEALED" in capsys.readouterr().out


@pytest.mark.parametrize("command,expect_role", [
    (["provision-operator", "--tenant", "t1", "--role", "operator"], True),
    (["provision-agent", "--tenant", "t1"], False),
])
def test_provisioning_works_locally_with_no_vault(local, tmp_path, monkeypatch,
                                                  capsys, command,
                                                  expect_role):
    """These survive in local posture because handing a token to an EXTERNAL
    agent is a real integration need, not ceremony. What does not survive is the
    vault-custody gate around it."""
    import knowledge_hub.deploy_cli as dc

    monkeypatch.setattr(dc, "_vault_status",
                        lambda addr: pytest.fail("no vault in local posture"))
    monkeypatch.chdir(tmp_path)

    rc = dc.main([*command, "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "digest only" in out
    assert "cannot be read back" in out
    assert "khctl provision-" in out, "say how to issue another"

    resolver = make_credential_resolver()
    records = list(resolver.store.read()["principals"].values())
    assert len(records) == 1
    assert bool(records[0]["roles"]) is expect_role


# ===========================================================================
# Which credential check each posture verifies
# ===========================================================================
def test_local_verify_swaps_openbao_for_the_posture_agnostic_check(local):
    """A local box has no vault, so asking `khctl verify` to authenticate
    against one would fail a healthy machine. The claim still proven is the one
    that matters — a credential can be stored, injected, and never leaked."""
    from knowledge_hub.deploy_cli import verify_checks_for
    from knowledge_hub.deploy_profiles import load_profiles, resolve_plan
    from test_deploy_plan import INFRA_DIR, make_probe

    plan = resolve_plan(load_profiles(INFRA_DIR / "profiles.toml"),
                        "appliance", make_probe(), tenants=["ops"])
    names = [name for name, _ in verify_checks_for(plan)]
    assert "credential seam" in names
    assert "openbao" not in names
    # The protected checks are still selected, in local posture too.
    assert "migration ledger" in names
    assert "side doors (§8.8 rider)" in names
    assert "core boundary (corpus-agnostic)" in names


def test_deployed_verify_still_asks_for_the_vault(deployed):
    from knowledge_hub.deploy_cli import verify_checks_for
    from knowledge_hub.deploy_profiles import load_profiles, resolve_plan
    from test_deploy_plan import INFRA_DIR, make_probe

    plan = resolve_plan(load_profiles(INFRA_DIR / "profiles.toml"),
                        "appliance", make_probe(), tenants=["ops"])
    names = [name for name, _ in verify_checks_for(plan)]
    assert "openbao" in names
    assert "credential seam" not in names


def test_the_credential_seam_check_passes_with_no_vault(local):
    """The check itself, run for real in local posture — the thing
    `khctl verify` and check_stack.py now call on this bench."""
    from knowledge_hub.checks import check_credential_seam

    detail = check_credential_seam()
    assert "local posture" in detail
    assert "masked" in detail


# ===========================================================================
# Scope — Stage 3 must not have touched the protected checks
# ===========================================================================
# The modules the credential seam must not reach. Checked as IMPORTS, which is
# the structural version of the claim: a module that does not import these
# cannot call into them, and unlike a text scan it cannot be tripped by a
# docstring that merely names them (which is how the first draft of this test
# failed — third time learning that lesson in this build).
FORBIDDEN_IMPORTS = frozenset({
    "knowledge_hub.grounding",           # span verification
    "knowledge_hub.ontology",            # the allowlist binding
    "knowledge_hub.ontology_registry",   # the allowlist gate
    "knowledge_hub.migrations",          # the ledger
    "knowledge_hub.extraction",
    "knowledge_hub.extraction_llm",      # allowlist quarantine
    "knowledge_hub.scoring_tiered",      # adjudication
    "knowledge_hub.resolution",
    "knowledge_hub.checks",
})


def _imported_modules(source: str) -> set[str]:
    """Every module name an import statement in `source` names."""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
    return names


def test_the_new_modules_do_not_touch_provenance_or_correctness():
    """Same tripwire discipline as Stages 1 and 2. The credential seam decides
    where an IDENTITY comes from; it must not reach into span grounding, the
    ontology allowlist, the migration ledger, or adjudication."""
    import inspect

    from knowledge_hub import credentials, secrets_local

    for module in (credentials, secrets_local):
        imported = _imported_modules(inspect.getsource(module))
        offenders = imported & FORBIDDEN_IMPORTS
        assert not offenders, (
            f"{module.__name__} imports {sorted(offenders)} — the credential "
            f"seam must not reach into provenance or correctness machinery")


def test_the_import_tripwire_works():
    """It catches a real import and ignores prose that merely names one."""
    assert _imported_modules(
        "from knowledge_hub.grounding import SpanGrounder"
    ) & FORBIDDEN_IMPORTS
    assert _imported_modules("import knowledge_hub.checks") & FORBIDDEN_IMPORTS
    assert not _imported_modules(
        '"""Nothing here gates grounding or knowledge_hub.checks."""'
    ) & FORBIDDEN_IMPORTS


def test_the_resolver_does_not_bypass_the_choke_point():
    """The local resolver returns an identity; it must not offer any way to
    execute a query or mint a FilteredQuery. Enforcement stays where it was."""
    for attr in ("enforce", "read", "execute", "query", "connection", "_conn"):
        assert not hasattr(LocalFileCredentialResolver, attr), (
            f"LocalFileCredentialResolver.{attr} — the resolver resolves "
            f"identity and must not reach the enforcement boundary")
