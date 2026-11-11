/**
 * Echoes — multi-wallet connect layer for the live console.
 *
 * Built into `site/assets/wallet-selector.js` by `npm run build:wallet`
 * (esbuild, IIFE, global name `EchoesWallet`). Source lives here so the bundle
 * is reproducible rather than a mystery blob in git.
 *
 * Why a bundle and not a CDN script: @near-wallet-selector ships ESM only, and
 * the console is a static page with no bundler at runtime. Vendoring the build
 * keeps GitHub Pages / Vercel / dashboard.py all served from the same file.
 *
 * Design rules:
 *  - no modal of our own: the console's wallet modal renders the picker, this
 *    module only knows how to sign in / sign out / sign a transaction.
 *  - one selector instance per (network, contractId); rebuilt only when the
 *    console switches vault accounts.
 *  - every failure is thrown with a readable message, never swallowed, so the
 *    UI can show "extension not installed" instead of hanging.
 */
import { setupWalletSelector } from "@near-wallet-selector/core";
import { setupMyNearWallet } from "@near-wallet-selector/my-near-wallet";
import { setupMeteorWallet } from "@near-wallet-selector/meteor-wallet";
import { setupHereWallet } from "@near-wallet-selector/here-wallet";
import { setupSender } from "@near-wallet-selector/sender";
import { setupNightly } from "@near-wallet-selector/nightly";

const NETWORK = "testnet";
const ICONS = {
  "my-near-wallet": "assets/wallets/my-near-wallet.png",
  "meteor-wallet": "assets/wallets/meteor.png",
  "here-wallet": "assets/wallets/here.png",
  sender: "assets/wallets/sender.png",
  nightly: "assets/wallets/nightly.png",
};

// Order matters: the same order the console's picker renders.
const WALLETS = [
  { id: "my-near-wallet", name: "MyNearWallet", note: "web · redirect" },
  { id: "meteor-wallet", name: "Meteor", note: "extension or popup" },
  { id: "here-wallet", name: "HERE", note: "popup sign-in" },
  { id: "sender", name: "Sender", note: "extension" },
  { id: "nightly", name: "Nightly", note: "extension or app" },
];

const modules = [
  setupMyNearWallet({ iconUrl: ICONS["my-near-wallet"] }),
  setupMeteorWallet({ iconUrl: ICONS["meteor-wallet"] }),
  setupHereWallet({ iconUrl: ICONS["here-wallet"] }),
  setupSender({ iconUrl: ICONS["sender"] }),
  setupNightly({ iconUrl: ICONS["nightly"] }),
];

let selector = null;
let selectorKey = "";
let lastOutcome = null;
const listeners = new Set();

function emit() {
  const state = snapshot();
  listeners.forEach((fn) => {
    try {
      fn(state);
    } catch (_) {
      /* a broken listener must not break signing */
    }
  });
}

function activeAccount(sel) {
  const state = sel.store.getState();
  const accounts = state.accounts || [];
  const wanted = state.selectedWalletId || "";
  const owned = accounts.filter((a) => a.walletId === wanted);
  const pick = (owned.length ? owned : accounts)[0];
  return pick ? { accountId: pick.accountId, walletId: pick.walletId || wanted } : null;
}

function snapshot() {
  const acc = selector ? activeAccount(selector) : null;
  return {
    signedIn: Boolean(acc),
    accountId: acc ? acc.accountId : null,
    walletId: acc ? acc.walletId : null,
    wallets: WALLETS.slice(),
  };
}

/** One selector per contract: the access key it mints is scoped to that contract. */
async function ensureSelector(contractId) {
  const key = `${NETWORK}:${contractId}`;
  if (selector && selectorKey === key) return selector;
  selectorKey = key;
  selector = await setupWalletSelector({
    network: NETWORK,
    debug: false,
    modules,
    createAccessKeyFor: contractId,
  });
  selector.on("signedIn", emit);
  selector.on("signedOut", emit);
  selector.on("accountsChanged", emit);
  return selector;
}

async function connect(walletId, contractId, methodNames) {
  if (!walletId) throw new Error("connect(walletId) requires a wallet id");
  if (!contractId) throw new Error("connect(walletId, contractId) requires a contract account id");
  // Default scope is the guardrail method only: the wallet mints a
  // FunctionCall(spend_as) key, never a full-access key for the contract.
  const scope = Array.isArray(methodNames) && methodNames.length ? methodNames : ["spend_as"];
  const sel = await ensureSelector(contractId);
  const wallet = await sel.wallet(walletId);
  const here = typeof window !== "undefined" ? window.location.href : "";
  try {
    await wallet.signIn({
      contractId,
      methodNames: scope,
      successUrl: here,
      failureUrl: here,
    });
  } catch (err) {
    const raw = String((err && err.message) || err);
    const hint = /not installed|not available|undefined|Cannot read/i.test(raw) && walletId !== "my-near-wallet"
      ? ` — ${walletId} not detected in this browser. Install the extension, or pick a web wallet (MyNearWallet / HERE / Meteor).`
      : "";
    throw new Error(raw + hint);
  }
  const acc = activeAccount(sel);
  emit();
  return acc ? acc.accountId : null;
}

async function disconnect() {
  if (!selector) return;
  const acc = activeAccount(selector);
  if (!acc) return;
  const wallet = await selector.wallet(acc.walletId);
  await wallet.signOut();
  lastOutcome = null;
  emit();
}

/**
 * Rehydrate a previous sign-in without opening anything. The selector persists
 * walletId + accounts in localStorage; initialising with the same contract is
 * enough to get the account back, which is what the console calls on page load.
 */
async function restore(contractId) {
  if (!contractId) return null;
  const sel = await ensureSelector(contractId);
  const acc = activeAccount(sel);
  emit();
  return acc ? acc.accountId : null;
}

async function signAndSend({ receiverId, methodName, args = {}, gas = "100000000000000", deposit = "0" }) {
  if (!selector) throw new Error("wallet selector not initialised — connect a wallet first");
  const acc = activeAccount(selector);
  if (!acc) throw new Error("no wallet connected");
  const wallet = await selector.wallet(acc.walletId);
  const outcome = await wallet.signAndSendTransaction({
    receiverId,
    actions: [{ type: "FunctionCall", params: { methodName, args, gas, deposit } }],
  });
  lastOutcome = outcome || null;
  const txHash =
    (outcome && outcome.transaction && outcome.transaction.hash) ||
    (outcome && outcome.transaction_outcome && outcome.transaction_outcome.id) ||
    null;
  return { txHash, outcome, accountId: acc.accountId };
}

const api = {
  version: "1.0.0",
  network: NETWORK,
  wallets: WALLETS.slice(),
  connect,
  disconnect,
  restore,
  signAndSend,
  isSignedIn: () => snapshot().signedIn,
  getAccountId: () => snapshot().accountId,
  getWalletId: () => snapshot().walletId,
  state: snapshot,
  lastOutcome: () => lastOutcome,
  onChange(fn) {
    listeners.add(fn);
    fn(snapshot());
    return () => listeners.delete(fn);
  },
  /** Probe which extension wallets this browser actually exposes (UI hint only). */
  detected() {
    if (typeof window === "undefined") return [];
    const found = [];
    if (window.meteorWallet || window.meteorWalletNear) found.push("meteor-wallet");
    if (window.herewallet) found.push("here-wallet");
    if (window.sender || window.senderNear) found.push("sender");
    if (window.nightly) found.push("nightly");
    return found;
  },
};

window.EchoesWallet = api;
export default api;
