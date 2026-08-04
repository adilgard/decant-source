"""BP46 Fixes 3 and 5, the OS/python wall, and an honest seam for an
operator-supplied local GPU.

Fix 3: the launcher hard-pinned the HOST's python3.12 while the wheelhouse
ships cp312 wheels, so it refused on Ubuntu 26.04 (python 3.14, no 3.12
available), and 26.04 is very likely what makes the Strix Halo GPU work, so
downgrading the OS was never an option. The kit now CARRIES a portable
CPython 3.12. These tests pin the launcher's new bootstrap and the build-time
verification of the tarball.

Fix 5: the seam vocabulary was local | remote, so an in-chassis GPU we did
not provision had to be planned as "remote", labelling an on-premises deploy
as off-premises, which a client security review would find. These tests pin
`local-external` end to end: plan, rendered config, apply, verify selection
and the locality sentence.
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from knowledge_hub.deploy_apply import ApplyError
from knowledge_hub.deploy_kit import (
    PORTABLE_PY_ASSET,
    PORTABLE_PY_KIT_REL,
    PORTABLE_PY_REQUIRED,
    PORTABLE_PY_VERSION,
    KitContext,
    render_launch_sh,
    stage_python,
    verify_portable_python,
)
from knowledge_hub.deploy_cli import PILOT_ENV_DEFAULTS, verify_checks_for
from knowledge_hub.deploy_profiles import (
    PlanError,
    load_profiles,
    render_env,
    resolve_plan,
)

INFRA_DIR = Path(__file__).resolve().parents[2]
PROFILES = load_profiles(INFRA_DIR / "profiles.toml")


def make_gpu_probe(vram: float = 192.0):
    from knowledge_hub.deploy_probe import (
        GpuDevice,
        GpuReport,
        HostReport,
        ProbeReport,
    )
    return ProbeReport(
        host=HostReport(os="Linux 6.8", machine="x86_64", docker=True,
                        docker_compose=True),
        gpu=(GpuReport(present=True,
                       devices=[GpuDevice(name="test-gpu", vram_gb=vram)],
                       vram_gb_total=vram, vram_gb_max_single=vram)
             if vram else GpuReport(present=False,
                                    error="no supported GPU detected")))


# ---------------------------------------------------------------------------
# Fix 3, the kit carries the interpreter its wheelhouse was built for
# ---------------------------------------------------------------------------
def _portable_py_tarball(path: Path, members=PORTABLE_PY_REQUIRED,
                         extra: tuple[str, ...] = ()) -> Path:
    """A stand-in for a python-build-standalone install_only asset: the same
    member layout, none of the 30MB."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        for name in (*members, *extra):
            data = b"#!/bin/false\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_portable_python_tarball_is_verified_on_the_bench(tmp_path):
    pins = verify_portable_python(
        _portable_py_tarball(tmp_path / PORTABLE_PY_ASSET))
    assert pins["version"] == PORTABLE_PY_VERSION
    assert pins["sha256"] and pins["bytes"] > 0
    assert PORTABLE_PY_ASSET in pins["asset"]


def test_python_tarball_without_venv_fails_the_BUILD_not_the_site(tmp_path):
    # `python -m venv` IS the bootstrap. An archive that cannot do it must
    # die on the bench, not after a 60GB drive has flown to a client.
    only_interpreter = _portable_py_tarball(
        tmp_path / "no-venv.tar.gz", members=("python/bin/python3.12",))
    with pytest.raises(ApplyError, match="venv"):
        verify_portable_python(only_interpreter)


def test_python_tarball_with_escaping_member_is_refused(tmp_path):
    evil = _portable_py_tarball(tmp_path / "evil.tar.gz",
                                extra=("../../etc/cron.d/payload",))
    with pytest.raises(ApplyError, match="escaping"):
        verify_portable_python(evil)


def test_corrupt_python_tarball_is_refused(tmp_path):
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tarball at all")
    with pytest.raises(ApplyError, match="gzip tar"):
        verify_portable_python(junk)


def test_unexpected_digest_fails_the_build(tmp_path):
    # The interpreter is the one kit artifact fetched from the public
    # internet at build time, so its digest is pinned in code and enforced
    # BEFORE signing: a substituted or truncated download must not be able
    # to ride into a signed kit on the strength of a matching filename.
    tarball = _portable_py_tarball(tmp_path / PORTABLE_PY_ASSET)
    with pytest.raises(ApplyError, match="expected"):
        verify_portable_python(tarball, expect_sha256="0" * 64)
    # ... and with no expectation passed, structure alone still governs
    assert verify_portable_python(tarball)["version"] == PORTABLE_PY_VERSION


@pytest.mark.skipif(not (Path.home() / ".cache" / "knowledge-hub" / "python"
                        / PORTABLE_PY_ASSET).exists(),
                    reason="pinned interpreter not fetched on this bench")
def test_the_REAL_pinned_interpreter_satisfies_the_bench_check():
    """Runs against the actual upstream artifact when the bench has fetched
    it. This is the only test here that validates the assumption Fix 3 rests
    on (that a python-build-standalone install_only_stripped archive really
    carries bin/python3.12 + venv + ensurepip at the expected paths) against
    the real file rather than a fabricated stand-in."""
    from knowledge_hub.deploy_kit import PORTABLE_PY_SHA256

    cached = (Path.home() / ".cache" / "knowledge-hub" / "python"
              / PORTABLE_PY_ASSET)
    pins = verify_portable_python(cached, expect_sha256=PORTABLE_PY_SHA256)
    assert pins["sha256"] == PORTABLE_PY_SHA256
    assert pins["members"] > 1000                 # a whole stdlib, not a stub
    assert 20e6 < pins["bytes"] < 45e6            # stripped, not the 97MB one


def test_stage_python_ships_and_pins_the_interpreter(tmp_path, monkeypatch):
    import knowledge_hub.deploy_kit as dk
    from knowledge_hub.deploy_kit import sha256_file

    src = _portable_py_tarball(tmp_path / "src" / PORTABLE_PY_ASSET)
    monkeypatch.setattr(dk, "_resolve_portable_python", lambda: src)
    # the stand-in is not the pinned upstream artifact, so point the pin at
    # it; the digest gate itself is asserted below and in its own test
    monkeypatch.setattr(dk, "PORTABLE_PY_SHA256", sha256_file(src))
    out = tmp_path / "kit"
    ctx = KitContext(infra_dir=tmp_path, out_dir=out, models=[])
    lines = stage_python(ctx)
    shipped = out / PORTABLE_PY_KIT_REL
    assert shipped.exists()
    assert ctx.components["python"] is True
    assert ctx.pins["python"]["version"] == PORTABLE_PY_VERSION
    # the operator is told to compare the digest with upstream before signing
    assert any("sha256" in line for line in lines)
    assert any("26.04" in line for line in lines)

    # the digest gate is live in the STAGE, not just in the helper: a
    # substituted interpreter cannot reach a signed kit
    monkeypatch.setattr(dk, "PORTABLE_PY_SHA256", "0" * 64)
    with pytest.raises(ApplyError, match="expected"):
        stage_python(KitContext(infra_dir=tmp_path, out_dir=tmp_path / "kit3",
                                models=[]))

    # a bench that cannot resolve it fails the BUILD with the fix named
    def _boom():
        raise OSError("no cache, no egress")
    monkeypatch.setattr(dk, "_resolve_portable_python", _boom)
    with pytest.raises(ApplyError, match="KH_PORTABLE_PYTHON"):
        stage_python(KitContext(infra_dir=tmp_path, out_dir=tmp_path / "kit2",
                                models=[]))


def test_skipping_python_is_recorded_and_says_what_the_box_must_supply(tmp_path):
    ctx = KitContext(infra_dir=tmp_path, out_dir=tmp_path / "kit", models=[],
                     skip={"python"})
    [line] = stage_python(ctx)
    assert ctx.components["python"] is False
    assert "cp312" in line and "python3.12" in line


def test_launcher_bootstraps_from_the_kit_python_not_the_host(tmp_path):
    script = render_launch_sh()
    # the kit's interpreter is found by a stable glob (no release tag in it)
    assert '"$KIT"/python/cpython-3.12-*.tar.gz' in script
    # extracted onto the BOX, and it must survive: the venv points at it
    assert '"$WORK/.python3.12"' in script
    assert '"$PY" -m venv "$VENV"' in script
    # the pre-BP46 hard pin is gone from the bootstrap
    assert 'python3.12 -m venv' not in script
    # a version assertion guards the cp312 wheelhouse either way
    assert "sys.version_info[:2] == (3, 12)" in script
    # --skip python kits still deploy on a 24.04-style host, and a box with
    # neither gets an honest refusal that names the rebuild first
    assert "using the host python3.12" in script
    assert "rebuild the kit WITH its python component" in script
    assert "cp312-only" in script
    # unpack precedes the venv, which precedes running khctl
    assert script.index("unpacking the kit's portable python") \
        < script.index('"$PY" -m venv') \
        < script.index('"$VENV/bin/khctl" launch')


def _bash() -> str | None:
    """The bash these tests must run under, or None if there is none.

    NOT `shutil.which("bash")`. On Windows that finds whichever bash is
    first on PATH, and on a machine with WSL installed that is usually
    `WindowsApps\\bash.exe` — the Microsoft Store app-execution alias, which
    launches bash INSIDE the WSL distro. Two independent reasons that is the
    wrong interpreter here:

      * `_posix()` below converts paths with `cygpath`, so a Windows temp
        directory becomes `/tmp/pytest-of-.../...`. Under Git Bash `/tmp`
        IS the Windows temp directory and that path resolves. Under WSL
        `/tmp` is the distro's own filesystem, where the harness script does
        not exist. The path convention and the interpreter have to agree.
      * the alias needs the ambient Windows environment to activate, and
        these tests deliberately strip the environment to a bare PATH to
        simulate a hostile host. Stripped, the alias fails to launch at all
        and prints a Windows RPC error where the script's output should be.

    So: pick the bash that agrees with `cygpath`, which is its own sibling.
    One rule, and it is the rule that makes the paths valid. Elsewhere
    (Linux CI, the deploy target) `cygpath` does not exist, there is no
    alias to trip over, and plain PATH resolution is already correct.
    """
    cygpath = shutil.which("cygpath")
    if cygpath:
        sibling = Path(cygpath).with_name("bash.exe")
        if sibling.is_file():
            return str(sibling)
    found = shutil.which("bash")
    if found and "WindowsApps" in found:
        return None  # the alias only; see above — skip rather than mislead
    return found


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None,
                                reason="no POSIX bash on this bench (a WSL "
                                       "app-execution alias does not count)")


@needs_bash
def test_rendered_launcher_is_valid_bash():
    # A launcher that does not parse is a client-site failure, and the string
    # assertions above cannot see a syntax error.
    script = render_launch_sh()
    out = subprocess.run([BASH, "-n"], input=script, text=True,
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr


def _python_selection_harness(script: str) -> str:
    """The launcher's interpreter-selection block, lifted verbatim and made
    runnable: everything from PY="" up to (not including) the venv build."""
    start = script.index('PY=""')
    end = script.index('if [ ! -x "$VENV/bin/khctl" ]')
    return ('set -euo pipefail\nKIT="$1"\nWORK="$2"\n'
            'hold_open() { :; }\n' + script[start:end] +
            '\necho "SELECTED=$PY"\n')


def _hostile_env() -> dict[str, str]:
    """A host with no python3.12 anywhere on PATH — the Ubuntu 26.04
    situation the launcher exists to survive.

    PATH is the whole hypothesis; everything else the environment carries is
    incidental and only ever removed as collateral. On an MSYS bash (Git for
    Windows) one piece of that collateral is load-bearing for the bench:
    MSYS resolves `/tmp` from TMP/TEMP, so dropping them silently moves
    `/tmp` off the Windows temp directory that `_posix()` just translated
    paths into, and the harness script stops existing. That is a bench
    artifact with nothing to say about the launcher, so keep those two and
    strip the rest. On Linux the environment is bare, which is the point.
    """
    env = {"PATH": "/usr/bin:/bin"}
    for name in ("TMP", "TEMP", "SYSTEMROOT"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _posix(path: Path) -> str:
    """Paths handed to bash must be POSIX: GNU tar reads a Windows `C:\\...`
    as a remote host spec. A bench artifact, not a launcher concern (the
    launcher only ever sees Linux paths)."""
    cygpath = shutil.which("cygpath")
    if cygpath:
        out = subprocess.run([cygpath, "-u", str(path)], capture_output=True,
                             text=True, timeout=60)
        if out.returncode == 0:
            return out.stdout.strip()
    return str(path)


def _fake_kit_python(kit: Path) -> None:
    """A kit python/ tarball whose 'interpreter' is a stub that accepts the
    launcher's `-c` version assertion."""
    from knowledge_hub.deploy_kit import PORTABLE_PY_KIT_REL

    target = kit / PORTABLE_PY_KIT_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    stub = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(target, "w:gz") as tar:
        info = tarfile.TarInfo("python/bin/python3.12")
        info.size = len(stub)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(stub))


@needs_bash
def test_launcher_actually_picks_the_kit_python_on_a_hostile_host(tmp_path):
    """RUNS the selection logic (not a string match) with the host python3.12
    removed from PATH: exactly the Ubuntu 26.04 situation that refused."""
    kit, work = tmp_path / "kit", tmp_path / "work"
    work.mkdir()
    _fake_kit_python(kit)
    harness = tmp_path / "select.sh"
    harness.write_text(_python_selection_harness(render_launch_sh()),
                       encoding="utf-8", newline="\n")
    out = subprocess.run(
        [BASH, _posix(harness), _posix(kit), _posix(work)],
        capture_output=True, text=True, timeout=120,
        env=_hostile_env())     # no python3.12 anywhere
    assert out.returncode == 0, out.stderr
    assert "unpacking the kit's portable python" in out.stdout
    assert (f"SELECTED={_posix(work)}/.python3.12/python/bin/python3.12"
            in out.stdout)
    # and it is idempotent: a second run reuses the unpacked interpreter
    again = subprocess.run(
        [BASH, _posix(harness), _posix(kit), _posix(work)],
        capture_output=True, text=True, timeout=120,
        env=_hostile_env())
    assert again.returncode == 0
    assert "unpacking" not in again.stdout


@needs_bash
def test_launcher_refuses_honestly_with_no_kit_python_and_no_host_python(tmp_path):
    kit, work = tmp_path / "kit", tmp_path / "work"
    (kit / "python").mkdir(parents=True)     # present but empty
    work.mkdir()
    harness = tmp_path / "select.sh"
    harness.write_text(_python_selection_harness(render_launch_sh()),
                       encoding="utf-8", newline="\n")
    out = subprocess.run(
        [BASH, _posix(harness), _posix(kit), _posix(work)],
        capture_output=True, text=True, timeout=120,
        env=_hostile_env())
    assert out.returncode == 1
    assert "no usable python 3.12" in out.stdout
    assert "rebuild the kit WITH its python component" in out.stdout
    # the unmatched glob must not crash the script under `set -e`
    assert "No such file" not in out.stderr


# ---------------------------------------------------------------------------
# Fix 5, local-external: on premises, not ours, and never called "remote"
# ---------------------------------------------------------------------------
LOCAL_EXTERNAL = "inference=local-external:http://localhost:11434"


def test_local_external_plan_records_an_on_premises_deploy():
    plan = resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                        use=[LOCAL_EXTERNAL], tenants=["ops"])
    seam = plan.seams["inference"]
    assert seam.choice == "local-external"      # distinct from BOTH old values
    assert seam.endpoint == "http://localhost:11434"
    assert plan.inference_choice() == "local-external"
    assert plan.text_leaves_premises() is False
    locality = plan.data_locality()
    assert locality.startswith("ON PREMISES")
    assert "NOT installed by us" in locality
    # no tier: we did not provision the model, so there is nothing to judge
    assert plan.extraction_tier is None and plan.extraction_model is None
    # and it round-trips as itself
    from knowledge_hub.deploy_profiles import DeployPlan
    assert DeployPlan.from_json(plan.to_json()) == plan


def test_remote_stays_off_premises_and_the_two_never_collapse():
    remote = resolve_plan(PROFILES, "hosted", make_gpu_probe(vram=0),
                          use=["inference=remote:https://infer.example.com"])
    assert remote.text_leaves_premises() is True
    assert remote.data_locality().startswith("OFF PREMISES")
    local_ext = resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                             use=[LOCAL_EXTERNAL])
    assert local_ext.data_locality() != remote.data_locality()
    assert local_ext.seams["inference"].choice != "remote"
    # a plan that is on-premises must never carry the off-premises word
    assert "OFF PREMISES" not in local_ext.data_locality()


def test_local_external_refuses_an_endpoint_that_is_not_on_this_box():
    with pytest.raises(PlanError, match="IS remote inference"):
        resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                     use=["inference=local-external:http://gpu.corp.lan:11434"])
    with pytest.raises(PlanError, match="needs the operator-supplied local"):
        resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                     use=["inference=local-external"])


def test_unknown_inference_mode_lists_all_three_values():
    with pytest.raises(PlanError, match="local \\| local-external \\| remote"):
        resolve_plan(PROFILES, "client-gpu", make_gpu_probe(),
                     use=["inference=borrowed:http://localhost:11434"])


def test_rendered_env_carries_the_seam_word_for_every_mode():
    """Config is where logs and audit answers read it from, so the seam value
    itself lands in .env, never reconstructed from OLLAMA_HOST, which a
    local-external and a remote deploy both carry."""
    local_ext = render_env(
        resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                     use=[LOCAL_EXTERNAL]), PILOT_ENV_DEFAULTS)
    assert "INFERENCE_SEAM=local-external" in local_ext
    assert "OLLAMA_HOST=http://localhost:11434" in local_ext
    remote = render_env(
        resolve_plan(PROFILES, "hosted", make_gpu_probe(vram=0),
                     use=["inference=remote:https://infer.example.com"]),
        PILOT_ENV_DEFAULTS)
    assert "INFERENCE_SEAM=remote" in remote
    ours = render_env(resolve_plan(PROFILES, "appliance", make_gpu_probe()),
                      PILOT_ENV_DEFAULTS)
    assert "INFERENCE_SEAM=local" in ours


def test_settings_expose_the_seam_so_runtime_can_report_it_truthfully(tmp_path,
                                                                     monkeypatch):
    from knowledge_hub.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text("INFERENCE_SEAM=local-external\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().inference_seam == "local-external"
    # the default is the self-contained deploy, never the endpoint cases
    assert Settings.model_fields["inference_seam"].default == "local"


def test_apply_says_whose_models_those_are(tmp_path):
    from knowledge_hub.deploy_apply import ApplyContext, phase_models

    plan = resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                        use=[LOCAL_EXTERNAL])
    env_file = tmp_path / ".env.deploy"
    env_file.write_text(render_env(plan, PILOT_ENV_DEFAULTS), encoding="utf-8")
    [line] = phase_models(ApplyContext(
        plan=plan, infra_dir=tmp_path, kit_dir=tmp_path, env_file=env_file,
        dry_run=True))
    assert "NOT installed by us" in line
    assert "never leaves this box" in line
    assert "remote" not in line          # the wrong story about a local box


def test_verify_selects_the_local_external_check_not_the_remote_one():
    plan = resolve_plan(PROFILES, "client-gpu", make_gpu_probe(vram=0),
                        use=[LOCAL_EXTERNAL], tenants=["ops"])
    names = [name for name, _ in verify_checks_for(plan)]
    assert "local-external inference (operator-supplied)" in names
    assert "remote inference" not in names
    # we installed no model here, so the model-dependent gate is not claimed
    assert "ollama" not in names


def test_local_external_check_refuses_a_non_local_endpoint():
    from knowledge_hub.checks import check_local_external_inference

    with pytest.raises(RuntimeError, match="not on this box"):
        check_local_external_inference("https://infer.example.com")
