"""x402 v2 protocol helpers and public facilitator verification/settlement."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Awaitable, Callable

import httpx

from .config import (
    RECEIVER_ADDRESS_CHECKSUM,
    X402_FACILITATOR_URLS,
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
        "maxAmountRequired": str(amount_atomic),
        "asset": BASE_USDC_CHECKSUM,
        "payTo": pay_to,
        "maxTimeoutSeconds": 120,
        "extra": {"name": "USD Coin", "version": "2"},
    }


def build_payment_required_payload(
    resource_url: str,
    description: str,
    method: str = "GET",
    error: str = "PAYMENT-SIGNATURE header is required",
    amount_atomic: str = X402_PRICE_ATOMIC,
    pay_to: str = RECEIVER_ADDRESS_CHECKSUM,
) -> dict[str, Any]:
    """Build spec-compliant x402 v2 PaymentRequired payload with Bazaar metadata."""
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

    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
            "serviceName": "Neon Web-to-Markdown API",
            "tags": ["markdown", "web", "extract", "scraping", "llm", "agent"],
        },
        "accepts": [build_payment_requirements(amount_atomic=amount_atomic, pay_to=pay_to)],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": input_info,
                    "output": {"type": "json", "example": output_example},
                },
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "input": input_schema,
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
) -> dict[str, Any]:
    """Verify a base64 PAYMENT-SIGNATURE header across configured facilitators."""
    payment_payload = decode_b64_json(payment_header)
    raw_ver = payment_payload.get("x402Version", 2)
    try:
        x402_version = int(raw_ver)
    except (TypeError, ValueError) as exc:
        raise X402Error("Invalid x402Version in payment payload") from exc
    payment_payload = {**payment_payload, "x402Version": x402_version}

    req_body = {
        "x402Version": x402_version,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }
    sender = post_fn or default_facilitator_post
    urls = facilitator_urls or X402_FACILITATOR_URLS
    errors: list[str] = []

    for fac_url in urls:
        endpoint = f"{fac_url.rstrip('/')}/verify"
        try:
            status_code, body = await sender(endpoint, req_body)
        except Exception as exc:
            errors.append(f"{fac_url}: {type(exc).__name__}")
            continue

        if status_code >= 500 or status_code in (401, 403, 404):
            errors.append(f"{fac_url}: HTTP {status_code}")
            continue

        if body.get("isValid") is True:
            return {
                "facilitator_url": fac_url,
                "x402_version": x402_version,
                "payment_payload": payment_payload,
                "payer": str(body.get("payer", "")),
            }

        if body.get("isValid") is False or status_code == 400:
            reason = str(body.get("invalidReason") or body.get("invalidMessage") or "payment_invalid")
            raise X402PaymentRejected(reason)

        errors.append(f"{fac_url}: unexpected response HTTP {status_code}")

    raise X402FacilitatorUnavailable(
        "All x402 facilitators unavailable: " + "; ".join(errors) if errors else "No x402 facilitators configured"
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
