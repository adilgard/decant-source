"""Operator access — credential provisioning + the console door (BP23),
against the REAL vault. What must hold:

* deploy bootstrap mints the FIRST operator credential: printed once,
  digest registered with attribution, idempotent (a re-run says so and
  never re-mints or re-prints);
* the printed token actually resolves as an operator principal (the login
  check the UI performs);
* `provision-operator` issues additional credentials (reviewer scope stays
  reviewer);
* `khctl console` in DEV context mints + prints a dev key and opens the
  browser at /ui/; in a DEPLOYED context it NEVER mints;
* no credential value ever lands on disk — vault stores the digest path +
  attribution only, markers carry principal ids, work dirs stay clean.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import hvac
import pytest

from knowledge_hub import deploy_cli
from knowledge_hub.choke_point import OpenBaoCredentialResolver
from knowledge_hub.config import settings
from knowledge_hub.deploy_apply import (
    ApplyContext,
    phase_tenants,
    provision_operator_credential,
)
from knowledge_hub.deploy_kit import write_ssd_root
from knowledge_hub.deploy_profiles import DeployPlan


def make_plan(tenant: str) -> DeployPlan:
    return DeployPlan.from_json(json.dumps({
        "plan_version": "1", "profile": "appliance", "shape": "A",
        "placement": "single_box", "secrets_custody": "operator",
        "custody_overridden": False, "seams": {},
        "extraction_tier": "fp16", "extraction_model": "qwen3.6",
        "tenants": [tenant],
    }))


def make_ctx(tmp_path: Path, tenant: str) -> ApplyContext:
    env_file = tmp_path / ".env.deploy"
    env_file.write_text(
        f"BAO_ADDR={settings.bao_addr}\n"
        f"BAO_ROOT_TOKEN={settings.bao_root_token}\n"
        f"BAO_KV_MOUNT={settings.bao_kv_mount}\n", encoding="utf-8")
    ctx = ApplyContext(plan=make_plan(tenant), infra_dir=tmp_path,
                       kit_dir=tmp_path, env_file=env_file)
    ctx.env = {"BAO_ADDR": settings.bao_addr,
               "BAO_ROOT_TOKEN": settings.bao_root_token,
               "BAO_KV_MOUNT": settings.bao_kv_mount}
    return ctx


def extract_tokens(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines()
            if line.strip().startswith("kh-")]


def vault_client() -> hvac.Client:
    return hvac.Client(url=settings.bao_addr,
                       token=settings.bao_root_token)


# ---------------------------------------------------------------------------
# 1. Deploy bootstrap: first operator credential, print-once, idempotent
# ---------------------------------------------------------------------------


def test_bootstrap_mints_first_operator_credential_print_once_idempotent(
        tmp_path, capsys):
    tenant = f"boot-{uuid.uuid4().hex[:10]}"
    ctx = make_ctx(tmp_path, tenant)

    lines = phase_tenants(ctx)
    out = capsys.readouterr().out
    tokens = extract_tokens(out)
    # Two print-once credentials: the agent serving token + THE operator
    # console token.
    assert len(tokens) == 2
    agent_tok = next(t for t in tokens if t.startswith(f"kh-{tenant}-"))
    op_tok = next(t for t in tokens if t.startswith(f"kh-operator-{tenant}-"))
    assert "OPERATOR CONSOLE credential" in out
    assert "shown once" in out
    assert any("operator console credential minted" in l for l in lines)

    # The printed token IS the login: it resolves to an operator principal
    # (exactly the check the UI's unlock performs server-side).
    resolver = OpenBaoCredentialResolver(client=vault_client())
    principal = resolver.resolve_principal(op_tok)
    assert principal.tenant_id == tenant
    assert principal.roles == ["operator"]
    agent = resolver.resolve_principal(agent_tok)
    assert agent.roles == []                       # read-principal, no writes

    # Attribution rides the registry record — never the token value.
    record = vault_client().secrets.kv.v2.read_secret_version(
        mount_point=settings.bao_kv_mount,
        path=OpenBaoCredentialResolver.path_for(op_tok),
        raise_on_deleted_version=True)["data"]["data"]
    assert record["provisioned_by"]
    assert record["provisioned_at"]
    assert op_tok not in json.dumps(record)        # digest path only

    # Idempotent: a re-run says so and mints/prints NOTHING new.
    lines2 = phase_tenants(ctx)
    out2 = capsys.readouterr().out
    assert extract_tokens(out2) == []
    assert any("already bootstrapped" in l for l in lines2)
    assert any("already provisioned" in l for l in lines2)

    # And nothing credential-shaped ever landed in the work dir.
    for f in tmp_path.rglob("*"):
        if f.is_file():
            content = f.read_text(encoding="utf-8", errors="ignore")
            assert op_tok not in content and agent_tok not in content, f


def test_dry_run_bootstrap_mentions_the_operator_credential(tmp_path,
                                                            capsys):
    tenant = f"dry-{uuid.uuid4().hex[:10]}"
    ctx = make_ctx(tmp_path, tenant)
    ctx.dry_run = True
    lines = phase_tenants(ctx)
    assert any("OPERATOR CONSOLE credential" in l for l in lines)
    assert extract_tokens(capsys.readouterr().out) == []   # dry-run: no mint


# ---------------------------------------------------------------------------
# POSTURE (d.s Stage 3): this whole module is about the VAULT credential path —
# custody as the provisioning gate, the dev-mint branch, the deployed-context
# refusal. In local posture none of that applies: provisioning writes the local
# store, the console logs itself in, and there is no custody to refuse. So the
# deployed posture is pinned once here rather than in each test.
#
# The local-posture behavior of these same commands is covered in
# test_posture_credentials.py. Nothing here was weakened to accommodate it.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _deployed_posture(monkeypatch):
    from knowledge_hub.config import POSTURE_DEPLOYED, settings
    monkeypatch.setattr(settings, "posture", POSTURE_DEPLOYED)


# ---------------------------------------------------------------------------
# 2. Issue-more: provision-operator (reviewer stays reviewer-scoped)
# ---------------------------------------------------------------------------


def test_provision_operator_cli_issues_reviewer_credential(tmp_path, capsys,
                                                           monkeypatch):
    monkeypatch.chdir(tmp_path)                    # dev bench context
    tenant = f"prov-{uuid.uuid4().hex[:10]}"
    rc = deploy_cli.main(["provision-operator", "--tenant", tenant,
                          "--role", "reviewer"])
    out = capsys.readouterr().out
    assert rc == 0
    (token,) = extract_tokens(out)
    assert "REVIEWER console credential" in out
    assert "shown once" in out and "cannot be recovered" in out

    principal = OpenBaoCredentialResolver(
        client=vault_client()).resolve_principal(token)
    assert principal.tenant_id == tenant
    assert principal.roles == ["reviewer"]         # reviewer scope ONLY


def test_provision_operator_refuses_without_vault_custody(tmp_path, capsys,
                                                          monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"BAO_ADDR={settings.bao_addr}\nBAO_ROOT_TOKEN=wrong-token\n",
        encoding="utf-8")
    rc = deploy_cli.main(["provision-operator", "--tenant", "x",
                          "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "gate working" in out
    assert extract_tokens(out) == []


# ---------------------------------------------------------------------------
# 3/4. khctl console: dev mints, deployed NEVER mints; browser opens at /ui/
# ---------------------------------------------------------------------------


@pytest.fixture()
def console_spies(monkeypatch):
    import webbrowser

    import knowledge_hub.deploy_launch as dl

    opened: list[str] = []
    ensured: list[Path] = []
    monkeypatch.setattr(webbrowser, "open",
                        lambda url: opened.append(url) or True)
    monkeypatch.setattr(dl, "ensure_operator",
                        lambda work, env, say: ensured.append(work) or True)
    return opened, ensured


def test_console_dev_context_mints_prints_and_opens(tmp_path, capsys,
                                                    monkeypatch,
                                                    console_spies):
    opened, ensured = console_spies
    monkeypatch.chdir(tmp_path)                    # no deploy_plan.json = dev
    tenant = f"dev-{uuid.uuid4().hex[:10]}"
    rc = deploy_cli.main(["console", "--tenant", tenant,
                          "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dev/pilot context" in out
    assert "DEV-ONLY" in out
    (token,) = extract_tokens(out)
    principal = OpenBaoCredentialResolver(
        client=vault_client()).resolve_principal(token)
    assert principal.tenant_id == tenant and principal.roles == ["operator"]
    assert ensured == [tmp_path]                   # reuses ensure_operator
    assert opened == ["http://127.0.0.1:8081/ui/"]
    # The dev key never landed on disk either.
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert token not in f.read_text(encoding="utf-8",
                                            errors="ignore"), f


def test_console_deployed_context_never_mints(tmp_path, capsys,
                                              console_spies):
    opened, ensured = console_spies
    (tmp_path / "deploy_plan.json").write_text("{}", encoding="utf-8")
    rc = deploy_cli.main(["console", "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DEPLOYED context" in out
    assert "no credential is minted here" in out
    assert extract_tokens(out) == []               # NOTHING minted
    assert "provision-operator" in out             # points at the real path
    assert opened == ["http://127.0.0.1:8081/ui/"]


def test_console_pilot_bench_with_stray_plan_is_still_dev(tmp_path, capsys,
                                                          monkeypatch,
                                                          console_spies):
    """The bench accumulates test deploy_plan.json artifacts, but its .env
    carries the dev-vault literal — that pair is DEV. A real deployment
    never has the literal on disk (root token lives with custody), so this
    can never misfire the other way."""
    opened, _ = console_spies
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deploy_plan.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "BAO_ROOT_TOKEN=kh_pilot_root_token\n", encoding="utf-8")
    tenant = f"bench-{uuid.uuid4().hex[:10]}"
    rc = deploy_cli.main(["console", "--tenant", tenant,
                          "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dev/pilot context" in out and "DEV-ONLY" in out
    assert len(extract_tokens(out)) == 1


# ---------------------------------------------------------------------------
# The SSD Open Console shortcut ships with make-ssd
# ---------------------------------------------------------------------------


def test_make_ssd_emits_the_console_shortcut(tmp_path):
    lines = write_ssd_root(tmp_path)
    inner = tmp_path / "decant.Source"      # BP27: inside the one folder
    assert (inner / "console.sh").exists()
    assert (inner / "Open Console.desktop").exists()
    console = (inner / "console.sh").read_text(encoding="utf-8")
    assert "khctl" in console and "console --work-dir" in console
    assert "deploy_plan.json" in console           # refuses an undeployed box
    assert "kh-" not in console                    # no credential, ever
    desktop = (inner / "Open Console.desktop").read_text(encoding="utf-8")
    assert "decant.Source — Operator Console" in desktop
    assert "console.sh" in desktop
    assert any("Open Console.desktop" in l for l in lines)
    # LF endings — a CRLF .sh dies on Ubuntu.
    raw = (inner / "console.sh").read_bytes()
    assert b"\r\n" not in raw


def test_provision_rejects_unknown_role(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    with pytest.raises(ApplyError, match="role must be one of"):
        provision_operator_credential(vault_client(),
                                      settings.bao_kv_mount,
                                      "t", "admin", "tester")


# ---------------------------------------------------------------------------
# BP25/F16 — the agent serving credential has a re-mint path
# ---------------------------------------------------------------------------


def test_provision_agent_cli_remints_a_serving_credential(tmp_path, capsys,
                                                          monkeypatch):
    """F16: losing the print-once agent credential used to mean hand-rolled
    hvac on-site — provision-operator minted only operator/reviewer. Now
    `khctl provision-agent` mints, registers, and resolves a fresh serving
    principal through the same registry path + print-once ceremony."""
    monkeypatch.chdir(tmp_path)                    # dev bench context
    tenant = f"agent-{uuid.uuid4().hex[:10]}"
    rc = deploy_cli.main(["provision-agent", "--tenant", tenant])
    out = capsys.readouterr().out
    assert rc == 0
    (token,) = extract_tokens(out)
    assert "AGENT SERVING credential" in out
    assert "shown once" in out

    principal = OpenBaoCredentialResolver(
        client=vault_client()).resolve_principal(token)
    assert principal.tenant_id == tenant
    assert principal.roles == []                   # serving-only: no console,
    assert principal.principal_id.startswith(f"{tenant}-agent-")  # no writes

    # Attribution rides the registry record; the value never does.
    record = vault_client().secrets.kv.v2.read_secret_version(
        mount_point=settings.bao_kv_mount,
        path=OpenBaoCredentialResolver.path_for(token),
        raise_on_deleted_version=True)["data"]["data"]
    assert record["provisioned_by"]
    assert token not in json.dumps(record)

    # And nothing credential-shaped landed on disk.
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert token not in f.read_text(encoding="utf-8",
                                            errors="ignore"), f


def test_provision_agent_refuses_without_vault_custody(tmp_path, capsys,
                                                       monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"BAO_ADDR={settings.bao_addr}\nBAO_ROOT_TOKEN=wrong-token\n",
        encoding="utf-8")
    rc = deploy_cli.main(["provision-agent", "--tenant", "x",
                          "--work-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "gate working" in out
    assert extract_tokens(out) == []
