# Echoes — demo video script (75s)

Target: 75 seconds, 16:9, 1080p, English VO (or text-only + music if no mic).
Rule: every number and hash on screen must exist on chain. Nothing simulated.

Verified facts to use (checked 2026-11-10 against rpc.testnet.near.org, `FeZL3sAZ…` code):

| Fact | Value |
|---|---|
| Contract | `allowance-swarm.testnet` |
| On-chain code hash | `FeZL3sAZN6Z6d4UgUJd1soy9T9XbuNubd5m8dKj1k4H6` (192,979 bytes) |
| Main vault policy | 0.50 N per tx, 5.00 N per day, allowlist `allowance-merchant.testnet` |
| Demo vault (the one filmed) | `demo.allowance-merchant.testnet` — 0.06 N per tx, 8.00 N per day |
| Agent in the filmed run | `sandbox-worker` — 0.05 N per tx, 5.00 N per day, key scoped `spend_as` |
| **Accepted tx (real model)** | `DJjC2iKMQCzsdakrZwUkAZN9t5ttxu2VqFrt7cnB7fUH` → `SuccessValue` |
| **Reverted tx (real model)** | `CusTBQ5hFaYwCpWe3X6ywkEQG1mUU2JkxfJXn5uo2Uuy` → `Failure`, log `allowkey: blocked — recipient is not on the allowlist` |
| Keyless path, accepted | `FbUcp9B8p5M7PnoMdjGsvGH7RBmYpLet3zLTLrvFPZfy` (`node echoes/sandbox.js`) |
| Keyless path, reverted | `2GGMytK1DvVkqg3VGjPgMxe1mcQfb7JavTmZErcCzSwo` |
| Allowlisted recipient | `allowance-merchant.testnet` |
| Attacker recipient | `acme-gpu-payments.testnet` |
| Agent key scope | `FunctionCall(spend_as)` — receiver = vault, method = `spend_as`, no FullAccess |
| Live pages | xhanny6969.github.io/echoes (200), /console.html (200) |

### Read the revert correctly on an explorer (a judge will check)

The blocked run is a `FunctionCall`, so the transaction top-level status is
`SuccessReceiptId` and the **failure lives in the receipt**. If you show only the tx hash
and a judge opens it, they see "Success" and conclude the claim is false. Quote the receipt:

| Run | tx hash | failed receipt | log |
|---|---|---|---|
| real model, hijacked vendor | `CusTBQ5h…2Uuy` | `4qBU9AGAUHRdZNboE4pEPk2kskcJsMoEJMHMReNeBKb1` | `allowkey: blocked — recipient is not on the allowlist` |
| keyless sandbox | `2GGMytK1…CzSwo` | `3YitQobkFkohWt8566CwJE7EuBToXboK2iDugH5soNHv` | same |

Same reason the earlier accepted run is `SuccessReceiptId`: the vault schedules the payout
as a promise, and every policy check runs before that promise is constructed.

### Record these three takes, in this order

```bash
# take 1 — the keyless proof (no wallet, 20s, always works)
cd ~/echoes && node echoes/sandbox.js

# take 2 — the real model, two outcomes (the money shot; needs OPENROUTER_API_KEY)
export OPENROUTER_API_KEY=...            # never on screen, never committed
node echoes/agent/demo.js .echoes/demo.allowance-merchant.testnet.json

# take 3 — verifiability
python3 contract/verify_onchain.py
```

Take 2 prints the model's own reasoning line, then the chain verdict. That single
terminal window is the whole product: same model, same request, honest vendor accepted,
hijacked vendor reverted before any transfer.

Before rolling, confirm the demo vault still has room:
`near view demo.allowance-merchant.testnet get_agents` → `spent_today` must leave ≥ 0.05 N
of the agent's daily 5.00 N. One `reset_vault_day` call from the owner resets the counters
if you re-record many times.


---

## Shot list

### 0:00–0:08 — Hook
**Visual:** slow zoom on the console page, treasury panel visible. No logo yet.
**On screen (big, left):** `Autonomous agents need to spend money.`
**VO:** "Every autonomous agent eventually needs to pay for something. Today the only way to let it do that is to hand it a private key — and hope."

### 0:08–0:18 — The real failure mode
**Visual:** the hijacked invoice JSON, attacker account line highlighted in red.
**On screen:** `One compromised vendor. No jailbreak required.`
**VO:** "You don't even need a jailbreak. The vendor's API gets compromised, it returns a perfectly valid invoice pointing at the attacker's account, and your agent — doing its job correctly — pays it."

### 0:18–0:30 — What Echoes is
**Visual:** console page, Layers 1–4 strip scrolling into view.
**On screen:** `Echoes — on-chain guardrail vault for AI agents on NEAR`
**VO:** "Echoes gives an agent a wallet without giving it the keys. Two layers, neither of them in the agent's code. The key is scoped at the protocol level — one method, `spend_as`, on one contract. And the contract enforces the budget: per-tx cap, daily cap, per-agent quota, and an allowlist of recipients."

### 0:30–0:33 — Live, not a mockup
**Visual:** console "Vault Treasury Balance" + "Block #…" ticker, cursor highlights RPC LIVE badge.
**On screen:** `allowance-swarm.testnet · read live over RPC`
**VO:** "This is a live testnet contract, read from the browser over RPC."

### 0:33–0:50 — The two runs (the whole point)
**Visual:** terminal, font ≥ 18pt, both runs in one take. Cut nothing.
**VO:** "Same agent, same model, same request. Honest vendor: the model decides to pay, and the chain accepts."
**On screen at 0:40:** `ACCEPTED · DJjC2iKMQCzs…` (green)
**VO:** "Now the vendor is hijacked. The model reads the page, finds nothing wrong, and decides to pay the new account. The chain reverts it."
**On screen at 0:47:** `REVERTED · recipient is not on the allowlist` (red)

### 0:50–1:02 — Verifiability
**Visual:** `python3 contract/verify_onchain.py` output.
**On screen:** the three matching hashes, then `VERIFIED — live contract == this source`
**VO:** "And you don't have to trust the repo. The deployed contract rebuilds byte-identical from source — hash on chain, hash on disk, same string."

### 1:02–1:12 — Close
**Visual:** hero page, repo URL + contract URL as text.
**On screen:** `github.com/xhanny6969/echoes` · `allowance-swarm.testnet` · `Track 06 — AI Security & Defense`
**VO:** "Billions in agent payments are coming. Every one of them needs a limit the agent cannot argue with. That's Echoes. Built at NEAR Hackathon."

---

## Recording order (do it in one sitting)

```bash
# 1. terminal prep — big font, clean shell, dark theme
cd "$(git rev-parse --show-toplevel)"   # any checkout, not one machine
export OPENROUTER_API_KEY=...           # any OpenRouter key; the model only writes the invoice text
node echoes/cli.js status --vault lab.allowance-swarm.testnet   # warm-up, show nothing

# 2. record terminal (this is the money shot)
node echoes/agent/demo.js echoes/sandbox.json                   # both runs, one take (public sandbox key)

# 3. record verify step
python3 contract/verify_onchain.py

# 4. browser: xhanny6969.github.io/echoes/console.html — scroll slowly, no fast flicks
```

**Screen recording:** `ffmpeg -f x11grab -video_size 1920x1080 -framerate 30 -i :0 -c:v libx264 -crf 18 out.mp4`
(or OBS, 1080p30, x264, CRF 18). Record terminal and browser as two clips, cut with `ffmpeg concat`.

**Audio:** if VO, 150 wpm ceiling — the narration above is ~185 words, fits 75s. If no mic: burn the narration as on-screen text (same timings) and add low-volume ambient. Do not use TTS that sounds synthetic; judges discount it.

---

## X post (required by the rules — copy exactly)

```
Built at NEAR HACKATHON: Echoes — an on-chain guardrail vault for autonomous AI agents.

Give an agent a wallet without giving it the keys:
• key scoped to spend_as() only, enforced by the NEAR runtime
• per-tx / daily / per-agent caps + recipient allowlist, enforced by the contract
• a hijacked vendor invoice reverts on-chain instead of draining the vault

Live: xhanny6969.github.io/echoes
Code: github.com/xhanny6969/echoes
Contract: allowance-swarm.testnet (source-verified, hash matches)

@NEARProtocol #NEARHackathon
```

Attach the video. The rules require: mention NEAR HACKATHON, tag **@NEARProtocol**, hashtag **#NEARHackathon**, and the demo video or screenshots attached — all four in the post itself, not in a reply.

---

## Pitfalls

- **Do not show the simulated "Run Swarm Demo" button in the video** if you can show the real `node` run instead. A judge who notices the demo is simulated discounts the whole submission.
- **Do not say "jailbreak"** — the framing is counterparty compromise. That distinction is the strongest part of the story.
- **Do not show a hash you haven't queried.** Re-check before recording:
  `curl -s -X POST https://rpc.testnet.near.org -H 'content-type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tx","params":["<hash>","demo.allowance-merchant.testnet"]}'`
- **Do not put the repo/contract links only in the reply.** They go in the submission form (both fields are required) and in the post.
- Video length: if it goes past 90s, cut the verifiability step to a still frame — never cut the two runs.
