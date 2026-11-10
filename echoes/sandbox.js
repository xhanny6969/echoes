#!/usr/bin/env node
/**
 * echoes sandbox — a 60-second, self-service test of the guardrail vault.
 *
 * No LLM, no owner key, no wallet. It uses the deliberately public sandbox agent
 * key (testnet only, capped at 0.05 N/tx and 5 N/day) to attempt two spends:
 *
 *   1. a payment to the allowlisted merchant  -> must be ACCEPTED on chain
 *   2. a payment to an account NOT on the list -> must REVERT inside the contract
 *
 * Both outcomes are read back from the chain, not from local state. If either
 * expectation fails the script exits non-zero, so an agent can gate on it.
 *
 *   node echoes/sandbox.js            # uses echoes/sandbox.json
 *   node echoes/sandbox.js --json     # machine-readable result
 */
const fs = require('fs');
const path = require('path');

function loadNearApi() {
  // Portable resolution: local node_modules first, then the global npm root.
  // No author-machine paths — this must work on a judge's laptop too.
  const candidates = ['near-api-js'];
  try {
    const root = require('child_process')
      .execSync('npm root -g', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString().trim();
    if (root) candidates.push(require('path').join(root, 'near-api-js'));
  } catch (e) { /* npm missing is fine, a local install may still resolve */ }
  for (const c of candidates) {
    try {
      return require(c);
    } catch (e) {
      /* next */
    }
  }
  if (false) return null;
  throw new Error('near-api-js not installed — run `npm install` (or `npm i -g near-api-js`)');
}

const { connect, keyStores, KeyPair } = loadNearApi();
const AS_JSON = process.argv.includes('--json');

function loadSandbox() {
  const explicit = process.argv.slice(2).find((a) => !a.startsWith('--') && a.endsWith('.json'));
  const p = explicit ? path.resolve(explicit) : path.join(__dirname, 'sandbox.json');
  if (!fs.existsSync(p)) throw new Error(`sandbox config not found: ${p}`);
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

/** Receipt logs live on the chain, not in the thrown error — fetch them by hash. */
async function txLogs(rpcUrl, hash, senderId) {
  if (!hash) return { logs: [], ok: null };
  try {
    const res = await fetch(rpcUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0', id: 'echoes-sandbox', method: 'tx',
        params: { tx_hash: hash, sender_account_id: senderId, wait_until: 'FINAL' },
      }),
    });
    const data = await res.json();
    if (!data.result) return { logs: [], ok: null };
    const receipts = data.result.receipts_outcome || [];
    const logs = receipts.flatMap((r) => (r.outcome && r.outcome.logs) || []);
    const status = data.result.status || {};
    return { logs, ok: !Object.keys(status).includes('Failure') };
  } catch (e) {
    return { logs: [], ok: null };
  }
}

const nearAmount = (x) => {
  const s = String(x).trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error(`invalid NEAR amount: ${x}`);
  const [whole, frac = ''] = s.split('.');
  const frac24 = (frac + '0'.repeat(24)).slice(0, 24);
  return (BigInt(whole || '0') * 10n ** 24n + BigInt(frac24)).toString();
};

async function main() {
  const cfg = loadSandbox();
  const keyStore = new keyStores.InMemoryKeyStore();
  keyStore.setKey(cfg.network || 'testnet', cfg.vault, KeyPair.fromString(cfg.secret_key));
  const near = await connect({
    networkId: cfg.network || 'testnet',
    nodeUrl: cfg.rpc || 'https://rpc.testnet.fastnear.com',
    keyStore,
    headers: {},
  });
  const account = await near.account(cfg.vault);

  const tests = [
    {
      name: 'allowlisted recipient',
      expect: 'accepted',
      recipient: cfg.allowed_recipient,
      amount: cfg.amount,
      why: 'recipient is on the vault allowlist and inside every cap',
    },
    {
      name: 'recipient not on allowlist',
      expect: 'reverted',
      recipient: cfg.blocked_recipient,
      amount: cfg.amount,
      why: 'the vault only pays allowlisted accounts — this is the hijack case',
    },
  ];

  const results = [];
  for (const t of tests) {
    let hash = null;
    let ok = false;
    let reason = '';
    try {
      const res = await account.functionCall({
        contractId: cfg.vault,
        methodName: 'spend_as',
        args: { amount: nearAmount(t.amount), recipient: t.recipient, memo: 'echoes sandbox self-test' },
        gas: '30000000000000',
        attachedDeposit: '0',
      });
      hash = res.transaction_outcome.id;
      ok = true;
    } catch (err) {
      const msg = String(err && err.message ? err.message : err);
      const logHit = msg.match(/allowkey:\s*[^"\\]+/i);
      const blockedHit = msg.match(/[a-z ]*blocked[^"\\]*/i);
      reason = String((logHit && logHit[0]) || (blockedHit && blockedHit[0]) || 'reverted (no log captured)')
        .split('\\n')[0]
        .trim();
      hash = (err && err.transaction_outcome && err.transaction_outcome.id)
        || (msg.match(/"transaction_outcome":\{"id":"([1-9A-HJ-NP-Za-km-z]+)"/) || [])[1]
        || null;
      ok = false;
      const onchain = await txLogs(cfg.rpc || 'https://rpc.testnet.fastnear.com', hash, cfg.vault);
      const hit = (onchain.logs || []).find((l) => /blocked|allowkey|cap|paused/i.test(l));
      if (hit) reason = hit.trim();
      if (t.expect === 'reverted' && !/recipient is not on the allowlist/i.test(reason)) {
        // A daily/tx cap revert is NOT proof of the allowlist. Never report it as a pass.
        results.push({ test: t.name, recipient: t.recipient, amount: t.amount,
          expected: t.expect, outcome: 'reverted', pass: false, hash, reason: reason || 'revert reason unavailable',
          explorer: hash ? `https://testnet.nearblocks.io/txns/${hash}` : null });
        if (!AS_JSON) {
          console.log(`FAIL  ${t.name}`);
          console.log(`      expected allowlist block, got: ${reason || 'revert reason unavailable'}`);
          if (hash) console.log(`      tx ${hash}`);
          console.log('');
        }
        continue;
      }
    }
    const outcome = ok ? 'accepted' : 'reverted';
    results.push({
      test: t.name,
      recipient: t.recipient,
      amount: t.amount,
      expected: t.expect,
      outcome,
      pass: outcome === t.expect,
      hash,
      reason: reason || (ok ? 'transfer executed' : ''),
      explorer: hash ? `https://testnet.nearblocks.io/txns/${hash}` : null,
    });
    if (!AS_JSON) {
      const mark = outcome === t.expect ? 'PASS' : 'FAIL';
      console.log(`${mark}  ${t.name}`);
      console.log(`      recipient ${t.recipient} · ${t.amount} N · expected ${t.expect}, got ${outcome}`);
      if (hash) console.log(`      tx ${hash}`);
      if (reason) console.log(`      chain said: ${reason}`);
      console.log('');
    }
  }

  const allPass = results.every((r) => r.pass);
  if (AS_JSON) {
    console.log(JSON.stringify({ vault: cfg.vault, agent: cfg.agent, all_pass: allPass, results }, null, 2));
  } else {
    console.log(`vault ${cfg.vault} · agent ${cfg.agent}`);
    console.log(allPass
      ? 'SELF-TEST PASSED — the contract blocked what it is supposed to block, on chain.'
      : 'SELF-TEST FAILED — an expected outcome did not happen. Paste this output to the maintainer.');
  }
  process.exit(allPass ? 0 : 1);
}

main().catch((err) => {
  console.error(`error: ${err.message}`);
  process.exit(2);
});
