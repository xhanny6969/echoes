# Echoes

**On-chain spending vault and guardrail layer for autonomous AI agents on NEAR.**
Give an agent a wallet without giving it the keys to everything.

`Track 06 — AI Security & Defense` · NEAR Hackathon 2026

- Repo: https://github.com/xhanny6969/echoes
- Live showcase: https://xhanny6969.github.io/echoes/
- Live console: https://xhanny6969.github.io/echoes/console.html
- Contract: [`allowance-swarm.testnet`](https://testnet.nearblocks.io/address/allowance-swarm.testnet)
- Deployed code hash: `9JNpk7uGkuWyp6Yd834muCtADcjGXFufHJu6A3jdMTrn`

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
  chain: ACCEPTED HENHbkBQycS9d56NX2K8zV6VN5DqaZn4QkEpJvoR3cX3

=== hijacked vendor API ===
model decided: spend 0.05 -> acme-gpu-payments.testnet
  chain: REVERTED 6GvjL2aWxBhnVVkAqdyx31PQpMmCLguUTKoBjzSrnyuA
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

The shipped wasm rebuilds byte-identically from this repo:

```bash
cd contract && cargo build --release --target wasm32-unknown-unknown
sha256sum target/wasm32-unknown-unknown/release/allowance_vault.wasm
# 7b510b71…  ->  base58 9JNpk7uGkuWyp6Yd834muCtADcjGXFufHJu6A3jdMTrn  (the deployed code hash)
```

Even better, check it against the chain instead of trusting me:

```
$ python3 contract/verify_onchain.py
code bytes onchain 192109
code hash onchain  9JNpk7uGkuWyp6Yd834muCtADcjGXFufHJu6A3jdMTrn
node reported      9JNpk7uGkuWyp6Yd834muCtADcjGXFufHJu6A3jdMTrn  (agrees)
code hash local    9JNpk7uGkuWyp6Yd834muCtADcjGXFufHJu6A3jdMTrn

VERIFIED — the live contract is byte-identical to this source tree.
```

That pulls the live code bytes from RPC and hashes them, so it does not depend on
an explorer badge being granted.

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
- Revocation is **owner-triggered**, not automatic. We do not claim an auto-revoke
  primitive: a contract cannot self-disable a key on a reverted receipt without a keeper.
- The dashboard's playground is a client-side replay of the gate logic and is labelled
  `SIMULATION`. The numbers above it are live reads from `/api/state`.
- No TEE attestation yet — that is the obvious next layer, and it composes cleanly with
  a scoped key (attest, then hold the key).

## License

MIT