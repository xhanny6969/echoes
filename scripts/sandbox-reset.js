#!/usr/bin/env node
/**
 * sandbox-reset — rolls the public sandbox agent's daily counter and keeps it funded.
 *
 * The public sandbox key is capped at 0.25 N/day, so after ~5 self-tests the vault starts
 * rejecting legitimate runs. This runs as the OWNER (the vault's own key, which is NOT
 * published) and calls reset_agent_day for the sandbox agent.
 *
 *   node scripts/sandbox-reset.js            # uses echoes/sandbox.json for vault/agent ids
 *   node scripts/sandbox-reset.js --topup 0.5
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const { connect, keyStores, KeyPair } = require('near-api-js');

const NETWORK = 'testnet';
const RPC = process.env.ECHOES_RPC || 'https://rpc.testnet.fastnear.com';
const cfg = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'echoes', 'sandbox.json'), 'utf8'));
const vault = cfg.vault;
const topupIdx = process.argv.indexOf('--topup');
const topup = topupIdx > -1 ? process.argv[topupIdx + 1] : null;

(async () => {
  const credPath = path.join(os.homedir(), '.near-credentials', NETWORK, `${vault}.json`);
  if (!fs.existsSync(credPath)) {
    console.error(`no owner key at ${credPath} — this script needs the vault owner key (never publish it)`);
    process.exit(2);
  }
  const cred = JSON.parse(fs.readFileSync(credPath, 'utf8'));
  const keyStore = new keyStores.InMemoryKeyStore();
  keyStore.setKey(NETWORK, vault, KeyPair.fromString(cred.private_key || cred.secret_key));
  const near = await connect({ networkId: NETWORK, nodeUrl: RPC, keyStore, headers: {} });
  const account = await near.account(vault);

  const res = await account.functionCall({
    contractId: vault,
    methodName: 'reset_agent_day',
    args: { agent_id: cfg.agent },
    gas: '30000000000000',
    attachedDeposit: '0',
  });
  console.log(`reset_agent_day ${cfg.agent} -> ${res.transaction_outcome.id}`);

  if (topup) {
    // move funds from the master account that funds the sandbox (owner key of the vault
    // is enough for policy calls; funding needs a transfer from a funded account).
    console.log(`topup requested ${topup} N — send it from a funded testnet account to ${vault}`);
  }
})().catch((e) => {
  console.error(`error: ${e.message}`);
  process.exit(1);
});
