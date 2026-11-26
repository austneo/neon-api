"""FastAPI application for Neon Web-to-Markdown + USDC Auto-Redemption API."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .chain import TxVerificationError, verify_usdc_transfer
from .config import (
    CHAINS,
    CREDITS_PER_USDC,
    DB_PATH,
    FREE_KEYS_PER_IP_24H,
    FREE_TRIAL_CREDITS,
    NOTIFY_SCRIPT_PATH,
    PERMANENT_PUBLIC_URL,
    RECEIVER_ADDRESS,
    RECEIVER_ADDRESS_CHECKSUM,
    REVENUE_LOG_PATH,
    X402_PRICE_ATOMIC,
)
from .x402 import (
    BASE_CAIP2,
    X402Error,
    X402FacilitatorUnavailable,
    X402PaymentRejected,
    build_payment_required_payload,
    build_payment_requirements,
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
    lifespan=lifespan,
)


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
  <p>Agent Specs: <a href="/llms.txt">/llms.txt</a> &middot; <a href="/skill.md">/skill.md</a> &middot; <a href="/.well-known/agent.json">/.well-known/agent.json</a> &middot; <a href="/docs">/docs</a></p>
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
                "mcp": "POST /mcp",
            },
        }
    )


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
            "paymentUris1Usdc": _eip681_usdc_uris(1.0),
        },
        "capabilities": {
            "extract": f"{base}/v1/extract",
            "extractBatch": f"{base}/v1/extract/batch",
            "x402Extract": f"{base}/v1/x402/extract",
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
        f"8. MCP Server (JSON-RPC 2.0): POST {base}/mcp\n"
        f"9. Full agent integration guide: GET {base}/skill.md\n"
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
        f"- **Native x402 v2 (no API key)**: `$0.01 USDC` per call on Base (`{BASE_CAIP2}`) via `GET/POST {base}/v1/x402/extract`\n"
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
) -> JSONResponse:
    payload = build_payment_required_payload(
        resource_url=resource_url,
        description=description,
        method=method,
        error=error,
    )
    return JSONResponse(
        status_code=402,
        content=payload,
        headers={
            "PAYMENT-REQUIRED": encode_b64_json(payload),
            "Cache-Control": "no-store",
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
    resource_url = _x402_resource_url(request, "/v1/x402/extract")
    payment_header = request.headers.get("payment-signature") or request.headers.get("x-payment")
    if not payment_header:
        return _x402_402_response(resource_url, description, method)

    requirements = build_payment_requirements()
    try:
        verified = await verify_payment(payment_header, requirements)
    except (X402Error, X402PaymentRejected) as exc:
        return _x402_402_response(resource_url, description, method, error=str(exc))
    except X402FacilitatorUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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

    try:
        settlement = await settle_payment(
            verified["facilitator_url"],
            verified["x402_version"],
            verified["payment_payload"],
            requirements,
        )
    except X402PaymentRejected as exc:
        return _x402_402_response(resource_url, description, method, error=str(exc))

    amount_usdc = round(int(X402_PRICE_ATOMIC) / 1_000_000, 6)
    _record_revenue_event(
        {
            "source": "x402",
            "chain": "base",
            "network": settlement.get("network", BASE_CAIP2),
            "amount_usdc": amount_usdc,
            "credits_added": 0,
            "tx_hash": settlement.get("transaction", ""),
            "payer": settlement.get("payer", "") or verified.get("payer", ""),
        }
    )

    body: dict[str, Any] = {
        "url": doc.url,
        "title": doc.title,
        "description": doc.description,
        "markdown": doc.markdown,
        "word_count": doc.word_count,
        "char_count": doc.char_count,
        "payment": {
            "protocol": "x402",
            "network": settlement.get("network", BASE_CAIP2),
            "amount_usdc": amount_usdc,
            "transaction": settlement.get("transaction", ""),
            "payer": settlement.get("payer", "") or verified.get("payer", ""),
        },
    }
    if extract_links:
        body["links"] = list(doc.links)

    encoded_settlement = encode_b64_json(settlement)
    return JSONResponse(
        status_code=200,
        content=body,
        headers={
            "PAYMENT-RESPONSE": encoded_settlement,
            "X-PAYMENT-RESPONSE": encoded_settlement,
        },
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
