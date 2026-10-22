"""Customer keys and the privacy of logs and traces."""

import base64
import logging

import pytest
from cryptography.exceptions import InvalidTag
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from mrp import secrets, telemetry
from tests.conftest import KEK1, KEK2, chat

pytestmark = pytest.mark.invariant
KMS1 = secrets.LocalKms({"v1": base64.b64decode(KEK1)}, "v1")
KMS2 = secrets.LocalKms({"v2": base64.b64decode(KEK2), "v1": base64.b64decode(KEK1)}, "v2")
SECRET = "sk-ant-api03-THIS-IS-A-CUSTOMER-KEY-0123456789abcdef"


def test_envelope_encryption_round_trip_and_tenant_binding():
    s = secrets.seal(KMS1, "ws-a", "closed-a", SECRET)
    assert SECRET.encode() not in s.ciphertext and s.fingerprint == "…cdef"
    assert secrets.open_sealed(KMS1, "ws-a", "closed-a", s) == SECRET
    with pytest.raises(InvalidTag):  # the same row copied into another workspace cannot be opened
        secrets.open_sealed(KMS1, "ws-b", "closed-a", s)
    with pytest.raises(InvalidTag):
        secrets.open_sealed(KMS1, "ws-a", "closed-b", s)


def test_rotation_rewraps_without_touching_the_secret():
    s = secrets.seal(KMS1, "ws-a", "closed-a", SECRET)
    r = secrets.rewrap(KMS2, "ws-a", "closed-a", s)
    assert r.kek_version == "v2" and r.ciphertext == s.ciphertext and r.wrapped_dek != s.wrapped_dek
    only_new = secrets.LocalKms({"v2": base64.b64decode(KEK2)}, "v2")  # the old KEK can now be retired
    assert secrets.open_sealed(only_new, "ws-a", "closed-a", r) == SECRET


def test_redaction_patterns():
    line = f"calling with {SECRET} and Bearer mrp_abcdefghijklmnopqrstuvwxyz and AKIAIOSFODNN7EXAMPLE"
    out = secrets.redact(line)
    assert SECRET not in out and "mrp_abcdef" not in out and "AKIA" not in out


async def test_customer_keys_and_content_never_reach_logs_or_traces(env, caplog):
    exporter = InMemorySpanExporter()
    provider = telemetry.setup(exporter=None)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    ws, h = await env.workspace(plan="team")
    r = await env.client.put("/v1/provider-keys/closed-a", json={"api_key": SECRET}, headers=h)
    assert r.status_code == 200 and r.json()["fingerprint"] == "…cdef"
    listed = (await env.client.get("/v1/provider-keys", headers=h)).json()
    assert SECRET not in str(listed)
    content = "Summarize this confidential board memo about the Project Falcon acquisition."
    caplog.set_level(logging.DEBUG)
    r = await env.client.post("/v1/chat/completions", json=chat(content, model="frontier-mid"), headers=h)
    assert r.status_code == 200
    logging.getLogger("mrp.gateway").info("debug dump %s", SECRET)  # a careless log line is still scrubbed
    text = caplog.text + str([s.attributes for s in exporter.get_finished_spans()])
    assert SECRET not in text and "Falcon" not in text and "confidential" not in text
    assert any(s.name == "provider.call" for s in exporter.get_finished_spans())
    async with env.db.owner() as conn:  # stored sealed, never in clear
        raw = await conn.fetchrow("SELECT ciphertext, wrapped_dek FROM mrp.provider_keys")
    assert SECRET.encode() not in bytes(raw["ciphertext"])


async def test_byok_is_a_plan_feature(env):
    _, h = await env.workspace(plan="free")
    r = await env.client.put("/v1/provider-keys/closed-a", json={"api_key": SECRET}, headers=h)
    assert r.status_code == 403


def test_scaleway_key_manager_adapter_wraps_through_the_service():
    import json

    import httpx

    store = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["X-Auth-Token"] == "scw-secret" and request.url.path.startswith(
            "/key-manager/v1alpha1/regions/fr-par/keys/k-1/"
        )
        if request.url.path.endswith("/encrypt"):
            ct = base64.b64encode(b"wrapped:" + base64.b64decode(body["plaintext"])).decode()
            store[ct] = body["associated_data"]
            return httpx.Response(200, json={"key_id": "k-1", "ciphertext": ct})
        if store.get(body["ciphertext"]) != body["associated_data"]:
            return httpx.Response(400, json={"message": "invalid associated data"})
        return httpx.Response(
            200,
            json={
                "key_id": "k-1",
                "plaintext": base64.b64encode(base64.b64decode(body["ciphertext"])[8:]).decode(),
            },
        )

    kms = secrets.ScalewayKms("k-1", "scw-secret", transport=httpx.MockTransport(handler))
    s = secrets.seal(kms, "ws-a", "closed-a", SECRET)
    assert s.kek_version == "scw:k-1" and secrets.open_sealed(kms, "ws-a", "closed-a", s) == SECRET
    with pytest.raises(httpx.HTTPStatusError):
        secrets.open_sealed(kms, "ws-b", "closed-a", s)
