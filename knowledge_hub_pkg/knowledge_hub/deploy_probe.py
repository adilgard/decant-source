"""Pre-flight environment probe — the plug-and-play mechanism (§8.9).

STRICTLY READ-ONLY. The probe inspects a client environment and reports; it
never creates, writes, or mutates anything. Anything that requires a write to
prove (e.g. WORM enforcement needs a sacrificial object) is reported as
`unknown` here and proven later by `khctl verify` against whatever the plan
actually adopted — fail-closed: unknown never qualifies a component.

Two jobs, deliberately separated in the report:
  DISCOVERY      — what exists (GPU, Postgres, object store, vault, Ollama,
                   docker, RAM/disk, egress).
  QUALIFICATION  — raw evidence a "theirs" adoption bar is judged against
                   (extension availability, object-lock config, versions).
                   The judging itself lives in deploy_profiles.py, driven by
                   the rules in profiles.toml — data, not code.

Every section degrades gracefully: an unreachable service or missing binary
becomes `found=False` / an `error` string, never an exception. A probe that
crashes on a weird client box is a probe that failed at its one job.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

# "2" (BP46): the GPU section gained the AMD / unified-memory fields
# (vendor, gtt_gb, memory_class, vram_gb_budget, system_ram_gb). Old reports
# stay readable — every new field defaults, and vram_gb_budget=None means
# "pre-BP46, the budget IS vram_gb_total".
PROBE_VERSION = "2"

# Ports the "ours" stack wants (docker-compose + serving defaults).
DEFAULT_PORTS = (5432, 8333, 8200, 11434, 8080)

_SUBPROCESS_TIMEOUT = 15
_NET_TIMEOUT = 5

# --- AMD detection (BP46 Fix 1) --------------------------------------------
# amdgpu exposes its memory pools per DRM card, with no dependency on a
# system ROCm install — which matters: the §8.26 spike ran on Ollama's
# BUNDLED rocm_v7_2 and node-a has no rocm-smi at all. sysfs is therefore
# the primary source of NUMBERS; rocm-smi, when present, only enriches the
# device NAME (and is the fallback when sysfs is unreadable).
DRM_ROOT = Path("/sys/class/drm")
AMD_VENDOR_ID = "0x1002"
# A unified-memory APU's dedicated carve-out is tiny next to its GTT pool
# (node-a: 2 GB dedicated vs 62 GB GTT = 31x). A discrete card's GTT is a
# fraction-of-RAM aperture and never dwarfs its own VRAM like that, so the
# ratio is the discriminator. Deliberately wide: 8x keeps an 8 GB discrete
# card with a 32 GB aperture (4x) classified as DEDICATED.
UNIFIED_GTT_RATIO = 8.0
# On a unified box every byte of "VRAM" and GTT is system RAM, so the budget
# can never honestly exceed what the OS can spare. Clamp, do not trust.
UNIFIED_RAM_FRACTION = 0.75


# ---------------------------------------------------------------------------
# Report shapes — the probe_report.json contract
# ---------------------------------------------------------------------------
class GpuDevice(BaseModel):
    name: str
    # DEDICATED video memory as the driver reports it. On a unified-memory
    # APU this is the BIOS carve-out (node-a: 2 GB) and is NOT the usable
    # budget — gtt_gb is where the real pool lives.
    vram_gb: float
    vendor: Literal["nvidia", "amd"] = "nvidia"
    # GTT / shared system RAM the GPU can address. 0.0 on NVIDIA (the probe
    # does not read CUDA's host-memory fallback: spilling a model to host
    # RAM over PCIe is a failure mode there, not a budget).
    gtt_gb: float = 0.0
    memory: Literal["dedicated", "unified"] = "dedicated"

    @property
    def budget_gb(self) -> float:
        return round(self.vram_gb + self.gtt_gb, 1)


class GpuReport(BaseModel):
    present: bool = False
    devices: list[GpuDevice] = Field(default_factory=list)
    # Sum of DEDICATED vram only — unchanged meaning, so an NVIDIA report
    # reads exactly as it did before BP46.
    vram_gb_total: float = 0.0
    vram_gb_max_single: float = 0.0
    # --- BP46: the unified-memory APU story, reported not inferred --------
    memory_class: Literal["dedicated", "unified"] = "dedicated"
    memory_evidence: Optional[str] = None      # why that class, in numbers
    gtt_gb_total: float = 0.0
    system_ram_gb: Optional[float] = None
    # THE number the tier ladder consults. Equal to vram_gb_total on a
    # dedicated-VRAM box; dedicated + GTT (RAM-clamped) on a unified APU,
    # because a Strix Halo that visibly runs a 23 GB model must not fail
    # every tier floor on the strength of its 2 GB carve-out.
    vram_gb_budget: Optional[float] = None
    error: Optional[str] = None

    @property
    def budget_gb(self) -> float:
        """The tier ladder's input. None = a pre-BP46 report, where the
        budget WAS vram_gb_total — fall back rather than read 0.0."""
        return (self.vram_gb_total if self.vram_gb_budget is None
                else self.vram_gb_budget)


class PostgresReport(BaseModel):
    dsn_redacted: str
    reachable: bool = False
    server_version: Optional[str] = None
    major_version: Optional[int] = None
    is_superuser: Optional[bool] = None
    # name -> available to CREATE EXTENSION (pg_available_extensions)
    ext_available: dict[str, bool] = Field(default_factory=dict)
    # name -> already installed (installed_version not null)
    ext_installed: dict[str, bool] = Field(default_factory=dict)
    error: Optional[str] = None


class ObjectStoreReport(BaseModel):
    endpoint: str
    reachable: bool = False
    bucket: Optional[str] = None
    # None = could not determine read-only (no bucket to inspect) -> unknown.
    object_lock: Optional[bool] = None
    versioning: Optional[bool] = None
    error: Optional[str] = None


class SecretsReport(BaseModel):
    addr: str
    reachable: bool = False
    initialized: Optional[bool] = None
    sealed: Optional[bool] = None
    error: Optional[str] = None


class OllamaReport(BaseModel):
    host: str
    reachable: bool = False
    version: Optional[str] = None
    models: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class HostReport(BaseModel):
    os: str
    machine: str
    cpu_count: Optional[int] = None
    ram_gb: Optional[float] = None
    disk_free_gb: Optional[float] = None
    docker: bool = False
    docker_compose: bool = False
    # port -> True means something is already listening (busy for "ours").
    ports_listening: dict[int, bool] = Field(default_factory=dict)


class EgressReport(BaseModel):
    # target url -> reachable; empty dict = no targets configured.
    targets: dict[str, bool] = Field(default_factory=dict)

    @property
    def any_egress(self) -> bool:
        return any(self.targets.values())


class ProbeReport(BaseModel):
    probe_version: str = PROBE_VERSION
    probed_at: str = ""
    host: HostReport
    gpu: GpuReport
    postgres: list[PostgresReport] = Field(default_factory=list)
    object_store: list[ObjectStoreReport] = Field(default_factory=list)
    secrets: list[SecretsReport] = Field(default_factory=list)
    ollama: list[OllamaReport] = Field(default_factory=list)
    egress: EgressReport = Field(default_factory=EgressReport)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> "ProbeReport":
        return cls.model_validate(json.loads(text))


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------
def parse_nvidia_smi(stdout: str) -> list[GpuDevice]:
    """`nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,
    nounits` -> devices. Pure, so a captured CUDA fixture is unit-testable
    (BP46: the NVIDIA path must be provably unregressed on a bench that has
    no AMD hardware, and vice versa)."""
    devices = []
    for line in stdout.strip().splitlines():
        name, _, mem = line.rpartition(",")
        devices.append(GpuDevice(name=name.strip(), vendor="nvidia",
                                 vram_gb=round(float(mem) / 1024, 1),
                                 memory="dedicated"))
    return devices


def probe_nvidia_gpu() -> GpuReport:
    """The CUDA path, behaviour-identical to the pre-BP46 probe_gpu()."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return GpuReport(present=False, error="nvidia-smi not found")
    try:
        out = subprocess.run(
            [smi, "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
        if out.returncode != 0:
            return GpuReport(present=False,
                             error=out.stderr.strip() or "nvidia-smi failed")
        devices = parse_nvidia_smi(out.stdout)
        if not devices:
            return GpuReport(present=False, error="nvidia-smi listed no GPUs")
        return _finish_gpu_report(devices, system_ram_gb=None)
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        return GpuReport(present=False, error=f"{type(e).__name__}: {e}")


# --- AMD -------------------------------------------------------------------
def _read_sysfs_int(path: Path) -> Optional[int]:
    try:
        return int(path.read_text(encoding="ascii").strip(), 0)
    except (OSError, ValueError):
        return None


def _sysfs_device_name(device_dir: Path) -> str:
    """A name from sysfs alone. amdgpu exports no marketing string, so the
    PCI id is the honest answer; rocm-smi upgrades it when installed."""
    pci_id = None
    try:
        for line in (device_dir / "uevent").read_text(
                encoding="ascii", errors="replace").splitlines():
            if line.startswith("PCI_ID="):
                pci_id = line.split("=", 1)[1].strip()
    except OSError:
        pass
    return f"AMD GPU (amdgpu{f', pci {pci_id}' if pci_id else ''})"


def amd_devices_from_sysfs(drm_root: Path = DRM_ROOT) -> list[GpuDevice]:
    """Read amdgpu's memory pools out of /sys/class/drm/card*/device/.

    Pure over the filesystem tree, so the captured node-a numbers (2 GB
    mem_info_vram_total, 62 GB mem_info_gtt_total) drive a unit test on a
    bench with no AMD hardware. Needs no ROCm install: node-a ran the spike
    on Ollama's bundled rocm_v7_2 and has no rocm-smi (§8.26)."""
    devices: list[GpuDevice] = []
    try:
        cards = sorted(p for p in drm_root.glob("card*")
                       if p.name[4:].isdigit())
    except OSError:
        return devices
    for card in cards:
        device_dir = card / "device"
        vendor = None
        try:
            vendor = (device_dir / "vendor").read_text(
                encoding="ascii").strip().lower()
        except OSError:
            continue
        if vendor != AMD_VENDOR_ID:
            continue
        vram_bytes = _read_sysfs_int(device_dir / "mem_info_vram_total")
        if vram_bytes is None:
            continue                      # not an amdgpu render node
        gtt_bytes = _read_sysfs_int(device_dir / "mem_info_gtt_total") or 0
        devices.append(GpuDevice(
            name=_sysfs_device_name(device_dir), vendor="amd",
            vram_gb=round(vram_bytes / 1024**3, 1),
            gtt_gb=round(gtt_bytes / 1024**3, 1),
            memory=classify_amd_memory(round(vram_bytes / 1024**3, 1),
                                       round(gtt_bytes / 1024**3, 1))))
    return devices


def classify_amd_memory(vram_gb: float, gtt_gb: float) -> str:
    """dedicated | unified. See UNIFIED_GTT_RATIO for why the ratio decides.
    The classification is REPORTED with its evidence (memory_evidence) so an
    operator can see the call the probe made rather than trust it."""
    if gtt_gb > 0 and vram_gb > 0 and gtt_gb >= UNIFIED_GTT_RATIO * vram_gb:
        return "unified"
    if gtt_gb > 0 and vram_gb == 0:
        return "unified"
    return "dedicated"


def parse_rocm_smi_json(text: str) -> list[GpuDevice]:
    """`rocm-smi --showmeminfo vram gtt --showproductname --json` -> devices.
    Key spellings drift across ROCm versions, so match on substrings rather
    than exact keys, and skip a card whose VRAM total cannot be read."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    devices: list[GpuDevice] = []
    for card, fields in sorted(data.items()):
        if not card.lower().startswith("card") or not isinstance(fields, dict):
            continue

        def _find(*needles: str) -> Optional[str]:
            for key, value in fields.items():
                low = key.lower()
                if all(n in low for n in needles):
                    return str(value)
            return None

        vram = _find("vram", "total", "memory")
        if vram is None:
            continue
        gtt = _find("gtt", "total", "memory")
        try:
            vram_gb = round(int(str(vram).strip()) / 1024**3, 1)
            gtt_gb = (round(int(str(gtt).strip()) / 1024**3, 1)
                      if gtt is not None else 0.0)
        except ValueError:
            continue
        name = (_find("card", "series") or _find("card", "model")
                or _find("device", "name") or f"AMD GPU ({card})")
        devices.append(GpuDevice(
            name=name.strip(), vendor="amd", vram_gb=vram_gb, gtt_gb=gtt_gb,
            memory=classify_amd_memory(vram_gb, gtt_gb)))
    return devices


def _rocm_smi_devices() -> list[GpuDevice]:
    smi = shutil.which("rocm-smi")
    if smi is None:
        return []
    try:
        out = subprocess.run(
            [smi, "--showmeminfo", "vram", "gtt", "--showproductname",
             "--json"],
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return []
    if out.returncode != 0:
        return []
    return parse_rocm_smi_json(out.stdout)


def probe_amd_gpu() -> GpuReport:
    """The ROCm/amdgpu path. sysfs owns the numbers (always there when the
    driver is loaded); rocm-smi supplies real product names when installed,
    and the numbers too if sysfs could not be read."""
    devices = amd_devices_from_sysfs()
    named = _rocm_smi_devices()
    if devices and named:
        for i, device in enumerate(devices):
            if i < len(named) and named[i].name:
                devices[i] = device.model_copy(update={"name": named[i].name})
    elif not devices:
        devices = named
    if not devices:
        return GpuReport(
            present=False,
            error=f"no AMD GPU in {DRM_ROOT} (amdgpu vendor {AMD_VENDOR_ID}) "
                  f"and rocm-smi reported none")
    return _finish_gpu_report(devices, system_ram_gb=_ram_gb())


def _finish_gpu_report(devices: list[GpuDevice],
                       system_ram_gb: Optional[float]) -> GpuReport:
    """Roll devices up into the report, including THE budget the tier ladder
    consults. Unified boxes count GTT (that is the whole point of Fix 1) but
    never claim more than the box's RAM can back."""
    vram_total = round(sum(d.vram_gb for d in devices), 1)
    gtt_total = round(sum(d.gtt_gb for d in devices), 1)
    unified = any(d.memory == "unified" for d in devices)
    if not unified:
        budget = vram_total
        evidence = (f"dedicated VRAM {vram_total}GB"
                    + (f", GTT {gtt_total}GB not counted" if gtt_total
                       else ""))
    else:
        raw = round(vram_total + gtt_total, 1)
        budget = raw
        evidence = (f"unified memory: {vram_total}GB dedicated + "
                    f"{gtt_total}GB GTT/shared = {raw}GB")
        if system_ram_gb:
            clamp = round(system_ram_gb * UNIFIED_RAM_FRACTION, 1)
            if clamp < raw:
                budget = clamp
                evidence += (f", clamped to {clamp}GB "
                             f"({UNIFIED_RAM_FRACTION:.0%} of {system_ram_gb}"
                             f"GB system RAM: on a unified box every byte "
                             f"is system RAM)")
    return GpuReport(
        present=True, devices=devices,
        vram_gb_total=vram_total,
        vram_gb_max_single=max(d.vram_gb for d in devices),
        memory_class="unified" if unified else "dedicated",
        memory_evidence=evidence,
        gtt_gb_total=gtt_total,
        system_ram_gb=system_ram_gb,
        vram_gb_budget=budget)


def probe_gpu() -> GpuReport:
    """NVIDIA first (unchanged: the appliance BOM and every pilot box), then
    AMD/amdgpu (BP46 Fix 1 — a Strix Halo box running a 23 GB model on its
    GPU used to probe as `gpu: NONE` and escalate into a sales fork).

    A report with present=False now means exactly one thing: NO SUPPORTED
    GPU WAS RECOGNIZED. It is a detection result, never a hardware verdict —
    resolve_plan stops on it for operator confirmation instead of offering
    to sell a GPU (Fix 2)."""
    nvidia = probe_nvidia_gpu()
    if nvidia.present:
        return nvidia
    amd = probe_amd_gpu()
    if amd.present:
        return amd
    return GpuReport(
        present=False,
        error=f"no supported GPU detected. nvidia: {nvidia.error}; "
              f"amd: {amd.error}")


def _redact_dsn(dsn: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(dsn)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@")
        return dsn.replace(parts.netloc, netloc)
    return dsn


def probe_postgres(dsn: str,
                   extensions: tuple[str, ...] = ("vector", "age", "pg_trgm"),
                   ) -> PostgresReport:
    report = PostgresReport(dsn_redacted=_redact_dsn(dsn))
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=_NET_TIMEOUT) as conn:
            report.reachable = True
            report.server_version = conn.execute(
                "SHOW server_version").fetchone()[0]
            report.major_version = int(report.server_version.split(".")[0])
            report.is_superuser = conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT name, installed_version IS NOT NULL"
                "  FROM pg_available_extensions WHERE name = ANY(%s)",
                (list(extensions),)).fetchall()
            found = {name: installed for name, installed in rows}
            for ext in extensions:
                report.ext_available[ext] = ext in found
                report.ext_installed[ext] = found.get(ext, False)
    except Exception as e:  # any failure = not reachable, with the reason
        report.error = f"{type(e).__name__}: {e}"
    return report


def probe_object_store(endpoint: str, access_key: str, secret_key: str,
                       bucket: str | None = None) -> ObjectStoreReport:
    """Read-only S3 probe. Object-lock/versioning can only be READ off an
    existing bucket; with no bucket to inspect they stay None (unknown), and
    unknown never qualifies — verify proves enforcement post-adoption with
    S3RawStore.verify_worm() (which needs a sacrificial write)."""
    report = ObjectStoreReport(endpoint=endpoint, bucket=bucket)
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3", endpoint_url=endpoint,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=Config(connect_timeout=_NET_TIMEOUT,
                          read_timeout=_NET_TIMEOUT,
                          retries={"max_attempts": 1}))
        client.list_buckets()
        report.reachable = True
        if bucket:
            try:
                lock = client.get_object_lock_configuration(Bucket=bucket)
                report.object_lock = (
                    lock.get("ObjectLockConfiguration", {})
                    .get("ObjectLockEnabled") == "Enabled")
            except Exception:
                report.object_lock = False
            ver = client.get_bucket_versioning(Bucket=bucket)
            report.versioning = ver.get("Status") == "Enabled"
    except Exception as e:
        report.error = f"{type(e).__name__}: {e}"
    return report


def probe_secrets(addr: str) -> SecretsReport:
    """GET /v1/sys/health is unauthenticated and read-only on both OpenBao
    and Vault. Non-200 codes still carry the health body (503 = sealed,
    501 = uninitialized), so read the body regardless of status."""
    report = SecretsReport(addr=addr)
    try:
        req = urllib.request.Request(
            f"{addr.rstrip('/')}/v1/sys/health", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            body = json.load(e)
        report.reachable = True
        report.initialized = body.get("initialized")
        report.sealed = body.get("sealed")
    except Exception as e:
        report.error = f"{type(e).__name__}: {e}"
    return report


def probe_ollama(host: str) -> OllamaReport:
    report = OllamaReport(host=host)
    base = host.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/api/version",
                                    timeout=_NET_TIMEOUT) as r:
            report.version = json.load(r).get("version")
        report.reachable = True
        with urllib.request.urlopen(f"{base}/api/tags",
                                    timeout=_NET_TIMEOUT) as r:
            report.models = sorted(
                m["name"] for m in json.load(r).get("models", []))
    except Exception as e:
        report.error = f"{type(e).__name__}: {e}"
    return report


def _ram_gb() -> Optional[float]:
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) / 1024 / 1024, 1)
        elif platform.system() == "Windows":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _MemStatus(dwLength=ctypes.sizeof(_MemStatus))
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.ullTotalPhys / 1024**3, 1)
    except Exception:
        pass
    return None


def _docker_ok(args: list[str]) -> bool:
    try:
        return subprocess.run(args, capture_output=True,
                              timeout=_SUBPROCESS_TIMEOUT).returncode == 0
    except Exception:
        return False


def probe_host(ports: tuple[int, ...] = DEFAULT_PORTS) -> HostReport:
    listening: dict[int, bool] = {}
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                listening[port] = True
        except OSError:
            listening[port] = False
    return HostReport(
        os=f"{platform.system()} {platform.release()}",
        machine=platform.machine(),
        cpu_count=os.cpu_count(),
        ram_gb=_ram_gb(),
        disk_free_gb=round(shutil.disk_usage(".").free / 1024**3, 1),
        docker=shutil.which("docker") is not None
               and _docker_ok(["docker", "version"]),
        docker_compose=_docker_ok(["docker", "compose", "version"]),
        ports_listening=listening)


def probe_egress(targets: list[str]) -> EgressReport:
    results: dict[str, bool] = {}
    for target in targets:
        try:
            req = urllib.request.Request(target, method="HEAD")
            urllib.request.urlopen(req, timeout=_NET_TIMEOUT)
            results[target] = True
        except urllib.error.HTTPError:
            results[target] = True  # an HTTP status IS reachability
        except Exception:
            results[target] = False
    return EgressReport(targets=results)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def run_probe(postgres_dsns: list[str] | None = None,
              s3_endpoints: list[tuple[str, str, str, str | None]] | None = None,
              vault_addrs: list[str] | None = None,
              ollama_hosts: list[str] | None = None,
              egress_targets: list[str] | None = None) -> ProbeReport:
    """One full sweep. Candidate lists come from CLI flags / profiles.toml;
    the CLI seeds pilot-default localhost candidates so a box already running
    the stack (or pieces of it) is discovered without any flags."""
    return ProbeReport(
        probed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        host=probe_host(),
        gpu=probe_gpu(),
        postgres=[probe_postgres(dsn) for dsn in (postgres_dsns or [])],
        object_store=[probe_object_store(e, ak, sk, b)
                      for e, ak, sk, b in (s3_endpoints or [])],
        secrets=[probe_secrets(a) for a in (vault_addrs or [])],
        ollama=[probe_ollama(h) for h in (ollama_hosts or [])],
        egress=probe_egress(egress_targets or []))


def summarize(report: ProbeReport) -> str:
    """Human summary printed after the JSON is written — the operator's
    walk-in glance, not the machine contract."""
    lines = ["Knowledge Hub — environment probe", "-" * 44]
    h = report.host
    lines.append(
        f"host: {h.os} ({h.machine}), {h.cpu_count} cpu, "
        f"ram={h.ram_gb}GB, disk_free={h.disk_free_gb}GB, "
        f"docker={'yes' if h.docker else 'NO'}"
        f"{' (+compose)' if h.docker_compose else ''}")
    busy = [str(p) for p, on in h.ports_listening.items() if on]
    lines.append(f"ports listening: {', '.join(busy) if busy else 'none'}")
    gpu = report.gpu
    if gpu.present:
        names = ", ".join(
            f"{d.name} ({d.vram_gb}GB vram"
            + (f" + {d.gtt_gb}GB gtt" if d.gtt_gb else "") + ")"
            for d in gpu.devices)
        lines.append(f"gpu: {names}")
        # The budget is what the tier ladder actually judges, so print it
        # WITH its evidence — on a unified box the headline vram number is
        # the least useful of the three (BP46).
        lines.append(f"     memory class: {gpu.memory_class}, "
                     f"tier budget: {gpu.budget_gb}GB"
                     + (f"  [{gpu.memory_evidence}]" if gpu.memory_evidence
                        else ""))
    else:
        # Wording matters: this is a DETECTION result. It is not evidence
        # that the box lacks a GPU, and nothing downstream may treat it as
        # a reason to recommend buying hardware (BP46 Fix 2).
        lines.append(f"gpu: NO SUPPORTED GPU DETECTED ({gpu.error})")
    for pg in report.postgres:
        if pg.reachable:
            exts = ", ".join(f"{k}={'y' if v else 'N'}"
                             for k, v in pg.ext_available.items())
            lines.append(f"postgres: {pg.dsn_redacted} v{pg.server_version} "
                         f"superuser={pg.is_superuser} ext[{exts}]")
        else:
            lines.append(f"postgres: {pg.dsn_redacted} UNREACHABLE "
                         f"({pg.error})")
    for s3 in report.object_store:
        if s3.reachable:
            lines.append(
                f"object store: {s3.endpoint} reachable, "
                f"bucket={s3.bucket or '-'} lock={s3.object_lock} "
                f"versioning={s3.versioning}")
        else:
            lines.append(f"object store: {s3.endpoint} UNREACHABLE "
                         f"({s3.error})")
    for v in report.secrets:
        state = (f"initialized={v.initialized} sealed={v.sealed}"
                 if v.reachable else f"UNREACHABLE ({v.error})")
        lines.append(f"vault: {v.addr} {state}")
    for o in report.ollama:
        state = (f"v{o.version}, models: {', '.join(o.models) or 'none'}"
                 if o.reachable else f"UNREACHABLE ({o.error})")
        lines.append(f"ollama: {o.host} {state}")
    if report.egress.targets:
        eg = ", ".join(f"{t}={'y' if ok else 'N'}"
                       for t, ok in report.egress.targets.items())
        lines.append(f"egress: {eg}")
    return "\n".join(lines)
