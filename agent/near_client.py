"""Minimal NEAR JSON-RPC client used by Allowkey agents and dashboard.

Wraps the ed25519 signing helpers from near-api-py for write calls, and
supports keyless read-only initialization (with stdlib urllib fallback)
for views and dashboard queries.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover — fallback to stdlib urllib for read-only mode
    requests = None  # type: ignore[assignment]

DEFAULT_RPC = "https://rpc.testnet.fastnear.com"
TGAS = 1_000_000_000_000  # 1 TGas in gas units


class NearRpcError(Exception):
    """Raised when the JSON-RPC node returns an error object."""


def _post_json(url: str, body: dict, timeout: int) -> dict:
    if requests is not None:
        resp = requests.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


class NearClient:
    def __init__(
        self,
        account_id: str,
        secret_key: str | None = None,
        rpc_url: str = DEFAULT_RPC,
    ):
        self.account_id = account_id
        self.rpc_url = rpc_url
        if secret_key:
            from near_api.signer import KeyPair, Signer

            self.signer: Any = Signer(account_id, KeyPair(secret_key))
            # encoded ed25519:<base58> — this is the key the protocol scopes
            self.public_key: str | None = "ed25519:" + self.signer.key_pair.encoded_public_key()
        else:
            self.signer = None
            self.public_key = None

    # ------------------------------------------------------------------ rpc
    def _rpc(self, method: str, params: Any, timeout: int = 45) -> Any:
        body = {"jsonrpc": "2.0", "id": "allowance", "method": method, "params": params}
        payload = _post_json(self.rpc_url, body, timeout=timeout)
        if payload.get("error"):
            raise NearRpcError(json.dumps(payload["error"]))
        return payload["result"]

    # ---------------------------------------------------------------- views
    def view(self, contract: str, method: str, args: dict | None = None) -> Any:
        args_b64 = base64.b64encode(json.dumps(args or {}).encode()).decode()
        result = self._rpc(
            "query",
            {
                "request_type": "call_function",
                "account_id": contract,
                "method_name": method,
                "args_base64": args_b64,
                "finality": "final",
            },
        )
        raw = result.get("result") or []
        if not raw:
            return None
        return json.loads(bytes(raw).decode())

    def access_key(self, account_id: str | None = None) -> dict:
        """The signer's own access key record (nonce, permission).

        The key lives on the *signer* account, so this defaults to
        ``self.account_id``. (It used to be queried on the call's receiver,
        which only worked because every Allowkey call targets the vault
        account itself.)
        """
        if not self.public_key:
            raise RuntimeError("NearClient initialized in read-only mode (no secret_key)")
        return self._rpc(
            "query",
            {
                "request_type": "view_access_key",
                "account_id": account_id or self.account_id,
                "public_key": self.public_key,
                "finality": "final",
            },
        )

    def access_key_list(self, account_id: str) -> list[dict]:
        """Every access key on an account. Used by the dashboard to show the
        protocol-level permission of each swarm agent key."""
        res = self._rpc(
            "query",
            {
                "request_type": "view_access_key_list",
                "account_id": account_id,
                "finality": "final",
            },
        )
        return res.get("keys") or []

    def account_balance(self, account_id: str) -> int:
        res = self._rpc(
            "query",
            {"request_type": "view_account", "account_id": account_id, "finality": "final"},
        )
        return int(res["amount"])

    # -------------------------------------------------------------- writes
    def call(
        self,
        contract: str,
        method: str,
        args: dict | None = None,
        gas: int = 100 * TGAS,
        deposit: int = 0,
        wait: bool = True,
    ) -> dict:
        """Sign and broadcast a function call with the scoped key.

        Returns a dict: {ok, tx, status, logs, error}.
        A protocol-level rejection (key not allowed to call this method,
        allowance exhausted) surfaces as ok=False with the node's message.
        """
        args = args or {}
        if not self.signer:
            raise RuntimeError("NearClient initialized in read-only mode (no secret_key)")
        import base58
        from near_api.transactions import (
            create_function_call_action,
            sign_and_serialize_transaction,
        )

        nonce = self.access_key()["nonce"] + 1
        block_hash = base58.b58decode(self._rpc("block", {"finality": "final"})["header"]["hash"])

        action = create_function_call_action(
            method, json.dumps(args).encode(), gas, deposit
        )
        signed = sign_and_serialize_transaction(contract, nonce, [action], block_hash, self.signer)
        tx_b64 = base64.b64encode(signed).decode()

        try:
            outcome = self._rpc(
                "broadcast_tx_commit" if wait else "broadcast_tx_async",
                [tx_b64],
                timeout=90,
            )
        except NearRpcError as exc:
            return {"ok": False, "tx": None, "status": "rpc_error", "logs": [], "error": str(exc)}

        if not wait:
            return {"ok": True, "tx": outcome, "status": "submitted", "logs": [], "error": None}

        tx_hash = outcome.get("transaction_outcome", {}).get("id")
        status = outcome.get("status")
        logs: list[str] = []
        for outcome_item in [outcome.get("transaction_outcome", {})] + outcome.get("receipts_outcome", []):
            logs.extend(outcome_item.get("outcome", {}).get("logs", []) or [])

        ok = isinstance(status, dict) and "SuccessValue" in status
        error = None
        if not ok:
            error = json.dumps(status) if status else "unknown failure"

        return {
            "ok": ok,
            "tx": tx_hash,
            "status": status,
            "logs": logs,
            "error": error,
            "explorer": f"https://testnet.nearblocks.io/txns/{tx_hash}" if tx_hash else None,
        }


def wait_for_final(tx_hash: str, account_id: str, rpc_url: str = DEFAULT_RPC, timeout: int = 60) -> dict:
    """Poll tx status until final. Used when broadcasting async."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            payload = _post_json(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": "allowance",
                    "method": "tx",
                    "params": {"tx_hash": tx_hash, "sender_account_id": account_id, "wait_until": "FINAL"},
                },
                timeout=40,
            )
            if payload.get("result"):
                return payload["result"]
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f"tx {tx_hash} not final after {timeout}s")
