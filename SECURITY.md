# Security

Echoes is a guardrail vault: it exists so an autonomous agent never needs a key that can
move more than a policy allows. This file documents what the guardrail does and does not
protect, and it answers the one finding every automated scanner raises on this repo.

---

## Reporting a vulnerability

Open a private security advisory on this repository, or email the address in the GitHub
profile. Please do not open a public issue for a live exploit. Testnet-only, no bug bounty.

---

## If you scanned this repo and found `ed25519` secrets

**Expected. Three of them. All testnet, all scoped to a single method, none of them a
full-access key.** They were committed under `.echoes/` in early development and are still
present in git history (commit `a6a759c`). They have since been untracked and `.echoes/`
is now gitignored, but history is history, so here is the verifiable state instead of a
promise.

Checked against `rpc.testnet.fastnear.com` on **2026-11-10 08:15 UTC**:

| account | leaked key | permission | receiver | methods | allowance |
|---|---|---|---|---|---|
| `demo.allowance-merchant.testnet` | `ed25519:GiemQuxw…` | FunctionCall | itself | `["spend_as"]` | 0.247325 N |
| `lab.allowance-swarm.testnet` | `ed25519:5jAbzxos…` | FunctionCall | itself | `["spend_as"]` | 0.248499 N |
| `sandbox.allowance-merchant.testnet` | `ed25519:9FGvU6MA…` | FunctionCall | itself | `["spend_as"]` | 0.247957 N |

Reproduce it yourself:

```bash
curl -s -X POST https://rpc.testnet.fastnear.com -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"query","params":{
       "request_type":"view_access_key_list","finality":"final",
       "account_id":"sandbox.allowance-merchant.testnet"}}' | jq '.result.keys'
```

### What such a key can and cannot do

A NEAR `FunctionCall` access key is bound to `(receiver_id, method_names, allowance)`. For
these three keys that means:

- **can** call exactly one method, `spend_as`, on one contract account — the vault itself;
- **cannot** attach a deposit (protocol rule for `FunctionCall` keys), so it cannot move NEAR
  as a transfer;
- **cannot** call `transfer`, `add_key`, `delete_key`, `delegate_action`, `stake`, or any
  other method — the runtime rejects the transaction before WASM runs;
- **cannot** be re-scoped by its holder; only the account's full-access key can change it;
- **gas is the only real budget**: ~0.248 N of allowance per key, which bounds how many
  (reverted) calls an attacker could spam. That is the same bound every agent key has.

Worst case with all three: a few thousand reverted `spend_as` calls on a testnet account,
each capped by the vault at 0.05 N/tx and 5 N/day, with the destination restricted to the
allowlist. There is no path from these keys to a transfer, and no mainnet deployment uses
them.

### Why one of them is public on purpose

`echoes/sandbox.json` holds the `demo.allowance-merchant.testnet` key **deliberately**: it is
the shared sandbox so any agent can prove the guardrail works in 60 seconds
(`node echoes/sandbox.js`) without a human provisioning anything. The file carries a
`_warning` field stating this. If it were a secret, the whole "an agent can install and
verify this itself" claim would be false.

If you are auditing this repo for a hackathon: the finding is real, it is disclosed here,
and the remediation is scope — not a fresh commit history.

---

## Threat model

**Protects:** funds held by the vault contract, against a compromised or prompt-injected
agent. The agent's key cannot name an arbitrary recipient (allowlist) and cannot exceed
per-transaction, per-agent-daily or vault-daily ceilings. Every check runs before
`Promise::transfer` is constructed, so a breach reverts the whole receipt.

**Does not protect:**

- **Gas.** A leaked agent key can burn its own allowance on reverted calls. Bounded by the
  key's allowance (≤ 0.25 N), not by the vault. Revoke the key to stop it.
- **Revocation is owner-triggered.** There is no automatic self-revoke on breach — a
  contract cannot safely disable a key on a reverted receipt without a keeper. Stated in
  the FAQ, not hidden.
- **The allowlisted recipient itself.** If a trusted recipient is compromised, payouts to it
  are policy-compliant. Caps limit the loss per day; they do not audit the counterparty.
- **Storage rent.** The vault keeps a storage buffer; `available_balance` subtracts it.

## Known limitations in this build

- Native NEAR only. `spend_as` schedules `Promise::transfer`; there is **no** NEP-141 /
  `ft_transfer` path yet. The stated roadmap (see FAQ Q08) is scoped but not implemented.
- Chain Signatures and NEAR Intents are roadmap, not shipped.
- Log is bounded (`MAX_LOG_ENTRIES`); the oldest entries are evicted, so on-chain history is
  a window, not an archive. Use an indexer for permanent records.
