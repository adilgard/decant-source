# Runbook — offline Knowledge Hub deploy on headless Ubuntu Server

**Kit:** decant.Source 0.26.3 (signed, trust anchor `org-2026`)
**Validated by:** BP28 WSL2 rehearsal (2026-07-26, kit 0.26.0, RED) and the
BP33 re-rehearsals (2026-07-27, kits 0.26.1 → 0.26.2, fresh distro each,
egress genuinely blocked): the 0.26.2 run completed the launcher's full
six-step fresh deploy with **ZERO manual interventions** and `LAUNCH_RC=0`,
in-session verify 12/12 green. All on `Pinnacle`.
**BP28.3 (2026-07-27, kit 0.26.2)** then proved the two-machine case: deployed
**by hand from a separate laptop over a real network hop**, secrets captured
live into a password manager, console reached over an SSH tunnel, full pipeline
run (4 docs → 269 facts) with **egress blocked and counted**. Section 5b stayed
empty. Findings from that run are folded into the sections below.
**0.26.3 (BP34/BP35, 2026-07-27)** adds the two behaviors that make the deploy
safe next to a live client Postgres — the adoption gate and the busy-port step
(§5c) — plus on-site hardening. SSD rebuilt and re-signed under `org-2026`;
`verify-kit` green on the drive.
**Target:** Ubuntu Server 24.04, no desktop, reached over SSH from a laptop

> **How you drive this box — the whole model in one paragraph.** Everything
> happens from YOUR laptop (the ROG). The deploy runs in a TERMINAL: SSH into
> the server and run `bash decant.Source/launch.sh` inside `tmux` (§5). The
> console is a BROWSER page — but the browser runs on the laptop, through an
> SSH tunnel (`ssh -L 8081:localhost:8081`, §7), because the services bind
> `127.0.0.1` on purpose. There is no double-click step, no desktop, and no
> browser on the server — if an instruction seems to need one, you are
> reading the wrong document.
>
> **Travelling companions on the SSD** (all in `decant.Source/`, outside the
> signed `kit/`): `PREREQS.txt` (host packages, install BEFORE going
> offline), `REFCARD_vault_unseal.md` (the unseal command), and
> `REFCARD_credentials.md` (the four print-once secrets + capture ceremony).

> Status: VALIDATED end-to-end against kit 0.26.2; 0.26.3 carries the BP34
> gate/port additions on top (unit-tested; the gate's live-decoy rehearsal is
> still owed — expect the SILENT path on-site, §5c). Commands are verbatim as
> executed. Lines marked **[WSL-ONLY]** are rehearsal-environment fixes that
> do NOT apply on the real server. Lines marked **[ON-SITE]** replace a
> rehearsal step in the field.

> **Scope (added 2026-07-28):** this runbook is the **NVIDIA appliance
> path**: CUDA, `nvidia-smi`, and the dense `qwen3.6:27b-bf16` extraction
> model carried by the 0.26.x kits. The extraction model is now
> backend-dependent: the AMD Strix Halo path runs the MoE
> `qwen3.6:35b-a3b-q4_K_M` on Ollama's bundled ROCm instead (a dense 27B is
> memory-bandwidth-bound on that hardware). Nothing in this runbook changes
> for NVIDIA deploys. AMD-path setup is NOT covered here; see
> DEPLOY_NOTES.md (BP43 section) and the spike record
> `.Handoff Docs/strix-halo-inference-spike-2026-07-28.md`.

---

## 0. Before you fly

- [ ] SSD in hand, kit verified as 0.26.3 signed (`kit/manifest.json.minisig` present)
- [ ] Know where the SSD goes: **into the SERVER** (the GPU box), not the
      laptop. Headless Ubuntu does NOT auto-mount USB — the mount is a
      command, not a plug-and-wait (§1a).
- [ ] Server hostname / IP and the deploy login (from IT)
- [ ] Confirm with IT that the **NVIDIA Linux driver is installed** on the server
      (`nvidia-smi` must list the cards). This is the most common on-arrival gap.
- [ ] Internet (or an apt mirror) available for the prereqs — they cannot come
      off the SSD. Everything after that is offline. **`iptables`** (needed to
      verify or enforce no-egress) and **`tmux`** (needed for any deploy driven
      over SSH) are NOT on a stock Ubuntu 24.04 image and cannot be added once
      you are offline — install them with the other five.
- [ ] Password manager on the operator laptop, **installed and both vaults
      created before you arrive** — see §6. It was missing at the start of
      BP28.3.
- [ ] 3 of 5 OpenBao custody shares available if the vault will be unsealed

---

## 1. Get a shell on the box

**[ON-SITE]** From your laptop:

```bash
ssh <deploy-user>@<server-ip>
```

**[WSL-ONLY]** Rehearsal equivalent, from PowerShell on the build machine:

```powershell
wsl --install -d Ubuntu-24.04
```

Then confirm where you are:

```bash
cd ~
cat /etc/os-release | head -2; echo "--- whoami: $(whoami)"; echo "--- home: $HOME"
```

---

## 1a. First 10 minutes on the box — two pre-flight checks BEFORE any deploy

Run both of these the moment you have a shell. Each one, if it fails, is
cheapest to fix at minute 10 and most expensive to discover at minute 90.

### 1a-1. Plug in the SSD — on the SERVER — and mount it

The desktop rehearsals hid this step: a desktop Ubuntu auto-mounts USB under
`/media/<user>/`; a headless server mounts **nothing** until you tell it to.
Plug the SSD into the **server** (it is the deploy source; the laptop never
needs it), then:

```bash
lsblk -o NAME,LABEL,SIZE,FSTYPE,MOUNTPOINT
```

Find the SSD in the list (the ~1 TB exFAT partition — e.g. `/dev/sda1`; the
device letter varies with what else is attached). Then:

```bash
sudo mkdir -p /mnt/ssd
sudo mount /dev/sda1 /mnt/ssd      # substitute your device from lsblk
```

exFAT support is in the stock Ubuntu 24.04 kernel. If the mount refuses with
an fs-type error (it shouldn't), install the userspace tools — this needs
internet, one more reason to do it in the first 10 minutes:

```bash
sudo apt-get install -y exfatprogs
```

Confirm it is readable before moving on:

```bash
ls /mnt/ssd/decant.Source/            # want: kit/ launch.sh PREREQS.txt + the guides
head -3 /mnt/ssd/decant.Source/PREREQS.txt
```

The SSD is **transport only** — the kit is copied to the server's local ext4
disk in §4b and the deploy runs from there, never off the USB.

### 1a-2. Prove the NVIDIA driver is really there

```bash
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
```

Want: the actual GPUs with a real driver version. **The kit will NOT stop on
a missing driver** — the probe records `gpu: NONE` and the plan degrades
politely. Nothing downstream shouts. If `nvidia-smi` is absent or errors,
stop and get the driver installed (IT) before anything else; a deploy
finished on the CPU is a deploy you get to do twice.

---

## 2. Snapshot the environment before installing anything

Read-only. Tells you what the box already has, and catches a Docker that is
not the Docker you think it is.

```bash
cd ~
echo "=== init system:"; ps -p 1 -o comm=
echo "=== docker:";     command -v docker    || echo "  ABSENT (correct for a fresh box)"
echo "=== nvidia-smi:"; command -v nvidia-smi && nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader || echo "  ABSENT"
echo "=== python3.12:"; python3.12 --version  || echo "  ABSENT"
echo "=== minisign:";   command -v minisign  || echo "  ABSENT"
echo "=== ollama:";     command -v ollama    || echo "  ABSENT"
echo "=== pwd:";        pwd
```

Rehearsal result on `Pinnacle`: systemd already PID 1; `nvidia-smi` present
(2x RTX PRO 6000 Blackwell Max-Q, 97887 MiB each, driver 595.97);
python3.12.3 present; minisign and ollama absent; **docker resolved to a
Windows path** — see 2a.

### 2a. **[WSL-ONLY]** Kill the Windows PATH leak

WSL2 appends the whole Windows `PATH` into Linux, so Docker Desktop's `docker`
shadows the native one. `launch.sh` would report `docker ✓` while the deploy
talked to Docker Desktop on Windows — a false pass on "Docker works on Linux".
A real server has no Windows PATH, so removing it *raises* fidelity.

Authenticate sudo first, so the password prompt does not eat the pasted block:

```bash
sudo -v
```

Then:

```bash
echo "=== existing /etc/wsl.conf:"; cat /etc/wsl.conf 2>/dev/null || echo "  (none)"
sudo cp /etc/wsl.conf /etc/wsl.conf.bak 2>/dev/null || true
sudo tee /etc/wsl.conf >/dev/null <<'EOF'
[boot]
systemd=true

[interop]
enabled=true
appendWindowsPath=false
EOF
echo "=== new /etc/wsl.conf:"; cat /etc/wsl.conf
```

Restart the distro from PowerShell, then re-enter and confirm the leak is gone:

```powershell
wsl --shutdown
wsl -d Ubuntu-24.04
```

```bash
cd ~
echo "=== init system:"; ps -p 1 -o comm=
echo "=== docker (want ABSENT):"; command -v docker || echo "  ABSENT - leak closed"
echo "=== PATH:"; echo "$PATH"
```

---

## 3. Pre-provision the prereqs — NEEDS INTERNET, do this BEFORE going offline

This section is the executable form of `PREREQS.txt` at the SSD root — same
five packages (Docker apt-repo method, minisign, python3.12 + venv, zstd,
Ollama) plus the same headless extras (openssh-server, iptables, tmux; rsync
optional). If the two ever disagree, PREREQS.txt on the 0.26.3 SSD is the one
the kit's own refusal messages point at — reconcile before proceeding.

Cache the sudo credential first, so password prompts do not swallow lines from
a pasted block:

```bash
sudo -v
```

### 3a. Docker Engine (official apt repo), minisign, python3.12-venv, zstd, iptables, tmux, openssh-server

The apt repo is used rather than `curl get.docker.com | sh` because it is the
method PREREQS.txt links to and it works behind a corporate apt mirror, which
`get.docker.com` may not.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl minisign python3.12-venv zstd iptables tmux openssh-server
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Group membership only applies in a NEW login shell. Log out and back in.

**[ON-SITE]** `exit`, then `ssh <deploy-user>@<server-ip>` again.
**[WSL-ONLY]** `exit`, then `wsl -d Ubuntu-24.04`.

### 3b. Ollama (native, systemd service, GPU host)

```bash
sudo -v
curl -fsSL https://ollama.com/install.sh | sh
```

### 3c. Verify the prereq wall will pass

```bash
cd ~
echo "=== docker binary:"; command -v docker
echo "=== docker version:"; docker --version
echo "=== compose plugin:"; docker compose version
echo "=== docker daemon (systemd):"; systemctl is-active docker
echo "=== groups:"; groups
echo "=== minisign:"; minisign -v
echo "=== python3.12 venv module:"; python3.12 -c 'import ensurepip; print("ensurepip OK")'
echo "=== DOCKER RUNTIME TEST ==="; docker run --rm hello-world
echo "=== ollama version:"; ollama --version
echo "=== ollama service:"; systemctl is-active ollama
echo "=== listening on 11434:"; ss -tlnp 2>/dev/null | grep 11434 || echo "  NOT LISTENING"
echo "=== GPU seen by ollama:"; sudo journalctl -u ollama --no-pager | grep -iE "cuda|gpu|inference compute" | tail -5
echo "=== models present (expect EMPTY before deploy):"; ollama list
```

Expected: `/usr/bin/docker` (a Linux path — **not** `/mnt/c/...`), daemon
`active`, `docker` in groups, `Hello from Docker!`, ollama `active` and holding
11434, CUDA/GPU lines in the ollama journal, and `ollama list` **empty**
(models are imported offline from the kit, not pulled).

Rehearsal result on `Pinnacle`: Docker 29.6.2, Compose v5.3.1, minisign 0.11,
ensurepip OK, hello-world passed.

## 4. Final environment check, then copy the kit to the box's local disk

### 4a. Ports free (checked from inside Linux, which is what the deploy sees)

```bash
for p in 8080 8081 5432 8333 8200 11434; do printf "port %-6s " $p; ss -tln | grep -q ":$p " && echo "IN USE" || echo "free"; done
```

Expect 8080/8081/8333/8200 **free** and 11434 **IN USE** — that is the
native Ollama installed in step 3b, which is correct.

**5432 IN USE is also expected on the real target** — that is the client's
own live Postgres, and it is not a blocker and not yours to
touch. The launcher notices it and binds OUR Postgres to a free host port
(5433+) instead; see §5c for exactly what you will and won't be asked.
Only an unexpected squatter on 8080/8081/8333/8200 needs investigating.

**[WSL-ONLY]** On the build machine, the pilot stack and Windows-native Ollama
must be stopped first or they squat these ports and can silently serve the
rehearsal (Windows Ollama on 11434 is the dangerous one — it would false-pass
the "inference ran on Linux" claim):

```powershell
docker compose -p knowledge-hub down
Stop-Process -Name ollama, "ollama app" -Force -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 8080,8081,5432,8333,8200,11434 -State Listen -ErrorAction SilentlyContinue
```

### 4b. Copy the kit off the SSD onto local disk

Do NOT run the deploy off the SSD. The kit must land on the box's own
filesystem so the install survives unplugging the SSD, and so the stack and the
~53 GB model store sit on a real Linux filesystem with real permissions.

`rsync` is used over `cp` only for the live progress readout.

```bash
sudo -v
sudo apt-get install -y rsync
mkdir -p ~/dS
```

**[ON-SITE]** The SSD was mounted at `/mnt/ssd` in §1a (headless boxes do NOT
auto-mount — if you skipped §1a, do it now). Copy from the mount point:

```bash
mount | grep /mnt/ssd    # confirm it is still mounted
time rsync -ah --info=progress2 /mnt/ssd/decant.Source ~/dS/
```

After the copy verifies (§4c) the SSD's job is done — it can stay plugged in,
but everything from here on runs off `~/dS` on the local disk.

**[WSL-ONLY]** The SSD appears as a Windows drive letter:

```bash
time rsync -ah --info=progress2 /mnt/d/decant.Source ~/dS/
```

**Measured on `Pinnacle`: 60.48 GB in 5m30s at 174 MB/s, all 309 files.**
Budget ~6-10 minutes, not an hour. Ownership warnings from the exFAT source are
harmless (exFAT has no Unix ownership to preserve).

### 4c. Verify the copy

```bash
echo "=== filesystem (want ext4):"; df -hT ~/dS | tail -1
echo "=== copied size (expect ~57G):"; du -sh ~/dS/decant.Source
echo "=== file count (expect 320 at kit 0.26.3):"; find ~/dS/decant.Source -type f | wc -l
echo "=== kit version (want 0.26.3):"; grep -o '"package_version"[^,]*' ~/dS/decant.Source/kit/manifest.json
echo "=== signature present:"; ls -l ~/dS/decant.Source/kit/manifest.json.minisig
echo "=== launcher present:"; ls -l ~/dS/decant.Source/launch.sh
```

No need to hand-verify file hashes: `launch.sh` checks the manifest signature
against its embedded trust anchor before executing anything from the kit, and
`khctl launch` then re-runs the full arrival gate (per-file hashes, tree audit,
no-secrets scan).

## 5. Deploy from the terminal

**Run it inside `tmux`.** The session lives on the *server*, so a dropped SSH
link cannot kill the deploy — you reconnect and `tmux attach -t deploy`, and it
has carried on without you. This matters most during the secrets ceremony (§6),
the one step that cannot be repeated. It also lets a second terminal read the
output with `tmux capture-pane -pt deploy -S -300` without touching the session.

```bash
tmux new -s deploy
cd ~/dS
bash decant.Source/launch.sh
```

Six launcher steps. What each prompt wants:

| Prompt | Answer | Why |
|---|---|---|
| `probe report above — [Enter] continues` | **Enter** | Probe is read-only. Check it lists the GPUs. On a shared target the client's Postgres shows REACHABLE on 5432 — expected; ours/object store/vault show UNREACHABLE (they don't exist yet). |
| `EXISTING POSTGRES DETECTED — OPERATOR DECISION REQUIRED` (**expect: does NOT appear** — §5c) | **Enter** if it does | The adoption gate. It fires ONLY if the probe actually logged into the detected DB with our pilot defaults — on-site it should stay SILENT, which is correct, not a miss. Enter = self-contained stack (ours), always safe. NEVER type `a`/`ADOPT` on this engagement. |
| `host port for OUR Postgres [Enter=5433]` | **Enter** | 5432 is held by the client's live Postgres; the launcher offers the first free port from 5433. Their DB is never contested or touched (§5c). |
| `tenant id(s) to bootstrap (or 'none')` | **a real tenant id** | NEVER `none`. A tenant-less deploy mints NO credentials and the console can never be logged into. Lowercase, hyphenated, no spaces — it becomes the partition key on every document, fact and graph edge. **Agree the real tenant id with the client BEFORE flying**; renaming later is a data migration. |
| `plan gate>` | **`deploy`** | NOT `rehearse` — that is `apply --dry-run` and changes nothing. Word trap. |
| `type RECORDED ...` (x3) | **`RECORDED`** | Print-once secrets. See section 6 first — have your capture method ready BEFORE you reach this point. |

Then 9 phases: kit verification (hashes ~60GB, several minutes of silence), preflight,
env install, services, schema + migrations, openbao bootstrap, model store (~53GB
copy), python env, tenant bootstrap.

**Resuming after a failed phase** — this skips the seeding step, so any repair you
made in the deployment home survives (a plain `launch.sh` re-run re-seeds and
reverts it):

```bash
cd ~/knowledge-hub
~/knowledge-hub/.venv/bin/khctl apply --plan deploy_plan.json --env-file .env.deploy --infra-dir . --kit ~/dS/decant.Source/kit
```

### 5a. Prove it, then start the services

```bash
cd ~/knowledge-hub
~/knowledge-hub/.venv/bin/khctl verify --plan deploy_plan.json
~/knowledge-hub/.venv/bin/python check_stack.py     # want 11/11
```

`verify` reports the FULL picture instead of stopping at the first failure — run it
always. A silent service fatal can pass all 9 apply phases and only show up here.

The 8080/8081 services are NOT started by apply. Re-run the launcher; on a deployed
box it goes straight to a different menu and does **not** re-seed:

```bash
cd ~/dS
bash decant.Source/launch.sh      # then press Enter at `launch>`
```

```bash
ss -tln | grep -E ':(8080|8081)'   # both bind 127.0.0.1 — the tunnel is mandatory
```

### 5b. Known defects in the shipped kit — **EMPTY** (proven at 0.26.2; no new defects known in 0.26.3)

**Nothing to do here. This is the section the re-rehearsals existed to empty.**

The nine BP28 workarounds (kit 0.26.0) were fixed in code by BP30/BP31 and
proven obsolete by the 2026-07-27 re-rehearsals: two full fresh-distro deploys
(kits 0.26.1 and 0.26.2), egress genuinely blocked, **zero manual
interventions** end to end. The 0.26.1 run then surfaced one NEW defect —
the launcher process bound pilot-default settings at import, so its own
step-6 verify reported 7 false FAILs on a healthy deploy and the first
in-session ingest crashed on the deploy's minted S3 credentials — fixed in
0.26.2 (settings singleton refreshed when the launcher enters the deployment
home and again after apply writes `.env`) and proven by the 0.26.2 run:
`LAUNCH_RC=0`, in-session verify 12/12 green, in-session ingest clean.

If a future rehearsal ever needs a manual fix to reach a working stack,
record it HERE and treat the kit as NOT flyable until this section is empty
again.

**Normal operations that look like problems (not defects):**

- **First launcher re-run after a fresh deploy finds the vault SEALED.**
  `compose up` recreates the openbao container exactly once (its config
  hash changes when `.env` gains the real bootstrap token), and a recreated
  raft vault always comes back sealed — by design. Since 0.26.2 the
  launcher waits for the vault to answer and then says so honestly. Unseal
  with 3 of the 5 custody shares and re-run the launcher:

```bash
docker exec -it -e BAO_ADDR=http://127.0.0.1:8200 kh-openbao bao operator unseal
```

  Run it 3x with 3 different shares. Every unseal command the tooling
  prints (PREREQS.txt, the console lock screen, launcher failures) carries
  the `-e BAO_ADDR=...` form since 0.26.1 — the bare `bao` CLI defaults to
  HTTPS and fails against the plain-HTTP listener. The full recipe, plus
  what "SEALED" does and does not mean, is on `REFCARD_vault_unseal.md`
  next to this runbook.

### 5c. Coexisting with the client's live Postgres — the adoption gate and the port step (0.26.3)

The target already runs the client's own Postgres on 5432. Two
independent 0.26.3 behaviors make that a non-event, and it is worth knowing
which is which so silence doesn't read as a missed detection:

**The adoption gate — expect it to stay SILENT, and that is the correct
outcome.** The gate exists for the case where the probe finds a client
database it could actually *use*: it fires only when the probe genuinely
**logged in** with our pilot-default credentials. The client's DB has its own
credentials, so the login fails, no adoption candidate exists, and the
launcher simply prints `no adoptable client infrastructure detected —
self-contained stack.` and moves on. **A silent gate is the gate working**
— fail-closed detection, not a blind spot. (This is also why the gate's
live rehearsal needs a decoy DB seeded with our defaults; absence of the
prompt on-site proves nothing is adoptable, which is what we want.)

**IF the gate does appear** (their credentials happened to match ours), it
announces `EXISTING POSTGRES DETECTED — OPERATOR DECISION REQUIRED` and
stops for a choice:

- **Enter** = deploy the self-contained stack (ours). The safe default, and
  the only correct answer on this engagement.
- Adopting their DB requires deliberately typing `a`, supplying their DSN,
  and then typing `ADOPT` at a stakes prompt that spells out the writes
  (schema install, migrations, CREATE EXTENSION inside their database).
  **Do not.** A mis-key cannot adopt — anything other than the exact word
  backs out.
- `q` stops the launcher with nothing changed.

**The port step — this one you WILL see.** Independent of the gate (a
Postgres holds 5432 whether or not anyone can log into it), the launcher
sees the port is taken and offers the first free host port from 5433 up for
OUR Postgres. Press **Enter** to accept; the choice is recorded in the plan
as `--use postgres=ours:<port>`. The holder of 5432 is never contested,
probed with writes, or touched.

**The non-disruption promise, in one line:** the only contact our tooling
ever makes with the client's database is the probe's single read-only login
attempt (which their credentials refuse); it is written by nothing, never
port-contended (proven in the BP34 decoy rehearsal: zero connections during
and after apply, `pg_dumpall` byte-identical), and adopting it is impossible
without three deliberate keystrokes ending in the typed word `ADOPT`.

---

## 6. Credentials — what they are and where they go

The deploy prints **four** things exactly once, to stdout, never to disk. Have the
capture method ready **before** you start the deploy. The pocket version of
this section — the four secrets, which one logs into the console, and the
no-interrupt ceremony rules — travels as `REFCARD_credentials.md` next to
this runbook; keep it in view during the ceremony.

| Secret | Used for | Recoverable? |
|---|---|---|
| **5 OpenBao unseal shares** (threshold 3) | Unsealing the vault after any reboot | **NO — nowhere but that one moment of stdout** |
| **Vault root token** | Bootstrap; day-2 hardening replaces it with a scoped admin token | Yes — written to `~/knowledge-hub/.env` as `BAO_ROOT_TOKEN` |
| **Agent serving credential** (`principal <tenant>-default`) | Agents / API clients against **serving on :8080** | Yes — re-mint with `khctl provision-agent` |
| **Operator console credential** (`principal <tenant>-operator-<hex>`) | **Logging into the browser UI** at `http://localhost:8081/ui/` — this is the one a human types into the login form | Check re-mint path before relying on it |

Do not confuse the last two. Both look like `kh-<tenant>-<hex>`; only the
`-operator-` principal logs into the console.

> **⚠ THE CEREMONY IS THE ONLY IRREVERSIBLE STEP OF THE DEPLOY.** Vault
> initialisation happens *before* capture completes, so **any interruption after
> init leaves an initialised, permanently unsealable vault** — `Initialized:
> true`, `Sealed: true`, threshold 3-of-5, fewer than three shares in hand. The
> only recovery is destroying the raft volume and redeploying (§9a). A stray
> `Ctrl+C`, a dropped SSH session or a laptop going to sleep is enough.
> **This happened in BP28.3 and cost a full redeploy.**
>
> Before you type `deploy`:
> - Password manager **open, both vaults unlocked, entries pre-created** so each
>   value is pasted into a waiting field, not typed into an entry built under
>   pressure. Save (`Ctrl+S`) after **each** value, not once at the end — an
>   unsaved buffer is not a captured secret.
> - **`Ctrl+C` is never the right key.** In a terminal, copy is `Ctrl+Shift+C`;
>   `Ctrl+C` sends SIGINT. Reaching for the wrong one is what destroyed the
>   BP28.3 vault. **Test your copy method on harmless text before you start.**
> - Run the deploy inside `tmux` so a dropped link cannot kill it mid-ceremony
>   (§5). Nothing is timing you — the gate waits indefinitely.
>
> **⚠ OPEN ITEM — secure capture procedure does not exist yet.** During the BP28
> rehearsal these were captured by pasting into Notepad. That is not a procedure.
> Before flight, decide and write down: who records each secret, into what
> (password manager entries, ideally separate custodians for the 5 shares so
> Shamir's sharing isn't collapsed back into a single secret), who holds the 3
> that can reconstitute the vault, and where the root token lives separately from
> the shares. Also decide operator-vs-client custody — the `appliance` profile
> defaults to `operator`, meaning WE hold the shares for a box in the client's
> server room, and it cannot be changed after the vault is initialized.

## 7. Reach the console over an SSH tunnel

The serving and operator services bind **`127.0.0.1`**, so they are unreachable
from outside the box. The tunnel is the mechanism, not a workaround.

On the box (`openssh-server` was installed with the prereqs in §3a — and on a
server you SSH'd into, it is by definition already running):

```bash
sudo systemctl enable --now ssh
systemctl is-active ssh
hostname -I
```

> **If you need a NON-DEFAULT ssh port**, note that Ubuntu 24.04's
> `openssh-server` is **socket-activated**: `ssh.socket` owns the listening
> port and the `Port` directive in `sshd_config` is silently ignored. Override
> the socket instead — and bind IPv4 explicitly, since a bare `ListenStream=`
> can yield an IPv6-only listener:
>
> ```bash
> sudo mkdir -p /etc/systemd/system/ssh.socket.d
> printf '[Socket]\nListenStream=\nListenStream=0.0.0.0:2222\nListenStream=[::]:2222\n' \
>   | sudo tee /etc/systemd/system/ssh.socket.d/override.conf
> sudo systemctl daemon-reload && sudo systemctl restart ssh.socket
> ```
>
> The bare `ListenStream=` clears the inherited `:22`; without it you get both.
> Port 22 needs none of this.

From the client machine (**[ON-SITE]** your laptop; **[WSL-ONLY]** PowerShell on
the build machine, since a WSL VM's private IP is not routable from the LAN):

```bash
ssh -L 8081:localhost:8081 <deploy-user>@<box-ip>
```

Leave that session **open** — closing it closes the tunnel. Then browse to
`http://localhost:8081/ui/` and log in with the **operator console credential**.

## 8. Ingest and verify

**Set the operator credential once per session, first.** Otherwise every single
`khctl` call prompts for it — over a working session that is many repetitions,
and it tempts an operator into copying the credential vault onto the server,
which is precisely what the custody model exists to prevent. `read -s` keeps it
out of shell history, unlike an inline `export`:

```bash
read -s -p "operator credential: " KH_OPERATOR_TOKEN; export KH_OPERATOR_TOKEN; echo
```

```bash
~/knowledge-hub/.venv/bin/khctl ingest --tenant <tenant> --add-source <ref>=<folder>
~/knowledge-hub/.venv/bin/khctl ingest --tenant <tenant>            # --watch for continuous
~/knowledge-hub/.venv/bin/khctl alerts                              # list · --retry · --ack
```

`--add-source` registers the folder and captures; the second call runs
process → extract → resolve. **Both are silent until they finish** and print one
summary line — silence is not a hang.

> **⚠ RUN ONE INGEST AT A TIME.** Two `khctl ingest` processes against the same
> tenant claim the same extraction units; one wins and the other dies with
> `UniqueViolation: ... "ux_extraction_unit"`, parking queue items with an
> alarming error. It *is* recoverable — the parked items complete on a later
> retry once the competing driver is gone — but it looks like a broken pipeline
> for as long as it lasts. Note the trap: because ingest prints nothing while it
> works, a long run looks hung, and starting a second one is the natural
> reaction. Check `pgrep -af khctl` before starting another.
>
> Idempotency holds **for capture** — a clean re-run reports
> `landed=0 replayed=0 tombstoned=0 status=ok` with counts unchanged. It does
> **not** hold for concurrent extraction.

**Timing, measured BP28.3** (4 real documents, ~45 KB total, 109 child chunks):
roughly **60-90 seconds per chunk**, and the whole ingest took **~45 minutes**
end to end. The earlier "25-30s per document" figure was measured on a much
smaller corpus and will badly understate a real one — an operator told to expect
thirty seconds who then waits forty-five minutes will conclude it has hung and
start a second ingest. Quote the per-chunk figure, not the per-document one.
`OLLAMA_KEEP_ALIVE` is 5 minutes, so the first call after an idle period also
reloads ~53 GB into VRAM before doing any work.

**Prove the GPU is doing the work** (the claim worth evidencing on-site):

```bash
ollama ps          # want the extraction model resident with '100% GPU'
nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv
```

BP28 measured `qwen3.6:27b-bf16 · 52 GB · 100% GPU` alongside
`bge-m3 · 664 MB · 100% GPU`.

### 8a. What to watch in the console

`Ingestion monitor` refreshes every 5s: the pipeline board should advance
`01 CAPTURE → 02 PROCESS → 03 EXTRACT → 04 RESOLVE → 05 FACTS`, the activity
stream should print timestamped adjudication decisions with tier and score, and
`Per-source progress` should show the source with a Pause control.

`Review queue` aggregates **three** kinds of human work — ambiguous merges,
quarantined extractions, flagged documents. Decisions are keyboard-driven
(A / R / S / space) and every attempt is audited. Each item shows the verbatim
passage that produced it with chunk provenance, so a fact can always be traced
to its source text.

> **Unwired surfaces (design references, not live):** `System connections`,
> `LOOKUP` in the header bar, and **`Facts & entities`**. The last one matters:
> it is where a client asks *"what did it actually learn?"*, and after the BP28.3
> ingest there were **269 promoted facts with no way to see them in the UI**.
> Provenance itself is intact — `facts.source_chunk_id` joins straight to the
> verbatim chunk, and the review queue renders the passage that produced each
> item with chunk provenance — so this is a missing surface, not a missing
> capability. **Wire it before any client demo.**

> **Ontology caveat — do not misread quality numbers.** The shipped ontology is
> `baseline-0.1`, whose own seed note reads *"Placeholder vocabulary. Shaped like
> the real ontology; type names are throwaway."* It has 10 generic predicates
> (`authored_by`, `mentions`, `part_of`, …) that cannot express domain facts. In
> BP28 all 8 quarantined facts were `unbound_predicate` — the extractor produced
> correct domain predicates (`has_certification`, `has_test_date`, `has_quantity`)
> and the placeholder had nowhere to put them. That is the quarantine gate working,
> not an extraction failure. Quarantine rate and confidence distribution are
> meaningless until `real-1.0` is built from the client's actual corpus.

---

## 9. Teardown

### 9a. **[ON-SITE]** Cleaning up a failed deploy before retrying

You need this if a deploy has to be restarted from scratch on a real server —
most likely because the vault was initialised but the custody shares were not
captured (see §6). **Both compose files must be named**, or the OpenBao
container and its raft volume survive, the retry comes up on the *old* vault,
and it fails with `SEALED` — which reads like a fresh-deploy bug and is not:

```bash
cd ~/knowledge-hub
docker compose -f docker-compose.yml -f docker-compose.openbao-prod.yml down -v --remove-orphans
docker volume ls          # must show NO knowledge-hub_* volumes
docker ps -a              # must show NO kh-* containers
```

`docker compose down -v` against `docker-compose.yml` alone is **not enough** —
the production vault lives in `docker-compose.openbao-prod.yml`. Verified the
hard way in BP28.3: an incomplete teardown cost a full extra deploy cycle.

Then move the deployment home aside rather than deleting it (its `.env` and
`deploy_plan.json` are evidence if you need to explain what happened), and
re-run `launch.sh`:

```bash
cd ~ && mv ~/knowledge-hub ~/knowledge-hub.failed-1
```

### 9b. Rehearsal teardown **[WSL-ONLY]**

```powershell
wsl --shutdown
wsl --unregister Ubuntu-24.04
```

`--unregister` deletes the whole VM — filesystem, Docker daemon, containers,
volumes, model store. Stopping the stack first is tidiness, not a requirement.
Copy anything you want to keep out via `\\wsl.localhost\Ubuntu-24.04\...` first,
and never copy `.env` (it holds the vault root token).

Restore the build machine's pilot afterwards — rehearsal must be gone first or the
ports collide:

```powershell
docker compose -f "C:\Users\<you>\Documents\Documents Workspace\decant-source\docker-compose.yml" up -d
```

…then restart Windows-native Ollama.

---

## 10. Pre-flight checklist

- [x] Kit rebuilt and re-signed as **0.26.3** (BP35, 2026-07-27) — verify-kit
      green on the Crucial X9; guide bundle (this runbook + both REFCARDs)
      copied to the SSD's `decant.Source/` alongside `PREREQS.txt` (BP36)
- [x] Rehearsal re-run end to end on a fresh distro, twice (0.26.1 → found one
      new launcher defect → 0.26.2 → **zero manual workarounds, LAUNCH_RC=0**).
      Section 5b is empty. (0.26.3 gate/port additions are unit-tested; the
      live adoption-gate rehearsal against a credential-matching decoy is
      still owed — the expected on-site path is the silent one, §5c.)
- [x] `PREREQS.txt` lists five prereqs incl. `zstd` + the headless extras
      (openssh-server, iptables, tmux) + the `BAO_ADDR` unseal note
- [x] HF tokenizer bundled; both re-rehearsals ran with egress GENUINELY
      blocked (iptables + DNS blackhole): rx delta ≈ +21KB for the entire
      deploy + pipeline, no HF/docling cache appeared, probe recorded egress=N
- [ ] Print-once secrets capture procedure written and agreed (section 6 OPEN ITEM)
- [ ] Custody mode (operator vs client) confirmed with the client
- [ ] Real tenant id agreed with the client
- [ ] Confirmed with IT that the server has the NVIDIA Linux driver and that
      `nvidia-smi` lists the cards (re-proven on arrival — §1a-2)
- [ ] Decided whether `real-1.0` ontology work happens before or after the deploy

**Rehearsal-environment lessons (all [WSL-ONLY], for the next dress run —
none apply on real hardware):** keep ONE wsl.exe session attached for the
whole rehearsal (`wsl -d <distro> -- sleep infinity` in a hidden window) or
WSL idle-terminates the distro mid-deploy between short-lived commands;
systemd units don't inherit WSL's session PATH, so give them
`/usr/lib/wsl/lib` explicitly or `nvidia-smi` is invisible to the probe;
WSL's DNS tunneling bypasses eth0 iptables — blocking egress for real needs
`generateResolvConf=false` + a blackhole `nameserver 127.0.0.1` as well;
`ssh-keygen -R <wsl-ip>` before tunneling to a REBUILT distro (recycled IP,
new host key).
