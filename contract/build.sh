#!/usr/bin/env bash
# Build the guardrail vault. The deployed testnet contract was produced by
# exactly this command; `sha256sum` of the output must match code hash
# FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6 (base58) on allowance-swarm.testnet.
set -euo pipefail
cd "$(dirname "$0")"
cargo build --release --target wasm32-unknown-unknown
cp target/wasm32-unknown-unknown/release/allowance_vault.wasm ./allowance_vault.wasm
python3 - <<'PY'
import hashlib, base58
h = hashlib.sha256(open('allowance_vault.wasm','rb').read()).digest()
print("code hash:", base58.b58encode(h).decode())
PY
