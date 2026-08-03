"""Deployment planning logic — pure, no live services.

Everything here drives resolve_plan/qualify/select_tier/render_env with
synthetic probe reports: the walk-in scenarios from §8.9 as table rows.
The probe's live sections are exercised by running `khctl probe` against
the real pilot stack (they are I/O wrappers, not logic).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from knowledge_hub.deploy_probe import (
    EgressReport,
    GpuDevice,
    GpuReport,
    HostReport,
    ObjectStoreReport,
    PostgresReport,
    ProbeReport,
)
from knowledge_hub.deploy_profiles import (
    DeployPlan,
    PlanError,
    load_profiles,
    render_env,
    resolve_plan,
    select_tier,
)

INFRA_DIR = Path(__file__).resolve().parents[2]
PROFILES = load_profiles(INFRA_DIR / "profiles.toml")

PILOT_DEFAULTS = {
    "POSTGRES_USER": "kh", "POSTGRES_PASSWORD": "kh_pilot_pw",
    "POSTGRES_DB": "knowledge_hub", "S3_ENDPOINT": "http://localhost:8333",
    "S3_ACCESS_KEY": "kh_s3_admin", "S3_SECRET_KEY": "kh_s3_secret_pw",
    "S3_RAW_BUCKET": "kh-raw", "BAO_ADDR": "http://localhost:8200",
    "BAO_ROOT_TOKEN": "kh_pilot_root_token",
}


def make_probe(vram: float = 192.0, postgres=None, object_store=None,
               ports_listening=None) -> ProbeReport:
    return ProbeReport(
        probed_at="2026-07-24T00:00:00+00:00",
        host=HostReport(os="Linux 6.8", machine="x86_64", docker=True,
                        docker_compose=True,
                        ports_listening=ports_listening or {}),
        gpu=(GpuReport(present=True,
                       devices=[GpuDevice(name="test-gpu", vram_gb=vram)],
                       vram_gb_total=vram, vram_gb_max_single=vram)
             if vram else GpuReport(present=False, error="none")),
        postgres=postgres or [],
        object_store=object_store or [],
        egress=EgressReport())


def qualified_pg(**overrides) -> PostgresReport:
    fields = dict(
        dsn_redacted="postgresql://svc:***@pg.client.lan:5432/kh",
        reachable=True, server_version="16.4", major_version=16,
        is_superuser=False,
        ext_available={"vector": True, "age": True, "pg_trgm": True},
        ext_installed={"vector": False, "age": False, "pg_trgm": True})
    fields.update(overrides)
    return PostgresReport(**fields)


# ---------------------------------------------------------------------------
# profiles.toml — the shipped kit data is itself under test
# ---------------------------------------------------------------------------
def test_shipped_profiles_load_and_cover_the_three_offerings():
    assert set(PROFILES.presets) == {"appliance", "client-gpu", "hosted"}
    assert PROFILES.presets["appliance"].shape == "A"
    assert PROFILES.presets["hosted"].shape == "B"
    # tiers are consulted highest-first
    floors = [t.vram_gb for t in PROFILES.tiers]
    assert floors == sorted(floors, reverse=True)


def test_secrets_are_ours_in_every_shipped_profile():
    # S2 principal-registry isolation: no preset may offer secrets adoption.
    for preset in PROFILES.presets.values():
        assert preset.seams.get("secrets", "ours") == "ours"


# ---------------------------------------------------------------------------
# Unseal-key custody — per-offering default, per-engagement dial
# ---------------------------------------------------------------------------
def test_shipped_custody_defaults_per_offering():
    assert PROFILES.presets["appliance"].custody == "operator"
    assert PROFILES.presets["client-gpu"].custody == "client"
    assert PROFILES.presets["hosted"].custody == "auto"


def test_no_shape_a_profile_defaults_to_auto_unseal():
    # auto-unseal on premises-local undermines local-first (DEPLOY_NOTES).
    for preset in PROFILES.presets.values():
        if preset.shape == "A":
            assert preset.custody != "auto"


def test_plan_records_custody_default_and_override():
    plan = resolve_plan(PROFILES, "appliance", make_probe())
    assert plan.secrets_custody == "operator" and not plan.custody_overridden
    upgraded = resolve_plan(PROFILES, "appliance", make_probe(),
                            custody="client")
    assert upgraded.secrets_custody == "client" and upgraded.custody_overridden


def test_hosted_plan_records_custody_without_a_secrets_seam():
    plan = resolve_plan(PROFILES, "hosted", make_probe(vram=0),
                        use=["inference=remote:https://infer.example.com"])
    assert "secrets" not in plan.seams
    assert plan.secrets_custody == "auto"


def test_unknown_custody_mode_is_refused():
    with pytest.raises(PlanError, match="custody 'tpm' not recognized"):
        resolve_plan(PROFILES, "appliance", make_probe(), custody="tpm")


# ---------------------------------------------------------------------------
# Tier ladder — the ONE variable (§8.9)
# ---------------------------------------------------------------------------
def test_tier_fp16_selected_when_vram_fits():
    tier = select_tier(PROFILES.tiers, vram_gb_budget=192.0)
    assert tier.name == "fp16_27b"


def test_gated_tier_never_selected_silently():
    # 32 GB fits the quantized floor (30) but that tier is Axis-D-gated.
    assert select_tier(PROFILES.tiers, vram_gb_budget=32.0) is None
    tier = select_tier(PROFILES.tiers, vram_gb_budget=32.0, allow_gated=True)
    assert tier.name == "quant_27b"


def test_scenario_2_fork_needs_a_CONFIRMED_absence_not_a_probe_miss():
    """BP46 Fix 2 — the defect: an undetected GPU walked into a fork whose
    first option is selling a GPU appliance. It fired on node-a, a box
    running a 23GB model on its own iGPU. Detection failure and hardware
    absence are now two different outcomes."""
    with pytest.raises(PlanError) as miss:
        resolve_plan(PROFILES, "appliance", make_probe(vram=0))
    text = str(miss.value)
    assert "NO SUPPORTED GPU DETECTED" in text
    assert "DETECTION result" in text
    # the commercial offer must NOT be in the unconfirmed message
    assert "bring/sell" not in text
    assert "KIT DEFECT" in text          # names the likelier cause first
    assert "--confirm-no-gpu" in text    # and the way to proceed honestly

    # Only an operator-confirmed absence reaches the commercial conversation.
    with pytest.raises(PlanError, match="bring/sell") as confirmed:
        resolve_plan(PROFILES, "appliance", make_probe(vram=0),
                     confirm_no_gpu=True)
    assert "Scenario-2" in str(confirmed.value)


# ---------------------------------------------------------------------------
# Scenario 1 — fully equipped walk-ins
# ---------------------------------------------------------------------------
def test_appliance_all_ours_plan():
    plan = resolve_plan(PROFILES, "appliance", make_probe(),
                        tenants=["ops", "finance"])
    assert plan.shape == "A"
    assert {d.choice for d in plan.seams.values()} == {"ours", "local"}
    assert sorted(plan.compose_services()) == ["openbao", "postgres",
                                               "seaweedfs"]
    assert plan.extraction_tier == "fp16_27b"


def test_qualified_theirs_candidate_forces_operator_call():
    probe = make_probe(postgres=[qualified_pg()])
    with pytest.raises(PlanError, match="operator's call"):
        resolve_plan(PROFILES, "client-gpu", probe)


def test_operator_adopts_their_postgres_with_evidence():
    probe = make_probe(postgres=[qualified_pg()])
    plan = resolve_plan(
        PROFILES, "client-gpu", probe,
        use=["postgres=theirs:postgresql://svc:pw@pg.client.lan:5432/kh"])
    decision = plan.seams["postgres"]
    assert decision.choice == "theirs" and decision.operator_override
    assert all(r.passed for r in decision.qualification)
    # the un-overridden storage seam had no qualified candidate -> ours
    assert plan.seams["object_store"].choice == "ours"


def test_unqualified_postgres_falls_back_to_ours_silently():
    # AGE missing (managed Postgres) -> not qualified -> no operator fork.
    probe = make_probe(postgres=[qualified_pg(
        ext_available={"vector": True, "age": False, "pg_trgm": True})])
    plan = resolve_plan(PROFILES, "client-gpu", probe)
    assert plan.seams["postgres"].choice == "ours"


def test_override_to_unprobed_endpoint_records_unproven():
    plan = resolve_plan(
        PROFILES, "client-gpu", make_probe(),
        use=["postgres=theirs:postgresql://u:p@never-probed:5432/db"])
    [result] = plan.seams["postgres"].qualification
    assert not result.passed and "not probed" in result.rule


def test_object_store_unknown_worm_fails_closed():
    # Reachable store, but no bucket to inspect -> lock/versioning unknown.
    probe = make_probe(object_store=[ObjectStoreReport(
        endpoint="http://minio.client.lan:9000", reachable=True)])
    plan = resolve_plan(PROFILES, "client-gpu", probe)
    assert plan.seams["object_store"].choice == "ours"


# ---------------------------------------------------------------------------
# Shape B — hosted
# ---------------------------------------------------------------------------
def test_hosted_requires_explicit_remote_endpoint():
    with pytest.raises(PlanError, match="remote inference needs an endpoint"):
        resolve_plan(PROFILES, "hosted", make_probe(vram=0))


def test_hosted_plan_has_no_local_tier_and_no_compose_services():
    plan = resolve_plan(PROFILES, "hosted", make_probe(vram=0),
                        use=["inference=remote:https://infer.example.com"])
    assert plan.shape == "B" and plan.footprint == "connector_agent"
    assert plan.extraction_tier is None
    assert plan.compose_services() == []
    assert plan.seams["inference"].endpoint == "https://infer.example.com"


# ---------------------------------------------------------------------------
# Rendering + round-trip — the plan is the config, provably
# ---------------------------------------------------------------------------
def test_render_env_ours_keeps_pilot_defaults():
    plan = resolve_plan(PROFILES, "appliance", make_probe(), tenants=["ops"])
    env = render_env(plan, PILOT_DEFAULTS)
    assert "POSTGRES_HOST" not in env          # ours -> compose default
    assert "EXTRACTION_MODEL=qwen3.6" in env
    # adjudication rides the extraction tag (config.py default-shared) so an
    # exact-tag tier can't leave it pointing at a model the kit lacks
    assert ("ADJUDICATION_MODEL=" + plan.extraction_model) in env
    assert "SERVING_TENANTS=ops" in env


def test_render_env_theirs_postgres_overrides_connection():
    probe = make_probe(postgres=[qualified_pg()])
    plan = resolve_plan(
        PROFILES, "client-gpu", probe,
        use=["postgres=theirs:postgresql://svc:pw@pg.client.lan:5433/khdb"])
    env = render_env(plan, PILOT_DEFAULTS)
    assert "POSTGRES_HOST=pg.client.lan" in env
    assert "POSTGRES_PORT=5433" in env
    assert "POSTGRES_DB=khdb" in env
    assert "POSTGRES_USER=svc" in env


# ---------------------------------------------------------------------------
# BP34 — ours-Postgres host port (the declined-DB port-collision guardrail)
# ---------------------------------------------------------------------------
def test_use_ours_port_records_host_port_and_wins_over_the_gate_flag():
    # The launcher appends postgres=ours:<port> AFTER the gate's
    # postgres=ours — later --use wins, the plan carries the port.
    probe = make_probe(postgres=[qualified_pg()])
    plan = resolve_plan(PROFILES, "client-gpu", probe,
                        use=["postgres=ours", "postgres=ours:5433"])
    decision = plan.seams["postgres"]
    assert decision.choice == "ours" and decision.operator_override
    assert decision.host_port == 5433
    assert decision.compose_service == "postgres"


def test_render_env_ours_postgres_port_default_and_shifted():
    plan = resolve_plan(PROFILES, "appliance", make_probe(),
                        use=["postgres=ours"])
    assert "POSTGRES_PORT=5432" in render_env(plan, PILOT_DEFAULTS)
    probe = make_probe(postgres=[qualified_pg()])
    shifted = resolve_plan(PROFILES, "client-gpu", probe,
                           use=["postgres=ours:5433"])
    env = render_env(shifted, PILOT_DEFAULTS)
    assert "POSTGRES_PORT=5433" in env
    assert "POSTGRES_HOST" not in env          # still OUR loopback stack


def test_render_env_unoverridden_ours_postgres_still_renders_the_port():
    # Compose binds ${POSTGRES_PORT:-5432}; the rendered .env must always
    # carry the key so compose + settings + dsn_from_env agree.
    plan = resolve_plan(PROFILES, "appliance", make_probe())
    assert "POSTGRES_PORT=5432" in render_env(plan, PILOT_DEFAULTS)


def test_use_ours_port_junk_and_wrong_seam_refuse_loudly():
    with pytest.raises(PlanError, match="host\\s+port"):
        resolve_plan(PROFILES, "appliance", make_probe(),
                     use=["postgres=ours:club"])
    with pytest.raises(PlanError, match="takes no extra"):
        resolve_plan(PROFILES, "appliance", make_probe(),
                     use=["object_store=ours:9999"])


def test_compose_binds_the_parameterized_host_port():
    # Lock-step with the plan: the shipped compose must consume the same
    # key render_env writes, or the port shift silently does nothing.
    compose = (INFRA_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    assert '127.0.0.1:${POSTGRES_PORT:-5432}:5432' in compose


def test_render_env_remote_inference_overrides_ollama_host():
    plan = resolve_plan(PROFILES, "hosted", make_probe(vram=0),
                        use=["inference=remote:https://infer.example.com"])
    env = render_env(plan, PILOT_DEFAULTS)
    assert "OLLAMA_HOST=https://infer.example.com" in env


def test_plan_round_trips_through_json():
    probe = make_probe(postgres=[qualified_pg()])
    plan = resolve_plan(
        PROFILES, "client-gpu", probe,
        use=["postgres=theirs:postgresql://svc:pw@pg.client.lan:5432/kh"],
        tenants=["ops"])
    restored = DeployPlan.from_json(plan.to_json())
    assert restored == plan


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------
def test_unknown_profile_and_bad_use_are_actionable():
    with pytest.raises(PlanError, match="unknown profile"):
        resolve_plan(PROFILES, "saas", make_probe())
    with pytest.raises(PlanError, match="unknown seam"):
        resolve_plan(PROFILES, "appliance", make_probe(),
                     use=["database=theirs:x"])
    with pytest.raises(PlanError, match="allows only"):
        # appliance is all-ours; adopting theirs is not a flag away.
        resolve_plan(PROFILES, "appliance", make_probe(),
                     use=["postgres=theirs:postgresql://u:p@h/db"])


def test_probe_report_round_trips_through_json():
    probe = make_probe(postgres=[qualified_pg()])
    restored = ProbeReport.from_json(probe.to_json())
    assert restored == probe


# ---------------------------------------------------------------------------
# Verify — plan-driven check selection (selection logic only; the check
# bodies run live via check_stack.py / khctl verify against the real stack)
# ---------------------------------------------------------------------------
def _check_names(plan) -> list[str]:
    from knowledge_hub.deploy_cli import verify_checks_for
    return [name for name, _ in verify_checks_for(plan)]


def test_shape_a_verify_runs_the_full_pilot_gate_plus_side_doors():
    plan = resolve_plan(PROFILES, "appliance", make_probe(), tenants=["ops"])
    names = _check_names(plan)
    assert names[0] == "version integrity"          # drift fails first
    assert "side doors (§8.8 rider)" in names       # every visit, always
    # every pilot check present: same primitives as check_stack.py
    for expected in ("postgres", "seaweedfs (s3)", "openbao", "ollama",
                     "extraction (ontology·llm·ground)",
                     "resolution (policy·splink·adjudicate)",
                     "benchmark harness", "serving service (S5)"):
        assert expected in names
    assert "remote inference" not in names


def test_shape_b_verify_swaps_gpu_checks_for_the_remote_endpoint():
    plan = resolve_plan(PROFILES, "hosted", make_probe(vram=0),
                        use=["inference=remote:https://infer.example.com"])
    names = _check_names(plan)
    assert names == ["version integrity", "remote inference"]
    # no client-side DB stack in a connector-agent footprint -> no DB
    # checks, no side-door audit target


def test_run_check_reports_instead_of_raising():
    from knowledge_hub.checks import run_check

    ok = run_check("demo", lambda: "demo: fine")
    assert ok.passed and ok.detail == "demo: fine"
    boom = run_check("demo", lambda: (_ for _ in ()).throw(
        RuntimeError("kaput")))
    assert not boom.passed and "kaput" in boom.detail


# ---------------------------------------------------------------------------
# Apply — pure helpers (the wet phases run live via --dry-run / Ubuntu replay)
# ---------------------------------------------------------------------------
def test_env_file_round_trip_and_dsn(tmp_path):
    from knowledge_hub.deploy_apply import dsn_from_env, parse_env_file

    env_file = tmp_path / ".env.deploy"
    env_file.write_text(
        "# comment\n\nPOSTGRES_USER=svc\nPOSTGRES_PASSWORD=p@ss word\n"
        "POSTGRES_HOST=pg.client.lan\nPOSTGRES_PORT=5433\n"
        "POSTGRES_DB=khdb\n", encoding="utf-8")
    env = parse_env_file(env_file)
    assert env["POSTGRES_PASSWORD"] == "p@ss word"
    # credentials must survive URL-hostile characters
    assert dsn_from_env(env) == \
        "postgresql://svc:p%40ss%20word@pg.client.lan:5433/khdb"


def test_kit_manifest_verifies_and_refuses_tamper(tmp_path):
    import json as jsonlib

    from knowledge_hub.deploy_apply import ApplyError, verify_kit_manifest
    import hashlib

    payload = tmp_path / "images" / "kh.tar"
    payload.parent.mkdir()
    payload.write_bytes(b"pretend image")
    digest = hashlib.sha256(b"pretend image").hexdigest()
    (tmp_path / "manifest.json").write_text(jsonlib.dumps(
        {"artifacts": [{"path": "images/kh.tar", "sha256": digest}]}),
        encoding="utf-8")
    lines = verify_kit_manifest(tmp_path)
    assert any("verified images/kh.tar" in line for line in lines)

    payload.write_bytes(b"tampered image")
    with pytest.raises(ApplyError, match="HASH MISMATCH"):
        verify_kit_manifest(tmp_path)


def test_kitless_dir_is_an_honest_dev_install(tmp_path):
    from knowledge_hub.deploy_apply import verify_kit_manifest

    [line] = verify_kit_manifest(tmp_path)
    assert "DEV install" in line


def test_custody_ceremonies_exist_and_auto_is_refused():
    from knowledge_hub.deploy_apply import ApplyError, ceremony_text

    assert "OUR kit custody" in ceremony_text("operator")
    assert "retain ZERO" in ceremony_text("client")
    with pytest.raises(ApplyError, match="Shape-B"):
        ceremony_text("auto")


# ---------------------------------------------------------------------------
# make-kit / verify-kit — the producer to apply's consumer
# ---------------------------------------------------------------------------
def _fake_infra(tmp_path):
    """A minimal build folder satisfying the bundle allowlist, salted with
    exactly the secret-shaped files that must NOT ship."""
    from knowledge_hub.deploy_kit import BUNDLE_FILES

    infra = tmp_path / "infra"
    for rel in BUNDLE_FILES:
        target = infra / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel == "docker-compose.yml":
            # the pilot compose carries a build: key — the kit copy must not
            target.write_text("services:\n  postgres:\n"
                              "    build: ./postgres\n"
                              "    image: kh/postgres:test\n"
                              "  seaweedfs:\n"
                              "    image: kh/seaweed:test\n",
                              encoding="utf-8")
        elif rel == "requirements.lock.txt":
            target.write_text(
                "boto3==1.43.54\n"
                'pywin32==311; sys_platform == "win32"\n'
                "psycopg==3.2.0\n", encoding="utf-8")
        else:
            target.write_text(f"# {rel}\n", encoding="utf-8")
    (infra / "migrations").mkdir()
    (infra / "migrations" / "001_test.sql").write_text("SELECT 1;",
                                                       encoding="utf-8")
    (infra / "ontologies").mkdir()   # d.s Stage 1: ships the baseline set
    (infra / "ontologies" / "baseline-0.1.json").write_text(
        '{"version": "baseline-0.1", "entity_types": ["A"],'
        ' "predicates": ["p"]}', encoding="utf-8")
    pkg = infra / "knowledge_hub_pkg"
    (pkg / "knowledge_hub").mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='x'",
                                        encoding="utf-8")
    (pkg / "knowledge_hub" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "junk.pyc").write_bytes(b"x")
    # the salt: engagement artifacts + secrets lying around the build folder
    (infra / ".env").write_text("BAO_ROOT_TOKEN=supersecret", encoding="utf-8")
    (infra / ".env.deploy").write_text("POSTGRES_PASSWORD=pw", encoding="utf-8")
    (infra / "deploy_plan.json").write_text("{}", encoding="utf-8")
    (infra / "probe_report.json").write_text("{}", encoding="utf-8")
    (infra / "serving_usage.jsonl").write_text("", encoding="utf-8")
    return infra


def _build_kit(tmp_path, **overrides):
    from knowledge_hub.deploy_kit import KitContext, run_make_kit

    infra = _fake_infra(tmp_path)
    out = tmp_path / "kit"
    overrides.setdefault("allow_unsigned", True)  # dev-bench default in tests
    ctx = KitContext(infra_dir=infra, out_dir=out, models=[],
                     skip={"wheelhouse", "python", "images", "models",
                           "tokenizer"},
                     **overrides)
    assert run_make_kit(ctx) == 0
    return out


def test_lockfile_filter_strips_win32_lines_only():
    from knowledge_hub.deploy_kit import filter_lockfile_for_linux

    filtered = filter_lockfile_for_linux(
        "boto3==1.43.54\npywin32==311; sys_platform == \"win32\"\n"
        "psycopg==3.2.0\n")
    assert "pywin32" not in filtered
    assert "boto3==1.43.54" in filtered and "psycopg==3.2.0" in filtered


def test_compose_images_are_derived_not_maintained():
    from knowledge_hub.deploy_kit import compose_images

    images = compose_images(INFRA_DIR)
    assert "knowledge-hub/postgres:16-age-pgvector" in images
    assert any(i.startswith("chrislusf/seaweedfs") for i in images)
    assert any(i.startswith("openbao/openbao") for i in images)


def test_kit_round_trip_builds_verifies_and_excludes_secrets(tmp_path, capsys):
    from knowledge_hub.deploy_kit import verify_kit_strict

    kit = _build_kit(tmp_path)
    out = capsys.readouterr().out
    assert "no secrets / engagement artifacts" in out
    assert "[WARN] UNSIGNED kit" in out
    # the salt never shipped
    for forbidden in (".env", ".env.deploy", "deploy_plan.json",
                      "probe_report.json", "serving_usage.jsonl"):
        assert not (kit / forbidden).exists()
    assert not (kit / "knowledge_hub_pkg" / "__pycache__").exists()
    # arrival gate passes clean — but ONLY with the recorded dev override
    lines = verify_kit_strict(kit, allow_unsigned=True)
    assert any("no unlisted files" in line for line in lines)
    assert any("ACCEPTED via --allow-unsigned" in line for line in lines)
    manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["components"] == {
        "bundle": True, "package": True, "wheelhouse": False,
        "python": False, "images": False, "models": False,
        "tokenizer": False}
    # win32 pins never target the linux kit's lockfile copy... the
    # allowlist ships the original lockfile; bootstrap uses wheelhouse's
    # filtered copy when present
    assert (kit / "requirements.lock.txt").exists()


def test_tampered_kit_artifact_is_refused(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    kit = _build_kit(tmp_path)
    (kit / "check_stack.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="HASH MISMATCH"):
        verify_kit_strict(kit, allow_unsigned=True)


def test_planted_file_breaks_chain_of_custody(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    kit = _build_kit(tmp_path)
    (kit / "extra_payload.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    with pytest.raises(ApplyError, match="NOT in the manifest"):
        verify_kit_strict(kit, allow_unsigned=True)


def test_planted_env_fails_the_no_secrets_guard(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import assert_no_secrets

    kit = _build_kit(tmp_path)
    (kit / ".env").write_text("BAO_ROOT_TOKEN=oops", encoding="utf-8")
    with pytest.raises(ApplyError, match="no secrets"):
        assert_no_secrets(kit)


def test_unmanifested_dir_is_not_a_kit(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    with pytest.raises(ApplyError, match="no manifest.json"):
        verify_kit_strict(tmp_path)


def test_model_resolution_and_blob_hash_verification(tmp_path):
    import hashlib

    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import resolve_model_files

    store = tmp_path / "store"
    blob_content = b"pretend model weights"
    digest = hashlib.sha256(blob_content).hexdigest()
    manifest_dir = store / "manifests" / "registry.ollama.ai" / "library" / "bge-m3"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "latest").write_text(json.dumps({
        "layers": [{"digest": f"sha256:{digest}"}],
        "config": {"digest": f"sha256:{digest}"}}), encoding="utf-8")
    (store / "blobs").mkdir()
    (store / "blobs" / f"sha256-{digest}").write_bytes(blob_content)

    files = resolve_model_files(store, "bge-m3")
    assert len(files) == 3  # manifest + layer blob + config blob (same file)
    with pytest.raises(ApplyError, match="not in local store"):
        resolve_model_files(store, "qwen3.6")


def _fake_model_store(store: Path, models: dict[str, bytes]) -> dict[str, str]:
    """A synthetic ollama store: one manifest + one content-addressed blob
    per model. Returns model -> blob digest."""
    import hashlib

    digests = {}
    (store / "blobs").mkdir(parents=True, exist_ok=True)
    for model, content in models.items():
        name, _, tag = model.partition(":")
        digest = hashlib.sha256(content).hexdigest()
        manifest_dir = (store / "manifests" / "registry.ollama.ai"
                        / "library" / name)
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / (tag or "latest")).write_text(json.dumps(
            {"layers": [{"digest": f"sha256:{digest}"}]}), encoding="utf-8")
        (store / "blobs" / f"sha256-{digest}").write_bytes(content)
        digests[model] = digest
    return digests


def test_restage_with_narrowed_model_set_prunes_stale_files(tmp_path,
                                                            monkeypatch):
    """The BP33 stale-state leak: stage_models converged in place but never
    pruned, so a rebuild that narrowed the model set still carried the old
    model's manifest + blobs — swept into the kit manifest by the tree walk
    and SIGNED, so verify-kit passed an 84GB kit holding two extraction
    models. A re-stage must converge to EXACTLY the pinned set."""
    import types

    from knowledge_hub.deploy_kit import KitContext, stage_models

    monkeypatch.setattr(
        "knowledge_hub.deploy_probe.probe_ollama",
        lambda host: types.SimpleNamespace(reachable=True, version="test",
                                           error=None))
    store = tmp_path / "store"
    digests = _fake_model_store(store, {
        "qwen-test:latest": b"the wrong quant the bench runs",
        "qwen-test:27b-bf16": b"the kit-pinned bf16 weights",
        "bge-m3:latest": b"embedding weights"})
    out = tmp_path / "kit"

    def stage(models):
        ctx = KitContext(infra_dir=tmp_path, out_dir=out, models=models,
                         ollama_store=store)
        return stage_models(ctx)

    # first staging carried the wrong extraction model (the unsafe default)
    stage(["bge-m3:latest", "qwen-test:latest"])
    stale_blob = (out / "ollama_models" / "blobs"
                  / f"sha256-{digests['qwen-test:latest']}")
    stale_manifest = (out / "ollama_models" / "manifests"
                      / "registry.ollama.ai" / "library" / "qwen-test"
                      / "latest")
    assert stale_blob.exists() and stale_manifest.exists()

    # the corrected rebuild narrows the set — stale files must be pruned
    lines = stage(["bge-m3:latest", "qwen-test:27b-bf16"])
    assert not stale_blob.exists()
    assert not stale_manifest.exists()
    assert stale_manifest.parent.exists()  # sibling tag still lives there
    for model in ("qwen-test:27b-bf16", "bge-m3:latest"):
        assert (out / "ollama_models" / "blobs"
                / f"sha256-{digests[model]}").exists()
    assert any("pruned" in line for line in lines)
    # idempotence: a clean re-stage has nothing left to prune
    assert not any("pruned" in line
                   for line in stage(["bge-m3:latest", "qwen-test:27b-bf16"]))


def test_default_kit_models_use_kit_data_not_pilot_settings():
    """The BP33 unsafe default: settings.extraction_model is the bench's
    RUNTIME name ("qwen3.6" -> :latest, a different digest than the kit
    pin). A defaulted make-kit must pin extraction from profiles.toml
    tier data instead, and a gated tier never defaults in."""
    from knowledge_hub.config import settings
    from knowledge_hub.deploy_kit import default_kit_models

    models = default_kit_models(INFRA_DIR)
    assert models[0] == settings.embedding_model
    assert "qwen3.6:27b-bf16" in models              # the default tier pin
    assert settings.extraction_model not in models   # the :latest trap
    assert "qwen3.6:27b-q8_0" not in models          # gated: --models only
    # BP46 Fix 4: a kit that can deploy on EITHER backend has to carry both
    # extraction models — the AMD box's MoE tier is default-status, so a
    # defaulted build pins it too (+23GB, deliberate and visible).
    assert "qwen3.6:35b-a3b-q4_K_M" in models


def test_default_kit_models_fail_closed_without_usable_profiles(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import default_kit_models

    with pytest.raises(ApplyError, match="--models"):
        default_kit_models(tmp_path)  # no profiles.toml at all
    (tmp_path / "profiles.toml").write_text(
        '[tiers.quant]\nextraction_model = "q"\nvram_gb = 30\n'
        'status = "gated"\n\n[profiles.x]\nshape = "A"\nseams = { }\n',
        encoding="utf-8")
    with pytest.raises(ApplyError, match="no default-status tier"):
        default_kit_models(tmp_path)


def test_defaulted_make_kit_resolves_models_from_kit_data(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """khctl make-kit without --models must build the profile-pinned set
    (and say so) — never the pilot bench's runtime extraction setting."""
    from knowledge_hub import deploy_cli
    from knowledge_hub.config import settings

    captured = {}
    monkeypatch.setattr(
        "knowledge_hub.deploy_kit.run_make_kit",
        lambda ctx: captured.update(models=ctx.models) or 0)
    rc = deploy_cli.main(["make-kit", "--out", str(tmp_path / "kit"),
                          "--infra-dir", str(INFRA_DIR),
                          "--allow-unsigned"])
    assert rc == 0
    assert captured["models"] == [settings.embedding_model,
                                  "qwen3.6:27b-bf16",
                                  "qwen3.6:35b-a3b-q4_K_M"]
    assert "defaulted from profiles.toml" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Kit signing — the trust anchor and the attack matrix (throwaway test keys
# only; the org key is minted by the operator per the DEPLOY_NOTES ceremony)
# ---------------------------------------------------------------------------
import shutil as _shutil
import subprocess as _subprocess

needs_minisign = pytest.mark.skipif(_shutil.which("minisign") is None,
                                    reason="minisign not installed")


def _keypair(tmp_path, name):
    pub, sec = tmp_path / f"{name}.pub", tmp_path / f"{name}.key"
    _subprocess.run(["minisign", "-G", "-W", "-p", str(pub), "-s", str(sec)],
                    check=True, capture_output=True, timeout=60)
    pubkey = pub.read_text(encoding="utf-8").strip().splitlines()[-1]
    return pubkey, sec


@needs_minisign
def test_signed_kit_happy_path(tmp_path, monkeypatch, capsys):
    from knowledge_hub import deploy_kit
    from knowledge_hub.deploy_kit import verify_kit_strict

    pubkey, seckey = _keypair(tmp_path, "trusted")
    monkeypatch.setattr(deploy_kit, "TRUSTED_PUBKEYS", {"test-key": pubkey})
    kit = _build_kit(tmp_path, sign_key=seckey, allow_unsigned=False)
    assert "signed + self-verified against trusted key 'test-key'" in \
        capsys.readouterr().out
    lines = verify_kit_strict(kit)          # NO override needed when signed
    assert any("signature verified against trusted key 'test-key'" in line
               for line in lines)


def test_unsigned_build_is_refused_without_override(tmp_path, capsys):
    from knowledge_hub.deploy_kit import KitContext, run_make_kit

    infra = _fake_infra(tmp_path)
    ctx = KitContext(infra_dir=infra, out_dir=tmp_path / "kit", models=[],
                     skip={"wheelhouse", "images", "models"})
    assert run_make_kit(ctx) == 1
    assert "REQUIRES a signature" in capsys.readouterr().out


def test_unsigned_kit_is_refused_at_arrival_without_override(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    kit = _build_kit(tmp_path)  # allow_unsigned build
    with pytest.raises(ApplyError, match="UNSIGNED"):
        verify_kit_strict(kit)


@needs_minisign
def test_tampered_manifest_fails_signature_first(tmp_path, monkeypatch):
    from knowledge_hub import deploy_kit
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    pubkey, seckey = _keypair(tmp_path, "trusted")
    monkeypatch.setattr(deploy_kit, "TRUSTED_PUBKEYS", {"test-key": pubkey})
    kit = _build_kit(tmp_path, sign_key=seckey, allow_unsigned=False)
    manifest = kit / "manifest.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            '"kit_format": 1', '"kit_format": 2'), encoding="utf-8")
    # the hashes inside would still all pass — the SIGNATURE is what dies
    with pytest.raises(ApplyError, match="does NOT verify"):
        verify_kit_strict(kit)


@needs_minisign
def test_valid_signature_from_untrusted_key_is_refused(tmp_path, monkeypatch):
    from knowledge_hub import deploy_kit
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    trusted_pub, _ = _keypair(tmp_path, "trusted")
    _, stranger_sec = _keypair(tmp_path, "stranger")
    monkeypatch.setattr(deploy_kit, "TRUSTED_PUBKEYS",
                        {"test-key": trusted_pub})
    # build unsigned, then the stranger signs it VALIDLY with their own key
    kit = _build_kit(tmp_path)
    _subprocess.run(["minisign", "-Sm", str(kit / "manifest.json"),
                     "-s", str(stranger_sec)],
                    check=True, capture_output=True, timeout=60)
    with pytest.raises(ApplyError, match="NO.*trusted key|does NOT verify"):
        verify_kit_strict(kit)


@needs_minisign
def test_swapped_pubkey_attack_is_refused(tmp_path, monkeypatch):
    """THE circular-trust test: attacker repacks the kit, re-signs with
    their own key, and helpfully includes their own public key in the kit.
    The verifier must use ITS OWN embedded anchor — never a key from the
    kit — so the attack dies at the signature step."""
    from knowledge_hub import deploy_kit
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import verify_kit_strict

    trusted_pub, trusted_sec = _keypair(tmp_path, "trusted")
    attacker_pub, attacker_sec = _keypair(tmp_path, "attacker")
    monkeypatch.setattr(deploy_kit, "TRUSTED_PUBKEYS",
                        {"test-key": trusted_pub})
    kit = _build_kit(tmp_path, sign_key=trusted_sec, allow_unsigned=False)

    # the attack: swap a payload, drop THEIR pubkey in, re-hash, re-sign
    (kit / "check_stack.py").write_text("# malicious\n", encoding="utf-8")
    (kit / "minisign.pub").write_text(
        f"untrusted comment: attacker key\n{attacker_pub}\n",
        encoding="utf-8")
    manifest = json.loads((kit / "manifest.json").read_text(
        encoding="utf-8"))
    import hashlib as _hashlib
    for artifact in manifest["artifacts"]:
        artifact["sha256"] = _hashlib.sha256(
            (kit / artifact["path"]).read_bytes()).hexdigest()
    manifest["artifacts"].append({
        "path": "minisign.pub",
        "sha256": _hashlib.sha256(
            (kit / "minisign.pub").read_bytes()).hexdigest(),
        "bytes": (kit / "minisign.pub").stat().st_size})
    (kit / "manifest.json").write_text(json.dumps(manifest, indent=2),
                                       encoding="utf-8")
    _subprocess.run(["minisign", "-Sm", str(kit / "manifest.json"),
                     "-s", str(attacker_sec)],
                    check=True, capture_output=True, timeout=60)
    # internally consistent, validly signed — and still DEAD at step 1
    with pytest.raises(ApplyError, match="NO.*trusted key|does NOT verify"):
        verify_kit_strict(kit)


def test_secret_signing_key_cannot_be_kitted(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import assert_no_secrets

    kit = _build_kit(tmp_path)
    (kit / "minisign.key").write_text("SECRET", encoding="utf-8")
    with pytest.raises(ApplyError, match="no secrets"):
        assert_no_secrets(kit)


def test_apply_consumes_a_built_kit(tmp_path, capsys):
    """The producer->consumer round trip: apply's phase_kit verifies the
    freshly built manifest artifact-by-artifact."""
    from knowledge_hub.deploy_apply import ApplyContext, run_apply
    kit = _build_kit(tmp_path)
    plan = resolve_plan(PROFILES, "appliance", make_probe(), tenants=["ops"])
    env_file = tmp_path / ".env.deploy"
    env_file.write_text(render_env(plan, PILOT_DEFAULTS), encoding="utf-8")
    rc = run_apply(ApplyContext(plan=plan, infra_dir=kit, kit_dir=kit,
                                env_file=env_file, dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "verified check_stack.py" in out          # manifest consumed
    assert "DEV install" not in out                  # a real kit, not kitless


# ---------------------------------------------------------------------------
# BP30 (BP28 #10/#19/#20/#21) — the kit ships what a CLEAN box needs
# ---------------------------------------------------------------------------
def test_kit_compose_ships_without_build_keys(tmp_path):
    kit = _build_kit(tmp_path)
    staged = (kit / "docker-compose.yml").read_text(encoding="utf-8")
    assert "build:" not in staged
    # every image: line survives — compose runs the LOADED images
    assert "image: kh/postgres:test" in staged
    assert "image: kh/seaweed:test" in staged


def test_stage_bundle_refuses_a_multiline_build_block(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import strip_build_keys

    with pytest.raises(ApplyError, match="build"):
        strip_build_keys("services:\n  postgres:\n    build:\n"
                         "      context: ./postgres\n")


def test_kit_ships_the_postgres_init_script(tmp_path):
    kit = _build_kit(tmp_path)
    init = kit / "postgres" / "init" / "00-extensions.sql"
    assert init.exists()
    manifest = json.loads((kit / "manifest.json").read_text(encoding="utf-8"))
    assert any(a["path"] == "postgres/init/00-extensions.sql"
               for a in manifest["artifacts"])


def test_s3config_is_forbidden_in_kits(tmp_path):
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import assert_no_secrets

    kit = _build_kit(tmp_path)
    (kit / "seaweedfs").mkdir()
    (kit / "seaweedfs" / "s3config.json").write_text(
        '{"identities": []}', encoding="utf-8")
    with pytest.raises(ApplyError, match="no secrets"):
        assert_no_secrets(kit)


def test_stage_tokenizer_bundles_and_pins(tmp_path, monkeypatch):
    from knowledge_hub import deploy_kit
    from knowledge_hub.deploy_apply import ApplyError
    from knowledge_hub.deploy_kit import KitContext, stage_tokenizer

    src = tmp_path / "cached_tokenizer.json"
    src.write_text('{"model": "fake"}', encoding="utf-8")
    monkeypatch.setattr(deploy_kit, "_resolve_tokenizer_source", lambda: src)
    monkeypatch.setattr(deploy_kit, "_smoke_load_tokenizer", lambda p: None)
    out = tmp_path / "kit"
    ctx = KitContext(infra_dir=tmp_path, out_dir=out, models=[])
    lines = stage_tokenizer(ctx)
    shipped = out / "tokenizer" / "bge-m3" / "tokenizer.json"
    assert shipped.exists()
    assert ctx.pins["tokenizer"]["repo"] == "BAAI/bge-m3"
    assert ctx.components["tokenizer"] is True
    assert any("zero egress" in line for line in lines)
    # resolver failure fails the BUILD, not a client site
    def _boom():
        raise OSError("no cache, no egress")
    monkeypatch.setattr(deploy_kit, "_resolve_tokenizer_source", _boom)
    with pytest.raises(ApplyError, match="MUST ship"):
        stage_tokenizer(KitContext(infra_dir=tmp_path,
                                   out_dir=tmp_path / "kit2", models=[]))


def test_render_env_mints_unique_s3_credentials():
    plan = resolve_plan(PROFILES, "appliance", make_probe(), tenants=["ops"])
    first = dict(line.split("=", 1) for line in
                 render_env(plan, PILOT_DEFAULTS).splitlines()
                 if "=" in line and not line.startswith("#"))
    second = dict(line.split("=", 1) for line in
                  render_env(plan, PILOT_DEFAULTS).splitlines()
                  if "=" in line and not line.startswith("#"))
    # per-deploy mint: never the committed pilot pair, never repeated
    assert first["S3_ACCESS_KEY"] != "kh_s3_admin"
    assert first["S3_SECRET_KEY"] != "kh_s3_secret_pw"
    assert first["S3_ACCESS_KEY"] != second["S3_ACCESS_KEY"]
    assert first["S3_SECRET_KEY"] != second["S3_SECRET_KEY"]
    # everything else still renders from the defaults
    assert first["POSTGRES_USER"] == "kh"
    assert first["S3_ENDPOINT"] == "http://localhost:8333"


def test_render_env_theirs_object_store_keeps_its_endpoint():
    plan = resolve_plan(PROFILES, "appliance", make_probe(), tenants=["ops"])
    plan.seams["object_store"] = plan.seams["object_store"].model_copy(
        update={"choice": "theirs", "compose_service": None,
                "endpoint": "http://minio.client.lan:9000"})
    env = dict(line.split("=", 1) for line in
               render_env(plan, PILOT_DEFAULTS).splitlines()
               if "=" in line and not line.startswith("#"))
    assert env["S3_ENDPOINT"] == "http://minio.client.lan:9000"
    # adopted store: their creds are their business — no mint, defaults ride
    assert env["S3_ACCESS_KEY"] == "kh_s3_admin"


def test_compose_publishes_loopback_only():
    import re as _re

    text = (INFRA_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    mappings = [m.group(1) for m in
                _re.finditer(r'^\s*-\s*"([^"]+)"', text, _re.MULTILINE)
                if m.group(1).count(":") >= 1 and
                m.group(1).split(":")[-1].isdigit()]
    assert mappings, "no port mappings found — wrong compose?"
    for mapping in mappings:
        assert mapping.startswith("127.0.0.1:"), \
            f"port mapping {mapping!r} is not loopback-scoped (BP28 #21)"


def test_dry_run_walks_every_phase_and_touches_nothing(tmp_path, capsys):
    from knowledge_hub.deploy_apply import ApplyContext, run_apply
    from knowledge_hub.deploy_cli import PILOT_ENV_DEFAULTS

    plan = resolve_plan(PROFILES, "appliance", make_probe(),
                        tenants=["ops"])
    env_file = tmp_path / ".env.deploy"
    env_file.write_text(render_env(plan, PILOT_DEFAULTS), encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    rc = run_apply(ApplyContext(plan=plan, infra_dir=tmp_path,
                                kit_dir=tmp_path, env_file=env_file,
                                dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY RUN" in out and "dry run complete" in out
    # every phase reported, nothing executed
    for phase in ("kit verification", "preflight", "env install", "services",
                  "schema + migrations", "openbao bootstrap", "model store",
                  "tenant bootstrap"):
        assert phase in out
    assert not (tmp_path / ".env").exists()          # env not installed
    assert "docker-compose.openbao-prod.yml" in out  # prod vault declared
