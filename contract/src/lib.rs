//! Allowance — protocol-enforced spending vault for AI agents on NEAR.
//!
//! The owner funds the vault and registers *function call access keys* on the
//! vault account whose allowance covers gas only and whose method allowlist is
//! exactly `["spend"]` (single agent) or `["spend_as"]` (swarm agents).
//!
//! The protocol therefore stops an agent from calling anything else on the
//! vault, and stops it once its key allowance runs out. This contract adds the
//! money rules on top:
//!
//!   * vault-level per-tx cap, rolling daily cap, recipient allowlist, kill switch
//!   * per-agent budgets in swarm mode: each registered agent key has its own
//!     per-tx and daily cap, so agent A cannot eat agent B's budget.
//!
//! The vault-level caps apply in *both* modes: `spend_as` checks the vault
//! per-tx / daily caps and increments the vault `spent_today` counter, so
//! the sum of all agents can never exceed the vault daily cap. An agent's
//! own caps may never exceed the vault caps (enforced at registration and
//! at `set_agent_policy`).
//!
//! Swarm identity is the *signer public key*. A function call key lives on the
//! vault account, so `env::signer_account_pk()` tells the contract which
//! registered agent signed the call. No off-chain registry, no trusted
//! middleware, nothing the agent can spoof by changing its own arguments.

use near_sdk::{env, near, AccountId, NearToken, PanicOnDefault, Promise, PublicKey};

const NS_PER_DAY: u64 = 86_400_000_000_000;
const MAX_MEMO_LEN: usize = 256;
const MAX_LOG_ENTRIES: usize = 200;

fn parse_u128(raw: &str, label: &str) -> u128 {
    raw.parse::<u128>()
        .unwrap_or_else(|_| fail(&format!("{label} not a number")))
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct SpendEntry {
    pub id: u64,
    pub caller: AccountId,
    pub agent: String,
    pub amount: String,
    pub recipient: AccountId,
    pub memo: String,
    pub timestamp: u64,
}

#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct Policy {
    pub owner: AccountId,
    pub per_tx: String,
    pub daily: String,
    pub spent_today: String,
    pub day: u64,
    pub recipients: Vec<AccountId>,
    pub paused: bool,
    pub agent_count: u64,
}

/// What a judge sees when they ask about one swarm agent.
#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct AgentPolicy {
    pub agent_id: AccountId,
    pub public_key: String,
    pub per_tx: String,
    pub daily: String,
    pub spent_today: String,
    pub remaining_today: String,
    pub active: bool,
}

/// Internal storage record. `public_key` is the raw key bytes as returned by
/// `env::signer_account_pk().into_bytes()`.
#[near(serializers = [json, borsh])]
#[derive(Clone)]
pub struct AgentState {
    pub agent_id: AccountId,
    pub public_key: Vec<u8>,
    pub per_tx: u128,
    pub daily: u128,
    pub day: u64,
    pub spent_today: u128,
    pub active: bool,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Vault {
    owner: AccountId,
    per_tx: u128,
    daily: u128,
    day: u64,
    spent_today: u128,
    recipients: Vec<AccountId>,
    paused: bool,
    log: Vec<SpendEntry>,
    next_id: u64,
    agents: Vec<AgentState>,
}

/// Fail a call with a reason the caller can actually read.
///
/// The wasm profile ends up compiled with `panic = "abort"`, so a bare
/// `panic_str` surfaces to the RPC as an opaque
/// `WebAssembly trap: An 'unreachable' opcode was executed` with no message.
/// Logging first puts the reason in the receipt logs, which is what the demo,
/// the dashboard and anyone reading the explorer sees.
fn fail(msg: &str) -> ! {
    env::log_str(&format!("allowkey: blocked — {msg}"));
    env::panic_str(msg)
}

/// Render raw key bytes back into the `ed25519:...` form users paste into the CLI.
fn key_to_string(raw: &[u8]) -> String {
    PublicKey::try_from(raw.to_vec())
        .map(|k| k.to_string())
        .unwrap_or_else(|_| String::from("<invalid key>"))
}

fn to_agent_policy(a: &AgentState, now_day: u64) -> AgentPolicy {
    let spent = if a.day == now_day { a.spent_today } else { 0 };
    AgentPolicy {
        agent_id: a.agent_id.clone(),
        public_key: key_to_string(&a.public_key),
        per_tx: a.per_tx.to_string(),
        daily: a.daily.to_string(),
        spent_today: spent.to_string(),
        remaining_today: a.daily.saturating_sub(spent).to_string(),
        active: a.active,
    }
}

#[near]
impl Vault {
    /// `per_tx` and `daily` are in yoctoNEAR. 1 NEAR = 10^24 yoctoNEAR.
    #[init]
    pub fn new(
        owner: AccountId,
        per_tx: String,
        daily: String,
        recipients: Vec<AccountId>,
    ) -> Self {
        if env::state_exists() {
            fail("already initialized");
        }
        let per_tx: u128 = parse_u128(&per_tx, "per_tx");
        let daily: u128 = parse_u128(&daily, "daily");
        if per_tx == 0 {
            fail("per_tx must be > 0");
        }
        if per_tx > daily {
            fail("per_tx must be <= daily");
        }
        Self {
            owner,
            per_tx,
            daily,
            day: 0,
            spent_today: 0,
            recipients,
            paused: false,
            log: Vec::new(),
            next_id: 1,
            agents: Vec::new(),
        }
    }

    // ------------------------------------------------------------------
    // owner-only controls
    // ------------------------------------------------------------------

    pub fn set_policy(&mut self, per_tx: String, daily: String, recipients: Vec<AccountId>) {
        self.assert_owner();
        let per_tx: u128 = parse_u128(&per_tx, "per_tx");
        let daily: u128 = parse_u128(&daily, "daily");
        if per_tx == 0 {
            fail("per_tx must be > 0");
        }
        if per_tx > daily {
            fail("per_tx must be <= daily");
        }
        self.per_tx = per_tx;
        self.daily = daily;
        self.recipients = recipients;
    }

    /// Add a recipient account to the vault allowlist if not already present.
    pub fn add_to_allowlist(&mut self, recipient: AccountId) {
        self.assert_owner();
        if !self.recipients.iter().any(|r| r == &recipient) {
            self.recipients.push(recipient);
        }
    }

    /// Kill switch. While paused every `spend` and `spend_as` is rejected on-chain.
    pub fn set_paused(&mut self, paused: bool) {
        self.assert_owner();
        self.paused = paused;
    }

    pub fn transfer_ownership(&mut self, owner: AccountId) {
        self.assert_owner();
        self.owner = owner;
    }

    /// Register a swarm agent. `public_key` is the `ed25519:...` string of the
    /// function call access key that will sign `spend_as` calls. That key must
    /// already exist on the vault account with `method_names = ["spend_as"]`.
    pub fn register_agent(
        &mut self,
        agent_id: AccountId,
        public_key: String,
        per_tx: String,
        daily: String,
    ) {
        self.assert_owner();
        let per_tx: u128 = parse_u128(&per_tx, "per_tx");
        let daily: u128 = parse_u128(&daily, "daily");
        if per_tx == 0 {
            fail("agent per_tx must be > 0");
        }
        if per_tx > daily {
            fail("agent per_tx must be <= daily");
        }
        // An agent cap above the vault cap could never be spent anyway
        // (spend_as enforces the vault caps too), so reject it here with
        // a clear error instead of silently capping the agent later.
        if per_tx > self.per_tx {
            fail("agent per_tx must be <= vault per_tx");
        }
        if daily > self.daily {
            fail("agent daily must be <= vault daily");
        }
        let pk: PublicKey = public_key
            .parse()
            .unwrap_or_else(|_| fail("public_key is not a valid ed25519:... key"));
        let raw = pk.into_bytes();
        if self.agents.iter().any(|a| a.public_key == raw) {
            fail("this public key is already registered to an agent");
        }
        self.agents.push(AgentState {
            agent_id,
            public_key: raw,
            per_tx,
            daily,
            day: 0,
            spent_today: 0,
            active: true,
        });
    }

    /// Change an agent's budget. The key stays the same.
    pub fn set_agent_policy(&mut self, public_key: String, per_tx: String, daily: String) {
        self.assert_owner();
        let raw = self.parse_key(&public_key);
        let per_tx: u128 = parse_u128(&per_tx, "per_tx");
        let daily: u128 = parse_u128(&daily, "daily");
        if per_tx == 0 {
            fail("agent per_tx must be > 0");
        }
        if per_tx > daily {
            fail("agent per_tx must be <= daily");
        }
        if per_tx > self.per_tx {
            fail("agent per_tx must be <= vault per_tx");
        }
        if daily > self.daily {
            fail("agent daily must be <= vault daily");
        }
        let mut found = false;
        for a in self.agents.iter_mut() {
            if a.public_key == raw {
                a.per_tx = per_tx;
                a.daily = daily;
                found = true;
                break;
            }
        }
        if !found {
            fail("agent is not registered");
        }
    }

    /// Revoke an agent's budget on-chain. The access key itself still lives on
    /// the account until the owner deletes it with `near delete-key`; this
    /// stops the agent from spending in the meantime. Reversible with
    /// `reactivate_agent`; revocation does not delete the registration.
    pub fn revoke_agent(&mut self, public_key: String) {
        self.assert_owner();
        let raw = self.parse_key(&public_key);
        let mut found = false;
        for a in self.agents.iter_mut() {
            if a.public_key == raw {
                a.active = false;
                found = true;
                break;
            }
        }
        if !found {
            fail("agent is not registered");
        }
    }

    /// Re-activate a revoked agent, restoring its budget (per-tx / daily /
    /// spent counter are unchanged by revocation). The access key itself is
    /// untouched: if it was deleted with `near delete-key`, re-add it first.
    pub fn reactivate_agent(&mut self, public_key: String) {
        self.assert_owner();
        let raw = self.parse_key(&public_key);
        let mut found = false;
        for a in self.agents.iter_mut() {
            if a.public_key == raw {
                a.active = true;
                found = true;
                break;
            }
        }
        if !found {
            fail("agent is not registered");
        }
    }

    /// Owner-only: zero one agent's daily counter so the demo can be re-run
    /// inside the same UTC day. On a live deployment this happens by itself at
    /// UTC midnight (`block_timestamp / 86_400s`); this call exists because the
    /// demo and the judges' re-runs would otherwise hit an already-spent budget.
    pub fn reset_agent_day(&mut self, public_key: String) {
        self.assert_owner();
        let raw = self.parse_key(&public_key);
        let day = env::block_timestamp() / NS_PER_DAY;
        let mut found = false;
        for a in self.agents.iter_mut() {
            if a.public_key == raw {
                if self.day == day && a.day == day {
                    self.spent_today = self.spent_today.saturating_sub(a.spent_today);
                }
                a.day = day;
                a.spent_today = 0;
                found = true;
                break;
            }
        }
        if !found {
            fail("agent is not registered");
        }
    }

    /// Owner-only: zero the vault-level daily counter.
    pub fn reset_vault_day(&mut self) {
        self.assert_owner();
        self.day = env::block_timestamp() / NS_PER_DAY;
        self.spent_today = 0;
    }

    // ------------------------------------------------------------------
    // agent surface — single agent (vault-level policy)
    // ------------------------------------------------------------------

    /// Spend from the vault using the vault's own policy. Callable by a
    /// function call key on the vault account whose method allowlist is
    /// `["spend"]`, or directly by the vault owner.
    pub fn spend(&mut self, amount: String, recipient: AccountId, memo: String) -> SpendEntry {
        let caller = env::predecessor_account_id();
        if caller != env::current_account_id() && caller != self.owner {
            fail("only the vault or owner can call spend");
        }
        if self.paused {
            fail("vault is paused by owner");
        }
        if memo.len() > MAX_MEMO_LEN {
            fail("memo exceeds maximum length (256 bytes)");
        }
        let amount: u128 = parse_u128(&amount, "amount");
        self.assert_vault_amount(amount);

        let day = env::block_timestamp() / NS_PER_DAY;
        if day != self.day {
            self.day = day;
            self.spent_today = 0;
        }
        if self.spent_today.saturating_add(amount) > self.daily {
            fail("daily cap exceeded");
        }
        self.assert_recipient(&recipient);
        let storage_buffer = self.estimate_entry_storage("vault", &recipient, &memo);
        if self.available_balance().saturating_sub(storage_buffer) < amount {
            fail("vault balance is lower than the requested amount");
        }

        self.spent_today += amount;
        self.record("vault", amount, recipient, memo)
    }

    // ------------------------------------------------------------------
    // agent surface — swarm mode (per-agent policy)
    // ------------------------------------------------------------------

    /// Spend as a registered swarm agent. The signer's public key selects the
    /// agent's own budget; agent A cannot touch agent B's cap.
    ///
    /// Both caps apply: the agent's own per-tx / daily cap *and* the vault
    /// per-tx / daily cap. The vault `spent_today` counter is incremented
    /// together with the agent's, so the sum of all agents can never exceed
    /// the vault daily cap. (Before this fix, `spend_as` never touched the
    /// vault counters, so the vault daily cap did not bind swarm spends.)
    pub fn spend_as(&mut self, amount: String, recipient: AccountId, memo: String) -> SpendEntry {
        if env::predecessor_account_id() != env::signer_account_id() {
            fail("cross-contract calls to spend_as are not allowed");
        }
        if self.paused {
            fail("vault is paused by owner");
        }
        if memo.len() > MAX_MEMO_LEN {
            fail("memo exceeds maximum length (256 bytes)");
        }
        let amount: u128 = parse_u128(&amount, "amount");
        if amount == 0 {
            fail("amount must be > 0");
        }
        let signer_key = env::signer_account_pk().into_bytes();
        let day = env::block_timestamp() / NS_PER_DAY;

        // Vault-level caps first: roll the vault day over, then enforce.
        // (Same rollover-then-check order as `spend`.)
        if day != self.day {
            self.day = day;
            self.spent_today = 0;
        }
        if amount > self.per_tx {
            fail("vault per-transaction cap exceeded");
        }
        if self.spent_today.saturating_add(amount) > self.daily {
            fail("vault daily cap exceeded");
        }

        let mut idx: Option<usize> = None;
        for (i, a) in self.agents.iter().enumerate() {
            if a.public_key == signer_key {
                idx = Some(i);
                break;
            }
        }
        let i = idx.unwrap_or_else(|| fail("signer key is not a registered agent"));
        if !self.agents[i].active {
            fail("agent is revoked");
        }
        if amount > self.agents[i].per_tx {
            fail("agent per-transaction cap exceeded");
        }
        if day != self.agents[i].day {
            self.agents[i].day = day;
            self.agents[i].spent_today = 0;
        }
        if self.agents[i].spent_today.saturating_add(amount) > self.agents[i].daily {
            fail("agent daily cap exceeded");
        }
        self.assert_recipient(&recipient);
        let label = self.agents[i].agent_id.to_string();
        let storage_buffer = self.estimate_entry_storage(&label, &recipient, &memo);
        if self.available_balance().saturating_sub(storage_buffer) < amount {
            fail("vault balance is lower than the requested amount");
        }

        self.spent_today += amount;
        self.agents[i].spent_today += amount;
        self.record(&label, amount, recipient, memo)
    }

    // ------------------------------------------------------------------
    // views
    // ------------------------------------------------------------------

    pub fn get_policy(&self) -> Policy {
        let day = env::block_timestamp() / NS_PER_DAY;
        let spent = if day == self.day { self.spent_today } else { 0 };
        Policy {
            owner: self.owner.clone(),
            per_tx: self.per_tx.to_string(),
            daily: self.daily.to_string(),
            spent_today: spent.to_string(),
            day: self.day,
            recipients: self.recipients.clone(),
            paused: self.paused,
            agent_count: self.agents.len() as u64,
        }
    }

    pub fn get_remaining_today(&self) -> String {
        let day = env::block_timestamp() / NS_PER_DAY;
        let spent = if day == self.day { self.spent_today } else { 0 };
        self.daily.saturating_sub(spent).to_string()
    }

    pub fn get_balance(&self) -> String {
        self.available_balance().to_string()
    }

    pub fn get_log(&self) -> Vec<SpendEntry> {
        self.log.clone()
    }

    pub fn get_log_len(&self) -> u64 {
        self.log.len() as u64
    }

    /// Every registered swarm agent with its live budget.
    pub fn get_agents(&self) -> Vec<AgentPolicy> {
        let day = env::block_timestamp() / NS_PER_DAY;
        self.agents.iter().map(|a| to_agent_policy(a, day)).collect()
    }

    /// One agent, looked up by its `ed25519:...` public key.
    pub fn get_agent_by_key(&self, public_key: String) -> Option<AgentPolicy> {
        let raw = public_key.parse::<PublicKey>().ok()?.into_bytes();
        let day = env::block_timestamp() / NS_PER_DAY;
        self.agents
            .iter()
            .find(|a| a.public_key == raw)
            .map(|a| to_agent_policy(a, day))
    }

    // ------------------------------------------------------------------
    // internals
    // ------------------------------------------------------------------

    fn parse_key(&self, public_key: &str) -> Vec<u8> {
        public_key
            .parse::<PublicKey>()
            .map(|k| k.into_bytes())
            .unwrap_or_else(|_| fail("public_key is not a valid ed25519:... key"))
    }

    fn assert_owner(&self) {
        if env::predecessor_account_id() != self.owner {
            fail("only the vault owner can call this");
        }
    }

    fn assert_vault_amount(&self, amount: u128) {
        if amount == 0 {
            fail("amount must be > 0");
        }
        if amount > self.per_tx {
            fail("per-transaction cap exceeded");
        }
    }

    fn assert_recipient(&self, recipient: &AccountId) {
        if !self.recipients.iter().any(|r| r == recipient) {
            fail("recipient is not on the allowlist");
        }
    }

    fn record(
        &mut self,
        agent_label: &str,
        amount: u128,
        recipient: AccountId,
        memo: String,
    ) -> SpendEntry {
        let entry = SpendEntry {
            id: self.next_id,
            caller: env::predecessor_account_id(),
            agent: agent_label.to_string(),
            amount: amount.to_string(),
            recipient: recipient.clone(),
            memo,
            timestamp: env::block_timestamp(),
        };
        self.next_id += 1;
        if self.log.len() >= MAX_LOG_ENTRIES {
            self.log.remove(0);
        }
        self.log.push(entry.clone());
        Promise::new(recipient)
            .transfer(NearToken::from_yoctonear(amount))
            .detach();
        entry
    }

    fn estimate_entry_storage(&self, agent_label: &str, recipient: &AccountId, memo: &str) -> u128 {
        if self.log.len() >= MAX_LOG_ENTRIES {
            return 0;
        }
        let approx_bytes = (128 + agent_label.len() + recipient.as_str().len() + memo.len()) as u128;
        approx_bytes.saturating_mul(env::storage_byte_cost().as_yoctonear())
    }

    /// Balance minus the NEAR that is locked for contract storage.
    fn available_balance(&self) -> u128 {
        let storage_cost = env::storage_usage() as u128 * env::storage_byte_cost().as_yoctonear();
        env::account_balance()
            .as_yoctonear()
            .saturating_sub(storage_cost)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const PK_ALPHA: &str = "ed25519:6E8sCci9badyRkXb3JoRpBj5p8C6Tw41ELDZoiihKEtp";
    const PK_BETA: &str = "ed25519:7PGseFbWxvYVgZ89K1uTJKYoKetWs7BJtbyXDzfbAcqX";

    fn sample_agent(pk_str: &str, day: u64, spent_today: u128) -> AgentState {
        let pk: PublicKey = pk_str.parse().unwrap();
        AgentState {
            agent_id: "agent-alpha".parse().unwrap(),
            public_key: pk.into_bytes(),
            per_tx: 200_000_000_000_000_000_000_000,
            daily: 1_000_000_000_000_000_000_000_000,
            day,
            spent_today,
            active: true,
        }
    }

    #[test]
    fn key_round_trips_to_ed25519_string() {
        let pk: PublicKey = PK_ALPHA.parse().unwrap();
        assert_eq!(key_to_string(&pk.into_bytes()), PK_ALPHA);
        let pk2: PublicKey = PK_BETA.parse().unwrap();
        assert_eq!(key_to_string(&pk2.into_bytes()), PK_BETA);
    }

    #[test]
    fn agent_policy_same_day_tracks_spent_and_remaining() {
        let a = sample_agent(PK_ALPHA, 20_766, 250_000_000_000_000_000_000_000);
        let p = to_agent_policy(&a, 20_766);
        assert_eq!(p.public_key, PK_ALPHA);
        assert_eq!(p.spent_today, "250000000000000000000000");
        assert_eq!(p.remaining_today, "750000000000000000000000");
        assert!(p.active);
    }

    #[test]
    fn agent_policy_rolls_over_on_new_utc_day() {
        let a = sample_agent(PK_ALPHA, 20_766, 900_000_000_000_000_000_000_000);
        let p = to_agent_policy(&a, 20_767);
        assert_eq!(p.spent_today, "0");
        assert_eq!(p.remaining_today, "1000000000000000000000000");
    }
}
