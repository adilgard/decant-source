# REFCARD — Credentials: the four print-once secrets + the capture ceremony

*Kit 0.26.3 · 2026-07-27 · companion to `RUNBOOK_headless_ubuntu_deploy.md` §6/§8*

## ⚠ THE ONE IRREVERSIBLE STEP — do not interrupt the ceremony

Vault initialisation happens **before** secret capture completes. **Any
interruption between vault init and the end of capture leaves an
initialised, permanently unsealable vault** — `Initialized: true`,
`Sealed: true`, threshold 3-of-5, fewer than three shares in hand. The only
recovery is destroying the raft volume and redeploying (runbook §9a). This
happened in BP28.3 and cost a full redeploy.

- **`Ctrl+C` is never the right key.** Terminal copy is `Ctrl+Shift+C`;
  `Ctrl+C` sends SIGINT and kills the deploy. Test your copy method on
  harmless text BEFORE the ceremony.
- Run the deploy inside `tmux` (runbook §5) so a dropped SSH link or a
  sleeping laptop cannot kill it mid-ceremony.
- Nothing is timing you — the `type RECORDED` gate waits indefinitely.
  Slow is fine. Interrupted is fatal.

## The four secrets (printed exactly once, to stdout, never to disk)

| # | Secret | Used for | Recoverable? |
|---|---|---|---|
| 1 | **5 OpenBao unseal shares** (threshold 3) | Unsealing the vault after any reboot (`REFCARD_vault_unseal.md`) | **NO — nowhere but that one moment of stdout** |
| 2 | **Vault root token** | Bootstrap; day-2 hardening replaces it with a scoped admin token | Yes — written to `~/knowledge-hub/.env` as `BAO_ROOT_TOKEN` |
| 3 | **Agent serving credential** (`principal <tenant>-default`) | Agents / API clients against **serving on :8080** | Yes — re-mint with `khctl provision-agent` |
| 4 | **Operator console credential** (`principal <tenant>-operator-<hex>`) | **Logging into the browser UI** at `http://localhost:8081/ui/` — the one a human types into the login form | Check re-mint path before relying on it |

**Do not confuse #3 and #4.** Both look like `kh-<tenant>-<hex>`; only the
`-operator-` principal logs into the console.

## Capture checklist (before typing `deploy` at the plan gate)

- Password manager on the **operator laptop** — never on the server — open,
  both vaults unlocked, **entries pre-created** so each value is pasted into
  a waiting field, not typed into an entry built under pressure.
- Save (`Ctrl+S`) after **each** value, not once at the end — an unsaved
  buffer is not a captured secret.
- Ideally separate custodians for the 5 shares (Shamir's sharing collapses
  back into a single secret if one person holds all five), and the root
  token stored separately from the shares.

## Operator sessions — set the credential ONCE per shell

`khctl` re-prompts for the operator credential on **every call**. Export it
once at the start of the session — `read -s` keeps it out of shell history,
unlike an inline `export`, and removes the temptation to copy the credential
vault onto the server (which the custody model exists to prevent):

```bash
read -s -p "operator credential: " KH_OPERATOR_TOKEN; export KH_OPERATOR_TOKEN; echo
```

Paste the **operator console credential** (#4) at the prompt. The variable
dies with the shell; nothing lands on disk.
