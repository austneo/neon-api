"""FastAPI application for Neon Web-to-Markdown + USDC Auto-Redemption API."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .chain import TxVerificationError, verify_usdc_transfer
from .config import (
    CHAINS,
    CREDITS_PER_USDC,
    DB_PATH,
    FREE_KEYS_PER_IP_24H,
    FREE_TRIAL_CREDITS,
    INDEX402_CLAIM_PATH,
    NOTIFY_SCRIPT_PATH,
    PERMANENT_PUBLIC_URL,
    RECEIVER_ADDRESS,
    RECEIVER_ADDRESS_CHECKSUM,
    REVENUE_LOG_PATH,
    X402_BATCH_PRICE_ATOMIC,
    X402_LINKS_PRICE_ATOMIC,
    X402_PRICE_ATOMIC,
)
from .x402 import (
    BASE_CAIP2,
    X402Error,
    X402FacilitatorUnavailable,
    X402PaymentRejected,
    build_batch_bazaar_spec,
    build_links_bazaar_spec,
    build_payment_required_payload,
    build_payment_requirements,
    build_well_known_x402_manifest,
    encode_b64_json,
    settle_payment,
    verify_payment,
)
from .db import (
    DuplicateTxError,
    FreeKeyRateLimitError,
    KeyNotFoundError,
    claim_free_api_key,
    deduct_credit,
    get_key_info,
    get_public_stats,
    init_db,
    record_redemption,
    record_x402_settlement,
)
from .extractor import ExtractionError, extract_url


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db(DB_PATH)
    yield


app = FastAPI(
    title="Neon Developer API",
    description="Web-to-Clean-Markdown extraction API with permissionless Base/Polygon USDC TxHash billing.",
    version="0.1.0",
    docs_url=None,
    lifespan=lifespan,
)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html() -> HTMLResponse:
    """Serve Swagger UI with explicit SEO title and meta description."""
    resp = get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title="Neon Developer API — Web-to-Markdown & x402 USDC API Docs",
    )
    body = resp.body.decode("utf-8")
    meta_tag = (
        '<meta name="description" content="Interactive OpenAPI documentation for Neon Developer API — '
        'SSRF-safe Web-to-Clean-Markdown extraction with permissionless Base/Polygon USDC billing and native x402 v2." />'
    )
    if "</head>" in body and 'name="description"' not in body:
        body = body.replace("</head>", f"    {meta_tag}\n    </head>", 1)
    return HTMLResponse(content=body, status_code=200)


class RedeemRequest(BaseModel):
    chain: str = Field(..., description="base or polygon")
    tx_hash: str = Field(..., description="0x-prefixed 64-hex transaction hash")
    api_key: str | None = Field(default=None, description="Optional existing key to top up")


class ExtractRequest(BaseModel):
    url: str = Field(..., description="Public http/https URL to extract")
    include_links: bool = Field(default=True, description="Preserve markdown hyperlinks")
    extract_links: bool = Field(default=False, description="Return deduplicated structured links list")
    max_chars: int | None = Field(default=None, ge=100, le=500000, description="Optional max markdown length")


class BatchExtractRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, max_length=10, description="Up to 10 public http/https URLs")
    include_links: bool = Field(default=True, description="Preserve markdown hyperlinks")
    extract_links: bool = Field(default=False, description="Return deduplicated structured links list")
    max_chars: int | None = Field(default=None, ge=100, le=500000, description="Optional max markdown length")


PUBLIC_URL_FILE = os.environ.get(
    "NEON_PUBLIC_URL_FILE",
    "/home/neon/workspace/state/neon_api_public_url.txt",
)


def _public_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).split(",")[0].strip()
    if host.endswith((".lhr.life", ".trycloudflare.com", ".workers.dev", ".pages.dev")):
        proto = "https"
    if host and not host.startswith(("127.0.0.1", "localhost")) and not host.endswith(".lhr.life"):
        return f"{proto}://{host}"
    if PERMANENT_PUBLIC_URL:
        return PERMANENT_PUBLIC_URL
    if host and not host.startswith(("127.0.0.1", "localhost")):
        return f"{proto}://{host}"
    if os.path.exists(PUBLIC_URL_FILE):
        try:
            pub = open(PUBLIC_URL_FILE, "r", encoding="utf-8").read().strip().rstrip("/")
            if pub.startswith(("http://", "https://")):
                return pub
        except Exception:
            pass
    if host:
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")


def _eip681_usdc_uris(amount_usdc: float = 1.0) -> dict[str, str]:
    raw_units = int(round(amount_usdc * 1_000_000))
    return {
        name: f"ethereum:{cfg.usdc_contracts[0]}@{cfg.chain_id}/transfer?address={RECEIVER_ADDRESS}&uint256={raw_units}"
        for name, cfg in CHAINS.items()
    }


def _client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _record_revenue_event(redemption: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(REVENUE_LOG_PATH), exist_ok=True)
        with open(REVENUE_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(redemption, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if os.environ.get("NEON_NOTIFY_ON_REDEEM", "1") == "1" and os.path.exists(NOTIFY_SCRIPT_PATH):
        if redemption.get("source") == "x402":
            msg = (
                f"💰 neon-api 收到 x402 实时结算入账！\n"
                f"- 网络：{redemption.get('network', BASE_CAIP2)}\n"
                f"- 金额：${redemption.get('amount_usdc')} USDC\n"
                f"- TxHash：{redemption.get('tx_hash')}"
            )
        else:
            msg = (
                f"💰 neon-api 收到链上 USDC 核销入账！\n"
                f"- 链：{redemption.get('chain')}\n"
                f"- 金额：${redemption.get('amount_usdc')} USDC (+{redemption.get('credits_added')} credits)\n"
                f"- TxHash：{redemption.get('tx_hash')}"
            )
        try:
            subprocess.Popen(
                ["python3", NOTIFY_SCRIPT_PATH, "--tg", "--text", msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def _payment_required_error(required_credits: int, available_credits: int) -> HTTPException:
    return HTTPException(
        status_code=402,
        headers={
            "X-Payment-Address": RECEIVER_ADDRESS,
            "X-Payment-Chains": ",".join(CHAINS.keys()),
            "X-Payment-Rate": f"{CREDITS_PER_USDC} credits / 1 USDC",
            "X-Payment-Redeem-Url": "/v1/billing/redeem",
        },
        detail={
            "error": "insufficient_credits",
            "message": "Insufficient credits. Top up via POST /v1/billing/redeem with Base/Polygon USDC.",
            "required_credits": required_credits,
            "available_credits": available_credits,
            "receiver_address": RECEIVER_ADDRESS,
            "rate": f"{CREDITS_PER_USDC} credits per 1.00 USDC (1 credit = 1 extract)",
            "chains": list(CHAINS.keys()),
            "payment_uris_1_usdc": _eip681_usdc_uris(1.0),
            "redeem_endpoint": "/v1/billing/redeem",
            "free_trial_endpoint": "/v1/keys/free",
        },
    )


def _require_key(x_api_key: str | None) -> str:
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header",
            headers={
                "X-Free-Trial-Url": "/v1/keys/free",
                "X-Billing-Info-Url": "/v1/billing/info",
            },
        )
    info = get_key_info(DB_PATH, x_api_key)
    if not info:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


@app.get("/", response_model=None)
async def root(request: Request) -> JSONResponse | HTMLResponse:
    accept = request.headers.get("accept", "")
    base = _public_base_url(request)
    if "text/html" in accept and "application/json" not in accept:
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Neon Developer API — Web-to-Markdown + USDC Billing</title>
  <meta name="description" content="SSRF-safe Web-to-Clean-Markdown extraction API for AI agents and developers. Permissionless Base/Polygon USDC billing and native x402 v2." />
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #111827; background: #f9fafb; }}
    .card {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 20px; margin: 16px 0; }}
    pre {{ background: #111827; color: #f3f4f6; padding: 14px; border-radius: 8px; overflow-x: auto; font-size: 13px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, monospace; }}
    .badge {{ display: inline-block; background: #dcfce7; color: #166534; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>Neon Developer API <span class="badge">v0.1.0 Online</span></h1>
  <p>SSRF-safe Web-to-Clean-Markdown extraction API for AI agents &amp; developers. Permissionless pay-as-you-go billing with <strong>Base / Polygon USDC</strong>.</p>
  <div class="card">
    <h3>Pricing &amp; USDC Receiver</h3>
    <ul>
      <li><strong>Free Trial:</strong> {FREE_TRIAL_CREDITS} credits (<code>POST /v1/keys/free</code>)</li>
      <li><strong>Paid Rate:</strong> {CREDITS_PER_USDC} credits per 1.00 USDC (<code>$0.001 / URL</code>)</li>
      <li><strong>Receiver (Base / Polygon):</strong> <code>{RECEIVER_ADDRESS}</code></li>
    </ul>
  </div>
  <div class="card">
    <h3>Quickstart</h3>
    <pre>curl -s -X POST {base}/v1/keys/free
curl -s -X POST {base}/v1/extract -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' -d '{{"url": "https://example.com"}}'
curl -s -X POST {base}/v1/billing/redeem -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' -d '{{"chain": "base", "tx_hash": "0x..."}}'</pre>
  </div>
  <p>Agent Specs: <a href="/llms.txt">/llms.txt</a> &middot; <a href="/skill.md">/skill.md</a> &middot; <a href="/.well-known/x402">/.well-known/x402</a> &middot; <a href="/.well-known/mcp.json">/.well-known/mcp.json</a> &middot; <a href="/.well-known/agent.json">/.well-known/agent.json</a> &middot; <a href="/docs">/docs</a></p>
</body>
</html>"""
        return HTMLResponse(content=html)

    return JSONResponse(
        {
            "service": "neon-api",
            "version": "0.1.0",
            "description": "Web-to-Clean-Markdown extraction API with permissionless Base/Polygon USDC billing.",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "llms_txt": "/llms.txt",
            "skill_md": "/skill.md",
            "well_known_x402": "/.well-known/x402",
            "well_known_mcp": "/.well-known/mcp.json",
            "agent_card": "/.well-known/agent.json",
            "endpoints": {
                "health": "GET /health",
                "stats": "GET /v1/stats",
                "billing_info": "GET /v1/billing/info",
                "free_trial_key": "POST /v1/keys/free",
                "key_status": "GET /v1/keys/me",
                "redeem_usdc": "POST /v1/billing/redeem",
                "extract": "POST /v1/extract",
                "extract_batch": "POST /v1/extract/batch",
                "x402_extract_get": "GET /v1/x402/extract?url=https://example.com",
                "x402_extract_post": "POST /v1/x402/extract",
                "x402_links_get": "GET /v1/x402/links?url=https://example.com",
                "x402_extract_batch_post": "POST /v1/x402/extract/batch",
                "mcp": "POST /mcp",
            },
        }
    )


@app.get("/.well-known/x402.json")
@app.get("/.well-known/x402")
async def well_known_x402(request: Request) -> dict[str, Any]:
    """Machine-readable x402 service discovery manifest."""
    return build_well_known_x402_manifest(_public_base_url(request))


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt() -> PlainTextResponse:
    """Allow search engines and AI agents to index public docs and discovery routes."""
    return PlainTextResponse("User-agent: *\nAllow: /\n", status_code=200)


@app.get("/.well-known/402index-verify.txt", response_class=PlainTextResponse)
async def well_known_402index_verify() -> PlainTextResponse:
    """Serve 402 Index domain ownership verification hash without redirect."""
    if not INDEX402_CLAIM_PATH or not os.path.exists(INDEX402_CLAIM_PATH):
        raise HTTPException(status_code=404, detail="402index verification hash not configured")
    try:
        with open(INDEX402_CLAIM_PATH, "r", encoding="utf-8") as fh:
            claim_data = json.load(fh)
        verification_hash = str(claim_data.get("verification_hash", "")).strip()
    except Exception as exc:
        raise HTTPException(status_code=404, detail="402index verification hash unavailable") from exc
    if not verification_hash:
        raise HTTPException(status_code=404, detail="402index verification hash not configured")
    return PlainTextResponse(content=verification_hash, status_code=200)


@app.get("/.well-known/mcp.json")
async def well_known_mcp(request: Request) -> dict[str, Any]:
    """Machine-readable MCP server discovery metadata."""
    base = _public_base_url(request)
    return {
        "name": "neon-api",
        "version": "0.1.0",
        "description": "SSRF-safe Web-to-Clean-Markdown MCP server with free trial keys and Base/Polygon USDC billing.",
        "url": f"{base}/mcp",
        "transport": "streamable-http",
        "protocolVersion": "2024-11-05",
        "documentationUrl": f"{base}/skill.md",
        "tools": MCP_TOOLS,
    }


@app.get("/.well-known/agent-card.json")
@app.get("/.well-known/agent.json")
async def agent_card(request: Request) -> dict[str, Any]:
    base = _public_base_url(request)
    return {
        "name": "Neon Developer API",
        "description": "SSRF-safe Web-to-Clean-Markdown extraction API with permissionless Base/Polygon USDC billing.",
        "url": base,
        "version": "0.1.0",
        "documentationUrl": f"{base}/skill.md",
        "llmsTxtUrl": f"{base}/llms.txt",
        "openapiUrl": f"{base}/openapi.json",
        "x402ManifestUrl": f"{base}/.well-known/x402",
        "mcpManifestUrl": f"{base}/.well-known/mcp.json",
        "authentication": {
            "schemes": ["apiKey", "x402"],
            "header": "X-API-Key",
            "x402Header": "PAYMENT-SIGNATURE",
            "freeTrialEndpoint": f"{base}/v1/keys/free",
        },
        "billing": {
            "currency": "USDC",
            "chains": list(CHAINS.keys()),
            "receiverAddress": RECEIVER_ADDRESS,
            "creditsPerUsdc": CREDITS_PER_USDC,
            "redeemEndpoint": f"{base}/v1/billing/redeem",
            "x402Network": BASE_CAIP2,
            "x402PriceUsdc": round(int(X402_PRICE_ATOMIC) / 1_000_000, 6),
            "x402LinksPriceUsdc": round(int(X402_LINKS_PRICE_ATOMIC) / 1_000_000, 6),
            "x402BatchPriceUsdc": round(int(X402_BATCH_PRICE_ATOMIC) / 1_000_000, 6),
            "paymentUris1Usdc": _eip681_usdc_uris(1.0),
        },
        "capabilities": {
            "extract": f"{base}/v1/extract",
            "extractBatch": f"{base}/v1/extract/batch",
            "x402Extract": f"{base}/v1/x402/extract",
            "x402Links": f"{base}/v1/x402/links",
            "x402ExtractBatch": f"{base}/v1/x402/extract/batch",
            "mcp": f"{base}/mcp",
        },
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "neon-api", "version": "0.1.0"}


@app.get("/v1/stats")
async def public_stats() -> dict[str, Any]:
    init_db(DB_PATH)
    stats = get_public_stats(DB_PATH)
    return {
        "service": "neon-api",
        "version": "0.1.0",
        "receiver_address": RECEIVER_ADDRESS,
        "chains": list(CHAINS.keys()),
        **stats,
    }


@app.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt(request: Request) -> str:
    base = _public_base_url(request)
    return (
        "# Neon Developer API\n\n"
        "Web-to-Clean-Markdown extraction API with permissionless Base/Polygon USDC billing.\n\n"
        "## Quickstart\n"
        f"1. Free trial ({FREE_TRIAL_CREDITS} credits): POST {base}/v1/keys/free\n"
        f"2. Check billing & receiver: GET {base}/v1/billing/info\n"
        f"3. Send USDC (Base or Polygon) to {RECEIVER_ADDRESS} (1 USDC = {CREDITS_PER_USDC} credits)\n"
        f"4. Redeem TxHash: POST {base}/v1/billing/redeem {{\"chain\": \"base\", \"tx_hash\": \"0x...\"}}\n"
        f"5. Extract Markdown: POST {base}/v1/extract with header X-API-Key and {{\"url\": \"https://...\"}}\n"
        f"6. Batch Extract (up to 10 URLs): POST {base}/v1/extract/batch with header X-API-Key and {{\"urls\": [\"https://...\"]}}\n"
        f"7. Pay-per-call x402 (no API key, $0.01 USDC on Base): GET {base}/v1/x402/extract?url=https://example.com or POST {base}/v1/x402/extract\n"
        f"8. Pay-per-call x402 links ($0.005 USDC on Base): GET {base}/v1/x402/links?url=https://example.com\n"
        f"9. Pay-per-call x402 batch ($0.05 USDC on Base, up to 10 URLs): POST {base}/v1/x402/extract/batch\n"
        f"10. Discovery manifests: GET {base}/.well-known/x402 | GET {base}/.well-known/mcp.json | GET {base}/openapi.json\n"
        f"11. MCP Server (JSON-RPC 2.0): POST {base}/mcp\n"
        f"12. Full agent integration guide: GET {base}/skill.md\n"
    )


@app.get("/skill.md", response_class=PlainTextResponse)
async def skill_md(request: Request) -> str:
    base = _public_base_url(request)
    return (
        "---\n"
        "name: neon-extract-api\n"
        "description: SSRF-safe Web-to-Clean-Markdown extraction API with permissionless Base/Polygon USDC billing and native x402 v2.\n"
        "---\n\n"
        "# Neon Developer API Skill\n\n"
        "Convert any public webpage into clean, LLM-ready Markdown. Pay-as-you-go with Base or Polygon USDC (no OAuth, no credit card).\n\n"
        "## Pricing\n"
        f"- **Free trial**: {FREE_TRIAL_CREDITS} credits (`POST {base}/v1/keys/free`)\n"
        f"- **Prepaid API key**: {CREDITS_PER_USDC} credits per 1.00 USDC (`$0.001 / URL`)\n"
        f"- **Native x402 v2 (no API key, Base `{BASE_CAIP2}`)**:\n"
        f"  - `GET/POST {base}/v1/x402/extract` — `$0.01 USDC` per URL\n"
        f"  - `GET {base}/v1/x402/links` — `$0.005 USDC` per URL\n"
        f"  - `POST {base}/v1/x402/extract/batch` — `$0.05 USDC` for up to 10 URLs\n"
        f"- **USDC Receiver (Base / Polygon)**: `{RECEIVER_ADDRESS}`\n\n"
        "## Usage\n"
        "```bash\n"
        f"# 1. Get a free trial key ({FREE_TRIAL_CREDITS} credits)\n"
        f"curl -s -X POST {base}/v1/keys/free\n\n"
        "# 2. Extract a single URL (1 credit)\n"
        f"curl -s -X POST {base}/v1/extract -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \\\n"
        "  -d '{\"url\": \"https://example.com\"}'\n\n"
        "# 3. Batch extract up to 10 URLs (1 credit per successful URL)\n"
        f"curl -s -X POST {base}/v1/extract/batch -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \\\n"
        "  -d '{\"urls\": [\"https://example.com\", \"https://iana.org\"]}'\n\n"
        "# 4. Top up with Base or Polygon USDC TxHash\n"
        f"curl -s -X POST {base}/v1/billing/redeem -H 'X-API-Key: YOUR_KEY' -H 'Content-Type: application/json' \\\n"
        "  -d '{\"chain\": \"base\", \"tx_hash\": \"0x...\"}'\n"
        "```\n"
    )


@app.get("/v1/billing/info")
async def billing_info() -> dict[str, Any]:
    return {
        "receiver_address": RECEIVER_ADDRESS,
        "rate": f"{CREDITS_PER_USDC} credits per 1.00 USDC (1 credit = 1 extract)",
        "free_trial_credits": FREE_TRIAL_CREDITS,
        "payment_uris_1_usdc": _eip681_usdc_uris(1.0),
        "chains": {
            name: {
                "chain_id": cfg.chain_id,
                "usdc_contracts": list(cfg.usdc_contracts),
                "min_confirmations": cfg.min_confirmations,
            }
            for name, cfg in CHAINS.items()
        },
    }


@app.post("/v1/keys/free")
async def issue_free_key(request: Request) -> dict[str, Any]:
    init_db(DB_PATH)
    try:
        return claim_free_api_key(
            DB_PATH,
            client_ip=_client_ip(request),
            credits=FREE_TRIAL_CREDITS,
            max_per_24h=FREE_KEYS_PER_IP_24H,
        )
    except FreeKeyRateLimitError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "free_trial_rate_limited",
                "message": str(exc),
                "receiver_address": RECEIVER_ADDRESS,
                "rate": f"{CREDITS_PER_USDC} credits per 1.00 USDC (1 credit = 1 extract)",
                "chains": list(CHAINS.keys()),
                "redeem_endpoint": "/v1/billing/redeem",
            },
        ) from exc


@app.get("/v1/keys/me")
async def key_status(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    raw_key = _require_key(x_api_key)
    info = get_key_info(DB_PATH, raw_key)
    assert info is not None
    return info


@app.post("/v1/billing/redeem")
async def redeem_usdc(
    req: RedeemRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    init_db(DB_PATH)
    target_key = req.api_key or x_api_key
    try:
        verified = await verify_usdc_transfer(req.chain, req.tx_hash)
    except TxVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = record_redemption(
            DB_PATH,
            tx_hash=verified.tx_hash,
            chain=verified.chain,
            amount_usdc=verified.amount_usdc,
            credits_added=verified.credits,
            raw_key=target_key,
        )
    except DuplicateTxError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    _record_revenue_event(result)
    return result


@app.post("/v1/extract")
async def extract_endpoint(
    req: ExtractRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    raw_key = _require_key(x_api_key)
    info = get_key_info(DB_PATH, raw_key)
    if not info or int(info["credits"]) < 1:
        raise _payment_required_error(1, int(info["credits"]) if info else 0)

    extract_kwargs: dict[str, Any] = {"include_links": req.include_links}
    if req.extract_links:
        extract_kwargs["extract_links"] = True
    if req.max_chars is not None:
        extract_kwargs["max_chars"] = req.max_chars

    try:
        doc = await extract_url(req.url, **extract_kwargs)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {type(exc).__name__}") from exc

    remaining = deduct_credit(DB_PATH, raw_key, amount=1)
    if remaining is None:
        raise _payment_required_error(1, 0)

    payload: dict[str, Any] = {
        "url": doc.url,
        "title": doc.title,
        "description": doc.description,
        "markdown": doc.markdown,
        "word_count": doc.word_count,
        "char_count": doc.char_count,
        "credits_remaining": remaining,
    }
    if req.extract_links:
        payload["links"] = list(doc.links)
    return payload


@app.post("/v1/extract/batch")
async def extract_batch_endpoint(
    req: BatchExtractRequest,
    x_api_key: str | None = Header(default=None),
) -> dict[str, Any]:
    raw_key = _require_key(x_api_key)
    info = get_key_info(DB_PATH, raw_key)
    available = int(info["credits"]) if info else 0
    if available < len(req.urls):
        raise _payment_required_error(len(req.urls), available)

    async def _fetch_one(target_url: str) -> dict[str, Any]:
        extract_kwargs: dict[str, Any] = {"include_links": req.include_links}
        if req.extract_links:
            extract_kwargs["extract_links"] = True
        if req.max_chars is not None:
            extract_kwargs["max_chars"] = req.max_chars
        try:
            doc = await extract_url(target_url, **extract_kwargs)
            item: dict[str, Any] = {
                "ok": True,
                "url": doc.url,
                "title": doc.title,
                "description": doc.description,
                "markdown": doc.markdown,
                "word_count": doc.word_count,
                "char_count": doc.char_count,
            }
            if req.extract_links:
                item["links"] = list(doc.links)
            return item
        except ExtractionError as exc:
            return {"ok": False, "url": target_url, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "url": target_url, "error": f"Upstream fetch failed: {type(exc).__name__}"}

    results = await asyncio.gather(*(_fetch_one(u) for u in req.urls))
    succeeded = sum(1 for r in results if r["ok"])
    remaining = available
    if succeeded > 0:
        deducted = deduct_credit(DB_PATH, raw_key, amount=succeeded)
        if deducted is None:
            raise _payment_required_error(succeeded, available)
        remaining = deducted

    return {
        "total": len(req.urls),
        "succeeded": succeeded,
        "failed": len(req.urls) - succeeded,
        "credits_charged": succeeded,
        "credits_remaining": remaining,
        "results": results,
    }


def _x402_resource_url(request: Request, path: str) -> str:
    base = _public_base_url(request)
    query = request.url.query
    return f"{base}{path}?{query}" if query else f"{base}{path}"


def _x402_402_response(
    resource_url: str,
    description: str,
    method: str,
    error: str = "PAYMENT-SIGNATURE header is required",
    *,
    amount_atomic: str = X402_PRICE_ATOMIC,
    bazaar_spec: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> JSONResponse:
    spec_kwargs: dict[str, Any] = {}
    if bazaar_spec is not None:
        spec_kwargs["input_info"], spec_kwargs["input_schema"], spec_kwargs["output_example"] = bazaar_spec
    if tags is not None:
        spec_kwargs["tags"] = tags
    payload = build_payment_required_payload(
        resource_url=resource_url,
        description=description,
        method=method,
        error=error,
        amount_atomic=amount_atomic,
        **spec_kwargs,
    )
    return JSONResponse(
        status_code=402,
        content=payload,
        headers={
            "PAYMENT-REQUIRED": encode_b64_json(payload),
            "Cache-Control": "no-store",
        },
    )


async def _verify_x402_or_challenge(
    request: Request,
    *,
    path: str,
    method: str,
    description: str,
    amount_atomic: str = X402_PRICE_ATOMIC,
    bazaar_spec: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, JSONResponse | None]:
    resource_url = _x402_resource_url(request, path)
    payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
    if not payment_header:
        return None, None, _x402_402_response(
            resource_url,
            description,
            method,
            amount_atomic=amount_atomic,
            bazaar_spec=bazaar_spec,
            tags=tags,
        )

    requirements = build_payment_requirements(amount_atomic=amount_atomic)
    try:
        verified = await verify_payment(
            payment_header,
            requirements,
            resource_url=resource_url,
            description=description,
        )
    except (X402Error, X402PaymentRejected) as exc:
        return None, None, _x402_402_response(
            resource_url,
            description,
            method,
            error=str(exc),
            amount_atomic=amount_atomic,
            bazaar_spec=bazaar_spec,
            tags=tags,
        )
    except X402FacilitatorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return verified, verified.get("effective_requirements", requirements), None


async def _settle_and_build_x402_response(
    request: Request,
    *,
    path: str,
    method: str,
    description: str,
    verified: dict[str, Any],
    requirements: dict[str, Any],
    amount_atomic: str,
    body: dict[str, Any],
    bazaar_spec: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> JSONResponse:
    resource_url = _x402_resource_url(request, path)
    try:
        settlement = await settle_payment(
            verified["facilitator_url"],
            verified["x402_version"],
            verified["payment_payload"],
            requirements,
        )
    except X402PaymentRejected as exc:
        return _x402_402_response(
            resource_url,
            description,
            method,
            error=str(exc),
            amount_atomic=amount_atomic,
            bazaar_spec=bazaar_spec,
            tags=tags,
        )

    amount_usdc = round(int(amount_atomic) / 1_000_000, 6)
    payer = settlement.get("payer", "") or verified.get("payer", "")
    tx_hash = settlement.get("transaction", "")
    settled_network = settlement.get("network", BASE_CAIP2)
    try:
        init_db(DB_PATH)
        record_x402_settlement(
            DB_PATH,
            tx_hash=tx_hash,
            network=settled_network,
            endpoint=path,
            amount_usdc=amount_usdc,
            payer=payer,
        )
    except Exception:
        pass
    _record_revenue_event(
        {
            "source": "x402",
            "chain": "base",
            "network": settled_network,
            "amount_usdc": amount_usdc,
            "credits_added": 0,
            "tx_hash": tx_hash,
            "payer": payer,
        }
    )

    body["payment"] = {
        "protocol": "x402",
        "network": settlement.get("network", BASE_CAIP2),
        "amount_usdc": amount_usdc,
        "transaction": settlement.get("transaction", ""),
        "payer": payer,
    }
    encoded_settlement = encode_b64_json(settlement)
    return JSONResponse(
        status_code=200,
        content=body,
        headers={
            "PAYMENT-RESPONSE": encoded_settlement,
            "X-PAYMENT-RESPONSE": encoded_settlement,
        },
    )


async def _handle_x402_extract(
    request: Request,
    *,
    method: str,
    url: str,
    include_links: bool,
    extract_links: bool,
    max_chars: int | None,
) -> JSONResponse:
    description = (
        "SSRF-safe Web-to-Clean-Markdown extraction for AI agents. "
        "Returns clean Markdown, title, metadata, word count, and optional links. "
        "$0.01 USDC per call on Base via x402."
    )
    verified, requirements, challenge = await _verify_x402_or_challenge(
        request,
        path="/v1/x402/extract",
        method=method,
        description=description,
        amount_atomic=X402_PRICE_ATOMIC,
    )
    if challenge is not None:
        return challenge
    assert verified is not None and requirements is not None

    extract_kwargs: dict[str, Any] = {"include_links": include_links}
    if extract_links:
        extract_kwargs["extract_links"] = True
    if max_chars is not None:
        extract_kwargs["max_chars"] = max_chars

    try:
        doc = await extract_url(url, **extract_kwargs)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {type(exc).__name__}") from exc

    body: dict[str, Any] = {
        "url": doc.url,
        "title": doc.title,
        "description": doc.description,
        "markdown": doc.markdown,
        "word_count": doc.word_count,
        "char_count": doc.char_count,
    }
    if extract_links:
        body["links"] = list(doc.links)

    return await _settle_and_build_x402_response(
        request,
        path="/v1/x402/extract",
        method=method,
        description=description,
        verified=verified,
        requirements=requirements,
        amount_atomic=X402_PRICE_ATOMIC,
        body=body,
    )


def _x402_head_402_response(
    request: Request,
    *,
    path: str,
    description: str,
    amount_atomic: str,
    bazaar_spec: tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None = None,
    tags: list[str] | None = None,
) -> PlainTextResponse:
    challenge = _x402_402_response(
        _x402_resource_url(request, path),
        description,
        "GET",
        amount_atomic=amount_atomic,
        bazaar_spec=bazaar_spec,
        tags=tags,
    )
    return PlainTextResponse(
        content="",
        status_code=402,
        headers={
            "PAYMENT-REQUIRED": challenge.headers["PAYMENT-REQUIRED"],
            "Cache-Control": "no-store",
        },
    )


@app.head("/v1/x402/extract")
async def x402_extract_head(request: Request) -> PlainTextResponse:
    """HEAD probe support for x402 Web-to-Markdown endpoint (returns 402 + PAYMENT-REQUIRED)."""
    return _x402_head_402_response(
        request,
        path="/v1/x402/extract",
        description=(
            "SSRF-safe Web-to-Clean-Markdown extraction for AI agents. "
            "Returns clean Markdown, title, metadata, word count, and optional links. "
            "$0.01 USDC per call on Base via x402."
        ),
        amount_atomic=X402_PRICE_ATOMIC,
    )


@app.get("/v1/x402/extract")
async def x402_extract_get(
    request: Request,
    url: str = "https://example.com",
    include_links: bool = True,
    extract_links: bool = False,
    max_chars: int | None = None,
) -> JSONResponse:
    """Pay-per-call x402 v2 Web-to-Markdown endpoint (GET)."""
    return await _handle_x402_extract(
        request,
        method="GET",
        url=url,
        include_links=include_links,
        extract_links=extract_links,
        max_chars=max_chars,
    )


@app.post("/v1/x402/extract")
async def x402_extract_post(request: Request) -> JSONResponse:
    """Pay-per-call x402 v2 Web-to-Markdown endpoint (POST)."""
    try:
        raw_body = await request.json()
        body = raw_body if isinstance(raw_body, dict) else {}
    except Exception:
        body = {}

    raw_max_chars = body.get("max_chars")
    max_chars: int | None = None
    if raw_max_chars is not None:
        try:
            max_chars = int(raw_max_chars)
        except (TypeError, ValueError):
            max_chars = None

    return await _handle_x402_extract(
        request,
        method="POST",
        url=str(body.get("url") or "https://example.com"),
        include_links=bool(body.get("include_links", True)),
        extract_links=bool(body.get("extract_links", False)),
        max_chars=max_chars,
    )


async def _handle_x402_links(
    request: Request,
    *,
    method: str,
    url: str,
) -> JSONResponse:
    description = (
        "SSRF-safe structured outbound link extraction for AI agents. "
        "Returns deduplicated absolute HTTP/HTTPS links with anchor text. "
        "$0.005 USDC per call on Base via x402."
    )
    bazaar_spec = build_links_bazaar_spec()
    tags = ["links", "crawler", "web", "scraping", "llm", "agent"]
    verified, requirements, challenge = await _verify_x402_or_challenge(
        request,
        path="/v1/x402/links",
        method=method,
        description=description,
        amount_atomic=X402_LINKS_PRICE_ATOMIC,
        bazaar_spec=bazaar_spec,
        tags=tags,
    )
    if challenge is not None:
        return challenge
    assert verified is not None and requirements is not None

    try:
        doc = await extract_url(url, include_links=True, extract_links=True)
    except ExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {type(exc).__name__}") from exc

    links = list(doc.links)
    return await _settle_and_build_x402_response(
        request,
        path="/v1/x402/links",
        method=method,
        description=description,
        verified=verified,
        requirements=requirements,
        amount_atomic=X402_LINKS_PRICE_ATOMIC,
        body={
            "url": doc.url,
            "title": doc.title,
            "description": doc.description,
            "link_count": len(links),
            "links": links,
        },
        bazaar_spec=bazaar_spec,
        tags=tags,
    )


@app.head("/v1/x402/links")
async def x402_links_head(request: Request) -> PlainTextResponse:
    """HEAD probe support for x402 outbound link extractor (returns 402 + PAYMENT-REQUIRED)."""
    return _x402_head_402_response(
        request,
        path="/v1/x402/links",
        description=(
            "SSRF-safe structured outbound link extraction for AI agents. "
            "Returns deduplicated absolute HTTP/HTTPS links with anchor text. "
            "$0.005 USDC per call on Base via x402."
        ),
        amount_atomic=X402_LINKS_PRICE_ATOMIC,
        bazaar_spec=build_links_bazaar_spec(),
        tags=["links", "crawler", "web", "scraping", "llm", "agent"],
    )


@app.get("/v1/x402/links")
async def x402_links_get(
    request: Request,
    url: str = "https://example.com",
) -> JSONResponse:
    """Pay-per-call x402 v2 structured outbound link extractor ($0.005 USDC on Base, GET)."""
    return await _handle_x402_links(request, method="GET", url=url)


@app.post("/v1/x402/links")
async def x402_links_post(
    request: Request,
    url: str = "https://example.com",
) -> JSONResponse:
    """Pay-per-call x402 v2 structured outbound link extractor ($0.005 USDC on Base, POST)."""
    try:
        raw_body = await request.json()
        body = raw_body if isinstance(raw_body, dict) else {}
    except Exception:
        body = {}
    target_url = str(body.get("url") or url or "https://example.com")
    return await _handle_x402_links(request, method="POST", url=target_url)


@app.post("/v1/x402/extract/batch")
async def x402_extract_batch_post(request: Request) -> JSONResponse:
    """Pay-per-call x402 v2 concurrent batch extraction for up to 10 URLs ($0.05 USDC on Base)."""
    description = (
        "SSRF-safe concurrent Web-to-Clean-Markdown extraction for up to 10 URLs. "
        "$0.05 USDC per batch call on Base via x402."
    )
    bazaar_spec = build_batch_bazaar_spec()
    tags = ["batch", "markdown", "web", "extract", "llm", "agent"]
    verified, requirements, challenge = await _verify_x402_or_challenge(
        request,
        path="/v1/x402/extract/batch",
        method="POST",
        description=description,
        amount_atomic=X402_BATCH_PRICE_ATOMIC,
        bazaar_spec=bazaar_spec,
        tags=tags,
    )
    if challenge is not None:
        return challenge
    assert verified is not None and requirements is not None

    try:
        raw_body = await request.json()
        body = raw_body if isinstance(raw_body, dict) else {}
    except Exception:
        body = {}

    urls = body.get("urls")
    if not isinstance(urls, list) or not (1 <= len(urls) <= 10) or not all(isinstance(u, str) and u.strip() for u in urls):
        raise HTTPException(status_code=400, detail="Body 'urls' must be a list of 1 to 10 non-empty URL strings")

    include_links = bool(body.get("include_links", True))
    extract_links = bool(body.get("extract_links", False))
    raw_max_chars = body.get("max_chars")
    max_chars: int | None = None
    if raw_max_chars is not None:
        try:
            parsed_max = int(raw_max_chars)
            if 100 <= parsed_max <= 500000:
                max_chars = parsed_max
        except (TypeError, ValueError):
            max_chars = None

    async def _fetch_one(target_url: str) -> dict[str, Any]:
        extract_kwargs: dict[str, Any] = {"include_links": include_links}
        if extract_links:
            extract_kwargs["extract_links"] = True
        if max_chars is not None:
            extract_kwargs["max_chars"] = max_chars
        try:
            doc = await extract_url(target_url, **extract_kwargs)
            item: dict[str, Any] = {
                "ok": True,
                "url": doc.url,
                "title": doc.title,
                "description": doc.description,
                "markdown": doc.markdown,
                "word_count": doc.word_count,
                "char_count": doc.char_count,
            }
            if extract_links:
                item["links"] = list(doc.links)
            return item
        except ExtractionError as exc:
            return {"ok": False, "url": target_url, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "url": target_url, "error": f"Upstream fetch failed: {type(exc).__name__}"}

    results = await asyncio.gather(*(_fetch_one(u) for u in urls))
    succeeded = sum(1 for r in results if r["ok"])
    if succeeded == 0:
        raise HTTPException(status_code=400, detail={"error": "all_urls_failed", "results": results})

    return await _settle_and_build_x402_response(
        request,
        path="/v1/x402/extract/batch",
        method="POST",
        description=description,
        verified=verified,
        requirements=requirements,
        amount_atomic=X402_BATCH_PRICE_ATOMIC,
        body={
            "total": len(urls),
            "succeeded": succeeded,
            "failed": len(urls) - succeeded,
            "results": results,
        },
        bazaar_spec=bazaar_spec,
        tags=tags,
    )


MCP_TOOLS = [
    {
        "name": "claim_free_key",
        "description": "Claim a free trial API key (20 credits, IP rate-limited to 3 per 24h).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "key_status",
        "description": "Check remaining credits and usage stats for an API key.",
        "inputSchema": {
            "type": "object",
            "properties": {"api_key": {"type": "string"}},
        },
    },
    {
        "name": "billing_info",
        "description": "Get Neon API USDC receiver address, supported chains (base/polygon), EIP-681 payment URIs, and credit rate.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "redeem_usdc",
        "description": "Verify a Base or Polygon USDC transfer tx_hash and credit an existing or new API key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chain": {"type": "string", "enum": ["base", "polygon"]},
                "tx_hash": {"type": "string"},
                "api_key": {"type": "string"},
            },
            "required": ["chain", "tx_hash"],
        },
    },
    {
        "name": "extract_markdown",
        "description": "Fetch a public URL (SSRF-safe) and extract clean LLM-ready Markdown (1 credit).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "api_key": {"type": "string"},
                "include_links": {"type": "boolean", "default": True},
                "extract_links": {"type": "boolean", "default": False},
                "max_chars": {"type": "integer", "minimum": 100, "maximum": 500000},
            },
            "required": ["url"],
        },
    },
]


def _mcp_ok(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _mcp_err(req_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


@app.post("/mcp")
async def mcp_endpoint(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> JSONResponse:
    """Streamable HTTP JSON-RPC 2.0 MCP server endpoint."""
    try:
        body = await request.json()
    except Exception:
        return _mcp_err(None, -32700, "Parse error")

    if not isinstance(body, dict):
        return _mcp_err(None, -32600, "Invalid Request")

    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method == "initialize":
        return _mcp_ok(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "neon-api", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        )
    if method == "notifications/initialized":
        return _mcp_ok(req_id, {})
    if method == "tools/list":
        return _mcp_ok(req_id, {"tools": MCP_TOOLS})
    if method != "tools/call":
        return _mcp_err(req_id, -32601, f"Method not found: {method}")

    tool_name = params.get("name", "")
    args = params.get("arguments") or {}

    if tool_name == "claim_free_key":
        try:
            data = await issue_free_key(request)
            return _mcp_ok(req_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]})
        except HTTPException as exc:
            return _mcp_err(req_id, -32000, json.dumps(exc.detail, ensure_ascii=False) if isinstance(exc.detail, dict) else str(exc.detail))

    if tool_name == "key_status":
        effective_key = args.get("api_key") or x_api_key
        try:
            data = await key_status(x_api_key=effective_key)
            return _mcp_ok(req_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]})
        except HTTPException as exc:
            return _mcp_err(req_id, -32000, str(exc.detail))

    if tool_name == "billing_info":
        data = await billing_info()
        return _mcp_ok(req_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]})

    if tool_name == "redeem_usdc":
        try:
            redeem_req = RedeemRequest(
                chain=str(args.get("chain", "")),
                tx_hash=str(args.get("tx_hash", "")),
                api_key=args.get("api_key") or x_api_key,
            )
            data = await redeem_usdc(redeem_req, x_api_key=x_api_key)
            return _mcp_ok(req_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]})
        except HTTPException as exc:
            return _mcp_err(req_id, -32000, str(exc.detail))

    if tool_name == "extract_markdown":
        effective_key = args.get("api_key") or x_api_key
        try:
            ext_req = ExtractRequest(
                url=str(args.get("url", "")),
                include_links=bool(args.get("include_links", True)),
                extract_links=bool(args.get("extract_links", False)),
                max_chars=args.get("max_chars"),
            )
            data = await extract_endpoint(ext_req, x_api_key=effective_key)
            return _mcp_ok(req_id, {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]})
        except HTTPException as exc:
            return _mcp_err(req_id, -32000, json.dumps(exc.detail, ensure_ascii=False) if isinstance(exc.detail, dict) else str(exc.detail))

    return _mcp_err(req_id, -32602, f"Unknown tool: {tool_name}")


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["info"]["x-guidance"] = (
        "Neon Developer API extracts clean LLM-ready Markdown and structured outbound links from public URLs "
        "with SSRF protection. Pay-per-call x402 v2 endpoints on Base (eip155:8453 USDC): "
        "GET/POST /v1/x402/extract ($0.01), GET /v1/x402/links ($0.005), and POST /v1/x402/extract/batch "
        "($0.05 for up to 10 URLs). Prepaid API keys (20 free trial credits via POST /v1/keys/free, "
        "1,000 credits per 1 USDC via POST /v1/billing/redeem) and MCP (POST /mcp) are also available."
    )
    schema["servers"] = [{"url": PERMANENT_PUBLIC_URL or "https://neon-api.austneo.workers.dev"}]

    extract_200_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "markdown": {"type": "string"},
            "word_count": {"type": "integer"},
            "char_count": {"type": "integer"},
            "links": {"type": "array", "items": {"type": "object"}},
            "payment": {"type": "object"},
        },
        "required": ["url", "markdown", "word_count", "char_count", "payment"],
    }
    links_200_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "link_count": {"type": "integer"},
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "url": {"type": "string", "format": "uri"},
                    },
                    "required": ["text", "url"],
                },
            },
            "payment": {"type": "object"},
        },
        "required": ["url", "link_count", "links", "payment"],
    }
    batch_200_schema = {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            "succeeded": {"type": "integer"},
            "failed": {"type": "integer"},
            "results": {"type": "array", "items": {"type": "object"}},
            "payment": {"type": "object"},
        },
        "required": ["total", "succeeded", "failed", "results", "payment"],
    }

    paid_configs: dict[tuple[str, str], dict[str, Any]] = {
        ("/v1/x402/extract", "get"): {
            "amount": f"{int(X402_PRICE_ATOMIC) / 1_000_000:.6f}",
            "response_schema": extract_200_schema,
        },
        ("/v1/x402/extract", "post"): {
            "amount": f"{int(X402_PRICE_ATOMIC) / 1_000_000:.6f}",
            "response_schema": extract_200_schema,
            "request_body": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "format": "uri", "description": "Public http/https URL"},
                                "include_links": {"type": "boolean", "default": True},
                                "extract_links": {"type": "boolean", "default": False},
                                "max_chars": {"type": "integer", "minimum": 100, "maximum": 500000},
                            },
                            "required": ["url"],
                        }
                    }
                },
            },
        },
        ("/v1/x402/links", "get"): {
            "amount": f"{int(X402_LINKS_PRICE_ATOMIC) / 1_000_000:.6f}",
            "response_schema": links_200_schema,
        },
        ("/v1/x402/links", "post"): {
            "amount": f"{int(X402_LINKS_PRICE_ATOMIC) / 1_000_000:.6f}",
            "response_schema": links_200_schema,
            "request_body": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "format": "uri", "description": "Public http/https URL"},
                            },
                            "required": ["url"],
                        }
                    }
                },
            },
        },
        ("/v1/x402/extract/batch", "post"): {
            "amount": f"{int(X402_BATCH_PRICE_ATOMIC) / 1_000_000:.6f}",
            "response_schema": batch_200_schema,
            "request_body": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "urls": {
                                    "type": "array",
                                    "items": {"type": "string", "format": "uri"},
                                    "minItems": 1,
                                    "maxItems": 10,
                                },
                                "include_links": {"type": "boolean", "default": True},
                                "extract_links": {"type": "boolean", "default": False},
                                "max_chars": {"type": "integer", "minimum": 100, "maximum": 500000},
                            },
                            "required": ["urls"],
                        }
                    }
                },
            },
        },
    }

    http_methods = {"get", "post", "put", "patch", "delete"}
    for path, path_item in schema.get("paths", {}).items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            m = method.lower()
            if m not in http_methods or not isinstance(op, dict):
                continue
            cfg = paid_configs.get((path, m))
            if cfg is None:
                op["security"] = []
                continue
            op["x-payment-info"] = {
                "price": {"mode": "fixed", "currency": "USD", "amount": cfg["amount"]},
                "protocols": [{"x402": {}}],
            }
            if "request_body" in cfg:
                op["requestBody"] = cfg["request_body"]
            responses = op.setdefault("responses", {})
            responses["200"] = {
                "description": "Successful response",
                "content": {"application/json": {"schema": cfg["response_schema"]}},
            }
            responses["402"] = {"description": "Payment Required"}

    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]
