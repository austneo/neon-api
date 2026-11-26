"""SQLite persistence for API keys, credits, and redeemed USDC transactions."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


class DuplicateTxError(Exception):
    """Raised when a transaction hash has already been redeemed."""


class KeyNotFoundError(Exception):
    """Raised when an API key does not exist."""


class FreeKeyRateLimitError(Exception):
    """Raised when an IP exceeds the daily free trial key quota."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key_hash TEXT PRIMARY KEY,
                key_prefix TEXT NOT NULL,
                label TEXT NOT NULL,
                credits INTEGER NOT NULL,
                total_spent INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redeemed_txs (
                tx_hash TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                amount_usdc REAL NOT NULL,
                credits_added INTEGER NOT NULL,
                key_hash TEXT NOT NULL,
                redeemed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS free_key_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_hash TEXT NOT NULL,
                claimed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_free_claims_ip_time ON free_key_claims (ip_hash, claimed_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS x402_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tx_hash TEXT NOT NULL,
                network TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                amount_usdc REAL NOT NULL,
                payer TEXT NOT NULL DEFAULT '',
                settled_at TEXT NOT NULL
            )
            """
        )


def claim_free_api_key(
    db_path: str,
    *,
    client_ip: str,
    credits: int,
    max_per_24h: int = 3,
) -> dict[str, Any]:
    """Atomically enforce per-IP 24h free key quota and mint a free trial API key."""
    ip_hash = hash_api_key(client_ip.strip() or "unknown")
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    cutoff = (now_dt - timedelta(hours=24)).isoformat(timespec="seconds")

    raw_key = f"neon_{secrets.token_urlsafe(24)}"
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:12]

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM free_key_claims WHERE ip_hash = ? AND claimed_at >= ?",
                (ip_hash, cutoff),
            ).fetchone()
            if row and int(row["cnt"]) >= max_per_24h:
                raise FreeKeyRateLimitError(
                    f"Free trial limit reached ({max_per_24h} keys per 24h). Top up with USDC."
                )
            conn.execute(
                "INSERT INTO free_key_claims (ip_hash, claimed_at) VALUES (?, ?)",
                (ip_hash, now),
            )
            conn.execute(
                "INSERT INTO api_keys (key_hash, key_prefix, label, credits, total_spent, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                (key_hash, key_prefix, "free_trial", credits, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "api_key": raw_key,
        "key_prefix": key_prefix,
        "label": "free_trial",
        "credits": credits,
        "created_at": now,
    }


def create_api_key(db_path: str, credits: int, label: str = "trial") -> dict[str, Any]:
    raw_key = f"neon_{secrets.token_urlsafe(24)}"
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:12]
    created_at = _utc_now()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO api_keys (key_hash, key_prefix, label, credits, total_spent, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (key_hash, key_prefix, label, credits, created_at),
        )
        conn.execute("COMMIT")
    return {
        "api_key": raw_key,
        "key_prefix": key_prefix,
        "label": label,
        "credits": credits,
        "created_at": created_at,
    }


def get_key_info(db_path: str, raw_key: str) -> dict[str, Any] | None:
    key_hash = hash_api_key(raw_key)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT key_prefix, label, credits, total_spent, created_at FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def deduct_credit(db_path: str, raw_key: str, amount: int = 1) -> int | None:
    """Atomically deduct credits. Returns remaining credits, or None if key missing / insufficient."""
    key_hash = hash_api_key(raw_key)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE api_keys SET credits = credits - ?, total_spent = total_spent + ? WHERE key_hash = ? AND credits >= ?",
            (amount, amount, key_hash, amount),
        )
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            return None
        row = conn.execute(
            "SELECT credits FROM api_keys WHERE key_hash = ?",
            (key_hash,),
        ).fetchone()
        conn.execute("COMMIT")
    return int(row["credits"]) if row else None


def record_redemption(
    db_path: str,
    *,
    tx_hash: str,
    chain: str,
    amount_usdc: float,
    credits_added: int,
    raw_key: str | None = None,
) -> dict[str, Any]:
    """Atomically record a verified USDC tx_hash and credit an existing or newly minted API key."""
    tx_norm = tx_hash.lower()
    now = _utc_now()
    issued_key: str | None = None

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT tx_hash FROM redeemed_txs WHERE tx_hash = ?",
                (tx_norm,),
            ).fetchone()
            if existing:
                raise DuplicateTxError(f"Transaction {tx_norm} already redeemed")

            if raw_key:
                key_hash = hash_api_key(raw_key)
                cur = conn.execute(
                    "UPDATE api_keys SET credits = credits + ? WHERE key_hash = ?",
                    (credits_added, key_hash),
                )
                if cur.rowcount == 0:
                    raise KeyNotFoundError("Provided API key does not exist")
            else:
                issued_key = f"neon_{secrets.token_urlsafe(24)}"
                key_hash = hash_api_key(issued_key)
                conn.execute(
                    "INSERT INTO api_keys (key_hash, key_prefix, label, credits, total_spent, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                    (key_hash, issued_key[:12], f"paid:{chain}", credits_added, now),
                )

            conn.execute(
                "INSERT INTO redeemed_txs (tx_hash, chain, amount_usdc, credits_added, key_hash, redeemed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (tx_norm, chain, amount_usdc, credits_added, key_hash, now),
            )
            row = conn.execute(
                "SELECT key_prefix, credits FROM api_keys WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    result: dict[str, Any] = {
        "tx_hash": tx_norm,
        "chain": chain,
        "amount_usdc": amount_usdc,
        "credits_added": credits_added,
        "total_credits": int(row["credits"]),
        "key_prefix": row["key_prefix"],
        "redeemed_at": now,
    }
    if issued_key:
        result["api_key"] = issued_key
    return result


def record_x402_settlement(
    db_path: str,
    *,
    tx_hash: str,
    network: str,
    endpoint: str,
    amount_usdc: float,
    payer: str = "",
) -> dict[str, Any]:
    """Persist a settled x402 payment event into SQLite."""
    now = _utc_now()
    tx_norm = tx_hash.strip()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO x402_settlements (tx_hash, network, endpoint, amount_usdc, payer, settled_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tx_norm, network, endpoint, float(amount_usdc), payer, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "tx_hash": tx_norm,
        "network": network,
        "endpoint": endpoint,
        "amount_usdc": round(float(amount_usdc), 6),
        "payer": payer,
        "settled_at": now,
    }


def get_public_stats(db_path: str) -> dict[str, Any]:
    """Return aggregate non-PII service usage and billing metrics."""
    with _connect(db_path) as conn:
        keys_row = conn.execute(
            "SELECT COUNT(*) AS total_keys, COALESCE(SUM(total_spent), 0) AS total_extractions FROM api_keys"
        ).fetchone()
        tx_row = conn.execute(
            "SELECT COUNT(*) AS total_redemptions, COALESCE(SUM(amount_usdc), 0.0) AS total_usdc FROM redeemed_txs"
        ).fetchone()
        x402_row = conn.execute(
            "SELECT COUNT(*) AS x402_count, COALESCE(SUM(amount_usdc), 0.0) AS x402_usdc FROM x402_settlements"
        ).fetchone()
    total_usdc_redeemed = round(float(tx_row["total_usdc"]) if tx_row else 0.0, 6)
    x402_usdc_settled = round(float(x402_row["x402_usdc"]) if x402_row else 0.0, 6)
    return {
        "total_api_keys": int(keys_row["total_keys"]) if keys_row else 0,
        "total_extractions_served": int(keys_row["total_extractions"]) if keys_row else 0,
        "total_redemptions": int(tx_row["total_redemptions"]) if tx_row else 0,
        "total_usdc_redeemed": total_usdc_redeemed,
        "x402_payments_count": int(x402_row["x402_count"]) if x402_row else 0,
        "x402_usdc_settled": x402_usdc_settled,
        "total_usdc_earned": round(total_usdc_redeemed + x402_usdc_settled, 6),
    }
