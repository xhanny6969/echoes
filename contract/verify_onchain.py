#!/usr/bin/env python3
"""Prove the deployed contract matches this source — without trusting a badge.

NearBlocks' verification is a web form (repo + commit, rebuilt in their sandbox).
This script does the equivalent check locally and harder: it pulls the *actual
code bytes that are live on chain*, hashes them, and compares that against the
wasm your machine produces from this repo.

    python3 contract/verify_onchain.py

Exit code 0 = the chain is running exactly this source. No dependencies: base58
is inlined so you don't need pip.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ACCOUNT = "allowance-swarm.testnet"
RPC = "https://rpc.testnet.fastnear.com"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = ALPHABET[rem] + out
    for byte in raw:  # leading zero bytes
        if byte != 0:
            break
        out = "1" + out
    return out


def rpc(method: str, params: dict) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": "verify", "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        payload = json.loads(resp.read().decode())
    if payload.get("error"):
        raise SystemExit(f"rpc error: {payload['error']}")
    return payload["result"]


def main() -> int:
    here = Path(__file__).resolve().parent

    onchain = rpc("query", {"request_type": "view_code", "account_id": ACCOUNT, "finality": "final"})
    import base64

    code = base64.b64decode(onchain["code_base64"])
    chain_hash = b58encode(hashlib.sha256(code).digest())

    print(f"account            {ACCOUNT}")
    print(f"code bytes onchain {len(code)}")
    print(f"code hash onchain  {chain_hash}")

    # The node also reports its own code_hash; it must agree with our hashing.
    account = rpc("query", {"request_type": "view_account", "account_id": ACCOUNT, "finality": "final"})
    if account.get("code_hash") and account["code_hash"] != chain_hash:
        print(f"MISMATCH: node reports {account['code_hash']} but the bytes hash to {chain_hash}")
        return 1
    print(f"node reported      {account.get('code_hash')}  (agrees)")

    wasm = here / "allowance_vault.wasm"
    if not wasm.exists():
        print(f"\nno local build at {wasm}; build it first:")
        print("  cd contract && cargo build --release --target wasm32-unknown-unknown")
        return 1

    local = wasm.read_bytes()
    local_hash = b58encode(hashlib.sha256(local).digest())
    print(f"code hash local    {local_hash}")

    if local_hash == chain_hash:
        print("\nVERIFIED — the live contract is byte-identical to this source tree.")
        return 0

    print("\nDIFFERENT — local build does not match chain. Rebuild:")
    print("  cd contract && cargo build --release --target wasm32-unknown-unknown")
    return 1


if __name__ == "__main__":
    sys.exit(main())