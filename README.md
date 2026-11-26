# Neon Developer API (`neon-api`)

Web-to-Clean-Markdown extraction API with permissionless **Base / Polygon USDC** billing and native **x402 v2** pay-per-call support.

- **Live Endpoint**: `https://neon-api.austneo.workers.dev`
- **USDC Receiver (Base / Polygon)**: `0xfBF41F92592B3FF315983E41f058C21A1e8B8d48`

## Live Marketplace Listings
- **PayAPI Market (Settlement-Verified)**: `https://payapi.market/api/neon-web-to-markdown-api`
- **402 Index**: `https://402index.io` (`neon-api.austneo.workers.dev`, domain verified, 3 paid endpoints)
- **x402scan / x402dash / true402.dev**: Listed and active

## Payment Options

1. **Native x402 v2 (No API Key Required)**
   - `GET https://neon-api.austneo.workers.dev/v1/x402/extract?url=https://example.com` (`$0.01 USDC`)
   - `POST https://neon-api.austneo.workers.dev/v1/x402/extract` (`$0.01 USDC`)
   - `GET https://neon-api.austneo.workers.dev/v1/x402/links?url=https://example.com` (`$0.005 USDC`)
   - `POST https://neon-api.austneo.workers.dev/v1/x402/extract/batch` (`$0.05 USDC`, up to 10 URLs)
   - Network: Base (`eip155:8453`, exact scheme). Returns HTTP `402` with base64 `PAYMENT-REQUIRED` header and Bazaar metadata; settles automatically via `PAYMENT-SIGNATURE`.

2. **Prepaid API Key + Base/Polygon USDC TxHash Auto-Redemption**
   - `1,000 credits per 1.00 USDC` (`$0.001 / URL`), plus `20` free trial credits (`POST /v1/keys/free`).

## Endpoints
- `GET /health` — Health check
- `GET /v1/stats` — Public aggregate usage & USDC redemption metrics
- `GET /llms.txt` & `GET /skill.md` — Agent-ready quickstart & skill specification
- `GET /.well-known/x402` — Machine-readable x402 service & Bazaar discovery manifest
- `GET /.well-known/mcp.json` — MCP server discovery manifest
- `GET /.well-known/agent.json` — A2A Agent Card
- `POST /mcp` — Streamable HTTP JSON-RPC 2.0 MCP server (`tools/list`, `tools/call`)
- `GET /v1/x402/extract?url=https://example.com` & `POST /v1/x402/extract` — Native x402 v2 pay-per-call extraction (`$0.01 USDC` on Base `eip155:8453`)
- `GET /v1/x402/links?url=https://example.com` — Native x402 v2 structured page link extraction (`$0.005 USDC` on Base `eip155:8453`)
- `POST /v1/x402/extract/batch` — Native x402 v2 concurrent batch extraction for up to 10 URLs (`$0.05 USDC` on Base `eip155:8453`)
- `GET /v1/billing/info` — Receiver address, supported chains (`base`, `polygon`), USDC contracts, EIP-681 URIs, and credit rate
- `POST /v1/keys/free` — Issue a 20-credit free trial API key
- `GET /v1/keys/me` — Check remaining credits (`X-API-Key` header)
- `POST /v1/billing/redeem` — Verify a Base/Polygon USDC transfer `tx_hash` via JSON-RPC and atomically credit an existing or new API key (replay-protected)
- `POST /v1/extract` — SSRF-safe URL fetch & HTML-to-Clean-Markdown extraction (`1 credit / request`, supports `max_chars` & `extract_links`)
- `POST /v1/extract/batch` — Concurrent extraction for up to 10 URLs (`1 credit / successful URL`)

## MCP Client Configuration
```json
{
  "mcpServers": {
    "neon-api": {
      "url": "https://neon-api.austneo.workers.dev/mcp"
    }
  }
}
```

## Run Locally
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## Run Tests
```bash
PYTHONPATH=. pytest -v tests
```
