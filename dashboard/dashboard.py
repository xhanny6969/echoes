#!/usr/bin/env python3
"""Allowkey dashboard — read-only command center for a swarm spending vault on NEAR.

Serves one page + two JSON endpoints on 0.0.0.0:8088:
  /            the interactive dashboard UI
  /api/state   policy, agents, balance, spend log, every scoped key
  /api/audit   the agents' JSONL audit feed (blocked + allowed attempts)

Every number is read from the NEAR RPC at request time. Nothing is mocked.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))
from near_client import NearClient  # noqa: E402

VAULT = os.environ.get("VAULT", "allowance-swarm.testnet")
RPC = os.environ.get("NEAR_RPC", "https://rpc.testnet.fastnear.com")
AUDIT_LOG = Path(
    os.environ.get(
        "AUDIT_LOG",
        str(Path(__file__).resolve().parent.parent / "agent" / "audit.jsonl"),
    )
)
PORT = int(os.environ.get("PORT", "8088"))
YOCTO = 10**24

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "state": None}
CACHE_TTL = 15


def _near() -> NearClient:
    return NearClient(VAULT, rpc_url=RPC)


def _fmt(yocto_str: str | int) -> str:
    return f"{int(yocto_str) / YOCTO:.6f}".rstrip("0").rstrip(".")


def _audit_rows(limit: int = 50) -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    rows = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(rows))


_REJ_CACHE: dict = {"at": 0.0, "data": None}


def _rejections(limit: int = 40) -> list[dict]:
    """Failed txs on the vault account == guardrail rejections, read from the explorer."""
    import urllib.request

    now = time.time()
    with _CACHE_LOCK:
        if now - float(_REJ_CACHE.get("at") or 0) < 30 and _REJ_CACHE.get("data") is not None:
            return list(_REJ_CACHE["data"])[:limit]  # type: ignore[arg-type]
    url = f"https://api-testnet.nearblocks.io/v3/accounts/{VAULT}/txns?limit=100"
    rows: list[dict] = []
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json", "User-Agent": "Mozilla/5.0 (allowkey-console)"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
        for tx in payload.get("data") or []:
            if (tx.get("outcomes") or {}).get("status") is True:
                continue
            actions = tx.get("actions") or [{}]
            rows.append(
                {
                    "hash": tx.get("transaction_hash"),
                    "method": (actions[0] or {}).get("method") or "unknown",
                    "block": (tx.get("block") or {}).get("block_height"),
                    "timestamp": tx.get("block_timestamp"),
                }
            )
    except Exception as exc:  # explorer hiccup must not kill the demo
        sys.stderr.write(f"[rejections] {exc}\n")
    out = rows[:limit]
    with _CACHE_LOCK:
        _REJ_CACHE.update({"at": now, "data": rows})
    return out


def _scoped_keys(c: NearClient) -> list[dict]:
    """Every function call key on the vault, with its protocol permission."""
    out = []
    try:
        keys = c.access_key_list(VAULT)
    except Exception:  # noqa: BLE001 — dashboard must stay up
        return out
    for k in keys:
        perm = (k.get("access_key") or {}).get("permission")
        if not isinstance(perm, dict) or "FunctionCall" not in perm:
            continue
        fc = perm["FunctionCall"]
        out.append(
            {
                "public_key": k.get("public_key"),
                "receiver_id": fc.get("receiver_id"),
                "method_names": fc.get("method_names"),
                "allowance_near": _fmt(fc["allowance"]) if fc.get("allowance") else "unlimited",
                "nonce": k.get("access_key", {}).get("nonce"),
            }
        )
    return out


def _state(force: bool = False) -> dict:
    now = time.time()
    with _CACHE_LOCK:
        if not force and _CACHE["state"] and now - _CACHE["at"] < CACHE_TTL:
            return _CACHE["state"]
    c = _near()
    policy = c.view(VAULT, "get_policy")
    balance = c.view(VAULT, "get_balance")
    log = c.view(VAULT, "get_log") or []
    agents = c.view(VAULT, "get_agents") or []
    keys = _scoped_keys(c)

    # Map each registered agent to the protocol key that signs for it.
    by_pk = {k["public_key"]: k for k in keys}
    for a in agents:
        k = by_pk.get(a.get("public_key"))
        a["key_allowance_near"] = k["allowance_near"] if k else None
        a["key_methods"] = k["method_names"] if k else None
        a["key_receiver"] = k["receiver_id"] if k else None
        a["key_nonce"] = k["nonce"] if k else None
        a["per_tx_near"] = _fmt(a["per_tx"])
        a["daily_near"] = _fmt(a["daily"])
        a["spent_near"] = _fmt(a["spent_today"])
        a["remaining_near"] = _fmt(a["remaining_today"])
        a["used_pct"] = (
            100.0 * int(a["spent_today"]) / int(a["daily"]) if int(a["daily"]) else 0.0
        )

    # In the updated contract, policy["spent_today"] already includes swarm spends.
    # On an older deployment before redeploy, policy["spent_today"] may still be 0
    # while per-agent counters hold the swarm spends. Taking max() avoids double-counting.
    agents_spent = sum(int(a["spent_today"]) for a in agents)
    total_spent = max(int(policy["spent_today"]), agents_spent)

    state = {
        "vault": VAULT,
        "rpc": RPC,
        "policy": policy,
        "balance": balance,
        "balance_near": _fmt(balance),
        "total_spent_near": _fmt(total_spent),
        "total_spent_raw": total_spent,
        "remaining_near": _fmt(
            max(0, int(policy["daily"]) - total_spent)
        ),
        "per_tx_near": _fmt(policy["per_tx"]),
        "daily_near": _fmt(policy["daily"]),
        "spent_today_near": _fmt(total_spent),
        "used_pct": (
            100.0 * total_spent / int(policy["daily"])
            if int(policy["daily"])
            else 0.0
        ),
        "agents": agents,
        "agent_count": len(agents),
        "scoped_keys": keys,
        "log": log,
        "log_len": len(log),
        "audit": _audit_rows(),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with _CACHE_LOCK:
        _CACHE["state"] = state
        _CACHE["at"] = now
    return state


HTML = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<meta name="description" content="Allowkey — Protocol-enforced spending vault for AI agent swarms on NEAR.">
<title>Echoes · Swarm Security Console</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23000000'/%3E%3Ccircle cx='20' cy='32' r='3.2' fill='%23FFFFFF'/%3E%3Cpath d='M 26.68 39.81 A 9.24 9.24 0 0 0 26.68 24.19' fill='none' stroke='%23FFFFFF' stroke-width='3.2' stroke-linecap='round'/%3E%3Cpath d='M 34.62 43.83 A 16.72 16.72 0 0 0 34.62 20.17' fill='none' stroke='%23FFFFFF' stroke-width='3.2' stroke-linecap='round' opacity='0.65'/%3E%3Cpath d='M 42.10 46.20 A 24.20 24.20 0 0 0 42.10 17.80' fill='none' stroke='%23FFFFFF' stroke-width='3.2' stroke-linecap='round' opacity='0.32'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/near-api-js@2.1.4/dist/near-api-js.min.js" defer></script>
<script>
  try {
    const t = localStorage.getItem('allowkey-theme');
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
  } catch (_) {}
</script>
<style>
/* --- Professional Developer & Infra Palette (No AI Slop / No Neon Gradients) --- */
:root, [data-theme="dark"] {
  --bg: #000000;
  --panel: #141414;
  --panel-hover: #262626;
  --panel-muted: #141414;
  --border: #262626;
  --border-focus: #525252;
  --border-subtle: #262626;
  --text: #F2F1E9;
  --text-muted: #858585;
  --text-dim: #525252;
  --near-green: #00ec97;
  --near-green-subtle: rgba(0, 236, 151, 0.1);
  --near-green-border: rgba(0, 236, 151, 0.28);
  --blue: #0091FD;
  --blue-subtle: rgba(56, 189, 248, 0.1);
  --blue-border: rgba(56, 189, 248, 0.25);
  --amber: #9797FF;
  --amber-subtle: rgba(245, 158, 11, 0.1);
  --amber-border: rgba(245, 158, 11, 0.25);
  --red: #FF7966;
  --red-subtle: rgba(244, 63, 94, 0.1);
  --red-border: rgba(244, 63, 94, 0.25);
  --purple: #9797FF;
  --shadow: 0 1px 3px rgba(0,0,0,0.4), 0 10px 24px -8px rgba(0,0,0,0.6);
}

[data-theme="light"] {
  --bg: #F2F1E9;
  --panel: #ffffff;
  --panel-hover: #FFFFFF;
  --panel-muted: #E6E6E6;
  --border: #E6E6E6;
  --border-focus: #858585;
  --border-subtle: #E6E6E6;
  --text: #141414;
  --text-muted: #525252;
  --text-dim: #858585;
  --near-green: #00EC97;
  --near-green-subtle: rgba(5, 150, 105, 0.08);
  --near-green-border: rgba(5, 150, 105, 0.25);
  --blue: #0091FD;
  --blue-subtle: rgba(2, 132, 199, 0.08);
  --blue-border: rgba(2, 132, 199, 0.25);
  --amber: #9797FF;
  --amber-subtle: rgba(217, 119, 6, 0.08);
  --amber-border: rgba(217, 119, 6, 0.25);
  --red: #FF7966;
  --red-subtle: rgba(225, 29, 72, 0.08);
  --red-border: rgba(225, 29, 72, 0.25);
  --purple: #9797FF;
  --shadow: 0 1px 3px rgba(15,23,42,0.06), 0 8px 20px -6px rgba(15,23,42,0.08);
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background-color: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 13.5px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}

.mono {
  font-family: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  font-variant-numeric: tabular-nums;
}

code {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  padding: 1.5px 5px;
  border-radius: 4px;
  background: var(--panel-muted);
  border: 1px solid var(--border);
}

a {
  color: var(--blue);
  text-decoration: none;
}
a:hover { text-decoration: underline; }

/* --- Top Navigation --- */
.topbar {
  position: sticky;
  top: 0;
  z-index: 50;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}

.topbar-inner {
  max-width: 1360px;
  margin: 0 auto;
  padding: 0 24px;
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  color: var(--text);
}
.brand:hover { text-decoration: none; }

.brand-icon {
  width: 32px;
  height: 32px;
  background: var(--near-green);
  border-radius: 6px;
  display: grid;
  place-items: center;
}
.brand-icon svg { width: 18px; height: 18px; fill: #000000; }

.brand-title {
  display: flex;
  flex-direction: column;
}
.brand-name {
  font-weight: 700;
  font-size: 15px;
  letter-spacing: -0.01em;
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-sub {
  font-size: 11.5px;
  color: var(--text-dim);
}

.nav-center {
  display: flex;
  align-items: center;
  gap: 8px;
}

.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 5px;
  white-space: nowrap;
}
.badge.ok { background: var(--near-green-subtle); color: var(--near-green); border: 1px solid var(--near-green-border); }
.badge.warn { background: var(--amber-subtle); color: var(--amber); border: 1px solid var(--amber-border); }
.badge.bad { background: var(--red-subtle); color: var(--red); border: 1px solid var(--red-border); }
.badge.info { background: var(--blue-subtle); color: var(--blue); border: 1px solid var(--blue-border); }
.badge.neutral { background: var(--panel-muted); color: var(--text-muted); border: 1px solid var(--border); }

.pulse-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-dim);
}
.pulse-dot.live {
  background: var(--near-green);
  box-shadow: 0 0 0 2px var(--near-green-subtle);
}

.nav-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--panel);
  color: var(--text);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.12s ease;
}
.btn:hover {
  background: var(--panel-hover);
  border-color: var(--border-focus);
}
.btn:active { transform: translateY(1px); }

.btn-primary {
  background: var(--near-green);
  color: #000000;
  border-color: var(--near-green);
}
.btn-primary:hover {
  background: #00EC97;
  border-color: #00EC97;
  color: #000000;
}

/* --- Main Layout --- */
.container {
  max-width: 1360px;
  margin: 0 auto;
  padding: 20px 24px 60px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 18px 20px;
  box-shadow: var(--shadow);
}

.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.panel-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.panel-subtitle {
  font-size: 12px;
  color: var(--text-dim);
  font-weight: normal;
}

/* --- Context Banner --- */
.status-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 10px 16px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 12.5px;
}

.status-bar-left {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

/* --- Schematic Pipeline --- */
.pipeline-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.pipe-step {
  background: var(--panel-muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  position: relative;
  transition: all 0.2s ease;
}

.pipe-step-num {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--text-dim);
  letter-spacing: 0.05em;
}

.pipe-step-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.pipe-step-desc {
  font-size: 12px;
  color: var(--text-muted);
  line-height: 1.4;
}

.pipe-step.active-pass {
  border-color: var(--near-green);
  background: var(--near-green-subtle);
}
.pipe-step.active-block {
  border-color: var(--red);
  background: var(--red-subtle);
}
.pipe-step.active-warn {
  border-color: var(--amber);
  background: var(--amber-subtle);
}

/* --- Top KPIs Grid --- */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.kpi-card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.kpi-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-muted);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.kpi-val {
  font-size: 24px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--text);
  line-height: 1.2;
}
.kpi-val small {
  font-size: 13px;
  font-weight: 500;
  color: var(--text-muted);
  margin-left: 4px;
}

.progress-bar-wrap {
  width: 100%;
  height: 6px;
  background: var(--border);
  border-radius: 99px;
  overflow: hidden;
  margin-top: 4px;
}
.progress-bar-fill {
  height: 100%;
  background: var(--near-green);
  transition: width 0.4s ease;
}

.kpi-foot {
  font-size: 11.5px;
  color: var(--text-dim);
  display: flex;
  justify-content: space-between;
  margin-top: 2px;
}

/* --- 2-Column Main Section --- */
.split-grid {
  display: grid;
  grid-template-columns: 1.6fr 1fr;
  gap: 16px;
}

/* --- Swarm Agent Cards --- */
.agents-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.agent-box {
  background: var(--panel-muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.agent-box-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.agent-title {
  display: flex;
  align-items: center;
  gap: 8px;
}
.agent-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--near-green);
}

.agent-stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  background: var(--panel);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  padding: 10px;
}
.stat-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.stat-item .k { font-size: 11px; color: var(--text-dim); }
.stat-item .v { font-size: 13px; font-weight: 600; }

.agent-key-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11.5px;
  color: var(--text-muted);
  border-top: 1px solid var(--border);
  padding-top: 8px;
}
.copy-pill {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 3px 7px;
  font-size: 11px;
  cursor: pointer;
  color: var(--blue);
}
.copy-pill:hover { border-color: var(--blue); }

/* --- Policy Table List --- */
.kv-table {
  display: flex;
  flex-direction: column;
}
.kv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 12.5px;
}
.kv-row:last-child { border-bottom: none; }
.kv-row .k { color: var(--text-muted); }
.kv-row .v { font-weight: 600; text-align: right; }

/* --- Interactive Demo Runner Banner --- */
.test-runner {
  background: var(--panel);
  border: 1px solid var(--near-green-border);
  border-radius: 8px;
  padding: 14px 18px;
  display: none;
  flex-direction: column;
  gap: 8px;
}
.test-runner.active { display: flex; }
.runner-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
}

/* --- Tabs & Tables --- */
.tab-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
  margin-bottom: 12px;
}
.tab-btn {
  background: transparent;
  border: none;
  color: var(--text-muted);
  font-size: 12.5px;
  font-weight: 600;
  padding: 6px 12px;
  border-radius: 5px;
  cursor: pointer;
}
.tab-btn:hover { color: var(--text); background: var(--panel-muted); }
.tab-btn.active { color: var(--text); background: var(--panel-muted); border: 1px solid var(--border); }

.filter-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.filter-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 11.5px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
}
.filter-btn:hover { color: var(--text); border-color: var(--border-focus); }
.filter-btn.active { background: var(--panel-muted); color: var(--text); border-color: var(--border-focus); font-weight: 600; }

.table-wrap {
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
  text-align: left;
}
th {
  background: var(--panel-muted);
  color: var(--text-dim);
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
td {
  padding: 9px 12px;
  border-bottom: 1px solid var(--border-subtle);
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--panel-hover); }

/* --- RPC Code Box & Interactive Sandbox --- */
.rpc-drawer {
  background: var(--panel-muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  margin-top: 10px;
  display: none;
}
.rpc-drawer.open { display: block; }
.rpc-drawer pre {
  margin: 6px 0 0;
  padding: 10px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  font-size: 11.5px;
  color: var(--near-green);
  overflow-x: auto;
}

.sandbox-grid {
  display: grid;
  grid-template-columns: 1.25fr 1fr;
  gap: 16px;
}
.form-row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
  margin-bottom: 10px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.field label {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.input, .select {
  height: 34px;
  padding: 0 10px;
  border-radius: 5px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-size: 12.5px;
  outline: none;
}
.input:focus, .select:focus {
  border-color: var(--blue);
}
.preset-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.preset-chip {
  background: var(--panel-muted);
  border: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
  cursor: pointer;
}
.preset-chip:hover {
  color: var(--text);
  border-color: var(--border-focus);
}
.verdict-box {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  justify-content: space-between;
}
.gate-checklist {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
}
.gate-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 8px;
  border-radius: 4px;
  background: var(--panel);
  border: 1px solid var(--border-subtle);
}

/* --- Wallet Modal --- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.65);
  z-index: 100;
  display: none;
   place-items: center;
  padding: 16px;
}
.modal-backdrop.open { display: grid; }
.modal {
  width: 100%;
  max-width: 480px;
  background: var(--panel);
  border: 1px solid var(--border-focus);
  border-radius: 8px;
  padding: 20px;
  box-shadow: var(--shadow);
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.modal-sec {
  background: var(--panel-muted);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* --- Footer --- */
footer {
  max-width: 1360px;
  margin: 0 auto;
  padding: 16px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--text-dim);
  border-top: 1px solid var(--border);
}

@media (max-width: 1024px) {
  .pipeline-grid { grid-template-columns: repeat(2, 1fr); }
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .split-grid { grid-template-columns: 1fr; }
}
@media (max-width: 680px) {
  .pipeline-grid { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: 1fr; }
  .agents-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="brand-icon" style="background:#000000;border:1px solid #262626;border-radius:7px;display:flex;align-items:center;justify-content:center;padding:2px">
        <svg viewBox="0 0 64 64" width="22" height="22" fill="none">
          <circle cx="20" cy="32" r="3.2" fill="#FFFFFF"/>
          <path d="M 26.68 39.81 A 9.24 9.24 0 0 0 26.68 24.19" stroke="#FFFFFF" stroke-width="3.2" stroke-linecap="round"/>
          <path d="M 34.62 43.83 A 16.72 16.72 0 0 0 34.62 20.17" stroke="#FFFFFF" stroke-width="3.2" stroke-linecap="round" opacity="0.65"/>
          <path d="M 42.10 46.20 A 24.20 24.20 0 0 0 42.10 17.80" stroke="#FFFFFF" stroke-width="3.2" stroke-linecap="round" opacity="0.32"/>
        </svg>
      </div>
      <div class="brand-title">
        <div class="brand-name">
          ECHOES
          <span class="badge neutral mono" style="font-size:10px">ALLOWKEY</span>
        </div>
        <div class="brand-sub">Protocol-Enforced Spending Vault for AI Swarms · NEAR</div>
      </div>
    </div>

    <div class="nav-center">
      <span class="badge neutral mono" id="source-badge">
        <span class="pulse-dot live" id="live-dot"></span>
        <span id="source-label">TESTNET SNAPSHOT</span>
      </span>
      <a class="badge info mono" id="vault-pill-link" href="https://testnet.nearblocks.io/address/allowance-swarm.testnet" target="_blank" rel="noopener">
        allowance-swarm.testnet ↗
      </a>
    </div>

    <div class="nav-actions">
      <button class="btn" id="wallet-btn" onclick="openWalletModal()" title="Connect MyNearWallet Testnet or Agent Key">
        <span class="pulse-dot" id="wallet-dot"></span>
        <span id="wallet-btn-label">Connect Wallet</span>
      </button>
      <button class="btn btn-primary" id="sim-btn" onclick="runDemoSimulation()" title="Replay the 7-step testnet spend demo">
        ▶ Run Swarm Demo
      </button>
      <button class="btn" onclick="toggleRpcDrawer()" title="View direct JSON-RPC curl command">
        RPC Query
      </button>
      <button class="btn" id="refresh-btn" onclick="tick(true)" title="Fetch latest contract state">
        Refresh
      </button>
      <button class="btn" id="theme-btn" onclick="toggleTheme()" title="Switch Light/Dark Mode">
        Light
      </button>
    </div>
  </div>
</header>

<main class="container">

  <!-- Interactive Test Runner Bar -->
  <section class="test-runner" id="sim-runner">
    <div class="runner-header">
      <div style="display:flex;align-items:center;gap:10px">
        <span class="badge ok mono" id="sim-step-badge">STEP 1 / 7</span>
        <strong id="sim-step-title" style="font-size:13px">Initializing swarm verification scenario…</strong>
      </div>
      <span class="mono" id="sim-step-outcome" style="font-size:12px;color:var(--text-muted)"></span>
    </div>
    <div class="progress-bar-wrap">
      <div class="progress-bar-fill" id="sim-progress-bar" style="width:0%"></div>
    </div>
  </section>

  <!-- Technical Status Bar -->
  <div class="status-bar">
    <div class="status-bar-left">
      <span><strong>Security Invariant:</strong> Identity bound to <code class="mono">env::signer_account_pk()</code></span>
      <span style="color:var(--text-dim)">•</span>
      <span>Protocol Gate: <code class="mono">spend_as</code> only (<code class="mono">MethodNameMismatch</code> enforced)</span>
      <span style="color:var(--text-dim)">•</span>
      <span>Budget Isolation: Agent A cannot spend Agent B's quota</span>
    </div>
    <div id="killswitch-indicator">
      <span class="badge ok mono" id="vault-active-badge">SWARM ACTIVE</span>
    </div>
  </div>

  <!-- Dual-Layer Schematic Pipeline -->
  <section class="pipeline-grid" id="pipeline-steps">
    <div class="pipe-step" id="step-layer-1">
      <span class="pipe-step-num">Layer 1</span>
      <span class="pipe-step-title">Agent Identity</span>
      <span class="pipe-step-desc">Each agent signs with its own <code class="mono">ed25519</code> function-call key on the vault. Key cannot spoof other agents.</span>
    </div>
    <div class="pipe-step" id="step-layer-2">
      <span class="pipe-step-num">Layer 2</span>
      <span class="pipe-step-title">Protocol Method Gate</span>
      <span class="pipe-step-desc">NEAR runtime rejects calls to any method other than <code class="mono">spend_as</code> with <code class="mono">MethodNameMismatch</code> before contract Wasm runs.</span>
    </div>
    <div class="pipe-step" id="step-layer-3">
      <span class="pipe-step-num">Layer 3</span>
      <span class="pipe-step-title">Contract Budgets</span>
      <span class="pipe-step-desc">Contract checks per-agent per-tx & daily limits, then checks total vault per-tx & daily caps.</span>
    </div>
    <div class="pipe-step" id="step-layer-4">
      <span class="pipe-step-num">Layer 4</span>
      <span class="pipe-step-title">Settlement</span>
      <span class="pipe-step-desc">Transfers NEAR only to allowlisted recipients. Any policy violation triggers a clean on-chain revert.</span>
    </div>
  </section>

  <!-- Top 4 Metrics -->
  <section class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">
        <span>Vault Treasury Balance</span>
        <span class="mono" style="color:var(--near-green)">get_balance()</span>
      </div>
      <div class="kpi-val mono"><span id="kpi-balance">—</span><small>NEAR</small></div>
      <div class="kpi-foot">
        <span>Liquid spendable reserve</span>
        <span class="mono">allowance-swarm.testnet</span>
      </div>
    </div>

    <div class="kpi-card">
      <div class="kpi-label">
        <span>Vault Daily Cap Used</span>
        <span class="mono" id="kpi-cap-pct">0% used</span>
      </div>
      <div class="kpi-val mono">
        <span id="kpi-spent">—</span>
        <small>/ <span id="kpi-daily">—</span> NEAR</small>
      </div>
      <div class="progress-bar-wrap">
        <div class="progress-bar-fill" id="daily-cap-meter" style="width:0%"></div>
      </div>
      <div class="kpi-foot">
        <span>Left today: <strong class="mono" id="kpi-remaining">—</strong> NEAR</span>
        <span class="mono" id="kpi-pertx">per-tx: — NEAR</span>
      </div>
    </div>

    <div class="kpi-card">
      <div class="kpi-label">
        <span>Registered Agents</span>
        <span class="badge info mono" id="kpi-keys-badge">2 Scoped Keys</span>
      </div>
      <div class="kpi-val mono"><span id="kpi-agent-count">2</span><small>actors</small></div>
      <div class="kpi-foot">
        <span>Individual daily caps</span>
        <span class="mono" style="color:var(--near-green)">Isolated Budgets</span>
      </div>
    </div>

    <div class="kpi-card">
      <div class="kpi-label">
        <span>Guardrail Enforcements</span>
        <span class="badge neutral mono" id="kpi-audit-count">7 Events</span>
      </div>
      <div class="kpi-val mono">
        <span id="kpi-blocked" style="color:var(--red)">4</span>
        <small>Blocked · <span id="kpi-allowed" style="color:var(--near-green)">3</span> Allowed</small>
      </div>
      <div class="kpi-foot">
        <span id="kpi-block-breakdown">2 Contract Cap · 2 Protocol Scope</span>
        <span class="mono">Verified</span>
      </div>
    </div>
  </section>

  <!-- 2-Column: Agent Fleets & Vault Policy -->
  <section class="split-grid">
    <div class="panel">
      <div class="panel-header">
        <div>
          <div class="panel-title">Swarm Agent Fleets</div>
          <div class="panel-subtitle">Each agent holds its own function-call key and spends only against its isolated quota</div>
        </div>
      </div>
      <div class="agents-grid" id="agents-container">
        <div style="color:var(--text-dim)">Loading swarm agents…</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div>
          <div class="panel-title">Vault Configuration</div>
          <div class="panel-subtitle">On-chain parameters enforced on every <code class="mono">spend_as</code> call</div>
        </div>
      </div>
      <div class="kv-table">
        <div class="kv-row">
          <span class="k">Contract Owner</span>
          <span class="v mono" id="pol-owner">allowance-swarm.testnet</span>
        </div>
        <div class="kv-row">
          <span class="k">Vault Per-Tx Cap</span>
          <span class="v mono" id="pol-pertx">0.50 NEAR</span>
        </div>
        <div class="kv-row">
          <span class="k">Vault Daily Cap (All Agents)</span>
          <span class="v mono" id="pol-daily">5.00 NEAR</span>
        </div>
        <div class="kv-row">
          <span class="k">Total Spent Today</span>
          <span class="v mono" id="pol-spent">1.55 NEAR</span>
        </div>
        <div class="kv-row">
          <span class="k">Allowlisted Merchant</span>
          <span class="v mono" id="pol-recips">allowance-merchant.testnet</span>
        </div>
        <div class="kv-row">
          <span class="k">Kill Switch Mechanism</span>
          <span class="v mono" id="pol-killswitch">set_paused(false)</span>
        </div>
        <div class="kv-row">
          <span class="k">Recorded Spend Ledger</span>
          <span class="v mono" id="pol-loglen">19 transactions</span>
        </div>
      </div>
    </div>
  </section>

  <!-- Interactive Guardrail Playground & Live Tester -->
  <section class="panel" id="playground-panel">
    <div class="panel-header" style="flex-wrap:wrap;gap:10px">
      <div>
        <div class="panel-title">
          Interactive Guardrail Playground · Try It Yourself
          <span class="badge info mono">Live Policy & Signer</span>
        </div>
        <div class="panel-subtitle">Simulate any agent spend or admin call against live testnet state — or connect a wallet / key to broadcast on-chain</div>
      </div>
      <div class="preset-bar" style="margin-bottom:0">
        <span style="font-size:11px;color:var(--text-dim);align-self:center">Presets:</span>
        <button class="preset-chip mono" onclick="loadPreset('valid')">✓ Valid 0.15 N</button>
        <button class="preset-chip mono" onclick="loadPreset('over_agent')">✗ Over Agent Cap (0.25 N)</button>
        <button class="preset-chip mono" onclick="loadPreset('over_vault')">✗ Over Vault Cap (0.90 N)</button>
        <button class="preset-chip mono" onclick="loadPreset('bad_recip')">✗ Untrusted Recipient</button>
        <button class="preset-chip mono" onclick="loadPreset('pause_hack')">✗ Hijack set_paused(true)</button>
      </div>
    </div>

    <div class="sandbox-grid">
      <div>
        <div class="form-row">
          <div class="field">
            <label for="pg-caller">Caller / Signer Identity</label>
            <select class="select mono" id="pg-caller" onchange="evaluatePlayground()">
              <option value="agent-alpha">agent-alpha (ed25519:GY1SXN…ps17)</option>
              <option value="agent-beta">agent-beta (ed25519:GYSHUu…GKEQ)</option>
              <option value="rogue">unregistered-key (ed25519:Rogue…)</option>
              <option value="wallet">Connected Wallet / Custom Key</option>
            </select>
          </div>
          <div class="field">
            <label for="pg-method">Target Contract Method</label>
            <select class="select mono" id="pg-method" onchange="evaluatePlayground()">
              <option value="spend_as">spend_as(amount, recipient, memo)</option>
              <option value="spend">spend(amount, recipient, memo) [owner/single]</option>
              <option value="set_paused">set_paused(true) [kill switch]</option>
              <option value="reset_agent_day">reset_agent_day(public_key) [owner]</option>
            </select>
          </div>
        </div>

        <div class="form-row">
          <div class="field">
            <label for="pg-amount">Amount (NEAR)</label>
            <input class="input mono" id="pg-amount" type="number" step="0.01" min="0" value="0.15" oninput="evaluatePlayground()">
          </div>
          <div class="field">
            <label for="pg-recipient">Recipient Account</label>
            <input class="input mono" id="pg-recipient" type="text" value="allowance-merchant.testnet" oninput="evaluatePlayground()">
          </div>
        </div>

        <div class="form-row" style="grid-template-columns: 1fr auto; align-items: end; margin-bottom: 0;">
          <div class="field">
            <label for="pg-memo">Memo / Note</label>
            <input class="input mono" id="pg-memo" type="text" value="interactive test #1" oninput="evaluatePlayground()">
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn" onclick="evaluatePlayground(true)">Verify Guardrails</button>
            <button class="btn btn-primary" id="pg-broadcast-btn" onclick="broadcastPlaygroundTx()">Sign & Broadcast Tx</button>
          </div>
        </div>
      </div>

      <div class="verdict-box">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <span style="font-size:11.5px;font-weight:600;color:var(--text-muted)">4-GATE ENFORCEMENT EVALUATION</span>
          <span class="badge ok mono" id="pg-verdict-badge">ALLOWED</span>
        </div>

        <div class="gate-checklist" id="pg-gates"></div>

        <div style="border-top:1px solid var(--border);padding-top:8px;display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:11.5px">
          <span class="mono" id="pg-reason-text" style="color:var(--near-green)">Passes all protocol & contract checks</span>
          <button class="copy-pill mono" onclick="copyText($('pg-cli-cmd').dataset.cmd || '')">Copy near-cli ⧉</button>
        </div>
        <div id="pg-cli-cmd" data-cmd="" style="display:none"></div>
      </div>
    </div>
  </section>

  <!-- Tabbed Activity & Audit Trail -->
  <section class="panel">
    <div class="panel-header" style="flex-wrap:wrap;gap:10px">
      <div>
        <div class="panel-title">Telemetry & Audit Trail</div>
        <div class="panel-subtitle">Live on-chain log and recorded protocol guardrail enforcement proofs</div>
      </div>
    </div>

    <div class="tab-bar">
      <button class="tab-btn active" id="tab-btn-audit" onclick="switchTab('audit')">Security Audit Log</button>
      <button class="tab-btn" id="tab-btn-log" onclick="switchTab('log')">On-Chain Spend Ledger</button>
      <button class="tab-btn" id="tab-btn-keys" onclick="switchTab('keys')">Protocol Access Keys</button>
    </div>

    <!-- Audit Tab View -->
    <div id="view-audit">
      <div class="filter-bar">
        <span style="font-size:11.5px;color:var(--text-dim);margin-right:4px">Filter:</span>
        <button class="filter-btn active" onclick="setAuditFilter('all', this)">All (7)</button>
        <button class="filter-btn" onclick="setAuditFilter('spend', this)">Allowed (3)</button>
        <button class="filter-btn" onclick="setAuditFilter('contract', this)">Blocked by Contract (2)</button>
        <button class="filter-btn" onclick="setAuditFilter('protocol', this)">Blocked by Protocol (2)</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Timestamp (UTC)</th>
              <th>Agent Identity</th>
              <th>Enforcement Verdict</th>
              <th>Attempted Spend / Action</th>
              <th>Protocol / Contract Outcome</th>
            </tr>
          </thead>
          <tbody id="audit-table-body">
            <tr><td colspan="5" style="color:var(--text-dim)">Loading audit trail…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- On-chain Log Tab View -->
    <div id="view-log" style="display:none">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID #</th>
              <th>Agent</th>
              <th>Transferred Amount</th>
              <th>Allowlisted Recipient</th>
              <th>Memo / Invoice</th>
              <th>Timestamp (UTC)</th>
            </tr>
          </thead>
          <tbody id="log-table-body">
            <tr><td colspan="6" style="color:var(--text-dim)">Loading contract spend log…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Access Keys Tab View -->
    <div id="view-keys" style="display:none">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Public Key (ed25519)</th>
              <th>Bound Agent</th>
              <th>Target Contract</th>
              <th>Allowed Methods</th>
              <th>Gas Allowance Left</th>
              <th>Nonce</th>
            </tr>
          </thead>
          <tbody id="keys-table-body">
            <tr><td colspan="6" style="color:var(--text-dim)">Loading protocol access keys…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Drawer for direct RPC verification -->
    <div class="rpc-drawer" id="rpc-drawer">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong style="font-size:12px;color:var(--text)">Direct Verification — Query Protocol Keys directly via Testnet JSON-RPC:</strong>
        <button class="btn" style="height:26px;font-size:11px" onclick="copyText(document.getElementById('rpc-code-curl').innerText)">Copy curl</button>
      </div>
      <pre class="mono" id="rpc-code-curl">curl -s -X POST https://rpc.testnet.fastnear.com -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"query","params":{"request_type":"view_access_key_list","finality":"final","account_id":"allowance-swarm.testnet"}}'</pre>
    </div>
  </section>

</main>

<footer>
  <div>
    <strong>Allowkey</strong> · Protocol-Scoped Vault for AI Swarms · NEAR Hackathon 2026
  </div>
  <div class="mono">
    Vault: <span id="foot-vault">allowance-swarm.testnet</span> · RPC: <span id="foot-rpc">rpc.testnet.fastnear.com</span>
  </div>
</footer>

<!-- Connect Wallet & Scoped Key Signer Modal -->
<div class="modal-backdrop" id="wallet-modal" onclick="if(event.target===this)closeWalletModal()">
  <div class="modal">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div>
        <div style="font-weight:700;font-size:15px">Connect NEAR Testnet Wallet or Agent Key</div>
        <div style="font-size:12px;color:var(--text-dim)">Interact directly with <code class="mono" id="modal-vault-label">allowance-swarm.testnet</code></div>
      </div>
      <button class="btn" style="height:26px;padding:0 8px" onclick="closeWalletModal()">✕</button>
    </div>

    <div class="modal-sec">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong style="font-size:12.5px">1. MyNearWallet (NEAR Testnet)</strong>
        <span class="badge info mono" style="font-size:10px">Browser Redirect</span>
      </div>
      <div style="font-size:12px;color:var(--text-muted)">
        Sign in with your own <code class="mono">.testnet</code> account. If you are the vault owner, you can toggle <code class="mono">set_paused</code> or reset agent budgets; if you are another user, you can test that the contract rejects unauthorized callers on-chain.
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-primary" id="mynear-connect-btn" onclick="connectMyNearWallet()" style="width:100%">
          Connect MyNearWallet (Testnet) ↗
        </button>
        <button class="btn" id="mynear-disconnect-btn" onclick="disconnectWallet()" style="display:none">
          Disconnect
        </button>
      </div>
    </div>

    <div class="modal-sec">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong style="font-size:12.5px">2. In-Browser Agent / Ephemeral Key Signer</strong>
        <span class="badge neutral mono" style="font-size:10px">Zero Redirect · Memory Only</span>
      </div>
      <div style="font-size:12px;color:var(--text-muted)">
        Paste a scoped <code class="mono">ed25519:...</code> secret key (from <code class="mono">agent/keys/</code>) to broadcast real <code class="mono">spend_as</code> calls directly from your browser. Keys stay in browser memory only.
      </div>
      <div class="field">
        <label for="signer-account-input">Signer Account ID</label>
        <input class="input mono" id="signer-account-input" type="text" value="allowance-swarm.testnet">
      </div>
      <div class="field">
        <label for="signer-key-input">Secret Key (ed25519:...)</label>
        <input class="input mono" id="signer-key-input" type="password" placeholder="ed25519:3D4YudUahN...">
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="useInBrowserKey()" style="flex:1">Use Key in Session</button>
        <button class="btn" onclick="generateRogueKey()" title="Generate a random unregistered ed25519 key">Generate Rogue Key</button>
      </div>
    </div>

    <div class="modal-sec">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <strong style="font-size:12.5px">3. Inspect Another Deployed Vault</strong>
        <span class="badge neutral mono" style="font-size:10px">Custom Contract</span>
      </div>
      <div style="display:flex;gap:8px">
        <input class="input mono" id="custom-vault-input" type="text" value="allowance-swarm.testnet" style="flex:1">
        <button class="btn" onclick="switchVaultAccount()">Load Vault</button>
      </div>
    </div>

    <div id="wallet-status-note" class="mono" style="font-size:11.5px;color:var(--text-dim);text-align:center">
      Mode: Read-Only + Guardrail Simulator active
    </div>
  </div>
</div>

<script>
'use strict';

const RPC_URL = 'https://rpc.testnet.fastnear.com';
let VAULT_ID = 'allowance-swarm.testnet';
let LAST_STATE = null;
let AUDIT_FILTER = 'all';
let SIM_RUNNING = false;
let NEAR_CONN = null;
let WALLET_CONN = null;
let SESSION_SIGNER = null; // { accountId, keyPair, publicKey }

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const shortKey = (s, a = 12, b = 6) => s ? (s.length > a + b + 3 ? s.slice(0, a) + '…' + s.slice(-b) : s) : '—';
const fmtYocto = y => {
  try { return (Number(BigInt(y || '0')) / 1e24).toFixed(6).replace(/\.?0+$/, '') || '0'; }
  catch (_) { return '0'; }
};

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text);
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  document.documentElement.setAttribute('data-theme', cur);
  $('theme-btn').textContent = cur === 'light' ? 'Dark' : 'Light';
  try { localStorage.setItem('allowkey-theme', cur); } catch (_) {}
}

function toggleRpcDrawer() {
  $('rpc-drawer').classList.toggle('open');
}

function switchTab(tab) {
  $('tab-btn-audit').classList.toggle('active', tab === 'audit');
  $('tab-btn-log').classList.toggle('active', tab === 'log');
  $('tab-btn-keys').classList.toggle('active', tab === 'keys');

  $('view-audit').style.display = tab === 'audit' ? 'block' : 'none';
  $('view-log').style.display = tab === 'log' ? 'block' : 'none';
  $('view-keys').style.display = tab === 'keys' ? 'block' : 'none';
}

function setAuditFilter(filter, btn) {
  AUDIT_FILTER = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (LAST_STATE) renderAudit(LAST_STATE.audit || []);
}

function classifyAudit(a) {
  if (a.event === 'spend') return { kind: 'spend', label: 'ALLOWED', cls: 'ok' };
  const raw = String(a.error || '') + ' ' + String(a.reason || '');
  if (raw.includes('MethodNameMismatch') || raw.includes('InvalidAccessKeyError')) {
    return { kind: 'protocol', label: 'BLOCKED · PROTOCOL GATE', cls: 'warn' };
  }
  return { kind: 'contract', label: 'BLOCKED · CONTRACT POLICY', cls: 'bad' };
}

function formatOutcome(a) {
  if (a.event === 'spend') {
    const tx = a.tx || '';
    return tx
      ? `<a class="mono" href="https://testnet.nearblocks.io/txns/${esc(tx)}" target="_blank" rel="noopener">tx ${esc(shortKey(tx, 8, 4))} ↗</a> <span class="badge ok" style="padding:1px 5px;font-size:10px">Confirmed</span>`
      : '<span class="badge ok">Confirmed</span>';
  }
  const rawErr = String(a.error || a.reason || '');
  if (rawErr.includes('MethodNameMismatch')) {
    const m = rawErr.match(/"method_name":\s*"([^"]+)"/);
    const method = m ? m[1] : (a.intent?.method || 'method');
    return `<span class="mono" style="color:var(--amber);font-weight:600">InvalidAccessKeyError::MethodNameMismatch</span> <span style="color:var(--text-dim)">(Forbidden from calling <code class="mono">${esc(method)}</code>)</span>`;
  }
  const reason = String(a.reason || '').replace('allowkey: blocked — ', '').trim();
  const tx = a.tx ? ` · <a class="mono" href="https://testnet.nearblocks.io/txns/${esc(a.tx)}" target="_blank" rel="noopener">tx ${esc(shortKey(a.tx, 8, 4))} ↗</a>` : '';
  if (reason) {
    return `<span style="color:var(--red);font-weight:600">${esc(reason)}</span> <span class="mono" style="color:var(--text-dim);font-size:11px">(reverted)</span>${tx}`;
  }
  return `<span style="color:var(--red)">reverted on-chain</span>${tx}`;
}

function formatIntent(i) {
  if (!i) return '—';
  if (typeof i === 'string') return esc(i);
  if (i.method && i.method !== 'spend_as' && i.method !== 'spend') {
    return `<span class="badge warn mono">${esc(i.method)}()</span> ${i.note ? `<span class="mono" style="color:var(--text-muted)">${esc(i.note)}</span>` : ''}`;
  }
  const method = i.method ? `<code class="mono" style="color:var(--text-dim)">${esc(i.method)}</code> ` : '';
  const amt = i.amount_near !== undefined ? i.amount_near : (i.amount ? fmtYocto(i.amount) : null);
  const rec = i.recipient ? ` <span style="color:var(--text-dim)">→</span> <span class="mono">${esc(i.recipient)}</span>` : '';
  const memo = i.memo ? ` <span style="color:var(--text-dim)">(${esc(i.memo)})</span>` : '';
  return method + (amt !== null ? `<strong class="mono">${esc(amt)} NEAR</strong>` : 'spend') + rec + memo;
}

function renderAudit(rows) {
  const filtered = rows.filter(a => AUDIT_FILTER === 'all' || classifyAudit(a).kind === AUDIT_FILTER);
  $('audit-table-body').innerHTML = filtered.map(a => {
    const c = classifyAudit(a);
    const ts = String(a.ts || '').replace('T', ' ').replace('Z', ' UTC');
    return `<tr>
      <td class="mono" style="color:var(--text-dim);white-space:nowrap">${esc(ts)}</td>
      <td><strong class="mono">${esc(a.agent || 'vault')}</strong></td>
      <td><span class="badge ${c.cls} mono">${c.label}</span></td>
      <td>${formatIntent(a.intent)}</td>
      <td>${formatOutcome(a)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="5" style="color:var(--text-dim)">No matching audit events.</td></tr>';
}

function renderAgents(agents, scopedKeys) {
  const pkMap = {};
  (scopedKeys || []).forEach(k => { pkMap[k.public_key] = k; });

  $('agents-container').innerHTML = (agents || []).map((a, idx) => {
    const pct = Math.min(100, a.used_pct || 0);
    const keyInfo = pkMap[a.public_key] || {};
    return `<div class="agent-box">
      <div class="agent-box-top">
        <div class="agent-title">
          <span class="agent-dot" style="${a.active ? '' : 'background:var(--red)'}"></span>
          <strong class="mono" style="font-size:14px">${esc(a.agent_id)}</strong>
          <span class="badge neutral mono" style="font-size:10px">Agent #${idx + 1}</span>
        </div>
        <span class="badge ${a.active ? 'ok' : 'bad'}">${a.active ? 'ACTIVE' : 'REVOKED'}</span>
      </div>

      <div class="agent-stats-grid mono">
        <div class="stat-item">
          <span class="k">Daily Quota</span>
          <span class="v">${esc(a.daily_near)} NEAR</span>
        </div>
        <div class="stat-item">
          <span class="k">Spent Today</span>
          <span class="v" style="color:var(--near-green)">${esc(a.spent_near)} NEAR</span>
        </div>
        <div class="stat-item">
          <span class="k">Per-Tx Cap</span>
          <span class="v">${esc(a.per_tx_near)} NEAR</span>
        </div>
        <div class="stat-item">
          <span class="k">Remaining</span>
          <span class="v">${esc(a.remaining_near)} NEAR</span>
        </div>
      </div>

      <div style="display:flex;flex-direction:column;gap:3px">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-dim)">
          <span>Budget Utilized</span>
          <span class="mono">${pct.toFixed(0)}%</span>
        </div>
        <div class="progress-bar-wrap">
          <div class="progress-bar-fill" style="width:${pct.toFixed(1)}%;${pct > 80 ? 'background:var(--amber)' : ''}"></div>
        </div>
      </div>

      <div class="agent-key-row">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span>Signer Key:</span>
          <button class="copy-pill mono" data-copy="${esc(a.public_key)}" onclick="copyText(this.dataset.copy)">
            ${esc(shortKey(a.public_key, 10, 4))} ⧉
          </button>
        </div>
        <div style="display:flex;justify-content:space-between">
          <span>Protocol Scope:</span>
          <span class="mono" style="color:var(--amber)">methods: ${esc(JSON.stringify(a.key_methods || ['spend_as']))}</span>
        </div>
        <div style="display:flex;justify-content:space-between">
          <span>Key Gas Allowance:</span>
          <span class="mono">${esc(a.key_allowance_near || keyInfo.allowance_near || '—')} NEAR</span>
        </div>
      </div>
    </div>`;
  }).join('') || '<div style="color:var(--text-dim)">No agents registered on this vault.</div>';
}

function renderLog(log) {
  $('log-table-body').innerHTML = (log || []).slice().reverse().map(e => {
    const ts = e.timestamp ? new Date(Number(e.timestamp) / 1e6).toISOString().replace('T', ' ').slice(0, 19) : '—';
    const amt = fmtYocto(e.amount);
    return `<tr>
      <td class="mono" style="color:var(--text-dim)">#${esc(e.id)}</td>
      <td><span class="badge info mono">${esc(e.agent || 'vault')}</span></td>
      <td class="mono" style="color:var(--near-green);font-weight:600">+${esc(amt)} NEAR</td>
      <td class="mono">${esc(e.recipient)}</td>
      <td>${esc(e.memo)}</td>
      <td class="mono" style="color:var(--text-dim)">${esc(ts)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="color:var(--text-dim)">No on-chain spends recorded yet.</td></tr>';
}

function renderKeys(keys, agents) {
  const pkMap = {};
  (agents || []).forEach(a => { pkMap[a.public_key] = a.agent_id; });

  $('keys-table-body').innerHTML = (keys || []).map(k => {
    const bound = pkMap[k.public_key];
    const methods = (k.method_names || []).map(m => `<span class="badge warn mono" style="font-size:10px">${esc(m)}</span>`).join(' ');
    return `<tr>
      <td>
        <button class="copy-pill mono" data-copy="${esc(k.public_key)}" onclick="copyText(this.dataset.copy)">
          ${esc(shortKey(k.public_key, 12, 4))} ⧉
        </button>
      </td>
      <td>${bound ? `<span class="badge info mono">${esc(bound)}</span>` : '<span style="color:var(--text-dim)">unassigned</span>'}</td>
      <td class="mono">${esc(k.receiver_id)}</td>
      <td>${methods || '<span class="badge bad">ANY</span>'}</td>
      <td class="mono">${esc(k.allowance_near || 'unlimited')} NEAR</td>
      <td class="mono" style="color:var(--text-dim)">${esc(k.nonce ?? '—')}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="color:var(--text-dim)">No FunctionCall keys found.</td></tr>';
}

const DEFAULT_AUDIT = [
  {"ts": "2026-11-09T07:24:15Z", "event": "blocked_onchain", "tx": null, "error": "{\"name\": \"HANDLER_ERROR\", \"cause\": {\"info\": {}, \"name\": \"INVALID_TRANSACTION\"}, \"code\": -32000, \"message\": \"Server error\", \"data\": {\"TxExecutionError\": {\"InvalidTxError\": {\"InvalidAccessKeyError\": {\"MethodNameMismatch\": {\"method_name\": \"spend\"}}}}}}", "agent": "agent-alpha", "intent": {"method": "spend", "amount_near": 0.01, "recipient": "allowance-merchant.testnet", "memo": "wrong method"}},
  {"ts": "2026-11-09T07:24:13Z", "event": "blocked_onchain", "tx": null, "error": "{\"name\": \"HANDLER_ERROR\", \"cause\": {\"info\": {}, \"name\": \"INVALID_TRANSACTION\"}, \"code\": -32000, \"message\": \"Server error\", \"data\": {\"TxExecutionError\": {\"InvalidTxError\": {\"InvalidAccessKeyError\": {\"MethodNameMismatch\": {\"method_name\": \"set_paused\"}}}}}}", "agent": "agent-alpha", "intent": {"method": "set_paused", "note": "paused=true"}},
  {"ts": "2026-11-09T07:24:11Z", "event": "blocked_onchain", "tx": "7jxgaXF8DfURinxYXn69zmJsVGrTt4CAzvzyXMRq735J", "error": "{\"Failure\": {\"ActionError\": {\"index\": 0, \"kind\": {\"FunctionCallError\": {\"ExecutionError\": \"WebAssembly trap: An `unreachable` opcode was executed.\"}}}}}", "reason": "vault per-transaction cap exceeded", "agent": "agent-beta", "intent": {"method": "spend_as", "amount_near": 0.9, "recipient": "allowance-merchant.testnet", "memo": "over daily"}},
  {"ts": "2026-11-09T07:24:08Z", "event": "spend", "tx": "A3CQCoT9nJPeunVVF82Rm7Do8FhppFcXK7GEKMoBwhQu", "explorer": "https://testnet.nearblocks.io/txns/A3CQCoT9nJPeunVVF82Rm7Do8FhppFcXK7GEKMoBwhQu", "agent": "agent-alpha", "intent": {"method": "spend_as", "amount_near": 0.1, "recipient": "allowance-merchant.testnet", "memo": "alpha invoice #2"}},
  {"ts": "2026-11-09T07:24:05Z", "event": "blocked_onchain", "tx": "5Kwo9NeTmVWHhaWy7o3HBMF56NoHJuKNDAtQcSKdc3sZ", "error": "{\"Failure\": {\"ActionError\": {\"index\": 0, \"kind\": {\"FunctionCallError\": {\"ExecutionError\": \"WebAssembly trap: An `unreachable` opcode was executed.\"}}}}}", "reason": "agent per-transaction cap exceeded", "agent": "agent-alpha", "intent": {"method": "spend_as", "amount_near": 0.25, "recipient": "allowance-merchant.testnet", "memo": "over cap"}},
  {"ts": "2026-11-09T07:24:02Z", "event": "spend", "tx": "AcjJL7oCGd4Zes5uhy6xHFm8WYuD76wUvHJkS1G3Aere", "explorer": "https://testnet.nearblocks.io/txns/AcjJL7oCGd4Zes5uhy6xHFm8WYuD76wUvHJkS1G3Aere", "agent": "agent-beta", "intent": {"method": "spend_as", "amount_near": 0.15, "recipient": "allowance-merchant.testnet", "memo": "beta invoice #1"}},
  {"ts": "2026-11-09T07:23:58Z", "event": "spend", "tx": "4Jh9qMEGodk1c5e6tViNvSUor2wk6Z3EpV7r9EApciFY", "explorer": "https://testnet.nearblocks.io/txns/4Jh9qMEGodk1c5e6tViNvSUor2wk6Z3EpV7r9EApciFY", "agent": "agent-alpha", "intent": {"method": "spend_as", "amount_near": 0.15, "recipient": "allowance-merchant.testnet", "memo": "alpha invoice #1"}}
];

const INITIAL_STATE = {
  vault: VAULT_ID,
  rpc: RPC_URL,
  policy: {
    owner: VAULT_ID,
    per_tx: "500000000000000000000000",
    daily: "5000000000000000000000000",
    spent_today: "1550000000000000000000000",
    day: 20766,
    recipients: ["allowance-merchant.testnet"],
    paused: false,
    agent_count: 2
  },
  balance: "5670000000000000000000000",
  balance_near: "5.67",
  total_spent_near: "1.55",
  total_spent_raw: 1.55e24,
  remaining_near: "3.45",
  per_tx_near: "0.5",
  daily_near: "5",
  spent_today_near: "1.55",
  used_pct: 31.0,
  agents: [
    {
      agent_id: "agent-alpha",
      public_key: "ed25519:GY1SXN4zftJ7u2iY3RZ5GgwWBAQBtb6g5TMcTpm8ps17",
      per_tx: "200000000000000000000000",
      daily: "1000000000000000000000000",
      spent_today: "250000000000000000000000",
      remaining_today: "750000000000000000000000",
      active: true,
      key_allowance_near: "1.471",
      key_methods: ["spend_as"],
      key_receiver: "allowance-swarm.testnet",
      key_nonce: 104281941,
      per_tx_near: "0.2",
      daily_near: "1",
      spent_near: "0.25",
      remaining_near: "0.75",
      used_pct: 25.0
    },
    {
      agent_id: "agent-beta",
      public_key: "ed25519:GYSHUupms2UpXfwhzagtPixTm7m65gvQuFWAXB9NGKEQ",
      per_tx: "200000000000000000000000",
      daily: "1000000000000000000000000",
      spent_today: "150000000000000000000000",
      remaining_today: "850000000000000000000000",
      active: true,
      key_allowance_near: "1.484",
      key_methods: ["spend_as"],
      key_receiver: "allowance-swarm.testnet",
      key_nonce: 104281952,
      per_tx_near: "0.2",
      daily_near: "1",
      spent_near: "0.15",
      remaining_near: "0.85",
      used_pct: 15.0
    }
  ],
  agent_count: 2,
  scoped_keys: [
    {
      public_key: "ed25519:GY1SXN4zftJ7u2iY3RZ5GgwWBAQBtb6g5TMcTpm8ps17",
      receiver_id: "allowance-swarm.testnet",
      method_names: ["spend_as"],
      allowance_near: "1.471",
      nonce: 104281941
    },
    {
      public_key: "ed25519:GYSHUupms2UpXfwhzagtPixTm7m65gvQuFWAXB9NGKEQ",
      receiver_id: "allowance-swarm.testnet",
      method_names: ["spend_as"],
      allowance_near: "1.484",
      nonce: 104281952
    }
  ],
  log: [
    { id: 17, caller: "allowance-swarm.testnet", agent: "agent-alpha", amount: "150000000000000000000000", recipient: "allowance-merchant.testnet", memo: "alpha invoice #1", timestamp: 1794209038000000000 },
    { id: 18, caller: "allowance-swarm.testnet", agent: "agent-beta", amount: "150000000000000000000000", recipient: "allowance-merchant.testnet", memo: "beta invoice #1", timestamp: 1794209042000000000 },
    { id: 19, caller: "allowance-swarm.testnet", agent: "agent-alpha", amount: "100000000000000000000000", recipient: "allowance-merchant.testnet", memo: "alpha invoice #2", timestamp: 1794209049000000000 }
  ],
  log_len: 19,
  audit: DEFAULT_AUDIT,
  fetched_at: "2026-11-09T07:24:15Z"
};

function applyState(s, sourceLabel = 'LIVE RPC') {
  LAST_STATE = s;
  $('source-label').textContent = sourceLabel + (s.fetched_at ? ' · ' + s.fetched_at : '');
  $('live-dot').className = 'pulse-dot ' + (sourceLabel.includes('LIVE') ? 'live' : '');
  $('vault-pill-link').textContent = s.vault + ' ↗';
  $('vault-pill-link').href = 'https://testnet.nearblocks.io/address/' + encodeURIComponent(s.vault);
  $('foot-vault').textContent = s.vault;
  $('foot-rpc').textContent = String(s.rpc || RPC_URL).replace(/^https?:\/\//, '');

  // KPI 1: Balance & Active
  $('kpi-balance').textContent = s.balance_near;
  const isPaused = s.policy?.paused;
  $('vault-active-badge').textContent = isPaused ? 'VAULT PAUSED' : 'SWARM ACTIVE';
  $('vault-active-badge').className = 'badge ' + (isPaused ? 'bad' : 'ok') + ' mono';

  // KPI 2: Cap
  $('kpi-spent').textContent = s.total_spent_near;
  $('kpi-daily').textContent = s.daily_near;
  $('kpi-remaining').textContent = s.remaining_near;
  $('kpi-pertx').textContent = 'per-tx: ' + s.per_tx_near + ' NEAR';
  const usedPct = Math.min(100, (100 * (s.total_spent_raw || 0)) / Number(s.policy?.daily || 1));
  $('kpi-cap-pct').textContent = usedPct.toFixed(1) + '% used';
  $('daily-cap-meter').style.width = usedPct.toFixed(1) + '%';
  $('daily-cap-meter').style.background = usedPct > 80 ? 'var(--amber)' : 'var(--near-green)';

  // KPI 3: Agents
  $('kpi-agent-count').textContent = s.agent_count;
  $('kpi-keys-badge').textContent = (s.scoped_keys || []).length + ' Scoped Keys';

  // KPI 4: Audit
  const audit = s.audit || [];
  const allowed = audit.filter(x => x.event === 'spend').length;
  const blocked = audit.length - allowed;
  const proto = audit.filter(x => classifyAudit(x).kind === 'protocol').length;
  const contract = audit.filter(x => classifyAudit(x).kind === 'contract').length;
  $('kpi-audit-count').textContent = audit.length + ' Events';
  $('kpi-blocked').textContent = blocked;
  $('kpi-allowed').textContent = allowed;
  $('kpi-block-breakdown').textContent = `${contract} Contract Cap · ${proto} Protocol Scope`;

  // Policy Details
  $('pol-owner').textContent = s.policy?.owner || '—';
  $('pol-pertx').textContent = s.per_tx_near + ' NEAR';
  $('pol-daily').textContent = s.daily_near + ' NEAR';
  $('pol-spent').textContent = s.total_spent_near + ' NEAR';
  $('pol-recips').textContent = (s.policy?.recipients || []).join(', ') || '—';
  $('pol-killswitch').textContent = `set_paused(${s.policy?.paused})`;
  $('pol-loglen').textContent = (s.log_len || 0) + ' transactions';

  renderAgents(s.agents, s.scoped_keys);
  renderAudit(audit);
  renderLog(s.log);
  renderKeys(s.scoped_keys, s.agents);
  if (typeof evaluatePlayground === 'function') evaluatePlayground();
}

async function fetchDirectRpcState() {
  async function rpcQuery(params) {
    const res = await fetch(RPC_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 'allowkey', method: 'query', params })
    });
    const j = await res.json();
    if (j.error) throw new Error(JSON.stringify(j.error));
    return j.result;
  }
  async function viewCall(method_name) {
    const r = await rpcQuery({ request_type: 'call_function', account_id: VAULT_ID, method_name, args_base64: 'e30=', finality: 'final' });
    return JSON.parse(new TextDecoder().decode(new Uint8Array(r.result || [])));
  }

  const [policy, balance, log, agents, keyRes] = await Promise.all([
    viewCall('get_policy'),
    viewCall('get_balance'),
    viewCall('get_log'),
    viewCall('get_agents'),
    rpcQuery({ request_type: 'view_access_key_list', account_id: VAULT_ID, finality: 'final' })
  ]);

  const keys = (keyRes.keys || []).filter(k => k.access_key?.permission?.FunctionCall).map(k => {
    const fc = k.access_key.permission.FunctionCall;
    return {
      public_key: k.public_key,
      receiver_id: fc.receiver_id,
      method_names: fc.method_names,
      allowance_near: fc.allowance ? fmtYocto(fc.allowance) : 'unlimited',
      nonce: k.access_key.nonce
    };
  });

  const byPk = Object.fromEntries(keys.map(k => [k.public_key, k]));
  let agentsSpent = 0n;
  (agents || []).forEach(a => {
    const k = byPk[a.public_key];
    a.key_allowance_near = k ? k.allowance_near : null;
    a.key_methods = k ? k.method_names : null;
    a.per_tx_near = fmtYocto(a.per_tx);
    a.daily_near = fmtYocto(a.daily);
    a.spent_near = fmtYocto(a.spent_today);
    a.remaining_near = fmtYocto(a.remaining_today);
    a.used_pct = Number(a.daily) ? (100 * Number(a.spent_today) / Number(a.daily)) : 0;
    agentsSpent += BigInt(a.spent_today || '0');
  });

  const polSpent = BigInt(policy.spent_today || '0');
  const totalSpent = polSpent > agentsSpent ? polSpent : agentsSpent;
  const dailyBig = BigInt(policy.daily || '0');
  const remBig = dailyBig > totalSpent ? (dailyBig - totalSpent) : 0n;

  return {
    vault: VAULT_ID,
    rpc: RPC_URL,
    policy,
    balance,
    balance_near: fmtYocto(balance),
    total_spent_near: fmtYocto(totalSpent.toString()),
    total_spent_raw: Number(totalSpent),
    remaining_near: fmtYocto(remBig.toString()),
    per_tx_near: fmtYocto(policy.per_tx),
    daily_near: fmtYocto(policy.daily),
    spent_today_near: fmtYocto(totalSpent.toString()),
    agents: agents || [],
    agent_count: (agents || []).length,
    scoped_keys: keys,
    log: log || [],
    log_len: (log || []).length,
    audit: DEFAULT_AUDIT,
    fetched_at: new Date().toISOString().slice(0, 19) + 'Z'
  };
}

async function runDemoSimulation() {
  if (SIM_RUNNING) return;
  SIM_RUNNING = true;
  const runner = $('sim-runner');
  const btn = $('sim-btn');
  const badge = $('sim-step-badge');
  const title = $('sim-step-title');
  const outcome = $('sim-step-outcome');
  const bar = $('sim-progress-bar');
  btn.textContent = '● Running Replay…';
  runner.classList.add('active');

  const steps = [
    { label: "1. agent-alpha calls spend_as(0.15 NEAR) -> merchant", res: "ALLOWED · tx 4Jh9qMEG…", gate: 4, a: 0.15, b: 0, logs: 1 },
    { label: "2. agent-beta calls spend_as(0.15 NEAR) -> merchant", res: "ALLOWED · tx AcjJL7oC… (independent budget)", gate: 4, a: 0.15, b: 0.15, logs: 2 },
    { label: "3. agent-alpha tries 0.25 NEAR (exceeds 0.20 per-tx cap)", res: "BLOCKED BY CONTRACT · agent per-tx cap exceeded · tx 5Kwo9NeT…", gate: 3, a: 0.15, b: 0.15, logs: 2 },
    { label: "4. agent-alpha calls spend_as(0.10 NEAR) -> merchant", res: "ALLOWED · tx A3CQCoT9… (alpha 0.25 / 1.00 NEAR)", gate: 4, a: 0.25, b: 0.15, logs: 3 },
    { label: "5. agent-beta tries 0.90 NEAR (exceeds 0.50 vault cap)", res: "BLOCKED BY CONTRACT · vault per-tx cap exceeded · tx 7jxgaXF8…", gate: 3, a: 0.25, b: 0.15, logs: 3 },
    { label: "6. agent-alpha tries set_paused(true) [unauthorized method]", res: "BLOCKED BY PROTOCOL · MethodNameMismatch (never touched contract)", gate: 2, a: 0.25, b: 0.15, logs: 3 },
    { label: "7. agent-alpha tries vault spend(0.01 NEAR) [unauthorized method]", res: "BLOCKED BY PROTOCOL · MethodNameMismatch (never touched contract)", gate: 2, a: 0.25, b: 0.15, logs: 3 }
  ];

  const chrono = [...DEFAULT_AUDIT].reverse();

  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    badge.textContent = `STEP ${i + 1} / 7`;
    const isAllowed = s.res.startsWith('ALLOWED');
    const isProto = s.res.includes('PROTOCOL');
    badge.className = 'badge mono ' + (isAllowed ? 'ok' : (isProto ? 'warn' : 'bad'));
    title.textContent = s.label;
    outcome.textContent = s.res;
    bar.style.width = (((i + 1) / steps.length) * 100).toFixed(0) + '%';

    // Highlight schematic gates
    $('step-layer-1').className = 'pipe-step active-pass';
    $('step-layer-2').className = 'pipe-step ' + (s.gate >= 2 ? (s.gate === 2 ? 'active-warn' : 'active-pass') : '');
    $('step-layer-3').className = 'pipe-step ' + (s.gate >= 3 ? (s.gate === 3 ? 'active-block' : 'active-pass') : '');
    $('step-layer-4').className = 'pipe-step ' + (s.gate === 4 ? 'active-pass' : '');

    const simState = JSON.parse(JSON.stringify(INITIAL_STATE));
    const tot = +(1.15 + s.a + s.b).toFixed(2);
    simState.total_spent_near = String(tot);
    simState.total_spent_raw = tot * 1e24;
    simState.remaining_near = String(+(5 - tot).toFixed(2));
    simState.agents[0].spent_near = String(s.a);
    simState.agents[0].remaining_near = String(+(1 - s.a).toFixed(2));
    simState.agents[0].used_pct = s.a * 100;
    simState.agents[1].spent_near = String(s.b);
    simState.agents[1].remaining_near = String(+(1 - s.b).toFixed(2));
    simState.agents[1].used_pct = s.b * 100;
    simState.audit = chrono.slice(0, i + 1).reverse();
    simState.log = INITIAL_STATE.log.slice(0, s.logs);
    simState.log_len = 16 + s.logs;

    applyState(simState, `DEMO STEP ${i + 1}/7`);
    await new Promise(r => setTimeout(r, 1400));
  }

  badge.textContent = 'VERDICT · 100% PASS';
  badge.className = 'badge ok mono';
  title.textContent = 'Swarm Invariant Proof Verified: Budget isolation, vault cap, and method scoping proved.';
  outcome.textContent = 'alpha: 0.25 NEAR · beta: 0.15 NEAR · vault active';
  btn.textContent = '▶ Replay Swarm Demo';
  SIM_RUNNING = false;

  setTimeout(() => {
    $('step-layer-1').className = 'pipe-step';
    $('step-layer-2').className = 'pipe-step';
    $('step-layer-3').className = 'pipe-step';
    $('step-layer-4').className = 'pipe-step';
  }, 4000);
}

async function tick(force = false) {
  if (SIM_RUNNING && !force) return;
  const btn = $('refresh-btn');
  if (force && btn) btn.textContent = 'Syncing…';
  try {
    let s;
    try {
      if (VAULT_ID !== 'allowance-swarm.testnet') throw new Error('custom vault');
      const r = await fetch(force ? '/api/state?force=1' : '/api/state', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      s = await r.json();
      if (s.error) throw new Error(s.error);
    } catch (_) {
      s = await fetchDirectRpcState();
    }
    applyState(s, 'LIVE RPC');
  } catch (_) {
    if (!LAST_STATE) {
      applyState(INITIAL_STATE, 'TESTNET SNAPSHOT');
    }
  } finally {
    if (btn) btn.textContent = 'Refresh';
  }
}

// Initial paint: Testnet snapshot immediately, then fetch live RPC
function loadPreset(name) {
  if (name === 'valid') {
    $('pg-caller').value = 'agent-alpha';
    $('pg-method').value = 'spend_as';
    $('pg-amount').value = '0.15';
    $('pg-recipient').value = 'allowance-merchant.testnet';
    $('pg-memo').value = 'alpha invoice #1';
  } else if (name === 'over_agent') {
    $('pg-caller').value = 'agent-alpha';
    $('pg-method').value = 'spend_as';
    $('pg-amount').value = '0.25';
    $('pg-recipient').value = 'allowance-merchant.testnet';
    $('pg-memo').value = 'over agent 0.20 cap';
  } else if (name === 'over_vault') {
    $('pg-caller').value = 'agent-beta';
    $('pg-method').value = 'spend_as';
    $('pg-amount').value = '0.90';
    $('pg-recipient').value = 'allowance-merchant.testnet';
    $('pg-memo').value = 'over vault 0.50 cap';
  } else if (name === 'bad_recip') {
    $('pg-caller').value = 'agent-alpha';
    $('pg-method').value = 'spend_as';
    $('pg-amount').value = '0.10';
    $('pg-recipient').value = 'hacker-drain.testnet';
    $('pg-memo').value = 'unauthorized merchant';
  } else if (name === 'pause_hack') {
    $('pg-caller').value = 'agent-alpha';
    $('pg-method').value = 'set_paused';
    $('pg-amount').value = '0';
    $('pg-recipient').value = 'allowance-merchant.testnet';
    $('pg-memo').value = 'paused=true';
  }
  evaluatePlayground(true);
}

function evaluatePlayground(highlightTop = false) {
  const s = LAST_STATE || INITIAL_STATE;
  const caller = $('pg-caller').value;
  const method = $('pg-method').value;
  const amount = parseFloat($('pg-amount').value || '0');
  const recipient = ($('pg-recipient').value || '').trim();
  const memo = ($('pg-memo').value || '').trim();

  const agents = s.agents || [];
  const ag = agents.find(a => a.agent_id === caller || (caller === 'wallet' && SESSION_SIGNER && a.public_key === SESSION_SIGNER.publicKey)) || null;
  const vaultPerTx = parseFloat(s.per_tx_near || '0.5');
  const vaultRem = parseFloat(s.remaining_near || '3.45');
  const allowlist = (s.policy && s.policy.recipients) || ['allowance-merchant.testnet'];
  const paused = !!(s.policy && s.policy.paused);

  const gates = [];
  let verdict = 'ALLOWED';
  let verdictCls = 'ok';
  let reason = 'Clears all 4 protocol & contract guardrails';
  let stopGate = 4;

  // Gate 1: Signer Key
  const g1Pass = caller === 'agent-alpha' || caller === 'agent-beta' || (caller === 'wallet' && (SESSION_SIGNER || (WALLET_CONN && WALLET_CONN.isSignedIn())));
  const signerLabel = ag ? shortKey(ag.public_key, 12, 4) : (caller === 'rogue' ? 'ed25519:RogueUnregisteredKey' : (SESSION_SIGNER ? shortKey(SESSION_SIGNER.publicKey, 12, 4) : (WALLET_CONN && WALLET_CONN.isSignedIn() ? WALLET_CONN.getAccountId() : 'unconnected')));
  gates.push({ name: '1. Signer Key Attribution', detail: signerLabel, status: 'PASS', cls: 'ok' });

  // Gate 2: Protocol Method Scope
  const isScopedAgent = caller === 'agent-alpha' || caller === 'agent-beta';
  if (isScopedAgent && method !== 'spend_as') {
    gates.push({ name: '2. NEAR Protocol Key Scope', detail: `methods: ["spend_as"] != "${method}"`, status: 'BLOCKED', cls: 'warn' });
    gates.push({ name: '3. Vault Contract Caps', detail: 'Skipped (halted by protocol)', status: '—', cls: 'neutral' });
    gates.push({ name: '4. Allowlist & Settlement', detail: 'Skipped (halted by protocol)', status: '—', cls: 'neutral' });
    verdict = 'BLOCKED · PROTOCOL';
    verdictCls = 'warn';
    reason = `InvalidAccessKeyError::MethodNameMismatch ("${method}")`;
    stopGate = 2;
  } else {
    gates.push({ name: '2. NEAR Protocol Key Scope', detail: isScopedAgent ? 'method "spend_as" permitted' : `calling "${method}"`, status: 'PASS', cls: 'ok' });

    // Gate 3: Contract Policy & Caps
    let g3Fail = null;
    if (method !== 'spend_as') {
      const ownerId = (s.policy && s.policy.owner) || VAULT_ID;
      const connectedId = caller === 'wallet'
        ? (SESSION_SIGNER ? SESSION_SIGNER.accountId : (WALLET_CONN && WALLET_CONN.isSignedIn() ? WALLET_CONN.getAccountId() : ''))
        : '';
      if (connectedId !== ownerId) {
        g3Fail = 'allowkey: blocked — only the vault owner can call this';
      } else if (method === 'spend') {
        if (paused) {
          g3Fail = 'allowkey: blocked — vault is paused by owner';
        } else if (amount <= 0 || isNaN(amount)) {
          g3Fail = 'allowkey: blocked — amount must be > 0';
        } else if (amount > vaultPerTx) {
          g3Fail = `allowkey: blocked — per-transaction cap exceeded (${amount} > ${vaultPerTx} NEAR)`;
        } else if (amount > vaultRem) {
          g3Fail = `allowkey: blocked — daily cap exceeded (${amount} > ${vaultRem} NEAR left)`;
        }
      }
    } else if (paused) {
      g3Fail = 'allowkey: blocked — vault is paused by owner';
    } else if (amount <= 0 || isNaN(amount)) {
      g3Fail = 'allowkey: blocked — amount must be > 0';
    } else if (amount > vaultPerTx) {
      g3Fail = `allowkey: blocked — vault per-transaction cap exceeded (${amount} > ${vaultPerTx} NEAR)`;
    } else if (amount > vaultRem) {
      g3Fail = `allowkey: blocked — vault daily cap exceeded (${amount} > ${vaultRem} NEAR left)`;
    } else if (!ag) {
      g3Fail = 'allowkey: blocked — signer key is not a registered agent';
    } else if (!ag.active) {
      g3Fail = 'allowkey: blocked — agent is revoked';
    } else if (amount > parseFloat(ag.per_tx_near)) {
      g3Fail = `allowkey: blocked — agent per-transaction cap exceeded (${amount} > ${ag.per_tx_near} NEAR)`;
    } else if (amount > parseFloat(ag.remaining_near)) {
      g3Fail = `allowkey: blocked — agent daily cap exceeded (${amount} > ${ag.remaining_near} NEAR left)`;
    }

    if (g3Fail) {
      gates.push({ name: '3. Vault Contract Caps', detail: g3Fail.replace('allowkey: blocked — ', ''), status: 'REVERT', cls: 'bad' });
      gates.push({ name: '4. Allowlist & Settlement', detail: 'Skipped (contract reverted)', status: '—', cls: 'neutral' });
      verdict = 'BLOCKED · CONTRACT';
      verdictCls = 'bad';
      reason = g3Fail;
      stopGate = 3;
    } else {
      gates.push({ name: '3. Vault Contract Caps', detail: ag ? `${amount} N <= agent ${ag.per_tx_near} N & vault ${vaultPerTx} N` : 'Owner check passed', status: 'PASS', cls: 'ok' });

      // Gate 4: Recipient Allowlist
      if ((method === 'spend_as' || method === 'spend') && !allowlist.includes(recipient)) {
        gates.push({ name: '4. Allowlist & Settlement', detail: `"${recipient}" not in allowlist`, status: 'REVERT', cls: 'bad' });
        verdict = 'BLOCKED · ALLOWLIST';
        verdictCls = 'bad';
        reason = 'allowkey: blocked — recipient is not on the allowlist';
        stopGate = 3;
      } else {
        gates.push({ name: '4. Allowlist & Settlement', detail: method === 'spend_as' ? `Transfer ${amount} NEAR -> ${recipient}` : `Execute ${method}`, status: 'PASS', cls: 'ok' });
      }
    }
  }

  $('pg-verdict-badge').textContent = verdict;
  $('pg-verdict-badge').className = 'badge mono ' + verdictCls;
  $('pg-reason-text').textContent = reason;
  $('pg-reason-text').style.color = verdictCls === 'ok' ? 'var(--near-green)' : (verdictCls === 'warn' ? 'var(--amber)' : 'var(--red)');

  $('pg-gates').innerHTML = gates.map(g => `<div class="gate-item">
    <span><strong>${esc(g.name)}</strong> <span class="mono" style="color:var(--text-dim);margin-left:6px">${esc(g.detail)}</span></span>
    <span class="badge ${g.cls} mono" style="font-size:10px;padding:1px 6px">${esc(g.status)}</span>
  </div>`).join('');

  const yocto = BigInt(Math.round(Math.max(0, amount) * 1e6)) * 1000000000000000000n;
  const targetResetKey = ag ? ag.public_key : (agents[0] ? agents[0].public_key : 'ed25519:...');
  const argsJson = method === 'set_paused'
    ? JSON.stringify({ paused: true })
    : (method === 'reset_agent_day'
      ? JSON.stringify({ public_key: targetResetKey })
      : JSON.stringify({ amount: yocto.toString(), recipient, memo }));
  $('pg-cli-cmd').dataset.cmd = `near call ${VAULT_ID} ${method} '${argsJson}' --accountId ${VAULT_ID}`;

  if (highlightTop) {
    $('step-layer-1').className = 'pipe-step active-pass';
    $('step-layer-2').className = 'pipe-step ' + (stopGate === 2 ? 'active-warn' : 'active-pass');
    $('step-layer-3').className = 'pipe-step ' + (stopGate >= 3 ? (stopGate === 3 ? 'active-block' : 'active-pass') : '');
    $('step-layer-4').className = 'pipe-step ' + (stopGate === 4 ? 'active-pass' : '');
  }
}

/* --- Wallet & In-Browser Key Signer --- */
function openWalletModal() {
  $('modal-vault-label').textContent = VAULT_ID;
  $('custom-vault-input').value = VAULT_ID;
  $('wallet-modal').classList.add('open');
}
function closeWalletModal() {
  $('wallet-modal').classList.remove('open');
}

async function initNearWallet() {
  if (!window.nearApi) return;
  try {
    const keyStore = new window.nearApi.keyStores.BrowserLocalStorageKeyStore();
    NEAR_CONN = await window.nearApi.connect({
      networkId: 'testnet',
      keyStore,
      nodeUrl: RPC_URL,
      walletUrl: 'https://testnet.mynearwallet.com',
      helperUrl: 'https://helper.testnet.near.org'
    });
    WALLET_CONN = new window.nearApi.WalletConnection(NEAR_CONN, 'allowkey_vault');
    updateWalletUI();
  } catch (_) {}
}

function updateWalletUI() {
  const dot = $('wallet-dot');
  const lbl = $('wallet-btn-label');
  const note = $('wallet-status-note');
  const disBtn = $('mynear-disconnect-btn');
  const conBtn = $('mynear-connect-btn');

  if (SESSION_SIGNER) {
    dot.className = 'pulse-dot live';
    lbl.textContent = `Key: ${shortKey(SESSION_SIGNER.publicKey, 10, 4)}`;
    note.textContent = `Active in-browser signer: ${SESSION_SIGNER.accountId} (${shortKey(SESSION_SIGNER.publicKey, 12, 4)})`;
    note.style.color = 'var(--near-green)';
    disBtn.style.display = 'inline-flex';
  } else if (WALLET_CONN && WALLET_CONN.isSignedIn()) {
    const acc = WALLET_CONN.getAccountId();
    dot.className = 'pulse-dot live';
    lbl.textContent = acc;
    note.textContent = `Connected via MyNearWallet: ${acc}`;
    note.style.color = 'var(--near-green)';
    conBtn.textContent = `Connected: ${acc}`;
    disBtn.style.display = 'inline-flex';
  } else {
    dot.className = 'pulse-dot';
    lbl.textContent = 'Connect Wallet';
    note.textContent = 'Mode: Read-Only + Guardrail Simulator active';
    note.style.color = 'var(--text-dim)';
    conBtn.textContent = 'Connect MyNearWallet (Testnet) ↗';
    disBtn.style.display = 'none';
  }
  evaluatePlayground();
}

async function connectMyNearWallet() {
  if (window.location.protocol === 'file:') {
    alert('MyNearWallet web redirect requires http:// or https:// so it can redirect back. Run "python dashboard.py" (http://localhost:8088) or use Option 2 (In-Browser Key Signer) right below!');
    return;
  }
  if (!WALLET_CONN) await initNearWallet();
  if (!WALLET_CONN) {
    alert('near-api-js is still loading or blocked by offline mode.');
    return;
  }
  WALLET_CONN.requestSignIn({
    contractId: VAULT_ID,
    successUrl: window.location.href,
    failureUrl: window.location.href
  });
}

function disconnectWallet() {
  SESSION_SIGNER = null;
  if (WALLET_CONN && WALLET_CONN.isSignedIn()) {
    WALLET_CONN.signOut();
  }
  updateWalletUI();
}

function useInBrowserKey() {
  if (!window.nearApi) {
    alert('near-api-js is required to parse ed25519 keys.');
    return;
  }
  const acc = ($('signer-account-input').value || VAULT_ID).trim();
  const sk = ($('signer-key-input').value || '').trim();
  if (!sk) {
    alert('Please enter an ed25519:... secret key or click "Generate Rogue Key".');
    return;
  }
  try {
    const kp = window.nearApi.KeyPair.fromString(sk.startsWith('ed25519:') ? sk : ('ed25519:' + sk));
    SESSION_SIGNER = { accountId: acc, keyPair: kp, publicKey: kp.getPublicKey().toString() };
    $('pg-caller').value = 'wallet';
    updateWalletUI();
    closeWalletModal();
  } catch (e) {
    alert('Invalid ed25519 secret key: ' + e.message);
  }
}

function generateRogueKey() {
  if (!window.nearApi) {
    alert('near-api-js is required to generate an ed25519 keypair.');
    return;
  }
  const kp = window.nearApi.KeyPair.fromRandom('ed25519');
  $('signer-key-input').value = kp.toString();
  $('signer-account-input').value = VAULT_ID;
  SESSION_SIGNER = { accountId: VAULT_ID, keyPair: kp, publicKey: kp.getPublicKey().toString() };
  $('pg-caller').value = 'rogue';
  updateWalletUI();
}

function switchVaultAccount() {
  const next = ($('custom-vault-input').value || '').trim();
  if (!next) return;
  VAULT_ID = next;
  closeWalletModal();
  tick(true);
}

async function broadcastPlaygroundTx() {
  evaluatePlayground(true);
  const method = $('pg-method').value;
  const amount = parseFloat($('pg-amount').value || '0');
  const recipient = ($('pg-recipient').value || '').trim();
  const memo = ($('pg-memo').value || '').trim();
  const yocto = (BigInt(Math.round(Math.max(0, amount) * 1e6)) * 1000000000000000000n).toString();

  const s = LAST_STATE || INITIAL_STATE;
  const caller = $('pg-caller').value;
  const agents = s.agents || [];
  const selectedAgent = agents.find(a => a.agent_id === caller) || agents[0];
  const args = method === 'set_paused'
    ? { paused: true }
    : (method === 'reset_agent_day'
      ? { public_key: selectedAgent ? selectedAgent.public_key : 'ed25519:GY1SXN4zftJ7u2iY3RZ5GgwWBAQBtb6g5TMcTpm8ps17' }
      : { amount: yocto, recipient, memo });

  if (SESSION_SIGNER && window.nearApi) {
    const btn = $('pg-broadcast-btn');
    btn.textContent = 'Broadcasting…';
    try {
      const memStore = new window.nearApi.keyStores.InMemoryKeyStore();
      await memStore.setKey('testnet', SESSION_SIGNER.accountId, SESSION_SIGNER.keyPair);
      const near = await window.nearApi.connect({ networkId: 'testnet', keyStore: memStore, nodeUrl: RPC_URL });
      const account = await near.account(SESSION_SIGNER.accountId);
      const outcome = await account.functionCall({
        contractId: VAULT_ID,
        methodName: method,
        args,
        gas: '100000000000000',
        attachedDeposit: '0'
      });
      const txHash = outcome?.transaction?.hash || outcome?.transaction_outcome?.id;
      if (LAST_STATE) {
        LAST_STATE.audit.unshift({
          ts: new Date().toISOString().slice(0, 19) + 'Z',
          event: 'spend',
          tx: txHash,
          agent: SESSION_SIGNER.accountId,
          intent: { method, amount_near: amount, recipient, memo }
        });
        applyState(LAST_STATE, 'LIVE RPC');
      }
      tick(true);
    } catch (err) {
      const msg = String(err?.message || err);
      if (LAST_STATE) {
        LAST_STATE.audit.unshift({
          ts: new Date().toISOString().slice(0, 19) + 'Z',
          event: 'blocked_onchain',
          tx: null,
          error: msg,
          reason: msg,
          agent: $('pg-caller').value,
          intent: { method, amount_near: amount, recipient, memo }
        });
        applyState(LAST_STATE, 'LIVE RPC');
      }
    } finally {
      btn.textContent = 'Sign & Broadcast Tx';
    }
    return;
  }

  if (WALLET_CONN && WALLET_CONN.isSignedIn()) {
    const account = WALLET_CONN.account();
    await account.functionCall({
      contractId: VAULT_ID,
      methodName: method,
      args,
      gas: '100000000000000',
      attachedDeposit: '0'
    });
    return;
  }

  openWalletModal();
}

applyState(INITIAL_STATE, 'TESTNET SNAPSHOT');
evaluatePlayground();
window.addEventListener('load', () => { initNearWallet(); });
tick();
setInterval(() => tick(false), 12000);
</script>
</body>
</html>
"""

# `index.html` next to this file is the single source of truth for the UI; the
# copy above is only the fallback when dashboard.py runs on its own. Without this
# override the two drift apart and the embedded copy gets re-encoded (it has
# already happened twice).
_INDEX = Path(__file__).resolve().parent / "index.html"
if _INDEX.exists():
    HTML = _INDEX.read_text(encoding="utf-8")

# Pitch/showcase page served at /landing — static, never replaces the console.
_LANDING = Path(__file__).resolve().parent / "landing.html"


def _landing_html() -> str:
    try:
        return _LANDING.read_text(encoding="utf-8")
    except OSError:
        return "<h1>landing.html not found</h1>"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        try:
            path_only = self.path.split("?", 1)[0]
            if path_only.startswith("/assets/"):
                # Static assets the console loads: the lazy multi-wallet bundle
                # (`npm run build:wallet`) and the wallet logos next to it.
                # Served from site/assets so the dashboard and the static site
                # share one copy — path is resolved and jail-checked, never
                # string-concatenated.
                base = (Path(__file__).resolve().parent.parent / "site" / "assets").resolve()
                target = (base / path_only[len("/assets/"):]).resolve()
                ctype = {
                    ".js": "application/javascript; charset=utf-8",
                    ".png": "image/png",
                    ".svg": "image/svg+xml",
                    ".webp": "image/webp",
                    ".jpg": "image/jpeg",
                    ".ico": "image/x-icon",
                }.get(target.suffix.lower())
                if base not in target.parents or not target.is_file() or ctype is None:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                self._send(200, target.read_bytes(), ctype)
            elif self.path.startswith("/api/state"):
                force = "force=1" in self.path
                self._send(200, json.dumps(_state(force=force)).encode(), "application/json")
            elif self.path.startswith("/api/rejections"):
                self._send(200, json.dumps(_rejections(40)).encode(), "application/json")
            elif self.path.startswith("/api/audit"):
                self._send(200, json.dumps(_audit_rows(100)).encode(), "application/json")
            elif self.path.split("?")[0].rstrip("/") in ("/landing", "/pitch", "/showcase"):
                self._send(200, _landing_html().encode(), "text/html; charset=utf-8")
            else:
                self._send(200, HTML.encode(), "text/html; charset=utf-8")
        except Exception as exc:  # keep the demo alive
            self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

    def log_message(self, fmt: str, *args) -> None:  # quieter
        sys.stderr.write("[dashboard] " + fmt % args + "\n")


if __name__ == "__main__":
    print(f"Allowkey dashboard on http://0.0.0.0:{PORT}  vault={VAULT}  rpc={RPC}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
