"""Profile presets, qualification rules, and deployment-plan resolution (§8.9).

The pipeline is:  probe_report.json × profiles.toml × operator choices
                  → deploy_plan.json + .env.deploy

Profiles are DATA (profiles.toml in the kit), not code branches — the
`--profile` flag names the commercial offering (appliance | client-gpu |
hosted); the preset resolves it to a shape (A/B) plus per-seam defaults.
Adding an offering = adding a preset table, never a codepath.

Resolution rules the operator can rely on:
  * A seam preset of "ours" or "theirs" is fixed; "ours|theirs" means BOTH
    are viable and the call is the operator's — if a qualified "theirs"
    candidate exists, plan REFUSES to guess and demands an explicit
    --use seam=… (probe recommends, operator confirms; adopting a client's
    Postgres is a commitment, not a default).
  * Qualification is fail-closed: a rule that cannot be evaluated from the
    probe report (unknown) FAILS. --use seam=theirs:… overrides a failed
    qualification but the plan records the override + the failed rules, and
    verify still has to prove the component live.
  * Inference tiers come from the [tiers] ladder: highest tier whose VRAM
    floor fits the probed GPU BUDGET wins, among tiers valid for that box's
    memory class (dedicated VRAM vs a unified-memory APU — BP46). A tier
    with status="gated" (quantized — quality cost unmeasured until Axis D)
    needs --allow-gated-tier. A recognized GPU with no fitting tier -> the
    Scenario-2 fork is surfaced as an error listing the three options; the
    installer never silently downgrades extraction.
  * NO RECOGNIZED GPU is a different thing entirely and stops with a
    different error (BP46 Fix 2): a detection miss must never escalate into
    an offer to sell hardware, so `plan` refuses and waits for an operator
    who has confirmed, with their own eyes, that the box has no GPU.
"""
from __future__ import annotations

import json
import secrets as pysecrets
import tomllib
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

SEAMS = ("postgres", "object_store", "secrets", "inference")

# Inference seam vocabulary (BP46 Fix 5). The third value exists because
# "local" and "remote" could not describe an in-chassis GPU we did not
# provision, and forcing such a box to "remote" labels an on-premises deploy
# as off-premises — a lie a client security review will find:
#   local           we install and manage the model on this box
#   local-external  inference on a LOCAL endpoint the operator supplied; the
#                   model is NOT installed by us; text does NOT leave the box
#   remote          inference over a tunnel; text LEAVES the premises
INFERENCE_MODES = ("local", "local-external", "remote")
# Choices whose text never leaves the box — the honest basis for any
# data-locality claim in a plan, a log, or an audit answer.
ON_PREMISES_INFERENCE = ("local", "local-external")

# Compose service each "ours" seam brings up (docker-compose.yml names).
OURS_COMPOSE = {"postgres": "postgres", "object_store": "seaweedfs",
                "secrets": "openbao"}


class PlanError(Exception):
    """Resolution cannot proceed — the message tells the operator exactly
    which flag or fork resolves it."""


# ---------------------------------------------------------------------------
# profiles.toml shapes
# ---------------------------------------------------------------------------
class ModelTier(BaseModel):
    name: str
    extraction_model: str
    vram_gb: float
    status: Literal["default", "gated"] = "default"
    # Which GPU memory topology this tier is valid on (BP46 Fix 4).
    #   dedicated  needs real VRAM. Dense models belong here: they are
    #              memory-BANDWIDTH-bound on a unified pool (§8.26b item 1 —
    #              a dense 27B reads ~16GB per token, ceiling ~15 tok/s on
    #              Strix Halo's ~256GB/s; 12.7 measured), so "it fits" is
    #              not the same as "it works".
    #   unified    the tier FOR a unified-memory APU (MoE: ~3B params active
    #              per token, 58.8 tok/s measured on the same box).
    #   any        no topology constraint.
    # Keeping the dense tiers "dedicated" is also what makes the NVIDIA
    # ladder provably unchanged by this build.
    memory: Literal["any", "dedicated", "unified"] = "any"


CUSTODY_MODES = ("operator", "client", "auto")


class ProfilePreset(BaseModel):
    name: str
    shape: Literal["A", "B"]
    seams: dict[str, str]                     # seam -> ours|theirs|ours|theirs|local|remote
    qualify: dict[str, list[str]] = Field(default_factory=dict)
    placement: str = "single_box"
    footprint: Optional[str] = None           # hosted: "connector_agent"
    # Unseal-key custody for the production OpenBao (DEPLOY_NOTES.md):
    # operator = we hold the shares; client = sealed-envelope ceremony,
    # we retain zero; auto = KMS auto-unseal (Shape B / our infra).
    # A per-offering DEFAULT — the per-engagement dial is --custody.
    custody: Literal["operator", "client", "auto"] = "operator"


class KitConfig(BaseModel):
    egress_targets: list[str] = Field(default_factory=list)


class Profiles(BaseModel):
    tiers: list[ModelTier]                     # kept in VRAM-descending order
    presets: dict[str, ProfilePreset]
    kit: KitConfig = Field(default_factory=KitConfig)


def load_profiles(path: Path) -> Profiles:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tiers = [ModelTier(name=name, **tier)
             for name, tier in data.get("tiers", {}).items()]
    tiers.sort(key=lambda t: t.vram_gb, reverse=True)
    presets = {name: ProfilePreset(name=name, **preset)
               for name, preset in data.get("profiles", {}).items()}
    if not tiers:
        raise PlanError(f"{path}: no [tiers] defined")
    if not presets:
        raise PlanError(f"{path}: no [profiles] defined")
    return Profiles(tiers=tiers, presets=presets,
                    kit=KitConfig(**data.get("kit", {})))


# ---------------------------------------------------------------------------
# Qualification — judging probe evidence against a preset's bar
# ---------------------------------------------------------------------------
class RuleResult(BaseModel):
    rule: str
    passed: bool
    evidence: str


def _eval_postgres_rule(rule: str, pg) -> RuleResult:
    if rule.startswith("version>="):
        floor = int(rule.split(">=")[1])
        ok = pg.major_version is not None and pg.major_version >= floor
        return RuleResult(rule=rule, passed=ok,
                          evidence=f"server_version={pg.server_version}")
    if rule.startswith("ext:"):
        ext = rule.split(":", 1)[1]
        ok = pg.ext_available.get(ext, False)
        state = ("installed" if pg.ext_installed.get(ext) else
                 "available" if ok else "MISSING")
        return RuleResult(rule=rule, passed=ok, evidence=f"{ext}: {state}")
    return RuleResult(rule=rule, passed=False,
                      evidence="unknown postgres rule (fail-closed)")


def _eval_object_store_rule(rule: str, s3) -> RuleResult:
    if rule in ("object_lock", "versioning"):
        value = getattr(s3, rule)
        # None = probe could not read it (no bucket) -> unknown -> fail.
        return RuleResult(rule=rule, passed=value is True,
                          evidence=f"{rule}={'unknown' if value is None else value}")
    return RuleResult(rule=rule, passed=False,
                      evidence="unknown object_store rule (fail-closed)")


def qualify_candidate(seam: str, rules: list[str], candidate) -> list[RuleResult]:
    if seam == "postgres":
        if not candidate.reachable:
            return [RuleResult(rule="reachable", passed=False,
                               evidence=candidate.error or "unreachable")]
        return [_eval_postgres_rule(r, candidate) for r in rules]
    if seam == "object_store":
        if not candidate.reachable:
            return [RuleResult(rule="reachable", passed=False,
                               evidence=candidate.error or "unreachable")]
        return [_eval_object_store_rule(r, candidate) for r in rules]
    return [RuleResult(rule="(no rules)", passed=False,
                       evidence=f"no qualification defined for seam {seam!r}")]


# ---------------------------------------------------------------------------
# The deployment plan — the engagement record's third artifact
# ---------------------------------------------------------------------------
class SeamDecision(BaseModel):
    seam: str
    # ours | theirs (storage seams)
    # local | local-external | remote (inference; see INFERENCE_MODES)
    choice: str
    endpoint: Optional[str] = None             # DSN / URL when theirs/remote
    compose_service: Optional[str] = None      # when ours
    # BP34: the HOST port an "ours" postgres binds (loopback). None = the
    # default 5432. Plan data because a DECLINED co-resident client Postgres
    # may already hold 5432 — ours moves, theirs is never contested.
    host_port: Optional[int] = None
    qualification: list[RuleResult] = Field(default_factory=list)
    operator_override: bool = False


class DeployPlan(BaseModel):
    plan_version: str = "1"
    profile: str
    shape: Literal["A", "B"]
    placement: str
    footprint: Optional[str] = None
    # Plan-level (not on the secrets SeamDecision) because hosted plans have
    # no client-side secrets seam yet custody still needs recording — the
    # vault lives on our side there.
    secrets_custody: Literal["operator", "client", "auto"] = "operator"
    custody_overridden: bool = False
    seams: dict[str, SeamDecision]
    extraction_tier: Optional[str] = None      # None for remote inference
    extraction_model: Optional[str] = None
    tenants: list[str] = Field(default_factory=list)
    probe_file: Optional[str] = None

    def compose_services(self) -> list[str]:
        return [d.compose_service for d in self.seams.values()
                if d.compose_service]

    def inference_choice(self) -> Optional[str]:
        seam = self.seams.get("inference")
        return seam.choice if seam else None

    def text_leaves_premises(self) -> bool:
        """The one question a client security review actually asks. Answered
        from the seam value, so the answer cannot drift from the deploy
        (BP46 Fix 5: local-external is on-premises and must never be
        collapsed into 'remote' just because it carries an endpoint)."""
        choice = self.inference_choice()
        return choice is not None and choice not in ON_PREMISES_INFERENCE

    def data_locality(self) -> str:
        """One line, printable, honest — for the plan summary and the .env
        (INFERENCE_SEAM), which is what logs and audit answers read."""
        choice = self.inference_choice()
        if choice is None:
            return "no inference seam in this plan"
        if choice == "local":
            return ("ON PREMISES: inference on this box, model installed "
                    "and managed by us; text never leaves the box")
        if choice == "local-external":
            return ("ON PREMISES: inference on an operator-supplied LOCAL "
                    "endpoint; the model is NOT installed by us; text never "
                    "leaves the box")
        return ("OFF PREMISES: inference over a tunnel to a remote "
                "endpoint; client text LEAVES the premises")

    def to_json(self) -> str:
        return json.dumps(self.model_dump(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "DeployPlan":
        return cls.model_validate(json.loads(text))


def select_tier(tiers: list[ModelTier], vram_gb_budget: float,
                memory_class: str = "dedicated",
                allow_gated: bool = False) -> ModelTier | None:
    """Highest tier whose floor fits the GPU BUDGET, among tiers valid for
    this box's memory class.

    The budget is a total, not a per-device number: Ollama splits layers
    across devices, so a multi-GPU box is one budget. On a unified-memory
    APU the budget includes the GTT/shared pool (probe_gpu, BP46 Fix 1) —
    without that, a box that visibly runs a 23GB model fails every floor on
    the strength of a 2GB dedicated carve-out."""
    for tier in tiers:  # already VRAM-descending
        if tier.status == "gated" and not allow_gated:
            continue
        if tier.memory != "any" and tier.memory != memory_class:
            continue
        if vram_gb_budget >= tier.vram_gb:
            return tier
    return None


SCENARIO_2_OPTIONS = (
    "no local inference tier fits this GPU budget — the Scenario-2 fork, "
    "and it is a commercial call, not the installer's:\n"
    "  (a) bring/sell a GPU deployment appliance (plug-in inference box)\n"
    "  (b) deploy the quantized tier on their hardware "
    "(--allow-gated-tier; quality cost unmeasured until Axis D)\n"
    "  (c) client provisions GPU (re-probe afterwards)")

# BP46 Fix 2 — THE dangerous half of the AMD blocker. Before this build,
# `gpu: NONE` walked straight into SCENARIO_2_OPTIONS, so a probe that could
# not SEE a working GPU offered to sell the client a GPU appliance. That
# happened on node-a: a box running a 23GB model on its own iGPU at 58.8
# tok/s was told to buy inference hardware. A detection miss must never
# reach a commercial recommendation, so this is a full stop instead: no
# tier, no fork, no offer — until a human confirms the absence.
NO_GPU_DETECTED = (
    "NO SUPPORTED GPU DETECTED. Stopping, and deliberately NOT offering a "
    "hardware fork.\n"
    "  This is a DETECTION result, not a verdict about this box. The probe "
    "looks for\n"
    "  nvidia-smi and for amdgpu under /sys/class/drm (plus rocm-smi when "
    "installed);\n"
    "  a GPU it cannot see through one of those is invisible to it, not "
    "absent.\n"
    "  Two very different situations, and only an operator can tell them "
    "apart:\n"
    "    (1) the box HAS a GPU the probe missed -> that is a KIT DEFECT. "
    "Capture the\n"
    "        probe error above plus `ollama ps` / the ollama log, and fix "
    "detection.\n"
    "        Do NOT recommend hardware to a box that already has working "
    "hardware.\n"
    "    (2) the box genuinely has NO GPU -> confirm it explicitly with "
    "--confirm-no-gpu\n"
    "        and re-run; the Scenario-2 options are then surfaced as the "
    "commercial\n"
    "        conversation they are. Alternatively plan inference elsewhere:\n"
    "        --use inference=local-external:<local-endpoint>  (their GPU/"
    "runtime, on-prem)\n"
    "        --use inference=remote:<https-url>               (our GPUs; "
    "text leaves)")

LOCAL_EXTERNAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0")


def _parse_ours_port(seam: str, extra: Optional[str]) -> Optional[int]:
    """BP34: `--use postgres=ours:<host_port>` rides the existing override
    grammar — the extra after `ours:` is the loopback HOST port our own
    Postgres binds (a declined client Postgres may hold 5432). Only the
    postgres seam takes one; anything non-numeric refuses loudly."""
    if extra is None:
        return None
    if seam != "postgres":
        raise PlanError(f"--use {seam}=ours takes no extra — only "
                        f"postgres=ours:<host_port> does")
    if not extra.isdigit() or not 1024 <= int(extra) <= 65535:
        raise PlanError(f"--use postgres=ours:{extra}: expected a host "
                        f"port number 1024-65535")
    return int(extra)


def _parse_use(overrides: list[str]) -> dict[str, tuple[str, Optional[str]]]:
    """--use seam=choice[:endpoint] -> {seam: (choice, endpoint)}."""
    parsed: dict[str, tuple[str, Optional[str]]] = {}
    for item in overrides:
        if "=" not in item:
            raise PlanError(f"--use {item!r}: expected seam=choice[:endpoint]")
        seam, _, value = item.partition("=")
        if seam not in SEAMS:
            raise PlanError(f"--use {item!r}: unknown seam {seam!r} "
                            f"(seams: {', '.join(SEAMS)})")
        choice, _, endpoint = value.partition(":")
        parsed[seam] = (choice, endpoint or None)
    return parsed


def resolve_plan(profiles: Profiles, profile_name: str, probe,
                 use: list[str] | None = None,
                 tenants: list[str] | None = None,
                 allow_gated_tier: bool = False,
                 custody: str | None = None,
                 probe_file: str | None = None,
                 confirm_no_gpu: bool = False) -> DeployPlan:
    if profile_name not in profiles.presets:
        raise PlanError(f"unknown profile {profile_name!r} "
                        f"(have: {', '.join(sorted(profiles.presets))})")
    preset = profiles.presets[profile_name]
    if custody is not None and custody not in CUSTODY_MODES:
        raise PlanError(f"custody {custody!r} not recognized "
                        f"({' | '.join(CUSTODY_MODES)})")
    overrides = _parse_use(use or [])
    decisions: dict[str, SeamDecision] = {}

    # --- storage/secrets seams -------------------------------------------
    candidates = {"postgres": probe.postgres, "object_store": probe.object_store}
    for seam in ("postgres", "object_store", "secrets"):
        if seam not in preset.seams:
            continue
        allowed = preset.seams[seam]
        rules = preset.qualify.get(seam, [])
        override = overrides.get(seam)

        if override:
            choice, endpoint = override
            if choice not in allowed.split("|"):
                raise PlanError(f"--use {seam}={choice}: profile "
                                f"{profile_name!r} allows only {allowed!r}")
            if choice == "theirs":
                if not endpoint:
                    raise PlanError(f"--use {seam}=theirs needs an endpoint: "
                                    f"{seam}=theirs:<dsn-or-url>")
                qual = _qualify_endpoint(seam, rules, endpoint,
                                         candidates.get(seam, []))
                decisions[seam] = SeamDecision(
                    seam=seam, choice="theirs", endpoint=endpoint,
                    qualification=qual, operator_override=True)
            else:
                decisions[seam] = SeamDecision(
                    seam=seam, choice="ours",
                    compose_service=OURS_COMPOSE[seam],
                    operator_override=True,
                    host_port=_parse_ours_port(seam, endpoint))
            continue

        if allowed == "ours":
            decisions[seam] = SeamDecision(
                seam=seam, choice="ours", compose_service=OURS_COMPOSE[seam])
        elif allowed == "theirs":
            raise PlanError(f"seam {seam!r} is theirs-only in profile "
                            f"{profile_name!r}; pass --use "
                            f"{seam}=theirs:<endpoint>")
        else:  # "ours|theirs" — refuse to guess when a qualified theirs exists
            qualified = [c for c in candidates.get(seam, [])
                         if c.reachable and all(
                             r.passed for r in qualify_candidate(seam, rules, c))]
            if qualified:
                names = ", ".join(getattr(c, "dsn_redacted", None)
                                  or c.endpoint for c in qualified)
                raise PlanError(
                    f"seam {seam!r}: qualified client-side candidate(s) found "
                    f"({names}) and the profile allows either — this is the "
                    f"operator's call. Re-run with --use {seam}=theirs:"
                    f"<endpoint> or --use {seam}=ours")
            decisions[seam] = SeamDecision(
                seam=seam, choice="ours", compose_service=OURS_COMPOSE[seam])

    # --- inference — THE variable (§8.9) ----------------------------------
    plan_tier: ModelTier | None = None
    inference_mode = preset.seams.get("inference", "local")
    override = overrides.get("inference")
    if override:
        inference_mode = override[0]
    if inference_mode == "remote":
        endpoint = (override[1] if override else None)
        if not endpoint:
            raise PlanError("remote inference needs an endpoint: "
                            "--use inference=remote:<https-url>")
        decisions["inference"] = SeamDecision(
            seam="inference", choice="remote", endpoint=endpoint,
            operator_override=bool(override))
    elif inference_mode == "local-external":
        # BP46 Fix 5: their GPU/runtime, in their chassis, on premises. We
        # install no model here, so there is no tier and no VRAM judgement
        # to make — but the deploy is emphatically NOT off-premises, and
        # nothing may record it as "remote".
        endpoint = (override[1] if override else None)
        if not endpoint:
            raise PlanError(
                "local-external inference needs the operator-supplied local "
                "endpoint: --use inference=local-external:<url> (e.g. "
                "http://localhost:11434)")
        host = (urlsplit(endpoint).hostname or "").lower()
        if host not in LOCAL_EXTERNAL_HOSTS:
            raise PlanError(
                f"--use inference=local-external:{endpoint}: host {host!r} "
                f"is not on this box ({' | '.join(LOCAL_EXTERNAL_HOSTS)}). "
                f"local-external means the text never leaves the box; an "
                f"endpoint anywhere else IS remote inference and must be "
                f"planned as --use inference=remote:<url> so the deploy "
                f"record stays honest")
        decisions["inference"] = SeamDecision(
            seam="inference", choice="local-external", endpoint=endpoint,
            operator_override=bool(override))
    elif inference_mode == "local":
        # Fix 2: separate the two failures. "no GPU we recognize" is a
        # detection outcome and stops here; "a recognized GPU that no tier
        # fits" is the commercial Scenario-2 fork.
        if not probe.gpu.present and not confirm_no_gpu:
            raise PlanError(f"{NO_GPU_DETECTED}\n\n  probe said: "
                            f"{probe.gpu.error or 'gpu section absent'}")
        plan_tier = select_tier(profiles.tiers, probe.gpu.budget_gb,
                                memory_class=probe.gpu.memory_class,
                                allow_gated=allow_gated_tier)
        if plan_tier is None:
            raise PlanError(SCENARIO_2_OPTIONS)
        decisions["inference"] = SeamDecision(
            seam="inference", choice="local",
            operator_override=bool(override))
    else:
        raise PlanError(f"inference mode {inference_mode!r} not recognized "
                        f"({' | '.join(INFERENCE_MODES)})")

    return DeployPlan(
        profile=profile_name, shape=preset.shape,
        placement=preset.placement, footprint=preset.footprint,
        secrets_custody=custody or preset.custody,
        custody_overridden=custody is not None,
        seams=decisions,
        extraction_tier=plan_tier.name if plan_tier else None,
        extraction_model=plan_tier.extraction_model if plan_tier else None,
        tenants=tenants or [], probe_file=probe_file)


def _qualify_endpoint(seam: str, rules: list[str], endpoint: str,
                      candidates) -> list[RuleResult]:
    """An explicit --use theirs:<endpoint> is matched against the probed
    candidates so the plan carries real evidence; an endpoint the probe never
    saw records that fact (verify must prove it live)."""
    for candidate in candidates:
        probed = getattr(candidate, "dsn_redacted", None) or candidate.endpoint
        if endpoint.split("@")[-1] in probed or probed in endpoint:
            return qualify_candidate(seam, rules, candidate)
    return [RuleResult(rule="(not probed)", passed=False,
                       evidence="endpoint was not in the probe sweep — "
                                "verify must prove it live")]


# ---------------------------------------------------------------------------
# Rendering — the plan becomes the pilot's own config surface (.env)
# ---------------------------------------------------------------------------
def render_env(plan: DeployPlan, pilot_defaults: dict[str, str]) -> str:
    """Render .env.deploy for pydantic Settings. 'ours' seams keep the pilot
    compose defaults; 'theirs'/'remote' seams override from plan endpoints.
    Secrets values here are the OURS-stack bootstrap creds — theirs-postgres
    credentials ride inside the DSN the operator supplied."""
    env = dict(pilot_defaults)
    pg = plan.seams.get("postgres")
    if pg and pg.choice == "theirs":
        parts = urlsplit(pg.endpoint)
        env["POSTGRES_USER"] = parts.username or ""
        env["POSTGRES_PASSWORD"] = parts.password or ""
        env["POSTGRES_HOST"] = parts.hostname or ""
        env["POSTGRES_PORT"] = str(parts.port or 5432)
        env["POSTGRES_DB"] = parts.path.lstrip("/") or "knowledge_hub"
    elif pg:
        # BP34: rendered ALWAYS for an ours-postgres, so compose (which
        # binds 127.0.0.1:${POSTGRES_PORT:-5432}) and every .env consumer
        # (settings, dsn_from_env, stack_alive) agree on the same port —
        # including when a declined client Postgres forced us off 5432.
        env["POSTGRES_PORT"] = str(pg.host_port or 5432)
        # Same rule for the HOST: compose binds 127.0.0.1 literally, so the
        # rendered .env must dial 127.0.0.1 literally. The 'localhost'
        # fallback resolves ::1 first on dual-stack boxes, and Docker
        # Desktop's ::1 proxy black-holes under load — every fresh
        # connection then eats the full connect_timeout before falling
        # through to IPv4 (the pilot .env carried this pin by hand; a
        # re-plan must not strip it).
        env["POSTGRES_HOST"] = "127.0.0.1"
    s3 = plan.seams.get("object_store")
    if s3 and s3.choice == "theirs":
        env["S3_ENDPOINT"] = s3.endpoint
    else:
        # BP28 #21: an OURS object store never deploys with the committed
        # pilot defaults — mint a per-deploy pair. phase_env preserves a
        # LIVE deployment's pair over this fresh mint (SeaweedFS reads
        # s3config.json only at container start), and phase_services
        # renders s3config.json from the same values.
        env["S3_ACCESS_KEY"] = f"kh-s3-{pysecrets.token_hex(4)}"
        env["S3_SECRET_KEY"] = pysecrets.token_hex(24)
    inference = plan.seams.get("inference")
    if inference:
        # BP46 Fix 5: the seam value itself lands in the deployment's config
        # so every runtime consumer, log line and audit answer reads the same
        # word the plan recorded — "local-external" can never be reported as
        # "remote" by something reconstructing intent from OLLAMA_HOST.
        env["INFERENCE_SEAM"] = inference.choice
        if inference.choice in ("remote", "local-external"):
            env["OLLAMA_HOST"] = inference.endpoint
    if plan.extraction_model:
        env["EXTRACTION_MODEL"] = plan.extraction_model
        # Adjudication defaults to the extraction model BY DESIGN (config.py:
        # swap independently only when the benchmark picks a dedicated one).
        # Rendered explicitly so an exact-tag tier (qwen3.6:27b-bf16) can't
        # leave adjudication pointing at a tag the kit doesn't carry.
        env["ADJUDICATION_MODEL"] = plan.extraction_model
    if plan.tenants:
        env["SERVING_TENANTS"] = ",".join(plan.tenants)
    # §8.8: one minted password per least-privilege Postgres role. Same
    # discipline as the S3 pair above — fresh on every plan, and phase_env
    # preserves a LIVE deployment's values so a re-plan never rotates the
    # credentials out from under running services. These are what turn the
    # isolation property from a client-side promise into a server-side
    # grant; a deployment rendered without them leaves every role NOLOGIN
    # and every consumer loudly falling back to the bootstrap account.
    from knowledge_hub.roles import PASSWORD_ENV

    for var in PASSWORD_ENV.values():
        env[var] = pysecrets.token_hex(24)
    # ASCII-only header: .env files get read by naive tooling.
    lines = [f"# Rendered by khctl plan - profile={plan.profile} "
             f"shape={plan.shape} (do not hand-edit; re-plan instead)"]
    lines += [f"{key}={value}" for key, value in env.items()]
    return "\n".join(lines) + "\n"
