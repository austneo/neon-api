"""On-chain USDC Transfer verification via Base / Polygon JSON-RPC."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx

from .config import CHAINS, CREDITS_PER_USDC, RECEIVER_ADDRESS

TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class TxVerificationError(Exception):
    """Raised when an on-chain transaction cannot be verified as a valid USDC payment."""


@dataclass(frozen=True)
class VerifiedTransfer:
    chain: str
    tx_hash: str
    amount_usdc: float
    credits: int
    confirmations: int


RpcCaller = Callable[[str, str, list[Any]], Awaitable[Any]]


async def default_rpc_call(rpc_url: str, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(rpc_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    if "error" in data and data["error"]:
        raise TxVerificationError(f"RPC error: {data['error']}")
    return data.get("result")


async def _call_with_fallback(
    rpc_urls: tuple[str, ...],
    caller: RpcCaller,
    method: str,
    params: list[Any],
) -> Any:
    last_exc: Exception | None = None
    for url in rpc_urls:
        try:
            return await caller(url, method, params)
        except Exception as exc:
            last_exc = exc
            continue
    if isinstance(last_exc, TxVerificationError):
        raise last_exc
    raise TxVerificationError(f"All RPC endpoints failed for {method}: {last_exc}")


async def verify_usdc_transfer(
    chain: str,
    tx_hash: str,
    *,
    rpc_call: RpcCaller | None = None,
) -> VerifiedTransfer:
    chain_key = chain.strip().lower()
    if chain_key not in CHAINS:
        raise TxVerificationError(f"Unsupported chain '{chain}'. Supported: {', '.join(CHAINS)}")
    if not TX_HASH_RE.match(tx_hash.strip()):
        raise TxVerificationError("Invalid transaction hash format")

    cfg = CHAINS[chain_key]
    tx_norm = tx_hash.strip().lower()
    caller = rpc_call or default_rpc_call

    receipt = await _call_with_fallback(cfg.rpc_urls, caller, "eth_getTransactionReceipt", [tx_norm])
    if not receipt or not isinstance(receipt, dict):
        raise TxVerificationError("Transaction receipt not found (pending or unknown)")

    if str(receipt.get("status", "")).lower() != "0x1":
        raise TxVerificationError("Transaction failed on-chain (status != 0x1)")

    tx_block_hex = receipt.get("blockNumber")
    latest_block_hex = await _call_with_fallback(cfg.rpc_urls, caller, "eth_blockNumber", [])
    if not tx_block_hex or not latest_block_hex:
        raise TxVerificationError("Missing block number in RPC response")

    confirmations = int(latest_block_hex, 16) - int(tx_block_hex, 16) + 1
    if confirmations < cfg.min_confirmations:
        raise TxVerificationError(
            f"Insufficient confirmations ({confirmations}/{cfg.min_confirmations})"
        )

    target_addr_noprefix = RECEIVER_ADDRESS[2:].lower()
    raw_usdc_units = 0

    for log in receipt.get("logs", []):
        contract = str(log.get("address", "")).lower()
        if contract not in cfg.usdc_contracts:
            continue
        topics = [str(t).lower() for t in log.get("topics", [])]
        if len(topics) < 3 or topics[0] != TRANSFER_EVENT_TOPIC:
            continue
        to_topic = topics[2]
        if len(to_topic) == 66 and to_topic.endswith(target_addr_noprefix):
            data_hex = str(log.get("data", "0x0"))
            raw_usdc_units += int(data_hex, 16)

    if raw_usdc_units <= 0:
        raise TxVerificationError(
            f"No USDC Transfer to {RECEIVER_ADDRESS} found in transaction logs"
        )

    credits = (raw_usdc_units * CREDITS_PER_USDC) // 1_000_000
    if credits <= 0:
        raise TxVerificationError("Transfer amount too small (minimum 0.001 USDC)")

    amount_usdc = round(raw_usdc_units / 1_000_000, 6)
    return VerifiedTransfer(
        chain=chain_key,
        tx_hash=tx_norm,
        amount_usdc=amount_usdc,
        credits=credits,
        confirmations=confirmations,
    )
