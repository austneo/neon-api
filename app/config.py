"""Configuration for Neon Developer API."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ChainConfig:
    name: str
    chain_id: int
    rpc_urls: tuple[str, ...]
    usdc_contracts: tuple[str, ...]
    min_confirmations: int

    @property
    def rpc_url(self) -> str:
        return self.rpc_urls[0]


def _dedup_urls(*urls: str) -> tuple[str, ...]:
    seen: list[str] = []
    for u in urls:
        u_clean = u.strip()
        if u_clean and u_clean not in seen:
            seen.append(u_clean)
    return tuple(seen)


RECEIVER_ADDRESS_CHECKSUM = os.environ.get(
    "NEON_RECEIVER_ADDRESS",
    "0xfBF41F92592B3FF315983E41f058C21A1e8B8d48",
)
RECEIVER_ADDRESS = RECEIVER_ADDRESS_CHECKSUM.lower()

PERMANENT_PUBLIC_URL = os.environ.get("NEON_PERMANENT_PUBLIC_URL", "").strip().rstrip("/")
X402_PRICE_ATOMIC = os.environ.get("NEON_X402_PRICE_ATOMIC", "10000")  # $0.01 USDC (6 decimals)
X402_FACILITATOR_URLS = _dedup_urls(
    *(
        os.environ.get(
            "NEON_X402_FACILITATORS",
            "https://facilitator.openx402.ai,https://facilitator.payai.network,https://api.x402.unibase.com/v2",
        ).split(",")
    )
)

DB_PATH = os.environ.get(
    "NEON_API_DB_PATH",
    "/home/neon/workspace/state/neon_api.db",
)

CREDITS_PER_USDC = 1000  # 1 USDC = 1,000 credits ($0.001 / extract)
FREE_TRIAL_CREDITS = 20
FREE_KEYS_PER_IP_24H = int(os.environ.get("NEON_FREE_KEYS_PER_IP_24H", "3"))
REVENUE_LOG_PATH = os.environ.get(
    "NEON_REVENUE_LOG_PATH",
    "/home/neon/workspace/state/neon_api_revenue.jsonl",
)
NOTIFY_SCRIPT_PATH = os.environ.get(
    "NEON_NOTIFY_SCRIPT",
    "/home/neon/workspace/scripts/notify.py",
)

CHAINS: dict[str, ChainConfig] = {
    "base": ChainConfig(
        name="base",
        chain_id=8453,
        rpc_urls=_dedup_urls(
            os.environ.get("BASE_RPC_URL", "https://mainnet.base.org"),
            "https://base-rpc.publicnode.com",
            "https://1rpc.io/base",
        ),
        usdc_contracts=(
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # Native USDC on Base
        ),
        min_confirmations=1,
    ),
    "polygon": ChainConfig(
        name="polygon",
        chain_id=137,
        rpc_urls=_dedup_urls(
            os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com"),
            "https://1rpc.io/matic",
            "https://polygon.drpc.org",
        ),
        usdc_contracts=(
            "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",  # Native USDC on Polygon
            "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # Bridged USDC.e on Polygon
        ),
        min_confirmations=2,
    ),
}
