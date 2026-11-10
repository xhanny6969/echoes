# Install Echoes into your agent

Echoes is an on-chain spending vault for autonomous AI agents. The agent gets a wallet
**without** the keys to the whole balance: its key is scoped by the NEAR runtime to a single
method (`spend_as`) on a single contract, and the contract enforces per-transaction caps,
daily caps, per-agent quotas and a recipient allowlist.

This file exists so an **agent can install and verify Echoes by itself**, with no human
setup. That is the point of the project — the guardrail has to be something an agent can be
given, not something a human has to babysit.

---

## 1. Paste this into your agent (Claude Code, Cursor, Codex, any agent with a shell)

```
Install and self-test Echoes — an on-chain spending vault for AI agents (NEAR).
Work in a temp directory and do exactly this:
git clone --depth 1 https://github.com/xhanny6969/echoes.git
cd echoes
npm install --no-audit --no-fund
node echoes/sandbox.js

Expected: exactly two PASS lines —
  PASS allowlisted recipient            -> accepted on chain
  PASS recipient not on allowlist       -> reverted, log "recipient is not on the allowlist"
Any other revert (e.g. daily cap) must be reported as FAIL, not counted as proof.

Then verify both transactions yourself over NEAR RPC (method "tx", sender_account_id
= the vault printed by the script) and show me the two statuses: SuccessValue and Failure.

Rules: do not modify the repo, do not create accounts, do not touch any key other than
the public sandbox key already in echoes/sandbox.json. If a line says FAIL, stop and
paste the full output instead of retrying.
```

Expected runtime: **60–120 seconds** (mostly `npm install`).

## 2. Shared sandbox limits

The demo uses one shared testnet vault; it is for sequential evaluation, not unlimited concurrent users. Each successful self-test sends 0.05 NEAR to the allowlisted testnet merchant and consumes gas allowance. The agent budget is capped at 5 NEAR/day, but the shared key's separate NEAR FunctionCall gas allowance may run out earlier. Concurrent tests may collide or exhaust a shared cap. If the script reports a cap/allowance error or a FAIL, stop, save the output, and try later—do not retry in a loop. This is a testnet demo, not a production SLA.

## 3. What the agent will see

```
PASS  allowlisted recipient
      recipient allowance-merchant.testnet · 0.05 N · expected accepted, got accepted
      tx 4Uh… (open on testnet.nearblocks.io)

PASS  recipient not on allowlist
      recipient not-on-the-allowlist.testnet · 0.05 N · expected reverted, got reverted
      tx 3Xd… 
      chain said: allowkey: blocked — recipient is not on the allowlist

vault demo.allowance-merchant.testnet · agent sandbox-worker
SELF-TEST PASSED — the contract blocked what it is supposed to block, on chain.
```

Exit code `0` means both expectations held. `1` means an outcome did not happen — the
script is safe for an agent to gate on.

## 3. Why the key in `echoes/sandbox.json` is public on purpose

- It is a NEAR **FunctionCall** access key, created with
  `receiver_id = demo.allowance-merchant.testnet` and `method_names = ["spend_as"]`.
  The runtime rejects any other method with `MethodNameMismatch` **before the contract runs**.
- It cannot transfer, stake, add keys, delete keys, or call any other app.
- It can call only `spend_as` on this one vault; no other contract methods.
- This demo vault pays only `allowance-merchant.testnet` (the allowlist). Reverted calls do not transfer the requested amount.
- It is **testnet only**. The vault is shared and rate-limited; simultaneous users may consume the daily or gas allowance and cause the self-test to fail. It is not an unlimited public service. If it fails with a cap/allowance error, wait and try later; don't retry in a loop.
- **Never reuse this pattern on mainnet.** A public key with a spend allowance is a public
  spending allowance. On mainnet, the key stays secret and the vault caps are the last line
  of defence, not the only one.

The sandbox vault, the agent and the caps are all readable on chain:

```bash
curl -s -X POST https://rpc.testnet.near.org -H 'content-type: application/json' -d '{
  "jsonrpc":"2.0","id":1,"method":"query","params":{
    "request_type":"call","finality":"final",
    "account_id":"demo.allowance-merchant.testnet",
    "method_name":"get_policy","args_base64":"e30="}}'
```

## 4. Put it in front of your own agent (production path)

You run this once, as the owner — the agent never sees this step:

```bash
npm install
node echoes/cli.js init \
  --vault myvault \
  --master <your>.testnet \
  --agent worker-1 \
  --recipient merchant.testnet \
  --per-tx 0.05 --daily 0.25 \
  --vault-per-tx 0.1 --vault-daily 1.0
```

Then hand the agent **only** `secret_key` from the generated config (`.echoes/<vault>.json`).
That key can call `spend_as` on that vault and nothing else. Example call:

```bash
ECHOES_AGENT_ID=worker-1 node echoes/agent/demo.js .echoes/myvault.testnet.json
```

Owner controls: `register_agent`, `set_agent_policy`, `revoke_agent`, `reactivate_agent`,
`reset_agent_day`, `set_paused`, `add_to_allowlist` — all behind `assert_owner()`.

## 5. Roadmap (honest status)

Not shipped yet, do not claim otherwise:

- **MCP server** exposing `vault.spend_as`, `vault.status`, `vault.policy` as native tools, so
  an MCP-capable agent needs one config line instead of a shell.
- Per-agent webhooks on revert (so an operator learns a hijack attempt happened).
- Multi-vault routing for agent fleets.

---

Contract: `demo.allowance-merchant.testnet` · sandbox: `demo.allowance-merchant.testnet` ·
source-verified, rebuilt wasm hash matches the deployed code hash byte for byte
(`node contract/verify_onchain.py`).
