#!/usr/bin/env python3
"""Add two judge-facing FAQ entries to site/index.html:
   - new Q02: why not a plain sub-account (inserted right after Q01)
   - NEP-141 roadmap bullet in the roadmap answer
   Renumbers Q02..Q07 -> Q03..Q08 and updates the ALL QUESTIONS counter.
Idempotent: bails out if the marker string is already present.
"""
import re
import sys
from pathlib import Path

P = Path(__file__).resolve().parent.parent / "site" / "index.html"
src = P.read_text(encoding="utf-8")
orig = src

MARKER = "VS. PLAIN SUB-ACCOUNT"
if MARKER in src:
    print("already applied — nothing to do")
    sys.exit(0)

# ---------------------------------------------------------------- 1. renumber
# highest first so we never collide with a number we still need.
for old, new in [(7, 8), (6, 7), (5, 6), (4, 5), (3, 4), (2, 3)]:
    needle = "[Q%02d]" % old
    assert src.count(needle) == 1, f"expected 1 occurrence of {needle}, got {src.count(needle)}"
    src = src.replace(needle, "[Q%02d]" % new)
# comment banners
for old, new in [(7, 8), (6, 7), (5, 6), (4, 5), (3, 4), (2, 3)]:
    needle = "<!-- FAQ %02d -->" % old
    assert src.count(needle) == 1, f"expected 1 occurrence of {needle}"
    src = src.replace(needle, "<!-- FAQ %02d -->" % new)

# --------------------------------------------------------------- 2. new Q02
NEW = """          <!-- FAQ 02 -->
          <div class="faq-item border border-near-border rounded bg-near-obsidian/80 transition-colors" data-category="security">
            <button onclick="toggleFaqItem(this)" class="w-full text-left p-4 sm:p-5 flex items-start justify-between gap-4 focus:outline-none">
              <div class="flex items-start gap-3 min-w-0">
                <span class="font-mono text-xs text-near-green font-bold mt-0.5">[Q02]</span>
                <div>
                  <h3 class="font-sans font-bold text-base sm:text-lg text-near-alabaster">
                    Why not just fund a plain sub-account with a small balance? Why does an agent need a vault contract at all?
                  </h3>
                  <span class="inline-block mt-1 font-mono text-[10px] px-2 py-0.5 rounded bg-near-border/40 text-near-slate">VS. PLAIN SUB-ACCOUNT</span>
                </div>
              </div>
              <span class="faq-icon w-6 h-6 rounded border border-near-border flex items-center justify-center font-mono text-sm text-near-slate shrink-0 transition-transform">+</span>
            </button>
            <div class="faq-answer px-4 sm:px-5 pb-5 pt-1 text-sm text-near-slate space-y-3 border-t border-near-border/60">
              <p>
                Because a sub-account constrains <em>how much sits in the account</em> — never <em>where the money may go</em>. Three structural gaps make "capped sub-account" a weaker guarantee than it looks:
              </p>
              <ol class="list-decimal list-inside space-y-2 font-mono text-xs text-near-alabaster bg-near-carbon p-3.5 rounded border border-near-border">
                <li>
                  <span class="text-near-green">Access keys scope method names, never arguments.</span>
                  A NEAR access key is a tuple of <code class="text-near-cyan">(receiver_id, method_names, allowance)</code>. The runtime checks the <em>method being called</em>; it never parses that method's JSON arguments. A key scoped to <code class="font-mono">usdc.testnet</code> / <code class="font-mono">["ft_transfer"]</code> is therefore a key that can move <strong class="text-near-red">any amount to any recipient</strong> — <code class="font-mono">ft_transfer({"receiver_id": "hacker-drain.testnet", "amount": "..."})</code> is a valid call for that key. A recipient allowlist cannot live in a key; it can only live in a contract that owns the funds.
                </li>
                <li>
                  <span class="text-near-green">Splitting liquidity destroys the global ceiling.</span>
                  N sub-accounts means N independent balances. There is then no single on-chain place that enforces your real fleet-wide limit: to know the swarm spent at most 5 Ⓝ today you must aggregate off-chain — i.e. trust a keeper bot. Echoes keeps one treasury and enforces the per-agent daily cap and the vault daily cap against the same counters inside the same receipt, so the global ceiling is a <span class="text-near-green">storage invariant</span>, not a monitoring claim.
                </li>
                <li>
                  <span class="text-near-green">The balance is all-or-nothing.</span>
                  With a sub-account the pre-loaded balance is the only real limit, which forces a bad choice: fund it small and the agent dies mid-task on an unhelpful "insufficient balance" (or silently retries and burns gas); fund it large and one compromise takes everything at once, because nothing caps the <em>individual</em> transfer. Echoes separates "funded" from "spendable": the vault holds the capital, and policy decides what leaves per transaction and per day.
                </li>
              </ol>
              <p>
                <strong class="text-near-alabaster">The Echoes answer: the agent never holds the money.</strong> It holds a <code class="text-near-cyan">FunctionCall</code> key on <code class="font-mono text-near-alabaster">allowance-swarm.testnet</code> scoped to <code class="text-near-green">["spend", "spend_as"]</code>, with no transferable allowance — protocol rules already forbid that key from attaching a deposit. Every payout must pass <code class="text-near-cyan">assert_recipient()</code> plus the per-agent and vault arithmetic ceilings <em>before</em> <code class="font-mono">Promise::transfer</code> is even constructed. Worst case for a fully compromised agent key: at most <code class="font-mono">per_tx</code> NEAR, to an address you approved in advance, with the attempt sitting in <code class="font-mono">get_log()</code>.
              </p>
              <p class="font-mono text-[11px] text-near-dim">
                Honest limit: this protects the <strong>funds in the vault</strong>. A leaked agent key can still burn its own gas allowance on reverted calls — that is bounded by the key's gas allowance (≤ 0.25 Ⓝ) and by <code class="font-mono">revoke_agent</code>, not by Echoes' policy engine.
              </p>
            </div>
          </div>

          <!-- FAQ 03 -->"""

src = src.replace("          <!-- FAQ 03 -->", NEW, 1)

# ------------------------------------------------- 3. counter in the tab label
assert src.count("ALL QUESTIONS (7)") == 1
src = src.replace("ALL QUESTIONS (7)", "ALL QUESTIONS (8)")

# ------------------------------------- 4. correct the spend_as call signature
bad = "<code class=\"font-mono text-near-green\">spend_as(agent_id, recipient, amount)</code> call traverses five sequential verification gates"
good = "<code class=\"font-mono text-near-green\">spend_as(amount, recipient, memo)</code> call traverses five sequential verification gates &mdash; the agent is identified by the <code class=\"font-mono text-near-cyan\">signer_account_pk()</code> of the calling key, not by a passed-in id"
assert src.count(bad) == 1
src = src.replace(bad, good)

# --------------------------------------------------- 5. NEP-141 roadmap bullet
anchor = """                <li><strong class="text-near-cyan">NEAR Intents Solver Verification:</strong> Agents can submit bounded swap or compute intents where Echoes verifies maximum slippage and daily notional caps before committing solver settlement.</li>"""
assert src.count(anchor) == 1
nep = """                <li><strong class="text-near-alabaster">NEP-141 stablecoin policies (USDC / USDT) &mdash; roadmap, not in this build:</strong> real agents pay in stablecoins, and today <code class="font-mono text-near-green">spend_as</code> schedules only a native <code class="font-mono">Promise::transfer</code>. Shipping NEP-141 spending needs three concrete changes we have scoped but deliberately not faked: a <code class="font-mono text-near-cyan">spend_ft(amount, token, recipient, memo)</code> path that calls <code class="font-mono">ft_transfer</code> on the token contract (with the same <code class="font-mono">assert_recipient</code> allowlist), a per-token <code class="font-mono">spent_today</code> counter keyed by token contract account, and a one-time <code class="font-mono">storage_deposit</code> so the vault can hold the token balance. Gates 02&ndash;05 already operate on plain <code class="font-mono">u128</code> integers and are token-agnostic, so the policy struct applies per token unchanged.</li>
                <li><strong class="text-near-cyan">NEAR Intents Solver Verification:</strong> Agents can submit bounded swap or compute intents where Echoes verifies maximum slippage and daily notional caps before committing solver settlement &mdash; the same counters, extended to a notional ceiling so an intent can be rejected before a solver ever quotes.</li>"""
src = src.replace(anchor, nep, 1)

P.write_text(src, encoding="utf-8")
print("ok  delta=%+d bytes" % (len(src) - len(orig)))
