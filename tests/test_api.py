"""Unit and integration tests for Neon Developer API."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import config, main
from app.chain import TRANSFER_EVENT_TOPIC, VerifiedTransfer
from app.extractor import ExtractedDocument


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_neon_api.db")
    rev_file = str(tmp_path / "test_revenue.jsonl")
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(main, "DB_PATH", db_file)
    monkeypatch.setattr(config, "REVENUE_LOG_PATH", rev_file)
    monkeypatch.setattr(main, "REVENUE_LOG_PATH", rev_file)
    monkeypatch.setattr(config, "PERMANENT_PUBLIC_URL", "")
    monkeypatch.setattr(main, "PERMANENT_PUBLIC_URL", "")
    monkeypatch.setenv("NEON_NOTIFY_ON_REDEEM", "0")
    with TestClient(main.app) as c:
        yield c


def _padded_addr(addr: str) -> str:
    return "0x" + ("0" * 24) + addr.lower().replace("0x", "")


def test_health_and_billing_info(client: TestClient):
    idx = client.get("/")
    assert idx.status_code == 200
    assert idx.json()["service"] == "neon-api"

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    llms = client.get("/llms.txt")
    assert llms.status_code == 200
    assert config.RECEIVER_ADDRESS in llms.text

    b = client.get("/v1/billing/info")
    assert b.status_code == 200
    data = b.json()
    assert data["receiver_address"] == config.RECEIVER_ADDRESS
    assert "base" in data["chains"]
    assert "polygon" in data["chains"]


def test_free_key_and_extract_flow(client: TestClient):
    r = client.post("/v1/keys/free")
    assert r.status_code == 200
    key = r.json()["api_key"]
    assert r.json()["credits"] == 20

    me = client.get("/v1/keys/me", headers={"X-API-Key": key})
    assert me.status_code == 200
    assert me.json()["credits"] == 20

    fake_doc = ExtractedDocument(
        url="https://example.com/article",
        title="Example Article",
        description="Test desc",
        markdown="# Example Article\n\nClean body text.",
        word_count=6,
        char_count=37,
    )
    with patch("app.main.extract_url", new=AsyncMock(return_value=fake_doc)):
        ex = client.post(
            "/v1/extract",
            headers={"X-API-Key": key},
            json={"url": "https://example.com/article"},
        )
    assert ex.status_code == 200
    body = ex.json()
    assert body["title"] == "Example Article"
    assert body["credits_remaining"] == 19
    assert "Clean body text." in body["markdown"]


def test_ssrf_blocks_private_ips(client: TestClient):
    r = client.post("/v1/keys/free")
    key = r.json()["api_key"]

    for bad_url in [
        "http://127.0.0.1:8080/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/admin",
        "file:///etc/passwd",
    ]:
        res = client.post(
            "/v1/extract",
            headers={"X-API-Key": key},
            json={"url": bad_url},
        )
        assert res.status_code == 400


def test_ssrf_blocks_private_redirect(client: TestClient):
    import httpx
    from app.extractor import ExtractionError, default_fetch_html

    async def run_check():
        redirect_resp = httpx.Response(
            302,
            headers={"location": "http://127.0.0.1:8080/health"},
            request=httpx.Request("GET", "https://example.com/start"),
        )
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=redirect_resp)):
            with pytest.raises(ExtractionError, match="Blocked non-public target IP"):
                await default_fetch_html("https://example.com/start")

    import asyncio
    asyncio.run(run_check())


def test_redeem_base_usdc_and_replay_protection(client: TestClient):
    tx_hash = "0x" + "ab" * 32
    base_usdc = config.CHAINS["base"].usdc_contracts[0]

    async def mock_rpc(rpc_url: str, method: str, params: list):
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "blockNumber": "0x10",
                "logs": [
                    {
                        "address": base_usdc,
                        "topics": [
                            TRANSFER_EVENT_TOPIC,
                            _padded_addr("0x1111111111111111111111111111111111111111"),
                            _padded_addr(config.RECEIVER_ADDRESS),
                        ],
                        # 2.50 USDC = 2_500_000 = 0x2625a0
                        "data": hex(2_500_000),
                    }
                ],
            }
        if method == "eth_blockNumber":
            return "0x12"
        raise AssertionError(f"Unexpected RPC method {method}")

    with patch("app.chain.default_rpc_call", new=mock_rpc):
        res = client.post(
            "/v1/billing/redeem",
            json={"chain": "base", "tx_hash": tx_hash},
        )
        assert res.status_code == 200
        payload = res.json()
        assert payload["amount_usdc"] == 2.5
        assert payload["credits_added"] == 2500
        assert payload["total_credits"] == 2500
        assert payload["api_key"].startswith("neon_")

        # Replay attempt must return 409 Conflict
        replay = client.post(
            "/v1/billing/redeem",
            json={"chain": "base", "tx_hash": tx_hash},
        )
        assert replay.status_code == 409


def test_redeem_topup_existing_key_polygon(client: TestClient):
    free_res = client.post("/v1/keys/free")
    existing_key = free_res.json()["api_key"]
    tx_hash = "0x" + "cd" * 32

    verified = VerifiedTransfer(
        chain="polygon",
        tx_hash=tx_hash,
        amount_usdc=1.0,
        credits=1000,
        confirmations=5,
    )
    with patch("app.main.verify_usdc_transfer", new=AsyncMock(return_value=verified)):
        res = client.post(
            "/v1/billing/redeem",
            headers={"X-API-Key": existing_key},
            json={"chain": "polygon", "tx_hash": tx_hash},
        )
    assert res.status_code == 200
    assert res.json()["total_credits"] == 1020
    assert "api_key" not in res.json()


def test_redeem_rejects_wrong_receiver(client: TestClient):
    tx_hash = "0x" + "ef" * 32
    base_usdc = config.CHAINS["base"].usdc_contracts[0]

    async def mock_rpc(rpc_url: str, method: str, params: list):
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "blockNumber": "0x10",
                "logs": [
                    {
                        "address": base_usdc,
                        "topics": [
                            TRANSFER_EVENT_TOPIC,
                            _padded_addr("0x1111111111111111111111111111111111111111"),
                            _padded_addr("0x2222222222222222222222222222222222222222"),
                        ],
                        "data": hex(5_000_000),
                    }
                ],
            }
        if method == "eth_blockNumber":
            return "0x15"
        raise AssertionError(f"Unexpected RPC method {method}")

    with patch("app.chain.default_rpc_call", new=mock_rpc):
        res = client.post(
            "/v1/billing/redeem",
            json={"chain": "base", "tx_hash": tx_hash},
        )
    assert res.status_code == 400
    assert "No USDC Transfer" in res.json()["detail"]


def test_skill_md_and_batch_extract_partial_success(client: TestClient):
    skill = client.get("/skill.md")
    assert skill.status_code == 200
    assert "neon-extract-api" in skill.text
    assert config.RECEIVER_ADDRESS in skill.text

    key = client.post("/v1/keys/free").json()["api_key"]

    async def fake_extract(url: str, include_links: bool = True):
        from app.extractor import ExtractionError

        if "bad" in url:
            raise ExtractionError("Blocked non-public target IP: 127.0.0.1")
        return ExtractedDocument(
            url=url,
            title="Good Page",
            description="Desc",
            markdown="# Good Page\n\nBody",
            word_count=3,
            char_count=17,
        )

    with patch("app.main.extract_url", new=fake_extract):
        res = client.post(
            "/v1/extract/batch",
            headers={"X-API-Key": key},
            json={"urls": ["https://example.com/ok", "http://127.0.0.1/bad"]},
        )
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert body["credits_charged"] == 1
    assert body["credits_remaining"] == 19
    assert body["results"][0]["ok"] is True
    assert body["results"][1]["ok"] is False


def test_structured_402_payment_required(client: TestClient):
    from app.db import deduct_credit

    key = client.post("/v1/keys/free").json()["api_key"]
    assert deduct_credit(config.DB_PATH, key, amount=20) == 0

    res = client.post(
        "/v1/extract",
        headers={"X-API-Key": key},
        json={"url": "https://example.com"},
    )
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert detail["error"] == "insufficient_credits"
    assert detail["receiver_address"] == config.RECEIVER_ADDRESS
    assert "base" in detail["chains"]
    assert "polygon" in detail["chains"]


def test_free_key_rate_limit_per_ip(client: TestClient):
    headers = {"X-Forwarded-For": "203.0.113.42"}
    for _ in range(config.FREE_KEYS_PER_IP_24H):
        ok = client.post("/v1/keys/free", headers=headers)
        assert ok.status_code == 200
        assert ok.json()["credits"] == 20

    blocked = client.post("/v1/keys/free", headers=headers)
    assert blocked.status_code == 429
    detail = blocked.json()["detail"]
    assert detail["error"] == "free_trial_rate_limited"
    assert detail["receiver_address"] == config.RECEIVER_ADDRESS


def test_dynamic_public_base_url_and_revenue_log(client: TestClient):
    proxy_headers = {"X-Forwarded-Proto": "https", "Host": "demo.app.lhr.life"}
    llms = client.get("/llms.txt", headers=proxy_headers)
    assert llms.status_code == 200
    assert "https://demo.app.lhr.life/v1/keys/free" in llms.text

    # Even if edge proxy sends http on .lhr.life, force https
    lhr_http_headers = {"X-Forwarded-Proto": "http", "Host": "demo.app.lhr.life"}
    llms_forced = client.get("/llms.txt", headers=lhr_http_headers)
    assert "https://demo.app.lhr.life/v1/keys/free" in llms_forced.text

    skill = client.get("/skill.md", headers=proxy_headers)
    assert skill.status_code == 200
    assert "https://demo.app.lhr.life/v1/extract/batch" in skill.text

    tx_hash = "0x" + "99" * 32
    verified = VerifiedTransfer(
        chain="base",
        tx_hash=tx_hash,
        amount_usdc=1.5,
        credits=1500,
        confirmations=3,
    )
    with patch("app.main.verify_usdc_transfer", new=AsyncMock(return_value=verified)):
        res = client.post("/v1/billing/redeem", json={"chain": "base", "tx_hash": tx_hash})
    assert res.status_code == 200
    assert os.path.exists(main.REVENUE_LOG_PATH)
    with open(main.REVENUE_LOG_PATH, "r", encoding="utf-8") as fh:
        lines = [line.strip() for line in fh if line.strip()]
    assert len(lines) == 1
    assert tx_hash in lines[0]


def test_agent_card_html_landing_and_eip681_uris(client: TestClient):
    card = client.get("/.well-known/agent.json", headers={"Host": "demo.app.lhr.life"})
    assert card.status_code == 200
    data = card.json()
    assert data["url"] == "https://demo.app.lhr.life"
    assert data["billing"]["receiverAddress"] == config.RECEIVER_ADDRESS
    assert "ethereum:" in data["billing"]["paymentUris1Usdc"]["base"]
    assert "@8453/transfer" in data["billing"]["paymentUris1Usdc"]["base"]

    html = client.get("/", headers={"Accept": "text/html", "Host": "demo.app.lhr.life"})
    assert html.status_code == 200
    assert "text/html" in html.headers["content-type"]
    assert config.RECEIVER_ADDRESS in html.text
    assert "https://demo.app.lhr.life/v1/keys/free" in html.text


def test_stats_x402_headers_max_chars_and_extract_links(client: TestClient):
    from app.extractor import html_to_clean_markdown

    title, desc, md, links = html_to_clean_markdown(
        "<html><head><title>T</title></head><body><main><p>Hello <a href='/docs'>Docs</a></p></main></body></html>",
        base_url="https://example.com/page",
        extract_links=True,
    )
    assert title == "T"
    assert links == ({"text": "Docs", "url": "https://example.com/docs"},)

    r = client.post("/v1/keys/free")
    key = r.json()["api_key"]

    fake_doc = ExtractedDocument(
        url="https://example.com/page",
        title="T",
        description="",
        markdown="A" * 200,
        word_count=1,
        char_count=200,
        links=({"text": "Docs", "url": "https://example.com/docs"},),
    )
    with patch("app.main.extract_url", new=AsyncMock(return_value=fake_doc)):
        ex = client.post(
            "/v1/extract",
            headers={"X-API-Key": key},
            json={"url": "https://example.com/page", "extract_links": True, "max_chars": 120},
        )
    assert ex.status_code == 200
    assert ex.json()["links"] == [{"text": "Docs", "url": "https://example.com/docs"}]

    stats = client.get("/v1/stats")
    assert stats.status_code == 200
    assert stats.json()["total_api_keys"] >= 1
    assert stats.json()["total_extractions_served"] >= 1

    # Batch of 10 with 19 credits works; 2nd + 3rd batch drains to <10 and triggers 402 with X-Payment-* headers
    with patch("app.main.extract_url", new=AsyncMock(return_value=fake_doc)):
        b1 = client.post(
            "/v1/extract/batch",
            headers={"X-API-Key": key},
            json={"urls": [f"https://example.com/{i}" for i in range(10)]},
        )
        assert b1.status_code == 200
        b2 = client.post(
            "/v1/extract/batch",
            headers={"X-API-Key": key},
            json={"urls": [f"https://example.com/{i}" for i in range(10)]},
        )
    assert b2.status_code == 402
    assert b2.headers["x-payment-address"] == config.RECEIVER_ADDRESS
    assert "base" in b2.headers["x-payment-chains"]


def test_mcp_server_endpoint(client: TestClient):
    init_res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_res.status_code == 200
    assert init_res.json()["result"]["serverInfo"]["name"] == "neon-api"

    list_res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert list_res.status_code == 200
    names = [t["name"] for t in list_res.json()["result"]["tools"]]
    assert {"billing_info", "redeem_usdc", "extract_markdown"} <= set(names)

    bill_res = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "billing_info", "arguments": {}}},
    )
    assert bill_res.status_code == 200
    text = bill_res.json()["result"]["content"][0]["text"]
    assert config.RECEIVER_ADDRESS in text

    key = client.post("/v1/keys/free").json()["api_key"]
    fake_doc = ExtractedDocument(
        url="https://example.com",
        title="Example",
        description="",
        markdown="# Example",
        word_count=2,
        char_count=9,
    )
    with patch("app.main.extract_url", new=AsyncMock(return_value=fake_doc)):
        ext_res = client.post(
            "/mcp",
            headers={"X-API-Key": key},
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "extract_markdown", "arguments": {"url": "https://example.com"}},
            },
        )
    assert ext_res.status_code == 200
    assert "# Example" in ext_res.json()["result"]["content"][0]["text"]


def test_rpc_failover_verifies_usdc_transfer():
    import asyncio
    from app.chain import verify_usdc_transfer

    calls: list[str] = []
    tx_hash = "0x" + "ab" * 32

    async def flaky_rpc(rpc_url: str, method: str, params: list):
        calls.append(rpc_url)
        if rpc_url == config.CHAINS["base"].rpc_urls[0]:
            raise RuntimeError("primary rpc timeout")
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "blockNumber": "0x10",
                "logs": [
                    {
                        "address": config.CHAINS["base"].usdc_contracts[0],
                        "topics": [
                            TRANSFER_EVENT_TOPIC,
                            _padded_addr("0x" + "11" * 20),
                            _padded_addr(config.RECEIVER_ADDRESS),
                        ],
                        "data": hex(2_500_000),  # 2.5 USDC
                    }
                ],
            }
        if method == "eth_blockNumber":
            return "0x12"
        raise AssertionError(f"unexpected method: {method}")

    verified = asyncio.run(verify_usdc_transfer("base", tx_hash, rpc_call=flaky_rpc))
    assert verified.amount_usdc == 2.5
    assert verified.credits == 2500
    assert config.CHAINS["base"].rpc_urls[0] in calls
    assert config.CHAINS["base"].rpc_urls[1] in calls


def test_mcp_self_onboarding_and_401_headers(client: TestClient):
    unauth = client.post("/v1/extract", json={"url": "https://example.com"})
    assert unauth.status_code == 401
    assert unauth.headers["x-free-trial-url"] == "/v1/keys/free"

    claim_res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "claim_free_key", "arguments": {}},
        },
    )
    assert claim_res.status_code == 200
    import json

    claimed = json.loads(claim_res.json()["result"]["content"][0]["text"])
    assert claimed["credits"] == 20
    key = claimed["api_key"]

    status_res = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "key_status", "arguments": {"api_key": key}},
        },
    )
    assert status_res.status_code == 200
    status_data = json.loads(status_res.json()["result"]["content"][0]["text"])
    assert status_data["credits"] == 20


def test_x402_challenge_get_and_post_conformance(client: TestClient):
    import base64
    import json

    headers = {"Host": "neon-api.example.workers.dev", "X-Forwarded-Proto": "https"}
    get_res = client.get("/v1/x402/extract?url=https://example.com", headers=headers)
    assert get_res.status_code == 402
    raw_header = get_res.headers["payment-required"]
    decoded = json.loads(base64.b64decode(raw_header).decode("utf-8"))
    assert decoded["x402Version"] == 2
    assert decoded["resource"]["url"] == "https://neon-api.example.workers.dev/v1/x402/extract?url=https://example.com"
    accept = decoded["accepts"][0]
    assert accept["scheme"] == "exact"
    assert accept["network"] == "eip155:8453"
    assert accept["amount"] == config.X402_PRICE_ATOMIC
    assert accept["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert accept["payTo"] == config.RECEIVER_ADDRESS_CHECKSUM
    assert accept["extra"] == {"name": "USD Coin", "version": "2"}
    assert "bazaar" in decoded["extensions"]

    post_res = client.post("/v1/x402/extract", headers=headers)
    assert post_res.status_code == 402
    post_decoded = json.loads(base64.b64decode(post_res.headers["payment-required"]).decode("utf-8"))
    assert post_decoded["resource"]["url"] == "https://neon-api.example.workers.dev/v1/x402/extract"
    assert post_decoded["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"


def test_x402_paid_extract_verify_settle_and_revenue_log(client: TestClient):
    import base64
    import json
    from app.x402 import encode_b64_json

    sig_header = encode_b64_json(
        {
            "x402Version": 2,
            "payload": {"signature": "0x" + "11" * 65, "authorization": {"from": "0x" + "22" * 20}},
        }
    )
    fake_doc = ExtractedDocument(
        url="https://example.com",
        title="Example Domain",
        description="",
        markdown="# Example Domain\n\nBody.",
        word_count=4,
        char_count=23,
    )
    tx_hash = "0x" + "77" * 32

    async def fake_post(url: str, payload: dict):
        if url.endswith("/verify"):
            return 200, {"isValid": True, "payer": "0x" + "22" * 20}
        if url.endswith("/settle"):
            return 200, {
                "success": True,
                "transaction": tx_hash,
                "network": "eip155:8453",
                "payer": "0x" + "22" * 20,
            }
        raise AssertionError(f"Unexpected URL: {url}")

    with (
        patch("app.x402.default_facilitator_post", new=fake_post),
        patch("app.main.extract_url", new=AsyncMock(return_value=fake_doc)),
    ):
        res = client.post(
            "/v1/x402/extract",
            headers={"PAYMENT-SIGNATURE": sig_header},
            json={"url": "https://example.com"},
        )

    assert res.status_code == 200
    body = res.json()
    assert "# Example Domain" in body["markdown"]
    assert body["payment"]["protocol"] == "x402"
    assert body["payment"]["transaction"] == tx_hash
    settlement = json.loads(base64.b64decode(res.headers["payment-response"]).decode("utf-8"))
    assert settlement["success"] is True
    assert settlement["transaction"] == tx_hash

    assert os.path.exists(main.REVENUE_LOG_PATH)
    with open(main.REVENUE_LOG_PATH, "r", encoding="utf-8") as fh:
        lines = [line.strip() for line in fh if line.strip()]
    assert len(lines) == 1
    assert tx_hash in lines[0]


def test_x402_facilitator_failover_and_invalid_signature(client: TestClient):
    import asyncio
    from app.x402 import (
        X402PaymentRejected,
        build_payment_requirements,
        encode_b64_json,
        verify_payment,
    )

    sig_header = encode_b64_json({"x402Version": 2, "payload": {"signature": "0xdead"}})
    reqs = build_payment_requirements()
    seen: list[str] = []

    async def failover_post(url: str, payload: dict):
        seen.append(url)
        if url.startswith("https://fac1.test"):
            return 503, {"error": "down"}
        return 200, {"isValid": True, "payer": "0xabc"}

    res = asyncio.run(
        verify_payment(
            sig_header,
            reqs,
            facilitator_urls=("https://fac1.test", "https://fac2.test"),
            post_fn=failover_post,
        )
    )
    assert res["facilitator_url"] == "https://fac2.test"
    assert seen == ["https://fac1.test/verify", "https://fac2.test/verify"]

    settle_called = False

    async def reject_post(url: str, payload: dict):
        nonlocal settle_called
        if url.endswith("/settle"):
            settle_called = True
        return 200, {"isValid": False, "invalidReason": "invalid_signature"}

    with patch("app.x402.default_facilitator_post", new=reject_post):
        bad = client.get(
            "/v1/x402/extract?url=https://example.com",
            headers={"PAYMENT-SIGNATURE": sig_header},
        )
    assert bad.status_code == 402
    assert bad.json()["error"] == "invalid_signature"
    assert settle_called is False


def test_x402_ssrf_rejected_before_settlement(client: TestClient):
    from app.x402 import encode_b64_json

    sig_header = encode_b64_json({"x402Version": 2, "payload": {"signature": "0x" + "11" * 65}})
    settle_called = False

    async def mock_post(url: str, payload: dict):
        nonlocal settle_called
        if url.endswith("/verify"):
            return 200, {"isValid": True, "payer": "0x" + "22" * 20}
        if url.endswith("/settle"):
            settle_called = True
            return 200, {"success": True, "transaction": "0x" + "99" * 32}
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("app.x402.default_facilitator_post", new=mock_post):
        res = client.get(
            "/v1/x402/extract?url=http://127.0.0.1:8080/secret",
            headers={"PAYMENT-SIGNATURE": sig_header},
        )
    assert res.status_code == 400
    assert settle_called is False
