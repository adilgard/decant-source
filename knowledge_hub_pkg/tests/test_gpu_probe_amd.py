"""BP46 Fixes 1, 2 and 4, GPU detection on AMD, and the sales fork it fed.

WHAT THIS FILE CAN AND CANNOT PROVE. Pinnacle is an NVIDIA box, so nothing
here is evidence that the kit deploys on AMD hardware. What it does prove is
that the DETECTION LOGIC is correct against numbers CAPTURED from the real
Strix Halo box (node-a, §8.26 spike / §8.27 rehearsal), that the same logic
leaves the CUDA path byte-for-byte unchanged, and that a detection miss can
no longer reach a hardware-sales recommendation. The wet proof is a deploy on
node-a; until then AMD support is UNVERIFIED (§8.28).

The captured node-a numbers, used as the fixture throughout:
    dedicated VRAM   2 GB        (the BIOS carve-out, tiny, and the reason
                                  every tier floor used to fail)
    GTT / shared    62 GB        (the pool the model actually lives in)
    system RAM     121 GB        unified
    running         qwen3.6:35b-a3b-q4_K_M, 23 GB on disk, 21.8 GiB runner,
                    42/42 layers offloaded, 100% GPU, 58.8 tok/s
"""
from __future__ import annotations

import json

import pytest

from knowledge_hub.deploy_probe import (
    GpuDevice,
    GpuReport,
    ProbeReport,
    amd_devices_from_sysfs,
    classify_amd_memory,
    parse_nvidia_smi,
    parse_rocm_smi_json,
    probe_gpu,
    summarize,
)
from knowledge_hub.deploy_profiles import (
    PlanError,
    load_profiles,
    resolve_plan,
    select_tier,
)

from pathlib import Path

INFRA_DIR = Path(__file__).resolve().parents[2]
PROFILES = load_profiles(INFRA_DIR / "profiles.toml")

GB = 1024 ** 3

# --- the captured fixtures -------------------------------------------------
# node-a, AMD Radeon 8060S (gfx1151, Strix Halo iGPU), amdgpu sysfs.
NODE_A_VRAM_BYTES = 2 * GB
NODE_A_GTT_BYTES = 62 * GB
NODE_A_RAM_GB = 121.0

# A pilot NVIDIA box, `nvidia-smi --query-gpu=name,memory.total
# --format=csv,noheader,nounits` (MiB).
NVIDIA_SMI_2xA6000 = ("NVIDIA RTX A6000, 49140\n"
                      "NVIDIA RTX A6000, 49140\n")


def write_amd_sysfs(root: Path, vram_bytes: int = NODE_A_VRAM_BYTES,
                    gtt_bytes: int = NODE_A_GTT_BYTES,
                    vendor: str = "0x1002", card: str = "card0") -> Path:
    """A minimal /sys/class/drm tree in the shape amdgpu exposes."""
    device = root / card / "device"
    device.mkdir(parents=True, exist_ok=True)
    (device / "vendor").write_text(f"{vendor}\n", encoding="ascii")
    (device / "mem_info_vram_total").write_text(f"{vram_bytes}\n",
                                                encoding="ascii")
    (device / "mem_info_gtt_total").write_text(f"{gtt_bytes}\n",
                                               encoding="ascii")
    (device / "uevent").write_text(
        "DRIVER=amdgpu\nPCI_CLASS=30000\nPCI_ID=1002:150E\n"
        "PCI_SLOT_NAME=0000:bd:00.0\n", encoding="ascii")
    return root


# ---------------------------------------------------------------------------
# Fix 1, the probe sees an AMD APU, and counts the pool the model lives in
# ---------------------------------------------------------------------------
def test_amd_apu_is_detected_from_sysfs_with_its_gtt_pool(tmp_path):
    devices = amd_devices_from_sysfs(write_amd_sysfs(tmp_path / "drm"))
    assert len(devices) == 1
    device = devices[0]
    assert device.vendor == "amd"
    assert device.vram_gb == 2.0            # the carve-out, reported honestly
    assert device.gtt_gb == 62.0            # and the pool that matters
    assert device.memory == "unified"
    assert "amdgpu" in device.name and "1002:150E" in device.name


def test_apu_budget_counts_gtt_and_clears_the_moe_floor(tmp_path):
    """THE root-cause assertion. 2 GB of dedicated VRAM fails every tier
    floor in the ladder; 2 + 62 clears the MoE tier that this box measurably
    runs at 58.8 tok/s."""
    from knowledge_hub.deploy_probe import _finish_gpu_report

    report = _finish_gpu_report(
        amd_devices_from_sysfs(write_amd_sysfs(tmp_path / "drm")),
        system_ram_gb=NODE_A_RAM_GB)
    assert report.present
    assert report.memory_class == "unified"
    assert report.vram_gb_total == 2.0      # unchanged meaning: DEDICATED
    assert report.gtt_gb_total == 62.0
    assert report.budget_gb == 64.0         # 2 + 62, under the RAM clamp
    assert "GTT" in (report.memory_evidence or "")

    # dedicated VRAM alone fits nothing; the real budget fits the MoE tier
    assert select_tier(PROFILES.tiers, vram_gb_budget=report.vram_gb_total,
                       memory_class="unified") is None
    tier = select_tier(PROFILES.tiers, vram_gb_budget=report.budget_gb,
                       memory_class="unified")
    assert tier is not None
    assert tier.extraction_model == "qwen3.6:35b-a3b-q4_K_M"
    assert tier.vram_gb <= 62.0             # the floor fits the GTT pool


def test_unified_budget_is_clamped_by_system_ram(tmp_path):
    # On a unified box every byte of both pools IS system RAM, so the budget
    # may never exceed what the OS can spare. 8 + 200 with only 64 GB RAM is
    # a driver-reported fantasy; the clamp keeps the plan honest.
    from knowledge_hub.deploy_probe import _finish_gpu_report

    devices = amd_devices_from_sysfs(write_amd_sysfs(
        tmp_path / "drm", vram_bytes=8 * GB, gtt_bytes=200 * GB))
    report = _finish_gpu_report(devices, system_ram_gb=64.0)
    assert report.memory_class == "unified"
    assert report.budget_gb == 48.0          # 75% of 64, not 208
    assert "clamped" in report.memory_evidence


def test_non_amd_and_unreadable_sysfs_are_skipped_not_guessed(tmp_path):
    # An Intel iGPU card node must not be read as an AMD budget.
    intel = write_amd_sysfs(tmp_path / "intel", vendor="0x8086")
    assert amd_devices_from_sysfs(intel) == []
    # A card dir with no amdgpu memory files at all -> nothing claimed.
    bare = tmp_path / "bare" / "card0" / "device"
    bare.mkdir(parents=True)
    (bare / "vendor").write_text("0x1002\n", encoding="ascii")
    assert amd_devices_from_sysfs(tmp_path / "bare") == []
    # A missing /sys tree (this Windows bench, or a container) degrades to
    # empty rather than raising, a probe that crashes failed at its one job.
    assert amd_devices_from_sysfs(tmp_path / "nope") == []


def test_memory_class_ratio_keeps_discrete_amd_cards_dedicated():
    # APU: 62 GB GTT against a 2 GB carve-out (31x) -> unified.
    assert classify_amd_memory(2.0, 62.0) == "unified"
    # Discrete 8 GB card with a 32 GB GTT aperture (4x) -> still dedicated,
    # so its budget stays its real VRAM and no tier spills over PCIe.
    assert classify_amd_memory(8.0, 32.0) == "dedicated"
    # Discrete card with no GTT reported at all.
    assert classify_amd_memory(24.0, 0.0) == "dedicated"


def test_rocm_smi_json_is_parsed_when_installed():
    """node-a had NO system ROCm (Ollama's bundled rocm_v7_2 ran the spike),
    which is why sysfs owns the numbers. rocm-smi is still parsed when it is
    there, because it carries the real product name."""
    text = json.dumps({"card0": {
        "Card Series": "Radeon 8060S Graphics",
        "VRAM Total Memory (B)": str(NODE_A_VRAM_BYTES),
        "VRAM Total Used Memory (B)": str(1 * GB),
        "GTT Total Memory (B)": str(NODE_A_GTT_BYTES)}})
    [device] = parse_rocm_smi_json(text)
    assert device.name == "Radeon 8060S Graphics"
    assert (device.vram_gb, device.gtt_gb) == (2.0, 62.0)
    assert device.memory == "unified"
    # junk in, nothing out, never a partial device
    assert parse_rocm_smi_json("not json") == []
    assert parse_rocm_smi_json(json.dumps({"card0": {"Temp": "45"}})) == []


# ---------------------------------------------------------------------------
# Fix 1, and the NVIDIA path is untouched (the regression proof)
# ---------------------------------------------------------------------------
def test_nvidia_fixture_reports_exactly_as_before():
    from knowledge_hub.deploy_probe import _finish_gpu_report

    devices = parse_nvidia_smi(NVIDIA_SMI_2xA6000)
    report = _finish_gpu_report(devices, system_ram_gb=None)
    assert [d.name for d in devices] == ["NVIDIA RTX A6000"] * 2
    assert report.vram_gb_total == 96.0        # 2 x 48.0
    assert report.vram_gb_max_single == 48.0
    assert report.memory_class == "dedicated"
    # the budget IS the dedicated total on CUDA: no GTT, no clamp, no change
    assert report.budget_gb == report.vram_gb_total
    assert report.gtt_gb_total == 0.0


def test_nvidia_box_still_selects_the_dense_bf16_tier():
    # The ladder on a CUDA box must be indifferent to everything BP46 added:
    # the new MoE tier is scoped memory="unified" precisely so a
    # dedicated-VRAM box can never reach it.
    tier = select_tier(PROFILES.tiers, vram_gb_budget=96.0,
                       memory_class="dedicated")
    assert tier.name == "fp16_27b"
    assert select_tier(PROFILES.tiers, vram_gb_budget=32.0,
                       memory_class="dedicated") is None
    assert select_tier(PROFILES.tiers, vram_gb_budget=32.0,
                       memory_class="dedicated",
                       allow_gated=True).name == "quant_27b"
    # ... and the MoE tier is not reachable on dedicated VRAM at any size,
    # including 26 GB, where it would otherwise be the only fitting tier
    # (that box still gets the Scenario-2 conversation it got before BP46).
    for budget in (26.0, 64.0, 192.0):
        for gated in (False, True):
            selected = select_tier(PROFILES.tiers, vram_gb_budget=budget,
                                   memory_class="dedicated",
                                   allow_gated=gated)
            assert selected is None or selected.name != "moe_35b_a3b_q4"


def test_probe_gpu_prefers_nvidia_then_amd_then_says_neither(monkeypatch):
    import knowledge_hub.deploy_probe as dp

    nvidia = GpuReport(present=True, devices=[
        GpuDevice(name="NVIDIA RTX A6000", vram_gb=48.0)],
        vram_gb_total=48.0, vram_gb_max_single=48.0)
    amd = GpuReport(present=True, devices=[
        GpuDevice(name="AMD GPU (amdgpu)", vendor="amd", vram_gb=2.0,
                  gtt_gb=62.0, memory="unified")],
        vram_gb_total=2.0, vram_gb_max_single=2.0, memory_class="unified",
        gtt_gb_total=62.0, vram_gb_budget=64.0)

    monkeypatch.setattr(dp, "probe_nvidia_gpu", lambda: nvidia)
    monkeypatch.setattr(dp, "probe_amd_gpu", lambda: amd)
    assert probe_gpu().devices[0].name == "NVIDIA RTX A6000"

    monkeypatch.setattr(dp, "probe_nvidia_gpu",
                        lambda: GpuReport(present=False, error="nvidia-smi not found"))
    assert probe_gpu().memory_class == "unified"

    monkeypatch.setattr(dp, "probe_amd_gpu",
                        lambda: GpuReport(present=False, error="no AMD GPU"))
    none = probe_gpu()
    assert not none.present
    # both attempts named in one error: the operator sees WHAT was looked for
    assert "no supported GPU detected" in none.error
    assert "nvidia-smi not found" in none.error and "no AMD GPU" in none.error


def test_summary_prints_the_budget_and_never_says_gpu_none(tmp_path):
    from knowledge_hub.deploy_probe import HostReport, _finish_gpu_report

    gpu = _finish_gpu_report(
        amd_devices_from_sysfs(write_amd_sysfs(tmp_path / "drm")),
        system_ram_gb=NODE_A_RAM_GB)
    text = summarize(ProbeReport(
        host=HostReport(os="Linux 7.0.0-28-generic", machine="x86_64"),
        gpu=gpu))
    assert "62.0GB gtt" in text
    assert "tier budget: 64.0GB" in text
    assert "unified" in text
    assert "NONE" not in text          # the old line the rehearsal saw

    missing = summarize(ProbeReport(
        host=HostReport(os="Linux", machine="x86_64"),
        gpu=GpuReport(present=False, error="nvidia: x; amd: y")))
    # honest and specific: a detection outcome, not a hardware verdict
    assert "NO SUPPORTED GPU DETECTED" in missing


def test_pre_bp46_probe_report_still_plans():
    """Old probe_report.json files carry no budget field. They must keep
    meaning what they meant (budget = dedicated VRAM), not read as 0."""
    old = GpuReport(present=True, devices=[
        GpuDevice(name="NVIDIA RTX A6000", vram_gb=48.0)],
        vram_gb_total=48.0, vram_gb_max_single=48.0)
    assert old.vram_gb_budget is None
    assert old.budget_gb == 48.0


# ---------------------------------------------------------------------------
# Fixes 1 + 2 + 4 together, the end-to-end AMD plan, and no sales fork
# ---------------------------------------------------------------------------
def amd_probe(tmp_path) -> ProbeReport:
    from knowledge_hub.deploy_probe import HostReport, _finish_gpu_report

    return ProbeReport(
        probed_at="2026-07-29T00:00:00+00:00",
        host=HostReport(os="Linux 7.0.0-28-generic", machine="x86_64",
                        cpu_count=16, ram_gb=NODE_A_RAM_GB, docker=True,
                        docker_compose=True),
        gpu=_finish_gpu_report(
            amd_devices_from_sysfs(write_amd_sysfs(tmp_path / "drm")),
            system_ram_gb=NODE_A_RAM_GB))


def test_amd_apu_plan_selects_the_moe_tier(tmp_path):
    plan = resolve_plan(PROFILES, "appliance", amd_probe(tmp_path),
                        tenants=["ops"])
    assert plan.extraction_tier == "moe_35b_a3b_q4"
    assert plan.extraction_model == "qwen3.6:35b-a3b-q4_K_M"
    assert plan.seams["inference"].choice == "local"
    # no gate flag needed: the tier is default-status, not Axis-D-gated
    assert not plan.seams["inference"].operator_override


def test_amd_apu_never_triggers_the_sell_a_gpu_path(tmp_path):
    """THE regression test for Fix 2. This box has working hardware; nothing
    in the deploy path may offer to sell it hardware."""
    from knowledge_hub.deploy_profiles import SCENARIO_2_OPTIONS

    probe = amd_probe(tmp_path)
    plan = resolve_plan(PROFILES, "appliance", probe, tenants=["ops"])
    # planning SUCCEEDS, so the fork is not reachable at all
    assert plan.extraction_tier == "moe_35b_a3b_q4"
    assert "sell" in SCENARIO_2_OPTIONS      # the text still exists...
    assert "sell" not in plan.to_json()      # ...and never reaches this plan
    assert "appliance" not in summarize(probe)


def test_amd_apu_reaches_the_moe_tier_on_the_client_gpu_profile(tmp_path):
    # The offering a client-owned Strix Halo box actually walks in under.
    plan = resolve_plan(PROFILES, "client-gpu", amd_probe(tmp_path),
                        tenants=["ops"])
    assert plan.extraction_model == "qwen3.6:35b-a3b-q4_K_M"


def test_shipped_moe_tier_is_pinned_and_fits_the_captured_box():
    """profiles.toml is kit DATA and is itself under test, the floor and the
    tag are what the AMD box will actually get."""
    tier = {t.name: t for t in PROFILES.tiers}["moe_35b_a3b_q4"]
    assert tier.extraction_model == "qwen3.6:35b-a3b-q4_K_M"  # exact tag
    assert tier.status == "default"
    assert tier.memory == "unified"
    # 21.8 GiB measured runner <= floor <= the captured 62 GB GTT pool
    assert 22.0 <= tier.vram_gb <= 62.0
    # the dense tiers stay dedicated-only, which is what protects CUDA
    for name in ("fp16_27b", "quant_27b"):
        assert {t.name: t for t in PROFILES.tiers}[name].memory == "dedicated"


def test_a_gpuless_box_stops_before_any_commercial_conversation():
    from knowledge_hub.deploy_probe import HostReport

    probe = ProbeReport(
        host=HostReport(os="Linux", machine="x86_64"),
        gpu=GpuReport(present=False,
                      error="no supported GPU detected, nvidia: "
                            "nvidia-smi not found; amd: no AMD GPU"))
    with pytest.raises(PlanError) as stopped:
        resolve_plan(PROFILES, "appliance", probe)
    assert "bring/sell" not in str(stopped.value)
    assert "--confirm-no-gpu" in str(stopped.value)
