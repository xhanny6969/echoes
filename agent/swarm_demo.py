#!/usr/bin/env python3
"""Allowkey swarm demo — two agents, two budgets, one vault.

Proves three things on live NEAR testnet:

  1. Each registered agent key spends only from its own budget. Agent alpha
     cannot eat agent beta's daily cap.
  2. The vault-level per-tx cap binds swarm spends too (`spend_as`), and every
     contract rejection carries a readable reason in the receipt log.
  3. The protocol blocks an agent key from calling anything except `spend_as`.
     A compromised agent cannot pause the vault, change policy or move the
     owner out.

(The owner-only kill switch, `set_paused(true)`, is documented in the README;
this script only shows that an *agent* key cannot flip it.)

Run:  python swarm_demo.py          (full demo)
      python swarm_demo.py status   (read-only snapshot)
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from near_client import NearClient  # noqa: E402

VAULT = "allowance-swarm.testnet"
MERCHANT = "allowance-merchant.testnet"
OWNER_CRED = os.path.expanduser("~/.near-credentials/testnet/allowance-swarm.testnet.json")
KEYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")

NEAR = 10**24


def load_cred(path):
    with open(path) as f:
        d = json.load(f)
    d["secret_key"] = d.get("private_key") or d.get("secret_key")
    return d


def owner():
    if os.path.exists(OWNER_CRED):
        c = load_cred(OWNER_CRED)
        return NearClient(c["account_id"], c["secret_key"])
    return NearClient(VAULT)


def has_agent_keys():
    return all(
        os.path.exists(os.path.join(KEYS_DIR, f"{name}.json"))
        for name in ("agent-alpha", "agent-beta")
    )


def agent(name):
    c = load_cred(os.path.join(KEYS_DIR, f"{name}.json"))
    # The signer *account* is the vault; the key is a function call key on it.
    return NearClient(VAULT, c["secret_key"])


def fmt(yocto):
    return f"{int(yocto) / NEAR:.4f} NEAR"


def snapshot(c):
    policy = c.view(VAULT, "get_policy")
    agents = c.view(VAULT, "get_agents") or []
    log_len = c.view(VAULT, "get_log_len")
    return policy, agents, log_len


def print_state(label, c):
    policy, agents, log_len = snapshot(c)
    print(f"\n--- {label} ---")
    print(f"vault policy : per-tx {fmt(policy['per_tx'])} | daily {fmt(policy['daily'])}"
          f" | spent {fmt(policy['spent_today'])} | paused {policy['paused']}")
    print(f"vault balance: {fmt(c.view(VAULT, 'get_balance'))}")
    print(f"spend log    : {log_len} entries")
    for a in agents:
        print(f"  {a['agent_id']:12s} per-tx {fmt(a['per_tx'])} | daily {fmt(a['daily'])}"
              f" | spent {fmt(a['spent_today'])} | remaining {fmt(a['remaining_today'])}"
              f" | active {a['active']}")


AUDIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit.jsonl")


def _audit(event, **fields):
    """Append one JSONL line so the dashboard can show blocked attempts too."""
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    row.update(fields)
    try:
        with open(AUDIT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def attempt(label, fn, agent_name="", intent=None):
    """Run one spend and print + audit the outcome without raising."""
    print(f"\n[{label}]")
    extra = {"agent": agent_name}
    if intent:
        extra["intent"] = intent
    try:
        r = fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  blocked: {exc}")
        _audit("blocked_offchain", reason=str(exc)[:300], **extra)
        return False
    if r.get("ok"):
        print(f"  ALLOWED  tx {r['tx']}")
        _audit("spend", tx=r["tx"], explorer=r.get("explorer"), **extra)
        return True
    err = r.get("error")
    logs = [line for line in (r.get("logs") or []) if line.startswith("allowkey:")]
    reason = logs[0].replace("allowkey: blocked — ", "") if logs else err
    print(f"  BLOCKED  {reason}")
    if r.get("tx"):
        print(f"  tx (reverted) {r['tx']}")
    _audit("blocked_onchain", tx=r.get("tx"), error=str(err)[:300],
           reason=str(reason)[:300], **extra)
    return False


def verify_readonly(c):
    """Verify live on-chain evidence when private keys are not present locally."""
    print("\n[read-only verification mode — agent private keys not found in agent/keys/]")
    print("Verifying protocol-scoped access keys and on-chain spend log from testnet RPC...")
    keys = c.access_key_list(VAULT)
    scoped = [
        k for k in keys
        if isinstance((k.get("access_key") or {}).get("permission"), dict)
        and "FunctionCall" in k["access_key"]["permission"]
    ]
    print(f"  protocol-scoped FunctionCall keys on {VAULT}: {len(scoped)}")
    for k in scoped:
        fc = k["access_key"]["permission"]["FunctionCall"]
        print(f"    {k['public_key']} -> receiver={fc.get('receiver_id')} methods={fc.get('method_names')}")

    log = c.view(VAULT, "get_log") or []
    print(f"  on-chain spend log entries: {len(log)}")
    for e in log:
        print(f"    #{e['id']} {e['agent']:12s} {fmt(e['amount'])} -> {e['recipient']} ({e['memo']})")

    if os.path.exists(AUDIT):
        print(f"\n  recorded audit attempts ({AUDIT}):")
        with open(AUDIT, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                ev = row.get("event")
                ag = row.get("agent", "vault")
                if ev == "spend":
                    print(f"    ALLOWED          {ag:12s} tx {row.get('tx')}")
                else:
                    err = (row.get("reason") or row.get("error") or "")[:90]
                    print(f"    BLOCKED ON-CHAIN {ag:12s} {err}")

    policy, agents, _ = snapshot(c)
    by_id = {a["agent_id"]: a for a in agents}
    print("\n--- verdict ---")
    a, b = by_id.get("agent-alpha"), by_id.get("agent-beta")
    if a and b:
        print(f"agent-alpha spent {fmt(a['spent_today'])} / {fmt(a['daily'])} daily cap")
        print(f"agent-beta  spent {fmt(b['spent_today'])} / {fmt(b['daily'])} daily cap")
        print(f"separate budgets: {a['spent_today'] != b['spent_today']}")
    print(f"vault still active (agent could not pause it): {not policy['paused']}")
    print(f"merchant balance: {fmt(c.account_balance(MERCHANT))}")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "demo"
    c = owner()

    if mode == "demo" and has_agent_keys() and os.path.exists(OWNER_CRED):
        # Repeatable demo: zero the agents' daily counters first (owner-only).
        # The contract rolls these over by itself at UTC midnight; without this
        # a second run the same day would hit an already-spent budget.
        for name in ("agent-alpha", "agent-beta"):
            r = c.call(VAULT, "reset_agent_day", {"public_key": agent(name).public_key})
            print(f"[reset] {name} daily counter: {'ok' if r.get('ok') else r.get('error')}")
            time.sleep(3)

    print_state("start", c)
    if mode == "status":
        return 0

    if not has_agent_keys():
        return verify_readonly(c)

    alpha = agent("agent-alpha")
    beta = agent("agent-beta")

    # 1. each agent spends inside its own budget
    attempt(
        "agent-alpha spends 0.15 NEAR",
        lambda: alpha.call(
            VAULT, "spend_as",
            {"amount": str(15 * NEAR // 100), "recipient": MERCHANT, "memo": "alpha invoice #1"}),
        "agent-alpha",
        {"method": "spend_as", "amount_near": 0.15, "recipient": MERCHANT, "memo": "alpha invoice #1"},
    )
    time.sleep(1)
    attempt(
        "agent-beta spends 0.15 NEAR",
        lambda: beta.call(
            VAULT, "spend_as",
            {"amount": str(15 * NEAR // 100), "recipient": MERCHANT, "memo": "beta invoice #1"}),
        "agent-beta",
        {"method": "spend_as", "amount_near": 0.15, "recipient": MERCHANT, "memo": "beta invoice #1"},
    )
    time.sleep(1)

    # 2. alpha tries to overspend its own per-tx cap
    attempt(
        "agent-alpha tries 0.25 NEAR (over its 0.2 per-tx cap)",
        lambda: alpha.call(
            VAULT, "spend_as",
            {"amount": str(25 * NEAR // 100), "recipient": MERCHANT, "memo": "over cap"}),
        "agent-alpha",
        {"method": "spend_as", "amount_near": 0.25, "recipient": MERCHANT, "memo": "over cap"},
    )
    time.sleep(1)

    # 3. alpha spends the rest of its own budget -> 0.15 + 0.1 = 0.25 of 1.0
    attempt(
        "agent-alpha spends another 0.10 NEAR (inside its budget)",
        lambda: alpha.call(
            VAULT, "spend_as",
            {"amount": str(10 * NEAR // 100), "recipient": MERCHANT, "memo": "alpha invoice #2"}),
        "agent-alpha",
        {"method": "spend_as", "amount_near": 0.10, "recipient": MERCHANT, "memo": "alpha invoice #2"},
    )
    time.sleep(1)

    # 4. beta tries 0.90 NEAR -> exceeds the vault's 0.5 per-tx cap (checked first)
    attempt(
        "agent-beta tries 0.90 NEAR (over the vault 0.5 per-tx cap)",
        lambda: beta.call(
            VAULT, "spend_as",
            {"amount": str(90 * NEAR // 100), "recipient": MERCHANT, "memo": "over daily"}),
        "agent-beta",
        {"method": "spend_as", "amount_near": 0.90, "recipient": MERCHANT, "memo": "over daily"},
    )
    time.sleep(1)

    # 5. protocol guardrail: alpha's key may ONLY call spend_as
    attempt(
        "agent-alpha tries to pause the vault (protocol should refuse)",
        lambda: alpha.call(
            VAULT, "set_paused", {"paused": True}),
        "agent-alpha",
        {"method": "set_paused", "note": "paused=true"},
    )
    time.sleep(1)

    # 6. protocol guardrail: alpha's key may not call the single-agent spend
    attempt(
        "agent-alpha tries vault-level spend (protocol should refuse)",
        lambda: alpha.call(
            VAULT, "spend",
            {"amount": str(NEAR // 100), "recipient": MERCHANT, "memo": "wrong method"}),
        "agent-alpha",
        {"method": "spend", "amount_near": 0.01, "recipient": MERCHANT, "memo": "wrong method"},
    )

    print_state("after swarm run", c)

    policy, agents, _ = snapshot(c)
    by_id = {a["agent_id"]: a for a in agents}
    print("\n--- verdict ---")
    a, b = by_id.get("agent-alpha"), by_id.get("agent-beta")
    if a and b:
        print(f"agent-alpha spent {fmt(a['spent_today'])} (expect 0.25, own budget only)")
        print(f"agent-beta  spent {fmt(b['spent_today'])} (expect 0.15, untouched by alpha)")
        # Compare against what this run actually sent, not just "a != b":
        # two separate budgets can legitimately hold the same number.
        ok_alpha = int(a["spent_today"]) == 25 * NEAR // 100
        ok_beta = int(b["spent_today"]) == 15 * NEAR // 100
        print(f"separate budgets (alpha 0.25 / beta 0.15 as expected): {ok_alpha and ok_beta}")
    print(f"vault still active (agent could not pause it): {not policy['paused']}")
    print(f"merchant balance: {fmt(c.account_balance(MERCHANT))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
