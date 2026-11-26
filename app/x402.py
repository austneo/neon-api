"""x402 v2 protocol helpers and public facilitator verification/settlement."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Awaitable, Callable

import httpx

from .config import (
    RECEIVER_ADDRESS_CHECKSUM,
    X402_BATCH_PRICE_ATOMIC,
    X402_FACILITATOR_URLS,
    X402_LINKS_PRICE_ATOMIC,
    X402_PRICE_ATOMIC,
)

BASE_CAIP2 = "eip155:8453"
BASE_USDC_CHECKSUM = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class X402Error(Exception):
    """Base error for malformed x402 payment payloads."""


class X402PaymentRejected(X402Error):
    """Raised when facilitator rejects a payment authorization or settlement."""


class X402FacilitatorUnavailable(Exception):
    """Raised when all configured x402 facilitators fail or are unreachable."""


FacilitatorPostFn = Callable[[str, dict[str, Any]], Awaitable[tuple[int, dict[str, Any]]]]


def encode_b64_json(obj: dict[str, Any]) -> str:
    """Serialize a dict as compact UTF-8 JSON and base64-encode it."""
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_b64_json(value: str) -> dict[str, Any]:
    """Decode a base64 or base64url JSON header into a dict."""
    cleaned = value.strip()
    if not cleaned:
        raise X402Error("Invalid base64 payment header")
    padded = cleaned + ("=" * (-len(cleaned) % 4))
    raw_bytes: bytes | None = None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw_bytes = decoder(padded, validate=False)
            break
        except (binascii.Error, ValueError):
            continue
    if raw_bytes is None:
        raise X402Error("Invalid base64 payment header")
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise X402Error("Invalid base64 payment header") from exc
    if not isinstance(parsed, dict):
        raise X402Error("Invalid base64 payment header")
    return parsed


def build_payment_requirements(
    amount_atomic: str = X402_PRICE_ATOMIC,
    pay_to: str = RECEIVER_ADDRESS_CHECKSUM,
) -> dict[str, Any]:
    """Build Base mainnet Circle USDC x402 exact payment requirements."""
    return {
        "scheme": "exact",
        "network": BASE_CAIP2,
        "amount": str(amount_atomic),
        "asset": BASE_USDC_CHECKSUM,
        "payTo": pay_to,
        "maxTimeoutSeconds": 120,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def adapt_requirements_for_version(
    requirements: dict[str, Any],
    x402_version: int,
    resource_url: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Normalize paymentRequirements to match x402 v1 ('base') or v2 ('eip155:8453') facilitator expectations."""
    if x402_version == 1:
        adapted: dict[str, Any] = {
            "scheme": requirements.get("scheme", "exact"),
            "network": "base",
            "maxAmountRequired": str(requirements.get("maxAmountRequired") or requirements.get("amount", X402_PRICE_ATOMIC)),
            "asset": requirements.get("asset", BASE_USDC_CHECKSUM),
            "payTo": requirements.get("payTo", RECEIVER_ADDRESS_CHECKSUM),
            "maxTimeoutSeconds": requirements.get("maxTimeoutSeconds", 120),
            "mimeType": "application/json",
            "extra": dict(requirements.get("extra") or {"name": "USD Coin", "version": "2"}),
        }
        if resource_url:
            adapted["resource"] = resource_url
        if description:
            adapted["description"] = description
        return adapted
    return {k: v for k, v in requirements.items() if k != "maxAmountRequired"}


def _default_bazaar_spec(method: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    upper_method = method.upper()
    if upper_method == "POST":
        input_info: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "body": {"url": "https://example.com"},
        }
        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "http"},
                "method": {"type": "string", "const": "POST"},
                "bodyType": {"type": "string", "const": "json"},
                "body": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri"},
                        "include_links": {"type": "boolean"},
                        "extract_links": {"type": "boolean"},
                        "max_chars": {"type": "integer", "minimum": 100, "maximum": 500000},
                    },
                    "required": ["url"],
                },
            },
            "required": ["type", "method"],
        }
    else:
        input_info = {
            "type": "http",
            "method": "GET",
            "queryParams": {"url": "https://example.com"},
        }
        input_schema = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "const": "http"},
                "method": {"type": "string", "enum": ["GET", "HEAD"]},
                "queryParams": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "format": "uri"},
                        "include_links": {"type": "boolean"},
                        "extract_links": {"type": "boolean"},
                        "max_chars": {"type": "integer", "minimum": 100, "maximum": 500000},
                    },
                    "required": ["url"],
                },
            },
            "required": ["type", "method"],
        }

    output_example = {
        "url": "https://example.com",
        "title": "Example Domain",
        "description": "",
        "markdown": "# Example Domain\n\n...",
        "word_count": 28,
        "char_count": 162,
        "payment": {
            "protocol": "x402",
            "network": BASE_CAIP2,
            "amount_usdc": 0.01,
            "transaction": "0x...",
        },
    }
    return input_info, input_schema, output_example


def build_links_bazaar_spec() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Bazaar input/output metadata for GET /v1/x402/links."""
    input_info: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "queryParams": {"url": "https://example.com"},
    }
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "enum": ["GET", "HEAD"]},
            "queryParams": {
                "type": "object",
                "properties": {"url": {"type": "string", "format": "uri"}},
                "required": ["url"],
            },
        },
        "required": ["type", "method"],
    }
    output_example = {
        "url": "https://example.com",
        "title": "Example Domain",
        "description": "",
        "link_count": 1,
        "links": [{"text": "More information...", "url": "https://www.iana.org/domains/example"}],
        "payment": {
            "protocol": "x402",
            "network": BASE_CAIP2,
            "amount_usdc": 0.005,
            "transaction": "0x...",
        },
    }
    return input_info, input_schema, output_example


def build_batch_bazaar_spec() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Bazaar input/output metadata for POST /v1/x402/extract/batch."""
    input_info: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "bodyType": "json",
        "body": {
            "urls": ["https://example.com"],
            "include_links": True,
            "extract_links": False,
            "max_chars": 20000,
        },
    }
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "const": "POST"},
            "bodyType": {"type": "string", "const": "json"},
            "body": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string", "format": "uri"},
                        "minItems": 1,
                        "maxItems": 10,
                    },
                    "include_links": {"type": "boolean"},
                    "extract_links": {"type": "boolean"},
                    "max_chars": {"type": "integer", "minimum": 100, "maximum": 500000},
                },
                "required": ["urls"],
            },
        },
        "required": ["type", "method"],
    }
    output_example = {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "results": [
            {
                "ok": True,
                "url": "https://example.com",
                "title": "Example Domain",
                "description": "",
                "markdown": "# Example Domain\n\n...",
                "word_count": 28,
                "char_count": 162,
            }
        ],
        "payment": {
            "protocol": "x402",
            "network": BASE_CAIP2,
            "amount_usdc": 0.05,
            "transaction": "0x...",
        },
    }
    return input_info, input_schema, output_example


def build_payment_required_payload(
    resource_url: str,
    description: str,
    method: str = "GET",
    error: str = "PAYMENT-SIGNATURE header is required",
    amount_atomic: str = X402_PRICE_ATOMIC,
    pay_to: str = RECEIVER_ADDRESS_CHECKSUM,
    input_info: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_example: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Build spec-compliant x402 v2 PaymentRequired payload with Bazaar metadata."""
    default_info, default_schema, default_output = _default_bazaar_spec(method)
    eff_input_info = input_info if input_info is not None else default_info
    eff_input_schema = input_schema if input_schema is not None else default_schema
    eff_output_example = output_example if output_example is not None else default_output
    eff_tags = tags if tags is not None else ["markdown", "web", "extract", "scraping", "llm", "agent"]

    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
            "serviceName": "Neon Web-to-Markdown API",
            "tags": eff_tags,
        },
        "accepts": [build_payment_requirements(amount_atomic=amount_atomic, pay_to=pay_to)],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": eff_input_info,
                    "output": {"type": "json", "example": eff_output_example},
                },
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "input": eff_input_schema,
                        "output": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "example": {"type": "object"},
                            },
                            "required": ["type"],
                        },
                    },
                    "required": ["input"],
                },
            }
        },
    }


def build_well_known_x402_manifest(base_url: str) -> dict[str, Any]:
    """Build /.well-known/x402 discovery manifest listing all paid x402 endpoints."""
    base = base_url.rstrip("/")
    ext_get = build_payment_required_payload(
        resource_url=f"{base}/v1/x402/extract",
        description="SSRF-safe Web-to-Clean-Markdown extraction (GET).",
        method="GET",
        amount_atomic=X402_PRICE_ATOMIC,
    )
    ext_post = build_payment_required_payload(
        resource_url=f"{base}/v1/x402/extract",
        description="SSRF-safe Web-to-Clean-Markdown extraction (POST).",
        method="POST",
        amount_atomic=X402_PRICE_ATOMIC,
    )
    links_in, links_schema, links_out = build_links_bazaar_spec()
    links_payload = build_payment_required_payload(
        resource_url=f"{base}/v1/x402/links",
        description="SSRF-safe structured outbound link extraction ($0.005 USDC).",
        method="GET",
        amount_atomic=X402_LINKS_PRICE_ATOMIC,
        input_info=links_in,
        input_schema=links_schema,
        output_example=links_out,
        tags=["links", "crawler", "web", "scraping", "llm", "agent"],
    )
    batch_in, batch_schema, batch_out = build_batch_bazaar_spec()
    batch_payload = build_payment_required_payload(
        resource_url=f"{base}/v1/x402/extract/batch",
        description="SSRF-safe concurrent batch Web-to-Markdown extraction for up to 10 URLs ($0.05 USDC).",
        method="POST",
        amount_atomic=X402_BATCH_PRICE_ATOMIC,
        input_info=batch_in,
        input_schema=batch_schema,
        output_example=batch_out,
        tags=["batch", "markdown", "web", "extract", "llm", "agent"],
    )

    entries = [
        ("/v1/x402/extract", "GET", ext_get, X402_PRICE_ATOMIC),
        ("/v1/x402/extract", "POST", ext_post, X402_PRICE_ATOMIC),
        ("/v1/x402/links", "GET", links_payload, X402_LINKS_PRICE_ATOMIC),
        ("/v1/x402/extract/batch", "POST", batch_payload, X402_BATCH_PRICE_ATOMIC),
    ]
    services = []
    for path, method, payload, atomic in entries:
        req = dict(payload["accepts"][0])
        req["priceUsd"] = round(int(atomic) / 1_000_000, 6)
        services.append(
            {
                "path": path,
                "method": method,
                "url": f"{base}{path}",
                "description": payload["resource"]["description"],
                "enabled": True,
                "payment": req,
                "extensions": payload["extensions"],
            }
        )
    return {
        "version": 1,
        "x402Version": 2,
        "service": "neon-api",
        "name": "Neon Web-to-Markdown API",
        "baseUrl": base,
        "networks": [BASE_CAIP2],
        "asset": BASE_USDC_CHECKSUM,
        "payTo": RECEIVER_ADDRESS_CHECKSUM,
        "payment_header": "PAYMENT-SIGNATURE",
        "challenge_header": "PAYMENT-REQUIRED",
        "receipt_header": "PAYMENT-RESPONSE",
        "resources": [
            f"{base}/v1/x402/extract",
            f"{base}/v1/x402/links",
            f"{base}/v1/x402/extract/batch",
        ],
        "services": services,
    }


def build_well_known_x402_service_manifest(base_url: str) -> dict[str, Any]:
    """Build /.well-known/x402-service.json manifest (true402 / x402-service v1.0)."""
    base = base_url.rstrip("/")
    extract_price = f"{int(X402_PRICE_ATOMIC) / 1_000_000:g}"
    links_price = f"{int(X402_LINKS_PRICE_ATOMIC) / 1_000_000:g}"
    batch_price = f"{int(X402_BATCH_PRICE_ATOMIC) / 1_000_000:g}"
    facilitator = X402_FACILITATOR_URLS[0] if X402_FACILITATOR_URLS else "https://facilitator.payai.network"
    return {
        "x402": "1.0",
        "name": "Neon Web-to-Markdown API",
        "description": (
            "SSRF-safe Web-to-Clean-Markdown extraction, structured outbound link extraction, "
            "and concurrent batch URL extraction (up to 10 URLs) for AI agents. "
            "Pay per call with Base USDC (x402 v2/v1)."
        ),
        "capabilities": ["web", "extract", "markdown", "links", "scraping", "batch", "readability"],
        "endpoint": f"{base}/v1/x402/extract",
        "pricing": {
            "currency": "USDC",
            "base": extract_price,
            "unit": "request",
        },
        "payment": {
            "address": RECEIVER_ADDRESS_CHECKSUM,
            "chain": "base",
            "facilitator": facilitator,
        },
        "services": [
            {
                "id": "extract",
                "name": "Neon Web-to-Markdown Extract",
                "capabilities": ["web", "extract", "markdown", "scraping", "readability"],
                "endpoint": f"{base}/v1/x402/extract",
                "pricing": {"currency": "USDC", "base": extract_price, "unit": "request"},
            },
            {
                "id": "links",
                "name": "Neon Outbound Link Extractor",
                "capabilities": ["web", "links", "crawler", "scraping"],
                "endpoint": f"{base}/v1/x402/links",
                "pricing": {"currency": "USDC", "base": links_price, "unit": "request"},
            },
            {
                "id": "extract-batch",
                "name": "Neon Batch Web-to-Markdown Extract",
                "capabilities": ["web", "extract", "markdown", "batch", "scraping"],
                "endpoint": f"{base}/v1/x402/extract/batch",
                "pricing": {"currency": "USDC", "base": batch_price, "unit": "batch"},
            },
        ],
    }


async def default_facilitator_post(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST JSON payload to an x402 facilitator endpoint."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, json=payload)
    try:
        data = resp.json()
    except Exception:
        data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


async def verify_payment(
    payment_header: str,
    requirements: dict[str, Any],
    facilitator_urls: tuple[str, ...] | None = None,
    post_fn: FacilitatorPostFn | None = None,
    resource_url: str = "",
    description: str = "",
) -> dict[str, Any]:
    """Verify a base64 PAYMENT-SIGNATURE / X-PAYMENT header across configured facilitators."""
    payment_payload = decode_b64_json(payment_header)
    raw_ver = payment_payload.get("x402Version", 2)
    try:
        x402_version = int(raw_ver)
    except (TypeError, ValueError) as exc:
        raise X402Error("Invalid x402Version in payment payload") from exc
    payment_payload = {**payment_payload, "x402Version": x402_version}
    effective_requirements = adapt_requirements_for_version(
        requirements,
        x402_version,
        resource_url=resource_url,
        description=description,
    )

    req_body = {
        "x402Version": x402_version,
        "paymentPayload": payment_payload,
        "paymentRequirements": effective_requirements,
    }
    sender = post_fn or default_facilitator_post
    urls = facilitator_urls or X402_FACILITATOR_URLS
    infra_errors: list[str] = []
    compat_errors: list[str] = []
    compat_keywords = (
        "unsupported",
        "version",
        "network",
        "not_registered",
        "not registered",
        "invalid_payment_requirements",
    )

    for fac_url in urls:
        endpoint = f"{fac_url.rstrip('/')}/verify"
        try:
            status_code, body = await sender(endpoint, req_body)
        except Exception as exc:
            infra_errors.append(f"{fac_url}: {type(exc).__name__}")
            continue

        if status_code >= 500 or status_code in (401, 403, 404):
            infra_errors.append(f"{fac_url}: HTTP {status_code}")
            continue

        if body.get("isValid") is True:
            return {
                "facilitator_url": fac_url,
                "x402_version": x402_version,
                "payment_payload": payment_payload,
                "payer": str(body.get("payer", "")),
                "effective_requirements": effective_requirements,
            }

        if body.get("isValid") is False or status_code == 400:
            reason = str(
                body.get("invalidReason")
                or body.get("invalidMessage")
                or body.get("error")
                or body.get("message")
                or "payment_invalid"
            )
            if any(kw in reason.lower() for kw in compat_keywords):
                compat_errors.append(f"{fac_url}: {reason}")
                continue
            raise X402PaymentRejected(reason)

        infra_errors.append(f"{fac_url}: unexpected response HTTP {status_code}")

    if compat_errors and not infra_errors:
        raise X402PaymentRejected("; ".join(compat_errors))

    all_errors = infra_errors + compat_errors
    raise X402FacilitatorUnavailable(
        "All x402 facilitators unavailable: " + "; ".join(all_errors) if all_errors else "No x402 facilitators configured"
    )


async def settle_payment(
    facilitator_url: str,
    x402_version: int,
    payment_payload: dict[str, Any],
    requirements: dict[str, Any],
    post_fn: FacilitatorPostFn | None = None,
) -> dict[str, Any]:
    """Settle a verified x402 payment via the chosen facilitator."""
    sender = post_fn or default_facilitator_post
    endpoint = f"{facilitator_url.rstrip('/')}/settle"
    req_body = {
        "x402Version": x402_version,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    try:
        status_code, body = await sender(endpoint, req_body)
    except Exception as exc:
        raise X402PaymentRejected(f"settlement_transport_error: {type(exc).__name__}") from exc

    if status_code == 200 and body.get("success") is True:
        return {
            "success": True,
            "transaction": str(body.get("transaction", "")),
            "network": str(body.get("network", requirements.get("network", BASE_CAIP2))),
            "payer": str(body.get("payer", "")),
        }

    reason = str(body.get("errorReason") or body.get("errorMessage") or f"settlement_failed_http_{status_code}")
    raise X402PaymentRejected(reason)
