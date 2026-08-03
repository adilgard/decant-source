# REFCARD — Unsealing the vault (OpenBao)

*Kit 0.26.3 · 2026-07-27 · companion to `RUNBOOK_headless_ubuntu_deploy.md` §5b/§6*

## First, the honest part

**A vault that reports `SEALED` after a reboot (or after the openbao
container was recreated) is NORMAL and BY DESIGN — it is not a lost
credential and not a broken deploy.** A raft-backed production vault always
comes back sealed; unsealing it is a routine operator action, not a
recovery. The launcher (since 0.26.2) waits for the vault to answer and says
so honestly rather than failing mysteriously.

## The command — exactly this, three times

```bash
docker exec -it -e BAO_ADDR=http://127.0.0.1:8200 kh-openbao bao operator unseal
```

Run it **3 times, each with a DIFFERENT one of the 5 custody shares**
(threshold is 3-of-5). Each run prompts for one share; after the third,
`Sealed` flips to `false`.

Check status any time (read-only, safe):

```bash
docker exec -e BAO_ADDR=http://127.0.0.1:8200 kh-openbao bao status
```

## Why the `-e BAO_ADDR=...` matters

The bare `bao` CLI defaults to **HTTPS**; the listener inside the container
is **plain HTTP** on 8200. Without `BAO_ADDR` the command fails with a TLS
error that looks like a broken vault. Every unseal command the tooling
prints (PREREQS.txt, the console lock screen, launcher failure messages)
carries the `-e BAO_ADDR=...` form since 0.26.1 — if you are typing one that
doesn't, you are typing it from memory. Don't.

## Rules

- The shares live in the **password manager on the operator laptop** — they
  are NEVER copied onto the server, pasted into files on the box, or stored
  in shell history (`docker exec -it` prompts interactively; paste the share
  at the prompt).
- 3 different shares. Entering the same share twice does not count twice.
- **Fewer than 3 shares in hand = the vault is permanently unrecoverable.**
  There is no back door. The only path forward is the failed-deploy teardown
  (runbook §9a — both compose files, `-v`) and a full redeploy with a new
  secrets ceremony (`REFCARD_credentials.md`).

## When you will need this card

- After any server reboot.
- After the **first launcher re-run following a fresh deploy** — `compose up`
  recreates the openbao container exactly once (its config hash changes when
  `.env` gains the real bootstrap token), and a recreated raft vault comes
  back sealed. Unseal, then re-run the launcher.
- Any time the console lock screen or a `khctl` failure says the vault is
  sealed.
