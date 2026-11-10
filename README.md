# Echoes

**On-chain spending vault and guardrail layer for autonomous AI agents on NEAR.**
Give an agent a wallet without giving it the keys to everything.

`Track 06 — AI Security & Defense` · NEAR Hackathon 2026

- Repo: https://github.com/xhanny6969/echoes
- Live showcase: https://xhanny6969.github.io/echoes/
- Live console: https://xhanny6969.github.io/echoes/console.html
- Contract: [`allowance-swarm.testnet`](https://testnet.nearblocks.io/address/allowance-swarm.testnet)
- Deployed code hash: `FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6`

---

## Ten-line quickstart

```bash
npm install                                              # near-api-js
node echoes/cli.js doctor                                # sanity check
node echoes/cli.js init \
  --vault lab --master <your>.testnet \
  --agent lab-worker --recipient merchant.testnet        # one command = vault + key + policy
node echoes/cli.js status --vault lab.<your>.testnet     # read it back from chain
node echoes/agent/demo.js .echoes/lab.<your>.testnet.json # real model, real txs
python3 contract/verify_onchain.py                       # live bytes == this source
```

`init` creates the sub-account, deploys the contract, sets the vault policy, mints a
`FunctionCall` key scoped to `spend_as` on that contract only, and registers one agent
with its own budget. The agent never sees a full-access key.

---

## The problem

Handing an autonomous agent a wallet today means handing it a **full-access key**. One
prompt injection, one compromised dependency, one bad tool call and the balance is gone.
Limits implemented in the agent's own code or in a backend database are not limits — a
compromised agent simply doesn't call them.

## What Echoes does

Two layers, both enforced by the protocol or the contract, never by the agent:

**Layer 1 — the key is scoped.** Each agent gets its own `ed25519` FunctionCall access
key with `receiver_id = vault` and `method_names = ["spend_as"]`. The NEAR runtime
rejects any other call before it reaches a contract. A stolen agent key cannot transfer,
stake, add keys, or call a different app.

**Layer 2 — the contract budgets.** `spend_as` checks, in order: vault paused → amount
> 0 → vault per-tx cap → vault daily cap → signer key is a registered agent → agent
active → agent per-tx cap → agent daily cap → recipient allowlisted → balance. Any
failure traps the receipt: nothing moves, no counter increments, no log entry is
written.

**Owner controls** — `register_agent`, `revoke_agent`, `reactivate_agent`, `set_paused`,
`set_policy`, `add_to_allowlist`, `reset_agent_day`, all behind `assert_owner()`.

## Proof: a real agent, two real outcomes

`echoes/agent/demo.js` runs a real LLM against a vendor API. Nothing is pre-validated in
JavaScript — whatever the model decides is forwarded to the chain untouched.

```
=== honest vendor API ===
model decided: spend 0.05 -> allowance-merchant.testnet
  chain: ACCEPTED 2y4tr89iwPazm5RYeHT3jL46GgqqehCLQyXezEy8qVoc

=== hijacked vendor API ===
model decided: spend 0.05 -> acme-gpu-payments.testnet
  chain: REVERTED 2ugSFi8CMr8eAKNgpbTAjeonkhnkeowyETqdKkXw57j5
  reason: allowkey: blocked — recipient is not on the allowlist
```

The second run is the interesting one: the vendor endpoint is compromised and answers
with a perfectly well-formed invoice pointing at the attacker's account. There is nothing
contradictory for the model to refuse — it does its job correctly and pays. The payout
still cannot happen, because the destination is not on the vault allowlist.

We did not script a "jailbreak" to get there. Modern models refuse obvious injected
instructions, which proves the model is polite, not that the money is safe. Counterparty
compromise needs no jailbreak.

## Why the source is verifiable

One command reads the live testnet deployment and asserts it, no keys needed:

```bash
node scripts/acceptance.js
# PASS  policy reads from chain
# PASS  balance reads from chain
# PASS  registered agents visible
# PASS  spend log readable
# PASS  deployed wasm == repo wasm byte for byte
```

Add `ECHOES_TX=1` plus an owner key and an agent key to also exercise the guardrails end to
end — allowlisted spend accepted, rogue recipient reverted, per-tx cap reverted, agent key
rejected for `set_paused` / `revoke_agent`, owner pause blocking spends, and `spent_today`
moving by exactly one accepted amount.

The shipped wasm rebuilds byte-identically from this repo:

```bash
cd contract && cargo build --release --target wasm32-unknown-unknown
sha256sum target/wasm32-unknown-unknown/release/allowance_vault.wasm
# d9a2a929…  ->  base58 FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6  (the deployed code hash)
```

Even better, check it against the chain instead of trusting me:

```
$ python3 contract/verify_onchain.py
code bytes onchain 192979
code hash onchain  FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6
node reported      FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6  (agrees)
code hash local    FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6

VERIFIED — the live contract is byte-identical to this source tree.
```

That pulls the live code bytes from RPC and hashes them, so it does not depend on
an explorer badge being granted.

## Hardening pass (post-audit)

Five issues were reported against the first bundle; each was checked against the source rather
than accepted at face value, then the ones that were real were fixed and re-proved on chain:

| Report | Verdict | What actually changed |
|---|---|---|
| Agent identity can be spoofed via an `agent_id` argument | **not applicable** — `spend_as` never took an `agent_id`; identity comes from `env::signer_account_pk()` | unchanged by design; a direct-call guard was added so `spend_as` can only be called by the signer's own transaction, never through a cross-contract call |
| `revoke_agent` does not remove the protocol key | **true, by design and now stated** — revocation blocks spending but keeps the key on the account | docs now separate "revoke" from "delete the key"; the contract still never claims to delete keys |
| Missing per-agent cap / daily window | **not applicable** — per-agent and vault per-tx/daily caps were already enforced together | reset is per UTC day; `reset_vault_day` added for demo re-runs |
| Async transfer failure leaves counters incremented | **true** — the transfer is detached, so a failed payout is not rolled back automatically | **disclosed, not silently "fixed"**: the vault only pays accounts the owner allowlisted, and this limitation is listed below |
| No storage-staking reserve | **partly true** — balance was storage-adjusted but not for the next log entry | `estimate_entry_storage()` now reserves the marginal cost; the log is capped at 200 entries; memo is capped at 256 bytes |

The one **real, unreported** hole that this pass found and closed: `spend()` (the single-agent
path) had no caller check at all, so any account could invoke it and trigger a payout to an
allowlisted recipient. It now requires `predecessor == vault || predecessor == owner`, proved by a
rejected call on the live contract:

```
$ # allowance-merchant.testnet calling allowance-swarm.spend directly
allowkey: blocked — only the vault or owner can call spend
tx (reverted) DsrngUhdcG7ur7qAwWLNxybMDK3q8Mm9w9f6WSy9X17n
```

Contract upgraded in place (state preserved) and re-verified afterwards:

- upgrade tx `5maMhL5X356Uf45dSXbTbauTKpqS8Yq77aitG65QvaAE`
- `python3 contract/verify_onchain.py` -> `FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6` on both sides
- public sandbox self-test re-run on the shipped code (`node echoes/sandbox.js`): accepted
  `C4tAUuCTUrRBwyrkX7Tv5GHqRL4Jrbk3YxbHUZXDwZ1f`, reverted at the allowlist
  `HkvvXd8EfZAMmBzXKK4UuAfg76kZaCg2WdnkcCfG2aiX` — `allowkey: blocked — recipient is not on the allowlist`

## Hosting

The showcase and console are fully static: they read the vault over NEAR RPC from the
browser (`Access-Control-Allow-Origin: *`), so no server, no tunnel, nothing to keep
alive. `site/` holds the built pages; `vercel.json` points Vercel at it.

```bash
# GitHub Pages: push site/ to the gh-pages branch (already live)
# Vercel: import the repo, it reads vercel.json (outputDirectory: site)
```

## Layout

```
contract/        Rust guardrail vault (near-sdk 5) — the actual product
echoes/cli.js    one-command bootstrap: account + contract + scoped key + policy
echoes/agent/    real LLM agent (careful + compromised profiles) and the demo runner
echoes/evidence/ transcript of the last demo run, including revert logs
dashboard/       live console + showcase page, reads chain state through /api/state
agent/           original swarm demo + invariant checker used during development
```

## Disclosures

- Testnet only. Amounts are small and the vault is deliberately public.
- A payout that reverts at the *transfer* receipt (for example because the allowlisted
  recipient account was deleted) is not rolled back automatically: the transfer is detached,
  so the quota for that spend stays consumed and the log keeps the entry.
- Revocation is **owner-triggered**, not automatic. We do not claim an auto-revoke
  primitive: a contract cannot self-disable a key on a reverted receipt without a keeper.
- The dashboard's playground is a client-side replay of the gate logic and is labelled
  `SIMULATION`. The numbers above it are live reads from `/api/state`.
- No TEE attestation yet — that is the obvious next layer, and it composes cleanly with
  a scoped key (attest, then hold the key).
- `.echoes/` was tracked in early commits and therefore still sits in git history with three
  scoped testnet keys. They cannot transfer, cannot be re-scoped and are bounded to `spend_as`
  with ~0.248 N of gas allowance — the on-chain proof, and why one of them is public on
  purpose, is in [SECURITY.md](SECURITY.md). `.echoes/` is untracked and gitignored now.

## License

MIT