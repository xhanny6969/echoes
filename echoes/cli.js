#!/usr/bin/env node
/**
 * echoes — one command to spin up a guardrailed spending vault on NEAR.
 *
 *   echoes init --vault mylab.testnet --master mylab.testnet \
 *               --agent agent-1 --recipient merchant.testnet
 *
 * Creates the sub-account, deploys the guardrail contract, sets the policy,
 * mints a scoped FunctionCall key for one agent and registers that agent's
 * budget. The agent never receives a full-access key.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

function loadNearApi() {
  for (const c of ['near-api-js', '/root/.hermes/node/lib/node_modules/near-cli/node_modules/near-api-js']) {
    try {
      return require(c);
    } catch (e) {
      /* next */
    }
  }
  throw new Error('near-api-js not installed — run `npm install`');
}

const { connect, keyStores, KeyPair, utils } = loadNearApi();
const NETWORK = process.env.ECHOES_NETWORK || 'testnet';
const RPC = process.env.ECHOES_RPC || (NETWORK === 'testnet'
  ? 'https://rpc.testnet.fastnear.com'
  : 'https://rpc.mainnet.fastnear.com');
const YOCTO = 10n ** 24n;

const argv = process.argv.slice(2);
const cmd = argv[0];
const flags = {};
for (let i = 1; i < argv.length; i++) {
  if (argv[i].startsWith('--')) {
    const key = argv[i].slice(2);
    const next = argv[i + 1];
    flags[key] = next && !next.startsWith('--') ? (i++, next) : true;
  }
}

const nearAmount = (x) => utils.format.parseNearAmount(String(x));
const toYocto = (x) => (BigInt(Math.round(Number(x) * 1e6)) * 10n ** 18n).toString();

function credentialsPath(account) {
  return path.join(os.homedir(), '.near-credentials', NETWORK, `${account}.json`);
}

function readKey(account) {
  const p = credentialsPath(account);
  if (!fs.existsSync(p)) {
    throw new Error(`no key for ${account} at ${p} — run \`near login\` or pass --key`);
  }
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  // ~/.near-credentials uses `private_key`; the old near-api key store used
  // `secret_key`. Accept both so the CLI works with either store.
  return j.secret_key || j.private_key;
}

async function signer(accountId, secretKey) {
  const keyStore = new keyStores.InMemoryKeyStore();
  keyStore.setKey(NETWORK, accountId, KeyPair.fromString(secretKey));
  const near = await connect({ networkId: NETWORK, nodeUrl: RPC, keyStore, headers: {} });
  return near.account(accountId);
}

async function accountExists(accountId) {
  const res = await fetch(RPC, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 'echoes', method: 'query',
      params: { request_type: 'view_account', account_id: accountId, finality: 'final' },
    }),
  });
  const data = await res.json();
  return !data.error;
}

function configPath(vault) {
  return path.join(process.cwd(), '.echoes', `${vault}.json`);
}

function saveConfig(cfg) {
  const p = configPath(cfg.vault);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(cfg, null, 2));
  fs.chmodSync(p, 0o600);
  return p;
}

async function loadConfig(vault) {
  const p = vault ? configPath(vault) : path.join(process.cwd(), '.echoes', 'vault.json');
  if (!fs.existsSync(p)) throw new Error(`config not found: ${p}`);
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

// ---------------------------------------------------------------- commands
async function init() {
  const master = flags.master;
  let vault = flags.vault;
  if (!vault) throw new Error('--vault is required (a label like "lab", or a full account id)');
  if (!vault.includes('.')) {
    // A master account can only create its own sub-accounts, so a bare label
    // becomes <label>.<master>. Top-level names have to be registered first.
    if (!master) throw new Error('--master is required when --vault is a bare label');
    vault = `${vault}.${master}`;
  }
  if (vault !== master && master && !vault.endsWith(`.${master}`)) {
    throw new Error(`${vault} is not a sub-account of ${master}; only <label>.${master} can be created by it`);
  }
  if (vault.length > 64) throw new Error('account id too long (max 64 chars)');

  const wasmPath = flags.wasm || path.join(__dirname, '..', 'contract', 'allowance_vault.wasm');
  if (!fs.existsSync(wasmPath)) throw new Error(`wasm not found: ${wasmPath}`);
  const wasm = fs.readFileSync(wasmPath);

  const vaultPerTx = flags['vault-per-tx'] || '0.5';
  const vaultDaily = flags['vault-daily'] || '5.0';
  const agentPerTx = flags['per-tx'] || '0.2';
  const agentDaily = flags.daily || '1.0';
  const recipients = String(flags.recipient || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (!recipients.length) throw new Error('--recipient is required (the only accounts money may go to)');

  const fund = flags.fund || '4.0';

  console.log(`vault      ${vault}`);
  console.log(`master     ${master}`);
  console.log(`policy     ${vaultPerTx} /tx, ${vaultDaily} /day`);
  console.log(`recipients ${recipients.join(', ')}`);
  console.log(`wasm       ${wasmPath} (${wasm.length} bytes)`);

  const masterKey = flags.key || readKey(master);
  const masterAccount = await signer(master, masterKey);

  const vaultKey = KeyPair.fromRandom('ed25519');
  const exists = await accountExists(vault);

  if (!exists) {
    console.log(`\n[1/4] creating ${vault} with ${fund} NEAR …`);
    const res = await masterAccount.createAccount(vault, vaultKey.getPublicKey(), nearAmount(fund));
    console.log(`      ${res.transaction_outcome.id}`);
    await new Promise((r) => setTimeout(r, 1500));
  } else {
    console.log(`\n[1/4] ${vault} already exists — reusing`);
  }

  const vaultAccount = await signer(vault, vaultKey.toString());

  console.log('[2/4] deploying guardrail contract …');
  const dep = await vaultAccount.deployContract(wasm);
  console.log(`      ${dep.transaction_outcome.id}`);

  console.log('[3/4] initialising policy …');
  const initRes = await vaultAccount.functionCall({
    contractId: vault,
    methodName: 'new',
    args: { owner: vault, per_tx: toYocto(vaultPerTx), daily: toYocto(vaultDaily), recipients },
    gas: '100000000000000',
    attachedDeposit: '0',
  });
  console.log(`      ${initRes.transaction.hash}`);

  const agents = [];
  const agentIds = String(flags.agent || 'agent-1').split(',').map((s) => s.trim()).filter(Boolean);
  console.log('[4/4] minting scoped agent keys …');
  for (const agentId of agentIds) {
    const agentKey = KeyPair.fromRandom('ed25519');
    const pub = agentKey.getPublicKey().toString();
    // The protocol itself enforces this: receiver_id = vault, one method only.
    const ak = await vaultAccount.addKey(pub, vault, ['spend_as'], nearAmount(flags['key-allowance'] || '0.25'));
    console.log(`      key  ${pub.slice(0, 20)}…  ${ak.transaction_outcome.id}`);
    const reg = await vaultAccount.functionCall({
      contractId: vault,
      methodName: 'register_agent',
      args: { agent_id: agentId, public_key: pub, per_tx: toYocto(agentPerTx), daily: toYocto(agentDaily) },
      gas: '50000000000000',
      attachedDeposit: '0',
    });
    console.log(`      reg  ${agentId}  ${reg.transaction.hash}`);
    agents.push({
      id: agentId,
      public_key: pub,
      secret_key: agentKey.toString(),
      per_tx: agentPerTx,
      daily: agentDaily,
    });
    await new Promise((r) => setTimeout(r, 900));
  }

  const cfg = {
    vault,
    network: NETWORK,
    rpc: RPC,
    owner: vault,
    policy: { per_tx: vaultPerTx, daily: vaultDaily, recipients },
    agents,
    created_at: new Date().toISOString(),
  };
  const saved = saveConfig(cfg);
  console.log(`\nsaved ${saved}`);
  console.log('\nNext:');
  console.log(`  echoes status --vault ${vault}`);
  console.log(`  ECHOES_AGENT_ID=${agents[0].id} node agent/demo.js ${saved}`);
  console.log('\nHand the agent its secret_key and nothing else. It cannot transfer,');
  console.log('add keys, or call any method other than spend_as on this vault.');
}

async function status() {
  const cfg = await loadConfig(flags.vault);
  const c = await fetch(RPC, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 'echoes', method: 'query',
      params: {
        request_type: 'call_function', account_id: cfg.vault, method_name: 'get_policy',
        args_base64: Buffer.from('{}').toString('base64'), finality: 'final',
      },
    }),
  });
  const data = await c.json();
  if (data.error) throw new Error(JSON.stringify(data.error));
  const policy = JSON.parse(Buffer.from(data.result.result).toString());
  const near = (y) => (Number(y) / 1e24).toFixed(4);
  console.log(`vault        ${cfg.vault}`);
  console.log(`owner        ${policy.owner}`);
  console.log(`paused       ${policy.paused}`);
  console.log(`vault cap    ${near(policy.per_tx)} /tx   ${near(policy.daily)} /day`);
  console.log(`spent today  ${near(policy.spent_today)}`);
  console.log(`allowlist    ${policy.recipients.join(', ')}`);
  console.log(`agents       ${policy.agent_count}`);
  const agents = await fetch(RPC, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 'echoes', method: 'query',
      params: {
        request_type: 'call_function', account_id: cfg.vault, method_name: 'get_agents',
        args_base64: Buffer.from('{}').toString('base64'), finality: 'final',
      },
    }),
  }).then((r) => r.json());
  if (!agents.error && agents.result) {
    for (const a of JSON.parse(Buffer.from(agents.result.result).toString())) {
      console.log(`  ${a.agent_id.padEnd(14)} ${a.active ? 'active ' : 'REVOKED'} spent ${near(a.spent_today)} / ${near(a.daily)}`);
    }
  }
}

async function revoke() {
  const cfg = await loadConfig(flags.vault);
  const pub = flags.key || (cfg.agents.find((a) => a.id === flags.agent) || {}).public_key;
  if (!pub) throw new Error('pass --key ed25519:… or --agent <id> from the config');
  const ownerKey = flags.ownerKey || readKey(cfg.owner);
  const owner = await signer(cfg.owner, ownerKey);
  const res = await owner.functionCall({
    contractId: cfg.vault,
    methodName: 'revoke_agent',
    args: { public_key: pub },
    gas: '30000000000000',
    attachedDeposit: '0',
  });
  console.log(`revoked ${pub.slice(0, 24)}…  ${res.transaction.hash}`);
}

function doctor() {
  const wasm = path.join(__dirname, '..', 'contract', 'allowance_vault.wasm');
  console.log(`node            ${process.version}`);
  console.log(`network         ${NETWORK}`);
  console.log(`rpc             ${RPC}`);
  console.log(`near-api-js     ${loadNearApi() ? 'ok' : 'missing'}`);
  console.log(`wasm            ${fs.existsSync(wasm) ? fs.statSync(wasm).size + ' bytes' : 'MISSING'}`);
  console.log(`credentials dir ${path.join(os.homedir(), '.near-credentials', NETWORK)}`);
}

const commands = { init, status, revoke, doctor };

(async () => {
  if (!commands[cmd]) {
    console.log(`echoes — guardrailed spending vault for AI agents

  echoes init   --vault <id> --master <acct> --agent <id> --recipient <acct> [--fund 4]
  echoes status --vault <id>
  echoes revoke --vault <id> --agent <id>
  echoes doctor

Flags for init:
  --per-tx 0.2 --daily 1.0           per-agent budget (NEAR)
  --vault-per-tx 0.5 --vault-daily 5 total vault budget (NEAR)
  --key-allowance 0.25               gas allowance on the scoped key
  --wasm <path>                      override the contract build
`);
    process.exit(cmd ? 1 : 0);
  }
  try {
    await commands[cmd]();
  } catch (err) {
    console.error(`error: ${err.message}`);
    process.exit(1);
  }
})();