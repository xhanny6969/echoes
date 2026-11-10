#!/usr/bin/env node
/**
 * Echoes — acceptance suite against the live testnet deployment.
 *
 * Read-only checks always run. Write checks run only with ECHOES_TX=1 and keys
 * supplied via env, so a judge can verify state without holding anything:
 *
 *   node scripts/acceptance.js                       # read-only
 *   ECHOES_TX=1 \
 *   ECHOES_OWNER_KEY=ed25519:... \
 *   ECHOES_AGENT_KEY=ed25519:... \
 *   node scripts/acceptance.js                       # full suite
 *
 * Every PASS/FAIL line is derived from an RPC response or a receipt outcome,
 * never from local bookkeeping. Exit code is 0 only if nothing failed.
 */
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

function loadNearApi() {
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
    } catch (e) { /* next */ }
  }
  throw new Error('near-api-js not installed — run `npm install`');
}

const { connect, keyStores, KeyPair } = loadNearApi();

// Minimal base58 (bitcoin alphabet) so this script has no dependency beyond near-api-js.
const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
function bs58encode(buf) {
  let zeros = 0;
  while (zeros < buf.length && buf[zeros] === 0) zeros++;
  let n = BigInt('0x' + (buf.toString('hex') || '0'));
  let out = '';
  while (n > 0n) { out = B58[Number(n % 58n)] + out; n /= 58n; }
  return '1'.repeat(zeros) + out;
}
const bs58 = { encode: bs58encode };

const VAULT = process.env.ECHOES_VAULT || 'allowance-swarm.testnet';
const RPC = process.env.ECHOES_RPC || 'https://rpc.testnet.fastnear.com';
const NETWORK = process.env.ECHOES_NETWORK || 'testnet';
const ALLOWED = process.env.ECHOES_ALLOWED || 'allowance-merchant.testnet';

const results = [];
function record(name, pass, detail) {
  results.push({ name, pass, detail });
  console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`);
}

async function view(method, args = {}) {
  const res = await fetch(RPC, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 'acceptance', method: 'query',
      params: { request_type: 'call_function', finality: 'final', account_id: VAULT, method_name: method, args_base64: Buffer.from(JSON.stringify(args)).toString('base64') },
    }),
  });
  const j = await res.json();
  if (j.error) throw new Error(`${method}: ${JSON.stringify(j.error.cause || j.error)}`);
  return JSON.parse(Buffer.from(j.result.result).toString('utf8'));
}

function connector(accountId, secretKey) {
  const keyStore = new keyStores.InMemoryKeyStore();
  keyStore.setKey(NETWORK, accountId, KeyPair.fromString(secretKey));
  return connect({ networkId: NETWORK, nodeUrl: RPC, keyStore, headers: {} });
}

/** Returns { ok, reason, hash } from a receipt outcome, or the RPC-level failure.
 *
 *  near-api-js throws on a failed receipt and the thrown error carries only the raw
 *  Failure JSON — the contract's own log line ("blocked — …") is printed to the
 *  console instead. So we tee console.log for the duration of the call and pick the
 *  receipt log out of it. That log line is the actual proof of which gate fired. */
async function call(account, method, args) {
  const captured = [];
  const origLog = console.log;
  console.log = (...a) => { captured.push(a.map(String).join(' ')); origLog(...a); };
  const receiptLogs = () => captured
    .filter((l) => /Log \[/.test(l))
    .map((l) => l.replace(/^.*?Log \[[^\]]*\]:\s*/, '').trim())
    .filter(Boolean);
  try {
    const r = await account.functionCall({
      contractId: VAULT, methodName: method, args, gas: '30000000000000', attachedDeposit: '0',
    });
    const status = r.status;
    if (typeof status === 'object' && status.Failure) {
      return { ok: false, reason: receiptLogs().join(' | ') || JSON.stringify(status.Failure).slice(0, 200), hash: r.transaction_outcome.id };
    }
    return { ok: true, reason: 'executed', hash: r.transaction_outcome.id };
  } catch (e) {
    const logs = receiptLogs().join(' | ');
    return {
      ok: false,
      reason: logs || `wasm trap: ${String(e.message || e).replace(/\s+/g, ' ').slice(0, 120)}`,
      hash: null,
    };
  } finally {
    console.log = origLog;
  }
}

(async () => {
  console.log(`vault: ${VAULT}  rpc: ${RPC}\n`);

  // ---------------------------------------------------------------- read-only
  const policy = await view('get_policy');
  record('policy reads from chain', !!policy.owner_id || !!policy.owner, `owner=${policy.owner_id || policy.owner} per_tx=${policy.per_tx} daily=${policy.daily}`);

  const balance = await view('get_balance');
  record('balance reads from chain', /^\d+$/.test(String(balance)), `${(Number(balance) / 1e24).toFixed(4)} N`);

  const agents = await view('get_agents');
  const active = agents.filter((a) => a.active);
  record('registered agents visible', agents.length > 0, `${agents.length} total, ${active.length} active`);

  const logLen = await view('get_log_len');
  record('spend log readable', Number(logLen) >= 0, `${logLen} entries`);

  // code hash vs the wasm in this repo
  const wasmPath = path.join(__dirname, '..', 'contract', 'allowance_vault.wasm');
  const localHash = bs58.encode(crypto.createHash('sha256').update(fs.readFileSync(wasmPath)).digest());
  const codeRes = await fetch(RPC, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: 'code', method: 'query', params: { request_type: 'view_code', finality: 'final', account_id: VAULT } }),
  }).then((r) => r.json());
  const onchainHash = codeRes.result && codeRes.result.code_base64
    ? bs58.encode(crypto.createHash('sha256').update(Buffer.from(codeRes.result.code_base64, 'base64')).digest())
    : null;
  record('deployed wasm == repo wasm byte for byte', onchainHash === localHash, `${String(onchainHash).slice(0, 16)}… vs ${localHash.slice(0, 16)}…`);

  if (process.env.ECHOES_TX !== '1') {
    console.log('\nwrite checks skipped (set ECHOES_TX=1 + ECHOES_OWNER_KEY / ECHOES_AGENT_KEY)');
  } else {
    const ownerKey = process.env.ECHOES_OWNER_KEY;
    const agentKey = process.env.ECHOES_AGENT_KEY;
    if (!ownerKey || !agentKey) throw new Error('ECHOES_TX=1 requires ECHOES_OWNER_KEY and ECHOES_AGENT_KEY');

    const owner = await (await connector(VAULT, ownerKey)).account(VAULT);
    const agent = await (await connector(VAULT, agentKey)).account(VAULT);

    const before = await view('get_policy');
    const room = Number(before.daily) - Number(before.spent_today);
    console.log(`\nremaining today: ${(room / 1e24).toFixed(3)} N\n`);

    // 1. happy path
    const ok = await call(agent, 'spend_as', { amount: '50000000000000000000000', recipient: ALLOWED, memo: 'acceptance: allowlisted' });
    record('spend_as to allowlisted recipient executes', ok.ok === true, ok.hash ? `tx ${ok.hash}` : ok.reason);

    // 2. allowlist breach
    const rogue = await call(agent, 'spend_as', { amount: '50000000000000000000000', recipient: 'not-on-the-allowlist.testnet', memo: 'acceptance: rogue' });
    record('spend_as to unknown recipient reverts', rogue.ok === false && /allowlist/i.test(rogue.reason), rogue.hash ? `tx ${rogue.hash} → ${rogue.reason.slice(0, 60)}` : rogue.reason.slice(0, 80));

    // 3. per-transaction cap
    const over = await call(agent, 'spend_as', { amount: '300000000000000000000000', recipient: ALLOWED, memo: 'acceptance: over per-tx' });
    record('spend_as above per-tx cap reverts', over.ok === false && /per-transaction|per_tx/i.test(over.reason), over.reason.slice(0, 70));

    // 4. admin isolation at the protocol layer
    const esc = await call(agent, 'set_paused', { paused: false });
    record('agent key cannot call set_paused (protocol rejects)', esc.ok === false, esc.reason.slice(0, 90));

    const esc2 = await call(agent, 'revoke_agent', { public_key: 'ed25519:11111111111111111111111111111111' });
    record('agent key cannot call revoke_agent (protocol rejects)', esc2.ok === false, esc2.reason.slice(0, 90));

    // 5. owner circuit breaker
    const p1 = await call(owner, 'set_paused', { paused: true });
    const blocked = await call(agent, 'spend_as', { amount: '10000000000000000000000', recipient: ALLOWED, memo: 'acceptance: paused' });
    const p2 = await call(owner, 'set_paused', { paused: false });
    record('owner pause blocks agent spending', p1.ok && blocked.ok === false && /paused/i.test(blocked.reason), blocked.reason.slice(0, 70));
    record('owner unpause restores spending', p2.ok, p2.hash ? `tx ${p2.hash}` : p2.reason);

    // 6. accounting moved by exactly the accepted amount
    const after = await view('get_policy');
    const delta = Number(after.spent_today) - Number(before.spent_today);
    record('spent_today moved by exactly one accepted tx', delta === 50000000000000000000000, `${(delta / 1e24).toFixed(4)} N`);
  }

  const failed = results.filter((r) => !r.pass);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  process.exit(failed.length === 0 ? 0 : 1);
})().catch((e) => {
  console.error('acceptance aborted:', e.message);
  process.exit(2);
});
