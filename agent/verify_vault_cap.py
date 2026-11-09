#!/usr/bin/env python3
"""Prove the vault-level caps actually bind swarm spends.

Before the fix, `spend_as` checked only the *agent's* caps: the vault per-tx /
daily caps were enforced for the single-agent `spend` path but never for
`spend_as`, and the vault `spent_today` counter was never incremented. The
result was that N agents could spend N x their own daily cap out of one vault.

This script checks the invariant on the live contract (10 checks, T0b–T6c;
T0a only runs if an earlier run left the agent revoked):

  T0b reset the agent's daily counter (owner-only)       -> accepted
  T1  register_agent with per_tx above the vault per_tx  -> rejected
  T2  owner tightens the vault per_tx cap               -> accepted
  T3  an agent spend inside its own cap but above the
      vault cap                                          -> rejected, and the
                                                            reason is readable
                                                            in the receipt log
  T4  owner restores the *original* vault policy         -> accepted (always runs,
                                                            even if T2/T3 crash)
  T5  the same spend now lands, and the vault
      spent_today counter moves (it used to stay at 0)   -> accepted (+ T5b)
  T6  revoke_agent then reactivate_agent                 -> accepted, reversible
                                                            (T6a, T6b, T6c)

Broadcast mode needs the owner credential plus at least one agent key:

    ~/.near-credentials/testnet/allowance-swarm.testnet.json
    allowance/agent/keys/agent-alpha.json

Run:  python3 verify_vault_cap.py
      python3 verify_vault_cap.py --read-only   # just print the current state
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from near_client import NearClient  # noqa: E402

VAULT = os.environ.get("VAULT", "allowance-swarm.testnet")
MERCHANT = os.environ.get("MERCHANT", "allowance-merchant.testnet")
OWNER_CRED = os.path.expanduser("~/.near-credentials/testnet/allowance-swarm.testnet.json")
KEYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")
ALPHA_CRED = os.path.join(KEYS_DIR, "agent-alpha.json")

NEAR = 10**24
# A key that is valid ed25519 but is not registered on the vault.
DUMMY_PK = "ed25519:7PGseFbWxvYVgZ89K1uTJKYoKetWs7BJtbyXDzfbAcqX"


def cred(path: str) -> str:
    with open(path) as fh:
        data = json.load(fh)
    return data.get("private_key") or data["secret_key"]


def fmt(yocto) -> str:
    return f"{int(yocto) / NEAR:.4f} NEAR"


def readonly() -> int:
    c = NearClient(VAULT)
    policy = c.view(VAULT, "get_policy")
    print(f"vault      : per-tx {fmt(policy['per_tx'])} | daily {fmt(policy['daily'])}"
          f" | spent_today {fmt(policy['spent_today'])} | paused {policy['paused']}")
    for a in c.view(VAULT, "get_agents") or []:
        print(f"  {a['agent_id']:12s} per-tx {fmt(a['per_tx'])} | daily {fmt(a['daily'])}"
              f" | spent {fmt(a['spent_today'])} | active {a['active']}")
    return 0


def main() -> int:
    if "--read-only" in sys.argv:
        return readonly()
    if not os.path.exists(OWNER_CRED) or not os.path.exists(ALPHA_CRED):
        print("owner credential or agent key missing — falling back to --read-only")
        return readonly()

    owner = NearClient(VAULT, cred(OWNER_CRED))
    alpha = NearClient(VAULT, cred(ALPHA_CRED))
    agent_pk = alpha.public_key
    rows: list[tuple[str, str, str | None]] = []

    def send(tag: str, client: NearClient, method: str, args: dict,
             expect_block: str | None = None) -> dict:
        res = None
        for _ in range(3):
            res = client.call(VAULT, method, args)
            blob = json.dumps(res)
            if "InvalidNonce" not in blob and "INVALID_TRANSACTION" not in blob:
                break
            time.sleep(5)  # the node needs a block to pick up the new nonce
        logs = [line for line in (res.get("logs") or []) if line.startswith("allowkey:")]
        ok = bool(res.get("ok"))
        if expect_block:
            passed = (not ok) and any(expect_block in line for line in logs)
        else:
            passed = ok
        shown = logs[0].replace("allowkey: blocked — ", "") if logs else (res.get("error") or "")[:100]
        verdict = "PASS" if passed else "FAIL"
        print(f"{verdict}  {tag}\n      -> {shown}")
        rows.append((verdict, tag, res.get("tx")))
        time.sleep(4)  # nonce safety, same reason as above
        return res

    state = {a["public_key"]: a["active"] for a in owner.view(VAULT, "get_agents") or []}
    if agent_pk not in state:
        print(f"agent key {agent_pk} is not registered on {VAULT} — nothing to verify")
        return 1

    # Snapshot the live policy so T2/T4 restore *exactly* what was there,
    # instead of overwriting the daily cap / recipients with hard-coded values.
    original = owner.view(VAULT, "get_policy")
    orig_args = {"per_tx": original["per_tx"], "daily": original["daily"],
                 "recipients": original["recipients"]}
    tight_per_tx = NEAR // 20  # 0.05 NEAR, below the 0.10 test spend
    spend_amount = NEAR // 10  # 0.10 NEAR, inside the agent's own 0.2 cap
    if int(original["per_tx"]) < spend_amount:
        print("vault per_tx is already below the 0.10 NEAR test spend — refusing to modify it")
        return 1
    if MERCHANT not in original["recipients"]:
        print(f"{MERCHANT} is not on the vault allowlist — refusing to modify it")
        return 1

    if state.get(agent_pk) is False:
        send("T0a reactivate the agent left revoked by an earlier run", owner,
             "reactivate_agent", {"public_key": agent_pk})
    # T5 needs headroom in the agent's own daily budget, and this script is meant
    # to be re-runnable the same UTC day, so zero its counter first (owner-only).
    send("T0b reset the agent's daily counter (owner-only, for repeatable runs)", owner,
         "reset_agent_day", {"public_key": agent_pk})

    send(f"T1  register agent with per_tx 2.0 > vault per_tx {fmt(original['per_tx'])}",
         owner, "register_agent",
         {"agent_id": "test-overcap", "public_key": DUMMY_PK,
          "per_tx": str(2 * NEAR), "daily": str(3 * NEAR)},
         expect_block="vault per_tx")

    try:
        send("T2  owner tightens the vault per_tx cap to 0.05 NEAR", owner, "set_policy",
             {**orig_args, "per_tx": str(tight_per_tx)})

        send("T3  agent spend 0.10 (inside its own cap, over the vault cap)", alpha, "spend_as",
             {"amount": str(spend_amount), "recipient": MERCHANT, "memo": "vault-cap-proof"},
             expect_block="vault per-transaction")
    finally:
        # Always put the original cap back, even if T2/T3 raised mid-way;
        # otherwise a crash would leave the live vault at 0.05 NEAR per tx.
        send(f"T4  owner restores the original vault per_tx cap ({fmt(original['per_tx'])})",
             owner, "set_policy", orig_args)

    before = int(owner.view(VAULT, "get_policy")["spent_today"])
    send("T5  the same spend now lands", alpha, "spend_as",
         {"amount": str(spend_amount), "recipient": MERCHANT, "memo": "vault-counter-proof"})
    time.sleep(3)
    after = int(owner.view(VAULT, "get_policy")["spent_today"])
    moved = after - before
    passed = moved == spend_amount
    print(f"{'PASS' if passed else 'FAIL'}  T5b vault spent_today moved {fmt(moved)}"
          f" ({fmt(before)} -> {fmt(after)})")
    rows.append(("PASS" if passed else "FAIL", "T5b vault counter increments on spend_as", None))

    revoked: list[bool] = []
    try:
        send("T6a revoke the agent", owner, "revoke_agent", {"public_key": agent_pk})
        revoked = [a["active"] for a in owner.view(VAULT, "get_agents") if a["public_key"] == agent_pk]
    finally:
        # Never leave the demo agent revoked if something failed in between.
        send("T6b reactivate the agent", owner, "reactivate_agent", {"public_key": agent_pk})
    restored = [a["active"] for a in owner.view(VAULT, "get_agents") if a["public_key"] == agent_pk]
    passed = revoked == [False] and restored == [True]
    print(f"{'PASS' if passed else 'FAIL'}  T6c active flag revoked={revoked} restored={restored}")
    rows.append(("PASS" if passed else "FAIL", "T6c revoke -> reactivate toggles active", None))

    npass = sum(1 for verdict, _, _ in rows if verdict == "PASS")
    print(f"\n{npass}/{len(rows)} checks passed")
    return 0 if npass == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
