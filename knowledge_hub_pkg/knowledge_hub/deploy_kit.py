"""khctl make-kit / verify-kit — the deployment kit builder + integrity gate.

PRODUCER/CONSUMER SYMMETRY: the kit layout below is derived from what
`deploy_apply` actually reads — never invent a parallel structure. If apply
changes where it looks, this module and the contract in DEPLOY_NOTES.md
change in the same commit.

    <kit>/                          (= apply's --kit AND --infra-dir)
      manifest.json                 phase_kit: sha256 per artifact
      manifest.json.minisig         phase_kit: minisign, verified when present
      docker-compose.yml            phase_services (compose base) — DERIVED:
                                    stage_bundle strips `build:` keys, the
                                    kit runs LOADED images only (BP28 #10)
      docker-compose.openbao-prod.yml   phase_services (secrets=ours override)
      openbao/config.hcl            mounted by the prod-bao override
      postgres/init/00-extensions.sql   compose bind-mount target (first-run
                                    init; BP28 #10). seaweedfs/s3config.json
                                    deliberately does NOT ship — phase_services
                                    renders it on site from .env (BP28 #19/#21)
      knowledge_hub_baseline_schema.sql phase_schema
      migrations/*.sql              phase_schema (sorted replay)
      profiles.toml                 khctl plan on site
      check_stack.py                the pilot gate, runnable on the box
      install-ubuntu.sh             bootstrap (venv + editable install + khctl)
      requirements.txt / requirements.lock.txt   bootstrap inputs
      knowledge_hub_pkg/            SOURCE tree — installed EDITABLE on site
                                    (checks.version_triple reads its
                                    pyproject.toml; a bare wheel install
                                    would break version integrity)
      python/cpython-3.12-linux-x86_64.tar.gz   the interpreter the
                                    wheelhouse was built for (BP46 Fix 3):
                                    launch.sh extracts it into the WORK dir
                                    and creates the venv with it, so the
                                    deploy does not depend on the host's
                                    system python (26.04 has 3.14, no 3.12)
      wheelhouse/                   dependency wheels for the LINUX target +
                                    requirements-linux.txt (resolved inside
                                    a linux container — direct deps pinned,
                                    transitives locked by the wheelhouse
                                    itself); bootstrap installs offline
                                    from here, build backends included
      images/*.tar                  phase_services: docker load, in order
      ollama_models/                phase_models: copied to where THIS box's
                                    ollama reads (systemd service ->
                                    /usr/share/ollama/.ollama/models, owned
                                    ollama:ollama; else ~/.ollama/models —
                                    BP28 #18); retry skips a present store
        manifests/registry.ollama.ai/library/<model>/<tag>
        blobs/sha256-<digest>       content-addressed; hash-verified HERE at
                                    build time AND by the manifest on arrival
      tokenizer/bge-m3/tokenizer.json   seeded into the deployment home;
                                    chunking counts tokens with it offline —
                                    the runtime HF download is gone (BP28 #20)

Discipline (same as apply): fail-closed — a missing pinned model, a blob
hash mismatch, or an un-saveable image STOPS the build; idempotent — re-runs
skip already-copied content-addressed blobs and converge to EXACTLY the
pinned set (stage_models prunes leftovers from earlier stagings — an
unpruned rebuild once shipped two extraction models, BP33); NO SECRETS — the
bundle is an explicit ALLOWLIST and a second-net guard scans the finished
kit for anything env/plan/usage-shaped. The kit carries only our software +
public model weights.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from knowledge_hub.deploy_apply import (
    UNSEAL_COMMAND,
    ApplyError,
    verify_kit_manifest,
)
from knowledge_hub.secret_names import FORBIDDEN_NAMES

KIT_FORMAT = 1

# ---------------------------------------------------------------------------
# THE TRUST ANCHOR. The verifier trusts ONLY these keys — never a public key
# found inside the kit being verified (an attacker who repacks a kit can
# include any pubkey they like; that is why this set lives HERE, delivered
# with khctl through the same trusted channel as the code itself).
#
# Versioned SET so rotation/retirement is a code change + redeploy: add the
# new key, ship, re-sign kits, retire the old id. Keep it small.
#
# Custody truth: signing proves a kit came from a holder of the secret key,
# untampered. It does NOT protect against a compromised secret key — the
# secret key's custody IS the security (DEPLOY_NOTES ceremony).
#
# org-2026: minted 2026-07-24 by the operator per the DEPLOY_NOTES ceremony
# (passphrase-protected secret key held offline by the operator; never in repo/kit/
# session). dev-2026-07 (the build-bench throwaway) is RETIRED — kits it
# signed no longer verify and must be re-signed with org-2026.
# Keep install-ubuntu.sh's copy of this set in lock-step.
TRUSTED_PUBKEYS: dict[str, str] = {
    "org-2026": "RWS6/dyR5MslCZKw8pvhLnz3IIIPuXG7mh/IJDSPNUSkhLlr2BH88feP",
}


def verify_manifest_signature(kit_dir: Path) -> Optional[str]:
    """ORDER OF TRUST, step 1: prove the manifest itself before believing a
    byte of it. Returns the trusted key id, or None when unsigned (callers
    decide whether unsigned is tolerable). Raises on a signature that fails
    or matches no trusted key — a validly-signed-by-a-stranger manifest is
    an attack, not a warning."""
    manifest = kit_dir / "manifest.json"
    sig = kit_dir / "manifest.json.minisig"
    if not sig.exists():
        return None
    if not shutil.which("minisign"):
        raise ApplyError("kit is signed but minisign is not on PATH — "
                         "trust cannot be established; install minisign")
    for key_id, pubkey in TRUSTED_PUBKEYS.items():
        out = subprocess.run(
            ["minisign", "-Vm", str(manifest), "-P", pubkey],
            capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            return key_id
    raise ApplyError(
        "manifest signature does NOT verify against any trusted key — "
        "tampered or re-signed by an untrusted party; REFUSE the kit "
        f"(trusted ids: {', '.join(TRUSTED_PUBKEYS)})")

# The bundle allowlist IS the contract — a file not listed here does not
# ship, no matter what is lying around the build folder.
BUNDLE_FILES = (
    "docker-compose.yml",
    "docker-compose.openbao-prod.yml",
    "openbao/config.hcl",
    "postgres/init/00-extensions.sql",
    "knowledge_hub_baseline_schema.sql",
    "profiles.toml",
    "check_stack.py",
    "install-ubuntu.sh",
    "requirements.txt",
    "requirements.lock.txt",
)
# ontologies/ ships the portable BASELINE set only (d.s Stage 1 shipping
# rule): a deployment's own imported versions accumulate in its work-dir
# copy, which seed_work_dir never overwrites (present files are kept).
BUNDLE_DIRS = ("migrations", "ontologies")
# Kit dirs seeded into the deployment home for RUNTIME use (not read by
# apply itself) — consumed by deploy_launch.seed_work_dir, same
# producer/consumer symmetry as BUNDLE_DIRS.
KIT_RUNTIME_DIRS = ("tokenizer",)
PKG_DIR = "knowledge_hub_pkg"
PKG_EXCLUDE = ("__pycache__", "*.egg-info", ".venv", ".pytest_cache")

# The no-secrets definition (and the tracked files that deliberately wear
# the shape) live in knowledge_hub.secret_names — imported at the top of
# this module so the kit gate and the pre-commit hook read ONE definition.
# assert_no_secrets uses FORBIDDEN_NAMES only: COMMIT_ALLOWLIST is a repo
# concept, and none of its paths is ever staged into a kit.

_WIN_MARKER = re.compile(r";\s*sys_platform\s*==\s*[\"']win32[\"']")

# The wheelhouse resolver runs INSIDE this image (matches the kit target:
# linux/x86_64, python 3.12). Cross-platform `pip download --platform` from
# Windows is a trap discovered on the first full-scale build: pip evaluates
# environment markers against the RUNNING interpreter, so a Windows-side
# resolve both pulls win32-only transitives (pywin32 via dlt) and silently
# OMITS linux-only ones (the nvidia/triton wheels torch needs) — the kit
# would have failed at the client site, not the bench. Docker is already a
# make-kit prerequisite (stage_images), so the resolver is linux-native.
WHEELHOUSE_IMAGE = "python:3.12-slim"

# Build backends for the ON-SITE editable install (`pip install -e
# knowledge_hub_pkg` offline): pip's isolated build env resolves these from
# the wheelhouse via --find-links; without them the air-gapped bootstrap
# dies reaching for an index. Pure-python universal wheels, a few hundred KB.
BUILD_BACKEND_WHEELS = ("hatchling", "editables")

# Pins that publish NO wheel on PyPI (sdist-only). `pip download --platform`
# refuses sdists outright, so these are wheel-BUILT on the bench instead —
# legitimate ONLY for pure-python packages: the build must yield a universal
# (py3-none-any) wheel or the stage fails, because a compiled sdist would
# silently produce a build-machine wheel inside a Linux kit. Found the hard
# way on the first full-scale build: antlr4-python3-runtime==4.9.3
# (omegaconf <- docling) and pylatexenc==2.10 (docling) ship as sdist only.
SDIST_ONLY_PURE = ("antlr4-python3-runtime", "pylatexenc")


@dataclass
class KitContext:
    infra_dir: Path
    out_dir: Path
    models: list[str]
    # of {wheelhouse,python,images,models,tokenizer}
    skip: set[str] = field(default_factory=set)
    sign_key: Optional[Path] = None
    allow_unsigned: bool = False                   # dev bench ONLY; self-records
    ollama_store: Optional[Path] = None            # default resolved in stage
    ollama_host: str = "http://localhost:11434"    # version pin source
    pins: dict = field(default_factory=dict)
    components: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------
def filter_lockfile_for_linux(text: str) -> str:
    """Strip win32-marker lines — pip evaluates markers against the BUILD
    machine, so on a Windows builder pywin32 would be selected and then
    fail to resolve a manylinux wheel."""
    kept = [line for line in text.splitlines()
            if not _WIN_MARKER.search(line)]
    return "\n".join(kept) + "\n"


_REQ_NAME = re.compile(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?")

# Transitive pins that must NOT float on the linux re-resolve: the model
# runtime the pilot validated (and, for torch, a multi-GB nvidia dependency
# stack that should change deliberately, never as resolver drift).
PIN_THROUGH = ("torch", "torchvision")


def build_linux_requirements(requirements: str, lock: str,
                             pyproject_deps: list[str]) -> str:
    """The kit's linux requirement set — a first-full-scale-build lesson.

    A Windows `pip freeze` is NOT a linux lockfile: it omits linux-only
    transitives (torch's nvidia/triton wheels) and its transitive pin set is
    not even guaranteed to RESOLVE on linux (fsspec==2026.6.0 vs dlt/
    datasets/torch/huggingface-hub — ResolutionImpossible, found live).
    So: DIRECT dependencies (requirements.txt + the package's pyproject
    deps + PIN_THROUGH) keep their pilot-validated lock versions; the
    transitive closure re-resolves linux-natively inside the wheelhouse
    container. On site the install runs --no-index against the wheelhouse,
    so the downloaded wheel set — hash-pinned by the kit manifest — IS the
    effective lock."""
    lock_pins: dict[str, str] = {}
    for line in filter_lockfile_for_linux(lock).splitlines():
        if "==" in line:
            name, _, version = line.partition("==")
            base = _REQ_NAME.match(name.strip())
            if base:
                lock_pins[base.group(1).lower().replace("_", "-")] = \
                    version.split()[0].strip()

    out: dict[str, str] = {}

    def _add(spec: str) -> None:
        match = _REQ_NAME.match(spec.strip())
        if not match:
            return
        base = match.group(1).lower().replace("_", "-")
        extras = match.group(2) or ""
        version = lock_pins.get(base)
        out[base] = (f"{match.group(1)}{extras}=={version}" if version
                     else f"{match.group(1)}{extras}")

    for line in requirements.splitlines():
        spec = line.split("#", 1)[0].strip()
        if spec:
            _add(spec)
    for dep in pyproject_deps:
        _add(dep)
    for name in PIN_THROUGH:
        if name in lock_pins:
            _add(name)
    header = ("# Generated by khctl make-kit: direct deps at pilot-validated"
              " versions;\n# transitives resolve linux-natively and are"
              " locked by the wheelhouse itself\n# (every wheel is"
              " hash-pinned in the kit manifest).\n")
    return header + "\n".join(out[k] for k in sorted(out)) + "\n"


def compose_images(infra_dir: Path) -> list[str]:
    """The image list is DERIVED from the compose files, not maintained by
    hand — a new service ships automatically or fails loudly, never
    silently missing from the kit."""
    images = []
    for name in ("docker-compose.yml", "docker-compose.openbao-prod.yml"):
        path = infra_dir / name
        if not path.exists():
            continue
        for match in re.finditer(r"^\s*image:\s*(\S+)", path.read_text(
                encoding="utf-8"), re.MULTILINE):
            if match.group(1) not in images:
                images.append(match.group(1))
    if not images:
        raise ApplyError("no image: lines found in compose files — wrong "
                         "--infra-dir?")
    return images


def resolve_model_files(store: Path, model: str) -> list[Path]:
    """A model's manifest + its content-addressed blobs, as paths RELATIVE
    to the store root (the kit preserves the store's own layout)."""
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    manifest_rel = Path("manifests") / "registry.ollama.ai" / "library" / name / tag
    manifest = store / manifest_rel
    if not manifest.exists():
        raise ApplyError(f"model {model!r} not in local store {store} — "
                         f"`ollama pull {model}` on the build machine first")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    digests = [layer["digest"] for layer in data.get("layers", [])]
    if "config" in data:
        digests.append(data["config"]["digest"])
    files = [manifest_rel]
    for digest in digests:
        blob_rel = Path("blobs") / digest.replace(":", "-")
        if not (store / blob_rel).exists():
            raise ApplyError(f"model {model!r}: blob {digest} missing from "
                             f"store — re-pull to repair")
        files.append(blob_rel)
    return files


def default_kit_models(infra_dir: Path) -> list[str]:
    """The defaulted --models set — resolved from KIT DATA, never from
    pilot bench settings. settings.extraction_model is the bench's RUNTIME
    name ("qwen3.6" -> :latest, a different digest than the tier pin), and
    an unsafe default that read it once silently built a kit carrying the
    wrong extraction model (BP33). Extraction pins come from profiles.toml
    [tiers]; gated tiers are excluded — a gated model ships only via an
    explicit --models, mirroring --allow-gated-tier (never silent). The
    embedding model has no tier ladder and stays a settings pin."""
    from knowledge_hub.config import settings
    from knowledge_hub.deploy_profiles import PlanError, load_profiles
    path = infra_dir / "profiles.toml"
    try:
        profiles = load_profiles(path)
    except (OSError, PlanError, ValueError) as e:
        raise ApplyError(
            f"cannot resolve the default model set from {path} "
            f"({e}) — fix profiles.toml or pass --models explicitly")
    models = [settings.embedding_model]
    for tier in profiles.tiers:
        if tier.status == "default" and tier.extraction_model not in models:
            models.append(tier.extraction_model)
    if len(models) == 1:
        raise ApplyError(
            f"{path} has no default-status tier to pin the extraction "
            f"model from — pass --models explicitly")
    return models


def sha256_file(path: Path) -> str:
    # One streaming implementation for build AND arrival (deploy_apply owns
    # it) — a second copy is how the read-whole-file bug happened once.
    from knowledge_hub.deploy_apply import sha256_stream
    return sha256_stream(path)


def assert_no_secrets(kit_dir: Path) -> int:
    """Second net behind the allowlist: nothing env/plan/usage/key-shaped
    may exist ANYWHERE in a finished kit."""
    offenders = [p.relative_to(kit_dir) for p in kit_dir.rglob("*")
                 if p.is_file() and FORBIDDEN_NAMES.match(p.name)]
    if offenders:
        raise ApplyError(
            f"kit contains forbidden artifact(s) — kits carry no secrets, "
            f"no client data, no engagement records: "
            f"{', '.join(str(o) for o in offenders)}")
    return sum(1 for p in kit_dir.rglob("*") if p.is_file())


def build_kit_manifest(kit_dir: Path, pins: dict, components: dict,
                       package_version: str) -> dict:
    artifacts = []
    for path in sorted(kit_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(kit_dir).as_posix()
        if rel in ("manifest.json", "manifest.json.minisig"):
            continue
        artifacts.append({"path": rel, "sha256": sha256_file(path),
                          "bytes": path.stat().st_size})
    return {
        "kit_format": KIT_FORMAT,
        "package_version": package_version,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": {"os": "linux", "arch": "x86_64", "python": "3.12"},
        "pins": pins,
        "components": components,
        "artifacts": artifacts,
    }


def verify_kit_strict(kit_dir: Path,
                      allow_unsigned: bool = False) -> list[str]:
    """The arrival gate (khctl verify-kit). ORDER OF TRUST: (1) the manifest
    SIGNATURE against the verifier's own embedded keys — an unverified
    manifest is attacker-controlled data; (2) only then the hashes inside
    it; (3) the whole-tree audit: no-manifest = not a kit, unlisted extras
    = chain of custody broken, no-secrets guard. Unsigned = REFUSAL unless
    the dev-bench override is passed, and the override records itself."""
    if not (kit_dir / "manifest.json").exists():
        raise ApplyError(f"{kit_dir} has no manifest.json — not a built kit "
                         f"(khctl make-kit produces one)")
    lines = []
    key_id = verify_manifest_signature(kit_dir)
    if key_id is None:
        if not allow_unsigned:
            raise ApplyError(
                "kit is UNSIGNED — do not deploy from it. A client kit is "
                "always signed; for a dev bench pass --allow-unsigned "
                "(the override is recorded in this output)")
        lines.append("[WARN] UNSIGNED kit ACCEPTED via --allow-unsigned "
                     "override — dev bench only, NEVER a client kit")
    else:
        lines.append(f"manifest signature verified against trusted key "
                     f"{key_id!r} (the verifier's own anchor, not the kit's)")
    lines += verify_kit_manifest(kit_dir)
    manifest = json.loads((kit_dir / "manifest.json").read_text(
        encoding="utf-8"))
    listed = {a["path"] for a in manifest["artifacts"]}
    listed |= {"manifest.json", "manifest.json.minisig"}
    extras = [p.relative_to(kit_dir).as_posix() for p in kit_dir.rglob("*")
              if p.is_file()
              and p.relative_to(kit_dir).as_posix() not in listed]
    if extras:
        raise ApplyError(f"kit contains file(s) NOT in the manifest — "
                         f"chain of custody broken: {', '.join(extras)}")
    assert_no_secrets(kit_dir)
    lines.append(f"no unlisted files, no secret-shaped artifacts "
                 f"({len(manifest['artifacts'])} artifacts + manifest)")
    return lines


# ---------------------------------------------------------------------------
# Build stages (fail-closed; each returns progress lines)
# ---------------------------------------------------------------------------
def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def strip_build_keys(compose_text: str) -> str:
    """The kit's compose is a DERIVATIVE of the pilot's: on site every
    image arrives via `docker load`, and the kit ships no build contexts —
    so `build:` keys must not survive into the kit copy (BP28 #10: 0.26.0
    shipped `build: ./postgres` and could not deploy on a clean box).
    One-line `build: <path>` entries are dropped; any other build-shaped
    line (the multi-line mapping form this stripper does not understand)
    fails the build rather than ship a compose that tries to build
    offline."""
    kept = [line for line in compose_text.splitlines()
            if not re.match(r"^\s*build:\s*\S+\s*$", line)]
    for line in kept:
        if re.match(r"^\s*build:", line):
            raise ApplyError(
                "compose carries a build: mapping the kit stripper does "
                "not understand (multi-line form?) — the kit runs LOADED "
                "images only; flatten it to one-line `build: <path>` or "
                "remove it")
    return "\n".join(kept) + "\n"


def stage_bundle(ctx: KitContext) -> list[str]:
    derived = 0
    for rel in BUNDLE_FILES:
        src = ctx.infra_dir / rel
        if not src.exists():
            raise ApplyError(f"bundle file missing from build folder: {rel}")
        dst = ctx.out_dir / rel
        if rel.startswith("docker-compose"):
            # The kit compose references loaded images only — never the
            # pilot bench's build contexts.
            dst.parent.mkdir(parents=True, exist_ok=True)
            text = src.read_text(encoding="utf-8")
            stripped = strip_build_keys(text)
            dst.write_text(stripped, encoding="utf-8", newline="\n")
            derived += (stripped != text)
        else:
            _copy_file(src, dst)
    for rel in BUNDLE_DIRS:
        src = ctx.infra_dir / rel
        shutil.copytree(src, ctx.out_dir / rel, dirs_exist_ok=True)
    n_migrations = len(list((ctx.out_dir / "migrations").glob("*.sql")))
    ctx.components["bundle"] = True
    return [f"{len(BUNDLE_FILES)} bundle files + {n_migrations} migrations "
            f"(explicit allowlist — nothing else ships)"
            + (f"; compose derived: build: keys stripped — the kit runs "
               f"loaded images only" if derived else "")]


def stage_package(ctx: KitContext) -> list[str]:
    src = ctx.infra_dir / PKG_DIR
    dst = ctx.out_dir / PKG_DIR
    shutil.copytree(
        src, dst, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*PKG_EXCLUDE))
    from knowledge_hub import __version__
    ctx.pins["knowledge_hub"] = __version__
    n = sum(1 for p in dst.rglob("*.py"))
    ctx.components["package"] = True
    return [f"knowledge_hub_pkg source ({n} .py files, v{__version__}) — "
            f"installed EDITABLE on site (version-integrity contract)"]


def stage_wheelhouse(ctx: KitContext) -> list[str]:
    if "wheelhouse" in ctx.skip:
        ctx.components["wheelhouse"] = False
        return ["SKIPPED by flag — bootstrap will need egress or a "
                "pre-provisioned venv on site"]
    import tomllib

    wheel_dir = ctx.out_dir / "wheelhouse"
    wheel_dir.mkdir(parents=True, exist_ok=True)
    lock = (ctx.infra_dir / "requirements.lock.txt").read_text(
        encoding="utf-8")
    pyproject = tomllib.loads(
        (ctx.infra_dir / PKG_DIR / "pyproject.toml").read_text(
            encoding="utf-8"))
    linux_reqs = build_linux_requirements(
        (ctx.infra_dir / "requirements.txt").read_text(encoding="utf-8"),
        lock, pyproject["project"]["dependencies"])
    reqs = wheel_dir / "requirements-linux.txt"
    reqs.write_text(linux_reqs, encoding="utf-8")  # the site reads this

    def _pin_name(line: str) -> str:
        return line.split("==")[0].strip().lower().replace("_", "-")

    sdist_pins = [line for line in filter_lockfile_for_linux(lock).splitlines()
                  if _pin_name(line) in SDIST_ONLY_PURE]
    # One container run does it all, in dependency-safe order: (1) wheel-build
    # the sdist-only pins so (2) the FULL resolving download can satisfy their
    # constraints via --find-links while completing the linux-only transitive
    # closure the Windows-frozen lockfile cannot list (nvidia/triton for
    # torch); (3) build backends for the on-site editable install.
    script = ""
    for pin in sdist_pins:
        script += f"pip wheel -q '{pin.strip()}' --no-deps -w /wheelhouse && "
    script += (
        "pip download -q -r /wheelhouse/requirements-linux.txt"
        " -d /wheelhouse --only-binary=:all: --find-links /wheelhouse"
        " && pip download -q " + " ".join(BUILD_BACKEND_WHEELS) +
        " -d /wheelhouse --only-binary=:all:")
    out = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{wheel_dir}:/wheelhouse",
         WHEELHOUSE_IMAGE, "sh", "-c", script],
        capture_output=True, text=True, timeout=5400)
    if out.returncode != 0:
        raise ApplyError(
            f"wheelhouse resolve inside {WHEELHOUSE_IMAGE} failed (docker + "
            f"egress to PyPI/pytorch index required at BUILD time): "
            f"{out.stderr.strip()[-800:]}")
    for pin in sdist_pins:
        name = _pin_name(pin).replace("-", "_")
        built = sorted(wheel_dir.glob(f"{name}-*.whl"))
        if not built or not built[-1].name.endswith("py3-none-any.whl"):
            raise ApplyError(
                f"sdist-only pin {pin!r} built a NON-universal wheel "
                f"({built[-1].name if built else 'none found'}) — that is "
                f"platform-specific, not a portable Linux artifact; refuse")
    wheels = list(wheel_dir.glob("*.whl"))
    ctx.components["wheelhouse"] = True
    return [f"{len(wheels)} linux wheels + requirements-linux.txt "
            f"(resolved inside {WHEELHOUSE_IMAGE}: linux-native markers, "
            f"full closure; build backends + sdist-built wheels included)"]


def stage_images(ctx: KitContext) -> list[str]:
    if "images" in ctx.skip:
        ctx.components["images"] = False
        return ["SKIPPED by flag — apply will compose build/pull "
                "(needs egress on site)"]
    images = compose_images(ctx.infra_dir)
    image_dir = ctx.out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    ctx.pins["images"] = {}
    for image in images:
        inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=60)
        if inspect.returncode != 0:
            raise ApplyError(f"image {image} not present locally — build/"
                             f"pull it before making a kit")
        image_id = inspect.stdout.strip()
        tar = image_dir / (re.sub(r"[/:]", "_", image) + ".tar")
        out = subprocess.run(["docker", "save", "-o", str(tar), image],
                             capture_output=True, text=True, timeout=1800)
        if out.returncode != 0:
            raise ApplyError(f"docker save {image}: {out.stderr.strip()}")
        ctx.pins["images"][image] = image_id
        lines.append(f"{tar.name} ({tar.stat().st_size / 1e9:.2f}GB, "
                     f"{image_id[:19]}…)")
    ctx.components["images"] = True
    return lines


def prune_unpinned_model_files(target: Path,
                               pinned: set[Path]) -> tuple[int, int]:
    """Stale-state guard behind the converge-in-place model stage (BP33):
    staging only ever ADDS files, so a kit dir that previously staged a
    different model keeps that model's manifest + blobs on disk — and the
    manifest tree-walk sweeps them in, so the kit ships them signed and
    verify-kit rightly passes (an 84GB kit carrying two extraction models
    shipped exactly this way). Deleting everything outside the pinned set
    makes a re-stage converge to EXACTLY the pinned models. Returns
    (files_removed, bytes_removed)."""
    if not target.exists():
        return 0, 0
    removed = removed_bytes = 0
    for path in sorted(p for p in target.rglob("*") if p.is_file()):
        if path.relative_to(target) in pinned:
            continue
        removed_bytes += path.stat().st_size
        path.unlink()
        removed += 1
    # deepest-first so emptied parents empty out in turn (a bare
    # manifests/<model>/ tree left behind is clutter, not a model)
    for path in sorted((p for p in target.rglob("*") if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        if not any(path.iterdir()):
            path.rmdir()
    return removed, removed_bytes


def stage_models(ctx: KitContext) -> list[str]:
    if "models" in ctx.skip:
        ctx.components["models"] = False
        return ["SKIPPED by flag — site must already serve the pinned "
                "models (apply verifies)"]
    store = ctx.ollama_store or (Path.home() / ".ollama" / "models")
    target = ctx.out_dir / "ollama_models"
    lines = []
    ctx.pins["models"] = {}
    from knowledge_hub.deploy_probe import probe_ollama
    report = probe_ollama(ctx.ollama_host)
    if not report.reachable:
        raise ApplyError(f"cannot pin ollama version — {ctx.ollama_host} "
                         f"unreachable ({report.error})")
    ctx.pins["ollama_version"] = report.version
    pinned_files: set[Path] = set()
    for model in ctx.models:
        files = resolve_model_files(store, model)
        pinned_files.update(files)
        copied = skipped = total_bytes = 0
        for rel in files:
            src, dst = store / rel, target / rel
            total_bytes += src.stat().st_size
            # blobs are content-addressed: same name + same size = same
            # bytes; the build-time hash check below still proves it
            if dst.exists() and dst.stat().st_size == src.stat().st_size:
                skipped += 1
                continue
            _copy_file(src, dst)
            copied += 1
        # hash-verify every copied blob against its content address —
        # a corrupt SSD write dies HERE, not at a client site
        for rel in files:
            if rel.parts[0] != "blobs":
                continue
            expected = rel.name.replace("sha256-", "")
            actual = sha256_file(target / rel)
            if actual != expected:
                raise ApplyError(
                    f"model {model!r}: blob {rel.name} hash mismatch after "
                    f"copy (expected {expected[:12]}…, got {actual[:12]}…) — "
                    f"bad media? refuse and rebuild")
        ctx.pins["models"][model] = {
            "files": len(files), "bytes": total_bytes}
        lines.append(f"{model}: {len(files)} files, "
                     f"{total_bytes / 1e9:.2f}GB "
                     f"({copied} copied, {skipped} already present), "
                     f"blobs hash-verified")
    pruned, pruned_bytes = prune_unpinned_model_files(target, pinned_files)
    if pruned:
        lines.append(f"pruned {pruned} stale file(s), "
                     f"{pruned_bytes / 1e9:.2f}GB, from earlier stagings — "
                     f"the kit carries EXACTLY the pinned model set")
    ctx.components["models"] = True
    return lines


# ---------------------------------------------------------------------------
# Portable CPython (BP46 Fix 3) — the kit stops depending on the host's
# system python.
#
# The wall it removes: launch.sh hard-pinned python3.12 and the wheelhouse is
# cp312-only, so the launcher refused on Ubuntu 26.04 (python 3.14, no 3.12
# available). The two obvious repairs are both wrong. Targeting 24.04 would
# trade a software problem for a hardware one: 26.04's newer kernel/Mesa is
# very likely WHY the Strix Halo GPU works at all (§8.26), so downgrading the
# OS risks the GPU. Rebuilding the wheelhouse for the host's python would
# make every kit OS-version-locked, needs cp314 wheels that do not all exist
# yet for this dependency set (torch/docling), and would break 24.04 boxes.
#
# So: carry the interpreter. One ~30MB tarball makes the kit work on 24.04,
# 26.04 and whatever ships next, and the existing cp312 wheelhouse stays
# exactly as validated. python-build-standalone's install_only archive is a
# relocatable prefix rooted at `python/` — extract, run `python -m venv`.
PORTABLE_PY_VERSION = "3.12.11"
PORTABLE_PY_RELEASE = "20250818"     # python-build-standalone release tag
# install_only_STRIPPED, measured 2026-07-29: 29 MiB against 97 MiB for plain
# install_only. The difference is debug symbols, which a deploy box has no use
# for, and this artifact is the bulk of every cross-country kit patch. Both
# variants carry bin/python3.12, the venv module, and ensurepip with a bundled
# pip wheel (pip-25.0.1), which is what lets `python -m venv` produce a working
# pip with zero egress. Nothing in the on-site install compiles anything (the
# wheelhouse is prebuilt wheels, the package is pure-python via hatchling), so
# the stripped build is sufficient.
PORTABLE_PY_ASSET = (
    f"cpython-{PORTABLE_PY_VERSION}+{PORTABLE_PY_RELEASE}"
    f"-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz")
PORTABLE_PY_URL = (
    f"https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PORTABLE_PY_RELEASE}/{PORTABLE_PY_ASSET}")
# Upstream digest, fetched and verified on the bench 2026-07-29. ENFORCED at
# build time, so a swapped or corrupted download fails the BUILD instead of
# being signed into a kit on the strength of a matching filename. Updating the
# version/release pin above means re-measuring this (the build error says so).
PORTABLE_PY_SHA256 = \
    "b5a4f189f25cbacba0f76c9bd6f3ea8c35d2064068aa74ccbb6863068caababd"
# Kit-relative name the launcher globs for. Deliberately NOT the upstream
# asset name: launch.sh must find it without knowing the release tag.
PORTABLE_PY_KIT_REL = Path("python") / "cpython-3.12-linux-x86_64.tar.gz"
# Members that must exist inside the archive for the launcher's one use of it
# (`<prefix>/bin/python3.12 -m venv`) to work. Confirmed present in the pinned
# asset by inspection on the bench; ensurepip is not optional, without its
# bundled wheel the offline venv has no pip and the whole bootstrap dies.
PORTABLE_PY_REQUIRED = (
    "python/bin/python3.12",
    f"python/lib/python{PORTABLE_PY_VERSION[:4]}/venv/__init__.py",
    f"python/lib/python{PORTABLE_PY_VERSION[:4]}/ensurepip/__init__.py")

BGE_M3_REPO = "BAAI/bge-m3"
TOKENIZER_KIT_REL = Path("tokenizer") / "bge-m3" / "tokenizer.json"


def _smoke_load_tokenizer(path: Path) -> None:
    from tokenizers import Tokenizer
    Tokenizer.from_file(str(path))


def _resolve_tokenizer_source() -> Path:
    """Locate BAAI/bge-m3's tokenizer.json on the BUILD bench: local HF
    cache first, one hub download otherwise (the bench has egress; the
    deployed box never does — which is exactly why the kit ships it)."""
    from huggingface_hub import hf_hub_download, try_to_load_from_cache
    cached = try_to_load_from_cache(BGE_M3_REPO, "tokenizer.json")
    if isinstance(cached, str):
        return Path(cached)
    return Path(hf_hub_download(BGE_M3_REPO, "tokenizer.json"))


def stage_tokenizer(ctx: KitContext) -> list[str]:
    """BP28 #20: the processing check used to pull this tokenizer from the
    HF hub at RUNTIME — the kit's entire measured egress. Ship it instead;
    chunking loads the seeded local copy (config.bge_m3_tokenizer_json)."""
    if "tokenizer" in ctx.skip:
        ctx.components["tokenizer"] = False
        return ["SKIPPED by flag — chunking will need the HF hub (egress) "
                "on site"]
    try:
        src = _resolve_tokenizer_source()
    except Exception as e:
        raise ApplyError(
            f"cannot resolve {BGE_M3_REPO} tokenizer.json on the build "
            f"bench ({type(e).__name__}: {e}) — the kit MUST ship it; the "
            f"deployed box has no egress to download it (BP28 #20)")
    # Smoke-load BEFORE shipping — a corrupt file dies on the bench, not
    # at a client site.
    try:
        _smoke_load_tokenizer(src)
    except Exception as e:
        raise ApplyError(f"tokenizer.json at {src} does not load "
                         f"({e}) — clear the HF cache entry and rebuild")
    dst = ctx.out_dir / TOKENIZER_KIT_REL
    _copy_file(src, dst)
    ctx.pins["tokenizer"] = {"repo": BGE_M3_REPO, "file": "tokenizer.json",
                             "bytes": dst.stat().st_size}
    ctx.components["tokenizer"] = True
    return [f"{TOKENIZER_KIT_REL.as_posix()} "
            f"({dst.stat().st_size / 1e6:.1f}MB, smoke-loaded) — "
            f"processing runs with zero egress"]


def verify_portable_python(tarball: Path,
                           expect_sha256: Optional[str] = None) -> dict:
    """Prove a portable-python tarball on the BENCH, not at a client site.

    Cannot execute a Linux binary from a Windows/any builder, so the proof is
    structural, plus a digest match against the pinned upstream value. That is
    enough for the one thing the launcher does with it:
      * the bytes are the bytes we pinned (expect_sha256, when given),
      * it is a readable gzip tar,
      * it carries the interpreter, the venv module AND ensurepip (no venv or
        no bundled pip = no offline bootstrap, which is the whole point),
      * no member escapes the archive root (a kit is extracted with tar on a
        client box; path traversal is not a theoretical concern there).
    Returns pin metadata for the manifest."""
    import tarfile

    if not tarball.exists():
        raise ApplyError(f"portable python tarball missing: {tarball}")
    digest = sha256_file(tarball)
    if expect_sha256 and digest != expect_sha256:
        raise ApplyError(
            f"portable python tarball {tarball} has sha256 {digest}, expected "
            f"{expect_sha256} (deploy_kit.PORTABLE_PY_SHA256). Either the "
            f"download is corrupt/substituted, or the version pin moved and "
            f"the digest was not re-measured. Do NOT sign a kit around an "
            f"unexplained interpreter")
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            names = tar.getnames()
    except tarfile.TarError as e:
        raise ApplyError(
            f"portable python tarball {tarball} is not a readable gzip tar "
            f"({type(e).__name__}: {e}) — re-download it")
    escaping = [n for n in names
                if n.startswith("/") or ".." in Path(n).parts]
    if escaping:
        raise ApplyError(
            f"portable python tarball {tarball} has member(s) escaping the "
            f"archive root ({', '.join(escaping[:3])}) — refuse")
    members = set(names)
    missing = [rel for rel in PORTABLE_PY_REQUIRED if rel not in members]
    if missing:
        raise ApplyError(
            f"portable python tarball {tarball} lacks {missing} — the "
            f"launcher bootstraps with `python -m venv`, so an archive "
            f"without the interpreter and the venv module is unusable "
            f"(expect a python-build-standalone *install_only* asset)")
    return {"version": PORTABLE_PY_VERSION,
            "release": PORTABLE_PY_RELEASE,
            "asset": PORTABLE_PY_ASSET,
            "url": PORTABLE_PY_URL,
            "sha256": digest,
            "bytes": tarball.stat().st_size,
            "members": len(names)}


def _portable_python_cache() -> Path:
    return Path.home() / ".cache" / "knowledge-hub" / "python"


def _resolve_portable_python() -> Path:
    """Locate the tarball on the BUILD bench: an explicit override, then the
    bench cache, then one download (the bench has egress; the deployed box
    never does — which is exactly why the kit ships it). Mirrors
    _resolve_tokenizer_source deliberately."""
    import os
    import urllib.request

    override = os.environ.get("KH_PORTABLE_PYTHON")
    if override:
        path = Path(override)
        if not path.exists():
            raise ApplyError(f"KH_PORTABLE_PYTHON={override} does not exist")
        return path
    cached = _portable_python_cache() / PORTABLE_PY_ASSET
    if cached.exists():
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(".part")
    urllib.request.urlretrieve(PORTABLE_PY_URL, tmp)
    tmp.replace(cached)
    return cached


def stage_python(ctx: KitContext) -> list[str]:
    """Ship the interpreter the wheelhouse was built for (BP46 Fix 3)."""
    if "python" in ctx.skip:
        ctx.components["python"] = False
        return ["SKIPPED by flag — the box must then supply python3.12 "
                "itself (launch.sh falls back to a host python3.12 and "
                "refuses honestly if there is none; the wheelhouse is "
                "cp312-only, so 3.13/3.14 cannot install it)"]
    try:
        src = _resolve_portable_python()
    except Exception as e:
        raise ApplyError(
            f"cannot resolve the portable python tarball on the build bench "
            f"({type(e).__name__}: {e}) — the kit MUST ship it: the client "
            f"box may be on any Ubuntu and the wheelhouse is cp312-only. "
            f"Download {PORTABLE_PY_URL} once and point KH_PORTABLE_PYTHON "
            f"at it (or --skip python, accepting the host-python dependency)")
    dst = ctx.out_dir / PORTABLE_PY_KIT_REL
    _copy_file(src, dst)
    pins = verify_portable_python(dst, expect_sha256=PORTABLE_PY_SHA256)
    ctx.pins["python"] = pins
    ctx.components["python"] = True
    return [f"{PORTABLE_PY_KIT_REL.as_posix()} — CPython "
            f"{PORTABLE_PY_VERSION} (python-build-standalone "
            f"{PORTABLE_PY_RELEASE}, install_only_stripped), "
            f"{pins['bytes'] / 1e6:.0f}MB, structure-verified. The deploy no "
            f"longer depends on the host's system python: works on 24.04 "
            f"(3.12) and 26.04 (3.14) alike",
            f"sha256 {pins['sha256'][:16]}… matches the pinned upstream "
            f"digest; the kit manifest then binds this exact copy"]


def stage_guard(ctx: KitContext) -> list[str]:
    n = assert_no_secrets(ctx.out_dir)
    return [f"no secrets / engagement artifacts in {n} files"]


def stage_manifest(ctx: KitContext) -> list[str]:
    import os

    from knowledge_hub import __version__

    sign_key = ctx.sign_key or (
        Path(os.environ["KH_SIGN_KEY"]) if os.environ.get("KH_SIGN_KEY")
        else None)
    if sign_key is None and not ctx.allow_unsigned:
        raise ApplyError(
            "no signing key — a kit build REQUIRES a signature "
            "(--sign-key <secret-key> or KH_SIGN_KEY env). The dev-bench "
            "escape hatch is --allow-unsigned, and it records itself; "
            "a client kit is ALWAYS signed")

    manifest = build_kit_manifest(ctx.out_dir, ctx.pins, ctx.components,
                                  __version__)
    manifest_path = ctx.out_dir / "manifest.json"
    sig_path = ctx.out_dir / "manifest.json.minisig"
    if sig_path.exists():
        sig_path.unlink()  # a stale signature never survives a rebuild
    if sign_key is None:
        manifest["unsigned_override"] = True   # self-recording override
    manifest_path.write_text(json.dumps(manifest, indent=2),
                             encoding="utf-8")
    total = sum(a["bytes"] for a in manifest["artifacts"])
    lines = [f"{len(manifest['artifacts'])} artifacts hashed, "
             f"{total / 1e9:.2f}GB total"]

    if sign_key is None:
        lines.append("[WARN] UNSIGNED kit by --allow-unsigned override "
                     "(recorded in the manifest) — dev bench only")
        return lines
    if not shutil.which("minisign"):
        raise ApplyError("signing requested but minisign not on PATH")
    if not sign_key.exists():
        raise ApplyError(f"signing key not found: {sign_key} — the secret "
                         f"key is human-held (DEPLOY_NOTES ceremony), "
                         f"never in the repo or the kit")
    # Inherit the console: an encrypted key prompts for its passphrase and
    # the OPERATOR answers it (that prompt is the custody model working).
    # Non-interactive contexts have no stdin to read -> minisign fails fast
    # rather than hanging; the timeout is a walked-away-from-keyboard backstop.
    out = subprocess.run(
        ["minisign", "-Sm", str(manifest_path), "-s", str(sign_key)],
        timeout=300)
    if out.returncode != 0:
        raise ApplyError(
            "minisign signing failed — an encrypted key prompts for its "
            "passphrase, so run make-kit from an interactive terminal "
            "(wrong passphrase and unreadable key fail the same way)")
    # Self-check: the signature we just made must verify against OUR OWN
    # trusted set — signing with a stray key fails at the build bench,
    # not at a client site.
    key_id = verify_manifest_signature(ctx.out_dir)
    lines.append(f"manifest signed + self-verified against trusted key "
                 f"{key_id!r}")
    return lines


# ---------------------------------------------------------------------------
# khctl make-ssd — the SSD root (Build Prompt 18; nested layout BP27).
# The SSD root shows exactly TWO things: one launch shortcut, and one
# folder (decant.Source/) holding everything else — launch.sh, kit/, the
# console pair, PREREQS.txt. launch.sh resolves the kit RELATIVE TO ITSELF
# ($SCRIPT_DIR/kit), so the script and the kit move as a unit; the root
# shortcut is the only thing that points across the folder boundary. The
# wrapper is THIN by design — signature gate + offline python bootstrap,
# then it execs `khctl launch`, where all real logic lives. Its
# trust-anchor array is RENDERED from TRUSTED_PUBKEYS at write time, so
# unlike install-ubuntu.sh (hand-kept in lock-step) it cannot drift.
# ---------------------------------------------------------------------------
NESTED_DIR_DEFAULT = "decant.Source"
_LAUNCH_SH = """\
#!/usr/bin/env bash
# Knowledge Hub — SSD launcher (generated by `khctl make-ssd`; do not
# hand-edit). Double-click "Knowledge Hub.desktop" (right-click -> Allow
# Launching first, on GNOME), or from a terminal:  bash launch.sh
# Flags pass through to `khctl launch` (e.g. bash launch.sh --dry-run).
#
# THIN BY DESIGN: everything after bootstrap is `khctl launch` (the guided,
# stateful flow). This wrapper only (1) refuses early when an OS
# prerequisite is missing (offline-honest — the kit cannot install those),
# (2) verifies the kit manifest signature against the trust anchor embedded
# below BEFORE installing anything from the kit, and (3) bootstraps an
# offline python env on the box from the kit wheelhouse. All deployment
# logic lives in khctl.
set -euo pipefail

SSD_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT="$SSD_ROOT/@KIT_SUBDIR@"
WORK="${KH_WORK_DIR:-$HOME/knowledge-hub}"

# Double-click terminals live exactly as long as this script — never let a
# message (or a print-once secret) vanish with the window (B2).
hold_open() { echo; read -r -p "press Enter to close " _ || true; }

# --- Offline prereq wall (B1): these need internet to install and are NOT
# --- on this SSD. They must be on the box BEFORE going offline — the list
# --- and install commands are in PREREQS.txt at the SSD root.
missing=""
command -v docker   >/dev/null 2>&1 || missing="$missing docker"
command -v minisign >/dev/null 2>&1 || missing="$missing minisign"
command -v tar      >/dev/null 2>&1 || missing="$missing tar"
command -v ollama   >/dev/null 2>&1 || missing="$missing ollama"
# NOTE (BP46 Fix 3): python is NOT on this list any more. The kit carries a
# portable CPython 3.12 matching its cp312 wheelhouse, so the host's system
# python is irrelevant — which is what makes this launcher run on Ubuntu
# 26.04 (python 3.14, no 3.12 installable) as well as on 24.04. A kit built
# with --skip python falls back to a host python3.12 further down and
# refuses there, honestly, if the host has none.
if [ -n "$missing" ]; then
  echo "!! missing prerequisite(s):$missing"
  echo "   These must be installed BEFORE going offline — this kit cannot"
  echo "   install them. See PREREQS.txt at the root of this SSD."
  hold_open; exit 1
fi

[ -f "$KIT/manifest.json" ] || {
  echo "!! no kit at $KIT (expected manifest.json) — wrong SSD layout?"
  hold_open; exit 1; }

# --- Trust anchor: rendered from deploy_kit.TRUSTED_PUBKEYS at make-ssd
# --- time (lock-step by construction). Signature FIRST — nothing from the
# --- kit is executed or installed before the manifest verifies; khctl
# --- launch then re-runs the FULL arrival gate (hashes + tree audit +
# --- no-secrets) before deploying.
TRUSTED_PUBKEYS=(
@ANCHOR_LINES@
)
if [ ! -f "$KIT/manifest.json.minisig" ]; then
  echo "!! kit manifest is UNSIGNED — refusing (a walk-in kit is always signed)"
  hold_open; exit 1
fi
verified=0
for pk in "${TRUSTED_PUBKEYS[@]}"; do
  if minisign -Vm "$KIT/manifest.json" -P "$pk" >/dev/null 2>&1; then
    verified=1; break
  fi
done
if [ "$verified" != "1" ]; then
  echo "!! manifest signature matches NO trusted key — tampered or re-signed; REFUSING"
  hold_open; exit 1
fi
echo "-- kit signature verified against the embedded trust anchor"

# --- Offline python bootstrap (idempotent). The venv and the package
# --- source live on the BOX so the install survives unplugging the SSD;
# --- wheels come from the kit wheelhouse (no egress needed).
mkdir -p "$WORK"
VENV="$WORK/.venv"

# --- Which interpreter builds that venv (BP46 Fix 3). The wheelhouse is
# --- cp312, so this is not a preference: it is 3.12 or nothing. The kit
# --- ships one, and it is extracted into WORK (on the BOX) — never onto the
# --- read-only SSD, and it must SURVIVE after the SSD leaves, because the
# --- venv it creates points at it by absolute path.
PY=""
KIT_PY_TGZ=""
for candidate in "$KIT"/python/cpython-3.12-*.tar.gz; do
  # `if`, not `[ ... ] && break`: an unmatched glob would make the list fail
  # and `set -e` would kill the launcher on a --skip-python kit.
  if [ -f "$candidate" ]; then KIT_PY_TGZ="$candidate"; break; fi
done
if [ -n "$KIT_PY_TGZ" ]; then
  PYROOT="$WORK/.python3.12"
  if [ ! -x "$PYROOT/python/bin/python3.12" ]; then
    echo "-- unpacking the kit's portable python 3.12 into $PYROOT"
    rm -rf "$PYROOT"
    mkdir -p "$PYROOT"
    tar -xzf "$KIT_PY_TGZ" -C "$PYROOT" || {
      echo "!! could not unpack $KIT_PY_TGZ: the kit's python component is damaged"
      hold_open; exit 1; }
  fi
  PY="$PYROOT/python/bin/python3.12"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  # Kit built with --skip python: fall back to a host 3.12 if there is one.
  if command -v python3.12 >/dev/null 2>&1 \\
     && python3.12 -c 'import ensurepip' >/dev/null 2>&1; then
    PY="python3.12"
    echo "-- this kit ships no portable python: using the host python3.12"
  else
    echo "!! no usable python 3.12 on this box and none in the kit."
    echo "   The kit wheelhouse is cp312-only, so the host's python 3.13/3.14"
    echo "   CANNOT install it. Two ways forward, and the first is preferred:"
    echo "     - rebuild the kit WITH its python component (khctl make-kit"
    echo "       without --skip python), which makes the deploy independent"
    echo "       of whatever python this OS ships;"
    echo "     - or provide python3.12 + its venv module on this box."
    hold_open; exit 1
  fi
fi
"$PY" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version' || {
  echo "!! the resolved python is not 3.12; the kit wheelhouse is cp312-only. REFUSING"
  hold_open; exit 1; }

if [ ! -x "$VENV/bin/khctl" ]; then
  echo "-- bootstrapping $VENV from the kit (offline)"
  "$PY" -m venv "$VENV" || {
    echo "!! venv creation failed with $PY"
    hold_open; exit 1; }
  rm -rf "$WORK/knowledge_hub_pkg"
  cp -r "$KIT/knowledge_hub_pkg" "$WORK/knowledge_hub_pkg"
  if [ -d "$KIT/wheelhouse" ]; then
    echo "-- installing python dependencies from the kit wheelhouse (~3GB — several minutes; pip prints progress)"
    "$VENV/bin/pip" install --no-index \\
      --find-links "$KIT/wheelhouse" -r "$KIT/wheelhouse/requirements-linux.txt"
    "$VENV/bin/pip" install --no-index \\
      --find-links "$KIT/wheelhouse" --no-deps -e "$WORK/knowledge_hub_pkg"
  else
    echo "-- kit built without a wheelhouse (recorded in its manifest) — needs egress"
    "$VENV/bin/pip" install -r "$KIT/requirements.lock.txt"
    "$VENV/bin/pip" install --no-deps -e "$WORK/knowledge_hub_pkg"
  fi
fi

# khctl on PATH (L1): every printed hint says `khctl ...` — make that true
# for login shells (~/.local/bin is on Ubuntu's default PATH). Repaired on
# every launch; best-effort.
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/khctl" "$HOME/.local/bin/khctl" 2>/dev/null || true

# Run the guided flow, pass flags through ("$@" — recovery flags like
# --allow-gated-tier must be reachable from the SSD entry point), and hold
# the window open afterwards: the launcher prints print-once secrets and
# diagnostics that must never disappear with the terminal.
set +e
"$VENV/bin/khctl" launch --kit "$KIT" --work-dir "$WORK" "$@"
rc=$?
hold_open
exit $rc
"""

_DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Version=1.0
Name=Knowledge Hub — Data Ingestion
Comment=Deploy the Knowledge Hub from this SSD, or start it if already deployed
Exec=bash -c 'exec bash "$(dirname "$(realpath "%k")")/launch.sh"'
Terminal=true
Icon=drive-harddisk
Categories=Utility;System;
"""

# The nested layout's ONE root shortcut: it sits at the SSD root while
# launch.sh lives inside the folder, so its Exec is the single place that
# crosses the folder boundary (rendered at write time, never hand-kept).
_ROOT_DESKTOP = """\
[Desktop Entry]
Type=Application
Version=1.0
Name=Launch decant.Source
Comment=Deploy decant.Source from this SSD, or start it if already deployed
Exec=bash -c 'exec bash "$(dirname "$(realpath "%k")")/@NESTED_DIR@/launch.sh"'
Terminal=true
Icon=drive-harddisk
Categories=Utility;System;
"""

# SSD shortcut #2 (BP23): the operator's one-click into the LIVE console on
# a deployed box. THIN like launch.sh — it only finds the deployment the
# launcher created and execs `khctl console` there (which reuses
# ensure_operator and never mints in a deployed context). It carries no
# trust logic and no credentials; if the box isn't deployed yet it says so
# and points at the launcher.
_CONSOLE_SH = """\
#!/usr/bin/env bash
# decant.Source — Open Console (generated by `khctl make-ssd`; do not
# hand-edit). Opens the operator console of an ALREADY-DEPLOYED box. Log in
# with the print-once operator credential from deploy bootstrap (or one
# issued by `khctl provision-operator`).
set -euo pipefail

WORK="${KH_WORK_DIR:-$HOME/knowledge-hub}"
KHCTL="$WORK/.venv/bin/khctl"

if [ ! -x "$KHCTL" ] || [ ! -f "$WORK/deploy_plan.json" ]; then
  echo "!! no deployment found at $WORK"
  echo "   Deploy first: double-click 'Knowledge Hub.desktop' (or run"
  echo "   launch.sh). The console needs a deployed stack to talk to."
  read -r -p "press Enter to close " _
  exit 1
fi

exec "$KHCTL" console --work-dir "$WORK"
"""

_CONSOLE_DESKTOP = """\
[Desktop Entry]
Type=Application
Version=1.0
Name=decant.Source — Operator Console
Comment=Open the live operator console (watch ingestion, resolve reviews)
Exec=bash -c 'exec bash "$(dirname "$(realpath "%k")")/console.sh"'
Terminal=true
Icon=utilities-system-monitor
Categories=Utility;System;
"""

# B1: the SSD root's own honest answer to "why won't it start?". The kit is
# fully offline EXCEPT these host packages — nothing on the SSD can install
# them, and every in-kit failure hint now points here instead of at
# apt-get/curl commands that cannot work without egress.
_PREREQS_TXT = """\
decant.Source — SSD prerequisites  (install BEFORE going offline)
=================================================================

Everything on this SSD installs OFFLINE — except these four host
packages, which need internet (or a package mirror) and must already
be on the box before launch.sh can do anything:

  1. Docker Engine + compose plugin  (official apt repo — NOT the
     get.docker.com convenience script: the apt repo works behind a
     corporate apt mirror, which get.docker.com may not)
       sudo apt-get update
       sudo apt-get install -y ca-certificates curl
       sudo install -m 0755 -d /etc/apt/keyrings
       sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
       sudo chmod a+r /etc/apt/keyrings/docker.asc
       echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
       sudo apt-get update
       sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
       sudo usermod -aG docker "$USER"
       # group membership needs a NEW login shell: log out and back in
       # (over SSH: exit, then ssh back in), then confirm:
       docker run hello-world && docker compose version
       (reference: https://docs.docker.com/engine/install/ubuntu/)
  2. minisign  (verifies the kit signature)
       sudo apt-get install -y minisign
  3. zstd  (Ollama's installer aborts without it — stock Ubuntu 24.04
     does not ship it)
       sudo apt-get install -y zstd
  4. Ollama  (native install on the GPU host; needs zstd first)
       curl -fsSL https://ollama.com/install.sh | sh
       (reference: https://ollama.com/download/linux)

python is NOT on that list. It used to be: the launcher required the
host's python3.12, which made the kit unusable on Ubuntu 26.04 (it
ships python 3.14 and no 3.12 is available). The kit now CARRIES a
portable CPython 3.12 matching its own dependency wheels, unpacks it
into the deployment home, and builds the venv with it. Whatever python
this OS ships is irrelevant, and no version of it needs installing.
(A kit deliberately built without that component falls back to a host
python3.12 and says so plainly if there is none.)

A headless box (reached over SSH) additionally needs:
  - openssh-server  — REQUIRED: the console binds 127.0.0.1 by design
    and is reached through an SSH tunnel
       sudo apt-get install -y openssh-server
  - iptables  — REQUIRED if you intend to verify or enforce that the
    box makes no outbound connections. Stock Ubuntu 24.04 does not
    ship it, and it cannot be added once you are offline.
       sudo apt-get install -y iptables
  - tmux  — REQUIRED for an SSH-driven deploy: the launcher runs in a
    tmux session so a dropped SSH connection never kills a deploy (or
    loses a print-once credential) mid-flight.
       sudo apt-get install -y tmux
  - rsync  — optional: kit copy off the SSD with a progress readout
       sudo apt-get install -y rsync

DO NOT run kit/install-ubuntu.sh on a deployment box. It is the PILOT
REPLAY script for a dev workstation: it builds container images from
source, pulls models from the internet and pip-installs from PyPI —
none of which a deployed box should do, and all of which defeat the
offline design. Use the commands above, then launch.sh.

launch.sh checks the tools it needs up front and refuses with this
list if any is missing — that refusal is expected on an unprepared
box, not a fault in the kit.

After a reboot, note: the production vault comes back SEALED by
design. Unseal with 3 of the 5 custody shares (run 3x; the BAO_ADDR
is required — the CLI defaults to HTTPS, the listener is plain HTTP):
  {unseal_command}
""".format(unseal_command=UNSEAL_COMMAND)


def render_launch_sh(kit_subdir: str = "kit") -> str:
    """launch.sh with the CURRENT trust anchor rendered in. LF endings are
    the caller's job (write_ssd_root uses newline='\\n' — a CRLF launch.sh
    dies on Ubuntu with '/usr/bin/env: bash\\r: No such file')."""
    anchor = "\n".join(f'  "{pubkey}"  # {key_id}'
                       for key_id, pubkey in TRUSTED_PUBKEYS.items())
    return (_LAUNCH_SH
            .replace("@ANCHOR_LINES@", anchor)
            .replace("@KIT_SUBDIR@", kit_subdir))


def write_ssd_root(root: Path, kit_subdir: str = "kit",
                   nested_dir: Optional[str] = NESTED_DIR_DEFAULT
                   ) -> list[str]:
    """Write the SSD root. Nested (the default): the root shows exactly
    TWO things — 'Launch <nested_dir>.desktop' and <nested_dir>/ holding
    launch.sh, the console pair, PREREQS.txt and (built separately) kit/.
    launch.sh finds the kit relative to itself, so script + kit move as a
    unit and only the root shortcut's Exec crosses the folder boundary.
    nested_dir=None keeps the pre-BP27 flat layout (everything at the
    root). The kit itself is built separately (khctl make-kit --out
    <inner>/<kit_subdir>) so the expensive artifact and the wrapper files
    have independent lifecycles."""
    root.mkdir(parents=True, exist_ok=True)
    inner = (root / nested_dir) if nested_dir else root
    inner.mkdir(parents=True, exist_ok=True)
    launch = inner / "launch.sh"
    with launch.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_launch_sh(kit_subdir))
    try:
        launch.chmod(0o755)  # best-effort: exFAT/NTFS have no exec bit
    except OSError:
        pass
    if nested_dir:
        desktop = root / f"Launch {nested_dir}.desktop"
        with desktop.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(_ROOT_DESKTOP.replace("@NESTED_DIR@", nested_dir))
    else:
        desktop = inner / "Knowledge Hub.desktop"
        with desktop.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(_DESKTOP_ENTRY)
    console = inner / "console.sh"
    with console.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(_CONSOLE_SH)
    try:
        console.chmod(0o755)
    except OSError:
        pass
    console_desktop = inner / "Open Console.desktop"
    with console_desktop.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(_CONSOLE_DESKTOP)
    prereqs = inner / "PREREQS.txt"
    with prereqs.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(_PREREQS_TXT)
    lines = [f"launch.sh + {desktop.name!r} written "
             f"({'nested: ' + str(inner) if nested_dir else str(root)}; "
             f"trust anchor: {', '.join(TRUSTED_PUBKEYS)})",
             f"console.sh + 'Open Console.desktop' written to {inner} "
             f"(the operator's one-click into a deployed console)",
             f"PREREQS.txt written to {inner} (the five host packages that "
             f"must be installed BEFORE going offline)"]
    kit_dir = inner / kit_subdir
    if (kit_dir / "manifest.json").exists():
        lines.append(f"kit present at {kit_dir}")
    else:
        lines.append(f"kit NOT yet at {kit_dir} — build it: "
                     f"khctl make-kit --out {kit_dir}")
    return lines


STAGES: list[tuple[str, Callable[[KitContext], list[str]]]] = [
    ("bundle", stage_bundle),
    ("package", stage_package),
    ("wheelhouse", stage_wheelhouse),
    ("python", stage_python),
    ("images", stage_images),
    ("models", stage_models),
    ("tokenizer", stage_tokenizer),
    ("no-secrets guard", stage_guard),
    ("manifest", stage_manifest),
]


def run_make_kit(ctx: KitContext) -> int:
    print(f"Knowledge Hub — make-kit -> {ctx.out_dir}\n" + "-" * 44)
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in STAGES:
        try:
            for line in fn(ctx):
                print(f"[ OK ] {name}: {line}")
        except ApplyError as e:
            print(f"[FAIL] {name}: {e}")
            print("-" * 44)
            print(f"[FAIL] make-kit stopped at {name!r} — fix and re-run "
                  f"(stages are idempotent)")
            return 1
    print("-" * 44)
    print(f"[ OK ] kit built — gate it on arrival: khctl verify-kit "
          f"--kit {ctx.out_dir}")
    return 0
